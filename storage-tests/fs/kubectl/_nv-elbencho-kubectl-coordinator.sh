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

# Each execution runs in a subshell so its complete group snapshot cannot
# change the parent coordinator's common profile or another group's settings.
# shellcheck disable=SC2030,SC2031
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

_coordinator_execution_ids() {
    local directory="$1" suffix="$2" path name
    [[ -d "$directory" && ! -L "$directory" \
        && "$suffix" =~ ^\.[a-z]+$ ]] || return 1
    local -a ids=()
    for path in "$directory"/[0-9][0-9][0-9][0-9]"$suffix"; do
        [[ -f "$path" && ! -L "$path" ]] || continue
        name=${path##*/}
        name=${name%"$suffix"}
        [[ "$name" =~ ^[0-9]{4}$ ]] || return 1
        ids+=("$name")
    done
    [[ ${#ids[@]} -gt 0 ]] || return 0
    printf '%s\n' "${ids[@]}" | LC_ALL=C sort
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
    # Enumeration must never pick up an execution omitted from the verified
    # bundle. Reject unlisted files rather than treating directory contents as
    # another source of execution membership.
    local bundle_files
    bundle_files=$(_coordinator_relative_files "$CONTROL_DIR") || return 1
    while IFS= read -r relative; do
        [[ "$relative" == bundle-manifest.tsv || -v seen["$relative"] ]] || return 1
    done <<< "$bundle_files"
    if [[ -v seen[batch-manifest.tsv] ]]; then
        [[ -v seen[_batch_functions.sh] && -v seen[batch-sealed.sha256] ]] || return 1
        # shellcheck disable=SC1090,SC1091  # The library digest was checked above.
        source "$CONTROL_DIR/_batch_functions.sh" || return 1
        elbencho_batch_verify_manifest "$CONTROL_DIR" subset || return 1
        for relative in "${!seen[@]}"; do
            [[ "$relative" == executions/[0-9][0-9][0-9][0-9].sh ]] || continue
            local execution_id="${relative#executions/}" expected_output expected_group expected_kind
            execution_id="${execution_id%.sh}"
            expected_output=$(elbencho_batch_execution_output_dir "$CONTROL_DIR" "$execution_id") || return 1
            expected_output="${expected_output#"$CONTROL_DIR"/}"
            expected_group="${expected_output#groups/}"
            expected_group="${expected_group%%/*}"
            expected_kind=$(awk -F '\t' -v id="$expected_group" '$1 == "group" && $2 == id {print $3}' "$CONTROL_DIR/batch-manifest.tsv") || return 1
            (
                unset ELBENCHO_BATCH_GROUP_ID ELBENCHO_BATCH_OUTPUT_RELATIVE ELBENCHO_EXECUTION_KIND
                # shellcheck disable=SC1090  # The execution digest was checked above.
                source "$CONTROL_DIR/$relative" || exit 1
                [[ "${ELBENCHO_BATCH_GROUP_ID:-}" == "$expected_group" \
                    && "${ELBENCHO_BATCH_OUTPUT_RELATIVE:-}" == "$expected_output" \
                    && "${ELBENCHO_EXECUTION_KIND:-}" == "$expected_kind" ]]
            ) || return 1
        done
    fi
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
                    && "$value" =~ ^(elbencho|mdtest-elbencho|filesystem-batch)-[0-9]{8}Z[0-9]{6}$ ]] || return 1
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
    local coordinator_mount=/mnt/storage-scale-test
    if [[ "${STORAGE_SCALE_TEST_INTEGRATION:-}" == 1 \
            && -n "${KUBECTL_INTEGRATION_PVC_ROOT:-}" ]]; then
        coordinator_mount="$KUBECTL_INTEGRATION_PVC_ROOT"
    fi
    local run_root="${CONTROL_DIR%/control}"
    COORDINATOR_CONTROL_ROOT="${run_root%/runs/"$ATTEMPT_ID"}"
    COORDINATOR_TEST_ROOT="${COORDINATOR_CONTROL_ROOT%/.storage-scale-test}"
    if [[ "${STORAGE_SCALE_TEST_INTEGRATION:-}" != 1 ]]; then
        [[ "$run_root" == "$COORDINATOR_CONTROL_ROOT/runs/$ATTEMPT_ID" \
            && "$COORDINATOR_CONTROL_ROOT" == "$COORDINATOR_TEST_ROOT/.storage-scale-test" \
            && "$COORDINATOR_TEST_ROOT" == "$coordinator_mount/"* \
            && "$SCRATCH_DIR" == "/tmp/storage-scale-test/$ATTEMPT_ID" ]] || {
                _coordinator_error "control or scratch path is outside the Kubernetes attempt layout"
                return 1
            }
    fi
}

_coordinator_validate_state_tree() {
    if [[ -e "$STATE_DIR" ]] \
            && find -P "$STATE_DIR" -type l -print -quit | grep -q .; then
        _coordinator_error "attempt state contains a symlink"
        return 1
    fi
    mkdir -p "$STATE_DIR/executions" "$STATE_DIR/results" || return 1
}

_coordinator_set_control_layout() {
    if [[ "${STORAGE_SCALE_TEST_INTEGRATION:-}" == 1 ]]; then
        local logical_root mapped_root
        kubectl_select_control_root logical_root mapped_root || return 1
        kubectl_set_control_layout "$logical_root" "$mapped_root"
        return
    fi
    KUBECTL_CONTROL_TEST_ROOT="$COORDINATOR_TEST_ROOT"
    KUBECTL_CONTROL_ROOT="$COORDINATOR_CONTROL_ROOT"
    KUBECTL_CONTROL_LOGICAL_ROOT="${COORDINATOR_TEST_ROOT#"$KUBECTL_SWEEP_MOUNT_ROOT"/}"
    export KUBECTL_CONTROL_TEST_ROOT KUBECTL_CONTROL_ROOT \
        KUBECTL_CONTROL_LOGICAL_ROOT
    kubectl_validate_saved_control_layout
}

_coordinator_initialize_snapshots() {
    local allow_create="${1:-1}" snapshot
    [[ "$allow_create" =~ ^[01]$ ]] || return 1
    local -a snapshots=(env_used.sh env_used.yaml)
    if [[ -f "$CONTROL_DIR/batch-manifest.tsv" ]]; then
        snapshots+=(batch-manifest.tsv batch-sealed.sha256)
        local relative
        while IFS= read -r relative; do
            snapshots+=("$relative/env_used.sh" "$relative/env_used.yaml")
        done < <(awk -F '\t' '$1 == "group" {print $4}' "$CONTROL_DIR/batch-manifest.tsv")
    fi
    for snapshot in "${snapshots[@]}"; do
        if [[ ! -e "$STATE_DIR/$snapshot" ]]; then
            [[ "$allow_create" -eq 1 ]] || return 1
            mkdir -p -- "$(dirname "$STATE_DIR/$snapshot")" || return 1
            cp -- "$CONTROL_DIR/$snapshot" "$STATE_DIR/$snapshot" || return 1
        elif [[ ! -f "$STATE_DIR/$snapshot" || -L "$STATE_DIR/$snapshot" ]] \
                || ! cmp -s -- "$CONTROL_DIR/$snapshot" "$STATE_DIR/$snapshot"; then
            _coordinator_error "existing working snapshot differs: $snapshot"
            return 1
        fi
    done
}

_coordinator_initialize_execution_states() {
    local allow_create="${1:-1}" definition id state_file
    [[ "$allow_create" =~ ^[01]$ ]] || return 1
    for definition in "$CONTROL_DIR"/executions/[0-9][0-9][0-9][0-9].sh; do
        [[ -f "$definition" ]] || continue
        id=$(basename "$definition" .sh)
        state_file="$STATE_DIR/executions/$id.status"
        if [[ ! -e "$state_file" ]]; then
            [[ "$allow_create" -eq 1 ]] || return 1
            _coordinator_atomic_write "$state_file" PENDING || return 1
        elif [[ ! -f "$state_file" || -L "$state_file" ]]; then
            _coordinator_error "execution state is not a regular file: $id"
            return 1
        fi
    done
}

_coordinator_initialize_state() {
    _coordinator_validate_state_tree || return 1
    [[ ! -e "$STATE_DIR/coordinator.lock" ]] || {
        _coordinator_error "another coordinator owns this attempt"
        return 1
    }
    if ! mkdir "$STATE_DIR/coordinator.lock"; then
        _coordinator_error "unable to acquire coordinator lock"
        return 1
    fi
    local owner_tmp="$STATE_DIR/coordinator.lock/.owner.tmp.${BASHPID:-$$}.${RANDOM}"
    {
        printf 'schema\t%s\n' "$KUBECTL_COORDINATOR_SCHEMA"
        printf 'attempt_id\t%s\n' "$ATTEMPT_ID"
        printf 'pid\t%s\n' "${BASHPID:-$$}"
        printf 'started_utc\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$owner_tmp" \
        && mv -- "$owner_tmp" "$STATE_DIR/coordinator.lock/owner.tsv" || return 1
    _coordinator_integration_crash_after after-lock
    _coordinator_initialize_snapshots || return 1
    _coordinator_integration_crash_after after-snapshots
    _coordinator_initialize_execution_states || return 1
    _coordinator_integration_crash_after after-execution-state
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
    # Keep socket code in this file. Some constrained hosts terminate socket
    # programs carried as inline shell text even though ordinary Pod traffic
    # is allowed.
    timeout --kill-after=2s 5s bash "$0" --probe-endpoint "$endpoint"
}

_coordinator_probe_endpoint_request() {
    local endpoint="$1" response
    _coordinator_valid_ipv4 "$endpoint" || return 1
    exec 3<>"/dev/tcp/$endpoint/1611"
    printf 'GET /status HTTP/1.0\r\nHost: %s:1611\r\n\r\n' "$endpoint" >&3
    IFS= read -r response <&3
    exec 3>&-
    [[ "$response" =~ ^HTTP/[0-9.]+[[:space:]]200([[:space:]]|$) ]]
}

_coordinator_health_hook() {
    local phase_name="$1"
    local endpoint attempt healthy
    for endpoint in "${COORDINATOR_SELECTED_ENDPOINTS[@]}"; do
        healthy=0
        for attempt in 1 2 3; do
            if _coordinator_probe_endpoint "$endpoint"; then
                healthy=1
                break
            fi
            if ((attempt < 3)); then
                printf 'Warning: worker endpoint %s probe %s/3 failed after %s; retrying\n' \
                    "$endpoint" "$attempt" "$phase_name" >&2
                sleep 1 || return 1
            fi
        done
        if ((healthy == 0)); then
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
    local relative source destination destination_dir tmp scratch_files
    scratch_files=$(_coordinator_relative_files "$scratch") || return 1
    [[ -n "$scratch_files" ]] || return 0
    while IFS= read -r relative; do
        source="$scratch/$relative"
        destination="$durable/$relative"
        destination_dir=$(dirname "$destination")
        mkdir -p "$destination_dir" || return 1
        tmp="$destination.tmp.${BASHPID:-$$}.${RANDOM}"
        cp -- "$source" "$tmp" && mv -f -- "$tmp" "$destination" || return 1
    done <<< "$scratch_files"
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
    local id status result_root relative output_relative
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
        output_relative=""
        if [[ -f "$CONTROL_DIR/batch-manifest.tsv" ]]; then
            output_relative=$(elbencho_batch_execution_output_dir "$CONTROL_DIR" "$id") || return 1
            output_relative="${output_relative#"$CONTROL_DIR"/}/"
        fi
        if [[ -d "$result_root" ]]; then
            local result_files
            result_files=$(_coordinator_relative_files "$result_root") || return 1
            [[ -n "$result_files" ]] || continue
            while IFS= read -r relative; do
                _coordinator_append_manifest_file "$tmp" result \
                    "results/$id/$relative" "$output_relative$relative" || return 1
            done <<< "$result_files"
        fi
    done < <(_coordinator_execution_ids "$CONTROL_DIR/executions" .sh)
    _coordinator_append_manifest_file "$tmp" ledger run.status run.status || return 1
    _coordinator_append_manifest_file "$tmp" ledger run-summary.tsv run-summary.tsv || return 1
    [[ ! -f "$STATE_DIR/startup-error.txt" ]] || \
        _coordinator_append_manifest_file "$tmp" ledger startup-error.txt startup-error.txt || return 1
    [[ ! -f "$STATE_DIR/coordinator-loss.tsv" ]] || \
        _coordinator_append_manifest_file "$tmp" ledger coordinator-loss.tsv coordinator-loss.tsv || return 1
    _coordinator_append_manifest_file "$tmp" snapshot env_used.sh env_used.sh || return 1
    _coordinator_append_manifest_file "$tmp" snapshot env_used.yaml env_used.yaml || return 1
    if [[ -f "$CONTROL_DIR/batch-manifest.tsv" ]]; then
        _coordinator_append_manifest_file "$tmp" snapshot batch-manifest.tsv batch-manifest.tsv || return 1
        _coordinator_append_manifest_file "$tmp" snapshot batch-sealed.sha256 batch-sealed.sha256 || return 1
        while IFS= read -r relative; do
            _coordinator_append_manifest_file "$tmp" snapshot \
                "$relative/env_used.sh" "$relative/env_used.sh" || return 1
            _coordinator_append_manifest_file "$tmp" snapshot \
                "$relative/env_used.yaml" "$relative/env_used.yaml" || return 1
        done < <(awk -F '\t' '$1 == "group" {print $4}' "$CONTROL_DIR/batch-manifest.tsv")
    fi
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
    done < <(_coordinator_execution_ids "$CONTROL_DIR/executions" .sh)
    if [[ "$terminal" =~ ^(SUCCESS|FAILED|CANCELLED)$ && "$running" -ne 0 ]]; then
        _coordinator_error "refusing terminal attempt publication with RUNNING executions"
        return 1
    fi
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
    local overlay_rc=0
    if [[ "$benchmark_rc" -eq 0 ]]; then
        _coordinator_run_overlay after-benchmark "$id" "$scratch" \
            "$_ignored_durable" || overlay_rc=$?
        [[ "$overlay_rc" -eq 0 ]] || benchmark_rc="$overlay_rc"
    fi
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
    _coordinator_publish_manifest || return 1
    return "$benchmark_rc"
}

_coordinator_require_success_artifacts() {
    local id="$1" scratch="$2"
    if [[ "${ELBENCHO_EXECUTION_KIND:-io}" == mdtest ]]; then
        local iteration stem
        for ((iteration = 1; iteration <= MDTEST_ITERATIONS; iteration++)); do
            # shellcheck disable=SC2154  # Sourced from the verified MD execution definition.
            stem=$(printf '%s/mdtest-elbencho-c_%03d-t_%03d_%s_iter%d' \
                "$scratch" "$nodes" "$tasks_per_node" \
                "${COORDINATOR_OUTPUT_BASENAME##*-}" "$iteration")
            if [[ ! -s "$stem.out" || -L "$stem.out" \
                    || ! -s "$stem.csv" || -L "$stem.csv" ]]; then
                _coordinator_error "successful MD execution $id lacks iteration $iteration results"
                return 1
            fi
        done
        local completion_file
        completion_file=$(_mdtest_completion_path "$scratch" "$id") || return 1
        if ! _mdtest_completion_is_valid "$completion_file"; then
            _coordinator_error "successful MD execution $id lacks completion evidence"
            return 1
        fi
        return 0
    fi
    # The pre-existing workload contract identifies completion files only for
    # shared-directory and staged-read modes. Legacy worker-directory output is
    # parsed from Elbencho's regular .out/.csv artifacts and has no synthetic
    # completion sidecar to require here.
    local required=()
    if [[ -n "${ELBENCHO_SWEEP_READ_FROM:-}" \
            && "${ELBENCHO_SINGLE_BIG_FILE:-0}" != 1 ]]; then
        required+=("$scratch/executions/$id.workload.tsv")
    elif [[ "${ELBENCHO_FILE_LAYOUT:-worker-directories}" == shared-directory \
            && -n "${ELBENCHO_FILES_PER_NODE:-}" ]]; then
        required+=("$scratch/executions/$id.workload.tsv" \
            "$scratch/executions/$id.write.json")
        [[ "${ELBENCHO_SWEEP_WRITE_ONLY:-0}" == 1 \
            || "${ELBENCHO_SWEEP_WRITE_NO_READ:-0}" == 1 ]] \
            || required+=("$scratch/executions/$id.read.json")
        [[ "${ELBENCHO_SWEEP_WRITE_ONLY:-0}" == 1 ]] \
            || required+=("$scratch/executions/$id.delete.json")
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
    if [[ -d "$scratch" && ! -L "$scratch" ]] \
            && ! _coordinator_copy_scratch_results "$id" "$scratch"; then
        _coordinator_error "could not preserve partial scratch results for execution $id"
    fi
    _coordinator_atomic_write "$STATE_DIR/executions/$id.exitcode" "$rc" || return 1
    _coordinator_atomic_write "$STATE_DIR/executions/$id.status" FAILED || return 1
    _coordinator_write_summary FAILED "$id" "$rc" || return 1
    _coordinator_publish_manifest
}

_coordinator_finish_startup_failure() {
    local message="$1" rc="${2:-1}"
    _coordinator_error "$message"
    _coordinator_atomic_write "$STATE_DIR/startup-error.txt" "$message" || return 1
    _coordinator_write_summary FAILED '' "$rc" || return 1
    _coordinator_atomic_write "$STATE_DIR/run.status" FAILED || return 1
    _coordinator_publish_manifest
}

# A Job signal is best-effort: it cannot make emptyDir scratch durable, but it
# can preserve any already-copied evidence and ensure collection sees a
# terminal attempt rather than an unexplained RUNNING ledger.
_coordinator_signal_handler() {
    local signal_name="$1" rc=143
    [[ "$signal_name" == INT ]] && rc=130
    trap - INT TERM
    if [[ -n "${COORDINATOR_ACTIVE_PID:-}" ]]; then
        kill -TERM "$COORDINATOR_ACTIVE_PID" 2>/dev/null || true
        wait "$COORDINATOR_ACTIVE_PID" 2>/dev/null || true
    fi
    if [[ -n "${COORDINATOR_ACTIVE_ID:-}" \
            && "$(cat "$STATE_DIR/executions/$COORDINATOR_ACTIVE_ID.status" 2>/dev/null || true)" == RUNNING ]]; then
        _coordinator_finish_prebenchmark_failure "$COORDINATOR_ACTIVE_ID" \
            "$COORDINATOR_ACTIVE_SCRATCH" "$rc" || {
                _coordinator_error "cannot finalize interrupted execution $COORDINATOR_ACTIVE_ID"
                exit "$rc"
            }
    fi
    _coordinator_finalize_run "$rc" "${COORDINATOR_ACTIVE_ID:-}" || \
        _coordinator_error "cannot finalize signalled attempt; durable state retained for recovery"
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
            && ! "${KUBECTL_INTEGRATION_CRASH_AFTER}" =~ ^(after-lock|after-snapshots|after-execution-state|after-run-status|after-running|after-copy|after-terminal|after-manifest)$ ]]; then
        _coordinator_error "unknown integration crash boundary"
        return 1
    fi
}

_coordinator_integration_crash_after() {
    local boundary="$1"
    [[ "${KUBECTL_INTEGRATION_CRASH_AFTER:-}" == "$boundary" ]] || return 0
    [[ "${STORAGE_SCALE_TEST_INTEGRATION:-}" == 1 ]] || return 1
    # A cell runs in an isolated process. Kill it with the coordinator so no
    # child can publish beyond the injected crash boundary.
    kill -KILL "$$" "$BASHPID"
}

_coordinator_run_overlay() {
    [[ -z "${KUBECTL_INTEGRATION_FAILURE_OVERLAY:-}" ]] && return 0
    local boundary="$1"
    shift
    "$KUBECTL_INTEGRATION_FAILURE_OVERLAY" "$@" "$boundary"
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
    if [[ "${ELBENCHO_EXECUTION_KIND:-io}" == mdtest ]]; then
        local benchmark_rc=0 publication_rc=0
        # shellcheck disable=SC2034  # Read through dynamic scope by the metadata runner.
        local output_dir="$scratch"
        run_elbencho_metadata_benchmark || benchmark_rc=$?
        _coordinator_result_publication_hook "$benchmark_rc" "$scratch" "$durable" \
            || publication_rc=$?
        [[ "$benchmark_rc" -eq 0 ]] || return "$benchmark_rc"
        return "$publication_rc"
    fi
    run_elbencho_cell
}

_coordinator_validate_mdtest_definition() {
    [[ "${tasks_per_node:-}" =~ ^[1-9][0-9]*$ \
        && "${MDTEST_ITERATIONS:-}" =~ ^[1-9][0-9]*$ \
        && "${MDTEST_BRANCH_FACTOR:-}" =~ ^[1-9][0-9]*$ \
        && "${MDTEST_ITEMS_PER_DIR:-}" =~ ^[1-9][0-9]*$ ]] || return 1
    case "${MDTEST_LAYOUT:-}" in
        standard) return 0 ;;
        single-dir)
            [[ "${MDTEST_SINGLE_DIR_TARGET_FILES:-}" =~ ^[1-9][0-9]*$ \
                && "${MDTEST_SINGLE_DIR_FILES_PER_WORKER:-}" =~ ^[1-9][0-9]*$ ]]
            ;;
        *) return 1 ;;
    esac
}

