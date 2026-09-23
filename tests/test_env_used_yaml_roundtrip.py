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

"""Round-trip tests: bash env snapshot writers -> env_used.yaml -> Python yaml.safe_load.

Covers ``write_elbencho_env_used`` (IO sweep) and ``write_mdtest_elbencho_env_used``
(mdtest-elbencho sweep).

Requires PyYAML (same as utils/extract-elbencho.py). Run from repo root, e.g.:
  python3 -m unittest tests.test_env_used_yaml_roundtrip
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

# Repo root: .../storage-scale-test/tests/ -> parent
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# pylint: disable=wrong-import-position
from lib.env_used_yaml import apply_env_used_to_metrics, load_env_used_yaml

# Shared bash fragment: env_functions source + sample TEST_DIRS + FS limits (both writers).
_BASH_SOURCE_AND_FS = """\
declare -A TEST_DIRS
TEST_DIRS['/mnt/fs1']=2
TEST_DIRS['/mnt/foo bar/baz']=1
export FS_MAX_AGG_THROUGHPUT=9000
export FS_MAX_NODE_THROUGHPUT_GBPS=12
export FS_MAX_NODE_IOPS=500000
"""


def _bash_script_with_repo(body_after_source: str) -> str:
    repo = str(_REPO_ROOT)
    return f"""set -euo pipefail
# shellcheck source=lib/env_functions.sh
source "{repo}/lib/env_functions.sh"
{_BASH_SOURCE_AND_FS}{body_after_source}"""


def _run_bash_write(out_dir: str, script: str) -> None:
    subprocess.run(["bash", "-c", script], check=True, cwd=out_dir)


def _assert_yaml_nonempty_with_common_fs(
    testcase: unittest.TestCase, loaded: dict
) -> None:
    testcase.assertIsInstance(loaded, dict)
    testcase.assertNotEqual(loaded, {})
    testcase.assertEqual(loaded["EXECUTION_SUBSTRATE"], "slurm")
    td = loaded.get("TEST_DIRS")
    testcase.assertIsInstance(td, dict)
    testcase.assertEqual(td["/mnt/fs1"], 2)
    testcase.assertEqual(td["/mnt/foo bar/baz"], 1)
    testcase.assertEqual(loaded["FS_MAX_AGG_THROUGHPUT"], 9000)
    testcase.assertEqual(loaded["FS_MAX_NODE_THROUGHPUT_GBPS"], 12)
    testcase.assertEqual(loaded["FS_MAX_NODE_IOPS"], 500000)


def _bash_write_env_used_yaml(out_dir: str) -> str:
    """Bash script body: IO sweep env + write_elbencho_env_used."""
    return _bash_script_with_repo(
        """export EXECUTION_SUBSTRATE=slurm
export ELBENCHO_SCALE_THREAD_LIST=("1" "2" "4" "8" "16")
export ELBENCHO_SCALE_IO_SIZES=("1M" "r4K" "4K,8K")
export ELBENCHO_IODEPTH_LIST=("1" "4" "8")
export ELBENCHO_FILE_SIZE_MULTIPLIER=16384
export ELBENCHO_FILE_LAYOUT=shared-directory
export ELBENCHO_FILES_PER_NODE=8
export ELBENCHO_FILE_SIZE=64G
export ELBENCHO_SCALE_READ_WRITE_DURATION=600
export ELBENCHO_READ_AFTER_WRITE_PAUSE=5
export ELBENCHO_LIVE_CSV_EXTENDED=1
export ELBENCHO_LIVEINT=750
export ELBENCHO_SINGLE_BIG_FILE=1
export ELBENCHO_SINGLE_BIG_FILE_BASENAME=test-bigfile
export ELBENCHO_SINGLE_BIG_FILE_SIZE=700G
export ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=1
"""
        + f'write_elbencho_env_used "{out_dir}/env_used.yaml" \\\n'
        + '  "dio" "0" "0" "0" "0" "" "1-4"\n'
    )


def _bash_write_env_used_yaml_write_no_read(out_dir: str) -> str:
    """Bash script body: IO sweep env + write_elbencho_env_used (--write-no-read)."""
    return _bash_script_with_repo(
        """export EXECUTION_SUBSTRATE=slurm
