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
        || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4) )); then
    printf 'Bash 4.4 or newer is required; on macOS run: brew install bash\n' >&2
    exit 1
fi

set -uo pipefail

# Pinned elbencho release and its upstream archive SHA-256 values.
readonly ELBENCHO_VERSION_TAG='v3.4-1'
readonly ELBENCHO_RELEASE_URL_BASE="https://github.com/breuner/elbencho/releases/download/${ELBENCHO_VERSION_TAG}"
readonly ELBENCHO_SHA256_X86_64="0ff66bd031fe5cb0004bf74e40e13a6e99ee8961dc9393afd6d48d196db1e328"
readonly ELBENCHO_SHA256_AARCH64="a5a18384c432a30e871cc94e0b87b0e7434dc0d1a251cbe12abd1afe2c1dd30e"

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd) || {
    echo "Error: Failed to determine script directory" >&2
    exit 2
}
readonly SCRIPT_DIR
# shellcheck source=lib/project_version.sh
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../lib/project_version.sh" || exit 2
# shellcheck source=lib/_platform_functions.sh
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../lib/_platform_functions.sh" || exit 2
project_version_option "$SCRIPT_DIR/../.." "$@"
UTILS_DIR=$(cd "${SCRIPT_DIR}/.." &>/dev/null && pwd) || {
    echo "Error: Failed to determine utils directory" >&2
    exit 2
}
readonly UTILS_DIR

usage() {
    cat << EOF
Usage: $0 [--force-download]
       $0 --version

Downloads and verifies the pinned elbencho ${ELBENCHO_VERSION_TAG} static binaries
into ${UTILS_DIR}. An existing binary is kept when it is elbencho
${ELBENCHO_MIN_VERSION} or newer (from --version, or from the .version stamp
written beside each download); otherwise it is replaced.
--force-download replaces every binary.
Exits 1 if a download fails, 2 on a usage or configuration error.
EOF
    return 0
}

_warn() {
    echo "WARNING: $*" >&2
    return 0
}

_error() {
    echo "Error: $*" >&2
    return 0
}

elbencho_sha256_for_arch() {
    local arch="$1"

    case "${arch}" in
        x86_64)
            echo "${ELBENCHO_SHA256_X86_64}"
            ;;
        aarch64)
            echo "${ELBENCHO_SHA256_AARCH64}"
            ;;
        *)
            _error "No elbencho checksum is configured for architecture '${arch}'"
            return 1
            ;;
    esac
    return 0
}

calculate_sha256() {
    local file_path="$1"
    local output

    if type -P sha256sum >/dev/null 2>&1; then
        output=$(sha256sum "${file_path}") || return 1
        echo "${output%% *}"
    elif type -P shasum >/dev/null 2>&1; then
        output=$(shasum -a 256 "${file_path}") || return 1
        echo "${output%% *}"
    elif type -P openssl >/dev/null 2>&1; then
        output=$(openssl dgst -sha256 "${file_path}") || return 1
        echo "${output##* }"
    else
        _error "Cannot verify downloads: sha256sum, shasum, or openssl is required"
        return 1
    fi
    return 0
}

validate_expected_sha256() {
    local expected="$1"
    local display_name="$2"

    if [[ ! "${expected}" =~ ^[[:xdigit:]]{64}$ ]]; then
        _error "Refusing to use ${display_name}: replace checksum placeholder '${expected}' with its published SHA-256 value"
        return 1
    fi
    return 0
}

verify_sha256() {
    local file_path="$1"
    local expected="$2"
    local display_name="$3"
    local actual expected_lower actual_lower

    validate_expected_sha256 "${expected}" "${display_name}" || return 1

    actual=$(calculate_sha256 "${file_path}") || return 1
    expected_lower=$(printf '%s' "${expected}" | tr '[:upper:]' '[:lower:]')
    actual_lower=$(printf '%s' "${actual}" | tr '[:upper:]' '[:lower:]')
    if [[ "${actual_lower}" != "${expected_lower}" ]]; then
        _error "SHA-256 verification failed for ${display_name}: expected ${expected_lower}, got ${actual_lower}"
        return 1
    fi
    return 0
}

validate_elbencho_checksum_configuration() {
    local arch expected

    elbencho_version_at_least "${ELBENCHO_VERSION_TAG}" "${ELBENCHO_MIN_VERSION}" || {
        _error "Pinned elbencho ${ELBENCHO_VERSION_TAG} is below the minimum ${ELBENCHO_MIN_VERSION}"
        return 1
    }

    for arch in x86_64 aarch64; do
        expected=$(elbencho_sha256_for_arch "${arch}") || return 1
        if ! validate_expected_sha256 "${expected}" "elbencho ${ELBENCHO_VERSION_TAG} ${arch}"; then
            return 1
        fi
    done
    return 0
}

