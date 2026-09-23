# shellcheck shell=bash

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

# Source-only foundations for the asynchronous Kubernetes sweep lifecycle.
# Resource-specific templates and dispatch remain intentionally absent until
# the complete state machine is available.

if [[ "${STORAGE_SCALE_TEST_INTEGRATION:-}" == 1 \
        && -n "${KUBECTL_INTEGRATION_PVC_ROOT:-}" ]]; then
    readonly KUBECTL_SWEEP_MOUNT_ROOT="$KUBECTL_INTEGRATION_PVC_ROOT"
else
    readonly KUBECTL_SWEEP_MOUNT_ROOT=/mnt/storage-scale-test
fi
readonly KUBECTL_SWEEP_RESERVED_ROOT=.storage-scale-test
readonly KUBECTL_ATTEMPT_SCHEMA_VERSION=1
readonly KUBECTL_ATTEMPT_RESOURCE_SCHEMA_VERSION=1
readonly KUBECTL_ATTEMPT_CREATION_INTENT_SCHEMA_VERSION=1
readonly KUBECTL_COLLECTION_MAX_BYTES=$((2 * 1024 * 1024 * 1024))
readonly KUBECTL_COLLECTION_MAX_MEMBERS=50000
readonly KUBECTL_COLLECTION_TIMEOUT_SECONDS_DEFAULT=7200
readonly KUBECTL_COLLECTION_HEADROOM_BYTES=$((64 * 1024 * 1024))
readonly KUBECTL_JOB_QUIESCENCE_TIMEOUT_SECONDS_DEFAULT=120
readonly KUBECTL_CREATION_AMBIGUITY_SECONDS_DEFAULT=30
readonly KUBECTL_CREATION_ABSENCE_RECHECK_SECONDS_DEFAULT=2
readonly KUBECTL_OBSERVATION_ATTEMPTS_DEFAULT=3
readonly KUBECTL_OBSERVATION_BACKOFF_SECONDS_DEFAULT=1
declare -gA KUBECTL_LOCAL_LOCK_ROOTS=()

kubectl_normalize_logical_path() {
    local value="$1"
    local output_variable="$2"
    if [[ ! "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "Error: invalid output variable name" >&2
        return 1
    fi
    if [[ -z "$value" || "$value" == *$'\n'* || "$value" == *$'\r'* \
            || "$value" == *$'\t'* ]]; then
        echo "Error: Kubernetes logical paths must be nonempty single-line values" >&2
        return 1
    fi
    while [[ "$value" == /* ]]; do
        value="${value#/}"
    done
    local -a components=()
    local component
    IFS=/ read -ra components <<< "$value"
    local -a normalized=()
    for component in "${components[@]}"; do
        [[ -z "$component" ]] && continue
        if [[ "$component" == . || "$component" == .. ]]; then
            echo "Error: Kubernetes logical paths may not contain '.' or '..'" >&2
            return 1
        fi
        normalized+=("$component")
    done
    if [[ ${#normalized[@]} -eq 0 ]]; then
        echo "Error: Kubernetes logical paths may not resolve to the mount root" >&2
        return 1
    fi
    local joined
    joined=$(IFS=/; printf '%s' "${normalized[*]}")
    if [[ "$joined" == "$KUBECTL_SWEEP_RESERVED_ROOT" \
            || "$joined" == "$KUBECTL_SWEEP_RESERVED_ROOT/"* ]]; then
        echo "Error: logical path overlaps reserved Kubernetes orchestration state" >&2
        return 1
    fi
    printf -v "$output_variable" '%s' "$joined"
    return 0
}

kubectl_map_logical_path() {
    local logical
    kubectl_normalize_logical_path "$1" logical || return 1
    printf '%s/%s\n' "$KUBECTL_SWEEP_MOUNT_ROOT" "$logical"
}

kubectl_map_test_dirs() {
    local output_name="$1"
    if [[ ! "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "Error: invalid TEST_DIRS output name" >&2
        return 1
    fi
    local -n output_ref="$output_name"
    output_ref=()
    local source_path mapped_path
    for source_path in "${!TEST_DIRS[@]}"; do
        mapped_path=$(kubectl_map_logical_path "$source_path") || return 1
        if [[ -v output_ref["$mapped_path"] ]]; then
            echo "Error: multiple TEST_DIRS keys map to $mapped_path" >&2
            return 1
        fi
        output_ref["$mapped_path"]="${TEST_DIRS[$source_path]}"
    done
    return 0
}

kubectl_map_read_from_path() {
    local read_path
    kubectl_normalize_logical_path "$1" read_path || return 1
    local matches=0
    local mapped=""
    local root normalized_root suffix
    for root in "${!TEST_DIRS[@]}"; do
        kubectl_normalize_logical_path "$root" normalized_root || return 1
        if [[ "$read_path" == "$normalized_root" ]]; then
            suffix=""
        elif [[ "$read_path" == "$normalized_root/"* ]]; then
            suffix="/${read_path#"$normalized_root/"}"
        else
            continue
        fi
        matches=$((matches + 1))
        mapped="$KUBECTL_SWEEP_MOUNT_ROOT/$normalized_root$suffix"
    done
    if [[ "$matches" -ne 1 ]]; then
        echo "Error: read-from path must belong to exactly one logical TEST_DIRS root" >&2
        return 1
    fi
    printf '%s\n' "$mapped"
}

kubectl_validate_dns_subdomain() {
    local value="$1"
    [[ -n "$value" && ${#value} -le 253 \
        && "$value" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || return 1
    local -a labels=()
    local label
    IFS=. read -ra labels <<< "$value"
    for label in "${labels[@]}"; do
        [[ -n "$label" && ${#label} -le 63 \
            && "$label" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || return 1
    done
    return 0
}

kubectl_validate_object_name() {
    kubectl_validate_dns_subdomain "$1"
}

kubectl_validate_namespace_name() {
    local value="$1"
    [[ -n "$value" && ${#value} -le 63 \
        && "$value" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]
}

kubectl_validate_uid() {
    local value="$1"
    [[ -n "$value" && ${#value} -le 128 \
        && "$value" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]*$ ]]
}

kubectl_validate_label_name() {
    local value="$1"
    [[ -n "$value" && ${#value} -le 63 \
        && "$value" =~ ^[A-Za-z0-9]([A-Za-z0-9_.-]*[A-Za-z0-9])?$ ]]
}

kubectl_validate_label_value() {
    local value="$1"
    [[ -z "$value" || ( ${#value} -le 63 \
        && "$value" =~ ^[A-Za-z0-9]([A-Za-z0-9_.-]*[A-Za-z0-9])?$ ) ]]
}

kubectl_validate_label_key() {
    local key="$1"
    local prefix="" name="$key"
    if [[ "$key" == */* ]]; then
        prefix="${key%/*}"
        name="${key##*/}"
        [[ "$prefix" != */* ]] || return 1
        kubectl_validate_dns_subdomain "$prefix" || return 1
    fi
    kubectl_validate_label_name "$name"
}

kubectl_validate_node_selector() {
    local selector="$1"
    [[ -n "$selector" ]] || {
        echo "Error: KUBECTL_NODE_SELECTOR is required" >&2
        return 1
    }
    local -A seen=()
    local -a expressions=()
    local expression key value
    IFS=, read -ra expressions <<< "$selector"
    for expression in "${expressions[@]}"; do
        if [[ "$expression" != *=* || "${expression#*=}" == *=* ]]; then
            echo "Error: unsupported Kubernetes equality selector: $expression" >&2
            return 1
        fi
        key="${expression%%=*}"
        value="${expression#*=}"
        if ! kubectl_validate_label_key "$key" \
                || ! kubectl_validate_label_value "$value"; then
            echo "Error: unsupported Kubernetes equality selector: $expression" >&2
            return 1
        fi
        if [[ -v seen["$key"] ]]; then
            echo "Error: duplicate Kubernetes selector key: $key" >&2
            return 1
        fi
        seen["$key"]=1
    done
    return 0
}

_kubectl_random_hex() {
    local bytes="$1"
    od -An -N "$bytes" -tx1 /dev/urandom | tr -d ' \n'
}

kubectl_generate_attempt_id() {
    _kubectl_random_hex 4
}

kubectl_generate_ownership_nonce() {
    _kubectl_random_hex 16
}

_kubectl_validate_lifecycle_state() {
    [[ "$1" =~ ^(PREPARED|SUBMITTED|CANCEL_REQUESTED|TERMINAL|COLLECTION_IN_PROGRESS|COLLECTED|SUBMISSION_FAILED)$ ]]
}

kubectl_local_lock_acquire() {
    local kubernetes_dir="$1" output_variable="$2"
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    _kubectl_validate_local_directory_path "$kubernetes_dir" || return 1
    mkdir -p "$kubernetes_dir" || return 1
    [[ ! -L "$kubernetes_dir" && ! -L "$kubernetes_dir/lifecycle.lock" ]] || return 1
    local acquired_fd
    exec {acquired_fd}>"$kubernetes_dir/lifecycle.lock" || return 1
    if ! flock -n "$acquired_fd"; then
        eval "exec ${acquired_fd}>&-"
        return 1
    fi
    KUBECTL_LOCAL_LOCK_ROOTS["$acquired_fd"]="$kubernetes_dir"
    printf -v "$output_variable" '%s' "$acquired_fd"
}

_kubectl_validate_local_directory_path() {
    local requested="$1" current component
    [[ -n "$requested" && "$requested" != *$'\n'* && "$requested" != *$'\r'* ]] \
        || return 1
    if [[ "$requested" == /* ]]; then
        current=/
    else
        current=$PWD
    fi
    local normalized_path="${requested#/}"
    local -a path_components=()
    IFS=/ read -ra path_components <<< "$normalized_path"
    for component in "${path_components[@]}"; do
        [[ -n "$component" ]] || continue
        [[ "$component" != . && "$component" != .. ]] || {
            echo "Error: Kubernetes local state paths may not contain '.' or '..'" >&2
            return 1
        }
    done
    for component in "${path_components[@]}"; do
        [[ -n "$component" ]] || continue
        [[ ! -L "$current/$component" ]] || {
            echo "Error: Kubernetes local state may not traverse a symbolic link" >&2
            return 1
        }
        [[ -e "$current/$component" ]] || break
        [[ -d "$current/$component" ]] || return 1
        current="$current/$component"
    done
}

kubectl_local_lock_release() {
    local lock_fd="$1"
    [[ "$lock_fd" =~ ^[0-9]+$ && -v KUBECTL_LOCAL_LOCK_ROOTS["$lock_fd"] ]] \
        || return 1
    unset 'KUBECTL_LOCAL_LOCK_ROOTS[$lock_fd]'
    flock -u "$lock_fd" || return 1
    eval "exec ${lock_fd}>&-"
}

_kubectl_require_local_lock() {
    local kubernetes_dir="$1" lock_fd="$2"
    [[ "$lock_fd" =~ ^[0-9]+$ \
        && "${KUBECTL_LOCAL_LOCK_ROOTS[$lock_fd]:-}" == "$kubernetes_dir" ]]
}

kubectl_attempt_recover_incomplete_metadata() {
    local kubernetes_dir="$1" lock_fd="$2"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    local attempts_dir="$kubernetes_dir/attempts" staging restore_nullglob=0
    [[ -d "$attempts_dir" && ! -L "$attempts_dir" ]] || return 0
    shopt -q nullglob && restore_nullglob=1
    shopt -s nullglob
    for staging in "$attempts_dir"/.attempt-[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f].tmp.*; do
        [[ -d "$staging" && ! -L "$staging" ]] || continue
        rm -rf -- "$staging" || return 1
    done
    [[ "$restore_nullglob" -eq 1 ]] || shopt -u nullglob
}

kubectl_attempt_create_identity() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    local ownership_nonce="$4" namespace="$5" namespace_uid="$6"
    local pv="$7" pv_uid="$8" pvc="$9" pvc_uid="${10}"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    [[ "$ownership_nonce" =~ ^[0-9a-f]{32}$ ]] || return 1
    if ! kubectl_validate_namespace_name "$namespace" \
            || ! kubectl_validate_uid "$namespace_uid" \
            || ! kubectl_validate_object_name "$pv" \
            || ! kubectl_validate_uid "$pv_uid" \
            || ! kubectl_validate_object_name "$pvc" \
            || ! kubectl_validate_uid "$pvc_uid"; then
        echo "Error: invalid Kubernetes attempt identity" >&2
        return 1
    fi
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id"
    _kubectl_validate_local_directory_path "$kubernetes_dir" || return 1
    mkdir -p "$kubernetes_dir/attempts" || return 1
    kubectl_attempt_recover_incomplete_metadata "$kubernetes_dir" "$lock_fd" || return 1
    [[ ! -e "$metadata_dir" && ! -L "$metadata_dir" ]] || return 1
    # Publish the complete identity directory once. A failed preparation stays
    # under an opaque temporary name; readers cannot mistake it for an attempt.
    local staging_dir="$kubernetes_dir/attempts/.attempt-${attempt_id}.tmp.${BASHPID:-$$}.$RANDOM"
    mkdir "$staging_dir" || return 1
    local shell_tmp="$staging_dir/identity.sh"
    local yaml_tmp="$staging_dir/identity.yaml"
    {
        printf '# Trusted storage-scale-test Kubernetes attempt metadata.\n'
        printf 'KUBECTL_ATTEMPT_SCHEMA=%q\n' "$KUBECTL_ATTEMPT_SCHEMA_VERSION"
        printf 'KUBECTL_ATTEMPT_ID=%q\n' "$attempt_id"
        printf 'KUBECTL_OWNERSHIP_NONCE=%q\n' "$ownership_nonce"
        printf 'KUBECTL_NAMESPACE=%q\n' "$namespace"
        printf 'KUBECTL_NAMESPACE_UID=%q\n' "$namespace_uid"
        printf 'KUBECTL_PV=%q\n' "$pv"
        printf 'KUBECTL_PV_UID=%q\n' "$pv_uid"
        printf 'KUBECTL_PVC=%q\n' "$pvc"
        printf 'KUBECTL_PVC_UID=%q\n' "$pvc_uid"
    } > "$shell_tmp" || {
        rm -rf -- "$staging_dir"
        return 1
    }
    {
        printf 'schema: %s\n' "$KUBECTL_ATTEMPT_SCHEMA_VERSION"
        printf 'attempt_id: "%s"\n' "$attempt_id"
        printf 'ownership_nonce: "%s"\n' "$ownership_nonce"
        printf 'namespace: "%s"\n' "$namespace"
        printf 'namespace_uid: "%s"\n' "$namespace_uid"
        printf 'pv: "%s"\n' "$pv"
        printf 'pv_uid: "%s"\n' "$pv_uid"
        printf 'pvc: "%s"\n' "$pvc"
        printf 'pvc_uid: "%s"\n' "$pvc_uid"
    } > "$yaml_tmp" || {
        rm -rf -- "$staging_dir"
        return 1
    }
    mv -T -n "$staging_dir" "$metadata_dir" 2>/dev/null || true
    if [[ -d "$staging_dir" || ! -d "$metadata_dir" ]]; then
        rm -rf -- "$staging_dir"
        return 1
    fi
}

kubectl_attempt_write_current() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    kubectl_attempt_load_identity "$kubernetes_dir/attempts/$attempt_id" || return 1
    local tmp="$kubernetes_dir/current-attempt.tmp.${BASHPID:-$$}.$RANDOM"
    printf '%s\n' "$attempt_id" > "$tmp" \
        && mv -f "$tmp" "$kubernetes_dir/current-attempt"
}

kubectl_attempt_write_predecessor() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" predecessor="$4"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && "$predecessor" =~ ^[0-9a-f]{8}$ ]] || return 1
    kubectl_attempt_load_identity "$kubernetes_dir/attempts/$attempt_id" || return 1
    local path="$kubernetes_dir/attempts/$attempt_id/predecessor-attempt"
    local tmp="$path.tmp.${BASHPID:-$$}.$RANDOM"
    if ! printf '%s\n' "$predecessor" > "$tmp" \
        || ! mv -n "$tmp" "$path" 2>/dev/null; then
            rm -f -- "$tmp"
            return 1
    fi
    [[ ! -e "$tmp" ]] || {
        rm -f -- "$tmp"
        return 1
    }
}

kubectl_attempt_restore_predecessor() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local path="$kubernetes_dir/attempts/$attempt_id/predecessor-attempt"
    [[ ! -e "$path" ]] && return 0
    [[ -f "$path" && ! -L "$path" ]] || return 1
    local predecessor
    predecessor=$(cat -- "$path") || return 1
    [[ "$predecessor" =~ ^[0-9a-f]{8}$ ]] || return 1
    kubectl_attempt_write_current "$kubernetes_dir" "$lock_fd" "$predecessor"
}

kubectl_attempt_write_state() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" state="$4"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    _kubectl_validate_lifecycle_state "$state" || return 1
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id"
    kubectl_attempt_load_identity "$metadata_dir" || return 1
    if [[ -e "$metadata_dir/state.sh" ]]; then
        kubectl_attempt_load_metadata "$metadata_dir" || return 1
        kubectl_transition_is_valid "$KUBECTL_LIFECYCLE_STATE" "$state" || return 1
    elif [[ "$state" != PREPARED ]]; then
        return 1
    fi
    local tmp="$metadata_dir/state.sh.tmp.${BASHPID:-$$}.$RANDOM"
    printf 'KUBECTL_LIFECYCLE_STATE=%q\n' "$state" > "$tmp" \
        && mv -f "$tmp" "$metadata_dir/state.sh"
}

kubectl_attempt_load_identity() {
    local metadata_dir="$1" basename
    [[ -d "$metadata_dir" && ! -L "$metadata_dir" ]] || return 1
    basename="${metadata_dir##*/}"
    [[ "$basename" =~ ^[0-9a-f]{8}$ ]] || return 1
    local identity_file="$metadata_dir/identity.sh"
    local yaml_file="$metadata_dir/identity.yaml"
    [[ -f "$identity_file" && ! -L "$identity_file" \
        && -f "$yaml_file" && ! -L "$yaml_file" ]] || return 1
    unset KUBECTL_ATTEMPT_SCHEMA KUBECTL_ATTEMPT_ID KUBECTL_OWNERSHIP_NONCE
    unset KUBECTL_NAMESPACE KUBECTL_NAMESPACE_UID KUBECTL_PV KUBECTL_PV_UID
    unset KUBECTL_PVC KUBECTL_PVC_UID
    # shellcheck disable=SC1090  # Trusted, result-directory-local identity.
    source "$identity_file" || return 1
    [[ "${KUBECTL_ATTEMPT_SCHEMA:-}" == "$KUBECTL_ATTEMPT_SCHEMA_VERSION" \
        && "${KUBECTL_ATTEMPT_ID:-}" == "$basename" \
        && "${KUBECTL_OWNERSHIP_NONCE:-}" =~ ^[0-9a-f]{32}$ ]] || return 1
    grep -Fqx "schema: $KUBECTL_ATTEMPT_SCHEMA_VERSION" "$yaml_file" \
        && grep -Fqx "attempt_id: \"$KUBECTL_ATTEMPT_ID\"" "$yaml_file" \
        && grep -Fqx "ownership_nonce: \"$KUBECTL_OWNERSHIP_NONCE\"" "$yaml_file" \
        || return 1
    kubectl_validate_namespace_name "${KUBECTL_NAMESPACE:-}" \
        && kubectl_validate_uid "${KUBECTL_NAMESPACE_UID:-}" \
        && kubectl_validate_object_name "${KUBECTL_PV:-}" \
        && kubectl_validate_uid "${KUBECTL_PV_UID:-}" \
        && kubectl_validate_object_name "${KUBECTL_PVC:-}" \
        && kubectl_validate_uid "${KUBECTL_PVC_UID:-}"
}

kubectl_attempt_load_metadata() {
    local metadata_dir="$1"
    local state_file="$metadata_dir/state.sh"
    kubectl_attempt_load_identity "$metadata_dir" || return 1
    [[ -f "$state_file" && ! -L "$state_file" ]] || return 1
    unset KUBECTL_LIFECYCLE_STATE
    # The state sidecar has no identity fields. Preserve the already loaded
    # immutable identity while loading its independently atomically-published
    # lifecycle transition.
    # shellcheck disable=SC1090  # Trusted, result-directory-local state.
    source "$state_file" || return 1
    _kubectl_validate_lifecycle_state "${KUBECTL_LIFECYCLE_STATE:-}"
}

kubectl_emit_submission_identity() {
    local results_dir="$1" attempt_id="$2"
    [[ -n "$results_dir" && ! "$results_dir" =~ [[:cntrl:]] \
        && "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    printf 'STORAGE_SCALE_TEST_RESULTS_DIR=%s\n' "$results_dir"
    printf 'STORAGE_SCALE_TEST_ATTEMPT_ID=%s\n' "$attempt_id"
}

kubectl_emit_lifecycle_commands() {
    local results_dir="$1"
    [[ -d "$results_dir" && ! -L "$results_dir" ]] || return 1
    printf 'STORAGE_SCALE_TEST_STATUS_COMMAND=%q --status %q\n' "$0" "$results_dir"
    printf 'STORAGE_SCALE_TEST_CANCEL_COMMAND=%q --cancel %q\n' "$0" "$results_dir"
    printf 'STORAGE_SCALE_TEST_COLLECT_COMMAND=%q --collect %q\n' "$0" "$results_dir"
}

kubectl_emit_state() {
    local state="$1"
    [[ "$state" =~ ^(PREPARED|SUBMITTED|RUNNING|SUCCESS|FAILED|CANCELLED|SUBMISSION_FAILED|COLLECTED)$ ]] \
        || return 1
    printf 'STORAGE_SCALE_TEST_KUBECTL_STATE=%s\n' "$state"
}

_kubectl_validate_placeholder() {
    local token="$1" value="$2"
    [[ "$value" != *$'\n'* && "$value" != *$'\r'* \
        && "$value" != *$'\t'* ]] || return 1
    case "$token" in
        NAMESPACE) kubectl_validate_namespace_name "$value" ;;
        PVC_NAME|RESOURCE_NAME|NODE_NAME|COORDINATOR_NODE)
            kubectl_validate_object_name "$value"
            ;;
        ATTEMPT_ID) [[ "$value" =~ ^[0-9a-f]{8}$ ]] ;;
        OWNERSHIP_NONCE) [[ "$value" =~ ^[0-9a-f]{32}$ ]] ;;
        OPERATION_TOKEN)
            [[ -n "$value" && ${#value} -le 63 \
                && "$value" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]]
            ;;
        IMAGE)
            [[ -n "$value" && ${#value} -le 512 \
                && "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._/@:-]*$ ]]
            ;;
        IMAGE_PULL_POLICY) [[ "$value" =~ ^(Always|IfNotPresent|Never)$ ]] ;;
        RUN_AS_USER|RUN_AS_GROUP) [[ "$value" =~ ^[1-9][0-9]*$ ]] ;;
        COMPONENT)
            [[ "$value" =~ ^(worker|coordinator|controller|probe)$ ]]
            ;;
        SELECTOR_KEY) kubectl_validate_label_key "$value" ;;
        SELECTOR_VALUE) kubectl_validate_label_value "$value" ;;
        REMOTE_RUN_DIRECTORY)
            [[ "$value" =~ ^/mnt/storage-scale-test/\.storage-scale-test/runs/[0-9a-f]{8}(/[A-Za-z0-9._-]+)*$ ]]
            ;;
        *) return 1 ;;
    esac
}

kubectl_render_template() {
    local template="$1"
    shift
    local rendered="$template" assignment token value
    for assignment in "$@"; do
        if [[ "$assignment" != *=* || "${assignment#*=}" == *=* ]]; then
            echo "Error: Kubernetes template replacements require TOKEN=value" >&2
            return 1
        fi
        token="${assignment%%=*}"
        value="${assignment#*=}"
        if ! _kubectl_validate_placeholder "$token" "$value" \
                || [[ "$rendered" != *"@@$token@@"* ]]; then
            echo "Error: unsafe or unknown Kubernetes template replacement" >&2
            return 1
        fi
        rendered="${rendered//@@$token@@/$value}"
    done
    if [[ "$rendered" == *@@* ]]; then
        echo "Error: unresolved Kubernetes template token" >&2
        return 1
    fi
    printf '%s' "$rendered"
}

kubectl_run_bounded() {
    local request_timeout="${KUBECTL_REQUEST_TIMEOUT_SECONDS:-20}"
    local process_timeout="${KUBECTL_PROCESS_TIMEOUT_SECONDS:-30}"
    # Keep kubectl in timeout's child process group. In particular, a wedged
    # exec stream can otherwise survive TERM while timeout waits in foreground.
    timeout --kill-after=5s "${process_timeout}s" \
        kubectl --request-timeout="${request_timeout}s" "$@"
}

kubectl_report_lifecycle_error() {
    local operation="$1" phase="$2" reason="$3" safe_next_action="$4"
    local may_still_be_running="$5" kind="${6:-}" name="${7:-}"
    local namespace="${8:-}" expected_uid="${9:-}" observed_uid="${10:-}"
    local diagnostic_path="${11:-}"
    [[ "$operation" =~ ^[a-z][a-z0-9-]*$ \
        && "$phase" =~ ^[a-z][a-z0-9-]*$ \
        && "$reason" =~ ^(AUTH|TIMEOUT|API_THROTTLED|API_UNAVAILABLE|IDENTITY_MISMATCH|POD_UNSCHEDULABLE|IMAGE_PULL|PVC_MOUNT|PVC_IO|ENOSPC|LOCAL_IO|LEDGER_INCONSISTENT|OWNERSHIP_AMBIGUOUS)$ \
        && "$may_still_be_running" =~ ^(yes|no|unknown)$ ]] || return 1
    {
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_OPERATION=%q\n' "$operation"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_PHASE=%q\n' "$phase"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_ATTEMPT_ID=%q\n' \
            "${KUBECTL_ATTEMPT_ID:-unknown}"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_LOCAL_STATE=%q\n' \
            "${KUBECTL_LIFECYCLE_STATE:-unknown}"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_REMOTE_STATE=%q\n' \
            "${KUBECTL_REMOTE_STATE:-unknown}"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=%q\n' "$reason"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_KIND=%q\n' "$kind"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_NAME=%q\n' "$name"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_NAMESPACE=%q\n' "$namespace"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_EXPECTED_UID=%q\n' "$expected_uid"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_OBSERVED_UID=%q\n' "$observed_uid"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_LOCAL_STATE_PATH=%q\n' \
            "${KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH:-}"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_REMOTE_STATE_PATH=%q\n' \
            "${KUBECTL_DIAGNOSTIC_REMOTE_STATE_PATH:-}"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_MAY_STILL_BE_RUNNING=%q\n' \
            "$may_still_be_running"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_PATH=%q\n' "$diagnostic_path"
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_SAFE_NEXT_ACTION=%q\n' \
            "$safe_next_action"
    } >&2
}

