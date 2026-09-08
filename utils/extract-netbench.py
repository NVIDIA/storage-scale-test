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

"""
Analyze elbencho netbench benchmark results.

Parses result files from nv-netbench tests, aggregates metrics across
iterations and directions, and generates performance reports and visualizations.
"""

import argparse
import csv
import os
import re
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set, Any

import matplotlib.pyplot as plt

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.join_datestamps import (  # pylint: disable=wrong-import-position
    join_datestamps as join_datestamps_lib,
    join_datestamps_for_filename,
)
from lib.reporting_common import (  # pylint: disable=wrong-import-position
    add_common_report_arguments,
    discover_result_pairs,
    filter_metrics_by_scale,
    format_decimal_aligned_latency_ms,
    format_latency_ms,
    latency_column_widths,
    parse_int_values_with_ranges as parse_int_ranges,
    parse_integer_histogram,
    strip_command_options,
)
from lib.stdout_report_file import (  # pylint: disable=wrong-import-position
    REPORT_TXT_FILENAME,
    mirror_stdout_to_file,
)


def eprint(*args, **kwargs):
    """Print to stderr (for warnings, progress messages, etc.)."""
    print(*args, file=sys.stderr, **kwargs)


# =============================================================================
# Constants
# =============================================================================

# Conversion factor: 1 MiB = 8.388608 Mbit, so MiB/s * 8.388608 / 1000 = Gbps
MIB_S_TO_GBPS = 8.388608 / 1000


# =============================================================================
# Data Structures
# =============================================================================


@dataclass
class IterationMetrics:
    """Metrics from a single iteration/direction of a netbench run."""

    # Identification
    mode: str  # "bidir" or "half"
    node_count: int
    thread_count: int
    datestamp: str
    iteration: int
    direction: str  # "AtoB" or "BtoA"

    # Throughput (from CSV "first" columns) - stored in MiB/s
    mib_s_write: float  # MiB/s [first] (write only)
    mib_s_total: float  # MiB/s [first] + rwmix read MiB/s [first]

    # Latency bounds (from CSV, in seconds)
    lat_min_sec: float  # IO lat us [min] / 1e6
    lat_max_sec: float  # IO lat us [max] / 1e6

    # Histogram (from OUT file, bucket_us -> count)
    histogram: Dict[int, int] = field(default_factory=dict)

    # Command (from CSV)
    command: str = ""


@dataclass
class AggregatedMetrics:
    """Aggregated metrics across iterations for a (mode, node_count, thread_count) tuple."""

    # Identification
    mode: str  # "bidir" or "half"
    node_count: int
    thread_count: int
    datestamps: Set[str] = field(default_factory=set)
    sample_count: int = 0  # iterations × directions combined

    # Throughput statistics (stored in MiB/s, converted to Gbps for display)
    # For half-and-half mode: use write only (unidirectional traffic)
    # For bidirectional mode: use total (full-duplex traffic)
    mib_s_write_avg: float = 0.0
    mib_s_write_stddev: float = 0.0
    mib_s_total_avg: float = 0.0
    mib_s_total_stddev: float = 0.0

    # Latency (from combined histogram + min/max bounds)
    lat_p0_ms: float = 0.0  # Minimum min_lat across all samples (ms)
    lat_p50_ms: float = 0.0  # From combined histogram
    lat_p90_ms: float = 0.0  # From combined histogram
    lat_p99_ms: float = 0.0  # From combined histogram
    lat_p100_ms: float = 0.0  # Maximum max_lat across all samples (ms)

    # Combined histogram (sum of all iteration histograms)
    histogram: Dict[int, int] = field(default_factory=dict)

    # Representative command
    command: str = ""


# =============================================================================
# Unit Conversion
# =============================================================================


def mib_s_to_gbps(mib_s: float) -> float:
    """Convert MiB/s to Gbps (1 MiB = 8.388608 Mbit)."""
    return mib_s * MIB_S_TO_GBPS


def format_throughput(gbps: float) -> Tuple[str, str]:
    """
    Format throughput in Gbps or Tbps.

    Returns:
        Tuple of (formatted_value, unit_label)
    """
    if gbps >= 1000:
        return f"{gbps / 1000:.2f}", "Tbps"
    return f"{gbps:.2f}", "Gbps"


# =============================================================================
# File Discovery and Parsing
# =============================================================================

# Regex pattern for netbench result files
# netbench-{mode}-c_{nodes}-t_{threads}_{datestamp}_iter{N}_{direction}.{csv|out}
FILENAME_PATTERN = re.compile(
    r"netbench-(bidir|half)-c_(\d+)-t_(\d+)_(\d{8}Z\d{6})_iter(\d+)_(AtoB|BtoA)\.(csv|out)"
)


def discover_files(
    input_dirs: List[str],
) -> Dict[Tuple[str, int, int, str, int, str], Tuple[str, str]]:
    """
    Scan directories for matching file pairs.

    Args:
        input_dirs: List of directories to scan

    Returns:
        Dict mapping (mode, node_count, thread_count, datestamp, iteration, direction)
        to (csv_path, out_path)
    """

    def key_from_match(match: re.Match[str]) -> Tuple[str, int, int, str, int, str]:
        return (
            match.group(1),
            int(match.group(2)),
            int(match.group(3)),
            match.group(4),
            int(match.group(5)),
            match.group(6),
        )

    return discover_result_pairs(
        input_dirs, FILENAME_PATTERN, key_from_match, extension_group=7, warn=eprint
    )


