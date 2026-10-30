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
Analyze mdtest-elbencho metadata benchmark results.

Parses result files from nv-mdtest-elbencho tests, aggregates metrics across
iterations, and generates performance reports and visualizations.
"""

from concurrent.futures import Executor
import contextlib
import csv
import math
import os
import re
import shlex
import sys
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Bootstrap must precede runtime dependencies, including imports for plotting.
# pylint: disable=wrong-import-position,wrong-import-order
from lib.report_cli import extract_mdtest_elbencho_parser, prepare_report_runtime

if __name__ == "__main__":
    prepare_report_runtime(__file__, extract_mdtest_elbencho_parser)

import matplotlib.pyplot as plt  # pylint: disable=wrong-import-position
from matplotlib.axes import Axes
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter


from lib.env_used_yaml import (  # pylint: disable=wrong-import-position
    load_env_used_yaml,
)
from lib import (  # pylint: disable=wrong-import-position
    filesystem_results as results_json,
)
from lib.filesystem_batch import (  # pylint: disable=wrong-import-position
    is_batch_document,
    report_documents,
    route_batch_report,
)
from lib.filesystem_coordinates import (  # pylint: disable=wrong-import-position
    MdtestDefinition,
    is_mdtest_definition,
    mdtest_coordinates,
    optional_int,
    parse_mdtest_definition,
)
from lib.filesystem_report_options import (  # pylint: disable=wrong-import-position
    document_output_dir,
    resolve_report_options,
    unique_inputs,
    validate_analysis_arguments,
    validate_integer_filter,
    validate_report_inputs,
)
from lib.join_datestamps import (  # pylint: disable=wrong-import-position
    join_datestamps as join_datestamps_lib,
    join_datestamps_for_filename,
)
from lib.reporting_common import (  # pylint: disable=wrong-import-position
    PNG_SAVE_OPTIONS,
    filter_metrics_by_scale,
    format_decimal_aligned_latency_ms,
    latency_column_widths,
    strip_command_options,
)
from lib.results_document import (  # pylint: disable=wrong-import-position
    recorded_options,
)
from lib.storage_target import (  # pylint: disable=wrong-import-position
    MIXED_PREFIX,
    guarded_document_target,
    guarded_report_target,
    storage_target_from_env,
)
from lib.stdout_report_file import (  # pylint: disable=wrong-import-position
    REPORT_TXT_FILENAME,
    mirror_stdout_to_file,
    remove_stale_report,
)
from lib.project_version import (  # pylint: disable=wrong-import-position
    UNKNOWN as UNKNOWN_VERSION,
)
from lib.report_pool import (  # pylint: disable=wrong-import-position
    InlineExecutor,
    captured,
    replay,
    report_pool,
)
from lib.report_provenance import (  # pylint: disable=wrong-import-position
    VERSION_SUFFIX,
    recorded_result_version,
    selected_versions,
    with_versions,
)


def eprint(*args, **kwargs):
    """Print to stderr (for warnings, progress messages, etc.)."""
    print(*args, file=sys.stderr, **kwargs)


# =============================================================================
# Data Structures
# =============================================================================


@dataclass
class IterationMetrics:
    """Metrics from a single iteration of a (node_count, thread_count) tuple."""

    # Identification
    node_count: int
    thread_count: int
    datestamp: str
    iteration: int

    # Rates (operations per second) - from entries/s [first]
    create_rate: float
    stat_rate: float
    delete_rate: float

    # Phase elapsed times (seconds) - from time ms [last]
    create_elapsed_sec: float
    stat_elapsed_sec: float
    delete_elapsed_sec: float

    # Latency metrics (in seconds) - from CSV
    create_lat_min: float
    create_lat_avg: float
    create_lat_max: float
    stat_lat_min: float
    stat_lat_avg: float
    stat_lat_max: float
    delete_lat_min: float
    delete_lat_avg: float
    delete_lat_max: float

    # Histograms (bucket_us -> count) - from OUT file
    create_histogram: Dict[float, int] = field(default_factory=dict)
    stat_histogram: Dict[float, int] = field(default_factory=dict)
    delete_histogram: Dict[float, int] = field(default_factory=dict)

    # Commands per operation type
    create_command: str = ""
    stat_command: str = ""
    delete_command: str = ""


@dataclass
class AggregatedMetrics:
    """Aggregated metrics across iterations for a (node_count, thread_count) tuple."""

    # Identification
    node_count: int
    thread_count: int
    datestamps: Set[str] = field(default_factory=set)
    iteration_count: int = 0

    # Rate statistics (per operation type)
    create_rate_avg: float = 0.0
    create_rate_stddev: float = 0.0
    create_rate_min: float = 0.0
    create_rate_max: float = 0.0

    stat_rate_avg: float = 0.0
    stat_rate_stddev: float = 0.0
    stat_rate_min: float = 0.0
    stat_rate_max: float = 0.0

    delete_rate_avg: float = 0.0
    delete_rate_stddev: float = 0.0
    delete_rate_min: float = 0.0
    delete_rate_max: float = 0.0

    # Average phase elapsed times to last worker completion (seconds)
    create_elapsed_avg_sec: float = 0.0
    stat_elapsed_avg_sec: float = 0.0
    delete_elapsed_avg_sec: float = 0.0

    # Combined histograms (sum of all iteration histograms)
    create_histogram: Dict[float, int] = field(default_factory=dict)
    stat_histogram: Dict[float, int] = field(default_factory=dict)
    delete_histogram: Dict[float, int] = field(default_factory=dict)

    # Computed percentiles from combined histogram (in milliseconds)
    create_lat_p0: float = 0.0  # min
    create_lat_p50: float = 0.0  # median
    create_lat_p90: float = 0.0
    create_lat_p99: float = 0.0
    create_lat_p100: float = 0.0  # max

    stat_lat_p0: float = 0.0
    stat_lat_p50: float = 0.0
    stat_lat_p90: float = 0.0
    stat_lat_p99: float = 0.0
    stat_lat_p100: float = 0.0

    delete_lat_p0: float = 0.0
    delete_lat_p50: float = 0.0
    delete_lat_p90: float = 0.0
    delete_lat_p99: float = 0.0
    delete_lat_p100: float = 0.0

    # Representative commands per operation type
    create_command: str = ""
    stat_command: str = ""
    delete_command: str = ""


# =============================================================================
# File Discovery and Parsing
# =============================================================================

# Regex pattern for mdtest-elbencho result files
FILENAME_PATTERN = re.compile(
    r"mdtest-elbencho-c_(\d+)-t_(\d+)_(\d{8}Z\d{6})_iter(\d+)\.(csv|out)"
)
EXIT_INCOMPLETE = 3
REQUIRED_OPERATIONS = ("WRITE", "STAT", "RMFILES")
MDTEST_CONFIG_KEYS = (
    "MDTEST_BRANCH_FACTOR",
    "MDTEST_ITEMS_PER_DIR",
    "MDTEST_ITERATIONS",
)


def _collect_configuration_values(
    values_by_key: Dict[str, List[str]], source: Dict[str, Any]
) -> None:
    """Collect unique, non-empty configuration values from one source."""
    for key in MDTEST_CONFIG_KEYS:
        value = source.get(key)
        rendered_value = str(value) if value is not None else ""
        if rendered_value and rendered_value not in values_by_key[key]:
            values_by_key[key].append(rendered_value)


def _render_configuration_values(
    values_by_key: Dict[str, List[str]], source_description: str
) -> Dict[str, str]:
    """Render collected configuration values and warn about conflicts."""
    configuration = {}
    for key, values in values_by_key.items():
        if len(values) > 1:
            eprint(
                f"Warning: Multiple {key} values across {source_description}: "
                f"{', '.join(values)}"
            )
        if values:
            configuration[key] = ", ".join(values)
    return configuration


def parse_csv_file(csv_path: str) -> Dict[str, Dict[str, Any]]:
    """
    Parse CSV file and return per-operation data.

    Args:
        csv_path: Path to the CSV file

    Returns:
        Dict mapping operation (WRITE, STAT, RMFILES) to metrics dict
    """
    result = {}
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            op = row.get("operation", "").upper()
            if op not in REQUIRED_OPERATIONS:
                continue

            # Extract rate (entries/s [first])
            rate = 0.0
            rate_valid = False
            rate_str = row.get("entries/s [first]", "")
            if rate_str:
                try:
                    rate = float(rate_str)
                    rate_valid = math.isfinite(rate)
                except ValueError:
                    pass

            # Extract elapsed wall time through last worker completion
            elapsed_sec = 0.0
            elapsed_valid = False
            elapsed_ms_str = row.get("time ms [last]", "")
            if elapsed_ms_str:
                try:
                    elapsed_sec = float(elapsed_ms_str) / 1000
                    elapsed_valid = math.isfinite(elapsed_sec)
                except ValueError:
                    pass

            # Extract latency (microseconds -> seconds)
            lat_min = 0.0
            lat_avg = 0.0
            lat_max = 0.0
            lat_valid = 0

            lat_min_str = row.get("Ent lat us [min]", "")
            lat_avg_str = row.get("Ent lat us [avg]", "")
            lat_max_str = row.get("Ent lat us [max]", "")

            if lat_min_str:
                try:
                    lat_min = float(lat_min_str) / 1_000_000
                    lat_valid += 1
                except ValueError:
                    pass
            if lat_avg_str:
                try:
                    lat_avg = float(lat_avg_str) / 1_000_000
                    lat_valid += 1
                except ValueError:
                    pass
            if lat_max_str:
                try:
                    lat_max = float(lat_max_str) / 1_000_000
                    lat_valid += 1
                except ValueError:
                    pass

            # Extract command
            command = row.get("command", "")

            result[op] = {
                "rate": rate,
                "elapsed_sec": elapsed_sec,
                "lat_min": lat_min,
                "lat_avg": lat_avg,
                "lat_max": lat_max,
                "command": command,
                "rate_valid": rate_valid,
                "elapsed_valid": elapsed_valid,
                "lat_valid": lat_valid == 3,
                "complete": rate_valid and elapsed_valid and bool(command),
            }

    return result


def incomplete_result_reason(
    csv_data: Dict[str, Dict[str, Any]],
    out_data: Dict[str, Dict[float, int]],
) -> str:
    """Explain why a result pair is not yet safe to aggregate."""
    missing_csv = [op for op in REQUIRED_OPERATIONS if op not in csv_data]
    partial_csv = [
        op
        for op in REQUIRED_OPERATIONS
        if op in csv_data and not csv_data[op].get("complete", False)
    ]
    missing_histograms = [op for op in REQUIRED_OPERATIONS if not out_data.get(op)]

    reasons = []
    if missing_csv:
        reasons.append(f"missing CSV operations: {', '.join(missing_csv)}")
    if partial_csv:
        reasons.append(f"partial CSV operations: {', '.join(partial_csv)}")
    if missing_histograms:
        reasons.append(f"missing OUT histograms: {', '.join(missing_histograms)}")
    return "; ".join(reasons)


def parse_latency_histogram(histogram_text: str) -> Dict[float, int]:
    """Parse elbencho latency buckets, including sub-microsecond decimals."""
    histogram = {}
    for item in histogram_text.split(","):
        bucket_text, separator, count_text = item.partition(":")
        if not separator:
            continue
        try:
            bucket = float(bucket_text.strip())
            count = int(count_text.strip())
        except ValueError:
            continue
        if math.isfinite(bucket) and bucket >= 0 and count >= 0:
            # Repeated spellings of the same bucket represent additional
            # observations; preserve all counts after float normalization.
            histogram[bucket] = histogram.get(bucket, 0) + count
    return histogram


def parse_out_file(out_path: str) -> Dict[str, Dict[float, int]]:
    """
    Parse OUT file for histograms.

    Args:
        out_path: Path to the .out file

    Returns:
        Dict mapping operation (WRITE, STAT, RMFILES) to histogram {bucket_us: count}
    """
    result = {"WRITE": {}, "STAT": {}, "RMFILES": {}}

    with open(out_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Split into sections by "---"
    sections = content.split("---")

    # Pattern to find operation type and histogram
    op_pattern = re.compile(r"^(WRITE|STAT|RMFILES)\s+Elapsed time", re.MULTILINE)
    hist_pattern = re.compile(r"(?:Files|Dirs) lat hist\s*:\s*\[\s*([^\]]+)\s*\]")

    for section in sections:
        # Find operation type
        op_match = op_pattern.search(section)
        if not op_match:
            continue

        op = op_match.group(1)

        # Find histogram
        hist_match = hist_pattern.search(section)
        if not hist_match:
            continue

        result[op] = parse_latency_histogram(hist_match.group(1))

    return result


def metrics_from_parsed(
    csv_data: Dict[str, Dict[str, Any]],
    out_data: Dict[str, Dict[float, int]],
    node_count: int,
    thread_count: int,
    datestamp: str,
    iteration: int,
) -> IterationMetrics:
    """Build IterationMetrics from complete parsed CSV and OUT data."""
    # Map operations
    write_data = csv_data.get("WRITE", {})
    stat_data = csv_data.get("STAT", {})
    rmfiles_data = csv_data.get("RMFILES", {})

    return IterationMetrics(
        node_count=node_count,
        thread_count=thread_count,
        datestamp=datestamp,
        iteration=iteration,
        # Rates
        create_rate=write_data.get("rate", 0.0),
        stat_rate=stat_data.get("rate", 0.0),
        delete_rate=rmfiles_data.get("rate", 0.0),
        # Elapsed times
        create_elapsed_sec=write_data.get("elapsed_sec", 0.0),
        stat_elapsed_sec=stat_data.get("elapsed_sec", 0.0),
        delete_elapsed_sec=rmfiles_data.get("elapsed_sec", 0.0),
        # Latency
        create_lat_min=write_data.get("lat_min", 0.0),
        create_lat_avg=write_data.get("lat_avg", 0.0),
        create_lat_max=write_data.get("lat_max", 0.0),
        stat_lat_min=stat_data.get("lat_min", 0.0),
        stat_lat_avg=stat_data.get("lat_avg", 0.0),
        stat_lat_max=stat_data.get("lat_max", 0.0),
        delete_lat_min=rmfiles_data.get("lat_min", 0.0),
        delete_lat_avg=rmfiles_data.get("lat_avg", 0.0),
        delete_lat_max=rmfiles_data.get("lat_max", 0.0),
        # Histograms
        create_histogram=out_data.get("WRITE", {}),
        stat_histogram=out_data.get("STAT", {}),
        delete_histogram=out_data.get("RMFILES", {}),
        # Commands per operation
        create_command=write_data.get("command", ""),
        stat_command=stat_data.get("command", ""),
        delete_command=rmfiles_data.get("command", ""),
    )


# =============================================================================
# Aggregation
# =============================================================================


def percentile_from_histogram(histogram: Dict[float, int], percentile: float) -> float:
    """
    Calculate percentile from histogram.

    Args:
        histogram: {bucket_us: count}
        percentile: 0.0 to 100.0

    Returns:
        Latency value in milliseconds at the given percentile
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

    for bucket in sorted_buckets:
        cumulative += histogram[bucket]
        if cumulative >= target_count:
            # Convert microseconds to milliseconds
            return bucket / 1000.0

    # Return the last bucket if we didn't find the percentile
    return sorted_buckets[-1] / 1000.0 if sorted_buckets else 0.0


