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

"""Tests for steady-window client fairness and fleet latency."""

import csv
import tempfile
import unittest
from pathlib import Path

from lib.elbencho_live_report import (
    LIVE_CSV_COLUMNS,
    MIB,
    DomainAnalysis,
    LiveClientId,
    LiveDomainKey,
    LiveFileMetadata,
    analyze_live_csv,
)
from lib.elbencho_live_steady import (
    CLASS_OK,
    CLASS_PERSISTENT,
    CLASS_TRANSIENT,
    REASON_NO_FIRST_TIME,
    REASON_NO_STEADY_STATE,
    REASON_TOO_SHORT,
    WINDOW_30S,
    WINDOW_5S,
    WINDOW_INTERVAL,
    ClientSteady,
    PhaseContext,
    SteadyAnalysis,
    SteadyWindow,
    _average_ranks,
    _spearman,
    analyze_steady,
    classified_clients,
    collect_windowed_client_detail,
    interval_ms_of,
    select_heatmap_rows,
    steady_window,
    window_length_intervals,
)

PHASE = "WRITE"
NO_CONTEXT = PhaseContext(None, None, None, None)


def _client(rank, service=""):
    return LiveClientId(str(rank), service)


def _csv_rows(  # pylint: disable=too-many-arguments
    rates, services=None, latencies=None, totals=None, missing=()
):
    """Live rows at 1 s intervals; ``rates[i][c]`` is client c's MiB in
    interval i. ``missing`` holds (interval, client) rows to omit; the
    client's counter still advances."""
    counters = [0] * len(rates[0])
    rows = []
    for index, interval_rates in enumerate(rates):
        runtime_ms = (index + 1) * 1000
        for client, rate in enumerate(interval_rates):
            counters[client] += rate * MIB
        total = sum(interval_rates)
        rows.append(
            _live_row(
                runtime_ms,
                "Total",
                sum(counters),
                mib_s=totals[index] if totals else total,
                iops=total,
                lat_io=latencies[index] if latencies else "",
            )
        )
        for client in range(len(interval_rates)):
            if (index, client) in missing:
                continue
            service = services[client] if services else ""
            rows.append(
                _live_row(runtime_ms, client, counters[client], service=service)
            )
    return rows


def _live_row(
    runtime_ms, rank, done_bytes, *, service="", mib_s="", iops="", lat_io=""
):
    row = dict.fromkeys(LIVE_CSV_COLUMNS, "")
    row.update(
        {
            "ISO Date": f"2026-07-29T12:00:{runtime_ms // 1000:02d}.000+00:00",
            "Phase": PHASE,
            "RuntimeMS": str(runtime_ms),
            "Rank": str(rank),
            "DoneBytes": str(done_bytes),
            "MiB/s": str(mib_s),
            "IOPS": str(iops),
            "Lat IO us": str(lat_io),
            "Service": service,
        }
    )
    return row


def _uniform(intervals, clients, rate=100, overrides=None):
    """Every client at ``rate``; ``overrides[(interval, client)]`` replaces one."""
    overrides = overrides or {}
    return [
        [overrides.get((interval, client), rate) for client in range(clients)]
        for interval in range(intervals)
    ]


class SteadyTestCase(unittest.TestCase):
    """Write a synthetic live CSV and analyze it."""

    def setUp(self):
        # pylint: disable=consider-using-with
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = (
            Path(self.temp_dir.name) / "elbencho-1M-c_001-s_004-d_001_x.live.csv"
        )

    def _analysis(self, rows):
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, LIVE_CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        metadata = LiveFileMetadata(str(self.path), "1M", 1, 4, 1, "x")
        analysis = analyze_live_csv(metadata)
        return analysis, analysis.domains[LiveDomainKey(PHASE, "")]

    def _steady(self, rows, first_ms, **kwargs):
        analysis, domain = self._analysis(rows)
        context = kwargs.pop("context", None) or PhaseContext(
            first_ms, first_ms, None, None
        )
        return analysis, domain, analyze_steady(analysis, domain, context, **kwargs)


