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
# This library provides warp benchmark functions intended to be sourced in
# two environments:
# 1. SLURM
#   - script run by sbatch on one client node
#   - would have access to env.sh, lib/env_functions.sh, etc.
# 2. SSH
#   - scriptlet run by ssh on one client node
#   - does NOT have access to env.sh, lib/env_functions.sh, etc.
#
# Because of the ssh case constraints, this script cannot itself source
# env.sh, lib/env_functions.sh, etc. itself.  Instead, the sites where
# this script is sourced will need to source the appropriate files, use
# the functions from those files, and then call run_warp_io_sweep_iteration().
#
# The required environment variables are:
# output_dir
# obj_size
# put_duration
# put_min_objs
# max_get_time
# multipart
# ranged
# s3_express          (optional; "true" enables S3 Express One Zone mode)
# thread_list  (bash array)
# WARP_RPS_BUDGET_GET (optional)
# WARP_RPS_BUDGET_PUT (optional)
# WARP        # path to warp binary (on client nodes)
# S3TEST      # path to s3test binary (on client nodes)
# WARP_RANGE_OBJ_SIZE (when ranged is true)
# WARP_PREFIXES (optional)
# WARP_ACCESS_KEY
# WARP_SECRET_KEY
# OBJ_BUCKET
# OBJ_REGION
# OBJ_HOST
# OBJ_HOST_PORT

# SSH case only env vars:
# SSH_NODELIST    # comma-separated list of client nodes

# Slurm case only env vars:
# SLURM_JOB_NODELIST  # comma-separated list of client nodes (may use slurm compact representation!)
# SLURM_JOB_NUM_NODES # integer number of nodes in the reservation (don't try to parse SLURM_JOB_NODELIST)
# nodelist_expanded_comma_separated # comma-separated list of client nodes (expanded from SLURM_JOB_NODELIST)

# Conditional derived env vars:
# node_count        # number of client nodes (from SLURM_JOB_NUM_NODES or SSH_NODELIST)
# remote_output_dir # basename for ssh, full path for slurm

run_a_warp() {
    printf "# warp %s\n" "$*"
    # Force flush stdout by redirecting file descriptor 1 to itself
    # This works because redirecting causes the shell to flush the buffer
    exec 1>&1
    if [ -n "$SLURM_JOB_NODELIST" ]; then
        if [ -n "$SLURM_JOB_NUM_NODES" ] && [[ "$SLURM_JOB_NUM_NODES" -gt 1 ]]; then
            "$WARP" "$@" --warp-client="$nodelist_expanded_comma_separated"
        else
            "$WARP" "$@"
        fi
    else
        "$WARP" "$@" --warp-client="$SSH_NODELIST"
    fi
}

# Function to parse golang time durations (e.g., "5s", "10m", "2h")
# Returns the number of seconds
parse_golang_duration() {
    local duration="$1"
    local num="${duration%[smh]}"
    local unit="${duration: -1}"

    case "$unit" in
        s) echo "$num" ;;
        m) echo "$((num * 60))" ;;
        h) echo "$((num * 3600))" ;;
        *) echo "Error: Unsupported time unit '$unit'. Only s, m, h are supported" >&2; return 1 ;;
    esac
}

# Calculate analyze duration from a duration string
# Args:
#   $1: duration string in golang format
# Returns:
#   analyze duration in seconds
calculate_analyze_duration() {
    local time_seconds
    time_seconds=$(parse_golang_duration "$1")
    local analyze_duration=$(( (time_seconds + 6) / 12 ))  # Divide by 12 and round
    if [ "$analyze_duration" -lt 1 ]; then
        analyze_duration=1  # Ensure it's at least 1 second
    fi
    echo "$analyze_duration"
}

