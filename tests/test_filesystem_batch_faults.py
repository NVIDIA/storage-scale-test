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

"""Batch publication fault boundaries, concurrent preparation and permanent seals."""

import hashlib
import os
import shlex
import subprocess
import time

import pytest

from lib.filesystem_batch import read_batch_manifest

# Pytest registers the imported fixture under its declared "checkout" name.
# pylint: disable=unused-import
from tests.test_filesystem_batch_shell import (
    IO,
    MD,
    checkout_fixture,
    create,
    launch,
    script,
)

# pylint: enable=unused-import

MANIFEST = "batch-manifest.tsv"
SEAL = "batch-sealed.sha256"


@pytest.fixture(name="fault_checkout")
def fault_checkout_fixture(checkout):
    """Provide a local executable so real preflight can validate deployment identity."""
    for name in ("elbencho", "elbencho.aarch64"):
        executable = checkout / "utils" / name
        executable.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    return checkout


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stage(base, batch, nodes="1"):
    stage = base / "candidate"
    stage.mkdir()
    datestamp = batch.name.removeprefix("filesystem-batch-")
    source = (
        f"ELBENCHO_BATCH_PREPARE_DIR={shlex.quote(str(stage))} "
        f"ELBENCHO_BATCH_DATESTAMP={shlex.quote(datestamp)} "
        f'"$BASH" "$SCALE_TEST_BASE/storage-tests/fs/{IO}" --nodes {nodes}; '
        f"_elbencho_batch_complete_group_snapshot {shlex.quote(str(stage))}"
    )
    result = script(base, source)
    assert result.returncode == 0, result.stdout + result.stderr
    return stage, datestamp


def _membership(batch):
    records = [
        line.split("\t")
        for line in (batch / MANIFEST).read_text(encoding="utf-8").splitlines()
    ]
    return tuple(row[1] for row in records if row[0] == "execution")


