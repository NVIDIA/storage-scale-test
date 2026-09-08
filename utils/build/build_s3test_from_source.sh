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

set -Eeuo pipefail

readonly ALPINE_IMAGE="alpine:3.24"
declare -ra S3TEST_CFLAGS=(
    -D_GNU_SOURCE
    -g0
    -Os
    -pipe
    -Wall
    -Wextra
    -ffunction-sections
    -fdata-sections
)
declare -ra S3TEST_LDFLAGS=(
    -static
    -s
    "-Wl,--gc-sections"
)

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd) || {
    echo "Error: Failed to determine script directory" >&2
    exit 1
}
readonly SCRIPT_DIR
REPO_ROOT=$(cd "${SCRIPT_DIR}/../.." &>/dev/null && pwd) || {
    echo "Error: Failed to determine repository root" >&2
    exit 1
}
readonly REPO_ROOT
UTILS_DIR="${REPO_ROOT}/utils"
readonly UTILS_DIR
SOURCE_FILE="${SCRIPT_DIR}/s3-test.c"
readonly SOURCE_FILE

TMP_ROOT=""
BUILD_SOURCE=""
FORCE_BUILD=false
declare -a REQUESTED_ARCHES=()

usage() {
    cat << EOF
Usage: $0 [--force] [--arch amd64|arm64|all]

Builds static Linux s3test binaries from ${SOURCE_FILE}:
  ${UTILS_DIR}/s3test
  ${UTILS_DIR}/s3test.aarch64

By default, each binary is built only when missing or older than s3-test.c.

Options:
  --force           Rebuild even when binaries are current.
  --arch <arch>     Build only amd64/x86_64, arm64/aarch64, or all.
  -h, --help        Display this help message and exit.
EOF
    return 0
}

warn() {
    echo "WARNING: $*" >&2
    return 0
}

die() {
    echo "Error: $*" >&2
    exit 1
}

info() {
    echo "==> $*" >&2
    return 0
}

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

cleanup() {
    local rc=$?
    if [[ -n "${TMP_ROOT}" && -d "${TMP_ROOT}" ]]; then
        chmod -R u+w "${TMP_ROOT}" 2>/dev/null ||:
        rm -rf "${TMP_ROOT}" 2>/dev/null ||:
        if [[ -d "${TMP_ROOT}" ]] && command_exists docker && docker info >/dev/null 2>&1; then
            # shellcheck disable=SC2016  # $1 is expanded by the inner sh -c.
            docker run --rm \
                -v "$(dirname "${TMP_ROOT}"):/cleanup-parent" \
                "${ALPINE_IMAGE}" \
                sh -c 'chmod -R u+w "$1" 2>/dev/null || true; rm -rf "$1"' \
                _ "/cleanup-parent/$(basename "${TMP_ROOT}")" >/dev/null 2>&1 ||:
        fi
        if [[ -d "${TMP_ROOT}" ]]; then
            warn "Failed to remove temporary directory: ${TMP_ROOT}"
        fi
    fi
    exit "$rc"
}
trap 'cleanup' EXIT HUP INT TERM

make_temp_root() {
    local parent_dir=${S3TEST_BUILD_TMPDIR:-${REPO_ROOT}/tmp}
    local temp_root

    mkdir -p "${parent_dir}" || die "Failed to create temporary parent directory ${parent_dir}"
    temp_root=$(mktemp -d "${parent_dir%/}/s3test-build.XXXXXXXXXX")
    printf "%s\n" "${temp_root}"
    return 0
}

normalize_arch() {
    local arch=$1

    case "${arch}" in
        amd64|x86_64)
            echo "amd64"
            return 0
            ;;
        arm64|aarch64)
            echo "arm64"
            return 0
            ;;
        all)
            echo "all"
            return 0
            ;;
        *)
            die "Unsupported architecture '${arch}'. Use amd64, arm64, or all."
            ;;
    esac
}

add_arch() {
    local arch=$1
    local existing

    for existing in "${REQUESTED_ARCHES[@]}"; do
        if [[ "${existing}" == "${arch}" ]]; then
            return 0
        fi
    done
    REQUESTED_ARCHES+=("${arch}")
    return 0
}

