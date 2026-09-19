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
# Elbencho sweep coordinator -- runs as the body of ONE sbatch allocation
# sized to max(nodes among non-SUCCESS executions). Replaces the per-
# (nodes,io_size) sbatch chain used previously: services are started once
# via background srun, then every non-SUCCESS execution is run sequentially
# on a per-execution subset of the allocation's hosts. Aborts on first FAILED
# so the user can fix and re-run nv-elbencho-sweep.sh with --resume.
#
# Usage (typically invoked indirectly via dispatch_slurm_executions):
#   sbatch [opts] _nv-elbencho-coordinator.sh <OUTPUT_DIR> <DISPATCH_LOCK_TOKEN>

if [[ -z "${SLURM_JOB_ID}" ]]; then
    echo "This script should be run via slurm." >&2
    exit 1
fi

# Boilerplate: find SCALE_TEST_BASE by walking up from SLURM_SUBMIT_DIR
dir="${SLURM_SUBMIT_DIR}"
while [[ "$dir" != "/" && ! -d "$dir/storage-tests" ]]; do
    dir="$(dirname "$dir")"
done
[[ "$dir" == "/" ]] && { echo "Error: Could not find SCALE_TEST_BASE" >&2; exit 1; }
SCALE_TEST_BASE="$dir"

if ! source_output=$("$SHELL" -c ". ${SCALE_TEST_BASE}/env.sh" 2>&1); then
    printf "%s\n\nFailed to source env.sh; fix ^^^^^^^^^^\n" "$source_output"
    exit 1
fi

# shellcheck disable=SC1091
source "${SCALE_TEST_BASE}/env.sh"

# If invoked for an existing sweep results dir, also source the canonical
# env_used.sh sidecar so that the original sweep-level settings (TEST_DIRS,
# FS_MAX_*, ELBENCHO_*, CLI flags) are in scope even when env.sh on this
# compute node has different defaults. Falls back silently if absent for
# backward-compat with the initial-run case where the parent script has
# already laid the sidecar down.
OUTPUT_DIR="${1:?missing OUTPUT_DIR argument}"
DISPATCH_LOCK_TOKEN="${2:?missing DISPATCH_LOCK_TOKEN argument}"
if [[ -f "${OUTPUT_DIR}/env_used.sh" ]]; then
    # Explicit legacy defaults keep an older snapshot isolated from the
    # compute node's current env.sh configuration.
    ELBENCHO_FILE_LAYOUT=worker-directories
    ELBENCHO_FILES_PER_NODE=
    ELBENCHO_FILE_SIZE=
    export ELBENCHO_FILE_LAYOUT ELBENCHO_FILES_PER_NODE ELBENCHO_FILE_SIZE
    # shellcheck disable=SC1091
    source "${OUTPUT_DIR}/env_used.sh"
fi

# Source the elbencho functions library (defines run_elbencho_io_sweep_iteration,
# coordinator_run_one_execution lives in env_functions.sh already sourced via env.sh).
# shellcheck disable=SC1091
source "${SCALE_TEST_BASE}/lib/_elbencho_functions.sh"

EXECUTIONS_DIR="${OUTPUT_DIR}/executions"
if [[ ! -d "$EXECUTIONS_DIR" ]]; then
    echo "Error: missing executions dir: $EXECUTIONS_DIR" >&2
    exit 1
fi

COORDINATOR_LOCK_OWNED=0
# shellcheck disable=SC2317,SC2329  # invoked via trap EXIT
_coordinator_release_dispatch_lock() {
    if [[ "$COORDINATOR_LOCK_OWNED" -eq 1 ]]; then
        _elbencho_release_dispatch_lock \
            "$EXECUTIONS_DIR" "$DISPATCH_LOCK_TOKEN" || true
        COORDINATOR_LOCK_OWNED=0
    fi
    return 0
}
trap '_coordinator_release_dispatch_lock' EXIT

# Adopt the exact lock token created before sbatch submission. A different
# dispatcher can neither overwrite this lease nor reset its RUNNING work.
_elbencho_adopt_slurm_dispatch_lock \
    "$EXECUTIONS_DIR" "$DISPATCH_LOCK_TOKEN" "$SLURM_JOB_ID" || exit 1
COORDINATOR_LOCK_OWNED=1

