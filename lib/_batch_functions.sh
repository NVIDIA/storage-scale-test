# shellcheck shell=bash disable=SC1090,SC1091,SC2034,SC2154,SC2030,SC2031
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

# This library is also shipped to the credential-free Kubernetes coordinator.
# Manifest rows, never directory enumeration, define committed membership.
# Cell preparation/validation intentionally restore globals only in their own
# subshells; later adapters restore their own context rather than those values.
# Batch preflight validates Kubernetes claims; the coordinator bundle ships this
# library without that helper and never needs it.
if ! declare -F kubectl_validate_test_dirs >/dev/null \
        && [[ -f "$(dirname "${BASH_SOURCE[0]}")/_kubectl_claims.sh" ]]; then
    # shellcheck source=lib/_kubectl_claims.sh
    source "$(dirname "${BASH_SOURCE[0]}")/_kubectl_claims.sh"
fi

elbencho_batch_is_batch() {
    local marker
    # Exact batch markers keep a damaged batch on the validating lifecycle path;
    # a missing manifest must never downgrade it to an ordinary results tree.
    for marker in batch-manifest.tsv batch-profile.tsv batch-sealed.sha256 batch-sealed-environment.sha256; do
        [[ ! -e "$1/$marker" && ! -L "$1/$marker" ]] || return 0
    done
    return 1
}

_elbencho_batch_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum < "$1" | awk '{print $1}'
    else
        shasum -a 256 < "$1" | awk '{print $1}'
    fi
}

_elbencho_batch_error() { printf 'Error: filesystem batch: %s\n' "$*" >&2; return 1; }

# _elbencho_quote_argument ARGUMENT: print ARGUMENT as one copy-paste-safe shell
# word. Plain words (including commas, so "--nodes 1,8" stays readable) are
# left bare; anything else is quoted with ${argument@Q}.
_elbencho_quote_argument() {
    if [[ "$1" =~ ^[A-Za-z0-9_./,:=@+-]+$ ]]; then
        printf '%s' "$1"
    else
        printf '%s' "${1@Q}"
    fi
}

# elbencho_reject_multi_target_run COUNT LAUNCHER [ARG...]: explain why a plain
# run cannot take several --target files, and print the equivalent batch
# commands with the operator's own arguments (LAUNCHER and ARGs are the original
# invocation). Always fails.
elbencho_reject_multi_target_run() {
    local count="$1" launcher="$2" argument quoted_launcher quoted_arguments=""
    shift 2
    quoted_launcher=$(_elbencho_quote_argument "$launcher")
    for argument in "$@"; do
        quoted_arguments+=" $(_elbencho_quote_argument "$argument")"
    done
    {
        printf 'Error: %s --target files were given, but a plain run tests exactly one target.\n' \
            "$count"
        printf 'Prepare a batch instead; it creates one group per --target, in command-line order:\n'
        printf '  %s --batch%s\n' "$quoted_launcher" "$quoted_arguments"
        printf '  %s --start <directory printed as STORAGE_SCALE_TEST_BATCH_RESULTS>\n' \
            "$quoted_launcher"
        printf 'To run a single target directly, pass only one --target.\n'
    } >&2
    return 1
}

elbencho_batch_execution_output_dir() {
    local root="$1" id="$2" relative
    if ! elbencho_batch_is_batch "$root"; then
        printf '%s\n' "$root"
        return 0
    fi
    relative=$(awk -F '\t' -v id="$id" '
        $1=="group" {groups[$2]=$4}
        $1=="execution" && $2==id {group=$3; found++}
        END {if(found==1 && group in groups) print groups[group]; else exit 1}
    ' "$root/batch-manifest.tsv") || return 1
    [[ "$relative" =~ ^groups/[0-9]{4}/(elbencho|mdtest-elbencho)-[0-9]{8}Z[0-9]{6}$ ]] || return 1
    printf '%s/%s\n' "$root" "$relative"
}

elbencho_batch_load_execution_context() {
    local root="$1" id="$2" output
    if elbencho_batch_is_batch "$root"; then
        output=$(elbencho_batch_execution_output_dir "$root" "$id") || return 1
        source "$output/env_used.sh" || return 1
    fi
    source "$root/executions/$id.sh"
}

# Full local trees require every committed definition. Resume bundles may carry
# only pending definitions, but retain the full manifest and group provenance.
elbencho_batch_verify_manifest() {
    local root="$1" mode="${2:-full}" manifest="$1/batch-manifest.tsv"
    [[ -f "$manifest" && ! -L "$manifest" ]] || {
        _elbencho_batch_error "missing or symlinked manifest: $manifest"; return 1;
    }
    [[ -d "$root/executions" && ! -L "$root/executions" ]] || {
        _elbencho_batch_error "missing or symlinked execution directory: $root/executions"; return 1;
    }
    awk -F '\t' '
        NR==2 {revision=$2}
        NR==1 {if(NF!=2 || $1!="version" || $2!="1") exit 1; next}
        NR==2 {if(NF!=2 || $1!="revision" || $2!~/^[1-9][0-9]*$/) exit 1; next}
        NR==3 {if(NF!=2 || $1!="datestamp" || $2!~/^[0-9]+Z[0-9]+$/) exit 1; next}
        $1=="group" {if(NF!=6 || $2!=sprintf("%04d",++g) || ($3!="io" && $3!="mdtest")) exit 1; groups[$2]=1; next}
        $1=="execution" {if(NF!=4 || $2!=sprintf("%04d",++e) || !($3 in groups)) exit 1; next}
        {exit 1}
        END {if(g==0 || e==0 || e>9999 || revision!=g) exit 1}
    ' "$manifest" || { _elbencho_batch_error 'invalid manifest membership'; return 1; }
    local row id kind relative sh_hash yaml_hash path hash unused datestamp basename status_path cell_state
    datestamp=$(awk -F '\t' '$1=="datestamp" {print $2}' "$manifest")
    [[ "$datestamp" =~ ^[0-9]{8}Z[0-9]{6}$ ]] || return 1
    while IFS=$'\t' read -r row id kind relative sh_hash yaml_hash; do
        case "$row" in
            group)
                basename=elbencho
                [[ "$kind" != mdtest ]] || basename=mdtest-elbencho
                [[ "$relative" == "groups/$id/$basename-$datestamp" ]] || return 1
                for unused in env_used.sh env_used.yaml; do
                    path="$root/$relative/$unused"
                    [[ -f "$path" && ! -L "$path" && ! -L "$root/groups" \
                        && ! -L "$root/groups/$id" && ! -L "$root/$relative" ]] || return 1
                    hash="$sh_hash"
                    [[ "$unused" != env_used.yaml ]] || hash="$yaml_hash"
                    [[ "$hash" =~ ^[0-9a-f]{64}$ && "$(_elbencho_batch_sha256 "$path")" == "$hash" ]] || {
                        _elbencho_batch_error "snapshot digest mismatch: $path"; return 1;
                    }
                done ;;
            execution)
                path="$root/executions/$id.sh"
                [[ "$relative" =~ ^[0-9a-f]{64}$ ]] || return 1
                [[ "$mode" != subset || -e "$path" ]] || continue
                [[ -f "$path" && ! -L "$path" && "$relative" =~ ^[0-9a-f]{64}$ \
                    && "$(_elbencho_batch_sha256 "$path")" == "$relative" ]] || {
                    _elbencho_batch_error "definition digest mismatch: $path"; return 1;
                }
                if [[ "$mode" == full ]]; then
                    status_path="$root/executions/$id.status"
                    [[ -f "$status_path" && ! -L "$status_path" ]] || {
                        _elbencho_batch_error "missing or symlinked cell status: $status_path"; return 1;
                    }
                    cell_state=$(cat "$status_path") || return 1
                    case "$cell_state" in
                        PENDING|RUNNING|SUCCESS|FAILED) ;;
                        *) _elbencho_batch_error "invalid cell status: $status_path"; return 1 ;;
                    esac
                fi ;;
        esac
    done < "$manifest"
    if [[ -e "$root/batch-sealed.sha256" || -L "$root/batch-sealed.sha256" ]]; then
        [[ ! -L "$root/batch-sealed.sha256" \
            && "$(cat "$root/batch-sealed.sha256")" == "$(_elbencho_batch_sha256 "$manifest")" ]] || {
            _elbencho_batch_error 'sealed manifest changed'; return 1;
        }
    fi
    if [[ "$mode" == full ]] && [[ -e "$root/common-env.sh" || -L "$root/common-env.sh" \
        || -e "$root/batch-profile.tsv" || -L "$root/batch-profile.tsv" ]]; then
        _elbencho_batch_verify_profile_files "$root" || return 1
    fi
}

