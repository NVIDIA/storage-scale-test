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

"""Storage target identity, labelling, and the mixed-target guard."""

import json
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from lib.storage_target import (
    ALLOW_MIXED_TARGETS_FLAG,
    UNKNOWN_TARGET,
    MixedStorageTargetsError,
    StorageTarget,
    default_storage_target_name,
    describe_storage_target,
    guarded_report_target,
    mixed_target_label,
    resolve_report_target,
    storage_target_from_env,
)

ROOT = Path(__file__).resolve().parents[1]
KUBECTL_ENV = {
    "EXECUTION_SUBSTRATE": "kubectl",
    "KUBECTL_NAMESPACE": "team-a",
    "KUBECTL_PVC": "claim-1",
}


# A v2.0.0 snapshot: claim-qualified TEST_DIRS keys and no KUBECTL_PVC.
QUALIFIED_ENV = {"EXECUTION_SUBSTRATE": "kubectl", "KUBECTL_NAMESPACE": "team-a"}


def test_kubectl_default_normalizes_claim_qualified_roots():
    """A key without a namespace takes KUBECTL_NAMESPACE; paths are normalized."""
    env = {
        **QUALIFIED_ENV,
        "TEST_DIRS": {"lustre-b:scale/": 1, "team-b/lustre-a:/x//y": 2},
    }
    assert (
        default_storage_target_name(env) == "team-a/lustre-b:scale,team-b/lustre-a:x/y"
    )


def test_explicit_name_wins_and_is_recorded():
    env = {**KUBECTL_ENV, "STORAGE_TARGET_NAME": "lustre-a", "TEST_DIRS": {"/x": 1}}
    assert storage_target_from_env(env) == StorageTarget("lustre-a", True)


def test_refusal_marks_names_derived_from_old_results():
    """Refusing derived names says where they came from."""
    with pytest.raises(MixedStorageTargetsError) as refused:
        resolve_report_target(
            [("a", StorageTarget("/mnt/a", False)), ("b", StorageTarget("x", True))],
            allow_mixed=False,
            warn=lambda _message: None,
        )
    message = str(refused.value)
    assert '"/mnt/a" (derived): a' in message
    assert '"x": b' in message
    assert "results saved before v2.0.0" in message


@pytest.mark.parametrize(
    "env",
    [
        {**KUBECTL_ENV, "KUBECTL_PVC": ""},
        {**KUBECTL_ENV, "KUBECTL_NAMESPACE": None},
        {**KUBECTL_ENV, "EXECUTION_SUBSTRATE": "ssh"},
    ],
)
def test_incomplete_kubectl_identity_falls_back_to_roots(env):
    env["TEST_DIRS"] = {"/mnt/a/": 1}
    assert storage_target_from_env(env) == StorageTarget("/mnt/a", False)


def test_roots_are_stripped_deduplicated_and_sorted_by_bytes():
    roots = {"/z/": 1, "/a": 1, "/a/": 2, "/é": 1, "/": 1, "//": 1, "/B": 1}
    assert default_storage_target_name({"TEST_DIRS": roots}) == "/,/B,/a,/z,/é"


@pytest.mark.parametrize(
    "env",
    [None, {}, {"TEST_DIRS": {}}, {"TEST_DIRS": None}, {"TEST_DIRS": "/a"}, [], "x"],
)
def test_missing_identity_is_unknown(env):
    assert storage_target_from_env(env) == StorageTarget(None, False)
    assert default_storage_target_name(env) is None


def test_describe_marks_derived_and_unknown():
    assert describe_storage_target(StorageTarget("a", True)) == "a"
    assert describe_storage_target(StorageTarget("a", False)) == "a (derived)"
    assert describe_storage_target(StorageTarget(None, False)) == UNKNOWN_TARGET


def _resolve(sources, allow_mixed=False):
    warnings = []
    label = resolve_report_target(sources, allow_mixed, warnings.append)
    return label, warnings


