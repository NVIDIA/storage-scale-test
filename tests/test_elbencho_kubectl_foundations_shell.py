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
import re
import subprocess
import textwrap

import pytest
import yaml

from tests.kubectl_segment_test_helpers import DEFAULT_SEQUENCER_IMAGE

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
            KUBECTL_NAMESPACE=test-ns
            KUBECTL_CONTROL_LOGICAL_ROOT=test-ns/test-pvc:benchmark
            KUBECTL_CONTROL_TEST_ROOT="$KUBECTL_SWEEP_MOUNT_ROOT/test-pvc/benchmark"
            KUBECTL_CONTROL_ROOT="$KUBECTL_CONTROL_TEST_ROOT/.storage-scale-test"
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
        ("claim-a:/bench/fs1", "/mnt/storage-scale-test/claim-a/bench/fs1"),
        ("claim-a:bench//fs2/", "/mnt/storage-scale-test/claim-a/bench/fs2"),
        ("other-ns/claim.b:x", "/mnt/storage-scale-test/claim.b/x"),
        ("test-ns/claim-a:x/y", "/mnt/storage-scale-test/claim-a/x/y"),
    ),
)
def test_logical_paths_map_under_the_fixed_mount(logical, expected):
    """Users never supply the container mount prefix; each claim has its own."""
    result = _run_bash(f"kubectl_map_logical_path {logical!r}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    ("key", "normalized"),
    (
        ("claim-a:bench", "test-ns/claim-a:bench"),
        ("claim-a:/bench//fs/", "test-ns/claim-a:bench/fs"),
        ("other-ns/claim.b:x", "other-ns/claim.b:x"),
        ("test-ns/claim-a:bench", "test-ns/claim-a:bench"),
    ),
)
def test_keys_normalize_to_namespace_claim_and_path(key, normalized):
    """Every spelling of a root normalizes to one "namespace/claim:path" form."""
    result = _run_bash(f"""
        set -e
        kubectl_normalize_logical_path {key!r} once
        [[ "$once" == {normalized!r} ]]
        kubectl_normalize_logical_path "$once" twice
        [[ "$twice" == "$once" ]]
        kubectl_split_test_dir_key {key!r} namespace claim path
        [[ "$namespace/$claim:$path" == {normalized!r} ]]
        # The namespace defaults only for keys that name none.
        KUBECTL_NAMESPACE=
        if [[ {key!r} != */*:* ]]; then
            ! kubectl_normalize_logical_path {key!r} unused
        fi
    """)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "logical",
    (
        "",
        "/bench/fs1",
        "claim-a:",
        "claim-a:/",
        "claim-a:.",
        "claim-a:a/../b",
        "claim-a:.storage-scale-test",
        "claim-a:/.storage-scale-test/run",
        "claim-a:bench/.storage-scale-test/data",
        "claim-a:bench/path with spaces",
        "Claim-A:bench",
        "a/b/claim:bench",
        "Bad_NS/claim:bench",
        "/claim:bench",
    ),
)
def test_logical_paths_reject_escape_and_reserved_state(logical):
    """Mapped benchmark data cannot escape or overlap orchestration state."""
    result = _run_bash(f"kubectl_map_logical_path {logical!r}")
    assert result.returncode != 0


def test_test_dir_weights_and_read_from_suffix_are_preserved():
    """Mapping changes only path identity, not workload weighting."""
    result = _run_bash("""
        declare -A TEST_DIRS=([lustre-a:/bench/primary]=2 [test-ns/lustre-b:bench/secondary]=1)
        declare -A mapped=()
        kubectl_map_test_dirs mapped
        [[ "${mapped[/mnt/storage-scale-test/lustre-a/bench/primary]}" == 2 ]]
        [[ "${mapped[/mnt/storage-scale-test/lustre-b/bench/secondary]}" == 1 ]]
        [[ $(kubectl_map_read_from_path lustre-a:/bench/primary/dataset) \
            == /mnt/storage-scale-test/lustre-a/bench/primary/dataset ]]
        [[ $(kubectl_map_read_from_path test-ns/lustre-b:bench/secondary/d) \
            == /mnt/storage-scale-test/lustre-b/bench/secondary/d ]]
        # The read path must be on the same claim as its root.
        ! kubectl_map_read_from_path lustre-b:bench/primary/dataset
        ! kubectl_map_read_from_path other-ns/lustre-a:bench/primary/dataset
        ! kubectl_map_read_from_path /bench/primary/dataset
    """)
    assert result.returncode == 0, result.stderr


def test_nested_roots_make_read_from_mapping_ambiguous():
    """A read path must be owned by exactly one logical root."""
    result = _run_bash("""
        declare -A TEST_DIRS=([claim:bench]=1 [claim:bench/nested]=1)
        kubectl_map_read_from_path claim:bench/nested/data
    """)
    assert result.returncode != 0
    assert "exactly one" in result.stderr


def test_control_root_is_the_lexically_first_normalized_test_dir():
    """Associative-array order and weights do not change durable-state placement."""
    result = _run_bash("""
        declare -A TEST_DIRS=([zeta:a]=99 [beta:/alpha/data]=1 [test-ns/middle:x]=2 [beta:zz]=1)
        kubectl_select_control_root logical mapped
        [[ "$logical" == test-ns/beta:alpha/data ]]
        [[ "$mapped" == /mnt/storage-scale-test/beta/alpha/data ]]
        kubectl_set_control_layout "$logical" "$mapped"
        [[ "$KUBECTL_CONTROL_ROOT" == \
            /mnt/storage-scale-test/beta/alpha/data/.storage-scale-test ]]
        kubectl_validate_saved_control_layout
        # The layout must be the root's own claim mount.
        ! kubectl_set_control_layout "$logical" /mnt/storage-scale-test/alpha/data
        # A batch union holds normalized keys from several namespaces.
        declare -A TEST_DIRS=([ns-b/claim:x]=1 [ns-a/claim:y]=1)
        kubectl_select_control_root logical mapped
        [[ "$logical" == ns-a/claim:y ]]
    """)
    assert result.returncode == 0, result.stderr


def test_read_from_cannot_scan_the_control_test_root():
    """A read scan cannot ingest orchestration state beneath its selected root."""
    result = _run_bash("""
        declare -A TEST_DIRS=([claim:alpha]=1 [claim:zeta]=1)
        kubectl_select_control_root logical mapped
        kubectl_set_control_layout "$logical" "$mapped"
        ! kubectl_map_read_from_path claim:alpha
        ! kubectl_map_read_from_path test-ns/claim:/alpha/
        [[ $(kubectl_map_read_from_path claim:alpha/dataset) == \
            /mnt/storage-scale-test/claim/alpha/dataset ]]
    """)
    assert result.returncode == 0, result.stderr


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
    assert lines[2] == "STATE=RUNNING"


def test_attempt_metadata_and_current_pointer_are_atomic_and_versioned(tmp_path):
    """Identity is create-only while state and current remain mutable."""
    result = _run_bash(f"""
        root={str(tmp_path / 'kubernetes')!r}
        mv() {{
            [[ " $* " != *' -T '* ]] || return 64
            command mv "$@"
        }}
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
        sed -i.bak 's/KUBECTL_ATTEMPT_SCHEMA=4/KUBECTL_ATTEMPT_SCHEMA=99/' \
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
    """[S-10] [T-01] Observations retry throttling, not authorization."""
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
    """[S-11] [T-02] API exhaustion reports stable actionable evidence."""
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
    """[S-12] DaemonSet readiness failure names the exact resource."""
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
    """[S-12] Diagnostics include bounded Pod, ledger, and identity evidence."""
    attempt = tmp_path / "state" / "attempts" / "1234abcd"
    attempt.mkdir(parents=True)
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
        kubectl_capture_retained_resource_identities() {{
            printf 'key\tsource\tkind\tname\tnamespace\tuid\tnonce\n' > "$3"
            printf 'workers\tjournal\tDaemonSet\tworkers\ttest-ns\tuid\tnonce\n' >> "$3"
        }}
        kubectl_capture_control_state_evidence() {{
            printf '===== publication-manifest.tsv =====\nmanifest\n' > "$4"
        }}
        path=$(kubectl_capture_resource_diagnostics {str(attempt)!r} \
            workers-not-ready test-ns DaemonSet sst-elb-1234abcd-workers \
            1234abcd)
        [[ -d "$path" ]]
        [[ -f "$path/resource.yaml" && -f "$path/resource.describe" ]]
        [[ -f "$path/pods.yaml" && -f "$path/pods.log" ]]
        [[ -f "$path/events.txt" ]]
        [[ -f "$path/pod-1.describe" ]]
        grep -F publication-manifest.tsv "$path/pvc-control-state.txt"
        grep -F $'workers\tjournal\tDaemonSet' \
          "$path/retained-resource-identities.tsv"
        grep -F $'schema\t1' "$path/bundle.tsv"
        grep -F $'attempt_id\t1234abcd' "$path/bundle.tsv"
        grep -F $'resource_kind\tDaemonSet' "$path/bundle.tsv"
        grep -F -- '--field-selector involvedObject.name=worker-a' "$calls"
        grep -F -- 'describe pod worker-a' "$calls"
        ! grep -F -- 'get events --sort-by' "$calls"
    """)
    assert result.returncode == 0, result.stderr


