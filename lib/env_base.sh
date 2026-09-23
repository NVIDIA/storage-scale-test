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

# This file gets sourced from env.sh and should contain things
# NOT expected to need to be edited or changed between different
# slurm clusters.

# Variables defined in env.sh BEFORE this file is sourced:
# SCALE_TEST_BASE
# TEST_DIR
# RESULTS_DIR
# LOGS_DIR
# client_type {cpu,gpu}
#
# EXECUTION_SUBSTRATE {slurm,ssh,kubectl}
#
# For SSH-based testing:
# SSH_HOST_LIST
# SSH_USER
#
# For SLURM-based testing:
# account
# reservation
# partition
# run_time
# MODULES
# SLURM_NODE_IGNORES
# SLURM_NODE_INCLUDES
#
# When you see these variables referenced with a trailing ":?" it indicates
# that we expect them to be defined and non-empty, and this script will
# abort if they aren't (satisfies ShellCheck SC2154)

# Load utility functions
# shellcheck disable=SC1091
. "${SCALE_TEST_BASE:?}/lib/env_functions.sh"

# Select one execution substrate explicitly. Do this before creating local
# directories, loading modules, or querying Slurm so an invalid environment
# has no setup side effects.
case "${EXECUTION_SUBSTRATE:-}" in
    slurm)
        export SLURM_ENABLED=1
        export SSH_ENABLED=""
        export KUBECTL_ENABLED=""
        ;;
    ssh)
        if [[ -z "${SSH_HOST_LIST:-}" ]]; then
            echo "Error: SSH_HOST_LIST is required when EXECUTION_SUBSTRATE=ssh" >&2
            return 1
        fi
        export SLURM_ENABLED=""
        export SSH_ENABLED=1
        export KUBECTL_ENABLED=""
        ;;
    kubectl)
        export SLURM_ENABLED=""
        export SSH_ENABLED=""
        export KUBECTL_ENABLED=1
        ;;
    "")
        echo "Error: EXECUTION_SUBSTRATE is required (slurm, ssh, or kubectl)" >&2
        return 1
        ;;
    *)
        echo "Error: unsupported EXECUTION_SUBSTRATE '${EXECUTION_SUBSTRATE}' (expected slurm, ssh, or kubectl)" >&2
        return 1
        ;;
esac
export EXECUTION_SUBSTRATE

# Make directories if not present
mkdir -p "${RESULTS_DIR:?}"
mkdir -p "${LOGS_DIR:?}"

# Add utils Path
PATH=$PATH:${SCALE_TEST_BASE:?}/utils
export PATH

