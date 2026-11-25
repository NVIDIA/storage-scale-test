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

"""Per-run live detail: aggregate, spread, fairness, clients and heatmap.

Per-client charts, the heatmap and the client table need live detail
``full``: they stream the domain's sidecar once and keep only the clients
shown (the heatmap's slowest rows and a few representative lines).
"""

from __future__ import annotations

import math
import statistics
from typing import Sequence

from lib import elbencho_services as services_lib
from lib.elbencho_live_steady import CLASS_OK, CLASS_PERSISTENT, CLASS_TRANSIENT
from lib.report_html import model
from lib.report_html.common import (
    ELAPSED_S,
    FMT_GBS,
    FMT_MS,
    GB,
    GBS,
    IO_PHASES,
    K_CLIENT,
    K_LIVE,
    K_MIX,
    K_OCCURRENCE,
    K_PHASE,
    LATENCY_MS,
    LIVE_FULL,
    LIVE_NONE,
    MB,
    MS_PER_S,
    NO_VALUE,
    US_PER_MS,
    GroupContext,
    axis,
    line_spec,
    phase_of,
    results_of,
    scatter_spec,
    series,
    steady_span,
)
from lib.report_html.model import (
    Column,
    Figure,
    RunDetail,
    RunDomain,
    Table,
    category_color,
    slug,
)
from lib.report_html.reduce import (
    CLIENT_TABLE_MAX_ROWS,
    HEATMAP_HI,
    HEATMAP_LO,
    HEATMAP_MAX_ROWS,
    HEATMAP_MISSING,
    decimate,
    encode_heatmap,
    finite,
    heatmap_step,
    scaled,
    sig,
)
from lib.report_html.services import service_figures
from lib.reporting_common import natural_key
from lib.results_document import ResultsDocumentError, read_client_series

MIN_SPREAD_CLIENTS = 3
MIN_SCATTER_CLIENTS = 2
SCATTER_MAX_POINTS = 500
# Client classes in legend order; "ok" is the normal class.
_CLASS_COLORS = (
    (CLASS_PERSISTENT, model.P99),
    (CLASS_TRANSIENT, category_color(8)),
    (CLASS_OK, model.NEUTRAL),
)
SLOWEST_LINES = 4
JAIN_WINDOWS = ("5s", "30s")
MB_PER_CLIENT = "MB/s per client"
BELOW_FULL_NOTE = "Per-client charts, heatmap and client table need live detail full."
_STEADY_NOTE = " Shaded: steady window."
# elbencho phases that move no data; results.json keeps them.
_NO_DATA_PHASES = frozenset({"SYNC", "DROPCACHE"})


def domain_slug(domain: dict) -> str:
    """Anchor part of a live domain: ``write``, ``write-2``, ``read-mix-a``."""
    text = slug(domain.get(K_PHASE) or K_PHASE)
    if domain.get(K_MIX):
        text += f"-{slug(domain['mix_type'])}"
    occurrence = domain.get(K_OCCURRENCE) or 0
    return f"{text}-{occurrence + 1}" if occurrence else text


def domain_label(domain: dict) -> str:
    """Display label of a live domain."""
    text = str(domain.get(K_PHASE) or K_PHASE)
    if domain.get(K_MIX):
        text += f" ({domain['mix_type']})"
    occurrence = domain.get(K_OCCURRENCE) or 0
    return f"{text} #{occurrence + 1}" if occurrence else text


def domain_order(domain: dict) -> tuple:
    """Write, then read, then other phases by name; then mix and occurrence."""
    phase = str(domain.get(K_PHASE) or "").lower()
    rank = IO_PHASES.index(phase) if phase in IO_PHASES else len(IO_PHASES)
    return rank, phase, str(domain.get(K_MIX) or ""), domain.get(K_OCCURRENCE) or 0


def _analyzed(live) -> bool:
    return isinstance(live, dict) and bool(live.get("aggregate_series"))


def live_domains(execution: dict, level: str = LIVE_FULL) -> list[dict]:
    """Analyzed live domains: each phase's ``live``, then
    ``additional_live_domains[]`` (schema 1.1; absent in 1.0). Live detail
    ``level`` none has none."""
    if level == LIVE_NONE:
        return []
    results = results_of(execution)
    domains = []
    for phase in results.get("phases") or []:
        live = phase.get(K_LIVE)
        if _analyzed(live):
            domains.append(_domain_entry(live, phase.get(K_PHASE)))
    for live in results.get("additional_live_domains") or []:
        if _analyzed(live):
            domains.append(_domain_entry(live, None))
    return [d for d in domains if str(d[K_PHASE]).upper() not in _NO_DATA_PHASES]