def test_local_diagnostic_size_does_not_require_gnu_du(tmp_path):
    """Launcher-side byte accounting works with BSD/macOS userland tools."""
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "one").write_bytes(b"12345")
    (tree / "two").write_bytes(b"678")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "du").write_text(
        "#!/bin/sh\necho 'du must not be called' >&2\nexit 99\n",
        encoding="utf-8",
    )
    (fake_bin / "du").chmod(0o755)
    result = _run_bash(f"""
        PATH={str(fake_bin)!r}:$PATH
        bytes=$(_kubectl_local_tree_apparent_bytes {str(tree)!r})
        [[ "$bytes" == 8 ]]
    """)
    assert result.returncode == 0, result.stderr
    assert "du must not be called" not in result.stderr


def test_diagnostic_capture_failure_prints_manual_inspection(tmp_path):
    """Supplemental failure retains the primary path and a safe operator command."""
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    result = _run_bash(f"""
        KUBECTL_NAMESPACE=test-ns
        kubectl_capture_attempt_diagnostics() {{ return 1; }}
        kubectl_preserve_attempt_diagnostics {str(attempt)!r} 1234abcd collect-failed
    """)
    assert result.returncode == 0, result.stderr
    assert "automatic Kubernetes diagnostic capture failed" in result.stderr
    assert "kubectl -n test-ns get job" in result.stderr
    assert "last proven local and PVC state was retained" in result.stderr


def test_readiness_diagnosis_bounds_event_queries(tmp_path):
    """A large failed Pod set cannot turn diagnosis into another long outage."""
    calls = tmp_path / "calls"
    result = _run_bash(f"""
        calls={str(calls)!r}
        kubectl_run_observational() {{
            printf '%s\n' "$*" >> "$calls"
            if [[ "$*" == *'-o name'* ]]; then
                for i in $(seq 1 20); do printf 'pod/worker-%s\n' "$i"; done
            elif [[ "$*" == *'get events'* ]]; then
                printf 'FailedScheduling\tno capacity\n'
            fi
        }}
        kubectl_classify_readiness_failure reason test-ns 1234abcd
        [[ "$reason" == POD_UNSCHEDULABLE ]]
        [[ $(grep -c 'get events' "$calls") -eq 4 ]]
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


def test_control_state_diagnostics_use_guarded_bounded_pvc_reads(tmp_path):
    """[C-13] Durable ledger evidence is guarded, selected, and size bounded."""
    captured = tmp_path / "script"
    destination = tmp_path / "evidence"
    result = _run_bash(f"""
        kubectl_pvc_exec() {{
            printf '%s' "$5" > {str(captured)!r}
            printf 'durable evidence\n'
        }}
        kubectl_capture_control_state_evidence test-ns helper 1234abcd \
          {str(destination)!r}
        grep -F 'realpath -e -- "$run"' {str(captured)!r}
        grep -F 'publication-manifest.tsv' {str(captured)!r}
        grep -F 'run-summary.tsv' {str(captured)!r}
        grep -F '*.workers.tsv' {str(captured)!r}
        grep -F 'control_root=${{run%"$run_suffix"}}' {str(captured)!r}
        grep -F 'test_root=${{control_root%"$control_suffix"}}' {str(captured)!r}
        [[ $(wc -c < {str(destination)!r}) -le 524288 ]]
    """)
    assert result.returncode == 0, result.stderr


def test_remote_pvc_enospc_has_distinct_diagnostics(tmp_path):
    """[C-14] Remote ENOSPC is distinct from local collection space."""
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
        set -e
        export KUBECTL_PVC=test-pvc
        kubectl_capture_resource_diagnostics() {{
            mkdir -p {str(tmp_path / 'capture')!r}
            printf {str(tmp_path / 'capture')!r}
        }}
        kubectl_run_bounded() {{ printf 'pvc evidence\n'; }}
        kubectl_pvc_exec() {{ printf 'filesystem capacity\ninode capacity\n'; }}
        path=$(kubectl_capture_pvc_diagnostics {str(attempt)!r} test-ns helper \
            1234abcd Job sst-elb-1234abcd-sweep)
        grep -F 'pvc evidence' "$path/pvc-0.yaml"
        grep -Fx '# claim: test-pvc' "$path/pvc-0.yaml"
        grep -F 'filesystem capacity' "$path/pvc-filesystem.txt"
        grep -F 'inode capacity' "$path/pvc-filesystem.txt"
    """)
    assert result.returncode == 0, result.stderr


def test_pvc_diagnostics_name_files_by_position_and_report_write_failures(
    tmp_path,
):
    """[R-28] A 253-character claim name cannot become a file name: each claim's
    evidence is pvc-<N>.yaml with the claim named inside. A file that cannot
    be written is reported on stderr without failing the capture."""
    long_claim = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])
    assert len(long_claim) == 253
    capture = tmp_path / "capture"
    result = _run_bash(f"""
        set -e
        kubectl_segment_claims() {{ printf '%s\\n' short {long_claim}; }}
        kubectl_capture_resource_diagnostics() {{
            mkdir -p {str(capture)!r}
            printf {str(capture)!r}
        }}
        kubectl_run_bounded() {{ printf 'pvc evidence\\n'; }}
        kubectl_pvc_exec() {{ printf 'filesystem capacity\\n'; }}
        path=$(kubectl_capture_pvc_diagnostics {str(tmp_path)!r} test-ns helper \\
            1234abcd Job sst-elb-1234abcd-sweep)
        grep -Fx '# claim: short' "$path/pvc-0.yaml"
        grep -Fx '# claim: {long_claim}' "$path/pvc-1.yaml"
        grep -F 'pvc evidence' "$path/pvc-1.yaml"
        # An unwritable destination is reported, not hidden, and not fatal.
        mkdir "$path/pvc-0.yaml.d"
        rm -f "$path/pvc-0.yaml"
        mv "$path/pvc-0.yaml.d" "$path/pvc-0.yaml"
        path=$(kubectl_capture_pvc_diagnostics {str(tmp_path)!r} test-ns helper \\
            1234abcd Job sst-elb-1234abcd-sweep)
        grep -Fx '# claim: {long_claim}' "$path/pvc-1.yaml"
    """)
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in capture.iterdir()) == [
        "pvc-0.yaml",
        "pvc-1.yaml",
        "pvc-filesystem.txt",
    ]
    assert (
        f"Warning: could not write the diagnostics of claim short to "
        f"{capture}/pvc-0.yaml" in result.stderr
    )


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
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd workers \
            DaemonSet sst-elb-1234abcd-workers test-ns workers-uid \
            0123456789abcdef0123456789abcdef
        kubectl_capture_resource_diagnostics() {{
            printf '%s|%s|%s|%s|%s|%s\n' "$@" >> {str(tmp_path / 'capture')!r}
            mkdir -p "$1/diagnostics"
            path=$(mktemp -d "$1/diagnostics/exact.XXXXXXXX")
            printf '%s\n' "$path"
        }}
        first=$(kubectl_capture_attempt_diagnostics "$root" 1234abcd submit-failed)
        second=$(kubectl_capture_attempt_diagnostics "$root" 1234abcd submit-failed)
        [[ $(wc -l <<< "$first") -eq 2 ]]
        [[ $(wc -l <<< "$second") -eq 2 ]]
        [[ "$first" == *"$root/attempts/1234abcd/diagnostics/exact."* ]]
        [[ "$second" == *"$root/attempts/1234abcd/diagnostics/exact."* ]]
        [[ "$second" != "$first" ]]
        [[ $(wc -l < {str(tmp_path / 'capture')!r}) -eq 4 ]]
        grep -F -- \
          'submit-failed|test-ns|Job|sst-elb-1234abcd-sweep|1234abcd' \
          {str(tmp_path / 'capture')!r}
        grep -F -- \
          'submit-failed|test-ns|DaemonSet|sst-elb-1234abcd-workers|1234abcd' \
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


# Segment and claim lifecycle foundations (identity schema 4).

