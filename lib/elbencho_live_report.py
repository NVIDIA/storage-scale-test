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

"""Bounded-memory analysis and reporting for elbencho live CSV files."""

from __future__ import annotations

import csv
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

MIB = 1024 * 1024
TOTAL_RANK = "Total"
MODE_SERVICE = "service"
MODE_WORKER = "worker"
INVALID_RATE = None
SKIPPED_METADATA_PHASES = frozenset({"CREATE", "DELETE", "MKDIR", "STAT"})
OCCURRENCE_RATE_JUMP_MAX_MS = 100
OCCURRENCE_RATE_JUMP_FACTOR = 4.0

LIVE_CSV_COLUMNS = [
    "ISO Date",
    "Label",
    "Phase",
    "RuntimeMS",
    "Rank",
    "MixType",
    "Done%",
    "DoneBytes",
    "MiB/s",
    "IOPS",
    "Entries",
    "Entries/s",
    "Lat Ent us",
    "Lat IO us",
    "Active",
    "CPU",
    "Service",
]
REQUIRED_COLUMNS = frozenset(
    {
        "ISO Date",
        "Phase",
        "RuntimeMS",
        "Rank",
        "MixType",
        "DoneBytes",
        "MiB/s",
        "IOPS",
        "Service",
    }
)


@dataclass(frozen=True, order=True)
class LiveDomainKey:
    """Independent live-counter domain within one CSV file."""

    phase: str
    mix_type: str
    occurrence: int = 0

    @property
    def label(self) -> str:
        """Human-readable phase/mix label."""
        base = f"{self.phase} ({self.mix_type})" if self.mix_type else self.phase
        if self.occurrence:
            return f"{base} occurrence {self.occurrence + 1}"
        return base

    @property
    def slug(self) -> str:
        """Filesystem-safe phase/mix label."""
        parts = [part for part in (self.phase, self.mix_type) if part]
        if self.occurrence:
            parts.append(f"occurrence-{self.occurrence + 1}")
        raw = "-".join(parts)
        return re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-") or "unknown"


@dataclass(frozen=True, order=True)
class LiveClientId:
    """Stable identity for a standalone worker or distributed service."""

    rank: str
    service: str

    @property
    def mode(self) -> str:
        """Whether this row represents a service or standalone worker."""
        return MODE_SERVICE if self.service else MODE_WORKER

    @property
    def label(self) -> str:
        """Display label preserving both service and rank identity."""
        if self.service:
            return f"{self.service} [rank {self.rank}]"
        return f"worker-{self.rank}"


@dataclass(frozen=True)
class LiveFileMetadata:
    """Benchmark parameters associated with one sibling live CSV."""

    path: str
    io_size: str
    nodes: int
    threads: int
    io_depth: int
    datestamp: str

    @property
    def stem(self) -> str:
        """Live CSV basename without the .live.csv suffix."""
        suffix = ".live.csv"
        name = Path(self.path).name
        return name[: -len(suffix)] if name.endswith(suffix) else Path(name).stem


class P2Median:
    """Constant-memory P² estimator for the median."""

    def __init__(self) -> None:
        self._initial: List[float] = []
        self._heights: List[float] = []
        self._positions: List[int] = []
        self._desired: List[float] = []
        self._increments = (0.0, 0.25, 0.5, 0.75, 1.0)

    def add(self, value: float) -> None:
        """Add one observation."""
        if len(self._initial) < 5:
            self._initial.append(value)
            if len(self._initial) == 5:
                self._initialize_markers()
            return
        marker = self._find_marker(value)
        for idx in range(marker + 1, 5):
            self._positions[idx] += 1
        for idx, increment in enumerate(self._increments):
            self._desired[idx] += increment
        self._adjust_markers()

    def value(self) -> float:
        """Return the exact startup median or current P² estimate."""
        if not self._initial:
            return 0.0
        if len(self._initial) < 5:
            return _percentile(self._initial, 50)
        return self._heights[2]

    def _initialize_markers(self) -> None:
        self._initial.sort()
        self._heights = list(self._initial)
        self._positions = [1, 2, 3, 4, 5]
        self._desired = [1.0, 2.0, 3.0, 4.0, 5.0]

    def _find_marker(self, value: float) -> int:
        if value < self._heights[0]:
            self._heights[0] = value
            return 0
        if value >= self._heights[4]:
            self._heights[4] = value
            return 3
        for idx in range(4):
            if self._heights[idx] <= value < self._heights[idx + 1]:
                return idx
        return 3

    def _adjust_markers(self) -> None:
        for idx in range(1, 4):
            delta = self._desired[idx] - self._positions[idx]
            direction = 1 if delta >= 1 else -1 if delta <= -1 else 0
            if not direction or not self._can_move(idx, direction):
                continue
            candidate = self._parabolic_height(idx, direction)
            if self._heights[idx - 1] < candidate < self._heights[idx + 1]:
                self._heights[idx] = candidate
            else:
                self._heights[idx] = self._linear_height(idx, direction)
            self._positions[idx] += direction

    def _can_move(self, idx: int, direction: int) -> bool:
        next_gap = self._positions[idx + 1] - self._positions[idx]
        prev_gap = self._positions[idx - 1] - self._positions[idx]
        return (direction > 0 and next_gap > 1) or (direction < 0 and prev_gap < -1)

    def _parabolic_height(self, idx: int, direction: int) -> float:
        positions = self._positions
        heights = self._heights
        left = (
            (positions[idx] - positions[idx - 1] + direction)
            * (heights[idx + 1] - heights[idx])
            / (positions[idx + 1] - positions[idx])
        )
        right = (
            (positions[idx + 1] - positions[idx] - direction)
            * (heights[idx] - heights[idx - 1])
            / (positions[idx] - positions[idx - 1])
        )
        return heights[idx] + direction * (left + right) / (
            positions[idx + 1] - positions[idx - 1]
        )

    def _linear_height(self, idx: int, direction: int) -> float:
        neighbor = idx + direction
        return self._heights[idx] + direction * (
            self._heights[neighbor] - self._heights[idx]
        ) / (self._positions[neighbor] - self._positions[idx])


