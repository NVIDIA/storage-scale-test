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

"""Tests for the elbencho IO results fragment and document export."""

import csv
import json
import sys
from pathlib import Path

import pytest

from lib import elbencho_live_results as live_results
from lib.elbencho_live_report import LIVE_CSV_COLUMNS
from tests.extract_elbencho_test_support import load_extract_elbencho_module
from tests.test_extract_elbencho_resume import _write_execution_metadata
from tests.test_extract_mdtest_elbencho import _CSV_FIELDS as MDTEST_CSV_FIELDS
from tests.test_filesystem_results_schema import validate_results_document

_EXTRACT = load_extract_elbencho_module("extract_elbencho_results_json")

_DS = "20261026Z120000"
_PERCENTILES = "[ 1%<=88 50%<=512 75%<=704 99%<=3072 ]"
_HISTOGRAM = "[ 64: 13, 128: 9921, 256: 7 ]"
# Elbencho's labeled result CSV; IO runs have no entry-latency columns.
_CSV_FIELDS = [field for field in MDTEST_CSV_FIELDS if not field.startswith("Ent lat")]
_ENV_USED = (
    "ELBENCHO_LIVEINT: 500\n"
    'KUBECTL_MAPPED_READ_FROM: "/mnt/mapped/dataset"\n'
    "EXECUTION_SUBSTRATE: ssh\n"
)


def _phase_row(operation, direct, rand, first, last, command):
    """One labeled CSV row; first/last are (elapsed_ms, iops, mib_s)."""
    row = dict.fromkeys(_CSV_FIELDS, "0")
    row.update(
        {
            "ISO date": "2026-10-26T12:00:00-0700",
            "file size": "1073741824",
            "direct IO": "1" if direct else "0",
            "random": "1" if rand else "0",
            "operation": operation,
            "time ms [first]": str(first[0]),
            "time ms [last]": str(last[0]),
            "IOPS [first]": str(first[1]),
            "IOPS [last]": str(last[1]),
            "MiB/s [first]": str(first[2]),
            "MiB/s [last]": str(last[2]),
            "IO lat us [min]": "41",
            "IO lat us [avg]": "627.4",
            "IO lat us [max]": "18233",
            "command": command,
        }
    )
    return row


def _out_section(operation, percentiles=True, histogram=True):
    lines = [
        f"{operation:<12}Elapsed time     :     59.874s     60.311s",
        "            IOPS             :      812345      799112",
    ]
    if percentiles:
        lines.append(f"            IO lat % us      : {_PERCENTILES}")
    if histogram:
        lines.append(f"            IO lat hist      : {_HISTOGRAM}")
    return "\n".join(lines) + "\n---\n"


def _write_result(
    result_dir,
    *,
    nodes=1,
    io_size="1M",
    threads=4,
    depth=1,
    phases=("WRITE", "READ"),
    direct=True,
    percentiles=True,
    extra_out="",
    rand_phases=(),
):
    """Write one real-format .csv/.out pair; return its base path."""
    base = result_dir / (
        f"elbencho-{io_size}-c_{nodes:03d}-s_{threads:03d}-d_{depth:03d}_{_DS}"
    )
    rows = []
    for operation in phases:
        command = (
            f'"elbencho" "--hosts" "h1,h2" "--{operation.lower()}" "--timelimit" "60"'
        )
        rows.append(
            _phase_row(
                operation,
                direct,
                operation in rand_phases,
                (59874, 812345.04, 3173.0),
                (60311, 799112.0, 3121.5),
                command,
            )
        )
    with open(f"{base}.csv", "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, _CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    out = extra_out + "".join(
        _out_section(operation, percentiles) for operation in phases
    )
    Path(f"{base}.out").write_text(out, encoding="utf-8")
    return base


