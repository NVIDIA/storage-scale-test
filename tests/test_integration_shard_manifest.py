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

"""Substrate shards must cover the unsharded integration plan exactly once."""

from collections import Counter
import importlib.util
import json
from pathlib import Path
import sys
import time

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "shard_manifest_under_test",
    _REPO_ROOT / "integration-tests" / "lib" / "shard_manifest.py",
)
assert _SPEC and _SPEC.loader
_MANIFEST = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MANIFEST
_SPEC.loader.exec_module(_MANIFEST)
_PLANNER = sys.modules["scenario_planner"]

_SHA = "0123456789abcdef0123456789abcdef01234567"
_ARCHITECTURES = ("amd64", "arm64")


def _shards(changes=None):
    """A complete, successful six-shard manifest set, optionally altered."""
    shards = []
    for architecture in _ARCHITECTURES:
        for substrate in _MANIFEST.SHARD_SUBSTRATES:
            planned = _MANIFEST.planned_work(substrate)
            shard = {
                "schema": _MANIFEST.MANIFEST_SCHEMA,
                "source_sha": _SHA,
                "architecture": architecture,
                "backend": "nfs",
                "substrate": substrate,
                "boot_id": f"{architecture}-{substrate}",
                "exit_code": 0,
                "planned": planned,
                "completed": list(planned),
                "timings": [],
            }
            shard.update((changes or {}).get((architecture, substrate), {}))
            shards.append(shard)
    return shards


def _verify(shards):
    return _MANIFEST.verify_manifests(
        shards, expected_sha=_SHA, backend="nfs", architectures=_ARCHITECTURES
    )


def test_substrate_shards_partition_the_unsharded_plan():
    """The catalog's shard plans add up to the full plan, with no repeats."""
    full = Counter(_MANIFEST.planned_work("all"))
    shards = Counter()
    for substrate in _MANIFEST.SHARD_SUBSTRATES:
        plan = _MANIFEST.planned_work(substrate)
        assert len(plan) == len(set(plan))
        assert all(item.endswith(f"/{substrate}") for item in plan)
        shards.update(plan)
    assert shards == full
    assert max(full.values()) == 1


@pytest.mark.parametrize("substrate", ["ssh", "slurm", "kubectl"])
def test_only_ssh_shards_transition_worker_homes(substrate):
    """Home-mode transitions stay with the SSH work that needs them."""
    plan = _PLANNER.plan_scenarios(substrate=substrate)
    transitions = [s for s in plan if isinstance(s, _PLANNER.SshHomeTransition)]
    assert bool(transitions) == (substrate == "ssh")


def test_complete_successful_shard_set_verifies():
    assert not _verify(_shards())


@pytest.mark.parametrize(
    "shards, message",
    [
        (_shards()[1:], "missing manifest"),
        (_shards() + _shards()[:1], "duplicate manifest"),
        (_shards({("arm64", "ssh"): {"source_sha": "f" * 40}}), "tested f"),
        (_shards({("amd64", "slurm"): {"backend": "sbx-shared"}}), "backend"),
        (_shards({("amd64", "slurm"): {"exit_code": 1}}), "exit code 1"),
        (_shards({("arm64", "kubectl"): {"completed": []}}), "completed work"),
        (_shards({("arm64", "kubectl"): {"planned": []}}), "planned work"),
        (_shards({("amd64", "ssh"): {"substrate": "all"}}), "unexpected manifest"),
    ],
)
def test_incomplete_or_inconsistent_shard_sets_fail(shards, message):
    errors = _verify(shards)
    assert any(message in error for error in errors), errors


def test_recorder_keeps_planned_and_finished_items(tmp_path):
    plan = _PLANNER.plan_scenarios(substrate="slurm")
    work = [step for step in plan if isinstance(step, _PLANNER.WorkItem)]
    path = tmp_path / _MANIFEST.WORK_ITEMS_FILENAME
    recorder = _MANIFEST.WorkItemRecorder(path, work)
    assert json.loads(path.read_text())["items"] == []
    recorder.record(work[0], "passed", time.monotonic())
    recorder.record(work[1], "failed", time.monotonic())
    document = json.loads(path.read_text())
    assert document["planned"] == _MANIFEST.planned_work("slurm")
    assert [item["outcome"] for item in document["items"]] == ["passed", "failed"]


