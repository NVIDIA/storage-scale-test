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

"""Plots of one elbencho live CSV domain: throughput, latency and fairness.

Every function returns the PNG path, or None when there is nothing to plot,
and closes its figure. A missing value is plotted as a gap (NaN), never 0.
Steady-window bounds (``SteadyAnalysis.window``) shade warmup and tail time.
"""

from __future__ import annotations

import math
import os
from typing import List, Optional, Sequence, Tuple

from lib.elbencho_live_report import (
    DomainAnalysis,
    LiveClientId,
    LiveFileMetadata,
    percentile_from_sorted,
)
from lib.elbencho_live_steady import (
    WINDOW_5S,
    WINDOW_30S,
    WINDOW_INTERVAL,
    SteadyAnalysis,
    WindowedClientDetail,
)

DPI = 150
SHADE_COLOR = "lightgray"
SHADE_ALPHA = 0.35
TIME_LABEL = "Sample time (ISO Date)"
ELAPSED_LABEL = "Elapsed time (s)"
THROUGHPUT_LABEL = "Throughput (MiB/s)"
NO_CLASS = "n/a"
WINDOW_COLORS = {
    WINDOW_INTERVAL: "lightsteelblue",
    WINDOW_5S: "steelblue",
    WINDOW_30S: "navy",
}
WINDOW_NAMES = {WINDOW_INTERVAL: "interval", WINDOW_5S: "5 s", WINDOW_30S: "30 s"}
HEATMAP_TICKS = (50, 75, 100, 125, 150)
HEATMAP_TICK_LABELS = ("<=50%", "75%", "100%", "125%", ">=150%")


def nan_for_none(values: Sequence[Optional[float]]) -> List[float]:
    """Return ``values`` with None replaced by NaN (a plot gap)."""
    return [math.nan if value is None else float(value) for value in values]


def aligned_values(values: Sequence[Optional[float]], count: int) -> List[float]:
    """NaN-fill and truncate or pad ``values`` to exactly ``count`` entries."""
    padded = nan_for_none(list(values)[:count])
    return padded + [math.nan] * (count - len(padded))


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


def series_with_gap_breaks(
    x_values: Sequence[float],
    y_values: Sequence[float],
) -> Tuple[List[float], List[float]]:
    """Insert a NaN midway into any gap wider than 1.5x the median spacing."""
    if len(x_values) < 3:
        return list(x_values), list(y_values)
    deltas = sorted(
        current - previous
        for previous, current in zip(x_values, x_values[1:])
        if current > previous
    )
    expected = percentile_from_sorted(deltas, 50) if deltas else 0.0
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


def _steady_bounds_sec(
    steady: Optional[SteadyAnalysis],
) -> Tuple[Optional[float], Optional[float]]:
    """Steady start and end in seconds (None where not computed)."""
    if steady is None:
        return None, None
    window = steady.window
    start = None if window.start_ms is None else window.start_ms / 1000.0
    end = None if window.end_ms is None else window.end_ms / 1000.0
    return start, end


def _shade_non_steady(axes, steady: Optional[SteadyAnalysis]) -> None:
    """Shade warmup and tail time gray on every axis."""
    start, end = _steady_bounds_sec(steady)
    if start is None and end is None:
        return
    for axis in axes:
        low, high = axis.get_xlim()
        if start is not None and start > low:
            axis.axvspan(low, start, color=SHADE_COLOR, alpha=SHADE_ALPHA, zorder=0)
        if end is not None and end < high:
            axis.axvspan(end, high, color=SHADE_COLOR, alpha=SHADE_ALPHA, zorder=0)
        axis.set_xlim(low, high)


def _mark_first_last(axes, steady: Optional[SteadyAnalysis]) -> None:
    """Draw dashed lines at elbencho's [first] and [last] times."""
    if steady is None:
        return
    for value, name in (
        (steady.context.first_ms, "[first]"),
        (steady.context.last_ms, "[last]"),
    ):
        if value is None:
            continue
        for axis in axes:
            axis.axvline(value / 1000.0, color="dimgray", linestyle="--", linewidth=1)
        axes[0].annotate(
            name,
            (value / 1000.0, 1.0),
            xycoords=("data", "axes fraction"),
            ha="left",
            va="top",
            fontsize=8,
            color="dimgray",
        )


def _steady_note(steady: Optional[SteadyAnalysis]) -> str:
    """Title suffix when the steady window is unavailable."""
    if steady is None or steady.window.available:
        return ""
    return f"\nsteady window unavailable ({steady.window.reason})"


def _output_path(
    metadata: LiveFileMetadata, domain: DomainAnalysis, output_dir: str, name: str
) -> str:
    return os.path.join(output_dir, f"{metadata.stem}-{domain.key.slug}-{name}.png")


def _save(figure, plt, path: str) -> str:
    figure.savefig(path, bbox_inches="tight", dpi=DPI)
    plt.close(figure)
    return path