def combine_histograms(histograms: List[Dict[float, int]]) -> Dict[float, int]:
    """
    Combine multiple histograms by summing counts.

    Args:
        histograms: List of {bucket_us: count} dicts

    Returns:
        Combined histogram
    """
    result: Dict[float, int] = {}
    for hist in histograms:
        for bucket, count in hist.items():
            result[bucket] = result.get(bucket, 0) + count
    return result


def aggregate_metrics(
    iterations: List[IterationMetrics],
) -> Dict[Tuple[int, int], AggregatedMetrics]:
    """
    Aggregate iterations by (node_count, thread_count).

    Args:
        iterations: List of IterationMetrics

    Returns:
        Dict mapping (node_count, thread_count) to AggregatedMetrics
    """
    # Group by (node_count, thread_count)
    groups: Dict[Tuple[int, int], List[IterationMetrics]] = {}
    for m in iterations:
        key = (m.node_count, m.thread_count)
        if key not in groups:
            groups[key] = []
        groups[key].append(m)

    result = {}
    for key, group in groups.items():
        node_count, thread_count = key

        # Collect rates
        create_rates = [m.create_rate for m in group]
        stat_rates = [m.stat_rate for m in group]
        delete_rates = [m.delete_rate for m in group]
        create_elapsed = [m.create_elapsed_sec for m in group]
        stat_elapsed = [m.stat_elapsed_sec for m in group]
        delete_elapsed = [m.delete_elapsed_sec for m in group]

        # Collect datestamps
        datestamps = {m.datestamp for m in group}

        # Combine histograms
        create_hist = combine_histograms([m.create_histogram for m in group])
        stat_hist = combine_histograms([m.stat_histogram for m in group])
        delete_hist = combine_histograms([m.delete_histogram for m in group])

        # Calculate rate statistics
        def calc_stats(rates: List[float]) -> Tuple[float, float, float, float]:
            if not rates:
                return 0.0, 0.0, 0.0, 0.0
            avg = statistics.mean(rates)
            stddev = statistics.stdev(rates) if len(rates) > 1 else 0.0
            return avg, stddev, min(rates), max(rates)

        cr_avg, cr_std, cr_min, cr_max = calc_stats(create_rates)
        st_avg, st_std, st_min, st_max = calc_stats(stat_rates)
        dl_avg, dl_std, dl_min, dl_max = calc_stats(delete_rates)

        def calc_elapsed_avg(values: List[float]) -> float:
            present_values = [value for value in values if value > 0]
            return statistics.mean(present_values) if present_values else 0.0

        # Get representative commands
        create_cmd = group[0].create_command if group else ""
        stat_cmd = group[0].stat_command if group else ""
        delete_cmd = group[0].delete_command if group else ""

        agg = AggregatedMetrics(
            node_count=node_count,
            thread_count=thread_count,
            datestamps=datestamps,
            iteration_count=len(group),
            # Create rates
            create_rate_avg=cr_avg,
            create_rate_stddev=cr_std,
            create_rate_min=cr_min,
            create_rate_max=cr_max,
            # Stat rates
            stat_rate_avg=st_avg,
            stat_rate_stddev=st_std,
            stat_rate_min=st_min,
            stat_rate_max=st_max,
            # Delete rates
            delete_rate_avg=dl_avg,
            delete_rate_stddev=dl_std,
            delete_rate_min=dl_min,
            delete_rate_max=dl_max,
            # Elapsed times
            create_elapsed_avg_sec=calc_elapsed_avg(create_elapsed),
            stat_elapsed_avg_sec=calc_elapsed_avg(stat_elapsed),
            delete_elapsed_avg_sec=calc_elapsed_avg(delete_elapsed),
            # Histograms
            create_histogram=create_hist,
            stat_histogram=stat_hist,
            delete_histogram=delete_hist,
            # Percentiles (computed from combined histograms)
            create_lat_p0=percentile_from_histogram(create_hist, 0),
            create_lat_p50=percentile_from_histogram(create_hist, 50),
            create_lat_p90=percentile_from_histogram(create_hist, 90),
            create_lat_p99=percentile_from_histogram(create_hist, 99),
            create_lat_p100=percentile_from_histogram(create_hist, 100),
            stat_lat_p0=percentile_from_histogram(stat_hist, 0),
            stat_lat_p50=percentile_from_histogram(stat_hist, 50),
            stat_lat_p90=percentile_from_histogram(stat_hist, 90),
            stat_lat_p99=percentile_from_histogram(stat_hist, 99),
            stat_lat_p100=percentile_from_histogram(stat_hist, 100),
            delete_lat_p0=percentile_from_histogram(delete_hist, 0),
            delete_lat_p50=percentile_from_histogram(delete_hist, 50),
            delete_lat_p90=percentile_from_histogram(delete_hist, 90),
            delete_lat_p99=percentile_from_histogram(delete_hist, 99),
            delete_lat_p100=percentile_from_histogram(delete_hist, 100),
            # Commands
            create_command=create_cmd,
            stat_command=stat_cmd,
            delete_command=delete_cmd,
        )
        result[key] = agg

    return result