def _write_ledger(result_dir, execution_id, io_size="1M", threads=4, **exports):
    """Write executions/NNNN.sh and NNNN.status for one execution."""
    folder = result_dir / "executions"
    folder.mkdir(exist_ok=True)
    values = {
        "dio_or_bio": "dio",
        "use_random": "0",
        "run_to_completion": "0",
        "ELBENCHO_SWEEP_WRITE_ONLY": "0",
        "ELBENCHO_SWEEP_WRITE_NO_READ": "0",
        "ELBENCHO_SWEEP_READ_FROM": "''",
        "ELBENCHO_FILE_LAYOUT": "worker-directories",
        "ELBENCHO_SINGLE_BIG_FILE": "0",
        "ELBENCHO_ALL_NODES_ACCESS_ALL_DATA": "0",
    }
    values.update(exports)
    lines = [
        "# Auto-generated; do not edit. "
        f"Coords: nodes=1 io_size={io_size} thread_count={threads} io_depth=1",
        "export ELBENCHO_EXECUTION_KIND=io",
        "export nodes=1",
        f'export io_size="{io_size}"',
        *(f"export {name}={value}" for name, value in values.items()),
    ]
    (folder / f"{execution_id}.sh").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    (folder / f"{execution_id}.status").write_text("SUCCESS\n", encoding="utf-8")


def _live_csv(path, clients=3, intervals=4, phase="WRITE"):
    """Write an extended live CSV: Total rows plus one row per client."""
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, LIVE_CSV_COLUMNS)
        writer.writeheader()
        for step in range(1, intervals + 1):
            for rank in ["Total", *range(clients)]:
                row = dict.fromkeys(LIVE_CSV_COLUMNS, "")
                row.update(
                    {
                        "ISO Date": f"2026-10-26T12:00:{step:02d}.000+00:00",
                        "Phase": phase,
                        "RuntimeMS": str(step * 1000),
                        "Rank": str(rank),
                        "DoneBytes": str(
                            step * 1048576 * (clients if rank == "Total" else 1)
                        ),
                        "MiB/s": str(100 * (clients if rank == "Total" else 1)),
                        "IOPS": "100",
                        "Service": "" if rank == "Total" else f"host{rank}",
                    }
                )
                writer.writerow(row)


def _live_request(level="summary", sidecar=None):
    return _EXTRACT.LiveRequest(
        level=level,
        shortfall_percent=5.0,
        min_slow_windows=3,
        selection_limits=(8, 8),
        sidecar_dir=sidecar,
        configured_interval_ms=500,
    )


def _fragment(result_dir, *, filters=None, level="summary", sidecar=None):
    """Parse a directory the way main() does and build its fragment."""
    pairs = {}
    for csv_path in sorted(result_dir.glob("*.csv")):
        if csv_path.name.endswith(".live.csv"):
            continue
        base = str(csv_path)[: -len(".csv")]
        pairs[base] = _EXTRACT.parse_elbencho_files(base)
    env = {"ELBENCHO_LIVEINT": 500, "KUBECTL_MAPPED_READ_FROM": "/mnt/mapped/dataset"}
    metrics = [metric for items in pairs.values() for metric in items]
    _EXTRACT.apply_execution_workloads(str(result_dir), metrics)
    data = _EXTRACT.IoResultsInput(
        str(result_dir),
        env,
        filters or _EXTRACT.ReportFilters(),
        _live_request(level, sidecar),
        pairs,
    )
    return _EXTRACT.build_io_fragment(data)


def _entry(fragment, execution_id):
    return next(e for e in fragment["executions"] if e["execution_id"] == execution_id)


def _phase(entry, name):
    return next(p for p in entry["results"]["phases"] if p["phase"] == name)


def test_write_read_split_spec_direct_and_buffered(tmp_path):
    _write_ledger(tmp_path, "0001", io_size="1M,r4K", threads=4)
    _write_ledger(tmp_path, "0002", io_size="1M,r4K", threads=8, dio_or_bio="bio")
    _write_result(tmp_path, io_size="1M,r4K", threads=4, rand_phases=("READ",))
    _write_result(
        tmp_path, io_size="1M,r4K", threads=8, direct=False, rand_phases=("READ",)
    )
    fragment = _fragment(tmp_path)

    direct = _entry(fragment, "0001")
    assert direct["coordinate_key"] == "io/c001/s004/d001/w1M-seq/r4K-rand/dio"
    assert direct["coordinates"] == {
        "nodes": 1,
        "threads_per_node": 4,
        "io_depth": 1,
        "io_size_spec": "1M,r4K",
        "phase_plan": "write_read",
        "write_block_size_bytes": 1048576,
        "write_access": "sequential",
        "read_block_size_bytes": 4096,
        "read_access": "random",
        "direct_io": True,
        "run_to_completion": False,
        "file_layout": "worker-directories",
        "single_big_file": False,
        "all_nodes_all_data": False,
    }
    assert direct["result_state"] == "complete"
    assert direct["problems"] == []
    assert [p["phase"] for p in direct["results"]["phases"]] == ["write", "read"]
    assert direct["results"]["workload"] is None
    assert [r["path"] for r in direct["result_files"]] == [
        f"elbencho-1M,r4K-c_001-s_004-d_001_{_DS}.csv",
        f"elbencho-1M,r4K-c_001-s_004-d_001_{_DS}.out",
    ]
    buffered = _entry(fragment, "0002")
    assert buffered["coordinate_key"].endswith("/bio")
    assert buffered["coordinates"]["direct_io"] is False


