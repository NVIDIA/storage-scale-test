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

"""Script to analyze elbencho filesystem benchmark results.

Optional fields capture nv-elbencho-sweep / _elbencho_functions.sh log lines when
present in .out files: write-only dir, read-from path, treescan stats (raw + parsed).
Treescan may appear only in per-task job .out files; when the aggregate CSV .out
lacks it, the result directory is scanned for a Treescan line in a sibling .out that
matches the same io_size and datestamp as the benchmark being parsed (see
``apply_treescan_from_directory_scan``).
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import re
import sys
from pathlib import Path
import traceback
from collections import defaultdict
from dataclasses import dataclass, fields, asdict, field
from functools import partial
from itertools import chain
from typing import (
    Any,
    DefaultDict,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    TypedDict,
    cast,
)

import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import numpy as np

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Local import after sys.path: repo root must be on path first.
# pylint: disable=wrong-import-position
from lib.env_used_yaml import apply_env_used_to_metrics, load_env_used_yaml
from lib.elbencho_live_report import (
    DomainAnalysis,
    LiveAnalysis,
    LiveDomainKey,
    LiveFileMetadata,
    analyze_live_csv,
    collect_selected_client_detail,
    plot_aggregate_timeseries,
    plot_client_timeseries,
    plot_underperformance_heatmap,
    select_clients,
    select_heatmap_clients,
    write_client_summaries,
)
from lib.join_datestamps import join_datestamps, join_datestamps_for_filename
from lib.parse_only_sizes import parse_only_sizes_arg
from lib.reporting_common import histogram_axis_ranges
from lib.stdout_report_file import (  # pylint: disable=wrong-import-position
    REPORT_TXT_FILENAME,
    mirror_stdout_to_file,
)


class PlotMetadata(TypedDict):
    """Plotting metadata from metrics_metadata plus plot_metadata."""

    sn_or_mn_str: str
    sn_or_mn: str
    dio_or_bio: str
    io_mode_str: str
    io_order: str
    datestamps: Set[str]
    x_values: List[int]
    colors: List[str] | np.ndarray
    markers: List[str]
    x_label: str


# Matplotlib legend placement
LEGEND_LOC_CENTER_LEFT = (
    "center left"  # outside plot, to the right of axes (single-node)
)
LEGEND_LOC_LOWER_RIGHT = (
    "lower right"  # inside plot (multi-node throughput / IOPS vs. nodes)
)

# Elbencho CSV export: IO latency columns (values in microseconds)
_IO_LAT_US_COL_PREFIX = "IO lat us "
CSV_COL_IO_LAT_US_MIN = _IO_LAT_US_COL_PREFIX + "[min]"
CSV_COL_IO_LAT_US_AVG = _IO_LAT_US_COL_PREFIX + "[avg]"
CSV_COL_IO_LAT_US_MAX = _IO_LAT_US_COL_PREFIX + "[max]"
CSV_COL_TIME_MS_FIRST = "time ms [first]"
CSV_COL_TIME_MS_LAST = "time ms [last]"

# Labeled CSV uses "ISO date" in the first column; --nocsvlabels read-only runs omit the header.
_ELBE_CSV_LABEL_FIRST_CELL = "ISO date"
_ELBE_CSV_TS_FIRST_LINE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_ELBE_CSV_KNOWN_PHASE_OPS = frozenset(
    {
        "READ",
        "WRITE",
        "SYNC",
        "MIGRATE",
        "FSYNC",
        "CREATE",
        "DELETE",
        "STAT",
        "LIST",
        "MKDIR",
        "RMDIR",
        "RMFILES",
        "REMOVE",
        "RENAME",
        "SYMLINK",
    }
)
# Offsets from the "operation" column match Statistics::printPhaseResultsToStringVec (elbencho).
# Phase block order: operation; time ms x2; entries/s x2; IOPS x2; MiB/s x2; CPU% x2; entries x2; MiB x2; ...
_ELBE_CSV_OFF_OP_TO_IOPS_FIRST = 5
_ELBE_CSV_OFF_OP_TO_MIBS_FIRST = 7
_ELBE_CSV_OFF_OP_TO_IO_LAT_MIN = 18
_ELBE_CSV_FILE_SIZE_COLS_BEFORE_OP = 8


def eprint(*args, **kwargs):
    """Print to stderr (for warnings, progress messages, etc.)."""
    print(*args, file=sys.stderr, **kwargs)


@dataclass
class ElbenchoMetrics:
    """Container for parsed Elbencho metrics."""

    # Core identification metadata
    nodes: int  # Node count from filename c_XXX
    io_size: str  # IO size from filename (can include comma for dif r/w sizes)
    threads: int  # Thread count from filename s_XXX
    io_depth: int  # IO depth from filename d_XXX (defaults to 1 for old format)
    operation: str  # Operation type (READ/WRITE)
    datestamp: str  # Datestamp from filename format YYYYMMDDZHHMISS
    is_multi_node: bool  # Whether this metric was from a multi-node run
    command: str  # The command used to run elbencho
    file_size_bytes: int  # file size in bytes
    direct_io: int  # Direct IO flag: 1 = DirectIO (dio), 0 = BufferedIO (bio)
    random_io: int  # Random IO flag: 1 = Random, 0 = Sequential

    # Throughput metrics
    iops: float  # IOPS [first] value
    throughput_mib_s: float  # MiB/s [first] value
    throughput_mb_s: (
        float  # Converted to MB/s (kept for CSV compatibility, display as GB/s)
    )
    throughput_gbps: float  # Converted to Gb/s

    # Latency metrics (from .out file "IO latency" lines)
    min_lat_sec: float  # min latency in seconds
    avg_lat_sec: float  # avg latency in seconds
    max_lat_sec: float  # max latency in seconds

    # Latency percentiles (from "IO lat % us" line)
    lat_pct_1: float  # 1% latency in seconds
    lat_pct_50: float  # 50% latency in seconds
    lat_pct_75: float  # 75% latency in seconds
    lat_pct_99: float  # 99% latency in seconds

    # Configured phase duration (seconds), from --timelimit in CSV command or sweep log
    io_duration_sec: int = 0
    # Measured phase wall time from CSV: time ms [last] (ms from phase start to last completion); 0 if unknown
    phase_wall_duration_ms: int = 0
    # Measured time to first completion from CSV: time ms [first]; pairs with elbencho [first] IOPS/MiB/s stats
    phase_first_duration_ms: int = 0
    # nv-elbencho-sweep -s/--single (env_used.yaml single_option: 1): one combined elbencho run for the sweep
    sweep_single_option: bool = False

    # Optional sweep log annotations (from nv-elbencho-sweep / _elbencho_functions.sh .out text)
    write_only_data_dir: str = ""  # path from ELBENCHO_WRITE_ONLY_DATA_DIR=
    sweep_read_from_path: str = (
        ""  # path from Read-from: / ELBENCHO_SWEEP_READ_FROM= (do not infer from --treescan)
    )
    treescan_size_stats_line: str = (
        ""  # Treescan file sizes / first-file line (verbatim)
    )
    treescan_file_count: int = 0
    treescan_avg_bytes: int = 0
    treescan_min_bytes: int = 0
    treescan_max_bytes: int = 0
    treescan_first_file_bytes: int = 0  # when only first-file fallback line exists

    # Single-file mode annotations (from .out log lines)
    is_single_big_file: bool = False
    all_nodes_all_data: bool = False

    # Raw shared-workload configuration from env_used.yaml. These values are
    # intentionally separate from the effective per-execution metadata below.
    configured_file_layout: str = ""
    configured_files_per_node: str = ""
    configured_file_size: str = ""

    # Explicit per-execution workload metadata. Counts remain strings so the
    # metadata null marker stays distinct from a valid zero-file staged tree.
    workload_layout: str = ""
    dataset_count_source: str = ""
    treefile_source: str = ""
    treefile_cache_publish_outcome: str = ""
    requested_files_per_node: str = ""
    effective_files_per_node: str = ""
    dataset_files_total: str = ""
    dataset_bytes_total: str = ""
    reader_nodes: str = ""
    reader_threads_per_node: str = ""
    reader_iodepth: str = ""
    files_per_reader_node: str = ""
    termination_mode: str = ""
    configured_duration_seconds: str = ""
    effective_timelimit_seconds: str = ""
    completion_state: str = ""
    failure_cleanup_state: str = ""
    write_expected_files: str = ""
    write_expected_bytes: str = ""
    write_completed_files: str = ""
    write_completed_bytes: str = ""
    write_elapsed_time_ms: str = ""
    write_completion_state: str = ""
    read_expected_files: str = ""
    read_expected_bytes: str = ""
    read_completed_files: str = ""
    read_completed_bytes: str = ""
    read_elapsed_time_ms: str = ""
    read_completion_state: str = ""
    delete_expected_files: str = ""
    delete_completed_files: str = ""
    delete_elapsed_time_ms: str = ""
    delete_completion_state: str = ""
    write_delete_elapsed_time_ms: str = ""
    lifecycle_elapsed_time_ms: str = ""

    # Histogram data (optional, for visualization); latency histogram data
    # as {time_sec: count}
    histogram: Dict[float, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionCoordinates:
    """Coordinates and configured layout captured in one reified execution."""

    nodes: int
    io_size: str
    threads: int
    io_depth: int
    configured_layout: str


class AnnotatedSizeGroup(TypedDict):
    """One size-group bucket for plotting and reporting (from cluster_metrics_for_reporting)."""

    size_group: List[str]
    group_name: str
    metrics_by_size: Dict[str, List[ElbenchoMetrics]]


# is_multi_node -> operation -> direct_io -> annotated plot groups for reporting
ClusteredAnnotatedSizeGroups = DefaultDict[
    bool, DefaultDict[str, DefaultDict[int, List[AnnotatedSizeGroup]]]
]


def bytes_to_elbencho_size_string(file_size_bytes: int) -> str:
    """
    Convert a byte count to an Elbencho size string with appropriate suffix.

    Args:
        file_size_bytes: Size in bytes

    Returns:
        A string with an integer and a size suffix (K, M, or G)
        that most closely represents the input size
    """
    # Define the size units
    K = 1024
    M = 1024 * K
    G = 1024 * M

    # Find the appropriate unit
    if file_size_bytes >= G and file_size_bytes % G == 0:
        return f"{file_size_bytes // G}G"
    elif file_size_bytes >= M and file_size_bytes % M == 0:
        return f"{file_size_bytes // M}M"
    elif file_size_bytes >= K and file_size_bytes % K == 0:
        return f"{file_size_bytes // K}K"

    # If not an exact multiple, find the closest representation
    if file_size_bytes >= G:
        return f"{round(file_size_bytes / G)}G"
    elif file_size_bytes >= M:
        return f"{round(file_size_bytes / M)}M"
    else:
        return f"{max(1, round(file_size_bytes / K))}K"


# Matches elbencho CSV "command" field (--timelimit appears as quoted argv tokens)
TIMELIMIT_CMD_PATTERN_TUP = (
    re.compile(r'--timelimit"\s+"(\d+)"', re.IGNORECASE),
    re.compile(r"--timelimit=(\d+)", re.IGNORECASE),
    re.compile(r"--timelimit\s+(\d+)", re.IGNORECASE),
)

# Sweep summary line from print_elbencho_sweep_summary (lib/env_functions.sh)
_WR_IO_DURATION_LOG_RE = re.compile(
    r"(?:W/R IO Duration|Read IO Duration|Write IO Duration):\s*(\d+)"
)

# nv-elbencho-sweep / run_elbencho_io_sweep_iteration auxiliary lines (lib/_elbencho_functions.sh)
_RE_WRITE_ONLY_DATA_DIR = re.compile(
    r"^ELBENCHO_WRITE_ONLY_DATA_DIR=(.+)$", re.MULTILINE
)
_RE_TREESCAN_SIZES_LINE = re.compile(
    r"^Treescan file sizes \(bytes\):.*$", re.MULTILINE
)
_RE_TREESCAN_FIRST_FILE_LINE = re.compile(
    r"^Treescan first file size \(bytes\):.*$", re.MULTILINE
)
_RE_SWEEP_READ_FROM = re.compile(
    r"^(?:ELBENCHO_SWEEP_READ_FROM=|Read-from:\s*)(.+)$", re.MULTILINE
)
_RE_TREESCAN_SIZES_NUMS = re.compile(
    r"^Treescan file sizes \(bytes\): count=(\d+) avg=(\d+) min=(\d+) max=(\d+)\s*$"
)
_RE_TREESCAN_FIRST_FILE_NUM = re.compile(
    r"^Treescan first file size \(bytes\): (\d+)\s*$"
)

# nv-elbencho-sweep SLURM log: elbencho-<DS>-<max_nodes>-<nodes>-<io_size>-<jobid>.out
_SLURM_ELBENCHO_JOB_OUT_RE = re.compile(
    r"^elbencho-(\d{8}Z\d{6})-\d+-\d+-(.+)-\d+\.out$"
)
_REIFIED_COORDS_RE = re.compile(
    r"^# Auto-generated; do not edit\. Coords: "
    r"nodes=(\d+) io_size=(\S+) thread_count=(\d+) io_depth=(\d+)$",
    re.MULTILINE,
)
_REIFIED_LAYOUT_RE = re.compile(
    r"^export ELBENCHO_FILE_LAYOUT=(['\"]?)([a-z-]+)\1$", re.MULTILINE
)
_WORKLOAD_METADATA_KEYS = (
    "dataset_count_source",
    "treefile_source",
    "treefile_cache_publish_outcome",
    "requested_files_per_node",
    "effective_files_per_node",
    "dataset_files_total",
    "dataset_bytes_total",
    "reader_nodes",
    "reader_threads_per_node",
    "reader_iodepth",
    "files_per_reader_node",
    "termination_mode",
    "configured_duration_seconds",
    "effective_timelimit_seconds",
    "completion_state",
    "failure_cleanup_state",
    "write_expected_files",
    "write_expected_bytes",
    "write_completed_files",
    "write_completed_bytes",
    "write_elapsed_time_ms",
    "write_completion_state",
    "read_expected_files",
    "read_expected_bytes",
    "read_completed_files",
    "read_completed_bytes",
    "read_elapsed_time_ms",
    "read_completion_state",
    "delete_expected_files",
    "delete_completed_files",
    "delete_elapsed_time_ms",
    "delete_completion_state",
    "write_delete_elapsed_time_ms",
    "lifecycle_elapsed_time_ms",
)
_WORKLOAD_DISTRIBUTED_CLEANUP_DEFAULTS = {
    "write_elapsed_time_ms": "null",
    "read_elapsed_time_ms": "null",
    "delete_expected_files": "null",
    "delete_completed_files": "null",
    "delete_elapsed_time_ms": "null",
    "delete_completion_state": "not_applicable",
    "write_delete_elapsed_time_ms": "null",
    "lifecycle_elapsed_time_ms": "null",
}
_WORKLOAD_LEGACY_METADATA_KEYS = frozenset(_WORKLOAD_METADATA_KEYS) - frozenset(
    _WORKLOAD_DISTRIBUTED_CLEANUP_DEFAULTS
)
_WORKLOAD_DECIMAL_OR_NULL_KEYS = frozenset(
    {
        "requested_files_per_node",
        "effective_files_per_node",
        "reader_nodes",
        "reader_threads_per_node",
        "reader_iodepth",
        "files_per_reader_node",
        "configured_duration_seconds",
        "effective_timelimit_seconds",
        "write_expected_files",
        "write_expected_bytes",
        "write_completed_files",
        "write_completed_bytes",
        "write_elapsed_time_ms",
        "read_expected_files",
        "read_expected_bytes",
        "read_completed_files",
        "read_completed_bytes",
        "read_elapsed_time_ms",
        "delete_expected_files",
        "delete_completed_files",
        "delete_elapsed_time_ms",
        "write_delete_elapsed_time_ms",
        "lifecycle_elapsed_time_ms",
    }
)
_WORKLOAD_DATASET_TOTAL_KEYS = frozenset({"dataset_files_total", "dataset_bytes_total"})
_WORKLOAD_ENUM_VALUES = {
    "dataset_count_source": frozenset({"configuration", "treefile"}),
    "treefile_source": frozenset(
        {"null", "cache_hit", "cache_miss_scan", "cache_unavailable_scan"}
    ),
    "treefile_cache_publish_outcome": frozenset(
        {
            "pending",
            "reused",
            "created",
            "already_present",
            "not_created",
            "not_applicable",
        }
    ),
    "termination_mode": frozenset(
        {"completion", "time_bounded_repeat", "single_pass_with_time_ceiling"}
    ),
    "completion_state": frozenset(
        {"pending", "completed", "incomplete", "not_applicable_time_based"}
    ),
    "failure_cleanup_state": frozenset({"not_needed", "completed", "failed"}),
    "write_completion_state": frozenset(
        {"not_started", "completed", "incomplete", "not_applicable"}
    ),
    "read_completion_state": frozenset(
        {
            "not_started",
            "completed",
            "incomplete",
            "not_applicable",
            "not_applicable_time_based",
        }
    ),
    "delete_completion_state": frozenset(
        {"not_started", "completed", "incomplete", "not_applicable"}
    ),
}
_CANONICAL_DECIMAL_RE = re.compile(r"^(?:0|[1-9][0-9]*)$")


def _parse_reified_execution(path: Path) -> Optional[ExecutionCoordinates]:
    """Read the coordinate tuple and configured layout from one NNNN.sh."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        eprint(f"Warning: Failed to load reified execution {path}: {exc}")
        return None
    coords_match = _REIFIED_COORDS_RE.search(content)
    if not coords_match:
        eprint(f"Warning: Missing execution coordinates in {path}")
        return None
    layout_match = _REIFIED_LAYOUT_RE.search(content)
    configured_layout = layout_match.group(2) if layout_match else ""
    return ExecutionCoordinates(
        nodes=int(coords_match.group(1)),
        io_size=coords_match.group(2),
        threads=int(coords_match.group(3)),
        io_depth=int(coords_match.group(4)),
        configured_layout=configured_layout,
    )


def _parse_workload_metadata(path: Path) -> Optional[Dict[str, str]]:
    """Read a validated subset of atomic NNNN.workload.tsv metadata."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        eprint(f"Warning: Failed to load workload metadata {path}: {exc}")
        return None
    parsed: Dict[str, str] = {}
    for line in lines:
        parts = line.split("\t")
        if len(parts) != 2 or not parts[0] or parts[0] in parsed:
            eprint(f"Warning: Invalid workload metadata record in {path}")
            return None
        parsed[parts[0]] = parts[1]
    parsed_keys = set(parsed)
    if parsed_keys == _WORKLOAD_LEGACY_METADATA_KEYS:
        parsed.update(_WORKLOAD_DISTRIBUTED_CLEANUP_DEFAULTS)
    elif parsed_keys != set(_WORKLOAD_METADATA_KEYS):
        eprint(f"Warning: Incomplete or unknown workload metadata in {path}")
        return None
    if not all(_workload_value_is_valid(key, value) for key, value in parsed.items()):
        eprint(f"Warning: Invalid workload metadata value in {path}")
        return None
    if not _workload_cleanup_is_coherent(parsed):
        eprint(f"Warning: Contradictory workload cleanup metadata in {path}")
        return None
    return parsed


def _workload_value_is_valid(key: str, value: str) -> bool:
    """Validate one field using the workload metadata's documented domain."""
    if key in _WORKLOAD_DECIMAL_OR_NULL_KEYS:
        return value == "null" or bool(_CANONICAL_DECIMAL_RE.fullmatch(value))
    if key in _WORKLOAD_DATASET_TOTAL_KEYS:
        return value == "pending" or bool(_CANONICAL_DECIMAL_RE.fullmatch(value))
    allowed_values = _WORKLOAD_ENUM_VALUES.get(key)
    return allowed_values is not None and value in allowed_values


def _workload_completed_cleanup_is_coherent(metadata: Dict[str, str]) -> bool:
    """Validate exact counts and timings for a completed RMFILES phase."""
    required = (
        "delete_expected_files",
        "delete_completed_files",
        "delete_elapsed_time_ms",
        "write_elapsed_time_ms",
        "write_delete_elapsed_time_ms",
        "lifecycle_elapsed_time_ms",
    )
    if any(metadata[key] == "null" for key in required):
        return False
    if metadata["delete_expected_files"] != metadata["delete_completed_files"]:
        return False
    combined = int(metadata["write_elapsed_time_ms"]) + int(
        metadata["delete_elapsed_time_ms"]
    )
    if combined != int(metadata["write_delete_elapsed_time_ms"]):
        return False
    return True


