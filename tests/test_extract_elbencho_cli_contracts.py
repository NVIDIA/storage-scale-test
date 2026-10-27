#!/usr/bin/env python3

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

"""Fast CLI contract tests for the Elbencho report extractor."""

import sys
from dataclasses import replace

import pytest

from tests.extract_elbencho_test_support import load_extract_elbencho_module

_EXTRACT = load_extract_elbencho_module("extract_elbencho_cli_contracts")


def _metric():
    """Return one valid metric suitable for a stubbed report invocation."""
    return _EXTRACT.ElbenchoMetrics(
        nodes=1,
        io_size="4K",
        threads=1,
        io_depth=1,
        operation="WRITE",
        datestamp="20260101Z000000",
        is_multi_node=False,
        command="elbencho --size 4K",
        file_size_bytes=4096,
        direct_io=0,
        random_io=0,
        iops=1.0,
        throughput_mib_s=1.0,
        throughput_mb_s=1.048576,
        throughput_gbps=0.008,
        min_lat_sec=0.001,
        avg_lat_sec=0.002,
        max_lat_sec=0.003,
        lat_pct_1=0.001,
        lat_pct_50=0.002,
        lat_pct_75=0.003,
        lat_pct_99=0.004,
    )


def _run_main(monkeypatch, *arguments):
    """Run the extractor entry point with a temporary argv."""
    monkeypatch.setattr(sys, "argv", ["extract-elbencho.py", *arguments])
    return _EXTRACT.main()


@pytest.mark.parametrize(
    "option, value",
    [
        ("--only-threads", "1,,2"),
        ("--only-nodes", "2-1"),
        ("--only-iodepths", "not-an-integer"),
        ("--only-sizes", "4K;;1M"),
        ("--only-sizes", "not-a-size"),
    ],
)
def test_filter_parser_rejects_malformed_syntax(option, value):
    """Malformed filters fail instead of silently broadening a report."""
    if option == "--only-sizes":
        with pytest.raises(ValueError):
            _EXTRACT.parse_only_sizes_filter([value])
    else:
        with pytest.raises(ValueError):
            _EXTRACT.parse_int_values_with_ranges(value)


def test_valid_filter_matching_no_metrics_is_an_error(monkeypatch, tmp_path):
    """A successful process must not hide an accidentally empty report."""
    directory = _target_dir(tmp_path, "a", None)
    monkeypatch.setattr(_EXTRACT, "parse_elbencho_files", lambda _base: [_metric()])

    with pytest.raises(SystemExit) as raised:
        _run_main(
            monkeypatch,
            str(directory),
            "--only-nodes",
            "99",
            "--markdown",
        )
    assert raised.value.code == 1


def _mixed_report_input(tmp_path, live_nodes):
    """Create recognizable aggregate and live filenames for CLI tests."""
    stem = "elbencho-4K-c_001-s_001-d_001_20260101Z000000"
    (tmp_path / f"{stem}.csv").write_text("aggregate\n", encoding="utf-8")
    (tmp_path / f"{stem}.out").write_text("aggregate\n", encoding="utf-8")
    live_stem = f"elbencho-4K-c_{live_nodes:03d}-s_001-d_001_20260101Z000000"
    (tmp_path / f"{live_stem}.live.csv").write_text("live\n", encoding="utf-8")


def test_live_match_cannot_hide_empty_filtered_aggregate(monkeypatch, tmp_path):
    """Each discovered report family must independently survive filtering."""
    _mixed_report_input(tmp_path, live_nodes=2)
    monkeypatch.setattr(_EXTRACT, "parse_elbencho_files", lambda _base: [_metric()])

    with pytest.raises(SystemExit) as raised:
        _run_main(
            monkeypatch,
            str(tmp_path),
            "--only-nodes",
            "2",
            "--markdown",
        )

    assert raised.value.code == 1


def test_failed_live_reports_cannot_hide_behind_aggregate(monkeypatch, tmp_path):
    """Aggregate output does not turn a failed live report into success."""
    _mixed_report_input(tmp_path, live_nodes=1)
    monkeypatch.setattr(_EXTRACT, "parse_elbencho_files", lambda _base: [_metric()])
    monkeypatch.setattr(_EXTRACT, "print_markdown_table", lambda *_args: None)
    monkeypatch.setattr(_EXTRACT, "plot_metrics", lambda *_args: None)
    monkeypatch.setattr(_EXTRACT, "_report_one_live_file", lambda *_args: False)

    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, str(tmp_path), "--markdown")

    assert raised.value.code == 1


