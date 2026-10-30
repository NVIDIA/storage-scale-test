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

"""Renderer-neutral report model: sections, figures, tables and anchors.

Every figure and table names its template (one of ``TEMPLATES``) and a
stable anchor derived from content: ``g<group>-<shape>-<template>``, plus
``-r<execution>-<domain>`` inside per-run detail, prefixed with ``d<N>-``
only when a report has several documents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

# Template names, catalogued for users so feedback can name every instance.
HEADER_CARDS = "header-cards"
TLDR_TABLE = "tldr-table"
COMPARISON_CHART = "comparison-chart"
SWEEP_THROUGHPUT = "sweep-throughput"
SWEEP_IOPS = "sweep-iops"
SWEEP_EFFICIENCY = "sweep-efficiency"
SWEEP_SHARE = "sweep-share"
SWEEP_LATENCY = "sweep-latency"
SWEEP_HISTOGRAM = "sweep-histogram"
RESULTS_TABLE = "results-table"
FAIRNESS_TABLE = "fairness-table"
MD_RATES = "md-rates"
MD_LATENCY = "md-latency"
RUN_AGGREGATE = "run-aggregate"
RUN_SPREAD = "run-spread"
RUN_FAIRNESS = "run-fairness"
RUN_CLIENTS = "run-clients"
RUN_HEATMAP = "run-heatmap"
RUN_CLIENT_TABLE = "run-client-table"
PROVENANCE = "provenance"
TEMPLATES = (
    HEADER_CARDS,
    TLDR_TABLE,
    COMPARISON_CHART,
    SWEEP_THROUGHPUT,
    SWEEP_IOPS,
    SWEEP_EFFICIENCY,
    SWEEP_SHARE,
    SWEEP_LATENCY,
    SWEEP_HISTOGRAM,
    RESULTS_TABLE,
    FAIRNESS_TABLE,
    MD_RATES,
    MD_LATENCY,
    RUN_AGGREGATE,
    RUN_SPREAD,
    RUN_FAIRNESS,
    RUN_CLIENTS,
    RUN_HEATMAP,
    RUN_CLIENT_TABLE,
    PROVENANCE,
)

# Series colours. Read/write and percentile roles stay fixed across charts;
# CATEGORICAL colours configurations, clients and other domains.
READ = "#2468c8"
WRITE = "#d4561a"
P50 = "#32874a"
P99 = "#be3434"
NEUTRAL = "#6e7885"
CATEGORICAL = (
    "#2468c8",
    "#d4561a",
    "#32874a",
    "#7349c2",
    "#be3434",
    "#1b8a94",
    "#b8417a",
    "#7d5a3c",
    "#9a7d12",
    "#4f5d6b",
)
PHASE_COLORS = {"read": READ, "write": WRITE}

_SLUG = re.compile(r"[^a-z0-9]+")


def slug(text: Any) -> str:
    """Lowercase ``text`` to ``[a-z0-9-]`` for anchors."""
    return _SLUG.sub("-", str(text).lower()).strip("-") or "x"


def category_color(index: int) -> str:
    """Categorical colour ``index``, cycling."""
    return CATEGORICAL[index % len(CATEGORICAL)]


@dataclass
class Column:
    """One table column; ``extra`` columns hide until "show all columns"."""

    label: str
    numeric: bool = True
    extra: bool = False
    best: str | None = None  # "max" or "min": highlight the best value
    badge: bool = False  # render text as a small coloured badge
    digits: int = 4


@dataclass(frozen=True)
class Link:
    """A table cell linking to an anchor in the report."""

    text: str
    href: str


@dataclass(frozen=True)
class Measure:
    """A table cell shown as ``text`` and sorted by ``value``."""

    value: float | None
    text: str


@dataclass
class Table:
    """A server-rendered table; ``best_by`` scopes highlighting to a column."""

    template: str
    anchor: str
    caption: str
    columns: list[Column]
    rows: list[list[Any]]
    best_by: int | None = None


@dataclass
class Figure:
    """A chart; ``spec`` is the JSON handed to the browser."""

    template: str
    anchor: str
    title: str
    caption: str
    spec: dict
    small: bool = False


@dataclass
class Card:
    """One headline value."""

    label: str
    value: float
    unit: str
    detail: str
    href: str
    subtitle: str = ""


@dataclass
class RunDomain:
    """Live detail of one execution phase (domain)."""

    anchor: str
    phase: str
    heading: str
    figures: list[Figure] = field(default_factory=list)
    table: Table | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class RunDetail:
    """Collapsed per-run live detail for one execution."""

    anchor: str
    execution: str
    label: str
    chips: list[tuple[str, str]]
    domains: list[RunDomain]


@dataclass
class Section:
    """One group and shape (IO) or one metadata group."""

    anchor: str
    title: str
    target: str
    group_id: str
    document: int
    settings: list[tuple[str, str]]
    settings_text: str | None = None
    figures: list[Figure] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    runs: list[RunDetail] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    best: dict = field(default_factory=dict)
    executions: int = 0


@dataclass
class Provenance:
    """Where one document came from and what went wrong collecting it."""

    title: str
    entries: list[tuple[str, str]]
    diagnostics: list[str]
    omitted: int = 0


@dataclass
class Comparison:
    """Overlay charts of sections that differ in one setting (or forced)."""

    anchor: str
    title: str
    caption: str
    members: list[Link]
    figures: list[Figure]


@dataclass
class Report:
    """Everything the renderer needs, in display order.

    ``tool`` is the header line naming the rendering (and, if different,
    exporting) version. ``comparisons`` holds overlay charts of paired
    sections; empty renders no comparison part.
    """

    title: str
    subtitle: str
    sources: list[str]
    tool: str
    cards: list[Card]
    tldr: Table
    sections: list[Section]
    provenance: list[Provenance]
    comparisons: list[Comparison] = field(default_factory=list)
