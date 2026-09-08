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

"""Tests for apply_treescan_from_directory_scan (io_size-specific sibling .out)."""

import tempfile
import unittest
from pathlib import Path

from tests.extract_elbencho_test_support import load_extract_elbencho_module

_EXTRACT_MOD = load_extract_elbencho_module("csp_extract_elbencho_treescan")

ElbenchoMetrics = _EXTRACT_MOD.ElbenchoMetrics
apply_treescan_from_directory_scan = _EXTRACT_MOD.apply_treescan_from_directory_scan

_DS = "20260426Z214750"
_TREESCAN_1M = "Treescan file sizes (bytes): count=2 avg=9999 min=1 max=10000\n"
_TREESCAN_R64 = "Treescan file sizes (bytes): count=2 avg=1111 min=1 max=2000\n"


def _minimal_metric(io_size: str) -> ElbenchoMetrics:
    return ElbenchoMetrics(
        io_size=io_size,
        nodes=1,
        threads=1,
        io_depth=1,
        operation="READ",
        datestamp=_DS,
        is_multi_node=False,
        iops=1.0,
        throughput_mib_s=1.0,
        throughput_mb_s=1.0,
        throughput_gbps=0.01,
        min_lat_sec=0.001,
        avg_lat_sec=0.002,
        max_lat_sec=0.003,
        lat_pct_1=0.0,
        lat_pct_50=0.0,
        lat_pct_75=0.0,
        lat_pct_99=0.0,
        direct_io=1,
        random_io=1,
        command="",
        file_size_bytes=1024,
        io_duration_sec=60,
        phase_wall_duration_ms=0,
        phase_first_duration_ms=0,
        write_only_data_dir="",
        sweep_read_from_path="",
        treescan_size_stats_line="",
        treescan_file_count=0,
        treescan_avg_bytes=0,
        treescan_min_bytes=0,
        treescan_max_bytes=0,
        treescan_first_file_bytes=0,
        histogram={},
    )


class TestTreescanDirectoryScan(unittest.TestCase):
    """apply_treescan_from_directory_scan picks io_size-matched .out."""

    def test_prefers_matching_io_size_not_lexicographic_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            (tdir / f"elbencho-1M-c_001-s_001-d_001_{_DS}.out").write_text(
                _TREESCAN_1M, encoding="utf-8"
            )
            (tdir / f"elbencho-r64K-c_001-s_001-d_001_{_DS}.out").write_text(
                _TREESCAN_R64, encoding="utf-8"
            )
            metrics = [_minimal_metric("r64K")]
            apply_treescan_from_directory_scan(str(tdir), metrics)
            self.assertEqual(metrics[0].treescan_avg_bytes, 1111)

    def test_slurm_job_log_name_matches_io_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            (tdir / f"elbencho-1M-c_001-s_001-d_001_{_DS}.out").write_text(
                _TREESCAN_1M, encoding="utf-8"
            )
            (tdir / f"elbencho-{_DS}-256-1-r64K-999.out").write_text(
                _TREESCAN_R64, encoding="utf-8"
            )
            metrics = [_minimal_metric("r64K")]
            apply_treescan_from_directory_scan(str(tdir), metrics)
            self.assertEqual(metrics[0].treescan_avg_bytes, 1111)

    def test_matches_per_execution_log_to_exact_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tdir = Path(tmp)
            executions = tdir / "executions"
            executions.mkdir()
            base = tdir / f"elbencho-r64K-c_001-s_001-d_001_{_DS}"
            (Path(f"{base}.out")).write_text("benchmark output\n", encoding="utf-8")
            wrong_result = f"elbencho-1M-c_001-s_001-d_001_{_DS}.out"
            (executions / "0001.log").write_text(
                f"(remote) Human readable output file: {wrong_result}\n{_TREESCAN_1M}",
                encoding="utf-8",
            )
            (executions / "0002.log").write_text(
                f"(remote) Human readable output file: {base.name}.out\n"
                f"{_TREESCAN_R64}",
                encoding="utf-8",
            )
            metrics = [_minimal_metric("r64K")]
            apply_treescan_from_directory_scan(str(tdir), metrics, str(base))
            self.assertEqual(metrics[0].treescan_avg_bytes, 1111)


if __name__ == "__main__":
    unittest.main()
