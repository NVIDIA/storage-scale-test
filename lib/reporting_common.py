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

"""Shared parsing and presentation helpers for benchmark result reports."""

import argparse
import os
import re
import shlex
from collections.abc import Callable, Hashable, Iterable, Sequence
from typing import Any, TypeVar

_ResultKey = TypeVar("_ResultKey", bound=Hashable)
_Metric = TypeVar("_Metric")


def discover_result_pairs(
    input_dirs: Sequence[str],
    filename_pattern: re.Pattern[str],
    key_from_match: Callable[[re.Match[str]], _ResultKey],
    extension_group: int,
    warn: Callable[[str], Any],
) -> dict[_ResultKey, tuple[str, str]]:
    """Find complete CSV/OUT result pairs matching a benchmark filename pattern."""
    file_pairs: dict[_ResultKey, dict[str, str]] = {}

    for input_dir in input_dirs:
        if not os.path.isdir(input_dir):
            warn(f"Warning: {input_dir} is not a directory, skipping")
            continue

        for filename in os.listdir(input_dir):
            match = filename_pattern.match(filename)
            if not match:
                continue
            key = key_from_match(match)
            file_pairs.setdefault(key, {})[match.group(extension_group)] = os.path.join(
                input_dir, filename
            )

    result: dict[_ResultKey, tuple[str, str]] = {}
    for key, paths in file_pairs.items():
        if "csv" in paths and "out" in paths:
            result[key] = (paths["csv"], paths["out"])
        elif "csv" in paths:
            warn(f"Warning: Missing .out file for {paths['csv']}")
        elif "out" in paths:
            warn(f"Warning: Missing .csv file for {paths['out']}")
    return result


def parse_integer_histogram(histogram_text: str) -> dict[int, int]:
    """Parse comma-separated ``bucket: count`` integer histogram entries."""
    histogram: dict[int, int] = {}
    for item in histogram_text.split(","):
        parts = item.strip().split(":")
        if len(parts) != 2:
            continue
        try:
            histogram[int(parts[0].strip())] = int(parts[1].strip())
        except ValueError:
            continue
    return histogram


def format_latency_ms(latency_ms: float) -> str:
    """Format a millisecond latency with magnitude-appropriate precision."""
    if latency_ms == 0:
        return "0"
    if latency_ms < 0.01:
        return f"{latency_ms:.4f}"
    if latency_ms < 0.1:
        return f"{latency_ms:.3f}"
    if latency_ms < 1:
        return f"{latency_ms:.2f}"
    if latency_ms < 10:
        return f"{latency_ms:.1f}"
    return f"{latency_ms:.0f}"


def latency_column_widths(values: Iterable[float]) -> tuple[int, int]:
    """Return maximum character widths before and after the decimal point."""
    max_before = 0
    max_after = 0
    for value in values:
        before, separator, after = format_latency_ms(value).partition(".")
        max_before = max(max_before, len(before))
        if separator:
            max_after = max(max_after, len(after))
    return max_before, max_after


def format_decimal_aligned_latency_ms(
    latency_ms: float, before_width: int, after_width: int, total_width: int
) -> str:
    """Format a latency with its decimal point aligned to a column."""
    formatted = format_latency_ms(latency_ms)
    before, separator, after = formatted.partition(".")
    if separator:
        result = f"{before:>{before_width}}.{after.ljust(after_width)}"
    elif after_width > 0:
        result = f"{formatted:>{before_width}}" + " " * (after_width + 1)
    else:
        result = f"{formatted:>{before_width}}"
    return result.rjust(total_width)


def strip_command_options(command: str, option_names: Iterable[str]) -> str:
    """Remove selected valued options and absolute positional paths from a command."""
    if not command:
        return ""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()

    options = set(option_names)
    result = []
    skip_next = False
    for part in parts:
        if skip_next:
            skip_next = False
            continue
        option = part.split("=", 1)[0]
        if option in options:
            skip_next = "=" not in part
            continue
        if not part.startswith("/"):
            result.append(part)
    return " ".join(result)


def parse_int_values_with_ranges(value: str, warn: Callable[[str], Any]) -> set[int]:
    """Parse comma-separated integers and inclusive integer ranges."""
    result: set[int] = set()
    if not value:
        return result

    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            try:
                start, end = map(int, part.split("-", 1))
                result.update(range(start, end + 1))
            except ValueError:
                warn(f"Warning: Invalid range format: {part}")
        else:
            try:
                result.add(int(part))
            except ValueError:
                warn(f"Warning: Invalid integer: {part}")
    return result


def filter_metrics_by_scale(
    metrics: list[_Metric],
    only_nodes: set[int] | None = None,
    only_threads: set[int] | None = None,
) -> list[_Metric]:
    """Filter metrics exposing ``node_count`` and ``thread_count`` attributes."""
    result = metrics
    if only_nodes:
        result = [metric for metric in result if metric.node_count in only_nodes]
    if only_threads:
        result = [metric for metric in result if metric.thread_count in only_threads]
    return result


def add_common_report_arguments(parser: argparse.ArgumentParser) -> None:
    """Add report-input, scale-filter, and Markdown arguments shared by analyzers."""
    parser.add_argument(
        "--from-csv",
        metavar="FILE",
        help="Read metrics from previously saved CSV file",
    )
    parser.add_argument(
        "--only-threads",
        metavar="THREADS",
        help="Only include these thread counts (comma-separated, supports ranges)",
    )
    parser.add_argument(
        "--only-nodes",
        metavar="NODES",
        help="Only include these node counts (comma-separated, supports ranges)",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Output report in Markdown format (report to stdout, progress to stderr)",
    )


def histogram_axis_ranges(
    series: Iterable[tuple[Iterable[float], Iterable[float]]],
) -> dict[str, float]:
    """Calculate padded latency/count bounds suitable for logarithmic histograms."""
    min_latency = float("inf")
    max_latency = 0.0
    min_count = float("inf")
    max_count = 0.0

    for x_values_iter, y_values_iter in series:
        x_values = list(x_values_iter)
        y_values = list(y_values_iter)
        positive_latencies = [value for value in x_values if value > 0]
        if positive_latencies:
            min_latency = min(min_latency, *positive_latencies)
            max_latency = max(max_latency, *positive_latencies)
        if y_values:
            nonzero_counts = [value for value in y_values if value > 0]
            if nonzero_counts:
                min_count = min(min_count, *nonzero_counts)
            max_count = max(max_count, *y_values)

    latency_data_found = min_latency != float("inf")
    min_latency = 0.1 if not latency_data_found else min_latency
    max_latency = 1000.0 if max_latency == 0 else max_latency
    min_count = 1.0 if min_count == float("inf") else min_count
    max_count = 100.0 if max_count == 0 else max_count

    padding_factor = 0.1
    return {
        "min_latency": (
            min_latency * (1 - padding_factor) if latency_data_found else min_latency
        ),
        "max_latency": max_latency * (1 + padding_factor),
        "min_count": max(1.0, min_count * (1 - padding_factor)),
        "max_count": max_count * (1 + padding_factor),
    }
