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

"""Prepared batch assembly, immutable provenance and local CLI contracts."""

from pathlib import Path
import hashlib
import os
import shlex
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
IO = "nv-elbencho-sweep.sh"
MD = "nv-mdtest-elbencho.sh"


@pytest.fixture(name="checkout")
def checkout_fixture(tmp_path):
    """Real launchers/libraries with a bounded, completely local environment."""
    base = tmp_path / "checkout's space"
    base.mkdir()
    shutil.copytree(ROOT / "lib", base / "lib")
    shutil.copytree(ROOT / "storage-tests/fs", base / "storage-tests/fs")
    (base / "utils").mkdir()
    (base / "utils/elbencho").write_text("#!/usr/bin/env bash\nexit 0\n")
    (base / "utils/elbencho").chmod(0o755)
    (base / "hosts").write_text("first,second\n")
    text = (ROOT / "env.sh.template").read_text()
    overrides = f"""
export EXECUTION_SUBSTRATE=ssh
export SSH_HOST_LIST={shlex.quote(str(base / 'hosts'))}
export SSH_USER=tester
export RESULTS_DIR={shlex.quote(str(base / 'results'))}
export LOGS_DIR={shlex.quote(str(base / 'logs'))}
TEST_DIRS=(["/data/one"]=1)
export ELBENCHO_SCALE_THREAD_LIST=(1)
export ELBENCHO_SCALE_IO_SIZES=(4K)
export ELBENCHO_IODEPTH_LIST=(1)
export MDTEST_BRANCH_FACTOR=1 MDTEST_ITEMS_PER_DIR=2 MDTEST_ITERATIONS=1
"""
    (base / "env.sh").write_text(
        text.replace("# STORAGE_SCALE_TEST_INTEGRATION_OVERRIDES", overrides)
    )
    return base


