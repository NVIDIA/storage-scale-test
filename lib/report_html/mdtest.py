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

"""Metadata (mdtest) sections: rates and latency vs the swept dimension.

Values come from ``results.aggregate``, which uses complete iterations only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from lib.report_html import model
from lib.report_html.common import (
    CATEGORY,
    FMT_MS,
    K_COORDS,
    K_NODES,
    LATENCY_MS,
    LOG,
    MD_DIMS,
    MIN,
    MS_PER_S,
    NO_VALUE,
    P50_KEY,
    P99_KEY,
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
    normalized_text,
    results_of,
    series,
    sort_executions,
    sweep_axes,
    sweep_axis,
)
from lib.report_html.model import Column, Figure, Section, Table, category_color
from lib.report_html.reduce import finite, scaled, sig

MD_OPS = ("create", "stat", "delete")
MD_COLORS = {
    "create": model.CATEGORICAL[5],
    "stat": model.CATEGORICAL[3],
    "delete": model.CATEGORICAL[7],
}
OPS_PER_S = "ops/s"


def aggregate_phase(execution: dict, op: str) -> dict | None:
    """The aggregate entry of one metadata operation."""
    aggregate = results_of(execution).get("aggregate") or {}
    for phase in aggregate.get("phases") or []:
        if phase.get("phase") == op:
            return phase
    return None


def md_rate(execution: dict, op: str):
    """Mean ops/s of one metadata operation (None unless finite)."""
    rates = (aggregate_phase(execution, op) or {}).get("ops_per_s") or {}
    return finite(rates.get("mean"))


def md_latency_ms(execution: dict, op: str, key: str):
    """One latency percentile of a metadata operation, in ms."""
    latency = (aggregate_phase(execution, op) or {}).get("latency_us") or {}
    return scaled(latency.get(key), US_PER_MS)


@dataclass(frozen=True)
class MdShape:
    """Tree shape shared by a metadata section."""

    branch_factor: int | None
    items_per_dir: int | None

    def slug(self) -> str:
        """Anchor part, e.g. ``md-b4-i1000``."""
        return f"md-b{self.branch_factor}-i{self.items_per_dir}"

    def title(self) -> str:
        """Display title."""
        return (
            f"Metadata · branch factor {self.branch_factor} · "
            f"{self.items_per_dir} items/dir"
        )

    def sort_key(self) -> tuple:
        """Branch factor, then items per directory."""
        return (self.branch_factor or 0, self.items_per_dir or 0)


class _MdSweep:  # pylint: disable=too-many-instance-attributes
    """One metadata section's executions and sweep layout."""

    def __init__(self, ctx: GroupContext, shape: MdShape, executions: list[dict]):
        self.executions = sort_executions(executions)
        self.ids = execution_ids(self.executions)
        coords = [e[K_COORDS] for e in self.executions]
        self.x, self.others = sweep_axes(coords, MD_DIMS)
        self.combos = sorted({self.combo(c) for c in coords}, key=none_last)
        self.parts = config_parts(len(self.combos))
        self.ops = [op for op in MD_OPS if any(md_rate(e, op) for e in self.executions)]
        self.anchor = ctx.anchor(shape.slug())
        self.nodes = node_title(coords)
        self.title = " · ".join(word for word in (shape.title(), self.nodes) if word)
        self.xlabel = dim_label(self.x, MD_DIMS)
        self.xtype = axis_type(c.get(self.x) for c in coords) if self.x else CATEGORY

    def combo(self, coords: dict) -> tuple:
        """Values of the non-x varying dimensions."""
        return tuple(coords.get(name) for name in self.others)

    def points(self, combo: tuple, value) -> list:
        """Sorted [x, value] points for executions of one combination;
        executions without an x value or a finite value are left out."""
        points = []
        for execution in self.executions:
            coords = execution[K_COORDS]
            x = coords.get(self.x) if self.x else 0
            if self.combo(coords) != combo or x is None:
                continue
            number = sig(value(execution))
            if number is not None:
                points.append([x, number])
        return sorted(points)

    def name(self, combo: tuple, *words: str) -> str:
        """Series name: combination label plus words."""
        label = combo_label(dict(zip(self.others, combo)), self.others, MD_DIMS)
        return " ".join([label, *words]).strip()

    def x_axis(self) -> dict:
        """The sweep x axis (a single category without a sweep)."""
        if self.x:
            return sweep_axis(self.xlabel, self.xtype)
        label = config_label(self.executions[0][K_COORDS], MD_DIMS)
        return {"label": "configuration", "type": CATEGORY, "categories": [label]}

    def figure(
        self,
        template: str,
        title: str,
        caption: str,
        spec: dict,
        tail="",
        part: Part = WHOLE,
    ) -> Figure:
        """A figure of this section; ``tail`` tells apart several of a
        template, ``part`` the charts of a split configuration list."""
        anchor = f"{self.anchor}-{template}{tail}{part.tail}"
        return Figure(template, anchor, title + part.suffix, caption, spec, True)

    def per_op(self) -> bool:
        """Whether charts are split per operation (several combinations take
        the colours, so operations can no longer)."""
        return len(self.combos) > 1


