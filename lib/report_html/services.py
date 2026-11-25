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

"""Per-service final results of one IO phase (elbencho 3.4 ``--svcstats``):
sorted final latency and completion-time charts. They need only the results
document, not live CSV data. At most ``SERVICE_MAX_POINTS`` services are drawn
(evenly spaced in rank, ends kept); host names appear on hover."""

from __future__ import annotations

from typing import Callable

from lib import elbencho_services as services_lib
from lib.report_html import model
from lib.report_html.common import (
    FMT_MS,
    LATENCY_MS,
    MS_PER_S,
    P50_KEY,
    P99_KEY,
    US_PER_MS,
    axis,
    line_spec,
    series,
)
from lib.report_html.model import Figure
from lib.report_html.reduce import scaled, sig

MIN_SERVICES = 2
SERVICE_MAX_POINTS = 300
RANK_BY_P99 = "service rank (lowest p99 first)"
RANK_BY_TIME = "service rank (fastest first)"
COMPLETION_S = "completion (s)"
_ELAPSED_KEY = "elapsed_ms"
_HOVER = " (hover for the host)"

# ``make(template, title, caption, spec)`` builds a figure of the domain.
FigureMaker = Callable[[str, str, str, dict], Figure]


def _spaced(count: int, limit: int) -> list[int]:
    """At most ``limit`` evenly spaced indices of ``count`` items, ends kept."""
    if count <= limit:
        return list(range(count))
    return sorted({round(i * (count - 1) / (limit - 1)) for i in range(limit)})


def _ranked(entries: list, key: Callable) -> list:
    """``entries`` ascending by ``key`` (host breaks ties), thinned to the limit."""
    ordered = sorted(entries, key=lambda e: (key(e), str(e.get("host"))))
    return [ordered[i] for i in _spaced(len(ordered), SERVICE_MAX_POINTS)]


def _thinned(shown: list, entries: list) -> str:
    if len(shown) == len(entries):
        return ""
    return f" The {len(shown)} services drawn are evenly spaced in rank."


def _rank_series(name: str, color: str, entries: list, value: Callable) -> dict:
    points = [[rank, sig(value(e))] for rank, e in enumerate(entries, 1)]
    return {
        **series(name, color, points),
        "names": [str(e.get("host")) for e in entries],
    }


def _p99_us(entry):
    return services_lib.latency_of(entry, P99_KEY)


def _latency_ms(key: str) -> Callable:
    return lambda entry: scaled(services_lib.latency_of(entry, key), US_PER_MS)


def _seconds(entry):
    return scaled(entry[_ELAPSED_KEY], MS_PER_S)


def latency_figure(
    entries: list, fleet_p99_us, label: str, make: FigureMaker
) -> Figure | None:
    """Per-service final p99 and p50 (ms) ordered by p99, with the phase p99."""
    entries = [e for e in entries if _p99_us(e) is not None]
    if len(entries) < MIN_SERVICES:
        return None
    shown = _ranked(entries, _p99_us)
    lines = [
        _rank_series("service p99", model.P99, shown, _latency_ms(P99_KEY)),
        _rank_series("service p50", model.P50, shown, _latency_ms(P50_KEY)),
    ]
    caption = (
        f"Final IO latency of {len(entries)} services ordered by p99"
        f"{_HOVER}; the p50 line follows the same order."
    )
    if fleet_p99_us is not None:
        fleet = sig(fleet_p99_us / US_PER_MS)
        points = [[1, fleet], [len(shown), fleet]]
        lines.append(series("phase p99", model.NEUTRAL, points, True))
        caption += " Dashed: the phase's overall p99."
    spec = line_spec(
        axis(RANK_BY_P99, fmt="num", integer=True),
        axis(LATENCY_MS, fmt=FMT_MS, min=0),
        lines,
    )
    title = f"{label} per-service final latency"
    return make(model.SERVICE_LATENCY, title, caption + _thinned(shown, entries), spec)


def completion_figure(entries: list, label: str, make: FigureMaker) -> Figure | None:
    """Per-service completion time (s), shortest first."""
    entries = [e for e in entries if e.get(_ELAPSED_KEY) is not None]
    if len(entries) < MIN_SERVICES:
        return None
    shown = _ranked(entries, lambda e: e[_ELAPSED_KEY])
    line = _rank_series("completion", model.category_color(5), shown, _seconds)
    caption = (
        f"Time each of {len(entries)} services took to finish, shortest "
        f"first{_HOVER}. A long tail means stragglers held the phase open."
    )
    spec = line_spec(
        axis(RANK_BY_TIME, fmt="num", integer=True),
        axis(COMPLETION_S, fmt="s", min=0),
        [line],
    )
    title = f"{label} per-service completion time"
    return make(
        model.SERVICE_COMPLETION, title, caption + _thinned(shown, entries), spec
    )


def service_figures(phase: dict, label: str, make: FigureMaker) -> list[Figure]:
    """The phase's per-service figures; none without two or more services."""
    entries = services_lib.instances(phase.get("services"))
    if len(entries) < MIN_SERVICES:
        return []
    fleet = (phase.get("latency_us") or {}).get(P99_KEY)
    figures = [
        latency_figure(entries, fleet, label, make),
        completion_figure(entries, label, make),
    ]
    return [f for f in figures if f]
