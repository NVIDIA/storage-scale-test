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

"""Steady-window client fairness and fleet latency for one live CSV domain.

The first pass (``elbencho_live_report.analyze_live_csv``) finds the domains,
clients, aggregate points and fleet points. This module bounds the steady
window with elbencho's ``time ms [first]`` and a ramp threshold, then streams
the file again to evaluate sliding windows of one interval, 5 s and 30 s:
Jain's index and each client's ratio to the fleet median, which classify
clients as persistent, transient or ok. Memory stays O(intervals) plus a
30 s ring of ``DoneBytes`` per client.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Set, Tuple

from lib.elbencho_live_report import (
    MIB,
    DomainAnalysis,
    LiveAnalysis,
    LiveClientId,
    client_sort_key,
    iter_parsed_rows,
    live_csv_reader,
    percentile_from_sorted,
)

# Sliding windows, in display order. "interval" is one live interval.
WINDOW_INTERVAL = "interval"
WINDOW_5S = "5s"
WINDOW_30S = "30s"
WINDOW_LABELS = (WINDOW_INTERVAL, WINDOW_5S, WINDOW_30S)
WINDOW_SECONDS = {WINDOW_5S: 5.0, WINDOW_30S: 30.0}

# Steady state starts once aggregate MiB/s reaches this share of the median
# aggregate MiB/s between that point and the end of the steady window.
RAMP_FRACTION = 0.75
# A Jain value below this counts toward share_below_0_95.
JAIN_LOW = 0.95

DEFAULT_SHORTFALL_PERCENT = 5.0
DEFAULT_MIN_SLOW_WINDOWS = 3
# A transient client's share of slow 5 s windows is at least this multiple of
# the median client's share (calibrated on a 5,184-client reference batch).
TRANSIENT_SHARE_FACTOR = 2.0

CLASS_PERSISTENT = "persistent"
CLASS_TRANSIENT = "transient"
CLASS_OK = "ok"
# Display and sort order; clients without steady data sort last.
CLASS_ORDER = (CLASS_PERSISTENT, CLASS_TRANSIENT, CLASS_OK)

REASON_NO_FIRST_TIME = "no_first_time"
REASON_TOO_SHORT = "too_short"
REASON_NO_STEADY_STATE = "no_steady_state"
END_SOURCE_FIRST = "elbencho_first"

# Fallback live interval when neither ELBENCHO_LIVEINT nor row spacing is known.
DEFAULT_INTERVAL_MS = 1000
# Spearman rho needs at least this many window pairs.
MIN_RHO_PAIRS = 6


@dataclass(frozen=True)
class PhaseContext:
    """What the aggregate CSV/.out says about one phase of a live CSV."""

    first_ms: Optional[int]  # aggregate CSV "time ms [first]"
    last_ms: Optional[int]  # aggregate CSV "time ms [last]"
    block_size_bytes: Optional[int]  # the phase's block size, for the floor
    final_avg_latency_us: Optional[float]  # .out IO latency avg


@dataclass(frozen=True)
class SteadyWindow:
    """Inclusive interval bounds of the steady window, or why there is none."""

    start_interval: Optional[int] = None
    end_interval: Optional[int] = None
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    reason: Optional[str] = None  # None when available

    @property
    def available(self) -> bool:
        """Whether the window has bounds."""
        return self.reason is None and self.start_interval is not None


@dataclass
class WindowSeries:  # pylint: disable=too-many-instance-attributes
    """One sliding window size over the whole phase, one entry per window.

    A window ending at interval t covers intervals (t - length, t]. Lists are
    aligned; ``in_steady`` marks windows wholly inside the steady window, and
    only those feed the summary statistics.
    """

    label: str
    length_intervals: int
    window_ms: int
    end_interval: List[int] = field(default_factory=list)
    end_ms: List[int] = field(default_factory=list)
    clients: List[int] = field(default_factory=list)
    jain: List[Optional[float]] = field(default_factory=list)
    median_bytes: List[Optional[float]] = field(default_factory=list)
    min_over_median: List[Optional[float]] = field(default_factory=list)
    floor: List[Optional[float]] = field(default_factory=list)
    in_steady: List[bool] = field(default_factory=list)


@dataclass(frozen=True)
class WindowSummary:  # pylint: disable=too-many-instance-attributes
    """Steady-window statistics of one WindowSeries (None when no windows)."""

    label: str
    window_ms: int
    windows: int
    jain_min: Optional[float]
    jain_p10: Optional[float]
    jain_p50: Optional[float]
    jain_min_at_ms: Optional[int]
    share_below_0_95: Optional[float]
    min_over_median_p10: Optional[float]
    floor_p50: Optional[float]


@dataclass
class FleetBand:
    """5 s windowed fleet rate percentiles (MiB/s), one entry per window."""

    end_ms: List[int] = field(default_factory=list)
    p10_mib_s: List[Optional[float]] = field(default_factory=list)
    p50_mib_s: List[Optional[float]] = field(default_factory=list)
    p90_mib_s: List[Optional[float]] = field(default_factory=list)


@dataclass
class ClientSteady:  # pylint: disable=too-many-instance-attributes
    """One client's steady-window statistics and class.

    Ratios compare the client's bytes in a window with the median over the
    clients that have both samples of that window. ``classification`` is None
    when the steady window is unavailable.
    """

    client: LiveClientId
    steady_bytes: Optional[int] = None
    steady_mean_mib_s: Optional[float] = None
    steady_ratio: Optional[float] = None
    worst_5s_ratio: Optional[float] = None
    worst_5s_ms: Optional[int] = None
    worst_30s_ratio: Optional[float] = None
    worst_30s_ms: Optional[int] = None
    windows_5s: int = 0
    short_5s: int = 0  # 5 s windows with ratio < 1 - S
    slow_5s: int = 0  # 5 s windows with ratio < 1 - 2S
    windows_30s: int = 0
    short_30s: int = 0  # 30 s windows with ratio < 1 - S
    classification: Optional[str] = None

    @property
    def short_5s_share(self) -> Optional[float]:
        """Share of steady 5 s windows below 1 - S, or None without windows."""
        return self.short_5s / self.windows_5s if self.windows_5s else None


@dataclass(frozen=True)
class LatencySummary:  # pylint: disable=too-many-instance-attributes
    """Steady-window fleet IO latency from Total rows (microseconds)."""

    p50_us: Optional[float]
    p90_us: Optional[float]
    max_us: Optional[float]
    max_over_p50: Optional[float]
    cv: Optional[float]
    steady_mean_us: Optional[float]  # op-weighted over the steady window
    final_avg_us: Optional[float]  # from PhaseContext, for comparison
    rho_jain_latency: Optional[float]  # Spearman over steady 5 s windows
    rho_latency_throughput: Optional[float]  # Spearman over steady 5 s windows


@dataclass
class SteadyAnalysis:  # pylint: disable=too-many-instance-attributes
    """Everything the reports need about one domain's steady state."""

    window: SteadyWindow
    context: PhaseContext
    interval_ms: int
    shortfall_percent: float
    min_slow_windows: int
    series: Dict[str, WindowSeries] = field(default_factory=dict)
    band_5s: FleetBand = field(default_factory=FleetBand)
    clients: Dict[LiveClientId, ClientSteady] = field(default_factory=dict)
    latency: Optional[LatencySummary] = None

    def summary(self, label: str) -> Optional[WindowSummary]:
        """Steady statistics of one window size, or None without windows."""
        series = self.series.get(label)
        if series is None:
            return None
        indices = [
            index
            for index, jain in enumerate(series.jain)
            if jain is not None and series.in_steady[index]
        ]
        if not indices:
            return None
        jains = sorted(series.jain[index] for index in indices)
        min_index = min(indices, key=lambda index: series.jain[index])
        return WindowSummary(
            label=label,
            window_ms=series.window_ms,
            windows=len(indices),
            jain_min=jains[0],
            jain_p10=percentile_from_sorted(jains, 10),
            jain_p50=percentile_from_sorted(jains, 50),
            jain_min_at_ms=series.end_ms[min_index],
            share_below_0_95=sum(1 for jain in jains if jain < JAIN_LOW) / len(jains),
            min_over_median_p10=_percentile_of(
                [series.min_over_median[index] for index in indices], 10
            ),
            floor_p50=_percentile_of([series.floor[index] for index in indices], 50),
        )

    def class_counts(self) -> Dict[str, int]:
        """Client count per class (every CLASS_ORDER key present)."""
        counts = {name: 0 for name in CLASS_ORDER}
        for client in self.clients.values():
            if client.classification in counts:
                counts[client.classification] += 1
        return counts