class TestIntervalsAndWindowLengths(unittest.TestCase):
    """Interval length inference and window sizes."""

    def test_interval_ms_prefers_configured_then_spacing(self):
        domain = DomainAnalysis(LiveDomainKey(PHASE, ""))
        self.assertEqual(interval_ms_of(domain, None), 1000)
        domain.interval_times = [0.5, 1.0, 1.5, 2.6, 3.0]
        self.assertEqual(interval_ms_of(domain, None), 500)
        self.assertEqual(interval_ms_of(domain, 2000), 2000)
        self.assertEqual(interval_ms_of(domain, 0), 500)

    def test_window_length_intervals(self):
        self.assertEqual(window_length_intervals(5, 1000), 5)
        self.assertEqual(window_length_intervals(30, 1000), 30)
        self.assertEqual(window_length_intervals(5, 300), 17)
        self.assertEqual(window_length_intervals(5, 20000), 1)


class TestSteadyWindow(SteadyTestCase):
    """Window bounds from [first], client progress and the ramp."""

    def _window(self, rows, first_ms):
        _, domain = self._analysis(rows)
        return steady_window(domain, PhaseContext(first_ms, None, None, None), 1000)

    def test_completion_bound_phase_ends_at_first(self):
        window = self._window(_csv_rows(_uniform(25, 3)), 15500)
        self.assertEqual(window, SteadyWindow(1, 14, 2000, 15000, None))
        self.assertTrue(window.available)

    def test_time_limited_phase_ends_at_last_row(self):
        window = self._window(_csv_rows(_uniform(20, 3)), 20004)
        self.assertEqual((window.start_ms, window.end_ms), (2000, 20000))
        self.assertTrue(window.available)

    def test_no_first_time(self):
        rows = _csv_rows(_uniform(20, 3))
        for first_ms in (None, 999):
            window = self._window(rows, first_ms)
            self.assertEqual(window, SteadyWindow(reason=REASON_NO_FIRST_TIME))
            self.assertFalse(window.available)

    def test_too_short_keeps_bounds(self):
        window = self._window(_csv_rows(_uniform(20, 3)), 11000)
        self.assertEqual(window, SteadyWindow(1, 10, 2000, 11000, REASON_TOO_SHORT))
        self.assertFalse(window.available)
        self.assertTrue(self._window(_csv_rows(_uniform(20, 3)), 12000).available)

    def test_late_starter_delays_start(self):
        rates = _uniform(30, 3, overrides={(i, 2): 0 for i in range(5)})
        window = self._window(_csv_rows(rates), 30000)
        self.assertEqual((window.start_interval, window.start_ms), (5, 6000))
        self.assertTrue(window.available)

    def test_late_start_past_half_is_no_steady_state(self):
        rates = _uniform(40, 3, overrides={(i, 2): 0 for i in range(25)})
        window = self._window(_csv_rows(rates), 40000)
        self.assertEqual(window.reason, REASON_NO_STEADY_STATE)
        self.assertEqual((window.start_interval, window.end_interval), (25, 39))

    def test_never_started_client_is_persistent(self):
        rates = _uniform(20, 3, overrides={(i, 2): 0 for i in range(20)})
        _, _, steady = self._steady(_csv_rows(rates), 20000)
        self.assertTrue(steady.window.available)
        stalled = steady.clients[_client(2)]
        self.assertEqual(stalled.steady_ratio, 0)
        self.assertEqual(stalled.classification, CLASS_PERSISTENT)
        self.assertEqual(steady.clients[_client(0)].classification, CLASS_OK)

    def test_client_missing_at_a_bound_keeps_its_steady_ratio(self):
        # Client 2 runs at half speed and misses the rows at both bounds.
        rates = _uniform(30, 3, overrides={(i, 2): 50 for i in range(30)})
        rows = _csv_rows(rates, missing={(1, 2), (29, 2)})
        _, _, steady = self._steady(rows, 30000)
        self.assertEqual(steady.window.start_interval, 1)
        self.assertAlmostEqual(steady.clients[_client(2)].steady_ratio, 0.5)

    def test_ramp_threshold(self):
        totals = [100, 100, 200, 250, 300] + [400] * 25
        window = self._window(_csv_rows(_uniform(30, 3), totals=totals), 30000)
        self.assertEqual(window.start_interval, 4)

    def test_aggregate_only_domain(self):
        rows = [
            _live_row((index + 1) * 1000, "Total", index * MIB, mib_s=100)
            for index in range(20)
        ]
        window = self._window(rows, 20000)
        self.assertEqual((window.start_interval, window.end_interval), (1, 19))
        self.assertTrue(window.available)