# Build conditional warp connection args for S3 Express support.
# Sets warp_region_args (--region when not S3 Express) and
# warp_s3express_args (--signature=IAM when S3 Express).
_build_warp_conn_args() {
    warp_region_args=()
    warp_s3express_args=()
    if [[ "${s3_express:-false}" == "true" ]]; then
        warp_s3express_args=("--signature=IAM")
    else
        warp_region_args=("--region=${OBJ_REGION}")
    fi
    return 0
}

function ensure_bucket_empty() {
    local thread_count="${1:-64}"
    local node_count="${2:-1}"

    _build_warp_conn_args

    local temp_dir
    temp_dir=$(mktemp -d)

    extra_del_args=()

    # Add RPS GET limit if budget is specified
    if [[ -n "${WARP_RPS_BUDGET_GET:-}" ]]; then
        # Calculate per-node, per-thread RPS GET limit
        # Round to nearest integer
        local rps_limit_get=$(( (WARP_RPS_BUDGET_GET + (node_count / 2)) / node_count ))
        extra_del_args+=("--rps-limit=$rps_limit_get")
    fi

    warp_cleanup_args=(
        delete
        --tls
        --bucket="${OBJ_BUCKET}"
        "${warp_region_args[@]}"
        --host="${OBJ_HOST}:${OBJ_HOST_PORT}"
        "${warp_s3express_args[@]}"
        --concurrent="$thread_count"
        --duration=30m
        --list-existing
        --objects=0
        --noclear
        "${extra_del_args[@]}"
        --stress
        --benchdata="$temp_dir/foo"
    )
    run_a_warp "${warp_cleanup_args[@]}" ||:

    rm -rf "$temp_dir" ||:
}

