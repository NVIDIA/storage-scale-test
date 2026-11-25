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

# Resolve a path with GNU realpath, using Homebrew's prefixed command on macOS.
_portable_realpath_m() {
    local path="$1"
    local resolved
    if resolved=$(realpath -m -- "$path" 2>/dev/null); then
        printf '%s\n' "$resolved"
        return 0
    fi
    grealpath -m -- "$path"
}

# Print the filesystem device ID with GNU stat or Homebrew's prefixed command.
_portable_stat_device_id() {
    local path="$1"
    local device_id
    if device_id=$(stat -c %d "$path" 2>/dev/null); then
        printf '%s\n' "$device_id"
        return 0
    fi
    gstat -c %d "$path"
}

# Run one command with GNU timeout or Homebrew's prefixed command.
# Returns the command status, or 124 when the deadline expires.
_run_command_with_timeout() {
    local max_seconds="$1"
    shift
    if command -v timeout >/dev/null 2>&1; then
        timeout --signal=TERM --kill-after=2s "${max_seconds}s" "$@"
    else
        gtimeout --signal=TERM --kill-after=2s "${max_seconds}s" "$@"
    fi
}

# Parse --resfile from elbencho argv. Prints path to stdout (may be empty).
# Usage: resfile=$(_elbencho_resfile_from_args "$@")
_elbencho_resfile_from_args() {
    local path=""
    local skip_next=false
    local arg
    for arg in "$@"; do
        if [[ "$skip_next" == true ]]; then
            path="$arg"
            skip_next=false
            continue
        fi
        if [[ "$arg" == --resfile ]]; then
            skip_next=true
            continue
        fi
        if [[ "$arg" == --resfile=* ]]; then
            path="${arg#--resfile=}"
        fi
    done
    printf '%s' "$path"
    return 0
}

# Record the project version that produced an elbencho result file beside it,
# as RESULT.project-version; reports show it. Launchers and coordinators
# export STORAGE_SCALE_TEST_VERSION (see lib/project_version.sh). The record is
# informational, so failing to write it never fails the benchmark.
_write_result_version() {
    local result="$1"
    [[ -n "$result" ]] || return 0
    printf '%s\n' "${STORAGE_SCALE_TEST_VERSION:-unknown}" > "$result.project-version" \
        || echo "Warning: could not record the project version for $result" >&2
    return 0
}

# The oldest elbencho this project runs; the one definition of the floor.
# Every sweep, validate_env.sh and the Kubernetes validation Job enforce it.
ELBENCHO_MIN_VERSION=3.4-1

# Print "MAJOR MINOR PATCH" for vX.Y-Z, X.Y-Z, X.Y.Z or vX.Y.Z (release tag,
# `elbencho --version`, JSON config.version, net protocol), so that 3.4-1 equals
# 3.4.1 and 3.10-1 is above 3.4-1. Returns 1 for anything else.
elbencho_version_tuple() {
    local version="${1-}"
    if [[ "$version" =~ ^v?([0-9]+)[.]([0-9]+)[.-]([0-9]+)$ ]]; then
        printf '%d %d %d\n' "$((10#${BASH_REMATCH[1]}))" \
            "$((10#${BASH_REMATCH[2]}))" "$((10#${BASH_REMATCH[3]}))"
        return 0
    fi
    return 1
}

# elbencho_version_at_least FOUND MINIMUM: succeed when FOUND >= MINIMUM.
# Returns 1 when FOUND is older and 2 when either string is unparseable.
elbencho_version_at_least() {
    local found minimum index
    local -a found_parts minimum_parts
    found=$(elbencho_version_tuple "${1-}") || return 2
    minimum=$(elbencho_version_tuple "${2-}") || return 2
    read -r -a found_parts <<< "$found"
    read -r -a minimum_parts <<< "$minimum"
    for index in 0 1 2; do
        (( found_parts[index] == minimum_parts[index] )) && continue
        (( found_parts[index] > minimum_parts[index] )) && return 0
        return 1
    done
    return 0
}

# Read `elbencho --version` output on stdin and print its "* Version:" value.
elbencho_version_from_output() {
    local line
    while IFS= read -r line; do
        if [[ "$line" =~ ^[[:space:]]*\*[[:space:]]*Version:[[:space:]]*([^[:space:]]+)[[:space:]]*$ ]]; then
            printf '%s\n' "${BASH_REMATCH[1]}"
            return 0
        fi
    done
    return 1
}

# Print the SHA-256 of a file with whichever tool the host has.
_platform_file_sha256() {
    local output
    if type -P sha256sum >/dev/null 2>&1; then
        output=$(sha256sum "$1") || return 1
        printf '%s\n' "${output%% *}"
    elif type -P shasum >/dev/null 2>&1; then
        output=$(shasum -a 256 "$1") || return 1
        printf '%s\n' "${output%% *}"
    elif type -P openssl >/dev/null 2>&1; then
        output=$(openssl dgst -sha256 "$1") || return 1
        printf '%s\n' "${output##* }"
    else
        return 1
    fi
    return 0
}

# Print the version a binary reports, when it runs on this host.
elbencho_binary_version() {
    local output
    output=$(_run_command_with_timeout 15 "$1" --version 2>/dev/null) || return 1
    elbencho_version_from_output <<< "$output"
}

# Print the tag that utils/build/fetch_elbencho.sh stamped beside BINARY
# (BINARY.version holds "TAG SHA256"), if the stamp describes this very file.
elbencho_stamp_version() {
    local binary="$1" tag="" stamped_sha="" actual
    [[ -f "$binary.version" ]] || return 1
    read -r tag stamped_sha _ < "$binary.version" || [[ -n "$tag" ]] || return 1
    actual=$(_platform_file_sha256 "$binary") || return 1
    [[ -n "$stamped_sha" && "$actual" == "$stamped_sha" ]] || return 1
    printf '%s\n' "$tag"
}

# Print the version of an elbencho binary: from --version when it runs here
# (a binary of another architecture does not), otherwise from its fetch stamp.
elbencho_version_of_binary() {
    elbencho_binary_version "$1" || elbencho_stamp_version "$1"
}

# elbencho_version_error WHAT FOUND HINT: the one error for an elbencho below
# the floor (FOUND empty or unparseable: version unknown).
elbencho_version_error() {
    local what="$1" found="${2-}" hint="$3"
    if [[ -z "$found" ]]; then
        echo "Error: cannot determine the version of ${what}; elbencho ${ELBENCHO_MIN_VERSION} or newer is required." >&2
    elif elbencho_version_tuple "$found" >/dev/null; then
        echo "Error: ${what} is elbencho ${found}; ${ELBENCHO_MIN_VERSION} or newer is required." >&2
    else
        echo "Error: ${what} reports unparseable elbencho version '${found}'; ${ELBENCHO_MIN_VERSION} or newer is required." >&2
    fi
    echo "Fix: ${hint}" >&2
    return 1
}

# elbencho_check_binary_version BINARY: fail with the error above unless the
# binary is at least ELBENCHO_MIN_VERSION.
elbencho_check_binary_version() {
    local binary="$1" found=""
    found=$(elbencho_version_of_binary "$binary") || found=""
    elbencho_version_at_least "$found" "$ELBENCHO_MIN_VERSION" && return 0
    elbencho_version_error "$binary" "$found" \
        "run utils/build/fetch_elbencho.sh --force-download (or supply elbencho ${ELBENCHO_MIN_VERSION}+ at that path)"
}