_NONCE = "0123456789abcdef0123456789abcdef"
_SEGMENT_LABEL = "storage-scale-test.nvidia.com/segment"
_TEMPLATES = "storage-tests/fs/kubectl/templates"
_SEQUENCER_SCRIPT = (
    _REPOSITORY_ROOT
    / "storage-tests"
    / "fs"
    / "kubectl"
    / "_nv-elbencho-kubectl-sequencer.sh"
)
# Three claims in two namespaces, given in segment order: ns-b first. The
# identity sorts the claims by namespace, then name: pvc-a, pvc-z (both in
# segment 1, ns-a), then pvc-1 (segment 0, ns-b).
_THREE_CLAIMS = (
    "ns-b ns-b-uid pv-1 pv-1-uid pvc-1 pvc-1-uid "
    "ns-a ns-a-uid pv-0 pv-0-uid pvc-z pvc-z-uid "
    "ns-a ns-a-uid pv-2 pv-2-uid pvc-a pvc-a-uid"
)
_SINGLE_CLAIM = "test-ns ns-uid test-pv pv-uid test-pvc pvc-uid"
_INSPECTOR_KEYS = ["status-0badcafe", "collector-feedf00d"]
_SINGLE_SEGMENT_ORDER = [
    "sweep",
    "workers",
    "transfer",
    "status",
    "collector",
    "coord-net",
    "worker-net",
]


def _create_identity(root, claims):
    return f"""
        root={str(root)!r}
        kubectl_local_lock_acquire "$root" fd
        kubectl_attempt_create_identity "$root" "$fd" 1234abcd {_NONCE} {claims}
        kubectl_local_lock_release "$fd"
    """


def test_schema4_identity_records_segments_and_sorted_claims(tmp_path):
    """Segments keep their order; claims sort by namespace, then name."""
    result = _run_bash(_create_identity(tmp_path / "k8s", _THREE_CLAIMS) + """
        set -e
        kubectl_attempt_load_identity "$root/attempts/1234abcd"
        [[ "$KUBECTL_ATTEMPT_SCHEMA" == 4 ]]
        [[ "$KUBECTL_SEGMENT_COUNT" == 2 && "$KUBECTL_CLAIM_COUNT" == 3 ]]
        [[ "${KUBECTL_SEGMENT_NAMESPACES[*]}" == 'ns-b ns-a' ]]
        [[ "${KUBECTL_SEGMENT_NAMESPACE_UIDS[*]}" == 'ns-b-uid ns-a-uid' ]]
        [[ "${KUBECTL_CLAIM_PVCS[*]}" == 'pvc-a pvc-z pvc-1' ]]
        [[ "${KUBECTL_CLAIM_SEGMENTS[*]}" == '1 1 0' ]]
        [[ "${KUBECTL_CLAIM_NAMESPACES[*]}" == 'ns-a ns-a ns-b' ]]
        [[ "${KUBECTL_CLAIM_PVS[*]}" == 'pv-2 pv-0 pv-1' ]]
        [[ "${KUBECTL_CLAIM_PVC_UIDS[*]}" == 'pvc-a-uid pvc-z-uid pvc-1-uid' ]]
        [[ "${KUBECTL_CLAIM_LEASE_NAMES[2]}" == \
            "$(kubectl_pvc_lease_name ns-b-uid pv-1-uid pvc-1-uid)" ]]
        # Segment 0 is selected after a load, with its first claim.
        [[ "$KUBECTL_ACTIVE_SEGMENT" == 0 && "$KUBECTL_NAMESPACE" == ns-b ]]
        [[ "$KUBECTL_PVC" == pvc-1 && "$KUBECTL_ACTIVE_CLAIM" == 2 ]]
        [[ "$(kubectl_segment_claims 1 | paste -sd, -)" == pvc-a,pvc-z ]]
        [[ "$(kubectl_segment_claim_indexes 0)" == 2 ]]
    """)
    assert result.returncode == 0, result.stderr
    identity = yaml.safe_load(
        (tmp_path / "k8s/attempts/1234abcd/identity.yaml").read_text(encoding="utf-8")
    )
    assert identity["schema"] == 4
    assert [segment["namespace"] for segment in identity["segments"]] == [
        "ns-b",
        "ns-a",
    ]
    assert [claim["claim"] for claim in identity["segments"][1]["claims"]] == [
        "pvc-a",
        "pvc-z",
    ]
    claim = identity["segments"][0]["claims"][0]
    assert claim == {
        "index": 2,
        "namespace": "ns-b",
        "claim": "pvc-1",
        "pv": "pv-1",
        "namespace_uid": "ns-b-uid",
        "pv_uid": "pv-1-uid",
        "pvc_uid": "pvc-1-uid",
        "lease_name": claim["lease_name"],
    }
    assert claim["lease_name"].startswith("sst-elb-pvc-")


def test_single_claim_identity_is_one_segment(tmp_path):
    """A plain run's identity is one segment with one claim."""
    result = _run_bash(_create_identity(tmp_path / "k8s", _SINGLE_CLAIM) + """
        set -e
        kubectl_attempt_load_identity "$root/attempts/1234abcd"
        [[ "$KUBECTL_SEGMENT_COUNT" == 1 && "$KUBECTL_CLAIM_COUNT" == 1 ]]
        [[ "$KUBECTL_NAMESPACE/$KUBECTL_PVC/$KUBECTL_PV" == test-ns/test-pvc/test-pv ]]
        [[ "$KUBECTL_PVC_LEASE_NAME" == "$(kubectl_pvc_lease_name ns-uid pv-uid pvc-uid)" ]]
    """)
    assert result.returncode == 0, result.stderr
    shell = (tmp_path / "k8s/attempts/1234abcd/identity.sh").read_text(encoding="utf-8")
    assert shell.splitlines()[1:6] == [
        "KUBECTL_ATTEMPT_SCHEMA=4",
        "KUBECTL_ATTEMPT_ID=1234abcd",
        f"KUBECTL_OWNERSHIP_NONCE={_NONCE}",
        "KUBECTL_SEGMENT_COUNT=1",
        "KUBECTL_CLAIM_COUNT=1",
    ]
    assert "declare -ga KUBECTL_CLAIM_PVCS=( test-pvc )" in shell
    assert "KUBECTL_PVC=" not in shell


@pytest.mark.parametrize("schema", ("2", "3"))
def test_v1_identity_is_not_loaded_and_names_v1x(tmp_path, schema):
    """An attempt started by v1.x must be collected with v1.x."""
    result = _run_bash(f"""
        metadata={str(tmp_path / 'k8s/attempts/1234abcd')!r}
        mkdir -p "$metadata"
        printf '%s\\n' KUBECTL_ATTEMPT_SCHEMA={schema} KUBECTL_ATTEMPT_ID=1234abcd \\
            KUBECTL_OWNERSHIP_NONCE={_NONCE} KUBECTL_NAMESPACE=test-ns \\
            KUBECTL_PVC=test-pvc > "$metadata/identity.sh"
        printf 'schema: {schema}\\n' > "$metadata/identity.yaml"
        ! kubectl_attempt_load_identity "$metadata"
    """)
    assert result.returncode == 0, result.stderr
    assert "storage-scale-test v1.x" in result.stderr
    assert f"identity schema {schema}" in result.stderr
    assert "Collect this attempt with storage-scale-test v1.x" in result.stderr


@pytest.mark.parametrize(
    "claims",
    (
        pytest.param("", id="no-claim"),
        pytest.param("test-ns ns-uid test-pv pv-uid test-pvc", id="partial-tuple"),
        pytest.param(f"{_SINGLE_CLAIM} sst-elb-pvc-wrong", id="trailing-field"),
        pytest.param(
            f"{_SINGLE_CLAIM} test-ns ns-uid other-pv pv-2 test-pvc pvc-2",
            id="duplicate-claim",
        ),
        pytest.param(
            f"{_SINGLE_CLAIM} test-ns ns-uid test-pv pv-2 other-pvc pvc-2",
            id="duplicate-pv",
        ),
        pytest.param(
            f"{_SINGLE_CLAIM} test-ns ns-uid other-pv pv-uid other-pvc pvc-2",
            id="duplicate-pv-uid",
        ),
        pytest.param(
            f"{_SINGLE_CLAIM} test-ns ns-uid other-pv pv-2 other-pvc pvc-uid",
            id="duplicate-pvc-uid",
        ),
        pytest.param(
            f"{_SINGLE_CLAIM} test-ns other-uid other-pv pv-2 other-pvc pvc-2",
            id="namespace-with-two-uids",
        ),
        pytest.param(
            f"{_SINGLE_CLAIM} other-ns ns-uid other-pv pv-2 other-pvc pvc-2",
            id="uid-of-two-namespaces",
        ),
        pytest.param(
            " ".join(f"ns n pv{i} v{i} pvc{i} c{i}" for i in range(33)),
            id="too-many-claims",
        ),
        pytest.param(
            " ".join(f"ns{i} n{i} pv{i} v{i} pvc{i} c{i}" for i in range(17)),
            id="too-many-segments",
        ),
        pytest.param("BAD ns-uid test-pv pv-uid test-pvc pvc-uid", id="bad-namespace"),
        pytest.param("test-ns ns-uid test-pv pv-uid Bad_PVC pvc-uid", id="bad-claim"),
    ),
)
def test_identity_creation_rejects_invalid_claim_lists(tmp_path, claims):
    """No two claims may alias one claim, PV, or Lease."""
    result = _run_bash(f"""
        root={str(tmp_path / 'k8s')!r}
        kubectl_local_lock_acquire "$root" fd
        ! kubectl_attempt_create_identity "$root" "$fd" 1234abcd {_NONCE} {claims}
        [[ ! -e "$root/attempts/1234abcd" ]]
    """)
    assert result.returncode == 0, result.stderr


