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

"""Render the report model as one self-contained HTML page.

Tables are plain HTML and work without JavaScript. Chart specs travel in one
``<script type="application/json">`` blob with ``<`` written as ``\\u003c``;
``assets/report.js`` draws them with Chart.js, the page's only external
reference. Every document string is HTML-escaped here.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
import re
from typing import Any, Sequence

from lib.report_html import model
from lib.report_html.common import NO_VALUE
from lib.report_html.model import (
    Card,
    Figure,
    Link,
    Measure,
    Provenance,
    Report,
    RunDetail,
    RunDomain,
    Section,
    Table,
    slug,
)
from lib.report_html.reduce import sig

CHART_JS_URL = "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.js"
CHART_JS_INTEGRITY = (
    "sha384-dug+JxfBvklEQdJ4AYuBBAIScUz0bVN73xpy273gcAwHjb3qI0fXmuYNaNfdyYJG"
)
ASSET_DIR = Path(__file__).resolve().parent / "assets"
CHART_DATA_ID = "report-charts"
_LEADING_COMMENT = re.compile(r"\A\s*/\*.*?\*/\s*", re.DOTALL)
_SUMMARY = "summary"
_COMPARISONS = "comparisons"
_SECTION_CLOSE = "</section>"


def esc(value: Any) -> str:
    """HTML-escape any value (None is empty)."""
    return html.escape("" if value is None else str(value), quote=True)


def asset(name: str) -> str:
    """An asset's text without its leading license comment."""
    text = (ASSET_DIR / name).read_text(encoding="utf-8")
    return _LEADING_COMMENT.sub("", text, count=1)


def number_text(value: Any, digits: int = 4) -> str:
    """Table and card number text: thousands separators, else ``digits``."""
    if value is None:
        return NO_VALUE
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{sig(value, digits):g}"


