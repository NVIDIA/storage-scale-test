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

"""Prepared batch provenance validation and successful-only group reporting."""

import argparse
from dataclasses import FrozenInstanceError
import hashlib
from pathlib import Path
import shlex
import subprocess
import shutil
import sys
from unittest import mock

import pytest

from tests.extract_elbencho_test_support import load_extract_elbencho_module
from lib.filesystem_batch import (
    BATCH_MARKERS,
    ENV_SHELL,
    ENV_YAML,
    MANIFEST_FILENAME,
    SEAL_FILENAME,
    SUCCESS,
    is_batch_directory,
    read_batch_manifest,
    report_batch,
    route_batch_report,
    stage_group_inputs,
)

DATESTAMP = "20260930Z120000"
IO_STEM = f"elbencho-1M-c_001-s_001-d_001_{DATESTAMP}"
MD_STEM = f"mdtest-elbencho-c_002-t_004_{DATESTAMP}_iter1"


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_batch(root, kinds=("io", "mdtest", "io")):
    root.mkdir()
    ledger = root / "executions"
    ledger.mkdir()
    records = ["version\t1", f"revision\t{len(kinds)}", f"datestamp\t{DATESTAMP}"]
    for sequence, kind in enumerate(kinds, 1):
        identifier = f"{sequence:04d}"
        basename = "elbencho" if kind == "io" else "mdtest-elbencho"
        relative = f"groups/{identifier}/{basename}-{DATESTAMP}"
        group = root / relative
        group.mkdir(parents=True)
        (group / ENV_SHELL).write_text(
            f"export TEST_ROOT=/saved/{identifier}\n", encoding="utf-8"
        )
        (group / ENV_YAML).write_text(
            f"TEST_ROOT: /saved/{identifier}\n", encoding="utf-8"
        )
        (group / "executions").mkdir()
        definition = ledger / f"{identifier}.sh"
        coordinates = (
            "export nodes=1\nexport io_size=1M\nexport thread_count=1\nexport io_depth=1\n"
            if kind == "io"
            else "export nodes=2\nexport tasks_per_node=4\nexport MDTEST_ITERATIONS=1\n"
        )
        definition.write_text(
            f"export ELBENCHO_EXECUTION_KIND={kind}\n"
            f"export ELBENCHO_BATCH_GROUP_ID={identifier}\n"
            f"export ELBENCHO_BATCH_OUTPUT_RELATIVE={relative}\n{coordinates}",
            encoding="utf-8",
        )
        (ledger / f"{identifier}.status").write_text("PENDING\n", encoding="utf-8")
        records.append(
            f"group\t{identifier}\t{kind}\t{relative}\t{_digest(group / ENV_SHELL)}\t{_digest(group / ENV_YAML)}"
        )
        records.append(f"execution\t{identifier}\t{identifier}\t{_digest(definition)}")
    (root / MANIFEST_FILENAME).write_text("\n".join(records) + "\n", encoding="utf-8")
    return read_batch_manifest(root)


def _publish_result(manifest, group, status=SUCCESS):
    path = manifest.group_path(group)
    stem = IO_STEM if group.kind == "io" else MD_STEM
    for extension in ("csv", "out", "live.csv"):
        (path / f"{stem}.{extension}").write_text(group.group_id, encoding="utf-8")
    for execution in manifest.group_executions(group.group_id):
        (manifest.root / "executions" / f"{execution.execution_id}.status").write_text(
            status, encoding="utf-8"
        )
        (path / "executions" / f"{execution.execution_id}.log").write_text(
            "Saved treescan evidence", encoding="utf-8"
        )
    return path


def _replace_manifest(root, before, after):
    path = root / MANIFEST_FILENAME
    path.write_text(
        path.read_text(encoding="utf-8").replace(before, after), encoding="utf-8"
    )


def test_manifest_membership_is_immutable_and_aliases_resolve(tmp_path):
    manifest = _create_batch(tmp_path / "batch 'quoted'")
    alias = tmp_path / "alias"
    alias.symlink_to(manifest.root, target_is_directory=True)
    assert is_batch_directory(alias)
    assert read_batch_manifest(alias) == manifest
    assert tuple(group.kind for group in manifest.groups) == ("io", "mdtest", "io")
    assert tuple(item.execution_id for item in manifest.executions) == (
        "0001",
        "0002",
        "0003",
    )
    with pytest.raises(FrozenInstanceError):
        manifest.revision = 4