def parse_csv_file(csv_path: str) -> Dict[str, Any]:
    """
    Parse CSV file and return metrics.

    Args:
        csv_path: Path to the CSV file

    Returns:
        Dict with 'mib_s_write', 'mib_s_read', 'lat_min_us', 'lat_max_us', 'command'
    """
    result = {
        "mib_s_write": 0.0,
        "mib_s_read": 0.0,
        "lat_min_us": 0.0,
        "lat_max_us": 0.0,
        "command": "",
    }

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # We only want NET operation rows
            op = row.get("operation", "").upper()
            if op != "NET":
                continue

            # Extract write throughput (MiB/s [first])
            mib_s_str = row.get("MiB/s [first]", "")
            if mib_s_str:
                try:
                    result["mib_s_write"] = float(mib_s_str)
                except ValueError:
                    pass

            # Extract read throughput (rwmix read MiB/s [first])
            mib_s_read_str = row.get("rwmix read MiB/s [first]", "")
            if mib_s_read_str:
                try:
                    result["mib_s_read"] = float(mib_s_read_str)
                except ValueError:
                    pass

            # Extract latency bounds (IO lat us [min/max])
            lat_min_str = row.get("IO lat us [min]", "")
            lat_max_str = row.get("IO lat us [max]", "")

            if lat_min_str:
                try:
                    result["lat_min_us"] = float(lat_min_str)
                except ValueError:
                    pass
            if lat_max_str:
                try:
                    result["lat_max_us"] = float(lat_max_str)
                except ValueError:
                    pass

            # Extract command
            result["command"] = row.get("command", "")

    return result