_coordinator_validate_mdtest_targets() {
    local id="$1" csv="$2" target root mapped_root basename index
    local expected_suffix="${ELBENCHO_RUN_TEST_DIR_SUFFIX:-}"
    [[ "$expected_suffix" =~ ^-[0-9]{8}Z[0-9]{6}-e${id}$ ]] || return 1
    local -a targets=()
    IFS=, read -ra targets <<< "$csv"
    [[ ${#targets[@]} -gt 0 ]] || return 1
    for target in "${targets[@]}"; do
        basename="${target##*/}"
        [[ "$basename" == mdtest-elbencho-target-*"$expected_suffix" ]] || return 1
        index="${basename#mdtest-elbencho-target-}"
        index="${index%"$expected_suffix"}"
        [[ "$index" =~ ^[1-9][0-9]*$ ]] || return 1
        local matches=0
        for root in "${!TEST_DIRS[@]}"; do
            mapped_root=$(kubectl_map_logical_path "$root") || return 1
            [[ "${target%/*}" == "$mapped_root" ]] && matches=$((matches + 1))
        done
        [[ "$matches" -eq 1 ]] || return 1
    done
}

_coordinator_prepare_active_execution() {
    local id="$1" output_basename="$COORDINATOR_OUTPUT_BASENAME" group_output
    if [[ -f "$CONTROL_DIR/batch-manifest.tsv" ]]; then
        group_output=$(elbencho_batch_execution_output_dir "$CONTROL_DIR" "$id") || return 1
        output_basename="${group_output##*/}"
    fi
    COORDINATOR_ACTIVE_ID="$id"
    COORDINATOR_ACTIVE_SCRATCH="$SCRATCH_DIR/$id/$output_basename"
    # The parent must own both the path and RUNNING checkpoint before dispatch.
    # Child-local assignments cannot inform signal or abnormal-exit recovery.
    _coordinator_atomic_write "$STATE_DIR/executions/$id.status" RUNNING || return 1
    _coordinator_write_summary RUNNING
}

_coordinator_complete_active_execution() {
    local rc="$1" id="$COORDINATOR_ACTIVE_ID" status
    status=$(cat "$STATE_DIR/executions/$id.status") || return 1
    case "$status" in
        RUNNING)
            [[ "$rc" -ne 0 ]] || rc=1
            _coordinator_error "execution $id exited before terminal cell publication (rc=$rc)"
            _coordinator_finish_prebenchmark_failure "$id" "$COORDINATOR_ACTIVE_SCRATCH" "$rc" \
                || return 1
            ;;
        SUCCESS) ;;
        FAILED)
            if [[ "$rc" -eq 0 ]]; then
                rc=$(cat "$STATE_DIR/executions/$id.exitcode") || return 1
                [[ "$rc" =~ ^[1-9][0-9]*$ ]] || return 1
            fi
            ;;
        *) _coordinator_error "invalid execution state after child exit: $id=$status"; return 1 ;;
    esac
    return "$rc"
}