@pytest.mark.parametrize(
    "before,after",
    [
        ("version\t1", "version\t2"),
        ("revision\t3", "revision\t2"),
        (f"datestamp\t{DATESTAMP}", "datestamp\tinvalid"),
        ("group\t0001\tio", "group\t0004\tio"),
        ("group\t0001\tio", "group\t0001\tunknown"),
        ("execution\t0002\t0002", "execution\t0004\t0002"),
        ("execution\t0002\t0002", "execution\t0002\t9999"),
        ("groups/0001/elbencho-", "../outside/elbencho-"),
    ],
)
def test_invalid_manifest_records_are_rejected(tmp_path, before, after):
    manifest = _create_batch(tmp_path / "batch")
    _replace_manifest(manifest.root, before, after)
    with pytest.raises(ValueError):
        read_batch_manifest(manifest.root)


@pytest.mark.parametrize("artifact", [ENV_SHELL, ENV_YAML, "definition"])
def test_modified_immutable_evidence_is_rejected(tmp_path, artifact):
    manifest = _create_batch(tmp_path / "batch")
    path = (
        manifest.root / "executions/0001.sh"
        if artifact == "definition"
        else manifest.group_path(manifest.groups[0]) / artifact
    )
    path.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="digest mismatch"):
        read_batch_manifest(manifest.root)


def test_definition_membership_is_validated_even_with_updated_digest(tmp_path):
    manifest = _create_batch(tmp_path / "batch")
    definition = manifest.root / "executions/0001.sh"
    before = _digest(definition)
    definition.write_text(
        definition.read_text(encoding="utf-8").replace(
            "ELBENCHO_BATCH_GROUP_ID=0001", "ELBENCHO_BATCH_GROUP_ID=0002"
        ),
        encoding="utf-8",
    )
    _replace_manifest(manifest.root, before, _digest(definition))
    with pytest.raises(ValueError, match="incorrect ELBENCHO_BATCH_GROUP_ID"):
        read_batch_manifest(manifest.root)


def test_seal_and_uncommitted_definitions(tmp_path):
    manifest = _create_batch(tmp_path / "batch")
    (manifest.root / "executions/9999.sh").write_text("uncommitted", encoding="utf-8")
    (manifest.root / SEAL_FILENAME).write_text(manifest.digest + "\n", encoding="utf-8")
    assert len(read_batch_manifest(manifest.root).executions) == 3
    _replace_manifest(manifest.root, "version\t1", "version\t1\nversion\t1")
    with pytest.raises(ValueError, match="Duplicate batch header"):
        read_batch_manifest(manifest.root)


def test_invalid_seal_is_rejected(tmp_path):
    manifest = _create_batch(tmp_path / "batch")
    (manifest.root / SEAL_FILENAME).write_text("0" * 64, encoding="utf-8")
    with pytest.raises(ValueError, match="Sealed batch manifest digest mismatch"):
        read_batch_manifest(manifest.root)


def test_dangling_seal_is_rejected_with_an_intact_manifest(tmp_path):
    manifest = _create_batch(tmp_path / "batch")
    (manifest.root / SEAL_FILENAME).symlink_to(manifest.root / "missing seal target")
    assert is_batch_directory(manifest.root)
    with pytest.raises(ValueError, match="Expected nonsymlink batch file"):
        read_batch_manifest(manifest.root)
    with mock.patch("lib.filesystem_batch.subprocess.run") as engine:
        with pytest.raises(ValueError, match="Expected nonsymlink batch file"):
            report_batch(manifest.root)
        engine.assert_not_called()
    assert not (manifest.root / "reports").exists()


def test_cross_group_snapshot_symlink_is_rejected(tmp_path):
    manifest = _create_batch(tmp_path / "batch")
    source = manifest.group_path(manifest.groups[0]) / ENV_YAML
    source.unlink()
    source.symlink_to(manifest.group_path(manifest.groups[2]) / ENV_YAML)
    with pytest.raises(ValueError, match="Symlink"):
        read_batch_manifest(manifest.root)


