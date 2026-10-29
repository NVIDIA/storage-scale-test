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

# Exit on undefined variables
set -u

if (( BASH_VERSINFO[0] < 4 \
        || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4) )); then
    printf 'Bash 4.4 or newer is required; on macOS run: brew install bash\n' >&2
    exit 1
fi

# Initialize error tracking
declare -a errors=()
declare source_output=""

# Get absolute path to script directory
SCALE_TEST_BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/project_version.sh
# shellcheck disable=SC1091
source "$SCALE_TEST_BASE/lib/project_version.sh" || exit 1
project_version_option "$SCALE_TEST_BASE" "$@"

validate_env_usage() {
    cat <<'USAGE'
Usage: validate_env.sh [--sequencer] [-v|--verbose] [--version] [-h|--help]

Check env.sh and the environment it describes, reporting every problem.
When nothing is wrong it prints a short summary of what was validated;
errors, warnings, and notes about skipped checks are always printed.

  -v, --verbose
               Also print a detail line for every check that passed.
  --sequencer  Kubernetes only: run the full probe of the in-cluster sequencer
               that a batch spanning several namespaces needs (server
               version, RBAC, and a probe Job under its RBAC) and treat its
               problems as errors. Without it, the image and RBAC checks
               are warnings only.
  --version    Print the version and exit.
  -h, --help   Print this help and exit.
USAGE
}

# Set by --sequencer: run the sequencer's full probe and make problems errors.
VALIDATE_SEQUENCER=""
# Set by --verbose: print a detail line for every check that passed.
VALIDATE_VERBOSE=""
for argument in "$@"; do
    case "$argument" in
        --sequencer) VALIDATE_SEQUENCER=1 ;;
        -v|--verbose) VALIDATE_VERBOSE=1 ;;
        -h|--help) validate_env_usage; exit 0 ;;
        *)
            printf 'Error: unknown argument: %s\n' "$argument" >&2
            validate_env_usage >&2
            exit 2
            ;;
    esac
done

# Initialize error accumulation system
ERROR_FILE=""
WARNING_FILE=""
SUMMARY_FILE=""

# Initialize error file and set up cleanup
init_error_tracking() {
    ERROR_FILE=$(mktemp "${TMPDIR:-/tmp}/validate_env_errors.XXXXXX")
    if [[ -z "$ERROR_FILE" || ! -f "$ERROR_FILE" ]]; then
        printf "Failed to create temporary error file\n" >&2
        exit 1
    fi

    WARNING_FILE=$(mktemp "${TMPDIR:-/tmp}/validate_env_warnings.XXXXXX")
    if [[ -z "$WARNING_FILE" || ! -f "$WARNING_FILE" ]]; then
        printf "Failed to create temporary warning file\n" >&2
        exit 1
    fi

    SUMMARY_FILE=$(mktemp "${TMPDIR:-/tmp}/validate_env_summary.XXXXXX")
    if [[ -z "$SUMMARY_FILE" || ! -f "$SUMMARY_FILE" ]]; then
        printf "Failed to create temporary summary file\n" >&2
        exit 1
    fi

    # Ensure cleanup on exit
    trap 'cleanup_error_tracking' EXIT INT TERM
}

# Clean up error file
# shellcheck disable=SC2317,SC2329  # Called indirectly via trap
cleanup_error_tracking() {
    if [[ -n "$ERROR_FILE" && -f "$ERROR_FILE" ]]; then
        rm -f "$ERROR_FILE"
    fi
    if [[ -n "$WARNING_FILE" && -f "$WARNING_FILE" ]]; then
        rm -f "$WARNING_FILE"
    fi
    if [[ -n "$SUMMARY_FILE" && -f "$SUMMARY_FILE" ]]; then
        rm -f "$SUMMARY_FILE"
    fi
    rm -f "${ERROR_FILE:-}.lock" "${WARNING_FILE:-}.lock" \
        "${SUMMARY_FILE:-}.lock"
}

# Register an error (thread-safe across subshells)
register_error() {
    local error_message="$1"
    if [[ -z "$ERROR_FILE" ]]; then
        printf "Error tracking not initialized\n" >&2
        return 1
    fi

    # Use flock for thread safety when writing to the error file, if available
    if type -P flock >/dev/null 2>&1; then
        (
            flock -x 200
            printf '%s\0' "$error_message" >> "$ERROR_FILE"
        ) 200>"$ERROR_FILE.lock"
    else
        # Fallback: direct write without locking (potential race condition)
        printf '%s\0' "$error_message" >> "$ERROR_FILE"
    fi
}

# Register a non-fatal warning (printed at end; does not fail validation)
register_warning() {
    local warning_message="$1"
    if [[ -z "$WARNING_FILE" ]]; then
        printf "Warning tracking not initialized\n" >&2
        return 1
    fi

    if type -P flock >/dev/null 2>&1; then
        (
            flock -x 200
            printf '%s\0' "$warning_message" >> "$WARNING_FILE"
        ) 200>"$WARNING_FILE.lock"
    else
        printf '%s\0' "$warning_message" >> "$WARNING_FILE"
    fi
}

# Register one line of the success summary: what a passed check validated. The
# lines are printed together, in registration order, after the checks. Like
# errors and warnings, a concurrent branch's lines are replayed in order.
register_summary() {
    local summary_message="$1"
    if [[ -z "$SUMMARY_FILE" ]]; then
        printf "Summary tracking not initialized\n" >&2
        return 1
    fi

    if type -P flock >/dev/null 2>&1; then
        (
            flock -x 200
            printf '%s\0' "$summary_message" >> "$SUMMARY_FILE"
        ) 200>"$SUMMARY_FILE.lock"
    else
        printf '%s\0' "$summary_message" >> "$SUMMARY_FILE"
    fi
}

# Get all registered errors as a bash array
all_registered_errors() {
    local -n result_array=$1
    result_array=()

    if [[ -z "$ERROR_FILE" || ! -f "$ERROR_FILE" ]]; then
        return 0
    fi

    # Read errors from file into array
    while IFS= read -r -d '' line; do
        result_array+=("$line")
    done < "$ERROR_FILE"
}

# Get all registered warnings as a bash array (records are NUL-separated so
# each message may contain embedded newlines).
all_registered_warnings() {
    local -n result_warns=$1
    result_warns=()

    if [[ -z "$WARNING_FILE" || ! -f "$WARNING_FILE" ]]; then
        return 0
    fi

    # Concurrent checks may each see the same kubectl advisory; print it once.
    local -A seen=()
    while IFS= read -r -d '' rec; do
        [[ -z "${seen[$rec]+set}" ]] || continue
        seen[$rec]=1
        result_warns+=("$rec")
    done < "$WARNING_FILE"
}

# Get all registered summary lines as a bash array.
all_registered_summaries() {
    local -n result_summaries=$1
    result_summaries=()

    if [[ -z "$SUMMARY_FILE" || ! -f "$SUMMARY_FILE" ]]; then
        return 0
    fi

    while IFS= read -r -d '' rec; do
        result_summaries+=("$rec")
    done < "$SUMMARY_FILE"
}

# The number of NUL-terminated records in FILE (errors, warnings or summary
# lines registered so far). Digits only: BSD wc pads its count.
_validate_record_count() {
    local count=0
    [[ ! -f "$1" ]] || count=$(( $(tr -cd '\0' < "$1" | wc -c) ))
    printf '%s\n' "$count"
}

# Initialize the error tracking system
init_error_tracking

# Source env.sh, capturing any output
if [[ ! -f "${SCALE_TEST_BASE}/env.sh" ]]; then
    echo "Error: env.sh not found; copy env.sh.template to env.sh and edit it" >&2
    exit 1
fi
if ! source_output=$("$BASH" -c "source \"\$1\"" env-loader \
        "${SCALE_TEST_BASE}/env.sh" 2>&1); then
    printf "%s\n\nFailed to source env.sh; fix ^^^^^^^^^^\n" "$source_output"
    exit 1
fi

# shellcheck disable=SC1091
. "${SCALE_TEST_BASE}/env.sh"
# env.sh normally loads lib/env_functions.sh (via env_base.sh), which defines it.
! declare -F apply_kubectl_defaults >/dev/null || apply_kubectl_defaults

# Check required directories
check_dirs() {
    [[ -d "${RESULTS_DIR}" ]] || register_error "Directory ${RESULTS_DIR} does not exist"
    [[ -d "${LOGS_DIR}" ]] || register_error "Directory ${LOGS_DIR} does not exist"
}