# =============================================================================
# Output Formatting
# =============================================================================


def format_rate_with_std(avg: float, std: float) -> str:
    """Format rate with standard deviation."""
    return f"{int(round(avg)):,} ± {int(round(std)):,}"


def format_elapsed_time(seconds: float) -> str:
    """Format a phase elapsed time with millisecond precision."""
    if seconds <= 0:
        return "n/a"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.3f}s"

    minutes, remaining_seconds = divmod(seconds, 60)
    return f"{int(minutes)}m {remaining_seconds:.3f}s"


def compute_rate_column_widths(
    metrics: List[AggregatedMetrics],
    get_avg: Callable[[AggregatedMetrics], float],
    get_std: Callable[[AggregatedMetrics], float],
) -> Tuple[int, int]:
    """Compute max widths for rate avg and std columns."""
    max_avg_width = 0
    max_std_width = 0
    for m in metrics:
        avg_str = f"{int(round(get_avg(m))):,}"
        std_str = f"{int(round(get_std(m))):,}"
        max_avg_width = max(max_avg_width, len(avg_str))
        max_std_width = max(max_std_width, len(std_str))
    return max_avg_width, max_std_width


def format_aligned_rate_with_std(
    avg: float, std: float, avg_width: int, std_width: int
) -> str:
    """Format rate with standard deviation, aligned to specified widths."""
    avg_str = f"{int(round(avg)):,}"
    std_str = f"{int(round(std)):,}"
    return f"{avg_str:>{avg_width}} ± {std_str:>{std_width}}"


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


def strip_command(cmd: str) -> str:
    """Strip --hosts, --resfile, --csvfile, and positional path args from command."""
    return strip_command_options(cmd, {"--hosts", "--resfile", "--csvfile"})


def join_datestamps(datestamps: Set[str], max_len: Optional[int] = None) -> str:
    """Join datestamps with + separator (full join unless max_len is set)."""
    return join_datestamps_lib(datestamps, sep="+", max_len=max_len)


def _datestamp_token(prefix: str, datestamps: Set[str]) -> str:
    """Datestamp token keeping ``prefix``-based plot basenames within NAME_MAX."""
    return join_datestamps_for_filename(datestamps, prefix=prefix, sep="+")


def _plot_basename(prefix: str, datestamps: Set[str]) -> str:
    """Plot basename for ``prefix`` within NAME_MAX."""
    return f"{prefix}{_datestamp_token(prefix, datestamps)}.png"


# Operation types that get one plot each.
PLOT_OPERATIONS = ("create", "stat", "delete")


def _plot_prefixes(is_multi_node: bool) -> List[str]:
    """Every plot basename prefix a run of this kind can write."""
    base = f"mdtest-elbencho-{'mn' if is_multi_node else 'sn'}-"
    if is_multi_node:
        efficiency = [f"{base}efficiency-{op}-" for op in PLOT_OPERATIONS]
    else:
        efficiency = [f"{base}efficiency-"]
    histograms = [f"{base}latency-hist-{op}-" for op in PLOT_OPERATIONS]
    return [f"{base}rates-"] + efficiency + histograms


def _title_datestamp_token(datestamps: Set[str], is_multi_node: bool) -> str:
    """One datestamp token for every chart title in a run.

    Budgeting each title against its own plot's prefix makes charts in the same
    run disagree, because the token only abbreviates once that plot's basename
    would overflow NAME_MAX. Budget against the longest prefix the run can write
    instead: every title then reads the same, and the token stays short enough
    to use in any basename this script writes.
    """
    return _datestamp_token(max(_plot_prefixes(is_multi_node), key=len), datestamps)


def _normalize_value(
    value: float, node_count: int, normalize_to: Optional[int]
) -> float:
    """Scale a per-cluster value to a target node count for display.

    Returns ``value * normalize_to / node_count``. When ``normalize_to`` is
    ``None`` (or ``node_count`` is non-positive), returns ``value`` unchanged.
    """
    if normalize_to is None or node_count <= 0:
        return value
    return value * normalize_to / node_count


def _format_nodes_col(node_count: int, normalize_to: Optional[int]) -> str:
    """Format the ``Nodes`` column.

    With ``normalize_to`` set, returns ``"<normalize_to> (<node_count>)"`` so
    the reader can see both the normalized basis and the actual run size.
    Without normalization, returns the bare node count.
    """
    if normalize_to is None:
        return str(node_count)
    return f"{normalize_to} ({node_count})"


def _normalization_note(normalize_to: Optional[int]) -> str:
    """Return a short note string describing rate normalization, or ''."""
    if normalize_to is None:
        return ""
    suffix = "" if normalize_to == 1 else "s"
    return f", normalized to {normalize_to} node{suffix}"


def _make_norm_getter(
    attr_name: str, normalize_to: Optional[int]
) -> Callable[[AggregatedMetrics], float]:
    """Return a getter that yields the normalized ``attr_name`` for a metric."""

    def get(m: AggregatedMetrics) -> float:
        return _normalize_value(getattr(m, attr_name), m.node_count, normalize_to)

    return get


def _print_terminal_elapsed_table(
    metrics: List[AggregatedMetrics], nodes_w: int, threads_w: int, iters_w: int
) -> None:
    """Print average elapsed wall time for each measured metadata phase."""
    elapsed_headers = ["Create avg", "Stat avg", "Delete avg"]
    elapsed_rows = [
        [
            format_elapsed_time(m.create_elapsed_avg_sec),
            format_elapsed_time(m.stat_elapsed_avg_sec),
            format_elapsed_time(m.delete_elapsed_avg_sec),
        ]
        for m in metrics
    ]
    elapsed_widths = [
        max(len(header), *(len(row[index]) for row in elapsed_rows))
        for index, header in enumerate(elapsed_headers)
    ]

    print("\n=== Average Phase Elapsed Times ===\n")
    print(
        "Mean measured-operation wall time to last worker completion across iterations.\n"
    )
    header_parts = [
        "Nodes".rjust(nodes_w),
        "Threads".rjust(threads_w),
        "Iters".rjust(iters_w),
        *(
            header.rjust(width)
            for header, width in zip(elapsed_headers, elapsed_widths)
        ),
    ]
    print("  ".join(header_parts))
    total_width = sum(len(part) for part in header_parts) + 2 * (len(header_parts) - 1)
    print("-" * total_width)

    for metric, elapsed_values in zip(metrics, elapsed_rows):
        row_parts = [
            str(metric.node_count).rjust(nodes_w),
            str(metric.thread_count).rjust(threads_w),
            str(metric.iteration_count).rjust(iters_w),
            *(
                value.rjust(width)
                for value, width in zip(elapsed_values, elapsed_widths)
            ),
        ]
        print("  ".join(row_parts))


def _print_terminal_test_configuration(configuration: Dict[str, str]) -> None:
    """Print mdtest settings loaded from env_used.yaml."""
    if not configuration:
        return

    key_width = max(len(key) for key in configuration)
    print("\n=== MDTest Configuration (env_used.yaml) ===\n")
    for key in MDTEST_CONFIG_KEYS:
        if key in configuration:
            print(f"{key.ljust(key_width)}  {configuration[key]}")


