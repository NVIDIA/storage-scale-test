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

"""Fault and race contracts of multi-segment Kubernetes attempts.

A segment is one namespace: its claims (one Lease each), its workers, its
run tree and its coordinator Job. A batch whose targets span namespaces runs
one segment per namespace, started in order by the sequencer. These tests
drive the real lifecycle functions against a fake cluster made of function
overrides that log every external effect and keep per-namespace Job and
PVC-ledger state in files.
"""

import os
from pathlib import Path
import signal
import subprocess
import textwrap
import time

import pytest
import yaml

from tests.kubectl_segment_test_helpers import (
    bash_with_layout,
    events as _events,
    fake_cluster,
    keys as _keys,
    local_state as _local_state,
    publication as _publication,
    steps as _steps,
)
from tests.test_elbencho_kubectl_coordinator_shell import (
    _BASH,
    _CLAIMS,
    _COORDINATOR,
    _REPOSITORY_ROOT,
    _make_executable,
    _run_coordinator,
    _write_bundle,
)

_FUNCTIONS = (
    _REPOSITORY_ROOT / "storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
)
_ATTEMPT = "1234abcd"
_NONCE = "0123456789abcdef0123456789abcdef"
_SEQUENCER_IMAGE = "registry.example/kubectl@sha256:" + "a" * 64
_HELPER = "0000aaaa"
_SWEEP_A = f"sst-elb-{_ATTEMPT}-sweep"
_SWEEP_B = f"sst-elb-{_ATTEMPT}-s1-sweep"
_SEQUENCER = f"sst-elb-{_ATTEMPT}-sequencer"
_RESUMED = "5678ef01"
_ROOT_A = "ns-a/pvc-a:benchmark"
_ROOT_B = "ns-b/pvc-b:benchmark"
_ROOT_C = "ns-c/pvc-c:benchmark"


def _bash(body: str) -> subprocess.CompletedProcess[str]:
    """Run BODY after loading the lifecycle helpers and segment 0's layout."""
    return bash_with_layout(body, _ROOT_A)


def _fake_cluster(tmp_path: Path) -> str:
    """The shared fake cluster, with this file's one-claim-per-namespace
    identities and its coarser event lines."""
    return fake_cluster(tmp_path) + textwrap.dedent("""\
        kubectl_validate_cluster_identity() {
            local namespace="$1" claim
            shift
            event "identity $namespace $*"
            for claim in "$@"; do
                printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' \\
                    "$namespace" "$namespace" "$claim" "$claim" "$claim" "$claim"
            done
        }
        kubectl_validate_pvc_paths() { event "paths $1 $2"; }
        kubectl_validate_test_root_access() { event "access $1 $2"; }
        kubectl_acquire_pvc_lease() {
            kubectl_attempt_load_identity "$1/attempts/$3" || return 1
            local key
            key=$(_kubectl_selected_claim_lease_key) || return 1
            if [[ "${HELD_LEASE:-}" == "$KUBECTL_NAMESPACE" ]]; then
                event "lease-held $KUBECTL_NAMESPACE"
                return 1
            fi
            kubectl_attempt_journal_resource "$1" "$2" "$3" "$key" Lease \\
                "$KUBECTL_PVC_LEASE_NAME" "$KUBECTL_NAMESPACE" "uid-$key" \\
                "$KUBECTL_OWNERSHIP_NONCE" || return 1
            event "lease $KUBECTL_NAMESPACE $key"
        }
        kubectl_reserve_remote_attempt() {
            event "reserve $1 $2"
            if [[ "${ALIASED_NAMESPACE:-}" == "$1" ]]; then
                echo "attempt run tree already exists: /mnt/storage-scale-test/x" >&2
                return 1
            fi
        }
        kubectl_verify_journaled_pvc_lease() { event "verify-lease $KUBECTL_NAMESPACE"; }
        """)


def _configuration(*roots: str) -> str:
    """Shell lines writing "$metadata/configuration.sh" for these control roots."""
    first = roots[0]
    claim, path = first.split("/", 1)[1].split(":", 1)
    return textwrap.dedent(f"""\
        {{
            printf 'KUBECTL_CONTROL_LOGICAL_ROOT={first}\\n'
            printf 'KUBECTL_CONTROL_TEST_ROOT=/mnt/storage-scale-test/{claim}/{path}\\n'
            printf 'KUBECTL_CONTROL_ROOT=/mnt/storage-scale-test/{claim}/{path}/.storage-scale-test\\n'
            printf 'declare -ga KUBECTL_SEGMENT_CONTROL_ROOTS=( {' '.join(roots)} )\\n'
        }} > "$metadata/configuration.sh"
        declare -ga KUBECTL_SEGMENT_CONTROL_ROOTS=( {' '.join(roots)} )
        """)


