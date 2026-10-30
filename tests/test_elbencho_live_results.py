#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the machine-readable live-client results objects."""

import csv
import hashlib
import importlib
import io
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone

import jsonschema
import pytest
import zstandard

from lib import elbencho_live_steady as steady_mod
from lib.elbencho_live_report import LIVE_CSV_COLUMNS, MIB, LiveClientId
from lib.elbencho_live_steady import (
    ClientSteady,
    LatencySummary,
    PhaseContext,
    SteadyAnalysis,
    SteadyWindow,
    WindowSeries,
    WindowSummary,
)

SIDECAR_PREFIX = "0001"
SCHEMA_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "docs",
    "schemas",
    "filesystem-batch-results-1.schema.json",
)


def _validate_live(obj):
    """Validate a live object against the published schema's live definition."""
    with open(SCHEMA_PATH, encoding="utf-8") as handle:
        schema = json.load(handle)
    wrapper = {
        "$schema": schema["$schema"],
        "$ref": "#/$defs/live",
        "$defs": schema["$defs"],
    }
    jsonschema.Draft202012Validator(wrapper).validate(obj)


def _stub_filesystem_results():
    """Minimal stand-in used only while lib.filesystem_results is absent."""
    stub = types.ModuleType("lib.filesystem_results")
    stub.LIVE_SCHEMA = "storage-scale-test/live-client-series"
    stub.LIVE_SCHEMA_VERSION = "1.1"
    stub.mib_s_to_bytes_per_s = lambda v: None if v is None else round(v * 1048576)

    def file_record(path, rel):
        with open(path, "rb") as handle:
            data = handle.read()
        return {
            "path": rel,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }

    class SidecarWriter:  # pylint: disable=missing-class-docstring
        def __init__(self, path, header):
            self.path, self.header = path, header
            self.lines = [header]
            self.sha256 = self.size_bytes = None

        def __enter__(self):
            return self

        def write_interval(self, elapsed_ms, rates, deltas=None):
            line = {"elapsed_ms": elapsed_ms, "bytes_per_s": rates}
            if deltas is not None:
                line["done_bytes_delta"] = deltas
            self.lines.append(line)

        def __exit__(self, *exc):
            text = "".join(
                json.dumps(x, separators=(",", ":")) + "\n" for x in self.lines
            )
            data = zstandard.ZstdCompressor().compress(text.encode())
            with open(self.path, "wb") as handle:
                handle.write(data)
            self.sha256 = hashlib.sha256(data).hexdigest()
            self.size_bytes = len(data)
            return False

    stub.file_record = file_record
    stub.SidecarWriter = SidecarWriter
    return stub


class _FakeSteady(SteadyAnalysis):
    """SteadyAnalysis whose derived statistics come from the test."""

    summaries: dict = {}
    counts: dict = {}

    def summary(self, label):
        return self.summaries.get(label)

    def class_counts(self):
        return dict(self.counts)


def _fake_steady(window=None, **extra):
    """A hand-built steady analysis (the real one is covered separately)."""
    result = _FakeSteady(
        window or SteadyWindow(reason=steady_mod.REASON_TOO_SHORT),
        PhaseContext(None, None, None, None),
        1000,
        5.0,
        3,
    )
    for name, value in extra.items():
        setattr(result, name, value)
    return result


@pytest.fixture(name="live")
def live_fixture(monkeypatch):
    """Import lib.elbencho_live_results, stubbing filesystem_results if absent.

    ``analyze_steady`` is replaced by one that reports an unavailable window;
    tests of the steady objects install their own.
    """
    try:
        importlib.import_module("lib.filesystem_results")
    except ImportError:
        monkeypatch.setitem(
            sys.modules, "lib.filesystem_results", _stub_filesystem_results()
        )
    sys.modules.pop("lib.elbencho_live_results", None)
    module = importlib.import_module("lib.elbencho_live_results")
    monkeypatch.setattr(module, "analyze_steady", lambda *a, **k: _fake_steady())
    yield module
    sys.modules.pop("lib.elbencho_live_results", None)