@dataclass
class ClientSummary:
    """Online summary for one client."""

    client: LiveClientId
    sample_count: int = 0
    mean_mib_s: float = 0.0
    _mean_square_delta: float = 0.0
    median_estimator: P2Median = field(default_factory=P2Median)
    relative_median_sum: float = 0.0
    relative_median_count: int = 0
    underperform_count: int = 0
    longest_underperform_streak: int = 0
    current_underperform_streak: int = 0
    missing_count: int = 0
    invalid_count: int = 0
    worst_z_score: float = 0.0
    worst_interval: int = -1
    worst_elapsed_sec: float = 0.0
    worst_iso_timestamp: str = ""

    def add_rate(
        self,
        rate_mib_s: float,
        fleet_median: float,
        z_score: float,
        interval: int,
        elapsed_sec: float,
        iso_timestamp: str,
        underperforming: bool,
    ) -> None:
        """Update online rate and underperformance statistics."""
        self.sample_count += 1
        delta = rate_mib_s - self.mean_mib_s
        self.mean_mib_s += delta / self.sample_count
        self._mean_square_delta += delta * (rate_mib_s - self.mean_mib_s)
        self.median_estimator.add(rate_mib_s)
        if fleet_median > 0:
            self.relative_median_sum += rate_mib_s / fleet_median
            self.relative_median_count += 1
        if underperforming:
            self.underperform_count += 1
            self.current_underperform_streak += 1
            self.longest_underperform_streak = max(
                self.longest_underperform_streak,
                self.current_underperform_streak,
            )
            if self.worst_interval < 0 or z_score < self.worst_z_score:
                self.worst_z_score = z_score
                self.worst_interval = interval
                self.worst_elapsed_sec = elapsed_sec
                self.worst_iso_timestamp = iso_timestamp
        else:
            self.current_underperform_streak = 0

    def mark_missing(self) -> None:
        """Record a missing interval and break persistence."""
        self.missing_count += 1
        self.current_underperform_streak = 0

    def mark_invalid(self) -> None:
        """Record an invalid delta and break persistence."""
        self.invalid_count += 1
        self.current_underperform_streak = 0

    @property
    def median_mib_s(self) -> float:
        """Estimated median throughput."""
        return self.median_estimator.value()

    @property
    def relative_to_fleet_median(self) -> float:
        """Mean ratio to the corresponding interval fleet median."""
        if not self.relative_median_count:
            return 0.0
        return self.relative_median_sum / self.relative_median_count

    @property
    def underperform_percent(self) -> float:
        """Percentage of valid samples flagged as underperforming."""
        if not self.sample_count:
            return 0.0
        return 100.0 * self.underperform_count / self.sample_count

    @property
    def problem_count(self) -> int:
        """Intervals that were slow, missing, or unusable."""
        return self.underperform_count + self.missing_count + self.invalid_count


@dataclass(frozen=True)
class AggregatePoint:
    """Native Rank=Total sample."""

    interval: int
    elapsed_sec: float
    iso_timestamp: str
    throughput_mib_s: float
    iops: float


@dataclass(frozen=True)
class FleetPoint:
    """Cross-client percentile summary for one interval."""

    interval: int
    elapsed_sec: float
    iso_timestamp: str
    p10_mib_s: float
    p50_mib_s: float
    p90_mib_s: float
    client_count: int
    mean_mib_s: float
    stddev_mib_s: float


@dataclass
class DomainAnalysis:
    """First-pass result for one phase/mix domain."""

    key: LiveDomainKey
    aggregate_points: List[AggregatePoint] = field(default_factory=list)
    fleet_points: List[FleetPoint] = field(default_factory=list)
    clients: Dict[LiveClientId, ClientSummary] = field(default_factory=dict)
    interval_times: List[float] = field(default_factory=list)
    interval_iso_timestamps: List[str] = field(default_factory=list)
    runtime_to_interval: Dict[int, int] = field(default_factory=dict)
    diagnostics: Counter = field(default_factory=Counter)

    @property
    def has_extended_rows(self) -> bool:
        """Whether this domain contains client rows."""
        return bool(self.clients)


