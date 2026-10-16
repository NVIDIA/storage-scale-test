# shellcheck shell=bash
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

# Kubernetes claim-qualified TEST_DIRS keys.
#
# On Kubernetes every TEST_DIRS key names the PersistentVolumeClaim its root
# lives on: "[NAMESPACE/]CLAIM:PATH". NAMESPACE defaults to KUBECTL_NAMESPACE
# and PATH is relative to the claim. The roots of one TEST_DIRS map (one
# target) must share a namespace, because a Pod mounts claims of its own
# namespace only; several roots, on one claim or several, make one aggregate
# target whose load is split by weight. KUBECTL_PVC and KUBECTL_PV were
# removed in v2.0.0.
#
# lib/env_functions.sh sources this file, so it is available wherever env.sh
# is. The parser itself lives in the Kubernetes lifecycle helpers, which the
# credential-free coordinator also runs; they are loaded on first use, so SSH
# and Slurm deployments never need them.

_kubectl_claims_load_helpers() {
    declare -F kubectl_split_test_dir_key >/dev/null && return 0
    local helpers
    helpers="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh" \
        || return 1
    if [[ ! -f "$helpers" ]]; then
        printf 'Error: Kubernetes lifecycle helpers are missing from this deployment: %s\n' \
            "$helpers" >&2
        return 1
    fi
    # shellcheck disable=SC1090  # Checked-in Kubernetes lifecycle helpers.
    source "$helpers"
}

# kubectl_resolve_sequencer_image: export KUBECTL_SEQUENCER_IMAGE as the image
# the sequencer resolves to now (the default when it is empty or unset), so a
# prepared batch freezes that image rather than an empty value.
kubectl_resolve_sequencer_image() {
    local image
    _kubectl_claims_load_helpers || return 1
    image=$(kubectl_sequencer_image) || return 1
    export KUBECTL_SEQUENCER_IMAGE="$image"
}

# kubectl_parse_test_dir_key KEY: print "NAMESPACE<TAB>CLAIM<TAB>PATH" for one
# claim-qualified key, with the namespace resolved (KUBECTL_NAMESPACE when the
# key names none) and PATH normalized (no leading, trailing or repeated "/").
# Prints the problem on stderr and returns 1 for an invalid key.
kubectl_parse_test_dir_key() {
    local namespace claim path
    _kubectl_claims_load_helpers || return 1
    kubectl_split_test_dir_key "$1" namespace claim path || return 1
    printf '%s\t%s\t%s\n' "$namespace" "$claim" "$path"
}

# Print a TEST_DIRS key or value as a double-quoted shell word.
_kubectl_claims_quote() {
    local value="$1"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//\$/\\\$}"
    value="${value//\`/\\\`}"
    printf '"%s"' "$value"
}