check_macos_launcher_prerequisites() {
    [[ $(uname -s) == Darwin ]] || return 0

    case "${EXECUTION_SUBSTRATE:-}" in
        ssh|kubectl) ;;
        *)
            register_error "macOS may initiate only SSH or kubectl filesystem sweeps; EXECUTION_SUBSTRATE=${EXECUTION_SUBSTRATE:-unset} is unsupported"
            return 1
            ;;
    esac

    local -a missing=()
    local command
    for command in grealpath gstat; do
        command -v "$command" >/dev/null 2>&1 || missing+=("$command")
    done
    if [[ "${EXECUTION_SUBSTRATE:-}" == kubectl ]]; then
        for command in gtimeout gtar flock; do
            command -v "$command" >/dev/null 2>&1 || missing+=("$command")
        done
    fi
    if (( ${#missing[@]} > 0 )); then
        local packages="bash coreutils"
        [[ "${EXECUTION_SUBSTRATE:-}" == kubectl ]] \
            && packages+=" gnu-tar flock"
        register_error "macOS ${EXECUTION_SUBSTRATE} launcher prerequisites are missing: ${missing[*]}. Install them with: brew install $packages"
        return 1
    fi
    validate_detail '  macOS launcher prerequisites: passed for %s\n' "$EXECUTION_SUBSTRATE"
}

# validate_detail FORMAT [ARG...]: printf a per-check success detail line, only
# with --verbose. Without it, register_summary lines state what passed.
validate_detail() {
    [[ -n "${VALIDATE_VERBOSE:-}" ]] || return 0
    # shellcheck disable=SC2059  # The caller supplies a literal format.
    printf "$@"
}

# Informational, read-only inventory of objects this toolset created that still
# exist in the cluster. Existence never fails validation, and labels are never
# treated as proof of ownership (only a result directory's UID journal is).
# Only stdout is parsed: kubectl warnings (for example version skew) go to
# stderr and must not become table rows.
_kubectl_inventory_list() {
    local stderr_path="$1"
    shift
    kubectl_run_observational "$@" \
        -l app.kubernetes.io/name=storage-scale-test \
        -o 'jsonpath={range .items[*]}{.metadata.namespace}{"|"}{.metadata.name}{"|"}{.metadata.labels.storage-scale-test\.nvidia\.com/run}{"|"}{.metadata.labels.app\.kubernetes\.io/component}{"|"}{.metadata.creationTimestamp}{"\n"}{end}' \
        2> "$stderr_path"
}

# The last kubectl error line, without retry notices or diagnostic records.
_kubectl_inventory_error() {
    local error
    error=$(grep -Ev '^(STORAGE_SCALE_TEST_DIAGNOSTIC_|Warning: transient Kubernetes observation failed)' \
        -- "$1" | grep . | tail -n 1)
    printf '%s' "${error:0:200}"
}

_print_kubectl_inventory_table() {
    awk -F'|' '
        { for (i = 1; i <= 6; i++) { cell[NR, i] = ($i == "" ? "-" : $i) } }
        END {
            split("KIND|NAMESPACE|NAME|RUN|COMPONENT|CREATED", header, "|")
            for (i = 1; i <= 6; i++) { cell[0, i] = header[i] }
            for (j = 0; j <= NR; j++) {
                for (i = 1; i <= 6; i++) {
                    if (length(cell[j, i]) > width[i]) { width[i] = length(cell[j, i]) }
                }
            }
            for (j = 0; j <= NR; j++) {
                line = ""
                for (i = 1; i <= 6; i++) { line = line sprintf("  %-" width[i] "s", cell[j, i]) }
                sub(/ +$/, "", line)
                print line
            }
        }'
}

# List one resource in each namespace given; fail if any listing fails.
_kubectl_inventory_list_namespaces() {
    local stderr_path="$1" resource="$2" namespace rows
    shift 2
    for namespace in "$@"; do
        rows=$(_kubectl_inventory_list "$stderr_path" -n "$namespace" get "$resource") \
            || return 1
        [[ -z "$rows" ]] || printf '%s\n' "$rows"
    done
}

# check_kubectl_owned_resources [NAMESPACE...]: the namespaces listed when
# cluster-wide listing is forbidden (default: KUBECTL_NAMESPACE).
check_kubectl_owned_resources() {
    local stderr_path resource kind output rows="" fallback="" unlisted=""
    local probe_failures="" scope="all namespaces" namespace_label
    local -a namespaces=("$@")
    [[ ${#namespaces[@]} -gt 0 ]] || namespaces=("$KUBECTL_NAMESPACE")
    namespace_label="namespace ${namespaces[0]}"
    if [[ ${#namespaces[@]} -gt 1 ]]; then
        namespace_label="namespaces $(printf '%s, ' "${namespaces[@]}")"
        namespace_label="${namespace_label%, }"
    fi
    stderr_path=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-validate-inventory.XXXXXX") || {
        register_warning "Could not create temporary storage for the Kubernetes object inventory"
        return 0
    }
    for resource in Job:jobs DaemonSet:daemonsets Pod:pods \
            NetworkPolicy:networkpolicies Lease:leases.coordination.k8s.io; do
        kind="${resource%%:*}"
        resource="${resource#*:}"
        if ! output=$(_kubectl_inventory_list "$stderr_path" get "$resource" -A); then
            if grep -qi forbidden -- "$stderr_path"; then
                fallback+="${fallback:+, }$kind"
                output=$(_kubectl_inventory_list_namespaces "$stderr_path" \
                    "$resource" "${namespaces[@]}") || {
                    unlisted+="${unlisted:+, }$kind"
                    probe_failures+="${probe_failures:+; }$kind: $(_kubectl_inventory_error "$stderr_path")"
                    continue
                }
            else
                unlisted+="${unlisted:+, }$kind"
                probe_failures+="${probe_failures:+; }$kind: $(_kubectl_inventory_error "$stderr_path")"
                continue
            fi
        fi
        local row
        while IFS= read -r row; do
            [[ -z "$row" ]] || rows+="$kind|$row"$'\n'
        done <<< "$output"
    done
    rm -f -- "$stderr_path"
    [[ -z "$fallback" ]] \
        || scope="$namespace_label where cluster-wide listing is forbidden: $fallback"
    if [[ -n "$unlisted" ]]; then
        register_warning "Could not list Kubernetes objects from this toolset ($unlisted): $probe_failures"
        scope+="; could not list: $unlisted"
    fi
    if [[ -z "$rows" ]]; then
        validate_detail '  Kubernetes objects from this toolset: none found (%s)\n' "$scope"
        return 0
    fi
    printf '  Kubernetes objects from this toolset: %s found (%s)\n' \
        "$(grep -c . <<< "$rows")" "$scope"
    printf '%s' "$rows" | _print_kubectl_inventory_table
    printf '  These may belong to an active or uncollected attempt. Find the result directory\n'
    printf '  whose kubernetes/attempts/<RUN>/ matches and use --status, --collect, or --cancel.\n'
    printf '  Delete manually only after confirming the object ownership annotation matches\n'
    printf '  the nonce recorded in that attempt (resources/ or ambiguous-absence/ files).\n'
}

# Kubernetes 1.21 and newer label every namespace kubernetes.io/metadata.name.
_check_kubectl_namespace_label() {
    local namespace="$1" label
    if ! label=$(kubectl_run_observational get namespace "$namespace" \
            -o 'jsonpath={.metadata.labels.kubernetes\.io/metadata\.name}' 2>/dev/null); then
        register_error "Could not read the labels of Kubernetes namespace $namespace"
        return 1
    fi
    [[ "$label" == "$namespace" ]] && return 0
    register_error "Kubernetes namespace $namespace lacks the label kubernetes.io/metadata.name=$namespace, which Kubernetes 1.21 and newer set automatically; use a newer cluster"
    return 1
}

# Print the API server's "<major> <minor>" version numbers.
_kubectl_server_version() {
    local json server major minor
    json=$(kubectl_run_observational version -o json 2>/dev/null) || return 1
    server=$(tr -d ' \t\n' <<< "$json" | sed -n 's/.*"serverVersion":{\([^}]*\)}.*/\1/p')
    major=$(sed -n 's/.*"major":"\([0-9][0-9]*\).*/\1/p' <<< "$server")
    minor=$(sed -n 's/.*"minor":"\([0-9][0-9]*\).*/\1/p' <<< "$server")
    [[ -n "$major" && -n "$minor" ]] || return 1
    printf '%s %s\n' "$major" "$minor"
}

# _kubectl_can_i VERB RESOURCE NAMESPACE
_kubectl_can_i() {
    local allowed
    allowed=$(kubectl_run_observational auth can-i "$1" "$2" -n "$3" 2>/dev/null) || true
    [[ "$allowed" == yes ]]
}

# The advice for clusters that cannot run a batch whose targets span
# namespaces.
_kubectl_sequencer_fallback_hint() {
    printf '%s\n' 'keep each batch in one namespace (it then needs no sequencer or its RBAC), or run one batch per namespace'
}

# Print one line if the server version lacks suspended Jobs (Kubernetes 1.24
# and newer) or cannot be determined.
_kubectl_sequencer_version_problem() {
    local version
    if ! version=$(_kubectl_server_version); then
        printf '%s\n' "could not determine the Kubernetes server version (1.24 or newer is required)"
    elif (( ${version% *} == 1 && ${version#* } < 24 )); then
        printf '%s\n' "Kubernetes server ${version/ /.} is older than 1.24, which suspended Jobs require"
    fi
}

# _kubectl_sequencer_permission_problem NAMESPACE "VERB RESOURCE": print one
# line if the current user lacks that permission in NAMESPACE.
_kubectl_sequencer_permission_problem() {
    local namespace="$1" permission="$2"
    # shellcheck disable=SC2086  # Split "<verb> <resource>".
    _kubectl_can_i $permission "$namespace" \
        || printf '%s\n' "permission to $permission in namespace $namespace"
}

# Fill the array named by $1 with one line per sequencer prerequisite that
# NAMESPACE ($2) lacks: the server version and, in every namespace of a batch
# spanning namespaces, permission to create, get (ownership read-back) and
# delete the sequencer's ServiceAccount (first namespace), Roles and
# RoleBindings, and to create, unsuspend and delete the coordinator Jobs.
# The checks are independent API reads, so each runs concurrently in a
# background subshell that writes its problem line (if any) to a numbered
# file; the files are read in the fixed order of the checks, so the result
# never depends on completion order.
_kubectl_sequencer_prerequisite_problems() {
    local -n found_problems="$1"
    local namespace="$2" answers_dir permission index=0 pid
    local -a pids=()
    found_problems=()
    if ! answers_dir=$(mktemp -d "${TMPDIR:-/tmp}/storage-scale-test-validate-prerequisites.XXXXXX"); then
        found_problems+=("could not create temporary storage for the prerequisite checks")
        return 0
    fi
    _kubectl_sequencer_version_problem > "$answers_dir/0" &
    pids+=("$!")
    for permission in "create serviceaccounts" "get serviceaccounts" \
            "delete serviceaccounts" \
            "create roles.rbac.authorization.k8s.io" \
            "get roles.rbac.authorization.k8s.io" \
            "delete roles.rbac.authorization.k8s.io" \
            "create rolebindings.rbac.authorization.k8s.io" \
            "get rolebindings.rbac.authorization.k8s.io" \
            "delete rolebindings.rbac.authorization.k8s.io" \
            "patch jobs.batch" "create jobs.batch" "delete jobs.batch"; do
        index=$((index + 1))
        _kubectl_sequencer_permission_problem "$namespace" "$permission" \
            > "$answers_dir/$index" &
        pids+=("$!")
    done
    for pid in "${pids[@]}"; do
        _kubectl_validate_wait "$pid" || true
    done
    _kubectl_validate_read_problems found_problems "$answers_dir" "${#pids[@]}"
    rm -rf -- "$answers_dir"
}

# _kubectl_validate_read_problems ARRAY DIR COUNT: append the lines of the
# files DIR/0 .. DIR/COUNT-1, in that order, to the array named ARRAY.
_kubectl_validate_read_problems() {
    local -n collected_problems="$1"
    local answers_dir="$2" count="$3" index problem
    for ((index = 0; index < count; index++)); do
        while IFS= read -r problem; do
            [[ -z "$problem" ]] || collected_problems+=("$problem")
        done < "$answers_dir/$index"
    done
}

# Concurrent validation. Independent checks run as background subshells that
# the main script waits for by PID. INT and TERM are held in this shell and
# forwarded as TERM to every PID being waited for (a background subshell
# ignores INT unless it traps it); each subshell lets its own cleanup finish
# first (the sequencer Job and the runtime Job are always deleted), and the
# script then exits with 130 or 143.
_VALIDATE_PIDS=()
_VALIDATE_INTERRUPTED=""
_VALIDATE_SAVED_TRAPS=""
# What the Kubernetes summary line names: the context, namespace and claims
# (set before the concurrent checks) and the matching Ready worker nodes (set
# by the node check in its branch).
_VALIDATE_KUBECTL_TARGET=""
_VALIDATE_KUBECTL_NODES=""

# Trap handler: remember the status to exit with and pass the signal on.
# shellcheck disable=SC2317,SC2329  # Called indirectly via trap
_kubectl_validate_forward() {
    local pid
    _VALIDATE_INTERRUPTED="${_VALIDATE_INTERRUPTED:-$1}"
    for pid in ${_VALIDATE_PIDS[@]+"${_VALIDATE_PIDS[@]}"}; do
        kill -TERM "$pid" 2>/dev/null || true
    done
}

_kubectl_validate_hold_signals() {
    _VALIDATE_PIDS=()
    _VALIDATE_INTERRUPTED=""
    _VALIDATE_SAVED_TRAPS="$(trap -p INT)"$'\n'"$(trap -p TERM)"
    trap '_kubectl_validate_forward 130' INT
    trap '_kubectl_validate_forward 143' TERM
}

_kubectl_validate_release_signals() {
    trap - INT TERM
    eval "$_VALIDATE_SAVED_TRAPS"
}

# Leave now, with the status of a held INT or TERM, if one has arrived.
_kubectl_validate_exit_if_interrupted() {
    [[ -z "$_VALIDATE_INTERRUPTED" ]] || exit "$_VALIDATE_INTERRUPTED"
}

# Remember PID so a signal is forwarded to it (at once if one already came).
_kubectl_validate_track() {
    _VALIDATE_PIDS+=("$1")
    [[ -z "$_VALIDATE_INTERRUPTED" ]] || kill -TERM "$1" 2>/dev/null || true
}

# Wait for PID and return its status. A trapped signal ends `wait` early while
# the child is still cleaning up, so wait again until it is gone.
_kubectl_validate_wait() {
    local pid="$1" rc=0
    wait "$pid" || rc=$?
    while [[ -n "$_VALIDATE_INTERRUPTED" ]] && kill -0 "$pid" 2>/dev/null; do
        wait "$pid" || rc=$?
    done
    return "$rc"
}

# _kubectl_validate_capture OUTPUT COMMAND [ARG...]: run COMMAND in the
# background with stdout and stderr in the file OUTPUT, wait for it, and
# return its status. A signal is forwarded to it.
_kubectl_validate_capture() {
    local output_path="$1" pid rc=0
    shift
    "$@" > "$output_path" 2>&1 &
    pid=$!
    _kubectl_validate_track "$pid"
    _kubectl_validate_wait "$pid" || rc=$?
    _VALIDATE_PIDS=()
    return "$rc"
}

# Run CHECK NAMESPACE (a sequencer probe or image check, which cleans up its
# temporary objects) with its output captured in sequencer_check_output and its
# status in sequencer_check_rc. An INT or TERM is forwarded to the check and
# held until it has cleaned up, then this shell exits with 130 or 143.
_run_sequencer_check() {
    local check="$1" namespace="$2" output_path
    sequencer_check_rc=0
    sequencer_check_output=""
    _kubectl_validate_exit_if_interrupted
    if ! output_path=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-validate-sequencer.XXXXXX"); then
        sequencer_check_rc=1
        sequencer_check_output="could not create temporary storage for the check output"
        return 0
    fi
    KUBECTL_NAMESPACE="$namespace" _kubectl_validate_capture "$output_path" "$check" \
        || sequencer_check_rc=$?
    sequencer_check_output=$(<"$output_path")
    rm -f -- "$output_path"
    [[ "$sequencer_check_rc" -eq 0 ]] || sequencer_check_output=$(
        _validate_kubectl_failure_text "$sequencer_check_output" "sequencer-$namespace")
    _kubectl_validate_exit_if_interrupted
    return 0
}

# A batch whose targets span namespaces runs one segment per namespace,
# started in order by the in-cluster sequencer. That needs its image, suspended
# Jobs (Kubernetes 1.24 and newer) and the RBAC listed above, in env.sh's
# namespace for these checks. A batch in one namespace needs none of them, so
# by default every problem is only a warning; with --sequencer the full probe
# also runs and every problem is an error.
check_kubectl_sequencer_prerequisites() {
    local namespace="$1"
    local -a problems=()
    validate_detail '  Kubernetes sequencer (batches spanning namespaces): checking image %s in namespace %s\n' \
        "$(kubectl_sequencer_image)" "$namespace"
    _kubectl_sequencer_prerequisite_problems problems "$namespace"
    _kubectl_validate_exit_if_interrupted
    if [[ -n "$VALIDATE_SEQUENCER" ]]; then
        _check_kubectl_sequencer_asked "$namespace" ${problems[@]+"${problems[@]}"}
        return 0
    fi
    local passed=1
    if [[ ${#problems[@]} -eq 0 ]]; then
        validate_detail '  Kubernetes sequencer prerequisites: server version and sequencer permissions passed\n'
    else
        register_warning "$(_kubectl_sequencer_problem_message "${problems[@]}")"
        passed=""
    fi
    check_kubectl_sequencer_image "$namespace" || passed=""
    validate_detail '  Kubernetes sequencer checks only warn: they matter only for batches spanning namespaces; validate_env.sh --sequencer runs the full probe and treats problems as errors\n'
    [[ -z "$passed" ]] \
        || register_summary "Kubernetes sequencer (only for batches spanning namespaces): OK"
    return 0
}

# The prerequisite problems as one multi-line message with the fallback.
_kubectl_sequencer_problem_message() {
    printf 'Kubernetes sequencer prerequisites (needed only for batches spanning namespaces) are missing:'
    printf '\n  - %s' "$@"
    printf '\n  Fallback: %s' "$(_kubectl_sequencer_fallback_hint)"
}

# --sequencer: problems are errors; with none, the probe covers the image.
_check_kubectl_sequencer_asked() {
    local namespace="$1"
    shift
    if [[ $# -eq 0 ]]; then
        validate_detail '  Kubernetes sequencer prerequisites: server version and sequencer permissions passed\n'
        check_kubectl_sequencer_probe "$namespace"
    else
        register_error "$(_kubectl_sequencer_problem_message "$@")"
    fi
}

# Run the sequencer's image (kubectl_sequencer_image) once in NAMESPACE with no
# API call: it must pull and provide sh, kubectl, grep and sleep. A failure is
# a warning naming the reason and the mirror setting, and the status is 1.
check_kubectl_sequencer_image() {
    local namespace="$1" image
    image=$(kubectl_sequencer_image)
    _run_sequencer_check kubectl_validate_sequencer_image "$namespace"
    if [[ "$sequencer_check_rc" -eq 0 ]]; then
        validate_detail '  Kubernetes sequencer image %s: pulls and provides sh, kubectl, grep, sleep\n' \
            "$image"
        return 0
    fi
    register_warning "$(printf 'Kubernetes sequencer image %s (needed only for batches spanning namespaces) failed its check in namespace %s%s' \
            "$image" "$namespace" "${sequencer_check_output:+: $sequencer_check_output}"
        printf '\n  Hint: set KUBECTL_SEQUENCER_IMAGE to a digest-pinned image the cluster can pull, such as a reachable mirror; a batch in one namespace needs no sequencer')"
    return 1
}

# Run the full sequencer probe in NAMESPACE: the image must pull, provide sh,
# kubectl, grep and sleep, and reach the API server with its own token. The
# probe's temporary objects are always deleted. A failure is an error.
check_kubectl_sequencer_probe() {
    local namespace="$1"
    _run_sequencer_check kubectl_validate_sequencer_probe "$namespace"
    if [[ "$sequencer_check_rc" -eq 0 ]]; then
        validate_detail '  Kubernetes sequencer probe in namespace %s: image, kubectl, token and API access passed\n' \
            "$namespace"
        register_summary "Kubernetes sequencer (full probe): OK"
        return 0
    fi
    register_error "$(printf 'Kubernetes sequencer probe (needed only for batches spanning namespaces) failed in namespace %s%s' \
            "$namespace" "${sequencer_check_output:+: $sequencer_check_output}"
        printf '\n  Fallback: %s' "$(_kubectl_sequencer_fallback_hint)")"
    return 0
}

# Validate the cluster identity of the target's namespace and every claim
# (Bound, ReadWriteMany, Filesystem mode, and the PV it is bound to), and the
# namespace label.
_check_kubectl_claim_identities() {
    local namespace="$1" identity messages messages_path rc=0 line
    local row_namespace namespace_uid pv pv_uid claim pvc_uid extra
    messages_path=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-validate-identity.XXXXXX") || {
        register_error "Could not create temporary storage for Kubernetes identity validation"
        return 1
    }
    # Rows come only from stdout; kubectl may print advisories (such as a
    # client/server version-skew warning) on stderr, which must not be parsed.
    identity=$(kubectl_validate_cluster_identity 2> "$messages_path") || rc=$?
    messages=$(<"$messages_path")
    rm -f -- "$messages_path"
    if [[ "$rc" -ne 0 ]]; then
        register_error "Kubernetes API or storage validation failed for namespace $namespace: ${messages:-$identity}"
        return 1
    fi
    while IFS= read -r line; do
        [[ "$line" != Warning:* ]] || register_warning "kubectl: $line"
    done <<< "$messages"
    while IFS=$'\t' read -r row_namespace namespace_uid pv pv_uid claim pvc_uid extra; do
        [[ -n "$row_namespace" && -n "$pvc_uid" && -z "$extra" ]] || continue
        validate_detail '  Kubernetes claim %s/%s: Bound to PV %s (namespace UID %s, PV UID %s, PVC UID %s)\n' \
            "$row_namespace" "$claim" "$pv" "$namespace_uid" "$pv_uid" "$pvc_uid"
    done <<< "$identity"
    _check_kubectl_namespace_label "$namespace"
}

check_kubectl_filesystem_prerequisites() {
    local kubectl_functions="${SCALE_TEST_BASE}/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    local claims_library="${SCALE_TEST_BASE}/lib/_kubectl_claims.sh" path
    for path in "$kubectl_functions" "$claims_library"; do
        if [[ ! -f "$path" ]]; then
            register_error "Kubernetes filesystem support files are missing: $path"
            return 1
        fi
    done
    # shellcheck disable=SC1090  # Checked-in filesystem-sweep validation helpers.
    source "$kubectl_functions"
    # shellcheck disable=SC1090  # Checked-in claim-qualified TEST_DIRS helpers.
    source "$claims_library"
    local output namespace claims settings_rc=0
    # Report the roots and the workload settings together before stopping.
    if ! output=$(kubectl_validate_test_dirs 2>&1); then
        register_error "$output"
        settings_rc=1
    fi
    if ! output=$(kubectl_validate_runtime_configuration 2>&1); then
        register_error "$output"
        settings_rc=1
    fi
    [[ "$settings_rc" -eq 0 ]] || return 1
    if ! namespace=$(kubectl_test_dirs_namespace) || ! claims=$(kubectl_test_dirs_claims); then
        register_error "Could not resolve the Kubernetes namespace and claims of TEST_DIRS"
        return 1
    fi
    claims="$(paste -sd, - <<< "$claims")"
    validate_detail '  Kubernetes target: namespace %s, claim(s) %s\n' "$namespace" "$claims"
    if ! command -v kubectl >/dev/null 2>&1; then
        register_error "kubectl is required for EXECUTION_SUBSTRATE=kubectl but is not on PATH"
        return 1
    fi
    if ! command -v flock >/dev/null 2>&1; then
        register_error "flock is required for kubectl lifecycle locking (on macOS: brew install flock)"
        return 1
    fi
    local current_context
    if ! current_context=$(kubectl config current-context 2>&1) \
            || [[ -z "$current_context" ]]; then
        register_error "Could not determine the active kubectl context: $current_context"
        return 1
    fi
    validate_detail '  Kubernetes context: %s\n' "$current_context"
    _VALIDATE_KUBECTL_TARGET="context $current_context, namespace $namespace, claim"
    [[ "$claims" != *,* ]] || _VALIDATE_KUBECTL_TARGET+="s"
    _VALIDATE_KUBECTL_TARGET+=" $claims"
    _check_kubectl_claim_identities "$namespace" || return 1
    _check_kubectl_target_concurrently "$namespace"
}

# Count and describe the Ready worker nodes the selector matches.
_check_kubectl_worker_nodes() {
    local nodes_dir nodes_path node_output node_count node_arch node_names
    nodes_dir=$(mktemp -d "${TMPDIR:-/tmp}/storage-scale-test-validate-nodes.XXXXXX") || {
        register_error "Could not create temporary storage for Kubernetes node validation"
        return 1
    }
    nodes_path="$nodes_dir/nodes.tsv"
    if ! node_output=$(kubectl_discover_candidate_nodes \
            "$KUBECTL_NODE_SELECTOR" "$nodes_path" 2>&1); then
        register_error "Kubernetes worker selector validation failed: $node_output"
        rm -rf -- "$nodes_dir"
        return 1
    fi
    node_count=$(( $(wc -l < "$nodes_path") ))
    node_arch=$(cut -f3 "$nodes_path" | sort -u | paste -sd, -)
    node_names=$(cut -f1 "$nodes_path" | paste -sd, -)
    validate_detail '  Kubernetes worker selector: %s (%s Ready %s node(s): %s)\n' \
        "$KUBECTL_NODE_SELECTOR" "$node_count" "$node_arch" "$node_names"
    _VALIDATE_KUBECTL_NODES="$node_count Ready $node_arch node"
    [[ "$node_count" -eq 1 ]] || _VALIDATE_KUBECTL_NODES+="s"
    rm -rf -- "$nodes_dir"
}

# Start of a concurrent branch, in the background subshell that runs it: its
# registrations go to PREFIX.errors, PREFIX.warnings and PREFIX.summary (the
# same NUL-terminated format as the main script's files) for
# _kubectl_validate_replay_branch and _kubectl_validate_replay_summary, and
# the branch holds INT and TERM itself, replacing the main script's handling.
# Its stdout and stderr are already redirected to PREFIX.out and PREFIX.err.
_kubectl_validate_branch_setup() {
    ERROR_FILE="$1.errors"
    WARNING_FILE="$1.warnings"
    SUMMARY_FILE="$1.summary"
    # Temporary files live in the branch directory, so they are removed with
    # it even when the branch is killed.
    ! mkdir -- "$1.tmp" || export TMPDIR="$1.tmp"
    _kubectl_validate_hold_signals
}

# Branch A of the concurrent checks: the sequencer checks.
_check_kubectl_sequencer_branch() {
    _kubectl_validate_branch_setup "$1"
    check_kubectl_sequencer_prerequisites "$2"
}

# Branch B of the concurrent checks: the worker nodes, then the runtime
# validation Job, which needs the nodes to be valid.
_check_kubectl_cluster_branch() {
    local namespace="$2"
    _kubectl_validate_branch_setup "$1"
    _check_kubectl_worker_nodes || return 1
    _kubectl_validate_exit_if_interrupted
    check_kubectl_target_runtime "$namespace" || return 1
    register_summary "Kubernetes: $_VALIDATE_KUBECTL_TARGET, $_VALIDATE_KUBECTL_NODES: OK"
}

# _kubectl_validate_replay_records FUNCTION FILE: call FUNCTION with each
# NUL-terminated record of FILE (the format register_error writes), in order.
_kubectl_validate_replay_records() {
    local record
    [[ -f "$2" ]] || return 0
    while IFS= read -r -d '' record; do
        "$1" "$record"
    done < "$2"
}

# Register the summary lines a branch produced, in the order it produced them.
_kubectl_validate_replay_summary() {
    _kubectl_validate_replay_records register_summary "$1.summary"
}

# Print the stdout and stderr a branch buffered, then register its errors and
# warnings, in the order it produced them.
_kubectl_validate_replay_branch() {
    cat -- "$1.out"
    cat -- "$1.err" >&2
    _kubectl_validate_replay_records register_error "$1.errors"
    _kubectl_validate_replay_records register_warning "$1.warnings"
}

# The checks that follow the claim identities are independent, so two branches
# run concurrently: (A) the sequencer checks, (B) the worker nodes and the
# runtime Job. The owned-object inventory runs first, alone, so it never lists
# the temporary Jobs these checks create. Each part buffers its output and
# registrations; they are replayed here in the fixed order A, inventory, B, so
# the output and the registered errors and warnings never depend on which
# branch finishes first. The summary lines are replayed B first, so the
# Kubernetes line precedes the sequencer's. A branch that fails without
# registering an error (killed, say) gets one. Returns branch B's status.
_check_kubectl_target_concurrently() {
    local namespace="$1" branch_dir sequencer_pid cluster_pid sequencer_rc=0 cluster_rc=0
    branch_dir=$(mktemp -d "${TMPDIR:-/tmp}/storage-scale-test-validate-branches.XXXXXX") || {
        register_error "Could not create temporary storage for the Kubernetes validation checks"
        return 1
    }
    _kubectl_validate_hold_signals
    (
        _kubectl_validate_branch_setup "$branch_dir/inventory"
        check_kubectl_owned_resources "$namespace"
    ) > "$branch_dir/inventory.out" 2> "$branch_dir/inventory.err"
    if [[ -n "$_VALIDATE_INTERRUPTED" ]]; then
        rm -rf -- "$branch_dir"
        exit "$_VALIDATE_INTERRUPTED"
    fi
    _check_kubectl_sequencer_branch "$branch_dir/sequencer" "$namespace" \
        > "$branch_dir/sequencer.out" 2> "$branch_dir/sequencer.err" &
    sequencer_pid=$!
    _kubectl_validate_track "$sequencer_pid"
    _check_kubectl_cluster_branch "$branch_dir/cluster" "$namespace" \
        > "$branch_dir/cluster.out" 2> "$branch_dir/cluster.err" &
    cluster_pid=$!
    _kubectl_validate_track "$cluster_pid"
    _kubectl_validate_wait "$sequencer_pid" || sequencer_rc=$?
    _kubectl_validate_wait "$cluster_pid" || cluster_rc=$?
    _VALIDATE_PIDS=()
    if [[ -n "$_VALIDATE_INTERRUPTED" ]]; then
        rm -rf -- "$branch_dir"
        exit "$_VALIDATE_INTERRUPTED"
    fi
    _kubectl_validate_replay_summary "$branch_dir/cluster"
    _kubectl_validate_replay_summary "$branch_dir/sequencer"
    _kubectl_validate_replay_branch "$branch_dir/sequencer"
    [[ "$sequencer_rc" -eq 0 ]] \
        || register_error "The Kubernetes sequencer checks ended unexpectedly (status $sequencer_rc)"
    _kubectl_validate_replay_branch "$branch_dir/inventory"
    _kubectl_validate_replay_branch "$branch_dir/cluster"
    [[ "$cluster_rc" -eq 0 \
        || $(_validate_record_count "$branch_dir/cluster.errors") -gt 0 ]] \
        || register_error "The Kubernetes worker and runtime checks ended unexpectedly (status $cluster_rc)"
    rm -rf -- "$branch_dir"
    _kubectl_validate_release_signals
    return "$cluster_rc"
}

# _validate_kubectl_failure_text OUTPUT LABEL: print a Kubernetes check's
# failure OUTPUT condensed for an error or warning. "Warning:" lines (kubectl
# advisories such as a client/server version skew, retries) are registered as
# warnings instead. The Pod and Job dump, from the first "===== Kubernetes
# validation diagnostic:" line on, stays inline with --verbose; otherwise it is
# saved under LOGS_DIR (LABEL names the file) and only its path is printed.
_validate_kubectl_failure_text() {
    local output="$1" label="$2" line text="" dump="" path=""
    while IFS= read -r line; do
        if [[ -z "$dump" && "$line" != "===== Kubernetes validation diagnostic: "* ]]; then
            if [[ "$line" == Warning:* ]]; then
                register_warning "kubectl: $line"
            elif [[ -n "$line" ]]; then
                text+="${text:+$'\n'}${line#Error: }"
            fi
        else
            dump+="$line"$'\n'
        fi
    done <<< "$output"
    [[ -n "$dump" ]] || { printf '%s' "$text"; return 0; }
    if [[ -z "${VALIDATE_VERBOSE:-}" && -d "${LOGS_DIR:-}" ]] \
        && path=$(mktemp "$LOGS_DIR/validate_env-kubectl-$label-$(date +%Y%m%d-%H%M%S).XXXXXX") \
        && printf '%s' "$dump" > "$path"; then
        printf '%s\nFull Pod and Job diagnostics: %s (or rerun with --verbose)' "$text" "$path"
    else
        [[ -z "$path" ]] || rm -f -- "$path"
        printf '%s\n\n%s' "$text" "${dump%$'\n'}"
    fi
}

# Run the deadline-bounded runtime validation Job: one Pod in the target's
# namespace mounting every claim, with path and write checks per root.
check_kubectl_target_runtime() {
    local namespace="$1" runtime_output output_path runtime_rc=0
    if ! output_path=$(mktemp "${TMPDIR:-/tmp}/storage-scale-test-validate-runtime.XXXXXX"); then
        register_error "Could not create temporary storage for the Kubernetes runtime validation in namespace $namespace"
        return 1
    fi
    _kubectl_validate_capture "$output_path" kubectl_validate_runtime_pod || runtime_rc=$?
    runtime_output=$(<"$output_path")
    rm -f -- "$output_path"
    _kubectl_validate_exit_if_interrupted
    if [[ "$runtime_rc" -ne 0 ]]; then
        runtime_output=$(_validate_kubectl_failure_text "$runtime_output" "runtime-$namespace")
        register_error "Kubernetes workload image or PVC runtime validation failed in namespace $namespace${runtime_output:+: $runtime_output}"
        return 1
    fi
    validate_detail '  Kubernetes runtime in namespace %s: image, UID/GID, required tools, and read/write on every root passed\n' \
        "$namespace"
}

# Print "script" or "mismatch" when binary $1 cannot be shown to match the
# file(1) architecture $2. -b omits the path, which may name an architecture.
binary_arch_problem() {
    local type
    type=$(file -b -L "$1")
    if [[ "$type" == *script* ]]; then
        echo script
    elif [[ "$type" != *"$2"* ]]; then
        echo mismatch
    fi
}

# Determine architecture & check for binaries
check_arch_and_binaries() {
    # Since we support SSH mode, we can no longer rely on an assumption that the invoking
    # host and the target compute host are the same architecture.  So here, we have to
    # probe the architecture of one compute node.

    # The probes (validate_probe_scriptlet) already reported each node's
    # architecture: srun's on Slurm (as before), one SSH session's otherwise.
    local probe_label=srun
    if [[ "${slurm_rc:-1}" -eq 0 ]]; then
        remote_arch=$PROBE_srun_ARCH
        if [[ -n "$PROBE_sbatch_ARCH" && "$PROBE_sbatch_ARCH" != "$remote_arch" ]]; then
            register_warning "sbatch and srun nodes report different architectures (${PROBE_sbatch_ARCH} and ${remote_arch:-none})"
        fi
    else
        probe_label=ssh
        run_ssh_validation_probe
        remote_arch=$PROBE_ssh_ARCH
    fi
    if [[ -z "$remote_arch" ]]; then
        register_error "${probe_label}: the validation probe reported no architecture"
        return
    fi

    if [[ "$remote_arch" != "${client_arch:?}" ]]; then
        register_error "Probed architecture (${remote_arch}) != env.sh client_arch (${client_arch})!"
    fi

    if [[ "$remote_arch" != "x86_64" && "$remote_arch" != "aarch64" ]]; then
        register_error "Unsupported remote architecture: ${remote_arch}"
        return
    fi

    # Map uname architecture to file command architecture string
    # The file command returns "aarch64" for ARM binaries
    case "$remote_arch" in
        x86_64)  remote_arch_pattern="x86-64"  ;;
        aarch64) remote_arch_pattern="aarch64" ;;
    esac

    # Collect binaries that run on remote hosts
    # Required binaries cause errors if missing; optional binaries just warn
    remote_binaries=()
    optional_binaries=()

    if [ -n "$FS_ENABLED" ]; then
        # elbencho is required for FS tests
        remote_binaries+=( "${ELBENCHO-}" )
    fi
    if [ -n "$OBJ_ENABLED" ]; then
        # warp and s3test are required for object storage tests
        remote_binaries+=( "${WARP-}" "${S3TEST-}" )
    fi

    # Validate required remote executables
    local any_bin_missing=
    for binary in "${remote_binaries[@]}"; do
        if [[ -z "$binary" ]]; then
            register_error "Required binary path not set"
            any_bin_missing=1
            continue
        fi

        if [[ ! -x "$binary" ]]; then
            register_error "Binary not executable: ${binary}"
            any_bin_missing=1
            continue
        fi

        case $(binary_arch_problem "$binary" "$remote_arch_pattern") in
            script) register_warning "Cannot check the architecture of script ${binary}" ;;
            mismatch) register_error "Binary ${binary} not compiled for ${remote_arch}" ;;
        esac
    done
    if [[ -n "$any_bin_missing" ]]; then
        register_error "Missing binaries. Ensure you extracted a complete prepared tarball."
    fi

    # Validate optional remote executables (warn only, don't fail)
    for binary in "${optional_binaries[@]}"; do
        if [[ -z "$binary" ]]; then
            continue  # Not configured, skip silently
        fi

        if [[ ! -x "$binary" ]]; then
            register_warning "$(printf '%s\n%s' "Optional binary not found (some tests may be unavailable):" "$binary")"
            continue
        fi

        if [[ $(binary_arch_problem "$binary" "$remote_arch_pattern") == mismatch ]]; then
            register_warning "$(printf '%s\n%s' "Optional binary not compiled for ${remote_arch}:" "$binary")"
        fi
    done
}

# The roots the probes check: every nonempty TEST_DIRS key when filesystem
# tests are enabled (not on kubectl), in C-locale order. Results come back by
# 1-based index into this list, never by path.
validate_probe_roots() {
    VALIDATE_PROBE_ROOTS=()
    [[ -n "${FS_ENABLED:-}" && -z "${KUBECTL_ENABLED:-}" ]] || return 0
    local root
    for root in "${!TEST_DIRS[@]}"; do
        [[ -z "$root" ]] || VALIDATE_PROBE_ROOTS+=("$root")
    done
    [[ ${#VALIDATE_PROBE_ROOTS[@]} -eq 0 ]] \
        || mapfile -t VALIDATE_PROBE_ROOTS < <(printf '%s\n' "${VALIDATE_PROBE_ROOTS[@]}" | LC_ALL=C sort)
}

# The probe: one POSIX sh scriptlet run once per transport (an sbatch job with
# the production options, an srun step with the minimal ones, or one SSH
# session) instead of one allocation or session per check. Arguments: $1 is
# the protocol version (1), $2... the roots. Every result line starts with
# "VE1<TAB>", so the parser ignores prolog and module noise; a root's result
# is keyed by its index. Each remote file operation is bounded by `timeout 30`
# where available, so a hung mount marks that root "timeout". It always ends
# with an "end" line and exits 0.
validate_probe_scriptlet() {
    cat <<'EOF'
emit() { printf 'VE1'; for field in "$@"; do printf '\t%s' "$field"; done; printf '\n'; }
if command -v timeout >/dev/null 2>&1; then
    bounded() { timeout 30 "$@"; }
else
    bounded() { "$@"; }
fi
probe_root() {
    bounded test -d "$1"; rc=$?
    [ "$rc" -ne 124 ] || { echo timeout; return; }
    [ "$rc" -eq 0 ] || { echo notdir; return; }
    device=$(bounded stat -c %d -- "$1"); rc=$?
    [ "$rc" -ne 124 ] || { echo timeout; return; }
    [ "$rc" -eq 0 ] && [ -n "$root_device" ] || { echo statfail; return; }
    [ "$device" != "$root_device" ] || { echo samedev; return; }
    bounded test -w "$1"; rc=$?
    [ "$rc" -ne 124 ] || { echo timeout; return; }
    [ "$rc" -eq 0 ] || { echo nowrite; return; }
    echo ok
}
emit begin "$(hostname -s 2>/dev/null || hostname)"
emit echo ABCDEF
emit arch "$(uname -m)"
shift
root_device=$(bounded stat -c %d -- /) || root_device=
index=0
for dir in "$@"; do
    index=$((index + 1))
    emit fs "$index" "$(probe_root "$dir")"
done
emit end 0
exit 0
EOF
}

# parse_validate_probe LABEL TEXT: set PROBE_<LABEL>_{HOST,ECHO,ARCH,END} and
# the array PROBE_<LABEL>_FS (indexed from 1) from a probe's output. Lines
# without the VE1 prefix are ignored; the first value of each field wins.
parse_validate_probe() {
    local label="$1" prefix tag first second _
    declare -g "PROBE_${label}_HOST=" "PROBE_${label}_ECHO=" \
        "PROBE_${label}_ARCH=" "PROBE_${label}_END="
    declare -ga "PROBE_${label}_FS=()"
    local -n probe_host="PROBE_${label}_HOST" probe_echo="PROBE_${label}_ECHO"
    local -n probe_arch="PROBE_${label}_ARCH" probe_end="PROBE_${label}_END"
    local -n probe_fs="PROBE_${label}_FS"
    while IFS=$'\t' read -r prefix tag first second _; do
        [[ "$prefix" == VE1 ]] || continue
        case "$tag" in
            begin) [[ -n "$probe_host" ]] || probe_host=$first ;;
            echo) [[ -n "$probe_echo" ]] || probe_echo=$first ;;
            arch) [[ -n "$probe_arch" ]] || probe_arch=$first ;;
            end) probe_end=1 ;;
            fs)
                [[ "$first" =~ ^[1-9][0-9]*$ && -z "${probe_fs[first]:-}" ]] \
                    && probe_fs[first]=$second
                ;;
        esac
    done <<< "$2"
}

# _validate_probe_missing_roots LABEL: the roots the probe gave no result for.
_validate_probe_missing_roots() {
    # shellcheck disable=SC2178  # A nameref to the array, not a string.
    local -n probe_fs="PROBE_${1}_FS"
    local index missing=()
    for index in "${!VALIDATE_PROBE_ROOTS[@]}"; do
        [[ -n "${probe_fs[index + 1]:-}" ]] || missing+=("${VALIDATE_PROBE_ROOTS[index]}")
    done
    _validate_join_list ${missing[@]+"${missing[@]}"}
}

# _validate_probe_complete LABEL: register an error when the probe's output
# has no end marker (its job or step ended early), naming the roots it never
# reported; return 1 then.
_validate_probe_complete() {
    local -n probe_end="PROBE_${1}_END"
    [[ -z "$probe_end" ]] || return 0
    local missing
    missing=$(_validate_probe_missing_roots "$1")
    register_error "${1}: probe output incomplete (no end marker)${missing:+; results missing for roots: $missing}"
    return 1
}

# check_fs_probe_result LABEL ROOT: register the error, if any, for ROOT's
# result in the LABEL probe. A root with no result was reported as missing by
# _validate_probe_complete.
check_fs_probe_result() {
    local label="$1" root="$2" index status host
    local -n probe_fs="PROBE_${label}_FS" probe_host="PROBE_${label}_HOST"
    for index in "${!VALIDATE_PROBE_ROOTS[@]}"; do
        [[ "${VALIDATE_PROBE_ROOTS[index]}" != "$root" ]] || break
    done
    [[ "${VALIDATE_PROBE_ROOTS[index]:-}" == "$root" ]] || return 0
    status=${probe_fs[index + 1]:-}
    host=${probe_host:-unknown}
    case "$status" in
        ok|'') ;;
        samedev) register_error "${label}: ${root} is not on a filesystem distinct from / on compute node ${host}" ;;
        notdir) register_error "${label}: ${root} is not on a filesystem distinct from / on compute node ${host} (it is not a directory)" ;;
        statfail) register_error "${label}: ${root} is not on a filesystem distinct from / on compute node ${host} (stat failed)" ;;
        nowrite) register_error "${label}: ${root} is not writable on compute node ${host}" ;;
        timeout) register_error "${label}: ${root} did not respond within 30 s on compute node ${host}" ;;
        *) register_error "${label}: ${root}: unexpected probe result '${status}' on compute node ${host}" ;;
    esac
}