@dataclass
class LiveAnalysis:
    """Analysis result for one live CSV file."""

    metadata: LiveFileMetadata
    domains: Dict[LiveDomainKey, DomainAnalysis]


@dataclass
class SelectedClientDetail:
    """Second-pass bounded detail for selected clients."""

    selected: List[LiveClientId]
    rates_by_client: Dict[LiveClientId, List[Optional[float]]]
    heatmap_by_client: Dict[LiveClientId, List[Optional[float]]]


@dataclass
class _PreviousSample:
    done_bytes: int
    runtime_ms: int
    interval: int


@dataclass
class _IntervalBuffer:
    runtime_ms: int
    iso_timestamp: str
    client_rows: Dict[LiveClientId, int] = field(default_factory=dict)
    aggregate_mib_s: Optional[float] = None
    aggregate_iops: Optional[float] = None


@dataclass
class _DomainState:
    analysis: DomainAnalysis
    previous: Dict[LiveClientId, _PreviousSample] = field(default_factory=dict)
    buffer: Optional[_IntervalBuffer] = None


@dataclass
class _OccurrenceState:
    occurrence: int = 0
    runtime_ms: Optional[int] = None
    identities: Set[object] = field(default_factory=set)
    total_done_bytes: Optional[int] = None


@dataclass(frozen=True)
class _LiveCSVColumns:
    iso_date: int
    phase: int
    runtime_ms: int
    rank: int
    mix_type: int
    done_bytes: int
    mib_per_sec: int
    iops: int
    service: int


class _OccurrenceTracker:
    """Assign phase occurrence numbers while streaming native interval rows."""

    def __init__(self) -> None:
        self._states: Dict[Tuple[str, str], _OccurrenceState] = {}

    def domain_key(
        self,
        key: LiveDomainKey,
        runtime_ms: int,
        identity: object,
        done_bytes: Optional[int],
        aggregate_mib_s: Optional[float],
    ) -> LiveDomainKey:
        """Return the occurrence-qualified key for one parsed row."""
        base = (key.phase, key.mix_type)
        state = self._states.setdefault(base, _OccurrenceState())
        if self._starts_new_occurrence(
            state,
            runtime_ms,
            identity,
            done_bytes,
            aggregate_mib_s,
        ):
            state.occurrence += 1
            state.identities.clear()
            state.total_done_bytes = None
        elif state.runtime_ms != runtime_ms:
            state.identities.clear()
        state.runtime_ms = runtime_ms
        state.identities.add(identity)
        if identity == TOTAL_RANK and done_bytes is not None:
            state.total_done_bytes = done_bytes
        return LiveDomainKey(key.phase, key.mix_type, state.occurrence)

    @staticmethod
    def _starts_new_occurrence(
        state: _OccurrenceState,
        runtime_ms: int,
        identity: object,
        done_bytes: Optional[int],
        aggregate_mib_s: Optional[float],
    ) -> bool:
        if identity != TOTAL_RANK or state.runtime_ms is None:
            return False
        runtime_restarted = runtime_ms < state.runtime_ms or (
            runtime_ms == state.runtime_ms and identity in state.identities
        )
        counter_restarted = (
            done_bytes is not None
            and state.total_done_bytes is not None
            and done_bytes < state.total_done_bytes
        )
        rate_jump_restarted = _total_rate_jump_restarted(
            state,
            runtime_ms,
            done_bytes,
            aggregate_mib_s,
        )
        return runtime_restarted or counter_restarted or rate_jump_restarted


def _total_rate_jump_restarted(
    state: _OccurrenceState,
    runtime_ms: int,
    done_bytes: Optional[int],
    aggregate_mib_s: Optional[float],
) -> bool:
    """Whether nearby Total counters imply an impossible continuation rate."""
    if (
        state.runtime_ms is None
        or state.total_done_bytes is None
        or done_bytes is None
        or aggregate_mib_s is None
        or aggregate_mib_s <= 0
    ):
        return False
    elapsed_ms = runtime_ms - state.runtime_ms
    byte_delta = done_bytes - state.total_done_bytes
    if elapsed_ms <= 0 or byte_delta <= 0:
        return False
    if elapsed_ms > OCCURRENCE_RATE_JUMP_MAX_MS:
        return False
    implied_mib_s = byte_delta * 1000.0 / elapsed_ms / MIB
    restart_threshold = max(
        aggregate_mib_s * OCCURRENCE_RATE_JUMP_FACTOR,
        aggregate_mib_s + 1.0,
    )
    return implied_mib_s > restart_threshold