class TestWindowSeries(SteadyTestCase):
    """Sliding windows, Jain, ratios, floor and band."""

    def test_series_values(self):
        rates = _uniform(20, 4, overrides={(i, 3): 90 for i in range(20)})
        context = PhaseContext(20000, 20000, MIB, None)
        _, _, steady = self._steady(_csv_rows(rates), None, context=context)
        five = steady.series[WINDOW_5S]
        self.assertEqual((five.length_intervals, five.window_ms), (5, 5000))
        self.assertEqual(five.end_interval, list(range(5, 20)))
        self.assertEqual(five.end_ms[0], 6000)
        self.assertEqual(five.in_steady, [False] + [True] * 14)
        self.assertEqual(set(five.clients), {4})
        jain = 1950**2 / (4 * (3 * 500**2 + 450**2))
        self.assertAlmostEqual(five.jain[0], jain)
        self.assertEqual(five.median_bytes[0], 500 * MIB)
        self.assertAlmostEqual(five.min_over_median[0], 0.9)
        self.assertAlmostEqual(five.floor[0], 487.5 / 488.5)
        self.assertEqual(len(steady.series[WINDOW_INTERVAL].end_ms), 19)
        self.assertEqual(steady.series[WINDOW_30S].end_ms, [])
        self.assertIsNone(steady.summary(WINDOW_30S))
        self.assertIsNone(steady.summary("missing"))
        self.assertEqual(steady.band_5s.end_ms, five.end_ms)
        self.assertAlmostEqual(steady.band_5s.p50_mib_s[0], 100.0)
        self.assertAlmostEqual(steady.band_5s.p10_mib_s[0], 93.0)
        summary = steady.summary(WINDOW_5S)
        self.assertEqual(summary.windows, 14)
        self.assertAlmostEqual(summary.jain_min, jain)
        self.assertAlmostEqual(summary.jain_p50, jain)
        self.assertEqual(summary.jain_min_at_ms, 7000)
        self.assertEqual(summary.share_below_0_95, 0.0)
        self.assertAlmostEqual(summary.min_over_median_p10, 0.9)

    def test_missing_client_is_left_out_of_windows(self):
        rows = _csv_rows(_uniform(20, 3), missing={(9, 2)})
        _, domain, steady = self._steady(rows, 20000)
        self.assertEqual(domain.clients[_client(2)].missing_count, 1)
        one = steady.series[WINDOW_INTERVAL]
        by_end = dict(zip(one.end_interval, one.clients))
        self.assertEqual((by_end[8], by_end[9], by_end[10], by_end[11]), (3, 2, 2, 3))
        self.assertEqual(steady.clients[_client(2)].classification, CLASS_OK)

    def test_unavailable_window_still_has_series(self):
        _, _, steady = self._steady(_csv_rows(_uniform(8, 3)), 8000)
        self.assertEqual(steady.window.reason, REASON_TOO_SHORT)
        self.assertEqual(len(steady.series[WINDOW_5S].end_ms), 3)
        self.assertFalse(any(steady.series[WINDOW_5S].in_steady))
        self.assertIsNone(steady.summary(WINDOW_5S))
        self.assertTrue(all(c.classification is None for c in steady.clients.values()))
        self.assertEqual(
            steady.class_counts(),
            {CLASS_PERSISTENT: 0, CLASS_TRANSIENT: 0, CLASS_OK: 0},
        )

    def test_two_service_multi_node(self):
        rates = _uniform(20, 2, overrides={(i, 1): 80 for i in range(20)})
        _, _, steady = self._steady(
            _csv_rows(rates, services=["node-a", "node-b"]), 20000
        )
        slow = steady.clients[_client(1, "node-b")]
        fast = steady.clients[_client(0, "node-a")]
        self.assertAlmostEqual(slow.steady_ratio, 80 / 90)
        self.assertAlmostEqual(slow.steady_mean_mib_s, 80.0)
        self.assertEqual(slow.classification, CLASS_PERSISTENT)
        self.assertEqual(fast.classification, CLASS_OK)
        self.assertAlmostEqual(
            steady.summary(WINDOW_5S).jain_p50, 180**2 / (2 * (100**2 + 80**2))
        )