@dataclass
class WindowedClientDetail:
    """5 s windowed rates and ratios of selected clients over the phase.

    Lists align with ``SteadyAnalysis.series[WINDOW_5S].end_ms``; None marks a
    window without both samples.
    """

    clients: List[LiveClientId]
    rates_mib_s: Dict[LiveClientId, List[Optional[float]]]
    ratios: Dict[LiveClientId, List[Optional[float]]]


def interval_ms_of(domain: DomainAnalysis, configured_ms: Optional[int]) -> int:
    """The live interval: configured ELBENCHO_LIVEINT, else the median spacing."""
    if configured_ms is not None and configured_ms > 0:
        return int(configured_ms)
    times = domain.interval_times
    spacings = sorted(
        current - previous
        for previous, current in zip(times, times[1:])
        if current > previous
    )
    if spacings:
        median_ms = round(percentile_from_sorted(spacings, 50) * 1000)
        if median_ms > 0:
            return median_ms
    return DEFAULT_INTERVAL_MS


def window_length_intervals(seconds: float, interval_ms: int) -> int:
    """Intervals in a window of ``seconds`` (rounded, at least 1)."""
    return max(1, round(seconds * 1000 / interval_ms))


def steady_window(
    domain: DomainAnalysis, context: PhaseContext, interval_ms: int
) -> SteadyWindow:
    """Bound the steady window of one domain (see the module docstring).

    The end is the last interval at or before ``time ms [first]``; the start
    is the first interval after every client progressed (never the first
    row) where aggregate MiB/s reaches RAMP_FRACTION of the steady median.
    """
    runtimes = _runtimes_ms(domain)
    end = _last_interval_at_or_before(runtimes, context.first_ms)
    if end is None:
        return SteadyWindow(reason=REASON_NO_FIRST_TIME)
    end_ms = runtimes[end]
    candidate = 1 if not domain.clients else domain.all_started_interval
    if candidate is None or max(1, candidate) > end:
        return SteadyWindow(
            end_interval=end, end_ms=end_ms, reason=REASON_NO_STEADY_STATE
        )
    start = _ramp_start(domain, max(1, candidate), end)
    start_ms = runtimes[start]
    min_span_ms = max(2 * WINDOW_SECONDS[WINDOW_5S] * 1000, 2 * interval_ms)
    reason = None
    if end_ms - start_ms < min_span_ms:
        reason = REASON_TOO_SHORT
    elif start_ms - runtimes[0] > 0.5 * (end_ms - runtimes[0]):
        reason = REASON_NO_STEADY_STATE
    return SteadyWindow(start, end, start_ms, end_ms, reason)


