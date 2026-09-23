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

readonly KUBECTL_SWEEP_MOUNT_ROOT=/mnt/storage-scale-test
readonly KUBECTL_SWEEP_RESERVED_ROOT=.storage-scale-test
readonly KUBECTL_ATTEMPT_SCHEMA_VERSION=1
readonly KUBECTL_ATTEMPT_RESOURCE_SCHEMA_VERSION=1
readonly KUBECTL_COLLECTION_MAX_BYTES=$((2 * 1024 * 1024 * 1024))
readonly KUBECTL_COLLECTION_MAX_MEMBERS=50000
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
    timeout --foreground --kill-after=5s "${process_timeout}s" \
        kubectl --request-timeout="${request_timeout}s" "$@"
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
    observed=$(kubectl_run_bounded -n "$namespace" get "$kind" "$name" \
        -o "jsonpath=$jsonpath") || return 1
    local observed_kind observed_name observed_uid observed_nonce observed_run
    IFS=$'\t' read -r observed_kind observed_name observed_uid observed_nonce \
        observed_run <<< "$observed"
    [[ "${observed_kind,,}" == "${kind,,}" && "$observed_name" == "$name" \
        && "$observed_nonce" == "$nonce" \
        && "$observed_run" == "$run_id" ]] || return 1
    kubectl_validate_uid "$observed_uid" || return 1
    [[ -z "$expected_uid" || "$observed_uid" == "$expected_uid" ]] || return 1
    printf '%s\n' "$observed_uid"
}

kubectl_create_owned_object() {
    local output_variable="$1" kind="$2" name="$3" namespace="$4"
    local nonce="$5" run_id="$6" manifest="$7"
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
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
    mkdir -p "$steps_dir" || return 1
    local tmp="$steps_dir/$step.tmp.${BASHPID:-$$}.$RANDOM"
    if ! : > "$tmp" || ! mv -n "$tmp" "$steps_dir/$step" 2>/dev/null \
            || [[ -e "$tmp" ]]; then
        rm -f -- "$tmp"
        return 1
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
    [[ ! -e "$tmp" ]] || return 1
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
    uid=$(kubectl_run_bounded "${args[@]}") || return 1
    kubectl_validate_uid "$uid" || return 1
    printf '%s\n' "$uid"
}

