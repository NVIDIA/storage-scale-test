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
        "--foreground --kill-after=5s 11s kubectl " "--request-timeout=7s get pods"
    )


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
