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

"""Shared fake cluster and helpers for the multi-segment Kubernetes tests.

The fake cluster replaces every cluster and PVC effect with function overrides
that log one line per effect; a test file may append its own overrides after
it, since later shell definitions win.
"""

from pathlib import Path
import hashlib
import re
import shutil
import subprocess
import textwrap

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
FUNCTIONS = (
    REPOSITORY_ROOT / "storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
)
BASH = shutil.which("bash") or "/bin/bash"
ATTEMPT = "1234abcd"
NONCE = "0123456789abcdef0123456789abcdef"
SEQUENCER_IMAGE = "registry.example/kubectl@sha256:" + "a" * 64


def _default_sequencer_image() -> str:
    """Return KUBECTL_DEFAULT_SEQUENCER_IMAGE as the lifecycle helpers define it."""
    match = re.search(
        r"^readonly KUBECTL_DEFAULT_SEQUENCER_IMAGE=(\S+)$",
        FUNCTIONS.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if match is None:
        raise AssertionError("KUBECTL_DEFAULT_SEQUENCER_IMAGE is not defined")
    return match.group(1)


DEFAULT_SEQUENCER_IMAGE = _default_sequencer_image()
HELPER = "0000aaaa"


def bash_with_layout(body: str, logical_root: str) -> subprocess.CompletedProcess[str]:
    """Run BODY after loading the lifecycle helpers and the control layout of
    LOGICAL_ROOT ("NAMESPACE/CLAIM:PATH")."""
    claim_path = logical_root.split("/", 1)[1].replace(":", "/", 1)
    script = "\n".join(
        (
            f"source {FUNCTIONS!s}",
            f"KUBECTL_CONTROL_LOGICAL_ROOT={logical_root}",
            f'KUBECTL_CONTROL_TEST_ROOT="$KUBECTL_SWEEP_MOUNT_ROOT/{claim_path}"',
            'KUBECTL_CONTROL_ROOT="$KUBECTL_CONTROL_TEST_ROOT/.storage-scale-test"',
            textwrap.dedent(body),
        )
    )
    return subprocess.run(
        [BASH, "-c", script],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def fake_cluster(tmp_path: Path) -> str:
    """Overrides that stand in for every cluster and PVC effect.

    Each effect appends one line to the events file. Per-namespace fake state
    lives in files under fake/: job-<ns> (SUSPENDED, ACTIVE, COMPLETE,
    FAILED), remote-<ns> (the PVC run.status), and progress-<ns>.
    """
    return textwrap.dedent(f"""\
        events={str(tmp_path / 'events')!r}
        manifests={str(tmp_path / 'manifests')!r}
        fake={str(tmp_path / 'fake')!r}
        mkdir -p "$manifests" "$fake"
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000
        export KUBECTL_RUN_AS_GROUP=2000 KUBECTL_SEQUENCER_IMAGE={SEQUENCER_IMAGE}
        event() {{ printf '%s\\n' "$*" >> "$events"; }}
        kubectl_generate_attempt_id() {{ printf '{ATTEMPT}\\n'; }}
        kubectl_generate_ownership_nonce() {{ printf '{NONCE}\\n'; }}
        _kubectl_random_hex() {{ printf '{HELPER}\\n'; }}
        elbencho_batch_verify_manifest() {{ :; }}
        kubectl_validate_cluster_identity() {{
            local namespace="$1" claim
            shift
            event "identity $namespace $*"
            for claim in "$@"; do
                printf '%s\\tnsuid-%s\\tpv-%s\\tpvuid-%s\\t%s\\tpvcuid-%s\\n' \\
                    "$namespace" "${{namespace#ns-}}" "${{claim#pvc-}}" \\
                    "${{claim#pvc-}}" "$claim" "${{claim#pvc-}}"
            done
        }}
        kubectl_discover_candidate_nodes() {{
            event nodes
            printf 'node-a\\tuid-a\\tamd64\\nnode-b\\tuid-b\\tamd64\\n' > "$2"
        }}
        kubectl_choose_coordinator_node() {{ printf 'node-a\\n'; }}
        kubectl_create_owned_object() {{
            if [[ ",${{FAIL_CREATE:-}}," == *",$2/$4,"* ]]; then
                event "create-denied $2 $4/$3"
                return 1
            fi
            printf '%s' "$7" > "$manifests/$2.$4.$3.yaml"
            event "create $2 $4/$3 ${{10:-}}"
            printf -v "$1" 'uid-%s' "$3"
        }}
        kubectl_delete_owned_object() {{
            if [[ "${{FAIL_DELETE_ONCE:-}}" == "$1/$3" && ! -e "$fake/delete-failed" ]]; then
                : > "$fake/delete-failed"
                event "delete-failed $1 $3/$2"
                return 1
            fi
            event "delete $1 $3/$2"
        }}
        kubectl_wait_owned_ready_pod() {{ :; }}
        kubectl_preserve_attempt_diagnostics() {{ event diagnostics; }}
        kubectl_validate_pvc_paths() {{ event "paths $1 $2 ${{*:3}}"; }}
        kubectl_validate_test_root_access() {{ event "access $1 $2 ${{*:3}}"; }}
        kubectl_acquire_pvc_lease() {{
            kubectl_attempt_load_identity "$1/attempts/$3" || return 1
            local key
            key=$(_kubectl_selected_claim_lease_key) || return 1
            if [[ "${{HELD_LEASE:-}}" == "$KUBECTL_PVC" ]]; then
                event "lease-held $KUBECTL_NAMESPACE/$KUBECTL_PVC"
                return 1
            fi
            kubectl_attempt_journal_resource "$1" "$2" "$3" "$key" Lease \\
                "$KUBECTL_PVC_LEASE_NAME" "$KUBECTL_NAMESPACE" "uid-$key" \\
                "$KUBECTL_OWNERSHIP_NONCE" || return 1
            event "lease $KUBECTL_NAMESPACE/$KUBECTL_PVC $key"
        }}
        kubectl_release_pvc_lease() {{
            local key
            key=$(_kubectl_selected_claim_lease_key) || return 1
            ! kubectl_attempt_resource_exists "$1" "$3" "$key" \\
                || event "lease-delete $key"
            kubectl_attempt_journal_step "$1" "$2" "$3" "release-$key"
        }}
        kubectl_observe_pvc_lease() {{
            local key
            key=$(_kubectl_selected_claim_lease_key) || return 1
            if [[ -f "$root/attempts/{ATTEMPT}/cleanup/release-$key" ]]; then
                printf -v "$1" ''
                return 0
            fi
            printf -v "$1" '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s' "uid-$key" \\
                "$KUBECTL_OWNERSHIP_NONCE" {ATTEMPT} "$KUBECTL_NAMESPACE_UID" \\
                "$KUBECTL_PV_UID" "$KUBECTL_PVC_UID" "{ATTEMPT}/$KUBECTL_OWNERSHIP_NONCE"
        }}
        kubectl_reserve_remote_attempt() {{
            event "reserve $1 $2 $KUBECTL_CONTROL_LOGICAL_ROOT"
            if [[ "${{ALIASED_NAMESPACE:-}}" == "$1" ]]; then
                echo "attempt run tree already exists: /mnt/storage-scale-test/x" >&2
                return 1
            fi
        }}
        kubectl_initialize_remote_control_tree() {{ event "control $1 $2"; }}
        kubectl_pvc_exec() {{
            if [[ "${{FAIL_EXEC_ONCE:-}}" == "$1" && ! -e "$fake/exec-failed" ]]; then
                : > "$fake/exec-failed"
                event "exec-failed $1 $2"
                return 1
            fi
            event "exec $1 $2"
        }}
        kubectl_wait_worker_endpoints() {{
            event "endpoints $1"
            [[ "${{UNREADY_NAMESPACE:-}}" != "$1" ]] || return 1
            printf 'node-a\\tuid-a\\tpod-%s\\tpuid-%s\\t10.0.0.1\\tamd64\\tsha256:i\\n' \\
                "$1" "$1" > "$4"
        }}
        kubectl_observe_segment_job() {{
            event "observe $3"
            printf -v "$1" '%s' "$(cat "$fake/job-$3")"
        }}
        kubectl_unsuspend_segment_job() {{
            event "unsuspend $2/$1 $3"
            printf ACTIVE > "$fake/job-$2"
        }}
        kubectl_job_terminal_state() {{
            case "$(cat "$fake/job-$3" 2>/dev/null)" in
                COMPLETE|FAILED) printf -v "$1" '%s' "$(cat "$fake/job-$3")" ;;
                MISSING|'') return 1 ;;
                *) return 2 ;;
            esac
        }}
        _kubectl_verify_saved_cluster_identity() {{ event "verify-identity $KUBECTL_NAMESPACE"; }}
        kubectl_verify_journaled_pvc_lease() {{
            event "verify-lease $KUBECTL_NAMESPACE/$KUBECTL_PVC"
        }}
        _kubectl_create_inspector() {{
            event "inspector $KUBECTL_NAMESPACE $3"
            printf -v "$1" 'inspector-%s' "$KUBECTL_NAMESPACE"
            printf -v "$2" 'uid-inspector-%s' "$KUBECTL_NAMESPACE"
        }}
        _kubectl_remove_inspector() {{ event "remove $3"; }}
        kubectl_read_remote_status() {{ cat "$fake/remote-$1"; }}
        kubectl_read_remote_execution_progress() {{ cat "$fake/progress-$1"; }}
        _kubectl_verify_saved_worker_endpoints() {{
            event "endpoints-check $KUBECTL_NAMESPACE"
            [[ "${{DRIFT_NAMESPACE:-}}" != "$KUBECTL_NAMESPACE" ]] || return 2
        }}
        kubectl_preserve_storage_failure_diagnostics() {{ event "storage-diagnostics $2"; }}
        kubectl_recover_lost_coordinator() {{
            event "recover $1"
            case "$(cat "$fake/remote-$1")" in
                PREPARED|RUNNING) printf '%s' "${{RECOVER_TO:-FAILED}}" > "$fake/remote-$1" ;;
            esac
        }}
        kubectl_finalize_cancelled_attempt() {{
            event "finalize-cancelled $1 $2"
            printf CANCELLED > "$fake/remote-$1"
        }}
        kubectl_wait_journaled_job_quiescent() {{ event "quiescent $(_kubectl_selected_segment)"; }}
        set_fake() {{ printf '%s' "$3" > "$fake/$1-$2"; }}
        """)


def events(tmp_path: Path) -> list[str]:
    path = tmp_path / "events"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def keys(output: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def steps(state: Path) -> set[str]:
    return {path.name for path in (state / "attempts" / ATTEMPT / "cleanup").iterdir()}


def local_state(state: Path) -> str:
    text = (state / "attempts" / ATTEMPT / "state.sh").read_text(encoding="utf-8")
    return text.strip().split("=", 1)[1]


def publication(
    root: Path,
    execution: str,
    summary: str,
    outcome: str = "SUCCESS",
    diagnostics: tuple[str, ...] = (),
    attempt: str = ATTEMPT,
) -> None:
    """A complete one-cell publication under root/<attempt>/state.

    A FAILED outcome fails its one cell with exit code 143. DIAGNOSTICS names
    attempt-level ledger files (coordinator-loss.tsv, startup-error.txt) whose
    content identifies the summary they belong to.
    """
    state = root / attempt / "state"
    files = {
        "run.status": ("ledger", "run.status", f"{outcome}\n"),
        "run-summary.tsv": ("ledger", "run-summary.tsv", summary),
        "env_used.sh": ("snapshot", "env_used.sh", "export TEST=1\n"),
        "env_used.yaml": ("snapshot", "env_used.yaml", "TEST: 1\n"),
        f"executions/{execution}.status": (
            "ledger",
            f"executions/{execution}.status",
            f"{outcome}\n",
        ),
        f"executions/{execution}.exitcode": (
            "ledger",
            f"executions/{execution}.exitcode",
            "0\n" if outcome == "SUCCESS" else "143\n",
        ),
        f"executions/{execution}.workers.tsv": (
            "ledger",
            f"executions/{execution}.workers.tsv",
            "node-a\tpod-a\tuid-a\t10.0.0.1\n",
        ),
        f"results/{execution}/result-{execution}.txt": (
            "result",
            f"result-{execution}.txt",
            f"result {execution}\n",
        ),
    }
    for name in diagnostics:
        files[name] = ("ledger", name, f"{name} of {summary}")
    rows = ["schema\t1", f"attempt_id\t{attempt}", f"execution\t{execution}\t{outcome}"]
    for relative, (role, destination, contents) in files.items():
        path = state / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        digest = hashlib.sha256(contents.encode()).hexdigest()
        rows.append(
            f"{role}\t{relative}\t{destination}\t{len(contents.encode())}\t{digest}"
        )
    (state / "publication-manifest.tsv").write_text("\n".join(rows) + "\n")
