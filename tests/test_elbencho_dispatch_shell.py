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

"""Shell-level tests for elbencho per-execution dispatch/resume helpers."""

import os
import shutil
import subprocess
import textwrap
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ELBENCHO_FUNCTIONS = _REPO_ROOT / "lib" / "_elbencho_functions.sh"
_SLURM_COORDINATOR = (
    _REPO_ROOT / "storage-tests" / "fs" / "sbatch" / "_nv-elbencho-coordinator.sh"
)
_ENV_FUNCTIONS = _REPO_ROOT / "lib" / "env_functions.sh"
_BASH = shutil.which("bash") or "bash"


def _run_bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_BASH, "-c", script],
        check=False,
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
    )


class TestElbenchoDispatchShell(unittest.TestCase):
    """Dispatch helpers preserve per-execution state across retries."""

    def test_slurm_dispatch_signals_cancel_once_and_preserve_exit_trap(self) -> None:
        """INT and TERM cancel once before the caller's EXIT cleanup runs."""
        for signal_name, expected_rc in (("INT", 130), ("TERM", 143)):
            with self.subTest(signal=signal_name):
                with tempfile.TemporaryDirectory() as temp_dir:
                    record = Path(temp_dir) / "record"
                    script = f"""
                    source "{_ENV_FUNCTIONS}"
                    RECORD={record!s}
                    trap 'printf "owner\\n" >> "$RECORD"' EXIT
                    scancel() {{ printf 'cancel\\n' >> "$RECORD"; }}
                    squeue() {{ return 0; }}
                    _slurm_arm_dispatch_cleanup 4242
                    kill -{signal_name} $$
                    """
                    result = _run_bash(textwrap.dedent(script))
                    self.assertEqual(result.returncode, expected_rc, result.stderr)
                    self.assertEqual(
                        record.read_text(encoding="utf-8").splitlines(),
                        ["cancel", "owner"],
                    )

    def test_slurm_dispatch_exit_cancels_once_and_runs_inherited_exit(self) -> None:
        """Unexpected shell exit retains both job and caller cleanup actions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            record = Path(temp_dir) / "record"
            script = f"""
            source "{_ENV_FUNCTIONS}"
            RECORD={record!s}
            trap 'printf "owner\\n" >> "$RECORD"' EXIT
            scancel() {{ printf 'cancel\\n' >> "$RECORD"; }}
            squeue() {{ return 0; }}
            _slurm_arm_dispatch_cleanup 4242
            exit 9
            """
            result = _run_bash(textwrap.dedent(script))
            self.assertEqual(result.returncode, 9, result.stderr)
            self.assertEqual(
                record.read_text(encoding="utf-8").splitlines(),
                ["cancel", "owner"],
            )

    def test_reified_execution_records_stable_test_dirs(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        declare -gA TEST_DIRS=(["$tmp/fs"]=1)
        DS=20260731Z010203
        nodes_spec=4
        rand_option=0
        ELBENCHO_SCALE_THREAD_LIST=(8)
        ELBENCHO_SCALE_IO_SIZES=(r64K)
        ELBENCHO_IODEPTH_LIST=(2)
        ELBENCHO_SCALE_READ_WRITE_DURATION=60s
        ELBENCHO_FILE_SIZE_MULTIPLIER=1024
        ELBENCHO_FILE_LAYOUT=shared-directory
        ELBENCHO_FILES_PER_NODE=8
        ELBENCHO_FILE_SIZE=64G
        ELBENCHO_READ_AFTER_WRITE_PAUSE=0
        FS_MAX_AGG_THROUGHPUT=0
        FS_MAX_NODE_THROUGHPUT_GBPS=0
        FS_MAX_NODE_IOPS=0
        ELBENCHO_SINGLE_BIG_FILE=0
        ELBENCHO_SINGLE_BIG_FILE_BASENAME=elbencho-bigfile
        ELBENCHO_SINGLE_BIG_FILE_SIZE=
        ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0
        mkdir -p "$tmp/results/elbencho-$DS/executions"
        reify_elbencho_execution \
            "$tmp/results/elbencho-$DS/executions/0007.sh" \
            4 r64K 8 2 0 dio 0 0 0 0 ""
        source "$tmp/results/elbencho-$DS/executions/0007.sh"
        [[ "$ELBENCHO_SWEEP_WRITE_NO_READ" == 0 ]]
        [[ "$ELBENCHO_FILE_LAYOUT" == shared-directory ]]
        [[ "$ELBENCHO_FILES_PER_NODE" == 8 ]]
        [[ "$ELBENCHO_FILE_SIZE" == 64G ]]
        SLURM_JOB_ID=999999
        _compute_test_dirs_csv_for_execution
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            result.stdout.strip().endswith(
                "/fs/elbencho-sweep-target-1-20260731Z010203-e0007"
            ),
            result.stdout,
        )
        self.assertNotIn("999999", result.stdout)

    def test_result_artifact_cleanup_removes_csv_out_live_and_treefile(self) -> None:
        script = f"""
        set -e
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        _elbencho_io_set_resfile_csvfile "$tmp" r64K 4 8 2 20260731Z010203
        _elbencho_io_set_livecsvfile "$tmp" r64K 4 8 2 20260731Z010203
        _elbencho_io_set_treefile "$tmp" r64K 4 8 2 20260731Z010203
        touch "$resfile" "$csvfile" "$livecsvfile" "$treefile"
        _elbencho_io_cleanup_result_artifacts \
            "$resfile" "$csvfile" "$livecsvfile" "$treefile"
        for f in "$resfile" "$csvfile" "$livecsvfile" "$treefile"; do
            [[ ! -e "$f" ]] || exit 1
        done
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_slurm_execution_log_is_teed_live_and_preserves_failure(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        output_dir="$tmp/elbencho-20260804Z160341"
        executions_dir="$output_dir/executions"
        mkdir -p "$executions_dir"
        cat > "$executions_dir/0001.sh" <<'EOF'
        export nodes=1
        export io_size=1M
        export thread_count=64
        export io_depth=4
        export dio_or_bio=dio
        export use_random=0
        export force_single=1
        export ELBENCHO_FILE_LAYOUT=shared-directory
        export ELBENCHO_FILES_PER_NODE=8
        export ELBENCHO_FILE_SIZE=64G
        EOF
        printf 'PENDING\n' > "$executions_dir/0001.status"
        printf 'stale\n' > "$executions_dir/0001.log"
        _compute_test_dirs_csv_for_execution() {{ printf '/tmp/target'; }}
        run_elbencho_io_sweep_iteration() {{
            [[ "$ELBENCHO_RUN_CONTEXT_READY" == 1 ]]
            [[ "$ELBENCHO_RUN_NODE_COUNT" == 1 ]]
            [[ "$ELBENCHO_RUN_HOSTS_CSV" == host-a ]]
            [[ "$ELBENCHO_RUN_TEST_DIRS_CSV" == /tmp/target ]]
            [[ "$ELBENCHO_RUN_SCRATCH_OUTPUT_DIR" == "$output_dir" ]]
            [[ "$ELBENCHO_RUN_DURABLE_OUTPUT_DIR" == "$output_dir" ]]
            [[ "$ELBENCHO_FILE_LAYOUT" == shared-directory ]]
            [[ "$ELBENCHO_FILES_PER_NODE" == 8 ]]
            [[ "$ELBENCHO_FILE_SIZE" == 64G ]]
            echo 'LIVE STDOUT'
            echo 'LIVE STDERR' >&2
            return 17
        }}
        SLURM_JOB_ID=12345
        set +e
        coordinator_run_one_execution \
            "$executions_dir" 0001 host-a "$output_dir" \
            > "$tmp/coordinator.log" 2>&1
        rc=$?
        set -e
        [[ "$rc" -eq 17 ]]
        [[ "$(cat "$executions_dir/0001.exitcode")" == 17 ]]
        [[ "$(cat "$executions_dir/0001.status")" == FAILED ]]
        grep -q 'LIVE STDOUT' "$tmp/coordinator.log"
        grep -q 'LIVE STDERR' "$tmp/coordinator.log"
        grep -q 'LIVE STDOUT' "$executions_dir/0001.log"
        grep -q 'LIVE STDERR' "$executions_dir/0001.log"
        ! grep -q stale "$executions_dir/0001.log"
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cell_context_drives_hooks_and_preserves_benchmark_failure(self) -> None:
        script = f"""
        set -e
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        io_size=1M
        thread_count=8
        io_depth=4
        dio_or_bio=dio
        use_random=0
        force_single=1
        health_hook() {{ printf 'health:%s\n' "$1" >> "$tmp/hooks"; }}
        publish_hook() {{
            printf 'publish:%s:%s:%s\n' "$1" "$2" "$3" >> "$tmp/hooks"
            [[ "$2" != "$3" && -f "$2/artifact" ]]
            mkdir -p "$3"
            cp -f "$2/artifact" "$3/artifact"
            return "${{PUBLISH_RC:-0}}"
        }}
        run_elbencho_io_sweep_iteration() {{
            printf 'bound:%s:%s:%s:%s:%s:%s:%s\n' \
                "$output_dir" "$io_size" "$thread_count" "$io_depth" \
                "$dio_or_bio" "$use_random" "$force_single" >> "$tmp/hooks"
            mkdir -p "$output_dir"
            printf 'scratch-only\n' > "$output_dir/artifact"
            _elbencho_run_service_health_hook write
            return "${{BENCHMARK_RC:-0}}"
        }}
        elbencho_set_cell_run_context 0007 2 host-a,host-b \
            /mnt/a,/mnt/b "$tmp/scratch" "$tmp/durable" \
            health_hook publish_hook
        [[ "$ELBENCHO_RUN_CONTEXT_READY" == 1 ]]
        [[ "$ELBENCHO_RUN_EXECUTION_ID" == 0007 ]]
        [[ "$ELBENCHO_RUN_TEST_DIRS_CSV" == /mnt/a,/mnt/b ]]
        [[ "$ELBENCHO_RUN_SCRATCH_OUTPUT_DIR" == "$tmp/scratch" ]]
        [[ "$ELBENCHO_RUN_DURABLE_OUTPUT_DIR" == "$tmp/durable" ]]
        io_size=corrupted
        thread_count=999
        io_depth=999
        dio_or_bio=corrupted
        use_random=999
        force_single=999

        BENCHMARK_RC=17
        PUBLISH_RC=23
        set +e
        run_elbencho_cell
        first_rc=$?
        set -e
        [[ "$first_rc" -eq 17 ]]
        grep -q "^publish:17:$tmp/scratch:$tmp/durable$" "$tmp/hooks"
        grep -q "^bound:$tmp/scratch:1M:8:4:dio:0:1$" "$tmp/hooks"
        grep -q '^scratch-only$' "$tmp/durable/artifact"

        BENCHMARK_RC=0
        set +e
        run_elbencho_cell
        second_rc=$?
        set -e
        [[ "$second_rc" -eq 23 ]]
        [[ $(grep -c '^health:write$' "$tmp/hooks") -eq 2 ]]
        grep -q "^publish:0:$tmp/scratch:$tmp/durable$" "$tmp/hooks"
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cell_context_rejects_incomplete_inputs_and_ambient_fallback(
        self,
    ) -> None:
        script = f"""
        set -e
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        output_dir="$tmp/ambient-output"
        test_dirs_csv="$tmp/ambient-data"
        SLURM_JOB_NUM_NODES=2
        nodelist_expanded_comma_separated=host-a,host-b
        SSH_NODELIST=host-a,host-b
        local_context_probe() {{
            local node_count remote_output_dir hosts_csv
            _elbencho_resolve_run_context
        }}
        ! local_context_probe 2> "$tmp/no-context"
        grep -q 'was not initialized' "$tmp/no-context"

        ELBENCHO_RUN_CONTEXT_READY=1
        ELBENCHO_RUN_EXECUTION_ID=0007
        ELBENCHO_RUN_NODE_COUNT=2
        ELBENCHO_RUN_HOSTS_CSV=host-a,host-b
        ELBENCHO_RUN_TEST_DIRS_CSV=/mnt/a,/mnt/b
        run_elbencho_io_sweep_iteration() {{ touch "$tmp/forged-ran"; }}
        ! local_context_probe 2> "$tmp/forged-context"
        grep -q 'context paths must not be empty' "$tmp/forged-context"
        ! run_elbencho_cell 2> "$tmp/forged-cell"
        grep -q 'context paths must not be empty' "$tmp/forged-cell"
        [[ ! -e "$tmp/forged-ran" ]]
        unset ELBENCHO_RUN_CONTEXT_READY

        io_size=1M
        thread_count=8
        io_depth=4
        dio_or_bio=dio
        use_random=0
        unset force_single
        ! elbencho_set_cell_run_context 0007 2 host-a,host-b \
            /mnt/a,/mnt/b "$tmp/scratch" "$tmp/durable" \
            _elbencho_noop_cell_hook _elbencho_noop_cell_hook \
            2> "$tmp/missing-coordinate"
        grep -q 'lacks saved coordinate: force_single' "$tmp/missing-coordinate"

        force_single=1
        ! elbencho_set_cell_run_context 0007 2 host-a \
            /mnt/a,/mnt/b "$tmp/scratch" "$tmp/durable" \
            _elbencho_noop_cell_hook _elbencho_noop_cell_hook \
            2> "$tmp/missing-endpoint"
        grep -q 'requires 2 worker endpoints' "$tmp/missing-endpoint"
        ! elbencho_set_cell_run_context 0007 3 host-a,,host-c \
            /mnt/a,/mnt/b "$tmp/scratch" "$tmp/durable" \
            _elbencho_noop_cell_hook _elbencho_noop_cell_hook \
            2> "$tmp/empty-endpoint"
        grep -q 'contains an empty endpoint' "$tmp/empty-endpoint"
        ! elbencho_set_cell_run_context 0007 2 'host-a, host-b' \
            /mnt/a,/mnt/b "$tmp/scratch" "$tmp/durable" \
            _elbencho_noop_cell_hook _elbencho_noop_cell_hook \
            2> "$tmp/whitespace-endpoint"
        grep -q 'contains whitespace' "$tmp/whitespace-endpoint"
        ! elbencho_set_cell_run_context 0007 2 host-a,host-b \
            /mnt/a,/mnt/b "$tmp/scratch" "$tmp/durable" \
            unavailable_hook _elbencho_noop_cell_hook \
            2> "$tmp/missing-hook"
        grep -q 'hook is unavailable' "$tmp/missing-hook"
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_slurm_health_check_fast_path_does_not_sleep(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        mkdir -p "$tmp/bin"
        cat > "$tmp/bin/srun" <<'EOF'
        #!{_BASH}
        echo '[OK] host-a'
        echo '[OK] host-b'
        EOF
        chmod +x "$tmp/bin/srun"
        sleep() {{
            touch "$tmp/slept"
            command sleep "$@"
        }}
        PATH="$tmp/bin:$PATH"
        SLURM_JOB_NUM_NODES=2
        check_elbencho_services_srun 1611 3
        [[ ! -e "$tmp/slept" ]]
        echo 'NO_SLEEP'
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("NO_SLEEP", result.stdout)
        self.assertIn("elbencho services ready on port 1611", result.stdout)
        self.assertIn("(2/2 nodes)", result.stdout)

    def test_execution_ids_sort_numerically_after_four_digits(self) -> None:
        script = f"""
        set -e
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        mkdir -p "$tmp/executions"
        touch "$tmp/executions/9999.sh"
        touch "$tmp/executions/10000.sh"
        touch "$tmp/executions/0002.sh"
        touch "$tmp/executions/not-an-id.sh"
        list_elbencho_execution_ids "$tmp/executions" > "$tmp/ids"
        diff -u <(printf '0002\\n9999\\n10000\\n') "$tmp/ids"
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compact_summary_reads_lowercase_restored_sweep_flags(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        DS=20260804Z160341
        nodes_spec=4
        dio_or_bio=dio
        rand_option=0
        ELBENCHO_SCALE_IO_SIZES=(r64K)
        ELBENCHO_SCALE_THREAD_LIST=(8)
        ELBENCHO_IODEPTH_LIST=(2)
        ELBENCHO_SCALE_READ_WRITE_DURATION=60
        ELBENCHO_FILE_SIZE_MULTIPLIER=1024
        ELBENCHO_SINGLE_BIG_FILE=0
        sweep_write_only=1
        sweep_write_no_read=1
        sweep_read_from=/mnt/existing-tree
        tmp_out=$(mktemp)
        trap 'rm -f "$tmp_out"' EXIT
        print_elbencho_sweep_compact_summary "$DS" "$nodes_spec" > "$tmp_out"
        grep -q 'fs_mul=N/A single_big=0 wro=1 wnr=1 rf=/mnt/existing-tree' "$tmp_out"
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_validate_srun_includes_gpu_request(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        printf '#!/bin/sh\\nexit 0\\n' > "$tmp/srun"
        chmod +x "$tmp/srun"
        PATH="$tmp:$PATH"
        account=account-a
        partition=gpu-only
        run_time=00:05:00
        reservation=
        SLURM_GPUS_PER_NODE_OPT=--gpus-per-node=4
        SLURM_EXTRA_ARGS=(--qos=normal)
        declare -a srun_cmd
        build_srun_validate_scale_cmd srun_cmd
        [[ " ${{srun_cmd[*]}} " == *' --gpus-per-node=4 '* ]]
        [[ " ${{srun_cmd[*]}} " == *' --qos=normal '* ]]
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compact_summary_prefers_lowercase_flags_over_stale_exports(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        DS=20260804Z160341
        nodes_spec=4
        dio_or_bio=dio
        rand_option=0
        ELBENCHO_SCALE_IO_SIZES=(r64K)
        ELBENCHO_SCALE_THREAD_LIST=(8)
        ELBENCHO_IODEPTH_LIST=(2)
        ELBENCHO_SCALE_READ_WRITE_DURATION=60
        ELBENCHO_FILE_SIZE_MULTIPLIER=1024
        ELBENCHO_SINGLE_BIG_FILE=0
        export ELBENCHO_SWEEP_WRITE_ONLY=0
        export ELBENCHO_SWEEP_WRITE_NO_READ=1
        export ELBENCHO_SWEEP_READ_FROM=/stale/tree
        sweep_write_only=1
        sweep_write_no_read=0
        sweep_read_from=
        tmp_out=$(mktemp)
        trap 'rm -f "$tmp_out"' EXIT
        print_elbencho_sweep_compact_summary "$DS" "$nodes_spec" > "$tmp_out"
        grep -q 'fs_mul=1024 single_big=0 wro=1 wnr=0' "$tmp_out"
        ! grep -q 'rf=' "$tmp_out"
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_slurm_health_check_has_one_overall_deadline(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        mkdir -p "$tmp/bin"
        cat > "$tmp/bin/srun" <<'EOF'
        #!{_BASH}
        exec sleep 10
        EOF
        chmod +x "$tmp/bin/srun"
        PATH="$tmp/bin:$PATH"
        SLURM_JOB_NUM_NODES=1
        started=$SECONDS
        set +e
        check_elbencho_services_srun 1611 1 >"$tmp/output" 2>&1
        rc=$?
        set -e
        elapsed=$((SECONDS - started))
        if [[ "$rc" -ne 1 || "$elapsed" -lt 1 || "$elapsed" -gt 4 ]] ||
                ! grep -q 'reached its 1s deadline' "$tmp/output"; then
            printf 'health-check rc=%s elapsed=%ss output:\\n' "$rc" "$elapsed" >&2
            cat "$tmp/output" >&2
            exit 1
        fi
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_phase_restart_persists_new_service_owner_pid(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        SLURM_JOB_ID=12345
        SLURM_JOB_NUM_NODES=2
        SRUN_ELBENCHO_PID=111
        SRUN_ELBENCHO_PID_FILE="$tmp/service.pid"
        _atomic_write_sentinel "$SRUN_ELBENCHO_PID_FILE" "$SRUN_ELBENCHO_PID"
        check_calls=0
        check_elbencho_services_srun() {{
            check_calls=$((check_calls + 1))
            [[ "$check_calls" -ge 2 ]]
        }}
        stop_elbencho_services_srun() {{
            [[ "$1" == 111 ]]
        }}
        start_elbencho_services_srun() {{
            printf '222\n'
        }}
        maybe_restart_elbencho_services_slurm write
        [[ "$SRUN_ELBENCHO_PID" == 222 ]]
        [[ "$(cat "$SRUN_ELBENCHO_PID_FILE")" == 222 ]]
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_coordinator_retries_initial_services_without_per_execution_probe(
        self,
    ) -> None:
        coordinator = _SLURM_COORDINATOR.read_text(encoding="utf-8")
        self.assertEqual(coordinator.count("check_elbencho_services_srun"), 1)
        self.assertIn("for attempt in 1 2; do", coordinator)
        self.assertIn('start_elbencho_services_srun "" "$OUTPUT_DIR"', coordinator)
        self.assertIn('"${SRUN_ELBENCHO_LOG}.attempt-${attempt}"', coordinator)
        self.assertIn("failed after one restart", coordinator)
        self.assertNotIn(
            'maybe_restart_elbencho_services_slurm "execution-${ID}"',
            coordinator,
        )
        common_functions = _ELBENCHO_FUNCTIONS.read_text(encoding="utf-8")
        adapter_functions = _ENV_FUNCTIONS.read_text(encoding="utf-8")
        self.assertIn('_elbencho_run_service_health_hook "write"', common_functions)
        self.assertNotIn("maybe_restart_elbencho_services_slurm()", common_functions)
        self.assertIn("maybe_restart_elbencho_services_slurm()", adapter_functions)

    def test_live_slurm_coordinator_blocks_redispatch(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        mkdir -p "$tmp/executions"
        printf '777\n' > "$tmp/executions/.coordinator.jobid"
        squeue() {{ printf '777\n'; }}
        set +e
        _elbencho_verify_no_active_slurm_coordinator "$tmp/executions"
        active_rc=$?
        set -e
        [[ "$active_rc" -eq 1 ]]
        _elbencho_verify_no_active_slurm_coordinator "$tmp/executions" 777
        squeue() {{ return 0; }}
        _elbencho_verify_no_active_slurm_coordinator "$tmp/executions"
        squeue() {{ return 1; }}
        sacct() {{ printf '777|FAILED\n'; }}
        _elbencho_verify_no_active_slurm_coordinator "$tmp/executions"
        sacct() {{ return 1; }}
        set +e
        _elbencho_verify_no_active_slurm_coordinator "$tmp/executions"
        unverifiable_rc=$?
        set -e
        [[ "$unverifiable_rc" -eq 1 ]]
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("still active", result.stderr)
        self.assertIn("terminal sacct state", result.stderr)

    def test_dispatch_lock_handoff_is_exclusive(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        mkdir -p "$tmp/executions"

        first_token=
        _elbencho_acquire_dispatch_lock \
            "$tmp/executions" slurm-submit first_token
        [[ -n "$first_token" ]]

        second_token=
        set +e
        _elbencho_acquire_dispatch_lock "$tmp/executions" ssh second_token
        second_rc=$?
        set -e
        [[ "$second_rc" -eq 1 ]]
        [[ -z "$second_token" ]]

        _elbencho_adopt_slurm_dispatch_lock \
            "$tmp/executions" "$first_token" 4242
        _elbencho_adopt_slurm_dispatch_lock \
            "$tmp/executions" "$first_token" 4242
        [[ "$(cat "$tmp/executions/.dispatch.lock/mode")" == slurm ]]
        [[ "$(cat "$tmp/executions/.dispatch.lock/jobid")" == 4242 ]]

        set +e
        _elbencho_adopt_slurm_dispatch_lock \
            "$tmp/executions" wrong-token 4242
        wrong_token_rc=$?
        set -e
        [[ "$wrong_token_rc" -eq 1 ]]

        squeue() {{ printf '4242\n'; }}
        set +e
        _elbencho_acquire_dispatch_lock "$tmp/executions" ssh second_token
        active_job_rc=$?
        set -e
        [[ "$active_job_rc" -eq 1 ]]

        _elbencho_release_dispatch_lock "$tmp/executions" "$first_token"
        [[ ! -e "$tmp/executions/.dispatch.lock" ]]
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("refusing concurrent resume", result.stderr)

    def test_slurm_dispatch_preserves_lock_after_handoff(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        mkdir -p "$tmp/executions"
        printf 'export nodes=4\n' > "$tmp/executions/0001.sh"
        printf 'PENDING\n' > "$tmp/executions/0001.status"
        SCALE_TEST_BASE="{_REPO_ROOT}"

        build_sbatch_cmd() {{
            local -n cmd_ref="$1"
            cmd_ref=(sbatch)
        }}
        run_sbatch_job() {{
            [[ "$1" == 4 ]]
            JOBID=5151
            log_files+=("$tmp/coordinator-5151.log")
            return 0
        }}
        tail_until_complete() {{
            [[ "$1" == 5151 ]]
            [[ "${{#log_files[@]}}" -eq 1 ]]
            return 0
        }}

        dispatch_slurm_executions "$tmp"
        [[ -d "$tmp/executions/.dispatch.lock" ]]
        [[ "$(cat "$tmp/executions/.dispatch.lock/mode")" == slurm ]]
        [[ "$(cat "$tmp/executions/.dispatch.lock/jobid")" == 5151 ]]
        [[ "$(cat "$tmp/executions/.coordinator.jobid")" == 5151 ]]
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("circular name reference", result.stderr)

    def test_slurm_adoption_failure_cancels_armed_job_and_waits(self) -> None:
        """A submitted job is cancellation-owned before lock adoption starts."""
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        mkdir -p "$tmp/executions"
        printf 'export nodes=1\n' > "$tmp/executions/0001.sh"
        printf 'PENDING\n' > "$tmp/executions/0001.status"
        SCALE_TEST_BASE="{_REPO_ROOT}"

        build_sbatch_cmd() {{ local -n ref="$1"; ref=(sbatch); }}
        run_sbatch_job() {{ JOBID=6161; log_files+=("$tmp/coordinator.log"); }}
        _elbencho_adopt_slurm_dispatch_lock() {{
            [[ "${{ELBENCHO_SLURM_CANCEL_ARMED:-0}}" -eq 1 ]]
            [[ "${{ELBENCHO_SLURM_ACTIVE_JOB_ID:-}}" == 6161 ]]
            return 1
        }}
        tail_until_complete() {{ touch "$tmp/unexpected-monitor"; }}
        scancel() {{ [[ "$1" == 6161 ]]; touch "$tmp/cancelled"; }}
        squeue() {{ [[ "$*" == *6161* ]]; return 0; }}
        sleep() {{ :; }}

        set +e
        dispatch_slurm_executions "$tmp"
        rc=$?
        set -e
        [[ "$rc" -eq 1 ]]
        [[ -e "$tmp/cancelled" ]]
        [[ ! -e "$tmp/unexpected-monitor" ]]
        [[ ! -e "$tmp/executions/.dispatch.lock" ]]
        [[ "$(trap -p EXIT)" == *'rm -rf'* ]]
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_slurm_adoption_failure_retains_lock_when_cancel_is_unverified(
        self,
    ) -> None:
        """An allocation of unknown state keeps exclusive dispatch ownership."""
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        mkdir -p "$tmp/executions"
        printf 'export nodes=1\n' > "$tmp/executions/0001.sh"
        printf 'PENDING\n' > "$tmp/executions/0001.status"
        SCALE_TEST_BASE="{_REPO_ROOT}"

        build_sbatch_cmd() {{ local -n ref="$1"; ref=(sbatch); }}
        run_sbatch_job() {{ JOBID=6262; log_files+=("$tmp/coordinator.log"); }}
        _elbencho_adopt_slurm_dispatch_lock() {{ return 1; }}
        scancel() {{ [[ "$1" == 6262 ]]; }}
        squeue() {{ return 1; }}

        set +e
        dispatch_slurm_executions "$tmp"
        rc=$?
        set -e
        [[ "$rc" -eq 1 ]]
        [[ -d "$tmp/executions/.dispatch.lock" ]]
        [[ "$(cat "$tmp/executions/.dispatch.lock/token")" != "" ]]
        printf '999999\n' > "$tmp/executions/.dispatch.lock/pid"
        retry_token=""
        if _elbencho_acquire_dispatch_lock \
                "$tmp/executions" slurm-submit retry_token; then
            exit 1
        fi
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("retaining dispatch lock", result.stderr)

    def test_slurm_monitor_timeout_cancels_exact_handed_off_job(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        mkdir -p "$tmp/executions"
        printf 'export nodes=1\n' > "$tmp/executions/0001.sh"
        printf 'PENDING\n' > "$tmp/executions/0001.status"
        SCALE_TEST_BASE="{_REPO_ROOT}"

        build_sbatch_cmd() {{ local -n ref="$1"; ref=(sbatch); }}
        run_sbatch_job() {{ JOBID=6262; log_files+=("$tmp/coordinator.log"); }}
        tail_until_complete() {{ return 124; }}
        scancel() {{ [[ "$1" == 6262 ]]; touch "$tmp/cancelled"; }}
        squeue() {{ [[ "$*" == *6262* ]]; return 0; }}
        sleep() {{ :; }}

        set +e
        dispatch_slurm_executions "$tmp"
        rc=$?
        set -e
        [[ "$rc" -eq 124 ]]
        [[ -e "$tmp/cancelled" ]]
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_active_lock_blocks_slurm_and_ssh_before_status_reset(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        mkdir -p "$tmp/executions"
        owner_token=
        _elbencho_acquire_dispatch_lock "$tmp/executions" ssh owner_token

        _elbencho_verify_no_active_slurm_coordinator() {{ return 0; }}
        _elbencho_sweep_running_to_pending() {{ touch "$tmp/status-reset"; }}

        set +e
        dispatch_slurm_executions "$tmp"
        slurm_rc=$?
        dispatch_ssh_executions "$tmp"
        ssh_rc=$?
        set -e

        [[ "$slurm_rc" -eq 1 ]]
        [[ "$ssh_rc" -eq 1 ]]
        [[ ! -e "$tmp/status-reset" ]]
        _elbencho_release_dispatch_lock "$tmp/executions" "$owner_token"
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stale_ssh_lock_reclaims_but_slurm_handoff_fails_closed(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        mkdir -p "$tmp/executions"
        stale_token=
        _elbencho_acquire_dispatch_lock "$tmp/executions" ssh stale_token
        _atomic_write_sentinel "$tmp/executions/.dispatch.lock/pid" 99999999
        replacement_token=
        _elbencho_acquire_dispatch_lock \
            "$tmp/executions" ssh replacement_token
        [[ -n "$replacement_token" && "$replacement_token" != "$stale_token" ]]
        _elbencho_release_dispatch_lock "$tmp/executions" "$replacement_token"

        slurm_token=
        _elbencho_acquire_dispatch_lock \
            "$tmp/executions" slurm-submit slurm_token
        _atomic_write_sentinel "$tmp/executions/.dispatch.lock/pid" 99999999
        blocked_token=
        set +e
        _elbencho_acquire_dispatch_lock \
            "$tmp/executions" ssh blocked_token
        ambiguous_rc=$?
        set -e
        [[ "$ambiguous_rc" -eq 1 ]]
        [[ -z "$blocked_token" ]]
        _elbencho_release_dispatch_lock "$tmp/executions" "$slurm_token"
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tail_until_complete_uses_main_job_failure_not_completed_step(
        self,
    ) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        tail() {{ return 0; }}
        sleep() {{ return 0; }}
        kill() {{ return 0; }}
        wait() {{ return 0; }}
        sacct() {{
            if [[ "$*" == *JobIDRaw,State* ]]; then
                printf '42|FAILED\n42.extern|COMPLETED\n'
            else
                printf '42|FAILED|1:0\n42.extern|COMPLETED|0:0\n'
            fi
        }}
        set +e
        tail_until_complete 42 /dev/null
        rc=$?
        set -e
        [[ "$rc" -eq 1 ]]
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ssh_startup_stops_hosts_before_pruning_them(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        SSH_ALL_HOSTS=(host-a host-b host-c)
        SSH_HOST_COUNT=3
        calls=0
        stopped_hosts=
        _ssh_fan_out_to_each_host() {{
            calls=$((calls + 1))
            if [[ "$calls" -eq 1 ]]; then
                local -n selected="$3"
                selected=(host-a host-c)
                return 1
            fi
            stopped_hosts="$1"
            return 0
        }}
        _ssh_start_services_on_all_hosts
        [[ "${{SSH_ALL_HOSTS[*]}}" == "host-a host-c" ]]
        [[ "$SSH_HOST_COUNT" -eq 2 ]]
        [[ "$stopped_hosts" == host-b ]]
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ssh_startup_cleans_up_when_all_hosts_fail(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        SSH_ALL_HOSTS=(host-a host-b)
        SSH_HOST_COUNT=2
        calls=0
        stopped_hosts=
        _ssh_fan_out_to_each_host() {{
            calls=$((calls + 1))
            if [[ "$calls" -eq 1 ]]; then
                local -n selected="$3"
                selected=()
                return 1
            fi
            stopped_hosts="$1"
            return 0
        }}
        set +e
        _ssh_start_services_on_all_hosts
        rc=$?
        set -e
        [[ "$rc" -eq 1 ]]
        [[ "$SSH_HOST_COUNT" -eq 0 ]]
        [[ "$stopped_hosts" == host-a,host-b ]]
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ssh_execution_log_is_teed_live(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        output_dir="$tmp/elbencho-20260804Z160341"
        executions_dir="$output_dir/executions"
        mkdir -p "$executions_dir"
        printf 'export nodes=1\n' > "$executions_dir/0001.sh"
        printf 'PENDING\n' > "$executions_dir/0001.status"
        printf 'stale\n' > "$executions_dir/0001.log"
        choose_N_ssh_hosts() {{
            SSH_NODELIST=host-a
            return 0
        }}
        _compute_test_dirs_csv_for_execution() {{ printf '/tmp/target'; }}
        _cleanup_local_elbencho_result_artifacts_for_execution() {{ return 0; }}
        _ssh_build_execution_scriptlet() {{ printf 'remote script'; }}
        run_ssh_single() {{
            [[ "$3" == /dev/stdout ]]
            printf '0\n' > "$2"
            echo 'LIVE SSH STDOUT'
            echo 'LIVE SSH STDERR'
            return 0
        }}
        _ssh_retrieve_remote_output() {{ return 0; }}
        _ssh_dispatch_one_execution "$output_dir" 0001 > "$tmp/terminal.log" 2>&1
        grep -q 'LIVE SSH STDOUT' "$tmp/terminal.log"
        grep -q 'LIVE SSH STDERR' "$tmp/terminal.log"
        grep -q 'LIVE SSH STDOUT' "$executions_dir/0001.log"
        grep -q 'LIVE SSH STDERR' "$executions_dir/0001.log"
        ! grep -q stale "$executions_dir/0001.log"
        [[ "$(cat "$executions_dir/0001.status")" == SUCCESS ]]
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ssh_term_after_remote_success_finalizes_shared_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = f"""
            set -e
            source "{_ENV_FUNCTIONS}"
            source "{_ELBENCHO_FUNCTIONS}"
            output_dir={str(Path(tmp) / "elbencho-20260820Z120000")!r}
            executions_dir="$output_dir/executions"
            mkdir -p "$executions_dir"
            cat > "$executions_dir/0001.sh" <<'EOF'
            export nodes=1
            export ELBENCHO_FILE_LAYOUT=shared-directory
            export ELBENCHO_FILES_PER_NODE=1
            export ELBENCHO_FILE_SIZE=1M
            export ELBENCHO_SWEEP_READ_FROM=
            EOF
            printf 'PENDING\\n' > "$executions_dir/0001.status"
            choose_N_ssh_hosts() {{ SSH_NODELIST=host-a; }}
            _compute_test_dirs_csv_for_execution() {{ printf '/tmp/target'; }}
            _cleanup_local_elbencho_result_artifacts_for_execution() {{ return 0; }}
            _ssh_build_execution_scriptlet() {{ printf 'remote script'; }}
            run_ssh_single() {{
                printf '0\\n' > "$2"
                printf 'REMOTE SUCCESS\\n'
            }}
            _ssh_finalize_remote_generated_shared_failure() {{
                printf cleanup > {str(Path(tmp) / "cleanup")!r}
            }}
            _ssh_retrieve_remote_output() {{
                kill -TERM "$BASHPID"
            }}
            _ssh_dispatch_one_execution "$output_dir" 0001
            """
            result = _run_bash(textwrap.dedent(script))
            status = (
                Path(tmp) / "elbencho-20260820Z120000" / "executions" / "0001.status"
            ).read_text(encoding="utf-8")
            cleanup_exists = (Path(tmp) / "cleanup").is_file()
        self.assertEqual(result.returncode, 143, result.stderr)
        self.assertEqual(status, "FAILED\n")
        self.assertTrue(cleanup_exists)

    def test_ssh_active_shared_exit_and_int_preserve_status(self) -> None:
        cases = (("exit 17", 17), ('kill -INT "$BASHPID"', 130))
        for trigger, expected_rc in cases:
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory() as tmp:
                script = f"""
                set -e
                source "{_ENV_FUNCTIONS}"
                source "{_ELBENCHO_FUNCTIONS}"
                nnnn={str(Path(tmp) / "0001.sh")!r}
                status_file={str(Path(tmp) / "0001.status")!r}
                cat > "$nnnn" <<'EOF'
                export nodes=1
                export ELBENCHO_FILE_LAYOUT=shared-directory
                export ELBENCHO_FILES_PER_NODE=1
                export ELBENCHO_SWEEP_READ_FROM=
                EOF
                printf 'RUNNING\\n' > "$status_file"
                _ssh_finalize_remote_generated_shared_failure() {{
                    printf cleanup > {str(Path(tmp) / "cleanup")!r}
                }}
                _ssh_arm_active_generated_shared_finalization \
                    host-a elbencho-test 0001 "$nnnn" "$status_file"
                {trigger}
                """
                result = _run_bash(textwrap.dedent(script))
                status = (Path(tmp) / "0001.status").read_text(encoding="utf-8")
                cleanup_exists = (Path(tmp) / "cleanup").is_file()
            self.assertEqual(result.returncode, expected_rc, result.stderr)
            self.assertEqual(status, "FAILED\n")
            self.assertTrue(cleanup_exists)

    def test_ssh_shared_success_disarms_without_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = f"""
            set -e
            source "{_ENV_FUNCTIONS}"
            source "{_ELBENCHO_FUNCTIONS}"
            output_dir={str(Path(tmp) / "elbencho-20260820Z120001")!r}
            executions_dir="$output_dir/executions"
            mkdir -p "$executions_dir"
            cat > "$executions_dir/0001.sh" <<'EOF'
            export nodes=1
            export ELBENCHO_FILE_LAYOUT=shared-directory
            export ELBENCHO_FILES_PER_NODE=1
            export ELBENCHO_FILE_SIZE=1M
            export ELBENCHO_SWEEP_READ_FROM=
            EOF
            printf 'PENDING\\n' > "$executions_dir/0001.status"
            choose_N_ssh_hosts() {{ SSH_NODELIST=host-a; }}
            _compute_test_dirs_csv_for_execution() {{ printf '/tmp/target'; }}
            _cleanup_local_elbencho_result_artifacts_for_execution() {{ return 0; }}
            _ssh_build_execution_scriptlet() {{ printf 'remote script'; }}
            run_ssh_single() {{ printf '0\\n' > "$2"; }}
            _ssh_retrieve_remote_output() {{ return 0; }}
            _ssh_finalize_remote_generated_shared_failure() {{
                printf cleanup > {str(Path(tmp) / "cleanup")!r}
            }}
            _ssh_dispatch_one_execution "$output_dir" 0001
            [[ "$(cat "$executions_dir/0001.status")" == SUCCESS ]]
            [[ ! -e {str(Path(tmp) / "cleanup")!r} ]]
            """
            result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_coredump_wrapper_resolves_relative_elbencho_before_cd(self) -> None:
        script = f"""
        set -e
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        cd "$tmp"
        cat > elbencho <<'EOF'
        #!{_BASH}
        printf 'ran-cwd=%s\\n' "$PWD" > "$ELBENCHO_RAN_FILE"
        EOF
        chmod +x elbencho
        export ELBENCHO=./elbencho
        export OUTPUT_DIR="$tmp/output"
        export ELBENCHO_RUN_EXECUTION_ID=0001
        export ELBENCHO_RAN_FILE="$tmp/ran"
        _elbencho_run_master_with_coredump --placeholder
        grep -q "ran-cwd=$tmp/output/executions" "$tmp/ran"
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ssh_scriptlet_absolutizes_relative_remote_output_dir(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        nnnn="$tmp/0001.sh"
        printf '%s\\n' \
            'export nodes=2' \
            'export ELBENCHO_FILE_LAYOUT=shared-directory' \
            'export ELBENCHO_FILES_PER_NODE=8' \
            'export ELBENCHO_FILE_SIZE=64G' > "$nnnn"
        cat > "$tmp/_elbencho_functions.sh" <<'EOS'
        elbencho_set_cell_run_context() {{
            export ELBENCHO_RUN_EXECUTION_ID="$1"
            export ELBENCHO_RUN_NODE_COUNT="$2"
            export ELBENCHO_RUN_HOSTS_CSV="$3"
            export ELBENCHO_RUN_TEST_DIRS_CSV="$4"
            export ELBENCHO_RUN_SCRATCH_OUTPUT_DIR="$5"
        }}
        run_elbencho_io_sweep_iteration() {{
            printf 'remote=%s\\n' "$ELBENCHO_RUN_SCRATCH_OUTPUT_DIR"
            printf 'output=%s\\n' "$output_dir"
            printf 'layout=%s\\n' "$ELBENCHO_FILE_LAYOUT"
            printf 'files=%s\\n' "$ELBENCHO_FILES_PER_NODE"
            printf 'size=%s\\n' "$ELBENCHO_FILE_SIZE"
        }}
        run_elbencho_cell() {{ run_elbencho_io_sweep_iteration; }}
        EOS
        scriptlet=$(_ssh_build_execution_scriptlet \
            "$nnnn" "host-a,host-b" 2 "/data/a,/data/b" "elbencho-DS" "0001")
        (cd "$tmp" && bash -s <<< "$scriptlet")
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = dict(line.split("=", 1) for line in result.stdout.splitlines())
        self.assertTrue(lines["remote"].startswith("/"), result.stdout)
        self.assertTrue(lines["remote"].endswith("/elbencho-DS"), result.stdout)
        self.assertEqual(lines["output"], lines["remote"])
        self.assertEqual(lines["layout"], "shared-directory")
        self.assertEqual(lines["files"], "8")
        self.assertEqual(lines["size"], "64G")

    def test_read_from_passes_nonempty_treefile_to_elbencho(self) -> None:
        script = """
        set -e
        source "__ENV_FUNCTIONS__"
        source "__ELBENCHO_FUNCTIONS__"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        capture="$tmp/args"
        run_an_elbencho() {
            printf '%s\n' "$@" > "$capture"
            local tree_arg
            tree_arg=$(awk '$0 == "--treefile" { getline; print; exit }' "$capture")
            printf 'f 1 sample\n' > "$tree_arg"
        }
        output_dir="$tmp/elbencho-20260731Z010203"
        mkdir -p "$tmp/read-from"
        test_dirs_csv="$tmp/read-from"
        ELBENCHO_SWEEP_READ_FROM="$tmp/read-from"
        ELBENCHO_SCALE_READ_WRITE_DURATION=1s
        ELBENCHO_FILE_SIZE_MULTIPLIER=1
        ELBENCHO_READ_AFTER_WRITE_PAUSE=0
        ELBENCHO_LIVE_CSV_EXTENDED=0
        ELBENCHO_SINGLE_BIG_FILE=0
        io_size=r64K
        thread_count=8
        io_depth=2
        force_single=0
        single_option=0
        use_random=0
        dio_or_bio=dio
        elbencho_set_cell_run_context 0001 1 host-a "$tmp/read-from" \
            "$output_dir" "$output_dir" _elbencho_noop_cell_hook \
            _elbencho_noop_cell_hook
        run_elbencho_io_sweep_iteration
        tree_arg=$(awk '$0 == "--treefile" { getline; print; exit }' "$capture")
        [[ -n "$tree_arg" ]]
        grep -Fx -- '--treescan' "$capture"
        ! grep -Eq -- '^--(dirs|files)(=|$)' "$capture"
        [[ -f "$tmp/read-from/.storage-scale-test-elbencho-treefile.txt" ]]
        grep -q $'treefile_source\tcache_miss_scan' \
            "$output_dir/executions/0001.workload.tsv"
        grep -q $'dataset_files_total\t1' \
            "$output_dir/executions/0001.workload.tsv"
        grep -q $'dataset_bytes_total\t1' \
            "$output_dir/executions/0001.workload.tsv"
        """
        script = script.replace("__ENV_FUNCTIONS__", str(_ENV_FUNCTIONS))
        script = script.replace("__ELBENCHO_FUNCTIONS__", str(_ELBENCHO_FUNCTIONS))
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_read_from_reuses_cached_treefile_without_scan(self) -> None:
        script = """
        set -e
        source "__ENV_FUNCTIONS__"
        source "__ELBENCHO_FUNCTIONS__"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        capture="$tmp/args"
        mkdir -p "$tmp/read-from"
        printf 'f 1 sample\\n' > "$tmp/read-from/.storage-scale-test-elbencho-treefile.txt"
        run_an_elbencho() { printf '%s\\n' "$@" > "$capture"; }
        output_dir="$tmp/elbencho-20260731Z010203"
        test_dirs_csv="$tmp/read-from"
        ELBENCHO_SWEEP_READ_FROM="$tmp/read-from"
        ELBENCHO_SCALE_READ_WRITE_DURATION=1s
        ELBENCHO_FILE_SIZE_MULTIPLIER=1
        ELBENCHO_READ_AFTER_WRITE_PAUSE=0
        ELBENCHO_LIVE_CSV_EXTENDED=0
        ELBENCHO_SINGLE_BIG_FILE=0
        io_size=r64K
        thread_count=8
        io_depth=2
        force_single=0
        single_option=0
        use_random=0
        dio_or_bio=dio
        elbencho_set_cell_run_context 0001 1 host-a "$tmp/read-from" \
            "$output_dir" "$output_dir" _elbencho_noop_cell_hook \
            _elbencho_noop_cell_hook
        run_elbencho_io_sweep_iteration
        grep -Fx -- '--treefile' "$capture"
        grep -Fx -- "$tmp/read-from/.storage-scale-test-elbencho-treefile.txt" "$capture"
        ! grep -Fx -- '--treescan' "$capture"
        ! grep -Eq -- '^--(dirs|files)(=|$)' "$capture"
        grep -q $'\\thit\\tstarted\\t' "$output_dir/executions/0001.treefile-cache"
        grep -q $'\\thit\\treused\\t' "$output_dir/executions/0001.treefile-cache"
        grep -q $'treefile_source\\tcache_hit' \
            "$output_dir/executions/0001.workload.tsv"
        grep -q $'dataset_files_total\\t1' \
            "$output_dir/executions/0001.workload.tsv"
        grep -q $'dataset_bytes_total\\t1' \
            "$output_dir/executions/0001.workload.tsv"
        """
        script = script.replace("__ENV_FUNCTIONS__", str(_ENV_FUNCTIONS))
        script = script.replace("__ELBENCHO_FUNCTIONS__", str(_ELBENCHO_FUNCTIONS))
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_read_from_mount_root_falls_back_to_treescan(self) -> None:
        script = """
        set -e
        source "__ENV_FUNCTIONS__"
        source "__ELBENCHO_FUNCTIONS__"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        capture="$tmp/args"
        mkdir -p "$tmp/read-from"
        stat() { [[ "$3" == "$tmp/read-from" ]] && printf '2\\n' || printf '1\\n'; }
        run_an_elbencho() {
            printf '%s\\n' "$@" > "$capture"
            local tree_arg
            tree_arg=$(awk '$0 == "--treefile" { getline; print; exit }' "$capture")
            printf 'f 1 sample\\n' > "$tree_arg"
        }
        output_dir="$tmp/elbencho-20260731Z010203"
        test_dirs_csv="$tmp/read-from"
        ELBENCHO_SWEEP_READ_FROM="$tmp/read-from"
        ELBENCHO_SCALE_READ_WRITE_DURATION=1s
        ELBENCHO_FILE_SIZE_MULTIPLIER=1
        ELBENCHO_READ_AFTER_WRITE_PAUSE=0
        ELBENCHO_LIVE_CSV_EXTENDED=0
        ELBENCHO_SINGLE_BIG_FILE=0
        io_size=r64K
        thread_count=8
        io_depth=2
        force_single=0
        single_option=0
        use_random=0
        dio_or_bio=dio
        elbencho_set_cell_run_context 0001 1 host-a "$tmp/read-from" \
            "$output_dir" "$output_dir" _elbencho_noop_cell_hook \
            _elbencho_noop_cell_hook
        run_elbencho_io_sweep_iteration
        grep -Fx -- '--treescan' "$capture"
        grep -Fx -- '--treefile' "$capture"
        ! grep -Eq -- '^--(dirs|files)(=|$)' "$capture"
        ! [[ -e "$tmp/read-from/.storage-scale-test-elbencho-treefile.txt" ]]
        grep -q $'\\tunavailable\\tfallback\\t' "$output_dir/executions/0001.treefile-cache"
        grep -q $'treefile_source\\tcache_unavailable_scan' \
            "$output_dir/executions/0001.workload.tsv"
        grep -q $'dataset_files_total\\t1' \
            "$output_dir/executions/0001.workload.tsv"
        grep -q $'dataset_bytes_total\\t1' \
            "$output_dir/executions/0001.workload.tsv"
        """
        script = script.replace("__ENV_FUNCTIONS__", str(_ENV_FUNCTIONS))
        script = script.replace("__ELBENCHO_FUNCTIONS__", str(_ELBENCHO_FUNCTIONS))
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_treefile_cache_failed_publish_removes_staging(self) -> None:
        script = """
        set -e
        source "__ELBENCHO_FUNCTIONS__"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        output_dir="$tmp/output"
        ELBENCHO_RUN_EXECUTION_ID=0001
        mkdir -p "$tmp/read-from"
        _elbencho_treefile_cache_prepare "$tmp/read-from"
        printf 'f 1 sample\\n' > "$ELBENCHO_TREEFILE_CACHE_STAGING"
        staging="$ELBENCHO_TREEFILE_CACHE_STAGING"
        mv() { return 1; }
        _elbencho_treefile_cache_finish 0
        [[ ! -e "$staging" ]]
        grep -q $'\\tmiss\\tnot-created\\t' "$output_dir/executions/0001.treefile-cache"
        """
        script = script.replace("__ELBENCHO_FUNCTIONS__", str(_ELBENCHO_FUNCTIONS))
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_treefile_cache_snapshot_update_keeps_attempts(self) -> None:
        script = """
        set -e
        source "__ENV_FUNCTIONS__"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        output_dir="$tmp/output"
        mkdir -p "$output_dir/executions"
        declare -A TEST_DIRS=(["$tmp/fs"]=1)
        FS_MAX_AGG_THROUGHPUT=0
        FS_MAX_NODE_THROUGHPUT_GBPS=0
        FS_MAX_NODE_IOPS=0
        ELBENCHO_SCALE_THREAD_LIST=(1)
        ELBENCHO_SCALE_IO_SIZES=(1M)
        ELBENCHO_IODEPTH_LIST=(1)
        ELBENCHO_FILE_SIZE_MULTIPLIER=1
        ELBENCHO_SCALE_READ_WRITE_DURATION=1
        ELBENCHO_READ_AFTER_WRITE_PAUSE=0
        ELBENCHO_LIVE_CSV_EXTENDED=0
        ELBENCHO_LIVEINT=1000
        ELBENCHO_SINGLE_BIG_FILE=0
        ELBENCHO_SINGLE_BIG_FILE_BASENAME=elbencho-bigfile
        ELBENCHO_SINGLE_BIG_FILE_SIZE=
        ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0
        EXECUTION_SUBSTRATE=slurm
        write_elbencho_env_used "$output_dir/env_used.yaml" dio 0 0 0 0 "$tmp/read-from" 1
        printf '2026-08-17T00:00:00Z\\thit\\treused\\t%s\\n' \\
            "$tmp/read-from/.storage-scale-test-elbencho-treefile.txt" \\
            > "$output_dir/executions/0001.treefile-cache"
        update_elbencho_env_used_treefile_cache_usage "$output_dir"
        grep -q 'state_at_start: "hit"' "$output_dir/env_used.yaml"
        source "$output_dir/env_used.sh"
        [[ "${#ELBENCHO_TREEFILE_CACHE_ATTEMPTS[@]}" -eq 1 ]]
        """
        script = script.replace("__ENV_FUNCTIONS__", str(_ENV_FUNCTIONS))
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_active_ssh_pool_keeps_hosts_that_pass_probe(self) -> None:
        script = """
        set -e
        source "__ENV_FUNCTIONS__"
        SSH_ALL_HOSTS=(host-a host-b host-c)
        SSH_HOST_COUNT=3
        _ssh_fan_out_to_each_host() {
            local -n selected="$3"
            selected=(host-a host-c)
            return 1
        }
        _ssh_update_active_hosts "test probe" ':'
        [[ "$SSH_HOST_COUNT" -eq 2 ]]
        [[ "${SSH_ALL_HOSTS[*]}" == "host-a host-c" ]]
        """
        script = script.replace("__ENV_FUNCTIONS__", str(_ENV_FUNCTIONS))
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("excluding 1 SSH host", result.stderr)

    def test_ssh_retrieval_extracts_only_current_execution_artifacts(self) -> None:
        script = """
        set -e
        source "__ENV_FUNCTIONS__"
        source "__ELBENCHO_FUNCTIONS__"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        local_parent="$tmp/local"
        remote_parent="$tmp/remote"
        remote_basename=elbencho-20260731Z010203
        output_dir="$local_parent/$remote_basename"
        mkdir -p "$output_dir/executions" "$remote_parent/$remote_basename/executions"
        nnnn="$output_dir/executions/0002.sh"
        printf '%s\n' \
            'export nodes=2' \
            'export io_size=r64K' \
            'export thread_count=8' \
            'export io_depth=2' > "$nnnn"
        artifact_output=$(_elbencho_result_artifacts_for_execution "$output_dir" "$nnnn")
        while IFS= read -r artifact; do
            remote_artifact="$remote_parent/$remote_basename/${artifact#"$output_dir/"}"
            mkdir -p "$(dirname "$remote_artifact")"
            touch "$remote_artifact"
        done <<< "$artifact_output"
        touch "$remote_parent/$remote_basename/old.csv"
        touch "$remote_parent/$remote_basename/executions/0001.core"
        touch "$remote_parent/$remote_basename/executions/0002.core"
        run_ssh_single() {
            local rc_file="$2"
            local stdout_file="$3"
            local scriptlet="$5"
            shift 5
            local rc=0
            (cd "$remote_parent" && bash -s -- "$@" <<< "$scriptlet") > "$stdout_file" || rc=$?
            printf '%s\n' "$rc" > "$rc_file"
        }
        _ssh_retrieve_remote_output host-a "$output_dir" "$remote_basename" 0002 "$nnnn"
        while IFS= read -r artifact; do
            [[ -f "$artifact" ]]
        done <<< "$artifact_output"
        [[ -f "$output_dir/executions/0002.core" ]]
        [[ ! -e "$output_dir/old.csv" ]]
        [[ ! -e "$output_dir/executions/0001.core" ]]
        """
        script = script.replace("__ENV_FUNCTIONS__", str(_ENV_FUNCTIONS))
        script = script.replace("__ELBENCHO_FUNCTIONS__", str(_ELBENCHO_FUNCTIONS))
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ssh_retrieval_requires_shared_completion_evidence(self) -> None:
        script = """
        set -e
        source "__ENV_FUNCTIONS__"
        source "__ELBENCHO_FUNCTIONS__"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        local_parent="$tmp/local"
        remote_parent="$tmp/remote"
        remote_basename=elbencho-20260820Z010203
        output_dir="$local_parent/$remote_basename"
        mkdir -p "$output_dir/executions" \
            "$remote_parent/$remote_basename/executions"
        nnnn="$output_dir/executions/0001.sh"
        printf '%s\n' \
            'export nodes=1' \
            'export io_size=4K' \
            'export thread_count=1' \
            'export io_depth=1' \
            'export ELBENCHO_FILE_LAYOUT=shared-directory' \
            'export ELBENCHO_FILES_PER_NODE=1' \
            'export ELBENCHO_FILE_SIZE=4K' \
            'export ELBENCHO_SINGLE_BIG_FILE=0' \
            'export ELBENCHO_SWEEP_READ_FROM=' \
            'export ELBENCHO_SWEEP_WRITE_ONLY=1' \
            'export ELBENCHO_SWEEP_WRITE_NO_READ=0' >"$nnnn"
        required=$(_elbencho_required_result_artifacts_for_execution \
            "$output_dir" 0001 "$nnnn")
        grep -Fx -- "$output_dir/executions/0001.workload.tsv" <<<"$required"
        grep -Fx -- "$output_dir/executions/0001.write.json" <<<"$required"
        ! grep -Fq -- '.read.json' <<<"$required"
        ! grep -Fq -- '.delete.json' <<<"$required"

        artifact=$(_elbencho_result_artifacts_for_execution \
            "$output_dir" "$nnnn" | head -n 1)
        remote_artifact="$remote_parent/$remote_basename/${artifact#"$output_dir/"}"
        mkdir -p "$(dirname "$remote_artifact")"
        touch "$remote_artifact"
        run_ssh_single() {
            local rc_file="$2" stdout_file="$3" scriptlet="$5"
            shift 5
            local rc=0
            (cd "$remote_parent" && bash -s -- "$@" <<<"$scriptlet") \
                >"$stdout_file" || rc=$?
            printf '%s\n' "$rc" >"$rc_file"
        }
        ! _ssh_retrieve_remote_output \
            host-a "$output_dir" "$remote_basename" 0001 "$nnnn"
        [[ -f "$artifact" ]]
        """
        script = script.replace("__ENV_FUNCTIONS__", str(_ENV_FUNCTIONS))
        script = script.replace("__ELBENCHO_FUNCTIONS__", str(_ELBENCHO_FUNCTIONS))
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("required execution artifact was not retrieved", result.stderr)

    def test_required_artifacts_include_distributed_delete_evidence(self) -> None:
        script = """
        set -e
        source "__ENV_FUNCTIONS__"
        source "__ELBENCHO_FUNCTIONS__"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        output_dir="$tmp/elbencho-20260820Z010204"
        mkdir -p "$output_dir/executions"
        for mode in default no-read; do
            nnnn="$output_dir/executions/$mode.sh"
            printf '%s\n' \
                'export ELBENCHO_FILE_LAYOUT=shared-directory' \
                'export ELBENCHO_FILES_PER_NODE=1' \
                'export ELBENCHO_SWEEP_READ_FROM=' \
                'export ELBENCHO_SWEEP_WRITE_ONLY=0' \
                "export ELBENCHO_SWEEP_WRITE_NO_READ=$([[ \"$mode\" == no-read ]] && echo 1 || echo 0)" \
                >"$nnnn"
            required=$(_elbencho_required_result_artifacts_for_execution \
                "$output_dir" 0002 "$nnnn")
            grep -Fx -- "$output_dir/executions/0002.delete.json" <<<"$required"
            if [[ "$mode" == default ]]; then
                grep -Fx -- "$output_dir/executions/0002.read.json" <<<"$required"
            else
                ! grep -Fq -- '.read.json' <<<"$required"
            fi
        done
        """
        script = script.replace("__ENV_FUNCTIONS__", str(_ENV_FUNCTIONS))
        script = script.replace("__ELBENCHO_FUNCTIONS__", str(_ELBENCHO_FUNCTIONS))
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ssh_delete_evidence_failure_retries_with_clean_artifacts(self) -> None:
        script = """
        set -e
        source "__ENV_FUNCTIONS__"
        source "__ELBENCHO_FUNCTIONS__"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        output_dir="$tmp/elbencho-20260820Z010205"
        executions="$output_dir/executions"
        mkdir -p "$executions"
        cat >"$executions/0001.sh" <<'EOF'
        export nodes=1
        export io_size=4K
        export thread_count=1
        export io_depth=1
        export ELBENCHO_FILE_LAYOUT=shared-directory
        export ELBENCHO_FILES_PER_NODE=1
        export ELBENCHO_FILE_SIZE=4K
        export ELBENCHO_SINGLE_BIG_FILE=0
        export ELBENCHO_SWEEP_READ_FROM=
        export ELBENCHO_SWEEP_WRITE_ONLY=0
        export ELBENCHO_SWEEP_WRITE_NO_READ=1
        EOF
        printf 'PENDING\n' >"$executions/0001.status"
        choose_N_ssh_hosts() { SSH_NODELIST=host-a; }
        _compute_test_dirs_csv_for_execution() { printf '/tmp/target'; }
        _ssh_build_execution_scriptlet() { printf 'remote script'; }
        printf '0\n' >"$tmp/runner-attempts"
        run_ssh_single() {
            local runner_attempts
            runner_attempts=$(<"$tmp/runner-attempts")
            runner_attempts=$((runner_attempts + 1))
            printf '%s\n' "$runner_attempts" >"$tmp/runner-attempts"
            if [[ "$runner_attempts" -eq 2 ]]; then
                [[ ! -e "$executions/0001.write.json" ]]
                [[ ! -e "$executions/0001.delete.json" ]]
                [[ ! -e "$executions/0001.workload.tsv" ]]
            fi
            printf '0\n' >"$2"
        }
        printf '0\n' >"$tmp/retrieve-attempts"
        _ssh_retrieve_remote_output() {
            local retrieve_attempts
            retrieve_attempts=$(<"$tmp/retrieve-attempts")
            retrieve_attempts=$((retrieve_attempts + 1))
            printf '%s\n' "$retrieve_attempts" >"$tmp/retrieve-attempts"
            if [[ "$retrieve_attempts" -le 2 ]]; then
                printf stale >"$executions/0001.write.json"
                printf stale >"$executions/0001.workload.tsv"
                return 1
            fi
            printf fresh >"$executions/0001.write.json"
            printf fresh >"$executions/0001.delete.json"
            printf fresh >"$executions/0001.workload.tsv"
        }
        printf '0\n' >"$tmp/cleanup-calls"
        _ssh_finalize_remote_generated_shared_failure() {
            local cleanup_calls
            cleanup_calls=$(<"$tmp/cleanup-calls")
            printf '%s\n' $((cleanup_calls + 1)) >"$tmp/cleanup-calls"
        }
        set +e
        _ssh_dispatch_one_execution "$output_dir" 0001
        first_rc=$?
        set -e
        [[ "$first_rc" -ne 0 ]]
        [[ "$(<"$executions/0001.status")" == FAILED ]]
        [[ "$(<"$tmp/cleanup-calls")" -ge 1 ]]
        _ssh_dispatch_one_execution "$output_dir" 0001
        [[ "$(<"$executions/0001.status")" == SUCCESS ]]
        [[ "$(<"$executions/0001.delete.json")" == fresh ]]
        [[ "$(<"$tmp/runner-attempts")" -eq 2 ]]
        """
        script = script.replace("__ENV_FUNCTIONS__", str(_ENV_FUNCTIONS))
        script = script.replace("__ELBENCHO_FUNCTIONS__", str(_ELBENCHO_FUNCTIONS))
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_gather_ssh_waits_for_every_child(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        (sleep 0.05) & p1=$!
        (sleep 0.25) & p2=$!
        (sleep 0.10) & p3=$!
        touch "$tmp/$p1.host-a" "$tmp/$p2.host-b" "$tmp/$p3.host-c"
        gather_N_ssh "$tmp" 1 "$p1" "$p2" "$p3" > "$tmp/progress"
        [[ -f "$tmp/host-a.done" ]]
        [[ -f "$tmp/host-b.done" ]]
        [[ -f "$tmp/host-c.done" ]]
        [[ $(wc -l < "$tmp/progress") -eq 3 ]]
        grep -q '^3/3$' "$tmp/progress"
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_single_big_file_write_no_read_skips_read_and_cleans_up(self) -> None:
        script = f"""
        set -e
        source "{_ELBENCHO_FUNCTIONS}"
        tmp=$(mktemp -d)
        trap 'rm -rf "$tmp"' EXIT
        output_dir="$tmp/elbencho-20260804Z160341"
        test_dirs_csv="$tmp/data"
        io_size=1M
        thread_count=1
        io_depth=1
        dio_or_bio=dio
        ELBENCHO_SWEEP_WRITE_ONLY=0
        ELBENCHO_SWEEP_WRITE_NO_READ=1
        ELBENCHO_SWEEP_READ_FROM=
        ELBENCHO_SINGLE_BIG_FILE_SIZE=1M
        ELBENCHO_SINGLE_BIG_FILE_BASENAME=elbencho-bigfile
        ELBENCHO_SCALE_READ_WRITE_DURATION=1
        ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0
        _elbencho_resolve_run_context() {{
            node_count=1
            remote_output_dir="$tmp/output"
            hosts_csv=
            mkdir -p "$remote_output_dir"
        }}
        _elbencho_cleanup_stale_single_big_file_sweep() {{ mkdir -p "$1"; }}
        _elbencho_io_cleanup_result_artifacts() {{ return 0; }}
        _elbencho_io_build_common_args() {{ common_args=(); }}
        _elbencho_io_append_rotated_hosts_for_read() {{ return 0; }}
        run_an_elbencho() {{ printf '%s\n' "$*" >> "$tmp/calls"; }}
        _elbencho_run_service_health_hook() {{ return 0; }}
        _elbencho_cleanup_single_big_file_sweep() {{
            printf 'cleanup:%s:%s\n' "$1" "$2" >> "$tmp/cleanup"
        }}
        _elbencho_maybe_pause_before_read() {{ touch "$tmp/paused"; }}
        run_elbencho_io_sweep_iteration_single_big_file
        [[ $(wc -l < "$tmp/calls") -eq 1 ]]
        grep -q -- '--write' "$tmp/calls"
        ! grep -q -- '--read' "$tmp/calls"
        grep -q '^cleanup:' "$tmp/cleanup"
        [[ ! -e "$tmp/paused" ]]
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_resume_validates_saved_environment_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp) / "repo"
            script_dir = fake_root / "storage-tests" / "fs"
            lib_dir = fake_root / "lib"
            resume_dir = Path(tmp) / "results" / "elbencho-20260731Z010203"
            script_dir.mkdir(parents=True)
            lib_dir.mkdir(parents=True)
            (resume_dir / "executions").mkdir(parents=True)
            sweep_script = script_dir / "nv-elbencho-sweep.sh"
            shutil.copy2(
                _REPO_ROOT / "storage-tests" / "fs" / "nv-elbencho-sweep.sh",
                sweep_script,
            )
            (lib_dir / "_elbencho_functions.sh").write_text("", encoding="utf-8")
            env_text = """
            SCALE_TEST_BASE=__FAKE_ROOT__
            EXECUTION_SUBSTRATE=slurm
            SLURM_ENABLED=1
            SSH_ENABLED=
            ELBENCHO_FILE_LAYOUT=shared-directory
            ELBENCHO_FILES_PER_NODE=8
            ELBENCHO_FILE_SIZE=64G
            declare -gA TEST_DIRS=()
            FS_ENABLED=
            validate_integer_array() { [[ "${SAVED_CONFIG:-0}" == 1 ]]; }
            validate_elbencho_io_sizes() { [[ "${SAVED_CONFIG:-0}" == 1 ]]; }
            validate_elbencho_duration() { [[ "${SAVED_CONFIG:-0}" == 1 ]]; }
            validate_elbencho_live_csv() { [[ "${SAVED_CONFIG:-0}" == 1 ]]; }
            validate_elbencho_file_workload_env() { [[ "${SAVED_CONFIG:-0}" == 1 ]]; }
            validate_elbencho_sweep_workload_mode() {
                [[ "${SAVED_CONFIG:-0}" == 1 ]]
                [[ "$ELBENCHO_FILE_LAYOUT" == worker-directories ]]
                [[ -z "$ELBENCHO_FILES_PER_NODE" ]]
                [[ -z "$ELBENCHO_FILE_SIZE" ]]
            }
            dispatch_slurm_executions() { printf 'RESTORED_DISPATCH:%s\n' "$1"; }
            """.replace("__FAKE_ROOT__", str(fake_root))
            (fake_root / "env.sh").write_text(
                textwrap.dedent(env_text), encoding="utf-8"
            )
            saved_text = """
            unset TEST_DIRS
            declare -gA TEST_DIRS=([__SAVED_DIR__]=1)
            export SAVED_CONFIG=1
            export dio_or_bio=dio
            export rand_option=0
            export single_option=0
            export sweep_write_only=0
            export sweep_write_no_read=0
            export sweep_read_from=
            export nodes_spec=1
            """.replace("__SAVED_DIR__", str(Path(tmp) / "saved-fs"))
            (resume_dir / "env_used.sh").write_text(
                textwrap.dedent(saved_text), encoding="utf-8"
            )
            env = os.environ.copy()
            env["SHELL"] = _BASH
            env["EXECUTION_SUBSTRATE"] = "slurm"
            result = subprocess.run(
                [str(sweep_script), "--resume", str(resume_dir)],
                check=False,
                cwd=fake_root,
                env=env,
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESTORED_DISPATCH:", result.stdout)

    def test_resume_restores_new_shared_workload_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp) / "repo"
            script_dir = fake_root / "storage-tests" / "fs"
            lib_dir = fake_root / "lib"
            resume_dir = Path(tmp) / "results" / "elbencho-20260820Z010203"
            script_dir.mkdir(parents=True)
            lib_dir.mkdir(parents=True)
            (resume_dir / "executions").mkdir(parents=True)
            sweep_script = script_dir / "nv-elbencho-sweep.sh"
            shutil.copy2(
                _REPO_ROOT / "storage-tests" / "fs" / "nv-elbencho-sweep.sh",
                sweep_script,
            )
            (lib_dir / "_elbencho_functions.sh").write_text("", encoding="utf-8")
            env_text = """
            SCALE_TEST_BASE=__FAKE_ROOT__
            EXECUTION_SUBSTRATE=slurm
            SLURM_ENABLED=1
            SSH_ENABLED=
            ELBENCHO_FILE_LAYOUT=worker-directories
            ELBENCHO_FILES_PER_NODE=
            ELBENCHO_FILE_SIZE=
            declare -gA TEST_DIRS=()
            FS_ENABLED=
            validate_integer_array() { [[ "${SAVED_CONFIG:-0}" == 1 ]]; }
            validate_elbencho_io_sizes() { [[ "${SAVED_CONFIG:-0}" == 1 ]]; }
            validate_elbencho_duration() { [[ "${SAVED_CONFIG:-0}" == 1 ]]; }
            validate_elbencho_live_csv() { [[ "${SAVED_CONFIG:-0}" == 1 ]]; }
            validate_elbencho_file_workload_env() {
                [[ "$ELBENCHO_FILE_LAYOUT" == shared-directory ]]
                [[ "$ELBENCHO_FILES_PER_NODE" == 8 ]]
                [[ "$ELBENCHO_FILE_SIZE" == 64G ]]
            }
            validate_elbencho_sweep_workload_mode() {
                [[ "$ELBENCHO_FILE_LAYOUT" == shared-directory ]]
                [[ "$ELBENCHO_FILES_PER_NODE" == 8 ]]
                [[ "$ELBENCHO_FILE_SIZE" == 64G ]]
            }
            dispatch_slurm_executions() { printf 'SHARED_DISPATCH:%s\n' "$1"; }
            """.replace("__FAKE_ROOT__", str(fake_root))
            (fake_root / "env.sh").write_text(
                textwrap.dedent(env_text), encoding="utf-8"
            )
            saved_text = """
            unset TEST_DIRS
            declare -gA TEST_DIRS=([__SAVED_DIR__]=1)
            export SAVED_CONFIG=1
            export ELBENCHO_FILE_LAYOUT=shared-directory
            export ELBENCHO_FILES_PER_NODE=8
            export ELBENCHO_FILE_SIZE=64G
            export dio_or_bio=dio
            export rand_option=0
            export single_option=1
            export sweep_write_only=0
            export sweep_write_no_read=0
            export sweep_read_from=
            export nodes_spec=1
            """.replace("__SAVED_DIR__", str(Path(tmp) / "saved-fs"))
            (resume_dir / "env_used.sh").write_text(
                textwrap.dedent(saved_text), encoding="utf-8"
            )
            env = os.environ.copy()
            env["SHELL"] = _BASH
            env["EXECUTION_SUBSTRATE"] = "slurm"
            result = subprocess.run(
                [str(sweep_script), "--resume", str(resume_dir)],
                check=False,
                cwd=fake_root,
                env=env,
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SHARED_DISPATCH:", result.stdout)

    def test_slurm_warning_uses_largest_requested_node_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_root = Path(tmp) / "repo"
            script_dir = fake_root / "storage-tests" / "fs"
            lib_dir = fake_root / "lib"
            script_dir.mkdir(parents=True)
            lib_dir.mkdir(parents=True)
            sweep_script = script_dir / "nv-elbencho-sweep.sh"
            shutil.copy2(
                _REPO_ROOT / "storage-tests" / "fs" / "nv-elbencho-sweep.sh",
                sweep_script,
            )
            (lib_dir / "_elbencho_functions.sh").write_text("", encoding="utf-8")
            env_text = """
            SCALE_TEST_BASE=__FAKE_ROOT__
            RESULTS_DIR=__RESULTS_DIR__
            EXECUTION_SUBSTRATE=slurm
            SLURM_ENABLED=1
            SSH_ENABLED=
            declare -gA TEST_DIRS=([/tmp/fs]=1)
            validate_integer_array() { return 0; }
            validate_elbencho_io_sizes() { return 0; }
            validate_elbencho_duration() { return 0; }
            validate_elbencho_live_csv() { return 0; }
            validate_elbencho_file_workload_env() { return 0; }
            validate_elbencho_single_big_file_env() { return 0; }
            validate_elbencho_sweep_workload_mode() { return 0; }
            validate_elbencho_sweep_single_test_dirs_key() { return 0; }
            validate_elbencho_sweep_one_generated_target_dir() { return 0; }
            parse_range_specification() { tr ',' '\\n' <<< "$1"; }
            write_elbencho_env_used() { return 0; }
            print_slurm_node_warnings() { printf 'WARNING_NODES=%s\\n' "$1"; }
            reify_all_elbencho_executions() { return 0; }
            dispatch_slurm_executions() { return 0; }
            """.replace("__FAKE_ROOT__", str(fake_root)).replace(
                "__RESULTS_DIR__", str(Path(tmp) / "results")
            )
            (fake_root / "env.sh").write_text(
                textwrap.dedent(env_text), encoding="utf-8"
            )
            env = os.environ.copy()
            env["SHELL"] = _BASH
            result = subprocess.run(
                [str(sweep_script), "--nodes", "1024,820,616,412,208"],
                check=False,
                cwd=fake_root,
                env=env,
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WARNING_NODES=1024", result.stdout)


if __name__ == "__main__":
    unittest.main()