def launch(checkout, entry, *args):
    """Run a public entry point, not a separate test-only reifier."""
    return subprocess.run(
        ["bash", str(checkout / "storage-tests/fs" / entry), *map(str, args)],
        cwd=checkout,
        env={
            key: value
            for key, value in os.environ.items()
            if key != "EXECUTION_SUBSTRATE"
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def create(checkout, entry=IO):
    args = ("--nodes", "1") if entry == IO else ("--nodes", "1", "--tasks", "1")
    result = launch(checkout, entry, "--batch", *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STORAGE_SCALE_TEST_BATCH_EXECUTIONS=0001-0001" in result.stdout
    return next((checkout / "results").glob("filesystem-batch-*"))


def script(checkout, source):
    """Run shared helpers under the actual saved environment."""
    return subprocess.run(
        [
            "bash",
            "-ceu",
            'source "$1/env.sh"; source "$1/lib/_elbencho_functions.sh"; ' + source,
            "batch-test",
            str(checkout),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize("entry", [IO, MD])
def test_batch_preparation_is_local_and_both_group_orders_work(checkout, entry):
    batch = create(checkout, entry)
    other = MD if entry == IO else IO
    args = ("--nodes", "2", "--tasks", "3") if other == MD else ("--nodes", "2")
    result = launch(checkout, other, "--append", batch, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STORAGE_SCALE_TEST_BATCH_EXECUTIONS=0002-0002" in result.stdout
    data = (batch / "batch-manifest.tsv").read_text()
    assert "revision\t2" in data
    assert "execution\t0001\t0001\t" in data
    assert "execution\t0002\t0002\t" in data
    assert not (batch / "batch-sealed.sha256").exists()
    assert not (batch / "kubernetes").exists()
    assert all(
        path.read_text() == "PENDING\n"
        for path in (batch / "executions").glob("*.status")
    )
    for entrypoint in (IO, MD):
        status = launch(checkout, entrypoint, "--status", batch)
        assert status.returncode == 0, status.stderr
        assert "BATCH=DRAFT" in status.stdout
        assert "EXECUTIONS_PENDING=2" in status.stdout


def test_append_repeated_coordinates_keeps_own_settings_and_global_targets(checkout):
    batch = create(checkout)
    with (checkout / "env.sh").open("a") as stream:
        stream.write(
            '\nTEST_DIRS=(["/data/two"]=1)\nexport ELBENCHO_SCALE_READ_WRITE_DURATION=9\n'
        )
    result = launch(checkout, IO, "--append", batch, "--nodes", "1")
    assert result.returncode == 0, result.stderr
    definitions = [
        (batch / "executions" / f"{number:04d}.sh").read_text() for number in (1, 2)
    ]
    assert "/data/one" in definitions[0] and "/data/two" not in definitions[0]
    assert (
        "/data/two" in definitions[1]
        and "ELBENCHO_SCALE_READ_WRITE_DURATION=9" in definitions[1]
    )
    assert "-e0002" in definitions[1] and "-e0001" not in definitions[1]
    assert "groups/0001/" in definitions[0] and "groups/0002/" in definitions[1]


@pytest.mark.parametrize("entry", [IO, MD])
def test_append_changes_generated_basename_not_operator_root_text(checkout, entry):
    batch = create(checkout)
    datestamp = batch.name.removeprefix("filesystem-batch-")
    configured_root = f"/data/retained-{datestamp}-e0001"
    with (checkout / "env.sh").open("a", encoding="utf-8") as stream:
        stream.write(f"\nTEST_DIRS=([{shlex.quote(configured_root)}]=1)\n")
        if entry == IO:
            stream.write(
                "export ELBENCHO_FILE_LAYOUT=shared-directory\n"
                "export ELBENCHO_FILES_PER_NODE=1\n"
            )
    args = ("--nodes", "1") if entry == IO else ("--nodes", "1", "--tasks", "1")
    result = launch(checkout, entry, "--append", batch, *args)
    assert result.returncode == 0, result.stdout + result.stderr
    definition = batch / "executions/0002.sh"
    inspected = script(
        checkout,
        f'source {shlex.quote(str(definition))}; printf "%s\\n" '
        '"$ELBENCHO_RUN_TEST_DIR_SUFFIX" "$ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV" '
        '"$ELBENCHO_RUN_GENERATED_TEST_ROOT" "${ELBENCHO_READ_HOST_ROTATE_STEPS:-}"',
    )
    assert inspected.returncode == 0, inspected.stderr
    prefix = "elbencho-sweep" if entry == IO else "mdtest-elbencho"
    suffix, generated, root, rotation = inspected.stdout.splitlines()
    assert suffix == f"-{datestamp}-e0002"
    assert generated == f"{configured_root}/{prefix}-target-1{suffix}"
    assert root == (configured_root if entry == IO else "")
    assert rotation == ("0" if entry == IO else "")
    for field in (
        "ELBENCHO_RUN_TEST_DIR_SUFFIX",
        "ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV",
    ):
        assert definition.read_text().count(f"export {field}=") == 1


def test_append_read_from_does_not_rewrite_existing_dataset_identity(checkout):
    batch = create(checkout)
    datestamp = batch.name.removeprefix("filesystem-batch-")
    read_from = f"/data/one/retained-{datestamp}-e0001"
    result = launch(
        checkout, IO, "--append", batch, "--nodes", "1", "--read-from", read_from
    )
    assert result.returncode == 0, result.stdout + result.stderr
    inspected = script(
        checkout,
        f'source {shlex.quote(str(batch / "executions/0002.sh"))}; '
        'printf "%s\\n" "$ELBENCHO_SWEEP_READ_FROM" "$ELBENCHO_RUN_TEST_DIR_SUFFIX" '
        '"$ELBENCHO_TREEFILE_CACHE_PATH" "$ELBENCHO_TREEFILE_CACHE_BASENAME"',
    )
    assert inspected.returncode == 0, inspected.stderr
    actual_read, suffix, cache_path, cache_basename = inspected.stdout.splitlines()
    assert actual_read == read_from
    assert suffix == f"-{datestamp}-e0002"
    assert cache_path == f"{read_from}/{cache_basename}"


def test_append_retains_group_local_read_rotation(checkout):
    batch = create(checkout)
    with (checkout / "env.sh").open("a", encoding="utf-8") as stream:
        stream.write("\nexport ELBENCHO_SCALE_THREAD_LIST=(1 2)\n")
    result = launch(checkout, IO, "--append", batch, "--nodes", "1")
    assert result.returncode == 0, result.stdout + result.stderr
    inspected = script(
        checkout,
        f'for id in 0002 0003; do source {shlex.quote(str(batch / "executions"))}/$id.sh; '
        'printf "%s\\n" "$ELBENCHO_READ_HOST_ROTATE_STEPS"; done',
    )
    assert inspected.returncode == 0, inspected.stderr
    assert inspected.stdout.splitlines() == ["0", "1"]


@pytest.mark.parametrize("entry", [IO, MD])
@pytest.mark.parametrize("operation", ["--resume", "--status"])
@pytest.mark.parametrize(
    "marker",
    ["batch-profile.tsv", "batch-sealed.sha256", "batch-sealed-environment.sha256"],
)
def test_missing_manifest_cannot_downgrade_batch_lifecycle(
    checkout, entry, operation, marker
):
    batch = create(checkout)
    (batch / "batch-manifest.tsv").unlink()
    (batch / "batch-profile.tsv").unlink()
    (batch / marker).write_text("batch marker\n", encoding="utf-8")
    (checkout / "env.sh").write_text(
        "invalid current environment ' \n", encoding="utf-8"
    )
    result = launch(checkout, entry, operation, batch)
    assert result.returncode != 0
    assert "missing or symlinked manifest" in result.stderr
    assert "unexpected EOF" not in result.stderr


@pytest.mark.parametrize(
    "field,override",
    [("SSH_HOST_LIST_CONTENT", "hosts"), ("SSH_USER", "export SSH_USER=different")],
)
def test_append_rejects_frozen_environment_drift_without_commit(
    checkout, field, override
):
    batch = create(checkout)
    manifest = (batch / "batch-manifest.tsv").read_bytes()
    if override == "hosts":
        (checkout / "hosts").write_text("replacement\n")
    else:
        with (checkout / "env.sh").open("a") as stream:
            stream.write(f"\n{override}\n")
    result = launch(checkout, MD, "--append", batch, "--nodes", "1", "--tasks", "1")
    assert result.returncode != 0
    assert field in result.stderr
    assert (batch / "batch-manifest.tsv").read_bytes() == manifest


def test_sealed_append_rejected_before_current_env_load(checkout):
    batch = create(checkout)
    seal = hashlib.sha256((batch / "batch-manifest.tsv").read_bytes()).hexdigest()
    (batch / "batch-sealed-environment.sha256").write_text(
        hashlib.sha256((batch / "env_used.sh").read_bytes()).hexdigest() + "\n"
    )
    (batch / "batch-sealed.sha256").write_text(seal + "\n")
    (checkout / "env.sh").write_text("this is not valid shell ' \n")
    result = launch(checkout, MD, "--append", batch, "--nodes", "1", "--tasks", "1")
    assert result.returncode != 0 and "sealed batch is immutable" in result.stderr
    assert "Failed to source" not in result.stderr
    result = launch(checkout, IO, "--status", batch)
    assert result.returncode == 0, result.stderr
    assert "SEALED" in result.stdout


def test_manifest_ignores_uncommitted_definitions(checkout):
    batch = create(checkout)
    (batch / "executions/0002.sh").write_text("export nodes=999\n")
    result = script(
        checkout,
        f'list_elbencho_execution_ids {shlex.quote(str(batch / "executions"))}',
    )
    assert result.returncode == 0 and result.stdout.strip() == "0001"


def test_insufficient_saved_pool_does_not_seal(checkout):
    result = launch(checkout, IO, "--batch", "--nodes", "3")
    assert result.returncode == 0, result.stderr
    batch = next((checkout / "results").glob("filesystem-batch-*"))
    result = launch(checkout, MD, "--start", batch)
    assert result.returncode != 0 and "saved SSH pool contains 2" in result.stderr
    assert not (batch / "batch-sealed.sha256").exists()


def test_invalid_preparation_does_not_create_batch(checkout):
    result = launch(checkout, MD, "--batch", "--nodes", "1", "--tasks", "0")
    assert result.returncode != 0
    assert not list((checkout / "results").glob("filesystem-batch-*"))


@pytest.mark.parametrize(
    "arguments",
    [
        ("--batch", "--resume", "missing"),
        ("--batch", "--delete-only", "/data/foo"),
        ("--start", "missing", "--nodes", "1"),
    ],
)
def test_batch_operation_grammar_rejects_ambiguous_requests(checkout, arguments):
    result = launch(checkout, IO, *arguments)
    assert result.returncode != 0
    assert "filesystem batch" in result.stderr


@pytest.mark.parametrize("entry", [IO, MD])
@pytest.mark.parametrize(
    "arguments,expected",
    [
        (
            ("--nodes", "2", "--batch", "existing", "-b", "--write-no-read"),
            "use --append existing",
        ),
        (("--batch", "existing", "--nodes", "2"), "takes no directory"),
        (("--append",), "--append requires a results directory"),
        (("--append", "-b"), "--append requires a results directory"),
        (("--start", "--nodes", "2"), "--start requires a results directory"),
        (("--collect", ""), "--collect requires a results directory"),
        (("--batch", "--batch"), "--batch may be specified only once"),
        (
            ("--batch", "--append", "existing"),
            "--batch and --append are mutually exclusive",
        ),
        (
            ("--start", "existing", "--batch"),
            "--start and --batch are mutually exclusive",
        ),
        (("--append", "existing"), "--append requires a prepared batch: existing"),
        (("--start", "existing"), "--start requires a prepared batch: existing"),
        (
            ("--batch", "--delete-only", "/data/tree"),
            "--delete-only cannot be combined with --batch",
        ),
    ],
)
def test_batch_argument_errors_identify_the_flag_before_loading_env(
    checkout, entry, arguments, expected
):
    """Malformed batch commands need no working configuration or remote adapter."""
    (checkout / "env.sh").write_text("echo UNEXPECTED_ENV_LOAD >&2; exit 73\n")
    result = launch(checkout, entry, *arguments)
    assert result.returncode != 0
    assert expected in result.stderr
    assert "UNEXPECTED_ENV_LOAD" not in result.stderr
    assert not (checkout / "results").exists()


@pytest.mark.parametrize("entry", [IO, MD])
def test_batch_directory_hint_quotes_spaces_and_apostrophes(checkout, entry):
    directory = "someone's saved batch"
    result = launch(checkout, entry, "--batch", directory)
    assert result.returncode != 0
    suggested = result.stderr.split("use --append ", 1)[1].split(" to add", 1)[0]
    assert shlex.split(suggested) == [directory]


@pytest.mark.parametrize("entry", [IO, MD])
def test_batch_lifecycle_workload_error_identifies_operation(checkout, entry):
    batch = create(checkout)
    result = launch(checkout, entry, "--start", batch, "--nodes", "2")
    assert result.returncode != 0
    assert "--start rejects workload arguments (--nodes)" in result.stderr
    assert "set them with --batch or --append" in result.stderr
    assert not (batch / "batch-sealed.sha256").exists()


@pytest.mark.parametrize("entry", [IO, MD])
def test_batch_workload_flags_remain_order_independent(checkout, entry):
    arguments = ("--nodes", "1", "--batch", "-b", "--write-no-read")
    if entry == MD:
        arguments = ("--tasks", "1", "--batch", "--nodes", "1")
    result = launch(checkout, entry, *arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    batch = next((checkout / "results").glob("filesystem-batch-*"))
    append = ("--nodes", "2", "--append", batch, "-b", "--write-no-read")
    if entry == MD:
        append = ("--tasks", "1", "--append", batch, "--nodes", "2")
    result = launch(checkout, entry, *append)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STORAGE_SCALE_TEST_BATCH_EXECUTIONS=0002-0002" in result.stdout


@pytest.mark.parametrize("flag", ["--nodes", "--tasks", "--single-dir-file-target"])
def test_metadata_duplicate_workload_flags_are_not_silent(checkout, flag):
    result = launch(checkout, MD, flag, "1", flag, "2")
    assert result.returncode != 0
    assert f"{flag} may be specified only once" in result.stderr


@pytest.mark.parametrize("entry", [IO, MD])
def test_kubernetes_batch_start_loads_helpers_in_preflight_and_dispatch(
    checkout, entry
):
    """The actual batch path must tolerate both loads under strict Bash."""
    with (checkout / "env.sh").open("a") as environment:
        environment.write(
            "\nexport EXECUTION_SUBSTRATE=kubectl\nTEST_DIRS=([data/one]=1)\n"
            "export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc\n"
            "export KUBECTL_NODE_SELECTOR=benchmark=true\n"
        )
    batch = create(checkout, entry)
    body = r"""
        SCRIPT_DIR="$1/storage-tests/fs"
        source "$1/lib/_batch_functions.sh"
        source() {
            builtin source "$@" || return
            if [[ "$1" == */_nv-elbencho-kubectl-functions.sh ]]; then
                kubectl_validate_runtime_configuration() { :; }
                kubectl_validate_cluster_identity() { :; }
                kubectl_discover_candidate_nodes() { printf 'worker\n' > "$2"; }
                kubectl_submit_sweep() {
                    [[ "$2" == 1 && "$KUBECTL_CONTROL_LOGICAL_ROOT" == data/one ]]
                    printf 'DISPATCH_REACHED\n'
                }
            fi
        }
        _elbencho_batch_run start "$2"
    """
    result = subprocess.run(
        ["bash", "-ceu", body, "batch-start", str(checkout), str(batch)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DISPATCH_REACHED" in result.stdout
    assert "readonly variable" not in result.stderr
    assert (batch / "batch-sealed.sha256").is_file()


def test_batch_status_labels_counts_as_local(checkout):
    batch = create(checkout)
    result = launch(checkout, IO, "--status", batch)
    assert result.returncode == 0, result.stderr
    assert "PROGRESS_SOURCE=LOCAL" in result.stdout
    assert "EXECUTION_SCOPE=BATCH" in result.stdout
    assert "STATE=NOT_STARTED" in result.stdout
    assert "EXECUTIONS_PENDING=1" in result.stdout
    assert "NEXT_ACTION=START" in result.stdout


@pytest.mark.parametrize(
    "cell,owner,state,next_action",
    [
        ("RUNNING", 0, "RUNNING", "WAIT"),
        ("PENDING", 0, "BETWEEN_EXECUTIONS", "WAIT"),
        ("SUCCESS", 0, "AWAITING_COMPLETION", "WAIT"),
        ("FAILED", 1, "FAILED", "RESUME"),
        ("SUCCESS", 1, "SUCCESS", "NONE"),
        ("SUCCESS", 2, "UNKNOWN", "INSPECT"),
    ],
)
def test_local_batch_status_distinguishes_owner_from_cell_state(
    checkout, cell, owner, state, next_action
):
    """An empty running count is not proof of executor exit on SSH or Slurm."""
    batch = create(checkout)
    (batch / "batch-sealed.sha256").touch()
    (batch / "executions/.dispatch.lock").mkdir()
    (batch / "executions/0001.status").write_text(cell + "\n")
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1/lib/_batch_functions.sh"; '
            f"_elbencho_dispatch_lock_is_active() {{ return {owner}; }}; "
            '_elbencho_batch_status "$2"',
            "status-test",
            str(checkout),
            str(batch),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert fields["BATCH"] == "SEALED"
    assert fields["STATE"] == state
    assert fields["NEXT_ACTION"] == next_action
    assert fields["EXECUTION_SCOPE"] == "BATCH"