def _domain_entry(live: dict, phase) -> dict:
    ident = {K_PHASE: phase, K_MIX: "", K_OCCURRENCE: 0}
    ident.update(live.get("domain") or {})
    return {**ident, K_LIVE: live}


def _seconds(values: Sequence) -> list:
    return [scaled(v, MS_PER_S) for v in values or []]


def _shade(live: dict):
    span = steady_span(live)
    if span is None:
        return None
    return [sig(span[0] / MS_PER_S), sig(span[1] / MS_PER_S)]


def _color(phase: str) -> str:
    return model.PHASE_COLORS.get(phase, category_color(3))


class _DomainBuilder:
    """Builds the figures and table of one live domain."""

    def __init__(
        self,
        ctx: GroupContext,
        anchors: tuple[str, str],
        domain: dict,
        services: dict | None = None,
    ):
        self.ctx = ctx
        # The phase's final per-service results (None before elbencho 3.4).
        self.services = services
        self.section_anchor, self.suffix = anchors
        self.domain = domain
        self.live = domain[K_LIVE]
        self.phase = str(domain.get(K_PHASE) or "")
        self.label = domain_label(domain)
        self.color = _color(self.phase)
        self.shade = _shade(self.live)
        # Steady-window statistics exist only with a usable window.
        self.has_steady = self.shade is not None

    def anchor(self, template: str) -> str:
        """Figure or table anchor inside this run detail."""
        return f"{self.section_anchor}-{template}{self.suffix}"

    def figure(self, template: str, title: str, caption: str, spec: dict, small=True):
        """A figure of this domain."""
        return Figure(template, self.anchor(template), title, caption, spec, small)

    def aggregate(self) -> Figure:
        """Aggregate GB/s and mean latency over time."""
        agg = self.live["aggregate_series"]
        times = _seconds(agg.get("elapsed_ms"))
        gbs = [scaled(v, GB) for v in agg.get("bytes_per_s") or []]
        latency = [scaled(v, US_PER_MS) for v in agg.get("io_latency_avg_us") or []]
        lines = [series(GBS, self.color, decimate(times, gbs))]
        if any(v is not None for v in latency):
            lines.append(
                series(
                    "mean latency", model.NEUTRAL, decimate(times, latency), True, "y2"
                )
            )
        caption = "Total GB/s (left) and fleet mean IO latency (right) per interval."
        worst = self._worst_latency(agg, times)
        if worst:
            lines.append(worst)
            caption = (
                "Total GB/s (left) and fleet IO latency per interval (right): "
                "mean, and dashed the slowest single IO."
            )
        if agg.get("start_time"):
            caption += f" Started {agg['start_time']}."
        spec = line_spec(
            axis(ELAPSED_S),
            axis(GBS, fmt=FMT_GBS, min=0),
            lines,
            y2=axis(LATENCY_MS, fmt=FMT_MS, min=0),
            shade=self.shade,
        )
        title = f"{self.label} aggregate throughput and latency"
        return self.figure(
            model.RUN_AGGREGATE, title, caption + _shade_note(self.shade), spec
        )

    def _worst_latency(self, agg: dict, times: list) -> dict | None:
        """The per-interval slowest single IO (elbencho 3.4), on the latency axis."""
        worst = [scaled(v, US_PER_MS) for v in agg.get("io_latency_max_us") or []]
        if all(v is None for v in worst):
            return None
        points = decimate(times, worst)
        return series("max latency (any IO)", category_color(4), points, True, "y2")

    def client_scatter(self) -> Figure | None:
        """Steady MB/s against steady mean IO latency, one point per client."""
        records = _summary_records(self.live)
        points = [
            r
            for r in records
            if r.get("steady_bytes_per_s") is not None
            and r.get("steady_io_latency_avg_us") is not None
        ]
        if len(points) < MIN_SCATTER_CLIENTS:
            return None
        shown = _scatter_sample(points)
        groups = []
        for name, color in _CLASS_COLORS:
            members = [r for r in shown if (r.get("class") or CLASS_OK) == name]
            if members:
                groups.append(_class_series(name, color, members))
        spec = scatter_spec(
            axis("steady MB/s", min=0),
            axis(LATENCY_MS, fmt=FMT_MS, min=0),
            groups,
        )
        caption = (
            "One point per client over the steady window (hover for the name). "
            "Low throughput at normal latency points at the client host; high "
            "latency points at that client's IO path. ok: normal clients."
        )
        if len(shown) < len(points):
            caption += f" Showing {len(shown)} of {len(points)} clients: the slowest and the highest-latency."
        return self.figure(
            model.RUN_CLIENT_SCATTER,
            f"{self.label} client throughput vs latency",
            caption,
            spec,
        )

    def spread(self) -> Figure | None:
        """Per-client p10-p90 band with the median client."""
        fleet = self.live.get("fleet_series")
        if not fleet or (self.live.get("clients") or 0) < MIN_SPREAD_CLIENTS:
            return None
        times = _seconds(fleet.get("elapsed_ms"))

        def mbs(key):
            return decimate(times, [scaled(v, MB) for v in fleet.get(key) or []])

        spec = line_spec(
            axis(ELAPSED_S),
            axis(MB_PER_CLIENT, min=0),
            [series("p50", self.color, mbs("bytes_per_s_p50"))],
            band={
                "name": "p10–p90",
                "color": self.color,
                "lower": mbs("bytes_per_s_p10"),
                "upper": mbs("bytes_per_s_p90"),
            },
            shade=self.shade,
        )
        caption = "Per-client MB/s per interval: p10–p90 band and median."
        return self.figure(
            model.RUN_SPREAD,
            f"{self.label} per-client throughput spread",
            caption + _shade_note(self.shade),
            spec,
        )

    def fairness(self) -> Figure | None:
        """Jain index over the steady window's windows."""
        if not self.has_steady:
            return None
        fairness = self.live.get("fairness") or {}
        lines = []
        styles = ((category_color(5), False), (model.NEUTRAL, True))
        for key, (color, dash) in zip(JAIN_WINDOWS, styles):
            window = fairness.get(key) or {}
            points = window.get("series") or {}
            if not points.get("jain"):
                continue
            seconds = (window.get("window_ms") or 0) / MS_PER_S
            name = f"Jain, {seconds:g} s windows" if seconds else f"Jain, {key}"
            times = _seconds(points.get("end_ms"))
            lines.append(series(name, color, decimate(times, points["jain"]), dash))
        if not lines:
            return None
        spec = line_spec(
            axis("window end (s)"), axis("Jain index", fmt="ratio", max=1), lines
        )
        caption = "1.0: every client got an equal share; 1/N: one client got all."
        return self.figure(
            model.RUN_FAIRNESS, f"{self.label} fairness (Jain index)", caption, spec
        )

    def build(self, heading_anchor: str, heading: str) -> RunDomain:
        """All figures, the client table and notes of this domain."""
        result = RunDomain(heading_anchor, self.phase, heading)
        figures = (self.aggregate(), self.spread(), self.fairness())
        for figure in figures + (self.client_scatter(),):
            if figure:
                result.figures.append(figure)
        ref = self.live.get("client_series")
        full = self.ctx.live_detail == LIVE_FULL
        if not (full and isinstance(ref, dict) and ref.get("path")):
            result.notes.append(BELOW_FULL_NOTE)
            return result
        try:
            result.figures.extend(_ClientFigures(self, ref).build())
        except ResultsDocumentError as error:
            result.notes.append(f"Per-client data unavailable: {error}")
        result.table = client_table(self)
        return result