def _row(runtime_ms, rank, done_bytes, *, phase="WRITE", service="", mib_s="", iops=""):
    stamp = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc) + timedelta(
        milliseconds=runtime_ms
    )
    values = {name: "" for name in LIVE_CSV_COLUMNS}
    values.update(
        {
            "ISO Date": stamp.isoformat(timespec="milliseconds"),
            "Phase": phase,
            "RuntimeMS": str(runtime_ms),
            "Rank": str(rank),
            "DoneBytes": str(done_bytes),
            "MiB/s": str(mib_s),
            "IOPS": str(iops),
            "Service": service,
        }
    )
    return values


def _write_csv(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LIVE_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return str(path)


def _build_csv(path, per_client_mib, services=None, drop=None):
    """Rows for len(per_client_mib) clients over 4 one-second intervals.

    ``drop`` is a set of (interval, rank) samples to omit.
    """
    rows = []
    done = [0] * len(per_client_mib)
    for step in range(1, 5):
        total = 0
        for rank, rate in enumerate(per_client_mib):
            done[rank] += int(rate * MIB)
            total += done[rank]
        rows.append(
            _row(step * 1000, "Total", total, mib_s=sum(per_client_mib), iops=1234.5678)
        )
        for rank, _ in enumerate(per_client_mib):
            if drop and (step, rank) in drop:
                continue
            service = services[rank] if services else ""
            rows.append(_row(step * 1000, rank, done[rank], service=service))
    return _write_csv(path, rows)


def _build(live, path, level="summary", **overrides):
    """The first live object of each phase."""
    objects = _build_objects(live, path, level, **overrides)
    return objects.split(set(objects.by_phase))[0]


def _build_objects(live, path, level="summary", **overrides):
    args = {
        "live_csv_path": path,
        "relative_source_path": "g/live.csv",
        "level": level,
        "configured_interval_ms": 1000,
        "contexts": {},
        "classification": (5.0, 3),
        "selection_limits": (2, 3),
        "sidecar_dir": None,
        "sidecar_prefix": SIDECAR_PREFIX,
        "execution_id": "0001",
        "workers": None,
    }
    args.update(overrides)
    return live.build_live_objects(**args)


def _client(rank):
    # The label ("worker-3") is derived from an empty service and rank.
    return LiveClientId(rank, "")


def _columns(obj):
    cols = obj["clients_summary"]["columns"]
    return [dict(zip(cols, row)) for row in obj["clients_summary"]["rows"]]


def test_worker_mode_summary(live, tmp_path):
    path = _build_csv(tmp_path / "a.live.csv", [100, 200, 300])
    out = _build(live, path)
    assert list(out) == ["write"]
    obj = out["write"]
    assert obj["source"]["path"] == "g/live.csv"
    assert obj["source"]["size_bytes"] == (tmp_path / "a.live.csv").stat().st_size
    assert obj["client_mode"] == "worker"
    assert obj["domain"] == {"phase": "write", "mix_type": "", "occurrence": 0}
    assert obj["intervals"] == 4
    assert obj["clients"] == 3
    assert obj["client_series"] is None
    assert obj["aggregate_series"]["elapsed_ms"] == [1000, 2000, 3000, 4000]
    assert obj["aggregate_series"]["bytes_per_s"][0] == 600 * MIB
    assert obj["aggregate_series"]["ops_per_s"][0] == 1234.57
    assert obj["fleet_series"]["client_count"] == [3, 3, 3]
    assert obj["fleet_series"]["bytes_per_s_p50"] == [200 * MIB] * 3
    rows = _columns(obj)
    assert [r["client"] for r in rows] == ["worker-0", "worker-1", "worker-2"]
    assert [r["rank"] for r in rows] == [0, 1, 2]
    assert rows[0]["service"] is None
    assert rows[0]["kubernetes_node"] is None
    assert rows[2]["mean_bytes_per_s"] == 300 * MIB
    assert rows[0]["mean_ratio_to_fleet_median"] == 0.5
    assert rows[0]["class"] is None
    assert rows[0]["steady_ratio"] is None
    assert list(obj["diagnostics"]) == sorted(obj["diagnostics"])


def test_distribution_known_case(live, tmp_path):
    path = _build_csv(tmp_path / "d.live.csv", [100, 200, 300])
    dist = _build(live, path)["write"]["distribution"]
    means = dist["client_mean_bytes_per_s"]
    assert (means["min"], means["p50"], means["max"]) == (
        100 * MIB,
        200 * MIB,
        300 * MIB,
    )
    assert means["p10"] == 120 * MIB
    assert means["p90"] == 280 * MIB
    # Not pytest.approx: other tests stub numpy, which breaks its array probing.
    assert abs(means["cv"] - 0.408248) < 1e-6
    assert dist["min_over_median"] == 0.5
    assert dist["max_over_median"] == 1.5
    assert "jain_fairness" not in dist
    done = dist["final_done_bytes"]
    assert (done["min"], done["p50"], done["max"]) == (400 * MIB, 800 * MIB, 1200 * MIB)
    assert dist["finish_elapsed_ms"] == {"min": 4000, "p50": 4000, "max": 4000}
    assert "underperforming_clients" not in dist
    assert dist["persistent_clients"] is None
    assert dist["transient_clients"] is None


def test_finish_elapsed_is_last_change(live, tmp_path):
    rows = []
    for step, done_a in ((1, 100), (2, 200), (3, 200), (4, 200)):
        rows.append(_row(step * 1000, "Total", done_a + 50 * step, mib_s=1))
        rows.append(_row(step * 1000, 0, done_a))
        rows.append(_row(step * 1000, 1, 50 * step))
    obj = _build(live, _write_csv(tmp_path / "f.live.csv", rows))["write"]
    by_rank = {r["rank"]: r for r in _columns(obj)}
    assert by_rank[0]["final_done_bytes"] == 200
    assert by_rank[0]["finish_elapsed_ms"] == 2000
    assert by_rank[1]["final_done_bytes"] == 200
    assert by_rank[1]["finish_elapsed_ms"] == 4000
    assert obj["distribution"]["finish_elapsed_ms"]["min"] == 2000


def test_service_mode_and_workers_join(live, tmp_path):
    services = ["10.0.0.1:1611", "10.0.0.2"]
    path = _build_csv(tmp_path / "s.live.csv", [100, 100], services=services)
    workers = {"10.0.0.1": ("node-a", "pod-a"), "10.0.0.2": ("node-b", "pod-b")}
    obj = _build(live, path, workers=workers)["write"]
    assert obj["client_mode"] == "service"
    rows = _columns(obj)
    assert rows[0]["client"] == "10.0.0.1:1611 [rank 0]"
    assert rows[0]["service"] == "10.0.0.1:1611"
    assert (rows[0]["kubernetes_node"], rows[0]["kubernetes_pod"]) == (
        "node-a",
        "pod-a",
    )
    assert (rows[1]["kubernetes_node"], rows[1]["kubernetes_pod"]) == (
        "node-b",
        "pod-b",
    )
    unmatched = _build(live, path, workers={"1.2.3.4": ("n", "p")})["write"]
    assert _columns(unmatched)[0]["kubernetes_node"] is None


def test_load_workers_tsv(live, tmp_path):
    path = tmp_path / "x.workers.tsv"
    path.write_text("node\tpod\tpod_uid\tendpoint\nn1\tp1\tu1\t10.1.1.1\nbad\n")
    assert live.load_workers_tsv(str(path)) == {"10.1.1.1": ("n1", "p1")}
    assert live.load_workers_tsv(str(tmp_path / "missing")) == {}


def test_null_live_and_unreadable(live, tmp_path):
    assert live.null_live("not_captured") == {"reason": "not_captured"}
    with pytest.raises(live.LiveCSVError):
        _build(live, str(tmp_path / "missing.live.csv"))
    bad = tmp_path / "bad.live.csv"
    bad.write_text("a,b\n1,2\n")
    with pytest.raises(live.LiveCSVError):
        _build(live, str(bad))


def test_missing_samples_are_null_in_series(live, tmp_path):
    path = _build_csv(tmp_path / "m.live.csv", [100, 100], drop={(3, 1)})
    sidecar_dir = tmp_path / "side"
    obj = _build(live, path, level="full", sidecar_dir=str(sidecar_dir))["write"]
    rows = _columns(obj)
    assert rows[1]["missing_samples"] == 1
    assert obj["diagnostics"]["missing_samples"] == 1
    header, *intervals = _read_sidecar(sidecar_dir / "0001.write.jsonl.zst")
    assert header["clients"] == ["worker-0", "worker-1"]
    assert [i["bytes_per_s"][1] for i in intervals] == [None, 100 * MIB, None, None]
    assert intervals[0]["bytes_per_s"][0] is None


def _read_sidecar(path):
    data = zstandard.ZstdDecompressor().stream_reader(open(path, "rb")).read()
    return [json.loads(line) for line in io.StringIO(data.decode()) if line.strip()]


def test_selected_level_inline(live, tmp_path, monkeypatch):
    path = _build_csv(tmp_path / "e.live.csv", [100, 100, 100, 10])
    slow = _client("3")
    monkeypatch.setattr(
        live,
        "analyze_steady",
        lambda *a, **k: _fake_steady(
            SteadyWindow(1, 4, 1000, 4000),
            clients={slow: ClientSteady(slow, classification="persistent")},
        ),
    )
    monkeypatch.setattr(live, "classified_clients", lambda steady, limit: [slow])
    monkeypatch.setattr(live, "select_heatmap_rows", lambda steady, limit: [slow])
    obj = _build(live, path, level="selected")["write"]
    series = obj["client_series"]
    assert series["elapsed_ms"] == [1000, 2000, 3000, 4000]
    assert "worker-3" in series["clients"]
    assert 1 <= len(series["clients"]) <= 3
    assert len(series["bytes_per_s"]) == len(series["clients"])
    slow = series["bytes_per_s"][series["clients"].index("worker-3")]
    assert slow == [None, 10 * MIB, 10 * MIB, 10 * MIB]
    assert series["clients"] == ["worker-3"]


def test_full_level_sidecar(live, tmp_path):
    path = _build_csv(tmp_path / "g.live.csv", [100, 200, 300])
    sidecar_dir = tmp_path / "side"
    obj = _build(live, path, level="full", sidecar_dir=str(sidecar_dir))["write"]
    ref = obj["client_series"]
    stored = (sidecar_dir / ref["path"]).read_bytes()
    assert ref["path"] == "0001.write.jsonl.zst"
    assert ref["sha256"] == hashlib.sha256(stored).hexdigest()
    assert ref["size_bytes"] == len(stored)
    assert (ref["clients"], ref["intervals"]) == (3, 4)
    header, *intervals = _read_sidecar(sidecar_dir / ref["path"])
    assert header["execution_id"] == "0001"
    assert header["phase"] == "write"
    assert header["clients"] == ["worker-0", "worker-1", "worker-2"]
    assert [i["elapsed_ms"] for i in intervals] == [1000, 2000, 3000, 4000]
    assert intervals[1]["bytes_per_s"] == [100 * MIB, 200 * MIB, 300 * MIB]
    with pytest.raises(ValueError):
        _build(live, path, level="full")


def test_multiple_domains_and_phases(live, tmp_path):
    rows = []
    for phase in ("WRITE", "READ"):
        for step in range(1, 4):
            rows.append(_row(step * 1000, "Total", step * 10, phase=phase, mib_s=1))
            rows.append(_row(step * 1000, 0, step * 10, phase=phase))
    # second WRITE occurrence: runtime restarts
    for step in range(1, 3):
        rows.append(_row(step * 1000, "Total", step * 10, phase="WRITE", mib_s=1))
        rows.append(_row(step * 1000, 0, step * 10, phase="WRITE"))
    out = _build(live, _write_csv(tmp_path / "p.live.csv", rows))
    assert set(out) == {"write", "read"}
    assert out["write"]["diagnostics"]["additional_domains"] == 1
    assert "additional_domains" not in out["read"]["diagnostics"]


def test_every_domain_has_an_object_and_sidecar(live, tmp_path):
    rows = []
    for phase in ("WRITE", "SYNC", "READ"):
        for step in range(1, 4):
            rows.append(_row(step * 1000, "Total", step * 10, phase=phase, mib_s=1))
            if phase != "SYNC":
                rows.append(_row(step * 1000, 0, step * 10, phase=phase))
    for step in range(1, 3):  # a second WRITE occurrence: runtime restarts
        rows.append(_row(step * 1000, "Total", step * 10, phase="WRITE", mib_s=1))
        rows.append(_row(step * 1000, 0, step * 10, phase="WRITE"))
    sidecars = tmp_path / "side"
    context = PhaseContext(3000, 3000, 4096, None)
    objects = _build_objects(
        live,
        _write_csv(tmp_path / "p.live.csv", rows),
        level="full",
        sidecar_dir=str(sidecars),
        contexts={"write": context},
    )
    # Every domain is stored once: in phases[].live or among the others.
    phases, others = objects.split({"write", "read"})
    every = [*phases.values(), *others]
    domains = [(o["domain"]["phase"], o["domain"]["occurrence"]) for o in others]
    assert domains == [("sync", 0), ("write", 1)]
    assert sorted(phases) == ["read", "write"]
    assert phases["write"]["domain"]["occurrence"] == 0
    assert phases["write"]["diagnostics"]["additional_domains"] == 1
    assert "additional_domains" not in phases["read"]["diagnostics"]
    assert "additional_domains" not in others[1]["diagnostics"]
    assert sorted(o["client_series"]["path"] for o in every) == sorted(
        path.name for path in sidecars.iterdir()
    )
    assert others[1]["client_series"]["path"] == "0001.write.occ1.jsonl.zst"
    assert phases["read"]["phase_context"] is None
    assert others[0]["phase_context"] is None
    assert phases["write"]["phase_context"]["first_ms"] == 3000
    assert others[0]["clients"] == 0
    _, unpaired = objects.split(())
    assert [_domain(o) for o in unpaired] == [
        ("read", 0),
        ("sync", 0),
        ("write", 0),
        ("write", 1),
    ]
    header, *_ = _read_sidecar(sidecars / "0001.write.occ1.jsonl.zst")
    assert (header["phase"], header["mix_type"], header["occurrence"]) == (
        "write",
        "",
        1,
    )
    for obj in every:
        _validate_live(obj)


def _domain(obj):
    return obj["domain"]["phase"], obj["domain"]["occurrence"]


def test_sidecar_done_bytes_delta_rebuilds_counters(live, tmp_path):
    done = {0: [100, 300, 600, 650], 1: [50, None, 250, 40]}  # 1: missing, reset
    rows = []
    for step in range(4):
        rows.append(_row((step + 1) * 1000, "Total", 0, mib_s=1))
        for rank, values in done.items():
            if values[step] is not None:
                rows.append(_row((step + 1) * 1000, rank, values[step]))
    sidecars = tmp_path / "side"
    _build(
        live,
        _write_csv(tmp_path / "d.live.csv", rows),
        level="full",
        sidecar_dir=str(sidecars),
    )
    _, *intervals = _read_sidecar(sidecars / "0001.write.jsonl.zst")
    assert [i["done_bytes_delta"] for i in intervals] == [
        [100, 50],
        [200, None],
        [300, 200],
        [50, -210],
    ]
    rebuilt = {0: [], 1: []}
    for line in intervals:
        for rank, delta in enumerate(line["done_bytes_delta"]):
            last = next((v for v in reversed(rebuilt[rank]) if v is not None), 0)
            rebuilt[rank].append(None if delta is None else last + delta)
    assert rebuilt == done


def test_unpaired_sidecar_name_comes_from_the_csv(live, tmp_path):
    path = _build_csv(tmp_path / "run_20261026Z120000.live.csv", [100])
    sidecars = tmp_path / "side"
    obj = _build(
        live, path, level="full", sidecar_dir=str(sidecars), sidecar_prefix=None
    )["write"]
    digest = obj["source"]["sha256"][:12]
    assert obj["client_series"]["path"] == (
        f"run_20261026Z120000.{digest}.write.jsonl.zst"
    )


def test_wall_clock_start_and_skew(live, tmp_path):
    rows = []
    for step, skew in ((1, 0), (2, 0), (3, 1), (4, -1)):
        row = _row(step * 1000, "Total", step, mib_s=1, iops=2)
        row["ISO Date"] = f"2026-07-29T23:59:5{step + 5}.{skew + 5:03d}+0000"
        row["Lat Ent us"] = "7"
        rows.append(row)
        rows.append(_row(step * 1000, 0, step))
    rows[-2]["ISO Date"] = "not a date"
    obj = _build(live, _write_csv(tmp_path / "w.live.csv", rows))["write"]
    series = obj["aggregate_series"]
    assert series["start_time"] == "2026-07-29T23:59:56.005+0000"
    # Each ISO Date is its own clock: 1 s after 23:59:56.005 is 23:59:57.005.
    assert series["clock_skew_ms"] == [0, 0, 1, None]
    assert series["entries_latency_avg_us"] == [7.0] * 4
    assert live.live_timestamp(series["start_time"], 2000 + 1) == (
        "2026-07-29T23:59:58.006+0000"
    )
    assert live.live_timestamp("2026-07-29T23:59:59Z", 1500) == ("2026-07-30T00:00:00Z")
    assert live.live_timestamp("garbage", 0) is None
    first = _columns(obj)[0]
    assert first["first_progress_elapsed_ms"] == 1000


def test_deterministic(live, tmp_path):
    path = _build_csv(tmp_path / "t.live.csv", [100, 150, 90, 20])
    first = _build(live, path, level="full", sidecar_dir=str(tmp_path / "s1"))
    second = _build(live, path, level="full", sidecar_dir=str(tmp_path / "s2"))
    assert json.dumps(first, sort_keys=False) == json.dumps(second, sort_keys=False)


def test_empty_domain_guards(live):
    dist = live._distribution([], _fake_steady())  # pylint: disable=protected-access
    assert dist["min_over_median"] is None
    assert dist["client_mean_bytes_per_s"]["cv"] is None
    assert dist["finish_elapsed_ms"]["p50"] is None


def _window_series(label, length, ends, jains, in_steady):
    count = len(ends)
    return WindowSeries(
        label,
        length,
        length * 1000,
        end_interval=list(range(count)),
        end_ms=list(ends),
        clients=[3] * count,
        jain=list(jains),
        median_bytes=[1.0] * count,
        min_over_median=[1.0] * count,
        floor=[0.99] * count,
        in_steady=list(in_steady),
    )


def test_steady_fairness_latency_objects(live, tmp_path, monkeypatch):
    path = _build_csv(tmp_path / "x.live.csv", [100, 200, 300])
    series = _window_series(
        "5s", 5, [4000, 5000, 6000], [0.9, 0.97123456789, 0.98], [False, True, True]
    )
    summary = WindowSummary(
        "5s", 5000, 2, 0.97123456789, 0.97123456789, 0.975, 5000, 0.5, 0.91, 0.99
    )
    latency = LatencySummary(
        100.04, 120.0, 250.0, 2.50001, 0.1234567, 101.0, 99.5, -0.31234, None
    )
    slow = LiveClientId("0", "")
    clients = {
        slow: ClientSteady(
            slow,
            steady_mean_mib_s=90.0,
            steady_ratio=0.9,
            worst_5s_ratio=0.81,
            worst_5s_ms=5000,
            windows_5s=2,
            short_5s=1,
            classification="persistent",
        )
    }
    fake = _fake_steady(
        SteadyWindow(1, 4, 1000, 4000),
        series={"5s": series},
        clients=clients,
        latency=latency,
    )
    fake.summaries = {"5s": summary}
    fake.counts = {"persistent": 1, "transient": 2, "ok": 0}
    seen = {}

    def fake_analyze(*args):
        seen["args"] = args
        return fake

    monkeypatch.setattr(live, "analyze_steady", fake_analyze)
    context = PhaseContext(4000, 5000, 1048576, 99.5)
    obj = _build(live, path, contexts={"write": context}, classification=(7.5, 4))[
        "write"
    ]
    assert seen["args"][2:] == (context, 1000, 7.5, 4)
    assert list(obj)[:10] == [
        "source",
        "configured_interval_ms",
        "client_mode",
        "domain",
        "phase_context",
        "intervals",
        "clients",
        "steady",
        "aggregate_series",
        "fleet_series",
    ]
    assert list(obj)[10:13] == ["fairness", "latency", "distribution"]
    assert obj["phase_context"] == {
        "first_ms": 4000,
        "last_ms": 5000,
        "block_size_bytes": 1048576,
        "final_avg_latency_us": 99.5,
    }
    assert obj["steady"] == {
        "start_ms": 1000,
        "end_ms": 4000,
        "end_source": "elbencho_first",
        "reason": None,
        "interval_ms": 1000,
    }
    assert obj["aggregate_series"]["io_latency_avg_us"] == [None] * 4
    fairness = obj["fairness"]
    assert fairness["interval"] is None and fairness["30s"] is None
    five = fairness["5s"]
    assert (five["window_ms"], five["windows"], five["jain_min_at_ms"]) == (
        5000,
        2,
        5000,
    )
    assert five["jain"] == {"min": 0.971235, "p10": 0.971235, "p50": 0.975}
    assert five["share_below_0_95"] == 0.5
    assert five["series"] == {
        "end_ms": [5000, 6000],
        "clients": [3, 3],
        "jain": [0.971235, 0.98],
    }
    assert obj["latency"] == {
        "p50_us": 100.0,
        "p90_us": 120.0,
        "max_us": 250.0,
        "max_over_p50": 2.5,
        "cv": 0.123457,
        "steady_mean_us": 101.0,
        "final_avg_us": 99.5,
        "rho_jain_latency": -0.3123,
        "rho_latency_throughput": None,
    }
    assert obj["distribution"]["persistent_clients"] == 1
    assert obj["distribution"]["transient_clients"] == 2
    first = _columns(obj)[0]
    assert first["class"] == "persistent"
    assert first["steady_bytes_per_s"] == 90 * MIB
    assert first["steady_ratio"] == 0.9
    assert first["worst_5s_ratio"] == 0.81
    assert first["worst_5s_elapsed_ms"] == 5000
    assert first["short_5s_share"] == 0.5
    assert (first["short_5s_windows"], first["steady_5s_windows"]) == (1, 2)
    assert _columns(obj)[1]["class"] is None
    assert list(_columns(obj)[0]) == live.CLIENT_COLUMNS
    _validate_live(obj)


def test_unavailable_steady_window(live, tmp_path, monkeypatch):
    path = _build_csv(tmp_path / "u.live.csv", [100, 100])
    for reason, source in (
        ("too_short", "elbencho_first"),
        ("no_first_time", None),
    ):
        window = SteadyWindow(reason=reason)
        monkeypatch.setattr(
            live, "analyze_steady", lambda *a, w=window: _fake_steady(w)
        )
        obj = _build(live, path)["write"]
        assert obj["steady"]["reason"] == reason
        assert obj["steady"]["end_source"] == source
        assert obj["fairness"] is None
        assert obj["latency"] is None
        assert obj["distribution"]["persistent_clients"] is None
        _validate_live(obj)


def test_default_context_when_phase_missing(live, tmp_path, monkeypatch):
    path = _build_csv(tmp_path / "c.live.csv", [100, 100])
    seen = []
    monkeypatch.setattr(
        live, "analyze_steady", lambda *a: seen.append(a[2]) or _fake_steady()
    )
    _build(live, path, contexts={"read": PhaseContext(1, 2, 3, 4.0)})
    assert seen == [PhaseContext(None, None, None, None)]


def test_real_steady_analysis_end_to_end(live, tmp_path, monkeypatch):
    """Uses the real analyze_steady: a 20 s phase with one slow client."""
    monkeypatch.setattr(live, "analyze_steady", steady_mod.analyze_steady)
    rates = [100, 100, 100, 50]
    rows = []
    done = [0] * len(rates)
    for step in range(1, 21):
        for rank, rate in enumerate(rates):
            done[rank] += int(rate * MIB)
        rows.append(
            _row(
                step * 1000,
                "Total",
                sum(done),
                mib_s=sum(rates),
                iops=sum(rates),
            )
        )
        rows.extend(_row(step * 1000, r, done[r]) for r in range(len(rates)))
    path = _write_csv(tmp_path / "r.live.csv", rows)
    context = PhaseContext(20000, 20000, MIB, None)
    obj = _build(live, path, contexts={"write": context})["write"]
    assert obj["steady"]["reason"] is None
    assert obj["steady"]["end_ms"] == 20000
    assert obj["fairness"]["5s"]["windows"] > 0
    assert obj["distribution"]["persistent_clients"] == 1
    classes = {r["client"]: r["class"] for r in _columns(obj)}
    assert classes["worker-3"] == "persistent"
    assert classes["worker-0"] == "ok"
    assert obj["aggregate_series"]["io_latency_avg_us"] == [None] * 20


def _analysis_state(domain):
    """Everything the live reports read from a first-pass domain analysis."""
    clients = {}
    for client, summary in domain.clients.items():
        values = dict(vars(summary))
        values["median"] = values.pop("median_estimator").value()
        clients[client] = values
    return (
        domain.key,
        domain.aggregate_points,
        domain.fleet_points,
        domain.interval_times,
        domain.runtime_to_interval,
        clients,
    )


def test_document_replay_matches_the_live_csv(live, tmp_path, monkeypatch):
    """A full live object and its sidecar replay the live CSV's analysis."""
    monkeypatch.setattr(live, "analyze_steady", steady_mod.analyze_steady)
    done = {0: [100, 300, 600, 650, 900], 1: [50, None, 250, 40, 90]}
    rows = []
    for step in range(5):
        rows.append(_row((step + 1) * 1000, "Total", 0, mib_s=1.5, iops=2))
        rows[-1]["Lat IO us"] = str(400 + step)
        rows.extend(
            _row((step + 1) * 1000, rank, values[step], service=f"host{rank}:1611")
            for rank, values in done.items()
            if values[step] is not None
        )
    rows.append(_row(1000, 0, 7, phase="READ"))
    path = _write_csv(tmp_path / "x_20261026Z120000.live.csv", rows)
    sidecars = tmp_path / "side"
    objects = _build_objects(live, path, level="full", sidecar_dir=str(sidecars))
    metadata = live.LiveFileMetadata(path, "", 0, 0, 0, "")
    original = live.analyze_live_csv(metadata)
    for obj in [*objects.by_phase.values(), *objects.later]:
        sidecar = str(sidecars / obj["client_series"]["path"])
        analysis, domain = live.analyze_document_live(obj, sidecar, metadata)
        expected = original.domains[live.live_domain_key(obj)]
        assert _analysis_state(domain) == _analysis_state(expected)
        context = live.document_phase_context(obj)
        replayed = steady_mod.analyze_steady(analysis, domain, context, 1000)
        assert replayed == steady_mod.analyze_steady(original, expected, context, 1000)
