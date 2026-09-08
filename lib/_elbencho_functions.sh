# shellcheck shell=bash
# shellcheck disable=SC2154  # Variables are set by the calling/sourcing script
#
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
# This library provides elbencho-related functions for benchmarking.
#
# =============================================================================
# Available Functions
# =============================================================================
#
# Service/Setup:
#   start_elbencho_service             - Start elbencho service with watchdog
#   stop_elbencho_service              - Stop elbencho service, watchdog, and clean up logs
#   check_elbencho_status              - Check elbencho HTTP /status (with PID and timeout)
#   kill_elbencho_by_pid               - Kill specific elbencho by PID and clean up logs
#   kill_all_elbencho_processes        - Kill all elbencho processes on host
#   kill_elbencho_watchdog             - Kill watchdog processes (using fuser on log file)
#
# Benchmark Runners (filesystem):
#   run_elbencho_io_sweep_iteration    - Run a single IO benchmark iteration
#   run_elbencho_metadata_benchmark    - Run metadata operations benchmark
#   maybe_restart_elbencho_services_slurm    - Check/restart services (SLURM multi-node)
#
# Execution Reification (per-execution dispatch model):
#   reify_elbencho_execution           - Write one executions/NNNN.sh definition
#   reify_all_elbencho_executions      - Write all NNNN.sh + NNNN.status=PENDING
#   list_elbencho_execution_ids        - Enumerate NNNN ids in dispatch order
#   count_elbencho_remaining_executions - count of non-SUCCESS executions
#   max_nodes_remaining_executions     - max($nodes) over non-SUCCESS executions
#   _elbencho_sweep_running_to_pending - Reset interrupted RUNNING -> PENDING
#   _atomic_write_sentinel             - Atomic mv-into-place sentinel writer
#
# Internal Helpers:
#   run_an_elbencho                    - Execute elbencho with proper host handling
#   _elbencho_resolve_run_context      - node_count, remote_output_dir, hosts_csv, mkdir
#   _elbencho_io_build_common_args     - shared IO sweep elbencho argv preamble (see ELBENCHO_IO_EXTENT_INFERRED)
#   _elbencho_io_append_rotated_hosts_for_read - append rotated --hosts to read args
#   _elbencho_io_set_resfile_csvfile   - IO sweep resfile/csvfile paths
#   _elbencho_io_set_livecsvfile       - IO sweep live CSV path
#   _elbencho_io_set_treefile          - IO sweep treescan path
#   _elbencho_io_cleanup_result_artifacts - remove stale per-execution result files
#   _elbencho_finish_sweep_read_from_only - post-read-only elbencho tail (restart, optional treefile stats, echo)
#   _elbencho_maybe_pause_before_read  - ELBENCHO_READ_AFTER_WRITE_PAUSE sleep
#   _elbencho_emit_write_only_data_dir - print ELBENCHO_WRITE_ONLY_DATA_DIR and path
#
# Utility Functions:
#   elbencho_size_string_to_bytes      - Convert size string (4K, 1M) to bytes
#   bytes_to_elbencho_size_string      - Convert bytes to size string (4K, 1M)
#   elbencho_size_str_to_int           - Convert size strings to integers
#   round_and_divide                   - Rounded integer division
#   compute_target_file_count_per_thread - Compute target file count for IO
#   rotate_csv_list                    - Rotate a comma-separated list by N positions
#
# =============================================================================
#
# It is intended to be sourced (with no side effects) in two environments:
# 1. SLURM
#   - script run by sbatch on one client node
#   - would have access to env.sh, lib/env_functions.sh, etc.
# 2. SSH
#   - scriptlet run by ssh on one client node
#   - does NOT have access to env.sh, lib/env_functions.sh, etc.
#
# Because of the ssh case constraints, this script cannot itself source
# env.sh, lib/env_functions.sh, etc. Instead, the call sites must source
# the appropriate files, set required environment variables, source this
# file, and then call the desired function.
#
# =============================================================================
# run_elbencho_io_sweep_iteration() required environment variables:
# =============================================================================
# FS_MAX_NODE_THROUGHPUT_GBPS  # straight from env.sh
# FS_MAX_NODE_IOPS             # straight from env.sh
# FS_MAX_AGG_THROUGHPUT        # straight from env.sh
# ELBENCHO        # path to elbencho binary (on client nodes)
# io_size         # string per env.sh e.g. "1M" or "4K,1M"
# thread_count    # integer
# output_dir      # full path to output directory (e.g. /path/to/elbencho-20250811Z220605)
# dio_or_bio      # "dio" or "bio"
# use_random      # "1" or "0"
# force_single    # "1" or "0"
# io_depth        # integer
# ELBENCHO_SCALE_READ_WRITE_DURATION # string per env.sh e.g. "5s" or "10m"
# ELBENCHO_FILE_SIZE_MULTIPLIER # integer multiplier for file size (default 1024)
# test_dirs_csv   # comma-separated list of test directories
# ELBENCHO_SWEEP_WRITE_ONLY  # optional "0" or "1" — skip read/delete; print data dir path
# ELBENCHO_SWEEP_WRITE_NO_READ  # optional "0" or "1" — write + per-iteration cleanup; skip read
# ELBENCHO_SWEEP_READ_FROM   # optional non-empty path — read-only from pre-existing tree (skip mkdirs/write/delete)
# ELBENCHO_SINGLE_BIG_FILE   # optional "1" — one shared large file path mode (sequential only)
# ELBENCHO_SINGLE_BIG_FILE_BASENAME  # optional filename under generated dir (default elbencho-bigfile)
# ELBENCHO_SINGLE_BIG_FILE_SIZE      # required when ELBENCHO_SINGLE_BIG_FILE=1 — elbencho --size for the file
# ELBENCHO_ALL_NODES_ACCESS_ALL_DATA  # optional "1" — every node touches full file (elbencho --nosvcshare)
#
# SSH case only env vars:
# SSH_NODELIST    # comma-separated list of client nodes
#
# Slurm case only env vars:
# SLURM_JOB_NODELIST  # compact representation of client nodes
# SLURM_JOB_NUM_NODES # integer number of nodes in the reservation
# nodelist_expanded_comma_separated # comma-separated list (expanded from SLURM_JOB_NODELIST)
#
# =============================================================================
# run_elbencho_metadata_benchmark() required environment variables:
# =============================================================================
# ELBENCHO             # path to elbencho binary (on client nodes)
# MDTEST_BRANCH_FACTOR # subdirs per TEST_DIR, also used for dirs per thread
# MDTEST_ITEMS_PER_DIR # files per dir per thread (-N flag)
# MDTEST_ITERATIONS    # number of iterations (--iterations flag)
# output_dir           # full path to output directory
# test_dirs_csv        # comma-separated list of base test directories
# tasks_per_node       # threads per host (-t flag)
#
# Dense (single flat directory) layout, set by _mdtest_export_layout_env:
# MDTEST_LAYOUT                       # "standard" (default) or "single-dir"
# MDTEST_SINGLE_DIR_TARGET_FILES      # requested approximate total file count
# MDTEST_SINGLE_DIR_FILES_PER_WORKER  # derived uniform per-worker count (-N)
#
# SSH case only env vars:
# SSH_NODELIST         # comma-separated list of client nodes
#
# Slurm case only env vars:
# SLURM_JOB_NODELIST   # compact representation of client nodes
# SLURM_JOB_NUM_NODES  # integer number of nodes
# nodelist_expanded_comma_separated # comma-separated list (expanded)


# Check if elbencho service is healthy via HTTP /status endpoint
# Usage: check_elbencho_status [port] [pid]
#   port - Port to check (default: 1611)
#   pid  - Optional PID to verify is still alive before HTTP check
# Returns: 0 if HTTP 200 received within timeout, 1 otherwise
# Note: Uses pure bash /dev/tcp - no curl/wget/nc dependency
# Note: Checks for "200" in response since cat exit code is unreliable
#       (server may send RST on close causing cat to exit non-zero)
# Note: HTTP check runs with 5-second timeout to handle tarpit scenarios
check_elbencho_status() {
    local port="${1:-1611}"
    local pid="${2:-}"
    local timeout_secs=5

    # If PID provided, check it's still alive first (fail fast)
    if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
        return 1  # Process is dead, no point checking HTTP
    fi

    # Use temp file for response (backgrounded process can't return data directly)
    local response_file
    response_file=$(mktemp) || return 1

    # Run HTTP check in background subshell
    (
        exec 3<>/dev/tcp/127.0.0.1/"$port" 2>/dev/null && \
        echo -e "GET /status HTTP/1.0\r\nHost: 127.0.0.1:$port\r\n\r\n" >&3 && \
        cat <&3 2>/dev/null
        exec 3>&- 2>/dev/null
    ) > "$response_file" 2>/dev/null &
    local http_pid=$!

    # Wait up to timeout_secs for the check to complete
    local i
    for ((i = 0; i < timeout_secs; i++)); do
        if ! kill -0 "$http_pid" 2>/dev/null; then
            break  # Process finished
        fi
        sleep 1
    done

    # Kill if still running (tarpit case: accepts but never responds)
    if kill -0 "$http_pid" 2>/dev/null; then
        kill "$http_pid" 2>/dev/null || true
        kill -9 "$http_pid" 2>/dev/null || true  # Force kill
        wait "$http_pid" 2>/dev/null || true     # Reap zombie
        rm -f "$response_file"
        return 1  # Timeout
    fi

    # Reap the finished process
    wait "$http_pid" 2>/dev/null || true

    # Check response content
    local response
    response=$(<"$response_file")
    rm -f "$response_file"

    # Check for "200" in response (reliable even if cat exited non-zero)
    [[ "$response" == *"200"* ]]
}

# Kill a specific elbencho process by PID
# Usage: kill_elbencho_by_pid pid port
#   pid  - Process ID to kill
#   port - Port number for log file cleanup
kill_elbencho_by_pid() {
    local pid="$1"
    local port="${2:-1611}"

    # Check if process exists
    if ! kill -0 "$pid" 2>/dev/null; then
        # Process already dead, but still clean up log files
        rm -rf "/tmp/elbencho*p${port}.log" 2>/dev/null || true
        return 0
    fi

    # SIGTERM first
    kill "$pid" 2>/dev/null || true
    sleep 2

    # SIGKILL if still alive
    if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
        sleep 1
    fi

    # Clean up log files
    rm -rf "/tmp/elbencho*p${port}.log" 2>/dev/null || true
}

# Kill all elbencho processes on this host
# Usage: kill_all_elbencho_processes [port]
kill_all_elbencho_processes() {
    local port="${1:-1611}"

    # Determine kill tool (prefer pkill over killall)
    local kill_tool=""
    if type -P pkill >/dev/null 2>&1; then
        kill_tool="pkill"
    elif type -P killall >/dev/null 2>&1; then
        kill_tool="killall"
    else
        echo "Error: Neither pkill nor killall is available" >&2
        return 1
    fi

    # SIGTERM
    if [[ "$kill_tool" == "pkill" ]]; then
        pkill elbencho >/dev/null 2>&1 || true
    else
        killall elbencho >/dev/null 2>&1 || true
    fi
    sleep 2

    # SIGKILL
    if [[ "$kill_tool" == "pkill" ]]; then
        pkill -9 elbencho >/dev/null 2>&1 || true
    else
        killall -9 elbencho >/dev/null 2>&1 || true
    fi
    sleep 2

    # Clean up log files
    rm -rf "/tmp/elbencho*p${port}.log" 2>/dev/null || true
}

# Kill elbencho watchdog processes
# The watchdog runs as a bash process with "elbencho_watchdog" in its command line
# Note: This function is safe to use pkill -f because it runs in a context where the
# calling shell's command line does NOT contain "elbencho_watchdog". If you need to
# kill the watchdog from an inline SSH command, use fuser instead (see scp_remote_file_N_ssh).
kill_elbencho_watchdog() {
    local watchdog_log="/tmp/elbencho_service_watchdog.log"

    # Kill watchdog by matching command line (pkill -f matches full cmdline)
    if type -P pkill >/dev/null 2>&1; then
        pkill -9 -f elbencho_watchdog 2>/dev/null || true
    fi

    # Fallback: kill processes that have the watchdog log file open
    if [[ -f "$watchdog_log" ]]; then
        if type -P fuser >/dev/null 2>&1; then
            fuser -k "$watchdog_log" 2>/dev/null || true
        fi
    fi

    sleep 1

    # Note: We intentionally do NOT delete the watchdog log file.
    # It lives in /tmp and provides debugging info across runs.
}

# Stop elbencho service and watchdog on this host, clean up logs
# Usage: stop_elbencho_service [port]
#   port - Port for log cleanup (default: 1611)
# This is the counterpart to start_elbencho_service for proper cleanup
stop_elbencho_service() {
    local port="${1:-1611}"

    # Kill watchdog FIRST to prevent it from restarting elbencho
    kill_elbencho_watchdog

    # Kill all elbencho processes (this includes the service)
    kill_all_elbencho_processes "$port"

    # Kill watchdog again in case it was in the middle of restarting elbencho
    kill_elbencho_watchdog
}

# Kill existing elbencho processes and start a fresh elbencho service with watchdog
# Usage: start_elbencho_service [port] [skip_kill] [existing_pid]
#   port         - Optional port number (default: elbencho default 1611)
#   skip_kill    - If "true", skip killing all processes
#   existing_pid - If provided, kill this specific PID (used by watchdog restart)
#
# After starting elbencho, spawns a background watchdog that monitors the service
# every 5 seconds. If the service becomes unresponsive, the watchdog restarts it
# by recursively calling this function with the dead PID.
start_elbencho_service() {
    local port="${1:-}"
    local skip_kill="${2:-false}"
    local existing_pid="${3:-}"
    local check_port="${port:-1611}"

    # Handle process cleanup
    if [[ -n "$existing_pid" ]]; then
        # Watchdog calling back - kill specific process
        echo "$(date -Iseconds) Watchdog: killing elbencho PID $existing_pid"
        kill_elbencho_by_pid "$existing_pid" "$check_port"
    elif [[ "$skip_kill" != "true" ]]; then
        # Initial startup - kill all elbencho processes
        kill_all_elbencho_processes "$check_port"
    fi

    # Build and start elbencho service
    # Use --foreground to prevent elbencho from daemonizing itself
    # We background it ourselves to get the PID
    # Note: $ELBENCHO must be set by the caller (env.sh sets it for SLURM,
    #       SSH scriptlets set it to ./elbencho after copying the binary)
    if [[ -z "${ELBENCHO:-}" ]]; then
        echo "$(date -Iseconds) Error: ELBENCHO is not set" >&2
        return 1
    fi
    local service_cmd=("$ELBENCHO" --service --foreground)
    if [[ -n "$port" ]]; then
        service_cmd+=(--port "$port")
    fi

    "${service_cmd[@]}" >/dev/null 2>&1 &
    local elbencho_pid=$!
    echo "$(date -Iseconds) Started elbencho service (PID $elbencho_pid) on port $check_port"

    # Wait for service to be ready (up to 12 seconds, check after each sleep)
    local max_wait=12
    local waited=0
    while [[ $waited -lt $max_wait ]]; do
        sleep 1
        ((waited++))
        if check_elbencho_status "$check_port" "$elbencho_pid"; then
            echo "$(date -Iseconds) elbencho service ready on port $check_port after ${waited}s"
            break
        fi
    done

    if [[ $waited -ge $max_wait ]]; then
        echo "$(date -Iseconds) Error: elbencho service not responding on port $check_port after ${max_wait}s" >&2
        return 1
    fi

    # Spawn background watchdog as a named process (so pkill can find it)
    # The watchdog monitors service health and restarts if needed
    # Redirect stdin to /dev/null and stdout/stderr to log file so SSH sessions can close
    # (SSH waits for all inherited file descriptors to close)
    local watchdog_log="/tmp/elbencho_service_watchdog.log"

    # Find where this script is located so watchdog can re-source it
    local funcs_path="${BASH_SOURCE[0]}"

    # Launch watchdog as a named bash process
    # Arguments: $1=funcs_path $2=ELBENCHO $3=check_port $4=elbencho_pid $5=port_arg
    # shellcheck disable=SC2016  # Single quotes intentional - variables expand inside bash -c
    "$BASH" -c '
        funcs_path="$1"
        export ELBENCHO="$2"
        watchdog_port="$3"
        watchdog_pid="$4"
        watchdog_port_arg="$5"

        # Source the functions library
        # shellcheck disable=SC1090
        source "$funcs_path" || exit 1

        while true; do
            sleep 5
            if ! check_elbencho_status "$watchdog_port" "$watchdog_pid"; then
                echo "$(date -Iseconds) Watchdog: elbencho not responding on port $watchdog_port, restarting..."
                # Recursive call with existing PID - this spawns a new watchdog
                start_elbencho_service "$watchdog_port_arg" "true" "$watchdog_pid"
                # Exit this watchdog (new one spawned by recursive call)
                exit 0
            fi
        done
    ' elbencho_watchdog "$funcs_path" "$ELBENCHO" "$check_port" "$elbencho_pid" "$port" \
        </dev/null >>"$watchdog_log" 2>&1 &

    # Disown the watchdog so it doesn't become a zombie
    disown $! 2>/dev/null || true
}

# Convert a string with a size suffix (K, M, G) to bytes
# Usage: size_to_bytes "4K" -> 4096
function elbencho_size_string_to_bytes() {
    local size="$1"
    local num_pattern="^([0-9]+)([kKmMgG])$"

    if [[ "$size" =~ $num_pattern ]]; then
        local num="${BASH_REMATCH[1]}"
        local suffix="${BASH_REMATCH[2]}"

        # Convert suffix to uppercase for consistency
        suffix="${suffix^^}"

        case "$suffix" in
            K)
                echo "$((num * 1024))"
                ;;
            M)
                echo "$((num * 1024 * 1024))"
                ;;
            G)
                echo "$((num * 1024 * 1024 * 1024))"
                ;;
            *)
                echo "Error: Unknown suffix $suffix" >&2
                return 1
                ;;
        esac
    else
        echo "Error: Invalid format. Expected <integer><K|M|G>" >&2
        return 1
    fi
}

# Utility function: rounds and divides, requires bc/python3/perl
# If none are available, falls back to Bash: does ceiling division instead of round.
# Usage: round_and_divide value divisor
# Returns: integer result (rounded quotient)
# Example: round_and_divide 5555 1024 => 5
round_and_divide() {
    local value="$1"
    local divisor="$2"
    local result
    if type -P bc >/dev/null 2>&1; then
        result="$(echo "scale=0; ($value + $((divisor / 2))) / $divisor" | bc)"
    elif type -P python3 >/dev/null 2>&1; then
        result="$(python3 -c "print(round(($value + $divisor // 2) / $divisor))")"
    elif type -P perl >/dev/null 2>&1; then
        result="$(perl -e "print int((($value + $divisor/2)/$divisor) + 0.5)")"
    else
        # Bash fallback: Use ceiling division, which is "best effort" here
        # Bash integer arithmetic truncates toward zero, so we add (divisor - 1)
        result=$(( (value + divisor - 1) / divisor ))
    fi
    echo "$result"
}

# Convert a byte count to an Elbencho size string with appropriate suffix
# Usage: bytes_to_elbencho_size_string 4096 -> "4K"
function bytes_to_elbencho_size_string() {
    local file_size_bytes="$1"

    # Define the size units
    local K=$((1024))
    local M=$((1024 * K))
    local G=$((1024 * M))

    # Find the appropriate unit
    if [[ $file_size_bytes -ge $G && $((file_size_bytes % G)) -eq 0 ]]; then
        echo "$((file_size_bytes / G))G"
    elif [[ $file_size_bytes -ge $M && $((file_size_bytes % M)) -eq 0 ]]; then
        echo "$((file_size_bytes / M))M"
    elif [[ $file_size_bytes -ge $K && $((file_size_bytes % K)) -eq 0 ]]; then
        echo "$((file_size_bytes / K))K"
    # If not an exact multiple, find the closest representation
    elif [[ $file_size_bytes -ge $G ]]; then
        local rounded
        rounded="$(round_and_divide "$file_size_bytes" "$G")" || return 1
        echo "${rounded}G"
    elif [[ $file_size_bytes -ge $M ]]; then
        local rounded
        rounded="$(round_and_divide "$file_size_bytes" "$M")" || return 1
        echo "${rounded}M"
    else
        # Ensure we return at least 1K
        local k_value
        k_value="$(round_and_divide "$file_size_bytes" "$K")" || return 1
        if [[ $k_value -lt 1 ]]; then
            echo "1K"
        else
            echo "${k_value}K"
        fi
    fi
}

# Convert size strings like "4K", "1M", "10M", "5G" to integers
# Supports K (1024), M (1024*1024) and G (1024*1024*1024) suffixes
elbencho_size_str_to_int() {
    local size_str="$1"
    local num="${size_str%[KMG]}"
    local suffix="${size_str: -1}"
    local multiplier=1

    case "$suffix" in
        K) multiplier=1024 ;;
        M) multiplier=$((1024 * 1024)) ;;
        G) multiplier=$((1024 * 1024 * 1024)) ;;
        *) echo "Error: Unsupported suffix '$suffix'. Only K, M, G are supported" >&2; return 1 ;;
    esac

    echo $((num * multiplier))
}

# Canonical exact-decimal helpers. These intentionally avoid Bash and awk
# numeric coercion for values that can exceed their integer/float ranges.
_elbencho_decimal_canonicalize() {
    local value="$1"
    if [[ ! "$value" =~ ^[0-9]+$ ]]; then
        return 1
    fi
    value="${value#"${value%%[!0]*}"}"
    printf '%s\n' "${value:-0}"
    return 0
}

