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

"""Tests for Warp JSON cache histogram integrity."""

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "utils" / "extract-warp.py"
_SPEC = importlib.util.spec_from_file_location("extract_warp_histogram", _MODULE_PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


class _FakeZstd:
    """Small decompressor stub that returns the cache payload unchanged."""

    class ZstdDecompressor:
        """Stand-in for zstandard's decompressor."""

        @staticmethod
        def decompress(payload):
            return payload


@pytest.mark.parametrize(
    "histogram_path", ["ttfb_histogram", "ttfb_histograms_by_client"]
)
@pytest.mark.parametrize(
    "histogram_json, error_message",
    [
        ('{"2.4": 5, "2.40": 7}', "Duplicate histogram bucket"),
        ('{"2.4": 5, "2.4": 7}', "Duplicate JSON object key"),
    ],
)
def test_cached_histograms_reject_duplicate_buckets(
    tmp_path, monkeypatch, histogram_path, histogram_json, error_message
):
    """Overall and per-client caches reject literal and normalized collisions."""
    monkeypatch.setattr(_MODULE, "zstd", _FakeZstd)
    if histogram_path == "ttfb_histogram":
        histogram_fields = f'"ttfb_histogram": {histogram_json}'
    else:
        histogram_fields = (
            f'"ttfb_histograms_by_client": {{"client-a": {histogram_json}}}'
        )

    payload = (
        '{"version": 1, "datestamp": "20260929Z000000", "metrics": [{'
        '"nodes": 1, "obj_size": "1MiB", "threads": 1, "med_lat_ms": 1,'
        '"p99_lat_ms": 1, "max_lat_ms": 1, "max_bw_mib": 1, "med_bw_mib": 1,'
        '"med_bw_gbps": 1, "min_bw_mib": 1, "avg_rate_obj": 1, "med_rate_obj": 1,'
        f"{histogram_fields}"
        "}]} "
    )
    cache = tmp_path / "metrics.json.zst"
    cache.write_bytes(payload.encode("utf-8"))

    with pytest.raises(ValueError, match=error_message):
        _MODULE.read_json(str(cache))


def test_raw_warp_histogram_accumulates_equivalent_buckets():
    """Repeated raw bucket values retain every sample count."""
    buckets = [
        {"millis": "2.4", "n": 5},
        {"millis": "2.40", "n": 7},
    ]

    assert _MODULE._decode_warp_histogram_buckets(  # pylint: disable=protected-access
        buckets
    ) == {2.4: 12}


@pytest.mark.parametrize(
    "histogram, error_message",
    [
        ({"NaN": 1}, "Invalid histogram bucket"),
        ({"-1": 1}, "Invalid histogram bucket"),
        ({"1": -1}, "Invalid histogram count"),
        ({"1": 1.5}, "Invalid histogram count"),
        ({"1": True}, "Invalid histogram count"),
    ],
)
def test_cached_histogram_rejects_invalid_values(histogram, error_message):
    """Cached buckets and counts must be finite, nonnegative, and integral."""
    with pytest.raises(ValueError, match=error_message):
        _MODULE._decode_cached_histogram(histogram)  # pylint: disable=protected-access