def _runtimes_ms(domain: DomainAnalysis) -> List[int]:
    """RuntimeMS of each interval."""
    return [round(seconds * 1000) for seconds in domain.interval_times]


def _last_interval_at_or_before(
    runtimes: Sequence[int], limit_ms: Optional[int]
) -> Optional[int]:
    if limit_ms is None:
        return None
    found = None
    for interval, runtime_ms in enumerate(runtimes):
        if runtime_ms <= limit_ms:
            found = interval
    return found


def _ramp_start(domain: DomainAnalysis, candidate: int, end: int) -> int:
    """First interval in [candidate, end] past the ramp threshold."""
    rates = [
        (point.interval, point.throughput_mib_s)
        for point in domain.aggregate_points
        if candidate <= point.interval <= end and point.throughput_mib_s is not None
    ]
    if not rates:
        return candidate
    median = percentile_from_sorted(sorted(rate for _, rate in rates), 50)
    for interval, rate in sorted(rates):
        if rate >= RAMP_FRACTION * median:
            return interval
    return candidate


def _percentile_of(values: Sequence[Optional[float]], pct: float) -> Optional[float]:
    """Percentile of the non-None values, or None when there are none."""
    present = sorted(value for value in values if value is not None)
    return percentile_from_sorted(present, pct) if present else None


