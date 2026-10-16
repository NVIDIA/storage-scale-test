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

"""Unit tests for summarize-elbencho env_used.yaml summary lines."""

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_SUMMARIZE_PATH = _REPO_ROOT / "utils" / "summarize-elbencho.py"
_SUMMARIZE_SPEC = importlib.util.spec_from_file_location(
    "csp_summarize_elbencho", _SUMMARIZE_PATH
)
assert _SUMMARIZE_SPEC and _SUMMARIZE_SPEC.loader
_SUMMARIZE_MOD = importlib.util.module_from_spec(_SUMMARIZE_SPEC)
sys.modules["csp_summarize_elbencho"] = _SUMMARIZE_MOD
_SUMMARIZE_SPEC.loader.exec_module(_SUMMARIZE_MOD)
# pylint: disable=protected-access
_format_three_lines = _SUMMARIZE_MOD._format_three_lines
_execution_status_incomplete_note = _SUMMARIZE_MOD._execution_status_incomplete_note


class TestSummarizeElbenchoEnv(unittest.TestCase):
    """_format_three_lines reflects sweep mode flags from env_used.yaml."""

    def _minimal_env(self, **overrides):
        env = {
            "nodes_spec": "1,4",
            "ELBENCHO_SCALE_READ_WRITE_DURATION": 600,
            "ELBENCHO_FILE_SIZE_MULTIPLIER": 1024,
            "ELBENCHO_SCALE_IO_SIZES": ["1M"],
            "ELBENCHO_SCALE_THREAD_LIST": ["8"],
            "ELBENCHO_IODEPTH_LIST": ["1"],
            "ELBENCHO_SINGLE_BIG_FILE": 0,
            "sweep_write_only": 0,
            "sweep_write_no_read": 0,
            "sweep_read_from": "",
        }
        env.update(overrides)
        return env

    def test_write_no_read_shows_wnr_on_line_three(self):
        env = self._minimal_env(sweep_write_no_read=1)
        with tempfile.TemporaryDirectory() as result_dir:
            triple = _format_three_lines(
                "elbencho-20260101Z120000", env, result_dir, result_dir
            )
        line3 = triple[2]
        self.assertIn("wro=0", line3)
        self.assertIn("wnr=1", line3)

    def test_single_test_dir_on_line_one(self):
        test_dir = "/mnt/fs/scale-test"
        env = self._minimal_env(TEST_DIRS={test_dir: 1})
        with tempfile.TemporaryDirectory() as result_dir:
            triple = _format_three_lines(
                "elbencho-20260601Z180740", env, result_dir, result_dir
            )
        self.assertIn(f"nodes: 1,4 {test_dir}", triple[0])

    def test_multiple_test_dirs_on_line_one(self):
        env = self._minimal_env(
            TEST_DIRS={
                "/mnt/fs/dir-a": 1,
                "/mnt/fs/dir-b": 1,
            }
        )
        with tempfile.TemporaryDirectory() as result_dir:
            triple = _format_three_lines(
                "elbencho-20260601Z180740", env, result_dir, result_dir
            )
        self.assertIn("nodes: 1,4 (multiple TEST_DIRs)", triple[0])

    def test_shared_directory_marks_duration_inactive_and_reports_layout(self):
        env = self._minimal_env(
            ELBENCHO_FILE_LAYOUT="shared-directory",
            ELBENCHO_FILES_PER_NODE="8",
            ELBENCHO_FILE_SIZE="64G",
        )
        with tempfile.TemporaryDirectory() as result_dir:
            executions = Path(result_dir) / "executions"
            executions.mkdir()
            (executions / "0001.workload.tsv").write_text(
                "write_elapsed_time_ms\t101\n"
                "read_elapsed_time_ms\t202\n"
                "delete_completed_files\t4800\n"
                "delete_elapsed_time_ms\t31\n"
                "write_delete_elapsed_time_ms\t132\n"
                "lifecycle_elapsed_time_ms\t150\n"
                "delete_completion_state\tcompleted\n",
                encoding="utf-8",
            )
            triple = _format_three_lines(
                "elbencho-20260820Z120000", env, result_dir, result_dir
            )
        self.assertIn("configured_dur: 600s (inactive; completion-based)", triple[0])
        self.assertIn("layout=shared-directory", triple[2])
        self.assertIn("requested_files_per_node=8", triple[2])
        self.assertIn("effective_files_per_node=8", triple[2])
        self.assertIn("write_elapsed_time_ms=101", triple[2])
        self.assertIn("read_elapsed_time_ms=202", triple[2])
        self.assertIn("delete_completed_files=4800", triple[2])
        self.assertIn("delete_elapsed_time_ms=31", triple[2])
        self.assertIn("write_delete_elapsed_time_ms=132", triple[2])
        self.assertIn("lifecycle_elapsed_time_ms=150", triple[2])

    def test_staged_summary_does_not_report_inherited_file_count(self):
        env = self._minimal_env(
            ELBENCHO_FILE_LAYOUT="shared-directory",
            ELBENCHO_FILES_PER_NODE="8",
            sweep_read_from="/mnt/fs/staged",
        )
        with tempfile.TemporaryDirectory() as result_dir:
            executions = Path(result_dir) / "executions"
            executions.mkdir()
            (executions / "0001.workload.tsv").write_text(
                "dataset_files_total\t8\n"
                "dataset_bytes_total\t65536\n"
                "reader_nodes\t3\n"
                "reader_threads_per_node\t3\n"
                "reader_iodepth\t1\n",
                encoding="utf-8",
            )
            triple = _format_three_lines(
                "elbencho-20260820Z120001", env, result_dir, result_dir
            )
        self.assertIn("dur: 600s", triple[0])
        self.assertIn("layout=staged-tree", triple[2])
        self.assertIn("files_per_node=N/A", triple[2])
        self.assertIn("dataset_files=8", triple[2])
        self.assertIn("dataset_bytes=65536", triple[2])
        self.assertIn("reader_nodes=3", triple[2])
        self.assertIn("reader_threads_per_node=3", triple[2])
        self.assertIn("reader_iodepth=1", triple[2])
        self.assertIn("files_per_reader_node=N/A", triple[2])

    def test_reified_execution_status_is_authoritative_for_completeness(self):
        with tempfile.TemporaryDirectory() as result_dir:
            executions = Path(result_dir) / "executions"
            executions.mkdir()
            (executions / "0001.sh").write_text("# execution\n", encoding="utf-8")
            (executions / "0001.status").write_text("FAILED\n", encoding="utf-8")
            self.assertIn(
                "1/1 executions not SUCCESS",
                _execution_status_incomplete_note(result_dir),
            )
            (executions / "0001.status").write_text("SUCCESS\n", encoding="utf-8")
            self.assertEqual(_execution_status_incomplete_note(result_dir), "")

    def _line1(self, **overrides):
        env = self._minimal_env(**overrides)
        with tempfile.TemporaryDirectory() as result_dir:
            return _format_three_lines(
                "elbencho-20260101Z120000", env, result_dir, result_dir
            )[0]

    def test_roots_default_adds_no_suffix(self):
        line1 = self._line1(TEST_DIRS={"/mnt/a/": 1})
        self.assertTrue(line1.endswith("nodes: 1,4 /mnt/a/"), line1)
        self.assertNotIn("target=", line1)

    def test_missing_identity_adds_no_suffix(self):
        self.assertNotIn("target=", self._line1())

    def test_explicit_name_is_appended(self):
        line1 = self._line1(TEST_DIRS={"/mnt/a": 1}, STORAGE_TARGET_NAME="lustre-a")
        self.assertTrue(line1.endswith("nodes: 1,4 /mnt/a target=lustre-a"), line1)

    def test_kubectl_default_is_appended(self):
        line1 = self._line1(
            TEST_DIRS={"/mnt/a": 1},
            EXECUTION_SUBSTRATE="kubectl",
            KUBECTL_NAMESPACE="team-a",
            KUBECTL_PVC="claim-1",
        )
        self.assertTrue(line1.endswith(" target=team-a/claim-1"), line1)

    def test_explicit_name_equal_to_the_roots_adds_no_suffix(self):
        line1 = self._line1(TEST_DIRS={"/mnt/a": 1}, STORAGE_TARGET_NAME="/mnt/a")
        self.assertNotIn("target=", line1)

    def test_several_roots_still_show_the_target_that_line_one_cannot(self):
        # Line 1 collapses several TEST_DIRS keys to a marker, so the derived
        # (or recorded) name is the only place the roots appear.
        line1 = self._line1(
            TEST_DIRS={"/mnt/b": 1, "/mnt/a/": 1}, STORAGE_TARGET_NAME="/mnt/a,/mnt/b"
        )
        self.assertIn("(multiple TEST_DIRs)", line1)
        self.assertTrue(line1.endswith(" target=/mnt/a,/mnt/b"), line1)

    def test_several_roots_without_a_name_show_the_derived_target(self):
        line1 = self._line1(TEST_DIRS={"/mnt/b": 1, "/mnt/a": 1})
        self.assertTrue(line1.endswith(" target=/mnt/a,/mnt/b"), line1)

    def test_slash_only_root_equal_to_the_name_adds_no_suffix(self):
        line1 = self._line1(TEST_DIRS={"//": 1}, STORAGE_TARGET_NAME="/")
        self.assertNotIn("target=", line1)


if __name__ == "__main__":
    unittest.main()
