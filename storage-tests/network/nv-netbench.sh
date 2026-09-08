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

usage() {
    cat <<EOF
Usage: $(basename "$0") --nodes <node_spec> [--mode <mode>] [--bidirectional]

Runs network performance benchmarks using elbencho netbench mode.

This script tests network throughput and latency between nodes using
elbencho's --netbench mode with configurable block sizes, response sizes,
and connection parameters.

REQUIRED FLAGS:
  --nodes <node_spec>   Comma-separated list of node counts or ranges

SPECIFICATION FORMAT (for --nodes):
  X           Single value (e.g., "4" runs with 4 nodes)
  X-Y         Range from X to Y with increment 1 (e.g., "2-4" runs 2,3,4)
  X-Y+Z       Range from X to Y with increment Z (e.g., "2-8+2" runs 2,4,6,8)

  Multiple elements can be comma-separated:
    "2,4,8"         runs with counts: 2, 4, 8
    "2-4,8"         runs with counts: 2, 3, 4, 8

MODES (--mode, default: half-and-half):
  half-and-half   Split nodes into clients and servers.
                  Default: Unidirectional - runs both directions sequentially
                           (Group A → B, then Group B → A; two benchmark runs)
                  With --bidirectional: Both directions run simultaneously
                           (one benchmark run with full mesh traffic)

OPTIONS:
  --bidirectional     Enable simultaneous bidirectional traffic.
                      Uses 2 elbencho services per node (ports N and N+1).
                      Runs 2 coordinator processes in parallel (A→B and B→A).
                      Tests full duplex network capacity with no localhost traffic.
  -h, --help          Display this help message

ENVIRONMENT VARIABLES (from env.sh):
  NETBENCH_HOST_NIC_GBPS  Host NIC speed in Gbps (currently: ${NETBENCH_HOST_NIC_GBPS})
  NETBENCH_TARGET_RUNTIME Target runtime in seconds (currently: ${NETBENCH_TARGET_RUNTIME})
  NETBENCH_PORT           Service port (currently: ${NETBENCH_PORT})
  NETBENCH_BLOCKSIZE      Bytes sent per request (currently: ${NETBENCH_BLOCKSIZE})
  NETBENCH_RESPSIZE       Bytes returned per response (currently: ${NETBENCH_RESPSIZE})
  NETBENCH_THREADS        Thread counts to sweep (currently: ${NETBENCH_THREADS[*]})
  NETBENCH_ITERATIONS     Number of iterations (currently: ${NETBENCH_ITERATIONS})

  Per-thread size is calculated as: (TARGET_RUNTIME × NIC_GBPS × 125MB) / threads
  This normalizes runtime across different thread counts.

EXAMPLES:
  $(basename "$0") --nodes 2,4,8

    Runs unidirectional netbench with 2, 4, and 8 nodes.
    Tests both directions sequentially: Group A → B, then Group B → A.

  $(basename "$0") --nodes 2-8+2 --bidirectional

    Runs bidirectional netbench (full mesh) with 2, 4, 6, 8 nodes.
    Every node sends to and receives from every other node simultaneously.

OUTPUT:
  Results are written to \$RESULTS_DIR/netbench-<mode>-<datestamp>/
  Each configuration produces:
    - netbench-<mode>-c_<nodes>-t_<threads>_<datestamp>_iter<N>.out
    - netbench-<mode>-c_<nodes>-t_<threads>_<datestamp>_iter<N>.csv

EOF
    exit 1
}

# Initialize flags
nodes_spec=""
mode=""
bidirectional="false"

# Parse flags
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            usage
            ;;
        --nodes)
            if [[ $# -lt 2 ]]; then
                echo "Error: --nodes requires an argument" >&2
                usage
            fi
            nodes_spec="$2"
            shift 2
            ;;
        --mode)
            if [[ $# -lt 2 ]]; then
                echo "Error: --mode requires an argument" >&2
                usage
            fi
            mode="$2"
            shift 2
            ;;
        --bidirectional)
            bidirectional="true"
            shift
            ;;
        -*)
            echo "Error: Unknown option: $1" >&2
            usage
            ;;
        *)
            echo "Error: Unexpected argument: $1" >&2
            echo "       This script accepts --nodes (required), --mode (optional), and --bidirectional flags." >&2
            usage
            ;;
    esac