def _domain_client_rows(
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    wanted: Optional[frozenset],
) -> Iterator[Tuple[int, LiveClientId, int]]:
    """Stream (interval, client, DoneBytes) of the domain's client rows."""
    key = domain.key
    to_interval = domain.runtime_to_interval
    path = analysis.metadata.path
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader, columns = live_csv_reader(handle, path)
        for row in iter_parsed_rows(reader, columns):
            client = row.client
            if client is None or row.done_bytes is None or row.key != key:
                continue
            if wanted is not None and client not in wanted:
                continue
            interval = to_interval.get(row.runtime_ms)
            if interval is not None:
                yield interval, client, row.done_bytes


def _iter_interval_snapshots(
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    wanted: Optional[frozenset] = None,
) -> Iterator[Tuple[int, Dict[LiveClientId, int]]]:
    """Stream the file; yield (interval, {client: DoneBytes}) for every
    interval of the domain in order, including intervals without client rows.

    ``wanted`` limits the snapshot to those clients.
    """
    current = 0
    snapshot: Dict[LiveClientId, int] = {}
    rows: Iterator[Tuple[int, LiveClientId, int]] = iter(())
    if domain.clients and (wanted is None or wanted):
        rows = _domain_client_rows(analysis, domain, wanted)
    for interval, client, done_bytes in rows:
        if interval < current:
            continue
        while current < interval:
            yield current, snapshot
            snapshot = {}
            current += 1
        snapshot[client] = done_bytes
    while current < len(domain.interval_times):
        yield current, snapshot
        snapshot = {}
        current += 1


def _window_deltas(
    snapshot: Dict[LiveClientId, int],
    previous: Dict[LiveClientId, int],
    resets: Dict[LiveClientId, int],
    begin: int,
) -> Dict[LiveClientId, int]:
    """Bytes per client between two snapshots.

    Negative deltas are skipped, and so are clients whose counter went back
    after interval ``begin`` (a reset can hide inside a positive delta).
    """
    deltas = {}
    for client, done_bytes in snapshot.items():
        prior = previous.get(client)
        if prior is None or done_bytes < prior:
            continue
        if resets and resets.get(client, -1) > begin:
            continue
        deltas[client] = done_bytes - prior
    return deltas


class _SnapshotRing:
    """The last ``depth + 1`` interval snapshots and each client's last reset."""

    def __init__(self, depth: int) -> None:
        self.depth = depth
        self.snapshots: Dict[int, Dict[LiveClientId, int]] = {}
        self.last_done: Dict[LiveClientId, int] = {}
        self.reset_at: Dict[LiveClientId, int] = {}

    def add(self, interval: int, snapshot: Dict[LiveClientId, int]) -> None:
        """Store one completed interval and note counter resets."""
        last_done = self.last_done
        for client, done_bytes in snapshot.items():
            prior = last_done.get(client)
            if prior is not None and done_bytes < prior:
                self.reset_at[client] = interval
        last_done.update(snapshot)
        self.snapshots[interval] = snapshot
        self.snapshots.pop(interval - self.depth - 1, None)

    def deltas(
        self,
        interval: int,
        begin: int,
        previous: Optional[Dict[LiveClientId, int]] = None,
    ) -> Optional[Dict[LiveClientId, int]]:
        """Window deltas over (begin, interval], or None without a baseline.

        ``previous`` overrides the ring's snapshot at ``begin``.
        """
        if previous is None:
            previous = self.snapshots.get(begin) if begin >= 0 else None
        if previous is None:
            return None
        return _window_deltas(self.snapshots[interval], previous, self.reset_at, begin)


