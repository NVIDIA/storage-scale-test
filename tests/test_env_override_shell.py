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
PROVENANCE = "STORAGE_SCALE_TEST_ENV_OVERRIDE_FILE"
SAVED_ONLY = "existing results always use their saved env_used.sh"
MDTEST_ITEMS_SETTING = "MDTEST_ITEMS_PER_DIR"


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


def test_relative_edits_types_and_scratch_variables_are_applied(checkout):
    override = write_override(
        checkout,
        "mixed.env",
        "ELBENCHO_SCALE_THREAD_LIST+=(2 4)\n"
        'ELBENCHO_SCALE_IO_SIZES=(1M "1M,4K")\n'
        'TEST_DIRS=(["/data/two"]=1)\n'
        "for size in 64G; do ELBENCHO_FILE_SIZE=$size; done\n"
        "echo noisy output must not become a declaration\n",
    )
    result = apply_and_print(
        checkout,
        override,
        ("ELBENCHO_SCALE_THREAD_LIST", "ELBENCHO_SCALE_IO_SIZES", "TEST_DIRS"),
    )
    assert result.returncode == 0, result.stderr
    assert '([0]="1" [1]="2" [2]="4")' in result.stdout
    assert '([0]="1M" [1]="1M,4K")' in result.stdout
    test_dirs = next(
        line for line in result.stdout.splitlines() if "TEST_DIRS=" in line
    )
    assert "/data/two" in test_dirs and "/data/one" not in test_dirs
    assert "noisy output" in result.stderr
    assert (
        "VARIABLES=ELBENCHO_FILE_SIZE ELBENCHO_SCALE_IO_SIZES "
        "ELBENCHO_SCALE_THREAD_LIST TEST_DIRS"
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
            'd=$(filesystem_env_override_declarations "$2"); eval "$d"; '
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
        ("unset TEST_DIRS\ndeclare -a TEST_DIRS=(x)\n", "must be an associative"),
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
    md_override = write_override(
        checkout, "md.env", 'MDTEST_ITEMS_PER_DIR=500\nTEST_DIRS=(["/data/two"]=1)\n'
    )
    env_before = (checkout / "env.sh").read_text()
    io_args = ("--nodes", "1")
    md_args = ("--nodes", "1", "--tasks", "1")
    first, second = (IO, MD) if entry == IO else (MD, IO)
    first_override, second_override = (
        (io_override, md_override) if entry == IO else (md_override, io_override)
    )
    created = launch(
        checkout,
        first,
        "--batch",
        OVERRIDE_FLAG,
        first_override.relative_to(checkout),
        *(io_args if first == IO else md_args),
    )
    assert created.returncode == 0, created.stderr
    batch = batch_root(created.stdout)
    appended = launch(
        checkout,
        second,
        "--append",
        batch,
        OVERRIDE_FLAG,
        second_override,
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
    assert yaml.safe_load((groups["io"] / "env_used.yaml").read_text())[
        "ELBENCHO_SCALE_THREAD_LIST"
    ] == ["1", "2"]

    # Change both inputs after preparation: saved definitions are unaffected.
    (checkout / "env.sh").write_text(env_before + "\nexport ELBENCHO_LIVEINT=999\n")
    io_override.write_text("ELBENCHO_LIVEINT=777\n")
    md_override.write_text("MDTEST_ITEMS_PER_DIR=777\n")
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


@pytest.mark.parametrize(
    ("entry", "args", "message"),
    [
        (IO, ("--resume", "/missing"), SAVED_ONLY),
        (IO, ("--status", "/missing"), SAVED_ONLY),
        (IO, ("--delete-only", "/data/one/x"), SAVED_ONLY),
        (MD, ("--resume", "/missing"), SAVED_ONLY),
        (MD, ("--collect", "/missing"), SAVED_ONLY),
        (IO, ("--start", "{batch}"), "rejects workload arguments (--env-override)"),
        (IO, (OVERRIDE_FLAG, "{file}", "--nodes", "1"), "only once"),
        (
            MD,
            ("--batch", OVERRIDE_FLAG, "{file}", "--nodes", "1", "--tasks", "1"),
            "only once",
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


@pytest.mark.parametrize(
    ("path", "message"),
    [("overrides/missing.env", "not found"), ("overrides", "regular file")],
)
def test_override_path_must_be_a_readable_file(checkout, path, message):
    result = launch(checkout, IO, "--batch", OVERRIDE_FLAG, path, "--nodes", "1")
    assert result.returncode == 1 and message in result.stderr


def test_override_names_are_snapshotted_and_cover_every_default():
    """Every overridable name must survive later env.sh reloads."""
    library = (ROOT / "lib/env_functions.sh").read_text()
    names = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; filesystem_env_override_names | cut -f1',
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