def test_first_last_latency_and_histogram(tmp_path):
    _write_ledger(tmp_path, "0001")
    _write_result(tmp_path)
    write = _phase(_entry(_fragment(tmp_path), "0001"), "write")

    assert write["elbencho_first"] == {
        "ops_per_s": 812345.04,
        "bytes_per_s": round(3173.0 * 1048576),
        "elapsed_ms": 59874,
    }
    assert write["elbencho_last"] == {
        "ops_per_s": 799112.0,
        "bytes_per_s": round(3121.5 * 1048576),
        "elapsed_ms": 60311,
    }
    assert write["latency_us"] == {
        "min": 41.0,
        "avg": 627.4,
        "max": 18233.0,
        "p1": 88.0,
        "p50": 512.0,
        "p75": 704.0,
        "p99": 3072.0,
    }
    assert write["histogram_us"] == {
        "buckets_us": [64.0, 128.0, 256.0],
        "counts": [13, 9921, 7],
    }
    assert write["configured_time_limit_s"] == 60
    assert write["command"][:3] == ["elbencho", "--hosts", "h1,h2"]
    assert write["completion_state"] is None
    assert write["work"] is None


def test_headerless_csv_last_columns():
    row = (
        "2026-07-22T10:48:37-0700,0,0,0,0,0,0,0,0,WRITE,60244,60261,35,37,"
        "652998,658022,652998.5,658022,0,0,0,0,0,0,0,0,0,0,0,0,2220,3640,"
        '3210000,1,0,"elbencho --write --direct --size=1G"'
    )
    parsed = getattr(_EXTRACT, "_elbencho_headerless_row_to_dict")(
        next(csv.reader([row]))
    )
    metrics = []
    getattr(_EXTRACT, "_try_append_elbencho_metric_from_csv_row")(
        parsed,
        {"io_size": "1G", "nodes": 1, "threads": 1, "io_depth": 1, "datestamp": _DS},
        metrics,
    )
    assert metrics[0].iops == 652998.0
    assert metrics[0].iops_last == 658022.0
    assert metrics[0].throughput_mib_s_last == 658022.0
    assert metrics[0].phase_wall_duration_ms == 60261


def test_missing_percentiles_are_null_and_flagged(tmp_path):
    _write_ledger(tmp_path, "0001")
    _write_result(tmp_path, percentiles=False)
    entry = _entry(_fragment(tmp_path), "0001")

    write = _phase(entry, "write")
    assert {write["latency_us"][k] for k in ("p1", "p50", "p75", "p99")} == {None}
    assert write["latency_us"]["avg"] == 627.4
    codes = [(p["code"], p["phase"]) for p in entry["problems"]]
    assert codes == [("percentiles_missing", "write"), ("percentiles_missing", "read")]
    assert entry["result_state"] == "partial"


def test_percentile_parsing_keeps_existing_table_fields(tmp_path):
    _write_result(tmp_path)
    base = str(next(tmp_path.glob("*.csv")))[:-4]
    metric = _EXTRACT.parse_elbencho_files(base)[0]
    assert abs(metric.lat_pct_99 - 0.003072) < 1e-9
    assert metric.lat_pct_found == frozenset({"1", "50", "75", "99"})


