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

"""Shell-level tests for exact generated shared-directory execution."""

import subprocess
import textwrap
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FUNCTIONS = _REPO_ROOT / "lib" / "_elbencho_functions.sh"


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", textwrap.dedent(script)],
        cwd=_REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


class TestElbenchoSharedDirectoryShell(unittest.TestCase):
    """The bounded workload remains exact and fails closed."""

    def test_decimal_helpers_do_not_use_native_integer_precision(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            [[ "$(_elbencho_decimal_add 999999999999999999999 1)" == \
                1000000000000000000000 ]]
            [[ "$(_elbencho_decimal_multiply 999999999999999999999 9)" == \
                8999999999999999999991 ]]
            [[ "$(_elbencho_decimal_subtract 1000000000000000000000 1)" == \
                999999999999999999999 ]]
            ! _elbencho_decimal_subtract 1 2
            [[ "$(_elbencho_decimal_mod 1000000000000000000001 3)" == 2 ]]
            [[ "$(_elbencho_shell_signed_max)" == 9223372036854775807 ]]
            ELBENCHO_FILE_SIZE_MULTIPLIER=1024
            [[ "$(_elbencho_resolve_generated_file_size 1M)" == 1G ]]
            [[ "$(_elbencho_shared_files_per_worker 1 1)" == 1 ]]
            [[ "$(_elbencho_shared_files_per_worker 8 8)" == 1 ]]
            [[ "$(_elbencho_shared_files_per_worker 8 1)" == 8 ]]
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_artifact_path_helpers_assign_every_caller_variable(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            io_size=4K node_count=2 thread_count=4 io_depth=1
            resfile= csvfile= livecsvfile= treefile= workload=
            _elbencho_staged_set_artifact_paths /results/out DS 0001 \
                resfile csvfile livecsvfile treefile workload
            [[ "$resfile" == /results/out/elbencho-4K-c_002-s_004-d_001_DS.out ]]
            [[ "$csvfile" == /results/out/elbencho-4K-c_002-s_004-d_001_DS.csv ]]
            [[ "$livecsvfile" == /results/out/elbencho-4K-c_002-s_004-d_001_DS.live.csv ]]
            [[ "$treefile" == /results/out/elbencho-treescan-4K-c_002-s_004-d_001_DS.txt ]]
            [[ "$workload" == /results/out/executions/0001.workload.tsv ]]
            write_json= read_json= delete_json=
            _elbencho_shared_set_artifact_paths /results/out DS 0001 \
                resfile csvfile livecsvfile treefile write_json read_json \
                delete_json workload
            [[ "$write_json" == /results/out/executions/0001.write.json ]]
            [[ "$read_json" == /results/out/executions/0001.read.json ]]
            [[ "$delete_json" == /results/out/executions/0001.delete.json ]]
            [[ "$workload" == /results/out/executions/0001.workload.tsv ]]
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_workload_rewrite_uses_valid_cleanup_defaults(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            workload="$tmp/0001.workload.tsv"
            ELBENCHO_SCALE_READ_WRITE_DURATION=1
            _elbencho_shared_workload_begin "$workload" 1 1 4096 0 0
            while IFS=$'\t' read -r key value; do
                case "$key" in
                    write_elapsed_time_ms|read_elapsed_time_ms|delete_*|\
                    write_delete_elapsed_time_ms|lifecycle_elapsed_time_ms)
                        continue
                        ;;
                esac
                printf '%s\t%s\n' "$key" "$value"
            done <"$workload" >"$workload.legacy"
            mv "$workload.legacy" "$workload"
            [[ $(wc -l <"$workload") -eq 26 ]]
            _elbencho_workload_update_failure_cleanup_state \
                "$workload" completed
            [[ $(wc -l <"$workload") -eq 34 ]]
            grep -q $'delete_completion_state\tnot_applicable' "$workload"
            grep -q $'delete_expected_files\tnull' "$workload"
            grep -q $'failure_cleanup_state\tcompleted' "$workload"
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_json_parser_accepts_sync_and_rejects_duplicate_counter(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            printf '%s\n' \
                '{{"phase_type":"SYNC"}}' \
                '{{"phase_type":"WRITE","last_done":{{"elapsed_time_ms":"7","entries":"2","bytes":"8192"}}}}' \
                '{{"phase_type":"SYNC"}}' >"$tmp/good"
            [[ "$(_elbencho_parse_phase_json "$tmp/good" WRITE)" == $'2\t8192\t7' ]]
            tr -d '\n' <"$tmp/good" >"$tmp/adjacent"
            [[ "$(_elbencho_parse_phase_json "$tmp/adjacent" WRITE)" == $'2\t8192\t7' ]]
            printf '%s\n' \
                '{{"phase_type":"WRITE","last_done":{{"elapsed_time_ms":"7","entries":"2","entries":"2","bytes":"8192"}}}}' \
                >"$tmp/bad"
            ! _elbencho_parse_phase_json "$tmp/bad" WRITE
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_json_parser_rejects_ambiguous_or_malformed_records(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            printf '%s\n' \
                '{{"phase_type":"READ","last_done":{{"elapsed_time_ms":"1","entries":"1","bytes":"4"}}}}' \
                '{{"phase_type":"READ","last_done":{{"elapsed_time_ms":"1","entries":"1","bytes":"4"}}}}' \
                >"$tmp/two"
            ! _elbencho_parse_phase_json "$tmp/two" READ
            printf '%s\n' \
                '{{"phase_type":"WRITE","last_done":{{"elapsed_time_ms":"1","entries":"-1","bytes":"4"}}}}' \
                >"$tmp/negative"
            ! _elbencho_parse_phase_json "$tmp/negative" WRITE
            printf '%s\n' \
                '{{"phase_type":"DELETEFILES","last_done":{{"elapsed_time_ms":"1","entries":"1","bytes":"4"}}}}' \
                >"$tmp/wrong"
            ! _elbencho_parse_phase_json "$tmp/wrong" WRITE
            printf '%s\n' '{{"phase_type":"WRITE"}}' >"$tmp/missing"
            ! _elbencho_parse_phase_json "$tmp/missing" WRITE
            printf '%s\n' '{{broken' >"$tmp/malformed"
            ! _elbencho_parse_phase_json "$tmp/malformed" WRITE
            : >"$tmp/blank"
            ! _elbencho_parse_phase_json "$tmp/blank" WRITE
            ! _elbencho_parse_phase_json "$tmp/absent" WRITE
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rmfiles_json_requires_entries_and_elapsed_without_bytes(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            printf '%s\n' \
                '{{"phase_type":"SYNC"}}' \
                '{{"phase_type":"RMFILES","last_done":{{"elapsed_time_ms":"37","entries":"12000"}}}}' \
                >"$tmp/good"
            [[ "$(_elbencho_parse_phase_json "$tmp/good" RMFILES)" == \
                $'12000\tnull\t37' ]]
            printf '%s\n' \
                '{{"phase_type":"RMFILES","last_done":{{"entries":"12000"}}}}' \
                >"$tmp/no-time"
            ! _elbencho_parse_phase_json "$tmp/no-time" RMFILES
            printf '%s\n' \
                '{{"phase_type":"RMFILES","last_done":{{"elapsed_time_ms":"37","entries":"12000","bytes":"0"}}}}' \
                >"$tmp/bytes"
            ! _elbencho_parse_phase_json "$tmp/bytes" RMFILES
            printf '%s\n' \
                '{{"phase_type":"RMFILES","last_done":{{"elapsed_time_ms":"037","entries":"12000"}}}}' \
                >"$tmp/noncanonical-time"
            ! _elbencho_parse_phase_json "$tmp/noncanonical-time" RMFILES
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_treefile_aggregates_are_exact_and_malformed_sizes_fail(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            : >"$tmp/empty"
            _elbencho_treefile_aggregate "$tmp/empty" files bytes
            [[ "$files" == 0 && "$bytes" == 0 ]]
            printf '%s\n' \
                'f 999999999999999999999 first' \
                'd 0 ignored directory' \
                'f 2 a filename with spaces' >"$tmp/huge"
            _elbencho_treefile_aggregate "$tmp/huge" files bytes
            [[ "$files" == 2 && "$bytes" == 1000000000000000000001 ]]
            printf '%s\n' 'f -1 invalid' >"$tmp/negative"
            ! _elbencho_treefile_aggregate "$tmp/negative" files bytes
            printf '%s\n' 'f nope invalid' >"$tmp/nonnumeric"
            ! _elbencho_treefile_aggregate "$tmp/nonnumeric" files bytes
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_mode_validation_checks_threads_and_exact_blocks(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            declare -A TEST_DIRS=([/tmp/root]=1)
            ELBENCHO_FILE_LAYOUT=shared-directory
            ELBENCHO_FILES_PER_NODE=8
            ELBENCHO_FILE_SIZE=64K
            ELBENCHO_SINGLE_BIG_FILE=0
            ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0
            ELBENCHO_FILE_SIZE_MULTIPLIER=1024
            ELBENCHO_SCALE_THREAD_LIST=(1 8)
            ELBENCHO_SCALE_IO_SIZES=(4K)
            ELBENCHO_IODEPTH_LIST=(1)
            validate_elbencho_sweep_workload_mode dio 0 ""
            ELBENCHO_FILES_PER_NODE=7
            err=$(validate_elbencho_sweep_workload_mode dio 0 "" 2>&1) && exit 1
            [[ "$err" == *"ELBENCHO_FILES_PER_NODE=7"* ]]
            [[ "$err" == *"ELBENCHO_SCALE_THREAD_LIST value 8"* ]]
            [[ "$err" == *"nearby valid ELBENCHO_FILES_PER_NODE value(s): 8"* ]]
            ELBENCHO_FILES_PER_NODE=8
            ELBENCHO_SCALE_THREAD_LIST=(1)
            ELBENCHO_FILE_SIZE=65M
            ELBENCHO_SCALE_IO_SIZES=(4M)
            ! validate_elbencho_sweep_workload_mode dio 0 ""
            validate_elbencho_sweep_workload_mode bio 0 ""
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_compound_exactness_and_staged_exemptions(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            declare -A TEST_DIRS=([/tmp/root]=1)
            ELBENCHO_SINGLE_BIG_FILE=0
            ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0
            ELBENCHO_IODEPTH_LIST=(1)
            ELBENCHO_SCALE_THREAD_LIST=(1)
            ELBENCHO_FILE_LAYOUT=shared-directory
            ELBENCHO_FILES_PER_NODE=1
            ELBENCHO_FILE_SIZE=
            ELBENCHO_FILE_SIZE_MULTIPLIER=1024
            ELBENCHO_SCALE_IO_SIZES=(1M,3M)
            ! validate_elbencho_sweep_workload_mode dio 0 ""
            ELBENCHO_SCALE_IO_SIZES=(1M,4M)
            validate_elbencho_sweep_workload_mode dio 0 ""
            ELBENCHO_FILE_LAYOUT=worker-directories
            ELBENCHO_FILES_PER_NODE=
            ELBENCHO_FILE_SIZE=
            ELBENCHO_SCALE_IO_SIZES=(1M,3M)
            validate_elbencho_sweep_workload_mode dio 0 ""
            ELBENCHO_FILE_SIZE=65M
            ELBENCHO_SCALE_IO_SIZES=(4M)
            ! validate_elbencho_sweep_workload_mode dio 0 ""
            validate_elbencho_sweep_workload_mode bio 0 ""
            ELBENCHO_FILE_LAYOUT=shared-directory
            ELBENCHO_FILES_PER_NODE=4
            ELBENCHO_SCALE_THREAD_LIST=(3)
            validate_elbencho_sweep_workload_mode dio 0 /tmp/root/staged
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shared_runner_is_single_pass_and_records_completion(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            root="$tmp/data"
            target="$root/target-DS-e0001"
            mkdir -p "$root"
            output_dir="$tmp/out-DS"
            test_dirs_csv="$target"
            ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV="$target"
            ELBENCHO_RUN_GENERATED_TEST_ROOT="$root"
            ELBENCHO_RUN_TEST_DIR_SUFFIX=-DS-e0001
            ELBENCHO_FILE_LAYOUT=shared-directory
            ELBENCHO_FILES_PER_NODE=8
            ELBENCHO_FILE_SIZE=64K
            ELBENCHO_SCALE_READ_WRITE_DURATION=1
            ELBENCHO_LIVE_CSV_EXTENDED=0
            ELBENCHO_READ_AFTER_WRITE_PAUSE=0
            ELBENCHO_SWEEP_WRITE_ONLY=0
            ELBENCHO_SWEEP_WRITE_NO_READ=0
            ELBENCHO_SWEEP_READ_FROM=
            io_size=4K thread_count=4 io_depth=1 dio_or_bio=dio
            use_random=0 force_single=1
            printf '0\n' >"$tmp/tick-index"
            _elbencho_monotonic_milliseconds() {{
                local index
                index=$(<"$tmp/tick-index")
                printf '%s\n' $((index + 1)) >"$tmp/tick-index"
                if [[ "$index" -eq 0 ]]; then printf '1000\n'; else printf '1800\n'; fi
            }}
            run_an_elbencho() {{
                printf 'CALL %s\n' "$*" >>"$tmp/calls"
                local phase= path= arg
                for arg in "$@"; do
                    [[ "$arg" == --write ]] && phase=WRITE
                    [[ "$arg" == --read ]] && phase=READ
                    [[ "$arg" == --delfiles ]] && phase=RMFILES
                    [[ "$arg" == --jsonfile=* ]] && path="${{arg#--jsonfile=}}"
                done
                if [[ "$phase" == RMFILES ]]; then
                    printf '{{"phase_type":"RMFILES","last_done":{{"elapsed_time_ms":"300","entries":"16"}}}}\n' \
                        >"$path"
                elif [[ -n "$phase" ]]; then
                    printf '{{"phase_type":"%s","last_done":{{"elapsed_time_ms":"100","entries":"16","bytes":"1048576"}}}}\n' \
                        "$phase" >"$path"
                fi
            }}
            elbencho_set_cell_run_context 0001 2 a,b "$target" \
                "$output_dir" "$output_dir" _elbencho_noop_cell_hook \
                _elbencho_noop_cell_hook
            run_elbencho_io_sweep_iteration_shared_generated
            [[ ! -e "$target" ]]
            grep -q -- '--dirs=0 --files=2' "$tmp/calls"
            ! grep -Eq -- '--timelimit|--infloop' "$tmp/calls"
            grep -q $'completion_state\tcompleted' \
                "$output_dir/executions/0001.workload.tsv"
            grep -q $'delete_completed_files\t16' \
                "$output_dir/executions/0001.workload.tsv"
            grep -q $'delete_elapsed_time_ms\t300' \
                "$output_dir/executions/0001.workload.tsv"
            grep -q $'write_delete_elapsed_time_ms\t400' \
                "$output_dir/executions/0001.workload.tsv"
            grep -q $'lifecycle_elapsed_time_ms\t800' \
                "$output_dir/executions/0001.workload.tsv"
            grep -q $'failure_cleanup_state\tnot_needed' \
                "$output_dir/executions/0001.workload.tsv"
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_short_byte_completion_fails_cleans_and_skips_read(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            root="$tmp/data"
            target="$root/target-DS-e0002"
            mkdir -p "$root" "$tmp/out-DS/executions"
            output_dir="$tmp/out-DS"
            test_dirs_csv="$target"
            ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV="$target"
            ELBENCHO_RUN_GENERATED_TEST_ROOT="$root"
            ELBENCHO_RUN_TEST_DIR_SUFFIX=-DS-e0002
            ELBENCHO_FILE_LAYOUT=shared-directory
            ELBENCHO_FILES_PER_NODE=2
            ELBENCHO_FILE_SIZE=4K
            ELBENCHO_SCALE_READ_WRITE_DURATION=999
            ELBENCHO_LIVE_CSV_EXTENDED=0
            ELBENCHO_READ_AFTER_WRITE_PAUSE=0
            ELBENCHO_SWEEP_WRITE_ONLY=0
            ELBENCHO_SWEEP_WRITE_NO_READ=0
            ELBENCHO_SWEEP_READ_FROM=
            io_size=4K thread_count=1 io_depth=1 dio_or_bio=dio
            use_random=0 force_single=1
            stale="$output_dir/executions/0002.write.json"
            printf '%s\n' stale duplicate >"$stale"
            compute_target_file_count_per_thread() {{ exit 91; }}
            run_an_elbencho() {{
                printf '%s\n' "$*" >>"$tmp/calls"
                local phase= path= arg
                for arg in "$@"; do
                    [[ "$arg" == --write ]] && phase=WRITE
                    [[ "$arg" == --read ]] && phase=READ
                    [[ "$arg" == --jsonfile=* ]] && path="${{arg#--jsonfile=}}"
                done
                if [[ "$phase" == WRITE ]]; then
                    [[ ! -e "$path" ]]
                    printf '%s\n' \
                        '{{"phase_type":"SYNC"}}' \
                        '{{"phase_type":"WRITE","last_done":{{"elapsed_time_ms":"1","entries":"2","bytes":"4096"}}}}' \
                        >"$path"
                fi
            }}
            elbencho_set_cell_run_context 0002 1 a "$target" \
                "$output_dir" "$output_dir" _elbencho_noop_cell_hook \
                _elbencho_noop_cell_hook
            set +e
            run_elbencho_io_sweep_iteration
            rc=$?
            set -e
            [[ "$rc" -ne 0 ]]
            [[ ! -e "$target" ]]
            grep -q -- '--write' "$tmp/calls"
            ! grep -q -- '--read' "$tmp/calls"
            [[ -f "$output_dir/executions/0002.write.json" ]]
            [[ -f "$output_dir/executions/0002.workload.tsv" ]]
            grep -q $'completion_state\tincomplete' \
                "$output_dir/executions/0002.workload.tsv"
            grep -q $'write_completed_bytes\t4096' \
                "$output_dir/executions/0002.workload.tsv"
            grep -q $'failure_cleanup_state\tcompleted' \
                "$output_dir/executions/0002.workload.tsv"
            ! grep -Eq -- '--timelimit|--infloop' "$tmp/calls"
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rmfiles_nonzero_rc_preserves_rc_and_uses_safe_backstop(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            root="$tmp/data"
            target="$root/target-DS-e0007"
            output_dir="$tmp/out-DS"
            mkdir -p "$root"
            test_dirs_csv="$target"
            ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV="$target"
            ELBENCHO_RUN_GENERATED_TEST_ROOT="$root"
            ELBENCHO_RUN_TEST_DIR_SUFFIX=-DS-e0007
            ELBENCHO_FILE_LAYOUT=shared-directory
            ELBENCHO_FILES_PER_NODE=2
            ELBENCHO_FILE_SIZE=4K
            ELBENCHO_SCALE_READ_WRITE_DURATION=1
            ELBENCHO_LIVE_CSV_EXTENDED=0
            ELBENCHO_READ_AFTER_WRITE_PAUSE=0
            ELBENCHO_SWEEP_WRITE_ONLY=0
            ELBENCHO_SWEEP_WRITE_NO_READ=1
            ELBENCHO_SWEEP_READ_FROM=
            io_size=4K thread_count=1 io_depth=1 dio_or_bio=bio
            use_random=0 force_single=0
            run_an_elbencho() {{
                local phase= path= arg
                for arg in "$@"; do
                    [[ "$arg" == --write ]] && phase=WRITE
                    [[ "$arg" == --delfiles ]] && phase=RMFILES
                    [[ "$arg" == --jsonfile=* ]] && path="${{arg#--jsonfile=}}"
                done
                if [[ "$phase" == WRITE ]]; then
                    touch "$target/r0-f0" "$target/r0-f1"
                    printf '{{"phase_type":"WRITE","last_done":{{"elapsed_time_ms":"10","entries":"2","bytes":"8192"}}}}\n' >"$path"
                elif [[ "$phase" == RMFILES ]]; then
                    rm -f "$target/r0-f0" "$target/r0-f1"
                    printf '{{"phase_type":"RMFILES","last_done":{{"elapsed_time_ms":"3","entries":"2"}}}}\n' >"$path"
                    return 23
                fi
            }}
            elbencho_set_cell_run_context 0007 1 a "$target" \
                "$output_dir" "$output_dir" _elbencho_noop_cell_hook \
                _elbencho_noop_cell_hook
            set +e
            run_elbencho_io_sweep_iteration_shared_generated
            rc=$?
            set -e
            [[ "$rc" -eq 23 ]]
            [[ ! -e "$target" ]]
            grep -q $'delete_completed_files\t2' \
                "$output_dir/executions/0007.workload.tsv"
            grep -q $'delete_completion_state\tincomplete' \
                "$output_dir/executions/0007.workload.tsv"
            grep -q $'completion_state\tincomplete' \
                "$output_dir/executions/0007.workload.tsv"
            grep -q $'failure_cleanup_state\tcompleted' \
                "$output_dir/executions/0007.workload.tsv"
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_successful_rmfiles_still_requires_an_empty_target(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            root="$tmp/data"
            target="$root/target-DS-e0008"
            mkdir -p "$target"
            touch "$target/unexpected"
            ELBENCHO_RUN_EXECUTION_ID=0008
            ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV="$target"
            ELBENCHO_RUN_GENERATED_TEST_ROOT="$root"
            ELBENCHO_RUN_TEST_DIR_SUFFIX=-DS-e0008
            ELBENCHO_FILE_LAYOUT=shared-directory
            ELBENCHO_SWEEP_READ_FROM=
            ! _elbencho_remove_empty_captured_shared_target
            [[ -f "$target/unexpected" ]]
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rmfiles_final_argv_uses_captured_distributed_hosts(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            ELBENCHO_RUN_NODE_COUNT=2
            ELBENCHO_RUN_HOSTS_CSV=host-a,host-b
            _elbencho_run_master_with_coredump() {{
                printf '%s\n' "$@" >"$tmp/argv"
            }}
            run_an_elbencho --delfiles --threads=4 --dirs=0 --files=1 \
                --jsonfile="$tmp/delete.json" /checkpoint/old
            grep -Fx -- '--hosts' "$tmp/argv"
            grep -Fx -- 'host-a,host-b' "$tmp/argv"
            grep -Fx -- '--threads=4' "$tmp/argv"
            grep -Fx -- '--dirs=0' "$tmp/argv"
            grep -Fx -- '--files=1' "$tmp/argv"
            grep -Fx -- '/checkpoint/old' "$tmp/argv"
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rmfiles_rc_survives_failed_recursive_backstop(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            root="$tmp/data"
            target="$root/target-DS-e0009"
            workload="$tmp/out/executions/0009.workload.tsv"
            mkdir -p "$target"
            touch "$target/r0-f0"
            ELBENCHO_RUN_EXECUTION_ID=0009
            ELBENCHO_RUN_SCRATCH_OUTPUT_DIR="$tmp/out"
            ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV="$target"
            ELBENCHO_RUN_GENERATED_TEST_ROOT="$root"
            ELBENCHO_RUN_TEST_DIR_SUFFIX=-DS-e0009
            ELBENCHO_FILE_LAYOUT=shared-directory
            ELBENCHO_FILES_PER_NODE=1
            ELBENCHO_SWEEP_READ_FROM=
            ELBENCHO_SCALE_READ_WRITE_DURATION=1
            _elbencho_shared_workload_begin "$workload" 1 1 4096 0 1
            _elbencho_workload_set completion_state incomplete
            _elbencho_workload_set delete_completion_state incomplete
            _elbencho_workload_write
            _elbencho_shared_arm_cleanup
            _elbencho_remove_captured_shared_target() {{ return 1; }}
            set +e
            _elbencho_shared_fail 29
            rc=$?
            set -e
            [[ "$rc" -eq 29 ]]
            [[ -f "$target/r0-f0" ]]
            grep -q $'completion_state\tincomplete' "$workload"
            grep -q $'delete_completion_state\tincomplete' "$workload"
            grep -q $'failure_cleanup_state\tfailed' "$workload"
            ELBENCHO_SHARED_CLEANUP_ARMED=0
            _elbencho_shared_restore_traps
            """)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("GENERATED DATASET FAILURE CLEANUP FAILED", result.stderr)

    def test_cache_race_keeps_staging_totals(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            mkdir -p "$tmp/data"
            output_dir="$tmp/out-DS"
            test_dirs_csv="$tmp/data"
            ELBENCHO_SWEEP_READ_FROM="$tmp/data"
            ELBENCHO_SINGLE_BIG_FILE=0
            ELBENCHO_SCALE_READ_WRITE_DURATION=1
            ELBENCHO_LIVE_CSV_EXTENDED=0
            io_size=4K thread_count=1 io_depth=1 dio_or_bio=bio use_random=0
            force_single=0
            run_an_elbencho() {{
                local tree= arg
                while [[ $# -gt 0 ]]; do
                    arg="$1"; shift
                    if [[ "$arg" == --treefile ]]; then tree="$1"; shift; fi
                done
                printf 'f 5 scanned\n' >"$tree"
                printf 'f 999 competing\n' \
                    >"$tmp/data/.storage-scale-test-elbencho-treefile.txt"
            }}
            elbencho_set_cell_run_context 0001 1 a "$tmp/data" \
                "$output_dir" "$output_dir" _elbencho_noop_cell_hook \
                _elbencho_noop_cell_hook
            run_elbencho_io_sweep_iteration_staged
            grep -q $'dataset_bytes_total\t5' \
                "$output_dir/executions/0001.workload.tsv"
            grep -q $'treefile_cache_publish_outcome\talready_present' \
                "$output_dir/executions/0001.workload.tsv"
            grep -q $'reader_nodes\t1' \
                "$output_dir/executions/0001.workload.tsv"
            grep -q $'reader_threads_per_node\t1' \
                "$output_dir/executions/0001.workload.tsv"
            grep -q $'reader_iodepth\t1' \
                "$output_dir/executions/0001.workload.tsv"
            grep -q $'files_per_reader_node\tnull' \
                "$output_dir/executions/0001.workload.tsv"
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cache_publish_failure_keeps_captured_staging_totals(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            mkdir -p "$tmp/data"
            output_dir="$tmp/out-DS"
            test_dirs_csv="$tmp/data"
            ELBENCHO_SWEEP_READ_FROM="$tmp/data"
            ELBENCHO_SINGLE_BIG_FILE=0
            ELBENCHO_SCALE_READ_WRITE_DURATION=1
            ELBENCHO_LIVE_CSV_EXTENDED=0
            io_size=4K thread_count=1 io_depth=1 dio_or_bio=bio use_random=0
            force_single=0
            run_an_elbencho() {{
                local tree= arg
                while [[ $# -gt 0 ]]; do
                    arg="$1"; shift
                    if [[ "$arg" == --treefile ]]; then tree="$1"; shift; fi
                done
                printf 'f 7 staging-view\n' >"$tree"
            }}
            mv() {{
                local destination="${{@: -1}}"
                if [[ "$destination" == \
                        "$tmp/data/.storage-scale-test-elbencho-treefile.txt" ]]; then
                    return 1
                fi
                command mv "$@"
            }}
            elbencho_set_cell_run_context 0001 1 a "$tmp/data" \
                "$output_dir" "$output_dir" _elbencho_noop_cell_hook \
                _elbencho_noop_cell_hook
            run_elbencho_io_sweep_iteration_staged
            [[ ! -e "$tmp/data/.storage-scale-test-elbencho-treefile.txt" ]]
            grep -q $'dataset_files_total\t1' \
                "$output_dir/executions/0001.workload.tsv"
            grep -q $'dataset_bytes_total\t7' \
                "$output_dir/executions/0001.workload.tsv"
            grep -q $'treefile_cache_publish_outcome\tnot_created' \
                "$output_dir/executions/0001.workload.tsv"
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cleanup_refuses_unsuffixed_target(self) -> None:
        result = _run(f"""
            set -e
            source "{_FUNCTIONS}"
            tmp=$(mktemp -d)
            trap 'rm -rf "$tmp"' EXIT
            mkdir -p "$tmp/root/unsafe"
            ELBENCHO_FILE_LAYOUT=shared-directory
            ELBENCHO_FILES_PER_NODE=1
            ELBENCHO_SWEEP_READ_FROM=
            ELBENCHO_RUN_EXECUTION_ID=0001
            ELBENCHO_RUN_GENERATED_TEST_ROOT="$tmp/root"
            ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV="$tmp/root/unsafe"
            ELBENCHO_RUN_TEST_DIR_SUFFIX=-DS-e0001
            ! _elbencho_remove_captured_shared_target
            [[ -d "$tmp/root/unsafe" ]]
            """)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