def parse_out_file(out_path: str) -> Dict[int, int]:
    """
    Parse OUT file for histogram.

    Args:
        out_path: Path to the .out file

    Returns:
        Histogram as {bucket_us: count}
    """
    with open(out_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Pattern to find histogram: "IO wr lat hist   : [ bucket: count, ... ]"
    hist_pattern = re.compile(r"IO wr lat hist\s*:\s*\[\s*([^\]]+)\s*\]")

    hist_match = hist_pattern.search(content)
    if not hist_match:
        return {}
    return parse_integer_histogram(hist_match.group(1))


def parse_file_pair(
    csv_path: str,
    out_path: str,
    mode: str,
    node_count: int,
    thread_count: int,
    datestamp: str,
    iteration: int,
    direction: str,
) -> Optional[IterationMetrics]:
    """
    Parse a CSV/OUT file pair and return IterationMetrics.

    Args:
        csv_path: Path to CSV file
        out_path: Path to OUT file
        mode: "bidir" or "half"
        node_count: Node count from filename
        thread_count: Thread count from filename
        datestamp: Datestamp from filename
        iteration: Iteration number from filename
        direction: "AtoB" or "BtoA"

    Returns:
        IterationMetrics or None if parsing failed
    """
    try:
        csv_data = parse_csv_file(csv_path)
        histogram = parse_out_file(out_path)
    except Exception as e:  # pylint: disable=broad-exception-caught
        eprint(f"Error parsing {csv_path} / {out_path}: {e}")
        return None

    # Throughput values
    mib_s_write = csv_data["mib_s_write"]
    mib_s_total = mib_s_write + csv_data["mib_s_read"]

    return IterationMetrics(
        mode=mode,
        node_count=node_count,
        thread_count=thread_count,
        datestamp=datestamp,
        iteration=iteration,
        direction=direction,
        mib_s_write=mib_s_write,
        mib_s_total=mib_s_total,
        lat_min_sec=csv_data["lat_min_us"] / 1_000_000,
        lat_max_sec=csv_data["lat_max_us"] / 1_000_000,
        histogram=histogram,
        command=csv_data["command"],
    )


# =============================================================================
# Aggregation
# =============================================================================


def percentile_from_histogram(histogram: Dict[int, int], percentile: float) -> float:
    """
    Calculate percentile from histogram using linear interpolation.

    Args:
        histogram: {bucket_us: count}
        percentile: 0.0 to 100.0

    Returns:
        Latency value in microseconds at the given percentile
    """
    if not histogram:
        return 0.0

    # Sort buckets
    sorted_buckets = sorted(histogram.keys())
    total_count = sum(histogram.values())

    if total_count == 0:
        return 0.0

    target_count = total_count * percentile / 100.0
    cumulative = 0

    for i, bucket in enumerate(sorted_buckets):
        count = histogram[bucket]
        if cumulative + count >= target_count:
            # Target percentile falls in this bucket
            # Linear interpolation within bucket
            prev_bucket = sorted_buckets[i - 1] if i > 0 else 0
            fraction = (target_count - cumulative) / count if count > 0 else 0
            return prev_bucket + (bucket - prev_bucket) * fraction
        cumulative += count

    # Return the last bucket if we didn't find the percentile
    return sorted_buckets[-1] if sorted_buckets else 0.0


def combine_histograms(histograms: List[Dict[int, int]]) -> Dict[int, int]:
    """
    Combine multiple histograms by summing counts.

    Args:
        histograms: List of {bucket_us: count} dicts

    Returns:
        Combined histogram
    """
    result: Dict[int, int] = {}
    for hist in histograms:
        for bucket, count in hist.items():
            result[bucket] = result.get(bucket, 0) + count
    return result


def aggregate_metrics(
    iterations: List[IterationMetrics],
) -> Dict[Tuple[str, int, int], AggregatedMetrics]:
    """
    Aggregate iterations by (mode, node_count, thread_count).

    Args:
        iterations: List of IterationMetrics

    Returns:
        Dict mapping (mode, node_count, thread_count) to AggregatedMetrics
    """
    # Group by (mode, node_count, thread_count)
    groups: Dict[Tuple[str, int, int], List[IterationMetrics]] = {}
    for m in iterations:
        key = (m.mode, m.node_count, m.thread_count)
        if key not in groups:
            groups[key] = []
        groups[key].append(m)

    result = {}
    for key, group in groups.items():
        mode, node_count, thread_count = key

        # Collect throughput values
        write_values = [m.mib_s_write for m in group]
        total_values = [m.mib_s_total for m in group]

        # Collect datestamps
        datestamps = {m.datestamp for m in group}

        # Combine histograms
        combined_hist = combine_histograms([m.histogram for m in group])

        # Calculate throughput statistics
        mib_s_write_avg = statistics.mean(write_values) if write_values else 0.0
        mib_s_write_stddev = (
            statistics.stdev(write_values) if len(write_values) > 1 else 0.0
        )
        mib_s_total_avg = statistics.mean(total_values) if total_values else 0.0
        mib_s_total_stddev = (
            statistics.stdev(total_values) if len(total_values) > 1 else 0.0
        )

        # Get p0 and p100 from min/max latencies
        lat_p0_ms = min(m.lat_min_sec for m in group) * 1000 if group else 0.0
        lat_p100_ms = max(m.lat_max_sec for m in group) * 1000 if group else 0.0

        # Compute percentiles from combined histogram (convert us to ms)
        lat_p50_ms = percentile_from_histogram(combined_hist, 50) / 1000
        lat_p90_ms = percentile_from_histogram(combined_hist, 90) / 1000
        lat_p99_ms = percentile_from_histogram(combined_hist, 99) / 1000

        # Get representative command
        command = group[0].command if group else ""

        agg = AggregatedMetrics(
            mode=mode,
            node_count=node_count,
            thread_count=thread_count,
            datestamps=datestamps,
            sample_count=len(group),
            mib_s_write_avg=mib_s_write_avg,
            mib_s_write_stddev=mib_s_write_stddev,
            mib_s_total_avg=mib_s_total_avg,
            mib_s_total_stddev=mib_s_total_stddev,
            lat_p0_ms=lat_p0_ms,
            lat_p50_ms=lat_p50_ms,
            lat_p90_ms=lat_p90_ms,
            lat_p99_ms=lat_p99_ms,
            lat_p100_ms=lat_p100_ms,
            histogram=combined_hist,
            command=command,
        )
        result[key] = agg

    return result


# =============================================================================
# Output Formatting
# =============================================================================


def format_latency(lat_ms: float) -> str:
    """Format latency in milliseconds with appropriate precision."""
    return format_latency_ms(lat_ms)


def compute_latency_column_widths(values: List[float]) -> Tuple[int, int]:
    """
    Compute max widths before and after decimal for latency column.

    Returns:
        Tuple of (max_before_decimal, max_after_decimal)
    """
    return latency_column_widths(values)


def format_decimal_aligned_latency(
    lat_ms: float, before_w: int, after_w: int, total_w: int
) -> str:
    """Format latency with decimal alignment, right-padded to total_w."""
    return format_decimal_aligned_latency_ms(lat_ms, before_w, after_w, total_w)


def get_throughput_for_mode(m: AggregatedMetrics) -> Tuple[float, float]:
    """
    Get the appropriate throughput values based on mode.

    For half-and-half mode: use write only (unidirectional traffic)
    For bidirectional mode: use total (full-duplex traffic)

    Returns:
        Tuple of (avg_mib_s, stddev_mib_s)
    """
    if m.mode == "half":
        return m.mib_s_write_avg, m.mib_s_write_stddev
    else:
        return m.mib_s_total_avg, m.mib_s_total_stddev


def compute_throughput_column_widths(
    metrics: List[AggregatedMetrics],
) -> Tuple[int, int, str]:
    """
    Compute max widths for throughput avg and std columns.

    Also determines whether to use Gbps or Tbps based on max value.

    Returns:
        Tuple of (max_avg_width, max_std_width, unit_label)
    """
    # Check if any value exceeds 1000 Gbps
    max_gbps = 0
    if metrics:
        max_gbps = max(mib_s_to_gbps(get_throughput_for_mode(m)[0]) for m in metrics)
    use_tbps = max_gbps >= 1000

    max_avg_width = 0
    max_std_width = 0

    for m in metrics:
        avg_mib_s, std_mib_s = get_throughput_for_mode(m)
        avg_gbps = mib_s_to_gbps(avg_mib_s)
        std_gbps = mib_s_to_gbps(std_mib_s)

        if use_tbps:
            avg_str = f"{avg_gbps / 1000:.2f}"
            std_str = f"{std_gbps / 1000:.2f}"
        else:
            avg_str = f"{avg_gbps:.1f}"
            std_str = f"{std_gbps:.1f}"

        max_avg_width = max(max_avg_width, len(avg_str))
        max_std_width = max(max_std_width, len(std_str))

    unit_label = "Tbps" if use_tbps else "Gbps"
    return max_avg_width, max_std_width, unit_label


def format_aligned_throughput_with_std(
    mib_s_avg: float,
    mib_s_std: float,
    avg_width: int,
    std_width: int,
    use_tbps: bool,
) -> str:
    """Format throughput with standard deviation, aligned to specified widths."""
    avg_gbps = mib_s_to_gbps(mib_s_avg)
    std_gbps = mib_s_to_gbps(mib_s_std)

    if use_tbps:
        avg_str = f"{avg_gbps / 1000:.2f}"
        std_str = f"{std_gbps / 1000:.2f}"
    else:
        avg_str = f"{avg_gbps:.1f}"
        std_str = f"{std_gbps:.1f}"

    return f"{avg_str:>{avg_width}} ± {std_str:>{std_width}}"


def strip_command(cmd: str) -> str:
    """Strip hosts, resfile, csvfile, and positional path args from command."""
    return strip_command_options(
        cmd, {"--clients", "--servers", "--resfile", "--csvfile"}
    )


def join_datestamps(datestamps: Set[str], max_len: Optional[int] = None) -> str:
    """Join datestamps with + separator (full join unless max_len is set)."""
    return join_datestamps_lib(datestamps, sep="+", max_len=max_len)


def get_mode_display_name(mode: str) -> str:
    """Get display name for mode."""
    if mode == "bidir":
        return "Bidirectional"
    if mode == "half":
        return "Half-and-Half"
    return mode


def print_terminal_table(metrics: List[AggregatedMetrics]) -> None:
    """Print formatted metrics table to terminal."""
    if not metrics:
        print("No metrics to display")
        return

    # Collect all datestamps
    all_datestamps: Set[str] = set()
    for m in metrics:
        all_datestamps.update(m.datestamps)

    print(f"\nnetbench results from {join_datestamps(all_datestamps)}")

    # Group by mode
    modes = sorted({m.mode for m in metrics})

    for mode in modes:
        mode_metrics = [m for m in metrics if m.mode == mode]

        # Sort by (node_count, thread_count)
        sorted_metrics = sorted(
            mode_metrics, key=lambda m: (m.node_count, m.thread_count)
        )

        print(f"\n=== {get_mode_display_name(mode)} Mode ===\n")

        # Compute column widths
        nodes_w = max(5, max(len(str(m.node_count)) for m in sorted_metrics))
        threads_w = max(7, max(len(str(m.thread_count)) for m in sorted_metrics))
        samples_w = max(7, max(len(str(m.sample_count)) for m in sorted_metrics))

        # Throughput column
        tp_avg_w, tp_std_w, tp_unit = compute_throughput_column_widths(sorted_metrics)
        use_tbps = tp_unit == "Tbps"
        tp_col_w = tp_avg_w + 3 + tp_std_w  # avg + " ± " + std
        tp_label = "Total" if mode == "bidir" else "Write"
        tp_header = f"{tp_label} {tp_unit} (avg±std)"
        tp_col_w = max(tp_col_w, len(tp_header))

        # Latency columns
        p0_vals = [m.lat_p0_ms for m in sorted_metrics]
        p50_vals = [m.lat_p50_ms for m in sorted_metrics]
        p90_vals = [m.lat_p90_ms for m in sorted_metrics]
        p99_vals = [m.lat_p99_ms for m in sorted_metrics]
        p100_vals = [m.lat_p100_ms for m in sorted_metrics]

        p0_b, p0_a = compute_latency_column_widths(p0_vals)
        p50_b, p50_a = compute_latency_column_widths(p50_vals)
        p90_b, p90_a = compute_latency_column_widths(p90_vals)
        p99_b, p99_a = compute_latency_column_widths(p99_vals)
        p100_b, p100_a = compute_latency_column_widths(p100_vals)

        p0_w = p0_b + (1 + p0_a if p0_a > 0 else 0)
        p50_w = p50_b + (1 + p50_a if p50_a > 0 else 0)
        p90_w = p90_b + (1 + p90_a if p90_a > 0 else 0)
        p99_w = p99_b + (1 + p99_a if p99_a > 0 else 0)
        p100_w = p100_b + (1 + p100_a if p100_a > 0 else 0)

        # Ensure headers fit (headers include " (ms)" suffix)
        p0_w = max(p0_w, 7)  # "p0 (ms)"
        p50_w = max(p50_w, 8)  # "p50 (ms)"
        p90_w = max(p90_w, 8)  # "p90 (ms)"
        p99_w = max(p99_w, 8)  # "p99 (ms)"
        p100_w = max(p100_w, 9)  # "p100 (ms)"

        # Print header
        header_parts = [
            "Nodes".rjust(nodes_w),
            "Threads".rjust(threads_w),
            "Samples".rjust(samples_w),
            tp_header.rjust(tp_col_w),
            "p0 (ms)".rjust(p0_w),
            "p50 (ms)".rjust(p50_w),
            "p90 (ms)".rjust(p90_w),
            "p99 (ms)".rjust(p99_w),
            "p100 (ms)".rjust(p100_w),
        ]
        print("  ".join(header_parts))
        total_width = sum(len(p) for p in header_parts) + 2 * (len(header_parts) - 1)
        print("-" * total_width)

        # Print rows
        for m in sorted_metrics:
            avg_mib_s, std_mib_s = get_throughput_for_mode(m)
            tp_cell = format_aligned_throughput_with_std(
                avg_mib_s, std_mib_s, tp_avg_w, tp_std_w, use_tbps
            )

            row_parts = [
                str(m.node_count).rjust(nodes_w),
                str(m.thread_count).rjust(threads_w),
                str(m.sample_count).rjust(samples_w),
                tp_cell.rjust(tp_col_w),
                format_decimal_aligned_latency(m.lat_p0_ms, p0_b, p0_a, p0_w),
                format_decimal_aligned_latency(m.lat_p50_ms, p50_b, p50_a, p50_w),
                format_decimal_aligned_latency(m.lat_p90_ms, p90_b, p90_a, p90_w),
                format_decimal_aligned_latency(m.lat_p99_ms, p99_b, p99_a, p99_w),
                format_decimal_aligned_latency(m.lat_p100_ms, p100_b, p100_a, p100_w),
            ]
            print("  ".join(row_parts))

    # Print representative command
    if metrics:
        print("\n=== Representative Command ===\n")
        print(f"# {strip_command(metrics[0].command)}")

    print()


def print_markdown_report(metrics: List[AggregatedMetrics]) -> None:
    """Print formatted markdown report."""
    if not metrics:
        print("No metrics to display")
        return

    # Collect all datestamps
    all_datestamps: Set[str] = set()
    for m in metrics:
        all_datestamps.update(m.datestamps)

    datestamp_str = join_datestamps(all_datestamps)

    # Group by mode
    modes = sorted({m.mode for m in metrics})

    # Determine if single-node or multi-node (across all modes)
    unique_nodes = {m.node_count for m in metrics}
    is_multi_node = len(unique_nodes) > 1
    sn_mn = "mn" if is_multi_node else "sn"

    # Get unique thread counts
    unique_threads = sorted({m.thread_count for m in metrics})

    # =========================================================================
    # Section 1: Summary
    # =========================================================================
    print("# Netbench Benchmark Results\n")
    print("## 1. Summary\n")
    print("| Property | Value |")
    print("|:---------|:------|")
    print(f"| **Datestamps** | {datestamp_str} |")
    print(f"| **Modes** | {', '.join(get_mode_display_name(m) for m in modes)} |")
    print(f"| **Node Counts** | {', '.join(str(n) for n in sorted(unique_nodes))} |")
    print(f"| **Thread Counts** | {', '.join(str(t) for t in unique_threads)} |")
    print()

    # Peak performance per mode
    for mode in modes:
        mode_metrics = [m for m in metrics if m.mode == mode]
        if mode_metrics:
            peak_m = max(mode_metrics, key=lambda x: get_throughput_for_mode(x)[0])
            peak_gbps = mib_s_to_gbps(get_throughput_for_mode(peak_m)[0])
            val_str, unit = format_throughput(peak_gbps)
            tp_label = "Total" if mode == "bidir" else "Write"
            print(f"**Peak Performance ({get_mode_display_name(mode)}):**")
            print(
                f"- **{tp_label}:** {val_str} {unit} @ "
                f"{peak_m.node_count}n/{peak_m.thread_count}t"
            )
            print()

    # =========================================================================
    # Section 2: Test Configuration
    # =========================================================================
    print("---\n")
    print("## 2. Test Configuration\n")
    if metrics:
        print("**Representative Command:**\n")
        print("```bash")
        print(strip_command(metrics[0].command))
        print("```")
        print("\n*(Hosts, resfile, csvfile omitted for brevity)*")

    # =========================================================================
    # Sections 3+: Per-mode results
    # =========================================================================
    section_num = 3
    for mode in modes:
        mode_metrics = [m for m in metrics if m.mode == mode]
        sorted_metrics = sorted(
            mode_metrics, key=lambda m: (m.node_count, m.thread_count)
        )

        print("\n---\n")
        print(f"## {section_num}. {get_mode_display_name(mode)} Results\n")

        # Determine throughput unit and label
        max_gbps = (
            max(mib_s_to_gbps(get_throughput_for_mode(m)[0]) for m in sorted_metrics)
            if sorted_metrics
            else 0
        )
        use_tbps = max_gbps >= 1000
        tp_unit = "Tbps" if use_tbps else "Gbps"
        tp_label = "Total" if mode == "bidir" else "Write"

        # Table
        print(f"### {section_num}.1 Throughput and Latency\n")
        print(
            f"| Nodes | Threads | Samples | {tp_label} {tp_unit} | ±std | "
            "p0 (ms) | p50 (ms) | p90 (ms) | p99 (ms) | p100 (ms) |"
        )
        print(
            "|------:|--------:|--------:|-----------:|-----:|"
            "--------:|---------:|---------:|---------:|----------:|"
        )

        for m in sorted_metrics:
            avg_mib_s, std_mib_s = get_throughput_for_mode(m)
            avg_gbps = mib_s_to_gbps(avg_mib_s)
            std_gbps = mib_s_to_gbps(std_mib_s)
            if use_tbps:
                avg_str = f"{avg_gbps / 1000:.2f}"
                std_str = f"{std_gbps / 1000:.2f}"
            else:
                avg_str = f"{avg_gbps:.1f}"
                std_str = f"{std_gbps:.1f}"

            print(
                f"| {m.node_count} | {m.thread_count} | {m.sample_count} "
                f"| {avg_str} | {std_str} "
                f"| {format_latency(m.lat_p0_ms)} | {format_latency(m.lat_p50_ms)} "
                f"| {format_latency(m.lat_p90_ms)} | {format_latency(m.lat_p99_ms)} "
                f"| {format_latency(m.lat_p100_ms)} |"
            )

        # Plot references (must match savefig basenames / NAME_MAX abbreviation)
        print(f"\n### {section_num}.2 Charts\n")
        print("*Insert the following images here:*\n")
        for label, kind in (
            ("Throughput Chart", "throughput"),
            ("Scaling Efficiency", "efficiency"),
            ("Latency Histogram", "latency-hist"),
        ):
            prefix = f"netbench-{mode}-{sn_mn}-{kind}-"
            stamp = join_datestamps_for_filename(all_datestamps, prefix=prefix, sep="+")
            print(f"* **{label}**: `{prefix}{stamp}.png`")

        section_num += 1

    # =========================================================================
    # Appendix
    # =========================================================================
    print("\n---\n")
    print(f"## {section_num}. Appendix\n")
    print(f"### {section_num}.1 Metrics Explanation\n")
    print("| Metric | Description |")
    print("|:-------|:------------|")
    print("| **Total Gbps** | Total throughput (write + read) in gigabits per second |")
    print("| **p0** | Minimum latency observed across all samples |")
    print("| **p50** | Median latency (50th percentile from combined histogram) |")
    print("| **p90** | 90th percentile latency |")
    print("| **p99** | 99th percentile latency |")
    print("| **p100** | Maximum latency observed across all samples |")
    print(
        "| **±std** | Standard deviation of throughput across iterations/directions |"
    )

    print(f"\n### {section_num}.2 Ideal Performance\n")
    print(
        "For bidirectional mode, the theoretical maximum is 2× NIC wire speed "
        "(assuming the NIC supports full bidirectional throughput). For example, "
        "a 100 Gbps NIC could achieve up to 200 Gbps in total throughput."
    )

    print()


# =============================================================================
# CSV Import/Export
# =============================================================================


def encode_histogram(hist: Dict[int, int]) -> str:
    """Encode histogram as a single string for CSV storage."""
    if not hist:
        return ""
    # Sort by bucket for deterministic output
    pairs = sorted(hist.items())
    # Use semicolon as pair separator (CSV uses comma)
    # Use colon as key:value separator
    return ";".join(f"{bucket}:{count}" for bucket, count in pairs)


def decode_histogram(encoded: str) -> Dict[int, int]:
    """Decode histogram from CSV string."""
    if not encoded:
        return {}
    hist = {}
    for pair in encoded.split(";"):
        if ":" not in pair:
            continue
        bucket_str, count_str = pair.split(":", 1)
        try:
            hist[int(bucket_str)] = int(count_str)
        except ValueError:
            continue
    return hist


def write_csv_export(csv_path: str, metrics: List[AggregatedMetrics]) -> None:
    """Write aggregated metrics to CSV file."""
    if not metrics:
        return

    fieldnames = [
        "mode",
        "node_count",
        "thread_count",
        "sample_count",
        "datestamps",
        "mib_s_write_avg",
        "mib_s_write_stddev",
        "mib_s_total_avg",
        "mib_s_total_stddev",
        "lat_p0_ms",
        "lat_p50_ms",
        "lat_p90_ms",
        "lat_p99_ms",
        "lat_p100_ms",
        "histogram",
        "command",
    ]

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, quoting=csv.QUOTE_ALL)
        writer.writeheader()

        for m in metrics:
            row = {
                "mode": m.mode,
                "node_count": m.node_count,
                "thread_count": m.thread_count,
                "sample_count": m.sample_count,
                "datestamps": "|".join(sorted(m.datestamps)),
                "mib_s_write_avg": m.mib_s_write_avg,
                "mib_s_write_stddev": m.mib_s_write_stddev,
                "mib_s_total_avg": m.mib_s_total_avg,
                "mib_s_total_stddev": m.mib_s_total_stddev,
                "lat_p0_ms": m.lat_p0_ms,
                "lat_p50_ms": m.lat_p50_ms,
                "lat_p90_ms": m.lat_p90_ms,
                "lat_p99_ms": m.lat_p99_ms,
                "lat_p100_ms": m.lat_p100_ms,
                "histogram": encode_histogram(m.histogram),
                "command": m.command,
            }
            writer.writerow(row)