_coordinator_run_one() (
    local id="$1"
    local definition="$CONTROL_DIR/executions/$id.sh"
    local workers="$STATE_DIR/executions/$id.workers.tsv"
    local scratch="$COORDINATOR_ACTIVE_SCRATCH"
    COORDINATOR_OUTPUT_BASENAME="${scratch##*/}"
    local durable="$STATE_DIR/results/$id"
    local rc=0
    local runtime_overlay="${KUBECTL_INTEGRATION_FAILURE_OVERLAY:-}"
    local runtime_fake="${KUBECTL_INTEGRATION_FAKE_ELBENCHO:-}"
    COORDINATOR_ACTIVE_PID=""
    trap '_coordinator_signal_handler INT' INT
    trap '_coordinator_signal_handler TERM' TERM
    unset ELBENCHO_EXECUTION_KIND tasks_per_node MDTEST_LAYOUT
    unset MDTEST_SINGLE_DIR_TARGET_FILES MDTEST_SINGLE_DIR_FILES_PER_WORKER
    unset MDTEST_BRANCH_FACTOR MDTEST_ITEMS_PER_DIR MDTEST_ITERATIONS
    # shellcheck disable=SC1090  # Digest-verified control bundle definition.
    source "$definition" || {
        _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
        return 1
    }
    # Group snapshots carry the submitting host's executable path. Workloads
    # always execute the binary provided by the pinned coordinator image.
    export ELBENCHO=/usr/bin/elbencho
    # Bundle preparation relocates an integration overlay into this attempt,
    # and resume removes it. Saved group snapshots contain launch-host paths,
    # so retain only the already validated attempt-level runtime hooks.
    unset KUBECTL_INTEGRATION_FAILURE_OVERLAY KUBECTL_INTEGRATION_FAKE_ELBENCHO
    [[ -z "$runtime_overlay" ]] || export KUBECTL_INTEGRATION_FAILURE_OVERLAY="$runtime_overlay"
    [[ -z "$runtime_fake" ]] || export KUBECTL_INTEGRATION_FAKE_ELBENCHO="$runtime_fake"
    rm -rf -- "${SCRATCH_DIR:?}/$id"
    mkdir -p "$scratch" "$durable" || return 1
    COORDINATOR_ACTIVE_SCRATCH="$scratch"
    [[ "${nodes:-}" =~ ^[1-9][0-9]*$ ]] || {
        _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
        return 1
    }
    case "${ELBENCHO_EXECUTION_KIND:-io}" in
        io) ;;
        mdtest)
            _coordinator_validate_mdtest_definition || {
                _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
                return 1
            }
            ;;
        *)
            _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
            return 1
            ;;
    esac
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
    if [[ "${ELBENCHO_EXECUTION_KIND:-io}" == mdtest ]] \
            && ! _coordinator_validate_mdtest_targets "$id" "$test_dirs_csv"; then
        _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
        return 1
    fi
    # Reified generated-target ownership is expressed in user-facing logical
    # paths. The shared workload's deletion safeguards consult these captured
    # values directly, so freeze their Kubernetes mappings before any phase.
    ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV="$test_dirs_csv"
    if [[ -n "${ELBENCHO_RUN_GENERATED_TEST_ROOT:-}" ]]; then
        ELBENCHO_RUN_GENERATED_TEST_ROOT=$(
            kubectl_map_logical_path "$ELBENCHO_RUN_GENERATED_TEST_ROOT"
        ) || {
            _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
            return 1
        }
        _coordinator_validate_pvc_path "$ELBENCHO_RUN_GENERATED_TEST_ROOT" || {
            _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
            return 1
        }
    fi
    export ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV ELBENCHO_RUN_GENERATED_TEST_ROOT
    if [[ "${ELBENCHO_EXECUTION_KIND:-io}" == io \
            && -n "${ELBENCHO_SWEEP_READ_FROM:-}" ]]; then
        mapped_read_from=$(kubectl_map_read_from_path "$ELBENCHO_SWEEP_READ_FROM") || {
            _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
            return 1
        }
        ELBENCHO_SWEEP_READ_FROM="$mapped_read_from"
        export ELBENCHO_SWEEP_READ_FROM
        # The many-file tree cache creates a staging file in the dataset's
        # parent before publishing beneath the dataset. Validate both derived
        # locations against live PVC symlinks immediately before workload IO.
        if ! _coordinator_validate_pvc_path "$ELBENCHO_SWEEP_READ_FROM" \
                || ! _coordinator_validate_pvc_path \
                    "$(dirname "$ELBENCHO_SWEEP_READ_FROM")"; then
            _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
            return 1
        fi
    fi
    if [[ "${ELBENCHO_EXECUTION_KIND:-io}" == mdtest ]]; then
        _elbencho_set_metadata_core_context "$id" "$scratch" || {
            _coordinator_finish_prebenchmark_failure "$id" "$scratch" 1
            return 1
        }
        ELBENCHO_RUN_NODE_COUNT="$nodes"
        ELBENCHO_RUN_HOSTS_CSV=""
        if [[ "$nodes" -gt 1 ]]; then
            ELBENCHO_RUN_HOSTS_CSV=$(IFS=,; printf '%s' "${COORDINATOR_SELECTED_ENDPOINTS[*]}")
        fi
        ELBENCHO_RUN_REMOTE_OUTPUT_DIR="$scratch"
        ELBENCHO_RUN_TEST_DIRS_CSV="$test_dirs_csv"
        ELBENCHO_RUN_COORDINATOR_LOCAL=0
        [[ "$nodes" -ne 1 ]] || ELBENCHO_RUN_COORDINATOR_LOCAL=1
        export ELBENCHO_RUN_NODE_COUNT \
            ELBENCHO_RUN_HOSTS_CSV ELBENCHO_RUN_REMOTE_OUTPUT_DIR \
            ELBENCHO_RUN_TEST_DIRS_CSV ELBENCHO_RUN_COORDINATOR_LOCAL
        test_dirs_csv="$ELBENCHO_RUN_TEST_DIRS_CSV"
    elif [[ "$nodes" -eq 1 ]]; then
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
    _coordinator_run_overlay before-benchmark "$id" "$scratch" "$durable" \
        || rc=$?
    if [[ "$rc" -eq 0 ]]; then
        _coordinator_run_cell "$id" "$scratch" "$durable" || rc=$?
    fi
    if [[ "$rc" -ne 0 && ! -f "$STATE_DIR/executions/$id.exitcode" ]]; then
        _coordinator_result_publication_hook "$rc" "$scratch" "$durable" || true
    fi
    return "$rc"
)