def test_the_largest_supported_identity_is_accepted(tmp_path):
    """Sixteen namespaces and thirty-two claims fit one attempt."""
    claims = " ".join(
        f"ns{i % 16} n{i % 16} pv{i} v{i} pvc{i:02d} c{i}" for i in range(32)
    )
    result = _run_bash(_create_identity(tmp_path / "k8s", claims) + """
        set -e
        kubectl_attempt_load_identity "$root/attempts/1234abcd"
        [[ "$KUBECTL_SEGMENT_COUNT" == 16 && "$KUBECTL_CLAIM_COUNT" == 32 ]]
        kubectl_use_segment 15
        [[ "$KUBECTL_NAMESPACE" == ns15 && "$KUBECTL_PVC" == pvc15 ]]
    """)
    assert result.returncode == 0, result.stderr


def _installed_locales():
    try:
        listing = subprocess.run(
            ["locale", "-a"], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return set()
    return {name.strip().lower().replace("utf-8", "utf8") for name in listing.split()}


# Names whose byte order differs from en_US.UTF-8 collation, which ignores
# punctuation at first: byte order puts "a" < "a-b" < "a.c" < "ab" and
# "fs-x" < "fsa", and "storage-test" < "storage-test-2" < "storagetest".
_COLLATION_CLAIMS = (
    "storage-test-2 n2 pv-1 v1 fsa c1 "
    "storage-test n1 pv-2 v2 fs-x c2 "
    "storage-test n1 pv-3 v3 ab c3 "
    "storage-test n1 pv-4 v4 a.c c4 "
    "storage-test n1 pv-5 v5 a-b c5 "
    "storage-test n1 pv-6 v6 a c6 "
    "storagetest n3 pv-7 v7 a c7"
)


@pytest.mark.parametrize("locale_name", ("C", "C.UTF-8", "en_US.UTF-8"))
def test_identity_claim_order_is_byte_order_in_every_locale(tmp_path, locale_name):
    """[S-30] Claims (and their Lease order) sort by namespace, then name, in byte
    order, and the identity validator compares them the same way, whatever the
    caller's collation locale."""
    normalized = locale_name.lower().replace("utf-8", "utf8")
    if normalized not in _installed_locales():
        pytest.skip(f"locale {locale_name} is not installed")
    result = _run_bash(
        f"export LC_ALL={locale_name}\n"
        + _create_identity(tmp_path / "k8s", _COLLATION_CLAIMS)
        + """
        set -e
        kubectl_attempt_load_identity "$root/attempts/1234abcd"
        _kubectl_validate_attempt_identity_arrays
        paste -d/ <(printf '%s\\n' "${KUBECTL_CLAIM_NAMESPACES[@]}") \
            <(printf '%s\\n' "${KUBECTL_CLAIM_PVCS[@]}") > "$root/order"
    """
    )
    assert result.returncode == 0, result.stderr
    order = (tmp_path / "k8s/order").read_text(encoding="utf-8").split()
    assert order == [
        "storage-test/a",
        "storage-test/a-b",
        "storage-test/a.c",
        "storage-test/ab",
        "storage-test/fs-x",
        "storage-test-2/fsa",
        "storagetest/a",
    ]


@pytest.mark.parametrize(
    "mutation",
    (
        pytest.param("unset 'KUBECTL_CLAIM_PVS[2]'", id="short-array"),
        pytest.param("KUBECTL_CLAIM_PVS[5]=pv-5", id="sparse-array"),
        pytest.param("KUBECTL_CLAIM_COUNT=4", id="claim-count-mismatch"),
        pytest.param("KUBECTL_SEGMENT_COUNT=3", id="segment-count-mismatch"),
        pytest.param("KUBECTL_SEGMENT_COUNT=0", id="zero-segments"),
        pytest.param("KUBECTL_CLAIM_PVCS[1]=pvc-a", id="duplicate-claim"),
        pytest.param("KUBECTL_CLAIM_PVS[1]=pv-2", id="duplicate-pv"),
        pytest.param("KUBECTL_CLAIM_LEASE_NAMES[1]=sst-elb-pvc-x", id="bad-lease"),
        pytest.param(
            "KUBECTL_CLAIM_PVCS=(pvc-z pvc-a pvc-1)", id="claims-out-of-order"
        ),
        pytest.param("KUBECTL_CLAIM_SEGMENTS[0]=2", id="claim-segment-out-of-range"),
        pytest.param("KUBECTL_CLAIM_SEGMENTS[2]=1", id="claim-in-another-namespace"),
        pytest.param(
            "KUBECTL_CLAIM_NAMESPACE_UIDS[0]=other", id="claim-namespace-uid-differs"
        ),
        pytest.param(
            "KUBECTL_SEGMENT_NAMESPACES[1]=ns-b", id="duplicate-segment-namespace"
        ),
        pytest.param("declare -gA KUBECTL_CLAIM_PV_UIDS=([0]=a)", id="not-indexed"),
    ),
)
def test_identity_validator_rejects_inconsistent_arrays(tmp_path, mutation):
    """The loader's array checks fail closed on every inconsistency."""
    result = _run_bash(_create_identity(tmp_path / "k8s", _THREE_CLAIMS) + f"""
        kubectl_attempt_load_identity "$root/attempts/1234abcd" || exit 1
        _kubectl_validate_attempt_identity_arrays || exit 1
        {'unset KUBECTL_CLAIM_PV_UIDS' if 'gA' in mutation else ':'}
        {mutation}
        ! _kubectl_validate_attempt_identity_arrays
    """)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "tamper",
    (
        "sed -i.bak 's/ pvc-a / pvc-b /' \"$identity\"",
        "sed -i.bak 's/pvc-a-uid/pvc-9-uid/g' \"$identity\"",
        "sed -i.bak 's/KUBECTL_CLAIM_SEGMENTS=( 1 1 0 )/"
        'KUBECTL_CLAIM_SEGMENTS=( 0 0 1 )/\' "$identity"',
        "printf 'KUBECTL_EXTRA=1\\n' >> \"$identity\"",
        "printf 'extra: 1\\n' >> \"${identity%.sh}.yaml\"",
    ),
)
def test_loader_rejects_tampered_identity(tmp_path, tamper):
    """The identity files must be exactly what the writer renders."""
    result = _run_bash(_create_identity(tmp_path / "k8s", _THREE_CLAIMS) + f"""
        identity="$root/attempts/1234abcd/identity.sh"
        kubectl_attempt_load_identity "$root/attempts/1234abcd" || exit 1
        {tamper}
        ! kubectl_attempt_load_identity "$root/attempts/1234abcd"
    """)
    assert result.returncode == 0, result.stderr


def test_segment_and_claim_selection_survive_identity_reloads(tmp_path):
    """Helpers that reload identity act on the selected segment or claim."""
    result = _run_bash(_create_identity(tmp_path / "k8s", _THREE_CLAIMS) + """
        set -e
        kubectl_attempt_load_identity "$root/attempts/1234abcd"
        show() {
            kubectl_attempt_load_identity "$root/attempts/1234abcd" || return 1
            printf '%s %s %s %s\\n' "$KUBECTL_ACTIVE_SEGMENT" "$KUBECTL_NAMESPACE" \\
                "$KUBECTL_PVC" "$KUBECTL_PVC_LEASE_NAME"
        }
        lease_z=$(kubectl_pvc_lease_name ns-a-uid pv-0-uid pvc-z-uid)
        lease_a=$(kubectl_pvc_lease_name ns-a-uid pv-2-uid pvc-a-uid)
        [[ $(kubectl_with_segment 1 show) == "1 ns-a pvc-a $lease_a" ]]
        [[ $(kubectl_with_claim 1 show) == "0 ns-a pvc-z $lease_z" ]]
        [[ $(kubectl_with_segment 1 kubectl_with_claim 1 show) == "1 ns-a pvc-z $lease_z" ]]
        [[ $(kubectl_with_claim 1 _kubectl_selected_claim_lease_key) == pvc-lease-c1 ]]
        kubectl_with_segment 1 show > /dev/null
        kubectl_with_claim 1 show > /dev/null
        [[ "$KUBECTL_PVC" == pvc-1 && "$KUBECTL_ACTIVE_SEGMENT" == 0 ]]
        [[ "$KUBECTL_NAMESPACE" == ns-b ]]
        ! kubectl_with_segment 2 show
        ! kubectl_with_claim 3 show
        ! kubectl_with_segment 1
        ! kubectl_use_segment -1
    """)
    assert result.returncode == 0, result.stderr