def test_guard_single_name_is_silent():
    label, warnings = _resolve(
        [("d1", StorageTarget("a", True)), ("d2", "a"), ("d3", StorageTarget("a"))]
    )
    assert (label, warnings) == ("a", [])


@pytest.mark.parametrize("unknown", [None, "", StorageTarget(None, False)])
def test_guard_all_unknown_is_silent(unknown):
    assert _resolve([("d1", unknown), ("d2", None)]) == (UNKNOWN_TARGET, [])
    assert _resolve([]) == (UNKNOWN_TARGET, [])


def test_guard_known_with_unknown_warns_and_keeps_name():
    label, warnings = _resolve([("d1", "a"), ("d2", None), ("d3", "")])
    assert label == "a"
    assert warnings == ["cannot verify storage target of 2 input(s): d2, d3"]


def test_guard_mixed_names_raise_listing_names_and_sources():
    with pytest.raises(MixedStorageTargetsError) as raised:
        resolve_report_target(
            [("d1", "b"), ("d2", "a"), ("d3", "b"), ("d4", None)], False, print
        )
    message = str(raised.value)
    assert '"a": d2' in message and '"b": d1, d3' in message
    assert "report each target separately" in message
    assert ALLOW_MIXED_TARGETS_FLAG in message
    assert "d4" not in message


def test_guard_mixed_names_allowed_warn_and_label():
    label, warnings = _resolve([("d1", "b"), ("d2", "a"), ("d3", None)], True)
    assert label == 'mixed: "a", "b"'
    assert len(warnings) == 2
    assert 'mixes storage targets: "a" (d2), "b" (d1)' in warnings[0]
    assert "cannot verify storage target of 1 input(s): d3" in warnings[1]


def test_guarded_report_target_exits_one_with_error(capsys):
    with pytest.raises(SystemExit) as raised:
        guarded_report_target([("d1", "a"), ("d2", "b")], False)
    assert raised.value.code == 1
    assert capsys.readouterr().err.startswith("ERROR: inputs come from different")
    assert guarded_report_target([("d1", "a"), ("d2", "b")], True) == 'mixed: "a", "b"'
    assert "WARNING:" in capsys.readouterr().err


AWKWARD_NAMES = ['a"b', "back\\slash", "line\nbreak", 'x", "y', "comma, space", "é中"]


@pytest.mark.parametrize("name", AWKWARD_NAMES)
def test_mixed_label_quotes_any_name_as_a_json_string(name):
    label, _ = _resolve([("d1", name), ("d2", "plain")], True)
    names = sorted([name, "plain"], key=lambda item: item.encode("utf-8"))
    assert label == mixed_target_label(names)
    assert label == "mixed: " + ", ".join(
        json.dumps(n, ensure_ascii=False) for n in names
    )
    assert "\n" not in label


def test_name_with_a_quote_is_not_split_by_the_guard():
    quoted = 'x", "y'
    label, _ = _resolve([("d1", quoted), ("d2", "z")], True)
    assert label == 'mixed: "x\\", \\"y", "z"'
    with pytest.raises(MixedStorageTargetsError) as raised:
        resolve_report_target([("d1", quoted), ("d2", "z")], False, print)
    assert '"x\\", \\"y": d1' in str(raised.value)


def test_current_snapshots_record_the_name_and_display_it_bare():
    # The shell stores the derived default in STORAGE_TARGET_NAME, so a current
    # snapshot is "recorded"; only a snapshot lacking the name is "(derived)".
    current = {"STORAGE_TARGET_NAME": "/mnt/a,/mnt/b", "TEST_DIRS": {"/mnt/a": 1}}
    assert storage_target_from_env(current) == StorageTarget("/mnt/a,/mnt/b", True)
    assert describe_storage_target(storage_target_from_env(current)) == "/mnt/a,/mnt/b"
    old = {"TEST_DIRS": {"/mnt/a": 1, "/mnt/b": 1}}
    assert describe_storage_target(storage_target_from_env(old)) == (
        "/mnt/a,/mnt/b (derived)"
    )