kubectl_map_generated_csv() {
    local csv="$1" item mapped output=()
    [[ -n "$csv" && "$csv" != ,* && "$csv" != *, && "$csv" != *,,* ]] || return 1
    IFS=, read -ra _coordinator_generated_paths <<< "$csv"
    for item in "${_coordinator_generated_paths[@]}"; do
        mapped=$(kubectl_map_logical_path "$item") || return 1
        _coordinator_validate_pvc_path "$mapped" || return 1
        output+=("$mapped")
    done
    # Set IFS before expanding the array, not as a command-local assignment.
    local IFS=,
    printf '%s' "${output[*]}"
}

_coordinator_validate_pvc_path() {
    local candidate="$1" root_real control_real resolved
    [[ ( "$candidate" == "$KUBECTL_SWEEP_MOUNT_ROOT" \
            || "$candidate" == "$KUBECTL_SWEEP_MOUNT_ROOT/"* ) \
        && "$candidate" != *$'\n'* && "$candidate" != *$'\r'* \
        && "$candidate" != *$'\t'* ]] || return 1
    root_real=$(realpath -e -- "$KUBECTL_SWEEP_MOUNT_ROOT") || return 1
    control_real=$(realpath -m -- "$KUBECTL_CONTROL_ROOT") || return 1
    resolved=$(realpath -m -- "$candidate") || return 1
    [[ ( "$resolved" == "$root_real" || "$resolved" == "$root_real/"* ) \
        && "$resolved" != "$control_real" \
        && "$resolved" != "$control_real/"* ]] || {
            _coordinator_error "workload path escapes the mounted PVC: $candidate"
            return 1
        }
}

