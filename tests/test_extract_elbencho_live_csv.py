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

"""Tests for bounded-memory elbencho live CSV reporting."""

import csv
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lib.elbencho_live_report import (
    MIB,
    LIVE_CSV_COLUMNS,
    LiveClientId,
    LiveDomainKey,
    LiveFileMetadata,
    P2Median,
    ParsedRow,
    analyze_live_csv,
    client_sort_key,
    collect_selected_client_detail,
    iter_parsed_rows,
    live_csv_reader,
    write_client_summaries,
)
from lib.elbencho_live_steady import (
    CLASS_OK,
    CLASS_PERSISTENT,
    CLASS_TRANSIENT,
    PhaseContext,
    analyze_steady,
)
from utils.benchmark_elbencho_live_csv import generate_live_csv


def _row(
    runtime_ms,
    rank,
    done_bytes,
    *,
    phase="READ",
    mix_type="",
    service="",
    mib_s="",
    iops="",
    entries_s="",
    lat_io="",
    lat_ent="",
):
    iso_timestamp = (
        datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
        + timedelta(milliseconds=runtime_ms)
    ).isoformat(timespec="milliseconds")
    return {
        "ISO Date": iso_timestamp,
        "Label": "",
        "Phase": phase,
        "RuntimeMS": str(runtime_ms),
        "Rank": str(rank),
        "MixType": mix_type,
        "Done%": "0",
        "DoneBytes": str(done_bytes),
        "MiB/s": str(mib_s),
        "IOPS": str(iops),
        "Entries": "0",
        "Entries/s": str(entries_s),
        "Lat Ent us": str(lat_ent),
        "Lat IO us": str(lat_io),
        "Active": "",
        "CPU": "",
        "Service": service,
    }


class LiveCSVTestCase(unittest.TestCase):
    """Fixture helpers for live CSV tests."""

    def setUp(self):
        # pylint: disable=consider-using-with
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = (
            Path(self.temp_dir.name)
            / "elbencho-r64K-c_004-s_008-d_002_20260729Z120000.live.csv"
        )

    def _analyze(self, rows, columns=None):
        fieldnames = columns or LIVE_CSV_COLUMNS
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        metadata = LiveFileMetadata(
            path=str(self.path),
            io_size="r64K",
            nodes=4,
            threads=8,
            io_depth=2,
            datestamp="20260729Z120000",
        )
        return analyze_live_csv(metadata)


