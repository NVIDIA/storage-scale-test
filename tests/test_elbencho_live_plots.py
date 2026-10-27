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

"""Tests for lib.elbencho_live_plots using directly built analyses."""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import matplotlib

# Unusable when another test module installed a matplotlib stub first.
_REAL_MATPLOTLIB = hasattr(matplotlib, "use")
if _REAL_MATPLOTLIB:
    matplotlib.use("Agg")

from lib import (
    elbencho_live_plots as plots,
)  # noqa: E402  pylint: disable=wrong-import-position
from lib.elbencho_live_report import (  # noqa: E402  pylint: disable=wrong-import-position
    AggregatePoint,
    DomainAnalysis,
    LiveClientId,
    LiveDomainKey,
    LiveFileMetadata,
)
from lib.elbencho_live_steady import (  # noqa: E402  pylint: disable=wrong-import-position
    ClientSteady,
    FleetBand,
    PhaseContext,
    SteadyAnalysis,
    SteadyWindow,
    WindowedClientDetail,
    WindowSeries,
    WindowSummary,
)

WINDOWS = 6
SLOW = LiveClientId("1", "node-b")
FAST = LiveClientId("0", "node-a")


def _metadata(directory: str) -> LiveFileMetadata:
    return LiveFileMetadata(
        path=str(Path(directory) / "run.live.csv"),
        io_size="1M",
        nodes=2,
        threads=1,
        io_depth=1,
        datestamp="20260101Z000000",
    )


def _domain(latency: bool = True, gap: bool = False) -> DomainAnalysis:
    points = []
    for interval in range(10):
        if gap and interval == 4:
            continue
        points.append(
            AggregatePoint(
                interval,
                float(interval + 1),
                f"2026-01-01T00:00:{interval + 1:02d}Z",
                100.0 if interval != 5 else None,
                10.0,
                io_latency_us=500.0 if latency else None,
            )
        )
    return DomainAnalysis(key=LiveDomainKey("READ", ""), aggregate_points=points)


def _window(reason=None) -> SteadyWindow:
    return SteadyWindow(2, 8, 3000, 9000, reason)


def _steady(reason=None) -> SteadyAnalysis:
    steady = SteadyAnalysis(
        window=_window(reason),
        context=PhaseContext(9000, 10000, 1048576, 500.0),
        interval_ms=1000,
        shortfall_percent=5.0,
        min_slow_windows=3,
    )
    for label, length in (("interval", 1), ("5s", 5), ("30s", 30)):
        steady.series[label] = WindowSeries(
            label,
            length,
            length * 1000,
            end_interval=list(range(WINDOWS)),
            end_ms=[(index + 1) * 1000 for index in range(WINDOWS)],
            clients=[2] * WINDOWS,
            jain=[0.99, None, 0.97, 0.98, 0.96, 0.99],
            min_over_median=[0.9, None, 0.8, 0.95, 0.7, 0.99],
            floor=[0.9] * WINDOWS,
            in_steady=[False, False, True, True, True, False],
        )
    steady.band_5s = FleetBand(
        end_ms=list(steady.series["5s"].end_ms),
        p10_mib_s=[1.0, None, 3.0, 3.0, 3.0, 3.0],
        p50_mib_s=[2.0, 2.0, 4.0, 4.0, 4.0, 4.0],
        p90_mib_s=[3.0, 3.0, 5.0, 5.0, 5.0, 5.0],
    )
    steady.clients = {
        SLOW: ClientSteady(SLOW, classification="persistent"),
        FAST: ClientSteady(FAST, classification="ok"),
    }
    return steady


def _detail() -> WindowedClientDetail:
    return WindowedClientDetail(
        [SLOW, FAST],
        {
            SLOW: [1.0, None, 2.0, 2.0, 2.0, 2.0],
            FAST: [4.0, 4.0, 4.0, None, 4.0, 4.0],
        },
        {
            SLOW: [0.5, None, 0.5, 0.5, 0.5, 0.5],
            FAST: [1.0, 1.0, 1.0, None, 1.0, 1.0],
        },
    )


def _summary(_self, label: str) -> WindowSummary:
    return WindowSummary(label, 5000, 3, 0.96, 0.96, 0.98, 5000, 0.0, 0.7, 0.9)