done

# Validate required flags
if [[ -z "$nodes_spec" ]]; then
    echo "Error: --nodes is required" >&2
    usage
fi

# Default mode to half-and-half if not specified
if [[ -z "$mode" ]]; then
    mode="half-and-half"
fi

# Validate mode
case "$mode" in
    half-and-half)
        ;;
    *)
        echo "Error: Invalid mode '$mode'. Must be 'half-and-half'" >&2
        exit 1
        ;;
esac

# Parse node specification into array
if ! mapfile -t node_counts < <(parse_range_specification "$nodes_spec"); then
    echo "Error: Invalid node specification: $nodes_spec" >&2
    exit 1
fi
if [[ ${#node_counts[@]} -eq 0 ]]; then
    echo "Error: Node specification produced no node counts" >&2
    exit 1
fi

# Validate minimum node count (all modes require at least 2 nodes)
for nc in "${node_counts[@]}"; do
    if [[ "$nc" -lt 2 ]]; then
        echo "Error: All netbench modes require at least 2 nodes (got $nc in specification)" >&2
        exit 1
    fi
done

# Validate NETBENCH_THREADS array
if [[ ${#NETBENCH_THREADS[@]} -eq 0 ]]; then
    echo "Error: NETBENCH_THREADS must have at least one element" >&2
    exit 1
fi

# Validate NETBENCH_ITERATIONS
if [[ -z "${NETBENCH_ITERATIONS:-}" ]] || [[ "$NETBENCH_ITERATIONS" -lt 1 ]]; then
    echo "Error: NETBENCH_ITERATIONS must be at least 1" >&2
    exit 1
fi

# Mode abbreviation for directory naming
if [[ "$bidirectional" == "true" ]]; then
    mode_abbrev="bidir"
else
    mode_abbrev="half"
fi

# Using one datestamp for all the jobs
DS=$(date -u +"%Y%m%dZ%H%M%S")
OUTPUT_DIR="${RESULTS_DIR}/netbench-${mode_abbrev}-${DS}"

mkdir -p "$OUTPUT_DIR"

# Log output to file as well as the invoking stdout/stderr streams
out_log="${OUTPUT_DIR}/netbench-${mode_abbrev}-sweep-${DS}-runner.log"
exec 1> >(tee -a "${out_log}")
exec 2> >(tee -a "${out_log}" >&2)

echo "Starting netbench sweep at $(date -u +"%Y-%m-%d %H:%M:%S UTC")"
echo "Mode: $mode"
echo "Bidirectional: $bidirectional"
echo "Node counts: ${node_counts[*]}"
echo "Thread counts: ${NETBENCH_THREADS[*]}"
echo "Iterations: $NETBENCH_ITERATIONS"
echo "Output directory: $OUTPUT_DIR"
if [[ "$bidirectional" == "true" ]]; then
    echo "Note: Bidirectional uses 2 services per node (ports $NETBENCH_PORT and $((NETBENCH_PORT + 1)))"
    echo "      and runs 2 coordinator processes simultaneously (A→B and B→A)"
fi
echo

cd "${SCALE_TEST_BASE}/storage-tests/network" || exit 1

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
    if [ -n "$SLURM_ENABLED" ]; then
        job_name=$(make_sbatch_job_name "netbench-${mode_abbrev}" "$DS" "${nodes}n")
        output_fmt="${OUTPUT_DIR}/${job_name}-%j.out"
        if ! run_sbatch_job "$nodes" "$job_name" "$output_fmt" \
                "${nodes} node" \
                sbatch/_nv-netbench.sh \
                    "$OUTPUT_DIR" "$bidirectional"; then
            exit 1
        fi
    fi

    if [ -n "$SSH_ENABLED" ]; then
        # Run the job over ssh
        choose_N_ssh_hosts "$nodes"
        ssh/_nv-netbench.sh "$OUTPUT_DIR" "$bidirectional"
    fi
done

if [ -n "$SLURM_ENABLED" ]; then
    tail_until_complete "$JOBID" "${log_files[@]}"
    exit $?
fi