def _workload_cleanup_is_coherent(metadata: Dict[str, str]) -> bool:
    """Reject contradictory distributed-cleanup states and counters."""
    state = metadata["delete_completion_state"]
    nullable = (
        "delete_expected_files",
        "delete_completed_files",
        "delete_elapsed_time_ms",
        "write_delete_elapsed_time_ms",
        "lifecycle_elapsed_time_ms",
    )
    if state == "not_applicable":
        return all(metadata[key] == "null" for key in nullable)
    if state == "not_started":
        return metadata["delete_expected_files"] != "null" and all(
            metadata[key] == "null" for key in nullable[1:]
        )
    if state == "completed":
        return _workload_completed_cleanup_is_coherent(metadata)
    if metadata["completion_state"] != "incomplete":
        return False
    return metadata["delete_expected_files"] != "null"


def _execution_workload_layout(
    coordinates: ExecutionCoordinates, metadata: Dict[str, str]
) -> str:
    """Return the explicit workload layout represented by execution metadata."""
    if metadata["dataset_count_source"] == "treefile":
        return "staged-tree"
    if coordinates.configured_layout:
        return coordinates.configured_layout
    return ""


def _execution_coordinate_key(
    nodes: int, io_size: str, threads: int, io_depth: int
) -> Tuple[int, str, int, int]:
    """Return the stable join key shared by metrics and NNNN.sh coordinates."""
    return (nodes, io_size, threads, io_depth)


def load_execution_workloads(
    result_dir: str,
) -> Dict[Tuple[int, str, int, int], Tuple[ExecutionCoordinates, Dict[str, str]]]:
    """Load workload metadata indexed by reified execution coordinates."""
    workloads = {}
    executions_dir = Path(result_dir) / "executions"
    for execution_path in sorted(executions_dir.glob("[0-9]*.sh")):
        if not execution_path.stem.isdigit():
            continue
        metadata_path = execution_path.with_suffix(".workload.tsv")
        if not metadata_path.is_file():
            continue
        coordinates = _parse_reified_execution(execution_path)
        metadata = _parse_workload_metadata(metadata_path)
        if coordinates is None or metadata is None:
            continue
        key = _execution_coordinate_key(
            coordinates.nodes,
            coordinates.io_size,
            coordinates.threads,
            coordinates.io_depth,
        )
        if key in workloads:
            eprint(
                f"Warning: Duplicate reified execution coordinates in {execution_path}"
            )
            continue
        workloads[key] = (coordinates, metadata)
    return workloads


def _apply_execution_workload(
    metric: ElbenchoMetrics,
    coordinates: ExecutionCoordinates,
    metadata: Dict[str, str],
) -> None:
    """Apply one complete explicit workload record to a joined metric."""
    metric.workload_layout = _execution_workload_layout(coordinates, metadata)
    for key in _WORKLOAD_METADATA_KEYS:
        setattr(metric, key, metadata[key])


def apply_execution_workloads(result_dir: str, metrics: List[ElbenchoMetrics]) -> None:
    """Join NNNN.workload.tsv records to parsed metrics through NNNN.sh."""
    workloads = load_execution_workloads(result_dir)
    for metric in metrics:
        key = _execution_coordinate_key(
            metric.nodes, metric.io_size, metric.threads, metric.io_depth
        )
        workload = workloads.get(key)
        if workload:
            _apply_execution_workload(metric, *workload)


def metric_display_file_size_bytes(metric: ElbenchoMetrics) -> int:
    """Bytes to show in File Size headers: treescan avg, else first-file, else CSV."""
    if metric.treescan_avg_bytes > 0:
        return metric.treescan_avg_bytes
    if metric.treescan_first_file_bytes > 0:
        return metric.treescan_first_file_bytes
    return metric.file_size_bytes


def file_size_header_prefix_for_metrics(metrics: List[ElbenchoMetrics]) -> str:
    """Return header prefix: 'Single File Size: ', 'Avg File Size: ', or 'File Size: '."""
    if any(m.is_single_big_file for m in metrics):
        return "Single File Size: "
    if any(m.treescan_avg_bytes > 0 for m in metrics):
        return "Avg File Size: "
    return "File Size: "


def _all_nodes_all_data_suffix(metrics: List[ElbenchoMetrics]) -> str:
    """Return ' (all-nodes-all-data)' when the flag is set, else ''."""
    if any(m.all_nodes_all_data for m in metrics):
        return " (all-nodes-all-data)"
    return ""


def _apply_treescan_numeric_fields(metric: ElbenchoMetrics, raw_line: str) -> None:
    """Populate treescan_* int fields from one treescan summary line."""
    stripped = raw_line.strip()
    full = _RE_TREESCAN_SIZES_NUMS.match(stripped)
    if full:
        metric.treescan_file_count = int(full.group(1))
        metric.treescan_avg_bytes = int(full.group(2))
        metric.treescan_min_bytes = int(full.group(3))
        metric.treescan_max_bytes = int(full.group(4))
        return
    first_only = _RE_TREESCAN_FIRST_FILE_NUM.match(stripped)
    if first_only:
        metric.treescan_first_file_bytes = int(first_only.group(1))


def _sorted_unique_datestamp_path_pairs(
    metrics: List[ElbenchoMetrics], path_attr: str
) -> List[Tuple[str, str]]:
    """Unique (datestamp, path) pairs for write-only dir or read-from path; sorted."""
    seen: Set[Tuple[str, str]] = set()
    out: List[Tuple[str, str]] = []
    for m in metrics:
        raw = getattr(m, path_attr, "") or ""
        path = raw.strip()
        if not path:
            continue
        pair = (m.datestamp, path)
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    out.sort(key=lambda p: (p[0], p[1]))
    return out


def print_sweep_auxiliary_lines_for_metrics(op_metrics: List[ElbenchoMetrics]) -> None:
    """Echo write-only / read-from paths when present (one line per datestamp + path)."""
    for ds, path in _sorted_unique_datestamp_path_pairs(
        op_metrics, "write_only_data_dir"
    ):
        print(f"ELBENCHO_WRITE_ONLY_DATA_DIR (datestamp {ds}): {path}")
    for ds, path in _sorted_unique_datestamp_path_pairs(
        op_metrics, "sweep_read_from_path"
    ):
        print(f"Read-from (datestamp {ds}): {path}")


def print_workload_metadata_for_metrics(op_metrics: List[ElbenchoMetrics]) -> None:
    """Print explicit per-execution layout, volume, and reader topology."""
    records = {
        (
            metric.workload_layout,
            metric.dataset_count_source,
            metric.treefile_source,
            metric.requested_files_per_node,
            metric.effective_files_per_node,
            metric.dataset_files_total,
            metric.dataset_bytes_total,
            metric.reader_nodes,
            metric.reader_threads_per_node,
            metric.reader_iodepth,
            metric.write_elapsed_time_ms,
            metric.read_elapsed_time_ms,
            metric.delete_expected_files,
            metric.delete_completed_files,
            metric.delete_elapsed_time_ms,
            metric.delete_completion_state,
            metric.write_delete_elapsed_time_ms,
            metric.lifecycle_elapsed_time_ms,
            metric.completion_state,
            metric.failure_cleanup_state,
        )
        for metric in op_metrics
        if metric.workload_layout
    }
    for record in sorted(records):
        (
            layout,
            count_source,
            treefile_source,
            requested,
            effective,
            files,
            byte_count,
            nodes,
            threads,
            depth,
            write_elapsed,
            read_elapsed,
            delete_expected,
            delete_completed,
            delete_elapsed,
            delete_state,
            write_delete_elapsed,
            lifecycle_elapsed,
            completion_state,
            fallback_state,
        ) = record
        print(
            "Workload: "
            f"layout={layout} dataset_count_source={count_source} "
            f"treefile_source={treefile_source} "
            f"requested_files_per_node={requested} "
            f"effective_files_per_node={effective} dataset_files={files} "
            f"dataset_bytes={byte_count} reader_nodes={nodes} "
            f"reader_threads_per_node={threads} reader_iodepth={depth}"
        )
        print(
            "Lifecycle: "
            f"write_elapsed_ms={write_elapsed} read_elapsed_ms={read_elapsed} "
            f"delete_expected_files={delete_expected} "
            f"delete_completed_files={delete_completed} "
            f"delete_elapsed_ms={delete_elapsed} "
            f"write_delete_elapsed_ms={write_delete_elapsed} "
            f"lifecycle_elapsed_ms={lifecycle_elapsed} "
            f"delete_state={delete_state} completion_state={completion_state} "
            f"fallback_state={fallback_state}"
        )


def apply_sweep_auxiliary_log_lines(
    content: str, metrics: List[ElbenchoMetrics]
) -> None:
    """Fill optional sweep fields from .out log lines (write-only, read-from, treescan stats)."""
    if not metrics:
        return
    wo = _RE_WRITE_ONLY_DATA_DIR.search(content)
    rf = _RE_SWEEP_READ_FROM.search(content)
    ts_line = _RE_TREESCAN_SIZES_LINE.search(content)
    if not ts_line:
        ts_line = _RE_TREESCAN_FIRST_FILE_LINE.search(content)
    for metric in metrics:
        if wo:
            metric.write_only_data_dir = wo.group(1).strip()
        if rf:
            metric.sweep_read_from_path = rf.group(1).strip()
        if ts_line:
            raw_ts = ts_line.group(0).strip()
            metric.treescan_size_stats_line = raw_ts
            _apply_treescan_numeric_fields(metric, raw_ts)


def _out_basename_without_ext(path: str) -> str:
    base = os.path.basename(path)
    return base[:-4] if base.endswith(".out") else base


def _treescan_sibling_out_paths(
    out_paths: Sequence[str], target_io: str, target_ds: str
) -> List[str]:
    """Paths to .out files tied to this io_size and datestamp (merged name or SLURM log)."""
    found: List[str] = []
    for path in out_paths:
        base = os.path.basename(path)
        stem = _out_basename_without_ext(path)
        params = parse_benchmark_filename(stem)
        if (
            params
            and params["io_size"] == target_io
            and params["datestamp"] == target_ds
        ):
            found.append(path)
            continue
        job_m = _SLURM_ELBENCHO_JOB_OUT_RE.match(base)
        if job_m and job_m.group(1) == target_ds and job_m.group(2) == target_io:
            found.append(path)
    return sorted(found)


def _apply_first_treescan_line_from_out_file(
    path: str,
    metrics: List[ElbenchoMetrics],
    required_result_name: str = "",
) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return False
    if required_result_name and required_result_name not in content:
        return False
    ts_line = _RE_TREESCAN_SIZES_LINE.search(content)
    if not ts_line:
        ts_line = _RE_TREESCAN_FIRST_FILE_LINE.search(content)
    if not ts_line:
        return False
    raw_ts = ts_line.group(0).strip()
    for metric in metrics:
        metric.treescan_size_stats_line = raw_ts
        _apply_treescan_numeric_fields(metric, raw_ts)
    return True


def apply_treescan_from_directory_scan(
    result_dir: str,
    metrics: List[ElbenchoMetrics],
    base_filename: str = "",
) -> None:
    """Fill treescan_* from a sibling .out when the aggregate benchmark .out omits Treescan.

    SLURM step logs (e.g. elbencho-<date>-<max>-<nodes>-r64K-<jobid>.out) often include
    ``Treescan file sizes`` while the merged elbencho-r64K-c_*.out may not.
    Per-execution dispatch also searches executions/NNNN.log and requires the
    exact benchmark result name before applying its treescan metadata.

    Prefers .out files that match this batch's io_size and datestamp so multi-io-size
    sweeps do not reuse another size's treescan (sorted glob would otherwise favor e.g.
    ``1M`` over ``r64K``).
    """
    if not metrics:
        return
    if any(
        m.treescan_avg_bytes > 0 or m.treescan_first_file_bytes > 0 for m in metrics
    ):
        return
    out_paths = sorted(glob.glob(os.path.join(result_dir, "*.out")))
    target_io = metrics[0].io_size
    target_ds = metrics[0].datestamp
    preferred = _treescan_sibling_out_paths(out_paths, target_io, target_ds)
    for path in preferred:
        if _apply_first_treescan_line_from_out_file(path, metrics):
            return
    if base_filename:
        # Per-execution dispatch writes operator treescan statistics to
        # executions/NNNN.log. Match the log to this result by the .out name
        # printed near the start of that same execution.
        target_result_name = f"{os.path.basename(base_filename)}.out"
        execution_logs = [
            path
            for path in sorted(
                glob.glob(os.path.join(result_dir, "executions", "*.log"))
            )
            if os.path.splitext(os.path.basename(path))[0].isdigit()
        ]
        for path in execution_logs:
            if _apply_first_treescan_line_from_out_file(
                path, metrics, target_result_name
            ):
                return
    if not preferred:
        for path in out_paths:
            if _apply_first_treescan_line_from_out_file(path, metrics):
                return


def parse_timelimit_seconds_from_command(command: str) -> int:
    """Return --timelimit value from an elbencho command string, or 0 if absent."""
    if not command:
        return 0
    for pattern in TIMELIMIT_CMD_PATTERN_TUP:
        match = pattern.search(command)
        if match:
            return int(match.group(1))
    return 0


def section_duration_label_sec(metrics: List[ElbenchoMetrics]) -> str:
    """Return '300s - ' style fragment for text section headers, or '' if unknown."""
    values = sorted({m.io_duration_sec for m in metrics if m.io_duration_sec > 0})
    if not values:
        return ""
    if len(values) == 1:
        return f"{values[0]}s - "
    return f"{values[0]}-{values[-1]}s - "


def _phase_wall_duration_ms_from_csv_row(row: Dict[str, str]) -> int:
    """Return READ/WRITE wall duration (ms): phase start to last completion via time ms [last].

    time ms [first] is the first thread's completion time; last-first is only the straggler span.
    """
    raw_last = row.get(CSV_COL_TIME_MS_LAST, "")
    if not raw_last:
        return 0
    try:
        last_ms = int(float(raw_last))
    except (ValueError, TypeError):
        return 0
    return last_ms if last_ms > 0 else 0


def _phase_first_duration_ms_from_csv_row(row: Dict[str, str]) -> int:
    """Return ms from phase start to first completion via CSV time ms [first]."""
    raw_first = row.get(CSV_COL_TIME_MS_FIRST, "")
    if not raw_first:
        return 0
    try:
        first_ms = int(float(raw_first))
    except (ValueError, TypeError):
        return 0
    return first_ms if first_ms > 0 else 0


def format_dur_xmys_ms(wall_ms: int) -> str:
    """Format wall-clock milliseconds as XmYs using whole seconds (rounded), or '' if unknown."""
    if wall_ms <= 0:
        return ""
    total_sec = max(0, int(round(wall_ms / 1000)))
    minutes, seconds = divmod(total_sec, 60)
    return f"{minutes}m{seconds}s"


def _metric_should_show_csv_phase_dur(metric: ElbenchoMetrics) -> bool:
    """Whether Dur / DurTot columns should include CSV phase times for this row.

    - **Dur** uses ``phase_first_duration_ms`` (CSV ``time ms [first]``), aligned with elbencho
      ``[first]`` IOPS/MiB/s.
    - **DurTot** uses ``phase_wall_duration_ms`` (CSV ``time ms [last]``), total wall time to last
      completion.

    - If ``env_used.yaml`` has ``single_option: 1`` (nv-elbencho-sweep ``-s`` / ``--single``), we show
      these columns whenever at least one of the phase times is set (see ``apply_env_used_to_metrics``).
    - Single-big-file **buffered** IO: the read phase omits ``--infloop``, so wall time is driven by one
      logical pass (file size / aggregate rate), not by ``--timelimit`` even when timelimit appears in
      the command line. Show columns whenever we have a phase time (``is_single_big_file``
      from CSV or ``env_used.yaml`` via ``apply_env_used_to_metrics``).
    - Otherwise: show only when there is no configured phase duration (``io_duration_sec == 0``
      after CSV command + optional ``W/R IO Duration`` in ``.out``), so timed sweeps keep the columns off.
    """
    if metric.phase_wall_duration_ms <= 0 and metric.phase_first_duration_ms <= 0:
        return False
    if metric.sweep_single_option:
        return True
    if metric.is_single_big_file and metric.direct_io == 0:
        return True
    return metric.io_duration_sec == 0


def section_file_size_label_bytes(metrics: List[ElbenchoMetrics]) -> str:
    """Return 'File Size: 1G - ' or 'Avg File Size: 49M - ' for text headers, or ''.

    Prefers treescan avg (or first-file fallback) over CSV file_size_bytes when set.
    """
    values = sorted(
        {
            metric_display_file_size_bytes(m)
            for m in metrics
            if metric_display_file_size_bytes(m) > 0
        }
    )
    if not values:
        return ""
    prefix = file_size_header_prefix_for_metrics(metrics)
    suffix = _all_nodes_all_data_suffix(metrics)
    if len(values) == 1:
        return f"{prefix}{bytes_to_elbencho_size_string(values[0])}{suffix} - "
    low = bytes_to_elbencho_size_string(values[0])
    high = bytes_to_elbencho_size_string(values[-1])
    return f"{prefix}{low}-{high}{suffix} - "


def strip_hosts_from_command(command: str) -> str:
    """Remove --hosts and its (potentially very long) value from a command string.

    Handles both quoted ("--hosts" "value") and unquoted (--hosts value) forms,
    as well as --hosts=value variants. The hosts list can contain hundreds of
    IPs and adds noise to reports.
    """
    if not command:
        return ""
    # Quoted forms (elbencho COMMAND LINE format): "--hosts" "value" or "--hosts=value"
    result = re.sub(r'\s*"--hosts"\s+"[^"]*"', "", command)
    result = re.sub(r'\s*"--hosts=[^"]*"', "", result)
    # Unquoted forms (fallback): --hosts value or --hosts=value
    result = re.sub(r"\s*--hosts\s+\S+", "", result)
    result = re.sub(r"\s*--hosts=\S+", "", result)
    return result


def parse_size_string(size_str: str) -> Tuple[str, str]:
    """Parse size string, returning write_size and read_size.

    Preserves the optional 'r' prefix which indicates random IO.
    For formats like "1M,4K", returns ("1M", "4K").
    For formats like "r1M,4K", returns ("r1M", "4K").
    For single value formats like "r4K", returns ("r4K", "r4K").
    """
    if "," in size_str:
        write_size, read_size = size_str.split(",", 1)
        return write_size, read_size
    return size_str, size_str


def parse_size_with_order(size_str: str) -> Tuple[str, str]:
    """Parse size string with optional random prefix, returning order and clean size.

    Args:
        size_str: Size string that may have 'r' prefix (e.g., "r4K", "1M", "r10M")

    Returns:
        Tuple of (order, clean_size) where:
        - order is "Rand" or "Seq"
        - clean_size is the size without the 'r' prefix

    Examples:
        "r4K" -> ("Rand", "4K")
        "4K" -> ("Seq", "4K")
        "1M" -> ("Seq", "1M")
        "r1M" -> ("Rand", "1M")
    """
    if size_str.startswith("r"):
        return ("Rand", size_str[1:])
    return ("Seq", size_str)


def normalize_time(time_str: str) -> float:
    """Convert time string (with units) to seconds."""
    if not time_str:
        return 0.0

    # Handle microsecond format (e.g., "12345us")
    if "us" in time_str:
        return float(time_str.replace("us", "")) / 1_000_000

    # Handle millisecond format (e.g., "123.4ms")
    if "ms" in time_str:
        return float(time_str.replace("ms", "")) / 1_000

    # Handle second format (e.g., "1.23s")
    if "s" in time_str and not time_str.endswith("us") and not time_str.endswith("ms"):
        return float(time_str.replace("s", ""))

    # If no unit is provided, assume seconds
    try:
        return float(time_str)
    except ValueError as exc:
        raise ValueError(f"Invalid time format: {time_str}") from exc