class TestClassification(SteadyTestCase):
    """Persistent and transient rules at their boundaries."""

    def test_steady_ratio_boundary(self):
        rates = _uniform(20, 5, rate=1000)
        for interval in range(20):
            rates[interval][3] = 951
            rates[interval][4] = 949
        _, _, steady = self._steady(_csv_rows(rates), 20000)
        self.assertEqual(steady.clients[_client(3)].classification, CLASS_OK)
        self.assertEqual(steady.clients[_client(4)].classification, CLASS_PERSISTENT)
        self.assertEqual(steady.clients[_client(4)].short_5s_share, 1.0)

    def test_transient_needs_min_slow_windows(self):
        rates = _uniform(40, 4, overrides={(20, 3): 0})
        _, _, steady = self._steady(_csv_rows(rates), 40000, min_slow_windows=5)
        dipped = steady.clients[_client(3)]
        self.assertEqual(dipped.slow_5s, 5)
        self.assertAlmostEqual(dipped.worst_5s_ratio, 0.8)
        self.assertEqual(dipped.worst_5s_ms, 21000)
        self.assertEqual(dipped.windows_30s, 9)
        self.assertEqual(dipped.short_30s, 0)
        self.assertEqual(dipped.classification, CLASS_TRANSIENT)
        _, _, steady = self._steady(_csv_rows(rates), 40000, min_slow_windows=6)
        self.assertEqual(steady.clients[_client(3)].classification, CLASS_OK)

    def test_turn_taking_is_ok_and_twice_as_often_is_transient(self):
        # Every client dips once per 24 s cycle; client 3 also dips mid-cycle.
        dips = {
            (cycle * 24 + 3 * client + 2, client)
            for cycle in range(3)
            for client in range(4)
        }
        dips |= {(cycle * 24 + 16, 3) for cycle in range(3)}
        rates = _uniform(72, 4, overrides={key: 40 for key in dips})
        _, _, steady = self._steady(_csv_rows(rates), 72000)
        classes = {c.rank: s.classification for c, s in steady.clients.items()}
        self.assertEqual(
            classes,
            {"0": CLASS_OK, "1": CLASS_OK, "2": CLASS_OK, "3": CLASS_TRANSIENT},
        )
        others = sorted(steady.clients[_client(rank)].slow_5s for rank in range(3))
        self.assertGreaterEqual(steady.clients[_client(3)].slow_5s, 2 * others[1])

    def test_shortfall_option_widens_ok_band(self):
        rates = _uniform(20, 4, overrides={(i, 3): 92 for i in range(20)})
        _, _, steady = self._steady(_csv_rows(rates), 20000, shortfall_percent=10)
        self.assertEqual(steady.clients[_client(3)].classification, CLASS_OK)
        self.assertEqual(steady.clients[_client(3)].short_5s, 0)

    def test_persistent_by_30s_windows(self):
        rates = _uniform(60, 4)
        for interval in range(60):
            rates[interval][3] = 130 if 2 <= interval <= 6 else 94
        _, _, steady = self._steady(_csv_rows(rates), 60000)
        slow = steady.clients[_client(3)]
        self.assertGreaterEqual(slow.steady_ratio, 0.95)
        self.assertGreaterEqual(slow.short_30s, slow.windows_30s / 2)
        self.assertEqual(slow.classification, CLASS_PERSISTENT)


