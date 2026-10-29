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

"""Export and report the oldest pre-batch result layouts (elbencho 3.0.37).

Those directories have no executions/ ledger and only env_used.yaml; the
runner log, treefile and per-node Slurm logs beside the results are not
result pairs. All values are invented.
"""

import argparse
import csv
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

from lib.filesystem_report_options import (
    LIVE_DETAIL_FLAG,
    REPORT_OPTIONS,
    add_analysis_arguments,
    options_by_kind,
)
from tests.test_extract_elbencho_results_json import (
    _CSV_FIELDS,
    _EXTRACT,
    _out_section,
    _phase_row,
)
from tests.test_extract_mdtest_elbencho import (
    _DENSE_CSV,
    _DENSE_OUT,
    _EXTRACT_MOD as _MDTEST,
)
from tests.test_filesystem_results_schema import validate_results_document

_DS = "20240611Z080000"
_VERSION = "3.0.37"
_DATASET = "/mnt/test/dataset"
_NODES = (2, 4, 8)
_THREADS = 64
_DEPTH = 2
_IO_ENV_USED = f"""# env_used.yaml - elbencho sweep configuration snapshot
TEST_DIRS:
  "/mnt/test/data": 1
ELBENCHO_SCALE_THREAD_LIST: [{_THREADS}]
ELBENCHO_SCALE_IO_SIZES: [1M]
ELBENCHO_IODEPTH_LIST: [{_DEPTH}]
ELBENCHO_SCALE_READ_WRITE_DURATION: 60
ELBENCHO_SINGLE_BIG_FILE: {{single_big_file}}
ELBENCHO_ALL_NODES_ACCESS_ALL_DATA: {{all_nodes_all_data}}
dio_or_bio: "dio"
rand_option: 0
run_to_completion_option: {{run_to_completion}}
sweep_write_only: 0
sweep_write_no_read: 0
sweep_read_from: "{_DATASET}"
nodes_spec: "{",".join(map(str, _NODES))}"
"""
_TREESCAN = "Treescan file sizes (bytes): count=1000 avg=1048576 min=4096 max=8388608"
_MDTEST_TASKS = 16
_MDTEST_ENV_USED = (
    "# env_used.yaml - mdtest-elbencho sweep configuration snapshot\n"
    "MDTEST_BRANCH_FACTOR: 10\nMDTEST_ITEMS_PER_DIR: 100\nMDTEST_ITERATIONS: 2\n"
)
_NO_LEDGER = ["no_execution_ledger"]


def _io_out(command: str) -> str:
    """A 3.0.37-style .out with the run header around the READ section."""
    return (
        "ISO DATE start: 2024-06-11T08:00:00-0700\n"
        f"COMMAND LINE: {command}\n\n"
        "OPERATION   RESULT TYPE         FIRST DONE   LAST DONE\n"
        "=========== ================    ==========   =========\n"
        f"{_out_section('READ')}"
        "\nISO DATE end  : 2024-06-11T08:01:00-0700\n"
    )