@unittest.skipUnless(_REAL_MATPLOTLIB, "matplotlib stubbed by another test")
class PlotFileTests(unittest.TestCase):
    """Each plot writes its PNG, or returns None when there is no data."""

    def setUp(self) -> None:
        patcher = mock.patch.object(SteadyAnalysis, "summary", _summary)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _assert_png(self, path, directory: str, suffix: str) -> None:
        self.assertIsNotNone(path)
        self.assertTrue(Path(path).is_file())
        self.assertEqual(Path(path).parent, Path(directory))
        self.assertEqual(Path(path).name, f"run-read-{suffix}.png")
        self.assertGreater(Path(path).stat().st_size, 0)

    def test_aggregate_with_and_without_latency(self) -> None:
        for latency in (True, False):
            with tempfile.TemporaryDirectory() as directory:
                path = plots.plot_aggregate_timeseries(
                    _metadata(directory),
                    _domain(latency=latency, gap=True),
                    _steady(),
                    directory,
                )
                self._assert_png(path, directory, "aggregate-timeseries")

    def test_aggregate_without_steady_or_with_unavailable_window(self) -> None:
        for steady in (None, _steady("too_short")):
            with tempfile.TemporaryDirectory() as directory:
                path = plots.plot_aggregate_timeseries(
                    _metadata(directory), _domain(), steady, directory
                )
                self._assert_png(path, directory, "aggregate-timeseries")

    def test_aggregate_none_without_points(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = DomainAnalysis(key=LiveDomainKey("READ", ""))
            self.assertIsNone(
                plots.plot_aggregate_timeseries(
                    _metadata(directory), domain, None, directory
                )
            )

    def test_fairness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = plots.plot_fairness_timeseries(
                _metadata(directory), _domain(), _steady(), directory
            )
            self._assert_png(path, directory, "fairness-timeseries")

    def test_fairness_none_without_jain(self) -> None:
        steady = _steady()
        for series in steady.series.values():
            series.jain = [None] * WINDOWS
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(
                plots.plot_fairness_timeseries(
                    _metadata(directory), _domain(), steady, directory
                )
            )

    def test_client_timeseries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = plots.plot_client_timeseries(
                _metadata(directory), _domain(), _steady(), _detail(), directory
            )
            self._assert_png(path, directory, "client-timeseries")

    def test_client_timeseries_none_without_windows(self) -> None:
        steady = _steady()
        del steady.series["5s"]
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(
                plots.plot_client_timeseries(
                    _metadata(directory), _domain(), steady, _detail(), directory
                )
            )

    def test_heatmap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = plots.plot_client_ratio_heatmap(
                _metadata(directory), _domain(), _steady(), _detail(), directory
            )
            self._assert_png(path, directory, "client-ratio-heatmap")

    def test_heatmap_none_without_rows_or_windows(self) -> None:
        empty = WindowedClientDetail([], {}, {})
        no_windows = _steady()
        del no_windows.series["5s"]
        with tempfile.TemporaryDirectory() as directory:
            metadata = _metadata(directory)
            self.assertIsNone(
                plots.plot_client_ratio_heatmap(
                    metadata, _domain(), _steady(), empty, directory
                )
            )
            self.assertIsNone(
                plots.plot_client_ratio_heatmap(
                    metadata, _domain(), no_windows, _detail(), directory
                )
            )


class DataHelperTests(unittest.TestCase):
    """Missing values become NaN, never zero."""

    def test_nan_for_none(self) -> None:
        values = plots.nan_for_none([1, None, 0])
        self.assertEqual(values[0], 1.0)
        self.assertTrue(math.isnan(values[1]))
        self.assertEqual(values[2], 0.0)

    def test_aligned_values_pad_and_truncate(self) -> None:
        padded = plots.aligned_values([1.0], 3)
        self.assertEqual(padded[0], 1.0)
        self.assertTrue(all(math.isnan(value) for value in padded[1:]))
        self.assertEqual(plots.aligned_values([1.0, 2.0, 3.0], 2), [1.0, 2.0])

    def test_ratio_matrix_percent_with_nan(self) -> None:
        matrix = plots.ratio_matrix(_detail(), WINDOWS)
        self.assertEqual(matrix[0][0], 50.0)
        self.assertTrue(math.isnan(matrix[0][1]))
        self.assertTrue(math.isnan(matrix[1][3]))
        self.assertEqual(len(matrix), 2)

    def test_gap_breaks_insert_nan(self) -> None:
        x_values, y_values = plots.series_with_gap_breaks(
            [1.0, 2.0, 3.0, 6.0], [1.0, 1.0, 1.0, 1.0]
        )
        self.assertEqual(len(x_values), 5)
        self.assertTrue(math.isnan(y_values[3]))

    def test_non_steady_runs(self) -> None:
        self.assertEqual(
            plots._non_steady_runs(  # pylint: disable=protected-access
                [False, False, True, True, False]
            ),
            [(0, 1), (4, 4)],
        )


if __name__ == "__main__":
    unittest.main()
