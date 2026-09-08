#!/bin/bash

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
#
# SLURM dispatcher for netbench tests.

if [ -z "${SLURM_JOB_ID}" ]; then
    echo "This script should be run via slurm."
    exit 1
fi

# Boilerplate to find and source env.sh from within a slurm job
dir="${SLURM_SUBMIT_DIR}"
while [[ "$dir" != "/" && ! -d "$dir/storage-tests" ]]; do
   dir="$(dirname "$dir")"
done
[[ "$dir" == "/" ]] && { echo "Error: Could not find SCALE_TEST_BASE" >&2;
   exit 1; }
SCALE_TEST_BASE="$dir"

if ! source_output=$("$SHELL" -c ". ${SCALE_TEST_BASE}/env.sh" 2>&1); then
    printf "%s\n\nFailed to source env.sh; fix ^^^^^^^^^^\n" "$source_output"
    exit 1
fi

# shellcheck disable=SC1091
source "${SCALE_TEST_BASE}/env.sh"

OUTPUT_DIR="$1"
bidirectional="$2"

# Extract datestamp identifier from end of the OUTPUT_DIR
DS="${OUTPUT_DIR##*-}"

if [ -z "$OUTPUT_DIR" ]; then
    echo "ERROR: first arg must be an existing directory!"
    exit 1
fi
if [ -z "$DS" ]; then
    echo "ERROR: directory $OUTPUT_DIR should end in '-<datestamp>'"
    exit 1
fi
if [ -z "$bidirectional" ]; then
    echo "ERROR: second arg must be bidirectional (true or false)"
    exit 1
fi

# Determine mode description for logging
if [[ "$bidirectional" == "true" ]]; then
    mode_desc="bidirectional"
else
    mode_desc="unidirectional"
fi

# Print summary
echo "Running netbench ($mode_desc) on $SLURM_JOB_NUM_NODES nodes"
echo "  NODELIST:        $SLURM_JOB_NODELIST"
echo "  Threads:         ${NETBENCH_THREADS[*]}"
echo "  Iterations:      ${NETBENCH_ITERATIONS}"
echo "  Block Size:      ${NETBENCH_BLOCKSIZE}"
echo "  Response Size:   ${NETBENCH_RESPSIZE}"
echo "  NIC Speed:       ${NETBENCH_HOST_NIC_GBPS} Gbps"
echo "  Target Runtime:  ${NETBENCH_TARGET_RUNTIME} sec"
echo "  Port:            ${NETBENCH_PORT}"
echo "  JobID:           $SLURM_JOB_ID"
echo "  Datestamp:       $DS"
echo "  OutputDir:       $OUTPUT_DIR"

# Build comma-separated list of node IPs (resolve hostnames to avoid DNS issues)
nodelist_ips=()
while read -r node; do
    ip=$(getent ahostsv4 "$node" | awk '{print $1; exit}')
    if [[ -z "$ip" ]]; then
        >&2 echo "Warning: Could not resolve $node to IPv4 address, using hostname"
        ip="$node"
    fi
    nodelist_ips+=("$ip")
done < <(scontrol show hostname "${SLURM_JOB_NODELIST:-}")

nodelist_expanded_comma_separated=$(IFS=,; echo "${nodelist_ips[*]}")
export nodelist_expanded_comma_separated

SRUN_PRIMARY_PID=""
SRUN_SECONDARY_PID=""
secondary_port=""
if [[ "$SLURM_JOB_NUM_NODES" -gt 1 ]]; then
    # Stop any existing elbencho services and start fresh (srun stays running in background)
    echo "Starting primary elbencho services on port $NETBENCH_PORT..."
    stop_elbencho_services_srun "" "$NETBENCH_PORT"  # Empty PID for initial cleanup
    SRUN_PRIMARY_PID=$(start_elbencho_services_srun "$NETBENCH_PORT" "$OUTPUT_DIR")
    sleep 2  # Give services time to start
    check_elbencho_services_srun "$NETBENCH_PORT"

    # For bidirectional mode, start secondary services (on NETBENCH_PORT + 1)
    if [[ "$bidirectional" == "true" ]]; then
        secondary_port=$((NETBENCH_PORT + 1))
        echo "Starting secondary elbencho services on port $secondary_port..."
        stop_elbencho_services_srun "" "$secondary_port"
        SRUN_SECONDARY_PID=$(start_elbencho_services_srun "$secondary_port" "$OUTPUT_DIR")
        sleep 2
        check_elbencho_services_srun "$secondary_port"
    fi
fi

# Set env vars for lib/_netbench_functions.sh
output_dir="$OUTPUT_DIR"
export output_dir bidirectional

# Source the functions library
# shellcheck disable=SC1091
source "$SCALE_TEST_BASE/lib/_netbench_functions.sh"

# Helper function to check and restart netbench elbencho services if needed
maybe_restart_netbench_services_slurm() {
    if [[ "$SLURM_JOB_NUM_NODES" -le 1 ]]; then
        return 0
    fi

    # Check primary services
    if ! check_elbencho_services_srun "$NETBENCH_PORT" 2>/dev/null; then
        echo "Primary services unhealthy, restarting..."
        stop_elbencho_services_srun "$SRUN_PRIMARY_PID" "$NETBENCH_PORT"
        SRUN_PRIMARY_PID=$(start_elbencho_services_srun "$NETBENCH_PORT" "$OUTPUT_DIR")
        sleep 2
        check_elbencho_services_srun "$NETBENCH_PORT"
    fi

    # Check secondary services (bidirectional mode only)
    if [[ "$bidirectional" == "true" ]] && [[ -n "$secondary_port" ]] &&
        ! check_elbencho_services_srun "$secondary_port" 2>/dev/null; then
        echo "Secondary services unhealthy, restarting..."
        stop_elbencho_services_srun "$SRUN_SECONDARY_PID" "$secondary_port"
        SRUN_SECONDARY_PID=$(start_elbencho_services_srun "$secondary_port" "$OUTPUT_DIR")
        sleep 2
        check_elbencho_services_srun "$secondary_port"
    fi
}

# Run the benchmark loop: iterations → thread counts
for ((iteration = 1; iteration <= NETBENCH_ITERATIONS; iteration++)); do
    echo "=== Iteration $iteration of $NETBENCH_ITERATIONS ==="

    for thread_count in "${NETBENCH_THREADS[@]}"; do
        echo "  Threads: $thread_count"

        # Check services are healthy before each run (restart if needed)
        maybe_restart_netbench_services_slurm

        # Export per-invocation variables
        export thread_count iteration

        run_netbench_half_half
    done
done

echo "Netbench sweep complete."

# Stop elbencho services on all nodes
if [[ "$SLURM_JOB_NUM_NODES" -gt 1 ]]; then
    echo "Stopping elbencho services..."
    stop_elbencho_services_srun "$SRUN_PRIMARY_PID" "$NETBENCH_PORT"
    if [[ "$bidirectional" == "true" ]]; then
        stop_elbencho_services_srun "$SRUN_SECONDARY_PID" "$secondary_port"
    fi
fi