def print_terminal_table(
    metrics: List[AggregatedMetrics],
    normalize_to: Optional[int] = None,
    test_configuration: Optional[Dict[str, str]] = None,
) -> None:
    """Print formatted metrics table to terminal."""
    if not metrics:
        print("No metrics to display")
        return

    # Collect all datestamps
    all_datestamps: Set[str] = set()
    for m in metrics:
        all_datestamps.update(m.datestamps)

    print(f"\nmdtest-elbencho results from {join_datestamps(all_datestamps)}")

    # Sort by (node_count, thread_count)
    sorted_metrics = sorted(metrics, key=lambda m: (m.node_count, m.thread_count))

    # === Performance Rates Table ===
    # Wrap rate getters with normalization so column widths and cell values
    # all use the same scaled numbers.
    n_create_avg = _make_norm_getter("create_rate_avg", normalize_to)
    n_create_std = _make_norm_getter("create_rate_stddev", normalize_to)
    n_stat_avg = _make_norm_getter("stat_rate_avg", normalize_to)
    n_stat_std = _make_norm_getter("stat_rate_stddev", normalize_to)
    n_delete_avg = _make_norm_getter("delete_rate_avg", normalize_to)
    n_delete_std = _make_norm_getter("delete_rate_stddev", normalize_to)

    create_avg_w, create_std_w = compute_rate_column_widths(
        sorted_metrics, n_create_avg, n_create_std
    )
    stat_avg_w, stat_std_w = compute_rate_column_widths(
        sorted_metrics, n_stat_avg, n_stat_std
    )
    delete_avg_w, delete_std_w = compute_rate_column_widths(
        sorted_metrics, n_delete_avg, n_delete_std
    )

    # Rates table uses formatted Nodes column ("<normalize_to> (<actual>)"
    # when normalizing); latency tables show the actual node count only.
    rates_nodes_w = max(
        5,
        max(len(_format_nodes_col(m.node_count, normalize_to)) for m in sorted_metrics),
    )
    nodes_w = max(5, max(len(str(m.node_count)) for m in sorted_metrics))
    threads_w = max(7, max(len(str(m.thread_count)) for m in sorted_metrics))
    iters_w = max(5, max(len(str(m.iteration_count)) for m in sorted_metrics))

    # Rate column widths (avg + " ± " + std)
    create_col_w = create_avg_w + 3 + create_std_w
    stat_col_w = stat_avg_w + 3 + stat_std_w
    delete_col_w = delete_avg_w + 3 + delete_std_w

    # Header widths (ensure header fits)
    create_hdr = "Create/s (avg±std)"
    stat_hdr = "Stat/s (avg±std)"
    delete_hdr = "Delete/s (avg±std)"
    create_col_w = max(create_col_w, len(create_hdr))
    stat_col_w = max(stat_col_w, len(stat_hdr))
    delete_col_w = max(delete_col_w, len(delete_hdr))

    print(f"\n=== Performance Rates (ops/sec{_normalization_note(normalize_to)}) ===\n")

    # Print header
    header_parts = [
        "Nodes".rjust(rates_nodes_w),
        "Threads".rjust(threads_w),
        "Iters".rjust(iters_w),
        create_hdr.rjust(create_col_w),
        stat_hdr.rjust(stat_col_w),
        delete_hdr.rjust(delete_col_w),
    ]
    print("  ".join(header_parts))
    total_width = sum(len(p) for p in header_parts) + 2 * (len(header_parts) - 1)
    print("-" * total_width)

    # Print rows
    for m in sorted_metrics:
        create_cell = format_aligned_rate_with_std(
            n_create_avg(m), n_create_std(m), create_avg_w, create_std_w
        )
        stat_cell = format_aligned_rate_with_std(
            n_stat_avg(m), n_stat_std(m), stat_avg_w, stat_std_w
        )
        delete_cell = format_aligned_rate_with_std(
            n_delete_avg(m), n_delete_std(m), delete_avg_w, delete_std_w
        )

        row_parts = [
            _format_nodes_col(m.node_count, normalize_to).rjust(rates_nodes_w),
            str(m.thread_count).rjust(threads_w),
            str(m.iteration_count).rjust(iters_w),
            create_cell.rjust(create_col_w),
            stat_cell.rjust(stat_col_w),
            delete_cell.rjust(delete_col_w),
        ]
        print("  ".join(row_parts))

    _print_terminal_elapsed_table(sorted_metrics, nodes_w, threads_w, iters_w)

    # === Latency Percentile Tables (one per operation) ===
    for op_name, get_percentiles in [
        (
            "Create",
            lambda m: (
                m.create_lat_p0,
                m.create_lat_p50,
                m.create_lat_p90,
                m.create_lat_p99,
                m.create_lat_p100,
            ),
        ),
        (
            "Stat",
            lambda m: (
                m.stat_lat_p0,
                m.stat_lat_p50,
                m.stat_lat_p90,
                m.stat_lat_p99,
                m.stat_lat_p100,
            ),
        ),
        (
            "Delete",
            lambda m: (
                m.delete_lat_p0,
                m.delete_lat_p50,
                m.delete_lat_p90,
                m.delete_lat_p99,
                m.delete_lat_p100,
            ),
        ),
    ]:
        print(f"\n=== {op_name} Latency Percentiles (ms) ===\n")

        # Collect all values per percentile column
        p0_vals = [get_percentiles(m)[0] for m in sorted_metrics]
        p50_vals = [get_percentiles(m)[1] for m in sorted_metrics]
        p90_vals = [get_percentiles(m)[2] for m in sorted_metrics]
        p99_vals = [get_percentiles(m)[3] for m in sorted_metrics]
        p100_vals = [get_percentiles(m)[4] for m in sorted_metrics]

        # Compute column widths (before_decimal, after_decimal)
        p0_b, p0_a = compute_latency_column_widths(p0_vals)
        p50_b, p50_a = compute_latency_column_widths(p50_vals)
        p90_b, p90_a = compute_latency_column_widths(p90_vals)
        p99_b, p99_a = compute_latency_column_widths(p99_vals)
        p100_b, p100_a = compute_latency_column_widths(p100_vals)

        # Total column widths (before + "." + after, or just before if no decimals)
        p0_w = p0_b + (1 + p0_a if p0_a > 0 else 0)
        p50_w = p50_b + (1 + p50_a if p50_a > 0 else 0)
        p90_w = p90_b + (1 + p90_a if p90_a > 0 else 0)
        p99_w = p99_b + (1 + p99_a if p99_a > 0 else 0)
        p100_w = p100_b + (1 + p100_a if p100_a > 0 else 0)

        # Ensure header fits
        p0_w = max(p0_w, 2)
        p50_w = max(p50_w, 3)
        p90_w = max(p90_w, 3)
        p99_w = max(p99_w, 3)
        p100_w = max(p100_w, 4)

        # Print header
        lat_header_parts = [
            "Nodes".rjust(nodes_w),
            "Threads".rjust(threads_w),
            "p0".rjust(p0_w),
            "p50".rjust(p50_w),
            "p90".rjust(p90_w),
            "p99".rjust(p99_w),
            "p100".rjust(p100_w),
        ]
        print("  ".join(lat_header_parts))
        lat_total_width = sum(len(p) for p in lat_header_parts) + 2 * (
            len(lat_header_parts) - 1
        )
        print("-" * lat_total_width)

        # Print rows
        for m in sorted_metrics:
            p0, p50, p90, p99, p100 = get_percentiles(m)
            row_parts = [
                str(m.node_count).rjust(nodes_w),
                str(m.thread_count).rjust(threads_w),
                format_decimal_aligned_latency(p0, p0_b, p0_a, p0_w),
                format_decimal_aligned_latency(p50, p50_b, p50_a, p50_w),
                format_decimal_aligned_latency(p90, p90_b, p90_a, p90_w),
                format_decimal_aligned_latency(p99, p99_b, p99_a, p99_w),
                format_decimal_aligned_latency(p100, p100_b, p100_a, p100_w),
            ]
            print("  ".join(row_parts))

    _print_terminal_test_configuration(test_configuration or {})

    # Print representative commands (one per operation)
    if sorted_metrics:
        m = sorted_metrics[0]
        print("\n=== Representative Commands ===\n")
        if m.create_command:
            print(f"# Create: {strip_command(m.create_command)}")
        if m.stat_command:
            print(f"# Stat:   {strip_command(m.stat_command)}")
        if m.delete_command:
            print(f"# Delete: {strip_command(m.delete_command)}")

    print()


# =============================================================================
# Plotting
# =============================================================================

# Colorblind-safe colors for operation types
OPERATION_COLORS = {
    "create": "#0077BB",  # Blue
    "stat": "#EE7733",  # Orange
    "delete": "#009988",  # Teal
}

LEGEND_LOC_UPPER_LEFT = "upper left"


def _normalize_to_max_percent(values: List[float]) -> List[float]:
    """
    Normalize a series to percent-of-maximum (max becomes 100%).

    If all values are <= 0, returns a zeroed series.
    """
    max_val = max(values, default=0.0)
    if max_val <= 0:
        return [0.0 for _ in values]
    return [(v / max_val) * 100.0 for v in values]


def _format_tick_with_commas(value, pos):  # pylint: disable=unused-argument
    """Format tick label with comma thousands separator."""
    if value >= 1 or value == 0:
        return f"{int(value):,}"
    elif value >= 0.1:
        return f"{value:.1f}"
    else:
        return f"{value:.2f}"


def draw_candlestick(
    ax: Axes,
    x: float,
    avg: float,
    stddev: float,
    min_val: float,
    max_val: float,
    color: str,
) -> None:
    """
    Draw a candlestick at the specified x position.

    Args:
        ax: Matplotlib axes
        x: X position
        avg: Average value (center of fat bar)
        stddev: Standard deviation (half-width of fat bar)
        min_val: Minimum value (bottom of thin line)
        max_val: Maximum value (top of thin line)
        color: Color for the candlestick
    """
    # Thin line from min to max
    ax.vlines(x, min_val, max_val, color=color, linewidth=1)

    # Fat bar from avg-stddev to avg+stddev
    lower = max(0, avg - stddev)
    upper = avg + stddev
    ax.vlines(x, lower, upper, color=color, linewidth=6)


def plot_single_node_rates(
    metrics: List[AggregatedMetrics],
    output_dir: str,
    datestamps: Set[str],
    title_stamp: str,
) -> None:
    """Generate rate plot for single-node data (X-axis = thread count)."""
    prefix = "mdtest-elbencho-sn-rates-"
    plt.figure(figsize=(12, 8))
    ax = plt.gca()

    # Sort by thread count
    sorted_metrics = sorted(metrics, key=lambda m: m.thread_count)
    thread_counts = [m.thread_count for m in sorted_metrics]

    # Plot each operation type
    for op_name, color, get_rates in [
        (
            "Create",
            OPERATION_COLORS["create"],
            lambda m: (
                m.create_rate_avg,
                m.create_rate_stddev,
                m.create_rate_min,
                m.create_rate_max,
            ),
        ),
        (
            "Stat",
            OPERATION_COLORS["stat"],
            lambda m: (
                m.stat_rate_avg,
                m.stat_rate_stddev,
                m.stat_rate_min,
                m.stat_rate_max,
            ),
        ),
        (
            "Delete",
            OPERATION_COLORS["delete"],
            lambda m: (
                m.delete_rate_avg,
                m.delete_rate_stddev,
                m.delete_rate_min,
                m.delete_rate_max,
            ),
        ),
    ]:
        x_positions = []
        offset = {"Create": -0.15, "Stat": 0, "Delete": 0.15}[op_name]
        for m in sorted_metrics:
            x = m.thread_count + offset
            x_positions.append(x)

            avg, stddev, min_val, max_val = get_rates(m)
            draw_candlestick(ax, x, avg, stddev, min_val, max_val, color)

        avgs = [get_rates(m)[0] for m in sorted_metrics]
        ax.plot(
            x_positions, avgs, "-o", color=color, alpha=0.5, label=op_name, markersize=8
        )

    ax.set_xlabel("Thread Count")
    ax.set_ylabel("Operations per Second")
    ax.set_title(f"MDTest-Elbencho Performance Rates - {title_stamp}")
    ax.set_xticks(thread_counts)
    ax.set_xticklabels([str(t) for t in thread_counts])
    ax.yaxis.set_major_formatter(FuncFormatter(_format_tick_with_commas))
    ax.set_ylim(bottom=0)
    ax.legend(loc=LEGEND_LOC_UPPER_LEFT)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(
        os.path.join(output_dir, _plot_basename(prefix, datestamps)),
        bbox_inches="tight",
        dpi=150,
        pil_kwargs=PNG_SAVE_OPTIONS,
    )
    plt.close()


