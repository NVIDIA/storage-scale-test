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

# Durable, API-independent body of a Kubernetes filesystem sweep Job.
#
# The submission side prepares a verified control bundle on the shared PVC and
# starts this program as the Job's command. This program deliberately has no
# kubectl dependency or Kubernetes API credentials: after it starts, the PVC,
# the frozen worker endpoint list, and Pod networking are its entire substrate.
#
# Usage:
#   _nv-elbencho-kubectl-coordinator.sh CONTROL_DIR STATE_DIR SCRATCH_DIR ID
#
# CONTROL_DIR contains bundle-manifest.tsv, run-metadata.tsv, env_used.{sh,yaml},
# the two shared libraries, worker-endpoints.tsv, and executions/NNNN.sh. STATE_DIR is the
# durable attempt ledger. SCRATCH_DIR is an emptyDir-backed, disposable result
# root. bundle-manifest.tsv is tab-separated SHA-256 and control-relative path
# rows, with an optional leading comment. It is verified before any ledger
# mutation. publication-manifest.tsv is atomically replaced *after* each cell's
# artifacts, terminal state, and summary are durable; it is the collection
# commit point.

set -uo pipefail

readonly KUBECTL_COORDINATOR_SCHEMA=1
KUBECTL_COORDINATOR_BASENAME=$(basename "$0")
readonly KUBECTL_COORDINATOR_BASENAME

_coordinator_error() {
    printf 'Error: kubectl coordinator: %s\n' "$*" >&2
}

_coordinator_atomic_write() {
    local path="$1" value="$2"
    local directory tmp
    directory=$(dirname "$path") || return 1
    tmp="$directory/.${path##*/}.tmp.${BASHPID:-$$}.${RANDOM}"
    printf '%s\n' "$value" > "$tmp" && mv -f -- "$tmp" "$path"
}

_coordinator_regular_relative_path() {
    local value="$1"
    [[ -n "$value" && "$value" != /* && "$value" != *$'\n'* \
        && "$value" != *$'\r'* && "$value" != *$'\t'* \
        && "$value" != . && "$value" != .. && "$value" != */../* \
        && "$value" != ../* && "$value" != */.. ]] || return 1
    local component
    IFS=/ read -ra _coordinator_path_parts <<< "$value"
    for component in "${_coordinator_path_parts[@]}"; do
        [[ -n "$component" && "$component" != . && "$component" != .. ]] || return 1
    done
}

_coordinator_hash_file() {
    sha256sum -- "$1" | awk '{print $1}'
}

_coordinator_verify_bundle() {
    local manifest="$CONTROL_DIR/bundle-manifest.tsv"
    [[ -f "$manifest" && ! -L "$manifest" ]] || {
        _coordinator_error "missing regular bundle manifest"
        return 1
    }
    local line digest relative path calculated rows=0
    local -A seen=()
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        IFS=$'\t' read -r digest relative extra <<< "$line"
        if [[ -n "${extra:-}" || ! "$digest" =~ ^[0-9a-f]{64}$ \
                || -v seen["$relative"] ]] \
                || ! _coordinator_regular_relative_path "$relative"; then
            _coordinator_error "invalid bundle manifest row"
            return 1
        fi
        path="$CONTROL_DIR/$relative"
        [[ -f "$path" && ! -L "$path" ]] || {
            _coordinator_error "bundle file is missing or not regular: $relative"
            return 1
        }
        calculated=$(_coordinator_hash_file "$path") || return 1
        [[ "$calculated" == "$digest" ]] || {
            _coordinator_error "bundle digest mismatch: $relative"
            return 1
        }
        seen["$relative"]=1
        rows=$((rows + 1))
    done < "$manifest"
    [[ "$rows" -gt 0 ]] || {
        _coordinator_error "bundle manifest has no entries"
        return 1
    }
    local required
    for required in env_used.sh env_used.yaml _platform_functions.sh \
        _elbencho_functions.sh _nv-elbencho-kubectl-functions.sh \
        coordinator.sh worker-endpoints.tsv run-metadata.tsv; do
        [[ -v seen["$required"] ]] || {
            _coordinator_error "bundle manifest omits required file: $required"
            return 1
        }
    done
    local found_execution=0
    for relative in "${!seen[@]}"; do
        [[ "$relative" == executions/[0-9][0-9][0-9][0-9].sh ]] \
            && found_execution=1
    done
    [[ "$found_execution" -eq 1 ]] || {
        _coordinator_error "bundle manifest has no execution definitions"
        return 1
    }
}