_coordinator_finalize_run() {
    local rc="$1" failed_id="${2:-}"
    local final=SUCCESS
    [[ "$rc" -eq 0 ]] || final=FAILED
    if [[ "$final" == FAILED && ( -z "$failed_id" \
            || "$(cat "$STATE_DIR/executions/$failed_id.status" 2>/dev/null)" != FAILED ) ]]; then
        # Infrastructure failure after a successful checkpoint must preserve
        # that cell, but still supply attempt-level failure evidence to collect.
        _coordinator_atomic_write "$STATE_DIR/startup-error.txt" \
            "coordinator interrupted outside a failed cell (execution=${failed_id:-none}, rc=$rc)" \
            || return 1
        failed_id=""
    fi
    _coordinator_write_summary "$final" "$failed_id" "$rc" || return 1
    _coordinator_atomic_write "$STATE_DIR/run.status" "$final" || return 1
    _coordinator_publish_manifest
}

_coordinator_repair_terminal_publication() {
    local terminal="$1" status_file id status failed_id="" exit_code=0
    local pending=0 failed=0
    [[ "$terminal" =~ ^(SUCCESS|FAILED|CANCELLED)$ ]] || return 1
    for status_file in "$STATE_DIR"/executions/[0-9][0-9][0-9][0-9].status; do
        [[ -f "$status_file" && ! -L "$status_file" ]] || continue
        id=$(basename "$status_file" .status)
        status=$(cat "$status_file") || return 1
        case "$status" in
            SUCCESS) ;;
            PENDING) pending=$((pending + 1)) ;;
            FAILED)
                failed=$((failed + 1))
                if [[ -z "$failed_id" ]]; then
                    failed_id="$id"
                    exit_code=$(cat "$STATE_DIR/executions/$id.exitcode" 2>/dev/null || true)
                    [[ "$exit_code" =~ ^[1-9][0-9]*$ ]] || return 1
                fi
                ;;
            RUNNING|*) return 1 ;;
        esac
    done
    case "$terminal" in
        SUCCESS) [[ "$pending" -eq 0 && "$failed" -eq 0 ]] || return 1 ;;
        FAILED)
            if [[ "$failed" -eq 0 ]]; then
                [[ -f "$STATE_DIR/startup-error.txt" \
                    && ! -L "$STATE_DIR/startup-error.txt" ]] || return 1
                exit_code=1
            fi
            ;;
        CANCELLED) exit_code=143 ;;
    esac
    _coordinator_write_summary "$terminal" "$failed_id" "$exit_code" || return 1
    _coordinator_publish_manifest
}