_kubectl_observation_failure_is_transient() {
    local rc="$1" output="$2"
    [[ "$rc" =~ ^(124|137|143)$ ]] && return 0
    grep -Eqi 'too many requests|(^|[^0-9])429([^0-9]|$)|timeout|timed out|i/o timeout|connection (refused|reset)|tls handshake timeout|temporarily unavailable|service unavailable|internal server error|bad gateway|gateway timeout|(^|[^0-9])50[0234]([^0-9]|$)|unexpected EOF' \
        <<< "$output"
}

_kubectl_classify_observation_failure() {
    local output_variable="$1" rc="$2" output="$3"
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    local classified_reason=API_UNAVAILABLE
    if grep -Eqi 'unauthorized|forbidden|authentication|credentials' <<< "$output"; then
        classified_reason=AUTH
    elif grep -Eqi 'too many requests|(^|[^0-9])429([^0-9]|$)' <<< "$output"; then
        classified_reason=API_THROTTLED
    elif [[ "$rc" =~ ^(124|137|143)$ ]] \
            || grep -Eqi 'timeout|timed out|i/o timeout|tls handshake timeout' \
                <<< "$output"; then
        classified_reason=TIMEOUT
    elif grep -Eqi 'not found|notfound' <<< "$output"; then
        classified_reason=IDENTITY_MISMATCH
    fi
    printf -v "$output_variable" '%s' "$classified_reason"
}

_kubectl_observation_safe_action() {
    local reason="$1"
    case "$reason" in
        AUTH) printf '%s' 'authenticate kubectl and retry the same lifecycle command' ;;
        IDENTITY_MISMATCH)
            printf '%s' 'inspect the expected resource identity before retrying'
            ;;
        *) printf '%s' 'retry the same lifecycle command' ;;
    esac
}

# GET/list/status observations are safe to repeat. Mutating operations must
# continue to use kubectl_run_bounded directly and reconcile their intent.
kubectl_run_observational() {
    local attempts="${KUBECTL_OBSERVATION_ATTEMPTS:-$KUBECTL_OBSERVATION_ATTEMPTS_DEFAULT}"
    local backoff="${KUBECTL_OBSERVATION_BACKOFF_SECONDS:-$KUBECTL_OBSERVATION_BACKOFF_SECONDS_DEFAULT}"
    [[ "$attempts" =~ ^[1-9][0-9]*$ && "$backoff" =~ ^[0-9]+$ ]] || return 1
    local tmp_dir stdout_path stderr_path output rc attempt reason safe_action
    tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/storage-scale-test-kubectl-observe.XXXXXX") \
        || return 1
    stdout_path="$tmp_dir/stdout"
    stderr_path="$tmp_dir/stderr"
    for ((attempt = 1; attempt <= attempts; attempt++)); do
        rc=0
        kubectl_run_bounded "$@" > "$stdout_path" 2> "$stderr_path" || rc=$?
        if [[ "$rc" -eq 0 ]]; then
            local copy_rc=0
            cat -- "$stdout_path" || copy_rc=1
            cat -- "$stderr_path" >&2 || copy_rc=1
            rm -rf -- "$tmp_dir" || copy_rc=1
            [[ "$copy_rc" -eq 0 ]] || {
                kubectl_report_lifecycle_error kubectl-observe local-output \
                    LOCAL_IO "repair local temporary storage and retry" unknown \
                    "${KUBECTL_DIAGNOSTIC_RESOURCE_KIND:-}" \
                    "${KUBECTL_DIAGNOSTIC_RESOURCE_NAME:-}" \
                    "${KUBECTL_DIAGNOSTIC_RESOURCE_NAMESPACE:-}" || true
                return 1
            }
            return 0
        fi
        output=$(cat -- "$stderr_path" "$stdout_path") || output=""
        if [[ "$attempt" -ge "$attempts" ]] \
                || ! _kubectl_observation_failure_is_transient "$rc" "$output"; then
            cat -- "$stderr_path" >&2
            cat -- "$stdout_path"
            rm -rf -- "$tmp_dir"
            _kubectl_classify_observation_failure reason "$rc" "$output" \
                || reason=API_UNAVAILABLE
            safe_action=$(_kubectl_observation_safe_action "$reason") \
                || safe_action="retry the same lifecycle command"
            if [[ "$reason" != IDENTITY_MISMATCH ]]; then
                kubectl_report_lifecycle_error kubectl-observe api-observation \
                    "$reason" "$safe_action" unknown \
                    "${KUBECTL_DIAGNOSTIC_RESOURCE_KIND:-}" \
                    "${KUBECTL_DIAGNOSTIC_RESOURCE_NAME:-}" \
                    "${KUBECTL_DIAGNOSTIC_RESOURCE_NAMESPACE:-}" || true
            fi
            return "$rc"
        fi
        printf 'Warning: transient Kubernetes observation failed (attempt %d/%d); retrying\n' \
            "$attempt" "$attempts" >&2
        local delay=$((backoff * attempt))
        (( backoff == 0 )) || delay=$((delay + RANDOM % (backoff + 1)))
        sleep "$delay"
    done
    rm -rf -- "$tmp_dir"
    return 1
}