def plot_aggregate_timeseries(
    metadata: LiveFileMetadata,
    domain: DomainAnalysis,
    steady: Optional[SteadyAnalysis],
    output_dir: str,
) -> Optional[str]:
    """Plot aggregate MiB/s, IOPS and fleet IO latency with steady shading."""
    points = domain.aggregate_points
    if not points:
        return None
    import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel

    elapsed = [point.elapsed_sec for point in points]
    isos = [point.iso_timestamp for point in points]
    x_values, throughput = series_with_gap_breaks(
        elapsed, nan_for_none([point.throughput_mib_s for point in points])
    )
    _, iops = series_with_gap_breaks(
        elapsed, nan_for_none([point.iops for point in points])
    )
    latency = nan_for_none([point.io_latency_us for point in points])
    has_latency = any(not math.isnan(value) for value in latency)
    rows = 2 if has_latency else 1
    figure, axes = plt.subplots(
        rows,
        1,
        sharex=True,
        figsize=(14, 9 if has_latency else 7),
        squeeze=False,
        gridspec_kw={"height_ratios": [2, 1] if has_latency else [1]},
    )
    top = axes[0][0]
    iops_axis = top.twinx()
    top.plot(x_values, throughput, color="steelblue", label="MiB/s")
    iops_axis.plot(x_values, iops, color="darkorange", alpha=0.75, label="IOPS")
    top.set_ylabel(THROUGHPUT_LABEL, color="steelblue")
    iops_axis.set_ylabel("IOPS", color="darkorange")
    top.grid(True, alpha=0.3)
    top.set_title(
        f"Elbencho Aggregate Live Throughput - {domain.key.label}"
        f"{_steady_note(steady)}"
    )
    bottom = axes[rows - 1][0]
    if has_latency:
        _, latency_line = series_with_gap_breaks(elapsed, latency)
        bottom.plot(x_values, latency_line, color="seagreen", label="IO latency")
        bottom.set_ylabel("Fleet IO latency (us)")
        bottom.grid(True, alpha=0.3)
    _set_timestamp_ticks(bottom, elapsed, isos)
    bottom.set_xlabel(TIME_LABEL)
    shaded = [top] + ([bottom] if has_latency else [])
    _shade_non_steady(shaded, steady)
    _mark_first_last(shaded, steady)
    figure.tight_layout()
    path = _output_path(metadata, domain, output_dir, "aggregate-timeseries")
    return _save(figure, plt, path)


def _fairness_title(domain: DomainAnalysis, steady: SteadyAnalysis) -> str:
    title = f"Elbencho Client Fairness - {domain.key.label}"
    summary = steady.summary(WINDOW_5S)
    if summary is not None and summary.jain_p50 is not None:
        title += f"\n5 s Jain p50 {summary.jain_p50:.3f}"
        if summary.jain_min is not None:
            title += f", min {summary.jain_min:.3f}"
    return title + _steady_note(steady)


def plot_fairness_timeseries(
    metadata: LiveFileMetadata,
    domain: DomainAnalysis,
    steady: SteadyAnalysis,
    output_dir: str,
) -> Optional[str]:
    """Plot Jain's index per window size and the 5 s slowest/median ratio."""
    if not any(
        value is not None for series in steady.series.values() for value in series.jain
    ):
        return None
    import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel

    figure, (top, bottom) = plt.subplots(2, 1, sharex=True, figsize=(14, 8))
    for label, series in steady.series.items():
        x_values = [value / 1000.0 for value in series.end_ms]
        top.plot(
            x_values,
            nan_for_none(series.jain),
            color=WINDOW_COLORS.get(label, "gray"),
            label=f"Jain ({WINDOW_NAMES.get(label, label)})",
        )
    summary = steady.summary(WINDOW_5S)
    if summary is not None and summary.floor_p50 is not None:
        top.axhline(
            summary.floor_p50,
            color="dimgray",
            linestyle=":",
            label="expected floor (5 s)",
        )
    top.set_ylabel("Jain's fairness index")
    top.grid(True, alpha=0.3)
    top.legend(loc="lower right", fontsize=8)
    top.set_title(_fairness_title(domain, steady))
    five = steady.series.get(WINDOW_5S)
    if five is not None:
        bottom.plot(
            [value / 1000.0 for value in five.end_ms],
            nan_for_none(five.min_over_median),
            color="steelblue",
            label="slowest / median (5 s)",
        )
    shortfall = steady.shortfall_percent / 100.0
    for level, style in ((1 - shortfall, "--"), (1 - 2 * shortfall, ":")):
        bottom.axhline(level, color="firebrick", linestyle=style, label=f"{level:.2f}")
    bottom.set_ylabel("Slowest client / median")
    bottom.set_xlabel(ELAPSED_LABEL)
    bottom.grid(True, alpha=0.3)
    bottom.legend(loc="lower right", fontsize=8)
    _shade_non_steady([top, bottom], steady)
    figure.tight_layout()
    path = _output_path(metadata, domain, output_dir, "fairness-timeseries")
    return _save(figure, plt, path)


def _client_label(steady: SteadyAnalysis, client: LiveClientId) -> str:
    entry = steady.clients.get(client)
    return f"{client.label} ({(entry.classification if entry else None) or NO_CLASS})"


