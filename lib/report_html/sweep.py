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

"""IO sections: one per group and shape (block sizes, access, IO mode).

Sweep charts put the swept dimension on x (nodes, then threads, then IO
depth) with one series per combination of the other varying dimensions and
phase. Throughput is elbencho's ``[first]`` aggregate, as in the text report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from lib.report_html import model
from lib.report_html.common import (
    CATEGORY,
    FMT_GBS,
    FMT_MS,
    GB,
    GBS,
    GIB,
    IO_DIMS,
    IO_PHASES,
    K_COORDS,
    K_DEPTH,
    K_NODES,
    K_THREADS,
    LATENCY_MS,
    LOG,
    MAX,
    MIN,
    MS_PER_S,
    NO_VALUE,
    P50_KEY,
    P99_KEY,
    PHASE_WRITE,
    US_PER_MS,
    WHOLE,
    GroupContext,
    Part,
    axis,
    axis_type,
    bar_spec,
    combo_label,
    config_label,
    config_parts,
    dim_label,
    drop_column,
    execution_ids,
    has_points,
    lap_dash,
    line_spec,
    node_title,
    none_last,
    phase_of,
    results_of,
    series,
    size_slug,
    size_text,
    sort_executions,
    steady_missing_text,
    steady_span,
    sweep_axes,
    sweep_axis,
)
from lib.report_html.live import build_run, domain_label, domain_order, live_domains
from lib.report_html.model import (
    Column,
    Figure,
    Link,
    Section,
    Table,
    category_color,
    slug,
)
from lib.report_html.reduce import finite, scaled, sig

_ACCESS = {"sequential": "seq", "random": "rand"}
_ACCESS_TITLE = {"sequential": "Seq", "random": "Rand"}
_ONLY_TITLE = {"ro": "read only", "wo": "write only"}
_LATENCY_KEYS = (P50_KEY, P99_KEY)
_RUN = "run"
_BEST_NOTE = "Best value in bold."
# IO sizes up to this lead with IOPS rather than throughput.
SMALL_IO_BYTES = 64 * 1024
# Sections whose phases all run at least this long name the length.
LONG_PHASE_S = 600


def _phase_seconds(phase: dict) -> float | None:
    limit = phase.get("configured_time_limit_s")
    if limit:
        return limit
    elapsed = (phase.get("elbencho_first") or {}).get("elapsed_ms")
    return elapsed / MS_PER_S if elapsed else None


def _long_phases(executions: list[dict]) -> str:
    """``30 min per phase`` when every write and read phase ran at least
    ``LONG_PHASE_S``, else empty."""
    seconds = [
        _phase_seconds(phase)
        for execution in executions
        for phase in results_of(execution).get("phases") or []
        if phase.get("phase") in IO_PHASES
    ]
    if not seconds or None in seconds or min(seconds) < LONG_PHASE_S:
        return ""
    low, high = round(min(seconds) / 60), round(max(seconds) / 60)
    return f"{low if low == high else f'{low}–{high}'} min per phase"


def _access_slug(access: str | None) -> str:
    if not access:
        return "x"
    return _ACCESS.get(access) or slug(access)


@dataclass(frozen=True)
class IoShape:
    """Block sizes, access patterns and IO mode shared by a section."""

    write_block: int | None
    write_access: str | None
    read_block: int | None
    read_access: str | None
    direct_io: bool

    @classmethod
    def of(cls, coords: dict) -> "IoShape":
        """The shape of one execution's coordinates."""
        return cls(
            coords.get("write_block_size_bytes"),
            coords.get("write_access"),
            coords.get("read_block_size_bytes"),
            coords.get("read_access"),
            bool(coords.get("direct_io")),
        )

    def _parts(self) -> list[tuple[str, int | None, str | None]]:
        write = ("w", self.write_block, self.write_access)
        read = ("r", self.read_block, self.read_access)
        if not self.read_block:
            return [write]
        if not self.write_block or write[1:] == read[1:]:
            return [read]
        return [write, read]

    @property
    def block(self) -> int | None:
        """The primary block size (read when present)."""
        return self.read_block or self.write_block

    @property
    def access(self) -> str | None:
        """The primary access pattern (read when present)."""
        return self.read_access or self.write_access

    def sort_key(self) -> tuple:
        """Block size, access, then direct before buffered."""
        return (
            self.block or 0,
            self.write_block or 0,
            str(self.access),
            str(self.write_access),
            not self.direct_io,
        )

    def _only(self) -> str:
        """``ro`` (read only), ``wo`` (write only) or empty (both phases)."""
        if not self.write_block:
            return "ro" if self.read_block else ""
        return "" if self.read_block else "wo"

    def slug(self) -> str:
        """Anchor part, e.g. ``1m-seq-dio``, ``1m-seq-ro-dio`` (read only) or
        ``w1m-seq-r4k-rand-dio``."""
        parts = self._parts()
        prefix = len(parts) > 1
        words = [
            f"{tag if prefix else ''}{size_slug(size)}-{_access_slug(access)}"
            for tag, size, access in parts
        ]
        only = self._only()
        words += [only] if only else []
        return "-".join(words + ["dio" if self.direct_io else "bio"])

    def sizes_title(self) -> str:
        """Block sizes and access without the IO mode, e.g. ``1 MiB seq``
        or ``1 MiB seq, read only``."""
        parts = self._parts()
        names = {"w": "write ", "r": "read "}
        words = [
            f"{names[tag] if len(parts) > 1 else ''}{size_text(size)} "
            f"{_ACCESS.get(access, access or '')}".strip()
            for tag, size, access in parts
        ]
        only = self._only()
        words += [_ONLY_TITLE[only]] if only else []
        return ", ".join(words)

    def phase_size(self, phase: str) -> tuple[int | None, str | None]:
        """Block size and access of one phase (the primary ones if unset)."""
        if phase == PHASE_WRITE and self.write_block:
            return self.write_block, self.write_access
        if phase != PHASE_WRITE and self.read_block:
            return self.read_block, self.read_access
        return self.block, self.access

    def mode_title(self) -> str:
        """``DirectIO`` or ``Buffered``."""
        return "DirectIO" if self.direct_io else "Buffered"

    def title(self) -> str:
        """Display title, e.g. ``1 MiB seq · DirectIO``."""
        return f"{self.sizes_title()} · {self.mode_title()}"