kubectl_capture_resource_diagnostics() {
    local metadata_dir="$1" operation="$2" namespace="$3" kind="$4" name="$5"
    local run_id="$6"
    _kubectl_validate_local_directory_path "$metadata_dir" \
        && [[ -d "$metadata_dir" && ! -L "$metadata_dir" \
        && "$operation" =~ ^[a-z][a-z0-9-]*$ \
        && "$kind" =~ ^[A-Za-z][A-Za-z0-9.-]*$ \
        && "$run_id" =~ ^[0-9a-f]{8}$ ]] \
        && kubectl_validate_namespace_name "$namespace" \
        && kubectl_validate_object_name "$name" || return 1
    local root="$metadata_dir/diagnostics" destination
    if [[ -e "$root" || -L "$root" ]]; then
        [[ -d "$root" && ! -L "$root" ]] || return 1
    else
        mkdir -- "$root" || return 1
    fi
    destination="$root/${operation}-$(date -u +%Y%m%dT%H%M%SZ)-${BASHPID:-$$}-$RANDOM"
    mkdir -- "$destination" || return 1
    KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
        kubectl_run_bounded -n "$namespace" get "$kind" "$name" -o yaml \
            2>&1 | head -c 524288 > "$destination/resource.yaml" || true
    KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
        kubectl_run_bounded -n "$namespace" describe "$kind" "$name" \
            2>&1 | head -c 524288 > "$destination/resource.describe" || true
    KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
        kubectl_run_bounded -n "$namespace" get pods \
            -l "storage-scale-test.nvidia.com/run=$run_id" -o yaml \
            2>&1 | head -c 524288 > "$destination/pods.yaml" || true
    KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
        kubectl_run_bounded -n "$namespace" logs \
            -l "storage-scale-test.nvidia.com/run=$run_id" \
            --all-containers=true --prefix=true --tail=200 \
            2>&1 | head -c 1048576 > "$destination/pods.log" || true
    local pod_names="" pod_ref pod_name
    pod_names=$(KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
        kubectl_run_bounded -n "$namespace" get pods \
            -l "storage-scale-test.nvidia.com/run=$run_id" -o name 2>/dev/null \
            | head -c 65536) \
        || pod_names=""
    local pod_count=0
    : > "$destination/events.txt" || return 1
    while IFS= read -r pod_ref; do
        [[ "$pod_ref" == pod/* ]] || continue
        pod_count=$((pod_count + 1))
        (( pod_count <= 4 )) || break
        pod_name=${pod_ref#pod/}
        kubectl_validate_object_name "$pod_name" || continue
        KUBECTL_REQUEST_TIMEOUT_SECONDS=3 KUBECTL_PROCESS_TIMEOUT_SECONDS=3 \
            kubectl_run_bounded -n "$namespace" get events \
                --field-selector "involvedObject.name=$pod_name" \
                --sort-by=.metadata.creationTimestamp \
                2>&1 | head -c 131072 >> "$destination/events.txt" || true
    done <<< "$pod_names"
    printf '%s\n' "$destination"
}

kubectl_capture_pvc_diagnostics() {
    local metadata_dir="$1" namespace="$2" helper_pod="$3" attempt_id="$4"
    local kind="$5" name="$6"
    local destination="" remote_run guard_script
    destination=$(kubectl_capture_resource_diagnostics "$metadata_dir" \
        storage-failure "$namespace" "$kind" "$name" "$attempt_id") \
        || destination=""
    [[ -n "$destination" ]] || return 1
    KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
        kubectl_run_bounded -n "$namespace" get pvc "$KUBECTL_PVC" -o yaml \
            2>&1 | head -c 524288 > "$destination/pvc.yaml" || true
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || {
        printf '%s\n' "$destination"
        return 0
    }
    guard_script=$(kubectl_remote_tree_guard_script) || {
        printf '%s\n' "$destination"
        return 0
    }
    KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
        kubectl_pvc_exec "$namespace" "$helper_pod" /bin/bash -ceu \
            "$guard_script
            df -Pk -- \"\$run\"
            df -Pi -- \"\$run\"" bash "$remote_run" "$attempt_id" \
            2>&1 | head -c 131072 > "$destination/pvc-filesystem.txt" || true
    printf '%s\n' "$destination"
}

kubectl_classify_storage_diagnostics() {
    local output_variable="$1" diagnostic_path="$2"
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
        && -d "$diagnostic_path" && ! -L "$diagnostic_path" ]] || return 1
    local evidence
    evidence=$(find -P "$diagnostic_path" -maxdepth 1 -type f -size -2M \
        -exec cat -- {} + 2>/dev/null) || evidence=""
    local storage_reason=""
    if grep -Eqi 'no space left on device|disk quota exceeded|ENOSPC' \
            <<< "$evidence"; then
        storage_reason=ENOSPC
    elif grep -Eqi 'input/output error|stale file handle|read-only file system|PVC_IO' \
            <<< "$evidence"; then
        storage_reason=PVC_IO
    else
        return 1
    fi
    printf -v "$output_variable" '%s' "$storage_reason"
}

kubectl_preserve_storage_failure_diagnostics() {
    local metadata_dir="$1" namespace="$2" helper_pod="$3" attempt_id="$4"
    local kind="$5" name="$6" expected_uid="$7"
    local diagnostic_path="" reason=""
    diagnostic_path=$(kubectl_capture_pvc_diagnostics "$metadata_dir" "$namespace" \
        "$helper_pod" "$attempt_id" "$kind" "$name") || diagnostic_path=""
    [[ -n "$diagnostic_path" ]] || return 0
    if kubectl_classify_storage_diagnostics reason "$diagnostic_path"; then
        KUBECTL_ATTEMPT_ID="$attempt_id" \
            KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="$metadata_dir/state.sh" \
            KUBECTL_DIAGNOSTIC_REMOTE_STATE_PATH="$(kubectl_attempt_remote_root "$attempt_id")/state/run.status" \
            kubectl_report_lifecycle_error recover-coordinator pvc-evidence \
                "$reason" \
                "free space or repair the PVC, then retry status or collection" \
                no "$kind" "$name" "$namespace" "$expected_uid" "" \
                "$diagnostic_path" || true
    else
        printf 'Kubernetes coordinator diagnostics: %s\n' "$diagnostic_path" >&2
    fi
    return 0
}

kubectl_capture_attempt_diagnostics() {
    local kubernetes_dir="$1" attempt_id="$2" operation="$3"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ \
        && "$operation" =~ ^[a-z][a-z0-9-]*$ ]] \
        && _kubectl_validate_local_directory_path "$kubernetes_dir" || return 1
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id"
    [[ -d "$metadata_dir" && ! -L "$metadata_dir" ]] || return 1
    local diagnostic_root="$metadata_dir/diagnostics"
    if [[ -e "$diagnostic_root" || -L "$diagnostic_root" ]]; then
        [[ -d "$diagnostic_root" && ! -L "$diagnostic_root" ]] || return 1
    fi
    local key intent_file diagnostic_path=""
    local -a resource_keys=(sweep workers transfer)
    for key in "${resource_keys[@]}"; do
        if kubectl_attempt_resource_exists "$kubernetes_dir" "$attempt_id" "$key"; then
            kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$key" \
                || continue
            diagnostic_path=$(kubectl_capture_resource_diagnostics "$metadata_dir" \
                "$operation" "$KUBECTL_RESOURCE_NAMESPACE" \
                "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" "$attempt_id") \
                || diagnostic_path=""
            if [[ -n "$diagnostic_path" ]]; then
                printf '%s\n' "$diagnostic_path"
                return 0
            fi
        fi
    done
    for key in "${resource_keys[@]}"; do
        intent_file="$metadata_dir/creation-intents/$key.sh"
        [[ -e "$intent_file" || -L "$intent_file" ]] || continue
        kubectl_attempt_load_creation_intent "$intent_file" || continue
        diagnostic_path=$(kubectl_capture_resource_diagnostics "$metadata_dir" \
            "$operation" "$KUBECTL_INTENT_NAMESPACE" "$KUBECTL_INTENT_KIND" \
            "$KUBECTL_INTENT_NAME" "$attempt_id") || diagnostic_path=""
        if [[ -n "$diagnostic_path" ]]; then
            printf '%s\n' "$diagnostic_path"
            return 0
        fi
    done
    return 1
}

kubectl_preserve_attempt_diagnostics() {
    local kubernetes_dir="$1" attempt_id="$2" operation="$3" diagnostic_path=""
    diagnostic_path=$(kubectl_capture_attempt_diagnostics "$kubernetes_dir" \
        "$attempt_id" "$operation") || diagnostic_path=""
    [[ -z "$diagnostic_path" ]] \
        || printf 'Kubernetes failure diagnostics: %s\n' "$diagnostic_path" >&2
    return 0
}

kubectl_classify_readiness_failure() {
    local output_variable="$1" namespace="$2" run_id="$3"
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
        && "$run_id" =~ ^[0-9a-f]{8}$ ]] \
        && kubectl_validate_namespace_name "$namespace" || return 1
    local evidence="" pod_names="" pod_ref pod_name pod_events
    evidence=$(KUBECTL_OBSERVATION_ATTEMPTS=1 \
        kubectl_run_observational -n "$namespace" get pods \
            -l "storage-scale-test.nvidia.com/run=$run_id" \
            -o 'jsonpath={range .items[*]}{range .status.containerStatuses[*]}{.state.waiting.reason}{"\n"}{end}{range .status.conditions[?(@.type=="PodScheduled")]}{.reason}{"\n"}{.message}{"\n"}{end}{end}' \
            2>/dev/null) || evidence=""
    pod_names=$(KUBECTL_OBSERVATION_ATTEMPTS=1 \
        kubectl_run_observational -n "$namespace" get pods \
            -l "storage-scale-test.nvidia.com/run=$run_id" -o name 2>/dev/null) \
        || pod_names=""
    while IFS= read -r pod_ref; do
        [[ "$pod_ref" == pod/* ]] || continue
        pod_name=${pod_ref#pod/}
        kubectl_validate_object_name "$pod_name" || continue
        pod_events=$(KUBECTL_OBSERVATION_ATTEMPTS=1 \
            kubectl_run_observational -n "$namespace" get events \
                --field-selector "involvedObject.name=$pod_name" \
                -o 'jsonpath={range .items[*]}{.reason}{"\t"}{.message}{"\n"}{end}' \
                2>/dev/null) || pod_events=""
        evidence+=$'\n'"$pod_events"
    done <<< "$pod_names"
    local classified_reason=TIMEOUT
    if grep -Eqi 'ErrImagePull|ImagePullBackOff|InvalidImageName' <<< "$evidence"; then
        classified_reason=IMAGE_PULL
    elif grep -Eqi 'FailedMount|FailedAttachVolume|MountVolume' <<< "$evidence"; then
        classified_reason=PVC_MOUNT
    elif grep -Eqi 'Unschedulable|FailedScheduling' <<< "$evidence"; then
        classified_reason=POD_UNSCHEDULABLE
    fi
    printf -v "$output_variable" '%s' "$classified_reason"
}

kubectl_verify_object_identity() {
    local kind="$1" name="$2" namespace="$3" nonce="$4" run_id="$5"
    local expected_uid="${6:-}"
    [[ "$kind" =~ ^[A-Za-z][A-Za-z0-9.-]*$ \
        && "$nonce" =~ ^[0-9a-f]{32}$ && "$run_id" =~ ^[0-9a-f]{8}$ ]] \
        || return 1
    kubectl_validate_object_name "$name" \
        && kubectl_validate_namespace_name "$namespace" || return 1
    [[ -z "$expected_uid" ]] || kubectl_validate_uid "$expected_uid" \
        || return 1
    local jsonpath='{.kind}{"\t"}{.metadata.name}{"\t"}{.metadata.uid}{"\t"}{.metadata.annotations.storage-scale-test\.nvidia\.com/ownership}{"\t"}{.metadata.labels.storage-scale-test\.nvidia\.com/run}'
    local observed
    observed=$(kubectl_run_observational -n "$namespace" get "$kind" "$name" \
        --ignore-not-found -o "jsonpath=$jsonpath") || return 1
    local observed_kind observed_name observed_uid observed_nonce observed_run
    IFS=$'\t' read -r observed_kind observed_name observed_uid observed_nonce \
        observed_run <<< "$observed"
    [[ -n "$observed" ]] || return 1
    if [[ "${observed_kind,,}" != "${kind,,}" || "$observed_name" != "$name" \
            || "$observed_nonce" != "$nonce" || "$observed_run" != "$run_id" \
            || ( -n "$expected_uid" && "$observed_uid" != "$expected_uid" ) ]]; then
        KUBECTL_ATTEMPT_ID="$run_id" kubectl_report_lifecycle_error \
            verify-identity resource-identity IDENTITY_MISMATCH \
            "inspect the expected and observed resource before retrying" \
            unknown "$kind" "$name" "$namespace" "$expected_uid" \
            "$observed_uid" "" || true
        return 1
    fi
    [[ "${observed_kind,,}" == "${kind,,}" && "$observed_name" == "$name" \
        && "$observed_nonce" == "$nonce" \
        && "$observed_run" == "$run_id" ]] || return 1
    kubectl_validate_uid "$observed_uid" || return 1
    printf '%s\n' "$observed_uid"
}

kubectl_create_owned_object() {
    local output_variable="$1" kind="$2" name="$3" namespace="$4"
    local nonce="$5" run_id="$6" manifest="$7"
    local kubernetes_dir="${8:-}" lock_fd="${9:-}" resource_key="${10:-}"
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    if [[ -n "$kubernetes_dir" || -n "$lock_fd" || -n "$resource_key" ]]; then
        [[ -n "$kubernetes_dir" && -n "$lock_fd" && -n "$resource_key" ]] || return 1
        kubectl_attempt_write_creation_intent "$kubernetes_dir" "$lock_fd" \
            "$run_id" "$resource_key" "$kind" "$name" "$namespace" "$nonce" \
            || return 1
    fi
    local create_output=""
    if ! create_output=$(printf '%s' "$manifest" \
            | kubectl_run_bounded -n "$namespace" create -f - 2>&1); then
        printf 'Warning: create response was not authoritative: %s\n' \
            "$create_output" >&2
    fi
    local _kubectl_created_object_uid
    _kubectl_created_object_uid=$(kubectl_verify_object_identity \
        "$kind" "$name" "$namespace" "$nonce" "$run_id") || return 1
    printf -v "$output_variable" '%s' "$_kubectl_created_object_uid"
    return 0
}

# Kubernetes lifecycle helpers below deliberately do not dispatch a benchmark.
# They provide the bounded, ownership-checked substrate operations consumed by
# the coordinator and the filesystem sweep once its state machine is complete.

kubectl_template_directory() {
    local source_dir
    source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) || return 1
    printf '%s/templates\n' "$source_dir"
}

kubectl_attempt_remote_root() {
    local attempt_id="$1"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    printf '%s/%s/runs/%s\n' "$KUBECTL_SWEEP_MOUNT_ROOT" \
        "$KUBECTL_SWEEP_RESERVED_ROOT" "$attempt_id"
}

kubectl_attempt_remote_lock_directory() {
    printf '%s/%s/locks/kubernetes-elbencho-sweep\n' \
        "$KUBECTL_SWEEP_MOUNT_ROOT" "$KUBECTL_SWEEP_RESERVED_ROOT"
}

# This fragment is embedded in every remote control-tree mutation.  The
# lexical check prevents argument substitution, while the realpath checks
# detect a reserved run or any of its parents being replaced by a symlink
# between lifecycle operations.  Keep it self-contained: it runs in the
# workload image, not in the submitting shell.
kubectl_remote_tree_guard_script() {
    cat <<'EOF'
run=$1
attempt=$2
mount=/mnt/storage-scale-test
root=$mount/.storage-scale-test
[[ "$attempt" =~ ^[0-9a-f]{8}$ && "$run" == "$root/runs/$attempt" ]] || exit 1
mount_real=$(realpath -e -- "$mount") || exit 1
root_real=$(realpath -e -- "$root") || exit 1
[[ "$root_real" == "$mount_real/.storage-scale-test" ]] || exit 1
[[ -d "$run" && ! -L "$run" ]] || exit 1
run_real=$(realpath -e -- "$run") || exit 1
[[ "$run_real" == "$root_real/runs/$attempt" ]] || exit 1
EOF
}

kubectl_validate_remote_run_directory() {
    local value="$1" expected
    expected=$(kubectl_attempt_remote_root "${KUBECTL_ATTEMPT_ID:-}") || return 1
    [[ "$value" == "$expected" ]]
}

kubectl_transition_is_valid() {
    local previous="$1" next="$2"
    case "$previous:$next" in
        PREPARED:SUBMITTED|PREPARED:SUBMISSION_FAILED|SUBMITTED:CANCEL_REQUESTED|\
        SUBMITTED:TERMINAL|CANCEL_REQUESTED:TERMINAL|TERMINAL:COLLECTION_IN_PROGRESS|\
        COLLECTION_IN_PROGRESS:COLLECTED)
            return 0
            ;;
        *) return 1 ;;
    esac
}

kubectl_attempt_transition() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" next="$4"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$attempt_id" || return 1
    kubectl_transition_is_valid "$KUBECTL_LIFECYCLE_STATE" "$next" || {
        echo "Error: invalid Kubernetes lifecycle transition $KUBECTL_LIFECYCLE_STATE -> $next" >&2
        return 1
    }
    kubectl_attempt_write_state "$kubernetes_dir" "$lock_fd" "$attempt_id" "$next"
}

_kubectl_validate_resource_key() {
    [[ "$1" =~ ^[a-z][a-z0-9-]{0,61}$ ]]
}

kubectl_attempt_journal_resource() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" resource_key="$4"
    local kind="$5" name="$6" namespace="$7" uid="$8" nonce="$9"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] \
        && _kubectl_validate_resource_key "$resource_key" \
        && kubectl_validate_object_name "$name" \
        && kubectl_validate_namespace_name "$namespace" \
        && kubectl_validate_uid "$uid" \
        && [[ "$nonce" =~ ^[0-9a-f]{32}$ ]] || return 1
    local attempt_dir="$kubernetes_dir/attempts/$attempt_id"
    [[ -f "$attempt_dir/identity.sh" && ! -L "$attempt_dir/identity.sh" ]] || return 1
    mkdir -p "$attempt_dir/resources" || return 1
    local resource_file="$attempt_dir/resources/$resource_key.sh"
    [[ ! -e "$resource_file" && ! -L "$resource_file" ]] || return 1
    local tmp="$resource_file.tmp.${BASHPID:-$$}.$RANDOM"
    {
        printf '# Trusted storage-scale-test Kubernetes resource journal.\n'
        printf 'KUBECTL_RESOURCE_SCHEMA=%q\n' "$KUBECTL_ATTEMPT_RESOURCE_SCHEMA_VERSION"
        printf 'KUBECTL_RESOURCE_KIND=%q\n' "$kind"
        printf 'KUBECTL_RESOURCE_NAME=%q\n' "$name"
        printf 'KUBECTL_RESOURCE_NAMESPACE=%q\n' "$namespace"
        printf 'KUBECTL_RESOURCE_UID=%q\n' "$uid"
        printf 'KUBECTL_RESOURCE_NONCE=%q\n' "$nonce"
    } > "$tmp" || return 1
    mv -n "$tmp" "$resource_file" 2>/dev/null || {
        rm -f -- "$tmp"
        return 1
    }
    [[ ! -e "$tmp" ]] || {
        rm -f -- "$tmp"
        return 1
    }
}

kubectl_attempt_write_creation_intent() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" resource_key="$4"
    local kind="$5" name="$6" namespace="$7" nonce="$8"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] \
        && _kubectl_validate_resource_key "$resource_key" \
        && [[ "$kind" =~ ^[A-Za-z][A-Za-z0-9.-]*$ ]] \
        && kubectl_validate_object_name "$name" \
        && kubectl_validate_namespace_name "$namespace" \
        && [[ "$nonce" =~ ^[0-9a-f]{32}$ ]] || return 1
    local intent_dir="$kubernetes_dir/attempts/$attempt_id/creation-intents"
    mkdir -p "$intent_dir" || return 1
    local intent_file="$intent_dir/$resource_key.sh"
    [[ ! -e "$intent_file" && ! -L "$intent_file" ]] || return 1
    local tmp="$intent_file.tmp.${BASHPID:-$$}.$RANDOM"
    {
        printf '# Trusted storage-scale-test Kubernetes creation intent.\n'
        printf 'KUBECTL_INTENT_SCHEMA=%q\n' "$KUBECTL_ATTEMPT_CREATION_INTENT_SCHEMA_VERSION"
        printf 'KUBECTL_INTENT_KIND=%q\n' "$kind"
        printf 'KUBECTL_INTENT_NAME=%q\n' "$name"
        printf 'KUBECTL_INTENT_NAMESPACE=%q\n' "$namespace"
        printf 'KUBECTL_INTENT_NONCE=%q\n' "$nonce"
    } > "$tmp" || return 1
    mv -n "$tmp" "$intent_file" 2>/dev/null || {
        rm -f -- "$tmp"
        return 1
    }
    [[ ! -e "$tmp" ]] || {
        rm -f -- "$tmp"
        return 1
    }
}

kubectl_attempt_load_creation_intent() {
    local intent_file="$1"
    [[ -f "$intent_file" && ! -L "$intent_file" ]] || return 1
    unset KUBECTL_INTENT_SCHEMA KUBECTL_INTENT_KIND KUBECTL_INTENT_NAME
    unset KUBECTL_INTENT_NAMESPACE KUBECTL_INTENT_NONCE
    # shellcheck disable=SC1090  # Trusted, result-directory-local intent.
    source "$intent_file" || return 1
    [[ "$KUBECTL_INTENT_SCHEMA" == "$KUBECTL_ATTEMPT_CREATION_INTENT_SCHEMA_VERSION" \
        && "$KUBECTL_INTENT_KIND" =~ ^[A-Za-z][A-Za-z0-9.-]*$ \
        && "$KUBECTL_INTENT_NONCE" =~ ^[0-9a-f]{32}$ ]] \
        && kubectl_validate_object_name "$KUBECTL_INTENT_NAME" \
        && kubectl_validate_namespace_name "$KUBECTL_INTENT_NAMESPACE"
}

kubectl_attempt_clear_creation_intent() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" resource_key="$4"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] \
        && _kubectl_validate_resource_key "$resource_key" || return 1
    local intent_file="$kubernetes_dir/attempts/$attempt_id/creation-intents/$resource_key.sh"
    [[ ! -e "$intent_file" && ! -L "$intent_file" ]] || rm -f -- "$intent_file"
}

kubectl_cleanup_creation_intent() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" resource_key="$4"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] \
        && _kubectl_validate_resource_key "$resource_key" || return 1
    local intent_file="$kubernetes_dir/attempts/$attempt_id/creation-intents/$resource_key.sh"
    kubectl_attempt_load_creation_intent "$intent_file" || return 1
    local observed_uid
    observed_uid=$(kubectl_run_observational -n "$KUBECTL_INTENT_NAMESPACE" \
        get "$KUBECTL_INTENT_KIND" "$KUBECTL_INTENT_NAME" --ignore-not-found \
        -o 'jsonpath={.metadata.uid}') || return 1
    if [[ -z "$observed_uid" ]]; then
        # A timed-out create request can still be finishing in the API server.
        # Do not erase its only recovery identity until the create's process
        # deadline has elapsed and a later linearizable GET also sees absence.
        local intent_epoch now remaining
        intent_epoch=$(stat -c %Y -- "$intent_file") || return 1
        now=$(date +%s) || return 1
        [[ "$intent_epoch" =~ ^[0-9]+$ && "$now" =~ ^[0-9]+$ \
            && "$now" -ge "$intent_epoch" ]] || return 1
        remaining=$((KUBECTL_CREATION_AMBIGUITY_SECONDS_DEFAULT - now + intent_epoch))
        (( remaining <= 0 )) || sleep "$remaining"
        sleep "$KUBECTL_CREATION_ABSENCE_RECHECK_SECONDS_DEFAULT"
        observed_uid=$(kubectl_run_observational -n "$KUBECTL_INTENT_NAMESPACE" \
            get "$KUBECTL_INTENT_KIND" "$KUBECTL_INTENT_NAME" --ignore-not-found \
            -o 'jsonpath={.metadata.uid}') || return 1
    fi
    if [[ -n "$observed_uid" ]]; then
        kubectl_verify_object_identity "$KUBECTL_INTENT_KIND" \
            "$KUBECTL_INTENT_NAME" "$KUBECTL_INTENT_NAMESPACE" \
            "$KUBECTL_INTENT_NONCE" "$attempt_id" "$observed_uid" >/dev/null \
            && kubectl_delete_owned_object "$KUBECTL_INTENT_KIND" \
                "$KUBECTL_INTENT_NAME" "$KUBECTL_INTENT_NAMESPACE" \
                "$KUBECTL_INTENT_NONCE" "$attempt_id" "$observed_uid" \
            || return 1
    else
        # The bounded ambiguity horizon has expired, so ordinary rollback may
        # proceed. Retain the deterministic possible identity permanently in
        # case a severely delayed API create appears later; never reconstruct
        # or delete such an object from a broad label query.
        local ambiguous_dir="$kubernetes_dir/attempts/$attempt_id/ambiguous-absence"
        local ambiguous_file="$ambiguous_dir/$resource_key.sh" ambiguous_tmp
        mkdir -p -- "$ambiguous_dir" || return 1
        [[ -d "$ambiguous_dir" && ! -L "$ambiguous_dir" ]] || return 1
        if [[ -e "$ambiguous_file" || -L "$ambiguous_file" ]]; then
            [[ -f "$ambiguous_file" && ! -L "$ambiguous_file" ]] \
                && cmp -s -- "$intent_file" "$ambiguous_file" || return 1
        else
            ambiguous_tmp="$ambiguous_file.tmp.${BASHPID:-$$}.$RANDOM"
            if ! cp -- "$intent_file" "$ambiguous_tmp" \
                    || ! mv -n -- "$ambiguous_tmp" "$ambiguous_file"; then
                    rm -f -- "$ambiguous_tmp"
                    return 1
            fi
        fi
        KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
            reconcile-create creation-ambiguity OWNERSHIP_AMBIGUOUS \
            "inspect the deterministic resource name before deleting it manually" \
            unknown "$KUBECTL_INTENT_KIND" "$KUBECTL_INTENT_NAME" \
            "$KUBECTL_INTENT_NAMESPACE" "" "" "$ambiguous_file" || true
    fi
    kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" "$resource_key"
}

kubectl_attempt_load_resource() {
    local kubernetes_dir="$1" attempt_id="$2" resource_key="$3"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] \
        && _kubectl_validate_resource_key "$resource_key" || return 1
    local resource_file="$kubernetes_dir/attempts/$attempt_id/resources/$resource_key.sh"
    [[ -f "$resource_file" && ! -L "$resource_file" ]] || return 1
    unset KUBECTL_RESOURCE_SCHEMA KUBECTL_RESOURCE_KIND KUBECTL_RESOURCE_NAME
    unset KUBECTL_RESOURCE_NAMESPACE KUBECTL_RESOURCE_UID KUBECTL_RESOURCE_NONCE
    # shellcheck disable=SC1090  # Trusted, result-directory-local journal.
    source "$resource_file" || return 1
    [[ "$KUBECTL_RESOURCE_SCHEMA" == "$KUBECTL_ATTEMPT_RESOURCE_SCHEMA_VERSION" \
        && "$KUBECTL_RESOURCE_KIND" =~ ^[A-Za-z][A-Za-z0-9.-]*$ \
        && "$KUBECTL_RESOURCE_NONCE" =~ ^[0-9a-f]{32}$ ]] \
        && kubectl_validate_object_name "$KUBECTL_RESOURCE_NAME" \
        && kubectl_validate_namespace_name "$KUBECTL_RESOURCE_NAMESPACE" \
        && kubectl_validate_uid "$KUBECTL_RESOURCE_UID"
}

kubectl_attempt_resource_exists() {
    local kubernetes_dir="$1" attempt_id="$2" resource_key="$3"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] \
        && _kubectl_validate_resource_key "$resource_key" || return 1
    local resource_file="$kubernetes_dir/attempts/$attempt_id/resources/$resource_key.sh"
    [[ -e "$resource_file" || -L "$resource_file" ]]
}

kubectl_attempt_journal_step() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" step="$4"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && "$step" =~ ^[a-z][a-z0-9-]{0,61}$ ]] \
        || return 1
    local steps_dir="$kubernetes_dir/attempts/$attempt_id/cleanup"
    local step_path="$steps_dir/$step"
    if [[ -e "$steps_dir" || -L "$steps_dir" ]]; then
        [[ -d "$steps_dir" && ! -L "$steps_dir" ]] || return 1
    else
        mkdir "$steps_dir" || return 1
    fi
    # Cleanup is deliberately retryable. A caller can be killed after this
    # marker is published but before its next lifecycle transition; seeing the
    # same regular marker again proves that step was already committed.
    if [[ -e "$step_path" || -L "$step_path" ]]; then
        [[ -f "$step_path" && ! -L "$step_path" ]]
        return
    fi
    local tmp="$step_path.tmp.${BASHPID:-$$}.$RANDOM"
    if ! : > "$tmp" || ! mv -n "$tmp" "$step_path" 2>/dev/null; then
        rm -f -- "$tmp"
        return 1
    fi
    if [[ -e "$tmp" ]]; then
        rm -f -- "$tmp"
        [[ -f "$step_path" && ! -L "$step_path" ]] || return 1
    fi
}

kubectl_attempt_step_done() {
    local kubernetes_dir="$1" attempt_id="$2" step="$3"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && "$step" =~ ^[a-z][a-z0-9-]{0,61}$ ]] \
        || return 1
    [[ -f "$kubernetes_dir/attempts/$attempt_id/cleanup/$step" \
        && ! -L "$kubernetes_dir/attempts/$attempt_id/cleanup/$step" ]]
}

kubectl_attempt_write_configuration() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" mapped_dirs_name="$4"
    local mapped_read_from="${5:-}"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_validate_runtime_configuration || return 1
    [[ "$mapped_dirs_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    local -n mapped_dirs_ref="$mapped_dirs_name"
    [[ ${#mapped_dirs_ref[@]} -gt 0 ]] || return 1
    local path
    for path in "${!mapped_dirs_ref[@]}"; do
        [[ "$path" == "$KUBECTL_SWEEP_MOUNT_ROOT/"* ]] || return 1
    done
    [[ -z "$mapped_read_from" || "$mapped_read_from" == "$KUBECTL_SWEEP_MOUNT_ROOT/"* ]] \
        || return 1
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id"
    kubectl_attempt_load_identity "$metadata_dir" || return 1
    local config_file="$metadata_dir/configuration.sh"
    [[ ! -e "$config_file" && ! -L "$config_file" ]] || return 1
    local tmp="$config_file.tmp.${BASHPID:-$$}.$RANDOM"
    {
        printf '# Trusted storage-scale-test Kubernetes attempt configuration.\n'
        printf 'KUBECTL_NAMESPACE=%q\n' "$KUBECTL_NAMESPACE"
        printf 'KUBECTL_PV=%q\n' "$KUBECTL_PV"
        printf 'KUBECTL_PVC=%q\n' "$KUBECTL_PVC"
        printf 'KUBECTL_NODE_SELECTOR=%q\n' "$KUBECTL_NODE_SELECTOR"
        printf 'KUBECTL_ELBENCHO_IMAGE=%q\n' "$KUBECTL_ELBENCHO_IMAGE"
        printf 'KUBECTL_IMAGE_PULL_POLICY=%q\n' "$KUBECTL_IMAGE_PULL_POLICY"
        printf 'KUBECTL_RUN_AS_USER=%q\n' "$KUBECTL_RUN_AS_USER"
        printf 'KUBECTL_RUN_AS_GROUP=%q\n' "$KUBECTL_RUN_AS_GROUP"
        printf 'KUBECTL_MAPPED_READ_FROM=%q\n' "$mapped_read_from"
        declare -p "$mapped_dirs_name" \
            | sed "s/^declare -A $mapped_dirs_name=/declare -A KUBECTL_MAPPED_TEST_DIRS=/"
    } > "$tmp" || return 1
    mv -n "$tmp" "$config_file" 2>/dev/null || {
        rm -f -- "$tmp"
        return 1
    }
    [[ ! -e "$tmp" ]] || {
        rm -f -- "$tmp"
        return 1
    }
}

kubectl_render_node_selector() {
    local selector="$1"
    kubectl_validate_node_selector "$selector" || return 1
    local -a pairs=()
    local pair key value
    IFS=, read -ra pairs <<< "$selector"
    for pair in "${pairs[@]}"; do
        key="${pair%%=*}"
        value="${pair#*=}"
        printf '        %s: "%s"\n' "$key" "$value"
    done
}

kubectl_render_node_affinity_values() {
    local node node_names=""
    for node in "$@"; do
        kubectl_validate_object_name "$node" || return 1
        node_names+="                      - $node"$'\n'
    done
    [[ -n "$node_names" ]] || return 1
    printf '%s' "$node_names"
}

_kubectl_validate_multiline_placeholder() {
    local token="$1" value="$2" line
    case "$token" in
        NODE_SELECTOR_BLOCK)
            local selector_key selector_value
            while IFS= read -r line; do
                [[ "$line" =~ ^[[:space:]]{8}[A-Za-z0-9./_-]+:[[:space:]]\"[A-Za-z0-9._-]*\"$ ]] || return 1
                selector_key="${line#        }"
                selector_key="${selector_key%%:*}"
                selector_value="${line#*: \"}"
                selector_value="${selector_value%\"}"
                kubectl_validate_label_key "$selector_key" \
                    && kubectl_validate_label_value "$selector_value" || return 1
            done <<< "$value"
            ;;
        NODE_AFFINITY_VALUES)
            while IFS= read -r line; do
                [[ "$line" =~ ^[[:space:]]{22}-[[:space:]][a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || return 1
            done <<< "$value"
            ;;
        *) return 1 ;;
    esac
}

kubectl_render_attempt_template() {
    local template_path="$1"
    shift
    [[ -f "$template_path" && ! -L "$template_path" ]] || return 1
    local template
    template=$(<"$template_path") || return 1
    local -a basic=()
    local assignment token value rendered="$template"
    for assignment in "$@"; do
        [[ "$assignment" == *=* ]] || return 1
        token="${assignment%%=*}"
        value="${assignment#*=}"
        case "$token" in
            NODE_SELECTOR_BLOCK|NODE_AFFINITY_VALUES)
                _kubectl_validate_multiline_placeholder "$token" "$value" || return 1
                [[ "$rendered" == *"@@$token@@"* ]] || return 1
                rendered="${rendered//@@$token@@/$value}"
                ;;
            *) basic+=("$assignment") ;;
        esac
    done
    kubectl_render_template "$rendered" "${basic[@]}"
}

kubectl_validate_runtime_configuration() {
    local value
    for value in KUBECTL_NAMESPACE KUBECTL_PV KUBECTL_PVC \
        KUBECTL_NODE_SELECTOR KUBECTL_ELBENCHO_IMAGE KUBECTL_IMAGE_PULL_POLICY \
        KUBECTL_RUN_AS_USER KUBECTL_RUN_AS_GROUP; do
        [[ -n "${!value:-}" ]] || {
            echo "Error: $value is required for Kubernetes filesystem sweeps" >&2
            return 1
        }
    done
    if ! kubectl_validate_namespace_name "$KUBECTL_NAMESPACE" \
            || ! kubectl_validate_object_name "$KUBECTL_PV" \
            || ! kubectl_validate_object_name "$KUBECTL_PVC" \
            || ! kubectl_validate_node_selector "$KUBECTL_NODE_SELECTOR" \
            || ! _kubectl_validate_placeholder IMAGE "$KUBECTL_ELBENCHO_IMAGE" \
            || ! _kubectl_validate_placeholder IMAGE_PULL_POLICY "$KUBECTL_IMAGE_PULL_POLICY" \
            || ! _kubectl_validate_placeholder RUN_AS_USER "$KUBECTL_RUN_AS_USER" \
            || ! _kubectl_validate_placeholder RUN_AS_GROUP "$KUBECTL_RUN_AS_GROUP"; then
        echo "Error: invalid Kubernetes filesystem sweep configuration" >&2
        return 1
    fi
    if [[ "$KUBECTL_IMAGE_PULL_POLICY" == Always \
            && ! "$KUBECTL_ELBENCHO_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]; then
        echo "Error: KUBECTL_IMAGE_PULL_POLICY=Always requires a digest-qualified KUBECTL_ELBENCHO_IMAGE" >&2
        return 1
    fi
}

kubectl_get_object_uid() {
    local kind="$1" name="$2" namespace="${3:-}"
    [[ "$kind" =~ ^[A-Za-z][A-Za-z0-9.-]*$ ]] \
        && kubectl_validate_object_name "$name" || return 1
    local -a args=(get "$kind" "$name" -o 'jsonpath={.metadata.uid}')
    [[ -z "$namespace" ]] || {
        kubectl_validate_namespace_name "$namespace" || return 1
        args=(-n "$namespace" "${args[@]}")
    }
    local uid
    uid=$(kubectl_run_observational "${args[@]}") || return 1
    kubectl_validate_uid "$uid" || return 1
    printf '%s\n' "$uid"
}

kubectl_validate_cluster_identity() {
    kubectl_validate_runtime_configuration || return 1
    kubectl_run_observational version >/dev/null || return 1
    local namespace_uid pv_uid pvc_uid volume_name
    namespace_uid=$(kubectl_get_object_uid namespace "$KUBECTL_NAMESPACE") || return 1
    pv_uid=$(kubectl_get_object_uid pv "$KUBECTL_PV") || return 1
    pvc_uid=$(kubectl_get_object_uid pvc "$KUBECTL_PVC" "$KUBECTL_NAMESPACE") || return 1
    volume_name=$(kubectl_run_observational -n "$KUBECTL_NAMESPACE" get pvc "$KUBECTL_PVC" \
        -o 'jsonpath={.spec.volumeName}{"\t"}{.status.phase}{"\t"}{.spec.volumeMode}{"\t"}{.spec.accessModes[*]}') || return 1
    local observed_pv phase mode access
    IFS=$'\t' read -r observed_pv phase mode access <<< "$volume_name"
    [[ "$observed_pv" == "$KUBECTL_PV" && "$phase" == Bound \
        && ( -z "$mode" || "$mode" == Filesystem ) && "$access" == *ReadWriteMany* ]] || {
        echo "Error: configured Kubernetes PVC is not the expected bound RWX filesystem claim" >&2
        return 1
    }
    printf '%s\t%s\t%s\n' "$namespace_uid" "$pv_uid" "$pvc_uid"
}

kubectl_validate_runtime_pod() {
    # Use a deadline-bounded, TTL-cleaned Job so SIGKILL of the local validator
    # cannot leave an unjournaled sleeping Pod. The Job validates the image,
    # workload identity, command contract, and actual PVC write/read/remove.
    kubectl_validate_cluster_identity >/dev/null || return 1
    local nodes_dir nodes_path helper_uid attempt_id nonce node helper_name manifest rc=0
    nodes_dir=$(mktemp -d "${TMPDIR:-/tmp}/storage-scale-test-kubectl-nodes.XXXXXX") || return 1
    nodes_path="$nodes_dir/nodes.tsv"
    kubectl_discover_candidate_nodes "$KUBECTL_NODE_SELECTOR" "$nodes_path" || {
        rm -rf -- "$nodes_dir"
        return 1
    }
    node=$(kubectl_choose_coordinator_node "$nodes_path") || {
        rm -rf -- "$nodes_dir"
        return 1
    }
    rm -rf -- "$nodes_dir"
    attempt_id=$(kubectl_generate_attempt_id) || return 1
    nonce=$(kubectl_generate_ownership_nonce) || return 1
    helper_name="sst-elb-$attempt_id-validation"
    manifest=$(kubectl_render_attempt_template \
        "$(kubectl_template_directory)/validation-job.yaml.tmpl" \
        "NAMESPACE=$KUBECTL_NAMESPACE" "RESOURCE_NAME=$helper_name" \
        "ATTEMPT_ID=$attempt_id" "OWNERSHIP_NONCE=$nonce" \
        "IMAGE=$KUBECTL_ELBENCHO_IMAGE" "IMAGE_PULL_POLICY=$KUBECTL_IMAGE_PULL_POLICY" \
        "RUN_AS_USER=$KUBECTL_RUN_AS_USER" "RUN_AS_GROUP=$KUBECTL_RUN_AS_GROUP" \
        "PVC_NAME=$KUBECTL_PVC" "NODE_NAME=$node") || return 1
    kubectl_create_owned_object helper_uid Job "$helper_name" "$KUBECTL_NAMESPACE" \
        "$nonce" "$attempt_id" "$manifest" || return 1
    local deadline=$((SECONDS + 180)) terminal="" wait_rc
    while (( SECONDS < deadline )); do
        wait_rc=0
        KUBECTL_REQUEST_TIMEOUT_SECONDS=10 KUBECTL_PROCESS_TIMEOUT_SECONDS=15 \
            kubectl_job_terminal_state terminal "$helper_name" "$KUBECTL_NAMESPACE" \
                "$nonce" "$attempt_id" "$helper_uid" || wait_rc=$?
        [[ "$wait_rc" -eq 0 ]] && break
        [[ "$wait_rc" -eq 2 ]] || { rc=1; break; }
        sleep 1
    done
    if [[ "$terminal" != COMPLETE ]]; then
        echo "Error: Kubernetes runtime validation Job did not complete successfully" >&2
        KUBECTL_REQUEST_TIMEOUT_SECONDS=10 KUBECTL_PROCESS_TIMEOUT_SECONDS=20 \
            kubectl_run_bounded -n "$KUBECTL_NAMESPACE" logs "job/$helper_name" \
                --all-containers=true --tail=200 >&2 || true
        KUBECTL_REQUEST_TIMEOUT_SECONDS=10 KUBECTL_PROCESS_TIMEOUT_SECONDS=20 \
            kubectl_run_bounded -n "$KUBECTL_NAMESPACE" describe Job "$helper_name" \
                >&2 || true
        KUBECTL_REQUEST_TIMEOUT_SECONDS=10 KUBECTL_PROCESS_TIMEOUT_SECONDS=20 \
            kubectl_run_bounded -n "$KUBECTL_NAMESPACE" get events \
                --field-selector "involvedObject.name=$helper_name" \
                --sort-by=.metadata.creationTimestamp >&2 || true
        local readiness_reason=TIMEOUT
        kubectl_classify_readiness_failure readiness_reason "$KUBECTL_NAMESPACE" \
            "$attempt_id" || true
        KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
            validate-runtime runtime-validation "$readiness_reason" \
            "inspect the Job diagnostics, correct the fixture, and rerun validation" \
            no Job "$helper_name" "$KUBECTL_NAMESPACE" "$helper_uid" "" "" || true
        rc=1
    fi
    kubectl_delete_owned_object Job "$helper_name" "$KUBECTL_NAMESPACE" "$nonce" \
        "$attempt_id" "$helper_uid" || rc=1
    return "$rc"
}

kubectl_discover_candidate_nodes() {
    local selector="$1" output_path="$2"
    kubectl_validate_node_selector "$selector" || return 1
    [[ -n "$output_path" && ! -e "$output_path" ]] || return 1
    local jsonpath
    jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.uid}{"\t"}{.metadata.labels.kubernetes\.io/arch}{"\t"}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\n"}{end}'
    local rows
    rows=$(kubectl_run_observational get nodes -l "$selector" -o "jsonpath=$jsonpath") \
        || return 1
    local tmp="$output_path.tmp.${BASHPID:-$$}.$RANDOM"
    local name uid arch ready common_arch="" count=0
    while IFS=$'\t' read -r name uid arch ready; do
        [[ -n "$name" ]] || continue
        kubectl_validate_object_name "$name" \
            && kubectl_validate_uid "$uid" \
            && [[ "$arch" =~ ^(amd64|arm64)$ && "$ready" == True ]] || {
                rm -f -- "$tmp"
                echo "Error: selected Kubernetes node is not Ready with a supported architecture" >&2
                return 1
            }
        if [[ -z "$common_arch" ]]; then
            common_arch="$arch"
        elif [[ "$common_arch" != "$arch" ]]; then
            rm -f -- "$tmp"
            echo "Error: Kubernetes worker selector matched mixed architectures" >&2
            return 1
        fi
        printf '%s\t%s\t%s\n' "$name" "$uid" "$arch" >> "$tmp" || return 1
        count=$((count + 1))
    done <<< "$rows"
    [[ "$count" -gt 0 ]] || {
        rm -f -- "$tmp"
        echo "Error: Kubernetes selector matched no Ready nodes" >&2
        return 1
    }
    LC_ALL=C sort -u -o "$tmp" "$tmp"
    [[ $(wc -l < "$tmp") -eq "$count" ]] || {
        rm -f -- "$tmp"
        echo "Error: Kubernetes selector returned duplicate node evidence" >&2
        return 1
    }
    mv -n "$tmp" "$output_path" 2>/dev/null || {
        rm -f -- "$tmp"
        return 1
    }
    [[ ! -e "$tmp" ]] || {
        rm -f -- "$tmp"
        return 1
    }
}

kubectl_choose_coordinator_node() {
    local nodes_path="$1"
    [[ -f "$nodes_path" && ! -L "$nodes_path" ]] || return 1
    local -a nodes=()
    mapfile -t nodes < <(cut -f1 "$nodes_path")
    [[ ${#nodes[@]} -gt 0 ]] || return 1
    if [[ -n "${ORDER_NODES_ENABLED:-}" ]]; then
        printf '%s\n' "${nodes[0]}"
    else
        printf '%s\n' "${nodes[RANDOM % ${#nodes[@]}]}"
    fi
}

kubectl_wait_owned_ready_pod() {
    local namespace="$1" pod_name="$2" nonce="$3" run_id="$4"
    local deadline_seconds="${5:-${KUBECTL_READY_TIMEOUT_SECONDS:-120}}"
    local metadata_dir="${6:-}"
    kubectl_validate_namespace_name "$namespace" \
        && kubectl_validate_object_name "$pod_name" \
        && [[ "$deadline_seconds" =~ ^[1-9][0-9]*$ ]] || return 1
    local deadline=$((SECONDS + deadline_seconds)) remaining
    while (( SECONDS < deadline )); do
        remaining=$((deadline - SECONDS))
        if KUBECTL_OBSERVATION_ATTEMPTS=1 \
            KUBECTL_REQUEST_TIMEOUT_SECONDS=$(( remaining < 10 ? remaining : 10 )) \
            KUBECTL_PROCESS_TIMEOUT_SECONDS="$remaining" \
            kubectl_verify_object_identity Pod "$pod_name" "$namespace" "$nonce" "$run_id" >/dev/null \
            && KUBECTL_REQUEST_TIMEOUT_SECONDS=$(( remaining < 10 ? remaining : 10 )) \
                KUBECTL_PROCESS_TIMEOUT_SECONDS="$remaining" \
                kubectl_run_bounded -n "$namespace" wait --for=condition=Ready "pod/$pod_name" \
                --timeout=10s >/dev/null; then
            return 0
        fi
        sleep 1
    done
    echo "Error: timed out waiting for owned Pod $pod_name to become Ready" >&2
    local diagnostic_path="" readiness_reason=TIMEOUT
    if [[ -n "$metadata_dir" ]]; then
        diagnostic_path=$(kubectl_capture_resource_diagnostics "$metadata_dir" \
            helper-not-ready "$namespace" Pod "$pod_name" "$run_id") || diagnostic_path=""
    fi
    kubectl_classify_readiness_failure readiness_reason "$namespace" "$run_id" || true
    KUBECTL_ATTEMPT_ID="$run_id" \
        KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="${metadata_dir:+$metadata_dir/state.sh}" \
        kubectl_report_lifecycle_error create-helper helper-readiness \
            "$readiness_reason" \
            "inspect diagnostics, correct Pod scheduling or startup, and retry" \
            no Pod "$pod_name" "$namespace" "" "" "$diagnostic_path" || true
    return 1
}

kubectl_create_helper_pod() {
    local output_variable="$1" template_name="$2" namespace="$3" name="$4"
    local nonce="$5" run_id="$6" node_name="$7" operation_token="$8"
    local kubernetes_dir="${9:-}" lock_fd="${10:-}" resource_key="${11:-}"
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
        && [[ "$template_name" =~ ^(transfer|status|collector)$ ]] \
        && kubectl_validate_namespace_name "$namespace" \
        && kubectl_validate_object_name "$name" \
        && kubectl_validate_object_name "$node_name" \
        && [[ "$operation_token" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || return 1
    local template
    template="$(kubectl_template_directory)/$template_name-pod.yaml.tmpl"
    local manifest _kubectl_created_helper_uid remote_run
    remote_run=$(kubectl_attempt_remote_root "$run_id") || return 1
    manifest=$(kubectl_render_attempt_template "$template" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$name" "ATTEMPT_ID=$run_id" \
        "OWNERSHIP_NONCE=$nonce" "OPERATION_TOKEN=$operation_token" \
        "REMOTE_RUN_DIRECTORY=$remote_run" \
        "IMAGE=$KUBECTL_ELBENCHO_IMAGE" "IMAGE_PULL_POLICY=$KUBECTL_IMAGE_PULL_POLICY" \
        "RUN_AS_USER=$KUBECTL_RUN_AS_USER" "RUN_AS_GROUP=$KUBECTL_RUN_AS_GROUP" \
        "PVC_NAME=$KUBECTL_PVC" "NODE_NAME=$node_name") || return 1
    kubectl_create_owned_object _kubectl_created_helper_uid Pod "$name" "$namespace" \
        "$nonce" "$run_id" "$manifest" "$kubernetes_dir" "$lock_fd" "$resource_key" || return 1
    printf -v "$output_variable" '%s' "$_kubectl_created_helper_uid"
    if [[ -n "$kubernetes_dir" ]]; then
        kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$run_id" \
            "$resource_key" Pod "$name" "$namespace" \
            "$_kubectl_created_helper_uid" "$nonce" || {
                kubectl_delete_owned_object Pod "$name" "$namespace" "$nonce" \
                    "$run_id" "$_kubectl_created_helper_uid" || true
                return 1
            }
        kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
            "$run_id" "$resource_key" || return 1
    fi
    if ! kubectl_wait_owned_ready_pod "$namespace" "$name" "$nonce" "$run_id" \
            "${KUBECTL_READY_TIMEOUT_SECONDS:-120}" \
            "${kubernetes_dir:+$kubernetes_dir/attempts/$run_id}"; then
        if [[ -z "$kubernetes_dir" ]]; then
            kubectl_delete_owned_object Pod "$name" "$namespace" "$nonce" "$run_id" \
                "$_kubectl_created_helper_uid" || true
        fi
        return 1
    fi
}

kubectl_pvc_exec() {
    local namespace="$1" pod_name="$2"
    shift 2
    kubectl_validate_namespace_name "$namespace" && kubectl_validate_object_name "$pod_name" \
        || return 1
    # Keep stdin attached: control bundles and result archives use the same
    # bounded exec adapter as ordinary status commands.
    kubectl_run_bounded -n "$namespace" exec -i "$pod_name" -- "$@"
}

kubectl_validate_pvc_paths() {
    local namespace="$1" pod_name="$2"
    shift 2
    [[ $# -gt 0 ]] || return 1
    local path
    for path in "$@"; do
        [[ "$path" == "$KUBECTL_SWEEP_MOUNT_ROOT/"* ]] || return 1
    done
    # Resolve the nearest existing parent as well as the future path. This
    # rejects a symlinked component before benchmark creation can escape the
    # PVC, while preserving support for intentionally not-yet-created paths.
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu '
        root=$(realpath -e -- "$1")
        reserved=$(realpath -m -- "$root/.storage-scale-test")
        case "$reserved" in "$root"/*) ;; *) exit 1 ;; esac
        shift
        for candidate in "$@"; do
            resolved=$(realpath -m -- "$candidate")
            existing=$candidate
            while [[ ! -e "$existing" ]]; do
                parent=${existing%/*}
                [[ -n "$parent" && "$parent" != "$existing" ]] || exit 1
                existing=$parent
            done
            existing=$(realpath -e -- "$existing")
            case "$resolved" in
                "$root"/*) ;;
                *) printf "path escapes PVC mount: %s\\n" "$candidate" >&2; exit 1 ;;
            esac
            case "$existing" in
                "$root"|"$root"/*) ;;
                *) printf "path has an unsafe existing parent: %s\\n" "$candidate" >&2; exit 1 ;;
            esac
            if [[ -n "$reserved" ]]; then
                case "$resolved" in
                    "$reserved"|"$reserved"/*)
                        printf "workload path overlaps orchestration state: %s\\n" "$candidate" >&2
                        exit 1
                        ;;
                esac
                case "$reserved" in
                    "$resolved"|"$resolved"/*)
                        printf "orchestration state overlaps workload path: %s\\n" "$candidate" >&2
                        exit 1
                        ;;
                esac
            fi
        done
    ' bash "$KUBECTL_SWEEP_MOUNT_ROOT" "$@"
}

kubectl_reserve_remote_attempt() {
    local namespace="$1" pod_name="$2" attempt_id="$3" nonce="$4"
    kubectl_validate_namespace_name "$namespace" \
        && kubectl_validate_object_name "$pod_name" \
        && [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && "$nonce" =~ ^[0-9a-f]{32}$ ]] || return 1
    local remote_run lock_dir
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    lock_dir=$(kubectl_attempt_remote_lock_directory) || return 1
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu '
        lock=$1 run=$2 attempt=$3 nonce=$4
        root=${lock%/locks/kubernetes-elbencho-sweep}
        mount=${root%/.storage-scale-test}
        [[ "$root" == /mnt/storage-scale-test/.storage-scale-test \
            && -d "$mount" && ! -L "$mount" ]] || exit 1
        mount_real=$(realpath -e -- "$mount") || exit 1
        root_real=$(realpath -m -- "$root") || exit 1
        [[ "$root_real" == "$mount_real/.storage-scale-test" ]] || exit 1
        umask 077
        pending="$lock.pending.$attempt.$nonce"
        made_pending=0 made_lock=0 made_run=0
        cleanup() {
            [[ $made_run -eq 0 ]] || rm -rf -- "$run"
            [[ $made_lock -eq 0 ]] || rm -rf -- "$lock"
            [[ $made_pending -eq 0 ]] || rm -rf -- "$pending"
        }
        trap cleanup EXIT
        for path in "$root" "$root/locks" "$root/runs"; do
            if [[ -e "$path" || -L "$path" ]]; then
                [[ -d "$path" && ! -L "$path" ]] || exit 1
            else
                mkdir -- "$path"
            fi
        done
        root_real=$(realpath -e -- "$root") || exit 1
        [[ "$root_real" == "$mount_real/.storage-scale-test" ]] || exit 1
        # Publish a fully initialized lock directory in one same-filesystem
        # rename. A killed writer must never leave the canonical lock without
        # the identity needed by exact recovery.
        [[ ! -e "$pending" && ! -L "$pending" ]] || exit 1
        mkdir -- "$pending"
        made_pending=1
        printf "%s\\t%s\\n" "$attempt" "$nonce" > "$pending/owner"
        mv -T -n -- "$pending" "$lock"
        [[ ! -e "$pending" && ! -L "$pending" ]] || exit 1
        made_pending=0
        made_lock=1
        mkdir -- "$run"
        made_run=1
        trap - EXIT
    ' bash "$lock_dir" "$remote_run" "$attempt_id" "$nonce"
}

kubectl_attempt_journal_remote_reservation() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" nonce="$4"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && "$nonce" =~ ^[0-9a-f]{32}$ ]] || return 1
    local attempt_dir="$kubernetes_dir/attempts/$attempt_id"
    kubectl_attempt_load_identity "$attempt_dir" || return 1
    [[ "$KUBECTL_OWNERSHIP_NONCE" == "$nonce" ]] || return 1
    local reservation_file="$attempt_dir/remote-reservation.sh"
    [[ ! -e "$reservation_file" && ! -L "$reservation_file" ]] || return 1
    local remote_run lock_dir tmp
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    lock_dir=$(kubectl_attempt_remote_lock_directory) || return 1
    tmp="$reservation_file.tmp.${BASHPID:-$$}.$RANDOM"
    {
        printf '# Trusted storage-scale-test Kubernetes remote reservation.\n'
        printf 'KUBECTL_REMOTE_ATTEMPT_ID=%q\n' "$attempt_id"
        printf 'KUBECTL_REMOTE_OWNERSHIP_NONCE=%q\n' "$nonce"
        printf 'KUBECTL_REMOTE_RUN_DIRECTORY=%q\n' "$remote_run"
        printf 'KUBECTL_REMOTE_LOCK_DIRECTORY=%q\n' "$lock_dir"
        printf 'KUBECTL_REMOTE_RESERVATION_PHASE=%q\n' INTENDED
    } > "$tmp" || return 1
    mv -n "$tmp" "$reservation_file" 2>/dev/null || {
        rm -f -- "$tmp"
        return 1
    }
    [[ ! -e "$tmp" ]] || {
        rm -f -- "$tmp"
        return 1
    }
}

kubectl_attempt_load_remote_reservation() {
    local kubernetes_dir="$1" attempt_id="$2"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local reservation_file="$kubernetes_dir/attempts/$attempt_id/remote-reservation.sh"
    [[ -f "$reservation_file" && ! -L "$reservation_file" ]] || return 1
    unset KUBECTL_REMOTE_ATTEMPT_ID KUBECTL_REMOTE_OWNERSHIP_NONCE
    unset KUBECTL_REMOTE_RUN_DIRECTORY KUBECTL_REMOTE_LOCK_DIRECTORY
    unset KUBECTL_REMOTE_RESERVATION_PHASE
    # shellcheck disable=SC1090  # Trusted, result-directory-local reservation.
    source "$reservation_file" || return 1
    [[ "$KUBECTL_REMOTE_ATTEMPT_ID" == "$attempt_id" \
        && "$KUBECTL_REMOTE_OWNERSHIP_NONCE" =~ ^[0-9a-f]{32}$ \
        && "$KUBECTL_REMOTE_RESERVATION_PHASE" =~ ^(INTENDED|ACQUIRED)$ ]] || return 1
    [[ "$KUBECTL_REMOTE_RUN_DIRECTORY" == "$(kubectl_attempt_remote_root "$attempt_id")" \
        && "$KUBECTL_REMOTE_LOCK_DIRECTORY" == "$(kubectl_attempt_remote_lock_directory)" ]]
}

kubectl_attempt_mark_remote_reservation_acquired() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_attempt_load_remote_reservation "$kubernetes_dir" "$attempt_id" || return 1
    [[ "$KUBECTL_REMOTE_RESERVATION_PHASE" == INTENDED ]] || return 1
    local reservation_file="$kubernetes_dir/attempts/$attempt_id/remote-reservation.sh"
    local tmp="$reservation_file.tmp.${BASHPID:-$$}.$RANDOM"
    sed 's/^KUBECTL_REMOTE_RESERVATION_PHASE=.*/KUBECTL_REMOTE_RESERVATION_PHASE=ACQUIRED/' \
        "$reservation_file" > "$tmp" \
        && mv -f "$tmp" "$reservation_file"
}

kubectl_release_remote_attempt() {
    local namespace="$1" pod_name="$2" attempt_id="$3" nonce="$4"
    local kubernetes_dir="${5:-}" lock_fd="${6:-}"
    kubectl_validate_namespace_name "$namespace" \
        && kubectl_validate_object_name "$pod_name" \
        && [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && "$nonce" =~ ^[0-9a-f]{32}$ ]] || return 1
    local remote_run lock_dir reservation_phase=ACQUIRED
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    lock_dir=$(kubectl_attempt_remote_lock_directory) || return 1
    if [[ -n "$kubernetes_dir" || -n "$lock_fd" ]]; then
        _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
        kubectl_attempt_load_remote_reservation "$kubernetes_dir" "$attempt_id" || return 1
        [[ "$KUBECTL_REMOTE_OWNERSHIP_NONCE" == "$nonce" ]] || return 1
        remote_run="$KUBECTL_REMOTE_RUN_DIRECTORY"
        lock_dir="$KUBECTL_REMOTE_LOCK_DIRECTORY"
        reservation_phase="$KUBECTL_REMOTE_RESERVATION_PHASE"
    fi
    if [[ -z "$kubernetes_dir" ]] || ! kubectl_attempt_step_done \
            "$kubernetes_dir" "$attempt_id" release-remote-run; then
        # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
        kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu '
            lock=$1 run=$2 attempt=$3 nonce=$4 phase=$5
            expected=$(printf "%s\\t%s" "$attempt" "$nonce")
            pending="$lock.pending.$attempt.$nonce"
            case "$run" in /mnt/storage-scale-test/.storage-scale-test/runs/????????) ;; *) exit 1 ;; esac
            mount=/mnt/storage-scale-test
            root=$mount/.storage-scale-test
            mount_real=$(realpath -e -- "$mount") || exit 1
            if [[ ! -e "$root" && ! -L "$root" ]]; then
                [[ ! -e "$pending" && ! -L "$pending" \
                    && ! -e "$lock" && ! -L "$lock" \
                    && ! -e "$run" && ! -L "$run" ]]
                exit
            fi
            [[ -d "$root" && ! -L "$root" ]] || exit 1
            root_real=$(realpath -e -- "$root") || exit 1
            [[ "$root_real" == "$mount_real/.storage-scale-test" ]] || exit 1
            if [[ -e "$pending" || -L "$pending" ]]; then
                [[ -d "$pending" && ! -L "$pending" ]] || exit 1
                if [[ -e "$pending/owner" || -L "$pending/owner" ]]; then
                    [[ -f "$pending/owner" && ! -L "$pending/owner" \
                        && $(cat -- "$pending/owner") == "$expected" \
                        && $(find "$pending" -mindepth 1 -maxdepth 1 -printf . | wc -c) -eq 1 ]] \
                        || exit 1
                    rm -- "$pending/owner" || exit 1
                fi
                rmdir -- "$pending"
                [[ ! -e "$pending" && ! -L "$pending" ]] || exit 1
            fi
            if [[ ! -e "$lock" && ! -L "$lock" && ! -e "$run" && ! -L "$run" ]]; then
                exit 0
            fi
            if [[ -e "$lock" || -L "$lock" ]]; then
                [[ -d "$lock" && ! -L "$lock" ]] || exit 1
                lock_real=$(realpath -e -- "$lock") || exit 1
                [[ "$lock_real" == "$root_real/locks/kubernetes-elbencho-sweep" ]] || exit 1
            fi
            if [[ "$phase" == INTENDED && -d "$lock" && ! -L "$lock" \
                    && -f "$lock/owner" && ! -L "$lock/owner" \
                    && $(cat -- "$lock/owner") != "$expected" ]]; then
                [[ ! -e "$run" && ! -L "$run" ]] || exit 1
                exit 0
            fi
            if [[ -e "$run" || -L "$run" ]]; then
                [[ -d "$run" && ! -L "$run" ]] || exit 1
                run_real=$(realpath -e -- "$run") || exit 1
                [[ "$run_real" == "$root_real/runs/$attempt" ]] || exit 1
            fi
            [[ -d "$lock" && ! -L "$lock" \
                && -f "$lock/owner" && ! -L "$lock/owner" \
                && $(cat -- "$lock/owner") == "$expected" ]] || exit 1
            rm -rf -- "$run"
            [[ ! -e "$run" && ! -L "$run" ]]
        ' bash "$lock_dir" "$remote_run" "$attempt_id" "$nonce" \
            "$reservation_phase" || return 1
        [[ -z "$kubernetes_dir" ]] || kubectl_attempt_journal_step \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" release-remote-run || return 1
    fi
    if [[ -z "$kubernetes_dir" ]] || ! kubectl_attempt_step_done \
            "$kubernetes_dir" "$attempt_id" release-remote-lock; then
        # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
        kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu '
            lock=$1 run=$2 attempt=$3 nonce=$4 phase=$5
            expected=$(printf "%s\\t%s" "$attempt" "$nonce")
            pending="$lock.pending.$attempt.$nonce"
            [[ ! -e "$run" && ! -L "$run" ]] || exit 1
            if [[ -e "$pending" || -L "$pending" ]]; then
                [[ -d "$pending" && ! -L "$pending" ]] || exit 1
                if [[ -e "$pending/owner" || -L "$pending/owner" ]]; then
                    [[ -f "$pending/owner" && ! -L "$pending/owner" \
                        && $(cat -- "$pending/owner") == "$expected" \
                        && $(find "$pending" -mindepth 1 -maxdepth 1 -printf . | wc -c) -eq 1 ]] \
                        || exit 1
                    rm -- "$pending/owner" || exit 1
                fi
                rmdir -- "$pending"
                [[ ! -e "$pending" && ! -L "$pending" ]] || exit 1
            fi
            if [[ ! -e "$lock" && ! -L "$lock" ]]; then
                exit 0
            fi
            mount=/mnt/storage-scale-test
            root=$mount/.storage-scale-test
            mount_real=$(realpath -e -- "$mount") || exit 1
            if [[ ! -e "$root" && ! -L "$root" ]]; then
                [[ ! -e "$pending" && ! -L "$pending" \
                    && ! -e "$lock" && ! -L "$lock" ]]
                exit
            fi
            [[ -d "$root" && ! -L "$root" ]] || exit 1
            root_real=$(realpath -e -- "$root") || exit 1
            [[ "$root_real" == "$mount_real/.storage-scale-test" ]] || exit 1
            [[ -d "$lock" && ! -L "$lock" ]] || exit 1
            lock_real=$(realpath -e -- "$lock") || exit 1
            [[ "$lock_real" == "$root_real/locks/kubernetes-elbencho-sweep" ]] || exit 1
            if [[ "$phase" == INTENDED \
                    && -f "$lock/owner" && ! -L "$lock/owner" \
                    && $(cat -- "$lock/owner") != "$expected" ]]; then
                exit 0
            fi
            [[ -d "$lock" && ! -L "$lock" \
                && -f "$lock/owner" && ! -L "$lock/owner" \
                && $(cat -- "$lock/owner") == "$expected" ]] || exit 1
            rm -rf -- "$lock"
            [[ ! -e "$lock" && ! -L "$lock" ]]
        ' bash "$lock_dir" "$remote_run" "$attempt_id" "$nonce" \
            "$reservation_phase" || return 1
        [[ -z "$kubernetes_dir" ]] || kubectl_attempt_journal_step \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" release-remote-lock || return 1
    fi
}