_coordinator_load_run_metadata() {
    local metadata="$CONTROL_DIR/run-metadata.tsv" key value extra rows=0
    [[ -f "$metadata" && ! -L "$metadata" ]] || return 1
    unset COORDINATOR_OUTPUT_BASENAME
    while IFS=$'\t' read -r key value extra || [[ -n "${key:-}" ]]; do
        [[ -z "${extra:-}" ]] || return 1
        case "$key" in
            output_basename)
                [[ -z "${COORDINATOR_OUTPUT_BASENAME:-}" \
                    && "$value" =~ ^elbencho-[0-9]{8}Z[0-9]{6}$ ]] || return 1
                COORDINATOR_OUTPUT_BASENAME="$value"
                ;;
            attempt_id) [[ "$value" == "$ATTEMPT_ID" ]] || return 1 ;;
            *) return 1 ;;
        esac
        rows=$((rows + 1))
    done < "$metadata"
    [[ "$rows" -eq 2 && -n "${COORDINATOR_OUTPUT_BASENAME:-}" ]]
}

_coordinator_validate_arguments() {
    [[ "$#" -eq 4 ]] || {
        _coordinator_error "usage: $KUBECTL_COORDINATOR_BASENAME CONTROL_DIR STATE_DIR SCRATCH_DIR ID"
        return 1
    }
    local input_control="$1" input_state="$2" input_scratch="$3"
    ATTEMPT_ID="$4"
    [[ "$ATTEMPT_ID" =~ ^[0-9a-f]{8}$ ]] || {
        _coordinator_error "invalid attempt ID"
        return 1
    }
    local path
    for path in "$input_control" "$input_state" "$input_scratch"; do
        [[ "$path" == /* && "$path" != *$'\n'* && "$path" != *$'\r'* \
            && "$path" != *$'\t'* ]] || {
            _coordinator_error "paths must be absolute single-line values"
            return 1
        }
    done
    CONTROL_DIR=$(realpath -e -- "$input_control") || return 1
    [[ -d "$CONTROL_DIR" && ! -L "$CONTROL_DIR" ]] || return 1
    local expected_state
    expected_state="$(dirname "$CONTROL_DIR")/state"
    STATE_DIR=$(realpath -m -- "$input_state") || return 1
    [[ "$STATE_DIR" == "$expected_state" ]] || {
        _coordinator_error "state directory must be the control directory's sibling state"
        return 1
    }
    [[ ! -e "$STATE_DIR" || ( -d "$STATE_DIR" && ! -L "$STATE_DIR" ) ]] || {
        _coordinator_error "state directory must not be a symlink or non-directory"
        return 1
    }
    SCRATCH_DIR=$(realpath -m -- "$input_scratch") || return 1
    [[ "$SCRATCH_DIR" != / && "$SCRATCH_DIR" != /tmp \
        && "$SCRATCH_DIR" != "$STATE_DIR" \
        && "$SCRATCH_DIR" != "$STATE_DIR"/* ]] || {
        _coordinator_error "unsafe coordinator scratch directory"
        return 1
    }
    if [[ "${STORAGE_SCALE_TEST_INTEGRATION:-}" != 1 ]]; then
        [[ "$CONTROL_DIR" == "/mnt/storage-scale-test/.storage-scale-test/runs/$ATTEMPT_ID/control" \
            && "$SCRATCH_DIR" == "/tmp/storage-scale-test/$ATTEMPT_ID" ]] || {
                _coordinator_error "control or scratch path is outside the Kubernetes attempt layout"
                return 1
            }
    fi
}

_coordinator_initialize_state() {
    if [[ -e "$STATE_DIR" ]] \
            && find -P "$STATE_DIR" -type l -print -quit | grep -q .; then
        _coordinator_error "attempt state contains a symlink"
        return 1
    fi
    mkdir -p "$STATE_DIR/executions" "$STATE_DIR/results" || return 1
    [[ ! -e "$STATE_DIR/coordinator.lock" ]] || {
        _coordinator_error "another coordinator owns this attempt"
        return 1
    }
    if ! mkdir "$STATE_DIR/coordinator.lock"; then
        _coordinator_error "unable to acquire coordinator lock"
        return 1
    fi
    {
        printf 'schema\t%s\n' "$KUBECTL_COORDINATOR_SCHEMA"
        printf 'attempt_id\t%s\n' "$ATTEMPT_ID"
        printf 'pid\t%s\n' "${BASHPID:-$$}"
        printf 'started_utc\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$STATE_DIR/coordinator.lock/owner.tsv" || return 1
    local snapshot
    for snapshot in env_used.sh env_used.yaml; do
        if [[ ! -e "$STATE_DIR/$snapshot" ]]; then
            cp -- "$CONTROL_DIR/$snapshot" "$STATE_DIR/$snapshot" || return 1
        elif [[ ! -f "$STATE_DIR/$snapshot" || -L "$STATE_DIR/$snapshot" ]] \
                || ! cmp -s -- "$CONTROL_DIR/$snapshot" "$STATE_DIR/$snapshot"; then
            _coordinator_error "existing working snapshot differs: $snapshot"
            return 1
        fi
    done
    local definition id state_file
    for definition in "$CONTROL_DIR"/executions/[0-9][0-9][0-9][0-9].sh; do
        [[ -f "$definition" ]] || continue
        id=$(basename "$definition" .sh)
        state_file="$STATE_DIR/executions/$id.status"
        if [[ ! -e "$state_file" ]]; then
            _coordinator_atomic_write "$state_file" PENDING || return 1
        elif [[ ! -f "$state_file" || -L "$state_file" ]]; then
            _coordinator_error "execution state is not a regular file: $id"
            return 1
        fi
    done
}

_coordinator_valid_ipv4() {
    local address="$1" octet
    [[ "$address" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
    IFS=. read -ra _coordinator_octets <<< "$address"
    for octet in "${_coordinator_octets[@]}"; do
        ((10#$octet <= 255)) || return 1
    done
}

_coordinator_validate_endpoint_rows() {
    local line node pod_name uid ip extra count=0
    declare -ga COORDINATOR_NODES=()
    declare -ga COORDINATOR_PODS=()
    declare -ga COORDINATOR_POD_UIDS=()
    declare -ga COORDINATOR_ENDPOINTS=()
    local -A seen_nodes=() seen_pods=() seen_uids=() seen_ips=()
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "$line" || "$line" == \#* ]] && continue
        IFS=$'\t' read -r node pod_name uid ip extra <<< "$line"
        if [[ -n "${extra:-}" || ! "$node" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ \
                || ! "$pod_name" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ \
                || ! "$uid" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]*$ \
                || -v seen_nodes["$node"] || -v seen_pods["$pod_name"] \
                || -v seen_uids["$uid"] \
                || -v seen_ips["$ip"] ]] \
                || ! _coordinator_valid_ipv4 "$ip"; then
            _coordinator_error "invalid worker endpoint row"
            return 1
        fi
        COORDINATOR_NODES+=("$node")
        COORDINATOR_PODS+=("$pod_name")
        COORDINATOR_POD_UIDS+=("$uid")
        COORDINATOR_ENDPOINTS+=("$ip")
        seen_nodes["$node"]=1
        seen_pods["$pod_name"]=1
        seen_uids["$uid"]=1
        seen_ips["$ip"]=1
        count=$((count + 1))
    done < "$CONTROL_DIR/worker-endpoints.tsv"
    [[ "$count" -gt 0 ]] || {
        _coordinator_error "worker endpoint list is empty"
        return 1
    }
}

_coordinator_probe_endpoint() {
    local endpoint="$1"
    # shellcheck disable=SC2016  # $1 and $response belong to the inner Bash.
    timeout --kill-after=2s 5s bash -c '
        exec 3<>"/dev/tcp/$1/1611"
        printf "GET /status HTTP/1.0\\r\\nHost: %s:1611\\r\\n\\r\\n" "$1" >&3
        IFS= read -r response <&3
        [[ "$response" == *" 200 "* ]]
    ' bash "$endpoint"
}

_coordinator_health_hook() {
    local phase_name="$1"
    local endpoint
    for endpoint in "${COORDINATOR_SELECTED_ENDPOINTS[@]}"; do
        if ! _coordinator_probe_endpoint "$endpoint"; then
            _coordinator_error "worker endpoint $endpoint is unhealthy after $phase_name"
            return 1
        fi
    done
}

_coordinator_pick_workers() {
    local id="$1" nodes="$2" output="$3"
    [[ "$nodes" =~ ^[1-9][0-9]*$ && "$nodes" -le "${#COORDINATOR_ENDPOINTS[@]}" ]] || {
        _coordinator_error "execution $id requires unavailable worker count $nodes"
        return 1
    }
    if [[ -e "$output" ]]; then
        _coordinator_error "worker selection already exists for pending execution $id"
        return 1
    fi
    local -a positions=()
    local index
    if [[ -n "${ORDER_NODES_ENABLED:-}" ]]; then
        for ((index = 0; index < nodes; index++)); do positions+=("$index"); done
    else
        mapfile -t positions < <(shuf -i "0-$((${#COORDINATOR_ENDPOINTS[@]} - 1))" -n "$nodes") || return 1
    fi
    local tmp="$output.tmp.${BASHPID:-$$}.${RANDOM}"
    : > "$tmp" || return 1
    COORDINATOR_SELECTED_ENDPOINTS=()
    for index in "${positions[@]}"; do
        printf '%s\t%s\t%s\t%s\n' "${COORDINATOR_NODES[$index]}" \
            "${COORDINATOR_PODS[$index]}" "${COORDINATOR_POD_UIDS[$index]}" \
            "${COORDINATOR_ENDPOINTS[$index]}" >> "$tmp" || return 1
        COORDINATOR_SELECTED_ENDPOINTS+=("${COORDINATOR_ENDPOINTS[$index]}")
    done
    mv -f -- "$tmp" "$output"
}

_coordinator_load_workers() {
    local path="$1" node pod_name uid endpoint extra
    COORDINATOR_SELECTED_ENDPOINTS=()
    [[ -f "$path" && ! -L "$path" ]] || return 1
    while IFS=$'\t' read -r node pod_name uid endpoint extra; do
        if [[ -n "${extra:-}" || -z "$node" \
                || ! "$pod_name" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ \
                || ! "$uid" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]*$ ]] \
                || ! _coordinator_valid_ipv4 "$endpoint"; then
            return 1
        fi
        COORDINATOR_SELECTED_ENDPOINTS+=("$endpoint")
    done < "$path"
}

_coordinator_relative_files() {
    local root="$1" path relative
    if find -P "$root" -type l -print -quit | grep -q .; then
        _coordinator_error "refusing symlinked durable artifact"
        return 1
    fi
    while IFS= read -r -d '' path; do
        [[ -f "$path" && ! -L "$path" ]] || continue
        relative="${path#"$root"/}"
        _coordinator_regular_relative_path "$relative" || return 1
        printf '%s\n' "$relative"
    done < <(find -P "$root" -type f -print0 | LC_ALL=C sort -z)
}

_coordinator_copy_scratch_results() {
    local id="$1" scratch="$2"
    local durable="$STATE_DIR/results/$id"
    [[ -d "$scratch" && ! -L "$scratch" ]] || return 1
    mkdir -p "$durable" || return 1
    local relative source destination destination_dir tmp
    while IFS= read -r relative; do
        source="$scratch/$relative"
        destination="$durable/$relative"
        destination_dir=$(dirname "$destination")
        mkdir -p "$destination_dir" || return 1
        tmp="$destination.tmp.${BASHPID:-$$}.${RANDOM}"
        cp -- "$source" "$tmp" && mv -f -- "$tmp" "$destination" || return 1
    done < <(_coordinator_relative_files "$scratch")
}

_coordinator_append_manifest_file() {
    local manifest="$1" role="$2" remote="$3" local_path="$4"
    local full="$STATE_DIR/$remote" size digest
    [[ -f "$full" && ! -L "$full" ]] || return 1
    size=$(wc -c < "$full") || return 1
    digest=$(_coordinator_hash_file "$full") || return 1
    printf '%s\t%s\t%s\t%s\t%s\n' "$role" "$remote" "$local_path" \
        "$size" "$digest" >> "$manifest"
}

_coordinator_publish_manifest() {
    local tmp="$STATE_DIR/.publication-manifest.tmp.${BASHPID:-$$}.${RANDOM}"
    {
        printf 'schema\t%s\n' "$KUBECTL_COORDINATOR_SCHEMA"
        printf 'attempt_id\t%s\n' "$ATTEMPT_ID"
    } > "$tmp" || return 1
    local id status result_root relative
    while IFS= read -r id; do
        status=$(cat "$STATE_DIR/executions/$id.status") || return 1
        case "$status" in
            PENDING|RUNNING|SUCCESS|FAILED) ;;
            *) return 1 ;;
        esac
        printf 'execution\t%s\t%s\n' "$id" "$status" >> "$tmp" || return 1
        _coordinator_append_manifest_file "$tmp" ledger \
            "executions/$id.status" "executions/$id.status" || return 1
        if [[ "$status" =~ ^(SUCCESS|FAILED)$ ]]; then
            [[ -f "$STATE_DIR/executions/$id.exitcode" ]] || return 1
            _coordinator_append_manifest_file "$tmp" ledger \
                "executions/$id.exitcode" "executions/$id.exitcode" || return 1
        fi
        if [[ -f "$STATE_DIR/executions/$id.workers.tsv" ]]; then
            _coordinator_append_manifest_file "$tmp" ledger \
                "executions/$id.workers.tsv" "executions/$id.workers.tsv" || return 1
        fi
        result_root="$STATE_DIR/results/$id"
        if [[ -d "$result_root" ]]; then
            while IFS= read -r relative; do
                _coordinator_append_manifest_file "$tmp" result \
                    "results/$id/$relative" "$relative" || return 1
            done < <(_coordinator_relative_files "$result_root")
        fi
    done < <(find -P "$CONTROL_DIR/executions" -maxdepth 1 -type f -name '[0-9][0-9][0-9][0-9].sh' \
        -printf '%f\n' | sed 's/\.sh$//' | LC_ALL=C sort)
    _coordinator_append_manifest_file "$tmp" ledger run.status run.status || return 1
    _coordinator_append_manifest_file "$tmp" ledger run-summary.tsv run-summary.tsv || return 1
    [[ ! -f "$STATE_DIR/startup-error.txt" ]] || \
        _coordinator_append_manifest_file "$tmp" ledger startup-error.txt startup-error.txt || return 1
    _coordinator_append_manifest_file "$tmp" snapshot env_used.sh env_used.sh || return 1
    _coordinator_append_manifest_file "$tmp" snapshot env_used.yaml env_used.yaml || return 1
    mv -f -- "$tmp" "$STATE_DIR/publication-manifest.tsv" || return 1
    _coordinator_integration_crash_after after-manifest
}

_coordinator_write_summary() {
    local terminal="$1" failed_id="${2:-}" exit_code="${3:-}"
    local tmp="$STATE_DIR/.run-summary.tmp.${BASHPID:-$$}.${RANDOM}"
    local pending=0 running=0 succeeded=0 failed=0 id value
    while IFS= read -r id; do
        value=$(cat "$STATE_DIR/executions/$id.status" 2>/dev/null || true)
        case "$value" in
            PENDING) pending=$((pending + 1)) ;;
            RUNNING) running=$((running + 1)) ;;
            SUCCESS) succeeded=$((succeeded + 1)) ;;
            FAILED) failed=$((failed + 1)) ;;
            *) _coordinator_error "invalid execution state for summary: $id"; return 1 ;;
        esac
    done < <(find -P "$CONTROL_DIR/executions" -maxdepth 1 -type f -name '[0-9][0-9][0-9][0-9].sh' \
        -printf '%f\n' | sed 's/\.sh$//' | LC_ALL=C sort)
    {
        printf 'schema\t%s\n' "$KUBECTL_COORDINATOR_SCHEMA"
        printf 'attempt_id\t%s\n' "$ATTEMPT_ID"
        printf 'updated_utc\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf 'run_status\t%s\n' "$terminal"
        printf 'pending\t%s\n' "$pending"
        printf 'running\t%s\n' "$running"
        printf 'success\t%s\n' "$succeeded"
        printf 'failed\t%s\n' "$failed"
        [[ -z "$failed_id" ]] || printf 'failed_execution\t%s\n' "$failed_id"
        [[ -z "$exit_code" ]] || printf 'exit_code\t%s\n' "$exit_code"
    } > "$tmp" && mv -f -- "$tmp" "$STATE_DIR/run-summary.tsv"
}

_coordinator_result_publication_hook() {
    local benchmark_rc="$1" scratch="$2" _ignored_durable="$3"
    local id="$ELBENCHO_RUN_EXECUTION_ID"
    if [[ "$benchmark_rc" -eq 0 ]] \
            && ! _coordinator_require_success_artifacts "$id" "$scratch"; then
        benchmark_rc=1
    fi
    _coordinator_copy_scratch_results "$id" "$scratch" || return 1
    _coordinator_integration_crash_after after-copy
    _coordinator_atomic_write "$STATE_DIR/executions/$id.exitcode" "$benchmark_rc" || return 1
    if [[ "$benchmark_rc" -eq 0 ]]; then
        _coordinator_atomic_write "$STATE_DIR/executions/$id.status" SUCCESS || return 1
        _coordinator_write_summary RUNNING || return 1
    else
        _coordinator_atomic_write "$STATE_DIR/executions/$id.status" FAILED || return 1
        _coordinator_write_summary FAILED "$id" "$benchmark_rc" || return 1
    fi
    _coordinator_integration_crash_after after-terminal
    _coordinator_publish_manifest
}

_coordinator_require_success_artifacts() {
    local id="$1" scratch="$2"
    # The pre-existing workload contract identifies completion files only for
    # shared-directory and staged-read modes. Legacy worker-directory output is
    # parsed from Elbencho's regular .out/.csv artifacts and has no synthetic
    # completion sidecar to require here.
    local required=("$scratch/executions/$id.workload.tsv")
    if [[ -n "${ELBENCHO_SWEEP_READ_FROM:-}" \
            && "${ELBENCHO_SINGLE_BIG_FILE:-0}" != 1 ]]; then
        :
    elif [[ "${ELBENCHO_FILE_LAYOUT:-worker-directories}" == shared-directory \
            && -n "${ELBENCHO_FILES_PER_NODE:-}" ]]; then
        required+=("$scratch/executions/$id.write.json")
        [[ "${ELBENCHO_SWEEP_WRITE_ONLY:-0}" == 1 \
            || "${ELBENCHO_SWEEP_WRITE_NO_READ:-0}" == 1 ]] \
            || required+=("$scratch/executions/$id.read.json")
        [[ "${ELBENCHO_SWEEP_WRITE_ONLY:-0}" == 1 ]] \
            || required+=("$scratch/executions/$id.delete.json")
    else
        return 0
    fi
    local artifact
    for artifact in "${required[@]}"; do
        [[ -f "$artifact" && ! -L "$artifact" ]] || {
            _coordinator_error "successful execution $id lacks required artifact ${artifact##*/}"
            return 1
        }
    done
}

