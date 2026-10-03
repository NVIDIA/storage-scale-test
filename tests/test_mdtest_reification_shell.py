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

"""Fast contracts for metadata execution definitions and retry isolation."""

import subprocess
import shutil
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


class TestMdtestReification(unittest.TestCase):
    """The ledger freezes workload values and gives each cell private paths."""

    def test_cartesian_definitions_and_statuses(self) -> None:
        """A metadata sweep records node/task order and distinct target suffixes."""
        with tempfile.TemporaryDirectory() as temp_dir:
            script = f"""
                source '{_ROOT}/lib/env_functions.sh'
                source '{_ROOT}/lib/_elbencho_functions.sh'
                declare -A TEST_DIRS=(['{temp_dir}/root']=1)
                MDTEST_BRANCH_FACTOR=2 MDTEST_ITEMS_PER_DIR=3 MDTEST_ITERATIONS=4
                ELBENCHO_READ_AFTER_WRITE_PAUSE=0
                node_counts=(1 2)
                task_counts=(3 5)
                reify_all_mdtest_executions '{temp_dir}/mdtest-elbencho-20260929Z120000' \
                    20260929Z120000 node_counts task_counts '' ''
            """
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            executions = (
                Path(temp_dir) / "mdtest-elbencho-20260929Z120000" / "executions"
            )
            definitions = sorted(executions.glob("*.sh"))
            self.assertEqual(
                [path.stem for path in definitions], ["0001", "0002", "0003", "0004"]
            )
            self.assertEqual(
                [
                    (
                        "nodes=1" in path.read_text(),
                        "tasks_per_node=3" in path.read_text(),
                    )
                    for path in definitions
                ],
                [(True, True), (True, False), (False, True), (False, False)],
            )
            for definition in definitions:
                data = definition.read_text(encoding="utf-8")
                self.assertIn("ELBENCHO_EXECUTION_KIND=mdtest", data)
                self.assertIn(f"-e{definition.stem}", data)
                self.assertEqual(
                    definition.with_suffix(".status").read_text(), "PENDING\n"
                )

    def test_retry_clears_only_its_generated_target(self) -> None:
        """An interrupted cell cannot contaminate its next CREATE phase."""
        with tempfile.TemporaryDirectory() as temp_dir:
            script = f"""
                source '{_ROOT}/lib/_elbencho_functions.sh'
                export output_dir='{temp_dir}/mdtest-elbencho-20260929Z120000'
                export ELBENCHO_RUN_REMOTE_OUTPUT_DIR="$output_dir"
                export ELBENCHO_RUN_NODE_COUNT=1
                export ELBENCHO_EXECUTION_KIND=mdtest
                export ELBENCHO_RUN_EXECUTION_ID=0001
                export ELBENCHO_RUN_TEST_DIR_SUFFIX=-20260929Z120000-e0001
                export test_dirs_csv='{temp_dir}/mdtest-elbencho-target-1-20260929Z120000-e0001'
                export tasks_per_node=1 MDTEST_BRANCH_FACTOR=1
                export MDTEST_ITEMS_PER_DIR=2 MDTEST_ITERATIONS=2
                export MDTEST_LAYOUT=standard ELBENCHO_READ_AFTER_WRITE_PAUSE=0
                mkdir -p "$test_dirs_csv" "$output_dir"
                touch "$test_dirs_csv/stale"
                run_an_elbencho() {{
                    [[ ! -e "$test_dirs_csv/stale" ]] || return 1
                    local arg
                    for arg in "$@"; do
                        case "$arg" in
                            --resfile=*) printf 'result\\n' >> "${{arg#--resfile=}}" ;;
                            --csvfile=*) printf 'result\\n' >> "${{arg#--csvfile=}}" ;;
                        esac
                    done
                }}
                run_elbencho_metadata_benchmark || exit 1
                mkdir -p "$test_dirs_csv"
                touch "$test_dirs_csv/stale"
                run_elbencho_metadata_benchmark || exit 1
                [[ ! -e "$test_dirs_csv" ]]
            """
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = Path(temp_dir) / "mdtest-elbencho-20260929Z120000"
            for suffix in ("csv", "out"):
                artifacts = sorted(output.glob(f"*.{suffix}"))
                self.assertEqual(len(artifacts), 2)
                for artifact in artifacts:
                    self.assertEqual(artifact.read_text().splitlines(), ["result"] * 3)
            self.assertEqual(
                (output / "executions/0001.mdtest.complete").read_text(),
                "COMPLETE\n",
            )

    def test_preflight_failure_removes_previous_result_evidence(self) -> None:
        """A failed target reset cannot leave reportable retry artifacts."""
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "mdtest-elbencho-20260929Z120000"
            target = Path(temp_dir) / "mdtest-elbencho-target-1-20260929Z120000-e0001"
            output.mkdir()
            (output / "executions").mkdir()
            target.mkdir()
            base = output / "mdtest-elbencho-c_001-t_001_20260929Z120000_iter1"
            for suffix in ("out", "csv"):
                base.with_suffix(f".{suffix}").write_text("old result\n")
            marker = output / "executions/0001.mdtest.complete"
            marker.write_text("COMPLETE\n")
            script = f"""
                source '{_ROOT}/lib/_elbencho_functions.sh'
                export output_dir='{output}'
                export ELBENCHO_RUN_REMOTE_OUTPUT_DIR="$output_dir"
                export ELBENCHO_RUN_NODE_COUNT=1 ELBENCHO_RUN_EXECUTION_ID=0001
                export ELBENCHO_EXECUTION_KIND=mdtest
                export ELBENCHO_RUN_TEST_DIR_SUFFIX=-20260929Z120000-e0001
                export test_dirs_csv='{target}' tasks_per_node=1
                export MDTEST_BRANCH_FACTOR=1 MDTEST_ITEMS_PER_DIR=2
                export MDTEST_ITERATIONS=1 MDTEST_LAYOUT=standard
                rm() {{
                    if [[ "$1" == -rf && "$3" == "$test_dirs_csv" ]]; then
                        return 1
                    fi
                    command rm "$@"
                }}
                if run_elbencho_metadata_benchmark; then exit 1; fi
            """
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for suffix in ("out", "csv"):
                self.assertFalse(base.with_suffix(f".{suffix}").exists())
            self.assertFalse(marker.exists())

    def test_success_artifacts_must_be_nonempty_regular_files(self) -> None:
        """The shared SSH and Slurm gate rejects empty files and symlinks."""
        with tempfile.TemporaryDirectory() as temp_dir:
            artifact = Path(temp_dir) / "result.csv"
            link = Path(temp_dir) / "linked.csv"
            marker = Path(temp_dir) / "0001.mdtest.complete"
            script = f"""
                source '{_ROOT}/lib/env_functions.sh'
                source '{_ROOT}/lib/_elbencho_functions.sh'
                _elbencho_required_artifact_is_valid '{artifact}' && exit 1
                printf 'data\\n' > '{artifact}'
                _elbencho_required_artifact_is_valid '{artifact}' || exit 1
                ln -s '{artifact}' '{link}'
                _elbencho_required_artifact_is_valid '{link}' && exit 1
                : > '{artifact}'
                _elbencho_required_artifact_is_valid '{artifact}' && exit 1
                printf 'incomplete\\n' > '{marker}'
                _elbencho_required_artifact_is_valid '{marker}' && exit 1
                printf 'COMPLETE\\n' > '{marker}'
                _elbencho_required_artifact_is_valid '{marker}' || exit 1
                exit 0
            """
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_launcher_resume_uses_saved_ledger(self) -> None:
        """The CLI resumes its original definitions without expanding new cells."""
        with tempfile.TemporaryDirectory() as temp_dir:
            deployment = Path(temp_dir) / "deployment"
            launcher = deployment / "storage-tests/fs/nv-mdtest-elbencho.sh"
            launcher.parent.mkdir(parents=True)
            shutil.copy2(_ROOT / "storage-tests/fs/nv-mdtest-elbencho.sh", launcher)
            (deployment / "lib").mkdir()
            shutil.copy2(_ROOT / "lib/project_version.sh", deployment / "lib")
            results = Path(temp_dir) / "results"
            test_root = Path(temp_dir) / "data"
            env_file = deployment / "env.sh"
            env_file.write_text(
                f"""
                SCALE_TEST_BASE='{_ROOT}'
                RESULTS_DIR='{results}'
                EXECUTION_SUBSTRATE=ssh SSH_ENABLED=1 SLURM_ENABLED= KUBECTL_ENABLED=
                FS_ENABLED=1 ORDER_NODES=0 ORDER_NODES_ENABLED=
                FS_MAX_AGG_THROUGHPUT=1 FS_MAX_NODE_THROUGHPUT_GBPS=1
                FS_MAX_NODE_IOPS=1 ELBENCHO_READ_AFTER_WRITE_PAUSE=0
                MDTEST_BRANCH_FACTOR=1 MDTEST_ITEMS_PER_DIR=2 MDTEST_ITERATIONS=1
                declare -A TEST_DIRS=(['{test_root}']=1)
                source '{_ROOT}/lib/env_functions.sh'
                dispatch_ssh_executions() {{ printf 'DISPATCH:%s\\n' "$1"; }}
                """,
                encoding="utf-8",
            )
            first = subprocess.run(
                ["bash", str(launcher), "--nodes", "1,2", "--tasks", "1"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            output = next(results.glob("mdtest-elbencho-*"))
            definitions = sorted((output / "executions").glob("*.sh"))
            self.assertEqual(len(definitions), 2)
            self.assertTrue((output / "env_used.sh").is_file())
            second = subprocess.run(
                ["bash", str(launcher), "--resume", str(output)],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(sorted((output / "executions").glob("*.sh")), definitions)
            self.assertIn("DISPATCH:", second.stdout)


if __name__ == "__main__":
    unittest.main()