def test_written_manifest_counts_only_passed_items(tmp_path):
    work_items = tmp_path / "work-items.json"
    work_items.write_text(
        json.dumps(
            {
                "planned": ["baseline/ssh", "default-dio/ssh"],
                "items": [
                    {"work_item": "baseline/ssh", "outcome": "passed"},
                    {"work_item": "default-dio/ssh", "outcome": "failed"},
                ],
            }
        )
    )
    timings = tmp_path / "timings.tsv"
    timings.write_text("setup-1\t480\t0\ntest\t1200\t1\n")
    output = tmp_path / "out" / "amd64-ssh.json"
    arguments = [
        "write",
        f"--output={output}",
        f"--source-sha={_SHA}",
        "--architecture=amd64",
        "--backend=nfs",
        "--substrate=ssh",
        "--exit-code=1",
        f"--timings={timings}",
    ]
    assert _MANIFEST.main([*arguments, f"--work-items={work_items}"]) == 0
    manifest = json.loads(output.read_text())
    assert manifest["completed"] == ["baseline/ssh"]
    assert manifest["planned"] == _MANIFEST.planned_work("ssh")
    assert manifest["timings"][1] == {"step": "test", "seconds": 1200, "exit_code": 1}
    assert _MANIFEST.main([*arguments, f"--work-items={tmp_path / 'absent'}"]) == 0
    assert json.loads(output.read_text())["completed"] == []


def test_workflow_matrix_matches_the_status_check():
    """Every shard is pinned to one commit and checked by the status job."""
    workflow = yaml.safe_load(
        (_REPO_ROOT / ".github/workflows/integration.yml").read_text(encoding="utf-8")
    )
    jobs = workflow["jobs"]
    matrix = jobs["integration"]["strategy"]["matrix"]
    assert jobs["integration"]["strategy"]["fail-fast"] is False
    assert {item["name"]: item["runner"] for item in matrix["architecture"]} == {
        "amd64": "linux-amd64-cpu4",
        "arm64": "linux-arm64-cpu4",
    }
    assert tuple(matrix["substrate"]) == _MANIFEST.SHARD_SUBSTRATES
    pinned = "${{ needs.source.outputs.sha }}"
    for name in ("integration", "integration-status"):
        checkout = next(
            step
            for step in jobs[name]["steps"]
            if step.get("name") == "Check out source"
        )
        assert checkout["with"]["ref"] == pinned
    lifecycle = next(
        step
        for step in jobs["integration"]["steps"]
        if step.get("name") == "Run the bounded integration lifecycle"
    )
    assert '"${{ matrix.substrate }}"' in lifecycle["run"]
    status = jobs["integration-status"]
    assert status["name"] == "Filesystem integration status"
    assert status["if"] == "${{ always() }}"
    verify = next(step for step in status["steps"] if "verify" in step.get("run", ""))
    assert "--architectures " + ",".join(_ARCHITECTURES) in verify["run"]
    assert "--backend nfs" in verify["run"]


def test_workflow_wrapper_and_driver_agree_on_shared_names():
    """Names written in one place and read in another must not drift apart."""
    workflow = yaml.safe_load(
        (_REPO_ROOT / ".github/workflows/integration.yml").read_text(encoding="utf-8")
    )
    wrapper = (_REPO_ROOT / "integration-tests/bin/ci-integration.sh").read_text(
        encoding="utf-8"
    )
    steps = {
        step.get("name"): step for step in workflow["jobs"]["integration"]["steps"]
    }
    status = {
        step.get("name"): step
        for step in workflow["jobs"]["integration-status"]["steps"]
    }
    upload = steps["Upload the shard manifest"]["with"]
    download = status["Download shard manifests"]["with"]
    # The wrapper writes manifests where the workflow uploads them from.
    assert f"$repo_root/{upload['path']}}}" in wrapper
    # The status job downloads exactly the uploaded manifest artifacts.
    assert download["pattern"].endswith("*")
    assert upload["name"].startswith(download["pattern"][:-1])
    assert not steps["Upload integration diagnostics"]["with"]["name"].startswith(
        download["pattern"][:-1]
    )
    # Shards run, and the status job verifies, the same storage backend.
    lifecycle = steps["Run the bounded integration lifecycle"]["run"]
    verify = status["Require the shards to cover the full plan once"]["run"]
    assert ' nfs "${{ matrix.substrate }}"' in lifecycle
    assert "--backend nfs" in verify
    # The wrapper preserves the record the driver writes.
    assert f'"$run/{_MANIFEST.WORK_ITEMS_FILENAME}"' in wrapper
