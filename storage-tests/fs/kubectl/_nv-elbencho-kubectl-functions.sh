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

# Source-only helpers for the asynchronous Kubernetes sweep lifecycle.
# Preflight and dispatch may load these in the same shell. Preserve constants
# and active ownership maps rather than reinitializing them on a second load.
[[ "${_STORAGE_SCALE_TEST_KUBECTL_HELPERS_LOADED:-}" != 1 ]] || return 0

if [[ "${STORAGE_SCALE_TEST_INTEGRATION:-}" == 1 \
        && -n "${KUBECTL_INTEGRATION_PVC_ROOT:-}" ]]; then
    readonly KUBECTL_SWEEP_MOUNT_ROOT="$KUBECTL_INTEGRATION_PVC_ROOT"
else
    readonly KUBECTL_SWEEP_MOUNT_ROOT=/mnt/storage-scale-test
fi
readonly KUBECTL_SWEEP_RESERVED_ROOT=.storage-scale-test
# Identity schema 4: one segment per namespace, each with the claims its
# Pods mount (one Lease per claim). Attempts written by storage-scale-test
# v1.x (schemas 2 and 3) are not loaded: collect them with a v1.x checkout.
readonly KUBECTL_ATTEMPT_SCHEMA_VERSION=4
readonly KUBECTL_MAX_SEGMENTS=16
readonly KUBECTL_MAX_CLAIMS=32
readonly KUBECTL_SEGMENT_LABEL_KEY=storage-scale-test.nvidia.com/segment
readonly KUBECTL_SEQUENCER_POLL_SECONDS_DEFAULT=10
# The in-cluster sequencer's image unless KUBECTL_SEQUENCER_IMAGE overrides it
# (with any digest-pinned image providing sh, kubectl, grep and sleep). Its
# only API calls, get job -o jsonpath and patch job --type=json, are stable
# across Kubernetes versions. env.sh.template shows this value; a unit test
# keeps the two equal.
readonly KUBECTL_DEFAULT_SEQUENCER_IMAGE=docker.io/alpine/kubectl:1.37.0@sha256:954b65dea0eb140219b440413ede17c2eca205866df350d3c1fbc84d28af14ae
# validate_env.sh sequencer probe: wait bound, and the syntactically valid UID
# of the absent Job triples it hands the sequencer (which must answer NotFound).
readonly KUBECTL_SEQUENCER_PROBE_TIMEOUT_SECONDS_DEFAULT=120
readonly KUBECTL_SEQUENCER_PROBE_ABSENT_UID=00000000-0000-0000-0000-000000000000
# Per-segment and per-claim identity arrays of a loaded attempt.
readonly -a KUBECTL_SEGMENT_IDENTITY_ARRAYS=(
    KUBECTL_SEGMENT_NAMESPACES KUBECTL_SEGMENT_NAMESPACE_UIDS
)
readonly -a KUBECTL_CLAIM_IDENTITY_ARRAYS=(
    KUBECTL_CLAIM_SEGMENTS KUBECTL_CLAIM_NAMESPACES KUBECTL_CLAIM_NAMESPACE_UIDS
    KUBECTL_CLAIM_PVCS KUBECTL_CLAIM_PVC_UIDS KUBECTL_CLAIM_PVS KUBECTL_CLAIM_PV_UIDS
    KUBECTL_CLAIM_LEASE_NAMES
)
readonly KUBECTL_ATTEMPT_RESOURCE_SCHEMA_VERSION=1
readonly KUBECTL_ATTEMPT_CREATION_INTENT_SCHEMA_VERSION=1
readonly KUBECTL_COLLECTION_MAX_BYTES=$((2 * 1024 * 1024 * 1024))
readonly KUBECTL_COLLECTION_MAX_MEMBERS=50000
readonly KUBECTL_COLLECTION_TIMEOUT_SECONDS_DEFAULT=7200
readonly KUBECTL_COLLECTION_HEADROOM_BYTES=$((64 * 1024 * 1024))
readonly KUBECTL_JOB_QUIESCENCE_TIMEOUT_SECONDS_DEFAULT=120
readonly KUBECTL_CREATION_AMBIGUITY_SECONDS_DEFAULT=30
readonly KUBECTL_CREATION_ABSENCE_RECHECK_SECONDS_DEFAULT=2
readonly KUBECTL_PROCESS_TIMEOUT_SECONDS_DEFAULT=30
readonly KUBECTL_CREATE_ATTEMPTS_DEFAULT=3
readonly KUBECTL_OBSERVATION_ATTEMPTS_DEFAULT=3
readonly KUBECTL_OBSERVATION_BACKOFF_SECONDS_DEFAULT=1
readonly KUBECTL_DIAGNOSTIC_SCHEMA_VERSION=1
readonly KUBECTL_DIAGNOSTIC_MAX_BUNDLES=8
readonly KUBECTL_DIAGNOSTIC_MAX_BYTES=$((4 * 1024 * 1024))
readonly KUBECTL_DIAGNOSTIC_MAX_PODS=4
readonly KUBECTL_COLLECTION_ERROR_MAX_BYTES=$((128 * 1024))
readonly KUBECTL_COLLECTION_STREAM_ATTEMPTS=3
declare -gA KUBECTL_LOCAL_LOCK_ROOTS=()

# Claim-qualified roots. On Kubernetes every TEST_DIRS key names the claim it
# lives on: "[NAMESPACE/]CLAIM:PATH". The key splits at its first ":" (claim
# names cannot contain ":" or "/"); NAMESPACE defaults to KUBECTL_NAMESPACE;
# PATH is relative to the claim. Every claim is mounted at
# KUBECTL_SWEEP_MOUNT_ROOT/<claim>, so "CLAIM:PATH" maps to
# KUBECTL_SWEEP_MOUNT_ROOT/<claim>/<path>. The normalized form of a key,
# "NAMESPACE/CLAIM:PATH", is its logical path everywhere below.

# _kubectl_normalize_claim_path VALUE OUT: the claim-relative PATH of a key,
# without leading or repeated slashes. Components must be plain names; ".",
# "..", the reserved control directory and the claim root itself are refused.
_kubectl_normalize_claim_path() {
    local value="$1" output_variable="$2"
    if [[ -z "$value" || "$value" == *$'\n'* || "$value" == *$'\r'* \
            || "$value" == *$'\t'* ]]; then
        echo "Error: Kubernetes logical paths must be nonempty single-line values" >&2
        return 1
    fi
    while [[ "$value" == /* ]]; do
        value="${value#/}"
    done
    local -a components=() normalized=()
    local component
    IFS=/ read -ra components <<< "$value"
    for component in "${components[@]}"; do
        [[ -z "$component" ]] && continue
        if [[ "$component" == . || "$component" == .. ]]; then
            echo "Error: Kubernetes logical paths may not contain '.' or '..'" >&2
            return 1
        fi
        if [[ "$component" == "$KUBECTL_SWEEP_RESERVED_ROOT" ]]; then
            echo "Error: Kubernetes logical paths may not contain the reserved component $KUBECTL_SWEEP_RESERVED_ROOT" >&2
            return 1
        fi
        if [[ ! "$component" =~ ^[A-Za-z0-9._-]+$ ]]; then
            echo "Error: Kubernetes logical path components may contain only letters, digits, '.', '_', and '-'" >&2
            return 1
        fi
        normalized+=("$component")
    done
    if [[ ${#normalized[@]} -eq 0 ]]; then
        echo "Error: Kubernetes logical paths may not resolve to the claim root" >&2
        return 1
    fi
    local joined
    joined=$(IFS=/; printf '%s' "${normalized[*]}")
    printf -v "$output_variable" '%s' "$joined"
}

# kubectl_split_test_dir_key KEY NAMESPACE_OUT CLAIM_OUT PATH_OUT: validate
# one claim-qualified key and set its namespace (KUBECTL_NAMESPACE when the
# key names none), claim and normalized claim-relative path.
kubectl_split_test_dir_key() {
    # Locals carry a prefix so they never shadow the caller's output names.
    local _kstk_key="$1" _kstk_qualifier _kstk_rest _kstk_namespace _kstk_claim
    local _kstk_path _kstk_name
    for _kstk_name in "$2" "$3" "$4"; do
        [[ "$_kstk_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || {
            echo "Error: invalid output variable name" >&2
            return 1
        }
    done
    if [[ "$_kstk_key" != *:* ]]; then
        printf 'Error: Kubernetes TEST_DIRS key %q must name its claim as [namespace/]claim:path\n' \
            "$_kstk_key" >&2
        return 1
    fi
    _kstk_qualifier="${_kstk_key%%:*}"
    _kstk_rest="${_kstk_key#*:}"
    if [[ "$_kstk_qualifier" == */* ]]; then
        _kstk_namespace="${_kstk_qualifier%%/*}"
        _kstk_claim="${_kstk_qualifier#*/}"
    else
        _kstk_namespace="${KUBECTL_NAMESPACE:-}"
        _kstk_claim="$_kstk_qualifier"
        if [[ -z "$_kstk_namespace" ]]; then
            printf 'Error: Kubernetes TEST_DIRS key %q names no namespace and KUBECTL_NAMESPACE is empty; write it as namespace/%s\n' \
                "$_kstk_key" "$_kstk_key" >&2
            return 1
        fi
    fi
    if ! kubectl_validate_namespace_name "$_kstk_namespace"; then
        printf 'Error: Kubernetes TEST_DIRS key %q: namespace %q must be a lowercase DNS-1123 label (at most 63 characters)\n' \
            "$_kstk_key" "$_kstk_namespace" >&2
        return 1
    fi
    if [[ "$_kstk_claim" == */* ]] || ! kubectl_validate_object_name "$_kstk_claim"; then
        printf 'Error: Kubernetes TEST_DIRS key %q: claim %q must be a lowercase DNS-1123 PersistentVolumeClaim name\n' \
            "$_kstk_key" "$_kstk_claim" >&2
        return 1
    fi
    _kubectl_normalize_claim_path "$_kstk_rest" _kstk_path || {
        printf 'Error: Kubernetes TEST_DIRS key %q has an invalid path after its claim\n' \
            "$_kstk_key" >&2
        return 1
    }
    printf -v "$2" '%s' "$_kstk_namespace"
    printf -v "$3" '%s' "$_kstk_claim"
    printf -v "$4" '%s' "$_kstk_path"
}

# kubectl_normalize_logical_path KEY OUT: set OUT to KEY's normalized logical
# path "NAMESPACE/CLAIM:PATH". A normalized path normalizes to itself.
kubectl_normalize_logical_path() {
    local _knlp_output="$2" _knlp_namespace _knlp_claim _knlp_path
    if [[ ! "$_knlp_output" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "Error: invalid output variable name" >&2
        return 1
    fi
    kubectl_split_test_dir_key "$1" _knlp_namespace _knlp_claim _knlp_path || return 1
    printf -v "$_knlp_output" '%s/%s:%s' "$_knlp_namespace" "$_knlp_claim" "$_knlp_path"
}

# _kubectl_logical_parts NORMALIZED NAMESPACE_OUT CLAIM_OUT PATH_OUT: split a
# path that kubectl_normalize_logical_path produced (no validation).
_kubectl_logical_parts() {
    local _klp_value="$1" _klp_qualifier
    _klp_qualifier="${_klp_value%%:*}"
    printf -v "$2" '%s' "${_klp_qualifier%%/*}"
    printf -v "$3" '%s' "${_klp_qualifier#*/}"
    printf -v "$4" '%s' "${_klp_value#*:}"
}

# The Pod mount root of CLAIM.
kubectl_claim_mount_root() {
    kubectl_validate_object_name "$1" || return 1
    printf '%s/%s\n' "$KUBECTL_SWEEP_MOUNT_ROOT" "$1"
}

# kubectl_mapped_mount_root MAPPED OUT: set OUT to the claim mount root that
# contains the Pod path MAPPED (KUBECTL_SWEEP_MOUNT_ROOT/<claim>/...).
kubectl_mapped_mount_root() {
    local mapped="$1" rest claim
    [[ "$mapped" == "$KUBECTL_SWEEP_MOUNT_ROOT/"*/* ]] || return 1
    rest="${mapped#"$KUBECTL_SWEEP_MOUNT_ROOT/"}"
    claim="${rest%%/*}"
    kubectl_validate_object_name "$claim" || return 1
    printf -v "$2" '%s/%s' "$KUBECTL_SWEEP_MOUNT_ROOT" "$claim"
}

kubectl_map_logical_path() {
    local namespace claim path
    kubectl_split_test_dir_key "$1" namespace claim path || return 1
    printf '%s/%s/%s\n' "$KUBECTL_SWEEP_MOUNT_ROOT" "$claim" "$path"
}

# kubectl_map_test_dirs OUT: OUT (an associative array) maps the Pod path of
# every TEST_DIRS root to its weight. Every root must share one namespace,
# because one Pod mounts claims of its own namespace only.
kubectl_map_test_dirs() {
    local output_name="$1"
    if [[ ! "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
        echo "Error: invalid TEST_DIRS output name" >&2
        return 1
    fi
    local -n output_ref="$output_name"
    output_ref=()
    local source_path mapped_path namespace claim path first_namespace=""
    for source_path in "${!TEST_DIRS[@]}"; do
        kubectl_split_test_dir_key "$source_path" namespace claim path || return 1
        if [[ -n "$first_namespace" && "$namespace" != "$first_namespace" ]]; then
            echo "Error: TEST_DIRS roots span Kubernetes namespaces $first_namespace and $namespace" >&2
            return 1
        fi
        first_namespace="$namespace"
        mapped_path="$KUBECTL_SWEEP_MOUNT_ROOT/$claim/$path"
        if [[ -v output_ref["$mapped_path"] ]]; then
            echo "Error: multiple TEST_DIRS keys map to $mapped_path" >&2
            return 1
        fi
        output_ref["$mapped_path"]="${TEST_DIRS[$source_path]}"
    done
    return 0
}

# kubectl_select_control_root LOGICAL_OUT MAPPED_OUT: the lexically first
# normalized TEST_DIRS root, and its Pod path. The attempt's run tree lives
# below it, on that root's claim.
kubectl_select_control_root() {
    local logical_output="$1" mapped_output="$2"
    [[ "$logical_output" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
        && "$mapped_output" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    local root normalized_path
    local -a roots=()
    for root in "${!TEST_DIRS[@]}"; do
        kubectl_normalize_logical_path "$root" normalized_path || return 1
        roots+=("$normalized_path")
    done
    [[ ${#roots[@]} -gt 0 ]] || return 1
    mapfile -t roots < <(printf '%s\n' "${roots[@]}" | LC_ALL=C sort -u)
    [[ ${#roots[@]} -eq ${#TEST_DIRS[@]} ]] || {
        echo "Error: multiple TEST_DIRS keys resolve to the same Kubernetes path" >&2
        return 1
    }
    local mapped
    mapped=$(kubectl_map_logical_path "${roots[0]}") || return 1
    printf -v "$logical_output" '%s' "${roots[0]}"
    printf -v "$mapped_output" '%s' "$mapped"
}

kubectl_set_control_layout() {
    local logical="$1" mapped="$2" expected
    kubectl_normalize_logical_path "$logical" logical || return 1
    expected=$(kubectl_map_logical_path "$logical") || return 1
    [[ "$mapped" == "$expected" ]] || return 1
    KUBECTL_CONTROL_LOGICAL_ROOT="$logical"
    KUBECTL_CONTROL_TEST_ROOT="$mapped"
    KUBECTL_CONTROL_ROOT="$mapped/$KUBECTL_SWEEP_RESERVED_ROOT"
    export KUBECTL_CONTROL_LOGICAL_ROOT KUBECTL_CONTROL_TEST_ROOT KUBECTL_CONTROL_ROOT
}

# kubectl_map_read_from_path PATH: the Pod path of a single-root mode's
# claim-qualified PATH ("[NAMESPACE/]CLAIM:PATH"), which must lie on exactly
# one TEST_DIRS root (same namespace and claim, and that root's path or below
# it) other than the control root itself.
kubectl_map_read_from_path() {
    local read_path read_claim read_relative
    # shellcheck disable=SC2034  # The namespace part is not needed here.
    local read_namespace
    kubectl_normalize_logical_path "$1" read_path || return 1
    _kubectl_logical_parts "$read_path" read_namespace read_claim read_relative
    local matches=0 root normalized_root
    for root in "${!TEST_DIRS[@]}"; do
        kubectl_normalize_logical_path "$root" normalized_root || return 1
        [[ "$read_path" == "$normalized_root" \
            || "$read_path" == "$normalized_root/"* ]] || continue
        matches=$((matches + 1))
    done
    if [[ "$matches" -ne 1 ]]; then
        echo "Error: read-from path must belong to exactly one logical TEST_DIRS root" >&2
        return 1
    fi
    if [[ -n "${KUBECTL_CONTROL_LOGICAL_ROOT:-}" \
            && "$read_path" == "$KUBECTL_CONTROL_LOGICAL_ROOT" ]]; then
        echo "Error: read-from may not scan the Kubernetes control-root TEST_DIRS entry" >&2
        return 1
    fi
    printf '%s/%s/%s\n' "$KUBECTL_SWEEP_MOUNT_ROOT" "$read_claim" "$read_relative"
}

# kubectl_read_from_logical_path VALUE OUT: set OUT to VALUE, or, when VALUE is
# a Pod path KUBECTL_SWEEP_MOUNT_ROOT/CLAIM[/PATH] (as --write-only prints it),
# to NAMESPACE/CLAIM:PATH, where NAMESPACE is that of the TEST_DIRS roots on
# CLAIM. A claim no root (or roots in several namespaces) uses is an error.
kubectl_read_from_logical_path() {
    local value="$1" output="$2" rest claim key namespace key_claim path
    local -A namespaces=()
    [[ "$output" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    printf -v "$output" '%s' "$value"
    [[ "$value" == "$KUBECTL_SWEEP_MOUNT_ROOT/"* ]] || return 0
    rest="${value#"$KUBECTL_SWEEP_MOUNT_ROOT/"}"
    claim="${rest%%/*}"
    for key in "${!TEST_DIRS[@]}"; do
        kubectl_split_test_dir_key "$key" namespace key_claim path || return 1
        [[ "$key_claim" != "$claim" ]] || namespaces["$namespace"]=1
    done
    if [[ ${#namespaces[@]} -ne 1 ]]; then
        printf 'Error: --read-from %s is not on a claim of this submission'\''s TEST_DIRS; pass it as [namespace/]claim:path\n' \
            "$value" >&2
        return 1
    fi
    path=""
    [[ "$rest" != */* ]] || path="${rest#*/}"
    printf -v "$output" '%s/%s:%s' "${!namespaces[@]}" "$claim" "$path"
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
    if ! command -v flock >/dev/null 2>&1; then
        echo "Error: flock is required for Kubernetes lifecycle locking (on macOS: brew install flock)" >&2
        return 1
    fi
    _kubectl_validate_local_directory_path "$kubernetes_dir" || return 1
    mkdir -p "$kubernetes_dir" || return 1
    [[ ! -L "$kubernetes_dir" && ! -L "$kubernetes_dir/lifecycle.lock" ]] || return 1
    local acquired_fd
    exec {acquired_fd}>"$kubernetes_dir/lifecycle.lock" || return 1
    if ! flock -n "$acquired_fd"; then
        eval "exec ${acquired_fd}>&-"
        printf 'Error: another Kubernetes lifecycle operation is active for %s\n' \
            "$kubernetes_dir" >&2
        echo "Wait for that operation to finish, then retry this command." >&2
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

_kubectl_local_tree_apparent_bytes() {
    local directory="$1"
    [[ -d "$directory" && ! -L "$directory" ]] || return 1
    (set -o pipefail
        find -P "$directory" -type f -exec sh -c '
            for path do
                wc -c < "$path" || exit 1
            done
        ' sh {} + | awk '{ total += $1 } END { print total + 0 }')
}

_kubectl_local_file_bytes() {
    local path="$1" output_name="$2" _kubectl_file_bytes_raw
    [[ -f "$path" && ! -L "$path" \
        && "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    _kubectl_file_bytes_raw=$(wc -c < "$path") || return 1
    # BSD wc right-aligns counts with leading spaces. Command substitution
    # removes the trailing newline; remove any remaining display padding.
    _kubectl_file_bytes_raw=${_kubectl_file_bytes_raw//[[:space:]]/}
    [[ "$_kubectl_file_bytes_raw" =~ ^[0-9]+$ ]] || return 1
    printf -v "$output_name" '%s' "$_kubectl_file_bytes_raw"
}

_kubectl_local_path_mtime() {
    local path="$1" mtime=""
    [[ -e "$path" && ! -L "$path" ]] || return 1
    mtime=$(stat -c %Y -- "$path" 2>/dev/null) \
        || mtime=$(stat -f %m -- "$path" 2>/dev/null) \
        || return 1
    [[ "$mtime" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$mtime"
}

_kubectl_local_path_link_count() {
    local path="$1" links=""
    [[ -f "$path" && ! -L "$path" ]] || return 1
    links=$(stat -c %h -- "$path" 2>/dev/null) \
        || links=$(stat -f %l -- "$path" 2>/dev/null) \
        || links=$(gstat -c %h -- "$path" 2>/dev/null) \
        || return 1
    [[ "$links" =~ ^[1-9][0-9]*$ ]] || return 1
    printf '%s\n' "$links"
}

_kubectl_local_realpath_existing() {
    local path="$1" resolved=""
    [[ -e "$path" ]] || return 1
    resolved=$(realpath -e -- "$path" 2>/dev/null) \
        || resolved=$(grealpath -e -- "$path" 2>/dev/null) \
        || resolved=$(realpath "$path" 2>/dev/null) \
        || return 1
    [[ "$resolved" == /* ]] || return 1
    printf '%s\n' "$resolved"
}

_kubectl_sha256_file() {
    local path="$1" digest=""
    [[ -f "$path" && ! -L "$path" ]] || return 1
    if command -v sha256sum >/dev/null 2>&1; then
        digest=$(sha256sum -- "$path" | awk '{print $1}') || return 1
    elif command -v gsha256sum >/dev/null 2>&1; then
        digest=$(gsha256sum -- "$path" | awk '{print $1}') || return 1
    elif command -v shasum >/dev/null 2>&1; then
        digest=$(shasum -a 256 -- "$path" | awk '{print $1}') || return 1
    else
        echo "Error: SHA-256 requires sha256sum, gsha256sum, or shasum" >&2
        return 1
    fi
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || return 1
    printf '%s\n' "$digest"
}

_kubectl_sha256_text() {
    local value="$1" digest=""
    if command -v sha256sum >/dev/null 2>&1; then
        digest=$(printf '%s' "$value" | sha256sum | awk '{print $1}') || return 1
    elif command -v gsha256sum >/dev/null 2>&1; then
        digest=$(printf '%s' "$value" | gsha256sum | awk '{print $1}') || return 1
    elif command -v shasum >/dev/null 2>&1; then
        digest=$(printf '%s' "$value" | shasum -a 256 | awk '{print $1}') || return 1
    else
        echo "Error: SHA-256 requires sha256sum, gsha256sum, or shasum" >&2
        return 1
    fi
    [[ "$digest" =~ ^[0-9a-f]{64}$ ]] || return 1
    printf '%s\n' "$digest"
}

kubectl_pvc_lease_name() {
    local namespace_uid="$1" pv_uid="$2" pvc_uid="$3" digest
    kubectl_validate_uid "$namespace_uid" && kubectl_validate_uid "$pv_uid" \
        && kubectl_validate_uid "$pvc_uid" || return 1
    digest=$(_kubectl_sha256_text \
        "lease-v1|${#namespace_uid}:$namespace_uid|${#pv_uid}:$pv_uid|${#pvc_uid}:$pvc_uid") \
        || return 1
    printf 'sst-elb-pvc-%s\n' "${digest:0:32}"
}

_kubectl_write_bundle_manifest() {
    local bundle="$1" manifest="$1/bundle-manifest.tsv"
    [[ -d "$bundle" && ! -L "$bundle" ]] || return 1
    local temporary="$manifest.tmp.${BASHPID:-$$}.$RANDOM"
    local path relative digest
    : > "$temporary" || return 1
    for path in "$bundle"/* "$bundle"/executions/* "$bundle"/groups/*/*/env_used.*; do
        [[ -f "$path" && ! -L "$path" && "$path" != "$manifest" \
            && "$path" != "$temporary" ]] || continue
        relative=${path#"$bundle/"}
        digest=$(_kubectl_sha256_file "$path") || {
            rm -f -- "$temporary"
            return 1
        }
        printf '%s\t%s\n' "$digest" "$relative" >> "$temporary" || {
            rm -f -- "$temporary"
            return 1
        }
    done
    if [[ ! -s "$temporary" ]] || ! mv -f -- "$temporary" "$manifest"; then
        rm -f -- "$temporary"
        return 1
    fi
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

_kubectl_validate_segment_index() {
    [[ "$1" =~ ^(0|[1-9][0-9]?)$ ]] && (( $1 < KUBECTL_MAX_SEGMENTS ))
}

_kubectl_validate_claim_index() {
    [[ "$1" =~ ^(0|[1-9][0-9]?)$ ]] && (( $1 < KUBECTL_MAX_CLAIMS ))
}

_kubectl_validate_segment_count() {
    [[ "$1" =~ ^[1-9][0-9]?$ ]] && (( $1 <= KUBECTL_MAX_SEGMENTS ))
}

_kubectl_validate_claim_count() {
    [[ "$1" =~ ^[1-9][0-9]?$ ]] && (( $1 <= KUBECTL_MAX_CLAIMS ))
}

# The named variable is an indexed array with exactly the indices 0..COUNT-1.
_kubectl_array_is_dense() {
    local array_name="$1" expected="$2" position=0 key
    [[ "$(declare -p "$array_name" 2>/dev/null)" =~ ^declare\ -[A-Za-z]*a[A-Za-z]*\  ]] \
        || return 1
    local -n _kubectl_dense_array="$array_name"
    for key in "${!_kubectl_dense_array[@]}"; do
        [[ "$key" == "$position" ]] || return 1
        position=$((position + 1))
    done
    [[ "$position" -eq "$expected" ]]
}

# One claim's fields: valid names and UIDs, the namespace (and its UID) of its
# segment, and the deterministic Lease name of its storage identity.
_kubectl_validate_claim_identity_entry() {
    local index="$1" segment lease
    segment="${KUBECTL_CLAIM_SEGMENTS[$index]}"
    _kubectl_validate_segment_index "$segment" \
        && (( segment < KUBECTL_SEGMENT_COUNT )) \
        && [[ "${KUBECTL_CLAIM_NAMESPACES[$index]}" == "${KUBECTL_SEGMENT_NAMESPACES[$segment]}" \
            && "${KUBECTL_CLAIM_NAMESPACE_UIDS[$index]}" == "${KUBECTL_SEGMENT_NAMESPACE_UIDS[$segment]}" ]] \
        && kubectl_validate_object_name "${KUBECTL_CLAIM_PVCS[$index]}" \
        && kubectl_validate_uid "${KUBECTL_CLAIM_PVC_UIDS[$index]}" \
        && kubectl_validate_object_name "${KUBECTL_CLAIM_PVS[$index]}" \
        && kubectl_validate_uid "${KUBECTL_CLAIM_PV_UIDS[$index]}" || return 1
    lease=$(kubectl_pvc_lease_name "${KUBECTL_CLAIM_NAMESPACE_UIDS[$index]}" \
        "${KUBECTL_CLAIM_PV_UIDS[$index]}" "${KUBECTL_CLAIM_PVC_UIDS[$index]}") \
        || return 1
    [[ "${KUBECTL_CLAIM_LEASE_NAMES[$index]}" == "$lease" ]]
}

# Validate the segment and claim identity visible in the caller's scope (the
# loader's globals, or the creator's same-named locals). Segments are distinct
# namespaces with distinct UIDs. Claims are sorted by namespace, then name
# (their Lease acquisition order); each belongs to one segment of its own
# namespace, is bound to a PV no other claim uses, and every segment has at
# least one claim. Claim order is byte order, as LC_ALL=C sort produced it,
# whatever the caller's collation locale.
_kubectl_validate_attempt_identity_arrays() {
    local LC_ALL=C
    local segments="${KUBECTL_SEGMENT_COUNT:-}" claims="${KUBECTL_CLAIM_COUNT:-}"
    local array_name index previous="" current
    _kubectl_validate_segment_count "$segments" \
        && _kubectl_validate_claim_count "$claims" && (( claims >= segments )) \
        || return 1
    for array_name in "${KUBECTL_SEGMENT_IDENTITY_ARRAYS[@]}"; do
        _kubectl_array_is_dense "$array_name" "$segments" || return 1
    done
    for array_name in "${KUBECTL_CLAIM_IDENTITY_ARRAYS[@]}"; do
        _kubectl_array_is_dense "$array_name" "$claims" || return 1
    done
    local -A namespaces=() namespace_uids=() pvs=() pv_uids=() pvc_uids=() used=()
    for ((index = 0; index < segments; index++)); do
        kubectl_validate_namespace_name "${KUBECTL_SEGMENT_NAMESPACES[$index]}" \
            && kubectl_validate_uid "${KUBECTL_SEGMENT_NAMESPACE_UIDS[$index]}" \
            && [[ ! -v namespaces["${KUBECTL_SEGMENT_NAMESPACES[$index]}"] \
                && ! -v namespace_uids["${KUBECTL_SEGMENT_NAMESPACE_UIDS[$index]}"] ]] \
            || return 1
        namespaces["${KUBECTL_SEGMENT_NAMESPACES[$index]}"]=1
        namespace_uids["${KUBECTL_SEGMENT_NAMESPACE_UIDS[$index]}"]=1
    done
    for ((index = 0; index < claims; index++)); do
        _kubectl_validate_claim_identity_entry "$index" || return 1
        current="${KUBECTL_CLAIM_NAMESPACES[$index]}"$'\t'"${KUBECTL_CLAIM_PVCS[$index]}"
        [[ -z "$previous" || "$previous" < "$current" ]] || return 1
        previous="$current"
        [[ ! -v pvs["${KUBECTL_CLAIM_PVS[$index]}"] \
            && ! -v pv_uids["${KUBECTL_CLAIM_PV_UIDS[$index]}"] \
            && ! -v pvc_uids["${KUBECTL_CLAIM_PVC_UIDS[$index]}"] ]] || return 1
        pvs["${KUBECTL_CLAIM_PVS[$index]}"]=1
        pv_uids["${KUBECTL_CLAIM_PV_UIDS[$index]}"]=1
        pvc_uids["${KUBECTL_CLAIM_PVC_UIDS[$index]}"]=1
        used["${KUBECTL_CLAIM_SEGMENTS[$index]}"]=1
    done
    [[ ${#used[@]} -eq "$segments" ]]
}

# Fill the caller's KUBECTL_SEGMENT_* and KUBECTL_CLAIM_* arrays from
# consecutive six-field claim tuples
#   NAMESPACE NAMESPACE_UID PV PV_UID PVC PVC_UID
# Segments are the tuples' namespaces in order of first appearance; claims
# are sorted by namespace, then name, and get their deterministic Lease names.
_kubectl_collect_claim_tuples() {
    (( $# > 0 && $# % 6 == 0 && $# / 6 <= KUBECTL_MAX_CLAIMS )) || return 1
    local -A segment_of=()
    local -a rows=()
    local namespace row pvc ns_uid pv pv_uid pvc_uid lease
    while (( $# > 0 )); do
        namespace="$1"
        [[ "$namespace" != *$'\t'* && "$3$5" != *$'\t'* ]] || return 1
        if [[ ! -v segment_of["$namespace"] ]]; then
            segment_of["$namespace"]=${#KUBECTL_SEGMENT_NAMESPACES[@]}
            KUBECTL_SEGMENT_NAMESPACES+=("$namespace")
            KUBECTL_SEGMENT_NAMESPACE_UIDS+=("$2")
        fi
        rows+=("$namespace"$'\t'"$5"$'\t'"$2"$'\t'"$3"$'\t'"$4"$'\t'"$6")
        shift 6
    done
    while IFS=$'\t' read -r namespace pvc ns_uid pv pv_uid pvc_uid; do
        lease=$(kubectl_pvc_lease_name "$ns_uid" "$pv_uid" "$pvc_uid") || return 1
        KUBECTL_CLAIM_SEGMENTS+=("${segment_of[$namespace]}")
        KUBECTL_CLAIM_NAMESPACES+=("$namespace")
        KUBECTL_CLAIM_NAMESPACE_UIDS+=("$ns_uid")
        KUBECTL_CLAIM_PVCS+=("$pvc")
        KUBECTL_CLAIM_PVC_UIDS+=("$pvc_uid")
        KUBECTL_CLAIM_PVS+=("$pv")
        KUBECTL_CLAIM_PV_UIDS+=("$pv_uid")
        KUBECTL_CLAIM_LEASE_NAMES+=("$lease")
    done < <(printf '%s\n' "${rows[@]}" | LC_ALL=C sort -t $'\t' -k1,1 -k2,2)
    KUBECTL_SEGMENT_COUNT=${#KUBECTL_SEGMENT_NAMESPACES[@]}
    KUBECTL_CLAIM_COUNT=${#KUBECTL_CLAIM_PVCS[@]}
}

_kubectl_render_identity_shell() {
    local attempt_id="$1" ownership_nonce="$2" array_name reference value
    printf '# Trusted storage-scale-test Kubernetes attempt metadata.\n'
    printf 'KUBECTL_ATTEMPT_SCHEMA=%q\n' "$KUBECTL_ATTEMPT_SCHEMA_VERSION"
    printf 'KUBECTL_ATTEMPT_ID=%q\n' "$attempt_id"
    printf 'KUBECTL_OWNERSHIP_NONCE=%q\n' "$ownership_nonce"
    printf 'KUBECTL_SEGMENT_COUNT=%q\n' "$KUBECTL_SEGMENT_COUNT"
    printf 'KUBECTL_CLAIM_COUNT=%q\n' "$KUBECTL_CLAIM_COUNT"
    for array_name in "${KUBECTL_SEGMENT_IDENTITY_ARRAYS[@]}" \
            "${KUBECTL_CLAIM_IDENTITY_ARRAYS[@]}"; do
        reference="${array_name}[@]"
        printf 'declare -ga %s=(' "$array_name"
        for value in "${!reference}"; do
            printf ' %q' "$value"
        done
        printf ' )\n'
    done
}

_kubectl_render_identity_yaml() {
    local attempt_id="$1" ownership_nonce="$2" segment claim
    printf 'schema: %s\n' "$KUBECTL_ATTEMPT_SCHEMA_VERSION"
    printf 'attempt_id: "%s"\n' "$attempt_id"
    printf 'ownership_nonce: "%s"\n' "$ownership_nonce"
    printf 'segments:\n'
    for ((segment = 0; segment < KUBECTL_SEGMENT_COUNT; segment++)); do
        printf '  - index: %s\n' "$segment"
        printf '    namespace: "%s"\n' "${KUBECTL_SEGMENT_NAMESPACES[$segment]}"
        printf '    namespace_uid: "%s"\n' "${KUBECTL_SEGMENT_NAMESPACE_UIDS[$segment]}"
        printf '    claims:\n'
        for ((claim = 0; claim < KUBECTL_CLAIM_COUNT; claim++)); do
            [[ "${KUBECTL_CLAIM_SEGMENTS[$claim]}" == "$segment" ]] || continue
            printf '      - index: %s\n' "$claim"
            printf '        namespace: "%s"\n' "${KUBECTL_CLAIM_NAMESPACES[$claim]}"
            printf '        claim: "%s"\n' "${KUBECTL_CLAIM_PVCS[$claim]}"
            printf '        pv: "%s"\n' "${KUBECTL_CLAIM_PVS[$claim]}"
            printf '        namespace_uid: "%s"\n' "${KUBECTL_CLAIM_NAMESPACE_UIDS[$claim]}"
            printf '        pv_uid: "%s"\n' "${KUBECTL_CLAIM_PV_UIDS[$claim]}"
            printf '        pvc_uid: "%s"\n' "${KUBECTL_CLAIM_PVC_UIDS[$claim]}"
            printf '        lease_name: "%s"\n' "${KUBECTL_CLAIM_LEASE_NAMES[$claim]}"
        done
    done
}

# Publish the immutable identity of a new attempt:
#   kubectl_attempt_create_identity DIR LOCK_FD ATTEMPT_ID NONCE CLAIM...
# Each CLAIM is six consecutive fields:
#   NAMESPACE NAMESPACE_UID PV PV_UID PVC PVC_UID
# The attempt's segments are the claims' namespaces in order of first
# appearance; see _kubectl_collect_claim_tuples. Job UIDs are journaled later
# as resources.
kubectl_attempt_create_identity() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" ownership_nonce="$4"
    shift 4
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    [[ "$ownership_nonce" =~ ^[0-9a-f]{32}$ ]] || return 1
    # Shadow the loader's globals: the shared validator and renderers then
    # describe this new identity without disturbing an already loaded attempt.
    local KUBECTL_SEGMENT_COUNT=0 KUBECTL_CLAIM_COUNT=0
    local -a KUBECTL_SEGMENT_NAMESPACES=() KUBECTL_SEGMENT_NAMESPACE_UIDS=()
    local -a KUBECTL_CLAIM_SEGMENTS=() KUBECTL_CLAIM_NAMESPACES=()
    local -a KUBECTL_CLAIM_NAMESPACE_UIDS=() KUBECTL_CLAIM_PVCS=()
    local -a KUBECTL_CLAIM_PVC_UIDS=() KUBECTL_CLAIM_PVS=() KUBECTL_CLAIM_PV_UIDS=()
    local -a KUBECTL_CLAIM_LEASE_NAMES=()
    if ! _kubectl_collect_claim_tuples "$@" \
            || ! _kubectl_validate_attempt_identity_arrays; then
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
    if ! _kubectl_render_identity_shell "$attempt_id" "$ownership_nonce" \
                > "$staging_dir/identity.sh" \
            || ! _kubectl_render_identity_yaml "$attempt_id" "$ownership_nonce" \
                > "$staging_dir/identity.yaml"; then
        rm -rf -- "$staging_dir"
        return 1
    fi
    # The lifecycle lock serializes publishers for this results directory.
    # Use the create-only option shared by GNU and BSD mv; macOS mv does not
    # implement GNU's -T option.
    mv -n "$staging_dir" "$metadata_dir" 2>/dev/null || true
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
        && mv -f "$tmp" "$metadata_dir/state.sh" || return 1
    # Keep the in-memory view equal to the durable state just published, so
    # diagnostics after a transition never report the superseded state.
    KUBECTL_LIFECYCLE_STATE="$state"
}

# An identity written by storage-scale-test v1.x (schema 2 or 3) belongs to an
# attempt that only v1.x can still inspect, cancel or collect. The loaders
# return 2 for it so lifecycle callers can skip their generic diagnostics.
_kubectl_reject_v1_identity() {
    local identity_file="$1" schema
    schema=$(sed -n 's/^KUBECTL_ATTEMPT_SCHEMA=\([0-9]*\)$/\1/p' "$identity_file" \
        2>/dev/null) || schema=""
    [[ "$schema" == 2 || "$schema" == 3 ]] || return 1
    printf 'Error: Kubernetes attempt %s was started by storage-scale-test v1.x (attempt identity schema %s), which this version does not inspect, cancel, collect or resume. Collect this attempt with a separate storage-scale-test v1.x checkout (for example v1.8.0).\n' \
        "${identity_file%/identity.sh}" "$schema" >&2
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
    if _kubectl_reject_v1_identity "$identity_file"; then
        return 2
    fi
    unset KUBECTL_ATTEMPT_SCHEMA KUBECTL_ATTEMPT_ID KUBECTL_OWNERSHIP_NONCE
    unset KUBECTL_NAMESPACE_UID KUBECTL_PV KUBECTL_PV_UID
    unset KUBECTL_PVC KUBECTL_PVC_UID KUBECTL_PVC_LEASE_NAME
    unset KUBECTL_SEGMENT_COUNT KUBECTL_CLAIM_COUNT
    unset "${KUBECTL_SEGMENT_IDENTITY_ARRAYS[@]}" "${KUBECTL_CLAIM_IDENTITY_ARRAYS[@]}"
    # shellcheck disable=SC1090  # Trusted, result-directory-local identity.
    source "$identity_file" || return 1
    [[ "${KUBECTL_ATTEMPT_SCHEMA:-}" == "$KUBECTL_ATTEMPT_SCHEMA_VERSION" \
        && "${KUBECTL_ATTEMPT_ID:-}" == "$basename" \
        && "${KUBECTL_OWNERSHIP_NONCE:-}" =~ ^[0-9a-f]{32}$ ]] || return 1
    _kubectl_verify_loaded_identity "$identity_file" "$yaml_file" || return 1
    # Reloading keeps a selection made with kubectl_with_segment (and
    # kubectl_with_claim); otherwise the scalar globals describe segment 0.
    kubectl_use_segment "${KUBECTL_SEGMENT_SELECTION:-0}" || return 1
    [[ -z "${KUBECTL_CLAIM_SELECTION:-}" ]] \
        || kubectl_use_claim "$KUBECTL_CLAIM_SELECTION"
}

# An identity must be exactly what the writer renders for its arrays.
_kubectl_verify_loaded_identity() {
    local identity_file="$1" yaml_file="$2" expected
    _kubectl_validate_attempt_identity_arrays || return 1
    expected=$(_kubectl_render_identity_shell "$KUBECTL_ATTEMPT_ID" \
        "$KUBECTL_OWNERSHIP_NONCE") || return 1
    [[ "$(<"$identity_file")" == "$expected" ]] || return 1
    expected=$(_kubectl_render_identity_yaml "$KUBECTL_ATTEMPT_ID" \
        "$KUBECTL_OWNERSHIP_NONCE") || return 1
    [[ "$(<"$yaml_file")" == "$expected" ]]
}

# Select the per-claim scalars (KUBECTL_PVC, KUBECTL_PVC_UID, KUBECTL_PV,
# KUBECTL_PV_UID, KUBECTL_PVC_LEASE_NAME) and the claim's namespace
# (KUBECTL_NAMESPACE, KUBECTL_NAMESPACE_UID) of claim J of the loaded identity,
# recording J in KUBECTL_ACTIVE_CLAIM. The Lease and identity helpers then act
# on that claim.
kubectl_use_claim() {
    local index="$1"
    if ! _kubectl_validate_claim_index "$index" \
            || [[ ! "${KUBECTL_CLAIM_COUNT:-}" =~ ^[1-9][0-9]?$ ]] \
            || (( index >= KUBECTL_CLAIM_COUNT )); then
        printf 'Error: Kubernetes claim %q is not in the loaded attempt identity\n' \
            "$index" >&2
        return 1
    fi
    KUBECTL_NAMESPACE="${KUBECTL_CLAIM_NAMESPACES[$index]}"
    KUBECTL_NAMESPACE_UID="${KUBECTL_CLAIM_NAMESPACE_UIDS[$index]}"
    KUBECTL_PVC="${KUBECTL_CLAIM_PVCS[$index]}"
    KUBECTL_PVC_UID="${KUBECTL_CLAIM_PVC_UIDS[$index]}"
    # shellcheck disable=SC2034  # Part of the selected claim's identity.
    KUBECTL_PV="${KUBECTL_CLAIM_PVS[$index]}"
    KUBECTL_PV_UID="${KUBECTL_CLAIM_PV_UIDS[$index]}"
    KUBECTL_PVC_LEASE_NAME="${KUBECTL_CLAIM_LEASE_NAMES[$index]}"
    KUBECTL_ACTIVE_CLAIM="$index"
}

# The claim indexes of segment K of the loaded identity, in claim order.
kubectl_segment_claim_indexes() {
    local segment="$1" index
    for ((index = 0; index < ${KUBECTL_CLAIM_COUNT:-0}; index++)); do
        [[ "${KUBECTL_CLAIM_SEGMENTS[$index]}" != "$segment" ]] \
            || printf '%s\n' "$index"
    done
}

# The sorted claim names of segment K of the loaded identity.
kubectl_segment_claims() {
    local index
    while IFS= read -r index; do
        printf '%s\n' "${KUBECTL_CLAIM_PVCS[$index]}"
    done < <(kubectl_segment_claim_indexes "$1")
}

# Apply segment K's control layout when KUBECTL_SEGMENT_CONTROL_ROOTS (the
# loaded configuration's, or the submitter's plan) has a control root in its
# namespace; otherwise leave the current KUBECTL_CONTROL_* layout unchanged.
# The roots are found by namespace, not by position: a namespace's control
# root is the lexically first root of every group in it, so it is the same
# for every attempt of one results directory, whichever segments each has.
_kubectl_apply_segment_control_layout() {
    local index="$1" logical="" candidate namespace claim path
    declare -p KUBECTL_SEGMENT_CONTROL_ROOTS >/dev/null 2>&1 || return 0
    for candidate in "${KUBECTL_SEGMENT_CONTROL_ROOTS[@]}"; do
        [[ "$candidate" == "${KUBECTL_SEGMENT_NAMESPACES[index]}/"*:* ]] || continue
        logical="$candidate"
        break
    done
    [[ -n "$logical" ]] || return 0
    _kubectl_logical_parts "$logical" namespace claim path
    KUBECTL_CONTROL_LOGICAL_ROOT="$logical"
    KUBECTL_CONTROL_TEST_ROOT="$KUBECTL_SWEEP_MOUNT_ROOT/$claim/$path"
    KUBECTL_CONTROL_ROOT="$KUBECTL_CONTROL_TEST_ROOT/$KUBECTL_SWEEP_RESERVED_ROOT"
    export KUBECTL_CONTROL_LOGICAL_ROOT KUBECTL_CONTROL_TEST_ROOT KUBECTL_CONTROL_ROOT
}

# Point the segment globals (KUBECTL_NAMESPACE, KUBECTL_NAMESPACE_UID) at
# segment K of the loaded identity, select its first claim (kubectl_use_claim)
# and its control layout, and record K in KUBECTL_ACTIVE_SEGMENT. A later
# identity load resets the selection to segment 0 unless it runs under
# kubectl_with_segment.
kubectl_use_segment() {
    local index="$1" first_claim
    if ! _kubectl_validate_segment_index "$index" \
            || [[ ! "${KUBECTL_SEGMENT_COUNT:-}" =~ ^[1-9][0-9]?$ ]] \
            || (( index >= KUBECTL_SEGMENT_COUNT )); then
        printf 'Error: Kubernetes segment %q is not in the loaded attempt identity\n' \
            "$index" >&2
        return 1
    fi
    first_claim=$(kubectl_segment_claim_indexes "$index" | head -n 1)
    [[ -n "$first_claim" ]] || return 1
    kubectl_use_claim "$first_claim" || return 1
    KUBECTL_NAMESPACE="${KUBECTL_SEGMENT_NAMESPACES[$index]}"
    KUBECTL_NAMESPACE_UID="${KUBECTL_SEGMENT_NAMESPACE_UIDS[$index]}"
    KUBECTL_ACTIVE_SEGMENT="$index"
    _kubectl_apply_segment_control_layout "$index"
}

# kubectl_with_segment K COMMAND [ARG...]: run COMMAND with segment K selected,
# including after every identity reload COMMAND performs, then restore the
# previously active segment of the loaded identity.
kubectl_with_segment() {
    local KUBECTL_SEGMENT_SELECTION="$1" previous="${KUBECTL_ACTIVE_SEGMENT:-0}" rc=0
    local KUBECTL_CLAIM_SELECTION=""
    shift
    [[ $# -gt 0 ]] && _kubectl_validate_segment_index "$KUBECTL_SEGMENT_SELECTION" \
        || return 1
    if [[ -n "${KUBECTL_SEGMENT_COUNT:-}" ]]; then
        kubectl_use_segment "$KUBECTL_SEGMENT_SELECTION" || return 1
    fi
    "$@" || rc=$?
    if [[ -n "${KUBECTL_SEGMENT_COUNT:-}" ]] && (( previous < KUBECTL_SEGMENT_COUNT )); then
        kubectl_use_segment "$previous" || true
    fi
    return "$rc"
}

# kubectl_with_claim J COMMAND [ARG...]: run COMMAND with claim J selected
# (kubectl_use_claim), also after every identity reload, then restore the
# active segment's selection.
kubectl_with_claim() {
    local KUBECTL_CLAIM_SELECTION="$1" segment="${KUBECTL_ACTIVE_SEGMENT:-0}" rc=0
    local KUBECTL_SEGMENT_SELECTION="${KUBECTL_ACTIVE_SEGMENT:-0}"
    shift
    [[ $# -gt 0 ]] && _kubectl_validate_claim_index "$KUBECTL_CLAIM_SELECTION" \
        || return 1
    kubectl_use_claim "$KUBECTL_CLAIM_SELECTION" || return 1
    "$@" || rc=$?
    if [[ -n "${KUBECTL_SEGMENT_COUNT:-}" ]] && (( segment < KUBECTL_SEGMENT_COUNT )); then
        kubectl_use_segment "$segment" || true
    fi
    return "$rc"
}

# _kubectl_for_each_claim COMMAND [ARG...]: run COMMAND for every claim of the
# loaded identity in claim order (the Lease acquisition order); stop at the
# first failure.
_kubectl_for_each_claim() {
    local index
    for ((index = 0; index < ${KUBECTL_CLAIM_COUNT:-0}; index++)); do
        kubectl_with_claim "$index" "$@" || return 1
    done
}

# Resource names. Segment 0 keeps the plain names; segment K>0 inserts
# "-s<K>" after the attempt ID:
#   sst-elb-<id>[-s<K>]-{workers,worker-net,coord-net,sweep,sequencer}
#   sst-elb-<id>[-s<K>]-{upload,status,collector}-<token>
# The per-segment "sequencer" name is that segment's sequencer Role and
# RoleBinding. The sequencer Job and ServiceAccount (namespace 0) are named by
# kubectl_sequencer_resource_name; Kubernetes scopes names by kind, so segment
# 0's Role may share that name.
kubectl_segment_suffix() {
    local index="$1"
    _kubectl_validate_segment_index "$index" || return 1
    if [[ "$index" == 0 ]]; then
        printf '\n'
    else
        printf -- '-s%s\n' "$index"
    fi
}

kubectl_segment_resource_name() {
    local attempt_id="$1" index="$2" component="$3" token="${4:-}" suffix=""
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] && _kubectl_validate_segment_index "$index" \
        || return 1
    [[ "$index" == 0 ]] || suffix="-s$index"
    case "$component" in
        workers|worker-net|coord-net|sweep|sequencer)
            [[ -z "$token" ]] || return 1
            printf 'sst-elb-%s%s-%s\n' "$attempt_id" "$suffix" "$component"
            ;;
        upload|status|collector)
            [[ "$token" =~ ^[0-9a-f]{8}$ ]] || return 1
            printf 'sst-elb-%s%s-%s-%s\n' "$attempt_id" "$suffix" "$component" "$token"
            ;;
        *) return 1 ;;
    esac
}

kubectl_sequencer_resource_name() {
    local attempt_id="$1"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    printf 'sst-elb-%s-sequencer\n' "$attempt_id"
}

# Journal keys. Segment 0 keeps the plain keys; segment K>0 appends "-s<K>".
# The multi-segment-only sequencer RBAC keys always carry the segment
# ("sequencer-role-s0"). The sequencer Job and ServiceAccount use the fixed
# keys "sequencer" and "sequencer-sa". Leases are per claim
# (kubectl_claim_lease_key).
kubectl_segment_resource_key() {
    local index="$1" base="$2"
    _kubectl_validate_segment_index "$index" || return 1
    case "$base" in
        sweep|workers|transfer|worker-net|coord-net)
            if [[ "$index" == 0 ]]; then
                printf '%s\n' "$base"
            else
                printf '%s-s%s\n' "$base" "$index"
            fi
            ;;
        sequencer-role|sequencer-binding) printf '%s-s%s\n' "$base" "$index" ;;
        *) return 1 ;;
    esac
}

# The journal key of claim J's Lease: pvc-lease for claim 0, else
# pvc-lease-c<J>. Its release step is "release-" followed by the key.
kubectl_claim_lease_key() {
    local index="$1"
    _kubectl_validate_claim_index "$index" || return 1
    if [[ "$index" == 0 ]]; then
        printf 'pvc-lease\n'
    else
        printf 'pvc-lease-c%s\n' "$index"
    fi
}

kubectl_resource_key_is_lease() {
    [[ "$1" =~ ^pvc-lease(-c[1-9][0-9]?)?$ ]]
}

# The segment whose per-segment journal keys, steps and local files an
# operation uses: the kubectl_with_segment selection, else the active segment.
_kubectl_selected_segment() {
    printf '%s\n' "${KUBECTL_SEGMENT_SELECTION:-${KUBECTL_ACTIVE_SEGMENT:-0}}"
}

# Empty for segment 0, "-s<K>" for segment K: appended to per-segment local
# files (remote-reservation.sh, worker-endpoints.tsv, control-bundle, ...)
# and journal steps (release-remote-run, ...).
_kubectl_selected_segment_suffix() {
    kubectl_segment_suffix "$(_kubectl_selected_segment)"
}

# _kubectl_selected_segment_key BASE: the selected segment's journal key.
_kubectl_selected_segment_key() {
    kubectl_segment_resource_key "$(_kubectl_selected_segment)" "$1"
}

# The selected claim's Lease journal key.
_kubectl_selected_claim_lease_key() {
    kubectl_claim_lease_key "${KUBECTL_CLAIM_SELECTION:-${KUBECTL_ACTIVE_CLAIM:-0}}"
}

# How messages name segment INDEX of the loaded attempt.
_kubectl_segment_label() {
    printf 'segment %s (namespace %s)\n' "$1" "${KUBECTL_SEGMENT_NAMESPACES[$1]:-unknown}"
}

# _kubectl_attempt_results_suffixes OUT KUBERNETES_DIR ATTEMPT_ID: per segment,
# the suffix of its results-root ledger files. One segment publishes the
# plain names (""). Several publish "-<namespace>" each, because run.status
# and run-summary.tsv then hold the attempt's aggregate outcome. Reads only
# saved files, never the loaded globals.
_kubectl_attempt_results_suffixes() {
    local -n _kubectl_results_suffixes="$1"
    local kubernetes_dir="$2" attempt_id="$3" namespaces namespace
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    namespaces=$(kubectl_attempt_load_identity "$kubernetes_dir/attempts/$attempt_id" \
        && printf '%s\n' "${KUBECTL_SEGMENT_NAMESPACES[@]}") || return 1
    _kubectl_results_suffixes=()
    while IFS= read -r namespace; do
        _kubectl_results_suffixes+=("-$namespace")
    done <<< "$namespaces"
    (( ${#_kubectl_results_suffixes[@]} > 1 )) || _kubectl_results_suffixes=("")
}

# The results-root ledger suffix of the loaded attempt's segment INDEX.
_kubectl_results_suffix() {
    local index="$1"
    if [[ "${KUBECTL_SEGMENT_COUNT:-1}" -gt 1 ]]; then
        printf -- '-%s\n' "${KUBECTL_SEGMENT_NAMESPACES[index]}"
    else
        printf '\n'
    fi
}

# _kubectl_append_segment_keys OUTPUT COUNT forward|reverse BASE: append the
# BASE journal key of every segment to the caller's OUTPUT array.
_kubectl_append_segment_keys() {
    local -n _kubectl_append_keys_output="$1"
    local count="$2" order="$3" base="$4" index position
    for ((position = 0; position < count; position++)); do
        index="$position"
        [[ "$order" == forward ]] || index=$((count - 1 - position))
        if [[ "$base" == sequencer-* ]]; then
            _kubectl_append_keys_output+=("$base-s$index")
        elif [[ "$index" == 0 ]]; then
            _kubectl_append_keys_output+=("$base")
        else
            _kubectl_append_keys_output+=("$base-s$index")
        fi
    done
}

# kubectl_workload_keys_for_segment_count OUTPUT COUNT: keys of the actors
# that may use a claim, in stop order: the sequencer (several segments only,
# so it cannot start another Job), every sweep Job, then DaemonSets in
# reverse segment order. One segment: "sweep workers".
kubectl_workload_keys_for_segment_count() {
    local output_name="$1" count="$2"
    [[ "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
        && _kubectl_validate_segment_count "$count" || return 1
    local -a _kubectl_workload_keys=()
    (( count == 1 )) || _kubectl_workload_keys+=(sequencer)
    _kubectl_append_segment_keys _kubectl_workload_keys "$count" forward sweep
    _kubectl_append_segment_keys _kubectl_workload_keys "$count" reverse workers
    local -n _kubectl_workload_output="$output_name"
    _kubectl_workload_output=("${_kubectl_workload_keys[@]}")
}

# Ordered cleanup keys for COUNT segments, excluding Leases (see
# kubectl_lease_keys_for_claim_count):
#   sequencer, sweep Jobs, DaemonSets (reverse segment order), transfer
#   helpers, inspectors (fixed status/collector keys, then the given
#   ephemeral INSPECTOR_KEYs), policies (coord-net then worker-net per
#   segment), then sequencer RoleBindings, Roles and ServiceAccount.
# One segment keeps the order in which the ephemeral inspectors follow the
# policies:
#   sweep workers transfer status collector coord-net worker-net INSPECTOR...
kubectl_resource_keys_for_segment_count() {
    local output_name="$1" count="$2" index suffix
    shift 2
    [[ "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
        && _kubectl_validate_segment_count "$count" || return 1
    local -a _kubectl_cleanup_keys=()
    kubectl_workload_keys_for_segment_count _kubectl_cleanup_keys "$count" || return 1
    _kubectl_append_segment_keys _kubectl_cleanup_keys "$count" forward transfer
    _kubectl_cleanup_keys+=(status collector)
    (( count == 1 )) || _kubectl_cleanup_keys+=("$@")
    for ((index = 0; index < count; index++)); do
        suffix=""
        [[ "$index" == 0 ]] || suffix="-s$index"
        _kubectl_cleanup_keys+=("coord-net$suffix" "worker-net$suffix")
    done
    if (( count == 1 )); then
        _kubectl_cleanup_keys+=("$@")
    else
        _kubectl_append_segment_keys _kubectl_cleanup_keys "$count" forward sequencer-binding
        _kubectl_append_segment_keys _kubectl_cleanup_keys "$count" forward sequencer-role
        _kubectl_cleanup_keys+=(sequencer-sa)
    fi
    local -n _kubectl_cleanup_output="$output_name"
    _kubectl_cleanup_output=("${_kubectl_cleanup_keys[@]}")
}

# Lease keys in release order: the reverse of acquisition (claim order), so
# claim J's Lease is released only after every later claim's.
kubectl_lease_keys_for_claim_count() {
    local output_name="$1" count="$2" index
    [[ "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
        && _kubectl_validate_claim_count "$count" || return 1
    local -a _kubectl_lease_keys=()
    for ((index = count - 1; index >= 0; index--)); do
        _kubectl_lease_keys+=("$(kubectl_claim_lease_key "$index")")
    done
    local -n _kubectl_lease_output="$output_name"
    _kubectl_lease_output=("${_kubectl_lease_keys[@]}")
}

# Read the saved identity's segment count without changing loaded globals.
kubectl_attempt_segment_count() {
    local output_name="$1" kubernetes_dir="$2" attempt_id="$3" counted
    [[ "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
        && "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    counted=$(kubectl_attempt_load_identity "$kubernetes_dir/attempts/$attempt_id" \
        && printf '%s' "$KUBECTL_SEGMENT_COUNT") || return 1
    _kubectl_validate_segment_count "$counted" || return 1
    printf -v "$output_name" '%s' "$counted"
}

# Read the saved identity's claim count without changing loaded globals.
kubectl_attempt_claim_count() {
    local output_name="$1" kubernetes_dir="$2" attempt_id="$3" counted
    [[ "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
        && "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    counted=$(kubectl_attempt_load_identity "$kubernetes_dir/attempts/$attempt_id" \
        && printf '%s' "$KUBECTL_CLAIM_COUNT") || return 1
    _kubectl_validate_claim_count "$counted" || return 1
    printf -v "$output_name" '%s' "$counted"
}

# Diagnostics are best effort: an unreadable identity still inspects segment 0.
_kubectl_attempt_diagnostic_segment_count() {
    kubectl_attempt_segment_count "$@" 2>/dev/null || printf -v "$1" '%s' 1
}

# kubectl_attempt_resource_keys OUTPUT KUBERNETES_DIR ATTEMPT_ID: the one
# ordered cleanup enumeration for an attempt, derived from its saved identity
# and including journaled status/collector inspector keys.
# An attempt whose identity directory was never published cannot own any
# journaled resource: every journal lives inside that directory.
_kubectl_attempt_keys_if_unpublished() {
    local output_name="$1" kubernetes_dir="$2" attempt_id="$3"
    [[ "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ && "$attempt_id" =~ ^[0-9a-f]{8}$ ]] \
        || return 1
    [[ ! -e "$kubernetes_dir/attempts/$attempt_id" \
        && ! -L "$kubernetes_dir/attempts/$attempt_id" ]] || return 1
    local -n _kubectl_unpublished_output="$output_name"
    _kubectl_unpublished_output=()
}

kubectl_attempt_resource_keys() {
    local output_name="$1" kubernetes_dir="$2" attempt_id="$3" count resource_file
    local -a inspector_keys=()
    _kubectl_attempt_keys_if_unpublished "$@" && return 0
    kubectl_attempt_segment_count count "$kubernetes_dir" "$attempt_id" || return 1
    for resource_file in "$kubernetes_dir/attempts/$attempt_id/resources"/{status,collector}-*.sh; do
        [[ -f "$resource_file" && ! -L "$resource_file" ]] || continue
        inspector_keys+=("$(basename "$resource_file" .sh)")
    done
    kubectl_resource_keys_for_segment_count "$output_name" "$count" "${inspector_keys[@]}"
}

kubectl_attempt_lease_keys() {
    local output_name="$1" kubernetes_dir="$2" attempt_id="$3" count
    _kubectl_attempt_keys_if_unpublished "$@" && return 0
    kubectl_attempt_claim_count count "$kubernetes_dir" "$attempt_id" || return 1
    kubectl_lease_keys_for_claim_count "$output_name" "$count"
}

kubectl_attempt_workload_keys() {
    local output_name="$1" kubernetes_dir="$2" attempt_id="$3" count
    _kubectl_attempt_keys_if_unpublished "$@" && return 0
    kubectl_attempt_segment_count count "$kubernetes_dir" "$attempt_id" || return 1
    kubectl_workload_keys_for_segment_count "$output_name" "$count"
}

# The worker Pods of the active segment of attempt RUN_ID.
kubectl_worker_pod_selector() {
    local run_id="$1"
    [[ "$run_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    printf 'storage-scale-test.nvidia.com/run=%s,app.kubernetes.io/component=workers,%s=%s\n' \
        "$run_id" "$KUBECTL_SEGMENT_LABEL_KEY" "${KUBECTL_ACTIVE_SEGMENT:-0}"
}

# kubectl_attempt_pod_selector RUN_ID [SEGMENT]: the label selector for one
# segment's Pods (workers, coordinator, helpers) of the loaded attempt RUN_ID;
# SEGMENT defaults to the active segment. A Pod without a segment (SEGMENT
# "-", such as the sequencer) and a run that is not the loaded attempt
# (validation Jobs) are selected by the run label only.
kubectl_attempt_pod_selector() {
    local run_id="$1" segment="${2:-${KUBECTL_ACTIVE_SEGMENT:-0}}" selector
    [[ "$run_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    [[ "$segment" == - ]] || _kubectl_validate_segment_index "$segment" || return 1
    selector="storage-scale-test.nvidia.com/run=$run_id"
    if [[ "$segment" != - && "${KUBECTL_ATTEMPT_ID:-}" == "$run_id" ]]; then
        selector+=",$KUBECTL_SEGMENT_LABEL_KEY=$segment"
    fi
    printf '%s\n' "$selector"
}

# The segment of journal KEY ("-" for the segment-less sequencer and Lease
# keys).
_kubectl_resource_key_segment() {
    local key="$1"
    case "$key" in
        sequencer|sequencer-sa|pvc-lease|pvc-lease-c*) printf -- '-\n' ;;
        *-s[0-9]|*-s[0-9][0-9]) printf '%s\n' "${key##*-s}" ;;
        *) printf '0\n' ;;
    esac
}

kubectl_attempt_load_metadata() {
    local metadata_dir="$1"
    local state_file="$metadata_dir/state.sh"
    kubectl_attempt_load_identity "$metadata_dir" || return
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
    [[ "$state" =~ ^(PREPARED|SUBMITTED|RUNNING|SUCCESS|FAILED|CANCELLED|CANCEL_REQUESTED|SUBMISSION_FAILED|COLLECTED)$ ]] \
        || return 1
    printf 'STATE=%s\n' "$state"
}

# A failed submission collects nothing: a batch resumes (retrying it), and a
# plain run needs its cause fixed and a new submission.
_kubectl_emit_submission_failed() {
    local operation="$1" results_dir="$2" next=INSPECT
    kubectl_emit_state SUBMISSION_FAILED
    [[ "$operation" == status ]] || return 0
    [[ ! -f "$results_dir/batch-manifest.tsv" ]] || next=RESUME
    printf 'NEXT_ACTION=%s\n' "$next"
}

# Presentation only: durable lifecycle states and collection gates are unchanged.
kubectl_emit_status_view() {
    local results="$1" attempt="$2" outcome="$3" source="$4" collection="$5"
    local progress="$6" pending running success failed failed_id extra display next total
    read -r pending running success failed failed_id extra <<< "$progress"
    [[ "$pending:$running:$success:$failed" =~ ^[0-9]+:[0-9]+:[0-9]+:[0-9]+$ \
        && "$failed_id" =~ ^([0-9]{4})?$ && -z "$extra" && "$outcome" =~ ^(PREPARED|SUBMITTED|RUNNING|SUCCESS|FAILED|CANCELLED)$ \
        && "$source" =~ ^(LOCAL|PVC)$ \
        && "$collection" =~ ^(PENDING|CLEANUP_PENDING|COMPLETE)$ ]] || {
        echo 'Error: invalid Kubernetes status view' >&2; return 1;
    }
    total=$((pending + running + success + failed))
    (( total > 0 && total <= 9999 )) || return 1
    display="$outcome" next=WAIT
    case "$outcome" in
        PREPARED|SUBMITTED) display=PREPARING ;;
        SUCCESS|FAILED|CANCELLED)
            next=COLLECT
            if [[ "$collection" == COMPLETE ]]; then
                next=NONE
                (( pending + running + failed == 0 )) || next=RESUME
            fi ;;
    esac
    [[ ! -f "$results/batch-manifest.tsv" ]] || printf 'BATCH=SEALED\n'
    printf 'ATTEMPT=%s\nSTATE=%s\n' "$attempt" "$display"
    printf 'EXECUTION_SCOPE=CURRENT_ATTEMPT\nPROGRESS_SOURCE=%s\n' "$source"
    printf 'EXECUTIONS_TOTAL=%s\nEXECUTIONS_PENDING=%s\nEXECUTIONS_RUNNING=%s\n' \
        "$total" "$pending" "$running"
    printf 'EXECUTIONS_SUCCEEDED=%s\nEXECUTIONS_FAILED=%s\n' "$success" "$failed"
    printf 'RESULT_COLLECTION=%s\nNEXT_ACTION=%s\n' "$collection" "$next"
    [[ -z "$failed_id" ]] || _kubectl_print_failed_execution "$results" "$attempt" \
        "$failed_id" || return 1
    [[ ! -f "$results/batch-manifest.tsv" ]] || _elbencho_batch_print_group_targets "$results"
}

# _kubectl_print_failed_execution RESULTS ATTEMPT ID: the FAILED_* keys, with
# the newest coordinator log _kubectl_save_coordinator_log kept as FAILED_LOG.
# The launchers always load lib/_batch_functions.sh; load it when they did not.
_kubectl_print_failed_execution() {
    local results="$1" attempt="$2" id="$3"
    if ! declare -F elbencho_print_failed_execution >/dev/null; then
        # shellcheck disable=SC1091  # The launchers normally load it first.
        source "$(dirname "${BASH_SOURCE[0]}")/../../../lib/_batch_functions.sh" \
            || return 1
    fi
    elbencho_print_failed_execution "$results" "$id" \
        "$(elbencho_newest_file "$results/kubernetes/attempts/$attempt"/coordinator*.log)"
}

# kubectl_emit_state for --collect, then, for a FAILED attempt, the FAILED_*
# keys of the first failed cell in the collected segment publications.
_kubectl_emit_collected_state() {
    local terminal="$1" results_dir="$2" attempt_id="$3" index suffix state_dir progress
    local id=""
    kubectl_emit_state "$terminal" || return 1
    [[ "$terminal" == FAILED ]] || return 0
    for ((index = 0; index < ${KUBECTL_SEGMENT_COUNT:-1}; index++)); do
        suffix=$(kubectl_segment_suffix "$index") || return 1
        state_dir="$results_dir/kubernetes/attempts/$attempt_id/collected-state$suffix"
        [[ -d "$state_dir" && ! -L "$state_dir" ]] || continue
        progress=$(kubectl_read_collected_execution_progress "$state_dir") || continue
        read -r _ _ _ _ id _ <<< "$progress"
        [[ -z "$id" ]] || break
    done
    [[ -z "$id" ]] || _kubectl_print_failed_execution "$results_dir" "$attempt_id" "$id"
}

kubectl_read_collected_execution_progress() {
    # The caller first verifies the complete local publication manifest.
    local manifest="$1/publication-manifest.tsv"
    awk -F '\t' '
        $1=="execution" {counts[$3]++; total++}
        $1=="execution" && $3=="FAILED" && first=="" {first=$2}
        END {if(!total) exit 1; printf "%d %d %d %d%s\n",
            counts["PENDING"], counts["RUNNING"], counts["SUCCESS"], counts["FAILED"],
            (first == "" ? "" : " " first)}
    ' "$manifest"
}

_kubectl_validate_placeholder() {
    local token="$1" value="$2"
    [[ "$value" != *$'\n'* && "$value" != *$'\r'* \
        && "$value" != *$'\t'* ]] || return 1
    case "$token" in
        NAMESPACE) kubectl_validate_namespace_name "$value" ;;
        RESOURCE_NAME|NODE_NAME|COORDINATOR_NODE)
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
        SEGMENT_INDEX) _kubectl_validate_segment_index "$value" ;;
        JOB_SUSPEND) [[ "$value" =~ ^(true|false)$ ]] ;;
        JOB_NAME|SERVICE_ACCOUNT_NAME) kubectl_validate_object_name "$value" ;;
        SUBJECT_NAMESPACE) kubectl_validate_namespace_name "$value" ;;
        POLL_SECONDS) _kubectl_validate_sequencer_poll_seconds "$value" ;;
        ACTIVE_DEADLINE_SECONDS) [[ "$value" =~ ^[1-9][0-9]{0,4}$ ]] ;;
        RUN_AS_USER|RUN_AS_GROUP) [[ "$value" =~ ^[1-9][0-9]*$ ]] ;;
        COMPONENT)
            [[ "$value" =~ ^(worker|coordinator|controller|probe)$ ]]
            ;;
        SELECTOR_KEY) kubectl_validate_label_key "$value" ;;
        SELECTOR_VALUE) kubectl_validate_label_value "$value" ;;
        REMOTE_RUN_DIRECTORY)
            [[ "$value" =~ ^/mnt/storage-scale-test/([A-Za-z0-9._-]+/){2,}\.storage-scale-test/runs/[0-9a-f]{8}(/[A-Za-z0-9._-]+)*$ ]]
            ;;
        TEST_ROOT)
            [[ "$value" =~ ^/mnt/storage-scale-test/([A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+$ ]]
            ;;
        MOUNT_ROOT) [[ "$value" =~ ^(/[A-Za-z0-9._-]+)+$ ]] ;;
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
    local process_timeout="${KUBECTL_PROCESS_TIMEOUT_SECONDS:-$KUBECTL_PROCESS_TIMEOUT_SECONDS_DEFAULT}"
    # Keep kubectl in timeout's child process group. In particular, a wedged
    # exec stream can otherwise survive TERM while timeout waits in foreground.
    _kubectl_local_timeout --kill-after=5s "${process_timeout}s" \
        kubectl --request-timeout="${request_timeout}s" "$@"
}

_kubectl_local_timeout() {
    local timeout_command=timeout
    if [[ $(uname -s) == Darwin ]]; then
        timeout_command=gtimeout
    elif ! command -v timeout >/dev/null 2>&1 \
            && command -v gtimeout >/dev/null 2>&1; then
        timeout_command=gtimeout
    fi
    command -v "$timeout_command" >/dev/null 2>&1 || {
        echo "Error: GNU timeout is required (on macOS: brew install coreutils)" >&2
        return 127
    }
    "$timeout_command" "$@"
}

_kubectl_local_tar() {
    local tar_command=tar
    if [[ $(uname -s) == Darwin ]]; then
        tar_command=gtar
    fi
    command -v "$tar_command" >/dev/null 2>&1 || {
        echo "Error: GNU tar is required (on macOS: brew install gnu-tar)" >&2
        return 127
    }
    "$tar_command" "$@"
}

kubectl_report_lifecycle_error() {
    local operation="$1" phase="$2" reason="$3" safe_next_action="$4"
    local may_still_be_running="$5" kind="${6:-}" name="${7:-}"
    local namespace="${8:-}" expected_uid="${9:-}" observed_uid="${10:-}"
    local diagnostic_path="${11:-}" job_evidence="${12:-}"
    [[ "$operation" =~ ^[a-z][a-z0-9-]*$ \
        && "$phase" =~ ^[a-z][a-z0-9-]*$ \
        && "$reason" =~ ^(AUTH|TIMEOUT|API_THROTTLED|API_UNAVAILABLE|TRANSFER_FAILED|IDENTITY_MISMATCH|POD_UNSCHEDULABLE|IMAGE_PULL|PVC_MOUNT|PVC_IO|ENOSPC|LOCAL_IO|PATH_REJECTED|INSUFFICIENT_CAPACITY|ARCHIVE_INVALID|LEDGER_INCONSISTENT|OWNERSHIP_AMBIGUOUS|CANCELLATION_INCOMPLETE|INVALID_LIFECYCLE_OPERATION)$ \
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
        printf 'STORAGE_SCALE_TEST_DIAGNOSTIC_JOB_EVIDENCE=%q\n' "$job_evidence"
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

kubectl_report_collection_failure() {
    local attempt_id="$1" phase="$2" reason="$3" action="$4"
    local local_path="$5" remote_path="$6" diagnostic_path="${7:-}"
    local kind="${8:-}" name="${9:-}" namespace="${10:-}"
    local expected_uid="${11:-}" observed_uid="${12:-}" job_evidence="${13:-}"
    KUBECTL_ATTEMPT_ID="$attempt_id" \
        KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="$local_path" \
        KUBECTL_DIAGNOSTIC_REMOTE_STATE_PATH="$remote_path" \
        kubectl_report_lifecycle_error collect "$phase" "$reason" "$action" no \
            "$kind" "$name" "${namespace:-${KUBECTL_NAMESPACE:-}}" \
            "$expected_uid" "$observed_uid" "$diagnostic_path" \
            "$job_evidence" || true
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
        LOCAL_IO) printf '%s' 'repair local storage or permissions and retry submission' ;;
        PATH_REJECTED)
            printf '%s' 'correct the configured PVC path or unsafe symlink and retry submission'
            ;;
        INSUFFICIENT_CAPACITY)
            printf '%s' 'adjust the node selector or add eligible worker capacity and retry submission'
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

kubectl_run_prepare_phase() {
    local phase="$1"
    shift
    local stderr_path rc=0 output=""
    [[ "$phase" =~ ^[a-z][a-z0-9-]*$ ]] || return 1
    stderr_path=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-prepare.XXXXXX") \
        || return 1
    "$@" 2> "$stderr_path" || rc=$?
    output=$(head -c 524288 -- "$stderr_path" 2>/dev/null) || output=""
    cat -- "$stderr_path" >&2 || rc=1
    rm -f -- "$stderr_path" || rc=1
    if [[ "$rc" -ne 0 ]]; then
        KUBECTL_PREPARE_FAILURE_PHASE="$phase"
        KUBECTL_PREPARE_FAILURE_REASON=""
        KUBECTL_PREPARE_FAILURE_RC="$rc"
        KUBECTL_PREPARE_FAILURE_OUTPUT="$output"
    fi
    return "$rc"
}

kubectl_run_prepare_phase_capture() {
    local output_variable="$1" phase="$2"
    shift 2
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    local output_path stderr_path rc=0 output="" error_output=""
    output_path=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-prepare-out.XXXXXX") \
        || return 1
    stderr_path=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-prepare-err.XXXXXX") \
        || { rm -f -- "$output_path"; return 1; }
    "$@" > "$output_path" 2> "$stderr_path" || rc=$?
    output=$(cat -- "$output_path") || rc=1
    error_output=$(head -c 524288 -- "$stderr_path" 2>/dev/null) || error_output=""
    cat -- "$stderr_path" >&2 || rc=1
    rm -f -- "$output_path" "$stderr_path" || rc=1
    printf -v "$output_variable" '%s' "$output"
    if [[ "$rc" -ne 0 ]]; then
        KUBECTL_PREPARE_FAILURE_PHASE="$phase"
        KUBECTL_PREPARE_FAILURE_REASON=""
        KUBECTL_PREPARE_FAILURE_RC="$rc"
        KUBECTL_PREPARE_FAILURE_OUTPUT="$error_output$output"
    fi
    return "$rc"
}

kubectl_classify_prepare_failure() {
    local output_variable="$1" phase="$2" rc="$3" output="$4"
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    local reason=""
    if [[ "$phase" == local-* ]]; then
        reason=LOCAL_IO
    else
        _kubectl_classify_observation_failure reason "$rc" "$output" \
            || reason=API_UNAVAILABLE
    fi
    if grep -Eqi 'no space left|enospc' <<< "$output"; then
        reason=ENOSPC
    elif [[ "$phase" == node-capacity ]]; then
        reason=INSUFFICIENT_CAPACITY
    elif grep -Eqi 'path escapes (PVC|its claim) mount|overlaps orchestration state|unsafe symlink|symlink containment' \
            <<< "$output"; then
        reason=PATH_REJECTED
    elif [[ "$phase" =~ ^(remote-reservation|remote-control)$ \
            && "$reason" == API_UNAVAILABLE ]] \
            && grep -Eqi 'permission denied|read-only file system|input/output error|stale file handle' \
                <<< "$output"; then
        reason=PVC_IO
    elif [[ "$phase" =~ ^(helper-ready|worker-ready)$ \
            && "$reason" == API_UNAVAILABLE ]]; then
        reason=POD_UNSCHEDULABLE
    fi
    printf -v "$output_variable" '%s' "$reason"
}

kubectl_record_prepare_failure() {
    local phase="$1" reason="$2" rc="$3" output="$4"
    [[ "$phase" =~ ^[a-z][a-z0-9-]*$ \
        && "$reason" =~ ^(LOCAL_IO|LEDGER_INCONSISTENT|INSUFFICIENT_CAPACITY|PATH_REJECTED)$ \
        && "$rc" =~ ^[1-9][0-9]*$ ]] || return 1
    KUBECTL_PREPARE_FAILURE_PHASE="$phase"
    KUBECTL_PREPARE_FAILURE_REASON="$reason"
    KUBECTL_PREPARE_FAILURE_RC="$rc"
    KUBECTL_PREPARE_FAILURE_OUTPUT="$output"
}

kubectl_capture_retained_resource_identities() {
    local metadata_dir="$1" run_id="$2" destination="$3"
    local kubernetes_dir="${metadata_dir%/attempts/"$run_id"}"
    [[ "$metadata_dir" == "$kubernetes_dir/attempts/$run_id" \
        && "$run_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local key intent_file count
    local -a keys=()
    _kubectl_attempt_diagnostic_segment_count count "$kubernetes_dir" "$run_id"
    kubectl_workload_keys_for_segment_count keys "$count" || return 1
    _kubectl_append_segment_keys keys "$count" forward transfer
    {
        printf 'schema\t%s\n' "$KUBECTL_ATTEMPT_RESOURCE_SCHEMA_VERSION"
        printf 'key\tsource\tkind\tname\tnamespace\tuid\tnonce\n'
        for key in "${keys[@]}"; do
            if kubectl_attempt_resource_exists "$kubernetes_dir" "$run_id" "$key" \
                    && kubectl_attempt_load_resource "$kubernetes_dir" "$run_id" "$key"; then
                printf '%s\tjournal\t%s\t%s\t%s\t%s\t%s\n' "$key" \
                    "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" \
                    "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_UID" \
                    "$KUBECTL_RESOURCE_NONCE"
            fi
            intent_file="$metadata_dir/creation-intents/$key.sh"
            if [[ -f "$intent_file" && ! -L "$intent_file" ]] \
                    && kubectl_attempt_load_creation_intent "$intent_file"; then
                printf '%s\tintent\t%s\t%s\t%s\t%s\t%s\n' "$key" \
                    "$KUBECTL_INTENT_KIND" "$KUBECTL_INTENT_NAME" \
                    "$KUBECTL_INTENT_NAMESPACE" \
                    "${KUBECTL_INTENT_UID:-unknown}" "$KUBECTL_INTENT_NONCE"
            fi
        done
    } > "$destination"
}

kubectl_capture_control_state_evidence() {
    local namespace="$1" pod="$2" run_id="$3" destination="$4"
    local remote_run guard_script
    remote_run=$(kubectl_attempt_remote_root "$run_id") || return 1
    guard_script=$(kubectl_remote_tree_guard_script) || return 1
    KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
        kubectl_pvc_exec "$namespace" "$pod" /bin/bash -ceu \
            "$guard_script
            state=\"\$run/state\"
            [[ -d \"\$state\" && ! -L \"\$state\" ]] || exit 1
            state_real=\$(realpath -e -- \"\$state\") || exit 1
            [[ \"\$state_real\" == \"\$run_real/state\" ]] || exit 1
            emit_file() {
                local relative=\"\$1\" path=\"\$state/\$1\"
                [[ -f \"\$path\" && ! -L \"\$path\" ]] || return 0
                printf '===== %s =====\\n' \"\$relative\"
                head -c 65536 -- \"\$path\"
                printf '\\n'
            }
            emit_file run.status
            emit_file run-summary.tsv
            emit_file publication-manifest.tsv
            emit_file coordinator-loss.tsv
            if [[ -d \"\$state/executions\" && ! -L \"\$state/executions\" ]]; then
                while IFS= read -r path; do
                    relative=\${path#\"\$state/\"}
                    emit_file \"\$relative\"
                done < <(find -P \"\$state/executions\" -maxdepth 1 -type f \
                    \\( -name '*.status' -o -name '*.exitcode' -o -name '*.workers.tsv' \\) \
                    -print | LC_ALL=C sort | head -n 512)
            fi" bash "$remote_run" "$run_id" \
            2>&1 | head -c 524288 > "$destination"
}

# kubectl_capture_resource_diagnostics METADATA_DIR OPERATION NAMESPACE KIND
# NAME RUN_ID [SEGMENT]: SEGMENT selects the Pods (see
# kubectl_attempt_pod_selector).
kubectl_capture_resource_diagnostics() {
    local metadata_dir="$1" operation="$2" namespace="$3" kind="$4" name="$5"
    local run_id="$6" target="${7:-${KUBECTL_ACTIVE_SEGMENT:-0}}" pod_selector
    _kubectl_validate_local_directory_path "$metadata_dir" \
        && [[ -d "$metadata_dir" && ! -L "$metadata_dir" \
        && "$operation" =~ ^[a-z][a-z0-9-]*$ \
        && "$kind" =~ ^[A-Za-z][A-Za-z0-9.-]*$ \
        && "$run_id" =~ ^[0-9a-f]{8}$ ]] \
        && kubectl_validate_namespace_name "$namespace" \
        && kubectl_validate_object_name "$name" || return 1
    pod_selector=$(kubectl_attempt_pod_selector "$run_id" "$target") || return 1
    local root="$metadata_dir/diagnostics" destination temporary candidate
    if [[ -e "$root" || -L "$root" ]]; then
        [[ -d "$root" && ! -L "$root" ]] || return 1
    else
        mkdir -- "$root" || return 1
    fi
    for candidate in "$root"/.diagnostic-tmp-*; do
        [[ -e "$candidate" || -L "$candidate" ]] || continue
        [[ -d "$candidate" && ! -L "$candidate" ]] || return 1
        rm -rf -- "$candidate" || return 1
    done
    destination="$root/${operation}-$(date -u +%Y%m%dT%H%M%SZ)-${BASHPID:-$$}-$RANDOM"
    temporary=$(mktemp -d "$root/.diagnostic-tmp-XXXXXXXX") || return 1
    {
        printf 'schema\t%s\n' "$KUBECTL_DIAGNOSTIC_SCHEMA_VERSION"
        printf 'operation\t%s\n' "$operation"
        printf 'attempt_id\t%s\n' "$run_id"
        printf 'namespace\t%s\n' "$namespace"
        printf 'resource_kind\t%s\n' "$kind"
        printf 'resource_name\t%s\n' "$name"
        printf 'captured_at\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$temporary/bundle.tsv" || {
        rm -rf -- "$temporary"
        return 1
    }
    KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
        kubectl_run_bounded -n "$namespace" get "$kind" "$name" -o yaml \
            2>&1 | head -c 524288 > "$temporary/resource.yaml" || true
    KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
        kubectl_run_bounded -n "$namespace" describe "$kind" "$name" \
            2>&1 | head -c 524288 > "$temporary/resource.describe" || true
    KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
        kubectl_run_bounded -n "$namespace" get pods \
            -l "$pod_selector" -o yaml \
            2>&1 | head -c 524288 > "$temporary/pods.yaml" || true
    KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
        kubectl_run_bounded -n "$namespace" logs \
            -l "$pod_selector" \
            --all-containers=true --prefix=true --tail=200 \
            2>&1 | head -c 1048576 > "$temporary/pods.log" || true
    local pod_names="" pod_ref pod_name evidence_pod=""
    pod_names=$(KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
        kubectl_run_bounded -n "$namespace" get pods \
            -l "$pod_selector" -o name 2>/dev/null \
            | head -c 65536) \
        || pod_names=""
    local pod_count=0
    : > "$temporary/events.txt" || {
        rm -rf -- "$temporary"
        return 1
    }
    while IFS= read -r pod_ref; do
        [[ "$pod_ref" == pod/* ]] || continue
        pod_count=$((pod_count + 1))
        (( pod_count <= KUBECTL_DIAGNOSTIC_MAX_PODS )) || break
        pod_name=${pod_ref#pod/}
        kubectl_validate_object_name "$pod_name" || continue
        [[ -n "$evidence_pod" ]] || evidence_pod="$pod_name"
        KUBECTL_REQUEST_TIMEOUT_SECONDS=3 KUBECTL_PROCESS_TIMEOUT_SECONDS=3 \
            kubectl_run_bounded -n "$namespace" describe pod "$pod_name" \
                2>&1 | head -c 65536 \
                > "$temporary/pod-${pod_count}.describe" || true
        KUBECTL_REQUEST_TIMEOUT_SECONDS=3 KUBECTL_PROCESS_TIMEOUT_SECONDS=3 \
            kubectl_run_bounded -n "$namespace" get events \
                --field-selector "involvedObject.name=$pod_name" \
                --sort-by=.metadata.creationTimestamp \
                2>&1 | head -c 131072 >> "$temporary/events.txt" || true
    done <<< "$pod_names"
    kubectl_capture_retained_resource_identities "$metadata_dir" "$run_id" \
        "$temporary/retained-resource-identities.tsv" || true
    if [[ -n "$evidence_pod" ]]; then
        kubectl_capture_control_state_evidence "$namespace" "$evidence_pod" \
            "$run_id" "$temporary/pvc-control-state.txt" || true
    fi
    local diagnostic_bytes
    diagnostic_bytes=$(_kubectl_local_tree_apparent_bytes "$temporary") || {
        rm -rf -- "$temporary"
        return 1
    }
    if [[ ! "$diagnostic_bytes" =~ ^[0-9]+$ \
            || "$diagnostic_bytes" -gt "$KUBECTL_DIAGNOSTIC_MAX_BYTES" ]]; then
        rm -rf -- "$temporary"
        return 1
    fi
    mv -- "$temporary" "$destination" || {
        rm -rf -- "$temporary"
        return 1
    }
    _kubectl_prune_diagnostic_bundles "$root" "$destination" || return 1
    printf '%s\n' "$destination"
}

_kubectl_prune_diagnostic_bundles() {
    local root="$1" retained="${2:-}" candidate candidate_mtime index
    local -a bundles=()
    [[ -z "$retained" ]] || bundles+=("$retained")
    while IFS=$'\t' read -r candidate_mtime candidate; do
        bundles+=("$candidate")
    done < <(
        for candidate in "$root"/*; do
            [[ "$candidate" != "$retained" ]] || continue
            [[ -d "$candidate" && ! -L "$candidate" ]] || continue
            candidate_mtime=$(_kubectl_local_path_mtime "$candidate") || exit 1
            printf '%s\t%s\n' "$candidate_mtime" "$candidate"
        done | LC_ALL=C sort -rn
    )
    for ((index = KUBECTL_DIAGNOSTIC_MAX_BUNDLES; \
            index < ${#bundles[@]}; index++)); do
        candidate="${bundles[$index]}"
        [[ -d "$candidate" && ! -L "$candidate" ]] || return 1
        rm -rf -- "$candidate" || return 1
    done
}

kubectl_capture_pvc_diagnostics() {
    local metadata_dir="$1" namespace="$2" helper_pod="$3" attempt_id="$4"
    local kind="$5" name="$6"
    local destination="" remote_run guard_script
    destination=$(kubectl_capture_resource_diagnostics "$metadata_dir" \
        failure "$namespace" "$kind" "$name" "$attempt_id") \
        || destination=""
    [[ -n "$destination" ]] || return 1
    # Every claim the selected segment's Pods mount, in pvc-<N>.yaml by its
    # position (a claim name of up to 253 bytes would not fit a file name),
    # with the claim's name on the first line. Diagnostics are supplemental:
    # a file that cannot be written is reported, never fatal.
    local claim index file
    local -a claims=()
    mapfile -t claims < <(kubectl_segment_claims "$(_kubectl_selected_segment)")
    [[ ${#claims[@]} -gt 0 ]] || claims=("${KUBECTL_PVC:-}")
    for index in "${!claims[@]}"; do
        claim="${claims[index]}"
        kubectl_validate_object_name "$claim" || continue
        file="$destination/pvc-$index.yaml"
        {
            printf '# claim: %s\n' "$claim"
            KUBECTL_REQUEST_TIMEOUT_SECONDS=5 KUBECTL_PROCESS_TIMEOUT_SECONDS=5 \
                kubectl_run_bounded -n "$namespace" get pvc "$claim" -o yaml \
                    2>&1 | head -c 524288
        } > "$file" || printf 'Warning: could not write the diagnostics of claim %s to %s\n' \
            "$claim" "$file" >&2
    done
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
            2>&1 | head -c 131072 > "$destination/pvc-filesystem.txt" \
        || printf 'Warning: could not write the claim capacity diagnostics to %s\n' \
            "$destination/pvc-filesystem.txt" >&2
    printf '%s\n' "$destination"
}

kubectl_classify_storage_diagnostics() {
    local output_variable="$1" diagnostic_path="$2"
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
        && -d "$diagnostic_path" && ! -L "$diagnostic_path" ]] || return 1
    local evidence="" evidence_path evidence_bytes
    for evidence_path in "$diagnostic_path"/*; do
        [[ -f "$evidence_path" && ! -L "$evidence_path" ]] || continue
        _kubectl_local_file_bytes "$evidence_path" evidence_bytes || continue
        [[ "$evidence_bytes" =~ ^[0-9]+$ && "$evidence_bytes" -lt 2097152 ]] \
            || continue
        evidence+=$'\n'"$(<"$evidence_path")"
    done
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

# Save a failed coordinator Job's log (its last 16 MiB) beside the attempt
# metadata as coordinator[-s<K>].log, which --status and --collect name as
# FAILED_LOG. A finished Pod's log no longer changes, so a saved copy is kept.
# Supplemental: a failure here is never fatal.
_kubectl_save_coordinator_log() {
    local metadata_dir="$1" namespace="$2" kind="$3" name="$4" destination temporary
    [[ "${kind,,}" == job ]] || return 0
    destination="$metadata_dir/coordinator$(_kubectl_selected_segment_suffix).log" \
        || return 0
    [[ ! -e "$destination" && ! -L "$destination" ]] || return 0
    # Created with the umask like other results; kubectl's own status decides.
    temporary="$metadata_dir/.coordinator-log.${BASHPID:-$$}.$RANDOM"
    if KUBECTL_REQUEST_TIMEOUT_SECONDS=60 KUBECTL_PROCESS_TIMEOUT_SECONDS=60 \
            kubectl_run_bounded -n "$namespace" logs "job/$name" --all-containers=true \
                2>/dev/null | tail -c 16777216 > "$temporary" \
            && [[ "${PIPESTATUS[0]}" -eq 0 && -s "$temporary" ]]; then
        mv -n -- "$temporary" "$destination" || true
    fi
    rm -f -- "$temporary" || true
}

kubectl_preserve_storage_failure_diagnostics() {
    local metadata_dir="$1" namespace="$2" helper_pod="$3" attempt_id="$4"
    local kind="$5" name="$6" expected_uid="$7"
    local diagnostic_path="" reason=""
    _kubectl_save_coordinator_log "$metadata_dir" "$namespace" "$kind" "$name"
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
    local key intent_file diagnostic_path="" identity captured=0 count claims
    local -a resource_keys=() lease_keys=()
    _kubectl_attempt_diagnostic_segment_count count "$kubernetes_dir" "$attempt_id"
    kubectl_attempt_claim_count claims "$kubernetes_dir" "$attempt_id" 2>/dev/null \
        || claims=1
    kubectl_workload_keys_for_segment_count resource_keys "$count" \
        && kubectl_lease_keys_for_claim_count lease_keys "$claims" || return 1
    _kubectl_append_segment_keys resource_keys "$count" forward transfer
    resource_keys+=("${lease_keys[@]}")
    local -A captured_identities=()
    for key in "${resource_keys[@]}"; do
        if kubectl_attempt_resource_exists "$kubernetes_dir" "$attempt_id" "$key"; then
            kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$key" \
                || continue
            identity="$KUBECTL_RESOURCE_NAMESPACE/$KUBECTL_RESOURCE_KIND/$KUBECTL_RESOURCE_NAME"
            [[ -z "${captured_identities[$identity]:-}" ]] || continue
            diagnostic_path=$(kubectl_capture_resource_diagnostics "$metadata_dir" \
                "$operation" "$KUBECTL_RESOURCE_NAMESPACE" \
                "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" "$attempt_id" \
                "$(_kubectl_resource_key_segment "$key")") \
                || diagnostic_path=""
            if [[ -n "$diagnostic_path" ]]; then
                printf '%s\n' "$diagnostic_path"
                captured=1
                captured_identities[$identity]=1
            fi
        fi
    done
    for key in "${resource_keys[@]}"; do
        intent_file="$metadata_dir/creation-intents/$key.sh"
        [[ -e "$intent_file" || -L "$intent_file" ]] || continue
        kubectl_attempt_load_creation_intent "$intent_file" || continue
        identity="$KUBECTL_INTENT_NAMESPACE/$KUBECTL_INTENT_KIND/$KUBECTL_INTENT_NAME"
        [[ -z "${captured_identities[$identity]:-}" ]] || continue
        diagnostic_path=$(kubectl_capture_resource_diagnostics "$metadata_dir" \
            "$operation" "$KUBECTL_INTENT_NAMESPACE" "$KUBECTL_INTENT_KIND" \
            "$KUBECTL_INTENT_NAME" "$attempt_id" "$(_kubectl_resource_key_segment "$key")") \
            || diagnostic_path=""
        if [[ -n "$diagnostic_path" ]]; then
            printf '%s\n' "$diagnostic_path"
            captured=1
            captured_identities[$identity]=1
        fi
    done
    [[ "$captured" -eq 1 ]]
}

kubectl_preserve_attempt_diagnostics() {
    local kubernetes_dir="$1" attempt_id="$2" operation="$3"
    local diagnostic_paths="" diagnostic_path=""
    diagnostic_paths=$(kubectl_capture_attempt_diagnostics "$kubernetes_dir" \
        "$attempt_id" "$operation") || diagnostic_paths=""
    if [[ -n "$diagnostic_paths" ]]; then
        while IFS= read -r diagnostic_path; do
            [[ -n "$diagnostic_path" ]] || continue
            printf 'Kubernetes failure diagnostics: %s\n' "$diagnostic_path" >&2
        done <<< "$diagnostic_paths"
    else
        printf 'Warning: automatic Kubernetes diagnostic capture failed.\n' >&2
        printf 'Inspect retained resources with:\n' >&2
        printf '  kubectl -n %q get job,daemonset,pod,networkpolicy -l %q -o wide\n' \
            "${KUBECTL_NAMESPACE:-unknown}" \
            "storage-scale-test.nvidia.com/run=$attempt_id" >&2
        printf 'The last proven local and PVC state was retained for retry.\n' >&2
    fi
    return 0
}

# kubectl_classify_readiness_failure OUT NAMESPACE RUN_ID [SEGMENT]: SEGMENT
# selects the Pods (see kubectl_attempt_pod_selector).
kubectl_classify_readiness_failure() {
    local output_variable="$1" namespace="$2" run_id="$3"
    local target="${4:-${KUBECTL_ACTIVE_SEGMENT:-0}}" pod_selector
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
        && "$run_id" =~ ^[0-9a-f]{8}$ ]] \
        && kubectl_validate_namespace_name "$namespace" || return 1
    pod_selector=$(kubectl_attempt_pod_selector "$run_id" "$target") || return 1
    local evidence="" pod_names="" pod_ref pod_name pod_events pod_count=0
    evidence=$(KUBECTL_OBSERVATION_ATTEMPTS=1 KUBECTL_REQUEST_TIMEOUT_SECONDS=3 \
        KUBECTL_PROCESS_TIMEOUT_SECONDS=3 \
        kubectl_run_observational -n "$namespace" get pods \
            -l "$pod_selector" \
            -o 'jsonpath={range .items[*]}{range .status.containerStatuses[*]}{.state.waiting.reason}{"\n"}{end}{range .status.conditions[?(@.type=="PodScheduled")]}{.reason}{"\n"}{.message}{"\n"}{end}{end}' \
            2>/dev/null | head -c 524288) || evidence=""
    pod_names=$(KUBECTL_OBSERVATION_ATTEMPTS=1 KUBECTL_REQUEST_TIMEOUT_SECONDS=3 \
        KUBECTL_PROCESS_TIMEOUT_SECONDS=3 \
        kubectl_run_observational -n "$namespace" get pods \
            -l "$pod_selector" -o name 2>/dev/null \
            | head -c 65536) \
        || pod_names=""
    while IFS= read -r pod_ref; do
        [[ "$pod_ref" == pod/* ]] || continue
        pod_name=${pod_ref#pod/}
        kubectl_validate_object_name "$pod_name" || continue
        pod_count=$((pod_count + 1))
        (( pod_count <= KUBECTL_DIAGNOSTIC_MAX_PODS )) || break
        pod_events=$(KUBECTL_OBSERVATION_ATTEMPTS=1 KUBECTL_REQUEST_TIMEOUT_SECONDS=3 \
            KUBECTL_PROCESS_TIMEOUT_SECONDS=3 \
            kubectl_run_observational -n "$namespace" get events \
                --field-selector "involvedObject.name=$pod_name" \
                -o 'jsonpath={range .items[*]}{.reason}{"\t"}{.message}{"\n"}{end}' \
                2>/dev/null | head -c 131072) || pod_events=""
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
    local attempts attempt create_output create_rc _kubectl_present_object_uid=""
    attempts=$(_kubectl_create_attempt_limit) || return 1
    # The intent is written once, before the first request. Every retry below
    # re-sends the same deterministic name, manifest, and ownership nonce.
    if [[ -n "$kubernetes_dir" || -n "$lock_fd" || -n "$resource_key" ]]; then
        [[ -n "$kubernetes_dir" && -n "$lock_fd" && -n "$resource_key" ]] || return 1
        kubectl_attempt_write_creation_intent "$kubernetes_dir" "$lock_fd" \
            "$run_id" "$resource_key" "$kind" "$name" "$namespace" "$nonce" \
            || return 1
    fi
    local intent_file=""
    [[ -z "$kubernetes_dir" ]] \
        || intent_file="$kubernetes_dir/attempts/$run_id/creation-intents/$resource_key.sh"
    for ((attempt = 1; ; attempt++)); do
        create_rc=0
        create_output=$(printf '%s' "$manifest" \
            | kubectl_run_bounded -n "$namespace" create -f - 2>&1) || create_rc=$?
        [[ "$create_rc" -ne 0 ]] || break
        printf 'Warning: create response was not authoritative: %s\n' \
            "$create_output" >&2
        # AlreadyExists and permanent failures get the single exact identity
        # verification below; neither is retried.
        _kubectl_create_failure_is_retryable "$create_rc" "$create_output" || break
        # Observation failure is not absence: never re-create on that basis.
        _kubectl_observe_object_uid _kubectl_present_object_uid "$kind" "$name" \
            "$namespace" || return 1
        [[ -z "$_kubectl_present_object_uid" ]] || break
        _kubectl_prepare_create_retry "$attempt" "$attempts" "$kind" "$name" \
            "$namespace" "$intent_file" || return 1
    done
    local _kubectl_created_object_uid
    _kubectl_created_object_uid=$(kubectl_verify_object_identity \
        "$kind" "$name" "$namespace" "$nonce" "$run_id" \
        "$_kubectl_present_object_uid") || return 1
    printf -v "$output_variable" '%s' "$_kubectl_created_object_uid"
    return 0
}

_kubectl_create_attempt_limit() {
    local attempts="${KUBECTL_CREATE_ATTEMPTS:-$KUBECTL_CREATE_ATTEMPTS_DEFAULT}"
    [[ "$attempts" =~ ^[1-9][0-9]*$ ]] || {
        echo "Error: KUBECTL_CREATE_ATTEMPTS must be a positive integer" >&2
        return 1
    }
    printf '%s\n' "$attempts"
}

# Only a transport-level failure with no API verdict may be retried. Every
# owned name is deterministic and the same manifest is re-sent, so the API
# server admits at most one object; AlreadyExists is resolved by identity.
_kubectl_create_failure_is_retryable() {
    local rc="$1" output="$2"
    ! grep -Eqi 'AlreadyExists|already exists' <<< "$output" \
        && _kubectl_observation_failure_is_transient "$rc" "$output"
}

_kubectl_observe_object_uid() {
    local output_variable="$1" kind="$2" name="$3" namespace="$4"
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    local _kubectl_observed_object_uid
    _kubectl_observed_object_uid=$(kubectl_run_observational -n "$namespace" \
        get "$kind" "$name" --ignore-not-found \
        -o 'jsonpath={.metadata.uid}') || return 1
    printf -v "$output_variable" '%s' "$_kubectl_observed_object_uid"
}

_kubectl_prepare_create_retry() {
    local attempt="$1" attempts="$2" kind="$3" name="$4" namespace="$5"
    local intent_file="$6"
    if [[ "$attempt" -ge "$attempts" ]]; then
        printf 'Error: create of %s %s/%s failed transiently %d times; its creation intent is retained\n' \
            "$kind" "$namespace" "$name" "$attempts" >&2
        return 1
    fi
    local backoff="${KUBECTL_OBSERVATION_BACKOFF_SECONDS:-$KUBECTL_OBSERVATION_BACKOFF_SECONDS_DEFAULT}"
    [[ "$backoff" =~ ^[0-9]+$ ]] || return 1
    printf 'Warning: %s %s/%s is absent after a transient create failure; retrying create of %s/%s (attempt %d/%d)\n' \
        "$kind" "$namespace" "$name" "$kind" "$name" "$((attempt + 1))" "$attempts" >&2
    local delay=$((backoff * attempt))
    (( backoff == 0 )) || delay=$((delay + RANDOM % (backoff + 1)))
    sleep "$delay"
    # The ambiguity horizon is measured from the intent mtime. Refresh it
    # before every re-send so the horizon covers the latest in-flight create.
    # Content is unchanged, so intent and tombstone comparisons still hold.
    if [[ -n "$intent_file" ]]; then
        [[ -f "$intent_file" && ! -L "$intent_file" ]] \
            && touch -c -- "$intent_file" || return 1
    fi
}

kubectl_render_pvc_lease() {
    local name="$1" namespace="$2" attempt_id="$3" nonce="$4"
    kubectl_validate_object_name "$name" \
        && kubectl_validate_namespace_name "$namespace" \
        && [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && "$nonce" =~ ^[0-9a-f]{32}$ ]] \
        && kubectl_validate_uid "${KUBECTL_NAMESPACE_UID:-}" \
        && kubectl_validate_uid "${KUBECTL_PV_UID:-}" \
        && kubectl_validate_uid "${KUBECTL_PVC_UID:-}" || return 1
    cat <<EOF
apiVersion: coordination.k8s.io/v1
kind: Lease
metadata:
  name: "$name"
  namespace: "$namespace"
  labels:
    app.kubernetes.io/name: storage-scale-test
    app.kubernetes.io/component: pvc-lock
    app.kubernetes.io/managed-by: storage-scale-test
    storage-scale-test.nvidia.com/run: "$attempt_id"
  annotations:
    storage-scale-test.nvidia.com/ownership: "$nonce"
    storage-scale-test.nvidia.com/namespace-uid: "$KUBECTL_NAMESPACE_UID"
    storage-scale-test.nvidia.com/pv-uid: "$KUBECTL_PV_UID"
    storage-scale-test.nvidia.com/pvc-uid: "$KUBECTL_PVC_UID"
spec:
  holderIdentity: "$attempt_id/$nonce"
EOF
}

kubectl_observe_pvc_lease() {
    local output_variable="$1" namespace="$2" name="$3"
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
        && kubectl_validate_namespace_name "$namespace" \
        && kubectl_validate_object_name "$name" || return 1
    # Keep the implementation variable distinct from every valid caller output
    # name. Bash locals are dynamically scoped, so a local named "observed"
    # would intercept printf -v when callers also request that name.
    local jsonpath _kubectl_observed_pvc_lease
    jsonpath='{.metadata.uid}{"\t"}{.metadata.annotations.storage-scale-test\.nvidia\.com/ownership}{"\t"}{.metadata.labels.storage-scale-test\.nvidia\.com/run}{"\t"}{.metadata.annotations.storage-scale-test\.nvidia\.com/namespace-uid}{"\t"}{.metadata.annotations.storage-scale-test\.nvidia\.com/pv-uid}{"\t"}{.metadata.annotations.storage-scale-test\.nvidia\.com/pvc-uid}{"\t"}{.spec.holderIdentity}'
    _kubectl_observed_pvc_lease=$(kubectl_run_observational -n "$namespace" \
        get Lease "$name" \
        --ignore-not-found -o "jsonpath=$jsonpath") || return 1
    printf -v "$output_variable" '%s' "$_kubectl_observed_pvc_lease"
}

kubectl_verify_pvc_lease() {
    local namespace="$1" name="$2" attempt_id="$3" nonce="$4" expected_uid="${5:-}"
    local observed uid observed_nonce observed_attempt namespace_uid pv_uid pvc_uid holder
    kubectl_observe_pvc_lease observed "$namespace" "$name" || return 1
    [[ -n "$observed" ]] || return 1
    IFS=$'\t' read -r uid observed_nonce observed_attempt namespace_uid pv_uid pvc_uid \
        holder <<< "$observed"
    if [[ "$observed_nonce" != "$nonce" || "$observed_attempt" != "$attempt_id" \
            || "$namespace_uid" != "$KUBECTL_NAMESPACE_UID" \
            || "$pv_uid" != "$KUBECTL_PV_UID" || "$pvc_uid" != "$KUBECTL_PVC_UID" \
            || "$holder" != "$attempt_id/$nonce" \
            || ( -n "$expected_uid" && "$uid" != "$expected_uid" ) ]]; then
        KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
            verify-lease pvc-lease IDENTITY_MISMATCH \
            "inspect the retained Lease and original attempt; do not delete it by name" \
            unknown Lease "$name" "$namespace" "$expected_uid" "$uid" "" || true
        return 1
    fi
    kubectl_validate_uid "$uid" || return 1
    printf '%s\n' "$uid"
}

kubectl_pvc_lease_observation_matches() {
    local observed="$1" attempt_id="$2" nonce="$3" expected_uid="${4:-}"
    local uid observed_nonce observed_attempt namespace_uid pv_uid pvc_uid holder
    [[ -n "$observed" ]] || return 1
    IFS=$'\t' read -r uid observed_nonce observed_attempt namespace_uid pv_uid pvc_uid \
        holder <<< "$observed"
    [[ "$observed_nonce" == "$nonce" && "$observed_attempt" == "$attempt_id" \
        && "$namespace_uid" == "$KUBECTL_NAMESPACE_UID" \
        && "$pv_uid" == "$KUBECTL_PV_UID" && "$pvc_uid" == "$KUBECTL_PVC_UID" \
        && "$holder" == "$attempt_id/$nonce" \
        && ( -z "$expected_uid" || "$uid" == "$expected_uid" ) ]] \
        && kubectl_validate_uid "$uid"
}

kubectl_acquire_pvc_lease() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_attempt_load_identity "$kubernetes_dir/attempts/$attempt_id" || return 1
    local name="$KUBECTL_PVC_LEASE_NAME" namespace="$KUBECTL_NAMESPACE"
    local nonce="$KUBECTL_OWNERSHIP_NONCE" manifest output="" create_rc=0 uid key
    key=$(_kubectl_selected_claim_lease_key) || return 1
    kubectl_attempt_write_creation_intent "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" "$key" Lease "$name" "$namespace" "$nonce" || return 1
    manifest=$(kubectl_render_pvc_lease "$name" "$namespace" "$attempt_id" "$nonce") \
        || return 1
    output=$(printf '%s' "$manifest" | kubectl_run_bounded -n "$namespace" \
        create -f - 2>&1) || create_rc=$?
    local observed="" foreign_uid="" foreign_attempt=""
    kubectl_observe_pvc_lease observed "$namespace" "$name" || return 1
    if kubectl_pvc_lease_observation_matches "$observed" "$attempt_id" "$nonce"; then
        IFS=$'\t' read -r uid _ <<< "$observed"
        kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "$key" Lease "$name" "$namespace" "$uid" "$nonce" || return 1
        kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" "$key" || return 1
        return 0
    fi
    if [[ -n "$observed" ]]; then
        IFS=$'\t' read -r foreign_uid _ foreign_attempt _ <<< "$observed"
        kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" "$key" || return 1
        printf 'Error: PVC %s/%s is reserved by Kubernetes attempt %s (Lease %s, UID %s)\n' \
            "$namespace" "$KUBECTL_PVC" "${foreign_attempt:-unknown}" "$name" \
            "${foreign_uid:-unknown}" >&2
        KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
            submit pvc-lease INVALID_LIFECYCLE_OPERATION \
            "use the owning attempt result directory to run --status, --cancel, or --collect" \
            unknown Lease "$name" "$namespace" "" "$foreign_uid" "" || true
        return 1
    fi
    [[ "$create_rc" -eq 0 ]] || printf 'Error: failed to create PVC Lease: %s\n' "$output" >&2
    return 1
}

kubectl_release_pvc_lease() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    local key
    key=$(_kubectl_selected_claim_lease_key) || return 1
    kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "release-$key" \
        && return 0
    local intent="$kubernetes_dir/attempts/$attempt_id/creation-intents/$key.sh"
    if ! kubectl_attempt_resource_exists "$kubernetes_dir" "$attempt_id" "$key"; then
        if [[ -e "$intent" || -L "$intent" ]]; then
            kubectl_attempt_load_identity "$kubernetes_dir/attempts/$attempt_id" || return 1
            kubectl_attempt_load_creation_intent "$intent" || return 1
            [[ "$KUBECTL_INTENT_KIND" == Lease \
                && "$KUBECTL_INTENT_NAME" == "$KUBECTL_PVC_LEASE_NAME" \
                && "$KUBECTL_INTENT_NAMESPACE" == "$KUBECTL_NAMESPACE" \
                && "$KUBECTL_INTENT_NONCE" == "$KUBECTL_OWNERSHIP_NONCE" ]] \
                || return 1
            # Lease creation has the same ambiguous-success window as every
            # other API object. Use the common bounded observation path so an
            # accepted but initially invisible Lease is deleted by exact UID,
            # or its possible late identity is retained for diagnosis.
            kubectl_cleanup_creation_intent "$kubernetes_dir" "$lock_fd" \
                "$attempt_id" "$key" || return 1
        fi
    elif [[ -e "$intent" || -L "$intent" ]]; then
        # Acquisition publishes the exact UID before removing its create
        # intent. A crash in that interval leaves both records. The journal is
        # authoritative, but clear the redundant intent only after proving it
        # describes that same owned Lease.
        kubectl_attempt_load_identity "$kubernetes_dir/attempts/$attempt_id" || return 1
        kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$key" \
            || return 1
        [[ "$KUBECTL_RESOURCE_KIND" == Lease \
            && "$KUBECTL_RESOURCE_NAME" == "$KUBECTL_PVC_LEASE_NAME" \
            && "$KUBECTL_RESOURCE_NAMESPACE" == "$KUBECTL_NAMESPACE" \
            && "$KUBECTL_RESOURCE_NONCE" == "$KUBECTL_OWNERSHIP_NONCE" ]] \
            || return 1
        kubectl_attempt_load_creation_intent "$intent" || return 1
        [[ "$KUBECTL_INTENT_KIND" == "$KUBECTL_RESOURCE_KIND" \
            && "$KUBECTL_INTENT_NAME" == "$KUBECTL_RESOURCE_NAME" \
            && "$KUBECTL_INTENT_NAMESPACE" == "$KUBECTL_RESOURCE_NAMESPACE" \
            && "$KUBECTL_INTENT_NONCE" == "$KUBECTL_RESOURCE_NONCE" ]] \
            || return 1
        kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" "$key" || return 1
    fi
    if ! kubectl_attempt_resource_exists "$kubernetes_dir" "$attempt_id" "$key"; then
        kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "release-$key"
        return
    fi
    kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$key" || return 1
    [[ "$KUBECTL_RESOURCE_KIND" == Lease ]] || return 1
    local uid="$KUBECTL_RESOURCE_UID" name="$KUBECTL_RESOURCE_NAME"
    local namespace="$KUBECTL_RESOURCE_NAMESPACE" nonce="$KUBECTL_RESOURCE_NONCE"
    local observed=""
    if ! kubectl_observe_pvc_lease observed "$namespace" "$name"; then
        return 1
    fi
    if [[ -n "$observed" ]]; then
        kubectl_verify_pvc_lease "$namespace" "$name" "$attempt_id" "$nonce" "$uid" \
            >/dev/null || return 1
        local delete_options
        delete_options=$(printf '{"apiVersion":"v1","kind":"DeleteOptions","preconditions":{"uid":"%s"}}' \
            "$uid") || return 1
        printf '%s' "$delete_options" | KUBECTL_REQUEST_TIMEOUT_SECONDS=30 \
            KUBECTL_PROCESS_TIMEOUT_SECONDS=45 kubectl_run_bounded -n "$namespace" \
                delete --raw="/apis/coordination.k8s.io/v1/namespaces/$namespace/leases/$name" \
                -f - >/dev/null 2>&1 || true
    fi
    kubectl_observe_pvc_lease observed "$namespace" "$name" || return 1
    # The deterministic name can be reacquired immediately after our
    # UID-preconditioned delete. Only the exact old UID must be absent.
    [[ -z "$observed" || "${observed%%$'\t'*}" != "$uid" ]] || return 1
    kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        "release-$key"
}

kubectl_verify_journaled_pvc_lease() {
    local kubernetes_dir="$1" attempt_id="$2"
    kubectl_attempt_load_identity "$kubernetes_dir/attempts/$attempt_id" || return 1
    local observed="" key
    key=$(_kubectl_selected_claim_lease_key) || return 1
    if kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "release-$key"; then
        kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$key" \
            || return 1
        kubectl_observe_pvc_lease observed "$KUBECTL_NAMESPACE" \
            "$KUBECTL_PVC_LEASE_NAME" || return 1
        # A later attempt may already own the deterministic Lease name. The
        # released journaled object is absent when the name is empty or its
        # current UID differs; never reject or delete that replacement.
        [[ -z "$observed" || "${observed%%$'\t'*}" != "$KUBECTL_RESOURCE_UID" ]]
        return
    fi
    if ! kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$key"; then
        KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
            verify-lease pvc-lease LEDGER_INCONSISTENT \
            "inspect the missing Lease journal; do not alter resources by name" \
            unknown Lease "$KUBECTL_PVC_LEASE_NAME" "$KUBECTL_NAMESPACE" \
            "" "" "" || true
        return 1
    fi
    [[ "$KUBECTL_RESOURCE_KIND" == Lease \
        && "$KUBECTL_RESOURCE_NAME" == "$KUBECTL_PVC_LEASE_NAME" ]] || return 1
    kubectl_verify_pvc_lease "$KUBECTL_RESOURCE_NAMESPACE" \
        "$KUBECTL_RESOURCE_NAME" "$attempt_id" "$KUBECTL_RESOURCE_NONCE" \
        "$KUBECTL_RESOURCE_UID" >/dev/null
}

# Releasing a PVC Lease is the last external action of collection: the local
# publication verifies, every segment's run tree is released, every other
# owned object is deleted, and every later claim's Lease is already released.
kubectl_collection_cleanup_precedes_lease_release() {
    local kubernetes_dir="$1" attempt_id="$2"
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id" count index selected suffix
    selected="${KUBECTL_CLAIM_SELECTION:-${KUBECTL_ACTIVE_CLAIM:-0}}"
    kubectl_attempt_load_metadata "$metadata_dir" || return 1
    [[ "$KUBECTL_LIFECYCLE_STATE" == COLLECTION_IN_PROGRESS ]] || return 1
    count="${KUBECTL_SEGMENT_COUNT:-1}"
    _kubectl_collection_is_published "$kubernetes_dir" "$attempt_id" "$count" \
        || return 1
    for ((index = 0; index < count; index++)); do
        suffix=$(kubectl_segment_suffix "$index") || return 1
        kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" \
            "release-remote-run$suffix" || return 1
    done
    for ((index = selected + 1; index < ${KUBECTL_CLAIM_COUNT:-1}; index++)); do
        kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" \
            "release-$(kubectl_claim_lease_key "$index")" || return 1
    done
    local intent resource key
    for intent in "$metadata_dir/creation-intents"/*.sh; do
        [[ -e "$intent" || -L "$intent" ]] || continue
        [[ -f "$intent" && ! -L "$intent" ]] || return 1
        kubectl_resource_key_is_lease "$(basename "$intent" .sh)" || return 1
    done
    for resource in "$metadata_dir/resources"/*.sh; do
        [[ -e "$resource" || -L "$resource" ]] || continue
        [[ -f "$resource" && ! -L "$resource" ]] || return 1
        key=$(basename "$resource" .sh)
        kubectl_resource_key_is_lease "$key" && continue
        [[ "$key" =~ ^[a-z0-9-]+$ ]] || return 1
        kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "delete-$key" \
            || return 1
    done
}

kubectl_reconcile_pvc_lease_release_after_collection_cleanup() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    local key
    key=$(_kubectl_selected_claim_lease_key) || return 1
    if ! kubectl_attempt_load_identity "$kubernetes_dir/attempts/$attempt_id"; then
        KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
            collect pvc-lease LEDGER_INCONSISTENT \
            "inspect the missing or corrupt attempt identity; do not alter resources by name" \
            unknown || true
        return 1
    fi
    if ! kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$key"; then
        KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
            collect pvc-lease LEDGER_INCONSISTENT \
            "inspect the missing or corrupt Lease journal; do not alter resources by name" \
            unknown Lease "$KUBECTL_PVC_LEASE_NAME" "$KUBECTL_NAMESPACE" \
            "" "" "" || true
        return 1
    fi
    if [[ "$KUBECTL_RESOURCE_KIND" != Lease \
            || "$KUBECTL_RESOURCE_NAME" != "$KUBECTL_PVC_LEASE_NAME" \
            || "$KUBECTL_RESOURCE_NAMESPACE" != "$KUBECTL_NAMESPACE" ]]; then
        KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
            collect pvc-lease LEDGER_INCONSISTENT \
            "inspect the conflicting Lease journal; do not alter resources by name" \
            unknown "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" \
            "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_UID" "" "" || true
        return 1
    fi
    local observed=""
    if ! kubectl_observe_pvc_lease observed "$KUBECTL_RESOURCE_NAMESPACE" \
            "$KUBECTL_RESOURCE_NAME"; then
        KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
            collect pvc-lease API_UNAVAILABLE \
            "restore Kubernetes API access and retry --collect" unknown \
            Lease "$KUBECTL_RESOURCE_NAME" "$KUBECTL_RESOURCE_NAMESPACE" \
            "$KUBECTL_RESOURCE_UID" "" "" || true
        return 1
    fi
    local observed_uid="${observed%%$'\t'*}"
    if [[ -n "$observed" && "$observed_uid" == "$KUBECTL_RESOURCE_UID" ]]; then
        if kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" \
                "release-$key"; then
            KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
                collect pvc-lease LEDGER_INCONSISTENT \
                "inspect the released Lease journal and live exact UID before retrying" \
                unknown Lease "$KUBECTL_RESOURCE_NAME" \
                "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_UID" \
                "$observed_uid" "" || true
            return 1
        fi
        if kubectl_pvc_lease_observation_matches "$observed" "$attempt_id" \
                "$KUBECTL_RESOURCE_NONCE" "$KUBECTL_RESOURCE_UID"; then
            return 0
        fi
        KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
            collect pvc-lease IDENTITY_MISMATCH \
            "inspect the retained Lease and original attempt; do not delete it by name" \
            unknown Lease "$KUBECTL_RESOURCE_NAME" \
            "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_UID" \
            "$observed_uid" "" || true
        return 1
    fi
    # An empty name or a replacement UID proves the exact journaled Lease is
    # gone. Reconcile only after local publication and every preceding exact
    # cleanup marker prove that releasing PVC ownership was the final action.
    if ! kubectl_collection_cleanup_precedes_lease_release "$kubernetes_dir" \
            "$attempt_id"; then
        KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
            collect pvc-lease LEDGER_INCONSISTENT \
            "inspect incomplete cleanup evidence before retrying --collect" \
            unknown Lease "$KUBECTL_RESOURCE_NAME" \
            "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_UID" \
            "$observed_uid" "" || true
        return 1
    fi
    kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        "release-$key"
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
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] \
        && kubectl_validate_saved_control_layout || return 1
    printf '%s/runs/%s\n' "$KUBECTL_CONTROL_ROOT" "$attempt_id"
}

# This fragment is embedded in every remote control-tree mutation.  The
# lexical check prevents argument substitution, while the realpath checks
# detect a reserved run or any of its parents being replaced by a symlink
# between lifecycle operations.  Keep it self-contained: it runs in the
# workload image, not in the submitting shell. The run tree lives on one
# claim, mounted at /mnt/storage-scale-test/<claim>; its test root must
# resolve inside that claim's mount.
kubectl_remote_tree_guard_script() {
    cat <<'EOF'
run=$1
attempt=$2
mounts=/mnt/storage-scale-test
run_suffix=/runs/$attempt
control_suffix=/.storage-scale-test
[[ "$run" == *"$run_suffix" ]] || exit 1
control_root=${run%"$run_suffix"}
[[ "$control_root" == *"$control_suffix" ]] || exit 1
test_root=${control_root%"$control_suffix"}
root=$control_root
[[ "$attempt" =~ ^[0-9a-f]{8}$ && "$test_root" == "$mounts/"*/* \
    && "$root" == "$test_root/.storage-scale-test" \
    && "$run" == "$root/runs/$attempt" ]] || exit 1
claim=${test_root#"$mounts/"}
claim=${claim%%/*}
[[ "$claim" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$ ]] || exit 1
mount=$mounts/$claim
mount_real=$(realpath -e -- "$mount") || exit 1
test_real=$(realpath -e -- "$test_root") || exit 1
root_real=$(realpath -e -- "$root") || exit 1
case "$test_real" in "$mount_real"/*) ;; *) exit 1 ;; esac
[[ "$root_real" == "$test_real/.storage-scale-test" ]] || exit 1
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

# The creation ambiguity horizon bounds how long after the latest create
# request (the creation intent mtime) a lost response may still yield a
# visible object: the longer of the 30-second floor and the effective kubectl
# process deadline that bounded that create.
kubectl_creation_ambiguity_seconds() {
    local process_timeout="${KUBECTL_PROCESS_TIMEOUT_SECONDS:-$KUBECTL_PROCESS_TIMEOUT_SECONDS_DEFAULT}"
    [[ "$process_timeout" =~ ^[1-9][0-9]*$ ]] || {
        echo "Error: KUBECTL_PROCESS_TIMEOUT_SECONDS must be a positive integer" >&2
        return 1
    }
    local horizon="$KUBECTL_CREATION_AMBIGUITY_SECONDS_DEFAULT"
    (( process_timeout <= horizon )) || horizon="$process_timeout"
    printf '%s\n' "$horizon"
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
        intent_epoch=$(_kubectl_local_path_mtime "$intent_file") || return 1
        now=$(date +%s) || return 1
        [[ "$intent_epoch" =~ ^[0-9]+$ && "$now" =~ ^[0-9]+$ \
            && "$now" -ge "$intent_epoch" ]] || return 1
        local horizon
        horizon=$(kubectl_creation_ambiguity_seconds) || return 1
        remaining=$((horizon - now + intent_epoch))
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
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" mapped_read_from="${4:-}"
    shift 4
    local -a control_roots=("$@")
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_validate_runtime_configuration || return 1
    # shellcheck disable=SC2034  # Only the validation matters.
    local mapped_root
    [[ -z "$mapped_read_from" ]] \
        || kubectl_mapped_mount_root "$mapped_read_from" mapped_root || return 1
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id"
    kubectl_attempt_load_identity "$metadata_dir" || return 1
    # One control root per segment, each on one of that segment's claims.
    local -a KUBECTL_SEGMENT_CONTROL_ROOTS=("${control_roots[@]}")
    _kubectl_validate_segment_control_roots || {
        echo "Error: invalid Kubernetes control roots for this attempt's segments" >&2
        return 1
    }
    _kubectl_apply_segment_control_layout 0 || return 1
    local config_file="$metadata_dir/configuration.sh"
    [[ ! -e "$config_file" && ! -L "$config_file" ]] || return 1
    local tmp="$config_file.tmp.${BASHPID:-$$}.$RANDOM" root
    {
        printf '# Trusted storage-scale-test Kubernetes attempt configuration.\n'
        printf 'KUBECTL_NODE_SELECTOR=%q\n' "$KUBECTL_NODE_SELECTOR"
        printf 'KUBECTL_ELBENCHO_IMAGE=%q\n' "$KUBECTL_ELBENCHO_IMAGE"
        printf 'KUBECTL_IMAGE_PULL_POLICY=%q\n' "$KUBECTL_IMAGE_PULL_POLICY"
        printf 'KUBECTL_RUN_AS_USER=%q\n' "$KUBECTL_RUN_AS_USER"
        printf 'KUBECTL_RUN_AS_GROUP=%q\n' "$KUBECTL_RUN_AS_GROUP"
        printf 'KUBECTL_MAPPED_READ_FROM=%q\n' "$mapped_read_from"
        printf 'KUBECTL_CONTROL_LOGICAL_ROOT=%q\n' "$KUBECTL_CONTROL_LOGICAL_ROOT"
        printf 'KUBECTL_CONTROL_TEST_ROOT=%q\n' "$KUBECTL_CONTROL_TEST_ROOT"
        printf 'KUBECTL_CONTROL_ROOT=%q\n' "$KUBECTL_CONTROL_ROOT"
        printf 'declare -ga KUBECTL_SEGMENT_CONTROL_ROOTS=('
        for root in "${control_roots[@]}"; do
            printf ' %q' "$root"
        done
        printf ' )\n'
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

# Every segment of the loaded identity has one normalized control root
# (KUBECTL_SEGMENT_CONTROL_ROOTS) in its own namespace, on one of its claims.
_kubectl_validate_segment_control_roots() {
    local index root normalized namespace claim path
    [[ "$(declare -p KUBECTL_SEGMENT_CONTROL_ROOTS 2>/dev/null)" == "declare -"*a* ]] \
        && _kubectl_array_is_dense KUBECTL_SEGMENT_CONTROL_ROOTS \
            "${KUBECTL_SEGMENT_COUNT:-0}" || return 1
    for index in "${!KUBECTL_SEGMENT_CONTROL_ROOTS[@]}"; do
        root="${KUBECTL_SEGMENT_CONTROL_ROOTS[index]}"
        [[ "$root" == */*:* ]] \
            && kubectl_normalize_logical_path "$root" normalized 2>/dev/null \
            && [[ "$normalized" == "$root" ]] || return 1
        _kubectl_logical_parts "$root" namespace claim path
        [[ "$namespace" == "${KUBECTL_SEGMENT_NAMESPACES[index]}" ]] \
            && kubectl_segment_claims "$index" | grep -Fqx -- "$claim" || return 1
    done
}

# The loaded control layout (KUBECTL_CONTROL_LOGICAL_ROOT, a normalized
# "NAMESPACE/CLAIM:PATH", its Pod path and the reserved directory below it).
kubectl_validate_saved_control_layout() {
    local logical="${KUBECTL_CONTROL_LOGICAL_ROOT:-}"
    local test_root="${KUBECTL_CONTROL_TEST_ROOT:-}"
    local control_root="${KUBECTL_CONTROL_ROOT:-}"
    local normalized_path namespace claim path
    [[ "$logical" == */*:* ]] || return 1
    kubectl_normalize_logical_path "$logical" normalized_path || return 1
    _kubectl_logical_parts "$logical" namespace claim path
    [[ "$normalized_path" == "$logical" \
        && "$test_root" == "$KUBECTL_SWEEP_MOUNT_ROOT/$claim/$path" \
        && "$control_root" == "$test_root/$KUBECTL_SWEEP_RESERVED_ROOT" ]]
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
        printf '        "%s": "%s"\n' "$key" "$value"
    done
}

kubectl_render_node_affinity_values() {
    local node node_names=""
    for node in "$@"; do
        kubectl_validate_object_name "$node" || return 1
        node_names+="                      - \"$node\""$'\n'
    done
    [[ -n "$node_names" ]] || return 1
    printf '%s' "$node_names"
}

kubectl_render_test_root_arguments() {
    local path
    local -a roots=("$@")
    [[ ${#roots[@]} -gt 0 ]] || return 1
    mapfile -t roots < <(printf '%s\n' "${roots[@]}" | LC_ALL=C sort -u)
    for path in "${roots[@]}"; do
        _kubectl_validate_placeholder TEST_ROOT "$path" || return 1
        printf '                "%s"\n' "$path"
    done
}

_kubectl_validate_multiline_placeholder() {
    local token="$1" value="$2" line
    case "$token" in
        NODE_SELECTOR_BLOCK)
            local selector_key selector_value
            while IFS= read -r line; do
                [[ "$line" =~ ^[[:space:]]{8}\"([A-Za-z0-9./_-]+)\":[[:space:]]\"([A-Za-z0-9._-]*)\"$ ]] \
                    || return 1
                selector_key="${BASH_REMATCH[1]}"
                selector_value="${BASH_REMATCH[2]}"
                kubectl_validate_label_key "$selector_key" \
                    && kubectl_validate_label_value "$selector_value" || return 1
            done <<< "$value"
            ;;
        NODE_AFFINITY_VALUES)
            while IFS= read -r line; do
                [[ "$line" =~ ^[[:space:]]{22}-[[:space:]]\"([a-z0-9]([a-z0-9.-]*[a-z0-9])?)\"$ ]] \
                    && kubectl_validate_object_name "${BASH_REMATCH[1]}" || return 1
            done <<< "$value"
            ;;
        TEST_ROOT_ARGUMENTS)
            while IFS= read -r line; do
                [[ "$line" =~ ^[[:space:]]{16}\"/mnt/storage-scale-test/([A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+\"$ ]] \
                    || return 1
            done <<< "$value"
            ;;
        SEQUENCER_SCRIPT)
            # Only the checked-in sequencer, indented by its own renderer.
            local expected_script
            expected_script=$(kubectl_render_sequencer_script) || return 1
            [[ "$value" == "$expected_script" ]]
            ;;
        SEQUENCER_ARGUMENTS)
            local expected_arguments
            local -a arguments=()
            while IFS= read -r line; do
                [[ "$line" =~ ^[[:space:]]{12}-[[:space:]]\"([A-Za-z0-9][A-Za-z0-9_.:-]*)\"$ ]] \
                    || return 1
                arguments+=("${BASH_REMATCH[1]}")
            done <<< "$value"
            expected_arguments=$(kubectl_render_sequencer_arguments "${arguments[@]}") \
                || return 1
            [[ "$value" == "$expected_arguments" ]]
            ;;
        CLAIM_VOLUME_MOUNTS|CLAIM_VOLUMES)
            _kubectl_validate_claim_volume_block "$token" "$value"
            ;;
        *) return 1 ;;
    esac
}

# Claim volumes. A Pod mounts each claim it needs at
# /mnt/storage-scale-test/<claim>, through a volume named claim-<N> by the
# claim's position N in the Pod's list. The two blocks replace
# @@CLAIM_VOLUME_MOUNTS@@ (a container's volumeMounts entries) and
# @@CLAIM_VOLUMES@@ (the Pod's volumes entries) at the template's own
# indentation: INDENT spaces before each "- name:".

# kubectl_render_claim_volume_mounts INDENT CLAIM...
kubectl_render_claim_volume_mounts() {
    local indent="$1" index=0 claim pad
    shift
    _kubectl_validate_claim_volume_list "$indent" "$@" || return 1
    printf -v pad '%*s' "$indent" ''
    for claim in "$@"; do
        printf '%s- name: claim-%s\n%s  mountPath: /mnt/storage-scale-test/%s\n' \
            "$pad" "$index" "$pad" "$claim"
        index=$((index + 1))
    done
}

# kubectl_render_claim_volumes INDENT CLAIM...
kubectl_render_claim_volumes() {
    local indent="$1" index=0 claim pad
    shift
    _kubectl_validate_claim_volume_list "$indent" "$@" || return 1
    printf -v pad '%*s' "$indent" ''
    for claim in "$@"; do
        printf '%s- name: claim-%s\n%s  persistentVolumeClaim:\n%s    claimName: "%s"\n' \
            "$pad" "$index" "$pad" "$pad" "$claim"
        index=$((index + 1))
    done
}

# INDENT is an even number of spaces from 2 to 16; the claims are one to
# KUBECTL_MAX_CLAIMS distinct claim names.
_kubectl_validate_claim_volume_list() {
    local indent="$1" claim
    shift
    [[ "$indent" =~ ^(2|4|6|8|10|12|14|16)$ ]] \
        && (( $# > 0 && $# <= KUBECTL_MAX_CLAIMS )) || return 1
    local -A seen=()
    for claim in "$@"; do
        kubectl_validate_object_name "$claim" && [[ ! -v seen["$claim"] ]] || return 1
        seen["$claim"]=1
    done
}

# A rendered claim block must be exactly what its renderer prints for the
# claims it names, so nothing else can enter a manifest through it.
_kubectl_validate_claim_volume_block() {
    local token="$1" value="$2" line indent expected
    local -a claims=()
    line="${value%%$'\n'*}"
    indent="${line%%[! ]*}"
    while IFS= read -r line; do
        if [[ "$line" =~ ^\ +mountPath:\ /mnt/storage-scale-test/([a-z0-9.-]+)$ \
                || "$line" =~ ^\ +claimName:\ \"([a-z0-9.-]+)\"$ ]]; then
            claims+=("${BASH_REMATCH[1]}")
        fi
    done <<< "$value"
    if [[ "$token" == CLAIM_VOLUME_MOUNTS ]]; then
        expected=$(kubectl_render_claim_volume_mounts "${#indent}" "${claims[@]}") \
            || return 1
    else
        expected=$(kubectl_render_claim_volumes "${#indent}" "${claims[@]}") || return 1
    fi
    [[ "$value" == "$expected" ]]
}

# _kubectl_claim_volume_arguments OUT pod|workload CLAIM...: set the array OUT
# to the CLAIM_VOLUME_MOUNTS and CLAIM_VOLUMES template assignments for a bare
# Pod template (transfer, status, collector) or a Pod template inside a Job
# or DaemonSet (sweep, workers, validation).
_kubectl_claim_volume_arguments() {
    local -n _kubectl_claim_volume_output="$1"
    # Prefixed locals never shadow the caller's output name.
    local _kcva_shape="$2" _kcva_mounts _kcva_volumes _kcva_mount_indent _kcva_volume_indent
    shift 2
    case "$_kcva_shape" in
        pod) _kcva_mount_indent=8 _kcva_volume_indent=4 ;;
        workload) _kcva_mount_indent=12 _kcva_volume_indent=8 ;;
        *) return 1 ;;
    esac
    _kcva_mounts=$(kubectl_render_claim_volume_mounts "$_kcva_mount_indent" "$@") \
        && _kcva_volumes=$(kubectl_render_claim_volumes "$_kcva_volume_indent" "$@") \
        || return 1
    _kubectl_claim_volume_output=("CLAIM_VOLUME_MOUNTS=$_kcva_mounts"
        "CLAIM_VOLUMES=$_kcva_volumes")
}

# _kubectl_segment_claim_volume_arguments OUT pod|workload: the claim volume
# assignments for every claim of the selected segment of the loaded attempt.
_kubectl_segment_claim_volume_arguments() {
    local -a claims=()
    mapfile -t claims < <(kubectl_segment_claims "$(_kubectl_selected_segment)")
    (( ${#claims[@]} > 0 )) || return 1
    _kubectl_claim_volume_arguments "$1" "$2" "${claims[@]}"
}

# _kubectl_control_claim_volume_arguments OUT: the claim volume assignments of
# a bare Pod template for the claim of the loaded control root alone, which
# must be a claim of the selected segment.
_kubectl_control_claim_volume_arguments() {
    local _kccva_namespace _kccva_claim _kccva_path
    kubectl_validate_saved_control_layout || return 1
    _kubectl_logical_parts "$KUBECTL_CONTROL_LOGICAL_ROOT" _kccva_namespace \
        _kccva_claim _kccva_path
    if [[ "$_kccva_namespace" != "${KUBECTL_NAMESPACE:-}" ]] \
            || ! kubectl_segment_claims "$(_kubectl_selected_segment)" \
                | grep -Fqx -- "$_kccva_claim"; then
        printf 'Error: the control root %s is not on a claim of Kubernetes namespace %s\n' \
            "$KUBECTL_CONTROL_LOGICAL_ROOT" "${KUBECTL_NAMESPACE:-}" >&2
        return 1
    fi
    _kubectl_claim_volume_arguments "$1" pod "$_kccva_claim"
}

# The checked-in POSIX sequencer, indented for the YAML block scalar in
# sequencer-job.yaml.tmpl. The Pod runs it as `sh -c SCRIPT NAME ARGS...`.
kubectl_render_sequencer_script() {
    local source_path line
    source_path="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_nv-elbencho-kubectl-sequencer.sh" \
        || return 1
    [[ -f "$source_path" && ! -L "$source_path" ]] || return 1
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" != *$'\t'* && "$line" != *$'\r'* && "$line" != *@@* ]] || return 1
        if [[ -z "$line" ]]; then
            printf '\n'
        else
            printf '                %s\n' "$line"
        fi
    done < "$source_path"
}

# kubectl_render_sequencer_arguments NAMESPACE JOB_NAME JOB_UID...: the
# sequencer's ordered per-segment Job triples as YAML list items.
kubectl_render_sequencer_arguments() {
    (( $# > 0 && $# % 3 == 0 && $# / 3 <= KUBECTL_MAX_SEGMENTS )) || return 1
    while (( $# > 0 )); do
        kubectl_validate_namespace_name "$1" && kubectl_validate_object_name "$2" \
            && kubectl_validate_uid "$3" || return 1
        printf '            - "%s"\n' "$1" "$2" "$3"
        shift 3
    done
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
            NODE_SELECTOR_BLOCK|NODE_AFFINITY_VALUES|TEST_ROOT_ARGUMENTS)
                _kubectl_validate_multiline_placeholder "$token" "$value" || return 1
                [[ "$rendered" == *"@@$token@@"* ]] || return 1
                rendered="${rendered//@@$token@@/$value}"
                ;;
            SEQUENCER_SCRIPT|SEQUENCER_ARGUMENTS|CLAIM_VOLUME_MOUNTS|CLAIM_VOLUMES)
                # Replace the single occurrence by splitting, so shell text
                # such as '&' is never interpreted by pattern substitution.
                _kubectl_validate_multiline_placeholder "$token" "$value" || return 1
                [[ "$rendered" == *"@@$token@@"* \
                    && "${rendered#*"@@$token@@"}" != *"@@$token@@"* ]] || return 1
                rendered="${rendered%%"@@$token@@"*}$value${rendered#*"@@$token@@"}"
                ;;
            *) basic+=("$assignment") ;;
        esac
    done
    kubectl_render_template "$rendered" "${basic[@]}"
}

# The workload settings every Kubernetes sweep needs. Storage comes from the
# claim-qualified TEST_DIRS keys (kubectl_validate_test_dirs in
# lib/_kubectl_claims.sh); KUBECTL_NAMESPACE, when set, is the namespace of
# keys that name none.
kubectl_validate_runtime_configuration() {
    local value rc=0
    for value in KUBECTL_NODE_SELECTOR KUBECTL_ELBENCHO_IMAGE KUBECTL_IMAGE_PULL_POLICY \
        KUBECTL_RUN_AS_USER KUBECTL_RUN_AS_GROUP; do
        if [[ -z "${!value:-}" ]]; then
            echo "Error: $value is required for Kubernetes filesystem sweeps" >&2
            rc=1
        fi
    done
    if [[ -n "${KUBECTL_NAMESPACE:-}" ]] \
            && ! kubectl_validate_namespace_name "$KUBECTL_NAMESPACE"; then
        printf 'Error: KUBECTL_NAMESPACE=%q must be a lowercase Kubernetes namespace name\n' \
            "$KUBECTL_NAMESPACE" >&2
        rc=1
    fi
    if [[ -n "${KUBECTL_NODE_SELECTOR:-}" ]] \
            && ! kubectl_validate_node_selector "$KUBECTL_NODE_SELECTOR"; then
        printf '%s\n' \
            'Error: KUBECTL_NODE_SELECTOR must contain comma-separated key=value labels' >&2
        rc=1
    fi
    if [[ -n "${KUBECTL_ELBENCHO_IMAGE:-}" ]] \
            && ! _kubectl_validate_placeholder IMAGE "$KUBECTL_ELBENCHO_IMAGE"; then
        printf 'Error: KUBECTL_ELBENCHO_IMAGE=%q is not a valid container image reference\n' \
            "$KUBECTL_ELBENCHO_IMAGE" >&2
        rc=1
    fi
    if [[ -n "${KUBECTL_IMAGE_PULL_POLICY:-}" ]] \
            && ! _kubectl_validate_placeholder IMAGE_PULL_POLICY \
                "$KUBECTL_IMAGE_PULL_POLICY"; then
        printf 'Error: KUBECTL_IMAGE_PULL_POLICY=%q must be Always, IfNotPresent, or Never\n' \
            "$KUBECTL_IMAGE_PULL_POLICY" >&2
        rc=1
    fi
    if [[ -n "${KUBECTL_RUN_AS_USER:-}" ]] \
            && ! _kubectl_validate_placeholder RUN_AS_USER "$KUBECTL_RUN_AS_USER"; then
        printf 'Error: KUBECTL_RUN_AS_USER=%q must be a positive numeric UID\n' \
            "$KUBECTL_RUN_AS_USER" >&2
        rc=1
    fi
    if [[ -n "${KUBECTL_RUN_AS_GROUP:-}" ]] \
            && ! _kubectl_validate_placeholder RUN_AS_GROUP "$KUBECTL_RUN_AS_GROUP"; then
        printf 'Error: KUBECTL_RUN_AS_GROUP=%q must be a positive numeric GID\n' \
            "$KUBECTL_RUN_AS_GROUP" >&2
        rc=1
    fi
    if [[ "${KUBECTL_IMAGE_PULL_POLICY:-}" == Always \
            && -n "${KUBECTL_ELBENCHO_IMAGE:-}" \
            && ! "$KUBECTL_ELBENCHO_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]; then
        printf 'Error: KUBECTL_IMAGE_PULL_POLICY=Always requires a digest-qualified KUBECTL_ELBENCHO_IMAGE (name@sha256:<digest>); current: %s\n' \
            "$KUBECTL_ELBENCHO_IMAGE" >&2
        rc=1
    fi
    kubectl_validate_sequencer_configuration 1 || rc=1
    return "$rc"
}

_kubectl_validate_sequencer_poll_seconds() {
    [[ "$1" =~ ^[1-9][0-9]{0,3}$ ]] && (( $1 <= 3600 ))
}

# kubectl_sequencer_image: print the sequencer's image, KUBECTL_SEQUENCER_IMAGE
# or, when that is empty or unset, KUBECTL_DEFAULT_SEQUENCER_IMAGE.
kubectl_sequencer_image() {
    printf '%s\n' "${KUBECTL_SEQUENCER_IMAGE:-$KUBECTL_DEFAULT_SEQUENCER_IMAGE}"
}

# kubectl_validate_sequencer_configuration SEGMENT_COUNT: more than one
# segment (a batch whose targets span namespaces) runs the sequencer. Its image
# defaults to KUBECTL_DEFAULT_SEQUENCER_IMAGE; a configured
# KUBECTL_SEQUENCER_IMAGE must be digest-pinned and is always checked, even
# when unused.
kubectl_validate_sequencer_configuration() {
    local count="$1" rc=0
    _kubectl_validate_segment_count "$count" || {
        printf 'Error: invalid Kubernetes segment count %q\n' "$count" >&2
        return 1
    }
    if [[ -n "${KUBECTL_SEQUENCER_IMAGE:-}" ]] \
            && { ! _kubectl_validate_placeholder IMAGE "$KUBECTL_SEQUENCER_IMAGE" \
                || [[ ! "$KUBECTL_SEQUENCER_IMAGE" =~ @sha256:[0-9a-f]{64}$ ]]; }; then
        printf 'Error: KUBECTL_SEQUENCER_IMAGE=%q must be a digest-qualified container image reference\n' \
            "$KUBECTL_SEQUENCER_IMAGE" >&2
        rc=1
    fi
    if [[ -n "${KUBECTL_SEQUENCER_POLL_SECONDS:-}" ]] \
            && ! _kubectl_validate_sequencer_poll_seconds "$KUBECTL_SEQUENCER_POLL_SECONDS"; then
        printf 'Error: KUBECTL_SEQUENCER_POLL_SECONDS=%q must be an integer from 1 to 3600\n' \
            "$KUBECTL_SEQUENCER_POLL_SECONDS" >&2
        rc=1
    fi
    return "$rc"
}

# kubectl_render_sequencer_job ATTEMPT_ID NONCE NAMESPACE JOB_TRIPLE...: the
# sequencer Job in segment 0's NAMESPACE. Each JOB_TRIPLE is the namespace, name
# and UID of one segment's suspended coordinator Job, in segment order.
kubectl_render_sequencer_job() {
    local attempt_id="$1" nonce="$2" namespace="$3" name selector_block script arguments
    shift 3
    # A sequencer exists only for more than one segment: check its settings.
    kubectl_validate_sequencer_configuration 2 || return 1
    name=$(kubectl_sequencer_resource_name "$attempt_id") || return 1
    selector_block=$(kubectl_render_node_selector "$KUBECTL_NODE_SELECTOR") || return 1
    script=$(kubectl_render_sequencer_script) || return 1
    arguments=$(kubectl_render_sequencer_arguments "$@") || return 1
    kubectl_render_attempt_template "$(kubectl_template_directory)/sequencer-job.yaml.tmpl" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$name" "ATTEMPT_ID=$attempt_id" \
        "OWNERSHIP_NONCE=$nonce" "SERVICE_ACCOUNT_NAME=$name" \
        "IMAGE=$(kubectl_sequencer_image)" "IMAGE_PULL_POLICY=$KUBECTL_IMAGE_PULL_POLICY" \
        "RUN_AS_USER=$KUBECTL_RUN_AS_USER" "RUN_AS_GROUP=$KUBECTL_RUN_AS_GROUP" \
        "POLL_SECONDS=${KUBECTL_SEQUENCER_POLL_SECONDS:-$KUBECTL_SEQUENCER_POLL_SECONDS_DEFAULT}" \
        "NODE_SELECTOR_BLOCK=$selector_block" "SEQUENCER_SCRIPT=$script" \
        "SEQUENCER_ARGUMENTS=$arguments"
}

kubectl_render_sequencer_service_account() {
    local attempt_id="$1" nonce="$2" namespace="$3" name
    name=$(kubectl_sequencer_resource_name "$attempt_id") || return 1
    kubectl_render_attempt_template \
        "$(kubectl_template_directory)/sequencer-serviceaccount.yaml.tmpl" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$name" "ATTEMPT_ID=$attempt_id" \
        "OWNERSHIP_NONCE=$nonce"
}

# kubectl_render_sequencer_role ATTEMPT_ID NONCE SEGMENT NAMESPACE JOB_NAME:
# get and patch on exactly segment SEGMENT's coordinator Job.
kubectl_render_sequencer_role() {
    local attempt_id="$1" nonce="$2" index="$3" namespace="$4" job_name="$5" name
    name=$(kubectl_segment_resource_name "$attempt_id" "$index" sequencer) || return 1
    kubectl_render_attempt_template "$(kubectl_template_directory)/sequencer-role.yaml.tmpl" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$name" "ATTEMPT_ID=$attempt_id" \
        "OWNERSHIP_NONCE=$nonce" "SEGMENT_INDEX=$index" "JOB_NAME=$job_name"
}

# kubectl_render_sequencer_role_binding ATTEMPT_ID NONCE SEGMENT NAMESPACE
# SUBJECT_NAMESPACE: grant segment SEGMENT's Role to the sequencer
# ServiceAccount in SUBJECT_NAMESPACE (segment 0's namespace).
kubectl_render_sequencer_role_binding() {
    local attempt_id="$1" nonce="$2" index="$3" namespace="$4" subject_namespace="$5"
    local name service_account
    name=$(kubectl_segment_resource_name "$attempt_id" "$index" sequencer) || return 1
    service_account=$(kubectl_sequencer_resource_name "$attempt_id") || return 1
    kubectl_render_attempt_template \
        "$(kubectl_template_directory)/sequencer-rolebinding.yaml.tmpl" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$name" "ATTEMPT_ID=$attempt_id" \
        "OWNERSHIP_NONCE=$nonce" "SEGMENT_INDEX=$index" \
        "SERVICE_ACCOUNT_NAME=$service_account" "SUBJECT_NAMESPACE=$subject_namespace"
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

# kubectl_test_dirs_segment NAMESPACE_OUT CLAIMS_OUT: the one namespace of
# the current TEST_DIRS roots and their sorted, unique claims (an array).
# Fails, naming the problem, when a key is invalid or the roots span
# namespaces.
kubectl_test_dirs_segment() {
    local -n _kubectl_tds_namespace="$1" _kubectl_tds_claims="$2"
    local _ktds_key _ktds_namespace _ktds_claim _ktds_path _ktds_first=""
    local -A _ktds_seen=()
    _kubectl_tds_claims=()
    if ! declare -p TEST_DIRS >/dev/null 2>&1 || (( ${#TEST_DIRS[@]} == 0 )); then
        echo "Error: TEST_DIRS must name at least one claim-qualified root" >&2
        return 1
    fi
    for _ktds_key in "${!TEST_DIRS[@]}"; do
        kubectl_split_test_dir_key "$_ktds_key" _ktds_namespace _ktds_claim _ktds_path \
            || return 1
        if [[ -n "$_ktds_first" && "$_ktds_namespace" != "$_ktds_first" ]]; then
            printf 'Error: TEST_DIRS roots span Kubernetes namespaces %s and %s\n' \
                "$_ktds_first" "$_ktds_namespace" >&2
            return 1
        fi
        _ktds_first="$_ktds_namespace"
        _ktds_seen["$_ktds_claim"]=1
    done
    mapfile -t _kubectl_tds_claims < <(printf '%s\n' "${!_ktds_seen[@]}" | LC_ALL=C sort)
    # shellcheck disable=SC2034  # Assigned through the caller's nameref.
    _kubectl_tds_namespace="$_ktds_first"
}

# Lease permission (get, create, delete) in NAMESPACE.
_kubectl_validate_namespace_lease_access() {
    local namespace="$1" verb allowed
    for verb in get create delete; do
        allowed=""
        if ! allowed=$(kubectl_run_observational auth can-i "$verb" \
                leases.coordination.k8s.io -n "$namespace"); then
            if [[ "$allowed" == no ]]; then
                printf 'Error: Kubernetes filesystem sweeps require permission to %s leases.coordination.k8s.io in namespace %s\n' \
                    "$verb" "$namespace" >&2
                return 1
            fi
            printf 'Error: could not verify Kubernetes Lease permission: %s leases.coordination.k8s.io in namespace %s\n' \
                "$verb" "$namespace" >&2
            return 1
        fi
        if [[ "$allowed" != yes ]]; then
            printf 'Error: Kubernetes filesystem sweeps require permission to %s leases.coordination.k8s.io in namespace %s\n' \
                "$verb" "$namespace" >&2
            return 1
        fi
    done
}

# kubectl_validate_claim_identity NAMESPACE CLAIM: CLAIM must be Bound,
# ReadWriteMany and Filesystem mode. Prints "PV<TAB>PV_UID<TAB>PVC_UID", where
# PV is the volume the claim is bound to.
kubectl_validate_claim_identity() {
    local namespace="$1" claim="$2" binding observed_pv phase mode access pv_uid pvc_uid
    binding=$(kubectl_run_observational -n "$namespace" get pvc "$claim" \
        -o 'jsonpath={.metadata.uid}{"\t"}{.spec.volumeName}{"\t"}{.status.phase}{"\t"}{.spec.volumeMode}{"\t"}{.spec.accessModes[*]}') || {
            printf 'Error: cannot read PersistentVolumeClaim %q in namespace %q\n' \
                "$claim" "$namespace" >&2
            return 1
        }
    IFS=$'\t' read -r pvc_uid observed_pv phase mode access <<< "$binding"
    if ! kubectl_validate_uid "${pvc_uid:-}" || [[ "$phase" != Bound ]] \
            || ! kubectl_validate_object_name "${observed_pv:-}" \
            || [[ -n "$mode" && "$mode" != Filesystem ]] \
            || [[ "$access" != *ReadWriteMany* ]]; then
        printf 'Error: PVC %s/%s does not satisfy the storage contract\n' \
            "$namespace" "$claim" >&2
        printf '  expected: phase=Bound volumeMode=Filesystem accessModes includes ReadWriteMany\n' >&2
        printf '  observed: volumeName=%s phase=%s volumeMode=%s accessModes=%s\n' \
            "${observed_pv:-<empty>}" "${phase:-<empty>}" \
            "${mode:-<empty>}" "${access:-<empty>}" >&2
        return 1
    fi
    pv_uid=$(kubectl_get_object_uid pv "$observed_pv") || {
        printf 'Error: cannot read PersistentVolume %q bound to PVC %s/%s; verify cluster-scope access\n' \
            "$observed_pv" "$namespace" "$claim" >&2
        return 1
    }
    printf '%s\t%s\t%s\n' "$observed_pv" "$pv_uid" "$pvc_uid"
}

# kubectl_validate_cluster_identity [NAMESPACE CLAIM...]: validate the
# runtime settings, API access, Lease permission in NAMESPACE, and every
# CLAIM (see kubectl_validate_claim_identity); every failing claim is named.
# Without arguments the namespace and claims come from TEST_DIRS. Prints one
# "NAMESPACE<TAB>NAMESPACE_UID<TAB>PV<TAB>PV_UID<TAB>CLAIM<TAB>PVC_UID" row
# per claim, in the given order: the claim tuple of
# kubectl_attempt_create_identity.
kubectl_validate_cluster_identity() {
    local namespace claim namespace_uid claim_identity rc=0
    local -a claims=()
    kubectl_validate_runtime_configuration || return 1
    if (( $# > 0 )); then
        namespace="$1"
        shift
        claims=("$@")
    else
        kubectl_test_dirs_segment namespace claims || return 1
    fi
    if ! kubectl_validate_namespace_name "$namespace" || (( ${#claims[@]} == 0 )); then
        printf 'Error: invalid Kubernetes namespace %q or no claims to validate\n' \
            "$namespace" >&2
        return 1
    fi
    if ! command -v kubectl >/dev/null 2>&1; then
        echo "Error: kubectl is not installed or is not on PATH" >&2
        return 1
    fi
    kubectl_run_observational version >/dev/null || {
        echo "Error: kubectl could not reach the API server using the active context" >&2
        return 1
    }
    _kubectl_validate_namespace_lease_access "$namespace" || return 1
    namespace_uid=$(kubectl_get_object_uid namespace "$namespace") || {
        printf 'Error: cannot read Kubernetes namespace %q with the active context\n' \
            "$namespace" >&2
        return 1
    }
    local -a rows=()
    local pv pv_uid pvc_uid
    for claim in "${claims[@]}"; do
        kubectl_validate_object_name "$claim" || {
            printf 'Error: invalid PersistentVolumeClaim name %q\n' "$claim" >&2
            rc=1
            continue
        }
        claim_identity=$(kubectl_validate_claim_identity "$namespace" "$claim") || {
            rc=1
            continue
        }
        IFS=$'\t' read -r pv pv_uid pvc_uid <<< "$claim_identity"
        rows+=("$namespace"$'\t'"$namespace_uid"$'\t'"$pv"$'\t'"$pv_uid"$'\t'"$claim"$'\t'"$pvc_uid")
    done
    [[ "$rc" -eq 0 ]] || return 1
    printf '%s\n' "${rows[@]}"
}

# _kubectl_print_validation_pod_cause NAMESPACE ATTEMPT BUNDLE: on stderr, the
# lines that usually name the cause, ahead of the full dump: the container's
# own "Error:" lines and the Pods' Warning events, with untruncated messages.
_kubectl_print_validation_pod_cause() {
    local namespace="$1" attempt_id="$2" bundle="$3" pod_ref pod_name pod_count=0
    local pod_names events
    if [[ -f "$bundle/pods.log" && ! -L "$bundle/pods.log" ]]; then
        # kubectl logs --prefix starts each line with "[pod/NAME/CONTAINER] ".
        sed -nE 's/^(\[[^]]*\] )?(Error: .*)$/Pod log: \2/p' -- "$bundle/pods.log" \
            | head -n 5 >&2 || true
    fi
    pod_names=$(KUBECTL_OBSERVATION_ATTEMPTS=1 KUBECTL_REQUEST_TIMEOUT_SECONDS=5 \
        KUBECTL_PROCESS_TIMEOUT_SECONDS=5 kubectl_run_observational -n "$namespace" \
        get pods -l "storage-scale-test.nvidia.com/run=$attempt_id" -o name \
        2>/dev/null | head -c 65536) || pod_names=""
    while IFS= read -r pod_ref; do
        [[ "$pod_ref" == pod/* ]] || continue
        pod_name=${pod_ref#pod/}
        kubectl_validate_object_name "$pod_name" || continue
        pod_count=$((pod_count + 1))
        (( pod_count <= KUBECTL_DIAGNOSTIC_MAX_PODS )) || break
        events=$(KUBECTL_OBSERVATION_ATTEMPTS=1 KUBECTL_REQUEST_TIMEOUT_SECONDS=5 \
            KUBECTL_PROCESS_TIMEOUT_SECONDS=5 kubectl_run_observational \
            -n "$namespace" get events \
            --field-selector "involvedObject.name=$pod_name,type=Warning" \
            -o 'jsonpath={range .items[*]}{"Pod event "}{.reason}{": "}{.message}{"\n"}{end}' \
            2>/dev/null | head -c 4096) || events=""
        [[ -z "$events" ]] || printf '%s\n' "${events%$'\n'}" >&2
    done <<< "$pod_names"
    return 0
}

_kubectl_print_runtime_validation_diagnostics() {
    local namespace="$1" helper_name="$2" attempt_id="$3"
    local temporary_root root metadata_dir bundle file
    temporary_root=$(cd -P -- "${TMPDIR:-/tmp}" 2>/dev/null && pwd -P) \
        || return 1
    root=$(mktemp -d "$temporary_root/storage-scale-test-validation-diag.XXXXXX") \
        || return 1
    metadata_dir="$root/attempts/$attempt_id"
    mkdir -p -- "$metadata_dir" || {
        rm -rf -- "$root"
        return 1
    }
    bundle=$(kubectl_capture_resource_diagnostics "$metadata_dir" \
        runtime-validation "$namespace" Job "$helper_name" "$attempt_id") \
        || bundle=""
    if [[ -z "$bundle" ]]; then
        rm -rf -- "$root"
        return 1
    fi
    _kubectl_print_validation_pod_cause "$namespace" "$attempt_id" "$bundle"
    for file in resource.describe pods.yaml pods.log pod-*.describe events.txt; do
        local evidence
        for evidence in "$bundle"/$file; do
            [[ -s "$evidence" && -f "$evidence" && ! -L "$evidence" ]] || continue
            printf '\n===== Kubernetes validation diagnostic: %s =====\n' \
                "$(basename "$evidence")" >&2
            cat -- "$evidence" >&2 || true
        done
    done
    rm -rf -- "$root"
}

# _kubectl_validation_pod_blocked NAMESPACE ATTEMPT JOB: print why the Pod of
# validation Job JOB can never start, and return 0; return 1 when nothing
# shows that now. Either its image cannot start (a fatal waiting reason), or
# the Pod is still Pending in ContainerCreating and a FailedMount event for it
# shows a claim's mount command itself failed ("mount failed: exit status N").
# Events are history and kubelet retries mounts, so a Pod that has since
# started (or finished) is never blocked by an old event; a FailedMount that
# only timed out may still clear, so it does not count either.
_kubectl_validation_pod_blocked() {
    local namespace="$1" attempt_id="$2" job_name="$3" reason rows line
    local name phase waiting
    local -A mounting=()
    if reason=$(_kubectl_pod_fatal_waiting_reason "$namespace" "$attempt_id"); then
        printf 'cannot start the image: %s\n' "$reason"
        return 0
    fi
    rows=$(KUBECTL_OBSERVATION_ATTEMPTS=1 KUBECTL_REQUEST_TIMEOUT_SECONDS=10 \
        KUBECTL_PROCESS_TIMEOUT_SECONDS=15 kubectl_run_observational -n "$namespace" \
        get pods -l "storage-scale-test.nvidia.com/run=$attempt_id" \
        -o 'jsonpath={range .items[*]}{.metadata.name}{"\t"}{.status.phase}{"\t"}{range .status.containerStatuses[*]}{.state.waiting.reason}{","}{end}{"\n"}{end}' \
        2>/dev/null | head -c 65536) || return 1
    while IFS=$'\t' read -r name phase waiting; do
        [[ "$name" == "$job_name"-* && "$phase" == Pending \
            && ( -z "$waiting" || "$waiting" == ContainerCreating, ) ]] \
            && mounting[$name]=1
    done <<< "$rows"
    (( ${#mounting[@]} > 0 )) || return 1
    rows=$(KUBECTL_OBSERVATION_ATTEMPTS=1 KUBECTL_REQUEST_TIMEOUT_SECONDS=10 \
        KUBECTL_PROCESS_TIMEOUT_SECONDS=15 kubectl_run_observational -n "$namespace" \
        get events \
        --field-selector 'type=Warning,reason=FailedMount,involvedObject.kind=Pod' \
        -o 'jsonpath={range .items[*]}{.involvedObject.name}{"\t"}{.message}{"\n"}{end}' \
        2>/dev/null | head -c 262144) || return 1
    while IFS= read -r line; do
        # An empty response still yields one empty row; skip rows without a
        # Pod name before looking one up.
        name=${line%%$'\t'*}
        [[ "$line" == *$'\t'* && -n "$name" && -n "${mounting[$name]:-}" \
            && "$line" == *$'\t'*"mount failed: exit status"* ]] || continue
        printf 'cannot mount a claim'"'"'s volume (FailedMount)\n'
        return 0
    done <<< "$rows"
    return 1
}

# _kubectl_validation_failed_phase NAMESPACE ATTEMPT: print the last
# STORAGE_SCALE_TEST_VALIDATION_PHASE the validation Pod reported.
_kubectl_validation_failed_phase() {
    KUBECTL_OBSERVATION_ATTEMPTS=1 KUBECTL_REQUEST_TIMEOUT_SECONDS=10 \
        KUBECTL_PROCESS_TIMEOUT_SECONDS=15 kubectl_run_observational -n "$1" \
        logs -l "storage-scale-test.nvidia.com/run=$2" --tail=50 2>/dev/null \
        | sed -n 's/^.*STORAGE_SCALE_TEST_VALIDATION_PHASE=\([a-z-]*\).*$/\1/p' \
        | tail -n 1
}

# _kubectl_print_runtime_validation_failure TERMINAL OBSERVE_RC NODE REASON
# FATAL: the one-line "Error:" headline of a runtime validation Job that did
# not complete, naming the node and the classified reason.
_kubectl_print_runtime_validation_failure() {
    local terminal="$1" observe_rc="$2" node="$3" reason="$4" fatal="$5" why=""
    if [[ -n "$fatal" ]]; then
        printf 'Error: the runtime validation Pod on node %s %s\n' "$node" "$fatal" >&2
    elif [[ "$terminal" == FAILED ]]; then
        printf 'Error: the runtime validation Job failed on node %s\n' "$node" >&2
    elif [[ "$observe_rc" -ne 0 ]]; then
        printf 'Error: the runtime validation Job could not be observed\n' >&2
    else
        case "$reason" in
            PVC_MOUNT) why=" (a claim's volume could not be mounted there)" ;;
            IMAGE_PULL) why=" (the image could not be pulled)" ;;
            POD_UNSCHEDULABLE) why=" (the Pod could not be scheduled)" ;;
        esac
        printf 'Error: the runtime validation Job did not finish within 180 seconds on node %s%s\n' \
            "$node" "$why" >&2
    fi
}

kubectl_validate_runtime_pod() {
    # Use a deadline-bounded, TTL-cleaned Job so SIGKILL of the local validator
    # cannot leave an unjournaled sleeping Pod. The Job validates the image,
    # workload identity, command contract, and actual PVC write/read/remove on
    # every root. It runs in the roots' namespace and mounts all their claims.
    kubectl_validate_cluster_identity >/dev/null || return 1
    local control_logical_root control_test_root namespace
    local -a claims=() claim_volumes=()
    kubectl_select_control_root control_logical_root control_test_root || return 1
    kubectl_set_control_layout "$control_logical_root" "$control_test_root" || return 1
    kubectl_test_dirs_segment namespace claims \
        && _kubectl_claim_volume_arguments claim_volumes workload "${claims[@]}" \
        || return 1
    local -A mapped_test_dirs=()
    kubectl_map_test_dirs mapped_test_dirs || return 1
    local test_root_arguments
    test_root_arguments=$(kubectl_render_test_root_arguments \
        "${!mapped_test_dirs[@]}") || return 1
    local KUBECTL_NAMESPACE="$namespace"
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
        "NODE_NAME=$node" "${claim_volumes[@]}" \
        "MOUNT_ROOT=$KUBECTL_SWEEP_MOUNT_ROOT" \
        "TEST_ROOT_ARGUMENTS=$test_root_arguments") || return 1
    # From the create until the delete, an INT or TERM only ends the wait: the
    # Job is still deleted, then the interrupt's status is returned.
    local interrupted="" previous_int previous_term
    previous_int=$(trap -p INT)
    previous_term=$(trap -p TERM)
    trap 'interrupted=130' INT
    trap 'interrupted=143' TERM
    kubectl_create_owned_object helper_uid Job "$helper_name" "$KUBECTL_NAMESPACE" \
        "$nonce" "$attempt_id" "$manifest" || {
            eval "${previous_int:-trap - INT}"
            eval "${previous_term:-trap - TERM}"
            return 1
        }
    # Stop before the Job's own active deadline so its Pod still exists when
    # diagnostics inspect scheduling, image pulls, mounts, logs, and events.
    # The deadline allows a slow first image pull. Each poll costs kubectl
    # processes (and any credential plugin), so the Job is polled every second
    # only for the first 10 seconds, when a healthy check finishes, then every
    # 2; every third poll looks for a Pod that can never start
    # (_kubectl_validation_pod_blocked), which ends the wait at once.
    local started=$SECONDS deadline=$((SECONDS + 180)) terminal="" wait_rc
    local fatal="" polls=0
    while (( SECONDS < deadline )) && [[ -z "$interrupted" ]]; do
        wait_rc=0
        KUBECTL_REQUEST_TIMEOUT_SECONDS=10 KUBECTL_PROCESS_TIMEOUT_SECONDS=15 \
            kubectl_job_terminal_state terminal "$helper_name" "$KUBECTL_NAMESPACE" \
                "$nonce" "$attempt_id" "$helper_uid" || wait_rc=$?
        [[ "$wait_rc" -eq 0 ]] && break
        [[ "$wait_rc" -eq 2 ]] || { rc=1; break; }
        polls=$((polls + 1))
        (( polls % 3 != 0 )) || ! fatal=$(_kubectl_validation_pod_blocked \
            "$KUBECTL_NAMESPACE" "$attempt_id" "$helper_name") || break
        if (( SECONDS - started < 10 )); then sleep 1; else sleep 2; fi
    done
    if [[ "$terminal" != COMPLETE && -z "$interrupted" ]]; then
        local readiness_reason=TIMEOUT
        kubectl_classify_readiness_failure readiness_reason "$KUBECTL_NAMESPACE" \
            "$attempt_id" || true
        # A Job that failed on its own was not a timeout; one that stopped in
        # a pvc-* phase found a TEST_DIRS root unusable.
        if [[ "$terminal" == FAILED && "$readiness_reason" == TIMEOUT ]] \
            && _kubectl_validation_failed_phase "$KUBECTL_NAMESPACE" "$attempt_id" \
                | grep -q '^pvc-'; then
            readiness_reason=PVC_IO
        fi
        _kubectl_print_runtime_validation_failure "$terminal" "$rc" "$node" \
            "$readiness_reason" "$fatal"
        _kubectl_print_runtime_validation_diagnostics "$KUBECTL_NAMESPACE" \
            "$helper_name" "$attempt_id" || {
                echo "Warning: automatic validation diagnostic capture failed" >&2
                printf 'Inspect before retrying: kubectl -n %q get pods -l %q -o wide\n' \
                    "$KUBECTL_NAMESPACE" \
                    "storage-scale-test.nvidia.com/run=$attempt_id" >&2
            }
        KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
            validate-runtime runtime-validation "$readiness_reason" \
            "inspect the Job diagnostics, correct the fixture, and rerun validation" \
            no Job "$helper_name" "$KUBECTL_NAMESPACE" "$helper_uid" "" "" || true
        rc=1
    fi
    kubectl_delete_owned_object Job "$helper_name" "$KUBECTL_NAMESPACE" "$nonce" \
        "$attempt_id" "$helper_uid" || rc=1
    eval "${previous_int:-trap - INT}"
    eval "${previous_term:-trap - TERM}"
    [[ -z "$interrupted" ]] || return "$interrupted"
    return "$rc"
}

# Validation only (validate_env.sh, batches spanning namespaces).
# kubectl_validate_sequencer_probe [SECOND_NAMESPACE]: run the checked-in
# sequencer once, in a deadline-bounded Job in KUBECTL_NAMESPACE (segment 0's)
# under the sequencer's RBAC shape, against one absent Job per namespace; it
# must exit 3 (API NotFound) for each. With SECOND_NAMESPACE, a Role and
# RoleBinding there grant the namespace-0 ServiceAccount get on one absent
# Job, exercising the cross-namespace binding a multi-segment attempt uses.
#
# The probe runs in a subshell so its traps stay local: every probe object is
# registered (kind, deterministic name, namespace) before its create, and an
# EXIT trap (also reached on INT and TERM) deletes them in reverse order. An
# object whose create response was lost is found by its deterministic name and
# deleted only after its ownership nonce and run label verify; a foreign
# object of that name is never deleted.
kubectl_validate_sequencer_probe() {
    _kubectl_run_forwarding_signals _kubectl_run_sequencer_probe "$@"
}

# _kubectl_run_forwarding_signals COMMAND [ARG...]: run COMMAND in a background
# subshell, so its traps and exit stay local, and wait for it. An INT or TERM
# received meanwhile is forwarded to the subshell as TERM (a background
# subshell ignores INT unless it traps it); its EXIT trap cleans up, this
# function waits for that, and returns 130 or 143 for the signal received,
# otherwise the subshell's status. Unlike a foreground subshell, this also
# works when the caller itself runs in the background, where nothing but this
# function can pass a signal on.
_kubectl_run_forwarding_signals() {
    local child="" signalled="" rc=0 previous_int previous_term
    previous_int=$(trap -p INT)
    previous_term=$(trap -p TERM)
    trap 'signalled=${signalled:-130}; [[ -z "$child" ]] || kill -TERM "$child" 2>/dev/null || true' INT
    trap 'signalled=${signalled:-143}; [[ -z "$child" ]] || kill -TERM "$child" 2>/dev/null || true' TERM
    ( "$@" ) &
    child=$!
    [[ -z "$signalled" ]] || kill -TERM "$child" 2>/dev/null || true
    wait "$child" || rc=$?
    while kill -0 "$child" 2>/dev/null; do
        wait "$child" || rc=$?
    done
    eval "${previous_int:-trap - INT}"
    eval "${previous_term:-trap - TERM}"
    return "${signalled:-$rc}"
}

# _kubectl_exit_on_signal CLEANUP: make INT and TERM exit with 130 or 143,
# then run CLEANUP as the EXIT trap. The order matters: in a background
# subshell, bash 5.1 and 5.2 silently keep INT ignored if the EXIT trap is set
# before the INT trap (5.3 does not), so the signal traps come first. Each
# handler ignores both signals before it exits: a Ctrl-C reaches the whole
# process group and the caller also forwards it as TERM, and a second signal
# arriving before the EXIT trap ignores them itself would otherwise end the
# cleanup early and leave objects behind.
_kubectl_exit_on_signal() {
    trap 'trap "" INT TERM; exit 130' INT
    trap 'trap "" INT TERM; exit 143' TERM
    # shellcheck disable=SC2064  # CLEANUP is a function name, fixed here.
    trap "$1" EXIT
}

_kubectl_run_sequencer_probe() {
    local namespace="${KUBECTL_NAMESPACE:-}" second="${1:-}" timeout name job_name absent
    local manifest selector_block script arguments template_dir rc=0
    timeout="${KUBECTL_SEQUENCER_PROBE_TIMEOUT_SECONDS:-$KUBECTL_SEQUENCER_PROBE_TIMEOUT_SECONDS_DEFAULT}"
    if ! _kubectl_validate_sequencer_poll_seconds "$timeout"; then
        printf 'Error: KUBECTL_SEQUENCER_PROBE_TIMEOUT_SECONDS=%q must be an integer from 1 to 3600\n' \
            "$timeout" >&2
        return 1
    fi
    kubectl_validate_sequencer_configuration 2 \
        && kubectl_validate_namespace_name "$namespace" || return 1
    if [[ -n "$second" ]] && { ! kubectl_validate_namespace_name "$second" \
            || [[ "$second" == "$namespace" ]]; }; then
        printf 'Error: invalid second sequencer probe namespace %q\n' "$second" >&2
        return 1
    fi
    _KUBECTL_PROBE_ATTEMPT=$(kubectl_generate_attempt_id) \
        && _KUBECTL_PROBE_NONCE=$(kubectl_generate_ownership_nonce) \
        && name=$(kubectl_sequencer_resource_name "$_KUBECTL_PROBE_ATTEMPT") \
        && selector_block=$(kubectl_render_node_selector "$KUBECTL_NODE_SELECTOR") \
        && script=$(kubectl_render_sequencer_script) \
        && template_dir=$(kubectl_template_directory) || return 1
    job_name="sst-elb-$_KUBECTL_PROBE_ATTEMPT-sequencer-probe"
    absent="sst-elb-$_KUBECTL_PROBE_ATTEMPT-absent"
    local -a triples=("$namespace" "$absent" "$KUBECTL_SEQUENCER_PROBE_ABSENT_UID")
    [[ -z "$second" ]] \
        || triples+=("$second" "$absent" "$KUBECTL_SEQUENCER_PROBE_ABSENT_UID")
    arguments=$(kubectl_render_sequencer_arguments "${triples[@]}") || return 1
    _KUBECTL_PROBE_OBJECTS=()
    _KUBECTL_PROBE_UIDS=()
    _kubectl_exit_on_signal _kubectl_cleanup_sequencer_probe
    manifest=$(kubectl_render_sequencer_service_account "$_KUBECTL_PROBE_ATTEMPT" \
        "$_KUBECTL_PROBE_NONCE" "$namespace") \
        && _kubectl_create_probe_object ServiceAccount "$name" "$namespace" "$manifest" \
        && _kubectl_create_probe_rbac 0 "$namespace" "$namespace" "$absent" || rc=1
    if [[ "$rc" -eq 0 && -n "$second" ]]; then
        _kubectl_create_probe_rbac 1 "$second" "$namespace" "$absent" || rc=1
    fi
    if [[ "$rc" -eq 0 ]]; then
        manifest=$(kubectl_render_attempt_template "$template_dir/sequencer-probe-job.yaml.tmpl" \
            "NAMESPACE=$namespace" "RESOURCE_NAME=$job_name" \
            "ATTEMPT_ID=$_KUBECTL_PROBE_ATTEMPT" "OWNERSHIP_NONCE=$_KUBECTL_PROBE_NONCE" \
            "SERVICE_ACCOUNT_NAME=$name" "IMAGE=$(kubectl_sequencer_image)" \
            "IMAGE_PULL_POLICY=$KUBECTL_IMAGE_PULL_POLICY" \
            "RUN_AS_USER=$KUBECTL_RUN_AS_USER" "RUN_AS_GROUP=$KUBECTL_RUN_AS_GROUP" \
            "ACTIVE_DEADLINE_SECONDS=$((timeout + 60))" \
            "NODE_SELECTOR_BLOCK=$selector_block" "SEQUENCER_SCRIPT=$script" \
            "SEQUENCER_ARGUMENTS=$arguments") \
            && _kubectl_create_probe_object Job "$job_name" "$namespace" "$manifest" \
            || rc=1
    fi
    if [[ "$rc" -eq 0 ]]; then
        _kubectl_wait_sequencer_probe "$namespace" "$job_name" "$_KUBECTL_PROBE_NONCE" \
            "$_KUBECTL_PROBE_ATTEMPT" "${_KUBECTL_PROBE_UIDS[-1]}" "$timeout" || rc=1
    fi
    # The EXIT trap deletes every registered object and keeps this status.
    exit "$rc"
}

# _kubectl_create_probe_rbac INDEX NAMESPACE SUBJECT_NAMESPACE ABSENT: the probe
# Role (get on Job ABSENT) and RoleBinding (to the SUBJECT_NAMESPACE
# ServiceAccount) for segment INDEX's namespace.
_kubectl_create_probe_rbac() {
    local index="$1" namespace="$2" subject_namespace="$3" absent="$4" role manifest
    role=$(kubectl_segment_resource_name "$_KUBECTL_PROBE_ATTEMPT" "$index" sequencer) \
        || return 1
    manifest=$(kubectl_render_sequencer_role "$_KUBECTL_PROBE_ATTEMPT" \
        "$_KUBECTL_PROBE_NONCE" "$index" "$namespace" "$absent") \
        && _kubectl_create_probe_object Role "$role" "$namespace" "$manifest" \
        || return 1
    manifest=$(kubectl_render_sequencer_role_binding "$_KUBECTL_PROBE_ATTEMPT" \
        "$_KUBECTL_PROBE_NONCE" "$index" "$namespace" "$subject_namespace") \
        && _kubectl_create_probe_object RoleBinding "$role" "$namespace" "$manifest"
}

# Register one probe object before creating it, then record its UID.
_kubectl_create_probe_object() {
    local kind="$1" name="$2" namespace="$3" manifest="$4" _kubectl_probe_uid=""
    _KUBECTL_PROBE_OBJECTS+=("$kind"$'\t'"$name"$'\t'"$namespace")
    _KUBECTL_PROBE_UIDS+=("")
    kubectl_create_owned_object _kubectl_probe_uid "$kind" "$name" "$namespace" \
        "$_KUBECTL_PROBE_NONCE" "$_KUBECTL_PROBE_ATTEMPT" "$manifest" || return 1
    _KUBECTL_PROBE_UIDS[-1]="$_kubectl_probe_uid"
}

# EXIT trap of kubectl_validate_sequencer_probe: delete every registered
# object in reverse creation order and keep the probe's exit status (1 if the
# probe passed but cleanup did not). Signals and a closed output pipe cannot
# stop the cleanup.
_kubectl_cleanup_sequencer_probe() {
    local rc=$? index kind name namespace
    trap '' PIPE INT TERM
    trap - EXIT
    for ((index = ${#_KUBECTL_PROBE_OBJECTS[@]} - 1; index >= 0; index--)); do
        IFS=$'\t' read -r kind name namespace <<< "${_KUBECTL_PROBE_OBJECTS[index]}"
        if ! _kubectl_remove_probe_object "$kind" "$name" "$namespace" \
                "${_KUBECTL_PROBE_UIDS[index]}"; then
            printf 'Error: could not delete sequencer probe %s %s/%s; delete it only if its storage-scale-test.nvidia.com/ownership annotation is %s\n' \
                "$kind" "$namespace" "$name" "$_KUBECTL_PROBE_NONCE" >&2
            [[ "$rc" -ne 0 ]] || rc=1
        fi
    done
    exit "$rc"
}

# Delete one probe object by its recorded UID, or, when its create never
# returned one, by its deterministic name after exact ownership verification.
_kubectl_remove_probe_object() {
    local kind="$1" name="$2" namespace="$3" uid="$4"
    if [[ -z "$uid" ]]; then
        _kubectl_observe_object_uid uid "$kind" "$name" "$namespace" || return 1
        [[ -n "$uid" ]] || return 0
    fi
    kubectl_delete_owned_object "$kind" "$name" "$namespace" "$_KUBECTL_PROBE_NONCE" \
        "$_KUBECTL_PROBE_ATTEMPT" "$uid"
}

# _kubectl_wait_sequencer_probe NAMESPACE JOB NONCE ATTEMPT UID [TIMEOUT]: wait
# until the probe Job is terminal. Up to three consecutive failed
# observations are retried within the deadline.
_kubectl_wait_sequencer_probe() {
    local namespace="$1" job_name="$2" nonce="$3" attempt_id="$4" uid="$5"
    local timeout="${6:-$KUBECTL_SEQUENCER_PROBE_TIMEOUT_SECONDS_DEFAULT}"
    local deadline=$((SECONDS + timeout)) terminal="" wait_rc failures=0
    while (( SECONDS < deadline )); do
        wait_rc=0
        KUBECTL_REQUEST_TIMEOUT_SECONDS=10 KUBECTL_PROCESS_TIMEOUT_SECONDS=15 \
            kubectl_job_terminal_state terminal "$job_name" "$namespace" "$nonce" \
                "$attempt_id" "$uid" || wait_rc=$?
        case "$wait_rc" in
            0) break ;;
            2) failures=0 ;;
            *)
                failures=$((failures + 1))
                (( failures < 3 )) || break
                ;;
        esac
        sleep 1
    done
    [[ "$terminal" == COMPLETE ]] && return 0
    if (( failures >= 3 )); then
        printf 'Error: could not observe the Kubernetes sequencer probe Job %s/%s\n' \
            "$namespace" "$job_name" >&2
    elif [[ -z "$terminal" ]]; then
        printf 'Error: the Kubernetes sequencer probe Job did not finish within %s seconds (KUBECTL_SEQUENCER_PROBE_TIMEOUT_SECONDS)\n' \
            "$timeout" >&2
    else
        echo "Error: the Kubernetes sequencer probe Job did not complete successfully" >&2
    fi
    _kubectl_print_runtime_validation_diagnostics "$namespace" "$job_name" \
        "$attempt_id" || printf 'Inspect before retrying: kubectl -n %q get pods -l %q -o wide\n' \
            "$namespace" "storage-scale-test.nvidia.com/run=$attempt_id" >&2
    return 1
}

# Validation only (validate_env.sh).
# kubectl_validate_sequencer_image: run the sequencer's image
# (kubectl_sequencer_image) once in a deadline-bounded Job in KUBECTL_NAMESPACE,
# with no ServiceAccount token and no API call: the image must pull and provide
# sh, kubectl, grep and sleep, and `kubectl version --client` must run. The
# wait is bounded by KUBECTL_SEQUENCER_PROBE_TIMEOUT_SECONDS and ends at once
# on a Pod waiting reason that cannot clear by itself (ErrImagePull,
# ImagePullBackOff, ErrImageNeverPull, InvalidImageName,
# CreateContainerConfigError). The Job is always deleted (same subshell and
# EXIT trap as kubectl_validate_sequencer_probe); a failure prints its reason
# on stderr and returns 1.
kubectl_validate_sequencer_image() {
    _kubectl_run_forwarding_signals _kubectl_run_sequencer_image_check
}

_kubectl_run_sequencer_image_check() {
    local namespace="${KUBECTL_NAMESPACE:-}" timeout job_name manifest selector_block rc=0
    timeout="${KUBECTL_SEQUENCER_PROBE_TIMEOUT_SECONDS:-$KUBECTL_SEQUENCER_PROBE_TIMEOUT_SECONDS_DEFAULT}"
    if ! _kubectl_validate_sequencer_poll_seconds "$timeout"; then
        printf 'Error: KUBECTL_SEQUENCER_PROBE_TIMEOUT_SECONDS=%q must be an integer from 1 to 3600\n' \
            "$timeout" >&2
        return 1
    fi
    kubectl_validate_sequencer_configuration 2 \
        && kubectl_validate_namespace_name "$namespace" || return 1
    _KUBECTL_PROBE_ATTEMPT=$(kubectl_generate_attempt_id) \
        && _KUBECTL_PROBE_NONCE=$(kubectl_generate_ownership_nonce) \
        && selector_block=$(kubectl_render_node_selector "$KUBECTL_NODE_SELECTOR") \
        || return 1
    job_name="sst-elb-$_KUBECTL_PROBE_ATTEMPT-sequencer-image"
    manifest=$(kubectl_render_attempt_template \
        "$(kubectl_template_directory)/sequencer-image-job.yaml.tmpl" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$job_name" \
        "ATTEMPT_ID=$_KUBECTL_PROBE_ATTEMPT" "OWNERSHIP_NONCE=$_KUBECTL_PROBE_NONCE" \
        "IMAGE=$(kubectl_sequencer_image)" \
        "IMAGE_PULL_POLICY=$KUBECTL_IMAGE_PULL_POLICY" \
        "RUN_AS_USER=$KUBECTL_RUN_AS_USER" "RUN_AS_GROUP=$KUBECTL_RUN_AS_GROUP" \
        "ACTIVE_DEADLINE_SECONDS=$((timeout + 60))" \
        "NODE_SELECTOR_BLOCK=$selector_block") || return 1
    _KUBECTL_PROBE_OBJECTS=()
    _KUBECTL_PROBE_UIDS=()
    _kubectl_exit_on_signal _kubectl_cleanup_sequencer_probe
    _kubectl_create_probe_object Job "$job_name" "$namespace" "$manifest" || rc=1
    if [[ "$rc" -eq 0 ]]; then
        _kubectl_wait_sequencer_image "$namespace" "$job_name" \
            "${_KUBECTL_PROBE_UIDS[-1]}" "$timeout" || rc=1
    fi
    # The EXIT trap deletes the Job and keeps this status.
    exit "$rc"
}

# _kubectl_pod_fatal_waiting_reason NAMESPACE ATTEMPT: print "REASON: MESSAGE"
# of the first Pod of validation attempt ATTEMPT with a waiting reason that
# cannot clear by itself; return 1 when there is none.
_kubectl_pod_fatal_waiting_reason() {
    local namespace="$1" attempt_id="$2" rows reason message
    rows=$(KUBECTL_OBSERVATION_ATTEMPTS=1 KUBECTL_REQUEST_TIMEOUT_SECONDS=10 \
        KUBECTL_PROCESS_TIMEOUT_SECONDS=15 kubectl_run_observational -n "$namespace" \
        get pods -l "storage-scale-test.nvidia.com/run=$attempt_id" \
        -o 'jsonpath={range .items[*]}{range .status.containerStatuses[*]}{.state.waiting.reason}{"\t"}{.state.waiting.message}{"\n"}{end}{end}' \
        2>/dev/null) || return 1
    while IFS=$'\t' read -r reason message; do
        case "$reason" in
            ErrImagePull|ImagePullBackOff|ErrImageNeverPull|InvalidImageName|CreateContainerConfigError)
                printf '%s%s\n' "$reason" "${message:+: ${message:0:300}}"
                return 0
                ;;
        esac
    done <<< "$rows"
    return 1
}

# _kubectl_wait_sequencer_image NAMESPACE JOB UID TIMEOUT: return 0 once the
# check Job completes; otherwise print why it did not (a fatal waiting reason
# at once, a failed run with the last log lines, a timeout) and return 1. Up to
# three consecutive failed observations are retried within the deadline.
_kubectl_wait_sequencer_image() {
    local namespace="$1" job_name="$2" uid="$3" timeout="$4"
    local deadline=$((SECONDS + timeout)) terminal="" wait_rc failures=0 reason logs
    while (( SECONDS < deadline )); do
        wait_rc=0
        KUBECTL_REQUEST_TIMEOUT_SECONDS=10 KUBECTL_PROCESS_TIMEOUT_SECONDS=15 \
            kubectl_job_terminal_state terminal "$job_name" "$namespace" \
                "$_KUBECTL_PROBE_NONCE" "$_KUBECTL_PROBE_ATTEMPT" "$uid" || wait_rc=$?
        case "$wait_rc" in
            0) break ;;
            2)
                failures=0
                if reason=$(_kubectl_pod_fatal_waiting_reason "$namespace" \
                        "$_KUBECTL_PROBE_ATTEMPT"); then
                    printf 'the Pod cannot start the image: %s\n' "$reason" >&2
                    return 1
                fi
                ;;
            *)
                failures=$((failures + 1))
                (( failures < 3 )) || break
                ;;
        esac
        sleep 1
    done
    [[ "$terminal" == COMPLETE ]] && return 0
    if (( failures >= 3 )); then
        printf 'could not observe the check Job %s/%s\n' "$namespace" "$job_name" >&2
    elif [[ -z "$terminal" ]]; then
        printf 'the check did not finish within %s seconds (KUBECTL_SEQUENCER_PROBE_TIMEOUT_SECONDS)\n' \
            "$timeout" >&2
    else
        logs=$(KUBECTL_OBSERVATION_ATTEMPTS=1 KUBECTL_REQUEST_TIMEOUT_SECONDS=10 \
            KUBECTL_PROCESS_TIMEOUT_SECONDS=15 kubectl_run_observational -n "$namespace" \
            logs -l "storage-scale-test.nvidia.com/run=$_KUBECTL_PROBE_ATTEMPT" \
            --tail=5 2>&1 | head -c 600) || logs=""
        printf 'the check failed in the image%s\n' "${logs:+: ${logs//$'\n'/; }}" >&2
    fi
    return 1
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
    local -a claim_volumes=()
    remote_run=$(kubectl_attempt_remote_root "$run_id") || return 1
    # A transfer helper checks paths and access on every claim of the selected
    # segment, so it mounts them all. Status and collector helpers touch only
    # the segment's run tree: they mount its control claim alone, so another
    # claim deleted later never keeps them from starting (cluster identity
    # checks still report that claim).
    if [[ "$template_name" == transfer ]]; then
        _kubectl_segment_claim_volume_arguments claim_volumes pod || return 1
    else
        _kubectl_control_claim_volume_arguments claim_volumes || return 1
    fi
    manifest=$(kubectl_render_attempt_template "$template" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$name" "ATTEMPT_ID=$run_id" \
        "OWNERSHIP_NONCE=$nonce" "OPERATION_TOKEN=$operation_token" \
        "REMOTE_RUN_DIRECTORY=$remote_run" \
        "IMAGE=$KUBECTL_ELBENCHO_IMAGE" "IMAGE_PULL_POLICY=$KUBECTL_IMAGE_PULL_POLICY" \
        "RUN_AS_USER=$KUBECTL_RUN_AS_USER" "RUN_AS_GROUP=$KUBECTL_RUN_AS_GROUP" \
        "NODE_NAME=$node_name" "${claim_volumes[@]}" \
        "SEGMENT_INDEX=$(_kubectl_selected_segment)") || return 1
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
    # Ordinary remote commands must not inherit an interactive caller's
    # terminal. kubectl exec -i waits for terminal input even when the remote
    # command consumes none, which can strand submission until its deadline.
    kubectl_run_bounded -n "$namespace" exec "$pod_name" -- "$@"
}

kubectl_pvc_exec_stdin() {
    local namespace="$1" pod_name="$2"
    shift 2
    kubectl_validate_namespace_name "$namespace" && kubectl_validate_object_name "$pod_name" \
        || return 1
    # Only callers that intentionally stream a finite payload may attach
    # stdin. They retain the same bounded kubectl process deadline.
    kubectl_run_bounded -n "$namespace" exec -i "$pod_name" -- "$@"
}

# _kubectl_validate_segment_mapped_paths PATH...: every Pod path lies below
# the mount root of one of the selected segment's claims (when an attempt
# identity is loaded), as kubectl_mapped_mount_root defines it.
_kubectl_validate_segment_mapped_paths() {
    local path mount
    local -a claims=()
    [[ -z "${KUBECTL_SEGMENT_COUNT:-}" ]] \
        || mapfile -t claims < <(kubectl_segment_claims "$(_kubectl_selected_segment)")
    for path in "$@"; do
        kubectl_mapped_mount_root "$path" mount || return 1
        (( ${#claims[@]} == 0 )) \
            || printf '%s\n' "${claims[@]}" | grep -Fqx -- "${mount##*/}" || {
                printf 'Error: %s is not on a claim of this segment (%s)\n' \
                    "$path" "${claims[*]}" >&2
                return 1
            }
    done
}

kubectl_validate_pvc_paths() {
    local namespace="$1" pod_name="$2"
    shift 2
    [[ $# -gt 0 ]] || return 1
    _kubectl_validate_segment_mapped_paths "$@" || return 1
    # Resolve the nearest existing parent as well as the future path. This
    # rejects a symlinked component before benchmark creation can escape its
    # claim, while preserving support for intentionally not-yet-created paths.
    # Each path must stay on its own claim, mounted at MOUNTS/<claim>.
    kubectl_validate_saved_control_layout || return 1
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu '
        mounts=$1
        reserved=$(realpath -m -- "$2")
        shift 2
        for candidate in "$@"; do
            claim=${candidate#"$mounts/"}
            claim=${claim%%/*}
            [[ -n "$claim" && "$candidate" == "$mounts/$claim/"* ]] || exit 1
            root=$(realpath -e -- "$mounts/$claim")
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
                *) printf "path escapes its claim mount: %s\\n" "$candidate" >&2; exit 1 ;;
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
            fi
        done
    ' bash "$KUBECTL_SWEEP_MOUNT_ROOT" "$KUBECTL_CONTROL_ROOT" "$@"
}

kubectl_validate_test_root_access() {
    local namespace="$1" pod_name="$2"
    shift 2
    [[ $# -gt 0 ]] || return 1
    _kubectl_validate_segment_mapped_paths "$@" || return 1
    # The configured roots are the only administrator-facing writable paths.
    # Probe them without creating the durable .storage-scale-test subtree.
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu '
        mounts=$1
        shift
        for candidate in "$@"; do
            claim=${candidate#"$mounts/"}
            claim=${claim%%/*}
            [[ -n "$claim" && "$candidate" == "$mounts/$claim/"* ]] || exit 1
            mount=$(realpath -e -- "$mounts/$claim") || exit 1
            [[ -d "$candidate" && -w "$candidate" ]] || {
                printf "configured TEST_DIRS path is not a writable directory: %s\\n" \
                    "$candidate" >&2
                exit 1
            }
            resolved=$(realpath -e -- "$candidate") || exit 1
            case "$resolved" in "$mount"/*) ;; *) exit 1 ;; esac
            probe=$(mktemp "$candidate/.sst-validation.XXXXXXXX") || exit 1
            printf "storage-scale-test-validation\\n" > "$probe" || exit 1
            [[ $(cat -- "$probe") == storage-scale-test-validation ]] || exit 1
            rm -f -- "$probe" || exit 1
        done
    ' bash "$KUBECTL_SWEEP_MOUNT_ROOT" "$@"
}

kubectl_reserve_remote_attempt() {
    local namespace="$1" pod_name="$2" attempt_id="$3" nonce="$4"
    kubectl_validate_namespace_name "$namespace" \
        && kubectl_validate_object_name "$pod_name" \
        && [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && "$nonce" =~ ^[0-9a-f]{32}$ ]] || return 1
    local remote_run
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu '
        run=$1 attempt=$2 nonce=$3 root=$4 test_root=$5
        mounts=/mnt/storage-scale-test
        [[ "$test_root" == "$mounts/"*/* && "$root" == "$test_root/.storage-scale-test" \
            && "$run" == "$root/runs/$attempt" ]] || exit 1
        claim=${test_root#"$mounts/"}
        mount=$mounts/${claim%%/*}
        mount_real=$(realpath -e -- "$mount") || exit 1
        test_real=$(realpath -e -- "$test_root") || exit 1
        case "$test_real" in "$mount_real"/*) ;; *) exit 1 ;; esac
        test -w "$test_root" || exit 1
        umask 077
        pending="$root/runs/.${attempt}.pending.$nonce"
        made_pending=0 made_run=0
        cleanup() {
            [[ $made_run -eq 0 ]] || rm -rf -- "$run"
            [[ $made_pending -eq 0 ]] || rm -rf -- "$pending"
        }
        trap cleanup EXIT
        for path in "$root" "$root/runs"; do
            if [[ -e "$path" || -L "$path" ]]; then
                [[ -d "$path" && ! -L "$path" ]] || exit 1
            else
                mkdir -- "$path"
            fi
        done
        root_real=$(realpath -e -- "$root") || exit 1
        [[ "$root_real" == "$test_real/.storage-scale-test" ]] || exit 1
        [[ ! -e "$pending" && ! -L "$pending" ]] || exit 1
        # Every attempt ID is new, so an existing run tree means another
        # segment of this attempt reaches the same storage at this path.
        if [[ -e "$run" || -L "$run" ]]; then
            printf "attempt run tree already exists: %s\\n" "$run" >&2
            exit 1
        fi
        mkdir -- "$pending"
        made_pending=1
        printf "%s\\t%s\\n" "$attempt" "$nonce" > "$pending/owner"
        mv -T -n -- "$pending" "$run"
        [[ ! -e "$pending" && ! -L "$pending" ]] || exit 1
        made_pending=0
        made_run=1
        trap - EXIT
    ' bash "$remote_run" "$attempt_id" "$nonce" "$KUBECTL_CONTROL_ROOT" \
        "$KUBECTL_CONTROL_TEST_ROOT"
}

kubectl_attempt_journal_remote_reservation() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" nonce="$4"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && "$nonce" =~ ^[0-9a-f]{32}$ ]] || return 1
    local attempt_dir="$kubernetes_dir/attempts/$attempt_id" suffix
    kubectl_attempt_load_identity "$attempt_dir" || return 1
    [[ "$KUBECTL_OWNERSHIP_NONCE" == "$nonce" ]] || return 1
    suffix=$(_kubectl_selected_segment_suffix) || return 1
    local reservation_file="$attempt_dir/remote-reservation$suffix.sh"
    [[ ! -e "$reservation_file" && ! -L "$reservation_file" ]] || return 1
    local remote_run tmp
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    tmp="$reservation_file.tmp.${BASHPID:-$$}.$RANDOM"
    {
        printf '# Trusted storage-scale-test Kubernetes remote reservation.\n'
        printf 'KUBECTL_REMOTE_ATTEMPT_ID=%q\n' "$attempt_id"
        printf 'KUBECTL_REMOTE_OWNERSHIP_NONCE=%q\n' "$nonce"
        printf 'KUBECTL_REMOTE_RUN_DIRECTORY=%q\n' "$remote_run"
        printf 'KUBECTL_REMOTE_CONTROL_ROOT=%q\n' "$KUBECTL_CONTROL_ROOT"
        printf 'KUBECTL_REMOTE_TEST_ROOT=%q\n' "$KUBECTL_CONTROL_TEST_ROOT"
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

# The selected segment's reservation (see _kubectl_selected_segment).
kubectl_attempt_load_remote_reservation() {
    local kubernetes_dir="$1" attempt_id="$2" suffix
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    suffix=$(_kubectl_selected_segment_suffix) || return 1
    local reservation_file="$kubernetes_dir/attempts/$attempt_id/remote-reservation$suffix.sh"
    [[ -f "$reservation_file" && ! -L "$reservation_file" ]] || return 1
    unset KUBECTL_REMOTE_ATTEMPT_ID KUBECTL_REMOTE_OWNERSHIP_NONCE
    unset KUBECTL_REMOTE_RUN_DIRECTORY KUBECTL_REMOTE_CONTROL_ROOT
    unset KUBECTL_REMOTE_TEST_ROOT
    unset KUBECTL_REMOTE_RESERVATION_PHASE
    # shellcheck disable=SC1090  # Trusted, result-directory-local reservation.
    source "$reservation_file" || return 1
    [[ "$KUBECTL_REMOTE_ATTEMPT_ID" == "$attempt_id" \
        && "$KUBECTL_REMOTE_OWNERSHIP_NONCE" =~ ^[0-9a-f]{32}$ \
        && "$KUBECTL_REMOTE_RESERVATION_PHASE" =~ ^(INTENDED|ACQUIRED)$ ]] || return 1
    [[ "$KUBECTL_REMOTE_RUN_DIRECTORY" == "$(kubectl_attempt_remote_root "$attempt_id")" \
        && "$KUBECTL_REMOTE_CONTROL_ROOT" == "$KUBECTL_CONTROL_ROOT" \
        && "$KUBECTL_REMOTE_TEST_ROOT" == "$KUBECTL_CONTROL_TEST_ROOT" ]]
}

kubectl_attempt_mark_remote_reservation_acquired() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_attempt_load_remote_reservation "$kubernetes_dir" "$attempt_id" || return 1
    [[ "$KUBECTL_REMOTE_RESERVATION_PHASE" == INTENDED ]] || return 1
    local suffix
    suffix=$(_kubectl_selected_segment_suffix) || return 1
    local reservation_file="$kubernetes_dir/attempts/$attempt_id/remote-reservation$suffix.sh"
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
    local remote_run control_root test_root step
    step="release-remote-run$(_kubectl_selected_segment_suffix)" || return 1
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    control_root="$KUBECTL_CONTROL_ROOT"
    test_root="$KUBECTL_CONTROL_TEST_ROOT"
    if [[ -n "$kubernetes_dir" || -n "$lock_fd" ]]; then
        _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
        kubectl_attempt_load_remote_reservation "$kubernetes_dir" "$attempt_id" || return 1
        [[ "$KUBECTL_REMOTE_OWNERSHIP_NONCE" == "$nonce" ]] || return 1
        remote_run="$KUBECTL_REMOTE_RUN_DIRECTORY"
        control_root="$KUBECTL_REMOTE_CONTROL_ROOT"
        test_root="$KUBECTL_REMOTE_TEST_ROOT"
    fi
    if [[ -z "$kubernetes_dir" ]] || ! kubectl_attempt_step_done \
            "$kubernetes_dir" "$attempt_id" "$step"; then
        # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
        kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu '
            run=$1 attempt=$2 nonce=$3 root=$4 test_root=$5
            expected=$(printf "%s\\t%s" "$attempt" "$nonce")
            pending="$root/runs/.${attempt}.pending.$nonce"
            mounts=/mnt/storage-scale-test
            [[ "$test_root" == "$mounts/"*/* && "$root" == "$test_root/.storage-scale-test" \
                && "$run" == "$root/runs/$attempt" ]] || exit 1
            claim=${test_root#"$mounts/"}
            mount=$mounts/${claim%%/*}
            mount_real=$(realpath -e -- "$mount") || exit 1
            test_real=$(realpath -e -- "$test_root") || exit 1
            case "$test_real" in "$mount_real"/*) ;; *) exit 1 ;; esac
            if [[ ! -e "$root" && ! -L "$root" ]]; then
                [[ ! -e "$pending" && ! -L "$pending" && ! -e "$run" && ! -L "$run" ]]
                exit
            fi
            [[ -d "$root" && ! -L "$root" ]] || exit 1
            root_real=$(realpath -e -- "$root") || exit 1
            [[ "$root_real" == "$test_real/.storage-scale-test" ]] || exit 1
            if [[ -e "$pending" || -L "$pending" ]]; then
                [[ -d "$pending" && ! -L "$pending" ]] || exit 1
                if [[ -e "$pending/owner" || -L "$pending/owner" ]]; then
                    entry_count=0
                    for entry in "$pending"/* "$pending"/.[!.]* "$pending"/..?*; do
                        [[ -e "$entry" || -L "$entry" ]] || continue
                        entry_count=$((entry_count + 1))
                    done
                    [[ -f "$pending/owner" && ! -L "$pending/owner" \
                        && $(cat -- "$pending/owner") == "$expected" \
                        && "$entry_count" -eq 1 ]] \
                        || exit 1
                    rm -- "$pending/owner" || exit 1
                fi
                rmdir -- "$pending"
                [[ ! -e "$pending" && ! -L "$pending" ]] || exit 1
            fi
            if [[ ! -e "$run" && ! -L "$run" ]]; then
                exit 0
            fi
            if [[ -e "$run" || -L "$run" ]]; then
                [[ -d "$run" && ! -L "$run" ]] || exit 1
                run_real=$(realpath -e -- "$run") || exit 1
                [[ "$run_real" == "$root_real/runs/$attempt" ]] || exit 1
                [[ -f "$run/owner" && ! -L "$run/owner" \
                    && $(cat -- "$run/owner") == "$expected" ]] || exit 1
            fi
            rm -rf -- "$run"
            [[ ! -e "$run" && ! -L "$run" ]] || exit 1
            rmdir -- "$root/runs" 2>/dev/null || true
            rmdir -- "$root" 2>/dev/null || true
        ' bash "$remote_run" "$attempt_id" "$nonce" "$control_root" \
            "$test_root" || return 1
        [[ -z "$kubernetes_dir" ]] || kubectl_attempt_journal_step \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" "$step" || return 1
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
    local rows selector
    selector=$(kubectl_worker_pod_selector "$run_id") || return 1
    rows=$(kubectl_run_observational -n "$namespace" get pods -l "$selector" \
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
    local daemonset_name
    daemonset_name=$(kubectl_segment_resource_name "$run_id" \
        "${KUBECTL_ACTIVE_SEGMENT:-0}" workers) || daemonset_name="sst-elb-$run_id-workers"
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
    local sweep_key
    sweep_key=$(_kubectl_selected_segment_key sweep) || return 1
    kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$sweep_key" || return 1
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
                || kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" \
                    "delete-$sweep_key"
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
            "$KUBECTL_RESOURCE_UID" "$observed_uid" "" \
            "${terminal_state:-ACTIVE}" || true
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

# Print NOT_STARTED when no coordinator ever touched the attempt's run tree on
# this claim (run.status is still PREPARED and there is no coordinator.lock,
# which a coordinator creates before it changes anything), else STARTED.
# Lost-coordinator recovery and --finalize-cancelled create that lock too.
kubectl_read_remote_start_evidence() {
    local namespace="$1" pod_name="$2" attempt_id="$3"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    local remote_run guard_script evidence
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    guard_script=$(kubectl_remote_tree_guard_script) || return 1
    # shellcheck disable=SC2016  # The quoted script executes in the helper Pod.
    evidence=$(kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu \
        "$guard_script
        status=\$run/state/run.status
        lock=\$run/state/coordinator.lock
        [[ -f \"\$status\" && ! -L \"\$status\" ]] || exit 1
        if [[ \"\$(cat -- \"\$status\")\" == PREPARED && ! -e \"\$lock\" && ! -L \"\$lock\" ]]; then
            printf 'NOT_STARTED\n'
        else
            printf 'STARTED\n'
        fi" bash "$remote_run" "$attempt_id") || return 1
    [[ "$evidence" =~ ^(NOT_STARTED|STARTED)$ ]] || return 1
    printf '%s\n' "$evidence"
}

# Read the current attempt only: resumed attempts omit previously successful
# cells. Each status is atomic, but these counts are a live, non-atomic snapshot.
kubectl_read_remote_execution_progress() {
    local namespace="$1" pod_name="$2" attempt_id="$3" remote_state="$4"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ \
        && "$remote_state" =~ ^(PREPARED|RUNNING|SUCCESS|FAILED|CANCELLED)$ ]] || return 1
    local remote_run guard_script
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    guard_script=$(kubectl_remote_tree_guard_script) || return 1
    local progress_script
    # shellcheck disable=SC2016  # This script runs in the Linux helper Pod.
    progress_script='
remote_state=$3
for directory in control control/executions state; do
    [[ -d "$run/$directory" && ! -L "$run/$directory" \
        && "$(realpath -e -- "$run/$directory")" == "$run_real/$directory" ]] || exit 1
done
ledger=$run/state/executions
if [[ -e "$ledger" || -L "$ledger" ]]; then
    [[ -d "$ledger" && ! -L "$ledger" \
        && "$(realpath -e -- "$ledger")" == "$run_real/state/executions" ]] || exit 1
else
    [[ "$remote_state" == PREPARED ]] || exit 1
fi
shopt -s nullglob
definitions=("$run"/control/executions/[0-9][0-9][0-9][0-9].sh)
[[ ${#definitions[@]} -gt 0 && ${#definitions[@]} -le 9999 ]] || exit 1
pending=0 running=0 success=0 failed=0 first_failed=""
for definition in "${definitions[@]}"; do
    [[ -f "$definition" && ! -L "$definition" ]] || exit 1
    id=${definition##*/}
    status_file=$ledger/${id%.sh}.status
    if [[ ! -e "$status_file" && ! -L "$status_file" && "$remote_state" == PREPARED ]]; then
        value=PENDING
    else
        [[ -f "$status_file" && ! -L "$status_file" ]] || exit 1
        value=$(< "$status_file") || exit 1
    fi
    case "$value" in
        PENDING) pending=$((pending + 1)) ;;
        RUNNING) running=$((running + 1)) ;;
        SUCCESS) success=$((success + 1)) ;;
        FAILED) failed=$((failed + 1)); first_failed=${first_failed:-${id%.sh}} ;;
        *) echo "Error: invalid Kubernetes execution status: ${id%.sh}" >&2; exit 1 ;;
    esac
done
printf "%s %s %s %s%s\n" "$pending" "$running" "$success" "$failed" "${first_failed:+ $first_failed}"
'
    kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu \
        "$guard_script
$progress_script" bash "$remote_run" "$attempt_id" "$remote_state"
}

kubectl_read_refreshed_execution_progress() {
    local namespace="$1" inspector="$2" attempt="$3" state="$4" refreshed progress
    progress=$(kubectl_read_remote_execution_progress "$namespace" "$inspector" \
        "$attempt" "$state") || return $?
    # Refresh after the ledger scan so an older RUNNING observation cannot
    # conceal a terminal outcome committed during the intervening API calls.
    refreshed=$(kubectl_read_remote_status "$namespace" "$inspector" "$attempt") || return $?
    if [[ "$refreshed" != "$state" ]]; then
        progress=$(kubectl_read_remote_execution_progress "$namespace" "$inspector" \
            "$attempt" "$refreshed") || return $?
    fi
    printf '%s %s\n' "$refreshed" "$progress"
}

kubectl_stream_remote_attempt() {
    local namespace="$1" pod_name="$2" attempt_id="$3" archive_path="$4"
    local local_state_path="${5:-}" remote_state_path="${6:-}"
    local job_kind="${7:-}" job_name="${8:-}" job_namespace="${9:-}"
    local job_uid="${10:-}" job_evidence="${11:-}"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ && -n "$archive_path" \
        && ! -e "$archive_path" && ! -L "$archive_path" ]] || return 1
    local collection_timeout="${KUBECTL_COLLECTION_TIMEOUT_SECONDS:-$KUBECTL_COLLECTION_TIMEOUT_SECONDS_DEFAULT}"
    local backoff="${KUBECTL_COLLECTION_RETRY_BACKOFF_SECONDS:-2}"
    [[ "$collection_timeout" =~ ^[1-9][0-9]*$ && "$backoff" =~ ^[0-9]+$ ]] || return 1
    # Reserve room for another transfer after a hung producer. Only transfers
    # before the first producer timeout are capped; the following transfer may
    # use the remaining total deadline so a slow archive is not cut short twice.
    local attempt_timeout="${KUBECTL_COLLECTION_ATTEMPT_TIMEOUT_SECONDS:-$((collection_timeout / KUBECTL_COLLECTION_STREAM_ATTEMPTS))}"
    [[ "$attempt_timeout" == 0 ]] && attempt_timeout=1
    [[ "$attempt_timeout" =~ ^[1-9][0-9]*$ ]] || return 1
    local remote_run error_path
    remote_run=$(kubectl_attempt_remote_root "$attempt_id") || return 1
    local guard_script
    guard_script=$(kubectl_remote_tree_guard_script) || return 1
    error_path=$(mktemp "${archive_path%/*}/.kubectl-collect-stream.XXXXXXXX") \
        || return 1
    if [[ "${STORAGE_SCALE_TEST_INTEGRATION:-}" == 1 \
            && -n "${KUBECTL_INTEGRATION_COLLECTION_HOLD_FILE:-}" ]]; then
        local hold_file="$KUBECTL_INTEGRATION_COLLECTION_HOLD_FILE"
        local results_real hold_parent hold_temporary
        results_real=$(_kubectl_local_realpath_existing "${archive_path%/*}/..") \
            || return 1
        hold_parent=$(_kubectl_local_realpath_existing "${hold_file%/*}") || return 1
        [[ "$hold_parent" == "$results_real" \
            && "${hold_file##*/}" == .integration-kubectl-collection-ready \
            && ! -e "$hold_file" && ! -L "$hold_file" ]] || return 1
        hold_temporary="$hold_file.${BASHPID:-$$}.tmp"
        (umask 077; printf 'ready\n' > "$hold_temporary") \
            && mv -- "$hold_temporary" "$hold_file" || return 1
        while [[ -f "$hold_file" && ! -L "$hold_file" ]]; do
            sleep 0.1
        done
        [[ ! -e "$hold_file" && ! -L "$hold_file" ]] || return 1
    fi
    local deadline=$((SECONDS + collection_timeout)) remaining attempt delay
    local reason=TRANSFER_FAILED action diagnostic_path="" producer_output attempt_capped=1
    local -a stream_status=()
    for ((attempt = 1; attempt <= KUBECTL_COLLECTION_STREAM_ATTEMPTS; attempt++)); do
        remaining=$((deadline - SECONDS))
        if ((remaining <= 0)); then
            reason=TIMEOUT
            break
        fi
        ((attempt_capped == 0 || remaining <= attempt_timeout)) || remaining=$attempt_timeout
        _kubectl_stream_attempt_once "$namespace" "$pod_name" "$attempt_id" \
            "$archive_path" "$error_path" "$guard_script" "$remote_run" \
            "$remaining" stream_status || stream_status=(1 74)
        if [[ "${stream_status[0]}" -eq 0 && "${stream_status[1]}" -eq 0 ]]; then
            if [[ -n "$diagnostic_path" ]]; then
                _kubectl_preserve_stream_error "$local_state_path" "$attempt_id" \
                    "$namespace" "$pod_name" "$attempt" "$error_path" 0 0 \
                    SUCCESS diagnostic_path || echo 'Warning: successful collection retry could not be journaled' >&2
            fi
            rm -f -- "$error_path" || return 1
            return 0
        fi
        producer_output=$(cat -- "$error_path") || producer_output=""
        [[ "${stream_status[0]}" -ne 124 ]] || attempt_capped=0
        reason=TRANSFER_FAILED
        if [[ "${stream_status[1]}" -eq 65 ]]; then
            reason=ARCHIVE_INVALID
        elif [[ "${stream_status[1]}" -ne 0 ]]; then
            reason=LOCAL_IO
        else
            _kubectl_classify_observation_failure reason "${stream_status[0]}" \
                "$producer_output" || reason=TRANSFER_FAILED
            [[ "$reason" != API_UNAVAILABLE ]] || reason=TRANSFER_FAILED
            if [[ "$reason" != AUTH ]] \
                    && grep -Eqi 'command terminated|tar:' <<< "$producer_output"; then
                reason=TRANSFER_FAILED
            elif grep -Eqi 'path.*(escapes|unsafe)|symlink|symbolic link' <<< "$producer_output"; then
                reason=PATH_REJECTED
            elif [[ -z "$producer_output" && "${stream_status[0]}" -ne 124 ]]; then
                reason=TRANSFER_FAILED
            elif [[ "$reason" == TIMEOUT && "${stream_status[0]}" -ne 124 ]] \
                    && ! _kubectl_collection_failure_is_transient "$producer_output"; then
                reason=TRANSFER_FAILED
            fi
        fi
        # Print evidence before best-effort persistence; capture failure must
        # never replace the transfer error or prevent safe partial cleanup.
        printf 'Kubernetes collection transfer %d/%d: attempt=%s producer_rc=%s consumer_rc=%s reason=%s\n' \
            "$attempt" "$KUBECTL_COLLECTION_STREAM_ATTEMPTS" "$attempt_id" \
            "${stream_status[0]}" "${stream_status[1]}" "$reason" >&2
        cat -- "$error_path" >&2 || true
        _kubectl_preserve_stream_error "$local_state_path" "$attempt_id" \
            "$namespace" "$pod_name" "$attempt" "$error_path" \
            "${stream_status[0]}" "${stream_status[1]}" "$reason" \
            diagnostic_path || echo 'Warning: collection transfer diagnostics could not be saved' >&2
        rm -f -- "$archive_path" || { reason=LOCAL_IO; break; }
        if ((attempt >= KUBECTL_COLLECTION_STREAM_ATTEMPTS)) \
                || [[ "${stream_status[1]}" -ne 0 ]] \
                || ! _kubectl_collection_transfer_is_retryable \
                    "$attempt_id" "${stream_status[0]}" "$producer_output"; then
            break
        fi
        delay=$((backoff * (1 << (attempt - 1))))
        ((backoff == 0)) || delay=$((delay + RANDOM % (backoff + 1)))
        remaining=$((deadline - SECONDS))
        if ((remaining <= delay)); then
            reason=TIMEOUT
            break
        fi
        printf 'Warning: retryable collection transfer failed; retrying in %s seconds (remote results retained)\n' \
            "$delay" >&2
        sleep "$delay" || break
    done
    case "$reason" in
        ARCHIVE_INVALID) action="inspect the remote attempt size before retrying --collect" ;;
        LOCAL_IO) action="repair local result storage and retry --collect" ;;
        TRANSFER_FAILED) action="inspect the captured transfer error before retrying --collect" ;;
        *) action=$(_kubectl_observation_safe_action "$reason") ;;
    esac
    kubectl_report_collection_failure "$attempt_id" archive-stream "$reason" \
        "$action; remote results were retained" "$local_state_path" \
        "${remote_state_path:-$remote_run/state/run.status}" "$diagnostic_path" \
        "$job_kind" "$job_name" "$job_namespace" "$job_uid" "" "$job_evidence"
    rm -f -- "$error_path"
    return 1
}

# A changed-source archive is never imported. Retry only tar's specific
# changed-file warning, not missing files, read errors, or mixed diagnostics.
# NFS attribute revalidation can trigger this even for quiescent files. A
# fresh transfer must still pass all publication hashes before any cleanup.
_kubectl_collection_transfer_is_retryable() {
    local attempt_id="$1" producer_rc="$2" output="$3" line changed_path warnings=0
    if [[ "$producer_rc" -eq 124 && -z "$output" ]]; then
        return 0
    fi
    if _kubectl_collection_failure_is_transient "$output"; then
        return 0
    fi
    [[ "$producer_rc" -eq 1 && "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        if [[ "$line" == "command terminated with exit code 1" ]]; then
            continue
        fi
        [[ "$line" == "tar: "*": file changed as we read it" ]] || return 1
        changed_path="${line#tar: }"
        changed_path="${changed_path%: file changed as we read it}"
        [[ "$changed_path" == "$attempt_id/state" \
            || "$changed_path" == "$attempt_id/state/"* ]] \
            && _kubectl_safe_archive_member "$changed_path" || return 1
        warnings=$((warnings + 1))
    done <<< "$output"
    ((warnings > 0))
}

# Do not mistake remote tar errors, auth denial, or an arbitrary killed
# process for a transient transport fault, even if they mention a timeout.
_kubectl_collection_failure_is_transient() {
    local output="$1"
    if grep -Eqi 'unauthorized|forbidden|authentication|credentials|not found|notfound|identity|unsafe|escapes|symlink|symbolic link|permission denied|command terminated|tar:|no such file|corrupt|checksum' <<< "$output"; then
        return 1
    fi
    grep -Eqi 'connection (refused|reset|closed)|i/o timeout|TLS handshake timeout|net/http:.*timeout|error:.*(unexpected EOF|context deadline exceeded)|error: EOF|too many requests|(^|[^0-9])429([^0-9]|$)|service unavailable|internal server error|bad gateway|gateway timeout|(^|[^0-9])50[0234]([^0-9]|$)|http2.*(client connection lost|stream closed)|stream error.*(INTERNAL_ERROR|REFUSED_STREAM)' <<< "$output"
}

_kubectl_preserve_stream_error() {
    local state_path="$1" attempt_id="$2" namespace="$3" pod="$4" ordinal="$5"
    local source="$6" producer="$7" consumer="$8" reason="$9" output_name="${10}"
    local metadata_dir="${state_path%/*}" root destination
    [[ -n "$state_path" && -d "$metadata_dir" && ! -L "$metadata_dir" ]] || return 1
    _kubectl_validate_local_directory_path "$metadata_dir" || return 1
    root="$metadata_dir/diagnostics"
    [[ ! -L "$root" ]] && mkdir -p -- "$root" || return 1
    local -n saved_path="$output_name"
    destination="$saved_path"
    if [[ -z "$destination" ]]; then
        destination=$(mktemp -d "$root/collection-stream.XXXXXXXX") || return 1
        printf 'schema\t%s\nattempt_id\t%s\nnamespace\t%s\npod\t%s\n' \
            "$KUBECTL_DIAGNOSTIC_SCHEMA_VERSION" "$attempt_id" "$namespace" "$pod" \
            > "$destination/bundle.tsv" || return 1
        saved_path="$destination"
        kubectl_capture_retained_resource_identities "$metadata_dir" "$attempt_id" \
            "$destination/retained-resource-identities.tsv" || true
    fi
    cp -- "$source" "$destination/transfer-$ordinal.stderr" || return 1
    printf '%s\t%s\t%s\t%s\t%s\n' "$ordinal" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$producer" "$consumer" "$reason" >> "$destination/transfers.tsv" || return 1
    _kubectl_prune_diagnostic_bundles "$root" "$destination" || return 1
    printf 'Kubernetes collection transfer diagnostics: %s\n' "$destination" >&2
}

_kubectl_stream_attempt_once() {
    local namespace="$1" pod_name="$2" attempt_id="$3" archive_path="$4"
    local error_path="$5" guard_script="$6" remote_run="$7" remaining="$8"
    local -n transfer_status="$9"
    local umask_previous
    umask_previous=$(umask)
    umask 077
    local error_fifo="$error_path.fifo" error_reader_pid
    mkfifo -m 600 -- "$error_fifo" || {
        umask "$umask_previous"
        rm -f -- "$error_path"
        return 1
    }
    {
        head -c "$KUBECTL_COLLECTION_ERROR_MAX_BYTES" > "$error_path"
        cat > /dev/null
    } < "$error_fifo" &
    error_reader_pid=$!
    # A valid collection can contain up to two GiB. Do not inherit the short
    # API-probe deadline used for status and object inspection. Keep at most
    # one byte beyond the limit so an oversized or unbounded producer is
    # stopped without first filling the local filesystem.
    KUBECTL_REQUEST_TIMEOUT_SECONDS="$remaining" \
        KUBECTL_PROCESS_TIMEOUT_SECONDS="$remaining" \
        kubectl_pvc_exec "$namespace" "$pod_name" /bin/bash -ceu "$guard_script
            # Only the manifest-published state is collected. Control files
            # are already retained locally and checked against that copy.
            # Avoid rereading unused control files and mutable lock metadata.
            export LC_ALL=C
            exec tar -C \"\${run%/*}\" -cf - \"\${run##*/}/state\"" \
            bash "$remote_run" "$attempt_id" 2> "$error_fifo" \
        | _kubectl_write_bounded_collection_stream "$archive_path" \
            "$KUBECTL_COLLECTION_MAX_BYTES"
    # shellcheck disable=SC2034  # Returned through the caller's array nameref.
    transfer_status=("${PIPESTATUS[@]}")
    umask "$umask_previous"
    wait "$error_reader_pid" || true
    rm -f -- "$error_fifo" || return 1
    return 0
}

_kubectl_write_bounded_collection_stream() {
    local archive_path="$1" max_bytes="$2"
    [[ -n "$archive_path" && ! -e "$archive_path" && ! -L "$archive_path" \
        && "$max_bytes" =~ ^[1-9][0-9]*$ ]] || return 1
    if ! head -c "$((max_bytes + 1))" > "$archive_path"; then
        rm -f -- "$archive_path"
        echo "Error: failed to write the Kubernetes collection archive; check local free space" >&2
        return 74
    fi
    local archive_bytes
    _kubectl_local_file_bytes "$archive_path" archive_bytes || {
        rm -f -- "$archive_path"
        return 74
    }
    if [[ "$archive_bytes" -gt "$max_bytes" ]]; then
        rm -f -- "$archive_path"
        echo "Error: Kubernetes collection archive exceeded its byte limit while streaming" >&2
        return 65
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
    available_blocks=$(df -Pk "$results_dir" | awk 'END {print $4}') || return 1
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

_kubectl_validate_archive_names_stream() {
    local attempt_id="$1" max_members="${2:-$KUBECTL_COLLECTION_MAX_MEMBERS}"
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ \
        && "$max_members" =~ ^[1-9][0-9]*$ ]] || return 1
    local member member_count=0
    while IFS= read -r member; do
        _kubectl_safe_archive_member "$member" || {
            echo "Error: unsafe member in Kubernetes collection archive" >&2
            return 1
        }
        [[ "$member" == "$attempt_id" || "$member" == "$attempt_id/"* ]] || {
            echo "Error: collection archive contains another attempt" >&2
            return 1
        }
        member_count=$((member_count + 1))
        if (( member_count > max_members )); then
            echo "Error: Kubernetes collection archive exceeds its member limit" >&2
            return 1
        fi
    done
    (( member_count > 0 ))
}

_kubectl_validate_archive_types_stream() {
    local max_members="${1:-$KUBECTL_COLLECTION_MAX_MEMBERS}"
    [[ "$max_members" =~ ^[1-9][0-9]*$ ]] || return 1
    local listing type member_count=0
    while IFS= read -r listing; do
        type="${listing:0:1}"
        [[ "$type" == - || "$type" == d ]] || {
            echo "Error: collection archive contains a nonregular member" >&2
            return 1
        }
        member_count=$((member_count + 1))
        (( member_count <= max_members )) || return 1
    done
    (( member_count > 0 ))
}

_kubectl_result_destination() {
    local output_name="$1" results_dir="$2" relative="$3" create_parents="$4"
    local reason_output="${5:-}"
    [[ "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
        && "$create_parents" =~ ^[01]$ \
        && ( -z "$reason_output" \
            || "$reason_output" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ) ]] || return 1
    [[ -z "$reason_output" ]] \
        || printf -v "$reason_output" '%s' LEDGER_INCONSISTENT
    _kubectl_safe_relative_file_path "$relative" \
        && _kubectl_validate_local_directory_path "$results_dir" \
        && [[ -d "$results_dir" && ! -L "$results_dir" ]] || return 1
    local results_real current component index
    results_real=$(_kubectl_local_realpath_existing "$results_dir") || return 1
    current="$results_dir"
    local -a components=()
    IFS=/ read -ra components <<< "$relative"
    for ((index = 0; index + 1 < ${#components[@]}; index++)); do
        component="${components[$index]}"
        current="$current/$component"
        if [[ -e "$current" || -L "$current" ]]; then
            [[ -d "$current" && ! -L "$current" ]] || return 1
        elif [[ "$create_parents" -eq 1 ]]; then
            if ! mkdir -- "$current"; then
                [[ -z "$reason_output" ]] \
                    || printf -v "$reason_output" '%s' LOCAL_IO
                return 1
            fi
        else
            printf -v "$output_name" '%s' "$results_dir/$relative"
            return 0
        fi
    done
    local parent_real resolved_destination="$results_dir/$relative"
    parent_real=$(_kubectl_local_realpath_existing "$current") || return 1
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
    _kubectl_local_file_bytes "$archive_path" archive_bytes || return 1
    [[ "$archive_bytes" -le "$KUBECTL_COLLECTION_MAX_BYTES" ]] || {
        echo "Error: Kubernetes collection archive exceeds its byte limit" >&2
        return 1
    }
    # Validate metadata as streams. A hostile archive cannot consume local
    # temporary storage by forcing complete name or verbose listings to disk.
    if ! (set -o pipefail
            _kubectl_local_tar -tf "$archive_path" \
                | _kubectl_validate_archive_names_stream "$attempt_id"); then
        return 1
    fi
    # Do not extract links, devices, or other special files into the local
    # results tree. Safe member spelling alone is not sufficient for tar.
    (set -o pipefail
        LC_ALL=C _kubectl_local_tar -tvf "$archive_path" \
            | _kubectl_validate_archive_types_stream)
}

kubectl_extract_attempt_archive() {
    local archive_path="$1" attempt_id="$2" results_dir="$3" output_dir="$4"
    kubectl_validate_attempt_archive "$archive_path" "$attempt_id" || return 65
    [[ -d "$results_dir" && ! -L "$results_dir" && -n "$output_dir" && ! -e "$output_dir" \
        && "${output_dir##*/}" =~ ^\.kubernetes-collect-[0-9a-f]{8}\.[A-Za-z0-9]+$ ]] || return 1
    _kubectl_validate_local_directory_path "$results_dir" \
        && _kubectl_validate_local_directory_path "${output_dir%/*}" || return 1
    local results_real parent_real
    results_real=$(_kubectl_local_realpath_existing "$results_dir") || return 1
    parent_real=$(_kubectl_local_realpath_existing "${output_dir%/*}") || return 1
    [[ "$parent_real" == "$results_real" || "$parent_real" == "$results_real/"* ]] || {
        echo "Error: Kubernetes collection staging must reside below results" >&2
        return 1
    }
    mkdir -p "$output_dir" || return 74
    _kubectl_local_tar -C "$output_dir" --no-same-owner \
        --no-same-permissions -xf "$archive_path" || {
        rm -rf -- "$output_dir"
        return 74
    }
    local extracted_bytes
    extracted_bytes=$(_kubectl_local_tree_apparent_bytes "$output_dir") || return 74
    [[ "$extracted_bytes" =~ ^[0-9]+$ && "$extracted_bytes" -le "$KUBECTL_COLLECTION_MAX_BYTES" ]] || {
        rm -rf -- "$output_dir"
        echo "Error: extracted Kubernetes collection exceeds its byte limit" >&2
        return 65
    }
    [[ -d "$output_dir/$attempt_id" && ! -L "$output_dir/$attempt_id" ]] \
        || return 65
}

# Policies and workers belong to the active segment (see
# kubectl_use_segment); segment 0 keeps the plain names and journal keys.
kubectl_create_attempt_policies() {
    local kubernetes_dir="$1" lock_fd="$2" namespace="$3" attempt_id="$4" nonce="$5"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    local target="${KUBECTL_ACTIVE_SEGMENT:-0}" worker_key coordinator_key
    local template_dir worker_name coordinator_name manifest uid
    template_dir=$(kubectl_template_directory) || return 1
    worker_name=$(kubectl_segment_resource_name "$attempt_id" "$target" worker-net) \
        && coordinator_name=$(kubectl_segment_resource_name "$attempt_id" "$target" coord-net) \
        && worker_key=$(kubectl_segment_resource_key "$target" worker-net) \
        && coordinator_key=$(kubectl_segment_resource_key "$target" coord-net) || return 1
    manifest=$(kubectl_render_attempt_template "$template_dir/worker-network-policy.yaml.tmpl" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$worker_name" "ATTEMPT_ID=$attempt_id" \
        "OWNERSHIP_NONCE=$nonce" "SEGMENT_INDEX=$target") || return 1
    kubectl_create_owned_object uid NetworkPolicy "$worker_name" "$namespace" "$nonce" \
        "$attempt_id" "$manifest" "$kubernetes_dir" "$lock_fd" "$worker_key" || return 1
    kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        "$worker_key" NetworkPolicy "$worker_name" "$namespace" "$uid" "$nonce" || {
            kubectl_delete_owned_object NetworkPolicy "$worker_name" "$namespace" "$nonce" \
                "$attempt_id" "$uid" || true
            return 1
        }
    kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" "$worker_key" || return 1
    manifest=$(kubectl_render_attempt_template "$template_dir/coordinator-network-policy.yaml.tmpl" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$coordinator_name" "ATTEMPT_ID=$attempt_id" \
        "OWNERSHIP_NONCE=$nonce" "SEGMENT_INDEX=$target") || return 1
    # A failure leaves its creation intent or journal for the caller's
    # rollback, which still needs every transfer helper to release run trees.
    kubectl_create_owned_object uid NetworkPolicy "$coordinator_name" "$namespace" "$nonce" \
        "$attempt_id" "$manifest" "$kubernetes_dir" "$lock_fd" "$coordinator_key" \
        || return 1
    kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        "$coordinator_key" NetworkPolicy "$coordinator_name" "$namespace" "$uid" "$nonce" || {
            kubectl_delete_owned_object NetworkPolicy "$coordinator_name" "$namespace" "$nonce" \
                "$attempt_id" "$uid" || true
            return 1
        }
    kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" "$coordinator_key" || return 1
}

kubectl_create_worker_daemonset() {
    local kubernetes_dir="$1" lock_fd="$2" namespace="$3" attempt_id="$4" nonce="$5"
    local nodes_path="$6"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    [[ -f "$nodes_path" && ! -L "$nodes_path" ]] || return 1
    local -a nodes=()
    mapfile -t nodes < <(cut -f1 "$nodes_path")
    [[ ${#nodes[@]} -gt 0 ]] || return 1
    local target="${KUBECTL_ACTIVE_SEGMENT:-0}" key
    local selector_block affinity_block name template manifest uid
    local -a claim_volumes=()
    # The workers mount every claim of their segment.
    _kubectl_segment_claim_volume_arguments claim_volumes workload || return 1
    selector_block=$(kubectl_render_node_selector "$KUBECTL_NODE_SELECTOR") || return 1
    affinity_block=$(kubectl_render_node_affinity_values "${nodes[@]}") || return 1
    name=$(kubectl_segment_resource_name "$attempt_id" "$target" workers) \
        && key=$(kubectl_segment_resource_key "$target" workers) || return 1
    template="$(kubectl_template_directory)/worker-daemonset.yaml.tmpl"
    manifest=$(kubectl_render_attempt_template "$template" \
        "NAMESPACE=$namespace" "RESOURCE_NAME=$name" "ATTEMPT_ID=$attempt_id" \
        "OWNERSHIP_NONCE=$nonce" "IMAGE=$KUBECTL_ELBENCHO_IMAGE" \
        "IMAGE_PULL_POLICY=$KUBECTL_IMAGE_PULL_POLICY" \
        "RUN_AS_USER=$KUBECTL_RUN_AS_USER" "RUN_AS_GROUP=$KUBECTL_RUN_AS_GROUP" \
        "${claim_volumes[@]}" "NODE_SELECTOR_BLOCK=$selector_block" \
        "NODE_AFFINITY_VALUES=$affinity_block" "SEGMENT_INDEX=$target") || return 1
    kubectl_create_owned_object uid DaemonSet "$name" "$namespace" "$nonce" \
        "$attempt_id" "$manifest" "$kubernetes_dir" "$lock_fd" "$key" || return 1
    kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        "$key" DaemonSet "$name" "$namespace" "$uid" "$nonce" || {
            kubectl_delete_owned_object DaemonSet "$name" "$namespace" "$nonce" \
                "$attempt_id" "$uid" || true
            return 1
        }
    kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" "$key" || return 1
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
        && "$output_basename" =~ ^(elbencho|mdtest-elbencho|filesystem-batch)-[0-9]{8}Z[0-9]{6}$ \
        && -f "$coordinator_source" && ! -L "$coordinator_source" ]] || return 1
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_attempt_load_identity "$kubernetes_dir/attempts/$attempt_id" || return 1
    local destination
    destination="$kubernetes_dir/attempts/$attempt_id/control-bundle$(_kubectl_selected_segment_suffix)" \
        || return 1
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
            || ! chmod 0700 "$destination/coordinator.sh" \
            || ! printf '%s\t%s\n%s\t%s\n%s\t%s\n' attempt_id "$attempt_id" output_basename \
                "$output_basename" project_version "${STORAGE_SCALE_TEST_VERSION:-unknown}" \
                > "$destination/run-metadata.tsv" \
            || ! _kubectl_write_bundle_manifest "$destination"; then
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
        if [[ -f "$path" ]]; then
            type="regular file"
        elif [[ -d "$path" ]]; then
            type=directory
        else
            echo "Error: control bundle may contain only regular files and directories" >&2
            scan_error=1
            break
        fi
        if [[ "$type" == "regular file" ]]; then
            if ! links=$(_kubectl_local_path_link_count "$path"); then
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
    if ! _kubectl_local_tar -C "$source_dir" -cf "$archive_path" .; then
        rm -f -- "$archive_path"
        return 1
    fi
    local upload_rc=0
    _kubectl_upload_control_archive "$namespace" "$pod_name" "$attempt_id" \
        "$remote_run" "$guard_script" "$archive_path" || upload_rc=$?
    rm -f -- "$archive_path"
    return "$upload_rc"
}

_kubectl_upload_control_archive() {
    local namespace="$1" pod_name="$2" attempt_id="$3" remote_run="$4"
    local guard_script="$5" archive_path="$6" digest error_path error_fifo error_reader attempt rc output
    local upload_timeout="${KUBECTL_CONTROL_UPLOAD_TIMEOUT_SECONDS:-120}"
    [[ "$upload_timeout" =~ ^[1-9][0-9]*$ ]] || return 1
    digest=$(_kubectl_sha256_file "$archive_path") || return 1
    error_path=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-upload-error.XXXXXXXX") || return 1
    error_fifo="$error_path.fifo"
    mkfifo -m 600 -- "$error_fifo" || { rm -f -- "$error_path"; return 1; }
    for attempt in 1 2 3; do
        rc=0
        { head -c 8192 > "$error_path"; cat > /dev/null; } < "$error_fifo" &
        error_reader=$!
        # Each retry has its own private tree. Only a complete, digest-verified
        # archive can replace the reserved empty directory. Lost acknowledgement
        # is reconciled by comparing the already published files, never by
        # overwriting controls that may have been published by another process.
        # shellcheck disable=SC2016  # Variables below belong to the helper Pod.
        KUBECTL_REQUEST_TIMEOUT_SECONDS="$((upload_timeout + 5))" \
            KUBECTL_PROCESS_TIMEOUT_SECONDS="$((upload_timeout + 5))" \
            kubectl_pvc_exec_stdin "$namespace" "$pod_name" \
            timeout --kill-after=2s "${upload_timeout}s" /bin/bash -ceu "$guard_script
                expected_digest=\"\$3\"
                control=\"\$run/control\"
                [[ ! -L \"\$control\" ]] || exit 1
                temporary=\$(mktemp -d \"\$run/.control-upload.XXXXXXXX\")
                trap 'rm -rf -- \"\$temporary\"' EXIT
                cat > \"\$temporary/archive.tar\"
                actual=\$(sha256sum \"\$temporary/archive.tar\" | cut -d ' ' -f 1)
                [[ \"\$actual\" == \"\$expected_digest\" ]] || { echo 'upload checksum mismatch' >&2; exit 1; }
                # Preserve the staging root's metadata: restoring the archive's
                # '.' entry can fail GNU tar's identity check on shared filesystems.
                mkdir -m 0700 \"\$temporary/tree\"
                tar -C \"\$temporary/tree\" --no-overwrite-dir -xf \"\$temporary/archive.tar\"
                if [[ -d \"\$control\" ]]; then
                    if find -P \"\$control\" ! -type d ! -type f -print -quit | grep -q .; then exit 1; fi
                    if [[ -n \$(find \"\$control\" -type f -print -quit) ]]; then
                        [[ \$(find \"\$control\" -type f | wc -l) == \$(find \"\$temporary/tree\" -type f | wc -l) ]] || { echo 'published control file list differs' >&2; exit 1; }
                        while IFS= read -r -d '' file; do
                            relative=\"\${file#\"\$temporary/tree/\"}\"
                            cmp -s -- \"\$file\" \"\$control/\$relative\" || { echo 'published control content differs' >&2; exit 1; }
                        done < <(find \"\$temporary/tree\" -type f -print0)
                        exit 0
                    fi
                    # Preparation reserves control/executions as empty directories.
                    # Remove only empty directories; a concurrent file stops rmdir.
                    find -P \"\$control\" -depth -type d -exec rmdir -- {} +
                fi
                mv -T -- \"\$temporary/tree\" \"\$control\"" \
                bash "$remote_run" "$attempt_id" "$digest" < "$archive_path" \
                2> "$error_fifo" || rc=$?
        wait "$error_reader" || true
        ((rc != 0)) || { rm -f -- "$error_path" "$error_fifo"; return 0; }
        output=$(cat "$error_path") || output=""
        printf 'Control upload %s/3 failed (rc=%s): %s\n' "$attempt" "$rc" "$output" >&2
        if ((attempt == 3)) \
                || ! { [[ "$rc" -eq 124 && -z "$output" ]] \
                    || [[ "$output" == 'command terminated with exit code 124' ]] \
                    || _kubectl_collection_failure_is_transient "$output"; }; then
            break
        fi
        sleep "$attempt" || break
    done
    rm -f -- "$error_path" "$error_fifo"
    return 1
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
        ! kubectl_resource_key_is_lease "$key" || continue
        if ! kubectl_cleanup_creation_intent "$kubernetes_dir" "$lock_fd" \
                "$attempt_id" "$key"; then
            printf 'Error: failed to reconcile Kubernetes creation intent for %s\n' \
                "$key" >&2
            primary_rc=1
        fi
    done
    local -a keys=()
    kubectl_attempt_resource_keys keys "$kubernetes_dir" "$attempt_id" || {
        printf 'Error: cannot enumerate Kubernetes resources of attempt %s\n' \
            "$attempt_id" >&2
        return 1
    }
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
    local -a keys=()
    kubectl_attempt_workload_keys keys "$kubernetes_dir" "$attempt_id" || return 1
    # A sweep Job can start before the submitter publishes SUBMITTED.  A
    # recovered PREPARED attempt must therefore stop every workload actor
    # before removing the PVC run tree that those actors may still use.  Keep
    # the transfer helper alive; it is needed for the subsequent exact remote
    # release.
    for key in "${keys[@]}"; do
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
    _kubectl_cancel_selected_segment "$kubernetes_dir" "$lock_fd" "$namespace" \
        "$helper_pod" "$attempt_id" || return 1
    # The coordinator may have finished while it was being cancelled; report
    # the outcome its ledger actually holds.
    observed_status=$(kubectl_read_remote_status "$namespace" "$helper_pod" \
        "$attempt_id") || return 1
    if [[ ! "$observed_status" =~ ^(SUCCESS|FAILED|CANCELLED)$ ]]; then
        printf 'Error: the PVC ledger is %s after cancellation\n' \
            "${observed_status:-unreadable}" >&2
        return 1
    fi
    kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" TERMINAL || return 1
    printf '%s\n' "$observed_status"
}

# Delete the selected segment's exact coordinator Job and finalize its
# run ledger as CANCELLED through HELPER_POD in that segment's namespace. The
# Job is observed once more right before its deletion: a Job that completed
# meanwhile published SUCCESS, so it is neither deleted nor finalized. The
# coordinator's --finalize-cancelled never overwrites a terminal outcome other
# than the FAILED that its own TERM handler publishes while the Job is deleted.
_kubectl_cancel_selected_segment() {
    local kubernetes_dir="$1" lock_fd="$2" namespace="$3" helper_pod="$4"
    local attempt_id="$5" sweep_key
    sweep_key=$(_kubectl_selected_segment_key sweep) || return 1
    # The exact Job is deleted through its journal. Its absence is not inferred
    # from a broad label query, and an ownership mismatch always stops cleanup.
    if ! kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$sweep_key"; then
        KUBECTL_ATTEMPT_ID="$attempt_id" \
            KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="$kubernetes_dir/attempts/$attempt_id/state.sh" \
            KUBECTL_DIAGNOSTIC_REMOTE_STATE_PATH="$(kubectl_attempt_remote_root "$attempt_id")/state/run.status" \
            kubectl_report_lifecycle_error cancel job-journal \
                LEDGER_INCONSISTENT \
                "inspect the missing or corrupt sweep Job journal; do not cancel by label" \
                unknown || true
        return 1
    fi
    if kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "delete-$sweep_key"; then
        : # The valid journal plus step prove which exact Job was deleted.
    else
        local job_state=""
        kubectl_observe_segment_job job_state "$KUBECTL_RESOURCE_NAME" \
            "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
            "$KUBECTL_RESOURCE_UID" || return 1
        [[ "$job_state" != COMPLETE ]] || return 0
        kubectl_delete_owned_object "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" \
            "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
            "$KUBECTL_RESOURCE_UID" || return 1
        kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "delete-$sweep_key" || return 1
    fi
    kubectl_finalize_cancelled_attempt "$namespace" "$helper_pod" "$attempt_id"
}

# A segment plan (kubectl_plan_segments) is a directory describing the
# attempt being submitted, one segment per namespace:
#   segments.tsv     INDEX<TAB>NAMESPACE<TAB>CONTROL_ROOT<TAB>CLAIM[,CLAIM...]
#                    in segment order; CONTROL_ROOT is the segment's lexically
#                    first normalized root, CLAIMs are sorted
#   segment-K.roots  the Pod paths of segment K's roots (sorted, unique)
#   segment-K.paths  every Pod path segment K's executions may use
#   segment-K.ids    segment K's execution IDs, in ledger order (batches only)

# _kubectl_read_segment_plan PLAN_DIR NAMESPACES_VAR CONTROL_ROOTS_VAR
# CLAIMS_VAR: load segments.tsv into three arrays indexed by segment (CLAIMS
# holds each segment's claims separated by spaces).
_kubectl_read_segment_plan() {
    local _krsp_dir="$1" _krsp_index _krsp_namespace _krsp_root _krsp_claims
    local _krsp_extra _krsp_expected=0
    local -n _kubectl_plan_namespaces="$2" _kubectl_plan_roots="$3" _kubectl_plan_claims="$4"
    _kubectl_plan_namespaces=() _kubectl_plan_roots=() _kubectl_plan_claims=()
    [[ -f "$_krsp_dir/segments.tsv" && ! -L "$_krsp_dir/segments.tsv" ]] || return 1
    while IFS=$'\t' read -r _krsp_index _krsp_namespace _krsp_root _krsp_claims _krsp_extra; do
        [[ "$_krsp_index" == "$_krsp_expected" && -z "${_krsp_extra:-}" ]] \
            && _kubectl_validate_segment_index "$_krsp_index" \
            && kubectl_validate_namespace_name "$_krsp_namespace" \
            && [[ "$_krsp_root" == "$_krsp_namespace/"*:* && -n "$_krsp_claims" ]] \
            || return 1
        _kubectl_plan_namespaces+=("$_krsp_namespace")
        _kubectl_plan_roots+=("$_krsp_root")
        _kubectl_plan_claims+=("${_krsp_claims//,/ }")
        _krsp_expected=$((_krsp_expected + 1))
    done < "$_krsp_dir/segments.tsv"
    (( _krsp_expected > 0 ))
}

# _kubectl_prepare_segment_identity FIELDS_VAR NAMESPACE CLAIM...: validate
# one segment's cluster identity and append its claims' six-field identity
# tuples to FIELDS_VAR.
_kubectl_prepare_segment_identity() {
    local -n _kubectl_identity_fields="$1"
    local namespace="$2" cluster_identity="" row
    shift 2
    kubectl_run_prepare_phase_capture cluster_identity cluster-identity \
        kubectl_validate_cluster_identity "$namespace" "$@" || return 1
    local field_namespace namespace_uid pv pv_uid claim pvc_uid
    while IFS=$'\t' read -r field_namespace namespace_uid pv pv_uid claim pvc_uid; do
        [[ -n "$field_namespace" ]] || continue
        kubectl_pvc_lease_name "$namespace_uid" "$pv_uid" "$pvc_uid" >/dev/null \
            || return 1
        _kubectl_identity_fields+=("$field_namespace" "$namespace_uid" "$pv" "$pv_uid" \
            "$claim" "$pvc_uid")
    done <<< "$cluster_identity"
    row=$((${#_kubectl_identity_fields[@]} % 6))
    [[ "$row" -eq 0 ]]
}

# Create the selected segment's transfer helper in its namespace, mounting
# every claim of the segment, and check every workload path of the segment
# (PLAN_DIR/segment-K.paths) and its roots (segment-K.roots).
# HELPERS_VAR[segment] names the helper.
_kubectl_prepare_segment_access() {
    local -n _kubectl_access_helpers="$1"
    local kubernetes_dir="$2" lock_fd="$3" attempt_id="$4" nonce="$5" nodes_path="$6"
    local plan_dir="$7"
    local segment node token helper_name helper_uid key
    local -a pvc_paths=() roots=()
    segment=$(_kubectl_selected_segment) || return 1
    kubectl_record_prepare_failure local-coordinator LOCAL_IO 1 \
        "could not select the coordinator or generate its operation token" \
        || return 1
    mapfile -t roots < "$plan_dir/segment-$segment.roots" \
        && mapfile -t pvc_paths < "$plan_dir/segment-$segment.paths" \
        && (( ${#roots[@]} > 0 && ${#pvc_paths[@]} > 0 )) || return 1
    node=$(kubectl_choose_coordinator_node "$nodes_path") \
        && token=$(_kubectl_random_hex 4) \
        && helper_name=$(kubectl_segment_resource_name "$attempt_id" "$segment" \
            upload "$token") \
        && key=$(kubectl_segment_resource_key "$segment" transfer) || return 1
    _kubectl_access_helpers[segment]="$helper_name"
    kubectl_run_prepare_phase helper-ready kubectl_create_helper_pod \
        helper_uid transfer "$KUBECTL_NAMESPACE" "$helper_name" \
        "$nonce" "$attempt_id" "$node" "$token" \
        "$kubernetes_dir" "$lock_fd" "$key" || return 1
    kubectl_run_prepare_phase pvc-paths kubectl_validate_pvc_paths \
        "$KUBECTL_NAMESPACE" "$helper_name" "${pvc_paths[@]}" || return 1
    kubectl_run_prepare_phase test-dir-access \
        kubectl_validate_test_root_access "$KUBECTL_NAMESPACE" "$helper_name" \
            "${roots[@]}"
}

# Reserve and initialize the attempt's run tree below the selected segment's
# control root, on that root's claim.
_kubectl_prepare_segment_run_tree() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" nonce="$4" helper_name="$5"
    local segment
    segment=$(_kubectl_selected_segment) || return 1
    kubectl_record_prepare_failure local-reservation-journal LOCAL_IO 1 \
        "could not publish durable PVC run-tree intent" || return 1
    kubectl_attempt_journal_remote_reservation "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" "$nonce" || return 1
    if ! kubectl_run_prepare_phase remote-reservation \
            kubectl_reserve_remote_attempt "$KUBECTL_NAMESPACE" "$helper_name" \
            "$attempt_id" "$nonce"; then
        if [[ "$segment" != 0 \
                && "${KUBECTL_PREPARE_FAILURE_OUTPUT:-}" == *'run tree already exists'* ]]; then
            printf 'Error: control root %s of %s already holds this attempt'\''s run tree; its claim may alias the storage of an earlier segment'\''s claim.\n' \
                "$KUBECTL_CONTROL_LOGICAL_ROOT" "$(_kubectl_segment_label "$segment")" >&2
        fi
        return 1
    fi
    kubectl_record_prepare_failure local-reservation-state LOCAL_IO 1 \
        "could not publish acquired PVC reservation state" || return 1
    kubectl_attempt_mark_remote_reservation_acquired "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" \
        && kubectl_run_prepare_phase remote-control \
            kubectl_initialize_remote_control_tree "$KUBECTL_NAMESPACE" \
            "$helper_name" "$attempt_id"
}

# The selected segment's NetworkPolicies, then its worker DaemonSet.
_kubectl_prepare_segment_workers() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" nonce="$4" nodes_path="$5"
    kubectl_run_prepare_phase network-policy kubectl_create_attempt_policies \
        "$kubernetes_dir" "$lock_fd" "$KUBECTL_NAMESPACE" "$attempt_id" "$nonce" \
        || return 1
    kubectl_run_prepare_phase worker-create kubectl_create_worker_daemonset \
        "$kubernetes_dir" "$lock_fd" "$KUBECTL_NAMESPACE" "$attempt_id" "$nonce" \
        "$nodes_path"
}

# Freeze the selected segment's Ready worker endpoints.
_kubectl_prepare_segment_endpoints() {
    local kubernetes_dir="$1" attempt_id="$2" suffix
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id"
    suffix=$(_kubectl_selected_segment_suffix) || return 1
    kubectl_run_prepare_phase worker-ready kubectl_wait_worker_endpoints \
        "$KUBECTL_NAMESPACE" "$attempt_id" "$metadata_dir/nodes.tsv" \
        "$metadata_dir/worker-endpoints$suffix.tsv" \
        "${KUBECTL_WORKER_READY_TIMEOUT_SECONDS:-180}" "$metadata_dir"
}

# _kubectl_for_each_segment COUNT COMMAND [ARG...]: run COMMAND for segments
# 0..COUNT-1 in order with each segment selected; stop at the first failure.
_kubectl_for_each_segment() {
    local count="$1" index
    shift
    for ((index = 0; index < count; index++)); do
        kubectl_with_segment "$index" "$@" || return 1
    done
}

# Release every reserved run tree of an attempt being rolled back through the
# transfer helper (HELPERS_VAR[segment]) still running in that segment's
# namespace, or through a fresh inspector when that helper is gone.
_kubectl_release_prepared_run_trees() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    local -n _kubectl_rollback_helpers="$4"
    local count index suffix rc=0
    _kubectl_attempt_diagnostic_segment_count count "$kubernetes_dir" "$attempt_id"
    for ((index = 0; index < count; index++)); do
        suffix=$(kubectl_segment_suffix "$index") || return 1
        [[ -f "$kubernetes_dir/attempts/$attempt_id/remote-reservation$suffix.sh" ]] \
            || continue
        kubectl_with_segment "$index" _kubectl_release_prepared_run_tree \
            "$kubernetes_dir" "$lock_fd" "${_kubectl_rollback_helpers[index]:-}" \
            "$attempt_id" || rc=1
    done
    return "$rc"
}

# The selected segment's run tree, through its transfer helper HELPER unless
# that helper is unknown or already deleted, else through a fresh inspector.
_kubectl_release_prepared_run_tree() {
    local kubernetes_dir="$1" lock_fd="$2" helper="$3" attempt_id="$4" key
    key=$(_kubectl_selected_segment_key transfer) || return 1
    if [[ -n "$helper" ]] \
            && ! kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "delete-$key"; then
        _kubectl_release_selected_run_tree "$kubernetes_dir" "$lock_fd" "$helper" \
            "$attempt_id"
    else
        _kubectl_release_selected_run_tree_with_inspector "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" status
    fi
}

_kubectl_release_selected_run_tree() {
    local kubernetes_dir="$1" lock_fd="$2" helper="$3" attempt_id="$4"
    kubectl_release_journaled_remote_attempt "$kubernetes_dir" "$lock_fd" \
        "$KUBECTL_NAMESPACE" "$helper" "$attempt_id"
}

# Release an attempt's PVC Leases in reverse claim order: claim J's Lease is
# released only after every later claim's. Stop at the first failure.
_kubectl_release_attempt_leases() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" count index
    kubectl_attempt_claim_count count "$kubernetes_dir" "$attempt_id" 2>/dev/null \
        || count=1
    for ((index = count - 1; index >= 0; index--)); do
        kubectl_with_claim "$index" kubectl_release_pvc_lease "$kubernetes_dir" \
            "$lock_fd" "$attempt_id" || return 1
    done
}

# Check the predecessor of a new attempt: a resume names the collected (or
# cleanly failed batch) attempt it replaces as EXPECTED_CURRENT. Sets the
# caller's current_attempt.
_kubectl_prepare_check_predecessor() {
    local kubernetes_dir="$1" results_dir="$2" expected_current="$3"
    if [[ -e "$kubernetes_dir/current-attempt" ]]; then
        kubectl_record_prepare_failure local-predecessor LEDGER_INCONSISTENT 1 \
            "current attempt metadata is inconsistent" || return 1
        current_attempt=$(kubectl_attempt_current_id "$kubernetes_dir") || return 1
        [[ -n "$expected_current" && "$current_attempt" == "$expected_current" ]] \
            || return 1
        kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$current_attempt" \
            || return 1
        if [[ "$KUBECTL_LIFECYCLE_STATE" != COLLECTED ]]; then
            [[ -f "$results_dir/batch-manifest.tsv" ]] \
                && _kubectl_batch_failed_submission_retryable "$kubernetes_dir" \
                    "$current_attempt" || return 1
        fi
    elif [[ -n "$expected_current" ]]; then
        kubectl_record_prepare_failure local-predecessor LEDGER_INCONSISTENT 1 \
            "expected collected predecessor is absent" || return 1
        return 1
    fi
}

# kubectl_prepare_attempt_lifecycle OUT RESULTS_DIR REQUIRED_NODES PLAN_DIR
# MAPPED_READ_FROM [EXPECTED_CURRENT]: establish the complete pre-Job state
# machine through real, exact object operations for every segment of the plan
# (kubectl_plan_segments), in segment order: the cluster identity of every
# claim, one node discovery, a transfer helper with path and access checks per
# segment, every claim's Lease in claim order, every run tree, then every
# segment's policies and workers, and finally every segment's endpoints.
# Submission creates the coordinator Jobs (and, for several segments, the
# sequencer). Sets OUT to the new attempt ID.
kubectl_prepare_attempt_lifecycle() {
    local output_variable="$1" results_dir="$2" required_nodes="$3" plan_dir="$4"
    local mapped_read_from="${5:-}" expected_current="${6:-}"
    local kubernetes_dir lock_fd generated_attempt_id="" nonce="" current_attempt=""
    [[ "$output_variable" =~ ^[A-Za-z_][A-Za-z0-9_]*$ \
        && "$required_nodes" =~ ^[1-9][0-9]*$ && -d "$results_dir" \
        && -d "$plan_dir" ]] || return 1
    _kubectl_validate_local_directory_path "$results_dir" || return 1
    local -a plan_namespaces=() plan_control_roots=() plan_claims=()
    _kubectl_read_segment_plan "$plan_dir" plan_namespaces plan_control_roots \
        plan_claims || return 1
    # Every segment's control layout; kubectl_use_segment applies it.
    local -a KUBECTL_SEGMENT_CONTROL_ROOTS=("${plan_control_roots[@]}")
    kubernetes_dir="$results_dir/kubernetes"
    kubectl_local_lock_acquire "$kubernetes_dir" lock_fd || return 1
    local primary_rc=0 candidate_nodes index segment_count=${#plan_namespaces[@]}
    local -a identity_fields=() helper_names=() segment_claims=()
    local KUBECTL_PREPARE_FAILURE_PHASE=local-initialization
    local KUBECTL_PREPARE_FAILURE_REASON=LOCAL_IO
    local KUBECTL_PREPARE_FAILURE_RC=1 KUBECTL_PREPARE_FAILURE_OUTPUT=""
    _kubectl_prepare_check_predecessor "$kubernetes_dir" "$results_dir" \
        "$expected_current" || primary_rc=1
    if [[ "$primary_rc" -eq 0 ]]; then
        for ((index = 0; index < segment_count; index++)); do
            read -ra segment_claims <<< "${plan_claims[index]}"
            _kubectl_prepare_segment_identity identity_fields "${plan_namespaces[index]}" \
                "${segment_claims[@]}" || { primary_rc=1; break; }
        done
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        kubectl_record_prepare_failure local-attempt-id LOCAL_IO 1 \
            "could not allocate a unique local attempt ID" || return 1
        for _ in {1..16}; do
            generated_attempt_id=$(kubectl_generate_attempt_id) || break
            [[ ! -e "$kubernetes_dir/attempts/$generated_attempt_id" ]] && break
            generated_attempt_id=""
        done
        [[ "$generated_attempt_id" =~ ^[0-9a-f]{8}$ ]] || primary_rc=1
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        kubectl_record_prepare_failure local-identity LOCAL_IO 1 \
            "could not publish local attempt identity" || return 1
        nonce=$(kubectl_generate_ownership_nonce) || primary_rc=1
        kubectl_attempt_create_identity "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" \
            "$nonce" "${identity_fields[@]}" || primary_rc=1
        if [[ "$primary_rc" -eq 0 && -n "$current_attempt" ]]; then
            kubectl_record_prepare_failure local-predecessor LEDGER_INCONSISTENT 1 \
                "could not publish collected predecessor" || return 1
            kubectl_attempt_write_predecessor "$kubernetes_dir" "$lock_fd" \
                "$generated_attempt_id" "$current_attempt" || primary_rc=1
        fi
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        kubectl_record_prepare_failure local-configuration LOCAL_IO 1 \
            "could not publish local attempt state or configuration" || return 1
        kubectl_attempt_write_state "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" PREPARED \
            && kubectl_attempt_write_configuration "$kubernetes_dir" "$lock_fd" \
                "$generated_attempt_id" "$mapped_read_from" "${plan_control_roots[@]}" \
            && kubectl_attempt_load_identity "$kubernetes_dir/attempts/$generated_attempt_id" \
            || primary_rc=1
    fi
    if [[ "$primary_rc" -eq 0 ]]; then
        # Publish the PREPARED pointer before any Kubernetes or PVC mutation.
        # A killed submitter can then be found and retried by status/cancel.
        kubectl_record_prepare_failure local-current-pointer LOCAL_IO 1 \
            "could not publish the current attempt pointer" || return 1
        kubectl_attempt_write_current "$kubernetes_dir" "$lock_fd" \
            "$generated_attempt_id" || primary_rc=1
    fi
    candidate_nodes="$kubernetes_dir/attempts/${generated_attempt_id:-invalid}/nodes.tsv"
    if [[ "$primary_rc" -eq 0 ]]; then
        # Every segment's workers run on the same discovered nodes.
        kubectl_run_prepare_phase node-discovery kubectl_discover_candidate_nodes \
            "$KUBECTL_NODE_SELECTOR" "$candidate_nodes" || primary_rc=1
        if [[ "$primary_rc" -eq 0 ]]; then
            local eligible_nodes
            eligible_nodes=$(wc -l < "$candidate_nodes" 2>/dev/null) \
                || eligible_nodes=0
            eligible_nodes=$(( eligible_nodes ))
            if (( eligible_nodes < required_nodes )); then
                kubectl_record_prepare_failure node-capacity INSUFFICIENT_CAPACITY 1 \
                    "required=$required_nodes eligible=$eligible_nodes selector=$KUBECTL_NODE_SELECTOR" \
                    || return 1
                primary_rc=1
            fi
        fi
    fi
    [[ "$primary_rc" -ne 0 ]] || _kubectl_for_each_segment "$segment_count" \
        _kubectl_prepare_segment_access helper_names "$kubernetes_dir" "$lock_fd" \
        "$generated_attempt_id" "$nonce" "$candidate_nodes" "$plan_dir" || primary_rc=1
    # Acquire every claim's Lease, in claim order, before touching any claim.
    [[ "$primary_rc" -ne 0 ]] || _kubectl_for_each_claim \
        kubectl_run_prepare_phase pvc-lease kubectl_acquire_pvc_lease \
        "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" || primary_rc=1
    if [[ "$primary_rc" -eq 0 ]]; then
        for ((index = 0; index < segment_count; index++)); do
            kubectl_with_segment "$index" _kubectl_prepare_segment_run_tree \
                "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" "$nonce" \
                "${helper_names[index]}" || { primary_rc=1; break; }
        done
    fi
    [[ "$primary_rc" -ne 0 ]] || _kubectl_for_each_segment "$segment_count" \
        _kubectl_prepare_segment_workers "$kubernetes_dir" "$lock_fd" \
        "$generated_attempt_id" "$nonce" "$candidate_nodes" || primary_rc=1
    [[ "$primary_rc" -ne 0 ]] || _kubectl_for_each_segment "$segment_count" \
        _kubectl_prepare_segment_endpoints "$kubernetes_dir" \
        "$generated_attempt_id" || primary_rc=1
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
        if [[ "$rollback_rc" -eq 0 && -n "$generated_attempt_id" ]]; then
            _kubectl_release_prepared_run_trees "$kubernetes_dir" "$lock_fd" \
                "$generated_attempt_id" helper_names || rollback_rc=1
        fi
        [[ -z "$generated_attempt_id" ]] || kubectl_cleanup_journaled_resources \
            "$kubernetes_dir" "$lock_fd" "$generated_attempt_id" || rollback_rc=1
        if [[ "$rollback_rc" -eq 0 && -n "$generated_attempt_id" ]]; then
            _kubectl_release_attempt_leases "$kubernetes_dir" "$lock_fd" \
                "$generated_attempt_id" || rollback_rc=1
        fi
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
        local prepare_reason="${KUBECTL_PREPARE_FAILURE_REASON:-}"
        local prepare_action="inspect the diagnostic bundle and retry submission"
        local prepare_may_run=unknown diagnostic_root=""
        if [[ -z "$prepare_reason" ]]; then
            kubectl_classify_prepare_failure prepare_reason \
                "$KUBECTL_PREPARE_FAILURE_PHASE" "$KUBECTL_PREPARE_FAILURE_RC" \
                "$KUBECTL_PREPARE_FAILURE_OUTPUT" || prepare_reason=LOCAL_IO
        fi
        prepare_action=$(_kubectl_observation_safe_action "$prepare_reason") \
            || prepare_action="inspect the diagnostic bundle and retry submission"
        [[ "$rollback_rc" -ne 0 ]] || prepare_may_run=no
        if [[ -n "$generated_attempt_id" \
                && -d "$kubernetes_dir/attempts/$generated_attempt_id" \
                && ! -L "$kubernetes_dir/attempts/$generated_attempt_id" ]]; then
            diagnostic_root="$kubernetes_dir/attempts/$generated_attempt_id/diagnostics"
        fi
        local diagnostic_local_state=""
        if [[ -n "$generated_attempt_id" \
                && -f "$kubernetes_dir/attempts/$generated_attempt_id/state.sh" \
                && ! -L "$kubernetes_dir/attempts/$generated_attempt_id/state.sh" ]]; then
            diagnostic_local_state="$kubernetes_dir/attempts/$generated_attempt_id/state.sh"
        fi
        KUBECTL_ATTEMPT_ID="${generated_attempt_id:-unknown}" \
            KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="$diagnostic_local_state" \
            KUBECTL_DIAGNOSTIC_REMOTE_STATE_PATH="${generated_attempt_id:+$(kubectl_attempt_remote_root "$generated_attempt_id" 2>/dev/null)/state/run.status}" \
            kubectl_report_lifecycle_error prepare \
                "$KUBECTL_PREPARE_FAILURE_PHASE" "$prepare_reason" \
                "$prepare_action" "$prepare_may_run" "" "" \
                "${KUBECTL_NAMESPACE:-}" "" "" "$diagnostic_root" || true
        kubectl_local_lock_release "$lock_fd" || true
        return 1
    fi
    kubectl_local_lock_release "$lock_fd" || return 1
    printf -v "$output_variable" '%s' "$generated_attempt_id"
}

# Add the frozen sweep input to the small, immutable coordinator bundle made by
# kubectl_prepare_control_bundle. This happens before the Job exists: a Job
# never observes a partly populated control tree. The manifest writer applies
# the bundle's supported top-level and execution-file inclusion rules once all
# inputs are present.
#
# A segment of a multi-segment attempt passes SEGMENT_IDS (a file of the
# execution IDs it runs) and SEGMENT_ROW ("INDEX<TAB>NAMESPACE"): the bundle
# then holds only those executions plus segment.tsv, which the coordinator
# checks against the namespace of every definition's own roots.
kubectl_populate_sweep_control_bundle() {
    local bundle="$1" results_dir="$2" endpoints="$3" selection_file="${4:-}"
    local segment_ids="${5:-}" segment_row="${6:-}"
    local segment_pattern=$'^[0-9]{1,2}\t[a-z0-9]([a-z0-9-]*[a-z0-9])?$'
    [[ -d "$bundle" && ! -L "$bundle" && -d "$results_dir" && ! -L "$results_dir" \
        && -f "$endpoints" && ! -L "$endpoints" ]] || return 1
    [[ -z "$segment_ids$segment_row" \
        || ( -f "$segment_ids" && ! -L "$segment_ids" \
            && "$segment_row" =~ $segment_pattern ) ]] || return 1
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
    if [[ -f "$results_dir/batch-manifest.tsv" ]]; then
        elbencho_batch_verify_manifest "$results_dir" || return 1
        cp -- "$results_dir/batch-manifest.tsv" "$bundle/batch-manifest.tsv" \
            && cp -- "$results_dir/batch-sealed.sha256" "$bundle/batch-sealed.sha256" \
            && cp -- "$source_dir/lib/_batch_functions.sh" "$bundle/_batch_functions.sh" \
            || return 1
        local group_relative
        while IFS= read -r group_relative; do
            _kubectl_safe_relative_file_path "$group_relative/env_used.sh" || return 1
            mkdir -p "$bundle/$group_relative" || return 1
            cp -- "$results_dir/$group_relative/env_used.sh" \
                "$results_dir/$group_relative/env_used.yaml" "$bundle/$group_relative/" \
                || return 1
        done < <(awk -F '\t' '$1 == "group" {print $4}' "$results_dir/batch-manifest.tsv")
    fi
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
        local -A selected_ids=()
        while IFS= read -r selected_id; do
            [[ "$selected_id" =~ ^[0-9]{4}$ && ! -v selected_ids["$selected_id"] ]] || return 1
            selected_ids["$selected_id"]=1
            if [[ -f "$results_dir/batch-manifest.tsv" ]]; then
                elbencho_batch_execution_output_dir "$results_dir" "$selected_id" >/dev/null || return 1
                [[ "$(cat "$results_dir/executions/$selected_id.status")" =~ ^(PENDING|RUNNING|FAILED)$ ]] || return 1
            fi
            definitions+=("$results_dir/executions/$selected_id.sh")
        done < "$selection_file"
    else
        if [[ -f "$results_dir/batch-manifest.tsv" ]]; then
            while IFS= read -r id; do
                definitions+=("$results_dir/executions/$id.sh")
            done < <(awk -F '\t' '$1 == "execution" {print $2}' "$results_dir/batch-manifest.tsv")
        else
            definitions=("$results_dir"/executions/[0-9][0-9][0-9][0-9].sh)
        fi
    fi
    if [[ -n "$segment_ids" ]]; then
        _kubectl_filter_segment_definitions definitions "$segment_ids" || return 1
        printf '%s\n' "$segment_row" > "$bundle/segment.tsv" || return 1
    fi
    [[ ${#definitions[@]} -gt 0 ]] || return 1
    for definition in "${definitions[@]}"; do
        [[ -f "$definition" && ! -L "$definition" ]] || return 1
        cp -- "$definition" "$bundle/executions/$(basename "$definition")" || return 1
    done
    [[ ! -f "$bundle/batch-manifest.tsv" ]] \
        || elbencho_batch_verify_manifest "$bundle" subset || return 1
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
    _kubectl_write_bundle_manifest "$bundle" || return 1
}

# _kubectl_filter_segment_definitions DEFINITIONS_VAR IDS_FILE: keep only the
# definitions whose execution ID is listed in IDS_FILE.
_kubectl_filter_segment_definitions() {
    local -n _kubectl_segment_definitions="$1"
    local ids_file="$2" id definition
    local -A wanted=()
    local -a kept=()
    while IFS= read -r id; do
        [[ "$id" =~ ^[0-9]{4}$ ]] || return 1
        wanted["$id"]=1
    done < "$ids_file"
    for definition in "${_kubectl_segment_definitions[@]}"; do
        id=$(basename "$definition" .sh)
        [[ ! -v wanted["$id"] ]] || kept+=("$definition")
    done
    _kubectl_segment_definitions=("${kept[@]}")
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
    kubectl_attempt_load_metadata "$metadata_dir" || return
    [[ -f "$metadata_dir/configuration.sh" && ! -L "$metadata_dir/configuration.sh" ]] || return 1
    unset KUBECTL_SEGMENT_CONTROL_ROOTS
    # shellcheck disable=SC1090,SC1091  # Trusted, result-directory-local immutable metadata.
    source "$metadata_dir/configuration.sh" || return 1
    kubectl_validate_saved_control_layout \
        && _kubectl_validate_segment_control_roots \
        && kubectl_use_segment 0 || return 1
    printf -v "$output_name" '%s' "$loaded_attempt_id"
}

# Verify the cluster identity of the selected segment of the loaded attempt:
# its namespace, and for every claim the claim, the PV it is bound to and
# their UIDs must equal the recorded identity.
_kubectl_verify_saved_cluster_identity() {
    local kubernetes_dir="${1:-}" attempt_id="${2:-${KUBECTL_ATTEMPT_ID:-unknown}}"
    local cluster_identity="" validation_error rc=0 reason=API_UNAVAILABLE
    local segment index
    local -a claims=() claim_indexes=()
    segment=$(_kubectl_selected_segment) || return 1
    mapfile -t claim_indexes < <(kubectl_segment_claim_indexes "$segment")
    for index in "${claim_indexes[@]}"; do
        claims+=("${KUBECTL_CLAIM_PVCS[$index]}")
    done
    validation_error=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-cluster-identity.XXXXXX") \
        || return 1
    cluster_identity=$(kubectl_validate_cluster_identity "$KUBECTL_NAMESPACE" \
        "${claims[@]}" 2> "$validation_error") || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        local evidence=""
        local failure_kind="" failure_name="" failure_namespace=""
        local failure_expected_uid="" failure_action=""
        evidence=$(head -c 524288 -- "$validation_error" 2>/dev/null) || evidence=""
        cat -- "$validation_error" >&2 || true
        rm -f -- "$validation_error" || true
        _kubectl_classify_observation_failure reason "$rc" "$evidence" \
            || reason=API_UNAVAILABLE
        failure_action=$(_kubectl_observation_safe_action "$reason") \
            || failure_action="retry the same lifecycle command"
        if [[ "$reason" == IDENTITY_MISMATCH ]]; then
            failure_action="restore access to the original resource; do not adopt or clean up a replacement"
            if grep -Eqi 'persistentvolumeclaims?|(^|[^a-z])pvc([^a-z]|$)' \
                    <<< "$evidence"; then
                failure_kind=PersistentVolumeClaim
                failure_namespace="${KUBECTL_NAMESPACE:-}"
                # The claim the evidence names, or the segment's only claim.
                for index in "${claim_indexes[@]}"; do
                    if grep -Fq -- "${KUBECTL_CLAIM_PVCS[$index]}" <<< "$evidence" \
                            || (( ${#claim_indexes[@]} == 1 )); then
                        failure_name="${KUBECTL_CLAIM_PVCS[$index]}"
                        failure_expected_uid="${KUBECTL_CLAIM_PVC_UIDS[$index]}"
                        break
                    fi
                done
            elif grep -Eqi 'persistentvolumes?|(^|[^a-z])pv([^a-z]|$)' \
                    <<< "$evidence"; then
                failure_kind=PersistentVolume
                for index in "${claim_indexes[@]}"; do
                    if grep -Fq -- "${KUBECTL_CLAIM_PVS[$index]}" <<< "$evidence" \
                            || (( ${#claim_indexes[@]} == 1 )); then
                        failure_name="${KUBECTL_CLAIM_PVS[$index]}"
                        failure_expected_uid="${KUBECTL_CLAIM_PV_UIDS[$index]}"
                        break
                    fi
                done
            elif grep -Eqi 'namespaces?' <<< "$evidence"; then
                failure_kind=Namespace
                failure_name="${KUBECTL_NAMESPACE:-unknown}"
                failure_expected_uid="${KUBECTL_NAMESPACE_UID:-}"
            fi
        fi
        KUBECTL_ATTEMPT_ID="$attempt_id" \
            KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="${kubernetes_dir:+$kubernetes_dir/attempts/$attempt_id/state.sh}" \
            KUBECTL_DIAGNOSTIC_REMOTE_STATE_PATH="$(kubectl_attempt_remote_root "$attempt_id" 2>/dev/null || true)/state/run.status" \
            kubectl_report_lifecycle_error verify-cluster cluster-identity \
                "$reason" "$failure_action" unknown "$failure_kind" \
                "$failure_name" "$failure_namespace" "$failure_expected_uid" \
                "" || true
        return 1
    fi
    rm -f -- "$validation_error" || return 1
    local kind="" name="" namespace="" expected="" observed="" position=0
    local row_namespace namespace_uid pv pv_uid claim pvc_uid
    while IFS=$'\t' read -r row_namespace namespace_uid pv pv_uid claim pvc_uid; do
        [[ -n "$row_namespace" ]] || continue
        index="${claim_indexes[position]:-}"
        position=$((position + 1))
        if [[ -z "$index" || "$claim" != "${KUBECTL_CLAIM_PVCS[$index]}" ]]; then
            kind=PersistentVolumeClaim name="$claim" namespace="$row_namespace"
            expected="" observed="$pvc_uid"
        elif [[ "$namespace_uid" != "$KUBECTL_NAMESPACE_UID" ]]; then
            kind=Namespace name="$row_namespace"
            expected="$KUBECTL_NAMESPACE_UID" observed="$namespace_uid"
        elif [[ "$pv" != "${KUBECTL_CLAIM_PVS[$index]}" \
                || "$pv_uid" != "${KUBECTL_CLAIM_PV_UIDS[$index]}" ]]; then
            # The claim is now bound to another PV, or its PV was replaced.
            kind=PersistentVolume name="$pv"
            expected="${KUBECTL_CLAIM_PV_UIDS[$index]}" observed="$pv_uid"
        elif [[ "$pvc_uid" != "${KUBECTL_CLAIM_PVC_UIDS[$index]}" ]]; then
            kind=PersistentVolumeClaim name="$claim" namespace="$row_namespace"
            expected="${KUBECTL_CLAIM_PVC_UIDS[$index]}" observed="$pvc_uid"
        fi
        [[ -z "$kind" ]] || break
    done <<< "$cluster_identity"
    if [[ -z "$kind" && "$position" -ne "${#claim_indexes[@]}" ]]; then
        kind=PersistentVolumeClaim name="${claims[*]}" namespace="$KUBECTL_NAMESPACE"
    fi
    [[ -n "$kind" ]] || return 0
    KUBECTL_ATTEMPT_ID="$attempt_id" \
        KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="${kubernetes_dir:+$kubernetes_dir/attempts/$attempt_id/state.sh}" \
        KUBECTL_DIAGNOSTIC_REMOTE_STATE_PATH="$(kubectl_attempt_remote_root "$attempt_id" 2>/dev/null || true)/state/run.status" \
        kubectl_report_lifecycle_error verify-cluster cluster-identity \
            IDENTITY_MISMATCH \
            "use credentials for the original cluster; do not adopt or clean up replacement resources" \
            unknown "$kind" "$name" "$namespace" "$expected" "$observed" || true
    return 1
}

# Compare the selected segment's frozen worker endpoints with the live
# Pods: return 2 on drift (written to endpoint-drift[-s<K>].tsv).
_kubectl_verify_saved_worker_endpoints() {
    local kubernetes_dir="$1" attempt_id="$2"
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id" workers_key suffix
    workers_key=$(_kubectl_selected_segment_key workers) \
        && suffix=$(_kubectl_selected_segment_suffix) || return 1
    kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$workers_key" || return 1
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
                "$metadata_dir/nodes.tsv" "$metadata_dir/worker-endpoints$suffix.tsv" \
                "$metadata_dir/endpoint-drift$suffix.tsv" || rc=$?
        [[ "$rc" -ne 0 ]] || return 0
        [[ "$rc" -ne 2 ]] || return 2
        sleep 1
    done
    echo "Error: timed out waiting for stable Kubernetes worker endpoints" >&2
    return 1
}

# Fail the selected segment after worker drift: delete its exact Job and
# recover its ledger to FAILED. With several segments the sequencer is
# stopped first and later segments never start. A segment that succeeds while
# this happens (its Job is Complete at the re-check, or its ledger SUCCESS
# after the deletion, then journaled segment-succeeded[-s<K>]) is kept: the
# call fails, and the next --status continues from the next segment, whose
# drift fails the attempt there.
_kubectl_fail_drifted_attempt() {
    local kubernetes_dir="$1" lock_fd="$2" helper_pod="$3" attempt_id="$4" sweep_key
    local multi_segment=0 job_state=""
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    sweep_key=$(_kubectl_selected_segment_key sweep) || return 1
    [[ "${KUBECTL_SEGMENT_COUNT:-1}" -le 1 ]] || multi_segment=1
    if [[ "$multi_segment" -eq 1 ]]; then
        _kubectl_stop_sequencer "$kubernetes_dir" "$lock_fd" "$attempt_id" || return 1
    fi
    kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$sweep_key" || return 1
    if [[ "$multi_segment" -eq 1 ]] \
            && ! kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "delete-$sweep_key"; then
        kubectl_observe_segment_job job_state "$KUBECTL_RESOURCE_NAME" \
            "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
            "$KUBECTL_RESOURCE_UID" || return 1
        if [[ "$job_state" == COMPLETE ]]; then
            printf 'Error: %s finished while its worker drift was handled; retry --status\n' \
                "$(_kubectl_segment_label "$(_kubectl_selected_segment)")" >&2
            return 1
        fi
    fi
    # Worker drift, not a user, stops the attempt: its aggregate is FAILED.
    if [[ "$multi_segment" -eq 1 ]]; then
        kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            stopped-by-drift || return 1
    fi
    kubectl_delete_owned_object "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" \
        "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
        "$KUBECTL_RESOURCE_UID" || return 1
    kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        "delete-$sweep_key" || return 1
    local remote_state
    remote_state=$(kubectl_read_remote_status "$KUBECTL_NAMESPACE" "$helper_pod" \
        "$attempt_id") || return 1
    if [[ "$remote_state" =~ ^(PREPARED|RUNNING)$ ]]; then
        kubectl_recover_lost_coordinator "$KUBECTL_NAMESPACE" "$helper_pod" \
            "$attempt_id" || return 1
        remote_state=$(kubectl_read_remote_status "$KUBECTL_NAMESPACE" "$helper_pod" \
            "$attempt_id") || return 1
    fi
    if [[ "$multi_segment" -eq 1 && "$remote_state" == SUCCESS ]]; then
        _kubectl_record_deleted_segment_success "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" || return 1
        printf 'Error: %s finished while its worker drift was handled; retry --status\n' \
            "$(_kubectl_segment_label "$(_kubectl_selected_segment)")" >&2
        return 1
    fi
    [[ "$remote_state" == FAILED ]] || return 1
    if [[ "${KUBECTL_SEGMENT_COUNT:-1}" -gt 1 ]]; then
        _kubectl_abandon_later_segments "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "$(( $(_kubectl_selected_segment) + 1 ))" || return 1
    fi
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
    name=$(kubectl_segment_resource_name "$attempt_id" "${KUBECTL_ACTIVE_SEGMENT:-0}" \
        "$template_name" "$token") || return 1
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
    # The journal, not the active segment, records where this inspector runs.
    kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$resource_key" \
        && [[ "$KUBECTL_RESOURCE_KIND" == Pod && "$KUBECTL_RESOURCE_NAME" == "$name" \
            && "$KUBECTL_RESOURCE_UID" == "$uid" ]] || return 1
    kubectl_delete_owned_object Pod "$name" "$KUBECTL_RESOURCE_NAMESPACE" \
        "$KUBECTL_RESOURCE_NONCE" "$attempt_id" "$uid" \
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

# kubectl_plan_segments RESULTS_DIR PLAN_DIR: write the segment plan of a new
# attempt (see _kubectl_read_segment_plan) to the empty directory PLAN_DIR.
# A results directory that is not a batch is one segment: the namespace and
# claims of its TEST_DIRS. A batch has one segment per namespace that runs at
# least one of the attempt's executions (all of them, or those listed in
# KUBECTL_SUBMIT_EXECUTIONS_FILE), in order of each namespace's first
# execution in the ledger; each execution belongs to the namespace of its
# group's roots. A segment's claims, roots, control root and checked paths
# cover every group of the batch in its namespace, so they stay the same for
# every attempt of the batch.
kubectl_plan_segments() {
    local results_dir="$1" plan_dir="$2"
    [[ -d "$plan_dir" && ! -L "$plan_dir" ]] || return 1
    if [[ -f "$results_dir/batch-manifest.tsv" ]]; then
        _kubectl_plan_batch_segments "$results_dir" "$plan_dir"
    else
        _kubectl_plan_single_segment "$plan_dir"
    fi
}

_kubectl_plan_single_segment() {
    local plan_dir="$1" namespace control_logical control_test claims_csv
    local -a claims=()
    local -A mapped=()
    kubectl_test_dirs_segment namespace claims \
        && kubectl_select_control_root control_logical control_test \
        && kubectl_set_control_layout "$control_logical" "$control_test" \
        && kubectl_map_test_dirs mapped || return 1
    printf '%s\n' "${!mapped[@]}" | LC_ALL=C sort -u > "$plan_dir/segment-0.roots" \
        || return 1
    {
        cat -- "$plan_dir/segment-0.roots"
        [[ -z "${ELBENCHO_SWEEP_READ_FROM:-}" ]] \
            || kubectl_map_read_from_path "$ELBENCHO_SWEEP_READ_FROM"
    } > "$plan_dir/segment-0.paths" || return 1
    claims_csv=$(IFS=,; printf '%s' "${claims[*]}")
    printf '0\t%s\t%s\t%s\n' "$namespace" "$control_logical" "$claims_csv" \
        > "$plan_dir/segments.tsv"
}

# Print one batch group's namespace, claims and roots, as
#   namespace<TAB>NAMESPACE / claim<TAB>CLAIM / root<TAB>LOGICAL<TAB>MAPPED
# reading only its own snapshot.
_kubectl_plan_group_roots() (
    local results_dir="$1" gid="$2" relative="$3" namespace root normalized mapped
    local -a claims=()
    unset TEST_DIRS KUBECTL_NAMESPACE
    # shellcheck disable=SC1090  # Digest-verified, result-local group snapshot.
    source "$results_dir/$relative/env_used.sh" >/dev/null || exit 1
    kubectl_test_dirs_segment namespace claims || {
        printf 'Error: batch group %s has no valid claim-qualified TEST_DIRS in one namespace\n' \
            "$gid" >&2
        exit 1
    }
    printf 'namespace\t%s\n' "$namespace"
    printf 'claim\t%s\n' "${claims[@]}"
    for root in "${!TEST_DIRS[@]}"; do
        kubectl_normalize_logical_path "$root" normalized \
            && mapped=$(kubectl_map_logical_path "$normalized") || exit 1
        printf 'root\t%s\t%s\n' "$normalized" "$mapped"
    done
)

# Print the Pod paths that execution ID of group RELATIVE may use: its
# generated directories and its read-from path. CONTROL_ROOT is the logical
# control root of the execution's namespace.
# shellcheck disable=SC2030  # The control root is set only in this subshell.
_kubectl_plan_execution_paths() (
    local results_dir="$1" relative="$2" id="$3" control_root="$4" target
    local -a targets=()
    unset TEST_DIRS KUBECTL_NAMESPACE ELBENCHO_SWEEP_READ_FROM
    # shellcheck disable=SC1090  # Digest-verified, result-local snapshots.
    source "$results_dir/$relative/env_used.sh" >/dev/null \
        && source "$results_dir/executions/$id.sh" >/dev/null || exit 1
    KUBECTL_CONTROL_LOGICAL_ROOT="$control_root"
    IFS=, read -ra targets <<< "${ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV:-}"
    [[ ${#targets[@]} -gt 0 ]] || exit 1
    for target in "${targets[@]}"; do
        kubectl_map_logical_path "$target" || exit 1
    done
    [[ -z "${ELBENCHO_SWEEP_READ_FROM:-}" ]] \
        || kubectl_map_read_from_path "$ELBENCHO_SWEEP_READ_FROM" || exit 1
)

_kubectl_plan_batch_segments() {
    local results_dir="$1" plan_dir="$2" manifest="$1/batch-manifest.tsv"
    local kind first second third extra gid id namespace field value mapped
    local -A group_relatives=() execution_groups=() group_namespaces=()
    local -A namespace_slots=() selected=()
    local -a group_ids=() execution_ids=() segment_namespaces=()
    elbencho_batch_verify_manifest "$results_dir" || return 1
    while IFS=$'\t' read -r kind first second third extra; do
        case "$kind" in
            group) group_relatives["$first"]="$third"; group_ids+=("$first") ;;
            execution) execution_groups["$first"]="$second"; execution_ids+=("$first") ;;
        esac
    done < "$manifest"
    # Every group's namespace, claims and roots, pooled per namespace.
    for gid in "${group_ids[@]}"; do
        namespace=""
        while IFS=$'\t' read -r field value mapped; do
            case "$field" in
                namespace)
                    namespace="$value"
                    if [[ ! -v namespace_slots["$namespace"] ]]; then
                        namespace_slots["$namespace"]=${#namespace_slots[@]}
                    fi
                    ;;
                claim) printf '%s\n' "$value" >> "$plan_dir/ns-${namespace_slots[$namespace]}.claims" ;;
                root)
                    printf '%s\n' "$value" >> "$plan_dir/ns-${namespace_slots[$namespace]}.logical"
                    printf '%s\n' "$mapped" >> "$plan_dir/ns-${namespace_slots[$namespace]}.roots"
                    ;;
                *) return 1 ;;
            esac || return 1
        done < <(_kubectl_plan_group_roots "$results_dir" "$gid" "${group_relatives[$gid]}" \
            || printf 'error\n')
        [[ -n "$namespace" ]] || return 1
        group_namespaces["$gid"]="$namespace"
    done
    # Every execution's paths, checked on its namespace's claims.
    local slot control_root
    for id in "${execution_ids[@]}"; do
        gid="${execution_groups[$id]}"
        slot="${namespace_slots[${group_namespaces[$gid]}]}"
        control_root=$(LC_ALL=C sort -u "$plan_dir/ns-$slot.logical" | head -n 1) || return 1
        _kubectl_plan_execution_paths "$results_dir" "${group_relatives[$gid]}" "$id" \
            "$control_root" >> "$plan_dir/ns-$slot.paths" || return 1
    done
    # The attempt's executions, in ledger order.
    if [[ -n "${KUBECTL_SUBMIT_EXECUTIONS_FILE:-}" ]]; then
        while IFS= read -r id; do
            [[ "$id" =~ ^[0-9]{4}$ && -v execution_groups["$id"] ]] || return 1
            selected["$id"]=1
        done < "$KUBECTL_SUBMIT_EXECUTIONS_FILE"
    else
        for id in "${execution_ids[@]}"; do selected["$id"]=1; done
    fi
    local -A segment_of=()
    local segment claims_csv
    : > "$plan_dir/segments.tsv" || return 1
    for id in "${execution_ids[@]}"; do
        [[ -v selected["$id"] ]] || continue
        namespace="${group_namespaces[${execution_groups[$id]}]}"
        if [[ ! -v segment_of["$namespace"] ]]; then
            segment=${#segment_namespaces[@]}
            _kubectl_validate_segment_index "$segment" || {
                printf 'Error: this attempt spans more than %s Kubernetes namespaces\n' \
                    "$KUBECTL_MAX_SEGMENTS" >&2
                return 1
            }
            segment_of["$namespace"]="$segment"
            segment_namespaces+=("$namespace")
            slot="${namespace_slots[$namespace]}"
            LC_ALL=C sort -u "$plan_dir/ns-$slot.roots" > "$plan_dir/segment-$segment.roots" \
                && cat -- "$plan_dir/segment-$segment.roots" "$plan_dir/ns-$slot.paths" \
                    | LC_ALL=C sort -u > "$plan_dir/segment-$segment.paths" || return 1
            control_root=$(LC_ALL=C sort -u "$plan_dir/ns-$slot.logical" | head -n 1) \
                && claims_csv=$(LC_ALL=C sort -u "$plan_dir/ns-$slot.claims" | paste -sd, -) \
                || return 1
            printf '%s\t%s\t%s\t%s\n' "$segment" "$namespace" "$control_root" "$claims_csv" \
                >> "$plan_dir/segments.tsv" || return 1
        fi
        printf '%s\n' "$id" >> "$plan_dir/segment-${segment_of[$namespace]}.ids" || return 1
    done
    (( ${#segment_namespaces[@]} > 0 ))
}

# Submit a new attempt from its segment plan (kubectl_plan_segments). Each
# segment has its namespace's claims (one Lease each), its workers, its run
# tree and its bundle. One segment is a plain attempt: its Job starts
# immediately. Several segments (a batch whose targets span namespaces) create
# every segment's Job suspended plus the in-cluster sequencer, which starts
# them one at a time in segment order.
kubectl_submit_sweep() {
    local results_dir="$1" required_nodes="$2" expected_current="${3:-}"
    [[ -d "$results_dir" && "$required_nodes" =~ ^[1-9][0-9]*$ ]] || return 1
    local plan_dir rc=0
    plan_dir=$(mktemp -d "${TMPDIR:-/tmp}/storage-scale-test-segments.XXXXXX") \
        || return 1
    kubectl_plan_segments "$results_dir" "$plan_dir" || rc=1
    [[ "$rc" -ne 0 ]] || _kubectl_submit_segments "$results_dir" "$required_nodes" \
        "$expected_current" "$plan_dir" || rc=$?
    rm -rf -- "$plan_dir" || [[ "$rc" -ne 0 ]] || rc=1
    return "$rc"
}

# A batch saves its canonical control root (the lexically first root of every
# group). The segment of that root's namespace must still choose it.
# shellcheck disable=SC2031  # The caller's own control root.
_kubectl_check_saved_control_root() {
    local index
    local -n _kubectl_saved_namespaces="$1" _kubectl_saved_roots="$2"
    [[ -n "${KUBECTL_CONTROL_LOGICAL_ROOT:-}" ]] || return 0
    kubectl_validate_saved_control_layout || return 1
    for index in "${!_kubectl_saved_namespaces[@]}"; do
        [[ "$KUBECTL_CONTROL_LOGICAL_ROOT" == "${_kubectl_saved_namespaces[index]}/"* ]] \
            || continue
        [[ "$KUBECTL_CONTROL_LOGICAL_ROOT" == "${_kubectl_saved_roots[index]}" ]] || {
            echo "Error: batch canonical control root differs from its saved union" >&2
            return 1
        }
    done
}

_kubectl_submit_segments() {
    local results_dir="$1" required_nodes="$2" expected_current="$3" plan_dir="$4"
    local segment_count index
    local -a plan_namespaces=() plan_control_roots=() plan_claims=()
    _kubectl_read_segment_plan "$plan_dir" plan_namespaces plan_control_roots \
        plan_claims || return 1
    segment_count=${#plan_namespaces[@]}
    kubectl_validate_runtime_configuration || return 1
    # Only several segments run the sequencer, so only they need its image.
    (( segment_count == 1 )) \
        || kubectl_validate_sequencer_configuration "$segment_count" || return 1
    _kubectl_check_saved_control_root plan_namespaces plan_control_roots || return 1
    local mapped_read_from="" mapped_control
    # A batch's read-from paths are its groups' own, checked per execution by
    # the plan against each group's roots; a value in the submitting shell is
    # not the batch's.
    if [[ -n "${ELBENCHO_SWEEP_READ_FROM:-}" \
            && ! -f "$results_dir/batch-manifest.tsv" ]]; then
        mapped_control=$(kubectl_map_logical_path "${plan_control_roots[0]}") \
            && kubectl_set_control_layout "${plan_control_roots[0]}" "$mapped_control" \
            && mapped_read_from=$(kubectl_map_read_from_path "$ELBENCHO_SWEEP_READ_FROM") \
            || return 1
    fi
    # Every segment's control layout; kubectl_use_segment applies it.
    local -a KUBECTL_SEGMENT_CONTROL_ROOTS=("${plan_control_roots[@]}")
    local attempt_id
    kubectl_prepare_attempt_lifecycle attempt_id "$results_dir" "$required_nodes" \
        "$plan_dir" "$mapped_read_from" "$expected_current" || return 1
    local kubernetes_dir="$results_dir/kubernetes" lock_fd
    kubectl_local_lock_acquire "$kubernetes_dir" lock_fd || return 1
    local rc=0
    kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$attempt_id" || rc=1
    if [[ "$rc" -eq 0 ]]; then
        for ((index = 0; index < segment_count; index++)); do
            kubectl_with_segment "$index" _kubectl_submit_segment_bundle "$results_dir" \
                "$kubernetes_dir" "$lock_fd" "$attempt_id" "$plan_dir" \
                || { rc=1; break; }
        done
    fi
    [[ "$rc" -ne 0 ]] || _kubectl_for_each_segment "$segment_count" \
        _kubectl_create_segment_job "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        "$segment_count" || rc=1
    if [[ "$rc" -eq 0 ]] && (( segment_count > 1 )); then
        _kubectl_create_sequencer "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "$segment_count" || rc=1
    fi
    if [[ "$rc" -eq 0 ]] && ! kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" SUBMITTED; then
        kubectl_cleanup_journaled_resources "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" || true
        rc=1
    fi
    if [[ "$rc" -ne 0 ]]; then
        # Nothing asynchronous is allowed to survive a failed handoff.  The
        # pre-Job lifecycle journal gives this rollback exact identities.
        local rollback_rc=0
        kubectl_quiesce_prepared_workloads "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" || rollback_rc=1
        kubectl_preserve_attempt_diagnostics "$kubernetes_dir" "$attempt_id" \
            submit-failed
        [[ "$rollback_rc" -ne 0 ]] || _kubectl_release_attempt_run_trees \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" || rollback_rc=1
        kubectl_cleanup_journaled_resources "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            || rollback_rc=1
        [[ "$rollback_rc" -ne 0 ]] || _kubectl_release_attempt_leases \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" || rollback_rc=1
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

# Build, upload and seal the selected segment's control bundle through its
# transfer helper, then delete that helper. A batch bundle holds only the
# segment's executions (PLAN_DIR/segment-K.ids) and names its namespace in
# segment.tsv.
_kubectl_submit_segment_bundle() {
    local results_dir="$1" kubernetes_dir="$2" lock_fd="$3" attempt_id="$4"
    local plan_dir="$5"
    local segment suffix transfer_key bundle
    local -a segment_arguments=()
    segment=$(_kubectl_selected_segment) && suffix=$(_kubectl_selected_segment_suffix) \
        && transfer_key=$(_kubectl_selected_segment_key transfer) || return 1
    # A batch bundle always names its segment's namespace, so the coordinator
    # finds its control root among the batch's roots of every namespace.
    if [[ -f "$plan_dir/segment-$segment.ids" ]]; then
        segment_arguments=("$plan_dir/segment-$segment.ids"
            "$segment"$'\t'"$KUBECTL_NAMESPACE")
    fi
    kubectl_prepare_control_bundle bundle "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        "${results_dir##*/}" "$(dirname "${BASH_SOURCE[0]}")/_nv-elbencho-kubectl-coordinator.sh" \
        && kubectl_populate_sweep_control_bundle "$bundle" "$results_dir" \
            "$kubernetes_dir/attempts/$attempt_id/worker-endpoints$suffix.tsv" \
            "${KUBECTL_SUBMIT_EXECUTIONS_FILE:-}" "${segment_arguments[@]}" || return 1
    kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$transfer_key" || return 1
    kubectl_upload_control_bundle "$KUBECTL_NAMESPACE" "$KUBECTL_RESOURCE_NAME" \
        "$attempt_id" "$bundle" || return 1
    # Upload has completed; the long-lived attempt owns no transfer Pod.
    kubectl_delete_owned_object "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" \
        "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
        "$KUBECTL_RESOURCE_UID" \
        && kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "delete-$transfer_key"
}

# Create the selected segment's coordinator Job, mounting every claim of the
# segment, suspended when the attempt has several segments (the sequencer
# starts it), and journal its exact UID.
_kubectl_create_segment_job() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" segment_count="$4"
    local segment key job_name coordinator_node manifest job_uid suspend=false
    local -a claim_volumes=()
    segment=$(_kubectl_selected_segment) && key=$(_kubectl_selected_segment_key sweep) \
        && job_name=$(kubectl_segment_resource_name "$attempt_id" "$segment" sweep) \
        && _kubectl_segment_claim_volume_arguments claim_volumes workload \
        || return 1
    (( segment_count == 1 )) || suspend=true
    coordinator_node=$(_kubectl_attempt_helper_node "$kubernetes_dir/attempts/$attempt_id") \
        || return 1
    manifest=$(kubectl_render_attempt_template "$(kubectl_template_directory)/sweep-job.yaml.tmpl" \
        "NAMESPACE=$KUBECTL_NAMESPACE" "RESOURCE_NAME=$job_name" "ATTEMPT_ID=$attempt_id" \
        "OWNERSHIP_NONCE=$KUBECTL_OWNERSHIP_NONCE" "IMAGE=$KUBECTL_ELBENCHO_IMAGE" \
        "IMAGE_PULL_POLICY=$KUBECTL_IMAGE_PULL_POLICY" "RUN_AS_USER=$KUBECTL_RUN_AS_USER" \
        "RUN_AS_GROUP=$KUBECTL_RUN_AS_GROUP" "${claim_volumes[@]}" \
        "COORDINATOR_NODE=$coordinator_node" \
        "REMOTE_RUN_DIRECTORY=$(kubectl_attempt_remote_root "$attempt_id")" \
        "SEGMENT_INDEX=$segment" "JOB_SUSPEND=$suspend") || return 1
    kubectl_create_owned_object job_uid Job "$job_name" "$KUBECTL_NAMESPACE" \
        "$KUBECTL_OWNERSHIP_NONCE" "$attempt_id" "$manifest" \
        "$kubernetes_dir" "$lock_fd" "$key" || return 1
    if ! kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "$key" Job "$job_name" "$KUBECTL_NAMESPACE" "$job_uid" \
            "$KUBECTL_OWNERSHIP_NONCE"; then
        # The Job has an exact UID but no durable journal yet. Delete it
        # immediately; later cleanup must never guess by labels.
        kubectl_delete_owned_object Job "$job_name" "$KUBECTL_NAMESPACE" \
            "$KUBECTL_OWNERSHIP_NONCE" "$attempt_id" "$job_uid" || true
        return 1
    fi
    kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" "$key"
}

# Create one owned object under its journal KEY: durable intent, create, exact
# identity, UID journal, intent removal.
_kubectl_create_journaled_object() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" key="$4" kind="$5"
    local name="$6" namespace="$7" manifest="$8" nonce="$KUBECTL_OWNERSHIP_NONCE"
    local _kubectl_journaled_uid=""
    kubectl_create_owned_object _kubectl_journaled_uid "$kind" "$name" "$namespace" \
        "$nonce" "$attempt_id" "$manifest" "$kubernetes_dir" "$lock_fd" "$key" \
        || return 1
    if ! kubectl_attempt_journal_resource "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "$key" "$kind" "$name" "$namespace" "$_kubectl_journaled_uid" "$nonce"; then
        kubectl_delete_owned_object "$kind" "$name" "$namespace" "$nonce" \
            "$attempt_id" "$_kubectl_journaled_uid" || true
        return 1
    fi
    kubectl_attempt_clear_creation_intent "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" "$key"
}

# Create the sequencer of a multi-segment attempt: its ServiceAccount in segment
# 0's namespace; in every segment's namespace a Role granting get and patch on
# exactly that segment's coordinator Job, bound to the ServiceAccount; then the
# sequencer Job with the ordered NAMESPACE JOB UID triples of those Jobs.
_kubectl_create_sequencer() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" count="$4"
    local nonce name subject_namespace index role manifest job_namespace job_name
    local -a triples=()
    kubectl_attempt_load_identity "$kubernetes_dir/attempts/$attempt_id" || return 1
    nonce="$KUBECTL_OWNERSHIP_NONCE"
    subject_namespace="${KUBECTL_SEGMENT_NAMESPACES[0]}"
    name=$(kubectl_sequencer_resource_name "$attempt_id") || return 1
    manifest=$(kubectl_render_sequencer_service_account "$attempt_id" "$nonce" \
        "$subject_namespace") || return 1
    _kubectl_create_journaled_object "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        sequencer-sa ServiceAccount "$name" "$subject_namespace" "$manifest" || return 1
    for ((index = 0; index < count; index++)); do
        kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" \
            "$(kubectl_segment_resource_key "$index" sweep)" \
            && [[ "$KUBECTL_RESOURCE_KIND" == Job ]] || return 1
        job_namespace="$KUBECTL_RESOURCE_NAMESPACE"
        job_name="$KUBECTL_RESOURCE_NAME"
        triples+=("$job_namespace" "$job_name" "$KUBECTL_RESOURCE_UID")
        role=$(kubectl_segment_resource_name "$attempt_id" "$index" sequencer) || return 1
        manifest=$(kubectl_render_sequencer_role "$attempt_id" "$nonce" "$index" \
            "$job_namespace" "$job_name") || return 1
        _kubectl_create_journaled_object "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "sequencer-role-s$index" Role "$role" "$job_namespace" "$manifest" || return 1
        manifest=$(kubectl_render_sequencer_role_binding "$attempt_id" "$nonce" "$index" \
            "$job_namespace" "$subject_namespace") || return 1
        _kubectl_create_journaled_object "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "sequencer-binding-s$index" RoleBinding "$role" "$job_namespace" "$manifest" \
            || return 1
    done
    manifest=$(kubectl_render_sequencer_job "$attempt_id" "$nonce" "$subject_namespace" \
        "${triples[@]}") || return 1
    _kubectl_create_journaled_object "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        sequencer Job "$name" "$subject_namespace" "$manifest"
}

# Release every reserved run tree of an attempt through a fresh inspector Pod
# in each segment's namespace (TEMPLATE: status, or collector).
_kubectl_release_attempt_run_trees() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" template="${4:-status}"
    local count index suffix rc=0
    _kubectl_attempt_diagnostic_segment_count count "$kubernetes_dir" "$attempt_id"
    for ((index = 0; index < count; index++)); do
        suffix=$(kubectl_segment_suffix "$index") || return 1
        [[ -f "$kubernetes_dir/attempts/$attempt_id/remote-reservation$suffix.sh" ]] \
            || continue
        kubectl_with_segment "$index" _kubectl_release_selected_run_tree_with_inspector \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" "$template" || rc=1
    done
    return "$rc"
}

_kubectl_release_selected_run_tree_with_inspector() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" template="$4"
    local helper="" helper_uid="" rc=0
    _kubectl_create_inspector helper helper_uid "$template" "$kubernetes_dir" \
        "$lock_fd" "$attempt_id" || return 1
    kubectl_release_journaled_remote_attempt "$kubernetes_dir" "$lock_fd" \
        "$KUBECTL_NAMESPACE" "$helper" "$attempt_id" || rc=1
    _kubectl_remove_inspector "$kubernetes_dir" "$lock_fd" "$helper" "$helper_uid" \
        "$attempt_id" || rc=1
    return "$rc"
}

kubectl_recover_prepared_attempt() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    _kubectl_require_local_lock "$kubernetes_dir" "$lock_fd" || return 1
    kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$attempt_id" || return 1
    [[ "$KUBECTL_LIFECYCLE_STATE" == PREPARED ]] || return 1
    local rc=0
    kubectl_quiesce_prepared_workloads "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" || rc=1
    kubectl_preserve_attempt_diagnostics "$kubernetes_dir" "$attempt_id" \
        recover-prepared
    [[ "$rc" -ne 0 ]] || _kubectl_release_attempt_run_trees "$kubernetes_dir" \
        "$lock_fd" "$attempt_id" || rc=1
    kubectl_cleanup_journaled_resources "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" || rc=1
    [[ "$rc" -ne 0 ]] || _kubectl_release_attempt_leases "$kubernetes_dir" \
        "$lock_fd" "$attempt_id" || rc=1
    [[ "$rc" -ne 0 ]] || kubectl_attempt_transition "$kubernetes_dir" \
        "$lock_fd" "$attempt_id" SUBMISSION_FAILED || rc=1
    [[ "$rc" -ne 0 ]] || kubectl_attempt_restore_predecessor "$kubernetes_dir" \
        "$lock_fd" "$attempt_id" || rc=1
    return "$rc"
}

# Observe the selected segment for --status through a fresh
# inspector in its namespace: recover a lost coordinator, fail the attempt on
# worker drift (on this segment or a later one, up to COUNT-1, that has not
# run yet), and set VIEW_VAR to the refreshed "STATE PENDING RUNNING
# SUCCEEDED FAILED" observation.
_kubectl_status_selected_segment() {
    local view_name="$1" kubernetes_dir="$2" lock_fd="$3" attempt_id="$4" count="${5:-1}"
    local inspector="" inspector_uid="" remote_state="" rc=0 sweep_key
    sweep_key=$(_kubectl_selected_segment_key sweep) || return 1
    [[ "$rc" -ne 0 ]] || _kubectl_create_inspector inspector inspector_uid status \
        "$kubernetes_dir" "$lock_fd" "$attempt_id" || rc=1
    [[ "$rc" -ne 0 ]] || remote_state=$(kubectl_read_remote_status "$KUBECTL_NAMESPACE" \
        "$inspector" "$attempt_id") || rc=1
    local job_state="" job_rc=0 observed_job_uid="" job_missing=0
    local job_kind="" job_name="" job_namespace="" job_uid=""
    if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED ]]; then
        kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$sweep_key" || rc=1
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
    # A terminal Job observation is newer than the initial PVC read.
    # Refresh before diagnosing a completed Job with a nonterminal ledger.
    if [[ "$rc" -eq 0 && "$job_rc" -eq 0 \
            && "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED ]]; then
        remote_state=$(kubectl_read_remote_status "$KUBECTL_NAMESPACE" \
            "$inspector" "$attempt_id") || rc=1
    fi
    if [[ "$rc" -eq 0 && "$remote_state" =~ ^(PREPARED|RUNNING)$ \
            && "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED ]]; then
        # Endpoint identity is the more specific failure evidence.  Check
        # it before treating a failed Job as an unexplained coordinator
        # loss: a worker replacement can make the coordinator fail first.
        local endpoint_rc=0
        _kubectl_verify_unfinished_endpoints "$kubernetes_dir" "$attempt_id" "$count" \
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
                    "$job_uid" "$job_uid" "" "$job_state" || true
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
    local progress="" progress_rc=0 progress_reason="" progress_action=""
    if [[ "$rc" -eq 0 ]]; then
        # Reuse the phase capture adapter to keep benign kubectl warnings
        # on stderr, not inside the structured progress response. Its
        # bounded failure evidence remains available for classification.
        KUBECTL_PREPARE_FAILURE_OUTPUT=""
        kubectl_run_prepare_phase_capture progress execution-progress \
            kubectl_read_refreshed_execution_progress \
            "$KUBECTL_NAMESPACE" "$inspector" "$attempt_id" "$remote_state" \
            || progress_rc=$?
        if [[ "$progress_rc" -ne 0 ]]; then
            local progress_error="$KUBECTL_PREPARE_FAILURE_OUTPUT"
            local progress_evidence
            progress_evidence=$(printf '%s\n' "$progress_error" \
                | sed '/^Warning: version difference between client /d')
            kubectl_classify_prepare_failure progress_reason remote-control \
                "$progress_rc" "$progress_error" || progress_reason=API_UNAVAILABLE
            progress_action=$(_kubectl_observation_safe_action "$progress_reason")
            if [[ "$progress_rc" -eq 1 && ( -z "$progress_evidence" \
                    || "$progress_error" == *'invalid Kubernetes execution status'* ) ]]; then
                progress_reason=LEDGER_INCONSISTENT
                progress_action="inspect the current attempt execution ledger and retry --status"
            fi
            KUBECTL_ATTEMPT_ID="$attempt_id" \
                KUBECTL_REMOTE_STATE="$remote_state" \
                kubectl_report_lifecycle_error status execution-progress \
                    "$progress_reason" "$progress_action" unknown || true
            rc=1
        fi
    fi
    [[ -z "${inspector:-}" ]] || _kubectl_remove_inspector "$kubernetes_dir" \
        "$lock_fd" "$inspector" "$inspector_uid" "$attempt_id" || rc=1
    [[ "$rc" -ne 0 ]] || printf -v "$view_name" '%s' "$progress"
    return "$rc"
}

# Worker drift on the selected segment or on any later segment up to COUNT-1
# (none of them has finished). Returns 2 on drift.
_kubectl_verify_unfinished_endpoints() {
    local kubernetes_dir="$1" attempt_id="$2" count="$3" selected index
    _kubectl_verify_saved_worker_endpoints "$kubernetes_dir" "$attempt_id" || return $?
    selected=$(_kubectl_selected_segment) || return 1
    for ((index = selected + 1; index < count; index++)); do
        kubectl_with_segment "$index" _kubectl_verify_saved_worker_endpoints \
            "$kubernetes_dir" "$attempt_id" || return $?
    done
}

# Multi-segment attempts. Each segment K of an attempt (one namespace) has
# its own coordinator Job (journal key sweep[-s<K>]). Segments run one at a
# time in segment order: the sequencer, or --status when the sequencer is
# lost, unsuspends Job K once Job K-1 is Complete, and nothing starts after a
# segment that did not succeed. Cancellation and drift record the later
# segments they abandon with a segment-not-started-s<K> step before deleting
# those still-suspended Jobs.

# kubectl_observe_segment_job OUT NAME NAMESPACE NONCE RUN_ID UID: classify
# the exact coordinator Job as SUSPENDED (never started), ACTIVE, COMPLETE,
# FAILED, or MISSING (absent).
kubectl_observe_segment_job() {
    local output_name="$1" name="$2" namespace="$3" nonce="$4" run_id="$5" expected_uid="$6"
    local evidence observed_uid suspend active conditions _kubectl_job_segment_state
    [[ "$output_name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    evidence=$(kubectl_run_observational -n "$namespace" get Job "$name" --ignore-not-found \
        -o 'jsonpath={.metadata.uid}{"|"}{.spec.suspend}{"|"}{.status.active}{"|"}{range .status.conditions[?(@.status=="True")]}{.type}{","}{end}') \
        || return 1
    if [[ -z "$evidence" ]]; then
        _kubectl_job_segment_state=MISSING
    else
        kubectl_verify_object_identity Job "$name" "$namespace" "$nonce" "$run_id" \
            "$expected_uid" >/dev/null || return 1
        IFS='|' read -r observed_uid suspend active conditions <<< "$evidence"
        [[ "$observed_uid" == "$expected_uid" ]] || return 1
        case ",$conditions" in
            *,Complete,*) _kubectl_job_segment_state=COMPLETE ;;
            *,Failed,*) _kubectl_job_segment_state=FAILED ;;
            *)
                _kubectl_job_segment_state=ACTIVE
                [[ "$suspend" != true ]] || _kubectl_job_segment_state=SUSPENDED
                ;;
        esac
    fi
    printf -v "$output_name" '%s' "$_kubectl_job_segment_state"
}

# Start one suspended coordinator Job. The JSON-patch test on the UID makes
# the patch apply only to the exact journaled Job, as the sequencer's does.
kubectl_unsuspend_segment_job() {
    local name="$1" namespace="$2" uid="$3" patch
    kubectl_validate_object_name "$name" && kubectl_validate_namespace_name "$namespace" \
        && kubectl_validate_uid "$uid" || return 1
    patch=$(printf '[{"op":"test","path":"/metadata/uid","value":"%s"},{"op":"replace","path":"/spec/suspend","value":false}]' \
        "$uid") || return 1
    kubectl_run_bounded -n "$namespace" patch job "$name" --type=json -p "$patch" >/dev/null
}

# _kubectl_observe_selected_segment OUT KUBERNETES_DIR ATTEMPT_ID: the selected
# segment's Job as kubectl_observe_segment_job reports it, or NOT_STARTED
# (abandoned while suspended), DELETED (deleted after it may have run), or
# COMPLETE when status recorded segment-succeeded[-s<K>]: the Job failed after
# its coordinator was lost, but recovery proved the segment's ledger SUCCESS.
_kubectl_observe_selected_segment() {
    local output_name="$1" kubernetes_dir="$2" attempt_id="$3" suffix key
    local _kubectl_selected_segment_state=""
    suffix=$(_kubectl_selected_segment_suffix) && key=$(_kubectl_selected_segment_key sweep) \
        || return 1
    if kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "segment-not-started$suffix"; then
        _kubectl_selected_segment_state=NOT_STARTED
    elif kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "segment-succeeded$suffix"; then
        _kubectl_selected_segment_state=COMPLETE
    elif kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "delete-$key"; then
        _kubectl_selected_segment_state=DELETED
    else
        kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$key" \
            && [[ "$KUBECTL_RESOURCE_KIND" == Job ]] || return 1
        kubectl_observe_segment_job _kubectl_selected_segment_state "$KUBECTL_RESOURCE_NAME" \
            "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
            "$KUBECTL_RESOURCE_UID" || return 1
    fi
    printf -v "$output_name" '%s' "$_kubectl_selected_segment_state"
}

# _kubectl_classify_segments STATES_VAR KUBERNETES_DIR ATTEMPT_ID COUNT: one
# observation per segment, stored in segment order, observed last to first.
# Segment K+1 starts only after segment K is Complete, and a Complete Job never
# changes, so if a later segment is seen started, every earlier one is still
# seen Complete when it is observed afterwards. Observed first to last, a
# segment that completes between two GETs (and lets the sequencer start the
# next one) would look unfinished while the next one runs.
_kubectl_classify_segments() {
    local -n _kubectl_classified_segments="$1"
    local kubernetes_dir="$2" attempt_id="$3" count="$4" index segment_state
    _kubectl_classified_segments=()
    for ((index = count - 1; index >= 0; index--)); do
        segment_state=""
        kubectl_with_segment "$index" _kubectl_observe_selected_segment segment_state \
            "$kubernetes_dir" "$attempt_id" || return 1
        _kubectl_classified_segments[index]="$segment_state"
    done
}

# _kubectl_current_segment OUT ATTEMPT_ID STATE...: the first segment whose Job
# is not Complete (the count when every Job is). Every later segment must
# never have started.
_kubectl_current_segment() {
    local output_name="$1" attempt_id="$2" index=0 first=-1 segment_state
    shift 2
    for segment_state in "$@"; do
        if (( first < 0 )); then
            [[ "$segment_state" == COMPLETE ]] || first="$index"
        elif [[ ! "$segment_state" =~ ^(SUSPENDED|NOT_STARTED)$ ]]; then
            printf 'Error: %s of attempt %s is %s although %s has not succeeded\n' \
                "$(_kubectl_segment_label "$index")" "$attempt_id" "$segment_state" \
                "$(_kubectl_segment_label "$first")" >&2
            KUBECTL_ATTEMPT_ID="$attempt_id" kubectl_report_lifecycle_error \
                segment-order segment-order LEDGER_INCONSISTENT \
                "inspect every segment's exact coordinator Job; do not delete resources by label" \
                unknown || true
            return 1
        fi
        index=$((index + 1))
    done
    (( first >= 0 )) || first="$index"
    printf -v "$output_name" '%s' "$first"
}

# Recovery for a lost sequencer: start the selected segment's suspended Job
# with the caller's own credentials once every earlier segment is Complete.
_kubectl_advance_selected_segment() {
    local kubernetes_dir="$1" attempt_id="$2" key
    key=$(_kubectl_selected_segment_key sweep) || return 1
    kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$key" \
        && [[ "$KUBECTL_RESOURCE_KIND" == Job ]] || return 1
    printf 'Starting suspended coordinator Job %s/%s for %s\n' \
        "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NAME" \
        "$(_kubectl_segment_label "$(_kubectl_selected_segment)")" >&2
    kubectl_unsuspend_segment_job "$KUBECTL_RESOURCE_NAME" \
        "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_UID"
}

# Delete the exact sequencer Job, so it starts no further segment.
_kubectl_stop_sequencer() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    local intent="$kubernetes_dir/attempts/$attempt_id/creation-intents/sequencer.sh"
    kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" delete-sequencer && return 0
    if [[ -e "$intent" || -L "$intent" ]]; then
        kubectl_cleanup_creation_intent "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            sequencer || return 1
    fi
    kubectl_attempt_resource_exists "$kubernetes_dir" "$attempt_id" sequencer || return 0
    kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" sequencer || return 1
    kubectl_delete_owned_object "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" \
        "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
        "$KUBECTL_RESOURCE_UID" || return 1
    kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" delete-sequencer
}

# Abandon segments FROM..count-1: each still-suspended Job is recorded as
# never started, then deleted. The caller stopped the sequencer first.
_kubectl_abandon_later_segments() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" from="$4" index
    for ((index = from; index < ${KUBECTL_SEGMENT_COUNT:-1}; index++)); do
        kubectl_with_segment "$index" _kubectl_abandon_selected_segment \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" || return 1
    done
}

_kubectl_abandon_selected_segment() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" suffix key observed=""
    suffix=$(_kubectl_selected_segment_suffix) && key=$(_kubectl_selected_segment_key sweep) \
        || return 1
    kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "delete-$key" && return 0
    if ! kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "segment-not-started$suffix"; then
        _kubectl_observe_selected_segment observed "$kubernetes_dir" "$attempt_id" || return 1
        if [[ "$observed" != SUSPENDED ]]; then
            printf 'Error: the coordinator Job of %s is %s, not suspended; refusing to abandon it\n' \
                "$(_kubectl_segment_label "$(_kubectl_selected_segment)")" \
                "$observed" >&2
            return 1
        fi
        kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "segment-not-started$suffix" || return 1
    fi
    kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$key" || return 1
    kubectl_delete_owned_object "$KUBECTL_RESOURCE_KIND" "$KUBECTL_RESOURCE_NAME" \
        "$KUBECTL_RESOURCE_NAMESPACE" "$KUBECTL_RESOURCE_NONCE" "$attempt_id" \
        "$KUBECTL_RESOURCE_UID" || return 1
    kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" "delete-$key"
}

# The number of execution definitions segment INDEX was submitted with.
_kubectl_segment_execution_count() {
    local kubernetes_dir="$1" attempt_id="$2" index="$3" suffix definition count=0
    suffix=$(kubectl_segment_suffix "$index") || return 1
    for definition in "$kubernetes_dir/attempts/$attempt_id/control-bundle$suffix/executions"/[0-9][0-9][0-9][0-9].sh; do
        [[ -f "$definition" && ! -L "$definition" ]] && count=$((count + 1))
    done
    (( count > 0 )) || return 1
    printf '%s\n' "$count"
}

# The public outcome of a multi-segment attempt from its CURRENT segment and
# that segment's PVC state.
_kubectl_multi_segment_outcome() {
    local current="$1" count="$2" current_state="$3"
    if (( current >= count )); then
        printf 'SUCCESS\n'
        return
    fi
    case "$current_state" in
        FAILED|CANCELLED) printf '%s\n' "$current_state" ;;
        SUCCESS)
            if (( current == count - 1 )); then printf 'SUCCESS\n'; else printf 'RUNNING\n'; fi
            ;;
        PREPARED)
            if (( current == 0 )); then printf 'PREPARED\n'; else printf 'RUNNING\n'; fi
            ;;
        *) printf 'RUNNING\n' ;;
    esac
}

# --status for a multi-segment attempt: earlier segments succeeded, the
# current segment is observed through an inspector in its namespace (and
# started here when the sequencer did not), later segments have not started.
# Prints the usual view with the counts of every segment, then SEGMENT_COUNT,
# SEGMENT_CURRENT and one SEGMENT_<K>_NAMESPACE and SEGMENT_<K>_STATE per
# segment.
_kubectl_multi_segment_status() {
    local results_dir="$1" kubernetes_dir="$2" lock_fd="$3" attempt_id="$4"
    local count="${KUBECTL_SEGMENT_COUNT:-1}" current index segment_view="" current_state=""
    local executions p=0 r=0 s=0 f=0 pending=0 running=0 success=0 failed=0 outcome
    local failed_id=""
    local -a segments=() states=()
    for ((index = 0; index < count; index++)); do
        _kubectl_classify_segments segments "$kubernetes_dir" "$attempt_id" "$count" \
            || return 1
        _kubectl_current_segment current "$attempt_id" "${segments[@]}" || return 1
        (( current < count )) || break
        if [[ "${segments[current]}" == SUSPENDED \
                && "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED ]]; then
            kubectl_with_segment "$current" _kubectl_advance_selected_segment \
                "$kubernetes_dir" "$attempt_id" || return 1
        fi
        kubectl_with_segment "$current" _kubectl_status_selected_segment segment_view \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" "$count" || return $?
        current_state="${segment_view%% *}"
        read -r p r s f failed_id <<< "${segment_view#* }"
        # A coordinator lost after its last cell leaves a failed (or deleted)
        # Job, so the sequencer stops; recovery proved the ledger SUCCESS.
        # Record that and continue with the next segment.
        kubectl_with_segment "$current" _kubectl_record_recovered_segment \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" "${segments[current]}" \
            "$current_state" "$count" || break
    done
    for ((index = 0; index < count; index++)); do
        if (( index == current )); then
            states+=("$current_state")
            [[ "$current_state" != PREPARED ]] || states[index]=PENDING
            pending=$((pending + p)) running=$((running + r))
            success=$((success + s)) failed=$((failed + f))
            continue
        fi
        executions=$(_kubectl_segment_execution_count "$kubernetes_dir" "$attempt_id" \
            "$index") || return 1
        if (( index < current )); then
            states+=(SUCCESS)
            success=$((success + executions))
        else
            states+=(NOT_STARTED)
            pending=$((pending + executions))
        fi
    done
    outcome=$(_kubectl_multi_segment_outcome "$current" "$count" "$current_state")
    kubectl_emit_status_view "$results_dir" "$attempt_id" "$outcome" PVC PENDING \
        "$pending $running $success $failed $failed_id" || return 1
    _kubectl_emit_segment_states "$(( current < count ? current : count - 1 ))" "${states[@]}"
}

# Whether worker drift handling stopped the attempt; it journals
# stopped-by-drift before it deletes a Job of a multi-segment attempt.
_kubectl_attempt_stopped_by_drift() {
    kubectl_attempt_step_done "$1" "$2" stopped-by-drift
}

# The outcome of a multi-segment attempt whose started segments all succeeded
# but whose later segments never started: FAILED when worker drift stopped
# it, otherwise CANCELLED, because a user's cancellation abandoned them.
_kubectl_abandoned_attempt_outcome() {
    if _kubectl_attempt_stopped_by_drift "$1" "$2"; then
        printf 'FAILED\n'
    else
        printf 'CANCELLED\n'
    fi
}

# Record segment-succeeded[-s<K>] for the selected segment when its Job
# (JOB_STATE) failed or vanished but its recovered PVC ledger (PVC_STATE) is
# SUCCESS and a later segment remains: status then starts the next segment,
# which the sequencer, having seen Failed, will not. Returns 1 otherwise.
_kubectl_record_recovered_segment() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" job_state="$4"
    local pvc_state="$5" count="$6" segment suffix
    segment=$(_kubectl_selected_segment) && suffix=$(_kubectl_selected_segment_suffix) \
        || return 1
    [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED && "$pvc_state" == SUCCESS \
        && "$job_state" =~ ^(FAILED|MISSING)$ ]] && (( segment < count - 1 )) || return 1
    printf '%s succeeded after coordinator recovery; continuing with %s\n' \
        "$(_kubectl_segment_label "$segment")" \
        "$(_kubectl_segment_label "$((segment + 1))")" >&2
    kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        "segment-succeeded$suffix"
}

# Status or collect of a multi-segment attempt whose publication is local.
_kubectl_project_collected_segments() {
    local operation="$1" results_dir="$2" kubernetes_dir="$3" attempt_id="$4"
    local collection="$5" terminal="" progress="" index current=0
    local -a states=()
    _kubectl_collected_segments_outcome terminal progress states "$kubernetes_dir" \
        "$attempt_id" "${KUBECTL_SEGMENT_COUNT:-1}" || return 1
    if [[ "$operation" != status ]]; then
        _kubectl_emit_collected_state "$terminal" "$results_dir" "$attempt_id"
        [[ "$terminal" == SUCCESS ]]
        return
    fi
    for index in "${!states[@]}"; do
        [[ "${states[index]}" == NOT_STARTED ]] || current="$index"
    done
    kubectl_emit_status_view "$results_dir" "$attempt_id" "$terminal" LOCAL \
        "$collection" "$progress" || return 1
    _kubectl_emit_segment_states "$current" "${states[@]}"
}

# Enter cancellation of a multi-segment attempt: SUBMITTED -> CANCEL_REQUESTED
# before any exact deletion, then stop the sequencer. Both happen before the
# segments are classified, so no segment can start after that observation.
# A repeated cancel (CANCEL_REQUESTED), or one after the outcome became
# terminal, only makes sure that the sequencer is gone.
_kubectl_begin_multi_segment_cancel() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$attempt_id" || return 1
    case "$KUBECTL_LIFECYCLE_STATE" in
        SUBMITTED)
            kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" \
                CANCEL_REQUESTED || return 1
            ;;
        CANCEL_REQUESTED|TERMINAL|COLLECTION_IN_PROGRESS) ;;
        *) return 1 ;;
    esac
    _kubectl_stop_sequencer "$kubernetes_dir" "$lock_fd" "$attempt_id"
}

# Cancel the selected segment through a fresh inspector when its PVC ledger is
# not terminal, and set OUT to the outcome the ledger then holds: CANCELLED,
# or the outcome it reached first. A coordinator can finish while cancellation
# is under way; its SUCCESS is never overwritten. A segment whose Job this
# cancellation deleted but whose ledger is SUCCESS is journaled
# segment-succeeded[-s<K>], so every later observation treats it as Complete.
_kubectl_cancel_selected_multi_segment() {
    local output_name="$1" kubernetes_dir="$2" lock_fd="$3" attempt_id="$4"
    local inspector="" inspector_uid="" observed="" rc=0
    _kubectl_create_inspector inspector inspector_uid status "$kubernetes_dir" \
        "$lock_fd" "$attempt_id" || return 1
    observed=$(kubectl_read_remote_status "$KUBECTL_NAMESPACE" "$inspector" \
        "$attempt_id") || rc=1
    if [[ "$rc" -eq 0 && "$observed" =~ ^(PREPARED|RUNNING)$ ]]; then
        _kubectl_cancel_selected_segment "$kubernetes_dir" "$lock_fd" \
            "$KUBECTL_NAMESPACE" "$inspector" "$attempt_id" || rc=1
        [[ "$rc" -ne 0 ]] || observed=$(kubectl_read_remote_status \
            "$KUBECTL_NAMESPACE" "$inspector" "$attempt_id") || rc=1
    fi
    if [[ "$rc" -eq 0 && ! "$observed" =~ ^(SUCCESS|FAILED|CANCELLED)$ ]]; then
        printf 'Error: %s has no terminal ledger after cancellation (observed %s)\n' \
            "$(_kubectl_segment_label "$(_kubectl_selected_segment)")" \
            "${observed:-nothing}" >&2
        rc=1
    fi
    if [[ "$rc" -eq 0 && "$observed" == SUCCESS ]]; then
        _kubectl_record_deleted_segment_success "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" || rc=1
    fi
    _kubectl_remove_inspector "$kubernetes_dir" "$lock_fd" "$inspector" \
        "$inspector_uid" "$attempt_id" || rc=1
    [[ "$rc" -ne 0 ]] || printf -v "$output_name" '%s' "$observed"
    return "$rc"
}

# The selected segment's ledger is SUCCESS although cancellation or drift
# handling deleted its Job (its coordinator finished first): journal
# segment-succeeded[-s<K>], so its DELETED Job is treated as Complete.
_kubectl_record_deleted_segment_success() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3" suffix key
    suffix=$(_kubectl_selected_segment_suffix) && key=$(_kubectl_selected_segment_key sweep) \
        || return 1
    kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "delete-$key" || return 0
    kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "segment-succeeded$suffix" \
        && return 0
    kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        "segment-succeeded$suffix"
}

# _kubectl_cancelling_segment OUT KUBERNETES_DIR ATTEMPT_ID: the segment that
# a CANCEL_REQUESTED multi-segment attempt is cancelling, for --status. The
# sequencer is stopped first, so that is the first segment whose Job is not
# Complete. When the Jobs cannot be observed, it is the first segment that the
# local journal records neither as succeeded nor as never started.
_kubectl_cancelling_segment() {
    local output_name="$1" kubernetes_dir="$2" attempt_id="$3"
    local count="${KUBECTL_SEGMENT_COUNT:-1}" index suffix _kubectl_cancelling=""
    local -a segments=()
    if _kubectl_classify_segments segments "$kubernetes_dir" "$attempt_id" "$count" \
            2>/dev/null \
            && _kubectl_current_segment _kubectl_cancelling "$attempt_id" "${segments[@]}" \
                >/dev/null 2>&1; then
        :
    else
        _kubectl_cancelling=""
        for ((index = 0; index < count; index++)); do
            suffix=$(kubectl_segment_suffix "$index") || break
            kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" \
                "segment-succeeded$suffix" && continue
            kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" \
                "segment-not-started$suffix" && continue
            _kubectl_cancelling="$index"
            break
        done
    fi
    [[ -n "$_kubectl_cancelling" ]] && (( _kubectl_cancelling < count )) \
        || _kubectl_cancelling=$((count - 1))
    printf -v "$output_name" '%s' "$_kubectl_cancelling"
}

# --cancel for a multi-segment attempt: enter CANCEL_REQUESTED and stop the
# sequencer first, then classify the segments and cancel the first one that
# has not succeeded. A segment that succeeds meanwhile passes cancellation to
# the next one, including a next one that the sequencer started just before it
# was stopped. Every later segment is abandoned. Workers, run trees and Leases
# remain for collection. Prints the attempt's outcome. Every step is journaled
# or idempotent, so repeating an interrupted cancel completes it.
_kubectl_cancel_multi_segment() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    local count="${KUBECTL_SEGMENT_COUNT:-1}" current segment_outcome="" outcome=SUCCESS
    local abandon_from
    local -a segments=()
    _kubectl_begin_multi_segment_cancel "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        || return 1
    _kubectl_classify_segments segments "$kubernetes_dir" "$attempt_id" "$count" \
        && _kubectl_current_segment current "$attempt_id" "${segments[@]}" || return 1
    abandon_from=$((current + 1))
    while (( current < count )); do
        abandon_from=$((current + 1))
        segment_outcome=""
        kubectl_with_segment "$current" _kubectl_cancel_selected_multi_segment \
            segment_outcome "$kubernetes_dir" "$lock_fd" "$attempt_id" || return 1
        outcome="$segment_outcome"
        if [[ "$segment_outcome" != SUCCESS ]] || (( current >= count - 1 )); then
            break
        fi
        current=$((current + 1))
    done
    if (( abandon_from < count )); then
        _kubectl_abandon_later_segments "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" "$abandon_from" || return 1
    fi
    kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$attempt_id" || return 1
    case "$KUBECTL_LIFECYCLE_STATE" in
        SUBMITTED|CANCEL_REQUESTED)
            kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" \
                TERMINAL || return 1
            ;;
        TERMINAL|COLLECTION_IN_PROGRESS) ;;
        *) return 1 ;;
    esac
    printf '%s\n' "$outcome"
}

# --collect for a multi-segment attempt. Every segment that started is gated
# (terminal PVC outcome, quiescent exact Job; worker drift on the last one),
# then streamed, then imported in segment order as collected-state[-s<K>];
# segments that never started contribute nothing. Then every segment's run
# tree is released, every owned object (including the sequencer and its RBAC)
# is deleted, and the Leases are released in reverse claim order.
_kubectl_collect_multi_segment_attempt() {
    local results_dir="$1" kubernetes_dir="$2" lock_fd="$3" attempt_id="$4"
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id" count="${KUBECTL_SEGMENT_COUNT:-1}"
    local current last index rc=0 terminal="" progress=""
    local -a segments=() inspectors=() inspector_uids=() remote_states=() states=()
    local -a archive_roots=() archives=()
    if [[ "$KUBECTL_LIFECYCLE_STATE" == COLLECTION_IN_PROGRESS ]] \
            && kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" collection-published; then
        _kubectl_collect_multi_segment_teardown "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" inspectors inspector_uids || rc=1
    else
        _kubectl_classify_segments segments "$kubernetes_dir" "$attempt_id" "$count" \
            && _kubectl_current_segment current "$attempt_id" "${segments[@]}" || return 1
        last=$(( current < count ? current : count - 1 ))
        # A segment abandoned before it started (cancellation) is not gated:
        # the last segment that ran is.
        if (( current < count && current > 0 )) \
                && [[ "${segments[current]}" == NOT_STARTED ]]; then
            last=$((current - 1))
        fi
        _kubectl_collect_multi_segment_segments || rc=1
        for index in "${!archive_roots[@]}"; do
            [[ -z "${archive_roots[index]}" ]] || rm -rf -- "${archive_roots[index]}" \
                || rc=1
        done
        [[ "$rc" -ne 0 ]] || _kubectl_collect_multi_segment_teardown "$kubernetes_dir" \
            "$lock_fd" "$attempt_id" inspectors inspector_uids || rc=1
        if [[ "$rc" -ne 0 ]]; then
            kubectl_preserve_attempt_diagnostics "$kubernetes_dir" "$attempt_id" \
                collect-failed
            for index in "${!inspectors[@]}"; do
                kubectl_with_segment "$index" _kubectl_remove_inspector "$kubernetes_dir" \
                    "$lock_fd" "${inspectors[index]}" "${inspector_uids[index]}" \
                    "$attempt_id" || true
            done
        fi
    fi
    [[ "$rc" -eq 0 ]] || return "$rc"
    _kubectl_collected_segments_outcome terminal progress states "$kubernetes_dir" \
        "$attempt_id" "$count" || return 1
    _kubectl_emit_collected_state "$terminal" "$results_dir" "$attempt_id"
    [[ "$terminal" == SUCCESS ]]
}

# The gate, stream and import phases of _kubectl_collect_multi_segment_attempt,
# for segments 0..last (its locals).
_kubectl_collect_multi_segment_segments() {
    local index remote_state archive_root archive staging state_dir
    local collect_job_state="" collect_job_rc=0 collect_job_uid=""
    local collect_job_kind="" collect_job_name="" collect_job_namespace=""
    local collect_expected_uid="" local_state_path="$metadata_dir/state.sh" remote_state_path
    remote_state_path="$(kubectl_attempt_remote_root "$attempt_id")/state/run.status" \
        || return 1
    for ((index = 0; index <= last; index++)); do
        remote_state="" collect_job_state="" collect_job_rc=0 collect_job_uid=""
        kubectl_with_segment "$index" _kubectl_create_inspector "inspectors[$index]" \
            "inspector_uids[$index]" collector "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" || return 1
        kubectl_with_segment "$index" _kubectl_collect_segment_gate "$kubernetes_dir" \
            "$attempt_id" "${inspectors[index]}" "$(( index == last ? 1 : 0 ))" \
            || return $?
        if (( index < last )) && [[ "$remote_state" != SUCCESS ]]; then
            printf 'Error: %s ended %s, but a later segment started\n' \
                "$(_kubectl_segment_label "$index")" "$remote_state" >&2
            return 1
        fi
        if (( index == last && last < count - 1 )) && [[ "$remote_state" == SUCCESS \
                && "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED ]]; then
            _kubectl_report_collect_terminal_gate "$attempt_id" \
                "wait until the next segment finishes (or run --cancel), then retry --collect"
            return 1
        fi
        remote_states[index]="$remote_state"
    done
    # Nothing may start once collection begins.
    _kubectl_stop_sequencer "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        && _kubectl_abandon_later_segments "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            "$((last + 1))" || return 1
    if [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED ]]; then
        kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            TERMINAL || return 1
    fi
    for ((index = 0; index <= last; index++)); do
        archive_root="" archive="" remote_state="${remote_states[index]}"
        kubectl_with_segment "$index" _kubectl_collect_segment_stream "$results_dir" \
            "$attempt_id" "${inspectors[index]}" || { archive_roots[index]="$archive_root"; return 1; }
        archive_roots[index]="$archive_root"
        archives[index]="$archive"
    done
    kubectl_attempt_load_metadata "$metadata_dir" || return 1
    if [[ "$KUBECTL_LIFECYCLE_STATE" == TERMINAL ]]; then
        kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            COLLECTION_IN_PROGRESS || return 1
    fi
    local import_rc
    for ((index = 0; index <= last; index++)); do
        archive="${archives[index]}" remote_state="${remote_states[index]}"
        staging="$results_dir/.kubernetes-collect-$attempt_id.${RANDOM}"
        import_rc=0
        kubectl_with_segment "$index" _kubectl_collect_segment_import "$results_dir" \
            "$kubernetes_dir" "$attempt_id" || import_rc=1
        rm -rf -- "$staging" || import_rc=1
        [[ "$import_rc" -eq 0 ]] || return 1
    done
    if ! _kubectl_publish_results_aggregate "$results_dir" "$kubernetes_dir" \
            "$attempt_id"; then
        kubectl_report_collection_failure "$attempt_id" aggregate-publish \
            LEDGER_INCONSISTENT \
            "inspect run.status, run-summary.tsv and the collected segments, then retry --collect" \
            "$local_state_path" "$remote_state_path" "" "" "" "" "" "" ""
        return 1
    fi
    kubectl_attempt_journal_step "$kubernetes_dir" "$lock_fd" "$attempt_id" \
        collection-published
}

# Release every segment's run tree (through INSPECTORS_VAR[K] when collection
# created one, else a fresh collector), delete every owned object, release the
# Leases in reverse claim order, and mark the attempt COLLECTED.
_kubectl_collect_multi_segment_teardown() {
    local kubernetes_dir="$1" lock_fd="$2" attempt_id="$3"
    local -n _kubectl_teardown_inspectors="$4" _kubectl_teardown_uids="$5"
    local count="${KUBECTL_SEGMENT_COUNT:-1}" index suffix rc=0
    if kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" release-pvc-lease; then
        # Claim 0's Lease is released last. Once it is, touch nothing else.
        kubectl_with_claim 0 kubectl_collection_cleanup_precedes_lease_release \
            "$kubernetes_dir" "$attempt_id" || return 1
        kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" COLLECTED
        return
    fi
    for ((index = 0; index < count; index++)); do
        suffix=$(kubectl_segment_suffix "$index") || return 1
        kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" \
            "release-remote-run$suffix" && continue
        [[ -f "$kubernetes_dir/attempts/$attempt_id/remote-reservation$suffix.sh" ]] \
            || continue
        if [[ -n "${_kubectl_teardown_inspectors[index]:-}" ]]; then
            kubectl_with_segment "$index" _kubectl_release_selected_run_tree \
                "$kubernetes_dir" "$lock_fd" "${_kubectl_teardown_inspectors[index]}" \
                "$attempt_id" || { rc=1; break; }
        else
            kubectl_with_segment "$index" _kubectl_release_selected_run_tree_with_inspector \
                "$kubernetes_dir" "$lock_fd" "$attempt_id" collector || { rc=1; break; }
        fi
    done
    [[ "$rc" -ne 0 ]] || kubectl_cleanup_journaled_resources "$kubernetes_dir" \
        "$lock_fd" "$attempt_id" || rc=1
    for index in "${!_kubectl_teardown_inspectors[@]}"; do
        _kubectl_remove_inspector "$kubernetes_dir" "$lock_fd" \
            "${_kubectl_teardown_inspectors[index]}" "${_kubectl_teardown_uids[index]}" \
            "$attempt_id" || rc=1
    done
    _kubectl_teardown_inspectors=()
    _kubectl_teardown_uids=()
    [[ "$rc" -ne 0 ]] || _kubectl_release_attempt_leases "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" || rc=1
    [[ "$rc" -ne 0 ]] || kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" COLLECTED || rc=1
    return "$rc"
}

# Report why a results directory has no loadable attempt. Load status 2 means
# the v1.x rejection has already explained it.
_kubectl_no_current_attempt_error() {
    local results_dir="$1" load_rc="$2" actions="$3"
    [[ "$load_rc" -ne 2 ]] || return 0
    echo "Error: result directory has no valid current Kubernetes attempt: $results_dir" >&2
    echo "A submission that failed before publishing attempt identity cannot be $actions." >&2
}

# Reject a missing directory or one without Kubernetes state before taking the
# lifecycle lock, which would otherwise create kubernetes/ inside it.
_kubectl_require_lifecycle_results_dir() {
    local operation="$1" results_dir="$2" substrate
    if [[ ! -d "$results_dir" ]]; then
        echo "Error: results directory not found: $results_dir" >&2
        return 1
    fi
    [[ ! -d "$results_dir/kubernetes" ]] || return 0
    substrate=$(sed -n 's/^export EXECUTION_SUBSTRATE=//p' "$results_dir/env_used.sh" \
        2>/dev/null | tail -n 1)
    case "$substrate" in
        ssh|slurm)
            echo "Error: --$operation applies to Kubernetes sweeps and prepared batches; $results_dir is an EXECUTION_SUBSTRATE=$substrate sweep (use --resume to continue it)" >&2
            ;;
        *) _kubectl_no_current_attempt_error "$results_dir" 1 "inspected, collected, or cancelled" ;;
    esac
    return 1
}

kubectl_lifecycle_operation() {
    local operation="$1" results_dir="$2" kubernetes_dir attempt_id lock_fd inspector inspector_uid remote_state rc=0
    [[ "$operation" =~ ^(status|cancel|collect)$ ]] || return 1
    _kubectl_require_lifecycle_results_dir "$operation" "$results_dir" || return 1
    kubernetes_dir="$results_dir/kubernetes"
    kubectl_local_lock_acquire "$kubernetes_dir" lock_fd || return 1
    _kubectl_load_saved_attempt "$results_dir" attempt_id || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        _kubectl_no_current_attempt_error "$results_dir" "$rc" \
            "inspected, collected, or cancelled"
        rc=1
    fi
    [[ "$rc" -ne 0 ]] || kubectl_cleanup_ephemeral_helpers "$kubernetes_dir" "$lock_fd" \
        "$attempt_id" || rc=1
    if [[ "$rc" -eq 0 && "${KUBECTL_SEGMENT_COUNT:-1}" -gt 1 ]] \
            && { [[ "$KUBECTL_LIFECYCLE_STATE" == COLLECTED ]] \
                || { [[ "$operation" == status \
                        && "$KUBECTL_LIFECYCLE_STATE" == COLLECTION_IN_PROGRESS ]] \
                    && kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" \
                        collection-published; }; }; then
        local collection_view=COMPLETE
        [[ "$KUBECTL_LIFECYCLE_STATE" == COLLECTED ]] || collection_view=CLEANUP_PENDING
        kubectl_local_lock_release "$lock_fd" || return 1
        _kubectl_project_collected_segments "$operation" "$results_dir" "$kubernetes_dir" \
            "$attempt_id" "$collection_view"
        return
    fi
    if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == COLLECTED ]]; then
        local collected_terminal=""
        _kubectl_validate_collected_publication \
            "$kubernetes_dir/attempts/$attempt_id/collected-state" "$attempt_id" \
            collected_terminal \
            "$kubernetes_dir/attempts/$attempt_id/control-bundle/executions" \
            >/dev/null || rc=1
        kubectl_local_lock_release "$lock_fd" || rc=1
        [[ "$rc" -eq 0 ]] || return "$rc"
        if [[ "$operation" == status ]]; then
            local collected_progress
            collected_progress=$(kubectl_read_collected_execution_progress \
                "$kubernetes_dir/attempts/$attempt_id/collected-state") || return 1
            kubectl_emit_status_view "$results_dir" "$attempt_id" "$collected_terminal" \
                LOCAL COMPLETE "$collected_progress" || return 1
        else
            _kubectl_emit_collected_state "$collected_terminal" "$results_dir" "$attempt_id"
        fi
        [[ "$operation" == status || "$collected_terminal" == SUCCESS ]]
        return
    fi
    if [[ "$rc" -eq 0 && "$operation" == status \
            && "$KUBECTL_LIFECYCLE_STATE" == COLLECTION_IN_PROGRESS \
            && -d "$kubernetes_dir/attempts/$attempt_id/collected-state" ]]; then
        # Local publication is the linearization point for result import. If
        # the client died after remote cleanup, status must still project the
        # durable outcome without requiring cluster access. A later collect
        # resumes any remaining exact cleanup and advances to COLLECTED.
        local published_terminal=""
        _kubectl_validate_collected_publication \
            "$kubernetes_dir/attempts/$attempt_id/collected-state" "$attempt_id" \
            published_terminal \
            "$kubernetes_dir/attempts/$attempt_id/control-bundle/executions" \
            >/dev/null || rc=1
        kubectl_local_lock_release "$lock_fd" || rc=1
        [[ "$rc" -eq 0 ]] || return "$rc"
        local published_progress
        published_progress=$(kubectl_read_collected_execution_progress \
            "$kubernetes_dir/attempts/$attempt_id/collected-state") || return 1
        kubectl_emit_status_view "$results_dir" "$attempt_id" "$published_terminal" \
            LOCAL CLEANUP_PENDING "$published_progress" || return 1
        return 0
    fi
    if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]; then
        # This repair is local durable state and must not be blocked by a
        # Kubernetes identity check.  It closes the crash window between
        # terminalizing a failed resume and restoring its collected parent.
        kubectl_attempt_restore_predecessor "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" || rc=1
    fi
    # Every segment's namespace, and every claim and its bound PV, must keep
    # its identity.
    [[ "$rc" -ne 0 ]] || _kubectl_for_each_segment "${KUBECTL_SEGMENT_COUNT:-1}" \
        _kubectl_verify_saved_cluster_identity "$kubernetes_dir" "$attempt_id" || rc=1
    if [[ "$rc" -eq 0 \
            && "$KUBECTL_LIFECYCLE_STATE" =~ ^(SUBMITTED|CANCEL_REQUESTED|TERMINAL|COLLECTION_IN_PROGRESS)$ ]]; then
        if [[ "$operation" == collect \
                && "$KUBECTL_LIFECYCLE_STATE" == COLLECTION_IN_PROGRESS ]]; then
            # Observe without first emitting the ordinary mismatch diagnostic:
            # a successor may legitimately own the deterministic Lease name
            # after this attempt deleted its exact UID but missed journaling it.
            # Leases are released last claim first, so reconcile in that order.
            local lease_claim
            for ((lease_claim = ${KUBECTL_CLAIM_COUNT:-1} - 1; lease_claim >= 0; lease_claim--)); do
                kubectl_with_claim "$lease_claim" \
                    kubectl_reconcile_pvc_lease_release_after_collection_cleanup \
                    "$kubernetes_dir" "$lock_fd" "$attempt_id" || { rc=1; break; }
            done
        else
            _kubectl_for_each_claim kubectl_verify_journaled_pvc_lease "$kubernetes_dir" \
                "$attempt_id" || rc=1
        fi
    fi
    if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == PREPARED ]]; then
        kubectl_recover_prepared_attempt "$kubernetes_dir" "$lock_fd" \
            "$attempt_id" || rc=1
        if [[ "$rc" -eq 0 ]]; then
            _kubectl_emit_submission_failed "$operation" "$results_dir"
            [[ "$operation" != collect ]] || rc=1
        fi
    elif [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]; then
        _kubectl_emit_submission_failed "$operation" "$results_dir"
        [[ "$operation" != collect ]] || rc=1
    elif [[ "$rc" -eq 0 && "$operation" == status \
            && "$KUBECTL_LIFECYCLE_STATE" == CANCEL_REQUESTED ]]; then
        local cancel_kind="" cancel_name="" cancel_namespace=""
        local cancel_uid="" cancel_target=0 cancel_key=sweep
        if [[ "${KUBECTL_SEGMENT_COUNT:-1}" -gt 1 ]]; then
            _kubectl_cancelling_segment cancel_target "$kubernetes_dir" "$attempt_id"
            cancel_key=$(kubectl_with_segment "$cancel_target" \
                _kubectl_selected_segment_key sweep) || cancel_key=""
        fi
        if [[ -n "$cancel_key" ]] \
                && kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$cancel_key"; then
            cancel_kind="$KUBECTL_RESOURCE_KIND"
            cancel_name="$KUBECTL_RESOURCE_NAME"
            cancel_namespace="$KUBECTL_RESOURCE_NAMESPACE"
            cancel_uid="$KUBECTL_RESOURCE_UID"
        else
            printf 'STATE=CANCEL_REQUESTED\nNEXT_ACTION=INSPECT\n'
            KUBECTL_ATTEMPT_ID="$attempt_id" \
                KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="$kubernetes_dir/attempts/$attempt_id/state.sh" \
                KUBECTL_DIAGNOSTIC_REMOTE_STATE_PATH="$(kubectl_attempt_remote_root "$attempt_id")/state/run.status" \
                kubectl_report_lifecycle_error status cancellation-journal \
                    LEDGER_INCONSISTENT \
                    "inspect the missing or corrupt sweep Job journal; do not cancel by label" \
                    unknown || true
            rc=1
        fi
        if [[ "$rc" -eq 0 ]]; then
            printf 'STATE=CANCEL_REQUESTED\nNEXT_ACTION=INSPECT\n'
            if [[ "${KUBECTL_SEGMENT_COUNT:-1}" -gt 1 ]]; then
                printf 'SEGMENT_CURRENT=%s\nSEGMENT_%s_NAMESPACE=%s\n' "$cancel_target" \
                    "$cancel_target" "${KUBECTL_SEGMENT_NAMESPACES[cancel_target]}"
                printf 'Cancellation of %s is incomplete\n' \
                    "$(_kubectl_segment_label "$cancel_target")" >&2
            fi
            KUBECTL_ATTEMPT_ID="$attempt_id" \
            KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="$kubernetes_dir/attempts/$attempt_id/state.sh" \
            KUBECTL_DIAGNOSTIC_REMOTE_STATE_PATH="$(kubectl_attempt_remote_root "$attempt_id")/state/run.status" \
            kubectl_report_lifecycle_error status cancellation \
                CANCELLATION_INCOMPLETE \
                "retry --cancel for the same results directory" unknown \
                "$cancel_kind" "$cancel_name" "$cancel_namespace" \
                "$cancel_uid" "" "" NOT_OBSERVED || true
            rc=1
        fi
    elif [[ "$rc" -eq 0 && "$operation" == status ]]; then
        if [[ "${KUBECTL_SEGMENT_COUNT:-1}" -gt 1 ]]; then
            _kubectl_multi_segment_status "$results_dir" "$kubernetes_dir" "$lock_fd" \
                "$attempt_id" || rc=$?
        else
            local segment_view=""
            _kubectl_status_selected_segment segment_view "$kubernetes_dir" "$lock_fd" \
                "$attempt_id" || rc=$?
            if [[ "$rc" -eq 0 ]]; then
                remote_state=${segment_view%% *}
                kubectl_emit_status_view "$results_dir" "$attempt_id" "$remote_state" \
                    PVC PENDING "${segment_view#* }" || rc=1
            fi
        fi
    elif [[ "$rc" -eq 0 && "$operation" == cancel ]]; then
        if [[ "$KUBECTL_LIFECYCLE_STATE" == COLLECTED ]]; then
            kubectl_emit_state COLLECTED
        elif [[ "${KUBECTL_SEGMENT_COUNT:-1}" -gt 1 ]]; then
            remote_state=$(_kubectl_cancel_multi_segment "$kubernetes_dir" "$lock_fd" \
                "$attempt_id") || rc=1
            [[ "$rc" -ne 0 ]] || kubectl_emit_state "$remote_state"
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

# Whether the attempt's collected publication is installed and verifies: one
# segment's collected-state, or, for several segments, the collection-published
# step plus every started segment's collected-state[-s<K>].
_kubectl_collection_is_published() {
    local kubernetes_dir="$1" attempt_id="$2" count="${3:-1}"
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id" terminal=""
    if (( count > 1 )); then
        kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" collection-published \
            || return 1
        local progress=""
        local -a states=()
        _kubectl_collected_segments_outcome terminal progress states \
            "$kubernetes_dir" "$attempt_id" "$count"
        return
    fi
    [[ -d "$metadata_dir/collected-state" && ! -L "$metadata_dir/collected-state" ]] \
        || return 1
    _kubectl_validate_collected_publication "$metadata_dir/collected-state" \
        "$attempt_id" terminal "$metadata_dir/control-bundle/executions" >/dev/null
}

# _kubectl_collected_segments_outcome TERMINAL_VAR PROGRESS_VAR STATES_VAR
# KUBERNETES_DIR ATTEMPT_ID COUNT: verify each collected segment publication
# and aggregate a multi-segment attempt. A segment that never started has no
# publication; its bundled executions remain PENDING. The outcome is the first
# segment's non-SUCCESS outcome (later segments never start), else SUCCESS.
_kubectl_collected_segments_outcome() {
    local -n _kubectl_outcome_terminal="$1" _kubectl_outcome_progress="$2"
    local -n _kubectl_outcome_states="$3"
    local kubernetes_dir="$4" attempt_id="$5" count="$6"
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id" index suffix state_dir
    local segment_terminal segment_progress outcome="" not_started=0
    local pending=0 running=0 success=0 failed=0 p r s f id failed_id="" definition
    _kubectl_outcome_states=()
    for ((index = 0; index < count; index++)); do
        suffix=$(kubectl_segment_suffix "$index") || return 1
        state_dir="$metadata_dir/collected-state$suffix"
        if [[ -e "$state_dir" || -L "$state_dir" ]]; then
            [[ -d "$state_dir" && ! -L "$state_dir" && "$not_started" -eq 0 ]] || return 1
            segment_terminal=""
            _kubectl_validate_collected_publication "$state_dir" "$attempt_id" \
                segment_terminal "$metadata_dir/control-bundle$suffix/executions" \
                >/dev/null || return 1
            segment_progress=$(kubectl_read_collected_execution_progress "$state_dir") \
                || return 1
            read -r p r s f id <<< "$segment_progress"
            failed_id=${failed_id:-$id}
            _kubectl_outcome_states+=("$segment_terminal")
            [[ -n "$outcome" || "$segment_terminal" == SUCCESS ]] \
                || outcome="$segment_terminal"
        else
            (( index > 0 )) || return 1
            p=0 r=0 s=0 f=0
            for definition in "$metadata_dir/control-bundle$suffix/executions"/[0-9][0-9][0-9][0-9].sh; do
                [[ -f "$definition" && ! -L "$definition" ]] && p=$((p + 1))
            done
            (( p > 0 )) || return 1
            _kubectl_outcome_states+=(NOT_STARTED)
            not_started=1
        fi
        pending=$((pending + p)) running=$((running + r))
        success=$((success + s)) failed=$((failed + f))
    done
    # Unstarted segments that follow only successes were abandoned: never
    # report them as a successful attempt (_kubectl_abandoned_attempt_outcome).
    [[ -n "$outcome" ]] || { [[ "$not_started" -eq 0 ]] && outcome=SUCCESS; } \
        || outcome=$(_kubectl_abandoned_attempt_outcome "$kubernetes_dir" "$attempt_id")
    _kubectl_outcome_terminal="$outcome"
    _kubectl_outcome_progress="$pending $running $success $failed $failed_id"
}

# Print the per-segment status keys of a multi-segment attempt: SEGMENT_COUNT,
# SEGMENT_CURRENT, and SEGMENT_<K>_NAMESPACE and SEGMENT_<K>_STATE per
# segment K, in segment order. CURRENT and the STATE arguments are in segment
# order.
_kubectl_emit_segment_states() {
    local current="$1" index=0 state
    shift
    printf 'SEGMENT_COUNT=%s\nSEGMENT_CURRENT=%s\n' "$#" "$current"
    for state in "$@"; do
        printf 'SEGMENT_%s_NAMESPACE=%s\nSEGMENT_%s_STATE=%s\n' "$index" \
            "${KUBECTL_SEGMENT_NAMESPACES[index]}" "$index" "$state"
        index=$((index + 1))
    done
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
    local control_dir="${definitions_dir%/*}" batch=0
    if [[ -f "$control_dir/batch-manifest.tsv" ]]; then
        elbencho_batch_verify_manifest "$control_dir" subset || return 1
        batch=1
    fi
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
                if [[ "$batch" -eq 1 && "$kind" == ledger ]]; then
                    [[ "$first" == "$second" \
                        && "$first" =~ ^(run\.status|run-summary\.tsv|startup-error\.txt|coordinator-loss\.tsv|executions/[0-9]{4}\.(status|exitcode|workers\.tsv))$ ]] || return 1
                fi
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
                _kubectl_local_file_bytes "$source" bytes || return 1
                digest=$(_kubectl_sha256_file "$source") || return 1
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
    local -a core_paths=(run.status run-summary.tsv env_used.sh env_used.yaml)
    if [[ "$batch" -eq 1 ]]; then
        core_paths+=(batch-manifest.tsv batch-sealed.sha256)
        while IFS= read -r core; do
            core_paths+=("$core/env_used.sh" "$core/env_used.yaml")
        done < <(awk -F '\t' '$1 == "group" {print $4}' "$control_dir/batch-manifest.tsv")
    fi
    for core in "${core_paths[@]}"; do
        [[ -v source_roles["$core"] \
            && "${source_destinations[$core]}" == "$core" ]] || return 1
        if [[ "$core" == env_used.* || "$core" == batch-* || "$core" == groups/* ]]; then
            [[ "${source_roles[$core]}" == snapshot ]] || return 1
            [[ "$batch" -eq 0 ]] || cmp -s -- "$state_dir/$core" "$control_dir/$core" || return 1
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
    if [[ "$batch" -eq 1 ]]; then
        local ledger_source
        for ledger_source in "${!source_roles[@]}"; do
            [[ "${source_roles[$ledger_source]}" == ledger \
                && "$ledger_source" =~ ^executions/([0-9]{4})\. ]] || continue
            [[ -v expected_executions["${BASH_REMATCH[1]}"] ]] || return 1
        done
    fi
    local row remote_result local_result relative_result result_id result_prefix
    for row in "${result_rows[@]}"; do
        IFS=$'\t' read -r remote_result local_result <<< "$row"
        [[ "$remote_result" =~ ^results/([0-9]{4})/(.+)$ ]] || return 1
        result_id="${BASH_REMATCH[1]}"
        relative_result="${BASH_REMATCH[2]}"
        [[ -v expected_executions["$result_id"] ]] || return 1
        if [[ "$batch" -eq 1 && "$relative_result" =~ ^executions/([0-9]{4})\. ]]; then
            [[ "${BASH_REMATCH[1]}" == "$result_id" ]] || return 1
        fi
        result_prefix=""
        if [[ "$batch" -eq 1 ]]; then
            result_prefix=$(elbencho_batch_execution_output_dir "$control_dir" "$result_id") || return 1
            result_prefix="${result_prefix#"$control_dir"/}/"
        fi
        [[ "$local_result" == "$result_prefix$relative_result" ]] || return 1
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
                        result_prefix=""
                        if [[ "$batch" -eq 1 ]]; then
                            result_prefix=$(elbencho_batch_execution_output_dir "$control_dir" "$id") || return 1
                            result_prefix="${result_prefix#"$control_dir"/}/"
                        fi
                        [[ "${destination_roles[${result_prefix}executions/$id.workload.tsv]:-}" \
                            == result \
                            && "${destination_sources[${result_prefix}executions/$id.workload.tsv]:-}" \
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
    unset ELBENCHO_EXECUTION_KIND
    ELBENCHO_FILE_LAYOUT=worker-directories
    # shellcheck disable=SC1090
    source "$definition" || return 2
    [[ "${ELBENCHO_EXECUTION_KIND:-io}" != mdtest ]] || return 1
    if [[ -n "${ELBENCHO_SWEEP_READ_FROM:-}" \
            && "${ELBENCHO_SINGLE_BIG_FILE:-0}" != 1 ]]; then
        return 0
    fi
    [[ "${ELBENCHO_FILE_LAYOUT:-worker-directories}" == shared-directory \
        && -n "${ELBENCHO_FILES_PER_NODE:-}" ]]
)

# SEGMENT_SUFFIX ("" or "-<namespace>"; see
# _kubectl_attempt_results_suffixes) places a multi-segment attempt's
# attempt-level ledger at distinct results-root paths (see
# _kubectl_segment_local_path).
_kubectl_merge_collected_results() {
    local state_dir="$1" results_dir="$2" attempt_id="$3" reason_output="$4"
    local segment_suffix="${5:-}"
    [[ "$reason_output" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    printf -v "$reason_output" '%s' LEDGER_INCONSISTENT
    [[ "$attempt_id" =~ ^[0-9a-f]{8}$ ]] || return 1
    if [[ -f "$results_dir/batch-manifest.tsv" ]]; then
        elbencho_batch_verify_manifest "$results_dir" || return 1
        cmp -s -- "$results_dir/batch-manifest.tsv" "$state_dir/batch-manifest.tsv" \
            && cmp -s -- "$results_dir/batch-sealed.sha256" "$state_dir/batch-sealed.sha256" \
            || return 1
    fi
    local manifest="$state_dir/publication-manifest.tsv" kind remote local_path bytes digest extra
    _kubectl_remove_superseded_collection_paths "$state_dir" "$results_dir" \
        "$reason_output" "$attempt_id" "$segment_suffix" \
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
        [[ -z "$segment_suffix" || "$kind" != ledger ]] \
            || local_path=$(_kubectl_segment_local_path "$segment_suffix" "$local_path") \
            || return 1
        local source="$state_dir/$remote" destination parent temporary
        [[ -f "$source" && ! -L "$source" ]] || return 1
        _kubectl_result_destination destination "$results_dir" "$local_path" 1 \
            "$reason_output" \
            || return 1
        parent=$(dirname "$destination")
        temporary="$parent/.${destination##*/}.kubectl-collect-$attempt_id.tmp"
        if [[ -e "$temporary" || -L "$temporary" ]]; then
            [[ -f "$temporary" && ! -L "$temporary" ]] || return 1
            if ! rm -f -- "$temporary"; then
                printf -v "$reason_output" '%s' LOCAL_IO
                return 1
            fi
        fi
        if [[ -e "$destination" ]]; then
            [[ -f "$destination" && ! -L "$destination" \
                && "$(_kubectl_sha256_file "$destination")" == "$digest" ]] || return 1
            continue
        fi
        printf -v "$reason_output" '%s' LOCAL_IO
        if ! (umask 077; set -o noclobber; : > "$temporary") 2>/dev/null \
                || ! cp -- "$source" "$temporary" \
                || ! mv -n -- "$temporary" "$destination" \
                || [[ -e "$temporary" ]]; then
            rm -f -- "$temporary"
            return 1
        fi
        printf -v "$reason_output" '%s' LEDGER_INCONSISTENT
    done < "$manifest"
    # Reporting and a later resume consume the ordinary result-side execution
    # ledger.  Publish terminal cells atomically after digest verification;
    # an existing SUCCESS may only be retained when it is byte-identical.
    local id source_status destination_status temporary
    for source_status in "$state_dir"/executions/[0-9][0-9][0-9][0-9].status; do
        [[ -f "$source_status" && ! -L "$source_status" ]] || continue
        id=$(basename "$source_status" .status)
        _kubectl_result_destination destination_status "$results_dir" \
            "executions/$id.status" 1 "$reason_output" || return 1
        temporary="$results_dir/executions/.${id}.status.kubectl-collect-$attempt_id.tmp"
        if [[ -e "$temporary" || -L "$temporary" ]]; then
            [[ -f "$temporary" && ! -L "$temporary" ]] || return 1
            if ! rm -f -- "$temporary"; then
                printf -v "$reason_output" '%s' LOCAL_IO
                return 1
            fi
        fi
        if [[ -e "$destination_status" ]]; then
            [[ -f "$destination_status" && ! -L "$destination_status" ]] || return 1
            if [[ "$(cat "$destination_status")" == SUCCESS ]]; then
                [[ "$(cat "$source_status")" == SUCCESS ]] || return 1
                # A prior collected SUCCESS is immutable; it belongs to a
                # different attempt and must not be rewritten by collection.
                continue
            fi
        fi
        printf -v "$reason_output" '%s' LOCAL_IO
        if ! (umask 077; set -o noclobber; : > "$temporary") 2>/dev/null \
                || ! cp -- "$source_status" "$temporary" \
                || ! mv -f -- "$temporary" "$destination_status"; then
            rm -f -- "$temporary"
            return 1
        fi
        printf -v "$reason_output" '%s' LEDGER_INCONSISTENT
    done
}

# The results-root ledger files every attempt publishes. A single-segment
# attempt publishes them under these names. Each segment of a multi-segment
# attempt publishes its own with the "-<namespace>" suffix of its namespace
# (run-ns-a.status, run-summary-ns-b.tsv, startup-error-ns-a.txt,
# coordinator-loss-ns-a.tsv), and the attempt's aggregate outcome goes to
# run.status and run-summary.tsv (see _kubectl_publish_results_aggregate).
# Every other published path is per execution or per group (disjoint between
# segments) or a byte-identical snapshot.
_kubectl_segment_local_path() {
    local suffix="$1" local_path="$2"
    if [[ -n "$suffix" \
            && "$local_path" =~ ^(run\.status|run-summary\.tsv|startup-error\.txt|coordinator-loss\.tsv)$ ]]; then
        printf '%s%s.%s\n' "${local_path%.*}" "$suffix" "${local_path##*.}"
    else
        printf '%s\n' "$local_path"
    fi
}

# Remove earlier collected artifacts that the current publication replaces:
# results of the cells it reran, every earlier attempt's run.status and
# run-summary.tsv (at their per-segment paths, plus a multi-segment attempt's
# aggregate copies), and an earlier attempt's startup-error.txt or
# coordinator-loss.tsv at a path the current publication also writes. Another
# segment of the current attempt (ATTEMPT_ID with a different SEGMENT_SUFFIX)
# never supersedes this one.
_kubectl_remove_superseded_collection_paths() {
    local state_dir="$1" results_dir="$2" reason_output="${3:-}"
    local attempt_id="${4:-}" segment_suffix="${5:-}"
    [[ -z "$reason_output" \
        || "$reason_output" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || return 1
    [[ -z "$reason_output" ]] \
        || printf -v "$reason_output" '%s' LEDGER_INCONSISTENT
    local current_manifest="$state_dir/publication-manifest.tsv"
    local kind first second _rest prior_manifest remote local_path bytes digest extra
    local prior_dir prior_ordinal prior_suffix prior_attempt
    # shellcheck disable=SC2034  # Passed by name to the helpers below.
    local -A resumed_ids=() current_digests=() replaced_paths=() replaced_digests=()
    # shellcheck disable=SC2034  # Passed by name to _kubectl_prior_results_suffix.
    local -A prior_suffixes=()
    while IFS=$'\t' read -r kind first second _rest; do
        [[ "$kind" == execution && "$first" =~ ^[0-9]{4}$ ]] || continue
        resumed_ids["$first"]=1
    done < "$current_manifest"
    while IFS=$'\t' read -r kind remote local_path bytes digest extra; do
        [[ "$kind" == result || "$kind" == ledger ]] || continue
        [[ -z "${extra:-}" && "$digest" =~ ^[0-9a-f]{64}$ ]] \
            && _kubectl_safe_relative_file_path "$local_path" || return 1
        [[ "$kind" != ledger ]] \
            || local_path=$(_kubectl_segment_local_path "$segment_suffix" "$local_path")
        current_digests["$local_path"]="$digest"
    done < "$current_manifest"
    # The current attempt's own aggregate, installed by an interrupted import.
    _kubectl_read_results_aggregate current_digests \
        "$results_dir/kubernetes/attempts/$attempt_id" || return 1
    local restore_nullglob=0
    shopt -q nullglob && restore_nullglob=1
    shopt -s nullglob
    # Every collected segment of every attempt (collected-state-s<K> for the
    # later segments of a multi-segment attempt).
    for prior_manifest in "$results_dir"/kubernetes/attempts/*/collected-state{,-s[1-9]*}/publication-manifest.tsv; do
        [[ -f "$prior_manifest" && ! -L "$prior_manifest" ]] || continue
        prior_dir="${prior_manifest%/publication-manifest.tsv}"
        prior_ordinal="${prior_dir##*/collected-state}"
        prior_ordinal="${prior_ordinal#-s}"
        prior_attempt="${prior_dir%/*}"
        prior_attempt="${prior_attempt##*/}"
        _kubectl_prior_results_suffix prior_suffix prior_suffixes "$results_dir" \
            "$prior_attempt" "${prior_ordinal:-0}" || return 1
        [[ "$prior_attempt" != "$attempt_id" || "$prior_suffix" == "$segment_suffix" ]] \
            || continue
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
                local_path=$(_kubectl_segment_local_path "$prior_suffix" "$local_path")
            elif [[ "$kind" == ledger \
                    && "$local_path" =~ ^(startup-error\.txt|coordinator-loss\.tsv)$ ]]; then
                local_path=$(_kubectl_segment_local_path "$prior_suffix" "$local_path")
                [[ ! -v current_digests["$local_path"] ]] || replace=1
            fi
            [[ "$replace" -eq 1 ]] || continue
            replaced_paths["$local_path"]=1
            replaced_digests["$local_path|$digest"]=1
        done < "$prior_manifest"
    done
    # Every earlier multi-segment attempt's aggregate run.status and summary.
    local aggregate_dir
    local -A aggregate_digests=()
    for aggregate_dir in "$results_dir"/kubernetes/attempts/*; do
        [[ "${aggregate_dir##*/}" != "$attempt_id" ]] || continue
        aggregate_digests=()
        _kubectl_read_results_aggregate aggregate_digests "$aggregate_dir" || return 1
        # shellcheck disable=SC2034  # Both are passed by name below.
        for local_path in "${!aggregate_digests[@]}"; do
            replaced_paths["$local_path"]=1
            replaced_digests["$local_path|${aggregate_digests[$local_path]}"]=1
        done
    done
    [[ "$restore_nullglob" -eq 1 ]] || shopt -u nullglob
    _kubectl_remove_replaced_paths "$results_dir" "$reason_output" replaced_paths \
        replaced_digests current_digests
}

# Set OUT to the results suffix of segment ORDINAL of PRIOR_ATTEMPT, caching
# each attempt's suffixes in CACHE_VAR as "ATTEMPT ORDINAL" keys.
_kubectl_prior_results_suffix() {
    local output_name="$1" results_dir="$3" prior_attempt="$4" ordinal="$5"
    local -n _kubectl_prior_suffix_cache="$2"
    local index
    local -a suffixes=()
    if [[ ! -v _kubectl_prior_suffix_cache["$prior_attempt 0"] ]]; then
        _kubectl_attempt_results_suffixes suffixes "$results_dir/kubernetes" \
            "$prior_attempt" || return 1
        for index in "${!suffixes[@]}"; do
            _kubectl_prior_suffix_cache["$prior_attempt $index"]="${suffixes[index]}"
        done
    fi
    [[ -v _kubectl_prior_suffix_cache["$prior_attempt $ordinal"] ]] || return 1
    printf -v "$output_name" '%s' "${_kubectl_prior_suffix_cache[$prior_attempt $ordinal]}"
}

# _kubectl_read_results_aggregate DIGESTS_VAR METADATA_DIR: add the
# "PATH<TAB>SHA256" rows of an attempt's results-aggregate.tsv, if it has one.
_kubectl_read_results_aggregate() {
    local -n _kubectl_aggregate_rows="$1"
    local record="$2/results-aggregate.tsv" path digest extra
    [[ -e "$record" || -L "$record" ]] || return 0
    [[ -f "$record" && ! -L "$record" ]] || return 1
    while IFS=$'\t' read -r path digest extra; do
        [[ "$path" =~ ^(run\.status|run-summary\.tsv)$ && "$digest" =~ ^[0-9a-f]{64}$ \
            && -z "$extra" ]] || return 1
        _kubectl_aggregate_rows["$path"]="$digest"
    done < "$record"
}

# Remove each replaced path once. Its current bytes must be those of some
# earlier publication of that path; an interrupted import may already have
# installed the current publication's bytes, which are kept. Unknown content
# remains an error.
_kubectl_remove_replaced_paths() {
    local results_dir="$1" reason_output="$2"
    local -n _kubectl_replaced_paths="$3" _kubectl_replaced_digests="$4"
    local -n _kubectl_current_digests="$5"
    local local_path destination destination_digest
    for local_path in "${!_kubectl_replaced_paths[@]}"; do
        _kubectl_result_destination destination "$results_dir" "$local_path" 0 \
            "$reason_output" || return 1
        [[ -e "$destination" ]] || continue
        [[ -f "$destination" && ! -L "$destination" ]] || return 1
        destination_digest=$(_kubectl_sha256_file "$destination") || return 1
        if [[ ! -v _kubectl_replaced_digests["$local_path|$destination_digest"] ]]; then
            [[ "${_kubectl_current_digests[$local_path]:-}" == "$destination_digest" ]] \
                || return 1
            continue
        fi
        if ! rm -f -- "$destination"; then
            [[ -z "$reason_output" ]] \
                || printf -v "$reason_output" '%s' LOCAL_IO
            return 1
        fi
    done
}

# Publish a multi-segment attempt's aggregate outcome at the results root, after
# every started segment is imported: run.status holds the outcome that
# _kubectl_collected_segments_outcome derives (SUCCESS only when every segment
# ran and succeeded; otherwise the FAILED or CANCELLED outcome of the segment
# that did not succeed, or _kubectl_abandoned_attempt_outcome when only
# abandoned segments remain), and run-summary.tsv the attempt's counts, a
# "reason worker-drift" row when drift handling stopped the attempt, and one
# "segment<TAB>K<TAB>NAMESPACE<TAB>OUTCOME" row per segment. Both are
# deterministic, so a repeated import publishes the same
# bytes. Their digests are recorded first in the attempt's
# results-aggregate.tsv, which lets a later attempt supersede them.
_kubectl_publish_results_aggregate() {
    local results_dir="$1" kubernetes_dir="$2" attempt_id="$3"
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id" terminal="" progress=""
    local staging record rows="" path digest index rc=0 pending running success failed
    local -a states=()
    _kubectl_collected_segments_outcome terminal progress states "$kubernetes_dir" \
        "$attempt_id" "${KUBECTL_SEGMENT_COUNT:-1}" || return 1
    read -r pending running success failed _ <<< "$progress"
    staging=$(mktemp -d "$metadata_dir/.results-aggregate.XXXXXXXX") || return 1
    {
        printf '%s\n' "$terminal" > "$staging/run.status"
        {
            printf 'schema\t1\nscope\tattempt\nattempt_id\t%s\nrun_status\t%s\n' \
                "$attempt_id" "$terminal"
            # Worker drift, not a user, stopped the attempt.
            ! _kubectl_attempt_stopped_by_drift "$kubernetes_dir" "$attempt_id" \
                || printf 'reason\tworker-drift\n'
            printf 'pending\t%s\nrunning\t%s\nsuccess\t%s\nfailed\t%s\nsegments\t%s\n' \
                "$pending" "$running" "$success" "$failed" "${#states[@]}"
            for index in "${!states[@]}"; do
                printf 'segment\t%s\t%s\t%s\n' "$index" \
                    "${KUBECTL_SEGMENT_NAMESPACES[index]}" "${states[index]}"
            done
        } > "$staging/run-summary.tsv"
    } || rc=1
    for path in run.status run-summary.tsv; do
        [[ "$rc" -eq 0 ]] || break
        digest=$(_kubectl_sha256_file "$staging/$path") || rc=1
        rows+="$path"$'\t'"$digest"$'\n'
    done
    record="$metadata_dir/results-aggregate.tsv"
    if [[ "$rc" -eq 0 && ( -e "$record" || -L "$record" ) ]]; then
        [[ -f "$record" && ! -L "$record" && "$(<"$record")"$'\n' == "$rows" ]] || {
            echo "Error: the recorded aggregate results differ from this collection" >&2
            rc=1
        }
    elif [[ "$rc" -eq 0 ]]; then
        printf '%s' "$rows" > "$staging/record" \
            && mv -n -- "$staging/record" "$record" && [[ ! -e "$staging/record" ]] || rc=1
    fi
    for path in run.status run-summary.tsv; do
        [[ "$rc" -eq 0 ]] || break
        _kubectl_install_aggregate_file "$results_dir" "$staging/$path" "$path" || rc=1
    done
    rm -rf -- "$staging" || rc=1
    return "$rc"
}

# Install SOURCE at the results-root PATH unless identical bytes are there.
_kubectl_install_aggregate_file() {
    local results_dir="$1" source="$2" path="$3" destination temporary
    _kubectl_result_destination destination "$results_dir" "$path" 1 || return 1
    if [[ -e "$destination" || -L "$destination" ]]; then
        [[ -f "$destination" && ! -L "$destination" \
            && "$(_kubectl_sha256_file "$destination")" == "$(_kubectl_sha256_file "$source")" ]] \
            && return 0
        printf 'Error: %s holds results that no collected attempt published\n' \
            "$destination" >&2
        return 1
    fi
    temporary="$results_dir/.${path}.kubectl-aggregate.tmp"
    rm -f -- "$temporary" && cp -- "$source" "$temporary" \
        && mv -n -- "$temporary" "$destination" && [[ ! -e "$temporary" ]] || {
            rm -f -- "$temporary"
            return 1
        }
}

kubectl_collect_attempt() {
    local results_dir="$1" kubernetes_dir="$2" lock_fd="$3" attempt_id="$4"
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id" inspector inspector_uid
    local archive_root archive staging state_dir remote_state rc=0
    local collect_job_state="" collect_job_rc=0 collect_job_uid=""
    local collect_job_kind="" collect_job_name="" collect_job_namespace=""
    local collect_expected_uid=""
    kubectl_attempt_load_metadata "$metadata_dir" || return 1
    local local_state_path="$metadata_dir/state.sh"
    local remote_state_path
    remote_state_path="$(kubectl_attempt_remote_root "$attempt_id")/state/run.status" \
        || return 1
    case "$KUBECTL_LIFECYCLE_STATE" in SUBMITTED|TERMINAL|COLLECTION_IN_PROGRESS) ;; *) return 1 ;; esac
    _kubectl_scavenge_collection_staging "$results_dir" || return 1
    if [[ "${KUBECTL_SEGMENT_COUNT:-1}" -gt 1 ]]; then
        _kubectl_collect_multi_segment_attempt "$results_dir" "$kubernetes_dir" \
            "$lock_fd" "$attempt_id"
        return
    fi
    if [[ "$KUBECTL_LIFECYCLE_STATE" == COLLECTION_IN_PROGRESS \
            && -d "$metadata_dir/collected-state" ]]; then
        local recovered_terminal=""
        _kubectl_validate_collected_publication "$metadata_dir/collected-state" "$attempt_id" \
            recovered_terminal "$metadata_dir/control-bundle/executions" \
            >/dev/null || return 1
        if kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" \
                release-pvc-lease; then
            # The Lease is the final external resource. Once its exact release
            # is journaled, recovery must not create new helpers or touch a
            # replacement Lease owned by a later attempt.
            kubectl_with_claim 0 kubectl_collection_cleanup_precedes_lease_release "$kubernetes_dir" \
                "$attempt_id" || return 1
            kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" \
                "$attempt_id" COLLECTED || return 1
            _kubectl_emit_collected_state "$recovered_terminal" "$results_dir" \
                "$attempt_id"
            [[ "$recovered_terminal" == SUCCESS ]]
            return
        fi
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
        [[ "$recovery_rc" -ne 0 ]] || _kubectl_release_attempt_leases \
            "$kubernetes_dir" "$lock_fd" "$attempt_id" || recovery_rc=1
        [[ "$recovery_rc" -eq 0 ]] || return "$recovery_rc"
        kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            COLLECTED || return 1
        _kubectl_emit_collected_state "$recovered_terminal" "$results_dir" "$attempt_id"
        [[ "$recovered_terminal" == SUCCESS ]]
        return
    fi
    _kubectl_create_inspector inspector inspector_uid collector "$kubernetes_dir" "$lock_fd" "$attempt_id" || return 1
    _kubectl_collect_segment_gate "$kubernetes_dir" "$attempt_id" "$inspector" 1 \
        || rc=$?
    if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == SUBMITTED ]]; then
        kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            TERMINAL || rc=1
        kubectl_attempt_load_metadata "$metadata_dir" || rc=1
    fi
    [[ "$rc" -ne 0 ]] || _kubectl_collect_segment_stream "$results_dir" \
        "$attempt_id" "$inspector" || rc=1
    if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == TERMINAL ]]; then
        kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" \
            COLLECTION_IN_PROGRESS || rc=1
    fi
    staging="$results_dir/.kubernetes-collect-$attempt_id.${RANDOM}"
    [[ "$rc" -ne 0 ]] || _kubectl_collect_segment_import "$results_dir" \
        "$kubernetes_dir" "$attempt_id" || rc=1
    _kubectl_collect_remove_staging "$attempt_id" || rc=1
    if [[ "$rc" -eq 0 ]]; then
        # Collection created a fresh exact helper after the upload Pod was
        # removed.  It releases the durable reservation before its own delete.
        kubectl_release_journaled_remote_attempt "$kubernetes_dir" "$lock_fd" \
            "$KUBECTL_NAMESPACE" "$inspector" "$attempt_id" || rc=1
        [[ "$rc" -ne 0 ]] || kubectl_cleanup_journaled_resources "$kubernetes_dir" \
            "$lock_fd" "$attempt_id" || rc=1
    fi
    if [[ "$rc" -ne 0 ]]; then
        kubectl_preserve_attempt_diagnostics "$kubernetes_dir" "$attempt_id" \
            collect-failed
    fi
    _kubectl_remove_inspector "$kubernetes_dir" "$lock_fd" "$inspector" \
        "$inspector_uid" "$attempt_id" || rc=1
    [[ "$rc" -ne 0 ]] || _kubectl_release_attempt_leases "$kubernetes_dir" \
        "$lock_fd" "$attempt_id" || rc=1
    [[ "$rc" -ne 0 ]] || kubectl_attempt_transition "$kubernetes_dir" "$lock_fd" "$attempt_id" COLLECTED || rc=1
    [[ "$rc" -ne 0 ]] || _kubectl_emit_collected_state "$remote_state" "$results_dir" \
        "$attempt_id"
    [[ "$remote_state" == SUCCESS ]] || rc=1
    return "$rc"
}

# Collection gate for the selected segment, through its
# INSPECTOR: the PVC outcome is terminal (recovering a lost coordinator first),
# the workers did not drift (when CHECK_ENDPOINTS is 1), and the exact Job is
# quiescent. Sets the caller's remote_state and collect_job_* evidence.
_kubectl_collect_segment_gate() {
    local kubernetes_dir="$1" attempt_id="$2" inspector="$3" check_endpoints="${4:-1}"

    local metadata_dir="$kubernetes_dir/attempts/$attempt_id" rc=0 sweep_key suffix
    sweep_key=$(_kubectl_selected_segment_key sweep) \
        && suffix=$(_kubectl_selected_segment_suffix) || return 1
    remote_state=$(kubectl_read_remote_status "$KUBECTL_NAMESPACE" "$inspector" "$attempt_id") || rc=1
    if [[ "$rc" -eq 0 \
            && "$remote_state" =~ ^(PREPARED|RUNNING|SUCCESS|FAILED|CANCELLED)$ ]]; then
        # A coordinator Pod can be killed after acquiring its durable lock and
        # either before or after publishing RUNNING. The Job's exact journaled
        # identity is the authority for deciding that no coordinator remains.
        kubectl_attempt_load_resource "$kubernetes_dir" "$attempt_id" "$sweep_key" || rc=1
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
    if [[ "$rc" -eq 0 && ! "$remote_state" =~ ^(SUCCESS|FAILED|CANCELLED)$ ]]; then
        _kubectl_report_collect_terminal_gate "$attempt_id" \
            "wait for terminal status or run --cancel, then retry --collect"
        rc=1
    fi
    if [[ "$rc" -eq 0 && "$check_endpoints" -eq 1 ]]; then
        local terminal_endpoint_rc=0
        _kubectl_verify_saved_worker_endpoints "$kubernetes_dir" "$attempt_id" \
            || terminal_endpoint_rc=$?
        if [[ "$terminal_endpoint_rc" -eq 2 ]]; then
            if kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "delete-$sweep_key" \
                    && [[ -f "$metadata_dir/endpoint-drift$suffix.tsv" \
                        && ! -L "$metadata_dir/endpoint-drift$suffix.tsv" ]]; then
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
    return "$rc"
}

_kubectl_report_collect_terminal_gate() {
    local attempt_id="$1" action="$2"
    KUBECTL_ATTEMPT_ID="$attempt_id" KUBECTL_REMOTE_STATE="$remote_state" \
        KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="$local_state_path" \
        KUBECTL_DIAGNOSTIC_REMOTE_STATE_PATH="$remote_state_path" \
        kubectl_report_lifecycle_error collect terminal-gate \
            INVALID_LIFECYCLE_OPERATION "$action" \
            yes "$collect_job_kind" "$collect_job_name" \
            "$collect_job_namespace" "$collect_expected_uid" \
            "$collect_job_uid" "" "$collect_job_state" || true
}

# Receive the selected segment's state tree through INSPECTOR into a fresh
# local archive. Sets the caller's archive_root and archive.
_kubectl_collect_segment_stream() {
    local results_dir="$1" attempt_id="$2" inspector="$3" rc=0
    local remote_apparent_bytes=""
    kubectl_remote_attempt_apparent_bytes "$KUBECTL_NAMESPACE" "$inspector" \
        "$attempt_id" remote_apparent_bytes || rc=1
    if [[ "$rc" -eq 0 ]] && ! _kubectl_require_collection_capacity \
            "$results_dir" "$remote_apparent_bytes"; then
        kubectl_report_collection_failure "$attempt_id" local-capacity ENOSPC \
            "free local result storage and retry --collect" \
            "$local_state_path" "$remote_state_path" "" \
            "$collect_job_kind" "$collect_job_name" \
            "$collect_job_namespace" "$collect_expected_uid" \
            "$collect_job_uid" "$collect_job_state"
        rc=1
    fi
    if [[ "$rc" -eq 0 ]]; then
        if archive_root=$(mktemp -d \
                "$results_dir/.kubernetes-collect-work-$attempt_id.XXXXXXXX"); then
            archive="$archive_root/attempt.tar"
        else
            kubectl_report_collection_failure "$attempt_id" archive-staging \
                LOCAL_IO "repair local result storage and retry --collect" \
                "$local_state_path" "$remote_state_path" "" \
                "$collect_job_kind" "$collect_job_name" \
                "$collect_job_namespace" "$collect_expected_uid" \
                "$collect_job_uid" "$collect_job_state"
            rc=1
        fi
    fi
    [[ "$rc" -ne 0 ]] || kubectl_stream_remote_attempt "$KUBECTL_NAMESPACE" "$inspector" \
        "$attempt_id" "$archive" "$local_state_path" "$remote_state_path" \
        "$collect_job_kind" "$collect_job_name" "$collect_job_namespace" \
        "$collect_expected_uid" "$collect_job_state" || rc=1
    return "$rc"
}

# Extract the selected segment's received archive (the caller's archive) into
# the caller's staging, verify its publication against the segment's bundled
# definitions and remote_state, merge it into the results, and install it as
# collected-state[-s<K>].
_kubectl_collect_segment_import() {
    local results_dir="$1" kubernetes_dir="$2" attempt_id="$3" rc=0 suffix results_suffix
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id"
    suffix=$(_kubectl_selected_segment_suffix) \
        && results_suffix=$(_kubectl_results_suffix "$(_kubectl_selected_segment)") || return 1
    local extract_rc=0 extract_reason=LOCAL_IO
    kubectl_extract_attempt_archive "$archive" "$attempt_id" \
        "$results_dir" "$staging" || extract_rc=$?
    if [[ "$extract_rc" -ne 0 ]]; then
        [[ "$extract_rc" -ne 65 ]] || extract_reason=ARCHIVE_INVALID
        kubectl_report_collection_failure "$attempt_id" archive-extract \
            "$extract_reason" \
            "repair local storage or inspect the retained remote attempt, then retry --collect" \
            "$local_state_path" "$remote_state_path" "" \
            "$collect_job_kind" "$collect_job_name" \
            "$collect_job_namespace" "$collect_expected_uid" \
            "$collect_job_uid" "$collect_job_state"
        return 1
    fi
    state_dir="$staging/$attempt_id/state"
    local verified_terminal=""
    if ! _kubectl_validate_collected_publication "$state_dir" \
            "$attempt_id" verified_terminal "$metadata_dir/control-bundle$suffix/executions" \
            >/dev/null; then
        kubectl_report_collection_failure "$attempt_id" publication-validate \
            LEDGER_INCONSISTENT \
            "inspect the retained PVC ledger and publication manifest before retrying" \
            "$local_state_path" "$remote_state_path" "" \
            "$collect_job_kind" "$collect_job_name" "$collect_job_namespace" \
            "$collect_expected_uid" "$collect_job_uid" "$collect_job_state"
        return 1
    fi
    if [[ "$verified_terminal" != "$remote_state" ]]; then
        kubectl_report_collection_failure "$attempt_id" outcome-validate \
            LEDGER_INCONSISTENT \
            "inspect the retained PVC ledger and local staging before retrying" \
            "$local_state_path" "$remote_state_path" "" \
            "$collect_job_kind" "$collect_job_name" "$collect_job_namespace" \
            "$collect_expected_uid" "$collect_job_uid" "$collect_job_state"
        return 1
    fi
    local merge_reason=LEDGER_INCONSISTENT
    if ! _kubectl_merge_collected_results "$state_dir" \
            "$results_dir" "$attempt_id" merge_reason "$results_suffix"; then
        local merge_action="inspect conflicting local results and the retained manifest"
        [[ "$merge_reason" != LOCAL_IO ]] \
            || merge_action="repair local result storage and retry --collect"
        kubectl_report_collection_failure "$attempt_id" result-merge "$merge_reason" \
            "$merge_action; remote results were retained" \
            "$local_state_path" "$remote_state_path" "" \
            "$collect_job_kind" "$collect_job_name" "$collect_job_namespace" \
            "$collect_expected_uid" "$collect_job_uid" "$collect_job_state"
        return 1
    fi
    local collected="$metadata_dir/collected-state$suffix"
    if { [[ ! -e "$collected" && ! -L "$collected" ]] \
            || { [[ -d "$collected" && ! -L "$collected" ]] \
                && rm -rf -- "$collected"; }; } \
            && mv -- "$state_dir" "$collected"; then
        :
    else
        kubectl_report_collection_failure "$attempt_id" collected-state-publish \
            LOCAL_IO "repair local metadata storage and retry --collect" \
            "$local_state_path" "$remote_state_path" "" \
            "$collect_job_kind" "$collect_job_name" \
            "$collect_job_namespace" "$collect_expected_uid" \
            "$collect_job_uid" "$collect_job_state"
        rc=1
    fi
    return "$rc"
}

# Remove the caller's local archive_root and staging.
_kubectl_collect_remove_staging() {
    local attempt_id="$1" rc=0
    if [[ -n "${archive_root:-}" ]] && ! rm -rf -- "$archive_root"; then
        echo "Error: failed to remove Kubernetes collection archive staging" >&2
        kubectl_report_collection_failure "$attempt_id" archive-staging-cleanup \
            LOCAL_IO "remove the named local staging path and retry --collect" \
            "$local_state_path" "$remote_state_path" "" \
            "$collect_job_kind" "$collect_job_name" \
            "$collect_job_namespace" "$collect_expected_uid" \
            "$collect_job_uid" "$collect_job_state"
        rc=1
    fi
    if [[ -n "${staging:-}" ]] && ! rm -rf -- "$staging"; then
        echo "Error: failed to remove Kubernetes collection extraction staging" >&2
        kubectl_report_collection_failure "$attempt_id" extraction-staging-cleanup \
            LOCAL_IO "remove the named local staging path and retry --collect" \
            "$local_state_path" "$remote_state_path" "" \
            "$collect_job_kind" "$collect_job_name" \
            "$collect_job_namespace" "$collect_expected_uid" \
            "$collect_job_uid" "$collect_job_state"
        rc=1
    fi
    return "$rc"
}

_kubectl_batch_failed_submission_retryable() {
    local kubernetes_dir="$1" attempt_id="$2"
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id" path key
    kubectl_attempt_load_metadata "$metadata_dir" || return 1
    [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED \
        && ! -e "$metadata_dir/predecessor-attempt" \
        && ! -L "$metadata_dir/predecessor-attempt" ]] || return 1
    local -a lease_keys=()
    kubectl_lease_keys_for_claim_count lease_keys "$KUBECTL_CLAIM_COUNT" || return 1
    for key in "${lease_keys[@]}"; do
        kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "release-$key" \
            || return 1
    done
    local index suffix
    for ((index = 0; index < KUBECTL_SEGMENT_COUNT; index++)); do
        suffix=$(kubectl_segment_suffix "$index") || return 1
        path="$metadata_dir/remote-reservation$suffix.sh"
        [[ -e "$path" || -L "$path" ]] || continue
        [[ -f "$path" && ! -L "$path" ]] || return 1
        kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" \
            "release-remote-run$suffix" || return 1
    done
    for path in "$metadata_dir/creation-intents"/*.sh; do
        [[ ! -e "$path" && ! -L "$path" ]] || return 1
    done
    for path in "$metadata_dir/resources"/*.sh; do
        [[ -e "$path" || -L "$path" ]] || continue
        [[ -f "$path" && ! -L "$path" ]] || return 1
        key=$(basename "$path" .sh)
        kubectl_resource_key_is_lease "$key" && continue
        kubectl_attempt_step_done "$kubernetes_dir" "$attempt_id" "delete-$key" || return 1
    done
    return 0
}

# Resume decisions (ID<TAB>SKIP|RUN) for a collected multi-segment attempt: a
# segment that succeeded skips its cells, another collected segment is
# interpreted by the current coordinator, and a segment that never started
# runs every cell. The new attempt plans its own segments from the cells that
# remain (kubectl_plan_segments).
_kubectl_select_multi_segment_resume() {
    local kubernetes_dir="$1" attempt_id="$2" coordinator_source="$3"
    local metadata_dir="$kubernetes_dir/attempts/$attempt_id" index suffix state_dir path
    for ((index = 0; index < KUBECTL_SEGMENT_COUNT; index++)); do
        suffix=$(kubectl_segment_suffix "$index") || return 1
        state_dir="$metadata_dir/collected-state$suffix"
        if [[ ! -e "$state_dir" && ! -L "$state_dir" ]]; then
            for path in "$metadata_dir/control-bundle$suffix/executions"/[0-9][0-9][0-9][0-9].sh; do
                [[ -f "$path" && ! -L "$path" ]] || return 1
                printf '%s\tRUN\n' "$(basename "$path" .sh)"
            done
        elif [[ "$(cat "$state_dir/run.status")" == SUCCESS ]]; then
            for path in "$state_dir"/executions/[0-9][0-9][0-9][0-9].status; do
                [[ "$(cat "$path")" == SUCCESS ]] || return 1
                printf '%s\tSKIP\n' "$(basename "$path" .status)"
            done
        else
            "$BASH" "$coordinator_source" --select-collected-resume "$state_dir" || return 1
        fi
    done
}

kubectl_resume_collected_sweep() {
    local results_dir="$1" attempt_id lock_fd state_dir coordinator_source
    local kubernetes_dir="$results_dir/kubernetes"
    kubectl_local_lock_acquire "$kubernetes_dir" lock_fd || return 1
    local rc=0
    _kubectl_load_saved_attempt "$results_dir" attempt_id || rc=$?
    if [[ "$rc" -ne 0 ]]; then
        _kubectl_no_current_attempt_error "$results_dir" "$rc" resumed
        rc=1
    fi
    [[ "$rc" -ne 0 ]] || kubectl_attempt_load_metadata "$kubernetes_dir/attempts/$attempt_id" || rc=1
    local retry_submission=0
    if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED \
        && -f "$results_dir/batch-manifest.tsv" ]]; then
        if _kubectl_batch_failed_submission_retryable "$kubernetes_dir" "$attempt_id"; then
            retry_submission=1
        else
            echo "Error: failed batch submission lacks complete rollback evidence" >&2
            rc=1
        fi
    fi
    if [[ "$rc" -eq 0 && "$KUBECTL_LIFECYCLE_STATE" != COLLECTED && "$retry_submission" -eq 0 ]]; then
        kubectl_emit_lifecycle_commands "$results_dir" || true
        KUBECTL_ATTEMPT_ID="$attempt_id" \
            KUBECTL_DIAGNOSTIC_LOCAL_STATE_PATH="$kubernetes_dir/attempts/$attempt_id/state.sh" \
            KUBECTL_DIAGNOSTIC_REMOTE_STATE_PATH="$(kubectl_attempt_remote_root "$attempt_id")/state/run.status" \
            kubectl_report_lifecycle_error resume collection-gate \
                INVALID_LIFECYCLE_OPERATION \
                "run the STORAGE_SCALE_TEST_COLLECT_COMMAND printed above before --resume" \
                unknown || true
        rc=1
    fi
    state_dir="$kubernetes_dir/attempts/$attempt_id/collected-state"
    coordinator_source="$(dirname "${BASH_SOURCE[0]}")/_nv-elbencho-kubectl-coordinator.sh"
    # Interpret the immutable collected ledger with the current trusted
    # deployment code. The attempt's bundled coordinator is an execution
    # snapshot and can contain a bug fixed after collection.
    [[ "$rc" -ne 0 ]] || [[ -f "$coordinator_source" && ! -L "$coordinator_source" ]] \
        || rc=1
    if [[ "$rc" -eq 0 ]]; then
        if [[ "$retry_submission" -eq 1 ]]; then
            local retry_id retry_status
            : > "$results_dir/executions/.kubectl-resume.tsv" || rc=1
            while IFS= read -r retry_id; do
                retry_status=$(cat "$results_dir/executions/$retry_id.status") || { rc=1; break; }
                [[ "$retry_status" == PENDING ]] || { rc=1; break; }
                printf '%s\tRUN\n' "$retry_id" >> "$results_dir/executions/.kubectl-resume.tsv" || { rc=1; break; }
            done < <(awk -F '\t' '$1 == "execution" {print $2}' "$results_dir/batch-manifest.tsv")
        elif [[ "${KUBECTL_SEGMENT_COUNT:-1}" -gt 1 ]]; then
            _kubectl_select_multi_segment_resume "$kubernetes_dir" "$attempt_id" \
                "$coordinator_source" > "$results_dir/executions/.kubectl-resume.tsv" \
                || rc=1
        else
            "$BASH" "$coordinator_source" --select-collected-resume "$state_dir" \
                > "$results_dir/executions/.kubectl-resume.tsv" || rc=1
        fi
    fi
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
    kubectl_validate_saved_control_layout || return 1
    export KUBECTL_SUBMIT_EXECUTIONS_FILE="$selection_ids"
    kubectl_submit_sweep "$results_dir" "$max_nodes" "$attempt_id"
}

readonly _STORAGE_SCALE_TEST_KUBECTL_HELPERS_LOADED=1