def read_csv_import(csv_path: str) -> List[AggregatedMetrics]:
    """Read aggregated metrics from CSV file."""
    metrics = []

    with open(csv_path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f, quoting=csv.QUOTE_ALL)
        for row in reader:
            # Handle backward compatibility: if write values missing, use total
            mib_s_write_avg = float(row.get("mib_s_write_avg", row["mib_s_total_avg"]))
            mib_s_write_stddev = float(
                row.get("mib_s_write_stddev", row["mib_s_total_stddev"])
            )
            m = AggregatedMetrics(
                mode=row["mode"],
                node_count=int(row["node_count"]),
                thread_count=int(row["thread_count"]),
                sample_count=int(row["sample_count"]),
                datestamps={*row["datestamps"].split("|")},
                mib_s_write_avg=mib_s_write_avg,
                mib_s_write_stddev=mib_s_write_stddev,
                mib_s_total_avg=float(row["mib_s_total_avg"]),
                mib_s_total_stddev=float(row["mib_s_total_stddev"]),
                lat_p0_ms=float(row["lat_p0_ms"]),
                lat_p50_ms=float(row["lat_p50_ms"]),
                lat_p90_ms=float(row["lat_p90_ms"]),
                lat_p99_ms=float(row["lat_p99_ms"]),
                lat_p100_ms=float(row["lat_p100_ms"]),
                histogram=decode_histogram(row["histogram"]),
                command=row.get("command", ""),
            )
            metrics.append(m)

    return metrics