def normalize_throughput_to_mb(mib_per_sec: float) -> float:
    """Convert MiB/s to MB/s (1 MiB = 1.048576 MB).

    Note: This value is stored in MB/s for CSV compatibility but displayed as GB/s.
    """
    return mib_per_sec * 1.048576


def mib_to_gbps(mib_per_sec: float) -> float:
    """Convert MiB/s to Gbps (1 MiB = 8.388608 Mbit)."""
    return (mib_per_sec * 8.388608) / 1000


def mb_s_to_gb_s(mb_per_sec: float) -> float:
    """Convert MB/s to GB/s (1 GB = 1000 MB)."""
    return mb_per_sec / 1000


def format_gb_s(gb_per_sec: float) -> str:
    """Format GB/s value with 4 significant figures for consistency with Gb/s column."""
    return format_with_sig_figs(gb_per_sec, 4)


def _format_tick_with_commas(value, pos):  # pylint: disable=unused-argument
    """Format tick label with comma thousands separator for integer-like values.

    Args:
        value: The tick value
        pos: The tick position (required by FuncFormatter but unused)

    Returns:
        Formatted string with commas for values >= 1
    """
    # For integer-like values >= 1, format with commas
    if value >= 1 or value == 0:
        return f"{int(value):,}"
    # For fractional values, use appropriate decimal precision
    elif value >= 0.1:
        return f"{value:.1f}"
    else:
        return f"{value:.2f}"


def _apply_lat_pct_fragments_to_result(
    result: Dict[str, Any], percentiles_str: str
) -> None:
    """Fill lat_pct_* from elbencho IO lat % us line content (values in microseconds)."""
    for pct_item in percentiles_str.split():
        if "<=" not in pct_item:
            continue
        pct, value = pct_item.split("<=", 1)
        pct = pct.strip("%")
        sec = float(value) / 1_000_000
        if pct == "1":
            result["lat_pct_1"] = sec
        elif pct == "50":
            result["lat_pct_50"] = sec
        elif pct == "75":
            result["lat_pct_75"] = sec
        elif pct == "99":
            result["lat_pct_99"] = sec


def _parse_op_hist_fragment(hist_str: str) -> Dict[float, int]:
    """Parse elbencho IO lat hist line fragment into {time_sec: count}."""
    hist_data: Dict[float, int] = {}
    for hist_item in hist_str.split(","):
        hist_item = hist_item.strip()
        if ": " not in hist_item:
            continue
        time_us, count = hist_item.split(": ", 1)
        time_sec = float(time_us) / 1_000_000
        hist_data[time_sec] = int(count)
    return hist_data


def parse_io_latency_section(content: str, operation: str) -> Dict[str, Any]:
    """Parse the IO latency section from an Elbencho .out file.

    Returns a dictionary with min_lat, avg_lat, max_lat, percentiles, and
    histogram.
    """
    result: Dict[str, Any] = {
        "min_lat_sec": 0.0,
        "avg_lat_sec": 0.0,
        "max_lat_sec": 0.0,
        "lat_pct_1": 0.0,
        "lat_pct_50": 0.0,
        "lat_pct_75": 0.0,
        "lat_pct_99": 0.0,
        "histogram": {},
    }

    # Find the operation section
    op_start = content.find(f"{operation}")
    if op_start == -1:
        return result

    # Extract content from operation start to next operation or end of file
    next_op = content.find("OPERATION", op_start + 1)
    if next_op != -1:
        op_content = content[op_start:next_op]
    else:
        op_content = content[op_start:]

    # Find IO latency line and extract min, avg, max values
    io_lat_match = re.search(
        r"IO latency\s+: \[ min=([^ ]+) avg=([^ ]+) max=([^ ]+) \]", op_content
    )
    if io_lat_match:
        min_lat, avg_lat, max_lat = io_lat_match.groups()
        result["min_lat_sec"] = normalize_time(min_lat)
        result["avg_lat_sec"] = normalize_time(avg_lat)
        result["max_lat_sec"] = normalize_time(max_lat)

    # Find IO latency percentiles
    io_lat_pct_match = re.search(r"IO lat % us\s+: \[ ([^\]]+) \]", op_content)
    if io_lat_pct_match:
        _apply_lat_pct_fragments_to_result(result, io_lat_pct_match.group(1))

    # Find IO latency histogram
    io_lat_hist_match = re.search(r"IO lat hist\s+: \[ ([^\]]+) \]", op_content)
    if io_lat_hist_match:
        result["histogram"] = _parse_op_hist_fragment(io_lat_hist_match.group(1))

    return result


def _metric_concurrency(metric: ElbenchoMetrics) -> int:
    """Total IO concurrency (threads * io_depth) for histogram grouping and labels."""
    return metric.threads * metric.io_depth


def _latency_hist_concurrency_label(io_size: str, concurrency: int) -> str:
    """Format primary latency-histogram legend line (io size, concurrency)."""
    return f"{io_size}, {concurrency}c"