def test_segment_selection_applies_each_segments_control_root(tmp_path):
    """Each segment's run tree lives below its own namespace's control root."""
    result = _run_bash(_create_identity(tmp_path / "k8s", _THREE_CLAIMS) + """
        set -e
        KUBECTL_SEGMENT_CONTROL_ROOTS=(ns-b/pvc-1:data ns-a/pvc-z:bench)
        kubectl_attempt_load_identity "$root/attempts/1234abcd"
        [[ "$KUBECTL_CONTROL_LOGICAL_ROOT" == ns-b/pvc-1:data ]]
        [[ "$KUBECTL_CONTROL_ROOT" == \\
            /mnt/storage-scale-test/pvc-1/data/.storage-scale-test ]]
        [[ $(kubectl_with_segment 1 kubectl_attempt_remote_root 1234abcd) == \\
            /mnt/storage-scale-test/pvc-z/bench/.storage-scale-test/runs/1234abcd ]]
        [[ "$KUBECTL_CONTROL_LOGICAL_ROOT" == ns-b/pvc-1:data ]]
        _kubectl_validate_segment_control_roots
        KUBECTL_SEGMENT_CONTROL_ROOTS=(ns-b/pvc-1:data ns-a/pvc-1:bench)
        ! _kubectl_validate_segment_control_roots
        KUBECTL_SEGMENT_CONTROL_ROOTS=(ns-a/pvc-z:bench ns-b/pvc-1:data)
        ! _kubectl_validate_segment_control_roots
    """)
    assert result.returncode == 0, result.stderr


def test_segment_names_and_journal_keys_keep_segment_zero_plain():
    """Segment 0 keeps the plain names; later segments gain -s<k>."""
    result = _run_bash("""
        set -e
        [[ -z "$(kubectl_segment_suffix 0)" && $(kubectl_segment_suffix 3) == -s3 ]]
        ! kubectl_segment_suffix 16
        ! kubectl_segment_suffix 01
        for component in workers worker-net coord-net sweep sequencer; do
            [[ $(kubectl_segment_resource_name 1234abcd 0 "$component") \\
                == "sst-elb-1234abcd-$component" ]]
            [[ $(kubectl_segment_resource_name 1234abcd 2 "$component") \\
                == "sst-elb-1234abcd-s2-$component" ]]
        done
        [[ $(kubectl_segment_resource_name 1234abcd 0 upload 0badcafe) \\
            == sst-elb-1234abcd-upload-0badcafe ]]
        [[ $(kubectl_segment_resource_name 1234abcd 1 status 0badcafe) \\
            == sst-elb-1234abcd-s1-status-0badcafe ]]
        ! kubectl_segment_resource_name 1234abcd 1 status
        ! kubectl_segment_resource_name 1234abcd 1 workers 0badcafe
        ! kubectl_segment_resource_name 1234abcd 1 lease
        [[ $(kubectl_sequencer_resource_name 1234abcd) == sst-elb-1234abcd-sequencer ]]
        for base in sweep workers transfer worker-net coord-net; do
            [[ $(kubectl_segment_resource_key 0 "$base") == "$base" ]]
            [[ $(kubectl_segment_resource_key 15 "$base") == "$base-s15" ]]
            _kubectl_validate_resource_key "$(kubectl_segment_resource_key 15 "$base")"
        done
        [[ $(kubectl_segment_resource_key 0 sequencer-role) == sequencer-role-s0 ]]
        [[ $(kubectl_segment_resource_key 4 sequencer-binding) == sequencer-binding-s4 ]]
        ! kubectl_segment_resource_key 1 status
        ! kubectl_segment_resource_key 1 pvc-lease
        [[ $(kubectl_claim_lease_key 0) == pvc-lease ]]
        [[ $(kubectl_claim_lease_key 31) == pvc-lease-c31 ]]
        ! kubectl_claim_lease_key 32
        kubectl_resource_key_is_lease pvc-lease
        kubectl_resource_key_is_lease pvc-lease-c2
        ! kubectl_resource_key_is_lease pvc-lease-c0
        ! kubectl_resource_key_is_lease pvc-lease-t2
        ! kubectl_resource_key_is_lease workers
        [[ $(_kubectl_resource_key_segment workers-s3) == 3 ]]
        [[ $(_kubectl_resource_key_segment pvc-lease-c3) == - ]]
        [[ $(_kubectl_resource_key_segment status-0badcafe) == 0 ]]
    """)
    assert result.returncode == 0, result.stderr


def _enumerate(tmp_path, claims):
    result = _run_bash(_create_identity(tmp_path / "k8s", claims) + """
        mkdir -p "$root/attempts/1234abcd/resources"
        : > "$root/attempts/1234abcd/resources/status-0badcafe.sh"
        : > "$root/attempts/1234abcd/resources/collector-feedf00d.sh"
        kubectl_attempt_resource_keys keys "$root" 1234abcd || exit 1
        kubectl_attempt_lease_keys leases "$root" 1234abcd || exit 1
        kubectl_attempt_workload_keys workloads "$root" 1234abcd || exit 1
        printf '%s\\n' "${keys[*]}" "${leases[*]}" "${workloads[*]}"
    """)
    assert result.returncode == 0, result.stderr
    return [line.split() for line in result.stdout.splitlines()]


def test_single_segment_enumeration_keeps_the_plain_order(tmp_path):
    """One segment cleans up in the order every plain attempt uses."""
    keys, leases, workloads = _enumerate(tmp_path, _SINGLE_CLAIM)
    assert keys == [*_SINGLE_SEGMENT_ORDER, *_INSPECTOR_KEYS]
    assert leases == ["pvc-lease"]
    assert workloads == ["sweep", "workers"]


def test_multi_segment_enumeration_stops_sequencer_first_and_rbac_last(tmp_path):
    """Workloads stop before helpers; DaemonSets and Leases unwind in reverse."""
    keys, leases, workloads = _enumerate(tmp_path, _THREE_CLAIMS)
    assert workloads == [
        "sequencer",
        "sweep",
        "sweep-s1",
        "workers-s1",
        "workers",
    ]
    assert keys == [
        *workloads,
        "transfer",
        "transfer-s1",
        "status",
        "collector",
        *_INSPECTOR_KEYS,
        "coord-net",
        "worker-net",
        "coord-net-s1",
        "worker-net-s1",
        "sequencer-binding-s0",
        "sequencer-binding-s1",
        "sequencer-role-s0",
        "sequencer-role-s1",
        "sequencer-sa",
    ]
    # One Lease per claim, released in reverse claim order.
    assert leases == ["pvc-lease-c2", "pvc-lease-c1", "pvc-lease"]


def test_enumeration_fails_closed_without_identity(tmp_path):
    result = _run_bash(f"""
        mkdir -p {str(tmp_path / 'k8s/attempts/1234abcd')!r}
        ! kubectl_attempt_resource_keys keys {str(tmp_path / 'k8s')!r} 1234abcd || exit 1
        ! kubectl_resource_keys_for_segment_count keys 17 || exit 1
        ! kubectl_lease_keys_for_claim_count keys 33 || exit 1
        # An unpublished attempt directory cannot hold any journal.
        keys=(stale)
        kubectl_attempt_resource_keys keys {str(tmp_path / 'k8s')!r} abcd1234
        [[ ${{#keys[@]}} -eq 0 ]]
    """)
    assert result.returncode == 0, result.stderr


def _render(body):
    result = _run_bash(body)
    assert result.returncode == 0, result.stderr
    return yaml.safe_load(result.stdout)


def _common_args(segment):
    return (
        f"ATTEMPT_ID=1234abcd OWNERSHIP_NONCE={_NONCE} NAMESPACE=ns-b "
        f"SEGMENT_INDEX={segment}"
    )


_CLAIM_VOLUMES = "_kubectl_claim_volume_arguments volumes {shape} lustre-a lustre-b\n"


@pytest.mark.parametrize(
    "template", ("worker-network-policy", "coordinator-network-policy")
)
def test_policies_select_only_their_segment(template):
    """Two segments' Pods never match each other's policies."""
    policy = _render(f"""
        kubectl_render_attempt_template {_TEMPLATES}/{template}.yaml.tmpl \\
            {_common_args(2)} RESOURCE_NAME=sst-elb-1234abcd-s2-policy
    """)
    assert policy["spec"]["podSelector"]["matchLabels"][_SEGMENT_LABEL] == "2"
    peers = policy["spec"].get("ingress") or policy["spec"]["egress"]
    peer = peers[0].get("from") or peers[0]["to"]
    assert peer[0]["podSelector"]["matchLabels"][_SEGMENT_LABEL] == "2"
    assert policy["metadata"]["labels"][_SEGMENT_LABEL] == "2"