def _fake_ssh(base, monkeypatch):
    directory = base / "fake-bin"
    directory.mkdir()
    executable = directory / "ssh"
    executable.write_text(
        '#!/usr/bin/env bash\nprintf "attempt\\n" >> "$BATCH_TEST_SSH_ATTEMPTS"\nexit 17\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    attempts = base / "ssh-attempts"
    monkeypatch.setenv("BATCH_TEST_SSH_ATTEMPTS", str(attempts))
    monkeypatch.setenv("PATH", str(directory) + os.pathsep + os.environ["PATH"])
    return attempts


@pytest.mark.parametrize("initial", [True, False])
@pytest.mark.parametrize("boundary", ["before", "after"])
def test_publication_interruption_has_one_manifest_commit_boundary(
    fault_checkout, initial, boundary
):
    base = fault_checkout
    batch = create(base)
    stage, datestamp = _stage(base, batch)
    if initial:
        (batch / MANIFEST).write_text(
            f"version\t1\nrevision\t0\ndatestamp\t{datestamp}\n", encoding="utf-8"
        )
        (batch / "env_used.sh").unlink()
    before = (batch / MANIFEST).read_bytes()
    if boundary == "before":
        injection = 'mv() { [[ "$1" != "$fault_batch/.batch-manifest.new" ]] || return 91; command mv "$@"; }; '
    else:
        injection = "_elbencho_batch_refresh_union() { return 91; }; "
    result = script(
        base,
        f"fault_batch={shlex.quote(str(batch))}; {injection}"
        f'_elbencho_batch_publish_group "$fault_batch" {shlex.quote(str(stage))} io {datestamp}',
    )
    assert result.returncode != 0
    expected_count = (0 if initial else 1) + (boundary == "after")
    assert _membership(batch) == tuple(
        f"{number:04d}" for number in range(1, expected_count + 1)
    )
    if boundary == "before":
        assert (batch / MANIFEST).read_bytes() == before
        retried = script(
            base,
            f"_elbencho_batch_publish_group {shlex.quote(str(batch))} "
            f"{shlex.quote(str(stage))} io {datestamp}",
        )
        assert retried.returncode == 0, retried.stdout + retried.stderr
        expected_count += 1
        assert _membership(batch) == tuple(
            f"{number:04d}" for number in range(1, expected_count + 1)
        )
    if expected_count:
        repaired = launch(base, MD, "--status", batch)
        assert repaired.returncode == 0, repaired.stdout + repaired.stderr
        assert f"EXECUTIONS_PENDING={expected_count}" in repaired.stdout
        assert (batch / "env_used.sh").is_file()
        assert len(read_batch_manifest(batch).executions) == expected_count
    assert not (batch / SEAL).exists()


def test_execution_limit_rejection_does_not_publish_any_candidate_state(fault_checkout):
    base = fault_checkout
    batch = create(base)
    stage, datestamp = _stage(base, batch)
    manifest = batch / MANIFEST
    with manifest.open("a", encoding="utf-8") as stream:
        for identifier in range(2, 10000):
            stream.write(f"execution\t{identifier:04d}\t0001\t{'0' * 64}\n")
    before = manifest.read_bytes()
    definition = (batch / "executions/0001.sh").read_bytes()
    result = script(
        base,
        f"_elbencho_batch_publish_group {shlex.quote(str(batch))} {shlex.quote(str(stage))} io {datestamp}",
    )
    assert result.returncode != 0 and "9999" in result.stderr
    assert manifest.read_bytes() == before
    assert (batch / "executions/0001.sh").read_bytes() == definition
    assert not (batch / "groups/0002").exists()
    assert not (batch / "executions/10000.sh").exists()


@pytest.mark.parametrize(
    "field,original,equivalent",
    [
        ("ORDER_NODES", "0", "false"),
        ("ORDER_NODES", "1", "yes"),
        ("ORDER_NODES", "TRUE", "1"),
        ("SLURM_EXCLUSIVE_USER", "0", "false"),
        ("SLURM_EXCLUSIVE_USER", "yes", "1"),
        ("SLURM_EXCLUSIVE_USER", "TRUE", "1"),
    ],
)
def test_semantic_ordering_equivalents_allow_changed_group_weights(
    fault_checkout, field, original, equivalent
):
    base = fault_checkout
    with (base / "env.sh").open("a", encoding="utf-8") as stream:
        stream.write(f"\nexport {field}={original}\n")
    batch = create(base)
    common = (batch / "common-env.sh").read_bytes()
    with (base / "env.sh").open("a", encoding="utf-8") as stream:
        stream.write(f'\nexport {field}={equivalent}\nTEST_DIRS=(["/data/one"]=2)\n')
    result = launch(base, MD, "--append", batch, "--nodes", "1", "--tasks", "1")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (batch / "common-env.sh").read_bytes() == common
    manifest = read_batch_manifest(batch)
    assert len(manifest.groups) == 2
    checked = script(
        base,
        f'source {shlex.quote(str(manifest.group_path(manifest.groups[1]) / "env_used.sh"))}; printf "%s\\n" "${{TEST_DIRS[/data/one]}}"',
    )
    assert checked.returncode == 0 and checked.stdout.strip() == "2", checked.stderr


def test_saved_unexported_common_scalars_round_trip(fault_checkout):
    base = fault_checkout
    with (base / "env.sh").open("a", encoding="utf-8") as stream:
        stream.write(
            "\nclient_arch=unexported-arch\nclient_type=cpu\n_SBATCH_OPTIONS_BASE='--account=saved --time=00:07:00'\n"
        )
    batch = create(base)
    result = script(
        base,
        f'unset client_arch client_type _SBATCH_OPTIONS_BASE; source {shlex.quote(str(batch / "common-env.sh"))}; printf "%s\\n" "$client_arch" "$client_type" "$_SBATCH_OPTIONS_BASE"',
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "unexported-arch",
        "cpu",
        "--account=saved --time=00:07:00",
    ]
    assert "invalid option" not in result.stderr


def test_dispatch_failure_preserves_seal_and_only_resume_can_retry(
    fault_checkout, monkeypatch
):
    base = fault_checkout
    attempts = _fake_ssh(base, monkeypatch)
    batch = create(base)
    started = launch(base, MD, "--start", batch)
    assert started.returncode != 0
    assert (batch / SEAL).read_text(encoding="utf-8").strip() == _digest(
        batch / MANIFEST
    )
    before = attempts.read_text(encoding="utf-8")
    assert before
    for entry, arguments in (
        (IO, ("--start", batch)),
        (MD, ("--append", batch, "--nodes", "1", "--tasks", "1")),
    ):
        result = launch(base, entry, *arguments)
        assert result.returncode != 0
        assert attempts.read_text(encoding="utf-8") == before
    resumed = launch(base, IO, "--resume", batch)
    assert resumed.returncode != 0
    assert len(attempts.read_text(encoding="utf-8")) > len(before)
    assert (batch / SEAL).read_text(encoding="utf-8").strip() == _digest(
        batch / MANIFEST
    )


@pytest.mark.parametrize("damage", ["content", "symlink"])
def test_sealed_environment_tamper_blocks_resume_before_remote_retry(
    fault_checkout, monkeypatch, damage
):
    base = fault_checkout
    attempts = _fake_ssh(base, monkeypatch)
    batch = create(base)
    assert launch(base, IO, "--start", batch).returncode != 0
    before = attempts.read_bytes()
    snapshot = batch / "env_used.sh"
    if damage == "symlink":
        external = base / "external-environment.sh"
        snapshot.rename(external)
        snapshot.symlink_to(external)
    else:
        with snapshot.open("a", encoding="utf-8") as stream:
            stream.write("\nexport SSH_USER=replaced-after-seal\n")
    result = launch(base, MD, "--resume", batch)
    assert result.returncode != 0
    assert "sealed execution environment changed" in result.stderr
    assert attempts.read_bytes() == before


@pytest.mark.parametrize(
    "artifact",
    [
        "common-env.sh",
        "missing-common-env",
        "common-fields.tsv",
        "config/SSH_HOST_LIST",
        "group-shell",
        "group-yaml",
        "execution",
    ],
)
def test_tampered_immutable_provenance_rejects_lifecycle_before_dispatch(
    fault_checkout, monkeypatch, artifact
):
    base = fault_checkout
    attempts = _fake_ssh(base, monkeypatch)
    batch = create(base)
    manifest = read_batch_manifest(batch)
    targets = {
        "missing-common-env": batch / "common-env.sh",
        "group-shell": manifest.group_path(manifest.groups[0]) / "env_used.sh",
        "group-yaml": manifest.group_path(manifest.groups[0]) / "env_used.yaml",
        "execution": batch / "executions/0001.sh",
    }
    target = targets.get(artifact, batch / artifact)
    if artifact == "missing-common-env":
        target.unlink()
    else:
        with target.open("a", encoding="utf-8") as stream:
            stream.write("\n# changed immutable evidence\n")
    result = launch(base, MD, "--start", batch)
    assert result.returncode != 0
    assert not (batch / SEAL).exists()
    assert not attempts.exists()


@pytest.mark.parametrize("operation", ["--status", "--start", "--resume", "--append"])
def test_execution_directory_symlink_cannot_redirect_batch_writes(
    fault_checkout, monkeypatch, operation
):
    base = fault_checkout
    attempts = _fake_ssh(base, monkeypatch)
    batch = create(base)
    external = base / "external-executions"
    (batch / "executions").rename(external)
    (batch / "executions").symlink_to(external, target_is_directory=True)
    before = {path.name: path.read_bytes() for path in external.iterdir()}
    args = ("--nodes", "1", "--tasks", "1") if operation == "--append" else ()
    result = launch(base, MD, operation, batch, *args)
    assert result.returncode != 0
    assert "symlinked execution directory" in result.stderr
    assert {path.name: path.read_bytes() for path in external.iterdir()} == before
    assert not attempts.exists()


def test_batch_root_symlink_alias_remains_supported(fault_checkout):
    base = fault_checkout
    batch = create(base)
    alias = base / "batch-alias"
    alias.symlink_to(batch, target_is_directory=True)
    result = launch(base, MD, "--status", alias)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "EXECUTIONS_PENDING=1" in result.stdout


@pytest.mark.parametrize("operation", ["--status", "--start", "--resume"])
def test_dangling_seal_is_corruption_not_an_unsealed_draft(
    fault_checkout, monkeypatch, operation
):
    base = fault_checkout
    attempts = _fake_ssh(base, monkeypatch)
    batch = create(base)
    seal = batch / SEAL
    seal.symlink_to(batch / "missing-seal-target")
    result = launch(base, MD, operation, batch)
    assert result.returncode != 0
    assert "sealed manifest changed" in result.stderr
    assert seal.is_symlink()
    assert not attempts.exists()


@pytest.mark.parametrize("damage", ["missing", "symlink", "invalid"])
@pytest.mark.parametrize("operation", ["--status", "--start", "--resume"])
def test_committed_cell_status_corruption_never_skips_work(
    fault_checkout, monkeypatch, damage, operation
):
    base = fault_checkout
    attempts = _fake_ssh(base, monkeypatch)
    batch = create(base)
    status = batch / "executions/0001.status"
    status.unlink()
    if damage == "symlink":
        replacement = base / "external-status"
        replacement.write_text("SUCCESS\n", encoding="utf-8")
        status.symlink_to(replacement)
    elif damage == "invalid":
        status.write_text("INVALID\n", encoding="utf-8")
    result = launch(base, MD, operation, batch)
    assert result.returncode != 0
    assert "cell status" in result.stderr
    assert not attempts.exists()
    assert not (batch / SEAL).exists()


def test_manifest_revision_must_match_committed_group_count(fault_checkout):
    batch = create(fault_checkout)
    manifest = batch / MANIFEST
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("revision\t1", "revision\t2"),
        encoding="utf-8",
    )
    result = launch(fault_checkout, MD, "--status", batch)
    assert result.returncode != 0
    assert "manifest membership" in result.stderr


