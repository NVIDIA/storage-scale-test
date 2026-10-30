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
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Set,
    Tuple,
)

if TYPE_CHECKING:  # pragma: no cover
    from lib.elbencho_live_steady import ClientSteady, SteadyAnalysis

MIB = 1024 * 1024
TOTAL_RANK = "Total"
MODE_SERVICE = "service"
MODE_WORKER = "worker"
INVALID_RATE = None
# elbencho's directory and entry phases (PHASENAME_* in its Common.h).
SKIPPED_METADATA_PHASES = frozenset({"MKDIRS", "RMDIRS", "RMFILES", "STAT", "STATDIRS"})
OCCURRENCE_RATE_JUMP_MAX_MS = 100
OCCURRENCE_RATE_JUMP_FACTOR = 4.0
IO_LATENCY_COLUMN = "Lat IO us"
ENTRIES_LATENCY_COLUMN = "Lat Ent us"
MALFORMED_CLIENT_ROWS = "malformed_client_rows"
# Malformed client rows named by line number (the rest are only counted).
MALFORMED_LINES_REPORTED = 5

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
    ENTRIES_LATENCY_COLUMN,
    IO_LATENCY_COLUMN,
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
    # Keeps outputs apart when several inputs have identically named runs.
    name_prefix: str = ""

    @property
    def stem(self) -> str:
        """Live CSV basename without the .live.csv suffix, after any prefix."""
        suffix = ".live.csv"
        name = Path(self.path).name
        base = name[: -len(suffix)] if name.endswith(suffix) else Path(name).stem
        return self.name_prefix + base


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
class ClientSummary:  # pylint: disable=too-many-instance-attributes
    """Online summary for one client."""

    client: LiveClientId
    sample_count: int = 0
    mean_mib_s: float = 0.0
    _mean_square_delta: float = 0.0
    median_estimator: P2Median = field(default_factory=P2Median)
    relative_median_sum: float = 0.0
    relative_median_count: int = 0
    missing_count: int = 0
    invalid_count: int = 0
    final_done_bytes: Optional[int] = None
    finish_elapsed_ms: Optional[int] = None
    first_progress_interval: Optional[int] = None

    def observe_done_bytes(
        self, done_bytes: int, runtime_ms: int, interval: Optional[int] = None
    ) -> None:
        """Track the last cumulative DoneBytes, when it was first reached, and
        the first interval with any progress."""
        if self.final_done_bytes != done_bytes:
            self.final_done_bytes = done_bytes
            self.finish_elapsed_ms = runtime_ms
        if self.first_progress_interval is None and done_bytes > 0:
            self.first_progress_interval = interval

    def add_rate(self, rate_mib_s: float, fleet_median: float) -> None:
        """Update the online rate statistics."""
        self.sample_count += 1
        delta = rate_mib_s - self.mean_mib_s
        self.mean_mib_s += delta / self.sample_count
        self._mean_square_delta += delta * (rate_mib_s - self.mean_mib_s)
        self.median_estimator.add(rate_mib_s)
        if fleet_median > 0:
            self.relative_median_sum += rate_mib_s / fleet_median
            self.relative_median_count += 1

    def mark_missing(self) -> None:
        """Record a missing interval."""
        self.missing_count += 1

    def mark_invalid(self) -> None:
        """Record an unusable delta."""
        self.invalid_count += 1

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


@dataclass(frozen=True)
class AggregatePoint:  # pylint: disable=too-many-instance-attributes
    """Native Rank=Total sample; a missing or invalid value is None."""

    interval: int
    elapsed_sec: float
    iso_timestamp: str
    throughput_mib_s: Optional[float]
    iops: Optional[float]
    io_latency_us: Optional[float] = None
    entries_latency_us: Optional[float] = None


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

    @property
    def all_started_interval(self) -> Optional[int]:
        """First interval by which every client that ever progressed had
        DoneBytes > 0; None when no client progressed. A client that never
        moves data must not hide the others: it classifies as slow instead."""
        started = [
            summary.first_progress_interval
            for summary in self.clients.values()
            if summary.first_progress_interval is not None
        ]
        return max(started) if started else None