export ELBENCHO_SCALE_THREAD_LIST=("8")
export ELBENCHO_SCALE_IO_SIZES=("1M")
export ELBENCHO_IODEPTH_LIST=("1")
export ELBENCHO_FILE_SIZE_MULTIPLIER=1024
export ELBENCHO_SCALE_READ_WRITE_DURATION=60
export ELBENCHO_READ_AFTER_WRITE_PAUSE=0
export ELBENCHO_SINGLE_BIG_FILE=0
export ELBENCHO_SINGLE_BIG_FILE_BASENAME=elbencho-bigfile
export ELBENCHO_SINGLE_BIG_FILE_SIZE=
export ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0
"""
        + f'write_elbencho_env_used "{out_dir}/env_used.yaml" \\\n'
        + '  "dio" "0" "0" "0" "1" "" "2-4"\n'
    )


def _bash_write_mdtest_env_used_yaml(out_dir: str, dense_args: str = "") -> str:
    """Bash script body: mdtest sweep env + write_mdtest_elbencho_env_used."""
    return _bash_script_with_repo(
        """export EXECUTION_SUBSTRATE=slurm
export MDTEST_BRANCH_FACTOR=7
export MDTEST_ITEMS_PER_DIR=100
export MDTEST_ITERATIONS=3
export ELBENCHO_READ_AFTER_WRITE_PAUSE=5
"""
        + f'write_mdtest_elbencho_env_used "{out_dir}/env_used.yaml" '
        + f'"1,2,4" "16-32+16" {dense_args}\n'
    )


def _metric_stub():
    """Minimal object with flags apply_env_used_to_metrics mutates."""
    return SimpleNamespace(
        is_single_big_file=False,
        all_nodes_all_data=False,
        sweep_single_option=False,
    )


class TestEnvUsedYamlRoundTrip(unittest.TestCase):
    """write_elbencho_env_used (bash) and load_env_used_yaml (Python)."""

    def test_round_trip_dict_and_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run_bash_write(tmp, _bash_write_env_used_yaml(tmp))

            loaded = load_env_used_yaml(tmp)
            _assert_yaml_nonempty_with_common_fs(self, loaded)

            self.assertEqual(
                loaded["ELBENCHO_SCALE_THREAD_LIST"],
                ["1", "2", "4", "8", "16"],
            )
            self.assertEqual(
                loaded["ELBENCHO_SCALE_IO_SIZES"],
                ["1M", "r4K", "4K,8K"],
            )
            self.assertEqual(
                loaded["ELBENCHO_IODEPTH_LIST"],
                ["1", "4", "8"],
            )

            self.assertEqual(loaded["ELBENCHO_FILE_SIZE_MULTIPLIER"], 16384)
            self.assertEqual(loaded["ELBENCHO_FILE_LAYOUT"], "shared-directory")
            self.assertEqual(loaded["ELBENCHO_FILES_PER_NODE"], "8")
            self.assertEqual(loaded["ELBENCHO_FILE_SIZE"], "64G")
            self.assertEqual(loaded["ELBENCHO_SCALE_READ_WRITE_DURATION"], 600)
            self.assertEqual(loaded["ELBENCHO_READ_AFTER_WRITE_PAUSE"], 5)
            self.assertEqual(loaded["ELBENCHO_LIVE_CSV_EXTENDED"], 1)
            self.assertEqual(loaded["ELBENCHO_LIVEINT"], 750)

            self.assertEqual(loaded["ELBENCHO_SINGLE_BIG_FILE"], 1)
            self.assertEqual(
                loaded["ELBENCHO_SINGLE_BIG_FILE_BASENAME"], "test-bigfile"
            )
            self.assertEqual(loaded["ELBENCHO_SINGLE_BIG_FILE_SIZE"], "700G")
            self.assertEqual(loaded["ELBENCHO_ALL_NODES_ACCESS_ALL_DATA"], 1)

            self.assertEqual(loaded["dio_or_bio"], "dio")
            self.assertEqual(loaded["rand_option"], 0)
            self.assertEqual(loaded["single_option"], 0)
            self.assertEqual(loaded["sweep_write_only"], 0)
            self.assertEqual(loaded["sweep_write_no_read"], 0)
            self.assertEqual(loaded["sweep_read_from"], "")
            self.assertEqual(loaded["nodes_spec"], "1-4")

    def test_write_no_read_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run_bash_write(tmp, _bash_write_env_used_yaml_write_no_read(tmp))

            loaded = load_env_used_yaml(tmp)
            self.assertEqual(loaded["sweep_write_only"], 0)
            self.assertEqual(loaded["sweep_write_no_read"], 1)
            self.assertEqual(loaded["sweep_read_from"], "")
            self.assertEqual(loaded["nodes_spec"], "2-4")

            shell_snapshot = (Path(tmp) / "env_used.sh").read_text(encoding="utf-8")
            self.assertIn("export sweep_write_only=0\n", shell_snapshot)
            self.assertIn("export sweep_write_no_read=1\n", shell_snapshot)
            self.assertIn("export sweep_read_from=''\n", shell_snapshot)
            self.assertIn("export nodes_spec=2-4\n", shell_snapshot)
            self.assertIn(
                "export ELBENCHO_FILE_LAYOUT=worker-directories\n",
                shell_snapshot,
            )
            self.assertIn("export ELBENCHO_FILES_PER_NODE=''\n", shell_snapshot)
            self.assertIn("export ELBENCHO_FILE_SIZE=''\n", shell_snapshot)

    def test_apply_env_used_sets_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run_bash_write(tmp, _bash_write_env_used_yaml(tmp))

            loaded = load_env_used_yaml(tmp)
            metric = _metric_stub()
            self.assertFalse(metric.is_single_big_file)
            self.assertFalse(metric.all_nodes_all_data)

            apply_env_used_to_metrics(loaded, [metric])
            self.assertTrue(metric.is_single_big_file)
            self.assertTrue(metric.all_nodes_all_data)
            self.assertFalse(metric.sweep_single_option)
            self.assertEqual(metric.configured_file_layout, "shared-directory")
            self.assertEqual(metric.configured_files_per_node, "8")
            self.assertEqual(metric.configured_file_size, "64G")

    def test_apply_env_used_sets_sweep_single_option(self):
        metric = _metric_stub()
        apply_env_used_to_metrics({"single_option": 1}, [metric])
        self.assertTrue(metric.sweep_single_option)
        self.assertFalse(metric.is_single_big_file)
        self.assertFalse(metric.all_nodes_all_data)

    def test_apply_env_used_preserves_raw_shared_configuration(self):
        metric = _metric_stub()
        apply_env_used_to_metrics(
            {
                "ELBENCHO_FILE_LAYOUT": "shared-directory",
                "ELBENCHO_FILES_PER_NODE": "16",
                "ELBENCHO_FILE_SIZE": "128M",
            },
            [metric],
        )
        self.assertEqual(metric.configured_file_layout, "shared-directory")
        self.assertEqual(metric.configured_files_per_node, "16")
        self.assertEqual(metric.configured_file_size, "128M")

    def test_kubernetes_snapshot_preserves_runtime_identity_and_mapped_paths(self):
        """Resume snapshots retain Kubernetes identity, not only env.sh defaults."""
        with tempfile.TemporaryDirectory() as tmp:
            script = _bash_script_with_repo("""export EXECUTION_SUBSTRATE=kubectl