def test_write_only_plan_and_data_dir(tmp_path):
    _write_ledger(tmp_path, "0001", ELBENCHO_SWEEP_WRITE_ONLY="1")
    _write_result(
        tmp_path,
        phases=("WRITE",),
        extra_out="ELBENCHO_WRITE_ONLY_DATA_DIR=/mnt/x/data\n",
    )
    entry = _entry(_fragment(tmp_path), "0001")

    assert entry["coordinates"]["phase_plan"] == "write_only"
    assert entry["coordinates"]["read_block_size_bytes"] is None
    assert entry["coordinates"]["read_access"] is None
    assert entry["coordinate_key"] == "io/c001/s004/d001/w1M-seq/rnone/dio"
    assert entry["dataset"]["write_only_data_dir"] == "/mnt/x/data"
    assert entry["dataset"]["source"] == "generated"
    assert entry["result_state"] == "complete"


def test_write_phase_missing_is_partial(tmp_path):
    _write_ledger(tmp_path, "0001")
    _write_result(tmp_path, phases=("WRITE",))
    entry = _entry(_fragment(tmp_path), "0001")

    assert [(p["code"], p["phase"]) for p in entry["problems"]] == [
        ("phase_missing", "read")
    ]
    assert entry["result_state"] == "partial"


def test_read_from_staged_tree_dataset(tmp_path):
    _write_execution_metadata(
        tmp_path,
        "0001",
        layout="worker-directories",
        count_source="treefile",
        treefile_source="cache_hit",
        publish_outcome="reused",
        requested="null",
        effective="null",
        files="8",
        byte_count="8192",
        write_state="not_applicable",
        delete_state="not_applicable",
    )
    # The metadata helper writes a bare NNNN.sh; replace it with the full ledger.
    _write_ledger(
        tmp_path, "0001", ELBENCHO_SWEEP_READ_FROM="/lustre/a/datasets/imagenet"
    )
    treescan = "Treescan file sizes (bytes): count=8 avg=1024 min=512 max=2048\n"
    _write_result(tmp_path, phases=("READ",), extra_out=treescan)
    entry = _entry(_fragment(tmp_path), "0001")

    assert entry["coordinates"]["phase_plan"] == "read_from"
    assert entry["coordinates"]["write_block_size_bytes"] is None
    assert entry["coordinate_key"] == "io/c001/s004/d001/wnone/r1M-seq/dio"
    assert entry["dataset"] == {
        "source": "read_from",
        "read_from": "/lustre/a/datasets/imagenet",
        "kubernetes_mapped_read_from": "/mnt/mapped/dataset",
        "write_only_data_dir": None,
        "layout": "staged-tree",
        "files_total": 8,
        "bytes_total": 8192,
        "treefile_source": "cache_hit",
        "scanned_file_sizes": {
            "count": 8,
            "avg_bytes": 1024,
            "min_bytes": 512,
            "max_bytes": 2048,
        },
    }
    assert entry["result_state"] == "complete"
    assert [p["phase"] for p in entry["results"]["phases"]] == ["read"]


def test_delete_phase_and_workload_from_workload_tsv(tmp_path):
    _write_execution_metadata(tmp_path, "0001", layout="shared-directory")
    _write_ledger(tmp_path, "0001", ELBENCHO_FILE_LAYOUT="shared-directory")
    _write_result(tmp_path, io_size="1M", threads=4)
    entry = _entry(_fragment(tmp_path), "0001")

    assert entry["dataset"]["layout"] == "shared-directory"
    assert entry["dataset"]["scanned_file_sizes"] is None
    phases = {p["phase"]: p for p in entry["results"]["phases"]}
    assert list(phases) == ["write", "read", "delete"]
    assert phases["write"]["completion_state"] == "completed"
    assert phases["write"]["work"] == {
        "expected_files": 4800,
        "completed_files": 4800,
        "expected_bytes": 322122547200,
        "completed_bytes": 322122547200,
    }
    assert phases["delete"] == {
        "phase": "delete",
        "completion_state": "completed",
        "work": {
            "expected_files": 4800,
            "completed_files": 4800,
            "expected_bytes": None,
            "completed_bytes": None,
        },
        "elapsed_ms": 31,
    }
    assert entry["results"]["workload"] == {
        "termination_mode": "completion",
        "completion_state": "completed",
        "failure_cleanup_state": "not_needed",
        "lifecycle_elapsed_ms": 350,
    }