target_filename() {
    local arch=$1

    case "${arch}" in
        amd64)
            echo "s3test"
            return 0
            ;;
        arm64)
            echo "s3test.aarch64"
            return 0
            ;;
        *)
            die "Internal error: unknown architecture '${arch}'"
            ;;
    esac
}

target_platform() {
    local arch=$1

    case "${arch}" in
        amd64)
            echo "linux/amd64"
            return 0
            ;;
        arm64)
            echo "linux/arm64"
            return 0
            ;;
        *)
            die "Internal error: unknown architecture '${arch}'"
            ;;
    esac
}

target_display() {
    local arch=$1

    case "${arch}" in
        amd64)
            echo "x86_64"
            return 0
            ;;
        arm64)
            echo "aarch64"
            return 0
            ;;
        *)
            die "Internal error: unknown architecture '${arch}'"
            ;;
    esac
}

host_target_arch() {
    case "$(uname -m)" in
        x86_64|amd64)
            echo "amd64"
            return 0
            ;;
        aarch64|arm64)
            echo "arm64"
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

output_path_for_arch() {
    local arch=$1
    local filename

    filename=$(target_filename "${arch}")
    printf "%s/%s\n" "${UTILS_DIR}" "${filename}"
    return 0
}

should_build_arch() {
    local arch=$1
    local output_path

    output_path=$(output_path_for_arch "${arch}")
    if [[ "${FORCE_BUILD}" == true ]]; then
        return 0
    fi
    if [[ ! -x "${output_path}" ]]; then
        return 0
    fi
    if [[ "${SOURCE_FILE}" -nt "${output_path}" ]]; then
        return 0
    fi
    return 1
}

strip_binary() {
    local arch=$1
    local output_path=$2
    local log_path=$3
    local strip_tool
    local -a strip_tools=()

    case "${arch}" in
        amd64)
            strip_tools+=(x86_64-linux-musl-strip x86_64-linux-gnu-strip)
            ;;
        arm64)
            strip_tools+=(aarch64-linux-musl-strip aarch64-linux-gnu-strip)
            ;;
    esac
    strip_tools+=(strip llvm-strip)

    for strip_tool in "${strip_tools[@]}"; do
        if ! command_exists "${strip_tool}"; then
            continue
        fi
        if "${strip_tool}" --strip-all "${output_path}" >>"${log_path}" 2>&1; then
            chmod 0755 "${output_path}" || return 1
            return 0
        fi
    done

    # The compiler link command also uses -s; lack of a separate strip tool should not fail the build.
    chmod 0755 "${output_path}" || return 1
    return 0
}

compile_with_compiler() {
    local arch=$1
    local compiler=$2
    local output_path=$3
    local log_path=$4

    : > "${log_path}"
    if "${compiler}" "${S3TEST_CFLAGS[@]}" "${S3TEST_LDFLAGS[@]}" \
        -o "${output_path}" "${BUILD_SOURCE}" \
        -lssl -lcrypto -lz -ldl -pthread >>"${log_path}" 2>&1; then
        strip_binary "${arch}" "${output_path}" "${log_path}" || return 1
        return 0
    fi
    if "${compiler}" "${S3TEST_CFLAGS[@]}" "${S3TEST_LDFLAGS[@]}" \
        -o "${output_path}" "${BUILD_SOURCE}" \
        -lssl -lcrypto -lz -pthread >>"${log_path}" 2>&1; then
        strip_binary "${arch}" "${output_path}" "${log_path}" || return 1
        return 0
    fi
    if "${compiler}" "${S3TEST_CFLAGS[@]}" "${S3TEST_LDFLAGS[@]}" \
        -o "${output_path}" "${BUILD_SOURCE}" \
        -lssl -lcrypto -pthread >>"${log_path}" 2>&1; then
        strip_binary "${arch}" "${output_path}" "${log_path}" || return 1
        return 0
    fi
    return 1
}