# Run the probe once over SSH (one session on one host): the architecture and
# every root. The SSH transport self-tests (check_ssh) stay separate.
run_ssh_validation_probe() {
    [[ -z "${_VALIDATE_SSH_PROBE_DONE:-}" ]] || return "$_VALIDATE_SSH_PROBE_RC"
    _VALIDATE_SSH_PROBE_DONE=1
    local output
    output=$(check_ssh_with "$(validate_probe_scriptlet)" 1 ${VALIDATE_PROBE_ROOTS[@]+"${VALIDATE_PROBE_ROOTS[@]}"})
    _VALIDATE_SSH_PROBE_RC=$?
    parse_validate_probe ssh "$output"
    if [[ "$_VALIDATE_SSH_PROBE_RC" -eq 0 ]]; then
        _validate_probe_complete ssh || _VALIDATE_SSH_PROBE_RC=1
    fi
    return "$_VALIDATE_SSH_PROBE_RC"
}

check_sbatch_with() {
    local scriptlet="$1"
    shift
    local args=("$@")

    temp_job=$(mktemp)
    trap '[[ -n "${temp_job:-}" ]] && rm -f "$temp_job"' EXIT

    printf '%s\n%s\n' "#!/bin/bash" "$scriptlet" > "$temp_job"
    chmod +x "$temp_job"

    return_val=1

    # Build base sbatch command array (handles SBATCH_OPTIONS + SLURM_EXTRA_ARGS)
    local -a sbatch_cmd
    build_sbatch_cmd sbatch_cmd
    local job_name
    job_name=$(make_sbatch_job_name "validate-env")
    if ! sbatch_output=$(
        "${sbatch_cmd[@]}" -N1 --job-name="$job_name" \
            "$temp_job" "${args[@]}" 2>&1
    ); then
        register_error "Failed to execute sbatch test"
        register_error "Command: ${sbatch_cmd[*]} -N1 --job-name=$job_name $temp_job ${args[*]} 2>&1"
        register_error "Output: $sbatch_output"
    else
        if [[ "$sbatch_output" =~ Submitted\ batch\ job\ ([0-9]+) ]]; then
            job_id="${BASH_REMATCH[1]}"
            while scontrol show job "$job_id" \
                    | grep -q "JobState=RUNNING\|JobState=PENDING"; do
                sleep 1
            done
            if grep -q "ExitCode=0:0" <(scontrol show job "$job_id"); then
                return_val=0
            fi
            # The poll ends at COMPLETING too, before the output may be
            # complete; wait briefly for the probe's end marker.
            local _
            for _ in 1 2 3 4 5; do
                ! grep -q $'^VE1\tend' "slurm-${job_id}.out" 2>/dev/null || break
                sleep 1
            done
            cat "slurm-${job_id}.out"
            rm -f "slurm-${job_id}.out"
        fi
    fi
    rm -f "$temp_job"
    trap - EXIT
    return $return_val
}