@dataclass
class LiveAnalysis:
    """Analysis result for one live CSV file.

    ``rows`` replays the parsed rows for every later pass; None re-reads
    ``metadata.path``.
    """

    metadata: LiveFileMetadata
    domains: Dict[LiveDomainKey, DomainAnalysis]
    rows: Optional[Callable[[], Iterator["ParsedRow"]]] = None


@dataclass
class SelectedClientDetail:
    """Second-pass bounded detail for selected clients."""

    selected: List[LiveClientId]
    rates_by_client: Dict[LiveClientId, List[Optional[float]]]


@dataclass
class PreviousSample:
    """The prior live row of one client, for deriving an interval rate."""

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
    io_latency_us: Optional[float] = None
    entries_latency_us: Optional[float] = None


@dataclass
class _DomainState:
    analysis: DomainAnalysis
    previous: Dict[LiveClientId, PreviousSample] = field(default_factory=dict)
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
    io_latency: Optional[int] = None
    entries_latency: Optional[int] = None


class ParsedRow(NamedTuple):
    """One parsed live CSV row; only Total rows carry rates and latency."""

    key: LiveDomainKey
    runtime_ms: int
    iso_timestamp: str
    rank: str
    client: Optional[LiveClientId]
    done_bytes: Optional[int]
    mib_s: Optional[float]
    iops: Optional[float]
    io_latency_us: Optional[float]
    entries_latency_us: Optional[float]


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


def iter_live_rows(analysis: LiveAnalysis) -> Iterator[ParsedRow]:
    """Stream the analysis's parsed rows from its source, once per call."""
    if analysis.rows is not None:
        yield from analysis.rows()
        return
    path = analysis.metadata.path
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader, columns = live_csv_reader(handle, path)
        yield from iter_parsed_rows(reader, columns)


def analyze_live_csv(
    metadata: LiveFileMetadata,
    rows: Optional[Callable[[], Iterator[ParsedRow]]] = None,
) -> LiveAnalysis:
    """Stream a live CSV (or ``rows``) once and return bounded first-pass analysis."""
    analysis = LiveAnalysis(metadata=metadata, domains={}, rows=rows)
    states: Dict[LiveDomainKey, _DomainState] = {}
    for line_number, parsed in enumerate(iter_live_rows(analysis), start=2):
        key = parsed.key
        state = states.get(key)
        if state is None:
            state = states[key] = _DomainState(analysis=DomainAnalysis(key=key))
        _advance_buffer(state, parsed.runtime_ms, parsed.iso_timestamp)
        if parsed.rank == TOTAL_RANK:
            buffer = state.buffer
            buffer.aggregate_mib_s = parsed.mib_s
            buffer.aggregate_iops = parsed.iops
            buffer.io_latency_us = parsed.io_latency_us
            buffer.entries_latency_us = parsed.entries_latency_us
            continue
        if parsed.client is None or parsed.done_bytes is None:
            _record_malformed(state.analysis.diagnostics, line_number)
            continue
        state.buffer.client_rows[parsed.client] = parsed.done_bytes
    for state in states.values():
        _flush_interval(state)
    analysis.domains = {key: state.analysis for key, state in states.items()}
    return analysis


def _record_malformed(diagnostics: Counter, line_number: int) -> None:
    """Count a malformed client row; name only the first few lines."""
    diagnostics[MALFORMED_CLIENT_ROWS] += 1
    if diagnostics[MALFORMED_CLIENT_ROWS] <= MALFORMED_LINES_REPORTED:
        diagnostics[f"line_{line_number}_invalid"] += 1


def client_sort_key(client: LiveClientId) -> Tuple[int, int, str, str]:
    """Numeric ranks by value first, then other ranks, then service."""
    try:
        return (0, int(client.rank), "", client.service)
    except ValueError:
        return (1, 0, client.rank, client.service)