class TestLiveAggregateAnalysis(LiveCSVTestCase):
    """Aggregate-only and counter-domain behavior."""

    def test_aggregate_only_uses_native_rates(self):
        analysis = self._analyze(
            [
                _row(1000, "Total", MIB, mib_s=100, iops=1600),
                _row(2100, "Total", 2 * MIB, mib_s=110, iops=1760),
            ]
        )
        domain = analysis.domains[LiveDomainKey("READ", "")]
        self.assertFalse(domain.has_extended_rows)
        self.assertEqual(
            [point.throughput_mib_s for point in domain.aggregate_points],
            [100.0, 110.0],
        )
        self.assertEqual(
            [point.elapsed_sec for point in domain.aggregate_points], [1, 2.1]
        )
        self.assertEqual(
            domain.aggregate_points[0].iso_timestamp,
            "2026-07-29T12:00:01.000+00:00",
        )

    def test_reordered_columns_are_mapped_by_header(self):
        analysis = self._analyze(
            [
                _row(1000, "Total", MIB, mib_s=100, iops=1600),
                _row(2000, "Total", 2 * MIB, mib_s=110, iops=1760),
            ],
            columns=list(reversed(LIVE_CSV_COLUMNS)),
        )
        domain = analysis.domains[LiveDomainKey("READ", "")]
        self.assertEqual(
            [point.throughput_mib_s for point in domain.aggregate_points],
            [100.0, 110.0],
        )

    def test_phase_and_mix_type_are_independent_domains(self):
        rows = []
        for phase, mix_type in (("RWMIX", "Read"), ("RWMIX", "Write"), ("READ", "")):
            rows.extend(
                [
                    _row(1000, 0, MIB, phase=phase, mix_type=mix_type),
                    _row(2000, 0, 2 * MIB, phase=phase, mix_type=mix_type),
                ]
            )
        analysis = self._analyze(rows)
        self.assertEqual(
            set(analysis.domains),
            {
                LiveDomainKey("RWMIX", "Read"),
                LiveDomainKey("RWMIX", "Write"),
                LiveDomainKey("READ", ""),
            },
        )
        for domain in analysis.domains.values():
            self.assertEqual(domain.clients[LiveClientId("0", "")].mean_mib_s, 1.0)

    def test_metadata_entry_phases_are_skipped(self):
        rows = [
            _row(1000, "Total", MIB, mib_s=1),
            _row(1000, 0, MIB),
            _row(2000, "Total", 2 * MIB, mib_s=1),
            _row(2000, 0, 2 * MIB),
        ]
        for phase in ("STAT", "CREATE", "MKDIR", "DELETE"):
            rows.extend(
                [
                    _row(1000, "Total", 0, phase=phase, mib_s=0, iops=0, entries_s=50),
                    _row(1000, 0, 0, phase=phase, entries_s=50),
                    _row(2000, "Total", 0, phase=phase, mib_s=0, iops=0, entries_s=60),
                    _row(2000, 0, 0, phase=phase, entries_s=60),
                ]
            )
        analysis = self._analyze(rows)
        self.assertEqual(set(analysis.domains), {LiveDomainKey("READ", "")})

    def test_repeated_phase_runtime_creates_new_occurrence(self):
        analysis = self._analyze(
            [
                _row(1000, "Total", MIB, mib_s=1),
                _row(1000, 0, MIB),
                _row(2000, "Total", 2 * MIB, mib_s=1),
                _row(2000, 0, 2 * MIB),
                _row(1000, "Total", MIB, mib_s=1),
                _row(1000, 0, MIB),
                _row(2000, "Total", 2 * MIB, mib_s=1),
                _row(2000, 0, 2 * MIB),
            ]
        )
        first = analysis.domains[LiveDomainKey("READ", "", 0)]
        second = analysis.domains[LiveDomainKey("READ", "", 1)]
        self.assertEqual(first.clients[LiveClientId("0", "")].mean_mib_s, 1.0)
        self.assertEqual(second.clients[LiveClientId("0", "")].mean_mib_s, 1.0)

    def test_short_appended_phase_rate_jump_creates_new_occurrence(self):
        analysis = self._analyze(
            [
                _row(1, "Total", 0, mib_s=1),
                _row(1, 0, 0),
                _row(1001, "Total", MIB, mib_s=1),
                _row(1001, 0, MIB),
                _row(1002, "Total", 2 * MIB, mib_s=1),
                _row(1002, 0, 2 * MIB),
                _row(2002, "Total", 3 * MIB, mib_s=1),
                _row(2002, 0, 3 * MIB),
            ]
        )
        first = analysis.domains[LiveDomainKey("READ", "", 0)]
        second = analysis.domains[LiveDomainKey("READ", "", 1)]
        client = LiveClientId("0", "")
        self.assertEqual(first.clients[client].sample_count, 1)
        self.assertEqual(second.clients[client].sample_count, 1)
        self.assertEqual(first.clients[client].mean_mib_s, 1.0)
        self.assertEqual(second.clients[client].mean_mib_s, 1.0)

    def test_short_interval_matching_total_rate_stays_in_same_occurrence(self):
        analysis = self._analyze(
            [
                _row(1000, "Total", 1000 * MIB, mib_s=1000),
                _row(1000, 0, 1000 * MIB),
                _row(1001, "Total", 1001 * MIB, mib_s=1000),
                _row(1001, 0, 1001 * MIB),
            ]
        )
        domain = analysis.domains[LiveDomainKey("READ", "")]
        client = LiveClientId("0", "")
        self.assertEqual(set(analysis.domains), {LiveDomainKey("READ", "")})
        self.assertEqual(domain.clients[client].sample_count, 1)
        self.assertEqual(domain.clients[client].mean_mib_s, 1000.0)

    def test_total_counter_reset_splits_occurrence_when_runtime_increases(self):
        analysis = self._analyze(
            [
                _row(1000, "Total", 10 * MIB, mib_s=1),
                _row(1000, 0, 10 * MIB),
                _row(2000, "Total", 20 * MIB, mib_s=1),
                _row(2000, 0, 20 * MIB),
                _row(2001, "Total", MIB, mib_s=1),
                _row(2001, 0, MIB),
                _row(3001, "Total", 2 * MIB, mib_s=1),
                _row(3001, 0, 2 * MIB),
            ]
        )
        first = analysis.domains[LiveDomainKey("READ", "", 0)]
        second = analysis.domains[LiveDomainKey("READ", "", 1)]
        self.assertEqual(first.clients[LiveClientId("0", "")].sample_count, 1)
        self.assertEqual(second.clients[LiveClientId("0", "")].sample_count, 1)