kubectl_validate_cluster_identity() {
    kubectl_validate_runtime_configuration || return 1
    kubectl_run_bounded version >/dev/null || return 1
    local namespace_uid pv_uid pvc_uid volume_name
    namespace_uid=$(kubectl_get_object_uid namespace "$KUBECTL_NAMESPACE") || return 1
    pv_uid=$(kubectl_get_object_uid pv "$KUBECTL_PV") || return 1
    pvc_uid=$(kubectl_get_object_uid pvc "$KUBECTL_PVC" "$KUBECTL_NAMESPACE") || return 1
    volume_name=$(kubectl_run_bounded -n "$KUBECTL_NAMESPACE" get pvc "$KUBECTL_PVC" \
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
    # Validate the configured image and PVC identity under the actual workload
    # UID/GID. This creates no durable attempt state and always deletes only
    # the exact helper it created.
    kubectl_validate_cluster_identity >/dev/null || return 1
    local nodes_dir nodes_path helper_uid attempt_id nonce operation_token node helper_name rc=0
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
    operation_token=$(_kubectl_random_hex 4) || return 1
    helper_name="sst-elb-$attempt_id-status-$operation_token"
    kubectl_create_helper_pod helper_uid status "$KUBECTL_NAMESPACE" "$helper_name" \
        "$nonce" "$attempt_id" "$node" "$operation_token" || return 1
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    kubectl_pvc_exec "$KUBECTL_NAMESPACE" "$helper_name" /bin/bash -ceu '
        test "$(id -u)" = "$1"
        test "$(id -g)" = "$2"
        command -v elbencho
        command -v bash
        command -v tar
        command -v realpath
        test -r /mnt/storage-scale-test && test -w /mnt/storage-scale-test
    ' bash "$KUBECTL_RUN_AS_USER" "$KUBECTL_RUN_AS_GROUP" || rc=1
    kubectl_delete_owned_object Pod "$helper_name" "$KUBECTL_NAMESPACE" "$nonce" \
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
    rows=$(kubectl_run_bounded get nodes -l "$selector" -o "jsonpath=$jsonpath") || return 1
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
    kubectl_validate_namespace_name "$namespace" \
        && kubectl_validate_object_name "$pod_name" \
        && [[ "$deadline_seconds" =~ ^[1-9][0-9]*$ ]] || return 1
    local deadline=$((SECONDS + deadline_seconds)) remaining
    while (( SECONDS < deadline )); do
        remaining=$((deadline - SECONDS))
        if KUBECTL_REQUEST_TIMEOUT_SECONDS=$(( remaining < 10 ? remaining : 10 )) \
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
    return 1
}

kubectl_create_helper_pod() {
    local output_variable="$1" template_name="$2" namespace="$3" name="$4"
    local nonce="$5" run_id="$6" node_name="$7" operation_token="$8"
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
        "$nonce" "$run_id" "$manifest" || return 1
    if ! kubectl_wait_owned_ready_pod "$namespace" "$name" "$nonce" "$run_id"; then
        kubectl_delete_owned_object Pod "$name" "$namespace" "$nonce" "$run_id" \
            "$_kubectl_created_helper_uid" || true
        return 1
    fi
    printf -v "$output_variable" '%s' "$_kubectl_created_helper_uid"
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
        made_lock=0 made_run=0
        cleanup() {
            [[ $made_run -eq 0 ]] || rm -rf -- "$run"
            [[ $made_lock -eq 0 ]] || rm -rf -- "$lock"
        }
        trap cleanup ERR
        for path in "$root" "$root/locks" "$root/runs"; do
            if [[ -e "$path" || -L "$path" ]]; then
                [[ -d "$path" && ! -L "$path" ]] || exit 1
            else
                mkdir -- "$path"
            fi
        done
        mkdir -- "$lock"
        made_lock=1
        owner="$lock/owner"
        tmp="$lock/.owner.$$"
        printf "%s\\t%s\\n" "$attempt" "$nonce" > "$tmp"
        mv -- "$tmp" "$owner"
        mkdir -- "$run"
        made_run=1
        trap - ERR
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
    local remote_run lock_dir
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    lock_dir=$(kubectl_attempt_remote_lock_directory) || return 1
    if [[ -n "$kubernetes_dir" || -n "$lock_fd" ]]; then
        _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
        kubectl_attempt_load_remote_reservation "$kubernetes_dir" "$attempt_id" || return 1
        [[ "$KUBECTL_REMOTE_OWNERSHIP_NONCE" == "$nonce" ]] || return 1
        remote_run="$KUBECTL_REMOTE_RUN_DIRECTORY"
        lock_dir="$KUBECTL_REMOTE_LOCK_DIRECTORY"
    fi
    if [[ -z "$kubernetes_dir" ]] || ! kubectl_attempt_step_done \
            "$kubernetes_dir" "$attempt_id" release-remote-run; then
        # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
        kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu '
            lock=$1 run=$2 attempt=$3 nonce=$4
            expected=$(printf "%s\\t%s" "$attempt" "$nonce")
            case "$run" in /mnt/storage-scale-test/.storage-scale-test/runs/????????) ;; *) exit 1 ;; esac
            if [[ ! -e "$lock" && ! -L "$lock" && ! -e "$run" && ! -L "$run" ]]; then
                exit 0
            fi
            [[ -d "$lock" && ! -L "$lock" \
                && $(cat -- "$lock/owner") == "$expected" ]] || exit 1
            [[ ! -L "$run" ]] || exit 1
            rm -rf -- "$run"
            [[ ! -e "$run" && ! -L "$run" ]]
        ' bash "$lock_dir" "$remote_run" "$attempt_id" "$nonce" || return 1
        [[ -z "$kubernetes_dir" ]] || kubectl_attempt_journal_step \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" release-remote-run || return 1
    fi
    if [[ -z "$kubernetes_dir" ]] || ! kubectl_attempt_step_done \
            "$kubernetes_dir" "$attempt_id" release-remote-lock; then
        # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
        kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu '
            lock=$1 run=$2 attempt=$3 nonce=$4
            expected=$(printf "%s\\t%s" "$attempt" "$nonce")
            [[ ! -e "$run" && ! -L "$run" ]] || exit 1
            if [[ ! -e "$lock" && ! -L "$lock" ]]; then
                exit 0
            fi
            [[ -d "$lock" && ! -L "$lock" \
                && $(cat -- "$lock/owner") == "$expected" ]] || exit 1
            rm -rf -- "$lock"
            [[ ! -e "$lock" && ! -L "$lock" ]]
        ' bash "$lock_dir" "$remote_run" "$attempt_id" "$nonce" || return 1
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
    local jsonpath
    # Put the optional deletion timestamp last. Bash collapses adjacent IFS
    # whitespace, so an empty field in the middle would shift readiness and
    # image evidence into the wrong columns.
    jsonpath='{range .items[*]}{.spec.nodeName}{"\t"}{.metadata.name}{"\t"}{.metadata.uid}{"\t"}{.status.podIP}{"\t"}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\t"}{range .status.containerStatuses[?(@.name=="elbencho")]}{.imageID}{end}{"\t"}{.metadata.deletionTimestamp}{"\n"}{end}'
    local rows
    rows=$(kubectl_run_bounded -n "$namespace" get pods \
        -l "storage-scale-test.nvidia.com/run=$run_id,app.kubernetes.io/component=workers" \
        -o "jsonpath=$jsonpath") || return 1
    local tmp="$output_path.tmp.${BASHPID:-$$}.$RANDOM"
    local -A node_uid=() node_arch=() seen=() seen_ip=()
    local node uid arch
    while IFS=$'\t' read -r node uid arch; do
        [[ -n "$node" ]] || continue
        node_uid["$node"]="$uid"
        node_arch["$node"]="$arch"
    done < "$nodes_path"
    local pod pod_uid ip deletion_timestamp ready image_id count=0
    while IFS=$'\t' read -r node pod pod_uid ip ready image_id deletion_timestamp; do
        [[ -n "$node" ]] || continue
        # A DaemonSet rollout can briefly expose the terminating Pod beside
        # its Ready replacement.  Only nonterminating Pods are candidates;
        # duplicate live candidates still fail closed below.
        [[ -z "$deletion_timestamp" ]] || continue
        if ! [[ -v node_uid["$node"] && ! -v seen["$node"] \
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
        printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$node" "${node_uid[$node]}" \
            "$pod" "$pod_uid" "$ip" "${node_arch[$node]}" "$image_id" >> "$tmp" || return 1
        count=$((count + 1))
    done <<< "$rows"
    [[ "$count" -eq "${#node_uid[@]}" ]] || {
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
    [[ "$deadline_seconds" =~ ^[1-9][0-9]*$ ]] || return 1
    local deadline=$((SECONDS + deadline_seconds))
    local remaining
    while (( SECONDS < deadline )); do
        remaining=$((deadline - SECONDS))
        if KUBECTL_REQUEST_TIMEOUT_SECONDS=$(( remaining < 10 ? remaining : 10 )) \
            KUBECTL_PROCESS_TIMEOUT_SECONDS="$remaining" \
            kubectl_discover_worker_endpoints "$namespace" "$run_id" "$nodes_path" "$output_path" \
            && kubectl_validate_worker_endpoint_images "$output_path" \
                "${KUBECTL_EXPECTED_IMAGE_ID:-}"; then
            return 0
        fi
        rm -f -- "$output_path"
        sleep 1
    done
    echo "Error: timed out waiting for the Kubernetes worker DaemonSet" >&2
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
    local node _frozen_node_uid frozen_pod frozen_uid frozen_ip _frozen_arch _frozen_image frozen_extra
    local current_node current_pod current_uid current_ip current_extra status
    local drift=0 parse_error=0
    declare -A current_rows=() seen_frozen=()
    while IFS=$'\t' read -r current_node _current_node_uid current_pod current_uid \
            current_ip _current_arch _current_image current_extra; do
        [[ -z "${current_extra:-}" && -n "$current_node" ]] || {
            parse_error=1
            break
        }
        current_rows["$current_node"]="${current_pod}"$'\t'"${current_uid}"$'\t'"${current_ip}"
    done < "$current_path"
    if [[ "$parse_error" -ne 0 ]]; then
        rm -f -- "$current_path" "$tmp"
        return 1
    fi
    : > "$tmp" || {
        rm -f -- "$current_path"
        return 1
    }
    while IFS=$'\t' read -r node _frozen_node_uid frozen_pod frozen_uid frozen_ip \
            _frozen_arch _frozen_image frozen_extra; do
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
        IFS=$'\t' read -r current_pod current_uid current_ip <<< "${current_rows[$node]}"
        if [[ "$current_ip" != "$frozen_ip" ]]; then
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
            IFS=$'\t' read -r current_pod current_uid current_ip <<< \
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
        observed_uid=$(kubectl_run_bounded -n "$namespace" get "$kind" "$name" \
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
    kubectl_verify_object_identity Job "$name" "$namespace" "$nonce" "$run_id" \
        "$expected_uid" >/dev/null || return 1
    local evidence active failed failed_status
    evidence=$(kubectl_run_bounded -n "$namespace" get Job "$name" \
        -o 'jsonpath={.status.active}{"|"}{.status.failed}{"|"}{range .status.conditions[?(@.type=="Failed")]}{.status}{end}') || return 1
    IFS='|' read -r active failed failed_status <<< "$evidence"
    [[ -z "$active" && "$failed" =~ ^[1-9][0-9]*$ && "$failed_status" == True ]]
}

kubectl_recover_lost_coordinator() {
    local namespace="$1" helper_pod="$2" attempt_id="$3"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local remote_run control state scratch
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    control="$remote_run/control"
    state="$remote_run/state"
    scratch="/tmp/storage-scale-test/$attempt_id"
    # The recovery mode is part of the digest-verified coordinator bundle and
    # only edits a PVC attempt whose Job has already reached Failed. It does
    # not need API credentials or a new coordinator Pod.
    # shellcheck disable=SC2016  # Positional parameters expand in the helper Pod.
    kubectl_pvc_exec "$namespace" "$helper_pod" /bin/bash -ceu \
        'exec "$1/coordinator.sh" --recover-lost "$1" "$2" "$3" "$4"' \
        bash "$control" "$state" "$scratch" "$attempt_id"
}

kubectl_finalize_cancelled_attempt() {
    local namespace="$1" helper_pod="$2" attempt_id="$3"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local remote_run control state scratch
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    control="$remote_run/control"
    state="$remote_run/state"
    scratch="/tmp/storage-scale-test/$attempt_id"
    # The Job has already been deleted. Finalize its durable ledger through a
    # short-lived helper so cancellation remains collectable even when the
    # coordinator did not receive enough grace time to publish its own exit.
    # shellcheck disable=SC2016  # Positional parameters expand in the helper Pod.
    kubectl_pvc_exec "$namespace" "$helper_pod" /bin/bash -ceu \
        'exec "$1/coordinator.sh" --finalize-cancelled "$1" "$2" "$3" "$4"' \
        bash "$control" "$state" "$scratch" "$attempt_id"
}

kubectl_record_remote_status() {
    local namespace="$1" pod_name="$2" attempt_id="$3" status="$4"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ \
        && "$status" =~ ^(PREPARED|RUNNING|SUCCESS|FAILED|CANCELLED)$ ]] || return 1
    local remote_run
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu '
        run=$1 status=$2
        [[ -d "$run/state" ]] || exit 1
        tmp="$run/state/run.status.tmp.$$"
        printf "%s\\n" "$status" > "$tmp"
        mv -f -- "$tmp" "$run/state/run.status"
    ' bash "$remote_run" "$status"
}

kubectl_read_remote_status() {
    local namespace="$1" pod_name="$2" attempt_id="$3"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local remote_run
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    local status
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    status=$(kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu \
        'test -f "$1/state/run.status" && cat -- "$1/state/run.status"' bash "$remote_run") || return 1
    [[ "$status" =~ ^(PREPARED|RUNNING|SUCCESS|FAILED|CANCELLED)$ ]] || return 1
    printf '%s\n' "$status"
}

kubectl_stream_remote_attempt() {
    local namespace="$1" pod_name="$2" attempt_id="$3" archive_path="$4"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && -n "$archive_path" && ! -e "$archive_path" ]] || return 1
    local remote_run parent base
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    parent="${remote_run%/*}"
    base="${remote_run##*/}"
    umask 077
    kubectl_pvc_exec "$namespace" "$pod_name" tar -C "$parent" -cf - "$base" > "$archive_path" || {
        rm -f -- "$archive_path"
        return 1
    }
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
        "$attempt_id" "$manifest" || return 1
    kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        worker-net NetworkPolicy "$worker_name" "$namespace" "$uid" "$nonce" || {
            kubectl_delete_owned_object NetworkPolicy "$worker_name" "$namespace" "$nonce" \
                "$attempt_id" "$uid" || true
            return 1
        }
    manifest=$(kubectl_render_attempt_template "$template_dir/coordinator-network-policy.yaml.tmpl" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$coordinator_name" "ATTEMPT_ID=$attempt_id" \
        "OWNERSHIP_NONCE=$nonce") || return 1
    kubectl_create_owned_object uid NetworkPolicy "$coordinator_name" "$namespace" "$nonce" \
        "$attempt_id" "$manifest" || {
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
        "$attempt_id" "$manifest" || return 1
    kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        workers DaemonSet "$name" "$namespace" "$uid" "$nonce" || {
            kubectl_delete_owned_object DaemonSet "$name" "$namespace" "$nonce" \
                "$attempt_id" "$uid" || true
            return 1
        }
}

kubectl_initialize_remote_control_tree() {
    local namespace="$1" pod_name="$2" attempt_id="$3"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local remote_run
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu '
        run=$1
        test -d "$run"
        [[ ! -e "$run/control" && ! -e "$run/state" && ! -e "$run/results" ]] || exit 1
        mkdir -- "$run/control" "$run/state" "$run/results"
        mkdir -- "$run/control/executions" "$run/state/executions" "$run/results/executions"
        umask 077
        : > "$run/state/publication-manifest.tsv"
        printf "PREPARED\\n" > "$run/state/run.status"
    ' bash "$remote_run"
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
    if ! kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu \
            'run=$1; test -d "$run/control"; tar -C "$run/control" -xf -' \
            bash "$remote_run" < "$archive_path"; then
        rm -f -- "$archive_path"
        return 1
    fi
    rm -f -- "$archive_path"
}

kubectl_cleanup_journaled_resources() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    local key resource_file primary_rc=0 rc
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
    local mapped_read_from="${5:-}" kubernetes_dir lock_fd generated_attempt_id nonce
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
        && "$required_nodes" =~ ^[1-9][0-9]*$ && -d "$results_dir" ]] || return 1
    _kubectl_validate_local_directory_path "$results_dir" || return 1
    kubernetes_dir="$results_dir/kubernetes"
    kubectl_local_lock_acquire "$kubernetes_dir" lock_fd || return 1
    local primary_rc=0 namespace_uid pv_uid pvc_uid candidate_nodes coordinator_node
    local helper_name helper_uid operation_token remote_reserved=0
    if ! IFS=$'\t' read -r namespace_uid pv_uid pvc_uid < <(kubectl_validate_cluster_identity); then
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
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        kubectl_attempt_write_state "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" PREPARED \
            && kubectl_attempt_write_configuration "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" \
                "$mapped_dirs_name" "$mapped_read_from" \
            && kubectl_attempt_write_current "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" \
            || primary_rc=1
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
            "$nonce" "$generated_attempt_id" "$coordinator_node" "$operation_token" || primary_rc=1
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" transfer \
            Pod "$helper_name" "$KUBECTL_NAMESPACE" "$helper_uid" "$nonce" || {
                kubectl_delete_owned_object Pod "$helper_name" "$KUBECTL_NAMESPACE" "$nonce" \
                    "$generated_attempt_id" "$helper_uid" || true
                primary_rc=1
            }
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
            || primary_rc=1
    fi
    if [[ "$primary_rc" -ne 0 ]]; then
        if [[ -n "${generated_attempt_id:-}" && -f "$kubernetes_dir/attempts/$generated_attempt_id/state.sh" ]]; then
            kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$generated_attempt_id" >/dev/null 2>&1 \
                && [[ "$KUBECTL_LIFECYCLE_STATE" == PREPARED ]] \
                && kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" SUBMISSION_FAILED \
                || true
        fi
        if [[ "$remote_reserved" -eq 1 ]]; then
            kubectl_release_journaled_remote_attempt "$kubernetes_dir" "$lock_fd" \
                "$KUBECTL_NAMESPACE" "$helper_name" "$generated_attempt_id" || true
        fi
        [[ -z "${generated_attempt_id:-}" ]] || kubectl_cleanup_journaled_resources \
            "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" || true
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
    kubectl_verify_object_identity DaemonSet "$KUBECTL_RESOURCE_NAME" \
        "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
        "$KUBECTL_RESOURCE_UID" >/dev/null || return 1
    local deadline=$((SECONDS + ${KUBECTL_ENDPOINT_STABILIZE_TIMEOUT_SECONDS:-30})) rc
    while (( SECONDS < deadline )); do
        rc=0
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
    if [[ "$remote_state" == RUNNING ]]; then
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
    kubectl_create_helper_pod uid "$template_name" "$KUBECTL_NAMESPACE" "$name" \
        "$KUBECTL_OWNERSHIP_NONCE" "$attempt_id" "$node" "$token" || return 1
    local resource_key="$template_name-$token"
    kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        "$resource_key" Pod "$name" "$KUBECTL_NAMESPACE" "$uid" \
        "$KUBECTL_OWNERSHIP_NONCE" || {
            kubectl_delete_owned_object Pod "$name" "$KUBECTL_NAMESPACE" \
                "$KUBECTL_OWNERSHIP_NONCE" "$attempt_id" "$uid" || true
            return 1
        }
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
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" resource_file key
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
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
    local results_dir="$1" required_nodes="$2"
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
        "$mapped_read_from" || return 1
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
                "$KUBECTL_OWNERSHIP_NONCE" "$attempt_id" "$manifest"; then
            if ! kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
                    sweep Job "$job_name" "$KUBECTL_NAMESPACE" "$job_uid" \
                    "$KUBECTL_OWNERSHIP_NONCE"; then
                # The Job has an exact UID but no durable journal yet. Delete
                # it immediately; later cleanup must never guess by labels.
                kubectl_delete_owned_object Job "$job_name" "$KUBECTL_NAMESPACE" \
                    "$KUBECTL_OWNERSHIP_NONCE" "$attempt_id" "$job_uid" || true
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
        if _kubectl_create_inspector rollback_helper rollback_uid status "$kubernetes_dir" \
                "$lock_fd" "$attempt_id"; then
            kubectl_release_journaled_remote_attempt "$kubernetes_dir" "$lock_fd" \
                "$KUBECTL_NAMESPACE" "$rollback_helper" "$attempt_id" || true
            _kubectl_remove_inspector "$kubernetes_dir" "$lock_fd" "$rollback_helper" \
                "$rollback_uid" "$attempt_id" || true
        fi
        kubectl_cleanup_journaled_resources "$kubernetes_dir" "$lock_fd" "$attempt_id" || true
        kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$attempt_id" >/dev/null 2>&1 \
            && [[ "$KUBECTL_LIFECYCLE_STATE" == PREPARED ]] \
            && kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" SUBMISSION_FAILED \
            || true
    fi
    kubectl_local_lock_release "$lock_fd" || rc=1
    [[ "$rc" -eq 0 ]] || return "$rc"
    kubectl_emit_submission_identity "$results_dir" "$attempt_id"
    printf 'STORAGE_SCALE_TEST_STATUS_COMMAND=%q --status %q\n' "$0" "$results_dir"
    printf 'STORAGE_SCALE_TEST_COLLECT_COMMAND=%q --collect %q\n' "$0" "$results_dir"
}

