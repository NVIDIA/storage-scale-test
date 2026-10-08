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
import contextlib
from dataclasses import FrozenInstanceError
import errno
import hashlib
import importlib.util
import io
import os
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
    REPORT_IDENTITY_FILENAME,
    SEAL_FILENAME,
    SUCCESS,
    is_batch_directory,
    read_batch_manifest,
    report_batch,
    route_batch_report,
    stage_group_inputs,
)
from lib.filesystem_report_options import (
    REPORT_OPTIONS,
    add_analysis_arguments,
    options_by_kind,
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
    assert "| 0001 | io |" in index
    assert "PENDING: 1" in index
    with pytest.raises(ValueError, match="Unknown batch groups"):
        report_batch(manifest.root, groups="9999")


@pytest.fixture(name="unified_reporter")
def unified_reporter_fixture():
    """Load the public front door without spawning an analyzer."""
    path = Path(__file__).resolve().parents[1] / "utils/extract-filesystem.py"
    spec = importlib.util.spec_from_file_location("filesystem_report_front_door", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unified_reporter_routes_common_and_kind_specific_options(
    tmp_path, unified_reporter
):
    manifest = _create_batch(tmp_path / "batch")
    for group in manifest.groups:
        _publish_result(manifest, group)
    called = {}

    def report(_manifest, group, _successful, output, options):
        called[group.group_id] = options
        (output / "report.txt").write_text(group.kind, encoding="utf-8")

    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert (
            unified_reporter.main(
                [
                    "--normalize-to",
                    "1",
                    "--only-nodes=1,2",
                    str(manifest.root),
                    "--only-sizes",
                    "1M,r4K;4K",
                    "--only-sizes=1M",
                    "--markdown",
                    "--per-client-plots",
                    "--client-max-heatmap-rows",
                    "12",
                    "--to-csv",
                ]
            )
            == 0
        )
    common = {"--only-nodes=1,2", "--markdown", "--to-csv"}
    assert set(called["0002"]) == common | {"--normalize-to=1"}
    assert set(called["0001"]) == common | {
        "--only-sizes=1M,r4K;4K",
        "--only-sizes=1M",
        "--per-client-plots",
        "--client-max-heatmap-rows=12",
    }
    assert called["0001"] == called["0003"]


@pytest.mark.parametrize("option", REPORT_OPTIONS, ids=lambda option: option.flag)
def test_all_analysis_options_share_registration_and_routing(option):
    parser = argparse.ArgumentParser()
    add_analysis_arguments(parser, "all", explicit_only=True)
    value = "3" if "type" in option.settings else "a path,with spaces"
    arguments = [option.flag]
    if option.settings.get("action") != "store_true":
        arguments.append(value)
    args = parser.parse_args(arguments)
    routed = options_by_kind(args, {"io", "mdtest"})
    expected = arguments[:1] if len(arguments) == 1 else [f"{option.flag}={value}"]
    if option.settings.get("type") is float:
        expected = [f"{option.flag}=3.0"]
    for kind in ("io", "mdtest"):
        assert routed[kind] == (expected if option.scope in ("common", kind) else [])
        native = argparse.ArgumentParser()
        add_analysis_arguments(native, kind)
        native.parse_args(routed[kind])
        if option.scope in ("common", kind):
            assert option.help in " ".join(native.format_help().split())


@pytest.mark.parametrize(
    "arguments, message",
    [
        (["--kind", "io", "--normalize-to", "1"], "no selected mdtest group"),
        (["--groups", "0002", "--per-client-plots"], "no selected io group"),
        (["--kind", "mdtest", "--only-sizes", "1M"], "no selected io group"),
        (["--kind", "io", "--groups", "0002"], "No batch groups match"),
        (["--normalize-to", "0"], "positive integer"),
        (["--client-max-heatmap-rows", "0"], "greater than zero"),
        (["--client-outlier-threshold", "nan"], "greater than zero"),
        (["--client-outlier-threshold", "inf"], "greater than zero"),
        (["--from-csv", "saved.csv"], "cannot report a prepared batch"),
        (["--test-parse", "result.csv"], "cannot report a prepared batch"),
    ],
)
def test_unified_option_errors_precede_report_mutation(
    tmp_path, unified_reporter, capsys, arguments, message
):
    manifest = _create_batch(tmp_path / "batch")
    with mock.patch("lib.filesystem_batch._report_group") as engine:
        with pytest.raises(SystemExit) as exited:
            unified_reporter.main([str(manifest.root), *arguments])
        engine.assert_not_called()
    assert exited.value.code == 2
    assert message in capsys.readouterr().err
    assert not (manifest.root / "reports").exists()


def test_filtered_reports_preserve_other_kinds_groups_and_index(
    tmp_path, unified_reporter
):
    manifest = _create_batch(tmp_path / "batch")
    for group in manifest.groups:
        _publish_result(manifest, group)
    calls = []

    def report(_manifest, group, _successful, output, options):
        calls.append(group.group_id)
        (output / "report.txt").write_text(str(options), encoding="utf-8")

    output = tmp_path / "custom reports"
    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert (
            unified_reporter.main(
                [
                    str(manifest.root),
                    "--kind",
                    "mdtest",
                    "--normalize-to",
                    "1",
                    "--output-dir",
                    str(output),
                ]
            )
            == 0
        )
        metadata_report = (output / "groups/0002/report.txt").read_bytes()
        assert (
            unified_reporter.main(
                [
                    str(manifest.root),
                    "--groups",
                    "0001,0003",
                    "--only-nodes",
                    "1",
                    "--output-dir",
                    str(output),
                ]
            )
            == 0
        )
        assert (output / "groups/0002/report.txt").read_bytes() == metadata_report
        assert (
            unified_reporter.main(
                [
                    str(manifest.root),
                    "--groups",
                    "0001",
                    "--only-threads",
                    "1",
                    "--output-dir",
                    str(output),
                ]
            )
            == 0
        )
    assert calls == ["0002", "0001", "0003", "0001"]
    index = (output / "index.md").read_text(encoding="utf-8")
    for identifier in ("0001", "0002", "0003"):
        assert f"groups/{identifier}/report.txt" in index
        assert f"| {identifier} |" in index
    assert not list(output.glob(".index-*"))


def test_failed_filtered_report_retains_other_report_links(tmp_path):
    manifest = _create_batch(tmp_path / "batch")
    for group in manifest.groups:
        _publish_result(manifest, group)

    def report(_manifest, group, _successful, output, _options):
        (output / "report.txt").write_text(group.kind, encoding="utf-8")

    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert report_batch(manifest.root) == 0
    with mock.patch(
        "lib.filesystem_batch._report_group", side_effect=ValueError("bad report")
    ):
        assert report_batch(manifest.root, groups="0002") == 1
    index = (manifest.root / "reports/index.md").read_text(encoding="utf-8")
    assert "Reporting failed" in index
    assert "groups/0001/report.txt" in index
    assert "groups/0003/report.txt" in index


def test_shared_report_destination_does_not_retain_another_batch(tmp_path):
    """Identical group IDs and settings do not establish ownership across batches."""
    first = _create_batch(tmp_path / "first")
    second = _create_batch(tmp_path / "second")
    output = tmp_path / "shared reports"
    for manifest in (first, second):
        for group in manifest.groups:
            _publish_result(manifest, group)

    def report(manifest, _group, _successful, destination, _options):
        (destination / "report.txt").write_text(str(manifest.root), encoding="utf-8")

    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert report_batch(first.root, output_dir=output) == 0
        assert report_batch(second.root, groups="0001", output_dir=output) == 0
        index = (output / "index.md").read_text(encoding="utf-8")
        assert "groups/0001/report.txt" in index
        assert "groups/0002/report.txt" not in index
        assert "groups/0003/report.txt" not in index
        assert not (output / "reports.txt").exists()
        assert report_batch(second.root, groups="0002", output_dir=output) == 0
    index = (output / "index.md").read_text(encoding="utf-8")
    assert "groups/0001/report.txt" in index
    assert "groups/0002/report.txt" in index
    assert "groups/0003/report.txt" not in index
    combined = (output / "reports.txt").read_text(encoding="utf-8")
    assert str(first.root) not in combined
    assert str(second.root) in combined


@pytest.mark.parametrize("fault", ["missing", "corrupt", "overwritten", "failed"])
def test_filtered_report_rejects_unproven_retained_content(tmp_path, fault):
    """Unrelated files and partial engine output cannot reuse stale provenance."""
    manifest = _create_batch(tmp_path / "batch")
    for group in manifest.groups:
        _publish_result(manifest, group)

    def report(_manifest, group, _successful, output, _options):
        (output / "report.txt").write_text(group.kind, encoding="utf-8")

    output = manifest.root / "reports"
    marker = output / "groups/0002" / REPORT_IDENTITY_FILENAME
    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert report_batch(manifest.root) == 0
        if fault == "missing":
            marker.unlink()
        elif fault == "corrupt":
            marker.write_bytes(b"\xff")
        elif fault == "overwritten":
            (marker.parent / "report.txt").write_text("foreign", encoding="utf-8")
        else:
            with mock.patch(
                "lib.filesystem_batch._report_group", side_effect=ValueError("partial")
            ):
                assert report_batch(manifest.root, groups="0002") == 1
            assert not marker.exists()
        assert report_batch(manifest.root, groups="0001") == 0
    index = (output / "index.md").read_text(encoding="utf-8")
    assert "groups/0001/report.txt" in index
    assert "groups/0002/report.txt" not in index
    assert "groups/0003/report.txt" in index


def test_retained_report_accepts_canonical_batch_alias(tmp_path):
    """A symlinked input resolving to the same batch retains valid provenance."""
    manifest = _create_batch(tmp_path / "batch")
    for group in manifest.groups:
        _publish_result(manifest, group)
    alias = tmp_path / "alias"
    alias.symlink_to(manifest.root, target_is_directory=True)

    def report(_manifest, group, _successful, output, _options):
        (output / "report.txt").write_text(group.kind, encoding="utf-8")

    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert report_batch(manifest.root) == 0
        assert report_batch(alias, groups="0001") == 0
    index = (manifest.root / "reports/index.md").read_text(encoding="utf-8")
    assert "groups/0002/report.txt" in index


def test_successful_engine_without_report_cannot_certify_prior_content(tmp_path):
    """An engine's zero exit alone does not prove it replaced the previous report."""
    manifest = _create_batch(tmp_path / "batch", kinds=("io",))
    _publish_result(manifest, manifest.groups[0])

    def report(_manifest, _group, _successful, output, _options):
        (output / "report.txt").write_text("original", encoding="utf-8")

    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert report_batch(manifest.root) == 0
    with mock.patch("lib.filesystem_batch._report_group"):
        assert report_batch(manifest.root) == 1
    output = manifest.root / "reports"
    assert "Reporting failed" in (output / "index.md").read_text(encoding="utf-8")
    assert not (output / "groups/0001" / REPORT_IDENTITY_FILENAME).exists()


def test_interrupted_index_publication_preserves_previous_index(tmp_path):
    manifest = _create_batch(tmp_path / "batch", kinds=("io",))
    output = manifest.root / "reports"
    output.mkdir()
    index = output / "index.md"
    index.write_text("previous complete index", encoding="utf-8")
    with mock.patch(
        "lib.filesystem_batch.os.replace", side_effect=OSError("full disk")
    ):
        with pytest.raises(OSError, match="full disk"):
            report_batch(manifest.root)
    assert index.read_text(encoding="utf-8") == "previous complete index"
    assert not list(output.glob(".index-*"))


@pytest.mark.parametrize("markdown", [False, True])
@pytest.mark.parametrize("custom_output", [False, True])
def test_omnibus_report_preserves_tables_with_each_saved_environment(
    tmp_path, markdown, custom_output
):
    """IO summaries and raw metadata settings precede each intact group report."""
    manifest = _create_batch(tmp_path / "batch", kinds=("io", "mdtest"))
    for group in manifest.groups:
        _publish_result(manifest, group)
    output = tmp_path / "custom reports" if custom_output else None
    report_root = output or manifest.root / "reports"
    omnibus = (output or manifest.root) / "reports.txt"
    contents = {}
    for group in manifest.groups:
        identifier = group.group_id
        if markdown:
            first_header = f"| Nodes | {identifier} first metric |"
            second_header = f"| Nodes | {identifier} second metric |"
            contents[identifier] = (
                f"# Group {identifier} results\n\n"
                "```text\n# Example command\n"
                "| Example | Value |\n| --- | --- |\n| fake | row |\n```\n\n"
                f"## First table\n\n{first_header}\n| --- | --- |\n| 1 | 10 |\n\n"
                f"## Second table\n\n{second_header}\n| --- | --- |\n| 1 | 20 |\n"
            )
        elif group.kind == "io":
            first_header = f"Nodes | Thrds | {identifier} first metric"
            second_header = f"Nodes | Thrds | {identifier} second metric"
            contents[identifier] = (
                "=== IO Size: 1M - WRITE ===\n"
                f"{first_header}\n----------------------------------------\n1 | 1 | 10\n\n"
                "=== IO Size: 1M - READ ===\n"
                f"{second_header}\n----------------------------------------\n1 | 1 | 20\n"
            )
        else:
            first_header = f"Nodes  Threads  Iters  {identifier} first metric"
            second_header = f"Nodes  Threads  Iters  {identifier} second metric"
            contents[identifier] = (
                "=== MDTest Configuration (env_used.yaml) ===\n"
                "MDTEST_ITERATIONS  1\n\n"
                "=== Performance Rates (ops/sec) ===\n\n"
                f"{first_header}\n----------------------------------------\n1  1  1  10\n\n"
                "=== Average Phase Elapsed Times ===\n\n"
                "Mean measured-operation wall time.\n\n"
                f"{second_header}\n----------------------------------------\n1  1  1  20\n\n"
                "=== Representative Commands ===\n# Create: elbencho --mkdir\n"
            )

    def report(_manifest, group, _successful, destination, _options):
        (destination / "report.txt").write_text(
            contents[group.group_id], encoding="utf-8"
        )

    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert report_batch(manifest.root, output_dir=output) == 0
    combined = omnibus.read_text(encoding="utf-8")
    previous_end = 0
    for group in manifest.groups:
        saved_environment = (manifest.group_path(group) / ENV_YAML).read_text(
            encoding="utf-8"
        )
        if group.kind == "mdtest":
            assert combined.count(saved_environment) == 1
            group_start = combined.index(contents[group.group_id])
            assert saved_environment in combined[previous_end:group_start]
            previous_end = group_start + len(contents[group.group_id])
        else:
            assert saved_environment not in combined
            group_start = combined.index(contents[group.group_id])
            prefix = combined[previous_end:group_start]
            assert prefix.count("Runner:") == 1
            assert prefix.count("Override:") == 1
            previous_end = group_start + len(contents[group.group_id])
        assert (report_root / "groups" / group.group_id / "report.txt").read_text(
            encoding="utf-8"
        ) == contents[group.group_id]
    assert "\n\n" in combined
    if markdown:
        assert combined.count("| Example | Value |\n| --- | --- |\n| fake | row |") == 2
    else:
        assert "MDTEST_ITERATIONS  1" in combined
        assert "# Create: elbencho --mkdir" in combined
    index = (report_root / "index.md").read_text(encoding="utf-8")
    assert "reports.txt" in index
    if not custom_output:
        assert not (report_root / "reports.txt").exists()


def test_filtered_omnibus_retains_owned_reports_and_removes_stale_output(tmp_path):
    """Filtered failures remove obsolete tables while retaining valid other groups."""
    manifest = _create_batch(tmp_path / "batch")
    for group in manifest.groups:
        _publish_result(manifest, group)
    omnibus = manifest.root / "reports.txt"

    def report(_manifest, group, _successful, destination, options):
        (destination / "report.txt").write_text(
            f"Group {group.group_id}: {options}\n", encoding="utf-8"
        )

    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert report_batch(manifest.root) == 0
        assert report_batch(manifest.root, groups="0001", engine_options=["new"]) == 0
    combined = omnibus.read_text(encoding="utf-8")
    assert "Group 0001: ['new']" in combined
    assert "Group 0001: []" not in combined
    assert combined.index("Group 0001:") < combined.index("Group 0002:")
    assert combined.index("Group 0002:") < combined.index("Group 0003:")
    with mock.patch(
        "lib.filesystem_batch._report_group", side_effect=ValueError("bad report")
    ):
        assert report_batch(manifest.root, groups="0002") == 1
    combined = omnibus.read_text(encoding="utf-8")
    assert "Group 0002:" not in combined
    assert "Group 0001:" in combined and "Group 0003:" in combined
    (manifest.root / "reports/groups/0003/report.txt").write_text(
        "foreign content", encoding="utf-8"
    )
    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert report_batch(manifest.root, groups="0001") == 0
    assert not omnibus.exists()
    assert "reports.txt" not in (manifest.root / "reports/index.md").read_text(
        encoding="utf-8"
    )


def test_single_group_batch_does_not_write_omnibus(tmp_path):
    manifest = _create_batch(tmp_path / "batch", kinds=("io",))
    _publish_result(manifest, manifest.groups[0])

    def report(_manifest, _group, _successful, destination, _options):
        (destination / "report.txt").write_text("single report\n", encoding="utf-8")

    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert report_batch(manifest.root) == 0
    assert not (manifest.root / "reports.txt").exists()


@pytest.mark.parametrize("markdown", [False, True])
def test_omnibus_keeps_actual_metadata_reports_together_by_saved_environment(
    tmp_path, markdown
):
    """Actual metadata reports stay contiguous beneath their own override settings."""
    manifest = _create_batch(tmp_path / "batch", kinds=("io", "mdtest", "mdtest"))
    for group in manifest.groups[1:]:
        snapshot = manifest.group_path(group) / ENV_YAML
        previous_digest = _digest(snapshot)
        snapshot.write_text(
            snapshot.read_text(encoding="utf-8")
            + f"env_override:\n  file: /saved/overrides/{group.group_id}.env\n",
            encoding="utf-8",
        )
        _replace_manifest(manifest.root, previous_digest, _digest(snapshot))
    manifest = read_batch_manifest(manifest.root)
    for group in manifest.groups:
        _publish_result(manifest, group)
    io_reporter = load_extract_elbencho_module("omnibus_io_tables")
    metadata_path = (
        Path(__file__).resolve().parents[1] / "utils/extract-mdtest-elbencho.py"
    )
    spec = importlib.util.spec_from_file_location(
        "omnibus_metadata_tables", metadata_path
    )
    metadata_reporter = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = metadata_reporter
    spec.loader.exec_module(metadata_reporter)
    io_metric = io_reporter.ElbenchoMetrics(
        nodes=1,
        io_size="1M",
        threads=1,
        io_depth=1,
        operation="WRITE",
        datestamp=DATESTAMP,
        is_multi_node=False,
        command="elbencho --write",
        file_size_bytes=1024**3,
        direct_io=1,
        random_io=0,
        iops=100,
        throughput_mib_s=100,
        throughput_mb_s=104.8576,
        throughput_gbps=0.8,
        min_lat_sec=0.001,
        avg_lat_sec=0.002,
        max_lat_sec=0.003,
        lat_pct_1=0.001,
        lat_pct_50=0.002,
        lat_pct_75=0.002,
        lat_pct_99=0.003,
    )
    metadata_metric = metadata_reporter.AggregatedMetrics(
        node_count=2,
        thread_count=4,
        datestamps={DATESTAMP},
        iteration_count=1,
        create_rate_avg=100,
        stat_rate_avg=200,
        delete_rate_avg=300,
        create_command="elbencho --mkdir",
        stat_command="elbencho --stat",
        delete_command="elbencho --delete",
    )
    contents = {}

    def report(_manifest, group, _successful, destination, _options):
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            if group.kind == "io":
                io_reporter.print_terminal_table([io_metric])
            else:
                printer = (
                    metadata_reporter.print_markdown_report
                    if markdown
                    else metadata_reporter.print_terminal_table
                )
                printer(
                    [metadata_metric],
                    test_configuration={"MDTEST_ITERATIONS": group.group_id},
                )
        contents[group.group_id] = captured.getvalue()
        (destination / "report.txt").write_text(
            contents[group.group_id], encoding="utf-8"
        )

    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert report_batch(manifest.root) == 0
    combined = (manifest.root / "reports.txt").read_text(encoding="utf-8")
    assert "TEST_ROOT: /saved/0001\n" not in combined
    previous_end = combined.index(contents["0001"]) + len(contents["0001"])
    for group in manifest.groups[1:]:
        environment = (manifest.group_path(group) / ENV_YAML).read_text(
            encoding="utf-8"
        )
        assert combined.count(environment) == 1
        report_start = combined.index(contents[group.group_id])
        assert environment in combined[previous_end:report_start]
        previous_end = report_start + len(contents[group.group_id])
        for operation in ("Create", "Stat", "Delete"):
            assert f"{operation} Latency Percentiles" in contents[group.group_id]
        assert "Performance Rates" in contents[group.group_id]
        assert "Average Phase Elapsed Times" in contents[group.group_id]
        for command in ("elbencho --mkdir", "elbencho --stat", "elbencho --delete"):
            assert command in contents[group.group_id]
        assert (
            manifest.root / "reports/groups" / group.group_id / "report.txt"
        ).read_text(encoding="utf-8") == contents[group.group_id]


def test_failed_omnibus_publication_removes_stale_report_and_keeps_group_reports(
    tmp_path,
):
    """An aggregate publication error cannot leave prior tables looking current."""
    manifest = _create_batch(tmp_path / "batch", kinds=("io", "mdtest"))
    for group in manifest.groups:
        _publish_result(manifest, group)
    omnibus = manifest.root / "reports.txt"

    def report(_manifest, group, _successful, destination, _options):
        (destination / "report.txt").write_text(group.group_id, encoding="utf-8")

    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert report_batch(manifest.root) == 0
        assert omnibus.exists()
        real_replace = os.replace

        def replace(source, destination):
            if Path(destination) == omnibus:
                raise OSError("full disk")
            return real_replace(source, destination)

        with mock.patch("lib.filesystem_batch.os.replace", side_effect=replace):
            assert report_batch(manifest.root) == 1
    assert not omnibus.exists()
    index = (manifest.root / "reports/index.md").read_text(encoding="utf-8")
    assert "reports.txt" not in index
    for group in manifest.groups:
        assert f"groups/{group.group_id}/report.txt" in index
        assert (
            manifest.root / "reports/groups" / group.group_id / "report.txt"
        ).exists()
    assert not list(manifest.root.glob(".index-*"))


def test_failed_stale_omnibus_deletion_does_not_link_old_tables(tmp_path, capsys):
    """An undeletable prior aggregate cannot certify freshly generated reports."""
    manifest = _create_batch(tmp_path / "batch", kinds=("io", "mdtest"))
    for group in manifest.groups:
        _publish_result(manifest, group)
    omnibus = manifest.root / "reports.txt"

    def report(_manifest, group, _successful, destination, options):
        (destination / "report.txt").write_text(
            f"Group {group.group_id}: {options}\n", encoding="utf-8"
        )

    real_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == omnibus:
            raise PermissionError(
                errno.EACCES, "permission denied for stale aggregate", str(path)
            )
        return real_unlink(path, *args, **kwargs)

    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert report_batch(manifest.root) == 0
        previous = omnibus.read_bytes()
        with mock.patch("lib.filesystem_batch.Path.unlink", new=unlink):
            assert report_batch(manifest.root, engine_options=["fresh"]) == 1
    assert omnibus.read_bytes() == previous
    assert "fresh" not in omnibus.read_text(encoding="utf-8")
    index = (manifest.root / "reports/index.md").read_text(encoding="utf-8")
    assert "reports.txt" not in index
    for group in manifest.groups:
        assert f"groups/{group.group_id}/report.txt" in index
        assert (
            manifest.root / "reports/groups" / group.group_id / "report.txt"
        ).read_text(encoding="utf-8") == f"Group {group.group_id}: ['fresh']\n"
    diagnostic = capsys.readouterr().err
    assert "permission denied for stale aggregate" in diagnostic
    assert str(omnibus) in diagnostic


@pytest.mark.parametrize("markdown", [False, True])
def test_omnibus_keeps_real_io_report_titles_and_tables_adjacent(tmp_path, markdown):
    """One summary precedes unchanged real WRITE/READ reports in either format."""
    manifest = _create_batch(tmp_path / "batch", kinds=("io", "io"))
    repository = Path(__file__).resolve().parents[1]
    renderer = """
from dataclasses import replace
import importlib.util
from pathlib import Path
import sys

spec = importlib.util.spec_from_file_location(
    "actual_io_reporter", Path(sys.argv[1]) / "utils/extract-elbencho.py"
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
metric = module.ElbenchoMetrics(
    nodes=int(sys.argv[3]), io_size="1M", threads=1, io_depth=1,
    operation="WRITE", datestamp="20260930Z120000", is_multi_node=True,
    command="elbencho --write --read", file_size_bytes=1024**3,
    direct_io=1, random_io=0, iops=100, throughput_mib_s=100,
    throughput_mb_s=104.8576, throughput_gbps=0.8,
    min_lat_sec=0.001, avg_lat_sec=0.002, max_lat_sec=0.003,
    lat_pct_1=0.001, lat_pct_50=0.002, lat_pct_75=0.002, lat_pct_99=0.003,
)
printer = module.print_markdown_table if sys.argv[2] == "True" else module.print_terminal_table
printer([metric, replace(metric, operation="READ")])
"""
    contents = {}
    for group in manifest.groups:
        _publish_result(manifest, group)
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                renderer,
                str(repository),
                str(markdown),
                group.group_id,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        contents[group.group_id] = completed.stdout

    def report(_manifest, group, _successful, destination, _options):
        (destination / "report.txt").write_text(
            contents[group.group_id], encoding="utf-8"
        )

    with mock.patch("lib.filesystem_batch._report_group", side_effect=report):
        assert report_batch(manifest.root) == 0
    combined = (manifest.root / "reports.txt").read_text(encoding="utf-8")
    previous_end = 0
    for group in manifest.groups:
        content = contents[group.group_id]
        assert "WRITE" in content and "READ" in content
        start = combined.index(content)
        summary = combined[previous_end:start]
        assert summary.count("Runner:") == 1
        assert summary.count("Files/sizing:") == 1
        assert summary.count("Override:") == 1
        assert "```yaml" not in summary
        assert (
            manifest.root / "reports/groups" / group.group_id / "report.txt"
        ).read_text(encoding="utf-8") == content
        previous_end = start + len(content)


@pytest.mark.parametrize("kind", ["io", "mdtest"])
@pytest.mark.parametrize("mode", ["--from-csv", "--test-parse"])
def test_unified_front_door_delegates_native_input_modes(
    tmp_path, unified_reporter, kind, mode
):
    path = tmp_path / "a 'quoted' file.csv"
    option = "--only-iodepths=1" if kind == "io" else "--normalize-to=1"
    with mock.patch.object(unified_reporter.subprocess, "run") as engine:
        engine.return_value.returncode = 7
        assert (
            unified_reporter.main(
                ["--kind", kind, mode, str(path), option, "--markdown"]
            )
            == 7
        )
    command = engine.call_args.args[0]
    assert command[1].endswith(
        "extract-elbencho.py" if kind == "io" else "extract-mdtest-elbencho.py"
    )
    assert f"{mode}={path}" in command
    assert option in command
    assert "--markdown" in command


def test_unified_ordinary_directory_inference_and_options(tmp_path, unified_reporter):
    results = tmp_path / "mdtest-elbencho-example"
    with mock.patch.object(unified_reporter.subprocess, "run") as engine:
        engine.return_value.returncode = 0
        assert (
            unified_reporter.main(
                [
                    str(results),
                    "--normalize-to=2",
                    "--to-csv",
                    "--output-dir",
                    str(tmp_path),
                ]
            )
            == 0
        )
    command = engine.call_args.args[0]
    assert command[1].endswith("extract-mdtest-elbencho.py")
    assert "--normalize-to=2" in command
    assert "--to-csv" in command
    assert command[2] == str(results)


@pytest.mark.parametrize(
    "arguments, message",
    [
        ([], "results_dir is required"),
        (["--from-csv", "cached.csv"], "requires --kind"),
        (["--test-parse", "raw.csv"], "requires --kind"),
        (["--kind", "mdtest", "--from-csv", "cache", "raw"], "cannot be combined"),
        (
            ["--kind", "mdtest", "--from-csv", "cache", "--test-parse", "raw"],
            "cannot be combined",
        ),
        (["--kind", "io", "--normalize-to", "1", "raw"], "no selected mdtest group"),
    ],
)
def test_unified_ordinary_option_errors(unified_reporter, capsys, arguments, message):
    with mock.patch.object(unified_reporter.subprocess, "run") as engine:
        with pytest.raises(SystemExit) as exited:
            unified_reporter.main(arguments)
        engine.assert_not_called()
    assert exited.value.code == 2
    assert message in capsys.readouterr().err


def test_unified_real_metadata_report_exports_and_reimports_normalized_metrics(
    tmp_path, reporting_checkout
):
    """Exercise actual engines and wrappers, not just option-forwarding mocks."""
    from tests.test_extract_mdtest_elbencho import _DENSE_CSV, _DENSE_OUT

    manifest = _create_batch(tmp_path / "batch", kinds=("mdtest",))
    group = manifest.groups[0]
    source = _publish_result(manifest, group)
    (source / f"{MD_STEM}.csv").write_text(_DENSE_CSV, encoding="utf-8")
    (source / f"{MD_STEM}.out").write_text(_DENSE_OUT, encoding="utf-8")
    wrapper = reporting_checkout / "utils/extract-filesystem.sh"
    completed = subprocess.run(
        [
            "bash",
            str(wrapper),
            "--normalize-to",
            "1",
            "--only-nodes",
            "2",
            "--only-threads",
            "4",
            "--to-csv",
            "--markdown",
            str(manifest.root),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = manifest.root / "reports/groups/0001/report.txt"
    assert "normalized to 1 node" in report.read_text(encoding="utf-8")
    exported = list(report.parent.glob("mdtest-elbencho-metrics-*.csv"))
    assert len(exported) == 1
    completed = subprocess.run(
        [
            "bash",
            str(wrapper),
            "--kind",
            "mdtest",
            "--from-csv",
            str(exported[0]),
            "--normalize-to",
            "1",
            "--markdown",
            "--output-dir",
            str(tmp_path / "cached"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "normalized to 1 node" in completed.stdout


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
