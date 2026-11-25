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

"""Readers of a phase's per-service final results (the results document's
``services`` object: ``instances``, ``ops_per_s``, ``bytes_per_s``). Every
function accepts None, which stands for a pre-3.4 result or a run without
``--svcstats``, and answers None."""

from __future__ import annotations

from typing import Any, Mapping, Optional, Tuple

Services = Optional[Mapping[str, Any]]


def instances(services: Services) -> list:
    """The per-service entries, or an empty list."""
    return list((services or {}).get("instances") or [])


def find_instance(services: Services, service: Optional[str]) -> Optional[Mapping]:
    """The entry of a live-CSV service ("host:port"), or None.

    An exact host string wins; otherwise a unique entry with the same host
    part (before the port) is taken, else None (no guess between several)."""
    if not service:
        return None
    entries = instances(services)
    for entry in entries:
        if entry.get("host") == service:
            return entry
    host = service.split(":", 1)[0]
    same = [e for e in entries if str(e.get("host") or "").split(":", 1)[0] == host]
    return same[0] if len(same) == 1 else None


def latency_of(entry: Optional[Mapping], key: str) -> Optional[float]:
    """One final IO latency statistic (us) of an entry, or None."""
    return ((entry or {}).get("latency_us") or {}).get(key)


def highest_p99(services: Services) -> Optional[Tuple[str, float]]:
    """(host, p99 us) of the service with the highest final p99, or None."""
    ranked = [
        (latency_of(e, "p99"), str(e.get("host")))
        for e in instances(services)
        if latency_of(e, "p99") is not None
    ]
    if not ranked:
        return None
    value, host = max(ranked, key=lambda item: item[0])
    return host, value


def rate_spread(services: Services, key: str) -> Optional[Mapping]:
    """The ``ops_per_s`` or ``bytes_per_s`` min/avg/max spread, or None."""
    spread = (services or {}).get(key)
    return spread if isinstance(spread, Mapping) else None


def spread_ratio(spread: Optional[Mapping]) -> Optional[float]:
    """Fastest over slowest host rate, or None without a positive minimum."""
    if not spread or not spread.get("min") or spread.get("max") is None:
        return None
    return spread["max"] / spread["min"]


def straggler_ratio(services: Services, fleet_p99: Optional[float]):
    """(host, p99 us, p99 / fleet p99) of the highest-p99 service, or None."""
    top = highest_p99(services)
    if top is None:
        return None
    ratio = top[1] / fleet_p99 if fleet_p99 else None
    return top[0], top[1], ratio
