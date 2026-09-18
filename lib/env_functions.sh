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

# shellcheck source=lib/_platform_functions.sh
# shellcheck disable=SC1091  # Resolved beside this library in the repository
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_platform_functions.sh"

# Parse SSH host file into an array of hostnames/IPs
# Usage:
#   mapfile -t my_hosts < <(parse_ssh_host_file "/path/to/hostfile")
#
# Note: mapfile is preferred over readarray (mapfile is the newer, more standard name)
# Both are equivalent - readarray is just an alias for mapfile in bash
parse_ssh_host_file() {
    local file="$1"

    if [[ ! -f "$file" ]]; then
        return 1
    fi

    while IFS= read -r line || [[ -n "$line" ]]; do
        # Skip empty lines and comments
        [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]] && continue

        # Split on spaces and commas, add non-empty entries
        IFS=' ,' read -ra hosts <<< "$line"
        for host in "${hosts[@]}"; do
            [[ -n "$host" ]] && printf '%s\n' "$host"
        done
    done < "$file"
}

# Run SSH command on a single host with output/error redirection
# Usage: run_ssh_single hostname exit_status_file stdout_file stderr_file scriptlet command [args...]
#
# Arguments:
#   hostname: hostname or IP to SSH to
#   exit_status_file: path where SSH exit status will be written
#   stdout_file: path where SSH stdout will be written
#   stderr_file: path where SSH stderr will be written (empty string = combine with stdout)
#   scriptlet: if non-empty, multi-line string to feed to stdin of ssh, with remaining args as bash
#              positional args
#       Special cases:
#          - If scriptlet starts with "@" and the rest is an existing file, that file
#            is redirected to stdin and the SSH command runs "/bin/bash -s" so the file
#            contents are executed as a script with positional args.
#          - If scriptlet starts with "-|" and the rest is an existing file, that file
#            is redirected to stdin but the SSH command is constructed normally (no
#            "/bin/bash -s").
#          For both special cases, if the file path does not exist, an error is written and
#          exit status 87 recorded.
#   command [args...]: command and arguments to execute via SSH
#
# This function preserves argument quoting and does not print anything itself.
run_ssh_single() {
    local hostname="$1"
    local exit_status_file="$2"
    local stdout_file="$3"
    local stderr_file="$4"
    local scriptlet="$5"
    shift 5

    # Build the SSH command with proper options
    local ssh_target
    if [[ -n "${SSH_USER:-}" ]]; then
        ssh_target="${SSH_USER}@${hostname}"
    else
        ssh_target="${hostname}"
    fi

    # Determine stdin mode and build SSH command
    local stdin_mode="none"  # one of: none, bash_string, bash_file, raw_file
    local stdin_file_path=""
    if [[ -n "$scriptlet" ]]; then
        if [[ "$scriptlet" == @* ]]; then
            stdin_mode="bash_file"
            stdin_file_path="${scriptlet#@}"
        elif [[ "$scriptlet" == -\|* ]]; then
            stdin_mode="raw_file"
            # Remove the leading "-|" while preserving any subsequent whitespace in the path
            stdin_file_path="${scriptlet:2}"
        else
            stdin_mode="bash_string"
        fi
    fi

    # Validate special-case file paths early
    if [[ "$stdin_mode" == "bash_file" || "$stdin_mode" == "raw_file" ]]; then
        if [[ ! -f "$stdin_file_path" ]]; then
            if [ -z "$stderr_file" ]; then
                printf 'Error: Scriptlet file not found: %s\n' "$stdin_file_path" >"$stdout_file" 2>/dev/null
            else
                printf 'Error: Scriptlet file not found: %s\n' "$stdin_file_path" >"$stderr_file" 2>/dev/null
            fi
            echo 87 > "$exit_status_file"
            return
        fi
    fi

    # Build SSH command according to stdin_mode
    local ssh_cmd
    if [[ "$stdin_mode" == "bash_string" || "$stdin_mode" == "bash_file" ]]; then
        ssh_cmd=(
            ssh
            -T
            -o ConnectTimeout=60
            -o StrictHostKeyChecking=no
            -o UserKnownHostsFile=/dev/null
            -o PreferredAuthentications=publickey
            -o LogLevel=ERROR
            "${ssh_target}"
            "/bin/bash" "-s" "--" "${@}"
        )
    else
        ssh_cmd=(
            ssh
            -T
            -o ConnectTimeout=60
            -o StrictHostKeyChecking=no
            -o UserKnownHostsFile=/dev/null
            -o PreferredAuthentications=publickey
            -o LogLevel=ERROR
            "${ssh_target}"
            "${@}"
        )
    fi

    # Replace empty string arguments with literal "" to preserve them through SSH
    local i
    for ((i=0; i<${#ssh_cmd[@]}; i++)); do
        if [[ -z "${ssh_cmd[i]}" ]]; then
            ssh_cmd[i]='""'
        fi
    done

    # Execute SSH with appropriate redirection and capture exit status
    local ssh_exit_status
    if [[ "$stdin_mode" != "none" ]]; then
        # Feed stdin to SSH process based on mode
        if [[ "$stdin_mode" == "bash_file" || "$stdin_mode" == "raw_file" ]]; then
            if [ -z "$stderr_file" ]; then
                "${ssh_cmd[@]}" < "$stdin_file_path" >"$stdout_file" 2>&1
                ssh_exit_status=$?
            else
                "${ssh_cmd[@]}" < "$stdin_file_path" >"$stdout_file" 2>"$stderr_file"
                ssh_exit_status=$?
            fi
        else
            # bash_string
            if [ -z "$stderr_file" ]; then
                printf '%s\n' "$scriptlet" | "${ssh_cmd[@]}" >"$stdout_file" 2>&1
                ssh_exit_status=$?
            else
                printf '%s\n' "$scriptlet" | "${ssh_cmd[@]}" >"$stdout_file" 2>"$stderr_file"
                ssh_exit_status=$?
            fi
        fi
    else
        # Normal execution without stdin. Explicitly close ssh's stdin via
        # /dev/null so the remote ssh process does NOT inherit (and silently
        # slurp) the calling shell's stdin -- which would break callers that
        # invoke run_ssh_single from inside a `while read … done < <(…)`
        # loop, where `read` is already consuming that stdin. All existing
        # empty-scriptlet callers run `tar -czf - <path>` or similar
        # filesystem-reading commands on the remote and never relied on
        # stdin propagation.
        if [ -z "$stderr_file" ]; then
            # Combine stdout and stderr to stdout file
            "${ssh_cmd[@]}" </dev/null >"$stdout_file" 2>&1
            ssh_exit_status=$?
        else
            # Separate stdout and stderr
            "${ssh_cmd[@]}" </dev/null >"$stdout_file" 2>"$stderr_file"
            ssh_exit_status=$?
        fi
    fi

    # Write exit status to file
    echo "$ssh_exit_status" > "$exit_status_file"
}

# Choose N SSH hosts from SSH_ALL_HOSTS and export as SSH_NODELIST/SSH_NUM_NODES
# Usage: choose_N_ssh_hosts node_count
choose_N_ssh_hosts() {
    local node_count="$1"

    # Validate SSH is enabled and hosts are available
    if [[ -z "${SSH_ENABLED:-}" ]]; then
        echo "Error: SSH is not enabled (SSH_ENABLED is empty)" >&2
        return 1
    fi

    if [[ ! "$node_count" =~ ^[0-9]+$ ]] || (( node_count < 1 )); then
        echo "Error: node_count must be a positive integer (got '$node_count')" >&2
        return 1
    fi

    if [[ ${#SSH_ALL_HOSTS[@]} -lt $node_count ]]; then
        echo "Error: SSH_ALL_HOSTS has ${#SSH_ALL_HOSTS[@]} hosts but $node_count requested" >&2
        return 1
    fi

    local selected_hosts=()
    if [[ -n "${ORDER_NODES_ENABLED:-}" ]]; then
        # Ordered mode: take the first N hosts from SSH_ALL_HOSTS
        selected_hosts=("${SSH_ALL_HOSTS[@]:0:$node_count}")
    else
        # Random mode: Fisher-Yates shuffle to select N hosts
        local temp_hosts=("${SSH_ALL_HOSTS[@]}")
        for ((i = 0; i < node_count; i++)); do
            local remaining=$((${#temp_hosts[@]} - i))
            local random_idx=$((RANDOM % remaining + i))
            selected_hosts+=("${temp_hosts[random_idx]}")
            local temp="${temp_hosts[i]}"
            temp_hosts[i]="${temp_hosts[random_idx]}"
            temp_hosts[random_idx]="$temp"
        done
    fi

    # Create and export comma-separated nodelist and node count
    local IFS=,
    SSH_NODELIST="${selected_hosts[*]}"
    SSH_NUM_NODES="${#selected_hosts[@]}"
    export SSH_NODELIST SSH_NUM_NODES
    return 0
}

# Spawn SSH commands on the selected SSH hosts and return process PIDs
# Usage: pids=($(spawn_N_ssh status_dir combine_stdout_stderr scriptlet command [args...]))
#
# Arguments:
#   node_count: number of nodes to run on
#   status_dir: directory for storing intermediate files
#   combine_stdout_stderr: if non-empty, combine stdout/stderr streams
#   scriptlet: if non-empty, multi-line string to feed to stdin of ssh, with remaining args as bash positional args
#   command [args...]: command and arguments to execute on each node
#
# Returns: space-separated list of process PIDs (empty if error)
spawn_N_ssh() {
    local status_dir="$1"
    local combine_stdout_stderr="$2"
    local scriptlet="$3"
    shift 3

    # Validate that SSH_NODELIST is set and non-empty
    if [[ -z "${SSH_NODELIST:-}" ]]; then
        echo "Error: SSH_NODELIST is not set or empty; call choose_N_ssh_hosts first" >&2
        return 1
    fi

    # Validate that status_dir exists and is writable
    if [[ ! -d "$status_dir" ]]; then
        echo "Error: status_dir '$status_dir' does not exist" >&2
        return 1
    fi
    if [[ ! -w "$status_dir" ]]; then
        echo "Error: status_dir '$status_dir' is not writable" >&2
        return 1
    fi

    local pids=()

    # Spawn SSH processes for each selected host
    local -a _ssh_hosts
    IFS=, read -r -a _ssh_hosts <<<"$SSH_NODELIST"
    for hostname in "${_ssh_hosts[@]}"; do
        local exit_status_file="$status_dir/${hostname}.rc"
        local stdout_file="$status_dir/${hostname}.stdout"
        local stderr_file=""

        # Set stderr file based on combine_stdout_stderr flag
        if [[ -z "$combine_stdout_stderr" ]]; then
            stderr_file="$status_dir/${hostname}.stderr"
        fi

        # Write out the expanded scriptlet and quoted arguments all on one line for debugging
        {
            printf '%q ' "$scriptlet"
            for arg in "$@"; do
                printf '%q ' "$arg"
            done
            printf '\n'
        } > "$status_dir/${hostname}.cmd"

        # Spawn run_ssh_single in background and capture PID
        run_ssh_single "$hostname" "$exit_status_file" "$stdout_file" "$stderr_file" "$scriptlet" "$@" &
        local pid="$!"
        pids+=("$pid")

        # Create PID-to-hostname mapping file
        touch "$status_dir/${pid}.${hostname}"
    done

    # Return space-separated list of PIDs
    printf '%s\n' "${pids[*]}"
}

# Gather results from SSH processes spawned by spawn_N_ssh
# Usage: gather_N_ssh status_dir print_status pid1 pid2 pid3...
#
# Arguments:
#   status_dir: directory containing SSH result files
#   print_status: if non-empty, show progress updates
#   pids...: process PIDs to wait for
#
# Creates empty <hostname>.done files when each SSH process completes
# The .done file serves as a marker that the process has finished (exit status is in <hostname>.rc)
# Returns: 0 if successfully waited for all PIDs to exit, 1 on error (bad parameters, missing files, etc.)
gather_N_ssh() {
    local status_dir="$1"
    local print_status="$2"
    shift 2
    local input_pids=("$@")

    # Validate status_dir exists
    if [[ ! -d "$status_dir" ]]; then
        echo "Error: status_dir '$status_dir' does not exist" >&2
        return 1
    fi

    # Create PID-to-hostname mapping and validate all PIDs have mapping files
    declare -A pid_to_hostname
    for pid in "${input_pids[@]}"; do
        local mapping_file
        mapping_file=$(find "$status_dir" -maxdepth 1 -name "${pid}.*" 2>/dev/null | head -1)
        if [[ -z "$mapping_file" ]]; then
            echo "Error: No mapping file found for PID $pid in $status_dir" >&2
            return 1
        fi
        # Extract hostname from mapping file name of the form "$status_dir/<pid>.<hostname>"
        local _basename="${mapping_file##*/}"
        local hostname="${_basename#*.}"
        pid_to_hostname["$pid"]="$hostname"
    done

    # Track remaining PIDs to wait for
    local remaining_pids=("${input_pids[@]}")
    local total_pids=${#input_pids[@]}
    local exited_count=0

    # Wait for each PID to exit
    # wait -n blocks until at least
    # one child exits, then we scan to find (and reap) all that finished.
    while [[ ${#remaining_pids[@]} -gt 0 ]]; do
        # Block until at least one child in the list exits
        wait -n "${remaining_pids[@]}" 2>/dev/null || true

        # Scan all remaining PIDs; reap every one that has exited
        local new_remaining_pids=()
        for pid in "${remaining_pids[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                new_remaining_pids+=("$pid")
            else
                # Reap the child (prevents zombies)
                wait "$pid" 2>/dev/null || true
                ((++exited_count))

                local exited_hostname="${pid_to_hostname[$pid]}"
                touch "$status_dir/${exited_hostname}.done"

                if [[ -n "$print_status" ]]; then
                    echo "${exited_count}/${total_pids}"
                fi
            fi
        done
        remaining_pids=("${new_remaining_pids[@]}")
    done

    # Return success - we successfully waited for all PIDs to exit
    return 0
}


# Symbolic names for the `item` arg accepted by results_N_ssh_pid (and any
# caller that wants to ask for an SSH result by category). Using constants
# avoids "hostname" / "rc" / etc. as bare string literals across the file.
readonly _SSH_RESULT_HOSTNAME="hostname"

# Get SSH result for a specific PID and item from status directory files
# Usage: results_N_ssh_pid status_dir pid item
#
# Arguments:
#   status_dir: directory containing SSH result files
#   pid: process PID to get results for
#   item: which item to return (_SSH_RESULT_HOSTNAME, "rc", "stdout", "stderr")
#
# Outputs the requested item (may contain newlines)
# Returns: 0 on success, 1 on error
results_N_ssh_pid() {
    local status_dir="$1"
    local pid="$2"
    local item="$3"

    # Validate arguments
    if [[ -z "$item" ]]; then
        echo "Error: item parameter required (hostname, rc, stdout, stderr)" >&2
        return 1
    fi

    # Validate status_dir exists
    if [[ ! -d "$status_dir" ]]; then
        echo "Error: status_dir '$status_dir' does not exist" >&2
        return 1
    fi

    # Find the hostname mapping file for this PID
    local mapping_file
    mapping_file=$(find "$status_dir" -maxdepth 1 -name "${pid}.*" 2>/dev/null | head -1)
    if [[ -z "$mapping_file" ]]; then
        echo "Error: No mapping file found for PID $pid in $status_dir" >&2
        return 1
    fi

    # Extract hostname from mapping file
    # Extract hostname from mapping file name of the form "$status_dir/<pid>.<hostname>"
    local _basename="${mapping_file##*/}"
    local hostname="${_basename#*.}"

    # Return the requested item
    case "$item" in
        "$_SSH_RESULT_HOSTNAME")
            printf '%s' "$hostname"
            ;;
        "rc")
            local rc_file="$status_dir/${hostname}.rc"
            if [[ ! -f "$rc_file" ]]; then
                echo "Error: RC file $rc_file does not exist" >&2
                return 1
            fi
            cat "$rc_file"
            ;;
        "stdout")
            local stdout_file="$status_dir/${hostname}.stdout"
            if [[ ! -f "$stdout_file" ]]; then
                echo "Error: stdout file $stdout_file does not exist" >&2
                return 1
            fi
            cat "$stdout_file"
            ;;
        "stderr")
            local stderr_file="$status_dir/${hostname}.stderr"
            if [[ -f "$stderr_file" ]]; then
                cat "$stderr_file"
            fi
            # Note: No error if stderr file doesn't exist (streams may be combined)
            ;;
        "cmd")
            local cmd_file="$status_dir/${hostname}.cmd"
            if [[ ! -f "$cmd_file" ]]; then
                echo "Error: cmd file $cmd_file does not exist" >&2
                return 1
            fi
            cat "$cmd_file"
            ;;
        *)
            echo "Error: Invalid item '$item'. Must be one of: hostname, rc, stdout, stderr, cmd" >&2
            return 1
            ;;
    esac

    return 0
}

# Function to add option if variable is non-empty
add_option() {
    local var=$1
    local flag=$2
    local value=$3
    if [ -n "$value" ]; then
        echo "$var $flag $value"
    else
        echo "$var"
    fi
}

module_exists() {
    module avail "$1" 2>&1 | grep -q "^$1"
}

# Tail an array of logfile names, robustly, until a slurm JOBID has
# transitioned into a terminal state.  Return 0 on COMPLETED or 1 otherwise.
# The final sacct information is printed after the terminal state has been
# reached, and the tail killed and waited upon.
#
# Call this function like:
#   tail_until_complete "$JOBID" "${log_files[@]}"
#
tail_until_complete() {
    local JOBID=$1
    shift
    local log_files=("$@")  # More reliable array handling
    local terminal_rc

    # the "-n" is so we don't miss many lines that got into the file
    # before tail noticed the file became available.
    tail -n 1024 -F "${log_files[@]}" &
    local TAIL_PID=$!

    while true; do
        local job_rows
        local main_state
        job_rows=$(sacct -j "$JOBID" --format=JobIDRaw,State --noheader --parsable2)
        main_state=$(
            awk -F'|' -v job_id="$JOBID" '$1 == job_id { print $2; exit }' <<<"$job_rows"
        )

        case "$main_state" in
            COMPLETED*)
                terminal_rc=0
                ;;
            BOOT_FAIL*|CANCELLED*|DEADLINE*|FAILED*|NODE_FAIL*|OUT_OF_MEMORY*|PREEMPTED*|REVOKED*|SPECIAL_EXIT*|TIMEOUT*)
                terminal_rc=1
                ;;
            *)
                # No main allocation row yet, or the allocation is still in a
                # non-terminal state. Child step rows must not decide success.
                sleep 15
                continue
                ;;
        esac

        # Give writers and tail's internal polling a short grace period before
        # killing the tail process, so final log lines are less likely to be
        # missed after the job reaches a terminal state.
        sleep 10

        kill $TAIL_PID
        wait $TAIL_PID 2>/dev/null

        sacct -j "$JOBID" --format=JobID,State,ExitCode,JobName%12,NodeList%20

        return "$terminal_rc"
    done
}

# Monitor array of Slurm array jobs until completion
# Args: Array of job IDs to monitor
# Returns: 0 on success, 1 on failure
#
# Usage example:
# jobs=(12345 12346 12347)
# monitor_array_jobs "${jobs[@]}"
monitor_array_jobs() {
    local -a job_ids=("$@")
    local count=0
    local PRINT_EVERY=${PRINT_EVERY:-10}
    local force_print=false

    while true; do
        local all_complete=true
        local any_failed=false

        for jobid in "${job_ids[@]}"; do
            local total_tasks=0
            local completed_tasks=0
            local job_state="RUNNING"

            # Get job info, only main job steps (not .batch/.extern)
            local job_info
            job_info=$(sacct -j "$jobid" --format=JobID,State --noheader --parsable2 | grep -E '^[0-9]+(_[0-9]+|_\[[0-9-]+\])\|' || true)

            if [ -z "$job_info" ]; then
                job_state="SUBMITTED"
                all_complete=false
            else
                # Check for array range notation (pre-execution state)
                if echo "$job_info" | grep -q '_\[[0-9-]*\]'; then
                    total_tasks=$(echo "$job_info" | perl -ne 'if(/_\[(\d+)-(\d+)\]/) {print $2-$1+1}')
                    job_state="PENDING"
                    all_complete=false
                else
                    # Parse individual task states
                    while IFS='|' read -r jobid_part state; do
                        # Only count main job steps (ignore .batch, .extern)
                        if [[ $jobid_part =~ _[0-9]+$ ]]; then
                            ((total_tasks++))

                            # Use partial matching for states instead of exact matching
                            if [[ "$state" == "COMPLETED" ]]; then
                                ((completed_tasks++))
                            elif [[ "$state" =~ ^(FAILED|TIMEOUT|CANCELLED|NODE_FAIL|PREEMPTED) ]]; then
                                any_failed=true
                                job_state="FAILED"
                            elif [[ "$state" =~ ^(PENDING|CONFIGURING|RUNNING|SUSPENDED|REQUEUED|RESIZING) ]]; then
                                all_complete=false
                            else
                                # For any unrecognized state, consider it non-complete
                                all_complete=false
                                echo "Warning: Unrecognized job state: $state for job $jobid_part" >&2
                            fi
                        fi
                    done <<< "$job_info"

                    # Set final state if all tasks completed
                    if [ "$completed_tasks" -eq "$total_tasks" ]; then
                        job_state="COMPLETED"
                    fi
                fi
            fi

            # Print status on interval or if forced
            if (( count % PRINT_EVERY == 0 )) || $force_print; then
                echo "JOBID: ${jobid} ${job_state} (${completed_tasks}/${total_tasks})"
            fi
        done

        # Force a final status print before exit
        if $any_failed || $all_complete; then
            if ! $force_print; then
                force_print=true
                continue
            fi
            if $any_failed; then
                echo "Error: One or more array jobs failed" >&2
                return 1
            fi
            return 0
        fi

        sleep 1
        ((count++))
        force_print=false
    done
}

# for loops between 1 and total_nodes, incrementing by increment_by,
# but which we want to always execute for 1 and total_nodes, we can
# use a while loop using this function like so:
#
#    nodes=1
#    while [ "$nodes" -le "$total_nodes" ]; do
#        # ...
#        next_nodes=$(next_node_in_sequence "$nodes" "$total_nodes" "$increment_by") || break
#        nodes=$next_nodes
#    done
#
next_node_in_sequence() {
    local nodes=$1
    local total_nodes=$2
    local increment_by=$3

    if [ "$nodes" -eq "$total_nodes" ]; then
        return 1  # Signal we're done
    elif [ "$nodes" -eq 1 ]; then
        if [ "$increment_by" -ge "$total_nodes" ]; then
            echo "$total_nodes"
        elif [ "$increment_by" -eq 1 ]; then
            echo "2"
        else
            echo "$increment_by"
        fi
    elif [ "$((nodes + increment_by))" -gt "$total_nodes" ]; then
        echo "$total_nodes"
    else
        echo "$((nodes + increment_by))"
    fi
    return 0  # Signal to continue (not break out of while loop)
}

error_exit() {
    echo "Error: $1" >&2
    exit 1
}

# Example usage:
#  python_path=$(setup_python_venv) || exit 1
#  python_path=$(setup_python_venv /path/to/requirements.txt) || exit 1
#  ...
#  "$python_path" ./my_script.py
setup_python_venv() {
    local requirements_file="${1:-${SCALE_TEST_BASE:?}/requirements.txt}"
    local venv_dir="${SCALE_TEST_BASE:?}/.venv"
    local stamp_file="${venv_dir}/.requirements.sha256"
    local req_hash

    type -P python3 >/dev/null 2>&1 || error_exit "python3 not found"

    if [[ ! -f "$requirements_file" ]]; then
        error_exit "Requirements file not found: ${requirements_file}"
    fi

    if [ ! -d "$venv_dir" ]; then
        echo "Creating virtual environment..." >&2
        python3 -m venv "$venv_dir" >&2 || error_exit "Failed to create virtual environment"
    fi

    # shellcheck disable=SC1091
    source "$venv_dir/bin/activate" >&2 || error_exit "Failed to activate virtual environment"

    req_hash=$(python3 -c "import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())" "$requirements_file") || error_exit "Failed to hash requirements file"

    if [[ ! -f "$stamp_file" ]] || [[ "$(<"$stamp_file")" != "$req_hash" ]]; then
        echo "Installing required packages from ${requirements_file}..." >&2
        pip install -r "$requirements_file" >&2 || error_exit "Failed to install required packages"
        printf '%s\n' "$req_hash" >"$stamp_file"
    fi

    printf "%s\n" "${venv_dir}/bin/python"
}

# Generate a list of test directories based on TEST_DIRS associative array
# Each key in TEST_DIRS will have N directories, where N is the value for that key
# Returns a newline-separated list of directories
generate_fs_test_directories() {
    local test_dir_segment="${1:-fs-test}"

    # Validate that TEST_DIRS is defined and has at least one key
    # TEST_DIRS is an associative array defined elsewhere in the codebase
    # shellcheck disable=SC2153
    if [[ -z "${TEST_DIRS[*]}" ]] || [[ ${#TEST_DIRS[@]} -eq 0 ]]; then
        echo "Error: TEST_DIRS is not defined or empty" >&2
        return 1
    fi

    # Suffix: explicit override when set; else SLURM job id when under Slurm;
    # else run datestamp (DS) when set (non-Slurm sweeps).
    local suffix=""
    if [[ -n "${FS_TEST_DIR_SUFFIX_OVERRIDE:-}" ]]; then
        suffix="$FS_TEST_DIR_SUFFIX_OVERRIDE"
    elif [[ -n "${SLURM_JOB_ID:-}" ]]; then
        suffix="-${SLURM_JOB_ID}"
    elif [[ -n "${DS:-}" ]]; then
        suffix="-${DS}"
    fi

    # Iterate through each key in TEST_DIRS
    for fs_path in "${!TEST_DIRS[@]}"; do
        local weight=${TEST_DIRS[$fs_path]}

        # Generate 'weight' number of directories for this filesystem path
        for ((i=1; i<=weight; i++)); do
            local dir_path="${fs_path}/${test_dir_segment}-target-${i}${suffix}"
            echo "${dir_path}"
        done
    done
}

# Create directories locally with mkdir -p for a variable number of paths
# Usage: mkdir_fs_test_directories_local path1 path2 ...
mkdir_fs_test_directories_local() {
    # If no arguments, nothing to do
    if [[ $# -eq 0 ]]; then
        return 0
    fi

    # Use NUL-delimited streaming to handle very large argument lists and spaces
    # xargs will batch to respect ARG_MAX automatically
    if ! printf '%s\0' "$@" | xargs -0 -n 200 mkdir -p; then
        echo "Error: mkdir -p failed for one or more directories" >&2
        return 1
    fi
}

# Copy a file over SSH using spawn_N_ssh and gather_N_ssh
# Usage: copy_a_file_over_ssh status_dir file_path remote_cat_command
# Arguments:
#   status_dir: directory for storing intermediate files
#   file_path: path to the file to copy
#   remote_cat_command: command to run on the remote node to cat the file
# Returns 0 if all SSH commands exited 0, otherwise 1
# Assumes choose_N_ssh_hosts has already been called
copy_a_file_over_ssh() {
    local status_dir="$1"
    local file_path="$2"
    local remote_cat_command="$3"
    local file_basename
    file_basename=$(basename "$file_path")

    local spawn_output
    if ! spawn_output=$(spawn_N_ssh "$status_dir" true "-|$file_path" "$remote_cat_command" 2>&1); then
        echo "Error: spawn_N_ssh failed for $file_basename copy." >&2
        echo "Command: spawn_N_ssh $status_dir true '-|$file_path' '$remote_cat_command'" >&2
        if [[ -n "$spawn_output" ]]; then
            echo "Output:" >&2
            echo "$spawn_output" >&2
        fi
        return 1
    fi

    # Parse PIDs
    local -a pids=()
    read -ra pids <<< "$spawn_output"
    if [[ ${#pids[@]} -lt 1 ]]; then
        echo "Error: spawn_N_ssh returned no PIDs; output follows:" >&2
        echo "$spawn_output" >&2
        return 1
    fi

    # Wait for completion
    local gather_output
    if ! gather_output=$(gather_N_ssh "$status_dir" "" "${pids[@]}" 2>&1); then
        echo "Error: gather_N_ssh failed while waiting for $file_basename copy to complete." >&2
        if [[ -n "$gather_output" ]]; then
            echo "Output:" >&2
            echo "$gather_output" >&2
        fi
        return 1
    fi

    # Inspect exit status for each PID
    local any_failed=0
    local pid rc hostname stdout_content
    for pid in "${pids[@]}"; do
        rc=$(results_N_ssh_pid "$status_dir" "$pid" "rc") || rc=1
        if [[ "$rc" != "0" ]]; then
            any_failed=1
            hostname=$(results_N_ssh_pid "$status_dir" "$pid" "$_SSH_RESULT_HOSTNAME" 2>/dev/null || true)
            echo "Error: SSH copy of $file_basename failed on host '${hostname:-unknown}' (pid=$pid) with rc=$rc" >&2
            cmd_content=$(results_N_ssh_pid "$status_dir" "$pid" "cmd" 2>/dev/null || true)
            if [[ -n "$cmd_content" ]]; then
                echo "--- cmd (${hostname:-unknown}) ---" >&2
                echo "$cmd_content" >&2
            fi
            stdout_content=$(results_N_ssh_pid "$status_dir" "$pid" "stdout" 2>/dev/null || true)
            if [[ -n "$stdout_content" ]]; then
                echo "--- stdout (${hostname:-unknown}) ---" >&2
                echo "$stdout_content" >&2
            fi
        fi
    done

    return $any_failed
}

# Ensure the elbencho binary is present on required SSH nodes by copying it via stdin piping
# - If SSH_HOMEDIR_SHARED is non-empty: copy to 1 node
# - Otherwise: copy to all nodes in SSH_ALL_HOSTS
# Returns 0 if all SSH commands exited 0, otherwise 1
ensure_all_ssh_nodes_can_elbencho() {
    # Validate ELBENCHO path
    if [[ -z "${ELBENCHO:-}" || ! -f "$ELBENCHO" ]]; then
        echo "Error: ELBENCHO is not set to a readable file: '$ELBENCHO'" >&2
        return 1
    fi

    # Determine node count
    local node_count
    if [[ -n "${SSH_HOMEDIR_SHARED:-}" ]]; then
        node_count=1
    else
        node_count=${#SSH_ALL_HOSTS[@]}
    fi

    # Require at least one node
    if [[ -z "$node_count" || "$node_count" -lt 1 ]]; then
        echo "Error: No SSH hosts available to copy elbencho to (node_count=$node_count)" >&2
        return 1
    fi

    # Create a temporary status directory
    local status_dir
    status_dir=$(mktemp -d) || return 1

    # Spawn the SSH copy operations; combine stdout/stderr to simplify handling
    choose_N_ssh_hosts "$node_count"
    # Kill watchdog first (using fuser on log file), then elbencho, wait, then copy
    # The watchdog would otherwise restart elbencho immediately after we kill it
    # Note: We can't use "pkill -f elbencho_watchdog" here because it would match the
    # SSH session's own shell (which has the command in its command line) and kill itself!
    # Instead, we use fuser on the watchdog log file, which safely identifies only the watchdog.
    local cmd='fuser -k /tmp/elbencho_service_watchdog.log 2>/dev/null; pkill -9 elbencho 2>/dev/null; killall -9 elbencho 2>/dev/null; sleep 1; cp /dev/stdin elbencho && chmod a+x elbencho'

    if ! copy_a_file_over_ssh "$status_dir" "$ELBENCHO" "$cmd"; then
        echo "Error: Failed to copy elbencho to all nodes" >&2
        rm -rf "$status_dir"
        return 1
    fi

    if ! copy_a_file_over_ssh "$status_dir" "${SCALE_TEST_BASE}/lib/_platform_functions.sh" "cp /dev/stdin _platform_functions.sh"; then
        echo "Error: Failed to copy platform functions library to all nodes" >&2
        rm -rf "$status_dir"
        return 1
    fi

    if ! copy_a_file_over_ssh "$status_dir" "${SCALE_TEST_BASE}/lib/_elbencho_functions.sh" "cp /dev/stdin _elbencho_functions.sh"; then
        echo "Error: Failed to copy elbencho functions library to all nodes" >&2
        rm -rf "$status_dir"
        return 1
    fi

    if ! copy_a_file_over_ssh "$status_dir" "${SCALE_TEST_BASE}/lib/_netbench_functions.sh" "cp /dev/stdin _netbench_functions.sh"; then
        echo "Error: Failed to copy netbench functions library to all nodes" >&2
        rm -rf "$status_dir"
        return 1
    fi

    rm -rf "$status_dir"
    return 0
}

# Deploy the s3test binary on all nodes previously selected with choose_N_ssh_hosts.
# Strategy: if the C source file is available, pipe it to the remote host and compile
# there (build-from-source). Otherwise, copy the pre-built static binary ($S3TEST).
# The tarball excludes utils/build/*.c but includes the pre-built static binary.
build_s3test_on_selected_ssh_nodes() {
    local s3test_source="${SCALE_TEST_BASE}/utils/build/s3-test.c"

    if [[ -f "$s3test_source" ]]; then
        _build_s3test_from_source "$s3test_source"
    elif [[ -n "${S3TEST:-}" && -f "$S3TEST" ]]; then
        _copy_s3test_binary "$S3TEST"
    else
        echo "Error: Neither s3-test.c source nor pre-built s3test binary found." >&2
        echo "  Looked for source at: $s3test_source" >&2
        echo "  S3TEST variable: ${S3TEST:-<unset>}" >&2
        return 1
    fi
}

# Build s3test from source on remote nodes via SSH
_build_s3test_from_source() {
    local s3test_source="$1"
    local status_dir
    status_dir=$(mktemp -d) || return 1

    local spawn_output
    if ! spawn_output=$(spawn_N_ssh "$status_dir" true "-|${s3test_source}" \
            "test -f ./s3test || gcc -o ./s3test -x c - -lssl -lcrypto" 2>&1); then
        echo "Error: spawn_N_ssh failed for s3test build." >&2
        echo "Command: spawn_N_ssh $status_dir true '-|${s3test_source}' 'test -f ./s3test || gcc -o ./s3test -x c - -lssl -lcrypto'" >&2
        if [[ -n "$spawn_output" ]]; then
            echo "Output:" >&2
            echo "$spawn_output" >&2
        fi
        rm -rf "$status_dir"
        return 1
    fi

    _wait_for_s3test_ssh "$status_dir" "$spawn_output" "build"
}

# Copy the pre-built s3test binary to remote nodes via SSH
_copy_s3test_binary() {
    local binary_path="$1"
    local status_dir
    status_dir=$(mktemp -d) || return 1

    if ! copy_a_file_over_ssh "$status_dir" "$binary_path" \
            "cp /dev/stdin s3test && chmod a+x s3test"; then
        echo "Error: Failed to copy pre-built s3test to remote nodes" >&2
        rm -rf "$status_dir"
        return 1
    fi

    rm -rf "$status_dir"
    return 0
}

# Wait for s3test SSH operations and report errors
_wait_for_s3test_ssh() {
    local status_dir="$1"
    local spawn_output="$2"
    local operation="$3"  # "build" for descriptive error messages

    local -a pids=()
    read -ra pids <<< "$spawn_output"
    if [[ ${#pids[@]} -lt 1 ]]; then
        echo "Error: spawn_N_ssh returned no PIDs; output follows:" >&2
        echo "$spawn_output" >&2
        rm -rf "$status_dir"
        return 1
    fi

    local gather_output
    if ! gather_output=$(gather_N_ssh "$status_dir" "" "${pids[@]}" 2>&1); then
        echo "Error: gather_N_ssh failed while waiting for s3test ${operation} to complete." >&2
        if [[ -n "$gather_output" ]]; then
            echo "Output:" >&2
            echo "$gather_output" >&2
        fi
        rm -rf "$status_dir"
        return 1
    fi

    local any_failed=0
    local pid rc hostname stdout_content
    for pid in "${pids[@]}"; do
        rc=$(results_N_ssh_pid "$status_dir" "$pid" "rc") || rc=1
        if [[ "$rc" != "0" ]]; then
            any_failed=1
            hostname=$(results_N_ssh_pid "$status_dir" "$pid" "$_SSH_RESULT_HOSTNAME" 2>/dev/null || true)
            echo "Error: SSH s3test ${operation} failed on host '${hostname:-unknown}' (pid=$pid) with rc=$rc" >&2
            cmd_content=$(results_N_ssh_pid "$status_dir" "$pid" "cmd" 2>/dev/null || true)
            if [[ -n "$cmd_content" ]]; then
                echo "--- cmd (${hostname:-unknown}) ---" >&2
                echo "$cmd_content" >&2
            fi
            stdout_content=$(results_N_ssh_pid "$status_dir" "$pid" "stdout" 2>/dev/null || true)
            if [[ -n "$stdout_content" ]]; then
                echo "--- stdout (${hostname:-unknown}) ---" >&2
                echo "$stdout_content" >&2
            fi
        fi
    done

    rm -rf "$status_dir"

    if [[ $any_failed -eq 0 ]]; then
        return 0
    fi
    return 1
}

# Print a summary of the elbencho sweep parameters
# Usage: print_elbencho_sweep_summary node_count nodelist dio_or_bio use_random force_single ds output_dir [job_id]
# Arguments:
#   node_count: number of nodes in the sweep
#   nodelist: comma-separated list of node names
#   dio_or_bio: "dio" or "bio"
#   use_random: "1" for random IO, "0" for sequential
#   force_single: "1" to force single combined run, "0" otherwise
#   ds: datestamp identifier
#   output_dir: output directory path
#   job_id: (optional) SLURM job ID
# Also uses exported ELBENCHO_SWEEP_READ_FROM / ELBENCHO_SWEEP_WRITE_ONLY /
# ELBENCHO_SWEEP_WRITE_NO_READ (set by sweep runners) to omit write-only fields for read-from,
# omit read fields for write-only / write-no-read, emit Read-from: path,
# and relabel duration / IO pattern lines.
print_elbencho_sweep_summary() {
    local node_count="$1"
    local nodelist="$2"
    local dio_or_bio="$3"
    local use_random="$4"
    local force_single="$5"
    local ds="$6"
    local output_dir="$7"
    local job_id="${8:-}"
    local sweep_read_from="${ELBENCHO_SWEEP_READ_FROM:-}"
    local sweep_write_only="${ELBENCHO_SWEEP_WRITE_ONLY:-0}"
    local sweep_write_no_read="${ELBENCHO_SWEEP_WRITE_NO_READ:-0}"
    local sweep_writes_without_read="0"
    if [[ "$sweep_write_only" == "1" || "$sweep_write_no_read" == "1" ]]; then
        sweep_writes_without_read="1"
    fi

    echo "Running the io-size/threads sweep on ${node_count} nodes for thread counts ${ELBENCHO_SCALE_THREAD_LIST[*]}"
    echo "  NODELIST:        ${nodelist}"
    # shellcheck disable=SC2153
    echo "  TEST_DIRS:       ${!TEST_DIRS[*]}"
    echo "  Threads:         ${ELBENCHO_SCALE_THREAD_LIST[*]}"
    echo "  IO Depths:       ${ELBENCHO_IODEPTH_LIST[*]}"
    echo "  IO Sizes:        ${ELBENCHO_SCALE_IO_SIZES[*]}"
    if [[ -n "$sweep_read_from" ]]; then
        echo "  Read-from:       ${sweep_read_from}"
        echo "  Read IO Duration: $ELBENCHO_SCALE_READ_WRITE_DURATION"
        if [[ "${ELBENCHO_SINGLE_BIG_FILE:-0}" != "1" ]]; then
            echo "  Cached treefile (unlink to rescan): ${sweep_read_from}/.storage-scale-test-elbencho-treefile.txt"
        fi
    else
        echo "  File Size Mult:  ${ELBENCHO_FILE_SIZE_MULTIPLIER:-1024}"
        if [[ "$sweep_writes_without_read" == "1" ]]; then
            echo "  Write IO Duration: $ELBENCHO_SCALE_READ_WRITE_DURATION"
        else
            echo "  W/R IO Duration: $ELBENCHO_SCALE_READ_WRITE_DURATION"
        fi
    fi
    if [[ "$sweep_write_no_read" == "1" ]]; then
        echo "  Write-no-read:   Yes"
    fi
    if [ "$dio_or_bio" = "dio" ]; then
        echo "  IO Type:         DIO"
    elif [ "$dio_or_bio" = "bio" ]; then
        echo "  IO Type:         BIO"
    fi
    local io_pattern
    if [[ -n "$sweep_read_from" ]]; then
        if [ "$use_random" = "1" ]; then
            io_pattern="Random (reads)"
        else
            io_pattern="Sequential for reads (unless overridden in IO Sizes)"
        fi
    elif [[ "$sweep_writes_without_read" == "1" ]]; then
        if [[ "$use_random" = "1" ]]; then
            io_pattern="Random (writes)"
        else
            io_pattern="Sequential for writes (unless overridden in IO Sizes)"
        fi
    elif [[ "$use_random" = "1" ]]; then
        io_pattern="Random"
    else
        io_pattern="Sequential (unless overridden in IO Sizes)"
    fi
    echo "  IO Pattern:      $io_pattern"
    echo "  Force Single:    $([ "$force_single" = "1" ] && echo "Yes" || echo "No")"
    if [ -n "$job_id" ]; then
        echo "  JobID:           $job_id"
    fi
    echo "  Datestamp:       $ds"
    echo "  OutputDir:       $output_dir"
}

# Join positional arguments with a separator. Prints the joined string.
# Usage: _join_with SEP a b c   ->   "a, b, c"  (when SEP=", ")
_join_with() {
    local sep="$1"
    shift
    [[ $# -eq 0 ]] && return 0
    local first="$1"
    printf '%s' "$first"
    shift
    local v
    for v in "$@"; do
        printf '%s%s' "$sep" "$v"
    done
    return 0
}

# Print a 3-line compact sweep summary in the style of summarize-elbencho.py.
# Useful for: NNNN.sh forensic header, plus a "what's about to run" banner the
# coordinator and SSH dispatcher print before iterating executions.
#
# Usage: print_elbencho_sweep_compact_summary <DS> <nodes_spec> [line_prefix]
#
# Reads from scope (any of: env vars set by env.sh / env_used.sh, or dynamically-
# scoped locals from a caller like reify_elbencho_execution):
#   $dio_or_bio, $rand_option, ELBENCHO_SCALE_IO_SIZES[@],
#   ELBENCHO_SCALE_THREAD_LIST[@], ELBENCHO_IODEPTH_LIST[@],
#   $ELBENCHO_FILE_SIZE_MULTIPLIER, $ELBENCHO_SINGLE_BIG_FILE,
#   $ELBENCHO_SWEEP_WRITE_ONLY, $ELBENCHO_SWEEP_WRITE_NO_READ,
#   $ELBENCHO_SWEEP_READ_FROM, $ELBENCHO_SCALE_READ_WRITE_DURATION.
print_elbencho_sweep_compact_summary() {
    local ds="$1"
    local nodes_spec="$2"
    local prefix="${3:-}"

    local dio_or_bio_caps
    case "${dio_or_bio:-}" in
        dio) dio_or_bio_caps=DIO ;;
        bio) dio_or_bio_caps=BIO ;;
        *)   dio_or_bio_caps="${dio_or_bio:-?}" ;;
    esac

    local io_sizes threads iodepths
    io_sizes=$(_join_with ", " "${ELBENCHO_SCALE_IO_SIZES[@]}")
    threads=$(_join_with ", " "${ELBENCHO_SCALE_THREAD_LIST[@]}")
    iodepths=$(_join_with ", " "${ELBENCHO_IODEPTH_LIST[@]}")

    local summary_sweep_read_from
    local summary_sweep_write_only
    local summary_sweep_write_no_read
    if [[ -n "${sweep_read_from+x}" ]]; then
        summary_sweep_read_from="$sweep_read_from"
    else
        summary_sweep_read_from="${ELBENCHO_SWEEP_READ_FROM:-}"
    fi
    if [[ -n "${sweep_write_only+x}" ]]; then
        summary_sweep_write_only="$sweep_write_only"
    else
        summary_sweep_write_only="${ELBENCHO_SWEEP_WRITE_ONLY:-0}"
    fi
    if [[ -n "${sweep_write_no_read+x}" ]]; then
        summary_sweep_write_no_read="$sweep_write_no_read"
    else
        summary_sweep_write_no_read="${ELBENCHO_SWEEP_WRITE_NO_READ:-0}"
    fi

    # fs_mul is N/A when the run reads from an existing tree (multiplier
    # doesn't apply because file sizes come from the existing files).
    local fs_mul
    if [[ -n "$summary_sweep_read_from" ]]; then
        fs_mul="N/A"
    else
        fs_mul="${ELBENCHO_FILE_SIZE_MULTIPLIER:-N/A}"
    fi
    local single_big="${ELBENCHO_SINGLE_BIG_FILE:-0}"
    local file_layout="${ELBENCHO_FILE_LAYOUT:-worker-directories}"
    local files_per_node="${ELBENCHO_FILES_PER_NODE:-}"
    local exact_file_size="${ELBENCHO_FILE_SIZE:-}"
    local wro="$summary_sweep_write_only"
    local wnr="$summary_sweep_write_no_read"
    local rand_part=""
    if [[ "${rand_option:-0}" == "1" ]]; then
        rand_part=" rand=1"
    fi
    local rf_part=""
    if [[ -n "$summary_sweep_read_from" ]]; then
        rf_part=" rf=${summary_sweep_read_from}"
    fi

    if [[ -z "$summary_sweep_read_from" \
            && "$file_layout" == shared-directory \
            && -n "$files_per_node" ]]; then
        printf '%selbencho-%s configured_dur: %s (inactive; completion-based) nodes_spec: %s\n' \
            "$prefix" "$ds" "${ELBENCHO_SCALE_READ_WRITE_DURATION:-?}" \
            "$nodes_spec"
    else
        printf '%selbencho-%s dur: %ss nodes_spec: %s\n' \
            "$prefix" "$ds" "${ELBENCHO_SCALE_READ_WRITE_DURATION:-?}" \
            "$nodes_spec"
    fi
    printf '%s    %s io_size(%s) threads(%s) io_depth(%s)\n' \
        "$prefix" "$dio_or_bio_caps" "$io_sizes" "$threads" "$iodepths"
    if [[ -n "$summary_sweep_read_from" ]]; then
        printf '%s    layout=staged-tree files_per_node=N/A fs_mul=%s single_big=%s wro=%s wnr=%s%s%s\n' \
            "$prefix" "$fs_mul" "$single_big" "$wro" "$wnr" "$rand_part" "$rf_part"
    else
        printf '%s    layout=%s files_per_node=%s file_size=%s fs_mul=%s single_big=%s wro=%s wnr=%s%s%s\n' \
            "$prefix" "$file_layout" "${files_per_node:-N/A}" \
            "${exact_file_size:-derived}" "$fs_mul" "$single_big" "$wro" \
            "$wnr" "$rand_part" "$rf_part"
    fi
    return 0
}

# Format bash array elements as a YAML flow sequence body: "v1", "v2", ...
# Usage: _yaml_flow_seq "${arr[@]}"
_yaml_flow_seq() {
    local result=""
    local sep=""
    local elem
    for elem in "$@"; do
        result="${result}${sep}\"${elem}\""
        sep=", "
    done
    printf '%s' "$result"
    return 0
}

# Write env_used.yaml to the given path with all sweep-relevant env vars.
# Also writes a sibling env_used.sh in the same directory (sourceable bash;
# canonical artifact for --resume rehydration since YAML parsing in shell is
# expensive). The YAML form is preserved as the human/external-tool snapshot.
#
# Usage: write_elbencho_env_used <out_file> <dio_or_bio> <rand_option> <single_option> \
#            <sweep_write_only> <sweep_write_no_read> <sweep_read_from> <nodes_spec> [records_dir]
_elbencho_env_used_treefile_cache_path() {
    local sweep_read_from="$1"
    if [[ -n "$sweep_read_from" && "${ELBENCHO_SINGLE_BIG_FILE:-0}" != "1" ]]; then
        printf '%s/.storage-scale-test-elbencho-treefile.txt' "$sweep_read_from"
    fi
    return 0
}

_elbencho_env_used_emit_treefile_cache_yaml() {
    local records_dir="$1"
    local record
    local execution_id
    printf '  attempts:\n'
    for record in "$records_dir"/*.treefile-cache; do
        [[ -f "$record" ]] || continue
        execution_id=$(basename "$record" .treefile-cache)
        while IFS=$'\t' read -r timestamp state outcome cache_path; do
            [[ -n "$timestamp" ]] || continue
            printf '    - timestamp: "%s"\n' "$timestamp"
            printf '      state_at_start: "%s"\n' "$state"
            printf '      outcome: "%s"\n' "$outcome"
            printf '      path: "%s"\n' "$cache_path"
            printf '      execution: "%s"\n' "$execution_id"
        done < "$record"
    done
    return 0
}

_elbencho_env_used_emit_treefile_cache_sh() {
    local records_dir="$1"
    local record
    local execution_id
    printf 'declare -ga ELBENCHO_TREEFILE_CACHE_ATTEMPTS=(\n'
    for record in "$records_dir"/*.treefile-cache; do
        [[ -f "$record" ]] || continue
        execution_id=$(basename "$record" .treefile-cache)
        while IFS=$'\t' read -r timestamp state outcome cache_path; do
            [[ -n "$timestamp" ]] || continue
            printf '    %q\n' "$execution_id|$timestamp|$state|$outcome|$cache_path"
        done < "$record"
    done
    printf ')\nexport ELBENCHO_TREEFILE_CACHE_ATTEMPTS\n'
    return 0
}

write_elbencho_env_used() {
    local out_file="$1"
    local dio_or_bio="$2"
    local rand_option="$3"
    local single_option="$4"
    local sweep_write_only="$5"
    local sweep_write_no_read="$6"
    local sweep_read_from="$7"
    local nodes_spec="$8"
    local records_dir="${9:-$(dirname "$out_file")/executions}"
    local cache_path
    cache_path=$(_elbencho_env_used_treefile_cache_path "$sweep_read_from")

    {
        printf '# env_used.yaml - elbencho sweep configuration snapshot\n'
        printf '# Generated: %s\n\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

        printf 'TEST_DIRS:\n'
        local _path
        for _path in "${!TEST_DIRS[@]}"; do
            printf '  "%s": %s\n' "$_path" "${TEST_DIRS[$_path]}"
        done

        printf '\nFS_MAX_AGG_THROUGHPUT: %s\n' "$FS_MAX_AGG_THROUGHPUT"
        printf 'FS_MAX_NODE_THROUGHPUT_GBPS: %s\n' "$FS_MAX_NODE_THROUGHPUT_GBPS"
        printf 'FS_MAX_NODE_IOPS: %s\n' "$FS_MAX_NODE_IOPS"

        printf '\nELBENCHO_SCALE_THREAD_LIST: [%s]\n' \
            "$(_yaml_flow_seq "${ELBENCHO_SCALE_THREAD_LIST[@]}")"
        printf 'ELBENCHO_SCALE_IO_SIZES: [%s]\n' \
            "$(_yaml_flow_seq "${ELBENCHO_SCALE_IO_SIZES[@]}")"
        printf 'ELBENCHO_IODEPTH_LIST: [%s]\n' \
            "$(_yaml_flow_seq "${ELBENCHO_IODEPTH_LIST[@]}")"
        printf 'ELBENCHO_FILE_SIZE_MULTIPLIER: %s\n' "$ELBENCHO_FILE_SIZE_MULTIPLIER"
        printf 'ELBENCHO_FILE_LAYOUT: "%s"\n' \
            "${ELBENCHO_FILE_LAYOUT:-worker-directories}"
        printf 'ELBENCHO_FILES_PER_NODE: "%s"\n' \
            "${ELBENCHO_FILES_PER_NODE:-}"
        printf 'ELBENCHO_FILE_SIZE: "%s"\n' "${ELBENCHO_FILE_SIZE:-}"
        printf 'ELBENCHO_SCALE_READ_WRITE_DURATION: %s\n' "$ELBENCHO_SCALE_READ_WRITE_DURATION"
        printf 'ELBENCHO_READ_AFTER_WRITE_PAUSE: %s\n' "$ELBENCHO_READ_AFTER_WRITE_PAUSE"
        printf 'ELBENCHO_LIVE_CSV_EXTENDED: %s\n' "${ELBENCHO_LIVE_CSV_EXTENDED:-0}"
        printf 'ELBENCHO_LIVEINT: %s\n' "${ELBENCHO_LIVEINT:-1000}"

        printf '\nELBENCHO_SINGLE_BIG_FILE: %s\n' "$ELBENCHO_SINGLE_BIG_FILE"
        printf 'ELBENCHO_SINGLE_BIG_FILE_BASENAME: "%s"\n' "$ELBENCHO_SINGLE_BIG_FILE_BASENAME"
        printf 'ELBENCHO_SINGLE_BIG_FILE_SIZE: "%s"\n' "${ELBENCHO_SINGLE_BIG_FILE_SIZE:-}"
        printf 'ELBENCHO_ALL_NODES_ACCESS_ALL_DATA: %s\n' "$ELBENCHO_ALL_NODES_ACCESS_ALL_DATA"

        printf '\ndio_or_bio: "%s"\n' "$dio_or_bio"
        printf 'rand_option: %s\n' "$rand_option"
        printf 'single_option: %s\n' "$single_option"
        printf 'sweep_write_only: %s\n' "$sweep_write_only"
        printf 'sweep_write_no_read: %s\n' "$sweep_write_no_read"
        printf 'sweep_read_from: "%s"\n' "$sweep_read_from"
        printf 'nodes_spec: "%s"\n' "$nodes_spec"
        printf '\ntreefile_cache:\n'
        printf '  path: "%s"\n' "$cache_path"
        _elbencho_env_used_emit_treefile_cache_yaml "$records_dir"
    } > "$out_file"

    # Also emit the sourceable sidecar next to the YAML so --resume can read
    # it without a YAML parser. The .sh form is the canonical resume artifact;
    # the .yaml form is for humans + external tools (downstream Python utils).
    local sh_path="${out_file%.yaml}.sh"
    if [[ "$sh_path" == "$out_file" ]]; then
        # Caller passed something other than .yaml; place .sh alongside as
        # <out_file>.sh to keep behavior consistent.
        sh_path="${out_file}.sh"
    fi
    _write_elbencho_env_used_sh "$sh_path" \
        "$dio_or_bio" "$rand_option" "$single_option" \
        "$sweep_write_only" "$sweep_write_no_read" "$sweep_read_from" "$nodes_spec" \
        "$cache_path" "$records_dir" || return 1
    return 0
}

# Emit a `declare -ga NAME=(...)` block for an array using shell-quoting
# so values with spaces / special chars round-trip correctly.
# Usage: _emit_bash_array_decl NAME val1 val2 ...
_emit_bash_array_decl() {
    local name="$1"
    shift
    printf 'unset %s\ndeclare -ga %s=(' "$name" "$name"
    local first=1
    local v
    for v in "$@"; do
        if [[ $first -eq 1 ]]; then
            printf '%q' "$v"
            first=0
        else
            printf ' %q' "$v"
        fi
    done
    printf ')\nexport %s\n' "$name"
    return 0
}

# Emit a `declare -gA TEST_DIRS=(...)` block. Operates on the global TEST_DIRS.
_emit_bash_test_dirs_decl() {
    printf 'unset TEST_DIRS\ndeclare -gA TEST_DIRS=(\n'
    local _path
    for _path in "${!TEST_DIRS[@]}"; do
        printf '    [%q]=%q\n' "$_path" "${TEST_DIRS[$_path]}"
    done
    printf ')\nexport TEST_DIRS\n'
    return 0
}

# Body of env_used.sh — sourceable bash sidecar emitted alongside env_used.yaml.
# Sourcing this restores TEST_DIRS, FS_MAX_*, ELBENCHO_* arrays/scalars, and
# the original CLI flag values needed to resume an interrupted sweep.
_write_elbencho_env_used_sh() {
    local sh_path="$1"
    local dio_or_bio="$2"
    local rand_option="$3"
    local single_option="$4"
    local sweep_write_only="$5"
    local sweep_write_no_read="$6"
    local sweep_read_from="$7"
    local nodes_spec="$8"
    local cache_path="$9"
    local records_dir="${10}"

    {
        printf '# shellcheck shell=bash disable=SC2034\n'
        printf '# env_used.sh - sourceable elbencho sweep configuration snapshot.\n'
        printf '# Auto-generated. The companion env_used.yaml is the human/external-tool form.\n'
        printf '# This file is the canonical artifact for nv-elbencho-sweep.sh --resume.\n'
        printf '# Generated: %s\n\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

        _emit_bash_test_dirs_decl
        printf '\n'

        printf 'export FS_MAX_AGG_THROUGHPUT=%q\n' "$FS_MAX_AGG_THROUGHPUT"
        printf 'export FS_MAX_NODE_THROUGHPUT_GBPS=%q\n' "$FS_MAX_NODE_THROUGHPUT_GBPS"
        printf 'export FS_MAX_NODE_IOPS=%q\n\n' "$FS_MAX_NODE_IOPS"

        _emit_bash_array_decl ELBENCHO_SCALE_THREAD_LIST "${ELBENCHO_SCALE_THREAD_LIST[@]}"
        _emit_bash_array_decl ELBENCHO_SCALE_IO_SIZES "${ELBENCHO_SCALE_IO_SIZES[@]}"
        _emit_bash_array_decl ELBENCHO_IODEPTH_LIST "${ELBENCHO_IODEPTH_LIST[@]}"
        printf '\n'

        printf 'export ELBENCHO_FILE_SIZE_MULTIPLIER=%q\n' "$ELBENCHO_FILE_SIZE_MULTIPLIER"
        printf 'export ELBENCHO_FILE_LAYOUT=%q\n' \
            "${ELBENCHO_FILE_LAYOUT:-worker-directories}"
        printf 'export ELBENCHO_FILES_PER_NODE=%q\n' \
            "${ELBENCHO_FILES_PER_NODE:-}"
        printf 'export ELBENCHO_FILE_SIZE=%q\n' "${ELBENCHO_FILE_SIZE:-}"
        printf 'export ELBENCHO_SCALE_READ_WRITE_DURATION=%q\n' "$ELBENCHO_SCALE_READ_WRITE_DURATION"
        printf 'export ELBENCHO_READ_AFTER_WRITE_PAUSE=%q\n' "$ELBENCHO_READ_AFTER_WRITE_PAUSE"
        printf 'export ELBENCHO_LIVE_CSV_EXTENDED=%q\n' "${ELBENCHO_LIVE_CSV_EXTENDED:-0}"
        printf 'export ELBENCHO_LIVEINT=%q\n' "${ELBENCHO_LIVEINT:-1000}"
        printf 'export ELBENCHO_SINGLE_BIG_FILE=%q\n' "$ELBENCHO_SINGLE_BIG_FILE"
        printf 'export ELBENCHO_SINGLE_BIG_FILE_BASENAME=%q\n' "$ELBENCHO_SINGLE_BIG_FILE_BASENAME"
        printf 'export ELBENCHO_SINGLE_BIG_FILE_SIZE=%q\n' "${ELBENCHO_SINGLE_BIG_FILE_SIZE:-}"
        printf 'export ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=%q\n\n' "$ELBENCHO_ALL_NODES_ACCESS_ALL_DATA"

        printf '# Original CLI flag values (resume reconstructs sweep behavior from these).\n'
        printf '# Names match the local variables used in nv-elbencho-sweep.sh.\n'
        printf 'export dio_or_bio=%q\n' "$dio_or_bio"
        printf 'export rand_option=%q\n' "$rand_option"
        printf 'export single_option=%q\n' "$single_option"
        printf 'export sweep_write_only=%q\n' "$sweep_write_only"
        printf 'export sweep_write_no_read=%q\n' "$sweep_write_no_read"
        printf 'export sweep_read_from=%q\n' "$sweep_read_from"
        printf 'export nodes_spec=%q\n' "$nodes_spec"
        printf 'export ELBENCHO_TREEFILE_CACHE_PATH=%q\n' "$cache_path"
        _elbencho_env_used_emit_treefile_cache_sh "$records_dir"
    } > "$sh_path" || return 1
    return 0
}

# Rebuild the two env snapshots after a runtime treefile-cache record has been
# added. The sourceable snapshot remains complete if a sweep is interrupted
# between executions.
update_elbencho_env_used_treefile_cache_usage() {
    local output_dir="$1"
    local snapshot_sh="${output_dir}/env_used.sh"
    local tmp_dir
    tmp_dir=$(mktemp -d "${output_dir}/.env-used-cache.XXXXXX") || return 1
    (
        ELBENCHO_FILE_LAYOUT=worker-directories
        ELBENCHO_FILES_PER_NODE=
        ELBENCHO_FILE_SIZE=
        export ELBENCHO_FILE_LAYOUT ELBENCHO_FILES_PER_NODE ELBENCHO_FILE_SIZE
        # shellcheck disable=SC1090
        source "$snapshot_sh" || exit 1
        write_elbencho_env_used "${tmp_dir}/env_used.yaml" \
            "$dio_or_bio" "$rand_option" "$single_option" \
            "$sweep_write_only" "$sweep_write_no_read" "$sweep_read_from" "$nodes_spec" \
            "${output_dir}/executions"
    ) || {
        rm -rf "$tmp_dir"
        return 1
    }
    mv -f "${tmp_dir}/env_used.yaml" "${output_dir}/env_used.yaml" || {
        rm -rf "$tmp_dir"
        return 1
    }
    mv -f "${tmp_dir}/env_used.sh" "${output_dir}/env_used.sh" || {
        rm -rf "$tmp_dir"
        return 1
    }
    rmdir "$tmp_dir" || true
    return 0
}

# Compute the uniform per-worker file count for the dense (single flat
# directory) metadata workload.
#
# Usage: files_per_worker=$(mdtest_single_dir_files_per_worker \
#            <target_files> <nodes> <tasks_per_node>)
#
# elbencho's -N value applies to every worker, so the achievable total is
# quantized to whole workers. We round to the nearest achievable total, which
# keeps the error below half a worker-group quantum, and never return 0 so a
# tiny target still creates at least one file per worker.
mdtest_single_dir_files_per_worker() {
    local target_files="$1"
    local nodes="$2"
    local tasks_per_node="$3"

    if ! [[ "$target_files" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: target file count must be a positive integer, got '$target_files'" >&2
        return 1
    fi
    if ! [[ "$nodes" =~ ^[1-9][0-9]*$ ]] || ! [[ "$tasks_per_node" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: nodes and tasks per node must be positive integers," \
            "got '$nodes' and '$tasks_per_node'" >&2
        return 1
    fi

    local workers=$((nodes * tasks_per_node))
    local files_per_worker=$(( (target_files + workers / 2) / workers ))
    if [[ "$files_per_worker" -lt 1 ]]; then
        files_per_worker=1
    fi

    printf '%s\n' "$files_per_worker"
    return 0
}

# Print the dense-mode target-versus-actual file counts before a run.
# Usage: print_mdtest_single_dir_target_summary <target_files> <nodes> \
#            <tasks_per_node> <files_per_worker> <actual_files>
print_mdtest_single_dir_target_summary() {
    local target_files="$1"
    local nodes="$2"
    local tasks_per_node="$3"
    local files_per_worker="$4"
    local actual_files="$5"

    local workers=$((nodes * tasks_per_node))
    local difference=$((actual_files - target_files))

    echo "Directory layout: dense (single flat directory, elbencho -n 0)"
    printf '  Workers (nodes x tasks):  %d (%d x %d)\n' \
        "$workers" "$nodes" "$tasks_per_node"
    printf '  Files per worker (-N):    %d\n' "$files_per_worker"
    printf '  Target files:             %d\n' "$target_files"
    printf '  Actual files:             %d\n' "$actual_files"
    printf '  Difference from target:   %+d files (%s%%)\n' \
        "$difference" \
        "$(awk -v d="$difference" -v t="$target_files" \
            'BEGIN { printf "%+.4f", (d * 100.0) / t }')"
    return 0
}

# Write env_used.yaml for mdtest-elbencho sweep (elbencho metadata phases).
# Usage: write_mdtest_elbencho_env_used <out_file> <nodes_spec> <tasks_spec> \
#            [single_dir_target_files] [single_dir_files_per_worker] \
#            [single_dir_actual_files]
# The trailing dense-mode arguments are empty for the standard layout.
write_mdtest_elbencho_env_used() {
    local out_file="$1"
    local nodes_spec="$2"
    local tasks_spec="$3"
    local single_dir_target_files="${4:-}"
    local single_dir_files_per_worker="${5:-}"
    local single_dir_actual_files="${6:-}"

    local layout="standard"
    if [[ -n "$single_dir_target_files" ]]; then
        layout="single-dir"
    fi

    {
        printf '# env_used.yaml - mdtest-elbencho configuration snapshot\n'
        printf '# Generated: %s\n\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

        printf 'TEST_DIRS:\n'
        local _path
        for _path in "${!TEST_DIRS[@]}"; do
            printf '  "%s": %s\n' "$_path" "${TEST_DIRS[$_path]}"
        done

        printf '\nFS_MAX_AGG_THROUGHPUT: %s\n' "$FS_MAX_AGG_THROUGHPUT"
        printf 'FS_MAX_NODE_THROUGHPUT_GBPS: %s\n' "$FS_MAX_NODE_THROUGHPUT_GBPS"
        printf 'FS_MAX_NODE_IOPS: %s\n' "$FS_MAX_NODE_IOPS"

        printf '\nMDTEST_BRANCH_FACTOR: %s\n' "$MDTEST_BRANCH_FACTOR"
        printf 'MDTEST_ITEMS_PER_DIR: %s\n' "$MDTEST_ITEMS_PER_DIR"
        printf 'MDTEST_ITERATIONS: %s\n' "$MDTEST_ITERATIONS"
        printf 'ELBENCHO_READ_AFTER_WRITE_PAUSE: %s\n' "$ELBENCHO_READ_AFTER_WRITE_PAUSE"

        # The file counts are emitted unquoted so they load as integers; in the
        # standard layout they are empty, which loads as null.
        printf '\nmdtest_layout: "%s"\n' "$layout"
        printf 'single_dir_target_files: %s\n' "$single_dir_target_files"
        printf 'single_dir_files_per_worker: %s\n' "$single_dir_files_per_worker"
        printf 'single_dir_actual_files: %s\n' "$single_dir_actual_files"

        printf '\nnodes_spec: "%s"\n' "$nodes_spec"
        printf 'tasks_spec: "%s"\n' "$tasks_spec"
    } > "$out_file"
    return 0
}

# =============================================================================
# Per-execution dispatch helpers (elbencho sweep)
# =============================================================================
#
# These helpers implement the per-execution dispatch + resume model:
#   - SLURM: one sbatch allocation sized to max(remaining-nodes); inside it
#     a coordinator iterates non-SUCCESS executions, running each via
#     coordinator_run_one_execution against a per-execution subset of the
#     allocation's hosts. Service workers are started once and shared.
#   - SSH: dispatcher loops on the local host, picks fresh SSH hosts per
#     execution (so a node that went bad mid-run isn't bound to the rest),
#     ships an inline scriptlet to the head host, retrieves remote outputs
#     via the existing tar | tar pipeline.
#
# Status sentinels are atomically managed via _atomic_write_sentinel
# (defined in lib/_elbencho_functions.sh).

# Compute the TSV-equivalent CSV of generated test dirs for a single execution.
# Honors ELBENCHO_SWEEP_READ_FROM override. Prints the CSV to stdout.
_compute_test_dirs_csv_for_execution() {
    if [[ -n "${ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV:-}" ]]; then
        if [[ -n "${ELBENCHO_SWEEP_READ_FROM:-}" ]]; then
            printf '%s' "$ELBENCHO_SWEEP_READ_FROM"
        else
            printf '%s' "$ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV"
        fi
        return 0
    fi

    local -a tdirs
    if [[ -n "${ELBENCHO_RUN_TEST_DIR_SUFFIX:-}" ]]; then
        if ! mapfile -t tdirs < <(
            FS_TEST_DIR_SUFFIX_OVERRIDE="$ELBENCHO_RUN_TEST_DIR_SUFFIX" \
                generate_fs_test_directories "elbencho-sweep"
        ); then
            return 1
        fi
    elif ! mapfile -t tdirs < <(generate_fs_test_directories "elbencho-sweep"); then
        return 1
    fi
    local csv
    csv=$(IFS=,; printf '%s' "${tdirs[*]}")
    if [[ -n "${ELBENCHO_SWEEP_READ_FROM:-}" ]]; then
        csv="$ELBENCHO_SWEEP_READ_FROM"
    fi
    printf '%s' "$csv"
    return 0
}

# Print the result artifact paths for one reified execution, one per line.
# Usage: _elbencho_result_artifacts_for_execution <output_dir> <nnnn.sh>
_elbencho_result_artifacts_for_execution() {
    local output_dir="$1"
    local nnnn_sh="$2"

    if [[ ! -f "$nnnn_sh" ]]; then
        echo "Error: missing execution definition: $nnnn_sh" >&2
        return 1
    fi

    (
        local nodes io_size thread_count io_depth
        # shellcheck disable=SC1090
        ELBENCHO_FILE_LAYOUT=worker-directories
        ELBENCHO_FILES_PER_NODE=
        ELBENCHO_FILE_SIZE=
        # shellcheck disable=SC1090
        source "$nnnn_sh" || exit 1
        local ds="${output_dir##*-}"
        local resfile
        local csvfile
        local livecsvfile
        local treefile
        _elbencho_io_set_resfile_csvfile "$output_dir" "$io_size" "$nodes" \
            "$thread_count" "$io_depth" "$ds"
        _elbencho_io_set_livecsvfile "$output_dir" "$io_size" "$nodes" \
            "$thread_count" "$io_depth" "$ds"
        _elbencho_io_set_treefile "$output_dir" "$io_size" "$nodes" \
            "$thread_count" "$io_depth" "$ds"
        local execution_id
        execution_id=$(basename "$nnnn_sh" .sh)
        printf '%s\n' "$resfile" "$csvfile" "$livecsvfile" "$treefile" \
            "${output_dir}/executions/${execution_id}.write.json" \
            "${output_dir}/executions/${execution_id}.read.json" \
            "${output_dir}/executions/${execution_id}.delete.json" \
            "${output_dir}/executions/${execution_id}.workload.tsv"
    )
}

# Print completion artifacts that must exist after a successful execution.
# Legacy many-file/single-file modes have no required JSON/TSV artifacts.
_elbencho_required_result_artifacts_for_execution() {
    local output_dir="$1"
    local execution_id="$2"
    local nnnn_sh="$3"
    (
        ELBENCHO_FILE_LAYOUT=worker-directories
        ELBENCHO_FILES_PER_NODE=
        ELBENCHO_FILE_SIZE=
        # shellcheck disable=SC1090
        source "$nnnn_sh" || exit 1
        local prefix="${output_dir}/executions/${execution_id}"
        if [[ -n "${ELBENCHO_SWEEP_READ_FROM:-}" \
                && "${ELBENCHO_SINGLE_BIG_FILE:-0}" != 1 ]]; then
            printf '%s.workload.tsv\n' "$prefix"
            exit 0
        fi
        if [[ "${ELBENCHO_FILE_LAYOUT:-worker-directories}" != shared-directory \
                || -z "${ELBENCHO_FILES_PER_NODE:-}" ]]; then
            exit 0
        fi
        printf '%s.workload.tsv\n%s.write.json\n' "$prefix" "$prefix"
        if [[ "${ELBENCHO_SWEEP_WRITE_ONLY:-0}" != 1 \
                && "${ELBENCHO_SWEEP_WRITE_NO_READ:-0}" != 1 ]]; then
            printf '%s.read.json\n' "$prefix"
        fi
        if [[ "${ELBENCHO_SWEEP_WRITE_ONLY:-0}" != 1 ]]; then
            printf '%s.delete.json\n' "$prefix"
        fi
    )
}

# Remove local result artifacts for one reified execution. This is primarily
# needed by SSH dispatch: remote cleanup cannot remove stale files that were
# already retrieved into the local results directory by an earlier failed attempt.
# Usage: _cleanup_local_elbencho_result_artifacts_for_execution <output_dir> <id> <nnnn.sh>
_cleanup_local_elbencho_result_artifacts_for_execution() {
    local output_dir="$1"
    local nnnn_sh="$3"
    local artifact_output
    if ! artifact_output=$(
        _elbencho_result_artifacts_for_execution "$output_dir" "$nnnn_sh"
    ); then
        return 1
    fi
    local -a artifacts=()
    mapfile -t artifacts <<<"$artifact_output"
    _elbencho_io_cleanup_result_artifacts "${artifacts[@]}"
}

# Print a message with a local ISO-8601 timestamp prefix.
# Usage: _echo_ts "message..."
_echo_ts() {
    printf '%s %s\n' "$(date +"%Y-%m-%dT%H:%M:%S%z")" "$*"
    return 0
}

# Apply generated shared-directory failure cleanup from a reified execution.
# The cleanup helper relies only on identity captured in NNNN.sh.
_elbencho_finalize_shared_failure_from_nnnn() {
    local nnnn_sh="$1"
    local execution_id="$2"
    local output_dir="$3"
    # shellcheck disable=SC2030  # Defaults and run identity intentionally stay in this finalizer subshell.
    (
        ELBENCHO_FILE_LAYOUT=worker-directories
        ELBENCHO_FILES_PER_NODE=
        ELBENCHO_FILE_SIZE=
        # shellcheck disable=SC1090
        source "$nnnn_sh" || exit 1
        export ELBENCHO_RUN_EXECUTION_ID="$execution_id"
        export ELBENCHO_RUN_REMOTE_OUTPUT_DIR="$output_dir"
        _elbencho_finalize_generated_shared_failure
    )
}

# Coordinator worker (SLURM): runs one execution within the sbatch allocation.
# Sources NNNN.sh in a subshell, applies per-execution overrides, runs
# run_elbencho_io_sweep_iteration with output redirected to NNNN.log,
# and atomically writes NNNN.status / NNNN.exitcode / NNNN.jobid.
#
# Usage: coordinator_run_one_execution <executions_dir> <id> <alloc_hosts_csv> <output_dir>
# Returns: 0 on SUCCESS, non-zero on FAILED (caller should abort the chain).
coordinator_run_one_execution() {
    local executions_dir="$1"
    local id="$2"
    local alloc_hosts_csv="$3"
    # output_dir is named to match the variable that run_elbencho_io_sweep_iteration
    # reads (across files, in the calling shell scope). Keeping the parameter
    # name identical avoids a redundant `local output_dir="$sweep_output_dir"`
    # copy in the subshell below -- which static analysis flags as an unused
    # local because it can't see the cross-function consumer.
    local output_dir="$4"

    local nnnn_sh="${executions_dir}/${id}.sh"
    local status_file="${executions_dir}/${id}.status"
    local log_file="${executions_dir}/${id}.log"
    local exitcode_file="${executions_dir}/${id}.exitcode"
    local jobid_file="${executions_dir}/${id}.jobid"

    if [[ ! -f "$nnnn_sh" ]]; then
        echo "Error: missing execution definition: $nnnn_sh" >&2
        return 1
    fi

    _atomic_write_sentinel "$status_file" RUNNING || return 1

    local rc=0
    local tee_rc=0
    local -a execution_pipe_status=()
    (
        # shellcheck disable=SC1090
        source "$nnnn_sh" || exit 1
        local first_n_hosts_csv
        first_n_hosts_csv=$(printf '%s\n' "$alloc_hosts_csv" \
            | tr ',' '\n' | head -n "${nodes:?missing nodes in NNNN.sh}" \
            | paste -sd, -)
        export ELBENCHO_RUN_NODE_COUNT="$nodes"
        export ELBENCHO_RUN_HOSTS_CSV="$first_n_hosts_csv"
        # shellcheck disable=SC2031  # Per-execution values are freshly assigned in this subshell.
        export ELBENCHO_RUN_REMOTE_OUTPUT_DIR="$output_dir"
        # shellcheck disable=SC2031  # Per-execution values are freshly assigned in this subshell.
        export ELBENCHO_RUN_EXECUTION_ID="$id"
        # test_dirs_csv is read as a plain shell variable by
        # run_elbencho_io_sweep_iteration (a bash function in this same shell
        # scope), so it doesn't need `export` -- and not exporting lets the
        # existing lowercase name stay without tripping the "env vars must be
        # ALL_CAPS" static-analysis rule. output_dir benefits from the same
        # convention; the function's `local output_dir="$4"` (above) is its
        # one and only declaration in this scope.
        local test_dirs_csv
        test_dirs_csv=$(_compute_test_dirs_csv_for_execution) || exit 1

        # shellcheck disable=SC2154  # nodes, io_size, thread_count, io_depth come from sourcing NNNN.sh
        _echo_ts "[coordinator] starting execution ${id}: nodes=${nodes} hosts=${first_n_hosts_csv} io_size=${io_size} threads=${thread_count} iodepth=${io_depth}"
        run_elbencho_io_sweep_iteration
    ) 2>&1 | tee "$log_file"
    execution_pipe_status=("${PIPESTATUS[@]}")
    rc="${execution_pipe_status[0]}"
    tee_rc="${execution_pipe_status[1]}"
    if [[ "$tee_rc" -ne 0 ]]; then
        echo "Error: failed to write execution log ${log_file} (tee rc=$tee_rc)" >&2
        if [[ "$rc" -eq 0 ]]; then
            rc="$tee_rc"
        fi
    fi
    if [[ -f "${executions_dir}/${id}.treefile-cache" ]]; then
        update_elbencho_env_used_treefile_cache_usage "$output_dir" || \
            echo "Warning: failed to update treefile-cache records in env_used snapshots" >&2
    fi
    printf '%s\n' "${SLURM_JOB_ID:-}" > "$jobid_file"
    if [[ "$rc" -eq 0 ]]; then
        if _atomic_write_sentinel "$status_file" SUCCESS; then
            _echo_ts "[coordinator] execution ${id} SUCCESS"
        else
            rc=1
            echo "Error: unable to record SUCCESS for execution ${id}" >&2
            _elbencho_finalize_shared_failure_from_nnnn \
                "$nnnn_sh" "$id" "$output_dir" || true
            _atomic_write_sentinel "$status_file" FAILED || \
                echo "Error: unable to record FAILED for execution ${id}" >&2
        fi
    else
        if ! _elbencho_finalize_shared_failure_from_nnnn \
                "$nnnn_sh" "$id" "$output_dir"; then
            echo "Error: final generated-target cleanup failed for execution ${id}" >&2
        fi
        _atomic_write_sentinel "$status_file" FAILED || \
            echo "Error: unable to record FAILED for execution ${id}" >&2
        _echo_ts "[coordinator] execution ${id} FAILED (rc=$rc); see ${log_file}"
    fi
    printf '%s\n' "$rc" > "$exitcode_file"
    return "$rc"
}

# Refuse to reset RUNNING work while its recorded Slurm coordinator allocation
# is still present in squeue. When squeue rejects a retired job ID, sacct can
# confirm that the coordinator is terminal; otherwise the check fails closed.
# The optional allowed_job_id lets the recorded coordinator verify itself.
# Usage: _elbencho_verify_no_active_slurm_coordinator <executions_dir> [allowed_job_id]
_elbencho_verify_no_active_slurm_coordinator() {
    local executions_dir="$1"
    local allowed_job_id="${2:-}"
    local owner_file="${executions_dir}/.coordinator.jobid"
    local owner_job_id
    local active_job_ids
    local squeue_rc=0

    if [[ ! -e "$owner_file" ]]; then
        return 0
    fi
    if ! read -r owner_job_id < "$owner_file" || [[ ! "$owner_job_id" =~ ^[0-9]+$ ]]; then
        echo "Error: invalid coordinator owner record: $owner_file" >&2
        return 1
    fi
    active_job_ids=$(
        squeue --noheader --jobs="$owner_job_id" --format="%A" 2>/dev/null
    ) || squeue_rc=$?
    if [[ "$squeue_rc" -ne 0 ]]; then
        if _elbencho_slurm_job_is_terminal "$owner_job_id"; then
            return 0
        fi
        echo "Error: unable to verify coordinator job $owner_job_id with squeue or terminal sacct state; refusing to reset RUNNING work" >&2
        return 1
    fi
    if ! awk -v owner_job_id="$owner_job_id" '$1 == owner_job_id { found=1 } END { exit !found }' <<<"$active_job_ids"; then
        return 0
    fi
    if [[ -n "$allowed_job_id" && "$owner_job_id" == "$allowed_job_id" ]]; then
        return 0
    fi
    echo "Error: coordinator job $owner_job_id is still active; refusing to reset or redispatch its RUNNING execution(s)" >&2
    return 1
}

# Release a dispatch lock only when the caller still owns its unique token.
# Usage: _elbencho_release_dispatch_lock <executions_dir> <token>
_elbencho_release_dispatch_lock() {
    local executions_dir="$1"
    local expected_token="$2"
    local lock_dir="${executions_dir}/.dispatch.lock"
    local actual_token

    if [[ ! -d "$lock_dir" ]]; then
        return 0
    fi
    if ! read -r actual_token < "$lock_dir/token" \
            || [[ -z "$actual_token" || "$actual_token" != "$expected_token" ]]; then
        echo "Error: refusing to release dispatch lock not owned by this process: $lock_dir" >&2
        return 1
    fi
    rm -f -- \
        "$lock_dir/token" "$lock_dir/mode" "$lock_dir/hostname" \
        "$lock_dir/pid" "$lock_dir/jobid"
    if ! rmdir -- "$lock_dir"; then
        echo "Error: unable to remove dispatch lock: $lock_dir" >&2
        return 1
    fi
    return 0
}

# Return 0 when the main Slurm job is terminal, 1 when it is nonterminal, and
# 2 when accounting cannot yet determine its state.
_elbencho_slurm_job_is_terminal() {
    local job_id="$1"
    local main_state
    if ! main_state=$(
        sacct -n -P -j "$job_id" --format=JobIDRaw,State 2>/dev/null \
            | awk -F'|' -v job_id="$job_id" '$1 == job_id { print $2; exit }'
    ); then
        return 2
    fi
    if [[ -z "$main_state" ]]; then
        return 2
    fi
    case "$main_state" in
        BOOT_FAIL*|CANCELLED*|COMPLETED*|DEADLINE*|FAILED*|NODE_FAIL*|OUT_OF_MEMORY*|PREEMPTED*|REVOKED*|SPECIAL_EXIT*|TIMEOUT*)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# Return 0 when a recorded lock owner is active, 1 when it is provably stale,
# and 2 when ownership cannot safely be determined.
_elbencho_dispatch_lock_is_active() {
    local executions_dir="$1"
    local lock_dir="${executions_dir}/.dispatch.lock"
    local mode owner_host owner_pid owner_job_id active_job_ids local_host

    if ! read -r mode < "$lock_dir/mode" \
            || ! read -r owner_host < "$lock_dir/hostname" \
            || ! read -r owner_pid < "$lock_dir/pid"; then
        return 2
    fi
    case "$mode" in
        ssh|slurm-submit)
            local_host=$(hostname) || return 2
            if [[ "$owner_host" != "$local_host" || ! "$owner_pid" =~ ^[0-9]+$ ]]; then
                return 2
            fi
            if kill -0 "$owner_pid" 2>/dev/null; then
                return 0
            fi
            if [[ "$mode" == "ssh" ]]; then
                return 1
            fi
            # The submitter may have died after sbatch accepted the job but
            # before the coordinator adopted the token. Never reclaim this
            # ambiguous handoff state.
            return 2
            ;;
        slurm)
            if ! read -r owner_job_id < "$lock_dir/jobid" \
                    || [[ ! "$owner_job_id" =~ ^[0-9]+$ ]]; then
                return 2
            fi
            if ! active_job_ids=$(
                squeue --noheader --jobs="$owner_job_id" --format="%A" 2>/dev/null
            ); then
                return 2
            fi
            if awk -v owner_job_id="$owner_job_id" \
                    '$1 == owner_job_id { found=1 } END { exit !found }' \
                    <<<"$active_job_ids"; then
                return 0
            fi
            if _elbencho_slurm_job_is_terminal "$owner_job_id"; then
                return 1
            fi
            return 2
            ;;
        *)
            return 2
            ;;
    esac
}

# Populate metadata after the lock directory has been created atomically.
# Keeping this separate avoids nested acquisition conditionals and makes a
# partially initialized directory fail closed to other dispatchers.
_elbencho_write_dispatch_lock_metadata() {
    local lock_dir="$1"
    local token="$2"
    local owner_host="$3"
    local owner_pid="$4"
    local mode="$5"

    _atomic_write_sentinel "$lock_dir/token" "$token" || return 1
    _atomic_write_sentinel "$lock_dir/hostname" "$owner_host" || return 1
    _atomic_write_sentinel "$lock_dir/pid" "$owner_pid" || return 1
    _atomic_write_sentinel "$lock_dir/mode" "$mode"
}

# Atomically acquire exclusive ownership of mutable execution state. A stale
# owner is reclaimed only when its SSH process is dead or its Slurm job has a
# terminal accounting record. A dead slurm-submit owner remains locked because
# its accepted job may still be starting.
# Usage: _elbencho_acquire_dispatch_lock <executions_dir> <ssh|slurm-submit> <token_var>
_elbencho_acquire_dispatch_lock() {
    local executions_dir="$1"
    local mode="$2"
    local token_var="$3"
    local lock_dir="${executions_dir}/.dispatch.lock"
    local local_host new_token existing_token active_rc attempt

    if [[ "$mode" != "ssh" && "$mode" != "slurm-submit" ]]; then
        echo "Error: invalid dispatch lock mode: $mode" >&2
        return 1
    fi
    local_host=$(hostname) || return 1
    new_token="${local_host}:${BASHPID:-$$}:$(date +%s):${RANDOM}"

    for attempt in 1 2; do
        if mkdir -- "$lock_dir" 2>/dev/null; then
            _elbencho_write_dispatch_lock_metadata \
                "$lock_dir" "$new_token" "$local_host" "${BASHPID:-$$}" "$mode" || {
                rm -f -- \
                    "$lock_dir/token" "$lock_dir/mode" "$lock_dir/hostname" \
                    "$lock_dir/pid" "$lock_dir/jobid"
                rmdir -- "$lock_dir" 2>/dev/null || true
                echo "Error: unable to initialize dispatch lock: $lock_dir" >&2
                return 1
            }
            printf -v "$token_var" '%s' "$new_token"
            return 0
        fi

        if ! read -r existing_token < "$lock_dir/token" || [[ -z "$existing_token" ]]; then
            echo "Error: dispatch lock is being initialized; refusing concurrent resume: $lock_dir" >&2
            return 1
        fi
        if _elbencho_dispatch_lock_is_active "$executions_dir"; then
            echo "Error: another dispatcher or coordinator owns $executions_dir; refusing concurrent resume" >&2
            return 1
        else
            active_rc=$?
        fi
        if [[ "$active_rc" -ne 1 ]]; then
            echo "Error: unable to verify dispatch lock owner; refusing concurrent resume: $lock_dir" >&2
            return 1
        fi
        if ! _elbencho_release_dispatch_lock "$executions_dir" "$existing_token"; then
            return 1
        fi
    done
    echo "Error: unable to acquire dispatch lock: $lock_dir" >&2
    return 1
}

# Transfer the pre-submission lock to the exact Slurm job. Both the submitting
# process and coordinator may call this idempotently with the same token/job.
# Usage: _elbencho_adopt_slurm_dispatch_lock <executions_dir> <token> <job_id>
_elbencho_adopt_slurm_dispatch_lock() {
    local executions_dir="$1"
    local expected_token="$2"
    local job_id="$3"
    local lock_dir="${executions_dir}/.dispatch.lock"
    local actual_token existing_job_id

    if [[ ! "$job_id" =~ ^[0-9]+$ ]] \
            || ! read -r actual_token < "$lock_dir/token" \
            || [[ "$actual_token" != "$expected_token" ]]; then
        echo "Error: Slurm coordinator does not own dispatch lock: $lock_dir" >&2
        return 1
    fi
    if [[ -e "$lock_dir/jobid" ]] \
            && { ! read -r existing_job_id < "$lock_dir/jobid" \
                || [[ "$existing_job_id" != "$job_id" ]]; }; then
        echo "Error: dispatch lock belongs to Slurm job ${existing_job_id:-unknown}, not $job_id" >&2
        return 1
    elif [[ ! -e "$lock_dir/jobid" ]] \
            && ! _atomic_write_sentinel "$lock_dir/jobid" "$job_id"; then
        return 1
    fi
    _atomic_write_sentinel "${executions_dir}/.coordinator.jobid" "$job_id" || return 1
    _atomic_write_sentinel "$lock_dir/mode" slurm || return 1
    return 0
}

# SLURM dispatcher (local host): submits ONE sbatch sized to max(remaining-nodes)
# that runs the coordinator script. Then tail_until_complete on the sbatch jobid.
# Usage: dispatch_slurm_executions <output_dir>
dispatch_slurm_executions() {
    local output_dir="$1"
    local executions_dir="${output_dir}/executions"
    if [[ ! -d "$executions_dir" ]]; then
        echo "Error: missing executions dir: $executions_dir" >&2
        return 1
    fi
    _elbencho_verify_no_active_slurm_coordinator "$executions_dir" || return 1
    local dispatch_lock_token=""
    local dispatch_lock_handed_off=0
    _elbencho_acquire_dispatch_lock \
        "$executions_dir" slurm-submit dispatch_lock_token || return 1
    local rc=0
    _dispatch_slurm_executions_owned \
        "$output_dir" "$dispatch_lock_token" dispatch_lock_handed_off || rc=$?
    if [[ "$dispatch_lock_handed_off" -eq 0 ]]; then
        _elbencho_release_dispatch_lock \
            "$executions_dir" "$dispatch_lock_token" || rc=1
    fi
    return "$rc"
}

# Run Slurm dispatch after the public entry point has acquired exclusive
# ownership. The output variable reports whether ownership transferred to
# the job.
_dispatch_slurm_executions_owned() {
    local output_dir="$1"
    local dispatch_lock_token="$2"
    local dispatch_lock_handed_off_var="$3"
    local executions_dir="${output_dir}/executions"
    _elbencho_sweep_running_to_pending "$executions_dir" || return 1

    local max_nodes
    if ! max_nodes=$(max_nodes_remaining_executions "$executions_dir"); then
        echo "All executions are SUCCESS; nothing to dispatch."
        return 0
    fi

    local ds="${output_dir##*-}"
    local job_name
    job_name=$(make_sbatch_job_name "elbencho" "$ds" "coordinator")
    local output_fmt="${output_dir}/coordinator-%j.log"

    # build_sbatch_cmd uses $sbatch_cmd in caller scope (existing convention);
    # we set up the same locals run_sbatch_job expects.
    # shellcheck disable=SC2034
    local -a sbatch_cmd=()
    if ! build_sbatch_cmd sbatch_cmd; then
        return 1
    fi
    # shellcheck disable=SC2034
    local sleep_time=10
    local -a log_files=()
    # shellcheck disable=SC2034
    local -a g_sbatch_opts=()
    local job_id=""
    declare -n JOBID=job_id  # nameref so run_sbatch_job assigns to job_id
    # The noop read below silences SC2034 for these variables used by run_sbatch_job.
    : "${#sbatch_cmd[@]}" "$sleep_time" "${g_sbatch_opts[*]:-}"

    echo "Submitting ONE sbatch coordinator (allocation: ${max_nodes} nodes) for $(count_elbencho_remaining_executions "$executions_dir") remaining execution(s)"
    cd "${SCALE_TEST_BASE}/storage-tests/fs" || return 1
    if ! run_sbatch_job "$max_nodes" "$job_name" "$output_fmt" "Elbencho coordinator" \
            sbatch/_nv-elbencho-coordinator.sh "$output_dir" "$dispatch_lock_token"; then
        echo "Error: sbatch submission failed" >&2
        return 1
    fi
    if ! _elbencho_adopt_slurm_dispatch_lock \
            "$executions_dir" "$dispatch_lock_token" "$job_id"; then
        echo "Error: unable to transfer dispatch lock to coordinator job $job_id; cancelling it" >&2
        scancel "$job_id" || true
        return 1
    fi
    printf -v "$dispatch_lock_handed_off_var" '%s' 1
    tail_until_complete "$job_id" "${log_files[@]}"
    return $?
}

# SSH dispatcher: sequential per-execution dispatch with fresh host selection.
# Aborts on first FAILED so the user can fix and --resume.
# Usage: dispatch_ssh_executions <output_dir>
dispatch_ssh_executions() {
    local output_dir="$1"
    local executions_dir="${output_dir}/executions"
    if [[ ! -d "$executions_dir" ]]; then
        echo "Error: missing executions dir: $executions_dir" >&2
        return 1
    fi
    local dispatch_lock_token=""
    local ssh_services_started=0
    _elbencho_acquire_dispatch_lock \
        "$executions_dir" ssh dispatch_lock_token || return 1
    local rc=0
    _dispatch_ssh_executions_owned \
        "$output_dir" ssh_services_started || rc=$?
    if [[ "$ssh_services_started" -eq 1 ]]; then
        _ssh_stop_services_on_all_hosts || true
    fi
    _elbencho_release_dispatch_lock \
        "$executions_dir" "$dispatch_lock_token" || rc=1
    return "$rc"
}

# Run SSH dispatch while the public entry point retains exclusive ownership.
# The nameref lets the entry point stop services before releasing the lock.
_dispatch_ssh_executions_owned() {
    local output_dir="$1"
    local -n ssh_services_started_ref="$2"
    local executions_dir="${output_dir}/executions"
    _elbencho_sweep_running_to_pending "$executions_dir" || return 1

    local total remaining
    total=$(list_elbencho_execution_ids "$executions_dir" | wc -l)
    remaining=$(count_elbencho_remaining_executions "$executions_dir")
    if [[ "$remaining" -eq 0 ]]; then
        echo "All executions are SUCCESS; nothing to dispatch."
        return 0
    fi

    if ! _ssh_update_active_hosts "reachability check" ':'; then
        echo "Error: no reachable SSH hosts remain" >&2
        return 1
    fi
    if ! ensure_all_ssh_nodes_can_elbencho; then
        echo "Error: failed to deploy elbencho to reachable SSH hosts" >&2
        return 1
    fi

    if ! _ssh_start_services_on_all_hosts; then
        echo "Error: failed to start elbencho services on any reachable SSH host" >&2
        return 1
    fi
    # shellcheck disable=SC2034  # Nameref assignment updates the caller's flag.
    ssh_services_started_ref=1

    local max_nodes
    if ! max_nodes=$(max_nodes_remaining_executions "$executions_dir"); then
        echo "Error: unable to determine the largest remaining execution" >&2
        return 1
    fi
    if [[ ${#SSH_ALL_HOSTS[@]} -lt "$max_nodes" ]]; then
        echo "Error: ${#SSH_ALL_HOSTS[@]} usable SSH host(s) remain, but a pending execution requires ${max_nodes}" >&2
        return 1
    fi
    echo "Dispatching SSH executions (remaining=${remaining}, total=${total}) from $executions_dir"
    # Compact 3-line sweep summary so the user knows what's about to run
    # during the long delay of executing. DS is in the basename suffix;
    # nodes_spec / dio_or_bio / etc. are in env (set by env.sh or
    # env_used.sh on resume).
    print_elbencho_sweep_compact_summary \
        "${output_dir##*-}" "${nodes_spec:-?}"

    local id
    local rc=0
    local done_count=0
    # Read the execution-id list via FD 3 instead of stdin so inner commands
    # in the loop body (ssh, srun, elbencho, etc.) cannot consume the loop's
    # input by inheriting the process-substitution from FD 0. Without this,
    # any inner command that reads stdin (or any srun that buffers stdin to
    # forward to remote tasks) eats the rest of the IDs and the loop exits
    # after one iteration.
    while IFS= read -r -u 3 id; do
        local status_file="${executions_dir}/${id}.status"
        [[ -f "$status_file" ]] || continue
        if [[ "$(cat "$status_file" 2>/dev/null)" == "SUCCESS" ]]; then
            done_count=$((done_count + 1))
            continue
        fi
        # Capture rc via || — `if ! cmd; then rc=$?` is always 0 (the ! succeeded).
        _ssh_dispatch_one_execution "$output_dir" "$id" || rc=$?
        if [[ "$rc" -ne 0 ]]; then
            echo "[dispatch] aborting remaining executions due to FAILED ${id}"
            break
        fi
        done_count=$((done_count + 1))
    done 3< <(list_elbencho_execution_ids "$executions_dir")

    if [[ "$rc" -eq 0 ]]; then
        echo "[dispatch] all $done_count execution(s) SUCCESS"
    fi
    return "$rc"
}

# Fan out an inline bash scriptlet to every host in a CSV list using
# spawn_N_ssh / gather_N_ssh, then verify each remote SSH exit code.
# Saves and restores SSH_NODELIST / SSH_NUM_NODES.
# When successful_hosts_array_name is supplied, it is populated with only the
# hosts whose SSH command returned zero, in the input order.
# Usage: _ssh_fan_out_to_each_host <hosts_csv> <bash_scriptlet> [successful_hosts_array_name]
_ssh_fan_out_to_each_host() {
    local hosts_csv="$1"
    local scriptlet="$2"
    local successful_hosts_array_name="${3:-}"
    local saved_nodelist="${SSH_NODELIST:-}"
    local saved_num="${SSH_NUM_NODES:-}"
    local hosts_count
    hosts_count=$(awk -F, '{print NF}' <<<"$hosts_csv")
    SSH_NODELIST="$hosts_csv"
    SSH_NUM_NODES="$hosts_count"
    export SSH_NODELIST SSH_NUM_NODES

    local status_dir
    status_dir=$(mktemp -d) || {
        SSH_NODELIST="$saved_nodelist"; SSH_NUM_NODES="$saved_num"
        return 1
    }

    local rc=0
    local -a completed_hosts=()
    local spawn_output
    if ! spawn_output=$(spawn_N_ssh "$status_dir" true "$scriptlet"); then
        rc=1
    else
        local -a pids=()
        # shellcheck disable=SC2206  # word-split on spaces is intentional
        pids=( $spawn_output )
        if [[ ${#pids[@]} -gt 0 ]]; then
            gather_N_ssh "$status_dir" "" "${pids[@]}" >/dev/null 2>&1 || true
            local pid ssh_rc hostname
            for pid in "${pids[@]}"; do
                hostname=$(results_N_ssh_pid "$status_dir" "${pid}" "$_SSH_RESULT_HOSTNAME" 2>/dev/null || echo "?")
                ssh_rc=$(results_N_ssh_pid "$status_dir" "${pid}" "rc" 2>/dev/null || echo 1)
                if [[ ! "$ssh_rc" =~ ^[0-9]+$ ]] || [[ "$ssh_rc" -ne 0 ]]; then
                    echo "Warning: SSH command failed on $hostname (rc=$ssh_rc)" >&2
                    rc=1
                else
                    completed_hosts+=("$hostname")
                fi
            done
        fi
    fi

    rm -rf "$status_dir"
    SSH_NODELIST="$saved_nodelist"
    SSH_NUM_NODES="$saved_num"
    export SSH_NODELIST SSH_NUM_NODES
    if [[ -n "$successful_hosts_array_name" ]]; then
        local -n successful_hosts_ref="$successful_hosts_array_name"
        # shellcheck disable=SC2034  # Assignment intentionally updates the caller's named array
        successful_hosts_ref=("${completed_hosts[@]}")
    fi
    return "$rc"
}

# Run a probe on the active SSH pool and remove only the hosts that fail it.
# Partial success is usable; an empty successful set is fatal.
# Usage: _ssh_update_active_hosts <description> <bash_scriptlet>
_ssh_update_active_hosts() {
    local description="$1"
    local scriptlet="$2"
    local hosts_csv
    hosts_csv=$(IFS=,; printf '%s' "${SSH_ALL_HOSTS[*]}")
    if [[ -z "$hosts_csv" ]]; then
        echo "Error: SSH_ALL_HOSTS is empty" >&2
        return 1
    fi

    local original_count=${#SSH_ALL_HOSTS[@]}
    local -a successful_hosts=()
    _ssh_fan_out_to_each_host "$hosts_csv" "$scriptlet" successful_hosts || true
    if [[ ${#successful_hosts[@]} -eq 0 ]]; then
        echo "Error: ${description} failed on all ${original_count} SSH host(s)" >&2
        return 1
    fi
    if [[ ${#successful_hosts[@]} -lt "$original_count" ]]; then
        echo "Warning: excluding $((original_count - ${#successful_hosts[@]})) SSH host(s) after ${description}; ${#successful_hosts[@]} remain" >&2
    fi
    SSH_ALL_HOSTS=("${successful_hosts[@]}")
    SSH_HOST_COUNT=${#SSH_ALL_HOSTS[@]}
    export SSH_ALL_HOSTS SSH_HOST_COUNT
    return 0
}

# Start elbencho services on each active host and prune hosts that fail startup.
_ssh_start_services_on_all_hosts() {
    echo "Starting elbencho services on ${#SSH_ALL_HOSTS[@]} SSH host(s)..."
    local -a original_hosts=("${SSH_ALL_HOSTS[@]}")
    local -a successful_hosts=()
    local -a failed_hosts=()
    local hosts_csv
    hosts_csv=$(IFS=,; printf '%s' "${original_hosts[*]}")

    # shellcheck disable=SC2016  # Single quotes intentional - command runs on remote
    _ssh_fan_out_to_each_host "$hosts_csv" 'export ELBENCHO=./elbencho && source ./_elbencho_functions.sh && start_elbencho_service' successful_hosts || true

    local -A successful_set=()
    local host
    for host in "${successful_hosts[@]}"; do
        successful_set["$host"]=1
    done
    for host in "${original_hosts[@]}"; do
        if [[ ! -v successful_set["$host"] ]]; then
            failed_hosts+=("$host")
        fi
    done

    # A startup command may launch elbencho and then fail its health check.
    # Stop every failed host explicitly before pruning it from the active pool.
    if [[ ${#failed_hosts[@]} -gt 0 ]]; then
        local failed_hosts_csv
        failed_hosts_csv=$(IFS=,; printf '%s' "${failed_hosts[*]}")
        # shellcheck disable=SC2016  # Single quotes intentional - command runs on remote
        _ssh_fan_out_to_each_host "$failed_hosts_csv" 'source ./_elbencho_functions.sh && stop_elbencho_service' || true
        echo "Warning: excluding ${#failed_hosts[@]} SSH host(s) after elbencho service startup; ${#successful_hosts[@]} remain" >&2
    fi

    SSH_ALL_HOSTS=("${successful_hosts[@]}")
    SSH_HOST_COUNT=${#SSH_ALL_HOSTS[@]}
    export SSH_ALL_HOSTS SSH_HOST_COUNT
    if [[ "$SSH_HOST_COUNT" -eq 0 ]]; then
        echo "Error: elbencho service startup failed on all ${#original_hosts[@]} SSH host(s)" >&2
        return 1
    fi
    return 0
}

# Stop elbencho services on every host in SSH_ALL_HOSTS (paired with above).
_ssh_stop_services_on_all_hosts() {
    local hosts_csv
    hosts_csv=$(IFS=,; printf '%s' "${SSH_ALL_HOSTS[*]}")
    if [[ -z "$hosts_csv" ]]; then
        return 0
    fi
    echo "Stopping elbencho services on ${#SSH_ALL_HOSTS[@]} SSH host(s)..."
    # shellcheck disable=SC2016  # Single quotes intentional - command runs on remote
    _ssh_fan_out_to_each_host "$hosts_csv" \
        'source ./_elbencho_functions.sh && stop_elbencho_service' || true
    return 0
}

# Validate $nodes from NNNN.sh; print to stdout. Returns 1 on bad/missing.
_ssh_extract_nodes_from_nnnn() {
    local nnnn_sh="$1"
    local nodes_value
    nodes_value=$(
        # shellcheck disable=SC1090
        source "$nnnn_sh" >/dev/null 2>&1 && printf '%s' "${nodes:-}"
    )
    if [[ -z "$nodes_value" ]] || [[ ! "$nodes_value" =~ ^[0-9]+$ ]] || [[ "$nodes_value" -lt 1 ]]; then
        return 1
    fi
    printf '%s' "$nodes_value"
    return 0
}

# Build the inline bash scriptlet shipped over SSH for one execution.
# Concatenates the body of NNNN.sh with the SSH-specific tail.
# Prints the scriptlet to stdout.
# Usage: _ssh_build_execution_scriptlet <nnnn.sh> <ssh_nodelist> <nodes> \
#   <test_dirs_csv> <remote_output_dir> <execution_id>
# shellcheck disable=SC2016  # Function emits variables for expansion by remote bash
_ssh_build_execution_scriptlet() {
    local nnnn_sh="$1"
    local ssh_nodelist="$2"
    local nodes="$3"
    local test_dirs_csv="$4"
    local remote_output_dir="$5"
    local execution_id="$6"

    printf '#!/usr/bin/env bash\n'
    printf '# Auto-generated SSH execution scriptlet\n'
    printf 'export ELBENCHO_FILE_LAYOUT=worker-directories\n'
    printf 'export ELBENCHO_FILES_PER_NODE=\n'
    printf 'export ELBENCHO_FILE_SIZE=\n'
    cat "$nnnn_sh"
    printf 'export SSH_NODELIST=%q\n' "$ssh_nodelist"
    printf '__elbencho_remote_output_dir=%q\n' "$remote_output_dir"
    printf 'case "$__elbencho_remote_output_dir" in\n'
    printf '    /*) __elbencho_remote_output_dir_abs="$__elbencho_remote_output_dir" ;;\n'
    printf '    *) __elbencho_remote_output_dir_abs="$(pwd)/$__elbencho_remote_output_dir" ;;\n'
    printf 'esac\n'
    printf 'export ELBENCHO_RUN_NODE_COUNT=%s\n' "$nodes"
    printf 'export ELBENCHO_RUN_HOSTS_CSV=%q\n' "$ssh_nodelist"
    printf 'export ELBENCHO_RUN_REMOTE_OUTPUT_DIR="$__elbencho_remote_output_dir_abs"\n'
    printf 'export ELBENCHO_RUN_EXECUTION_ID=%q\n' "$execution_id"
    printf 'export test_dirs_csv=%q\n' "$test_dirs_csv"
    printf 'export output_dir="$__elbencho_remote_output_dir_abs"\n'
    printf 'export ELBENCHO=./elbencho\n'
    printf 'source ./_elbencho_functions.sh || exit 1\n'
    printf 'set +e\n'
    printf 'run_elbencho_io_sweep_iteration\n'
    printf '__rc=$?\n'
    # shellcheck disable=SC2016  # Intentional: literal $__rc in the generated remote scriptlet
    printf 'exit "$__rc"\n'
    return 0
}

# Best-effort remote finalization used when local SSH orchestration turns an
# otherwise completed remote run into FAILED (for example, tee or retrieval).
_ssh_finalize_remote_generated_shared_failure() {
    local head_host="$1"
    local remote_basename="$2"
    local execution_id="$3"
    local nnnn_sh="$4"
    local scriptlet
    scriptlet=$(
        printf '#!/usr/bin/env bash\n'
        printf 'export ELBENCHO_FILE_LAYOUT=worker-directories\n'
        printf 'export ELBENCHO_FILES_PER_NODE=\n'
        printf 'export ELBENCHO_FILE_SIZE=\n'
        cat "$nnnn_sh"
        printf 'export ELBENCHO_RUN_EXECUTION_ID=%q\n' "$execution_id"
        printf 'export ELBENCHO_RUN_REMOTE_OUTPUT_DIR=%q\n' "$remote_basename"
        printf 'export output_dir=%q\n' "$remote_basename"
        printf 'source ./_elbencho_functions.sh || exit 1\n'
        printf '_elbencho_finalize_generated_shared_failure\n'
    ) || return 1
    local status_dir
    status_dir=$(mktemp -d) || return 1
    local rc_file="${status_dir}/cleanup.rc"
    local run_rc=0
    run_ssh_single "$head_host" "$rc_file" "/dev/stdout" "" \
        "$scriptlet" || run_rc=$?
    local remote_rc
    remote_rc=$(tr -d '\r\n' <"$rc_file" 2>/dev/null || echo 1)
    rm -rf "$status_dir"
    [[ "$remote_rc" =~ ^[0-9]+$ ]] || remote_rc=1
    if [[ "$run_rc" -ne 0 || "$remote_rc" -ne 0 ]]; then
        echo "ERROR: remote generated-target cleanup failed for execution ${execution_id}" >&2
        return 1
    fi
    return 0
}

# True only for an active generated shared-directory execution. Older reified
# files receive explicit legacy defaults before they are sourced.
_ssh_nnnn_is_generated_shared() {
    local nnnn_sh="$1"
    # shellcheck disable=SC2030  # Legacy defaults intentionally stay in this inspection subshell.
    (
        ELBENCHO_FILE_LAYOUT=worker-directories
        ELBENCHO_FILES_PER_NODE=
        ELBENCHO_FILE_SIZE=
        # shellcheck disable=SC1090
        source "$nnnn_sh" || exit 1
        [[ "${ELBENCHO_FILE_LAYOUT:-worker-directories}" == shared-directory \
                && -n "${ELBENCHO_FILES_PER_NODE:-}" \
                && -z "${ELBENCHO_SWEEP_READ_FROM:-}" ]]
    )
}

# Finalize the one SSH execution currently between RUNNING and SUCCESS. The
# guard is cleared before remote work so a second signal cannot recurse.
_ssh_finalize_active_generated_shared_execution() {
    if [[ "${ELBENCHO_SSH_ACTIVE_FINALIZATION_ARMED:-0}" != 1 ]]; then
        return 0
    fi
    ELBENCHO_SSH_ACTIVE_FINALIZATION_ARMED=0
    local status_file="${ELBENCHO_SSH_ACTIVE_STATUS_FILE:-}"
    if [[ -n "$status_file" \
            && "$(cat "$status_file" 2>/dev/null)" == SUCCESS ]]; then
        return 0
    fi
    _ssh_finalize_remote_generated_shared_failure \
        "$ELBENCHO_SSH_ACTIVE_HEAD_HOST" \
        "$ELBENCHO_SSH_ACTIVE_REMOTE_BASENAME" \
        "$ELBENCHO_SSH_ACTIVE_EXECUTION_ID" \
        "$ELBENCHO_SSH_ACTIVE_NNNN_SH" || true
    if [[ -n "$status_file" ]]; then
        _atomic_write_sentinel "$status_file" FAILED || \
            echo "Error: unable to record FAILED for active SSH execution ${ELBENCHO_SSH_ACTIVE_EXECUTION_ID}" >&2
    fi
    return 0
}

# Restore traps saved by _ssh_arm_active_generated_shared_finalization.
_ssh_disarm_active_generated_shared_finalization() {
    if [[ "${ELBENCHO_SSH_ACTIVE_TRAPS_INSTALLED:-0}" != 1 ]]; then
        return 0
    fi
    ELBENCHO_SSH_ACTIVE_FINALIZATION_ARMED=0
    ELBENCHO_SSH_ACTIVE_TRAPS_INSTALLED=0
    trap - EXIT INT TERM
    [[ -n "${ELBENCHO_SSH_OLD_EXIT_TRAP:-}" ]] \
        && eval "$ELBENCHO_SSH_OLD_EXIT_TRAP"
    [[ -n "${ELBENCHO_SSH_OLD_INT_TRAP:-}" ]] \
        && eval "$ELBENCHO_SSH_OLD_INT_TRAP"
    [[ -n "${ELBENCHO_SSH_OLD_TERM_TRAP:-}" ]] \
        && eval "$ELBENCHO_SSH_OLD_TERM_TRAP"
    return 0
}

# shellcheck disable=SC2317,SC2329  # Invoked by the active execution's EXIT trap.
_ssh_active_generated_shared_exit() {
    local original_rc="$?"
    local old_exit_trap="${ELBENCHO_SSH_OLD_EXIT_TRAP:-}"
    trap - EXIT INT TERM
    _ssh_finalize_active_generated_shared_execution
    # An EXIT trap cannot trigger a newly restored EXIT trap during the same
    # exit. Run the prior action in a child shell so filesystem cleanup and
    # other external side effects still occur without replacing original_rc.
    if [[ -n "$old_exit_trap" ]]; then
        (eval "$old_exit_trap"; exit "$original_rc") || true
    fi
    return "$original_rc"
}

# shellcheck disable=SC2317,SC2329  # Invoked by active execution signal traps.
_ssh_active_generated_shared_signal() {
    local signal_name="$1"
    local signal_rc=1
    [[ "$signal_name" == INT ]] && signal_rc=130
    [[ "$signal_name" == TERM ]] && signal_rc=143
    trap - EXIT INT TERM
    _ssh_finalize_active_generated_shared_execution
    # Restore the caller's EXIT trap before exiting with the signal status.
    # Its INT/TERM traps are restored for completeness but are not re-raised.
    [[ -n "${ELBENCHO_SSH_OLD_EXIT_TRAP:-}" ]] \
        && eval "$ELBENCHO_SSH_OLD_EXIT_TRAP"
    [[ -n "${ELBENCHO_SSH_OLD_INT_TRAP:-}" ]] \
        && eval "$ELBENCHO_SSH_OLD_INT_TRAP"
    [[ -n "${ELBENCHO_SSH_OLD_TERM_TRAP:-}" ]] \
        && eval "$ELBENCHO_SSH_OLD_TERM_TRAP"
    exit "$signal_rc"
}

# Arm local orchestration cleanup only for generated shared-directory work.
# Staged reads and legacy layouts retain their existing trap behavior.
_ssh_arm_active_generated_shared_finalization() {
    local head_host="$1"
    local remote_basename="$2"
    local execution_id="$3"
    local nnnn_sh="$4"
    local status_file="$5"
    if ! _ssh_nnnn_is_generated_shared "$nnnn_sh"; then
        return 0
    fi
    ELBENCHO_SSH_OLD_EXIT_TRAP=$(trap -p EXIT)
    ELBENCHO_SSH_OLD_INT_TRAP=$(trap -p INT)
    ELBENCHO_SSH_OLD_TERM_TRAP=$(trap -p TERM)
    ELBENCHO_SSH_ACTIVE_HEAD_HOST="$head_host"
    ELBENCHO_SSH_ACTIVE_REMOTE_BASENAME="$remote_basename"
    ELBENCHO_SSH_ACTIVE_EXECUTION_ID="$execution_id"
    ELBENCHO_SSH_ACTIVE_NNNN_SH="$nnnn_sh"
    ELBENCHO_SSH_ACTIVE_STATUS_FILE="$status_file"
    ELBENCHO_SSH_ACTIVE_FINALIZATION_ARMED=1
    ELBENCHO_SSH_ACTIVE_TRAPS_INSTALLED=1
    trap '_ssh_active_generated_shared_exit' EXIT
    trap '_ssh_active_generated_shared_signal INT' INT
    trap '_ssh_active_generated_shared_signal TERM' TERM
    return 0
}

# Tar only one execution's remote artifacts back to the local OUTPUT_DIR.
# Usage: _ssh_retrieve_remote_output <head_host> <output_dir> <remote_basename> <id> <nnnn.sh>
_ssh_retrieve_remote_output() {
    local head_host="$1"
    local output_dir="$2"
    local remote_basename="$3"
    local id="$4"
    local nnnn_sh="$5"

    local artifact_output
    if ! artifact_output=$(
        _elbencho_result_artifacts_for_execution "$output_dir" "$nnnn_sh"
    ); then
        return 1
    fi
    local -a artifacts=()
    mapfile -t artifacts <<<"$artifact_output"
    local -a remote_paths=()
    local artifact
    for artifact in "${artifacts[@]}"; do
        if [[ "$artifact" != "$output_dir/"* ]]; then
            echo "Error: result artifact is outside output directory: $artifact" >&2
            return 1
        fi
        remote_paths+=("${remote_basename}/${artifact#"$output_dir/"}")
    done
    remote_paths+=("${remote_basename}/executions/${id}.core")
    remote_paths+=("${remote_basename}/executions/${id}.treefile-cache")

    local parent
    parent=$(dirname "$output_dir")
    local status_dir
    status_dir=$(mktemp -d) || return 1
    local tar_rc_file="${status_dir}/tar.rc"

    # shellcheck disable=SC2016  # Variables expand in the remote bash process
    local archive_scriptlet='
existing=()
for path in "$@"; do
    [[ -e "$path" ]] && existing+=("$path")
done
[[ ${#existing[@]} -gt 0 ]] || exit 1
tar -czf - -- "${existing[@]}"
'
    local pipe_rc=0
    (
        cd "$parent" || exit 99
        run_ssh_single "$head_host" "$tar_rc_file" "/dev/stdout" "" \
            "$archive_scriptlet" "${remote_paths[@]}" | tar -xzf -
    ) || pipe_rc=$?
    local tar_rc
    tar_rc=$(tr -d '\r\n' <"$tar_rc_file" 2>/dev/null || echo 1)
    [[ "$tar_rc" =~ ^[0-9]+$ ]] || tar_rc=1
    rm -rf "$status_dir"
    if [[ "$pipe_rc" -ne 0 ]] || [[ "$tar_rc" -ne 0 ]]; then
        return 1
    fi
    local required_output
    required_output=$(_elbencho_required_result_artifacts_for_execution \
        "$output_dir" "$id" "$nnnn_sh") || return 1
    local required
    while IFS= read -r required; do
        [[ -n "$required" ]] || continue
        if [[ ! -f "$required" ]]; then
            echo "Error: required execution artifact was not retrieved: $required" >&2
            return 1
        fi
    done <<<"$required_output"
    return 0
}

# Dispatch one SSH execution end-to-end. Atomically updates status.
# Usage: _ssh_dispatch_one_execution <output_dir> <id>
# Returns: 0 on SUCCESS, non-zero on FAILED.
_ssh_dispatch_one_execution() {
    local output_dir="$1"
    local id="$2"
    local executions_dir="${output_dir}/executions"
    local nnnn_sh="${executions_dir}/${id}.sh"
    local status_file="${executions_dir}/${id}.status"
    local log_file="${executions_dir}/${id}.log"
    local exitcode_file="${executions_dir}/${id}.exitcode"

    local nodes_value
    if ! nodes_value=$(_ssh_extract_nodes_from_nnnn "$nnnn_sh"); then
        echo "Error: NNNN.sh ${id} did not export valid integer 'nodes'" >&2
        _atomic_write_sentinel "$status_file" FAILED
        return 1
    fi

    if ! choose_N_ssh_hosts "$nodes_value"; then
        echo "Error: choose_N_ssh_hosts failed for execution ${id} (nodes=${nodes_value})" >&2
        _atomic_write_sentinel "$status_file" FAILED
        return 1
    fi

    local remote_basename
    remote_basename=$(basename "$output_dir")
    local head_host="${SSH_NODELIST%%,*}"

    _atomic_write_sentinel "$status_file" RUNNING || return 1
    _ssh_arm_active_generated_shared_finalization \
        "$head_host" "$remote_basename" "$id" "$nnnn_sh" "$status_file"

    # Source NNNN.sh while computing target dirs so newly reified executions use
    # their saved per-execution directory suffix / target-dir state.
    local test_dirs_csv
    test_dirs_csv=$(
        # shellcheck disable=SC1090
        source "$nnnn_sh" || exit 1
        _compute_test_dirs_csv_for_execution
    ) || {
        _ssh_finalize_remote_generated_shared_failure \
            "$head_host" "$remote_basename" "$id" "$nnnn_sh" || true
        _atomic_write_sentinel "$status_file" FAILED
        _ssh_disarm_active_generated_shared_finalization
        return 1
    }

    if ! _cleanup_local_elbencho_result_artifacts_for_execution \
            "$output_dir" "$id" "$nnnn_sh"; then
        _ssh_finalize_remote_generated_shared_failure \
            "$head_host" "$remote_basename" "$id" "$nnnn_sh" || true
        _atomic_write_sentinel "$status_file" FAILED
        _ssh_disarm_active_generated_shared_finalization
        return 1
    fi

    local scriptlet
    if ! scriptlet=$(_ssh_build_execution_scriptlet \
            "$nnnn_sh" "$SSH_NODELIST" "$nodes_value" \
            "$test_dirs_csv" "$remote_basename" "$id"); then
        _ssh_finalize_remote_generated_shared_failure \
            "$head_host" "$remote_basename" "$id" "$nnnn_sh" || true
        _atomic_write_sentinel "$status_file" FAILED
        _ssh_disarm_active_generated_shared_finalization
        return 1
    fi

    local status_dir
    status_dir=$(mktemp -d) || {
        _ssh_finalize_remote_generated_shared_failure \
            "$head_host" "$remote_basename" "$id" "$nnnn_sh" || true
        _atomic_write_sentinel "$status_file" FAILED
        _ssh_disarm_active_generated_shared_finalization
        return 1
    }
    local rc_file="${status_dir}/run.rc"
    echo "[dispatch] starting execution ${id}: nodes=${nodes_value} hosts=${SSH_NODELIST}"
    local -a run_pipe_status=()
    run_ssh_single "$head_host" "$rc_file" "/dev/stdout" "" "$scriptlet" | tee "$log_file"
    run_pipe_status=("${PIPESTATUS[@]}")
    local runner_rc="${run_pipe_status[0]}"
    local tee_rc="${run_pipe_status[1]}"
    local rc
    rc=$(tr -d '\r\n' <"$rc_file" 2>/dev/null || echo 1)
    rm -rf "$status_dir"
    [[ "$rc" =~ ^[0-9]+$ ]] || rc=1

    if [[ "$rc" -eq 0 && "$runner_rc" -ne 0 ]]; then
        rc="$runner_rc"
    fi
    if [[ "$rc" -eq 0 && "$tee_rc" -ne 0 ]]; then
        rc="$tee_rc"
    fi
    # A runner/logging failure must clean generated data before any FAILED
    # sentinel. Retrieve afterward so cleanup metadata is part of the evidence.
    if [[ "$rc" -ne 0 ]]; then
        _ssh_finalize_remote_generated_shared_failure \
            "$head_host" "$remote_basename" "$id" "$nnnn_sh" || true
    fi

    # Always retrieve remote outputs. Keep an earlier execution/logging rc;
    # only promote success to failure if retrieval itself fails.
    if ! _ssh_retrieve_remote_output \
            "$head_host" "$output_dir" "$remote_basename" "$id" "$nnnn_sh"; then
        echo "Warning: tar retrieval failed for execution ${id}" >&2
        if [[ "$rc" -eq 0 ]]; then
            rc=1
            _ssh_finalize_remote_generated_shared_failure \
                "$head_host" "$remote_basename" "$id" "$nnnn_sh" || true
            # Cleanup changes workload metadata after the first retrieval
            # attempt, so make one best-effort evidence retrieval.
            _ssh_retrieve_remote_output \
                "$head_host" "$output_dir" "$remote_basename" "$id" \
                "$nnnn_sh" || true
        fi
    fi
    if [[ -f "${executions_dir}/${id}.treefile-cache" ]]; then
        update_elbencho_env_used_treefile_cache_usage "$output_dir" || \
            echo "Warning: failed to update treefile-cache records in env_used snapshots" >&2
    fi

    if [[ "$rc" -eq 0 ]]; then
        if _atomic_write_sentinel "$status_file" SUCCESS; then
            _ssh_disarm_active_generated_shared_finalization
            echo "[dispatch] execution ${id} SUCCESS"
        else
            rc=1
            echo "Error: unable to record SUCCESS for execution ${id}" >&2
            _ssh_finalize_remote_generated_shared_failure \
                "$head_host" "$remote_basename" "$id" "$nnnn_sh" || true
            _ssh_retrieve_remote_output \
                "$head_host" "$output_dir" "$remote_basename" "$id" \
                "$nnnn_sh" || true
            _atomic_write_sentinel "$status_file" FAILED || \
                echo "Error: unable to record FAILED for execution ${id}" >&2
            _ssh_disarm_active_generated_shared_finalization
        fi
    else
        _atomic_write_sentinel "$status_file" FAILED || \
            echo "Error: unable to record FAILED for execution ${id}" >&2
        _ssh_disarm_active_generated_shared_finalization
        echo "[dispatch] execution ${id} FAILED (rc=$rc); see ${log_file}"
    fi
    printf '%s\n' "$rc" > "$exitcode_file"
    return "$rc"
}

# Ensure the warp binary is present on required SSH nodes by copying it via stdin piping
# - If SSH_HOMEDIR_SHARED is non-empty: copy to 1 node
# - Otherwise: copy to all nodes in SSH_ALL_HOSTS
# Returns 0 if all SSH commands exited 0, otherwise 1
ensure_all_ssh_nodes_can_warp() {
    # Validate WARP path
    if [[ -z "${WARP:-}" || ! -f "$WARP" ]]; then
        echo "Error: WARP is not set to a readable file: '$WARP'" >&2
        return 1
    fi

    # Determine node count
    local node_count
    if [[ -n "${SSH_HOMEDIR_SHARED:-}" ]]; then
        node_count=1
    else
        node_count=${#SSH_ALL_HOSTS[@]}
    fi

    # Require at least one node
    if [[ -z "$node_count" || "$node_count" -lt 1 ]]; then
        echo "Error: No SSH hosts available to copy warp to (node_count=$node_count)" >&2
        return 1
    fi

    # Create a temporary status directory
    local status_dir
    status_dir=$(mktemp -d) || return 1

    # Spawn the SSH copy operations; combine stdout/stderr to simplify handling
    choose_N_ssh_hosts "$node_count"
    local cmd='(type -P pkill >/dev/null 2>&1 && pkill warp >/dev/null 2>&1 || killall warp >/dev/null 2>&1) && sleep 2; cp /dev/stdin warp && chmod a+x warp'
    if ! copy_a_file_over_ssh "$status_dir" "$WARP" "$cmd"; then
        echo "Error: Failed to copy warp to all nodes" >&2
        rm -rf "$status_dir"
        return 1
    fi

    # --- Copy _warp_functions.sh to all nodes in the same way ---
    local sweep_script="${SCALE_TEST_BASE}/lib/_warp_functions.sh"
    if [[ ! -f "$sweep_script" ]]; then
        echo "Error: warp functions script not found at '$sweep_script'" >&2
        rm -rf "$status_dir"
        return 1
    fi

    if ! copy_a_file_over_ssh "$status_dir" "$sweep_script" "cp /dev/stdin _warp_functions.sh"; then
        echo "Error: Failed to copy warp functions script to all nodes" >&2
        rm -rf "$status_dir"
        return 1
    fi

    # --- Copy $OBJ_AUTH_FILE to all nodes in the same way ---
    if [[ -n "${OBJ_AUTH_FILE:-}" && -f "$OBJ_AUTH_FILE" ]]; then
        if ! copy_a_file_over_ssh "$status_dir" "$OBJ_AUTH_FILE" "/bin/bash -c 'umask 077 && cp /dev/stdin .obj_auth'"; then
            echo "Error: Failed to copy OBJ_AUTH_FILE to all nodes" >&2
            rm -rf "$status_dir"
            return 1
        fi
    fi

    if ! build_s3test_on_selected_ssh_nodes; then
        echo "Error: Failed to build s3test on selected ssh nodes" >&2
        rm -rf "$status_dir"
        return 1
    fi

    rm -rf "$status_dir"
    return 0
}

# Parse a range specification string into a list of integer values
# This function supports flexible sweep specifications for node counts, task counts, etc.
#
# Usage:
#   mapfile -t values < <(parse_range_specification "$spec")
#
# Arguments:
#   $1 - specification string (comma-separated list of elements)
#
# Element formats:
#   X        - Single value (e.g., "5" outputs: 5)
#   X-Y      - Range from X to Y with increment 1 (e.g., "2-5" outputs: 2 3 4 5)
#   X-Y+Z    - Range from X to Y with increment Z (e.g., "1-10+3" outputs: 1 4 7 10)
#
# Multiple elements can be comma-separated and will execute in order:
#   "1,4,8"        outputs: 1 4 8
#   "1-4,16"       outputs: 1 2 3 4 16
#   "16,8,4,2,1"   outputs: 16 8 4 2 1 (descending)
#   "3-10+2,13"    outputs: 3 5 7 9 10 13
#
# Output:
#   Newline-delimited list of integers (one per line) to stdout
#
# Returns:
#   0 on success, 1 on error (with error message to stderr)
#
# Validation:
#   - All numbers must be positive integers (>= 1)
#   - For ranges X-Y[+Z]: X must be <= Y (X == Y produces single value)
#   - For ranges X-Y+Z: Z must be positive
#   - Empty specification is an error
#   - Leading/trailing commas are errors
#   - Multiple consecutive commas are errors
#   - Commas ONLY separate elements, not used within ranges
#
# Examples:
#   parse_range_specification "1,2,3"           → 1\n2\n3
#   parse_range_specification "1-5"             → 1\n2\n3\n4\n5
#   parse_range_specification "1-10+3"          → 1\n4\n7\n10
#   parse_range_specification "3-10+2"          → 3\n5\n7\n9\n10
#   parse_range_specification "1-1"             → 1
#   parse_range_specification "1-10+20"         → 1\n10 (increment exceeds range)
#   parse_range_specification "3-6,8,10-12"     → 3\n4\n5\n6\n8\n10\n11\n12
#   parse_range_specification "3-10+2,13,20-100+20"  → 3\n5\n7\n9\n10\n13\n20\n40\n60\n80\n100
parse_range_specification() {
    local spec="$1"

    # Validate spec is not empty
    if [[ -z "$spec" ]]; then
        echo "Error: Empty specification" >&2
        return 1
    fi

    # Check for leading comma
    if [[ "$spec" =~ ^, ]]; then
        echo "Error: Specification starts with comma: '$spec'" >&2
        return 1
    fi

    # Check for trailing comma
    if [[ "$spec" =~ ,$ ]]; then
        echo "Error: Specification ends with comma: '$spec'" >&2
        return 1
    fi

    # Check for double commas
    if [[ "$spec" =~ ,, ]]; then
        echo "Error: Specification contains consecutive commas: '$spec'" >&2
        return 1
    fi

    # Split on commas and process each element
    local IFS=','
    local -a elements
    read -ra elements <<< "$spec"

    for element in "${elements[@]}"; do
        # Skip empty elements (shouldn't happen after validation above, but be safe)
        [[ -z "$element" ]] && continue

        # Check if element is a range (contains hyphen)
        if [[ "$element" =~ ^([0-9]+)-([0-9]+)(\+([0-9]+))?$ ]]; then
            # Range format: X-Y or X-Y+Z
            local start="${BASH_REMATCH[1]}"
            local stop="${BASH_REMATCH[2]}"
            local increment="${BASH_REMATCH[4]}"

            # Default increment is 1 if not specified
            if [[ -z "$increment" ]]; then
                increment=1
            fi

            # Validate start is positive
            if [[ "$start" -lt 1 ]]; then
                echo "Error: Range start must be >= 1 (got $start in '$element')" >&2
                return 1
            fi

            # Validate stop is positive
            if [[ "$stop" -lt 1 ]]; then
                echo "Error: Range stop must be >= 1 (got $stop in '$element')" >&2
                return 1
            fi

            # Validate increment is positive
            if [[ "$increment" -lt 1 ]]; then
                echo "Error: Range increment must be >= 1 (got $increment in '$element')" >&2
                return 1
            fi

            # Validate start <= stop
            if [[ "$start" -gt "$stop" ]]; then
                echo "Error: Range start must be <= stop (got $start-$stop in '$element')" >&2
                return 1
            fi

            # Output the range
            # Always output start
            echo "$start"

            # If start == stop, we're done with this range
            if [[ "$start" -eq "$stop" ]]; then
                continue
            fi

            # Step through middle values
            local current=$((start + increment))
            while [[ $current -lt $stop ]]; do
                echo "$current"
                current=$((current + increment))
            done

            # Always output stop (we know stop != start from above)
            echo "$stop"

        elif [[ "$element" =~ ^[0-9]+$ ]]; then
            # Single value
            local value="$element"

            # Validate value is positive
            if [[ "$value" -lt 1 ]]; then
                echo "Error: Value must be >= 1 (got $value)" >&2
                return 1
            fi

            echo "$value"

        else
            # Invalid format
            echo "Error: Invalid element format: '$element' (must be X, X-Y, or X-Y+Z where X,Y,Z are positive integers)" >&2
            return 1
        fi
    done

    return 0
}

# Validate that a bash array variable contains at least one integer element
# Usage: validate_integer_array var_name
# Returns: 0 if valid, 1 if invalid (with error messages to stderr)
validate_integer_array() {
    local var_name="$1"
    local -n arr_ref="$var_name"

    # Check if array is declared
    if ! declare -p "$var_name" &>/dev/null; then
        echo "Error: ${var_name} is not defined" >&2
        return 1
    fi

    # Check if array has at least one element
    if [[ ${#arr_ref[@]} -eq 0 ]]; then
        echo "Error: ${var_name} must be a bash array with at least one element" >&2
        echo "  Example: ${var_name}=(\"1\" \"2\" \"4\")" >&2
        return 1
    fi

    # Validate each element is a positive integer. Check an all-zero string
    # without shell arithmetic so an unusually large value cannot overflow.
    local idx=0
    for elem in "${arr_ref[@]}"; do
        if ! [[ "$elem" =~ ^[0-9]+$ ]] || [[ "$elem" =~ ^0+$ ]]; then
            echo "Error: ${var_name}[${idx}]='${elem}' is not a positive integer" >&2
            echo "  All elements must be positive integers (e.g., \"1\", \"32\", \"128\")" >&2
            return 1
        fi
        idx=$((idx + 1))
    done

    return 0
}

# Print the largest positive integer supported by this Bash arithmetic build.
# The sequence reaches 2^N-1 before the next operation wraps negative.
_elbencho_shell_signed_integer_max() {
    local current=1
    local next
    while true; do
        next=$((current * 2 + 1))
        if [[ "$next" -le "$current" ]]; then
            printf '%s' "$current"
            return 0
        fi
        current="$next"
    done
}

# Return success when a canonical positive decimal string is larger than the
# shell's signed integer maximum. The caller validates the decimal grammar.
_elbencho_decimal_exceeds_shell_integer_max() {
    local value="$1"
    local maximum
    maximum=$(_elbencho_shell_signed_integer_max) || return 1
    if [[ ${#value} -gt ${#maximum} ]]; then
        return 0
    fi
    if [[ ${#value} -lt ${#maximum} ]]; then
        return 1
    fi
    local LC_ALL=C
    [[ "$value" > "$maximum" ]]
}

# Validate generated many-file controls whose validity is independent of CLI
# mode. Mode-aware requirements (staged reads, files per worker, targets, and
# I/O exactness) are validated by the elbencho sweep after parsing its command
# line.
# shellcheck disable=SC2031  # Reads caller configuration; unrelated finalizer subshell assignments do not escape.
validate_elbencho_file_workload_env() {
    local worker_layout=worker-directories
    local shared_layout=shared-directory
    local layout="${ELBENCHO_FILE_LAYOUT:-$worker_layout}"
    local files_per_node="${ELBENCHO_FILES_PER_NODE:-}"
    local file_size="${ELBENCHO_FILE_SIZE:-}"
    local validation_failed=0

    if [[ "$layout" != "$worker_layout" && "$layout" != "$shared_layout" ]]; then
        echo "Error: ELBENCHO_FILE_LAYOUT='${layout}' must be '${worker_layout}' or '${shared_layout}'" >&2
        validation_failed=1
    fi

    if [[ -n "$files_per_node" ]]; then
        if ! [[ "$files_per_node" =~ ^[1-9][0-9]*$ ]]; then
            echo "Error: ELBENCHO_FILES_PER_NODE='${files_per_node}' must be a canonical positive decimal integer" >&2
            echo "  Use digits without a sign or leading zeros (for example, 1 or 8)." >&2
            validation_failed=1
        elif _elbencho_decimal_exceeds_shell_integer_max "$files_per_node"; then
            local maximum
            maximum=$(_elbencho_shell_signed_integer_max) || return 1
            echo "Error: ELBENCHO_FILES_PER_NODE='${files_per_node}' exceeds this shell's signed integer maximum (${maximum})" >&2
            validation_failed=1
        fi
        if [[ "$layout" != "$shared_layout" ]]; then
            echo "Error: ELBENCHO_FILES_PER_NODE requires ELBENCHO_FILE_LAYOUT='${shared_layout}'" >&2
            validation_failed=1
        fi
    fi

    if [[ -n "$file_size" ]] && ! [[ "$file_size" =~ ^[1-9][0-9]*[KMG]$ ]]; then
        echo "Error: ELBENCHO_FILE_SIZE='${file_size}' must match [1-9][0-9]*[KMG] (for example, 64G)" >&2
        validation_failed=1
    fi

    [[ "$validation_failed" -eq 0 ]]
}

# Validate ELBENCHO_SCALE_IO_SIZES array
# Each element must match: ^r?\d+[KMG](,r?\d+[KMG])?$
# Usage: validate_elbencho_io_sizes
# Returns: 0 if valid, 1 if invalid (with error messages to stderr)
validate_elbencho_io_sizes() {
    local var_name="ELBENCHO_SCALE_IO_SIZES"
    local -n arr_ref="$var_name"

    # Check if array is declared
    if ! declare -p "$var_name" &>/dev/null; then
        echo "Error: ${var_name} is not defined" >&2
        return 1
    fi

    # Check if array has at least one element
    if [[ ${#arr_ref[@]} -eq 0 ]]; then
        echo "Error: ${var_name} must be a bash array with at least one element" >&2
        echo "  Example: ${var_name}=(\"4K\" \"1M\" \"r4K\" \"1M,4K\")" >&2
        return 1
    fi

    # Validate each element matches the IO size pattern
    local idx=0
    local pattern='^r?[0-9]+[KMG](,r?[0-9]+[KMG])?$'
    for elem in "${arr_ref[@]}"; do
        if ! [[ "$elem" =~ $pattern ]]; then
            echo "Error: ${var_name}[${idx}]='${elem}' has invalid format" >&2
            echo "  Format must be: [r]<SIZE>[KMG] or [r]<WRITE_SIZE>[KMG],[r]<READ_SIZE>[KMG]" >&2
            echo "  Examples: \"4K\", \"r4K\", \"1M,4K\", \"r1M,r4K\"" >&2
            echo "  No spaces allowed, only one optional comma allowed" >&2
            return 1
        fi
        ((idx++))
    done

    return 0
}

# Validate ELBENCHO_SCALE_READ_WRITE_DURATION is a positive integer
# Usage: validate_elbencho_duration
# Returns: 0 if valid, 1 if invalid (with error messages to stderr)
validate_elbencho_duration() {
    local var_name="ELBENCHO_SCALE_READ_WRITE_DURATION"
    local value="${!var_name:-}"

    # Check if variable is set
    if [[ -z "$value" ]]; then
        echo "Error: ${var_name} is not defined" >&2
        echo "  Set it to a positive integer (duration in seconds)" >&2
        echo "  Example: export ${var_name}=60" >&2
        return 1
    fi

    # Check if it's a positive integer
    if ! [[ "$value" =~ ^[0-9]+$ ]] || [[ "$value" -le 0 ]]; then
        echo "Error: ${var_name}='${value}' must be a positive integer > 0" >&2
        echo "  Example: export ${var_name}=60" >&2
        return 1
    fi

    return 0
}

# Validate optional elbencho live CSV settings.
# Unset variables use defaults so existing env.sh files remain compatible.
# Usage: validate_elbencho_live_csv
# Returns: 0 if valid, 1 if invalid (with messages to stderr)
validate_elbencho_live_csv() {
    local livecsvex="${ELBENCHO_LIVE_CSV_EXTENDED:-0}"
    local liveint="${ELBENCHO_LIVEINT:-1000}"

    if [[ "$livecsvex" != "0" && "$livecsvex" != "1" ]]; then
        echo "Error: ELBENCHO_LIVE_CSV_EXTENDED='${livecsvex}' must be 0 or 1" >&2
        return 1
    fi

    if ! [[ "$liveint" =~ ^[0-9]+$ ]] || [[ "$liveint" -le 0 ]]; then
        echo "Error: ELBENCHO_LIVEINT='${liveint}' must be a positive integer > 0" >&2
        return 1
    fi

    if [[ "$livecsvex" == "1" && "$liveint" -lt 250 ]]; then
        echo "Warning: ELBENCHO_LIVEINT=${liveint} may add significant capture overhead" >&2
    fi

    return 0
}

# Return the sole TEST_DIRS key (requires exactly one key). Prints path to stdout.
_elbencho_sweep_single_test_dirs_key() {
    if [[ ${#TEST_DIRS[@]} -ne 1 ]]; then
        return 1
    fi
    local k
    for k in "${!TEST_DIRS[@]}"; do
        printf '%s' "$k"
        return 0
    done
    return 1
}

# Validate exactly one TEST_DIRS entry for elbencho sweep special flags.
# Usage: validate_elbencho_sweep_single_test_dirs_key
# Returns 0 if OK, 1 with stderr message otherwise.
validate_elbencho_sweep_single_test_dirs_key() {
    if ! _elbencho_sweep_single_test_dirs_key >/dev/null; then
        echo "Error: --write-only, --read-from, and --delete-only require exactly one path in TEST_DIRS (one key in the TEST_DIRS map). Found ${#TEST_DIRS[@]} key(s)." >&2
        return 1
    fi
    return 0
}

# Require generate_fs_test_directories to yield exactly one directory (weight 1 on sole key).
# Usage: validate_elbencho_sweep_one_generated_target_dir
validate_elbencho_sweep_one_generated_target_dir() {
    local -a gtd
    if ! mapfile -t gtd < <(generate_fs_test_directories "elbencho-sweep"); then
        echo "Error: Failed to generate elbencho-sweep test directories." >&2
        return 1
    fi
    if [[ ${#gtd[@]} -ne 1 ]]; then
        echo "Error: --write-only and --read-from require exactly one generated target directory (set TEST_DIRS weight to 1 for the sole filesystem path). Got ${#gtd[@]} path(s)." >&2
        return 1
    fi
    return 0
}

# True if an elbencho IO size string uses random (leading r on a component).
# Usage: _elbencho_io_size_string_has_random <io_size>
_elbencho_io_size_string_has_random() {
    local io_size="$1"
    local part
    local -a _parts
    IFS=',' read -ra _parts <<< "$io_size"
    for part in "${_parts[@]}"; do
        if [[ "$part" == r* ]]; then
            return 0
        fi
    done
    return 1
}

# Validate ELBENCHO_SINGLE_BIG_FILE=1 constraints (sequential I/O only, one TEST_DIRS key).
# Usage: validate_elbencho_single_big_file_env [sweep_read_from_path]
# Optional sweep_read_from_path: when non-empty (nv-elbencho-sweep --read-from), ELBENCHO_SINGLE_BIG_FILE_SIZE
# may be unset; the read phase omits elbencho --size (extent from file metadata for single-big-file path reads).
# Returns 0 if OK, 1 with stderr message otherwise.
validate_elbencho_single_big_file_env() {
    local sweep_read_from_path="${1:-}"
    if [[ "${ELBENCHO_SINGLE_BIG_FILE:-0}" != "1" ]]; then
        return 0
    fi
    if ! validate_elbencho_sweep_single_test_dirs_key; then
        return 1
    fi
    local sz
    for sz in "${ELBENCHO_SCALE_IO_SIZES[@]}"; do
        if _elbencho_io_size_string_has_random "$sz"; then
            echo "Error: ELBENCHO_SINGLE_BIG_FILE=1 requires sequential IO sizes (no 'r' prefix in ELBENCHO_SCALE_IO_SIZES). Offending entry: '$sz'" >&2
            return 1
        fi
    done
    if [[ -z "${ELBENCHO_SINGLE_BIG_FILE_SIZE:-}" ]]; then
        if [[ -n "$sweep_read_from_path" ]]; then
            return 0
        fi
        echo "Error: ELBENCHO_SINGLE_BIG_FILE=1 requires ELBENCHO_SINGLE_BIG_FILE_SIZE (elbencho --size for write/read phases). Omit only when using nv-elbencho-sweep.sh --read-from (read omits --size; extent from file)." >&2
        return 1
    fi
    return 0
}

# True if resolved path equals base or is a descendant (for --read-from).
# Usage: _elbencho_path_under_or_equal_test_root <user_path> <base_path>
_elbencho_path_under_or_equal_test_root() {
    local user_path="$1"
    local base_path="$2"
    local rp_user rp_base
    rp_user=$(_portable_realpath_m "$user_path") || return 1
    rp_base=$(_portable_realpath_m "$base_path") || return 1
    [[ "$rp_user" == "$rp_base" ]] || [[ "$rp_user" == "$rp_base"/* ]]
}

# True if resolved path is a strict subdirectory of base (for --delete-only; not the root itself).
# Usage: _elbencho_path_strict_subdir_of_test_root <user_path> <base_path>
_elbencho_path_strict_subdir_of_test_root() {
    local user_path="$1"
    local base_path="$2"
    local rp_user rp_base
    rp_user=$(_portable_realpath_m "$user_path") || return 1
    rp_base=$(_portable_realpath_m "$base_path") || return 1
    [[ "$rp_user" == "$rp_base"/* ]]
}

# Validate --read-from path is under TEST_DIRS (may equal mount root).
# Usage: validate_elbencho_sweep_read_from_path <path>
validate_elbencho_sweep_read_from_path() {
    local read_path="$1"
    local base
    base=$(_elbencho_sweep_single_test_dirs_key) || {
        echo "Error: internal: TEST_DIRS key lookup failed" >&2
        return 1
    }
    if ! _elbencho_path_under_or_equal_test_root "$read_path" "$base"; then
        echo "Error: --read-from path must be the TEST_DIRS path or a subdirectory of it." >&2
        echo "  Got: $read_path" >&2
        echo "  Expected under: $base" >&2
        return 1
    fi
    return 0
}

# ELBENCHO_SINGLE_BIG_FILE=1 + --read-from requires a file path (not a directory on this host).
# Usage: validate_elbencho_single_big_file_read_from_is_not_dir <read_from_path>
validate_elbencho_single_big_file_read_from_is_not_dir() {
    local read_path="$1"
    if [[ "${ELBENCHO_SINGLE_BIG_FILE:-0}" != "1" || -z "$read_path" ]]; then
        return 0
    fi
    if [[ -d "$read_path" ]]; then
        echo "Error: ELBENCHO_SINGLE_BIG_FILE=1 with --read-from requires a file path, not a directory." >&2
        echo "  Got directory: $read_path" >&2
        echo "  Pass the file path; elbencho read uses that path (no directory treescan in this mode). Use ELBENCHO_SINGLE_BIG_FILE=0 to read a directory tree with treescan." >&2
        return 1
    fi
    return 0
}

# Validate --delete-only path is a strict subdirectory under TEST_DIRS (not the bare root).
# Usage: validate_elbencho_sweep_delete_only_path <path>
validate_elbencho_sweep_delete_only_path() {
    local del_path="$1"
    local base
    base=$(_elbencho_sweep_single_test_dirs_key) || {
        echo "Error: internal: TEST_DIRS key lookup failed" >&2
        return 1
    }
    if ! _elbencho_path_strict_subdir_of_test_root "$del_path" "$base"; then
        echo "Error: --delete-only path must be a strict subdirectory under TEST_DIRS (not the filesystem test root itself)." >&2
        echo "  Got: $del_path" >&2
        echo "  Must be under (and not equal to): $base" >&2
        return 1
    fi
    return 0
}

# Function to ensure no processes with the given binary name are running (used for cleanup)
# Usage: ensure_no_processes_running_srun binary_name
# Arguments:
#   binary_name: name of the binary to kill (e.g., "warp", "elbencho")
ensure_no_processes_running_srun() {
    local binary="$1"
    # Ensure no "$binary" processes are running on any node in the allocation
    # Use --overlap to allow this to run concurrently with other job steps if needed
    # shellcheck disable=SC2016  # Single quotes intentional - variables expanded by inner bash, not outer
    srun --overlap --ntasks="$SLURM_JOB_NUM_NODES" --ntasks-per-node=1 bash -c '
        bin="$1"
        if type -P pkill >/dev/null 2>&1; then
            pkill "$bin" || true
            sleep 2
            pkill -9 "$bin" || true
        elif type -P killall >/dev/null 2>&1; then
            killall "$bin" || true
            sleep 2
            killall -9 "$bin" || true
        else
            echo "ERROR: Neither pkill nor killall found on $HOSTNAME"
        fi
    ' bash "$binary"
}

# Function to ensure specified process is listening on a given port across allocation
# Usage: ensure_processes_running_srun binary_name port
# Arguments:
#   binary_name: name of the binary to check (e.g., "warp", "elbencho")
#   port: port number to check (e.g., 7761)
# Exits with status 1 if validation fails
ensure_processes_running_srun() {
    local binary="$1"
    local port="$2"

    # shellcheck disable=SC2016  # Single quotes - variables expanded by innermost shell (not this scope)
    if ! srun --overlap --ntasks="$SLURM_JOB_NUM_NODES" --ntasks-per-node=1 bash -c '
        bin="$1"
        port="$2"
        if type -P ss >/dev/null 2>&1; then
            # Match 0.0.0.0:port or *:port (where * means all interfaces)
            if ss -ltnp | grep -qE "(0\.0\.0\.0|\\*):${port}"; then
                echo "[OK] $bin process listening on 0.0.0.0:${port} on $(hostname)"
                exit 0
            else
                echo "[FAIL] $bin process not listening on 0.0.0.0:${port} on $(hostname)" >&2
                ps -efww | grep "[${bin:0:1}]${bin:1}" >&2
                exit 1
            fi
        elif type -P netstat >/dev/null 2>&1; then
            # netstat always shows 0.0.0.0:PORT (not *)
            if netstat -ltnp 2>/dev/null | grep -q "0.0.0.0:${port}"; then
                echo "[OK] $bin process listening on 0.0.0.0:${port} on $(hostname)"
                exit 0
            else
                echo "[FAIL] $bin process not listening on 0.0.0.0:${port} on $(hostname)" >&2
                ps -efww | grep "[${bin:0:1}]${bin:1}" >&2
                exit 1
            fi
        else
            echo "Cannot find ss or netstat to check for listening port ${port} on $(hostname)" >&2
            exit 1
        fi
    ' bash "$binary" "$port"; then
        echo >&2
        echo "ERROR: One or more nodes in the allocation failed to validate that a $binary process is listening on 0.0.0.0:$port." >&2
        echo "       Check the log messages above for which node(s) failed." >&2
        echo "       Aborting sweep for safety!" >&2
        exit 1
    fi
}

# Start background clients using srun across all nodes in the allocation
# Usage: pid=$(start_background_clients_srun command [args...])
# Arguments:
#   command [args...]: command and arguments to run via srun on each node
# Returns: PID of the backgrounded srun process
# The srun process runs in the background and manages the processes on all nodes
# Uses --overlap to allow multiple concurrent sruns in the same job allocation
start_background_clients_srun() {
    srun --overlap --ntasks="$SLURM_JOB_NUM_NODES" --ntasks-per-node=1 \
        "$@" < /dev/null > /dev/null 2>&1 &
    echo $!
}

# Stop background clients by killing the srun process
# Usage: stop_background_clients_srun pid
# Arguments:
#   pid: PID of the backgrounded srun process to stop
# This will cause srun to cleanly tear down all processes it manages on all nodes
stop_background_clients_srun() {
    local pid="$1"
    if [[ -z "$pid" ]]; then
        return 0
    fi
    if kill -0 "$pid" 2>/dev/null; then
        # Send SIGTERM first to allow srun to clean up gracefully
        kill -TERM "$pid" 2>/dev/null || true
        sleep 2
        # If still running, force kill with SIGKILL
        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL "$pid" 2>/dev/null || true
            sleep 1
        fi
        # Wait for the process to exit
        wait "$pid" 2>/dev/null || true
    fi
}

# =============================================================================
# Elbencho Service Management for SLURM
# =============================================================================
# Note: Unlike the SSH case, SLURM does not support per-node watchdogs because
# when an srun step exits, SLURM's cgroup tracking kills all spawned processes.
# Instead, we keep srun running in the background to manage the services.
# For self-healing in SLURM, consider adding coordinator-side health checks.
# =============================================================================

# Start elbencho services on all SLURM nodes (srun stays running in background)
# Usage: pid=$(start_elbencho_services_srun [port] [log_dir])
#   port    - Optional port number (default: 1611)
#   log_dir - Optional directory for service log (default: /tmp)
# Returns: PID of the backgrounded srun process (save this to stop services later)
start_elbencho_services_srun() {
    local port="${1:-}"
    local log_dir="${2:-/tmp}"
    local svc_log="${log_dir}/elbencho-svc-j${SLURM_JOB_ID:-0}-port${port:-default}.log"

    local srun_args=("$ELBENCHO" --service --foreground)
    if [[ -n "$port" ]]; then
        srun_args+=(--port "$port")
    fi

    # Start srun in background - it will manage the elbencho processes
    # When we kill this srun PID later, it will clean up all elbencho processes
    # --kill-on-bad-exit=0 prevents a single service crash from killing all services
    echo "  (service log: $svc_log)" >&2
    srun --overlap --kill-on-bad-exit=0 \
        --ntasks="$SLURM_JOB_NUM_NODES" --ntasks-per-node=1 \
        "${srun_args[@]}" </dev/null >"$svc_log" 2>&1 &
    echo $!
    return 0
}

# Check elbencho services are responding on all SLURM nodes via HTTP /status.
# The first probe is immediate. Failed probes retry within one overall deadline;
# GNU timeout bounds each srun to the remaining time so one stuck step cannot hang.
# Usage: check_elbencho_services_srun [port [max_wait]]
#   port     - Port number to check (default: 1611)
#   max_wait - Overall probe deadline in seconds (default: 60)
# Returns: 0 if all nodes healthy, 1 if any nodes unhealthy
check_elbencho_services_srun() {
    local port="${1:-1611}"
    local max_wait="${2:-60}"
    local started="$SECONDS"
    local deadline
    local attempt=0
    local remaining
    local check_output=""
    local srun_rc=0
    local ok_count=0
    local fail_count=0
    local check_output_file

    if [[ ! "$max_wait" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: elbencho service max_wait must be a positive integer: $max_wait" >&2
        return 2
    fi
    deadline=$((started + max_wait))
    check_output_file=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-health.XXXXXX") \
        || return 1

    while [[ $SECONDS -lt $deadline ]]; do
        attempt=$((attempt + 1))
        remaining=$((deadline - SECONDS))

        # Run HTTP /status check on each node
        # Only capture our [OK]/[FAIL] messages, suppress bash errors and srun noise
        # shellcheck disable=SC2016  # Single quotes intentional for remote execution
        if _run_command_with_timeout "$remaining" \
                srun --overlap --ntasks="$SLURM_JOB_NUM_NODES" \
                    --ntasks-per-node=1 bash -c '
                        port="$1"
                        response=$(
                            exec 3<>/dev/tcp/127.0.0.1/"$port" 2>/dev/null && \
                            echo -e "GET /status HTTP/1.0\r\nHost: 127.0.0.1:$port\r\n\r\n" >&3 && \
                            cat <&3 2>/dev/null
                            exec 3>&- 2>/dev/null
                        ) 2>/dev/null
                        if [[ "$response" == *"200"* ]]; then
                            echo "[OK] $(hostname)"
                            exit 0
                        else
                            echo "[FAIL] $(hostname)"
                            exit 1
                        fi
                    ' bash "$port" </dev/null >"$check_output_file" 2>/dev/null; then
            srun_rc=0
        else
            srun_rc=$?
        fi
        check_output=$(<"$check_output_file")

        ok_count=$(echo "$check_output" | grep -c '^\[OK\]' || true)
        fail_count=$(echo "$check_output" | grep -c '^\[FAIL\]' || true)
        if [[ $srun_rc -eq 0 ]]; then
            if [[ "$ok_count" -ge "$SLURM_JOB_NUM_NODES" ]]; then
                local elapsed=$((SECONDS - started))
                _echo_ts "elbencho services ready on port $port after ${elapsed}s ($ok_count/$SLURM_JOB_NUM_NODES nodes)"
                rm -f -- "$check_output_file"
                return 0
            fi
            echo "Warning: srun OK but only $ok_count/$SLURM_JOB_NUM_NODES nodes responded (port $port, attempt $attempt, ${max_wait}s deadline)"
        # GNU timeout returns 124; retain the defensive handling for a process
        # killed while enforcing the deadline.
        elif [[ $srun_rc -eq 124 || $srun_rc -eq 125 || $srun_rc -eq 137 ]] \
             || [[ $SECONDS -ge $deadline ]]; then
            echo "Warning: elbencho service probe reached its ${max_wait}s deadline (port $port, attempt $attempt)" >&2
        fi

        # Report the first concrete failure set. The final set is reported below.
        if [[ $attempt -eq 1 && $fail_count -gt 0 ]]; then
            local failed_nodes
            failed_nodes=$(echo "$check_output" | grep '^\[FAIL\]' | sed 's/\[FAIL\] //' | paste -sd, -)
            echo "Port $port: $ok_count OK, $fail_count FAILED (attempt $attempt, ${max_wait}s deadline): $failed_nodes"
        fi

        if [[ $SECONDS -lt $deadline ]]; then
            sleep 1
        fi
    done

    if [[ $attempt -gt 1 && $fail_count -gt 0 ]]; then
        local failed_nodes
        failed_nodes=$(echo "$check_output" | grep '^\[FAIL\]' | sed 's/\[FAIL\] //' | paste -sd, -)
        echo "Port $port: $ok_count OK, $fail_count FAILED (final attempt $attempt): $failed_nodes"
    fi
    echo "Error: elbencho services not responding on port $port after ${max_wait}s" >&2
    rm -f -- "$check_output_file"
    return 1
}

# Stop elbencho services by killing the srun process that manages them
# Usage: stop_elbencho_services_srun srun_pid [port]
#   srun_pid - PID of the srun process returned by start_elbencho_services_srun
#   port     - Port for additional cleanup (default: 1611)
stop_elbencho_services_srun() {
    local srun_pid="${1:-}"
    local port="${2:-1611}"

    # Kill the srun process which will clean up all elbencho processes it manages
    if [[ -n "$srun_pid" ]] && kill -0 "$srun_pid" 2>/dev/null; then
        kill -TERM "$srun_pid" 2>/dev/null || true
        sleep 2
        if kill -0 "$srun_pid" 2>/dev/null; then
            kill -KILL "$srun_pid" 2>/dev/null || true
            sleep 1
        fi
        wait "$srun_pid" 2>/dev/null || true
    fi

    # Fallback cleanup: kill only elbencho processes on the specific port
    # Use fuser to target the exact port, avoiding killing services on other ports
    # shellcheck disable=SC2016  # Single quotes intentional
    srun --overlap --ntasks="$SLURM_JOB_NUM_NODES" --ntasks-per-node=1 \
        bash -c '
            port="$1"
            # Use fuser to kill only processes listening on this specific port
            if type -P fuser >/dev/null 2>&1; then
                fuser -k -TERM "$port"/tcp 2>/dev/null || true
                sleep 1
                fuser -k -KILL "$port"/tcp 2>/dev/null || true
            fi
            rm -f /tmp/elbencho*p${port}.log 2>/dev/null || true
        ' bash "$port" 2>/dev/null || true
    return 0
}

# Expand SLURM nodelist to comma-separated list of hostnames
# Usage: nodelist=$(expanded_comma_sep_slurm_nodes)
# Returns: Comma-separated list of expanded node hostnames
# Uses scontrol show hostname to expand compact SLURM nodelist notation
expanded_comma_sep_slurm_nodes() {
    scontrol show hostname "${SLURM_JOB_NODELIST:-}" | paste -sd,
}

# Build the base sbatch command array with common options.
# Usage:
#   declare -a sbatch_cmd
#   build_sbatch_cmd sbatch_cmd
#   "${sbatch_cmd[@]}" --parsable --nodes=N script args...
#
# Parses _SBATCH_OPTIONS_BASE (account, partition, time, excludes/includes,
# --exclusive, --cpus-per-task) and appends the SLURM_EXTRA_ARGS array to
# preserve elements with embedded spaces.
#
# Note on duplicate options: For most Slurm options, the LAST occurrence wins.
# Since SLURM_EXTRA_ARGS comes after base options, it can override settings.
build_sbatch_cmd() {
    local -n _result_array="$1"

    local _sbatch_path
    _sbatch_path=$(type -P sbatch) || {
        echo "Error: sbatch not found in PATH" >&2
        return 1
    }
    _result_array=("$_sbatch_path")

    local -a _opts_array
    read -ra _opts_array <<< "$_SBATCH_OPTIONS_BASE"
    _result_array+=("${_opts_array[@]}")

    if [[ ${#SLURM_EXTRA_ARGS[@]} -gt 0 ]]; then
        _result_array+=("${SLURM_EXTRA_ARGS[@]}")
    fi
}

# Build the base srun command array (same option layering as build_sbatch_cmd).
# Usage:
#   declare -a srun_cmd
#   build_srun_cmd srun_cmd
#   "${srun_cmd[@]}" -N1 -n1 ...
#
# Parses _SRUN_OPTIONS_BASE and appends SLURM_EXTRA_ARGS.
build_srun_cmd() {
    # shellcheck disable=SC2178  # nameref to caller array
    local -n _result_array="$1"

    local _srun_path
    _srun_path=$(type -P srun) || {
        echo "Error: srun not found in PATH" >&2
        return 1
    }
    _result_array=("$_srun_path")

    local -a _opts_array
    read -ra _opts_array <<< "$_SRUN_OPTIONS_BASE"
    _result_array+=("${_opts_array[@]}")

    if [[ ${#SLURM_EXTRA_ARGS[@]} -gt 0 ]]; then
        _result_array+=("${SLURM_EXTRA_ARGS[@]}")
    fi
}

# Build a minimal srun for validate_env.sh scale tests only (check_srun_with).
# Omits --nodelist/--exclude/--exclusive/--cpus-per-task so Slurm can place a
# single task on any eligible node. Still includes SLURM_GPUS_PER_NODE_OPT when
# client_type=gpu: GPU-only partitions often reject jobs with no GPU request.
# Full _SRUN_OPTIONS_BASE is still used by benchmark scripts (mostly sbatch);
# some heterogeneous partitions cannot satisfy -N1 + --exclusive=user + huge
# --nodelist + --cpus-per-task for an interactive step.
# Usage: declare -a srun_cmd; build_srun_validate_scale_cmd srun_cmd
# shellcheck disable=SC2154  # account, partition, run_time, reservation from env.sh
build_srun_validate_scale_cmd() {
    # shellcheck disable=SC2178
    local -n _result_array="$1"

    local _srun_path
    _srun_path=$(type -P srun) || {
        echo "Error: srun not found in PATH" >&2
        return 1
    }
    _result_array=("$_srun_path")
    _result_array+=(-A "${account:?}" -p "${partition:?}" --time "${run_time:?}")
    if [[ -n "${reservation:-}" ]]; then
        _result_array+=(--reservation "${reservation}")
    fi
    # shellcheck disable=SC2154  # SLURM_GPUS_PER_NODE_OPT set in env_base.sh
    if [[ -n "${SLURM_GPUS_PER_NODE_OPT:-}" ]]; then
        _result_array+=("$SLURM_GPUS_PER_NODE_OPT")
    fi
    if [[ ${#SLURM_EXTRA_ARGS[@]} -gt 0 ]]; then
        _result_array+=("${SLURM_EXTRA_ARGS[@]}")
    fi
    return 0
}

# Print warnings about node excludes/includes before a SLURM submission sweep.
# Usage: print_slurm_node_warnings <max_node_count>
print_slurm_node_warnings() {
    local max_nodes="$1"
    if [[ "$SLURM_EXCLUDE_COUNT" -gt 0 ]]; then
        echo "WARNING!! EXCLUDING $SLURM_EXCLUDE_COUNT NODES"
        echo "    Make sure you have at least $max_nodes nodes left!"
        echo
    fi
    if [[ "$SLURM_INCLUDE_COUNT" -gt 0 ]]; then
        echo "NOTE: RESTRICTING TO $SLURM_INCLUDE_COUNT NODES (from $SLURM_NODE_INCLUDES)"
        if [[ "$max_nodes" -gt "$SLURM_INCLUDE_COUNT" ]]; then
            echo "    WARNING: max requested node count ($max_nodes) exceeds nodelist size!"
        fi
        if [[ -n "${ORDER_NODES_ENABLED:-}" ]]; then
            echo "    ORDER_NODES: each job will use the first N nodes from the include list"
        fi
        echo
    fi
    return 0
}

# Submit one sbatch job with dependency chaining, error checking, and log tracking.
#
# Relies on the following caller-scoped variables:
#   sbatch_cmd[@]   - base command array (from build_sbatch_cmd)
#   g_sbatch_opts[@] - per-sweep options (caller may leave empty)
#   sleep_time      - seconds to sleep between dependent jobs
#   JOBID           - updated on success (used for dependency chaining)
#   log_files[@]    - log file paths accumulated across calls
#
# Usage:
#   run_sbatch_job <nodes> <job_name> <output_fmt> <description> \
#       <batch_script> [script_args...]
#
# Returns non-zero on sbatch failure; caller decides how to handle it.
# shellcheck disable=SC2154  # Variables set by caller
run_sbatch_job() {
    local nodes="$1" job_name="$2" output_fmt="$3" description="$4"
    shift 4

    local -a loop_sbatch_opts=()
    if [[ -n "${JOBID:-}" ]]; then
        echo "  (sleeping $sleep_time seconds)"
        sleep "$sleep_time"
        loop_sbatch_opts+=("--dependency=afterany:${JOBID}")
    fi
    loop_sbatch_opts+=(
        "--job-name=${job_name}"
        "--output=${output_fmt}"
        "--error=${output_fmt}"
    )

    # When ORDER_NODES is active with an include list, override --nodelist
    # to exactly the first N nodes (last --nodelist wins in sbatch).
    if [[ -n "${ORDER_NODES_ENABLED:-}" && ${#SLURM_ORDERED_NODES[@]} -gt 0 ]]; then
        local -a first_n_nodes=("${SLURM_ORDERED_NODES[@]:0:$nodes}")
        local ordered_nodelist
        ordered_nodelist=$(IFS=,; echo "${first_n_nodes[*]}")
        loop_sbatch_opts+=("--nodelist=${ordered_nodelist}")
    fi

    JOBID="$( "${sbatch_cmd[@]}" --parsable --nodes="$nodes" \
        "${g_sbatch_opts[@]}" \
        "${loop_sbatch_opts[@]}" \
        "$@" 2>&1 )"
    local rc=$?
    if [[ $rc -ne 0 ]]; then
        echo "$JOBID" >&2
        return $rc
    fi

    local this_log_file="${output_fmt/\%j/$JOBID}"
    log_files+=("$this_log_file")
    echo "$description job ID: $JOBID (logfile: $this_log_file)"
    echo "  ${sbatch_cmd[*]} --parsable --nodes=$nodes ${g_sbatch_opts[*]} ${loop_sbatch_opts[*]} $*"
}

# Query a schedulable per-node CPU count for SLURM_EXCLUSIVE_USER --cpus-per-task.
# Uses the minimum CPUTot across expanded nodes from SLURM_INCLUDES (--nodelist) if set,
# otherwise the minimum CPUTot across nodes in the configured partition. (Using the
# minimum keeps srun/sbatch valid on heterogeneous partitions where a step may land on
# any listed node.)
# Prints the integer CPU count to stdout; returns 1 on failure.
get_slurm_target_node_cpus() {
    local cpu_count=""
    local min_cpu=""
    local hostlist
    local tmpf
    local line
    local batch
    local n
    local node

    if [[ "${SLURM_INCLUDE_COUNT:-0}" -gt 0 ]]; then
        hostlist="${SLURM_INCLUDES#--nodelist=}"
        tmpf=$(mktemp "${TMPDIR:-/tmp}/slurmcpus.XXXXXX") || {
            echo "Error: could not create temp file for CPU query" >&2
            return 1
        }
        local -a _nodes=()
        while IFS= read -r line; do
            [[ -n "$line" ]] && _nodes+=("$line")
        done < <(scontrol show hostname "$hostlist" 2>/dev/null)

        batch=""
        n=0
        for node in "${_nodes[@]}"; do
            if [[ -z "$batch" ]]; then
                batch="$node"
            else
                batch+=",$node"
            fi
            n=$((n + 1))
            if [[ "$n" -ge 100 ]]; then
                sinfo -N -n "$batch" -h -o "%c" 2>/dev/null | tr -d ' \t' >> "$tmpf"
                batch=""
                n=0
            fi
        done
        if [[ -n "$batch" ]]; then
            sinfo -N -n "$batch" -h -o "%c" 2>/dev/null | tr -d ' \t' >> "$tmpf"
        fi
        min_cpu=$(grep -E '^[0-9]+$' "$tmpf" | sort -n | head -1)
        rm -f "$tmpf"

        # Fallback: some Slurm versions reject batched -n; probe each node.
        if [[ -z "$min_cpu" ]] && [[ "${#_nodes[@]}" -gt 0 ]]; then
            min_cpu=""
            for node in "${_nodes[@]}"; do
                line=$(sinfo -n "$node" -h -o "%c" 2>/dev/null | head -1 | tr -d ' \t')
                if [[ "$line" =~ ^[0-9]+$ ]] && [[ -z "$min_cpu" || "$line" -lt "$min_cpu" ]]; then
                    min_cpu="$line"
                fi
            done
        fi
        cpu_count="$min_cpu"
    elif [[ -n "${partition:-}" ]]; then
        min_cpu=$(sinfo -p "${partition}" -N -h -o "%c" 2>/dev/null | tr -d ' \t' | grep -E '^[0-9]+$' | sort -n | head -1)
        cpu_count="$min_cpu"
    fi

    if [[ -z "$cpu_count" || "$cpu_count" -le 0 ]] 2>/dev/null; then
        echo "Error: could not determine CPU count for target nodes" >&2
        return 1
    fi

    echo "$cpu_count"
}

# Generate a consistent Slurm sbatch job name
# Usage: job_name=$(make_sbatch_job_name benchmark [datestamp] [suffix])
# Arguments:
#   benchmark: name of the benchmark (e.g., "elbencho", "warp", "netbench-half")
#   datestamp: optional datestamp to append (e.g., "20251211Z140530")
#   suffix: optional suffix to append (e.g., "8-4" for max_nodes-current_nodes)
# Returns: job name in format "[<prefix>]<benchmark>[-<datestamp>][-<suffix>]"
#   where <prefix> comes from SLURM_JOB_NAME_PREFIX if set and non-empty
#   (include any desired separator in SLURM_JOB_NAME_PREFIX itself, e.g., "myproject-")
make_sbatch_job_name() {
    local benchmark="$1"
    local datestamp="${2:-}"
    local suffix="${3:-}"
    local result=""

    # Prepend prefix if SLURM_JOB_NAME_PREFIX is set and non-empty
    if [[ -n "${SLURM_JOB_NAME_PREFIX:-}" ]]; then
        result="${SLURM_JOB_NAME_PREFIX}"
    fi

    result="${result}${benchmark}"

    if [[ -n "$datestamp" ]]; then
        result="${result}-${datestamp}"
    fi
    if [[ -n "$suffix" ]]; then
        result="${result}-${suffix}"
    fi
    printf '%s' "$result"
    return 0
}