# =============================================================================
# Plotting
# =============================================================================

# Colorblind-safe colors
COLORBLIND_COLORS = [
    "#0077BB",  # Blue
    "#EE7733",  # Orange
    "#009988",  # Teal
    "#CC3311",  # Red
    "#33BBEE",  # Cyan
    "#EE3377",  # Magenta
    "#BBBBBB",  # Grey
]


def plot_throughput(
    metrics: List[AggregatedMetrics],
    output_dir: str,
    datestamps: Set[str],
    mode: str,
    is_multi_node: bool,
) -> None:
    """Generate throughput plot with error bars for a specific mode."""
    sn_mn = "mn" if is_multi_node else "sn"
    tp_label = "Total" if mode == "bidir" else "Write"
    datestamp_str = join_datestamps(datestamps)

    plt.figure(figsize=(12, 8))
    ax = plt.gca()

    # Sort metrics by (node_count, thread_count)
    sorted_metrics = sorted(metrics, key=lambda m: (m.node_count, m.thread_count))

    # Determine units
    max_gbps = (
        max(mib_s_to_gbps(get_throughput_for_mode(m)[0]) for m in sorted_metrics)
        if sorted_metrics
        else 0
    )
    use_tbps = max_gbps >= 1000

    if is_multi_node:
        # X-axis = node count, one line per thread count
        node_counts = sorted({m.node_count for m in metrics})
        thread_counts = sorted({m.thread_count for m in metrics})

        for i, thread_count in enumerate(thread_counts):
            thread_metrics = sorted(
                [m for m in metrics if m.thread_count == thread_count],
                key=lambda m: m.node_count,
            )

            x_vals = [node_counts.index(m.node_count) for m in thread_metrics]
            y_vals = []
            y_errs = []

            for m in thread_metrics:
                avg_mib_s, std_mib_s = get_throughput_for_mode(m)
                avg = mib_s_to_gbps(avg_mib_s)
                std = mib_s_to_gbps(std_mib_s)
                if use_tbps:
                    avg /= 1000
                    std /= 1000
                y_vals.append(avg)
                y_errs.append(std)

            color = COLORBLIND_COLORS[i % len(COLORBLIND_COLORS)]
            ax.errorbar(
                x_vals,
                y_vals,
                yerr=y_errs,
                fmt="-o",
                color=color,
                label=f"{thread_count}t",
                capsize=4,
                markersize=8,
            )

        ax.set_xlabel("Node Count")
        ax.set_xticks(range(len(node_counts)))
        ax.set_xticklabels([str(n) for n in node_counts])
        ax.legend(title="Threads", loc="best")
    else:
        # X-axis = thread count
        thread_counts = sorted({m.thread_count for m in metrics})

        x_vals = []
        y_vals = []
        y_errs = []

        for i, tc in enumerate(thread_counts):
            m = next((m for m in metrics if m.thread_count == tc), None)
            if m:
                avg_mib_s, std_mib_s = get_throughput_for_mode(m)
                avg = mib_s_to_gbps(avg_mib_s)
                std = mib_s_to_gbps(std_mib_s)
                if use_tbps:
                    avg /= 1000
                    std /= 1000
                x_vals.append(i)
                y_vals.append(avg)
                y_errs.append(std)

        ax.errorbar(
            x_vals,
            y_vals,
            yerr=y_errs,
            fmt="-o",
            color=COLORBLIND_COLORS[0],
            capsize=4,
            markersize=8,
        )

        ax.set_xlabel("Thread Count")
        ax.set_xticks(range(len(thread_counts)))
        ax.set_xticklabels([str(t) for t in thread_counts])

    unit_label = "Tbps" if use_tbps else "Gbps"
    ax.set_ylabel(f"{tp_label} Throughput ({unit_label})")
    ax.set_title(f"Netbench {get_mode_display_name(mode)} Throughput - {datestamp_str}")
    ax.set_ylim(bottom=0)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    prefix = f"netbench-{mode}-{sn_mn}-throughput-"
    stamp = join_datestamps_for_filename(datestamps, prefix=prefix, sep="+")
    plt.savefig(
        os.path.join(output_dir, f"{prefix}{stamp}.png"),
        bbox_inches="tight",
        dpi=150,
    )
    plt.close()