def test_incomplete_phase_is_reported(tmp_path):
    _write_execution_metadata(
        tmp_path,
        "0001",
        write_state="incomplete",
        completion="incomplete",
        delete_state="incomplete",
    )
    _write_ledger(tmp_path, "0001")
    _write_result(tmp_path)
    entry = _entry(_fragment(tmp_path), "0001")

    incomplete = [
        p["phase"] for p in entry["problems"] if p["code"] == "completion_incomplete"
    ]
    assert incomplete == ["write", "delete"]
    assert entry["result_state"] == "partial"


def test_filtered_missing_and_unmatched_executions(tmp_path):
    _write_ledger(tmp_path, "0001", threads=4)
    _write_ledger(tmp_path, "0002", threads=8)
    _write_ledger(tmp_path, "0003", threads=16)
    _write_result(tmp_path, threads=4)
    _write_result(tmp_path, threads=8)
    _write_result(tmp_path, threads=64)
    filters = _EXTRACT.ReportFilters(threads={4, 16})
    fragment = _fragment(tmp_path, filters=filters)

    states = {e["execution_id"]: e["result_state"] for e in fragment["executions"]}
    assert states == {"0001": "complete", "0002": "not_reported", "0003": "missing"}
    skipped = _entry(fragment, "0002")
    assert skipped["results"] is None and skipped["result_files"] == []
    assert skipped["coordinate_key"].startswith("io/c001/s008/")
    missing = _entry(fragment, "0003")
    assert missing["results"] is None and missing["result_files"] == []

    unfiltered = _fragment(tmp_path)
    unmatched = [e for e in unfiltered["executions"] if e["execution_id"] is None]
    assert len(unmatched) == 1
    assert unmatched[0]["coordinates"]["threads_per_node"] == 64
    assert unmatched[0]["coordinate_key"].startswith("io/c001/s064/")
    assert unmatched[0]["result_state"] == "complete"
    assert all(f["path"].startswith("elbencho-") for f in unmatched[0]["result_files"])


def test_unparseable_result_pair(tmp_path):
    _write_ledger(tmp_path, "0001")
    base = _write_result(tmp_path)
    Path(f"{base}.csv").write_text("not,a\nvalid,csv\n", encoding="utf-8")
    entry = _entry(_fragment(tmp_path), "0001")

    assert entry["result_state"] == "unparseable"
    assert entry["results"] is None
    assert [p["code"] for p in entry["problems"]] == ["phase_unparseable"]
    assert len(entry["result_files"]) == 2


def test_live_levels_and_sidecar(tmp_path):
    _write_ledger(tmp_path, "0001")
    _write_ledger(tmp_path, "0002", threads=8)
    base = _write_result(tmp_path, phases=("WRITE",))
    _write_result(tmp_path, threads=8, phases=("WRITE",))
    _live_csv(f"{base}.live.csv")

    summary = _fragment(tmp_path)
    live = _phase(_entry(summary, "0001"), "write")["live"]
    assert live["source"]["path"] == f"elbencho-1M-c_001-s_004-d_001_{_DS}.live.csv"
    assert live["configured_interval_ms"] == 500
    assert live["clients"] == 3
    assert live["client_series"] is None
    paths = [r["path"] for r in _entry(summary, "0001")["result_files"]]
    assert paths[1].endswith(".live.csv") and len(paths) == 3
    assert _phase(_entry(summary, "0002"), "write")["live"] == {
        "reason": "not_captured"
    }

    none = _fragment(tmp_path, level="none")
    assert _phase(_entry(none, "0001"), "write")["live"] == {"reason": "not_requested"}
    assert _phase(_entry(none, "0002"), "write")["live"] == {"reason": "not_requested"}

    sidecars = tmp_path / "side"
    sidecars.mkdir()
    full = _fragment(tmp_path, level="full", sidecar=str(sidecars))
    series = _phase(_entry(full, "0001"), "write")["live"]["client_series"]
    assert series["clients"] == 3 and series["intervals"] == 4
    assert (sidecars / series["path"]).is_file()
    assert "/" not in series["path"]


