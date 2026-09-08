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

"""Tests for resume/appended elbencho .csv/.out parsing."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests.extract_elbencho_test_support import load_extract_elbencho_module

_EXTRACT_MOD = load_extract_elbencho_module("csp_extract_elbencho_resume")

ElbenchoMetrics = _EXTRACT_MOD.ElbenchoMetrics
add_elbencho_out_file_metrics = _EXTRACT_MOD.add_elbencho_out_file_metrics
apply_execution_workloads = _EXTRACT_MOD.apply_execution_workloads
parse_elbencho_csv_file = _EXTRACT_MOD.parse_elbencho_csv_file
print_workload_metadata_for_metrics = _EXTRACT_MOD.print_workload_metadata_for_metrics
read_csv = _EXTRACT_MOD.read_csv
write_csv = _EXTRACT_MOD.write_csv

_DS = "20260722Z030114"
_BASE = f"elbencho-r64K-c_600-s_004-d_001_{_DS}"
_FILENAME_PARAMS = {
    "io_size": "r64K",
    "nodes": 600,
    "threads": 4,
    "io_depth": 1,
    "datestamp": _DS,
}

_WRITE_SECTION_OLD = """\
ISO DATE: 2026-07-22T10:48:37-0700
COMMAND LINE: "elbencho" "--write"
OPERATION   RESULT TYPE         FIRST DONE   LAST DONE
=========== ================    ==========   =========
WRITE       Elapsed time     :    1m0.244s    1m0.261s
            IOPS             :      652998      658022
            IO lat % us      : [ 1%<=2896 50%<=4096 75%<=4096 99%<=6889 ]
            IO lat hist      : [ 2896: 100 ]
---
"""

_WRITE_SECTION_NEW = """\
ISO DATE start: 2026-07-22T11:07:04-0700
COMMAND LINE: "elbencho" "--write"
OPERATION   RESULT TYPE         FIRST DONE   LAST DONE
=========== ================    ==========   =========
WRITE       Elapsed time     :    1m0.243s    1m0.256s
            IOPS             :      648034      653172
            IO lat % us      : [ 1%<=3444 50%<=4096 75%<=4096 99%<=6889 ]
            IO lat hist      : [ 3444: 999 ]
---
ISO DATE end  : 2026-07-22T11:08:05-0700
"""

_READ_SECTION_OLD = """\
ISO DATE: 2026-07-22T11:08:17-0700
COMMAND LINE: "elbencho" "--read"
OPERATION   RESULT TYPE         FIRST DONE   LAST DONE
=========== ================    ==========   =========
READ        Elapsed time     :    1m0.243s    1m0.257s
            IOPS             :      874053      881209
            IO lat % us      : [ 1%<=2435 50%<=2896 75%<=2896 99%<=4096 ]
            IO lat hist      : [ 2435: 200 ]
---
ISO DATE: 2026-07-22T11:09:18-0700
"""

_READ_SECTION_NEW = """\
ISO DATE start: 2026-07-22T11:10:00-0700
COMMAND LINE: "elbencho" "--read"
OPERATION   RESULT TYPE         FIRST DONE   LAST DONE
=========== ================    ==========   =========
READ        Elapsed time     :    1m0.243s    1m0.257s
            IOPS             :      900000      910000
            IO lat % us      : [ 1%<=512 50%<=1024 75%<=1024 99%<=2048 ]
            IO lat hist      : [ 512: 500 ]