kubectl_release_journaled_remote_attempt() {
    local kubernetes_dir="$1" lock_fd="$2" namespace="$3" pod_name="$4" attempt_id="$5"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_attempt_load_remote_reservation "$kubernetes_dir" "$attempt_id" || return 1
    kubectl_release_remote_attempt "$namespace" "$pod_name" "$attempt_id" \
        "$KUBECTL_REMOTE_OWNERSHIP_NONCE" "$kubernetes_dir" "$lock_fd"
}

kubectl_discover_worker_endpoints() {
    local namespace="$1" run_id="$2" nodes_path="$3" output_path="$4"
    kubectl_validate_namespace_name "$namespace" \
        && [[ "$run_id" =~ ^[0-9a-f]{8}$ ]] \
        && [[ -f "$nodes_path" && ! -L "$nodes_path" && ! -e "$output_path" ]] || return 1
    local node_jsonpath
    node_jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.uid}{"\t"}{.metadata.labels.kubernetes\.io/arch}{"\t"}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\t"}{.metadata.deletionTimestamp}{"\n"}{end}'
    local node_rows
    node_rows=$(kubectl_run_observational get nodes -o "jsonpath=$node_jsonpath") || return 1
    local -A expected_nodes=() live_node_uid=() live_node_arch=()
    local node uid arch ready deletion_timestamp
    while IFS=$'\t' read -r node uid arch; do
        [[ -n "$node" && ! -v expected_nodes["$node"] ]] || return 1
        kubectl_validate_object_name "$node" && kubectl_validate_uid "$uid" \
            && [[ "$arch" =~ ^(amd64|arm64)$ ]] || return 1
        expected_nodes["$node"]=1
    done < "$nodes_path"
    while IFS=$'\t' read -r node uid arch ready deletion_timestamp; do
        [[ -n "$node" && -v expected_nodes["$node"] ]] || continue
        [[ -z "$deletion_timestamp" && "$ready" == True \
            && ! -v live_node_uid["$node"] ]] || return 1
        kubectl_validate_uid "$uid" && [[ "$arch" =~ ^(amd64|arm64)$ ]] || return 1
        live_node_uid["$node"]="$uid"
        live_node_arch["$node"]="$arch"
    done <<< "$node_rows"
    [[ "${#live_node_uid[@]}" -eq "${#expected_nodes[@]}" ]] || return 1
    local jsonpath
    # Put the optional deletion timestamp last. Bash collapses adjacent IFS
    # whitespace, so an empty field in the middle would shift readiness and
    # image evidence into the wrong columns.
    jsonpath='{range .items[*]}{.spec.nodeName}{"\t"}{.metadata.name}{"\t"}{.metadata.uid}{"\t"}{.status.podIP}{"\t"}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\t"}{range .status.containerStatuses[?(@.name=="elbencho")]}{.imageID}{end}{"\t"}{.metadata.deletionTimestamp}{"\n"}{end}'
    local rows
    rows=$(kubectl_run_observational -n "$namespace" get pods \
        -l "storage-scale-test.nvidia.com/run=$run_id,app.kubernetes.io/component=workers" \
        -o "jsonpath=$jsonpath") || return 1
    local tmp="$output_path.tmp.${BASHPID:-$$}.$RANDOM"
    local -A seen=() seen_ip=()
    local pod pod_uid ip image_id count=0
    while IFS=$'\t' read -r node pod pod_uid ip ready image_id deletion_timestamp; do
        [[ -n "$node" ]] || continue
        # A DaemonSet rollout can briefly expose the terminating Pod beside
        # its Ready replacement.  Only nonterminating Pods are candidates;
        # duplicate live candidates still fail closed below.
        [[ -z "$deletion_timestamp" ]] || continue
        if ! [[ -v live_node_uid["$node"] && ! -v seen["$node"] \
            && "$ready" == True \
            && ! -v seen_ip["$ip"] && -n "$image_id" ]] \
            || ! kubectl_validate_ipv4 "$ip" \
            || ! kubectl_validate_object_name "$pod" \
            || ! kubectl_validate_uid "$pod_uid"; then
            rm -f -- "$tmp"
            echo "Error: worker DaemonSet Pod evidence is incomplete or conflicting" >&2
            return 1
        fi
        seen["$node"]=1
        seen_ip["$ip"]=1
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$node" "${live_node_uid[$node]}" \
            "$pod" "$pod_uid" "$ip" "${live_node_arch[$node]}" "$image_id" >> "$tmp" || return 1
        count=$((count + 1))
    done <<< "$rows"
    [[ "$count" -eq "${#live_node_uid[@]}" ]] || {
        rm -f -- "$tmp"
        echo "Error: worker DaemonSet does not have exactly one Ready Pod per recorded node" >&2
        return 1
    }
    LC_ALL=C sort -o "$tmp" "$tmp"
    mv -n "$tmp" "$output_path" 2>/dev/null || {
        rm -f -- "$tmp"
        return 1
    }
    [[ ! -e "$tmp" ]] || {
        rm -f -- "$tmp"
        return 1
    }
}