def io_shapes(executions: list[dict]) -> list[tuple[IoShape, list[dict]]]:
    """Executions with results, split by shape, in shape order."""
    shapes: dict[IoShape, list[dict]] = {}
    for execution in executions:
        coords = execution.get(K_COORDS)
        if coords and (execution.get("results") or {}).get("phases"):
            shapes.setdefault(IoShape.of(coords), []).append(execution)
    return sorted(shapes.items(), key=lambda item: item[0].sort_key())


def _first_rate(phase: dict | None, key: str):
    return finite(((phase or {}).get("elbencho_first") or {}).get(key))


def gbs_metric(_coords, phase):
    """Aggregate GB/s (elbencho first-done) of a phase."""
    return scaled(_first_rate(phase, "bytes_per_s"), GB)


def _iops(_coords, phase):
    return _first_rate(phase, "ops_per_s")


def share_metric(coords, phase):
    """Aggregate GB/s per node."""
    value = gbs_metric(coords, phase)
    nodes = coords.get(K_NODES) or 0
    return value / nodes if value is not None and nodes else None


def latency_metric(key: str) -> Callable:
    """A metric giving a phase's ``key`` latency percentile in ms."""

    def metric(_coords, phase):
        return scaled(((phase or {}).get("latency_us") or {}).get(key), US_PER_MS)

    return metric


