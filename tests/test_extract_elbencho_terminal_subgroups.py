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

"""Tests for extract-elbencho terminal table subgrouping."""

import tempfile
import unittest
from pathlib import Path

from tests.extract_elbencho_test_support import load_extract_elbencho_module

_EXTRACT_MOD = load_extract_elbencho_module("csp_extract_elbencho")

ElbenchoMetrics = _EXTRACT_MOD.ElbenchoMetrics
_sorted_unique_datestamp_path_pairs = getattr(
    _EXTRACT_MOD, "_sorted_unique_datestamp_path_pairs"
)
_terminal_table_subgroups = getattr(_EXTRACT_MOD, "_terminal_table_subgroups")
_metric_should_show_csv_phase_dur = getattr(
    _EXTRACT_MOD, "_metric_should_show_csv_phase_dur"
)
parse_benchmark_filename = _EXTRACT_MOD.parse_benchmark_filename
discover_live_csv_files = _EXTRACT_MOD.discover_live_csv_files


def _metric(**kwargs) -> ElbenchoMetrics:
    """Minimal ElbenchoMetrics for subgroup tests."""
    defaults = {
        "io_size": "1M",
        "nodes": 10,
        "threads": 64,
        "io_depth": 1,
        "operation": "READ",
        "datestamp": "20260101Z000000",
        "is_multi_node": True,
        "command": "",
        "file_size_bytes": 16 * 1024**3,
        "direct_io": 1,
        "random_io": 0,
        "iops": 100.0,
        "throughput_mib_s": 100.0,
        "throughput_mb_s": 104.8576,
        "throughput_gbps": 0.8,
        "min_lat_sec": 0.001,
        "avg_lat_sec": 0.002,
        "max_lat_sec": 0.003,
        "lat_pct_1": 0.0,
        "lat_pct_50": 0.0,
        "lat_pct_75": 0.0,
        "lat_pct_99": 0.0,
        "io_duration_sec": 300,
        "phase_wall_duration_ms": 0,
        "phase_first_duration_ms": 0,
        "is_single_big_file": False,
        "sweep_single_option": False,
    }
    defaults.update(kwargs)
    return ElbenchoMetrics(**defaults)


class TestTerminalTableSubgroups(unittest.TestCase):
    """_terminal_table_subgroups bucketing."""

    def test_merge_same_key_different_datestamp(self):
        a = _metric(datestamp="20260413Z191151")
        b = _metric(datestamp="20260413Z205743")
        groups = _terminal_table_subgroups([a, b])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)
        self.assertEqual(groups[0][0].datestamp, "20260413Z191151")
        self.assertEqual(groups[0][1].datestamp, "20260413Z205743")

    def test_split_direct_io(self):
        dio = _metric(direct_io=1)
        bio = _metric(direct_io=0)
        groups = _terminal_table_subgroups([dio, bio])
        self.assertEqual(len(groups), 2)

    def test_split_read_from_path(self):
        x = _metric(sweep_read_from_path="/mnt/fs/a")
        y = _metric(sweep_read_from_path="/mnt/fs/b")
        groups = _terminal_table_subgroups([x, y])
        self.assertEqual(len(groups), 2)

    def test_merge_different_treescan_same_file_size(self):
        a = _metric(
            datestamp="20260413Z191151",
            treescan_avg_bytes=1000,
        )
        b = _metric(
            datestamp="20260413Z205743",
            treescan_avg_bytes=999999,
        )
        groups = _terminal_table_subgroups([a, b])
        self.assertEqual(len(groups), 1)

    def test_split_random_io(self):
        seq = _metric(random_io=0)
        rnd = _metric(random_io=1)
        groups = _terminal_table_subgroups([seq, rnd])
        self.assertEqual(len(groups), 2)

    def test_split_io_duration_sec(self):
        a = _metric(io_duration_sec=300)
        b = _metric(io_duration_sec=600)
        groups = _terminal_table_subgroups([a, b])
        self.assertEqual(len(groups), 2)


class TestMetricShouldShowCsvPhaseDur(unittest.TestCase):
    """_metric_should_show_csv_phase_dur (Dur column in text/markdown tables)."""

    def test_single_big_bio_shows_dur_even_with_timelimit_in_csv(self):
        """BIO single-big read: wall time is one pass, not timelimit — show Dur."""
        m = _metric(
            is_single_big_file=True,
            direct_io=0,
            io_duration_sec=180,
            phase_wall_duration_ms=167_000,
        )
        self.assertTrue(_metric_should_show_csv_phase_dur(m))

    def test_single_big_dio_suppresses_dur_when_timelimit_set(self):
        """DIO single-big uses --infloop + timelimit; keep Dur off like other timed sweeps."""
        m = _metric(
            is_single_big_file=True,
            direct_io=1,
            io_duration_sec=180,
            phase_wall_duration_ms=180_000,
        )
        self.assertFalse(_metric_should_show_csv_phase_dur(m))

    def test_many_file_bio_timed_sweep_suppresses_dur(self):
        m = _metric(
            is_single_big_file=False,
            direct_io=0,
            io_duration_sec=180,
            phase_wall_duration_ms=179_000,
        )
        self.assertFalse(_metric_should_show_csv_phase_dur(m))

    def test_no_wall_time_never_shows(self):
        m = _metric(
            is_single_big_file=True,
            direct_io=0,
            io_duration_sec=180,
            phase_wall_duration_ms=0,
        )
        self.assertFalse(_metric_should_show_csv_phase_dur(m))


class TestSortedUniqueDatestampPathPairs(unittest.TestCase):
    """_sorted_unique_datestamp_path_pairs."""

    def test_unique_and_sorted(self):
        m1 = _metric(
            datestamp="20260413Z205743",
            write_only_data_dir="/wo/b",
        )
        m2 = _metric(
            datestamp="20260413Z191151",
            write_only_data_dir="/wo/a",
        )
        m3 = _metric(
            datestamp="20260413Z191151",
            write_only_data_dir="/wo/a",
        )
        pairs = _sorted_unique_datestamp_path_pairs([m1, m2, m3], "write_only_data_dir")
        self.assertEqual(
            pairs,
            [
                ("20260413Z191151", "/wo/a"),
                ("20260413Z205743", "/wo/b"),
            ],
        )


class TestLiveCSVDiscovery(unittest.TestCase):
    """Live CSV discovery and sibling benchmark association."""

    def test_discovers_only_parseable_live_csv_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            valid = (
                Path(temp_dir)
                / "elbencho-r64K-c_004-s_008-d_002_20260729Z120000.live.csv"
            )
            invalid = Path(temp_dir) / "unrelated.live.csv"
            valid.write_text("Phase,RuntimeMS\n", encoding="utf-8")
            invalid.write_text("Phase,RuntimeMS\n", encoding="utf-8")
            discovered = discover_live_csv_files(temp_dir)
        self.assertEqual(len(discovered), 1)
        self.assertEqual(discovered[0].path, str(valid))
        self.assertEqual(discovered[0].io_size, "r64K")
        self.assertEqual(discovered[0].nodes, 4)
        self.assertEqual(discovered[0].threads, 8)
        self.assertEqual(discovered[0].io_depth, 2)


class TestBenchmarkFilenameParsing(unittest.TestCase):
    """Benchmark result discovery excludes live CSV stems."""

    def test_live_csv_stem_is_not_an_aggregate_result(self):
        filename = "elbencho-1M-c_001-s_008-d_001_20260729Z120000.live"
        self.assertIsNone(parse_benchmark_filename(filename))


if __name__ == "__main__":
    unittest.main()