class TestLatency(SteadyTestCase):
    """Steady fleet latency summary and Spearman links."""

    def test_latency_summary_and_rho(self):
        dips = (10, 20)
        rates = _uniform(30, 4, overrides={(i, 3): 50 for i in dips})
        latencies = [1000 if i in dips else 100 + i for i in range(30)]
        latencies[0] = 0
        context = PhaseContext(30000, 30000, None, 120.0)
        _, _, steady = self._steady(
            _csv_rows(rates, latencies=latencies), None, context=context
        )
        latency = steady.latency
        self.assertEqual(latency.max_us, 1000)
        self.assertAlmostEqual(latency.max_over_p50, 1000 / latency.p50_us)
        self.assertEqual(latency.final_avg_us, 120.0)
        self.assertGreater(latency.cv, 0)
        steady_values = latencies[1:30]
        weights = [350 if i in dips else 400 for i in range(1, 30)]
        self.assertAlmostEqual(
            latency.steady_mean_us,
            sum(v * w for v, w in zip(steady_values, weights)) / sum(weights),
        )
        self.assertLess(latency.rho_jain_latency, -0.5)
        self.assertLess(latency.rho_latency_throughput, -0.5)

    def test_no_latency_values(self):
        _, _, steady = self._steady(_csv_rows(_uniform(20, 3)), 20000)
        self.assertIsNone(steady.latency)

    def test_no_latency_summary_without_a_steady_window(self):
        latencies = [50 + i for i in range(20)]
        _, _, steady = self._steady(
            _csv_rows(_uniform(20, 3), latencies=latencies), None
        )
        self.assertEqual(steady.window.reason, REASON_NO_FIRST_TIME)
        self.assertIsNone(steady.latency)

    def test_spearman(self):
        self.assertEqual(_average_ranks([10, 20, 20, 5]), [2.0, 3.5, 3.5, 1.0])
        pairs = [(1, 10), (2, 20), (3, 30), (4, 40), (5, 50), (6, 60)]
        self.assertAlmostEqual(_spearman(pairs), 1.0)
        self.assertAlmostEqual(_spearman([(x, -y) for x, y in pairs]), -1.0)
        self.assertIsNone(_spearman(pairs[:5]))
        self.assertIsNone(_spearman([(x, 1.0) for x, _ in pairs]))


class TestSelection(unittest.TestCase):
    """Line and heatmap client selection."""

    @staticmethod
    def _steady(stats):
        steady = SteadyAnalysis(
            SteadyWindow(1, 10, 2000, 20000), NO_CONTEXT, 1000, 5, 3
        )
        steady.clients = {item.client: item for item in stats}
        return steady

    def test_classified_clients_order(self):
        steady = self._steady(
            [
                ClientSteady(
                    _client(10), steady_ratio=0.9, classification=CLASS_PERSISTENT
                ),
                ClientSteady(
                    _client(2), steady_ratio=0.9, classification=CLASS_PERSISTENT
                ),
                ClientSteady(
                    _client(3), steady_ratio=None, classification=CLASS_PERSISTENT
                ),
                ClientSteady(
                    _client(4), steady_ratio=0.5, classification=CLASS_PERSISTENT
                ),
                ClientSteady(
                    _client(5), worst_5s_ratio=0.7, classification=CLASS_TRANSIENT
                ),
                ClientSteady(
                    _client(6), worst_5s_ratio=0.6, classification=CLASS_TRANSIENT
                ),
                ClientSteady(_client(7), steady_ratio=0.1, classification=CLASS_OK),
            ]
        )
        self.assertEqual(
            classified_clients(steady, 10),
            [_client(r) for r in (4, 2, 10, 3, 6, 5)],
        )
        self.assertEqual(classified_clients(steady, 2), [_client(4), _client(2)])
        self.assertEqual(classified_clients(steady, 0), [])

    def test_heatmap_rows_add_representatives(self):
        stats = [
            ClientSteady(
                _client(rank), steady_ratio=1.0 + rank / 100, classification=CLASS_OK
            )
            for rank in range(1, 10)
        ]
        stats.append(
            ClientSteady(_client(0), steady_ratio=0.5, classification=CLASS_PERSISTENT)
        )
        stats.append(ClientSteady(_client(99)))
        steady = self._steady(stats)
        self.assertEqual(
            select_heatmap_rows(steady, 4),
            [_client(0), _client(1), _client(5), _client(99)],
        )
        self.assertEqual(len(select_heatmap_rows(steady, 50)), 11)
        self.assertEqual(select_heatmap_rows(steady, 0), [])


