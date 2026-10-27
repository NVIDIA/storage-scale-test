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

"""End-to-end: a realistic prepared batch through ``extract-filesystem.py --to-json``."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lib.filesystem_batch import ENV_SHELL, ENV_YAML, MANIFEST_FILENAME
from tests.test_extract_elbencho_results_json import _DS, _live_csv, _write_result
from tests.test_extract_mdtest_elbencho import _DENSE_CSV, _DENSE_OUT
from tests.test_extract_mdtest_results_json import _truncated_csv, _truncated_out
from tests.test_filesystem_results_schema import (
    validate_results_document,
    validate_sidecar_lines,
)

REPO = Path(__file__).resolve().parent.parent
FRONT_DOOR = REPO / "utils" / "extract-filesystem.py"
SPLIT = "1M,r4K"
EXIT_INCOMPLETE = 3

# (execution id, group id, ledger status); 0001 direct IO with live data,
# 0002 failed, 0003 buffered IO, 0004 pending, 0005 mdtest with a cut iteration.
LEDGER = {
    "0001": ("0001", "SUCCESS"),
    "0002": ("0001", "FAILED"),
    "0003": ("0002", "SUCCESS"),
    "0004": ("0002", "PENDING"),
    "0005": ("0003", "SUCCESS"),
}
ENV_YAML_TEXT = (
    "ELBENCHO_LIVEINT: 500\nEXECUTION_SUBSTRATE: kubectl\n"
    "MDTEST_BRANCH_FACTOR: 7\nMDTEST_ITEMS_PER_DIR: 100\nMDTEST_ITERATIONS: 3\n"
)
WORKERS = "node-a\tpod-a\tuid-a\thost0\nnode-b\tpod-b\tuid-b\thost1\n"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _io_definition(group_id, threads, bio):
    return (
        f"# Auto-generated; do not edit. Coords: nodes=2 io_size={SPLIT} "
        f"thread_count={threads} io_depth=1\n"
        "export ELBENCHO_EXECUTION_KIND=io\n"
        f"export ELBENCHO_BATCH_GROUP_ID={group_id}\n"
        f"export ELBENCHO_BATCH_OUTPUT_RELATIVE=groups/{group_id}/elbencho-{_DS}\n"
        f"export nodes=2\nexport io_size={SPLIT}\n"
        f"export thread_count={threads}\nexport io_depth=1\n"
        f"export dio_or_bio={'bio' if bio else 'dio'}\nexport use_random=0\n"
        "export run_to_completion=0\nexport ELBENCHO_SWEEP_WRITE_ONLY=0\n"
        "export ELBENCHO_SWEEP_WRITE_NO_READ=0\nexport ELBENCHO_SWEEP_READ_FROM=''\n"
        "export ELBENCHO_FILE_LAYOUT=worker-directories\n"
        "export ELBENCHO_SINGLE_BIG_FILE=0\n"
        "export ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0\n"
    )


def _md_definition(group_id):
    return (
        "export ELBENCHO_EXECUTION_KIND=mdtest\n"
        f"export ELBENCHO_BATCH_GROUP_ID={group_id}\n"
        f"export ELBENCHO_BATCH_OUTPUT_RELATIVE=groups/{group_id}/"
        f"mdtest-elbencho-{_DS}\n"
        "export nodes=2\nexport tasks_per_node=4\nexport MDTEST_ITERATIONS=3\n"
        "export MDTEST_BRANCH_FACTOR=7\nexport MDTEST_ITEMS_PER_DIR=100\n"
    )


def _build_batch(root):
    """Prepare a realistic three-group batch with published, partial, failed results."""
    root.mkdir()
    (root / "executions").mkdir()
    groups = {
        "0001": ("io", "elbencho"),
        "0002": ("io", "elbencho"),
        "0003": ("mdtest", "mdtest-elbencho"),
    }
    definitions = {
        "0001": _io_definition("0001", 4, False),
        "0002": _io_definition("0001", 8, False),
        "0003": _io_definition("0002", 4, True),
        "0004": _io_definition("0002", 8, True),
        "0005": _md_definition("0003"),
    }
    records = ["version\t1", f"revision\t{len(groups)}", f"datestamp\t{_DS}"]
    dirs = {}
    for group_id, (kind, base) in groups.items():
        relative = f"groups/{group_id}/{base}-{_DS}"
        group = root / relative
        (group / "executions").mkdir(parents=True)
        (group / ENV_SHELL).write_text(f"export TEST_ROOT=/t/{group_id}\n", "utf-8")
        (group / ENV_YAML).write_text(ENV_YAML_TEXT, "utf-8")
        dirs[group_id] = group
        records.append(
            f"group\t{group_id}\t{kind}\t{relative}\t"
            f"{_sha(group / ENV_SHELL)}\t{_sha(group / ENV_YAML)}"
        )
    for execution_id, (group_id, status) in LEDGER.items():
        path = root / "executions" / f"{execution_id}.sh"
        path.write_text(definitions[execution_id], "utf-8")
        (root / "executions" / f"{execution_id}.status").write_text(
            status + "\n", "utf-8"
        )
        records.append(f"execution\t{execution_id}\t{group_id}\t{_sha(path)}")
        if status == "SUCCESS":
            log = dirs[group_id] / "executions" / f"{execution_id}.log"
            log.write_text(f"run {execution_id}\n", "utf-8")
    (root / MANIFEST_FILENAME).write_text("\n".join(records) + "\n", "utf-8")
    return dirs


def _publish_results(dirs):
    """Real-format result files for the SUCCESS executions."""
    direct = _write_result(
        dirs["0001"], nodes=2, io_size=SPLIT, threads=4, rand_phases=("READ",)
    )
    _live_csv(f"{direct}.live.csv", clients=2, intervals=5)
    _write_result(
        dirs["0002"],
        nodes=2,
        io_size=SPLIT,
        threads=4,
        direct=False,
        rand_phases=("READ",),
    )
    for iteration in (1, 2, 3):
        stem = dirs["0003"] / f"mdtest-elbencho-c_002-t_004_{_DS}_iter{iteration}"
        cut = iteration == 3
        Path(f"{stem}.csv").write_text(_truncated_csv() if cut else _DENSE_CSV, "utf-8")
        Path(f"{stem}.out").write_text(_truncated_out() if cut else _DENSE_OUT, "utf-8")


@pytest.fixture(name="batch", scope="module")
def batch_fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2e") / "batch"
    dirs = _build_batch(root)
    _publish_results(dirs)
    # The Kubernetes coordinator collects workers.tsv into the root ledger.
    (root / "executions" / "0001.workers.tsv").write_text(WORKERS, "utf-8")
    return root


def _export(batch, *arguments, output=None, check=True):
    command = [sys.executable, str(FRONT_DOOR), str(batch), *arguments]
    if output is not None:
        command += ["--output-dir", str(output)]
    done = subprocess.run(
        command, capture_output=True, text=True, check=False, timeout=180
    )
    if check:
        assert done.returncode == 0, done.stderr
    return done


@pytest.fixture(name="exported", scope="module")
def exported_fixture(batch, tmp_path_factory):
    output = tmp_path_factory.mktemp("e2e-out") / "out"
    _export(batch, "--to-json", "--live-detail", "full", output=output)
    document = json.loads((output / "results.json").read_text("utf-8"))
    return batch, output, document


def _by_id(document):
    return {item["execution_id"]: item for item in document["executions"]}


def test_document_validates_and_reports_batch_outcome(exported):
    _, _, document = exported
    validate_results_document(document)
    assert document["batch"]["datestamp"] == _DS
    outcome = document["batch_outcome"]
    assert outcome["complete"] is False
    assert outcome["executions"]["total"] == 5
    assert outcome["executions"]["success"] == 3
    assert outcome["executions"]["failed"] == 1
    assert outcome["executions"]["pending"] == 1
    assert outcome["results"]["complete"] == 2
    assert outcome["results"]["partial"] == 1
    assert outcome["results"]["not_run"] == 2
    assert [group["group_id"] for group in document["groups"]] == [
        "0001",
        "0002",
        "0003",
    ]


def test_ledger_and_result_states_per_execution(exported):
    _, _, document = exported
    states = {
        key: (item["ledger_state"], item["result_state"])
        for key, item in _by_id(document).items()
    }
    assert states == {
        "0001": ("success", "complete"),
        "0002": ("failed", "not_run"),
        "0003": ("success", "complete"),
        "0004": ("pending", "not_run"),
        "0005": ("success", "partial"),
    }
    by_id = _by_id(document)
    iterations = by_id["0005"]["results"]["iterations"]
    assert [item["state"] for item in iterations] == [
        "complete",
        "complete",
        "incomplete",
    ]
    assert iterations[2]["problems"][0]["code"] == "iteration_incomplete"
    assert by_id["0005"]["results"]["aggregate"]["iterations_used"] == 2
    assert by_id["0002"]["results"] is None
    assert by_id["0004"]["result_files"] == []


def test_io_coordinates_and_live_detail(exported):
    _, output, document = exported
    direct = _by_id(document)["0001"]
    assert direct["coordinates"]["io_size_spec"] == SPLIT
    assert direct["coordinates"]["direct_io"] is True
    assert direct["coordinates"]["read_access"] == "random"
    assert direct["coordinates"]["read_block_size_bytes"] == 4096
    assert direct["coordinate_key"].endswith("/dio")
    assert _by_id(document)["0003"]["coordinates"]["direct_io"] is False
    phases = {phase["phase"]: phase for phase in direct["results"]["phases"]}
    live = phases["write"]["live"]
    assert live["client_series"]["path"].endswith(".jsonl.zst")
    assert (output / live["client_series"]["path"]).is_file()
    assert live["source"]["path"].endswith(".live.csv")
    # Workers collected by the Kubernetes coordinator join to the live clients.
    columns = live["clients_summary"]["columns"]
    rows = [dict(zip(columns, row)) for row in live["clients_summary"]["rows"]]
    assert [row["kubernetes_node"] for row in rows] == ["node-a", "node-b"]
    assert phases["read"]["live"] == {"reason": "not_captured"}


def test_sidecars_validate(exported):
    _, output, document = exported
    sidecars = [
        output / phase["live"]["client_series"]["path"]
        for item in document["executions"]
        for phase in (item["results"] or {}).get("phases", [])
        if "client_series" in (phase.get("live") or {})
    ]
    assert sidecars
    for path in sidecars:
        lines = validate_sidecar_lines(path)
        assert lines[0]["execution_id"] == "0001"
        assert len(lines[0]["clients"]) == 2


def test_paths_are_relative_to_batch_root_and_hashes_match(exported):
    batch, _, document = exported
    assert Path(document["batch"]["root"]) == batch.resolve()
    checked = 0
    for item in document["executions"]:
        for record in item["result_files"]:
            path = batch / record["path"]
            assert not Path(record["path"]).is_absolute()
            assert path.is_file(), record["path"]
            assert record["sha256"] == _sha(path)
            assert record["size_bytes"] == path.stat().st_size
            checked += 1
        if item["log"]:
            assert (batch / item["log"]).is_file()
    assert checked >= 2 * 2 + 3 * 2
    for item in document["executions"]:
        if item["ledger_state"] == "success":
            assert item["log"]


def test_require_complete_writes_then_exits_incomplete(batch, tmp_path):
    output = tmp_path / "out"
    done = _export(batch, "--to-json", "--require-complete", output=output, check=False)
    assert done.returncode == EXIT_INCOMPLETE, done.stderr
    document = json.loads((output / "results.json").read_text("utf-8"))
    validate_results_document(document)
    assert document["batch_outcome"]["executions"]["total"] == 5
    assert document["batch_outcome"]["complete"] is False