def collect_selected_client_detail(
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    selected: Sequence[LiveClientId],
) -> SelectedClientDetail:
    """Re-scan one CSV and retain one-interval rates only for selected clients."""
    selected_set = set(selected)
    interval_count = len(domain.interval_times)
    rates = {client: [INVALID_RATE] * interval_count for client in selected}
    previous: Dict[LiveClientId, PreviousSample] = {}
    for parsed in iter_live_rows(analysis):
        client = parsed.client
        if client not in selected_set or parsed.key != domain.key:
            continue
        done_bytes = parsed.done_bytes
        interval = domain.runtime_to_interval.get(parsed.runtime_ms)
        if done_bytes is None or interval is None:
            continue
        prior = previous.get(client)
        rate = derive_rate(prior, done_bytes, parsed.runtime_ms, interval)
        if rate is not None:
            rates[client][interval] = rate
        previous[client] = PreviousSample(done_bytes, parsed.runtime_ms, interval)
    return SelectedClientDetail(list(selected), rates)


SUMMARY_COLUMNS = [
    "client",
    "mode",
    "rank",
    "service",
    "class",
    "steady_mean_mib_s",
    "steady_ratio",
    "worst_5s_ratio",
    "worst_5s_elapsed_sec",
    "worst_30s_ratio",
    "worst_30s_elapsed_sec",
    "short_5s_percent",
    "mean_mib_s",
    "median_mib_s",
    "valid_samples",
    "missing_samples",
    "invalid_samples",
]
SUMMARY_TEXT_HEADER = (
    "host/worker",
    "class",
    "steady MiB/s",
    "steady/median",
    "worst 5 s",
    "worst 30 s",
    "5 s windows short",
    "mean MiB/s",
    "missing",
    "invalid",
)


def write_client_summaries(
    metadata: LiveFileMetadata,
    domain: DomainAnalysis,
    steady: Optional["SteadyAnalysis"],
    output_dir: str,
) -> Tuple[str, str]:
    """Write sorted CSV and text client summaries; return both paths."""
    base = f"{metadata.stem}-{domain.key.slug}-client-summary"
    csv_path = os.path.join(output_dir, f"{base}.csv")
    text_path = os.path.join(output_dir, f"{base}.txt")
    rows = _summary_rows(domain, steady)
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(SUMMARY_COLUMNS)
        for summary, client_steady in rows:
            writer.writerow(_summary_csv_row(summary, client_steady))
    with open(text_path, "w", encoding="utf-8") as handle:
        handle.write(f"Elbencho live client summary: {domain.key.label}\n")
        handle.write(_steady_heading(steady) + "\n")
        handle.write("\t".join(SUMMARY_TEXT_HEADER) + "\n")
        for summary, client_steady in rows:
            handle.write("\t".join(_summary_text_row(summary, client_steady)) + "\n")
    return csv_path, text_path


def _summary_rows(
    domain: DomainAnalysis, steady: Optional["SteadyAnalysis"]
) -> List[Tuple[ClientSummary, Optional["ClientSteady"]]]:
    """Clients with their steady statistics, sorted by class then ratio."""
    from lib.elbencho_live_steady import (  # pylint: disable=import-outside-toplevel
        CLASS_ORDER,
    )

    by_client = steady.clients if steady is not None else {}
    rows = [
        (summary, by_client.get(client)) for client, summary in domain.clients.items()
    ]

    def sort_key(item):
        summary, client_steady = item
        classification = client_steady.classification if client_steady else None
        ratio = client_steady.steady_ratio if client_steady else None
        return (
            (
                CLASS_ORDER.index(classification)
                if classification in CLASS_ORDER
                else len(CLASS_ORDER)
            ),
            ratio is None,
            ratio if ratio is not None else 0.0,
            client_sort_key(summary.client),
        )

    return sorted(rows, key=sort_key)


