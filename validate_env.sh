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

# Initialize error tracking
declare -a errors=()
declare source_output=""

# Get absolute path to script directory
SCALE_TEST_BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Initialize error accumulation system
ERROR_FILE=""
WARNING_FILE=""

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
    rm -f "${ERROR_FILE:-}.lock" "${WARNING_FILE:-}.lock"
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
            printf '%s\n' "$error_message" >> "$ERROR_FILE"
        ) 200>"$ERROR_FILE.lock"
    else
        # Fallback: direct write without locking (potential race condition)
        printf '%s\n' "$error_message" >> "$ERROR_FILE"
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

# Get all registered errors as a bash array
all_registered_errors() {
    local -n result_array=$1
    result_array=()

    if [[ -z "$ERROR_FILE" || ! -f "$ERROR_FILE" ]]; then
        return 0
    fi

    # Read errors from file into array
    while IFS= read -r line; do
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

    while IFS= read -r -d '' rec; do
        result_warns+=("$rec")
    done < "$WARNING_FILE"
}

# Initialize the error tracking system
init_error_tracking

# Source env.sh, capturing any output
if ! source_output=$("$SHELL" -c ". ${SCALE_TEST_BASE}/env.sh" 2>&1); then
    printf "%s\n\nFailed to soruce env.sh; fix ^^^^^^^^^^\n" "$source_output"
    exit 1
fi

# shellcheck disable=SC1091
. "${SCALE_TEST_BASE}/env.sh"

# Check required directories
check_dirs() {
    [[ -d "${RESULTS_DIR}" ]] || register_error "Directory ${RESULTS_DIR} does not exist"
    [[ -d "${LOGS_DIR}" ]] || register_error "Directory ${LOGS_DIR} does not exist"
}

check_kubectl_filesystem_prerequisites() {
    local kubectl_functions="${SCALE_TEST_BASE}/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    if [[ ! -f "$kubectl_functions" ]]; then
        register_error "Kubernetes filesystem support files are missing: $kubectl_functions"
        return 1
    fi
    # shellcheck disable=SC1090  # Checked-in filesystem-sweep validation helpers.
    source "$kubectl_functions"
    local identity
    if ! identity=$(kubectl_validate_cluster_identity 2>&1); then
        register_error "Kubernetes filesystem prerequisite validation failed: $identity"
        return 1
    fi
    if ! kubectl_validate_runtime_pod; then
        register_error "Kubernetes workload image or PVC runtime validation failed"
        return 1
    fi
    printf '  Kubernetes namespace/PV/PVC identity: %s\n' "$identity"
}

# Determine architecture & check for binaries
check_arch_and_binaries() {
    # Since we support SSH mode, we can no longer rely on an assumption that the invoking
    # host and the target compute host are the same architecture.  So here, we have to
    # probe the architecture of one compute node.

    if [ -n "$SLURM_ENABLED" ]; then
        # For slurm, we can use the Slurm node architecture.
        remote_arch=$(check_srun_with "uname -m")
    else
        # For SSH, we need to probe the architecture of one compute node.
        remote_arch=$(check_ssh_with "uname -m")
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

        # Check binary architecture
        if ! file "$binary" | grep -q "$remote_arch_pattern"; then
            register_error "Binary ${binary} not compiled for ${remote_arch}"
        fi
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

        # Check binary architecture
        if ! file "$binary" | grep -q "$remote_arch_pattern"; then
            register_warning "$(printf '%s\n%s' "Optional binary not compiled for ${remote_arch}:" "$binary")"
        fi
    done
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

    # First just basic check that sbatch runs.
    sbatch_output=$(check_sbatch_with "echo ABCDEF")
    sbatch_rc=$?
    if [ "$sbatch_rc" != "0" ] || ! echo "$sbatch_output" | grep -q "ABCDEF"; then
        register_error "sbatch: failed (rc=$sbatch_rc): $sbatch_output"
        # None of the rest of the checks will be useful, so exit here
        return 1
    fi

    srun_output=$(check_srun_with "echo ABCDEF")
    srun_rc=$?
    if [ "$srun_rc" != "0" ] || ! echo "$srun_output" | grep -q "ABCDEF"; then
        register_error "srun: failed (rc=$srun_rc): $srun_output"
        # None of the rest of the checks will be useful, so exit here
        return 1
    fi

    return 0
}