_coordinator_finish_prebenchmark_failure() {
    local id="$1" scratch="$2" rc="${3:-1}"
    [[ "$rc" -ne 0 ]] || rc=1
    [[ -d "$scratch" && ! -L "$scratch" ]] \
        && _coordinator_copy_scratch_results "$id" "$scratch" || true
    _coordinator_atomic_write "$STATE_DIR/executions/$id.exitcode" "$rc" || return 1
    _coordinator_atomic_write "$STATE_DIR/executions/$id.status" FAILED || return 1
    _coordinator_write_summary FAILED "$id" "$rc" || return 1
    _coordinator_publish_manifest
}

_coordinator_finish_startup_failure() {
    local message="$1" rc="${2:-1}"
    _coordinator_error "$message"
    _coordinator_atomic_write "$STATE_DIR/startup-error.txt" "$message" || return 1
    _coordinator_atomic_write "$STATE_DIR/run.status" FAILED || return 1
    _coordinator_write_summary FAILED '' "$rc" || return 1
    _coordinator_publish_manifest
}

# A Job signal is best-effort: it cannot make emptyDir scratch durable, but it
# can preserve any already-copied evidence and ensure collection sees a
# terminal attempt rather than an unexplained RUNNING ledger.
_coordinator_signal_handler() {
    local signal_name="$1" rc=143
    [[ "$signal_name" == INT ]] && rc=130
    trap - INT TERM
    if [[ -n "${COORDINATOR_ACTIVE_ID:-}" \
            && "$(cat "$STATE_DIR/executions/$COORDINATOR_ACTIVE_ID.status" 2>/dev/null || true)" == RUNNING ]]; then
        _coordinator_finish_prebenchmark_failure "$COORDINATOR_ACTIVE_ID" \
            "${COORDINATOR_ACTIVE_SCRATCH:-$SCRATCH_DIR}" "$rc" || true
    fi
    _coordinator_atomic_write "$STATE_DIR/run.status" FAILED || true
    _coordinator_write_summary FAILED "${COORDINATOR_ACTIVE_ID:-}" "$rc" || true
    _coordinator_publish_manifest || true
    exit "$rc"
}

