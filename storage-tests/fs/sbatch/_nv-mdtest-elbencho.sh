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
tasks_per_node="$2"
single_dir_target_files="${3:-}"
single_dir_files_per_worker="${4:-}"

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
if [ -z "$tasks_per_node" ]; then
    echo "ERROR: second arg must be tasks_per_node"
    exit 1
fi

# Build comma-separated list of node IPs
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

# Get test directories for this iteration
mapfile -t test_dirs < <(generate_fs_test_directories "mdtest-elbencho")
test_dirs_csv=$(IFS=,; echo "${test_dirs[*]}")

SRUN_ELBENCHO_PID=""
if [[ "$SLURM_JOB_NUM_NODES" -gt 1 ]]; then
    # Stop any existing elbencho services and start fresh (srun stays running in background)
    stop_elbencho_services_srun "" ""  # Empty PID for initial cleanup
    # shellcheck disable=SC2119  # No args = use default port
    SRUN_ELBENCHO_PID=$(start_elbencho_services_srun)
    sleep 2  # Give services time to start
    # shellcheck disable=SC2119
    check_elbencho_services_srun
fi

# Set env vars for lib/_elbencho_functions.sh
output_dir="$OUTPUT_DIR"
export output_dir tasks_per_node test_dirs_csv

# Source the functions and run the metadata benchmark
# shellcheck disable=SC1091
source "$SCALE_TEST_BASE/lib/_elbencho_functions.sh"
_mdtest_export_layout_env "$single_dir_target_files" "$single_dir_files_per_worker"
run_elbencho_metadata_benchmark

# Stop elbencho services on all nodes
if [[ "$SLURM_JOB_NUM_NODES" -gt 1 ]]; then
    stop_elbencho_services_srun "$SRUN_ELBENCHO_PID"
fi