def _write_io_pair(result: Path, nodes: int) -> None:
    """One read-from result pair for a node count."""
    base = result / f"elbencho-1M-c_{nodes:03d}-s_{_THREADS:03d}-d_{_DEPTH:03d}_{_DS}"
    hosts = ",".join(f"host{index:03d}" for index in range(1, nodes + 1))
    command = (
        f'"elbencho" "--hosts" "{hosts}" "--read" "--timelimit" "60" '
        f'"-t" "{_THREADS}" "--iodepth" "{_DEPTH}" "-b" "1M" "--direct" '
        f'"{_DATASET}"'
    )
    row = _phase_row(
        "READ", True, False, (59874, 4000, 4000.0), (60311, 3900, 3900.0), command
    )
    row.update(
        {
            "label": "",
            "path type": "dir",
            "paths": "1",
            "hosts": str(nodes),
            "threads": str(_THREADS),
            "block size": "1048576",
            "IO depth": str(_DEPTH),
            "version": _VERSION,
        }
    )
    with open(f"{base}.csv", "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, _CSV_FIELDS)
        writer.writeheader()
        writer.writerow(row)
    Path(f"{base}.out").write_text(_io_out(command), encoding="utf-8")
    # Per-node Slurm job log: read-from path and treescan statistics only.
    (result / f"elbencho-{_DS}-{max(_NODES)}-{nodes}-1M-{1000 + nodes}.out").write_text(
        f"Read-from: {_DATASET}\n{_TREESCAN}\n", encoding="utf-8"
    )


def write_prebatch_io_dir(root: Path, **settings: int) -> Path:
    """A pre-batch read-from sweep over several node counts."""
    result = root / f"elbencho-{_DS}"
    result.mkdir()
    values = {"single_big_file": 0, "all_nodes_all_data": 0, "run_to_completion": 0}
    values.update(settings)
    (result / "env_used.yaml").write_text(
        _IO_ENV_USED.format(**values), encoding="utf-8"
    )
    for nodes in _NODES:
        _write_io_pair(result, nodes)
    (result / f"elbencho-sweep-{_DS}-runner.log").write_text(
        "Starting elbencho sweep\n", encoding="utf-8"
    )
    (result / f"elbencho-treescan-{_DS}.txt").write_text(
        f"{_DATASET}/file000001 1048576\n", encoding="utf-8"
    )
    return result


def write_prebatch_mdtest_dir(root: Path) -> Path:
    """A pre-batch metadata sweep: two iterations per node count, no ledger."""
    result = root / f"mdtest-elbencho-{_DS}"
    result.mkdir()
    (result / "env_used.yaml").write_text(_MDTEST_ENV_USED, encoding="utf-8")
    csv_text = _DENSE_CSV.replace("3.1-2", _VERSION)
    for nodes in _NODES:
        for iteration in (1, 2):
            stem = (
                f"mdtest-elbencho-c_{nodes:03d}-t_{_MDTEST_TASKS:03d}"
                f"_{_DS}_iter{iteration}"
            )
            (result / f"{stem}.csv").write_text(csv_text, encoding="utf-8")
            (result / f"{stem}.out").write_text(_DENSE_OUT, encoding="utf-8")
    (result / f"mdtest-elbencho-sweep-{_DS}-runner.log").write_text(
        "Starting mdtest-elbencho sweep\n", encoding="utf-8"
    )
    return result


def _run_io(monkeypatch, *arguments) -> int:
    monkeypatch.setattr(_EXTRACT, "plot_metrics", lambda *_args: None)
    monkeypatch.setattr(sys, "argv", ["extract-elbencho.py", *map(str, arguments)])
    try:
        _EXTRACT.main()
    except SystemExit as exit_request:
        return exit_request.code or 0
    return 0


def _run_mdtest(*arguments) -> int:
    with (
        mock.patch.object(
            sys, "argv", ["extract-mdtest-elbencho.py", *map(str, arguments)]
        ),
        mock.patch.object(_MDTEST, "plot_all"),
    ):
        try:
            _MDTEST.main()
        except SystemExit as exit_request:
            return exit_request.code or 0
    return 0


def _document(path: Path) -> dict:
    document = json.loads(path.read_text(encoding="utf-8"))
    validate_results_document(document)
    return document


def _codes(document: dict) -> list:
    return [item["code"] for item in document["diagnostics"]]


def test_prebatch_io_directory_exports_read_from_sweep(tmp_path, monkeypatch, capsys):
    result = write_prebatch_io_dir(tmp_path)
    output = tmp_path / "out"

    assert _run_io(monkeypatch, result, "--to-json", "--output-dir", output) == 0
    document = _document(output / "results.json")

    assert "READ - Seq - DirectIO" in capsys.readouterr().out
    assert (output / "report.txt").is_file()
    assert _codes(document) == _NO_LEDGER
    assert document["groups"][0]["settings"]["sweep_read_from"] == _DATASET
    assert document["groups"][0]["settings_sha256"]["env_used_sh"] is None
    executions = document["executions"]
    assert [e["coordinates"]["nodes"] for e in executions] == list(_NODES)
    for execution in executions:
        assert execution["execution_id"] is None
        assert execution["ledger_state"] == "unknown"
        assert execution["result_state"] == "complete"
        assert execution["problems"] == []
        assert execution["coordinates"] == {
            "nodes": execution["coordinates"]["nodes"],
            "threads_per_node": _THREADS,
            "io_depth": _DEPTH,
            "io_size_spec": "1M",
            "phase_plan": "read_from",
            "write_block_size_bytes": None,
            "write_access": None,
            "read_block_size_bytes": 1048576,
            "read_access": "sequential",
            "direct_io": True,
            "run_to_completion": False,
            "file_layout": "worker-directories",
            "single_big_file": False,
            "all_nodes_all_data": False,
        }
        assert execution["dataset"]["read_from"] == _DATASET
        assert execution["dataset"]["scanned_file_sizes"]["count"] == 1000
        assert [p["phase"] for p in execution["results"]["phases"]] == ["read"]
        assert len(execution["result_files"]) == 2
    assert document["batch_outcome"]["complete"] is False


def test_prebatch_io_settings_come_from_env_used_yaml(tmp_path, monkeypatch):
    result = write_prebatch_io_dir(
        tmp_path, single_big_file=1, all_nodes_all_data=1, run_to_completion=1
    )

    assert _run_io(monkeypatch, result, "--to-json") == 0
    document = _document(result / "results.json")

    for execution in document["executions"]:
        coordinates = execution["coordinates"]
        assert coordinates["single_big_file"] is True
        assert coordinates["all_nodes_all_data"] is True
        assert coordinates["run_to_completion"] is True


def test_empty_executions_directory_is_no_io_ledger(tmp_path, monkeypatch):
    result = write_prebatch_io_dir(tmp_path)
    (result / "executions").mkdir()

    assert _run_io(monkeypatch, result, "--to-json") == 0
    document = _document(result / "results.json")

    assert _codes(document) == _NO_LEDGER
    assert [e["result_state"] for e in document["executions"]] == ["complete"] * 3


def test_prebatch_mdtest_directory_exports(tmp_path, capsys):
    result = write_prebatch_mdtest_dir(tmp_path)

    assert _run_mdtest(result, "--to-json") == 0
    document = _document(result / "results.json")

    assert _codes(document) == _NO_LEDGER
    assert document["groups"][0]["settings"]["MDTEST_ITERATIONS"] == 2
    executions = document["executions"]
    assert [e["coordinates"] for e in executions] == [
        {
            "nodes": nodes,
            "tasks_per_node": _MDTEST_TASKS,
            "branch_factor": 10,
            "items_per_dir": 100,
            "iterations": 2,
        }
        for nodes in _NODES
    ]
    for execution in executions:
        assert execution["execution_id"] is None
        assert execution["result_state"] == "complete"
        assert execution["results"]["aggregate"]["iterations_used"] == 2
    assert "Performance Rates" in capsys.readouterr().out


def test_empty_executions_directory_is_no_mdtest_ledger(tmp_path, capsys):
    result = write_prebatch_mdtest_dir(tmp_path)
    (result / "executions").mkdir()

    assert _run_mdtest(result, "--to-json") == 0
    document = _document(result / "results.json")

    captured = capsys.readouterr()
    assert "Skipping metadata result" not in captured.err
    assert "Performance Rates" in captured.out
    assert _codes(document) == _NO_LEDGER
    assert [e["result_state"] for e in document["executions"]] == ["complete"] * 3


def _explicit(*arguments: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_analysis_arguments(parser, "all", explicit_only=True)
    return parser.parse_args(list(arguments))


def test_default_live_detail_is_ignored_without_io_groups():
    option = next(item for item in REPORT_OPTIONS if item.flag == LIVE_DETAIL_FLAG)
    default = option.settings["default"]
    other = next(level for level in option.settings["choices"] if level != default)

    args = _explicit(LIVE_DETAIL_FLAG, default)
    assert options_by_kind(args, {"mdtest"}) == {"mdtest": []}
    routed = options_by_kind(args, {"io", "mdtest"})
    assert routed == {"io": [f"{LIVE_DETAIL_FLAG}={default}"], "mdtest": []}
    with pytest.raises(ValueError, match="no selected io group"):
        options_by_kind(_explicit(LIVE_DETAIL_FLAG, other), {"mdtest"})