class TestLiveClientRates(LiveCSVTestCase):
    """Rate derivation, identity, gaps, and resets."""

    def test_uses_actual_runtime_delta_and_service_identity(self):
        analysis = self._analyze(
            [
                _row(1000, "Total", 30 * MIB, mib_s=12, iops=192),
                _row(1000, 0, 10 * MIB, service="node-a"),
                _row(1000, 1, 20 * MIB, service="node-b"),
                _row(2250, "Total", 45 * MIB, mib_s=12, iops=192),
                _row(2250, 0, 15 * MIB, service="node-a"),
                _row(2250, 1, 30 * MIB, service="node-b"),
            ]
        )
        domain = analysis.domains[LiveDomainKey("READ", "")]
        clients = domain.clients
        self.assertAlmostEqual(clients[LiveClientId("0", "node-a")].mean_mib_s, 4.0)
        self.assertAlmostEqual(clients[LiveClientId("1", "node-b")].mean_mib_s, 8.0)
        derived_total = sum(summary.mean_mib_s for summary in clients.values())
        self.assertAlmostEqual(
            derived_total, domain.aggregate_points[-1].throughput_mib_s
        )
        self.assertEqual(clients[LiveClientId("0", "node-a")].client.mode, "service")

    def test_missing_interval_is_not_averaged_or_zero_filled(self):
        analysis = self._analyze(
            [
                _row(1000, 0, MIB),
                _row(1000, 1, MIB),
                _row(2000, 0, 2 * MIB),
                _row(3000, 0, 3 * MIB),
                _row(3000, 1, 3 * MIB),
            ]
        )
        domain = analysis.domains[LiveDomainKey("READ", "")]
        missing = domain.clients[LiveClientId("1", "")]
        self.assertEqual(missing.sample_count, 0)
        self.assertEqual(missing.missing_count, 1)
        self.assertEqual(missing.invalid_count, 0)
        self.assertEqual(domain.diagnostics["non_adjacent_samples"], 1)

    def test_counter_reset_is_invalid_then_new_baseline(self):
        analysis = self._analyze(
            [
                _row(1000, 0, 10 * MIB),
                _row(2000, 0, 12 * MIB),
                _row(3000, 0, MIB),
                _row(4000, 0, 3 * MIB),
            ]
        )
        domain = analysis.domains[LiveDomainKey("READ", "")]
        summary = domain.clients[LiveClientId("0", "")]
        self.assertEqual(summary.sample_count, 2)
        self.assertAlmostEqual(summary.mean_mib_s, 2.0)
        self.assertEqual(summary.invalid_count, 1)
        self.assertEqual(domain.diagnostics["counter_resets"], 1)

    def test_client_first_seen_late_counts_leading_missing_samples(self):
        analysis = self._analyze(
            [
                _row(1000, 0, MIB),
                _row(2000, 0, 2 * MIB),
                _row(3000, 0, 3 * MIB),
                _row(3000, 1, MIB),
            ]
        )
        domain = analysis.domains[LiveDomainKey("READ", "")]
        late = domain.clients[LiveClientId("1", "")]
        self.assertEqual(late.missing_count, 2)
        self.assertEqual(late.sample_count, 0)

    def test_non_positive_elapsed_time_is_invalid(self):
        analysis = self._analyze(
            [
                _row(1000, 0, MIB),
                _row(900, 0, 2 * MIB),
            ]
        )
        domain = analysis.domains[LiveDomainKey("READ", "")]
        self.assertEqual(domain.clients[LiveClientId("0", "")].invalid_count, 1)
        self.assertEqual(domain.diagnostics["non_positive_elapsed_time"], 1)


