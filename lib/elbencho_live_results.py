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

"""Machine-readable live-client analysis for the filesystem results JSON.

Reuses the single streaming pass in ``lib.elbencho_live_report`` and adds the
per-domain live object (summary, optional inline or sidecar client series).
"""

from __future__ import annotations

import csv
import math
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    Collection,
    Dict,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from lib.elbencho_live_report import (
    MIB,
    TOTAL_RANK,
    AggregatePoint,
    ClientSummary,
    DomainAnalysis,
    LiveAnalysis,
    LiveClientId,
    LiveDomainKey,
    LiveFileMetadata,
    ParsedRow,
    PreviousSample,
    analyze_live_csv,
    client_sort_key,
    collect_selected_client_detail,
    derive_rate,
    iter_live_rows,
    percentile_from_sorted,
)
from lib.elbencho_live_steady import (
    CLASS_PERSISTENT,
    CLASS_TRANSIENT,
    END_SOURCE_FIRST,
    REASON_NO_FIRST_TIME,
    WINDOW_LABELS,
    ClientSteady,
    PhaseContext,
    SteadyAnalysis,
    SteadyWindow,
    analyze_steady,
    classified_clients,
    select_heatmap_rows,
)
from lib.filesystem_results import (
    LIVE_SCHEMA,
    LIVE_SCHEMA_VERSION,
    SidecarWriter,
    file_record,
    mib_s_to_bytes_per_s,
    round_latency_us,
    round_ops,
)

LEVEL_SELECTED = "selected"
LEVEL_FULL = "full"
CLIENT_COLUMNS = [
    "client",
    "rank",
    "service",
    "kubernetes_node",
    "kubernetes_pod",
    "class",
    "mean_bytes_per_s",
    "median_bytes_per_s",
    "mean_ratio_to_fleet_median",
    "steady_bytes_per_s",
    "steady_ratio",
    "worst_5s_ratio",
    "worst_5s_elapsed_ms",
    "worst_30s_ratio",
    "worst_30s_elapsed_ms",
    "short_5s_share",
    "final_done_bytes",
    "finish_elapsed_ms",
    "valid_samples",
    "missing_samples",
    "invalid_samples",
    "short_5s_windows",
    "steady_5s_windows",
    "first_progress_elapsed_ms",
]
DEFAULT_DIAGNOSTICS = ("missing_samples", "counter_resets", "non_adjacent_samples")
ADDITIONAL_DOMAINS = "additional_domains"
NO_CONTEXT = PhaseContext(None, None, None, None)
LIVE_SUFFIX = ".live.csv"
DONE_BYTES_DELTA = "done_bytes_delta"
# An elbencho ISO Date: date and time, an optional fraction, then the zone.
_ISO_DATE = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,6}))?(.*)")


class LiveCSVError(Exception):
    """Raised when a live CSV cannot be read or analyzed."""


def _domain_order(live: dict) -> Tuple[str, str, int]:
    domain = live["domain"]
    return domain["phase"], domain["mix_type"], domain["occurrence"]


class LiveObjects(NamedTuple):
    """One live CSV's objects, each domain once: the first domain of each
    lowercase phase, and the later ones in domain order."""

    by_phase: Dict[str, dict]
    later: List[dict]

    def split(self, attached: Collection[str]) -> Tuple[Dict[str, dict], List[dict]]:
        """(``phases[].live`` of the ``attached`` phases, every other domain).

        An attached object counts its phase's later domains in
        ``diagnostics.additional_domains``.
        """
        counts = Counter(live["domain"]["phase"] for live in self.later)
        phases: Dict[str, dict] = {}
        others = list(self.later)
        for phase, live in self.by_phase.items():
            if phase not in attached:
                others.append(live)
                continue
            if counts[phase]:
                diagnostics = {**live["diagnostics"], ADDITIONAL_DOMAINS: counts[phase]}
                live = {**live, "diagnostics": dict(sorted(diagnostics.items()))}
            phases[phase] = live
        return phases, sorted(others, key=_domain_order)


def null_live(reason: str) -> dict:
    """Return the ``live`` value used when no analysis is available."""
    return {"reason": reason}


