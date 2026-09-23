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

if [[ -n "${KUBECTL_ENABLED:-}" ]]; then
    echo "Error: kubectl execution is not supported by nv-warp-sweep.sh" >&2
    exit 1
fi

# Only run directly, not from within slurm unless SSH_ENABLED is set (slurm
# may be used to get nodes to ssh to)
if [ -n "${SLURM_JOB_ID:-}" ] && [ -z "${SSH_ENABLED:-}" ]; then
    echo "Error: Don't run this with slurm, just run it directly." >&2
    exit 1
fi

# NOTE: lib/env_base.sh now auto-sources object credentials into the env
if ! [ -f "$OBJ_AUTH_FILE" ]; then
    echo "  WARNING: MISSING CREDS FILE $OBJ_AUTH_FILE"
    exit 1
fi

print_usage() {
    cat << EOF
Usage: $0 [FLAGS] <max_node_count> [<increment_by>]
   or: $0 [FLAGS] --nodes <node_spec>

Runs warp on one or more nodes.

LEGACY MODE (positional arguments):
  Required arguments:
    max_node_count        Maximum number of nodes (integer)

  Optional arguments:
    increment_by         Increment value (integer; defaults to 1)

  When max_node_count > 1, we always execute with 1 node first, then
  in increments of 'increment_by', always executing with max_node_count
  nodes.  E.g. "5 1" would run with node-counts of 1,2,3,4,5; "5 2" would
  run with node counts of 1,2,4,5; and "6 3" would run with 1,3,6.

  Special case: When increment_by >= max_node_count, runs only with
  max_node_count nodes (skips 1).

NEW MODE (--nodes flag):
  --nodes <node_spec>   Comma-separated list of node counts or ranges

  Node specification format:
    X           Single value (e.g., "5" runs with 5 nodes)
    X-Y         Range from X to Y with increment 1 (e.g., "2-5" runs 2,3,4,5)
    X-Y+Z       Range from X to Y with increment Z (e.g., "1-10+3" runs 1,4,7,10)

  Multiple elements can be comma-separated:
    "1,4,8"         runs with node counts: 1, 4, 8
    "1-4,16"        runs with node counts: 1, 2, 3, 4, 16
    "16,8,4,2,1"    runs with node counts: 16, 8, 4, 2, 1 (descending)
    "3-10+2,13"     runs with node counts: 3, 5, 7, 9, 10, 13

  NOTE: Cannot mix --nodes flag with positional arguments.

Flags:
  -h, --help          Show this help message and exit
  --multipart         Allow multipart uploads
  --ranged            Test range reads
  --s3-express        Enable S3 Express One Zone mode (adds --signature=IAM,
                      suppresses --region to avoid endpoint rewriting)
  --nodes <spec>      Specify node counts (see above)

When --ranged is specified, \$WARP_RANGE_OBJ_SIZE is used for PUT object
size, the PUT object count is the number of nodes in the test, and the
--range-size value comes from the \$WARP_OBJ_SIZES variable.

Examples:
  $0 8 2                         # Legacy: runs with 1,2,4,6,8 nodes
  $0 --nodes 1,2,4,6,8           # New: exact match to legacy using explicit list
  $0 --nodes 1-8+2               # New: runs with 1,3,5,7,8 nodes (different from legacy!)
  $0 --nodes 1,4,8,16            # New: runs with 1,4,8,16 nodes
  $0 --multipart --nodes 2-4     # New: multipart enabled, runs with 2,3,4 nodes

EOF
    exit 0
}

# Initialize flags
multipart=false
ranged=false
s3_express=false
nodes_spec=""

# Parse flags
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            print_usage
            ;;
        --multipart)
            multipart=true
            shift
            ;;
        --ranged)
            ranged=true
            shift
            ;;
        --s3-express)
            s3_express=true
            shift
            ;;
        --nodes)
            if [[ $# -lt 2 ]]; then
                echo "Error: --nodes requires an argument" >&2
                print_usage
            fi
            nodes_spec="$2"
            shift 2
            ;;
        -*)
            echo "Error: Unknown option: $1" >&2
            print_usage
            ;;
        *)
            break
            ;;
    esac
done