check_srun_with() {
    local args=("$@")

    # Scale-test srun with a minimal option set (see build_srun_validate_scale_cmd in
    # env_functions.sh): not the full SRUN_OPTIONS (nodelist, exclusive, cpus-per-task),
    # but still includes GPU GRES when client_type=gpu (required on some GPU-only
    # partitions). That full combination breaks some heterogeneous partitions for
    # interactive steps (-N1). Production runs use sbatch with full options; this
    # only checks that srun can run.
    local -a srun_cmd
    build_srun_validate_scale_cmd srun_cmd || return 1

    # Capture stdout and stderr separately to avoid Slurm job messages contaminating command output
    local temp_stderr temp_stdout
    temp_stderr=$(mktemp "${TMPDIR:-/tmp}/srun_stderr.XXXXXX")
    temp_stdout=$(mktemp "${TMPDIR:-/tmp}/srun_stdout.XXXXXX")

    # Function will clean up files at the end

    # shellcheck disable=SC2016
    # Remove timeout as Slurm jobs may take time to allocate, but the actual command is fast
    # -n1: one task (one line of output for echo/uname); do not use -N1 here.
    "${srun_cmd[@]}" -n1 sh -c "${args[@]}" >"$temp_stdout" 2>"$temp_stderr"
    srun_status=$?

    srun_output=""
    srun_stderr=""

    # Safely read output files
    [[ -f "$temp_stdout" ]] && srun_output=$(cat "$temp_stdout")
    [[ -f "$temp_stderr" ]] && srun_stderr=$(cat "$temp_stderr")

    if [[ $srun_status != 0 ]]; then
        register_error "Failed to execute srun test (validate scale: minimal options)"
        register_error "Command: ${srun_cmd[*]} -n1 sh -c ${args[*]}"
        register_error "Stdout: $srun_output"
        register_error "Stderr: $srun_stderr"
        register_error "Exit code: $srun_status"
    fi

    # Clean up temp files
    [[ -n "${temp_stderr:-}" && -f "$temp_stderr" ]] && rm -f "$temp_stderr"
    [[ -n "${temp_stdout:-}" && -f "$temp_stdout" ]] && rm -f "$temp_stdout"

    # Return only the actual command output (stdout), not Slurm job messages
    printf "%s\n" "$srun_output"
    return "$srun_status"
}