---
ISO DATE end  : 2026-07-22T11:11:00-0700
"""

# Headerless CSV rows (write then read, duplicated on resume).
_CSV_HEADERLESS = """\
2026-07-22T10:48:37-0700,0,0,0,0,0,0,0,0,WRITE,60244,60261,35,37,652998,658022,652998,658022,0,0,0,0,0,0,0,0,0,0,0,0,2220,3640,3210000,1,0,"elbencho --write --direct --size=1G"
2026-07-22T11:08:17-0700,0,0,0,0,0,0,0,0,READ,60243,60257,43,43,874053,881209,874053,881209,0,0,0,0,0,0,0,0,0,0,0,0,1810,2720,208000,0,1,"elbencho --read --direct --rand"
2026-07-22T11:07:04-0700,0,0,0,0,0,0,0,0,WRITE,60243,60256,35,37,648034,653172,648034,653172,0,0,0,0,0,0,0,0,0,0,0,0,2220,3670,3520000,1,0,"elbencho --write --direct --size=1G"
2026-07-22T11:10:00-0700,0,0,0,0,0,0,0,0,READ,60243,60257,43,43,900000,910000,900000,910000,0,0,0,0,0,0,0,0,0,0,0,0,1500,2500,150000,0,1,"elbencho --read --direct --rand"
"""


def _minimal_metric(operation: str) -> ElbenchoMetrics:
    return ElbenchoMetrics(
        io_size="r64K",
        nodes=600,
        threads=4,
        io_depth=1,
        operation=operation,
        datestamp=_DS,
        is_multi_node=True,
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
        file_size_bytes=0,
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


def _write_execution_metadata(
    result_dir: Path,
    execution_id: str,
    *,
    nodes: int = 600,
    io_size: str = "r64K",
    threads: int = 4,
    io_depth: int = 1,
    layout: str = "shared-directory",
    count_source: str = "configuration",
    treefile_source: str = "null",
    publish_outcome: str = "not_applicable",
    requested: str = "8",
    effective: str = "8",
    files: str = "4800",
    byte_count: str = "322122547200",
    reader_nodes: str = "null",
    reader_threads: str = "null",
    reader_depth: str = "null",
    termination: str = "completion",
    effective_timelimit: str = "null",
    completion: str = "completed",
    cleanup_state: str = "not_needed",
    write_state: str = "completed",
    read_state: str = "completed",
    delete_state: str = "completed",
) -> None:
    """Write one representative NNNN.sh plus NNNN.workload.tsv pair."""
    executions = result_dir / "executions"
    executions.mkdir(parents=True, exist_ok=True)
    (executions / f"{execution_id}.sh").write_text(
        "# Auto-generated; do not edit. "
        f"Coords: nodes={nodes} io_size={io_size} "
        f"thread_count={threads} io_depth={io_depth}\n"
        f"export ELBENCHO_FILE_LAYOUT={layout}\n",
        encoding="utf-8",
    )
    metadata = {
        "dataset_count_source": count_source,
        "treefile_source": treefile_source,
        "treefile_cache_publish_outcome": publish_outcome,
        "requested_files_per_node": requested,
        "effective_files_per_node": effective,
        "dataset_files_total": files,
        "dataset_bytes_total": byte_count,
        "reader_nodes": reader_nodes,
        "reader_threads_per_node": reader_threads,
        "reader_iodepth": reader_depth,
        "files_per_reader_node": "null",
        "termination_mode": termination,
        "configured_duration_seconds": "60",
        "effective_timelimit_seconds": effective_timelimit,
        "completion_state": completion,
        "failure_cleanup_state": cleanup_state,
        "write_expected_files": files if write_state != "not_applicable" else "null",
        "write_expected_bytes": (
            byte_count if write_state != "not_applicable" else "null"
        ),
        "write_completed_files": (files if write_state == "completed" else "null"),
        "write_completed_bytes": (byte_count if write_state == "completed" else "null"),
        "write_elapsed_time_ms": "101" if write_state == "completed" else "null",
        "write_completion_state": write_state,
        "read_expected_files": files if read_state == "completed" else "null",
        "read_expected_bytes": byte_count if read_state == "completed" else "null",
        "read_completed_files": files if read_state == "completed" else "null",
        "read_completed_bytes": byte_count if read_state == "completed" else "null",
        "read_elapsed_time_ms": "202" if read_state == "completed" else "null",
        "read_completion_state": read_state,
        "delete_expected_files": (files if count_source == "configuration" else "null"),
        "delete_completed_files": files if delete_state == "completed" else "null",
        "delete_elapsed_time_ms": "31" if delete_state == "completed" else "null",
        "delete_completion_state": (
            delete_state if count_source == "configuration" else "not_applicable"
        ),
        "write_delete_elapsed_time_ms": (
            "132"
            if count_source == "configuration" and delete_state == "completed"
            else "null"
        ),
        "lifecycle_elapsed_time_ms": (
            "350"
            if count_source == "configuration" and delete_state == "completed"
            else "null"
        ),
    }
    content = "".join(f"{key}\t{value}\n" for key, value in metadata.items())
    (executions / f"{execution_id}.workload.tsv").write_text(content, encoding="utf-8")


class TestExecutionWorkloadMetadata(unittest.TestCase):
    """Per-execution metadata is joined through reified coordinates."""

    def test_generated_shared_metadata_exposes_volume_and_topology(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_execution_metadata(Path(tmp), "0001")
            metrics = [_minimal_metric("WRITE"), _minimal_metric("READ")]

            apply_execution_workloads(tmp, metrics)

            for metric in metrics:
                self.assertEqual(metric.workload_layout, "shared-directory")
                self.assertEqual(metric.dataset_count_source, "configuration")
                self.assertEqual(metric.requested_files_per_node, "8")
                self.assertEqual(metric.effective_files_per_node, "8")
                self.assertEqual(metric.dataset_files_total, "4800")
                self.assertEqual(metric.dataset_bytes_total, "322122547200")
                self.assertEqual(metric.reader_nodes, "null")
                self.assertEqual(metric.reader_threads_per_node, "null")
                self.assertEqual(metric.reader_iodepth, "null")
                self.assertEqual(metric.termination_mode, "completion")
                self.assertEqual(metric.effective_timelimit_seconds, "null")
                self.assertEqual(metric.completion_state, "completed")
                self.assertEqual(metric.write_expected_bytes, "322122547200")
                self.assertEqual(metric.read_completion_state, "completed")
                self.assertEqual(metric.write_elapsed_time_ms, "101")
                self.assertEqual(metric.read_elapsed_time_ms, "202")
                self.assertEqual(metric.delete_expected_files, "4800")
                self.assertEqual(metric.delete_completed_files, "4800")
                self.assertEqual(metric.delete_elapsed_time_ms, "31")
                self.assertEqual(metric.delete_completion_state, "completed")
                self.assertEqual(metric.write_delete_elapsed_time_ms, "132")
                self.assertEqual(metric.lifecycle_elapsed_time_ms, "350")

    def test_staged_metadata_uses_tree_totals_and_null_per_node_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_execution_metadata(
                Path(tmp),
                "0001",
                layout="worker-directories",
                count_source="treefile",
                treefile_source="cache_hit",
                publish_outcome="reused",
                requested="null",
                effective="null",
                files="8",
                byte_count="8192",
                reader_nodes="3",
                reader_threads="3",
                reader_depth="2",
                termination="time_bounded_repeat",
                effective_timelimit="60",
                completion="not_applicable_time_based",
                write_state="not_applicable",
                read_state="not_applicable_time_based",
                delete_state="not_applicable",
            )
            metric = _minimal_metric("READ")
            metric.sweep_single_option = True

            apply_execution_workloads(tmp, [metric])

            self.assertEqual(metric.workload_layout, "staged-tree")
            self.assertEqual(metric.dataset_count_source, "treefile")
            self.assertEqual(metric.treefile_source, "cache_hit")
            self.assertEqual(metric.treefile_cache_publish_outcome, "reused")
            self.assertEqual(metric.requested_files_per_node, "null")
            self.assertEqual(metric.effective_files_per_node, "null")
            self.assertEqual(metric.dataset_files_total, "8")
            self.assertEqual(metric.dataset_bytes_total, "8192")
            self.assertEqual(metric.reader_nodes, "3")
            self.assertEqual(metric.reader_threads_per_node, "3")
            self.assertEqual(metric.reader_iodepth, "2")
            self.assertEqual(metric.termination_mode, "time_bounded_repeat")
            self.assertEqual(metric.effective_timelimit_seconds, "60")
            self.assertEqual(metric.read_completion_state, "not_applicable_time_based")
            self.assertEqual(metric.delete_completion_state, "not_applicable")

    def test_coordinate_mismatch_does_not_apply_another_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_execution_metadata(Path(tmp), "0001", threads=8)
            metric = _minimal_metric("WRITE")

            apply_execution_workloads(tmp, [metric])

            self.assertEqual(metric.workload_layout, "")
            self.assertEqual(metric.dataset_files_total, "")

    def test_compound_io_size_coordinates_join_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_execution_metadata(Path(tmp), "0001", io_size="1M,r4K")
            metric = _minimal_metric("READ")
            metric.io_size = "1M,r4K"

            apply_execution_workloads(tmp, [metric])

            self.assertEqual(metric.workload_layout, "shared-directory")
            self.assertEqual(metric.dataset_files_total, "4800")

    def test_huge_decimal_completion_fields_remain_strings(self) -> None:
        huge_bytes = "184467440737095516160000"
        with tempfile.TemporaryDirectory() as tmp:
            _write_execution_metadata(
                Path(tmp),
                "0001",
                byte_count=huge_bytes,
                completion="incomplete",
                delete_state="not_started",
            )
            workload = Path(tmp) / "executions" / "0001.workload.tsv"
            workload.write_text(
                workload.read_text(encoding="utf-8").replace(
                    "write_completion_state\tcompleted",
                    "write_completion_state\tincomplete",
                ),
                encoding="utf-8",
            )
            metric = _minimal_metric("WRITE")

            apply_execution_workloads(tmp, [metric])

            self.assertEqual(metric.dataset_bytes_total, huge_bytes)
            self.assertIsInstance(metric.dataset_bytes_total, str)
            self.assertEqual(metric.completion_state, "incomplete")
            self.assertEqual(metric.write_completion_state, "incomplete")
            self.assertEqual(metric.write_completed_bytes, huge_bytes)

            extracted_csv = Path(tmp) / "extracted.csv"
            write_csv(str(extracted_csv), [metric])
            reloaded = read_csv(str(extracted_csv))[0]
            self.assertEqual(reloaded.dataset_bytes_total, huge_bytes)
            self.assertEqual(reloaded.write_completed_bytes, huge_bytes)
            self.assertEqual(reloaded.write_completion_state, "incomplete")
            self.assertEqual(reloaded.delete_elapsed_time_ms, "null")
            self.assertEqual(reloaded.lifecycle_elapsed_time_ms, "null")

    def test_duplicate_or_noncanonical_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_execution_metadata(Path(tmp), "0001")
            workload = Path(tmp) / "executions" / "0001.workload.tsv"
            workload.write_text(
                workload.read_text(encoding="utf-8") + "dataset_files_total\t04800\n",
                encoding="utf-8",
            )
            metric = _minimal_metric("WRITE")

            apply_execution_workloads(tmp, [metric])

            self.assertEqual(metric.workload_layout, "")
            self.assertEqual(metric.dataset_files_total, "")

        with tempfile.TemporaryDirectory() as tmp:
            _write_execution_metadata(Path(tmp), "0001")
            workload = Path(tmp) / "executions" / "0001.workload.tsv"
            workload.write_text(
                workload.read_text(encoding="utf-8").replace(
                    "dataset_files_total\t4800", "dataset_files_total\t04800"
                ),
                encoding="utf-8",
            )
            metric = _minimal_metric("WRITE")

            apply_execution_workloads(tmp, [metric])

            self.assertEqual(metric.workload_layout, "")
            self.assertEqual(metric.dataset_files_total, "")

    def test_pre_rmfiles_workload_schema_gets_explicit_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_execution_metadata(Path(tmp), "0001")
            workload = Path(tmp) / "executions" / "0001.workload.tsv"
            new_keys = {
                "write_elapsed_time_ms",
                "read_elapsed_time_ms",
                "delete_expected_files",
                "delete_completed_files",
                "delete_elapsed_time_ms",
                "delete_completion_state",
                "write_delete_elapsed_time_ms",
                "lifecycle_elapsed_time_ms",
            }
            legacy_lines = [
                line
                for line in workload.read_text(encoding="utf-8").splitlines()
                if line.split("\t", 1)[0] not in new_keys
            ]
            workload.write_text("\n".join(legacy_lines) + "\n", encoding="utf-8")
            metric = _minimal_metric("WRITE")

            apply_execution_workloads(tmp, [metric])

            self.assertEqual(metric.workload_layout, "shared-directory")
            self.assertEqual(metric.delete_completion_state, "not_applicable")
            self.assertEqual(metric.delete_elapsed_time_ms, "null")
            self.assertEqual(metric.lifecycle_elapsed_time_ms, "null")

    def test_contradictory_cleanup_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_execution_metadata(Path(tmp), "0001")
            workload = Path(tmp) / "executions" / "0001.workload.tsv"
            workload.write_text(
                workload.read_text(encoding="utf-8").replace(
                    "delete_completion_state\tcompleted",
                    "delete_completion_state\tnot_applicable",
                ),
                encoding="utf-8",
            )
            metric = _minimal_metric("WRITE")

            apply_execution_workloads(tmp, [metric])

            self.assertEqual(metric.workload_layout, "")
            self.assertEqual(metric.delete_completion_state, "")

    def test_report_prints_explicit_workload_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_execution_metadata(Path(tmp), "0001")
            metric = _minimal_metric("WRITE")
            metric.sweep_single_option = True
            apply_execution_workloads(tmp, [metric])
            output = io.StringIO()

            with redirect_stdout(output):
                print_workload_metadata_for_metrics([metric])

            report = output.getvalue()
            self.assertIn("layout=shared-directory", report)
            self.assertIn("requested_files_per_node=8", report)
            self.assertIn("effective_files_per_node=8", report)
            self.assertIn("dataset_files=4800", report)
            self.assertIn("reader_nodes=null", report)
            self.assertIn("write_elapsed_ms=101", report)
            self.assertIn("read_elapsed_ms=202", report)
            self.assertIn("delete_completed_files=4800", report)
            self.assertIn("write_delete_elapsed_ms=132", report)
            self.assertIn("lifecycle_elapsed_ms=350", report)


class TestResumeCsvDedupe(unittest.TestCase):
    """parse_elbencho_csv_file keeps only the latest WRITE and READ rows."""

    def test_dedupes_duplicate_write_and_read_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / f"{_BASE}.csv"
            csv_path.write_text(_CSV_HEADERLESS, encoding="utf-8")
            metrics = parse_elbencho_csv_file(str(csv_path), _FILENAME_PARAMS)
            ops = [m.operation for m in metrics]
            self.assertEqual(ops, ["WRITE", "READ"])
            self.assertAlmostEqual(metrics[0].iops, 648034.0)
            self.assertAlmostEqual(metrics[1].iops, 900000.0)

    def test_partial_retry_drops_read_from_previous_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / f"{_BASE}.csv"
            partial_retry = "\n".join(_CSV_HEADERLESS.splitlines()[:3]) + "\n"
            csv_path.write_text(partial_retry, encoding="utf-8")
            metrics = parse_elbencho_csv_file(str(csv_path), _FILENAME_PARAMS)
            self.assertEqual([m.operation for m in metrics], ["WRITE"])
            self.assertAlmostEqual(metrics[0].iops, 648034.0)


class TestResumeOutLastSection(unittest.TestCase):
    """add_elbencho_out_file_metrics uses the last operation block."""

    def test_last_write_section_wins_with_mixed_iso_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / f"{_BASE}.out"
            out_path.write_text(
                _WRITE_SECTION_OLD + _WRITE_SECTION_NEW + _READ_SECTION_OLD,
                encoding="utf-8",
            )
            metrics = [_minimal_metric("WRITE")]
            add_elbencho_out_file_metrics(str(out_path), metrics)
            self.assertAlmostEqual(metrics[0].lat_pct_50, 0.004096)
            self.assertEqual(metrics[0].histogram.get(0.003444), 999)

    def test_last_read_section_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / f"{_BASE}.out"
            out_path.write_text(
                _WRITE_SECTION_NEW + _READ_SECTION_OLD + _READ_SECTION_NEW,
                encoding="utf-8",
            )
            metrics = [_minimal_metric("READ")]
            add_elbencho_out_file_metrics(str(out_path), metrics)
            self.assertAlmostEqual(metrics[0].lat_pct_50, 0.001024)
            self.assertEqual(metrics[0].histogram.get(0.000512), 500)


if __name__ == "__main__":
    unittest.main()
