#!/usr/bin/env bash

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

# Only run directly, not from within slurm unless SSH_ENABLED is set
if [ -n "${SLURM_JOB_ID:-}" ] && [ -z "${SSH_ENABLED:-}" ]; then
    echo "Error: Don't run this with slurm, just run it directly." >&2
    exit 1
fi

# Boilerplate to find and source env.sh
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd) || {
    echo "Error: Failed to determine script directory" >&2
    exit 1
}
if [[ ! -d "${SCRIPT_DIR}" ]]; then
    echo "Error: Script directory '${SCRIPT_DIR}' does not exist" >&2
    exit 1
fi
readonly SCRIPT_DIR

if ! source_output=$("$SHELL" -c ". '${SCRIPT_DIR}/../../env.sh'" 2>&1); then
    printf "%s\n\nFailed to source env.sh; fix ^^^^^^^^^^\n" "$source_output"
    exit 1
fi

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/../../env.sh"

# Validate FS testing is enabled
if [ -z "${FS_ENABLED:-}" ]; then
    echo "Error: Filesystem testing is not enabled (FS_ENABLED is empty)" >&2
    echo "  Set TEST_DIRS in env.sh to enable filesystem testing" >&2
    echo "  Example: declare -A TEST_DIRS=([\"/path/to/fs\"]=1)" >&2
    exit 1
fi

# Validate required mdtest-elbencho variables
if [ -z "${MDTEST_BRANCH_FACTOR:-}" ]; then
    echo "Error: MDTEST_BRANCH_FACTOR is not defined in env.sh" >&2
    exit 1
fi
if [ -z "${MDTEST_ITEMS_PER_DIR:-}" ]; then
    echo "Error: MDTEST_ITEMS_PER_DIR is not defined in env.sh" >&2
    exit 1
fi
if [ -z "${MDTEST_ITERATIONS:-}" ]; then
    echo "Error: MDTEST_ITERATIONS is not defined in env.sh" >&2
    exit 1
fi

usage() {
    cat <<EOF
Usage: $(basename "$0") --nodes <node_spec> --tasks <task_spec>
       [--single-dir-file-target <count>]

Benchmarks filesystem metadata operations using elbencho.

This script performs a parameter sweep over node counts and tasks-per-node,
running metadata operations (create dirs, create files, stat, delete files,
delete dirs) with elbencho's distributed benchmarking capabilities.

REQUIRED FLAGS:
  --nodes <node_spec>   Comma-separated list of node counts or ranges
  --tasks <task_spec>   Comma-separated list of task counts or ranges

  IMPORTANT: Both --nodes and --tasks must be specified together.

SPECIFICATION FORMAT (same for both --nodes and --tasks):
  X           Single value (e.g., "5" runs with 5 nodes/tasks)
  X-Y         Range from X to Y with increment 1 (e.g., "2-5" runs 2,3,4,5)
  X-Y+Z       Range from X to Y with increment Z (e.g., "1-10+3" runs 1,4,7,10)

  Multiple elements can be comma-separated:
    "1,4,8"         runs with counts: 1, 4, 8
    "1-4,16"        runs with counts: 1, 2, 3, 4, 16
    "16,8,4,2,1"    runs with counts: 16, 8, 4, 2, 1 (descending)
    "3-10+2,13"     runs with counts: 3, 5, 7, 9, 10, 13

OPTIONS:
  --single-dir-file-target <count>
                        Select the dense (single flat directory) metadata
                        workload instead of the default branched directory
                        layout, targeting approximately <count> files.

                        All workers create, stat, and delete uniquely named
                        zero-byte files directly in one shared directory
                        (elbencho "-n 0"); no per-rank/per-dir subdirectories
                        are created, so the directory-create and
                        directory-delete phases are skipped.

                        Requires exactly one node count, one task count, and a
                        TEST_DIRS configuration that resolves to exactly one
                        generated target directory.

                        TARGET vs ACTUAL: elbencho's files-per-worker value is
                        uniform, so the achievable total is quantized to
                        whole workers:

                          workers          = nodes x tasks
                          files_per_worker = round(count / workers)
                          actual_files     = workers x files_per_worker

                        The actual total may therefore differ slightly from
                        the requested target. Both values are printed before
                        the run and saved with the results. For example,
                        2 nodes x 64 tasks with a target of 1000000 yields
                        7813 files per worker and 1000064 actual files.

  -h, --help            Display this help message

ENVIRONMENT VARIABLES (from env.sh):
  MDTEST_BRANCH_FACTOR  Branching factor (currently: ${MDTEST_BRANCH_FACTOR:-unset})
                        - Subdirs created under each TEST_DIRS entry
                        - Also used as dirs-per-thread for elbencho (-n flag)
                        - Effective dirs per thread = MDTEST_BRANCH_FACTOR^2
  MDTEST_ITEMS_PER_DIR  Files per directory per thread (currently: ${MDTEST_ITEMS_PER_DIR:-unset})
  MDTEST_ITERATIONS     Number of benchmark iterations (currently: ${MDTEST_ITERATIONS:-unset})

  MDTEST_BRANCH_FACTOR and MDTEST_ITEMS_PER_DIR apply to the default branched
  layout only; --single-dir-file-target derives its own files-per-worker value.

FORMULA:
  Total files = nodes x tasks x MDTEST_BRANCH_FACTOR^2 x MDTEST_ITEMS_PER_DIR

  With --single-dir-file-target <count>:
    Total files = nodes x tasks x round(count / (nodes x tasks))

EXAMPLES:
  $(basename "$0") --nodes 1,2,4 --tasks 16,32,64

    Runs with:
      - Nodes: 1, 2, 4
      - Tasks per node: 16, 32, 64
      - Total combinations: 9 (3 node counts x 3 task counts)

  $(basename "$0") --nodes 1-8+2 --tasks 32,64,128

    Runs with:
      - Nodes: 1, 3, 5, 7, 8
      - Tasks per node: 32, 64, 128

  $(basename "$0") --nodes 2 --tasks 64 --single-dir-file-target 1000000

    Runs create, stat, and delete against one shared flat directory holding
    approximately 1,000,000 zero-byte files (actual: 1,000,064).

METADATA OPERATIONS:
  The benchmark runs elbencho in phases:
    1. Create directories and files (-w -d)
    2. Stat files (--stat)
    3. Delete files and directories (-F -D)

  With --single-dir-file-target, there are no directories to create or delete,
  so the phases are file create (-w), stat (--stat), and file delete (-F).

  Uses --rotatehosts 1 for the create phase so each node operates on files
  written by a different node, avoiding client cache effects. For stat/delete
  phases, the hosts list is rotated between phases. This applies to both
  directory layouts.

OUTPUT:
  Results are written to \$RESULTS_DIR/mdtest-elbencho-<datestamp>/
  Each configuration produces:
    - mdtest-elbencho-c_<nodes>-t_<tasks>_<datestamp>.out (human readable)
    - mdtest-elbencho-c_<nodes>-t_<tasks>_<datestamp>.csv (CSV format)

EOF
    exit 1
}

