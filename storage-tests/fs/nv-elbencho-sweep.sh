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

# Boilerplate to find and source env.sh
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd) || {
    echo "Error: Failed to determine script directory" >&2
    exit 1
}
if [[ ! -d "${SCRIPT_DIR}" ]]; then
    echo "Error: Script directory '${SCRIPT_DIR}' does not exist" >&2
    exit 1
fi
readonly SCRIPT_DIR

if ! source_output=$("$SHELL" -c ". '${SCRIPT_DIR}/../../env.sh'" 2>&1); then
    printf "%s\n\nFailed to source env.sh; fix ^^^^^^^^^^\n" "$source_output"
    exit 1
fi

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/../../env.sh"

# Source the elbencho helpers library directly. env.sh chains through
# lib/env_base.sh -> lib/env_functions.sh, but the elbencho-specific
# helpers (reify_all_elbencho_executions, _elbencho_sweep_running_to_pending,
# list_elbencho_execution_ids, max_nodes_remaining_executions, etc.) live
# in lib/_elbencho_functions.sh and are needed by this top-level
# orchestrator (reify + dispatch + --resume paths).
# shellcheck disable=SC1091
source "${SCALE_TEST_BASE}/lib/_elbencho_functions.sh"

# Only run directly, not from within slurm unless SSH_ENABLED is set (slurm
# may be used to get nodes to ssh to)
if [ -n "${SLURM_JOB_ID:-}" ] && [ -z "${SSH_ENABLED:-}" ]; then
    echo "Error: Don't run this with slurm, just run it directly." >&2
    exit 1
fi

print_usage() {
    cat << EOF
Usage: $0 [FLAGS] --nodes <node_spec>
   or: $0 [FLAGS] --delete-only <path>
   or: $0 --resume <results_dir>

Runs elbencho on one or more nodes (sweep over node counts and IO sizes), or deletes
a prior sweep subtree with --delete-only, or resumes an interrupted sweep with --resume.

Sweep modes (--write-only, --write-no-read, default, or --read-from <path>):
  --nodes <node_spec>   Required. Comma-separated list of node counts or ranges.

  Node specification format:
    X           Single value (e.g., "5" runs with 5 nodes)
    X-Y         Range from X to Y with increment 1 (e.g., "2-5" runs 2,3,4,5)
    X-Y+Z       Range from X to Y with increment Z (e.g., "1-10+3" runs 1,4,7,10)

  Multiple elements can be comma-separated:
    "1,4,8"         runs with node counts: 1, 4, 8
    "1-4,16"        runs with node counts: 1, 2, 3, 4, 16
    "16,8,4,2,1"    runs with node counts: 16, 8, 4, 2, 1 (descending)
    "3-10+2,13"     runs with node counts: 3, 5, 7, 9, 10, 13

Delete-only (no sweep, no sbatch elbencho workers):
  --delete-only <path>  Remove a strict subdirectory under TEST_DIRS (not the test root).
                        Requires **SLURM_ENABLED** or **SSH_ENABLED**: runs on one compute node
                        via **sbatch** or over **SSH** to one host. --nodes is not used (ignored if set).

Resume (continue an interrupted prior sweep run):
  --resume <results_dir>  Path to a prior \${RESULTS_DIR}/elbencho-<DS>/ directory. Sources
                          the dir's env_used.sh sidecar to restore TEST_DIRS, FS_MAX_*, the
                          ELBENCHO_* settings and the original CLI flags; skips reification;
                          dispatches any executions whose status is not SUCCESS in NNNN order.
                          A node that went bad after the original run is NOT rebound to remaining
                          executions: SLURM srun-steps pick fresh subsets of the live allocation;
                          SSH dispatch calls choose_N_ssh_hosts per execution. Mutually exclusive
                          with all other flags.

Path modes (at most one; --write-only and --read-from require a single TEST_DIRS entry):
  --write-only          Only mkdir + write; retain one uniquely suffixed data directory per
                        (nodes, IO size, threads, IO depth) execution and print one
                        ELBENCHO_WRITE_ONLY_DATA_DIR=... line for each successful execution.
                        Configure one value in each sweep dimension to create one dataset.
  --write-no-read       Mkdir + write each sweep iteration; skip read; delete test data after each iteration.
                        Shared-directory mode uses a verified, timed distributed RMFILES phase.
  --read-from <path>    Read-only from an existing path under TEST_DIRS. With ELBENCHO_SINGLE_BIG_FILE=0
                        (default), <path> is a directory (treescan). With ELBENCHO_SINGLE_BIG_FILE=1,
                        <path> must be a file passed through to elbencho read (error if <path> is a directory here).

Flags:
  -h, --help          Show this help message and exit
  -b, --bio           Use bio instead of the default dio (direct IO)
  -r, --rand          Use random IO patterns (default: sequential IO; not with ELBENCHO_SINGLE_BIG_FILE=1)
  -s, --single        Use computed file counts for a generated legacy one-target sweep;
                      inactive for shared-directory, --read-from, and single-file modes

Examples:
  $0 --nodes 1,2,4,6,8
  $0 --nodes 1,4,8,16
  $0 -b -r --nodes 2-4
  $0 --write-only --nodes 4
  $0 --write-no-read --nodes 4
  $0 --read-from /mnt/fs/elbencho-sweep-foo --nodes 8
  $0 --read-from /mnt/fs/elbencho-sweep-foo/elbencho-bigfile --nodes 8
  $0 --delete-only /mnt/fs/elbencho-sweep-foo

EOF
}

