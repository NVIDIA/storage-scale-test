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

"""Shell-level tests for the dense (single flat directory) metadata layout."""

import subprocess
import textwrap
import unittest
from pathlib import Path
from typing import List

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ELBENCHO_FUNCTIONS = _REPO_ROOT / "lib" / "_elbencho_functions.sh"
_ENV_FUNCTIONS = _REPO_ROOT / "lib" / "env_functions.sh"
_FS_TESTS = _REPO_ROOT / "storage-tests" / "fs"
_SLURM_WORKER = _FS_TESTS / "sbatch" / "_nv-mdtest-elbencho.sh"
_SSH_ORCHESTRATOR = _FS_TESTS / "ssh" / "_nv-mdtest-elbencho.sh"
_SSH_SCRIPTLET = _FS_TESTS / "ssh" / "_nv-mdtest-elbencho-remote-scriptlet.sh"

_PHASE_SEPARATOR = "==="
_DENSE_TARGET_FILES = 1000000


def _run_bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        check=False,
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
    )


def _benchmark_script(
    layout_args: str,
    tasks_per_node: int = 64,
    nodes: int = 2,
    test_dir_count: int = 1,
    extra_setup: str = "",
) -> str:
    """Bash that runs the metadata benchmark with elbencho invocations mocked.

    Each mocked invocation appends its argv followed by a separator line, so the
    three phases can be split apart and asserted on individually.
    """
    hosts = ",".join(f"host-{index}" for index in range(nodes))
    targets = " ".join(
        f'"$tmp/fs/mdtest-elbencho-target-{index}-20260812Z120000"'
        for index in range(1, test_dir_count + 1)
    )
    return textwrap.dedent(f"""
        set -e
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        capture="$tmp/argv"
        run_an_elbencho() {{
            printf '%s\\n' "$@" >> "$capture"
            printf '{_PHASE_SEPARATOR}\\n' >> "$capture"
        }}
        export ELBENCHO_RUN_NODE_COUNT={nodes}
        export ELBENCHO_RUN_HOSTS_CSV={hosts}
        export output_dir="$tmp/mdtest-elbencho-20260812Z120000"
        declare -a targets=({targets})
        test_dirs_csv=$(IFS=,; printf '%s' "${{targets[*]}}")
        export test_dirs_csv
        export tasks_per_node={tasks_per_node}
        export MDTEST_ITERATIONS=1
        export MDTEST_BRANCH_FACTOR=7
        export MDTEST_ITEMS_PER_DIR=100
        export ELBENCHO_READ_AFTER_WRITE_PAUSE=0
        {extra_setup}
        _mdtest_export_layout_env {layout_args}
        run_elbencho_metadata_benchmark
        printf 'SUMMARY_END\\n'
        cat "$capture"
        """)


def _phases(stdout: str) -> List[List[str]]:
    """Split captured argv output into one token list per elbencho invocation."""
    _, _, captured = stdout.partition("SUMMARY_END\n")
    phases = []
    current: List[str] = []
    for line in captured.splitlines():
        if line == _PHASE_SEPARATOR:
            phases.append(current)
            current = []
        else:
            current.append(line)
    return phases