check_ssh_with() {
    local scriptlet="$1"
    shift
    local args=("$@")
    local temp_dir
    local return_val=1

    # Create temporary directory for SSH status
    temp_dir=$(mktemp -d) || {
        register_error "Failed to create temporary directory for SSH test"
        return 99
    }

    # Set up trap to clean up temp directory on exit/signal
    trap '[[ -n "${temp_dir:-}" ]] && rm -rf "$temp_dir"' EXIT INT TERM

    # Use spawn_N_ssh to run command on one host.  This sounds excessively
    # complex, but this also serves as the next best thing we have to
    # a regression test for the choose_N_ssh_hosts, spawn_N_ssh, and
    # gather_N_ssh functions.
    choose_N_ssh_hosts 1
    local spawn_output pids
    if ! spawn_output=$(spawn_N_ssh "$temp_dir" true "$scriptlet" "${args[@]}" 2>&1); then
        register_error "Failed to execute spawn_N_ssh"
        register_error "Command: spawn_N_ssh $temp_dir true '$scriptlet' ${args[*]}"
        register_error "Output: $spawn_output"
        rm -rf "$temp_dir"
        trap - EXIT INT TERM
        return 98
    fi

    # Extract PIDs from spawn_N_ssh output
    read -ra pids <<< "$spawn_output"
    if [[ ${#pids[@]} -ne 1 ]]; then
        register_error "Expected exactly 1 PID from spawn_N_ssh, got ${#pids[@]}: ${pids[*]}"
        rm -rf "$temp_dir"
        trap - EXIT INT TERM
        return 97
    fi

    # Use gather_N_ssh to wait for completion
    local gather_output
    if ! gather_output=$(gather_N_ssh "$temp_dir" "" "${pids[@]}" 2>&1); then
        register_error "Failed to execute gather_N_ssh"
        register_error "Command: gather_N_ssh $temp_dir \"\" ${pids[*]}"
        register_error "Output: $gather_output"
        rm -rf "$temp_dir"
        trap - EXIT INT TERM
        return 96
    fi

    # Get hostname for the single node we ran on from PID mapping
    local pid="${pids[0]}"
    local hostname
    hostname=$(results_N_ssh_pid "$temp_dir" "$pid" "hostname")

    # Check if hostname was properly retrieved
    if [[ -z "$hostname" ]]; then
        register_error "Failed to get hostname for PID $pid from SSH results"
        rm -rf "$temp_dir"
        trap - EXIT INT TERM
        return 95
    fi

    local exit_code
    exit_code=$(results_N_ssh_pid "$temp_dir" "$pid" "rc")

    if [[ "$exit_code" -eq 0 ]]; then
        return_val=0
    else
        register_error "SSH command failed with exit code: $exit_code"
        register_error "Command: ${args[*]}"
        return_val="$exit_code"
    fi

    local stdout_content
    stdout_content=$(results_N_ssh_pid "$temp_dir" "$pid" "stdout")
    if [[ -n "$stdout_content" ]]; then
        printf "%s\n" "$stdout_content"
    fi

    # Clean up temp directory
    rm -rf "$temp_dir"
    trap - EXIT INT TERM

    return "$return_val"
}


check_ssh() {
    ssh_output=$(check_ssh_with "" "echo ABCDEF")
    ssh_rc=$?

    # Need to be able to run a simple command (we only use a single node here to not break in the degenerate case of only one node)
    if [ "$ssh_rc" != "0" ] || ! echo "$ssh_output" | grep -q "ABCDEF"; then
        register_error "ssh: failed (rc=$ssh_rc): $ssh_output"
        # None of the rest of the checks will be useful, so exit here
        return 1
    fi

    # Need to be able to run a scriptlet from a string
    local temp_scriptlet
    temp_scriptlet=$(mktemp "${TMPDIR:-/tmp}/test_scriptlet.XXXXXX")
    trap '[[ -n "${temp_scriptlet:-}" ]] && rm -f "$temp_scriptlet"' EXIT INT TERM

    # Store scriptlet content in a variable for later reuse
    # The weird stuff makes sure arguments get in properly and that special shell
    # characters don't get mangled.
    local scriptlet_content
    scriptlet_content=$(cat <<'EOF'
echo -n $1
SOMEVAR=CD
echo -n "$SOMEVAR"
echo -n '$EF'
EOF
)

    # Write the content to the temp file
    printf '%s\n' "$scriptlet_content" > "$temp_scriptlet"

    ssh_output=$(check_ssh_with "$scriptlet_content" "AB")
    ssh_rc=$?
    # shellcheck disable=SC2016
    if [ "$ssh_rc" != "0" ] || ! echo "$ssh_output" | grep -q 'ABCD\$EF'; then
        register_error "ssh: failed (rc=$ssh_rc): $ssh_output"
        # None of the rest of the checks will be useful, so exit here
        rm -f "$temp_scriptlet"
        trap - EXIT INT TERM
        return 1
    fi

    # Need to be able to run a scriptlet from a file
    ssh_output=$(check_ssh_with "@$temp_scriptlet" "AB")
    ssh_rc=$?
    # shellcheck disable=SC2016
    if [ "$ssh_rc" != "0" ] || ! echo "$ssh_output" | grep -q 'ABCD\$EF'; then
        register_error "ssh: failed (rc=$ssh_rc): $ssh_output"
        # None of the rest of the checks will be useful, so exit here
        rm -f "$temp_scriptlet"
        trap - EXIT INT TERM
        return 1
    fi
    rm -f "$temp_scriptlet"
    trap - EXIT INT TERM
}

check_slurm() {
    # Validate Slurm is available
    if ! type -P sinfo >/dev/null 2>&1; then
        register_error "Slurm is not available in the current environment"
        return 1
    fi

    # The same check run_sbatch_job applies before submitting.
    local poll_error
    if ! poll_error=$(_slurm_job_poll_interval 2>&1 >/dev/null); then
        register_error "${poll_error#Error: }"
    fi

    # Check partition access and GPU requirements
    target_partition="${partition-}"
    if [[ -z "$target_partition" ]]; then
        # Get default partition if none specified
        target_partition=$(scontrol show config | grep -i defaultpartition | awk '{print $3}')
    fi

    # Check if partition exists and is accessible
    if ! sinfo -p "$target_partition" --noheader >/dev/null 2>&1; then
        register_error "Partition '$target_partition' does not exist or you don't have access to it"
    else
        # client_type vs GRES: mixed CPU/GPU partitions are valid for client_type=cpu (warnings only, no early
        # return). GPU GRES is only requested when client_type is gpu (see env_base.sh SBATCH/SRUN options).
        has_gpus=0
        if sinfo -p "$target_partition" -o "%N %G" --noheader | grep -v "(null)" | grep -q "gpu:"; then
            has_gpus=1
        fi

        case "${client_type-}" in
            "gpu")
                if (( has_gpus == 0 )); then
                    register_error "Client type is 'gpu' but partition '$target_partition' has no GPUs"
                fi
                ;;
            "cpu")
                if (( has_gpus == 1 )); then
                    local _seu_exclusive="${SLURM_EXCLUSIVE_USER:-0}"
                    case "${_seu_exclusive,,}" in
                        1 | yes | true)
                            register_warning "$(cat <<EOF
Partition '${target_partition}' lists GPU GRES on some nodes, but
client_type is 'cpu'. Mixed partitions are OK; GPUs are not requested for
cpu clients. With SLURM_EXCLUSIVE_USER, --cpus-per-task is set from the
minimum CPUTot across your partition/nodelist (see
get_slurm_target_node_cpus in lib/env_functions.sh). If srun validation
still fails, verify the smallest node in your include list can satisfy
that CPU count.
EOF
)"
                            ;;
                        *)
                            register_warning "$(cat <<EOF