class _SteadyPass:  # pylint: disable=too-many-instance-attributes
    """Accumulate window series, the band and client ratios interval by interval."""

    def __init__(self, steady: SteadyAnalysis, runtimes: List[int]) -> None:
        self.steady = steady
        self.runtimes = runtimes
        self.ring = _SnapshotRing(
            max(series.length_intervals for series in steady.series.values())
        )
        # Each client's last (DoneBytes, RuntimeMS) at or before a bound.
        self.last_seen: Dict[LiveClientId, Tuple[int, int]] = {}
        self.at_start: Optional[Dict[LiveClientId, Tuple[int, int]]] = None
        self.at_end: Optional[Dict[LiveClientId, Tuple[int, int]]] = None
        # Clients whose counter reset inside the steady window, fixed at its end
        # so resets in the excluded tail cannot invalidate steady results.
        self.steady_resets: Set[LiveClientId] = set()
        fraction = steady.shortfall_percent / 100.0
        self.short_below = 1.0 - fraction
        self.slow_below = 1.0 - 2.0 * fraction
        # (end interval, jain, window bytes) of in-steady 5 s windows, for rho.
        self.steady_5s: List[Tuple[int, Optional[float], int]] = []

    def complete(self, interval: int, snapshot: Dict[LiveClientId, int]) -> None:
        """Evaluate every window ending at ``interval``."""
        window = self.steady.window
        if window.available:
            # A bound with no rows of its own is captured from earlier rows.
            self._capture_bounds(interval - 1, window)
        self.ring.add(interval, snapshot)
        runtime_ms = self.runtimes[interval]
        for client, done_bytes in snapshot.items():
            self.last_seen[client] = (done_bytes, runtime_ms)
        if window.available:
            self._capture_bounds(interval, window)
        for series in self.steady.series.values():
            deltas = self.ring.deltas(interval, interval - series.length_intervals)
            if deltas is not None:
                self._record(series, interval, deltas)

    def _record(
        self, series: WindowSeries, interval: int, deltas: Dict[LiveClientId, int]
    ) -> None:
        values = sorted(deltas.values())
        count = len(values)
        total = sum(values)
        squares = sum(value * value for value in values)
        median = percentile_from_sorted(values, 50) if count else None
        jain = min_over_median = floor = None
        if count >= 2 and squares > 0:
            jain = total * total / (count * squares)
            if median > 0:
                min_over_median = values[0] / median
        block_size = self.steady.context.block_size_bytes
        if block_size and count and total > 0:
            per_window = total / count / block_size
            floor = per_window / (per_window + 1.0)
        window = self.steady.window
        in_steady = (
            window.available
            and interval - series.length_intervals >= window.start_interval
            and interval <= window.end_interval
        )
        end_ms = self.runtimes[interval]
        series.end_interval.append(interval)
        series.end_ms.append(end_ms)
        series.clients.append(count)
        series.jain.append(jain)
        series.median_bytes.append(median)
        series.min_over_median.append(min_over_median)
        series.floor.append(floor)
        series.in_steady.append(in_steady)
        if series.label == WINDOW_5S:
            elapsed_ms = end_ms - self.runtimes[interval - series.length_intervals]
            self._record_band(end_ms, elapsed_ms, values)
            if in_steady:
                self.steady_5s.append((interval, jain, total))
        if in_steady and median and series.label in WINDOW_SECONDS:
            self._record_ratios(series.label, deltas, median, end_ms)

    def _record_band(self, end_ms: int, elapsed_ms: int, values: List[int]) -> None:
        band = self.steady.band_5s
        band.end_ms.append(end_ms)
        if not values or elapsed_ms <= 0:
            for column in (band.p10_mib_s, band.p50_mib_s, band.p90_mib_s):
                column.append(None)
            return
        # Rates use the samples' own RuntimeMS span, not the configured interval.
        scale = 1000.0 / elapsed_ms / MIB
        band.p10_mib_s.append(percentile_from_sorted(values, 10) * scale)
        band.p50_mib_s.append(percentile_from_sorted(values, 50) * scale)
        band.p90_mib_s.append(percentile_from_sorted(values, 90) * scale)

    def _record_ratios(
        self,
        label: str,
        deltas: Dict[LiveClientId, int],
        median: float,
        end_ms: int,
    ) -> None:
        clients = self.steady.clients
        for client, window_bytes in deltas.items():
            stats = clients.get(client)
            if stats is None:
                stats = clients[client] = ClientSteady(client=client)
            ratio = window_bytes / median
            if label == WINDOW_5S:
                stats.windows_5s += 1
                stats.short_5s += ratio < self.short_below
                stats.slow_5s += ratio < self.slow_below
                if stats.worst_5s_ratio is None or ratio < stats.worst_5s_ratio:
                    stats.worst_5s_ratio = ratio
                    stats.worst_5s_ms = end_ms
            else:
                stats.windows_30s += 1
                stats.short_30s += ratio < self.short_below
                if stats.worst_30s_ratio is None or ratio < stats.worst_30s_ratio:
                    stats.worst_30s_ratio = ratio
                    stats.worst_30s_ms = end_ms

    def _capture_bounds(self, completed: int, window: SteadyWindow) -> None:
        """Snapshot each client's last sample once ``completed`` reaches a bound."""
        if self.at_start is None and completed >= window.start_interval:
            self.at_start = dict(self.last_seen)
        if self.at_end is None and completed >= window.end_interval:
            self.at_end = dict(self.last_seen)
            self.steady_resets = {
                client
                for client, reset in self.ring.reset_at.items()
                if reset > window.start_interval
            }

    def _steady_deltas(self, window: SteadyWindow) -> Dict[LiveClientId, int]:
        """Steady-window bytes per client, scaled to the window's span when a
        client's own samples straddle the bounds; counter resets are dropped."""
        if self.at_start is None or self.at_end is None:
            return {}
        span_ms = window.end_ms - window.start_ms
        deltas = {}
        for client, (end_done, end_ms) in self.at_end.items():
            start = self.at_start.get(client)
            if start is None or client in self.steady_resets:
                continue
            start_done, start_ms = start
            if end_ms > start_ms and end_done >= start_done:
                deltas[client] = round(
                    (end_done - start_done) * span_ms / (end_ms - start_ms)
                )
        return deltas

    def finish(self) -> None:
        """Steady-window bytes, ratios and classes once the stream ends."""
        window = self.steady.window
        if not window.available:
            return
        self._steady_bytes(window, self._steady_deltas(window))
        shares = sorted(
            stats.slow_5s / stats.windows_5s
            for stats in self.steady.clients.values()
            if stats.windows_5s
        )
        fleet_share = percentile_from_sorted(shares, 50) if shares else 0.0
        for stats in self.steady.clients.values():
            stats.classification = self._classify(stats, fleet_share)

    def _steady_bytes(
        self, window: SteadyWindow, deltas: Dict[LiveClientId, int]
    ) -> None:
        seconds = (window.end_ms - window.start_ms) / 1000.0
        median = percentile_from_sorted(sorted(deltas.values()), 50) if deltas else 0
        for client, steady_bytes in deltas.items():
            stats = self.steady.clients.setdefault(client, ClientSteady(client=client))
            stats.steady_bytes = steady_bytes
            if seconds > 0:
                stats.steady_mean_mib_s = steady_bytes / seconds / MIB
            if median > 0:
                stats.steady_ratio = steady_bytes / median

    def _classify(self, stats: ClientSteady, fleet_share: float) -> Optional[str]:
        if stats.steady_ratio is None and not stats.windows_5s:
            return None
        if (
            stats.steady_ratio is not None and stats.steady_ratio < self.short_below
        ) or (stats.windows_30s and stats.short_30s >= 0.5 * stats.windows_30s):
            return CLASS_PERSISTENT
        # Clients take turns dipping; transient means dipping at least twice as
        # often as the median client, not merely dipping.
        if (
            stats.slow_5s >= self.steady.min_slow_windows
            and stats.slow_5s >= TRANSIENT_SHARE_FACTOR * fleet_share * stats.windows_5s
        ):
            return CLASS_TRANSIENT
        return CLASS_OK