def test_group_staging_preserves_provenance_and_scopes_live_data(tmp_path):
    manifest = _create_batch(tmp_path / "batch")
    group = manifest.groups[0]
    source = _publish_result(manifest, group)
    (source / f"elbencho-4K-c_001-s_004-d_001_{DATESTAMP}.live.csv").write_text(
        "running", encoding="utf-8"
    )
    destination = tmp_path / "private inputs"
    assert (
        stage_group_inputs(
            manifest, group, destination, manifest.group_executions(group.group_id)
        )
        == 1
    )
    assert (destination / ENV_YAML).read_bytes() == (source / ENV_YAML).read_bytes()
    assert list(destination.glob("*.live.csv")) == [destination / f"{IO_STEM}.live.csv"]
    assert (destination / "executions/0001.status").read_text(
        encoding="utf-8"
    ) == SUCCESS
    assert not (destination / "executions/0002.sh").exists()


def test_cross_group_result_symlink_is_rejected(tmp_path):
    manifest = _create_batch(tmp_path / "batch")
    first = _publish_result(manifest, manifest.groups[0])
    third = _publish_result(manifest, manifest.groups[2])
    target = first / f"{IO_STEM}.out"
    target.unlink()
    target.symlink_to(third / target.name)
    with pytest.raises(ValueError, match="nonsymlink"):
        stage_group_inputs(
            manifest,
            manifest.groups[0],
            tmp_path / "private",
            manifest.group_executions("0001"),
        )


def test_reports_keep_repeated_coordinates_separate_and_count_failed_cells(tmp_path):
    manifest = _create_batch(tmp_path / "batch 'space'")
    for group in manifest.groups:
        _publish_result(
            manifest, group, "FAILED" if group.kind == "mdtest" else SUCCESS
        )
    # A stale group-local status cannot override the authoritative root ledger.
    (manifest.group_path(manifest.groups[1]) / "executions/0002.status").write_text(
        SUCCESS, encoding="utf-8"
    )
    output = tmp_path / "custom reports"
    called = []

    def engine(command, check):
        assert check
        inputs = Path(command[2])
        identifier = (inputs / f"{IO_STEM}.csv").read_text(encoding="utf-8")
        called.append(identifier)
        output_dir = Path(command[4])
        assert output_dir.name == identifier
        (output_dir / "report.txt").write_text(identifier, encoding="utf-8")

    with mock.patch("lib.filesystem_batch.subprocess.run", side_effect=engine):
        assert report_batch(manifest.root, output_dir=output) == 0
    assert called == ["0001", "0003"]
    index = (output / "index.md").read_text(encoding="utf-8")
    assert "FAILED: 1" in index
    assert "No successful results" in index
    assert "groups/0001/report.txt" in index
    assert "groups/0003/report.txt" in index
    assert "%20" in index and "%27" in index
    assert not (output / "groups/0001/executions").exists()


def test_report_failure_retains_successful_other_group(tmp_path):
    manifest = _create_batch(tmp_path / "batch", kinds=("io", "io"))
    for group in manifest.groups:
        _publish_result(manifest, group)

    def engine(command, check):
        assert check
        output = Path(command[4])
        if output.name == "0001":
            raise subprocess.CalledProcessError(1, command)
        (output / "report.txt").write_text("retained", encoding="utf-8")

    with mock.patch("lib.filesystem_batch.subprocess.run", side_effect=engine):
        assert report_batch(manifest.root) == 1
    assert (manifest.root / "reports/groups/0002/report.txt").exists()
    assert "Reporting failed" in (manifest.root / "reports/index.md").read_text(
        encoding="utf-8"
    )


def test_selection_and_no_successful_results(tmp_path):
    manifest = _create_batch(tmp_path / "batch")
    with mock.patch("lib.filesystem_batch.subprocess.run") as engine:
        assert report_batch(manifest.root, kind="mdtest", groups="0002") == 1
        engine.assert_not_called()
    index = (manifest.root / "reports/index.md").read_text(encoding="utf-8")
    assert "| 0002 | mdtest |" in index
    assert "| 0001 |" not in index
    assert "PENDING: 1" in index
    with pytest.raises(ValueError, match="Unknown batch groups"):
        report_batch(manifest.root, groups="9999")