def plot_multi_node_rates(
    metrics: List[AggregatedMetrics],
    output_dir: str,
    datestamps: Set[str],
    title_stamp: str,
) -> None:
    """
    Generate rate plot for multi-node data (X-axis = node count).

    Uses colors for operation types (Create=blue, Stat=orange, Delete=teal)
    and line styles for thread counts. Each data point is labeled with
    the thread count for clarity.
    """
    prefix = "mdtest-elbencho-mn-rates-"
    plt.figure(figsize=(14, 10))
    ax = plt.gca()

    # Get unique node counts and thread counts
    node_counts = sorted({m.node_count for m in metrics})
    thread_counts = sorted({m.thread_count for m in metrics})

    # Line styles for different thread counts
    line_styles = ["-", "--", ":", "-."]
    markers = ["o", "s", "^", "D", "v", "<", ">", "p"]
    thread_style = {
        t: (line_styles[i % len(line_styles)], markers[i % len(markers)])
        for i, t in enumerate(thread_counts)
    }

    # Track legend entries (one per operation type)
    op_legend_handles = []
    op_legend_labels = []

    # Plot each operation type with its designated color
    for op_name, color, get_avg, get_std in [
        (
            "Create",
            OPERATION_COLORS["create"],
            lambda m: m.create_rate_avg,
            lambda m: m.create_rate_stddev,
        ),
        (
            "Stat",
            OPERATION_COLORS["stat"],
            lambda m: m.stat_rate_avg,
            lambda m: m.stat_rate_stddev,
        ),
        (
            "Delete",
            OPERATION_COLORS["delete"],
            lambda m: m.delete_rate_avg,
            lambda m: m.delete_rate_stddev,
        ),
    ]:
        # Plot one line per thread count
        for thread_count in thread_counts:
            # Filter and sort metrics for this thread count
            thread_metrics = sorted(
                [m for m in metrics if m.thread_count == thread_count],
                key=lambda m: m.node_count,
            )

            if not thread_metrics:
                continue

            linestyle, marker = thread_style[thread_count]
            x_vals = [m.node_count for m in thread_metrics]
            y_vals = [get_avg(m) for m in thread_metrics]
            y_errs = [get_std(m) for m in thread_metrics]

            # Plot line with error bars
            ax.errorbar(
                x_vals,
                y_vals,
                yerr=y_errs,
                color=color,
                linestyle=linestyle,
                marker=marker,
                markersize=5,
                capsize=3,
                alpha=0.8,
            )

            # Add thread count labels at each data point
            for x, y in zip(x_vals, y_vals):
                ax.annotate(
                    f"{thread_count}t",
                    (x, y),
                    textcoords="offset points",
                    xytext=(5, 5),
                    fontsize=7,
                    alpha=0.7,
                )

        # Add to operation legend (once per operation type)
        op_legend_handles.append(
            Line2D([0], [0], color=color, linewidth=2, marker="o", markersize=5)
        )
        op_legend_labels.append(op_name)

    ax.set_xlabel("Number of Nodes")
    ax.set_ylabel("Operations per Second (op/s)")
    ax.set_title(f"MDTest-Elbencho Multi-Node Performance Rates - {title_stamp}")
    ax.set_xticks(node_counts)  # Ensure clean integer tick marks
    ax.yaxis.set_major_formatter(FuncFormatter(_format_tick_with_commas))
    ax.set_ylim(bottom=0)

    # Legend for operation types (upper left)
    op_legend = ax.legend(
        op_legend_handles,
        op_legend_labels,
        loc=LEGEND_LOC_UPPER_LEFT,
        fontsize="small",
    )
    ax.add_artist(op_legend)  # Keep this legend when adding second one

    # Legend for thread counts / line styles (just right of operation legend)
    thread_legend_handles = []
    thread_legend_labels = []
    for thread_count in thread_counts:
        linestyle, marker = thread_style[thread_count]
        thread_legend_handles.append(
            Line2D(
                [0], [0], color="gray", linestyle=linestyle, marker=marker, markersize=5
            )
        )
        thread_legend_labels.append(f"{thread_count}t")

    ax.legend(
        thread_legend_handles,
        thread_legend_labels,
        title="Threads",
        loc=LEGEND_LOC_UPPER_LEFT,
        bbox_to_anchor=(0.12, 1.0),  # Position just right of operation legend
        fontsize="small",
    )

    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(
        os.path.join(output_dir, _plot_basename(prefix, datestamps)),
        bbox_inches="tight",
        dpi=150,
        pil_kwargs=PNG_SAVE_OPTIONS,
    )
    plt.close()


def plot_single_node_efficiency(
    metrics: List[AggregatedMetrics],
    output_dir: str,
    datestamps: Set[str],
    title_stamp: str,
) -> None:
    """Generate scaling efficiency plot for single-node data."""
    prefix = "mdtest-elbencho-sn-efficiency-"
    plt.figure(figsize=(12, 8))
    ax = plt.gca()

    # Sort by thread count
    sorted_metrics = sorted(metrics, key=lambda m: m.thread_count)
    thread_counts = [m.thread_count for m in sorted_metrics]
    # Plot each operation type
    for op_name, color, get_rate in [
        (
            "Create",
            OPERATION_COLORS["create"],
            lambda m: m.create_rate_avg,
        ),
        ("Stat", OPERATION_COLORS["stat"], lambda m: m.stat_rate_avg),
        (
            "Delete",
            OPERATION_COLORS["delete"],
            lambda m: m.delete_rate_avg,
        ),
    ]:
        rate_per_x = [get_rate(m) / m.thread_count for m in sorted_metrics]
        efficiencies = _normalize_to_max_percent(rate_per_x)

        ax.plot(
            thread_counts,
            efficiencies,
            "-o",
            color=color,
            label=op_name,
            markersize=8,
        )

    # Add perfect scaling line
    ax.axhline(y=100, linestyle="--", color="gray", alpha=0.7, label="Perfect Scaling")

    ax.set_xlabel("Thread Count")
    ax.set_ylabel("Scaling Efficiency (%)")
    ax.set_title(f"MDTest-Elbencho Scaling Efficiency - {title_stamp}")
    ax.set_xticks(thread_counts)
    ax.set_xticklabels([str(t) for t in thread_counts])
    ax.set_ylim(0, 105)
    ax.legend(loc="best")
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    plt.savefig(
        os.path.join(output_dir, _plot_basename(prefix, datestamps)),
        bbox_inches="tight",
        dpi=150,
        pil_kwargs=PNG_SAVE_OPTIONS,
    )
    plt.close()


def plot_multi_node_efficiency(
    metrics: List[AggregatedMetrics],
    output_dir: str,
    datestamps: Set[str],
    title_stamp: str,
) -> None:
    """Generate scaling efficiency plots for multi-node data (one per operation)."""
    # Get unique node and thread counts
    node_counts = sorted({m.node_count for m in metrics})
    thread_counts = sorted({m.thread_count for m in metrics})
    min_nodes = min(node_counts)

    # Create colormap for thread counts
    with plt.style.context("tableau-colorblind10"):
        colorblind_colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    thread_colors = {
        t: colorblind_colors[i % len(colorblind_colors)]
        for i, t in enumerate(thread_counts)
    }

    for op_name, get_rate in [
        ("create", lambda m: m.create_rate_avg),
        ("stat", lambda m: m.stat_rate_avg),
        ("delete", lambda m: m.delete_rate_avg),
    ]:
        prefix = f"mdtest-elbencho-mn-efficiency-{op_name}-"
        plt.figure(figsize=(12, 8))
        ax = plt.gca()

        for thread_count in thread_counts:
            # Get metrics for this thread count
            thread_metrics = sorted(
                [m for m in metrics if m.thread_count == thread_count],
                key=lambda m: m.node_count,
            )

            if not thread_metrics:
                continue

            # Find baseline (min node count for this thread count)
            baseline_metrics = [m for m in thread_metrics if m.node_count == min_nodes]
            if not baseline_metrics:
                continue
            baseline_rate = get_rate(baseline_metrics[0]) / min_nodes

            if baseline_rate <= 0:
                continue

            # Calculate efficiencies
            x_positions = [m.node_count for m in thread_metrics]
            rate_per_x = [get_rate(m) / m.node_count for m in thread_metrics]
            efficiencies = _normalize_to_max_percent(rate_per_x)

            ax.plot(
                x_positions,
                efficiencies,
                "-o",
                color=thread_colors[thread_count],
                label=f"{thread_count}t",
                markersize=8,
            )

        # Add perfect scaling line
        ax.axhline(
            y=100, linestyle="--", color="gray", alpha=0.7, label="Perfect Scaling"
        )

        ax.set_xlabel("Node Count")
        ax.set_ylabel("Scaling Efficiency (%)")
        ax.set_title(
            f"MDTest-Elbencho {op_name.capitalize()} Scaling Efficiency - "
            f"{title_stamp}"
        )
        ax.set_xticks(node_counts)
        ax.set_xticklabels([str(n) for n in node_counts])
        ax.set_ylim(0, 105)
        ax.legend(title="Threads", loc="best", fontsize="small")
        ax.grid(True, alpha=0.3)

        plt.tight_layout()
        plt.savefig(
            os.path.join(
                output_dir,
                _plot_basename(prefix, datestamps),
            ),
            bbox_inches="tight",
            dpi=150,
            pil_kwargs=PNG_SAVE_OPTIONS,
        )
        plt.close()