def _outlier_rows(intervals=4, slow_rate=10, fast_rate=100, clients=5):
    """One slow worker (rank 0) among fast ones, one row per second."""
    rows = []
    counters = [0] * clients
    for index in range(intervals):
        runtime_ms = (index + 1) * 1000
        rows.append(
            _row(
                runtime_ms,
                "Total",
                sum(counters),
                mib_s=slow_rate + fast_rate * (clients - 1),
            )
        )
        for rank in range(clients):
            counters[rank] += (slow_rate if rank == 0 else fast_rate) * MIB
            rows.append(_row(runtime_ms, rank, counters[rank]))
    return rows


class TestLiveSecondPassAndSummaries(LiveCSVTestCase):
    """Selected-client rates and the client summary files."""

    def test_second_pass_retains_only_selected_client(self):
        analysis = self._analyze(_outlier_rows())
        domain = analysis.domains[LiveDomainKey("READ", "")]
        selected = [LiveClientId("0", "")]
        detail = collect_selected_client_detail(analysis, domain, selected)
        self.assertEqual(list(detail.rates_by_client), selected)
        self.assertEqual(detail.rates_by_client[selected[0]], [None, 10.0, 10.0, 10.0])

    def test_zero_rate_remains_valid(self):
        analysis = self._analyze(
            [
                _row(1000, 0, 100 * MIB),
                _row(1000, 1, 100 * MIB),
                _row(2000, 0, 200 * MIB),
                _row(2000, 1, 200 * MIB),
                _row(3000, 0, 200 * MIB),
                _row(3000, 1, 200 * MIB),
            ]
        )
        domain = analysis.domains[LiveDomainKey("READ", "")]
        client = LiveClientId("0", "")
        detail = collect_selected_client_detail(analysis, domain, [client])
        self.assertEqual(detail.rates_by_client[client], [None, 100.0, 0.0])

    def test_client_summary_without_z_scores(self):
        analysis = self._analyze(_outlier_rows())
        summary = analysis.domains[LiveDomainKey("READ", "")].clients[
            LiveClientId("0", "")
        ]
        self.assertAlmostEqual(summary.mean_mib_s, 10.0)
        self.assertAlmostEqual(summary.relative_to_fleet_median, 0.1)
        self.assertEqual(summary.first_progress_interval, 0)
        for removed in ("underperform_count", "worst_z_score", "problem_count"):
            self.assertFalse(hasattr(summary, removed), removed)

    def test_summary_files_contain_steady_columns(self):
        analysis = self._analyze(_outlier_rows(intervals=15))
        domain = analysis.domains[LiveDomainKey("READ", "")]
        steady = analyze_steady(
            analysis, domain, PhaseContext(15000, 15000, None, None), 1000
        )
        csv_path, text_path = write_client_summaries(
            analysis.metadata, domain, steady, self.temp_dir.name
        )
        self.assertTrue(csv_path.endswith("-read-client-summary.csv"))
        with Path(csv_path).open(encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = list(reader)
        pinned_columns = (
            "client mode rank service class steady_mean_mib_s steady_ratio "
            "worst_5s_ratio worst_5s_elapsed_sec worst_30s_ratio "
            "worst_30s_elapsed_sec short_5s_percent mean_mib_s median_mib_s "
            "valid_samples missing_samples invalid_samples"
        )
        self.assertEqual(reader.fieldnames, pinned_columns.split())
        self.assertEqual(
            [row["client"] for row in rows],
            ["worker-0", "worker-1", "worker-2", "worker-3", "worker-4"],
        )
        slow = rows[0]
        self.assertEqual(slow["class"], CLASS_PERSISTENT)
        self.assertEqual(slow["steady_mean_mib_s"], "10.000")
        self.assertEqual(slow["steady_ratio"], "0.1000")
        self.assertEqual(slow["worst_5s_ratio"], "0.1000")
        self.assertEqual(slow["worst_5s_elapsed_sec"], "7.000")
        self.assertEqual(slow["worst_30s_ratio"], "")
        self.assertEqual(slow["short_5s_percent"], "100.0")
        self.assertEqual(slow["mean_mib_s"], "10.000")
        self.assertEqual(slow["valid_samples"], "14")
        self.assertEqual(rows[1]["class"], CLASS_OK)
        text = Path(text_path).read_text(encoding="utf-8").splitlines()
        self.assertEqual(text[0], "Elbencho live client summary: READ")
        self.assertEqual(
            text[1], "Steady window: 2.0-15.0 s; shortfall 5%; min slow windows 3"
        )
        self.assertTrue(text[2].startswith("host/worker\tclass\tsteady MiB/s"))
        self.assertTrue(text[3].startswith("worker-0\tpersistent\t10.000\t0.100"))

    def test_summary_files_without_steady_window(self):
        analysis = self._analyze(_outlier_rows())
        domain = analysis.domains[LiveDomainKey("READ", "")]
        steady = analyze_steady(
            analysis, domain, PhaseContext(4000, 4000, None, None), 1000
        )
        csv_path, text_path = write_client_summaries(
            analysis.metadata, domain, steady, self.temp_dir.name
        )
        with Path(csv_path).open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["class"], "")
        self.assertEqual(rows[0]["steady_ratio"], "")
        self.assertEqual(rows[0]["mean_mib_s"], "10.000")
        text = Path(text_path).read_text(encoding="utf-8").splitlines()
        self.assertEqual(text[1], "Steady window unavailable (too_short)")


