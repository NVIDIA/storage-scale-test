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

set -uo pipefail

_warn() {
    echo "WARNING: $*" >&2
    return 0
}

_error() {
    echo "Error: $*" >&2
    return 0
}

_detect_tar_cmd() {
    if type -P gtar >/dev/null 2>&1; then
        echo "gtar"
    else
        echo "tar"
    fi
    return 0
}

# Work back to find root of tree
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd) || {
    _error "Failed to determine script directory"
    exit 1
}
if [[ ! -d "${SCRIPT_DIR}" ]]; then
    _error "Script directory '${SCRIPT_DIR}' does not exist"
    exit 1
fi
readonly SCRIPT_DIR

# Change dir to just inside our tree
cd "${SCRIPT_DIR}/.." || exit

# Capture our tree's dir name (no assumptions)
TREE_DIR_NAME="$(basename "$(pwd)")"

cd .. || exit

# We do NOT commit binaries into git. Any static binaries in the
# tree will get used (acting as a cache) unless "--force-download" is given.
# Failing to reach upstream sources warns but does not prevent tarball creation.

# Pinned elbencho release for static binaries included in a user-created
# deployment tarball.
ELBENCHO_VERSION_TAG='v3.1-11'
ELBENCHO_RELEASE_URL_BASE="https://github.com/breuner/elbencho/releases/download/${ELBENCHO_VERSION_TAG}"
# SHA-256 values for the pinned upstream release archives. Downloads fail
# closed unless the configured value is exactly 64 hexadecimal characters.
ELBENCHO_SHA256_X86_64="8d7cf885481dbd8f39908b7f4ff588d9e80cbc0fd26eeaba0b764c77587884d2"
ELBENCHO_SHA256_AARCH64="a744c82ab4e15d8cf4023f7f5053c2008e148f77349e2ba53d2352c4f7508683"
UTILS_DIR="${SCRIPT_DIR}" # this script is in utils/

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

    for arch in x86_64 aarch64; do
        expected=$(elbencho_sha256_for_arch "${arch}") || return 1
        if ! validate_expected_sha256 "${expected}" "elbencho ${ELBENCHO_VERSION_TAG} ${arch}"; then
            return 1
        fi
    done
    return 0
}

# Helper function: download and extract a pinned elbencho static binary from
# the upstream GitHub release. Silent on success/failure - caller handles output.
# Arguments: $1=arch ("x86_64" or "aarch64"), $2=output_path
# Returns: 0 on success, 1 on failure.
download_and_extract_elbencho() {
    local arch="$1"
    local output_path="$2"
    local url="${ELBENCHO_RELEASE_URL_BASE}/elbencho-static-${arch}.tar.gz"
    local tmpdir
    local extracted
    local archive
    local expected_sha256

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

    cp "$extracted" "$output_path"
    chmod +x "$output_path"
    rm -rf "$tmpdir"
    return 0
}

check_warp_binaries() {
    local missing=()
    local binary_path

    for binary_path in "${UTILS_DIR}/warp" "${UTILS_DIR}/warp.aarch64"; do
        if [[ ! -x "${binary_path}" ]]; then
            missing+=("utils/${binary_path##*/}")
        fi
    done

    if [[ "${#missing[@]}" -eq 0 ]]; then
        echo "  found existing warp binaries (utils/warp and utils/warp.aarch64)"
        return 0
    fi

    _warn "Missing Warp binary/binaries: ${missing[*]}"
    _warn "The tarball will still be created, but object storage testing will not work without Warp."
    _warn "Build Warp first from the NVIDIA fork (the tooling expects this fork), for example:"
    _warn "  ./utils/build/build_warp_from_source.sh"
    return 0
}

s3test_needs_build() {
    local source_path="${UTILS_DIR}/build/s3-test.c"
    local binary_path

    if [[ ! -f "${source_path}" ]]; then
        return 1
    fi

    for binary_path in "${UTILS_DIR}/s3test" "${UTILS_DIR}/s3test.aarch64"; do
        if [[ ! -x "${binary_path}" || "${source_path}" -nt "${binary_path}" ]]; then
            return 0
        fi
    done

    return 1
}

check_s3test_binaries() {
    local source_path="${UTILS_DIR}/build/s3-test.c"
    local build_script="${UTILS_DIR}/build/build_s3test_from_source.sh"
    local arch
    local binary_path
    local display_arch
    local failed=0

    if [[ ! -f "${source_path}" ]]; then
        _warn "Missing s3test source file: ${source_path}"
        _warn "Object storage testing will not work without s3test."
        return 0
    fi

    if s3test_needs_build; then
        if [[ ! -x "${build_script}" ]]; then
            _warn "Missing executable s3test build script: ${build_script}"
        else
            echo "Building s3test binaries from source..."
            if ! "${build_script}"; then
                _warn "s3test build script reported one or more build failures."
            fi
        fi
    fi

    for arch in amd64 arm64; do
        case "${arch}" in
            amd64)
                binary_path="${UTILS_DIR}/s3test"
                display_arch="x86_64"
                ;;
            arm64)
                binary_path="${UTILS_DIR}/s3test.aarch64"
                display_arch="aarch64"
                ;;
        esac

        if [[ -x "${binary_path}" && ! "${source_path}" -nt "${binary_path}" ]]; then
            echo "  found current s3test binary for ${display_arch}: utils/${binary_path##*/}"
        else
            _warn "s3test for ${display_arch} is missing or older than utils/build/s3-test.c."
            _warn "Object storage testing will not work on ${display_arch} systems without utils/${binary_path##*/}."
            failed=1
        fi
    done

    return "${failed}"
}