def plot_latency_histograms(
    metrics: List[AggregatedMetrics],
    output_dir: str,
    datestamps: Set[str],
    title_stamp: str,
    is_multi_node: bool,
) -> None:
    """Generate latency histogram plots (one per operation type)."""
    sn_mn = "mn" if is_multi_node else "sn"

    # Get unique thread counts for coloring
    thread_counts = sorted({m.thread_count for m in metrics})

    # Create colormap for thread counts
    with plt.style.context("tableau-colorblind10"):
        colorblind_colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    thread_colors = {
        t: colorblind_colors[i % len(colorblind_colors)]
        for i, t in enumerate(thread_counts)
    }

    for op_name, get_histogram, get_percentiles in [
        (
            "create",
            lambda m: m.create_histogram,
            lambda m: (m.create_lat_p50, m.create_lat_p99),
        ),
        (
            "stat",
            lambda m: m.stat_histogram,
            lambda m: (m.stat_lat_p50, m.stat_lat_p99),
        ),
        (
            "delete",
            lambda m: m.delete_histogram,
            lambda m: (m.delete_lat_p50, m.delete_lat_p99),
        ),
    ]:
        prefix = f"mdtest-elbencho-{sn_mn}-latency-hist-{op_name}-"
        plt.figure(figsize=(12, 8))
        ax = plt.gca()

        # Track legend handles
        legend_handles = []
        legend_labels = []

        for thread_count in thread_counts:
            # Combine histograms for all metrics with this thread count
            thread_metrics = [m for m in metrics if m.thread_count == thread_count]
            combined_hist = combine_histograms(
                [get_histogram(m) for m in thread_metrics]
            )

            if not combined_hist:
                continue

            # Sort and plot
            sorted_buckets = sorted(combined_hist.keys())
            x_values = [b / 1000 for b in sorted_buckets]  # Convert us to ms
            y_values = [combined_hist[b] for b in sorted_buckets]

            color = thread_colors[thread_count]
            (line,) = ax.plot(
                x_values, y_values, "-o", color=color, markersize=3, alpha=0.8
            )
            legend_handles.append(line)
            legend_labels.append(f"{thread_count}t")

            # Add percentile lines (using first metric for this thread count)
            if thread_metrics:
                p50, p99 = get_percentiles(thread_metrics[0])
                if p50 > 0:
                    ax.axvline(
                        x=p50, color=color, linestyle="--", alpha=0.5, linewidth=1
                    )
                if p99 > 0:
                    ax.axvline(
                        x=p99, color=color, linestyle=":", alpha=0.5, linewidth=1.5
                    )

        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlabel("Latency (ms)")
        ax.set_ylabel("Count")
        ax.set_title(
            f"MDTest-Elbencho {op_name.capitalize()} Latency Distribution - "
            f"{title_stamp}"
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
        plt.savefig(
            os.path.join(
                output_dir,
                _plot_basename(prefix, datestamps),
            ),
            bbox_inches="tight",
            dpi=150,
            pil_kwargs=PNG_SAVE_OPTIONS,
        )
        plt.close()


def plot_all(
    metrics: List[AggregatedMetrics],
    output_dir: str,
) -> None:
    """Generate all plots."""
    if not metrics:
        return

    all_datestamps: Set[str] = set()
    for m in metrics:
        all_datestamps.update(m.datestamps)

    # Determine if single-node or multi-node
    unique_nodes = {m.node_count for m in metrics}
    is_multi_node = len(unique_nodes) > 1

    # One token for every title in this run; each savefig still budgets its own
    # basename against its own prefix.
    title_stamp = _title_datestamp_token(all_datestamps, is_multi_node)

    eprint(f"Generating plots in {output_dir}...")

    # Rate plots
    if is_multi_node:
        plot_multi_node_rates(metrics, output_dir, all_datestamps, title_stamp)
    else:
        plot_single_node_rates(metrics, output_dir, all_datestamps, title_stamp)

    # Efficiency plots
    if is_multi_node:
        plot_multi_node_efficiency(metrics, output_dir, all_datestamps, title_stamp)
    else:
        plot_single_node_efficiency(metrics, output_dir, all_datestamps, title_stamp)

    # Latency histogram plots
    plot_latency_histograms(
        metrics, output_dir, all_datestamps, title_stamp, is_multi_node
    )

    eprint("Plots generated successfully.")


# =============================================================================
# Filtering
# =============================================================================


def filter_metrics(
    metrics: List[AggregatedMetrics],
    only_nodes: Optional[Set[int]] = None,
    only_threads: Optional[Set[int]] = None,
) -> List[AggregatedMetrics]:
    """Filter metrics based on node and thread counts."""
    return filter_metrics_by_scale(metrics, only_nodes, only_threads)


# =============================================================================
# Machine-readable results (JSON fragment / document)
# =============================================================================

_JSON_PHASES = (("create", "WRITE"), ("stat", "STAT"), ("delete", "RMFILES"))


@dataclass(frozen=True)
class _Slot:
    """One result slot: a ledger execution, or result files with no ledger entry."""

    execution_id: Optional[str]
    definition: MdtestDefinition

    @property
    def nodes(self) -> int:
        """Node count of the slot."""
        return self.definition.nodes

    @property
    def tasks(self) -> int:
        """Tasks per node of the slot."""
        return self.definition.tasks

    @property
    def iterations(self) -> Optional[int]:
        """Configured iteration count, if known."""
        return self.definition.iterations


@dataclass
class _IterationFiles:
    """The result files of one iteration; either file may be missing."""

    datestamp: str
    iteration: int
    csv_path: Optional[str] = None
    out_path: Optional[str] = None


def _read_mdtest_ledger(input_dir: str, env: Dict[str, Any]) -> List[_Slot]:
    """List the metadata executions in executions/NNNN.sh, ordered by id."""
    executions_dir = os.path.join(input_dir, "executions")
    if not os.path.isdir(executions_dir):
        return []
    slots = []
    for filename in sorted(os.listdir(executions_dir)):
        if not re.fullmatch(r"[0-9]+\.sh", filename):
            continue
        try:
            with open(
                os.path.join(executions_dir, filename), "r", encoding="utf-8"
            ) as definition:
                content = definition.read()
        except OSError as error:
            eprint(f"Warning: Cannot read execution definition {filename}: {error}")
            continue
        if not is_mdtest_definition(content):
            continue
        definition = parse_mdtest_definition(content, env)
        if definition is None:
            eprint(f"Warning: Invalid metadata execution coordinates in {filename}")
            continue
        slots.append(_Slot(filename[:-3], definition))
    return slots


def _scan_result_files(
    input_dir: str,
) -> Dict[Tuple[int, int], List[_IterationFiles]]:
    """Group every result file in a directory by (nodes, tasks) and iteration."""
    by_iteration: Dict[Tuple[int, int, int, str], _IterationFiles] = {}
    for filename in sorted(os.listdir(input_dir)):
        match = FILENAME_PATTERN.fullmatch(filename)
        if not match:
            continue
        iteration = int(match.group(4))
        key = (int(match.group(1)), int(match.group(2)), iteration, match.group(3))
        files = by_iteration.setdefault(key, _IterationFiles(match.group(3), iteration))
        path = os.path.join(input_dir, filename)
        if match.group(5) == "csv":
            files.csv_path = path
        else:
            files.out_path = path
    grouped: Dict[Tuple[int, int], List[_IterationFiles]] = {}
    for key in sorted(by_iteration):
        grouped.setdefault((key[0], key[1]), []).append(by_iteration[key])
    return grouped


def _load_iteration(
    files: _IterationFiles,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[float, int]], List[str]]:
    """Parse whatever result files exist; return (csv, out, reasons it is not whole)."""
    reasons: List[str] = []
    csv_data: Dict[str, Dict[str, Any]] = {}
    out_data: Dict[str, Dict[float, int]] = {}
    if files.csv_path is None:
        reasons.append("missing CSV file")
    else:
        try:
            csv_data = parse_csv_file(files.csv_path)
        except (OSError, ValueError, csv.Error) as error:
            reasons.append(f"unparseable CSV file: {error}")
    if files.out_path is None:
        reasons.append("missing OUT file")
    else:
        try:
            out_data = parse_out_file(files.out_path)
        except (OSError, ValueError) as error:
            reasons.append(f"unparseable OUT file: {error}")
    if not reasons:
        incomplete = incomplete_result_reason(csv_data, out_data)
        if incomplete:
            reasons.append(incomplete)
    return csv_data, out_data, reasons


def _iteration_phase(
    name: str,
    csv_row: Optional[Dict[str, Any]],
    histogram: Dict[float, int],
) -> Optional[Dict[str, Any]]:
    """One phase of one iteration, or None when nothing of it was parsed."""
    if csv_row is None and not histogram:
        return None
    row = csv_row or {}
    latency = None
    if row.get("lat_valid"):
        latency = {
            "min": results_json.seconds_to_us(row["lat_min"]),
            "avg": results_json.seconds_to_us(row["lat_avg"]),
            "max": results_json.seconds_to_us(row["lat_max"]),
        }
    return {
        "phase": name,
        "ops_per_s": (
            results_json.round_ops(row["rate"]) if row.get("rate_valid") else None
        ),
        "elapsed_ms": (
            int(round(row["elapsed_sec"] * 1000)) if row.get("elapsed_valid") else None
        ),
        "latency_us": latency,
        "histogram_us": results_json.histogram_columnar(histogram),
        "command": results_json.command_argv(row["command"]) if csv_row else None,
    }


def _iteration_entry(
    files: _IterationFiles,
    csv_data: Dict[str, Dict[str, Any]],
    out_data: Dict[str, Dict[float, int]],
    reasons: List[str],
) -> Dict[str, Any]:
    """The per-iteration JSON object."""
    phases = [
        phase
        for name, operation in _JSON_PHASES
        if (
            phase := _iteration_phase(
                name, csv_data.get(operation), out_data.get(operation) or {}
            )
        )
    ]
    problems = (
        [results_json.problem(results_json.ITERATION_INCOMPLETE, "; ".join(reasons))]
        if reasons
        else []
    )
    return {
        "iteration": files.iteration,
        "datestamp": files.datestamp,
        "producer_version": (
            recorded_result_version(files.out_path) if files.out_path else None
        ),
        "state": "incomplete" if reasons else "complete",
        "problems": problems,
        "phases": phases,
    }


def _percentile_us(histogram: Dict[float, int], percentile: float) -> Optional[float]:
    """Pooled-histogram percentile in microseconds (None for an empty histogram)."""
    if not histogram:
        return None
    return results_json.round_latency_us(
        percentile_from_histogram(histogram, percentile) * 1000.0
    )


