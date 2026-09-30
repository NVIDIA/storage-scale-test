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

"""Argument and lifecycle matrix for shared-directory executions."""

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


_HARNESS = f"""
set -e
source "{_FUNCTIONS}"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
root="$tmp/data"
output_dir="$tmp/elbencho-DS"
mkdir -p "$root"
ELBENCHO_RUN_GENERATED_TEST_ROOT="$root"
ELBENCHO_FILE_LAYOUT=shared-directory
ELBENCHO_FILE_SIZE=4K
ELBENCHO_LIVE_CSV_EXTENDED=0
ELBENCHO_READ_AFTER_WRITE_PAUSE=0
ELBENCHO_SWEEP_READ_FROM=
io_size=4K
io_depth=1
dio_or_bio=dio
use_random=0
force_single=1
run_an_elbencho() {{
    printf '%s\n' "$*" >>"$CAPTURE"
    local phase= json_path= arg
    for arg in "$@"; do
        [[ "$arg" == --write ]] && phase=WRITE
        [[ "$arg" == --read ]] && phase=READ
        [[ "$arg" == --delfiles ]] && phase=RMFILES
        [[ "$arg" == --jsonfile=* ]] && json_path="${{arg#--jsonfile=}}"
    done
    if [[ "$phase" == RMFILES ]]; then
        printf '{{"phase_type":"RMFILES","last_done":{{"elapsed_time_ms":"3","entries":"%s"}}}}\n' \
            "$EXPECTED_FILES" >"$json_path"
    elif [[ -n "$phase" ]]; then
        printf '{{"phase_type":"%s","last_done":{{"elapsed_time_ms":"2","entries":"%s","bytes":"%s"}}}}\n' \
            "$phase" "$EXPECTED_FILES" "$EXPECTED_BYTES" >"$json_path"
    fi
}}
run_case() {{
    local id="$1" threads="$2" files="$3" mode="$4"
    ELBENCHO_RUN_EXECUTION_ID="$id"
    ELBENCHO_RUN_TEST_DIR_SUFFIX="-DS-e${{id}}"
    ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV="$root/target-DS-e${{id}}"
    test_dirs_csv="$ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV"
    ELBENCHO_RUN_TEST_DIRS_CSV="$test_dirs_csv"
    thread_count="$threads"
    ELBENCHO_FILES_PER_NODE="$files"
    EXPECTED_FILES="$files"
    EXPECTED_BYTES=$((files * 4096))
    ELBENCHO_SWEEP_WRITE_ONLY=0
    ELBENCHO_SWEEP_WRITE_NO_READ=0
    [[ "$mode" == write-only ]] && ELBENCHO_SWEEP_WRITE_ONLY=1
    [[ "$mode" == write-no-read ]] && ELBENCHO_SWEEP_WRITE_NO_READ=1
    elbencho_set_cell_run_context "$id" 1 host-a "$test_dirs_csv" \
        "$output_dir" "$output_dir" _elbencho_noop_cell_hook \
        _elbencho_noop_cell_hook
    run_elbencho_io_sweep_iteration_shared_generated
}}
"""


