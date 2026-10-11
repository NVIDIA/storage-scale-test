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

"""Claim-qualified Kubernetes TEST_DIRS keys (lib/_kubectl_claims.sh)."""

from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_CLAIMS = _ROOT / "lib/_kubectl_claims.sh"
_BASH = shutil.which("bash") or "/bin/bash"
_SEQUENCER_IMAGE = "registry.example/kubectl@sha256:" + "a" * 64


def _bash(body: str) -> subprocess.CompletedProcess[str]:
    """Run BODY with only the claims module sourced."""
    return subprocess.run(
        [_BASH, "-c", f"source {str(_CLAIMS)!r}\n" + textwrap.dedent(body)],
        cwd=_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "key,expected",
    [
        ("scale-test:data", "team-a\tscale-test\tdata"),
        ("team-b/scale-test:data", "team-b\tscale-test\tdata"),
        ("lustre.a:/x//y/", "team-a\tlustre.a\tx/y"),
        ("c:a:b", None),
        ("c:a/./b", None),
        ("c:a/../b", None),
        ("c:.storage-scale-test/x", None),
        ("c:/", None),
        ("Bad:x", None),
        ("Bad-NS/c:x", None),
        ("a/b/c:x", None),
        ("data", None),
    ],
)
def test_parse_test_dir_key_resolves_and_validates(key, expected):
    """The module parses keys on its own, loading the helpers lazily."""
    result = _bash(f"""
        KUBECTL_NAMESPACE=team-a
        kubectl_parse_test_dir_key {key!r}
        """)
    if expected is None:
        assert result.returncode != 0
        assert result.stdout == ""
        assert "Error:" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected + "\n"


def test_parse_test_dir_key_needs_a_namespace():
    """A key without a namespace while KUBECTL_NAMESPACE is empty is an error."""
    result = _bash("KUBECTL_NAMESPACE=\nkubectl_parse_test_dir_key scale-test:data\n")
    assert result.returncode != 0
    assert "names no namespace and KUBECTL_NAMESPACE is empty" in result.stderr
    assert "namespace/scale-test:data" in result.stderr


def _validate(settings: str) -> subprocess.CompletedProcess[str]:
    return _bash(f"""
        KUBECTL_NAMESPACE=team-a
        {settings}
        kubectl_validate_test_dirs
        """)