_elbencho_decimal_compare() {
    local left
    local right
    left=$(_elbencho_decimal_canonicalize "$1") || return 1
    right=$(_elbencho_decimal_canonicalize "$2") || return 1
    if [[ ${#left} -lt ${#right} ]]; then
        printf '%s\n' -1
    elif [[ ${#left} -gt ${#right} ]]; then
        printf '%s\n' 1
    elif [[ "$left" == "$right" ]]; then
        printf '%s\n' 0
    elif [[ "$left" < "$right" ]]; then
        printf '%s\n' -1
    else
        printf '%s\n' 1
    fi
    return 0
}

_elbencho_decimal_multiply() {
    local left
    local right
    left=$(_elbencho_decimal_canonicalize "$1") || return 1
    right=$(_elbencho_decimal_canonicalize "$2") || return 1
    awk -v a="$left" -v b="$right" '
    BEGIN {
        if (a == "0" || b == "0") { print "0"; exit }
        for (i = length(a); i >= 1; i--)
            for (j = length(b); j >= 1; j--)
                out[(length(a) - i) + (length(b) - j)] += \
                    substr(a, i, 1) * substr(b, j, 1)
        max = length(a) + length(b)
        for (i = 0; i <= max; i++) {
            out[i + 1] += int(out[i] / 10)
            out[i] %= 10
        }
        while (max > 0 && out[max] == 0) max--
        result = ""
        for (i = max; i >= 0; i--) result = result out[i]
        print result
    }'
}

_elbencho_decimal_add() {
    local left
    local right
    left=$(_elbencho_decimal_canonicalize "$1") || return 1
    right=$(_elbencho_decimal_canonicalize "$2") || return 1
    awk -v a="$left" -v b="$right" '
    BEGIN {
        i = length(a); j = length(b); carry = 0; out = ""
        while (i > 0 || j > 0 || carry > 0) {
            digit = carry
            if (i > 0) digit += substr(a, i--, 1)
            if (j > 0) digit += substr(b, j--, 1)
            out = (digit % 10) out
            carry = int(digit / 10)
        }
        print out
    }'
}

_elbencho_decimal_subtract() {
    local left
    local right
    left=$(_elbencho_decimal_canonicalize "$1") || return 1
    right=$(_elbencho_decimal_canonicalize "$2") || return 1
    [[ $(_elbencho_decimal_compare "$left" "$right") -ge 0 ]] || return 1
    awk -v a="$left" -v b="$right" '
    function canon(v) { sub(/^0+/, "", v); return v == "" ? "0" : v }
    BEGIN {
        out = ""; borrow = 0; j = length(b)
        for (i = length(a); i >= 1; i--) {
            digit = substr(a, i, 1) - borrow
            if (j > 0) digit -= substr(b, j--, 1)
            if (digit < 0) { digit += 10; borrow = 1 } else borrow = 0
            out = digit out
        }
        print canon(out)
    }'
}

_elbencho_monotonic_milliseconds() {
    if [[ -r /proc/uptime ]]; then
        local uptime_millis
        uptime_millis=$(awk '{
            split($1, parts, ".")
            fraction = substr(parts[2] "000", 1, 3)
            print parts[1] fraction
        }' /proc/uptime) || return 1
        _elbencho_decimal_canonicalize "$uptime_millis"
        return $?
    fi
    local epoch_millis
    epoch_millis=$(date +%s%3N 2>/dev/null) || epoch_millis=
    if [[ "$epoch_millis" =~ ^[0-9]+$ ]]; then
        _elbencho_decimal_canonicalize "$epoch_millis"
        return $?
    fi
    local epoch_seconds
    epoch_seconds=$(date +%s) || return 1
    _elbencho_decimal_canonicalize "${epoch_seconds}000"
}

_elbencho_shell_signed_max() {
    local current=1
    local next
    while true; do
        next=$((current * 2 + 1))
        if [[ "$next" -le "$current" ]]; then
            printf '%s\n' "$current"
            return 0
        fi
        current="$next"
    done
}

_elbencho_decimal_divide_small() {
    local value
    local divisor="$2"
    value=$(_elbencho_decimal_canonicalize "$1") || return 1
    [[ "$divisor" =~ ^[1-9][0-9]*$ ]] || return 1
    awk -v value="$value" -v divisor="$divisor" '
    BEGIN {
        carry = 0; result = ""
        for (i = 1; i <= length(value); i++) {
            carry = carry * 10 + substr(value, i, 1)
            digit = int(carry / divisor)
            carry %= divisor
            if (result != "" || digit != 0) result = result digit
        }
        if (result == "") result = "0"
        print result "\t" carry
    }'
}

_elbencho_decimal_mod() {
    local dividend
    local divisor
    dividend=$(_elbencho_decimal_canonicalize "$1") || return 1
    divisor=$(_elbencho_decimal_canonicalize "$2") || return 1
    [[ "$divisor" != 0 ]] || return 1
    awk -v dividend="$dividend" -v divisor="$divisor" '
    function canon(v) { sub(/^0+/, "", v); return v == "" ? "0" : v }
    function cmp(a, b) {
        a = canon(a); b = canon(b)
        if (length(a) != length(b)) return length(a) < length(b) ? -1 : 1
        return a == b ? 0 : (a < b ? -1 : 1)
    }
    function subdec(a, b,    i,j,borrow,d,out) {
        out = ""; borrow = 0; j = length(b)
        for (i = length(a); i >= 1; i--) {
            d = substr(a, i, 1) - borrow
            if (j > 0) d -= substr(b, j--, 1)
            if (d < 0) { d += 10; borrow = 1 } else borrow = 0
            out = d out
        }
        return canon(out)
    }
    BEGIN {
        rem = "0"
        for (i = 1; i <= length(dividend); i++) {
            rem = canon(rem substr(dividend, i, 1))
            while (cmp(rem, divisor) >= 0) rem = subdec(rem, divisor)
        }
        print rem
    }'
}

_elbencho_size_to_exact_bytes() {
    local size="$1"
    [[ "$size" =~ ^([1-9][0-9]*)([KMG])$ ]] || return 1
    local factor
    case "${BASH_REMATCH[2]}" in
        K) factor=1024 ;;
        M) factor=1048576 ;;
        G) factor=1073741824 ;;
    esac
    _elbencho_decimal_multiply "${BASH_REMATCH[1]}" "$factor"
}

_elbencho_duration_to_seconds_exact() {
    local duration="$1"
    [[ "$duration" =~ ^([0-9]+)([smh]?)$ ]] || return 1
    local factor=1
    [[ "${BASH_REMATCH[2]}" == m ]] && factor=60
    [[ "${BASH_REMATCH[2]}" == h ]] && factor=3600
    _elbencho_decimal_multiply "${BASH_REMATCH[1]}" "$factor"
}

# Resolve generated many-file size without rounding. The multiplier-derived
# representation is promoted through K/M/G only on exact 1024 boundaries.
_elbencho_resolve_generated_file_size() {
    local write_size="$1"
    if [[ -n "${ELBENCHO_FILE_SIZE:-}" ]]; then
        printf '%s\n' "$ELBENCHO_FILE_SIZE"
        return 0
    fi
    [[ "$write_size" =~ ^([1-9][0-9]*)([KMG])$ ]] || return 1
    local coefficient
    coefficient=$(_elbencho_decimal_multiply \
        "${BASH_REMATCH[1]}" "${ELBENCHO_FILE_SIZE_MULTIPLIER:-1024}") || return 1
    local suffix="${BASH_REMATCH[2]}"
    local divided remainder original
    while [[ "$suffix" != G ]]; do
        original="$coefficient"
        divided=$(_elbencho_decimal_divide_small "$coefficient" 1024) || return 1
        IFS=$'\t' read -r coefficient remainder <<<"$divided"
        if [[ "$remainder" != 0 ]]; then
            coefficient="$original"
            break
        fi
        [[ "$suffix" == K ]] && suffix=M || suffix=G
    done
    printf '%s%s\n' "$coefficient" "$suffix"
    return 0
}

_elbencho_shared_files_per_worker() {
    # Generated directory mode assigns complete files to workers. It cannot
    # assign multiple workers to one file.
    local files_per_node
    local thread_count_value
    files_per_node=$(_elbencho_decimal_canonicalize "$1") || return 1
    thread_count_value=$(_elbencho_decimal_canonicalize "$2") || return 1
    local shell_max
    shell_max=$(_elbencho_shell_signed_max) || return 1
    [[ "$files_per_node" != 0 && "$thread_count_value" != 0 ]] || return 1
    [[ $(_elbencho_decimal_compare "$files_per_node" "$shell_max") -le 0 ]] \
        || return 1
    [[ $(_elbencho_decimal_compare "$thread_count_value" "$shell_max") -le 0 ]] \
        || return 1
    (( files_per_node >= thread_count_value \
        && files_per_node % thread_count_value == 0 )) || return 1
    printf '%s\n' "$((files_per_node / thread_count_value))"
    return 0
}

_elbencho_validate_workload_pairing() {
    local layout="$1"
    local files_per_node="$2"
    if [[ -n "$files_per_node" && "$layout" != shared-directory ]]; then
        echo "Error: ELBENCHO_FILES_PER_NODE requires ELBENCHO_FILE_LAYOUT=shared-directory" >&2
        return 1
    fi
    if [[ "${ELBENCHO_SINGLE_BIG_FILE:-0}" != 1 \
            && "${ELBENCHO_ALL_NODES_ACCESS_ALL_DATA:-0}" != 0 ]]; then
        echo "Error: ELBENCHO_ALL_NODES_ACCESS_ALL_DATA is valid only with ELBENCHO_SINGLE_BIG_FILE=1" >&2
        return 1
    fi
    return 0
}

_elbencho_validate_shared_configuration() {
    local read_from="$1"
    local files_per_node="$2"
    if [[ "${ELBENCHO_SINGLE_BIG_FILE:-0}" == 1 ]]; then
        echo "Error: shared-directory is incompatible with ELBENCHO_SINGLE_BIG_FILE=1" >&2
        return 1
    fi
    # shellcheck disable=SC2153  # TEST_DIRS is supplied by env.sh/env_used.sh.
    if [[ ${#TEST_DIRS[@]} -ne 1 ]]; then
        echo "Error: shared-directory requires exactly one TEST_DIRS root; found ${#TEST_DIRS[@]}" >&2
        return 1
    fi
    local root
    for root in "${!TEST_DIRS[@]}"; do
        if [[ "${TEST_DIRS[$root]}" != 1 ]]; then
            echo "Error: shared-directory requires the sole TEST_DIRS weight to be 1 (got ${TEST_DIRS[$root]})" >&2
            return 1
        fi
    done
    if [[ -z "$read_from" && -z "$files_per_node" ]]; then
        echo "Error: generated shared-directory requires ELBENCHO_FILES_PER_NODE" >&2
        return 1
    fi
    return 0
}

_elbencho_nearby_valid_file_counts() {
    local files_per_node="$1"
    local thread_count_value="$2"
    local shell_max
    shell_max=$(_elbencho_shell_signed_max) || return 1
    [[ $(_elbencho_decimal_compare "$files_per_node" "$shell_max") -le 0 ]] \
        || return 1
    [[ $(_elbencho_decimal_compare "$thread_count_value" "$shell_max") -le 0 ]] \
        || return 1
    local remainder=$((files_per_node % thread_count_value))
    local lower=$((files_per_node - remainder))
    local upper
    upper=$(_elbencho_decimal_add "$lower" "$thread_count_value") || return 1
    if [[ "$lower" -ge "$thread_count_value" ]]; then
        printf '%s or %s\n' "$lower" "$upper"
    else
        printf '%s\n' "$upper"
    fi
    return 0
}

_elbencho_validate_one_shared_thread() {
    local files_per_node="$1"
    local thread_count_value="$2"
    _elbencho_shared_files_per_worker \
        "$files_per_node" "$thread_count_value" >/dev/null && return 0
    local correction
    correction=$(_elbencho_nearby_valid_file_counts \
        "$files_per_node" "$thread_count_value" 2>/dev/null) \
        || correction=unavailable
    echo "Error: ELBENCHO_FILES_PER_NODE=${files_per_node} must be greater than or equal to ELBENCHO_SCALE_THREAD_LIST value ${thread_count_value} and evenly divisible by it; nearby valid ELBENCHO_FILES_PER_NODE value(s): ${correction}" >&2
    return 1
}

_elbencho_validate_shared_threads() {
    local files_per_node
    files_per_node=$(_elbencho_decimal_canonicalize "$1") || {
        echo "Error: ELBENCHO_FILES_PER_NODE='$1' is not a positive decimal integer" >&2
        return 1
    }
    local thread_count_value
    for thread_count_value in "${ELBENCHO_SCALE_THREAD_LIST[@]}"; do
        _elbencho_validate_one_shared_thread \
            "$files_per_node" "$thread_count_value" || return 1
    done
    return 0
}

_elbencho_validate_exact_phase() {
    local phase_name="$1"
    local phase_component="$2"
    local io_spec="$3"
    local file_size="$4"
    local size_bytes="$5"
    local access_mode="$6"
    local global_random="$7"
    local phase_random="$global_random"
    [[ "$phase_component" == r* ]] && phase_random=1
    local phase_size="${phase_component#r}"
    [[ "$access_mode" == dio || "$phase_random" == 1 ]] || return 0
    local block_bytes effective cmp remainder
    block_bytes=$(_elbencho_size_to_exact_bytes "$phase_size") || return 1
    cmp=$(_elbencho_decimal_compare "$block_bytes" "$size_bytes") || return 1
    effective="$block_bytes"
    [[ "$cmp" -gt 0 ]] && effective="$size_bytes"
    remainder=$(_elbencho_decimal_mod "$size_bytes" "$effective") || return 1
    [[ "$remainder" == 0 ]] && return 0
    echo "Error: exact generated file size ${file_size} is not divisible by effective ${phase_name} block size ${phase_size} for io_size '${io_spec}'" >&2
    return 1
}

_elbencho_validate_exact_io_spec() {
    local io_spec="$1"
    local access_mode="$2"
    local global_random="$3"
    local write_component="${io_spec%%,*}"
    local read_component="$io_spec"
    [[ "$io_spec" == *,* ]] && read_component="${io_spec##*,}"
    local file_size size_bytes
    file_size=$(_elbencho_resolve_generated_file_size \
        "${write_component#r}") || return 1
    size_bytes=$(_elbencho_size_to_exact_bytes "$file_size") || return 1
    _elbencho_validate_exact_phase write "$write_component" "$io_spec" \
        "$file_size" "$size_bytes" "$access_mode" "$global_random" || return 1
    local write_only="${sweep_write_only:-${ELBENCHO_SWEEP_WRITE_ONLY:-0}}"
    local no_read="${sweep_write_no_read:-${ELBENCHO_SWEEP_WRITE_NO_READ:-0}}"
    [[ "$write_only" == 1 || "$no_read" == 1 ]] && return 0
    _elbencho_validate_exact_phase read "$read_component" "$io_spec" \
        "$file_size" "$size_bytes" "$access_mode" "$global_random"
}

_elbencho_validate_exact_io_sizes() {
    local access_mode="$1"
    local global_random="$2"
    local io_spec
    for io_spec in "${ELBENCHO_SCALE_IO_SIZES[@]}"; do
        _elbencho_validate_exact_io_spec "$io_spec" "$access_mode" \
            "$global_random" || return 1
    done
    return 0
}

# Validate the complete generated/staged workload mode after CLI parsing.
# Usage: validate_elbencho_sweep_workload_mode <dio_or_bio> <global_random> <read_from>
validate_elbencho_sweep_workload_mode() {
    local access_mode="$1"
    local global_random="$2"
    local read_from="$3"
    local layout="${ELBENCHO_FILE_LAYOUT:-worker-directories}"
    local files_per_node="${ELBENCHO_FILES_PER_NODE:-}"
    _elbencho_validate_workload_pairing "$layout" "$files_per_node" || return 1
    if [[ "$layout" == shared-directory ]]; then
        _elbencho_validate_shared_configuration "$read_from" \
            "$files_per_node" || return 1
    fi
    [[ -n "$read_from" || "${ELBENCHO_SINGLE_BIG_FILE:-0}" == 1 ]] && return 0
    if [[ "$layout" == shared-directory ]]; then
        _elbencho_validate_shared_threads "$files_per_node" || return 1
    fi
    [[ "$layout" == shared-directory || -n "${ELBENCHO_FILE_SIZE:-}" ]] \
        || return 0
    _elbencho_validate_exact_io_sizes "$access_mode" "$global_random"
}

# Helper function to compute target file count for a given IO size
compute_target_file_count_per_thread() {
    local io_size="$1"
    local file_size="$2"
    local node_count="$3"
    local thread_count="$4"
    local duration="$5"
    local max_node_throughput="${FS_MAX_NODE_THROUGHPUT_GBPS}"
    local max_node_iops="${FS_MAX_NODE_IOPS}"
    local max_agg_throughput="${FS_MAX_AGG_THROUGHPUT}"

    # Convert throughput limits to bytes per second
    local max_node_bytes_per_sec
    max_node_bytes_per_sec=$((max_node_throughput * 1024 * 1024 * 1024 / 8))
    local max_agg_bytes_per_sec
    max_agg_bytes_per_sec=$((max_agg_throughput * 1024 * 1024 * 1024))

    # Convert IO size to bytes using existing helper function
    local io_bytes
    io_bytes=$(elbencho_size_str_to_int "$io_size") || return 1

    # Convert file size to bytes using existing helper function
    local file_bytes
    file_bytes=$(elbencho_size_str_to_int "$file_size") || return 1

    # Compute total bytes possible in the duration for both throughput limits
    local node_total_bytes
    node_total_bytes=$((max_node_bytes_per_sec * node_count * duration))
    echo -e "node_total_bytes = max_node_bytes_per_sec * nodes * duration\n  $node_total_bytes = $max_node_bytes_per_sec * $node_count nodes * ${duration}s" >&2
    local agg_total_bytes
    agg_total_bytes=$((max_agg_bytes_per_sec * duration))
    echo -e "agg_total_bytes = max_agg_bytes_per_sec * duration\n  $agg_total_bytes = $max_agg_bytes_per_sec * ${duration}s" >&2

    # Use the smaller of the two throughput limits
    local total_bytes
    total_bytes=$((node_total_bytes < agg_total_bytes ? node_total_bytes : agg_total_bytes))

    # Calculate IOs per file (file_size / io_size)
    local ios_per_file
    ios_per_file=$((file_bytes / io_bytes))

    # Calculate maximum IOs possible in the duration based on a per-node IOPS limit
    local max_ios_by_iops
    max_ios_by_iops=$((max_node_iops * node_count * duration))
    echo -e "max_ios_by_iops = max_node_iops * nodes * duration\n  $max_ios_by_iops = $max_node_iops * $node_count nodes * ${duration}s" >&2
    # Calculate maximum IOs possible in the duration based on a whole-filesystem throughput limit
    local max_ios_by_throughput
    max_ios_by_throughput=$((total_bytes / io_bytes))
    echo -e "max_ios_by_throughput = total_bytes / io_bytes\n  $max_ios_by_throughput = $total_bytes / $io_bytes" >&2
    # Use the smaller of the two IO limits
    local max_ios
    max_ios=$((max_ios_by_iops < max_ios_by_throughput ? max_ios_by_iops : max_ios_by_throughput))

    # Calculate target file count by dividing max IOs by IOs per file; also
    # divide by node count and thread count since files are per host and thread
    local divisor
    divisor=$(( ios_per_file * node_count * thread_count ))
    local target_files
    target_files=$(( (max_ios + divisor - 1) / divisor ))
    echo -e "target_files = ceiling(max_ios / ios_per_file / nodes / threads)\n  $target_files = ceiling($max_ios / $ios_per_file / $node_count / $thread_count)" >&2

    # Ensure we have at least one file
    if [ "$target_files" -lt 1 ]; then
        target_files=1
    fi

    echo "$target_files"
}

# Check and restart elbencho services if unhealthy (SLURM multi-node only)
# Usage: maybe_restart_elbencho_services_slurm phase_name
#   phase_name - Name of the phase for logging (e.g., "write", "read")
# This function is a no-op for SSH mode or single-node SLURM jobs.
# Modifies global SRUN_ELBENCHO_PID if services are restarted.
maybe_restart_elbencho_services_slurm() {
    local phase_name="${1:-}"
    local restarted_pid

    # Only applies to SLURM multi-node jobs
    if [[ -z "${SLURM_JOB_ID:-}" ]] || [[ "${SLURM_JOB_NUM_NODES:-1}" -le 1 ]]; then
        return 0
    fi
    # Caller (e.g. the coordinator under the per-execution dispatch model)
    # owns the srun-services lifecycle via SRUN_ELBENCHO_PID. If it's not in
    # scope, we cannot perform a real restart: stop_elbencho_services_srun ""
    # would fall through to a port-fuser cleanup that breaks live services.
    # Treat as "leave services alone" -- the lifecycle owner handles checks.
    if [[ -z "${SRUN_ELBENCHO_PID:-}" ]]; then
        return 0
    fi

    if ! check_elbencho_services_srun; then
        echo "Elbencho services unhealthy after ${phase_name} phase, restarting..."
        stop_elbencho_services_srun "$SRUN_ELBENCHO_PID" || return 1
        restarted_pid=$(start_elbencho_services_srun) || return 1
        if [[ -n "${SRUN_ELBENCHO_PID_FILE:-}" ]] \
                && ! _atomic_write_sentinel "$SRUN_ELBENCHO_PID_FILE" "$restarted_pid"; then
            echo "Error: unable to record restarted elbencho service PID" >&2
            stop_elbencho_services_srun "$restarted_pid" || true
            return 1
        fi
        SRUN_ELBENCHO_PID="$restarted_pid"
        if ! check_elbencho_services_srun; then
            echo "Error: elbencho services still unhealthy after ${phase_name} restart" >&2
            return 1
        fi
    fi
    return 0
}

# Set node_count, remote_output_dir, hosts_csv for SLURM or SSH; mkdir remote_output_dir.
# Caller must: local node_count remote_output_dir hosts_csv
# Assignments intentionally omit 'local' so caller locals are updated.
#
# Resolution precedence (additive; backward-compatible):
#   1. Explicit per-execution overrides (set by the new dispatch model):
#      ELBENCHO_RUN_NODE_COUNT      -> node_count
#      ELBENCHO_RUN_HOSTS_CSV       -> hosts_csv
#      ELBENCHO_RUN_REMOTE_OUTPUT_DIR (optional) -> remote_output_dir
#   2. SLURM env (existing behavior when no override is set)
#   3. SSH env (existing behavior when no override is set and not SLURM)
_elbencho_resolve_run_context() {
    if [ -n "${ELBENCHO_RUN_NODE_COUNT:-}" ]; then
        node_count="$ELBENCHO_RUN_NODE_COUNT"
        hosts_csv="${ELBENCHO_RUN_HOSTS_CSV:-}"
        remote_output_dir="${ELBENCHO_RUN_REMOTE_OUTPUT_DIR:-$output_dir}"
    elif [ -n "${SLURM_JOB_NUM_NODES:-}" ]; then
        node_count="$SLURM_JOB_NUM_NODES"
        remote_output_dir="$output_dir"
        hosts_csv="$nodelist_expanded_comma_separated"
    else
        local _ssh_nodes
        IFS=',' read -ra _ssh_nodes <<< "$SSH_NODELIST"
        node_count="${#_ssh_nodes[@]}"
        remote_output_dir=$(cd "$(pwd)" && pwd)/"$(basename "$output_dir")" || {
            echo "Error: Unable to get absolute path" >&2
            return 1
        }
        hosts_csv="$SSH_NODELIST"
    fi
    mkdir -p "$remote_output_dir" || {
        echo "Error: Unable to create directory" >&2
        return 1
    }
}

# How elbencho learns per-file read/write extent for _elbencho_io_build_common_args (6th argument).
# inferred_extent — omit --size; elbencho uses tree sizes (treescan) or file metadata (single-file path).
ELBENCHO_IO_EXTENT_INFERRED=inferred_extent

# Build common_args for IO sweep (run_elbencho_io_sweep_iteration*).
# Caller must: local common_args
# Args: resfile csvfile livecsvfile this_file_size [enable_nosvcshare] [extent_mode]
# When enable_nosvcshare is non-empty, adds --nosvcshare if ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=1.
# extent_mode: empty — pass --size=this_file_size. ELBENCHO_IO_EXTENT_INFERRED — omit --size (inferred from target).
_elbencho_io_build_common_args() {
    local resfile="$1"
    local csvfile="$2"
    local livecsvfile="$3"
    local this_file_size="$4"
    local enable_nosvcshare="${5:-}"
    local extent_mode="${6:-}"
    case "$extent_mode" in
        "" | "$ELBENCHO_IO_EXTENT_INFERRED") ;;
        *)
            echo "Error: _elbencho_io_build_common_args: invalid extent_mode '$extent_mode' (use '' or ELBENCHO_IO_EXTENT_INFERRED)" >&2
            return 1
            ;;
    esac
    common_args=(--threads="$thread_count")
    if [[ -z "$extent_mode" ]]; then
        common_args+=(--size="$this_file_size")
    fi
    common_args+=(
        --sync
        --lat
        --lathisto
        --latpercent
        --nolive
        --resfile="$resfile"
        --csvfile="$csvfile"
        --iodepth="$io_depth"
    )
    if [[ "${ELBENCHO_LIVE_CSV_EXTENDED:-0}" == "1" ]]; then
        common_args+=(
            --livecsv="$livecsvfile"
            --livecsvex
            --liveint="${ELBENCHO_LIVEINT:-1000}"
        )
    fi
    if [ "$dio_or_bio" = "dio" ]; then
        common_args+=("--direct")
    elif [ "$dio_or_bio" = "bio" ]; then
        common_args+=("--norandalign")
    fi
    if [[ -n "$enable_nosvcshare" && "${ELBENCHO_ALL_NODES_ACCESS_ALL_DATA:-0}" == "1" ]]; then
        common_args+=("--nosvcshare")
    fi
    return 0
}

# Append rotated --hosts to read-phase args (multi-node) using hosts_csv from _elbencho_resolve_run_context.
# Arg: name of read-args array (nameref). Uses node_count, hosts_csv from caller scope.
# ELBENCHO_READ_HOST_ROTATE_STEPS (optional, default 0): added to the historical +1 offset.
# nv-elbencho-sweep.sh advances this across *separate* elbencho invocations so successive jobs on the
# same allocation do not all assign the same hosts to the same file byte ranges (reduces repeated
# client buffer-cache hits across invocations). This is independent of the BIO --infloop decision below.
_elbencho_io_append_rotated_hosts_for_read() {
    local -n _elbencho_read_args_ref="$1"
    if [[ "$node_count" -gt 1 ]]; then
        local rotated_hosts
        local extra="${ELBENCHO_READ_HOST_ROTATE_STEPS:-0}"
        rotated_hosts=$(rotate_csv_list "$hosts_csv" $((1 + extra)))
        _elbencho_read_args_ref+=(--hosts "$rotated_hosts")
    fi
}

# Set resfile and csvfile for standard IO sweep naming. Caller must: local resfile csvfile
_elbencho_io_set_resfile_csvfile() {
    local remote_out="$1"
    local io_sz="$2"
    local nc="$3"
    local tc="$4"
    local id="$5"
    local ds="$6"
    resfile=$(printf "%s/elbencho-%s-c_%03d-s_%03d-d_%03d_%s.out" \
        "$remote_out" "$io_sz" "$nc" "$tc" "$id" "$ds")
    csvfile=$(printf "%s/elbencho-%s-c_%03d-s_%03d-d_%03d_%s.csv" \
        "$remote_out" "$io_sz" "$nc" "$tc" "$id" "$ds")
}

# Set livecsvfile for standard IO sweep naming. Caller must: local livecsvfile
_elbencho_io_set_livecsvfile() {
    local remote_out="$1"
    local io_sz="$2"
    local nc="$3"
    local tc="$4"
    local id="$5"
    local ds="$6"
    livecsvfile=$(printf "%s/elbencho-%s-c_%03d-s_%03d-d_%03d_%s.live.csv" \
        "$remote_out" "$io_sz" "$nc" "$tc" "$id" "$ds")
    return 0
}

# Set treefile for standard IO sweep naming. Caller must: local treefile
_elbencho_io_set_treefile() {
    local remote_out="$1"
    local io_sz="$2"
    local nc="$3"
    local tc="$4"
    local id="$5"
    local ds="$6"
    treefile=$(printf "%s/elbencho-treescan-%s-c_%03d-s_%03d-d_%03d_%s.txt" \
        "$remote_out" "$io_sz" "$nc" "$tc" "$id" "$ds")
    return 0
}

# The many-files --read-from cache is deliberately stored in the dataset, so
# subsequent sweeps can find it without knowing an earlier results directory.
ELBENCHO_TREEFILE_CACHE_BASENAME=.storage-scale-test-elbencho-treefile.txt

_elbencho_treefile_cache_is_eligible() {
    [[ -n "${ELBENCHO_SWEEP_READ_FROM:-}" && "${ELBENCHO_SINGLE_BIG_FILE:-0}" != "1" ]]
}

_elbencho_treefile_cache_path() {
    local read_from="$1"
    printf '%s/%s' "$read_from" "$ELBENCHO_TREEFILE_CACHE_BASENAME"
}

_elbencho_treefile_cache_record() {
    local state="$1"
    local outcome="$2"
    local cache_path="$3"
    local execution_id="${ELBENCHO_RUN_EXECUTION_ID:-unknown}"
    local record_path="${output_dir}/executions/${execution_id}.treefile-cache"
    mkdir -p "$(dirname "$record_path")" || return 1
    printf '%s\t%s\t%s\t%s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")" "$state" \
        "$outcome" "$cache_path" >> "$record_path"
}

# Sets ELBENCHO_TREEFILE_CACHE_STATE, ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE,
# and ELBENCHO_TREEFILE_CACHE_STAGING. A missing cache is staged in the
# dataset parent, so publication into the dataset can use a same-filesystem
# rename and the scanner cannot discover its own output file.
_elbencho_treefile_cache_prepare() {
    local read_from="$1"
    local parent
    parent=$(dirname "$read_from")
    ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE=$(_elbencho_treefile_cache_path "$read_from")
    ELBENCHO_TREEFILE_CACHE_STAGING=""

    if [[ -e "$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE" ]]; then
        if [[ ! -f "$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE" || ! -r "$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE" ]]; then
            echo "Error: cached treefile exists but is not a readable regular file: $ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE" >&2
            return 1
        fi
        ELBENCHO_TREEFILE_CACHE_STATE=hit
        _elbencho_treefile_cache_record hit started "$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE"
        return 0
    fi

    local parent_dev
    local data_dev
    parent_dev=$(stat -c %d "$parent") || return 1
    data_dev=$(stat -c %d "$read_from") || return 1
    if [[ "$parent_dev" != "$data_dev" ]]; then
        ELBENCHO_TREEFILE_CACHE_STATE=unavailable
        echo "Note: dataset parent is on a different filesystem; scanning without a treefile cache: $read_from" >&2
        _elbencho_treefile_cache_record unavailable fallback "$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE"
        return 0
    fi

    ELBENCHO_TREEFILE_CACHE_STAGING=$(mktemp "${parent}/.${ELBENCHO_TREEFILE_CACHE_BASENAME}.tmp.XXXXXX") || return 1
    ELBENCHO_TREEFILE_CACHE_STATE=miss
    _elbencho_treefile_cache_record miss started "$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE"
    return 0
}

_elbencho_treefile_cache_finish() {
    local run_rc="$1"
    local outcome=not-created
    if [[ "$ELBENCHO_TREEFILE_CACHE_STATE" == hit ]]; then
        ELBENCHO_TREEFILE_CACHE_PUBLISH_OUTCOME=reused
        _elbencho_treefile_cache_record hit reused "$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE"
        return 0
    fi

    if [[ "$run_rc" -eq 0 && -f "$ELBENCHO_TREEFILE_CACHE_STAGING" ]]; then
        if [[ -e "$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE" ]]; then
            outcome=already-present
            rm -f -- "$ELBENCHO_TREEFILE_CACHE_STAGING"
        elif mv -n -- "$ELBENCHO_TREEFILE_CACHE_STAGING" "$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE"; then
            if [[ -e "$ELBENCHO_TREEFILE_CACHE_STAGING" ]]; then
                outcome=already-present
                rm -f -- "$ELBENCHO_TREEFILE_CACHE_STAGING"
            else
                outcome=created
            fi
        fi
    fi
    rm -f -- "$ELBENCHO_TREEFILE_CACHE_STAGING"
    case "$outcome" in
        already-present) ELBENCHO_TREEFILE_CACHE_PUBLISH_OUTCOME=already_present ;;
        not-created) ELBENCHO_TREEFILE_CACHE_PUBLISH_OUTCOME=not_created ;;
        *) ELBENCHO_TREEFILE_CACHE_PUBLISH_OUTCOME="$outcome" ;;
    esac
    _elbencho_treefile_cache_record miss "$outcome" "$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE"
    return 0
}

# Count file records and add their sizes exactly. Other valid tree record types
# are ignored; malformed file records fail closed. Empty trees are valid.
# Usage: _elbencho_treefile_aggregate <path> <count-var> <bytes-var>
_elbencho_treefile_aggregate() {
    local tree_path="$1"
    local -n count_ref="$2"
    local -n bytes_ref="$3"
    if [[ ! -f "$tree_path" || ! -r "$tree_path" ]]; then
        echo "Error: treefile is not a readable regular file: $tree_path" >&2
        return 1
    fi
    local aggregate
    aggregate=$(awk '
    function canon(v) { sub(/^0+/, "", v); return v == "" ? "0" : v }
    function add(a, b,    i,j,carry,d,out) {
        i = length(a); j = length(b); carry = 0; out = ""
        while (i > 0 || j > 0 || carry > 0) {
            d = carry
            if (i > 0) d += substr(a, i--, 1)
            if (j > 0) d += substr(b, j--, 1)
            out = (d % 10) out; carry = int(d / 10)
        }
        return canon(out)
    }
    BEGIN { count = "0"; bytes = "0"; failed = 0 }
    $1 == "f" {
        if (NF < 3 || $2 !~ /^[0-9]+$/) { failed = 1; next }
        count = add(count, "1"); bytes = add(bytes, canon($2))
    }
    END {
        if (failed) exit 1
        print canon(count) "\t" canon(bytes)
    }' "$tree_path") || {
        echo "Error: malformed file record in treefile: $tree_path" >&2
        return 1
    }
    # shellcheck disable=SC2034  # Nameref assignments intentionally update caller locals.
    IFS=$'\t' read -r count_ref bytes_ref <<<"$aggregate"
    return 0
}

_elbencho_workload_key_is_valid() {
    case "$1" in
        dataset_count_source|treefile_source|treefile_cache_publish_outcome|\
        requested_files_per_node|effective_files_per_node|dataset_files_total|\
        dataset_bytes_total|reader_nodes|reader_threads_per_node|reader_iodepth|\
        files_per_reader_node|termination_mode|configured_duration_seconds|\
        effective_timelimit_seconds|completion_state|failure_cleanup_state|\
        write_expected_files|write_expected_bytes|write_completed_files|\
        write_completed_bytes|write_elapsed_time_ms|write_completion_state|\
        read_expected_files|\
        read_expected_bytes|read_completed_files|read_completed_bytes|\
        read_elapsed_time_ms|read_completion_state|delete_expected_files|\
        delete_completed_files|delete_elapsed_time_ms|delete_completion_state|\
        write_delete_elapsed_time_ms|lifecycle_elapsed_time_ms) return 0 ;;
        *) return 1 ;;
    esac
}