def plot_latency_histograms(
    annotated_size_group: AnnotatedSizeGroup,
    output_dir: str,
    is_multi_node: bool,
    operation: str,
    direct_io: int,
    random_io: int,
) -> None:
    """Generate latency histogram plots for a group of IO sizes.

    Args:
        annotated_size_group: Metrics grouped by IO size for one plot/report bucket
        output_dir: Directory to save plots
        is_multi_node: Whether this is a multi-node run
        operation: Operation type (READ or WRITE)
        direct_io: Whether this is a direct IO run
        random_io: Whether this is a random IO run
    """
    metadata = plot_metadata(is_multi_node, direct_io, random_io, annotated_size_group)

    # Generate a color map that will be consistent across all plots
    # Map (io_size, concurrency) to colors; concurrency = threads * io_depth
    # NOTE: categorize_sizes() already sorted size_group with iosize_sorter and operation.
    all_hist_series_keys = sorted(
        {
            (m.io_size, _metric_concurrency(m))
            for m in chain.from_iterable(
                annotated_size_group["metrics_by_size"].values()
            )
        },
        key=lambda x: (iosize_sorter(x[0], operation), x[1]),
    )
    colors = metadata["colors"]
    n_colors = len(colors)
    series_key_to_color = {
        key: colors[i % n_colors] for i, key in enumerate(all_hist_series_keys)
    }

    # Count the number of node/thread values (i.e. the x-axis values
    # of the other plots) within our metrics data that have metrics
    # for every size value in size_group.
    potential_plot_values = sorted(
        {
            metric.nodes if is_multi_node else metric.threads
            for metric in chain.from_iterable(
                annotated_size_group["metrics_by_size"].values()
            )
            if all(
                any(
                    (
                        m.nodes == metric.nodes
                        if is_multi_node
                        else m.threads == metric.threads
                    )
                    for m in ms_for_size
                )
                for ms_for_size in annotated_size_group["metrics_by_size"].values()
            )
        }
    )

    # Select up to 5 values to plot, including min and max
    plot_values = []
    if len(potential_plot_values) <= 5:
        # If we have 5 or fewer values, use all of them
        plot_values = potential_plot_values
    else:
        # Always include min and max
        min_value = potential_plot_values[0]
        max_value = potential_plot_values[-1]

        # Select up to 3 values evenly distributed between min and max (reduced from 4)
        if len(potential_plot_values) > 2:
            middle_values = potential_plot_values[1:-1]
            step_size = max(1, len(middle_values) // 3)
            selected_middle = middle_values[::step_size][:3]  # Take up to 3 values
            plot_values = [min_value] + selected_middle + [max_value]

    # Now we basically turn the metrics_by_size grouping inside out,
    # and group by node/thread count (i.e. the x-axis values of the
    # other plots) and then (io_size, concurrency).  We also filter here for only metrics
    # containing the selected node/thread counts.

    metrics_by_value_and_size = defaultdict(partial(defaultdict, list))
    for metric in chain.from_iterable(annotated_size_group["metrics_by_size"].values()):
        node_or_thread_count = metric.nodes if is_multi_node else metric.threads
        if node_or_thread_count not in plot_values:
            continue
        metrics_by_value_and_size[node_or_thread_count][
            (metric.io_size, _metric_concurrency(metric))
        ].append(metric)

    # Calculate a reasonable figure size based on number of subplots and legend entries.
    # Each (io_size, concurrency) combo contributes up to 3 legend entries (data + 50% + 99%).
    num_plots = len(plot_values)
    num_combos = len(all_hist_series_keys)
    max_legend_entries = num_combos * 3
    legend_ncol = 1 + max_legend_entries // 12
    legend_rows = math.ceil(max_legend_entries / legend_ncol)
    fig_width = 10  # Fixed width
    subplot_height = max(2.5, 2.5 + 0.15 * max(0, legend_rows - 6))
    fig_height = min(40, max(8, subplot_height * num_plots + 1.5))

    # Create figure and axes - don't use sharex initially so we can set custom limits
    fig, axes = plt.subplots(
        num_plots,
        1,
        figsize=(fig_width, fig_height),
        constrained_layout=True,  # Use constrained_layout for better spacing
    )

    # Handle case with only one subplot
    if num_plots == 1:
        axes = [axes]

    # Add title
    fig.suptitle(
        f"{operation} Lat. Hist - "
        f'{annotated_size_group["group_name"]} '
        f'{metadata["io_order"]} - '
        f'{metadata["io_mode_str"]} - '
        f'{join_datestamps(metadata["datestamps"])}',
        fontsize=14,
    )

    # Gather histogram data bounds for consistent axes across subplots.
    axis_series = []
    for value in plot_values:
        value_metrics = chain.from_iterable(metrics_by_value_and_size[value].values())
        for metric in value_metrics:
            if not metric.histogram:
                continue
            sorted_items = sorted(
                [
                    (float(k), v)
                    for k, v in metric.histogram.items()
                    if isinstance(k, (str, float, int)) and isinstance(v, (int, float))
                ]
            )
            if not sorted_items:
                continue
            x_values = [item[0] * 1000 for item in sorted_items]  # Convert to ms
            y_values = [item[1] for item in sorted_items]
            axis_series.append((x_values, y_values))

    axis_ranges = histogram_axis_ranges(axis_series)
    global_min_latency = axis_ranges["min_latency"]
    global_max_latency = axis_ranges["max_latency"]
    global_min_count = axis_ranges["min_count"]
    global_max_count = axis_ranges["max_count"]

    # For each value, plot all size histograms
    for i, value in enumerate(plot_values):
        ax = axes[i]

        # Lines and percentile metadata keyed by (io_size, concurrency)
        series_lines = {}
        # Dictionary to store percentile lines for each (IO size, concurrency) combo
        percentile_lines = {}

        # Group by (IO size, concurrency) and sort by size then concurrency
        sorted_series_keys = sorted(
            metrics_by_value_and_size[value].keys(),
            key=lambda x: (iosize_sorter(x[0], operation), x[1]),
        )

        # Plot metrics in sorted (IO size, concurrency) order for consistency
        for io_size, concurrency in sorted_series_keys:
            for metric in metrics_by_value_and_size[value][(io_size, concurrency)]:
                # Convert histogram to sorted list of (latency, count) pairs
                sorted_items = sorted(
                    [
                        (float(k), v)
                        for k, v in metric.histogram.items()
                        if isinstance(k, (str, float, int))
                        and isinstance(v, (int, float))
                    ]
                )

                if not sorted_items:
                    continue

                x = [item[0] * 1000 for item in sorted_items]  # Convert to ms
                y = [item[1] for item in sorted_items]

                # Plot as line with markers with consistent color for this series
                combo_key = (metric.io_size, _metric_concurrency(metric))
                color = series_key_to_color[combo_key]
                label = _latency_hist_concurrency_label(io_size, concurrency)
                line = ax.plot(x, y, "-o", markersize=3, label=label, color=color)[0]

                # Store the line for the legend
                if combo_key not in series_lines:
                    series_lines[combo_key] = line

                    # Store percentile lines for this (IO size, concurrency) combo
                    percentile_lines[combo_key] = {}

                    # Only add 50% and 99% percentile lines for each series
                    if metric.lat_pct_50 > 0 and metric.lat_pct_99 > 0:
                        # 50% percentile
                        pct50_ms = metric.lat_pct_50 * 1000  # Convert to ms
                        # Format label value
                        if pct50_ms < 0.1:
                            pct50_formatted = f"{pct50_ms:.3f}"
                        elif pct50_ms < 1:
                            pct50_formatted = f"{pct50_ms:.2f}"
                        else:
                            pct50_formatted = f"{pct50_ms:.1f}"

                        # Draw 50% line with same color as data series
                        pct50_line = ax.axvline(
                            x=pct50_ms,
                            linestyle="--",
                            alpha=0.8,
                            color=color,
                            linewidth=1.2,
                            label=(
                                f"{_latency_hist_concurrency_label(io_size, concurrency)} "
                                f"50%: {pct50_formatted}ms"
                            ),
                        )
                        percentile_lines[combo_key]["50%"] = (
                            pct50_line,
                            pct50_formatted,
                        )

                        # Add text label at 20% of the plot height
                        label_y = (
                            global_min_count
                            * (global_max_count / global_min_count) ** 0.2
                        )
                        ax.text(
                            pct50_ms,
                            label_y,
                            "50%",
                            rotation=90,
                            verticalalignment="bottom",
                            horizontalalignment="right",
                            fontsize=7,
                            color=color,
                            fontweight="bold",
                        )

                        # 99% percentile
                        pct99_ms = metric.lat_pct_99 * 1000  # Convert to ms
                        # Format label value
                        if pct99_ms < 0.1:
                            pct99_formatted = f"{pct99_ms:.3f}"
                        elif pct99_ms < 1:
                            pct99_formatted = f"{pct99_ms:.2f}"
                        else:
                            pct99_formatted = f"{pct99_ms:.1f}"

                        # Draw 99% line with same color as data series
                        pct99_line = ax.axvline(
                            x=pct99_ms,
                            linestyle=":",
                            alpha=0.8,
                            color=color,
                            linewidth=1.5,
                            label=(
                                f"{_latency_hist_concurrency_label(io_size, concurrency)} "
                                f"99%: {pct99_formatted}ms"
                            ),
                        )
                        percentile_lines[combo_key]["99%"] = (
                            pct99_line,
                            pct99_formatted,
                        )

                        # Add text label at 30% of the plot height
                        label_y = (
                            global_min_count
                            * (global_max_count / global_min_count) ** 0.3
                        )
                        ax.text(
                            pct99_ms,
                            label_y,
                            "99%",
                            rotation=90,
                            verticalalignment="bottom",
                            horizontalalignment="right",
                            fontsize=7,
                            color=color,
                            fontweight="bold",
                        )

                # Only fill for the first few plots to avoid clutter
                if len(series_lines) <= 3:  # Limit to 3 fills regardless of order
                    ax.fill_between(x, y, alpha=0.2, color=color)

        # Set y-axis to log scale
        ax.set_yscale("log")

        # Set x-axis to log scale
        ax.set_xscale("log")

        # Force the x-axis limits to exactly match our calculated min/max
        ax.set_xlim(global_min_latency, global_max_latency)

        # Set y-axis limits
        ax.set_ylim(global_min_count, global_max_count)

        # Add custom ticks for percentiles under the x-axis
        # Get log-scaled ticks within our actual data range
        log_min = np.log10(global_min_latency)
        log_max = np.log10(global_max_latency)

        # Generate evenly spaced ticks in log space, but only within our data range
        log_steps = np.arange(np.floor(log_min), np.ceil(log_max) + 1)
        default_ticks = 10**log_steps

        # Filter to only include ticks within our range
        default_ticks = [
            tick
            for tick in default_ticks
            if global_min_latency <= tick <= global_max_latency
        ]

        default_ticklabels = [
            f"{tick:.1f}" if tick < 1000 else f"{tick:.0f}" for tick in default_ticks
        ]

        # Apply default ticks
        ax.set_xticks(default_ticks)
        ax.set_xticklabels(default_ticklabels, rotation=45, ha="right", fontsize=8)

        # Make sure the x-axis limits are enforced after all tick adjustments
        ax.set_xlim(global_min_latency, global_max_latency)

        # Add labels and title
        ax.set_ylabel("Count")
        ax.yaxis.set_major_formatter(FuncFormatter(_format_tick_with_commas))
        if i == num_plots - 1:  # Only add x-label on bottom subplot
            ax.set_xlabel("Latency (ms)")
        value_label = f'{value} {metadata["x_label"]}'
        ax.set_title(f"{operation} - {value_label}")

        # Create a custom legend with consistent ordering
        # First collect all lines in desired order
        legend_handles = []
        legend_labels = []

        # Add (IO size, concurrency) combos first in sorted order
        for io_size, concurrency in sorted_series_keys:
            combo_key = (io_size, concurrency)
            if combo_key in series_lines:
                legend_handles.append(series_lines[combo_key])
                legend_labels.append(
                    _latency_hist_concurrency_label(io_size, concurrency)
                )

                # Add percentile lines for this (IO size, concurrency) combo
                if combo_key in percentile_lines:
                    # Add 50% line for this combo
                    if "50%" in percentile_lines[combo_key]:
                        pct_line, formatted_value = percentile_lines[combo_key]["50%"]
                        legend_handles.append(pct_line)
                        legend_labels.append(
                            f"{_latency_hist_concurrency_label(io_size, concurrency)} "
                            f"50%: {formatted_value}ms"
                        )

                    # Add 99% line for this combo
                    if "99%" in percentile_lines[combo_key]:
                        pct_line, formatted_value = percentile_lines[combo_key]["99%"]
                        legend_handles.append(pct_line)
                        legend_labels.append(
                            f"{_latency_hist_concurrency_label(io_size, concurrency)} "
                            f"99%: {formatted_value}ms"
                        )

        # Add the custom legend (multi-column when many entries)
        ax.legend(
            legend_handles,
            legend_labels,
            loc="upper right",
            fontsize=7,
            ncol=legend_ncol,
        )

        # Add grid for better readability
        ax.grid(True, alpha=0.3)

    # Do a final check on all subplots to ensure they have the correct x-axis limits
    for ax in axes:
        ax.set_xlim(global_min_latency, global_max_latency)

    # Save figure - we're using constrained_layout so no need for tight_layout
    plt.savefig(
        os.path.join(
            output_dir,
            plot_filename(
                metadata["sn_or_mn"],
                operation,
                annotated_size_group["group_name"],
                metadata["dio_or_bio"],
                "latency-hist",
                metadata["datestamps"],
            ),
        ),
        bbox_inches="tight",
        dpi=150,  # Increase DPI for better resolution
    )
    plt.close()


# Helper function to get size in bytes
def get_size_in_bytes(size_str: str, operation: str) -> int:
    """Convert size string to bytes, handling split sizes and random IO prefix.

    Args:
        size_str: Size string (e.g., "4K", "r4K", "1M,4K", "r1M,r4K")
        operation: Operation type (READ or WRITE) - used for split sizes

    Returns:
        Size in bytes
    """
    # Handle split sizes (e.g., "1M,4K" or "r1M,r4K")
    if "," in size_str:
        size_parts = size_str.split(",")
        if operation == "WRITE":
            size_str = size_parts[0]
        else:
            size_str = size_parts[1]

    # Strip leading 'r' prefix if present (indicates random IO)
    size_str = size_str.lstrip("r")

    size_map = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
    match = re.match(r"(\d+)([KMGTkmgt])", size_str)
    if match:
        num, unit = match.groups()
        return int(num) * size_map.get(unit.upper(), 1)
    return 0


def iosize_sorter(iosize: str, operation: str) -> tuple[int, int]:
    """Sort key function for IO size strings by their byte values,
       for READ and WRITE operations, breaking ties by length of iosize.

    Args:
        iosize: IO size string; may be a single size or a split size
        operation: Operation type (READ or WRITE)

    Returns:
        Tuple of (byte value, length of iosize)
    """
    return (get_size_in_bytes(iosize, operation), len(iosize))


def categorize_sizes(sizes, operation):
    """Group IO sizes into clusters based on their byte values.

    Sizes that are within 1 order of magnitude of each other are grouped together.
    For example, 4K-8K (2x) stay together, but 64K and 1M (16x) split into
    separate groups representing different performance tiers (IOPS vs throughput).

    Args:
        sizes: Collection of IO size strings
        operation: Operation type (READ or WRITE)

    Returns:
        List of clusters, where each cluster is a list of IO size strings
    """
    size_to_bytes = {}

    for size in sizes:
        size_to_bytes[size] = get_size_in_bytes(size, operation)

    if not size_to_bytes:
        return []

    sorted_sizes = sorted(sizes, key=partial(iosize_sorter, operation=operation))

    clusters = []
    current_cluster = [sorted_sizes[0]]
    current_min = size_to_bytes[sorted_sizes[0]]

    for size in sorted_sizes[1:]:
        bytes_value = size_to_bytes[size]
        if bytes_value > current_min * 10:
            clusters.append(current_cluster)
            current_cluster = [size]
            current_min = bytes_value
        else:
            current_cluster.append(size)

    # Add the last cluster if not empty
    if current_cluster:
        clusters.append(current_cluster)

    return clusters


def _annotated_size_group_for_size_group(
    size_group: List[str],
    metrics_list: List[ElbenchoMetrics],
    is_multi_node: bool,
    operation: str,
) -> AnnotatedSizeGroup:
    """Build one annotated size-group bucket (plot/report unit)."""
    if len(size_group) == 1:
        group_name = size_group[0]
    else:
        min_size = min(size_group, key=partial(iosize_sorter, operation=operation))
        max_size = max(size_group, key=partial(iosize_sorter, operation=operation))
        group_name = f"{min_size}-to-{max_size}"

    def _sort_key(m: ElbenchoMetrics) -> Tuple[int, int]:
        if is_multi_node:
            return (m.nodes, m.io_depth)
        return (m.threads, m.io_depth)

    return {
        "size_group": size_group,
        "group_name": group_name,
        "metrics_by_size": {
            size: sorted(
                [m for m in metrics_list if m.io_size == size],
                key=_sort_key,
            )
            for size in size_group
        },
    }


def cluster_metrics_for_reporting(
    metrics: List[ElbenchoMetrics],
) -> ClusteredAnnotatedSizeGroups:
    # Cluster metrics like this:
    # by is_multi_node
    #   by operation
    #     by direct/buffered io
    #       by size group

    clustered_metrics = defaultdict(partial(defaultdict, partial(defaultdict, list)))
    annotated_size_groups = defaultdict(
        partial(defaultdict, partial(defaultdict, list))
    )

    for metric in metrics:
        clustered_metrics[metric.is_multi_node][metric.operation][
            metric.direct_io
        ].append(metric)

    for is_multi_node, by_mn in clustered_metrics.items():
        for operation, by_op in by_mn.items():
            for direct_io, metrics_list in by_op.items():
                all_sizes = {m.io_size for m in metrics_list}
                size_groups = categorize_sizes(all_sizes, operation)

                for size_group in size_groups:
                    annotated_size_group = _annotated_size_group_for_size_group(
                        size_group, metrics_list, is_multi_node, operation
                    )
                    annotated_size_groups[is_multi_node][operation][direct_io].append(
                        annotated_size_group
                    )
    return annotated_size_groups


# pylint: disable=too-many-positional-arguments
def plot_filename(
    sn_or_mn: str,
    operation: str,
    group_name: str,
    io_mode_suffix: str,
    img_type: str,
    datestamps: Set[str],
) -> str:
    prefix = (
        f"elbencho-{sn_or_mn}-{operation}-{group_name}-{io_mode_suffix}-" f"{img_type}-"
    )
    stamp = join_datestamps_for_filename(datestamps, prefix=prefix)
    return f"{prefix}{stamp}.png"


def metrics_metadata(
    is_multi_node: bool,
    direct_io: int,
    random_io: int,
    metrics: Iterable[ElbenchoMetrics],
) -> Dict[str, Any]:
    if is_multi_node:
        sn_or_mn_str = "Multi-Node"
        sn_or_mn = "mn"
    else:
        sn_or_mn_str = "Single-Node"
        sn_or_mn = "sn"

    # Determine IO mode (DirectIO or BufferedIO)
    io_mode_str = "DirectIO" if direct_io else "BufferedIO"

    # Determine IO order (Random or Sequential)
    io_order = "Rand" if random_io else "Seq"

    datestamps = set()
    x_values = set()
    for m in metrics:
        datestamps.add(m.datestamp)
        x_values.add(m.nodes if is_multi_node else m.threads)

    return {
        "sn_or_mn_str": sn_or_mn_str,
        "sn_or_mn": sn_or_mn,
        "dio_or_bio": "dio" if direct_io else "bio",
        "io_mode_str": io_mode_str,
        "io_order": io_order,
        "datestamps": datestamps,
        "x_values": sorted(x_values),
    }


def plot_metadata(
    is_multi_node: bool,
    direct_io: int,
    random_io: int,
    annotated_size_group: AnnotatedSizeGroup,
) -> PlotMetadata:
    metadata = metrics_metadata(
        is_multi_node,
        direct_io,
        random_io,
        chain.from_iterable(annotated_size_group["metrics_by_size"].values()),
    )

    # Calculate unique (io_size, thread_count * io_depth) combinations for color allocation
    # For multi-node: each (io_size, thread_count, io_depth) gets its own series
    # For single-node: each (io_size, io_depth) gets its own series
    # We want to color by (io_size, threads*io_depth) so that different combinations
    # that produce the same "total depth" can share a color
    unique_color_keys = set()
    for size_metrics in annotated_size_group["metrics_by_size"].values():
        for metric in size_metrics:
            if is_multi_node:
                # For multi-node: color key is (io_size, thread_count * io_depth)
                color_key = (metric.io_size, metric.threads * metric.io_depth)
            else:
                # For single-node: color key is (io_size, io_depth)
                # (since thread_count varies along x-axis, not as separate series)
                color_key = (metric.io_size, metric.io_depth)
            unique_color_keys.add(color_key)

    num_colors_needed = len(unique_color_keys)

    # Choose appropriate colormap based on number of colors needed
    # Use colorblind-friendly palettes: tableau-colorblind10 for categorical,
    # cividis for continuous data
    if num_colors_needed <= 10:
        # Use tableau-colorblind10 colors (explicitly designed for colorblind accessibility)
        # These are the 10 colors from Tableau's colorblind-safe palette
        with plt.style.context("tableau-colorblind10"):
            colorblind_colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
        # Extract only the number of colors we need
        metadata["colors"] = [colorblind_colors[i] for i in range(num_colors_needed)]
    elif num_colors_needed <= 20:
        # Use tab20 for 11-20 colors (categorical, though not optimal for colorblindness)
        # Note: No colorblind-friendly categorical palette exists for >10 colors
        metadata["colors"] = plt.get_cmap("tab20")(np.linspace(0, 1, num_colors_needed))
    else:
        # Use cividis for many colors (perceptually uniform, colorblind-friendly)
        metadata["colors"] = plt.get_cmap("cividis")(
            np.linspace(0, 1, num_colors_needed)
        )

    metadata["markers"] = ["o", "^", "s", "D", "v", "<", ">", "p", "h", "8", "X", "P"]
    metadata["x_label"] = "Node Count" if is_multi_node else "Thread Count"
    return cast(PlotMetadata, metadata)


def _prepare_metrics_groups(
    annotated_size_group: AnnotatedSizeGroup,
    is_multi_node: bool,
    operation: str,
) -> Tuple[List[tuple], Dict[tuple, List[ElbenchoMetrics]]]:
    """
    Prepare metric groups for plotting.

    Args:
        annotated_size_group: The annotated size group containing metrics
        is_multi_node: Whether this is a multi-node run
        operation: Operation type (READ or WRITE)

    Returns:
        Tuple of:
        - sorted_keys: List of (size, thread_count, io_depth) or (size, io_depth) tuples
        - metrics_by_group: Dict mapping keys to list of metrics
    """
    # Create a new dictionary to group metrics
    # For multi-node: group by (io_size, thread_count, io_depth) - line varies by node count
    # For single-node: group by (io_size, io_depth) - line varies by thread count
    if is_multi_node:
        metrics_by_group = defaultdict(list)
        # Group metrics by (io_size, thread_count, io_depth) tuples for multi-node
        for size, size_metrics in annotated_size_group["metrics_by_size"].items():
            for metric in size_metrics:
                thread_count = metric.threads
                io_depth = metric.io_depth
                key = (size, thread_count, io_depth)
                metrics_by_group[key].append(metric)

        # Sort the keys for consistent ordering - first by io_size, then by thread_count, then by io_depth
        sorted_keys = sorted(
            metrics_by_group.keys(),
            key=lambda x: (iosize_sorter(x[0], operation), x[1], x[2]),
        )
    else:
        metrics_by_group = defaultdict(list)
        # Group metrics by (io_size, io_depth) for single-node
        for size, size_metrics in annotated_size_group["metrics_by_size"].items():
            for metric in size_metrics:
                io_depth = metric.io_depth
                key = (size, io_depth)
                metrics_by_group[key].append(metric)

        # Sort the keys for consistent ordering by io_size then io_depth
        sorted_keys = sorted(
            metrics_by_group.keys(),
            key=lambda x: (iosize_sorter(x[0], operation), x[1]),
        )

    return sorted_keys, metrics_by_group


def _create_legend_label(
    key: tuple,
    is_multi_node: bool,
    metric_type: str,
) -> str:
    """
    Create legend label for a given metric type.

    Args:
        key: (size, thread_count, io_depth) for MN or (size, io_depth) for SN
        is_multi_node: Whether this is multi-node
        metric_type: Type of metric ("IOPS", "BW", "Lat")

    Returns:
        Formatted label string
    """
    if is_multi_node:
        size, thread_count, io_depth = key
        return f"{size}, {thread_count}t, {io_depth}d {metric_type}"
    else:
        size, io_depth = key
        return f"{size}, {io_depth}d {metric_type}"


CSV_OUT_EXT_RE = r"\.(csv|out)$"

# Metric configuration for different plot types
METRIC_CONFIGS = {
    "iops": {
        "ylabel": "IOPS",
        "extractor": lambda m: m.iops,
        "label_suffix": "IOPS",
        "linestyle": "-",
        "linewidth": 1,
        "markersize": 8,
    },
    "bw": {
        "ylabel": "Throughput (GB/s)",
        "extractor": lambda m: mb_s_to_gb_s(m.throughput_mb_s),
        "label_suffix": "BW",
        "linestyle": "-",
        "linewidth": 1,
        "markersize": 8,
    },
    "latency": {
        "ylabel": "Average Latency (ms)",
        "extractor": lambda m: m.avg_lat_sec * 1000,
        "label_suffix": "Lat",
        "linestyle": "--",
        "linewidth": 1,
        "markersize": 6,
    },
}


def _generate_plot_title(
    metadata: PlotMetadata,
    operation: str,
    group_name: str,
    metrics_in_plot: List[str],
) -> str:
    """Generate plot title based on metrics being plotted."""
    if len(metrics_in_plot) == 1:
        # Single metric titles
        metric_titles = {
            "iops": "IOPS",
            "bw": "Throughput",
            "latency": "Latency",
        }
        metric_title = metric_titles[metrics_in_plot[0]]
    else:
        # Dual-axis titles
        if "iops" in metrics_in_plot:
            metric_title = "IOPS & Latency"
        else:
            metric_title = "Throughput & Latency"

    return (
        f"{metadata['sn_or_mn_str']} {operation} "
        f"({group_name}) "
        f"{metadata['io_order']} "
        f"{metadata['io_mode_str']} "
        f"{metric_title} vs. {metadata['x_label']}"
    )


def _plot_color_key_from_group_key(key: tuple, is_multi_node: bool) -> tuple:
    """Color-grouping key: (io_size, threads*io_depth) for MN, (io_size, io_depth) for SN."""
    if is_multi_node:
        size, thread_count, io_depth = key
        return (size, thread_count * io_depth)
    size, io_depth = key
    return (size, io_depth)


def _x_data_for_grouped_metrics(
    grouped_metrics: List[ElbenchoMetrics], is_multi_node: bool
) -> List[int]:
    """X-axis values: node counts (MN) or thread counts (SN)."""
    if is_multi_node:
        return [m.nodes for m in grouped_metrics]
    return [m.threads for m in grouped_metrics]


def _plot_build_color_key_to_index(
    sorted_keys: List[tuple], is_multi_node: bool
) -> Dict[tuple, int]:
    """Stable color index per color key across sorted_keys order."""
    color_key_to_index: Dict[tuple, int] = {}
    next_color_index = 0
    for key in sorted_keys:
        color_key = _plot_color_key_from_group_key(key, is_multi_node)
        if color_key not in color_key_to_index:
            color_key_to_index[color_key] = next_color_index
            next_color_index += 1
    return color_key_to_index


def _finalize_single_performance_plot(
    ax1: Any,
    ax2: Optional[Any],
    metadata: PlotMetadata,
    operation: str,
    group_name: str,
    metrics_in_plot: List[str],
    all_lines: List[Any],
    all_labels: List[str],
    output_dir: str,
    filename_suffix: str,
    is_multi_node: bool,
) -> None:
    """Apply labels, legend, limits, and save for a performance line plot."""
    ax1.set_xlabel(metadata["x_label"])
    ax1.set_ylabel(METRIC_CONFIGS[metrics_in_plot[0]]["ylabel"])
    ax1.yaxis.set_major_formatter(FuncFormatter(_format_tick_with_commas))
    if ax2:
        ax2.set_ylabel(METRIC_CONFIGS[metrics_in_plot[1]]["ylabel"])
        ax2.yaxis.set_major_formatter(FuncFormatter(_format_tick_with_commas))
    ax1.set_xticks(metadata["x_values"])

    title = _generate_plot_title(metadata, operation, group_name, metrics_in_plot)
    ax1.set_title(title)

    if is_multi_node:
        if len(all_lines) > 8:
            ax1.legend(
                all_lines,
                all_labels,
                loc=LEGEND_LOC_LOWER_RIGHT,
                fontsize="small",
            )
        else:
            ax1.legend(all_lines, all_labels, loc=LEGEND_LOC_LOWER_RIGHT)
    elif len(all_lines) > 8:
        ax1.legend(
            all_lines,
            all_labels,
            loc=LEGEND_LOC_CENTER_LEFT,
            bbox_to_anchor=(1.15, 0.5),
            fontsize="small",
        )
    else:
        ax1.legend(
            all_lines,
            all_labels,
            loc=LEGEND_LOC_CENTER_LEFT,
            bbox_to_anchor=(1.05, 0.5),
        )

    ax1.set_ylim(0, None)
    if ax2:
        ax2.set_ylim(0, None)

    plt.tight_layout()
    plt.savefig(
        os.path.join(
            output_dir,
            plot_filename(
                metadata["sn_or_mn"],
                operation,
                group_name,
                metadata["dio_or_bio"],
                filename_suffix,
                metadata["datestamps"],
            ),
        ),
        bbox_inches="tight",
    )
    plt.close()


def _generate_single_plot(
    plot_config: Dict[str, Any],
    sorted_keys: List[tuple],
    metrics_by_group: Dict[tuple, List[ElbenchoMetrics]],
    metadata: PlotMetadata,
    annotated_size_group: AnnotatedSizeGroup,
    output_dir: str,
    is_multi_node: bool,
    operation: str,
) -> None:
    """Generate a single plot (either dual-axis or single-axis) based on configuration."""
    metrics_in_plot = plot_config["metrics"]
    filename_suffix = plot_config["filename_suffix"]

    # Create figure
    plt.figure(figsize=(12, 8))
    ax1 = plt.gca()
    ax2 = ax1.twinx() if len(metrics_in_plot) == 2 else None

    # Lists for collecting lines and labels for legend
    all_lines = []
    all_labels = []

    color_key_to_index = _plot_build_color_key_to_index(sorted_keys, is_multi_node)

    # Process each group
    for i, key in enumerate(sorted_keys):
        color_key = _plot_color_key_from_group_key(key, is_multi_node)

        # Get the metrics for this key
        grouped_metrics = metrics_by_group[key]

        # Sort by nodes/threads to ensure proper ordering
        grouped_metrics.sort(key=lambda m: m.nodes if is_multi_node else m.threads)

        x_data = _x_data_for_grouped_metrics(grouped_metrics, is_multi_node)

        # Skip if we don't have enough data
        if not x_data or len(x_data) < 2:
            continue

        # Select color and marker using the pre-computed mapping
        color_index = color_key_to_index[color_key]
        color = metadata["colors"][color_index]
        markers = metadata["markers"]
        marker = markers[i % len(markers)]

        # Plot each metric in the configuration
        for metric_idx, metric_name in enumerate(metrics_in_plot):
            config = METRIC_CONFIGS[metric_name]

            # Extract y data
            y_data = [config["extractor"](m) for m in grouped_metrics]

            # Create label
            label = _create_legend_label(key, is_multi_node, config["label_suffix"])

            # Determine which axis to use (ax2 exists only for dual-metric plots)
            if metric_idx == 0:
                plot_ax = ax1
            else:
                assert ax2 is not None
                plot_ax = ax2

            # Plot the data
            line = plot_ax.plot(
                x_data,
                y_data,
                f"{config['linestyle']}{marker}",
                color=color,
                label=label,
                linewidth=config["linewidth"],
                markersize=config["markersize"],
            )[0]

            all_lines.append(line)
            all_labels.append(label)

    _finalize_single_performance_plot(
        ax1,
        ax2,
        metadata,
        operation,
        annotated_size_group["group_name"],
        metrics_in_plot,
        all_lines,
        all_labels,
        output_dir,
        filename_suffix,
        is_multi_node,
    )


def plot_performance_metrics(
    annotated_size_group: AnnotatedSizeGroup,
    output_dir: str,
    is_multi_node: bool,
    operation: str,
    direct_io: int,
    random_io: int,
    no_dual_y_axis: bool = False,
) -> None:
    """
    Generate performance metric plots.

    Behavior:
    - If no_dual_y_axis=False (default): Generate 2 dual-axis plots
      * "iops-latency" plot
      * "bw-latency" plot
    - If no_dual_y_axis=True: Generate 3 single-axis plots
      * "iops" plot
      * "bw" plot
      * "latency" plot

    Args:
        annotated_size_group: The annotated size group containing metrics
        output_dir: Directory to save plots
        is_multi_node: Whether this is a multi-node run
        operation: Operation type (READ or WRITE)
        direct_io: Whether this is a direct IO run
        random_io: Whether this is a random IO run
        no_dual_y_axis: If True, generate 3 single-axis plots instead of 2 dual-axis plots
    """
    metadata = plot_metadata(is_multi_node, direct_io, random_io, annotated_size_group)
    sorted_keys, metrics_by_group = _prepare_metrics_groups(
        annotated_size_group, is_multi_node, operation
    )

    # Determine which plots to generate
    if no_dual_y_axis:
        # Generate 3 single-axis plots
        plots_to_generate = [
            {"metrics": ["iops"], "filename_suffix": "iops"},
            {"metrics": ["bw"], "filename_suffix": "bw"},
            {"metrics": ["latency"], "filename_suffix": "latency"},
        ]
    else:
        # Generate 2 dual-axis plots
        plots_to_generate = [
            {"metrics": ["iops", "latency"], "filename_suffix": "iops-latency"},
            {"metrics": ["bw", "latency"], "filename_suffix": "bw-latency"},
        ]

    # Generate each configured plot
    for plot_config in plots_to_generate:
        _generate_single_plot(
            plot_config=plot_config,
            sorted_keys=sorted_keys,
            metrics_by_group=metrics_by_group,
            metadata=metadata,
            annotated_size_group=annotated_size_group,
            output_dir=output_dir,
            is_multi_node=is_multi_node,
            operation=operation,
        )


def plot_throughput_scale_efficiency(
    annotated_size_group: AnnotatedSizeGroup,
    output_dir: str,
    is_multi_node: bool,
    operation: str,
    direct_io: int,
    random_io: int,
) -> None:
    metadata = plot_metadata(is_multi_node, direct_io, random_io, annotated_size_group)

    plt.figure(figsize=(12, 8))
    ax = plt.gca()

    # Create a new dictionary to group metrics
    # For multi-node: group by (io_size, thread_count, io_depth) - line varies by node count
    # For single-node: group by (io_size, io_depth) - line varies by thread count
    if is_multi_node:
        metrics_by_group = defaultdict(list)
        # Group metrics by (io_size, thread_count, io_depth) tuples for multi-node
        for size, size_metrics in annotated_size_group["metrics_by_size"].items():
            for metric in size_metrics:
                thread_count = metric.threads
                io_depth = metric.io_depth
                key = (size, thread_count, io_depth)
                metrics_by_group[key].append(metric)

        # Sort the keys for consistent ordering - first by io_size, then by thread_count, then by io_depth
        sorted_keys = sorted(
            metrics_by_group.keys(),
            key=lambda x: (iosize_sorter(x[0], operation), x[1], x[2]),
        )
    else:
        metrics_by_group = defaultdict(list)
        # Group metrics by (io_size, io_depth) for single-node
        for size, size_metrics in annotated_size_group["metrics_by_size"].items():
            for metric in size_metrics:
                io_depth = metric.io_depth
                key = (size, io_depth)
                metrics_by_group[key].append(metric)

        # Sort the keys for consistent ordering by io_size then io_depth
        sorted_keys = sorted(
            metrics_by_group.keys(),
            key=lambda x: (iosize_sorter(x[0], operation), x[1]),
        )

    # Build a mapping from color_key to color_index BEFORE the loop
    # This ensures that the same color_key always gets the same color,
    # even if it appears multiple times in non-contiguous positions
    color_key_to_index = {}
    next_color_index = 0
    for key in sorted_keys:
        if is_multi_node:
            size, thread_count, io_depth = key
            color_key = (size, thread_count * io_depth)
        else:
            size, io_depth = key
            color_key = (size, io_depth)

        if color_key not in color_key_to_index:
            color_key_to_index[color_key] = next_color_index
            next_color_index += 1

    # Process each group (multi-node: (io_size, thread_count, io_depth), single-node: (io_size, io_depth))
    for i, key in enumerate(sorted_keys):
        if is_multi_node:
            size, thread_count, io_depth = key
            # Color key based on (io_size, thread_count * io_depth)
            color_key = (size, thread_count * io_depth)

            # Get the metrics for this (io_size, thread_count, io_depth) tuple
            grouped_metrics = metrics_by_group[key]

            # Create the legend label with io_size, thread_count, and io_depth
            label = f"{size}, {thread_count}t, {io_depth}d"
        else:
            size, io_depth = key
            # Color key based on (io_size, io_depth)
            color_key = (size, io_depth)

            # Get the metrics for this (io_size, io_depth) tuple
            grouped_metrics = metrics_by_group[key]

            # Create the legend label with io_size and io_depth
            label = f"{size}, {io_depth}d"

        # Sort by nodes/threads to ensure proper ordering
        grouped_metrics.sort(key=lambda m: m.nodes if is_multi_node else m.threads)

        # Extract x and y values
        if is_multi_node:
            x_data = [m.nodes for m in grouped_metrics]
        else:
            x_data = [m.threads for m in grouped_metrics]

        bw = [mb_s_to_gb_s(m.throughput_mb_s) for m in grouped_metrics]

        # Skip if we don't have enough data
        if not x_data or not bw or len(x_data) < 2:
            continue

        # Calculate throughput per unit (node/thread) for each data point
        bw_per_unit = [bw_val / x_val for bw_val, x_val in zip(bw, x_data)]

        # Find the maximum throughput per unit to use as the reference (100%)
        max_bw_per_unit = max(bw_per_unit)

        # Calculate efficiency values relative to the maximum throughput per unit
        # Formula: (bw_per_unit / max_bw_per_unit) * 100
        efficiency = [
            (bw_val / x_val) / max_bw_per_unit * 100
            for bw_val, x_val in zip(bw, x_data)
        ]

        # Select color and marker using the pre-computed mapping
        color_index = color_key_to_index[color_key]
        color = metadata["colors"][color_index]
        markers = metadata["markers"]
        marker = markers[i % len(markers)]

        # Plot efficiency
        ax.plot(
            x_data, efficiency, f"-{marker}", color=color, label=label, markersize=8
        )

    # Set labels and title
    ax.set_xlabel(metadata["x_label"])
    ax.set_ylabel("Scaling Efficiency (%)")
    ax.yaxis.set_major_formatter(FuncFormatter(_format_tick_with_commas))
    ax.set_xticks(metadata["x_values"])

    # Set y-axis to start from 0 and up to a bit more than 100% to show ideal scaling
    ax.set_ylim(0, max(105, ax.get_ylim()[1]))

    # Add a horizontal line at 100% to indicate perfect scaling
    ax.axhline(y=100, linestyle="--", color="gray", alpha=0.7, label="Perfect Scaling")

    ax.set_title(
        f'{metadata["sn_or_mn_str"]} {operation} '
        f'({annotated_size_group["group_name"]}) '
        f'{metadata["io_order"]} '
        f'{metadata["io_mode_str"]} '
        f'Throughput Scale Efficiency vs. {metadata["x_label"]}'
    )

    # Add legend with a good location based on number of items
    legend_items = len(sorted_keys) + 1  # +1 for the "Perfect Scaling" line
    if legend_items > 6:
        # Use an outside legend for many items
        ax.legend(
            loc=LEGEND_LOC_CENTER_LEFT,
            bbox_to_anchor=(1.02, 0.5),
            fontsize="small",
        )
    else:
        # Use an inside legend for fewer items
        ax.legend(loc="best")

    # Set grid for better readability
    ax.grid(True, alpha=0.3)

    # Save plot
    plt.tight_layout()
    plt.savefig(
        os.path.join(
            output_dir,
            plot_filename(
                metadata["sn_or_mn"],
                operation,
                annotated_size_group["group_name"],
                metadata["dio_or_bio"],
                "bw-efficiency",
                metadata["datestamps"],
            ),
        ),
        bbox_inches="tight",
    )
    plt.close()


def _plot_all_charts_for_annotated_size_group(
    annotated_size_group: AnnotatedSizeGroup,
    output_dir: str,
    is_multi_node: bool,
    operation: str,
    direct_io: int,
    no_dual_y_axis: bool,
) -> None:
    """Generate performance, throughput-efficiency, and latency-histogram plots for one group."""
    first_metrics = next(iter(annotated_size_group["metrics_by_size"].values()))
    random_io = first_metrics[0].random_io

    plot_performance_metrics(
        annotated_size_group,
        output_dir,
        is_multi_node,
        operation,
        direct_io,
        random_io,
        no_dual_y_axis,
    )

    if is_multi_node:
        plot_throughput_scale_efficiency(
            annotated_size_group,
            output_dir,
            is_multi_node,
            operation,
            direct_io,
            random_io,
        )

    plot_latency_histograms(
        annotated_size_group,
        output_dir,
        is_multi_node,
        operation,
        direct_io,
        random_io,
    )


def plot_metrics(
    metrics: List[ElbenchoMetrics],
    output_dir: str,
    no_dual_y_axis: bool = False,
) -> None:
    """Generate throughput and latency plots.

    Args:
        metrics: List of ElbenchoMetrics objects to plot
        output_dir: Directory to save plots
        no_dual_y_axis: If True, generate separate single-axis plots instead of dual y-axis plots
    """
    if not metrics:
        eprint("No metrics to plot")
        return

    clustered_metrics = cluster_metrics_for_reporting(metrics)

    for is_multi_node, by_mn in clustered_metrics.items():
        for operation, by_op in by_mn.items():
            for direct_io, annotated_size_groups in by_op.items():
                for annotated_size_group in annotated_size_groups:
                    _plot_all_charts_for_annotated_size_group(
                        annotated_size_group,
                        output_dir,
                        is_multi_node,
                        operation,
                        direct_io,
                        no_dual_y_axis,
                    )


def write_csv(csv_file: str, metrics: List[ElbenchoMetrics]) -> None:
    """Write metrics to a CSV file.

    Args:
        csv_file: Path to the CSV file
        metrics: List of ElbenchoMetrics objects
    """
    if not metrics:
        return

    field_names = [f.name for f in fields(ElbenchoMetrics)]

    try:
        with open(csv_file, "w", newline="", encoding="utf-8") as f:
            # Use QUOTE_ALL to ensure all fields are quoted, including booleans and numbers
            writer = csv.DictWriter(f, fieldnames=field_names, quoting=csv.QUOTE_ALL)
            writer.writeheader()

            for metric in metrics:
                # Convert histogram dictionary to string representation
                metric_dict = asdict(metric)
                if metric_dict["histogram"]:
                    metric_dict["histogram"] = json.dumps(metric_dict["histogram"])

                # If command field is None, set it to empty string to avoid None in CSV
                if metric_dict["command"] is None:
                    metric_dict["command"] = ""

                writer.writerow(metric_dict)
    except Exception as e:  # pylint: disable=broad-exception-caught
        eprint(f"Error writing to CSV: {e}")
        traceback.print_exc()
        raise IOError(f"Error writing to CSV: {e}") from e


_TREESCAN_CSV_KEYS = (
    "treescan_file_count",
    "treescan_avg_bytes",
    "treescan_min_bytes",
    "treescan_max_bytes",
    "treescan_first_file_bytes",
)
_WORKLOAD_STRING_CSV_KEYS = (
    "configured_file_layout",
    "configured_files_per_node",
    "configured_file_size",
    "workload_layout",
    *_WORKLOAD_METADATA_KEYS,
)


def _csv_row_set_if_absent_or_empty(
    row: Dict[str, Any], key: str, default: str
) -> None:
    """Set row[key] to default when missing or blank."""
    if key not in row or row.get(key) in (None, ""):
        row[key] = default


def _elbencho_csv_parse_histogram_cell(row: Dict[str, Any]) -> None:
    """Replace JSON histogram string with a dict (or {})."""
    if "histogram" in row and row["histogram"]:
        try:
            row["histogram"] = json.loads(row["histogram"])
        except json.JSONDecodeError:
            row["histogram"] = {}
    else:
        row["histogram"] = {}


def _elbencho_csv_apply_backward_compat_defaults(row: Dict[str, Any]) -> None:
    """Fill optional / legacy CSV columns with safe string defaults."""
    if "io_depth" not in row or not row.get("io_depth"):
        row["io_depth"] = "1"
    if "random_io" not in row or not row.get("random_io"):
        row["random_io"] = "0"
    _csv_row_set_if_absent_or_empty(row, "io_duration_sec", "0")
    _csv_row_set_if_absent_or_empty(row, "phase_wall_duration_ms", "0")
    _csv_row_set_if_absent_or_empty(row, "phase_first_duration_ms", "0")
    _csv_row_set_if_absent_or_empty(row, "sweep_single_option", "False")
    _csv_row_set_if_absent_or_empty(row, "write_only_data_dir", "")
    _csv_row_set_if_absent_or_empty(row, "treescan_size_stats_line", "")
    _csv_row_set_if_absent_or_empty(row, "sweep_read_from_path", "")
    for _ts_key in _TREESCAN_CSV_KEYS:
        _csv_row_set_if_absent_or_empty(row, _ts_key, "0")
    _csv_row_set_if_absent_or_empty(row, "is_single_big_file", "False")
    _csv_row_set_if_absent_or_empty(row, "all_nodes_all_data", "False")
    for workload_key in _WORKLOAD_STRING_CSV_KEYS:
        _csv_row_set_if_absent_or_empty(row, workload_key, "")
    _elbencho_csv_parse_histogram_cell(row)


def _elbencho_csv_coerce_field_types(row: Dict[str, Any]) -> None:
    """Coerce string DictReader values to ElbenchoMetrics field types in place."""
    for a_field in fields(ElbenchoMetrics):
        field_name = a_field.name
        field_value = row[field_name]

        if a_field.type == bool:
            if isinstance(field_value, str):
                row[field_name] = field_value.lower() == "true"
        elif a_field.type == int:
            row[field_name] = int(float(field_value)) if field_value else 0
        elif a_field.type == float:
            row[field_name] = float(field_value) if field_value else 0.0


def read_csv(csv_file: str) -> List[ElbenchoMetrics]:
    """Read metrics from a CSV file.

    Args:
        csv_file: Path to the CSV file

    Returns:
        List of ElbenchoMetrics objects
    """
    metrics = []

    try:
        with open(csv_file, "r", newline="", encoding="utf-8") as f:
            # Use QUOTE_ALL since we're now writing all fields with quotes
            reader = csv.DictReader(f, quoting=csv.QUOTE_ALL)

            # Verify required fields are present (except io_depth for backward compatibility)
            required_fields = [
                f.name
                for f in fields(ElbenchoMetrics)
                if f.name
                not in (
                    "io_depth",
                    "io_duration_sec",
                    "phase_wall_duration_ms",
                    "phase_first_duration_ms",
                    "sweep_single_option",
                    "write_only_data_dir",
                    "sweep_read_from_path",
                    "treescan_size_stats_line",
                    "treescan_file_count",
                    "treescan_avg_bytes",
                    "treescan_min_bytes",
                    "treescan_max_bytes",
                    "treescan_first_file_bytes",
                    "is_single_big_file",
                    "all_nodes_all_data",
                    *_WORKLOAD_STRING_CSV_KEYS,
                )
            ]
            fieldnames = reader.fieldnames
            if fieldnames is None:
                raise ValueError(f"CSV has no header row: {csv_file}")
            missing_fields = [
                field for field in required_fields if field not in fieldnames
            ]

            if missing_fields:
                raise ValueError(
                    f"CSV is missing required fields: {', '.join(missing_fields)}"
                )

            for row in reader:
                _elbencho_csv_apply_backward_compat_defaults(row)
                _elbencho_csv_coerce_field_types(row)
                metrics.append(ElbenchoMetrics(**cast(Any, row)))
    except Exception as e:  # pylint: disable=broad-exception-caught
        eprint(f"Error reading from CSV: {e}")
        traceback.print_exc()
        raise IOError(f"Error reading from CSV: {e}") from e

    return metrics


def filter_metrics(
    metrics: List[ElbenchoMetrics],
    only_sizes: Optional[Set[str]] = None,
    only_threads: Optional[Set[int]] = None,
    only_nodes: Optional[Set[int]] = None,
    only_iodepths: Optional[Set[int]] = None,
) -> List[ElbenchoMetrics]:
    """Filter metrics based on IO sizes, thread counts, node counts, and IO depths.

    Args:
        metrics: List of ElbenchoMetrics objects to filter
        only_sizes: Set of IO size strings to include (e.g., {'1M', '4K', '1M,4K'})
        only_threads: Set of thread counts to include
        only_nodes: Set of node counts to include
        only_iodepths: Set of IO depth values to include

    Returns:
        Filtered list of ElbenchoMetrics objects
    """
    if not metrics:
        return []

    filtered = metrics.copy()

    if only_sizes:
        filtered = [m for m in filtered if m.io_size in only_sizes]

    if only_threads:
        filtered = [m for m in filtered if m.threads in only_threads]

    if only_nodes:
        filtered = [m for m in filtered if m.nodes in only_nodes]

    if only_iodepths:
        filtered = [m for m in filtered if m.io_depth in only_iodepths]

    return filtered


# Helper function to format latency values with significant digits and decimal alignment
def format_with_sig_figs(value: float, sig_figs: int = 3) -> str:
    """Format a number with a specific number of significant figures.

    Args:
        value: The value to format
        sig_figs: Number of significant figures (default=3)

    Returns:
        Formatted string with appropriate significant figures, preserving
        trailing zeros when needed for significant digits
    """
    if value == 0:
        return "0"  # Just return "0" instead of "0.000"

    # For very small values, use more decimal places
    if value < 0.01:
        # Use scientific notation first to get significant digits
        sci_format = f"{value:.{sig_figs-1}e}"
        # Convert back to standard notation
        parts = sci_format.split("e")
        exponent = int(parts[1])
        if exponent < 0:
            # For very small numbers, show more decimal places
            return f"{value:.{abs(exponent)+sig_figs-1}f}"

    # For normal-sized values, determine the order of magnitude
    order_of_magnitude = math.floor(math.log10(abs(value)))
    decimal_places = max(0, sig_figs - 1 - order_of_magnitude)

    # Format with calculated decimal places
    formatted = f"{value:.{decimal_places}f}"

    # Don't strip trailing zeros if they're needed for sig figs
    if "." in formatted and decimal_places > 0:
        integer_part, decimal_part = formatted.split(".")
        visible_digits = len(integer_part.lstrip("0"))

        # If we need more digits to hit our significant figures, keep zeros
        if visible_digits < sig_figs:
            needed_decimals = sig_figs - visible_digits
            # Ensure we have exactly the needed decimals (pad with zeros if needed)
            if len(decimal_part) < needed_decimals:
                decimal_part += "0" * (needed_decimals - len(decimal_part))
            else:
                decimal_part = decimal_part[:needed_decimals]

            formatted = f"{integer_part}.{decimal_part}"
        else:
            # If all sig figs are in the integer part, remove decimal entirely
            formatted = integer_part

    return formatted


# Function to align decimal points in a column
def align_decimal_points(values: List[str], markdown: bool) -> List[str]:
    """Align decimal points in a column of values.

    Args:
        values: List of formatted string values

    Returns:
        List of aligned string values with proper decimal alignment
    """
    # Find the maximum number of digits before and after the decimal point
    max_before_decimal = 0
    max_after_decimal = 0

    for val in values:
        if "." in val:
            before, after = val.split(".")
            max_before_decimal = max(max_before_decimal, len(before))
            max_after_decimal = max(max_after_decimal, len(after))

    # Align each value
    aligned_values = []
    for val in values:
        if "." in val:
            before, after = val.split(".")
            # Right-align the part before decimal, left-align the part after decimal
            if markdown:
                # For Markdown, just use the original values without extra spaces
                aligned = f"{before}.{after}"
            else:
                # For console output, use fixed-width alignment
                aligned = f"{before.rjust(max_before_decimal)}.{after.ljust(max_after_decimal)}"
        else:
            # For integer values, right-align and add spaces for where decimal point would be
            if markdown:
                # For Markdown, just use the original values without extra spaces
                aligned = val
            else:
                # For console output, use fixed-width alignment
                aligned = (
                    f"{val.rjust(max_before_decimal)}{' ' * (1 + max_after_decimal)}"
                )

        aligned_values.append(aligned)

    return aligned_values


def _workload_identity_terminal_subgroup(
    metric: ElbenchoMetrics,
) -> Tuple[str, str | int]:
    """Subgroup workload id: read-from path if set, else configured file_size_bytes (not treescan)."""
    read_from = (metric.sweep_read_from_path or "").strip()
    if read_from:
        return ("read_from", read_from)
    return ("file_size", metric.file_size_bytes)


def _terminal_table_subgroup_key(metric: ElbenchoMetrics) -> Tuple[Any, ...]:
    """Hashable key for plain-text table buckets (excludes datestamp)."""
    return (
        metric.is_multi_node,
        metric.direct_io,
        metric.random_io,
        metric.io_duration_sec,
        _workload_identity_terminal_subgroup(metric),
    )


def _terminal_table_bucket_sort_key(key: Tuple[Any, ...]) -> Tuple[Any, ...]:
    """Sort order between subgroup buckets: dio before bio, then SN/MN, seq/rand, duration, workload."""
    is_mn, dio, rand_io, dur, workload = key
    return (-dio, is_mn, rand_io, dur, workload)


def _terminal_table_row_sort_key(metric: ElbenchoMetrics) -> Tuple[int, int, int, str]:
    """Row order within a subgroup; datestamp disambiguates merged runs at the same grid point."""
    return (
        metric.nodes,
        metric.io_depth * metric.threads,
        metric.threads,
        metric.datestamp,
    )


def _terminal_table_subgroups(
    op_metrics: List[ElbenchoMetrics],
) -> List[List[ElbenchoMetrics]]:
    """Split metrics into separate terminal tables by sweep-equivalence (not datestamp).

    Buckets share the same multi-node flag, Direct/Buffered IO, Rand/Seq, duration,
    and workload (read-from path if set, else configured file_size_bytes). Treescan
    averages are not used for bucketing.
    """
    buckets: DefaultDict[Tuple[Any, ...], List[ElbenchoMetrics]] = defaultdict(list)
    for m in op_metrics:
        buckets[_terminal_table_subgroup_key(m)].append(m)
    keys_sorted = sorted(buckets.keys(), key=_terminal_table_bucket_sort_key)
    out: List[List[ElbenchoMetrics]] = []
    for key in keys_sorted:
        rows = sorted(buckets[key], key=_terminal_table_row_sort_key)
        out.append(rows)
    return out


def print_terminal_table(metrics: List[ElbenchoMetrics]) -> None:
    """Print formatted metrics table in terminal text format.

    Args:
        metrics: List of ElbenchoMetrics objects to display
    """
    if not metrics:
        print("No metrics to display")
        return

    # Define table headers with shortened names for better readability
    base_headers = [
        "Nodes",
        "Thrds",
        "IODep",
        "IOPS",
        "AvgBW(GB/s)",
        "Gb/s",
        "MinLat (ms)",
        "AvgLat (ms)",
        "MaxLat (ms)",
        "50% (ms)",
        "75% (ms)",
        "99% (ms)",
    ]

    # Group metrics by IO size; within each size/op, split by sweep-equivalence (not
    # datestamp) so similar runs merge; rows disambiguate merged runs via datestamp order.
    metrics_by_size = {}
    for metric in metrics:
        io_size = metric.io_size
        if io_size not in metrics_by_size:
            metrics_by_size[io_size] = []
        metrics_by_size[io_size].append(metric)

    # Sort IO sizes for consistent ordering based on the write phase value
    io_sizes = sorted(
        metrics_by_size.keys(), key=lambda size: get_size_in_bytes(size, "WRITE")
    )

    # Print each IO size section
    for io_size in io_sizes:
        size_metrics = metrics_by_size[io_size]

        # Split metrics by operation type
        write_metrics = [m for m in size_metrics if m.operation == "WRITE"]
        read_metrics = [m for m in size_metrics if m.operation == "READ"]

        # WRITE operation metrics first, then READ
        for op_label, op_metrics in [("WRITE", write_metrics), ("READ", read_metrics)]:
            if not op_metrics:
                continue

            for sub_metrics in _terminal_table_subgroups(op_metrics):
                # Subgroup buckets are homogeneous; first row is representative.
                is_multi_node = sub_metrics[0].is_multi_node
                direct_io_flag = sub_metrics[0].direct_io
                random_io = sub_metrics[0].random_io
                metadata = metrics_metadata(
                    is_multi_node, direct_io_flag, random_io, sub_metrics
                )
                dur_label = section_duration_label_sec(sub_metrics)
                fs_label = section_file_size_label_bytes(sub_metrics)

                # Print section header in plain text format
                print(
                    f"\n=== IO Size: {io_size} - {fs_label}{op_label} - {metadata['io_order']} - "
                    f"{metadata['io_mode_str']} - {dur_label}"
                    f"{join_datestamps(metadata['datestamps'])} ==="
                )
                print_sweep_auxiliary_lines_for_metrics(sub_metrics)
                print_workload_metadata_for_metrics(sub_metrics)

                show_dur = any(
                    _metric_should_show_csv_phase_dur(m) for m in sub_metrics
                )
                table_headers = base_headers + (["Dur", "DurTot"] if show_dur else [])

                # Prepare data rows with raw values
                rows = []

                # Collect columns that need decimal alignment: GB/s + 6 latency columns
                gb_s_col = []
                latency_cols = [
                    [] for _ in range(6)
                ]  # 6 latency columns for regular output

                for metric in sub_metrics:
                    # Convert and format GB/s value
                    gb_s_value = mb_s_to_gb_s(metric.throughput_mb_s)
                    gb_s_fmt = format_gb_s(gb_s_value)
                    gb_s_col.append(gb_s_fmt)

                    # Convert latency values to ms for display
                    min_lat_ms = metric.min_lat_sec * 1000
                    avg_lat_ms = metric.avg_lat_sec * 1000
                    max_lat_ms = metric.max_lat_sec * 1000
                    lat_pct_50_ms = metric.lat_pct_50 * 1000
                    lat_pct_75_ms = metric.lat_pct_75 * 1000
                    lat_pct_99_ms = metric.lat_pct_99 * 1000

                    # Format all values
                    min_lat_fmt = format_with_sig_figs(min_lat_ms)
                    avg_lat_fmt = format_with_sig_figs(avg_lat_ms)
                    max_lat_fmt = format_with_sig_figs(max_lat_ms)
                    lat_pct_50_fmt = format_with_sig_figs(lat_pct_50_ms)
                    lat_pct_75_fmt = format_with_sig_figs(lat_pct_75_ms)
                    lat_pct_99_fmt = format_with_sig_figs(lat_pct_99_ms)

                    # Store formatted latency values for alignment
                    latency_cols[0].append(min_lat_fmt)
                    latency_cols[1].append(avg_lat_fmt)
                    latency_cols[2].append(max_lat_fmt)
                    latency_cols[3].append(lat_pct_50_fmt)
                    latency_cols[4].append(lat_pct_75_fmt)
                    latency_cols[5].append(lat_pct_99_fmt)

                    # Store other non-latency metrics (except GB/s which needs alignment)
                    # Prepare the row data with placeholders for GB/s and latency values
                    base_row = [
                        str(metric.nodes),
                        str(metric.threads),
                        str(metric.io_depth),
                        f"{int(metric.iops):,d}",  # Always use comma formatting for readability
                        None,  # Placeholder for GB/s (will be filled after alignment)
                        f"{metric.throughput_gbps:.2f}",
                    ]

                    # Add placeholders for latency columns
                    row = base_row + [
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                    ]  # 6 placeholder values for regular output
                    if show_dur:
                        row.append(None)
                        row.append(None)
                    rows.append(row)

                # Align the decimal points in GB/s column
                aligned_gb_s_col = align_decimal_points(gb_s_col, False)

                # Align the decimal points in latency columns
                aligned_latency_cols = []
                for lat_col in latency_cols:
                    aligned_latency_cols.append(align_decimal_points(lat_col, False))

                # Calculate column widths
                col_widths = [len(h) for h in table_headers]

                # Update widths for non-aligned columns (excluding GB/s at index 4 and latency at indices 6+)
                for row in rows:
                    for i, val in enumerate(row[:6]):
                        if (
                            val is not None and i != 4
                        ):  # Skip GB/s placeholder at index 4
                            col_widths[i] = max(col_widths[i], len(val))

                # Update width for aligned GB/s column (index 4)
                for val in aligned_gb_s_col:
                    col_widths[4] = max(col_widths[4], len(val))

                # Update widths for aligned latency columns
                for i, aligned_col in enumerate(aligned_latency_cols):
                    col_index = 6 + i  # Latency columns start at index 6
                    if col_index < len(
                        col_widths
                    ):  # Make sure we don't go out of bounds
                        for val in aligned_col:
                            col_widths[col_index] = max(col_widths[col_index], len(val))

                # Apply aligned GB/s values to rows (index 4)
                for row_idx, row in enumerate(rows):
                    row[4] = aligned_gb_s_col[row_idx]

                # Apply aligned latency values to rows
                for row_idx, row in enumerate(rows):
                    for col_idx, aligned_col in enumerate(aligned_latency_cols):
                        row[6 + col_idx] = aligned_col[
                            row_idx
                        ]  # Latency columns start at index 6

                if show_dur:
                    dur_col = len(base_headers)
                    dur_tot_col = dur_col + 1
                    for row_idx, row in enumerate(rows):
                        metric = sub_metrics[row_idx]
                        if _metric_should_show_csv_phase_dur(metric):
                            dur_str = format_dur_xmys_ms(metric.phase_first_duration_ms)
                            dur_tot_str = format_dur_xmys_ms(
                                metric.phase_wall_duration_ms
                            )
                        else:
                            dur_str = ""
                            dur_tot_str = ""
                        row[dur_col] = dur_str
                        row[dur_tot_col] = dur_tot_str
                        col_widths[dur_col] = max(col_widths[dur_col], len(dur_str))
                        col_widths[dur_tot_col] = max(
                            col_widths[dur_tot_col], len(dur_tot_str)
                        )

                # Ensure minimum widths
                min_widths = [
                    5,
                    5,
                    5,
                    8,
                    12,
                    5,
                    10,
                    10,
                    10,
                    7,
                    7,
                    7,
                ]  # 12 columns for regular output
                if show_dur:
                    min_widths.append(3)
                    min_widths.append(6)
                for i, col_width in enumerate(col_widths):
                    if i < len(min_widths):  # Make sure we don't go out of bounds
                        col_widths[i] = max(col_width, min_widths[i])

                # Create format strings for columns
                format_strings = []
                for i in range(len(table_headers)):
                    if i <= 2:  # Left-align text columns (Nodes, Threads, IODep)
                        format_strings.append(f"{{:<{col_widths[i]}}}")
                    else:  # Right-align numeric columns
                        format_strings.append(f"{{:>{col_widths[i]}}}")

                # Join with pipe separator
                row_format = " | ".join(format_strings)

                # Print headers
                print(row_format.format(*table_headers))
                print("-" * (sum(col_widths) + 3 * (len(col_widths) - 1)))

                # Print data rows
                for row in rows:
                    print(row_format.format(*row))

    print("")  # Add blank line at end


def print_markdown_table(
    metrics: List[ElbenchoMetrics], no_dual_y_axis: bool = False
) -> None:
    """Print formatted metrics table in Markdown format.

    Args:
        metrics: List of ElbenchoMetrics objects to display
        no_dual_y_axis: If True, reference separate single-axis plots instead of dual y-axis plots
    """
    if not metrics:
        print("No metrics to display")
        return

    clustered_metrics = cluster_metrics_for_reporting(metrics)

    # Create a main heading for all data
    print("# Elbencho Benchmark Results")

    # Create a list of unique runs performed
    unique_sn_runs = set()  # (io_size, dio_bio, datestamp, threads, io_depths) tuples
    unique_mn_runs = (
        set()
    )  # (io_size, dio_bio, datestamp, max_nodes, threads, io_depths) tuples

    for is_multi_node, by_mn in clustered_metrics.items():
        for operation, by_op_metrics in by_mn.items():
            for direct_io, annotated_size_groups in by_op_metrics.items():
                for annotated_size_group in annotated_size_groups:
                    for io_size, metric_list in annotated_size_group[
                        "metrics_by_size"
                    ].items():
                        if not is_multi_node:
                            unique_sn_runs.add(
                                (
                                    io_size,
                                    direct_io,
                                    metric_list[0].datestamp,
                                    ",".join(
                                        sorted({str(m.threads) for m in metric_list})
                                    ),
                                    ",".join(
                                        sorted(
                                            {str(m.io_depth) for m in metric_list},
                                            key=int,
                                        )
                                    ),
                                )
                            )
                        else:
                            unique_mn_runs.add(
                                (
                                    io_size,
                                    direct_io,
                                    metric_list[0].datestamp,
                                    max(m.nodes for m in metric_list),
                                    ",".join(
                                        sorted({str(m.threads) for m in metric_list})
                                    ),
                                    ",".join(
                                        sorted(
                                            {str(m.io_depth) for m in metric_list},
                                            key=int,
                                        )
                                    ),
                                )
                            )

    if unique_sn_runs:
        # Sort unique single-node runs by dio_bio, IO size (write phase value), datestamp
        # (direct io first, so we "not" the boolean)
        unique_sn_runs = sorted(
            unique_sn_runs,
            key=lambda t: (not t[1], get_size_in_bytes(t[0], "WRITE"), t[2]),
        )

        # Print list of unique runs
        print("\n# Unique Single-Node Runs Performed")
        print("\n| Mode | Wr | Rd | Datestamp | Threads | IODepths |")
        print("|:-----|:-------|:-------|:----------|:---------------|:---------|")

        for io_size, direct_io, datestamp, threads, io_depths in unique_sn_runs:
            # Parse the IO size string to get write and read components
            write_size, read_size = parse_size_string(io_size)

            # Parse order and clean size for write
            write_order, write_size_clean = parse_size_with_order(write_size)

            # Parse order and clean size for read
            read_order, read_size_clean = parse_size_with_order(read_size)

            # Format columns
            io_mode = "DirectIO" if direct_io else "BufferedIO"
            write_col = f"{write_order} {write_size_clean}"
            read_col = f"{read_order} {read_size_clean}"

            print(
                f"| {io_mode} | {write_col} | {read_col} | {datestamp} | {threads} | {io_depths} |"
            )

    if unique_mn_runs:
        # Sort unique multi-node runs by dio_bio, IO size (write phase value), datestamp
        # (direct io first, so we "not" the boolean)
        unique_mn_runs = sorted(
            unique_mn_runs,
            key=lambda t: (not t[1], get_size_in_bytes(t[0], "WRITE"), t[2]),
        )

        # Print list of unique runs
        print("\n# Unique Multi-Node Runs Performed")
        print("\n| Mode | Wr | Rd | Datestamp | Max Nodes | Threads | IODepths |")
        print(
            "|:-----|:-------|:-------|:----------|:---------------|:-------------|:---------|"
        )

        for (
            io_size,
            direct_io,
            datestamp,
            max_nodes,
            threads,
            io_depths,
        ) in unique_mn_runs:
            # Parse the IO size string to get write and read components
            write_size, read_size = parse_size_string(io_size)

            # Parse order and clean size for write
            write_order, write_size_clean = parse_size_with_order(write_size)

            # Parse order and clean size for read
            read_order, read_size_clean = parse_size_with_order(read_size)

            # Format columns
            io_mode = "DirectIO" if direct_io else "BufferedIO"
            write_col = f"{write_order} {write_size_clean}"
            read_col = f"{read_order} {read_size_clean}"

            print(
                f"| {io_mode} | {write_col} | {read_col} | {datestamp} | {max_nodes} | {threads} | {io_depths} |"
            )

    # Metrics are grouped by sn/mn, operation type, dio/bio, and then by size group
    for is_multi_node, by_mn in clustered_metrics.items():
        for operation, by_op_metrics in by_mn.items():
            for direct_io, annotated_size_groups in by_op_metrics.items():
                sn_mn_str = "MN" if is_multi_node else "SN"

                # Create a subsection for this operation
                print(
                    f"\n# {sn_mn_str} {operation} Operation ({'DirectIO' if direct_io else 'BufferedIO'})"
                )

                for annotated_size_group in annotated_size_groups:
                    # Get random_io from first metric (should be same for all in group)
                    first_metrics = next(
                        iter(annotated_size_group["metrics_by_size"].values())
                    )
                    random_io = first_metrics[0].random_io

                    group_name = annotated_size_group["group_name"]
                    size_group_list = annotated_size_group["size_group"]
                    # Create group heading
                    if len(size_group_list) == 1:
                        group_heading = f"IO Size: {size_group_list[0]}"
                    else:
                        # Join all sizes with forward slashes for the heading
                        group_heading = f"IO Size: {' / '.join(size_group_list)}"

                    # Print the section heading
                    print(f"\n## {group_heading}")

                    # Add image placeholders for charts
                    print("\n### Charts")
                    print("\n*Insert the following images here:*\n")

                    metadata = plot_metadata(
                        is_multi_node, direct_io, random_io, annotated_size_group
                    )

                    # Determine which images to reference based on no_dual_y_axis flag
                    if no_dual_y_axis:
                        # Generate references for 3 single-axis plots
                        if get_size_in_bytes(size_group_list[-1], operation) <= 2**16:
                            # Small IO - IOPS is more relevant
                            image_types = [
                                ("iops", f"IOPS vs. {metadata['x_label']}"),
                            ]
                        else:
                            # Large IO - Bandwidth is more relevant
                            image_types = [
                                ("bw", f"Bandwidth vs. {metadata['x_label']}"),
                            ]
                        # Always add latency for no-dual-y-axis mode
                        image_types.append(
                            ("latency", f"Latency vs. {metadata['x_label']}")
                        )
                    else:
                        # Original logic - dual axis plots
                        if get_size_in_bytes(size_group_list[-1], operation) <= 2**16:
                            image_types = [
                                (
                                    "iops-latency",
                                    f"IOPS/Latency vs. {metadata['x_label']}",
                                ),
                            ]
                        else:
                            image_types = [
                                (
                                    "bw-latency",
                                    f"Bandwidth/Latency vs. {metadata['x_label']}",
                                ),
                            ]
                    # Only include scaling efficiency plots for multi-node cases
                    if is_multi_node:
                        image_types.append(
                            (
                                "bw-efficiency",
                                f"Throughput Scale Efficiency vs. {metadata['x_label']}",
                            )
                        )
                    image_types.append(("latency-hist", "Latency Histogram"))

                    # Print image filenames as a list
                    for img_type, img_desc in image_types:
                        img_filename = plot_filename(
                            metadata["sn_or_mn"],
                            operation,
                            group_name,
                            metadata["dio_or_bio"],
                            img_type,
                            metadata["datestamps"],
                        )
                        print(f"* **{img_desc}**: `{img_filename}`")

                    # Create performance metrics section
                    print(
                        f"\n### Performance Metrics ({' / '.join(annotated_size_group['size_group'])})"
                    )

                    # Process each IO size within this group separately for metrics tables
                    for io_size in annotated_size_group["metrics_by_size"]:
                        print_markdown_metrics_table_and_command(
                            annotated_size_group["metrics_by_size"][io_size],
                            io_size,
                            operation,
                        )

    # Add metrics explanation at the end
    print("\n# Metrics Explanation")
    print("\n* **IOPS**: Input/Output Operations Per Second")
    print("* **AvgBW**: Average Bandwidth in GB/s")
    print("* **Gb/s**: Gigabits per second conversion of the bandwidth")
    print("* **Latency metrics**: All latency values are in milliseconds (ms)")
    print("  * **MinLat/AvgLat/MaxLat**: Minimum, Average, and Maximum latency")
    print(
        "  * **99%**: 99th percentile latency (representative of worst-case performance)"
    )
    print(
        "* **Dur**: Phase duration from CSV ``time ms [first]`` (aligned with elbencho "
        "``[first]`` throughput/IOPS)"
    )
    print("* **DurTot**: Total wall time from CSV ``time ms [last]`` (last completion)")


def test_parse(base_filename: str) -> None:
    """Test the parse_elbencho_file function on a specific file."""
    # Strip any extension
    base_filename = re.sub(CSV_OUT_EXT_RE, "", base_filename)

    metrics = parse_elbencho_files(base_filename)
    for metric in metrics:
        # Core identification metadata
        print(f"Operation: {metric.operation}")
        print(f"IO Size: {metric.io_size}")
        print(f"Nodes: {metric.nodes}")
        print(f"Threads: {metric.threads}")
        print(f"Datestamp: {metric.datestamp}")

        # Throughput metrics (display all formats)
        print(f"IOPS: {metric.iops:.2f}")
        print(f"Throughput (MiB/s): {metric.throughput_mib_s:.2f}")
        print(f"Throughput (GB/s): {mb_s_to_gb_s(metric.throughput_mb_s):.3f}")
        print(f"Throughput (Gbps): {metric.throughput_gbps:.2f}")

        # Latency metrics (all in ms for easier reading)
        print(f"Min Latency: {metric.min_lat_sec * 1000:.3f} ms")
        print(f"Avg Latency: {metric.avg_lat_sec * 1000:.3f} ms")
        print(f"Max Latency: {metric.max_lat_sec * 1000:.3f} ms")

        # Latency percentiles (all in ms)
        print(f"Lat 1%: {metric.lat_pct_1 * 1000:.3f} ms")
        print(f"Lat 50%: {metric.lat_pct_50 * 1000:.3f} ms")
        print(f"Lat 75%: {metric.lat_pct_75 * 1000:.3f} ms")
        print(f"Lat 99%: {metric.lat_pct_99 * 1000:.3f} ms")

        # IO mode information
        print(
            f"Direct IO: {metric.direct_io} ({'DirectIO' if metric.direct_io == 1 else 'BufferedIO'})"
        )

        # File size information
        if metric.file_size_bytes > 0:
            readable_size = bytes_to_elbencho_size_string(metric.file_size_bytes)
            print(f"File Size: {metric.file_size_bytes} bytes ({readable_size})")
        else:
            print("File Size: Not available")

        # Command information
        if metric.command:
            print(f"Command: {metric.command}")
        else:
            print("Command: Not available")

        # Histogram data
        print(f"Histogram (contains {len(metric.histogram)} entries):")
        if metric.histogram:
            # Only show the first 10 entries if there are many
            items = sorted([(float(k), v) for k, v in metric.histogram.items()])
            display_count = min(10, len(items))
            for bucket, count in items[:display_count]:
                print(f"  {bucket * 1000:.3f} ms: {count}")
            if len(items) > 10:
                print(f"  ... {len(items) - 10} more entries (showing first 10 only)")
        else:
            print("  No histogram data available")
        print()


# Helper function to parse value lists with ranges
def parse_int_values_with_ranges(value_str):
    """Parse a comma-separated list of integers or ranges.

    Ranges are specified as 'start-end'. Returns a set of integer values.
    Example: '1,2,5-10,15' would return {1, 2, 5, 6, 7, 8, 9, 10, 15}
    """
    result = set()
    if not value_str:
        return result

    for part in value_str.split(","):
        part = part.strip()
        if "-" in part:
            # Handle range (e.g., "1-10")
            try:
                start, end = map(int, part.split("-", 1))
                # Add all integers in the range (inclusive)
                result.update(range(start, end + 1))
            except ValueError:
                eprint(
                    f"Warning: Invalid range format: {part}. Using individual values only."
                )
                continue
        else:
            # Handle individual value
            try:
                result.add(int(part))
            except ValueError:
                eprint(f"Warning: Invalid integer value: {part}. Skipping.")
                continue

    return result


def parse_benchmark_filename(filename: str) -> Optional[Dict[str, Any]]:
    """Parse an elbencho benchmark filename to extract parameters.

    Args:
        filename: Benchmark filename

    Returns:
        Dictionary with parsed parameters or None if parsing failed
    """
    # Skip files with "elbencho-sweep" prefix as they are not benchmark result files
    if filename.startswith("elbencho-sweep"):
        return None

    # Format: elbencho-<size_string>-c_<node_count>-s_<thread_count>[-d_<io_depth>]_<datestamp>
    # The -d_<io_depth> segment is optional for backward compatibility
    match = re.match(
        r"elbencho-([^-]+)-c_(\d+)-s_(\d+)(?:-d_(\d+))?_(\d{8}Z\d{6})$", filename
    )
    if not match:
        return None

    io_size_str, nodes_str, threads_str, io_depth_str, datestamp = match.groups()

    return {
        "io_size": io_size_str,
        "nodes": int(nodes_str),
        "threads": int(threads_str),
        "io_depth": (
            int(io_depth_str) if io_depth_str else 1
        ),  # Default to 1 for old format
        "datestamp": datestamp,
    }


def discover_live_csv_files(input_dir: str) -> List[LiveFileMetadata]:
    """Discover live CSVs and associate them with sibling benchmark parameters."""
    discovered = []
    for filepath in sorted(glob.glob(os.path.join(input_dir, "*.live.csv"))):
        basename = os.path.basename(filepath)
        sibling_stem = basename[: -len(".live.csv")]
        params = parse_benchmark_filename(sibling_stem)
        if not params:
            eprint(f"Warning: Ignoring live CSV with unrecognized name: {filepath}")
            continue
        discovered.append(
            LiveFileMetadata(
                path=filepath,
                io_size=params["io_size"],
                nodes=params["nodes"],
                threads=params["threads"],
                io_depth=params["io_depth"],
                datestamp=params["datestamp"],
            )
        )
    return discovered


def _live_metadata_matches_filters(
    metadata: LiveFileMetadata,
    size_filter: Optional[Set[str]],
    thread_filter: Optional[Set[int]],
    node_filter: Optional[Set[int]],
    iodepth_filter: Optional[Set[int]],
) -> bool:
    """Return whether one live file passes the aggregate-report CLI filters."""
    checks = (
        size_filter is None or metadata.io_size in size_filter,
        thread_filter is None or metadata.threads in thread_filter,
        node_filter is None or metadata.nodes in node_filter,
        iodepth_filter is None or metadata.io_depth in iodepth_filter,
    )
    return all(checks)


def _report_one_live_file(
    metadata: LiveFileMetadata,
    output_dir: str,
    per_client_plots: bool,
    z_threshold: float,
    min_underperform_segments: int,
    max_timeseries_lines: int,
    max_heatmap_rows: int,
) -> bool:
    """Analyze one live CSV and generate aggregate and optional client reports."""
    analysis = analyze_live_csv(metadata, z_threshold)
    eprint(f"Analyzed live CSV: {metadata.path}")
    if not analysis.domains:
        eprint(f"Warning: No valid live domains found in {metadata.path}")
        return False
    for domain_key in sorted(analysis.domains):
        domain = analysis.domains[domain_key]
        _report_live_domain(
            analysis,
            metadata,
            domain_key,
            domain,
            output_dir,
            per_client_plots,
            min_underperform_segments,
            max_timeseries_lines,
            max_heatmap_rows,
        )
    return True


def _report_live_domain(
    analysis: LiveAnalysis,
    metadata: LiveFileMetadata,
    domain_key: LiveDomainKey,
    domain: DomainAnalysis,
    output_dir: str,
    per_client_plots: bool,
    min_underperform_segments: int,
    max_timeseries_lines: int,
    max_heatmap_rows: int,
) -> None:
    """Generate aggregate and optional client reports for one live domain."""
    aggregate_path = plot_aggregate_timeseries(metadata, domain, output_dir)
    if aggregate_path:
        eprint(f"Generated live aggregate plot: {aggregate_path}")
    elif not domain.aggregate_points:
        eprint(f"No Rank=Total rows for {metadata.stem} {domain_key.label}")
    if domain.diagnostics:
        diagnostics = ", ".join(
            f"{name}={count}" for name, count in sorted(domain.diagnostics.items())
        )
        eprint(f"Live CSV diagnostics for {domain_key.label}: {diagnostics}")
    if not per_client_plots:
        return
    _report_live_client_domain(
        analysis,
        metadata,
        domain_key,
        domain,
        output_dir,
        min_underperform_segments,
        max_timeseries_lines,
        max_heatmap_rows,
    )


def _report_live_client_domain(
    analysis: LiveAnalysis,
    metadata: LiveFileMetadata,
    domain_key: LiveDomainKey,
    domain: DomainAnalysis,
    output_dir: str,
    min_underperform_segments: int,
    max_timeseries_lines: int,
    max_heatmap_rows: int,
) -> None:
    """Generate bounded-detail client reports for one extended live domain."""
    if not domain.has_extended_rows:
        eprint(
            f"No extended client rows for {metadata.stem} "
            f"{domain_key.label}; skipping per-client reports"
        )
        return
    summary_paths = write_client_summaries(metadata, domain, output_dir)
    eprint(f"Generated live client summaries: {', '.join(summary_paths)}")
    prioritized = select_clients(
        domain,
        min_underperform_segments,
        max(max_timeseries_lines, max_heatmap_rows),
    )
    timeseries_clients = prioritized[:max_timeseries_lines]
    heatmap_clients = select_heatmap_clients(domain, prioritized, max_heatmap_rows)
    if not prioritized:
        eprint(
            f"No clients met the underperformance threshold for "
            f"{metadata.stem} {domain_key.label}; "
            "generating a representative fleet heatmap"
        )
    detail_clients = list(dict.fromkeys([*timeseries_clients, *heatmap_clients]))
    detail = collect_selected_client_detail(analysis, domain, detail_clients)
    report_paths = (
        plot_client_timeseries(
            metadata,
            domain,
            detail,
            timeseries_clients,
            output_dir,
        ),
        plot_underperformance_heatmap(
            metadata,
            domain,
            detail,
            heatmap_clients,
            output_dir,
        ),
    )
    for report_path in report_paths:
        if report_path:
            eprint(f"Generated live per-client plot: {report_path}")


def _elbencho_find_last_operation_section(
    content: str,
    operation: str,
) -> Optional[str]:
    """Return the last READ/WRITE Elapsed time block in a (possibly resumed) .out."""
    op_pattern = rf"{operation}\s+Elapsed time.*?(?:---|\Z)"
    matches = list(re.finditer(op_pattern, content, re.DOTALL))
    if not matches:
        return None
    return matches[-1].group(0)


def _elbencho_apply_out_section_histogram_and_percentiles(
    metric: ElbenchoMetrics,
    op_text: str,
) -> None:
    """Fill histogram and IO latency percentiles from one .out operation section."""
    lat_pct_match = re.search(r"IO lat % us\s+:\s+\[ ([^\]]+) \]", op_text)
    if lat_pct_match:
        percentiles_str = lat_pct_match.group(1)

        def extract_percentile(pattern: str, default: float = 0) -> float:
            match = re.search(pattern, percentiles_str)
            if match:
                return float(match.group(1)) / 1_000_000
            return default

        metric.lat_pct_1 = extract_percentile(r"1%<=(\d+)")
        metric.lat_pct_50 = extract_percentile(r"50%<=(\d+)")
        metric.lat_pct_75 = extract_percentile(r"75%<=(\d+)")
        metric.lat_pct_99 = extract_percentile(r"99%<=(\d+)")

    hist_match = re.search(r"IO lat hist\s+:\s+\[ ([^\]]+) \]", op_text)
    if hist_match:
        hist_str = hist_match.group(1)
        metric.histogram = {}
        for item in hist_str.split(","):
            item = item.strip()
            if ":" not in item:
                continue
            bucket, count = item.split(":", 1)
            bucket = bucket.strip()
            count = count.strip()
            if count.isdigit():
                bucket_sec = float(bucket) / 1_000_000
                metric.histogram[bucket_sec] = int(count)


def _elbencho_dedupe_latest_operation_metrics(
    metrics: List[ElbenchoMetrics],
) -> List[ElbenchoMetrics]:
    """Keep one coherent latest attempt when resume retries append CSV rows."""
    latest_attempt: List[ElbenchoMetrics] = []
    last_rank = -1
    operation_rank = {"WRITE": 0, "READ": 1}
    for metric in metrics:
        op = metric.operation.upper()
        if op not in operation_rank:
            continue
        rank = operation_rank[op]
        # A repeated operation or a move back from READ to WRITE starts a new
        # attempt. Do not combine its partial phases with an older attempt.
        if latest_attempt and rank <= last_rank:
            latest_attempt = []
        latest_attempt.append(metric)
        last_rank = rank
    return latest_attempt


def add_elbencho_out_file_metrics(
    out_file: str,
    metrics: List[ElbenchoMetrics],
) -> None:
    """Parse an elbencho .out file, extract histogram and percentile data,
       and add them to the given metrics.

    Args:
        out_file: Path to the .out file
        metrics: List of ElbenchoMetrics objects to which we add data
    """

    # Read the file
    with open(out_file, "r", encoding="utf-8") as f:
        content = f.read()

    wr_dur = _WR_IO_DURATION_LOG_RE.search(content)
    if wr_dur:
        sweep_secs = int(wr_dur.group(1))
        for metric in metrics:
            if metric.io_duration_sec == 0:
                metric.io_duration_sec = sweep_secs

    apply_sweep_auxiliary_log_lines(content, metrics)

    # Parse the output file for each operation (last section wins on resume append)
    for metric in metrics:
        operation = metric.operation
        op_text = _elbencho_find_last_operation_section(content, operation)
        if not op_text:
            continue
        _elbencho_apply_out_section_histogram_and_percentiles(metric, op_text)


def _elbencho_csv_first_line_is_headerless(first_line: str) -> bool:
    """True when the first line is data (ISO timestamp) and not a column header row."""
    if not first_line.strip():
        return False
    try:
        row = next(csv.reader([first_line]))
    except csv.Error:
        return False
    if not row:
        return False
    first = row[0].strip()
    if first == _ELBE_CSV_LABEL_FIRST_CELL:
        return False
    return bool(_ELBE_CSV_TS_FIRST_LINE_RE.match(first))


def _find_elbencho_csv_op_index(row: List[str]) -> Optional[int]:
    for idx, cell in enumerate(row):
        if cell in _ELBE_CSV_KNOWN_PHASE_OPS:
            return idx
    return None


def _elbencho_headerless_row_to_dict(row: List[str]) -> Optional[Dict[str, str]]:
    """Build a DictReader-shaped row from a headerless Statistics CSV line."""
    op_idx = _find_elbencho_csv_op_index(row)
    if op_idx is None or op_idx < _ELBE_CSV_FILE_SIZE_COLS_BEFORE_OP:
        return None
    min_len = op_idx + _ELBE_CSV_OFF_OP_TO_IO_LAT_MIN + 3
    if len(row) < min_len:
        return None
    fs_idx = op_idx - _ELBE_CSV_FILE_SIZE_COLS_BEFORE_OP
    cmd = row[-1]
    iops_idx = op_idx + _ELBE_CSV_OFF_OP_TO_IOPS_FIRST
    mibs_idx = op_idx + _ELBE_CSV_OFF_OP_TO_MIBS_FIRST
    lat0 = op_idx + _ELBE_CSV_OFF_OP_TO_IO_LAT_MIN
    out: Dict[str, str] = {
        "operation": row[op_idx],
        CSV_COL_TIME_MS_FIRST: row[op_idx + 1],
        CSV_COL_TIME_MS_LAST: row[op_idx + 2],
        "IOPS [first]": row[iops_idx],
        "MiB/s [first]": row[mibs_idx],
        CSV_COL_IO_LAT_US_MIN: row[lat0],
        CSV_COL_IO_LAT_US_AVG: row[lat0 + 1],
        CSV_COL_IO_LAT_US_MAX: row[lat0 + 2],
        "direct IO": "1" if "--direct" in cmd else "0",
        "random": "1" if "--rand" in cmd else "0",
        "command": cmd,
        "file size": row[fs_idx],
    }
    # IOPS/MiB [last] sit at +1 past [first] in the phase block (see comment on offsets).
    if len(row) >= op_idx + 9:
        out["IOPS [last]"] = row[op_idx + 6]
        out["MiB/s [last]"] = row[op_idx + 8]
    return out


def _elbencho_csv_parse_direct_io(row: Dict[str, str]) -> int:
    try:
        return 1 if row["direct IO"] == "1" else 0
    except KeyError:
        eprint("Error: CSV is missing required 'direct IO' column")
        sys.exit(1)


def _elbencho_csv_parse_random_io(row: Dict[str, str]) -> int:
    try:
        return 1 if row["random"] == "1" else 0
    except KeyError:
        eprint(
            "Warning: 'random' column not found in CSV row "
            f"{row.get('operation', '')}. Defaulting to sequential (0)."
        )
        return 0


def _elbencho_csv_parse_command(row: Dict[str, str]) -> str:
    try:
        command = strip_hosts_from_command(row["command"])
    except KeyError:
        eprint(
            "Warning: 'command' column not found in CSV. "
            f"Available columns: {list(row.keys())}"
        )
        return ""
    if not command and "command" in row:
        eprint(
            "Warning: Command field is present but empty in row: "
            f"{row.get('operation', '')}"
        )
    return command


def _elbencho_csv_parse_float_field(row: Dict[str, str], key: str) -> float:
    try:
        return float(row.get(key, 0) or 0)
    except (ValueError, TypeError):
        return 0.0


def _elbencho_csv_parse_io_lat_us_to_sec(row: Dict[str, str], key: str) -> float:
    if key not in row or not row[key]:
        return 0.0
    try:
        return float(row[key]) / 1_000_000
    except (ValueError, TypeError) as exc:
        eprint(f"Warning: Error parsing latency values: {exc}")
        return 0.0


def _elbencho_csv_parse_file_size_bytes(row: Dict[str, str]) -> int:
    try:
        return int(row["file size"])
    except (ValueError, TypeError, KeyError) as exc:
        eprint(f"Warning: Error parsing file size: {exc}")
        return 0


def _elbencho_csv_row_has_numeric_metrics(
    iops: float,
    throughput_mib_s: float,
    min_lat_sec: float,
    avg_lat_sec: float,
    max_lat_sec: float,
) -> bool:
    return bool(
        iops > 0
        or throughput_mib_s > 0
        or min_lat_sec > 0
        or avg_lat_sec > 0
        or max_lat_sec > 0
    )


def _try_append_elbencho_metric_from_csv_row(
    row: Dict[str, str],
    filename_params: Dict[str, Any],
    metrics: List[ElbenchoMetrics],
) -> None:
    """Parse one CSV row dict and append an ElbenchoMetrics if the row has benchmark data."""
    operation = row.get("operation", "").upper()
    if operation not in ("READ", "WRITE"):
        return

    direct_io = _elbencho_csv_parse_direct_io(row)
    random_io = _elbencho_csv_parse_random_io(row)
    command = _elbencho_csv_parse_command(row)
    io_duration_sec = parse_timelimit_seconds_from_command(command)
    phase_wall_duration_ms = _phase_wall_duration_ms_from_csv_row(row)
    phase_first_duration_ms = _phase_first_duration_ms_from_csv_row(row)

    iops = _elbencho_csv_parse_float_field(row, "IOPS [first]")
    throughput_mib_s = _elbencho_csv_parse_float_field(row, "MiB/s [first]")
    throughput_mb_s = normalize_throughput_to_mb(throughput_mib_s)
    throughput_gbps = mib_to_gbps(throughput_mib_s)

    min_lat_sec = _elbencho_csv_parse_io_lat_us_to_sec(row, CSV_COL_IO_LAT_US_MIN)
    avg_lat_sec = _elbencho_csv_parse_io_lat_us_to_sec(row, CSV_COL_IO_LAT_US_AVG)
    max_lat_sec = _elbencho_csv_parse_io_lat_us_to_sec(row, CSV_COL_IO_LAT_US_MAX)

    file_size_bytes = _elbencho_csv_parse_file_size_bytes(row)

    if not _elbencho_csv_row_has_numeric_metrics(
        iops,
        throughput_mib_s,
        min_lat_sec,
        avg_lat_sec,
        max_lat_sec,
    ):
        return

    io_size = filename_params["io_size"]
    nodes = filename_params["nodes"]
    threads = filename_params["threads"]
    io_depth = filename_params["io_depth"]
    datestamp = filename_params["datestamp"]

    metric = ElbenchoMetrics(
        io_size=io_size,
        nodes=nodes,
        threads=threads,
        io_depth=io_depth,
        operation=operation,
        datestamp=datestamp,
        is_multi_node=False,
        iops=iops,
        throughput_mib_s=throughput_mib_s,
        throughput_mb_s=throughput_mb_s,
        throughput_gbps=throughput_gbps,
        min_lat_sec=min_lat_sec,
        avg_lat_sec=avg_lat_sec,
        max_lat_sec=max_lat_sec,
        lat_pct_1=0,
        lat_pct_50=0,
        lat_pct_75=0,
        lat_pct_99=0,
        direct_io=direct_io,
        random_io=random_io,
        command=command,
        file_size_bytes=file_size_bytes,
        io_duration_sec=io_duration_sec,
        phase_wall_duration_ms=phase_wall_duration_ms,
        phase_first_duration_ms=phase_first_duration_ms,
        write_only_data_dir="",
        sweep_read_from_path="",
        treescan_size_stats_line="",
        treescan_file_count=0,
        treescan_avg_bytes=0,
        treescan_min_bytes=0,
        treescan_max_bytes=0,
        treescan_first_file_bytes=0,
        histogram={},
    )
    metrics.append(metric)


def parse_elbencho_csv_file(
    csv_file: str, filename_params: Dict[str, Any]
) -> List[ElbenchoMetrics]:
    """Parse an elbencho .csv file and extract all metrics except histogram
    and percentile data.

    Args:
        csv_file: Path to the .csv file
        filename_params: Parameters extracted from the filename

    Returns:
        List of ElbenchoMetrics objects with data from the CSV file
    """
    metrics: List[ElbenchoMetrics] = []

    try:
        with open(csv_file, "r", encoding="utf-8") as f:
            first_line = f.readline()
            if not first_line.strip():
                return []
            headerless = _elbencho_csv_first_line_is_headerless(first_line)
            f.seek(0)
            if headerless:
                reader = csv.reader(f, quoting=csv.QUOTE_MINIMAL)
                for row in reader:
                    as_dict = _elbencho_headerless_row_to_dict(row)
                    if as_dict is None:
                        continue
                    _try_append_elbencho_metric_from_csv_row(
                        as_dict, filename_params, metrics
                    )
            else:
                reader = csv.DictReader(f, quoting=csv.QUOTE_MINIMAL)
                for row in reader:
                    _try_append_elbencho_metric_from_csv_row(
                        row, filename_params, metrics
                    )

    except Exception as e:  # pylint: disable=broad-exception-caught
        eprint(f"Error parsing CSV file {csv_file}: {e}")
        traceback.print_exc()

    non_zero_sizes = {m.file_size_bytes for m in metrics if m.file_size_bytes > 0}
    if len(non_zero_sizes) > 1:
        raise ValueError("Multiple non-zero file sizes found in CSV file")
    write_file_size = non_zero_sizes.pop() if non_zero_sizes else 0

    for metric in metrics:
        if metric.operation == "READ" and metric.file_size_bytes == 0:
            metric.file_size_bytes = write_file_size

    return _elbencho_dedupe_latest_operation_metrics(metrics)


def parse_elbencho_files(base_filename: str) -> List[ElbenchoMetrics]:
    """Parse a .csv and .out pair of elbencho output files and extract metrics.

    Args:
        base_filename: Base path of the elbencho output files (without
        extension)

    Returns:
        List of ElbenchoMetrics objects
    """
    # Both files must exists
    out_file = f"{base_filename}.out"
    csv_file = f"{base_filename}.csv"

    if not os.path.exists(out_file) or not os.path.exists(csv_file):
        raise FileNotFoundError(
            f"One or both required files missing: {out_file} or {csv_file}"
        )

    # Get the directory and base name for extracting info from filename
    basename = os.path.basename(base_filename)

    # Extract parameters from the benchmark filename
    filename_params = parse_benchmark_filename(basename)
    if not filename_params:
        raise ValueError(
            "Cannot parse benchmark parameters from " f"filename: {basename}"
        )

    # Extract metrics primarily from the .csv file if it exists
    # This will include all metrics except histogram and percentile data
    metrics = []
    if os.path.exists(csv_file):
        try:
            metrics = parse_elbencho_csv_file(csv_file, filename_params)
            eprint(f"Extracted base metrics from {csv_file}")
        except Exception as e:  # pylint: disable=broad-exception-caught
            eprint(f"Error parsing {csv_file}: {e}")
            traceback.print_exc()

    # If we found no CSV metrics, we can't proceed
    if not metrics:
        eprint(
            f"Warning: No valid metrics found in {csv_file}. Check if file exists and has correct format."
        )
        return []

    # Extract histogram and percentile data and add them to our write/read metrics
    try:
        add_elbencho_out_file_metrics(out_file, metrics)
        eprint(f"Extracted histogram and percentile data from {out_file}")
    except Exception as e:  # pylint: disable=broad-exception-caught
        eprint(f"Error parsing {out_file}: {e}")
        traceback.print_exc()

    result_dir = os.path.dirname(base_filename) or "."
    apply_treescan_from_directory_scan(result_dir, metrics, base_filename)

    return metrics


def print_markdown_metrics_table_and_command(
    size_metrics: List[ElbenchoMetrics],
    io_size: str,
    operation: str,
) -> None:
    """Print a markdown table for a set of metrics and a representative command.

    Args:
        size_metrics: List of metrics for this IO size and operation
        io_size: IO size string (full string like "5M,4K")
        operation: Operation type (READ or WRITE)
        headers: Column headers for the table
    """
    # Sort metrics by nodes, then by (io_depth * threads, threads)
    size_metrics.sort(key=lambda m: (m.nodes, m.io_depth * m.threads, m.threads))

    # Determine IO mode for this specific size and operation
    io_mode = (
        "DirectIO (dio)"
        if any(m.direct_io == 1 for m in size_metrics)
        else "BufferedIO (bio)"
    )

    # Determine IO order for this specific size and operation
    io_order = "Rand" if any(m.random_io == 1 for m in size_metrics) else "Seq"

    # Get the operation-specific IO size
    write_size, read_size = parse_size_string(io_size)
    op_specific_size = write_size if operation == "WRITE" else read_size
    display_fs = metric_display_file_size_bytes(size_metrics[0])
    fs_label_txt = bytes_to_elbencho_size_string(display_fs) if display_fs > 0 else "0"
    fs_prefix = file_size_header_prefix_for_metrics(size_metrics)
    anad_suffix = _all_nodes_all_data_suffix(size_metrics)
    datestamp = size_metrics[0].datestamp
    print_sweep_auxiliary_lines_for_metrics(size_metrics)
    print_workload_metadata_for_metrics(size_metrics)
    # Print IO size header for this specific size
    print(
        f"\nIO Size: **{op_specific_size}** - "
        f"{fs_prefix}**{fs_label_txt}{anad_suffix}** - "
        f"Operation: **{operation}** - "
        f"IO Order: **{io_order}** - "
        f"IO Mode: **{io_mode}** - "
        f"Datestamp: **{datestamp}**\n"
    )

    # Define table headers with shortened names for better readability
    # For markdown, remove the 50% and 75% latency columns to make the table more compact
    md_base_headers = [
        "Nodes",
        "Thrds",
        "IODep",
        "IOPS",
        "AvgBW(GB/s)",
        "Gb/s",
        "MinLat (ms)",
        "AvgLat (ms)",
        "MaxLat (ms)",
        "99% (ms)",
    ]
    show_dur_md = any(_metric_should_show_csv_phase_dur(m) for m in size_metrics)
    table_headers = md_base_headers + (["Dur", "DurTot"] if show_dur_md else [])

    # Prepare data rows with raw values
    rows = []

    # Collect columns that need decimal alignment: GB/s + 4 latency columns for markdown
    gb_s_col = []
    latency_cols = [[] for _ in range(4)]

    for metric in size_metrics:
        # Convert and format GB/s value
        gb_s_value = mb_s_to_gb_s(metric.throughput_mb_s)
        gb_s_fmt = format_gb_s(gb_s_value)
        gb_s_col.append(gb_s_fmt)

        # Convert latency values to ms for display
        min_lat_ms = metric.min_lat_sec * 1000
        avg_lat_ms = metric.avg_lat_sec * 1000
        max_lat_ms = metric.max_lat_sec * 1000
        lat_pct_99_ms = metric.lat_pct_99 * 1000

        # Format all values
        min_lat_fmt = format_with_sig_figs(min_lat_ms)
        avg_lat_fmt = format_with_sig_figs(avg_lat_ms)
        max_lat_fmt = format_with_sig_figs(max_lat_ms)
        lat_pct_99_fmt = format_with_sig_figs(lat_pct_99_ms)

        # Store formatted latency values for alignment
        latency_cols[0].append(min_lat_fmt)
        latency_cols[1].append(avg_lat_fmt)
        latency_cols[2].append(max_lat_fmt)
        latency_cols[3].append(lat_pct_99_fmt)

        # Store other non-latency metrics (except GB/s which needs alignment)
        base_row = [
            str(metric.nodes),
            str(metric.threads),
            str(metric.io_depth),
            f"{int(metric.iops):,d}",
            None,  # Placeholder for GB/s (will be filled after alignment)
            f"{metric.throughput_gbps:.2f}",
        ]

        # Add placeholders for latency columns
        row = base_row + [None, None, None, None]  # 4 placeholder values for markdown
        if show_dur_md:
            row.append(None)
            row.append(None)
        rows.append(row)

    # Align the decimal points in GB/s column
    aligned_gb_s_col = align_decimal_points(gb_s_col, True)

    # Align the decimal points in latency columns
    aligned_latency_cols = []
    for lat_col in latency_cols:
        aligned_latency_cols.append(align_decimal_points(lat_col, True))

    # Calculate column widths
    col_widths = [len(h) for h in table_headers]

    # Update widths for non-aligned columns (excluding GB/s at index 4 and latency at indices 6+)
    for row in rows:
        for i, val in enumerate(row[:6]):
            if val is not None and i != 4:  # Skip GB/s placeholder at index 4
                col_widths[i] = max(col_widths[i], len(val))

    # Update width for aligned GB/s column (index 4)
    for val in aligned_gb_s_col:
        col_widths[4] = max(col_widths[4], len(val))

    # Update widths for aligned latency columns
    for i, aligned_col in enumerate(aligned_latency_cols):
        col_index = 6 + i  # Latency columns start at index 6
        if col_index < len(col_widths):  # Make sure we don't go out of bounds
            for val in aligned_col:
                col_widths[col_index] = max(col_widths[col_index], len(val))

    # Apply aligned GB/s values to rows (index 4)
    for row_idx, row in enumerate(rows):
        row[4] = aligned_gb_s_col[row_idx]

    # Apply aligned latency values to rows
    for row_idx, row in enumerate(rows):
        for col_idx, aligned_col in enumerate(aligned_latency_cols):
            row[6 + col_idx] = aligned_col[row_idx]  # Latency columns start at index 6

    if show_dur_md:
        dur_col = len(md_base_headers)
        dur_tot_col = dur_col + 1
        for row_idx, row in enumerate(rows):
            metric = size_metrics[row_idx]
            if _metric_should_show_csv_phase_dur(metric):
                dur_str = format_dur_xmys_ms(metric.phase_first_duration_ms)
                dur_tot_str = format_dur_xmys_ms(metric.phase_wall_duration_ms)
            else:
                dur_str = ""
                dur_tot_str = ""
            row[dur_col] = dur_str
            row[dur_tot_col] = dur_tot_str
            col_widths[dur_col] = max(col_widths[dur_col], len(dur_str))
            col_widths[dur_tot_col] = max(col_widths[dur_tot_col], len(dur_tot_str))

    # Ensure minimum widths
    min_widths = [5, 5, 5, 8, 12, 5, 10, 10, 10, 7]  # 10 columns for markdown
    if show_dur_md:
        min_widths.append(3)
        min_widths.append(6)
    for i, col_width in enumerate(col_widths):
        if i < len(min_widths):  # Make sure we don't go out of bounds
            col_widths[i] = max(col_width, min_widths[i])

    # Print markdown table header
    header_line = " | ".join(table_headers)
    print(f"| {header_line} |")

    # Print separator line with proper alignment
    separator = []
    for i, width in enumerate(col_widths):
        if i <= 2:  # Text columns (Nodes, Threads, IODep)
            separator.append(":" + "-" * (width - 1))
        else:  # Numeric columns (right-aligned)
            separator.append("-" * (width - 1) + ":")
    separator_line = " | ".join(separator)
    print(f"| {separator_line} |")

    # Print data rows
    ncols = len(table_headers)
    for row in rows:
        data_line = " | ".join(row[:ncols])
        print(f"| {data_line} |")

    # Add the Elbencho Command section
    print("\n**Representative Elbencho Command**")

    # Extract command from the first metric that has a non-empty command
    command = ""
    for metric in size_metrics:
        if metric.command and metric.command.strip():
            command = metric.command.strip()
            break

    # If we found a command, print it in a monospace font (using markdown code block)
    if command:
        print("\n```")
        print(command)
        print("```")
    else:
        print("\n*Command information not available*")


def main() -> None:
    """Script entry point."""
    parser = argparse.ArgumentParser(
        description="Analyze elbencho results",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "input_dirs",
        nargs="*",
        help="Directories containing elbencho output files (optional if --from-csv is provided)",
    )
    parser.add_argument(
        "--to-csv",
        action="store_true",
        help="Write metrics to a CSV file in the first input directory (filename will be auto-generated)",
    )
    parser.add_argument(
        "--from-csv",
        metavar="FILE",
        help="Read metrics from this CSV file instead of parsing benchmark files",
    )
    parser.add_argument(
        "--only-threads",
        metavar="THREADS",
        help="Only include benchmarks with these thread counts (comma-separated, can include ranges like 1-10)",
    )
    parser.add_argument(
        "--only-nodes",
        metavar="NODES",
        help="Only include benchmarks with these node counts (comma-separated, can include ranges like 1-10)",
    )
    parser.add_argument(
        "--only-sizes",
        action="append",
        metavar="SIZE",
        help=(
            "Only include benchmarks with these IO sizes (repeat flag for multiple). "
            "Commas are not split, so compound elbencho sizes like 1M,r64K are one value. "
            "Within one argument, separate multiple sizes with ';' (e.g. '1M,r64K;4K'). "
            "Former comma-separated lists must use ';' or multiple --only-sizes flags."
        ),
    )
    parser.add_argument(
        "--only-iodepths",
        metavar="IODEPTHS",
        help="Only include benchmarks with these IO depth values (comma-separated, can include ranges like 1-4)",
    )
    parser.add_argument(
        "--test-parse",
        metavar="FILE",
        help="Test the parse_elbencho_file function on a specific file",
    )
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Format output as Markdown for easy import into Google Docs (report to stdout, progress to stderr)",
    )
    parser.add_argument(
        "--no-dual-y-axis",
        action="store_true",
        help="Generate separate single-axis plots instead of dual y-axis plots for IOPS/Throughput vs Latency",
    )
    parser.add_argument(
        "--per-client-plots",
        action="store_true",
        help=(
            "Investigate client startup, slowdown, failover, and imbalance with "
            "live percentile, outlier, heatmap, and summary reports (adds a "
            "second CSV scan)"
        ),
    )
    parser.add_argument(
        "--client-outlier-threshold",
        type=float,
        default=2.0,
        help="Negative z-score magnitude used to identify underperforming clients",
    )
    parser.add_argument(
        "--client-min-underperform-segments",
        type=int,
        default=1,
        help=(
            "Minimum slow, missing, or skipped live intervals required to select "
            "a client"
        ),
    )
    parser.add_argument(
        "--client-max-timeseries-lines",
        type=int,
        default=10,
        help="Maximum individual client lines in each live time-series plot",
    )
    parser.add_argument(
        "--client-max-heatmap-rows",
        type=int,
        default=50,
        help="Maximum clients displayed in each live underperformance heatmap",
    )

    args = parser.parse_args()
    positive_live_options = (
        ("--client-outlier-threshold", args.client_outlier_threshold),
        (
            "--client-min-underperform-segments",
            args.client_min_underperform_segments,
        ),
        ("--client-max-timeseries-lines", args.client_max_timeseries_lines),
        ("--client-max-heatmap-rows", args.client_max_heatmap_rows),
    )
    for option_name, value in positive_live_options:
        if value <= 0:
            parser.error(f"{option_name} must be greater than zero")

    # Check if input_dir is required but not provided
    if not args.input_dirs and not args.from_csv and not args.test_parse:
        parser.error(
            "at least one input directory is required when not using --from-csv or --test-parse"
        )

    # Handle test-parse mode
    if args.test_parse:
        test_parse(args.test_parse)
        return

    # Read from CSV if input provided
    metrics = []

    csv_loaded_count = 0
    if args.from_csv:
        try:
            # Read the metrics
            metrics = read_csv(args.from_csv)
            eprint(f"Loaded {len(metrics)} metrics from {args.from_csv}")
            csv_loaded_count = len(metrics)
        except Exception as e:  # pylint: disable=broad-exception-caught
            eprint(f"Error reading CSV: {e}")
            sys.exit(1)

    # Parse elbencho files from input directories (if any)
    live_files = []
    for input_dir in args.input_dirs:
        discovered_live_files = discover_live_csv_files(input_dir)
        live_files.extend(discovered_live_files)
        eprint(f"Found {len(discovered_live_files)} live CSV files in {input_dir}")
        # Get a list of all elbencho output files in the directory
        dir_metrics = []
        base_filenames = set()
        for ext in ["csv", "out"]:
            pattern = os.path.join(input_dir, f"*.{ext}")
            for filepath in glob.glob(pattern):
                basename = os.path.basename(filepath)
                base_no_ext = re.sub(CSV_OUT_EXT_RE, "", basename)
                if not parse_benchmark_filename(base_no_ext):
                    continue
                base_filename = re.sub(CSV_OUT_EXT_RE, "", filepath)
                base_filenames.add(base_filename)

        eprint(
            f"Found {len(base_filenames)} unique benchmark file pairs in {input_dir}"
        )

        for base_filename in base_filenames:
            try:
                file_metrics = parse_elbencho_files(base_filename)
                dir_metrics.extend(file_metrics)
            except Exception as e:  # pylint: disable=broad-exception-caught
                eprint(f"Error parsing {base_filename}: {e}")

        env_used = load_env_used_yaml(input_dir)
        apply_env_used_to_metrics(env_used, dir_metrics)
        apply_execution_workloads(input_dir, dir_metrics)

        # Set multi-node flag if any node count is greater than 1
        if any(m.nodes > 1 for m in dir_metrics):
            for m in dir_metrics:
                m.is_multi_node = True

        metrics.extend(dir_metrics)

    # Print summary of metrics extracted from directories
    if args.input_dirs:
        eprint(
            f"Extracted {len(metrics)-csv_loaded_count} metrics from all files in the input directories"
        )

    had_aggregate_metrics = bool(metrics)

    # Verify that all metrics have a datestamp
    if metrics:
        missing_datestamps = [i for i, m in enumerate(metrics) if not m.datestamp]
        if missing_datestamps:
            eprint(f"ERROR: Missing datestamp in {len(missing_datestamps)} metrics")
            eprint(f"First missing datestamp at index: {missing_datestamps[0]}")
            sys.exit(1)
    elif not live_files:
        eprint("ERROR: No metrics found")
        sys.exit(1)

    # Filter metrics based on command-line options
    thread_filter = None
    node_filter = None
    size_filter = None
    iodepth_filter = None
    if args.only_threads or args.only_sizes or args.only_nodes or args.only_iodepths:
        thread_filter = (
            parse_int_values_with_ranges(args.only_threads)
            if args.only_threads
            else None
        )
        node_filter = (
            parse_int_values_with_ranges(args.only_nodes) if args.only_nodes else None
        )
        size_filter = parse_only_sizes_arg(args.only_sizes)
        iodepth_filter = (
            parse_int_values_with_ranges(args.only_iodepths)
            if args.only_iodepths
            else None
        )

        metrics = filter_metrics(
            metrics, size_filter, thread_filter, node_filter, iodepth_filter
        )
        eprint(f"After filtering, {len(metrics)} metrics remain")
        live_files = [
            metadata
            for metadata in live_files
            if _live_metadata_matches_filters(
                metadata,
                size_filter,
                thread_filter,
                node_filter,
                iodepth_filter,
            )
        ]
        eprint(f"After filtering, {len(live_files)} live CSV files remain")

    # Write to CSV if requested
    if args.to_csv:
        # Use the first input directory or current directory if none provided
        output_dir = args.input_dirs[0] if args.input_dirs else "."
        csv_filename = "elbencho-metrics.csv"
        csv_path = os.path.join(output_dir, csv_filename)
        try:
            write_csv(csv_path, metrics)
            eprint(f"Wrote {len(metrics)} metrics to {csv_path}")
        except Exception as e:  # pylint: disable=broad-exception-caught
            eprint(f"Error writing CSV: {e}")

    # Match plot output directory (used for report.txt and plots)
    output_dir = "."
    if args.input_dirs:
        output_dir = args.input_dirs[0]
    elif args.from_csv:
        output_dir = os.path.dirname(os.path.abspath(args.from_csv))

    # Preserve the pre-live behavior when filters remove all aggregate metrics.
    if had_aggregate_metrics:
        if args.markdown:
            print_markdown_table(metrics, args.no_dual_y_axis)
        else:
            mirror_stdout_to_file(
                os.path.join(output_dir, REPORT_TXT_FILENAME),
                print_terminal_table,
                metrics,
            )

    # Generate plots if we have metrics and an input directory for storing plots
    if metrics:
        # Create output directory if it doesn't exist
        os.makedirs(output_dir, exist_ok=True)

        # Generate plots
        plot_metrics(metrics, output_dir, args.no_dual_y_axis)
        eprint(f"Generated plots saved in {output_dir}")

    successful_live_reports = 0
    for metadata in live_files:
        try:
            if _report_one_live_file(
                metadata,
                output_dir,
                args.per_client_plots,
                args.client_outlier_threshold,
                args.client_min_underperform_segments,
                args.client_max_timeseries_lines,
                args.client_max_heatmap_rows,
            ):
                successful_live_reports += 1
        except Exception as exc:  # pylint: disable=broad-exception-caught
            eprint(f"Error reporting live CSV {metadata.path}: {exc}")
            traceback.print_exc()
    if live_files and not successful_live_reports and not had_aggregate_metrics:
        eprint("ERROR: No live CSV reports were generated")
        sys.exit(1)


if __name__ == "__main__":
    main()