# Command-line argument processing
usage() {
    cat <<EOF
Usage: $(basename "$0") [--force-download] [--help | -h]

Options:
  --force-download   Force (re-)download of the elbencho binaries, even if present.
  -h, --help         Display this help message and exit.
EOF
}

force_download=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force-download)
            force_download=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

echo "Creating deployment tarball..."

# Build list of elbencho binaries to download/extract. Format per entry:
#   arch|output_path|display_name
declare -a binaries_to_download=(
    "x86_64|${UTILS_DIR}/elbencho|elbencho amd64"
    "aarch64|${UTILS_DIR}/elbencho.aarch64|elbencho arm64"
)

# Refuse to produce a deployment tarball until the pinned upstream checksums have
# been filled in. This prevents placeholder values from degrading into the
# historical warn-and-continue download behavior below.
validate_elbencho_checksum_configuration || exit 1

# Warp binaries are user-built from OSS source when object storage testing is needed.
# Warn if either architecture is missing, but keep building the tarball.
check_warp_binaries

# Build s3test from in-tree source when the binaries are missing or stale.
# If one architecture cannot be built, keep creating the tarball but warn that
# object storage testing will not work for that architecture.
check_s3test_binaries ||:

# Download all selected files concurrently.
echo "Downloading required files..."
download_start_time=$(date +%s)
declare -a pids=()
declare -a display_names=()
declare -a skipped=()
declare -a succeeded=()
declare -a failed=()

for spec in "${binaries_to_download[@]}"; do
    IFS='|' read -r arch output_path display_name <<< "${spec}"

    if [[ -f "$output_path" ]] && [[ "$force_download" == false ]]; then
        skipped+=("$display_name")
        continue
    fi

    display_names+=("$display_name")
    (download_and_extract_elbencho "$arch" "$output_path") &
    pids+=("$!")
done

for name in "${skipped[@]}"; do
    echo "  using existing ${name} (add --force-download to override)"
done

for i in "${!pids[@]}"; do
    if wait "${pids[$i]}"; then
        succeeded+=("${display_names[$i]}")
    else
        failed+=("${display_names[$i]}")
    fi
done

download_end_time=$(date +%s)
download_elapsed=$((download_end_time - download_start_time))

for name in "${succeeded[@]}"; do
    echo "  successfully downloaded ${name}"
done
for name in "${failed[@]}"; do
    _warn "Failed to download ${name}"
done
echo "  Downloads completed in ${download_elapsed} seconds."

# Warp source tarball validation removed -- warp is no longer bundled here.

echo "Creating final tarball..."

NOTICE_PATH="${TREE_DIR_NAME}/NOTICE"
if [[ ! -f "${NOTICE_PATH}" ]]; then
    _error "Missing required NOTICE file at ${NOTICE_PATH}"
    exit 1
fi

OUTPUT_TARBALL="storage-scale-test.tar.gz"
DEPLOY_DIR_NAME="storage-scale-test"

# Detect tar command early (for use in final tarball creation)
# Mac users may install gtar using Brew; detect and use that if it's available
TAR_CMD="$(_detect_tar_cmd)"

# Use transform (GNU tar) or substitution (bsdtar) to ensure consistent top-level directory
if [[ "$($TAR_CMD --version)" =~ "GNU" ]]; then
    TAR_OPTS=(
        '--transform' "s|^${TREE_DIR_NAME}|${DEPLOY_DIR_NAME}|"
    )
else
    TAR_OPTS=(
        '-s' "/^${TREE_DIR_NAME}/${DEPLOY_DIR_NAME}/"
    )
fi

# NOTICE is at the repository root and must be included in every deployment tarball.
# It is picked up with the rest of the tree; no --exclude below may match it.
echo "  including NOTICE (third-party attribution)"

$TAR_CMD czf "$OUTPUT_TARBALL" \
    "${TAR_OPTS[@]}" \
    --exclude=*.gz \
    --exclude=*.xz \
    --exclude=.git* \
    --exclude=.venv \
    --exclude=.vscode \
    --exclude=.DS_Store \
    --exclude=.pytest_cache \
    --exclude=__pycache__ \
    --exclude=*.png \
    --exclude=env.sh \
    --exclude=bin-amd64 \
    --exclude=bin-arm64 \
    --exclude=.ci-cache \
    --exclude=docs \
    --exclude=tests \
    --exclude=tmp \
    --exclude=".pylintrc*" \
    --exclude=AGENTS.md \
    --exclude=CLAUDE.md \
    --exclude=pytest.ini \
    --exclude=pyrightconfig.json \
    --exclude=utils/build/*.c \
    --exclude=utils/build_tarball.sh \
    --exclude=utils/fix_up_developer_venv.sh \
    --exclude=static-binaries \
    "${TREE_DIR_NAME}" || {
    _error "Failed to create ${OUTPUT_TARBALL}"
    exit 1
}

# Read the whole listing rather than piping into "grep -q": an early-exiting
# grep would SIGPIPE tar, which "set -o pipefail" reports as a build failure.
if ! grep -qxF "${DEPLOY_DIR_NAME}/NOTICE" <<< "$($TAR_CMD tzf "$OUTPUT_TARBALL")"; then
    _error "${OUTPUT_TARBALL} does not contain ${DEPLOY_DIR_NAME}/NOTICE"
    exit 1
fi

echo "You can now scp the file $(pwd)/${OUTPUT_TARBALL} to your slurm cluster or SSH client host."