class TestWindowedClientDetail(SteadyTestCase):
    """Third pass: 5 s rates and ratios of chosen clients."""

    def test_rates_and_ratios_align_with_5s_series(self):
        rates = _uniform(20, 4, overrides={(i, 3): 90 for i in range(20)})
        rows = _csv_rows(rates, missing={(12, 1)})
        analysis, domain, steady = self._steady(rows, 20000)
        detail = collect_windowed_client_detail(
            analysis, domain, steady, [_client(3), _client(1), _client(3)]
        )
        self.assertEqual(detail.clients, [_client(3), _client(1)])
        windows = len(steady.series[WINDOW_5S].end_ms)
        self.assertEqual(len(detail.rates_mib_s[_client(3)]), windows)
        self.assertAlmostEqual(detail.rates_mib_s[_client(3)][0], 90.0)
        self.assertAlmostEqual(detail.ratios[_client(3)][0], 0.9)
        ends = steady.series[WINDOW_5S].end_interval
        missing = [ends.index(12), ends.index(17)]
        for index in missing:
            self.assertIsNone(detail.rates_mib_s[_client(1)][index])
            self.assertIsNone(detail.ratios[_client(1)][index])
        self.assertAlmostEqual(detail.rates_mib_s[_client(1)][ends.index(16)], 100.0)

    def test_empty_selection(self):
        analysis, domain, steady = self._steady(_csv_rows(_uniform(20, 3)), 20000)
        detail = collect_windowed_client_detail(analysis, domain, steady, [])
        self.assertEqual(
            (detail.clients, detail.rates_mib_s, detail.ratios), ([], {}, {})
        )


if __name__ == "__main__":
    unittest.main()


class TestReviewRegressions(SteadyTestCase):
    """Rates over late samples, tail resets, and Total-row-only latency."""

    def test_rates_use_actual_sample_spacing(self):
        # Samples arrive every 1.5 s although 1 s is configured; 100 MiB/s each.
        rows = []
        for index in range(20):
            runtime_ms = (index + 1) * 1500
            rows.append(_live_row(runtime_ms, "Total", 0, mib_s=300))
            for client in range(3):
                rows.append(_live_row(runtime_ms, client, (index + 1) * 150 * MIB))
        analysis, domain, steady = self._steady(
            rows, 30000, configured_interval_ms=1000
        )
        self.assertTrue(
            all(abs(rate - 100) < 1e-9 for rate in steady.band_5s.p50_mib_s if rate)
        )
        detail = collect_windowed_client_detail(analysis, domain, steady, [_client(0)])
        rates = [rate for rate in detail.rates_mib_s[_client(0)] if rate is not None]
        self.assertTrue(rates)
        self.assertTrue(all(abs(rate - 100) < 1e-9 for rate in rates))

    def test_tail_reset_does_not_change_steady_results(self):
        rates = _uniform(30, 3, overrides={(i, 2): 50 for i in range(30)})
        rows = _csv_rows(rates)
        for row in rows:
            if row["RuntimeMS"] == "25000" and row["Rank"] == "2":
                row["DoneBytes"] = "0"
        _, _, steady = self._steady(rows, 20000)
        slow = steady.clients[_client(2)]
        self.assertAlmostEqual(slow.steady_ratio, 0.5)
        self.assertEqual(slow.classification, CLASS_PERSISTENT)

    def test_total_rows_only_still_summarize_latency(self):
        rows = [
            _live_row(
                (index + 1) * 1000,
                "Total",
                index * MIB,
                mib_s=100,
                iops=100,
                lat_io=200,
            )
            for index in range(20)
        ]
        _, _, steady = self._steady(rows, 20000)
        self.assertTrue(steady.window.available)
        self.assertEqual(steady.latency.p50_us, 200)
        self.assertIsNone(steady.latency.rho_jain_latency)