def test_legacy_reporter_batch_routing_forwards_options(tmp_path):
    manifest = _create_batch(tmp_path / "batch")
    args = argparse.Namespace(
        input_dirs=[str(manifest.root)],
        groups="0001,0003",
        output_dir="saved",
        from_csv=None,
    )
    argv = [
        "--only-nodes",
        "1",
        str(manifest.root),
        "--groups=0001,0003",
        "--output-dir",
        "saved",
        "--to-csv",
    ]
    with mock.patch("lib.filesystem_batch.report_batch", return_value=0) as report:
        with pytest.raises(SystemExit) as exited:
            route_batch_report(args, "io", argv)
    assert exited.value.code == 0
    report.assert_called_once_with(
        str(manifest.root),
        "io",
        "0001,0003",
        "saved",
        ["--only-nodes", "1", "--to-csv"],
    )
    args.input_dirs.append(str(tmp_path))
    with pytest.raises(ValueError, match="sole raw"):
        route_batch_report(args, "io", argv)


def test_ordinary_reporter_routing_is_unchanged(tmp_path):
    args = argparse.Namespace(input_dirs=[str(tmp_path)], groups=None, from_csv=None)
    assert route_batch_report(args, "io", []) is False
    args.groups = "0001"
    with pytest.raises(ValueError, match="requires a prepared batch"):
        route_batch_report(args, "io", [])