def test_validate_accepts_an_aggregate_in_one_namespace():
    """Several claims of one namespace make one valid aggregate target."""
    result = _validate(
        'declare -A TEST_DIRS=(["lustre-a:scale-test"]=2 ["team-a/lustre-b:scale-test"]=1)'
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_kubectl_pvc_prints_the_exact_replacement():
    """KUBECTL_PVC=scale-test with key data names the replacement line."""
    result = _validate("KUBECTL_PVC=scale-test\ndeclare -A TEST_DIRS=([data]=1)")
    assert result.returncode != 0
    assert "KUBECTL_PVC and KUBECTL_PV were removed" in result.stderr
    assert 'TEST_DIRS=(["scale-test:data"]=1)' in result.stderr.splitlines()[-1]


def test_kubectl_pv_alone_is_rejected():
    """A leftover KUBECTL_PV fails even when every key names its claim."""
    result = _validate('KUBECTL_PV=pv-1\ndeclare -A TEST_DIRS=(["scale-test:data"]=1)')
    assert result.returncode != 0
    assert "KUBECTL_PVC and KUBECTL_PV were removed" in result.stderr


def test_unqualified_keys_print_a_replacement_with_a_claim_placeholder():
    """Keys without a claim keep their weights in the suggested line."""
    result = _validate('declare -A TEST_DIRS=([/data]=2 ["c:x"]=1)')
    assert result.returncode != 0
    assert "must name their claim as [namespace/]claim:path" in result.stderr
    assert 'TEST_DIRS=(["<claim>:data"]=2 ["c:x"]=1)' in result.stderr


def test_validate_reports_every_error_not_just_the_first():
    """Bad names, bad paths and a namespace span are all reported in one run."""
    result = _validate(
        'declare -A TEST_DIRS=(["Bad:x"]=1 ["c:.."]=1 ["a:x"]=1 ["team-b/b:y"]=1)'
    )
    assert result.returncode != 0
    assert "claim Bad must be a lowercase" in result.stderr
    assert "c:.. has an invalid path" in result.stderr
    assert "span Kubernetes namespaces (team-a,team-b)" in result.stderr
    assert "pass each file with --target" in result.stderr


def test_duplicate_roots_are_rejected():
    """Two spellings of one root are the same root."""
    result = _validate('declare -A TEST_DIRS=(["c:x/"]=1 ["team-a/c:/x"]=1)')
    assert result.returncode != 0
    assert "are the same Kubernetes root team-a/c:x" in result.stderr


def test_empty_test_dirs_are_rejected():
    """At least one root is required."""
    result = _validate("declare -A TEST_DIRS=()")
    assert result.returncode != 0
    assert 'TEST_DIRS=(["scale-test:data"]=1)' in result.stderr


def test_namespace_and_claims_of_the_test_dirs():
    """The one namespace and the sorted, unique claims are printed."""
    result = _bash("""
        KUBECTL_NAMESPACE=team-a
        declare -A TEST_DIRS=(["lustre-b:x"]=1 ["lustre-a:y"]=1 ["lustre-b:z"]=1)
        kubectl_test_dirs_namespace
        kubectl_test_dirs_claims
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "team-a\nlustre-a\nlustre-b\n"


def test_namespace_of_roots_that_span_namespaces_fails():
    """A map spanning namespaces has no one namespace."""
    result = _bash("""
        KUBECTL_NAMESPACE=team-a
        declare -A TEST_DIRS=(["a:x"]=1 ["team-b/b:y"]=1)
        kubectl_test_dirs_namespace
        """)
    assert result.returncode != 0
    assert "span Kubernetes namespaces" in result.stderr


def _batch(tmp_path: Path, groups: dict[str, str]) -> Path:
    """A minimal batch: one group per GROUPS entry (gid -> TEST_DIRS body)."""
    root = tmp_path / "batch"
    (root / "executions").mkdir(parents=True)
    rows = ["revision\t1"]
    execution = 0
    for gid, test_dirs in groups.items():
        relative = f"groups/{gid}/elbencho-20260101Z000000"
        (root / relative).mkdir(parents=True)
        (root / relative / "env_used.sh").write_text(
            f"export KUBECTL_NAMESPACE=team-a\ndeclare -gA TEST_DIRS=({test_dirs})\n",
            encoding="utf-8",
        )
        rows.append(f"group\t{gid}\tio\t{relative}")
        execution += 1
        identifier = f"{execution:04d}"
        keys = [part.split("]")[0].strip('["') for part in test_dirs.split("[")[1:]]
        generated = ",".join(
            f"{key}/elbencho-sweep-target-1-e{identifier}" for key in keys
        )
        (root / "executions" / f"{identifier}.sh").write_text(
            f"export ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV={generated}\n",
            encoding="utf-8",
        )
        (root / "executions" / f"{identifier}.status").write_text(
            "PENDING\n", encoding="utf-8"
        )
        rows.append(f"execution\t{identifier}\t{gid}\tdigest")
    (root / "batch-manifest.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return root


def _preflight(
    root: Path,
    union: str,
    nodes: int = 2,
    maximum: int = 2,
    settings: str = "",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run kubectl_batch_preflight ROOT with stubbed cluster calls."""
    log = root.parent / "calls.log"
    result = _bash(f"""
        source "$(dirname {str(_CLAIMS)!r})/../storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
        export KUBECTL_NAMESPACE=team-a KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=registry.example/elbencho:1
        export KUBECTL_IMAGE_PULL_POLICY=IfNotPresent KUBECTL_RUN_AS_USER=2000
        export KUBECTL_RUN_AS_GROUP=2000 KUBECTL_SEQUENCER_IMAGE=
        declare -gA TEST_DIRS=({union})
        {settings}
        elbencho_batch_verify_manifest() {{ :; }}
        max_nodes_remaining_executions() {{ printf '{maximum}\\n'; }}
        kubectl_validate_cluster_identity() {{
            local namespace="$1"
            shift
            printf 'IDENTITY %s %s\\n' "$namespace" "$*" >> {str(log)!r}
            [[ "$namespace" != bad-* ]] || {{ echo "Error: cannot read" >&2; return 1; }}
        }}
        kubectl_discover_candidate_nodes() {{
            echo DISCOVERY >> {str(log)!r}
            local index
            for ((index = 0; index < {nodes}; index++)); do
                printf 'node-%s\\tuid-%s\\tamd64\\n' "$index" "$index"
            done > "$2"
        }}
        kubectl_batch_preflight {str(root)!r}
        """)
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


_UNION_TWO_NAMESPACES = (
    '["team-a/lustre-a:x"]=1 ["team-a/lustre-b:y"]=1 ["team-b/vendor-b:z"]=1'
)


def test_kubectl_batch_preflight_checks_every_namespace_and_the_nodes(tmp_path):
    """One namespace with an aggregate needs no sequencer image."""
    root = _batch(
        tmp_path,
        {"0001": '["lustre-a:x"]=2 ["lustre-b:y"]=1', "0002": '["lustre-b:y"]=1'},
    )
    result, calls = _preflight(root, '["team-a/lustre-a:x"]=1 ["team-a/lustre-b:y"]=1')
    assert result.returncode == 0, result.stderr
    assert calls == ["IDENTITY team-a lustre-a lustre-b", "DISCOVERY"]
    assert not list(root.glob(".preflight.*"))


def test_kubectl_batch_preflight_checks_the_sequencer_image_across_namespaces(
    tmp_path,
):
    """Segments spanning namespaces run the default image or a pinned override."""
    groups = {
        "0001": '["lustre-a:x"]=1 ["lustre-b:y"]=1',
        "0002": '["team-b/vendor-b:z"]=1',
    }
    root = _batch(tmp_path, groups)
    result, calls = _preflight(root, _UNION_TWO_NAMESPACES)
    assert result.returncode == 0, result.stderr
    assert calls[-1] == "DISCOVERY"
    (tmp_path / "calls.log").unlink()
    result, calls = _preflight(
        root,
        _UNION_TWO_NAMESPACES,
        settings="export KUBECTL_SEQUENCER_IMAGE=registry.example/kubectl:1.31",
    )
    assert result.returncode != 0
    assert "must be a digest-qualified container image reference" in result.stderr
    assert not calls
    result, calls = _preflight(
        root,
        _UNION_TWO_NAMESPACES,
        settings=f"export KUBECTL_SEQUENCER_IMAGE={_SEQUENCER_IMAGE}",
    )
    assert result.returncode == 0, result.stderr
    assert calls[-1] == "DISCOVERY"


def test_kubectl_batch_preflight_names_a_failing_namespace(tmp_path):
    """Every namespace is checked; the failing one is named."""
    root = _batch(tmp_path, {"0001": '["lustre-a:x"]=1', "0002": '["bad-b/c:z"]=1'})
    result, calls = _preflight(
        root,
        '["team-a/lustre-a:x"]=1 ["bad-b/c:z"]=1',
        settings=f"export KUBECTL_SEQUENCER_IMAGE={_SEQUENCER_IMAGE}",
    )
    assert result.returncode != 0
    assert "Kubernetes namespace bad-b (claims c) failed validation" in result.stderr
    assert calls == ["IDENTITY team-a lustre-a", "IDENTITY bad-b c"]


def test_kubectl_batch_preflight_rejects_a_group_without_qualified_keys(tmp_path):
    """A group whose roots name no claim stops preflight before the cluster."""
    root = _batch(tmp_path, {"0001": '["lustre-a:x"]=1', "0002": '["/data"]=1'})
    result, calls = _preflight(root, '["team-a/lustre-a:x"]=1')
    assert result.returncode != 0
    assert "batch group 0002 has no valid claim-qualified TEST_DIRS" in result.stderr
    assert not calls


def test_kubectl_batch_preflight_checks_the_node_count(tmp_path):
    """The selector must satisfy the largest remaining execution."""
    root = _batch(tmp_path, {"0001": '["lustre-a:x"]=1'})
    result, calls = _preflight(root, '["team-a/lustre-a:x"]=1', nodes=1, maximum=3)
    assert result.returncode != 0
    assert "worker selector cannot satisfy 3 nodes" in result.stderr
    assert calls[-1] == "DISCOVERY"


def test_kubectl_batch_preflight_rejects_a_changed_control_root(tmp_path):
    """A sealed batch keeps its canonical control root; discovery never runs."""
    root = _batch(tmp_path, {"0001": '["lustre-a:x"]=1 ["lustre-b:y"]=1'})
    (root / "batch-sealed.sha256").write_text("digest\n", encoding="utf-8")
    result, calls = _preflight(
        root,
        '["team-a/lustre-a:x"]=1 ["team-a/lustre-b:y"]=1',
        settings=textwrap.dedent("""\
            export KUBECTL_CONTROL_LOGICAL_ROOT=team-a/lustre-b:y
            export KUBECTL_CONTROL_TEST_ROOT=/mnt/storage-scale-test/lustre-b/y
            export KUBECTL_CONTROL_ROOT=/mnt/storage-scale-test/lustre-b/y/.storage-scale-test
            """),
    )
    assert result.returncode != 0
    assert "canonical control root differs from its saved union" in result.stderr
    assert not calls


def test_kubectl_batch_preflight_checks_only_the_namespaces_still_to_run(tmp_path):
    """[R-26] Resuming a sealed batch plans the executions that have not succeeded,
    as the resume submits them: a namespace whose cells all succeeded is not
    checked (its claims may be gone), and one namespace left needs no
    sequencer image."""
    root = _batch(tmp_path, {"0001": '["lustre-a:x"]=1', "0002": '["bad-b/c:z"]=1'})
    union = '["team-a/lustre-a:x"]=1 ["bad-b/c:z"]=1'
    result, calls = _preflight(root, union)
    assert result.returncode != 0
    (tmp_path / "calls.log").unlink()
    (root / "executions" / "0002.status").write_text("SUCCESS\n", encoding="utf-8")
    result, calls = _preflight(root, union)
    assert result.returncode == 0, result.stderr
    assert calls == ["IDENTITY team-a lustre-a", "DISCOVERY"]
    (tmp_path / "calls.log").unlink()
    # A failed cell still runs again, so its namespace is checked.
    (root / "executions" / "0002.status").write_text("FAILED\n", encoding="utf-8")
    (root / "executions" / "0001.status").write_text("SUCCESS\n", encoding="utf-8")
    result, calls = _preflight(root, union)
    assert result.returncode != 0
    assert "Kubernetes namespace bad-b (claims c) failed validation" in result.stderr
    assert calls == ["IDENTITY bad-b c"]


def _claims(first: int, count: int) -> str:
    return " ".join(f'["claim-{index:02d}:x"]=1' for index in range(first, count))


def test_validate_rejects_more_claims_than_one_attempt_mounts():
    """[S-32] More than KUBECTL_MAX_CLAIMS claims are named with the limit."""
    result = _validate(f"declare -A TEST_DIRS=({_claims(0, 32)})")
    assert result.returncode == 0, result.stderr
    result = _validate(f"declare -A TEST_DIRS=({_claims(0, 33)})")
    assert result.returncode != 0
    assert (
        "TEST_DIRS uses 33 Kubernetes claims, but one attempt mounts at most 32"
        in result.stderr
    )


def test_kubectl_batch_preflight_rejects_more_claims_than_one_attempt_mounts(
    tmp_path,
):
    """[S-32] The groups the next attempt runs may not exceed the claim limit
    together, though each fits on its own; nothing reaches the cluster."""
    root = _batch(tmp_path, {"0001": _claims(0, 20), "0002": _claims(15, 33)})
    union = " ".join(f'["team-a/claim-{index:02d}:x"]=1' for index in range(0, 33))
    result, calls = _preflight(root, union)
    assert result.returncode != 0
    assert (
        "the next attempt uses 33 Kubernetes claims, but one attempt mounts at "
        "most 32" in result.stderr
    )
    assert not calls