def test_sweep_job_renders_segment_label_suspend_flag_and_every_claim():
    job = _render(_CLAIM_VOLUMES.format(shape="workload") + f"""
        kubectl_render_attempt_template {_TEMPLATES}/sweep-job.yaml.tmpl \\
            {_common_args(1)} RESOURCE_NAME=sst-elb-1234abcd-s1-sweep \\
            IMAGE=breuner/elbencho:v3.2-1 IMAGE_PULL_POLICY=Never \\
            RUN_AS_USER=2000 RUN_AS_GROUP=2000 "${{volumes[@]}}" \\
            COORDINATOR_NODE=node-a JOB_SUSPEND=true \\
            REMOTE_RUN_DIRECTORY=$KUBECTL_CONTROL_ROOT/runs/1234abcd
    """)
    assert job["spec"]["suspend"] is True
    assert job["metadata"]["labels"][_SEGMENT_LABEL] == "1"
    pod = job["spec"]["template"]
    assert pod["metadata"]["labels"][_SEGMENT_LABEL] == "1"
    mounts = pod["spec"]["containers"][0]["volumeMounts"]
    assert mounts == [
        {"name": "claim-0", "mountPath": "/mnt/storage-scale-test/lustre-a"},
        {"name": "claim-1", "mountPath": "/mnt/storage-scale-test/lustre-b"},
        {"name": "scratch", "mountPath": "/tmp/storage-scale-test"},
    ]
    assert pod["spec"]["volumes"] == [
        {"name": "claim-0", "persistentVolumeClaim": {"claimName": "lustre-a"}},
        {"name": "claim-1", "persistentVolumeClaim": {"claimName": "lustre-b"}},
        {"name": "scratch", "emptyDir": {}},
    ]


@pytest.mark.parametrize("template", ("worker-daemonset", "validation-job"))
def test_workload_templates_mount_every_claim(template):
    arguments = {
        "worker-daemonset": (
            "IMAGE=breuner/elbencho:v3.2-1 IMAGE_PULL_POLICY=Never "
            "RUN_AS_USER=2000 RUN_AS_GROUP=2000 SEGMENT_INDEX=1 "
            '"NODE_SELECTOR_BLOCK=$(kubectl_render_node_selector a=b)" '
            '"NODE_AFFINITY_VALUES=$(kubectl_render_node_affinity_values node-a)"'
        ),
        "validation-job": (
            "IMAGE=breuner/elbencho:v3.2-1 IMAGE_PULL_POLICY=Never "
            "RUN_AS_USER=2000 RUN_AS_GROUP=2000 NODE_NAME=node-a "
            "MOUNT_ROOT=/mnt/storage-scale-test "
            '"TEST_ROOT_ARGUMENTS=$(kubectl_render_test_root_arguments '
            '/mnt/storage-scale-test/lustre-a/x /mnt/storage-scale-test/lustre-b/y)"'
        ),
    }[template]
    manifest = _render(_CLAIM_VOLUMES.format(shape="workload") + f"""
        kubectl_render_attempt_template {_TEMPLATES}/{template}.yaml.tmpl \\
            ATTEMPT_ID=1234abcd OWNERSHIP_NONCE={_NONCE} NAMESPACE=ns-b \\
            RESOURCE_NAME=sst-elb-1234abcd-x "${{volumes[@]}}" {arguments}
    """)
    pod = manifest["spec"]["template"]["spec"]
    assert [mount["mountPath"] for mount in pod["containers"][0]["volumeMounts"]] == [
        "/mnt/storage-scale-test/lustre-a",
        "/mnt/storage-scale-test/lustre-b",
    ]
    assert [
        volume["persistentVolumeClaim"]["claimName"] for volume in pod["volumes"]
    ] == [
        "lustre-a",
        "lustre-b",
    ]


@pytest.mark.parametrize("helper", ("transfer", "status", "collector"))
def test_helper_pods_carry_their_segment_and_mount_its_claims(helper):
    pod = _render(_CLAIM_VOLUMES.format(shape="pod") + f"""
        kubectl_render_attempt_template {_TEMPLATES}/{helper}-pod.yaml.tmpl \\
            {_common_args(3)} RESOURCE_NAME=sst-elb-1234abcd-s3-{helper}-0badcafe \\
            OPERATION_TOKEN=0badcafe IMAGE=breuner/elbencho:v3.2-1 \\
            IMAGE_PULL_POLICY=Never RUN_AS_USER=2000 RUN_AS_GROUP=2000 \\
            "${{volumes[@]}}" NODE_NAME=node-a \\
            REMOTE_RUN_DIRECTORY=$KUBECTL_CONTROL_ROOT/runs/1234abcd
    """)
    assert pod["metadata"]["labels"][_SEGMENT_LABEL] == "3"
    assert pod["spec"]["containers"][0]["volumeMounts"] == [
        {"name": "claim-0", "mountPath": "/mnt/storage-scale-test/lustre-a"},
        {"name": "claim-1", "mountPath": "/mnt/storage-scale-test/lustre-b"},
    ]
    assert [volume["name"] for volume in pod["spec"]["volumes"]] == [
        "claim-0",
        "claim-1",
    ]


def test_claim_volume_renderers_name_each_claim_by_position():
    result = _run_bash("""
        set -e
        kubectl_render_claim_volume_mounts 4 lustre-a lustre.b
        kubectl_render_claim_volumes 2 lustre-a
        ! kubectl_render_claim_volume_mounts 4
        ! kubectl_render_claim_volume_mounts 3 lustre-a
        ! kubectl_render_claim_volume_mounts 4 lustre-a lustre-a
        ! kubectl_render_claim_volumes 4 Bad_Claim
        ! kubectl_render_claim_volumes 4 $(seq -f 'c%g' 1 33)
    """)
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "    - name: claim-0\n"
        "      mountPath: /mnt/storage-scale-test/lustre-a\n"
        "    - name: claim-1\n"
        "      mountPath: /mnt/storage-scale-test/lustre.b\n"
        "  - name: claim-0\n"
        "    persistentVolumeClaim:\n"
        '      claimName: "lustre-a"\n'
    )


def test_status_and_collector_helpers_mount_only_the_control_claim(tmp_path):
    """[R-27] Only the transfer helper checks paths on every claim; status and
    collector helpers reach just the run tree, so they mount the segment's
    control claim alone. A deleted claim of the segment is still drift."""
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    result = _run_bash(_create_identity(tmp_path / "k8s", _THREE_CLAIMS) + f"""
        set -e
        kubectl_attempt_load_identity "$root/attempts/1234abcd"
        kubectl_use_segment 1
        KUBECTL_CONTROL_LOGICAL_ROOT=ns-a/pvc-z:bench
        KUBECTL_CONTROL_TEST_ROOT="$KUBECTL_SWEEP_MOUNT_ROOT/pvc-z/bench"
        KUBECTL_CONTROL_ROOT="$KUBECTL_CONTROL_TEST_ROOT/.storage-scale-test"
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000
        export KUBECTL_RUN_AS_GROUP=2000
        kubectl_create_owned_object() {{
            printf '%s\\n' "$7" > {str(manifests)!r}/"$3.yaml"
            printf -v "$1" uid
        }}
        kubectl_wait_owned_ready_pod() {{ :; }}
        for helper in transfer status collector; do
            kubectl_create_helper_pod uid "$helper" ns-a "sst-elb-1234abcd-s1-$helper" \\
                {_NONCE} 1234abcd node-a 0badcafe
        done
        # A control root on no claim of the segment renders no helper.
        KUBECTL_CONTROL_LOGICAL_ROOT=ns-a/pvc-1:bench
        KUBECTL_CONTROL_TEST_ROOT="$KUBECTL_SWEEP_MOUNT_ROOT/pvc-1/bench"
        KUBECTL_CONTROL_ROOT="$KUBECTL_CONTROL_TEST_ROOT/.storage-scale-test"
        ! kubectl_create_helper_pod uid status ns-a sst-elb-1234abcd-s1-bad \\
            {_NONCE} 1234abcd node-a 0badcafe
        [[ ! -e {str(manifests)!r}/sst-elb-1234abcd-s1-bad.yaml ]]
        # Deleting the segment's other claim is still identity drift.
        kubectl_validate_cluster_identity() {{
            [[ "$*" == 'ns-a pvc-a pvc-z' ]] || return 9
            printf 'persistentvolumeclaims "pvc-a" not found\\n' >&2
            return 1
        }}
        ! _kubectl_verify_saved_cluster_identity "$root" 1234abcd
    """)
    assert result.returncode == 0, result.stderr
    claims = {}
    for helper in ("transfer", "status", "collector"):
        pod = yaml.safe_load(
            (manifests / f"sst-elb-1234abcd-s1-{helper}.yaml").read_text(
                encoding="utf-8"
            )
        )
        claims[helper] = [
            volume["persistentVolumeClaim"]["claimName"]
            for volume in pod["spec"]["volumes"]
        ]
        assert [
            mount["mountPath"] for mount in pod["spec"]["containers"][0]["volumeMounts"]
        ] == [f"/mnt/storage-scale-test/{claim}" for claim in claims[helper]]
    assert claims == {
        "transfer": ["pvc-a", "pvc-z"],
        "status": ["pvc-z"],
        "collector": ["pvc-z"],
    }
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=IDENTITY_MISMATCH" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_NAME=pvc-a" in result.stderr


