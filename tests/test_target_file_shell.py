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

"""--target files: what to test, kept apart from how to test."""

from pathlib import Path
import shlex
import subprocess

import pytest
import yaml

from tests.filesystem_checkout_test_support import make_filesystem_checkout
from tests.test_env_override_shell import launch

ROOT = Path(__file__).resolve().parents[1]
IO = "nv-elbencho-sweep.sh"
MD = "nv-mdtest-elbencho.sh"
TARGET_FLAG = "--target"
ARGS_BY_ENTRY = {IO: ("--nodes", "1"), MD: ("--nodes", "1", "--tasks", "1")}
RESULT_GLOB = {IO: "elbencho-*", MD: "mdtest-elbencho-*"}
ROOTS_ONE = 'TEST_DIRS=(["/data/two"]=1)\n'
RULE = (
    "What to test comes from env.sh or --target; "
    "how to test comes from env.sh or --env-override."
)


@pytest.fixture(name="checkout")
def checkout_fixture(tmp_path):
    """Real launchers and libraries; unreachable hosts stop a run after reify."""
    base = make_filesystem_checkout(tmp_path, ROOT)
    (base / "shim").mkdir()
    for tool in ("ssh", "scp"):
        (base / "shim" / tool).write_text("#!/bin/sh\nexit 255\n")
        (base / "shim" / tool).chmod(0o755)
    (base / "targets").mkdir()
    return base


def write_target(checkout, name, content):
    """Create one target file beneath the checkout."""
    path = checkout / "targets" / name
    path.write_text(content)
    return path


def declarations(checkout, target, tail="", env_lines=""):
    """Apply a target file with the library loader, then run TAIL."""
    return subprocess.run(
        [
            "bash",
            "-ceu",
            'source "$1/env.sh"; source "$1/lib/_elbencho_functions.sh"; '
            + env_lines
            + f"d=$(filesystem_target_declarations {shlex.quote(str(target))} "
            'lbl) || exit 1; eval "$d"; ' + tail,
            "target-test",
            str(checkout),
        ],
        cwd=checkout,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_target_replaces_roots_whole_and_clears_the_name(checkout):
    with (checkout / "env.sh").open("a") as stream:
        stream.write('\nTEST_DIRS=(["/data/one"]=1 ["/data/extra"]=2)\n')
        stream.write("export STORAGE_TARGET_NAME=from-env FS_MAX_NODE_IOPS=4242\n")
    target = write_target(checkout, "t.env", ROOTS_ONE)
    result = declarations(
        checkout,
        target,
        'echo "${!TEST_DIRS[*]}|$STORAGE_TARGET_NAME|$FS_MAX_NODE_IOPS|$FS_ENABLED"; '
        'echo "$STORAGE_SCALE_TEST_TARGET_FILE|$STORAGE_SCALE_TEST_TARGET_FILE_VARIABLES"',
    )
    assert result.returncode == 0, result.stderr
    first, second = result.stdout.splitlines()
    # The roots are replaced whole, the env.sh name is gone, and an FS_MAX_*
    # the file does not set keeps env.sh's value.
    assert first == "/data/two||4242|1"
    assert second == "lbl|TEST_DIRS"


def test_target_may_set_its_name_and_hints(checkout):
    target = write_target(
        checkout,
        "full.env",
        ROOTS_ONE + "STORAGE_TARGET_NAME=vendor-a\nFS_MAX_AGG_THROUGHPUT=33\n"
        "FS_MAX_NODE_THROUGHPUT_GBPS=77\nFS_MAX_NODE_IOPS=99\n",
    )
    result = declarations(
        checkout,
        target,
        'echo "$STORAGE_TARGET_NAME $FS_MAX_AGG_THROUGHPUT '
        "$FS_MAX_NODE_THROUGHPUT_GBPS $FS_MAX_NODE_IOPS "
        '$STORAGE_SCALE_TEST_TARGET_FILE_VARIABLES"',
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        "vendor-a",
        "33",
        "77",
        "99",
        "FS_MAX_AGG_THROUGHPUT",
        "FS_MAX_NODE_IOPS",
        "FS_MAX_NODE_THROUGHPUT_GBPS",
        "STORAGE_TARGET_NAME",
        "TEST_DIRS",
    ]


def test_target_sees_env_sh_settings_and_discards_scratch_variables(checkout):
    target = write_target(
        checkout,
        "scratch.env",
        'for n in 3; do TEST_DIRS=(["/data/n$n"]=1); done\n'
        "FS_MAX_NODE_IOPS=$((FS_MAX_NODE_IOPS + 1))\n",
    )
    result = declarations(
        checkout,
        target,
        'echo "${!TEST_DIRS[*]} $FS_MAX_NODE_IOPS ${n:-unset}"',
        env_lines="export FS_MAX_NODE_IOPS=100; ",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["/data/n3", "101", "unset"]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("STORAGE_TARGET_NAME=x\n", "must set TEST_DIRS with at least one root"),
        ("TEST_DIRS=()\n", "must set TEST_DIRS with at least one root"),
        ("FS_MAX_NODE_IOPS=5\n", "must set TEST_DIRS"),
        ("unset TEST_DIRS\ndeclare -a TEST_DIRS=(x)\n", "must be an associative"),
        (ROOTS_ONE + "FS_MAX_NODE_IOPS=(1)\n", "FS_MAX_NODE_IOPS must be a scalar"),
        (ROOTS_ONE + "exit 0\n", "exited early"),
        (ROOTS_ONE + "false\n", "target file returned status 1"),
        (ROOTS_ONE + "TEST_DIR=/data/two\n", "Use TEST_DIRS"),
        (ROOTS_ONE + "PATH=/nowhere\n", "unsupported variable(s): PATH"),
        (ROOTS_ONE + "EXECUTION_SUBSTRATE=slurm\n", "unsupported variable(s)"),
    ],
)
def test_invalid_target_files_are_rejected(checkout, content, message):
    result = declarations(checkout, write_target(checkout, "bad.env", content))
    assert result.returncode == 1
    assert message in result.stderr


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("unset TEST_DIRS\n", "must set TEST_DIRS with at least one root"),
        ("TEST_DIRS=/mnt/foo\n", 'weight for "0" must be a positive integer'),
        ('TEST_DIRS=("/mnt/foo")\n', "must be a positive integer"),
        ('TEST_DIRS=(["/mnt/foo"]=)\n', 'weight for "/mnt/foo" must be a positive'),
        ('TEST_DIRS=(["/mnt/foo"]=0)\n', 'got "0"'),
        ('TEST_DIRS=(["/mnt/foo"]=-2)\n', 'got "-2"'),
        ('TEST_DIRS=(["/mnt/foo"]=1.5)\n', 'got "1.5"'),
        ('TEST_DIRS=(["/mnt/foo"]=x)\n', 'got "x"'),
        ('TEST_DIRS=(["mnt/foo"]=1)\n', 'root "mnt/foo" must be an absolute path'),
        ('TEST_DIRS=(["/mnt/a"]=1 ["rel"]=2)\n', 'root "rel" must be an absolute'),
    ],
)
def test_malformed_roots_in_a_target_file_name_the_file(checkout, content, message):
    result = declarations(checkout, write_target(checkout, "roots.env", content))
    assert result.returncode == 1, result.stdout
    assert message in result.stderr
    assert "Error: target file lbl: " in result.stderr
    assert "env.sh" not in result.stderr


