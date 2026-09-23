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

"""Shell contracts for dormant kubectl lifecycle foundations."""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_FOUNDATIONS = (
    _REPOSITORY_ROOT
    / "storage-tests"
    / "fs"
    / "kubectl"
    / "_nv-elbencho-kubectl-functions.sh"
)
_BASH = shutil.which("bash") or "/bin/bash"


def _run_bash(body):
    return subprocess.run(
        [
            _BASH,
            "-c",
            textwrap.dedent(f"""
            source {_FOUNDATIONS!s}
            {body}
        """),
        ],
        check=False,
        cwd=_REPOSITORY_ROOT,
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    ("logical", "expected"),
    (
        ("/bench/fs1", "/mnt/storage-scale-test/bench/fs1"),
        ("bench//fs2/", "/mnt/storage-scale-test/bench/fs2"),
    ),
)
def test_logical_paths_map_under_the_fixed_mount(logical, expected):
    """Users never supply the container mount prefix."""
    result = _run_bash(f"kubectl_map_logical_path {logical!r}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "logical",
    ("", "/", ".", "a/../b", ".storage-scale-test", "/.storage-scale-test/run"),
)
def test_logical_paths_reject_escape_and_reserved_state(logical):
    """Mapped benchmark data cannot escape or overlap orchestration state."""
    result = _run_bash(f"kubectl_map_logical_path {logical!r}")
    assert result.returncode != 0


def test_test_dir_weights_and_read_from_suffix_are_preserved():
    """Mapping changes only path identity, not workload weighting."""
    result = _run_bash("""
        declare -A TEST_DIRS=([/bench/primary]=2 [bench/secondary]=1)
        declare -A mapped=()
        kubectl_map_test_dirs mapped
        [[ "${mapped[/mnt/storage-scale-test/bench/primary]}" == 2 ]]
        [[ "${mapped[/mnt/storage-scale-test/bench/secondary]}" == 1 ]]
        [[ $(kubectl_map_read_from_path /bench/primary/dataset) \
            == /mnt/storage-scale-test/bench/primary/dataset ]]
    """)
    assert result.returncode == 0, result.stderr


def test_nested_roots_make_read_from_mapping_ambiguous():
    """A read path must be owned by exactly one logical root."""
    result = _run_bash("""
        declare -A TEST_DIRS=([bench]=1 [bench/nested]=1)
        kubectl_map_read_from_path bench/nested/data
    """)
    assert result.returncode != 0
    assert "exactly one" in result.stderr


@pytest.mark.parametrize(
    "selector",
    (
        "storage-test=true,role=client",
        "example.com/target=worker-1",
        "role=",
    ),
)
def test_equality_node_selectors_are_accepted(selector):
    result = _run_bash(f"kubectl_validate_node_selector {selector!r}")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "selector",
    (
        "",
        "role",
        "role in (worker)",
        "role=a,role=b",
        "role=a=b",
        "UPPER.example/key=value",
        "example.com/a/b=value",
    ),
)
def test_non_equality_or_ambiguous_selectors_are_rejected(selector):
    result = _run_bash(f"kubectl_validate_node_selector {selector!r}")
    assert result.returncode != 0


def test_attempt_identity_and_stable_output_contracts():
    """Names are short while the independent ownership nonce is strong."""
    result = _run_bash("""
        attempt=$(kubectl_generate_attempt_id)
        nonce=$(kubectl_generate_ownership_nonce)
        [[ "$attempt" =~ ^[0-9a-f]{8}$ ]]
        [[ "$nonce" =~ ^[0-9a-f]{32}$ ]]
        kubectl_emit_submission_identity /tmp/results "$attempt"
        kubectl_emit_state RUNNING
        ! kubectl_emit_state UNKNOWN
    """)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "STORAGE_SCALE_TEST_RESULTS_DIR=/tmp/results"
    assert lines[1].startswith("STORAGE_SCALE_TEST_ATTEMPT_ID=")
    assert lines[2] == "STORAGE_SCALE_TEST_KUBECTL_STATE=RUNNING"


