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

"""Comparison charts: one block of overlay charts per comparison set.

Each compared section is one series, coloured by its position and labelled
by the setting that differs. Sweeps get line charts per phase (IO) or
operation (metadata): throughput, per-node share (node sweeps) and p99
latency, or ops/s and p99 latency. At each x value a section contributes its
highest-throughput (or highest-rate) execution, which matters when other
dimensions vary too. Single points get bar charts. IO sets with live data
add an aggregate GB/s overlay of each section's best run that has one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from lib.report_html import model
from lib.report_html.common import (
    ELAPSED_S,
    FMT_GBS,
    FMT_MS,
    GB,
    GBS,
    IO_PHASES,
    K_COORDS,
    K_LIVE,
    K_MIX,
    K_NODES,
    K_OCCURRENCE,
    K_PHASE,
    LATENCY_MS,
    LOG,
    MS_PER_S,
    P99_KEY,
    GroupContext,
    axis,
    bar_spec,
    config_label,
    dim_label,
    has_points,
    line_spec,
    phase_of,
    series,
    sweep_axis,
    axis_type,
)
from lib.report_html.live import live_domains
from lib.report_html.mdtest import MD_COLORS, MD_OPS, OPS_PER_S, md_latency_ms, md_rate
from lib.report_html.model import Comparison, Figure, Link, category_color
from lib.report_html.pairing import (
    DIMENSION_TITLES,
    DIMENSIONS,
    IO_MODE,
    Candidate,
    CompareSet,
    comparison_sets,
)
from lib.report_html.reduce import decimate, finite, scaled, sig
from lib.report_html.sweep import gbs_metric, latency_metric, share_metric
from lib.results_document import LoadedDocument

_P99_CAPTION = "p99 latency of the plotted configurations; log scale."
_DURATIONS_NOTE = "Durations differ; see series labels."


@dataclass(frozen=True)
class _Metric:
    """One compared value: ``value(execution, phase_or_op)``."""

    slug: str
    title: str
    y: dict
    caption: str
    value: Callable[[dict, str], Any]
    nodes_only: bool = False


def _io_value(metric: Callable) -> Callable[[dict, str], Any]:
    def value(execution: dict, phase: str):
        data = phase_of(execution, phase)
        return metric(execution[K_COORDS], data) if data else None

    return value


_IO_METRICS = (
    _Metric(
        "throughput",
        "throughput",
        axis(GBS, fmt=FMT_GBS, min=0),
        "Aggregate GB/s (elbencho first-done).",
        _io_value(gbs_metric),
    ),
    _Metric(
        "share",
        "per-node share",
        axis("GB/s per node", fmt=FMT_GBS, min=0),
        "Aggregate GB/s ÷ nodes.",
        _io_value(share_metric),
        nodes_only=True,
    ),
    _Metric(
        "p99",
        "p99 latency",
        axis(LATENCY_MS, LOG, FMT_MS),
        _P99_CAPTION,
        _io_value(latency_metric(P99_KEY)),
    ),
)
_MD_METRICS = (
    _Metric(
        "rate",
        OPS_PER_S,
        axis(OPS_PER_S, LOG),
        "Mean ops/s over complete iterations; log scale.",
        md_rate,
    ),
    _Metric(
        "p99",
        "p99 latency",
        axis(LATENCY_MS, LOG, FMT_MS),
        _P99_CAPTION,
        lambda execution, op: md_latency_ms(execution, op, P99_KEY),
    ),
)


def _first_case(text: str, upper: bool) -> str:
    """Change the first letter's case, unless the text starts with an
    abbreviation such as ``IO`` or ``p99``."""
    if not text[1:2].islower():
        return text
    return (text[0].upper() if upper else text[0].lower()) + text[1:]


def _disambiguate(labels: list[str], extras: list[str]) -> list[str]:
    """Append ``extras`` to empty or repeated labels."""
    out = []
    for label, extra in zip(labels, extras):
        if not label:
            out.append(extra)
        elif labels.count(label) > 1:
            out.append(f"{label} ({extra})")
        else:
            out.append(label)
    return out


def _setting_labels(compare_set: CompareSet, several: bool) -> list[str]:
    members = compare_set.members
    if compare_set.dimension:
        names = [compare_set.dimension]
    else:
        names = [
            name
            for name in DIMENSIONS
            if len({m.values[name].key for m in members}) > 1
        ]
    labels = [" · ".join(m.values[name].label for name in names) for m in members]
    labels = _disambiguate(labels, [m.ref(several) for m in members])
    return _disambiguate(labels, [m.anchor for m in members])


def member_labels(compare_set: CompareSet, several: bool) -> list[str]:
    """Series labels: the differing setting's values, made unique, plus each
    run duration when the durations differ."""
    labels = _setting_labels(compare_set, several)
    if not compare_set.durations_differ:
        return labels
    members = compare_set.members
    return [f"{label} · {m.duration_text}" for label, m in zip(labels, members)]


def _title(compare_set: CompareSet) -> str:
    first = compare_set.members[0]
    if compare_set.dimension is None:
        text = str(compare_set.title)
    elif compare_set.dimension == IO_MODE:
        text = f"{DIMENSION_TITLES[IO_MODE]} — {first.shape.sizes_title()}"
    else:
        text = f"{DIMENSION_TITLES[compare_set.dimension]} — {first.shape.title()}"
    index, parts = compare_set.part
    return f"{text} (part {index} of {parts})" if parts > 1 else text


def _live_domain(execution: dict, phase: str, level: str) -> dict | None:
    for domain in live_domains(execution, level):
        first = not domain.get(K_MIX) and not domain.get(K_OCCURRENCE)
        if first and domain.get(K_PHASE) == phase:
            return domain[K_LIVE]
    return None


class _SetCharts:
    """Builds the figures of one comparison set."""

    def __init__(self, compare_set: CompareSet, anchor: str, several: bool):
        self.members = compare_set.members
        self.anchor = anchor
        self.labels = member_labels(compare_set, several)
        self.durations_differ = compare_set.durations_differ
        first = self.members[0]
        self.io = first.kind == "io"
        self.metrics = _IO_METRICS if self.io else _MD_METRICS
        self.colors = model.PHASE_COLORS if self.io else MD_COLORS
        self.x = first.x
        self.xlabel = dim_label(self.x, first.dims)
        rank = self.metrics[0].value
        self.keys = [
            key
            for key in (IO_PHASES if self.io else MD_OPS)
            if any(rank(e, key) is not None for m in self.members for e in m.executions)
        ]
        self.others_vary = any(m.others_vary for m in self.members)

    def best(self, member: Candidate, key: str) -> list[tuple[Any, dict]]:
        """(x, execution) with the highest rank metric at each x value."""
        rank = self.metrics[0].value
        best: dict = {}
        for execution in member.executions:
            value = finite(rank(execution, key))
            x = execution[K_COORDS].get(self.x) if self.x else 0
            if value is None or x is None:
                continue
            if x not in best or value > best[x][0]:
                best[x] = (value, execution)
        return [(x, best[x][1]) for x in sorted(best)]

    def figure(self, suffix: str, title: str, caption: str, spec: dict) -> Figure:
        """A comparison figure."""
        anchor = f"{self.anchor}-{suffix}"
        if self.durations_differ:
            caption = f"{caption} {_DURATIONS_NOTE}"
        return Figure(model.COMPARISON_CHART, anchor, title, caption, spec, True)

    def _caption(self, metric: _Metric) -> str:
        caption = metric.caption
        if self.others_vary and metric is self.metrics[0]:
            caption += (
                " Each point is the section's best configuration of the other "
                "swept dimensions."
            )
        return caption

    def _line(self, metric: _Metric, key: str, xs: list) -> Figure | None:
        lines = []
        for index, member in enumerate(self.members):
            points = []
            for x, execution in self.best(member, key):
                value = sig(metric.value(execution, key))
                if value is not None:
                    points.append([x, value])
            lines.append(series(self.labels[index], category_color(index), points))
        if not has_points(lines):
            return None
        spec = line_spec(sweep_axis(self.xlabel, axis_type(xs)), metric.y, lines)
        title = f"{key.capitalize()} {metric.title} vs {self.xlabel}"
        return self.figure(f"{metric.slug}-{key}", title, self._caption(metric), spec)

    def line_figures(self) -> list[Figure]:
        """One line chart per metric and phase (or operation)."""
        xs = [e[K_COORDS].get(self.x) for m in self.members for e in m.executions]
        figures = []
        for metric in self.metrics:
            if metric.nodes_only and self.x != K_NODES:
                continue
            for key in self.keys:
                figure = self._line(metric, key, xs)
                if figure:
                    figures.append(figure)
        return figures

    def _bar_values(self, metric: _Metric, key: str) -> list:
        """One value per member: its single point's metric, or None."""
        values = []
        for member in self.members:
            best = self.best(member, key)
            values.append(sig(metric.value(best[0][1], key)) if best else None)
        return values

    def bar_figures(self) -> list[Figure]:
        """Single points: one bar chart per metric, one bar per phase."""
        figures = []
        for metric in self.metrics:
            if metric.nodes_only:
                continue
            bars = [
                {
                    "name": key,
                    "color": self.colors[key],
                    "values": self._bar_values(metric, key),
                }
                for key in self.keys
            ]
            if not has_points(bars):
                continue
            spec = bar_spec(list(self.labels), "section", metric.y, bars)
            title = f"{_first_case(metric.title, upper=True)} at one configuration"
            figures.append(self.figure(metric.slug, title, self._caption(metric), spec))
        return figures

    def _live_run(self, member: Candidate, phase: str):
        """(execution, live) of the highest-throughput run with live data."""
        runs = []
        for execution in member.executions:
            live = _live_domain(execution, phase, member.ctx.live_detail)
            value = finite(self.metrics[0].value(execution, phase)) if live else None
            if value is not None:
                runs.append((value, -len(runs), execution, live))
        if not runs:
            return None
        _, _, execution, live = max(runs, key=lambda run: run[:2])
        return execution, live

    def _live_line(self, index: int, run) -> dict:
        execution, live = run
        aggregate = live["aggregate_series"]
        times = [scaled(v, MS_PER_S) for v in aggregate.get("elapsed_ms") or []]
        gbs = [scaled(v, GB) for v in aggregate.get("bytes_per_s") or []]
        label = config_label(execution[K_COORDS], self.members[index].dims)
        name = f"{self.labels[index]} · {label}"
        return series(name, category_color(index), decimate(times, gbs))

    def live_figures(self) -> list[Figure]:
        """Aggregate GB/s over time per phase, when any section has live data."""
        if not self.io:
            return []
        figures = []
        for phase in self.keys:
            runs = [self._live_run(member, phase) for member in self.members]
            if not any(runs):
                continue
            lines = [
                self._live_line(index, run) for index, run in enumerate(runs) if run
            ]
            spec = line_spec(axis(ELAPSED_S), axis(GBS, fmt=FMT_GBS, min=0), lines)
            figures.append(
                self.figure(
                    f"live-{phase}",
                    f"{phase.capitalize()} aggregate throughput over time",
                    "Aggregate GB/s per interval of each section's "
                    "highest-throughput run with live data.",
                    spec,
                )
            )
        return figures

    def figures(self) -> list[Figure]:
        """Every figure of this set."""
        sweep = self.line_figures() if self.x else self.bar_figures()
        return sweep + self.live_figures()