_elbencho_batch_write_profile_files() {
    local root="$1" path
    {
        for path in common-env.sh common-fields.tsv config/SSH_HOST_LIST \
            config/SLURM_NODE_INCLUDES config/SLURM_NODE_IGNORES; do
            [[ ! -f "$root/$path" ]] || printf '%s\t%s\n' "$path" "$(_elbencho_batch_sha256 "$root/$path")"
        done
    } > "$root/batch-profile.tsv"
}

_elbencho_batch_verify_profile_files() {
    local root="$1" path digest count=0
    [[ -f "$root/batch-profile.tsv" && ! -L "$root/batch-profile.tsv" ]] || return 1
    while IFS=$'\t' read -r path digest; do
        case "$path" in
            common-env.sh|common-fields.tsv|config/SSH_HOST_LIST|config/SLURM_NODE_INCLUDES|config/SLURM_NODE_IGNORES) ;;
            *) return 1 ;;
        esac
        [[ -f "$root/$path" && ! -L "$root/$path" && ! -L "$root/config" \
            && "$digest" =~ ^[0-9a-f]{64}$ && "$(_elbencho_batch_sha256 "$root/$path")" == "$digest" ]] || {
            _elbencho_batch_error "saved execution environment changed: $path"; return 1;
        }
        [[ "$path" != common-env.sh && "$path" != common-fields.tsv ]] || count=$((count + 1))
    done < "$root/batch-profile.tsv"
    (( count == 2 )) || return 1
    if [[ -f "$root/batch-sealed.sha256" ]]; then
        [[ -f "$root/env_used.sh" && ! -L "$root/env_used.sh" \
            && -f "$root/batch-sealed-environment.sha256" && ! -L "$root/batch-sealed-environment.sha256" \
            && "$(cat "$root/batch-sealed-environment.sha256")" == "$(_elbencho_batch_sha256 "$root/env_used.sh")" ]] || {
            _elbencho_batch_error 'sealed execution environment changed'; return 1;
        }
    fi
}

# Emit declarations globally: these snapshots are sourced from functions as well
# as top-level scripts. Keep array ordering and distinguish unset from empty.
_elbencho_batch_emit_variable() {
    local name="$1" declaration
    if declaration=$(declare -p "$name" 2>/dev/null); then
        if [[ "$declaration" == 'declare -- '* ]]; then
            printf '%s\n' "${declaration/declare --/declare -g --}"
        else
            printf '%s\n' "${declaration/declare -/declare -g}"
        fi
    else
        printf 'unset %s\n' "$name"
    fi
}

_elbencho_batch_common_names() {
    printf '%s\n' EXECUTION_SUBSTRATE client_arch client_type ORDER_NODES SCALE_TEST_BASE \
        ELBENCHO
    case "$EXECUTION_SUBSTRATE" in
        ssh) printf '%s\n' SSH_USER SSH_HOMEDIR_SHARED SSH_OPTIONS SSH_OPTS ;;
        slurm) printf '%s\n' account reservation partition run_time MODULES \
            SLURM_EXTRA_ARGS SLURM_EXCLUSIVE_USER SLURM_JOB_NAME_PREFIX ;;
        # Each group's snapshot records its own roots (and so its claims and
        # namespace); the batch freezes only what is shared. The sequencer image
        # is frozen as the image it resolves to at prepare time (the default
        # when env.sh sets none), so neither a later env.sh edit nor a newer
        # default changes how a prepared batch chains its namespaces.
        kubectl) printf '%s\n' KUBECTL_NAMESPACE KUBECTL_NODE_SELECTOR \
            KUBECTL_ELBENCHO_IMAGE KUBECTL_IMAGE_PULL_POLICY KUBECTL_RUN_AS_USER \
            KUBECTL_RUN_AS_GROUP KUBECTL_SEQUENCER_IMAGE ;;
    esac
}

_elbencho_batch_list_file_names() {
    case "$EXECUTION_SUBSTRATE" in
        ssh) printf '%s\n' SSH_HOST_LIST ;;
        slurm) printf '%s\n' SLURM_NODE_INCLUDES SLURM_NODE_IGNORES ;;
    esac
}