def plot_client_timeseries(
    metadata: LiveFileMetadata,
    domain: DomainAnalysis,
    steady: SteadyAnalysis,
    detail: WindowedClientDetail,
    output_dir: str,
) -> Optional[str]:
    """Plot the fleet 5 s band and median with selected client lines."""
    five = steady.series.get(WINDOW_5S)
    if five is None or not five.end_ms:
        return None
    import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel
    from matplotlib import colormaps  # pylint: disable=import-outside-toplevel

    count = len(five.end_ms)
    x_values = [value / 1000.0 for value in five.end_ms]
    band = steady.band_5s
    figure, axis = plt.subplots(figsize=(14, 7))
    axis.fill_between(
        x_values,
        aligned_values(band.p10_mib_s, count),
        aligned_values(band.p90_mib_s, count),
        alpha=0.3,
        color="steelblue",
        label="p10-p90",
    )
    axis.plot(
        x_values,
        aligned_values(band.p50_mib_s, count),
        color="steelblue",
        linewidth=2,
        label="fleet median",
    )
    colors = colormaps["Set1"]
    for index, client in enumerate(detail.clients):
        axis.plot(
            x_values,
            aligned_values(detail.rates_mib_s.get(client, []), count),
            linewidth=1.3,
            alpha=0.85,
            color=colors(index % colors.N),
            label=_client_label(steady, client),
        )
    axis.set_xlabel(ELAPSED_LABEL)
    axis.set_ylabel("Throughput per client, 5 s window (MiB/s)")
    axis.set_ylim(0, None)
    axis.set_title(
        f"Elbencho Client Throughput - {domain.key.label}{_steady_note(steady)}"
    )
    axis.grid(True, alpha=0.3)
    _shade_non_steady([axis], steady)
    axis.legend(loc="center left", bbox_to_anchor=(1.02, 0.5), fontsize=8)
    figure.tight_layout()
    path = _output_path(metadata, domain, output_dir, "client-timeseries")
    return _save(figure, plt, path)


def ratio_matrix(detail: WindowedClientDetail, count: int) -> List[List[float]]:
    """Per-client 5 s ratios as percent rows, NaN where missing."""
    return [
        [
            value * 100.0
            for value in aligned_values(detail.ratios.get(client, []), count)
        ]
        for client in detail.clients
    ]


def _non_steady_runs(in_steady: Sequence[bool]) -> List[Tuple[int, int]]:
    """Inclusive (first, last) column runs of windows outside the steady span."""
    runs: List[Tuple[int, int]] = []
    start: Optional[int] = None
    for index, flag in enumerate(in_steady):
        if not flag and start is None:
            start = index
        elif flag and start is not None:
            runs.append((start, index - 1))
            start = None
    if start is not None:
        runs.append((start, len(in_steady) - 1))
    return runs


def plot_client_ratio_heatmap(
    metadata: LiveFileMetadata,
    domain: DomainAnalysis,
    steady: SteadyAnalysis,
    detail: WindowedClientDetail,
    output_dir: str,
) -> Optional[str]:
    """Plot each selected client's 5 s throughput as percent of the fleet median."""
    five = steady.series.get(WINDOW_5S)
    if not detail.clients or five is None or not five.end_ms:
        return None
    import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel
    import numpy as np  # pylint: disable=import-outside-toplevel
    from matplotlib import colormaps  # pylint: disable=import-outside-toplevel

    count = len(five.end_ms)
    matrix = np.array(ratio_matrix(detail, count), dtype=float)
    cmap = colormaps["RdYlBu"].with_extremes(bad=SHADE_COLOR)
    figure, axis = plt.subplots(figsize=(16, max(5, 0.3 * len(detail.clients))))
    image = axis.imshow(
        np.ma.masked_invalid(matrix),
        aspect="auto",
        cmap=cmap,
        vmin=50,
        vmax=150,
        interpolation="nearest",
    )
    for first, last in _non_steady_runs(five.in_steady):
        axis.axvspan(first - 0.5, last + 0.5, color="gray", alpha=0.35, linewidth=0)
    spacing = max(1, count // 10)
    ticks = list(range(0, count, spacing))
    axis.set_xticks(ticks)
    axis.set_xticklabels([f"{five.end_ms[idx] / 1000.0:.0f}" for idx in ticks])
    axis.set_yticks(range(len(detail.clients)))
    axis.set_yticklabels(
        [_client_label(steady, client) for client in detail.clients], fontsize=8
    )
    axis.set_xlabel("5 s window end (s)")
    axis.set_ylabel("Host / worker")
    axis.set_title(
        f"Elbencho Client 5 s Throughput vs Fleet Median - {domain.key.label}"
        f"{_steady_note(steady)}\ngray overlay = outside steady window, "
        "light gray = missing"
    )
    colorbar = figure.colorbar(image, ax=axis, label="% of fleet median")
    colorbar.set_ticks(list(HEATMAP_TICKS))
    colorbar.set_ticklabels(list(HEATMAP_TICK_LABELS))
    figure.tight_layout()
    path = _output_path(metadata, domain, output_dir, "client-ratio-heatmap")
    return _save(figure, plt, path)