# Valid DNS-1123 names that a plain YAML scalar would read as a number, a
# boolean or null.
_AMBIGUOUS_CLAIMS = ("123", "1.5", "yes", "on", "null")


def _workload_claim_names(manifest: dict) -> list:
    """The claimName of every claim volume of a Job or DaemonSet."""
    volumes: list = manifest["spec"]["template"]["spec"]["volumes"]
    return [
        volume["persistentVolumeClaim"]["claimName"]
        for volume in volumes
        if "persistentVolumeClaim" in volume
    ]


def _assert_strings(value, expected):
    assert isinstance(value, str), f"{value!r} is not a YAML string"
    assert value == expected


@pytest.mark.parametrize("helper", ("transfer", "status", "collector"))
def test_ambiguous_names_render_as_yaml_strings_in_helper_pods(helper):
    """[S-31] Claim, namespace and node names stay strings whatever they spell."""
    pod = _render(f"""
        _kubectl_claim_volume_arguments volumes pod {' '.join(_AMBIGUOUS_CLAIMS)}
        kubectl_render_attempt_template {_TEMPLATES}/{helper}-pod.yaml.tmpl \\
            ATTEMPT_ID=1234abcd OWNERSHIP_NONCE={_NONCE} NAMESPACE=null \\
            SEGMENT_INDEX=0 RESOURCE_NAME=sst-elb-1234abcd-{helper}-0badcafe \\
            OPERATION_TOKEN=0badcafe IMAGE=breuner/elbencho:v3.2-1 \\
            IMAGE_PULL_POLICY=Never RUN_AS_USER=2000 RUN_AS_GROUP=2000 \\
            "${{volumes[@]}}" NODE_NAME=on \\
            REMOTE_RUN_DIRECTORY=$KUBECTL_CONTROL_ROOT/runs/1234abcd
    """)
    _assert_strings(pod["metadata"]["namespace"], "null")
    _assert_strings(pod["spec"]["nodeName"], "on")
    for volume, claim in zip(pod["spec"]["volumes"], _AMBIGUOUS_CLAIMS, strict=True):
        _assert_strings(volume["persistentVolumeClaim"]["claimName"], claim)


def test_ambiguous_names_render_as_yaml_strings_in_workloads():
    """[S-31] The sweep Job, worker DaemonSet, validation Job, sequencer RBAC and
    claim Lease keep every name a string."""
    result = _run_bash(f"""
        set -e
        _kubectl_claim_volume_arguments volumes workload {' '.join(_AMBIGUOUS_CLAIMS)}
        common="ATTEMPT_ID=1234abcd OWNERSHIP_NONCE={_NONCE} NAMESPACE=yes"
        kubectl_render_attempt_template {_TEMPLATES}/sweep-job.yaml.tmpl $common \\
            SEGMENT_INDEX=0 RESOURCE_NAME=sst-elb-1234abcd-sweep \\
            IMAGE=breuner/elbencho:v3.2-1 IMAGE_PULL_POLICY=Never \\
            RUN_AS_USER=2000 RUN_AS_GROUP=2000 "${{volumes[@]}}" \\
            COORDINATOR_NODE=1.5 JOB_SUSPEND=false \\
            REMOTE_RUN_DIRECTORY=$KUBECTL_CONTROL_ROOT/runs/1234abcd
        printf '\\n---\\n'
        kubectl_render_attempt_template {_TEMPLATES}/worker-daemonset.yaml.tmpl \\
            $common SEGMENT_INDEX=0 RESOURCE_NAME=sst-elb-1234abcd-workers \\
            IMAGE=breuner/elbencho:v3.2-1 IMAGE_PULL_POLICY=Never \\
            RUN_AS_USER=2000 RUN_AS_GROUP=2000 "${{volumes[@]}}" \\
            "NODE_SELECTOR_BLOCK=$(kubectl_render_node_selector on=yes,null=123)" \\
            "NODE_AFFINITY_VALUES=$(kubectl_render_node_affinity_values 123 on)"
        printf '\\n---\\n'
        kubectl_render_attempt_template {_TEMPLATES}/validation-job.yaml.tmpl \\
            $common RESOURCE_NAME=sst-elb-1234abcd-validate \\
            IMAGE=breuner/elbencho:v3.2-1 IMAGE_PULL_POLICY=Never \\
            RUN_AS_USER=2000 RUN_AS_GROUP=2000 "${{volumes[@]}}" NODE_NAME=null \\
            MOUNT_ROOT=/mnt/storage-scale-test \\
            "TEST_ROOT_ARGUMENTS=$(kubectl_render_test_root_arguments \\
                /mnt/storage-scale-test/123/x)"
        printf '\\n---\\n'
        kubectl_render_sequencer_role_binding 1234abcd {_NONCE} 1 yes on
        printf '\\n---\\n'
        KUBECTL_NAMESPACE_UID=ns-uid KUBECTL_PV_UID=pv-uid KUBECTL_PVC_UID=pvc-uid \\
            kubectl_render_pvc_lease sst-elb-pvc-0123 null 1234abcd {_NONCE}
    """)
    assert result.returncode == 0, result.stderr
    job, daemonset, validation, binding, lease = list(yaml.safe_load_all(result.stdout))
    for manifest in (job, daemonset, validation, binding):
        _assert_strings(manifest["metadata"]["namespace"], "yes")
    _assert_strings(lease["metadata"]["namespace"], "null")
    _assert_strings(job["spec"]["template"]["spec"]["nodeName"], "1.5")
    _assert_strings(validation["spec"]["template"]["spec"]["nodeName"], "null")
    _assert_strings(binding["subjects"][0]["namespace"], "on")
    for manifest in (job, daemonset, validation):
        claims = _workload_claim_names(manifest)
        for claim, expected in zip(claims, _AMBIGUOUS_CLAIMS, strict=True):
            _assert_strings(claim, expected)
    pod = daemonset["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"on": "yes", "null": "123"}
    values = pod["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"]
    assert values == ["123", "on"]


_CLAIM_TEMPLATE = (
    'template=$(mktemp)\nprintf "%s\\n" "x:" "@@{token}@@" > "$template"\n'
)


@pytest.mark.parametrize(
    ("value", "accepted"),
    (
        ("$(kubectl_render_claim_volume_mounts 12 a b)", True),
        ("$(kubectl_render_claim_volumes 8 a b)", True),
        (
            "$(kubectl_render_claim_volume_mounts 12 a)\n            readOnly: true",
            False,
        ),
        ("$(kubectl_render_claim_volumes 8 a)\n        - name: extra", False),
        ("$(kubectl_render_claim_volume_mounts 12 a | sed s/claim-0/claim-9/)", False),
        ("$(kubectl_render_claim_volumes 8 a | sed s/claimName/claimNames/)", False),
        ("", False),
    ),
)
def test_claim_volume_placeholders_accept_only_rendered_blocks(value, accepted):
    """Nothing but a block the renderer prints can enter a manifest."""
    token = "CLAIM_VOLUMES" if "claim_volumes " in value else "CLAIM_VOLUME_MOUNTS"
    result = _run_bash(
        _CLAIM_TEMPLATE.format(token=token)
        + f'value="{value}"\n'
        + f'kubectl_render_attempt_template "$template" "{token}=$value"'
    )
    assert (result.returncode == 0) is accepted, result.stderr
    assert bool(result.stdout) is accepted


def test_sequencer_rbac_grants_only_get_and_patch_on_one_job():
    """Each Role names exactly that attempt's Job in its own namespace."""
    role = _render(
        f"kubectl_render_sequencer_role 1234abcd {_NONCE} 1 ns-b "
        "sst-elb-1234abcd-s1-sweep"
    )
    assert role["kind"] == "Role"
    assert role["metadata"]["name"] == "sst-elb-1234abcd-s1-sequencer"
    assert role["metadata"]["namespace"] == "ns-b"
    assert role["rules"] == [
        {
            "apiGroups": ["batch"],
            "resources": ["jobs"],
            "verbs": ["get", "patch"],
            "resourceNames": ["sst-elb-1234abcd-s1-sweep"],
        }
    ]
    binding = _render(
        f"kubectl_render_sequencer_role_binding 1234abcd {_NONCE} 1 ns-b ns-a"
    )
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "sst-elb-1234abcd-s1-sequencer",
    }
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": "sst-elb-1234abcd-sequencer",
            "namespace": "ns-a",
        }
    ]
    account = _render(
        f"kubectl_render_sequencer_service_account 1234abcd {_NONCE} ns-a"
    )
    assert account["kind"] == "ServiceAccount"
    assert account["automountServiceAccountToken"] is False