def test_unreadable_live_csv(tmp_path, monkeypatch):
    _write_ledger(tmp_path, "0001")
    base = _write_result(tmp_path)
    _live_csv(f"{base}.live.csv")

    def broken(*_args):
        raise live_results.LiveCSVError("boom")

    monkeypatch.setattr(_EXTRACT, "build_live_objects", broken)
    entry = _entry(_fragment(tmp_path), "0001")

    assert _phase(entry, "write")["live"] == {"reason": "unreadable"}
    assert [p["code"] for p in entry["problems"]] == ["live_csv_unreadable"]
    assert entry["result_state"] == "complete"


def test_workers_tsv_joins_kubernetes_names(tmp_path):
    _write_ledger(tmp_path, "0001")
    base = _write_result(tmp_path, phases=("WRITE",))
    _live_csv(f"{base}.live.csv", clients=2)
    (tmp_path / "executions" / "0001.workers.tsv").write_text(
        "node\tpod\tpod_uid\tendpoint\nnode-a\tpod-a\tuid-a\thost0\n", encoding="utf-8"
    )
    live = _phase(_entry(_fragment(tmp_path), "0001"), "write")["live"]
    columns = live["clients_summary"]["columns"]
    rows = live["clients_summary"]["rows"]
    node = columns.index("kubernetes_node")
    assert {row[node] for row in rows} == {"node-a", None}


def test_fragment_is_deterministic(tmp_path):
    _write_ledger(tmp_path, "0001")
    _write_ledger(tmp_path, "0002", threads=8)
    _write_result(tmp_path)
    _write_result(tmp_path, threads=8)
    assert json.dumps(_fragment(tmp_path)) == json.dumps(_fragment(tmp_path))


def _patch_reports(monkeypatch):
    monkeypatch.setattr(_EXTRACT, "plot_metrics", lambda *_args: None)
    monkeypatch.setattr(
        _EXTRACT, "print_terminal_table", lambda *_args, **_kw: print("TABLE")
    )
    monkeypatch.setattr(_EXTRACT, "_report_one_live_file", lambda *_args: True)


def _run_main(monkeypatch, *arguments):
    monkeypatch.setattr(sys, "argv", ["extract-elbencho.py", *arguments])
    return _EXTRACT.main()


def _ordinary_dir(tmp_path):
    result = tmp_path / f"elbencho-{_DS}"
    result.mkdir()
    (result / "env_used.yaml").write_text(_ENV_USED, encoding="utf-8")
    _write_ledger(result, "0001")
    _write_ledger(result, "0002", threads=8)
    _write_result(result)
    return result


def test_ordinary_document_is_written_and_valid(tmp_path, monkeypatch, capsys):
    _patch_reports(monkeypatch)
    result = _ordinary_dir(tmp_path)
    output = tmp_path / "out" / "results.json"

    _run_main(monkeypatch, str(result), "--to-json", "--output-dir", str(output.parent))
    document = json.loads(output.read_text(encoding="utf-8"))
    validate_results_document(document)

    assert "TABLE" in capsys.readouterr().out
    assert document["batch"]["root"] == str(result.resolve())
    assert document["batch"]["substrate"] == "ssh"
    states = {e["execution_id"]: e["result_state"] for e in document["executions"]}
    assert states == {"0001": "complete", "0002": "missing"}
    assert document["batch_outcome"]["complete"] is False
    assert document["generator"]["options"]["live_detail"] == "summary"
    assert document["generator"]["options"]["kind"] == "io"


def test_default_path_and_determinism(tmp_path, monkeypatch):
    _patch_reports(monkeypatch)
    result = _ordinary_dir(tmp_path)
    destination = result / "results.json"

    _run_main(monkeypatch, str(result), "--to-json")
    first = json.loads(destination.read_text(encoding="utf-8"))
    _run_main(monkeypatch, str(result), "--to-json")
    second = json.loads(destination.read_text(encoding="utf-8"))
    first["generator"].pop("generated_at")
    second["generator"].pop("generated_at")
    assert first == second


def test_to_json_takes_no_path(tmp_path, monkeypatch):
    _patch_reports(monkeypatch)
    result = _ordinary_dir(tmp_path)
    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, f"--to-json={tmp_path / 'doc.json'}", str(result))
    assert raised.value.code == 2