@pytest.mark.parametrize(
    "wrapper",
    ["extract-filesystem.sh", "extract-elbencho.sh", "extract-mdtest-elbencho.sh"],
)
def test_batch_wrappers_do_not_load_current_environment(tmp_path, wrapper):
    repository = tmp_path / "repo 'spaces'"
    utilities = repository / "utils"
    utilities.mkdir(parents=True)
    libraries = repository / "lib"
    libraries.mkdir()
    source = Path(__file__).resolve().parents[1] / "utils" / wrapper
    shutil.copyfile(source, utilities / wrapper)
    (repository / "env.sh").write_text("exit 73\n", encoding="utf-8")
    interpreter = repository / "python-stub"
    interpreter.write_text(
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\"\n", encoding="utf-8"
    )
    interpreter.chmod(0o755)
    (libraries / "env_functions.sh").write_text(
        'setup_python_venv() { printf "%s\\n" "$SCALE_TEST_BASE/python-stub"; }\n',
        encoding="utf-8",
    )
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / MANIFEST_FILENAME).write_text("version\t1\n", encoding="utf-8")
    completed = subprocess.run(
        ["bash", str(utilities / wrapper), str(batch)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert wrapper.replace(".sh", ".py") in completed.stdout
    assert str(batch) in completed.stdout


def test_markdown_batch_reports_have_persistent_report_file(tmp_path):
    manifest = _create_batch(tmp_path / "batch", kinds=("io",))
    _publish_result(manifest, manifest.groups[0])
    result = subprocess.CompletedProcess([], 0, stdout="# Saved Markdown\n")
    with mock.patch(
        "lib.filesystem_batch.subprocess.run", return_value=result
    ) as engine:
        assert report_batch(manifest.root, engine_options=["--markdown"]) == 0
    assert engine.call_args.kwargs["stdout"] == subprocess.PIPE
    assert (manifest.root / "reports/groups/0001/report.txt").read_text(
        encoding="utf-8"
    ) == result.stdout


@pytest.fixture(name="reporting_checkout")
def reporting_checkout_fixture(tmp_path):
    """Actual reporter wrappers and engines with pinned Python and invalid env.sh."""
    repository = tmp_path / "reporter checkout"
    source = Path(__file__).resolve().parents[1]
    shutil.copytree(source / "lib", repository / "lib")
    (repository / "utils").mkdir()
    for entry in source.glob("utils/extract-*.sh"):
        shutil.copyfile(entry, repository / "utils" / entry.name)
        shutil.copyfile(
            entry.with_suffix(".py"),
            repository / "utils" / entry.with_suffix(".py").name,
        )
    (repository / "env.sh").write_text("exit 73\n", encoding="utf-8")
    (repository / "lib/env_functions.sh").write_text(
        f"setup_python_venv() {{ printf '%s\\n' {shlex.quote(sys.executable)}; }}\n",
        encoding="utf-8",
    )
    return repository


@pytest.mark.parametrize(
    "wrapper",
    ["extract-filesystem.sh", "extract-elbencho.sh", "extract-mdtest-elbencho.sh"],
)
@pytest.mark.parametrize("input_kind", ["group", "alias", "groups-parent"])
def test_raw_group_inputs_cannot_bypass_root_success_filter(
    tmp_path, reporting_checkout, wrapper, input_kind
):
    manifest = _create_batch(tmp_path / "batch", kinds=("io", "mdtest"))
    group = manifest.groups[1 if wrapper == "extract-mdtest-elbencho.sh" else 0]
    source = _publish_result(manifest, group, status="FAILED")
    input_path = source
    if input_kind == "alias":
        input_path = tmp_path / "group alias"
        input_path.symlink_to(source, target_is_directory=True)
    elif input_kind == "groups-parent":
        input_path = manifest.root / "groups"
    completed = subprocess.run(
        ["bash", str(reporting_checkout / "utils" / wrapper), str(input_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert "Batch descendants cannot be raw reporting inputs" in completed.stderr
    assert str(manifest.root) in completed.stderr
    if input_kind != "groups-parent":
        assert f"--groups {group.group_id}" in completed.stderr
    assert not (manifest.root / "reports").exists()
    assert not (source / "report.txt").exists()


def test_selected_root_execution_log_preserves_treescan_stats_without_cross_group_pairing(
    tmp_path,
):
    manifest = _create_batch(tmp_path / "batch", kinds=("io", "io"))
    for group in manifest.groups:
        _publish_result(manifest, group)
    first_log = manifest.root / "executions/0001.log"
    first_log.write_text(
        f"Result: {IO_STEM}.out\nTreescan file sizes (bytes): count=10 avg=8192 min=4096 max=16384\n",
        encoding="utf-8",
    )
    (manifest.root / "executions/0002.log").write_text(
        f"Result: {IO_STEM}.out\nTreescan file sizes (bytes): count=20 avg=9999 min=9999 max=9999\n",
        encoding="utf-8",
    )
    destination = tmp_path / "private snapshot"
    stage_group_inputs(
        manifest, manifest.groups[0], destination, manifest.group_executions("0001")
    )
    assert (destination / "executions/0001.log").read_bytes() == first_log.read_bytes()
    assert not (destination / "executions/0002.log").exists()
    module = load_extract_elbencho_module("batch_root_log_reporting")
    metric = mock.Mock(
        io_size="1M",
        datestamp=DATESTAMP,
        treescan_avg_bytes=0,
        treescan_first_file_bytes=0,
    )
    module.apply_treescan_from_directory_scan(
        str(destination), [metric], str(destination / IO_STEM)
    )
    assert metric.treescan_file_count == 10
    assert metric.treescan_avg_bytes == 8192


@pytest.mark.parametrize("marker", BATCH_MARKERS)
@pytest.mark.parametrize("marker_kind", ["file", "directory", "dangling-symlink"])
def test_exact_batch_markers_fail_closed_with_missing_manifest(
    tmp_path, marker, marker_kind
):
    root = tmp_path / "results"
    root.mkdir()
    path = root / marker
    if marker_kind == "file":
        path.write_text("batch evidence", encoding="utf-8")
    elif marker_kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(root / "missing target")
    assert is_batch_directory(root)
    with pytest.raises(ValueError):
        read_batch_manifest(root)


@pytest.mark.parametrize(
    "wrapper",
    ["extract-filesystem.sh", "extract-elbencho.sh", "extract-mdtest-elbencho.sh"],
)
@pytest.mark.parametrize("input_kind", ["root", "group", "alias"])
def test_missing_batch_manifest_cannot_enter_legacy_reporter_path(
    tmp_path, reporting_checkout, wrapper, input_kind
):
    manifest = _create_batch(tmp_path / "batch", kinds=("io", "mdtest"))
    group = manifest.groups[1 if wrapper == "extract-mdtest-elbencho.sh" else 0]
    source = _publish_result(manifest, group, status="FAILED")
    (manifest.root / MANIFEST_FILENAME).unlink()
    (manifest.root / "batch-profile.tsv").write_text(
        "saved batch profile", encoding="utf-8"
    )
    input_path = manifest.root if input_kind == "root" else source
    if input_kind == "alias":
        input_path = tmp_path / "batch alias"
        input_path.symlink_to(manifest.root, target_is_directory=True)
    completed = subprocess.run(
        ["bash", str(reporting_checkout / "utils" / wrapper), str(input_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert (
        "batch-manifest.tsv" in completed.stderr
        or "Batch descendants" in completed.stderr
    )
    assert not (manifest.root / "reports").exists()
    assert not (source / "report.txt").exists()