def test_a_target_file_accepts_several_weighted_absolute_roots(checkout):
    target = write_target(
        checkout, "ok.env", 'TEST_DIRS=(["/mnt/a"]=2 ["/mnt/b c"]=10)\n'
    )
    result = declarations(checkout, target, 'echo "${#TEST_DIRS[@]}"')
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[-1] == "2"


def test_kubectl_target_roots_need_only_be_non_empty(checkout):
    """Claim-qualified keys are checked later by kubectl_validate_test_dirs."""
    target = write_target(
        checkout, "claim.env", 'TEST_DIRS=(["ns/claim-a:data"]=1 ["claim-b:"]=1)\n'
    )
    result = declarations(
        checkout,
        target,
        'echo "${#TEST_DIRS[@]}"',
        env_lines="EXECUTION_SUBSTRATE=kubectl; ",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[-1] == "2"
    empty = write_target(checkout, "empty.env", 'TEST_DIRS=([""]=1)\n')
    result = declarations(checkout, empty, env_lines="EXECUTION_SUBSTRATE=kubectl; ")
    # Bash 5.3 refuses an empty key itself; the loader's check covers older ones.
    assert result.returncode == 1
    assert "empty root" in result.stderr or "bad array subscript" in result.stderr


@pytest.mark.parametrize(
    ("setting", "hint"),
    [
        ("KUBECTL_PVC=claim", "KUBECTL_PVC was removed in v2.0.0; name the claim"),
        ("KUBECTL_PV=pv", "KUBECTL_PV was removed in v2.0.0; name the claim"),
        ("KUBECTL_NAMESPACE=ns", "Name the namespace in each TEST_DIRS key instead"),
    ],
)
def test_kubernetes_claim_settings_in_a_target_file_point_at_test_dirs_keys(
    checkout, setting, hint
):
    """The Kubernetes claim settings are not told they belong in env.sh."""
    target = write_target(
        checkout, "k8s.env", 'TEST_DIRS=(["claim:data"]=1)\n' + setting + "\n"
    )
    result = declarations(checkout, target, env_lines="EXECUTION_SUBSTRATE=kubectl; ")
    assert result.returncode == 1
    assert hint in result.stderr
    assert "belong in env.sh" not in result.stderr
    empty = write_target(checkout, "empty.env", "STORAGE_TARGET_NAME=x\n")
    result = declarations(checkout, empty, env_lines="EXECUTION_SUBSTRATE=kubectl; ")
    assert 'e.g. TEST_DIRS=(["claim:path"]=1)' in result.stderr


@pytest.mark.parametrize(
    "setting",
    ["ELBENCHO_SCALE_THREAD_LIST=(1 2)", "MDTEST_ITERATIONS=9", "ELBENCHO_LIVEINT=5"],
)
def test_a_workload_setting_in_a_target_file_points_at_env_override(checkout, setting):
    target = write_target(checkout, "mixed.env", ROOTS_ONE + setting + "\n")
    result = declarations(checkout, target)
    assert result.returncode == 1
    variable = setting.split("=")[0]
    assert f"target file lbl sets workload setting(s): {variable}\n" in result.stderr
    assert "pass it with --env-override" in result.stderr
    assert "Error: invalid target file: lbl" in result.stderr


@pytest.mark.parametrize("entry", [IO, MD])
def test_a_workload_setting_in_a_launcher_target_names_the_file(checkout, entry):
    target = write_target(checkout, "mixed.env", ROOTS_ONE + "MDTEST_ITERATIONS=9\n")
    result = launch(checkout, entry, TARGET_FLAG, target, *ARGS_BY_ENTRY[entry])
    assert result.returncode == 1
    assert f"Error: target file {target} sets workload setting(s)" in result.stderr
    assert "MDTEST_ITERATIONS" in result.stderr
    assert not (checkout / "results").exists() or not any(
        (checkout / "results").glob("*")
    )


@pytest.mark.parametrize("entry", [IO, MD])
def test_one_target_on_a_plain_run_is_saved_with_provenance(checkout, entry):
    target = write_target(
        checkout, "one.env", ROOTS_ONE + "STORAGE_TARGET_NAME=vendor-a:fs1\n"
    )
    submitted = launch(checkout, entry, TARGET_FLAG, target, *ARGS_BY_ENTRY[entry])
    assert f"Target file: {target} (sha256 " in submitted.stdout
    assert "): STORAGE_TARGET_NAME TEST_DIRS\n" in submitted.stdout
    assert "Storage target: vendor-a:fs1\n" in submitted.stdout
    result = next((checkout / "results").glob(RESULT_GLOB[entry]))
    saved = yaml.safe_load((result / "env_used.yaml").read_text())
    assert list(saved["TEST_DIRS"]) == ["/data/two"]
    assert saved["target_file"]["file"] == str(target)
    assert saved["target_file"]["variables"] == ["STORAGE_TARGET_NAME", "TEST_DIRS"]
    assert saved["env_override"] is None
    # A later shell that sources the snapshot sees the recorded provenance.
    reloaded = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1/env_used.sh"; echo "$STORAGE_SCALE_TEST_TARGET_FILE"; '
            'echo "${!TEST_DIRS[*]}"',
            "reload",
            str(result),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert reloaded.stdout.splitlines() == [str(target), "/data/two"]


@pytest.mark.parametrize("entry", [IO, MD])
def test_a_plain_run_rejects_several_targets_and_prints_the_batch_commands(
    checkout, entry
):
    first = write_target(checkout, "first one.env", ROOTS_ONE)
    second = write_target(checkout, "second.env", 'TEST_DIRS=(["/data/three"]=1)\n')
    arguments = (TARGET_FLAG, first, TARGET_FLAG, second, *ARGS_BY_ENTRY[entry])
    result = launch(checkout, entry, *arguments)
    assert result.returncode == 1
    assert "2 --target files were given" in result.stderr
    launcher = str(checkout / "storage-tests/fs" / entry)
    lines = result.stderr.splitlines()
    batch_line = next(line for line in lines if " --batch " in line)
    start_line = next(line for line in lines if " --start " in line)
    # The commands are copy-paste safe: they parse back to the exact arguments
    # even though the checkout and the first target path contain spaces.
    assert shlex.split(batch_line) == [
        launcher,
        "--batch",
        *map(str, arguments),
    ]
    assert shlex.split(start_line)[:2] == [launcher, "--start"]
    # Plain words stay bare, so the commands stay readable.
    assert batch_line.endswith(" " + " ".join(ARGS_BY_ENTRY[entry]))
    assert not (checkout / "results").exists() or not any(
        (checkout / "results").glob("*")
    )


@pytest.mark.parametrize("entry", [IO, MD])
def test_help_states_the_rule_and_shows_target_with_env_override(checkout, entry):
    result = launch(checkout, entry, "--help")
    assert result.returncode == 0, result.stderr
    text = " ".join(result.stdout.split())
    assert RULE in text
    assert "--target <file>" in result.stdout
    example = next(
        line
        for line in result.stdout.splitlines()
        if "--batch" in line
        and "--target" in line
        and "--env-override" in line
        and "<file>]" not in line
    )
    assert example.count(TARGET_FLAG) == 2 and example.count("--env-override") == 1
    # The old way of passing target settings is gone from the help text.
    assert "TEST_DIRS, FS_MAX_*, MDTEST_*" not in result.stdout


def test_target_is_a_repeatable_option_of_both_launchers_without_a_value(checkout):
    for entry in (IO, MD):
        result = launch(checkout, entry, TARGET_FLAG)
        assert result.returncode == 1
        assert "--target requires" in result.stderr


def drop_env_roots(checkout):
    """Leave TEST_DIRS empty in env.sh, so only target files name roots."""
    env_sh = checkout / "env.sh"
    env_sh.write_text(
        env_sh.read_text().replace('TEST_DIRS=(["/data/one"]=1)', "TEST_DIRS=()")
    )


def test_an_empty_env_sh_hint_offers_target_on_both_launchers(checkout):
    drop_env_roots(checkout)
    for entry in (IO, MD):
        result = launch(checkout, entry, *ARGS_BY_ENTRY[entry])
        assert result.returncode == 1
        assert "  Set TEST_DIRS in env.sh or pass --target FILE\n" in result.stderr
        assert "to enable filesystem testing" not in result.stderr


def test_delete_only_resolves_test_dirs_from_one_target_file(checkout):
    drop_env_roots(checkout)
    target = write_target(checkout, "only.env", ROOTS_ONE)
    outside = launch(
        checkout, IO, "--delete-only", "/data/other/x", TARGET_FLAG, target
    )
    assert outside.returncode == 1
    assert "must be a strict subdirectory under TEST_DIRS" in outside.stderr
    assert "Must be under (and not equal to): /data/two\n" in outside.stderr
    root = launch(checkout, IO, "--delete-only", "/data/two", TARGET_FLAG, target)
    assert "Must be under (and not equal to): /data/two\n" in root.stderr
    inside = launch(checkout, IO, "--delete-only", "/data/two/x", TARGET_FLAG, target)
    # It passes validation and goes on to the (unreachable) SSH host.
    assert "strict subdirectory" not in inside.stderr
    assert "--target" not in inside.stderr


def test_delete_only_target_replaces_env_sh_roots(checkout):
    target = write_target(checkout, "only.env", ROOTS_ONE)
    old = launch(checkout, IO, "--delete-only", "/data/one/x", TARGET_FLAG, target)
    assert old.returncode == 1
    assert "Must be under (and not equal to): /data/two\n" in old.stderr


def test_delete_only_rejects_several_targets_and_bad_target_files(checkout):
    first = write_target(checkout, "a.env", ROOTS_ONE)
    second = write_target(checkout, "b.env", 'TEST_DIRS=(["/data/three"]=1)\n')
    result = launch(
        checkout, IO, "--delete-only", "/data/two/x",
        TARGET_FLAG, first, TARGET_FLAG, second,
    )  # fmt: skip
    assert result.returncode == 1
    assert "--delete-only takes at most one --target file (2 given)" in result.stderr
    bad = write_target(checkout, "bad.env", "unset TEST_DIRS\n")
    result = launch(checkout, IO, "--delete-only", "/data/two/x", TARGET_FLAG, bad)
    assert result.returncode == 1
    assert f"Error: target file {bad}: must set TEST_DIRS" in result.stderr


def test_delete_only_target_still_rejects_overrides_and_other_lifecycles(checkout):
    target = write_target(checkout, "only.env", ROOTS_ONE)
    override = checkout / "targets" / "o.env"
    override.write_text("MDTEST_ITERATIONS=2\n")
    result = launch(
        checkout, IO, "--delete-only", "/data/two/x",
        TARGET_FLAG, target, "--env-override", override,
    )  # fmt: skip
    assert result.returncode == 1 and "--env-override applies only" in result.stderr
    result = launch(checkout, IO, "--status", "/missing", TARGET_FLAG, target)
    assert result.returncode == 1 and "--target applies only" in result.stderr


def test_help_says_delete_only_takes_one_target(checkout):
    result = launch(checkout, IO, "--help")
    assert "TEST_DIRS comes from env.sh or one --target <file>." in result.stdout
    assert "(one file) with" in result.stdout