class TestLiveLatencyAndDiagnostics(LiveCSVTestCase):
    """Total-row latency parsing, optional columns, and diagnostic caps."""

    def test_total_row_latency_parsing(self):
        analysis = self._analyze(
            [
                _row(1000, "Total", MIB, mib_s=1, iops=16, lat_io=250, lat_ent=0),
                _row(2000, "Total", MIB, mib_s=0, iops=0, lat_io=0, lat_ent=7.5),
                _row(3000, "Total", 2 * MIB, mib_s=1, iops="", lat_io=0),
                _row(4000, "Total", 3 * MIB, mib_s=0, iops=5, lat_io=0),
                _row(5000, "Total", 4 * MIB, iops="", lat_io=90),
                _row(6000, "Total", 4 * MIB, lat_io="nan"),
            ]
        )
        points = analysis.domains[LiveDomainKey("READ", "")].aggregate_points
        self.assertEqual(
            [point.io_latency_us for point in points], [250.0, None, None, 0.0, 90.0]
        )
        self.assertEqual(
            [point.entries_latency_us for point in points],
            [None, 7.5, None, None, None],
        )
        self.assertEqual([point.interval for point in points], [0, 1, 2, 3, 4])

    def test_latency_columns_are_optional(self):
        columns = [
            name for name in LIVE_CSV_COLUMNS if name not in ("Lat IO us", "Lat Ent us")
        ]
        analysis = self._analyze(
            [
                _row(1000, "Total", MIB, mib_s=1, iops=16),
                _row(1000, 0, MIB),
            ],
            columns=columns,
        )
        point = analysis.domains[LiveDomainKey("READ", "")].aggregate_points[0]
        self.assertIsNone(point.io_latency_us)
        self.assertIsNone(point.entries_latency_us)
        self.assertEqual(point.throughput_mib_s, 1.0)

    def test_parsed_rows_are_named(self):
        self._analyze(
            [
                _row(1000, "Total", MIB, mib_s=1, iops=16, lat_io=12),
                _row(1000, 0, MIB, service="node-a"),
            ]
        )
        with self.path.open(encoding="utf-8", newline="") as handle:
            reader, columns = live_csv_reader(handle, str(self.path))
            rows = list(iter_parsed_rows(reader, columns))
        self.assertIsInstance(rows[0], ParsedRow)
        self.assertEqual(rows[0].io_latency_us, 12.0)
        self.assertIsNone(rows[0].client)
        self.assertEqual(rows[1].client, LiveClientId("0", "node-a"))
        self.assertEqual(rows[1].done_bytes, MIB)
        self.assertIsNone(rows[1].io_latency_us)
        self.assertIsNone(rows[1].mib_s)

    def test_malformed_line_diagnostics_are_capped(self):
        rows = [_row(1000, 0, MIB)]
        rows.extend(_row(1000, 1, "bad") for _ in range(8))
        analysis = self._analyze(rows)
        diagnostics = analysis.domains[LiveDomainKey("READ", "")].diagnostics
        self.assertEqual(diagnostics["malformed_client_rows"], 8)
        named = sorted(key for key in diagnostics if key.startswith("line_"))
        self.assertEqual(named, [f"line_{number}_invalid" for number in range(3, 8)])

    def test_all_started_interval(self):
        analysis = self._analyze(
            [
                _row(1000, 0, MIB),
                _row(1000, 1, 0),
                _row(2000, 0, 2 * MIB),
                _row(2000, 1, 0),
                _row(3000, 0, 3 * MIB),
                _row(3000, 1, MIB),
            ]
        )
        domain = analysis.domains[LiveDomainKey("READ", "")]
        self.assertEqual(
            domain.clients[LiveClientId("0", "")].first_progress_interval, 0
        )
        self.assertEqual(domain.all_started_interval, 2)
        # A client that never moves data does not hold back the others.
        stalled = self._analyze([_row(1000, 0, MIB), _row(1000, 1, 0)])
        self.assertEqual(
            stalled.domains[LiveDomainKey("READ", "")].all_started_interval, 0
        )
        idle = self._analyze([_row(1000, 0, 0), _row(1000, 1, 0)])
        self.assertIsNone(idle.domains[LiveDomainKey("READ", "")].all_started_interval)
        aggregate_only = self._analyze([_row(1000, "Total", MIB, mib_s=1)])
        self.assertIsNone(
            aggregate_only.domains[LiveDomainKey("READ", "")].all_started_interval
        )

    def test_client_sort_key_orders_ranks_numerically(self):
        clients = [
            LiveClientId("10", ""),
            LiveClientId("b", ""),
            LiveClientId("2", "node-b"),
            LiveClientId("2", "node-a"),
            LiveClientId("a", ""),
        ]
        self.assertEqual(
            sorted(clients, key=client_sort_key),
            [
                LiveClientId("2", "node-a"),
                LiveClientId("2", "node-b"),
                LiveClientId("10", ""),
                LiveClientId("a", ""),
                LiveClientId("b", ""),
            ],
        )


