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

"""Data reduction for the HTML report: rounding, decimation, heatmap cells.

Limits keep a large batch's report small: time series hold at most
``MAX_POINTS`` points, chart values ``SIG_FIGS`` significant figures, heatmaps
``HEATMAP_MAX_ROWS`` x ``HEATMAP_MAX_COLS`` one-byte cells, and client tables
``CLIENT_TABLE_MAX_ROWS`` rows.
"""

from __future__ import annotations

import base64
import math
from typing import Sequence

MAX_POINTS = 300
SIG_FIGS = 4
HEATMAP_MAX_ROWS = 64
HEATMAP_MAX_COLS = 160
CLIENT_TABLE_MAX_ROWS = 200
# Heatmap cells encode a client's share of the fleet median, clamped to
# [HEATMAP_LO, HEATMAP_HI], as 0..HEATMAP_TOP; HEATMAP_MISSING marks no sample.
HEATMAP_LO = 0.5
HEATMAP_HI = 1.5
HEATMAP_TOP = 254
HEATMAP_MISSING = 255


def sig(value, digits: int = SIG_FIGS):
    """Round to ``digits`` significant figures; integral results become int."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int) and abs(value) < 10**digits:
        return value
    if not math.isfinite(value):
        return None
    if value == 0:
        return 0
    rounded = float(f"{value:.{digits}g}")
    if rounded.is_integer() and abs(rounded) < 1e15:
        return int(rounded)
    return rounded


def scaled(value, divisor: float):
    """``value / divisor``, keeping None."""
    return None if value is None else value / divisor


def finite(value):
    """``value`` if it is a finite number, else None (NaN, inf, missing)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _bucket_extremes(points: list, start: int, end: int) -> list:
    chunk = range(start, end)
    low = min(chunk, key=lambda i: points[i][1])
    high = max(chunk, key=lambda i: points[i][1])
    return [points[i] for i in sorted({low, high})]


def decimate(xs: Sequence, ys: Sequence, limit: int = MAX_POINTS) -> list:
    """Min/max bucket decimation to at most ``limit`` [x, y] points.

    Each bucket keeps its lowest and highest sample, so peaks and dips
    survive; missing and non-finite samples are dropped. A ``limit`` of one
    keeps the highest sample.
    """
    points = [
        (x, y)
        for x, y in zip(xs, ys)
        if finite(x) is not None and finite(y) is not None
    ]
    if limit < 2:
        points = [max(points, key=lambda p: p[1])] if points and limit > 0 else []
    elif len(points) > limit:
        buckets = limit // 2
        size = len(points) / buckets
        kept = []
        for bucket in range(buckets):
            start, end = int(bucket * size), int((bucket + 1) * size)
            if end > start:
                kept.extend(_bucket_extremes(points, start, end))
        points = kept
    return [[sig(x), sig(y)] for x, y in points]


def heatmap_cell(ratios: Sequence) -> int:
    """One byte for the mean ratio of a cell's finite samples."""
    ratios = [r for r in ratios if finite(r) is not None]
    if not ratios:
        return HEATMAP_MISSING
    mean = sum(ratios) / len(ratios)
    clamped = min(max(mean, HEATMAP_LO), HEATMAP_HI)
    return int(round((clamped - HEATMAP_LO) / (HEATMAP_HI - HEATMAP_LO) * HEATMAP_TOP))


def heatmap_step(intervals: int) -> int:
    """Intervals merged into one heatmap column."""
    return max(1, -(-intervals // HEATMAP_MAX_COLS))


def encode_heatmap(rows: Sequence[Sequence], step: int) -> tuple[str, int]:
    """Base64 cells (row-major) and column count for per-interval ratios.

    ``rows`` holds one list of ratios (or None) per client; ``step``
    consecutive intervals merge into one column.
    """
    intervals = max((len(row) for row in rows), default=0)
    columns = -(-intervals // step) if intervals else 0
    cells = bytearray()
    for row in rows:
        for column in range(columns):
            window = row[column * step : (column + 1) * step]
            cells.append(heatmap_cell(window))
    return base64.b64encode(bytes(cells)).decode("ascii"), columns