_elbencho_batch_write_profile() {
    local destination="$1" name value
    : > "$destination" || return 1
    while IFS= read -r name; do
        value=$(_elbencho_batch_emit_variable "$name") || return 1
        case "$name" in
            ORDER_NODES) value="${ORDER_NODES_ENABLED:-0}" ;;
            SLURM_EXCLUSIVE_USER)
                local exclusive_value="${SLURM_EXCLUSIVE_USER:-0}"
                case "${exclusive_value,,}" in
                    1|yes|true) value=1 ;; *) value=0 ;;
                esac ;;
        esac
        printf '%s\t%s\n' "$name" "$value" >> "$destination" || return 1
    done < <(_elbencho_batch_common_names)
    while IFS= read -r name; do
        value=""
        if [[ -f "${!name:-}" ]]; then
            if [[ "$name" == SSH_HOST_LIST ]]; then
                value=$(parse_ssh_host_file "${!name}") || return 1
            else
                value=$(sed '/^[[:space:]]*$/d' "${!name}") || return 1
            fi
        fi
        printf '%s_CONTENT\t%q\n' "$name" "$value" >> "$destination" || return 1
    done < <(_elbencho_batch_list_file_names)
    value=unavailable
    [[ ! -f "${ELBENCHO:-}" ]] || value=$(_elbencho_batch_sha256 "$ELBENCHO") || return 1
    printf 'ELBENCHO_CONTENT\t%s\n' "$value" >> "$destination"
}

_elbencho_batch_write_common_snapshot() {
    local root="$1" name
    mkdir -p "$root/config" || return 1
    while IFS= read -r name; do
        if [[ -f "${!name:-}" ]]; then cp "${!name}" "$root/config/$name" || return 1; fi
    done < <(_elbencho_batch_list_file_names)
    {
        _elbencho_batch_common_names | while IFS= read -r name; do
            _elbencho_batch_emit_variable "$name"
        done
        for name in ORDER_NODES_ENABLED SSH_ENABLED SLURM_ENABLED KUBECTL_ENABLED \
            SSH_ALL_HOSTS SSH_HOST_COUNT SSH_NODELIST FS_ENABLED \
            SBATCH_OPTIONS SRUN_OPTIONS _SBATCH_OPTIONS_BASE _SRUN_OPTIONS_BASE \
            SLURM_EXCLUSIVE_OPT SLURM_GPUS_PER_NODE_OPT SLURM_EXCLUSIVE_USER_CPUS \
            SLURM_ORDERED_NODES SLURM_INCLUDES SLURM_EXCLUDES SLURM_INCLUDE_COUNT \
            SLURM_EXCLUDE_COUNT LOGS_DIR RESULTS_DIR \
            STORAGE_SCALE_TEST_INTEGRATION KUBECTL_INTEGRATION_FAILURE_OVERLAY; do
            _elbencho_batch_emit_variable "$name"
        done
        printf 'export ELBENCHO_BATCH_ROOT=%q\n' "$root"
        printf 'export DS=%q\n' "${root##*-}"
        while IFS= read -r name; do
            printf 'export %s=%q\n' "$name" "$root/config/$name"
        done < <(_elbencho_batch_list_file_names)
    } > "$root/common-env.sh"
}

_elbencho_batch_complete_group_snapshot() {
    local group="$1" name temporary="$1/.complete-env.sh"
    {
        for name in ELBENCHO_SWEEP_READ_FROM ELBENCHO_SWEEP_WRITE_ONLY \
            ELBENCHO_SWEEP_WRITE_NO_READ ELBENCHO_TREEFILE_CACHE_PATH \
            MDTEST_LAYOUT MDTEST_SINGLE_DIR_TARGET_FILES MDTEST_SINGLE_DIR_FILES_PER_WORKER; do
            printf 'unset %s\n' "$name"
        done
        # Each supported workload variable is restored explicitly, even when
        # unset, so an IO cell cannot inherit metadata settings accidentally.
        while IFS= read -r name; do
            case "$name" in
                ELBENCHO_*|MDTEST_*|FS_MAX_*)
                    [[ "$name" != ELBENCHO_BATCH_* && "$name" != ELBENCHO_RUN_* \
                        && "$name" != ELBENCHO_CELL_* ]] || continue
                    _elbencho_batch_emit_variable "$name" ;;
            esac
        done < <(compgen -A variable | LC_ALL=C sort)
        cat "$group/env_used.sh"
    } > "$temporary" || return 1
    mv "$temporary" "$group/env_used.sh"
}

_elbencho_batch_compare_profile() {
    local root="$1" candidate="$2" line name expected observed rc=0
    while IFS= read -r line; do
        name="${line%%$'\t'*}"
        expected="${line#*$'\t'}"
        observed=$(awk -F '\t' -v field="$name" '$1==field {sub(/^[^\t]*\t/, ""); print}' "$candidate")
        if [[ "$expected" != "$observed" ]]; then
            printf 'Error: filesystem batch frozen setting differs: %s\n' "$name" >&2
            rc=1
        fi
    done < "$root/common-fields.tsv"
    return "$rc"
}

# _elbencho_batch_normalize_kubectl_root KEY HELPERS OUT_VAR: set OUT_VAR to the
# union form of a Kubernetes TEST_DIRS key. A claim-qualified key becomes
# "namespace/claim:path" (kubectl_parse_test_dir_key), so groups that spell the
# same root differently share one union entry; HELPERS is the Kubernetes helper
# library, loaded only when the claim parser is unavailable and the key is a
# plain path.
_elbencho_batch_normalize_kubectl_root() {
    local key="$1" helpers="$2" namespace claim relative parsed
    local -n result_ref="$3"
    if declare -F kubectl_parse_test_dir_key >/dev/null; then
        parsed=$(kubectl_parse_test_dir_key "$key") || return 1
        IFS=$'\t' read -r namespace claim relative <<< "$parsed"
        result_ref="$namespace/$claim:$relative"
        return 0
    fi
    declare -F kubectl_normalize_logical_path >/dev/null || source "$helpers" || return 1
    kubectl_normalize_logical_path "$key" result_ref
}

_elbencho_batch_refresh_union() (
    local root="$1" row id kind relative unused
    local kube_helpers
    kube_helpers="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    declare -A union=()
    while IFS=$'\t' read -r row id kind relative unused; do
        [[ "$row" == group ]] || continue
        unset TEST_DIRS
        source "$root/$relative/env_used.sh" || exit 1
        local path
        for path in "${!TEST_DIRS[@]}"; do
            if [[ "$EXECUTION_SUBSTRATE" == kubectl ]]; then
                _elbencho_batch_normalize_kubectl_root "$path" "$kube_helpers" path || exit 1
            fi
            union["$path"]=1
        done
    done < "$root/batch-manifest.tsv"
    unset TEST_DIRS
    declare -gA TEST_DIRS=()
    local path
    for path in "${!union[@]}"; do TEST_DIRS["$path"]=1; done
    {
        cat "$root/common-env.sh"
        _emit_bash_test_dirs_decl
        printf 'export FS_ENABLED=1\n'
    } > "$root/.env_used.sh.new" || exit 1
    mv "$root/.env_used.sh.new" "$root/env_used.sh" || exit 1
    printf 'batch: true\nEXECUTION_SUBSTRATE: %s\n' "$EXECUTION_SUBSTRATE" > "$root/env_used.yaml"
)