def _fmt(value: Optional[float], digits: int, scale: float = 1.0) -> str:
    """Format a number to fixed digits; None is an empty string."""
    return "" if value is None else f"{value * scale:.{digits}f}"


def _summary_csv_row(
    summary: ClientSummary, client_steady: Optional["ClientSteady"]
) -> List[object]:
    """One client-summary CSV row; missing values are empty cells."""
    has_samples = summary.sample_count > 0
    row: List[object] = [
        summary.client.label,
        summary.client.mode,
        summary.client.rank,
        summary.client.service,
    ]
    if client_steady is None:
        row.extend([""] * 8)
    else:
        row.extend(
            [
                client_steady.classification or "",
                _fmt(client_steady.steady_mean_mib_s, 3),
                _fmt(client_steady.steady_ratio, 4),
                _fmt(client_steady.worst_5s_ratio, 4),
                _fmt(client_steady.worst_5s_ms, 3, 0.001),
                _fmt(client_steady.worst_30s_ratio, 4),
                _fmt(client_steady.worst_30s_ms, 3, 0.001),
                _fmt(client_steady.short_5s_share, 1, 100.0),
            ]
        )
    row.extend(
        [
            _fmt(summary.mean_mib_s if has_samples else None, 3),
            _fmt(summary.median_mib_s if has_samples else None, 3),
            summary.sample_count,
            summary.missing_count,
            summary.invalid_count,
        ]
    )
    return row


def _summary_text_row(
    summary: ClientSummary, client_steady: Optional["ClientSteady"]
) -> List[str]:
    """One tab-separated text row; missing values are '-'."""

    def text(value: Optional[float], digits: int) -> str:
        return _fmt(value, digits) or "-"

    cells = [summary.client.label]
    if client_steady is None:
        cells.extend(["-"] * 6)
    else:
        short = (
            f"{client_steady.short_5s}/{client_steady.windows_5s}"
            if client_steady.windows_5s
            else "-"
        )
        cells.extend(
            [
                client_steady.classification or "-",
                text(client_steady.steady_mean_mib_s, 3),
                text(client_steady.steady_ratio, 3),
                text(client_steady.worst_5s_ratio, 3),
                text(client_steady.worst_30s_ratio, 3),
                short,
            ]
        )
    cells.extend(
        [
            text(summary.mean_mib_s if summary.sample_count else None, 3),
            str(summary.missing_count),
            str(summary.invalid_count),
        ]
    )
    return cells


def _steady_heading(steady: Optional["SteadyAnalysis"]) -> str:
    """The text summary's steady-window line."""
    if steady is None:
        return "Steady window unavailable (not analyzed)"
    window = steady.window
    if not window.available:
        return f"Steady window unavailable ({window.reason})"
    return (
        f"Steady window: {window.start_ms / 1000:.1f}-{window.end_ms / 1000:.1f} s; "
        f"shortfall {steady.shortfall_percent:g}%; "
        f"min slow windows {steady.min_slow_windows}"
    )


def live_csv_reader(handle, path: str):
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
        io_latency=positions.get(IO_LATENCY_COLUMN),
        entries_latency=positions.get(ENTRIES_LATENCY_COLUMN),
    )


def _optional_cell(row: Sequence[str], index: Optional[int]) -> Optional[float]:
    """Parse an optional column's cell; absent columns and cells are None."""
    if index is None or index >= len(row):
        return None
    return _optional_float(row[index])


def _parse_row(row: Sequence[str], columns: _LiveCSVColumns) -> Optional[tuple]:
    """Parse one row into ParsedRow fields (before occurrence numbering)."""
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
    if not phase or phase.upper() in SKIPPED_METADATA_PHASES:
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
            _optional_cell(row, columns.io_latency),
            _optional_cell(row, columns.entries_latency),
        )
    if not rank:
        return key, runtime_ms, iso_timestamp, rank, None, None, None, None, None, None
    return (
        key,
        runtime_ms,
        iso_timestamp,
        rank,
        LiveClientId(rank=rank, service=service),
        _optional_int(done_bytes_value),
        None,
        None,
        _optional_cell(row, columns.io_latency),
        _optional_cell(row, columns.entries_latency),
    )