def _scatter_sample(records: list[dict]) -> list[dict]:
    """All records, or the slowest and highest-latency half each over the cap."""
    if len(records) <= SCATTER_MAX_POINTS:
        return records
    half = SCATTER_MAX_POINTS // 2
    slow = sorted(records, key=lambda r: r["steady_bytes_per_s"])[:half]
    late = sorted(records, key=lambda r: -r["steady_io_latency_avg_us"])
    keep = {id(r) for r in slow}
    extra = [r for r in late if id(r) not in keep][: SCATTER_MAX_POINTS - len(slow)]
    chosen = {id(r) for r in slow + extra}
    return [r for r in records if id(r) in chosen]


def _class_series(name: str, color: str, members: list[dict]) -> dict:
    points = [
        [
            sig(scaled(r["steady_bytes_per_s"], MB)),
            sig(scaled(r["steady_io_latency_avg_us"], US_PER_MS)),
        ]
        for r in members
    ]
    return {
        **series(name, color, points),
        "names": [str(r.get(K_CLIENT)) for r in members],
    }


def _shade_note(shade) -> str:
    return _STEADY_NOTE if shade else ""


def _share_basis(builder: _DomainBuilder) -> str:
    """Client shares are over the steady window, else over the whole run."""
    return "steady" if builder.has_steady else "mean"