Partition '${target_partition}' lists GPU GRES on at least some nodes,
but client_type is 'cpu'. Mixed CPU/GPU partitions are allowed; GPU jobs
are only requested when client_type is 'gpu'.
EOF
)"
                            ;;
                    esac
                fi
                ;;
            *)
                register_error "Invalid client_type: '${client_type-}'. Must be 'cpu' or 'gpu'"
                ;;
        esac
    fi

    # Check account only if specified
    if [[ -n "${account-}" ]]; then
        if ! sacctmgr show assoc where user="$(whoami)" format=account%-30 --noheader 2>/dev/null | grep -q "^${account} *$"; then
            register_error "Account '$account' is not available to your user"
        fi
    fi

    # Check reservation only if specified
    if [[ -n "${reservation-}" ]]; then
        if ! scontrol show reservation "$reservation" 2>/dev/null | grep -q "^ReservationName=${reservation} "; then
            register_error "Reservation '$reservation' does not exist or you don't have access to it"
        fi
    fi

    # Only proceed with Slurm tests if we have the required options
    if [[ -z "${SBATCH_OPTIONS-}" ]] || [[ -z "${SRUN_OPTIONS-}" ]]; then
        register_error "Required Slurm variables not set: SBATCH_OPTIONS and/or SRUN_OPTIONS missing"
        return 1
    fi

    # Two allocations in all: one probe (validate_probe_scriptlet) as an
    # sbatch job with the production options, one as an srun step with the
    # minimal options. Each reports the architecture and every root, which
    # check_arch_and_binaries and check_fs_slurm then read.
    local scriptlet
    scriptlet=$(validate_probe_scriptlet)
    sbatch_output=$(check_sbatch_with "$scriptlet" 1 ${VALIDATE_PROBE_ROOTS[@]+"${VALIDATE_PROBE_ROOTS[@]}"})
    sbatch_rc=$?
    parse_validate_probe sbatch "$sbatch_output"
    if [ "$sbatch_rc" != "0" ] || [[ "$PROBE_sbatch_ECHO" != ABCDEF ]]; then
        register_error "sbatch: failed (rc=$sbatch_rc): $sbatch_output"
        # None of the rest of the checks will be useful, so exit here
        return 1
    fi
    _validate_probe_complete sbatch || true

    srun_output=$(check_srun_with "$scriptlet" _ 1 ${VALIDATE_PROBE_ROOTS[@]+"${VALIDATE_PROBE_ROOTS[@]}"})
    srun_rc=$?
    parse_validate_probe srun "$srun_output"
    if [ "$srun_rc" != "0" ] || [[ "$PROBE_srun_ECHO" != ABCDEF ]]; then
        register_error "srun: failed (rc=$srun_rc): $srun_output"
        # None of the rest of the checks will be useful, so exit here
        return 1
    fi
    _validate_probe_complete srun || true

    return 0
}

