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
    analyze_live_csv,
    collect_selected_client_detail,
    select_clients,
    select_heatmap_clients,
    write_client_summaries,
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
        "Lat Ent us": "",
        "Lat IO us": "",
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

    def _analyze(self, rows, threshold=2.0, columns=None):
        fieldnames = columns or LIVE_CSV_COLUMNS
        with self.path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames)
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
        return analyze_live_csv(metadata, threshold)


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
        self.assertEqual(
            select_clients(domain, 1, 10),
            [LiveClientId("1", "")],
        )

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
        self.assertEqual(
            select_clients(domain, 1, 10),
            [LiveClientId("0", "")],
        )

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


class TestLiveUnderperformance(LiveCSVTestCase):
    """Fleet statistics, persistence, selection, and second pass."""

    def _outlier_rows(self):
        rows = []
        counters = {str(rank): 0 for rank in range(5)}
        for runtime_ms in (1000, 2000, 3000, 4000):
            for rank in range(5):
                rate = 10 if rank == 0 else 100
                counters[str(rank)] += rate * MIB
                rows.append(_row(runtime_ms, rank, counters[str(rank)]))
        return rows

    def test_z_scores_streaks_and_selection(self):
        analysis = self._analyze(self._outlier_rows(), threshold=1.5)
        domain = analysis.domains[LiveDomainKey("READ", "")]
        slow = domain.clients[LiveClientId("0", "")]
        self.assertEqual(slow.underperform_count, 3)
        self.assertEqual(slow.longest_underperform_streak, 3)
        self.assertAlmostEqual(slow.mean_mib_s, 10.0)
        self.assertEqual(
            select_clients(domain, 2, 10),
            [LiveClientId("0", "")],
        )
        self.assertEqual(select_clients(domain, 4, 10), [])

    def test_second_pass_retains_only_selected_client(self):
        analysis = self._analyze(self._outlier_rows(), threshold=1.5)
        domain = analysis.domains[LiveDomainKey("READ", "")]
        selected = select_clients(domain, 1, 1)
        detail = collect_selected_client_detail(analysis, domain, selected)
        self.assertEqual(list(detail.rates_by_client), [LiveClientId("0", "")])
        self.assertEqual(detail.rates_by_client[selected[0]], [None, 10.0, 10.0, 10.0])
        self.assertEqual(detail.heatmap_by_client[selected[0]][0], None)
        self.assertTrue(
            all(value == 10.0 for value in detail.heatmap_by_client[selected[0]][1:])
        )

    def test_zero_rate_remains_valid_when_fleet_median_is_zero(self):
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
        self.assertEqual(detail.heatmap_by_client[client][2], 0.0)

    def test_heatmap_adds_representative_fleet_clients(self):
        analysis = self._analyze(self._outlier_rows(), threshold=1.5)
        domain = analysis.domains[LiveDomainKey("READ", "")]
        prioritized = select_clients(domain, 1, 5)
        all_clients = select_heatmap_clients(domain, prioritized, 5)
        self.assertEqual(all_clients[0], LiveClientId("0", ""))
        self.assertEqual(set(all_clients), set(domain.clients))
        sampled = select_heatmap_clients(domain, prioritized, 3)
        self.assertEqual(
            sampled,
            [
                LiveClientId("0", ""),
                LiveClientId("1", ""),
                LiveClientId("4", ""),
            ],
        )

    def test_summary_files_contain_required_fields(self):
        analysis = self._analyze(self._outlier_rows(), threshold=1.5)
        domain = analysis.domains[LiveDomainKey("READ", "")]
        csv_path, text_path = write_client_summaries(
            analysis.metadata,
            domain,
            self.temp_dir.name,
        )
        self.assertTrue(Path(csv_path).is_file())
        self.assertTrue(Path(text_path).is_file())
        with Path(csv_path).open(encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[0]["client"], "worker-0")
        self.assertEqual(rows[0]["underperform_intervals"], "3")
        self.assertEqual(rows[0]["avg_fleet_median_percent"], "10.000")
        self.assertEqual(
            rows[0]["worst_iso_timestamp"],
            "2026-07-29T12:00:02.000+00:00",
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
                "6",
                "--work-dir",
                self.temp_dir.name,
                "--no-plots",
            ],
            check=False,
            text=True,
            capture_output=True,
        )

    def test_generated_fixture_exercises_anomalies(self):
        generate_live_csv(self.path, client_count=50, interval_count=18)
        metadata = LiveFileMetadata(
            path=str(self.path),
            io_size="r64K",
            nodes=50,
            threads=1,
            io_depth=1,
            datestamp="20260729Z120000",
        )
        analysis = analyze_live_csv(metadata, z_threshold=1.5)
        domain = analysis.domains[LiveDomainKey("READ", "")]
        self.assertEqual(len(domain.clients), 50)
        self.assertGreater(domain.diagnostics["missing_samples"], 0)
        self.assertGreater(domain.diagnostics["counter_resets"], 0)
        persistent = domain.clients[LiveClientId("0", "client-00000")]
        self.assertGreater(persistent.underperform_count, 0)
        selected = select_clients(domain, min_underperform_segments=1, limit=50)
        self.assertIn(LiveClientId("20", "client-00020"), selected)
        self.assertIn(LiveClientId("30", "client-00030"), selected)

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
