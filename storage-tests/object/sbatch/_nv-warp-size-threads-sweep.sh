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
# Find base directory by walking up from SLURM_SUBMIT_DIR until we find
# the storage-tests directory.
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

# NOTE: lib/env_base.sh now auto-sources object credentials into the env
if ! [ -f "$OBJ_AUTH_FILE" ]; then
    echo "ERROR: MISSING CREDS FILE $OBJ_AUTH_FILE"
    exit 1
fi

OUTPUT_DIR="$1"
multipart="${2:-false}"
ranged="${3:-false}"
s3_express="${4:-false}"
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

# Use unified warp configuration variables
THREAD_LIST=("${WARP_THREAD_LIST[@]}")
OBJ_SIZES=("${WARP_OBJ_SIZES[@]}")
put_duration="$WARP_PUT_DURATION"
put_min_objs="$WARP_PUT_MIN_FILES_PER_CLIENT"
max_get_time="$WARP_GET_DURATION"

echo "Running the size/threads sweep on $SLURM_JOB_NUM_NODES nodes for thread counts ${THREAD_LIST[*]}"
echo "  NODELIST:  $SLURM_JOB_NODELIST"
echo "  Bucket:    $OBJ_BUCKET"
echo "  Threads:   ${THREAD_LIST[*]}"
echo "  ObjSizes:  ${OBJ_SIZES[*]}"
echo "  Put Duration: $put_duration"
echo "  Put Min Objs: $put_min_objs"
echo "  Max Get Time: $max_get_time"
echo "  Multipart: $multipart"
echo "  Ranged:    $ranged"
echo "  S3Express: $( [[ "$s3_express" == "true" ]] && echo "True" || echo "False" )"
echo "  RPS Budget: GET: ${WARP_RPS_BUDGET_GET:-(no limit)}, PUT: ${WARP_RPS_BUDGET_PUT:-(no limit)}"
echo "  JobID:     $SLURM_JOB_ID"
echo "  Datestamp: $DS"
echo "  OutputDir: $OUTPUT_DIR"

# NOTE: we used to call ensure_bucket_empty here, but to support the SSH case,
# that's now done (more frequently) inside run_warp_io_sweep_iteration()

nodelist_expanded_comma_separated=$(expanded_comma_sep_slurm_nodes)
export nodelist_expanded_comma_separated

if [[ "$SLURM_JOB_NUM_NODES" -gt 1 ]]; then
    ensure_no_processes_running_srun "warp"
    sleep 2

    # Start all warp clients, one per node, using srun in background
    # Keep srun running so it manages the processes; we'll kill srun to clean up later
    SRUN_WARP_PID=$(start_background_clients_srun "$WARP" client 0.0.0.0:7761)

    sleep 2

    # Validate each node has a warp client listening on 0.0.0.0:7761
    ensure_processes_running_srun "warp" 7761
fi

for this_obj_size in "${OBJ_SIZES[@]}"; do
    obj_size="$this_obj_size"
    output_dir="$OUTPUT_DIR"
    thread_list=("${THREAD_LIST[@]}")
    export obj_size output_dir thread_list

    # shellcheck disable=SC1091  # Sourced script path is dynamic
    source "$SCALE_TEST_BASE/lib/_warp_functions.sh"
    run_warp_io_sweep_iteration
done

# Stop all warp clients by killing the backgrounded srun process
# This will cause srun to cleanly tear down all warp processes on all nodes
if [[ "$SLURM_JOB_NUM_NODES" -gt 1 ]] && [[ -n "${SRUN_WARP_PID:-}" ]]; then
    stop_background_clients_srun "$SRUN_WARP_PID"
    # Fallback: ensure no warp processes remain (in case srun didn't clean up everything)
    ensure_no_processes_running_srun "warp"
fi
