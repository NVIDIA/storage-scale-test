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

"""Tests for shared benchmark-report helpers."""

import re
from types import SimpleNamespace

from lib.reporting_common import (
    discover_result_pairs,
    filter_metrics_by_scale,
    format_decimal_aligned_latency_ms,
    histogram_axis_ranges,
    latency_column_widths,
    parse_int_values_with_ranges,
    parse_integer_histogram,
    strip_command_options,
)


def test_discover_result_pairs_returns_complete_pairs_and_warns(tmp_path):
    """Discovery keeps complete pairs while explaining incomplete inputs."""
    for filename in ("result-1.csv", "result-1.out", "result-2.csv"):
        (tmp_path / filename).touch()

    warnings = []
    pairs = discover_result_pairs(
        [str(tmp_path)],
        re.compile(r"result-(\d+)\.(csv|out)"),
        lambda match: int(match.group(1)),
        extension_group=2,
        warn=warnings.append,
    )

    assert pairs == {
        1: (str(tmp_path / "result-1.csv"), str(tmp_path / "result-1.out"))
    }
    assert warnings == [f"Warning: Missing .out file for {tmp_path / 'result-2.csv'}"]


def test_discover_result_pairs_never_joins_different_directories(tmp_path):
    """Matching names in separate runs cannot make one result pair."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "result-1.csv").touch()
    (second / "result-1.out").touch()
    warnings = []

    pairs = discover_result_pairs(
        [str(first), str(second)],
        re.compile(r"result-(\d+)\.(csv|out)"),
        lambda match: int(match.group(1)),
        extension_group=2,
        warn=warnings.append,
    )

    assert pairs == {}
    assert len(warnings) == 2
    assert all("Missing" in warning for warning in warnings)


def test_discover_result_pairs_rejects_duplicate_complete_keys(tmp_path):
    """Two complete runs with the same key are ambiguous, even if ordered."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    for directory in (first, second):
        (directory / "result-1.csv").touch()
        (directory / "result-1.out").touch()
    warnings = []

    pairs = discover_result_pairs(
        [str(first), str(second), str(first)],
        re.compile(r"result-(\d+)\.(csv|out)"),
        lambda match: int(match.group(1)),
        extension_group=2,
        warn=warnings.append,
    )

    assert pairs == {}
    assert len(warnings) == 1
    assert "Duplicate result pair" in warnings[0]


def test_discover_result_pairs_rejects_normalized_name_aliases(tmp_path):
    """Two CSV spellings for one numeric key cannot silently overwrite."""
    for filename in ("result-1.csv", "result-01.csv", "result-1.out"):
        (tmp_path / filename).touch()
    warnings = []

    pairs = discover_result_pairs(
        [str(tmp_path)],
        re.compile(r"result-(\d+)\.(csv|out)"),
        lambda match: int(match.group(1)),
        extension_group=2,
        warn=warnings.append,
    )

    assert pairs == {}
    assert len(warnings) == 1
    assert "Duplicate .csv artifact" in warnings[0]


def test_discover_result_pairs_keeps_distinct_keys_across_directories(tmp_path):
    """Separate runs remain usable when their logical keys are distinct."""
    expected = {}
    for name, key in (("first", 1), ("second", 2)):
        directory = tmp_path / name
        directory.mkdir()
        csv_path = directory / f"result-{key}.csv"
        out_path = directory / f"result-{key}.out"
        csv_path.touch()
        out_path.touch()
        expected[key] = (str(csv_path), str(out_path))
    warnings = []

    pairs = discover_result_pairs(
        [str(tmp_path / "first"), str(tmp_path / "second")],
        re.compile(r"result-(\d+)\.(csv|out)"),
        lambda match: int(match.group(1)),
        extension_group=2,
        warn=warnings.append,
    )

    assert pairs == expected
    assert warnings == []


def test_report_parsers_and_formatters():
    """Shared parsers preserve the analyzer-facing formatting behavior."""
    warnings = []
    assert parse_integer_histogram("1: 2, bad, 3: 4") == {1: 2, 3: 4}
    assert parse_integer_histogram("1: 2, 1: 3, 2: 4") == {1: 5, 2: 4}
    assert parse_int_values_with_ranges("1,3-5,bad", warnings.append) == {
        1,
        3,
        4,
        5,
    }
    assert warnings == ["Warning: Invalid integer: bad"]
    assert latency_column_widths([0.0012, 12]) == (2, 4)
    assert format_decimal_aligned_latency_ms(0.2, 2, 2, 5) == " 0.20"
    assert (
        strip_command_options(
            "elbencho --hosts 'a,b' --csvfile=x /data --read", {"--hosts", "--csvfile"}
        )
        == "elbencho --read"
    )


def test_scale_filter_and_histogram_ranges():
    """Scale filters and padded log bounds handle both data and defaults."""
    metrics = [
        SimpleNamespace(node_count=1, thread_count=4),
        SimpleNamespace(node_count=2, thread_count=8),
    ]
    assert filter_metrics_by_scale(metrics, {2}, {8}) == [metrics[1]]
    assert histogram_axis_ranges([([1.0, 10.0], [0.0, 5.0])]) == {
        "min_latency": 0.9,
        "max_latency": 11.0,
        "min_count": 4.5,
        "max_count": 5.5,
    }
    assert histogram_axis_ranges([]) == {
        "min_latency": 0.1,
        "max_latency": 1100.0,
        "min_count": 1.0,
        "max_count": 110.00000000000001,
    }
    assert histogram_axis_ranges([([0.0012, 0.091], [4080.0, 1.0])]) == {
        "min_latency": 0.00108,
        "max_latency": 0.10010000000000001,
        "min_count": 1.0,
        "max_count": 4488.0,
    }
