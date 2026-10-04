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

if (( BASH_VERSINFO[0] < 4 \
        || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 3) )); then
    printf 'Bash 4.3 or newer is required; on macOS run: brew install bash\n' >&2
    exit 1
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) || exit 1
readonly SCRIPT_DIR
readonly INVOKING_EXECUTION_SUBSTRATE="${EXECUTION_SUBSTRATE:-}"

if [[ -f "$SCRIPT_DIR/../../lib/_batch_functions.sh" ]]; then
    # The independent batch library restores globals only in isolated contexts.
    # shellcheck source=/dev/null
    source "$SCRIPT_DIR/../../lib/_batch_functions.sh" || exit 1
    batch_route_rc=0
    elbencho_batch_cli_route mdtest "$0" "$@" || batch_route_rc=$?
    [[ "$batch_route_rc" == 2 ]] || exit "$batch_route_rc"
fi

usage() {
    cat <<USAGE
Usage: $0 --nodes <node_spec> --tasks <task_spec> [--single-dir-file-target <count>]
          [--env-override <file>]
       $0 --resume <results_dir>
       $0 --status <results_dir>
       $0 --cancel <results_dir>
       $0 --collect <results_dir>
       $0 --batch [--env-override <file>] --nodes <node_spec> --tasks <task_spec>
       $0 --append <batch_dir> [--env-override <file>] --nodes <node_spec> --tasks <task_spec>
       $0 --start <batch_dir>

--batch prepares the first group without executing. --append adds a group only
before first start. Either filesystem launcher can --start or --resume the
mixed batch; starting permanently freezes its execution set and saved settings.
--status reports scoped execution progress, collection readiness, and next action.

Sweep node counts and tasks per node using Elbencho metadata create, stat,
and delete phases. Specifications accept comma-separated counts, inclusive
ranges X-Y, and stepped ranges X-Y+Z. Results are saved beneath
\${RESULTS_DIR}/mdtest-elbencho-<datestamp>/.

--single-dir-file-target selects one flat directory of approximately <count>
zero-byte files. It requires one node count, one task count, and one generated
target directory. The achievable file count is rounded to whole workers.

--env-override sources <file> after env.sh so its filesystem workload settings
(TEST_DIRS, FS_MAX_*, MDTEST_*, ELBENCHO_*) take precedence. It is accepted
once, only for a new submission or --batch/--append. Values are saved in
env_used.sh; later lifecycle operations never reread the file or env.sh.

--resume restores the saved execution definitions and retries non-SUCCESS cells.
For Kubernetes, first collect the terminal attempt. --status, --cancel, and
--collect manage the asynchronous Kubernetes attempt.
USAGE
}

nodes_spec=""
tasks_spec=""
single_dir_target_files=""
env_override_file=""
operation="submit"
operation_dir=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --nodes|--tasks|--single-dir-file-target|--env-override|--resume|--status|--cancel|--collect)
            [[ $# -ge 2 && -n "$2" && "$2" != --* ]] || {
                echo "Error: $1 requires an argument" >&2; exit 1;
            }
            case "$1" in
                --nodes)
                    [[ -z "$nodes_spec" ]] || {
                        echo "Error: --nodes may be specified only once" >&2; exit 1;
                    }
                    nodes_spec="$2" ;;
                --tasks)
                    [[ -z "$tasks_spec" ]] || {
                        echo "Error: --tasks may be specified only once" >&2; exit 1;
                    }
                    tasks_spec="$2" ;;
                --single-dir-file-target)
                    [[ -z "$single_dir_target_files" ]] || {
                        echo "Error: --single-dir-file-target may be specified only once" >&2; exit 1;
                    }
                    single_dir_target_files="$2" ;;
                --env-override)
                    [[ -z "$env_override_file" ]] || {
                        echo "Error: --env-override may be specified only once" >&2; exit 1;
                    }
                    env_override_file="$2" ;;
                *)
                    [[ "$operation" == submit ]] || {
                        echo "Error: lifecycle operations are mutually exclusive" >&2; exit 1;
                    }
                    operation="${1#--}"
                    operation_dir="$2" ;;
            esac
            shift 2 ;;
        *) echo "Error: unexpected argument: $1" >&2; usage >&2; exit 1 ;;
    esac
done

if [[ "$operation" != submit && -n "$env_override_file" ]]; then
    echo "Error: --env-override applies only to a new submission or --batch/--append;" \
        "existing results always use their saved env_used.sh" >&2
    exit 1
fi
if [[ "$operation" != submit && ( -n "$nodes_spec" || -n "$tasks_spec" \
        || -n "$single_dir_target_files" ) ]]; then
    echo "Error: $operation is mutually exclusive with workload flags" >&2
    exit 1
fi
if [[ "$operation" == submit && ( -z "$nodes_spec" || -z "$tasks_spec" ) ]]; then
    echo "Error: --nodes and --tasks are required together" >&2
    exit 1
fi

_source_kubectl_helpers() {
    # shellcheck disable=SC1091
    source "$SCRIPT_DIR/kubectl/_nv-elbencho-kubectl-functions.sh" || return 1
}