def analyze_steady(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    context: PhaseContext,
    configured_interval_ms: Optional[int] = None,
    shortfall_percent: float = DEFAULT_SHORTFALL_PERCENT,
    min_slow_windows: int = DEFAULT_MIN_SLOW_WINDOWS,
) -> SteadyAnalysis:
    """Second streaming pass: window series, band, client classes, latency."""
    interval_ms = interval_ms_of(domain, configured_interval_ms)
    steady = SteadyAnalysis(
        window=steady_window(domain, context, interval_ms),
        context=context,
        interval_ms=interval_ms,
        shortfall_percent=shortfall_percent,
        min_slow_windows=min_slow_windows,
    )
    for label in WINDOW_LABELS:
        length = window_length_intervals(WINDOW_SECONDS.get(label, 0.0), interval_ms)
        steady.series[label] = WindowSeries(label, length, length * interval_ms)
    steady.clients = {client: ClientSteady(client=client) for client in domain.clients}
    if domain.all_started_interval is None:
        # No client moved data (SYNC, or Total rows only): skip the second read
        # of the file; Total-row latency needs no client data.
        steady.latency = _latency_summary(domain, steady, [])
        return steady
    accumulator = _SteadyPass(steady, _runtimes_ms(domain))
    for interval, snapshot in _iter_interval_snapshots(analysis, domain):
        accumulator.complete(interval, snapshot)
    accumulator.finish()
    steady.latency = _latency_summary(domain, steady, accumulator.steady_5s)
    return steady