# If TEST_DIR is defined and TEST_DIRS is empty, use TEST_DIR as the only test directory
if [[ -n "${TEST_DIR:-}" ]]; then
    # Check if TEST_DIRS is not defined or is empty
    if ! declare -p TEST_DIRS &>/dev/null || [[ ${#TEST_DIRS[@]} -eq 0 ]]; then
        # Initialize TEST_DIRS as an associative array if not already defined
        unset TEST_DIRS
        declare -gA TEST_DIRS
        # Set TEST_DIR as the only test directory with weight 1
        TEST_DIRS["$TEST_DIR"]=1
    fi
fi

# Key env vars toggle the various storage types:
#   "" if not enabled, "1" if they are
export FS_ENABLED=""
for dir in "${!TEST_DIRS[@]}"; do
    if [ -n "$dir" ]; then
        export FS_ENABLED="1"
        break
    fi
done
export OBJ_ENABLED=${OBJ_BUCKET:+1}
export BLOCK_ENABLED=

# Default ORDER_NODES to disabled; when enabled (1/yes/true),
# node selection is deterministic (first N) rather than random.
: "${ORDER_NODES:=0}"
case "${ORDER_NODES,,}" in
    1|yes|true) ORDER_NODES_ENABLED=1 ;;
    *)          ORDER_NODES_ENABLED="" ;;
esac
export ORDER_NODES ORDER_NODES_ENABLED

# Parse the SSH host file into an array of hostnames/IPs
if [ -n "$SSH_ENABLED" ]; then
    mapfile -t SSH_ALL_HOSTS < <(parse_ssh_host_file "$SSH_HOST_LIST")
    export SSH_ALL_HOSTS
    export SSH_HOST_COUNT=${#SSH_ALL_HOSTS[@]}
fi

# Do some slurm-related initialization if SLURM_ENABLED is set
if [ -n "$SLURM_ENABLED" ]; then
    # Load the modules we can
    MODULES+=(
        "slurm"
        "shared"
        "boost"  # needed to build elbencho?
        "ucx"
    )

    # Try to load each module
    for module_name in "${MODULES[@]}"; do
        if module_exists "$module_name"; then
            module load "$module_name"
        fi
    done

    # See if an automatically-detected account can be used
    if [ -z "$account" ]; then
        my_user="$(whoami)"
        output=$(sacctmgr -nP show assoc where user="$my_user" format=account 2>/dev/null ||:)
        if [ "$output" != "" ]; then  # -n didn't work here with Axis shell trickery
            account="$output"
        fi
    fi

    # Initialize empty options strings
    SBATCH_OPTIONS=""
    SRUN_OPTIONS=""

    # GPU GRES request for GPU clients. Empty for cpu. Some GPU-only
    # partitions reject jobs that omit a GPU specification entirely.
    SLURM_GPUS_PER_NODE_OPT=""
    if [[ "${client_type:?}" = "gpu" ]]; then
        SLURM_GPUS_PER_NODE_OPT="--gpus-per-node=4"
    fi
    export SLURM_GPUS_PER_NODE_OPT

    # Build SBATCH_OPTIONS string incrementally
    SBATCH_OPTIONS=$(add_option "$SBATCH_OPTIONS" "-A" "$account")
    SBATCH_OPTIONS=$(add_option "$SBATCH_OPTIONS" "--reservation" "${reservation:-}")
    SBATCH_OPTIONS=$(add_option "$SBATCH_OPTIONS" "-p" "${partition:-}")
    if [[ -n "$SLURM_GPUS_PER_NODE_OPT" ]]; then
        SBATCH_OPTIONS="$SBATCH_OPTIONS $SLURM_GPUS_PER_NODE_OPT"
    fi
    SBATCH_OPTIONS=$(add_option "$SBATCH_OPTIONS" "--time" "${run_time:?}")

    # Build SRUN_OPTIONS string
    SRUN_OPTIONS=$(add_option "$SRUN_OPTIONS" "-A" "$account")
    SRUN_OPTIONS=$(add_option "$SRUN_OPTIONS" "--reservation" "${reservation:-}")
    SRUN_OPTIONS=$(add_option "$SRUN_OPTIONS" "-p" "${partition:-}")
    if [[ -n "$SLURM_GPUS_PER_NODE_OPT" ]]; then
        SRUN_OPTIONS="$SRUN_OPTIONS $SLURM_GPUS_PER_NODE_OPT"
    fi
    SRUN_OPTIONS=$(add_option "$SRUN_OPTIONS" "--time" "${run_time:?}")

    # Default SLURM_EXTRA_ARGS to empty array for backward compatibility.
    # Elements are folded into SBATCH_OPTIONS/SRUN_OPTIONS below so both
    # manual and programmatic callers get a complete option string.
    if ! declare -p SLURM_EXTRA_ARGS &>/dev/null; then
        SLURM_EXTRA_ARGS=()
    fi
    export SLURM_EXTRA_ARGS

    # Default SLURM_EXCLUSIVE_USER to disabled; when enabled (1/yes/true),
    # sbatch uses --exclusive=user instead of --exclusive.
    : "${SLURM_EXCLUSIVE_USER:=0}"
    case "${SLURM_EXCLUSIVE_USER,,}" in
        1|yes|true) SLURM_EXCLUSIVE_OPT="--exclusive=user" ;;
        *)          SLURM_EXCLUSIVE_OPT="--exclusive" ;;
    esac
    export SLURM_EXCLUSIVE_USER SLURM_EXCLUSIVE_OPT

    # Read a slurm node file, join non-blank lines with commas, and count
    # the expanded nodes.  Sets two variables named by the caller:
    #   $1 = file path       (e.g. $SLURM_NODE_IGNORES)
    #   $2 = slurm flag name (e.g. "--exclude" or "--nodelist")
    #   $3 = variable name for the resulting flag string
    #   $4 = variable name for the node count
    _read_slurm_node_file() {
        local file="$1" flag="$2" var_arg="$3" var_count="$4"
        local hostlist node_count=0

        if [[ -s "$file" ]]; then
            hostlist=$(grep -v '^[[:space:]]*$' "$file" | paste -sd, || true)
            if [[ -n "$hostlist" ]]; then
                node_count=$(scontrol show hostname "$hostlist" | wc -l)
            fi
        fi

        if [[ "$node_count" -gt 0 ]]; then
            printf -v "$var_arg" '%s=%s' "$flag" "$hostlist"
        else
            printf -v "$var_arg" ''
        fi
        printf -v "$var_count" '%d' "$node_count"
        return 0
    }

    # $SLURM_NODE_IGNORES points to a file of slurm clients to exclude.
    # The file may contain hostlists in any valid slurm format, one per
    # line (blank lines are ignored, lines are joined with commas).
    _read_slurm_node_file "$SLURM_NODE_IGNORES" "--exclude" \
        SLURM_EXCLUDES SLURM_EXCLUDE_COUNT
    export SLURM_EXCLUDES
    export SLURM_EXCLUDE_COUNT

    # $SLURM_NODE_INCLUDES points to a file of slurm clients to restrict
    # jobs to (via --nodelist).  Same file format as SLURM_NODE_IGNORES.
    _read_slurm_node_file "$SLURM_NODE_INCLUDES" "--nodelist" \
        SLURM_INCLUDES SLURM_INCLUDE_COUNT
    export SLURM_INCLUDES
    export SLURM_INCLUDE_COUNT

    # When ORDER_NODES is enabled and an include list is present,
    # expand the hostlist into an ordered array so run_sbatch_job can
    # restrict each job to exactly the first N nodes.
    SLURM_ORDERED_NODES=()
    if [[ -n "${ORDER_NODES_ENABLED:-}" && "$SLURM_INCLUDE_COUNT" -gt 0 ]]; then
        mapfile -t SLURM_ORDERED_NODES < <(
            scontrol show hostname "${SLURM_INCLUDES#--nodelist=}"
        )
    fi
    export SLURM_ORDERED_NODES

    # Fold excludes/includes into SBATCH_OPTIONS and SRUN_OPTIONS so
    # that these strings alone are sufficient for a complete slurm
    # command line.
    if [[ -n "$SLURM_EXCLUDES" ]]; then
        SBATCH_OPTIONS="$SBATCH_OPTIONS $SLURM_EXCLUDES"
        SRUN_OPTIONS="$SRUN_OPTIONS $SLURM_EXCLUDES"
    fi
    if [[ -n "$SLURM_INCLUDES" ]]; then
        SBATCH_OPTIONS="$SBATCH_OPTIONS $SLURM_INCLUDES"
        SRUN_OPTIONS="$SRUN_OPTIONS $SLURM_INCLUDES"
    fi

    # When --exclusive=user is active, query the target node CPU count so
    # --cpus-per-task can be folded into the option strings.  Unlike bare
    # --exclusive, --exclusive=user does NOT implicitly allocate all CPUs;
    # without an explicit request SLURM defaults to 1 CPU and cgroup
    # enforces it.
    # The query is skipped inside batch jobs (SLURM_JOB_ID is set) where
    # the allocation is already made.
    SLURM_EXCLUSIVE_USER_CPUS=""
    if [[ -z "${SLURM_JOB_ID:-}" && "$SLURM_EXCLUSIVE_OPT" == "--exclusive=user" ]]; then
        SLURM_EXCLUSIVE_USER_CPUS=$(get_slurm_target_node_cpus) || true
        if [[ -n "$SLURM_EXCLUSIVE_USER_CPUS" ]]; then
            echo "SLURM_EXCLUSIVE_USER: requesting $SLURM_EXCLUSIVE_USER_CPUS CPUs per task"
        else
            echo "Warning: --exclusive=user enabled but could not determine node CPU count" >&2
        fi
    fi
    export SLURM_EXCLUSIVE_USER_CPUS

    # Fold exclusive mode and CPU requirement into the option strings.
    SBATCH_OPTIONS="$SBATCH_OPTIONS $SLURM_EXCLUSIVE_OPT"
    SRUN_OPTIONS="$SRUN_OPTIONS $SLURM_EXCLUSIVE_OPT"

    if [[ -n "${SLURM_EXCLUSIVE_USER_CPUS:-}" ]]; then
        SBATCH_OPTIONS="$SBATCH_OPTIONS --cpus-per-task=$SLURM_EXCLUSIVE_USER_CPUS"
        SRUN_OPTIONS="$SRUN_OPTIONS --cpus-per-task=$SLURM_EXCLUSIVE_USER_CPUS"
    fi

    # Snapshot the base options (everything except SLURM_EXTRA_ARGS).
    # build_sbatch_cmd / build_srun_cmd parse these and then append the
    # SLURM_EXTRA_ARGS array so that elements with embedded spaces are
    # preserved in the programmatic path.
    _SBATCH_OPTIONS_BASE="$SBATCH_OPTIONS"
    _SRUN_OPTIONS_BASE="$SRUN_OPTIONS"
    export _SBATCH_OPTIONS_BASE _SRUN_OPTIONS_BASE

    # Append flattened SLURM_EXTRA_ARGS to the public strings for manual
    # human use (e.g., srun $SRUN_OPTIONS -N 1 hostname).  Values with
    # embedded spaces are best-effort in this flat-string form.
    if [[ ${#SLURM_EXTRA_ARGS[@]} -gt 0 ]]; then
        SBATCH_OPTIONS="$SBATCH_OPTIONS ${SLURM_EXTRA_ARGS[*]}"
        SRUN_OPTIONS="$SRUN_OPTIONS ${SLURM_EXTRA_ARGS[*]}"
    fi

    export SBATCH_OPTIONS
    export SRUN_OPTIONS

    # Default SLURM_JOB_NAME_PREFIX to empty string for backward compatibility
    export SLURM_JOB_NAME_PREFIX="${SLURM_JOB_NAME_PREFIX:-}"
fi

# Default values for FS_MAX_* estimates
# If IOR_FS_MAX_AGG_THROUGHPUT is defined, use it as the default for FS_MAX_AGG_THROUGHPUT
if [ -n "${IOR_FS_MAX_AGG_THROUGHPUT+x}" ]; then
    export FS_MAX_AGG_THROUGHPUT=${FS_MAX_AGG_THROUGHPUT:-$IOR_FS_MAX_AGG_THROUGHPUT}   # Units are GB/s
else
    export FS_MAX_AGG_THROUGHPUT=${FS_MAX_AGG_THROUGHPUT:-10}   # Units are GB/s
fi
export FS_MAX_NODE_THROUGHPUT_GBPS=${FS_MAX_NODE_THROUGHPUT_GBPS:-40}  # Units are Gbps
export FS_MAX_NODE_IOPS=${FS_MAX_NODE_IOPS:-10000}

# Set some defaults in case old env.sh files don't get updated in-place when
# new code comes in that defines new vars (merely a convenience)
export MDTEST_BRANCH_FACTOR=${MDTEST_BRANCH_FACTOR:-7}
export MDTEST_ITEMS_PER_DIR=${MDTEST_ITEMS_PER_DIR:-100}
export MDTEST_ITERATIONS=${MDTEST_ITERATIONS:-3}
#
# Set defaults for ELBENCHO settings
declare -p ELBENCHO_SCALE_THREAD_LIST &>/dev/null || \
    export ELBENCHO_SCALE_THREAD_LIST=("1" "2" "4" "8" "16" "32" "64" "128" "256")
declare -p ELBENCHO_SCALE_IO_SIZES &>/dev/null || \
    export ELBENCHO_SCALE_IO_SIZES=("4K" "16K" "64K" "1M" "1M,4K")
if ! declare -p ELBENCHO_IODEPTH_LIST &>/dev/null || \
   [[ "${#ELBENCHO_IODEPTH_LIST[@]}" -eq 0 ]] || \
   [[ -z "${ELBENCHO_IODEPTH_LIST[0]}" ]]; then
    export ELBENCHO_IODEPTH_LIST=("1")  # default is to only always use 1
fi
export ELBENCHO_SCALE_READ_WRITE_DURATION=${ELBENCHO_SCALE_READ_WRITE_DURATION:-30}
export ELBENCHO_READ_AFTER_WRITE_PAUSE=${ELBENCHO_READ_AFTER_WRITE_PAUSE:-0}
export ELBENCHO_FILE_SIZE_MULTIPLIER=${ELBENCHO_FILE_SIZE_MULTIPLIER:-1024}
# Generated many-file layout and bounded-workload controls. Empty count/size
# values preserve the historical worker-directory workload.
# Snapshot restore helpers assign these names in isolated subshells; the
# parent-shell defaults here remain intentional.
# shellcheck disable=SC2031
export ELBENCHO_FILE_LAYOUT=${ELBENCHO_FILE_LAYOUT:-worker-directories} \
    ELBENCHO_FILES_PER_NODE=${ELBENCHO_FILES_PER_NODE:-} \
    ELBENCHO_FILE_SIZE=${ELBENCHO_FILE_SIZE:-}
# Single shared large file (elbencho file path mode); sequential I/O only (mutually exclusive with random)
export ELBENCHO_SINGLE_BIG_FILE=${ELBENCHO_SINGLE_BIG_FILE:-0}
export ELBENCHO_SINGLE_BIG_FILE_BASENAME=${ELBENCHO_SINGLE_BIG_FILE_BASENAME:-elbencho-bigfile}
# When ELBENCHO_SINGLE_BIG_FILE=1: required for write/read-after-write; optional for nv-elbencho-sweep --read-from (read omits -s)
export ELBENCHO_SINGLE_BIG_FILE_SIZE=${ELBENCHO_SINGLE_BIG_FILE_SIZE:-}
# When 1, maps to elbencho --nosvcshare (each service touches full file)
export ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=${ELBENCHO_ALL_NODES_ACCESS_ALL_DATA:-0}
#
export WARP_RPS_BUDGET_PUT=${WARP_RPS_BUDGET_PUT:-}
export WARP_RPS_BUDGET_GET=${WARP_RPS_BUDGET_GET:-}
#
# Set defaults for WARP settings
#
# WARP_THREAD_LIST
if ! declare -p WARP_THREAD_LIST &>/dev/null; then
    export WARP_THREAD_LIST=(8 32 64 128 256)
fi
#
# WARP_OBJ_SIZES
if ! declare -p WARP_OBJ_SIZES &>/dev/null; then
    export WARP_OBJ_SIZES=("4MiB" "8MiB" "16MiB" "32MiB" "64MiB")
fi
#
# WARP_PUT_DURATION
export WARP_PUT_DURATION=${WARP_PUT_DURATION:-3m}
#
# WARP_PUT_MIN_FILES_PER_CLIENT
export WARP_PUT_MIN_FILES_PER_CLIENT=${WARP_PUT_MIN_FILES_PER_CLIENT:-10000}
#
# WARP_GET_DURATION
export WARP_GET_DURATION=${WARP_GET_DURATION:-5m}
#
export WARP_RANGE_OBJ_SIZE=${WARP_RANGE_OBJ_SIZE:-5GiB}
#
# Set defaults for NETBENCH settings
export NETBENCH_HOST_NIC_GBPS=${NETBENCH_HOST_NIC_GBPS:-100}
export NETBENCH_TARGET_RUNTIME=${NETBENCH_TARGET_RUNTIME:-10}
export NETBENCH_PORT=${NETBENCH_PORT:-12865}
export NETBENCH_BLOCKSIZE=${NETBENCH_BLOCKSIZE:-1M}
export NETBENCH_RESPSIZE=${NETBENCH_RESPSIZE:-4K}
declare -p NETBENCH_THREADS &>/dev/null || \
    export NETBENCH_THREADS=("4" "8" "16" "32")
export NETBENCH_ITERATIONS=${NETBENCH_ITERATIONS:-3}

#
# Source object credentials, if present.
# Export explicitly in case the auth file uses bare assignments without "export".
if [ -f "$OBJ_AUTH_FILE" ]; then
    # shellcheck disable=SC1090
    source "$OBJ_AUTH_FILE"
    export WARP_ACCESS_KEY WARP_SECRET_KEY
    # Also expose creds via the standard S3 SDK credential env vars (needed for
    # S3 Express). These variable names are mandated by the S3 client SDK.
    export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-$WARP_ACCESS_KEY}"
    export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-$WARP_SECRET_KEY}"
fi

# Backwards compatibility for old env.sh files that don't have client_arch set,
# we preserve old behavior of using uname -m to determine the architecture.
if [ -z "${client_arch+x}" ]; then
    client_arch=$(uname -m)
fi

suffix=
if [ "${client_arch:?}" = "aarch64" ]; then
    suffix=.aarch64
fi

# Set executables location with architecture suffix
export S3TEST="${SCALE_TEST_BASE}/utils/s3test${suffix}"
export WARP="${SCALE_TEST_BASE}/utils/warp${suffix}"
export ELBENCHO="${SCALE_TEST_BASE}/utils/elbencho${suffix}"