_coordinator_check_integration_overlay() {
    [[ -z "${KUBECTL_INTEGRATION_FAILURE_OVERLAY:-}" \
        && -z "${KUBECTL_INTEGRATION_FAKE_ELBENCHO:-}" ]] && return 0
    [[ "${STORAGE_SCALE_TEST_INTEGRATION:-}" == 1 ]] || {
        _coordinator_error "integration-only coordinator hooks are not allowed outside integration tests"
        return 1
    }
    if [[ -n "${KUBECTL_INTEGRATION_FAILURE_OVERLAY:-}" \
            && ( ! -x "$KUBECTL_INTEGRATION_FAILURE_OVERLAY" \
                || -L "$KUBECTL_INTEGRATION_FAILURE_OVERLAY" ) ]]; then
        _coordinator_error "integration failure overlay is not executable"
        return 1
    fi
    if [[ -n "${KUBECTL_INTEGRATION_FAKE_ELBENCHO:-}" \
            && ( ! -x "$KUBECTL_INTEGRATION_FAKE_ELBENCHO" \
                || -L "$KUBECTL_INTEGRATION_FAKE_ELBENCHO" ) ]]; then
        _coordinator_error "integration fake Elbencho is not executable"
        return 1
    fi
    if [[ -n "${KUBECTL_INTEGRATION_CRASH_AFTER:-}" \
            && ! "${KUBECTL_INTEGRATION_CRASH_AFTER}" =~ ^(after-running|after-copy|after-terminal|after-manifest)$ ]]; then
        _coordinator_error "unknown integration crash boundary"
        return 1
    fi
}