def _wait_for_file(path):
    deadline = time.monotonic() + 10
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"Timed out waiting for {path}"


@pytest.mark.parametrize("operation,seal_lock_call", [("status", 1), ("start", 2)])
def test_draft_repair_cannot_rewrite_concurrently_sealed_snapshot(
    fault_checkout, operation, seal_lock_call
):
    """Both union-refresh sites must recheck the seal after acquiring the lock."""
    base = fault_checkout
    batch = create(base)
    layout = (
        "export KUBECTL_CONTROL_LOGICAL_ROOT=data/one\n"
        "export KUBECTL_CONTROL_TEST_ROOT=/mnt/storage-scale-test/data/one\n"
        "export KUBECTL_CONTROL_ROOT=/mnt/storage-scale-test/data/one/.storage-scale-test\n"
    )
    expected = (batch / "env_used.sh").read_bytes() + layout.encode()
    source = f"""
SCRIPT_DIR={shlex.quote(str(base / 'storage-tests/fs'))}
race_root={shlex.quote(str(batch))}
race_lock_calls=0
source() {{
    builtin source "$@" || return
    if [[ "$1" == */lib/_elbencho_functions.sh ]]; then
        eval "$(declare -f _elbencho_acquire_dispatch_lock | sed '1s/_elbencho_acquire_dispatch_lock/_batch_test_real_acquire/')"
        _elbencho_acquire_dispatch_lock() {{
            race_lock_calls=$((race_lock_calls + 1))
            if [[ "$race_lock_calls" == {seal_lock_call} ]]; then
                printf '%s' {shlex.quote(layout)} >> "$race_root/env_used.sh"
                _elbencho_batch_sha256 "$race_root/env_used.sh" > "$race_root/batch-sealed-environment.sha256"
                _elbencho_batch_sha256 "$race_root/{MANIFEST}" > "$race_root/{SEAL}"
            fi
            _batch_test_real_acquire "$@"
        }}
    fi
}}
_elbencho_batch_run {operation} "$race_root"
"""
    result = subprocess.run(
        [
            "bash",
            "-ceu",
            f"source {shlex.quote(str(base / 'lib/_batch_functions.sh'))};\n" + source,
        ],
        cwd=base,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if operation == "status":
        assert result.returncode == 0, result.stdout + result.stderr
        assert "BATCH=SEALED" in result.stdout
    else:
        assert result.returncode != 0
        assert "another invocation already sealed" in result.stderr
    assert (batch / "env_used.sh").read_bytes() == expected
    assert (
        batch / "batch-sealed-environment.sha256"
    ).read_text().strip() == hashlib.sha256(expected).hexdigest()
    assert (batch / SEAL).read_text().strip() == _digest(batch / MANIFEST)


def test_append_during_preflight_revalidates_new_revision_before_seal(
    fault_checkout, monkeypatch
):
    base = fault_checkout
    _fake_ssh(base, monkeypatch)
    batch = create(base)
    ready = base / "preflight-ready"
    release = base / "preflight-release"
    calls = base / "preflight-calls"
    body = f"""
source "$1/env.sh"
source "$1/lib/_elbencho_functions.sh"
SCRIPT_DIR="$1/storage-tests/fs"
eval "$(declare -f _elbencho_batch_preflight | sed '1s/_elbencho_batch_preflight/_batch_test_real_preflight/')"
_elbencho_batch_preflight() {{
    awk -F '\\t' '$1=="revision" {{print $2}}' "$1/batch-manifest.tsv" >> {shlex.quote(str(calls))}
    if [[ ! -f {shlex.quote(str(ready))} ]]; then
        : > {shlex.quote(str(ready))}
        while [[ ! -f {shlex.quote(str(release))} ]]; do sleep 0.02; done
    fi
    _batch_test_real_preflight "$@"
}}
_elbencho_batch_run start {shlex.quote(str(batch))}
"""
    with subprocess.Popen(
        ["bash", "-ceu", body, "preflight-test", str(base)],
        cwd=base,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            _wait_for_file(ready)
            appended = launch(
                base, MD, "--append", batch, "--nodes", "2", "--tasks", "1"
            )
            assert appended.returncode == 0, appended.stdout + appended.stderr
            release.touch()
            stdout, stderr = process.communicate(timeout=15)
            assert process.returncode != 0, stdout + stderr
        finally:
            release.touch()
            if process.poll() is None:
                process.kill()
                process.communicate()
    assert calls.read_text(encoding="utf-8").splitlines() == ["1", "2"]
    assert len(read_batch_manifest(batch).executions) == 2
    assert (batch / SEAL).read_text(encoding="utf-8").strip() == _digest(
        batch / MANIFEST
    )