_elbencho_batch_reidentify_execution() (
    source "$1/env_used.sh" || exit 1
    source "$1/executions/$2.sh" || exit 1
    local batch_suffix="-$3-e$4" batch_prefix=elbencho-sweep batch_paths batch_paths_csv
    local -a batch_generated_paths=()
    [[ "$5" != mdtest ]] || batch_prefix=mdtest-elbencho
    batch_paths=$(FS_TEST_DIR_SUFFIX_OVERRIDE="$batch_suffix" \
        generate_fs_test_directories "$batch_prefix") || exit 1
    [[ -n "$batch_paths" ]] || exit 1
    mapfile -t batch_generated_paths <<< "$batch_paths"
    batch_paths_csv=$(IFS=,; printf '%s' "${batch_generated_paths[*]}")
    # Preserve every operator-supplied path and group-local setting verbatim.
    # Only these two generated fields acquire the global execution identity.
    sed '/^export ELBENCHO_RUN_TEST_DIR_SUFFIX=/d; /^export ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV=/d' \
        "$1/executions/$2.sh" || exit 1
    printf 'export ELBENCHO_RUN_TEST_DIR_SUFFIX=%q\n' "$batch_suffix"
    printf 'export ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV=%q\n' "$batch_paths_csv"
)

# _elbencho_batch_publish_group ROOT STAGE KIND DS: publish one staged group.
_elbencho_batch_publish_group() {
    _elbencho_batch_publish_groups "$1" "$3" "$4" "$2"
}

# Append one staged group's rows to the uncommitted manifest TEMP_MANIFEST and
# write its snapshots and execution definitions. OFFSET_VAR names the caller's
# count of execution IDs already used; it is advanced past this group.
_elbencho_batch_stage_group_rows() {
    local root="$1" stage="$2" kind="$3" ds="$4" gid="$5" temp_manifest="$6"
    local -n offset_ref="$7"
    local relative target old_id id definition
    relative=$(_elbencho_batch_group_relative "$gid" "$kind" "$ds")
    target="$root/$relative"
    mkdir -p "$target" || return 1
    cp "$stage/env_used.sh" "$stage/env_used.yaml" "$target/" || return 1
    printf 'group\t%s\t%s\t%s\t%s\t%s\n' "$gid" "$kind" "$relative" \
        "$(_elbencho_batch_sha256 "$target/env_used.sh")" \
        "$(_elbencho_batch_sha256 "$target/env_used.yaml")" >> "$temp_manifest" || return 1
    while IFS= read -r old_id; do
        offset_ref=$((offset_ref + 1)); printf -v id '%04d' "$offset_ref"
        definition="$root/executions/$id.sh"
        {
            cat "$target/env_used.sh"
            printf 'export ELBENCHO_BATCH_GROUP_ID=%q\n' "$gid"
            printf 'export ELBENCHO_BATCH_OUTPUT_RELATIVE=%q\n' "$relative"
            _elbencho_batch_reidentify_execution "$stage" "$old_id" "$ds" "$id" "$kind"
        } > "$definition" || return 1
        _atomic_write_sentinel "$root/executions/$id.status" PENDING || return 1
        printf 'execution\t%s\t%s\t%s\n' "$id" "$gid" \
            "$(_elbencho_batch_sha256 "$definition")" >> "$temp_manifest" || return 1
    done < <(list_elbencho_execution_ids "$stage/executions")
}

_elbencho_batch_group_relative() {
    local gid="$1" kind="$2" ds="$3" basename=elbencho
    [[ "$kind" != mdtest ]] || basename=mdtest-elbencho
    printf 'groups/%s/%s-%s\n' "$gid" "$basename" "$ds"
}