def test_require_complete_exits_three_after_writing(tmp_path, monkeypatch):
    _patch_reports(monkeypatch)
    result = _ordinary_dir(tmp_path)
    output = result / "results.json"

    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, str(result), "--to-json", "--require-complete")
    assert raised.value.code == 3
    assert output.is_file()


def test_require_complete_passes_when_complete(tmp_path, monkeypatch):
    _patch_reports(monkeypatch)
    result = _ordinary_dir(tmp_path)
    (result / "executions" / "0002.sh").unlink()
    (result / "executions" / "0002.status").unlink()
    _run_main(monkeypatch, str(result), "--to-json", "--require-complete")
    validate_results_document(
        json.loads((result / "results.json").read_text(encoding="utf-8"))
    )


def test_fragment_mode_writes_fragment_only(tmp_path, monkeypatch):
    _patch_reports(monkeypatch)
    result = _ordinary_dir(tmp_path)
    fragment_path = tmp_path / "frag" / "results.fragment.json"
    fragment_path.parent.mkdir()

    _run_main(
        monkeypatch,
        str(result),
        "--results-fragment",
        str(fragment_path),
        "--to-json",
        "--only-threads",
        "8",
    )
    fragment = json.loads(fragment_path.read_text(encoding="utf-8"))

    assert not (result / "results.json").exists()
    assert fragment["kind"] == "io"
    states = {e["execution_id"]: e["result_state"] for e in fragment["executions"]}
    assert states == {"0001": "not_reported", "0002": "missing"}


def test_to_json_requires_exactly_one_input_dir(tmp_path, monkeypatch):
    _patch_reports(monkeypatch)
    first = _ordinary_dir(tmp_path)
    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, str(first), str(first), "--to-json")
    assert raised.value.code == 2


def test_options_absent_leaves_no_results_files(tmp_path, monkeypatch):
    _patch_reports(monkeypatch)
    result = _ordinary_dir(tmp_path)
    _run_main(monkeypatch, str(result))
    assert not (result / "results.json").exists()
    assert not (result / "results.live").exists()


def test_full_live_document_validates_and_sidecar_path_is_relative(
    tmp_path, monkeypatch
):
    _patch_reports(monkeypatch)
    result = _ordinary_dir(tmp_path)
    base = next(result.glob("*.csv")).with_suffix("")
    _live_csv(f"{base}.live.csv", clients=3)
    output = tmp_path / "out"

    _run_main(
        monkeypatch,
        str(result),
        "--to-json",
        "--live-detail",
        "full",
        "--output-dir",
        str(output),
    )
    document = json.loads((output / "results.json").read_text(encoding="utf-8"))
    validate_results_document(document)

    execution = next(e for e in document["executions"] if e["execution_id"] == "0001")
    series = next(
        p["live"]["client_series"]
        for p in execution["results"]["phases"]
        if p["phase"] == "write"
    )
    assert series["path"] == f"results.live/{series['path'].split('/')[-1]}"
    assert (output / series["path"]).is_file()


def test_unparseable_definition_stays_in_the_ordinary_document(tmp_path, monkeypatch):
    _patch_reports(monkeypatch)
    result = _ordinary_dir(tmp_path)
    (result / "executions" / "0002.sh").write_text(
        "export ELBENCHO_EXECUTION_KIND=io\n", encoding="utf-8"
    )
    (result / "executions" / "0002.status").write_text("FAILED\n", encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, str(result), "--to-json", "--require-complete")
    assert raised.value.code == 3
    document = json.loads((result / "results.json").read_text(encoding="utf-8"))
    validate_results_document(document)
    assert document["batch_outcome"]["executions"]["total"] == 2
    assert document["batch_outcome"]["complete"] is False
    unreadable = next(e for e in document["executions"] if e["execution_id"] == "0002")
    assert unreadable["ledger_state"] == "failed"
    assert unreadable["coordinates"] is None
    assert [p["code"] for p in unreadable["problems"]] == ["definition_unreadable"]