def _two_target_attempt(root: Path, transitions: str = "SUBMITTED") -> str:
    """Publish a submitted two-segment attempt (ns-a, ns-b) with every journal."""
    return (
        textwrap.dedent(f"""\
        root={str(root)!r}
        kubectl_local_lock_acquire "$root" fd
        kubectl_attempt_create_identity "$root" "$fd" {_ATTEMPT} {_NONCE} \\
            ns-a nsuid-a pv-a pvuid-a pvc-a pvcuid-a \\
            ns-b nsuid-b pv-b pvuid-b pvc-b pvcuid-b
        kubectl_attempt_write_state "$root" "$fd" {_ATTEMPT} PREPARED
        for next in {transitions}; do
            kubectl_attempt_transition "$root" "$fd" {_ATTEMPT} "$next"
        done
        kubectl_attempt_write_current "$root" "$fd" {_ATTEMPT}
        metadata="$root/attempts/{_ATTEMPT}"
        """)
        + _configuration(_ROOT_A, _ROOT_B)
        + textwrap.dedent(f"""\
        printf 'node-a\\tuid-a\\tamd64\\n' > "$metadata/nodes.tsv"
        journal() {{
            kubectl_attempt_journal_resource "$root" "$fd" {_ATTEMPT} "$1" "$2" "$3" \\
                "$4" "uid-$3" {_NONCE}
        }}
        journal sweep Job sst-elb-{_ATTEMPT}-sweep ns-a
        journal sweep-s1 Job sst-elb-{_ATTEMPT}-s1-sweep ns-b
        journal workers DaemonSet sst-elb-{_ATTEMPT}-workers ns-a
        journal workers-s1 DaemonSet sst-elb-{_ATTEMPT}-s1-workers ns-b
        journal worker-net NetworkPolicy sst-elb-{_ATTEMPT}-worker-net ns-a
        journal coord-net NetworkPolicy sst-elb-{_ATTEMPT}-coord-net ns-a
        journal worker-net-s1 NetworkPolicy sst-elb-{_ATTEMPT}-s1-worker-net ns-b
        journal coord-net-s1 NetworkPolicy sst-elb-{_ATTEMPT}-s1-coord-net ns-b
        journal sequencer-sa ServiceAccount sst-elb-{_ATTEMPT}-sequencer ns-a
        journal sequencer-role-s0 Role sst-elb-{_ATTEMPT}-sequencer ns-a
        journal sequencer-binding-s0 RoleBinding sst-elb-{_ATTEMPT}-sequencer ns-a
        journal sequencer-role-s1 Role sst-elb-{_ATTEMPT}-s1-sequencer ns-b
        journal sequencer-binding-s1 RoleBinding sst-elb-{_ATTEMPT}-s1-sequencer ns-b
        journal sequencer Job sst-elb-{_ATTEMPT}-sequencer ns-a
        kubectl_attempt_load_identity "$root/attempts/{_ATTEMPT}"
        for claim in 0 1; do
            key=$(kubectl_claim_lease_key "$claim")
            kubectl_attempt_journal_resource "$root" "$fd" {_ATTEMPT} "$key" Lease \\
                "${{KUBECTL_CLAIM_LEASE_NAMES[claim]}}" \\
                "${{KUBECTL_CLAIM_NAMESPACES[claim]}}" "uid-$key" {_NONCE}
        done
        for target in 0 1; do
            kubectl_with_segment "$target" kubectl_attempt_journal_remote_reservation \\
                "$root" "$fd" {_ATTEMPT} {_NONCE}
            kubectl_with_segment "$target" kubectl_attempt_mark_remote_reservation_acquired \\
                "$root" "$fd" {_ATTEMPT}
        done
        mkdir -p "$metadata/control-bundle/executions" "$metadata/control-bundle-s1/executions"
        for id in 0001 0003; do printf 'export nodes=1\\n' > "$metadata/control-bundle/executions/$id.sh"; done
        for id in 0002 0004; do printf 'export nodes=1\\n' > "$metadata/control-bundle-s1/executions/$id.sh"; done
        kubectl_local_lock_release "$fd"
        results={str(root.parent)!r}
        """)
    )


def _write_plan(plan: Path, segments: int = 2) -> None:
    """A segment plan (kubectl_plan_segments) for ns-a and, optionally, ns-b."""
    plan.mkdir(parents=True)
    rows = []
    for index, (namespace, claim) in enumerate(
        (("ns-a", "pvc-a"), ("ns-b", "pvc-b"))[:segments]
    ):
        rows.append(f"{index}\t{namespace}\t{namespace}/{claim}:benchmark\t{claim}")
        mapped = f"/mnt/storage-scale-test/{claim}/benchmark\n"
        (plan / f"segment-{index}.roots").write_text(mapped, encoding="utf-8")
        (plan / f"segment-{index}.paths").write_text(mapped, encoding="utf-8")
    (plan / "segments.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")


def _prepare_body(plan: Path) -> str:
    return textwrap.dedent(f"""\
        attempt=
        kubectl_prepare_attempt_lifecycle attempt "$results" 2 {str(plan)!r} ''
        """)


def _upload(attempt: str = _ATTEMPT, target: int = 0) -> str:
    infix = "" if target == 0 else f"-s{target}"
    return f"sst-elb-{attempt}{infix}-upload-{_HELPER}"


def _write_group(results: Path, group: str, kind: str, namespace: str, claim: str):
    basename = "elbencho" if kind == "io" else "mdtest-elbencho"
    relative = f"groups/{group}/{basename}-20260101Z000000"
    (results / relative).mkdir(parents=True)
    (results / relative / "env_used.sh").write_text(
        f'declare -gA TEST_DIRS=(["{namespace}/{claim}:benchmark"]=1)\n'
        f"export KUBECTL_NAMESPACE={namespace}\n",
        encoding="utf-8",
    )
    return f"group\t{group}\t{kind}\t{relative}\tx\ty"


def _write_batch(results: Path, namespaces: tuple[str, ...]) -> None:
    """A sealed batch with one IO group (and execution) per namespace."""
    rows = ["version\t1", "revision\t4", "datestamp\t20260101Z000000"]
    for number, namespace in enumerate(namespaces, start=1):
        rows.append(
            _write_group(
                results, f"{number:04d}", "io", namespace, f"pvc-{namespace[-1]}"
            )
        )
    for number in range(1, len(namespaces) + 1):
        rows.append(f"execution\t{number:04d}\t{number:04d}\th")
    (results / "batch-manifest.tsv").write_text("\n".join(rows) + "\n")
    (results / "executions").mkdir()
    for number in range(1, len(namespaces) + 1):
        (results / "executions" / f"{number:04d}.sh").write_text("export nodes=1\n")
    union = " ".join(
        f'["{namespace}/pvc-{namespace[-1]}:benchmark"]=1' for namespace in namespaces
    )
    (results / "env_used.sh").write_text(
        f"KUBECTL_NAMESPACE=ns-a\ndeclare -gA TEST_DIRS=({union})\n",
        encoding="utf-8",
    )


_SUBMIT = """
    elbencho_batch_verify_manifest() { :; }
    _kubectl_plan_execution_paths() { :; }
    kubectl_populate_sweep_control_bundle() {
        event "populate ${1##*/} $(paste -sd, "$5") ${6//$'\\t'/|}"
        printf x > "$1/payload"
    }
    kubectl_upload_control_bundle() { event "upload $1 $2 ${4##*/}"; }
"""


def _status(tmp_path: Path, setup: str, operation: str = "status"):
    state = tmp_path / "results" / "kubernetes"
    state.mkdir(parents=True)
    body = (
        _fake_cluster(tmp_path)
        + _two_target_attempt(state)
        + setup
        + ': > "$events"\n'
        + f'kubectl_lifecycle_operation {operation} "$results"\n'
    )
    return _bash(body), state


_COLLECT = f"""
    for target in 0 1; do
        rm -f "$metadata/control-bundle$(kubectl_segment_suffix $target)/executions"/*.sh
    done
    printf 'export nodes=1\\n' > "$metadata/control-bundle/executions/0001.sh"
    printf 'export nodes=1\\n' > "$metadata/control-bundle-s1/executions/0002.sh"
    set_fake job ns-a COMPLETE; set_fake job ns-b COMPLETE
    set_fake remote ns-a SUCCESS; set_fake remote ns-b SUCCESS
    kubectl_remote_attempt_apparent_bytes() {{ printf -v "$4" 1024; }}
    kubectl_stream_remote_attempt() {{
        event "stream $1 $2"
        tar -C "$fake/tree-$1" -cf "$4" {_ATTEMPT}
    }}
"""