_coordinator_ensure_recovery_lock() {
    local observed_run_status="$1" lock="$STATE_DIR/coordinator.lock"
    if [[ ! -e "$lock" && ! -L "$lock" ]]; then
        [[ "$observed_run_status" == PREPARED ]] || return 1
        mkdir "$lock" || return 1
    fi
    [[ -d "$lock" && ! -L "$lock" ]] || return 1
    local owner="$lock/owner.tsv"
    if [[ ! -e "$owner" && ! -L "$owner" ]]; then
        [[ "$observed_run_status" == PREPARED ]] || return 1
        local tmp="$lock/.owner.recovery.tmp.${BASHPID:-$$}.${RANDOM}"
        {
            printf 'schema\t%s\n' "$KUBECTL_COORDINATOR_SCHEMA"
            printf 'attempt_id\t%s\n' "$ATTEMPT_ID"
            printf 'recovered\ttrue\n'
            printf 'started_utc\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        } > "$tmp" && mv -- "$tmp" "$owner" || return 1
    fi
    [[ -f "$owner" && ! -L "$owner" \
        && $(awk -F '\t' '$1 == "schema" {print $2}' "$owner") \
            == "$KUBECTL_COORDINATOR_SCHEMA" \
        && $(awk -F '\t' '$1 == "attempt_id" {print $2}' "$owner") \
            == "$ATTEMPT_ID" ]]
}

_coordinator_recover_lost() {
    _coordinator_validate_arguments "$@" || return 1
    _coordinator_verify_bundle || return 1
    _coordinator_load_run_metadata || return 1
    # shellcheck disable=SC1091,SC1090  # Digest-verified control bundle.
    source "$CONTROL_DIR/env_used.sh" || return 1
    # shellcheck disable=SC1091,SC1090  # Digest-verified coordinator helpers.
    source "$CONTROL_DIR/_nv-elbencho-kubectl-functions.sh" || return 1
    _coordinator_set_control_layout || return 1
    # shellcheck disable=SC1091,SC1090  # Digest-verified workload library.
    source "$CONTROL_DIR/_elbencho_functions.sh" || return 1
    _coordinator_validate_state_tree || return 1
    local observed_run_status
    observed_run_status=$(cat "$STATE_DIR/run.status" 2>/dev/null || true)
    [[ "$observed_run_status" =~ ^(PREPARED|RUNNING|SUCCESS|FAILED|CANCELLED)$ ]] || {
        _coordinator_error "lost-coordinator recovery found an invalid attempt state"
        return 1
    }
    _coordinator_ensure_recovery_lock "$observed_run_status" || {
        _coordinator_error "lost-coordinator recovery found an invalid durable lock"
        return 1
    }
    if [[ "$observed_run_status" =~ ^(SUCCESS|FAILED|CANCELLED)$ ]]; then
        _coordinator_repair_terminal_publication "$observed_run_status"
        return
    fi
    local allow_startup_repair=0
    [[ "$observed_run_status" == PREPARED ]] && allow_startup_repair=1
    _coordinator_initialize_snapshots "$allow_startup_repair" || return 1
    _coordinator_initialize_execution_states "$allow_startup_repair" || return 1
    local status_file id status failed_id="" first_pending="" changed=0
    local terminal_failed_id="" recovery_terminal recovery_exit_code
    local execution_observed_status=""
    for status_file in "$STATE_DIR"/executions/[0-9][0-9][0-9][0-9].status; do
        [[ -f "$status_file" && ! -L "$status_file" ]] || continue
        id=$(basename "$status_file" .status)
        status=$(cat "$status_file") || return 1
        case "$status" in
            RUNNING)
                _coordinator_atomic_write "$status_file" FAILED || return 1
                _coordinator_atomic_write "$STATE_DIR/executions/$id.exitcode" 143 || return 1
                [[ -n "$failed_id" ]] || failed_id="$id"
                execution_observed_status=RUNNING
                changed=1
                ;;
            PENDING) [[ -n "$first_pending" ]] || first_pending="$id" ;;
            SUCCESS) ;;
            FAILED)
                [[ -n "$terminal_failed_id" ]] || terminal_failed_id="$id"
                ;;
            *) _coordinator_error "invalid execution state during recovery: $id=$status"; return 1 ;;
        esac
    done
    if [[ "$changed" -eq 0 ]]; then
        if [[ -n "$terminal_failed_id" ]]; then
            # A failed cell may be followed by pending cells when the
            # coordinator dies after that cell's terminal checkpoint. Keep
            # the original failure and leave those cells resumable.
            failed_id="$terminal_failed_id"
            execution_observed_status=FAILED
            recovery_terminal=FAILED
            recovery_exit_code=$(cat "$STATE_DIR/executions/$failed_id.exitcode" \
                2>/dev/null || printf '1')
            [[ "$recovery_exit_code" =~ ^[0-9]+$ && "$recovery_exit_code" -ne 0 ]] \
                || recovery_exit_code=1
        elif [[ -n "$first_pending" ]]; then
            # No cell started. Keep every cell PENDING so collection preserves
            # the exact ledger and resume retries all unfinished work. The
            # attempt-level startup record carries the failure evidence.
            failed_id=""
            execution_observed_status=PENDING
            recovery_terminal=FAILED
            recovery_exit_code=143
        else
            # A coordinator can die after the final cell has durably
            # published its terminal state but before it publishes the
            # attempt manifest. There is no work left to mark failed: derive
            # the result from the durable execution ledger and publish it now.
            execution_observed_status=SUCCESS
            recovery_terminal=SUCCESS
            recovery_exit_code=0
        fi
    else
        recovery_terminal=FAILED
        recovery_exit_code=143
    fi
    local recovery_tmp="$STATE_DIR/.coordinator-loss.tmp.${BASHPID:-$$}.${RANDOM}"
    {
        printf 'schema\t1\n'
        printf 'attempt_id\t%s\n' "$ATTEMPT_ID"
        printf 'execution\t%s\n' "${failed_id:-none}"
        printf 'observed_status\t%s\n' "$observed_run_status"
        printf 'execution_observed_status\t%s\n' "$execution_observed_status"
        printf 'recovered_status\t%s\n' "$recovery_terminal"
        printf 'exit_code\t%s\n' "$recovery_exit_code"
    } > "$recovery_tmp" \
        && mv -f -- "$recovery_tmp" "$STATE_DIR/coordinator-loss.tsv" || return 1
    _coordinator_write_summary "$recovery_terminal" "$failed_id" "$recovery_exit_code" || return 1
    _coordinator_atomic_write "$STATE_DIR/run.status" "$recovery_terminal" || return 1
    _coordinator_publish_manifest
}

