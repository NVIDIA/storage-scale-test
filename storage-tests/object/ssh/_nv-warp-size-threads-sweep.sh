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

if ! source_output=$("$SHELL" -c ". '${SCRIPT_DIR}/../../../env.sh'" 2>&1); then
    printf "%s\n\nFailed to source env.sh; fix ^^^^^^^^^^\n" "$source_output"
    exit 1
fi

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/../../../env.sh"

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
PUT_DURATION="$WARP_PUT_DURATION"
PUT_MIN_FILES="$WARP_PUT_MIN_FILES_PER_CLIENT"
GET_MAX_TIME="$WARP_GET_DURATION"

echo "Running the size/threads sweep on ${SSH_NUM_NODES:-0} nodes for thread counts ${THREAD_LIST[*]}"
echo "  NODELIST:  ${SSH_NODELIST:-}"
echo "  Bucket:    $OBJ_BUCKET"
echo "  Threads:   ${THREAD_LIST[*]}"
echo "  ObjSizes:  ${OBJ_SIZES[*]}"
echo "  Put Duration: $PUT_DURATION"
echo "  Put Min Objs: $PUT_MIN_FILES"
echo "  Max Get Time: $GET_MAX_TIME"
echo "  Multipart: $multipart"
echo "  Ranged:    $ranged"
echo "  S3Express: $( [[ "$s3_express" == "true" ]] && echo "True" || echo "False" )"
echo "  RPS Budget: GET: ${WARP_RPS_BUDGET_GET:-(no limit)}, PUT: ${WARP_RPS_BUDGET_PUT:-(no limit)}"
echo "  Datestamp: $DS"
echo "  OutputDir: $OUTPUT_DIR"

# Start a background server on every node
# Create a temporary status directory to track background ssh jobs
status_dir=$(mktemp -d) || { echo "Error: Unable to create temporary status directory" >&2; exit 1; }

# Spawn warp service on all selected nodes; combine stdout/stderr
if ! spawn_output=$(spawn_N_ssh "$status_dir" true "" \
        "killall warp >/dev/null 2>&1 && sleep 2; ./warp client 0.0.0.0 >/dev/null 2>&1 &"); then
    echo "Error: Failed to start warp services over SSH" >&2
    echo "$spawn_output" >&2
    rm -rf "$status_dir"
    exit 1
fi