kubectl_recover_prepared_attempt() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$attempt_id" || return 1
    [[ "$KUBECTL_LIFECYCLE_STATE" == PREPARED ]] || return 1
    local helper="" helper_uid="" rc=0
    if [[ -f "$kubernetes_dir/attempts/$attempt_id/remote-reservation.sh" ]]; then
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
            collected_terminal >/dev/null || rc=1
        kubectl_local_lock_release "$lock_fd" || rc=1
        [[ "$rc" -eq 0 ]] || return "$rc"
        kubectl_emit_state "$collected_terminal"
        [[ "$collected_terminal" == SUCCESS ]]
        return
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
        # A submitted attempt must still have the exact journaled Job.  A
        # terminal/collected attempt may have deliberately released it; do not
        # let a broad label query silently adopt a replacement.
        if [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED ]]; then
            kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" sweep \
                && kubectl_verify_object_identity "$KUBECTL_RESOURCE_KIND" \
                    "$KUBECTL_RESOURCE_NAME" "$KUBECTL_RESOURCE_NAMESPACE" \
                    "$KUBECTL_RESOURCE_NONCE" "$attempt_id" "$KUBECTL_RESOURCE_UID" >/dev/null \
                || rc=1
        fi
        [[ "$rc" -ne 0 ]] || _kubectl_create_inspector inspector inspector_uid status \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" || rc=1
        [[ "$rc" -ne 0 ]] || remote_state=$(kubectl_read_remote_status "$KUBECTL_NAMESPACE" \
            "$inspector" "$attempt_id") || rc=1
        if [[ "$rc" -eq 0 && "$remote_state" == RUNNING \
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
            elif kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" sweep \
                    && kubectl_job_is_terminal_failure Job "$KUBECTL_RESOURCE_NAME" \
                        "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" \
                        "$attempt_id" "$KUBECTL_RESOURCE_UID"; then
                kubectl_recover_lost_coordinator "$KUBECTL_NAMESPACE" "$inspector" \
                    "$attempt_id" || rc=1
                [[ "$rc" -ne 0 ]] || remote_state=$(kubectl_read_remote_status \
                    "$KUBECTL_NAMESPACE" "$inspector" "$attempt_id") || rc=1
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
    local state_dir="$1" attempt_id="$2" output_name="$3"
    local manifest="$state_dir/publication-manifest.tsv" line kind first second third fourth fifth extra
    [[ -d "$state_dir" && ! -L "$state_dir" && -f "$manifest" && ! -L "$manifest" \
        && "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local terminal="" saw_schema=0 saw_attempt=0
    local -a result_rows=()
    while IFS= read -r line || [[ -n "$line" ]]; do
        IFS=$'\t' read -r kind first second third fourth fifth extra <<< "$line"
        case "$kind" in
            schema) [[ "$first" == 1 && -z "${second:-}" ]] || return 1; saw_schema=1 ;;
            attempt_id) [[ "$first" == "$attempt_id" && -z "${second:-}" ]] || return 1; saw_attempt=1 ;;
            execution) [[ "$first" =~ ^[0-9]{4}$ && "$second" =~ ^(PENDING|RUNNING|SUCCESS|FAILED)$ \
                && -z "${third:-}" ]] || return 1 ;;
            ledger|snapshot|result)
                [[ "$first" =~ ^[A-Za-z0-9._/-]+$ && "$first" != /* \
                    && "$second" =~ ^[A-Za-z0-9._/-]+$ && "$second" != /* \
                    && "$third" =~ ^[0-9]+$ && "$fourth" =~ ^[0-9a-f]{64}$ \
                    && -z "${fifth:-}" ]] || return 1
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
    terminal=$(cat "$state_dir/run.status" 2>/dev/null || true)
    [[ "$terminal" =~ ^(SUCCESS|FAILED|CANCELLED)$ ]] || return 1
    printf -v "$output_name" '%s' "$terminal"
    printf '%s\0' "${result_rows[@]}"
}

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
        [[ -z "${extra:-}" && "$local_path" =~ ^[A-Za-z0-9._/-]+$ \
            && "$local_path" != /* && "$local_path" != *..* ]] || return 1
        local source="$state_dir/$remote" destination="$results_dir/$local_path" parent temporary
        [[ -f "$source" && ! -L "$source" ]] || return 1
        parent=$(dirname "$destination")
        mkdir -p -- "$parent" || return 1
        if [[ -e "$destination" ]]; then
            [[ -f "$destination" && ! -L "$destination" \
                && "$(sha256sum -- "$destination" | awk '{print $1}')" == "$digest" ]] || return 1
            continue
        fi
        temporary="$parent/.${destination##*/}.collect.${BASHPID:-$$}.${RANDOM}"
        if ! cp -- "$source" "$temporary" || ! mv -n -- "$temporary" "$destination"; then
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
        destination_status="$results_dir/executions/$id.status"
        [[ -d "$results_dir/executions" ]] || return 1
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
            [[ -z "${extra:-}" && "$local_path" =~ ^[A-Za-z0-9._/-]+$ \
                && "$local_path" != /* && "$local_path" != *..* \
                && "$digest" =~ ^[0-9a-f]{64}$ ]] || return 1
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
            local destination="$results_dir/$local_path"
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
    if [[ "$KUBECTL_LIFECYCLE_STATE" == COLLECTION_IN_PROGRESS \
            && -d "$metadata_dir/collected-state" ]]; then
        local recovered_terminal=""
        _kubectl_validate_collected_publication "$metadata_dir/collected-state" "$attempt_id" \
            recovered_terminal >/dev/null || return 1
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
    if [[ "$rc" -eq 0 && "$remote_state" == RUNNING ]]; then
        # A coordinator Pod can be killed after publishing RUNNING but before
        # its terminal manifest.  The Job's exact journaled identity is the
        # authority for deciding that no coordinator remains to finish it.
        if kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" sweep \
                && kubectl_job_is_terminal_failure Job "$KUBECTL_RESOURCE_NAME" \
                    "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" \
                    "$attempt_id" "$KUBECTL_RESOURCE_UID"; then
            kubectl_recover_lost_coordinator "$KUBECTL_NAMESPACE" "$inspector" \
                "$attempt_id" || rc=1
            [[ "$rc" -ne 0 ]] || remote_state=$(kubectl_read_remote_status \
                "$KUBECTL_NAMESPACE" "$inspector" "$attempt_id") || rc=1
        fi
    fi
    [[ "$rc" -ne 0 || "$remote_state" =~ ^(SUCCESS|FAILED|CANCELLED)$ ]] || rc=1
    if [[ "$rc" -eq 0 ]]; then
        archive_root=$(mktemp -d "${TMPDIR:-/tmp}/storage-scale-test-kubectl-collect.XXXXXX") \
            || rc=1
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
    [[ "$rc" -ne 0 ]] || _kubectl_validate_collected_publication "$state_dir" "$attempt_id" \
        verified_terminal >/dev/null || rc=1
    [[ "$rc" -ne 0 ]] || [[ "$verified_terminal" == "$remote_state" ]] || rc=1
    [[ "$rc" -ne 0 ]] || _kubectl_merge_collected_results "$state_dir" "$results_dir" || rc=1
    if [[ "$rc" -eq 0 ]]; then
        local collected="$metadata_dir/collected-state"
        [[ ! -e "$collected" ]] || rm -rf -- "$collected"
        mv -- "$state_dir" "$collected" || rc=1
    fi
    [[ -z "${archive_root:-}" ]] || rm -rf -- "$archive_root"
    rm -rf -- "${staging:-}"
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
    kubectl_submit_sweep "$results_dir" "$max_nodes"
}