_elbencho_workload_value_is_valid() {
    [[ "$1" =~ ^[0-9]+$ ]] && return 0
    case "$1" in
        null|configuration|treefile|cache_hit|cache_miss_scan|\
        cache_unavailable_scan|pending|reused|created|already_present|\
        not_created|not_applicable|completion|time_bounded_repeat|\
        single_pass_with_time_ceiling|completed|incomplete|not_started|\
        not_applicable_time_based|not_needed|failed) return 0 ;;
        *) return 1 ;;
    esac
}

_elbencho_workload_begin() {
    ELBENCHO_WORKLOAD_PATH="$1"
    declare -gA ELBENCHO_WORKLOAD_METADATA=()
    ELBENCHO_WORKLOAD_METADATA=()
    return 0
}

_elbencho_workload_set() {
    local key="$1"
    local value="$2"
    _elbencho_workload_key_is_valid "$key" || return 1
    _elbencho_workload_value_is_valid "$value" || return 1
    if [[ "$value" =~ ^[0-9]+$ ]]; then
        value=$(_elbencho_decimal_canonicalize "$value") || return 1
    fi
    ELBENCHO_WORKLOAD_METADATA["$key"]="$value"
    return 0
}

_elbencho_workload_write() {
    [[ -n "${ELBENCHO_WORKLOAD_PATH:-}" ]] || return 1
    local dir
    dir=$(dirname "$ELBENCHO_WORKLOAD_PATH")
    mkdir -p "$dir" || return 1
    local tmp="${ELBENCHO_WORKLOAD_PATH}.tmp.${BASHPID:-$$}.$RANDOM"
    local -a keys=(
        dataset_count_source treefile_source treefile_cache_publish_outcome
        requested_files_per_node effective_files_per_node dataset_files_total
        dataset_bytes_total reader_nodes reader_threads_per_node reader_iodepth
        files_per_reader_node termination_mode configured_duration_seconds
        effective_timelimit_seconds completion_state failure_cleanup_state
        write_expected_files write_expected_bytes write_completed_files
        write_completed_bytes write_elapsed_time_ms write_completion_state
        read_expected_files read_expected_bytes read_completed_files
        read_completed_bytes read_elapsed_time_ms read_completion_state
        delete_expected_files delete_completed_files delete_elapsed_time_ms
        delete_completion_state write_delete_elapsed_time_ms
        lifecycle_elapsed_time_ms
    )
    local key value
    : >"$tmp" || return 1
    for key in "${keys[@]}"; do
        value="${ELBENCHO_WORKLOAD_METADATA[$key]:-null}"
        if ! _elbencho_workload_value_is_valid "$value" \
                || ! printf '%s\t%s\n' "$key" "$value" >>"$tmp"; then
            rm -f -- "$tmp"
            return 1
        fi
    done
    if ! mv -f -- "$tmp" "$ELBENCHO_WORKLOAD_PATH"; then
        rm -f -- "$tmp"
        return 1
    fi
    return 0
}

