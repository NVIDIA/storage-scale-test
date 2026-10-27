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
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from lib.elbencho_live_report import (
    MIB,
    TOTAL_RANK,
    ClientSummary,
    DomainAnalysis,
    LiveAnalysis,
    LiveClientId,
    LiveFileMetadata,
    PreviousSample,
    analyze_live_csv,
    collect_selected_client_detail,
    derive_rate,
    iter_parsed_rows,
    live_csv_reader,
    percentile_from_sorted,
    select_clients,
    select_heatmap_clients,
)
from lib.filesystem_results import (
    LIVE_SCHEMA,
    LIVE_SCHEMA_VERSION,
    SidecarWriter,
    file_record,
    mib_s_to_bytes_per_s,
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
    "mean_bytes_per_s",
    "median_bytes_per_s",
    "mean_ratio_to_fleet_median",
    "final_done_bytes",
    "finish_elapsed_ms",
    "underperform_intervals",
    "longest_underperform_streak",
    "worst_z",
    "worst_elapsed_ms",
    "valid_samples",
    "missing_samples",
    "invalid_samples",
]
DEFAULT_DIAGNOSTICS = ("missing_samples", "counter_resets", "non_adjacent_samples")
ADDITIONAL_DOMAINS = "additional_domains"


class LiveCSVError(Exception):
    """Raised when a live CSV cannot be read or analyzed."""


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
    z_threshold: float,
    selection_limits: Tuple[int, int],
    min_underperform_segments: int,
    sidecar_dir: Optional[str],
    sidecar_prefix: str,
    execution_id: Optional[str],
    workers: Optional[Mapping[str, Tuple[str, str]]],
) -> Dict[str, dict]:
    """Analyze one live CSV and return live objects keyed by lowercase phase."""
    try:
        source = file_record(live_csv_path, relative_source_path)
        analysis = analyze_live_csv(
            LiveFileMetadata(live_csv_path, "", 0, 0, 0, ""), z_threshold
        )
    except (OSError, ValueError, csv.Error, UnicodeError) as exc:
        raise LiveCSVError(f"cannot read live CSV {live_csv_path}: {exc}") from exc
    by_phase: Dict[str, List[DomainAnalysis]] = {}
    for domain in analysis.domains.values():
        if domain.aggregate_points or domain.clients:
            by_phase.setdefault(domain.key.phase.lower(), []).append(domain)
    result: Dict[str, dict] = {}
    for phase, domains in by_phase.items():
        live = _build_live_object(
            analysis,
            domains[0],
            source,
            level,
            configured_interval_ms,
            selection_limits,
            min_underperform_segments,
            (sidecar_dir, sidecar_prefix, execution_id),
            workers or {},
        )
        if len(domains) > 1:
            live["diagnostics"][ADDITIONAL_DOMAINS] = len(domains) - 1
            live["diagnostics"] = dict(sorted(live["diagnostics"].items()))
        result[phase] = live
    return result


def _build_live_object(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    source: dict,
    level: str,
    configured_interval_ms: Optional[int],
    selection_limits: Tuple[int, int],
    min_underperform_segments: int,
    sidecar: Tuple[Optional[str], str, Optional[str]],
    workers: Mapping[str, Tuple[str, str]],
) -> dict:
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
        "intervals": len(domain.interval_times),
        "clients": len(ordered),
        "aggregate_series": _aggregate_series(domain),
        "fleet_series": _fleet_series(domain),
        "distribution": _distribution(summaries),
        "clients_summary": {
            "columns": list(CLIENT_COLUMNS),
            "rows": [_client_row(item, workers) for item in summaries],
        },
        "client_series": _client_series(
            analysis,
            domain,
            ordered,
            level,
            selection_limits,
            min_underperform_segments,
            sidecar,
        ),
        "diagnostics": dict(sorted(diagnostics.items())),
    }


def _rank_key(client: LiveClientId) -> tuple:
    numeric = client.rank.isdigit()
    return (
        0 if numeric else 1,
        int(client.rank) if numeric else 0,
        client.rank,
        client.service,
    )


def _ordered_clients(domain: DomainAnalysis) -> List[LiveClientId]:
    return sorted(domain.clients, key=_rank_key)


def _ms(seconds: float) -> int:
    return int(round(seconds * 1000.0))


def _bytes(mib_s: Optional[float]) -> Optional[int]:
    return mib_s_to_bytes_per_s(mib_s)