_SEQUENCER_ENVIRONMENT = f"""
    KUBECTL_SEQUENCER_IMAGE=registry.example/kubectl@sha256:{'a' * 64}
    KUBECTL_IMAGE_PULL_POLICY=IfNotPresent
    KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=3000
    KUBECTL_NODE_SELECTOR=storage-test=true
"""


def test_sequencer_job_embeds_the_checked_in_script_and_job_triples():
    """Only the sequencer Pod mounts a token, and it runs the reviewed script."""
    job = _render(_SEQUENCER_ENVIRONMENT + f"""
        kubectl_render_sequencer_job 1234abcd {_NONCE} ns-a \
            ns-a sst-elb-1234abcd-sweep uid-0 ns-b sst-elb-1234abcd-s1-sweep uid-1
    """)
    assert job["metadata"]["name"] == "sst-elb-1234abcd-sequencer"
    assert job["spec"]["backoffLimit"] > 0
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "sst-elb-1234abcd-sequencer"
    assert pod["automountServiceAccountToken"] is True
    assert pod["nodeSelector"] == {"storage-test": "true"}
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["runAsGroup"] == 3000
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert all("persistentVolumeClaim" not in volume for volume in pod["volumes"])
    container = pod["containers"][0]
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["image"].endswith("a" * 64)
    script = _SEQUENCER_SCRIPT.read_text(encoding="utf-8")
    assert container["command"] == [
        "/bin/sh",
        "-c",
        script,
        "storage-scale-test-sequencer",
    ]
    assert container["args"] == [
        "ns-a",
        "sst-elb-1234abcd-sweep",
        "uid-0",
        "ns-b",
        "sst-elb-1234abcd-s1-sweep",
        "uid-1",
    ]
    environment = {item["name"]: item["value"] for item in container["env"]}
    assert environment["KUBECTL_SEQUENCER_POLL_SECONDS"] == "10"


_ONE_TOKEN_TEMPLATE = (
    'template=$(mktemp)\nprintf "%s\\n" "x:" "@@{token}@@" > "$template"\n'
)
_VALID_SCRIPT = "script=$(kubectl_render_sequencer_script)\n"


@pytest.mark.parametrize(
    ("body", "accepted"),
    (
        ("kubectl_render_template 'n: @@SEGMENT_INDEX@@' SEGMENT_INDEX=15", True),
        ("kubectl_render_template 'n: @@SEGMENT_INDEX@@' SEGMENT_INDEX=16", False),
        ("kubectl_render_template 'n: @@SEGMENT_INDEX@@' SEGMENT_INDEX=01", False),
        ("kubectl_render_template 's: @@JOB_SUSPEND@@' JOB_SUSPEND=true", True),
        ("kubectl_render_template 's: @@JOB_SUSPEND@@' JOB_SUSPEND=yes", False),
        ("kubectl_render_template 'p: @@POLL_SECONDS@@' POLL_SECONDS=3600", True),
        ("kubectl_render_template 'p: @@POLL_SECONDS@@' POLL_SECONDS=0", False),
        (
            _VALID_SCRIPT
            + _ONE_TOKEN_TEMPLATE.format(token="SEQUENCER_SCRIPT")
            + 'kubectl_render_attempt_template "$template" "SEQUENCER_SCRIPT=$script"',
            True,
        ),
        (
            _VALID_SCRIPT
            + _ONE_TOKEN_TEMPLATE.format(token="SEQUENCER_SCRIPT")
            + 'kubectl_render_attempt_template "$template" "SEQUENCER_SCRIPT=$script x"',
            False,
        ),
        (
            _VALID_SCRIPT
            + 'template=$(mktemp)\nprintf "@@SEQUENCER_SCRIPT@@\\n%.0s" 1 2 > "$template"\n'
            + 'kubectl_render_attempt_template "$template" "SEQUENCER_SCRIPT=$script"',
            False,
        ),
        (
            "args=$(kubectl_render_sequencer_arguments ns-a job-a uid-a)\n"
            + _ONE_TOKEN_TEMPLATE.format(token="SEQUENCER_ARGUMENTS")
            + 'kubectl_render_attempt_template "$template" "SEQUENCER_ARGUMENTS=$args"',
            True,
        ),
        (
            _ONE_TOKEN_TEMPLATE.format(token="SEQUENCER_ARGUMENTS")
            + 'kubectl_render_attempt_template "$template" '
            + "$'SEQUENCER_ARGUMENTS=            - \"ns-a\"'",
            False,
        ),
        ("kubectl_render_sequencer_arguments ns-a job-a 'uid\"x'", False),
        (
            _SEQUENCER_ENVIRONMENT.replace("@sha256:" + "a" * 64, ":latest")
            + f"kubectl_render_sequencer_job 1234abcd {_NONCE} ns-a ns-a job uid",
            False,
        ),
    ),
)
def test_renderer_validates_multi_segment_placeholders(body, accepted):
    """Each new placeholder accepts its grammar and nothing that alters YAML."""
    result = _run_bash(body)
    assert (result.returncode == 0) is accepted, result.stderr
    assert bool(result.stdout) is accepted


@pytest.mark.parametrize(
    ("count", "image", "accepted"),
    (
        ("1", "", True),
        ("2", "", True),
        ("2", f"registry.example/kubectl@sha256:{'b' * 64}", True),
        ("2", "registry.example/kubectl:1.31", False),
        ("1", "registry.example/kubectl:1.31", False),
        ("17", f"registry.example/kubectl@sha256:{'b' * 64}", False),
    ),
)
def test_a_configured_sequencer_image_must_be_pinned(count, image, accepted):
    """Without a value the default applies; a value that is set is always checked."""
    result = _run_bash(
        f"KUBECTL_SEQUENCER_IMAGE={image!r} "
        f"kubectl_validate_sequencer_configuration {count}"
    )
    assert (result.returncode == 0) is accepted, result.stderr
    if not accepted:
        assert "Error:" in result.stderr


@pytest.mark.parametrize(
    ("setting", "expected"),
    (
        ("unset KUBECTL_SEQUENCER_IMAGE", DEFAULT_SEQUENCER_IMAGE),
        ("KUBECTL_SEQUENCER_IMAGE=", DEFAULT_SEQUENCER_IMAGE),
        (f"KUBECTL_SEQUENCER_IMAGE=registry.example/k@sha256:{'c' * 64}", None),
    ),
)
def test_the_sequencer_job_runs_the_default_image_unless_overridden(setting, expected):
    """An empty or unset KUBECTL_SEQUENCER_IMAGE selects the pinned default."""
    job = _render(
        _SEQUENCER_ENVIRONMENT.replace(
            f"KUBECTL_SEQUENCER_IMAGE=registry.example/kubectl@sha256:{'a' * 64}",
            setting,
        )
        + f"""
        kubectl_render_sequencer_job 1234abcd {_NONCE} ns-a \
            ns-a sst-elb-1234abcd-sweep uid-0 ns-b sst-elb-1234abcd-s1-sweep uid-1
    """
    )
    image = job["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image == (expected or setting.split("=", 1)[1])


def test_the_default_sequencer_image_is_pinned_and_valid():
    """The default passes the same checks as a configured image."""
    assert re.fullmatch(
        r"docker\.io/alpine/kubectl:[0-9.]+@sha256:[0-9a-f]{64}",
        DEFAULT_SEQUENCER_IMAGE,
    )
    result = _run_bash(
        f"KUBECTL_SEQUENCER_IMAGE={DEFAULT_SEQUENCER_IMAGE} "
        "kubectl_validate_sequencer_configuration 2 && kubectl_sequencer_image"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == DEFAULT_SEQUENCER_IMAGE


def test_env_template_shows_the_default_sequencer_image():
    """env.sh.template's commented example is the code's default, so they cannot drift."""
    template = (_REPOSITORY_ROOT / "env.sh.template").read_text(encoding="utf-8")
    examples = re.findall(
        r"^#export KUBECTL_SEQUENCER_IMAGE=(\S*)$", template, re.MULTILINE
    )
    assert examples == [DEFAULT_SEQUENCER_IMAGE]