# Record the release and SHA-256 of a binary this script installed in
# BINARY.version, so a host that cannot run the binary can still tell its version.
write_version_stamp() {
    local binary="$1" sha stamp="$1.version"

    sha=$(calculate_sha256 "${binary}") || return 1
    if ! printf '%s %s\n' "${ELBENCHO_VERSION_TAG}" "${sha}" > "${stamp}.$$" \
            || ! mv -f "${stamp}.$$" "${stamp}"; then
        rm -f "${stamp}.$$"
        return 1
    fi
    return 0
}

# Succeed (printing the version) when an existing binary is at the floor or newer;
# print what was found and fail otherwise.
existing_binary_is_current() {
    local binary="$1" found

    found=$(elbencho_version_of_binary "${binary}") || found=""
    if elbencho_version_at_least "${found}" "${ELBENCHO_MIN_VERSION}"; then
        echo "${found}"
        return 0
    fi
    echo "${found:-unknown}"
    return 1
}

# Download, verify, and atomically install one elbencho binary. Silent; the
# caller reports the outcome.
# Arguments: $1=arch ("x86_64" or "aarch64"), $2=output_path
download_and_extract_elbencho() {
    local arch="$1"
    local output_path="$2"
    local url="${ELBENCHO_RELEASE_URL_BASE}/elbencho-static-${arch}.tar.gz"
    local tmpdir
    local extracted
    local archive
    local expected_sha256
    local staged

    if ! tmpdir="$(mktemp -d 2>/dev/null)"; then
        return 1
    fi

    archive="${tmpdir}/elbencho.tar.gz"
    if ! curl -sL -f -o "${archive}" "$url"; then
        rm -rf "$tmpdir"
        return 1
    fi

    expected_sha256=$(elbencho_sha256_for_arch "${arch}") || {
        rm -rf "$tmpdir"
        return 1
    }
    if ! verify_sha256 "${archive}" "${expected_sha256}" "elbencho ${ELBENCHO_VERSION_TAG} ${arch}"; then
        rm -rf "$tmpdir"
        return 1
    fi

    if ! tar -xzf "${archive}" -C "$tmpdir"; then
        rm -rf "$tmpdir"
        return 1
    fi

    extracted="$(find "$tmpdir" -name 'elbencho' -type f 2>/dev/null | head -1)"
    if [[ -z "$extracted" ]]; then
        rm -rf "$tmpdir"
        return 1
    fi

    # Stage beside the destination so the final mv is an atomic rename.
    staged="${output_path}.download.$$"
    if ! cp "$extracted" "$staged" || ! chmod +x "$staged" \
            || ! mv -f "$staged" "$output_path"; then
        rm -f "$staged"
        rm -rf "$tmpdir"
        return 1
    fi
    rm -rf "$tmpdir"
    # A missing stamp only costs a re-download later, so it never fails this one.
    write_version_stamp "$output_path" || true
    return 0
}

main() {
    local force_download=false
    local spec arch output_path display_name name i existing_version
    local download_start_time download_elapsed
    # Per entry: arch|output_path|display_name
    local -a binaries_to_download=(
        "x86_64|${UTILS_DIR}/elbencho|elbencho amd64"
        "aarch64|${UTILS_DIR}/elbencho.aarch64|elbencho arm64"
    )
    local -a pids=() display_names=() skipped=() succeeded=() failed=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force-download)
                force_download=true
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                _error "Unknown option: $1"
                usage >&2
                exit 2
                ;;
        esac
        shift
    done

    validate_elbencho_checksum_configuration || exit 2

    echo "Downloading elbencho ${ELBENCHO_VERSION_TAG} binaries..."
    download_start_time=$(date +%s)
    for spec in "${binaries_to_download[@]}"; do
        IFS='|' read -r arch output_path display_name <<< "${spec}"

        if [[ -f "$output_path" ]] && [[ "$force_download" == false ]]; then
            if existing_version=$(existing_binary_is_current "$output_path"); then
                skipped+=("${display_name}|${existing_version}")
                continue
            fi
            echo "  replacing ${display_name}: version ${existing_version}, below ${ELBENCHO_MIN_VERSION}"
        fi

        display_names+=("$display_name")
        (download_and_extract_elbencho "$arch" "$output_path") &
        pids+=("$!")
    done

    for name in ${skipped[@]+"${skipped[@]}"}; do
        echo "  using existing ${name%%|*} (version ${name##*|}; add --force-download to override)"
    done

    for i in "${!pids[@]}"; do
        if wait "${pids[$i]}"; then
            succeeded+=("${display_names[$i]}")
        else
            failed+=("${display_names[$i]}")
        fi
    done

    download_elapsed=$(( $(date +%s) - download_start_time ))
    for name in ${succeeded[@]+"${succeeded[@]}"}; do
        echo "  successfully downloaded ${name}"
    done
    for name in ${failed[@]+"${failed[@]}"}; do
        _warn "Failed to download ${name}"
    done
    echo "  Downloads completed in ${download_elapsed} seconds."

    [[ "${#failed[@]}" -eq 0 ]] || exit 1
    exit 0
}

main "$@"