class TestElbenchoSharedDirectoryMatrixShell(unittest.TestCase):
    """Native arguments stay exact across the plan's topology matrix."""

    def test_all_topologies_apply_flat_counts_to_each_phase(self) -> None:
        result = _run(_HARNESS + """
            ELBENCHO_SCALE_READ_WRITE_DURATION=1
            FS_MAX_AGG_THROUGHPUT=1
            FS_MAX_NODE_THROUGHPUT_GBPS=1
            FS_MAX_NODE_IOPS=1
            for spec in 0001:1:1:1 0002:8:8:1 0003:1:8:8; do
                IFS=: read -r id threads files native_files <<<"$spec"
                CAPTURE="$tmp/$id.calls"
                run_case "$id" "$threads" "$files" default
                [[ $(wc -l <"$CAPTURE") -eq 4 ]]
                [[ $(grep -c -- "--dirs=0 --files=$native_files" "$CAPTURE") -eq 4 ]]
                [[ $(grep -c -- '--size=4K' "$CAPTURE") -eq 3 ]]
                [[ $(grep -c -- '--nocsvlabels' "$CAPTURE") -eq 3 ]]
                grep -q -- '--delfiles' "$CAPTURE"
                ! grep -- '--delfiles' "$CAPTURE" | \
                    grep -Eq -- '--size|--block|--direct|--iodepth|--sync'
                ! grep -Eq -- '--timelimit|--infloop|--nosvcshare' "$CAPTURE"
            done
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_duration_capacity_and_legacy_single_option_do_not_change_work(
        self,
    ) -> None:
        result = _run(_HARNESS + """
            compute_target_file_count_per_thread() { return 91; }
            thread_count=1
            ELBENCHO_FILES_PER_NODE=8
            ELBENCHO_SCALE_READ_WRITE_DURATION=1
            FS_MAX_AGG_THROUGHPUT=1
            FS_MAX_NODE_THROUGHPUT_GBPS=2
            FS_MAX_NODE_IOPS=3
            CAPTURE="$tmp/first.calls"
            run_case 0004 1 8 default
            cp "$output_dir/executions/0004.workload.tsv" "$tmp/first.tsv"

            ELBENCHO_SCALE_READ_WRITE_DURATION=9999
            FS_MAX_AGG_THROUGHPUT=9000000
            FS_MAX_NODE_THROUGHPUT_GBPS=8000000
            FS_MAX_NODE_IOPS=7000000
            CAPTURE="$tmp/second.calls"
            run_case 0004 1 8 default
            cmp "$tmp/first.calls" "$tmp/second.calls"
            grep -q $'dataset_files_total\t8' "$tmp/first.tsv"
            grep -q $'dataset_files_total\t8' \
                "$output_dir/executions/0004.workload.tsv"
            grep -q $'dataset_bytes_total\t32768' "$tmp/first.tsv"
            grep -q $'dataset_bytes_total\t32768' \
                "$output_dir/executions/0004.workload.tsv"
            """)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_write_only_retains_and_write_no_read_cleans(self) -> None:
        result = _run(_HARNESS + """
            ELBENCHO_SCALE_READ_WRITE_DURATION=1
            FS_MAX_AGG_THROUGHPUT=1
            FS_MAX_NODE_THROUGHPUT_GBPS=1
            FS_MAX_NODE_IOPS=1

            CAPTURE="$tmp/write-only.calls"
            run_case 0005 1 1 write-only
            [[ -d "$root/target-DS-e0005" ]]
            [[ -f "$output_dir/executions/0005.write.json" ]]
            [[ ! -e "$output_dir/executions/0005.read.json" ]]
            [[ ! -e "$output_dir/executions/0005.delete.json" ]]
            grep -q $'completion_state\tcompleted' \
                "$output_dir/executions/0005.workload.tsv"
            grep -q $'delete_completion_state\tnot_applicable' \
                "$output_dir/executions/0005.workload.tsv"
            grep -q $'delete_elapsed_time_ms\tnull' \
                "$output_dir/executions/0005.workload.tsv"
            grep -q $'lifecycle_elapsed_time_ms\tnull' \
                "$output_dir/executions/0005.workload.tsv"
            [[ $(wc -l <"$CAPTURE") -eq 2 ]]

            printf '0\n' >"$tmp/tick-index"
            _elbencho_monotonic_milliseconds() {
                local index
                index=$(<"$tmp/tick-index")
                printf '%s\n' $((index + 1)) >"$tmp/tick-index"
                if [[ "$index" -eq 0 ]]; then printf '2000\n'; else printf '2600\n'; fi
            }
            CAPTURE="$tmp/write-no-read.calls"
            run_case 0006 1 1 write-no-read
            [[ ! -e "$root/target-DS-e0006" ]]
            [[ -f "$output_dir/executions/0006.write.json" ]]
            [[ ! -e "$output_dir/executions/0006.read.json" ]]
            [[ -f "$output_dir/executions/0006.delete.json" ]]
            grep -q $'completion_state\tcompleted' \
                "$output_dir/executions/0006.workload.tsv"
            grep -q $'delete_completion_state\tcompleted' \
                "$output_dir/executions/0006.workload.tsv"
            grep -q $'write_elapsed_time_ms\t2' \
                "$output_dir/executions/0006.workload.tsv"
            grep -q $'read_elapsed_time_ms\tnull' \
                "$output_dir/executions/0006.workload.tsv"
            grep -q $'delete_elapsed_time_ms\t3' \
                "$output_dir/executions/0006.workload.tsv"
            grep -q $'write_delete_elapsed_time_ms\t5' \
                "$output_dir/executions/0006.workload.tsv"
            grep -q $'lifecycle_elapsed_time_ms\t600' \
                "$output_dir/executions/0006.workload.tsv"
            [[ $(wc -l <"$CAPTURE") -eq 3 ]]
            """)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