# Print the publication keys. Every published group gets
# "GROUP_<gid>_TARGET=<effective target name>" (mirroring the GROUP_<NNNN>_TARGET
# status keys). Several groups (one per --target file) add GROUPS; GROUP names
# the first group and EXECUTIONS spans all of them.
_elbencho_batch_print_publication() {
    local root="$1" ds="$2" kind="$3" first="$4" last="$5" gid target prefix=STORAGE_SCALE_TEST_BATCH
    shift 5
    printf '%s_RESULTS=%s\n%s_GROUP=%s\n' "$prefix" "$root" "$prefix" "$1"
    [[ $# -eq 1 ]] || printf '%s_GROUPS=%s\n' "$prefix" "$(IFS=,; printf '%s' "$*")"
    for gid in "$@"; do
        target=$(_elbencho_batch_group_target "$root/$(_elbencho_batch_group_relative "$gid" "$kind" "$ds")")
        printf '%s_GROUP_%s_TARGET=%q\n' "$prefix" "$gid" "$target"
    done
    printf '%s_EXECUTIONS=%04d-%04d\n' "$prefix" "$first" "$last"
}

# _elbencho_batch_publish_groups ROOT KIND DS STAGE...: publish one staged
# group per STAGE, in order, with a single manifest commit (revision += the
# group count), so an interruption never leaves part of a target set. The
# caller holds the dispatch lock.
_elbencho_batch_publish_groups() {
    local root="$1" kind="$2" ds="$3"
    shift 3
    local -a stages=("$@") gids=()
    local manifest="$root/batch-manifest.tsv" temp_manifest="$root/.batch-manifest.new"
    local stage count total=0 offset groups revision gid index first
    offset=$(awk -F '\t' '$1=="execution" {n++} END {print n+0}' "$manifest")
    for stage in "${stages[@]}"; do
        count=$(list_elbencho_execution_ids "$stage/executions" | wc -l)
        (( count > 0 )) || { _elbencho_batch_error 'staged group has no executions'; return 1; }
        total=$((total + count))
    done
    (( offset + total <= 9999 )) || {
        _elbencho_batch_error 'append exceeds the 9999-execution limit'; return 1;
    }
    groups=$(awk -F '\t' '$1=="group" {n++} END {print n+0}' "$manifest")
    revision=$(awk -F '\t' -v added="${#stages[@]}" '$1=="revision" {print $2+added}' "$manifest")
    # Uncommitted debris is never enumerated. A retry may replace only the next
    # uncommitted groups while holding the mutation lock.
    for index in "${!stages[@]}"; do
        printf -v gid '%04d' "$((groups + index + 1))"
        gids+=("$gid")
        [[ ! -e "$root/groups/$gid" ]] || rm -r "$root/groups/$gid" || return 1
    done
    awk -F '\t' -v rev="$revision" 'BEGIN {OFS="\t"} $1=="revision" {$2=rev} {print}' \
        "$manifest" > "$temp_manifest" || return 1
    first=$((offset + 1))
    for index in "${!stages[@]}"; do
        _elbencho_batch_stage_group_rows "$root" "${stages[index]}" "$kind" "$ds" \
            "${gids[index]}" "$temp_manifest" offset || return 1
    done
    mv "$temp_manifest" "$manifest" || return 1
    # The manifest rename is the commit. From here the groups are published, so
    # a failed union refresh must not hide their identifiers: the operator
    # would retry --append and duplicate every group. --status and --start
    # rebuild the union from the manifest.
    if ! _elbencho_batch_refresh_union "$root"; then
        printf 'Warning: group(s) %s were committed to %s but its combined environment (env_used.sh) could not be refreshed; do not re-run --append for them. Run --status or --start on the batch to repair it.\n' \
            "$(IFS=,; printf '%s' "${gids[*]}")" "$root" >&2
    fi
    _elbencho_batch_print_publication "$root" "$ds" "$kind" "$first" "$offset" "${gids[@]}"
}

# Move every "--target FILE" pair out of the launcher arguments (ARGS_VAR) and
# set TARGETS_VAR to the resolved files in command-line order; each becomes one
# group. The same file twice is an error: it would publish two identical groups.
_elbencho_batch_extract_targets() {
    # shellcheck disable=SC2178  # Namerefs to the caller's arrays.
    local -n args_ref="$1" targets_ref="$2"
    local -a remaining=()
    local index=0 resolved other
    targets_ref=()
    while (( index < ${#args_ref[@]} )); do
        if [[ "${args_ref[index]}" != --target ]]; then
            remaining+=("${args_ref[index]}")
            index=$((index + 1))
            continue
        fi
        resolved=$(resolve_target_file "${args_ref[index + 1]:-}") || return 1
        for other in "${targets_ref[@]}"; do
            [[ ! "$resolved" -ef "$other" ]] || {
                _elbencho_batch_error "--target given more than once for the same file: $resolved"
                return 1
            }
        done
        targets_ref+=("$resolved")
        index=$((index + 2))
    done
    args_ref=("${remaining[@]}")
}

# Copy a target file into a group's staging directory (.target-file.sh) and
# record the operator's path beside it (.target-file-origin, NUL-terminated)
# for provenance, as the override files are staged.
_elbencho_batch_stage_target_file() {
    local stage="$1" origin="$2"
    cp -- "$origin" "$stage/.target-file.sh" || return 1
    printf '%s\0' "$origin" > "$stage/.target-file-origin"
}

# _elbencho_batch_stage_groups WORK DS LAUNCHER TARGETS_VAR STAGES_VAR ARGS_VAR:
# run the launcher once per target file, each into its own staging directory,
# and set STAGES_VAR to those directories in target order. Each launcher run
# applies exactly that group's target file and then the override files. With at
# most one target (or none: the target comes from env.sh) the launcher stages
# into WORK itself, as it always has. Override files staged once in WORK serve
# every group.
_elbencho_batch_stage_groups() {
    local work="$1" ds="$2" launcher="$3" index stage count
    # shellcheck disable=SC2178  # Namerefs to the caller's arrays.
    local -n targets_ref="$4" stages_ref="$5" launch_args_ref="$6"
    local -a group_args
    count=${#targets_ref[@]}
    stages_ref=()
    for ((index = 0; index < (count > 0 ? count : 1); index++)); do
        stage="$work"
        if (( count > 1 )); then
            stage="$work/target-$index"
            mkdir "$stage" || return 1
            if [[ -f "$work/.env-override-origins" ]]; then
                cp "$work/.env-override-origins" "$stage/" || return 1
            fi
        fi
        group_args=("${launch_args_ref[@]}")
        if (( count > 0 )); then
            _elbencho_batch_stage_target_file "$stage" "${targets_ref[index]}" || return 1
            group_args+=(--target "$stage/.target-file.sh")
        fi
        ELBENCHO_BATCH_PREPARE_DIR="$stage" ELBENCHO_BATCH_DATESTAMP="$ds" \
            "$BASH" "$launcher" "${group_args[@]}" || return 1
        stages_ref+=("$stage")
    done
}

# Write STAGE's complete group snapshot with that group's own settings in
# effect: the launcher saved the declarations that applied its target file and
# overrides (.env-override-declarations.sh), and a subshell replays them so one
# group's settings never reach another's snapshot.
_elbencho_batch_complete_staged_group() (
    local stage="$1"
    if [[ -f "$stage/.env-override-declarations.sh" ]]; then
        # Reuse the launcher's evaluated values; sourcing the original files
        # here would repeat relative edits and any other evaluation side effects.
        # shellcheck disable=SC1091
        source "$stage/.env-override-declarations.sh" || exit 1
    fi
    _elbencho_batch_complete_group_snapshot "$stage"
)

_elbencho_batch_prepare() (
    local kind="$1" operation="$2" directory="$3" launcher="$4"
    shift 4
    # RESULTS_DIR is initialized by env.sh, not by this helper's caller.
    # shellcheck disable=SC2153
    local base="${SCRIPT_DIR}/../.." root work stage ds token="" rc=0 index
    [[ -f "$base/env.sh" ]] || {
        echo "Error: env.sh not found; copy env.sh.template to env.sh and edit it" >&2
        exit 1
    }
    source "$base/env.sh" || exit 1
    source "$base/lib/_elbencho_functions.sh" || exit 1
    local -a targets=() stages=()
    if [[ "$operation" == batch ]]; then
        ds=$(date -u +%Y%m%dZ%H%M%S)
        root="$RESULTS_DIR/filesystem-batch-$ds"
        [[ ! -e "$root" ]] || { _elbencho_batch_error "results directory already exists: $root"; exit 1; }
    else
        root=$(cd "$directory" && pwd -P) || exit 1
        elbencho_batch_verify_manifest "$root" || exit 1
        ds=$(awk -F '\t' '$1=="datestamp" {print $2}' "$root/batch-manifest.tsv")
    fi
    work=$(mktemp -d "$RESULTS_DIR/.group-staging.XXXXXX") || exit 1
    trap 'rm -r "$work"' EXIT
    local -a launcher_args=("$@")
    reset_env_override_provenance
    _elbencho_batch_extract_targets launcher_args targets || exit 1
    _elbencho_batch_stage_env_override "$work" launcher_args || exit 1
    _elbencho_batch_stage_groups "$work" "$ds" "$launcher" targets stages \
        launcher_args || exit 1
    for index in "${!stages[@]}"; do
        stage="${stages[index]}"
        _elbencho_batch_validate_candidate "$stage" || exit 1
        _elbencho_batch_complete_staged_group "$stage" || exit 1
    done
    if [[ "$EXECUTION_SUBSTRATE" == kubectl ]]; then
        kubectl_resolve_sequencer_image || exit 1
    fi
    _elbencho_batch_write_profile "$work/common-fields.tsv" || exit 1
    if [[ "$operation" == batch ]]; then
        mkdir "$root" || { _elbencho_batch_error "results directory already exists: $root"; exit 1; }
        mkdir "$root/executions" || exit 1
        printf 'version\t1\nrevision\t0\ndatestamp\t%s\n' "$ds" > "$root/batch-manifest.tsv" || exit 1
        _elbencho_batch_write_profile "$root/common-fields.tsv" || exit 1
        _elbencho_batch_write_common_snapshot "$root" || exit 1
        _elbencho_batch_write_profile_files "$root" || exit 1
    fi
    _elbencho_acquire_dispatch_lock "$root/executions" batch token || exit 1
    if [[ -e "$root/batch-sealed.sha256" ]]; then
        _elbencho_batch_error "sealed batch is immutable: $root; create a new --batch"
        rc=1
    elif ! _elbencho_batch_compare_profile "$root" "$work/common-fields.tsv"; then rc=1
    else _elbencho_batch_publish_groups "$root" "$kind" "$ds" "${stages[@]}" || rc=$?
    fi
    _elbencho_release_dispatch_lock "$root/executions" "$token" || rc=1
    exit "$rc"
)

# Copy each of a group's --env-override files into its staging directory
# (.env-override-<n>.sh, numbered from 1 in command-line order) and point the
# launcher arguments at the copies. The launcher evaluates the files once, in
# order, and returns declarations for the prepare shell's complete group
# snapshot. The operator's absolute paths are recorded NUL-separated in
# .env-override-origins for provenance. The same file given twice is rejected
# here, while the originals are still comparable.
_elbencho_batch_stage_env_override() {
    local stage="$1" position index
    # shellcheck disable=SC2178  # Nameref to the caller's argument array.
    local -n args_ref="$2"
    local -a positions=() origins=() labels=()
    for position in "${!args_ref[@]}"; do
        [[ "${args_ref[$position]}" == --env-override ]] || continue
        positions+=("$position")
        origins+=("${args_ref[$((position + 1))]:-}")
    done
    [[ "${#positions[@]}" -gt 0 ]] || return 0
    resolve_env_override_chain origins labels || return 1
    : > "$stage/.env-override-origins" || return 1
    for index in "${!positions[@]}"; do
        cp -- "${origins[index]}" "$stage/.env-override-$((index + 1)).sh" || return 1
        args_ref[positions[index] + 1]="$stage/.env-override-$((index + 1)).sh"
        printf '%s\0' "${origins[index]}" >> "$stage/.env-override-origins" || return 1
    done
}

_elbencho_batch_validate_candidate() {
    local stage="$1" id coordinates count
    count=$(list_elbencho_execution_ids "$stage/executions" | wc -l)
    (( count > 0 && count <= 9999 )) || {
        _elbencho_batch_error 'candidate exceeds the 9999-execution limit'; return 1;
    }
    declare -A seen=()
    while IFS= read -r id; do
        coordinates=$(
            source "$stage/executions/$id.sh" || exit 1
            if [[ "$ELBENCHO_EXECUTION_KIND" == mdtest ]]; then
                printf 'mdtest:%s:%s' "$nodes" "$tasks_per_node"
            else
                printf 'io:%s:%s:%s:%s' "$nodes" "$io_size" "$thread_count" "$io_depth"
            fi
        ) || return 1
        [[ ! -v seen["$coordinates"] ]] || {
            _elbencho_batch_error "duplicate coordinates within group: $coordinates"; return 1;
        }
        seen["$coordinates"]=1
    done < <(list_elbencho_execution_ids "$stage/executions")
}

# Print the effective storage target recorded in a group's env_used.sh, or
# "unknown" when it records none. The snapshot is read in a subshell that clears
# the name and the sources of its default first, so one group's value never
# leaks into another's.
_elbencho_batch_group_target() {
    (
        STORAGE_TARGET_NAME=
        unset TEST_DIRS KUBECTL_NAMESPACE KUBECTL_PVC
        source "$1/env_used.sh" >/dev/null 2>&1 || exit 1
        storage_target_name_effective
    ) 2>/dev/null || printf 'unknown\n'
}

# Print GROUP_<NNNN>_KIND and GROUP_<NNNN>_TARGET for every committed group.
_elbencho_batch_print_group_targets() {
    local root="$1" row gid kind relative unused target
    while IFS=$'\t' read -r row gid kind relative unused; do
        [[ "$row" == group ]] || continue
        target=$(_elbencho_batch_group_target "$root/$relative")
        printf 'GROUP_%s_KIND=%s\nGROUP_%s_TARGET=%q\n' "$gid" "$kind" "$gid" "$target"
    done < "$root/batch-manifest.tsv"
}

_elbencho_batch_status() {
    local root="$1" batch=DRAFT state=NOT_STARTED next=START id value
    local pending=0 running=0 success=0 failed=0 active=1
    while IFS= read -r id; do
        value=$(< "$root/executions/$id.status") || return 1
        case "$value" in
            PENDING) pending=$((pending + 1)) ;;
            RUNNING) running=$((running + 1)) ;;
            SUCCESS) success=$((success + 1)) ;;
            FAILED) failed=$((failed + 1)) ;;
            *) _elbencho_batch_error "invalid execution status: $id"; return 1 ;;
        esac
    done < <(awk -F '\t' '$1=="execution" {print $2}' "$root/batch-manifest.tsv")
    if [[ -e "$root/batch-sealed.sha256" ]]; then
        batch=SEALED state=STOPPED next=RESUME
        if [[ -d "$root/executions/.dispatch.lock" ]]; then
            _elbencho_dispatch_lock_is_active "$root/executions" && active=0 || active=$?
        fi
        if (( active == 0 )); then
            state=RUNNING next=WAIT
        elif (( active == 2 )); then
            state=UNKNOWN next=INSPECT
        elif (( pending + running + failed == 0 )); then
            state=SUCCESS next=NONE
        elif (( failed > 0 )); then
            state=FAILED
        fi
    fi
    printf 'BATCH=%s\nSTATE=%s\nEXECUTION_SCOPE=BATCH\nPROGRESS_SOURCE=LOCAL\n' "$batch" "$state"
    printf 'EXECUTIONS_TOTAL=%s\nEXECUTIONS_PENDING=%s\nEXECUTIONS_RUNNING=%s\n' \
        "$((pending + running + success + failed))" "$pending" "$running"
    printf 'EXECUTIONS_SUCCEEDED=%s\nEXECUTIONS_FAILED=%s\n' "$success" "$failed"
    printf 'RESULT_COLLECTION=NOT_REQUIRED\nNEXT_ACTION=%s\n' "$next"
    _elbencho_batch_print_group_targets "$root"
}

_elbencho_batch_preflight() {
    local root="$1" maximum
    maximum=$(max_nodes_remaining_executions "$root/executions") || return 1
    _elbencho_batch_validate_cells "$root" || return 1
    if [[ "$EXECUTION_SUBSTRATE" == slurm ]]; then
        local module_name
        for module_name in "${MODULES[@]}"; do
            if module_exists "$module_name"; then module load "$module_name" || return 1; fi
        done
    fi
    case "$EXECUTION_SUBSTRATE" in
        ssh) (( maximum <= SSH_HOST_COUNT )) || {
            _elbencho_batch_error "requires $maximum nodes, saved SSH pool contains $SSH_HOST_COUNT"; return 1;
        } ;;
        slurm)
            command -v sbatch >/dev/null || { _elbencho_batch_error 'sbatch is unavailable'; return 1; }
            [[ "${SLURM_INCLUDE_COUNT:-0}" == 0 ]] || (( maximum <= SLURM_INCLUDE_COUNT )) || {
                _elbencho_batch_error "requires $maximum nodes, saved Slurm include pool contains $SLURM_INCLUDE_COUNT"; return 1;
            } ;;
        kubectl)
            source "$SCALE_TEST_BASE/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh" || return 1
            declare -F kubectl_batch_preflight >/dev/null || {
                _elbencho_batch_error 'Kubernetes batch preflight (kubectl_batch_preflight) is unavailable'
                return 1
            }
            # Claim identity, runtime settings, control root, workload paths
            # and node discovery for the saved batch.
            kubectl_batch_preflight "$root" || return 1 ;;
        *) _elbencho_batch_error 'invalid saved execution substrate'; return 1 ;;
    esac
    _elbencho_batch_validate_executable "$root"
}