def plot_efficiency(
    metrics: List[AggregatedMetrics],
    output_dir: str,
    datestamps: Set[str],
    mode: str,
    is_multi_node: bool,
) -> None:
    """Generate scaling efficiency plot for a specific mode."""
    sn_mn = "mn" if is_multi_node else "sn"
    datestamp_str = join_datestamps(datestamps)

    plt.figure(figsize=(12, 8))
    ax = plt.gca()

    if is_multi_node:
        # X-axis = node count, one line per thread count
        # Each thread count has its own baseline (max per-unit throughput for that thread)
        node_counts = sorted({m.node_count for m in metrics})
        thread_counts = sorted({m.thread_count for m in metrics})

        # Compute per-unit throughput for all configs
        per_unit = {}
        for m in metrics:
            gbps = mib_s_to_gbps(get_throughput_for_mode(m)[0])
            per_unit[(m.node_count, m.thread_count)] = gbps / m.node_count

        for i, thread_count in enumerate(thread_counts):
            thread_metrics = sorted(
                [m for m in metrics if m.thread_count == thread_count],
                key=lambda m: m.node_count,
            )

            # Baseline for this thread count is the max per-unit throughput
            thread_per_units = [
                per_unit.get((m.node_count, thread_count), 0) for m in thread_metrics
            ]
            baseline = max(thread_per_units) if thread_per_units else 1.0

            x_vals = [node_counts.index(m.node_count) for m in thread_metrics]
            y_vals = []

            for m in thread_metrics:
                pu = per_unit.get((m.node_count, m.thread_count), 0)
                efficiency = (pu / baseline) * 100 if baseline > 0 else 0
                y_vals.append(efficiency)

            color = COLORBLIND_COLORS[i % len(COLORBLIND_COLORS)]
            ax.plot(
                x_vals,
                y_vals,
                "-o",
                color=color,
                label=f"{thread_count}t",
                markersize=8,
            )

        ax.set_xlabel("Node Count")
        ax.set_xticks(range(len(node_counts)))
        ax.set_xticklabels([str(n) for n in node_counts])
        ax.legend(title="Threads", loc="best")
    else:
        # X-axis = thread count
        # Baseline is the max per-unit throughput
        thread_counts = sorted({m.thread_count for m in metrics})

        # Compute per-unit throughput for all configs
        per_unit = {}
        for m in metrics:
            gbps = mib_s_to_gbps(get_throughput_for_mode(m)[0])
            per_unit[m.thread_count] = gbps / m.thread_count

        # Baseline is the max per-unit throughput
        baseline = max(per_unit.values()) if per_unit else 1.0

        x_vals = []
        y_vals = []

        for i, tc in enumerate(thread_counts):
            if tc in per_unit:
                pu = per_unit[tc]
                efficiency = (pu / baseline) * 100 if baseline > 0 else 0
                x_vals.append(i)
                y_vals.append(efficiency)

        ax.plot(
            x_vals,
            y_vals,
            "-o",
            color=COLORBLIND_COLORS[0],
            markersize=8,
        )

        ax.set_xlabel("Thread Count")
        ax.set_xticks(range(len(thread_counts)))
        ax.set_xticklabels([str(t) for t in thread_counts])

    # Add reference line at 100%
    ax.axhline(y=100, linestyle="--", color="gray", alpha=0.7, label="Reference (100%)")

    ax.set_ylabel("Scaling Efficiency (%)")
    ax.set_title(
        f"Netbench {get_mode_display_name(mode)} Scaling Efficiency - {datestamp_str}"
    )
    ax.set_ylim(0, max(105, ax.get_ylim()[1]))
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    prefix = f"netbench-{mode}-{sn_mn}-efficiency-"
    stamp = join_datestamps_for_filename(datestamps, prefix=prefix, sep="+")
    plt.savefig(
        os.path.join(output_dir, f"{prefix}{stamp}.png"),
        bbox_inches="tight",
        dpi=150,
    )
    plt.close()


