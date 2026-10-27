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
import yaml

from tests.filesystem_checkout_test_support import make_filesystem_checkout
from tests.kubectl_segment_test_helpers import (
    DEFAULT_SEQUENCER_IMAGE,
    SEQUENCER_IMAGE as MIRROR_SEQUENCER_IMAGE,
)

ROOT = Path(__file__).resolve().parents[1]
IO = "nv-elbencho-sweep.sh"
MD = "nv-mdtest-elbencho.sh"


@pytest.fixture(name="checkout")
def checkout_fixture(tmp_path):
    """Real launchers/libraries with a bounded, completely local environment."""
    return make_filesystem_checkout(tmp_path, ROOT)


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


def test_frozen_profile_ignores_retired_modules_field(checkout, tmp_path):
    """Batches prepared before MODULES was removed still accept appends."""
    (tmp_path / "common-fields.tsv").write_text(
        'account\tproj\nMODULES\tdeclare -a MODULES=([0]="cuda")\n'
    )
    (tmp_path / "candidate.tsv").write_text("account\tproj\n")
    compare = (
        'source "$1/lib/_batch_functions.sh"; '
        '_elbencho_batch_compare_profile "$2" "$2/candidate.tsv"'
    )
    result = subprocess.run(
        ["bash", "-c", compare, "profile-test", str(checkout), str(tmp_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    (tmp_path / "candidate.tsv").write_text("account\tother\n")
    result = subprocess.run(
        ["bash", "-c", compare, "profile-test", str(checkout), str(tmp_path)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    assert "differs: account" in result.stderr


@pytest.mark.parametrize("site_module", [True, False])
def test_slurm_batch_start_loads_the_slurm_module(checkout, tmp_path, site_module):
    """--start and --resume bypass env.sh, so preflight loads the slurm module."""
    tools = tmp_path / "slurm-module-bin"
    tools.mkdir()
    (tools / "sbatch").write_text("#!/bin/sh\nexit 0\n")
    (tools / "sbatch").chmod(0o755)
    # Only grep, so an ambient sbatch cannot satisfy the no-module case.
    isolated = tmp_path / "isolated-bin"
    isolated.mkdir()
    (isolated / "grep").symlink_to(shutil.which("grep"))
    available = "slurm/23.11" if site_module else ""
    body = f"""
        source "$1/lib/env_functions.sh"
        source "$1/lib/_batch_functions.sh"
        PATH={shlex.quote(str(isolated))}
        module() {{
            case "$1" in
                avail) printf '%s\\n' {shlex.quote(available)} >&2 ;;
                load) [[ "$2" == slurm ]] && PATH={shlex.quote(str(tools))}:$PATH ;;
            esac
        }}
        max_nodes_remaining_executions() {{ echo 1; }}
        _elbencho_batch_validate_cells() {{ :; }}
        _elbencho_batch_validate_executable() {{ :; }}
        EXECUTION_SUBSTRATE=slurm
        _elbencho_batch_preflight /unused
    """
    result = subprocess.run(
        ["bash", "-c", body, "preflight-test", str(checkout)],
        text=True,
        capture_output=True,
        check=False,
    )
    if site_module:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert "sbatch is unavailable" in result.stderr


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
    # The claim helpers are stubbed: this test is about sourcing, not claims.
    stubs = (
        "kubectl_validate_test_dirs() { :; }\n"
        "kubectl_parse_test_dir_key() { printf 'test-ns\\ttest-pvc\\tdata/one\\n'; }\n"
    )
    with (checkout / "env.sh").open("a") as environment:
        environment.write(
            "\nexport EXECUTION_SUBSTRATE=kubectl\n"
            'TEST_DIRS=(["test-pvc:data/one"]=1)\n'
            "export KUBECTL_NAMESPACE=test-ns\n"
            "export KUBECTL_NODE_SELECTOR=benchmark=true\n" + stubs
        )
    batch = create(checkout, entry)
    body = stubs + r"""
        SCRIPT_DIR="$1/storage-tests/fs"
        source "$1/lib/_batch_functions.sh"
        source() {
            builtin source "$@" || return
            if [[ "$1" == */_nv-elbencho-kubectl-functions.sh ]]; then
                kubectl_batch_preflight() { :; }
                kubectl_select_control_root() {
                    printf -v "$1" %s data/one
                    printf -v "$2" %s /mnt/storage-scale-test/test-pvc/data/one
                }
                kubectl_set_control_layout() {
                    export KUBECTL_CONTROL_LOGICAL_ROOT="$1" KUBECTL_CONTROL_TEST_ROOT="$2"
                    export KUBECTL_CONTROL_ROOT="$2/.storage-scale-test"
                }
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


def test_kubernetes_read_from_accepts_the_printed_pod_path(checkout):
    """--read-from takes the Pod path --write-only prints and saves claim:path."""
    with (checkout / "env.sh").open("a") as environment:
        environment.write(
            "\nexport EXECUTION_SUBSTRATE=kubectl\n"
            'TEST_DIRS=(["test-pvc:data/one"]=1)\n'
            "export KUBECTL_NAMESPACE=test-ns\n"
            "export KUBECTL_NODE_SELECTOR=benchmark=true\n"
        )
    result = launch(
        checkout, IO, "--batch", "--nodes", "1",
        "--read-from", "/mnt/storage-scale-test/test-pvc/data/one/ds",
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    batch = next((checkout / "results").glob("filesystem-batch-*"))
    definition = (batch / "executions/0001.sh").read_text(encoding="utf-8")
    assert "export ELBENCHO_SWEEP_READ_FROM=test-ns/test-pvc:data/one/ds" in definition


def test_batch_status_labels_counts_as_local(checkout):
    batch = create(checkout)
    result = launch(checkout, IO, "--status", batch)
    assert result.returncode == 0, result.stderr
    assert "PROGRESS_SOURCE=LOCAL" in result.stdout
    assert "EXECUTION_SCOPE=BATCH" in result.stdout
    assert "STATE=NOT_STARTED" in result.stdout
    assert "EXECUTIONS_PENDING=1" in result.stdout
    assert "NEXT_ACTION=START" in result.stdout


def test_batch_status_and_publish_report_each_groups_storage_target(checkout):
    batch = create(checkout)
    target = checkout / "named.env"
    target.write_text(
        'STORAGE_TARGET_NAME=vendor-b:fs-1\nTEST_DIRS=(["/data/two"]=1)\n'
    )
    appended = launch(
        checkout, MD, "--append", batch, "--nodes", "1", "--tasks", "1",
        "--target", target,
    )  # fmt: skip
    assert appended.returncode == 0, appended.stdout + appended.stderr
    assert "STORAGE_SCALE_TEST_BATCH_GROUP_TARGET=" not in appended.stdout
    assert (
        "STORAGE_SCALE_TEST_BATCH_GROUP_0002_TARGET=vendor-b:fs-1\n" in appended.stdout
    )
    assert "_CLAIM=" not in appended.stdout
    result = launch(checkout, IO, "--status", batch)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[lines.index("NEXT_ACTION=START") + 1 :] == [
        "GROUP_0001_KIND=io",
        "GROUP_0001_TARGET=/data/one",
        "GROUP_0002_KIND=mdtest",
        "GROUP_0002_TARGET=vendor-b:fs-1",
    ]
    assert len({line.partition("=")[0] for line in lines}) == len(lines)


@pytest.mark.parametrize(
    "cell,owner,state,next_action",
    [
        ("RUNNING", 0, "RUNNING", "WAIT"),
        ("PENDING", 0, "RUNNING", "WAIT"),
        ("SUCCESS", 0, "RUNNING", "WAIT"),
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


def failed_batch(checkout, *cells):
    """A sealed two-group batch whose executions have the given states."""
    batch = create(checkout)
    appended = launch(checkout, MD, "--append", batch, "--nodes", "1", "--tasks", "1")
    assert appended.returncode == 0, appended.stdout + appended.stderr
    (batch / "batch-sealed.sha256").touch()
    for number, cell in enumerate(cells, 1):
        (batch / f"executions/{number:04d}.status").write_text(cell + "\n")
    return batch


def status_lines(checkout, batch, substrate="ssh"):
    """Run the shared batch status for SSH or Slurm and return its lines."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1/lib/_batch_functions.sh"; '
            'EXECUTION_SUBSTRATE="$3"; _elbencho_batch_status "$2"',
            "status-test",
            str(checkout),
            str(batch),
            substrate,
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def write_cell_logs(batch, *ids):
    """Create executions/<id>.log for each ID, as SSH and Slurm dispatch do."""
    for execution in ids:
        (batch / f"executions/{execution}.log").write_text(f"cell {execution}\n")


@pytest.mark.parametrize("substrate", ["ssh", "slurm"])
def test_failed_batch_status_names_group_and_cell_log(checkout, substrate):
    """FAILED_* follows NEXT_ACTION and names the failed cell's own log."""
    batch = failed_batch(checkout, "SUCCESS", "FAILED")
    write_cell_logs(batch, "0001", "0002")
    lines = status_lines(checkout, batch, substrate)
    start = lines.index("NEXT_ACTION=RESUME") + 1
    assert lines[start : start + 2] == ["FAILED_EXECUTION=0002", "FAILED_GROUP=0002"]
    # The checkout path has a space and quote, so the value is %q-escaped.
    key, _, value = lines[start + 2].partition("=")
    assert key == "FAILED_LOG"
    assert shlex.split(value) == [str(batch / "executions/0002.log")]
    assert lines[start + 3] == "GROUP_0001_KIND=io"


def test_failed_batch_status_omits_the_log_when_none_exists(checkout):
    batch = failed_batch(checkout, "SUCCESS", "FAILED")
    write_cell_logs(batch, "0001")
    lines = status_lines(checkout, batch)
    assert "FAILED_EXECUTION=0002" in lines
    assert "FAILED_GROUP=0002" in lines
    assert not any(line.startswith("FAILED_LOG=") for line in lines)


def test_failed_batch_status_reports_the_first_failure_in_manifest_order(checkout):
    batch = failed_batch(checkout, "FAILED", "FAILED")
    lines = status_lines(checkout, batch)
    assert [line for line in lines if line.startswith("FAILED_")] == [
        "FAILED_EXECUTION=0001",
        "FAILED_GROUP=0001",
    ]
    assert "EXECUTIONS_FAILED=2" in lines


@pytest.mark.parametrize("cells", [("SUCCESS", "SUCCESS"), ("PENDING", "RUNNING")])
def test_batch_status_without_failures_has_no_failed_keys(checkout, cells):
    batch = failed_batch(checkout, *cells)
    write_cell_logs(batch, "0001", "0002")
    lines = status_lines(checkout, batch)
    assert not any(line.startswith("FAILED_") for line in lines)


MANIFEST = "batch-manifest.tsv"
GROUP = "STORAGE_SCALE_TEST_BATCH_GROUP"
GROUP_TARGET = f"{GROUP}_TARGET"
MDTEST = "MDTEST_ITEMS_PER_DIR"
ARGS = {IO: ("--nodes", "1"), MD: ("--nodes", "1", "--tasks", "1")}


def target_file(checkout, name, text):
    """Write a --target file beneath the checkout and return its path."""
    path = checkout / "targets" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(text)
    return path


def two_targets(checkout):
    """Target files for two filesystems: the first is named, the second is not."""
    vendor_a = target_file(
        checkout,
        "vendor-a.env",
        'STORAGE_TARGET_NAME=vendor-a\nTEST_DIRS=(["/data/a"]=1)\n',
    )
    vendor_b = target_file(
        checkout, "vendor-b.env", 'TEST_DIRS=(["/data/b"]=1)\nFS_MAX_NODE_IOPS=5000\n'
    )
    return vendor_a, vendor_b


def output_keys(stdout):
    """Return the STORAGE_SCALE_TEST_BATCH_* output lines as (key, value) pairs."""
    return [
        tuple(line.split("=", 1))
        for line in stdout.splitlines()
        if line.startswith("STORAGE_SCALE_TEST_BATCH_")
    ]


def batch_dir(stdout):
    """Return the results directory a --batch printed."""
    return Path(dict(output_keys(stdout))["STORAGE_SCALE_TEST_BATCH_RESULTS"])


def groups(batch):
    """Return the committed (gid, kind, relative) group rows."""
    return [
        tuple(row.split("\t")[1:4])
        for row in (batch / MANIFEST).read_text().splitlines()
        if row.startswith("group\t")
    ]


def saved_group(batch, relative):
    """Return a group's parsed env_used.yaml."""
    return yaml.safe_load((batch / relative / "env_used.yaml").read_text())


def prepare_two_targets(checkout, *extra):
    """Batch an IO submission and append an mdtest one across two target files."""
    vendor_a, vendor_b = two_targets(checkout)
    targets = ("--target", vendor_a, "--target", vendor_b)
    first = launch(checkout, IO, "--batch", *targets, *extra, *ARGS[IO])
    assert first.returncode == 0, first.stdout + first.stderr
    batch = next((checkout / "results").glob("filesystem-batch-*"))
    second = launch(checkout, MD, "--append", batch, *targets, *extra, *ARGS[MD])
    assert second.returncode == 0, second.stdout + second.stderr
    return batch, first, second


def test_a_target_file_sees_its_own_directory_in_a_batch_as_in_a_plain_run(checkout):
    """BASH_SOURCE names the operator's file, not the staged private copy."""
    target = target_file(
        checkout,
        "relative.env",
        'TEST_DIRS=(["$(dirname "${BASH_SOURCE[0]}")/data"]=1)\n',
    )
    plain = launch(checkout, IO, "--target", target, *ARGS[IO])
    assert "Target file: " in plain.stdout, plain.stderr
    plain_dir = next((checkout / "results").glob("elbencho-*"))
    expected = [str(target.parent / "data")]
    assert (
        list(yaml.safe_load((plain_dir / "env_used.yaml").read_text())["TEST_DIRS"])
        == expected
    )
    batch = launch(checkout, IO, "--batch", "--target", target, *ARGS[IO])
    assert batch.returncode == 0, batch.stdout + batch.stderr
    root = batch_dir(batch.stdout)
    ((_, _, relative),) = groups(root)
    assert list(saved_group(root, relative)["TEST_DIRS"]) == expected


@pytest.mark.parametrize(
    ("origin_changed", "expected"), [(False, "origin"), (True, "stage")]
)
def test_only_an_origin_with_the_staged_content_is_evaluated(
    checkout, origin_changed, expected
):
    """An edited or missing origin falls back to the staged copy."""
    text = 'TEST_DIRS=(["$(dirname "${BASH_SOURCE[0]}")"]=1)\n'
    (checkout / "targets" / "stage").mkdir(parents=True)
    (checkout / "targets" / "origin").mkdir()
    staged = checkout / "targets" / "stage" / "t.env"
    origin = checkout / "targets" / "origin" / "t.env"
    staged.write_text(text)
    origin.write_text(text + "# edited\n" if origin_changed else text)
    result = script(
        checkout,
        f"d=$(filesystem_target_declarations {shlex.quote(str(staged))} "
        f'{shlex.quote(str(origin))}) || exit 1; eval "$d"; '
        'echo "${!TEST_DIRS[*]}"',
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(checkout / "targets" / expected)


def test_each_submission_expands_one_group_per_target_in_command_line_order(checkout):
    batch, first, second = prepare_two_targets(checkout)
    assert output_keys(first.stdout) == [
        ("STORAGE_SCALE_TEST_BATCH_RESULTS", str(batch)),
        (GROUP, "0001"),
        ("STORAGE_SCALE_TEST_BATCH_GROUPS", "0001,0002"),
        (f"{GROUP}_0001_TARGET", "vendor-a"),
        (f"{GROUP}_0002_TARGET", "/data/b"),
        ("STORAGE_SCALE_TEST_BATCH_EXECUTIONS", "0001-0002"),
    ]
    assert output_keys(second.stdout)[2:] == [
        ("STORAGE_SCALE_TEST_BATCH_GROUPS", "0003,0004"),
        (f"{GROUP}_0003_TARGET", "vendor-a"),
        (f"{GROUP}_0004_TARGET", "/data/b"),
        ("STORAGE_SCALE_TEST_BATCH_EXECUTIONS", "0003-0004"),
    ]
    # The legacy single-group key and any claim key are never printed.
    assert GROUP_TARGET not in {key for key, _ in output_keys(first.stdout)}
    assert "_CLAIM" not in first.stdout + second.stdout
    assert "revision\t4\n" in (batch / MANIFEST).read_text()
    rows = groups(batch)
    assert [(gid, kind) for gid, kind, _ in rows] == [
        ("0001", "io"), ("0002", "io"), ("0003", "mdtest"), ("0004", "mdtest"),
    ]  # fmt: skip
    vendor_a, vendor_b = two_targets(checkout)
    expected = [
        (vendor_a, "vendor-a", "/data/a"),
        (vendor_b, "/data/b", "/data/b"),
    ] * 2
    for (gid, _, relative), (path, name, root) in zip(rows, expected):
        saved = saved_group(batch, relative)
        assert saved["STORAGE_TARGET_NAME"] == name, gid
        assert list(saved["TEST_DIRS"]) == [root], gid
        # Each group records the operator's own target file, not a staged copy.
        assert saved["target_file"]["file"] == str(path), gid
        assert (
            saved["target_file"]["sha256"]
            == hashlib.sha256(path.read_bytes()).hexdigest()
        )
        assert saved["env_override"] is None
    # An FS_MAX_* a target omits keeps env.sh's value; one it sets replaces it.
    assert saved_group(batch, rows[1][2])["FS_MAX_NODE_IOPS"] == 5000
    assert saved_group(batch, rows[0][2])["FS_MAX_NODE_IOPS"] != 5000
    # Staged copies and their origins are never published.
    assert not any(batch.rglob(".target-file*"))
    assert not list((checkout / "results").glob(".group-staging.*"))
    status = launch(checkout, IO, "--status", batch)
    assert status.returncode == 0, status.stderr
    assert "GROUP_0002_TARGET=/data/b" in status.stdout
    assert "GROUP_0003_TARGET=vendor-a" in status.stdout


def test_a_target_is_never_named_by_env_sh(checkout):
    """STORAGE_TARGET_NAME in env.sh is cleared before every target file."""
    with (checkout / "env.sh").open("a") as stream:
        stream.write("\nexport STORAGE_TARGET_NAME=env-name\n")
    vendor_a, vendor_b = two_targets(checkout)
    created = launch(
        checkout, IO, "--batch", "--target", vendor_b, "--target", vendor_a, *ARGS[IO]
    )
    assert created.returncode == 0, created.stdout + created.stderr
    assert f"{GROUP}_0001_TARGET=/data/b\n" in created.stdout
    assert f"{GROUP}_0002_TARGET=vendor-a\n" in created.stdout
    # Without a target file the one target still comes from env.sh.
    batch = batch_dir(created.stdout)
    plain = launch(checkout, MD, "--append", batch, *ARGS[MD])
    assert plain.returncode == 0, plain.stderr
    assert f"{GROUP}_0003_TARGET=env-name\n" in plain.stdout


def test_overrides_apply_to_every_target_after_that_groups_target(checkout):
    """Each group applies exactly its own target file and then the overrides."""
    recipe = target_file(
        checkout,
        "recipe.env",
        "ELBENCHO_FILE_SIZE_MULTIPLIER=$((FS_MAX_NODE_IOPS / 10))\n"
        "MDTEST_ITEMS_PER_DIR=$((FS_MAX_NODE_IOPS / 100))\n",
    )
    batch, _, _ = prepare_two_targets(checkout, "--env-override", recipe)
    rows = groups(batch)
    assert len(rows) == 4
    # The override reads the target's own FS_MAX_NODE_IOPS: each target is
    # applied to its group first, so the two filesystems' groups differ.
    divisors = {"io": ("ELBENCHO_FILE_SIZE_MULTIPLIER", 10), "mdtest": (MDTEST, 100)}
    for gid, kind, relative in rows:
        saved = saved_group(batch, relative)
        assert [item["file"] for item in saved["env_override"]["files"]] == [
            str(recipe)
        ], gid
        name, divisor = divisors[kind]
        assert saved[name] == saved["FS_MAX_NODE_IOPS"] // divisor, gid
    assert saved_group(batch, rows[1][2])["ELBENCHO_FILE_SIZE_MULTIPLIER"] == 500
    assert saved_group(batch, rows[0][2])["ELBENCHO_FILE_SIZE_MULTIPLIER"] != 500
    assert saved_group(batch, rows[3][2])[MDTEST] == 50


def test_failure_while_staging_a_later_target_commits_nothing(checkout):
    batch, _, _ = prepare_two_targets(checkout)
    vendor_a, _ = two_targets(checkout)
    broken = target_file(
        checkout, "broken.env", 'TEST_DIRS=(["/data/c"]=1)\nELBENCHO_LIVEINT=5\n'
    )
    before = (batch / MANIFEST).read_bytes()
    result = launch(
        checkout, IO, "--append", batch, "--target", vendor_a, "--target", broken,
        "--nodes", "2",
    )  # fmt: skip
    assert result.returncode != 0
    assert "sets workload setting(s): ELBENCHO_LIVEINT" in result.stderr
    assert (batch / MANIFEST).read_bytes() == before
    assert not (batch / "groups/0005").exists()
    assert not list((checkout / "results").glob(".group-staging.*"))
    fresh = launch(
        checkout,
        IO,
        "--batch",
        "--target",
        vendor_a,
        "--target",
        broken,
        "--nodes",
        "2",
    )
    assert fresh.returncode != 0
    assert len(list((checkout / "results").glob("filesystem-batch-*"))) == 1


def test_retry_replaces_uncommitted_debris_for_every_new_group(checkout):
    batch, _, _ = prepare_two_targets(checkout)
    vendor_a, vendor_b = two_targets(checkout)
    for gid in ("0005", "0006"):
        (batch / "groups" / gid / "debris").mkdir(parents=True)
    result = launch(
        checkout, IO, "--append", batch, "--target", vendor_a, "--target", vendor_b,
        "--nodes", "2",
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    assert "STORAGE_SCALE_TEST_BATCH_GROUPS=0005,0006" in result.stdout
    assert "revision\t6\n" in (batch / MANIFEST).read_text()
    assert not list(batch.glob("groups/*/debris"))


def test_single_group_publication_prints_its_per_group_key(checkout):
    """One group gets its own per-group key, as each of several groups does."""
    vendor_a, _ = two_targets(checkout)
    for arguments, name in (((), "/data/one"), (("--target", vendor_a), "vendor-a")):
        result = launch(checkout, IO, "--batch", *arguments, *ARGS[IO])
        assert result.returncode == 0, result.stderr
        keys = [key for key, _ in output_keys(result.stdout)]
        assert keys == [
            "STORAGE_SCALE_TEST_BATCH_RESULTS",
            GROUP,
            f"{GROUP}_0001_TARGET",
            "STORAGE_SCALE_TEST_BATCH_EXECUTIONS",
        ]
        assert f"{GROUP}_0001_TARGET={name}\n" in result.stdout
        # Remove the batch so the second pass has a fresh results directory.
        for path in (checkout / "results").glob("filesystem-batch-*"):
            subprocess.run(["rm", "-r", str(path)], check=True)


@pytest.mark.parametrize("spelling", ["same", "relative", "symlink"])
def test_the_same_target_file_twice_is_rejected(checkout, spelling):
    vendor_a, _ = two_targets(checkout)
    link = checkout / "targets" / "link.env"
    link.symlink_to(vendor_a)
    other = {
        "same": vendor_a,
        "relative": Path("targets/../targets/vendor-a.env"),
        "symlink": link,
    }[spelling]
    result = launch(
        checkout, IO, "--batch", "--target", vendor_a, "--target", other, *ARGS[IO]
    )
    assert result.returncode == 1
    assert "--target given more than once for the same file" in result.stderr
    assert not (checkout / "results").exists() or not any(
        (checkout / "results").glob("*")
    )


@pytest.mark.parametrize(
    ("path", "message"),
    [("targets/missing.env", "not found"), ("targets", "regular file")],
)
def test_target_path_must_be_a_readable_file(checkout, path, message):
    two_targets(checkout)
    result = launch(checkout, IO, "--batch", "--target", path, *ARGS[IO])
    assert result.returncode == 1 and message in result.stderr


@pytest.mark.parametrize("operation", ["--start", "--status", "--cancel", "--collect"])
@pytest.mark.parametrize("entry", [IO, MD])
def test_lifecycle_operations_reject_target(checkout, entry, operation):
    batch = create(checkout)
    vendor_a, _ = two_targets(checkout)
    result = launch(checkout, entry, operation, batch, "--target", vendor_a)
    assert result.returncode == 1
    assert f"{operation} rejects workload arguments (--target)" in result.stderr


def test_resume_rejects_target(checkout):
    vendor_a, _ = two_targets(checkout)
    result = launch(checkout, IO, "--resume", "/missing", "--target", vendor_a)
    assert result.returncode == 1
    assert "--target applies only to a new submission" in result.stderr


def test_kubernetes_batch_freezes_only_shared_settings(checkout):
    """Claims vary per group; PV/PVC and the old list are no longer batch settings."""
    result = script(
        checkout,
        "EXECUTION_SUBSTRATE=kubectl; _elbencho_batch_common_names | sed -n '/^KUBECTL/p'",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        "KUBECTL_NAMESPACE",
        "KUBECTL_NODE_SELECTOR",
        "KUBECTL_ELBENCHO_IMAGE",
        "KUBECTL_IMAGE_PULL_POLICY",
        "KUBECTL_RUN_AS_USER",
        "KUBECTL_RUN_AS_GROUP",
        "KUBECTL_SEQUENCER_IMAGE",
    ]


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("unset KUBECTL_SEQUENCER_IMAGE", DEFAULT_SEQUENCER_IMAGE),
        ("KUBECTL_SEQUENCER_IMAGE=", DEFAULT_SEQUENCER_IMAGE),
        (f"KUBECTL_SEQUENCER_IMAGE={MIRROR_SEQUENCER_IMAGE}", MIRROR_SEQUENCER_IMAGE),
    ],
)
def test_kubernetes_batch_freezes_the_effective_sequencer_image(
    checkout, tmp_path, setting, expected
):
    """The image the sequencer resolves to is frozen, so a newer default is drift."""
    profile = tmp_path / "profile.tsv"
    result = script(
        checkout,
        f"EXECUTION_SUBSTRATE=kubectl; {setting}; kubectl_resolve_sequencer_image; "
        f"_elbencho_batch_write_profile {shlex.quote(str(profile))}",
    )
    assert result.returncode == 0, result.stderr
    rows = dict(line.split("\t", 1) for line in profile.read_text().splitlines())
    assert rows["KUBECTL_SEQUENCER_IMAGE"] == (
        f'declare -gx KUBECTL_SEQUENCER_IMAGE="{expected}"'
    )
    assert not {"KUBECTL_PV", "KUBECTL_PVC", "KUBECTL_STORAGE_TARGETS"} & set(rows)


PREFLIGHT_BODY = r"""
    batch=$1
    EXECUTION_SUBSTRATE=kubectl
    source() {
        builtin source "$@" || return
        if [[ "$1" == */_nv-elbencho-kubectl-functions.sh ]]; then
            %s
        fi
    }
    _elbencho_batch_preflight "$batch"
"""


def preflight(checkout, batch, definition):
    """Run the Kubernetes branch of batch preflight with one stubbed helper."""
    return script(
        checkout,
        PREFLIGHT_BODY.replace("%s", definition)
        .replace("batch=$1", f"batch={shlex.quote(str(batch))}")
        .strip(),
    )


def test_kubernetes_preflight_delegates_to_the_claim_preflight(checkout):
    batch = create(checkout)
    result = preflight(
        checkout,
        batch,
        "kubectl_batch_preflight() { printf 'PREFLIGHT %s\\n' \"$1\"; }",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"PREFLIGHT {batch}\n"


def test_kubernetes_preflight_failure_stops_the_batch(checkout):
    batch = create(checkout)
    result = preflight(checkout, batch, "kubectl_batch_preflight() { return 7; }")
    assert result.returncode != 0


def test_kubernetes_preflight_without_the_claim_preflight_is_a_clear_error(checkout):
    batch = create(checkout)
    result = preflight(checkout, batch, "unset -f kubectl_batch_preflight")
    assert result.returncode != 0
    assert "kubectl_batch_preflight) is unavailable" in result.stderr


@pytest.mark.parametrize("entry", [IO, MD])
def test_help_wins_over_batch_and_a_missing_env_sh_is_explained(checkout, entry):
    """--batch --help prints usage; a missing env.sh names the template."""
    result = launch(checkout, entry, "--batch", "--help")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Usage: ")
    (checkout / "env.sh").unlink()
    for arguments in (("--batch", *ARGS[entry]), ARGS[entry]):
        result = launch(checkout, entry, *arguments)
        assert result.returncode == 1
        assert result.stderr.endswith(
            "\nError: env.sh not found; copy env.sh.template to env.sh and edit it\n"
        )


def test_kubernetes_target_errors_name_the_target_file(checkout):
    """Of several --target files, the one with a bad claim key is named."""
    with (checkout / "env.sh").open("a") as environment:
        environment.write(
            "\nexport EXECUTION_SUBSTRATE=kubectl\n"
            'TEST_DIRS=(["test-pvc:data/one"]=1)\n'
            "export KUBECTL_NAMESPACE=test-ns\n"
            "export KUBECTL_NODE_SELECTOR=benchmark=true\n"
        )
    good = target_file(checkout, "good.env", 'TEST_DIRS=(["test-pvc:data/a"]=1)\n')
    bad = target_file(checkout, "bad.env", 'TEST_DIRS=(["/data/b"]=1)\n')
    result = launch(
        checkout, IO, "--batch", "--target", good, "--target", bad, *ARGS[IO]
    )
    assert result.returncode != 0
    assert f"Error: invalid target file: {bad}" in result.stderr
    assert f"invalid target file: {good}" not in result.stderr


def test_mdtest_resume_of_a_snapshot_without_substrate_explains(checkout, tmp_path):
    results = tmp_path / "mdtest-elbencho-20260101Z000000"
    (results / "executions").mkdir(parents=True)
    (results / "env_used.sh").write_text("TEST_DIRS=()\n")
    result = launch(checkout, MD, "--resume", results)
    assert result.returncode == 1
    assert "saved EXECUTION_SUBSTRATE is missing or unsupported: unset" in result.stderr
    missing = launch(checkout, MD, "--resume", tmp_path / "absent")
    assert f"results directory not found: {tmp_path / 'absent'}" in missing.stderr


def test_delete_only_names_the_supported_substrates(checkout):
    with (checkout / "env.sh").open("a") as environment:
        # Appended after env_base.sh ran, so clear the selectors it derived.
        environment.write(
            "\nexport EXECUTION_SUBSTRATE=kubectl SSH_ENABLED= SLURM_ENABLED=\n"
            'TEST_DIRS=(["test-pvc:data/one"]=1)\n'
            "export KUBECTL_NAMESPACE=test-ns\n"
            "export KUBECTL_NODE_SELECTOR=benchmark=true\n"
        )
    result = launch(checkout, IO, "--delete-only", "test-pvc:data/one/old")
    assert result.returncode == 1
    assert (
        "--delete-only supports EXECUTION_SUBSTRATE=ssh or slurm (current: kubectl)"
        in result.stderr
    )