_coordinator_integration_crash_after() {
    local boundary="$1"
    [[ "${KUBECTL_INTEGRATION_CRASH_AFTER:-}" == "$boundary" ]] || return 0
    [[ "${STORAGE_SCALE_TEST_INTEGRATION:-}" == 1 ]] || return 1
    # $$ identifies the coordinator process even when this helper is called
    # from a function; BASHPID may instead identify a command substitution.
    kill -KILL "$$"
}

_coordinator_run_overlay() {
    [[ -z "${KUBECTL_INTEGRATION_FAILURE_OVERLAY:-}" ]] && return 0
    "$KUBECTL_INTEGRATION_FAILURE_OVERLAY" "$1" "$2" "$3"
}

_coordinator_run_cell() {
    local id="$1" scratch="$2" durable="$3"
    if [[ -n "${KUBECTL_INTEGRATION_FAKE_ELBENCHO:-}" ]]; then
        local fake_rc=0 publication_rc=0
        "$KUBECTL_INTEGRATION_FAKE_ELBENCHO" "$id" "$scratch" "$durable" \
            || fake_rc=$?
        _coordinator_result_publication_hook "$fake_rc" "$scratch" "$durable" \
            || publication_rc=$?
        [[ "$fake_rc" -eq 0 ]] || return "$fake_rc"
        local committed_status committed_rc
        committed_status=$(cat "$STATE_DIR/executions/$id.status" 2>/dev/null || true)
        committed_rc=$(cat "$STATE_DIR/executions/$id.exitcode" 2>/dev/null || true)
        if [[ "$committed_status" != SUCCESS ]]; then
            [[ "$committed_rc" =~ ^[1-9][0-9]*$ ]] || committed_rc=1
            return "$committed_rc"
        fi
        return "$publication_rc"
    fi
    run_elbencho_cell
}