_RATES_CAPTION = "Mean ops/s over complete iterations, log scale."


def _rates_bars(sweep: _MdSweep) -> Figure:
    execution = sweep.executions[0]
    bars = [
        {"name": op, "color": MD_COLORS[op], "values": [sig(md_rate(execution, op))]}
        for op in sweep.ops
    ]
    spec = bar_spec(
        sweep.x_axis()["categories"], "configuration", axis(OPS_PER_S, LOG), bars
    )
    return sweep.figure(
        model.MD_RATES, "Metadata ops/s at one configuration", _RATES_CAPTION, spec
    )


def _rates_figures(sweep: _MdSweep) -> list[Figure]:
    """One chart with a line per operation, or per operation a chart with a
    line per combination."""
    if not sweep.x:
        return [_rates_bars(sweep)]
    y = axis(OPS_PER_S, LOG)
    if not sweep.per_op():
        combo = sweep.combos[0]
        lines = [
            series(
                sweep.name(combo, op),
                MD_COLORS[op],
                sweep.points(combo, lambda e, op=op: md_rate(e, op)),
            )
            for op in sweep.ops
        ]
        title = f"Metadata ops/s vs {sweep.xlabel}"
        return [
            sweep.figure(
                model.MD_RATES,
                title,
                _RATES_CAPTION,
                line_spec(sweep.x_axis(), y, lines),
            )
        ]
    return [
        _op_rates_figure(sweep, op, part, y) for op in sweep.ops for part in sweep.parts
    ]


def _op_rates_figure(sweep: _MdSweep, op: str, part: Part, y: dict) -> Figure:
    """One operation's rates, a line per combination of ``part``."""
    lines = [
        series(
            sweep.name(sweep.combos[index]),
            category_color(number),
            sweep.points(sweep.combos[index], lambda e: md_rate(e, op)),
            lap_dash(number),
        )
        for number, index in enumerate(part.indices)
    ]
    return sweep.figure(
        model.MD_RATES,
        f"{op.capitalize()} metadata ops/s vs {sweep.xlabel}",
        _RATES_CAPTION + " One colour per configuration.",
        line_spec(sweep.x_axis(), y, lines),
        tail=f"-{op}",
        part=part,
    )


def _latency_line(sweep: _MdSweep, index: int, number: int, op: str, key: str) -> dict:
    """One percentile line of combination ``index``, styled as series
    ``number`` of its chart."""
    combo = sweep.combos[index]
    points = sweep.points(combo, lambda e: md_latency_ms(e, op, key))
    if not sweep.per_op():
        return series(sweep.name(combo, op, key), MD_COLORS[op], points, key == P50_KEY)
    dash = lap_dash(number, key == P50_KEY)
    line = series(sweep.name(combo, key), category_color(number), points, dash)
    if key == P50_KEY:
        line["hidden"] = True
    return line


def _latency_lines(sweep: _MdSweep, ops: Sequence[str], part: Part) -> list[dict]:
    """p50 (dashed) and p99 lines per combination and operation; several
    combinations take the colours, with p50 hidden until clicked."""
    return [
        _latency_line(sweep, index, number, op, key)
        for number, index in enumerate(part.indices)
        for op in ops
        for key in (P50_KEY, P99_KEY)
    ]


def _latency_figures(sweep: _MdSweep) -> list[Figure]:
    over = f"vs {sweep.xlabel}" if sweep.x else "at one configuration"
    caption = "p99 solid, p50 dashed, pooled over complete iterations; log scale."
    groups = [[op] for op in sweep.ops] if sweep.per_op() else [list(sweep.ops)]
    figures = []
    for ops in groups:
        for part in sweep.parts:
            lines = _latency_lines(sweep, ops, part)
            if not has_points(lines):
                continue
            spec = line_spec(sweep.x_axis(), axis(LATENCY_MS, LOG, FMT_MS), lines)
            if sweep.per_op():
                title = f"{ops[0].capitalize()} metadata latency {over}"
                figures.append(
                    sweep.figure(
                        model.MD_LATENCY,
                        title,
                        caption + " One colour per configuration; p50 hidden until "
                        "its legend entry is clicked.",
                        spec,
                        tail=f"-{ops[0]}",
                        part=part,
                    )
                )
            else:
                title = f"Metadata latency {over}"
                figures.append(sweep.figure(model.MD_LATENCY, title, caption, spec))
    return figures