def plot_latency_histogram(
    metrics: List[AggregatedMetrics],
    output_dir: str,
    datestamps: Set[str],
    mode: str,
    is_multi_node: bool,
) -> None:
    """Generate latency histogram plot (log-log) for a specific mode."""
    sn_mn = "mn" if is_multi_node else "sn"
    datestamp_str = join_datestamps(datestamps)

    plt.figure(figsize=(12, 8))
    ax = plt.gca()

    # Get unique thread counts for coloring
    thread_counts = sorted({m.thread_count for m in metrics})

    # Track legend handles
    legend_handles = []
    legend_labels = []

    for i, thread_count in enumerate(thread_counts):
        # Combine histograms for all metrics with this thread count
        thread_metrics = [m for m in metrics if m.thread_count == thread_count]
        combined_hist = combine_histograms([m.histogram for m in thread_metrics])

        if not combined_hist:
            continue

        # Sort and plot
        sorted_buckets = sorted(combined_hist.keys())
        x_values = [b / 1000 for b in sorted_buckets]  # Convert us to ms
        y_values = [combined_hist[b] for b in sorted_buckets]

        color = COLORBLIND_COLORS[i % len(COLORBLIND_COLORS)]
        (line,) = ax.plot(
            x_values, y_values, "-o", color=color, markersize=3, alpha=0.8
        )
        legend_handles.append(line)
        legend_labels.append(f"{thread_count}t")

        # Add percentile lines (using first metric for this thread count)
        if thread_metrics:
            p50 = thread_metrics[0].lat_p50_ms
            p99 = thread_metrics[0].lat_p99_ms
            if p50 > 0:
                ax.axvline(x=p50, color=color, linestyle="--", alpha=0.5, linewidth=1)
            if p99 > 0:
                ax.axvline(x=p99, color=color, linestyle=":", alpha=0.5, linewidth=1.5)

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Latency (ms)")
    ax.set_ylabel("Count")
    ax.set_title(
        f"Netbench {get_mode_display_name(mode)} Latency Distribution - {datestamp_str}"
    )
    ax.legend(
        legend_handles,
        legend_labels,
        title="Threads",
        loc="upper right",
        fontsize="small",
    )
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    prefix = f"netbench-{mode}-{sn_mn}-latency-hist-"
    stamp = join_datestamps_for_filename(datestamps, prefix=prefix, sep="+")
    plt.savefig(
        os.path.join(output_dir, f"{prefix}{stamp}.png"),
        bbox_inches="tight",
        dpi=150,
    )
    plt.close()