class _Sweep:  # pylint: disable=too-many-instance-attributes
    """One IO section's executions and sweep layout."""

    def __init__(self, ctx: GroupContext, shape: IoShape, executions: list[dict]):
        self.ctx = ctx
        self.shape = shape
        self.executions = sort_executions(executions)
        self.ids = execution_ids(self.executions)
        coords = [e[K_COORDS] for e in self.executions]
        self.x, self.others = sweep_axes(coords, IO_DIMS)
        self.combos = sorted({self.combo(c) for c in coords}, key=none_last)
        self.parts = config_parts(len(self.combos))
        self.phases = [
            p for p in IO_PHASES if any(phase_of(e, p) for e in self.executions)
        ]
        self.anchor = ctx.anchor(shape.slug())
        self.nodes = node_title(coords)
        words = [shape.title(), self.nodes, _long_phases(self.executions)]
        self.title = " · ".join(word for word in words if word)
        self.xlabel = dim_label(self.x, IO_DIMS)
        self.xtype = axis_type(c.get(self.x) for c in coords) if self.x else CATEGORY
        self.runs: dict[int, str] = {}

    def combo(self, coords: dict) -> tuple:
        """Values of the non-x varying dimensions."""
        return tuple(coords.get(name) for name in self.others)

    def eid(self, execution: dict) -> str:
        """Display ID of an execution."""
        return self.ids[id(execution)]

    def points(self, phase: str, combo: tuple, metric: Callable) -> list:
        """Sorted [x, value] points of one series; executions without an x
        value or a finite metric (older documents) are left out."""
        points = []
        for execution in self.executions:
            coords = execution[K_COORDS]
            x = coords.get(self.x) if self.x else 0
            if self.combo(coords) != combo or x is None:
                continue
            value = sig(metric(coords, phase_of(execution, phase)))
            if value is not None:
                points.append([x, value])
        return sorted(points)

    def style(self, index: int, phase: str) -> tuple[str, bool | str]:
        """Colour and dash: phase colours for one combination, else one
        categorical colour per combination with write dashed."""
        if len(self.combos) > 1:
            return category_color(index), lap_dash(index, phase == PHASE_WRITE)
        return model.PHASE_COLORS[phase], phase == PHASE_WRITE

    def name(self, combo: tuple, *words: str) -> str:
        """Series name: combination label plus words."""
        label = combo_label(dict(zip(self.others, combo)), self.others, IO_DIMS)
        return " ".join([label, *words]).strip()

    def metric_series(self, metric: Callable, part: Part) -> list[dict]:
        """One series per combination of ``part`` and phase."""
        out = []
        for number, index in enumerate(part.indices):
            combo = self.combos[index]
            for phase in self.phases:
                color, dash = self.style(number, phase)
                points = self.points(phase, combo, metric)
                out.append(series(self.name(combo, phase), color, points, dash))
        return out

    def figure(
        self,
        template: str,
        title: str,
        caption: str,
        spec: dict,
        small=True,
        tail="",
        part: Part = WHOLE,
    ):
        """A figure of this section; ``tail`` tells apart several of a
        template, ``part`` the charts of a split configuration list."""
        anchor = f"{self.anchor}-{template}{tail}{part.tail}"
        title = f"{title} — {self.title}{part.suffix}"
        return Figure(template, anchor, title, caption, spec, small)

    def x_axis(self) -> dict:
        """The sweep x axis (a single category without a sweep)."""
        if self.x:
            return sweep_axis(self.xlabel, self.xtype)
        label = config_label(self.executions[0][K_COORDS], IO_DIMS)
        return {"label": "configuration", "type": CATEGORY, "categories": [label]}


def _legend_note(sweep: _Sweep, part: Part) -> str:
    notes = []
    if len(sweep.phases) > 1:
        notes.append("Solid: read; dashed: write.")
    if len(sweep.combos) > 1:
        notes.append("Click legend entries to hide series.")
    if len(part.indices) > len(model.CATEGORICAL):
        notes.append("Colours repeat after ten configurations; the line style differs.")
    return (" " + " ".join(notes)) if notes else ""


def _bars(sweep: _Sweep, metric: Callable) -> list[dict]:
    execution = sweep.executions[0]
    return [
        {
            "name": phase,
            "color": model.PHASE_COLORS[phase],
            "values": [sig(metric(execution[K_COORDS], phase_of(execution, phase)))],
        }
        for phase in sweep.phases
    ]