def _collect(tmp_path: Path, repeat: int = 1):
    state = tmp_path / "results" / "kubernetes"
    state.mkdir(parents=True)
    _publication(tmp_path / "fake" / "tree-ns-a", "0001", "segment\t0\n")
    _publication(tmp_path / "fake" / "tree-ns-b", "0002", "segment\t1\n")
    runs = "\n".join(
        [
            'outcome=0; kubectl_lifecycle_operation collect "$results" || outcome=$?',
            'printf "COLLECT_EXIT=%s\\n" "$outcome"',
        ]
        * repeat
    )
    body = (
        _fake_cluster(tmp_path)
        + _two_target_attempt(state)
        + _COLLECT
        + ': > "$events"\n'
        + runs
        + "\n"
    )
    return _bash(body), state


def _fake_value(tmp_path: Path, name: str) -> str:
    return (tmp_path / "fake" / name).read_text(encoding="utf-8")


def _status_after(tmp_path: Path, state: Path, setup: str = "") -> dict[str, str]:
    """Run --status in a fresh process against the same fake cluster."""
    result = _bash(
        _fake_cluster(tmp_path)
        + f"results={str(state.parent)!r}\n"
        + setup
        + 'kubectl_lifecycle_operation status "$results"\n'
    )
    assert result.returncode == 0, result.stderr
    return _keys(result.stdout)


# Job ns-a is ACTIVE when the segments are classified and COMPLETE, with its
# ledger SUCCESS, when cancellation observes it again right before deletion.
_COMPLETES_BEFORE_DELETION = """
    kubectl_observe_segment_job() {
        event "observe $3"
        local count=0
        [[ ! -f "$fake/observed-$3" ]] || count=$(cat "$fake/observed-$3")
        printf '%s' "$((count + 1))" > "$fake/observed-$3"
        if [[ "$3" == ns-a && "$count" -ge 1 ]]; then
            set_fake job ns-a COMPLETE
            set_fake remote ns-a SUCCESS
        fi
        printf -v "$1" '%s' "$(cat "$fake/job-$3")"
    }
"""