build_with_local_compiler() {
    local arch=$1
    local output_path=$2
    local host_arch=""
    local compiler
    local log_path
    local -a compilers=()

    host_arch=$(host_target_arch ||:)
    if [[ "$(uname -s)" == "Linux" && "${host_arch}" == "${arch}" ]]; then
        compilers+=(cc gcc clang)
    fi

    case "${arch}" in
        amd64)
            compilers+=(x86_64-linux-musl-gcc x86_64-linux-gnu-gcc)
            ;;
        arm64)
            compilers+=(aarch64-linux-musl-gcc aarch64-linux-gnu-gcc)
            ;;
    esac

    for compiler in "${compilers[@]}"; do
        if ! command_exists "${compiler}"; then
            continue
        fi

        log_path="${TMP_ROOT}/local-${arch}-${compiler//\//_}.log"
        info "Trying local compiler '${compiler}' for $(target_display "${arch}")"
        if compile_with_compiler "${arch}" "${compiler}" "${output_path}" "${log_path}"; then
            info "Built $(target_filename "${arch}") with ${compiler}"
            return 0
        fi
    done

    return 1
}

build_with_docker_run() {
    local arch=$1
    local output_path=$2
    local platform
    local filename
    local log_path

    if ! command_exists docker || ! docker info >/dev/null 2>&1; then
        return 1
    fi

    platform=$(target_platform "${arch}")
    filename=$(target_filename "${arch}")
    log_path="${TMP_ROOT}/docker-run-${arch}.log"

    info "Trying Docker ${ALPINE_IMAGE} for ${platform}"
    # shellcheck disable=SC2016  # $1, HOST_UID, and HOST_GID are expanded inside the container.
    if docker run --rm \
        --platform "${platform}" \
        -e "HOST_UID=$(id -u)" \
        -e "HOST_GID=$(id -g)" \
        -v "${TMP_ROOT}:/work" \
        -w /work \
        "${ALPINE_IMAGE}" \
        sh -c 'set -eu
            apk add --no-cache build-base linux-headers openssl-dev openssl-libs-static zlib-static >/dev/null
            gcc -D_GNU_SOURCE -g0 -Os -pipe -Wall -Wextra -ffunction-sections -fdata-sections \
                -static -s -Wl,--gc-sections -o "/work/out/$1" \
                /work/src/s3-test.c -lssl -lcrypto -lz -ldl -pthread
            strip --strip-all "/work/out/$1" 2>/dev/null || true
            chown "$HOST_UID:$HOST_GID" "/work/out/$1" 2>/dev/null || true
            chmod 0755 "/work/out/$1"' \
        _ "${filename}" >"${log_path}" 2>&1; then
        if [[ -s "${output_path}" ]]; then
            return 0
        fi
    fi

    return 1
}

write_buildx_dockerfile() {
    local context_dir=$1

    cat > "${context_dir}/Dockerfile" << 'DOCKERFILE'
FROM alpine:3.24
ARG HOST_UID=0
ARG HOST_GID=0
RUN apk add --no-cache build-base linux-headers openssl-dev openssl-libs-static zlib-static
COPY s3-test.c /src/s3-test.c
RUN gcc -D_GNU_SOURCE -g0 -Os -pipe -Wall -Wextra -ffunction-sections -fdata-sections \
        -static -s -Wl,--gc-sections -o /s3test \
        /src/s3-test.c -lssl -lcrypto -lz -ldl -pthread && \
    strip --strip-all /s3test 2>/dev/null || true
RUN mkdir -p /out && cp /s3test /out/s3test && \
    chown "${HOST_UID}:${HOST_GID}" /out/s3test 2>/dev/null || true
DOCKERFILE
    return 0
}

build_with_docker_buildx() {
    local arch=$1
    local output_path=$2
    local platform
    local context_dir
    local output_dir
    local log_path

    if ! command_exists docker || ! docker buildx version >/dev/null 2>&1; then
        return 1
    fi

    platform=$(target_platform "${arch}")
    context_dir="${TMP_ROOT}/buildx-${arch}"
    output_dir="${TMP_ROOT}/buildx-out-${arch}"
    log_path="${TMP_ROOT}/docker-buildx-${arch}.log"
    mkdir -p "${context_dir}" "${output_dir}" || return 1
    cp "${BUILD_SOURCE}" "${context_dir}/s3-test.c" || return 1
    write_buildx_dockerfile "${context_dir}"

    info "Trying Docker Buildx for ${platform}"
    if docker buildx build \
        --platform "${platform}" \
        --build-arg "HOST_UID=$(id -u)" \
        --build-arg "HOST_GID=$(id -g)" \
        --output "type=local,dest=${output_dir}" \
        "${context_dir}" >"${log_path}" 2>&1; then
        if [[ -s "${output_dir}/s3test" ]]; then
            cp "${output_dir}/s3test" "${output_path}" || return 1
            chmod 0755 "${output_path}" || return 1
            return 0
        fi
    fi

    return 1
}