_coordinator_run_one() {
    local id="$1"
    local definition="$CONTROL_DIR/executions/$id.sh"
    local workers="$STATE_DIR/executions/$id.workers.tsv"
    local scratch="$SCRATCH_DIR/$id/$COORDINATOR_OUTPUT_BASENAME"
    local durable="$STATE_DIR/results/$id"
    local rc=0
    _coordinator_atomic_write "$STATE_DIR/executions/$id.status" RUNNING || return 1
    _coordinator_write_summary RUNNING || return 1
    rm -rf -- "${SCRATCH_DIR:?}/$id"
    mkdir -p "$scratch" "$durable" || return 1
    # shellcheck disable=SC1090  # Digest-verified control bundle definition.
    source "$definition" || {
        _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
        return 1
    }
    [[ "${nodes:-}" =~ ^[1-9][0-9]*$ ]] || {
        _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
        return 1
    }
    _coordinator_integration_crash_after after-running
    if [[ "$nodes" -eq 1 ]]; then
        COORDINATOR_SELECTED_ENDPOINTS=()
        [[ ! -e "$workers" ]] || {
            _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
            return 1
        }
        local worker_tmp="$workers.tmp.${BASHPID:-$$}.${RANDOM}"
        if ! printf '%s\t%s\t%s\t%s\n' \
                "${KUBECTL_COORDINATOR_POD_NODE:-}" \
                "${KUBECTL_COORDINATOR_POD_NAME:-}" \
                "${KUBECTL_COORDINATOR_POD_UID:-}" \
                "${KUBECTL_COORDINATOR_POD_IP:-}" > "$worker_tmp" \
                || ! mv -f -- "$worker_tmp" "$workers"; then
            rm -f -- "$worker_tmp"
            _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
            return 1
        fi
        _coordinator_load_workers "$workers" || {
            _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
            return 1
        }
        COORDINATOR_SELECTED_ENDPOINTS=()
    else
        _coordinator_pick_workers "$id" "$nodes" "$workers" || {
            _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
            return 1
        }
    fi
    if [[ "$nodes" -gt 1 ]] && ! _coordinator_health_hook before-cell; then
        _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1 || return 1
        return 1
    fi
    local test_dirs_csv mapped_read_from=""
    test_dirs_csv=$(kubectl_map_generated_csv "$ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV") || {
        _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
        return 1
    }
    if [[ -n "${ELBENCHO_SWEEP_READ_FROM:-}" ]]; then
        mapped_read_from=$(kubectl_map_read_from_path "$ELBENCHO_SWEEP_READ_FROM") || {
            _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
            return 1
        }
        ELBENCHO_SWEEP_READ_FROM="$mapped_read_from"
        export ELBENCHO_SWEEP_READ_FROM
    fi
    if [[ "$nodes" -eq 1 ]]; then
        elbencho_set_cell_run_context "$id" "$nodes" '' "$test_dirs_csv" \
            "$scratch" "$durable" _coordinator_health_hook \
            _coordinator_result_publication_hook 1 || {
                _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
                return 1
            }
    else
        local hosts_csv
        hosts_csv=$(IFS=,; printf '%s' "${COORDINATOR_SELECTED_ENDPOINTS[*]}")
        elbencho_set_cell_run_context "$id" "$nodes" "$hosts_csv" "$test_dirs_csv" \
            "$scratch" "$durable" _coordinator_health_hook \
            _coordinator_result_publication_hook || {
                _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
                return 1
            }
    fi
    _coordinator_run_overlay "$id" "$scratch" "$durable" || rc=$?
    if [[ "$rc" -eq 0 ]]; then
        _coordinator_run_cell "$id" "$scratch" "$durable" || rc=$?
    fi
    if [[ "$rc" -ne 0 && ! -f "$STATE_DIR/executions/$id.exitcode" ]]; then
        _coordinator_result_publication_hook "$rc" "$scratch" "$durable" || true
    fi
    return "$rc"
}