def test_cancel_keeps_a_segment_that_completed_before_its_deletion(tmp_path):
    """[T-15] A segment that completes during cancel is kept; the next is cancelled."""
    result, state = _status(
        tmp_path,
        """
        set_fake job ns-a ACTIVE; set_fake job ns-b SUSPENDED
        set_fake remote ns-a RUNNING; set_fake remote ns-b PREPARED
        set_fake progress ns-b '2 0 0 0'
        """ + _COMPLETES_BEFORE_DELETION,
        "cancel",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "STATE=CANCELLED"
    events = _events(tmp_path)
    # The sequencer is stopped before any segment is classified.
    assert events.index(f"delete Job ns-a/{_SEQUENCER}") < events.index("observe ns-b")
    assert f"delete Job ns-a/{_SWEEP_A}" not in events
    assert not any(event.startswith("finalize-cancelled ns-a") for event in events)
    assert f"delete Job ns-b/{_SWEEP_B}" in events
    assert "finalize-cancelled ns-b inspector-ns-b" in events
    assert _fake_value(tmp_path, "remote-ns-a") == "SUCCESS"
    assert _local_state(state) == "TERMINAL"
    keys = _status_after(tmp_path, state, "set_fake job ns-a COMPLETE\n")
    assert (keys["STATE"], keys["NEXT_ACTION"]) == ("CANCELLED", "COLLECT")
    assert (keys["SEGMENT_0_STATE"], keys["SEGMENT_1_STATE"]) == (
        "SUCCESS",
        "CANCELLED",
    )


def test_drift_handling_keeps_a_segment_that_completed_meanwhile(tmp_path):
    """[T-15] Drift fails the next segment, never one that just succeeded."""
    result, state = _status(
        tmp_path,
        """
        set_fake job ns-a ACTIVE; set_fake job ns-b SUSPENDED
        set_fake remote ns-a RUNNING; set_fake progress ns-a '1 1 0 0'
        set_fake remote ns-b PREPARED; set_fake progress ns-b '2 0 0 0'
        DRIFT_NAMESPACE=ns-b
        """ + _COMPLETES_BEFORE_DELETION,
    )
    assert result.returncode != 0
    assert "segment 0 (namespace ns-a) finished while its worker drift was handled" in (
        result.stderr
    )
    events = _events(tmp_path)
    # The re-check of Job ns-a follows the sequencer's deletion.
    recheck = len(events) - 1 - events[::-1].index("observe ns-a")
    assert events.index(f"delete Job ns-a/{_SEQUENCER}") < recheck
    assert f"delete Job ns-a/{_SWEEP_A}" not in events
    assert _local_state(state) == "SUBMITTED"
    keys = _status_after(tmp_path, state, "DRIFT_NAMESPACE=ns-b\n")
    assert keys["STATE"] == "FAILED"
    assert (keys["SEGMENT_0_STATE"], keys["SEGMENT_1_STATE"]) == ("SUCCESS", "FAILED")
    assert _fake_value(tmp_path, "remote-ns-a") == "SUCCESS"
    assert _local_state(state) == "TERMINAL"


def test_cancel_keeps_success_published_while_the_job_was_deleted(tmp_path):
    """[T-15] SUCCESS published during Job deletion survives and is journaled."""
    result, state = _status(
        tmp_path,
        f"""
        set_fake job ns-a ACTIVE; set_fake job ns-b SUSPENDED
        set_fake remote ns-a RUNNING; set_fake remote ns-b PREPARED
        set_fake progress ns-b '2 0 0 0'
        kubectl_delete_owned_object() {{
            event "delete $1 $3/$2"
            # The coordinator finishes its last cell while it is deleted.
            [[ "$1/$3/$2" != Job/ns-a/{_SWEEP_A} ]] || set_fake remote ns-a SUCCESS
        }}
        # Like the coordinator: never overwrite a terminal ledger.
        kubectl_finalize_cancelled_attempt() {{
            event "finalize-cancelled $1 $2"
            case "$(cat "$fake/remote-$1")" in
                PREPARED|RUNNING) set_fake remote "$1" CANCELLED ;;
            esac
        }}
        """,
        "cancel",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "STATE=CANCELLED"
    assert _fake_value(tmp_path, "remote-ns-a") == "SUCCESS"
    assert _fake_value(tmp_path, "remote-ns-b") == "CANCELLED"
    steps = _steps(state)
    assert {"delete-sweep", "segment-succeeded", "delete-sweep-s1"} <= steps
    keys = _status_after(tmp_path, state)
    assert (keys["SEGMENT_0_STATE"], keys["SEGMENT_1_STATE"]) == (
        "SUCCESS",
        "CANCELLED",
    )


def test_cancel_also_cancels_a_segment_the_sequencer_started_first(tmp_path):
    """[T-15] A next segment started just before the sequencer stopped is cancelled."""
    result, state = _status(
        tmp_path,
        """
        set_fake job ns-a COMPLETE; set_fake job ns-b ACTIVE
        set_fake remote ns-a SUCCESS; set_fake remote ns-b RUNNING
        """,
        "cancel",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "STATE=CANCELLED"
    assert "refusing to abandon" not in result.stderr
    events = _events(tmp_path)
    assert f"delete Job ns-a/{_SWEEP_A}" not in events
    assert f"delete Job ns-b/{_SWEEP_B}" in events
    assert "finalize-cancelled ns-b inspector-ns-b" in events
    assert _fake_value(tmp_path, "remote-ns-a") == "SUCCESS"
    assert _local_state(state) == "TERMINAL"


@pytest.mark.parametrize("failing_delete", ["Job/ns-a", "Job/ns-b"])
def test_interrupted_multi_target_cancel_completes_when_repeated(
    tmp_path, failing_delete
):
    """[T-08] A cancel stopped in CANCEL_REQUESTED finishes when it is repeated.

    Job/ns-a fails the sequencer's deletion (cancellation has only begun);
    Job/ns-b fails abandoning the later segment after the current one was
    cancelled and finalized.
    """
    state = tmp_path / "results" / "kubernetes"
    state.mkdir(parents=True)
    body = _fake_cluster(tmp_path) + _two_target_attempt(state) + textwrap.dedent(f"""\
            set_fake job ns-a ACTIVE; set_fake job ns-b SUSPENDED
            set_fake remote ns-a RUNNING
            FAIL_DELETE_ONCE={failing_delete}
            : > "$events"
            for run in 1 2; do
                outcome=0
                kubectl_lifecycle_operation cancel "$results" || outcome=$?
                printf 'CANCEL_EXIT=%s\\n' "$outcome"
                kubectl_attempt_load_metadata "$root/attempts/{_ATTEMPT}"
                printf 'LOCAL=%s\\n' "$KUBECTL_LIFECYCLE_STATE"
            done
            """)
    result = _bash(body)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "CANCEL_EXIT=1" in lines and "LOCAL=CANCEL_REQUESTED" in lines
    assert lines[-2:] == ["CANCEL_EXIT=0", "LOCAL=TERMINAL"]
    assert "STATE=CANCELLED" in lines
    events = _events(tmp_path)
    assert events.count("finalize-cancelled ns-a inspector-ns-a") == 1
    assert f"delete Job ns-b/{_SWEEP_B}" in events
    assert {"delete-sequencer", "delete-sweep", "delete-sweep-s1"} <= _steps(state)


def test_cancel_requested_status_names_the_segment_being_cancelled(tmp_path):
    """[T-09] Status of an incomplete cancel names the segment being cancelled."""
    state = tmp_path / "results" / "kubernetes"
    state.mkdir(parents=True)
    result = _bash(
        _fake_cluster(tmp_path)
        + _two_target_attempt(state, "SUBMITTED CANCEL_REQUESTED")
        + "set_fake job ns-a COMPLETE; set_fake job ns-b ACTIVE\n"
        + 'kubectl_lifecycle_operation status "$results"\n'
    )
    assert result.returncode != 0
    keys = _keys(result.stdout)
    assert keys["STATE"] == "CANCEL_REQUESTED"
    assert keys["SEGMENT_CURRENT"] == "1"
    assert keys["SEGMENT_1_NAMESPACE"] == "ns-b"
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=CANCELLATION_INCOMPLETE" in (
        result.stderr
    )
    assert f"STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_NAME={_SWEEP_B}" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_NAMESPACE=ns-b" in result.stderr
    assert "Cancellation of segment 1 (namespace ns-b) is incomplete" in result.stderr


def test_single_target_cancel_keeps_a_job_that_completed_first(tmp_path):
    """[T-15] One segment: a Job Complete at the re-check is neither deleted nor finalized."""
    ledger = tmp_path / "ledger"
    ledger.write_text("RUNNING", encoding="utf-8")
    result = _bash(f"""
        set -e
        root={str(tmp_path / 'state')!r}
        kubectl_local_lock_acquire "$root" fd
        kubectl_attempt_create_identity "$root" "$fd" {_ATTEMPT} {_NONCE} test-ns \\
            namespace-uid test-pv pv-uid test-pvc pvc-uid
        kubectl_attempt_write_state "$root" "$fd" {_ATTEMPT} PREPARED
        kubectl_attempt_transition "$root" "$fd" {_ATTEMPT} SUBMITTED
        kubectl_attempt_journal_resource "$root" "$fd" {_ATTEMPT} sweep \\
            Job {_SWEEP_A} test-ns job-uid {_NONCE}
        kubectl_read_remote_status() {{ cat {str(ledger)!r}; }}
        kubectl_observe_segment_job() {{
            printf SUCCESS > {str(ledger)!r}
            printf -v "$1" COMPLETE
        }}
        kubectl_delete_owned_object() {{ echo unexpected-delete >&2; return 1; }}
        kubectl_finalize_cancelled_attempt() {{ echo unexpected-finalize >&2; return 1; }}
        outcome=$(kubectl_cancel_journaled_attempt "$root" "$fd" test-ns helper \\
            {_ATTEMPT} {_NONCE})
        [[ "$outcome" == SUCCESS ]]
        kubectl_attempt_load_metadata "$root/attempts/{_ATTEMPT}"
        [[ "$KUBECTL_LIFECYCLE_STATE" == TERMINAL ]]
        ! kubectl_attempt_step_done "$root" {_ATTEMPT} delete-sweep
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr
    assert "unexpected" not in result.stderr


def _finalize_cancelled(control: Path, state_dir: Path, scratch: Path):
    environment = os.environ.copy()
    environment.update(
        {
            "STORAGE_SCALE_TEST_INTEGRATION": "1",
            "PATH": f"{control.parent / 'fake-bin'}:{os.environ['PATH']}",
        }
    )
    return subprocess.run(
        [
            _BASH,
            str(_COORDINATOR),
            "--finalize-cancelled",
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


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


@pytest.mark.parametrize(
    ("fail_id", "outcome", "exit_code"), [("", "SUCCESS", "0"), ("0001", "FAILED", "1")]
)
def test_finalize_cancelled_never_overwrites_a_terminal_outcome(
    tmp_path, fail_id, outcome, exit_code
):
    """[T-15] --finalize-cancelled keeps SUCCESS and a benchmark's own FAILED."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    finished = _run_coordinator(
        control, state_dir, scratch, fake, FAKE_ELBENCHO_FAIL_ID=fail_id
    )
    assert (finished.returncode == 0) is (outcome == "SUCCESS"), finished.stderr
    assert _read(state_dir / "run.status") == outcome
    finalized = _finalize_cancelled(control, state_dir, scratch)
    assert finalized.returncode == 0, finalized.stderr
    assert _read(state_dir / "run.status") == outcome
    assert _read(state_dir / "executions/0001.status") == outcome
    assert _read(state_dir / "executions/0001.exitcode") == exit_code
    manifest = (state_dir / "publication-manifest.tsv").read_text(encoding="utf-8")
    assert f"execution\t0001\t{outcome}" in manifest


def test_finalize_cancelled_converts_the_failure_its_job_deletion_caused(tmp_path):
    """[T-07] The FAILED a TERM-ed coordinator publishes becomes CANCELLED."""
    control, state_dir, scratch, fake = _write_bundle(tmp_path, execution_count=1)
    _make_executable(
        fake,
        """\
        #!/usr/bin/env bash
        mkdir -p "$2"
        printf 'started\\n' > "$2/started"
        exec sleep 30
        """,
    )
    environment = os.environ.copy()
    pvc_root = tmp_path / "pvc"
    for claim in _CLAIMS:
        (pvc_root / claim).mkdir(parents=True, exist_ok=True)
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
    with subprocess.Popen(
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
    ) as process:
        deadline = time.monotonic() + 10
        while not list(scratch.rglob("started")) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert list(scratch.rglob("started"))
        # Deleting the Job terminates the coordinator Pod's processes.
        os.killpg(process.pid, signal.SIGTERM)
        _, stderr = process.communicate(timeout=20)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert process.returncode == 143, stderr
    assert _read(state_dir / "run.status") == "FAILED"
    finalized = _finalize_cancelled(control, state_dir, scratch)
    assert finalized.returncode == 0, finalized.stderr
    assert _read(state_dir / "run.status") == "CANCELLED"
    assert "run_status\tCANCELLED" in (state_dir / "run-summary.tsv").read_text(
        encoding="utf-8"
    )


def test_segment_classification_observes_later_segments_first(tmp_path):
    """[T-16] A segment that completes between two GETs never looks out of order."""
    state = tmp_path / "results" / "kubernetes"
    state.mkdir(parents=True)
    result = _bash(
        _fake_cluster(tmp_path) + _two_target_attempt(state) + textwrap.dedent(f"""\
            # Before the first GET Job ns-a runs and Job ns-b is suspended; at
            # every later GET Job ns-a is Complete and the sequencer has
            # started Job ns-b.
            kubectl_observe_segment_job() {{
                event "observe $3"
                local now=0 observed
                [[ ! -f "$fake/clock" ]] || now=$(cat "$fake/clock")
                printf '%s' "$((now + 1))" > "$fake/clock"
                case "$now:$3" in
                    0:ns-a) observed=ACTIVE ;;
                    0:ns-b) observed=SUSPENDED ;;
                    *:ns-a) observed=COMPLETE ;;
                    *) observed=ACTIVE ;;
                esac
                printf -v "$1" '%s' "$observed"
            }}
            : > "$events"
            segments=()
            _kubectl_classify_segments segments "$root" {_ATTEMPT} 2
            _kubectl_current_segment current {_ATTEMPT} "${{segments[@]}}"
            printf 'SEGMENTS=%s CURRENT=%s\\n' "${{segments[*]}}" "$current"
            # Observing first to last over the same history is inconsistent.
            rm -f "$fake/clock"
            forward=()
            for target in 0 1; do
                kubectl_with_segment "$target" _kubectl_observe_selected_segment \\
                    observed "$root" {_ATTEMPT}
                forward+=("$observed")
            done
            printf 'FORWARD=%s\\n' "${{forward[*]}}"
            ! _kubectl_current_segment ignored {_ATTEMPT} "${{forward[@]}}" 2>/dev/null
            """)
    )
    assert result.returncode == 0, result.stderr
    assert "SEGMENTS=COMPLETE SUSPENDED CURRENT=1" in result.stdout
    assert "FORWARD=ACTIVE ACTIVE" in result.stdout
    assert _events(tmp_path)[:2] == ["observe ns-b", "observe ns-a"]


# The coordinator NetworkPolicy of FAIL_POLICY_NAMESPACE is refused. Deleted
# Pods are remembered so that a PVC exec through one of them fails.
_POLICY_REFUSED = """
    kubectl_create_owned_object() {
        if [[ "$2/$4" == "NetworkPolicy/$FAIL_POLICY_NAMESPACE" && "$3" == *-coord-net ]]; then
            event "create-denied $2 $4/$3"
            return 1
        fi
        printf '%s' "$7" > "$manifests/$2.$4.$3.yaml"
        event "create $2 $4/$3 ${10:-}"
        printf -v "$1" 'uid-%s' "$3"
    }
    kubectl_delete_owned_object() {
        event "delete $1 $3/$2"
        printf '%s\\n' "$2" >> "$fake/deleted"
    }
    kubectl_pvc_exec() {
        if grep -Fqx -- "$2" "$fake/deleted" 2>/dev/null; then
            event "exec-on-deleted-pod $1 $2"
            return 1
        fi
        event "exec $1 $2"
    }
"""


@pytest.mark.parametrize("segments", [1, 2])
def test_policy_create_failure_rolls_back_through_transfer_helpers(tmp_path, segments):
    """[S-24] A refused policy leaves the helpers that release every run tree."""
    namespace = "ns-a" if segments == 1 else "ns-b"
    results = tmp_path / "results"
    results.mkdir()
    plan = tmp_path / "plan"
    _write_plan(plan, segments)
    result = _bash(
        _fake_cluster(tmp_path)
        + f"results={str(results)!r}\nroot=$results/kubernetes\n"
        + f"FAIL_POLICY_NAMESPACE={namespace}\n"
        + _POLICY_REFUSED
        + _prepare_body(plan)
    )
    assert result.returncode != 0
    events = _events(tmp_path)
    assert f"create-denied NetworkPolicy {namespace}/" in "\n".join(events)
    assert not any(event.startswith("exec-on-deleted-pod") for event in events)
    state = results / "kubernetes"
    steps = _steps(state)
    expected = {"release-remote-run", "release-pvc-lease"}
    if segments == 2:
        expected |= {"release-remote-run-s1", "release-pvc-lease-c1"}
    assert expected <= steps
    for segment, upload_namespace in enumerate(("ns-a", "ns-b")[:segments]):
        helper = _upload(target=segment)
        assert events.index(f"exec {upload_namespace} {helper}") < events.index(
            f"delete Pod {upload_namespace}/{helper}"
        )
    assert _local_state(state) == "SUBMISSION_FAILED"


_SEQUENCER_OBJECTS = {
    "sequencer-sa": ("ServiceAccount", _SEQUENCER, "ns-a"),
    "sequencer-role-s0": ("Role", _SEQUENCER, "ns-a"),
    "sequencer-binding-s0": ("RoleBinding", _SEQUENCER, "ns-a"),
    "sequencer-role-s1": ("Role", f"sst-elb-{_ATTEMPT}-s1-sequencer", "ns-b"),
    "sequencer-binding-s1": ("RoleBinding", f"sst-elb-{_ATTEMPT}-s1-sequencer", "ns-b"),
    "sequencer": ("Job", _SEQUENCER, "ns-a"),
}

# The API accepted the create whose response was lost: every deterministic
# name a creation intent records exists with this attempt's identity.
_INTENT_OBJECT_EXISTS = """
    kubectl_run_observational() { event "get $*"; printf 'uid-intent'; }
    kubectl_verify_object_identity() { printf 'uid-intent\\n'; }
"""


def _crashed_sequencer_creation(state: Path, key: str) -> str:
    """A PREPARED attempt killed while creating KEY: KEY has only its intent."""
    kind, name, namespace = _SEQUENCER_OBJECTS[key]
    order = " ".join(_SEQUENCER_OBJECTS)
    return _two_target_attempt(state, "") + textwrap.dedent(f"""\
        found=0
        for key in {order}; do
            [[ "$key" != {key} ]] || found=1
            [[ "$found" -eq 0 ]] || rm -f -- "$metadata/resources/$key.sh"
        done
        kubectl_local_lock_acquire "$root" fd
        kubectl_attempt_write_creation_intent "$root" "$fd" {_ATTEMPT} {key} \\
            {kind} {name} {namespace} {_NONCE}
        kubectl_local_lock_release "$fd"
        """)


@pytest.mark.parametrize("key", list(_SEQUENCER_OBJECTS))
@pytest.mark.parametrize("operation", ["submit", "status", "collect"])
def test_sequencer_creation_intent_is_reconciled_by_rollback_and_collect(
    tmp_path, key, operation
):
    """[S-25] Only a creation intent for a sequencer object: its object is deleted.

    submit: the submitter's own rollback after the create's response was lost.
    status and collect: a later command after the submitter was killed.
    """
    kind, name, namespace = _SEQUENCER_OBJECTS[key]
    if operation == "submit":
        results = tmp_path / "filesystem-batch-20260101Z000000"
        state = results / "kubernetes"
        results.mkdir()
        _write_batch(results, ("ns-a", "ns-b"))
        body = (
            _fake_cluster(tmp_path)
            + f"results={str(results)!r}\nroot=$results/kubernetes\n"
            + f"source {str(results / 'env_used.sh')!r}\n"
            + _INTENT_OBJECT_EXISTS
            + textwrap.dedent(f"""\
                eval "original_$(declare -f kubectl_create_owned_object)"
                kubectl_create_owned_object() {{
                    if [[ "${{10:-}}" == {key} ]]; then
                        kubectl_attempt_write_creation_intent "$8" "$9" "$6" "${{10}}" \\
                            "$2" "$3" "$4" "$5" || return 1
                        event "create-lost $2 $4/$3"
                        return 1
                    fi
                    original_kubectl_create_owned_object "$@"
                }}
                """)
            + _SUBMIT
            + 'kubectl_submit_sweep "$results" 1\n'
        )
    else:
        state = tmp_path / "results" / "kubernetes"
        state.mkdir(parents=True)
        body = (
            _fake_cluster(tmp_path)
            + _crashed_sequencer_creation(state, key)
            + _INTENT_OBJECT_EXISTS
            + ': > "$events"\n'
            + f'kubectl_lifecycle_operation {operation} "$results"\n'
        )
    result = _bash(body)
    assert (result.returncode == 0) is (operation == "status"), result.stderr
    if operation != "submit":
        assert "STATE=SUBMISSION_FAILED" in result.stdout
    events = _events(tmp_path)
    assert f"delete {kind} {namespace}/{name}" in events
    metadata = state / "attempts" / _ATTEMPT
    assert not (metadata / "creation-intents" / f"{key}.sh").exists()
    assert {"release-pvc-lease", "release-pvc-lease-c1"} <= _steps(state)
    assert _local_state(state) == "SUBMISSION_FAILED"


def test_with_segment_restores_selection_after_failure_and_identity_reload(tmp_path):
    """kubectl_with_segment keeps its selection across reloads and restores after failure."""
    state = tmp_path / "results" / "kubernetes"
    state.mkdir(parents=True)
    result = _bash(_two_target_attempt(state) + textwrap.dedent(f"""\
            set -e
            kubectl_attempt_load_identity "$root/attempts/{_ATTEMPT}"
            kubectl_use_segment 1
            reload_and_fail() {{
                kubectl_attempt_load_identity "$root/attempts/{_ATTEMPT}" || return 9
                [[ "$KUBECTL_NAMESPACE/$KUBECTL_PVC" == ns-a/pvc-a \\
                    && "$KUBECTL_ACTIVE_SEGMENT" == 0 \\
                    && "$KUBECTL_CONTROL_LOGICAL_ROOT" == {_ROOT_A} ]] || return 8
                return 7
            }}
            rc=0
            kubectl_with_segment 0 reload_and_fail || rc=$?
            [[ "$rc" == 7 ]]
            [[ "$KUBECTL_ACTIVE_SEGMENT" == 1 && "$KUBECTL_NAMESPACE" == ns-b ]]
            [[ "$KUBECTL_PVC" == pvc-b && "$KUBECTL_PV_UID" == pvuid-b ]]
            [[ "$KUBECTL_CONTROL_LOGICAL_ROOT" == {_ROOT_B} ]]
            [[ "$KUBECTL_CONTROL_TEST_ROOT" == /mnt/storage-scale-test/pvc-b/benchmark ]]
            [[ "$(_kubectl_selected_segment)" == 1 ]]
            # Outside any selection a reload describes segment 0 again.
            kubectl_attempt_load_identity "$root/attempts/{_ATTEMPT}"
            [[ "$KUBECTL_ACTIVE_SEGMENT" == 0 && "$KUBECTL_NAMESPACE" == ns-a ]]
            """))
    assert result.returncode == 0, result.stderr


def test_claims_in_one_namespace_are_one_segment_that_mounts_every_claim(tmp_path):
    """[S-26] Two claims in one namespace: one segment, its Pods mount both claims.

    Each claim keeps its own Lease; the segment's workers, policies and
    selectors carry segment 0, and every claim is mounted at its own path.
    """
    state = tmp_path / "results" / "kubernetes"
    metadata = state / "attempts" / _ATTEMPT
    result = _bash(_fake_cluster(tmp_path) + textwrap.dedent(f"""\
            set -e
            root={str(state)!r}
            mkdir -p "$root"
            kubectl_local_lock_acquire "$root" fd
            kubectl_attempt_create_identity "$root" "$fd" {_ATTEMPT} {_NONCE} \\
                ns-a nsuid-a pv-b pvuid-b pvc-b pvcuid-b \\
                ns-a nsuid-a pv-a pvuid-a pvc-a pvcuid-a
            kubectl_attempt_load_identity "$root/attempts/{_ATTEMPT}"
            printf 'SEGMENTS=%s CLAIMS=%s\\n' "$KUBECTL_SEGMENT_COUNT" "$KUBECTL_CLAIM_COUNT"
            printf 'LEASES=%s %s\\n' "$(kubectl_claim_lease_key 0)" "$(kubectl_claim_lease_key 1)"
            printf 'ORDER=%s\\n' "${{KUBECTL_CLAIM_PVCS[*]}}"
            printf 'node-a\\tuid-a\\tamd64\\n' > {str(metadata)!r}/nodes.tsv
            printf 'WORKERS=%s\\n' "$(kubectl_worker_pod_selector {_ATTEMPT})"
            printf 'PODS=%s\\n' "$(kubectl_attempt_pod_selector {_ATTEMPT})"
            kubectl_create_attempt_policies "$root" "$fd" ns-a {_ATTEMPT} {_NONCE}
            kubectl_create_worker_daemonset "$root" "$fd" ns-a {_ATTEMPT} {_NONCE} \\
                {str(metadata)!r}/nodes.tsv
            printf 'SEQUENCER=%s\\n' "$(kubectl_attempt_pod_selector {_ATTEMPT} -)"
            kubectl_local_lock_release "$fd"
            """))
    assert result.returncode == 0, result.stderr
    keys = _keys(result.stdout)
    assert result.stdout.splitlines()[0] == "SEGMENTS=1 CLAIMS=2"
    assert keys["LEASES"] == "pvc-lease pvc-lease-c1"
    assert keys["ORDER"] == "pvc-a pvc-b"
    run = f"storage-scale-test.nvidia.com/run={_ATTEMPT}"
    segment_label = "storage-scale-test.nvidia.com/segment"
    assert keys["PODS"] == f"{run},{segment_label}=0"
    assert keys["WORKERS"].endswith(f",{segment_label}=0")
    assert keys["SEQUENCER"] == run
    manifests = tmp_path / "manifests"
    names = sorted(path.name for path in manifests.glob("*.ns-a.*.yaml"))
    assert len(names) == 3
    daemonset = yaml.safe_load(
        (manifests / f"DaemonSet.ns-a.sst-elb-{_ATTEMPT}-workers.yaml").read_text(
            encoding="utf-8"
        )
    )
    spec = daemonset["spec"]["template"]["spec"]
    assert daemonset["spec"]["selector"]["matchLabels"][segment_label] == "0"
    mounts = spec["containers"][0]["volumeMounts"]
    assert [mount["mountPath"] for mount in mounts] == [
        "/mnt/storage-scale-test/pvc-a",
        "/mnt/storage-scale-test/pvc-b",
    ]
    claims = {
        volume["name"]: volume["persistentVolumeClaim"]["claimName"]
        for volume in spec["volumes"]
    }
    assert {mount["name"]: claims[mount["name"]] for mount in mounts} == {
        mounts[0]["name"]: "pvc-a",
        mounts[1]["name"]: "pvc-b",
    }
    for policy in ("worker-net", "coord-net"):
        document = yaml.safe_load(
            (
                manifests / f"NetworkPolicy.ns-a.sst-elb-{_ATTEMPT}-{policy}.yaml"
            ).read_text(encoding="utf-8")
        )
        assert document["spec"]["podSelector"]["matchLabels"][segment_label] == "0"


def _collected_three_segment_attempt(state: Path) -> str:
    """Attempt 1: segment ns-a succeeded, ns-b failed, ns-c never started."""
    return (
        textwrap.dedent(f"""\
        root={str(state)!r}
        mkdir -p "$root"
        kubectl_local_lock_acquire "$root" fd
        kubectl_attempt_create_identity "$root" "$fd" {_ATTEMPT} {_NONCE} \\
            ns-a nsuid-a pv-a pvuid-a pvc-a pvcuid-a \\
            ns-b nsuid-b pv-b pvuid-b pvc-b pvcuid-b \\
            ns-c nsuid-c pv-c pvuid-c pvc-c pvcuid-c
        kubectl_attempt_write_state "$root" "$fd" {_ATTEMPT} PREPARED
        for next in SUBMITTED TERMINAL COLLECTION_IN_PROGRESS COLLECTED; do
            kubectl_attempt_transition "$root" "$fd" {_ATTEMPT} "$next"
        done
        kubectl_attempt_write_current "$root" "$fd" {_ATTEMPT}
        kubectl_local_lock_release "$fd"
        metadata="$root/attempts/{_ATTEMPT}"
        """)
        + _configuration(_ROOT_A, _ROOT_B, _ROOT_C)
        + textwrap.dedent("""\
        for target in 0 1 2; do
            suffix=$(kubectl_segment_suffix "$target")
            mkdir -p "$metadata/control-bundle$suffix/executions"
            printf 'export nodes=1\\n' \\
                > "$metadata/control-bundle$suffix/executions/000$((target + 1)).sh"
        done
        for pair in ':0001:SUCCESS' '-s1:0002:FAILED'; do
            IFS=: read -r suffix id outcome <<< "$pair"
            mkdir -p "$metadata/collected-state$suffix/executions"
            printf '%s\\n' "$outcome" > "$metadata/collected-state$suffix/run.status"
            printf '%s\\n' "$outcome" \\
                > "$metadata/collected-state$suffix/executions/$id.status"
            : > "$metadata/collected-state$suffix/publication-manifest.tsv"
        done
        """)
    )


def test_resume_plans_segments_from_the_namespaces_that_still_have_cells(tmp_path):
    """[R-25] Resume after ns-a finished: the new attempt's segments are ns-b, ns-c."""
    results = tmp_path / "filesystem-batch-20260101Z000000"
    results.mkdir()
    _write_batch(results, ("ns-a", "ns-b", "ns-c"))
    state = results / "kubernetes"
    body = (
        _fake_cluster(tmp_path)
        + _collected_three_segment_attempt(state)
        + f"results={str(results)!r}\n"
        + _SUBMIT
        + textwrap.dedent(f"""\
            kubectl_generate_attempt_id() {{ printf '{_RESUMED}\\n'; }}
            _elbencho_batch_print_group_targets() {{ :; }}
            kubectl_populate_sweep_control_bundle() {{
                event "populate ${{1##*/}} $(paste -sd, "$5") ${{6//$'\\t'/|}}"
                mkdir -p "$1/executions"
                local id
                while IFS= read -r id; do
                    cp -- "$2/executions/$id.sh" "$1/executions/$id.sh"
                done < "$5"
            }}
            : > "$events"
            kubectl_resume_collected_sweep "$results"
            printf 'RESUME_EXIT=%s\\n' "$?"
            set_fake job ns-b ACTIVE; set_fake job ns-c SUSPENDED
            set_fake remote ns-b RUNNING; set_fake progress ns-b '0 1 0 0'
            kubectl_lifecycle_operation status "$results"
            """)
    )
    result = _bash(body)
    assert result.returncode == 0, result.stderr
    assert "RESUME_EXIT=0" in result.stdout
    assert f"STORAGE_SCALE_TEST_ATTEMPT_ID={_RESUMED}" in result.stdout
    events = _events(tmp_path)
    assert [event for event in events if event.startswith("identity")] == [
        "identity ns-b pvc-b",
        "identity ns-c pvc-c",
    ]
    assert "populate control-bundle 0002 0|ns-b" in events
    assert "populate control-bundle-s1 0003 1|ns-c" in events
    assert f"create Job ns-b/sst-elb-{_RESUMED}-sweep sweep" in events
    assert f"create Job ns-c/sst-elb-{_RESUMED}-s1-sweep sweep-s1" in events
    configuration = (state / "attempts" / _RESUMED / "configuration.sh").read_text(
        encoding="utf-8"
    )
    assert "LIST_INDEXES" not in configuration
    assert f"KUBECTL_SEGMENT_CONTROL_ROOTS=( {_ROOT_B} {_ROOT_C} )" in configuration
    keys = _keys(result.stdout)
    assert (keys["SEGMENT_COUNT"], keys["SEGMENT_CURRENT"]) == ("2", "0")
    assert (keys["SEGMENT_0_NAMESPACE"], keys["SEGMENT_1_NAMESPACE"]) == (
        "ns-b",
        "ns-c",
    )
    assert (keys["SEGMENT_0_STATE"], keys["SEGMENT_1_STATE"]) == (
        "RUNNING",
        "NOT_STARTED",
    )
    assert "SEGMENT_2_STATE" not in keys
    assert keys["EXECUTIONS_TOTAL"] == "2"


def test_collect_publishes_namespace_named_ledgers_and_the_aggregate(tmp_path):
    """[R-25] Segment ledgers carry their namespace; run.status is the attempt outcome."""
    result, state = _collect(tmp_path, repeat=2)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("COLLECT_EXIT=0") == 2
    results = state.parent
    assert (results / "run-ns-a.status").read_text() == "SUCCESS\n"
    assert (results / "run-ns-b.status").read_text() == "SUCCESS\n"
    assert (results / "run-summary-ns-a.tsv").read_text() == "segment\t0\n"
    assert (results / "run-summary-ns-b.tsv").read_text() == "segment\t1\n"
    assert (results / "run.status").read_text() == "SUCCESS\n"
    summary = (results / "run-summary.tsv").read_text()
    assert "segments\t2\n" in summary
    assert "segment\t0\tns-a\tSUCCESS\n" in summary
    assert "segment\t1\tns-b\tSUCCESS\n" in summary
    assert "reason\t" not in summary
    record = (state / "attempts" / _ATTEMPT / "results-aggregate.tsv").read_text()
    assert [line.split("\t")[0] for line in record.splitlines()] == [
        "run.status",
        "run-summary.tsv",
    ]
    keys = _status_after(tmp_path, state)
    assert (
        keys["SEGMENT_CURRENT"],
        keys["SEGMENT_0_STATE"],
        keys["SEGMENT_1_STATE"],
    ) == (
        "1",
        "SUCCESS",
        "SUCCESS",
    )


@pytest.mark.parametrize(
    ("stopped_by_drift", "outcome"), [(False, "CANCELLED"), (True, "FAILED")]
)
def test_aggregate_outcome_of_a_cancelled_attempt_with_an_abandoned_segment(
    tmp_path, stopped_by_drift, outcome
):
    """[R-25] Segment 0 succeeded, segment 1 never started.

    A user's cancellation leaves the attempt CANCELLED; worker drift handling
    (journaled stopped-by-drift) leaves it FAILED, with a worker-drift reason.
    """
    drift = (
        'kubectl_local_lock_acquire "$root" fd\n'
        f'kubectl_attempt_journal_step "$root" "$fd" {_ATTEMPT} stopped-by-drift\n'
        'kubectl_local_lock_release "$fd"\n'
        if stopped_by_drift
        else ""
    )
    result = _bash(
        _fake_cluster(tmp_path)
        + _two_target_attempt(tmp_path / "results" / "kubernetes", "SUBMITTED TERMINAL")
        + drift
        + textwrap.dedent(f"""\
            set -e
            rm -f "$metadata/control-bundle/executions/0003.sh"
            mkdir -p "$metadata/collected-state/executions"
            printf 'SUCCESS\\n' > "$metadata/collected-state/run.status"
            _kubectl_validate_collected_publication() {{ printf -v "$3" SUCCESS; }}
            kubectl_read_collected_execution_progress() {{ printf '0 0 1 0\\n'; }}
            kubectl_attempt_load_identity "$root/attempts/{_ATTEMPT}"
            _kubectl_publish_results_aggregate "$results" "$root" {_ATTEMPT}
            """)
    )
    assert result.returncode == 0, result.stderr
    results = tmp_path / "results"
    assert (results / "run.status").read_text() == f"{outcome}\n"
    summary = (results / "run-summary.tsv").read_text()
    assert "segment\t0\tns-a\tSUCCESS\n" in summary
    assert "segment\t1\tns-b\tNOT_STARTED\n" in summary
    assert "pending\t2\n" in summary
    assert ("reason\tworker-drift\n" in summary) is stopped_by_drift