def _rate_chart(sweep: _Sweep, metric: Callable, y: dict, part: Part) -> dict:
    if sweep.x:
        return line_spec(sweep.x_axis(), y, sweep.metric_series(metric, part))
    label = sweep.x_axis()["categories"]
    return bar_spec(label, "configuration", y, _bars(sweep, metric))


def throughput_figures(sweep: _Sweep) -> list[Figure]:
    """Throughput and IOPS vs the swept dimension. Throughput leads, full
    width, except for small IO sizes, where linear IOPS leads instead."""
    over = f"vs {sweep.xlabel}" if sweep.x else "at one configuration"
    iops_first = 0 < (sweep.shape.block or 0) <= SMALL_IO_BYTES
    if iops_first:
        iops_axis, iops_caption = axis("IOPS", min=0), "Aggregate IOPS."
    else:
        iops_axis, iops_caption = axis("IOPS", LOG), "Aggregate IOPS, log scale."
    figures = []
    for part in sweep.parts:
        note = _legend_note(sweep, part)
        throughput = sweep.figure(
            model.SWEEP_THROUGHPUT,
            f"Throughput {over}",
            "Aggregate GB/s (elbencho first-done)." + note,
            _rate_chart(sweep, gbs_metric, axis(GBS, fmt=FMT_GBS, min=0), part),
            small=iops_first,
            part=part,
        )
        iops = sweep.figure(
            model.SWEEP_IOPS,
            f"IOPS {over}",
            iops_caption + note,
            _rate_chart(sweep, _iops, iops_axis, part),
            small=not iops_first,
            part=part,
        )
        figures += [iops, throughput] if iops_first else [throughput, iops]
    return figures


def _efficiency(points: list) -> list:
    """Per-node rate as a percentage of the series' first (fewest-node) point."""
    points = [[x, y] for x, y in points if x]
    if not points or not points[0][1]:
        return []
    base = points[0][1] / points[0][0]
    return [[x, sig(100.0 * (y / x) / base, 3)] for x, y in points]


def _node_part_figures(sweep: _Sweep, part: Part) -> list[Figure]:
    throughput = sweep.metric_series(gbs_metric, part)
    efficiency = [
        {**item, "points": _efficiency(item["points"])} for item in throughput
    ]
    share = sweep.metric_series(share_metric, part)
    x = sweep.x_axis()
    return [
        sweep.figure(
            model.SWEEP_EFFICIENCY,
            "Scaling efficiency vs nodes",
            "GB/s per node relative to each series' run with the fewest nodes; "
            "100% is linear scaling.",
            line_spec(x, axis("% of linear", fmt="pct", min=0), efficiency),
            part=part,
        ),
        sweep.figure(
            model.SWEEP_SHARE,
            "Per-node share vs nodes",
            "Aggregate GB/s ÷ nodes: each node's share of the aggregate.",
            line_spec(x, axis("GB/s per node", fmt=FMT_GBS, min=0), share),
            part=part,
        ),
    ]


def node_figures(sweep: _Sweep) -> list[Figure]:
    """Scaling efficiency and per-node share, for node sweeps only."""
    if sweep.x != K_NODES:
        return []
    return [
        figure for part in sweep.parts for figure in _node_part_figures(sweep, part)
    ]


def _latency_line(sweep: _Sweep, index: int, number: int, phase: str, key: str) -> dict:
    """One percentile line of configuration ``index``, styled as series
    ``number`` of its chart; several configurations hide p50 until clicked."""
    combo = sweep.combos[index]
    points = sweep.points(phase, combo, latency_metric(key))
    name = sweep.name(combo, key)
    if len(sweep.combos) == 1:
        colors = {P50_KEY: model.P50, P99_KEY: model.P99}
        return series(name, colors[key], points, key == P50_KEY)
    dash = lap_dash(number, key == P50_KEY)
    line = series(name, category_color(number), points, dash)
    if key == P50_KEY:
        line["hidden"] = True
    return line


