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
# This library provides netbench-related functions for network benchmarking.
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
# Available functions:
#   run_netbench_half_half   - Half clients, half servers (unidirectional or bidirectional)
#
# =============================================================================
# Required environment variables (all modes):
# =============================================================================
# ELBENCHO              # Path to elbencho binary (on client nodes)
# NETBENCH_PORT         # Service port (default 12865)
# NETBENCH_BLOCKSIZE    # Block size for transfers (e.g., "1M")
# NETBENCH_RESPSIZE     # Response size (e.g., "4K")
# NETBENCH_HOST_NIC_GBPS   # Host NIC speed in Gbps (e.g., 100)
# NETBENCH_TARGET_RUNTIME  # Target runtime in seconds (e.g., 20)
# output_dir            # Full path to output directory
# thread_count          # Current thread count for this invocation
# iteration             # Current iteration number (1-based)
# bidirectional         # "true" or "false"
#
# SSH case only env vars:
# SSH_NODELIST          # Comma-separated list of client nodes
#
# SLURM case only env vars:
# SLURM_JOB_NODELIST               # Compact representation of client nodes
# SLURM_JOB_NUM_NODES              # Integer number of nodes
# nodelist_expanded_comma_separated # Comma-separated list (expanded)


# Shuffle an array in-place using Fisher-Yates algorithm
# Usage: shuffle_array arr_name
shuffle_array() {
    local -n arr=$1
    local i j temp
    for ((i = ${#arr[@]} - 1; i > 0; i--)); do
        j=$((RANDOM % (i + 1)))
        temp="${arr[i]}"
        arr[i]="${arr[j]}"
        arr[j]="$temp"
    done
}

# Get the hosts argument based on execution context (SLURM vs SSH)
# Also sets node_count variable
# Usage: get_netbench_hosts_and_count
#        After call: hosts_arg and node_count are set
get_netbench_hosts_and_count() {
    if [ -n "${SLURM_JOB_NUM_NODES:-}" ]; then
        node_count="$SLURM_JOB_NUM_NODES"
        hosts_arg="$nodelist_expanded_comma_separated"
    else
        # Count the number of nodes in SSH_NODELIST (comma-separated)
        local _ssh_nodes
        IFS=',' read -ra _ssh_nodes <<< "$SSH_NODELIST"
        node_count="${#_ssh_nodes[@]}"
        hosts_arg="$SSH_NODELIST"
    fi
}

# Run a netbench elbencho command with logging
# Usage: run_a_netbench elbencho_args...
run_a_netbench() {
    printf "# elbencho %s\n" "$*"
    # Force flush stdout
    exec 1>&1
    # --svcwait is a ceiling, not a fixed delay; elbencho proceeds as soon as services respond.
    # 120s accommodates large clusters (80+ nodes) where some services start slowly.
    "$ELBENCHO" --svcwait 120 "$@"
}

# Build a host list with explicit ports
# Usage: build_host_list_with_port hosts_csv port
#        Outputs: comma-separated list with :port appended to each host
build_host_list_with_port() {
    local hosts_csv="$1"
    local port="$2"
    local -a hosts
    local -a result
    IFS=',' read -ra hosts <<< "$hosts_csv"
    for host in "${hosts[@]}"; do
        result+=("${host}:${port}")
    done
    local IFS=','
    echo "${result[*]}"
}

# Calculate per-thread transfer size to achieve target runtime
# Formula:
#   per_host_bytes = target_runtime × nic_gbps × 125,000,000
#                  = target_runtime × nic_gbps × (10^9 / 8)
#   per_thread_bytes = per_host_bytes / thread_count
#   per_thread_mib = floor(per_thread_bytes / 1,048,576)
#
# As thread_count increases, per_thread_size decreases, keeping total per-host
# transfer constant and thus runtime approximately constant.
#
# Usage: calculate_per_thread_size nic_gbps target_runtime thread_count
#        Outputs: size string for elbencho --size (e.g., "7450M")
calculate_per_thread_size() {
    local nic_gbps="$1"
    local target_runtime="$2"
    local thread_count="$3"

    # Calculate per-host bytes: target_runtime × nic_gbps × 125,000,000
    # 125,000,000 = 10^9 / 8 (converts Gbps to bytes/sec)
    # Then divide by thread_count and by 1,048,576 to get MiB
    #
    # To avoid overflow in bash arithmetic (which uses signed 64-bit),
    # we restructure: (runtime × nic × 125000000) / threads / 1048576
    # For 100 Gbps, 20 sec: 20 × 100 × 125000000 = 250,000,000,000 (fits in 64-bit)
    local per_thread_mib
    per_thread_mib=$(( (target_runtime * nic_gbps * 125000000) / thread_count / 1048576 ))

    # Validate we got a reasonable value (at least 1 MiB)
    if [[ "$per_thread_mib" -lt 1 ]]; then
        echo "Error: Calculated per-thread size is less than 1 MiB." >&2
        echo "  NIC: ${nic_gbps} Gbps, Runtime: ${target_runtime}s, Threads: ${thread_count}" >&2
        echo "  Try increasing TARGET_RUNTIME or decreasing thread count." >&2
        return 1
    fi

    echo "${per_thread_mib}M"
}

# Check TCP connectivity to host:port pairs in parallel from the current node.
# Launches all checks as background processes for fast results at scale.
# Outputs unreachable host:port pairs (one per line).
# Usage: _check_tcp_connectivity host_port_csv [host_port_csv2 ...]
_check_tcp_connectivity() {
    local -a pids=()
    local -a targets=()
    local csv hp host port

    for csv in "$@"; do
        local -a _entries
        IFS=',' read -ra _entries <<< "$csv"
        for hp in "${_entries[@]}"; do
            host="${hp%:*}"
            port="${hp##*:}"
            # shellcheck disable=SC2016  # Single quotes intentional for inline bash
            timeout 2 bash -c 'echo >/dev/tcp/"$1"/"$2"' _ "$host" "$port" 2>/dev/null &
            pids+=($!)
            targets+=("$hp")
        done
    done

    local i
    for i in "${!pids[@]}"; do
        if ! wait "${pids[$i]}" 2>/dev/null; then
            echo "${targets[$i]}"
        fi
    done
}

# Wait for all services to be TCP-reachable from the current (coordinator) node.
# This catches services that pass the srun localhost health check but aren't
# reachable over the network, or that died between the health check and now.
# Retries every 5s up to max_wait_secs.
# Usage: wait_for_services_reachable max_wait_secs host_port_csv [host_port_csv2 ...]
# Returns: 0 if all reachable, 1 if any unreachable after timeout
wait_for_services_reachable() {
    local max_wait_secs="$1"
    shift

    local waited=0
    local check_interval=5
    local failed_output fail_count

    while [[ $waited -le $max_wait_secs ]]; do
        failed_output=$(_check_tcp_connectivity "$@")

        if [[ -z "$failed_output" ]]; then
            echo "All services TCP-reachable from coordinator node (${waited}s)"
            return 0
        fi

        fail_count=$(echo "$failed_output" | wc -l | tr -d ' ')
        echo "  TCP pre-check: $fail_count services unreachable (${waited}s/${max_wait_secs}s)"

        sleep "$check_interval"
        ((waited += check_interval))
    done

    echo "Error: services still TCP-unreachable after ${max_wait_secs}s:" >&2
    echo "$failed_output" >&2
    return 1
}

# =============================================================================
# run_netbench_half_half
# =============================================================================
# Run netbench in half-and-half mode.
#
# When bidirectional=false (unidirectional):
#   Split nodes into Group A (clients) and Group B (servers)
#   Run A → B only
#   1 service per node on NETBENCH_PORT
#
# When bidirectional=true:
#   Split nodes into Group A and Group B
#   Run TWO coordinator processes SIMULTANEOUSLY:
#     - Coordinator 1: A → B
#     - Coordinator 2: B → A
#   This achieves true simultaneous bidirectional with NO localhost connections
#   2 services per node (ports NETBENCH_PORT and NETBENCH_PORT+1)
#
# Required env vars: See header comments
# Sets: resfile, csvfile (output file paths for primary direction)
#
run_netbench_half_half() {
    local node_count
    local hosts_arg
    local remote_output_dir

    get_netbench_hosts_and_count

    # Require at least 2 nodes
    if [ "$node_count" -lt 2 ]; then
        echo "Error: netbench requires at least 2 nodes (got $node_count)" >&2
        return 1
    fi

    # Determine output directory
    if [ -n "${SLURM_JOB_NUM_NODES:-}" ]; then
        remote_output_dir="$output_dir"
    else
        remote_output_dir=$(cd "$(pwd)" && pwd)/"$(basename "$output_dir")" || {
            echo "Error: Unable to get absolute path" >&2
            return 1
        }
    fi
    mkdir -p "$remote_output_dir" || {
        echo "Error: Unable to create directory $remote_output_dir" >&2
        return 1
    }

    # Extract datestamp from output_dir
    local ds="${output_dir##*-}"

    # Determine mode abbreviation for file naming
    local mode_abbrev
    if [[ "$bidirectional" == "true" ]]; then
        mode_abbrev="bidir"
    else
        mode_abbrev="half"
    fi

    # Parse hosts into array and shuffle for random group assignment
    local -a hosts
    IFS=',' read -ra hosts <<< "$hosts_arg"
    shuffle_array hosts

    # Split into Group A (ceil(N/2)) and Group B (floor(N/2))
    local group_a_count=$(( (node_count + 1) / 2 ))
    # group_b_count = node_count - group_a_count (floor(N/2))

    local -a group_a=("${hosts[@]:0:$group_a_count}")
    local -a group_b=("${hosts[@]:$group_a_count}")

    # Build comma-separated strings with explicit port
    local group_a_csv
    local group_b_csv
    group_a_csv=$(build_host_list_with_port "$(IFS=','; echo "${group_a[*]}")" "$NETBENCH_PORT")
    group_b_csv=$(build_host_list_with_port "$(IFS=','; echo "${group_b[*]}")" "$NETBENCH_PORT")

    # Calculate per-thread transfer size based on NIC speed and target runtime
    local per_thread_size
    if ! per_thread_size=$(calculate_per_thread_size "$NETBENCH_HOST_NIC_GBPS" "$NETBENCH_TARGET_RUNTIME" "$thread_count"); then
        return 1
    fi

    if [[ "$bidirectional" == "true" ]]; then
        # =====================================================================
        # BIDIRECTIONAL MODE
        # =====================================================================
        # Run TWO coordinator processes simultaneously:
        #   Coordinator 1: Group A → Group B (uses NETBENCH_PORT)
        #   Coordinator 2: Group B → Group A (uses NETBENCH_PORT+1)
        # 2 services per node; no localhost connections since groups are disjoint

        # Generate separate output files for each direction
        local resfile_ab csvfile_ab resfile_ba csvfile_ba
        resfile_ab=$(printf "%s/netbench-%s-c_%03d-t_%03d_%s_iter%d_AtoB.out" \
            "$remote_output_dir" "$mode_abbrev" "$node_count" "$thread_count" "$ds" "$iteration")
        csvfile_ab=$(printf "%s/netbench-%s-c_%03d-t_%03d_%s_iter%d_AtoB.csv" \
            "$remote_output_dir" "$mode_abbrev" "$node_count" "$thread_count" "$ds" "$iteration")
        resfile_ba=$(printf "%s/netbench-%s-c_%03d-t_%03d_%s_iter%d_BtoA.out" \
            "$remote_output_dir" "$mode_abbrev" "$node_count" "$thread_count" "$ds" "$iteration")
        csvfile_ba=$(printf "%s/netbench-%s-c_%03d-t_%03d_%s_iter%d_BtoA.csv" \
            "$remote_output_dir" "$mode_abbrev" "$node_count" "$thread_count" "$ds" "$iteration")

        # Each coordinator uses a DIFFERENT service port to avoid conflicts
        # (a single elbencho service can only handle one benchmark at a time)
        local port_ab="$NETBENCH_PORT"
        local port_ba=$((NETBENCH_PORT + 1))

        # Build host lists with their respective ports
        local group_a_port_ab group_b_port_ab group_a_port_ba group_b_port_ba
        group_a_port_ab=$(build_host_list_with_port "$(IFS=','; echo "${group_a[*]}")" "$port_ab")
        group_b_port_ab=$(build_host_list_with_port "$(IFS=','; echo "${group_b[*]}")" "$port_ab")
        group_a_port_ba=$(build_host_list_with_port "$(IFS=','; echo "${group_a[*]}")" "$port_ba")
        group_b_port_ba=$(build_host_list_with_port "$(IFS=','; echo "${group_b[*]}")" "$port_ba")

        echo "Netbench Bidirectional Parameters:"
        echo "  Datestamp: $ds"
        echo "  Output Dir: $remote_output_dir"
        echo "  Node Count: $node_count"
        echo "  Threads: $thread_count"
        echo "  Iteration: $iteration"
        echo "  Block Size: $NETBENCH_BLOCKSIZE"
        echo "  Response Size: $NETBENCH_RESPSIZE"
        echo "  Per-Thread Size: $per_thread_size (NIC: ${NETBENCH_HOST_NIC_GBPS}Gbps, Target: ${NETBENCH_TARGET_RUNTIME}s)"
        echo "  Group A: ${#group_a[@]} nodes"
        echo "  Group B: ${#group_b[@]} nodes"
        echo
        echo "Running TWO simultaneous coordinators (each uses different service port):"
        echo "  Coordinator 1 (A→B): port $port_ab - clients $group_a_port_ab → servers $group_b_port_ab"
        echo "  Coordinator 2 (B→A): port $port_ba - clients $group_b_port_ba → servers $group_a_port_ba"
        echo
        echo "Output files:"
        echo "  A→B: $resfile_ab, $csvfile_ab"
        echo "  B→A: $resfile_ba, $csvfile_ba"
        echo

        # Build args for both directions (each uses its own port)
        local netbench_args_ab=(
            --netbench
            --clients "$group_a_port_ab"
            --servers "$group_b_port_ab"
            --port "$port_ab"
            --block "$NETBENCH_BLOCKSIZE"
            --respsize "$NETBENCH_RESPSIZE"
            --size "$per_thread_size"
            --threads "$thread_count"
            --lat
            --lathisto
            --latpercent
            --nolive
            --resfile "$resfile_ab"
            --csvfile "$csvfile_ab"
        )

        local netbench_args_ba=(
            --netbench
            --clients "$group_b_port_ba"
            --servers "$group_a_port_ba"
            --port "$port_ba"
            --block "$NETBENCH_BLOCKSIZE"
            --respsize "$NETBENCH_RESPSIZE"
            --size "$per_thread_size"
            --threads "$thread_count"
            --lat
            --lathisto
            --latpercent
            --nolive
            --resfile "$resfile_ba"
            --csvfile "$csvfile_ba"
        )

        # Verify services are TCP-reachable from this node before launching coordinators
        echo "Verifying service connectivity from coordinator node..."
        if ! wait_for_services_reachable 60 \
                "$group_a_port_ab" "$group_b_port_ab" \
                "$group_b_port_ba" "$group_a_port_ba"; then
            echo "Error: Cannot reach all services, aborting benchmark" >&2
            return 1
        fi

        # Run both coordinators simultaneously
        echo "Starting coordinator 1 (A→B)..."
        printf "# elbencho --svcwait 120 %s\n" "${netbench_args_ab[*]}"
        "$ELBENCHO" --svcwait 120 "${netbench_args_ab[@]}" &
        local pid_ab=$!

        echo "Starting coordinator 2 (B→A)..."
        printf "# elbencho --svcwait 120 %s\n" "${netbench_args_ba[*]}"
        "$ELBENCHO" --svcwait 120 "${netbench_args_ba[@]}" &
        local pid_ba=$!

        # Wait for both to complete
        echo "Waiting for both coordinators to complete..."
        local rc_ab=0 rc_ba=0
        wait $pid_ab || rc_ab=$?
        wait $pid_ba || rc_ba=$?

        if [[ $rc_ab -ne 0 ]]; then
            echo "Error: Coordinator A→B failed with exit code $rc_ab" >&2
        fi
        if [[ $rc_ba -ne 0 ]]; then
            echo "Error: Coordinator B→A failed with exit code $rc_ba" >&2
        fi

        # On failure, diagnose which services are now unreachable
        if [[ $rc_ab -ne 0 ]] || [[ $rc_ba -ne 0 ]]; then
            echo "Post-failure diagnostic — checking which services are still alive..."
            local diag_output
            diag_output=$(_check_tcp_connectivity \
                "$group_a_port_ab" "$group_b_port_ab" \
                "$group_b_port_ba" "$group_a_port_ba")
            if [[ -n "$diag_output" ]]; then
                local dead_count
                dead_count=$(echo "$diag_output" | wc -l | tr -d ' ')
                echo "  $dead_count services now unreachable:"
                while IFS= read -r _line; do
                    echo "    $_line"
                done <<< "$diag_output"
            else
                echo "  All services still reachable (coordinator may have failed for another reason)"
            fi
            echo
            return 1
        fi

        echo "Bidirectional netbench complete."
        echo
    else
        # =====================================================================
        # UNIDIRECTIONAL MODE
        # =====================================================================
        # Run both directions SEQUENTIALLY: A → B, then B → A
        # 1 service per node on NETBENCH_PORT

        # Generate separate output files for each direction
        local resfile_ab csvfile_ab resfile_ba csvfile_ba
        resfile_ab=$(printf "%s/netbench-%s-c_%03d-t_%03d_%s_iter%d_AtoB.out" \
            "$remote_output_dir" "$mode_abbrev" "$node_count" "$thread_count" "$ds" "$iteration")
        csvfile_ab=$(printf "%s/netbench-%s-c_%03d-t_%03d_%s_iter%d_AtoB.csv" \
            "$remote_output_dir" "$mode_abbrev" "$node_count" "$thread_count" "$ds" "$iteration")
        resfile_ba=$(printf "%s/netbench-%s-c_%03d-t_%03d_%s_iter%d_BtoA.out" \
            "$remote_output_dir" "$mode_abbrev" "$node_count" "$thread_count" "$ds" "$iteration")
        csvfile_ba=$(printf "%s/netbench-%s-c_%03d-t_%03d_%s_iter%d_BtoA.csv" \
            "$remote_output_dir" "$mode_abbrev" "$node_count" "$thread_count" "$ds" "$iteration")

        echo "Netbench Unidirectional Parameters:"
        echo "  Datestamp: $ds"
        echo "  Output Dir: $remote_output_dir"
        echo "  Node Count: $node_count"
        echo "  Threads: $thread_count"
        echo "  Iteration: $iteration"
        echo "  Block Size: $NETBENCH_BLOCKSIZE"
        echo "  Response Size: $NETBENCH_RESPSIZE"
        echo "  Per-Thread Size: $per_thread_size (NIC: ${NETBENCH_HOST_NIC_GBPS}Gbps, Target: ${NETBENCH_TARGET_RUNTIME}s)"
        echo "  Port: $NETBENCH_PORT"
        echo "  Group A: ${#group_a[@]} nodes - $group_a_csv"
        echo "  Group B: ${#group_b[@]} nodes - $group_b_csv"
        echo
        echo "Running BOTH directions sequentially:"
        echo "  Direction 1: Group A → Group B"
        echo "  Direction 2: Group B → Group A"
        echo
        echo "Output files:"
        echo "  A→B: $resfile_ab, $csvfile_ab"
        echo "  B→A: $resfile_ba, $csvfile_ba"
        echo

        # Verify services are TCP-reachable from this node before launching coordinators
        echo "Verifying service connectivity from coordinator node..."
        if ! wait_for_services_reachable 60 "$group_a_csv" "$group_b_csv"; then
            echo "Error: Cannot reach all services, aborting benchmark" >&2
            return 1
        fi

        # Direction 1: A → B
        echo "=== Direction 1: Group A → Group B ==="
        local netbench_args_ab=(
            --netbench
            --clients "$group_a_csv"
            --servers "$group_b_csv"
            --port "$NETBENCH_PORT"
            --block "$NETBENCH_BLOCKSIZE"
            --respsize "$NETBENCH_RESPSIZE"
            --size "$per_thread_size"
            --threads "$thread_count"
            --lat
            --lathisto
            --latpercent
            --nolive
            --resfile "$resfile_ab"
            --csvfile "$csvfile_ab"
        )
        run_a_netbench "${netbench_args_ab[@]}"

        # Direction 2: B → A
        echo "=== Direction 2: Group B → Group A ==="
        local netbench_args_ba=(
            --netbench
            --clients "$group_b_csv"
            --servers "$group_a_csv"
            --port "$NETBENCH_PORT"
            --block "$NETBENCH_BLOCKSIZE"
            --respsize "$NETBENCH_RESPSIZE"
            --size "$per_thread_size"
            --threads "$thread_count"
            --lat
            --lathisto
            --latpercent
            --nolive
            --resfile "$resfile_ba"
            --csvfile "$csvfile_ba"
        )
        run_a_netbench "${netbench_args_ba[@]}"

        echo "Unidirectional netbench complete (both directions)."
        echo
    fi
}