# Kubernetes lifecycle commands consult the saved attempt, never today's env.sh.
if [[ "$operation" == status || "$operation" == cancel || "$operation" == collect ]]; then
    _source_kubectl_helpers || exit 1
    kubectl_lifecycle_operation "$operation" "$operation_dir"
    exit $?
fi

if [[ "$operation" == resume ]]; then
    [[ -d "$operation_dir" ]] || { echo "Error: results directory not found" >&2; exit 1; }
    OUTPUT_DIR=$(cd "$operation_dir" && pwd) || exit 1
    [[ "${OUTPUT_DIR##*/}" =~ ^mdtest-elbencho-[0-9]{8}Z[0-9]{6}$ \
        && -f "$OUTPUT_DIR/env_used.sh" && ! -L "$OUTPUT_DIR/env_used.sh" \
        && -d "$OUTPUT_DIR/executions" ]] || {
        echo "Error: results directory has no reified MD sweep snapshot" >&2; exit 1;
    }
    DS="${OUTPUT_DIR##*-}"
    unset EXECUTION_SUBSTRATE
    # shellcheck disable=SC1090,SC1091  # Trusted canonical snapshot.
    source "$OUTPUT_DIR/env_used.sh" || exit 1
    SAVED_EXECUTION_SUBSTRATE="${EXECUTION_SUBSTRATE:-}"
    case "$SAVED_EXECUTION_SUBSTRATE" in ssh|slurm|kubectl) ;; *) exit 1 ;; esac
    if [[ -n "$INVOKING_EXECUTION_SUBSTRATE" \
            && "$INVOKING_EXECUTION_SUBSTRATE" != "$SAVED_EXECUTION_SUBSTRATE" ]]; then
        echo "Error: inherited substrate differs from saved substrate" >&2
        exit 1
    fi
    if [[ "$SAVED_EXECUTION_SUBSTRATE" == kubectl ]]; then
        _source_kubectl_helpers || exit 1
        kubectl_resume_collected_sweep "$OUTPUT_DIR"
        exit $?
    fi
fi

# shellcheck disable=SC2016  # $1 expands in the child shell.
if ! source_output=$("$BASH" -c 'source "$1"' env-loader \
        "$SCRIPT_DIR/../../env.sh" 2>&1); then
    printf '%s\nFailed to source env.sh\n' "$source_output" >&2
    exit 1
fi
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../env.sh" || exit 1
if [[ "$operation" == resume ]]; then
    [[ "${EXECUTION_SUBSTRATE:-}" == "$SAVED_EXECUTION_SUBSTRATE" ]] || {
        echo "Error: current env.sh substrate differs from saved substrate" >&2
        exit 1
    }
    unset EXECUTION_SUBSTRATE
    # shellcheck disable=SC1090,SC1091
    source "$OUTPUT_DIR/env_used.sh" || exit 1
fi
# A new submission layers its --env-override over env.sh at top level, before
# any validation or output, so every check and snapshot sees effective values.
if [[ "$operation" == submit ]]; then
    reset_env_override_provenance
    if [[ -n "$env_override_file" ]]; then
        env_override_file=$(resolve_env_override_file "$env_override_file") || exit 1
        # Batch preparation passes a staged copy; record the operator's file.
        env_override_label="$env_override_file"
        if [[ -n "${ELBENCHO_BATCH_PREPARE_DIR:-}" ]]; then
            env_override_label="${ELBENCHO_BATCH_ENV_OVERRIDE_ORIGIN:-$env_override_file}"
        fi
        env_override_declarations=$(filesystem_env_override_declarations \
            "$env_override_file" "$env_override_label" \
            "${ELBENCHO_BATCH_PREPARE_DIR:+$ELBENCHO_BATCH_PREPARE_DIR/.env-override-declarations.sh}") || exit 1
        eval "$env_override_declarations"
    fi
fi
if [[ -n "${SLURM_JOB_ID:-}" && -z "${SSH_ENABLED:-}" ]]; then
    echo "Error: run this launcher outside a Slurm allocation" >&2
    exit 1
fi
# shellcheck disable=SC1091
source "$SCALE_TEST_BASE/lib/_elbencho_functions.sh" || exit 1
[[ -n "${FS_ENABLED:-}" ]] || { echo "Error: filesystem testing is disabled" >&2; exit 1; }

if [[ "$operation" == resume ]]; then
    resume_log="$OUTPUT_DIR/mdtest-elbencho-sweep-$DS-resume-$(date -u +%Y%m%dZ%H%M%S).log"
    exec 1> >(tee -a "$resume_log")
    exec 2> >(tee -a "$resume_log" >&2)
    cd "$SCALE_TEST_BASE/storage-tests/fs" || exit 1
    if [[ -n "${SLURM_ENABLED:-}" ]]; then
        dispatch_slurm_executions "$OUTPUT_DIR"; exit $?
    fi
    if [[ -n "${SSH_ENABLED:-}" ]]; then
        dispatch_ssh_executions "$OUTPUT_DIR"; exit $?
    fi
    echo "Error: saved substrate cannot dispatch" >&2
    exit 1
