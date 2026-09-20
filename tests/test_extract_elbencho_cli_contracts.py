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
    """Return one valid metric suitable for a CSV-only report invocation."""
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


def test_from_csv_rejects_raw_result_directories(monkeypatch, tmp_path):
    """CSV input is an alternative source, never a second source to merge."""
    with pytest.raises(SystemExit) as raised:
        _run_main(
            monkeypatch,
            "--from-csv",
            str(tmp_path / "metrics.csv"),
            str(tmp_path / "raw-results"),
        )
    assert raised.value.code == 2


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
    csv_path = tmp_path / "metrics.csv"
    _EXTRACT.write_csv(str(csv_path), [_metric()])

    with pytest.raises(SystemExit) as raised:
        _run_main(
            monkeypatch,
            "--from-csv",
            str(csv_path),
            "--only-nodes",
            "99",
            "--markdown",
        )
    assert raised.value.code == 1


def test_csv_round_trip_preserves_report_dimensions(tmp_path):
    """Persisted metrics retain the dimensions needed by later filtering."""
    metrics = [
        _metric(),
        replace(
            _metric(),
            nodes=2,
            io_size="1M,r4K",
            threads=8,
            io_depth=4,
            operation="READ",
            random_io=1,
        ),
    ]
    path = tmp_path / "metrics.csv"
    _EXTRACT.write_csv(str(path), metrics)
    loaded = _EXTRACT.read_csv(str(path))
    assert [
        (
            item.nodes,
            item.io_size,
            item.threads,
            item.io_depth,
            item.operation,
            item.random_io,
        )
        for item in loaded
    ] == [
        (1, "4K", 1, 1, "WRITE", 0),
        (2, "1M,r4K", 8, 4, "READ", 1),
    ]


def test_csv_cli_filters_compound_size_and_forwards_plot_mode(monkeypatch, tmp_path):
    """CLI filters preserve compound sizes and the single-axis request."""
    path = tmp_path / "metrics.csv"
    _EXTRACT.write_csv(
        str(path),
        [
            _metric(),
            replace(
                _metric(),
                nodes=2,
                io_size="1M,r4K",
                threads=8,
                io_depth=4,
                operation="READ",
            ),
        ],
    )
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
        "--from-csv",
        str(path),
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


def test_csv_cli_can_reemit_cache_and_default_terminal_report(monkeypatch, tmp_path):
    """CSV input supports cache output and the normal mirrored text report."""
    source = tmp_path / "source.csv"
    _EXTRACT.write_csv(str(source), [_metric()])
    mirrored = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        _EXTRACT,
        "mirror_stdout_to_file",
        lambda path, printer, metrics: mirrored.append((path, printer, metrics)),
    )
    monkeypatch.setattr(_EXTRACT, "plot_metrics", lambda *_arguments: None)

    _run_main(monkeypatch, "--from-csv", str(source), "--to-csv")

    cache = tmp_path / "elbencho-metrics.csv"
    assert cache.is_file()
    assert len(_EXTRACT.read_csv(str(cache))) == 1
    assert len(mirrored) == 1
    assert mirrored[0][0] == str(tmp_path / _EXTRACT.REPORT_TXT_FILENAME)
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
def test_live_report_limits_must_be_positive(monkeypatch, option):
    """Invalid live-report limits fail at the CLI boundary."""
    with pytest.raises(SystemExit) as raised:
        _run_main(monkeypatch, "--from-csv", "unused.csv", option, "0")
    assert raised.value.code == 2