def _flag_value(argv: List[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


class _PhaseMixin(unittest.TestCase):
    """Named access to the three captured phases."""

    phases: List[List[str]] = []

    def _phase(self, index: int) -> List[str]:
        self.assertEqual(len(self.phases), 3)
        return self.phases[index]

    @property
    def create_phase(self) -> List[str]:
        return self._phase(0)

    @property
    def stat_phase(self) -> List[str]:
        return self._phase(1)

    @property
    def delete_phase(self) -> List[str]:
        return self._phase(2)


class TestDenseCommandConstruction(_PhaseMixin):
    """Dense mode builds one flat benchmark path with no directory phases."""

    def setUp(self) -> None:
        result = _run_bash(_benchmark_script("1000000 7813"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.result = result
        self.phases = _phases(result.stdout)

    def test_runs_exactly_three_phases(self) -> None:
        self.assertEqual(len(self.phases), 3)

    def test_single_benchmark_path_is_the_generated_target(self) -> None:
        for argv in self.phases:
            paths = [token for token in argv if "mdtest-elbencho-target" in token]
            self.assertEqual(len(paths), 1, argv)
            self.assertTrue(paths[0].endswith("-target-1-20260812Z120000"), paths)

    def test_dirs_per_thread_is_zero_and_files_per_worker_is_derived(self) -> None:
        for argv in self.phases:
            self.assertEqual(_flag_value(argv, "-n"), "0")
            self.assertEqual(_flag_value(argv, "-N"), "7813")

    def test_no_directory_create_or_delete_flags(self) -> None:
        self.assertIn("-w", self.create_phase)
        self.assertNotIn("-d", self.create_phase)
        self.assertIn("--stat", self.stat_phase)
        self.assertIn("-F", self.delete_phase)
        self.assertNotIn("-D", self.delete_phase)

    def test_zero_byte_files_and_latency_reporting_retained(self) -> None:
        for argv in self.phases:
            self.assertEqual(_flag_value(argv, "-s"), "0")
            self.assertEqual(_flag_value(argv, "-b"), "0")
            self.assertIn("--lat", argv)
            self.assertIn("--lathisto", argv)
            self.assertIn("--latpercent", argv)

    def test_host_rotation_preserved_between_create_and_later_phases(self) -> None:
        self.assertEqual(_flag_value(self.create_phase, "--hosts"), "host-0,host-1")
        self.assertEqual(_flag_value(self.create_phase, "--rotatehosts"), "1")
        self.assertEqual(_flag_value(self.stat_phase, "--hosts"), "host-1,host-0")
        self.assertEqual(_flag_value(self.delete_phase, "--hosts"), "host-1,host-0")

    def test_summary_reports_target_and_actual_counts(self) -> None:
        self.assertIn("Directory Layout:     dense", self.result.stdout)
        self.assertIn("Target Files:         1000000", self.result.stdout)
        self.assertIn("Actual Files:         1000064", self.result.stdout)


class TestDenseFileCountRounding(unittest.TestCase):
    """files_per_worker rounds to the nearest achievable total."""

    def _files_per_worker(self, target: int, nodes: int, tasks: int) -> str:
        script = textwrap.dedent(f"""
            set -e
            source "{_ENV_FUNCTIONS}"
            mdtest_single_dir_files_per_worker {target} {nodes} {tasks}
            """)
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_exactly_divisible_target_hits_it_exactly(self) -> None:
        # 2 nodes x 32 tasks = 64 workers; 1,000,000 / 64 = 15,625 exactly.
        per_worker = int(self._files_per_worker(_DENSE_TARGET_FILES, 2, 32))
        self.assertEqual(per_worker, 15625)
        self.assertEqual(2 * 32 * per_worker, _DENSE_TARGET_FILES)

    def test_rounded_target_reports_nearest_achievable_total(self) -> None:
        # 2 nodes x 64 tasks = 128 workers; 1,000,000 / 128 = 7812.5 -> 7813.
        per_worker = int(self._files_per_worker(_DENSE_TARGET_FILES, 2, 64))
        self.assertEqual(per_worker, 7813)
        self.assertEqual(2 * 64 * per_worker, 1000064)

    def test_error_stays_below_half_a_worker_group(self) -> None:
        for nodes, tasks in ((2, 48), (2, 96), (3, 64), (7, 13)):
            workers = nodes * tasks
            per_worker = int(self._files_per_worker(_DENSE_TARGET_FILES, nodes, tasks))
            actual = workers * per_worker
            self.assertLessEqual(abs(actual - _DENSE_TARGET_FILES), workers / 2)

    def test_target_smaller_than_worker_count_still_creates_files(self) -> None:
        self.assertEqual(self._files_per_worker(4, 2, 64), "1")

    def test_non_positive_target_is_rejected(self) -> None:
        for target in ("0", "-5", "abc", "1.5"):
            script = textwrap.dedent(f"""
                source "{_ENV_FUNCTIONS}"
                mdtest_single_dir_files_per_worker {target} 2 64
                """)
            result = _run_bash(script)
            self.assertNotEqual(result.returncode, 0, f"{target}: {result.stdout}")
            self.assertIn("positive integer", result.stderr)


class TestStandardLayoutRegression(_PhaseMixin):
    """Omitting the dense flag leaves the branched layout untouched."""

    def setUp(self) -> None:
        result = _run_bash(_benchmark_script('"" ""', tasks_per_node=16))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.result = result
        self.phases = _phases(result.stdout)

    def test_branch_factor_paths_are_passed(self) -> None:
        for argv in self.phases:
            paths = [token for token in argv if "mdtest-elbencho-target" in token]
            self.assertEqual(len(paths), 7, argv)
            self.assertTrue(paths[0].endswith("/b0"), paths)
            self.assertTrue(paths[-1].endswith("/b6"), paths)

    def test_dirs_and_files_per_thread_unchanged(self) -> None:
        for argv in self.phases:
            self.assertEqual(_flag_value(argv, "-n"), "49")
            self.assertEqual(_flag_value(argv, "-N"), "100")

    def test_directory_create_and_delete_flags_present(self) -> None:
        self.assertIn("-w", self.create_phase)
        self.assertIn("-d", self.create_phase)
        self.assertIn("-F", self.delete_phase)
        self.assertIn("-D", self.delete_phase)

    def test_summary_reports_branched_totals(self) -> None:
        self.assertIn("Directory Layout:     standard", self.result.stdout)
        self.assertIn("Total Dirs:           1568", self.result.stdout)
        self.assertIn("Total Files:          156800", self.result.stdout)

    def test_default_layout_applies_without_calling_export_helper(self) -> None:
        script = _benchmark_script('"" ""', tasks_per_node=16).replace(
            '_mdtest_export_layout_env "" ""', "unset MDTEST_LAYOUT"
        )
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        create = _phases(result.stdout)[0]
        self.assertIn("-d", create)
        self.assertEqual(_flag_value(create, "-n"), "49")


class TestDenseLayoutRejectsMultipleTargets(unittest.TestCase):
    """The dense workload needs exactly one shared directory."""

    def test_two_generated_targets_fail_with_a_clear_error(self) -> None:
        result = _run_bash(_benchmark_script("1000000 7813", test_dir_count=2))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("requires exactly one", result.stderr)
        self.assertEqual(_phases(result.stdout), [])

    def test_missing_files_per_worker_fails(self) -> None:
        result = _run_bash(_benchmark_script('1000000 ""'))
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("MDTEST_SINGLE_DIR_FILES_PER_WORKER", result.stderr)


class TestDenseCleanupSafety(unittest.TestCase):
    """Cleanup removes the generated target but never its configured parent."""

    def _cleanup_script(self, layout_args: str, seed_paths: str) -> str:
        script = _benchmark_script(layout_args, tasks_per_node=4)
        # Seed files into the target after preparation but before the run ends,
        # by mocking run_an_elbencho to create them on the create phase.
        script = script.replace(
            "run_an_elbencho() {",
            textwrap.dedent(f"""
                run_an_elbencho() {{
                    if [[ ! -e "$tmp/seeded" ]]; then
                        touch "$tmp/seeded"
                        {seed_paths}
                    fi
                """).strip() + "\n",
        )
        return script.replace(
            'cat "$capture"',
            'printf \'ROOT_EXISTS=%s\\n\' "$([[ -d "$tmp/fs" ]] && echo yes || echo no)"\n'
            "        "
            'printf \'TARGETS_LEFT=%s\\n\' "$(find "$tmp/fs" -mindepth 1 | wc -l | tr -d " ")"',
        )

    def test_dense_target_removed_and_configured_root_survives(self) -> None:
        seed = (
            'mkdir -p "$tmp/fs/mdtest-elbencho-target-1-20260812Z120000" && '
            'touch "$tmp/fs/mdtest-elbencho-target-1-20260812Z120000/r0-f0" '
            '"$tmp/fs/mdtest-elbencho-target-1-20260812Z120000/r1-f0"'
        )
        result = _run_bash(self._cleanup_script("1000000 7813", seed))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ROOT_EXISTS=yes", result.stdout)
        self.assertIn("TARGETS_LEFT=0", result.stdout)

    def test_standard_target_removed_and_configured_root_survives(self) -> None:
        seed = (
            'mkdir -p "$tmp/fs/mdtest-elbencho-target-1-20260812Z120000/b0/r0/d0" && '
            'touch "$tmp/fs/mdtest-elbencho-target-1-20260812Z120000/b0/r0/d0/f0"'
        )
        result = _run_bash(self._cleanup_script('"" ""', seed))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ROOT_EXISTS=yes", result.stdout)
        self.assertIn("TARGETS_LEFT=0", result.stdout)


class TestExecutionSubstrateParity(unittest.TestCase):
    """Slurm and SSH paths hand the same layout parameters to the benchmark."""

    def test_layout_export_helper_sets_all_three_variables(self) -> None:
        script = textwrap.dedent(f"""
            set -e
            source "{_ELBENCHO_FUNCTIONS}"
            _mdtest_export_layout_env 1000000 7813
            printf '%s|%s|%s\\n' "$MDTEST_LAYOUT" \\
                "$MDTEST_SINGLE_DIR_TARGET_FILES" \\
                "$MDTEST_SINGLE_DIR_FILES_PER_WORKER"
            _mdtest_export_layout_env "" ""
            printf '%s|%s|%s\\n' "$MDTEST_LAYOUT" \\
                "$MDTEST_SINGLE_DIR_TARGET_FILES" \\
                "$MDTEST_SINGLE_DIR_FILES_PER_WORKER"
            """)
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            ["single-dir|1000000|7813", "standard||"],
        )

    def test_layout_variables_are_exported_to_child_processes(self) -> None:
        script = textwrap.dedent(f"""
            set -e
            source "{_ELBENCHO_FUNCTIONS}"
            _mdtest_export_layout_env 1000000 7813
            bash -c 'printf "%s|%s\\n" "$MDTEST_LAYOUT" \\
                "$MDTEST_SINGLE_DIR_FILES_PER_WORKER"'
            """)
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "single-dir|7813")

    def test_both_substrates_read_the_same_positional_arguments(self) -> None:
        slurm = _SLURM_WORKER.read_text(encoding="utf-8")
        ssh_orchestrator = _SSH_ORCHESTRATOR.read_text(encoding="utf-8")
        scriptlet = _SSH_SCRIPTLET.read_text(encoding="utf-8")

        for source in (slurm, ssh_orchestrator, scriptlet):
            self.assertIn('single_dir_target_files="${', source)
            self.assertIn('single_dir_files_per_worker="${', source)

        # Only the two paths that actually run the benchmark export the layout.
        for source in (slurm, scriptlet):
            self.assertIn(
                '_mdtest_export_layout_env "$single_dir_target_files" '
                '"$single_dir_files_per_worker"',
                source,
            )
        # The SSH orchestrator forwards them to the remote scriptlet instead.
        self.assertIn(
            '"$single_dir_target_files" "$single_dir_files_per_worker"',
            ssh_orchestrator,
        )


if __name__ == "__main__":
    unittest.main()