# Run a single iteration of the warp IO sweep benchmark.
# This function performs PUT, GET (across thread counts), and DELETE operations.
# Requires all environment variables documented at the top of this file.
run_warp_io_sweep_iteration() {
    _build_warp_conn_args

    # Derive node_count and remote_output_dir from SLURM_JOB_NUM_NODES or SSH_NODELIST
    if [ -n "${SLURM_JOB_NUM_NODES:-}" ]; then
        node_count="$SLURM_JOB_NUM_NODES"
        remote_output_dir="$output_dir"
    else
        # Count the number of nodes in SSH_NODELIST (comma-separated)
        IFS=',' read -ra _ssh_nodes <<< "$SSH_NODELIST"
        node_count="${#_ssh_nodes[@]}"
        unset _ssh_nodes

        # Since we're executing remotely, we need a path to store the result files.
        # We assume the current directory is writable and create a fresh subdirectory
        # for this run.
        remote_output_dir=$(cd "$(pwd)" && pwd)/"$(basename "$output_dir")" || { \
            echo "Error: Unable to get absolute path" >&2; exit 1; }
    fi
    mkdir -p "$remote_output_dir" || { echo "Error: Unable to create directory" >&2; exit 1; }

    # Initialize RPS budgets from environment variables with empty string defaults
    rps_budget_get="${WARP_RPS_BUDGET_GET:-}"
    rps_budget_put="${WARP_RPS_BUDGET_PUT:-}"
    datestamp="${output_dir##*-}"

    # Calculate analyze durations for this test
    analyze_duration_get=$(calculate_analyze_duration "$max_get_time")
    analyze_duration_put=$(calculate_analyze_duration "$put_duration")

    extra_get_args=()
    if [[ "$ranged" != "false" ]]; then
        # Ranged read test; override PUT size, duration, and count
        put_min_objs="$node_count"
        put_duration=30s
        actual_put_size=$WARP_RANGE_OBJ_SIZE
        # --range-size Use a fixed range size while doing random range
        #              offsets, --range is implied
        extra_get_args=(
            --range-size="$obj_size"
        )
    else
        # Convert what is a minimum object count per node to a total object count
        put_min_objs=$((put_min_objs * node_count))
        actual_put_size=$obj_size
    fi

    extra_put_args=()
    if [ -n "$WARP_PREFIXES" ]; then
        extra_put_args+=("--prefixes=$WARP_PREFIXES" "--noprefix")
    fi

    # Add RPS PUT limit if budget is specified
    if [[ -n "$rps_budget_put" ]]; then
        # Calculate per-node, per-thread RPS PUT limit
        # Round to nearest integer
        # Warp will apportion the limit across all threads on a node
        rps_limit_put=$(( (rps_budget_put + (node_count / 2)) / node_count ))
        extra_put_args+=("--rps-limit=$rps_limit_put")
    fi

    echo "Ensuring bucket is empty as we start..."
    ensure_bucket_empty "${thread_list[-1]}" "$node_count"

    put_objs=0

    # Just do the PUTs once; but we loop here until we've successfully
    # PUT at least $put_min_objs objects.
    while [[ "$put_objs" -lt "$put_min_objs" ]]; do
        echo "Doing single PUT stage."
        echo "PUT RPS limit: $rps_limit_put per thread (from budget: $rps_budget_put)"
        echo "  ($actual_put_size objects for $put_duration; " \
            "target > $put_min_objs, $put_objs done so far)"
        put_benchdata=$(printf "%s/warp-PUT-%s-%s-c_%03d-s_%03d_%s" \
            "$remote_output_dir" "$obj_size" "$put_objs" \
            "$node_count" "${thread_list[-1]}" "$datestamp")
        warp_put_args=(
            put
            --tls
            --bucket="${OBJ_BUCKET}"
            "${warp_region_args[@]}"
            --host="${OBJ_HOST}:${OBJ_HOST_PORT}"
            "${warp_s3express_args[@]}"
            --concurrent="${thread_list[-1]}"
            --noclear
            --duration="$put_duration"
            --obj.size="$actual_put_size"
            "${extra_put_args[@]}"
            --analyze.v
            --analyze.op=PUT
            --analyze.dur="${analyze_duration_put}s"
            --analyze.out="$(printf "%s/warp-PUT-%s-%s-c_%03d-s_%03d_%s.tsv" \
                "$remote_output_dir" "$obj_size" "$put_objs" \
                "$node_count" "${thread_list[-1]}" "$datestamp")"
            --benchdata="$put_benchdata"
        )
        if [ "$multipart" != "true" ]; then
            warp_put_args+=("--disable-multipart")
        fi

        run_a_warp "${warp_put_args[@]}"

        # Count PUT objects by running S3TEST up to 3 times with 2s delay; on
        # persistent failure, print warning and exit the while loop.
        attempt=1
        max_attempts=3
        success=false
        while [ "$attempt" -le "$max_attempts" ]; do
            if put_objs=$("$S3TEST"); then
                success=true
                break
            else
                echo "Warning: S3TEST attempt $attempt failed." >&2
                if [ "$attempt" -lt "$max_attempts" ]; then
                    sleep 2
                fi
            fi
            attempt=$((attempt + 1))
        done
        if [ "$success" != "true" ]; then
            echo "Warning: Failed to run s3test after $max_attempts attempts, aborting PUT loop..." >&2
            break  # Exit the enclosing while loop, do not exit script
        fi
    done

    if [ "$put_objs" -gt "$put_min_objs" ]; then
        echo "PUT $put_objs total objects... (> target $put_min_objs)"
    else
        echo "PUT $put_objs total objects... (below target $put_min_objs, possibly due to s3test count failure)"
    fi

    # Save off pre-loop stdout/err
    exec 3>&1 4>&2

    for numprocs in "${thread_list[@]}"; do
        LOOP_LOGFILE=$(printf "%s/warp-GET-%s-c_%03d-s_%03d_%s.out" \
            "$remote_output_dir" "$obj_size" "$node_count" "$numprocs" "$datestamp")
        LOOP_BENCHDATA=$(printf "%s/warp-GET-%s-c_%03d-s_%03d_%s" \
            "$remote_output_dir" "$obj_size" "$node_count" "$numprocs" "$datestamp")
        # This --analyze.out file has summarization per time interval (defaults
        # upstream to 1s); useful for plotting metrics per interval during the
        # run.  You can also recreate this file with a different time interval
        # bin value using the "LOOP_BENCHDATA" artifact, above.
        LOOP_TSV_FILE="$(printf "%s/warp-GET-%s-c_%03d-s_%03d_%s.tsv" \
            "$remote_output_dir" "$obj_size" "$node_count" "$numprocs" "$datestamp")"

        echo "Using --concurrent=${numprocs}"
        echo "Logging this loop to $LOOP_LOGFILE"

        warp_get_args=(
            get
            --tls
            --bucket="${OBJ_BUCKET}"
            "${warp_region_args[@]}"
            --host="${OBJ_HOST}:${OBJ_HOST_PORT}"
            "${warp_s3express_args[@]}"
            --concurrent="$numprocs"
            --list-existing
            --objects=0
            --noclear
            --duration="$max_get_time"
            "${extra_get_args[@]}"
            --analyze.v
            --analyze.op=GET
            --analyze.dur="${analyze_duration_get}s"
            --analyze.out="$LOOP_TSV_FILE"
            --benchdata="$LOOP_BENCHDATA"
        )

        # Add RPS GET limit if budget is specified
        if [[ -n "$rps_budget_get" ]]; then
            # Calculate per-node, per-thread RPS limit
            # Round to nearest integer
            # Warp will apportion the limit across all threads on a node
            rps_limit_get=$(( (rps_budget_get + (node_count / 2)) / node_count ))
            warp_get_args+=("--rps-limit=$rps_limit_get")
        fi

        if [ "$multipart" != "true" ]; then
            warp_get_args+=("--disable-multipart")
        fi

        # tee loop body stdout/stderr to log
        exec 1> >(tee -a "$LOOP_LOGFILE") 2> >(tee -a "$LOOP_LOGFILE" >&2)

        echo "Threads/node: $numprocs"
        if [ "$ranged" != "false" ]; then
            echo "Object Size : $actual_put_size"
            echo "Range Size  : $obj_size"
        else
            echo "Object Size : $obj_size"
        fi
        echo "GET RPS limit: $rps_limit_get per thread (from budget: $rps_budget_get)"
        echo "Analyze Duration: ${analyze_duration_get}s"
        echo

        run_a_warp "${warp_get_args[@]}"

        # Restore original stdout/stderr for anything after the loop
        exec 1>&3 2>&4
    done

    # Finally, clean up after ourselves (and, I guess, benchmark that too!)
    extra_del_args=()
    # Use RPS GET limit for DELETEs, if present
    if [[ -n "$rps_budget_get" ]]; then
        # Calculate per-node, per-thread RPS limit
        # Round to nearest integer
        # Warp will apportion the limit across all threads on a node
        rps_limit_get=$(( (rps_budget_get + (node_count / 2)) / node_count ))
        extra_del_args+=("--rps-limit=$rps_limit_get")
    fi

    warp_delete_args=(
        delete
        --tls
        --bucket="${OBJ_BUCKET}"
        "${warp_region_args[@]}"
        --host="${OBJ_HOST}:${OBJ_HOST_PORT}"
        "${warp_s3express_args[@]}"
        --concurrent="${thread_list[-1]}"
        --list-existing
        --objects=0
        --noclear
        "${extra_del_args[@]}"
        --analyze.v
        --analyze.op=DELETE
        --analyze.out="$(printf "%s/warp-DELETE-%s-c_%03d-s_%03d_%s.tsv" \
            "$remote_output_dir" "$obj_size" "$node_count" "${thread_list[-1]}" "$datestamp")"
        --benchdata="$(printf "%s/warp-DELETE-%s-c_%03d-s_%03d_%s" \
            "$remote_output_dir" "$obj_size" "$node_count" "${thread_list[-1]}" "$datestamp")"
    )
    run_a_warp "${warp_delete_args[@]}"
}