def _latency_summary(
    domain: DomainAnalysis,
    steady: SteadyAnalysis,
    steady_5s: Sequence[Tuple[int, Optional[float], int]],
) -> Optional[LatencySummary]:
    """Fleet IO latency over the steady window, or None without one."""
    window = steady.window
    if not window.available:
        return None
    points = [
        point
        for point in domain.aggregate_points
        if point.io_latency_us is not None
        and window.start_interval <= point.interval <= window.end_interval
    ]
    if not points:
        return None
    values = sorted(point.io_latency_us for point in points)
    p50 = percentile_from_sorted(values, 50)
    mean = sum(values) / len(values)
    weighted = [(point.io_latency_us, point.iops) for point in points if point.iops]
    weight = sum(iops for _, iops in weighted if iops > 0)
    rho_jain, rho_throughput = _latency_rhos(domain, steady, steady_5s)
    return LatencySummary(
        p50_us=p50,
        p90_us=percentile_from_sorted(values, 90),
        max_us=values[-1],
        max_over_p50=values[-1] / p50 if p50 > 0 else None,
        cv=statistics.pstdev(values) / mean if mean > 0 else None,
        steady_mean_us=(
            sum(latency * iops for latency, iops in weighted if iops > 0) / weight
            if weight > 0
            else None
        ),
        final_avg_us=steady.context.final_avg_latency_us,
        rho_jain_latency=rho_jain,
        rho_latency_throughput=rho_throughput,
    )


def _latency_rhos(
    domain: DomainAnalysis,
    steady: SteadyAnalysis,
    steady_5s: Sequence[Tuple[int, Optional[float], int]],
) -> Tuple[Optional[float], Optional[float]]:
    """Spearman rho of Jain vs latency and latency vs throughput (5 s)."""
    latency_by_interval = {
        point.interval: point.io_latency_us
        for point in domain.aggregate_points
        if point.io_latency_us is not None
    }
    length = steady.series[WINDOW_5S].length_intervals
    jain_pairs: List[Tuple[float, float]] = []
    throughput_pairs: List[Tuple[float, float]] = []
    for end, jain, total in steady_5s:
        latencies = [
            latency_by_interval[interval]
            for interval in range(end - length + 1, end + 1)
            if interval in latency_by_interval
        ]
        if not latencies:
            continue
        latency = sum(latencies) / len(latencies)
        if jain is not None:
            jain_pairs.append((jain, latency))
        throughput_pairs.append((latency, float(total)))
    return _spearman(jain_pairs), _spearman(throughput_pairs)


def _spearman(pairs: Sequence[Tuple[float, float]]) -> Optional[float]:
    """Spearman rank correlation, or None when undefined or too few pairs."""
    if len(pairs) < MIN_RHO_PAIRS:
        return None
    xs = [x for x, _ in pairs]
    ys = [y for _, y in pairs]
    if len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    return statistics.correlation(_average_ranks(xs), _average_ranks(ys))