def _bash_default(env, roots):
    """Evaluate the shell implementation of the identity rule's defaults."""
    declaration = " ".join(f"[{shlex.quote(root)}]=1" for root in roots)
    script = (
        f'source "{ROOT}/lib/env_functions.sh"\n'
        f"declare -A TEST_DIRS=({declaration})\n"
        "storage_target_name_default\n"
    )
    completed = subprocess.run(
        ["bash", "-c", script],
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "LC_ALL": "en_US.UTF-8", **env},
        capture_output=True,
        check=False,
    )
    output = completed.stdout.decode("utf-8")
    return completed.returncode, output


PARITY_CASES = {
    "one-root": ({}, ["/mnt/data"]),
    "several-unsorted": ({}, ["/zeta", "/Alpha", "/mnt/b", "/mnt/a", "/alpha"]),
    "trailing-slash": ({}, ["/mnt/data/", "/mnt/data", "/mnt/other//"]),
    "slash-root": ({}, ["/", "/mnt"]),
    "utf8": ({}, ["/mnt/é", "/mnt/z", "/mnt/中", "/mnt/Z"]),
    "space": ({}, ["/mnt/my data", "/mnt/a"]),
    "newline-key": ({}, ["/mnt/a\nb", "/mnt/a", "/mnt/a\nb/"]),
    "trailing-newline-key": ({}, ["/mnt/a\n", "/mnt/a"]),
    "glob-characters": ({}, ["/mnt/*", "/mnt/a?", "/mnt/[ab]/", "/mnt/{x,y}", "/m*/"]),
    "slashes-only-roots": ({}, ["//", "///"]),
    "slashes-only-and-real-root": ({}, ["//", "/mnt/a//"]),
    "backslashes": ({}, ["/mnt/a\\b", "/mnt/a\\", "/mnt/\\n/", "/mnt/a\\\\"]),
    "quotes-and-dollars": ({}, ['/mnt/"q"', "/mnt/$HOME", "/mnt/`id`", "/mnt/it's"]),
    "kubectl-unqualified-keys": (
        QUALIFIED_ENV,
        ["/mnt/b/", "/mnt/a"],
    ),
    "kubectl-claim-qualified": (
        QUALIFIED_ENV,
        ["lustre-b:scale/", "team-b/lustre-a:x//y", "lustre-a:/scale-test"],
    ),
    "kubectl-one-invalid-key": (
        QUALIFIED_ENV,
        ["Bad:x", "lustre-a:y"],
    ),
    "kubectl-reserved-component": (
        QUALIFIED_ENV,
        ["lustre-a:.storage-scale-test/x", "lustre-a:y"],
    ),
    "kubectl-no-default-namespace": (
        {**QUALIFIED_ENV, "KUBECTL_NAMESPACE": ""},
        ["lustre-a:y", "team-b/lustre-b:z"],
    ),
}


@pytest.mark.parametrize("case", sorted(PARITY_CASES))
def test_bash_and_python_agree_on_the_default_name(case):
    env, roots = PARITY_CASES[case]
    returncode, output = _bash_default(env, roots)
    python_env = {
        **env,
        "TEST_DIRS": {root: 1 for root in roots},
    }
    expected = default_storage_target_name(python_env)
    assert returncode == 0
    assert output == f"{expected}\n"


def test_bash_and_python_agree_when_no_default_exists():
    returncode, output = _bash_default({}, [])
    assert (returncode, output) == (1, "")
    assert default_storage_target_name({"TEST_DIRS": {}}) is None


def test_identity_and_batch_modules_import_without_third_party_packages():
    """--help of the reporters must work before PyYAML is installed."""
    script = (
        "import sys\n"
        "sys.modules['yaml'] = None\n"
        "import lib.storage_target, lib.filesystem_snapshot_summary\n"
        "import lib.filesystem_batch, lib.filesystem_report_options\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