kubectl_validate_ipv4() {
    local value="$1" octet
    [[ "$value" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || return 1
    local -a octets=()
    IFS=. read -ra octets <<< "$value"
    for octet in "${octets[@]}"; do
        [[ "$octet" =~ ^(0|[1-9][0-9]{0,2})$ && "$octet" -le 255 ]] || return 1
    done
}

kubectl_wait_worker_endpoints() {
    local namespace="$1" run_id="$2" nodes_path="$3" output_path="$4"
    local deadline_seconds="${5:-${KUBECTL_WORKER_READY_TIMEOUT_SECONDS:-180}}"
    local metadata_dir="${6:-}"
    [[ "$deadline_seconds" =~ ^[1-9][0-9]*$ ]] || return 1
    local deadline=$((SECONDS + deadline_seconds))
    local remaining
    while (( SECONDS < deadline )); do
        remaining=$((deadline - SECONDS))
        if KUBECTL_OBSERVATION_ATTEMPTS=1 \
            KUBECTL_REQUEST_TIMEOUT_SECONDS=$(( remaining < 10 ? remaining : 10 )) \
            KUBECTL_PROCESS_TIMEOUT_SECONDS="$remaining" \
            kubectl_discover_worker_endpoints "$namespace" "$run_id" "$nodes_path" "$output_path" \
            && kubectl_validate_worker_endpoint_nodes "$output_path" "$nodes_path" \
            && kubectl_validate_worker_endpoint_images "$output_path" \
                "${KUBECTL_EXPECTED_IMAGE_ID:-}"; then
            return 0
        fi
        rm -f -- "$output_path"
        sleep 1
    done
    echo "Error: timed out waiting for the Kubernetes worker DaemonSet" >&2
    local daemonset_name="sst-elb-$run_id-workers"
    local diagnostic_path="" readiness_reason=TIMEOUT
    if [[ -n "$metadata_dir" ]]; then
        diagnostic_path=$(kubectl_capture_resource_diagnostics "$metadata_dir" \
            workers-not-ready "$namespace" DaemonSet "$daemonset_name" "$run_id") \
            || diagnostic_path=""
    fi
    kubectl_classify_readiness_failure readiness_reason "$namespace" "$run_id" || true
    KUBECTL_ATTEMPT_ID="$run_id" \
        KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="${metadata_dir:+$metadata_dir/state.sh}" \
        kubectl_report_lifecycle_error start-workers worker-readiness \
            "$readiness_reason" \
            "inspect diagnostics, correct DaemonSet placement or startup, and retry" \
            no DaemonSet "$daemonset_name" "$namespace" "" "" \
            "$diagnostic_path" || true
    return 1
}

kubectl_compare_worker_endpoints() {
    local namespace="$1" run_id="$2" nodes_path="$3" frozen_path="$4"
    local output_path="$5"
    kubectl_validate_namespace_name "$namespace" \
        && [[ "$run_id" =~ ^[0-9a-f]{8}$ ]] \
        && [[ -f "$nodes_path" && ! -L "$nodes_path" ]] \
        && [[ -f "$frozen_path" && ! -L "$frozen_path" ]] \
        && [[ -n "$output_path" ]] || return 1
    _kubectl_validate_local_directory_path "${output_path%/*}" \
        && { [[ ! -e "$output_path" ]] \
            || [[ -f "$output_path" && ! -L "$output_path" ]]; } || return 1
    local current_path="$output_path.current.${BASHPID:-$$}.${RANDOM}"
    kubectl_discover_worker_endpoints "$namespace" "$run_id" "$nodes_path" \
        "$current_path" || {
        rm -f -- "$current_path"
        return 1
    }
    local tmp="$output_path.tmp.${BASHPID:-$$}.${RANDOM}"
    local node frozen_node_uid frozen_pod frozen_uid frozen_ip frozen_arch frozen_image frozen_extra
    local current_node current_node_uid current_pod current_uid current_ip current_arch current_image
    local current_extra status
    local drift=0 parse_error=0
    declare -A current_rows=() seen_frozen=()
    while IFS=$'\t' read -r current_node current_node_uid current_pod current_uid \
            current_ip current_arch current_image current_extra; do
        [[ -z "${current_extra:-}" && -n "$current_node" ]] || {
            parse_error=1
            break
        }
        current_rows["$current_node"]="${current_node_uid}"$'\t'"${current_pod}"$'\t'"${current_uid}"$'\t'"${current_ip}"$'\t'"${current_arch}"$'\t'"${current_image}"
    done < "$current_path"
    if [[ "$parse_error" -ne 0 ]]; then
        rm -f -- "$current_path" "$tmp"
        return 1
    fi
    : > "$tmp" || {
        rm -f -- "$current_path"
        return 1
    }
    while IFS=$'\t' read -r node frozen_node_uid frozen_pod frozen_uid frozen_ip \
            frozen_arch frozen_image frozen_extra; do
        [[ -z "${frozen_extra:-}" && -n "$node" ]] || {
            rm -f -- "$current_path" "$tmp"
            return 1
        }
        [[ ! -v seen_frozen["$node"] ]] || {
            rm -f -- "$current_path" "$tmp"
            return 1
        }
        seen_frozen["$node"]=1
        if [[ ! -v current_rows["$node"] ]]; then
            status=MISSING
            printf '%s\t%s\t%s\t%s\t-\t-\t-\t%s\n' \
                "$node" "$frozen_pod" "$frozen_uid" "$frozen_ip" "$status" >> "$tmp"
            drift=1
            continue
        fi
        IFS=$'\t' read -r current_node_uid current_pod current_uid current_ip \
            current_arch current_image <<< "${current_rows[$node]}"
        if [[ "$current_node_uid" != "$frozen_node_uid" ]]; then
            status=NODE_DRIFT
            drift=1
        elif [[ "$current_arch" != "$frozen_arch" ]]; then
            status=ARCH_DRIFT
            drift=1
        elif [[ "$current_image" != "$frozen_image" ]]; then
            status=IMAGE_DRIFT
            drift=1
        elif [[ "$current_ip" != "$frozen_ip" ]]; then
            status=IP_DRIFT
            drift=1
        elif [[ "$current_uid" != "$frozen_uid" || "$current_pod" != "$frozen_pod" ]]; then
            status=REPLACED_SAME_IP
        else
            status=UNCHANGED
        fi
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
            "$node" "$frozen_pod" "$frozen_uid" "$frozen_ip" \
            "$current_pod" "$current_uid" "$current_ip" "$status" >> "$tmp"
    done < "$frozen_path"
    for current_node in "${!current_rows[@]}"; do
        [[ -v seen_frozen["$current_node"] ]] || {
            IFS=$'\t' read -r current_node_uid current_pod current_uid current_ip \
                current_arch current_image <<< \
                "${current_rows[$current_node]}"
            printf '%s\t-\t-\t-\t%s\t%s\t%s\tUNEXPECTED\n' \
                "$current_node" "$current_pod" "$current_uid" "$current_ip" >> "$tmp"
            drift=1
        }
    done
    rm -f -- "$current_path"
    mv -f -- "$tmp" "$output_path" || {
        rm -f -- "$tmp"
        return 1
    }
    [[ "$drift" -eq 0 ]] || return 2
}

kubectl_validate_worker_endpoint_nodes() {
    local endpoints_path="$1" nodes_path="$2"
    [[ -f "$endpoints_path" && ! -L "$endpoints_path" \
        && -f "$nodes_path" && ! -L "$nodes_path" ]] || return 1
    local expected current
    expected=$(cut -f1-3 "$nodes_path" | LC_ALL=C sort) || return 1
    current=$(cut -f1,2,6 "$endpoints_path" | LC_ALL=C sort) || return 1
    [[ -n "$expected" && "$current" == "$expected" ]] || {
        echo "Error: Kubernetes worker node identity changed during startup" >&2
        return 1
    }
}

kubectl_validate_worker_endpoint_images() {
    local endpoints_path="$1" expected_image_id="${2:-}"
    [[ -f "$endpoints_path" && ! -L "$endpoints_path" ]] || return 1
    local common="" image_id
    while IFS=$'\t' read -r _ _ _ _ _ _ image_id; do
        [[ -n "$image_id" ]] || return 1
        if [[ -z "$common" ]]; then
            common="$image_id"
        elif [[ "$common" != "$image_id" ]]; then
            echo "Error: Kubernetes worker Pods resolved different container images" >&2
            return 1
        fi
    done < "$endpoints_path"
    [[ -n "$common" && ( -z "$expected_image_id" || "$common" == "$expected_image_id" ) ]] || {
        echo "Error: Kubernetes worker image does not match the expected resolved image" >&2
        return 1
    }
}

kubectl_delete_owned_object() {
    local kind="$1" name="$2" namespace="$3" nonce="$4" run_id="$5" expected_uid="$6"
    if ! kubectl_verify_object_identity "$kind" "$name" "$namespace" "$nonce" "$run_id" \
            "$expected_uid" >/dev/null; then
        # A completed prior delete is idempotent only when the API explicitly
        # reports absence. Any transport, authorization, or identity failure
        # remains fatal and must not be confused with a deleted resource.
        local observed_uid
        observed_uid=$(kubectl_run_observational -n "$namespace" get "$kind" "$name" \
            --ignore-not-found -o 'jsonpath={.metadata.uid}') || return 1
        [[ -z "$observed_uid" ]] || return 1
        return 0
    fi
    # Foreground deletion includes the Pod termination grace period. Keep it
    # bounded, but do not race the default 30-second Kubernetes grace period.
    KUBECTL_REQUEST_TIMEOUT_SECONDS=75 KUBECTL_PROCESS_TIMEOUT_SECONDS=90 \
        kubectl_run_bounded -n "$namespace" delete "$kind" "$name" \
            --wait=true --cascade=foreground >/dev/null
}

kubectl_job_is_terminal_failure() {
    local kind="$1" name="$2" namespace="$3" nonce="$4" run_id="$5" expected_uid="$6"
    [[ "$kind" == Job ]] || return 1
    local terminal_state=""
    kubectl_job_terminal_state terminal_state "$name" "$namespace" "$nonce" \
        "$run_id" "$expected_uid" || return 1
    [[ "$terminal_state" == FAILED ]]
}

kubectl_job_terminal_state() {
    local output_variable="$1" name="$2" namespace="$3" nonce="$4" run_id="$5"
    local expected_uid="$6"
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    kubectl_verify_object_identity Job "$name" "$namespace" "$nonce" "$run_id" \
        "$expected_uid" >/dev/null || return 1
    local evidence active succeeded failed complete_status failed_status
    evidence=$(kubectl_run_observational -n "$namespace" get Job "$name" \
        -o 'jsonpath={.status.active}{"|"}{.status.succeeded}{"|"}{.status.failed}{"|"}{range .status.conditions[?(@.type=="Complete")]}{.status}{end}{"|"}{range .status.conditions[?(@.type=="Failed")]}{.status}{end}') || return 1
    IFS='|' read -r active succeeded failed complete_status failed_status <<< "$evidence"
    [[ -z "$active" || "$active" == 0 ]] || return 2
    if [[ "$complete_status" == True && "$succeeded" =~ ^[1-9][0-9]*$ \
            && -z "$failed_status" ]]; then
        printf -v "$output_variable" '%s' COMPLETE
    elif [[ "$failed_status" == True && "$failed" =~ ^[1-9][0-9]*$ \
            && -z "$complete_status" ]]; then
        printf -v "$output_variable" '%s' FAILED
    elif [[ -z "$complete_status" && -z "$failed_status" ]]; then
        return 2
    else
        return 1
    fi
}

kubectl_wait_journaled_job_quiescent() {
    local kubernetes_dir="$1" attempt_id="$2"
    local timeout_seconds="${3:-${KUBECTL_JOB_QUIESCENCE_TIMEOUT_SECONDS:-$KUBECTL_JOB_QUIESCENCE_TIMEOUT_SECONDS_DEFAULT}}"
    local allow_absent="${4:-0}"
    [[ "$timeout_seconds" =~ ^[1-9][0-9]*$ && "$allow_absent" =~ ^[01]$ ]] || return 1
    kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" sweep || return 1
    [[ "$KUBECTL_RESOURCE_KIND" == Job ]] || return 1
    local deadline=$((SECONDS + timeout_seconds)) remaining per_call
    local observed_uid="" terminal_state="" rc
    while (( SECONDS < deadline )); do
        remaining=$((deadline - SECONDS))
        per_call=$((remaining / 3))
        (( per_call > 0 )) || per_call=1
        (( per_call <= 10 )) || per_call=10
        observed_uid=$(KUBECTL_OBSERVATION_ATTEMPTS=1 \
            KUBECTL_REQUEST_TIMEOUT_SECONDS="$per_call" \
            KUBECTL_PROCESS_TIMEOUT_SECONDS="$per_call" \
            kubectl_run_observational -n "$KUBECTL_RESOURCE_NAMESPACE" get Job \
                "$KUBECTL_RESOURCE_NAME" --ignore-not-found \
                -o 'jsonpath={.metadata.uid}') || return 1
        if [[ -z "$observed_uid" ]]; then
            [[ "$allow_absent" -eq 1 ]] \
                || kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" delete-sweep
            return
        fi
        [[ "$observed_uid" == "$KUBECTL_RESOURCE_UID" ]] || return 1
        remaining=$((deadline - SECONDS))
        (( remaining > 0 )) || break
        per_call=$((remaining / 2))
        (( per_call > 0 )) || per_call=1
        (( per_call <= 10 )) || per_call=10
        rc=0
        KUBECTL_OBSERVATION_ATTEMPTS=1 \
            KUBECTL_REQUEST_TIMEOUT_SECONDS="$per_call" \
            KUBECTL_PROCESS_TIMEOUT_SECONDS="$per_call" \
            kubectl_job_terminal_state terminal_state "$KUBECTL_RESOURCE_NAME" \
                "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" \
                "$attempt_id" "$KUBECTL_RESOURCE_UID" || rc=$?
        [[ "$rc" -eq 0 ]] && return 0
        [[ "$rc" -eq 2 ]] || return "$rc"
        sleep 1
    done
    echo "Error: timed out waiting for the exact Kubernetes sweep Job to become inactive" >&2
    KUBECTL_ATTEMPT_ID="$attempt_id" \
        KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="$kubernetes_dir/attempts/$attempt_id/state.sh" \
        kubectl_report_lifecycle_error collect job-quiescence TIMEOUT \
            "wait for the exact Job to become terminal, then retry collection" \
            yes Job "$KUBECTL_RESOURCE_NAME" "$KUBECTL_RESOURCE_NAMESPACE" \
            "$KUBECTL_RESOURCE_UID" "$observed_uid" "" || true
    return 1
}

kubectl_recover_lost_coordinator() {
    local namespace="$1" helper_pod="$2" attempt_id="$3"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local remote_run state scratch
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    state="$remote_run/state"
    scratch="/tmp/storage-scale-test/$attempt_id"
    local guard_script
    guard_script=$(kubectl_remote_tree_guard_script) || return 1
    # The recovery mode is part of the digest-verified coordinator bundle and
    # only edits a PVC attempt whose Job has already reached Failed. It does
    # not need API credentials or a new coordinator Pod.
    # shellcheck disable=SC2016  # Positional parameters expand in the helper Pod.
    kubectl_pvc_exec "$namespace" "$helper_pod" /bin/bash -ceu "$guard_script
        control=\$run/control
        state=\$run/state
        [[ -d \"\$control\" && ! -L \"\$control\" && -d \"\$state\" && ! -L \"\$state\" ]] || exit 1
        exec \"\$control/coordinator.sh\" \"\$4\" \"\$control\" \"\$state\" \"\$3\" \"\$2\"" \
        bash "$remote_run" "$attempt_id" "$scratch" --recover-lost
}

kubectl_finalize_cancelled_attempt() {
    local namespace="$1" helper_pod="$2" attempt_id="$3"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local remote_run state scratch
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    state="$remote_run/state"
    scratch="/tmp/storage-scale-test/$attempt_id"
    local guard_script
    guard_script=$(kubectl_remote_tree_guard_script) || return 1
    # The Job has already been deleted. Finalize its durable ledger through a
    # short-lived helper so cancellation remains collectable even when the
    # coordinator did not receive enough grace time to publish its own exit.
    # shellcheck disable=SC2016  # Positional parameters expand in the helper Pod.
    kubectl_pvc_exec "$namespace" "$helper_pod" /bin/bash -ceu "$guard_script
        control=\$run/control
        state=\$run/state
        [[ -d \"\$control\" && ! -L \"\$control\" && -d \"\$state\" && ! -L \"\$state\" ]] || exit 1
        exec \"\$control/coordinator.sh\" \"\$4\" \"\$control\" \"\$state\" \"\$3\" \"\$2\"" \
        bash "$remote_run" "$attempt_id" "$scratch" --finalize-cancelled
}

kubectl_record_remote_status() {
    local namespace="$1" pod_name="$2" attempt_id="$3" status="$4"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ \
        && "$status" =~ ^(PREPARED|RUNNING|SUCCESS|FAILED|CANCELLED)$ ]] || return 1
    local remote_run
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    local guard_script
    guard_script=$(kubectl_remote_tree_guard_script) || return 1
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu "$guard_script
        status=\$3
        [[ -d \"\$run/state\" && ! -L \"\$run/state\" ]] || exit 1
        state_real=\$(realpath -e -- \"\$run/state\") || exit 1
        [[ \"\$state_real\" == \"\$run_real/state\" ]] || exit 1
        tmp=\"\$run/state/run.status.tmp.\$\$\"
        printf \"%s\\\\n\" \"\$status\" > \"\$tmp\"
        mv -f -- \"\$tmp\" \"\$run/state/run.status\"
    " bash "$remote_run" "$attempt_id" "$status"
}

kubectl_read_remote_status() {
    local namespace="$1" pod_name="$2" attempt_id="$3"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local remote_run
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    local guard_script
    guard_script=$(kubectl_remote_tree_guard_script) || return 1
    local status
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    status=$(kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu \
        "$guard_script
        [[ -f \"\$run/state/run.status\" && ! -L \"\$run/state/run.status\" ]] || exit 1
        cat -- \"\$run/state/run.status\"" bash "$remote_run" "$attempt_id") || return 1
    [[ "$status" =~ ^(PREPARED|RUNNING|SUCCESS|FAILED|CANCELLED)$ ]] || return 1
    printf '%s\n' "$status"
}

kubectl_stream_remote_attempt() {
    local namespace="$1" pod_name="$2" attempt_id="$3" archive_path="$4"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && -n "$archive_path" && ! -e "$archive_path" ]] || return 1
    local collection_timeout="${KUBECTL_COLLECTION_TIMEOUT_SECONDS:-$KUBECTL_COLLECTION_TIMEOUT_SECONDS_DEFAULT}"
    [[ "$collection_timeout" =~ ^[1-9][0-9]*$ ]] || return 1
    local remote_run
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    local guard_script
    guard_script=$(kubectl_remote_tree_guard_script) || return 1
    umask 077
    # A valid collection can contain up to two GiB. Do not inherit the short
    # API-probe deadline used for status and object inspection. Keep at most
    # one byte beyond the limit so an oversized or unbounded producer is
    # stopped without first filling the local filesystem.
    KUBECTL_REQUEST_TIMEOUT_SECONDS="$collection_timeout" \
        KUBECTL_PROCESS_TIMEOUT_SECONDS=$((collection_timeout + 60)) \
        kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu "$guard_script
            exec tar -C \"\${run%/*}\" -cf - \"\${run##*/}\"" \
            bash "$remote_run" "$attempt_id" \
        | _kubectl_write_bounded_collection_stream "$archive_path" \
            "$KUBECTL_COLLECTION_MAX_BYTES"
    local -a stream_status=("${PIPESTATUS[@]}")
    if [[ "${stream_status[0]:-1}" -ne 0 || "${stream_status[1]:-1}" -ne 0 ]]; then
        rm -f -- "$archive_path"
        return 1
    fi
}

_kubectl_write_bounded_collection_stream() {
    local archive_path="$1" max_bytes="$2"
    [[ -n "$archive_path" && ! -e "$archive_path" \
        && "$max_bytes" =~ ^[1-9][0-9]*$ ]] || return 1
    if ! head -c "$((max_bytes + 1))" > "$archive_path"; then
        rm -f -- "$archive_path"
        echo "Error: failed to write the Kubernetes collection archive; check local free space" >&2
        return 1
    fi
    local archive_bytes
    archive_bytes=$(wc -c < "$archive_path") || {
        rm -f -- "$archive_path"
        return 1
    }
    if [[ ! "$archive_bytes" =~ ^[0-9]+$ || "$archive_bytes" -gt "$max_bytes" ]]; then
        rm -f -- "$archive_path"
        echo "Error: Kubernetes collection archive exceeded its byte limit while streaming" >&2
        return 1
    fi
}

kubectl_remote_attempt_apparent_bytes() {
    local namespace="$1" pod_name="$2" attempt_id="$3" output_name="$4"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ \
        && "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    local remote_run guard_script apparent_bytes
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    guard_script=$(kubectl_remote_tree_guard_script) || return 1
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    apparent_bytes=$(kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu \
        "$guard_script
        du -sb -- \"\$run\" | awk '{print \$1}'" \
        bash "$remote_run" "$attempt_id") || return 1
    [[ "$apparent_bytes" =~ ^[0-9]+$ \
        && "$apparent_bytes" -le "$KUBECTL_COLLECTION_MAX_BYTES" ]] || {
        echo "Error: Kubernetes collection source exceeds its byte limit" >&2
        return 1
    }
    printf -v "$output_name" '%s' "$apparent_bytes"
}

_kubectl_require_collection_capacity() {
    local results_dir="$1" apparent_bytes="$2"
    [[ -d "$results_dir" && ! -L "$results_dir" \
        && "$apparent_bytes" =~ ^[0-9]+$ ]] || return 1
    # Archive receipt, extraction staging, and result publication can briefly
    # coexist. Reserve all three copies plus tar/filesystem metadata headroom.
    local required_bytes=$((apparent_bytes * 3 + KUBECTL_COLLECTION_HEADROOM_BYTES))
    local available_blocks available_bytes
    available_blocks=$(df -Pk -- "$results_dir" | awk 'END {print $4}') || return 1
    [[ "$available_blocks" =~ ^[0-9]+$ ]] || return 1
    available_bytes=$((available_blocks * 1024))
    if (( available_bytes < required_bytes )); then
        echo "Error: insufficient local free space for Kubernetes collection" >&2
        printf 'Required: %s bytes; available below %s: %s bytes\n' \
            "$required_bytes" "$results_dir" "$available_bytes" >&2
        return 1
    fi
}

_kubectl_scavenge_collection_staging() {
    local results_dir="$1" candidate basename restore_nullglob=0
    _kubectl_validate_local_directory_path "$results_dir" \
        && [[ -d "$results_dir" && ! -L "$results_dir" ]] || return 1
    shopt -q nullglob && restore_nullglob=1
    shopt -s nullglob
    for candidate in "$results_dir"/.kubernetes-collect-*; do
        basename=${candidate##*/}
        [[ "$basename" =~ ^\.kubernetes-collect(-work)?-[0-9a-f]{8}\.[A-Za-z0-9]+$ ]] \
            || continue
        if [[ -L "$candidate" || ! -d "$candidate" ]]; then
            echo "Error: unsafe stale Kubernetes collection staging path: $candidate" >&2
            [[ "$restore_nullglob" -eq 1 ]] || shopt -u nullglob
            return 1
        fi
        rm -rf -- "$candidate" || {
            [[ "$restore_nullglob" -eq 1 ]] || shopt -u nullglob
            return 1
        }
    done
    [[ "$restore_nullglob" -eq 1 ]] || shopt -u nullglob
}

_kubectl_safe_archive_member() {
    local member="$1"
    [[ -n "$member" && "$member" != /* && "$member" != *$'\n'* \
        && "$member" != *$'\r'* && "$member" != *$'\t'* \
        && "$member" =~ ^[A-Za-z0-9._/-]+$ ]] || return 1
    local -a components=()
    local component
    IFS=/ read -ra components <<< "$member"
    for component in "${components[@]}"; do
        [[ -n "$component" && "$component" != . && "$component" != .. ]] || return 1
    done
}

_kubectl_safe_relative_file_path() {
    local path="$1"
    [[ "$path" != */ ]] && _kubectl_safe_archive_member "$path"
}

_kubectl_result_destination() {
    local output_name="$1" results_dir="$2" relative="$3" create_parents="$4"
    [[ "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
        && "$create_parents" =~ ^[01]$ ]] || return 1
    _kubectl_safe_relative_file_path "$relative" \
        && _kubectl_validate_local_directory_path "$results_dir" \
        && [[ -d "$results_dir" && ! -L "$results_dir" ]] || return 1
    local results_real current component index
    results_real=$(realpath -e -- "$results_dir") || return 1
    current="$results_dir"
    local -a components=()
    IFS=/ read -ra components <<< "$relative"
    for ((index = 0; index + 1 < ${#components[@]}; index++)); do
        component="${components[$index]}"
        current="$current/$component"
        if [[ -e "$current" || -L "$current" ]]; then
            [[ -d "$current" && ! -L "$current" ]] || return 1
        elif [[ "$create_parents" -eq 1 ]]; then
            mkdir -- "$current" || return 1
        else
            printf -v "$output_name" '%s' "$results_dir/$relative"
            return 0
        fi
    done
    local parent_real resolved_destination="$results_dir/$relative"
    parent_real=$(realpath -e -- "$current") || return 1
    [[ "$parent_real" == "$results_real" || "$parent_real" == "$results_real/"* ]] \
        || return 1
    [[ ! -L "$resolved_destination" ]] || return 1
    printf -v "$output_name" '%s' "$resolved_destination"
}

kubectl_validate_attempt_archive() {
    local archive_path="$1" attempt_id="$2"
    [[ -f "$archive_path" && ! -L "$archive_path" \
        && "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local archive_bytes
    archive_bytes=$(wc -c < "$archive_path") || return 1
    [[ "$archive_bytes" =~ ^[0-9]+$ && "$archive_bytes" -le "$KUBECTL_COLLECTION_MAX_BYTES" ]] || {
        echo "Error: Kubernetes collection archive exceeds its byte limit" >&2
        return 1
    }
    local listing_path
    listing_path=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-kubectl-list.XXXXXX") || return 1
    if ! tar -tf "$archive_path" > "$listing_path"; then
        rm -f -- "$listing_path"
        return 1
    fi
    local member_count=0 member archive_error=0
    while IFS= read -r member; do
        _kubectl_safe_archive_member "$member" || {
            echo "Error: unsafe member in Kubernetes collection archive" >&2
            archive_error=1
            break
        }
        [[ "$member" == "$attempt_id" || "$member" == "$attempt_id/"* ]] || {
            echo "Error: collection archive contains another attempt" >&2
            archive_error=1
            break
        }
        member_count=$((member_count + 1))
        [[ "$member_count" -le "$KUBECTL_COLLECTION_MAX_MEMBERS" ]] || {
            echo "Error: Kubernetes collection archive exceeds its member limit" >&2
            archive_error=1
            break
        }
    done < "$listing_path"
    [[ "$archive_error" -eq 0 && "$member_count" -gt 0 ]] || {
        rm -f -- "$listing_path"
        return 1
    }
    # Do not extract links, devices, or other special files into the local
    # results tree. Safe member spelling alone is not sufficient for tar.
    local verbose_path listing type
    verbose_path=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-kubectl-verbose.XXXXXX") || {
        rm -f -- "$listing_path"
        return 1
    }
    if ! LC_ALL=C tar -tvf "$archive_path" > "$verbose_path"; then
        rm -f -- "$listing_path" "$verbose_path"
        return 1
    fi
    while IFS= read -r listing; do
        type="${listing:0:1}"
        [[ "$type" == - || "$type" == d ]] || {
            echo "Error: collection archive contains a nonregular member" >&2
            archive_error=1
            break
        }
    done < "$verbose_path"
    rm -f -- "$listing_path" "$verbose_path"
    [[ "$archive_error" -eq 0 ]]
}

kubectl_extract_attempt_archive() {
    local archive_path="$1" attempt_id="$2" results_dir="$3" output_dir="$4"
    kubectl_validate_attempt_archive "$archive_path" "$attempt_id" || return 1
    [[ -d "$results_dir" && ! -L "$results_dir" && -n "$output_dir" && ! -e "$output_dir" \
        && "${output_dir##*/}" =~ ^\.kubernetes-collect-[0-9a-f]{8}\.[A-Za-z0-9]+$ ]] || return 1
    _kubectl_validate_local_directory_path "$results_dir" \
        && _kubectl_validate_local_directory_path "${output_dir%/*}" || return 1
    local results_real parent_real
    results_real=$(realpath -e -- "$results_dir") || return 1
    parent_real=$(realpath -e -- "${output_dir%/*}") || return 1
    [[ "$parent_real" == "$results_real" || "$parent_real" == "$results_real/"* ]] || {
        echo "Error: Kubernetes collection staging must reside below results" >&2
        return 1
    }
    mkdir -p "$output_dir" || return 1
    tar -C "$output_dir" --no-same-owner --no-same-permissions -xf "$archive_path" || {
        rm -rf -- "$output_dir"
        return 1
    }
    local extracted_bytes
    extracted_bytes=$(du -sb -- "$output_dir" | awk '{print $1}') || return 1
    [[ "$extracted_bytes" =~ ^[0-9]+$ && "$extracted_bytes" -le "$KUBECTL_COLLECTION_MAX_BYTES" ]] || {
        rm -rf -- "$output_dir"
        echo "Error: extracted Kubernetes collection exceeds its byte limit" >&2
        return 1
    }
    [[ -d "$output_dir/$attempt_id" && ! -L "$output_dir/$attempt_id" ]]
}

kubectl_create_attempt_policies() {
    local kubernetes_dir="$1" lock_fd="$2" namespace="$3" attempt_id="$4" nonce="$5"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    local template_dir worker_name coordinator_name manifest uid
    template_dir=$(kubectl_template_directory) || return 1
    worker_name="sst-elb-$attempt_id-worker-net"
    coordinator_name="sst-elb-$attempt_id-coord-net"
    manifest=$(kubectl_render_attempt_template "$template_dir/worker-network-policy.yaml.tmpl" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$worker_name" "ATTEMPT_ID=$attempt_id" \
        "OWNERSHIP_NONCE=$nonce") || return 1
    kubectl_create_owned_object uid NetworkPolicy "$worker_name" "$namespace" "$nonce" \
        "$attempt_id" "$manifest" "$kubernetes_dir" "$lock_fd" worker-net || return 1
    kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        worker-net NetworkPolicy "$worker_name" "$namespace" "$uid" "$nonce" || {
            kubectl_delete_owned_object NetworkPolicy "$worker_name" "$namespace" "$nonce" \
                "$attempt_id" "$uid" || true
            return 1
        }
    kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" worker-net || return 1
    manifest=$(kubectl_render_attempt_template "$template_dir/coordinator-network-policy.yaml.tmpl" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$coordinator_name" "ATTEMPT_ID=$attempt_id" \
        "OWNERSHIP_NONCE=$nonce") || return 1
    kubectl_create_owned_object uid NetworkPolicy "$coordinator_name" "$namespace" "$nonce" \
        "$attempt_id" "$manifest" "$kubernetes_dir" "$lock_fd" coord-net || {
            kubectl_cleanup_journaled_resources "$kubernetes_dir" "$lock_fd" "$attempt_id" || true
            return 1
        }
    kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        coord-net NetworkPolicy "$coordinator_name" "$namespace" "$uid" "$nonce" || {
            kubectl_delete_owned_object NetworkPolicy "$coordinator_name" "$namespace" "$nonce" \
                "$attempt_id" "$uid" || true
            kubectl_cleanup_journaled_resources "$kubernetes_dir" "$lock_fd" "$attempt_id" || true
            return 1
        }
    kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" coord-net || return 1
}

kubectl_create_worker_daemonset() {
    local kubernetes_dir="$1" lock_fd="$2" namespace="$3" attempt_id="$4" nonce="$5"
    local nodes_path="$6"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ -f "$nodes_path" && ! -L "$nodes_path" ]] || return 1
    local -a nodes=()
    mapfile -t nodes < <(cut -f1 "$nodes_path")
    [[ ${#nodes[@]} -gt 0 ]] || return 1
    local selector_block affinity_block name template manifest uid
    selector_block=$(kubectl_render_node_selector "$KUBECTL_NODE_SELECTOR") || return 1
    affinity_block=$(kubectl_render_node_affinity_values "${nodes[@]}") || return 1
    name="sst-elb-$attempt_id-workers"
    template="$(kubectl_template_directory)/worker-daemonset.yaml.tmpl"
    manifest=$(kubectl_render_attempt_template "$template" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$name" "ATTEMPT_ID=$attempt_id" \
        "OWNERSHIP_NONCE=$nonce" "IMAGE=$KUBECTL_ELBENCHO_IMAGE" \
        "IMAGE_PULL_POLICY=$KUBECTL_IMAGE_PULL_POLICY" \
        "RUN_AS_USER=$KUBECTL_RUN_AS_USER" "RUN_AS_GROUP=$KUBECTL_RUN_AS_GROUP" \
        "PVC_NAME=$KUBECTL_PVC" "NODE_SELECTOR_BLOCK=$selector_block" \
        "NODE_AFFINITY_VALUES=$affinity_block") || return 1
    kubectl_create_owned_object uid DaemonSet "$name" "$namespace" "$nonce" \
        "$attempt_id" "$manifest" "$kubernetes_dir" "$lock_fd" workers || return 1
    kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        workers DaemonSet "$name" "$namespace" "$uid" "$nonce" || {
            kubectl_delete_owned_object DaemonSet "$name" "$namespace" "$nonce" \
                "$attempt_id" "$uid" || true
            return 1
        }
    kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" workers || return 1
}

kubectl_initialize_remote_control_tree() {
    local namespace="$1" pod_name="$2" attempt_id="$3"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local remote_run
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    local guard_script
    guard_script=$(kubectl_remote_tree_guard_script) || return 1
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu "$guard_script
        umask 077
        [[ ! -e \"\$run/control\" && ! -e \"\$run/state\" && ! -e \"\$run/results\" ]] || exit 1
        mkdir -- \"\$run/control\" \"\$run/state\" \"\$run/results\"
        mkdir -- \"\$run/control/executions\" \"\$run/state/executions\" \"\$run/results/executions\"
        : > \"\$run/state/publication-manifest.tsv\"
        printf \"PREPARED\\\\n\" > \"\$run/state/run.status\"
    " bash "$remote_run" "$attempt_id"
}

kubectl_prepare_control_bundle() {
    # Build the immutable portion of the phase-6 coordinator bundle. The
    # coordinator script itself is deliberately supplied by phase 6, but every
    # coordinator must receive this exact trusted helper and run metadata.
    local output_variable="$1" kubernetes_dir="$2" lock_fd="$3" attempt_id="$4"
    local output_basename="$5" coordinator_source="$6"
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
        && "$attempt_id" =~ ^[0-9a-f]{8}$ \
        && "$output_basename" =~ ^elbencho-[0-9]{8}Z[0-9]{6}$ \
        && -f "$coordinator_source" && ! -L "$coordinator_source" ]] || return 1
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_attempt_load_identity "$kubernetes_dir/attempts/$attempt_id" || return 1
    local destination="$kubernetes_dir/attempts/$attempt_id/control-bundle"
    [[ ! -e "$destination" && ! -L "$destination" ]] || return 1
    mkdir "$destination" || return 1
    local source
    source=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) || {
        rmdir -- "$destination" || true
        return 1
    }
    if ! cp -- "$source/_nv-elbencho-kubectl-functions.sh" \
            "$destination/_nv-elbencho-kubectl-functions.sh" \
            || ! cp -- "$coordinator_source" "$destination/coordinator.sh" \
            || ! chmod 0700 -- "$destination/coordinator.sh" \
            || ! printf '%s\t%s\n%s\t%s\n' attempt_id "$attempt_id" output_basename \
                "$output_basename" > "$destination/run-metadata.tsv" \
            || ! (cd "$destination" && sha256sum _nv-elbencho-kubectl-functions.sh \
                coordinator.sh run-metadata.tsv | awk '{print $1 "\t" $2}') \
                > "$destination/bundle-manifest.tsv"; then
            rm -rf -- "$destination"
            return 1
    fi
    printf -v "$output_variable" '%s' "$destination"
}

kubectl_upload_control_bundle() {
    local namespace="$1" pod_name="$2" attempt_id="$3" source_dir="$4"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && -d "$source_dir" ]] || return 1
    local remote_run
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    local guard_script
    guard_script=$(kubectl_remote_tree_guard_script) || return 1
    # Reject link-bearing controls before streaming. The remote extraction is
    # intentionally constrained to the reserved, already-reserved run tree.
    local listing_path path type links
    listing_path=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-control-list.XXXXXX") || return 1
    if ! find "$source_dir" -xdev -print0 > "$listing_path"; then
        rm -f -- "$listing_path"
        return 1
    fi
    local scan_error=0
    while IFS= read -r -d '' path; do
        if [[ -L "$path" ]]; then
            echo "Error: control bundle may not contain symbolic links" >&2
            scan_error=1
            break
        fi
        if ! type=$(stat -c %F -- "$path"); then
            scan_error=1
            break
        fi
        if [[ "$type" != "regular file" && "$type" != directory ]]; then
            echo "Error: control bundle may contain only regular files and directories" >&2
            scan_error=1
            break
        fi
        if [[ "$type" == "regular file" ]]; then
            if ! links=$(stat -c %h -- "$path"); then
                scan_error=1
                break
            fi
            if [[ "$links" != 1 ]]; then
                echo "Error: control bundle may not contain hard-linked files" >&2
                scan_error=1
                break
            fi
        fi
    done < "$listing_path"
    rm -f -- "$listing_path"
    [[ "$scan_error" -eq 0 ]] || return 1
    # Materialize a local archive before transfer so a source-tree read error
    # cannot be hidden by pipeline status. The remote control directory was
    # created create-only during reservation; do not recreate it here.
    local archive_path
    archive_path=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-control.XXXXXX") || return 1
    if ! tar -C "$source_dir" -cf "$archive_path" .; then
        rm -f -- "$archive_path"
        return 1
    fi
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    if ! kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu "$guard_script
            control=\"\$run/control\"
            [[ -d \"\$control\" && ! -L \"\$control\" ]] || exit 1
            control_real=\$(realpath -e -- \"\$control\") || exit 1
            [[ \"\$control_real\" == \"\$run_real/control\" ]] || exit 1
            if find -P \"\$control\" -type l -print -quit | grep -q .; then
                exit 1
            fi
            exec tar -C \"\$control\" -xf -" \
            bash "$remote_run" "$attempt_id" < "$archive_path"; then
        rm -f -- "$archive_path"
        return 1
    fi
    rm -f -- "$archive_path"
}

kubectl_cleanup_journaled_resources() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    local key resource_file intent_file primary_rc=0 rc
    for intent_file in "$kubernetes_dir/attempts/$attempt_id/creation-intents"/*.sh; do
        [[ -e "$intent_file" || -L "$intent_file" ]] || continue
        if [[ ! -f "$intent_file" || -L "$intent_file" ]]; then
            printf 'Error: invalid Kubernetes creation intent path for %s\n' \
                "$(basename "$intent_file")" >&2
            primary_rc=1
            continue
        fi
        key=$(basename "$intent_file" .sh)
        if ! kubectl_cleanup_creation_intent "$kubernetes_dir" "$lock_fd" \
                "$attempt_id" "$key"; then
            printf 'Error: failed to reconcile Kubernetes creation intent for %s\n' \
                "$key" >&2
            primary_rc=1
        fi
    done
    local -a keys=(sweep workers transfer status collector coord-net worker-net)
    for resource_file in "$kubernetes_dir/attempts/$attempt_id/resources"/{status,collector}-*.sh; do
        [[ -f "$resource_file" && ! -L "$resource_file" ]] || continue
        keys+=("$(basename "$resource_file" .sh)")
    done
    for key in "${keys[@]}"; do
        kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "delete-$key" && continue
        if ! kubectl_attempt_resource_exists "$kubernetes_dir" "$attempt_id" "$key"; then
            continue
        fi
        kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$key" || {
            printf 'Error: corrupt Kubernetes resource journal for %s\n' "$key" >&2
            [[ "$primary_rc" -ne 0 ]] || primary_rc=1
            continue
        }
        if kubectl_delete_owned_object "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" \
            "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
            "$KUBECTL_RESOURCE_UID"; then
            kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
                "delete-$key" || primary_rc=1
        else
            rc=$?
            printf 'Warning: failed to delete owned Kubernetes %s %s\n' \
                "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" >&2
            [[ "$primary_rc" -ne 0 ]] || primary_rc="$rc"
        fi
    done
    return "$primary_rc"
}

kubectl_quiesce_prepared_workloads() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local key intent_file
    # A sweep Job can start before the submitter publishes SUBMITTED.  A
    # recovered PREPARED attempt must therefore stop every workload actor
    # before removing the PVC run tree that those actors may still use.  Keep
    # the transfer helper alive; it is needed for the subsequent exact remote
    # release.
    for key in sweep workers; do
        intent_file="$kubernetes_dir/attempts/$attempt_id/creation-intents/$key.sh"
        if [[ -e "$intent_file" || -L "$intent_file" ]]; then
            [[ -f "$intent_file" && ! -L "$intent_file" ]] || return 1
            kubectl_cleanup_creation_intent "$kubernetes_dir" "$lock_fd" \
                "$attempt_id" "$key" || return 1
        fi
        kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" \
            "delete-$key" && continue
        kubectl_attempt_resource_exists "$kubernetes_dir" "$attempt_id" \
            "$key" || continue
        kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" \
            "$key" || return 1
        kubectl_delete_owned_object "$KUBECTL_RESOURCE_KIND" \
            "$KUBECTL_RESOURCE_NAME" "$KUBECTL_RESOURCE_NAMESPACE" \
            "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
            "$KUBECTL_RESOURCE_UID" || return 1
        kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" "delete-$key" || return 1
    done
}

kubectl_cancel_journaled_attempt() {
    local kubernetes_dir="$1" lock_fd="$2" namespace="$3" helper_pod="$4"
    local attempt_id="$5" nonce="$6"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$attempt_id" || return 1
    [[ "$KUBECTL_NAMESPACE" == "$namespace" && "$KUBECTL_OWNERSHIP_NONCE" == "$nonce" ]] \
        || return 1
    local observed_status
    observed_status=$(kubectl_read_remote_status "$namespace" "$helper_pod" \
        "$attempt_id") || return 1
    if [[ "$observed_status" =~ ^(SUCCESS|FAILED|CANCELLED)$ ]]; then
        case "$KUBECTL_LIFECYCLE_STATE" in
            SUBMITTED|CANCEL_REQUESTED)
                kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" \
                    "$attempt_id" TERMINAL || return 1
                ;;
            TERMINAL|COLLECTION_IN_PROGRESS|COLLECTED) ;;
            *) return 1 ;;
        esac
        printf '%s\n' "$observed_status"
        return 0
    fi
    [[ "$observed_status" =~ ^(PREPARED|RUNNING)$ ]] || return 1
    case "$KUBECTL_LIFECYCLE_STATE" in
        PREPARED|SUBMITTED)
            kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" CANCEL_REQUESTED || return 1
            ;;
        TERMINAL|COLLECTION_IN_PROGRESS|COLLECTED)
            return 1
            ;;
        CANCEL_REQUESTED) ;;
        *) return 1 ;;
    esac
    # The exact Job is deleted through its journal. Its absence is not inferred
    # from a broad label query, and an ownership mismatch always stops cleanup.
    if kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" sweep; then
        kubectl_delete_owned_object "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" \
            "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
            "$KUBECTL_RESOURCE_UID" || return 1
        kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" delete-sweep || return 1
    fi
    kubectl_finalize_cancelled_attempt "$namespace" "$helper_pod" "$attempt_id" \
        || return 1
    kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" TERMINAL || return 1
    printf 'CANCELLED\n'
}

kubectl_prepare_attempt_lifecycle() {
    # Establish the complete pre-Job state machine through real, exact object
    # operations. Phase 6 intentionally owns the coordinator Job creation.
    local output_variable="$1" results_dir="$2" required_nodes="$3" mapped_dirs_name="$4"
    local mapped_read_from="${5:-}" expected_current="${6:-}"
    local kubernetes_dir lock_fd generated_attempt_id nonce current_attempt=""
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
        && "$required_nodes" =~ ^[1-9][0-9]*$ && -d "$results_dir" ]] || return 1
    _kubectl_validate_local_directory_path "$results_dir" || return 1
    kubernetes_dir="$results_dir/kubernetes"
    kubectl_local_lock_acquire "$kubernetes_dir" lock_fd || return 1
    local primary_rc=0 namespace_uid pv_uid pvc_uid candidate_nodes coordinator_node
    local helper_name helper_uid operation_token remote_reserved=0
    if [[ -e "$kubernetes_dir/current-attempt" ]]; then
        current_attempt=$(kubectl_attempt_current_id "$kubernetes_dir") || primary_rc=1
        if [[ "$primary_rc" -eq 0 ]]; then
            [[ -n "$expected_current" && "$current_attempt" == "$expected_current" ]] \
                || primary_rc=1
            kubectl_attempt_load_metadata \
                "$kubernetes_dir/attempts/$current_attempt" || primary_rc=1
            [[ "$primary_rc" -ne 0 || "$KUBECTL_LIFECYCLE_STATE" == COLLECTED ]] \
                || primary_rc=1
        fi
    elif [[ -n "$expected_current" ]]; then
        primary_rc=1
    fi
    if [[ "$primary_rc" -eq 0 ]] \
            && ! IFS=$'\t' read -r namespace_uid pv_uid pvc_uid \
                < <(kubectl_validate_cluster_identity); then
        primary_rc=1
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        for _ in {1..16}; do
            generated_attempt_id=$(kubectl_generate_attempt_id) || break
            [[ ! -e "$kubernetes_dir/attempts/$generated_attempt_id" ]] && break
            generated_attempt_id=""
        done
        [[ "$generated_attempt_id" =~ ^[0-9a-f]{8}$ ]] || primary_rc=1
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        nonce=$(kubectl_generate_ownership_nonce) || primary_rc=1
        kubectl_attempt_create_identity "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" "$nonce" \
            "$KUBECTL_NAMESPACE" "$namespace_uid" "$KUBECTL_PV" "$pv_uid" \
            "$KUBECTL_PVC" "$pvc_uid" || primary_rc=1
        if [[ "$primary_rc" -eq 0 && -n "$current_attempt" ]]; then
            kubectl_attempt_write_predecessor "$kubernetes_dir" "$lock_fd" \
                "$generated_attempt_id" "$current_attempt" || primary_rc=1
        fi
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        kubectl_attempt_write_state "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" PREPARED \
            && kubectl_attempt_write_configuration "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" \
                "$mapped_dirs_name" "$mapped_read_from" \
            || primary_rc=1
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        # Publish the PREPARED pointer before any Kubernetes or PVC mutation.
        # A killed submitter can then be found and retried by status/cancel.
        kubectl_attempt_write_current "$kubernetes_dir" "$lock_fd" \
            "$generated_attempt_id" || primary_rc=1
    fi
    candidate_nodes="$kubernetes_dir/attempts/${generated_attempt_id:-invalid}/nodes.tsv"
    if [[ "$primary_rc" -eq 0 ]]; then
        kubectl_discover_candidate_nodes "$KUBECTL_NODE_SELECTOR" "$candidate_nodes" || primary_rc=1
        [[ $(wc -l < "$candidate_nodes" 2>/dev/null || printf 0) -ge "$required_nodes" ]] \
            || primary_rc=1
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        coordinator_node=$(kubectl_choose_coordinator_node "$candidate_nodes") || primary_rc=1
        operation_token=$(_kubectl_random_hex 4) || primary_rc=1
        helper_name="sst-elb-$generated_attempt_id-upload-$operation_token"
        kubectl_create_helper_pod helper_uid transfer "$KUBECTL_NAMESPACE" "$helper_name" \
            "$nonce" "$generated_attempt_id" "$coordinator_node" "$operation_token" \
            "$kubernetes_dir" "$lock_fd" transfer || primary_rc=1
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        local -n mapped_dirs_ref="$mapped_dirs_name"
        local -a pvc_paths=("${!mapped_dirs_ref[@]}")
        [[ -z "$mapped_read_from" ]] || pvc_paths+=("$mapped_read_from")
        kubectl_validate_pvc_paths "$KUBECTL_NAMESPACE" "$helper_name" "${pvc_paths[@]}" || primary_rc=1
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        if kubectl_attempt_journal_remote_reservation "$kubernetes_dir" "$lock_fd" \
                "$generated_attempt_id" "$nonce"; then
            # Persist deterministic ownership intent before touching the PVC.
            # The idempotent release path can then recover a killed reserve.
            remote_reserved=1
            if kubectl_reserve_remote_attempt "$KUBECTL_NAMESPACE" "$helper_name" \
                    "$generated_attempt_id" "$nonce"; then
                kubectl_attempt_mark_remote_reservation_acquired "$kubernetes_dir" "$lock_fd" \
                    "$generated_attempt_id" \
                    && kubectl_initialize_remote_control_tree "$KUBECTL_NAMESPACE" "$helper_name" \
                        "$generated_attempt_id" \
                    || primary_rc=1
            else
                primary_rc=1
            fi
        else
            primary_rc=1
        fi
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        kubectl_create_attempt_policies "$kubernetes_dir" "$lock_fd" "$KUBECTL_NAMESPACE" \
            "$generated_attempt_id" "$nonce" \
            && kubectl_create_worker_daemonset "$kubernetes_dir" "$lock_fd" "$KUBECTL_NAMESPACE" \
                "$generated_attempt_id" "$nonce" "$candidate_nodes" \
            && kubectl_wait_worker_endpoints "$KUBECTL_NAMESPACE" "$generated_attempt_id" "$candidate_nodes" \
                "$kubernetes_dir/attempts/$generated_attempt_id/worker-endpoints.tsv" \
                "${KUBECTL_WORKER_READY_TIMEOUT_SECONDS:-180}" \
                "$kubernetes_dir/attempts/$generated_attempt_id" \
            || primary_rc=1
    fi
    if [[ "$primary_rc" -ne 0 ]]; then
        local rollback_rc=0
        # Keep PREPARED as the recovery state until every exact external
        # resource and reservation has been released successfully.
        if [[ -n "$generated_attempt_id" ]]; then
            kubectl_quiesce_prepared_workloads "$kubernetes_dir" "$lock_fd" \
                "$generated_attempt_id" || rollback_rc=1
            kubectl_preserve_attempt_diagnostics "$kubernetes_dir" \
                "$generated_attempt_id" prepare-failed
        fi
        if [[ "$rollback_rc" -eq 0 && "$remote_reserved" -eq 1 ]]; then
            kubectl_release_journaled_remote_attempt "$kubernetes_dir" "$lock_fd" \
                "$KUBECTL_NAMESPACE" "$helper_name" "$generated_attempt_id" \
                || rollback_rc=1
        fi
        [[ -z "$generated_attempt_id" ]] || kubectl_cleanup_journaled_resources \
            "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" || rollback_rc=1
        if [[ -n "$generated_attempt_id" \
                && -f "$kubernetes_dir/attempts/$generated_attempt_id/state.sh" ]]; then
            kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$generated_attempt_id" >/dev/null 2>&1 \
                && [[ "$KUBECTL_LIFECYCLE_STATE" == PREPARED ]] \
                && [[ "$rollback_rc" -eq 0 ]] \
                && kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" \
                    "$generated_attempt_id" SUBMISSION_FAILED \
                || rollback_rc=1
        fi
        if [[ "$rollback_rc" -eq 0 && -n "$current_attempt" ]]; then
            # A cleanly failed resume must not hide its collected predecessor;
            # a rollback failure deliberately leaves the new PREPARED attempt
            # as current so status/cancel can retry it.
            kubectl_attempt_restore_predecessor "$kubernetes_dir" "$lock_fd" \
                "$generated_attempt_id" || rollback_rc=1
        fi
        kubectl_local_lock_release "$lock_fd" || true
        return 1
    fi
    kubectl_local_lock_release "$lock_fd" || return 1
    printf -v "$output_variable" '%s' "$generated_attempt_id"
}

# Add the frozen sweep input to the small, immutable coordinator bundle made by
# kubectl_prepare_control_bundle.  This happens before the Job exists: a Job
# never observes a partly populated control tree.  The bundle's manifest is
# deliberately regenerated from the exact uploaded files rather than trying to
# reproduce the tar command's inclusion rules in a second implementation.
kubectl_populate_sweep_control_bundle() {
    local bundle="$1" results_dir="$2" endpoints="$3" selection_file="${4:-}"
    [[ -d "$bundle" && ! -L "$bundle" && -d "$results_dir" && ! -L "$results_dir" \
        && -f "$endpoints" && ! -L "$endpoints" ]] || return 1
    local source_dir
    source_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd) || return 1
    local source
    for source in "$results_dir/env_used.sh" "$results_dir/env_used.yaml" \
        "$source_dir/lib/_platform_functions.sh" "$source_dir/lib/_elbencho_functions.sh"; do
        [[ -f "$source" && ! -L "$source" ]] || return 1
    done
    cp -- "$results_dir/env_used.sh" "$bundle/env_used.sh" \
        && cp -- "$results_dir/env_used.yaml" "$bundle/env_used.yaml" \
        && cp -- "$source_dir/lib/_platform_functions.sh" "$bundle/_platform_functions.sh" \
        && cp -- "$source_dir/lib/_elbencho_functions.sh" "$bundle/_elbencho_functions.sh" \
        || return 1
    local overlay_source overlay_remote
    overlay_source=$(bash -c 'set -e; source "$1"; printf "%s" "${KUBECTL_INTEGRATION_FAILURE_OVERLAY:-}"' kubectl-env "$results_dir/env_used.sh") || return 1
    if [[ -n "$selection_file" ]]; then
        printf '\nunset KUBECTL_INTEGRATION_FAILURE_OVERLAY\n' >> "$bundle/env_used.sh" || return 1
    elif [[ -n "$overlay_source" ]]; then
        [[ -f "$overlay_source" && ! -L "$overlay_source" && -x "$overlay_source" ]] || {
            echo "Error: Kubernetes integration failure overlay is not executable" >&2
            return 1
        }
        overlay_remote="$(kubectl_attempt_remote_root "$attempt_id")/control/failure-overlay.sh" || return 1
        cp -- "$overlay_source" "$bundle/failure-overlay.sh" || return 1
        chmod 0700 "$bundle/failure-overlay.sh" || return 1
        printf '\nexport STORAGE_SCALE_TEST_INTEGRATION=1\nunset KUBECTL_INTEGRATION_FAILURE_OVERLAY\nexport KUBECTL_INTEGRATION_FAILURE_OVERLAY=%q\n' "$overlay_remote" >> "$bundle/env_used.sh" || return 1
    fi
    mkdir "$bundle/executions" || return 1
    local definition id node node_uid pod pod_uid ip arch image extra
    local -a definitions=()
    if [[ -n "$selection_file" ]]; then
        [[ -f "$selection_file" && ! -L "$selection_file" ]] || return 1
        local selected_id
        while IFS= read -r selected_id; do
            [[ "$selected_id" =~ ^[0-9]{4}$ ]] || return 1
            definitions+=("$results_dir/executions/$selected_id.sh")
        done < "$selection_file"
    else
        definitions=("$results_dir"/executions/[0-9][0-9][0-9][0-9].sh)
    fi
    [[ ${#definitions[@]} -gt 0 ]] || return 1
    for definition in "${definitions[@]}"; do
        [[ -f "$definition" && ! -L "$definition" ]] || return 1
        cp -- "$definition" "$bundle/executions/$(basename "$definition")" || return 1
    done
    : > "$bundle/worker-endpoints.tsv" || return 1
    while IFS=$'\t' read -r node node_uid pod pod_uid ip arch image extra; do
        [[ -z "${extra:-}" ]] && kubectl_validate_object_name "$node" \
            && kubectl_validate_uid "$node_uid" && kubectl_validate_object_name "$pod" \
            && kubectl_validate_uid "$pod_uid" && kubectl_validate_ipv4 "$ip" \
            && [[ "$arch" =~ ^(amd64|arm64)$ && -n "$image" ]] || return 1
        printf '%s\t%s\t%s\t%s\n' "$node" "$pod" "$pod_uid" "$ip" \
            >> "$bundle/worker-endpoints.tsv" || return 1
    done < "$endpoints"
    [[ -s "$bundle/worker-endpoints.tsv" ]] || return 1
    (cd "$bundle" && find -P . -type f ! -name bundle-manifest.tsv -print0 \
        | LC_ALL=C sort -z | xargs -0 sha256sum | awk '{sub(/^\.\//, "", $2); print $1 "\t" $2}') \
        > "$bundle/bundle-manifest.tsv" || return 1
}

kubectl_attempt_current_id() {
    local kubernetes_dir="$1" attempt_id
    local current="$kubernetes_dir/current-attempt"
    [[ -f "$current" && ! -L "$current" ]] || return 1
    attempt_id=$(cat -- "$current") || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && -d "$kubernetes_dir/attempts/$attempt_id" ]] || return 1
    printf '%s\n' "$attempt_id"
}

_kubectl_load_saved_attempt() {
    local results_dir="$1" output_name="$2"
    [[ -d "$results_dir" && ! -L "$results_dir" \
        && "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    local kubernetes_dir="$results_dir/kubernetes" loaded_attempt_id metadata_dir
    _kubectl_validate_local_directory_path "$kubernetes_dir" || return 1
    loaded_attempt_id=$(kubectl_attempt_current_id "$kubernetes_dir") || return 1
    metadata_dir="$kubernetes_dir/attempts/$loaded_attempt_id"
    kubectl_attempt_load_metadata "$metadata_dir" || return 1
    [[ -f "$metadata_dir/configuration.sh" && ! -L "$metadata_dir/configuration.sh" ]] || return 1
    # shellcheck disable=SC1090,SC1091  # Trusted, result-directory-local immutable metadata.
    source "$metadata_dir/configuration.sh" || return 1
    printf -v "$output_name" '%s' "$loaded_attempt_id"
}

_kubectl_verify_saved_cluster_identity() {
    local namespace_uid pv_uid pvc_uid
    IFS=$'\t' read -r namespace_uid pv_uid pvc_uid < <(kubectl_validate_cluster_identity) || return 1
    [[ "$namespace_uid" == "$KUBECTL_NAMESPACE_UID" && "$pv_uid" == "$KUBECTL_PV_UID" \
        && "$pvc_uid" == "$KUBECTL_PVC_UID" ]]
}

_kubectl_verify_saved_worker_endpoints() {
    local kubernetes_dir="$1" attempt_id="$2"
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id"
    kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" workers || return 1
    local worker_name="$KUBECTL_RESOURCE_NAME"
    local worker_namespace="$KUBECTL_RESOURCE_NAMESPACE"
    local worker_nonce="$KUBECTL_RESOURCE_NONCE"
    local worker_uid="$KUBECTL_RESOURCE_UID"
    if ! kubectl_verify_object_identity DaemonSet "$worker_name" \
            "$worker_namespace" "$worker_nonce" "$attempt_id" \
            "$worker_uid" >/dev/null; then
        local diagnostic_path=""
        diagnostic_path=$(kubectl_capture_resource_diagnostics "$metadata_dir" \
            workers-identity "$worker_namespace" DaemonSet "$worker_name" \
            "$attempt_id") || diagnostic_path=""
        KUBECTL_ATTEMPT_ID="$attempt_id" \
            KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="$metadata_dir/state.sh" \
            kubectl_report_lifecycle_error verify-workers endpoint-identity \
                IDENTITY_MISMATCH \
                "restore the exact worker DaemonSet or cancel and collect the attempt" \
                unknown DaemonSet "$worker_name" "$worker_namespace" \
                "$worker_uid" "" "$diagnostic_path" || true
        return 1
    fi
    local deadline=$((SECONDS + ${KUBECTL_ENDPOINT_STABILIZE_TIMEOUT_SECONDS:-30}))
    local rc remaining per_call
    while (( SECONDS < deadline )); do
        remaining=$((deadline - SECONDS))
        per_call=$((remaining / 3))
        (( per_call > 0 )) || per_call=1
        (( per_call <= 10 )) || per_call=10
        rc=0
        KUBECTL_OBSERVATION_ATTEMPTS=1 \
            KUBECTL_REQUEST_TIMEOUT_SECONDS="$per_call" \
            KUBECTL_PROCESS_TIMEOUT_SECONDS="$per_call" \
            kubectl_compare_worker_endpoints "$KUBECTL_NAMESPACE" "$attempt_id" \
                "$metadata_dir/nodes.tsv" "$metadata_dir/worker-endpoints.tsv" \
                "$metadata_dir/endpoint-drift.tsv" || rc=$?
        [[ "$rc" -ne 0 ]] || return 0
        [[ "$rc" -ne 2 ]] || return 2
        sleep 1
    done
    echo "Error: timed out waiting for stable Kubernetes worker endpoints" >&2
    return 1
}

_kubectl_fail_drifted_attempt() {
    local kubernetes_dir="$1" lock_fd="$2" helper_pod="$3" attempt_id="$4"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" sweep || return 1
    kubectl_delete_owned_object "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" \
        "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
        "$KUBECTL_RESOURCE_UID" || return 1
    kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        delete-sweep || return 1
    local remote_state
    remote_state=$(kubectl_read_remote_status "$KUBECTL_NAMESPACE" "$helper_pod" \
        "$attempt_id") || return 1
    if [[ "$remote_state" =~ ^(PREPARED|RUNNING)$ ]]; then
        kubectl_recover_lost_coordinator "$KUBECTL_NAMESPACE" "$helper_pod" \
            "$attempt_id" || return 1
        remote_state=$(kubectl_read_remote_status "$KUBECTL_NAMESPACE" "$helper_pod" \
            "$attempt_id") || return 1
    fi
    [[ "$remote_state" == FAILED ]] || return 1
    kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        TERMINAL || return 1
    printf '%s\n' "$remote_state"
}

_kubectl_attempt_helper_node() {
    local metadata_dir="$1"
    local nodes_path="$metadata_dir/nodes.tsv"
    [[ -f "$nodes_path" && ! -L "$nodes_path" ]] || return 1
    kubectl_choose_coordinator_node "$nodes_path"
}

_kubectl_create_inspector() {
    local name_output="$1" uid_output="$2" template_name="$3" kubernetes_dir="$4" lock_fd="$5" attempt_id="$6"
    local node token name uid
    [[ "$template_name" =~ ^(status|collector)$ ]] || return 1
    node=$(_kubectl_attempt_helper_node "$kubernetes_dir/attempts/$attempt_id") || return 1
    token=$(_kubectl_random_hex 4) || return 1
    name="sst-elb-$attempt_id-$template_name-$token"
    local resource_key="$template_name-$token"
    kubectl_create_helper_pod uid "$template_name" "$KUBECTL_NAMESPACE" "$name" \
        "$KUBECTL_OWNERSHIP_NONCE" "$attempt_id" "$node" "$token" \
        "$kubernetes_dir" "$lock_fd" "$resource_key" || return 1
    # Every observation has a fresh, journaled identity.  A recovery sweep can
    # delete a helper left by a killed status/collect/cancel process exactly.
    printf -v "$name_output" '%s' "$name"
    printf -v "$uid_output" '%s' "$uid"
}

_kubectl_remove_inspector() {
    local kubernetes_dir="$1" lock_fd="$2" name="$3" uid="$4" attempt_id="$5"
    local token="${name##*-}" template_name resource_key
    [[ "$token" =~ ^[0-9a-f]{8}$ ]] || return 1
    if [[ "$name" == *-status-* ]]; then template_name=status
    elif [[ "$name" == *-collector-* ]]; then template_name=collector
    else return 1; fi
    resource_key="$template_name-$token"
    kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" \
        "delete-$resource_key" && return 0
    kubectl_delete_owned_object Pod "$name" "$KUBECTL_NAMESPACE" \
        "$KUBECTL_OWNERSHIP_NONCE" "$attempt_id" "$uid" \
        && kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "delete-$resource_key"
}

kubectl_cleanup_ephemeral_helpers() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" resource_file intent_file key
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    # A killed helper creation can leave only its pre-create intent. Reconcile
    # those exact ephemeral names before scanning the UID-bearing journals;
    # core attempt intents remain for the full rollback/collection path.
    for intent_file in "$kubernetes_dir/attempts/$attempt_id/creation-intents"/{status,collector}-*.sh; do
        [[ -e "$intent_file" || -L "$intent_file" ]] || continue
        [[ -f "$intent_file" && ! -L "$intent_file" ]] || return 1
        key=$(basename "$intent_file" .sh)
        kubectl_cleanup_creation_intent "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" "$key" || return 1
    done
    for resource_file in "$kubernetes_dir/attempts/$attempt_id/resources"/{status,collector}-*.sh; do
        [[ -f "$resource_file" && ! -L "$resource_file" ]] || continue
        key=$(basename "$resource_file" .sh)
        kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "delete-$key" && continue
        kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$key" || return 1
        kubectl_delete_owned_object "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" \
            "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
            "$KUBECTL_RESOURCE_UID" || return 1
        kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" "delete-$key" || return 1
    done
}

kubectl_submit_sweep() {
    local results_dir="$1" required_nodes="$2" expected_current="${3:-}"
    [[ -d "$results_dir" && "$required_nodes" =~ ^[1-9][0-9]*$ ]] || return 1
    kubectl_validate_runtime_configuration || return 1
    # shellcheck disable=SC2034  # Passed by name to the lifecycle writer.
    local -A mapped_dirs=()
    kubectl_map_test_dirs mapped_dirs || return 1
    local mapped_read_from=""
    [[ -z "${ELBENCHO_SWEEP_READ_FROM:-}" ]] \
        || mapped_read_from=$(kubectl_map_read_from_path "$ELBENCHO_SWEEP_READ_FROM") || return 1
    local attempt_id
    kubectl_prepare_attempt_lifecycle attempt_id "$results_dir" "$required_nodes" mapped_dirs \
        "$mapped_read_from" "$expected_current" || return 1
    local kubernetes_dir="$results_dir/kubernetes" lock_fd bundle coordinator_node job_name manifest job_uid
    kubectl_local_lock_acquire "$kubernetes_dir" lock_fd || return 1
    local rc=0
    kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$attempt_id" || rc=1
    if [[ "$rc" -eq 0 ]]; then
        kubectl_prepare_control_bundle bundle "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "${results_dir##*/}" "$(dirname "${BASH_SOURCE[0]}")/_nv-elbencho-kubectl-coordinator.sh" \
            && kubectl_populate_sweep_control_bundle "$bundle" "$results_dir" \
                "$kubernetes_dir/attempts/$attempt_id/worker-endpoints.tsv" \
                "${KUBECTL_SUBMIT_EXECUTIONS_FILE:-}" \
            || rc=1
    fi
    if [[ "$rc" -eq 0 ]]; then
        kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" transfer || rc=1
        [[ "$rc" -ne 0 ]] || kubectl_upload_control_bundle "$KUBECTL_NAMESPACE" \
            "$KUBECTL_RESOURCE_NAME" "$attempt_id" "$bundle" || rc=1
        # Upload has completed; the long-lived attempt owns no transfer Pod.
        if [[ "$rc" -eq 0 ]]; then
            kubectl_delete_owned_object "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" \
                "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
                "$KUBECTL_RESOURCE_UID" \
                && kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
                    delete-transfer || rc=1
        fi
    fi
    if [[ "$rc" -eq 0 ]]; then
        coordinator_node=$(_kubectl_attempt_helper_node "$kubernetes_dir/attempts/$attempt_id") || rc=1
        job_name="sst-elb-$attempt_id-sweep"
        manifest=$(kubectl_render_attempt_template "$(kubectl_template_directory)/sweep-job.yaml.tmpl" \
            "NAMESPACE=$KUBECTL_NAMESPACE" "RESOURCE_NAME=$job_name" "ATTEMPT_ID=$attempt_id" \
            "OWNERSHIP_NONCE=$KUBECTL_OWNERSHIP_NONCE" "IMAGE=$KUBECTL_ELBENCHO_IMAGE" \
            "IMAGE_PULL_POLICY=$KUBECTL_IMAGE_PULL_POLICY" "RUN_AS_USER=$KUBECTL_RUN_AS_USER" \
            "RUN_AS_GROUP=$KUBECTL_RUN_AS_GROUP" "PVC_NAME=$KUBECTL_PVC" \
            "COORDINATOR_NODE=$coordinator_node") || rc=1
    fi
    if [[ "$rc" -eq 0 ]]; then
        if kubectl_create_owned_object job_uid Job "$job_name" "$KUBECTL_NAMESPACE" \
                "$KUBECTL_OWNERSHIP_NONCE" "$attempt_id" "$manifest" \
                "$kubernetes_dir" "$lock_fd" sweep; then
            if ! kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
                    sweep Job "$job_name" "$KUBECTL_NAMESPACE" "$job_uid" \
                    "$KUBECTL_OWNERSHIP_NONCE"; then
                # The Job has an exact UID but no durable journal yet. Delete
                # it immediately; later cleanup must never guess by labels.
                kubectl_delete_owned_object Job "$job_name" "$KUBECTL_NAMESPACE" \
                    "$KUBECTL_OWNERSHIP_NONCE" "$attempt_id" "$job_uid" || true
                rc=1
            elif ! kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
                    "$attempt_id" sweep; then
                rc=1
            elif ! kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" \
                    "$attempt_id" SUBMITTED; then
                kubectl_cleanup_journaled_resources "$kubernetes_dir" "$lock_fd" \
                    "$attempt_id" || true
                rc=1
            fi
        else
            rc=1
        fi
    fi
    if [[ "$rc" -ne 0 ]]; then
        # Nothing asynchronous is allowed to survive a failed handoff.  The
        # pre-Job lifecycle journal gives this rollback exact identities.
        local rollback_helper="" rollback_uid=""
        local rollback_rc=0
        kubectl_quiesce_prepared_workloads "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" || rollback_rc=1
        kubectl_preserve_attempt_diagnostics "$kubernetes_dir" "$attempt_id" \
            submit-failed
        if [[ "$rollback_rc" -eq 0 \
                && -f "$kubernetes_dir/attempts/$attempt_id/remote-reservation.sh" ]]; then
            if _kubectl_create_inspector rollback_helper rollback_uid status "$kubernetes_dir" \
                    "$lock_fd" "$attempt_id"; then
                kubectl_release_journaled_remote_attempt "$kubernetes_dir" "$lock_fd" \
                    "$KUBECTL_NAMESPACE" "$rollback_helper" "$attempt_id" \
                    || rollback_rc=1
                _kubectl_remove_inspector "$kubernetes_dir" "$lock_fd" "$rollback_helper" \
                    "$rollback_uid" "$attempt_id" || rollback_rc=1
            else
                rollback_rc=1
            fi
        fi
        kubectl_cleanup_journaled_resources "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            || rollback_rc=1
        kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$attempt_id" >/dev/null 2>&1 \
            && [[ "$KUBECTL_LIFECYCLE_STATE" == PREPARED ]] \
            && [[ "$rollback_rc" -eq 0 ]] \
            && kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" SUBMISSION_FAILED \
            || rollback_rc=1
        if [[ "$rollback_rc" -eq 0 ]]; then
            kubectl_attempt_restore_predecessor "$kubernetes_dir" "$lock_fd" \
                "$attempt_id" || rollback_rc=1
        fi
        [[ "$rollback_rc" -eq 0 ]] || rc=1
    fi
    kubectl_local_lock_release "$lock_fd" || rc=1
    [[ "$rc" -eq 0 ]] || return "$rc"
    kubectl_emit_submission_identity "$results_dir" "$attempt_id" \
        && kubectl_emit_lifecycle_commands "$results_dir"
}

kubectl_recover_prepared_attempt() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$attempt_id" || return 1
    [[ "$KUBECTL_LIFECYCLE_STATE" == PREPARED ]] || return 1
    local helper="" helper_uid="" rc=0
    kubectl_quiesce_prepared_workloads "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" || rc=1
    kubectl_preserve_attempt_diagnostics "$kubernetes_dir" "$attempt_id" \
        recover-prepared
    if [[ "$rc" -eq 0 \
            && -f "$kubernetes_dir/attempts/$attempt_id/remote-reservation.sh" ]]; then
        _kubectl_create_inspector helper helper_uid status "$kubernetes_dir" \
            "$lock_fd" "$attempt_id" || rc=1
        [[ "$rc" -ne 0 ]] || kubectl_release_journaled_remote_attempt \
            "$kubernetes_dir" "$lock_fd" "$KUBECTL_NAMESPACE" "$helper" \
            "$attempt_id" || rc=1
        if [[ -n "$helper" ]]; then
            _kubectl_remove_inspector "$kubernetes_dir" "$lock_fd" "$helper" \
                "$helper_uid" "$attempt_id" || rc=1
        fi
    fi
    kubectl_cleanup_journaled_resources "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" || rc=1
    [[ "$rc" -ne 0 ]] || kubectl_attempt_transition "$kubernetes_dir" \
        "$lock_fd" "$attempt_id" SUBMISSION_FAILED || rc=1
    [[ "$rc" -ne 0 ]] || kubectl_attempt_restore_predecessor "$kubernetes_dir" \
        "$lock_fd" "$attempt_id" || rc=1
    return "$rc"
}

kubectl_lifecycle_operation() {
    local operation="$1" results_dir="$2" kubernetes_dir attempt_id lock_fd inspector inspector_uid remote_state rc=0
    [[ "$operation" =~ ^(status|cancel|collect)$ && -d "$results_dir" ]] || return 1
    kubernetes_dir="$results_dir/kubernetes"
    kubectl_local_lock_acquire "$kubernetes_dir" lock_fd || return 1
    _kubectl_load_saved_attempt "$results_dir" attempt_id || rc=1
    [[ "$rc" -ne 0 ]] || kubectl_cleanup_ephemeral_helpers "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" || rc=1
    if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == COLLECTED ]]; then
        local collected_terminal=""
        _kubectl_validate_collected_publication \
            "$kubernetes_dir/attempts/$attempt_id/collected-state" "$attempt_id" \
            collected_terminal \
            "$kubernetes_dir/attempts/$attempt_id/control-bundle/executions" \
            >/dev/null || rc=1
        kubectl_local_lock_release "$lock_fd" || rc=1
        [[ "$rc" -eq 0 ]] || return "$rc"
        kubectl_emit_state "$collected_terminal"
        [[ "$collected_terminal" == SUCCESS ]]
        return
    fi
    if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]; then
        # This repair is local durable state and must not be blocked by a
        # Kubernetes identity check.  It closes the crash window between
        # terminalizing a failed resume and restoring its collected parent.
        kubectl_attempt_restore_predecessor "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" || rc=1
    fi
    [[ "$rc" -ne 0 ]] || _kubectl_verify_saved_cluster_identity || rc=1
    if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == PREPARED ]]; then
        kubectl_recover_prepared_attempt "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" || rc=1
        if [[ "$rc" -eq 0 ]]; then
            kubectl_emit_state SUBMISSION_FAILED
            [[ "$operation" != collect ]] || rc=1
        fi
    elif [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]; then
        kubectl_emit_state SUBMISSION_FAILED
        [[ "$operation" != collect ]] || rc=1
    elif [[ "$rc" -eq 0 && "$operation" == status ]]; then
        [[ "$rc" -ne 0 ]] || _kubectl_create_inspector inspector inspector_uid status \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" || rc=1
        [[ "$rc" -ne 0 ]] || remote_state=$(kubectl_read_remote_status "$KUBECTL_NAMESPACE" \
            "$inspector" "$attempt_id") || rc=1
        local job_state="" job_rc=0 observed_job_uid="" job_missing=0
        local job_kind="" job_name="" job_namespace="" job_uid=""
        if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED ]]; then
            kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" sweep || rc=1
            if [[ "$rc" -eq 0 ]]; then
                job_kind="$KUBECTL_RESOURCE_KIND"
                job_name="$KUBECTL_RESOURCE_NAME"
                job_namespace="$KUBECTL_RESOURCE_NAMESPACE"
                job_uid="$KUBECTL_RESOURCE_UID"
                kubectl_job_terminal_state job_state "$job_name" \
                    "$job_namespace" "$KUBECTL_RESOURCE_NONCE" \
                    "$attempt_id" "$job_uid" || job_rc=$?
            fi
            if [[ "$rc" -eq 0 && "$job_rc" -eq 1 ]]; then
                observed_job_uid=$(kubectl_run_observational -n "$job_namespace" \
                    get Job "$job_name" --ignore-not-found \
                    -o 'jsonpath={.metadata.uid}') || rc=1
                if [[ "$rc" -eq 0 && -z "$observed_job_uid" ]]; then
                    job_missing=1
                elif [[ "$rc" -eq 0 ]]; then
                    rc=1
                fi
            fi
        fi
        if [[ "$rc" -eq 0 && "$remote_state" =~ ^(PREPARED|RUNNING)$ \
                && "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED ]]; then
            # Endpoint identity is the more specific failure evidence.  Check
            # it before treating a failed Job as an unexplained coordinator
            # loss: a worker replacement can make the coordinator fail first.
            local endpoint_rc=0
            _kubectl_verify_saved_worker_endpoints "$kubernetes_dir" "$attempt_id" \
                || endpoint_rc=$?
            if [[ "$endpoint_rc" -eq 2 ]]; then
                remote_state=$(_kubectl_fail_drifted_attempt "$kubernetes_dir" \
                    "$lock_fd" "$inspector" "$attempt_id") || rc=1
            elif [[ "$endpoint_rc" -ne 0 ]]; then
                rc="$endpoint_rc"
            elif [[ ( "$job_rc" -eq 0 && "$job_state" == FAILED ) \
                    || "$job_missing" -eq 1 ]]; then
                kubectl_preserve_storage_failure_diagnostics \
                    "$kubernetes_dir/attempts/$attempt_id" "$job_namespace" \
                    "$inspector" "$attempt_id" "$job_kind" "$job_name" \
                    "$job_uid"
                kubectl_recover_lost_coordinator "$KUBECTL_NAMESPACE" "$inspector" \
                    "$attempt_id" || rc=1
                [[ "$rc" -ne 0 ]] || remote_state=$(kubectl_read_remote_status \
                    "$KUBECTL_NAMESPACE" "$inspector" "$attempt_id") || rc=1
            elif [[ "$job_rc" -eq 0 ]]; then
                echo "Error: completed Kubernetes Job lacks terminal durable state" >&2
                KUBECTL_ATTEMPT_ID="$attempt_id" \
                    KUBECTL_REMOTE_STATE="$remote_state" \
                    KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="$kubernetes_dir/attempts/$attempt_id/state.sh" \
                    KUBECTL_DIAGNOSTIC_REMOTE_STATE_PATH="$(kubectl_attempt_remote_root "$attempt_id")/state/run.status" \
                    kubectl_report_lifecycle_error status ledger-reconciliation \
                        LEDGER_INCONSISTENT \
                        "inspect the exact Job and PVC ledger; do not collect or delete resources" \
                        no "$job_kind" "$job_name" "$job_namespace" \
                        "$job_uid" "$job_uid" "" || true
                rc=1
            fi
        elif [[ "$rc" -eq 0 && "$remote_state" =~ ^(SUCCESS|FAILED|CANCELLED)$ \
                && "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED ]]; then
            # A failed Job may die after committing terminal run.status but
            # before replacing the summary or publication manifest. Repair
            # that bounded window before reporting terminal durability.
            if [[ ( "$job_rc" -eq 0 && "$job_state" == FAILED ) \
                    || "$job_missing" -eq 1 ]]; then
                kubectl_preserve_storage_failure_diagnostics \
                    "$kubernetes_dir/attempts/$attempt_id" "$job_namespace" \
                    "$inspector" "$attempt_id" "$job_kind" "$job_name" \
                    "$job_uid"
                kubectl_recover_lost_coordinator "$KUBECTL_NAMESPACE" "$inspector" \
                    "$attempt_id" || rc=1
            fi
        fi
        [[ -z "${inspector:-}" ]] || _kubectl_remove_inspector "$kubernetes_dir" \
            "$lock_fd" "$inspector" "$inspector_uid" "$attempt_id" || rc=1
        [[ "$rc" -ne 0 ]] || kubectl_emit_state "$remote_state"
    elif [[ "$rc" -eq 0 && "$operation" == cancel ]]; then
        if [[ "$KUBECTL_LIFECYCLE_STATE" == COLLECTED ]]; then
            kubectl_emit_state COLLECTED
        else
            _kubectl_create_inspector inspector inspector_uid status "$kubernetes_dir" "$lock_fd" "$attempt_id" || rc=1
            [[ "$rc" -ne 0 ]] || remote_state=$(kubectl_cancel_journaled_attempt \
                "$kubernetes_dir" "$lock_fd" "$KUBECTL_NAMESPACE" "$inspector" \
                "$attempt_id" "$KUBECTL_OWNERSHIP_NONCE") || rc=1
            [[ -z "${inspector:-}" ]] || _kubectl_remove_inspector "$kubernetes_dir" \
                "$lock_fd" "$inspector" "$inspector_uid" "$attempt_id" || rc=1
            [[ "$rc" -ne 0 ]] || kubectl_emit_state "$remote_state"
        fi
    elif [[ "$rc" -eq 0 && "$operation" == collect ]]; then
        kubectl_collect_attempt "$results_dir" "$kubernetes_dir" "$lock_fd" "$attempt_id" || rc=1
    fi
    kubectl_local_lock_release "$lock_fd" || rc=1
    return "$rc"
}

_kubectl_validate_collected_publication() {
    local state_dir="$1" attempt_id="$2" output_name="$3" definitions_dir="$4"
    local manifest="$state_dir/publication-manifest.tsv" line kind first second third fourth fifth extra
    [[ -d "$state_dir" && ! -L "$state_dir" && -f "$manifest" && ! -L "$manifest" \
        && -d "$definitions_dir" && ! -L "$definitions_dir" \
        && "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local terminal_state="" saw_schema=0 saw_attempt=0
    local -a result_rows=()
    local -A seen_executions=() execution_states=() seen_sources=()
    local -A seen_destinations=() source_roles=() source_destinations=()
    local -A destination_roles=() destination_sources=()
    while IFS= read -r line || [[ -n "$line" ]]; do
        IFS=$'\t' read -r kind first second third fourth fifth extra <<< "$line"
        case "$kind" in
            schema)
                [[ "$saw_schema" -eq 0 && "$first" == 1 && -z "${second:-}" ]] \
                    || return 1
                saw_schema=1
                ;;
            attempt_id)
                [[ "$saw_attempt" -eq 0 && "$first" == "$attempt_id" \
                    && -z "${second:-}" ]] || return 1
                saw_attempt=1
                ;;
            execution)
                [[ "$first" =~ ^[0-9]{4}$ \
                    && "$second" =~ ^(PENDING|RUNNING|SUCCESS|FAILED)$ \
                    && -z "${third:-}" && ! -v seen_executions["$first"] ]] \
                    || return 1
                seen_executions["$first"]=1
                execution_states["$first"]="$second"
                ;;
            ledger|snapshot|result)
                [[ "$first" =~ ^[A-Za-z0-9._/-]+$ \
                    && "$second" =~ ^[A-Za-z0-9._/-]+$ \
                    && "$third" =~ ^[0-9]+$ && "$fourth" =~ ^[0-9a-f]{64}$ \
                    && -z "${fifth:-}" ]] || return 1
                _kubectl_safe_relative_file_path "$first" \
                    && _kubectl_safe_relative_file_path "$second" \
                    && [[ ! -v seen_sources["$first"] \
                        && ! -v seen_destinations["$second"] ]] || return 1
                seen_sources["$first"]=1
                seen_destinations["$second"]=1
                source_roles["$first"]="$kind"
                source_destinations["$first"]="$second"
                destination_roles["$second"]="$kind"
                destination_sources["$second"]="$first"
                local source="$state_dir/$first" bytes digest
                [[ -f "$source" && ! -L "$source" ]] || return 1
                bytes=$(wc -c < "$source") || return 1
                digest=$(sha256sum -- "$source" | awk '{print $1}') || return 1
                [[ "$bytes" == "$third" && "$digest" == "$fourth" ]] || return 1
                [[ "$kind" != result ]] || result_rows+=("$first"$'\t'"$second")
                ;;
            *) return 1 ;;
        esac
    done < "$manifest"
    [[ "$saw_schema" -eq 1 && "$saw_attempt" -eq 1 ]] || return 1
    terminal_state=$(cat "$state_dir/run.status" 2>/dev/null || true)
    [[ "$terminal_state" =~ ^(SUCCESS|FAILED|CANCELLED)$ ]] || return 1
    local core
    for core in run.status run-summary.tsv env_used.sh env_used.yaml; do
        [[ -v source_roles["$core"] \
            && "${source_destinations[$core]}" == "$core" ]] || return 1
        if [[ "$core" == env_used.* ]]; then
            [[ "${source_roles[$core]}" == snapshot ]] || return 1
        else
            [[ "${source_roles[$core]}" == ledger ]] || return 1
        fi
    done
    local definition id status exit_path workers_path expected_count=0 failed_count=0
    local -A expected_executions=()
    for definition in "$definitions_dir"/[0-9][0-9][0-9][0-9].sh; do
        [[ -f "$definition" && ! -L "$definition" ]] || continue
        id=$(basename "$definition" .sh)
        [[ ! -v expected_executions["$id"] ]] || return 1
        expected_executions["$id"]=1
        expected_count=$((expected_count + 1))
    done
    [[ "$expected_count" -gt 0 \
        && "${#seen_executions[@]}" -eq "$expected_count" ]] || return 1
    for id in "${!seen_executions[@]}"; do
        [[ -v expected_executions["$id"] ]] || return 1
    done
    for id in "${!expected_executions[@]}"; do
        [[ -v execution_states["$id"] ]] || return 1
        status="${execution_states[$id]}"
        [[ "$status" != RUNNING ]] || return 1
        [[ -v source_roles["executions/$id.status"] \
            && "${source_roles[executions/$id.status]}" == ledger \
            && "${source_destinations[executions/$id.status]}" \
                == "executions/$id.status" \
            && "$(cat "$state_dir/executions/$id.status")" == "$status" ]] \
            || return 1
        exit_path="executions/$id.exitcode"
        workers_path="executions/$id.workers.tsv"
        case "$status" in
            SUCCESS)
                [[ -v source_roles["$exit_path"] \
                    && "${source_roles[$exit_path]}" == ledger \
                    && "${source_destinations[$exit_path]}" == "$exit_path" \
                    && "$(cat "$state_dir/$exit_path")" == 0 \
                    && -v source_roles["$workers_path"] \
                    && "${source_roles[$workers_path]}" == ledger \
                    && "${source_destinations[$workers_path]}" == "$workers_path" ]] \
                    || return 1
                local workload_rc=0
                _kubectl_execution_requires_workload_metadata \
                    "$definitions_dir/$id.sh" || workload_rc=$?
                case "$workload_rc" in
                    0)
                        [[ "${destination_roles[executions/$id.workload.tsv]:-}" \
                            == result \
                            && "${destination_sources[executions/$id.workload.tsv]:-}" \
                                == "results/$id/executions/$id.workload.tsv" ]] \
                            || return 1
                        ;;
                    1) ;;
                    *) return 1 ;;
                esac
                ;;
            FAILED)
                [[ -v source_roles["$exit_path"] \
                    && "${source_roles[$exit_path]}" == ledger \
                    && "${source_destinations[$exit_path]}" == "$exit_path" \
                    && "$(cat "$state_dir/$exit_path")" =~ ^[1-9][0-9]*$ ]] \
                    || return 1
                failed_count=$((failed_count + 1))
                ;;
            PENDING) ;;
            *) return 1 ;;
        esac
    done
    if [[ "$terminal_state" == SUCCESS ]]; then
        [[ "$failed_count" -eq 0 ]] || return 1
        for status in "${execution_states[@]}"; do
            [[ "$status" == SUCCESS ]] || return 1
        done
    elif [[ "$terminal_state" == FAILED ]]; then
        [[ "$failed_count" -gt 0 \
            || ( "${source_roles[startup-error.txt]:-}" == ledger \
                && "${source_destinations[startup-error.txt]:-}" == startup-error.txt ) ]] \
            || return 1
    fi
    printf -v "$output_name" '%s' "$terminal_state"
    printf '%s\0' "${result_rows[@]}"
}

_kubectl_execution_requires_workload_metadata() (
    # Execution definitions are the immutable local files generated before
    # submission, not content imported from the PVC. Use the same completion
    # contract as _elbencho_required_result_artifacts_for_execution().
    local definition="$1"
    [[ -f "$definition" && ! -L "$definition" ]] || return 2
    unset ELBENCHO_SWEEP_READ_FROM ELBENCHO_SINGLE_BIG_FILE
    unset ELBENCHO_FILE_LAYOUT ELBENCHO_FILES_PER_NODE
    ELBENCHO_FILE_LAYOUT=worker-directories
    # shellcheck disable=SC1090
    source "$definition" || return 2
    if [[ -n "${ELBENCHO_SWEEP_READ_FROM:-}" \
            && "${ELBENCHO_SINGLE_BIG_FILE:-0}" != 1 ]]; then
        return 0
    fi
    [[ "${ELBENCHO_FILE_LAYOUT:-worker-directories}" == shared-directory \
        && -n "${ELBENCHO_FILES_PER_NODE:-}" ]]
)

_kubectl_merge_collected_results() {
    local state_dir="$1" results_dir="$2"
    local manifest="$state_dir/publication-manifest.tsv" kind remote local_path bytes digest extra
    _kubectl_remove_superseded_collection_paths "$state_dir" "$results_dir" \
        || return 1
    while IFS=$'\t' read -r kind remote local_path bytes digest extra; do
        [[ "$kind" == result || "$kind" == ledger ]] || continue
        # Status has merge semantics of its own below: a collected SUCCESS is
        # immutable, while a failed status may be replaced by a resumed
        # attempt. Other manifest-declared ledgers are ordinary immutable
        # evidence and can use the digest-checked result copy path.
        [[ "$kind" != ledger \
            || "$local_path" != executions/[0-9][0-9][0-9][0-9].status ]] \
            || continue
        [[ -z "${extra:-}" ]] \
            && _kubectl_safe_relative_file_path "$remote" \
            && _kubectl_safe_relative_file_path "$local_path" || return 1
        local source="$state_dir/$remote" destination parent temporary
        [[ -f "$source" && ! -L "$source" ]] || return 1
        _kubectl_result_destination destination "$results_dir" "$local_path" 1 \
            || return 1
        parent=$(dirname "$destination")
        if [[ -e "$destination" ]]; then
            [[ -f "$destination" && ! -L "$destination" \
                && "$(sha256sum -- "$destination" | awk '{print $1}')" == "$digest" ]] || return 1
            continue
        fi
        temporary="$parent/.${destination##*/}.collect.${BASHPID:-$$}.${RANDOM}"
        if ! cp -- "$source" "$temporary" \
                || ! mv -n -- "$temporary" "$destination" \
                || [[ -e "$temporary" ]]; then
            rm -f -- "$temporary"
            return 1
        fi
    done < "$manifest"
    # Reporting and a later resume consume the ordinary result-side execution
    # ledger.  Publish terminal cells atomically after digest verification;
    # an existing SUCCESS may only be retained when it is byte-identical.
    local id source_status destination_status temporary
    for source_status in "$state_dir"/executions/[0-9][0-9][0-9][0-9].status; do
        [[ -f "$source_status" && ! -L "$source_status" ]] || continue
        id=$(basename "$source_status" .status)
        _kubectl_result_destination destination_status "$results_dir" \
            "executions/$id.status" 1 || return 1
        if [[ -e "$destination_status" ]]; then
            [[ -f "$destination_status" && ! -L "$destination_status" ]] || return 1
            if [[ "$(cat "$destination_status")" == SUCCESS ]]; then
                [[ "$(cat "$source_status")" == SUCCESS ]] || return 1
                # A prior collected SUCCESS is immutable; it belongs to a
                # different attempt and must not be rewritten by collection.
                continue
            fi
        fi
        temporary="$results_dir/executions/.${id}.status.collect.${BASHPID:-$$}.${RANDOM}"
        if ! cp -- "$source_status" "$temporary" || ! mv -f -- "$temporary" "$destination_status"; then
            rm -f -- "$temporary"
            return 1
        fi
    done
}

_kubectl_remove_superseded_collection_paths() {
    local state_dir="$1" results_dir="$2"
    local current_manifest="$state_dir/publication-manifest.tsv"
    local kind first second _rest prior_manifest remote local_path bytes digest extra
    local -A resumed_ids=()
    while IFS=$'\t' read -r kind first second _rest; do
        [[ "$kind" == execution && "$first" =~ ^[0-9]{4}$ ]] || continue
        resumed_ids["$first"]=1
    done < "$current_manifest"
    local restore_nullglob=0
    shopt -q nullglob && restore_nullglob=1
    shopt -s nullglob
    for prior_manifest in "$results_dir"/kubernetes/attempts/*/collected-state/publication-manifest.tsv; do
        [[ -f "$prior_manifest" && ! -L "$prior_manifest" ]] || continue
        while IFS=$'\t' read -r kind remote local_path bytes digest extra; do
            [[ "$kind" == result || "$kind" == ledger ]] || continue
            [[ -z "${extra:-}" && "$digest" =~ ^[0-9a-f]{64}$ ]] \
                && _kubectl_safe_relative_file_path "$remote" \
                && _kubectl_safe_relative_file_path "$local_path" || return 1
            local replace=0 id=""
            if [[ "$remote" =~ ^results/([0-9]{4})/ ]]; then
                id="${BASH_REMATCH[1]}"
                [[ -v resumed_ids["$id"] ]] && replace=1
            elif [[ "$remote" =~ ^executions/([0-9]{4})\.(exitcode|workers\.tsv)$ ]]; then
                id="${BASH_REMATCH[1]}"
                [[ -v resumed_ids["$id"] ]] && replace=1
            elif [[ "$kind" == ledger \
                    && "$local_path" =~ ^(run\.status|run-summary\.tsv)$ ]]; then
                replace=1
            fi
            [[ "$replace" -eq 1 ]] || continue
            local destination
            _kubectl_result_destination destination "$results_dir" "$local_path" 0 \
                || return 1
            [[ -e "$destination" ]] || continue
            [[ -f "$destination" && ! -L "$destination" \
                && "$(sha256sum -- "$destination" | awk '{print $1}')" == "$digest" ]] \
                || return 1
            rm -f -- "$destination" || return 1
        done < "$prior_manifest"
    done
    [[ "$restore_nullglob" -eq 1 ]] || shopt -u nullglob
}

kubectl_collect_attempt() {
    local results_dir="$1" kubernetes_dir="$2" lock_fd="$3" attempt_id="$4"
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id" inspector inspector_uid
    local archive_root archive staging state_dir remote_state rc=0
    kubectl_attempt_load_metadata "$metadata_dir" || return 1
    case "$KUBECTL_LIFECYCLE_STATE" in SUBMITTED|TERMINAL|COLLECTION_IN_PROGRESS) ;; *) return 1 ;; esac
    _kubectl_scavenge_collection_staging "$results_dir" || return 1
    if [[ "$KUBECTL_LIFECYCLE_STATE" == COLLECTION_IN_PROGRESS \
            && -d "$metadata_dir/collected-state" ]]; then
        local recovered_terminal=""
        _kubectl_validate_collected_publication "$metadata_dir/collected-state" "$attempt_id" \
            recovered_terminal "$metadata_dir/control-bundle/executions" \
            >/dev/null || return 1
        # Durable import precedes cleanup. Recover every remaining exact,
        # journaled release/delete step before advertising COLLECTED.
        local recovery_rc=0
        _kubectl_create_inspector inspector inspector_uid collector "$kubernetes_dir" \
            "$lock_fd" "$attempt_id" || recovery_rc=1
        [[ "$recovery_rc" -ne 0 ]] || kubectl_release_journaled_remote_attempt \
            "$kubernetes_dir" "$lock_fd" "$KUBECTL_NAMESPACE" "$inspector" \
            "$attempt_id" || recovery_rc=1
        [[ "$recovery_rc" -ne 0 ]] || kubectl_cleanup_journaled_resources \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" || recovery_rc=1
        [[ -z "${inspector:-}" ]] || _kubectl_remove_inspector "$kubernetes_dir" \
            "$lock_fd" "$inspector" "$inspector_uid" "$attempt_id" || recovery_rc=1
        [[ "$recovery_rc" -eq 0 ]] || return "$recovery_rc"
        kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            COLLECTED || return 1
        kubectl_emit_state "$recovered_terminal"
        [[ "$recovered_terminal" == SUCCESS ]]
        return
    fi
    _kubectl_create_inspector inspector inspector_uid collector "$kubernetes_dir" "$lock_fd" "$attempt_id" || return 1
    remote_state=$(kubectl_read_remote_status "$KUBECTL_NAMESPACE" "$inspector" "$attempt_id") || rc=1
    if [[ "$rc" -eq 0 && "$remote_state" =~ ^(PREPARED|RUNNING|SUCCESS|FAILED|CANCELLED)$ ]]; then
        # A coordinator Pod can be killed after acquiring its durable lock and
        # either before or after publishing RUNNING. The Job's exact journaled
        # identity is the authority for deciding that no coordinator remains.
        local collect_job_state="" collect_job_rc=0 collect_job_uid=""
        local collect_job_kind="" collect_job_name="" collect_job_namespace=""
        local collect_expected_uid=""
        kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" sweep || rc=1
        if [[ "$rc" -eq 0 ]]; then
            collect_job_kind="$KUBECTL_RESOURCE_KIND"
            collect_job_name="$KUBECTL_RESOURCE_NAME"
            collect_job_namespace="$KUBECTL_RESOURCE_NAMESPACE"
            collect_expected_uid="$KUBECTL_RESOURCE_UID"
            kubectl_job_terminal_state collect_job_state "$collect_job_name" \
                "$collect_job_namespace" "$KUBECTL_RESOURCE_NONCE" \
                "$attempt_id" "$collect_expected_uid" || collect_job_rc=$?
        fi
        if [[ "$rc" -eq 0 && "$collect_job_rc" -eq 1 ]]; then
            collect_job_uid=$(kubectl_run_observational -n "$collect_job_namespace" \
                get Job "$collect_job_name" --ignore-not-found \
                -o 'jsonpath={.metadata.uid}') || rc=1
            [[ "$rc" -ne 0 || -z "$collect_job_uid" ]] || rc=1
        fi
        if [[ "$rc" -eq 0 \
                && ( "$collect_job_state" == FAILED \
                    || ( "$collect_job_rc" -eq 1 && -z "$collect_job_uid" ) ) ]]; then
            kubectl_preserve_storage_failure_diagnostics "$metadata_dir" \
                "$collect_job_namespace" "$inspector" "$attempt_id" \
                "$collect_job_kind" "$collect_job_name" "$collect_expected_uid"
            kubectl_recover_lost_coordinator "$KUBECTL_NAMESPACE" "$inspector" \
                "$attempt_id" || rc=1
            [[ "$rc" -ne 0 ]] || remote_state=$(kubectl_read_remote_status \
                "$KUBECTL_NAMESPACE" "$inspector" "$attempt_id") || rc=1
        fi
    fi
    [[ "$rc" -ne 0 || "$remote_state" =~ ^(SUCCESS|FAILED|CANCELLED)$ ]] || rc=1
    if [[ "$rc" -eq 0 ]]; then
        local terminal_endpoint_rc=0
        _kubectl_verify_saved_worker_endpoints "$kubernetes_dir" "$attempt_id" \
            || terminal_endpoint_rc=$?
        if [[ "$terminal_endpoint_rc" -eq 2 ]]; then
            if kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" delete-sweep \
                    && [[ -f "$metadata_dir/endpoint-drift.tsv" \
                        && ! -L "$metadata_dir/endpoint-drift.tsv" ]]; then
                : # Status already froze the drift evidence and failed the Job.
            else
                echo "Error: worker identity drift prevents safe Kubernetes result collection" >&2
                rc=1
            fi
        elif [[ "$terminal_endpoint_rc" -ne 0 ]]; then
            rc="$terminal_endpoint_rc"
        fi
    fi
    # Durable ledger publication precedes Kubernetes Job status propagation.
    # Collection must wait for this exact Job to be terminal (or for its
    # journaled cancellation deletion to be observably complete) before it
    # imports data or starts foreground cleanup.
    [[ "$rc" -ne 0 ]] || kubectl_wait_journaled_job_quiescent \
        "$kubernetes_dir" "$attempt_id" \
        "$KUBECTL_JOB_QUIESCENCE_TIMEOUT_SECONDS_DEFAULT" 1 || rc=1
    if [[ "$rc" -eq 0 ]]; then
        local remote_apparent_bytes=""
        kubectl_remote_attempt_apparent_bytes "$KUBECTL_NAMESPACE" "$inspector" \
            "$attempt_id" remote_apparent_bytes || rc=1
        [[ "$rc" -ne 0 ]] || _kubectl_require_collection_capacity "$results_dir" \
            "$remote_apparent_bytes" || rc=1
    fi
    if [[ "$rc" -eq 0 ]]; then
        archive_root=$(mktemp -d \
            "$results_dir/.kubernetes-collect-work-$attempt_id.XXXXXXXX") || rc=1
        archive="$archive_root/attempt.tar"
    fi
    [[ "$rc" -ne 0 ]] || kubectl_stream_remote_attempt "$KUBECTL_NAMESPACE" "$inspector" \
        "$attempt_id" "$archive" || rc=1
    if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED ]]; then
        kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" TERMINAL || rc=1
        kubectl_attempt_load_metadata "$metadata_dir" || rc=1
    fi
    if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == TERMINAL ]]; then
        kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            COLLECTION_IN_PROGRESS || rc=1
    fi
    staging="$results_dir/.kubernetes-collect-$attempt_id.${RANDOM}"
    [[ "$rc" -ne 0 ]] || kubectl_extract_attempt_archive "$archive" "$attempt_id" \
        "$results_dir" "$staging" || rc=1
    state_dir="$staging/$attempt_id/state"
    local verified_terminal=""
    [[ "$rc" -ne 0 ]] || _kubectl_validate_collected_publication "$state_dir" \
        "$attempt_id" verified_terminal "$metadata_dir/control-bundle/executions" \
        >/dev/null || rc=1
    [[ "$rc" -ne 0 ]] || [[ "$verified_terminal" == "$remote_state" ]] || rc=1
    [[ "$rc" -ne 0 ]] || _kubectl_merge_collected_results "$state_dir" "$results_dir" || rc=1
    if [[ "$rc" -eq 0 ]]; then
        local collected="$metadata_dir/collected-state"
        [[ ! -e "$collected" ]] || rm -rf -- "$collected"
        mv -- "$state_dir" "$collected" || rc=1
    fi
    if [[ -n "${archive_root:-}" ]] && ! rm -rf -- "$archive_root"; then
        echo "Error: failed to remove Kubernetes collection archive staging" >&2
        rc=1
    fi
    if [[ -n "${staging:-}" ]] && ! rm -rf -- "$staging"; then
        echo "Error: failed to remove Kubernetes collection extraction staging" >&2
        rc=1
    fi
    if [[ "$rc" -eq 0 ]]; then
        # Collection created a fresh exact helper after the upload Pod was
        # removed.  It releases the durable reservation before its own delete.
        kubectl_release_journaled_remote_attempt "$kubernetes_dir" "$lock_fd" \
            "$KUBECTL_NAMESPACE" "$inspector" "$attempt_id" || rc=1
        [[ "$rc" -ne 0 ]] || kubectl_cleanup_journaled_resources "$kubernetes_dir" \
            "$lock_fd" "$attempt_id" || rc=1
    fi
    _kubectl_remove_inspector "$kubernetes_dir" "$lock_fd" "$inspector" \
        "$inspector_uid" "$attempt_id" || rc=1
    [[ "$rc" -ne 0 ]] || kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" COLLECTED || rc=1
    [[ "$rc" -ne 0 ]] || kubectl_emit_state "$remote_state"
    [[ "$remote_state" == SUCCESS ]] || rc=1
    return "$rc"
}

kubectl_resume_collected_sweep() {
    local results_dir="$1" attempt_id lock_fd state_dir
    local kubernetes_dir="$results_dir/kubernetes"
    kubectl_local_lock_acquire "$kubernetes_dir" lock_fd || return 1
    local rc=0
    _kubectl_load_saved_attempt "$results_dir" attempt_id || rc=1
    [[ "$rc" -ne 0 ]] || kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$attempt_id" || rc=1
    [[ "$rc" -ne 0 || "$KUBECTL_LIFECYCLE_STATE" == COLLECTED ]] || rc=1
    state_dir="$kubernetes_dir/attempts/$attempt_id/collected-state"
    # The coordinator provides the authoritative, collection-gated decision.
    # A new attempt receives only non-SUCCESS definitions; old successful
    # artifacts remain in the ordinary result tree and are never re-run.
    [[ "$rc" -ne 0 ]] || "$results_dir/kubernetes/attempts/$attempt_id/control-bundle/coordinator.sh" \
        --select-collected-resume "$state_dir" > "$results_dir/executions/.kubectl-resume.tsv" || rc=1
    local selection="$results_dir/executions/.kubectl-resume.tsv"
    local selection_ids="$results_dir/executions/.kubectl-resume-ids.tsv"
    local max_nodes=0 id action nodes
    if [[ "$rc" -eq 0 ]]; then
        : > "$selection_ids" || rc=1
        while IFS=$'\t' read -r id action; do
            [[ "$id" =~ ^[0-9]{4}$ && "$action" =~ ^(SKIP|RUN)$ ]] || { rc=1; break; }
            [[ "$action" == RUN ]] || continue
            printf '%s\n' "$id" >> "$selection_ids" || { rc=1; break; }
            nodes=$(sed -n 's/^export nodes=//p' "$results_dir/executions/$id.sh")
            [[ "$nodes" =~ ^[1-9][0-9]*$ ]] || { rc=1; break; }
            (( nodes <= max_nodes )) || max_nodes="$nodes"
        done < "$selection"
    fi
    kubectl_local_lock_release "$lock_fd" || rc=1
    [[ "$rc" -eq 0 ]] || return "$rc"
    [[ "$max_nodes" -gt 0 ]] || {
        kubectl_emit_state COLLECTED
        return 0
    }
    # env_used is an immutable result-side snapshot; no current env.sh is
    # consulted while reconstructing a collected Kubernetes attempt.
    # shellcheck disable=SC1090,SC1091
    source "$results_dir/env_used.sh" || return 1
    # Rehydrate immutable Kubernetes identity/configuration after the saved
    # workload snapshot.  A current env.sh is intentionally never consulted.
    # shellcheck disable=SC1090,SC1091
    source "$kubernetes_dir/attempts/$attempt_id/configuration.sh" || return 1
    export KUBECTL_SUBMIT_EXECUTIONS_FILE="$selection_ids"
    kubectl_submit_sweep "$results_dir" "$max_nodes" "$attempt_id"
}