_COLUMNS = (
    Column(K_NODES),
    Column("tasks"),
    Column("iterations", numeric=False),
    Column(OPS_PER_S, best="max"),
    Column("± sd", digits=3),
    Column("min ops/s", extra=True),
    Column("max ops/s", extra=True),
    Column("p50 ms", best=MIN, digits=3),
    Column("p90 ms", extra=True, digits=3),
    Column("p99 ms", best=MIN, digits=3),
    Column("p100 ms", extra=True, digits=3),
    Column("phase s", extra=True, digits=3),
    Column("run", numeric=False, extra=True),
)


def _row(sweep: _MdSweep, execution: dict, op: str) -> list:
    coords = execution[K_COORDS]
    aggregate = results_of(execution).get("aggregate") or {}
    phase = aggregate_phase(execution, op) or {}
    rates = phase.get("ops_per_s") or {}
    used, expected = aggregate.get("iterations_used"), aggregate.get(
        "iterations_expected"
    )
    return [
        coords.get(K_NODES),
        coords.get("tasks_per_node"),
        NO_VALUE if used is None else f"{used} of {expected}",
        sig(rates.get("mean")),
        sig(rates.get("stddev"), 3),
        sig(rates.get(MIN)),
        sig(rates.get("max")),
        sig(md_latency_ms(execution, op, P50_KEY), 3),
        sig(md_latency_ms(execution, op, "p90"), 3),
        sig(md_latency_ms(execution, op, P99_KEY), 3),
        sig(md_latency_ms(execution, op, "p100"), 3),
        sig(scaled(phase.get("mean_elapsed_ms"), MS_PER_S), 3),
        sweep.ids[id(execution)],
    ]


def _tables(sweep: _MdSweep) -> list[Table]:
    """A table per operation (create, stat, delete), one row per execution."""
    tables = []
    for op in sweep.ops:
        rows = [
            _row(sweep, execution, op)
            for execution in sweep.executions
            if aggregate_phase(execution, op)
        ]
        columns = list(_COLUMNS)
        if sweep.nodes:
            drop_column(columns, rows, K_NODES)
        tables.append(
            Table(
                model.RESULTS_TABLE,
                f"{sweep.anchor}-{model.RESULTS_TABLE}-{op}",
                f"{op.capitalize()} results per configuration — {sweep.title}. "
                "Best value in bold.",
                columns,
                rows,
            )
        )
    return tables


def _peaks(sweep: _MdSweep, normalize_to: int | None) -> dict:
    peaks = {}
    for op in sweep.ops:
        rated = [e for e in sweep.executions if md_rate(e, op) is not None]
        if not rated:
            continue
        best = max(rated, key=lambda e, op=op: md_rate(e, op))
        coords = best[K_COORDS]
        peaks[(op, "ops")] = {
            "test": f"Metadata {op}",
            "io_size": NO_VALUE,
            K_NODES: coords.get(K_NODES),
            "threads": coords.get("tasks_per_node"),
            "depth": None,
            "ops": md_rate(best, op),
            "p99_ms": sig(md_latency_ms(best, op, P99_KEY), 3),
            "config": config_label(coords, MD_DIMS),
            "note": normalized_text(normalize_to) if normalize_to else "",
        }
    return peaks


def md_shapes(executions: list[dict]) -> list[tuple[MdShape, list[dict]]]:
    """Executions with aggregate results, split by tree shape."""
    shapes: dict[MdShape, list[dict]] = {}
    for execution in executions:
        coords = execution.get(K_COORDS)
        if coords and results_of(execution).get("aggregate"):
            key = MdShape(coords.get("branch_factor"), coords.get("items_per_dir"))
            shapes.setdefault(key, []).append(execution)
    return sorted(shapes.items(), key=lambda item: item[0].sort_key())


def build_md_sections(ctx: GroupContext, executions: list[dict]) -> list[Section]:
    """Every metadata section of one group."""
    sections = []
    for shape, items in md_shapes(executions):
        sweep = _MdSweep(ctx, shape, items)
        section = Section(
            anchor=sweep.anchor,
            title=sweep.title,
            target=ctx.target,
            group_id=ctx.group_id,
            document=ctx.document,
            settings=[],
            executions=len(sweep.executions),
        )
        if sweep.ops:
            section.figures += _rates_figures(sweep)
        section.figures += _latency_figures(sweep)
        section.tables.extend(_tables(sweep))
        section.best = _peaks(sweep, ctx.normalize_to)
        if ctx.normalize_to:
            section.notes.append(
                f"Ops/s are {normalized_text(ctx.normalize_to)} (× "
                f"{ctx.normalize_to} ÷ nodes), as in the text report; latency is not."
            )
        sections.append(section)
    return sections