# Parse returned PIDs
declare -a service_pids=()
read -ra service_pids <<< "$spawn_output"
if [[ ${#service_pids[@]} -lt 1 ]]; then
    echo "Error: No PIDs returned when spawning warp services" >&2
    rm -rf "$status_dir"
    exit 1
fi

# Wait for all ssh client processes to exit (warp service processes
# on the remote nodes should remain running)
gather_N_ssh "$status_dir" "" "${service_pids[@]}" >/dev/null 2>&1 || true

# Make sure none of the ssh client spawning processes failed
for pid in "${service_pids[@]}"; do
    ssh_client_hostname=$(results_N_ssh_pid "$status_dir" "${pid}" "hostname" || true)
    if [[ -z "$ssh_client_hostname" ]]; then
        echo "Error: Failed to get hostname for PID $pid; $ssh_client_hostname" >&2
        rm -rf "$status_dir"
        exit 1
    fi
    ssh_client_rc=$(results_N_ssh_pid "$status_dir" "${pid}" "rc" || true)
    if [[ -z "$ssh_client_rc" ]]; then
        echo "Error: Failed to get rc for PID $pid; $ssh_client_rc" >&2
        rm -rf "$status_dir"
        exit 1
    fi
    if [[ "$ssh_client_rc" -ne 0 ]]; then
        ssh_client_stdout=$(results_N_ssh_pid "$status_dir" "${pid}" "stdout" || true)
        echo "Error: SSH client process for PID $pid on $ssh_client_hostname failed with rc $ssh_client_rc" >&2
        echo "$ssh_client_stdout" >&2
        rm -rf "$status_dir"
        exit 1
    fi
done

for this_obj_size in "${OBJ_SIZES[@]}"; do
    output_dir_basename=$(basename "$OUTPUT_DIR")
    output_dir_dirname=$(dirname "$OUTPUT_DIR")

    # Run the remote scriptlet
    run_ssh_single "${SSH_NODELIST%%,*}" "$status_dir/warp.rc" "/dev/stdout" "" \
        "@${SCRIPT_DIR}/_nv-warp-remote-scriptlet.sh" \
        "$OUTPUT_DIR" "$this_obj_size" "$PUT_DURATION" "$PUT_MIN_FILES" "$GET_MAX_TIME" \
        "$multipart" "$ranged" "$SSH_NODELIST" "${WARP_RPS_BUDGET_GET:-}" "${WARP_RPS_BUDGET_PUT:-}" \
        "$WARP_RANGE_OBJ_SIZE" "${WARP_PREFIXES:-}" \
        "$OBJ_BUCKET" "$OBJ_REGION" "$OBJ_HOST" "$OBJ_HOST_PORT" \
        "$s3_express" \
        "${THREAD_LIST[@]}"

    # Process the exit status and retrieve files from the remote output dir to the local one
    warp_rc_file="$status_dir/warp.rc"
    warp_rc=$(cat "$warp_rc_file" 2>/dev/null || echo "1")
    if [[ "$warp_rc" -ne 0 ]]; then
        echo "Error: Failed to run warp (rc=$warp_rc); you may need to clean them up manually." >&2
    fi

    (
        if ! cd "$output_dir_dirname"; then
            echo "Error: Failed to cd into '$output_dir_dirname'; not copying remote output dir." >&2
        else
            tar_rc_file="$status_dir/tar.rc"
            run_ssh_single "${SSH_NODELIST%%,*}" "$tar_rc_file" "/dev/stdout" "" "" \
                tar -czf - "${output_dir_basename}" | tar -xzf -
            tar_rc=$(cat "$tar_rc_file" 2>/dev/null || echo "1")
            if [[ "$tar_rc" -ne 0 ]]; then
                echo "Error: Failed to tar remote output dir (rc=$tar_rc); you may need to copy results manually." >&2
            fi
        fi
    )
done

# Kill all the warp clients; unlike elbencho, we can't just have a single warp
# command tell all the clients to exit.  So we have to do another spawn_N_ssh dance
# to run killall on all the client hosts.
if ! spawn_output=$(spawn_N_ssh "$status_dir" true "" "killall warp >/dev/null 2>&1"); then
    echo "Error: Failed to kill warp clients over SSH" >&2
    echo "$spawn_output" >&2
    rm -rf "$status_dir"
    exit 1
fi

# Parse returned PIDs
declare -a service_pids=()
read -ra service_pids <<< "$spawn_output"
if [[ ${#service_pids[@]} -lt 1 ]]; then
    echo "Error: No PIDs returned when spawning warp killall commands" >&2
    rm -rf "$status_dir"
    exit 1
fi

gather_N_ssh "$status_dir" "" "${service_pids[@]}" >/dev/null 2>&1 || true

# Warn if any of the killall commands failed
any_failed=0
for pid in "${service_pids[@]}"; do
    ssh_client_hostname=$(results_N_ssh_pid "$status_dir" "${pid}" "hostname" || true)
    if [[ -z "$ssh_client_hostname" ]]; then
        echo "Error: Failed to get hostname for PID $pid; $ssh_client_hostname" >&2
        any_failed=1
    fi
    ssh_client_rc=$(results_N_ssh_pid "$status_dir" "${pid}" "rc" || true)
    if [[ -z "$ssh_client_rc" ]]; then
        echo "Error: Failed to get rc for PID $pid; $ssh_client_rc" >&2
        any_failed=1
    fi
    if [[ "$ssh_client_rc" -ne 0 ]]; then
        ssh_client_stdout=$(results_N_ssh_pid "$status_dir" "${pid}" "stdout" || true)
        echo "Error: SSH client process for PID $pid on $ssh_client_hostname failed with rc $ssh_client_rc" >&2
        echo "$ssh_client_stdout" >&2
        any_failed=1
    fi
done

if [[ "$any_failed" -eq 1 ]]; then
    echo "Error: Failed to kill warp clients over SSH; you may need to clean them up manually." >&2
fi

rm -rf "$status_dir"

exit 0