check_fs() {
    local slurm_rc="$1"  # 0 if we can run slurm
    local fs_path="$2"   # filesystem path to test

    # This scriptlet verifies that the test path is a directory on a different
    # filesystem device than /. It does not prove that the path itself is the
    # exact mountpoint. It is used for both Slurm and SSH-based testing.
    local scriptlet
    scriptlet=$(cat << 'EOF'
TEST_DIR="$1"
fs_device_failure() { echo "failed_$(hostname -s)"; exit 1; }
test -d "$TEST_DIR" || fs_device_failure
test_dir_device=$(stat -c %d -- "$TEST_DIR") || fs_device_failure
root_device=$(stat -c %d -- "/") || fs_device_failure
[ "$test_dir_device" != "$root_device" ] || fs_device_failure
test -w "$TEST_DIR" || (echo "nowrite_$(hostname -s)" && exit 1)
EOF
)

    if [[ "$slurm_rc" == "0" ]]; then
        check_fs_slurm "$fs_path" "$scriptlet"
    elif [[ -n "${SSH_ENABLED:-}" ]]; then
        check_fs_ssh "$fs_path" "$scriptlet"
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
    local scriptlet="$2" # scriptlet to test

    ssh_output=$(check_ssh_with "$scriptlet" "$fs_path")
    ssh_rc=$?
    if [[ "$ssh_output" =~ failed_([[:alnum:]_-]+) ]]; then
        nodename="${BASH_REMATCH[1]}"
        register_error "ssh: ${fs_path} is not on a filesystem distinct from / on compute node ${nodename}"
    elif [[ "$ssh_output" =~ nowrite_([[:alnum:]_-]+) ]]; then
        nodename="${BASH_REMATCH[1]}"
        register_error "ssh: ${fs_path} is not writable on compute node ${nodename}"
    elif [[ $ssh_rc != 0 ]]; then
        register_error "ssh: $fs_path is not on a filesystem distinct from / on some node"
        register_error "ssh: output: $ssh_output"
    fi

    local fs_path_quoted
    printf -v fs_path_quoted '%q' "$fs_path"
    # shellcheck disable=SC2016
    local fs_check_cmd='TEST_DIR='"$fs_path_quoted"'; test -d "$TEST_DIR" && test_dir_device=$(stat -c %d -- "$TEST_DIR") && root_device=$(stat -c %d -- /) && [ "$test_dir_device" != "$root_device" ] || echo "failed_$(hostname -s)"'
    ssh_output=$(check_ssh_with "" "$fs_check_cmd")

    # Check for filesystem-device failure regardless of ssh exit status
    if [[ "$ssh_output" =~ failed_([[:alnum:]_-]+) ]]; then
        nodename="${BASH_REMATCH[1]}"
        register_error "ssh: ${fs_path} is not on a filesystem distinct from / on compute node ${nodename}"
    fi
}

check_fs_slurm() {
    local fs_path="$1"   # filesystem path to test
    local scriptlet="$2" # scriptlet to test

    sbatch_output=$(check_sbatch_with "$scriptlet" "$fs_path")
    sbatch_rc=$?
    if [[ $sbatch_rc != 0 ]]; then
        register_error "sbatch: $fs_path is not on a filesystem distinct from / on some node"
        register_error "sbatch: output: $sbatch_output"
    fi

    # shellcheck disable=SC2016
    srun_output=$(check_srun_with 'TEST_DIR="$1"; h=$(hostname -s); test -d "$TEST_DIR" && test_dir_device=$(stat -c %d -- "$TEST_DIR") && root_device=$(stat -c %d -- /) && [ "$test_dir_device" != "$root_device" ] || echo "failed_${h}"' _ "$fs_path")

    # Check for filesystem-device failure regardless of srun exit status
    if [[ "$srun_output" =~ failed_([[:alnum:]_-]+) ]]; then
        nodename="${BASH_REMATCH[1]}"
        register_error "srun: ${fs_path} is not on a filesystem distinct from / on compute node ${nodename}"
    fi
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

check_dirs

if [[ -n "${KUBECTL_ENABLED:-}" ]]; then
    check_kubectl_filesystem_prerequisites
    slurm_rc=1
    ssh_rc=1
elif [ -n "$SLURM_ENABLED" ]; then
    check_slurm
    slurm_rc=$?
else
    slurm_rc=1  # not enabled, so we can't run slurm checks
fi

if [ -n "$SSH_ENABLED" ]; then
    check_ssh
    ssh_rc=$?
else
    ssh_rc=1  # not enabled, so we can't run ssh checks
fi

if [[ "$slurm_rc" -eq 0 || "$ssh_rc" -eq 0 ]]; then
    check_arch_and_binaries
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
    }
    check_elbencho_config

    if [[ -z "${KUBECTL_ENABLED:-}" ]]; then
        # Loop over all filesystems in TEST_DIRS
        for fs_path in "${!TEST_DIRS[@]}"; do
            # Skip empty paths
            if [ -n "$fs_path" ]; then
                check_fs "$slurm_rc" "$fs_path"
            fi
        done
    fi
else
    printf "  (NOTE: FS not enabled; not checking filesystem testability)\n\n"
fi

if [[ -n "$OBJ_ENABLED" && -z "${KUBECTL_ENABLED:-}" ]]; then
    check_obj "$slurm_rc" "$ssh_rc"
else
    printf "  (NOTE: OBJ not enabled; not checking object testability)\n\n"
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