def analyze_live_csv(
    metadata: LiveFileMetadata,
    z_threshold: float = 2.0,
) -> LiveAnalysis:
    """Stream a live CSV once and return bounded first-pass analysis."""
    states: Dict[LiveDomainKey, _DomainState] = {}
    with open(metadata.path, "r", encoding="utf-8-sig", newline="") as handle:
        reader, columns = _live_csv_reader(handle, metadata.path)
        for line_number, parsed in enumerate(
            _iter_parsed_rows(reader, columns), start=2
        ):
            (
                key,
                runtime_ms,
                iso_timestamp,
                rank,
                client,
                done_bytes,
                mib_s,
                iops,
            ) = parsed
            state = states.setdefault(
                key,
                _DomainState(analysis=DomainAnalysis(key=key)),
            )
            _advance_buffer(state, runtime_ms, iso_timestamp, z_threshold)
            if rank == TOTAL_RANK:
                state.buffer.aggregate_mib_s = mib_s
                state.buffer.aggregate_iops = iops
                continue
            if client is None or done_bytes is None:
                state.analysis.diagnostics["malformed_client_rows"] += 1
                state.analysis.diagnostics[f"line_{line_number}_invalid"] += 1
                continue
            state.buffer.client_rows[client] = done_bytes
    for state in states.values():
        _flush_interval(state, z_threshold)
    return LiveAnalysis(
        metadata=metadata,
        domains={key: state.analysis for key, state in states.items()},
    )


def select_clients(
    domain: DomainAnalysis,
    min_underperform_segments: int,
    limit: int,
) -> List[LiveClientId]:
    """Select persistent/severe/slow clients deterministically."""
    candidates = [
        summary
        for summary in domain.clients.values()
        if summary.problem_count >= min_underperform_segments
    ]
    ordered = sorted(
        candidates,
        key=lambda summary: (
            -summary.problem_count,
            -summary.missing_count,
            -summary.invalid_count,
            summary.worst_z_score,
            summary.relative_to_fleet_median,
            summary.mean_mib_s,
            summary.client,
        ),
    )
    return [summary.client for summary in ordered[:limit]]


def select_heatmap_clients(
    domain: DomainAnalysis,
    prioritized: Sequence[LiveClientId],
    limit: int,
) -> List[LiveClientId]:
    """Select anomaly-first rows plus representative clients for fleet context."""
    available = set(domain.clients)
    selected = []
    selected_set = set()
    for client in prioritized:
        if client in available and client not in selected_set:
            selected.append(client)
            selected_set.add(client)
        if len(selected) == limit:
            return selected
    remaining = sorted(available - selected_set)
    slots = limit - len(selected)
    selected.extend(_representative_values(remaining, slots))
    return selected