_STEADY_FIELDS = (
    "class",
    "steady_bytes_per_s",
    "steady_ratio",
    "worst_5s_ratio",
    "worst_30s_ratio",
    "short_5s_windows",
    "steady_5s_windows",
)


def _summary_records(live: dict) -> list[dict]:
    summary = live.get("clients_summary") or {}
    columns = summary.get("columns") or []
    records = [dict(zip(columns, row)) for row in summary.get("rows") or []]
    if steady_span(live) is None:
        # Class and steady statistics need a usable steady window.
        records = [
            {k: v for k, v in record.items() if k not in _STEADY_FIELDS}
            for record in records
        ]
    return records


def _ratio_key(record: dict):
    ratio = record.get("steady_ratio")
    if ratio is None:
        ratio = record.get("mean_ratio_to_fleet_median")
    return (
        math.inf if ratio is None else ratio,
        natural_key(str(record.get(K_CLIENT))),
    )


def _median(values: Sequence):
    valid = [v for v in values if finite(v) is not None]
    return statistics.median(valid) if valid else None


def _share(value, median):
    return value / median if value is not None and median else None


def _value_at(values: Sequence, index: int):
    return values[index] if 0 <= index < len(values) else None


class _ClientFigures:
    """Selected-client lines and the heatmap, from one pass over a sidecar."""

    def __init__(self, builder: _DomainBuilder, ref: dict):
        self.builder = builder
        self.series = read_client_series(builder.ctx.loaded, ref)
        clients = self.series.clients
        ratios = {
            str(r.get(K_CLIENT)): _ratio_key(r) for r in _summary_records(builder.live)
        }
        self.ratios = ratios
        self.order = sorted(
            range(len(clients)),
            key=lambda i: ratios.get(clients[i], (math.inf, clients[i])),
        )

    def _line_clients(self) -> list[int]:
        order = self.order
        if len(order) <= SLOWEST_LINES + 2:
            return list(order)
        picked = order[:SLOWEST_LINES] + [order[len(order) // 2], order[-1]]
        return list(dict.fromkeys(picked))

    def _stream(self, heat: list[int], lines: list[int]):
        times, heat_rows, line_rows = [], [[] for _ in heat], [[] for _ in lines]
        for elapsed, values in self.series.rows:
            times.append(elapsed / MS_PER_S)
            median = _median(values)
            for row, index in zip(heat_rows, heat):
                row.append(_share(_value_at(values, index), median))
            for row, index in zip(line_rows, lines):
                row.append(scaled(_value_at(values, index), MB))
        return times, heat_rows, line_rows

    def _line_name(self, index: int) -> str:
        name = self.series.clients[index]
        ratio = self.ratios.get(name, (math.inf,))[0]
        return name if math.isinf(ratio) else f"{name} ({ratio:.2f}× median)"

    def build(self) -> list[Figure]:
        """Selected-client lines, then the heatmap (three or more clients)."""
        builder = self.builder
        clients = self.series.clients
        heat = self.order[:HEATMAP_MAX_ROWS] if len(clients) >= 3 else []
        lines = self._line_clients()
        times, heat_rows, line_rows = self._stream(heat, lines)
        chart_lines = [
            series(self._line_name(i), category_color(k), decimate(times, row))
            for k, (i, row) in enumerate(zip(lines, line_rows))
        ]
        spec = line_spec(
            axis(ELAPSED_S), axis("MB/s", min=0), chart_lines, shade=builder.shade
        )
        caption = (
            f"Slowest clients by {_share_basis(builder)} share, plus the median "
            "and fastest client, MB/s." + _shade_note(builder.shade)
        )
        figures = [
            builder.figure(
                model.RUN_CLIENTS, f"{builder.label} selected clients", caption, spec
            )
        ]
        if heat:
            figures.append(self._heatmap(times, heat, heat_rows))
        return figures

    def _heatmap(self, times: list, heat: list[int], rows: list) -> Figure:
        step = heatmap_step(len(times))
        data, columns = encode_heatmap(rows, step)
        interval = (times[-1] - times[0]) / (len(times) - 1) if len(times) > 1 else 1
        spec = {
            "kind": "heatmap",
            "x": {"label": ELAPSED_S},
            "heatmap": {
                "rows": [self.series.clients[i] for i in heat],
                "cols": columns,
                "t0": sig(times[0] - interval if times else 0),
                "dt": sig(interval * step),
                "lo": HEATMAP_LO,
                "hi": HEATMAP_HI,
                "missing": HEATMAP_MISSING,
                "data": data,
            },
        }
        total = len(self.series.clients)
        shown = f"the {len(heat)} slowest of {total}" if len(heat) < total else "all"
        caption = (
            f"Rows: {shown} clients, slowest {_share_basis(self.builder)} share "
            "first. Colour: client "
            "throughput ÷ fleet median per interval; blue below, red above, "
            "grey missing."
        )
        return self.builder.figure(
            model.RUN_HEATMAP,
            f"{self.builder.label} per-client share of fleet median",
            caption,
            spec,
            small=False,
        )


_LATENCY_STATS = (
    ("p50", "final p50 ms"),
    ("p99", "final p99 ms"),
    ("max", "final max ms"),
)
_LIVE_IO_COLUMNS = (
    ("steady_ops_per_s", "steady ops/s", False),
    ("steady_io_latency_avg_us", "IO lat avg ms", False),
    ("steady_io_latency_p90_us", "IO lat p90 ms", True),
    ("steady_io_latency_max_us", "IO lat max ms", True),
    ("steady_io_latency_ratio", "IO lat ÷ fleet median", False),
)


def _has(records: list[dict], key: str) -> bool:
    return any(r.get(key) is not None for r in records)


def _client_columns(records: list[dict], services: dict | None) -> list[Column]:
    columns = [Column(K_CLIENT, numeric=False)]
    if any(r.get("kubernetes_node") for r in records):
        columns.append(Column("node", numeric=False))
    columns += [
        Column("class", numeric=False, badge=True),
        Column("steady MB/s"),
        Column("steady ÷ median", digits=3),
        Column("worst 5 s ÷ median", digits=3),
        Column("worst 30 s ÷ median", extra=True, digits=3),
    ]
    if any("short_5s_windows" in r for r in records):
        columns.append(Column("short 5 s windows", numeric=False))
    for key, label, extra in _LIVE_IO_COLUMNS:
        if _has(records, key):
            columns.append(Column(label, extra=extra, digits=3))
    if services_lib.instances(services):
        columns += [
            Column(label, extra=key == "max", digits=3) for key, label in _LATENCY_STATS
        ]
    columns += [Column("finish s", extra=True), Column("bad samples")]
    return columns


def _final_latency_ms(record: dict, services: dict | None, key: str):
    entry = services_lib.find_instance(services, record.get("service"))
    return sig(scaled(services_lib.latency_of(entry, key), US_PER_MS), 3)


def _finish_s(record: dict, services: dict | None):
    """The service's own completion time, else the live CSV's estimate."""
    entry = services_lib.find_instance(services, record.get("service"))
    elapsed = (entry or {}).get("elapsed_ms")
    if elapsed is None:
        elapsed = record.get("finish_elapsed_ms")
    return sig(scaled(elapsed, MS_PER_S))


_US_KEYS = frozenset(
    {
        "steady_io_latency_avg_us",
        "steady_io_latency_p90_us",
        "steady_io_latency_max_us",
    }
)


def _client_row(record: dict, columns: list[Column], services: dict | None) -> list:
    labels = {c.label for c in columns}
    row = [record.get(K_CLIENT)]
    if "node" in labels:
        row.append(record.get("kubernetes_node") or "")
    row += [
        record.get("class") or NO_VALUE,
        sig(scaled(record.get("steady_bytes_per_s"), MB)),
        sig(record.get("steady_ratio"), 3),
        sig(record.get("worst_5s_ratio"), 3),
        sig(record.get("worst_30s_ratio"), 3),
    ]
    if "short 5 s windows" in labels:
        short, steady = record.get("short_5s_windows"), record.get("steady_5s_windows")
        row.append(NO_VALUE if short is None else f"{short} of {steady}")
    for key, label, _ in _LIVE_IO_COLUMNS:
        if label in labels:
            value = record.get(key)
            value = scaled(value, US_PER_MS) if key in _US_KEYS else value
            row.append(sig(value, 3 if key in _US_KEYS else 4))
    if "final p50 ms" in labels:
        row += [_final_latency_ms(record, services, k) for k, _ in _LATENCY_STATS]
    bad = (record.get("missing_samples") or 0) + (record.get("invalid_samples") or 0)
    row += [_finish_s(record, services), bad]
    return row


def client_table(builder: _DomainBuilder) -> Table | None:
    """The slowest clients by steady share, at most CLIENT_TABLE_MAX_ROWS."""
    records = sorted(_summary_records(builder.live), key=_ratio_key)
    if not records:
        return None
    shown = records[:CLIENT_TABLE_MAX_ROWS]
    columns = _client_columns(records, builder.services)
    total = len(records)
    count = f"{len(shown)} slowest of {total}" if len(shown) < total else str(total)
    caption = f"{builder.label} per-client summary, slowest first ({count} clients)"
    return Table(
        model.RUN_CLIENT_TABLE,
        builder.anchor(model.RUN_CLIENT_TABLE),
        caption,
        columns,
        [_client_row(r, columns, builder.services) for r in shown],
    )


def _chip(execution: dict, domain: dict) -> tuple[str, str]:
    live = domain[K_LIVE]
    parts = []
    phase = phase_of(execution, str(domain.get(K_PHASE)))
    rate = ((phase or {}).get("elbencho_first") or {}).get("bytes_per_s")
    if rate is not None and not domain.get(K_MIX):
        parts.append(f"{sig(rate / GB, 3)} GB/s")
    steady = steady_span(live) is not None
    jain = (((live.get("fairness") or {}).get("5s") or {}).get("jain") or {}).get("p50")
    jain = jain if steady else None
    parts.append(f"Jain {NO_VALUE if jain is None else f'{jain:.3f}'}")
    dist = live.get("distribution") or {}
    if steady and dist.get("persistent_clients") is not None:
        parts.append(
            f"{dist['persistent_clients']} persistent, "
            f"{dist.get('transient_clients') or 0} transient"
        )
    return str(domain.get(K_PHASE) or ""), " · ".join(parts)


def _domain_services(execution: dict, domain: dict) -> dict | None:
    """The per-service results of a plain domain's phase, else None."""
    if domain.get(K_MIX) or domain.get(K_OCCURRENCE):
        return None
    return (phase_of(execution, str(domain.get(K_PHASE))) or {}).get("services")


def _service_domains(
    section_anchor: str,
    execution_id: str,
    execution: dict,
    plain: dict[str, RunDomain],
) -> list[RunDomain]:
    """Add each IO phase's per-service figures to its live domain in ``plain``
    (by phase), or return a domain of their own for a phase without one."""
    extra = []
    for phase in results_of(execution).get("phases") or []:
        name = str(phase.get(K_PHASE))
        if name not in IO_PHASES:
            continue
        suffix = f"-r{slug(execution_id)}-{slug(name)}"

        def make(template, title, caption, spec, suffix=suffix):
            anchor = f"{section_anchor}-{template}{suffix}"
            return Figure(template, anchor, title, caption, spec, True)

        found = service_figures(phase, name, make)
        if not found:
            continue
        if name in plain:
            plain[name].figures.extend(found)
            continue
        count = len(services_lib.instances(phase.get("services")))
        heading = f"{name}: per-service results, {count} services"
        extra.append(RunDomain(f"{section_anchor}{suffix}", name, heading, found))
    return extra


def build_run(
    ctx: GroupContext, section_anchor: str, run: tuple[str, str], execution: dict
) -> RunDetail | None:
    """Collapsed detail of one execution: its live domains and the per-service
    results of its IO phases; None without either."""
    execution_id, label = run
    domains = live_domains(execution, ctx.live_detail)
    run_anchor = f"{section_anchor}-r{slug(execution_id)}"
    built = []
    plain = {}
    for domain in domains:
        suffix = f"-r{slug(execution_id)}-{domain_slug(domain)}"
        live = domain[K_LIVE]
        mode = live.get("client_mode") or K_CLIENT
        heading = f"{domain_label(domain)}: {live.get('clients') or 0} {mode}s"
        services = _domain_services(execution, domain)
        builder = _DomainBuilder(ctx, (section_anchor, suffix), domain, services)
        result = builder.build(f"{section_anchor}{suffix}", heading)
        built.append(result)
        if not (domain.get(K_MIX) or domain.get(K_OCCURRENCE)):
            plain.setdefault(str(domain.get(K_PHASE)), result)
    extra = _service_domains(section_anchor, execution_id, execution, plain)
    if not built and not extra:
        return None
    chips = [_chip(execution, d) for d in domains]
    return RunDetail(run_anchor, execution_id, label, chips, built + extra)