def load_workers_tsv(path: str) -> Dict[str, Tuple[str, str]]:
    """Read ``executions/<id>.workers.tsv`` into endpoint -> (node, pod)."""
    workers: Dict[str, Tuple[str, str]] = {}
    if not os.path.isfile(path):
        return workers
    with open(path, "r", encoding="utf-8", newline="") as handle:
        for fields in csv.reader(handle, delimiter="\t"):
            if len(fields) < 4 or fields[0] == "node":
                continue
            endpoint = fields[3].strip()
            if endpoint:
                workers[endpoint] = (fields[0].strip(), fields[1].strip())
    return workers


def build_live_objects(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    live_csv_path: str,
    relative_source_path: str,
    level: str,
    configured_interval_ms: Optional[int],
    contexts: Mapping[str, PhaseContext],
    classification: Tuple[float, int],
    selection_limits: Tuple[int, int],
    sidecar_dir: Optional[str],
    sidecar_prefix: Optional[str],
    execution_id: Optional[str],
    workers: Optional[Mapping[str, Tuple[str, str]]],
) -> LiveObjects:
    """Analyze one live CSV into one object per domain (see LiveObjects).

    ``classification`` is (shortfall percent, minimum slow 5 s windows). A
    ``sidecar_prefix`` of None derives one from the CSV name and digest.
    """
    try:
        source = file_record(live_csv_path, relative_source_path)
        analysis = analyze_live_csv(LiveFileMetadata(live_csv_path, "", 0, 0, 0, ""))
    except (OSError, ValueError, csv.Error, UnicodeError) as exc:
        raise LiveCSVError(f"cannot read live CSV {live_csv_path}: {exc}") from exc
    if sidecar_prefix is None:
        stem = os.path.basename(live_csv_path).removesuffix(LIVE_SUFFIX)
        sidecar_prefix = f"{stem}.{source['sha256'][:12]}"
    analyzed = [
        domain
        for domain in analysis.domains.values()
        if domain.aggregate_points or domain.clients
    ]
    built: Dict[LiveDomainKey, dict] = {}
    for domain in analyzed:
        context = contexts.get(domain.key.phase.lower())
        try:
            steady = analyze_steady(
                analysis,
                domain,
                context or NO_CONTEXT,
                configured_interval_ms,
                *classification,
            )
        except (OSError, ValueError, csv.Error, UnicodeError) as exc:
            raise LiveCSVError(f"cannot read live CSV {live_csv_path}: {exc}") from exc
        built[domain.key] = _build_live_object(
            (analysis, domain, steady, context),
            source,
            (level, configured_interval_ms, selection_limits),
            (sidecar_dir, sidecar_prefix, execution_id),
            workers or {},
        )
    by_phase: Dict[str, dict] = {}
    for domain in analyzed:
        by_phase.setdefault(domain.key.phase.lower(), built[domain.key])
    first = {id(live) for live in by_phase.values()}
    later = [built[key] for key in sorted(built) if id(built[key]) not in first]
    return LiveObjects(by_phase, later)


def _build_live_object(
    analyzed: Tuple[
        LiveAnalysis, DomainAnalysis, SteadyAnalysis, Optional[PhaseContext]
    ],
    source: dict,
    detail: Tuple[str, Optional[int], Tuple[int, int]],
    sidecar: Tuple[Optional[str], str, Optional[str]],
    workers: Mapping[str, Tuple[str, str]],
) -> dict:
    analysis, domain, steady, context = analyzed
    level, configured_interval_ms, selection_limits = detail
    ordered = _ordered_clients(domain)
    summaries = [domain.clients[client] for client in ordered]
    diagnostics = {name: 0 for name in DEFAULT_DIAGNOSTICS}
    diagnostics.update(domain.diagnostics)
    return {
        "source": source,
        "configured_interval_ms": configured_interval_ms,
        "client_mode": (
            "service" if any(client.service for client in ordered) else "worker"
        ),
        "domain": {
            "phase": domain.key.phase.lower(),
            "mix_type": domain.key.mix_type,
            "occurrence": domain.key.occurrence,
        },
        "phase_context": _phase_context(context),
        "intervals": len(domain.interval_times),
        "clients": len(ordered),
        "steady": _steady_object(steady),
        "aggregate_series": _aggregate_series(domain),
        "fleet_series": _fleet_series(domain),
        "fairness": _fairness(steady),
        "latency": _latency(steady),
        "distribution": _distribution(summaries, steady),
        "clients_summary": {
            "columns": list(CLIENT_COLUMNS),
            "rows": [_client_row(item, domain, steady, workers) for item in summaries],
        },
        "client_series": _client_series(
            analysis, domain, steady, ordered, level, selection_limits, sidecar
        ),
        "diagnostics": dict(sorted(diagnostics.items())),
    }


