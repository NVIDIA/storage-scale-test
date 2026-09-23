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

"""Fake-Elbencho contracts for the API-independent Kubernetes coordinator."""

import hashlib
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import textwrap
import time

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_COORDINATOR = (
    _REPOSITORY_ROOT
    / "storage-tests"
    / "fs"
    / "kubectl"
    / "_nv-elbencho-kubectl-coordinator.sh"
)
_KUBECTL_FUNCTIONS = _COORDINATOR.with_name("_nv-elbencho-kubectl-functions.sh")
_ELBENCHO_FUNCTIONS = _REPOSITORY_ROOT / "lib" / "_elbencho_functions.sh"
_PLATFORM_FUNCTIONS = _REPOSITORY_ROOT / "lib" / "_platform_functions.sh"
_BASH = shutil.which("bash") or "/bin/bash"


def _make_executable(path, text):
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refresh_bundle_manifest(control):
    rows = []
    for path in sorted(control.rglob("*")):
        if path.is_file() and path.name != "bundle-manifest.tsv":
            rows.append(f"{_sha256(path)}\t{path.relative_to(control)}\n")
    (control / "bundle-manifest.tsv").write_text("".join(rows), encoding="utf-8")


def _write_bundle(tmp_path, execution_count=2):
    """Create one PVC control bundle and a fake, context-aware Elbencho."""
    run = tmp_path / "run"
    control = run / "control"
    state_dir = run / "state"
    scratch = tmp_path / "scratch"
    executions = control / "executions"
    executions.mkdir(parents=True)
    shutil.copy2(_COORDINATOR, control / "coordinator.sh")
    for source in (_KUBECTL_FUNCTIONS, _ELBENCHO_FUNCTIONS, _PLATFORM_FUNCTIONS):
        shutil.copy2(source, control / source.name)
    (control / "env_used.yaml").write_text("schema: test\n", encoding="utf-8")
    (control / "run-metadata.tsv").write_text(
        "attempt_id\t1234abcd\noutput_basename\telbencho-20260923Z010203\n",
        encoding="utf-8",
    )
    (control / "env_used.sh").write_text(
        textwrap.dedent("""\
            declare -gA TEST_DIRS=([benchmark]=1)
            ELBENCHO_SCALE_READ_WRITE_DURATION=1s
            ELBENCHO_LIVE_CSV_EXTENDED=0
            ELBENCHO_LIVEINT=1000
            ELBENCHO_FILE_SIZE_MULTIPLIER=1
            ELBENCHO_FILE_LAYOUT=worker-directories
            ELBENCHO_FILES_PER_NODE=
            ELBENCHO_FILE_SIZE=
            ELBENCHO_READ_AFTER_WRITE_PAUSE=0
            FS_MAX_AGG_THROUGHPUT=0
            FS_MAX_NODE_THROUGHPUT_GBPS=0
            FS_MAX_NODE_IOPS=0
            ELBENCHO_SINGLE_BIG_FILE=0
            ELBENCHO_SINGLE_BIG_FILE_BASENAME=elbencho-bigfile
            ELBENCHO_SINGLE_BIG_FILE_SIZE=
            ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0
            ORDER_NODES_ENABLED=1
            """),
        encoding="utf-8",
    )
    (control / "worker-endpoints.tsv").write_text(
        "worker-a\tworker-a-pod\tpod-a\t10.10.0.1\n"
        "worker-b\tworker-b-pod\tpod-b\t10.10.0.2\n",
        encoding="utf-8",
    )
    for number in range(1, execution_count + 1):
        (executions / f"{number:04d}.sh").write_text(
            textwrap.dedent(f"""\
                export nodes=1
                export io_size=4K
                export thread_count=1
                export io_depth=1
                export dio_or_bio=dio
                export use_random=0
                export force_single=0
                export ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV=benchmark/target-{number}
                export ELBENCHO_RUN_GENERATED_TEST_ROOT=benchmark
                export ELBENCHO_RUN_TEST_DIR_SUFFIX=-test-e{number:04d}
                export ELBENCHO_SWEEP_WRITE_ONLY=1
                export ELBENCHO_SWEEP_WRITE_NO_READ=0
                export ELBENCHO_SWEEP_READ_FROM=''
                """),
            encoding="utf-8",
        )
    fake = tmp_path / "fake-elbencho"
    _make_executable(
        fake,
        """\
        #!/usr/bin/env bash
        set -eu
        id=$1 scratch=$2 durable=$3
        printf '%s|%s|%s|%s\n' "$id" "$ELBENCHO_RUN_COORDINATOR_LOCAL" \
            "$ELBENCHO_RUN_HOSTS_CSV" "$ELBENCHO_RUN_TEST_DIRS_CSV" \
            >> "$FAKE_ELBENCHO_RECORD"
        mkdir -p "$scratch/executions"
        printf 'artifact-%s\n' "$id" > "$scratch/result-${id}.txt"
        printf 'workload-%s\n' "$id" > "$scratch/executions/${id}.workload.tsv"
        [[ -z "${FAKE_ELBENCHO_FAIL_ID:-}" || "$id" != "$FAKE_ELBENCHO_FAIL_ID" ]]
        """,
    )
    fake_timeout = run / "fake-bin" / "timeout"
    fake_timeout.parent.mkdir()
    _make_executable(fake_timeout, "#!/usr/bin/env bash\nexit 0\n")
    _refresh_bundle_manifest(control)
    return control, state_dir, scratch, fake