def test_attempt_metadata_and_current_pointer_are_atomic_and_versioned(tmp_path):
    """Identity is create-only while state and current remain mutable."""
    result = _run_bash(f"""
        root={str(tmp_path / 'kubernetes')!r}
        kubectl_local_lock_acquire "$root" lock_fd
        kubectl_attempt_create_identity "$root" "$lock_fd" 1234abcd \
            0123456789abcdef0123456789abcdef test-ns ns-uid \
            test-pv pv-uid test-pvc pvc-uid
        identity="$root/attempts/1234abcd/identity.sh"
        identity_digest=$(sha256sum "$identity")
        ! kubectl_attempt_create_identity "$root" "$lock_fd" 1234abcd \
            fedcba9876543210fedcba9876543210 other-ns other-uid \
            other-pv other-pv-uid other-pvc other-pvc-uid
        [[ $(sha256sum "$identity") == "$identity_digest" ]]
        kubectl_attempt_write_state "$root" "$lock_fd" 1234abcd PREPARED
        kubectl_attempt_write_current "$root" "$lock_fd" 1234abcd
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_ATTEMPT_ID" == 1234abcd ]]
        [[ "$KUBECTL_NAMESPACE_UID" == ns-uid ]]
        [[ $(cat "$root/current-attempt") == 1234abcd ]]
        ! kubectl_attempt_write_state "$root" 999 1234abcd SUBMITTED
        ! kubectl_attempt_write_current "$root" 999 1234abcd
        kubectl_attempt_write_state "$root" "$lock_fd" 1234abcd SUBMITTED
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED ]]
        kubectl_local_lock_release "$lock_fd"
        sed -i.bak 's/KUBECTL_ATTEMPT_SCHEMA=1/KUBECTL_ATTEMPT_SCHEMA=99/' \
            "$identity"
        ! kubectl_attempt_load_metadata "$root/attempts/1234abcd"
    """)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("function", "accepted", "rejected"),
    (
        ("kubectl_validate_object_name", "a" * 63 + ".b", "a" * 64 + ".b"),
        ("kubectl_validate_namespace_name", "a" * 63, "a" * 64),
        ("kubectl_validate_label_name", "A" * 63, "A" * 64),
        ("kubectl_validate_label_value", "v" * 63, "v" * 64),
        ("kubectl_validate_uid", "uid:ABC_1", "uid/unsafe"),
    ),
)
def test_kubernetes_field_validators_enforce_distinct_grammars(
    function, accepted, rejected
):
    result = _run_bash(f"""
        {function} {accepted!r}
        ! {function} {rejected!r}
    """)
    assert result.returncode == 0, result.stderr


def test_dns_prefix_and_object_limits_match_kubernetes_boundaries():
    prefix_253 = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * 61))
    prefix_254 = prefix_253 + "d"
    result = _run_bash(f"""
        kubectl_validate_object_name {prefix_253!r}
        ! kubectl_validate_object_name {prefix_254!r}
        kubectl_validate_label_key {f'{prefix_253}/target'!r}
        ! kubectl_validate_label_key {f'{prefix_254}/target'!r}
    """)
    assert result.returncode == 0, result.stderr


def test_template_renderer_uses_allowlisted_yaml_safe_fields():
    """Replacement values cannot alter YAML structure."""
    result = _run_bash("""
        rendered=$(kubectl_render_template \
            'name: @@RESOURCE_NAME@@' RESOURCE_NAME=attempt-one)
        [[ "$rendered" == 'name: attempt-one' ]]
        ! kubectl_render_template 'name: @@RESOURCE_NAME@@' OTHER=value
        ! kubectl_render_template 'name: @@RESOURCE_NAME@@' RESOURCE_NAME
        ! kubectl_render_template 'name: @@RESOURCE_NAME@@' RESOURCE_NAME=a=b
        ! kubectl_render_template 'name: @@RESOURCE_NAME@@' \
            $'RESOURCE_NAME=ok\\nunsafe: yes'
        ! kubectl_render_template 'image: @@IMAGE@@' 'IMAGE=bad image: latest'
        ! kubectl_render_template 'name: @@UNRECOGNIZED@@' UNRECOGNIZED=value
    """)
    assert result.returncode == 0, result.stderr