_elbencho_batch_validate_executable() {
    local root="$1" saved
    [[ "$EXECUTION_SUBSTRATE" != kubectl ]] || return 0
    [[ -f "$ELBENCHO" && -x "$ELBENCHO" ]] || {
        _elbencho_batch_error "saved executable is missing or not executable: $ELBENCHO"; return 1;
    }
    saved=$(awk -F '\t' '$1=="ELBENCHO_CONTENT" {print $2}' "$root/common-fields.tsv")
    [[ "$saved" == "$(_elbencho_batch_sha256 "$ELBENCHO")" ]] || {
        _elbencho_batch_error "saved executable content changed: $ELBENCHO; prepare a new batch"; return 1;
    }
}

_elbencho_batch_validate_cell() (
    local root="$1" id="$2"
    elbencho_batch_load_execution_context "$root" "$id" || exit 1
    [[ "$nodes" =~ ^[1-9][0-9]*$ && ${#TEST_DIRS[@]} -gt 0 ]] || exit 1
    local path
    for path in "${!TEST_DIRS[@]}"; do
        [[ -n "$path" && "${TEST_DIRS[$path]}" =~ ^[1-9][0-9]*$ ]] || exit 1
    done
    if [[ "$ELBENCHO_EXECUTION_KIND" == mdtest ]]; then
        for path in "$tasks_per_node" "$MDTEST_BRANCH_FACTOR" "$MDTEST_ITEMS_PER_DIR" "$MDTEST_ITERATIONS"; do
            [[ "$path" =~ ^[1-9][0-9]*$ ]] || exit 1
        done
    else
        validate_elbencho_file_workload_env || exit 1
        validate_elbencho_single_big_file_env "${ELBENCHO_SWEEP_READ_FROM:-}" || exit 1
        validate_elbencho_sweep_workload_mode "$dio_or_bio" "$use_random" \
            "${ELBENCHO_SWEEP_READ_FROM:-}" "$run_to_completion" || exit 1
        [[ -z "${ELBENCHO_SWEEP_READ_FROM:-}" ]] || validate_elbencho_sweep_read_from_path "$ELBENCHO_SWEEP_READ_FROM" || exit 1
    fi
)

_elbencho_batch_validate_cells() {
    local root="$1" id
    while IFS= read -r id; do
        [[ "$(cat "$root/executions/$id.status")" != SUCCESS ]] || continue
        _elbencho_batch_validate_cell "$root" "$id" || {
            _elbencho_batch_error "invalid saved workload in execution $id"; return 1;
        }
    done < <(list_elbencho_execution_ids "$root/executions")
}

_elbencho_batch_run() (
    local operation="$1" directory="$2" base="${SCRIPT_DIR}/../.." root token="" digest rc=0
    root=$(cd "$directory" && pwd -P) || exit 1
    source "$base/lib/env_functions.sh" || exit 1
    source "$base/lib/_elbencho_functions.sh" || exit 1
    elbencho_batch_verify_manifest "$root" || exit 1
    if [[ ! -e "$root/batch-sealed.sha256" ]]; then
        # The manifest is authoritative even if the client died before writing
        # its derived union snapshot after an append's commit rename.
        _elbencho_acquire_dispatch_lock "$root/executions" batch token || exit 1
        # Another starter can seal between the draft check and lock acquisition.
        # Never regenerate its now-immutable snapshot under a stale draft check.
        if [[ ! -e "$root/batch-sealed.sha256" && ! -L "$root/batch-sealed.sha256" ]]; then
            _elbencho_batch_refresh_union "$root" || rc=1
        else
            elbencho_batch_verify_manifest "$root" || rc=1
        fi
        _elbencho_release_dispatch_lock "$root/executions" "$token" || rc=1
        (( rc == 0 )) || exit "$rc"
    fi
    source "$root/env_used.sh" || exit 1
    SCALE_TEST_BASE=$(cd "$base" && pwd -P) || exit 1
    export SCALE_TEST_BASE OUTPUT_DIR="$root" DS="${root##*-}"
    if [[ "$operation" == status ]]; then
        if [[ -e "$root/batch-sealed.sha256" && "$EXECUTION_SUBSTRATE" == kubectl \
            && -f "$root/kubernetes/current-attempt" ]]; then
            source "$base/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh" || exit 1
            kubectl_lifecycle_operation status "$root"
        else
            _elbencho_batch_status "$root"
        fi
        exit $?
    fi
    if [[ "$operation" == cancel || "$operation" == collect ]]; then
        [[ -e "$root/batch-sealed.sha256" && "$EXECUTION_SUBSTRATE" == kubectl ]] || {
            _elbencho_batch_error "$operation requires a submitted Kubernetes batch"; exit 1;
        }
        source "$base/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh" || exit 1
        kubectl_lifecycle_operation "$operation" "$root"; exit $?
    fi
    if [[ "$operation" == start && -e "$root/batch-sealed.sha256" ]]; then
        _elbencho_batch_error "batch already started; use --resume $root"; exit 1
    fi
    _elbencho_batch_validate_executable "$root" || exit 1
    if [[ -e "$root/batch-sealed.sha256" ]] \
        && (( $(count_elbencho_remaining_executions "$root/executions") > 0 )); then
        _elbencho_batch_preflight "$root" || exit 1
    fi
    if [[ ! -e "$root/batch-sealed.sha256" ]]; then
        # Revalidate if an append committed during non-mutating discovery.
        while :; do
            _elbencho_acquire_dispatch_lock "$root/executions" batch token || exit 1
            if [[ -e "$root/batch-sealed.sha256" || -L "$root/batch-sealed.sha256" ]]; then
                _elbencho_release_dispatch_lock "$root/executions" "$token" || exit 1
                _elbencho_batch_error 'another invocation already sealed this batch'; exit 1
            fi
            digest=$(_elbencho_batch_sha256 "$root/batch-manifest.tsv") || exit 1
            _elbencho_batch_refresh_union "$root" || exit 1
            source "$root/env_used.sh" || exit 1
            _elbencho_release_dispatch_lock "$root/executions" "$token" || exit 1
            _elbencho_batch_preflight "$root" || exit 1
            _elbencho_acquire_dispatch_lock "$root/executions" batch token || exit 1
            if [[ -e "$root/batch-sealed.sha256" ]]; then
                _elbencho_release_dispatch_lock "$root/executions" "$token" || exit 1
                _elbencho_batch_error 'another invocation already sealed this batch'; exit 1
            fi
            if [[ "$digest" != "$(_elbencho_batch_sha256 "$root/batch-manifest.tsv")" ]]; then
                _elbencho_release_dispatch_lock "$root/executions" "$token" || exit 1
                continue
            fi
            if [[ "$EXECUTION_SUBSTRATE" == kubectl ]]; then
                local logical_root test_root
                kubectl_select_control_root logical_root test_root || exit 1
                kubectl_set_control_layout "$logical_root" "$test_root" || exit 1
                {
                    printf 'export KUBECTL_CONTROL_LOGICAL_ROOT=%q\n' "$KUBECTL_CONTROL_LOGICAL_ROOT"
                    printf 'export KUBECTL_CONTROL_TEST_ROOT=%q\n' "$KUBECTL_CONTROL_TEST_ROOT"
                    printf 'export KUBECTL_CONTROL_ROOT=%q\n' "$KUBECTL_CONTROL_ROOT"
                } >> "$root/env_used.sh" || exit 1
            fi
            _atomic_write_sentinel "$root/batch-sealed-environment.sha256" \
                "$(_elbencho_batch_sha256 "$root/env_used.sh")" || rc=1
            [[ "$rc" != 0 ]] || _atomic_write_sentinel "$root/batch-sealed.sha256" "$digest" || rc=1
            _elbencho_release_dispatch_lock "$root/executions" "$token" || rc=1
            (( rc == 0 )) || exit "$rc"
            break
        done
    fi
    cd "$SCALE_TEST_BASE/storage-tests/fs" || exit 1
    case "$EXECUTION_SUBSTRATE" in
        ssh) dispatch_ssh_executions "$root" ;;
        slurm) dispatch_slurm_executions "$root" ;;
        kubectl)
            source "$base/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh" || exit 1
            if [[ -f "$root/kubernetes/current-attempt" ]]; then
                kubectl_resume_collected_sweep "$root"
            else
                kubectl_submit_sweep "$root" "$(max_nodes_remaining_executions "$root/executions")"
            fi ;;
    esac
)