def _phase_context(context: Optional[PhaseContext]) -> Optional[dict]:
    """What the result pair says about the phase, or None without one."""
    if context is None:
        return None
    return {
        "first_ms": context.first_ms,
        "last_ms": context.last_ms,
        "block_size_bytes": context.block_size_bytes,
        "final_avg_latency_us": round_latency_us(context.final_avg_latency_us),
    }


def _steady_object(steady: SteadyAnalysis) -> dict:
    window = steady.window
    return {
        "start_ms": window.start_ms,
        "end_ms": window.end_ms,
        "end_source": (
            None if window.reason == REASON_NO_FIRST_TIME else END_SOURCE_FIRST
        ),
        "reason": window.reason,
        "interval_ms": steady.interval_ms,
    }


def _ordered_clients(domain: DomainAnalysis) -> List[LiveClientId]:
    return sorted(domain.clients, key=client_sort_key)


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000.0))


def _bytes(mib_s: Optional[float]) -> Optional[int]:
    return mib_s_to_bytes_per_s(mib_s)


def _aggregate_series(domain: DomainAnalysis) -> dict:
    points = domain.aggregate_points
    start_time, skews = _clock_skews(points)
    return {
        "elapsed_ms": [_ms(point.elapsed_sec) for point in points],
        "bytes_per_s": [_bytes(point.throughput_mib_s) for point in points],
        "ops_per_s": [round_ops(point.iops) for point in points],
        "io_latency_avg_us": [
            round_latency_us(point.io_latency_us) for point in points
        ],
        "entries_latency_avg_us": [
            round_latency_us(point.entries_latency_us) for point in points
        ],
        "start_time": start_time,
        "clock_skew_ms": skews,
    }


def _split_iso_date(text: str) -> Optional[Tuple[datetime, int, str]]:
    """(naive date and time, fraction digits, zone suffix) of an ISO Date."""
    match = _ISO_DATE.fullmatch(text)
    if match is None:
        return None
    try:
        moment = datetime.fromisoformat(match.group(1))
    except ValueError:
        return None
    fraction = match.group(2) or ""
    if fraction:
        moment += timedelta(microseconds=int(fraction.ljust(6, "0")))
    return moment, len(fraction), match.group(3)


def _format_iso_date(parts: Tuple[datetime, int, str], offset_ms: int) -> str:
    start, digits, zone = parts
    moment = start + timedelta(milliseconds=offset_ms)
    text = moment.isoformat(timespec="seconds")
    if digits:
        text += "." + f"{moment.microsecond:06d}"[:digits]
    return text + zone


def live_timestamp(start_time: str, offset_ms: int) -> Optional[str]:
    """The ISO Date ``offset_ms`` after ``start_time``, written in its format.

    Rebuilds ``aggregate_series`` sample times: ``offset_ms`` is the sample's
    ``elapsed_ms`` minus the first one, plus its ``clock_skew_ms``.
    """
    parts = _split_iso_date(start_time)
    return None if parts is None else _format_iso_date(parts, offset_ms)


def _clock_skews(
    points: Sequence[AggregatePoint],
) -> Tuple[Optional[str], List[Optional[int]]]:
    """The first sample's ISO Date and, per sample, how far its ISO Date is
    from that start plus its RuntimeMS offset (elbencho reads two clocks).

    A skew is None when ``live_timestamp`` cannot rebuild the sample's text.
    """
    if not points or not points[0].iso_timestamp:
        return None, [None] * len(points)
    start_time = points[0].iso_timestamp
    start = _split_iso_date(start_time)
    origin_ms = _ms(points[0].elapsed_sec)
    skews: List[Optional[int]] = []
    for point in points:
        parts = _split_iso_date(point.iso_timestamp)
        if start is None or parts is None:
            skews.append(None)
            continue
        offset_ms = _ms(point.elapsed_sec) - origin_ms
        skew = round((parts[0] - start[0]).total_seconds() * 1000) - offset_ms
        rebuilt = _format_iso_date(start, offset_ms + skew)
        skews.append(skew if rebuilt == point.iso_timestamp else None)
    return start_time, skews