# Validate mutual exclusivity between --nodes and positional args
if [[ -n "$nodes_spec" && $# -gt 0 ]]; then
    echo "Error: Cannot specify both --nodes flag and positional arguments" >&2
    print_usage
fi

# Generate node_counts array from either --nodes or legacy positional args
if [[ -n "$nodes_spec" ]]; then
    # Parse --nodes specification into array
    if ! mapfile -t node_counts < <(parse_range_specification "$nodes_spec"); then
        echo "Error: Invalid node specification: $nodes_spec" >&2
        exit 1
    fi
    if [[ ${#node_counts[@]} -eq 0 ]]; then
        echo "Error: Node specification produced no node counts" >&2
        exit 1
    fi
else
    # Legacy mode: positional arguments
    # Check required positional arg
    if [[ $# -lt 1 ]]; then
        echo "Error: Missing required argument: max_node_count" >&2
        print_usage
    fi

    # Validate max_node_count is integer
    if ! [[ $1 =~ ^[0-9]+$ ]]; then
        echo "Error: max_node_count must be a positive integer" >&2
        exit 1
    fi
    max_node_count=$1
    shift

    # Handle optional increment_by
    if [[ $# -gt 0 ]]; then
        if ! [[ $1 =~ ^[0-9]+$ ]]; then
            echo "Error: increment_by must be a positive integer" >&2
            exit 1
        fi
        increment_by=$1
        shift
    else
        increment_by=1
    fi

    # Check for extra args
    if [[ $# -gt 0 ]]; then
        echo "Error: Too many arguments" >&2
        print_usage
    fi

    # Generate node_counts array using legacy logic
    # (replicates next_node_in_sequence behavior)
    node_counts=()
    if [ "$increment_by" -ge "$max_node_count" ]; then
        nodes=$max_node_count
    else
        nodes=1
    fi
    while [ "$nodes" -le "$max_node_count" ]; do
        node_counts+=("$nodes")
        next_nodes=$(next_node_in_sequence "$nodes" "$max_node_count" "$increment_by") || break
        nodes=$next_nodes
    done
fi

# Using one datestamp for all the jobs will give output parsing script(s)
# more options for how to group-by when given just a directory containing
# these log files (easier to specify).
DS=$(date -u +"%Y%m%dZ%H%M%S")
OUTPUT_DIR="${RESULTS_DIR}/warp-${DS}"

mkdir -p "$OUTPUT_DIR"

# Log output to file as well as the invoking stdout/stderr streams
out_log="${OUTPUT_DIR}/warp-sweep-${DS}-runner.log"
exec 1> >(tee -a "${out_log}")
exec 2> >(tee -a "${out_log}" >&2)


cd "${SCALE_TEST_BASE}/storage-tests/object" || exit 1

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
    # Idempotently ensure all possible ssh nodes can warp
    # (nodes are picked at random for each element of the sweep, so we need to ensure
    # all nodes can warp before running the sweep)
    if ! ensure_all_ssh_nodes_can_warp; then
        echo "Error: Failed to ensure all ssh nodes can warp" >&2
        exit 1
    fi
fi

echo "multipart=$multipart"
echo "ranged=$ranged"
echo "s3_express=$( [[ "$s3_express" == "true" ]] && echo "True" || echo "False" )"

for nodes in "${node_counts[@]}"; do
    if [ -n "$SLURM_ENABLED" ]; then
        job_name=$(make_sbatch_job_name "warp" "$DS" "${node_counts[-1]}-${nodes}")
        output_fmt="${OUTPUT_DIR}/${job_name}-%j.out"
        if ! run_sbatch_job "$nodes" "$job_name" "$output_fmt" \
                "${nodes} node" \
                sbatch/_nv-warp-size-threads-sweep.sh \
                    "$OUTPUT_DIR" "$multipart" "$ranged" "$s3_express"; then
            exit 1
        fi
    fi

    if [ -n "$SSH_ENABLED" ]; then
        # Run the job over ssh
        choose_N_ssh_hosts "$nodes"
        ssh/_nv-warp-size-threads-sweep.sh \
            "$OUTPUT_DIR" "$multipart" "$ranged" "$s3_express"
    fi
done

if [ -n "$SLURM_ENABLED" ]; then
    tail_until_complete "$JOBID" "${log_files[@]}"
    exit $?
fi
