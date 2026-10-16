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

"""--env-override files: precedence, validation, and saved provenance."""

# This module intentionally tests private integration-driver boundaries.
# pylint: disable=protected-access

from pathlib import Path
import hashlib
import importlib.util
import os
import re
import shlex
import subprocess
import sys

import pytest
import yaml

from tests.filesystem_checkout_test_support import make_filesystem_checkout

ROOT = Path(__file__).resolve().parents[1]
IO = "nv-elbencho-sweep.sh"
MD = "nv-mdtest-elbencho.sh"
OVERRIDE_FLAG = "--env-override"
PREFIX = "STORAGE_SCALE_TEST_ENV_OVERRIDE"
PROVENANCE = f"{PREFIX}_FILE"
SAVED_ONLY = "existing results always use their saved env_used.sh"
MDTEST_ITEMS_SETTING = "MDTEST_ITEMS_PER_DIR"
DUPLICATE = "given more than once for the same file"
THREAD_LIST = "ELBENCHO_SCALE_THREAD_LIST"
ARGS_BY_ENTRY = {IO: ("--nodes", "1"), MD: ("--nodes", "1", "--tasks", "1")}


@pytest.fixture(name="checkout")
def checkout_fixture(tmp_path):
    """Real launchers/libraries with a bounded, completely local environment."""
    base = make_filesystem_checkout(tmp_path, ROOT)
    # Unreachable hosts stop a direct submission right after reification.
    (base / "shim").mkdir()
    for tool in ("ssh", "scp"):
        (base / "shim" / tool).write_text("#!/bin/sh\nexit 255\n")
        (base / "shim" / tool).chmod(0o755)
    (base / "overrides").mkdir()
    return base


def write_override(checkout, name, content):
    """Create one override file beneath the checkout."""
    path = checkout / "overrides" / name
    path.write_text(content)
    return path


