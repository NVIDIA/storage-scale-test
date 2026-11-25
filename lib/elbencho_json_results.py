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

"""Read elbencho ``--jsonfile`` final results (elbencho 3.4 and later).

elbencho appends one JSON object per phase. Every value is a quoted string
and zero counters are omitted, so an absent rate or latency reads as None.
Histogram buckets are keyed by their upper bound in microseconds, matching
the ``.out`` "lat hist" lines (``"23-24us"`` is the ``.out`` bucket ``24``).
The open-ended top bucket (``"N+us"``) is keyed by its lower bound ``N``.

Per-service results come from ``--svcstats``: ``last_done.<group>`` holds
``instances[]`` (``elapsed``, ``lat``, ``percent``) and the ``iops`` and
``bytes/s`` spread (``minmax``). The group is ``services``, or ``clients``
and ``servers`` in netbench mode.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

SERVICES = "services"
SERVICE_GROUPS = (SERVICES, "clients", "servers")
_RANGE_RE = re.compile(r"^(\d+(?:\.\d+)?)(?:-(\d+(?:\.\d+)?))?(\+)?us$")
_BYTES_PER_S = "bytes/s"
_ELAPSED = "elapsed_time_ms"


def _number(value: Any) -> Optional[float]:
    """A quoted elbencho number as a finite float; None when absent or bad."""
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any) -> Optional[int]:
    number = _number(value)
    return int(number) if number is not None and number >= 0 else None


def _flag(value: Any) -> Optional[bool]:
    text = str(value).strip().lower() if value is not None else ""
    if text in ("true", "1"):
        return True
    if text in ("false", "0"):
        return False
    return None


def _child(tree: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = tree.get(key)
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class LatencyStats:
    """One latency subtree (``entries`` or ``IO``); values in microseconds."""

    min_us: Optional[float] = None
    avg_us: Optional[float] = None
    max_us: Optional[float] = None
    # Percentile label ("1", "50", "75", "99", "99.9", ...) -> upper bound.
    percentiles_us: Dict[str, float] = field(default_factory=dict)
    # Bucket upper bound -> count.
    histogram_us: Dict[float, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Counters:
    """``first_done`` or ``last_done`` counters (None when omitted)."""

    elapsed_ms: Optional[int] = None
    iops: Optional[float] = None
    bytes_per_s: Optional[float] = None
    entries_per_s: Optional[float] = None
    bytes: Optional[int] = None
    entries: Optional[int] = None
    cpu_percent: Optional[float] = None
    # Netbench receive (and --rwmixpct read) share of the rates.
    rwmix_read_iops: Optional[float] = None
    rwmix_read_bytes_per_s: Optional[float] = None


@dataclass(frozen=True)
class RateSpread:
    """``--svcstats minmax``: per-service rate min/avg/max and their hosts."""

    min: Optional[float]
    avg: Optional[float]
    max: Optional[float]
    min_host: Optional[str]
    max_host: Optional[str]


@dataclass(frozen=True)
class ServiceResult:
    """One ``instances[]`` entry of a service group."""

    host: str
    elapsed_ms: Optional[int]
    io_latency: Optional[LatencyStats]
    entries_latency: Optional[LatencyStats]


@dataclass(frozen=True)
class ServiceGroup:
    """A ``last_done`` service group (``services``, ``clients``, ``servers``)."""

    instances: Tuple[ServiceResult, ...] = ()
    iops: Optional[RateSpread] = None
    bytes_per_s: Optional[RateSpread] = None


@dataclass(frozen=True)
class PhaseResult:  # pylint: disable=too-many-instance-attributes
    """One phase object of an elbencho ``--jsonfile``."""

    phase_type: str
    label: str
    iso_start_date: str
    version: str
    command: str
    direct_io: Optional[bool]
    random_offsets: Optional[bool]
    file_size: Optional[int]
    block_size: Optional[int]
    first: Counters
    last: Counters
    io_latency: Optional[LatencyStats]
    entries_latency: Optional[LatencyStats]
    service_groups: Dict[str, ServiceGroup] = field(default_factory=dict)

    @property
    def services(self) -> Optional[ServiceGroup]:
        """The per-service results of a filesystem phase, if recorded."""
        return self.service_groups.get(SERVICES)


def _histogram(tree: Mapping[str, Any]) -> Dict[float, int]:
    histogram: Dict[float, int] = {}
    buckets = _child(tree, "histogram").get("buckets")
    for bucket in buckets if isinstance(buckets, list) else ():
        if not isinstance(bucket, Mapping):
            continue
        match = _RANGE_RE.match(str(bucket.get("range", "")))
        count = _integer(bucket.get("count"))
        if match is None or count is None:
            continue
        upper = float(match.group(2) or match.group(1))
        histogram[upper] = histogram.get(upper, 0) + count
    return histogram


def _latency(tree: Mapping[str, Any]) -> Optional[LatencyStats]:
    if not tree:
        return None
    percentiles = {
        str(label): number
        for label, value in _child(tree, "percentiles_us").items()
        if (number := _number(value)) is not None
    }
    return LatencyStats(
        _number(tree.get("min_us")),
        _number(tree.get("avg_us")),
        _number(tree.get("max_us")),
        percentiles,
        _histogram(tree),
    )


def _counters(tree: Mapping[str, Any]) -> Counters:
    read_mix = _child(tree, "rwmix_read")
    return Counters(
        elapsed_ms=_integer(tree.get(_ELAPSED)),
        iops=_number(tree.get("iops")),
        bytes_per_s=_number(tree.get(_BYTES_PER_S)),
        entries_per_s=_number(tree.get("entries/s")),
        bytes=_integer(tree.get("bytes")),
        entries=_integer(tree.get("entries")),
        cpu_percent=_number(tree.get("cpu%")),
        rwmix_read_iops=_number(read_mix.get("iops")),
        rwmix_read_bytes_per_s=_number(read_mix.get(_BYTES_PER_S)),
    )


def _spread(tree: Mapping[str, Any]) -> Optional[RateSpread]:
    if not tree:
        return None
    min_host, max_host = tree.get("min_host"), tree.get("max_host")
    return RateSpread(
        _number(tree.get("min")),
        _number(tree.get("avg")),
        _number(tree.get("max")),
        str(min_host) if min_host is not None else None,
        str(max_host) if max_host is not None else None,
    )


def _service(tree: Mapping[str, Any]) -> ServiceResult:
    latency = _child(tree, "latency")
    return ServiceResult(
        host=str(tree.get("host", "")),
        elapsed_ms=_integer(tree.get(_ELAPSED)),
        io_latency=_latency(_child(latency, "IO")),
        entries_latency=_latency(_child(latency, "entries")),
    )


def _service_groups(last_done: Mapping[str, Any]) -> Dict[str, ServiceGroup]:
    groups = {}
    for name in SERVICE_GROUPS:
        tree = _child(last_done, name)
        if not tree:
            continue
        instances = tree.get("instances")
        groups[name] = ServiceGroup(
            tuple(
                _service(item)
                for item in (instances if isinstance(instances, list) else ())
                if isinstance(item, Mapping)
            ),
            _spread(_child(tree, "iops")),
            _spread(_child(tree, _BYTES_PER_S)),
        )
    return groups


def phase_result(record: Mapping[str, Any]) -> PhaseResult:
    """Decode one phase object."""
    config = _child(record, "config")
    last_done = _child(record, "last_done")
    latency = _child(last_done, "latency")
    return PhaseResult(
        phase_type=str(record.get("phase_type", "")),
        label=str(record.get("label", "")),
        iso_start_date=str(record.get("iso_start_date", "")),
        version=str(config.get("version", "")),
        command=str(config.get("command", "")),
        direct_io=_flag(config.get("direct_io")),
        random_offsets=_flag(config.get("random_offsets")),
        file_size=_integer(config.get("file_size")),
        block_size=_integer(config.get("block_size")),
        first=_counters(_child(record, "first_done")),
        last=_counters(last_done),
        io_latency=_latency(_child(latency, "IO")),
        entries_latency=_latency(_child(latency, "entries")),
        service_groups=_service_groups(last_done),
    )


def parse_phase_results(text: str) -> List[PhaseResult]:
    """Decode every phase object of a ``--jsonfile`` text, in file order.

    Objects may be separated by newlines or blank lines, or be adjacent.
    Raises ValueError for malformed JSON or a non-object value.
    """
    decoder = json.JSONDecoder()
    phases: List[PhaseResult] = []
    position, end = 0, len(text)
    while True:
        while position < end and text[position].isspace():
            position += 1
        if position >= end:
            return phases
        record, position = decoder.raw_decode(text, position)
        if not isinstance(record, dict):
            raise ValueError("elbencho JSON result is not an object")
        phases.append(phase_result(record))


def read_phase_results(path: str) -> List[PhaseResult]:
    """Read and decode one ``--jsonfile`` (see parse_phase_results)."""
    with open(path, "r", encoding="utf-8") as handle:
        return parse_phase_results(handle.read())
