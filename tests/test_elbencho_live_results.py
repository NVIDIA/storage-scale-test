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
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
import zstandard

from lib.elbencho_live_report import LIVE_CSV_COLUMNS, MIB

SIDECAR_PREFIX = "0001"


def _stub_filesystem_results():
    """Minimal stand-in used only while lib.filesystem_results is absent."""
    stub = types.ModuleType("lib.filesystem_results")
    stub.LIVE_SCHEMA = "storage-scale-test/live-client-series"
    stub.LIVE_SCHEMA_VERSION = "1.0"
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

        def write_interval(self, elapsed_ms, rates):
            self.lines.append({"elapsed_ms": elapsed_ms, "bytes_per_s": rates})

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


@pytest.fixture(name="live")
def live_fixture(monkeypatch):
    """Import lib.elbencho_live_results, stubbing filesystem_results if absent."""
    try:
        importlib.import_module("lib.filesystem_results")
    except ImportError:
        monkeypatch.setitem(
            sys.modules, "lib.filesystem_results", _stub_filesystem_results()
        )
    sys.modules.pop("lib.elbencho_live_results", None)
    module = importlib.import_module("lib.elbencho_live_results")
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
    args = {
        "live_csv_path": path,
        "relative_source_path": "g/live.csv",
        "level": level,
        "configured_interval_ms": 1000,
        "z_threshold": 2.0,
        "selection_limits": (2, 3),
        "min_underperform_segments": 1,
        "sidecar_dir": None,
        "sidecar_prefix": SIDECAR_PREFIX,
        "execution_id": "0001",
        "workers": None,
    }
    args.update(overrides)
    return live.build_live_objects(**args)


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
    assert dist["underperforming_clients"] == 0


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


def test_selected_level_inline(live, tmp_path):
    path = _build_csv(tmp_path / "e.live.csv", [100, 100, 100, 10])
    obj = _build(live, path, level="selected")["write"]
    series = obj["client_series"]
    assert series["elapsed_ms"] == [1000, 2000, 3000, 4000]
    assert "worker-3" in series["clients"]
    assert 1 <= len(series["clients"]) <= 3
    assert len(series["bytes_per_s"]) == len(series["clients"])
    slow = series["bytes_per_s"][series["clients"].index("worker-3")]
    assert slow == [None, 10 * MIB, 10 * MIB, 10 * MIB]


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


def test_deterministic(live, tmp_path):
    path = _build_csv(tmp_path / "t.live.csv", [100, 150, 90, 20])
    first = _build(live, path, level="full", sidecar_dir=str(tmp_path / "s1"))
    second = _build(live, path, level="full", sidecar_dir=str(tmp_path / "s2"))
    assert json.dumps(first, sort_keys=False) == json.dumps(second, sort_keys=False)


def test_empty_domain_guards(live):
    dist = live._distribution([])  # pylint: disable=protected-access
    assert dist["min_over_median"] is None
    assert dist["client_mean_bytes_per_s"]["cv"] is None
    assert dist["finish_elapsed_ms"]["p50"] is None