def test_blank_first_rates_are_null_and_partial(tmp_path, monkeypatch):
    _patch_reports(monkeypatch)
    result = _ordinary_dir(tmp_path)
    (result / "executions" / "0002.sh").unlink()
    (result / "executions" / "0002.status").unlink()
    csv_path = next(p for p in result.glob("*.csv") if not p.name.endswith(".live.csv"))
    with open(csv_path, encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row["operation"] == "WRITE":
            row["IOPS [first]"] = ""
            row["MiB/s [first]"] = "n/a"
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, _CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, str(result), "--to-json", "--require-complete")
    assert raised.value.code == 3
    document = json.loads((result / "results.json").read_text(encoding="utf-8"))
    validate_results_document(document)
    execution = document["executions"][0]
    assert execution["result_state"] == "partial"
    phases = {p["phase"]: p for p in execution["results"]["phases"]}
    assert phases["write"]["elbencho_first"]["ops_per_s"] is None
    assert phases["write"]["elbencho_first"]["bytes_per_s"] is None
    assert phases["read"]["elbencho_first"]["ops_per_s"] == 812345.04
    assert [(p["code"], p["phase"]) for p in execution["problems"]] == [
        ("rates_missing", "write")
    ]


def test_directory_without_results_exits_by_completeness(tmp_path, monkeypatch):
    _patch_reports(monkeypatch)
    result = tmp_path / f"elbencho-{_DS}"
    result.mkdir()
    (result / "env_used.yaml").write_text(_ENV_USED, encoding="utf-8")
    _write_ledger(result, "0001")
    (result / "executions" / "0001.status").write_text("PENDING\n", encoding="utf-8")

    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, str(result), "--to-json")
    assert raised.value.code == 0
    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, str(result), "--to-json", "--require-complete")
    assert raised.value.code == 3
    document = json.loads((result / "results.json").read_text(encoding="utf-8"))
    assert document["batch_outcome"]["executions"]["pending"] == 1
    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, str(result))
    assert raised.value.code == 1


def _rewrite_csv(result, change):
    """Apply change(row) to each aggregate CSV row of a result directory."""
    csv_path = next(p for p in result.glob("*.csv") if not p.name.endswith(".live.csv"))
    with open(csv_path, encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        change(row)
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, _CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return csv_path


def test_zero_milliseconds_is_measured_and_blank_is_null(tmp_path, monkeypatch):
    _patch_reports(monkeypatch)
    result = _ordinary_dir(tmp_path)

    def change(row):
        if row["operation"] == "WRITE":
            row["time ms [first]"] = "0"
            row["time ms [last]"] = "0"
        else:
            row["time ms [first]"] = ""
            row["time ms [last]"] = "soon"

    _rewrite_csv(result, change)
    _run_main(monkeypatch, str(result), "--to-json")
    document = json.loads((result / "results.json").read_text(encoding="utf-8"))
    validate_results_document(document)
    execution = next(e for e in document["executions"] if e["execution_id"] == "0001")
    phases = {p["phase"]: p for p in execution["results"]["phases"]}
    assert phases["write"]["elbencho_first"]["elapsed_ms"] == 0
    assert phases["write"]["elbencho_last"]["elapsed_ms"] == 0
    assert phases["read"]["elbencho_first"]["elapsed_ms"] is None
    assert phases["read"]["elbencho_last"]["elapsed_ms"] is None


def test_non_finite_live_totals_become_null_samples(tmp_path, monkeypatch):
    _patch_reports(monkeypatch)
    result = _ordinary_dir(tmp_path)
    base = next(result.glob("*.csv")).with_suffix("")
    live_path = Path(f"{base}.live.csv")
    _live_csv(live_path, clients=3)
    with open(live_path, encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    totals = [row for row in rows if row["Rank"] == "Total"]
    totals[1]["IOPS"] = "NaN"
    totals[2]["MiB/s"] = "-Infinity"
    totals[3]["IOPS"] = "inf"
    with open(live_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, LIVE_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    _run_main(monkeypatch, str(result), "--to-json")
    document = json.loads((result / "results.json").read_text(encoding="utf-8"))
    validate_results_document(document)
    execution = next(e for e in document["executions"] if e["execution_id"] == "0001")
    write = next(p for p in execution["results"]["phases"] if p["phase"] == "write")
    series = write["live"]["aggregate_series"]
    assert series["ops_per_s"] == [100.0, None, 100.0, None]
    assert series["bytes_per_s"] == [300 * 1048576, 300 * 1048576, None, 300 * 1048576]