def iter_parsed_rows(
    reader: Iterable[Sequence[str]],
    columns: _LiveCSVColumns,
) -> Iterator[ParsedRow]:
    """Yield parsed rows while separating repeated phase/runtime occurrences."""
    tracker = _OccurrenceTracker()
    for row in reader:
        parsed = _parse_row(row, columns)
        if parsed is None:
            continue
        key, runtime_ms, _, rank, client, done_bytes, mib_s = parsed[:7]
        identity = TOTAL_RANK if rank == TOTAL_RANK else client
        yield ParsedRow(
            tracker.domain_key(key, runtime_ms, identity, done_bytes, mib_s),
            *parsed[1:],
        )


def _optional_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Optional[str]) -> Optional[float]:
    """Parse a cell; empty, malformed, NaN and infinite values are missing."""
    try:
        parsed = float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and math.isfinite(parsed) else None


def _advance_buffer(
    state: _DomainState,
    runtime_ms: int,
    iso_timestamp: str,
) -> None:
    if state.buffer is None:
        state.buffer = _IntervalBuffer(runtime_ms, iso_timestamp)
        return
    if state.buffer.runtime_ms != runtime_ms:
        _flush_interval(state)
        state.buffer = _IntervalBuffer(runtime_ms, iso_timestamp)


def _flush_interval(state: _DomainState) -> None:
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
    )
    state.buffer = None


def _record_aggregate(
    domain: DomainAnalysis,
    buffer: _IntervalBuffer,
    interval: int,
    elapsed_sec: float,
) -> None:
    io_latency = buffer.io_latency_us
    if io_latency == 0 and not buffer.aggregate_iops:
        io_latency = None  # elbencho prints 0 for an interval without IOs
    entries_latency = buffer.entries_latency_us or None
    if (
        buffer.aggregate_mib_s is None
        and buffer.aggregate_iops is None
        and io_latency is None
    ):
        return
    domain.aggregate_points.append(
        AggregatePoint(
            interval=interval,
            elapsed_sec=elapsed_sec,
            iso_timestamp=buffer.iso_timestamp,
            throughput_mib_s=buffer.aggregate_mib_s,
            iops=buffer.aggregate_iops,
            io_latency_us=io_latency,
            entries_latency_us=entries_latency,
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
        summary.observe_done_bytes(done_bytes, buffer.runtime_ms, interval)
        prior = state.previous.get(client)
        rate = derive_rate(prior, done_bytes, buffer.runtime_ms, interval)
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
        state.previous[client] = PreviousSample(done_bytes, buffer.runtime_ms, interval)
    return rates


def derive_rate(
    prior: Optional[PreviousSample],
    done_bytes: int,
    runtime_ms: int,
    interval: int,
) -> Optional[float]:
    """MiB/s since the immediately preceding interval, or None when unusable."""
    if prior is None or prior.interval != interval - 1:
        return None
    elapsed_ms = runtime_ms - prior.runtime_ms
    byte_delta = done_bytes - prior.done_bytes
    if elapsed_ms <= 0 or byte_delta < 0:
        return None
    return byte_delta * 1000.0 / elapsed_ms / MIB


def _invalid_rate_reason(
    prior: PreviousSample,
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
) -> None:
    if not rates:
        return
    values = list(rates.values())
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    stddev = math.sqrt(variance)
    ordered_values = sorted(values)
    p10, median, p90 = (
        percentile_from_sorted(ordered_values, percentile)
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
        domain.clients[client].add_rate(rate, median)


def _percentile(values: Iterable[float], percentile: float) -> float:
    ordered = sorted(values)
    return percentile_from_sorted(ordered, percentile)


def percentile_from_sorted(ordered: Sequence[float], percentile: float) -> float:
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