# Initialize flags (if any)
g_bio_or_dio="dio"
rand_option="0"
single_option="0"
nodes_spec=""
sweep_write_only="0"
sweep_write_no_read="0"
sweep_read_from=""
delete_only_path=""
resume_dir=""

# Parse flags
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            print_usage
            exit 0
            ;;
        -b|--bio)
            g_bio_or_dio="bio"
            shift
            ;;
        -r|--rand)
            rand_option="1"
            shift
            ;;
        -s|--single)
            single_option="1"
            shift
            ;;
        --nodes)
            if [[ $# -lt 2 ]]; then
                echo "Error: --nodes requires an argument" >&2
                print_usage
                exit 1
            fi
            nodes_spec="$2"
            shift 2
            ;;
        --write-only)
            sweep_write_only="1"
            shift
            ;;
        --write-no-read)
            sweep_write_no_read="1"
            shift
            ;;
        --read-from)
            if [[ $# -lt 2 ]]; then
                echo "Error: --read-from requires a path argument" >&2
                exit 1
            fi
            sweep_read_from="$2"
            shift 2
            ;;
        --delete-only)
            if [[ $# -lt 2 ]]; then
                echo "Error: --delete-only requires a path argument" >&2
                exit 1
            fi
            delete_only_path="$2"
            shift 2
            ;;
        --resume)
            if [[ $# -lt 2 ]]; then
                echo "Error: --resume requires a path argument" >&2
                exit 1
            fi
            resume_dir="$2"
            shift 2
            ;;
        -*)
            echo "Error: Unknown option: $1" >&2
            print_usage
            exit 1
            ;;
        *)
            echo "Error: Unexpected positional argument: $1" >&2
            echo "  This script does not accept positional arguments; use --nodes for sweep modes." >&2
            print_usage
            exit 1
            ;;
    esac
done

# Validate that --resume is the only flag in play.
_validate_resume_mutual_exclusivity() {
    local has_extra=0
    [[ "$g_bio_or_dio" != "dio" ]] && has_extra=1
    [[ "$rand_option" != "0" ]] && has_extra=1
    [[ "$single_option" != "0" ]] && has_extra=1
    [[ -n "$nodes_spec" ]] && has_extra=1
    [[ "$sweep_write_only" != "0" ]] && has_extra=1
    [[ "$sweep_write_no_read" != "0" ]] && has_extra=1
    [[ -n "$sweep_read_from" ]] && has_extra=1
    [[ -n "$delete_only_path" ]] && has_extra=1
    if [[ "$has_extra" -eq 1 ]]; then
        echo "Error: --resume is mutually exclusive with the other flags" >&2
        echo "  (when resuming, the original sweep's settings are read from <results_dir>/env_used.sh)" >&2
        exit 1
    fi
    return 0
}

# Load <results_dir>/env_used.sh into the current shell, setting OUTPUT_DIR,
# DS, and the sweep-script CLI vars (g_bio_or_dio, rand_option, etc.).
_resume_load_env_used() {
    local dir="$1"
    if [[ ! -d "$dir" ]]; then
        echo "Error: --resume directory does not exist: $dir" >&2
        exit 1
    fi
    local abs
    abs=$(cd "$dir" && pwd) || exit 1
    if [[ ! -f "$abs/env_used.sh" ]]; then
        echo "Error: $abs/env_used.sh not found (cannot rehydrate sweep configuration)" >&2
        echo "  --resume requires a results dir created by this code revision (env_used.sh sidecar)." >&2
        exit 1
    fi
    if [[ ! -d "$abs/executions" ]]; then
        echo "Error: $abs/executions/ not found (results dir was not reified by this code revision)" >&2
        exit 1
    fi
    OUTPUT_DIR="$abs"
    DS="${OUTPUT_DIR##*-}"
    echo "Resuming sweep at $OUTPUT_DIR (DS=$DS)"
    # Older snapshots predate the generated shared-directory controls. Reset
    # them before sourcing so current env.sh settings cannot leak into resume.
    ELBENCHO_FILE_LAYOUT=worker-directories
    ELBENCHO_FILES_PER_NODE=
    ELBENCHO_FILE_SIZE=
    export ELBENCHO_FILE_LAYOUT ELBENCHO_FILES_PER_NODE ELBENCHO_FILE_SIZE
    # shellcheck disable=SC1091
    source "$OUTPUT_DIR/env_used.sh" || {
        echo "Error: failed to source $OUTPUT_DIR/env_used.sh" >&2
        exit 1
    }
    # Bridge the lone variable name mismatch between env_used.sh's `dio_or_bio`
    # (matches the YAML key) and this script's local `g_bio_or_dio`.
    # shellcheck disable=SC2154  # dio_or_bio comes from sourcing env_used.sh above
    g_bio_or_dio="$dio_or_bio"
    return 0
}

# Validate the active configuration after env_used.sh has been restored on resume.
_validate_elbencho_sweep_environment() {
    local test_dirs_declaration
    # shellcheck disable=SC2153  # TEST_DIRS is loaded from env.sh or env_used.sh
    test_dirs_declaration=$(declare -p TEST_DIRS 2>/dev/null || true)
    if [[ -z "$test_dirs_declaration" || ${#TEST_DIRS[@]} -eq 0 ]]; then
        echo "Error: Filesystem testing is not enabled (TEST_DIRS is empty)" >&2
        echo "  Set TEST_DIRS in env.sh to enable filesystem testing" >&2
        return 1
    fi
    export FS_ENABLED=1

    local validation_failed=0
    if ! validate_integer_array ELBENCHO_SCALE_THREAD_LIST >&2; then
        validation_failed=1
    fi
    if ! validate_elbencho_io_sizes >&2; then
        validation_failed=1
    fi
    if ! validate_integer_array ELBENCHO_IODEPTH_LIST >&2; then
        validation_failed=1
    fi
    if ! validate_elbencho_file_workload_env >&2; then
        validation_failed=1
    fi
    if ! validate_elbencho_duration >&2; then
        validation_failed=1
    fi
    if ! validate_elbencho_live_csv >&2; then
        validation_failed=1
    fi

    if [[ "$validation_failed" -ne 0 ]]; then
        echo "Error: Elbencho configuration validation failed. Fix the errors above and try again." >&2
        return 1
    fi
    return 0
}

_run_delete_only_path() {
    local path="$1"
    local ds_delete
    ds_delete=$(date -u +"%Y%m%dZ%H%M%S")
    local delete_output_dir="${RESULTS_DIR}/elbencho-${ds_delete}"
    mkdir -p "${delete_output_dir}" || return 1

    if [[ -n "${SSH_ENABLED:-}" ]]; then
        if ! ensure_all_ssh_nodes_can_elbencho; then
            echo "Error: Failed to ensure all ssh nodes can elbencho (needed for delete helper files)" >&2
            return 1
        fi
        local ssh_host=""
        if [[ -n "${SSH_NODELIST:-}" ]]; then
            ssh_host="${SSH_NODELIST%%,*}"
        elif [[ ${#SSH_ALL_HOSTS[@]} -gt 0 ]]; then
            ssh_host="${SSH_ALL_HOSTS[0]}"
        else
            echo "Error: No SSH host available for --delete-only (set SSH_NODELIST or SSH_HOST_LIST)." >&2
            return 1
        fi
        local ssh_log="${delete_output_dir}/elbencho-delete-only-${ds_delete}-ssh.log"
        echo "Delete-only log (SSH): ${ssh_log}" >&2
        local status_dir
        status_dir=$(mktemp -d) || return 1
        run_ssh_single "$ssh_host" "${status_dir}/delete.rc" >(tee -a "$ssh_log") "" \
            "@${SCRIPT_DIR}/ssh/_nv-elbencho-delete-only-scriptlet.sh" "$path"
        local rc
        rc=$(tr -d '\r\n' <"${status_dir}/delete.rc" 2>/dev/null || echo 1)
        rm -rf "${status_dir}"
        [[ "$rc" =~ ^[0-9]+$ ]] || rc=1
        return "$rc"
    fi
    if [[ -n "${SLURM_ENABLED:-}" ]]; then
        local -a sbatch_cmd
        if ! build_sbatch_cmd sbatch_cmd; then
            return 1
        fi
        # shellcheck disable=SC2034  # Used by run_sbatch_job
        local sleep_time=10
        local log_files=()
        # shellcheck disable=SC2034  # Used by run_sbatch_job
        local g_sbatch_opts=()
        # run_sbatch_job assigns JOBID; nameref keeps a conventional local name (Sonar S7684).
        local job_id=""
        # shellcheck disable=SC2034  # JOBID is the nameref alias to job_id, written by run_sbatch_job
        declare -n JOBID=job_id
        local job_name
        job_name=$(make_sbatch_job_name "elbencho-delete" "$ds_delete" "")
        local output_fmt
        output_fmt="${delete_output_dir}/elbencho-delete-only-%j.out"
        cd "${SCRIPT_DIR}" || return 1
        # Consumed indirectly by run_sbatch_job (same shell scope; static analyzers do not always track)
        : "${#sbatch_cmd[@]}" "$sleep_time" "${g_sbatch_opts[*]:-}"
        if ! run_sbatch_job 1 "$job_name" "$output_fmt" "Delete-only" \
            sbatch/_nv-elbencho-delete-only.sh "$path"; then
            return 1
        fi
        tail_until_complete "$job_id" "${log_files[@]}"
        return $?
    fi
    echo "Error: --delete-only requires SLURM_ENABLED or SSH_ENABLED (set in env.sh)." >&2
    return 1
}

# --resume short-circuits everything else: rehydrate the prior sweep's
# configuration from <dir>/env_used.sh, then dispatch any non-SUCCESS executions
# already reified under <dir>/executions/ via the SLURM coordinator or SSH loop.
if [[ -n "$resume_dir" ]]; then
    _validate_resume_mutual_exclusivity
    _resume_load_env_used "$resume_dir"
    _validate_elbencho_sweep_environment || exit 1
    validate_elbencho_sweep_workload_mode \
        "$g_bio_or_dio" "$rand_option" "$sweep_read_from" || exit 1

    # Per-resume log file (separate from the original run's runner log so we
    # preserve forensics from both attempts in the same dir).
    resume_log="${OUTPUT_DIR}/elbencho-sweep-${DS}-resume-$(date -u +%Y%m%dZ%H%M%S).log"
    echo "Resume log: ${resume_log}"
    exec 1> >(tee -a "${resume_log}")
    exec 2> >(tee -a "${resume_log}" >&2)

    cd "${SCALE_TEST_BASE}/storage-tests/fs" || exit 1
    export DS

    if [[ -n "$SLURM_ENABLED" ]]; then
        dispatch_slurm_executions "$OUTPUT_DIR"
        exit $?
    fi
    if [[ -n "$SSH_ENABLED" ]]; then
        dispatch_ssh_executions "$OUTPUT_DIR"
        exit $?
    fi
    echo "Error: neither SLURM_ENABLED nor SSH_ENABLED is set; cannot dispatch" >&2
    exit 1
fi

_validate_elbencho_sweep_environment || exit 1

# Mutual exclusivity of path modes
mode_count=0
[[ "$sweep_write_only" == "1" ]] && mode_count=$((mode_count + 1))
[[ "$sweep_write_no_read" == "1" ]] && mode_count=$((mode_count + 1))
[[ -n "$sweep_read_from" ]] && mode_count=$((mode_count + 1))
[[ -n "$delete_only_path" ]] && mode_count=$((mode_count + 1))
if [[ "$mode_count" -gt 1 ]]; then
    echo "Error: Use at most one of --write-only, --write-no-read, --read-from, and --delete-only." >&2
    exit 1
fi

if [[ -z "$delete_only_path" ]] && ! validate_elbencho_single_big_file_env "$sweep_read_from" >&2; then
    exit 1
fi

if [[ -n "$delete_only_path" ]]; then
    if [[ -z "${SLURM_ENABLED:-}" && -z "${SSH_ENABLED:-}" ]]; then
        echo "Error: --delete-only requires SLURM_ENABLED or SSH_ENABLED (set in env.sh)." >&2
        exit 1
    fi
    if ! validate_elbencho_sweep_single_test_dirs_key; then
        exit 1
    fi
    if ! validate_elbencho_sweep_delete_only_path "$delete_only_path"; then
        exit 1
    fi
    if ! _run_delete_only_path "$delete_only_path"; then
        exit 1
    fi
    exit 0
fi

if [[ -z "$nodes_spec" ]]; then
    echo "Error: --nodes is required (unless using --delete-only)." >&2
    print_usage
    exit 1
fi

if [[ "${ELBENCHO_SINGLE_BIG_FILE:-0}" == "1" && "$rand_option" == "1" ]]; then
    echo "Error: ELBENCHO_SINGLE_BIG_FILE=1 is mutually exclusive with -r / --rand (single-big-file mode is sequential IO only)." >&2
    exit 1
fi

# Parse --nodes specification into array
if ! mapfile -t node_counts < <(parse_range_specification "$nodes_spec"); then
    echo "Error: Invalid node specification: $nodes_spec" >&2
    exit 1
fi
if [[ ${#node_counts[@]} -eq 0 ]]; then
    echo "Error: Node specification produced no node counts" >&2
    exit 1
fi

max_node_count=0
for node_count in "${node_counts[@]}"; do
    if [[ "$node_count" -gt "$max_node_count" ]]; then
        max_node_count="$node_count"
    fi
done

# Complete all CLI/mode-aware validation before selecting DS or creating any
# output. Invalid exact-workload requests must leave no partial result run.
validate_elbencho_sweep_workload_mode \
    "$g_bio_or_dio" "$rand_option" "$sweep_read_from" || exit 1

if [[ "$sweep_write_only" == "1" || -n "$sweep_read_from" ]]; then
    validate_elbencho_sweep_single_test_dirs_key || exit 1
    validate_elbencho_sweep_one_generated_target_dir || exit 1
fi
if [[ -n "$sweep_read_from" ]]; then
    validate_elbencho_sweep_read_from_path "$sweep_read_from" || exit 1
    validate_elbencho_single_big_file_read_from_is_not_dir \
        "$sweep_read_from" || exit 1
fi

# Using one datestamp for all the jobs will give output parsing script(s)
# more options for how to group-by when given just a directory containing
# these log files (easier to specify).
DS=$(date -u +"%Y%m%dZ%H%M%S")
OUTPUT_DIR="${RESULTS_DIR}/elbencho-${DS}"

mkdir -p "$OUTPUT_DIR"

write_elbencho_env_used "${OUTPUT_DIR}/env_used.yaml" \
    "$g_bio_or_dio" "$rand_option" "$single_option" \
    "$sweep_write_only" "$sweep_write_no_read" "$sweep_read_from" "$nodes_spec"

# Log output to file as well as the invoking stdout/stderr streams
out_log="${OUTPUT_DIR}/elbencho-sweep-${DS}-runner.log"
exec 1> >(tee -a "${out_log}")
exec 2> >(tee -a "${out_log}" >&2)

cd "${SCALE_TEST_BASE}/storage-tests/fs" || exit 1

export DS

if [[ -n "$sweep_read_from" ]]; then
    if [[ -d "$sweep_read_from" ]]; then
        :
    elif [[ "${ELBENCHO_SINGLE_BIG_FILE:-0}" == "1" ]]; then
        echo "Note: --read-from is not a directory on this host (ok for ELBENCHO_SINGLE_BIG_FILE file path or compute-only FS): $sweep_read_from" >&2
    else
        echo "Warning: --read-from path is not a directory on this host (may be visible only on compute nodes): $sweep_read_from" >&2
    fi
    if [[ "${ELBENCHO_SINGLE_BIG_FILE:-0}" != "1" ]]; then
        echo "Cached treefile (unlink to rescan): ${sweep_read_from}/.storage-scale-test-elbencho-treefile.txt"
    fi
fi

if [[ -n "$SLURM_ENABLED" ]]; then
    print_slurm_node_warnings "$max_node_count"
fi

# Reify every (nodes, io_size, thread_count, io_depth) tuple into a
# self-contained executions/NNNN.sh + NNNN.status=PENDING. This is the
# canonical record of what the run will execute, and is the unit of
# resumability if interrupted.
if ! reify_all_elbencho_executions "$OUTPUT_DIR" "$nodes_spec" \
        "$g_bio_or_dio" "$rand_option" "$single_option" \
        "$sweep_write_only" "$sweep_write_no_read" "$sweep_read_from"; then
    echo "Error: failed to reify executions" >&2
    exit 1
fi

# Dispatch: SLURM submits one coordinator sbatch and tails its log; SSH
# loops through executions on the local host with fresh per-execution
# host selection. Both abort on first FAILED so the user can fix and
# re-run with --resume.
if [[ -n "$SLURM_ENABLED" ]]; then
    dispatch_slurm_executions "$OUTPUT_DIR"
    exit $?
fi
if [[ -n "$SSH_ENABLED" ]]; then
    dispatch_ssh_executions "$OUTPUT_DIR"
    exit $?
fi
exit 0