def _run_coordinator(control, state_dir, scratch, fake, **extra_env):
    environment = os.environ.copy()
    pvc_root = control.parent.parent / "pvc"
    pvc_root.mkdir(exist_ok=True)
    environment.update(
        {
            "STORAGE_SCALE_TEST_INTEGRATION": "1",
            "KUBECTL_INTEGRATION_PVC_ROOT": str(pvc_root),
            "KUBECTL_INTEGRATION_FAKE_ELBENCHO": str(fake),
            "FAKE_ELBENCHO_RECORD": str(control.parent / "fake-record"),
            "PATH": f"{control.parent / 'fake-bin'}:{os.environ['PATH']}",
            "KUBECTL_COORDINATOR_POD_NODE": "coordinator-node",
            "KUBECTL_COORDINATOR_POD_NAME": "coordinator-pod",
            "KUBECTL_COORDINATOR_POD_UID": "coordinator-uid",
            "KUBECTL_COORDINATOR_POD_IP": "10.10.0.10",
        }
    )
    environment.update(extra_env)
    return subprocess.run(
        [
            _BASH,
            str(_COORDINATOR),
            str(control),
            str(state_dir),
            str(scratch),
            "1234abcd",
        ],
        cwd=_REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


def _run_recovery(control, state_dir, scratch):
    environment = os.environ.copy()
    pvc_root = control.parent.parent / "pvc"
    pvc_root.mkdir(exist_ok=True)
    environment.update(
        {
            "STORAGE_SCALE_TEST_INTEGRATION": "1",
            "KUBECTL_INTEGRATION_PVC_ROOT": str(pvc_root),
            "PATH": f"{control.parent / 'fake-bin'}:{os.environ['PATH']}",
        }
    )
    return subprocess.run(
        [
            _BASH,
            str(_COORDINATOR),
            "--recover-lost",
            str(control),
            str(state_dir),
            str(scratch),
            "1234abcd",
        ],
        cwd=_REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


def test_endpoint_probe_runs_socket_code_from_the_coordinator_file():
    """The bounded child does not carry socket code as inline shell text."""
    source = _COORDINATOR.read_text(encoding="utf-8")
    wrapper = source.split("_coordinator_probe_endpoint() {", maxsplit=1)[1].split(
        "_coordinator_probe_endpoint_request() {", maxsplit=1
    )[0]
    assert 'bash "$0" --probe-endpoint "$endpoint"' in wrapper
    assert "/dev/tcp" not in wrapper
    rejected = subprocess.run(
        [_BASH, str(_COORDINATOR), "--probe-endpoint", "999.10.0.1"],
        cwd=_REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0


def test_coordinator_uses_verified_bundle_context_and_manifest_last(tmp_path):
    """Only the durable manifest exposes both fully committed fake cells."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path)
    result = _run_coordinator(control, state_dir, scratch, fake)
    assert result.returncode == 0, result.stderr
    assert (state_dir / "run.status").read_text(encoding="utf-8").strip() == "SUCCESS"
    assert [
        (state_dir / "executions" / f"{number:04d}.status")
        .read_text(encoding="utf-8")
        .strip()
        for number in (1, 2)
    ] == ["SUCCESS", "SUCCESS"]
    manifest = (state_dir / "publication-manifest.tsv").read_text(encoding="utf-8")
    assert "execution\t0001\tSUCCESS" in manifest
    assert "execution\t0002\tSUCCESS" in manifest
    assert "result\tresults/0001/result-0001.txt\tresult-0001.txt" in manifest
    assert "ledger\texecutions/0002.status\texecutions/0002.status" in manifest
    assert (state_dir / "results" / "0001" / "result-0001.txt").read_text(
        encoding="utf-8"
    ) == "artifact-0001\n"
    pvc_root = control.parent.parent / "pvc"
    assert (control.parent / "fake-record").read_text(
        encoding="utf-8"
    ).splitlines() == [
        f"0001|1||{pvc_root}/benchmark/target-1",
        f"0002|1||{pvc_root}/benchmark/target-2",
    ]


def test_multinode_cell_persists_worker_selection_before_fake_elbencho(tmp_path):
    """Multi-node cells retain frozen Pod-IP evidence and pass it to Elbencho."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    definition = control / "executions" / "0001.sh"
    definition.write_text(
        definition.read_text(encoding="utf-8").replace(
            "export nodes=1", "export nodes=2"
        ),
        encoding="utf-8",
    )
    _refresh_bundle_manifest(control)
    fake_timeout = tmp_path / "bin" / "timeout"
    fake_timeout.parent.mkdir()
    _make_executable(fake_timeout, "#!/usr/bin/env bash\nexit 0\n")
    result = _run_coordinator(
        control,
        state_dir,
        scratch,
        fake,
        PATH=f"{fake_timeout.parent}:{os.environ['PATH']}",
    )
    assert result.returncode == 0, result.stderr
    assert (state_dir / "executions" / "0001.workers.tsv").read_text(
        encoding="utf-8"
    ).splitlines() == [
        "worker-a\tworker-a-pod\tpod-a\t10.10.0.1",
        "worker-b\tworker-b-pod\tpod-b\t10.10.0.2",
    ]
    pvc_root = control.parent.parent / "pvc"
    assert (control.parent / "fake-record").read_text(encoding="utf-8").strip() == (
        f"0001|0|10.10.0.1,10.10.0.2|{pvc_root}/benchmark/target-1"
    )


@pytest.mark.parametrize(
    ("boundary", "status", "manifest_present"),
    (
        ("after-running", "RUNNING", False),
        ("after-copy", "RUNNING", False),
        ("after-terminal", "SUCCESS", False),
        ("after-manifest", "SUCCESS", True),
    ),
)
def test_crash_boundaries_never_advertise_uncommitted_terminal_cells(
    tmp_path, boundary, status, manifest_present
):
    """A killed Job leaves only manifest-backed terminal work collectible."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    result = _run_coordinator(
        control,
        state_dir,
        scratch,
        fake,
        KUBECTL_INTEGRATION_CRASH_AFTER=boundary,
    )
    assert result.returncode != 0
    assert (state_dir / "executions" / "0001.status").read_text(
        encoding="utf-8"
    ).strip() == status
    manifest = state_dir / "publication-manifest.tsv"
    assert manifest.exists() is manifest_present
    if manifest_present:
        assert "execution\t0001\tSUCCESS" in manifest.read_text(encoding="utf-8")
    else:
        assert "execution\t0001\tSUCCESS" not in (
            manifest.read_text(encoding="utf-8") if manifest.exists() else ""
        )


@pytest.mark.parametrize(
    "boundary", ("after-lock", "after-snapshots", "after-execution-state")
)
def test_pre_running_crash_boundaries_recover_to_collectable_failure(
    tmp_path, boundary
):
    """A dead exact Job is recoverable after every durable startup checkpoint."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=2)
    state_dir.mkdir()
    (state_dir / "run.status").write_text("PREPARED\n", encoding="utf-8")
    crashed = _run_coordinator(
        control,
        state_dir,
        scratch,
        fake,
        KUBECTL_INTEGRATION_CRASH_AFTER=boundary,
    )
    assert crashed.returncode != 0
    assert (state_dir / "run.status").read_text(encoding="utf-8").strip() == (
        "PREPARED"
    )
    recovered = _run_recovery(control, state_dir, scratch)
    assert recovered.returncode == 0, recovered.stderr
    assert (state_dir / "run.status").read_text(encoding="utf-8").strip() == "FAILED"
    assert (state_dir / "executions/0001.status").read_text(
        encoding="utf-8"
    ).strip() == "FAILED"
    assert "observed_status\tPREPARED" in (
        state_dir / "coordinator-loss.tsv"
    ).read_text(encoding="utf-8")


def test_generated_target_symlink_cannot_escape_pvc(tmp_path):
    """Every derived workload path is resolved against live PVC symlinks."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    pvc_root = tmp_path / "pvc"
    outside = tmp_path / "outside"
    outside.mkdir()
    pvc_root.mkdir()
    (pvc_root / "benchmark").symlink_to(outside, target_is_directory=True)
    result = _run_coordinator(control, state_dir, scratch, fake)
    assert result.returncode != 0
    assert "workload path escapes the mounted PVC" in result.stderr
    assert list(outside.iterdir()) == []


def test_lost_coordinator_recovery_publishes_failed_running_cell(tmp_path):
    """A terminal Job can convert durable RUNNING evidence into a resume gate."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    crashed = _run_coordinator(
        control,
        state_dir,
        scratch,
        fake,
        KUBECTL_INTEGRATION_CRASH_AFTER="after-running",
    )
    assert crashed.returncode != 0
    assert (state_dir / "run.status").read_text(encoding="utf-8").strip() == "RUNNING"
    environment = os.environ.copy()
    environment.update(
        {
            "STORAGE_SCALE_TEST_INTEGRATION": "1",
            "PATH": f"{control.parent / 'fake-bin'}:{os.environ['PATH']}",
        }
    )
    recovered = subprocess.run(
        [
            _BASH,
            str(_COORDINATOR),
            "--recover-lost",
            str(control),
            str(state_dir),
            str(scratch),
            "1234abcd",
        ],
        cwd=_REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert (state_dir / "run.status").read_text(encoding="utf-8").strip() == "FAILED"
    assert (state_dir / "executions/0001.status").read_text(
        encoding="utf-8"
    ).strip() == "FAILED"
    assert (state_dir / "executions/0001.exitcode").read_text(
        encoding="utf-8"
    ).strip() == "143"
    assert "execution\t0001\tFAILED" in (
        state_dir / "publication-manifest.tsv"
    ).read_text(encoding="utf-8")


def test_job_loss_before_coordinator_lock_is_recoverable(tmp_path):
    """A Job that never started cannot strand a PREPARED PVC attempt."""
    control, state_dir, scratch, _ = _write_bundle(tmp_path, execution_count=1)
    state_dir.mkdir()
    (state_dir / "run.status").write_text("PREPARED\n", encoding="utf-8")
    recovered = _run_recovery(control, state_dir, scratch)
    assert recovered.returncode == 0, recovered.stderr
    assert (state_dir / "run.status").read_text(encoding="utf-8").strip() == "FAILED"
    owner = (state_dir / "coordinator.lock/owner.tsv").read_text(encoding="utf-8")
    assert "attempt_id\t1234abcd" in owner


def test_lost_coordinator_recovery_publishes_all_terminal_success(tmp_path):
    """A crash after the final cell is terminal still becomes collectable."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    crashed = _run_coordinator(
        control,
        state_dir,
        scratch,
        fake,
        KUBECTL_INTEGRATION_CRASH_AFTER="after-terminal",
    )
    assert crashed.returncode != 0
    assert (state_dir / "run.status").read_text(encoding="utf-8").strip() == "RUNNING"
    recovered = _run_recovery(control, state_dir, scratch)
    assert recovered.returncode == 0, recovered.stderr
    assert (state_dir / "run.status").read_text(encoding="utf-8").strip() == "SUCCESS"
    manifest = (state_dir / "publication-manifest.tsv").read_text(encoding="utf-8")
    assert "execution\t0001\tSUCCESS" in manifest
    assert "recovered_status\tSUCCESS" in (
        state_dir / "coordinator-loss.tsv"
    ).read_text(encoding="utf-8")


def test_lost_coordinator_repairs_terminal_status_manifest_window(tmp_path):
    """A crash after terminal run status can republish complete evidence."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    crashed = _run_coordinator(
        control,
        state_dir,
        scratch,
        fake,
        KUBECTL_INTEGRATION_CRASH_AFTER="after-terminal",
    )
    assert crashed.returncode != 0
    (state_dir / "run.status").write_text("SUCCESS\n", encoding="utf-8")
    (state_dir / "publication-manifest.tsv").unlink(missing_ok=True)
    recovered = _run_recovery(control, state_dir, scratch)
    assert recovered.returncode == 0, recovered.stderr
    manifest = (state_dir / "publication-manifest.tsv").read_text(encoding="utf-8")
    assert "execution\t0001\tSUCCESS" in manifest
    assert "ledger\trun.status\trun.status" in manifest


def test_lost_coordinator_recovery_preserves_all_terminal_failure(tmp_path):
    """All-terminal recovery retains a failed cell's durable exit code."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    crashed = _run_coordinator(
        control,
        state_dir,
        scratch,
        fake,
        FAKE_ELBENCHO_FAIL_ID="0001",
        KUBECTL_INTEGRATION_CRASH_AFTER="after-terminal",
    )
    assert crashed.returncode != 0
    recovered = _run_recovery(control, state_dir, scratch)
    assert recovered.returncode == 0, recovered.stderr
    assert (state_dir / "run.status").read_text(encoding="utf-8").strip() == "FAILED"
    assert "recovered_status\tFAILED" in (state_dir / "coordinator-loss.tsv").read_text(
        encoding="utf-8"
    )


def test_lost_coordinator_keeps_later_pending_cells_after_terminal_failure(tmp_path):
    """Recovery does not misattribute a prior failure to the next cell."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=2)
    crashed = _run_coordinator(
        control,
        state_dir,
        scratch,
        fake,
        FAKE_ELBENCHO_FAIL_ID="0001",
        KUBECTL_INTEGRATION_CRASH_AFTER="after-terminal",
    )
    assert crashed.returncode != 0
    assert (state_dir / "executions/0001.status").read_text(
        encoding="utf-8"
    ).strip() == "FAILED"
    assert (state_dir / "executions/0002.status").read_text(
        encoding="utf-8"
    ).strip() == "PENDING"
    recovered = _run_recovery(control, state_dir, scratch)
    assert recovered.returncode == 0, recovered.stderr
    assert (state_dir / "executions/0001.status").read_text(
        encoding="utf-8"
    ).strip() == "FAILED"
    assert (state_dir / "executions/0001.exitcode").read_text(
        encoding="utf-8"
    ).strip() == "1"
    assert (state_dir / "executions/0002.status").read_text(
        encoding="utf-8"
    ).strip() == "PENDING"
    assert "execution\t0002\tPENDING" in (
        state_dir / "publication-manifest.tsv"
    ).read_text(encoding="utf-8")


def test_lost_coordinator_before_first_cell_publishes_resumable_failure(tmp_path):
    """A Job lost after run startup marks the first pending cell resumable."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=2)
    crashed = _run_coordinator(
        control,
        state_dir,
        scratch,
        fake,
        KUBECTL_INTEGRATION_CRASH_AFTER="after-run-status",
    )
    assert crashed.returncode != 0
    assert (state_dir / "run.status").read_text(encoding="utf-8").strip() == "RUNNING"
    environment = os.environ.copy()
    environment.update(
        {
            "STORAGE_SCALE_TEST_INTEGRATION": "1",
            "PATH": f"{control.parent / 'fake-bin'}:{os.environ['PATH']}",
        }
    )
    recovered = subprocess.run(
        [
            _BASH,
            str(_COORDINATOR),
            "--recover-lost",
            str(control),
            str(state_dir),
            str(scratch),
            "1234abcd",
        ],
        cwd=_REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert (state_dir / "executions/0001.status").read_text(
        encoding="utf-8"
    ).strip() == "FAILED"
    assert (state_dir / "executions/0002.status").read_text(
        encoding="utf-8"
    ).strip() == "PENDING"
    assert "execution_observed_status\tPENDING" in (
        state_dir / "coordinator-loss.tsv"
    ).read_text(encoding="utf-8")


def test_cancel_recovery_publishes_collectable_terminal_ledger(tmp_path):
    """A deleted Job can still publish its interrupted cell as cancelled."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    crashed = _run_coordinator(
        control,
        state_dir,
        scratch,
        fake,
        KUBECTL_INTEGRATION_CRASH_AFTER="after-running",
    )
    assert crashed.returncode != 0
    environment = os.environ.copy()
    environment.update(
        {
            "STORAGE_SCALE_TEST_INTEGRATION": "1",
            "PATH": f"{control.parent / 'fake-bin'}:{os.environ['PATH']}",
        }
    )
    command = [
        _BASH,
        str(_COORDINATOR),
        "--finalize-cancelled",
        str(control),
        str(state_dir),
        str(scratch),
        "1234abcd",
    ]
    finalized = subprocess.run(
        command,
        cwd=_REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    assert finalized.returncode == 0, finalized.stderr
    assert (state_dir / "run.status").read_text(encoding="utf-8").strip() == (
        "CANCELLED"
    )
    assert (state_dir / "executions/0001.status").read_text(
        encoding="utf-8"
    ).strip() == "FAILED"
    assert (state_dir / "executions/0001.exitcode").read_text(
        encoding="utf-8"
    ).strip() == "143"
    manifest = (state_dir / "publication-manifest.tsv").read_text(encoding="utf-8")
    assert "execution\t0001\tFAILED" in manifest
    assert "ledger\trun.status\trun.status" in manifest
    repeated = subprocess.run(
        command,
        cwd=_REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    assert repeated.returncode == 0, repeated.stderr


def test_failure_overlay_preserves_first_failure_and_stops_later_cells(tmp_path):
    """The test-only overlay fails one cell without allowing the next cell."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=2)
    overlay = tmp_path / "fail-once"
    marker = tmp_path / "overlay-marker"
    _make_executable(
        overlay,
        f"""\
        #!/usr/bin/env bash
        if [[ "$1" == 0002 && "$4" == after-benchmark ]] \
                && mkdir {marker!s}; then
            printf 'injected\n' > "$2/injected.txt"
            exit 97
        fi
        """,
    )
    result = _run_coordinator(
        control,
        state_dir,
        scratch,
        fake,
        KUBECTL_INTEGRATION_FAILURE_OVERLAY=str(overlay),
    )
    assert result.returncode == 97
    assert (state_dir / "executions" / "0001.status").read_text(
        encoding="utf-8"
    ).strip() == "SUCCESS"
    assert (state_dir / "executions" / "0002.status").read_text(
        encoding="utf-8"
    ).strip() == "FAILED"
    assert (state_dir / "executions" / "0002.exitcode").read_text(
        encoding="utf-8"
    ).strip() == "97"
    assert "execution\t0001\tSUCCESS" in (
        state_dir / "publication-manifest.tsv"
    ).read_text(encoding="utf-8")
    assert "execution\t0002\tFAILED" in (
        state_dir / "publication-manifest.tsv"
    ).read_text(encoding="utf-8")
    assert (state_dir / "results" / "0002" / "injected.txt").exists()


def test_bundle_tampering_fails_before_state_or_lock_mutation(tmp_path):
    """The coordinator never adopts an altered control plan."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    (control / "executions" / "0001.sh").write_text("tampered\n", encoding="utf-8")
    result = _run_coordinator(control, state_dir, scratch, fake)
    assert result.returncode != 0
    assert not state_dir.exists()
    assert "bundle digest mismatch" in result.stderr


def test_startup_endpoint_failure_publishes_a_collectible_terminal_ledger(tmp_path):
    """Failure after the durable lock never strands an active-looking attempt."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    (control / "worker-endpoints.tsv").write_text(
        "broken\tbroken-pod\tpod\t999.10.0.1\n"
    )
    _refresh_bundle_manifest(control)
    result = _run_coordinator(control, state_dir, scratch, fake)
    assert result.returncode != 0
    assert (state_dir / "run.status").read_text(encoding="utf-8").strip() == "FAILED"
    assert (state_dir / "executions" / "0001.status").read_text(
        encoding="utf-8"
    ).strip() == "PENDING"
    manifest = (state_dir / "publication-manifest.tsv").read_text(encoding="utf-8")
    assert "ledger\tstartup-error.txt\tstartup-error.txt" in manifest
    assert "invalid worker endpoint evidence" in (
        state_dir / "startup-error.txt"
    ).read_text(encoding="utf-8")


def test_prebenchmark_mapping_failure_marks_the_cell_failed_and_committed(tmp_path):
    """A bad mapped target cannot leave a durable RUNNING cell behind."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    definition = control / "executions" / "0001.sh"
    definition.write_text(
        definition.read_text(encoding="utf-8").replace(
            "ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV=benchmark/target-1",
            "ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV=.storage-scale-test/unsafe",
        ),
        encoding="utf-8",
    )
    _refresh_bundle_manifest(control)
    result = _run_coordinator(control, state_dir, scratch, fake)
    assert result.returncode != 0
    assert (state_dir / "executions" / "0001.status").read_text(
        encoding="utf-8"
    ).strip() == "FAILED"
    assert (state_dir / "executions" / "0001.exitcode").read_text(
        encoding="utf-8"
    ).strip() == "1"
    assert "execution\t0001\tFAILED" in (
        state_dir / "publication-manifest.tsv"
    ).read_text(encoding="utf-8")


def test_missing_required_shared_workload_artifact_converts_success_to_failure(
    tmp_path,
):
    """A false-success fake Elbencho cannot publish incomplete shared output."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    definition = control / "executions" / "0001.sh"
    definition.write_text(
        definition.read_text(encoding="utf-8")
        + "export ELBENCHO_FILE_LAYOUT=shared-directory\n"
        + "export ELBENCHO_FILES_PER_NODE=1\n",
        encoding="utf-8",
    )
    _refresh_bundle_manifest(control)
    result = _run_coordinator(control, state_dir, scratch, fake)
    assert result.returncode != 0
    assert (state_dir / "executions" / "0001.status").read_text(
        encoding="utf-8"
    ).strip() == "FAILED"
    assert "lacks required artifact 0001.write.json" in result.stderr


def test_legacy_worker_directory_success_does_not_require_workload_metadata(tmp_path):
    """Legacy output remains complete without shared-layout TSV sidecars."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    fake_lines = fake.read_text(encoding="utf-8").splitlines()
    fake.write_text(
        "\n".join(
            line
            for line in fake_lines
            if "workload-%s" not in line and "workload.tsv" not in line
        )
        + "\n",
        encoding="utf-8",
    )
    result = _run_coordinator(control, state_dir, scratch, fake)
    assert result.returncode == 0, result.stderr
    assert (state_dir / "executions" / "0001.status").read_text(
        encoding="utf-8"
    ).strip() == "SUCCESS"
    assert not (state_dir / "results/0001/executions/0001.workload.tsv").exists()


def test_same_attempt_never_resets_running_or_retries_failed_cells(tmp_path):
    """Collection, not a second coordinator, is the resume boundary."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    state_dir.mkdir(parents=True)
    (state_dir / "executions").mkdir()
    (state_dir / "executions" / "0001.status").write_text("RUNNING\n", encoding="utf-8")
    result = _run_coordinator(control, state_dir, scratch, fake)
    assert result.returncode != 0
    assert "refusing to reset interrupted execution 0001" in result.stderr
    assert (state_dir / "executions" / "0001.status").read_text(
        encoding="utf-8"
    ).strip() == "RUNNING"


def test_collected_resume_selection_preserves_success_and_retries_other_states(
    tmp_path,
):
    """Only a collected terminal ledger is eligible for a new attempt."""
    state_dir = tmp_path / "collected-state"
    executions = state_dir / "executions"
    executions.mkdir(parents=True)
    (state_dir / "run.status").write_text("FAILED\n", encoding="utf-8")
    (state_dir / "publication-manifest.tsv").write_text("schema\t1\n", encoding="utf-8")
    for identifier, status in (
        ("0001", "SUCCESS"),
        ("0002", "FAILED"),
        ("0003", "RUNNING"),
        ("0004", "PENDING"),
    ):
        (executions / f"{identifier}.status").write_text(
            f"{status}\n", encoding="utf-8"
        )
    result = subprocess.run(
        [_BASH, str(_COORDINATOR), "--select-collected-resume", str(state_dir)],
        cwd=_REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "0001\tSKIP",
        "0002\tRUN",
        "0003\tRUN",
        "0004\tRUN",
    ]
    (state_dir / "run.status").write_text("RUNNING\n", encoding="utf-8")
    rejected = subprocess.run(
        [_BASH, str(_COORDINATOR), "--select-collected-resume", str(state_dir)],
        cwd=_REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode != 0


def test_coordinator_local_context_allows_only_the_one_node_hostless_case(tmp_path):
    """The Kubernetes one-node exception cannot weaken multi-node validation."""
    script = f"""
    source {_ELBENCHO_FUNCTIONS!s}
    io_size=4K
    thread_count=1
    io_depth=1
    dio_or_bio=dio
    use_random=0
    force_single=0
    elbencho_set_cell_run_context 0001 1 '' /mnt/test \\
        {tmp_path!s}/scratch {tmp_path!s}/durable \\
        _elbencho_noop_cell_hook _elbencho_noop_cell_hook 1
    [[ "$ELBENCHO_RUN_COORDINATOR_LOCAL" == 1 ]]
    ! elbencho_set_cell_run_context 0002 2 '' /mnt/test \\
        {tmp_path!s}/scratch {tmp_path!s}/durable \\
        _elbencho_noop_cell_hook _elbencho_noop_cell_hook 1
    ! elbencho_set_cell_run_context 0003 1 10.0.0.1 /mnt/test \\
        {tmp_path!s}/scratch {tmp_path!s}/durable \\
        _elbencho_noop_cell_hook _elbencho_noop_cell_hook 1
    """
    result = subprocess.run(
        [_BASH, "-c", textwrap.dedent(script)],
        cwd=_REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_signal_publishes_terminal_failure_without_erasing_scratch_evidence(tmp_path):
    """A terminating Job leaves a collector-visible FAILED cell and run."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    _make_executable(
        fake,
        """\
        #!/usr/bin/env bash
        set -eu
        mkdir -p "$2/executions"
        printf 'partial\n' > "$2/partial.txt"
        printf 'started\n' > "$2/started"
        sleep 30
        """,
    )
    environment = os.environ.copy()
    pvc_root = tmp_path / "pvc"
    pvc_root.mkdir()
    environment.update(
        {
            "STORAGE_SCALE_TEST_INTEGRATION": "1",
            "KUBECTL_INTEGRATION_PVC_ROOT": str(pvc_root),
            "KUBECTL_INTEGRATION_FAKE_ELBENCHO": str(fake),
            "FAKE_ELBENCHO_RECORD": str(control.parent / "fake-record"),
            "PATH": f"{control.parent / 'fake-bin'}:{os.environ['PATH']}",
            "KUBECTL_COORDINATOR_POD_NODE": "coordinator-node",
            "KUBECTL_COORDINATOR_POD_NAME": "coordinator-pod",
            "KUBECTL_COORDINATOR_POD_UID": "coordinator-uid",
            "KUBECTL_COORDINATOR_POD_IP": "10.10.0.10",
        }
    )
    process = subprocess.Popen(
        [
            _BASH,
            str(_COORDINATOR),
            str(control),
            str(state_dir),
            str(scratch),
            "1234abcd",
        ],
        cwd=_REPOSITORY_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        start_new_session=True,
    )
    running = state_dir / "executions" / "0001.status"
    deadline = time.monotonic() + 5
    while (
        not running.exists() or running.read_text(encoding="utf-8").strip() != "RUNNING"
    ) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert running.read_text(encoding="utf-8").strip() == "RUNNING"
    while not list(scratch.rglob("started")) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert list(scratch.rglob("started"))
    os.killpg(process.pid, signal.SIGTERM)
    _, stderr = process.communicate(timeout=10)
    assert process.returncode == 143, stderr
    assert running.read_text(encoding="utf-8").strip() == "FAILED"
    assert (state_dir / "run.status").read_text(encoding="utf-8").strip() == "FAILED"
    assert (state_dir / "results" / "0001" / "partial.txt").read_text(
        encoding="utf-8"
    ) == "partial\n"