def _fleet_series(domain: DomainAnalysis) -> dict:
    points = domain.fleet_points
    return {
        "elapsed_ms": [_ms(point.elapsed_sec) for point in points],
        "client_count": [point.client_count for point in points],
        "bytes_per_s_p10": [_bytes(point.p10_mib_s) for point in points],
        "bytes_per_s_p50": [_bytes(point.p50_mib_s) for point in points],
        "bytes_per_s_p90": [_bytes(point.p90_mib_s) for point in points],
        "bytes_per_s_mean": [_bytes(point.mean_mib_s) for point in points],
        "bytes_per_s_stddev": [_bytes(point.stddev_mib_s) for point in points],
    }


def _rounded(value: Optional[float], digits: int) -> Optional[float]:
    return None if value is None else round(value, digits)


def _window_object(steady: SteadyAnalysis, label: str) -> Optional[dict]:
    summary = steady.summary(label)
    if summary is None:
        return None
    series = steady.series[label]
    kept = [index for index, inside in enumerate(series.in_steady) if inside]
    return {
        "window_ms": summary.window_ms,
        "windows": summary.windows,
        "jain": {
            "min": _rounded(summary.jain_min, 6),
            "p10": _rounded(summary.jain_p10, 6),
            "p50": _rounded(summary.jain_p50, 6),
        },
        "jain_min_at_ms": summary.jain_min_at_ms,
        "share_below_0_95": _rounded(summary.share_below_0_95, 4),
        "min_over_median_p10": _rounded(summary.min_over_median_p10, 6),
        "floor_p50": _rounded(summary.floor_p50, 6),
        "series": {
            "end_ms": [series.end_ms[index] for index in kept],
            "clients": [series.clients[index] for index in kept],
            "jain": [_rounded(series.jain[index], 6) for index in kept],
        },
    }


def _fairness(steady: SteadyAnalysis) -> Optional[dict]:
    if not steady.window.available:
        return None
    return {label: _window_object(steady, label) for label in WINDOW_LABELS}


def _latency(steady: SteadyAnalysis) -> Optional[dict]:
    value = steady.latency
    if value is None:
        return None
    return {
        "p50_us": round_latency_us(value.p50_us),
        "p90_us": round_latency_us(value.p90_us),
        "max_us": round_latency_us(value.max_us),
        "max_over_p50": _rounded(value.max_over_p50, 4),
        "cv": _rounded(value.cv, 6),
        "steady_mean_us": round_latency_us(value.steady_mean_us),
        "final_avg_us": round_latency_us(value.final_avg_us),
        "rho_jain_latency": _rounded(value.rho_jain_latency, 4),
        "rho_latency_throughput": _rounded(value.rho_latency_throughput, 4),
    }


def _ratio(numerator: Optional[float], denominator: Optional[float]):
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator, 6)