# Build the allocation IPv4 nodelist. Slurm may canonicalize SLURM_JOB_NODELIST
# instead of preserving the configured include-list order, so restore that
# order when ORDER_NODES is active before choosing per-execution prefixes.
mapfile -t allocation_nodes < <(
    scontrol show hostname "${SLURM_JOB_NODELIST:-}"
)
if [[ -n "${ORDER_NODES_ENABLED:-}" && ${#SLURM_ORDERED_NODES[@]} -gt 0 ]]; then
    ordered_allocation_nodes=()
    for ordered_node in "${SLURM_ORDERED_NODES[@]}"; do
        for allocated_node in "${allocation_nodes[@]}"; do
            if [[ "$ordered_node" == "$allocated_node" ]]; then
                ordered_allocation_nodes+=("$allocated_node")
                break
            fi
        done
    done
    if [[ ${#ordered_allocation_nodes[@]} -ne ${#allocation_nodes[@]} ]]; then
        echo "Error: configured node order does not match the Slurm allocation" >&2
        exit 1
    fi
    allocation_nodes=("${ordered_allocation_nodes[@]}")
fi

# Some clusters intermittently fail to resolve short hostnames in elbencho's
# resolver, so resolve once here and propagate IPs.
nodelist_ips=()
while read -r node; do
    ip=$(getent ahostsv4 "$node" | awk '{print $1; exit}')
    if [[ -z "$ip" ]]; then
        >&2 echo "Warning: Could not resolve $node to IPv4, using hostname"
        ip="$node"
    fi
    nodelist_ips+=("$ip")
done < <(printf '%s\n' "${allocation_nodes[@]}")
ALLOC_HOSTS_CSV=$(IFS=,; printf '%s' "${nodelist_ips[*]}")
# nodelist_expanded_comma_separated is read by run_an_elbencho (a bash
# function in this same shell scope) as a fallback when no per-execution
# ELBENCHO_RUN_HOSTS_CSV override is set; populate it with the allocation-
# wide CSV for safety. Plain assignment (not `export`) keeps the lowercase
# name compatible with the "env vars must be ALL_CAPS" static-analysis rule.
# shellcheck disable=SC2034  # consumed dynamically by run_an_elbencho
nodelist_expanded_comma_separated="$ALLOC_HOSTS_CSV"

echo "Coordinator started: SLURM_JOB_ID=$SLURM_JOB_ID NODES=$SLURM_JOB_NUM_NODES OUTPUT_DIR=$OUTPUT_DIR"
echo "Allocation hosts (CSV): $ALLOC_HOSTS_CSV"
# 3-line compact sweep summary so the user knows what's about to run during
# the (potentially very long) execution loop. DS comes from the OUTPUT_DIR
# basename suffix; nodes_spec / dio_or_bio / rand_option etc. are restored
# from env_used.sh sourced above.
print_elbencho_sweep_compact_summary \
    "${OUTPUT_DIR##*-}" "${nodes_spec:-?}"

# Ownership was acquired before submission and adopted above, before touching
# RUNNING state.

# Sweep RUNNING -> PENDING (recover from any prior interruption)
_elbencho_sweep_running_to_pending "$EXECUTIONS_DIR" || exit 1

# Start elbencho services on every allocation node, in background, ONCE.
SRUN_ELBENCHO_PID=""
SRUN_ELBENCHO_PID_FILE=""
ACTIVE_EXECUTION_ID=""
if [[ "${SLURM_JOB_NUM_NODES:-1}" -gt 1 ]]; then
    stop_elbencho_services_srun "" || true  # initial cleanup of stale processes
    SRUN_ELBENCHO_PID=$(start_elbencho_services_srun "")
    if [[ ! "$SRUN_ELBENCHO_PID" =~ ^[0-9]+$ ]]; then
        echo "Error: unable to start elbencho service owner" >&2
        exit 1
    fi
    SRUN_ELBENCHO_PID_FILE="${EXECUTIONS_DIR}/.service-srun.pid"
    if ! _atomic_write_sentinel "$SRUN_ELBENCHO_PID_FILE" "$SRUN_ELBENCHO_PID"; then
        echo "Error: unable to record elbencho service owner PID" >&2
        stop_elbencho_services_srun "$SRUN_ELBENCHO_PID" || true
        exit 1
    fi
    if ! check_elbencho_services_srun ""; then
        echo "Error: elbencho services failed initial health check" >&2
        stop_elbencho_services_srun "$SRUN_ELBENCHO_PID" || true
        rm -f -- "$SRUN_ELBENCHO_PID_FILE"
        exit 1
    fi
fi

# Refresh the coordinator's copy after a phase-level check restarts services
# inside coordinator_run_one_execution's subshell.
_coordinator_refresh_service_pid() {
    local persisted_pid
    if [[ -z "${SRUN_ELBENCHO_PID_FILE:-}" ]]; then
        return 0
    fi
    if ! read -r persisted_pid < "$SRUN_ELBENCHO_PID_FILE" \
            || [[ ! "$persisted_pid" =~ ^[0-9]+$ ]]; then
        echo "Error: invalid elbencho service owner PID file: $SRUN_ELBENCHO_PID_FILE" >&2
        return 1
    fi
    SRUN_ELBENCHO_PID="$persisted_pid"
    return 0
}

# shellcheck disable=SC2317,SC2329  # invoked via trap EXIT
_coordinator_finalize_active_execution() {
    local active_id="${ACTIVE_EXECUTION_ID:-}"
    if [[ -z "$active_id" ]]; then
        return 0
    fi
    local active_status="${EXECUTIONS_DIR}/${active_id}.status"
    if [[ "$(cat "$active_status" 2>/dev/null)" != SUCCESS ]]; then
        _elbencho_finalize_shared_failure_from_nnnn \
            "${EXECUTIONS_DIR}/${active_id}.sh" "$active_id" \
            "$OUTPUT_DIR" || true
        _atomic_write_sentinel "$active_status" FAILED || \
            echo "Error: unable to record FAILED for active execution ${active_id}" >&2
    fi
    ACTIVE_EXECUTION_ID=""
    return 0
}

# shellcheck disable=SC2317,SC2329  # invoked via EXIT/INT/TERM traps
_coordinator_cleanup() {
    _coordinator_finalize_active_execution
    _coordinator_refresh_service_pid || true
    if [[ -n "${SRUN_ELBENCHO_PID:-}" ]]; then
        _echo_ts "[coordinator] stopping elbencho services (PID ${SRUN_ELBENCHO_PID})..."
        stop_elbencho_services_srun "$SRUN_ELBENCHO_PID" || true
        SRUN_ELBENCHO_PID=""
    fi
    if [[ -n "${SRUN_ELBENCHO_PID_FILE:-}" ]]; then
        rm -f -- "$SRUN_ELBENCHO_PID_FILE"
    fi
    return 0
}
trap '_coordinator_cleanup; _coordinator_release_dispatch_lock' EXIT

# shellcheck disable=SC2317,SC2329  # invoked via signal traps
_coordinator_signal_exit() {
    local signal_name="$1"
    local signal_rc=1
    [[ "$signal_name" == INT ]] && signal_rc=130
    [[ "$signal_name" == TERM ]] && signal_rc=143
    trap - INT TERM
    exit "$signal_rc"
}
trap '_coordinator_signal_exit INT' INT
trap '_coordinator_signal_exit TERM' TERM

# Iterate non-SUCCESS executions in NNNN order.
overall_rc=0
total_done=0
total_skipped=0
# Read the execution-id list via FD 3 instead of stdin. Inside the loop body
# coordinator_run_one_execution runs elbencho, whose phase-boundary service
# health checks invoke srun. srun in particular reads/buffers its parent's
# stdin to forward to remote tasks, which consumed the process-substitution
# feeding this loop (when it was wired to FD 0) and made the loop exit after
# the first iteration.
while IFS= read -r -u 3 ID; do
    status_file="${EXECUTIONS_DIR}/${ID}.status"
    if [[ ! -f "$status_file" ]]; then
        echo "[coordinator] warning: ${ID} has no status file; skipping"
        continue
    fi
    if [[ "$(cat "$status_file" 2>/dev/null)" == "SUCCESS" ]]; then
        total_skipped=$((total_skipped + 1))
        continue
    fi
    # Capture rc via || — `if ! cmd; then rc=$?` is always 0 (the ! succeeded).
    ACTIVE_EXECUTION_ID="$ID"
    coordinator_run_one_execution \
            "$EXECUTIONS_DIR" "$ID" "$ALLOC_HOSTS_CSV" "$OUTPUT_DIR" \
        || overall_rc=$?
    if [[ "$(cat "${EXECUTIONS_DIR}/${ID}.status" 2>/dev/null)" == SUCCESS ]]; then
        ACTIVE_EXECUTION_ID=""
    fi
    if ! _coordinator_refresh_service_pid && [[ "$overall_rc" -eq 0 ]]; then
        overall_rc=1
        _elbencho_finalize_shared_failure_from_nnnn \
            "${EXECUTIONS_DIR}/${ID}.sh" "$ID" "$OUTPUT_DIR" || true
        _atomic_write_sentinel "${EXECUTIONS_DIR}/${ID}.status" FAILED || true
    fi
    if [[ "$overall_rc" -ne 0 ]]; then
        echo "[coordinator] aborting remaining executions due to FAILED ${ID}"
        break
    fi
    total_done=$((total_done + 1))
done 3< <(list_elbencho_execution_ids "$EXECUTIONS_DIR")

echo "[coordinator] summary: ${total_done} succeeded, ${total_skipped} previously-SUCCESS skipped, overall_rc=${overall_rc}"
exit "$overall_rc"