kubectl_map_generated_csv() {
    local csv="$1" item mapped output=()
    [[ -n "$csv" && "$csv" != ,* && "$csv" != *, && "$csv" != *,,* ]] || return 1
    IFS=, read -ra _coordinator_generated_paths <<< "$csv"
    for item in "${_coordinator_generated_paths[@]}"; do
        mapped=$(kubectl_map_logical_path "$item") || return 1
        output+=("$mapped")
    done
    IFS=, printf '%s' "${output[*]}"
}

_coordinator_finalize_run() {
    local rc="$1" failed_id="${2:-}"
    local final=SUCCESS
    [[ "$rc" -eq 0 ]] || final=FAILED
    _coordinator_atomic_write "$STATE_DIR/run.status" "$final" || return 1
    _coordinator_write_summary "$final" "$failed_id" "$rc" || return 1
    _coordinator_publish_manifest
}

# Print collected-resume decisions for a verified, already-collected ledger.
# The local lifecycle controller is responsible for proving that collection
# occurred before invoking this helper. This program intentionally does not
# reset RUNNING in the remote attempt: that state is crash evidence until the
# collector imports it. Output is machine-readable ID<TAB>SKIP|RUN records.
_coordinator_select_collected_resume() {
    local collected_state="$1" run_status id execution_status
    [[ -d "$collected_state" && ! -L "$collected_state" \
        && -f "$collected_state/run.status" && ! -L "$collected_state/run.status" \
        && -f "$collected_state/publication-manifest.tsv" \
        && ! -L "$collected_state/publication-manifest.tsv" ]] || return 1
    run_status=$(cat "$collected_state/run.status") || return 1
    [[ "$run_status" =~ ^(FAILED|CANCELLED)$ ]] || return 1
    local execution_dir="$collected_state/executions"
    [[ -d "$execution_dir" && ! -L "$execution_dir" ]] || return 1
    for id in $(find -P "$execution_dir" -maxdepth 1 -type f \
            -name '[0-9][0-9][0-9][0-9].status' -printf '%f\n' \
            | sed 's/\.status$//' | LC_ALL=C sort); do
        execution_status=$(cat "$execution_dir/$id.status") || return 1
        case "$execution_status" in
            SUCCESS) printf '%s\tSKIP\n' "$id" ;;
            PENDING|RUNNING|FAILED) printf '%s\tRUN\n' "$id" ;;
            *) return 1 ;;
        esac
    done
}