# Initialize flags
nodes_spec=""
tasks_spec=""
single_dir_target_files=""

# Parse flags
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            usage
            ;;
        --single-dir-file-target)
            if [[ $# -lt 2 ]]; then
                echo "Error: --single-dir-file-target requires an argument" >&2
                usage
            fi
            single_dir_target_files="$2"
            shift 2
            ;;
        --nodes)
            if [[ $# -lt 2 ]]; then
                echo "Error: --nodes requires an argument" >&2
                usage
            fi
            nodes_spec="$2"
            shift 2
            ;;
        --tasks)
            if [[ $# -lt 2 ]]; then
                echo "Error: --tasks requires an argument" >&2
                usage
            fi
            tasks_spec="$2"
            shift 2
            ;;
        -*)
            echo "Error: Unknown option: $1" >&2
            usage
            ;;
        *)
            echo "Error: Unexpected argument: $1" >&2
            echo "       This script only accepts --nodes, --tasks and" >&2
            echo "       --single-dir-file-target flags." >&2
            usage
            ;;
    esac
done

# Validate that both flags are specified
if [[ -z "$nodes_spec" ]]; then
    echo "Error: --nodes is required" >&2
    usage
fi
if [[ -z "$tasks_spec" ]]; then
    echo "Error: --tasks is required" >&2
    usage
fi

# Parse specifications into arrays
if ! mapfile -t node_counts < <(parse_range_specification "$nodes_spec"); then
    echo "Error: Invalid node specification: $nodes_spec" >&2
    exit 1
fi
if [[ ${#node_counts[@]} -eq 0 ]]; then
    echo "Error: Node specification produced no node counts" >&2
    exit 1
fi

if ! mapfile -t task_counts < <(parse_range_specification "$tasks_spec"); then
    echo "Error: Invalid task specification: $tasks_spec" >&2
    exit 1
fi
if [[ ${#task_counts[@]} -eq 0 ]]; then
    echo "Error: Task specification produced no task counts" >&2
    exit 1
fi

# Dense (single flat directory) mode: validate the request and derive the
# uniform per-worker file count elbencho needs.
single_dir_files_per_worker=""
single_dir_actual_files=""
if [[ -n "$single_dir_target_files" ]]; then
    if ! [[ "$single_dir_target_files" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: --single-dir-file-target must be a positive integer" >&2
        echo "       Got: $single_dir_target_files" >&2
        exit 1
    fi
    if [[ ${#node_counts[@]} -ne 1 ]] || [[ ${#task_counts[@]} -ne 1 ]]; then
        echo "Error: --single-dir-file-target requires exactly one node count" >&2
        echo "       and one task count, because the per-worker file count is" >&2
        echo "       derived from nodes x tasks." >&2
        echo "       Got nodes: ${node_counts[*]}" >&2
        echo "       Got tasks: ${task_counts[*]}" >&2
        exit 1
    fi

    # The dense workload needs one shared directory, so TEST_DIRS must resolve
    # to a single generated target.
    if ! mapfile -t _dense_targets < <(generate_fs_test_directories "mdtest-elbencho"); then
        echo "Error: Unable to generate filesystem test directories" >&2
        exit 1
    fi
    if [[ ${#_dense_targets[@]} -ne 1 ]]; then
        echo "Error: --single-dir-file-target requires TEST_DIRS to resolve to" >&2
        echo "       exactly one generated target directory, but it produced" >&2
        echo "       ${#_dense_targets[@]}:" >&2
        printf '         %s\n' "${_dense_targets[@]}" >&2
        echo "       Use a single TEST_DIRS entry with weight 1." >&2
        exit 1
    fi
    unset _dense_targets

    if ! single_dir_files_per_worker=$(mdtest_single_dir_files_per_worker \
            "$single_dir_target_files" "${node_counts[0]}" "${task_counts[0]}"); then
        exit 1
    fi
    single_dir_actual_files=$(( node_counts[0] * task_counts[0] * single_dir_files_per_worker ))
fi

# Using one datestamp for all the jobs
DS=$(date -u +"%Y%m%dZ%H%M%S")
OUTPUT_DIR="${RESULTS_DIR}/mdtest-elbencho-${DS}"

mkdir -p "$OUTPUT_DIR"

write_mdtest_elbencho_env_used "${OUTPUT_DIR}/env_used.yaml" \
    "$nodes_spec" "$tasks_spec" \
    "$single_dir_target_files" "$single_dir_files_per_worker" \
    "$single_dir_actual_files"

# Log output to file as well as the invoking stdout/stderr streams
out_log="${OUTPUT_DIR}/mdtest-elbencho-sweep-${DS}-runner.log"
exec 1> >(tee -a "${out_log}")
exec 2> >(tee -a "${out_log}" >&2)

echo "Starting mdtest-elbencho sweep at $(date -u +"%Y-%m-%d %H:%M:%S UTC")"
echo "Node counts: ${node_counts[*]}"
echo "Task counts: ${task_counts[*]}"
echo "Output directory: $OUTPUT_DIR"
if [[ -n "$single_dir_target_files" ]]; then
    print_mdtest_single_dir_target_summary \
        "$single_dir_target_files" "${node_counts[0]}" "${task_counts[0]}" \
        "$single_dir_files_per_worker" "$single_dir_actual_files"
else
    echo "Directory layout: standard (branched, MDTEST_BRANCH_FACTOR=${MDTEST_BRANCH_FACTOR})"
fi
echo

cd "${SCALE_TEST_BASE}/storage-tests/fs" || exit 1

if [ -n "$SLURM_ENABLED" ]; then
    # shellcheck disable=SC2034  # Used by run_sbatch_job
    declare -a sbatch_cmd
    build_sbatch_cmd sbatch_cmd
    # shellcheck disable=SC2034
    sleep_time=10
    log_files=()
    # shellcheck disable=SC2034  # Used by run_sbatch_job
    g_sbatch_opts=()
    print_slurm_node_warnings "${node_counts[-1]}"
elif [ -n "$SSH_ENABLED" ]; then
    # Ensure all possible ssh nodes can run elbencho
    if ! ensure_all_ssh_nodes_can_elbencho; then
        echo "Error: Failed to ensure all ssh nodes can elbencho" >&2
        exit 1
    fi
fi

JOBID=""

for nodes in "${node_counts[@]}"; do
    for tasks_per_node in "${task_counts[@]}"; do
        if [ -n "$SLURM_ENABLED" ]; then
            job_name=$(make_sbatch_job_name "mdtest-elbencho" "$DS" "${nodes}n-${tasks_per_node}t")
            output_fmt="${OUTPUT_DIR}/${job_name}-%j.out"
            if ! run_sbatch_job "$nodes" "$job_name" "$output_fmt" \
                    "${nodes} node, ${tasks_per_node} tasks" \
                    sbatch/_nv-mdtest-elbencho.sh \
                        "$OUTPUT_DIR" "$tasks_per_node" \
                        "$single_dir_target_files" "$single_dir_files_per_worker"; then
                exit 1
            fi
        fi

        if [ -n "$SSH_ENABLED" ]; then
            # Run the job over ssh
            choose_N_ssh_hosts "$nodes"
            ssh/_nv-mdtest-elbencho.sh "$OUTPUT_DIR" "$tasks_per_node" \
                "$single_dir_target_files" "$single_dir_files_per_worker"
        fi
    done
done

if [ -n "$SLURM_ENABLED" ]; then
    tail_until_complete "$JOBID" "${log_files[@]}"
    exit $?
fi