_elbencho_batch_check_operation() {
    local current="$1" requested="$2"
    [[ -n "$current" ]] || return 0
    if [[ "--$current" == "$requested" ]]; then
        _elbencho_batch_error "$requested may be specified only once"
    else
        _elbencho_batch_error "--$current and $requested are mutually exclusive; choose one operation"
    fi
}

# Route batch operations before ordinary CLI parsing and, critically, before
# reading env.sh. Return 2 only when the ordinary launcher should continue.
elbencho_batch_cli_route() {
    local kind="$1" launcher="$2"
    shift 2
    [[ -z "${ELBENCHO_BATCH_PREPARE_DIR:-}" ]] || return 2
    local operation="" directory="" argument
    local -a workload=()
    while [[ $# -gt 0 ]]; do
        argument="$1"
        case "$argument" in
            -h|--help) return 2 ;;  # The launcher prints its usage.
            --batch)
                _elbencho_batch_check_operation "$operation" "$argument" || return 1
                if [[ $# -gt 1 && -n "$2" && "$2" != -* ]]; then
                    local append_path
                    printf -v append_path '%q' "$2"
                    _elbencho_batch_error "--batch creates a new batch and takes no directory; use --append $append_path to add executions"
                    return 1
                fi
                operation="batch"; shift ;;
            --append|--start|--resume|--status|--cancel|--collect)
                _elbencho_batch_check_operation "$operation" "$argument" || return 1
                [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || {
                    _elbencho_batch_error "$argument requires a results directory: $argument RESULTS_DIR"; return 1;
                }
                operation="${argument#--}"; directory="$2"; shift 2 ;;
            *) workload+=("$1"); shift ;;
        esac
    done
    if [[ "$operation" != batch && "$operation" != append && "$operation" != start ]]; then
        [[ -n "$directory" ]] && elbencho_batch_is_batch "$directory" || return 2
    fi
    case "$operation" in
        batch|append)
            if [[ "$operation" == append ]]; then
                elbencho_batch_is_batch "$directory" || {
                    _elbencho_batch_error "--append requires a prepared batch: $directory; create one with --batch"; return 1;
                }
                [[ ! -e "$directory/batch-sealed.sha256" ]] || {
                    _elbencho_batch_error "sealed batch is immutable: $directory; create a new --batch"; return 1;
                }
            fi
            for argument in "${workload[@]}"; do
                [[ "$argument" != --delete-only ]] || {
                    _elbencho_batch_error "--delete-only cannot be combined with --$operation; run it separately"; return 1;
                }
            done
            _elbencho_batch_prepare "$kind" "$operation" "$directory" "$launcher" "${workload[@]}" ;;
        *)
            [[ ${#workload[@]} -eq 0 ]] || {
                _elbencho_batch_error "--$operation rejects workload arguments (${workload[0]}); set them with --batch or --append"; return 1;
            }
            elbencho_batch_is_batch "$directory" || {
                _elbencho_batch_error "--$operation requires a prepared batch: $directory; create one with --batch"; return 1;
            }
            _elbencho_batch_run "$operation" "$directory" ;;
    esac
}