def _aggregate_phase(name: str, agg: AggregatedMetrics) -> Dict[str, Any]:
    """Aggregate one phase over the validated iterations."""
    histogram = getattr(agg, f"{name}_histogram")
    return {
        "phase": name,
        "ops_per_s": {
            "mean": results_json.round_ops(getattr(agg, f"{name}_rate_avg")),
            "stddev": results_json.round_ops(getattr(agg, f"{name}_rate_stddev")),
            "min": results_json.round_ops(getattr(agg, f"{name}_rate_min")),
            "max": results_json.round_ops(getattr(agg, f"{name}_rate_max")),
        },
        "mean_elapsed_ms": int(round(getattr(agg, f"{name}_elapsed_avg_sec") * 1000)),
        "latency_us": {
            f"p{percent}": _percentile_us(histogram, percent)
            for percent in (0, 50, 90, 99, 100)
        },
        "latency_method": "pooled_histogram_bucket",
    }


def _aggregate_json(
    metrics: List[IterationMetrics], expected: Optional[int]
) -> Dict[str, Any]:
    """The aggregate block; only validated iterations contribute."""
    phases = []
    if metrics:
        agg = next(iter(aggregate_metrics(metrics).values()))
        phases = [_aggregate_phase(name, agg) for name, _ in _JSON_PHASES]
    return {
        "iterations_expected": expected,
        "iterations_used": len(metrics),
        "phases": phases,
    }


def _result_state(
    iterations: List[Dict[str, Any]], missing: List[int], found: bool
) -> str:
    """complete, partial, missing or unparseable from the iteration outcomes."""
    if not found:
        return "missing"
    if not any(entry["phases"] for entry in iterations):
        return "unparseable"
    incomplete = any(entry["state"] != "complete" for entry in iterations)
    return "partial" if incomplete or missing else "complete"


def _missing_iterations(
    iterations: List[Dict[str, Any]], expected: Optional[int]
) -> List[int]:
    """Expected iteration numbers that have no result files at all."""
    if expected is None:
        return []
    present = {entry["iteration"] for entry in iterations}
    return [number for number in range(1, expected + 1) if number not in present]


def _result_file_records(
    files_list: List[_IterationFiles], input_dir: str
) -> List[Dict[str, Any]]:
    """file_record for every result file, relative to the input directory."""
    paths = sorted(
        path
        for files in files_list
        for path in (
            files.csv_path,
            files.out_path,
            files.out_path and files.out_path + VERSION_SUFFIX,
        )
        if path is not None and os.path.isfile(path)
    )
    return [
        results_json.file_record(path, os.path.relpath(path, input_dir))
        for path in paths
    ]


def _execution_entry(
    slot: _Slot, files_list: List[_IterationFiles], input_dir: str
) -> Dict[str, Any]:
    """The fragment entry of one slot with its result files."""
    iterations: List[Dict[str, Any]] = []
    metrics: List[IterationMetrics] = []
    for files in files_list:
        csv_data, out_data, reasons = _load_iteration(files)
        iterations.append(_iteration_entry(files, csv_data, out_data, reasons))
        if not reasons:
            metrics.append(
                metrics_from_parsed(
                    csv_data,
                    out_data,
                    slot.nodes,
                    slot.tasks,
                    files.datestamp,
                    files.iteration,
                )
            )
    missing = _missing_iterations(iterations, slot.iterations)
    # Execution problems say what made the execution partial; details stay on
    # each iteration.
    problems = [
        results_json.problem(
            item["code"], f"iteration {iteration['iteration']}: {item['detail']}"
        )
        for iteration in iterations
        for item in iteration["problems"]
    ]
    if missing:
        detail = "missing iterations: " + ", ".join(str(number) for number in missing)
        problems.append(results_json.problem(results_json.ITERATIONS_MISSING, detail))
    results = None
    if files_list:
        results = {
            "iterations": iterations,
            "aggregate": _aggregate_json(metrics, slot.iterations),
        }
    coordinates, coordinate_key = mdtest_coordinates(slot.definition, len(iterations))
    return {
        "execution_id": slot.execution_id,
        "coordinates": coordinates,
        "coordinate_key": coordinate_key,
        "dataset": None,
        "result_state": _result_state(iterations, missing, bool(files_list)),
        "problems": problems,
        "result_files": _result_file_records(files_list, input_dir),
        "results": results,
    }


def _not_reported_entry(slot: _Slot) -> Dict[str, Any]:
    """The entry of an execution removed by --only-nodes/--only-threads."""
    coordinates, coordinate_key = mdtest_coordinates(slot.definition)
    return {
        "execution_id": slot.execution_id,
        "coordinates": coordinates,
        "coordinate_key": coordinate_key,
        "dataset": None,
        "result_state": "not_reported",
        "problems": [],
        "result_files": [],
        "results": None,
    }


def _slot_entry(
    slot: _Slot, files_list: List[_IterationFiles], input_dir: str, selected: bool
) -> Dict[str, Any]:
    """Pool task: the fragment entry of one slot, parsing its result files."""
    if not selected:
        return _not_reported_entry(slot)
    return _execution_entry(slot, files_list, input_dir)


def _planned_slots(
    input_dir: str, env: Dict[str, Any]
) -> List[Tuple[_Slot, List[_IterationFiles]]]:
    """Every ledger slot, then every (nodes, tasks) of files without one."""
    scanned = _scan_result_files(input_dir)
    planned = [
        (slot, scanned.pop((slot.nodes, slot.tasks), []))
        for slot in _read_mdtest_ledger(input_dir, env)
    ]
    for (nodes, tasks), files_list in sorted(scanned.items()):
        definition = MdtestDefinition(
            nodes,
            tasks,
            optional_int(env.get("MDTEST_ITERATIONS")),
            optional_int(env.get("MDTEST_BRANCH_FACTOR")),
            optional_int(env.get("MDTEST_ITEMS_PER_DIR")),
        )
        planned.append((_Slot(None, definition), files_list))
    return planned


ScaleFilters = Tuple[Optional[Set[int]], Optional[Set[int]]]


def start_results_fragment(
    input_dir: str, env: Dict[str, Any], scale: ScaleFilters, pool: Executor
) -> Callable[[], Dict[str, Any]]:
    """Submit one parse task per slot; the returned call builds the fragment.

    It describes every ledger execution and result file; those that
    ``scale`` (--only-nodes, --only-threads) removes are not_reported.
    """
    only_nodes, only_threads = scale
    planned = [
        (
            slot,
            files_list,
            (only_nodes is None or slot.nodes in only_nodes)
            and (only_threads is None or slot.tasks in only_threads),
        )
        for slot, files_list in _planned_slots(input_dir, env)
    ]
    # Beside a ledger, result files outside it are only diagnostics; a
    # filtered-out one is left out.
    ledger = any(slot.execution_id for slot, _, _ in planned)
    tasks = [
        pool.submit(captured, _slot_entry, slot, files_list, input_dir, selected)
        for slot, files_list, selected in planned
        if selected or slot.execution_id or not ledger
    ]

    def finish() -> Dict[str, Any]:
        return {
            "fragment_version": results_json.FRAGMENT_VERSION,
            "kind": "mdtest",
            "executions": [replay(task) for task in tasks],
            "diagnostics": [],
        }

    return finish


def build_results_fragment(
    input_dir: str,
    env: Dict[str, Any],
    only_nodes: Optional[Set[int]] = None,
    only_threads: Optional[Set[int]] = None,
) -> Dict[str, Any]:
    """Describe every ledger execution and result file of one input directory."""
    return start_results_fragment(
        input_dir, env, (only_nodes, only_threads), InlineExecutor()
    )()


def validate_json_arguments(args) -> None:
    """Reject inputs the results document cannot describe."""
    validate_report_inputs(args.input_dirs, args.output_dir)
    if args.results_fragment and len(unique_inputs(args.input_dirs)) != 1:
        raise ValueError("--results-fragment needs exactly one input dir")


def write_json_outputs(
    args,
    environments: Dict[str, Dict[str, Any]],
    comparisons: Optional[List[Dict[str, Any]]] = None,
    pool: Optional[Executor] = None,
) -> Dict[str, Any]:
    """Write the fragment, or the document and report.html; return the document.

    For a fragment the document is only assembled, to render the text from.
    The input directories are parsed in ``pool`` tasks.
    """
    scale = _scale_filters(args)
    started = [
        (
            input_dir,
            start_results_fragment(input_dir, env, scale, pool or InlineExecutor()),
        )
        for input_dir, env in environments.items()
    ]
    fragments = [(input_dir, finish()) for input_dir, finish in started]
    generator = results_json.generator_block(
        ["utils/extract-mdtest-elbencho.py", *sys.argv[1:]],
        {
            "live_detail": getattr(args, "live_detail", None),
            "shortfall_percent": None,
            "min_slow_windows": None,
            "normalize_to": args.normalize_to,
            "only_nodes": args.only_nodes,
            "only_threads": args.only_threads,
            "kind": "mdtest",
        },
    )
    document = results_json.assemble_inputs_document(fragments, None, generator)
    if args.results_fragment:
        results_json.write_fragment(fragments[0][1], args.results_fragment)
        return document
    results_json.publish_document(
        document,
        os.path.join(args.output_dir or args.input_dirs[0], "results.json"),
        comparisons,
        _mdtest_selection(args),
    )
    return document


# =============================================================================
# Rendering from the results document
# =============================================================================


def _seconds(value: Optional[float], scale: float) -> float:
    return (value or 0.0) / scale