def test_cli_filters_compound_size_and_forwards_plot_mode(monkeypatch, tmp_path):
    """CLI filters preserve compound sizes and the single-axis request."""
    directory = _target_dir(tmp_path, "a", None)
    metrics = [
        _metric(),
        replace(
            _metric(),
            nodes=2,
            io_size="1M,r4K",
            threads=8,
            io_depth=4,
            operation="READ",
        ),
    ]
    monkeypatch.setattr(_EXTRACT, "parse_elbencho_files", lambda _base: metrics)
    reported = []
    plotted = []
    monkeypatch.setattr(
        _EXTRACT,
        "print_markdown_table",
        lambda metrics, single_axis: reported.append((metrics, single_axis)),
    )
    monkeypatch.setattr(
        _EXTRACT,
        "plot_metrics",
        lambda metrics, output_dir, single_axis: plotted.append(
            (metrics, output_dir, single_axis)
        ),
    )

    _run_main(
        monkeypatch,
        str(directory),
        "--only-nodes",
        "2",
        "--only-threads",
        "8",
        "--only-iodepths",
        "4",
        "--only-sizes",
        "1M,r4K",
        "--no-dual-y-axis",
        "--markdown",
    )

    assert len(reported) == len(plotted) == 1
    assert reported[0][1] is True
    assert plotted[0][2] is True
    assert [item.operation for item in reported[0][0]] == ["READ"]


def test_cli_default_terminal_report_is_mirrored_to_a_text_file(monkeypatch, tmp_path):
    """Without --markdown the normal mirrored text report is written."""
    directory = _target_dir(tmp_path, "a", None)
    monkeypatch.setattr(_EXTRACT, "parse_elbencho_files", lambda _base: [_metric()])
    mirrored = []
    monkeypatch.setattr(
        _EXTRACT,
        "mirror_stdout_to_file",
        lambda path, printer, metrics: mirrored.append((path, printer, metrics)),
    )
    monkeypatch.setattr(_EXTRACT, "plot_metrics", lambda *_arguments: None)

    _run_main(monkeypatch, str(directory))

    assert len(mirrored) == 1
    assert mirrored[0][0] == str(directory / _EXTRACT.REPORT_TXT_FILENAME)
    assert [item.operation for item in mirrored[0][2]] == ["WRITE"]


@pytest.mark.parametrize(
    "option",
    [
        "--client-outlier-threshold",
        "--client-min-underperform-segments",
        "--client-max-timeseries-lines",
        "--client-max-heatmap-rows",
    ],
)
def test_live_report_limits_must_be_positive(monkeypatch, tmp_path, option):
    """Invalid live-report limits fail at the CLI boundary."""
    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, str(tmp_path), option, "0")
    assert raised.value.code == 2


TARGET_LINE = "Storage target: "
_ALLOW_MIXED = "--allow-mixed-targets"


def _target_dir(root, name, target):
    """A result directory whose snapshot names ``target`` (None: no snapshot)."""
    directory = root / name
    directory.mkdir()
    stem = "elbencho-4K-c_001-s_001-d_001_20260101Z000000"
    (directory / f"{stem}.csv").write_text("aggregate\n", encoding="utf-8")
    (directory / f"{stem}.out").write_text("aggregate\n", encoding="utf-8")
    if target is not None:
        (directory / "env_used.yaml").write_text(
            f"STORAGE_TARGET_NAME: {target}\n", encoding="utf-8"
        )
    return directory


@pytest.fixture(name="stubbed_report")
def stubbed_report_fixture(monkeypatch):
    """Parse every pair as one metric; record what a run would have plotted."""
    plotted = []
    monkeypatch.setattr(_EXTRACT, "parse_elbencho_files", lambda _base: [_metric()])
    monkeypatch.setattr(_EXTRACT, "print_markdown_table", lambda *_args: None)
    monkeypatch.setattr(_EXTRACT, "print_terminal_table", lambda *_args: None)
    monkeypatch.setattr(_EXTRACT, "plot_metrics", lambda *args: plotted.append(args))
    return plotted


def _target_lines(output):
    return [line for line in output.splitlines() if TARGET_LINE in line]


def test_mixed_targets_exit_before_anything_is_written(
    monkeypatch, tmp_path, capsys, stubbed_report
):
    first = _target_dir(tmp_path, "a", "lustre-a")
    second = _target_dir(tmp_path, "b", "weka-b")
    output = tmp_path / "out"
    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, str(first), str(second), f"--output-dir={output}")
    assert raised.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.count("ERROR: ") == 1
    for needle in ('"lustre-a": ', str(first), '"weka-b": ', str(second)):
        assert needle in captured.err
    assert "--allow-mixed-targets" in captured.err
    assert not output.exists()
    assert not list(tmp_path.rglob("report.txt"))
    assert not stubbed_report