export KUBECTL_NAMESPACE=test-ns
export KUBECTL_PV=test-pv
export KUBECTL_PVC=test-pvc
export KUBECTL_NODE_SELECTOR=storage-test=true
export KUBECTL_ELBENCHO_IMAGE=example.invalid/elbencho@sha256:abc
export KUBECTL_IMAGE_PULL_POLICY=Never
export KUBECTL_RUN_AS_USER=2000
export KUBECTL_RUN_AS_GROUP=2000
export KUBECTL_MAPPED_READ_FROM=/mnt/storage-scale-test/bench/input
declare -A KUBECTL_MAPPED_TEST_DIRS=(
  [/mnt/storage-scale-test/bench/primary]=2
  [/mnt/storage-scale-test/bench/secondary]=1
)
export ELBENCHO_SCALE_THREAD_LIST=("1")
export ELBENCHO_SCALE_IO_SIZES=("4K")
export ELBENCHO_IODEPTH_LIST=("1")
export ELBENCHO_FILE_SIZE_MULTIPLIER=1024
export ELBENCHO_FILE_LAYOUT=worker-directories
export ELBENCHO_FILES_PER_NODE=
export ELBENCHO_FILE_SIZE=
export ELBENCHO_SCALE_READ_WRITE_DURATION=60
export ELBENCHO_READ_AFTER_WRITE_PAUSE=0
export ELBENCHO_LIVE_CSV_EXTENDED=0
export ELBENCHO_LIVEINT=1000
export ELBENCHO_SINGLE_BIG_FILE=0
export ELBENCHO_SINGLE_BIG_FILE_BASENAME=elbencho-bigfile
export ELBENCHO_SINGLE_BIG_FILE_SIZE=
export ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0
""" + f'write_elbencho_env_used "{tmp}/env_used.yaml" dio 0 0 0 0 "" 1\n')
            _run_bash_write(tmp, script)
            loaded = load_env_used_yaml(tmp)
            self.assertEqual(loaded["KUBECTL_NAMESPACE"], "test-ns")
            self.assertEqual(loaded["KUBECTL_PV"], "test-pv")
            self.assertEqual(loaded["KUBECTL_PVC"], "test-pvc")
            self.assertEqual(
                loaded["KUBECTL_MAPPED_TEST_DIRS"][
                    "/mnt/storage-scale-test/bench/primary"
                ],
                2,
            )
            shell_snapshot = (Path(tmp) / "env_used.sh").read_text(encoding="utf-8")
            self.assertIn("export KUBECTL_NAMESPACE=test-ns", shell_snapshot)
            self.assertIn("KUBECTL_MAPPED_TEST_DIRS", shell_snapshot)

    def test_mdtest_elbencho_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run_bash_write(tmp, _bash_write_mdtest_env_used_yaml(tmp))

            loaded = load_env_used_yaml(tmp)
            _assert_yaml_nonempty_with_common_fs(self, loaded)

            self.assertEqual(loaded["MDTEST_BRANCH_FACTOR"], 7)
            self.assertEqual(loaded["MDTEST_ITEMS_PER_DIR"], 100)
            self.assertEqual(loaded["MDTEST_ITERATIONS"], 3)
            self.assertEqual(loaded["ELBENCHO_READ_AFTER_WRITE_PAUSE"], 5)

            self.assertEqual(loaded["nodes_spec"], "1,2,4")
            self.assertEqual(loaded["tasks_spec"], "16-32+16")

            # Standard layout leaves the dense-mode file counts unset.
            self.assertEqual(loaded["mdtest_layout"], "standard")
            self.assertIsNone(loaded["single_dir_target_files"])
            self.assertIsNone(loaded["single_dir_files_per_worker"])
            self.assertIsNone(loaded["single_dir_actual_files"])

    def test_mdtest_elbencho_single_dir_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            _run_bash_write(
                tmp,
                _bash_write_mdtest_env_used_yaml(tmp, '"1000000" "7813" "1000064"'),
            )

            loaded = load_env_used_yaml(tmp)
            _assert_yaml_nonempty_with_common_fs(self, loaded)

            self.assertEqual(loaded["mdtest_layout"], "single-dir")
            self.assertEqual(loaded["single_dir_target_files"], 1000000)
            self.assertEqual(loaded["single_dir_files_per_worker"], 7813)
            self.assertEqual(loaded["single_dir_actual_files"], 1000064)

            # The branched-layout settings are still recorded, unused but visible.
            self.assertEqual(loaded["MDTEST_BRANCH_FACTOR"], 7)
            self.assertEqual(loaded["MDTEST_ITERATIONS"], 3)


if __name__ == "__main__":
    unittest.main()