def _phase_fields(name: str, phase: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """IterationMetrics fields of one create, stat or delete phase."""
    phase = phase or {}
    latency = phase.get("latency_us") or {}
    histogram = phase.get("histogram_us") or {"buckets_us": [], "counts": []}
    command = phase.get("command")
    return {
        f"{name}_rate": phase.get("ops_per_s") or 0.0,
        f"{name}_elapsed_sec": _seconds(phase.get("elapsed_ms"), 1000.0),
        f"{name}_lat_min": _seconds(latency.get("min"), 1e6),
        f"{name}_lat_avg": _seconds(latency.get("avg"), 1e6),
        f"{name}_lat_max": _seconds(latency.get("max"), 1e6),
        f"{name}_histogram": dict(zip(histogram["buckets_us"], histogram["counts"])),
        f"{name}_command": shlex.join(command) if command else "",
    }


def iteration_metrics(
    coordinates: Dict[str, Any], iteration: Dict[str, Any]
) -> IterationMetrics:
    """One complete iteration of a document execution."""
    phases = {phase["phase"]: phase for phase in iteration["phases"]}
    values: Dict[str, Any] = {}
    for name, _ in _JSON_PHASES:
        values.update(_phase_fields(name, phases.get(name)))
    return IterationMetrics(
        node_count=coordinates["nodes"],
        thread_count=coordinates["tasks_per_node"],
        datestamp=iteration["datestamp"],
        iteration=iteration["iteration"],
        **values,
    )


def _complete_iterations(
    document: Dict[str, Any], groups: List[Dict[str, Any]]
) -> List[Tuple[Dict[str, Any], IterationMetrics, str]]:
    """(group, metrics, version) of each complete iteration of the groups."""
    by_id = {group["group_id"]: group for group in groups}
    found = []
    for execution in document["executions"]:
        results = execution.get("results")
        group = by_id.get(execution["group_id"])
        if group is None or not results or execution["result_state"] == "not_reported":
            continue
        for iteration in results["iterations"]:
            if iteration["state"] == "complete":
                metrics = iteration_metrics(execution["coordinates"], iteration)
                version = iteration.get("producer_version") or UNKNOWN_VERSION
                found.append((group, metrics, version))
    return found


def _iteration_key(
    group: Dict[str, Any], metrics: IterationMetrics, by_target: bool
) -> Tuple[Any, ...]:
    """(target or "", nodes, tasks, datestamp, iteration) of one iteration."""
    space = (group["target"]["name"] or "") if by_target else ""
    return (
        space,
        metrics.node_count,
        metrics.thread_count,
        metrics.datestamp,
        metrics.iteration,
    )


def document_iterations(
    document: Dict[str, Any], groups: List[Dict[str, Any]], by_target: bool = False
) -> List[Tuple[IterationMetrics, str]]:
    """Complete iterations of the groups' reported executions, with versions.

    The same (nodes, tasks, datestamp, iteration) in two groups is ambiguous
    and skipped, unless ``by_target`` and their targets differ.
    """
    found = _complete_iterations(document, groups)
    first: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    ambiguous = set()
    for group, metrics, _ in found:
        key = _iteration_key(group, metrics, by_target)
        seen = first.setdefault(key, group)
        if seen is not group:
            eprint(
                f"Warning: Duplicate result pair for {key[1:]!r} in "
                f"{seen.get('source')} and {group.get('source')}; skipping both"
            )
            ambiguous.add(key)
    return [
        (metrics, version)
        for group, metrics, version in found
        if _iteration_key(group, metrics, by_target) not in ambiguous
    ]


def document_configuration(groups: List[Dict[str, Any]]) -> Dict[str, str]:
    """Reportable mdtest settings across the groups' saved settings."""
    values_by_key: Dict[str, List[str]] = {key: [] for key in MDTEST_CONFIG_KEYS}
    for group in groups:
        _collect_configuration_values(values_by_key, group.get("settings") or {})
    return _render_configuration_values(values_by_key, "input directories")


def _scale_filters(args) -> ScaleFilters:
    return (
        validate_integer_filter("--only-nodes", args.only_nodes),
        validate_integer_filter("--only-threads", args.only_threads),
    )


def _mdtest_selection(
    args, groups: Optional[FrozenSet[str]] = None
) -> results_json.ReportSelection:
    """What report.html shows: the mdtest executions the text tables show."""
    nodes, threads = _scale_filters(args)
    return results_json.ReportSelection(
        kinds=frozenset(["mdtest"]),
        groups=groups,
        nodes=frozenset(nodes) if nodes else None,
        threads=frozenset(threads) if threads else None,
    )


def _mdtest_groups(
    document: Dict[str, Any], groups: Optional[FrozenSet[str]]
) -> List[Dict[str, Any]]:
    """The document's mdtest groups that ``groups`` (None: all) selects."""
    return [
        group
        for group in document["groups"]
        if group["kind"] == "mdtest" and (groups is None or group["group_id"] in groups)
    ]


def render_text_report(
    document: Dict[str, Any],
    args,
    target_label: str,
    output_dir: str,
    groups: Optional[FrozenSet[str]] = None,
) -> int:
    """Print the table report and write the plots; 1 with nothing to report."""
    selected = _mdtest_groups(document, groups)
    versions = document_iterations(
        document, selected, target_label.startswith(MIXED_PREFIX)
    )
    eprint(f"Found {len(versions)} complete iterations")
    if not versions:
        eprint("No valid iterations parsed")
        return 1
    aggregated_metrics = list(
        aggregate_metrics([metric for metric, _ in versions]).values()
    )
    eprint(f"Aggregated into {len(aggregated_metrics)} (node, thread) groups")
    if args.only_nodes or args.only_threads:
        aggregated_metrics = filter_metrics(aggregated_metrics, *_scale_filters(args))
        eprint(f"After filtering: {len(aggregated_metrics)} groups")
    if not aggregated_metrics:
        eprint("No metrics remaining after filtering")
        return 1
    os.makedirs(output_dir, exist_ok=True)
    produced_by = selected_versions(
        versions,
        aggregated_metrics,
        lambda metric: (metric.node_count, metric.thread_count),
    )
    mirror_stdout_to_file(
        os.path.join(output_dir, REPORT_TXT_FILENAME),
        with_versions(print_terminal_table, produced_by, target=target_label),
        aggregated_metrics,
        normalize_to=args.normalize_to,
        test_configuration=document_configuration(selected),
    )
    plot_all(aggregated_metrics, output_dir)
    return 0


# =============================================================================
# Main
# =============================================================================


def _test_parse(test_parse: str) -> None:
    base_path = test_parse.replace(".csv", "").replace(".out", "")
    csv_path = base_path + ".csv"
    out_path = base_path + ".out"

    eprint(f"Testing parse of {csv_path} and {out_path}")

    csv_data = parse_csv_file(csv_path)
    eprint("\nCSV Data:")
    for op, data in csv_data.items():
        eprint(
            f"  {op}: rate={data['rate']:.0f}, "
            f"elapsed={format_elapsed_time(data['elapsed_sec'])}, "
            f"lat_avg={data['lat_avg']*1000:.3f}ms"
        )

    out_data = parse_out_file(out_path)
    eprint("\nOUT Data (histograms):")
    for op, hist in out_data.items():
        if hist:
            eprint(f"  {op}: {len(hist)} buckets, total count={sum(hist.values())}")


def _report_from_json(parser, args) -> int:
    """Render text, plots and report.html from results documents.

    Several documents render into per-document subdirectories
    (``document_output_dir``) and share one report.html. A batch document
    renders like a batch report (group reports and reports.txt).
    """
    documents, comparisons, groups = results_json.load_from_json_inputs(parser, args)
    if not args.batch_group and any(
        is_batch_document(loaded.data) for loaded in documents
    ):
        return report_documents(documents, args, "mdtest")
    labels = [
        guarded_document_target(
            _mdtest_groups(loaded.data, groups), args.allow_mixed_targets
        )
        for loaded in documents
    ]
    if not args.batch_group:
        try:
            results_json.publish_report_html(
                documents, args.output_dir, comparisons, _mdtest_selection(args, groups)
            )
        except ValueError as error:  # a comparison naming unknown sections
            parser.error(str(error))
    statuses = []
    for number, (loaded, label) in enumerate(zip(documents, labels), 1):
        document_dir = document_output_dir(args.output_dir, number, len(documents))
        # Valid by now, so the previous report can go, as a render that finds
        # nothing must not leave it behind.
        remove_stale_report(document_dir)
        statuses.append(
            render_text_report(
                loaded.data,
                # Options not given are the document's recorded ones.
                resolve_report_options(args, "mdtest", recorded_options(loaded.data)),
                label,
                document_dir,
                groups,
            )
        )
    incomplete = args.require_complete and not all(
        results_json.document_is_complete(loaded.data) for loaded in documents
    )
    status = EXIT_INCOMPLETE if incomplete else 0
    # A document with nothing to report is fine beside ones that have some,
    # and in a batch group whose filters matched nothing.
    if 0 not in statuses and not args.batch_group:
        eprint("No text report produced")
        return status or 1
    return status


def _parse_pool(args):
    """The pool for parse tasks; inline within a batch, which has its own."""
    if args.results_fragment:
        return contextlib.nullcontext(InlineExecutor())
    return report_pool()


def main() -> None:
    """Script entry point."""
    parser = extract_mdtest_elbencho_parser()
    args = parser.parse_args()
    try:
        validate_analysis_arguments(args)
        if args.from_json:
            sys.exit(_report_from_json(parser, args))
        args = resolve_report_options(args, "mdtest")
        validate_json_arguments(args)
        route_batch_report(args, "mdtest", sys.argv[1:])
        comparisons = results_json.load_comparisons(args.comparisons)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    if args.test_parse:
        _test_parse(args.test_parse)
        return

    environments = {
        input_dir: load_env_used_yaml(input_dir)
        for input_dir in unique_inputs(args.input_dirs)
    }
    # Refuse results from different storage targets before parsing or writing.
    target_label = guarded_report_target(
        [
            (input_dir, storage_target_from_env(env))
            for input_dir, env in environments.items()
        ],
        args.allow_mixed_targets,
    )
    try:
        with _parse_pool(args) as pool:
            document = write_json_outputs(args, environments, comparisons, pool)
    except ValueError as error:  # a comparison naming unknown sections
        parser.error(str(error))
    incomplete = (
        args.require_complete
        and not args.results_fragment
        and not results_json.document_is_complete(document)
    )
    status = render_text_report(
        document, args, target_label, args.output_dir or args.input_dirs[0]
    )
    if status:
        eprint("No text report produced; results.json was written")
        # An empty report still fails a plain run; in a batch the group's
        # fragment records why.
        if not args.results_fragment and not incomplete:
            sys.exit(1)
    if incomplete:
        sys.exit(EXIT_INCOMPLETE)


if __name__ == "__main__":
    main()