_elbencho_workload_load() {
    local path="$1"
    [[ -f "$path" && -r "$path" ]] || return 1
    _elbencho_workload_begin "$path"
    local key value extra
    while IFS=$'\t' read -r key value extra; do
        [[ -n "$key" && -z "$extra" ]] || return 1
        _elbencho_workload_key_is_valid "$key" || return 1
        [[ ! -v ELBENCHO_WORKLOAD_METADATA["$key"] ]] || return 1
        _elbencho_workload_value_is_valid "$value" || return 1
        ELBENCHO_WORKLOAD_METADATA["$key"]="$value"
    done <"$path"
    local key_count="${#ELBENCHO_WORKLOAD_METADATA[@]}"
    if [[ "$key_count" -eq 26 ]]; then
        local pair
        local -a legacy_defaults=(
            write_elapsed_time_ms null read_elapsed_time_ms null
            delete_expected_files null delete_completed_files null
            delete_elapsed_time_ms null delete_completion_state not_applicable
            write_delete_elapsed_time_ms null lifecycle_elapsed_time_ms null
        )
        for ((pair=0; pair<${#legacy_defaults[@]}; pair+=2)); do
            [[ ! -v ELBENCHO_WORKLOAD_METADATA["${legacy_defaults[pair]}"] ]] \
                || return 1
            ELBENCHO_WORKLOAD_METADATA["${legacy_defaults[pair]}"]="${legacy_defaults[pair + 1]}"
        done
    elif [[ "$key_count" -ne 34 ]]; then
        return 1
    fi
    return 0
}

_elbencho_workload_update_failure_cleanup_state() {
    local path="$1"
    local state="$2"
    _elbencho_workload_load "$path" || return 1
    _elbencho_workload_set failure_cleanup_state "$state" || return 1
    _elbencho_workload_write
}

# Parse elbencho newline-delimited JSON without a runtime JSON dependency.
# Prints canonical "entries<TAB>bytes-or-null<TAB>elapsed-ms" for one phase.
# WRITE/READ require bytes; RMFILES must not contain a bytes counter.
_elbencho_parse_phase_json() {
    local json_path="$1"
    local expected_phase="$2"
    [[ -f "$json_path" && -r "$json_path" ]] || {
        echo "Error: missing or unreadable ${expected_phase} JSON: $json_path" >&2
        return 1
    }
    awk -v expected="$expected_phase" '
    function fail() { bad = 1 }
    function ws() { while (p <= n && substr(s,p,1) ~ /[ \t\r\n]/) p++ }
    function string(    c,out,hex) {
        if (substr(s,p,1) != "\"") { fail(); return "" }
        p++; out = ""
        while (p <= n) {
            c = substr(s,p++,1)
            if (c == "\"") return out
            if (c == "\\") {
                if (p > n) { fail(); return "" }
                c = substr(s,p++,1)
                if (c == "u") {
                    hex = substr(s,p,4)
                    if (length(hex) != 4 || hex !~ /^[0-9A-Fa-f]+$/) { fail(); return "" }
                    p += 4; out = out "?"
                } else if (c ~ /^["\\\/bfnrt]$/) out = out "?"
                else { fail(); return "" }
            } else {
                if (c ~ /[[:cntrl:]]/) { fail(); return "" }
                out = out c
            }
        }
        fail(); return ""
    }
    function number(    start,token,c) {
        start = p
        while (p <= n && (c = substr(s,p,1)) ~ /[0-9eE+.-]/) p++
        token = substr(s,start,p-start)
        if (token !~ /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$/) fail()
        return token
    }
    function value(ctx,key,    c,token) {
        ws(); c = substr(s,p,1)
        if (c == "{") { object("generic"); return }
        if (c == "[") { array(); return }
        if (c == "\"") { string(); return }
        if (c ~ /[-0-9]/) { number(); return }
        token = substr(s,p,4)
        if (token == "true" || token == "null") { p += 4; return }
        if (substr(s,p,5) == "false") { p += 5; return }
        fail()
    }
    function tracked_decimal(    c,v) {
        ws(); c = substr(s,p,1)
        if (c == "\"") v = string()
        else if (c ~ /[0-9]/) v = number()
        else { fail(); return "" }
        if (v !~ /^(0|[1-9][0-9]*)$/) fail()
        return v
    }
    function array(    c) {
        if (substr(s,p,1) != "[") { fail(); return }
        p++; ws(); if (substr(s,p,1) == "]") { p++; return }
        while (!bad) {
            value("generic", ""); ws(); c = substr(s,p++,1)
            if (c == "]") return
            if (c != ",") { fail(); return }
        }
    }
    function object(ctx,    key,c) {
        if (substr(s,p,1) != "{") { fail(); return }
        p++; ws(); if (substr(s,p,1) == "}") { p++; return }
        while (!bad) {
            ws(); key = string(); ws()
            if (substr(s,p++,1) != ":") { fail(); return }
            if (ctx == "top" && key == "phase_type") {
                phase_count++; ws()
                if (substr(s,p,1) != "\"") { fail(); return }
                phase = string()
            } else if (ctx == "top" && key == "last_done") {
                last_count++; ws()
                if (substr(s,p,1) != "{") { fail(); return }
                object("last")
            } else if (ctx == "last" && key == "entries") {
                entries_count++; entries = tracked_decimal()
            } else if (ctx == "last" && key == "bytes") {
                bytes_count++; bytes = tracked_decimal()
            } else if (ctx == "last" && key == "elapsed_time_ms") {
                elapsed_count++; elapsed = tracked_decimal()
            } else value("generic", key)
            ws(); c = substr(s,p++,1)
            if (c == "}") return
            if (c != ",") { fail(); return }
        }
    }
    BEGIN { records = 0; matches = 0; bad = 0 }
    {
        if ($0 ~ /^[ \t\r]*$/) { fail(); next }
        s = $0; p = 1; n = length(s); records++
        phase_count = last_count = entries_count = bytes_count = elapsed_count = 0
        phase = entries = bytes = elapsed = ""
        ws(); object("top"); ws()
        if (bad || p <= n || phase_count != 1) { fail(); next }
        if (phase == expected) {
            matches++
            if (last_count != 1 || entries_count != 1 || elapsed_count != 1) fail()
            if (expected == "RMFILES" && bytes_count != 0) fail()
            if (expected != "RMFILES" && bytes_count != 1) fail()
            found_entries = entries
            found_bytes = expected == "RMFILES" ? "null" : bytes
            found_elapsed = elapsed
        } else if (phase != "SYNC") fail()
    }
    END {
        if (records == 0 || matches != 1 || bad) exit 1
        print found_entries "\t" found_bytes "\t" found_elapsed
    }' "$json_path" || {
        echo "Error: invalid or ambiguous ${expected_phase} completion JSON: $json_path" >&2
        return 1
    }
}

# Remove stale result artifacts for a single execution attempt.
# Usage: _elbencho_io_cleanup_result_artifacts file1 [file2 ...]
_elbencho_io_cleanup_result_artifacts() {
    local f
    local removed=0
    for f in "$@"; do
        [[ -n "$f" ]] || continue
        if [[ -e "$f" ]]; then
            removed=1
        fi
        if ! rm -f -- "$f"; then
            echo "Error: unable to remove stale elbencho result artifact: $f" >&2
            return 1
        fi
    done
    if [[ "$removed" -eq 1 ]]; then
        echo "Removed stale elbencho result artifacts for this execution"
    fi
    return 0
}

# After read-only elbencho run: SLURM restart check, echo Read-from path, optional treefile stats.
# Second arg: treefile path, or empty to skip stats (e.g. single-big-file read uses file path, no treescan).
_elbencho_finish_sweep_read_from_only() {
    local sweep_read_from="$1"
    local elbencho_treefile_path="${2:-}"
    local rfpath
    local service_rc=0
    rfpath=$(realpath "$sweep_read_from" 2>/dev/null || echo "$sweep_read_from")
    maybe_restart_elbencho_services_slurm "read" || service_rc=$?
    echo "Read-from: ${rfpath}"
    if [[ -n "$elbencho_treefile_path" ]]; then
        elbencho_treefile_print_operator_size_stats "$elbencho_treefile_path" || true
    fi
    return "$service_rc"
}

# Optional pause before a follow-up phase (read or stat).
# Args: [phase_label] [line_prefix] — e.g. _elbencho_maybe_pause_before_read stat "  "
_elbencho_maybe_pause_before_read() {
    local phase_label="${1:-read}"
    local line_prefix="${2:-}"
    if [[ "${ELBENCHO_READ_AFTER_WRITE_PAUSE:-0}" -gt 0 ]]; then
        echo "${line_prefix}Pausing ${ELBENCHO_READ_AFTER_WRITE_PAUSE}s before ${phase_label} phase..."
        sleep "$ELBENCHO_READ_AFTER_WRITE_PAUSE"
    fi
}

# Print write-only data dir line and exit path for callers that return after.
_elbencho_emit_write_only_data_dir() {
    local test_dir0="$1"
    local wpath
    wpath=$(realpath "$test_dir0" 2>/dev/null || echo "$test_dir0")
    echo "ELBENCHO_WRITE_ONLY_DATA_DIR=${wpath}"
}

readonly _ELBE_ISO_DATE_START_PREFIX='ISO DATE start: '
readonly _ELBE_ISO_DATE_END_PREFIX='ISO DATE end  : '

# Parse --resfile from elbencho argv. Prints path to stdout (may be empty).
# Usage: resfile=$(_elbencho_resfile_from_args "$@")
_elbencho_resfile_from_args() {
    local resfile=""
    local skip_next=false
    local arg
    for arg in "$@"; do
        if [[ "$skip_next" == true ]]; then
            resfile="$arg"
            skip_next=false
            continue
        fi
        if [[ "$arg" == --resfile ]]; then
            skip_next=true
            continue
        fi
        if [[ "$arg" == --resfile=* ]]; then
            resfile="${arg#--resfile=}"
        fi
    done
    printf '%s' "$resfile"
    return 0
}

# Rewrite legacy bare "ISO DATE:" lines in a resfile to aligned start/end labels.
# Lines before COMMAND LINE become start; all other bare ISO DATE lines become end.
# Usage: _elbencho_normalize_resfile_iso_date_labels <resfile>
_elbencho_normalize_resfile_iso_date_labels() {
    local resfile="$1"
    if [[ ! -f "$resfile" ]]; then
        return 0
    fi
    awk -v start_pref="${_ELBE_ISO_DATE_START_PREFIX}" \
        -v end_pref="${_ELBE_ISO_DATE_END_PREFIX}" '
    {
        lines[NR] = $0
    }
    END {
        for (i = 1; i <= NR; i++) {
            line = lines[i]
            if (line ~ /^ISO DATE: / && i < NR && lines[i + 1] ~ /^COMMAND LINE:/) {
                line = start_pref substr(line, 11)
            } else if (line ~ /^ISO DATE: /) {
                line = end_pref substr(line, 11)
            }
            print line
        }
    }
    ' "$resfile" > "${resfile}.tmp" && mv -f "${resfile}.tmp" "$resfile"
    return 0
}

# Normalize resfile ISO DATE labels and append a closing end marker after the run.
# Elbencho writes a bare ISO DATE at phase start; this wrapper adds ISO DATE end.
# Usage: _elbencho_append_end_iso_date_to_resfile <elbencho-args...>
_elbencho_append_end_iso_date_to_resfile() {
    local resfile
    resfile=$(_elbencho_resfile_from_args "$@")
    if [[ -n "$resfile" && -f "$resfile" ]]; then
        _elbencho_normalize_resfile_iso_date_labels "$resfile"
        printf '%s%s\n' "$_ELBE_ISO_DATE_END_PREFIX" "$(date +"%Y-%m-%dT%H:%M:%S%z")" >>"$resfile"
    fi
    return 0
}

# Directory where the elbencho master should dump cores (executions dir).
# Uses ELBENCHO_RUN_REMOTE_OUTPUT_DIR when set (dispatch), else OUTPUT_DIR.
# Creates the executions dir if needed (SSH remote may not have it yet).
# Prints path or empty if unknown / unusable.
_elbencho_coredump_dir() {
    local base="${ELBENCHO_RUN_REMOTE_OUTPUT_DIR:-${OUTPUT_DIR:-}}"
    if [[ -z "$base" ]]; then
        return 0
    fi
    local dir="${base}/executions"
    mkdir -p "$dir" 2>/dev/null || true
    if [[ -d "$dir" ]]; then
        printf '%s\n' "$dir"
    fi
    return 0
}

# Rename kernel-produced core / core.<pid> in DIR to <id>.core (latest wins).
# Kernel core_pattern is typically just "core" — we cannot name the dump
# <id>.core without root — so harvest after the process exits.
# Usage: _elbencho_harvest_coredump <dir> <execution_id>
_elbencho_harvest_coredump() {
    local dir="$1"
    local id="$2"
    if [[ -z "$dir" || -z "$id" || ! -d "$dir" ]]; then
        return 0
    fi
    local target="${dir}/${id}.core"
    local f
    local found=0
    local -a candidates=()
    shopt -s nullglob
    candidates=("${dir}/core" "${dir}"/core.[0-9]*)
    shopt -u nullglob
    for f in "${candidates[@]}"; do
        [[ -f "$f" ]] || continue
        if ! mv -f "$f" "$target"; then
            echo "Warning: failed to rename core dump $f -> $target" >&2
            continue
        fi
        found=1
    done
    if [[ "$found" -eq 1 ]]; then
        echo "Core dump saved: $target" >&2
    fi
    return 0
}

# Run elbencho master with core dumps enabled; cwd = executions dir so the
# kernel writes core/core.<pid> there, then rename to <ELBENCHO_RUN_EXECUTION_ID>.core.
# Usage: _elbencho_run_master_with_coredump <elbencho-args...>
# Returns: elbencho exit status.
_elbencho_run_master_with_coredump() {
    local coredump_dir=""
    local exec_id="${ELBENCHO_RUN_EXECUTION_ID:-}"
    local elbencho_cmd="${ELBENCHO:-elbencho}"
    if [[ "$elbencho_cmd" == */* && "$elbencho_cmd" != /* ]]; then
        elbencho_cmd="$(pwd)/$elbencho_cmd"
    fi
    coredump_dir=$(_elbencho_coredump_dir)

    local rc=0
    if [[ -n "$coredump_dir" ]]; then
        (
            # Soft limit is inherited by the elbencho child; unlimited is enough
            # for large master address spaces at 2k-host scale.
            ulimit -c unlimited || true
            cd "$coredump_dir" || exit 1
            "$elbencho_cmd" "$@"
        ) || rc=$?
        if [[ -n "$exec_id" ]]; then
            _elbencho_harvest_coredump "$coredump_dir" "$exec_id"
        fi
    else
        ulimit -c unlimited || true
        "$elbencho_cmd" "$@" || rc=$?
    fi
    return "$rc"
}

run_an_elbencho() {
    # Single pass: detect --hosts and build display args (omitting --hosts for readability)
    local display_args=()
    local has_hosts=false
    local skip_next=false
    local arg
    for arg in "$@"; do
        if [[ "$skip_next" == true ]]; then
            skip_next=false
            continue
        fi
        if [[ "$arg" == --hosts ]]; then
            has_hosts=true
            skip_next=true
            continue
        fi
        if [[ "$arg" == --hosts=* ]]; then
            has_hosts=true
            continue
        fi
        display_args+=("$arg")
    done

    printf "# elbencho %s\n" "${display_args[*]}"
    # Force flush stdout by redirecting file descriptor 1 to itself
    exec 1>&1

    # --svcwait is a ceiling, not a fixed delay; elbencho proceeds as soon as services respond.
    # 120s accommodates large clusters (80+ nodes) where some services start slowly.
    local extra_elbencho_args=(--svcwait 120)

    if [ "$has_hosts" != true ]; then
        # Per-execution override (new dispatch model): the dispatcher pre-set
        # ELBENCHO_RUN_NODE_COUNT and ELBENCHO_RUN_HOSTS_CSV for THIS execution,
        # which may be a subset of the SLURM allocation or a freshly-chosen
        # SSH host set. Use it directly; only emit --hosts when count > 1.
        if [ -n "${ELBENCHO_RUN_NODE_COUNT:-}" ]; then
            if [[ "${ELBENCHO_RUN_NODE_COUNT}" -gt 1 && -n "${ELBENCHO_RUN_HOSTS_CSV:-}" ]]; then
                extra_elbencho_args+=(--hosts "${ELBENCHO_RUN_HOSTS_CSV}")
            fi
            # Single-node override: no --hosts needed
        elif [ -n "${SLURM_JOB_NODELIST:-}" ]; then
            if [ -n "${SLURM_JOB_NUM_NODES:-}" ] && [[ "$SLURM_JOB_NUM_NODES" -gt 1 ]]; then
                extra_elbencho_args+=(--hosts "$nodelist_expanded_comma_separated")
            fi
            # Single node case: no --hosts needed
        else
            extra_elbencho_args+=(--hosts "$SSH_NODELIST")
        fi
    fi

    local rc=0
    _elbencho_run_master_with_coredump "${extra_elbencho_args[@]}" "$@" || rc=$?
    _elbencho_append_end_iso_date_to_resfile "$@"
    return "$rc"
}

# Print treescan treefile size statistics (avg/min/max, or first-file fallback without awk).
# Usage: elbencho_treefile_print_operator_size_stats <treefile_path>
elbencho_treefile_print_operator_size_stats() {
    local treefile="$1"
    if [[ ! -r "$treefile" ]]; then
        echo "Warning: Cannot read treefile for size stats: $treefile" >&2
        return 1
    fi
    if command -v awk >/dev/null 2>&1; then
        awk '/^f / {
            n = $2 + 0
            sum += n
            count++
            if (count == 1) { min = n; max = n }
            else {
                if (n < min) min = n
                if (n > max) max = n
            }
        }
        END {
            if (count > 0)
                printf "Treescan file sizes (bytes): count=%d avg=%.0f min=%d max=%d\n", count, sum / count, min, max
        }' "$treefile"
        return 0
    fi
    local first_line
    first_line=$(grep '^f ' "$treefile" | head -n 1)
    if [[ -z "$first_line" ]]; then
        echo "Warning: No file lines in treefile: $treefile" >&2
        return 1
    fi
    local sz
    sz=$(echo "$first_line" | cut -d' ' -f2)
    echo "Treescan first file size (bytes): $sz"
    echo "Note: min/max/average not computed (awk not available)." >&2
}

# Delete one path in parallel (immediate children) then remove the top path.
# Usage: delete_path_parallel_elbencho_sweep <path>
delete_path_parallel_elbencho_sweep() {
    local delete_path="$1"
    if [[ ! -e "$delete_path" ]]; then
        echo "Error: Path does not exist: $delete_path" >&2
        return 1
    fi
    if [[ -f "$delete_path" ]]; then
        rm -f "$delete_path"
        return 0
    fi
    if [[ ! -d "$delete_path" ]]; then
        echo "Error: Not a file or directory: $delete_path" >&2
        return 1
    fi
    local tmp
    tmp=$(mktemp) || return 1
    # shellcheck disable=SC2064
    trap "rm -f '$tmp'" RETURN
    find "$delete_path" -mindepth 1 -maxdepth 1 -print0 >"$tmp"
    local n
    n=$(tr -cd '\0' <"$tmp" | wc -c)
    n=$(echo "$n" | tr -d ' ')
    printf 'Deleting %s paths in parallel\n' "$n"
    time xargs -0 -P 32 -n 1 rm -rf <"$tmp"
    rmdir "$delete_path" 2>/dev/null || rm -rf "$delete_path"
}

# Remove immediate children of each sweep test dir, then remove the top-level dirs.
# Usage: _elbencho_cleanup_many_files_sweep_test_dirs dir1 [dir2 ...]
_elbencho_cleanup_many_files_sweep_test_dirs() {
    echo "Deleting test dirs $*"
    time find "$@" -mindepth 1 -maxdepth 1 -print0 | xargs -0 -P 32 -n 1 rm -rf
    rmdir "$@" || echo "WARNING: one or more dirs may still contain data: $*"
}

# Remove single-big-file sweep artifact and generated test dir.
# Usage: _elbencho_cleanup_single_big_file_sweep <big_path> <test_dir0>
_elbencho_cleanup_single_big_file_sweep() {
    local big_path="$1"
    local test_dir0="$2"
    echo "Removing single big file: $big_path"
    rm -f "$big_path"
    rmdir "$(dirname "$big_path")" 2>/dev/null || true
    rmdir "$test_dir0" 2>/dev/null || echo "WARNING: test dir may still exist: $test_dir0"
}

# Remove stale generated test dirs before retrying an execution. Missing paths
# are fine; a non-SUCCESS execution may have failed before creating data.
# Usage: _elbencho_cleanup_stale_many_files_sweep_test_dirs dir1 [dir2 ...]
_elbencho_cleanup_stale_many_files_sweep_test_dirs() {
    local -a existing=()
    local dir
    for dir in "$@"; do
        [[ -e "$dir" ]] && existing+=("$dir")
    done
    if [[ ${#existing[@]} -eq 0 ]]; then
        return 0
    fi
    echo "Removing stale test dirs before execution: ${existing[*]}"
    time find "${existing[@]}" -mindepth 1 -maxdepth 1 -print0 | xargs -0 -P 32 -n 1 rm -rf
    rm -rf "${existing[@]}"
}

_elbencho_validate_shared_cleanup_context() {
    local target="${ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV:-}"
    local root="${ELBENCHO_RUN_GENERATED_TEST_ROOT:-${ELBENCHO_RUN_TEST_ROOT:-}}"
    local suffix="${ELBENCHO_RUN_TEST_DIR_SUFFIX:-}"
    local execution_id="${ELBENCHO_RUN_EXECUTION_ID:-}"
    if [[ -z "$target" || "$target" == *,* || -z "$root" || -z "$suffix" \
            || ! "$execution_id" =~ ^[0-9]+$ \
            || -n "${ELBENCHO_SWEEP_READ_FROM:-}" \
            || "${ELBENCHO_FILE_LAYOUT:-worker-directories}" != shared-directory ]]; then
        echo "Error: refusing generated-target cleanup: incomplete or non-generated captured identity" >&2
        return 1
    fi
    if [[ "$suffix" != *"-e${execution_id}" ]]; then
        echo "Error: refusing generated-target cleanup: suffix '$suffix' does not match execution ${execution_id}" >&2
        return 1
    fi
    return 0
}

_elbencho_resolve_shared_cleanup_target() {
    _elbencho_validate_shared_cleanup_context || return 1
    local target="$ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV"
    local root="${ELBENCHO_RUN_GENERATED_TEST_ROOT:-${ELBENCHO_RUN_TEST_ROOT:-}}"
    local suffix="$ELBENCHO_RUN_TEST_DIR_SUFFIX"
    local resolved_target resolved_root
    resolved_target=$(realpath -m -- "$target") || return 1
    resolved_root=$(realpath -m -- "$root") || return 1
    if [[ "$resolved_target" == "$resolved_root" \
            || "$resolved_target" != "$resolved_root"/* \
            || ${#resolved_target} -lt ${#suffix} \
            || "${resolved_target: -${#suffix}}" != "$suffix" ]]; then
        echo "Error: refusing generated-target cleanup: '$resolved_target' is not the exact suffixed descendant of '$resolved_root'" >&2
        return 1
    fi
    printf '%s\n' "$resolved_target"
    return 0
}

# Remove only the exact reified shared-directory target. Every identity input
# is captured in NNNN.sh; this helper never consults current TEST_DIRS.
_elbencho_remove_captured_shared_target() {
    local resolved_target
    resolved_target=$(_elbencho_resolve_shared_cleanup_target) || return 1
    if [[ ! -e "$resolved_target" ]]; then
        return 0
    fi
    echo "Removing generated shared-directory target: $resolved_target"
    if ! rm -rf -- "$resolved_target" || [[ -e "$resolved_target" ]]; then
        echo "Error: failed to remove generated shared-directory target: $resolved_target" >&2
        return 1
    fi
    return 0
}

_elbencho_remove_empty_captured_shared_target() {
    local resolved_target
    resolved_target=$(_elbencho_resolve_shared_cleanup_target) || return 1
    if ! rmdir -- "$resolved_target"; then
        echo "Error: generated shared-directory target is not empty after RMFILES: $resolved_target" >&2
        return 1
    fi
    return 0
}

# Idempotent dispatcher/phase failure finalizer. This is intentionally public
# to the SLURM and SSH dispatch layers after they source the reified NNNN.sh.
_elbencho_finalize_generated_shared_failure() {
    if [[ "${ELBENCHO_FILE_LAYOUT:-worker-directories}" != shared-directory \
            || -z "${ELBENCHO_FILES_PER_NODE:-}" \
            || -n "${ELBENCHO_SWEEP_READ_FROM:-}" ]]; then
        return 0
    fi
    local cleanup_state=completed
    local cleanup_rc=0
    if ! _elbencho_remove_captured_shared_target; then
        cleanup_state=failed
        cleanup_rc=1
        echo "ERROR: GENERATED DATASET FAILURE CLEANUP FAILED; manual cleanup may be required" >&2
    fi
    local workload_path="${ELBENCHO_RUN_REMOTE_OUTPUT_DIR:-${output_dir:-}}/executions/${ELBENCHO_RUN_EXECUTION_ID:-unknown}.workload.tsv"
    if [[ -f "$workload_path" ]] \
            && ! _elbencho_workload_update_failure_cleanup_state \
                "$workload_path" "$cleanup_state"; then
        echo "Error: unable to record failure_cleanup_state=${cleanup_state} in $workload_path" >&2
        cleanup_rc=1
    fi
    return "$cleanup_rc"
}

_elbencho_shared_restore_traps() {
    trap - EXIT INT TERM
    if [[ "${ELBENCHO_SHARED_RESTORE_OLD_TRAPS:-0}" == 1 ]]; then
        [[ -n "${ELBENCHO_SHARED_OLD_EXIT_TRAP:-}" ]] \
            && eval "$ELBENCHO_SHARED_OLD_EXIT_TRAP"
        [[ -n "${ELBENCHO_SHARED_OLD_INT_TRAP:-}" ]] \
            && eval "$ELBENCHO_SHARED_OLD_INT_TRAP"
        [[ -n "${ELBENCHO_SHARED_OLD_TERM_TRAP:-}" ]] \
            && eval "$ELBENCHO_SHARED_OLD_TERM_TRAP"
    fi
    unset ELBENCHO_SHARED_RESTORE_OLD_TRAPS ELBENCHO_SHARED_OLD_EXIT_TRAP \
        ELBENCHO_SHARED_OLD_INT_TRAP ELBENCHO_SHARED_OLD_TERM_TRAP
    return 0
}

_elbencho_shared_exit_cleanup() {
    local original_rc="$?"
    trap - EXIT INT TERM
    if [[ "${ELBENCHO_SHARED_CLEANUP_ARMED:-0}" == 1 ]]; then
        _elbencho_finalize_generated_shared_failure || true
    fi
    return "$original_rc"
}

_elbencho_shared_signal_cleanup() {
    local signal_name="$1"
    local signal_rc=1
    [[ "$signal_name" == INT ]] && signal_rc=130
    [[ "$signal_name" == TERM ]] && signal_rc=143
    trap - EXIT INT TERM
    _elbencho_finalize_generated_shared_failure || true
    exit "$signal_rc"
}

_elbencho_shared_arm_cleanup() {
    ELBENCHO_SHARED_OLD_EXIT_TRAP=$(trap -p EXIT)
    ELBENCHO_SHARED_OLD_INT_TRAP=$(trap -p INT)
    ELBENCHO_SHARED_OLD_TERM_TRAP=$(trap -p TERM)
    # Bash exposes a parent's traps through trap -p in a subshell even though
    # those traps are not active there. Re-evaluating them would activate the
    # coordinator cleanup in its execution child. Only restore traps captured
    # by the owning shell; inherited traps remain reset after disarming.
    ELBENCHO_SHARED_RESTORE_OLD_TRAPS=1
    [[ "$BASHPID" != "$$" ]] && ELBENCHO_SHARED_RESTORE_OLD_TRAPS=0
    ELBENCHO_SHARED_CLEANUP_ARMED=1
    trap '_elbencho_shared_exit_cleanup' EXIT
    trap '_elbencho_shared_signal_cleanup INT' INT
    trap '_elbencho_shared_signal_cleanup TERM' TERM
    return 0
}

_elbencho_shared_disarm_cleanup() {
    ELBENCHO_SHARED_CLEANUP_ARMED=0
    _elbencho_shared_restore_traps
}

_elbencho_shared_fail() {
    local original_rc="$1"
    [[ "$original_rc" -ne 0 ]] || original_rc=1
    _elbencho_finalize_generated_shared_failure || true
    # Leave cleanup armed when it failed so EXIT makes one final attempt.
    if [[ "${ELBENCHO_WORKLOAD_METADATA[failure_cleanup_state]:-failed}" == completed ]]; then
        _elbencho_shared_disarm_cleanup
    fi
    return "$original_rc"
}

# Remove stale single-big-file target dir before retrying an execution.
# Usage: _elbencho_cleanup_stale_single_big_file_sweep <test_dir0>
_elbencho_cleanup_stale_single_big_file_sweep() {
    local test_dir0="$1"
    if [[ ! -e "$test_dir0" ]]; then
        return 0
    fi
    echo "Removing stale single-big-file test dir before execution: $test_dir0"
    rm -rf "$test_dir0"
}

# After a failed elbencho phase: log, run cleanup (best-effort), return the phase rc.
# Cleanup must not mask the original failure. Callers must not attempt later phases.
# Usage: _elbencho_abort_after_phase_failure <rc> [cleanup_fn [cleanup_args...]]
#        then: return $?
_elbencho_abort_after_phase_failure() {
    local rc="$1"
    shift
    echo "Error: elbencho phase failed (rc=$rc); skipping remaining phases and cleaning up" >&2
    if [[ $# -gt 0 ]]; then
        "$@" || true
    fi
    return "$rc"
}

# Run one IO sweep iteration for ELBENCHO_SINGLE_BIG_FILE=1 (single shared file path; sequential only).
# See run_elbencho_io_sweep_iteration for environment variables.
run_elbencho_io_sweep_iteration_single_big_file() {
    local node_count
    local remote_output_dir
    local hosts_csv

    if ! _elbencho_resolve_run_context; then
        return 1
    fi

    local test_dirs
    IFS=',' read -ra test_dirs <<< "$test_dirs_csv"

    local ds="${output_dir##*-}"
    local treefile
    local sweep_write_only="${ELBENCHO_SWEEP_WRITE_ONLY:-0}"
    local sweep_write_no_read="${ELBENCHO_SWEEP_WRITE_NO_READ:-0}"
    local sweep_read_from="${ELBENCHO_SWEEP_READ_FROM:-}"
    local sweep_writes_without_read="0"
    if [[ "$sweep_write_only" == "1" || "$sweep_write_no_read" == "1" ]]; then
        sweep_writes_without_read="1"
    fi
    local big_file_basename="${ELBENCHO_SINGLE_BIG_FILE_BASENAME:-elbencho-bigfile}"

    local io_size="${io_size:?}"
    local write_component
    local read_component
    if [[ "$io_size" == *","* ]]; then
        write_component="${io_size%%,*}"
        read_component="${io_size##*,}"
    else
        write_component="$io_size"
        read_component="$io_size"
    fi
    if [[ "$write_component" == r* || "$read_component" == r* ]]; then
        echo "Error: ELBENCHO_SINGLE_BIG_FILE=1 does not support random IO (remove 'r' prefix from io_size)." >&2
        return 1
    fi

    local this_write_size="$write_component"
    local this_read_size="$read_component"

    if [[ -z "${ELBENCHO_SINGLE_BIG_FILE_SIZE:-}" ]]; then
        if [[ -z "$sweep_read_from" ]]; then
            echo "Error: ELBENCHO_SINGLE_BIG_FILE=1 requires ELBENCHO_SINGLE_BIG_FILE_SIZE (elbencho --size) for write/read phases." >&2
            return 1
        fi
    fi
    local this_file_size="${ELBENCHO_SINGLE_BIG_FILE_SIZE:-}"

    local big_path
    if [[ -n "$sweep_read_from" ]]; then
        big_path="$sweep_read_from"
    else
        big_path="${test_dirs[0]}/${big_file_basename}"
    fi

    local resfile
    local csvfile
    local livecsvfile
    _elbencho_io_set_resfile_csvfile "$remote_output_dir" "$io_size" "$node_count" \
        "$thread_count" "$io_depth" "$ds"
    _elbencho_io_set_livecsvfile "$remote_output_dir" "$io_size" "$node_count" \
        "$thread_count" "$io_depth" "$ds"
    _elbencho_io_set_treefile "$remote_output_dir" "$io_size" "$node_count" \
        "$thread_count" "$io_depth" "$ds"
    _elbencho_io_cleanup_result_artifacts \
        "$resfile" "$csvfile" "$livecsvfile" "$treefile" || return 1

    if [[ -z "$sweep_read_from" ]]; then
        _elbencho_cleanup_stale_single_big_file_sweep "${test_dirs[0]}" || return 1
    fi

    if [[ -z "$sweep_read_from" ]]; then
        mkdir -p "$(dirname "$big_path")" || {
            echo "Error: Unable to create parent directory for $big_path" >&2
            return 1
        }
    fi

    echo "Datestamp: $ds"
    echo "Output Dir: $remote_output_dir"
    echo "Node Count: $node_count"
    echo "Single big file: $big_path"
    if [[ -n "$sweep_read_from" ]]; then
        if [[ -n "$this_file_size" ]]; then
            echo "Read extent: from file (elbencho --size omitted; ELBENCHO_SINGLE_BIG_FILE_SIZE unused for this read)"
        else
            echo "Read extent: from file (no ELBENCHO_SINGLE_BIG_FILE_SIZE; elbencho --size omitted on read)"
        fi
    else
        echo "File Size (-s): $this_file_size"
    fi
    if [[ -z "$sweep_read_from" ]]; then
        echo "Write Block Size: $this_write_size"
    fi
    if [[ "$sweep_writes_without_read" != "1" ]]; then
        echo "Read Block Size: $this_read_size"
    fi
    echo "Threads: $thread_count"
    echo "IO Depth: $io_depth"
    if [[ -n "$sweep_read_from" ]]; then
        echo "IO Pattern: read=seq (ELBENCHO_SINGLE_BIG_FILE)"
    elif [[ "$sweep_writes_without_read" == "1" ]]; then
        echo "IO Pattern: write=seq (ELBENCHO_SINGLE_BIG_FILE)"
    else
        echo "IO Pattern: sequential (ELBENCHO_SINGLE_BIG_FILE)"
    fi
    echo "ELBENCHO_ALL_NODES_ACCESS_ALL_DATA: ${ELBENCHO_ALL_NODES_ACCESS_ALL_DATA:-0}"

    echo "(remote) Human readable output file: $resfile"
    echo "(remote) CSV file: $csvfile"
    if [[ "${ELBENCHO_LIVE_CSV_EXTENDED:-0}" == "1" ]]; then
        echo "(remote) Live CSV file: $livecsvfile"
    fi

    if [[ -n "$sweep_read_from" ]]; then
        # Read-only: file path (same as --read-from); no --treescan/--treefile. Omit --size (ELBENCHO_IO_EXTENT_INFERRED).
        local common_args
        if ! _elbencho_io_build_common_args "$resfile" "$csvfile" "$livecsvfile" \
            "unused" "1" "$ELBENCHO_IO_EXTENT_INFERRED"; then
            return 1
        fi
        local elbencho_read_args=(--read --block="$this_read_size")
        elbencho_read_args+=(--timelimit="$ELBENCHO_SCALE_READ_WRITE_DURATION")
        elbencho_read_args+=("${common_args[@]}")
        # DIO: --infloop repeats the read phase until --timelimit; re-reads bypass page cache.
        # BIO: omit --infloop — with buffered IO, in-process wrap-around would re-touch warm buffer
        # cache and distort throughput; there is no elbencho-side way to avoid that for repeats.
        # BIO therefore does one logical pass (or stops at timelimit if slower): wall time is driven
        # mainly by file size and aggregate read rate, not by timelimit alone. Parameter sweeps can
        # yield a wide spread of actual runtimes. (Host rotation across sweep jobs is unrelated; it
        # only affects *separate* invocations — see ELBENCHO_READ_HOST_ROTATE_STEPS above.)
        if [ "$dio_or_bio" = "dio" ]; then
            elbencho_read_args+=(--infloop)
        fi
        _elbencho_io_append_rotated_hosts_for_read elbencho_read_args
        local read_rc=0
        run_an_elbencho "${elbencho_read_args[@]}" "$big_path" || read_rc=$?
        if [[ "$read_rc" -ne 0 ]]; then
            _elbencho_abort_after_phase_failure "$read_rc"
            return $?
        fi
        _elbencho_finish_sweep_read_from_only "$sweep_read_from" "" || return 1
        return 0
    fi

    local common_args
    _elbencho_io_build_common_args "$resfile" "$csvfile" "$livecsvfile" \
        "$this_file_size" "1"

    # Same --csvfile for write and read: first phase writes CSV column labels; later phases use
    # --nocsvlabels so appends do not repeat the header (no mkdir phase here, so write is first).
    local elbencho_write_args=(
        --write
        --block="$this_write_size"
        "${common_args[@]}"
    )
    local elbencho_read_args=(--read --block="$this_read_size" "${common_args[@]}")
    elbencho_read_args=(--nocsvlabels "${elbencho_read_args[@]}")
    _elbencho_io_append_rotated_hosts_for_read elbencho_read_args

    local write_rc=0
    run_an_elbencho "${elbencho_write_args[@]}" "$big_path" || write_rc=$?
    if [[ "$write_rc" -ne 0 ]]; then
        maybe_restart_elbencho_services_slurm "write" || true
        _elbencho_abort_after_phase_failure "$write_rc" \
            _elbencho_cleanup_single_big_file_sweep "$big_path" "${test_dirs[0]}"
        return $?
    fi

    maybe_restart_elbencho_services_slurm "write" || return 1

    if [[ "$sweep_write_only" == "1" ]]; then
        _elbencho_emit_write_only_data_dir "${test_dirs[0]}"
        return 0
    fi

    if [[ "$sweep_write_no_read" == "1" ]]; then
        _elbencho_cleanup_single_big_file_sweep "$big_path" "${test_dirs[0]}"
        return 0
    fi

    _elbencho_maybe_pause_before_read read

    local read_rc=0
    local service_rc=0
    run_an_elbencho "${elbencho_read_args[@]}" "$big_path" || read_rc=$?
    maybe_restart_elbencho_services_slurm "read" || service_rc=$?
    _elbencho_cleanup_single_big_file_sweep "$big_path" "${test_dirs[0]}" || true
    if [[ "$read_rc" -ne 0 ]]; then
        echo "Error: elbencho READ failed (rc=$read_rc)" >&2
        return "$read_rc"
    fi
    if [[ "$service_rc" -ne 0 ]]; then
        return "$service_rc"
    fi
    return 0
}

# Record one bounded phase and atomically retain its exact counters.
_elbencho_record_bounded_phase_completion() {
    local phase="$1" json_path="$2" expected_files="$3" expected_bytes="$4"
    local parsed completed_files completed_bytes elapsed_ms state=completed
    if ! parsed=$(_elbencho_parse_phase_json "$json_path" "${phase^^}"); then
        _elbencho_workload_set "${phase}_completion_state" incomplete || return 1
        _elbencho_workload_set completion_state incomplete || return 1
        _elbencho_workload_write || true
        return 1
    fi
    IFS=$'\t' read -r completed_files completed_bytes elapsed_ms <<<"$parsed"
    _elbencho_workload_set "${phase}_completed_files" "$completed_files" || return 1
    _elbencho_workload_set "${phase}_completed_bytes" "$completed_bytes" || return 1
    _elbencho_workload_set "${phase}_elapsed_time_ms" "$elapsed_ms" || return 1
    if [[ $(_elbencho_decimal_compare "$completed_files" "$expected_files") -ne 0 \
            || $(_elbencho_decimal_compare "$completed_bytes" "$expected_bytes") -ne 0 ]]; then
        state=incomplete
        echo "Error: ${phase^^} incomplete: expected files=${expected_files} bytes=${expected_bytes}; completed files=${completed_files} bytes=${completed_bytes}" >&2
        _elbencho_workload_set completion_state incomplete || return 1
    fi
    _elbencho_workload_set "${phase}_completion_state" "$state" || return 1
    _elbencho_workload_write || return 1
    [[ "$state" == completed ]]
}

_elbencho_record_bounded_delete_completion() {
    local json_path="$1" expected_files="$2"
    local parsed completed_files unused_bytes elapsed_ms state=completed
    if ! parsed=$(_elbencho_parse_phase_json "$json_path" RMFILES); then
        _elbencho_workload_set delete_completion_state incomplete || return 1
        _elbencho_workload_set completion_state incomplete || return 1
        _elbencho_workload_write || true
        return 1
    fi
    IFS=$'\t' read -r completed_files unused_bytes elapsed_ms <<<"$parsed"
    : "$unused_bytes"
    _elbencho_workload_set delete_completed_files "$completed_files" || return 1
    _elbencho_workload_set delete_elapsed_time_ms "$elapsed_ms" || return 1
    if [[ $(_elbencho_decimal_compare "$completed_files" "$expected_files") -ne 0 ]]; then
        state=incomplete
        echo "Error: RMFILES incomplete: expected files=${expected_files}; completed files=${completed_files}" >&2
        _elbencho_workload_set completion_state incomplete || return 1
    fi
    _elbencho_workload_set delete_completion_state "$state" || return 1
    _elbencho_workload_write || return 1
    [[ "$state" == completed ]]
}

_elbencho_mark_bounded_phase_incomplete() {
    local phase="$1"
    _elbencho_workload_set "${phase}_completion_state" incomplete || return 1
    _elbencho_workload_set completion_state incomplete || return 1
    _elbencho_workload_write
}

_elbencho_shared_workload_begin() {
    local path="$1" files_per_node="$2" total_files="$3" total_bytes="$4"
    local has_read="$5" has_delete="$6"
    local duration_seconds
    duration_seconds=$(_elbencho_duration_to_seconds_exact \
        "$ELBENCHO_SCALE_READ_WRITE_DURATION") || return 1
    _elbencho_workload_begin "$path"
    local pair
    local -a pairs=(
        dataset_count_source configuration treefile_source null
        treefile_cache_publish_outcome not_applicable
        requested_files_per_node "$files_per_node"
        effective_files_per_node "$files_per_node"
        dataset_files_total "$total_files" dataset_bytes_total "$total_bytes"
        reader_nodes null
        reader_threads_per_node null reader_iodepth null
        files_per_reader_node null termination_mode completion
        configured_duration_seconds "$duration_seconds"
        effective_timelimit_seconds null completion_state pending
        failure_cleanup_state not_needed write_expected_files "$total_files"
        write_expected_bytes "$total_bytes" write_completed_files null
        write_completed_bytes null write_elapsed_time_ms null
        write_completion_state not_started
    )
    if [[ "$has_read" == 1 ]]; then
        pairs+=(read_expected_files "$total_files" read_expected_bytes "$total_bytes"
            read_completed_files null read_completed_bytes null
            read_elapsed_time_ms null read_completion_state not_started)
    else
        pairs+=(read_expected_files null read_expected_bytes null
            read_completed_files null read_completed_bytes null
            read_elapsed_time_ms null read_completion_state not_applicable)
    fi
    if [[ "$has_delete" == 1 ]]; then
        pairs+=(delete_expected_files "$total_files" delete_completed_files null
            delete_elapsed_time_ms null delete_completion_state not_started
            write_delete_elapsed_time_ms null lifecycle_elapsed_time_ms null)
    else
        pairs+=(delete_expected_files null delete_completed_files null
            delete_elapsed_time_ms null delete_completion_state not_applicable
            write_delete_elapsed_time_ms null lifecycle_elapsed_time_ms null)
    fi
    for ((pair=0; pair<${#pairs[@]}; pair+=2)); do
        _elbencho_workload_set "${pairs[pair]}" "${pairs[pair + 1]}" || return 1
    done
    _elbencho_workload_write
}

_elbencho_staged_workload_begin() {
    local path="$1" source="$2" outcome="$3" termination="$4"
    local duration_seconds
    duration_seconds=$(_elbencho_duration_to_seconds_exact \
        "$ELBENCHO_SCALE_READ_WRITE_DURATION") || return 1
    _elbencho_workload_begin "$path"
    local pair
    local -a pairs=(
        dataset_count_source treefile treefile_source "$source"
        treefile_cache_publish_outcome "$outcome" requested_files_per_node null
        effective_files_per_node null dataset_files_total pending
        dataset_bytes_total pending reader_nodes "$node_count"
        reader_threads_per_node "$thread_count" reader_iodepth "$io_depth"
        files_per_reader_node null termination_mode "$termination"
        configured_duration_seconds "$duration_seconds"
        effective_timelimit_seconds "$duration_seconds"
        completion_state not_applicable_time_based failure_cleanup_state not_needed
        write_expected_files null write_expected_bytes null
        write_completed_files null write_completed_bytes null
        write_elapsed_time_ms null write_completion_state not_applicable
        read_expected_files null
        read_expected_bytes null read_completed_files null read_completed_bytes null
        read_elapsed_time_ms null read_completion_state not_applicable_time_based
        delete_expected_files null delete_completed_files null
        delete_elapsed_time_ms null delete_completion_state not_applicable
        write_delete_elapsed_time_ms null lifecycle_elapsed_time_ms null
    )
    for ((pair=0; pair<${#pairs[@]}; pair+=2)); do
        _elbencho_workload_set "${pairs[pair]}" "${pairs[pair + 1]}" || return 1
    done
    _elbencho_workload_write
}

_elbencho_staged_set_artifact_paths() {
    local remote_output_dir="$1"
    local ds="$2"
    local id="$3"
    local -n resfile_ref="$4" csvfile_ref="$5" livecsvfile_ref="$6"
    local -n treefile_ref="$7" workload_ref="$8"
    local -a paths
    mapfile -t paths < <(
        local resfile csvfile livecsvfile treefile
        _elbencho_io_set_resfile_csvfile "$remote_output_dir" "$io_size" \
            "$node_count" "$thread_count" "$io_depth" "$ds"
        _elbencho_io_set_livecsvfile "$remote_output_dir" "$io_size" \
            "$node_count" "$thread_count" "$io_depth" "$ds"
        _elbencho_io_set_treefile "$remote_output_dir" "$io_size" \
            "$node_count" "$thread_count" "$io_depth" "$ds"
        printf '%s\n' "$resfile" "$csvfile" "$livecsvfile" "$treefile"
    ) || return 1
    [[ ${#paths[@]} -eq 4 ]] || return 1
    # shellcheck disable=SC2034  # Nameref assignments update runner locals.
    resfile_ref="${paths[0]}"
    # shellcheck disable=SC2034  # Nameref assignments update runner locals.
    csvfile_ref="${paths[1]}"
    # shellcheck disable=SC2034  # Nameref assignments update runner locals.
    livecsvfile_ref="${paths[2]}"
    # shellcheck disable=SC2034  # Nameref assignments update runner locals.
    treefile_ref="${paths[3]}"
    # shellcheck disable=SC2034  # Nameref assignment updates the runner local.
    workload_ref="${remote_output_dir}/executions/${id}.workload.tsv"
    return 0
}

_elbencho_staged_prepare_cache() {
    local sweep_read_from="$1"
    local result_treefile="$2"
    local -n tree_path_ref="$3" tree_source_ref="$4" outcome_ref="$5" use_cache_ref="$6"
    _elbencho_treefile_cache_prepare "$sweep_read_from" || return 1
    # shellcheck disable=SC2034  # Nameref assignments update runner locals.
    case "$ELBENCHO_TREEFILE_CACHE_STATE" in
        hit)
            tree_path_ref="$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE"
            tree_source_ref=cache_hit; outcome_ref=reused; use_cache_ref=1
            ;;
        miss)
            tree_path_ref="$ELBENCHO_TREEFILE_CACHE_STAGING"
            tree_source_ref=cache_miss_scan; outcome_ref=pending; use_cache_ref=1
            ;;
        unavailable)
            tree_path_ref="$result_treefile"
            tree_source_ref=cache_unavailable_scan; outcome_ref=not_applicable; use_cache_ref=0
            ;;
        *) return 1 ;;
    esac
    return 0
}

_elbencho_staged_record_cache_hit_totals() {
    local tree_source="$1"
    local tree_path="$2"
    local files_name="$3"
    local bytes_name="$4"
    local -n files_ref="$files_name"
    local -n bytes_ref="$bytes_name"
    [[ "$tree_source" == cache_hit ]] || {
        echo "Staged dataset totals: pending tree scan"
        return 0
    }
    _elbencho_treefile_aggregate "$tree_path" "$files_name" "$bytes_name" || return 1
    _elbencho_workload_set dataset_files_total "$files_ref" || return 1
    _elbencho_workload_set dataset_bytes_total "$bytes_ref" || return 1
    _elbencho_workload_write
}

_elbencho_staged_build_read_args() {
    local -n args_ref="$1"
    local this_read_size="$2"
    local tree_path="$3"
    local tree_source="$4"
    local sweep_read_from="$5"
    local use_random_read="$6"
    shift 6
    args_ref=(--read --block="$this_read_size"
        --timelimit="$ELBENCHO_SCALE_READ_WRITE_DURATION" "$@"
        --treefile "$tree_path")
    [[ "$tree_source" != cache_hit ]] && args_ref+=(--treescan "$sweep_read_from")
    [[ "$dio_or_bio" == dio ]] && args_ref+=(--infloop)
    [[ "$use_random_read" == 1 ]] && args_ref+=(--rand)
    _elbencho_io_append_rotated_hosts_for_read args_ref
}

_elbencho_staged_capture_totals() {
    local tree_path="$1"
    local files_name="$2"
    local bytes_name="$3"
    local -n files_ref="$files_name"
    local -n bytes_ref="$bytes_name"
    _elbencho_treefile_aggregate "$tree_path" "$files_name" "$bytes_name" || return 1
    _elbencho_workload_set dataset_files_total "$files_ref" || return 1
    _elbencho_workload_set dataset_bytes_total "$bytes_ref"
}

_elbencho_staged_finish_cache() {
    local use_cache="$1"
    local read_rc="$2"
    local aggregate_rc="$3"
    [[ "$use_cache" == 1 ]] || return 0
    local finish_rc="$read_rc"
    [[ "$aggregate_rc" -ne 0 ]] && finish_rc=1
    _elbencho_treefile_cache_finish "$finish_rc" || return 1
    _elbencho_workload_set treefile_cache_publish_outcome \
        "${ELBENCHO_TREEFILE_CACHE_PUBLISH_OUTCOME:-not_created}"
}

_elbencho_staged_execute_read() {
    # shellcheck disable=SC2178  # Nameref resolves the caller's argument array.
    local -n args_ref="$1"
    local sweep_read_from="$2"
    local tree_path="$3"
    local use_cache="$4"
    local files bytes
    local read_rc=0 aggregate_rc=0
    run_an_elbencho "${args_ref[@]}" "$sweep_read_from" || read_rc=$?
    _elbencho_staged_capture_totals "$tree_path" files bytes || aggregate_rc=1
    _elbencho_staged_finish_cache "$use_cache" "$read_rc" \
        "$aggregate_rc" || aggregate_rc=1
    _elbencho_workload_write || aggregate_rc=1
    [[ "$read_rc" -eq 0 ]] || return "$read_rc"
    [[ "$aggregate_rc" -eq 0 ]] || return 1
    echo "Staged dataset: files=${files} bytes=${bytes}"
    echo "Reader topology: nodes=${node_count} threads_per_node=${thread_count} iodepth=${io_depth}"
    echo "Files per reader node: not applicable (staged dataset)"
    _elbencho_finish_sweep_read_from_only "$sweep_read_from" ""
}

run_elbencho_io_sweep_iteration_staged() {
    local node_count remote_output_dir hosts_csv
    _elbencho_resolve_run_context || return 1
    local sweep_read_from="${ELBENCHO_SWEEP_READ_FROM:?}"
    local read_component="$io_size" use_random_read="$use_random"
    [[ "$io_size" == *,* ]] && read_component="${io_size##*,}"
    [[ "$read_component" == r* ]] && use_random_read=1
    local this_read_size="${read_component#r}"
    local id="${ELBENCHO_RUN_EXECUTION_ID:-unknown}"
    local resfile csvfile livecsvfile treefile workload
    _elbencho_staged_set_artifact_paths "$remote_output_dir" \
        "${output_dir##*-}" "$id" resfile csvfile livecsvfile treefile \
        workload || return 1
    _elbencho_io_cleanup_result_artifacts "$resfile" "$csvfile" "$livecsvfile" "$treefile" \
        "${remote_output_dir}/executions/${id}.write.json" \
        "${remote_output_dir}/executions/${id}.read.json" \
        "${remote_output_dir}/executions/${id}.delete.json" "$workload" || return 1
    local tree_path tree_source outcome use_cache
    _elbencho_staged_prepare_cache "$sweep_read_from" "$treefile" \
        tree_path tree_source outcome use_cache || return 1
    local termination=single_pass_with_time_ceiling
    [[ "$dio_or_bio" == dio ]] && termination=time_bounded_repeat
    _elbencho_staged_workload_begin "$workload" "$tree_source" "$outcome" "$termination" || return 1
    local files bytes
    _elbencho_staged_record_cache_hit_totals \
        "$tree_source" "$tree_path" files bytes || return 1
    local common_args
    _elbencho_io_build_common_args "$resfile" "$csvfile" "$livecsvfile" unused "" "$ELBENCHO_IO_EXTENT_INFERRED" || return 1
    # shellcheck disable=SC2034  # Consumed through nameref by the execution helper.
    local -a args
    _elbencho_staged_build_read_args args "$this_read_size" "$tree_path" \
        "$tree_source" "$sweep_read_from" "$use_random_read" "${common_args[@]}"
    _elbencho_staged_execute_read args "$sweep_read_from" "$tree_path" "$use_cache"
}

_elbencho_shared_derive_workload() {
    local -a dirs
    IFS=',' read -ra dirs <<<"$test_dirs_csv"
    [[ ${#dirs[@]} -eq 1 ]] || return 1
    local target="${dirs[0]}" files_per_node files_per_worker
    files_per_node=$(_elbencho_decimal_canonicalize \
        "$ELBENCHO_FILES_PER_NODE") || return 1
    files_per_worker=$(_elbencho_shared_files_per_worker \
        "$files_per_node" "$thread_count") || return 1
    local write_component="$io_size" read_component="$io_size"
    if [[ "$io_size" == *,* ]]; then write_component="${io_size%%,*}"; read_component="${io_size##*,}"; fi
    local random_write="$use_random" random_read="$use_random"
    [[ "$write_component" == r* ]] && random_write=1
    [[ "$read_component" == r* ]] && random_read=1
    local write_size="${write_component#r}" read_size="${read_component#r}"
    local file_size file_bytes total_files total_bytes
    file_size=$(_elbencho_resolve_generated_file_size "$write_size") || return 1
    file_bytes=$(_elbencho_size_to_exact_bytes "$file_size") || return 1
    total_files=$(_elbencho_decimal_multiply \
        "$node_count" "$files_per_node") || return 1
    total_bytes=$(_elbencho_decimal_multiply "$total_files" "$file_bytes") || return 1
    local write_only="${ELBENCHO_SWEEP_WRITE_ONLY:-0}" no_read="${ELBENCHO_SWEEP_WRITE_NO_READ:-0}"
    local has_read=1 has_delete=1
    [[ "$write_only" == 1 || "$no_read" == 1 ]] && has_read=0
    [[ "$write_only" == 1 ]] && has_delete=0
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$target" "$files_per_node" "$files_per_worker" "$random_write" \
        "$random_read" "$write_size" \
        "$read_size" "$file_size" "$total_files" "$total_bytes" \
        "$write_only" "$no_read" "$has_read" "$has_delete" "$file_bytes"
    return 0
}

_elbencho_shared_set_artifact_paths() {
    local remote_output_dir="$1"
    local ds="$2"
    local id="$3"
    local -n resfile_ref="$4" csvfile_ref="$5" livecsvfile_ref="$6"
    local -n treefile_ref="$7" write_json_ref="$8" read_json_ref="$9"
    local -n delete_json_ref="${10}" workload_ref="${11}"
    local -a paths
    mapfile -t paths < <(
        local resfile csvfile livecsvfile treefile
        _elbencho_io_set_resfile_csvfile "$remote_output_dir" "$io_size" \
            "$node_count" "$thread_count" "$io_depth" "$ds"
        _elbencho_io_set_livecsvfile "$remote_output_dir" "$io_size" \
            "$node_count" "$thread_count" "$io_depth" "$ds"
        _elbencho_io_set_treefile "$remote_output_dir" "$io_size" \
            "$node_count" "$thread_count" "$io_depth" "$ds"
        printf '%s\n' "$resfile" "$csvfile" "$livecsvfile" "$treefile"
    ) || return 1
    [[ ${#paths[@]} -eq 4 ]] || return 1
    # shellcheck disable=SC2034  # Nameref assignments update runner locals.
    resfile_ref="${paths[0]}"
    # shellcheck disable=SC2034  # Nameref assignments update runner locals.
    csvfile_ref="${paths[1]}"
    # shellcheck disable=SC2034  # Nameref assignments update runner locals.
    livecsvfile_ref="${paths[2]}"
    # shellcheck disable=SC2034  # Nameref assignments update runner locals.
    treefile_ref="${paths[3]}"
    # shellcheck disable=SC2034  # Nameref assignments update runner locals.
    write_json_ref="${remote_output_dir}/executions/${id}.write.json"
    # shellcheck disable=SC2034  # Nameref assignments update runner locals.
    read_json_ref="${remote_output_dir}/executions/${id}.read.json"
    # shellcheck disable=SC2034  # Nameref assignments update runner locals.
    delete_json_ref="${remote_output_dir}/executions/${id}.delete.json"
    # shellcheck disable=SC2034  # Nameref assignment updates the runner local.
    workload_ref="${remote_output_dir}/executions/${id}.workload.tsv"
    return 0
}

_elbencho_shared_prepare_attempt() {
    local target="$1" workload="$2" files_per_node="$3"
    local total_files="$4" total_bytes="$5"
    local has_read="$6" has_delete="$7"
    shift 7
    if ! _elbencho_io_cleanup_result_artifacts "$resfile" "$csvfile" \
            "$livecsvfile" "$treefile" "$@"; then
        _elbencho_finalize_generated_shared_failure || true
        return 1
    fi
    if ! _elbencho_shared_workload_begin "$workload" "$files_per_node" \
            "$total_files" \
            "$total_bytes" "$has_read" "$has_delete"; then
        _elbencho_finalize_generated_shared_failure || true
        return 1
    fi
    if ! _elbencho_remove_captured_shared_target; then
        _elbencho_workload_set failure_cleanup_state failed || true
        _elbencho_workload_write || true
        return 1
    fi
    if ! mkdir -p -- "$target"; then
        _elbencho_finalize_generated_shared_failure || true
        return 1
    fi
    _elbencho_shared_arm_cleanup
    return 0
}

_elbencho_shared_build_phase_args() {
    local -n mkdir_ref="$1" write_ref="$2" read_ref="$3"
    local write_size="$4" read_size="$5" files_per_worker="$6"
    local write_json="$7" read_json="$8"
    local random_write="$9" random_read="${10}"
    shift 10
    # shellcheck disable=SC2034  # Nameref assignments update runner arrays.
    mkdir_ref=(--mkdirs "$@" --dirs=0 --files="$files_per_worker")
    # shellcheck disable=SC2034  # Nameref assignments update runner arrays.
    write_ref=(--write --block="$write_size" --nocsvlabels "$@"
        --dirs=0 --files="$files_per_worker" --jsonfile="$write_json")
    # shellcheck disable=SC2034  # Nameref assignments update runner arrays.
    read_ref=(--nocsvlabels --read --block="$read_size" "$@"
        --dirs=0 --files="$files_per_worker" --jsonfile="$read_json")
    [[ "$random_write" == 1 ]] && write_ref+=(--rand)
    [[ "$random_read" == 1 ]] && read_ref+=(--rand)
    _elbencho_io_append_rotated_hosts_for_read read_ref
}

_elbencho_shared_build_delete_args() {
    # shellcheck disable=SC2178  # Nameref resolves the caller's argument array.
    local -n args_ref="$1"
    local files_per_worker="$2" delete_json="$3"
    # Deletion is a metadata phase. I/O size, direct-I/O, iodepth, and --sync
    # do not apply and would distort the unlink timing.
    args_ref=(--delfiles --nocsvlabels --threads="$thread_count"
        --dirs=0 --files="$files_per_worker" --nolive --jsonfile="$delete_json")
}

_elbencho_shared_run_mkdir() {
    local target="$1"
    shift
    local phase_rc=0
    run_an_elbencho "$@" "$target" || phase_rc=$?
    [[ "$phase_rc" -eq 0 ]] && return 0
    _elbencho_shared_fail "$phase_rc"
}

_elbencho_shared_run_write() {
    local target="$1" write_json="$2" total_files="$3" total_bytes="$4"
    local complete_after_write="$5"
    shift 5
    rm -f -- "$write_json" || { _elbencho_shared_fail 1; return $?; }
    local phase_rc=0 check_rc=0
    ELBENCHO_SHARED_WRITE_STARTED_MS=$(_elbencho_monotonic_milliseconds) \
        || { _elbencho_shared_fail 1; return $?; }
    run_an_elbencho "$@" "$target" || phase_rc=$?
    _elbencho_record_bounded_phase_completion write "$write_json" \
        "$total_files" "$total_bytes" || check_rc=$?
    if [[ "$phase_rc" -ne 0 ]]; then
        _elbencho_mark_bounded_phase_incomplete write || true
        _elbencho_shared_fail "$phase_rc"
        return $?
    fi
    if [[ "$check_rc" -ne 0 ]]; then
        _elbencho_shared_fail "$check_rc"
        return $?
    fi
    if [[ "$complete_after_write" == 1 ]]; then
        _elbencho_workload_set completion_state completed || check_rc=1
        _elbencho_workload_write || check_rc=1
    fi
    if [[ "$check_rc" -ne 0 ]]; then
        _elbencho_shared_fail "$check_rc"
        return $?
    fi
    maybe_restart_elbencho_services_slurm write || { _elbencho_shared_fail 1; return $?; }
    return 0
}

_elbencho_shared_finish_write_only() {
    local target="$1"
    _elbencho_emit_write_only_data_dir "$target"
    _elbencho_shared_disarm_cleanup
    return 0
}

_elbencho_shared_run_read() {
    local target="$1" read_json="$2" total_files="$3" total_bytes="$4"
    shift 4
    _elbencho_maybe_pause_before_read read
    rm -f -- "$read_json" || { _elbencho_shared_fail 1; return $?; }
    local phase_rc=0 check_rc=0 service_rc=0
    run_an_elbencho "$@" "$target" || phase_rc=$?
    _elbencho_record_bounded_phase_completion read "$read_json" \
        "$total_files" "$total_bytes" || check_rc=$?
    _elbencho_workload_write || check_rc=1
    maybe_restart_elbencho_services_slurm read || service_rc=$?
    if [[ "$phase_rc" -ne 0 ]]; then
        _elbencho_mark_bounded_phase_incomplete read || true
        _elbencho_shared_fail "$phase_rc"
        return $?
    fi
    if [[ "$phase_rc" -ne 0 || "$check_rc" -ne 0 || "$service_rc" -ne 0 ]]; then
        phase_rc="$check_rc"
        [[ "$phase_rc" -ne 0 ]] || phase_rc="$service_rc"
        _elbencho_shared_fail "$phase_rc"
        return $?
    fi
    return 0
}

_elbencho_shared_record_cleanup_timings() {
    local lifecycle_finished_ms="$1"
    local write_ms="${ELBENCHO_WORKLOAD_METADATA[write_elapsed_time_ms]:-}"
    local delete_ms="${ELBENCHO_WORKLOAD_METADATA[delete_elapsed_time_ms]:-}"
    local combined lifecycle
    combined=$(_elbencho_decimal_add "$write_ms" "$delete_ms") || return 1
    lifecycle=$(_elbencho_decimal_subtract "$lifecycle_finished_ms" \
        "$ELBENCHO_SHARED_WRITE_STARTED_MS") || return 1
    _elbencho_workload_set write_delete_elapsed_time_ms "$combined" || return 1
    _elbencho_workload_set lifecycle_elapsed_time_ms "$lifecycle" || return 1
}

_elbencho_shared_run_delete() {
    local target="$1" delete_json="$2" total_files="$3"
    shift 3
    local resolved_target
    resolved_target=$(_elbencho_resolve_shared_cleanup_target) \
        || { _elbencho_shared_fail 1; return $?; }
    [[ "$resolved_target" == "$(realpath -m -- "$target")" ]] \
        || { _elbencho_shared_fail 1; return $?; }
    rm -f -- "$delete_json" || { _elbencho_shared_fail 1; return $?; }
    local phase_rc=0 check_rc=0 lifecycle_finished_ms
    run_an_elbencho "$@" "$resolved_target" || phase_rc=$?
    lifecycle_finished_ms=$(_elbencho_monotonic_milliseconds) || check_rc=1
    _elbencho_record_bounded_delete_completion "$delete_json" \
        "$total_files" || check_rc=$?
    if [[ "$check_rc" -eq 0 ]]; then
        _elbencho_shared_record_cleanup_timings "$lifecycle_finished_ms" \
            || check_rc=1
    fi
    if [[ "$phase_rc" -ne 0 || "$check_rc" -ne 0 ]]; then
        [[ "$phase_rc" -ne 0 ]] || phase_rc="$check_rc"
        _elbencho_workload_set delete_completion_state incomplete || true
        _elbencho_workload_set completion_state incomplete || true
        _elbencho_workload_write || true
        _elbencho_shared_fail "$phase_rc"
        return $?
    fi
    if ! _elbencho_remove_empty_captured_shared_target; then
        _elbencho_workload_set delete_completion_state incomplete || true
        _elbencho_workload_set completion_state incomplete || true
        _elbencho_workload_write || true
        _elbencho_shared_fail 1
        return $?
    fi
    _elbencho_workload_set completion_state completed \
        || { _elbencho_shared_fail 1; return $?; }
    _elbencho_workload_write || { _elbencho_shared_fail 1; return $?; }
    maybe_restart_elbencho_services_slurm delete \
        || { _elbencho_shared_fail 1; return $?; }
    _elbencho_shared_disarm_cleanup
    return 0
}

run_elbencho_io_sweep_iteration_shared_generated() {
    local node_count remote_output_dir hosts_csv
    _elbencho_resolve_run_context || return 1
    local derived target files_per_node files_per_worker
    local random_write random_read write_size read_size
    local file_size total_files total_bytes write_only no_read has_read has_delete file_bytes
    derived=$(_elbencho_shared_derive_workload) || return 1
    IFS=$'\t' read -r target files_per_node files_per_worker random_write \
        random_read write_size read_size file_size total_files total_bytes \
        write_only no_read has_read has_delete file_bytes <<<"$derived"
    : "$file_bytes"
    local id="${ELBENCHO_RUN_EXECUTION_ID:-unknown}"
    local resfile csvfile livecsvfile treefile write_json read_json delete_json workload
    _elbencho_shared_set_artifact_paths "$remote_output_dir" \
        "${output_dir##*-}" "$id" resfile csvfile livecsvfile treefile \
        write_json read_json delete_json workload || return 1
    _elbencho_shared_prepare_attempt "$target" "$workload" "$files_per_node" \
        "$total_files" "$total_bytes" "$has_read" "$has_delete" \
        "$write_json" "$read_json" "$delete_json" "$workload" || return 1
    echo "Generated shared-directory: --nodes=${node_count}; requested ELBENCHO_FILES_PER_NODE=${files_per_node}; effective files per node=${files_per_node}; ELBENCHO_SCALE_THREAD_LIST value=${thread_count}; files per worker=${files_per_worker}; ELBENCHO_IODEPTH_LIST value=${io_depth}"
    echo "File size=${file_size} total_files=${total_files} total_bytes=${total_bytes}; duration=not applicable (completion-based)"
    local common_args
    _elbencho_io_build_common_args "$resfile" "$csvfile" "$livecsvfile" "$file_size" || { _elbencho_shared_fail 1; return $?; }
    local -a mkdir_args write_args read_args
    _elbencho_shared_build_phase_args mkdir_args write_args read_args \
        "$write_size" "$read_size" "$files_per_worker" "$write_json" \
        "$read_json" \
        "$random_write" "$random_read" "${common_args[@]}"
    local -a delete_args
    _elbencho_shared_build_delete_args delete_args "$files_per_worker" \
        "$delete_json"
    _elbencho_shared_run_mkdir "$target" "${mkdir_args[@]}" || return $?
    _elbencho_shared_run_write "$target" "$write_json" "$total_files" \
        "$total_bytes" "$write_only" "${write_args[@]}" || return $?
    if [[ "$write_only" == 1 ]]; then
        _elbencho_shared_finish_write_only "$target"
        return $?
    fi
    if [[ "$no_read" == 1 ]]; then
        _elbencho_shared_run_delete "$target" "$delete_json" "$total_files" \
            "${delete_args[@]}"
        return $?
    fi
    _elbencho_shared_run_read "$target" "$read_json" "$total_files" \
        "$total_bytes" "${read_args[@]}" || return $?
    _elbencho_shared_run_delete "$target" "$delete_json" "$total_files" \
        "${delete_args[@]}"
}

# Run a single IO benchmark iteration with elbencho
# See header comments for required environment variables
run_elbencho_io_sweep_iteration() {
    local node_count
    local remote_output_dir

    if [[ "${ELBENCHO_SINGLE_BIG_FILE:-0}" == "1" ]]; then
        run_elbencho_io_sweep_iteration_single_big_file
        return $?
    fi

    if [[ -n "${ELBENCHO_SWEEP_READ_FROM:-}" ]]; then
        run_elbencho_io_sweep_iteration_staged
        return $?
    fi
    if [[ "${ELBENCHO_FILE_LAYOUT:-worker-directories}" == shared-directory \
            && -n "${ELBENCHO_FILES_PER_NODE:-}" ]]; then
        run_elbencho_io_sweep_iteration_shared_generated
        return $?
    fi

    local hosts_csv
    if ! _elbencho_resolve_run_context; then
        return 1
    fi

    # Convert test dirs CSV string to array
    local test_dirs
    IFS=',' read -ra test_dirs <<< "$test_dirs_csv"

    local ds="${output_dir##*-}"
    local treefile
    local sweep_write_only="${ELBENCHO_SWEEP_WRITE_ONLY:-0}"
    local sweep_write_no_read="${ELBENCHO_SWEEP_WRITE_NO_READ:-0}"
    local sweep_read_from="${ELBENCHO_SWEEP_READ_FROM:-}"
    local sweep_writes_without_read="0"
    if [[ "$sweep_write_only" == "1" || "$sweep_write_no_read" == "1" ]]; then
        sweep_writes_without_read="1"
    fi

    local infinite_file_count=2000000  # for time-bounded write case.

    # Initialize per-IO use_random settings from global use_random
    local use_random_write="$use_random"
    local use_random_read="$use_random"

    local write_component
    local read_component
    if [[ "$io_size" == *","* ]]; then
        write_component="${io_size%%,*}"
        read_component="${io_size##*,}"
        if [[ -z "$write_component" || -z "$read_component" ]]; then
            echo "Error: Invalid io_size string '$io_size'. Both write and read components must be non-empty (got write='$write_component', read='$read_component')." >&2
            return 1
        fi
    else
        write_component="$io_size"
        read_component="$io_size"
    fi

    # Parse and strip leading "r" if present, setting per-write/read flags
    local this_write_size
    local this_read_size
    if [[ "$write_component" == r* ]]; then
        use_random_write="1"
        this_write_size="${write_component:1}"
    else
        this_write_size="$write_component"
    fi

    if [[ "$read_component" == r* ]]; then
        use_random_read="1"
        this_read_size="${read_component:1}"
    else
        this_read_size="$read_component"
    fi

    local this_file_size
    if [[ -n "${ELBENCHO_FILE_SIZE:-}" ]]; then
        this_file_size="$ELBENCHO_FILE_SIZE"
    else
        local this_write_bytes
        this_write_bytes=$(elbencho_size_string_to_bytes "$this_write_size")
        local file_size_multiplier="${ELBENCHO_FILE_SIZE_MULTIPLIER:-1024}"
        this_file_size=$(bytes_to_elbencho_size_string "$((this_write_bytes * file_size_multiplier))")
    fi

    echo "Datestamp: $ds"
    echo "Output Dir: $remote_output_dir"
    echo "Node Count: $node_count"
    # Treefile is created during the read below; avg/min/max are not known until afterward
    # (see "Treescan file sizes" from elbencho_treefile_print_operator_size_stats in
    # _elbencho_finish_sweep_read_from_only). Read-from path is echoed after the run as "Read-from:".
    if [[ -n "$sweep_read_from" ]]; then
        echo "File sizes: from existing files (treescan avg/min/max are printed after this read completes)"
    else
        echo "File Size: $this_file_size"
    fi
    if [[ -z "$sweep_read_from" ]]; then
        echo "Write Block Size: $this_write_size"
    fi
    if [[ "$sweep_writes_without_read" != "1" ]]; then
        echo "Read Block Size: $this_read_size"
    fi
    echo "Threads: $thread_count"
    echo "IO Depth: $io_depth"
    if [[ -n "$sweep_read_from" ]]; then
        printf "IO Pattern: read=%s\n" \
            "$([ "$use_random_read" = "1" ] && echo "rand" || echo "seq")"
    elif [[ "$sweep_writes_without_read" == "1" ]]; then
        printf "IO Pattern: write=%s\n" \
            "$([ "$use_random_write" = "1" ] && echo "rand" || echo "seq")"
    else
        printf "IO Pattern: write=%s, read=%s\n" \
            "$([ "$use_random_write" = "1" ] && echo "rand" || echo "seq")" \
            "$([ "$use_random_read" = "1" ] && echo "rand" || echo "seq")"
    fi
    echo "Force Single Run: $([ "$force_single" = "1" ] && echo "Yes" || echo "No")"

    local resfile
    local csvfile
    local livecsvfile
    _elbencho_io_set_resfile_csvfile "$remote_output_dir" "$io_size" "$node_count" \
        "$thread_count" "$io_depth" "$ds"
    _elbencho_io_set_livecsvfile "$remote_output_dir" "$io_size" "$node_count" \
        "$thread_count" "$io_depth" "$ds"
    _elbencho_io_set_treefile "$remote_output_dir" "$io_size" "$node_count" \
        "$thread_count" "$io_depth" "$ds"
    _elbencho_io_cleanup_result_artifacts \
        "$resfile" "$csvfile" "$livecsvfile" "$treefile" || return 1

    # Create test directories (skip when reading pre-existing tree only)
    if [[ -z "$sweep_read_from" ]]; then
        _elbencho_cleanup_stale_many_files_sweep_test_dirs "${test_dirs[@]}" || return 1
        mkdir -p "${test_dirs[@]}" || {
            echo "Error: Unable to create test directories: ${test_dirs[*]}" >&2
            return 1
        }
    fi

    echo "(remote) Human readable output file: $resfile"
    echo "(remote) CSV file: $csvfile"
    if [[ "${ELBENCHO_LIVE_CSV_EXTENDED:-0}" == "1" ]]; then
        echo "(remote) Live CSV file: $livecsvfile"
    fi

    local common_args
    _elbencho_io_build_common_args "$resfile" "$csvfile" "$livecsvfile" \
        "$this_file_size"

    local use_treefile_cache=0
    local read_treefile="$treefile"
    if _elbencho_treefile_cache_is_eligible; then
        use_treefile_cache=1
        if ! _elbencho_treefile_cache_prepare "$sweep_read_from"; then
            return 1
        fi
        if [[ "$ELBENCHO_TREEFILE_CACHE_STATE" == hit ]]; then
            read_treefile="$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE"
        elif [[ "$ELBENCHO_TREEFILE_CACHE_STATE" == miss ]]; then
            read_treefile="$ELBENCHO_TREEFILE_CACHE_STAGING"
        else
            use_treefile_cache=0
        fi
        echo "Treefile cache: ${ELBENCHO_TREEFILE_CACHE_STATE} (${ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE})"
    fi

    # Not sure why or how, but some aspect of the mkdirs runtime is proportional
    # to the file count.  To keep that from starving our write timeout, if applicable,
    # we run the mkdirs by itself first.
    # Same --csvfile for mkdir, write, read: mkdir runs first without --nocsvlabels (one header row);
    # write and read use --nocsvlabels so they only append data (never a second header).
    local elbencho_mkdir_args=(
        --mkdirs
        "${common_args[@]}"
    )
    local elbencho_write_args=(
        --write
        --block="$this_write_size"
        --nocsvlabels
        "${common_args[@]}"
    )
    # --read-from runs only this read phase; include CSV column labels so extract-elbencho.py
    # can parse (nocsvlabels would emit headerless CSV and break DictReader).
    local elbencho_read_args=(
        --read
        --block="$this_read_size"
        --timelimit="$ELBENCHO_SCALE_READ_WRITE_DURATION"
        "${common_args[@]}"
    )
    if [[ -z "$sweep_read_from" ]]; then
        elbencho_read_args=(--nocsvlabels "${elbencho_read_args[@]}")
    fi
    # For DIO: use --infloop for time-bounded reads (re-reads hit disk, not cache)
    # For BIO: skip --infloop to avoid re-reads hitting page cache (inflating throughput numbers)
    # Reads will stop at timelimit OR after one pass through files, whichever comes first.
    if [ "$dio_or_bio" = "dio" ]; then
        elbencho_read_args+=(--infloop)
    fi
    # Rotate hosts for read phase to avoid caching effects (node B reads node A's data)
    # Note: --rotatehosts only works within a single elbencho invocation with multiple phases.
    # Since we run write and read as separate invocations, we must manually rotate the hosts list.
    _elbencho_io_append_rotated_hosts_for_read elbencho_read_args
    # Conditionally add --rand to write and/or read, depending on use_random_write/use_random_read
    if [ "$use_random_write" = "1" ]; then
        elbencho_write_args+=("--rand")
    fi
    if [ "$use_random_read" = "1" ]; then
        elbencho_read_args+=("--rand")
    fi

    local target_file_count
    # Multi-bench-path / force_single: mkdir+write+read use --files counts; reads do not
    # use --treescan/--treefile (no per-job treescan file for stats in this branch).
    if [[ ( ${#test_dirs[@]} -gt 1 || "$force_single" = "1" ) && -z "$sweep_read_from" ]]; then
        # Compute target file count from write size
        target_file_count=$(compute_target_file_count_per_thread \
            "$this_write_size" "$this_file_size" "$node_count" \
            "$thread_count" "$ELBENCHO_SCALE_READ_WRITE_DURATION") || return 1
        echo "Using computed target file count: $target_file_count"
        elbencho_mkdir_args+=(
            --files="$target_file_count"
        )
        elbencho_write_args+=(
            --files="$target_file_count"
        )
        elbencho_read_args+=(
            --files="$target_file_count"
        )
    else
        elbencho_mkdir_args+=(
            --files="$infinite_file_count"
        )
        elbencho_write_args+=(
            --infloop
            --files="$infinite_file_count"
            --timelimit="$ELBENCHO_SCALE_READ_WRITE_DURATION"
        )
        elbencho_read_args+=(
            --treefile "$read_treefile"
        )
        if [[ "$use_treefile_cache" -eq 0 || "$ELBENCHO_TREEFILE_CACHE_STATE" == miss ]]; then
            elbencho_read_args+=(--treescan "${test_dirs[0]}")
        fi
    fi

    if [[ -n "$sweep_read_from" ]]; then
        local read_rc=0
        run_an_elbencho "${elbencho_read_args[@]}" "${test_dirs[@]}" || read_rc=$?
        if [[ "$use_treefile_cache" -eq 1 ]]; then
            _elbencho_treefile_cache_finish "$read_rc" || true
            if [[ -f "$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE" ]]; then
                read_treefile="$ELBENCHO_TREEFILE_CACHE_PATH_ACTIVE"
            fi
        fi
        if [[ "$read_rc" -ne 0 ]]; then
            _elbencho_abort_after_phase_failure "$read_rc"
            return $?
        fi
        _elbencho_finish_sweep_read_from_only "$sweep_read_from" "$read_treefile" || return 1
        return 0
    fi

    local mkdir_rc=0
    run_an_elbencho "${elbencho_mkdir_args[@]}" "${test_dirs[@]}" || mkdir_rc=$?
    if [[ "$mkdir_rc" -ne 0 ]]; then
        _elbencho_abort_after_phase_failure "$mkdir_rc" \
            _elbencho_cleanup_many_files_sweep_test_dirs "${test_dirs[@]}"
        return $?
    fi

    local write_rc=0
    run_an_elbencho "${elbencho_write_args[@]}" "${test_dirs[@]}" || write_rc=$?
    if [[ "$write_rc" -ne 0 ]]; then
        maybe_restart_elbencho_services_slurm "write" || true
        _elbencho_abort_after_phase_failure "$write_rc" \
            _elbencho_cleanup_many_files_sweep_test_dirs "${test_dirs[@]}"
        return $?
    fi

    maybe_restart_elbencho_services_slurm "write" || return 1

    if [[ "$sweep_write_only" == "1" ]]; then
        _elbencho_emit_write_only_data_dir "${test_dirs[0]}"
        return 0
    fi

    if [[ "$sweep_write_no_read" == "1" ]]; then
        _elbencho_cleanup_many_files_sweep_test_dirs "${test_dirs[@]}"
        return 0
    fi

    _elbencho_maybe_pause_before_read read

    local read_rc=0
    local service_rc=0
    run_an_elbencho "${elbencho_read_args[@]}" "${test_dirs[@]}" || read_rc=$?

    maybe_restart_elbencho_services_slurm "read" || service_rc=$?

    if [[ -f "$treefile" ]]; then
        elbencho_treefile_print_operator_size_stats "$treefile" || true
    fi

    _elbencho_cleanup_many_files_sweep_test_dirs "${test_dirs[@]}" || true
    if [[ "$read_rc" -ne 0 ]]; then
        echo "Error: elbencho READ failed (rc=$read_rc)" >&2
        return "$read_rc"
    fi
    if [[ "$service_rc" -ne 0 ]]; then
        return "$service_rc"
    fi
    return 0
}

# =============================================================================
# Execution reification + status sentinel helpers
# =============================================================================
#
# An "execution" is one (nodes, io_size, thread_count, io_depth) tuple.
# Reification writes one sourceable shell file per execution under
#   ${OUTPUT_DIR}/executions/NNNN.sh
# along with a sibling status sentinel
#   ${OUTPUT_DIR}/executions/NNNN.status
# initially containing "PENDING". Status transitions to RUNNING -> SUCCESS
# or RUNNING -> FAILED via the dispatcher / coordinator / SSH worker.

# Atomically write a single line to a status / sentinel file.
# Usage: _atomic_write_sentinel <path> <value>
_atomic_write_sentinel() {
    local f="$1"
    local val="$2"
    local tmp="${f}.tmp.${BASHPID:-$$}.$RANDOM"
    if ! printf '%s\n' "$val" > "$tmp"; then
        rm -f -- "$tmp"
        return 1
    fi
    if ! mv -f "$tmp" "$f"; then
        rm -f -- "$tmp"
        return 1
    fi
    return 0
}

# Sweep any executions/NNNN.status == RUNNING back to PENDING. Used at the
# start of every dispatch (initial and resume) to recover from prior crashes
# / walltime kills / scancel where an in-flight execution was left RUNNING.
# Usage: _elbencho_sweep_running_to_pending <executions_dir>
_elbencho_sweep_running_to_pending() {
    local executions_dir="$1"
    local f
    local count=0
    if [[ ! -d "$executions_dir" ]]; then
        return 0
    fi
    for f in "$executions_dir"/*.status; do
        [[ -f "$f" ]] || continue
        if [[ "$(cat "$f" 2>/dev/null)" == "RUNNING" ]]; then
            _atomic_write_sentinel "$f" PENDING || return 1
            count=$((count + 1))
        fi
    done
    if [[ "$count" -gt 0 ]]; then
        echo "Reset $count interrupted execution(s) from RUNNING to PENDING"
    fi
    return 0
}

# Reify one execution definition file.
# Usage: reify_elbencho_execution <out_path> <nodes> <io_size> <thread_count> <io_depth> \
#                                 <rotate_steps> <dio_or_bio> <use_random> <force_single> \
#                                 <sweep_write_only> <sweep_write_no_read> <sweep_read_from>
# Captures both per-execution (varying) values and shared scalars so that any
# single NNNN.sh is fully self-describing (forensic readability) and source-able
# by either the SLURM coordinator's per-execution srun step or the SSH inline
# scriptlet on the remote.
reify_elbencho_execution() {
    local out_path="$1"
    local nodes="$2"
    local io_size="$3"
    local thread_count="$4"
    local io_depth="$5"
    local rotate_steps="$6"
    local dio_or_bio="$7"
    local use_random="$8"
    local force_single="$9"
    local sweep_write_only="${10}"
    local sweep_write_no_read="${11}"
    local sweep_read_from="${12}"
    local ds="${DS:-}"
    if [[ -z "$ds" ]]; then
        echo "Error: DS must be set before reifying elbencho executions" >&2
        return 1
    fi
    local execution_id
    execution_id=$(basename "$out_path" .sh)
    local treefile_cache_path=""
    if [[ -n "$sweep_read_from" && "${ELBENCHO_SINGLE_BIG_FILE:-0}" != "1" ]]; then
        treefile_cache_path=$(_elbencho_treefile_cache_path "$sweep_read_from")
    fi
    local run_test_dir_suffix="-${ds}-e${execution_id}"
    local -a run_test_dirs=()
    if ! mapfile -t run_test_dirs < <(
        FS_TEST_DIR_SUFFIX_OVERRIDE="$run_test_dir_suffix" \
            generate_fs_test_directories "elbencho-sweep"
    ); then
        echo "Error: failed to compute test dirs for execution ${execution_id}" >&2
        return 1
    fi
    local run_generated_test_dirs_csv
    run_generated_test_dirs_csv=$(IFS=,; printf '%s' "${run_test_dirs[*]}")
    local run_generated_test_root=""
    if [[ "${ELBENCHO_FILE_LAYOUT:-worker-directories}" == shared-directory ]]; then
        local test_root
        for test_root in "${!TEST_DIRS[@]}"; do
            run_generated_test_root="$test_root"
        done
    fi

    {
        printf '# Auto-generated; do not edit. '
        printf 'Coords: nodes=%s io_size=%s thread_count=%s io_depth=%s\n' \
            "$nodes" "$io_size" "$thread_count" "$io_depth"
        printf '# Generated: %s\n' "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
        # 3-line compact sweep summary (same across every NNNN.sh in this run).
        # Self-describes the sweep for forensic readability even if a NNNN.sh
        # is grabbed out of context. Reads $DS, $nodes_spec, $dio_or_bio,
        # $rand_option, and ELBENCHO_* arrays/scalars from the surrounding
        # dynamic scope (reify_all_elbencho_executions -> nv-elbencho-sweep.sh).
        print_elbencho_sweep_compact_summary \
            "${DS:-?}" "${nodes_spec:-?}" "# "
        printf '#\n'
        printf '# Per-execution coordinates (vary across NNNN.sh files):\n'
        printf 'export nodes=%s\n' "$nodes"
        printf 'export io_size="%s"\n' "$io_size"
        printf 'export thread_count=%s\n' "$thread_count"
        printf 'export io_depth=%s\n' "$io_depth"
        printf 'export ELBENCHO_READ_HOST_ROTATE_STEPS=%s\n' "$rotate_steps"
        printf 'export ELBENCHO_RUN_TEST_DIR_SUFFIX=%q\n' "$run_test_dir_suffix"
        printf 'export ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV=%q\n' \
            "$run_generated_test_dirs_csv"
        printf 'export ELBENCHO_RUN_GENERATED_TEST_ROOT=%q\n' \
            "$run_generated_test_root"
        printf '#\n'
        printf '# Sweep-level scalars (same across all NNNN.sh files in this run):\n'
        printf 'export dio_or_bio="%s"\n' "$dio_or_bio"
        printf 'export use_random=%s\n' "$use_random"
        printf 'export force_single=%s\n' "$force_single"
        printf 'export ELBENCHO_SWEEP_WRITE_ONLY=%s\n' "$sweep_write_only"
        printf 'export ELBENCHO_SWEEP_WRITE_NO_READ=%s\n' "$sweep_write_no_read"
        printf 'export ELBENCHO_SWEEP_READ_FROM=%q\n' "$sweep_read_from"
        printf 'export ELBENCHO_TREEFILE_CACHE_PATH=%q\n' "$treefile_cache_path"
        printf 'export ELBENCHO_SCALE_READ_WRITE_DURATION=%s\n' \
            "${ELBENCHO_SCALE_READ_WRITE_DURATION}"
        printf 'export ELBENCHO_LIVE_CSV_EXTENDED=%s\n' \
            "${ELBENCHO_LIVE_CSV_EXTENDED:-0}"
        printf 'export ELBENCHO_LIVEINT=%s\n' \
            "${ELBENCHO_LIVEINT:-1000}"
        printf 'export ELBENCHO_FILE_SIZE_MULTIPLIER=%s\n' \
            "${ELBENCHO_FILE_SIZE_MULTIPLIER:-1024}"
        printf 'export ELBENCHO_FILE_LAYOUT=%q\n' \
            "${ELBENCHO_FILE_LAYOUT:-worker-directories}"
        printf 'export ELBENCHO_FILES_PER_NODE=%q\n' \
            "${ELBENCHO_FILES_PER_NODE:-}"
        printf 'export ELBENCHO_FILE_SIZE=%q\n' "${ELBENCHO_FILE_SIZE:-}"
        printf 'export ELBENCHO_READ_AFTER_WRITE_PAUSE=%s\n' \
            "${ELBENCHO_READ_AFTER_WRITE_PAUSE:-0}"
        printf 'export FS_MAX_AGG_THROUGHPUT=%s\n' "${FS_MAX_AGG_THROUGHPUT}"
        printf 'export FS_MAX_NODE_THROUGHPUT_GBPS=%s\n' \
            "${FS_MAX_NODE_THROUGHPUT_GBPS}"
        printf 'export FS_MAX_NODE_IOPS=%s\n' "${FS_MAX_NODE_IOPS}"
        printf 'export ELBENCHO_SINGLE_BIG_FILE=%s\n' \
            "${ELBENCHO_SINGLE_BIG_FILE:-0}"
        printf 'export ELBENCHO_SINGLE_BIG_FILE_BASENAME=%q\n' \
            "${ELBENCHO_SINGLE_BIG_FILE_BASENAME:-elbencho-bigfile}"
        printf 'export ELBENCHO_SINGLE_BIG_FILE_SIZE=%q\n' \
            "${ELBENCHO_SINGLE_BIG_FILE_SIZE:-}"
        printf 'export ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=%s\n' \
            "${ELBENCHO_ALL_NODES_ACCESS_ALL_DATA:-0}"
    } > "$out_path" || return 1
    return 0
}

# Cartesian-product reifier: for the inner two loops only (thread_count x io_depth)
# inside a fixed (nodes, io_size). Factored out of reify_all_elbencho_executions
# to keep each function's cognitive complexity within the project limit of 15.
# Usage: _reify_inner_threads_iodepth <executions_dir> <nodes> <io_size> \
#            <dio_or_bio> <use_random> <force_single> \
#            <sweep_write_only> <sweep_write_no_read> <sweep_read_from> <seq_var_name>
_reify_inner_threads_iodepth() {
    local executions_dir="$1"
    local nodes="$2"
    local io_size="$3"
    local dio_or_bio="$4"
    local use_random="$5"
    local force_single="$6"
    local sweep_write_only="$7"
    local sweep_write_no_read="$8"
    local sweep_read_from="$9"
    local -n _seq_ref="${10}"

    local thread_count
    local io_depth
    local nnnn
    local rotate_steps
    for thread_count in "${ELBENCHO_SCALE_THREAD_LIST[@]}"; do
        for io_depth in "${ELBENCHO_IODEPTH_LIST[@]}"; do
            nnnn=$(printf '%04d' "$_seq_ref")
            # ELBENCHO_READ_HOST_ROTATE_STEPS uses 0-based cumulative index over
            # the full sweep order (nodes, io_size, thread_count, io_depth) so
            # successive elbencho invocations advance their host-to-byte mapping
            # for the read phase. This matches the historical formula:
            #   base += inner_runs_per_job  for each (nodes, io_size)
            #   inner += 1                  for each (thread, iodepth)
            # which collapses to seq-1 because step is always +1 per execution.
            rotate_steps=$((_seq_ref - 1))
            if ! reify_elbencho_execution \
                    "${executions_dir}/${nnnn}.sh" \
                    "$nodes" "$io_size" "$thread_count" "$io_depth" \
                    "$rotate_steps" \
                    "$dio_or_bio" "$use_random" "$force_single" \
                    "$sweep_write_only" "$sweep_write_no_read" "$sweep_read_from"; then
                echo "Error: failed to reify execution ${nnnn}" >&2
                return 1
            fi
            _atomic_write_sentinel "${executions_dir}/${nnnn}.status" PENDING || return 1
            _seq_ref=$((_seq_ref + 1))
        done
    done
    return 0
}

# Reify every (nodes, io_size, thread_count, io_depth) cell as executions/NNNN.sh
# plus NNNN.status=PENDING. Writes 1-indexed, zero-padded NNNN files.
# Usage: reify_all_elbencho_executions <output_dir> <nodes_spec> \
#            <dio_or_bio> <use_random> <force_single> \
#            <sweep_write_only> <sweep_write_no_read> <sweep_read_from>
reify_all_elbencho_executions() {
    local output_dir="$1"
    local nodes_spec="$2"
    local dio_or_bio="$3"
    local use_random="$4"
    local force_single="$5"
    local sweep_write_only="$6"
    local sweep_write_no_read="$7"
    local sweep_read_from="$8"

    local -a node_counts
    if ! mapfile -t node_counts < <(parse_range_specification "$nodes_spec"); then
        echo "Error: Invalid node specification: $nodes_spec" >&2
        return 1
    fi
    if [[ ${#node_counts[@]} -eq 0 ]]; then
        echo "Error: Node specification produced no node counts" >&2
        return 1
    fi

    local executions_dir="${output_dir}/executions"
    mkdir -p "$executions_dir" || return 1

    local seq=1
    local nodes
    local io_size
    for nodes in "${node_counts[@]}"; do
        for io_size in "${ELBENCHO_SCALE_IO_SIZES[@]}"; do
            if ! _reify_inner_threads_iodepth \
                    "$executions_dir" "$nodes" "$io_size" \
                    "$dio_or_bio" "$use_random" "$force_single" \
                    "$sweep_write_only" "$sweep_write_no_read" "$sweep_read_from" \
                    seq; then
                return 1
            fi
        done
    done
    echo "Reified $((seq - 1)) executions in ${executions_dir}"
    return 0
}

# Enumerate execution IDs in NNNN order. Prints one ID per line on stdout.
# Usage: list_elbencho_execution_ids <executions_dir>
list_elbencho_execution_ids() {
    local executions_dir="$1"
    local f
    if [[ ! -d "$executions_dir" ]]; then
        return 0
    fi
    for f in "$executions_dir"/*.sh; do
        [[ -f "$f" ]] || continue
        local base
        base=$(basename "$f" .sh)
        # Filter for NNNN.sh shape only (numeric)
        if [[ "$base" =~ ^[0-9]+$ ]]; then
            printf '%s\n' "$base"
        fi
    done | sort -n
    return 0
}

# Count executions whose status is NOT SUCCESS (what dispatch/--resume will run).
# Prints an integer to stdout (0 if none).
# Usage: count_elbencho_remaining_executions <executions_dir>
count_elbencho_remaining_executions() {
    local executions_dir="$1"
    local id
    local count=0
    local status_file
    while IFS= read -r id; do
        status_file="${executions_dir}/${id}.status"
        [[ -f "$status_file" ]] || continue
        if [[ "$(cat "$status_file" 2>/dev/null)" == "SUCCESS" ]]; then
            continue
        fi
        count=$((count + 1))
    done < <(list_elbencho_execution_ids "$executions_dir")
    printf '%s\n' "$count"
    return 0
}

# Compute max nodes among executions whose status is NOT SUCCESS.
# Prints integer to stdout. Returns 1 (with empty stdout) if no executions.
# Usage: max_nodes_remaining_executions <executions_dir>
max_nodes_remaining_executions() {
    local executions_dir="$1"
    local id
    local max_nodes=0
    local status_file
    while IFS= read -r id; do
        status_file="${executions_dir}/${id}.status"
        [[ -f "$status_file" ]] || continue
        if [[ "$(cat "$status_file" 2>/dev/null)" == "SUCCESS" ]]; then
            continue
        fi
        # Source NNNN.sh in a subshell to extract $nodes without polluting caller.
        local nodes_value
        nodes_value=$( \
            # shellcheck disable=SC1090,SC1091
            source "${executions_dir}/${id}.sh" >/dev/null 2>&1 && \
            printf '%s' "${nodes:-}" \
        )
        if [[ "$nodes_value" =~ ^[0-9]+$ ]] && [[ "$nodes_value" -gt "$max_nodes" ]]; then
            max_nodes="$nodes_value"
        fi
    done < <(list_elbencho_execution_ids "$executions_dir")
    if [[ "$max_nodes" -eq 0 ]]; then
        return 1
    fi
    printf '%s\n' "$max_nodes"
    return 0
}

# =============================================================================
# Metadata benchmark functions
# =============================================================================

# Rotate a comma-separated list by N positions
# Usage: rotated=$(rotate_csv_list "$csv_list" $positions)
# Example: rotate_csv_list "a,b,c" 1 → "b,c,a"
#
# Edge cases:
#   - Empty/0 hosts: returns empty string
#   - 1 host: returns original (no rotation possible)
#   - N hosts, rotate by N: returns original (N % N = 0)
rotate_csv_list() {
    local csv="$1"
    local positions="$2"

    # Handle empty input
    if [[ -z "$csv" ]]; then
        echo ""
        return
    fi

    local arr
    IFS=',' read -ra arr <<< "$csv"
    local len=${#arr[@]}

    # 1 host: no rotation possible
    if [[ $len -le 1 ]]; then
        echo "$csv"
        return
    fi

    # Normalize positions to be within array bounds
    positions=$((positions % len))

    # After normalization, 0 means no rotation needed
    if [[ $positions -eq 0 ]]; then
        echo "$csv"
        return
    fi

    # Build rotated array
    local rotated=()
    local i
    for ((i = 0; i < len; i++)); do
        rotated+=("${arr[((i + positions) % len)]}")
    done

    local IFS=','
    echo "${rotated[*]}"
}

# Export the mdtest-elbencho directory-layout settings consumed by
# run_elbencho_metadata_benchmark. Every execution substrate (Slurm and SSH)
# calls this, so all of them pass identical layout parameters.
#
# Usage: _mdtest_export_layout_env <target_files> <files_per_worker>
# Empty arguments select the standard branched layout.
_mdtest_export_layout_env() {
    local target_files="${1:-}"
    local files_per_worker="${2:-}"

    if [[ -n "$target_files" ]]; then
        MDTEST_LAYOUT="single-dir"
    else
        MDTEST_LAYOUT="standard"
    fi
    MDTEST_SINGLE_DIR_TARGET_FILES="$target_files"
    MDTEST_SINGLE_DIR_FILES_PER_WORKER="$files_per_worker"

    export MDTEST_LAYOUT MDTEST_SINGLE_DIR_TARGET_FILES
    export MDTEST_SINGLE_DIR_FILES_PER_WORKER
    return 0
}

# Prepare the standard branched directory layout for the metadata benchmark.
#
# Pre-creates MDTEST_BRANCH_FACTOR subdirs under each base test dir; these
# become the paths passed to elbencho. Named "b0", "b1", etc. (base dirs) to
# avoid confusion with elbencho's "r0/d0" naming.
#
# Reads: base_test_dirs, MDTEST_BRANCH_FACTOR, MDTEST_ITEMS_PER_DIR
# Sets: elbencho_paths, dirs_per_thread, files_per_worker, create_extra_args,
#       delete_extra_args, cleanup_mindepth
_mdtest_prepare_standard_layout() {
    local base_dir
    local i

    elbencho_paths=()
    for base_dir in "${base_test_dirs[@]}"; do
        for ((i = 0; i < MDTEST_BRANCH_FACTOR; i++)); do
            elbencho_paths+=("${base_dir}/b${i}")
        done
    done

    mkdir -p "${elbencho_paths[@]}" || {
        echo "Error: Unable to create test directories: ${elbencho_paths[*]}" >&2
        return 1
    }

    # Dirs per thread is MDTEST_BRANCH_FACTOR squared; the pre-created paths are
    # for load distribution and don't multiply the count.
    dirs_per_thread=$((MDTEST_BRANCH_FACTOR * MDTEST_BRANCH_FACTOR))
    files_per_worker="$MDTEST_ITEMS_PER_DIR"
    create_extra_args=(-d)
    delete_extra_args=(-D)
    # Structure is base_dir/b{i}/r{rank}/d{dir}: deepest branching at level 2.
    cleanup_mindepth=2
    return 0
}

# Prepare the dense (single flat directory) layout for the metadata benchmark.
#
# Passes the generated target directory itself to elbencho with "-n 0", so all
# workers create uniquely named files ("r<rank>-f<n>") directly in it with no
# per-rank/per-dir subdirectories. Directory create/delete phases are therefore
# omitted by leaving the extra-args arrays empty.
#
# Reads: base_test_dirs, MDTEST_SINGLE_DIR_FILES_PER_WORKER
# Sets: elbencho_paths, dirs_per_thread, files_per_worker, create_extra_args,
#       delete_extra_args, cleanup_mindepth
_mdtest_prepare_dense_layout() {
    if [[ ${#base_test_dirs[@]} -ne 1 ]]; then
        echo "Error: Dense (single flat directory) layout requires exactly one" >&2
        echo "       test directory, got ${#base_test_dirs[@]}: ${base_test_dirs[*]}" >&2
        return 1
    fi
    if ! [[ "${MDTEST_SINGLE_DIR_FILES_PER_WORKER:-}" =~ ^[1-9][0-9]*$ ]]; then
        echo "Error: MDTEST_SINGLE_DIR_FILES_PER_WORKER must be a positive" >&2
        echo "       integer, got '${MDTEST_SINGLE_DIR_FILES_PER_WORKER:-}'" >&2
        return 1
    fi

    elbencho_paths=("${base_test_dirs[0]}")

    mkdir -p "${elbencho_paths[@]}" || {
        echo "Error: Unable to create test directory: ${elbencho_paths[*]}" >&2
        return 1
    }

    dirs_per_thread=0
    files_per_worker="$MDTEST_SINGLE_DIR_FILES_PER_WORKER"
    create_extra_args=()
    delete_extra_args=()
    # Files live directly in the target dir: deepest branching at level 1.
    cleanup_mindepth=1
    return 0
}

# Print the metadata benchmark parameters for the selected directory layout.
# Reads the variables set by _elbencho_resolve_run_context and the layout
# preparation helpers.
_mdtest_print_run_summary() {
    local ds="$1"

    echo "Running metadata benchmark on ${node_count} nodes with ${tasks_per_node} tasks/node"
    echo "  Nodelist:             ${hosts_csv}"
    echo "  Test Dirs:            ${test_dirs_csv}"
    echo "  Tasks per Node:       ${tasks_per_node}"

    if [[ "${MDTEST_LAYOUT:-standard}" == "single-dir" ]]; then
        local actual_files=$((node_count * tasks_per_node * files_per_worker))
        echo "  Directory Layout:     dense (single flat directory, -n 0)"
        echo "  Dirs per Thread (-n): 0 (no subdirectories)"
        echo "  Files per Worker (-N): ${files_per_worker}"
        printf "  Target Files:         %d\n" "${MDTEST_SINGLE_DIR_TARGET_FILES:-0}"
        printf "  Actual Files:         %d\n" "$actual_files"
    else
        local total_dirs=$((node_count * tasks_per_node * dirs_per_thread))
        local total_files=$((total_dirs * files_per_worker))
        echo "  Directory Layout:     standard (branched)"
        echo "  MDTEST_BRANCH_FACTOR: ${MDTEST_BRANCH_FACTOR}"
        echo "  Dirs per Thread (-n): ${dirs_per_thread} (= ${MDTEST_BRANCH_FACTOR}^2)"
        echo "  Files per Dir per Thread (-N): ${files_per_worker}"
        printf "  Total Dirs:           %d\n" "$total_dirs"
        printf "  Total Files:          %d\n" "$total_files"
    fi

    echo "  Iterations:           ${MDTEST_ITERATIONS}"
    if [ -n "${SLURM_JOB_ID:-}" ]; then
        echo "  JobID:                ${SLURM_JOB_ID}"
    fi
    echo "  Datestamp:            ${ds}"
    echo "  Output Dir:           ${remote_output_dir}"
    echo "  Elbencho CLI Paths:   ${#elbencho_paths[@]}"
    echo
    return 0
}

# Run elbencho metadata benchmark (create dirs, create files, stat, delete files, delete dirs)
#
# The standard layout branches files across a b{i}/r{rank}/d{dir} tree. When
# MDTEST_LAYOUT is "single-dir", all workers instead operate on one shared flat
# directory and the directory create/delete phases are skipped.
#
# See header comments for required environment variables
run_elbencho_metadata_benchmark() {
    local node_count
    local remote_output_dir
    local hosts_csv

    if ! _elbencho_resolve_run_context; then
        return 1
    fi

    # Convert base test dirs CSV to array
    local base_test_dirs
    IFS=',' read -ra base_test_dirs <<< "$test_dirs_csv"

    # Set by the layout preparation helpers below
    local elbencho_paths=()
    local dirs_per_thread
    local files_per_worker
    local create_extra_args=()
    local delete_extra_args=()
    local cleanup_mindepth

    if [[ "${MDTEST_LAYOUT:-standard}" == "single-dir" ]]; then
        if ! _mdtest_prepare_dense_layout; then
            return 1
        fi
    elif ! _mdtest_prepare_standard_layout; then
        return 1
    fi

    # Extract datestamp from output_dir
    local ds="${output_dir##*-}"

    _mdtest_print_run_summary "$ds"

    # Base output file names (iteration number will be inserted)
    local base_resfile
    base_resfile=$(printf "%s/mdtest-elbencho-c_%03d-t_%03d_%s" \
        "$remote_output_dir" "$node_count" "$tasks_per_node" "$ds")
    local base_csvfile="$base_resfile"

    echo "Output files: ${base_resfile}_iter*.out / .csv"
    echo

    # Common elbencho arguments for metadata operations (without output files)
    # In the dense layout dirs_per_thread is 0, which makes elbencho create the
    # files directly in the given path, and files_per_worker is the per-worker
    # share of the requested file target.
    local common_args=(
        -t "$tasks_per_node"
        -n "$dirs_per_thread"
        -N "$files_per_worker"
        -s 0                        # Zero-byte files (metadata only)
        -b 0                        # Block size 0 (matches file size)
        --lat
        --lathisto
        --latpercent
        --nolive
    )

    # External iteration loop - gives fresh state each iteration and per-iteration output
    local iter
    local overall_rc=0
    for ((iter = 1; iter <= MDTEST_ITERATIONS; iter++)); do
        echo "=== Iteration $iter of $MDTEST_ITERATIONS ==="

        # Per-iteration output files
        local resfile="${base_resfile}_iter${iter}.out"
        local csvfile="${base_csvfile}_iter${iter}.csv"
        local iter_output_args=(
            --resfile="$resfile"
            --csvfile="$csvfile"
        )

        # Phase 1: Create directories and files
        # Use original hosts order with --rotatehosts=1 to avoid artificially fast
        # first-file-in-dir creation when the dir was also created by same host.
        # --rotatehosts also rotates once between CREATEDIRS and CREATEFILES in-process;
        # stat is a separate run, rank↔host for stat ≠ CREATEFILES after in-run rotate,
        # therefore, no cache reuse.
        if [[ "${MDTEST_LAYOUT:-standard}" == "single-dir" ]]; then
            echo "  Phase 1: Creating files..."
        else
            echo "  Phase 1: Creating directories and files..."
        fi
        local create_args=()
        if [ "$node_count" -gt 1 ]; then
            create_args+=(--hosts "$hosts_csv" --rotatehosts 1)
        fi
        create_args+=(
            -w                          # Write/create files
            ${create_extra_args[@]+"${create_extra_args[@]}"}  # -d unless dense
            "${common_args[@]}"
            "${iter_output_args[@]}"
            "${elbencho_paths[@]}"
        )
        local create_rc=0
        run_an_elbencho "${create_args[@]}" || create_rc=$?
        if [[ "$create_rc" -ne 0 ]]; then
            echo "Error: elbencho metadata CREATE failed (rc=$create_rc) on iteration $iter" >&2
            overall_rc=$create_rc
            break
        fi

        _elbencho_maybe_pause_before_read stat "  "

        local rotated_hosts=""
        if [ "$node_count" -gt 1 ]; then
            rotated_hosts=$(rotate_csv_list "$hosts_csv" 1)
        fi

        # Phase 2: Stat files
        # Rotate hosts by 1 so each node stats files created by a different node
        echo "  Phase 2: Stat files..."
        local stat_args=()
        if [ "$node_count" -gt 1 ]; then
            stat_args+=(--hosts "$rotated_hosts")
        fi
        stat_args+=(
            --stat                      # Stat files
            "${common_args[@]}"
            --nocsvlabels               # Don't repeat CSV header
            "${iter_output_args[@]}"
            "${elbencho_paths[@]}"
        )
        local stat_rc=0
        run_an_elbencho "${stat_args[@]}" || stat_rc=$?
        if [[ "$stat_rc" -ne 0 ]]; then
            echo "Error: elbencho metadata STAT failed (rc=$stat_rc) on iteration $iter" >&2
            overall_rc=$stat_rc
            break
        fi

        # Phase 3: Delete files and directories
        # Rotate hosts by 1 so each node deletes files statted by different node
        # (For 2 hosts: A deletes what B statted, B deletes what A statted)
        if [[ "${MDTEST_LAYOUT:-standard}" == "single-dir" ]]; then
            echo "  Phase 3: Deleting files..."
        else
            echo "  Phase 3: Deleting files and directories..."
        fi
        local delete_args=()
        if [ "$node_count" -gt 1 ]; then
            delete_args+=(--hosts "$rotated_hosts")
        fi
        delete_args+=(
            -F                          # Delete files
            ${delete_extra_args[@]+"${delete_extra_args[@]}"}  # -D unless dense
            "${common_args[@]}"
            --nocsvlabels               # Don't repeat CSV header
            "${iter_output_args[@]}"
            "${elbencho_paths[@]}"
        )
        local delete_rc=0
        run_an_elbencho "${delete_args[@]}" || delete_rc=$?
        if [[ "$delete_rc" -ne 0 ]]; then
            echo "Error: elbencho metadata DELETE failed (rc=$delete_rc) on iteration $iter" >&2
            overall_rc=$delete_rc
            break
        fi

        echo "  Iteration $iter complete."
        echo
    done

    if [[ "$overall_rc" -eq 0 ]]; then
        echo "Metadata benchmark complete ($MDTEST_ITERATIONS iterations)."
    else
        echo "Metadata benchmark aborted with rc=$overall_rc; cleaning up." >&2
    fi

    # Cleanup: Remove the generated test directories and their contents in
    # parallel. cleanup_mindepth is where the tree branches widest for the
    # selected layout (2 for base_dir/b{i}/r{rank}, 1 for the flat dense dir).
    # Only the generated target dirs are removed; the configured TEST_DIRS roots
    # are their parents and are never touched.
    echo "Deleting test dirs ${base_test_dirs[*]}"
    time find "${base_test_dirs[@]}" -mindepth "$cleanup_mindepth" \
        -maxdepth "$cleanup_mindepth" -print0 | xargs -0 -P 32 -n 1 rm -rf
    rm -rf "${base_test_dirs[@]}"

    return "$overall_rc"
}