def _aggregate_series(domain: DomainAnalysis) -> dict:
    points = domain.aggregate_points
    return {
        "elapsed_ms": [_ms(point.elapsed_sec) for point in points],
        "bytes_per_s": [_bytes(point.throughput_mib_s) for point in points],
        "ops_per_s": [round_ops(point.iops) for point in points],
    }


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


def _distribution(summaries: Sequence[ClientSummary]) -> dict:
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
        "underperforming_clients": sum(
            1 for item in summaries if item.underperform_count > 0
        ),
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


def _client_row(summary: ClientSummary, workers: Mapping[str, Tuple[str, str]]) -> list:
    client = summary.client
    node, pod = _kubernetes_names(client.service, workers)
    has_samples = summary.sample_count > 0
    has_worst = summary.worst_interval >= 0
    return [
        client.label,
        _rank_value(client.rank),
        client.service or None,
        node,
        pod,
        _bytes(summary.mean_mib_s) if has_samples else None,
        _bytes(summary.median_mib_s) if has_samples else None,
        (
            round(summary.relative_to_fleet_median, 6)
            if summary.relative_median_count
            else None
        ),
        summary.final_done_bytes,
        summary.finish_elapsed_ms,
        summary.underperform_count,
        summary.longest_underperform_streak,
        round(summary.worst_z_score, 4) if has_worst else None,
        _ms(summary.worst_elapsed_sec) if has_worst else None,
        summary.sample_count,
        summary.missing_count,
        summary.invalid_count,
    ]


def _client_series(
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    ordered: Sequence[LiveClientId],
    level: str,
    selection_limits: Tuple[int, int],
    min_underperform_segments: int,
    sidecar: Tuple[Optional[str], str, Optional[str]],
) -> Optional[dict]:
    if level == LEVEL_SELECTED:
        return _selected_series(
            analysis, domain, selection_limits, min_underperform_segments
        )
    if level == LEVEL_FULL:
        return _write_sidecar(analysis, domain, ordered, *sidecar)
    return None


def _selected_series(
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    selection_limits: Tuple[int, int],
    min_underperform_segments: int,
) -> dict:
    max_lines, max_rows = selection_limits
    prioritized = select_clients(
        domain, min_underperform_segments, max(max_lines, max_rows)
    )
    heatmap = select_heatmap_clients(domain, prioritized, max_rows)
    chosen = list(dict.fromkeys([*prioritized[:max_lines], *heatmap]))
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
        "clients": [client.label for client in ordered],
    }
    try:
        with SidecarWriter(os.path.join(sidecar_dir, name), header) as writer:
            for interval, rates in _iter_interval_rates(analysis, domain, ordered):
                writer.write_interval(_ms(domain.interval_times[interval]), rates)
    except (OSError, ValueError, csv.Error, UnicodeError) as exc:
        raise LiveCSVError(f"cannot write live sidecar {name}: {exc}") from exc
    return {
        "path": name,
        "sha256": writer.sha256,
        "size_bytes": writer.size_bytes,
        "clients": len(ordered),
        "intervals": len(domain.interval_times),
    }


def _iter_interval_rates(
    analysis: LiveAnalysis,
    domain: DomainAnalysis,
    ordered: Sequence[LiveClientId],
):
    """Yield (interval, per-client bytes/s) for every interval, O(clients) memory."""
    index = {client: position for position, client in enumerate(ordered)}
    count = len(domain.interval_times)
    previous: Dict[LiveClientId, PreviousSample] = {}
    pending = 0
    current: List[Optional[int]] = [None] * len(ordered)
    with open(analysis.metadata.path, "r", encoding="utf-8-sig", newline="") as handle:
        reader, columns = live_csv_reader(handle, analysis.metadata.path)
        for parsed in iter_parsed_rows(reader, columns):
            key, runtime_ms, _, rank, client, done_bytes, _, _ = parsed
            if key != domain.key or rank == TOTAL_RANK or client not in index:
                continue
            if done_bytes is None or runtime_ms not in domain.runtime_to_interval:
                continue
            interval = domain.runtime_to_interval[runtime_ms]
            while pending < interval:
                yield pending, current
                pending += 1
                current = [None] * len(ordered)
            rate = derive_rate(previous.get(client), done_bytes, runtime_ms, interval)
            current[index[client]] = _bytes(rate)
            previous[client] = PreviousSample(done_bytes, runtime_ms, interval)
    while pending < count:
        yield pending, current
        pending += 1
        current = [None] * len(ordered)