class TestP2Median(unittest.TestCase):
    """Constant-memory median estimator."""

    def test_tracks_ordered_sequence(self):
        estimator = P2Median()
        for value in range(1, 1001):
            estimator.add(float(value))
        self.assertAlmostEqual(estimator.value(), 500.5, delta=2.0)


class TestSyntheticBenchmarkGenerator(LiveCSVTestCase):
    """Small automated preset for the large benchmark generator."""

    def _run_benchmark(self, client_count):
        script = (
            Path(__file__).resolve().parent.parent
            / "utils"
            / "benchmark_elbencho_live_csv.py"
        )
        return subprocess.run(
            [
                sys.executable,
                str(script),
                "--clients",
                str(client_count),
                "--intervals",
                "15",
                "--work-dir",
                self.temp_dir.name,
                "--no-plots",
            ],
            check=False,
            text=True,
            capture_output=True,
        )

    def test_generated_fixture_exercises_anomalies(self):
        generate_live_csv(self.path, client_count=50, interval_count=60)
        metadata = LiveFileMetadata(
            path=str(self.path),
            io_size="r64K",
            nodes=50,
            threads=1,
            io_depth=1,
            datestamp="20260729Z120000",
        )
        analysis = analyze_live_csv(metadata)
        domain = analysis.domains[LiveDomainKey("READ", "")]
        self.assertEqual(len(domain.clients), 50)
        self.assertGreater(domain.diagnostics["missing_samples"], 0)
        self.assertGreater(domain.diagnostics["counter_resets"], 0)
        steady = analyze_steady(
            analysis, domain, PhaseContext(60000, 60000, None, None), 1000
        )
        self.assertTrue(steady.window.available)
        classes = {
            client.rank: stats.classification
            for client, stats in steady.clients.items()
        }
        self.assertEqual(classes["0"], CLASS_PERSISTENT)
        self.assertIn(classes["15"], (CLASS_PERSISTENT, CLASS_TRANSIENT))
        self.assertEqual(classes["45"], CLASS_OK)
        # A counter reset inside the steady window voids the steady ratio.
        reset = steady.clients[LiveClientId("35", "client-00035")]
        self.assertIsNone(reset.steady_ratio)
        self.assertEqual(reset.windows_30s, 0)
        self.assertEqual(reset.classification, CLASS_OK)

    def test_benchmark_cli_rejects_fleets_too_small_for_validation(self):
        result = self._run_benchmark(50)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--clients must be at least 51", result.stderr)
        self.assertNotIn("generation_seconds=", result.stdout)

    def test_benchmark_cli_accepts_smallest_valid_fleet(self):
        result = self._run_benchmark(51)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("clients_analyzed=51", result.stdout)


if __name__ == "__main__":
    unittest.main()