def test_allowed_mixed_targets_warn_and_label_the_report(
    monkeypatch, tmp_path, capsys, stubbed_report
):
    first = _target_dir(tmp_path, "a", "lustre-a")
    second = _target_dir(tmp_path, "b", "weka-b")
    _run_main(monkeypatch, str(first), str(second), "--markdown", _ALLOW_MIXED)
    captured = capsys.readouterr()
    assert _target_lines(captured.out) == [
        '- Storage target: mixed: "lustre-a", "weka-b"'
    ]
    assert "WARNING: report mixes storage targets" in captured.err
    assert len(stubbed_report) == 1


def test_one_target_is_named_in_both_report_forms(
    monkeypatch, tmp_path, capsys, stubbed_report
):
    first = _target_dir(tmp_path, "a", "lustre-a")
    second = _target_dir(tmp_path, "b", "lustre-a")
    _run_main(monkeypatch, str(first), str(second), "--markdown")
    assert _target_lines(capsys.readouterr().out) == ["- Storage target: lustre-a"]
    _run_main(monkeypatch, str(first), str(second))
    assert "Storage target: lustre-a\n" in (first / "report.txt").read_text("utf-8")
    assert stubbed_report


def test_known_and_unverifiable_inputs_warn_without_failing(
    monkeypatch, tmp_path, capsys, stubbed_report
):
    first = _target_dir(tmp_path, "a", "lustre-a")
    second = _target_dir(tmp_path, "b", None)
    _run_main(monkeypatch, str(first), str(second), "--markdown")
    captured = capsys.readouterr()
    assert _target_lines(captured.out) == ["- Storage target: lustre-a"]
    assert f"cannot verify storage target of 1 input(s): {second}" in captured.err
    assert stubbed_report


def test_unknown_targets_are_silent(monkeypatch, tmp_path, capsys, stubbed_report):
    directory = _target_dir(tmp_path, "a", None)
    _run_main(monkeypatch, str(directory), "--markdown")
    captured = capsys.readouterr()
    assert _target_lines(captured.out) == ["- Storage target: unknown"]
    assert "WARNING" not in captured.err
    assert stubbed_report


def test_snapshot_without_name_derives_it_from_the_roots(
    monkeypatch, tmp_path, capsys, stubbed_report
):
    directory = _target_dir(tmp_path, "a", None)
    (directory / "env_used.yaml").write_text(
        'TEST_DIRS:\n  "/mnt/b/": 1\n  "/mnt/a": 1\n', encoding="utf-8"
    )
    _run_main(monkeypatch, str(directory), "--markdown")
    assert _target_lines(capsys.readouterr().out) == ["- Storage target: /mnt/a,/mnt/b"]
    assert stubbed_report


@pytest.mark.parametrize(
    "filters",
    [
        ["--only-nodes", "1"],
        ["--only-nodes", "2"],
        ["--only-nodes", "99"],
        ["--only-threads", "99"],
        ["--only-sizes", "99G"],
    ],
)
def test_guard_runs_before_the_only_filters(
    monkeypatch, tmp_path, capsys, stubbed_report, filters
):
    """A filter that keeps one target's rows cannot hide the other target."""
    first = _target_dir(tmp_path, "a", "lustre-a")
    second = _target_dir(tmp_path, "b", "weka-b")
    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, str(first), str(second), *filters)
    assert raised.value.code == 1
    err = capsys.readouterr().err
    assert "inputs come from different storage targets" in err
    assert not stubbed_report


def test_allowed_mixed_label_ignores_the_only_filters(
    monkeypatch, tmp_path, capsys, stubbed_report
):
    first = _target_dir(tmp_path, "a", "lustre-a")
    second = _target_dir(tmp_path, "b", "weka-b")
    _run_main(
        monkeypatch,
        str(first),
        str(second),
        "--only-nodes",
        "1",
        "--markdown",
        _ALLOW_MIXED,
    )
    assert _target_lines(capsys.readouterr().out) == [
        '- Storage target: mixed: "lustre-a", "weka-b"'
    ]
    assert stubbed_report


def test_mixed_label_with_a_quote_in_a_name_is_not_split(
    monkeypatch, tmp_path, capsys, stubbed_report
):
    first = _target_dir(tmp_path, "a", "'x\", \"y'")
    second = _target_dir(tmp_path, "b", "z")
    _run_main(monkeypatch, str(first), str(second), "--markdown", _ALLOW_MIXED)
    assert _target_lines(capsys.readouterr().out) == [
        '- Storage target: mixed: "x\\", \\"y", "z"'
    ]
    assert stubbed_report