def launch(checkout, entry, *args):
    """Run a public entry point from the checkout, as an operator would."""
    env = {key: value for key, value in os.environ.items()}
    env.pop("EXECUTION_SUBSTRATE", None)
    env["PATH"] = f"{checkout / 'shim'}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(checkout / "storage-tests/fs" / entry), *map(str, args)],
        cwd=checkout,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def shell(checkout, source):
    """Run helpers after sourcing the checkout's env.sh at top level."""
    return subprocess.run(
        [
            "bash",
            "-ceu",
            'source "$1/env.sh"; source "$1/lib/_elbencho_functions.sh"; ' + source,
            "override-test",
            str(checkout),
        ],
        cwd=checkout,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def apply_and_print(checkout, override, names):
    """Apply an override like a launcher does, then print declarations."""
    printed = " ".join(f"declare -p {name};" for name in names)
    return shell(
        checkout,
        f"d=$(filesystem_env_override_declarations {shlex.quote(str(override))}); "
        f'eval "$d"; {printed} '
        'echo "VARIABLES=$STORAGE_SCALE_TEST_ENV_OVERRIDE_VARIABLES"',
    )


def batch_root(stdout):
    """Return the results directory printed by --batch/--append."""
    return Path(
        re.search(r"^STORAGE_SCALE_TEST_BATCH_RESULTS=(.*)$", stdout, re.M).group(1)
    )


def provenance(path):
    """Read the recorded override file from a snapshot or definition."""
    match = re.search(rf"^{PROVENANCE}=(.*)$", path.read_text(), re.M)
    assert match, f"{path} lacks provenance"
    values = shlex.split(match.group(1))
    return values[0] if values else ""


def target_provenance(path):
    """The (file, sha256, variables) recorded by a snapshot's target provenance."""
    values = []
    for name in ("FILE", "FILE_SHA256", "FILE_VARIABLES"):
        match = re.search(
            rf"^STORAGE_SCALE_TEST_TARGET_{name}=(.*)$", path.read_text(), re.M
        )
        assert match, f"{path} lacks target provenance {name}"
        words = shlex.split(match.group(1))
        values.append(words[0] if words else "")
    return tuple(values)


def test_relative_edits_types_and_scratch_variables_are_applied(checkout):
    override = write_override(
        checkout,
        "mixed.env",
        "ELBENCHO_SCALE_THREAD_LIST+=(2 4)\n"
        'ELBENCHO_SCALE_IO_SIZES=(1M "1M,4K")\n'
        "for size in 64G; do ELBENCHO_FILE_SIZE=$size; done\n"
        "echo noisy output must not become a declaration\n",
    )
    result = apply_and_print(
        checkout,
        override,
        ("ELBENCHO_SCALE_THREAD_LIST", "ELBENCHO_SCALE_IO_SIZES"),
    )
    assert result.returncode == 0, result.stderr
    assert '([0]="1" [1]="2" [2]="4")' in result.stdout
    assert '([0]="1M" [1]="1M,4K")' in result.stdout
    assert "noisy output" in result.stderr
    assert (
        "VARIABLES=ELBENCHO_FILE_SIZE ELBENCHO_SCALE_IO_SIZES "
        "ELBENCHO_SCALE_THREAD_LIST"
    ) in result.stdout
    assert "size" not in result.stdout.split("VARIABLES=")[1]


def test_unset_restores_the_env_base_default(checkout):
    override = write_override(
        checkout,
        "unset.env",
        "unset ELBENCHO_SCALE_IO_SIZES MDTEST_ITERATIONS\nELBENCHO_IODEPTH_LIST=()\n",
    )
    result = apply_and_print(
        checkout,
        override,
        ("ELBENCHO_SCALE_IO_SIZES", "MDTEST_ITERATIONS", "ELBENCHO_IODEPTH_LIST"),
    )
    assert result.returncode == 0, result.stderr
    assert '([0]="4K" [1]="16K" [2]="64K" [3]="1M" [4]="1M,4K")' in result.stdout
    assert 'MDTEST_ITERATIONS="3"' in result.stdout
    assert 'ELBENCHO_IODEPTH_LIST=([0]="1")' in result.stdout


def test_function_scope_eval_replaces_function_local_test_dirs(checkout):
    """Batch preparation sources env.sh inside a function."""
    override = write_override(checkout, "dirs.env", 'TEST_DIRS=(["/data/two"]=1)\n')
    result = subprocess.run(
        [
            "bash",
            "-ceu",
            'prepare() { source "$1/env.sh"; '
            'd=$(filesystem_target_declarations "$2"); eval "$d"; '
            'echo "${!TEST_DIRS[*]} FS_ENABLED=$FS_ENABLED"; }; prepare "$1" "$2"',
            "override-test",
            str(checkout),
            str(override),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/data/two FS_ENABLED=1"


@pytest.mark.parametrize(
    "helper",
    ["name", "file", "kind", "baseline", "digest", "output", "kinds", "stage", "root"],
)
@pytest.mark.parametrize("entry", [IO, MD])
def test_scratch_names_do_not_collide_with_implementation_locals(
    checkout, helper, entry
):
    override = write_override(
        checkout,
        "helper.env",
        f"for {helper} in 4; do MDTEST_ITEMS_PER_DIR=${helper}; done\n",
    )
    applied = apply_and_print(checkout, override, (MDTEST_ITEMS_SETTING,))
    assert applied.returncode == 0, applied.stderr
    assert 'MDTEST_ITEMS_PER_DIR="4"' in applied.stdout
    assert "VARIABLES=MDTEST_ITEMS_PER_DIR" in applied.stdout
    args = ("--nodes", "1") if entry == IO else ("--nodes", "1", "--tasks", "1")
    prepared = launch(checkout, entry, "--batch", OVERRIDE_FLAG, override, *args)
    assert prepared.returncode == 0, prepared.stderr


@pytest.mark.parametrize("helper", ["name", "file", "kind"])
def test_existing_site_variables_remain_protected(checkout, helper):
    env_file = checkout / "env.sh"
    env_file.write_text(env_file.read_text() + f"\n{helper}=site-value\n")
    override = write_override(checkout, "protected.env", f"{helper}=changed\n")
    result = apply_and_print(checkout, override, (MDTEST_ITEMS_SETTING,))
    assert result.returncode != 0
    assert f"unsupported variable(s): {helper}" in result.stderr


@pytest.mark.parametrize("entry", [IO, MD])
def test_batch_and_append_evaluate_relative_overrides_once(checkout, entry):
    # Older env.sh files leave duration to env_base.sh's exported default.
    env_file = checkout / "env.sh"
    env_file.write_text(
        env_file.read_text().replace("export ELBENCHO_SCALE_READ_WRITE_DURATION=60", "")
    )
    counter = checkout / "evaluations"
    override = write_override(
        checkout,
        "relative.env",
        f"printf 'evaluated\\n' >> {shlex.quote(str(counter))}\n"
        "ELBENCHO_SCALE_READ_WRITE_DURATION=$((ELBENCHO_SCALE_READ_WRITE_DURATION + 1))\n"
        "ELBENCHO_SCALE_THREAD_LIST+=(2)\n"
        "MDTEST_ITERATIONS=$((MDTEST_ITERATIONS + 1))\n",
    )
    args = ("--nodes", "1") if entry == IO else ("--nodes", "1", "--tasks", "1")
    created = launch(checkout, entry, "--batch", OVERRIDE_FLAG, override, *args)
    assert created.returncode == 0, created.stderr
    batch = batch_root(created.stdout)
    assert counter.read_text().splitlines() == ["evaluated"]
    appended = launch(
        checkout, entry, "--append", batch, OVERRIDE_FLAG, override, *args
    )
    assert appended.returncode == 0, appended.stderr
    assert counter.read_text().splitlines() == ["evaluated", "evaluated"]
    # Both shells retain the same evaluated settings, including settings the
    # launcher's own snapshot omits because they belong to the other workload.
    result = shell(
        checkout,
        f'r={shlex.quote(str(batch))}; elbencho_batch_verify_manifest "$r"; '
        'for id in $(list_elbencho_execution_ids "$r/executions"); do '
        '(elbencho_batch_load_execution_context "$r" "$id"; '
        'echo "$ELBENCHO_SCALE_READ_WRITE_DURATION $MDTEST_ITERATIONS '
        '${ELBENCHO_SCALE_THREAD_LIST[*]}"); done',
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["31 2 1 2"] * (4 if entry == IO else 2)
    assert not any(batch.rglob(".env-override-declarations.sh"))


def test_override_read_context_intentionally_excludes_unexported_site_helpers(checkout):
    env_file = checkout / "env.sh"
    env_file.write_text(env_file.read_text() + "\nsite_count=500\n")
    override = write_override(
        checkout, "limited-context.env", "MDTEST_ITEMS_PER_DIR=${site_count:-100}\n"
    )
    result = apply_and_print(checkout, override, (MDTEST_ITEMS_SETTING,))
    assert result.returncode == 0, result.stderr
    assert 'MDTEST_ITEMS_PER_DIR="100"' in result.stdout


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("EXECUTION_SUBSTRATE=slurm\n", "unsupported variable(s): EXECUTION_SUBSTRATE"),
        ("run_time=1-00:00:00\n", "unsupported variable(s): run_time"),
        ("TEST_DIR=/data/two\n", "Use TEST_DIRS"),
        ("ELBENCHO_RUN_IO_SIZE=4K\n", "unsupported variable(s): ELBENCHO_RUN_IO_SIZE"),
        ("ELBENCHO_SCALE_IO_SIZE=(4K)\n", "unsupported variable(s)"),
        ("PATH=/nowhere\n", "unsupported variable(s): PATH"),
        ('ELBENCHO_SCALE_IO_SIZES="4K 1M"\n', "contains whitespace"),
        ("ELBENCHO_FILE_SIZE=(1G)\n", "ELBENCHO_FILE_SIZE must be a scalar"),
        ("MDTEST_ITERATIONS=2\nexit 0\n", "exited early"),
        ("false\n", "returned status 1"),
        ("ELBENCHO_LIVEINT=(\n", "returned status"),
    ],
)
def test_invalid_overrides_are_rejected_without_output(checkout, content, message):
    override = write_override(checkout, "bad.env", content)
    result = launch(checkout, IO, OVERRIDE_FLAG, override, "--nodes", "1")
    assert result.returncode == 1
    assert message in result.stderr
    assert not (checkout / "results").exists() or not any(
        (checkout / "results").glob("elbencho-*")
    )


@pytest.mark.parametrize("entry", [IO, MD])
def test_batch_groups_each_save_their_own_override(checkout, entry):
    io_override = write_override(
        checkout, "io.env", "ELBENCHO_SCALE_THREAD_LIST=(1 2)\nELBENCHO_LIVEINT=250\n"
    )
    md_override = write_override(checkout, "md.env", "MDTEST_ITEMS_PER_DIR=500\n")
    md_target = write_override(
        checkout, "md-target.env", 'TEST_DIRS=(["/data/two"]=1)\n'
    )
    env_before = (checkout / "env.sh").read_text()
    io_args = ("--nodes", "1")
    md_args = ("--nodes", "1", "--tasks", "1")
    first, second = (IO, MD) if entry == IO else (MD, IO)
    io_files = (OVERRIDE_FLAG, io_override)
    md_files = ("--target", md_target, OVERRIDE_FLAG, md_override)
    first_files, second_files = (
        (io_files, md_files) if entry == IO else (md_files, io_files)
    )
    created = launch(
        checkout,
        first,
        "--batch",
        *first_files,
        *(io_args if first == IO else md_args),
    )
    assert created.returncode == 0, created.stderr
    batch = batch_root(created.stdout)
    appended = launch(
        checkout,
        second,
        "--append",
        batch,
        *second_files,
        *(io_args if second == IO else md_args),
    )
    assert appended.returncode == 0, appended.stderr
    assert (checkout / "env.sh").read_text() == env_before

    manifest = [
        line.split("\t")
        for line in (batch / "batch-manifest.tsv").read_text().splitlines()
    ]
    groups = {row[2]: batch / row[3] for row in manifest if row[0] == "group"}
    for kind, override in (("io", io_override), ("mdtest", md_override)):
        saved = yaml.safe_load((groups[kind] / "env_used.yaml").read_text())
        # The operator's file, not the staged copy, is the recorded provenance.
        assert saved["env_override"]["file"] == str(override)
        assert provenance(groups[kind] / "env_used.sh") == str(override)
    # Only the metadata group has a target file; it too records the original.
    assert (
        yaml.safe_load((groups["io"] / "env_used.yaml").read_text())["target_file"]
        is None
    )
    saved_target = yaml.safe_load((groups["mdtest"] / "env_used.yaml").read_text())[
        "target_file"
    ]
    assert saved_target["file"] == str(md_target)
    assert saved_target["sha256"] == sha256_of(md_target)
    assert saved_target["variables"] == ["TEST_DIRS"]
    assert yaml.safe_load((groups["io"] / "env_used.yaml").read_text())[
        "ELBENCHO_SCALE_THREAD_LIST"
    ] == ["1", "2"]

    # Change both inputs after preparation: saved definitions are unaffected.
    (checkout / "env.sh").write_text(env_before + "\nexport ELBENCHO_LIVEINT=999\n")
    io_override.write_text("ELBENCHO_LIVEINT=777\n")
    md_override.write_text("MDTEST_ITEMS_PER_DIR=777\n")
    md_target.write_text('TEST_DIRS=(["/data/changed"]=1)\n')
    result = shell(
        checkout,
        f'r={shlex.quote(str(batch))}; elbencho_batch_verify_manifest "$r"; '
        '_elbencho_batch_validate_cells "$r"; '
        'for id in $(list_elbencho_execution_ids "$r/executions"); do '
        '(elbencho_batch_load_execution_context "$r" "$id"; '
        'echo "$ELBENCHO_EXECUTION_KIND ${#ELBENCHO_SCALE_THREAD_LIST[@]} '
        '$ELBENCHO_LIVEINT $MDTEST_ITEMS_PER_DIR ${!TEST_DIRS[*]}"); done',
    )
    assert result.returncode == 0, result.stderr
    lines = sorted(result.stdout.splitlines())
    assert lines[:2] == ["io 2 250 2 /data/one", "io 2 250 2 /data/one"]
    assert lines[2].startswith("mdtest ") and lines[2].endswith(" 500 /data/two")


def test_direct_submission_saves_values_that_survive_env_sh_reloads(checkout):
    override = write_override(
        checkout,
        "direct.env",
        "ELBENCHO_SCALE_IO_SIZES=(1M)\nELBENCHO_SCALE_READ_WRITE_DURATION=7\n",
    )
    submitted = launch(checkout, IO, OVERRIDE_FLAG, override, "--nodes", "1")
    assert "Env override: " in submitted.stdout
    result = next((checkout / "results").glob("elbencho-*"))
    assert '"1M"' in (result / "executions/0001.sh").read_text()
    assert (
        "ELBENCHO_SCALE_READ_WRITE_DURATION=7"
        in (result / "executions/0001.sh").read_text()
    )
    saved = yaml.safe_load((result / "env_used.yaml").read_text())
    assert saved["env_override"]["variables"] == [
        "ELBENCHO_SCALE_IO_SIZES",
        "ELBENCHO_SCALE_READ_WRITE_DURATION",
    ]
    # Slurm coordinators and --resume source today's env.sh, then the snapshot.
    reloaded = shell(
        checkout,
        f"source {shlex.quote(str(result))}/env_used.sh; "
        'echo "${ELBENCHO_SCALE_IO_SIZES[*]} $ELBENCHO_SCALE_READ_WRITE_DURATION"',
    )
    assert reloaded.stdout.strip() == "1M 7", reloaded.stderr
    # A treefile-cache rewrite regenerates the YAML from env_used.sh.
    rewritten = shell(
        checkout,
        f"update_elbencho_env_used_treefile_cache_usage {shlex.quote(str(result))}",
    )
    assert rewritten.returncode == 0, rewritten.stderr
    again = yaml.safe_load((result / "env_used.yaml").read_text())
    assert again["env_override"] == saved["env_override"]
    resumed = launch(checkout, IO, "--resume", result, OVERRIDE_FLAG, override)
    assert resumed.returncode == 1 and SAVED_ONLY in resumed.stderr


def test_submission_without_override_records_none(checkout):
    launch(checkout, MD, "--nodes", "1", "--tasks", "1")
    result = next((checkout / "results").glob("mdtest-elbencho-*"))
    assert (
        yaml.safe_load((result / "env_used.yaml").read_text())["env_override"] is None
    )
    assert provenance(result / "env_used.sh") == ""
    assert yaml.safe_load((result / "env_used.yaml").read_text())["target_file"] is None
    assert target_provenance(result / "env_used.sh") == ("", "", "")


@pytest.mark.parametrize(
    ("entry", "args", "message"),
    [
        (IO, ("--resume", "/missing"), SAVED_ONLY),
        (IO, ("--status", "/missing"), SAVED_ONLY),
        (IO, ("--delete-only", "/data/one/x"), SAVED_ONLY),
        (MD, ("--resume", "/missing"), SAVED_ONLY),
        (MD, ("--collect", "/missing"), SAVED_ONLY),
        (IO, ("--start", "{batch}"), "rejects workload arguments (--env-override)"),
        (IO, ("--status", "/missing", OVERRIDE_FLAG, "{file}"), SAVED_ONLY),
        (IO, (OVERRIDE_FLAG, "{file}", "--nodes", "1"), DUPLICATE),
        (
            MD,
            ("--batch", OVERRIDE_FLAG, "{file}", "--nodes", "1", "--tasks", "1"),
            DUPLICATE,
        ),
        (IO, ("--batch", "--nodes", "1"), "--env-override requires a file argument"),
    ],
)
def test_cli_rejections(checkout, entry, args, message):
    override = write_override(checkout, "ok.env", "MDTEST_ITERATIONS=2\n")
    batch = ""
    if "{batch}" in args:
        batch = batch_root(launch(checkout, IO, "--batch", "--nodes", "1").stdout)
    rendered = [arg.format(batch=batch, file=override) for arg in args]
    trailing = [] if message.endswith("file argument") else [str(override)]
    result = launch(checkout, entry, *rendered, OVERRIDE_FLAG, *trailing)
    assert result.returncode != 0
    assert message in result.stderr


def flags(*files):
    """Repeat --env-override once per file."""
    return [part for file in files for part in (OVERRIDE_FLAG, file)]


def sha256_of(path):
    """Hex digest of an override file's bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def saved_files(snapshot_dir):
    """The ordered `files` list recorded in a result or group directory."""
    saved = yaml.safe_load((snapshot_dir / "env_used.yaml").read_text())
    return saved["env_override"]["files"]


def sourced_provenance(checkout, snapshot_sh):
    """Source a saved env_used.sh and return its provenance arrays."""
    names = ("FILES", "SHA256S", "FILE_VARIABLES")
    printed = "".join(
        f"printf '%s\\n' \"${{{PREFIX}_{name}[@]}}\"; echo ---; " for name in names
    )
    result = shell(checkout, f"source {shlex.quote(str(snapshot_sh))}; {printed}")
    assert result.returncode == 0, result.stderr
    sections = result.stdout.split("---\n")
    return [section.splitlines() for section in sections[:3]]


def test_duplicate_override_paths_are_rejected_however_they_are_spelled(checkout):
    override = write_override(checkout, "same.env", "MDTEST_ITERATIONS=2\n")
    link = checkout / "overrides" / "link.env"
    link.symlink_to(override)
    spellings = (
        (override, override),
        (Path("overrides/same.env"), override),
        (override, Path("overrides/../overrides/same.env")),
        (override, link),
    )
    for first, second in spellings:
        result = launch(checkout, IO, *flags(first, second), "--nodes", "1")
        assert result.returncode == 1, (first, second)
        assert DUPLICATE in result.stderr
    batched = launch(checkout, IO, "--batch", *flags(override, link), "--nodes", "1")
    assert batched.returncode == 1 and DUPLICATE in batched.stderr
    assert not (checkout / "results").exists() or not any(
        (checkout / "results").glob("*")
    )


@pytest.mark.parametrize("entry", [IO, MD])
def test_repeated_overrides_apply_in_order(checkout, entry):
    first = write_override(
        checkout, "first.env", f"{THREAD_LIST}=(1 2)\nMDTEST_ITERATIONS=5\n"
    )
    second = write_override(
        checkout, "second.env", f"{THREAD_LIST}+=(4)\nMDTEST_ITERATIONS=9\n"
    )
    created = launch(
        checkout, entry, "--batch", *flags(first, second), *ARGS_BY_ENTRY[entry]
    )
    assert created.returncode == 0, created.stderr
    batch = batch_root(created.stdout)
    context = (
        f'r={shlex.quote(str(batch))}; elbencho_batch_verify_manifest "$r"; '
        'for id in $(list_elbencho_execution_ids "$r/executions"); do '
        '(elbencho_batch_load_execution_context "$r" "$id"; '
        f'echo "${{{THREAD_LIST}[*]}} $MDTEST_ITERATIONS"); done'
    )
    result = shell(checkout, context)
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.splitlines()) == {"1 2 4 9"}
    # The reverse order is not equivalent: the second file replaces the list.
    result = apply_in_order(checkout, (second, first), THREAD_LIST)
    assert result.stdout.split()[:2] == ["1", "2"] and result.stdout.split()[-1] != "4"


def apply_in_order(checkout, files, name):
    """Apply override files like a launcher does; print one setting."""
    return shell(
        checkout,
        "for f in "
        + " ".join(shlex.quote(str(file)) for file in files)
        + '; do d=$(filesystem_env_override_declarations "$f") || exit 1; '
        f'eval "$d"; done; echo "${{{name}[*]}}"',
    )


def test_every_override_can_be_edited_relative_to_the_previous_result(checkout):
    files = [
        write_override(checkout, f"step{i}.env", f"{THREAD_LIST}+=({i})\n")
        for i in (7, 8, 9)
    ]
    result = apply_in_order(checkout, files, THREAD_LIST)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split()[-3:] == ["7", "8", "9"]


@pytest.mark.parametrize(
    ("entry", "glob", "setting"),
    [(IO, "elbencho-*", "ELBENCHO_SCALE_IO_SIZES"), (MD, "mdtest-elbencho-*", None)],
)
def test_provenance_lists_every_file_and_keeps_the_legacy_scalars(
    checkout, entry, glob, setting
):
    first = write_override(checkout, "a-recipe.env", "ELBENCHO_LIVEINT=250\n")
    second = write_override(
        checkout,
        "b-recipe.env",
        "MDTEST_ITERATIONS=4\n"
        + (f"{setting}=(1M)\n" if setting else "MDTEST_ITEMS_PER_DIR=7\n"),
    )
    submitted = launch(checkout, entry, *flags(first, second), *ARGS_BY_ENTRY[entry])
    lines = [line for line in submitted.stdout.splitlines() if "Env override" in line]
    assert [line.split(":")[0] for line in lines] == [
        "Env override 1/2",
        "Env override 2/2",
    ]
    assert f"{first} (sha256 {sha256_of(first)}): ELBENCHO_LIVEINT" in lines[0]
    result = next((checkout / "results").glob(glob))
    union = sorted(
        {"ELBENCHO_LIVEINT", "MDTEST_ITERATIONS"} | {setting or "MDTEST_ITEMS_PER_DIR"}
    )
    saved = yaml.safe_load((result / "env_used.yaml").read_text())["env_override"]
    assert saved["file"] == str(second) and saved["sha256"] == sha256_of(second)
    assert saved["variables"] == union
    assert saved["files"] == [
        {
            "file": str(first),
            "sha256": sha256_of(first),
            "variables": ["ELBENCHO_LIVEINT"],
        },
        {
            "file": str(second),
            "sha256": sha256_of(second),
            "variables": sorted(set(union) - {"ELBENCHO_LIVEINT"}),
        },
    ]
    assert provenance(result / "env_used.sh") == str(second)
    files, digests, per_file = sourced_provenance(checkout, result / "env_used.sh")
    assert files == [str(first), str(second)]
    assert digests == [sha256_of(first), sha256_of(second)]
    assert per_file[0] == "ELBENCHO_LIVEINT"
    assert (
        " " not in per_file[0] and per_file[1].split() == saved["files"][1]["variables"]
    )
    reloaded = shell(
        checkout,
        f"source {shlex.quote(str(result))}/env_used.sh; "
        f'echo "$STORAGE_SCALE_TEST_ENV_OVERRIDE_VARIABLES"',
    )
    assert reloaded.stdout.split() == union


def test_one_override_keeps_the_single_file_output(checkout):
    override = write_override(checkout, "only.env", "MDTEST_ITERATIONS=4\n")
    submitted = launch(checkout, MD, *flags(override), *ARGS_BY_ENTRY[MD])
    assert f"Env override: {override} (sha256 " in submitted.stdout
    assert "Env override 1/1" not in submitted.stdout
    result = next((checkout / "results").glob("mdtest-elbencho-*"))
    assert [entry["file"] for entry in saved_files(result)] == [str(override)]


@pytest.mark.parametrize("entry", [IO, MD])
def test_batch_and_append_record_each_groups_own_ordered_overrides(checkout, entry):
    names = ("t1.env", "w1.env", "t2.env", "w2.env")
    t1, w1, t2, w2 = (
        write_override(
            checkout, name, f"ELBENCHO_LIVEINT={i}00\nMDTEST_ITERATIONS={i}\n"
        )
        for i, name in enumerate(names, start=2)
    )
    other = MD if entry == IO else IO
    created = launch(checkout, entry, "--batch", *flags(t1, w1), *ARGS_BY_ENTRY[entry])
    assert created.returncode == 0, created.stderr
    batch = batch_root(created.stdout)
    # Relative spellings on the second group still record absolute paths.
    appended = launch(
        checkout,
        other,
        "--append",
        batch,
        *flags(t2.relative_to(checkout), w2.relative_to(checkout)),
        *ARGS_BY_ENTRY[other],
    )
    assert appended.returncode == 0, appended.stderr
    manifest = [
        line.split("\t")
        for line in (batch / "batch-manifest.tsv").read_text().splitlines()
    ]
    groups = {row[2]: batch / row[3] for row in manifest if row[0] == "group"}
    expected = {entry_kind(entry): (t1, w1), entry_kind(other): (t2, w2)}
    for kind, (target, workload) in expected.items():
        recorded = saved_files(groups[kind])
        assert [item["file"] for item in recorded] == [str(target), str(workload)]
        assert [item["sha256"] for item in recorded] == [
            sha256_of(target),
            sha256_of(workload),
        ]
        assert provenance(groups[kind] / "env_used.sh") == str(workload)
    assert not any(batch.rglob(".env-override*"))
    # Each execution loads its own group's final settings.
    result = shell(
        checkout,
        f'r={shlex.quote(str(batch))}; elbencho_batch_verify_manifest "$r"; '
        'for id in $(list_elbencho_execution_ids "$r/executions"); do '
        '(elbencho_batch_load_execution_context "$r" "$id"; '
        'echo "$ELBENCHO_EXECUTION_KIND $MDTEST_ITERATIONS"); done',
    )
    assert result.returncode == 0, result.stderr
    iterations = {
        entry_kind(entry): 3,
        entry_kind(other): 5,
    }
    assert {
        line.split()[0]: line.split()[1] for line in result.stdout.splitlines()
    } == {kind: str(value) for kind, value in iterations.items()}


def entry_kind(entry):
    """The batch group kind a launcher creates."""
    return "io" if entry == IO else "mdtest"


@pytest.mark.parametrize(
    ("scalars", "expected"),
    [
        (
            f"{PREFIX}_FILE=/ops/old.env\n"
            f"{PREFIX}_SHA256=abc123\n"
            f"{PREFIX}_VARIABLES='FS_MAX_NODE_IOPS MDTEST_ITERATIONS'\n",
            [
                {
                    "file": "/ops/old.env",
                    "sha256": "abc123",
                    "variables": ["FS_MAX_NODE_IOPS", "MDTEST_ITERATIONS"],
                }
            ],
        ),
        (f"{PREFIX}_FILE=''\n", None),
    ],
)
def test_old_scalar_only_snapshots_normalize_to_one_entry_files(
    checkout, scalars, expected
):
    old = checkout / "old-env-used.sh"
    old.write_text(scalars)
    # Stale arrays from some other snapshot must never leak into this one.
    result = shell(
        checkout,
        f"{PREFIX}_FILES=(/stale.env); {PREFIX}_SHA256S=(dead); "
        f"{PREFIX}_FILE_VARIABLES=(STALE); source {shlex.quote(str(old))}; "
        "_emit_env_override_provenance_yaml; _emit_env_override_provenance_sh",
    )
    assert result.returncode == 0, result.stderr
    yaml_text, _, shell_text = result.stdout.partition("# --env-override")
    recorded = yaml.safe_load(yaml_text)["env_override"]
    assert "stale" not in result.stdout.lower()
    if expected is None:
        assert recorded is None
        assert f"{PREFIX}_FILES=()" in shell_text
        return
    assert recorded["files"] == expected
    assert recorded["file"] == "/ops/old.env" and recorded["sha256"] == "abc123"
    assert f"{PREFIX}_FILES=(/ops/old.env)" in shell_text


@pytest.mark.parametrize(
    ("arrays", "survives"),
    [
        # The last entry matches the scalars but earlier entries add variables
        # the scalar union does not list: stale, rebuilt from the scalars.
        (
            "FILES=(/stale.env /ops/old.env); SHA256S=(dead abc123); "
            "FILE_VARIABLES=(STALE_ONE FS_MAX_NODE_IOPS)",
            False,
        ),
        # Same file and union but another digest.
        (
            "FILES=(/ops/old.env); SHA256S=(dead); FILE_VARIABLES=(FS_MAX_NODE_IOPS)",
            False,
        ),
        # Parallel arrays of different lengths cannot be consistent.
        (
            "FILES=(/ops/old.env); SHA256S=(); FILE_VARIABLES=(FS_MAX_NODE_IOPS)",
            False,
        ),
        # A genuine two-file snapshot: every array agrees with the scalars.
        (
            "FILES=(/ops/first.env /ops/old.env); SHA256S=(beef abc123); "
            "FILE_VARIABLES=(MDTEST_ITERATIONS FS_MAX_NODE_IOPS)",
            True,
        ),
    ],
)
def test_provenance_arrays_must_agree_with_every_scalar_to_survive(
    checkout, arrays, survives
):
    """Arrays left by an earlier snapshot never pass for this snapshot's."""
    scalars = (
        f"{PREFIX}_FILE=/ops/old.env; {PREFIX}_SHA256=abc123; "
        f"{PREFIX}_VARIABLES='FS_MAX_NODE_IOPS'; "
    )
    if survives:
        scalars = scalars.replace(
            "'FS_MAX_NODE_IOPS'", "'FS_MAX_NODE_IOPS MDTEST_ITERATIONS'"
        )
    assignments = "; ".join(f"{PREFIX}_{part.strip()}" for part in arrays.split("; "))
    result = shell(
        checkout,
        f"{assignments}; {scalars}normalize_env_override_provenance; "
        f"printf '%s|' \"${{{PREFIX}_FILES[@]}}\"; echo; "
        f"printf '%s|' \"${{{PREFIX}_FILE_VARIABLES[@]}}\"; echo",
    )
    assert result.returncode == 0, result.stderr
    files, variables = result.stdout.splitlines()
    if survives:
        assert files == "/ops/first.env|/ops/old.env|"
        assert variables == "MDTEST_ITERATIONS|FS_MAX_NODE_IOPS|"
    else:
        assert files == "/ops/old.env|"
        assert variables == "FS_MAX_NODE_IOPS|"
        assert "STALE" not in result.stdout


def test_summary_of_old_snapshot_state_prints_one_unnumbered_line(checkout):
    result = shell(
        checkout,
        f"{PREFIX}_FILE=/ops/old.env; {PREFIX}_SHA256=ab; "
        f"{PREFIX}_VARIABLES=MDTEST_ITERATIONS; print_env_override_summary",
    )
    assert (
        result.stdout == "Env override: /ops/old.env (sha256 ab): MDTEST_ITERATIONS\n"
    )


@pytest.mark.parametrize(
    ("entry", "glob", "key", "expected"),
    [
        (IO, "elbencho-*", "ELBENCHO_SCALE_IO_SIZES", ["1M"]),
        (MD, "mdtest-elbencho-*", MDTEST_ITEMS_SETTING, 7),
    ],
)
def test_a_target_file_and_a_workload_file_both_apply(
    checkout, entry, glob, key, expected
):
    target = write_override(
        checkout,
        "target.env",
        'STORAGE_TARGET_NAME=vendor-a:fs1\nTEST_DIRS=(["/data/two"]=1)\n'
        "FS_MAX_NODE_IOPS=777\n",
    )
    workload = write_override(
        checkout,
        "recipe.env",
        "ELBENCHO_SCALE_IO_SIZES=(1M)\nMDTEST_ITEMS_PER_DIR=7\n",
    )
    submitted = launch(
        checkout,
        entry,
        "--target",
        target,
        *flags(workload),
        *ARGS_BY_ENTRY[entry],
    )
    assert "Storage target: vendor-a:fs1\n" in submitted.stdout
    assert f"Target file: {target} (sha256 {sha256_of(target)}): " in submitted.stdout
    result = next((checkout / "results").glob(glob))
    saved = yaml.safe_load((result / "env_used.yaml").read_text())
    assert saved["STORAGE_TARGET_NAME"] == "vendor-a:fs1"
    assert list(saved["TEST_DIRS"]) == ["/data/two"]
    assert saved["FS_MAX_NODE_IOPS"] == 777
    assert saved[key] == expected
    assert saved["target_file"] == {
        "file": str(target),
        "sha256": sha256_of(target),
        "variables": ["FS_MAX_NODE_IOPS", "STORAGE_TARGET_NAME", "TEST_DIRS"],
    }
    # The target file is not an override: the list holds the workload file only.
    assert [item["file"] for item in saved["env_override"]["files"]] == [str(workload)]
    assert [item["variables"] for item in saved["env_override"]["files"]] == [
        ["ELBENCHO_SCALE_IO_SIZES", "MDTEST_ITEMS_PER_DIR"],
    ]
    assert target_provenance(result / "env_used.sh") == (
        str(target),
        sha256_of(target),
        "FS_MAX_NODE_IOPS STORAGE_TARGET_NAME TEST_DIRS",
    )


@pytest.mark.parametrize(
    ("path", "message"),
    [("overrides/missing.env", "not found"), ("overrides", "regular file")],
)
def test_override_path_must_be_a_readable_file(checkout, path, message):
    result = launch(checkout, IO, "--batch", OVERRIDE_FLAG, path, "--nodes", "1")
    assert result.returncode == 1 and message in result.stderr


@pytest.mark.parametrize(
    ("entry", "args", "glob"),
    [
        (IO, ("--nodes", "1"), "elbencho-*"),
        (MD, ("--nodes", "1", "--tasks", "1"), "mdtest-elbencho-*"),
    ],
)
def test_target_file_sets_the_storage_target_name(checkout, entry, args, glob):
    target = write_override(
        checkout,
        "name.env",
        'STORAGE_TARGET_NAME=vendor-a:fs1\nTEST_DIRS=(["/data/one"]=1)\n',
    )
    submitted = launch(checkout, entry, "--target", target, *args)
    assert "Storage target: vendor-a:fs1\n" in submitted.stdout
    result = next((checkout / "results").glob(glob))
    saved = yaml.safe_load((result / "env_used.yaml").read_text())
    assert saved["STORAGE_TARGET_NAME"] == "vendor-a:fs1"
    assert saved["target_file"]["variables"] == ["STORAGE_TARGET_NAME", "TEST_DIRS"]
    assert saved["env_override"] is None
    reloaded = shell(
        checkout,
        f"source {shlex.quote(str(result))}/env_used.sh; "
        'printf %s "$STORAGE_TARGET_NAME"',
    )
    assert reloaded.stdout == "vendor-a:fs1", reloaded.stderr


@pytest.mark.parametrize(
    ("entry", "args", "glob"),
    [
        (IO, ("--nodes", "1"), "elbencho-*"),
        (MD, ("--nodes", "1", "--tasks", "1"), "mdtest-elbencho-*"),
    ],
)
def test_unset_storage_target_name_is_derived_at_snapshot_time(
    checkout, entry, args, glob
):
    """Never frozen at env load: a target file's TEST_DIRS decides the default."""
    target = write_override(checkout, "roots.env", 'TEST_DIRS=(["/data/two/"]=1)\n')
    submitted = launch(checkout, entry, "--target", target, *args)
    assert "Storage target: /data/two\n" in submitted.stdout
    result = next((checkout / "results").glob(glob))
    saved = yaml.safe_load((result / "env_used.yaml").read_text())
    assert saved["STORAGE_TARGET_NAME"] == "/data/two"


@pytest.mark.parametrize(
    ("entry", "args", "glob"),
    [
        (IO, ("--nodes", "1"), "elbencho-*"),
        (MD, ("--nodes", "1", "--tasks", "1"), "mdtest-elbencho-*"),
    ],
)
@pytest.mark.parametrize("name", ["has space", "-leading", "a,b", "x" * 129, "unknown"])
def test_invalid_storage_target_name_is_rejected_before_any_output(
    checkout, entry, args, glob, name
):
    target = write_override(
        checkout,
        "bad-name.env",
        f'TEST_DIRS=(["/data/one"]=1)\nSTORAGE_TARGET_NAME={shlex.quote(name)}\n',
    )
    result = launch(checkout, entry, "--target", target, *args)
    assert result.returncode == 1
    assert "Error: STORAGE_TARGET_NAME" in result.stderr
    assert not (checkout / "results").exists() or not any(
        (checkout / "results").glob(glob)
    )


def test_override_names_are_snapshotted_and_cover_every_default():
    """Every overridable name must survive later env.sh reloads."""
    library = (ROOT / "lib/env_functions.sh").read_text()
    names = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; filesystem_env_override_names | cut -f1; '
            "filesystem_target_setting_names | cut -f1",
            "names",
            str(ROOT / "lib/env_functions.sh"),
        ],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.split()
    writers = "".join(
        library.split(f"{writer}() {{", 1)[1].split("\n}\n")[0]
        for writer in (
            "_write_elbencho_env_used_sh",
            "_write_mdtest_elbencho_env_used_sh",
        )
    )
    for name in names:
        # TEST_DIRS is emitted by _emit_bash_test_dirs_decl from both writers.
        assert name in writers or name == "TEST_DIRS", name
    defaults = library.split("apply_filesystem_workload_defaults() {", 1)[1].split(
        "\n}\n"
    )[0]
    assigned = set(re.findall(r"\b((?:FS_MAX|MDTEST|ELBENCHO)_[A-Z_]+)=", defaults))
    assert assigned and assigned <= set(names)


def _load_integration_driver():
    path = ROOT / "integration-tests/bin/integration-test.py"
    spec = importlib.util.spec_from_file_location(
        "integration_driver_env_override", path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return (
        sys.modules["filesystem_integration"],
        sys.modules["filesystem_scenario_specs"],
    )


def test_mixed_batch_scenario_overrides_prepare_locally(checkout):
    """Offline check of the real-infrastructure mixed-batch override contract."""
    filesystem, specs = _load_integration_driver()
    scenario = specs.SCENARIO_SPECS_BY_NAME["mixed-batch"]
    values = {
        "test_root": "/data/one",
        "test_root_secondary": "/data/two",
        "workspace": str(checkout),
        "batch_results_dir": "",
    }
    for step in scenario.steps:
        for item in step.render_support_files(values):
            target = checkout / item.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(item.content)
        entry = MD if step.workload_kind == "mdtest" else IO
        result = launch(checkout, entry, *step.render_arguments(values))
        assert result.returncode == 0, result.stderr
        values["batch_results_dir"] = str(batch_root(result.stdout))
    batch = Path(values["batch_results_dir"])
    _, executions = filesystem._mixed_batch_membership(batch)
    filesystem._assert_mixed_batch_overrides(batch, executions, scenario.steps)
    definitions = [(batch / f"executions/000{i}.sh").read_text() for i in (1, 2, 3)]
    assert "/data/two" in definitions[1]
    assert "export MDTEST_ITERATIONS=2" in definitions[1]
    assert "export ELBENCHO_FILE_SIZE=8M" in definitions[2]
    assert "export ELBENCHO_FILE_SIZE=16M" not in definitions[2]


def test_the_two_allowlists_split_target_from_workload_settings():
    """What to test and how to test never overlap."""
    listed = {}
    for function in (
        "filesystem_target_setting_names",
        "filesystem_env_override_names",
    ):
        listed[function] = subprocess.run(
            [
                "bash",
                "-c",
                f'source "$1"; {function} | cut -f1',
                "names",
                str(ROOT / "lib/env_functions.sh"),
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.split()
    target = listed["filesystem_target_setting_names"]
    workload = listed["filesystem_env_override_names"]
    assert sorted(target) == [
        "FS_MAX_AGG_THROUGHPUT",
        "FS_MAX_NODE_IOPS",
        "FS_MAX_NODE_THROUGHPUT_GBPS",
        "STORAGE_TARGET_NAME",
        "TEST_DIRS",
    ]
    assert not set(target) & set(workload)
    assert MDTEST_ITEMS_SETTING in workload and THREAD_LIST in workload


@pytest.mark.parametrize("operation", [(), ("--batch",)])
@pytest.mark.parametrize("entry", [IO, MD])
@pytest.mark.parametrize(
    "setting",
    [
        'TEST_DIRS=(["/data/two"]=1)',
        "STORAGE_TARGET_NAME=vendor-a",
        "FS_MAX_NODE_IOPS=777",
    ],
)
def test_override_setting_a_target_setting_points_at_target(
    checkout, operation, entry, setting
):
    """The v2.0.0 migration error names the file and variable and says --target."""
    override = write_override(
        checkout, "old-style.env", f"{setting}\nMDTEST_ITERATIONS=2\n"
    )
    result = launch(
        checkout, entry, *operation, OVERRIDE_FLAG, override, *ARGS_BY_ENTRY[entry]
    )
    assert result.returncode == 1
    variable = re.match(r"[A-Z_]+", setting).group(0)
    assert (
        f"Error: env override file {override} sets target setting(s): {variable}"
    ) in result.stderr
    assert "pass this file with --target instead of --env-override" in result.stderr
    assert not (checkout / "results").exists() or not any(
        (checkout / "results").glob("*-*")
    )


def test_override_changing_target_and_unsupported_settings_reports_both(checkout):
    override = write_override(
        checkout, "both.env", 'TEST_DIRS=(["/data/two"]=1)\nrun_time=1-00:00:00\n'
    )
    result = launch(checkout, IO, OVERRIDE_FLAG, override, "--nodes", "1")
    assert result.returncode == 1
    assert "sets target setting(s): TEST_DIRS" in result.stderr
    assert "unsupported variable(s): run_time" in result.stderr


def test_an_override_restating_an_unchanged_target_setting_is_a_noop(checkout):
    """Detection is by change: restating env.sh's value is accepted, a new one is not."""
    restated = write_override(
        checkout,
        "restated.env",
        'TEST_DIRS=(["/data/one"]=1)\nMDTEST_ITERATIONS=3\n',
    )
    result = shell(
        checkout,
        f"d=$(filesystem_env_override_declarations {shlex.quote(str(restated))}) "
        '|| exit 1; eval "$d"; echo "${!TEST_DIRS[*]} $MDTEST_ITERATIONS"; '
        'echo "${STORAGE_SCALE_TEST_ENV_OVERRIDE_FILE_VARIABLES[0]}"',
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["/data/one 3", "MDTEST_ITERATIONS"]
    changed = write_override(checkout, "changed.env", 'TEST_DIRS=(["/data/one"]=2)\n')
    result = shell(
        checkout,
        f"filesystem_env_override_declarations {shlex.quote(str(changed))}",
    )
    assert result.returncode == 1
    assert "sets target setting(s): TEST_DIRS" in result.stderr