def _latency_series(sweep: _Sweep, phase: str, part: Part) -> list[dict]:
    return [
        _latency_line(sweep, index, number, phase, key)
        for number, index in enumerate(part.indices)
        for key in _LATENCY_KEYS
    ]


def latency_figures(sweep: _Sweep) -> list[Figure]:
    """p50 and p99 per-IO latency vs the swept dimension, one chart per phase
    so a configuration's write and read lines never look alike."""
    if len(sweep.combos) > 1:
        caption = (
            "p99 solid; p50 dashed and hidden until its legend entry is "
            "clicked; one colour per configuration."
        )
    else:
        caption = "p99 (red) solid and p50 (green) dashed per-IO latency."
    over = f"vs {sweep.xlabel}" if sweep.x else "at one configuration"
    figures = []
    for phase in sweep.phases:
        for part in sweep.parts:
            lines = _latency_series(sweep, phase, part)
            if has_points(lines):
                spec = line_spec(sweep.x_axis(), axis(LATENCY_MS, LOG, FMT_MS), lines)
                figures.append(
                    sweep.figure(
                        model.SWEEP_LATENCY,
                        f"{phase.capitalize()} latency percentiles {over}",
                        caption + " Log scale.",
                        spec,
                        tail=f"-{phase}",
                        part=part,
                    )
                )
    return figures


def best_by(sweep: _Sweep, phase: str, metric: Callable):
    """(execution, phase) with the highest metric, or None."""
    best, best_value = None, None
    for execution in sweep.executions:
        data = phase_of(execution, phase)
        value = finite(metric(execution[K_COORDS], data)) if data else None
        if value is not None and (best_value is None or value > best_value):
            best, best_value = (execution, data), value
    return best


def histogram_figure(sweep: _Sweep) -> Figure | None:
    """Latency histogram at each phase's peak-throughput configuration."""
    lines = []
    for phase in sweep.phases:
        best = best_by(sweep, phase, gbs_metric)
        if not best:
            continue
        execution, data = best
        histogram = data.get("histogram_us") or {}
        points = [
            [sig(bucket / US_PER_MS, 3), count]
            for bucket, count in zip(
                histogram.get("buckets_us") or [], histogram.get("counts") or []
            )
            if count
        ]
        label = config_label(execution[K_COORDS], IO_DIMS)
        lines.append(
            series(
                f"{phase} @ {label}",
                model.PHASE_COLORS[phase],
                points,
                phase == PHASE_WRITE,
            )
        )
    if not has_points(lines):
        return None
    spec = line_spec(axis(LATENCY_MS, LOG), axis("IOs", LOG), lines, markers=False)
    return sweep.figure(
        model.SWEEP_HISTOGRAM,
        "Latency histogram at peak throughput",
        "elbencho latency histogram, log–log: IO count per latency bucket.",
        spec,
    )


_RESULT_COLUMNS = (
    Column(K_NODES),
    Column("threads"),
    Column("IO depth"),
    Column("IOPS", best=MAX),
    Column(GBS, best=MAX),
    Column("GiB/s", extra=True),
    Column("GB/s per node", best=MAX, digits=3),
    Column("last GB/s", extra=True),
    Column("min lat ms", extra=True, digits=3),
    Column("avg lat ms", best=MIN, digits=3),
    Column("max lat ms", extra=True, digits=3),
    Column("p50 ms", best=MIN, digits=3),
    Column("p75 ms", extra=True, digits=3),
    Column("p99 ms", best=MIN, digits=3),
    Column("duration s", extra=True, digits=3),
    Column(_RUN, numeric=False, extra=True),
)


def _run_cell(sweep: _Sweep, execution: dict):
    eid = sweep.eid(execution)
    anchor = sweep.runs.get(id(execution))
    return Link(eid, f"#{anchor}") if anchor else eid