_coordinator_main() {
    _coordinator_validate_arguments "$@" || return 1
    _coordinator_verify_bundle || return 1
    _coordinator_load_run_metadata || {
        _coordinator_error "invalid run metadata"
        return 1
    }
    # shellcheck disable=SC1091,SC1090  # Digest-verified control bundle snapshot.
    source "$CONTROL_DIR/env_used.sh" || return 1
    # The coordinator runs inside the pinned Elbencho image.  Do not inherit a
    # submitting host path from env_used.sh or allow an integration overlay to
    # replace this production executable.
    export ELBENCHO=/usr/bin/elbencho
    # shellcheck disable=SC1091,SC1090  # Digest-verified Kubernetes coordinator helpers.
    source "$CONTROL_DIR/_nv-elbencho-kubectl-functions.sh" || return 1
    # shellcheck disable=SC1091,SC1090  # Digest-verified, colocated control library.
    source "$CONTROL_DIR/_elbencho_functions.sh" || return 1
    _coordinator_initialize_state || return 1
    _coordinator_validate_endpoint_rows || {
        _coordinator_finish_startup_failure "invalid worker endpoint evidence"
        return 1
    }
    _coordinator_check_integration_overlay || {
        _coordinator_finish_startup_failure "invalid integration-only coordinator hook"
        return 1
    }
    COORDINATOR_SELECTED_ENDPOINTS=("${COORDINATOR_ENDPOINTS[@]}")
    _coordinator_health_hook startup || {
        _coordinator_finish_startup_failure "worker services failed initial health check"
        return 1
    }
    COORDINATOR_SELECTED_ENDPOINTS=()
    _coordinator_atomic_write "$STATE_DIR/run.status" RUNNING || return 1
    _coordinator_write_summary RUNNING || return 1
    COORDINATOR_ACTIVE_ID=""
    COORDINATOR_ACTIVE_SCRATCH=""
    trap '_coordinator_signal_handler INT' INT
    trap '_coordinator_signal_handler TERM' TERM
    local id status rc=0 failed_id=""
    while IFS= read -r id; do
        status=$(cat "$STATE_DIR/executions/$id.status" 2>/dev/null || true)
        case "$status" in
            SUCCESS) continue ;;
            PENDING) ;;
            RUNNING)
                _coordinator_error "refusing to reset interrupted execution $id in this attempt"
                rc=1
                failed_id="$id"
                break
                ;;
            FAILED)
                _coordinator_error "refusing to retry failed execution $id in this attempt"
                rc=1
                failed_id="$id"
                break
                ;;
            *)
                _coordinator_error "invalid execution state: $id=$status"
                rc=1
                failed_id="$id"
                break
                ;;
        esac
        COORDINATOR_ACTIVE_ID="$id"
        COORDINATOR_ACTIVE_SCRATCH="$SCRATCH_DIR/$id/$COORDINATOR_OUTPUT_BASENAME"
        _coordinator_run_one "$id" || rc=$?
        if [[ "$rc" -ne 0 ]]; then
            failed_id="$id"
            break
        fi
        COORDINATOR_ACTIVE_ID=""
        COORDINATOR_ACTIVE_SCRATCH=""
    done < <(find -P "$CONTROL_DIR/executions" -maxdepth 1 -type f -name '[0-9][0-9][0-9][0-9].sh' \
        -printf '%f\n' | sed 's/\.sh$//' | LC_ALL=C sort)
    _coordinator_finalize_run "$rc" "$failed_id" || return 1
    return "$rc"
}

if [[ "${1:-}" == --select-collected-resume ]]; then
    [[ "$#" -eq 2 ]] || {
        _coordinator_error "usage: $KUBECTL_COORDINATOR_BASENAME --select-collected-resume STATE_DIR"
        exit 1
    }
    _coordinator_select_collected_resume "$2"
    exit $?
fi

_coordinator_main "$@"