_coordinator_finalize_cancelled() {
    _coordinator_validate_arguments "$@" || return 1
    _coordinator_verify_bundle || return 1
    _coordinator_load_run_metadata || return 1
    # shellcheck disable=SC1091,SC1090  # Digest-verified control bundle.
    source "$CONTROL_DIR/env_used.sh" || return 1
    # shellcheck disable=SC1091,SC1090  # Digest-verified coordinator helpers.
    source "$CONTROL_DIR/_nv-elbencho-kubectl-functions.sh" || return 1
    _coordinator_set_control_layout || return 1
    # shellcheck disable=SC1091,SC1090  # Digest-verified workload library.
    source "$CONTROL_DIR/_elbencho_functions.sh" || return 1
    _coordinator_validate_state_tree || return 1
    local observed_run_status
    observed_run_status=$(cat "$STATE_DIR/run.status" 2>/dev/null || true)
    [[ "$observed_run_status" =~ ^(PREPARED|RUNNING|SUCCESS|FAILED|CANCELLED)$ ]] \
        && _coordinator_ensure_recovery_lock "$observed_run_status" || return 1
    local status_file id status failed_id=""
    for status_file in "$STATE_DIR"/executions/[0-9][0-9][0-9][0-9].status; do
        [[ -f "$status_file" && ! -L "$status_file" ]] || continue
        id=$(basename "$status_file" .status)
        status=$(cat "$status_file") || return 1
        case "$status" in
            RUNNING)
                _coordinator_atomic_write "$status_file" FAILED || return 1
                _coordinator_atomic_write "$STATE_DIR/executions/$id.exitcode" 143 \
                    || return 1
                [[ -n "$failed_id" ]] || failed_id="$id"
                ;;
            PENDING|SUCCESS|FAILED) ;;
            *) return 1 ;;
        esac
    done
    _coordinator_write_summary CANCELLED "$failed_id" 143 || return 1
    _coordinator_atomic_write "$STATE_DIR/run.status" CANCELLED || return 1
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
    while IFS= read -r id; do
        execution_status=$(cat "$execution_dir/$id.status") || return 1
        case "$execution_status" in
            SUCCESS) printf '%s\tSKIP\n' "$id" ;;
            PENDING|RUNNING|FAILED) printf '%s\tRUN\n' "$id" ;;
            *) return 1 ;;
        esac
    done < <(_coordinator_execution_ids "$execution_dir" .status)
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
    _coordinator_set_control_layout || return 1
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
    _coordinator_integration_crash_after after-run-status
    COORDINATOR_ACTIVE_ID=""
    COORDINATOR_ACTIVE_SCRATCH=""
    COORDINATOR_ACTIVE_PID=""
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
        _coordinator_prepare_active_execution "$id" || {
            _coordinator_error "cannot prepare execution $id; durable state retained for recovery"
            return 1
        }
        _coordinator_run_one "$id" &
        COORDINATOR_ACTIVE_PID=$!
        wait "$COORDINATOR_ACTIVE_PID" || rc=$?
        COORDINATOR_ACTIVE_PID=""
        _coordinator_complete_active_execution "$rc" || rc=$?
        if [[ "$rc" -ne 0 ]]; then
            failed_id="$id"
            break
        fi
        COORDINATOR_ACTIVE_ID=""
        COORDINATOR_ACTIVE_SCRATCH=""
    done < <(_coordinator_execution_ids "$CONTROL_DIR/executions" .sh)
    _coordinator_finalize_run "$rc" "$failed_id" || return 1
    return "$rc"
}