def plot_all(metrics: List[AggregatedMetrics], output_dir: str) -> None:
    """Generate all plots."""
    if not metrics:
        return

    # Collect datestamps
    all_datestamps: Set[str] = set()
    for m in metrics:
        all_datestamps.update(m.datestamps)

    # Group by mode
    modes = sorted({m.mode for m in metrics})

    # Determine if single-node or multi-node (across all modes)
    unique_nodes = {m.node_count for m in metrics}
    is_multi_node = len(unique_nodes) > 1

    eprint(f"Generating plots in {output_dir}...")

    for mode in modes:
        mode_metrics = [m for m in metrics if m.mode == mode]
        if not mode_metrics:
            continue

        plot_throughput(mode_metrics, output_dir, all_datestamps, mode, is_multi_node)
        plot_efficiency(mode_metrics, output_dir, all_datestamps, mode, is_multi_node)
        plot_latency_histogram(
            mode_metrics, output_dir, all_datestamps, mode, is_multi_node
        )

    eprint("Plots generated successfully.")


# =============================================================================
# Filtering
# =============================================================================


def parse_int_values_with_ranges(value_str: str) -> Set[int]:
    """
    Parse a comma-separated list of integers or ranges.

    Example: '1,2,5-10,15' -> {1, 2, 5, 6, 7, 8, 9, 10, 15}
    """
    return parse_int_ranges(value_str, eprint)


def filter_metrics(
    metrics: List[AggregatedMetrics],
    only_nodes: Optional[Set[int]] = None,
    only_threads: Optional[Set[int]] = None,
    only_mode: Optional[str] = None,
) -> List[AggregatedMetrics]:
    """Filter metrics based on node count, thread count, and mode."""
    result = filter_metrics_by_scale(metrics, only_nodes, only_threads)

    if only_mode:
        result = [m for m in result if m.mode == only_mode]

    return result


# =============================================================================
# Main
# =============================================================================


def main() -> None:
    """Script entry point."""
    parser = argparse.ArgumentParser(
        description="Analyze elbencho netbench benchmark results.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "input_dirs",
        nargs="*",
        help="Directories containing netbench result files",
    )
    parser.add_argument(
        "--to-csv",
        metavar="FILE",
        help="Write aggregated metrics to CSV file",
    )
    add_common_report_arguments(parser)
    parser.add_argument(
        "--only-mode",
        metavar="MODE",
        choices=["bidir", "half"],
        help="Only include this mode (bidir or half)",
    )
    parser.add_argument(
        "--test-parse",
        metavar="FILE",
        help="Test parsing a single file pair (provide path without extension)",
    )

    args = parser.parse_args()

    # Handle test-parse mode
    if args.test_parse:
        base_path = args.test_parse.replace(".csv", "").replace(".out", "")
        csv_path = base_path + ".csv"
        out_path = base_path + ".out"

        eprint(f"Testing parse of {csv_path} and {out_path}")

        csv_data = parse_csv_file(csv_path)
        eprint("\nCSV Data:")
        eprint(f"  mib_s_write: {csv_data['mib_s_write']:.0f}")
        eprint(f"  mib_s_read: {csv_data['mib_s_read']:.0f}")
        eprint(f"  lat_min_us: {csv_data['lat_min_us']:.0f}")
        eprint(f"  lat_max_us: {csv_data['lat_max_us']:.0f}")

        histogram = parse_out_file(out_path)
        eprint("\nOUT Data (histogram):")
        if histogram:
            eprint(f"  {len(histogram)} buckets, total count={sum(histogram.values())}")
        else:
            eprint("  No histogram found")
        return

    # Validate args
    if not args.input_dirs and not args.from_csv:
        parser.error("At least one input directory required (or use --from-csv)")

    # Load metrics
    aggregated_metrics: List[AggregatedMetrics] = []

    if args.from_csv:
        eprint(f"Loading metrics from {args.from_csv}")
        aggregated_metrics = read_csv_import(args.from_csv)
        eprint(f"Loaded {len(aggregated_metrics)} aggregated metrics")
    else:
        # Discover and parse files
        file_pairs = discover_files(args.input_dirs)
        eprint(f"Found {len(file_pairs)} file pairs")

        if not file_pairs:
            eprint("No valid file pairs found")
            sys.exit(1)

        # Parse all file pairs
        iterations: List[IterationMetrics] = []
        for key, (csv_path, out_path) in file_pairs.items():
            mode, node_count, thread_count, datestamp, iteration, direction = key
            m = parse_file_pair(
                csv_path,
                out_path,
                mode,
                node_count,
                thread_count,
                datestamp,
                iteration,
                direction,
            )
            if m:
                iterations.append(m)

        eprint(f"Parsed {len(iterations)} iterations")

        if not iterations:
            eprint("No valid iterations parsed")
            sys.exit(1)

        # Aggregate
        aggregated_dict = aggregate_metrics(iterations)
        aggregated_metrics = list(aggregated_dict.values())
        eprint(f"Aggregated into {len(aggregated_metrics)} (mode, node, thread) groups")

    # Apply filters
    if args.only_nodes or args.only_threads or args.only_mode:
        node_filter = (
            parse_int_values_with_ranges(args.only_nodes) if args.only_nodes else None
        )
        thread_filter = (
            parse_int_values_with_ranges(args.only_threads)
            if args.only_threads
            else None
        )
        aggregated_metrics = filter_metrics(
            aggregated_metrics, node_filter, thread_filter, args.only_mode
        )
        eprint(f"After filtering: {len(aggregated_metrics)} groups")

    if not aggregated_metrics:
        eprint("No metrics remaining after filtering")
        sys.exit(1)

    # Determine output directory
    output_dir = "."
    if args.input_dirs:
        output_dir = args.input_dirs[0]
    elif args.from_csv:
        output_dir = os.path.dirname(os.path.abspath(args.from_csv)) or "."

    # Export to CSV if requested
    if args.to_csv:
        write_csv_export(args.to_csv, aggregated_metrics)
        eprint(f"Wrote metrics to {args.to_csv}")

    # Print output
    if args.markdown:
        print_markdown_report(aggregated_metrics)
    else:
        mirror_stdout_to_file(
            os.path.join(output_dir, REPORT_TXT_FILENAME),
            print_terminal_table,
            aggregated_metrics,
        )

    # Generate plots
    plot_all(aggregated_metrics, output_dir)


if __name__ == "__main__":
    main()