def _representative_values(
    values: Sequence[LiveClientId],
    limit: int,
) -> List[LiveClientId]:
    """Return evenly spaced values, including both endpoints when possible."""
    if limit <= 0 or not values:
        return []
    if len(values) <= limit:
        return list(values)
    if limit == 1:
        return [values[len(values) // 2]]
    last = len(values) - 1
    indices = [round(index * last / (limit - 1)) for index in range(limit)]
    return [values[index] for index in indices]


def collect_selected_client_detail(
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    selected: Sequence[LiveClientId],
) -> SelectedClientDetail:
    """Re-scan one CSV and retain rates only for selected clients."""
    selected_set = set(selected)
    interval_count = len(domain.interval_times)
    rates = {client: [INVALID_RATE] * interval_count for client in selected}
    previous: Dict[LiveClientId, _PreviousSample] = {}
    with open(analysis.metadata.path, "r", encoding="utf-8-sig", newline="") as handle:
        reader, columns = _live_csv_reader(handle, analysis.metadata.path)
        for parsed in _iter_parsed_rows(reader, columns):
            key, runtime_ms, _, rank, client, done_bytes, _, _ = parsed
            if key != domain.key or rank == TOTAL_RANK or client not in selected_set:
                continue
            if done_bytes is None or runtime_ms not in domain.runtime_to_interval:
                continue
            interval = domain.runtime_to_interval[runtime_ms]
            prior = previous.get(client)
            rate = _derive_rate(prior, done_bytes, runtime_ms, interval)
            if rate is not None:
                rates[client][interval] = rate
            previous[client] = _PreviousSample(done_bytes, runtime_ms, interval)
    fleet_by_interval = {point.interval: point for point in domain.fleet_points}
    heatmap = {}
    for client in selected:
        heatmap[client] = [
            _heatmap_value(rate, fleet_by_interval.get(interval))
            for interval, rate in enumerate(rates[client])
        ]
    return SelectedClientDetail(list(selected), rates, heatmap)


def write_client_summaries(
    metadata: LiveFileMetadata,
    domain: DomainAnalysis,
    output_dir: str,
) -> Tuple[str, str]:
    """Write sorted CSV and text summaries; return both paths."""
    base = f"{metadata.stem}-{domain.key.slug}-client-summary"
    csv_path = os.path.join(output_dir, f"{base}.csv")
    text_path = os.path.join(output_dir, f"{base}.txt")
    summaries = sorted(
        domain.clients.values(),
        key=lambda item: (
            -item.problem_count,
            -item.missing_count,
            -item.invalid_count,
            item.worst_z_score,
            item.relative_to_fleet_median,
            item.client,
        ),
    )
    header = [
        "client",
        "mode",
        "rank",
        "service",
        "mean_mib_s",
        "median_mib_s",
        "avg_fleet_median_percent",
        "worst_sample_index",
        "worst_elapsed_sec",
        "worst_iso_timestamp",
        "worst_deviation_sigma",
        "underperform_intervals",
        "underperform_percent",
        "longest_underperform_streak",
        "valid_samples",
        "missing_samples",
        "skipped_delta_samples",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for summary in summaries:
            writer.writerow(_summary_row(summary))
    with open(text_path, "w", encoding="utf-8") as handle:
        handle.write(f"Elbencho live client summary: {domain.key.label}\n")
        handle.write(
            "host/worker\tmean MiB/s\tmedian MiB/s\tavg fleet median %\t"
            "underperform\tlongest streak\tworst timestamp\tmissing\tskipped deltas\n"
        )
        for summary in summaries:
            handle.write(
                f"{summary.client.label}\t{summary.mean_mib_s:.3f}\t"
                f"{summary.median_mib_s:.3f}\t"
                f"{summary.relative_to_fleet_median * 100:.1f}%\t"
                f"{summary.underperform_count}/{summary.sample_count} "
                f"({summary.underperform_percent:.2f}%)\t"
                f"{summary.longest_underperform_streak}\t"
                f"{summary.worst_iso_timestamp or '-'}\t"
                f"{summary.missing_count}\t{summary.invalid_count}\n"
            )
    return csv_path, text_path


def plot_aggregate_timeseries(
    metadata: LiveFileMetadata,
    domain: DomainAnalysis,
    output_dir: str,
) -> Optional[str]:
    """Plot native aggregate MiB/s and IOPS against elapsed time."""
    if not domain.aggregate_points:
        return None
    import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel

    points = domain.aggregate_points
    x_values, throughput = _series_with_gap_breaks(
        [point.elapsed_sec for point in points],
        [point.throughput_mib_s for point in points],
    )
    _, iops = _series_with_gap_breaks(
        [point.elapsed_sec for point in points],
        [point.iops for point in points],
    )
    figure, throughput_axis = plt.subplots(figsize=(14, 7))
    iops_axis = throughput_axis.twinx()
    throughput_axis.plot(x_values, throughput, color="steelblue", label="MiB/s")
    iops_axis.plot(x_values, iops, color="darkorange", alpha=0.75, label="IOPS")
    _set_timestamp_ticks(
        throughput_axis,
        [point.elapsed_sec for point in points],
        [point.iso_timestamp for point in points],
    )
    throughput_axis.set_xlabel("Sample time (ISO Date)")
    throughput_axis.set_ylabel("Throughput (MiB/s)", color="steelblue")
    iops_axis.set_ylabel("IOPS", color="darkorange")
    throughput_axis.set_title(
        f"Elbencho Aggregate Live Throughput - {domain.key.label}"
    )
    throughput_axis.grid(True, alpha=0.3)
    figure.tight_layout()
    path = os.path.join(
        output_dir,
        f"{metadata.stem}-{domain.key.slug}-aggregate-timeseries.png",
    )
    figure.savefig(path, bbox_inches="tight", dpi=150)
    plt.close(figure)
    return path


def plot_client_timeseries(
    metadata: LiveFileMetadata,
    domain: DomainAnalysis,
    detail: SelectedClientDetail,
    line_clients: Sequence[LiveClientId],
    output_dir: str,
) -> Optional[str]:
    """Plot fleet percentile band, median, and selected outlier lines."""
    if not domain.fleet_points:
        return None
    import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel
    from matplotlib import colormaps  # pylint: disable=import-outside-toplevel
    import numpy as np  # pylint: disable=import-outside-toplevel

    fleet_by_interval = {point.interval: point for point in domain.fleet_points}
    intervals = list(range(len(domain.interval_times)))
    x_values = domain.interval_times
    p10 = [
        (
            fleet_by_interval[interval].p10_mib_s
            if interval in fleet_by_interval
            else math.nan
        )
        for interval in intervals
    ]
    p50 = [
        (
            fleet_by_interval[interval].p50_mib_s
            if interval in fleet_by_interval
            else math.nan
        )
        for interval in intervals
    ]
    p90 = [
        (
            fleet_by_interval[interval].p90_mib_s
            if interval in fleet_by_interval
            else math.nan
        )
        for interval in intervals
    ]
    figure, axis = plt.subplots(figsize=(14, 7))
    axis.fill_between(x_values, p10, p90, alpha=0.3, color="steelblue", label="p10-p90")
    axis.plot(x_values, p50, color="steelblue", linewidth=2, label="fleet median")
    colors = colormaps["Set1"](np.linspace(0, 1, max(1, len(line_clients))))
    for color, client in zip(colors, line_clients):
        client_rates = [
            detail.rates_by_client[client][interval] for interval in intervals
        ]
        values = [math.nan if value is None else value for value in client_rates]
        axis.plot(
            x_values, values, linewidth=1.3, alpha=0.85, color=color, label=client.label
        )
    _set_timestamp_ticks(axis, x_values, domain.interval_iso_timestamps)
    axis.set_xlabel("Sample time (ISO Date)")
    axis.set_ylabel("Throughput (MiB/s)")
    axis.set_ylim(0, None)
    axis.set_title(f"Elbencho Client Throughput - {domain.key.label}")
    axis.grid(True, alpha=0.3)
    axis.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=8)
    figure.tight_layout()
    path = os.path.join(
        output_dir,
        f"{metadata.stem}-{domain.key.slug}-client-timeseries.png",
    )
    figure.savefig(path, bbox_inches="tight", dpi=150)
    plt.close(figure)
    return path


def plot_underperformance_heatmap(
    metadata: LiveFileMetadata,
    domain: DomainAnalysis,
    detail: SelectedClientDetail,
    heatmap_clients: Sequence[LiveClientId],
    output_dir: str,
) -> Optional[str]:
    """Plot selected-client throughput relative to each interval's fleet median."""
    if not heatmap_clients or not domain.interval_times:
        return None
    import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel
    import numpy as np  # pylint: disable=import-outside-toplevel
    from matplotlib import colormaps  # pylint: disable=import-outside-toplevel

    matrix = np.array(
        [
            [
                np.nan if value is None else value
                for value in detail.heatmap_by_client[client]
            ]
            for client in heatmap_clients
        ],
        dtype=float,
    )
    missing_color = "#0072B2"
    cmap = colormaps["YlOrBr_r"].copy()
    cmap.set_bad(color=missing_color)
    figure_height = max(5, 0.3 * len(heatmap_clients))
    figure, axis = plt.subplots(figsize=(16, figure_height))
    image = axis.imshow(
        matrix,
        aspect="auto",
        cmap=cmap,
        vmin=0,
        vmax=100,
        interpolation="nearest",
    )
    tick_spacing = max(1, len(domain.interval_times) // 10)
    ticks = list(range(0, len(domain.interval_times), tick_spacing))
    axis.set_xticks(ticks)
    axis.set_xticklabels(
        [_display_timestamp(domain.interval_iso_timestamps[idx]) for idx in ticks],
        rotation=30,
        ha="right",
    )
    axis.set_yticks(range(len(heatmap_clients)))
    axis.set_yticklabels([client.label for client in heatmap_clients], fontsize=8)
    axis.set_xlabel("Sample time (ISO Date)")
    axis.set_ylabel("Host / worker")
    axis.set_title(
        f"Elbencho Client Throughput vs Fleet Median - {domain.key.label}\n"
        "light=at/above fleet median, blue=missing/skipped, darker orange=slower"
    )
    colorbar = figure.colorbar(
        image,
        ax=axis,
        label="Throughput (% of interval fleet median)",
    )
    colorbar.set_ticks([0, 25, 50, 75, 100])
    colorbar.set_ticklabels(["0%", "25%", "50%", "75%", "≥100% (median or faster)"])
    figure.tight_layout()
    path = os.path.join(
        output_dir,
        f"{metadata.stem}-{domain.key.slug}-underperformance-heatmap.png",
    )
    figure.savefig(path, bbox_inches="tight", dpi=150)
    plt.close(figure)
    return path


def _live_csv_reader(handle, path: str):
    """Return an indexed CSV reader and validated column positions."""
    reader = csv.reader(handle)
    header = next(reader, None)
    return reader, _columns_from_header(header, path)


def _columns_from_header(
    header: Optional[List[str]],
    path: str,
) -> _LiveCSVColumns:
    positions = {name: index for index, name in enumerate(header or [])}
    missing = REQUIRED_COLUMNS - positions.keys()
    if missing:
        raise ValueError(
            f"Live CSV {path} is missing required columns: {', '.join(sorted(missing))}"
        )
    return _LiveCSVColumns(
        iso_date=positions["ISO Date"],
        phase=positions["Phase"],
        runtime_ms=positions["RuntimeMS"],
        rank=positions["Rank"],
        mix_type=positions["MixType"],
        done_bytes=positions["DoneBytes"],
        mib_per_sec=positions["MiB/s"],
        iops=positions["IOPS"],
        service=positions["Service"],
    )


def _parse_row(
    row: Sequence[str],
    columns: _LiveCSVColumns,
) -> Optional[
    Tuple[
        LiveDomainKey,
        int,
        str,
        str,
        Optional[LiveClientId],
        Optional[int],
        Optional[float],
        Optional[float],
    ]
]:
    try:
        phase = row[columns.phase].strip()
        runtime_value = row[columns.runtime_ms]
        rank = row[columns.rank].strip()
        iso_timestamp = row[columns.iso_date].strip()
        mix_type = row[columns.mix_type].strip()
        done_bytes_value = row[columns.done_bytes]
        mib_per_sec_value = row[columns.mib_per_sec]
        iops_value = row[columns.iops]
        service = row[columns.service].strip()
    except IndexError:
        return None
    if not phase:
        return None
    if phase.upper() in SKIPPED_METADATA_PHASES:
        return None
    try:
        runtime_ms = int(runtime_value)
    except ValueError:
        return None
    key = LiveDomainKey(phase=phase, mix_type=mix_type)
    if rank == TOTAL_RANK:
        return (
            key,
            runtime_ms,
            iso_timestamp,
            rank,
            None,
            _optional_int(done_bytes_value),
            _optional_float(mib_per_sec_value),
            _optional_float(iops_value),
        )
    if not rank:
        return key, runtime_ms, iso_timestamp, rank, None, None, None, None
    client = LiveClientId(rank=rank, service=service)
    return (
        key,
        runtime_ms,
        iso_timestamp,
        rank,
        client,
        _optional_int(done_bytes_value),
        None,
        None,
    )


def _iter_parsed_rows(
    reader: Iterable[Sequence[str]],
    columns: _LiveCSVColumns,
):
    """Yield parsed rows while separating repeated phase/runtime occurrences."""
    tracker = _OccurrenceTracker()
    for row in reader:
        parsed = _parse_row(row, columns)
        if parsed is None:
            continue
        key, runtime_ms, iso_timestamp, rank, client, done_bytes, mib_s, iops = parsed
        identity = TOTAL_RANK if rank == TOTAL_RANK else client
        yield (
            tracker.domain_key(key, runtime_ms, identity, done_bytes, mib_s),
            runtime_ms,
            iso_timestamp,
            rank,
            client,
            done_bytes,
            mib_s,
            iops,
        )


def _optional_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _advance_buffer(
    state: _DomainState,
    runtime_ms: int,
    iso_timestamp: str,
    z_threshold: float,
) -> None:
    if state.buffer is None:
        state.buffer = _IntervalBuffer(runtime_ms, iso_timestamp)
        return
    if state.buffer.runtime_ms != runtime_ms:
        _flush_interval(state, z_threshold)
        state.buffer = _IntervalBuffer(runtime_ms, iso_timestamp)


def _flush_interval(state: _DomainState, z_threshold: float) -> None:
    buffer = state.buffer
    if buffer is None:
        return
    domain = state.analysis
    interval = len(domain.interval_times)
    elapsed_sec = buffer.runtime_ms / 1000.0
    domain.interval_times.append(elapsed_sec)
    domain.interval_iso_timestamps.append(buffer.iso_timestamp)
    domain.runtime_to_interval[buffer.runtime_ms] = interval
    _record_aggregate(domain, buffer, interval, elapsed_sec)
    _mark_missing_clients(domain, buffer.client_rows)
    rates = _derive_interval_rates(state, buffer, interval)
    _record_fleet_statistics(
        domain,
        rates,
        interval,
        elapsed_sec,
        buffer.iso_timestamp,
        z_threshold,
    )
    state.buffer = None


def _record_aggregate(
    domain: DomainAnalysis,
    buffer: _IntervalBuffer,
    interval: int,
    elapsed_sec: float,
) -> None:
    if buffer.aggregate_mib_s is None and buffer.aggregate_iops is None:
        return
    domain.aggregate_points.append(
        AggregatePoint(
            interval=interval,
            elapsed_sec=elapsed_sec,
            iso_timestamp=buffer.iso_timestamp,
            throughput_mib_s=buffer.aggregate_mib_s or 0.0,
            iops=buffer.aggregate_iops or 0.0,
        )
    )


def _mark_missing_clients(
    domain: DomainAnalysis,
    current_rows: Dict[LiveClientId, int],
) -> None:
    for client in domain.clients.keys() - current_rows.keys():
        domain.clients[client].mark_missing()
        domain.diagnostics["missing_samples"] += 1


def _derive_interval_rates(
    state: _DomainState,
    buffer: _IntervalBuffer,
    interval: int,
) -> Dict[LiveClientId, float]:
    rates: Dict[LiveClientId, float] = {}
    for client, done_bytes in buffer.client_rows.items():
        if client not in state.analysis.clients:
            summary = ClientSummary(client=client, missing_count=interval)
            state.analysis.clients[client] = summary
            state.analysis.diagnostics["missing_samples"] += interval
        else:
            summary = state.analysis.clients[client]
        prior = state.previous.get(client)
        rate = _derive_rate(prior, done_bytes, buffer.runtime_ms, interval)
        if prior is not None and prior.interval != interval - 1:
            state.analysis.diagnostics["non_adjacent_samples"] += 1
        elif prior is not None and rate is None:
            summary.mark_invalid()
            reason = _invalid_rate_reason(
                prior, done_bytes, buffer.runtime_ms, interval
            )
            state.analysis.diagnostics[reason] += 1
        if rate is not None:
            rates[client] = rate
        state.previous[client] = _PreviousSample(
            done_bytes, buffer.runtime_ms, interval
        )
    return rates


def _derive_rate(
    prior: Optional[_PreviousSample],
    done_bytes: int,
    runtime_ms: int,
    interval: int,
) -> Optional[float]:
    if prior is None or prior.interval != interval - 1:
        return None
    elapsed_ms = runtime_ms - prior.runtime_ms
    byte_delta = done_bytes - prior.done_bytes
    if elapsed_ms <= 0 or byte_delta < 0:
        return None
    return byte_delta * 1000.0 / elapsed_ms / MIB


def _invalid_rate_reason(
    prior: _PreviousSample,
    done_bytes: int,
    runtime_ms: int,
    interval: int,
) -> str:
    if prior.interval != interval - 1:
        return "non_adjacent_samples"
    if runtime_ms <= prior.runtime_ms:
        return "non_positive_elapsed_time"
    if done_bytes < prior.done_bytes:
        return "counter_resets"
    return "invalid_samples"


def _record_fleet_statistics(
    domain: DomainAnalysis,
    rates: Dict[LiveClientId, float],
    interval: int,
    elapsed_sec: float,
    iso_timestamp: str,
    z_threshold: float,
) -> None:
    if not rates:
        return
    values = list(rates.values())
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    stddev = math.sqrt(variance)
    ordered_values = sorted(values)
    p10, median, p90 = (
        _percentile_from_sorted(ordered_values, percentile)
        for percentile in (10, 50, 90)
    )
    domain.fleet_points.append(
        FleetPoint(
            interval=interval,
            elapsed_sec=elapsed_sec,
            iso_timestamp=iso_timestamp,
            p10_mib_s=p10,
            p50_mib_s=median,
            p90_mib_s=p90,
            client_count=len(values),
            mean_mib_s=mean,
            stddev_mib_s=stddev,
        )
    )
    for client, rate in rates.items():
        z_score = (rate - mean) / stddev if stddev > 0 else 0.0
        underperforming = z_score < -z_threshold
        domain.clients[client].add_rate(
            rate,
            median,
            z_score,
            interval,
            elapsed_sec,
            iso_timestamp,
            underperforming,
        )


def _heatmap_value(
    rate_mib_s: Optional[float],
    fleet: Optional[FleetPoint],
) -> Optional[float]:
    if rate_mib_s is None or fleet is None:
        return None
    if fleet.p50_mib_s <= 0:
        return 0.0 if rate_mib_s <= 0 else 100.0
    return 100.0 * rate_mib_s / fleet.p50_mib_s


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(values)
    return _percentile_from_sorted(ordered, percentile)


def _percentile_from_sorted(ordered: Sequence[float], percentile: float) -> float:
    """Calculate one linearly interpolated percentile from sorted values."""
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile / 100.0
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _summary_row(summary: ClientSummary) -> List[object]:
    return [
        summary.client.label,
        summary.client.mode,
        summary.client.rank,
        summary.client.service,
        f"{summary.mean_mib_s:.6f}",
        f"{summary.median_mib_s:.6f}",
        f"{summary.relative_to_fleet_median * 100:.3f}",
        summary.worst_interval,
        f"{summary.worst_elapsed_sec:.3f}",
        summary.worst_iso_timestamp,
        f"{summary.worst_z_score:.6f}",
        summary.underperform_count,
        f"{summary.underperform_percent:.3f}",
        summary.longest_underperform_streak,
        summary.sample_count,
        summary.missing_count,
        summary.invalid_count,
    ]


def _display_timestamp(iso_timestamp: str) -> str:
    """Wrap an ISO timestamp for readable plot ticks without changing its value."""
    return iso_timestamp.replace("T", "\n", 1) if iso_timestamp else "unknown"


def _set_timestamp_ticks(
    axis,
    x_values: Sequence[float],
    iso_timestamps: Sequence[str],
    max_ticks: int = 10,
) -> None:
    """Label a numeric elapsed-time axis with corresponding ISO timestamps."""
    count = min(len(x_values), len(iso_timestamps))
    if not count:
        return
    spacing = max(1, math.ceil(count / max_ticks))
    indices = list(range(0, count, spacing))
    if indices[-1] != count - 1:
        indices.append(count - 1)
    axis.set_xticks([x_values[index] for index in indices])
    axis.set_xticklabels(
        [_display_timestamp(iso_timestamps[index]) for index in indices],
        rotation=30,
        ha="right",
    )


def _series_with_gap_breaks(
    x_values: Sequence[float],
    y_values: Sequence[float],
) -> Tuple[List[float], List[float]]:
    if len(x_values) < 3:
        return list(x_values), list(y_values)
    deltas = [
        current - previous
        for previous, current in zip(x_values, x_values[1:])
        if current > previous
    ]
    expected = _percentile(deltas, 50)
    if expected <= 0:
        return list(x_values), list(y_values)
    out_x: List[float] = [x_values[0]]
    out_y: List[float] = [y_values[0]]
    for previous, current, value in zip(x_values, x_values[1:], y_values[1:]):
        if current - previous > expected * 1.5:
            out_x.append((previous + current) / 2.0)
            out_y.append(math.nan)
        out_x.append(current)
        out_y.append(value)
    return out_x, out_y
