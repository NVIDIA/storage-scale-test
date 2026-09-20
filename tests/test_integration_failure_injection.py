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

"""Tests for scenario-owned Elbencho failure injection."""

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPOSITORY_ROOT / "integration-tests" / "lib"))

from failure_injection import (  # pylint: disable=wrong-import-position
    FailureInjectionError,
    FailureInjectionOperations,
    build_slurm_failure_injection_plan,
    build_ssh_failure_injection_plan,
    cleanup_failure_injection,
    stage_failure_injection,
    staged_failure_injection,
)

TARGET_ARGUMENT = "/mnt/storage-test/results-e0002"


def _write_delegate(path, body):
    """Create one executable delegate used by wrapper process tests."""
    path.write_text(f"#!/usr/bin/env bash\nset -u\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _local_plan(tmp_path, *, runtime=False, exit_code=97):
    """Return a staged local Slurm-shaped plan."""
    source = tmp_path / "source-ussegl"
    _write_delegate(source, 'printf "stdout:%s\\n" "$*"; printf "stderr\\n" >&2')
    source_runtime = None
    if runtime:
        source_runtime = tmp_path / "source-runtime"
        source_runtime.mkdir()
        (source_runtime / "library.so").write_text("runtime", encoding="utf-8")
    plan = build_slurm_failure_injection_plan(
        scenario_id="failure-resume",
        staging_root=tmp_path.as_posix(),
        source_binary=source,
        source_runtime=source_runtime,
        target_argument=TARGET_ARGUMENT,
        shared_endpoint="local",
        injected_exit_code=exit_code,
    )
    operations = _local_operations(tmp_path)
    stage_failure_injection(plan, operations)
    return plan, operations


def _local_operations(base):
    """Return staging operations that require the expected local endpoint."""

    def _check(endpoint, path):
        assert endpoint == "local"
        assert Path(path).is_relative_to(base)
        return Path(path)

    def _make(endpoint, path):
        _check(endpoint, path).mkdir(parents=True, exist_ok=True)

    def _copy_file(source, endpoint, path, mode):
        destination = _check(endpoint, path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        destination.chmod(mode)

    def _copy_tree(source, endpoint, path):
        shutil.copytree(source, _check(endpoint, path))

    def _write(endpoint, path, content, mode):
        destination = _check(endpoint, path)
        destination.write_text(content, encoding="utf-8")
        destination.chmod(mode)

    def _remove(endpoint, path):
        shutil.rmtree(_check(endpoint, path), ignore_errors=True)

    return FailureInjectionOperations(_make, _copy_file, _copy_tree, _write, _remove)


def test_target_succeeds_then_fails_once_and_marker_survives_resume(tmp_path):
    """A successful target fails once, while resume delegates successfully."""
    plan, _ = _local_plan(tmp_path)
    command = [str(plan.layout.wrapper), "--write", TARGET_ARGUMENT]

    first = subprocess.run(command, text=True, capture_output=True, check=False)
    second = subprocess.run(command, text=True, capture_output=True, check=False)

    assert first.returncode == 97
    assert first.stdout == f"stdout:--write {TARGET_ARGUMENT}\n"
    assert first.stderr == "stderr\n"
    assert (Path(plan.layout.marker) / "state").read_text(
        encoding="utf-8"
    ) == "injected\n"
    assert second.returncode == 0
    assert second.stdout == first.stdout
    assert second.stderr == first.stderr
    assert Path(plan.layout.marker).is_dir()


def test_service_mode_execs_delegate_with_same_process_identity(tmp_path):
    """Service mode replaces the wrapper instead of leaving a parent process."""
    plan, _ = _local_plan(tmp_path)
    _write_delegate(Path(plan.layout.delegate), 'printf "%s\\n" "$BASHPID"')

    process = subprocess.Popen(
        [str(plan.layout.wrapper), "--service", TARGET_ARGUMENT],
        text=True,
        stdout=subprocess.PIPE,
    )
    output = process.communicate(timeout=5)[0]

    assert process.returncode == 0
    assert int(output.strip()) == process.pid
    assert not Path(plan.layout.marker).exists()


def test_nontarget_and_real_failure_are_not_replaced(tmp_path):
    """Only a successful target is eligible for the injected exit status."""
    plan, _ = _local_plan(tmp_path)

    nontarget = subprocess.run(
        [str(plan.layout.wrapper), "--write", "/another/path"], check=False
    )
    assert nontarget.returncode == 0
    assert not Path(plan.layout.marker).exists()

    _write_delegate(Path(plan.layout.delegate), "exit 23")
    failed = subprocess.run([str(plan.layout.wrapper), TARGET_ARGUMENT], check=False)
    assert failed.returncode == 23
    assert not Path(plan.layout.marker).exists()


def test_coordinator_forwards_signal_and_preserves_child_status(tmp_path):
    """Signals reach the real coordinator and its failure remains authoritative."""
    plan, _ = _local_plan(tmp_path)
    _write_delegate(
        Path(plan.layout.delegate),
        "trap 'exit 42' TERM\nprintf '%s\\n' \"$BASHPID\"\nwhile true; do sleep 1; done",
    )
    process = subprocess.Popen(
        [str(plan.layout.wrapper), TARGET_ARGUMENT],
        text=True,
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None
    child_pid = int(process.stdout.readline().strip())

    process.send_signal(signal.SIGTERM)
    process.wait(timeout=5)

    assert process.returncode == 42
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)
    assert not Path(plan.layout.marker).exists()


class _Recorder:
    """Record substrate-neutral staging operations without touching hosts."""

    def __init__(self):
        self.calls = []

    def operations(self):
        """Return callbacks backed by this recorder."""
        return FailureInjectionOperations(
            lambda endpoint, path: self.calls.append(("mkdir", endpoint, path)),
            lambda source, endpoint, path, mode: self.calls.append(
                ("copy-file", source, endpoint, path, mode)
            ),
            lambda source, endpoint, path: self.calls.append(
                ("copy-tree", source, endpoint, path)
            ),
            lambda endpoint, path, content, mode: self.calls.append(
                ("write", endpoint, path, content, mode)
            ),
            lambda endpoint, path: self.calls.append(("remove", endpoint, path)),
        )


def test_ssh_staging_places_delegate_runtime_on_every_worker(tmp_path):
    """SSH workers receive delegate trees but production copies their wrapper."""
    source = tmp_path / "elbencho"
    _write_delegate(source, "exit 0")
    runtime = tmp_path / "elbencho-runtime"
    runtime.mkdir()
    plan = build_ssh_failure_injection_plan(
        scenario_id="failure-resume",
        staging_root="/var/tmp/storage-scale-test",
        source_binary=source,
        source_runtime=runtime,
        target_argument=TARGET_ARGUMENT,
        coordinator_endpoint="coordinator",
        worker_endpoints=("worker-1", "worker-2"),
    )
    recorder = _Recorder()

    stage_failure_injection(plan, recorder.operations())

    file_endpoints = {call[2] for call in recorder.calls if call[0] == "copy-file"}
    tree_endpoints = {call[2] for call in recorder.calls if call[0] == "copy-tree"}
    writes = [call for call in recorder.calls if call[0] == "write"]
    assert file_endpoints == {"coordinator", "worker-1", "worker-2"}
    assert tree_endpoints == file_endpoints
    assert len(writes) == 1
    assert writes[0][1] == "coordinator"
    assert f"readonly delegate={plan.layout.delegate}" in writes[0][3]


def test_outer_context_retains_artifacts_through_resume_then_cleans(tmp_path):
    """No per-attempt cleanup removes the marker needed by resume."""
    source = tmp_path / "source-elbencho"
    _write_delegate(source, "exit 0")
    plan = build_slurm_failure_injection_plan(
        scenario_id="failure-resume",
        staging_root=tmp_path.as_posix(),
        source_binary=source,
        source_runtime=None,
        target_argument=TARGET_ARGUMENT,
        shared_endpoint="local",
    )
    operations = _local_operations(tmp_path)

    with staged_failure_injection(plan, operations):
        command = [str(plan.layout.wrapper), TARGET_ARGUMENT]
        assert subprocess.run(command, check=False).returncode == 97
        assert Path(plan.layout.marker).is_dir()
        assert subprocess.run(command, check=False).returncode == 0
        assert Path(plan.layout.marker).is_dir()

    assert not Path(plan.layout.root).exists()


def test_staging_rejects_missing_or_unsafe_inputs(tmp_path):
    """Plans reject unsafe roots and staging rejects missing delegates."""
    source = tmp_path / "missing"
    with pytest.raises(FailureInjectionError, match="dedicated absolute"):
        build_slurm_failure_injection_plan(
            scenario_id="failure-resume",
            staging_root="relative",
            source_binary=source,
            source_runtime=None,
            target_argument=TARGET_ARGUMENT,
            shared_endpoint="local",
        )

    plan = build_slurm_failure_injection_plan(
        scenario_id="failure-resume",
        staging_root=tmp_path.as_posix(),
        source_binary=source,
        source_runtime=None,
        target_argument=TARGET_ARGUMENT,
        shared_endpoint="local",
    )
    with pytest.raises(FailureInjectionError, match="not a regular file"):
        stage_failure_injection(plan, _local_operations(tmp_path))


def test_cleanup_runs_in_reverse_target_order(tmp_path):
    """Cleanup removes workers before the coordinator marker owner."""
    source = tmp_path / "elbencho"
    _write_delegate(source, "exit 0")
    plan = build_ssh_failure_injection_plan(
        scenario_id="failure-resume",
        staging_root="/var/tmp/storage-scale-test",
        source_binary=source,
        source_runtime=None,
        target_argument=TARGET_ARGUMENT,
        coordinator_endpoint="coordinator",
        worker_endpoints=("worker-1", "worker-2"),
    )
    recorder = _Recorder()

    cleanup_failure_injection(plan, recorder.operations())

    assert [call[1] for call in recorder.calls] == [
        "worker-2",
        "worker-1",
        "coordinator",
    ]
