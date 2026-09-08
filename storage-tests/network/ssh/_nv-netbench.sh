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
#
# SSH dispatcher for netbench tests.
# Starts elbencho services on all nodes, runs the remote scriptlet,
# and cleans up.

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

OUTPUT_DIR="$1"
bidirectional="$2"

# Extract datestamp identifier from end of the OUTPUT_DIR
DS="${OUTPUT_DIR##*-}"

if [ -z "$OUTPUT_DIR" ] || [ ! -d "$OUTPUT_DIR" ]; then
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
echo "Running netbench ($mode_desc) on ${SSH_NUM_NODES:-0} nodes"
echo "  NODELIST:        ${SSH_NODELIST:-}"
echo "  Threads:         ${NETBENCH_THREADS[*]}"
echo "  Iterations:      ${NETBENCH_ITERATIONS}"
echo "  Block Size:      ${NETBENCH_BLOCKSIZE}"
echo "  Response Size:   ${NETBENCH_RESPSIZE}"
echo "  NIC Speed:       ${NETBENCH_HOST_NIC_GBPS} Gbps"
echo "  Target Runtime:  ${NETBENCH_TARGET_RUNTIME} sec"
echo "  Port:            ${NETBENCH_PORT}"
echo "  Datestamp:       $DS"
echo "  OutputDir:       $OUTPUT_DIR"

# Create a temporary status directory
status_dir=$(mktemp -d) || { echo "Error: Unable to create temporary status directory" >&2; exit 1; }

# Helper function to start elbencho services on all nodes
# Arguments: $1 = port (optional), $2 = skip_kill ("true" to skip)
# Returns: 0 on success, 1 on failure
start_elbencho_services() {
    local port="${1:-}"
    local skip_kill="${2:-false}"
    local port_desc="${port:-default}"
    local spawn_output
    local -a pids

    echo "Starting elbencho services on port $port_desc..."

    # Scriptlet sources _elbencho_functions.sh and calls start_elbencho_service
    # Set ELBENCHO=./elbencho since the binary was copied to the remote host
    # shellcheck disable=SC2016  # Single quotes intentional: $1/$2 expand on remote host
    if ! spawn_output=$(spawn_N_ssh "$status_dir" true \
            'export ELBENCHO=./elbencho && source ./_elbencho_functions.sh && start_elbencho_service "$1" "$2"' "$port" "$skip_kill"); then
        echo "Error: Failed to start elbencho services (port $port_desc)" >&2
        echo "$spawn_output" >&2
        return 1
    fi

    read -ra pids <<< "$spawn_output"
    if [[ ${#pids[@]} -lt 1 ]]; then
        echo "Error: No PIDs returned for elbencho services (port $port_desc)" >&2
        return 1
    fi

    # Wait for SSH clients to exit (services remain running on remote nodes)
    gather_N_ssh "$status_dir" "" "${pids[@]}" >/dev/null 2>&1 || true

    # Validate SSH clients succeeded
    for pid in "${pids[@]}"; do
        local ssh_client_hostname
        local ssh_client_rc
        ssh_client_hostname=$(results_N_ssh_pid "$status_dir" "${pid}" "hostname" || true)
        ssh_client_rc=$(results_N_ssh_pid "$status_dir" "${pid}" "rc" || true)
        if [[ "$ssh_client_rc" -ne 0 ]]; then
            local ssh_client_stdout
            ssh_client_stdout=$(results_N_ssh_pid "$status_dir" "${pid}" "stdout" || true)
            echo "Error: Service start on $ssh_client_hostname failed (port $port_desc, rc=$ssh_client_rc)" >&2
            echo "$ssh_client_stdout" >&2
            return 1
        fi
    done

    sleep 2  # Give services a little more time to start
    return 0
}

# Start elbencho services on all nodes
# - Unidirectional: 1 service per node on NETBENCH_PORT
# - Bidirectional: 2 services per node (each coordinator uses different port)
if ! start_elbencho_services "$NETBENCH_PORT" "false"; then
    rm -rf "$status_dir"
    exit 1
fi

# For bidirectional mode, start secondary service on NETBENCH_PORT + 1
if [[ "$bidirectional" == "true" ]]; then
    secondary_port=$((NETBENCH_PORT + 1))
    if ! start_elbencho_services "$secondary_port" "true"; then
        rm -rf "$status_dir"
        exit 1
    fi
fi

output_dir_basename=$(basename "$OUTPUT_DIR")
output_dir_dirname=$(dirname "$OUTPUT_DIR")

# Build thread list as CSV string (comma-separated for robust passing through SSH)
threads_str=$(IFS=','; echo "${NETBENCH_THREADS[*]}")

# Run the remote scriptlet
run_ssh_single "${SSH_NODELIST%%,*}" "$status_dir/netbench.rc" "/dev/stdout" "" \
    "@${SCRIPT_DIR}/_nv-netbench-remote-scriptlet.sh" \
    "$OUTPUT_DIR" "$bidirectional" "$SSH_NODELIST" \
    "$NETBENCH_PORT" "$NETBENCH_BLOCKSIZE" "$NETBENCH_RESPSIZE" \
    "$NETBENCH_HOST_NIC_GBPS" "$NETBENCH_TARGET_RUNTIME" \
    "$NETBENCH_ITERATIONS" "$threads_str"

# Process exit status and retrieve files
netbench_rc_file="$status_dir/netbench.rc"
netbench_rc=$(cat "$netbench_rc_file" 2>/dev/null || echo "1")
if [[ "$netbench_rc" -ne 0 ]]; then
    echo "Error: Failed to run netbench (rc=$netbench_rc)" >&2
fi

# Copy results from remote
(
    if ! cd "$output_dir_dirname"; then
        echo "Error: Failed to cd into '$output_dir_dirname'" >&2
    else
        run_ssh_single "${SSH_NODELIST%%,*}" "$status_dir/tar.rc" "/dev/stdout" "" "" \
            tar -czf - "${output_dir_basename}" | tar -xzf -
        tar_rc=$(cat "$status_dir/tar.rc" 2>/dev/null || echo "1")
        if [[ "$tar_rc" -ne 0 ]]; then
            echo "Error: Failed to copy results (rc=$tar_rc)" >&2
        fi
    fi
)

# Stop elbencho services and watchdogs on all nodes
echo "Stopping elbencho services..."

# Stop primary elbencho services
# shellcheck disable=SC2016  # Single quotes intentional: $1 expands on remote host
spawn_output=$(spawn_N_ssh "$status_dir" true \
    'source ./_elbencho_functions.sh && stop_elbencho_service "$1"' "$NETBENCH_PORT") || true
mapfile -t stop_pids < <(echo "$spawn_output" | tr ' ' '\n')
gather_N_ssh "$status_dir" "" "${stop_pids[@]}" || true

# For bidirectional mode, also stop secondary services
if [[ "$bidirectional" == "true" ]]; then
    secondary_port=$((NETBENCH_PORT + 1))
    # shellcheck disable=SC2016
    spawn_output=$(spawn_N_ssh "$status_dir" true \
        'source ./_elbencho_functions.sh && stop_elbencho_service "$1"' "$secondary_port") || true
    mapfile -t stop_pids < <(echo "$spawn_output" | tr ' ' '\n')
    gather_N_ssh "$status_dir" "" "${stop_pids[@]}" || true
fi

# Cleanup
rm -rf "$status_dir"

exit 0