check_fs() {
    local slurm_rc="$1"  # 0 if we can run slurm
    local fs_path="$2"   # filesystem path to test

    # The probes (validate_probe_scriptlet) checked that the path is a
    # directory on a different filesystem device than / and is writable. That
    # does not prove the path itself is the exact mountpoint.
    if [[ "$slurm_rc" == "0" ]]; then
        check_fs_slurm "$fs_path"
    elif [[ -n "${SSH_ENABLED:-}" ]]; then
        check_fs_ssh "$fs_path"
    else
        register_warning "$(cat <<EOF
Filesystem check skipped for:
${fs_path}
Slurm validation did not succeed and SSH is not configured. To probe
mounts over SSH, set SSH_HOST_LIST in env.sh. Otherwise fix Slurm
validation (e.g. srun/sbatch tests).
EOF
)"
    fi
}

check_fs_ssh() {
    local fs_path="$1"   # filesystem path to test
    if ! run_ssh_validation_probe && [[ -z "$PROBE_ssh_END" ]]; then
        # The session failed before reporting; check_ssh_with said why.
        register_error "ssh: ${fs_path} was not checked: the validation probe failed"
        return
    fi
    check_fs_probe_result ssh "$fs_path"
}

# The sbatch and srun probes in check_slurm already checked every root.
check_fs_slurm() {
    local fs_path="$1"   # filesystem path to test
    check_fs_probe_result sbatch "$fs_path"
    check_fs_probe_result srun "$fs_path"
}

check_obj() {
    local slurm_rc="$1"  # 0 if we can run slurm
    local ssh_rc="$2"    # 0 if we can run ssh
    functest_might_work=0

    if ! [ -f "$OBJ_AUTH_FILE" ]; then
        register_error "Create $OBJ_AUTH_FILE and have it export WARP_ACCESS_KEY and WARP_SECRET_KEY"
        functest_might_work=1
    fi

    if [ "$slurm_rc" = "0" ]; then
        if ! [ -f "${SCALE_TEST_BASE}/utils/s3test" ]; then
            # The check_arch_and_binaries will have already complained if this is
            # missing.
            functest_might_work=1
        fi
    fi

    if [ "$functest_might_work" = "0" ]; then
        if [ "$slurm_rc" = "0" ]; then
            check_obj_slurm
        elif [ "$ssh_rc" = "0" ]; then
            check_obj_ssh
        fi
    fi
    return 0
}

check_obj_slurm() {
    # TODO: actually run this in slurm
    local obj_count
    obj_count=$($S3TEST)
    local s3test_rc=$?
    if [ "$s3test_rc" != 0 ]; then
        register_error "Error validating object access; details follow"
        register_error "$obj_count"
        return 1
    fi
    if [[ $obj_count -gt 0 ]]; then
        register_error "WARNING!! Bucket ${OBJ_BUCKET} has ${obj_count} objects!"
        register_error "Warp WILL DELETE ALL OBJECTS IN THIS BUCKET!"
        return 1
    fi
    return 0
}