def _average_ranks(values: Sequence[float]) -> List[float]:
    """1-based ranks with ties sharing their average rank."""
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(order):
        tie_end = position
        while (
            tie_end + 1 < len(order)
            and values[order[tie_end + 1]] == values[order[position]]
        ):
            tie_end += 1
        average = (position + tie_end) / 2.0 + 1.0
        for index in order[position : tie_end + 1]:
            ranks[index] = average
        position = tie_end + 1
    return ranks


def _ratio_key(ratio: Optional[float]) -> Tuple[bool, float]:
    """Sort ascending with None last."""
    return (ratio is None, ratio if ratio is not None else 0.0)


def classified_clients(steady: SteadyAnalysis, limit: int) -> List[LiveClientId]:
    """Persistent clients, then transient ones, worst first, at most ``limit``."""
    if limit <= 0:
        return []
    clients = steady.clients.values()
    persistent = sorted(
        (stats for stats in clients if stats.classification == CLASS_PERSISTENT),
        key=lambda stats: (
            _ratio_key(stats.steady_ratio),
            client_sort_key(stats.client),
        ),
    )
    transient = sorted(
        (stats for stats in clients if stats.classification == CLASS_TRANSIENT),
        key=lambda stats: (
            _ratio_key(stats.worst_5s_ratio),
            client_sort_key(stats.client),
        ),
    )
    return [stats.client for stats in persistent + transient][:limit]


def select_heatmap_rows(steady: SteadyAnalysis, limit: int) -> List[LiveClientId]:
    """Classified clients first, then evenly spaced representatives of the
    rest; returned sorted by steady ratio (no ratio last), then client order."""
    if limit <= 0:
        return []
    chosen = classified_clients(steady, limit)
    chosen_set = set(chosen)

    def order(client: LiveClientId):
        return (
            _ratio_key(steady.clients[client].steady_ratio),
            client_sort_key(client),
        )

    remaining = sorted(
        (client for client in steady.clients if client not in chosen_set), key=order
    )
    chosen.extend(_representative_values(remaining, limit - len(chosen)))
    return sorted(chosen, key=order)


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


def collect_windowed_client_detail(
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    steady: SteadyAnalysis,
    clients: Sequence[LiveClientId],
) -> WindowedClientDetail:
    """Third streaming pass, bounded to ``clients``: 5 s rates and ratios."""
    chosen = list(dict.fromkeys(clients))
    series = steady.series.get(WINDOW_5S)
    if series is None:
        series = WindowSeries(WINDOW_5S, 1, steady.interval_ms)
    deltas_by_end = _client_window_deltas(analysis, domain, series, chosen)
    runtimes = _runtimes_ms(domain)
    # MiB/s per byte over each window's actual RuntimeMS span.
    scales = [
        1000.0 / elapsed / MIB if elapsed > 0 else None
        for elapsed in (
            runtimes[end] - runtimes[end - series.length_intervals]
            for end in series.end_interval
        )
    ]
    rates: Dict[LiveClientId, List[Optional[float]]] = {}
    ratios: Dict[LiveClientId, List[Optional[float]]] = {}
    for client in chosen:
        window_bytes = [
            deltas_by_end.get(end, {}).get(client) for end in series.end_interval
        ]
        rates[client] = [
            None if value is None or scale is None else value * scale
            for value, scale in zip(window_bytes, scales)
        ]
        ratios[client] = [
            value / median if value is not None and median else None
            for value, median in zip(window_bytes, series.median_bytes)
        ]
    return WindowedClientDetail(chosen, rates, ratios)


def _client_window_deltas(
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    series: WindowSeries,
    chosen: Sequence[LiveClientId],
) -> Dict[int, Dict[LiveClientId, int]]:
    """Window bytes of the chosen clients, keyed by window end interval."""
    deltas_by_end: Dict[int, Dict[LiveClientId, int]] = {}
    if not series.end_interval or not chosen:
        return deltas_by_end
    length = series.length_intervals
    wanted_ends = set(series.end_interval)
    ring = _SnapshotRing(length)
    for interval, snapshot in _iter_interval_snapshots(
        analysis, domain, frozenset(chosen)
    ):
        ring.add(interval, snapshot)
        deltas = ring.deltas(interval, interval - length)
        if interval in wanted_ends and deltas is not None:
            deltas_by_end[interval] = deltas
    return deltas_by_end