install_binary() {
    local source_path=$1
    local dest_path=$2
    local tmp_dest="${dest_path}.tmp.$$"

    cp "${source_path}" "${tmp_dest}" || die "Failed to copy ${source_path} to ${tmp_dest}"
    chmod 0755 "${tmp_dest}" || die "Failed to chmod ${tmp_dest}"
    mv "${tmp_dest}" "${dest_path}" || die "Failed to install ${dest_path}"
    info "Installed ${dest_path}"
    return 0
}

print_failure_context() {
    local arch=$1
    local log_path

    for log_path in \
        "${TMP_ROOT}/docker-run-${arch}.log" \
        "${TMP_ROOT}/docker-buildx-${arch}.log"; do
        if [[ -s "${log_path}" ]]; then
            warn "Last lines from ${log_path}:"
            tail -40 "${log_path}" >&2 ||:
        fi
    done
    return 0
}

build_arch() {
    local arch=$1
    local output_path

    output_path="${TMP_ROOT}/out/$(target_filename "${arch}")"

    rm -f "${output_path}"
    if build_with_local_compiler "${arch}" "${output_path}"; then
        return 0
    fi
    if build_with_docker_run "${arch}" "${output_path}"; then
        return 0
    fi
    if build_with_docker_buildx "${arch}" "${output_path}"; then
        return 0
    fi

    print_failure_context "${arch}"
    warn "Unable to build s3test for $(target_display "${arch}")."
    warn "Object storage testing will not work on $(target_display "${arch}") systems without utils/$(target_filename "${arch}")."
    return 1
}

parse_args() {
    local arch

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --force)
                FORCE_BUILD=true
                shift
                ;;
            --arch)
                if [[ $# -lt 2 ]]; then
                    die "--arch requires an argument"
                fi
                arch=$(normalize_arch "$2")
                if [[ "${arch}" == "all" ]]; then
                    add_arch amd64
                    add_arch arm64
                else
                    add_arch "${arch}"
                fi
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "Unknown option: $1"
                ;;
        esac
    done

    if [[ "${#REQUESTED_ARCHES[@]}" -eq 0 ]]; then
        REQUESTED_ARCHES=(amd64 arm64)
    fi
    return 0
}

prepare_temp_tree() {
    mkdir -p "${TMP_ROOT}/src" "${TMP_ROOT}/out" || die "Failed to create temporary build directories"
    BUILD_SOURCE="${TMP_ROOT}/src/s3-test.c"
    cp "${SOURCE_FILE}" "${BUILD_SOURCE}" || die "Failed to copy ${SOURCE_FILE} into ${TMP_ROOT}"
    return 0
}

main() {
    local arch
    local dest_path
    local failed=0

    parse_args "$@"

    [[ -f "${SOURCE_FILE}" ]] || die "Missing source file: ${SOURCE_FILE}"
    TMP_ROOT=$(make_temp_root) || die "Failed to create temporary build directory"
    prepare_temp_tree

    for arch in "${REQUESTED_ARCHES[@]}"; do
        dest_path=$(output_path_for_arch "${arch}")
        if ! should_build_arch "${arch}"; then
            echo "  using existing utils/$(target_filename "${arch}")"
            continue
        fi

        info "Building utils/$(target_filename "${arch}") from ${SOURCE_FILE}"
        if build_arch "${arch}"; then
            install_binary "${TMP_ROOT}/out/$(target_filename "${arch}")" "${dest_path}"
        else
            failed=1
        fi
    done

    return "${failed}"
}

main "$@"