def _cv(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    mean = sum(values) / len(values)
    if mean <= 0:
        return None
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return round(math.sqrt(variance) / mean, 6)


def _spread(
    values: Sequence[float], percentiles: Sequence[int]
) -> List[Optional[float]]:
    """Return min, the requested percentiles, then max (None when empty)."""
    if not values:
        return [None] * (len(percentiles) + 2)
    ordered = sorted(values)
    middle = [percentile_from_sorted(ordered, pct) for pct in percentiles]
    return [ordered[0], *middle, ordered[-1]]


def _whole(value: Optional[float]) -> Optional[int]:
    return None if value is None else int(round(value))


def _distribution(summaries: Sequence[ClientSummary], steady: SteadyAnalysis) -> dict:
    means = [item.mean_mib_s * MIB for item in summaries if item.sample_count]
    low, p10, p50, p90, high = _spread(means, (10, 50, 90))
    done = [
        item.final_done_bytes for item in summaries if item.final_done_bytes is not None
    ]
    done_min, done_p50, done_max = _spread(done, (50,))
    finish = [
        item.finish_elapsed_ms
        for item in summaries
        if item.finish_elapsed_ms is not None
    ]
    fin_min, fin_p50, fin_max = _spread(finish, (50,))
    counts = steady.class_counts() if steady.window.available else {}
    return {
        "client_mean_bytes_per_s": {
            "min": _whole(low),
            "p10": _whole(p10),
            "p50": _whole(p50),
            "p90": _whole(p90),
            "max": _whole(high),
            "cv": _cv(means),
        },
        "min_over_median": _ratio(low, p50),
        "max_over_median": _ratio(high, p50),
        "final_done_bytes": {
            "min": _whole(done_min),
            "p50": _whole(done_p50),
            "max": _whole(done_max),
            "cv": _cv(done),
        },
        "finish_elapsed_ms": {
            "min": _whole(fin_min),
            "p50": _whole(fin_p50),
            "max": _whole(fin_max),
        },
        "persistent_clients": counts.get(CLASS_PERSISTENT),
        "transient_clients": counts.get(CLASS_TRANSIENT),
    }


def _rank_value(rank: str):
    return int(rank) if rank.isdigit() else rank


def _kubernetes_names(
    service: str, workers: Mapping[str, Tuple[str, str]]
) -> Tuple[Optional[str], Optional[str]]:
    host = service
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    node_pod = workers.get(host) if host else None
    return node_pod if node_pod else (None, None)


def _client_row(
    summary: ClientSummary,
    domain: DomainAnalysis,
    steady: SteadyAnalysis,
    workers: Mapping[str, Tuple[str, str]],
) -> list:
    client = summary.client
    node, pod = _kubernetes_names(client.service, workers)
    has_samples = summary.sample_count > 0
    detail = steady.clients.get(client) or ClientSteady(client)
    first = summary.first_progress_interval
    return [
        client.label,
        _rank_value(client.rank),
        client.service or None,
        node,
        pod,
        detail.classification,
        _bytes(summary.mean_mib_s) if has_samples else None,
        _bytes(summary.median_mib_s) if has_samples else None,
        (
            round(summary.relative_to_fleet_median, 6)
            if summary.relative_median_count
            else None
        ),
        _bytes(detail.steady_mean_mib_s),
        _rounded(detail.steady_ratio, 6),
        _rounded(detail.worst_5s_ratio, 6),
        detail.worst_5s_ms,
        _rounded(detail.worst_30s_ratio, 6),
        detail.worst_30s_ms,
        _rounded(detail.short_5s_share, 4),
        summary.final_done_bytes,
        summary.finish_elapsed_ms,
        summary.sample_count,
        summary.missing_count,
        summary.invalid_count,
        detail.short_5s,
        detail.windows_5s,
        None if first is None else _ms(domain.interval_times[first]),
    ]


def _client_series(
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    steady: SteadyAnalysis,
    ordered: Sequence[LiveClientId],
    level: str,
    selection_limits: Tuple[int, int],
    sidecar: Tuple[Optional[str], str, Optional[str]],
) -> Optional[dict]:
    if level == LEVEL_SELECTED:
        return _selected_series(analysis, domain, steady, selection_limits)
    if level == LEVEL_FULL:
        return _write_sidecar(analysis, domain, ordered, *sidecar)
    return None


def _selected_series(
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    steady: SteadyAnalysis,
    selection_limits: Tuple[int, int],
) -> dict:
    max_lines, max_rows = selection_limits
    chosen = list(
        dict.fromkeys(
            [
                *classified_clients(steady, max_lines),
                *select_heatmap_rows(steady, max_rows),
            ]
        )
    )
    detail = collect_selected_client_detail(analysis, domain, chosen)
    return {
        "clients": [client.label for client in chosen],
        "elapsed_ms": [_ms(sec) for sec in domain.interval_times],
        "bytes_per_s": [
            [_bytes(rate) for rate in detail.rates_by_client[client]]
            for client in chosen
        ],
    }


def _sidecar_name(prefix: str, domain: DomainAnalysis) -> str:
    key = domain.key
    parts = [prefix, key.phase.lower()]
    if key.mix_type:
        parts.append(re.sub(r"[^a-z0-9]+", "-", key.mix_type.lower()).strip("-"))
    if key.occurrence:
        parts.append(f"occ{key.occurrence}")
    return ".".join(parts) + ".jsonl.zst"


def _write_sidecar(
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    ordered: Sequence[LiveClientId],
    sidecar_dir: Optional[str],
    sidecar_prefix: str,
    execution_id: Optional[str],
) -> dict:
    if not sidecar_dir:
        raise ValueError("live detail 'full' requires a sidecar directory")
    os.makedirs(sidecar_dir, exist_ok=True)
    name = _sidecar_name(sidecar_prefix, domain)
    header = {
        "schema": LIVE_SCHEMA,
        "schema_version": LIVE_SCHEMA_VERSION,
        "execution_id": execution_id,
        "phase": domain.key.phase.lower(),
        "mix_type": domain.key.mix_type,
        "occurrence": domain.key.occurrence,
        "clients": [client.label for client in ordered],
    }
    try:
        with SidecarWriter(os.path.join(sidecar_dir, name), header) as writer:
            for interval, rates, deltas in _iter_interval_samples(
                analysis, domain, ordered
            ):
                writer.write_interval(
                    _ms(domain.interval_times[interval]), rates, deltas
                )
    except (OSError, ValueError, csv.Error, UnicodeError) as exc:
        raise LiveCSVError(f"cannot write live sidecar {name}: {exc}") from exc
    return {
        "path": name,
        "sha256": writer.sha256,
        "size_bytes": writer.size_bytes,
        "clients": len(ordered),
        "intervals": len(domain.interval_times),
    }


class _IntervalSamples:
    """One interval's per-client rates and DoneBytes, and each client's last
    emitted DoneBytes (the base of the next delta)."""

    def __init__(self, count: int) -> None:
        self.count = count
        self.last_done: List[Optional[int]] = [None] * count
        self.rates: List[Optional[int]] = [None] * count
        self.done: List[Optional[int]] = [None] * count

    def emit(self) -> Tuple[List[Optional[int]], List[Optional[int]]]:
        """Return (bytes/s, DoneBytes deltas) and start the next interval."""
        deltas: List[Optional[int]] = [None] * self.count
        for position, done_bytes in enumerate(self.done):
            if done_bytes is not None:
                deltas[position] = done_bytes - (self.last_done[position] or 0)
                self.last_done[position] = done_bytes
        rates = self.rates
        self.rates = [None] * self.count
        self.done = [None] * self.count
        return rates, deltas


def _iter_interval_samples(
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    ordered: Sequence[LiveClientId],
):
    """Yield (interval, per-client bytes/s, per-client DoneBytes delta) for
    every interval in O(clients) memory. A delta is relative to the client's
    previous sample (its first is absolute); None means no sample."""
    index = {client: position for position, client in enumerate(ordered)}
    previous: Dict[LiveClientId, PreviousSample] = {}
    samples = _IntervalSamples(len(ordered))
    pending = 0
    for parsed in iter_live_rows(analysis):
        client, done_bytes = parsed.client, parsed.done_bytes
        if parsed.key != domain.key or parsed.rank == TOTAL_RANK:
            continue
        if client not in index or done_bytes is None:
            continue
        interval = domain.runtime_to_interval.get(parsed.runtime_ms)
        if interval is None or interval < pending:
            continue
        while pending < interval:
            yield (pending, *samples.emit())
            pending += 1
        rate = derive_rate(
            previous.get(client), done_bytes, parsed.runtime_ms, interval
        )
        samples.rates[index[client]] = _bytes(rate)
        samples.done[index[client]] = done_bytes
        previous[client] = PreviousSample(done_bytes, parsed.runtime_ms, interval)
    while pending < len(domain.interval_times):
        yield (pending, *samples.emit())
        pending += 1


# -- rebuilding the analysis from a results document -------------------------


def live_domain_key(live: dict) -> LiveDomainKey:
    """The analysis key of a live object (live CSV phases are upper case)."""
    domain = live["domain"]
    return LiveDomainKey(
        domain["phase"].upper(), domain["mix_type"], domain["occurrence"]
    )


def document_phase_context(live: dict) -> PhaseContext:
    """The live object's phase context, or the empty one without a result pair."""
    context = live.get("phase_context")
    if context is None:
        return NO_CONTEXT
    return PhaseContext(
        context["first_ms"],
        context["last_ms"],
        context["block_size_bytes"],
        context["final_avg_latency_us"],
    )


def _document_clients(live: dict, labels: Sequence[str]) -> List[LiveClientId]:
    """Client identities of the sidecar columns, from ``clients_summary``."""
    summary = live["clients_summary"]
    columns = summary["columns"]
    label, rank, service = (columns.index(name) for name in CLIENT_COLUMNS[:3])
    by_label = {
        row[label]: LiveClientId(str(row[rank]), row[service] or "")
        for row in summary["rows"]
    }
    try:
        return [by_label[name] for name in labels]
    except KeyError as exc:
        raise ValueError(f"sidecar client {exc} is not in clients_summary") from exc


def _total_row(key: LiveDomainKey, series: dict, index: int) -> ParsedRow:
    """The Rank=Total row of one aggregate sample, its ISO Date rebuilt."""
    elapsed = series["elapsed_ms"][index]
    # Schema 1.0 lacks start_time, clock_skew_ms and entries_latency_avg_us.
    start = series.get("start_time")
    skews = series.get("clock_skew_ms")
    skew = skews[index] if skews else None
    iso = ""
    if start is not None and skew is not None:
        iso = live_timestamp(start, elapsed - series["elapsed_ms"][0] + skew) or ""
    rate = series["bytes_per_s"][index]
    entries = series.get("entries_latency_avg_us")
    return ParsedRow(
        key,
        elapsed,
        iso,
        TOTAL_RANK,
        None,
        None,
        None if rate is None else rate / MIB,
        series["ops_per_s"][index],
        series["io_latency_avg_us"][index],
        entries[index] if entries else None,
    )


def _client_rows(
    key: LiveDomainKey,
    elapsed: int,
    clients: Sequence[LiveClientId],
    done: List[int],
    deltas: Sequence[Optional[int]],
) -> Iterator[ParsedRow]:
    """Client rows of one interval; ``done`` accumulates each DoneBytes."""
    for position, delta in enumerate(deltas):
        if delta is None:
            continue
        done[position] += delta
        client = clients[position]
        yield ParsedRow(
            key,
            elapsed,
            "",
            client.rank,
            client,
            done[position],
            None,
            None,
            None,
            None,
        )


def _document_rows(live: dict, series) -> Iterator[ParsedRow]:
    """The parsed rows of one domain, from its live object and sidecar.

    Summing ``done_bytes_delta`` restores each client's DoneBytes; every
    interval keeps a row, so the analysis sees the live CSV's intervals.
    """
    key = live_domain_key(live)
    clients = _document_clients(live, series.clients)
    aggregate = live["aggregate_series"]
    totals = {elapsed: index for index, elapsed in enumerate(aggregate["elapsed_ms"])}
    done = [0] * len(clients)
    for elapsed, deltas in series.rows:
        index = totals.get(elapsed)
        if index is not None:
            yield _total_row(key, aggregate, index)
        emitted = index is not None
        for row in _client_rows(key, elapsed, clients, done, deltas):
            emitted = True
            yield row
        if not emitted:
            yield ParsedRow(
                key, elapsed, "", TOTAL_RANK, None, None, None, None, None, None
            )


def analyze_document_live(
    live: dict, sidecar: str, metadata: LiveFileMetadata
) -> Tuple[LiveAnalysis, Optional[DomainAnalysis]]:
    """Rebuild one domain's first-pass analysis from a ``full`` live object.

    Later passes re-read the sidecar, as they would re-read the live CSV.
    """
    # Deferred: only rendering from a document reads sidecars.
    from lib.results_document import (  # pylint: disable=import-outside-toplevel
        open_client_series,
    )

    analysis = analyze_live_csv(
        metadata,
        lambda: _document_rows(
            live, open_client_series(Path(sidecar), DONE_BYTES_DELTA)
        ),
    )
    return analysis, analysis.domains.get(live_domain_key(live))


def document_steady(
    live: dict, shortfall_percent: float, min_slow_windows: int
) -> SteadyAnalysis:
    """The steady window of a live object, without its window series."""
    steady = live["steady"]
    interval_ms = steady.get("interval_ms")
    if interval_ms is None:
        # Schema 1.0: the interval the fairness windows were measured at.
        interval = (live.get("fairness") or {}).get("interval") or {}
        interval_ms = interval.get("window_ms") or live.get("configured_interval_ms")
    return SteadyAnalysis(
        window=SteadyWindow(
            start_ms=steady["start_ms"],
            end_ms=steady["end_ms"],
            reason=steady["reason"],
        ),
        context=document_phase_context(live),
        interval_ms=interval_ms,
        shortfall_percent=shortfall_percent,
        min_slow_windows=min_slow_windows,
    )


def document_aggregate_domain(live: dict) -> DomainAnalysis:
    """A domain holding only the live object's aggregate samples."""
    key = live_domain_key(live)
    series = live["aggregate_series"]
    domain = DomainAnalysis(key=key)
    for index in range(len(series["elapsed_ms"])):
        row = _total_row(key, series, index)
        domain.aggregate_points.append(
            AggregatePoint(
                interval=index,
                elapsed_sec=row.runtime_ms / 1000.0,
                iso_timestamp=row.iso_timestamp,
                throughput_mib_s=row.mib_s,
                iops=row.iops,
                io_latency_us=row.io_latency_us,
                entries_latency_us=row.entries_latency_us,
            )
        )
    return domain