def _result_row(sweep: _Sweep, execution: dict, data: dict) -> list:
    coords = execution[K_COORDS]
    first = data.get("elbencho_first") or {}
    latency = data.get("latency_us") or {}
    rate = first.get("bytes_per_s")
    last = (data.get("elbencho_last") or {}).get("bytes_per_s")

    def ms(key):
        return sig(scaled(latency.get(key), US_PER_MS), 3)

    return [
        coords.get(K_NODES),
        coords.get(K_THREADS),
        coords.get(K_DEPTH),
        sig(first.get("ops_per_s")),
        sig(scaled(rate, GB)),
        sig(scaled(rate, GIB)),
        sig(share_metric(coords, data), 3),
        sig(scaled(last, GB)),
        ms(MIN),
        ms("avg"),
        ms(MAX),
        ms(P50_KEY),
        ms("p75"),
        ms(P99_KEY),
        sig(scaled(first.get("elapsed_ms"), MS_PER_S), 3),
        _run_cell(sweep, execution),
    ]


def results_tables(sweep: _Sweep) -> list[Table]:
    """A table per phase (write, then read), one row per execution."""
    tables = []
    for phase in sweep.phases:
        rows = [
            _result_row(sweep, execution, data)
            for execution in sweep.executions
            if (data := phase_of(execution, phase))
        ]
        if rows:
            tables.append(
                _without_fixed_nodes(
                    sweep,
                    Table(
                        model.RESULTS_TABLE,
                        f"{sweep.anchor}-{model.RESULTS_TABLE}-{phase}",
                        f"{phase.capitalize()} results per configuration — "
                        f"{sweep.title}. {_BEST_NOTE}",
                        list(_RESULT_COLUMNS),
                        rows,
                    ),
                )
            )
    return tables


def _without_fixed_nodes(sweep: _Sweep, table: Table) -> Table:
    """Drop the nodes column when the section title names the node count."""
    if sweep.nodes:
        drop_column(table.columns, table.rows, K_NODES)
    return table


_FAIRNESS_COLUMNS = (
    Column(K_NODES),
    Column("threads"),
    Column("IO depth"),
    Column("clients"),
    Column("steady s", numeric=False),
    Column("Jain p50 5 s", best=MAX),
    Column("Jain min 5 s", best=MAX),
    Column("Jain p50 30 s", extra=True),
    Column("Jain min 30 s", extra=True),
    Column("slowest ÷ median p10", best=MAX, digits=3),
    Column("slow clients", numeric=False),
    Column("lat p50 ms", digits=3),
    Column("lat p90 ms", extra=True, digits=3),
    Column("lat max ÷ p50", digits=3),
    Column(_RUN, numeric=False, extra=True),
)


# Columns measured over the steady window: "Jain p50 5 s" to "lat max ÷ p50".
_STEADY_METRICS = 9


def _steady_text(live: dict) -> str:
    span = steady_span(live)
    if span is None:
        return f"none ({steady_missing_text(live)})"
    return f"{span[0] / MS_PER_S:.1f}–{span[1] / MS_PER_S:.1f}"


def _slow_text(live: dict) -> str:
    dist = live.get("distribution") or {}
    persistent = dist.get("persistent_clients")
    if persistent is None:
        return NO_VALUE
    transient = dist.get("transient_clients") or 0
    return f"{persistent}+{transient} of {live.get('clients') or 0}"


def _fairness_row(sweep: _Sweep, execution: dict, domain: dict) -> list:
    coords = execution[K_COORDS]
    live = domain["live"]
    head = [
        coords.get(K_NODES),
        coords.get(K_THREADS),
        coords.get(K_DEPTH),
        live.get("clients"),
        _steady_text(live),
    ]
    if steady_span(live) is None:
        # Everything between is measured over the steady window.
        return head + [NO_VALUE] * _STEADY_METRICS + [_run_cell(sweep, execution)]
    windows = live.get("fairness") or {}
    latency = live.get("latency") or {}

    def jain(window, key):
        return sig(((windows.get(window) or {}).get("jain") or {}).get(key), 4)

    return head + [
        jain("5s", P50_KEY),
        jain("5s", MIN),
        jain("30s", P50_KEY),
        jain("30s", MIN),
        sig((windows.get("5s") or {}).get("min_over_median_p10"), 3),
        _slow_text(live),
        sig(scaled(latency.get("p50_us"), US_PER_MS), 3),
        sig(scaled(latency.get("p90_us"), US_PER_MS), 3),
        sig(latency.get("max_over_p50"), 3),
        _run_cell(sweep, execution),
    ]