check_obj_ssh() {
    # Create temporary directory for misc files
    temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/ssh_status.XXXXXX") || {
        register_error "Failed to create temporary directory for misc files"
        return 99
    }

    # Set up trap to clean up temp directory on exit/signal
    trap '[[ -n "${temp_dir:-}" ]] && rm -rf "$temp_dir"' EXIT INT TERM

    # 1. Build the binary on the ssh host (if necessaary)
    choose_N_ssh_hosts 1
    local ssh_host="${SSH_NODELIST%%,*}"

    if ! build_s3test_on_selected_ssh_nodes; then
        echo "Error: Failed to build s3test on selected ssh nodes" >&2
        rm -rf "$temp_dir"
        trap - EXIT INT TERM
        return 1
    fi

    # 2. "scp" the credentials file over to the ssh host
    run_ssh_single "$ssh_host" "${temp_dir}/creds_copy.rc" "${temp_dir}/creds_copy.stdout" "" \
        "-|${OBJ_AUTH_FILE}" "/bin/bash" "-c" "umask 077 && cp /dev/stdin .obj_auth"
    if [ "$(cat "${temp_dir}/creds_copy.rc")" != 0 ]; then
        register_error "Error copying $OBJ_AUTH_FILE to $ssh_host"
        register_error "Output: $(cat "${temp_dir}/creds_copy.stdout")"
        rm -rf "$temp_dir"
        trap - EXIT INT TERM
        return 1
    fi

    # 3. Run s3test in a scriptlet with the necessary environment
    #    variables included (except credentials)
    local scriptlet_remote_s3test
    scriptlet_remote_s3test=$(cat <<EOF
export OBJ_BUCKET="${OBJ_BUCKET}"
export OBJ_REGION="${OBJ_REGION}"
export OBJ_HOST="${OBJ_HOST}"
export OBJ_HOST_PORT="${OBJ_HOST_PORT}"
. .obj_auth
export WARP_ACCESS_KEY WARP_SECRET_KEY
./s3test
rc=\$?
rm .obj_auth
exit \$rc
EOF
)
    run_ssh_single "$ssh_host" "${temp_dir}/s3test_run.rc" "${temp_dir}/s3test_run.stdout" "" \
        "$scriptlet_remote_s3test"
    local obj_count
    local ssh_rc
    obj_count="$(cat "${temp_dir}/s3test_run.stdout")"
    ssh_rc="$(cat "${temp_dir}/s3test_run.rc")"
    if [ "$ssh_rc" != 0 ]; then
        register_error "Error running s3test on $ssh_host"
        register_error "Output: $obj_count"
    elif [[ $obj_count -gt 0 ]]; then
        register_error "WARNING!! Bucket ${OBJ_BUCKET} has ${obj_count} objects!"
        register_error "Warp WILL DELETE ALL OBJECTS IN THIS BUCKET!"
    fi

    rm -rf "$temp_dir"
    trap - EXIT INT TERM
    return 0
}

# _validate_join_list ITEM...: the items as one comma-separated phrase.
_validate_join_list() {
    local joined="" item
    for item in "$@"; do
        joined+="${joined:+, }$item"
    done
    printf '%s' "$joined"
}

# _validate_summarize_if_clean ERRORS_BEFORE SUMMARY: register SUMMARY, a line
# stating what a check validated, unless the check registered an error (the
# error count was ERRORS_BEFORE when it started).
_validate_summarize_if_clean() {
    [[ "$(_validate_record_count "$ERROR_FILE")" -eq "$1" ]] || return 0
    register_summary "$2"
}

# Filled by validate_probe_roots and parse_validate_probe.
VALIDATE_PROBE_ROOTS=()
PROBE_sbatch_ECHO="" PROBE_sbatch_ARCH="" PROBE_srun_ECHO="" PROBE_srun_ARCH=""
PROBE_ssh_ARCH="" PROBE_ssh_END=""
check_dirs
launcher_prerequisites_rc=0
check_macos_launcher_prerequisites || launcher_prerequisites_rc=$?

if [[ -n "$VALIDATE_SEQUENCER" && -z "${KUBECTL_ENABLED:-}" ]]; then
    register_error "--sequencer applies only to EXECUTION_SUBSTRATE=kubectl"
fi

if [[ -n "${KUBECTL_ENABLED:-}" ]]; then
    if [[ "$launcher_prerequisites_rc" -eq 0 ]]; then
        check_kubectl_filesystem_prerequisites
    fi
    slurm_rc=1
    ssh_rc=1
elif [ -n "$SLURM_ENABLED" ]; then
    if [[ "$launcher_prerequisites_rc" -eq 0 ]]; then
        errors_before=$(_validate_record_count "$ERROR_FILE")
        validate_probe_roots
        check_slurm
        slurm_rc=$?
        [[ "$slurm_rc" -ne 0 ]] || _validate_summarize_if_clean "$errors_before" \
            "Slurm: partition ${target_partition:-default}, client type ${client_type-}, sbatch and srun: OK"
    else
        slurm_rc=1
    fi
else
    slurm_rc=1  # not enabled, so we can't run slurm checks
fi

if [ -n "$SSH_ENABLED" ]; then
    validate_probe_roots
    if [[ "$launcher_prerequisites_rc" -eq 0 ]]; then
        errors_before=$(_validate_record_count "$ERROR_FILE")
        check_ssh
        ssh_rc=$?
        [[ "$ssh_rc" -ne 0 ]] || _validate_summarize_if_clean "$errors_before" \
            "SSH: remote commands work (checked one of ${#SSH_ALL_HOSTS[@]} host(s)): OK"
    else
        ssh_rc=1
    fi
else
    ssh_rc=1  # not enabled, so we can't run ssh checks
fi

if [[ -n "${KUBECTL_ENABLED:-}" ]]; then
    : # The Kubernetes validation Job checks the image and target architecture.
elif [[ "$slurm_rc" -eq 0 || "$ssh_rc" -eq 0 ]]; then
    errors_before=$(_validate_record_count "$ERROR_FILE")
    check_arch_and_binaries
    _binary_names=()
    for _binary in ${remote_binaries[@]+"${remote_binaries[@]}"}; do
        _binary_names+=("${_binary##*/}")
    done
    _validate_summarize_if_clean "$errors_before" \
        "Architecture ${remote_arch:-unknown}, binaries ($(_validate_join_list ${_binary_names[@]+"${_binary_names[@]}"})): OK"
else
    printf "  (NOTE: Could not check binary presence or architecture because neither Slurm nor SSH validation succeeded)\n\n"
fi

if [ -n "$FS_ENABLED" ]; then
    # Validate elbencho configuration variables
    check_elbencho_config() {
        local validation_output
        if ! validation_output=$(validate_integer_array ELBENCHO_SCALE_THREAD_LIST 2>&1); then
            while IFS= read -r line; do
                register_error "$line"
            done <<< "$validation_output"
        fi

        if ! validation_output=$(validate_elbencho_io_sizes 2>&1); then
            while IFS= read -r line; do
                register_error "$line"
            done <<< "$validation_output"
        fi

        if ! validation_output=$(validate_integer_array ELBENCHO_IODEPTH_LIST 2>&1); then
            while IFS= read -r line; do
                register_error "$line"
            done <<< "$validation_output"
        fi

        if ! validation_output=$(validate_elbencho_file_workload_env 2>&1); then
            while IFS= read -r line; do
                register_error "$line"
            done <<< "$validation_output"
        fi

        if ! validation_output=$(validate_elbencho_duration 2>&1); then
            while IFS= read -r line; do
                register_error "$line"
            done <<< "$validation_output"
        fi

        if ! validation_output=$(validate_elbencho_live_csv 2>&1); then
            while IFS= read -r line; do
                register_error "$line"
            done <<< "$validation_output"
        elif [[ -n "$validation_output" ]]; then
            printf '%s\n' "$validation_output"
        fi

        if ! validation_output=$(validate_elbencho_single_big_file_env "" 2>&1); then
            while IFS= read -r line; do
                register_error "$line"
            done <<< "$validation_output"
        fi

        if ! validation_output=$(validate_storage_target_name 2>&1); then
            while IFS= read -r line; do
                register_error "$line"
            done <<< "$validation_output"
        fi
    }
    check_elbencho_config

    if [[ -z "${KUBECTL_ENABLED:-}" ]]; then
        # Loop over all filesystems in TEST_DIRS
        fs_before="$(_validate_record_count "$ERROR_FILE") $(_validate_record_count "$WARNING_FILE")"
        checked_roots=()
        for fs_path in "${!TEST_DIRS[@]}"; do
            # Skip empty paths
            if [ -n "$fs_path" ]; then
                check_fs "$slurm_rc" "$fs_path"
                checked_roots+=("$fs_path")
            fi
        done
        # A skipped check registers a warning, so the roots count as validated
        # only if no error or warning came from any of them.
        if [[ ${#checked_roots[@]} -gt 0 && "$fs_before" == \
                "$(_validate_record_count "$ERROR_FILE") $(_validate_record_count "$WARNING_FILE")" ]]; then
            mapfile -t checked_roots < <(printf '%s\n' "${checked_roots[@]}" | LC_ALL=C sort)
            register_summary "Filesystem roots ($(_validate_join_list "${checked_roots[@]}")): OK"
        fi
    fi
else
    if [[ -n "${KUBECTL_ENABLED:-}" ]]; then
        register_error "Kubernetes filesystem testing requires at least one nonempty TEST_DIRS entry"
    else
        validate_detail "  (NOTE: FS not enabled; not checking filesystem testability)\n\n"
    fi
fi

if [[ -n "$OBJ_ENABLED" && -z "${KUBECTL_ENABLED:-}" ]]; then
    errors_before=$(_validate_record_count "$ERROR_FILE")
    check_obj "$slurm_rc" "$ssh_rc"
    # check_obj tests nothing unless the credentials and a substrate are usable.
    [[ "${functest_might_work:-1}" -eq 0 && ( "$slurm_rc" -eq 0 || "$ssh_rc" -eq 0 ) ]] \
        && _validate_summarize_if_clean "$errors_before" \
            "Object storage: bucket ${OBJ_BUCKET:-} reachable and empty: OK"
elif [[ -z "${KUBECTL_ENABLED:-}" ]]; then
    validate_detail "  (NOTE: OBJ not enabled; not checking object testability)\n\n"
fi

# Print a multi-line message: first line indented 2 spaces, continuations 4 spaces.
_print_indented_message_block() {
    local _pim_msg="$1"
    local _pim_line=""
    local _pim_first=1
    while IFS= read -r _pim_line || [[ -n "${_pim_line:-}" ]]; do
        if [[ "$_pim_first" -eq 1 ]]; then
            printf '  %s\n' "$_pim_line"
            _pim_first=0
        else
            printf '    %s\n' "$_pim_line"
        fi
    done <<< "$_pim_msg"
    return 0
}

# State what the checks that passed validated, then report warnings and errors.
declare -a summaries=()
all_registered_summaries summaries
if (( ${#summaries[@]} > 0 )); then
    for _vs_msg in "${summaries[@]}"; do
        printf '%s\n' "$_vs_msg"
    done
fi

# Report warnings (non-fatal) before success/failure
declare -a warnings=()
all_registered_warnings warnings
if (( ${#warnings[@]} > 0 )); then
    printf "Validation notes / warnings:\n"
    _vw_msg=""
    for _vw_msg in "${warnings[@]}"; do
        _print_indented_message_block "$_vw_msg"
    done
    printf '\n'
fi

# Report all errors if any were found
all_registered_errors errors
if (( ${#errors[@]} > 0 )); then
    printf "Validation failed with the following errors:\n"
    _err_msg=""
    for _err_msg in "${errors[@]}"; do
        _print_indented_message_block "$_err_msg"
    done
    exit 1
fi

echo "All validation checks passed successfully"
exit 0