def test_submission_output_preserves_spaces_and_rejects_line_injection():
    """Stable key/value output remains one record per line."""
    result = _run_bash("""
        kubectl_emit_submission_identity '/tmp/results with spaces' 1234abcd
        ! kubectl_emit_submission_identity $'/tmp/results\\nBAD=value' 1234abcd
        ! kubectl_emit_submission_identity /tmp/results invalid
    """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "STORAGE_SCALE_TEST_RESULTS_DIR=/tmp/results with spaces",
        "STORAGE_SCALE_TEST_ATTEMPT_ID=1234abcd",
    ]


def test_kubectl_commands_have_request_and_process_timeouts():
    """One wedged API call cannot exceed its caller's operation deadline."""
    result = _run_bash("""
        timeout() { printf '%s\n' "$*"; }
        KUBECTL_REQUEST_TIMEOUT_SECONDS=7
        KUBECTL_PROCESS_TIMEOUT_SECONDS=11
        kubectl_run_bounded get pods
    """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "--kill-after=5s 11s kubectl --request-timeout=7s get pods"
    )


def test_observational_calls_retry_only_transient_api_failures(tmp_path):
    """Safe observations retry throttling but fail fast on authorization."""
    calls = tmp_path / "calls"
    output = tmp_path / "result"
    result = _run_bash(f"""
        calls={str(calls)!r}
        kubectl_run_bounded() {{
            count=$(wc -l < "$calls" 2>/dev/null || printf 0)
            printf 'call\n' >> "$calls"
            if [[ "$1" == forbidden ]]; then
                printf 'Error from server (Forbidden)\n' >&2
                return 1
            fi
            if [[ "$count" -eq 0 ]]; then
                printf 'Error from server (TooManyRequests): 429\n' >&2
                return 1
            fi
            printf 'ready\n'
        }}
        KUBECTL_OBSERVATION_BACKOFF_SECONDS=0 \
            kubectl_run_observational get pods > {str(output)!r}
        [[ $(cat {str(output)!r}) == ready ]]
        [[ $(wc -l < "$calls") -eq 2 ]]
        : > "$calls"
        ! kubectl_run_observational forbidden
        [[ $(wc -l < "$calls") -eq 1 ]]
    """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=AUTH" in result.stderr


def test_exhausted_observation_emits_actionable_diagnostic_envelope():
    """Transient API exhaustion reports stable fields and preserves failure."""
    result = _run_bash("""
        kubectl_run_bounded() {
            printf 'Service Unavailable (503)\n' >&2
            return 1
        }
        export KUBECTL_OBSERVATION_ATTEMPTS=2
        export KUBECTL_OBSERVATION_BACKOFF_SECONDS=0
        export KUBECTL_ATTEMPT_ID=1234abcd KUBECTL_LIFECYCLE_STATE=SUBMITTED
        ! kubectl_run_observational -n test-ns get pods
    """)
    assert result.returncode == 0
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_OPERATION=kubectl-observe" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_ATTEMPT_ID=1234abcd" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_LOCAL_STATE=SUBMITTED" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=API_UNAVAILABLE" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_SAFE_NEXT_ACTION=" in result.stderr


def test_observation_does_not_turn_local_output_failure_into_absence():
    """A successful API call is not success when its response cannot be read."""
    result = _run_bash("""
        kubectl_run_bounded() { printf 'owned-object\n'; }
        cat() {
            [[ "$2" != */stdout ]] || return 1
            command cat "$@"
        }
        output=sentinel
        ! output=$(kubectl_run_observational get Pod helper --ignore-not-found)
        [[ -z "$output" ]]
    """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=LOCAL_IO" in result.stderr


def test_helper_readiness_timeout_captures_diagnostics_without_masking_failure():
    """An unready helper reports classified evidence and still returns failure."""
    result = _run_bash("""
        kubectl_verify_object_identity() { return 1; }
        kubectl_capture_resource_diagnostics() { printf /tmp/helper-diagnostics; }
        kubectl_classify_readiness_failure() { printf -v "$1" IMAGE_PULL; }
        ! kubectl_wait_owned_ready_pod test-ns helper-a \
            0123456789abcdef0123456789abcdef 1234abcd 1 /tmp/attempt
    """)
    assert result.returncode == 0
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=IMAGE_PULL" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_KIND=Pod" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_PATH=/tmp/helper-diagnostics" in result.stderr


def test_worker_readiness_timeout_captures_daemonset_diagnostics(tmp_path):
    """DaemonSet convergence failure names the exact owned worker resource."""
    nodes = tmp_path / "nodes.tsv"
    nodes.write_text("node-a\tuid-a\tamd64\n", encoding="utf-8")
    result = _run_bash(f"""
        kubectl_discover_worker_endpoints() {{ return 1; }}
        kubectl_capture_resource_diagnostics() {{ printf /tmp/worker-diagnostics; }}
        kubectl_classify_readiness_failure() {{ printf -v "$1" POD_UNSCHEDULABLE; }}
        ! kubectl_wait_worker_endpoints test-ns 1234abcd {str(nodes)!r} \
            {str(tmp_path / 'workers.tsv')!r} 1 {str(tmp_path)!r}
    """)
    assert result.returncode == 0
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=POD_UNSCHEDULABLE" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_KIND=DaemonSet" in result.stderr
    assert (
        "STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_NAME=sst-elb-1234abcd-workers"
        in result.stderr
    )


def test_resource_diagnostics_are_bounded_and_best_effort(tmp_path):
    """Diagnostic failure never hides the path or queries unrelated events."""
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    calls = tmp_path / "calls"
    result = _run_bash(f"""
        calls={str(calls)!r}
        kubectl_run_bounded() {{
            printf '%s\n' "$*" >> "$calls"
            if [[ "$*" == *'-o name'* ]]; then
                printf 'pod/worker-a\n'
            elif [[ "$*" == *' logs '* ]]; then
                printf 'diagnostic log failed\n' >&2
                return 1
            else
                printf 'evidence\n'
            fi
        }}
        path=$(kubectl_capture_resource_diagnostics {str(attempt)!r} \
            workers-not-ready test-ns DaemonSet sst-elb-1234abcd-workers \
            1234abcd)
        [[ -d "$path" ]]
        [[ -f "$path/resource.yaml" && -f "$path/resource.describe" ]]
        [[ -f "$path/pods.yaml" && -f "$path/pods.log" ]]
        [[ -f "$path/events.txt" ]]
        grep -F -- '--field-selector involvedObject.name=worker-a' "$calls"
        ! grep -F -- 'get events --sort-by' "$calls"
    """)
    assert result.returncode == 0, result.stderr


def test_resource_diagnostics_truncate_large_outputs(tmp_path):
    """Supplemental evidence has a fixed local-space bound."""
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    result = _run_bash(f"""
        kubectl_run_bounded() {{ head -c 2097152 /dev/zero; }}
        path=$(kubectl_capture_resource_diagnostics {str(attempt)!r} \
            collect-failed test-ns Job sst-elb-1234abcd-sweep 1234abcd)
        [[ $(wc -c < "$path/resource.yaml") -le 524288 ]]
        [[ $(wc -c < "$path/resource.describe") -le 524288 ]]
        [[ $(wc -c < "$path/pods.yaml") -le 524288 ]]
        [[ $(wc -c < "$path/pods.log") -le 1048576 ]]
    """)
    assert result.returncode == 0, result.stderr


def test_remote_pvc_enospc_has_distinct_diagnostics(tmp_path):
    """Remote storage exhaustion is not confused with local collection space."""
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    diagnostic = tmp_path / "diagnostic"
    diagnostic.mkdir()
    (diagnostic / "pods.log").write_text(
        "write failed: No space left on device\n", encoding="utf-8"
    )
    result = _run_bash(f"""
        export KUBECTL_PVC=test-pvc
        kubectl_capture_pvc_diagnostics() {{ printf {str(diagnostic)!r}; }}
        kubectl_preserve_storage_failure_diagnostics {str(attempt)!r} test-ns \
            helper 1234abcd Job sst-elb-1234abcd-sweep job-uid
    """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=ENOSPC" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_PHASE=pvc-evidence" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_PATH=" in result.stderr


def test_pvc_diagnostics_capture_remote_capacity_and_claim(tmp_path):
    """Storage evidence includes PVC identity plus block and inode capacity."""
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    result = _run_bash(f"""
        export KUBECTL_PVC=test-pvc
        kubectl_capture_resource_diagnostics() {{
            mkdir -p {str(tmp_path / 'capture')!r}
            printf {str(tmp_path / 'capture')!r}
        }}
        kubectl_run_bounded() {{ printf 'pvc evidence\n'; }}
        kubectl_pvc_exec() {{ printf 'filesystem capacity\ninode capacity\n'; }}
        path=$(kubectl_capture_pvc_diagnostics {str(attempt)!r} test-ns helper \
            1234abcd Job sst-elb-1234abcd-sweep)
        grep -F 'pvc evidence' "$path/pvc.yaml"
        grep -F 'filesystem capacity' "$path/pvc-filesystem.txt"
        grep -F 'inode capacity' "$path/pvc-filesystem.txt"
    """)
    assert result.returncode == 0, result.stderr


def test_attempt_diagnostics_prefer_exact_journaled_workload(tmp_path):
    """Each rollback failure captures fresh evidence for the exact workload."""
    state = tmp_path / "state"
    result = _run_bash(f"""
        root={str(state)!r}
        kubectl_local_lock_acquire "$root" fd
        kubectl_attempt_create_identity "$root" "$fd" 1234abcd \
            0123456789abcdef0123456789abcdef test-ns ns-uid \
            test-pv pv-uid test-pvc pvc-uid
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd sweep \
            Job sst-elb-1234abcd-sweep test-ns job-uid \
            0123456789abcdef0123456789abcdef
        kubectl_capture_resource_diagnostics() {{
            printf '%s|%s|%s|%s|%s|%s\n' "$@" >> {str(tmp_path / 'capture')!r}
            mkdir -p "$1/diagnostics"
            path=$(mktemp -d "$1/diagnostics/exact.XXXXXXXX")
            printf '%s\n' "$path"
        }}
        first=$(kubectl_capture_attempt_diagnostics "$root" 1234abcd submit-failed)
        second=$(kubectl_capture_attempt_diagnostics "$root" 1234abcd submit-failed)
        [[ "$first" == "$root/attempts/1234abcd/diagnostics/exact."* ]]
        [[ "$second" == "$root/attempts/1234abcd/diagnostics/exact."* ]]
        [[ "$second" != "$first" ]]
        [[ $(wc -l < {str(tmp_path / 'capture')!r}) -eq 2 ]]
        grep -F -- \
          'submit-failed|test-ns|Job|sst-elb-1234abcd-sweep|1234abcd' \
          {str(tmp_path / 'capture')!r}
        kubectl_local_lock_release "$fd"
    """)
    assert result.returncode == 0, result.stderr


def test_create_only_verifies_exact_identity_before_returning_uid():
    """An ambiguous create response cannot adopt an object with another nonce."""
    result = _run_bash("""
        calls=$(mktemp)
        kubectl_run_bounded() {
            printf '%s\n' "$*" >> "$calls"
            if [[ " $* " == *' create '* ]]; then
                return 1
            fi
            printf 'Job\\tsst-elb-1234abcd-sweep\\tuid-1\\t%s\\t1234abcd' \
                0123456789abcdef0123456789abcdef
        }
        uid=
        kubectl_create_owned_object uid Job sst-elb-1234abcd-sweep test-ns \
            0123456789abcdef0123456789abcdef 1234abcd 'kind: Job'
        [[ "$uid" == uid-1 ]]
        grep -q -- 'create -f -' "$calls"
        ! grep -q -- 'apply' "$calls"
        kubectl_run_bounded() {
            printf 'Job\\tsst-elb-1234abcd-sweep\\tuid-2\\twrong\\t1234abcd'
        }
        ! kubectl_create_owned_object uid Job sst-elb-1234abcd-sweep test-ns \
            0123456789abcdef0123456789abcdef 1234abcd 'kind: Job'
    """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=IDENTITY_MISMATCH" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_KIND=Job" in result.stderr