if [[ "${1:-}" == --recover-lost ]]; then
    [[ "$#" -eq 5 ]] || {
        _coordinator_error "usage: $KUBECTL_COORDINATOR_BASENAME --recover-lost CONTROL_DIR STATE_DIR SCRATCH_DIR ID"
        exit 1
    }
    _coordinator_recover_lost "${@:2}"
    exit $?
fi

if [[ "${1:-}" == --finalize-cancelled ]]; then
    [[ "$#" -eq 5 ]] || {
        _coordinator_error "usage: $KUBECTL_COORDINATOR_BASENAME --finalize-cancelled CONTROL_DIR STATE_DIR SCRATCH_DIR ID"
        exit 1
    }
    _coordinator_finalize_cancelled "${@:2}"
    exit $?
fi

if [[ "${1:-}" == --select-collected-resume ]]; then
    [[ "$#" -eq 2 ]] || {
        _coordinator_error "usage: $KUBECTL_COORDINATOR_BASENAME --select-collected-resume STATE_DIR"
        exit 1
    }
    _coordinator_select_collected_resume "$2"
    exit $?
fi

if [[ "${1:-}" == --probe-endpoint ]]; then
    [[ "$#" -eq 2 ]] || {
        _coordinator_error "usage: $KUBECTL_COORDINATOR_BASENAME --probe-endpoint IPV4"
        exit 1
    }
    _coordinator_probe_endpoint_request "$2"
    exit $?
fi

_coordinator_main "$@"
