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

"""Normative state-graph tests for the Kubernetes sweep lifecycle."""

import ast
from itertools import product
from pathlib import Path
import re
import shutil
import subprocess

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_FUNCTIONS = _ROOT / "storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
_CONTRACT = _ROOT / "docs/KUBERNETES_ELBENCHO_LIFECYCLE.md"
_BASH = shutil.which("bash") or "/bin/bash"

_STATES = (
    "PREPARED",
    "SUBMITTED",
    "CANCEL_REQUESTED",
    "TERMINAL",
    "COLLECTION_IN_PROGRESS",
    "COLLECTED",
    "SUBMISSION_FAILED",
)
_LEGAL_TRANSITIONS = {
    ("PREPARED", "SUBMITTED"),
    ("PREPARED", "SUBMISSION_FAILED"),
    ("SUBMITTED", "CANCEL_REQUESTED"),
    ("SUBMITTED", "TERMINAL"),
    ("CANCEL_REQUESTED", "TERMINAL"),
    ("TERMINAL", "COLLECTION_IN_PROGRESS"),
    ("COLLECTION_IN_PROGRESS", "COLLECTED"),
}


def test_s04_local_lifecycle_lock_rejects_a_concurrent_mutator(
    tmp_path: Path,
) -> None:
    """[S-04] Only one local lifecycle mutator can cross its lock boundary."""
    ready = tmp_path / "ready"
    result = subprocess.run(
        [
            _BASH,
            "-c",
            (
                f"source {_FUNCTIONS!s}\n"
                "root=$1; ready=$2\n"
                '( kubectl_local_lock_acquire "$root" holder; '
                '  : > "$ready"; sleep 2; '
                '  kubectl_local_lock_release "$holder" ) &\n'
                "pid=$!\n"
                'for _ in {1..50}; do [[ -e "$ready" ]] && break; sleep .02; done\n'
                '[[ -e "$ready" ]]\n'
                '! kubectl_local_lock_acquire "$root" contender 2>"$root/error"\n'
                'grep -F "another Kubernetes lifecycle operation is active" "$root/error"\n'
                'grep -F "Wait for that operation to finish" "$root/error"\n'
                'wait "$pid"\n'
            ),
            str(tmp_path / "state"),
            str(ready),
        ],
        cwd=_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_every_covered_fault_id_has_existing_named_evidence() -> None:
    """The frozen matrix cannot claim coverage without resolvable test evidence."""
    document = _CONTRACT.read_text(encoding="utf-8")
    matrix = document.split("## Fault matrix", maxsplit=1)[1].split(
        "### Regression evidence map", maxsplit=1
    )[0]
    rows = re.findall(
        r"^\|\s*([SCTR]-\d{2})\s*\|.*\|\s*([^|]+?)\s*\|$",
        matrix,
        flags=re.MULTILINE,
    )
    identifiers = [identifier for identifier, _ in rows]
    assert identifiers
    assert len(identifiers) == len(set(identifiers))
    covered = {
        identifier for identifier, status in rows if "covered" in status.casefold()
    }

    evidence = document.split("### Regression evidence map", maxsplit=1)[1].split(
        "## Diagnostic contract", maxsplit=1
    )[0]
    mapped: dict[str, set[str]] = {}
    for line in evidence.splitlines():
        if not line.startswith("|") or "test_" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        fault_ids = re.findall(r"[SCTR]-\d{2}", cells[0])
        tests = set(re.findall(r"`(test_[a-z0-9_]+)`", cells[1]))
        for fault_id in fault_ids:
            mapped.setdefault(fault_id, set()).update(tests)

    existing_tests: dict[str, str] = {}
    for path in (_ROOT / "tests").glob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and node.name.startswith("test_"):
                existing_tests[node.name] = ast.get_docstring(node) or ""
    assert covered == set(mapped), (
        f"missing evidence={sorted(covered - set(mapped))}; "
        f"non-covered evidence={sorted(set(mapped) - covered)}"
    )
    for fault_id, test_names in mapped.items():
        assert test_names, f"{fault_id} has no focused test"
        assert test_names <= set(existing_tests), (
            f"{fault_id} references absent tests: "
            f"{sorted(test_names - set(existing_tests))}"
        )
        tagged = {
            name for name in test_names if f"[{fault_id}]" in existing_tests[name]
        }
        assert tagged, (
            f"{fault_id} has no mapped focused test whose docstring explicitly "
            "references that boundary"
        )


@pytest.mark.parametrize("initial", _STATES)
def test_new_attempt_has_exactly_one_legal_initial_state(
    tmp_path: Path, initial: str
) -> None:
    """The implicit new vertex has only the documented edge to PREPARED."""
    state_root = tmp_path / initial.lower()
    result = subprocess.run(
        [
            _BASH,
            "-c",
            (
                f"source {_FUNCTIONS!s}\n"
                "root=$1\n"
                "initial=$2\n"
                'kubectl_local_lock_acquire "$root" fd\n'
                'kubectl_attempt_create_identity "$root" "$fd" 1234abcd '
                "0123456789abcdef0123456789abcdef test-ns namespace-uid "
                "test-pv pv-uid test-pvc pvc-uid\n"
                'kubectl_attempt_write_state "$root" "$fd" 1234abcd "$initial"'
            ),
            "lifecycle-contract",
            str(state_root),
            initial,
        ],
        cwd=_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is (initial == "PREPARED"), (
        f"unexpected initial lifecycle edge new -> {initial}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@pytest.mark.parametrize(("previous", "next_state"), list(product(_STATES, repeat=2)))
def test_exact_local_attempt_transition_graph(
    tmp_path: Path, previous: str, next_state: str
) -> None:
    """Every documented local edge succeeds and every other state pair fails."""
    paths = {
        "PREPARED": (),
        "SUBMITTED": ("SUBMITTED",),
        "CANCEL_REQUESTED": ("SUBMITTED", "CANCEL_REQUESTED"),
        "TERMINAL": ("SUBMITTED", "TERMINAL"),
        "COLLECTION_IN_PROGRESS": (
            "SUBMITTED",
            "TERMINAL",
            "COLLECTION_IN_PROGRESS",
        ),
        "COLLECTED": (
            "SUBMITTED",
            "TERMINAL",
            "COLLECTION_IN_PROGRESS",
            "COLLECTED",
        ),
        "SUBMISSION_FAILED": ("SUBMISSION_FAILED",),
    }
    setup_transitions = "\n".join(
        f'kubectl_attempt_transition "$root" "$fd" 1234abcd {state}'
        for state in paths[previous]
    )
    result = subprocess.run(
        [
            _BASH,
            "-c",
            (
                f"source {_FUNCTIONS!s}\n"
                'root="$1"\n'
                'kubectl_local_lock_acquire "$root" fd\n'
                'kubectl_attempt_create_identity "$root" "$fd" 1234abcd '
                "0123456789abcdef0123456789abcdef test-ns namespace-uid "
                "test-pv pv-uid test-pvc pvc-uid\n"
                'kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED\n'
                f"{setup_transitions}\n"
                'kubectl_attempt_transition "$root" "$fd" 1234abcd "$2"\n'
                "transition_rc=$?\n"
                'kubectl_attempt_load_metadata "$root/attempts/1234abcd"\n'
                'printf "%s\\t%s\\n" "$transition_rc" '
                '"$KUBECTL_LIFECYCLE_STATE"\n'
                'kubectl_local_lock_release "$fd"\n'
            ),
            "lifecycle-contract",
            str(tmp_path / f"{previous}-{next_state}"),
            next_state,
        ],
        cwd=_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    expected = (previous, next_state) in _LEGAL_TRANSITIONS
    expected_state = next_state if expected else previous
    assert result.returncode == 0
    assert result.stdout.strip() == f"{0 if expected else 1}\t{expected_state}", (
        f"unexpected lifecycle edge result for {previous} -> {next_state}: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


@pytest.mark.parametrize("unknown", ("", "RUNNING", "SUCCESS", "FAILED", "UNKNOWN"))
def test_nonlocal_or_unknown_states_are_not_local_transition_vertices(
    unknown: str,
) -> None:
    """PVC-run outcomes and invented states cannot enter the local graph."""
    for known in _STATES:
        for previous, next_state in ((unknown, known), (known, unknown)):
            result = subprocess.run(
                [
                    _BASH,
                    "-c",
                    (
                        f"source {_FUNCTIONS!s}\n"
                        '! kubectl_transition_is_valid "$1" "$2"'
                    ),
                    "lifecycle-contract",
                    previous,
                    next_state,
                ],
                cwd=_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            assert result.returncode == 0, (
                f"unexpected graph vertex {previous!r} -> {next_state!r}: "
                f"stdout={result.stdout!r} stderr={result.stderr!r}"
            )