# _kubectl_claims_replacement CLAIM: the TEST_DIRS line that names CLAIM in
# every key that names no claim yet, keeping weights and claim-qualified keys.
_kubectl_claims_replacement() {
    local claim="$1" key new_key path line="TEST_DIRS=(" sep=""
    local -a keys=()
    mapfile -t keys < <(printf '%s\n' "${!TEST_DIRS[@]}" | LC_ALL=C sort)
    for key in "${keys[@]}"; do
        new_key="$key"
        if [[ "$key" != *:* ]]; then
            path="$key"
            while [[ "$path" == /* ]]; do path="${path#/}"; done
            new_key="$claim:${path:-data}"
        fi
        line+="${sep}[$(_kubectl_claims_quote "$new_key")]=${TEST_DIRS[$key]}"
        sep=" "
    done
    printf '%s)\n' "$line"
}

# Report KUBECTL_PVC/KUBECTL_PV (removed in v2.0.0) and keys without a claim.
_kubectl_claims_check_migration() {
    local key rc=0 claim="${KUBECTL_PVC:-}"
    local -a unqualified=()
    for key in "${!TEST_DIRS[@]}"; do
        [[ "$key" == *:* ]] || unqualified+=("$key")
    done
    if [[ -n "${KUBECTL_PVC:-}" || -n "${KUBECTL_PV:-}" ]]; then
        printf 'Error: KUBECTL_PVC and KUBECTL_PV were removed in storage-scale-test v2.0.0. Remove them from env.sh and name the claim in each TEST_DIRS key ([namespace/]claim:path); the PersistentVolume each claim is bound to is found and recorded automatically.\n' >&2
        if [[ -n "$claim" ]]; then
            printf '  Replace TEST_DIRS with:\n    %s\n' \
                "$(_kubectl_claims_replacement "$claim")" >&2
        fi
        rc=1
    fi
    if (( ${#unqualified[@]} > 0 )) && [[ -z "$claim" ]]; then
        printf 'Error: Kubernetes TEST_DIRS keys must name their claim as [namespace/]claim:path; these do not: %s\n' \
            "${unqualified[*]}" >&2
        printf '  For example, with your claim'\''s name for <claim>:\n    %s\n' \
            "$(_kubectl_claims_replacement '<claim>')" >&2
        rc=1
    fi
    return "$rc"
}

# kubectl_validate_test_dirs: check the current TEST_DIRS for Kubernetes and
# report every problem, not just the first: KUBECTL_PVC or KUBECTL_PV still
# set, keys without a claim (each error prints the replacement line), invalid
# namespaces, claims or paths, a namespace that is empty, keys that resolve to
# the same root, roots that span namespaces, and more claims than one attempt
# can mount (KUBECTL_MAX_CLAIMS). Returns 1 on any problem.
kubectl_validate_test_dirs() {
    local rc=0 key namespace claim path normalized
    _kubectl_claims_load_helpers || return 1
    if ! declare -p TEST_DIRS >/dev/null 2>&1 || (( ${#TEST_DIRS[@]} == 0 )); then
        echo 'Error: TEST_DIRS must name at least one root, e.g. TEST_DIRS=(["scale-test:data"]=1)' >&2
        return 1
    fi
    _kubectl_claims_check_migration || rc=1
    local -A namespaces=() normalized_roots=() claims=()
    for key in "${!TEST_DIRS[@]}"; do
        [[ "$key" == *:* ]] || continue
        if ! kubectl_split_test_dir_key "$key" namespace claim path; then
            rc=1
            continue
        fi
        normalized="$namespace/$claim:$path"
        if [[ -v normalized_roots["$normalized"] ]]; then
            printf 'Error: TEST_DIRS keys %q and %q are the same Kubernetes root %s\n' \
                "${normalized_roots[$normalized]}" "$key" "$normalized" >&2
            rc=1
        fi
        normalized_roots["$normalized"]="$key"
        namespaces["$namespace"]=1
        claims["$namespace/$claim"]=1
    done
    _kubectl_check_attempt_claim_count TEST_DIRS "${#claims[@]}" || rc=1
    if (( ${#namespaces[@]} > 1 )); then
        printf 'Error: TEST_DIRS roots span Kubernetes namespaces (%s), but the roots of one target must share a namespace: a Pod mounts claims of its own namespace only. Test each namespace as its own target: put its roots in a target file and pass each file with --target.\n' \
            "$(printf '%s\n' "${!namespaces[@]}" | LC_ALL=C sort | paste -sd, -)" >&2
        rc=1
    fi
    return "$rc"
}

# kubectl_test_dirs_namespace: print the one namespace of the current
# TEST_DIRS roots.
kubectl_test_dirs_namespace() {
    local namespace
    local -a claims=()
    _kubectl_claims_load_helpers || return 1
    kubectl_test_dirs_segment namespace claims || return 1
    printf '%s\n' "$namespace"
}

# kubectl_test_dirs_claims: print the sorted, unique claims of the current
# TEST_DIRS roots, one per line.
kubectl_test_dirs_claims() {
    local namespace
    local -a claims=()
    _kubectl_claims_load_helpers || return 1
    kubectl_test_dirs_segment namespace claims || return 1
    printf '%s\n' "${claims[@]}"
}

# Print the IDs of ROOT's batch executions that have not succeeded, in ledger
# order: the executions the next attempt submits.
_kubectl_batch_remaining_execution_ids() {
    local root="$1" kind id rest status
    while IFS=$'\t' read -r kind id rest; do
        [[ "$kind" == execution ]] || continue
        [[ "$id" =~ ^[0-9]{4}$ ]] || return 1
        status=$(cat -- "$root/executions/$id.status") || return 1
        [[ "$status" == SUCCESS ]] || printf '%s\n' "$id"
    done < "$root/batch-manifest.tsv"
}

# The claims of one attempt (every segment's together) must fit its identity.
_kubectl_check_attempt_claim_count() {
    local context="$1" count="$2"
    (( count <= KUBECTL_MAX_CLAIMS )) && return 0
    printf 'Error: %s uses %s Kubernetes claims, but one attempt mounts at most %s; split the claims across batches\n' \
        "$context" "$count" "$KUBECTL_MAX_CLAIMS" >&2
    return 1
}

# kubectl_batch_preflight ROOT: before a prepared batch starts (and before a
# sealed batch resumes), with the batch's saved environment loaded, check
# everything Kubernetes needs: the runtime settings; every group's claim-
# qualified roots, one namespace per group, and the Pod paths of every
# execution (kubectl_plan_segments); the batch's canonical control root
# (unchanged since sealing); and, for the segments the next attempt submits
# (the namespaces of the executions that have not succeeded, as a resume
# selects them), at most KUBECTL_MAX_CLAIMS claims, Lease permission and every
# claim's identity (Bound, ReadWriteMany, Filesystem) per namespace, the
# sequencer image when they span namespaces, and enough Ready nodes for the
# largest remaining execution. Every failing namespace is named.
kubectl_batch_preflight() {
    local root="$1" maximum plan rc=0 index logical_root test_root
    _kubectl_claims_load_helpers || return 1
    maximum=$(max_nodes_remaining_executions "$root/executions") || return 1
    kubectl_validate_runtime_configuration || return 1
    kubectl_select_control_root logical_root test_root || return 1
    if [[ -e "$root/batch-sealed.sha256" ]]; then
        kubectl_validate_saved_control_layout || return 1
        [[ "$logical_root" == "$KUBECTL_CONTROL_LOGICAL_ROOT" \
            && "$test_root" == "$KUBECTL_CONTROL_TEST_ROOT" ]] || {
            echo 'Error: filesystem batch: canonical control root differs from its saved union' >&2
            return 1
        }
    fi
    kubectl_set_control_layout "$logical_root" "$test_root" || return 1
    plan=$(mktemp -d "$root/.preflight.XXXXXX") || return 1
    # shellcheck disable=SC2034  # control_roots is filled and read by name.
    local -a namespaces=() control_roots=() claims=() segment_claims=()
    local claim_count=0
    mkdir "$plan/plan" || rc=1
    [[ "$rc" -ne 0 ]] || _kubectl_batch_remaining_execution_ids "$root" \
        > "$plan/remaining" || rc=1
    if [[ "$rc" -eq 0 ]] && ! (export KUBECTL_SUBMIT_EXECUTIONS_FILE="$plan/remaining"
            kubectl_plan_segments "$root" "$plan/plan"); then
        echo 'Error: filesystem batch: the groups'\'' Kubernetes roots or execution paths are invalid' >&2
        rc=1
    fi
    [[ "$rc" -ne 0 ]] || _kubectl_read_segment_plan "$plan/plan" namespaces \
        control_roots claims || rc=1
    [[ "$rc" -ne 0 ]] || _kubectl_check_saved_control_root namespaces control_roots \
        || rc=1
    if [[ "$rc" -eq 0 ]]; then
        for index in "${!namespaces[@]}"; do
            read -ra segment_claims <<< "${claims[index]}"
            claim_count=$((claim_count + ${#segment_claims[@]}))
        done
        _kubectl_check_attempt_claim_count 'filesystem batch: the next attempt' \
            "$claim_count" || rc=1
    fi
    if [[ "$rc" -eq 0 ]]; then
        for index in "${!namespaces[@]}"; do
            read -ra segment_claims <<< "${claims[index]}"
            kubectl_validate_cluster_identity "${namespaces[index]}" \
                "${segment_claims[@]}" >/dev/null && continue
            printf 'Error: filesystem batch: Kubernetes namespace %s (claims %s) failed validation\n' \
                "${namespaces[index]}" "${claims[index]// /, }" >&2
            rc=1
        done
        if (( ${#namespaces[@]} > 1 )); then
            kubectl_validate_sequencer_configuration "${#namespaces[@]}" || rc=1
        fi
    fi
    if [[ "$rc" -eq 0 ]]; then
        kubectl_discover_candidate_nodes "$KUBECTL_NODE_SELECTOR" "$plan/nodes.tsv" || rc=1
        if [[ "$rc" -eq 0 ]] && (( $(wc -l < "$plan/nodes.tsv") < maximum )); then
            echo "Error: filesystem batch: worker selector cannot satisfy $maximum nodes" >&2
            rc=1
        fi
    fi
    rm -rf -- "$plan" || rc=1
    return "$rc"
}