def _caption(compare_set: CompareSet, several: bool) -> str:
    """The lead text before the member links."""
    differ = compare_set.durations_differ
    if compare_set.dimension:
        name = _first_case(DIMENSION_TITLES[compare_set.dimension], upper=False)
        text = f"Differs in {name} and run duration." if differ else None
        text = text or f"Differs only in {name}:"
    else:
        text = "Chosen by the comparison file" + ("." if differ else ":")
    if not differ:
        return text
    labels = _setting_labels(compare_set, several)
    members = compare_set.members
    parts = [f"{m.duration_text} ({label})" for label, m in zip(labels, members)]
    return f"{text} Durations differ: {', '.join(parts)}. Sections:"


def _comparison(compare_set: CompareSet, anchor: str, several: bool) -> Comparison:
    charts = _SetCharts(compare_set, anchor, several)
    members = [
        Link(f"{label} ({member.ref(several)})", f"#{member.anchor}")
        for label, member in zip(charts.labels, compare_set.members)
    ]
    caption = _caption(compare_set, several)
    return Comparison(anchor, _title(compare_set), caption, members, charts.figures())


def build_comparisons(
    documents: Sequence[LoadedDocument],
    contexts: Sequence[tuple[GroupContext, list[dict]]],
    comparisons: Any = None,
) -> list[Comparison]:
    """Comparison blocks: forced sets in file order, then automatic ones."""
    several = len(documents) > 1
    blocks = []
    used: set[str] = set()
    for compare_set in comparison_sets(documents, contexts, comparisons):
        anchor = compare_set.anchor()
        while anchor in used:
            anchor += "-x"
        used.add(anchor)
        block = _comparison(compare_set, anchor, several)
        if block.figures:
            blocks.append(block)
    return blocks
