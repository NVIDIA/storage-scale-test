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

# Create a temporary status directory
status_dir=$(mktemp -d) || { echo "Error: Unable to create temporary status directory" >&2; exit 1; }

# Spawn elbencho service on all selected nodes
# Scriptlet sources _elbencho_functions.sh and calls start_elbencho_service
# Set ELBENCHO=./elbencho since the binary was copied to the remote host
# shellcheck disable=SC2016  # Single quotes intentional: $1/$2 expand on remote host
if ! spawn_output=$(spawn_N_ssh "$status_dir" true \
        'export ELBENCHO=./elbencho && source ./_elbencho_functions.sh && start_elbencho_service "$1" "$2"' "" "false"); then
    echo "Error: Failed to start elbencho services over SSH" >&2
    echo "$spawn_output" >&2
    rm -rf "$status_dir"
    exit 1
fi

# Parse returned PIDs
declare -a service_pids=()
read -ra service_pids <<< "$spawn_output"
if [[ ${#service_pids[@]} -lt 1 ]]; then
    echo "Error: No PIDs returned when spawning elbencho services" >&2
    rm -rf "$status_dir"
    exit 1
fi

# Wait for SSH clients to exit (services remain running on remote nodes)
gather_N_ssh "$status_dir" "" "${service_pids[@]}" >/dev/null 2>&1 || true

# Validate SSH clients succeeded
for pid in "${service_pids[@]}"; do
    ssh_client_hostname=$(results_N_ssh_pid "$status_dir" "${pid}" "hostname" || true)
    if [[ -z "$ssh_client_hostname" ]]; then
        echo "Error: Failed to get hostname for PID $pid" >&2
        rm -rf "$status_dir"
        exit 1
    fi
    ssh_client_rc=$(results_N_ssh_pid "$status_dir" "${pid}" "rc" || true)
    if [[ "$ssh_client_rc" -ne 0 ]]; then
        ssh_client_stdout=$(results_N_ssh_pid "$status_dir" "${pid}" "stdout" || true)
        echo "Error: SSH client for $ssh_client_hostname failed (rc=$ssh_client_rc)" >&2
        echo "$ssh_client_stdout" >&2
        rm -rf "$status_dir"
        exit 1
    fi
done

sleep 2  # Give services a little more time to start

# Get test directories for this iteration
mapfile -t test_dirs < <(generate_fs_test_directories "mdtest-elbencho")
test_dirs_csv=$(IFS=,; echo "${test_dirs[*]}")

output_dir_basename=$(basename "$OUTPUT_DIR")
output_dir_dirname=$(dirname "$OUTPUT_DIR")

# Run the remote scriptlet
run_ssh_single "${SSH_NODELIST%%,*}" "$status_dir/mdtest-elbencho.rc" "/dev/stdout" "" \
    "@${SCRIPT_DIR}/_nv-mdtest-elbencho-remote-scriptlet.sh" \
    "$OUTPUT_DIR" "$tasks_per_node" "$SSH_NODELIST" "$test_dirs_csv" \
    "$MDTEST_BRANCH_FACTOR" "$MDTEST_ITEMS_PER_DIR" "$MDTEST_ITERATIONS" \
    "$single_dir_target_files" "$single_dir_files_per_worker"

# Process exit status and retrieve files
mdtest_rc_file="$status_dir/mdtest-elbencho.rc"
mdtest_rc=$(cat "$mdtest_rc_file" 2>/dev/null || echo "1")
if [[ "$mdtest_rc" -ne 0 ]]; then
    echo "Error: Failed to run mdtest-elbencho (rc=$mdtest_rc)" >&2
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
# shellcheck disable=SC2016  # Single quotes intentional: expand on remote host
if ! spawn_output=$(spawn_N_ssh "$status_dir" true \
        'source ./_elbencho_functions.sh && stop_elbencho_service'); then
    echo "Warning: Failed to stop elbencho services on some nodes" >&2
fi

# Gather and check results
mapfile -t stop_pids < <(echo "$spawn_output" | tr ' ' '\n')
gather_N_ssh "$status_dir" "" "${stop_pids[@]}" || true

# Cleanup
rm -rf "$status_dir"

exit 0