def exact_text(value: Any) -> str:
    """A number in full, with thousands separators: ``109,235,641.23``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return number_text(value)
    if float(value).is_integer():
        return f"{value:,.0f}"
    return f"{value:,}"


def card_number_text(value: Any) -> str:
    """Card number text: millions as ``109M`` or ``14.16M``, so it fits."""
    if isinstance(value, (int, float)) and abs(value) >= 1e6:
        return f"{sig(value / 1e6, 4):g}M"
    return number_text(value, 3)


def permalink(anchor: str, template: str | None = None) -> str:
    """Hover-only "#" link; shows the template name when there is one."""
    data = f' data-template="{esc(template)}"' if template else ""
    return f'<a class="pl" href="#{esc(anchor)}"{data} title="Link to this part">#</a>'


def _badge(text: Any) -> str:
    return f'<span class="badge b-{esc(slug(text))}">{esc(text)}</span>'


class _Numbering:
    """Chapter, figure and table numbers in display order."""

    def __init__(self) -> None:
        self.chapter = 0
        self.figures = 0
        self.tables = 0

    def next_chapter(self) -> int:
        """Start the next chapter."""
        self.chapter += 1
        self.figures = self.tables = 0
        return self.chapter

    def figure(self) -> str:
        """Label of the next figure."""
        self.figures += 1
        return f"Figure {self.chapter}.{self.figures}"

    def table(self) -> str:
        """Label of the next table."""
        self.tables += 1
        return f"Table {self.chapter}.{self.tables}"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _row_group(table: Table, row: list):
    return row[table.best_by] if table.best_by is not None else None


def _best_values(table: Table) -> dict:
    """(group, column) -> best value, for columns that highlight one."""
    best: dict = {}
    for index, column in enumerate(table.columns):
        if not column.best:
            continue
        pick = max if column.best == "max" else min
        groups: dict = {}
        for row in table.rows:
            if _is_number(row[index]):
                groups.setdefault(_row_group(table, row), []).append(row[index])
        for key, values in groups.items():
            best[(key, index)] = pick(values)
    return best


def _cell(value: Any, column: model.Column, best: bool) -> str:
    classes = [] if column.numeric else ["txt"]
    if column.extra:
        classes.append("x")
    if best:
        classes.append("best")
    attrs = f' class="{" ".join(classes)}"' if classes else ""
    if isinstance(value, Link):
        return f'<td{attrs}><a href="{esc(value.href)}">{esc(value.text)}</a></td>'
    if isinstance(value, Measure):
        sort = "" if value.value is None else f' data-v="{value.value!r}"'
        return f"<td{attrs}{sort}>{esc(value.text)}</td>"
    if _is_number(value):
        text = number_text(value, column.digits)
        return f'<td{attrs} data-v="{value!r}" title="{value!r}">{esc(text)}</td>'
    if column.badge and value not in (None, "", NO_VALUE):
        return f"<td{attrs}>{_badge(value)}</td>"
    return f"<td{attrs}>{esc(NO_VALUE if value is None else value)}</td>"


def _head(columns: list[model.Column]) -> str:
    cells = []
    for column in columns:
        classes = ([] if column.numeric else ["txt"]) + (["x"] if column.extra else [])
        attrs = f' class="{" ".join(classes)}"' if classes else ""
        cells.append(f'<th{attrs} scope="col">{esc(column.label)}</th>')
    return "<thead><tr>" + "".join(cells) + "</tr></thead>"


class _Page:
    """Accumulates HTML and chart specs."""

    def __init__(self) -> None:
        self.out: list[str] = []
        self.specs: dict[str, dict] = {}
        self.numbers = _Numbering()

    def add(self, *parts: str) -> None:
        """Append markup."""
        self.out.extend(parts)

    def table(self, table: Table) -> None:
        """A numbered table with optional show-all-columns toggle."""
        label = self.numbers.table()
        best = _best_values(table)
        extra = any(c.extra for c in table.columns)
        toggle = (
            '<button type="button" class="cols">Show all columns</button>'
            if extra
            else ""
        )
        self.add(
            f'<div class="tbl" id="{esc(table.anchor)}">',
            f'<div class="tbar"><p class="cap"><span class="num">{label}</span> '
            f"{esc(table.caption)} {permalink(table.anchor, table.template)}</p>",
            f"{toggle}</div>",
            f'<div class="scroll"><table class="data{" lean" if extra else ""}">',
            _head(table.columns),
            "<tbody>",
        )
        for row in table.rows:
            key = _row_group(table, row)
            cells = [
                _cell(value, column, (key, i) in best and value == best[(key, i)])
                for i, (value, column) in enumerate(zip(row, table.columns))
            ]
            self.add("<tr>", *cells, "</tr>")
        self.add("</tbody></table></div></div>")

    def figure(self, figure: Figure) -> None:
        """A numbered chart box; its spec joins the data blob."""
        label = self.numbers.figure()
        self.specs[figure.anchor] = figure.spec
        anchor, title = esc(figure.anchor), esc(figure.title)
        canvas = (
            f'<canvas data-chart="{anchor}" role="img" aria-label="{title}"></canvas>'
        )
        if figure.spec.get("kind") == "heatmap":
            rows = len(figure.spec["heatmap"]["rows"])
            height = max(
                220, rows * (14 if rows <= 16 else 8 if rows <= 40 else 6) + 80
            )
            plot = (
                f'<div class="plot heat" style="height:{height}px">{canvas}'
                '<div class="heat-tip" hidden></div></div>'
            )
        else:
            plot = f'<div class="plot">{canvas}</div>'
        self.add(
            f'<figure class="fig" id="{anchor}">',
            f'<figcaption><span class="num">{label}</span> {title} '
            f"{permalink(figure.anchor, figure.template)}</figcaption>",
            plot,
            f'<p class="cap">{esc(figure.caption)}</p></figure>',
        )

    def grid(self, figures: list[Figure]) -> None:
        """Small figures side by side."""
        if figures:
            self.add('<div class="grid">')
            for figure in figures:
                self.figure(figure)
            self.add("</div>")

    def notes(self, notes: list[str]) -> None:
        """Highlighted notes."""
        for note in notes:
            self.add(f'<p class="note">{esc(note)}</p>')


def _card(card: Card) -> str:
    subtitle = f'<span class="sub">{esc(card.subtitle)}</span>' if card.subtitle else ""
    return (
        f'<a class="card" href="{esc(card.href)}"><span class="label">'
        f'{esc(card.label)}</span><span class="value" title="{esc(exact_text(card.value))}">'
        f"{esc(card_number_text(card.value))}<small>{esc(card.unit)}</small></span>"
        f'{subtitle}<span class="detail">{esc(card.detail)}</span></a>'
    )


def _summary(page: _Page, report: Report) -> None:
    number = page.numbers.next_chapter()
    page.add(
        f'<section id="{_SUMMARY}"><header class="top">',
        f"<h1>{esc(report.title)}</h1>",
        f'<p class="lede">{esc(report.subtitle)}</p>',
    )
    if report.sources:
        page.add(_fold(f"Sources ({len(report.sources)})", report.sources, "sources"))
    if report.tool:
        page.add(f'<p class="meta">{esc(report.tool)}</p>')
    page.add("</header>")
    page.add(
        f'<h2 id="{model.HEADER_CARDS}">{number}. Summary '
        f"{permalink(model.HEADER_CARDS, model.HEADER_CARDS)}</h2>"
    )
    if report.cards:
        page.add('<div class="cards">', *(_card(c) for c in report.cards), "</div>")
    page.add(
        '<p class="hint">Throughput is elbencho first-done aggregate GB/s '
        "(10<sup>9</sup> bytes/s), as in the text report.</p>"
    )
    if report.tldr.rows:
        page.table(report.tldr)
    page.add(_SECTION_CLOSE)


def _comparisons(page: _Page, report: Report) -> None:
    if not report.comparisons:
        return
    number = page.numbers.next_chapter()
    page.add(
        f'<section id="{_COMPARISONS}"><h2>{number}. Comparisons '
        f"{permalink(_COMPARISONS)}</h2>"
    )
    for comparison in report.comparisons:
        links = ", ".join(
            f'<a href="{esc(link.href)}">{esc(link.text)}</a>'
            for link in comparison.members
        )
        page.add(
            f'<div class="cmp" id="{esc(comparison.anchor)}">',
            f"<h3>{esc(comparison.title)} {permalink(comparison.anchor)}</h3>",
            f'<p class="meta">{esc(comparison.caption)} {links}</p>',
        )
        page.grid(comparison.figures)
        page.add("</div>")
    page.add(_SECTION_CLOSE)


def _settings(section: Section) -> str:
    rows = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in section.settings)
    text = ""
    if section.settings_text:
        text = f'<pre class="settings-text">{esc(section.settings_text)}</pre>'
    if not rows and not text:
        return ""
    return (
        '<details class="settings"><summary>Settings</summary>'
        f"<dl>{rows}</dl>{text}</details>"
    )


def _domain(page: _Page, domain: RunDomain) -> None:
    page.add(
        f'<h4 id="{esc(domain.anchor)}">{_badge(domain.phase)} '
        f"{esc(domain.heading)} {permalink(domain.anchor)}</h4>"
    )
    page.notes(domain.notes)
    page.grid([f for f in domain.figures if f.small])
    for figure in domain.figures:
        if not figure.small:
            page.figure(figure)
    if domain.table:
        page.table(domain.table)


def _run(page: _Page, run: RunDetail) -> None:
    chips = "".join(
        f'{_badge(phase)}<span class="chip">{esc(text)}</span>'
        for phase, text in run.chips
    )
    page.add(
        f'<details class="run" id="{esc(run.anchor)}"><summary>'
        f'<span class="rid">run {esc(run.execution)}</span>'
        f'<span class="cfg">{esc(run.label)}</span>{chips}</summary>'
        '<div class="run-body">'
    )
    for domain in run.domains:
        _domain(page, domain)
    page.add("</div></details>")


def _section(page: _Page, section: Section) -> None:
    number = page.numbers.next_chapter()
    anchor = esc(section.anchor)
    page.add(
        f'<section class="group" id="{anchor}">',
        f"<h2>{number}. {esc(section.target)} — {esc(section.title)} "
        f'<span class="meta">group {esc(section.group_id)} · '
        f"{section.executions} execution{'' if section.executions == 1 else 's'}"
        f"</span> {permalink(section.anchor)}</h2>",
        _settings(section),
    )
    page.notes(section.notes)
    # Charts first, then the tables together (per phase or operation).
    for figure in section.figures:
        if not figure.small:
            page.figure(figure)
    page.grid([f for f in section.figures if f.small])
    for table in section.tables:
        page.table(table)
    if section.runs:
        page.add(
            f'<div class="runs" id="{anchor}-runs"><div class="runbar">'
            f'<h3>Per-run live detail <span class="meta">{len(section.runs)} '
            "runs</span></h3>"
            '<span><button type="button" data-runs="open">Expand all</button> '
            '<button type="button" data-runs="close">Collapse all</button></span>'
            "</div>"
        )
        for run in section.runs:
            _run(page, run)
        page.add("</div>")
    page.add(_SECTION_CLOSE)


def _provenance_block(block: Provenance) -> str:
    rows = "".join(f"<dt>{esc(k)}</dt><dd>{esc(v)}</dd>" for k, v in block.entries)
    out = [f'<h3>{esc(block.title)}</h3><dl class="facts">{rows}</dl>']
    if block.diagnostics:
        lines = list(block.diagnostics)
        if block.omitted:
            lines.append(f"… and {block.omitted} more")
        count = len(block.diagnostics) + block.omitted
        out.append(_fold(f"Diagnostics ({count})", lines, "diag"))
    return "".join(out)


def _fold(title: str, lines: Sequence[str], kind: str) -> str:
    """A list collapsed under ``title`` until clicked."""
    items = "".join(f"<li>{esc(line)}</li>" for line in lines)
    return (
        f'<details class="fold"><summary>{esc(title)}</summary>'
        f'<ul class="{kind}">{items}</ul></details>'
    )


def _provenance(page: _Page, report: Report) -> None:
    number = page.numbers.next_chapter()
    page.add(
        f'<section id="{model.PROVENANCE}"><h2>{number}. Provenance '
        f"{permalink(model.PROVENANCE, model.PROVENANCE)}</h2>",
        *(_provenance_block(block) for block in report.provenance),
        _SECTION_CLOSE,
    )


def _toc(report: Report) -> str:
    links = [
        f'<a href="#{_SUMMARY}" data-filter="summary tl;dr">Summary</a>',
    ]
    if report.comparisons:
        links.append(
            f'<a href="#{_COMPARISONS}" data-filter="comparisons">Comparisons</a>'
        )
    heading = None
    for number, section in enumerate(report.sections, 2 + bool(report.comparisons)):
        group = (section.document, section.target)
        if group != heading:
            heading = group
            links.append(f'<p class="toc-group">{esc(section.target)}</p>')
        words = f"{section.target} {section.title} g{section.group_id}".lower()
        links.append(
            f'<a href="#{esc(section.anchor)}" data-filter="{esc(words)}">'
            f"{number}. {esc(section.title)}</a>"
        )
    links.append(
        f'<a href="#{model.PROVENANCE}" data-filter="provenance">Provenance</a>'
    )
    # Only runs with live CSV data have per-run detail to open.
    runs = ""
    if any(section.runs for section in report.sections):
        hint = 'title="Per-run live detail, from live CSV data"'
        runs = (
            f'<button type="button" data-runs="open" {hint}>Expand all runs</button>'
            f'<button type="button" data-runs="close" {hint}>Collapse all runs'
            "</button>"
        )
    return (
        '<nav class="toc" aria-label="Contents"><p class="toc-title">Contents</p>'
        '<input type="search" class="toc-filter" placeholder="Filter sections" '
        'aria-label="Filter sections">' + "".join(links) + f'<div class="tools">{runs}'
        '<button type="button" class="theme">Light/dark</button></div></nav>'
    )


def chart_data(specs: dict) -> str:
    """The chart blob: compact JSON with every ``<`` escaped."""
    text = json.dumps(specs, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return text.replace("<", "\\u003c")


def render_html(report: Report) -> str:
    """The complete page."""
    page = _Page()
    _summary(page, report)
    _comparisons(page, report)
    for section in report.sections:
        _section(page, section)
    _provenance(page, report)
    return "".join(
        [
            '<!doctype html><html lang="en"><head><meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            f"<title>{esc(report.title)}</title>",
            f"<style>{asset('report.css')}</style></head><body>",
            '<div class="page">',
            _toc(report),
            "<main>",
            '<noscript><p class="note">Charts need JavaScript; tables work '
            "without it.</p></noscript>",
            *page.out,
            "</main></div>",
            f'<script type="application/json" id="{CHART_DATA_ID}">',
            chart_data(page.specs),
            "</script>",
            f'<script src="{CHART_JS_URL}" integrity="{CHART_JS_INTEGRITY}" '
            'crossorigin="anonymous"></script>',
            f"<script>{asset('report.js')}</script>",
            "</body></html>\n",
        ]
    )
