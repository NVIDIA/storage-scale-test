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

"""Parse and analyze Warp benchmark results."""

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from collections import defaultdict
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional, Set, Tuple

import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.parse_only_sizes import (  # pylint: disable=wrong-import-position
    parse_only_sizes_arg,
)
from lib.reporting_common import (  # pylint: disable=wrong-import-position
    histogram_axis_ranges,
)
from lib.stdout_report_file import (  # pylint: disable=wrong-import-position
    REPORT_TXT_FILENAME,
    mirror_stdout_to_file,
)

try:
    import zstandard as zstd
except ImportError:
    zstd = None  # Will be checked before use

# File extensions
_JSON_EXT = ".json"
_JSON_ZST_EXT = ".json.zst"
_JSON_EXTENSIONS_TRY_ORDER = (_JSON_ZST_EXT, _JSON_EXT)

# Datestamp patterns / formats
_DATESTAMP_CAPTURE_PATTERN = r"(\d{8}Z\d{6})"
_DATESTAMP_AFTER_UNDERSCORE_PATTERN = "_" + _DATESTAMP_CAPTURE_PATTERN
_DATESTAMP_STRFTIME = "%Y%m%dZ%H%M%S"

_ZST_INSTALL_HINT = (
    "Install with: pip install -r requirements.txt "
    "(or run via ./utils/extract-warp.sh)"
)
# Matplotlib legend locations
_LEGEND_LOC_CENTER_LEFT = "center left"
_LEGEND_LOC_UPPER_RIGHT = "upper right"

# Plot axis labels
_YLABEL_THROUGHPUT_GBPS = "Throughput (Gbps)"
_YLABEL_TTFB_LATENCY_MS = "TTFB Latency (ms)"

# Printed summary table column headers
_COL_MEDLAT_MS = "MedLat (ms)"
_COL_P99LAT_MS = "99%Lat (ms)"
_COL_MAXLAT_MS = "MaxLat (ms)"
_COL_MAXBW_GBPS = "MaxBW (Gbps)"
_COL_MEDBW_GBPS = "MedBW (Gbps)"
_COL_MINBW_GBPS = "MinBW (Gbps)"
_COL_AVGBW_GBPS = "AvgBW (Gbps)"
_COL_AVG_OBJ_S = "Avg obj/s"
_COL_MED_OBJ_S = "Med obj/s"
_WARP_METRICS_TABLE_HEADERS = (
    _COL_MEDLAT_MS,
    _COL_P99LAT_MS,
    _COL_MAXLAT_MS,
    _COL_MAXBW_GBPS,
    _COL_MEDBW_GBPS,
    _COL_MINBW_GBPS,
    _COL_AVGBW_GBPS,
    _COL_AVG_OBJ_S,
    _COL_MED_OBJ_S,
)


@dataclass
class ClientRequestMetrics:
    """Container for per-client request metrics."""

    count: int
    avg_lat_ms: float
    min_lat_ms: float
    max_lat_ms: float
    p50_lat_ms: float
    p90_lat_ms: float
    stddev_lat_ms: float
    ttfb_avg_ms: float
    ttfb_min_ms: float
    ttfb_p25_ms: float
    ttfb_p50_ms: float
    ttfb_p75_ms: float
    ttfb_p90_ms: float
    ttfb_p99_ms: float
    ttfb_max_ms: float
    ttfb_stddev_ms: float


@dataclass
class ClientThroughputMetrics:
    """Container for per-client throughput metrics."""

    mib_per_s_avg: float
    obj_per_s_avg: float
    mib_per_s_max: float
    mib_per_s_p50: float
    mib_per_s_min: float


@dataclass
class WarpMetrics:
    """Container for parsed Warp metrics."""

    nodes: int  # Node count, defaulting to 1 for single-node
    obj_size: str
    threads: int
    med_lat_ms: float
    p99_lat_ms: float
    max_lat_ms: float
    max_bw_mib: float
    med_bw_mib: float
    med_bw_gbps: float
    min_bw_mib: float
    avg_bw_mib: float  # Average bandwidth in MiB/s
    avg_bw_gbps: float  # Average bandwidth in Gbps
    max_bw_gbps: float  # Maximum bandwidth in Gbps
    min_bw_gbps: float  # Minimum bandwidth in Gbps
    avg_rate_obj: float
    med_rate_obj: float
    stddev_bw_mib: float = 0.0  # Standard deviation of bandwidth in MiB/s
    stddev_bw_gbps: float = 0.0  # Standard deviation of bandwidth in Gbps
    window_size: str = "1s"  # Default to 1s if not specified
    requests_by_host: Dict[str, ClientRequestMetrics] = field(default_factory=dict)
    throughput_by_host: Dict[str, ClientThroughputMetrics] = field(default_factory=dict)
    # TTFB histogram data: {latency_ms: count}
    ttfb_histogram: Dict[float, int] = field(default_factory=dict)
    # Per-client TTFB histograms: {client_name: {latency_ms: count}}
    ttfb_histograms_by_client: Dict[str, Dict[float, int]] = field(default_factory=dict)
    # Per-client throughput segments: {client_name: [{"time": str, "bps": float, "ops": float}, ...]}
    # Segment duration is variable (stored in throughput_segment_duration_ms)
    throughput_segments_by_client: Dict[str, List[Dict[str, Any]]] = field(
        default_factory=dict
    )
    # Duration of each throughput segment in milliseconds (extracted from JSON)
    throughput_segment_duration_ms: int = 0
    # Per-client request segments: {client_name: [{"start": str, "end": str, "duration_s": float, "ttfb_median": float, ...}, ...]}
    # Segment duration is variable (calculated from start/end times in each segment)
    request_segments_by_client: Dict[str, List[Dict[str, Any]]] = field(
        default_factory=dict
    )


def parse_size(size_str: str) -> int:
    """Convert size string to bytes, handling MiB/GiB/etc."""
    size_map = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    match = re.match(r"(\d+)([KMGT])iB", size_str)
    if not match:
        raise ValueError(f"Invalid size format: {size_str}")
    num, unit = match.groups()
    return int(num) * size_map[unit]