def fairness_tables(sweep: _Sweep) -> list[Table]:
    """Steady-window fairness: a table per live domain (write, then read),
    one row per execution."""
    rows: dict[str, list] = {}
    order: dict[str, tuple] = {}
    for execution in sweep.executions:
        for domain in live_domains(execution, sweep.ctx.live_detail):
            label = domain_label(domain)
            order.setdefault(label, domain_order(domain))
            rows.setdefault(label, []).append(_fairness_row(sweep, execution, domain))
    return [
        _without_fixed_nodes(
            sweep,
            Table(
                model.FAIRNESS_TABLE,
                f"{sweep.anchor}-{model.FAIRNESS_TABLE}-{slug(label)}",
                f"{label.capitalize()} live fairness, steady window only — "
                f"{sweep.title}. Latency is the fleet mean per interval, not a "
                "per-IO percentile.",
                list(_FAIRNESS_COLUMNS),
                domain_rows,
            ),
        )
        for label, domain_rows in sorted(rows.items(), key=lambda item: order[item[0]])
    ]


def _peak(sweep: _Sweep, phase: str, metric: Callable) -> dict | None:
    best = best_by(sweep, phase, metric)
    if not best:
        return None
    execution, data = best
    coords = execution[K_COORDS]
    block, access = sweep.shape.phase_size(phase)
    access = _ACCESS_TITLE.get(access or "", "")
    return {
        "test": f"{access} {phase} · {sweep.shape.mode_title()}".strip(),
        "io_size": size_text(block),
        K_NODES: coords.get(K_NODES),
        "threads": coords.get(K_THREADS),
        "depth": coords.get(K_DEPTH),
        # Unrounded, so the peak across sections is the true one.
        "gbs": gbs_metric(coords, data),
        "iops": _iops(coords, data),
        "p99_ms": sig(latency_metric(P99_KEY)(coords, data), 3),
        "config": config_label(coords, IO_DIMS),
    }


def _peaks(sweep: _Sweep) -> dict:
    peaks = {}
    for phase in sweep.phases:
        for name, metric in (("gbs", gbs_metric), ("iops", _iops)):
            record = _peak(sweep, phase, metric)
            if record:
                peaks[(phase, name)] = record
    return peaks


def _runs(sweep: _Sweep) -> list:
    runs = []
    for execution in sweep.executions:
        label = config_label(execution[K_COORDS], IO_DIMS)
        run = build_run(
            sweep.ctx, sweep.anchor, (sweep.eid(execution), label), execution
        )
        if run:
            sweep.runs[id(execution)] = run.anchor
            runs.append(run)
    return runs


def build_section(ctx: GroupContext, shape: IoShape, executions: list[dict]) -> Section:
    """The section of one group and shape."""
    sweep = _Sweep(ctx, shape, executions)
    section = Section(
        anchor=sweep.anchor,
        title=sweep.title,
        target=ctx.target,
        group_id=ctx.group_id,
        document=ctx.document,
        settings=[],
        executions=len(sweep.executions),
    )
    section.runs = _runs(sweep)
    section.figures = throughput_figures(sweep) + node_figures(sweep)
    section.figures += latency_figures(sweep)
    histogram = histogram_figure(sweep)
    if histogram:
        section.figures.append(histogram)
    section.tables = results_tables(sweep) + fairness_tables(sweep)
    section.best = _peaks(sweep)
    return section


def build_io_sections(ctx: GroupContext, executions: list[dict]) -> list[Section]:
    """Every IO section of one group."""
    return [build_section(ctx, shape, items) for shape, items in io_shapes(executions)]