fi

for value in "$MDTEST_BRANCH_FACTOR" "$MDTEST_ITEMS_PER_DIR" "$MDTEST_ITERATIONS"; do
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
        echo "Error: MDTEST_BRANCH_FACTOR, MDTEST_ITEMS_PER_DIR, and MDTEST_ITERATIONS must be positive integers" >&2
        exit 1
    }
done
nodes_output=$(parse_range_specification "$nodes_spec") || exit 1
tasks_output=$(parse_range_specification "$tasks_spec") || exit 1
mapfile -t node_counts <<< "$nodes_output"
mapfile -t task_counts <<< "$tasks_output"
[[ ${#node_counts[@]} -gt 0 && ${#task_counts[@]} -gt 0 ]] || exit 1
max_node_count=0
for nodes in "${node_counts[@]}"; do
    [[ "$nodes" =~ ^[1-9][0-9]*$ ]] || exit 1
    (( nodes <= max_node_count )) || max_node_count="$nodes"
done
for tasks in "${task_counts[@]}"; do
    [[ "$tasks" =~ ^[1-9][0-9]*$ ]] || exit 1
done
if (( ${#node_counts[@]} * ${#task_counts[@]} > 9999 )); then
    echo "Error: sweep exceeds the 9999-execution ledger limit" >&2
    exit 1
fi
declare -A seen_pairs=()
for nodes in "${node_counts[@]}"; do
    for tasks in "${task_counts[@]}"; do
        pair="${nodes}:${tasks}"
        [[ ! -v seen_pairs["$pair"] ]] || {
            echo "Error: duplicate node/task pair: $pair" >&2
            exit 1
        }
        seen_pairs["$pair"]=1
    done
done
single_dir_files_per_worker=""
single_dir_actual_files=""
if [[ -n "$single_dir_target_files" ]]; then
    [[ "$single_dir_target_files" =~ ^[1-9][0-9]*$ \
        && ${#node_counts[@]} -eq 1 && ${#task_counts[@]} -eq 1 ]] || {
        echo "Error: dense mode requires a positive target and one node/task pair" >&2
        exit 1
    }
    mapfile -t dense_targets < <(generate_fs_test_directories mdtest-elbencho)
    [[ ${#dense_targets[@]} -eq 1 ]] || {
        echo "Error: dense mode requires one generated target" >&2; exit 1;
    }
    single_dir_files_per_worker=$(mdtest_single_dir_files_per_worker \
        "$single_dir_target_files" "${node_counts[0]}" "${task_counts[0]}") || exit 1
    single_dir_actual_files=$((node_counts[0] * task_counts[0] * single_dir_files_per_worker))
fi

DS=${ELBENCHO_BATCH_DATESTAMP:-$(date -u +%Y%m%dZ%H%M%S)}
OUTPUT_DIR=${ELBENCHO_BATCH_PREPARE_DIR:-"$RESULTS_DIR/mdtest-elbencho-$DS"}
mkdir -p "$OUTPUT_DIR" || exit 1
export DS OUTPUT_DIR
write_mdtest_elbencho_env_used "$OUTPUT_DIR/env_used.yaml" \
    "$nodes_spec" "$tasks_spec" "$single_dir_target_files" \
    "$single_dir_files_per_worker" "$single_dir_actual_files" || exit 1
runner_log="$OUTPUT_DIR/mdtest-elbencho-sweep-$DS-runner.log"
exec 1> >(tee -a "$runner_log")
exec 2> >(tee -a "$runner_log" >&2)
print_env_override_summary
echo "Metadata sweep: nodes=${node_counts[*]} tasks=${task_counts[*]} output=$OUTPUT_DIR"
if [[ -n "$single_dir_target_files" ]]; then
    print_mdtest_single_dir_target_summary "$single_dir_target_files" \
        "${node_counts[0]}" "${task_counts[0]}" \
        "$single_dir_files_per_worker" "$single_dir_actual_files"
fi
reify_all_mdtest_executions "$OUTPUT_DIR" "$DS" node_counts task_counts \
    "$single_dir_target_files" "$single_dir_files_per_worker" || exit 1
[[ -z "${ELBENCHO_BATCH_PREPARE_DIR:-}" ]] || exit 0
cd "$SCALE_TEST_BASE/storage-tests/fs" || exit 1
if [[ -n "${KUBECTL_ENABLED:-}" ]]; then
    _source_kubectl_helpers || exit 1
    kubectl_submit_sweep "$OUTPUT_DIR" "$max_node_count"
    exit $?
fi
if [[ -n "${SLURM_ENABLED:-}" ]]; then
    dispatch_slurm_executions "$OUTPUT_DIR"; exit $?
fi
if [[ -n "${SSH_ENABLED:-}" ]]; then
    dispatch_ssh_executions "$OUTPUT_DIR"; exit $?
fi
echo "Error: no execution substrate is enabled" >&2
exit 1