def format_size(size_bytes: int) -> str:
    """Convert bytes to human-readable size string (e.g., 67108864 -> '64MiB').

    For exact multiples of a unit (e.g., 67108864 bytes = 64 MiB), returns the
    integer representation. For non-exact sizes, returns the value in the largest
    unit where the result is >= 1.0, rounded to the nearest integer.

    Args:
        size_bytes: Size in bytes

    Returns:
        Human-readable size string (e.g., '64MiB', '54MiB', '1GiB')
    """
    units = [("TiB", 1024**4), ("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)]

    for unit_name, unit_size in units:
        if size_bytes >= unit_size:
            # Check if it's an exact multiple
            if size_bytes % unit_size == 0:
                return f"{size_bytes // unit_size}{unit_name}"
            # Otherwise, round to nearest integer
            value = round(size_bytes / unit_size)
            return f"{value}{unit_name}"

    # Fallback to bytes if smaller than 1 KiB
    return f"{size_bytes}B"


def normalize_time(time_str: str) -> float:
    """Convert time string to milliseconds."""
    # Handle '1m33.373s' format (minutes + seconds)
    minute_match = re.match(r"(\d+)m([\d.]+)s", time_str)
    if minute_match:
        minutes = int(minute_match.group(1))
        seconds = float(minute_match.group(2))
        return (minutes * 60 + seconds) * 1000

    if "ms" in time_str:
        return float(time_str.replace("ms", ""))
    return float(time_str.replace("s", "")) * 1000


def normalize_throughput(tp_str: str) -> float:
    """Convert throughput string to MiB/s."""
    match = re.match(r"([\d.]+)([MGT])iB/s", tp_str)
    if not match:
        raise ValueError(f"Invalid throughput format: {tp_str}")
    num, unit = match.groups()
    multiplier = {"M": 1, "G": 1024, "T": 1024**2}
    return float(num) * multiplier[unit]


def parse_throughput_value(tp_str: str) -> float:
    """Parse throughput value (handles values with or without units)."""
    match = re.match(r"([\d.]+)([MGT]iB/s)?", tp_str)
    if not match:
        raise ValueError(f"Invalid throughput value: {tp_str}")
    num = match.group(1)
    return float(num)


def mib_to_gbps(mib_per_sec: float) -> float:
    """Convert MiB/s to Gbps."""
    return (mib_per_sec * 8 * 1024 * 1024) / (1000 * 1000 * 1000)


def extract_client_name(client_str: str) -> str:
    """Extract the hostname from the client string.

    Format: "Client_<hostname>-<random_str>"
    """
    # Remove the "Client_" prefix
    if not client_str.startswith("Client_"):
        return client_str

    name_part = client_str[7:]  # Remove "Client_"

    # The random string at the end is always 8 characters
    # But the hostname can contain dashes, so we need to be careful
    parts = name_part.split("-")
    if len(parts) > 1:
        # Join all parts except the last random string part
        return "-".join(parts[:-1])
    return name_part


def bytes_per_sec_to_mib_per_sec(bytes_per_sec: float) -> float:
    """Convert bytes per second to MiB/s.

    Note: Warp JSON uses 'bps' to mean BYTES per second, not bits.
    """
    return bytes_per_sec / (1024**2)


def format_window_size_from_millis(millis: int) -> str:
    """Format milliseconds as duration string.

    Args:
        millis: Duration in milliseconds (e.g., 1000, 10000)

    Returns:
        Formatted string (e.g., "1s", "10s", "500ms")
    """
    if millis >= 1000 and millis % 1000 == 0:
        return f"{millis // 1000}s"
    return f"{millis}ms"


def open_json_file(filepath: str):
    """Open a JSON file, handling .zst compression if present.

    Args:
        filepath: Path to .json or .json.zst file

    Returns:
        Decoded JSON data (dict)

    Raises:
        ImportError: If zstandard is not installed for .zst files
        FileNotFoundError: If file doesn't exist
    """
    if filepath.endswith(".zst"):
        if zstd is None:
            raise ImportError(
                "zstandard library is required to read .zst files. " + _ZST_INSTALL_HINT
            )
        with open(filepath, "rb") as fh:
            dctx = zstd.ZstdDecompressor()
            with dctx.stream_reader(fh) as reader:
                data = json.loads(reader.read().decode("utf-8"))
                return data
    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


def parse_client_request_metrics(request_block: str) -> ClientRequestMetrics:
    """Parse a client request metrics block."""
    # Extract request count
    count_match = re.search(r"(\d+) requests", request_block)
    count = int(count_match.group(1)) if count_match else 0

    # Extract latency metrics - updated to handle 1m33.373s format
    lat_match = re.search(
        r"Avg:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s)\s+Fastest:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s)\s+"
        r"Slowest:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s)\s+50%:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s)\s+"
        r"90%:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s)\s+StdDev:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s)",
        request_block,
    )

    if not lat_match:
        raise ValueError("Could not find latency metrics in client block")

    avg_lat, min_lat, max_lat, p50_lat, p90_lat, stddev_lat = map(
        normalize_time, lat_match.groups()
    )

    # Extract TTFB metrics - note the First Byte field name in client blocks
    ttfb_match = re.search(
        r"First Byte:\s+Avg:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s),\s+Best:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s),\s+25th:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s),\s+"
        r"Median:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s),\s+75th:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s),\s+90th:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s),\s+"
        r"99th:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s),\s+Worst:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s)\s+StdDev:\s+([\d.]+(?:ms|s)|[\d]+m[\d.]+s)",
        request_block,
    )

    if not ttfb_match:
        raise ValueError("Could not find TTFB metrics in client block")

    (
        ttfb_avg,
        ttfb_min,
        ttfb_p25,
        ttfb_p50,
        ttfb_p75,
        ttfb_p90,
        ttfb_p99,
        ttfb_max,
        ttfb_stddev,
    ) = map(normalize_time, ttfb_match.groups())

    return ClientRequestMetrics(
        count=count,
        avg_lat_ms=round(avg_lat),
        min_lat_ms=round(min_lat),
        max_lat_ms=round(max_lat),
        p50_lat_ms=round(p50_lat),
        p90_lat_ms=round(p90_lat),
        stddev_lat_ms=round(stddev_lat),
        ttfb_avg_ms=round(ttfb_avg),
        ttfb_min_ms=round(ttfb_min),
        ttfb_p25_ms=round(ttfb_p25),
        ttfb_p50_ms=round(ttfb_p50),
        ttfb_p75_ms=round(ttfb_p75),
        ttfb_p90_ms=round(ttfb_p90),
        ttfb_p99_ms=round(ttfb_p99),
        ttfb_max_ms=round(ttfb_max),
        ttfb_stddev_ms=round(ttfb_stddev),
    )


def parse_client_throughput_metrics(throughput_block: str) -> ClientThroughputMetrics:
    """Parse a client throughput metrics block."""
    # Extract average throughput
    avg_match = re.search(
        r"Average:\s+([\d.]+)\s*([MGT]iB)/s,\s+([\d.]+)\s+obj/s", throughput_block
    )

    if not avg_match:
        raise ValueError("Could not find average throughput in client block")

    # Convert average throughput to MiB/s using normalize_throughput
    mib_per_s_avg = normalize_throughput(f"{avg_match.group(1)}{avg_match.group(2)}/s")
    obj_per_s_avg = float(avg_match.group(3))

    # Extract max, median, and min throughput - now handling different units
    max_match = re.search(r"Fastest:\s+([\d.]+)\s*([MGT]iB/s)", throughput_block)
    median_match = re.search(r"50% Median:\s+([\d.]+)\s*([MGT]iB/s)", throughput_block)
    min_match = re.search(r"Slowest:\s+([\d.]+)\s*([MGT]iB/s)", throughput_block)

    if not all([max_match, median_match, min_match]):
        raise ValueError("Could not find throughput metrics in client block")

    # Convert all values to MiB/s
    max_value = normalize_throughput(f"{max_match.group(1)}{max_match.group(2)}")
    median_value = normalize_throughput(
        f"{median_match.group(1)}{median_match.group(2)}"
    )
    min_value = normalize_throughput(f"{min_match.group(1)}{min_match.group(2)}")

    return ClientThroughputMetrics(
        mib_per_s_avg=round(mib_per_s_avg, 2),
        obj_per_s_avg=round(obj_per_s_avg, 2),
        mib_per_s_max=round(max_value, 2),
        mib_per_s_p50=round(median_value, 2),
        mib_per_s_min=round(min_value, 2),
    )


def parse_warp_file(filepath: str) -> Optional[WarpMetrics]:
    """Parse a Warp output file and extract relevant metrics."""
    basename = os.path.basename(filepath)
    if not basename.endswith(".out"):
        return None

    # Match new filename format
    match = re.match(r"warp-GET-(\d+[KMGT]iB)-c_(\d+)-s_(\d+)_", basename)
    if not match:
        return None

    obj_size, node_str, threads = match.groups()
    nodes = int(node_str)
    threads = int(threads)

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    # Find the TTFB metrics in the overall section - also handle minute format
    ttfb_match = re.search(
        r"TTFB:\s+Avg:\s+([\d.]+(?:ms|s)),\s+Best:\s+([\d.]+(?:ms|s)),"
        r"\s+25th:\s+([\d.]+(?:ms|s)),\s+Median:\s+([\d.]+(?:ms|s)),"
        r"\s+75th:\s+([\d.]+(?:ms|s)),\s+90th:\s+([\d.]+(?:ms|s)),"
        r"\s+99th:\s+([\d.]+(?:ms|s)),\s+Worst:\s+([\d.]+(?:ms|s))\s+StdDev:\s+([\d.]+(?:ms|s))",
        content,
    )
    if not ttfb_match:
        raise ValueError("Could not find TTFB metrics")

    # Find the overall average throughput
    avg_tp_match = re.search(
        r"Throughput:\s*\*\s*Average:\s+([\d.]+)\s*([MGT]iB)/s,\s+([\d.]+)\s+obj/s",
        content,
    )
    if not avg_tp_match:
        raise ValueError("Could not find average throughput metrics")

    # Find the split throughput metrics and window size
    split_match = re.search(
        r"Throughput, split into.*?:\s*"
        r"\*\s*Fastest:\s+([\d.]+)\s*([MGT]iB)/s,\s+([\d.]+)\s+obj/s\s*\((\d+[smh])",
        content,
        re.DOTALL,
    )
    if not split_match:
        raise ValueError("Could not find split throughput metrics")

    # Extract the window size from the fastest throughput entry
    window_size = split_match.group(4)

    # Continue with median and slowest throughput extraction
    median_match = re.search(
        r"\*\s*50% Median:\s+([\d.]+)\s*([MGT]iB)/s,\s+([\d.]+)\s+obj/s",
        content,
        re.DOTALL,
    )
    slowest_match = re.search(
        r"\*\s*Slowest:\s+([\d.]+)\s*([MGT]iB)/s,\s+([\d.]+)\s+obj/s",
        content,
        re.DOTALL,
    )

    if not median_match or not slowest_match:
        raise ValueError("Could not find complete throughput metrics")

    # Extract median and max latency from TTFB
    _, _, _, ttfb_median, _, _, ttfb_p99, ttfb_max, _ = map(
        normalize_time, ttfb_match.groups()
    )

    # Extract throughput values
    avg_bw_str, avg_bw_unit, avg_obj_str = avg_tp_match.groups()
    max_bw_str, max_bw_unit, _ = split_match.groups()[0:3]
    med_bw_str, med_bw_unit, med_obj_str = median_match.groups()
    min_bw_str, min_bw_unit, _ = slowest_match.groups()

    # Convert to MiB/s, handling GiB->MiB conversion if needed
    unit_multiplier = {"MiB": 1, "GiB": 1024, "TiB": 1024**2}
    avg_bw = float(avg_bw_str) * unit_multiplier[avg_bw_unit]
    max_bw = float(max_bw_str) * unit_multiplier[max_bw_unit]
    med_bw = float(med_bw_str) * unit_multiplier[med_bw_unit]
    min_bw = float(min_bw_str) * unit_multiplier[min_bw_unit]
    avg_obj = float(avg_obj_str)
    med_obj = float(med_obj_str)

    # Initialize the WarpMetrics object
    metrics = WarpMetrics(
        nodes=nodes,
        obj_size=obj_size,
        threads=threads,
        med_lat_ms=round(ttfb_median),
        p99_lat_ms=round(ttfb_p99),
        max_lat_ms=round(ttfb_max),
        max_bw_mib=round(max_bw),
        med_bw_mib=round(med_bw),
        med_bw_gbps=round(mib_to_gbps(med_bw), 2),
        min_bw_mib=round(min_bw),
        avg_bw_mib=round(avg_bw),
        avg_bw_gbps=round(mib_to_gbps(avg_bw), 2),
        max_bw_gbps=round(mib_to_gbps(max_bw), 2),
        min_bw_gbps=round(mib_to_gbps(min_bw), 2),
        avg_rate_obj=round(float(avg_obj)),
        med_rate_obj=round(float(med_obj)),
        window_size=window_size,
    )

    # Parse per-client request metrics
    requests_section = re.search(
        r"Requests by host:(.*)(?:Throughput:|$)", content, re.DOTALL
    )

    client_request_metrics = []

    if requests_section:
        client_blocks = re.finditer(
            r"\s*\*\s*(Client_[^-]+-.*-[^\s]+)\s*-\s*(.*)(?=\s*(?:\*|$))",
            requests_section.group(1),
            re.DOTALL,
        )

        for block in client_blocks:
            try:
                client_str = block.group(1)
                client_name = extract_client_name(client_str)
                client_metrics = parse_client_request_metrics(block.group(0))
                metrics.requests_by_host[client_name] = client_metrics
                client_request_metrics.append(client_metrics)
            except ValueError as e:
                # Log the error but continue processing other clients
                print(f"Warning: Error parsing client block for {client_str}: {e}")

    # Parse per-client throughput metrics
    throughput_section = re.search(
        r"Throughput by host:(.*)(?=(?:Throughput, split|$))", content, re.DOTALL
    )

    client_throughput_metrics = []

    if throughput_section:
        client_blocks = re.finditer(
            r"\s*\*\s*(Client_[^-]+-.*-[^\s]+):(.*)(?=\s*(?:\*|$))",
            throughput_section.group(1),
            re.DOTALL,
        )

        for block in client_blocks:
            try:
                client_str = block.group(1)
                client_name = extract_client_name(client_str)
                client_metrics = parse_client_throughput_metrics(block.group(2))
                metrics.throughput_by_host[client_name] = client_metrics
                client_throughput_metrics.append(client_metrics)
            except ValueError as e:
                # Log the error but continue processing other clients
                print(f"Warning: Error parsing throughput block for {client_str}: {e}")

    return metrics


def aggregate_histograms(histograms: list[list]) -> dict[float, int]:
    """Aggregate multiple TTFB histograms into a single histogram.

    Args:
        histograms: List of histogram arrays, each containing bucket objects
                   with "millis" and "n" fields

    Returns:
        Dictionary mapping millis -> cumulative count
    """
    combined = {}
    for hist_array in histograms:
        if not hist_array:
            continue
        for bucket in hist_array:
            millis = bucket.get("millis", 0)
            count = bucket.get("n", 0)
            combined[millis] = combined.get(millis, 0) + count
    return combined


def compute_percentile_from_histogram(
    histogram: dict[float, int], percentile: float
) -> float:
    """Compute a percentile from a histogram.

    Args:
        histogram: Dictionary mapping millis -> count
        percentile: Percentile to compute (0.0 to 1.0, e.g., 0.50 for median)

    Returns:
        Percentile value in milliseconds
    """
    if not histogram:
        return 0.0

    # Sort by millis and compute cumulative distribution
    sorted_buckets = sorted(histogram.items())
    total_count = sum(count for _, count in sorted_buckets)

    if total_count == 0:
        return 0.0

    target_count = total_count * percentile
    cumulative = 0

    for millis, count in sorted_buckets:
        cumulative += count
        if cumulative >= target_count:
            return float(millis)

    # Should not reach here, but return the last value if we do
    return float(sorted_buckets[-1][0])


def compute_stats_from_histogram(
    histogram: dict[float, int], sum_millis: float, sum_sq_millis: float
) -> dict[str, float]:
    """Compute statistical metrics from histogram data.

    Args:
        histogram: Dictionary mapping millis -> count
        sum_millis: Sum of (value * count) for all histogram buckets
        sum_sq_millis: Sum of (value^2 * count) for all histogram buckets

    Returns:
        Dictionary with keys: median_millis, p25_millis, p75_millis, p90_millis,
        p99_millis, average_millis, std_dev_millis, fastest_millis, slowest_millis
    """
    if not histogram:
        return {
            "median_millis": 0.0,
            "p25_millis": 0.0,
            "p75_millis": 0.0,
            "p90_millis": 0.0,
            "p99_millis": 0.0,
            "average_millis": 0.0,
            "std_dev_millis": 0.0,
            "fastest_millis": 0.0,
            "slowest_millis": 0.0,
        }

    total_count = sum(histogram.values())
    if total_count == 0:
        return {
            "median_millis": 0.0,
            "p25_millis": 0.0,
            "p75_millis": 0.0,
            "p90_millis": 0.0,
            "p99_millis": 0.0,
            "average_millis": 0.0,
            "std_dev_millis": 0.0,
            "fastest_millis": 0.0,
            "slowest_millis": 0.0,
        }

    # Compute percentiles
    median = compute_percentile_from_histogram(histogram, 0.50)
    p25 = compute_percentile_from_histogram(histogram, 0.25)
    p75 = compute_percentile_from_histogram(histogram, 0.75)
    p90 = compute_percentile_from_histogram(histogram, 0.90)
    p99 = compute_percentile_from_histogram(histogram, 0.99)

    # Compute average and standard deviation
    avg = sum_millis / total_count
    variance = (sum_sq_millis / total_count) - (avg * avg)
    stddev = variance**0.5 if variance > 0 else 0.0

    # Min and max
    min_millis = float(min(histogram.keys()))
    max_millis = float(max(histogram.keys()))

    return {
        "median_millis": median,
        "p25_millis": p25,
        "p75_millis": p75,
        "p90_millis": p90,
        "p99_millis": p99,
        "average_millis": avg,
        "std_dev_millis": stddev,
        "fastest_millis": min_millis,
        "slowest_millis": max_millis,
    }


def aggregate_multi_sized_first_byte(multi_sized: dict) -> tuple[Optional[dict], int]:
    """Aggregate first_byte data across all size ranges in multi_sized_requests.

    Args:
        multi_sized: The multi_sized_requests dictionary from Warp JSON

    Returns:
        Tuple of (first_byte dict with aggregated stats or None, total request count)
    """
    if "by_size" not in multi_sized or not multi_sized["by_size"]:
        return (None, 0)

    # Collect all histograms and accumulate sums from all size buckets
    histograms = []
    sum_millis = 0.0
    sum_sq_millis = 0.0
    total_requests = multi_sized.get("requests", 0)

    for size_bucket in multi_sized["by_size"]:
        if "first_byte" not in size_bucket:
            continue

        first_byte = size_bucket["first_byte"]

        # Accumulate pre-computed sums from each size bucket
        if "sum_millis" in first_byte:
            sum_millis += first_byte["sum_millis"]
        if "sum_sq_millis" in first_byte:
            sum_sq_millis += first_byte["sum_sq_millis"]

        # Collect histogram arrays for percentile computation
        if "ttfb_hist" in first_byte and first_byte["ttfb_hist"]:
            histograms.append(first_byte["ttfb_hist"])

    if not histograms:
        return (None, 0)

    # Aggregate all histograms into a single distribution
    combined_histogram = aggregate_histograms(histograms)

    # Compute statistics from combined histogram
    stats = compute_stats_from_histogram(combined_histogram, sum_millis, sum_sq_millis)

    # Convert combined histogram back to ttfb_hist array format
    ttfb_hist_array = [
        {"millis": millis, "n": count}
        for millis, count in sorted(combined_histogram.items())
    ]
    stats["ttfb_hist"] = ttfb_hist_array

    # Return a first_byte-like dictionary with aggregated statistics
    return (stats, total_requests)


def parse_warp_json(filepath: str) -> Optional[WarpMetrics]:
    """Parse a Warp JSON file and extract relevant metrics.

    Args:
        filepath: Path to .json or .json.zst file

    Returns:
        WarpMetrics object or None if parsing fails

    Extracts all metadata (operation type, object size, client count, threads)
    from the JSON content itself, not from the filename. This makes parsing
    robust to arbitrary filenames.
    """
    basename = os.path.basename(filepath)

    # Skip files written by this tool (our own analyzed output)
    if basename.endswith("-analyzed.json.zst") or basename.endswith("-analyzed.json"):
        return None

    # Load and parse JSON
    try:
        data = open_json_file(filepath)
    except (IOError, OSError, json.JSONDecodeError) as e:
        raise ValueError(f"Failed to read/parse JSON file: {e}") from e

    try:
        # Skip files written by this tool - detect by checking JSON structure
        # Our analyzed output has {"version": 1, "datestamp": ..., "metrics": [...]}
        # Warp's output has {"v": 3, "final": true, "total": {...}, ...}
        if isinstance(data, dict) and "version" in data and "metrics" in data:
            return None

        # Verify this is a final Warp benchmark result (not intermediate)
        if not data.get("final", False):
            return None

        # Extract metadata from JSON content
        total = data.get("total")
        if not total:
            raise ValueError("No 'total' section found in JSON")

        # 1. Operation type: Check by_op_type keys (e.g., "GET", "PUT")
        by_op_type = data.get("by_op_type", {})
        operation_types = list(by_op_type.keys())
        if len(operation_types) != 1:
            # We only handle single-operation benchmarks (GET only for now)
            return None
        operation = operation_types[0]
        if operation != "GET":
            # Only process GET operations
            return None

        # Get by_client section for metadata extraction
        by_client = data.get("by_client", {})
        if not by_client:
            raise ValueError("No client data found in JSON")

        # 2. Object size: Extract from per-client segmented request data
        # The obj_size in requests_total is aggregate; we need per-object size
        # Get it from the first client's first segment
        first_client_name = next(iter(by_client.keys()))
        requests_by_client = total.get("requests_by_client", {})
        if not requests_by_client or first_client_name not in requests_by_client:
            raise ValueError("No per-client request data found")

        first_client_segments = requests_by_client[first_client_name]
        if not first_client_segments or not isinstance(first_client_segments, list):
            raise ValueError("No request segments found for first client")

        first_segment = first_client_segments[0]

        # Extract object size from either single_sized or multi_sized requests
        obj_size_bytes = None
        if "single_sized_requests" in first_segment:
            single_sized = first_segment["single_sized_requests"]
            obj_size_bytes = single_sized.get("obj_size")
        elif "multi_sized_requests" in first_segment:
            multi_sized = first_segment["multi_sized_requests"]
            # For multi-sized, use avg_obj_size as representative
            obj_size_bytes = multi_sized.get("avg_obj_size")

        if obj_size_bytes is None:
            raise ValueError("No obj_size found in segment")

        # Convert bytes to human-readable format (e.g., 67108864 -> "64MiB")
        obj_size = format_size(obj_size_bytes)

        # 3. Client count: Number of clients in by_client section
        nodes = len(by_client)

        # 4. Thread count per client: Extract from per-client concurrency
        # Use the first client to determine threads per client
        first_client_data = next(iter(by_client.values()))
        threads = first_client_data.get("concurrency")
        if threads is None:
            raise ValueError("No concurrency found for client")

        # Extract overall TTFB metrics from either single or multi-sized requests
        requests_total = data["total"]["requests_total"]
        first_byte = None

        if "single_sized_requests" in requests_total:
            first_byte = requests_total["single_sized_requests"]["first_byte"]
        elif "multi_sized_requests" in requests_total:
            # For multi-sized, aggregate first_byte data
            multi_sized = requests_total["multi_sized_requests"]
            first_byte, _ = aggregate_multi_sized_first_byte(multi_sized)

        if first_byte is None:
            raise ValueError("No request data found in requests_total")

        med_lat_ms = round(first_byte["median_millis"])
        p99_lat_ms = round(first_byte["p99_millis"])
        max_lat_ms = round(first_byte.get("slowest_millis", 0))

        # Parse overall TTFB histogram if available
        ttfb_histogram = {}
        if "ttfb_hist" in first_byte:
            for bucket in first_byte["ttfb_hist"]:
                # bucket["millis"] is the upper edge, bucket["n"] is the count
                ttfb_histogram[float(bucket["millis"])] = int(bucket["n"])

        # Extract overall average throughput
        throughput = data["total"]["throughput"]
        if throughput["measure_duration_millis"] == 0:
            raise ValueError("Invalid measure_duration_millis: 0")

        avg_bw_mib = round(
            throughput["bytes"]
            / throughput["measure_duration_millis"]
            * 1000
            / (1024**2)
        )
        avg_bw_gbps = round(mib_to_gbps(avg_bw_mib), 2)
        avg_rate_obj = round(
            throughput["ops"] / (throughput["measure_duration_millis"] / 1000.0)
        )

        # Extract split throughput metrics
        segmented = throughput["segmented"]
        max_bw_mib = round(bytes_per_sec_to_mib_per_sec(segmented["fastest_bps"]))
        med_bw_mib = round(bytes_per_sec_to_mib_per_sec(segmented["median_bps"]))
        min_bw_mib = round(bytes_per_sec_to_mib_per_sec(segmented["slowest_bps"]))
        max_bw_gbps = round(mib_to_gbps(max_bw_mib), 2)
        med_bw_gbps = round(mib_to_gbps(med_bw_mib), 2)
        min_bw_gbps = round(mib_to_gbps(min_bw_mib), 2)
        stddev_bw_mib = round(bytes_per_sec_to_mib_per_sec(segmented["stddev_bps"]), 2)
        stddev_bw_gbps = round(mib_to_gbps(stddev_bw_mib), 2)
        med_rate_obj = round(segmented["median_ops"])
        window_size = format_window_size_from_millis(
            segmented["segment_duration_millis"]
        )

        # Create WarpMetrics object
        metrics = WarpMetrics(
            nodes=nodes,
            obj_size=obj_size,
            threads=threads,
            med_lat_ms=med_lat_ms,
            p99_lat_ms=p99_lat_ms,
            max_lat_ms=max_lat_ms,
            max_bw_mib=max_bw_mib,
            med_bw_mib=med_bw_mib,
            med_bw_gbps=med_bw_gbps,
            min_bw_mib=min_bw_mib,
            avg_bw_mib=avg_bw_mib,
            avg_bw_gbps=avg_bw_gbps,
            max_bw_gbps=max_bw_gbps,
            min_bw_gbps=min_bw_gbps,
            stddev_bw_mib=stddev_bw_mib,
            stddev_bw_gbps=stddev_bw_gbps,
            avg_rate_obj=avg_rate_obj,
            med_rate_obj=med_rate_obj,
            window_size=window_size,
            ttfb_histogram=ttfb_histogram,
        )

        # Check if by_client data exists
        if not data.get("by_client"):
            raise ValueError("No client data found in JSON")

        # Parse per-client request metrics
        for client_name in data["by_client"].keys():
            client_data = data["by_client"][client_name]
            client_requests_total = client_data["requests_total"]

            # Handle both single_sized and multi_sized requests
            requests = None
            fb = None

            if "single_sized_requests" in client_requests_total:
                requests = client_requests_total["single_sized_requests"]
                fb = requests["first_byte"]
            elif "multi_sized_requests" in client_requests_total:
                multi_sized = client_requests_total["multi_sized_requests"]
                fb, request_count = aggregate_multi_sized_first_byte(multi_sized)
                # Create a minimal requests dict for compatibility
                requests = {"requests": request_count}

            if requests is None or fb is None:
                continue  # Skip clients without valid request data

            client_request_metrics = ClientRequestMetrics(
                count=requests["requests"],
                avg_lat_ms=round(requests.get("dur_avg_millis", 0)),
                min_lat_ms=round(requests.get("fastest_millis", 0)),
                max_lat_ms=round(requests.get("slowest_millis", 0)),
                p50_lat_ms=round(requests.get("dur_median_millis", 0)),
                p90_lat_ms=round(requests.get("dur_90_millis", 0)),
                stddev_lat_ms=round(requests.get("std_dev_millis", 0)),
                ttfb_avg_ms=round(fb["average_millis"]),
                ttfb_min_ms=round(fb.get("fastest_millis", 0)),
                ttfb_p25_ms=round(fb.get("p25_millis", 0)),
                ttfb_p50_ms=round(fb["median_millis"]),
                ttfb_p75_ms=round(fb.get("p75_millis", 0)),
                ttfb_p90_ms=round(fb.get("p90_millis", 0)),
                ttfb_p99_ms=round(fb.get("p99_millis", 0)),
                ttfb_max_ms=round(fb.get("slowest_millis", 0)),
                ttfb_stddev_ms=round(fb.get("std_dev_millis", 0)),
            )
            metrics.requests_by_host[client_name] = client_request_metrics

        # Parse per-client throughput metrics
        for client_name in data["by_client"].keys():
            client_tp = data["by_client"][client_name]["throughput"]
            client_seg = client_tp["segmented"]

            if client_tp["measure_duration_millis"] == 0:
                raise ValueError(
                    f"Invalid measure_duration_millis for client {client_name}: 0"
                )

            client_throughput_metrics = ClientThroughputMetrics(
                mib_per_s_avg=round(
                    client_tp["bytes"]
                    / client_tp["measure_duration_millis"]
                    * 1000
                    / (1024**2),
                    2,
                ),
                obj_per_s_avg=round(
                    client_tp["ops"] / (client_tp["measure_duration_millis"] / 1000.0),
                    2,
                ),
                mib_per_s_max=round(
                    bytes_per_sec_to_mib_per_sec(client_seg["fastest_bps"]), 2
                ),
                mib_per_s_p50=round(
                    bytes_per_sec_to_mib_per_sec(client_seg["median_bps"]), 2
                ),
                mib_per_s_min=round(
                    bytes_per_sec_to_mib_per_sec(client_seg["slowest_bps"]), 2
                ),
            )
            metrics.throughput_by_host[client_name] = client_throughput_metrics

        # Parse per-client TTFB histograms
        ttfb_histograms_by_client = {}
        for client_name in data["by_client"].keys():
            client_requests_total = data["by_client"][client_name]["requests_total"]
            client_fb = None

            # Handle both single_sized and multi_sized requests
            if "single_sized_requests" in client_requests_total:
                client_requests = client_requests_total["single_sized_requests"]
                client_fb = client_requests["first_byte"]
            elif "multi_sized_requests" in client_requests_total:
                multi_sized = client_requests_total["multi_sized_requests"]
                client_fb, _ = aggregate_multi_sized_first_byte(multi_sized)

            if client_fb is None:
                continue

            client_histogram = {}
            if "ttfb_hist" in client_fb:
                for bucket in client_fb["ttfb_hist"]:
                    client_histogram[float(bucket["millis"])] = int(bucket["n"])

            if client_histogram:
                ttfb_histograms_by_client[client_name] = client_histogram

        # Store per-client histograms in metrics
        metrics.ttfb_histograms_by_client = ttfb_histograms_by_client

        # Parse per-client throughput segments with flexible duration
        throughput_segments_by_client = {}
        throughput_segment_duration_ms = 0
        for client_name in data["total"]["throughput_by_client"].keys():
            client_tp = data["total"]["throughput_by_client"][client_name]
            if "segmented" in client_tp and "segments" in client_tp["segmented"]:
                # Extract segment duration (in milliseconds) from JSON
                if (
                    "segment_duration_millis" in client_tp["segmented"]
                    and throughput_segment_duration_ms == 0
                ):
                    throughput_segment_duration_ms = client_tp["segmented"][
                        "segment_duration_millis"
                    ]

                segments = client_tp["segmented"]["segments"]
                throughput_segments_by_client[client_name] = [
                    {
                        "time": seg["start"],
                        "bps": seg["bytes_per_sec"],
                        "ops": seg["obj_per_sec"],
                    }
                    for seg in segments
                ]

        # Store per-client throughput segments and duration in metrics
        metrics.throughput_segments_by_client = throughput_segments_by_client
        metrics.throughput_segment_duration_ms = throughput_segment_duration_ms

        # Parse per-client request segments with variable duration
        request_segments_by_client = {}
        for client_name in data["total"]["requests_by_client"].keys():
            client_req_segments = data["total"]["requests_by_client"][client_name]
            if isinstance(client_req_segments, list):
                parsed_segments = []
                for seg in client_req_segments:
                    # Handle both single_sized_requests and multi_sized_requests
                    first_byte = None
                    request_count = 0

                    if "single_sized_requests" in seg:
                        # Single size case
                        single_sized = seg["single_sized_requests"]
                        if "first_byte" in single_sized:
                            first_byte = single_sized["first_byte"]
                            request_count = single_sized.get("requests", 0)
                    elif "multi_sized_requests" in seg:
                        # Multi-size case - aggregate across all size ranges
                        multi_sized = seg["multi_sized_requests"]
                        first_byte, request_count = aggregate_multi_sized_first_byte(
                            multi_sized
                        )

                    # Skip if we couldn't find valid first_byte data
                    if first_byte is None:
                        continue

                    # Calculate segment duration from start/end times
                    from datetime import datetime

                    start_time = datetime.fromisoformat(
                        seg["start_time"].replace("Z", "+00:00")
                    )
                    end_time = datetime.fromisoformat(
                        seg["end_time"].replace("Z", "+00:00")
                    )
                    duration_s = (end_time - start_time).total_seconds()

                    parsed_segments.append(
                        {
                            "start": seg["start_time"],
                            "end": seg["end_time"],
                            "duration_s": duration_s,
                            "ttfb_median": first_byte["median_millis"],
                            "ttfb_p25": first_byte["p25_millis"],
                            "ttfb_p75": first_byte["p75_millis"],
                            "ttfb_p90": first_byte["p90_millis"],
                            "ttfb_p99": first_byte["p99_millis"],
                            "ttfb_avg": first_byte["average_millis"],
                            "request_count": request_count,
                        }
                    )
                request_segments_by_client[client_name] = parsed_segments

        # Store per-client request segments in metrics
        metrics.request_segments_by_client = request_segments_by_client

        return metrics

    except KeyError as e:
        raise ValueError(f"Missing required field in JSON: {e}") from e
    except (TypeError, ZeroDivisionError) as e:
        raise ValueError(f"Invalid data in JSON: {e}") from e


def detect_warp_file_format(directory: str) -> str:
    """Detect which format of warp files is present in directory.

    Args:
        directory: Directory to check

    Returns:
        'json' if .json/.json.zst files found (preferred over .out)
        'text' if only .out files found
        'none' if neither found

    Note:
        When benchmark is run with JSON output, .out files may also be present
        but in a different format. We prefer JSON in that case.
        Files are filtered during parsing based on JSON structure, not filename.
    """
    has_json = False
    has_text = False

    for filename in os.listdir(directory):
        if filename.endswith(_JSON_EXT) or filename.endswith(_JSON_ZST_EXT):
            has_json = True
        elif filename.startswith("warp-GET") and filename.endswith(".out"):
            has_text = True

    # Prefer JSON if both are present
    if has_json:
        return "json"
    if has_text:
        return "text"
    return "none"


def get_plot_colors(num_colors: int) -> List[Any]:
    """Get colorblind-friendly color palette based on number of colors needed.

    Args:
        num_colors: Number of distinct colors required

    Returns:
        List of matplotlib colors

    Strategy:
        - ≤10 colors: tableau-colorblind10 (optimal accessibility)
        - 11-20 colors: tab20 (acceptable categorical)
        - >20 colors: cividis (perceptually uniform, colorblind-safe)
    """
    if num_colors <= 10:
        # Use tableau-colorblind10 colors (explicitly designed for colorblind accessibility)
        with plt.style.context("tableau-colorblind10"):
            colorblind_colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
        # Extract only the number of colors we need
        return [colorblind_colors[i] for i in range(num_colors)]
    elif num_colors <= 20:
        # Use tab20 for 11-20 colors (categorical, though not optimal for colorblindness)
        return plt.get_cmap("tab20")(np.linspace(0, 1, num_colors))
    else:
        # Use cividis for many colors (perceptually uniform, colorblind-friendly)
        return plt.get_cmap("cividis")(np.linspace(0, 1, num_colors))


def build_color_mapping(
    plot_combinations: List[Any], colors: List[Any]
) -> Dict[Any, Any]:
    """Build a deterministic mapping from configuration to color.

    Args:
        plot_combinations: List of configurations to plot
            - For multi-node: list of (obj_size, threads) tuples (as "size,threads" strings)
            - For single-node: list of obj_size strings
        colors: List of colors to assign

    Returns:
        Dictionary mapping configuration to color

    Ensures the same configuration always gets the same color across plots.
    """
    color_mapping = {}
    for i, combo in enumerate(plot_combinations):
        color_mapping[combo] = colors[i % len(colors)]
    return color_mapping


def identify_underperforming_clients(
    client_metrics: Dict[str, float],
    z_threshold: float = 2.0,
) -> Dict[str, Any]:
    """Identify statistically underperforming clients.

    Args:
        client_metrics: Dictionary mapping client name to metric value (e.g., throughput)
        z_threshold: Z-score threshold (2.0 = 95% confidence, 3.0 = 99.7%)

    Returns:
        Dictionary with:
            - "underperformers": List of (client_name, value, z_score) tuples, sorted worst-first
            - "normal_stats": Dict with mean, median, stddev, p10, p90, count

    Focuses on underperformers only (clients significantly below mean).
    """
    if not client_metrics:
        return {
            "underperformers": [],
            "normal_stats": {
                "count": 0,
                "mean": 0.0,
                "median": 0.0,
                "stddev": 0.0,
                "p10": 0.0,
                "p90": 0.0,
            },
        }

    values = np.array(list(client_metrics.values()))
    mean = np.mean(values)
    median = np.median(values)
    stddev = np.std(values)

    underperformers = []

    for client, value in client_metrics.items():
        z_score = (value - mean) / stddev if stddev > 0 else 0

        # Only flag underperformers (negative z-score below threshold)
        if z_score < -z_threshold:
            underperformers.append((client, value, z_score))

    # Sort by z-score (most severe first)
    underperformers.sort(key=lambda x: x[2])

    return {
        "underperformers": underperformers,
        "normal_stats": {
            "count": len(client_metrics) - len(underperformers),
            "mean": mean,
            "median": median,
            "stddev": stddev,
            "p10": np.percentile(values, 10),
            "p90": np.percentile(values, 90),
        },
    }


def _collect_client_avg_throughput(metrics: List[WarpMetrics]) -> Dict[str, float]:
    """Collect and average per-client throughput across runs.

    Args:
        metrics: List of metrics for a specific configuration

    Returns:
        Dictionary mapping client name to average throughput (Gbps)
    """
    client_throughput = {}
    for metric in metrics:
        for client_name, client_tp_metrics in metric.throughput_by_host.items():
            avg_gbps = mib_to_gbps(client_tp_metrics.mib_per_s_avg)
            if client_name not in client_throughput:
                client_throughput[client_name] = []
            client_throughput[client_name].append(avg_gbps)

    return {client: np.mean(values) for client, values in client_throughput.items()}


def _setup_plot_from_prepared_data(
    prepared_data: Dict[str, Any],
) -> tuple[
    Any, Dict[Any, Any], List[Any], List[str], List[int], str, str, str
]:  # Returns ax, color_mapping, plot_combinations, markers, x_values, x_label, plot_suffix, window_size
    """Extract common plot setup data and create figure.

    Args:
        prepared_data: Dictionary containing pre-computed plot data

    Returns:
        Tuple of (ax, color_mapping, plot_combinations, markers,
                  x_values, x_label, plot_suffix, window_size)
    """
    plt.figure(figsize=(12, 8))
    ax = plt.gca()

    color_mapping = prepared_data["color_mapping"]
    plot_combinations = prepared_data["plot_combinations"]
    markers = ["o", "^", "s", "D", "v", "<", ">", "p", "h", "8", "X", "P"]

    x_values = prepared_data["x_values"]
    x_label = prepared_data["x_label"]
    plot_suffix = prepared_data["plot_suffix"]
    window_size = prepared_data.get("window_size", "1s")

    return (
        ax,
        color_mapping,
        plot_combinations,
        markers,
        x_values,
        x_label,
        plot_suffix,
        window_size,
    )


def _finalize_and_save_plot(
    ax: Any,
    x_values: List[int],
    x_label: str,
    y_label: str,
    title: str,
    *,
    output_path: str,
    set_ylim_zero: bool = True,
) -> None:
    """Apply common plot finalization and save.

    Args:
        ax: Matplotlib axis object
        x_values: X-axis tick values
        x_label: X-axis label
        y_label: Y-axis label
        title: Plot title
        output_path: Full path to save PNG (keyword-only)
        set_ylim_zero: Whether to set y-axis lower limit to 0 (keyword-only)
    """
    ax.set_xlabel(x_label)
    ax.set_ylabel(y_label)
    ax.set_xticks(x_values)
    ax.set_title(title)
    ax.set_xlim(0.95, max(x_values) * 1.05)
    if set_ylim_zero:
        ax.set_ylim(0, None)

    ax.legend(loc=_LEGEND_LOC_CENTER_LEFT, bbox_to_anchor=(1.05, 0.5))
    plt.tight_layout()
    plt.savefig(output_path, bbox_inches="tight")
    plt.close()


def select_histogram_subplot_values(
    metrics: List[WarpMetrics], is_multi_node: bool, max_subplots: int = 5
) -> List[int]:
    """Select representative scale points for histogram subplots.

    Args:
        metrics: List of WarpMetrics objects
        is_multi_node: Whether this is multi-node data
        max_subplots: Maximum number of subplots to create

    Returns:
        List of node counts (multi-node) or thread counts (single-node).
        Includes min, max, and evenly distributed middle values.
    """
    # Get all unique values from metrics
    if is_multi_node:
        all_values = sorted({m.nodes for m in metrics})
    else:
        all_values = sorted({m.threads for m in metrics})

    # If we have <= max_subplots values, use all of them
    if len(all_values) <= max_subplots:
        return all_values

    # Otherwise, select min, max, and evenly distributed middle values
    min_value = all_values[0]
    max_value = all_values[-1]

    # Select up to 3 values evenly distributed between min and max
    middle_values = all_values[1:-1]
    if middle_values:
        step_size = max(1, len(middle_values) // 3)
        selected_middle = middle_values[::step_size][:3]
        return [min_value] + selected_middle + [max_value]
    else:
        return [min_value, max_value]


def extract_histogram_data(histogram: Dict[float, int]) -> tuple:
    """Convert histogram dict to sorted lists for plotting.

    Args:
        histogram: Dict mapping latency (ms) to count

    Returns:
        Tuple of (x_values, y_values) sorted by latency
    """
    if not histogram:
        return ([], [])

    sorted_items = sorted(histogram.items())
    x_values = [item[0] for item in sorted_items]
    y_values = [item[1] for item in sorted_items]
    return (x_values, y_values)


def calculate_global_axis_ranges(
    metrics: List[WarpMetrics], selected_values: List[int], is_multi_node: bool
) -> Dict[str, float]:
    """Calculate global min/max for X and Y axes across all subplots.

    Args:
        metrics: List of WarpMetrics for specific configuration
        selected_values: Selected node/thread counts for subplots
        is_multi_node: Whether this is multi-node data

    Returns:
        Dict with keys: min_latency, max_latency, min_count, max_count
    """
    axis_series = []
    for metric in metrics:
        scale_value = metric.nodes if is_multi_node else metric.threads
        if scale_value not in selected_values:
            continue

        if not metric.ttfb_histogram:
            continue

        x_values, y_values = extract_histogram_data(metric.ttfb_histogram)
        axis_series.append((x_values, y_values))

    return histogram_axis_ranges(axis_series)


def plot_throughput(
    prepared_data: Dict[str, Any], datestamp: str, output_dir: str
) -> None:
    """Generate throughput-only plot using prepared data."""
    # Setup plot
    (
        ax,
        color_mapping,
        plot_combinations,
        markers,
        x_values,
        x_label,
        plot_suffix,
        window_size,
    ) = _setup_plot_from_prepared_data(prepared_data)

    for i, combo in enumerate(plot_combinations):
        x_data = prepared_data[f"x_data_{i}"]
        med_bw = prepared_data[f"med_bw_{i}"]
        max_bw = prepared_data[f"max_bw_{i}"]
        min_bw = prepared_data[f"min_bw_{i}"]

        # Use pre-computed color mapping for consistency
        color = color_mapping[combo]
        marker = markers[i % len(markers)]

        # Plot median throughput with solid line
        ax.plot(
            x_data,
            med_bw,
            f"-{marker}",
            color=color,
            label=f"{combo} Med",
            markersize=6,
        )

        # Plot max throughput with dashed line, same color
        ax.plot(
            x_data,
            max_bw,
            f"--{marker}",
            color=color,
            label=f"{combo} Max",
            markersize=4,
            alpha=0.7,
        )

        # Plot min throughput with dotted line, same color
        ax.plot(
            x_data,
            min_bw,
            ":",
            color=color,
            label=f"{combo} Min",
            linewidth=1.5,
            alpha=0.7,
        )

    # Finalize and save
    _finalize_and_save_plot(
        ax,
        x_values,
        x_label,
        _YLABEL_THROUGHPUT_GBPS,
        f"Throughput vs. {x_label} (segmented: {window_size} windows)",
        output_path=os.path.join(
            output_dir, f"warp-{plot_suffix}-GET-throughput-{datestamp}.png"
        ),
    )


def plot_latency(
    prepared_data: Dict[str, Any], datestamp: str, output_dir: str
) -> None:
    """Generate TTFB latency-only plot using prepared data."""
    # Setup plot
    (
        ax,
        color_mapping,
        plot_combinations,
        markers,
        x_values,
        x_label,
        plot_suffix,
        _,  # window_size not used for latency plot
    ) = _setup_plot_from_prepared_data(prepared_data)

    for i, combo in enumerate(plot_combinations):
        x_data = prepared_data[f"x_data_{i}"]
        lat = prepared_data[f"lat_{i}"]

        # Use pre-computed color mapping for consistency
        color = color_mapping[combo]
        marker = markers[i % len(markers)]

        # Plot median TTFB latency
        ax.plot(
            x_data,
            lat,
            f"-{marker}",
            color=color,
            label=f"{combo} TTFB",
            linewidth=1,
            markersize=6,
        )

    # Finalize and save
    _finalize_and_save_plot(
        ax,
        x_values,
        x_label,
        _YLABEL_TTFB_LATENCY_MS,
        f"TTFB Latency vs. {x_label} (aggregate across entire run)",
        output_path=os.path.join(
            output_dir, f"warp-{plot_suffix}-GET-latency-{datestamp}.png"
        ),
    )


def plot_tput_scaling(
    prepared_data: Dict[str, Any], datestamp: str, output_dir: str
) -> None:
    """Generate throughput scaling efficiency plot using prepared data."""
    # Setup plot
    (
        ax,
        color_mapping,
        plot_combinations,
        markers,
        x_values,
        x_label,
        plot_suffix,
        window_size,
    ) = _setup_plot_from_prepared_data(prepared_data)

    # Add a reference line for perfect scaling (100%)
    ax.axhline(y=100, color="gray", linestyle="--", alpha=0.7, label="Perfect Scaling")

    for i, combo in enumerate(plot_combinations):
        x_data = prepared_data[f"x_data_{i}"]
        scaling = prepared_data[f"med_tput_scaling_{i}"]

        if not scaling:  # Skip if no scaling data
            continue

        # Use pre-computed color mapping for consistency
        color = color_mapping[combo]
        marker = markers[i % len(markers)]

        # Plot scaling efficiency
        ax.plot(
            x_data,
            scaling,
            f"-{marker}",
            color=color,
            label=f"{combo} Scaling",
            markersize=6,
        )

    # Set custom y-axis limit with some padding above 100%
    ax.set_ylim(
        0,
        max(
            105,
            max(
                (
                    max(prepared_data[f"med_tput_scaling_{i}"] or [0])
                    for i in range(len(plot_combinations))
                ),
                default=105,
            )
            + 5,
        ),
    )

    # Add grid
    ax.grid(True, linestyle="--", alpha=0.7)

    # Finalize and save (with custom y-axis handling)
    ax.set_xlabel(x_label)
    ax.set_ylabel("Scaling Efficiency (%)")
    ax.set_xticks(x_values)
    ax.set_title(
        f"Median BW Scaling Efficiency vs. {x_label} (segmented: {window_size} windows)"
    )
    ax.set_xlim(0.95, max(x_values) * 1.05)
    # Note: ylim already set above with custom logic

    ax.legend(loc=_LEGEND_LOC_CENTER_LEFT, bbox_to_anchor=(1.05, 0.5))
    plt.tight_layout()
    plt.savefig(
        os.path.join(output_dir, f"warp-{plot_suffix}-GET-scaling-{datestamp}.png"),
        bbox_inches="tight",
    )
    plt.close()


def plot_ttfb_histograms(
    metrics: List[WarpMetrics],
    datestamp: str,
    output_dir: str,
    is_multi_node: bool,
) -> None:
    """Generate TTFB latency histogram plots with multiple subplots.

    Args:
        metrics: List of WarpMetrics objects
        datestamp: Timestamp for filename
        output_dir: Output directory for PNG files
        is_multi_node: Whether this is multi-node data

    Creates a multi-subplot figure showing TTFB distribution
    at different scale points (node counts or thread counts).
    Only generates plot if histogram data is available.
    """
    # Check if we have any histogram data
    has_histogram_data = any(m.ttfb_histogram for m in metrics)
    if not has_histogram_data:
        print("No histogram data available, skipping histogram plots")
        return

    # Select representative values for subplots
    selected_values = select_histogram_subplot_values(metrics, is_multi_node)
    if not selected_values:
        print("No valid scale points for histogram plotting")
        return

    # Calculate global axis ranges for consistent scaling
    axis_ranges = calculate_global_axis_ranges(metrics, selected_values, is_multi_node)

    # Group metrics by obj_size only (combine all thread counts in one plot)
    config_groups = {}
    for metric in metrics:
        obj_size = metric.obj_size
        config_groups.setdefault(obj_size, []).append(metric)

    # Generate histogram plot for each object size
    for obj_size, config_metrics in config_groups.items():
        # Filter metrics with histogram data at selected values
        valid_metrics = [
            m
            for m in config_metrics
            if m.ttfb_histogram
            and (m.nodes if is_multi_node else m.threads) in selected_values
        ]

        if not valid_metrics:
            continue

        # Configuration label is just the object size
        config_label = obj_size

        # Create figure with subplots
        num_plots = len(selected_values)
        fig_width = 10
        fig_height = min(20, max(8, 2.5 * num_plots + 1.5))

        fig, axes = plt.subplots(
            num_plots, 1, figsize=(fig_width, fig_height), constrained_layout=True
        )

        # Handle single subplot case
        if num_plots == 1:
            axes = [axes]

        # Add figure title
        x_label = "Nodes" if is_multi_node else "Threads"
        fig.suptitle(
            f"TTFB Latency Histogram - {config_label} GET - {datestamp}",
            fontsize=14,
        )

        # Get unique thread counts and assign colors
        # Each thread count gets a distinct color
        unique_threads = sorted({m.threads for m in valid_metrics})
        num_colors = len(unique_threads)
        colors = get_plot_colors(num_colors)
        thread_to_color = {thread: colors[i] for i, thread in enumerate(unique_threads)}

        # Plot each subplot
        for plot_idx, scale_value in enumerate(selected_values):
            ax = axes[plot_idx]

            # Get metrics for this scale value
            scale_metrics = [
                m
                for m in valid_metrics
                if (m.nodes if is_multi_node else m.threads) == scale_value
            ]

            if not scale_metrics:
                ax.set_visible(False)
                continue

            # Sort metrics by thread count for consistent legend ordering
            scale_metrics = sorted(scale_metrics, key=lambda m: m.threads)

            # Plot histogram for each metric at this scale value
            for metric in scale_metrics:
                x_values, y_values = extract_histogram_data(metric.ttfb_histogram)

                if not x_values:
                    continue

                # Get color based on thread count
                color = thread_to_color[metric.threads]

                # Plot histogram line
                label = f"{metric.threads}t"
                ax.plot(
                    x_values, y_values, "-o", markersize=3, label=label, color=color
                )

                # Add semi-transparent fill for first 3 thread counts
                thread_idx = unique_threads.index(metric.threads)
                if thread_idx < 3:
                    ax.fill_between(x_values, y_values, alpha=0.2, color=color)

                # Add percentile markers (p50 and p99)
                if metric.med_lat_ms > 0:
                    # 50% percentile
                    p50_label = f"{metric.threads}t 50%: {metric.med_lat_ms}ms"
                    ax.axvline(
                        x=metric.med_lat_ms,
                        linestyle="--",
                        alpha=0.8,
                        color=color,
                        linewidth=1.2,
                        label=p50_label,
                    )

                    # Add text label at 20% of plot height (in log space)
                    label_y = (
                        axis_ranges["min_count"]
                        * (axis_ranges["max_count"] / axis_ranges["min_count"]) ** 0.2
                    )
                    ax.text(
                        metric.med_lat_ms,
                        label_y,
                        "50%",
                        rotation=90,
                        verticalalignment="bottom",
                        horizontalalignment="right",
                        fontsize=7,
                        color=color,
                        fontweight="bold",
                    )

                if metric.p99_lat_ms > 0:
                    # 99% percentile
                    p99_label = f"{metric.threads}t 99%: {metric.p99_lat_ms}ms"
                    ax.axvline(
                        x=metric.p99_lat_ms,
                        linestyle=":",
                        alpha=0.8,
                        color=color,
                        linewidth=1.5,
                        label=p99_label,
                    )

                    # Add text label at 30% of plot height (in log space)
                    label_y = (
                        axis_ranges["min_count"]
                        * (axis_ranges["max_count"] / axis_ranges["min_count"]) ** 0.3
                    )
                    ax.text(
                        metric.p99_lat_ms,
                        label_y,
                        "99%",
                        rotation=90,
                        verticalalignment="bottom",
                        horizontalalignment="right",
                        fontsize=7,
                        color=color,
                        fontweight="bold",
                    )

            # Set log scales
            ax.set_xscale("log")
            ax.set_yscale("log")

            # Set axis limits
            ax.set_xlim(axis_ranges["min_latency"], axis_ranges["max_latency"])
            ax.set_ylim(axis_ranges["min_count"], axis_ranges["max_count"])

            # Format x-axis (latency) ticks with integer labels
            # Generate evenly spaced ticks in log space within data range
            log_min = np.log10(axis_ranges["min_latency"])
            log_max = np.log10(axis_ranges["max_latency"])
            log_steps = np.arange(np.floor(log_min), np.ceil(log_max) + 1)
            x_ticks = 10**log_steps
            # Filter to only include ticks within our range
            x_ticks = [
                tick
                for tick in x_ticks
                if axis_ranges["min_latency"] <= tick <= axis_ranges["max_latency"]
            ]
            x_ticklabels = [f"{int(tick)}" for tick in x_ticks]
            ax.set_xticks(x_ticks)
            ax.set_xticklabels(x_ticklabels, rotation=45, ha="right", fontsize=8)

            # Format y-axis (count) with comma separators
            ax.yaxis.set_major_formatter(
                FuncFormatter(lambda y, _: f"{int(y):,}" if y >= 1 else f"{y:.1f}")
            )

            # Labels and title
            ax.set_ylabel("Count")
            if plot_idx == num_plots - 1:
                ax.set_xlabel("Latency (ms)")

            value_label = f"{scale_value} {x_label}"
            ax.set_title(f"GET - {value_label}")

            # Add legend
            ax.legend(loc=_LEGEND_LOC_UPPER_RIGHT, fontsize=8)

            # Add grid
            ax.grid(True, alpha=0.3)

        # Save figure
        plot_suffix = "mn" if is_multi_node else "sn"
        filename = (
            f"warp-{plot_suffix}-GET-{config_label}-ttfb-histogram-{datestamp}.png"
        )
        plt.savefig(os.path.join(output_dir, filename), bbox_inches="tight", dpi=150)
        plt.close()
        print(f"Generated histogram plot: {filename}")


def plot_client_throughput_distribution(
    metrics: List[WarpMetrics],
    datestamp: str,
    output_dir: str,
    config_label: str,
    z_threshold: float = 2.0,
) -> None:
    """Generate client throughput distribution histogram (PRIMARY PLOT).

    Shows clustering quality and identifies underperformers.

    Args:
        metrics: List of metrics for a specific configuration
        datestamp: Timestamp for filename
        output_dir: Directory to save plot
        config_label: Label like "4MiB-64t" for title/filename
        z_threshold: Z-score threshold for underperformer detection
    """
    # Collect and average per-client throughput across runs
    client_avg_throughput = _collect_client_avg_throughput(metrics)

    if not client_avg_throughput:
        print(f"No per-client data for {config_label}, skipping distribution histogram")
        return

    # Identify underperformers
    analysis = identify_underperforming_clients(client_avg_throughput, z_threshold)
    underperformers = analysis["underperformers"]
    stats = analysis["normal_stats"]

    # Calculate coefficient of variation (clustering metric)
    cv = (stats["stddev"] / stats["mean"] * 100) if stats["mean"] > 0 else 0

    # Create histogram
    plt.figure(figsize=(12, 8))
    ax = plt.gca()

    # Histogram of all client throughputs
    all_values = list(client_avg_throughput.values())
    num_bins = min(50, max(20, len(all_values) // 10))
    _ = ax.hist(
        all_values, bins=num_bins, alpha=0.7, color="steelblue", edgecolor="black"
    )

    # Add vertical lines for mean and median
    ax.axvline(stats["mean"], color="green", linestyle="--", linewidth=2, label="Mean")
    ax.axvline(
        stats["median"], color="orange", linestyle="--", linewidth=2, label="Median"
    )

    # Shade ±1σ band
    if stats["stddev"] > 0:
        ax.axvspan(
            stats["mean"] - stats["stddev"],
            stats["mean"] + stats["stddev"],
            alpha=0.2,
            color="green",
            label="±1σ (normal range)",
        )

    # Mark underperformers with vertical lines
    max_labels = 10  # Limit labels to avoid clutter
    for i, (client, value, _) in enumerate(underperformers[:max_labels]):
        ax.axvline(value, color="red", linestyle=":", linewidth=1, alpha=0.7)
        # Add label with client name
        y_pos = ax.get_ylim()[1] * 0.9 * (1 - 0.05 * (i % 10))
        ax.text(
            value,
            y_pos,
            f"{client[:20]}",
            rotation=90,
            va="top",
            ha="right",
            fontsize=7,
            color="red",
        )

    # Labels and title
    ax.set_xlabel(_YLABEL_THROUGHPUT_GBPS)
    ax.set_ylabel("Number of Clients")

    # Clustering quality assessment
    if cv < 5:
        clustering_quality = "tight cluster"
    elif cv < 10:
        clustering_quality = "moderate cluster"
    else:
        clustering_quality = "wide smear"

    title = (
        f"Client Throughput Distribution - {len(client_avg_throughput)} clients\n"
        f"Mean: {stats['mean']:.2f} Gbps, StdDev: {stats['stddev']:.2f} Gbps, "
        f"CV: {cv:.1f}% ({clustering_quality})"
    )
    if underperformers:
        title += f"\n{len(underperformers)} underperformers (z < {-z_threshold:.1f}) marked in red"
    ax.set_title(title)

    # Add text box with statistics
    textstr = (
        f"Median: {stats['median']:.2f} Gbps\n"
        f"p10-p90: {stats['p10']:.2f}-{stats['p90']:.2f} Gbps\n"
        f"Normal clients: {stats['count']}"
    )
    props = {"boxstyle": "round", "facecolor": "wheat", "alpha": 0.5}
    ax.text(
        0.02,
        0.98,
        textstr,
        transform=ax.transAxes,
        fontsize=9,
        verticalalignment="top",
        bbox=props,
    )

    ax.legend(loc=_LEGEND_LOC_UPPER_RIGHT, fontsize=9)
    ax.grid(axis="y", alpha=0.3)

    # Save figure
    plot_suffix = "mn" if len({m.nodes for m in metrics}) > 1 else "sn"
    filename = (
        f"warp-{plot_suffix}-GET-{config_label}-client-distribution-{datestamp}.png"
    )
    plt.savefig(os.path.join(output_dir, filename), bbox_inches="tight", dpi=150)
    plt.close()
    print(f"Generated client distribution histogram: {filename}")


def plot_underperforming_clients_bars(
    metrics: List[WarpMetrics],
    datestamp: str,
    output_dir: str,
    config_label: str,
    z_threshold: float = 2.0,
) -> None:
    """Generate bar chart of underperforming clients.

    Args:
        metrics: List of metrics for a specific configuration
        datestamp: Timestamp for filename
        output_dir: Directory to save plot
        config_label: Label like "4MiB-64t" for title/filename
        z_threshold: Z-score threshold for underperformer detection
    """
    # Collect and average per-client throughput across runs
    client_avg_throughput = _collect_client_avg_throughput(metrics)

    if not client_avg_throughput:
        print(
            f"No per-client data for {config_label}, skipping underperformer bar chart"
        )
        return

    # Identify underperformers
    analysis = identify_underperforming_clients(client_avg_throughput, z_threshold)
    underperformers = analysis["underperformers"]
    stats = analysis["normal_stats"]

    if not underperformers:
        print(
            f"No underperforming clients detected for {config_label} "
            f"(z_threshold={z_threshold}), skipping bar chart"
        )
        return

    # Limit to worst 50 if too many
    display_underperformers = underperformers[:50]

    # Create horizontal bar chart
    fig_height = max(6, len(display_underperformers) * 0.3)
    plt.figure(figsize=(10, fig_height))
    ax = plt.gca()

    # Extract data for plotting
    clients = [client for client, _, _ in display_underperformers]
    throughputs = [value for _, value, _ in display_underperformers]
    z_scores = [z_score for _, _, z_score in display_underperformers]

    # Color gradient (darker red = worse)
    colors = plt.get_cmap("Reds")(np.linspace(0.4, 0.9, len(clients)))

    # Plot bars
    y_pos = np.arange(len(clients))
    bars = ax.barh(y_pos, throughputs, color=colors, edgecolor="black", linewidth=0.5)

    # Annotate with z-scores
    for _, (rect, z_score) in enumerate(zip(bars, z_scores)):
        width = rect.get_width()
        ax.text(
            width,
            rect.get_y() + rect.get_height() / 2,
            f" z={z_score:.1f}",
            va="center",
            fontsize=8,
            color="darkred",
            fontweight="bold",
        )

    # Add vertical line for mean
    ax.axvline(stats["mean"], color="green", linestyle="--", linewidth=2, alpha=0.7)

    # Labels and title
    ax.set_yticks(y_pos)
    ax.set_yticklabels(clients, fontsize=8)
    ax.set_xlabel(_YLABEL_THROUGHPUT_GBPS)
    ax.set_title(
        f"Underperforming Clients (z < {-z_threshold:.1f}) - "
        f"{len(underperformers)} of {len(client_avg_throughput)} clients\n"
        f"Median (all clients): {stats['median']:.2f} Gbps"
    )
    ax.grid(axis="x", alpha=0.3)

    # Add legend
    ax.text(
        0.98,
        0.02,
        f"Green line = mean ({stats['mean']:.2f} Gbps)",
        transform=ax.transAxes,
        fontsize=9,
        verticalalignment="bottom",
        horizontalalignment="right",
        bbox={"boxstyle": "round", "facecolor": "wheat", "alpha": 0.5},
    )

    plt.tight_layout()

    # Save figure
    plot_suffix = "mn" if len({m.nodes for m in metrics}) > 1 else "sn"
    filename = f"warp-{plot_suffix}-GET-{config_label}-underperformers-{datestamp}.png"
    plt.savefig(os.path.join(output_dir, filename), bbox_inches="tight", dpi=150)
    plt.close()
    print(f"Generated underperformer bar chart: {filename}")


def _collect_ttfb_underperformer_metrics(
    metrics: List[WarpMetrics],
    underperformer_names: List[str],
) -> Tuple[Dict[str, List[Dict[str, float]]], List[float]]:
    """Gather TTFB percentile rows for underperformers and all clients' p50 list."""
    client_ttfb_data: Dict[str, List[Dict[str, float]]] = {
        client: [] for client in underperformer_names
    }
    all_client_ttfb_p50: List[float] = []
    for metric in metrics:
        for client_name, client_req_metrics in metric.requests_by_host.items():
            all_client_ttfb_p50.append(client_req_metrics.ttfb_p50_ms)
            if client_name not in client_ttfb_data:
                continue
            client_ttfb_data[client_name].append(
                {
                    "p10": client_req_metrics.ttfb_min_ms,
                    "p25": client_req_metrics.ttfb_p25_ms,
                    "p50": client_req_metrics.ttfb_p50_ms,
                    "p75": client_req_metrics.ttfb_p75_ms,
                    "p90": client_req_metrics.ttfb_p90_ms,
                }
            )
    return client_ttfb_data, all_client_ttfb_p50


def _build_ttfb_boxplot_data_and_labels(
    underperformer_names: List[str],
    client_ttfb_data: Dict[str, List[Dict[str, float]]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Build bxp dicts and x tick labels from per-client TTFB run lists."""
    box_data: List[Dict[str, Any]] = []
    labels: List[str] = []
    for client in underperformer_names:
        runs = client_ttfb_data[client]
        if not runs:
            continue
        avg_p25 = np.mean([r["p25"] for r in runs])
        avg_p50 = np.mean([r["p50"] for r in runs])
        avg_p75 = np.mean([r["p75"] for r in runs])
        avg_p10 = np.mean([r["p10"] for r in runs])
        avg_p90 = np.mean([r["p90"] for r in runs])
        box_data.append(
            {
                "whislo": avg_p10,
                "q1": avg_p25,
                "med": avg_p50,
                "q3": avg_p75,
                "whishi": avg_p90,
                "fliers": [],
            }
        )
        labels.append(client[:25])
    return box_data, labels


def plot_underperforming_clients_ttfb_boxplot(
    metrics: List[WarpMetrics],
    datestamp: str,
    output_dir: str,
    config_label: str,
    z_threshold: float = 2.0,
) -> None:
    """Generate box plot of TTFB latency for underperforming clients.

    Args:
        metrics: List of metrics for a specific configuration
        datestamp: Timestamp for filename
        output_dir: Directory to save plot
        config_label: Label like "4MiB-64t" for title/filename
        z_threshold: Z-score threshold for underperformer detection
    """
    # First, identify underperformers based on throughput
    client_avg_throughput = _collect_client_avg_throughput(metrics)

    if not client_avg_throughput:
        print(f"No per-client data for {config_label}, skipping TTFB box plot")
        return

    analysis = identify_underperforming_clients(client_avg_throughput, z_threshold)
    underperformers = analysis["underperformers"]

    if not underperformers:
        print(
            f"No underperforming clients detected for {config_label} "
            f"(z_threshold={z_threshold}), skipping TTFB box plot"
        )
        return

    # Limit to worst 20
    underperformer_names = [client for client, _, _ in underperformers[:20]]

    client_ttfb_data, all_client_ttfb_p50 = _collect_ttfb_underperformer_metrics(
        metrics, underperformer_names
    )

    # Calculate normal client statistics
    if all_client_ttfb_p50:
        normal_p10 = np.percentile(all_client_ttfb_p50, 10)
        normal_p50 = np.percentile(all_client_ttfb_p50, 50)
        normal_p90 = np.percentile(all_client_ttfb_p50, 90)
    else:
        normal_p10 = normal_p50 = normal_p90 = 0

    # Create box plot
    fig_width = max(10, len(underperformer_names) * 0.6)
    plt.figure(figsize=(fig_width, 8))
    ax = plt.gca()

    box_data, labels = _build_ttfb_boxplot_data_and_labels(
        underperformer_names, client_ttfb_data
    )

    if not box_data:
        print(f"No TTFB data for underperformers in {config_label}, skipping box plot")
        return

    # Plot boxes
    bp = ax.bxp(
        box_data,
        positions=range(len(box_data)),
        widths=0.6,
        patch_artist=True,
        showfliers=False,
    )

    # Color boxes red
    for patch in bp["boxes"]:
        patch.set_facecolor("lightcoral")
        patch.set_edgecolor("darkred")
        patch.set_linewidth(1.5)

    # Add horizontal reference lines for normal clients
    ax.axhline(normal_p50, color="green", linestyle="--", linewidth=1, alpha=0.7)
    ax.axhspan(normal_p10, normal_p90, alpha=0.1, color="green")

    # Labels and title
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel(_YLABEL_TTFB_LATENCY_MS)
    ax.set_title(
        f"Per-Client TTFB Latency - Underperformers Only\n"
        f"Showing {len(underperformer_names)} underperformers, "
        f"normal clients: p50={normal_p50:.1f}ms (range: {normal_p10:.1f}-{normal_p90:.1f}ms)"
    )
    ax.grid(axis="y", alpha=0.3)

    # Add legend
    ax.text(
        0.02,
        0.98,
        "Green line = normal p50\nGreen band = normal p10-p90",
        transform=ax.transAxes,
        fontsize=9,
        verticalalignment="top",
        bbox={"boxstyle": "round", "facecolor": "wheat", "alpha": 0.5},
    )

    plt.tight_layout()

    # Save figure
    plot_suffix = "mn" if len({m.nodes for m in metrics}) > 1 else "sn"
    filename = f"warp-{plot_suffix}-GET-{config_label}-ttfb-boxplot-{datestamp}.png"
    plt.savefig(os.path.join(output_dir, filename), bbox_inches="tight", dpi=150)
    plt.close()
    print(f"Generated TTFB box plot: {filename}")


def plot_per_client_metrics(
    metrics: List[WarpMetrics],
    datestamp: str,
    output_dir: str,
    is_multi_node: bool,
    outlier_threshold: float = 2.0,
    min_segments: int = 1,
) -> None:
    """Generate per-client comparison plots for multi-node runs.

    Identifies underperforming clients and visualizes performance clustering.

    Args:
        metrics: List of WarpMetrics objects
        datestamp: Timestamp for output files
        output_dir: Directory to save plots
        is_multi_node: Whether this is multi-node data
        outlier_threshold: Z-score threshold for underperformer detection
        min_segments: Minimum segments underperforming to flag in heatmap

    Only generates plots for multi-node runs where per-client comparison is meaningful.
    Generates three types of analysis:
    1. Per-node-count fairness plots (load balancing within each scale)
    2. Per-node-count temporal heatmaps (segment-level underperformance)
    3. Cross-run persistent underperformer analysis (bad hardware detection)
    """
    # Skip if single-node (only one client)
    if not is_multi_node:
        print("Skipping per-client plots (single-node run)")
        return

    # ========================================
    # Analysis 1: Per-node-count fairness
    # ========================================
    # Group by configuration: (obj_size, threads, nodes)
    # Note: We include nodes because per-client analysis is specific to a particular
    # node count - mixing 2-node and 3-node runs would show 5 different clients
    config_groups = defaultdict(list)
    for m in metrics:
        config_key = (m.obj_size, m.threads, m.nodes)
        config_groups[config_key].append(m)

    # Generate plots for each configuration
    for (obj_size, threads, nodes), config_metrics in config_groups.items():
        config_label = f"{obj_size}-{threads}t-{nodes}n"

        # Skip single-node configurations - per-client analysis is meaningless with only one client
        if nodes == 1:
            print(
                f"Skipping per-client plots for {config_label} (single-node, per-client analysis not applicable)"
            )
            continue

        # Check if we have per-client data
        if not any(m.throughput_by_host for m in config_metrics):
            print(f"No per-client data for {config_label}, skipping")
            continue

        print(f"\nGenerating per-client fairness plots for {config_label}...")

        # Generate the three core fairness plots
        plot_client_throughput_distribution(
            config_metrics, datestamp, output_dir, config_label, outlier_threshold
        )
        plot_underperforming_clients_bars(
            config_metrics, datestamp, output_dir, config_label, outlier_threshold
        )
        plot_underperforming_clients_ttfb_boxplot(
            config_metrics, datestamp, output_dir, config_label, outlier_threshold
        )

        # Generate segment-level temporal heatmap (if segment data available)
        plot_client_underperformance_heatmap(
            config_metrics,
            datestamp,
            output_dir,
            config_label,
            outlier_threshold,
            min_segments,
        )

        # Generate throughput time-series plot (if segment data available)
        plot_client_throughput_timeseries(
            config_metrics, datestamp, output_dir, config_label, outlier_threshold
        )

        # Generate TTFB percentile comparison plot
        plot_client_ttfb_percentile_lines(
            config_metrics, datestamp, output_dir, config_label, outlier_threshold
        )

    # ========================================
    # Analysis 2: Cross-run persistent underperformer detection
    # ========================================
    print("\nAnalyzing persistent underperformers across all runs...")
    plot_persistent_underperformers(metrics, datestamp, output_dir, outlier_threshold)


def _z_confidence_percent_label(z_threshold: float) -> str:
    """Return confidence percent label for common z-score thresholds (report text)."""
    if math.isclose(z_threshold, 2.0, rel_tol=0.0, abs_tol=1e-9):
        return "95"
    if math.isclose(z_threshold, 3.0, rel_tol=0.0, abs_tol=1e-9):
        return "99.7"
    return "?"


def plot_persistent_underperformers(
    metrics: List[WarpMetrics],
    datestamp: str,
    output_dir: str,
    z_threshold: float = 2.0,
) -> None:
    """Identify clients that consistently underperform across multiple runs.

    This analysis aggregates across all multi-node runs to find clients with
    persistent hardware/network issues. Single-node runs are excluded as per-client
    analysis is not meaningful with only one client.

    Args:
        metrics: List of all WarpMetrics objects
        datestamp: Timestamp for filename
        output_dir: Directory to save plots
        z_threshold: Z-score threshold for underperformer detection
    """
    # Track per-client statistics across all runs
    client_underperformance_count = defaultdict(int)
    client_total_runs = defaultdict(int)
    client_avg_z_scores = defaultdict(list)
    client_run_details = defaultdict(list)  # Track which runs had issues

    # Analyze each run independently (skip single-node runs)
    for metric in metrics:
        # Skip single-node runs - per-client analysis not meaningful
        if metric.nodes == 1:
            continue

        if not metric.throughput_by_host:
            continue

        # Get throughput for all clients in this run
        run_throughputs = {
            client: mib_to_gbps(tp.mib_per_s_avg)
            for client, tp in metric.throughput_by_host.items()
        }

        if not run_throughputs:
            continue

        # Identify underperformers in this specific run
        analysis = identify_underperforming_clients(run_throughputs, z_threshold)
        underperformers = analysis["underperformers"]

        # Track participation and underperformance for each client
        for client in run_throughputs.keys():
            client_total_runs[client] += 1

        for client, value, z_score in underperformers:
            client_underperformance_count[client] += 1
            client_avg_z_scores[client].append(z_score)
            client_run_details[client].append(
                {
                    "obj_size": metric.obj_size,
                    "threads": metric.threads,
                    "nodes": metric.nodes,
                    "z_score": z_score,
                    "throughput": value,
                }
            )

    # Calculate underperformance rates
    client_stats = []
    for client, total_runs in client_total_runs.items():
        underperf_count = client_underperformance_count[client]
        underperf_rate = (underperf_count / total_runs * 100) if total_runs > 0 else 0
        avg_z_score = (
            np.mean(client_avg_z_scores[client]) if client_avg_z_scores[client] else 0.0
        )

        client_stats.append(
            {
                "client": client,
                "underperf_count": underperf_count,
                "total_runs": total_runs,
                "underperf_rate": underperf_rate,
                "avg_z_score": avg_z_score,
            }
        )

    # Sort by underperformance rate (descending)
    client_stats.sort(key=lambda x: x["underperf_rate"], reverse=True)

    # Filter to only clients that underperformed in at least one run
    problematic_clients = [c for c in client_stats if c["underperf_count"] > 0]

    if not problematic_clients:
        print("No persistent underperformers detected across all runs")
        return

    # ========================================
    # Generate plot
    # ========================================
    fig_height = max(6, len(problematic_clients) * 0.4)
    plt.figure(figsize=(12, fig_height))
    ax = plt.gca()

    # Extract data for plotting
    clients = [c["client"] for c in problematic_clients]
    underperf_rates = [c["underperf_rate"] for c in problematic_clients]
    underperf_counts = [c["underperf_count"] for c in problematic_clients]
    total_runs = [c["total_runs"] for c in problematic_clients]

    # Color code: red gradient based on severity
    colors = plt.get_cmap("Reds")(np.linspace(0.4, 0.9, len(clients)))

    # Plot horizontal bars
    y_pos = np.arange(len(clients))
    bars = ax.barh(
        y_pos, underperf_rates, color=colors, edgecolor="black", linewidth=0.5
    )

    # Annotate bars with counts
    for i, (rect, count, runs) in enumerate(zip(bars, underperf_counts, total_runs)):
        width = rect.get_width()
        ax.text(
            width,
            rect.get_y() + rect.get_height() / 2,
            f" {count}/{runs} runs",
            va="center",
            fontsize=8,
            color="darkred",
            fontweight="bold",
        )

    # Add threshold line at 50% (more than half of runs)
    ax.axvline(
        50,
        color="orange",
        linestyle="--",
        linewidth=2,
        alpha=0.7,
        label="50% threshold",
    )

    # Labels and title
    ax.set_yticks(y_pos)
    ax.set_yticklabels(clients, fontsize=9)
    ax.set_xlabel("Underperformance Rate (%)")
    ax.set_title(
        f"Persistent Client Underperformers Across All Runs\n"
        f"Clients that underperformed (z < {-z_threshold:.1f}) in at least one run"
    )
    ax.set_xlim(0, 105)
    ax.grid(axis="x", alpha=0.3)

    # Add recommendation box
    severe_clients = [c for c in problematic_clients if c["underperf_rate"] >= 50]
    if severe_clients:
        recommendation = (
            f"⚠️  RECOMMENDED ACTION:\n"
            f"Investigate {len(severe_clients)} client(s) that underperformed\n"
            f"in ≥50% of runs - likely hardware/network issues"
        )
        ax.text(
            0.98,
            0.02,
            recommendation,
            transform=ax.transAxes,
            fontsize=10,
            verticalalignment="bottom",
            horizontalalignment="right",
            bbox={
                "boxstyle": "round",
                "facecolor": "lightyellow",
                "alpha": 0.9,
                "edgecolor": "red",
                "linewidth": 2,
            },
            color="darkred",
            fontweight="bold",
        )

    ax.legend(loc=_LEGEND_LOC_UPPER_RIGHT, fontsize=9)
    plt.tight_layout()

    # Save figure
    filename = f"warp-mn-GET-persistent-underperformers-{datestamp}.png"
    plt.savefig(os.path.join(output_dir, filename), bbox_inches="tight", dpi=150)
    plt.close()
    print(f"Generated persistent underperformer analysis: {filename}")

    # ========================================
    # Generate text report
    # ========================================
    report_filename = f"warp-mn-GET-persistent-underperformers-{datestamp}.txt"
    report_path = os.path.join(output_dir, report_filename)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write("PERSISTENT CLIENT UNDERPERFORMER ANALYSIS\n")
        f.write("=" * 80 + "\n\n")
        f.write(f"Analysis Date: {datestamp}\n")
        f.write(
            f"Z-score Threshold: {z_threshold} "
            f"({_z_confidence_percent_label(z_threshold)}% confidence)\n"
        )
        f.write(f"Total Clients Analyzed: {len(client_stats)}\n")
        f.write(f"Clients with Underperformance: {len(problematic_clients)}\n\n")

        if severe_clients:
            f.write("=" * 80 + "\n")
            f.write("⚠️  HIGH PRIORITY - RECOMMENDED FOR INVESTIGATION\n")
            f.write("=" * 80 + "\n\n")
            f.write(
                f"These {len(severe_clients)} client(s) underperformed in ≥50% of runs:\n\n"
            )

            for c in severe_clients:
                f.write(f"  • {c['client']}\n")
                f.write(
                    f"    - Underperformed in {c['underperf_count']}/{c['total_runs']} runs ({c['underperf_rate']:.1f}%)\n"
                )
                f.write(f"    - Average z-score: {c['avg_z_score']:.2f}\n")

                # Show details of runs where this client underperformed
                if client_run_details[c["client"]]:
                    f.write("    - Problem runs:\n")
                    for detail in client_run_details[c["client"]][
                        :5
                    ]:  # Limit to first 5
                        f.write(
                            f"      * {detail['obj_size']}-{detail['threads']}t-{detail['nodes']}n: "
                            f"z={detail['z_score']:.2f}, throughput={detail['throughput']:.2f} Gbps\n"
                        )
                    if len(client_run_details[c["client"]]) > 5:
                        f.write(
                            f"      * ... and {len(client_run_details[c['client']]) - 5} more\n"
                        )
                f.write("\n")

        f.write("=" * 80 + "\n")
        f.write("ALL CLIENTS WITH UNDERPERFORMANCE (sorted by rate)\n")
        f.write("=" * 80 + "\n\n")
        f.write(
            f"{'Rank':<6}{'Client':<35}{'Rate':<12}{'Count':<15}{'Avg Z-score':<12}\n"
        )
        f.write("-" * 80 + "\n")

        for i, c in enumerate(problematic_clients, 1):
            f.write(
                f"{i:<6}{c['client']:<35}{c['underperf_rate']:>6.1f}%    "
                f"{c['underperf_count']:>2}/{c['total_runs']:<8} "
                f"{c['avg_z_score']:>6.2f}\n"
            )

        f.write("\n" + "=" * 80 + "\n")
        f.write("LEGEND\n")
        f.write("=" * 80 + "\n")
        f.write("Rate:        Percentage of runs where client underperformed\n")
        f.write(
            "Count:       Number of runs with underperformance / Total runs participated\n"
        )
        f.write(
            "Avg Z-score: Average z-score across underperforming runs (more negative = worse)\n"
        )
        f.write("\n")
        f.write("RECOMMENDATION:\n")
        f.write("- Investigate clients with rate ≥50% for hardware/network issues\n")
        f.write("- Check network cables, NICs, CPU throttling, memory issues\n")
        f.write("- Compare against known-good clients to identify patterns\n")

    print(f"Generated persistent underperformer report: {report_filename}")


def plot_client_underperformance_heatmap(
    metrics: List[WarpMetrics],
    datestamp: str,
    output_dir: str,
    config_label: str,
    z_threshold: float = 2.0,
    min_segments: int = 1,
) -> None:
    """Generate heatmap showing per-segment client underperformance.

    Analyzes throughput segments to identify which clients underperform
    in specific time windows, enabling temporal analysis of performance issues.

    Args:
        metrics: List of metrics for a specific configuration
        datestamp: Timestamp for filename
        output_dir: Directory to save plot
        config_label: Label like "4MiB-64t-2n" for title/filename
        z_threshold: Z-score threshold for underperformer detection
        min_segments: Minimum segments underperforming to display client
    """
    # Check if we have segment data
    if not metrics or not any(m.throughput_segments_by_client for m in metrics):
        print(
            f"No throughput segment data for {config_label}, skipping underperformance heatmap"
        )
        return

    # Collect all segment data across all runs (typically just one run per config)
    # Build a unified timeline across all clients
    all_segments_data = []  # List of {client: throughput} dicts, one per time point

    for metric in metrics:
        if not metric.throughput_segments_by_client:
            continue

        # Get all clients for this run
        clients = list(metric.throughput_segments_by_client.keys())
        if not clients:
            continue

        # Find the maximum number of segments across all clients
        max_segments = max(
            len(metric.throughput_segments_by_client[c]) for c in clients
        )

        # Build per-segment client throughput data
        for segment_idx in range(max_segments):
            segment_throughputs = {}
            for client in clients:
                client_segments = metric.throughput_segments_by_client[client]
                if segment_idx < len(client_segments):
                    # Convert bytes per second to Gbps
                    bps = client_segments[segment_idx]["bps"]
                    gbps = (bps * 8) / (1000**3)
                    segment_throughputs[client] = gbps

            if segment_throughputs:
                all_segments_data.append(segment_throughputs)

    if not all_segments_data:
        print(
            f"No valid segment data for {config_label}, skipping underperformance heatmap"
        )
        return

    # Analyze each segment to identify underperformers
    client_underperformance = defaultdict(
        lambda: {"segments": [], "z_scores": []}
    )  # client: {segments: [indices], z_scores: [values]}

    for segment_idx, segment_throughputs in enumerate(all_segments_data):
        if len(segment_throughputs) < 2:
            continue  # Need at least 2 clients for statistical analysis

        values = np.array(list(segment_throughputs.values()))
        mean = np.mean(values)
        stddev = np.std(values)

        if stddev == 0:
            continue  # Skip if no variation

        # Calculate z-scores for all clients in this segment
        for client, throughput in segment_throughputs.items():
            z_score = (throughput - mean) / stddev

            # Flag underperformers
            if z_score < -z_threshold:
                client_underperformance[client]["segments"].append(segment_idx)
                client_underperformance[client]["z_scores"].append(z_score)

    # Filter clients that don't meet minimum segment threshold
    filtered_clients = {
        client: data
        for client, data in client_underperformance.items()
        if len(data["segments"]) >= min_segments
    }

    if not filtered_clients:
        print(
            f"No clients underperformed in ≥{min_segments} segments for {config_label}, "
            f"skipping heatmap (z_threshold={z_threshold})"
        )
        return

    # Sort clients by number of underperformance segments (worst first)
    sorted_clients = sorted(
        filtered_clients.items(), key=lambda x: len(x[1]["segments"]), reverse=True
    )

    # Limit to top 50 clients for readability
    max_clients_display = 50
    display_clients = sorted_clients[:max_clients_display]

    # Build heatmap data matrix
    client_names = [client for client, _ in display_clients]
    num_segments = len(all_segments_data)

    # Initialize heatmap matrix (clients x segments) with NaN (white)
    heatmap_data = np.full((len(client_names), num_segments), np.nan)

    for row_idx, (client, data) in enumerate(display_clients):
        for seg_idx, z_score in zip(data["segments"], data["z_scores"]):
            heatmap_data[row_idx, seg_idx] = z_score

    # Create figure
    fig_height = max(8, len(client_names) * 0.3)
    fig_width = max(12, num_segments * 0.1)
    fig_width = min(fig_width, 30)  # Cap at 30 inches

    plt.figure(figsize=(fig_width, fig_height))
    ax = plt.gca()

    # Create heatmap
    # Use diverging colormap: red (underperforming), white (normal), blue (overperforming)
    # But we only show underperformers, so use sequential red colormap
    cmap = plt.get_cmap("Reds_r")  # Darker = more severe underperformance
    im = ax.imshow(heatmap_data, aspect="auto", cmap=cmap, vmin=-5, vmax=-z_threshold)

    # Set ticks
    ax.set_yticks(range(len(client_names)))
    ax.set_yticklabels(client_names, fontsize=8)

    # X-axis: show every N-th segment for readability
    x_tick_spacing = max(1, num_segments // 20)
    x_ticks = list(range(0, num_segments, x_tick_spacing))
    ax.set_xticks(x_ticks)
    ax.set_xticklabels([f"{t}s" for t in x_ticks], fontsize=8)

    # Labels
    ax.set_xlabel("Time (seconds)", fontsize=10)
    ax.set_ylabel("Client", fontsize=10)
    ax.set_title(
        f"Client Underperformance by Time Segment - {config_label}\n"
        f"Showing {len(client_names)} clients (of {len(client_underperformance)}) "
        f"with ≥{min_segments} segments underperforming (z < {-z_threshold:.1f})",
        fontsize=12,
    )

    # Add colorbar
    cbar = plt.colorbar(im, ax=ax, label="Z-score")
    cbar.ax.tick_params(labelsize=8)

    # Add grid for readability
    ax.set_xticks(np.arange(num_segments) - 0.5, minor=True)
    ax.set_yticks(np.arange(len(client_names)) - 0.5, minor=True)
    ax.grid(which="minor", color="gray", linestyle="-", linewidth=0.2, alpha=0.3)

    plt.tight_layout()

    # Save figure
    plot_suffix = "mn" if len({m.nodes for m in metrics}) > 1 else "sn"
    filename = f"warp-{plot_suffix}-GET-{config_label}-underperformance-heatmap-{datestamp}.png"
    plt.savefig(os.path.join(output_dir, filename), bbox_inches="tight", dpi=150)
    plt.close()
    print(f"Generated underperformance heatmap: {filename}")


def plot_client_ttfb_percentile_lines(
    metrics: List[WarpMetrics],
    datestamp: str,
    output_dir: str,
    config_label: str,
    z_threshold: float = 2.0,
) -> None:
    """Generate line plot showing TTFB percentiles for outlier clients.

    Compares tail latency behavior (p25, p50, p75, p90, p99) across
    underperforming clients to identify latency-specific issues.

    Args:
        metrics: List of metrics for a specific configuration
        datestamp: Timestamp for filename
        output_dir: Directory to save plot
        config_label: Label like "4MiB-64t-2n" for title/filename
        z_threshold: Z-score threshold for underperformer detection
    """
    # First identify underperformers based on throughput
    client_avg_throughput = _collect_client_avg_throughput(metrics)

    if not client_avg_throughput:
        print(f"No per-client data for {config_label}, skipping TTFB percentile plot")
        return

    analysis = identify_underperforming_clients(client_avg_throughput, z_threshold)
    underperformers = analysis["underperformers"]

    if not underperformers:
        print(
            f"No underperforming clients detected for {config_label} "
            f"(z_threshold={z_threshold}), skipping TTFB percentile plot"
        )
        return

    # Limit to worst 20 underperformers
    display_underperformers = underperformers[:20]
    underperformer_names = {client for client, _, _ in display_underperformers}

    # Collect TTFB percentile data for underperformers
    client_percentiles = {}
    for metric in metrics:
        for client_name, client_req_metrics in metric.requests_by_host.items():
            if client_name in underperformer_names:
                if client_name not in client_percentiles:
                    client_percentiles[client_name] = {
                        "p25": [],
                        "p50": [],
                        "p75": [],
                        "p90": [],
                        "p99": [],
                    }
                client_percentiles[client_name]["p25"].append(
                    client_req_metrics.ttfb_p25_ms
                )
                client_percentiles[client_name]["p50"].append(
                    client_req_metrics.ttfb_p50_ms
                )
                client_percentiles[client_name]["p75"].append(
                    client_req_metrics.ttfb_p75_ms
                )
                client_percentiles[client_name]["p90"].append(
                    client_req_metrics.ttfb_p90_ms
                )
                client_percentiles[client_name]["p99"].append(
                    client_req_metrics.ttfb_p99_ms
                )

    # Average percentiles across runs
    client_avg_percentiles = {}
    for client, percentiles in client_percentiles.items():
        client_avg_percentiles[client] = {
            "p25": np.mean(percentiles["p25"]),
            "p50": np.mean(percentiles["p50"]),
            "p75": np.mean(percentiles["p75"]),
            "p90": np.mean(percentiles["p90"]),
            "p99": np.mean(percentiles["p99"]),
        }

    # Calculate normal client reference (all non-underperformers)
    normal_ttfb_percentiles = {"p25": [], "p50": [], "p75": [], "p90": [], "p99": []}
    for metric in metrics:
        for client_name, client_req_metrics in metric.requests_by_host.items():
            if client_name not in underperformer_names:
                normal_ttfb_percentiles["p25"].append(client_req_metrics.ttfb_p25_ms)
                normal_ttfb_percentiles["p50"].append(client_req_metrics.ttfb_p50_ms)
                normal_ttfb_percentiles["p75"].append(client_req_metrics.ttfb_p75_ms)
                normal_ttfb_percentiles["p90"].append(client_req_metrics.ttfb_p90_ms)
                normal_ttfb_percentiles["p99"].append(client_req_metrics.ttfb_p99_ms)

    # Calculate median of normal clients at each percentile
    normal_ref = {}
    if normal_ttfb_percentiles["p50"]:
        for pct in ["p25", "p50", "p75", "p90", "p99"]:
            normal_ref[pct] = np.median(normal_ttfb_percentiles[pct])
    else:
        # No normal clients, skip reference line
        normal_ref = None

    # Create plot
    plt.figure(figsize=(10, 8))
    ax = plt.gca()

    # X-axis percentile positions
    percentiles = [25, 50, 75, 90, 99]
    x_pos = range(len(percentiles))

    # Get colors for clients
    num_clients = len(client_avg_percentiles)
    colors = get_plot_colors(num_clients)

    # Plot each underperformer client
    for idx, (client, percs) in enumerate(sorted(client_avg_percentiles.items())):
        y_values = [
            percs["p25"],
            percs["p50"],
            percs["p75"],
            percs["p90"],
            percs["p99"],
        ]
        ax.plot(
            x_pos,
            y_values,
            "-o",
            color=colors[idx],
            label=f"{client[:25]}",
            markersize=6,
            linewidth=1.5,
        )

    # Plot reference line for normal clients
    if normal_ref:
        ref_values = [
            normal_ref["p25"],
            normal_ref["p50"],
            normal_ref["p75"],
            normal_ref["p90"],
            normal_ref["p99"],
        ]
        ax.plot(
            x_pos,
            ref_values,
            "--",
            color="gray",
            label="Normal clients (median)",
            linewidth=2.5,
            alpha=0.7,
        )

    # Labels and formatting
    ax.set_xticks(x_pos)
    ax.set_xticklabels([f"p{p}" for p in percentiles])
    ax.set_xlabel("Percentile")
    ax.set_ylabel(_YLABEL_TTFB_LATENCY_MS)
    ax.set_title(
        f"Per-Client TTFB Latency Percentiles - {config_label}\n"
        f"Showing {len(client_avg_percentiles)} underperforming clients "
        f"(z < {-z_threshold:.1f})"
    )
    ax.set_ylim(0, None)
    ax.grid(axis="y", alpha=0.3)

    # Legend - place outside plot area
    ax.legend(
        loc=_LEGEND_LOC_CENTER_LEFT,
        bbox_to_anchor=(1.05, 0.5),
        fontsize=8,
        framealpha=0.9,
    )

    plt.tight_layout()

    # Save figure
    plot_suffix = "mn" if len({m.nodes for m in metrics}) > 1 else "sn"
    filename = f"warp-{plot_suffix}-GET-{config_label}-ttfb-percentiles-{datestamp}.png"
    plt.savefig(os.path.join(output_dir, filename), bbox_inches="tight", dpi=150)
    plt.close()
    print(f"Generated TTFB percentile plot: {filename}")


def plot_client_throughput_timeseries(
    metrics: List[WarpMetrics],
    datestamp: str,
    output_dir: str,
    config_label: str,
    z_threshold: float = 2.0,
) -> None:
    """Generate time-series plot showing client throughput over time.

    Shows percentile bands (p10, p50, p90) across all clients with individual
    lines for outlier clients highlighted. Enables temporal analysis of
    performance issues and warm-up effects.

    Args:
        metrics: List of metrics for a specific configuration
        datestamp: Timestamp for filename
        output_dir: Directory to save plot
        config_label: Label like "4MiB-64t-2n" for title/filename
        z_threshold: Z-score threshold for underperformer detection
    """
    # Check if we have segment data
    if not metrics or not any(m.throughput_segments_by_client for m in metrics):
        print(
            f"No throughput segment data for {config_label}, skipping time-series plot"
        )
        return

    # First identify underperformers based on aggregate throughput
    client_avg_throughput = _collect_client_avg_throughput(metrics)

    if not client_avg_throughput:
        print(f"No per-client data for {config_label}, skipping time-series plot")
        return

    # Identify outliers (underperformers) using z-score
    analysis = identify_underperforming_clients(client_avg_throughput, z_threshold)
    outlier_clients = analysis[
        "underperformers"
    ]  # List of (client_name, value, z_score)

    # Collect all segment data from all runs and clients
    # We'll aggregate across runs by averaging for each client
    client_segment_data = {}  # {client_name: {time_idx: [values across runs]}}

    for metric in metrics:
        if not metric.throughput_segments_by_client:
            continue

        for client_name, segments in metric.throughput_segments_by_client.items():
            if client_name not in client_segment_data:
                client_segment_data[client_name] = {}

            for time_idx, seg in enumerate(segments):
                gbps = seg["bps"] / 1e9  # Convert bps to Gbps
                if time_idx not in client_segment_data[client_name]:
                    client_segment_data[client_name][time_idx] = []
                client_segment_data[client_name][time_idx].append(gbps)

    if not client_segment_data:
        print(f"No segment data available for {config_label}, skipping time-series")
        return

    # Determine the time range (max segments across all clients)
    max_time_idx = max(
        max(time_indices.keys())
        for time_indices in client_segment_data.values()
        if time_indices
    )
    time_points = list(range(max_time_idx + 1))

    # Calculate percentiles across all clients at each time point
    p10_values = []
    p50_values = []
    p90_values = []

    for time_idx in time_points:
        # Collect all client throughputs at this time point (averaged across runs)
        all_client_tp_at_time = []
        for client_name, time_data in client_segment_data.items():
            if time_idx in time_data:
                # Average across runs for this client at this time
                avg_tp = np.mean(time_data[time_idx])
                all_client_tp_at_time.append(avg_tp)

        if all_client_tp_at_time:
            p10_values.append(np.percentile(all_client_tp_at_time, 10))
            p50_values.append(np.percentile(all_client_tp_at_time, 50))
            p90_values.append(np.percentile(all_client_tp_at_time, 90))
        else:
            # No data at this time point
            p10_values.append(np.nan)
            p50_values.append(np.nan)
            p90_values.append(np.nan)

    # Create the plot
    plt.figure(figsize=(14, 7))
    ax = plt.gca()

    # Plot percentile bands
    ax.fill_between(
        time_points,
        p10_values,
        p90_values,
        alpha=0.3,
        color="steelblue",
        label="p10-p90 range (all clients)",
    )

    # Plot median line
    ax.plot(time_points, p50_values, color="steelblue", linewidth=2, label="p50 median")

    # Plot individual outlier client lines (limit to worst 10)
    if outlier_clients:
        # Sort by z-score (worst first) - tuples are (client_name, value, z_score)
        sorted_outliers = sorted(outlier_clients, key=lambda x: x[2])[:10]

        # Use distinct colors for outliers
        outlier_colors = plt.get_cmap("Set1")(np.linspace(0, 1, len(sorted_outliers)))

        for idx, (client_name, _, z_score) in enumerate(sorted_outliers):
            if client_name not in client_segment_data:
                continue

            # Get this client's time series (averaged across runs)
            client_time_series = []
            for time_idx in time_points:
                if time_idx in client_segment_data[client_name]:
                    avg_tp = np.mean(client_segment_data[client_name][time_idx])
                    client_time_series.append(avg_tp)
                else:
                    client_time_series.append(np.nan)

            # Plot this outlier
            ax.plot(
                time_points,
                client_time_series,
                color=outlier_colors[idx],
                linewidth=1.5,
                alpha=0.8,
                label=f"{client_name} (z={z_score:.2f})",
            )

    # Labels and formatting
    ax.set_xlabel("Time (seconds from start)")
    ax.set_ylabel(_YLABEL_THROUGHPUT_GBPS)
    num_clients = len(client_segment_data)
    num_outliers = len(outlier_clients) if outlier_clients else 0
    ax.set_title(
        f"Client Throughput Over Time - {config_label}\n"
        f"{num_clients} clients total, {num_outliers} outliers shown (z < -{z_threshold})"
    )

    ax.set_xlim(0, max(time_points))
    ax.set_ylim(0, None)
    ax.grid(True, alpha=0.3)

    # Legend - place outside plot area
    ax.legend(
        loc=_LEGEND_LOC_CENTER_LEFT,
        bbox_to_anchor=(1.05, 0.5),
        fontsize=9,
        framealpha=0.9,
    )

    plt.tight_layout()

    # Save figure
    plot_suffix = "mn" if len({m.nodes for m in metrics}) > 1 else "sn"
    filename = (
        f"warp-{plot_suffix}-GET-{config_label}-throughput-timeseries-{datestamp}.png"
    )
    plt.savefig(os.path.join(output_dir, filename), bbox_inches="tight", dpi=150)
    plt.close()

    print(f"Generated throughput time-series plot: {filename}")


def calculate_scaling_efficiency(
    throughput_values: List[float], scale_values: List[int]
) -> List[float]:
    """Calculate scaling efficiency as percentage of best observed efficiency.

    Args:
        throughput_values: List of throughput measurements (e.g., Gbps)
        scale_values: List of scale factors (e.g., node count or thread count)

    Returns:
        List of scaling efficiency percentages where the best efficiency = 100%

    The scaling efficiency is calculated by:
    1. Computing efficiency (throughput / scale_value) for each data point
    2. Finding the maximum efficiency across all points
    3. Expressing each point's efficiency as a percentage of the maximum

    This ensures no series can exceed 100% scaling efficiency, with the best
    observed efficiency serving as the baseline.
    """
    if not scale_values or not throughput_values:
        return []

    # Find the maximum efficiency (throughput / scale_value) across all data points
    # This becomes the 100% baseline for perfect scaling
    efficiencies = [
        bw / x if x != 0 else 0 for bw, x in zip(throughput_values, scale_values)
    ]
    base_efficiency = max(efficiencies) if efficiencies else 0

    if base_efficiency != 0:
        # Calculate scaling for all data points as percentage of best efficiency
        return [
            (bw / x) / base_efficiency * 100 if x != 0 else 0
            for bw, x in zip(throughput_values, scale_values)
        ]

    return [0] * len(scale_values)


def write_json(metrics: List[WarpMetrics], output_path: str, datestamp: str) -> None:
    """Write metrics to zstandard-compressed JSON file.

    Args:
        metrics: List of WarpMetrics objects
        output_path: Path to output .json.zst file
        datestamp: Datestamp string for this analysis run

    Serializes complete metrics including all per-client data and histograms.
    Output is compressed with zstandard for space efficiency.
    The JSON structure is an object with metadata and metrics array.
    """
    if not metrics:
        raise ValueError("No metrics to write to JSON")

    if zstd is None:
        raise ImportError(
            "zstandard library is required to write .json.zst files. "
            + _ZST_INSTALL_HINT
        )

    # Convert metrics to serializable format
    serializable_metrics = []
    for metric in metrics:
        metric_dict = asdict(metric)

        # Convert histogram float keys to strings for JSON compatibility
        metric_dict["ttfb_histogram"] = {
            str(k): v for k, v in metric.ttfb_histogram.items()
        }

        metric_dict["ttfb_histograms_by_client"] = {
            client: {str(k): v for k, v in hist.items()}
            for client, hist in metric.ttfb_histograms_by_client.items()
        }

        serializable_metrics.append(metric_dict)

    # Wrap in metadata container
    output_data = {
        "version": 1,  # Format version for future compatibility
        "datestamp": datestamp,
        "metrics": serializable_metrics,
    }

    # Serialize to JSON string with pretty printing
    json_str = json.dumps(output_data, indent=2)

    # Compress and write
    cctx = zstd.ZstdCompressor(level=3)  # Level 3: good balance of speed/compression
    compressed_data = cctx.compress(json_str.encode("utf-8"))

    with open(output_path, "wb") as f:
        f.write(compressed_data)


def read_json(filepath: str) -> tuple[List[WarpMetrics], str]:
    """Read metrics from zstandard-compressed JSON file.

    Args:
        filepath: Path to .json.zst file

    Returns:
        Tuple of (List of WarpMetrics objects, datestamp string)

    Deserializes complete metrics including all per-client data and histograms.
    Handles zstandard-compressed files transparently.
    """
    if zstd is None:
        raise ImportError(
            "zstandard library is required to read .json.zst files. "
            + _ZST_INSTALL_HINT
        )

    # Read and decompress
    with open(filepath, "rb") as f:
        dctx = zstd.ZstdDecompressor()
        decompressed_data = dctx.decompress(f.read())
        json_str = decompressed_data.decode("utf-8")

    # Parse JSON
    data = json.loads(json_str)

    # Check format - should be object with metadata
    if not isinstance(data, dict) or "metrics" not in data:
        raise ValueError(
            "Invalid JSON format: expected object with 'metrics' key. "
            "This may be an old format file or not an analyzed JSON file."
        )

    # Extract metadata and metrics
    metrics_data = data["metrics"]
    datestamp = data.get("datestamp")

    metrics = []
    for metric_dict in metrics_data:
        # Reconstruct per-client request metrics
        requests_by_host = {
            client: ClientRequestMetrics(**req_dict)
            for client, req_dict in metric_dict.get("requests_by_host", {}).items()
        }

        # Reconstruct per-client throughput metrics
        throughput_by_host = {
            client: ClientThroughputMetrics(**tp_dict)
            for client, tp_dict in metric_dict.get("throughput_by_host", {}).items()
        }

        # Reconstruct TTFB histogram (convert string keys back to floats)
        ttfb_histogram = {
            float(k): int(v) for k, v in metric_dict.get("ttfb_histogram", {}).items()
        }

        # Reconstruct per-client TTFB histograms
        ttfb_histograms_by_client = {
            client: {float(k): int(v) for k, v in hist.items()}
            for client, hist in metric_dict.get("ttfb_histograms_by_client", {}).items()
        }

        # Create WarpMetrics object
        metrics.append(
            WarpMetrics(
                nodes=int(metric_dict["nodes"]),
                obj_size=metric_dict["obj_size"],
                threads=int(metric_dict["threads"]),
                med_lat_ms=float(metric_dict["med_lat_ms"]),
                p99_lat_ms=float(metric_dict["p99_lat_ms"]),
                max_lat_ms=float(metric_dict["max_lat_ms"]),
                max_bw_mib=float(metric_dict["max_bw_mib"]),
                med_bw_mib=float(metric_dict["med_bw_mib"]),
                med_bw_gbps=float(metric_dict["med_bw_gbps"]),
                min_bw_mib=float(metric_dict["min_bw_mib"]),
                avg_bw_mib=float(
                    metric_dict.get("avg_bw_mib", metric_dict["med_bw_mib"])
                ),
                avg_bw_gbps=float(
                    metric_dict.get("avg_bw_gbps", metric_dict["med_bw_gbps"])
                ),
                max_bw_gbps=float(metric_dict.get("max_bw_gbps", 0)),
                min_bw_gbps=float(metric_dict.get("min_bw_gbps", 0)),
                stddev_bw_mib=float(metric_dict.get("stddev_bw_mib", 0.0)),
                stddev_bw_gbps=float(metric_dict.get("stddev_bw_gbps", 0.0)),
                avg_rate_obj=float(metric_dict["avg_rate_obj"]),
                med_rate_obj=float(metric_dict["med_rate_obj"]),
                window_size=metric_dict.get("window_size", "1s"),
                requests_by_host=requests_by_host,
                throughput_by_host=throughput_by_host,
                ttfb_histogram=ttfb_histogram,
                ttfb_histograms_by_client=ttfb_histograms_by_client,
            )
        )

    if not metrics:
        raise ValueError("No data found in JSON file")

    return metrics, datestamp


def find_analyzed_file(directory: str, base_filename: str) -> Optional[str]:
    """Find analyzed file with flexible extension matching.

    Args:
        directory: Directory to search
        base_filename: Filename with or without extensions (e.g., "20251029Z194526-analyzed"
                      or "20251029Z194526-analyzed.json.zst")

    Returns:
        Full path to the file if found, None otherwise

    Tries multiple extension patterns:
    1. Exact filename as provided
    2. base + ".json.zst"
    3. base + ".json"
    4. Extracts datestamp and looks for <datestamp>-analyzed.json.zst
    """
    # Try exact filename first
    full_path = os.path.join(directory, base_filename)
    if os.path.isfile(full_path):
        return full_path

    # Strip any extensions to get base
    base = base_filename
    for ext in (_JSON_ZST_EXT, _JSON_EXT, ".zst"):
        if base.endswith(ext):
            base = base[: -len(ext)]

    # Try common extensions
    for ext in _JSON_EXTENSIONS_TRY_ORDER:
        candidate = os.path.join(directory, base + ext)
        if os.path.isfile(candidate):
            return candidate

    # Try to extract datestamp and find standard filename
    datestamp_match = re.search(_DATESTAMP_CAPTURE_PATTERN, base)
    if datestamp_match:
        datestamp = datestamp_match.group(1)
        for ext in _JSON_EXTENSIONS_TRY_ORDER:
            candidate = os.path.join(directory, f"{datestamp}-analyzed{ext}")
            if os.path.isfile(candidate):
                return candidate

    return None


def plot_metrics(
    metrics: List[WarpMetrics],
    datestamp: str,
    output_dir: str,
    is_multi_node: bool,
) -> None:
    """Generate plots for Warp metrics.

    Args:
        metrics: List of WarpMetrics objects
        datestamp: Timestamp for output files
        output_dir: Directory to save plots
        is_multi_node: Whether this is multi-node data
    """
    # Prepare data for throughput-latency plot
    prepared_data = {}
    prepared_data["is_multi_node"] = is_multi_node

    # Get a representative window_size from the metrics
    window_sizes = {m.window_size for m in metrics}
    if len(window_sizes) == 1:
        window_size = window_sizes.pop()
    else:
        window_size = ", ".join(sorted(window_sizes))
    prepared_data["window_size"] = window_size

    if is_multi_node:
        # Multi-node data preparation
        combinations = sorted(
            {(m.obj_size, m.threads) for m in metrics},
            key=lambda x: (parse_size(x[0]), x[1]),
        )
        x_values = sorted({m.nodes for m in metrics})
        x_label = "Node Count"
        plot_suffix = "mn"

        prepared_data["x_values"] = x_values
        prepared_data["x_label"] = x_label
        prepared_data["plot_suffix"] = plot_suffix
        prepared_data["plot_combinations"] = [
            f"{size},{thread}" for size, thread in combinations
        ]

        # Build color mapping for consistent colors across all plots
        num_colors = len(combinations)
        colors = get_plot_colors(num_colors)
        color_mapping = build_color_mapping(prepared_data["plot_combinations"], colors)
        prepared_data["color_mapping"] = color_mapping

        for i, (size, thread) in enumerate(combinations):
            size_metrics = sorted(
                [m for m in metrics if m.obj_size == size and m.threads == thread],
                key=lambda x: x.nodes,
            )
            x_data = [m.nodes for m in size_metrics]
            med_bw = [m.med_bw_gbps for m in size_metrics]
            max_bw = [m.max_bw_gbps for m in size_metrics]
            min_bw = [m.min_bw_gbps for m in size_metrics]
            lat = [m.med_lat_ms for m in size_metrics]

            # Calculate scaling efficiency
            med_tput_scaling = calculate_scaling_efficiency(med_bw, x_data)

            prepared_data[f"x_data_{i}"] = x_data
            prepared_data[f"med_bw_{i}"] = med_bw
            prepared_data[f"max_bw_{i}"] = max_bw
            prepared_data[f"min_bw_{i}"] = min_bw
            prepared_data[f"lat_{i}"] = lat
            prepared_data[f"med_tput_scaling_{i}"] = med_tput_scaling
    else:
        # Single-node data preparation
        obj_sizes = sorted({m.obj_size for m in metrics}, key=parse_size)
        x_values = sorted({m.threads for m in metrics})
        x_label = "Threads"
        plot_suffix = "snp"

        prepared_data["x_values"] = x_values
        prepared_data["x_label"] = x_label
        prepared_data["plot_suffix"] = plot_suffix
        prepared_data["plot_combinations"] = obj_sizes

        # Build color mapping for consistent colors across all plots
        num_colors = len(obj_sizes)
        colors = get_plot_colors(num_colors)
        color_mapping = build_color_mapping(obj_sizes, colors)
        prepared_data["color_mapping"] = color_mapping

        for i, size in enumerate(obj_sizes):
            size_metrics = sorted(
                [m for m in metrics if m.obj_size == size], key=lambda x: x.threads
            )
            x_data = [m.threads for m in size_metrics]
            med_bw = [m.med_bw_gbps for m in size_metrics]
            max_bw = [m.max_bw_gbps for m in size_metrics]
            min_bw = [m.min_bw_gbps for m in size_metrics]
            lat = [m.med_lat_ms for m in size_metrics]

            # Calculate scaling efficiency
            med_tput_scaling = calculate_scaling_efficiency(med_bw, x_data)

            prepared_data[f"x_data_{i}"] = x_data
            prepared_data[f"med_bw_{i}"] = med_bw
            prepared_data[f"max_bw_{i}"] = max_bw
            prepared_data[f"min_bw_{i}"] = min_bw
            prepared_data[f"lat_{i}"] = lat
            prepared_data[f"med_tput_scaling_{i}"] = med_tput_scaling

    # Generate plots
    plot_throughput(prepared_data, datestamp, output_dir)
    plot_latency(prepared_data, datestamp, output_dir)
    plot_tput_scaling(prepared_data, datestamp, output_dir)

    # Generate histogram plots
    plot_ttfb_histograms(metrics, datestamp, output_dir, is_multi_node)


def filter_metrics(
    metrics: List[WarpMetrics],
    only_sizes: Optional[Set[str]] = None,
    only_threads: Optional[Set[int]] = None,
) -> List[WarpMetrics]:
    """Filter metrics based on object sizes and thread counts."""
    filtered = metrics
    if only_sizes:
        filtered = [m for m in filtered if m.obj_size in only_sizes]
    if only_threads:
        filtered = [m for m in filtered if m.threads in only_threads]
    return filtered


def format_numeric(value):
    """Format numeric values without trailing .0 for whole numbers."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def calculate_decimal_parts(value: float) -> tuple[int, int]:
    """Calculate digits before and after decimal point.

    Args:
        value: Numeric value

    Returns:
        Tuple of (digits_before_decimal, digits_after_decimal)

    Note: Integers will have at least 1 digit after decimal (the "0" in ".0")
    """
    # Format the same way as format_avg_bw_with_stddev does
    value_str = f"{value:.10f}".rstrip("0").rstrip(".")

    # Ensure at least one decimal place for integers
    if "." not in value_str:
        value_str += ".0"

    before, after = value_str.split(".")
    return (len(before), len(after))


def format_avg_bw_with_stddev(
    avg: float,
    stddev: float,
    avg_before: int = 0,
    avg_after: int = 0,
    stddev_before: int = 0,
    stddev_after: int = 0,
) -> str:
    """Format average bandwidth with standard deviation with decimal alignment.

    Args:
        avg: Average bandwidth in Gbps
        stddev: Standard deviation in Gbps
        avg_before: Max digits before decimal in avg values (for alignment)
        avg_after: Max digits after decimal in avg values (for alignment)
        stddev_before: Max digits before decimal in stddev values (for alignment)
        stddev_after: Max digits after decimal in stddev values (for alignment)

    Returns:
        Formatted string like " 92.85  ±  5.27" with decimal points aligned

    The formatting ensures decimal points land in the same column across all rows.
    Note: Integer values are always formatted with at least ".0" to maintain alignment.
    """
    # Format numbers, but preserve decimals (don't use format_numeric which strips .0)
    # Convert to string with enough precision
    avg_str = f"{avg:.10f}".rstrip("0").rstrip(".")
    stddev_str = f"{stddev:.10f}".rstrip("0").rstrip(".")

    # Ensure at least one decimal place for integers (e.g., "303" becomes "303.0")
    if "." not in avg_str:
        avg_str += ".0"
    if "." not in stddev_str:
        stddev_str += ".0"

    # Split into before/after decimal parts
    avg_b, avg_a = avg_str.split(".")
    stddev_b, stddev_a = stddev_str.split(".")

    # Pad to align decimal points
    avg_b_padded = avg_b.rjust(avg_before)
    avg_a_padded = avg_a.ljust(avg_after)
    stddev_b_padded = stddev_b.rjust(stddev_before)
    stddev_a_padded = stddev_a.ljust(stddev_after)

    # Reconstruct with decimal points
    avg_formatted = f"{avg_b_padded}.{avg_a_padded}"
    stddev_formatted = f"{stddev_b_padded}.{stddev_a_padded}"

    # Don't strip trailing spaces - they're needed to align the ± symbols
    return f"{avg_formatted} ± {stddev_formatted}"


def print_table(
    metrics: List[WarpMetrics],
    is_multi_node: bool,
    only_sizes: Optional[Set[str]] = None,
    only_threads: Optional[Set[int]] = None,
) -> None:
    """Print formatted metrics table.

    First prints detailed tables for each unique (obj_size, threads) configuration,
    then prints a combined summary table filtered by only_sizes and only_threads.
    """
    if not metrics:
        print("No metrics to display")
        return

    # Group metrics by (obj_size, threads)
    grouped_metrics = {}
    for m in metrics:
        key = (m.obj_size, m.threads)
        grouped_metrics.setdefault(key, []).append(m)

    # Helper function to convert MiB/s to Gbps
    def mib_to_gbps_local(mib_per_sec: float) -> float:
        return (mib_per_sec * 8 * 1024 * 1024) / (1000 * 1000 * 1000)

    # Print individual tables for each configuration
    for (obj_size, threads), config_metrics in sorted(
        grouped_metrics.items(), key=lambda x: (parse_size(x[0][0]), x[0][1])
    ):
        # Check if all metrics in this group have the same window size
        window_sizes = {m.window_size for m in config_metrics}
        window_size_str = (
            window_sizes.pop() if len(window_sizes) == 1 else ", ".join(window_sizes)
        )

        print(
            f"\nConfiguration: {obj_size} object size, {threads} threads per client, {window_size_str} windowing"
        )

        headers = ["Nodes", *_WARP_METRICS_TABLE_HEADERS]

        # Sort by node count
        sorted_config_metrics = sorted(config_metrics, key=lambda x: x.nodes)

        # Calculate max digits before/after decimal for proper alignment
        max_avg_before = max(
            calculate_decimal_parts(m.avg_bw_gbps)[0] for m in sorted_config_metrics
        )
        max_avg_after = max(
            calculate_decimal_parts(m.avg_bw_gbps)[1] for m in sorted_config_metrics
        )
        max_stddev_before = max(
            calculate_decimal_parts(m.stddev_bw_gbps)[0] for m in sorted_config_metrics
        )
        max_stddev_after = max(
            calculate_decimal_parts(m.stddev_bw_gbps)[1] for m in sorted_config_metrics
        )

        rows = []
        for m in sorted_config_metrics:
            row = [
                m.nodes,
                m.med_lat_ms,
                m.p99_lat_ms,
                m.max_lat_ms,
                round(mib_to_gbps_local(m.max_bw_mib), 2),
                m.med_bw_gbps,
                round(mib_to_gbps_local(m.min_bw_mib), 2),
                format_avg_bw_with_stddev(
                    m.avg_bw_gbps,
                    m.stddev_bw_gbps,
                    max_avg_before,
                    max_avg_after,
                    max_stddev_before,
                    max_stddev_after,
                ),
                m.avg_rate_obj,
                m.med_rate_obj,
            ]
            rows.append(row)

        # Calculate column widths
        str_rows = [
            [format_numeric(val) if val is not None else "" for val in row]
            for row in rows
        ]
        widths = [max(len(str(val)) for val in col) for col in zip(headers, *str_rows)]

        # Print headers
        header_fmt = "|".join(f"{{:{w}}}" for w in widths)
        print(header_fmt.format(*headers))
        print("-" * (sum(widths) + len(widths) - 1))

        # Print data rows
        row_fmt = "|".join(f"{{:{w}}}" for w in widths)
        for row in str_rows:
            print(row_fmt.format(*row))

    # Filter metrics for the summary table
    filtered_metrics = metrics
    if only_sizes:
        filtered_metrics = [m for m in filtered_metrics if m.obj_size in only_sizes]
    if only_threads:
        filtered_metrics = [m for m in filtered_metrics if m.threads in only_threads]

    if not filtered_metrics:
        print("\nNo metrics match the specified filters for summary table")
        return

    # Check if all filtered metrics have the same window size
    filtered_window_sizes = {m.window_size for m in filtered_metrics}
    filtered_window_size_str = (
        filtered_window_sizes.pop()
        if len(filtered_window_sizes) == 1
        else ", ".join(filtered_window_sizes)
    )

    # Print combined summary table
    print(f"\nSummary Table ({filtered_window_size_str} windowing):")
    if is_multi_node:
        headers = ["Nodes", "Size", "Threads", *_WARP_METRICS_TABLE_HEADERS]
    else:
        headers = ["Size", "Threads", *_WARP_METRICS_TABLE_HEADERS]

    sorted_metrics = sorted(
        filtered_metrics, key=lambda x: (x.nodes, parse_size(x.obj_size), x.threads)
    )

    # Calculate max digits before/after decimal for proper alignment
    max_avg_before = max(
        calculate_decimal_parts(m.avg_bw_gbps)[0] for m in sorted_metrics
    )
    max_avg_after = max(
        calculate_decimal_parts(m.avg_bw_gbps)[1] for m in sorted_metrics
    )
    max_stddev_before = max(
        calculate_decimal_parts(m.stddev_bw_gbps)[0] for m in sorted_metrics
    )
    max_stddev_after = max(
        calculate_decimal_parts(m.stddev_bw_gbps)[1] for m in sorted_metrics
    )

    rows = []
    for m in sorted_metrics:
        row = [
            m.nodes if is_multi_node else None,
            m.obj_size,
            m.threads,
            m.med_lat_ms,
            m.p99_lat_ms,
            m.max_lat_ms,
            round(mib_to_gbps_local(m.max_bw_mib), 2),
            m.med_bw_gbps,
            round(mib_to_gbps_local(m.min_bw_mib), 2),
            format_avg_bw_with_stddev(
                m.avg_bw_gbps,
                m.stddev_bw_gbps,
                max_avg_before,
                max_avg_after,
                max_stddev_before,
                max_stddev_after,
            ),
            m.avg_rate_obj,
            m.med_rate_obj,
        ]
        if not is_multi_node:
            row.pop(0)  # Remove nodes value for single-node data
        rows.append(row)

    # Calculate column widths
    str_rows = [
        [format_numeric(val) if val is not None else "" for val in row] for row in rows
    ]
    widths = [max(len(str(val)) for val in col) for col in zip(headers, *str_rows)]

    # Print headers
    header_fmt = "|".join(f"{{:{w}}}" for w in widths)
    print(header_fmt.format(*headers))
    print("-" * (sum(widths) + len(widths) - 1))

    # Print data rows
    row_fmt = "|".join(f"{{:{w}}}" for w in widths)
    for row in str_rows:
        print(row_fmt.format(*row))


def _should_include_file_for_earliest_mtime(filename: str, file_format: str) -> bool:
    """Whether to consider this file when picking earliest mtime for synthetic datestamp."""
    if file_format == "json":
        return filename.endswith(_JSON_EXT) or filename.endswith(_JSON_ZST_EXT)
    if file_format == "text":
        return filename.endswith(".out")
    return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="""
    Analyze Warp benchmark results.

    Usage:
      # Parse benchmark files in a directory
      %(prog)s /path/to/results [--to-json]

      # Read from analyzed JSON (with relative path - directory required)
      %(prog)s /path/to/results --from-json 20251029Z194526-analyzed

      # Read from analyzed JSON (with absolute path - directory optional)
      %(prog)s --from-json /path/to/results/20251029Z194526-analyzed.json.zst

      # Read from analyzed JSON (directory - finds first analyzed file)
      %(prog)s --from-json /path/to/results

      # Specify different output directory for plots
      %(prog)s /path/to/results --output-dir /path/to/plots
    """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "directory",
        nargs="?",
        help="Directory containing Warp benchmark files. "
        "Required when parsing benchmarks or using relative paths with --from-json. "
        "Optional when using --from-json with absolute path.",
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        help="Directory to save plots and reports. "
        "Defaults to: input directory when parsing, JSON's directory with --from-json, "
        "or current directory if neither is available.",
    )
    parser.add_argument(
        "--to-json",
        action="store_true",
        help="Write analyzed results to <datestamp>-analyzed.json.zst in the output directory. "
        "Includes complete data for all analysis including per-client metrics and histograms.",
    )
    parser.add_argument(
        "--from-json",
        metavar="PATH",
        type=str,
        help="Read metrics from a previously analyzed JSON file. "
        "Accepts absolute file path (/path/to/file.json.zst), "
        "directory path (will find first analyzed file), "
        "relative path (file.json.zst), "
        "or just datestamp (20251029Z194526). Will try .json.zst and .json extensions.",
    )
    parser.add_argument(
        "--only-sizes",
        action="append",
        metavar="SIZE",
        help=(
            "Only include benchmarks with these object sizes (repeat flag for multiple). "
            "Commas are not split; use ';' inside one argument for several sizes "
            "(e.g. '1MiB;1GiB') or pass --only-sizes multiple times. "
            "Former comma-separated lists must use ';' or multiple --only-sizes flags."
        ),
    )
    parser.add_argument(
        "--only-threads",
        help="Comma-separated list of thread counts to include (e.g., 1,2,4)",
    )
    parser.add_argument(
        "--per-client-plots",
        action="store_true",
        help="Generate per-client comparison plots for multi-node runs. "
        "Identifies underperforming clients and visualizes performance clustering.",
    )
    parser.add_argument(
        "--client-outlier-threshold",
        type=float,
        default=2.0,
        help="Z-score threshold for identifying underperforming clients. "
        "Default: 2.0 (95%% confidence, ~2.5%% of normal distribution). "
        "Lower values (e.g., 1.5) detect more outliers but may include random variance. "
        "Higher values (e.g., 3.0) detect only severe outliers (99.7%% confidence).",
    )
    parser.add_argument(
        "--client-min-underperform-segments",
        type=int,
        default=1,
        help="Minimum number of time segments a client must underperform to be flagged. "
        "Default: 1 (flag if underperforms in any segment). "
        "Higher values (e.g., 10) require persistent underperformance.",
    )
    args = parser.parse_args()

    # Parse filters if provided
    only_sizes = parse_only_sizes_arg(args.only_sizes)
    only_threads = (
        {int(t) for t in args.only_threads.split(",")} if args.only_threads else None
    )

    # Determine input directory and output directory based on arguments
    input_dir = None
    output_dir = None

    if args.from_json:
        # Reading from analyzed JSON file
        # Check if it's a directory
        if os.path.isdir(args.from_json):
            # Directory provided - search for first analyzed JSON file
            found_file = None
            input_dir = args.from_json

            # Look for files with our analyzed format
            for filename in sorted(os.listdir(input_dir)):
                if filename.endswith(_JSON_ZST_EXT) or filename.endswith(_JSON_EXT):
                    filepath = os.path.join(input_dir, filename)
                    try:
                        # Try to open and check if it's our format
                        data = open_json_file(filepath)
                        if (
                            isinstance(data, dict)
                            and "version" in data
                            and "metrics" in data
                        ):
                            found_file = filepath
                            print(f"Found analyzed file: {filename}")
                            break
                    except (IOError, OSError, json.JSONDecodeError):
                        continue

            if found_file is None:
                parser.error(
                    f"No analyzed JSON file found in directory: {args.from_json}\n"
                    f"Looking for files with 'version' and 'metrics' keys."
                )
        # Check if it's an absolute path to a file
        elif os.path.isabs(args.from_json):
            # Absolute path - can work without directory argument
            if os.path.isfile(args.from_json):
                found_file = args.from_json
                input_dir = os.path.dirname(args.from_json)
            else:
                parser.error(f"JSON file not found: {args.from_json}")
        else:
            # Relative path - need directory argument
            if not args.directory:
                parser.error(
                    "Directory argument required when using --from-json with relative path.\n"
                    f"Either provide directory: {parser.prog} /path/to/dir --from-json {args.from_json}\n"
                    f"Or use absolute path: {parser.prog} --from-json /absolute/path/to/{args.from_json}"
                )
            if not os.path.isdir(args.directory):
                parser.error(f"Directory does not exist: {args.directory}")

            input_dir = args.directory
            found_file = find_analyzed_file(input_dir, args.from_json)

            if found_file is None:
                parser.error(
                    f"Could not find analyzed JSON file matching '{args.from_json}' in {input_dir}\n"
                    f"Tried: {args.from_json}, {args.from_json}.json.zst, {args.from_json}.json"
                )

        print(f"Reading metrics from {found_file}...")
        try:
            metrics, json_datestamp = read_json(found_file)
            # Use datestamp from JSON if available, otherwise extract from filename, otherwise use mtime
            if json_datestamp:
                datestamp = json_datestamp
            else:
                datestamp_match = re.search(
                    _DATESTAMP_CAPTURE_PATTERN, os.path.basename(found_file)
                )
                if datestamp_match:
                    datestamp = datestamp_match.group(1)
                else:
                    # Use file modification time as datestamp
                    # pylint: disable=import-outside-toplevel
                    from datetime import datetime, timezone

                    mtime = os.path.getmtime(found_file)
                    datestamp = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                        _DATESTAMP_STRFTIME
                    )
                    print(
                        f"Warning: No datestamp in JSON or filename, using file modification time: {datestamp}"
                    )
        except (ValueError, OSError, ImportError) as e:
            parser.error(f"Error reading JSON: {e}")

        # Determine output directory
        if args.output_dir:
            output_dir = args.output_dir
        elif args.directory:
            output_dir = args.directory
        else:
            # Use JSON file's directory
            output_dir = os.path.dirname(found_file) or "."

    else:
        # Parsing benchmark files - directory is required
        if not args.directory:
            parser.error(
                f"Directory argument required when parsing benchmark files.\n"
                f"Usage: {parser.prog} /path/to/results"
            )

        if not os.path.isdir(args.directory):
            parser.error(f"Directory does not exist: {args.directory}")

        input_dir = args.directory
        output_dir = args.output_dir if args.output_dir else args.directory

        # Detect file format
        file_format = detect_warp_file_format(input_dir)

        if file_format == "none":
            parser.error("No GET benchmark files found with valid datestamp")

        # Extract datestamp based on format
        # Try to find a datestamp from any file (used only for output filenames)
        datestamp = None
        if file_format == "json":
            # Look for datestamp pattern in .json or .json.zst files
            # This is ONLY for output filename generation, not for parsing
            for filename in os.listdir(input_dir):
                if _JSON_EXT in filename:
                    match = re.search(_DATESTAMP_CAPTURE_PATTERN, filename)
                    if match:
                        datestamp = match.group(1)
                        break
        else:  # file_format == 'text'
            # Existing logic for .out files
            for filename in os.listdir(input_dir):
                if filename.startswith("warp-GET") and filename.endswith(".out"):
                    match = re.search(_DATESTAMP_AFTER_UNDERSCORE_PATTERN, filename)
                    if match:
                        datestamp = match.group(1)
                        break

        if datestamp is None:
            # Generate datestamp from earliest file modification time
            # pylint: disable=import-outside-toplevel
            from datetime import datetime, timezone

            earliest_mtime = None
            for filename in os.listdir(input_dir):
                if not _should_include_file_for_earliest_mtime(filename, file_format):
                    continue
                filepath = os.path.join(input_dir, filename)
                mtime = os.path.getmtime(filepath)
                if earliest_mtime is None or mtime < earliest_mtime:
                    earliest_mtime = mtime

            if earliest_mtime:
                datestamp = datetime.fromtimestamp(
                    earliest_mtime, tz=timezone.utc
                ).strftime(_DATESTAMP_STRFTIME)
                print(
                    f"Warning: No datestamp found in filenames, using earliest file modification time: {datestamp}"
                )
            else:
                # Fallback to current time if no files found
                datestamp = datetime.now(timezone.utc).strftime(_DATESTAMP_STRFTIME)
                print(
                    f"Warning: No benchmark files found for datestamp, using current time: {datestamp}"
                )

        # Parse files based on format
        metrics = []
        if file_format == "json":
            # Parse JSON files - accept any .json/.json.zst file that parses successfully
            for filename in os.listdir(input_dir):
                if filename.endswith(_JSON_EXT) or filename.endswith(_JSON_ZST_EXT):
                    filepath = os.path.join(input_dir, filename)
                    try:
                        metric = parse_warp_json(filepath)
                        if metric:
                            metrics.append(metric)
                            print(f"  Parsed: {filename}")
                    except (KeyError, ValueError) as e:
                        print(f"  Skipping {filename}: {e}")
        else:  # file_format == 'text'
            # Existing logic for .out files
            for filename in os.listdir(input_dir):
                if filename.endswith(".out"):
                    filepath = os.path.join(input_dir, filename)
                    try:
                        metric = parse_warp_file(filepath)
                        if metric:
                            metrics.append(metric)
                    except ValueError as e:
                        print(f"Warning: Skipping {filename}: {e}")

        if not metrics:
            parser.error("No valid GET benchmark files found")

        # Write JSON if requested
        if args.to_json:
            json_path = os.path.join(output_dir, f"{datestamp}-analyzed.json.zst")
            try:
                write_json(metrics, json_path, datestamp)
                print(f"Wrote analyzed metrics to {json_path}")

                # Report file size
                file_size_mb = os.path.getsize(json_path) / (1024 * 1024)
                print(f"  File size: {file_size_mb:.2f} MB")
            except (ValueError, OSError, ImportError) as e:
                print(f"Warning: Failed to write JSON: {e}")

    # Ensure output directory exists
    os.makedirs(output_dir, exist_ok=True)

    # No longer filtering metrics here - it's now done inside print_table for the summary table only
    if not metrics:
        parser.error("No metrics available")

    # Determine if multi-node by checking if any run used more than 1 node
    is_multi_node = any(m.nodes > 1 for m in metrics)

    # For plotting, filter the metrics according to the specified filters
    filtered_plot_metrics = metrics
    if only_sizes or only_threads:
        filtered_plot_metrics = filter_metrics(metrics, only_sizes, only_threads)
        if not filtered_plot_metrics:
            print("Warning: No metrics match the specified filters for plotting")

    plot_metrics(
        filtered_plot_metrics,
        datestamp,
        output_dir,
        is_multi_node,
    )

    # Generate per-client comparison plots if requested
    if args.per_client_plots:
        plot_per_client_metrics(
            filtered_plot_metrics,
            datestamp,
            output_dir,
            is_multi_node,
            args.client_outlier_threshold,
            args.client_min_underperform_segments,
        )

    print(f"\nPlots and reports saved to: {os.path.abspath(output_dir)}")

    # Print summary table last so it stays visible in terminal
    mirror_stdout_to_file(
        os.path.join(output_dir, REPORT_TXT_FILENAME),
        print_table,
        metrics,
        is_multi_node,
        only_sizes,
        only_threads,
    )


if __name__ == "__main__":
    main()
