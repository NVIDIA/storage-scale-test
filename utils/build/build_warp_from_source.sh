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

readonly MIN_GO_VERSION="1.26.5"
readonly DOCKER_GO_IMAGE="golang:${MIN_GO_VERSION}"
readonly DEFAULT_WARP_REPO_URL="https://github.com/NVIDIA/warp-minio"
readonly DEFAULT_WARP_REF="nv-main-oss"

export GIT_TERMINAL_PROMPT=0

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

TMP_ROOT=""
BUILD_LOG=""
CHECKOUT_FETCH_REF=""
WARP_BUILD_VERSION=""
WARP_RELEASE_TAG=""
WARP_RELEASE_TIME=""
WARP_COMMIT_ID=""
WARP_SHORT_COMMIT_ID=""

usage() {
    cat << EOF
Usage: $0 [<git-repo-url> <tag|branch|sha>]

Builds Warp from OSS source and installs:
  ${UTILS_DIR}/warp
  ${UTILS_DIR}/warp.aarch64

Existing binaries at those paths are overwritten atomically after successful
builds.

With no arguments, defaults to:
  ${DEFAULT_WARP_REPO_URL} ${DEFAULT_WARP_REF}

If any source argument is supplied, both <git-repo-url> and <tag|branch|sha>
must be supplied; partial defaults are not applied.

The repository URL must be a GitHub or GitLab HTTPS/SSH URL. Common HTTPS
browser URLs such as /tree/<branch>, /commit/<sha>, and GitLab /-/tree/<branch>
are normalized to their HTTPS clone URL. SSH clone URLs are preserved as SSH.

Environment overrides:
  WARP_BUILD_PACKAGE    Go package to build, for example "." or "./cmd/warp"
  WARP_BUILD_TMPDIR     Parent directory for temporary build directories

The script uses Go >= ${MIN_GO_VERSION}. It uses an adequate go from PATH, then
Docker (${DOCKER_GO_IMAGE}) if available, then a private Go installation under
a temporary directory.
EOF
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

cleanup() {
    local rc=$?
    if [[ -n "${TMP_ROOT}" && -d "${TMP_ROOT}" ]]; then
        chmod -R u+w "${TMP_ROOT}" 2>/dev/null ||:
        rm -rf "${TMP_ROOT}" 2>/dev/null ||:
        if [[ -d "${TMP_ROOT}" ]] && command_exists docker && docker info >/dev/null 2>&1; then
            # shellcheck disable=SC2016  # $1 is expanded by the inner sh -c.
            docker run --rm \
                -v "$(dirname "${TMP_ROOT}"):/cleanup-parent" \
                "${DOCKER_GO_IMAGE}" \
                sh -c 'chmod -R u+w "$1" 2>/dev/null || true; rm -rf "$1"' \
                _ "/cleanup-parent/$(basename "${TMP_ROOT}")" >/dev/null 2>&1 ||:
        fi
        if [[ -d "${TMP_ROOT}" ]]; then
            echo "Warning: Failed to remove temporary directory: ${TMP_ROOT}" >&2
        fi
    fi
    exit "$rc"
}
trap 'cleanup' EXIT HUP INT TERM

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

make_temp_root() {
    local parent_dir=${WARP_BUILD_TMPDIR:-}
    local temp_root

    if [[ -z "${parent_dir}" ]]; then
        # Docker Desktop for macOS commonly cannot bind-mount the default
        # /var/folders/... TMPDIR. Prefer the repo-local tmp directory so the
        # same temp tree works for local Go, Docker, and private Go installs.
        parent_dir="${REPO_ROOT}/tmp"
    fi

    mkdir -p "${parent_dir}" || die "Failed to create temporary parent directory ${parent_dir}"
    temp_root=$(mktemp -d "${parent_dir%/}/warp-build.XXXXXXXXXX")
    printf "%s\n" "${temp_root}"
    return 0
}

version_ge() {
    local have=$1
    local need=$2
    local have_major have_minor have_patch need_major need_minor need_patch

    IFS=. read -r have_major have_minor have_patch <<< "${have}"
    IFS=. read -r need_major need_minor need_patch <<< "${need}"

    have_patch=${have_patch:-0}
    need_patch=${need_patch:-0}

    if (( have_major > need_major )); then
        return 0
    elif (( have_major < need_major )); then
        return 1
    fi

    if (( have_minor > need_minor )); then
        return 0
    elif (( have_minor < need_minor )); then
        return 1
    fi

    if (( have_patch >= need_patch )); then
        return 0
    fi
    return 1
}

go_version_string() {
    local go_bin=$1
    local raw version

    raw=$("${go_bin}" version 2>/dev/null) || return 1
    version=$(sed -E 's/^go version go([0-9]+(\.[0-9]+){1,2}).*$/\1/' <<< "${raw}")
    [[ "${version}" =~ ^[0-9]+(\.[0-9]+){1,2}$ ]] || return 1
    echo "${version}"
    return 0
}

validate_git_host() {
    local host=$1

    case "${host}" in
        github.com|*github*|gitlab.com|*gitlab*)
            return 0
            ;;
        *)
            die "Unsupported repository host '${host}'. Only GitHub and GitLab URLs are supported."
            ;;
    esac
}

normalize_clone_url() {
    local input=$1
    local url host path authority host_port normalized_path

    url=${input%%\#*}
    url=${url%%\?*}
    url=${url%/}

    if [[ "${url}" == https://* ]]; then
        host=${url#https://}
        host=${host%%/*}
        path=${url#"https://${host}/"}
        validate_git_host "${host}"

        case "${host}" in
            github.com|*github*)
                path=${path%%/tree/*}
                path=${path%%/commit/*}
                path=${path%%/releases/*}
                ;;
            gitlab.com|*gitlab*)
                path=${path%%/-/tree/*}
                path=${path%%/-/commit/*}
                path=${path%%/tree/*}
                path=${path%%/commit/*}
                ;;
        esac

        normalized_path=${path%.git}
        [[ "${normalized_path}" != "${host}" && -n "${normalized_path}" ]] || die "Could not determine repository path from ${input}"
        [[ "${normalized_path}" == */* ]] || die "Repository URL must include an owner/group and repository name"

        printf "https://%s/%s.git\n" "${host}" "${normalized_path}"
        return 0
    fi

    if [[ "${url}" == ssh://* ]]; then
        authority=${url#ssh://}
        authority=${authority%%/*}
        path=${url#"ssh://${authority}/"}
        [[ "${path}" != "${url}" && -n "${path}" ]] || die "Could not determine SSH repository path from ${input}"

        host_port=${authority#*@}
        host=${host_port%%:*}
        validate_git_host "${host}"

        normalized_path=${path%.git}
        [[ -n "${normalized_path}" && "${normalized_path}" == */* ]] || die "SSH repository URL must include an owner/group and repository name"

        printf "ssh://%s/%s.git\n" "${authority}" "${normalized_path}"
        return 0
    fi

    if [[ "${url}" =~ ^[^/@]+@[^:]+:.+ ]]; then
        authority=${url%%:*}
        path=${url#*:}
        host=${authority#*@}
        validate_git_host "${host}"

        normalized_path=${path%.git}
        [[ -n "${normalized_path}" && "${normalized_path}" == */* ]] || die "SSH repository URL must include an owner/group and repository name"

        printf "%s:%s.git\n" "${authority}" "${normalized_path}"
        return 0
    fi

    die "Repository URL must be HTTPS or SSH, for example https://github.com/NVIDIA/warp-minio or ssh://git@gitlab.example.com:2222/group/repo.git"
}

remote_ref_exists() {
    local clone_url=$1
    local full_ref=$2

    if git ls-remote --exit-code --refs "${clone_url}" "${full_ref}" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

init_checkout() {
    local clone_url=$1
    local requested_ref=$2
    local checkout_dir=$3
    local is_sha=false
    local branch_exists=false
    local tag_exists=false
    local fetch_ref=""
    local access_output
    local checked_out_sha

    [[ -n "${requested_ref}" ]] || die "Ref must not be empty"

    if ! access_output=$(git ls-remote --exit-code "${clone_url}" HEAD 2>&1); then
        die "Unable to access ${clone_url}: ${access_output}"
    fi

    if [[ "${requested_ref}" =~ ^[0-9a-fA-F]{7,40}$ ]]; then
        is_sha=true
    fi

    info "Checking remote refs for ${requested_ref}"
    if remote_ref_exists "${clone_url}" "refs/heads/${requested_ref}"; then
        branch_exists=true
    fi
    if remote_ref_exists "${clone_url}" "refs/tags/${requested_ref}"; then
        tag_exists=true
    fi

    if [[ "${branch_exists}" == true && "${tag_exists}" == true ]]; then
        die "Remote has both a branch and a tag named '${requested_ref}'. Use the exact commit SHA."
    elif [[ "${tag_exists}" == true ]]; then
        fetch_ref="refs/tags/${requested_ref}"
    elif [[ "${branch_exists}" == true ]]; then
        fetch_ref="refs/heads/${requested_ref}"
    elif [[ "${requested_ref}" == refs/heads/* || "${requested_ref}" == refs/tags/* ]]; then
        fetch_ref="${requested_ref}"
    elif [[ "${is_sha}" == true ]]; then
        fetch_ref="${requested_ref}"
    else
        die "Could not find branch or tag '${requested_ref}' in ${clone_url}"
    fi
    CHECKOUT_FETCH_REF="${fetch_ref}"

    mkdir -p "${checkout_dir}" || die "Failed to create checkout directory ${checkout_dir}"
    git -C "${checkout_dir}" init -q
    git -C "${checkout_dir}" remote add origin "${clone_url}"

    info "Fetching ${fetch_ref} with depth=1"
    if ! git -C "${checkout_dir}" fetch --depth=1 origin "${fetch_ref}" >/dev/null 2>&1; then
        if [[ "${is_sha}" == true ]]; then
            die "Failed to fetch SHA '${requested_ref}'. Some servers reject shallow fetches by short or unadvertised SHA; try a full 40-character commit SHA from an advertised ref."
        fi
        die "Failed to fetch '${requested_ref}' from ${clone_url}"
    fi

    git -C "${checkout_dir}" checkout --detach -q FETCH_HEAD || die "Failed to check out ${requested_ref}"
    checked_out_sha=$(git -C "${checkout_dir}" rev-parse HEAD) || die "Failed to resolve checked-out commit"
    info "Checked out ${checked_out_sha}"
    return 0
}

try_git_describe() {
    local checkout_dir=$1

    git -C "${checkout_dir}" describe --tags --long --abbrev=12 HEAD 2>/dev/null
    return $?
}

format_warp_version_from_describe() {
    local describe_output=$1
    local tag commits_since_tag short_sha

    if [[ "${describe_output}" =~ ^(.+)-([0-9]+)-g([0-9a-fA-F]+)$ ]]; then
        tag=${BASH_REMATCH[1]}
        commits_since_tag=${BASH_REMATCH[2]}
        short_sha=${BASH_REMATCH[3]}
        if [[ "${commits_since_tag}" == "0" ]]; then
            echo "${tag}"
        else
            echo "${tag}-${commits_since_tag}-${short_sha}"
        fi
        return 0
    fi

    echo "${describe_output}"
    return 0
}

tag_from_git_describe() {
    local describe_output=$1

    if [[ "${describe_output}" =~ ^(.+)-([0-9]+)-g[0-9a-fA-F]+$ ]]; then
        echo "${BASH_REMATCH[1]}"
        return 0
    fi

    echo "${describe_output}"
    return 0
}

fetch_tags_for_version_metadata() {
    local checkout_dir=$1

    git -C "${checkout_dir}" fetch --force --depth=1 origin '+refs/tags/*:refs/tags/*' >/dev/null 2>&1
    return $?
}

deepen_checkout_for_version_metadata() {
    local checkout_dir=$1
    local deepen_by=$2

    [[ -n "${CHECKOUT_FETCH_REF}" ]] || return 1
    git -C "${checkout_dir}" fetch --deepen="${deepen_by}" origin "${CHECKOUT_FETCH_REF}" >/dev/null 2>&1
    return $?
}

compute_warp_build_metadata() {
    local checkout_dir=$1
    local describe_output=""
    local deepen_by

    WARP_COMMIT_ID=$(git -C "${checkout_dir}" rev-parse HEAD) || die "Failed to resolve Warp commit"
    WARP_SHORT_COMMIT_ID=${WARP_COMMIT_ID:0:12}
    WARP_RELEASE_TIME=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

    if ! fetch_tags_for_version_metadata "${checkout_dir}"; then
        info "Could not fetch tag refs for version metadata; will use commit metadata only if needed"
    fi

    describe_output=$(try_git_describe "${checkout_dir}" || :)
    for deepen_by in 50 200 1000 5000; do
        if [[ -n "${describe_output}" ]]; then
            break
        fi
        info "Deepening git history by ${deepen_by} commits to find the closest tag"
        deepen_checkout_for_version_metadata "${checkout_dir}" "${deepen_by}" || true
        describe_output=$(try_git_describe "${checkout_dir}" || :)
    done

    if [[ -n "${describe_output}" ]]; then
        WARP_BUILD_VERSION=$(format_warp_version_from_describe "${describe_output}")
        WARP_RELEASE_TAG=$(tag_from_git_describe "${describe_output}")
    else
        WARP_BUILD_VERSION="${WARP_SHORT_COMMIT_ID}"
        WARP_RELEASE_TAG="unknown"
        info "Could not find a reachable tag; using ${WARP_BUILD_VERSION} as the Warp version"
    fi

    info "Using Warp version ${WARP_BUILD_VERSION} (${WARP_SHORT_COMMIT_ID})"
    return 0
}

download_file() {
    local url=$1
    local output_path=$2

    if command_exists curl; then
        curl -fsSL "${url}" -o "${output_path}"
        return
    elif command_exists wget; then
        wget -q "${url}" -O "${output_path}"
        return
    fi
    return 127
}

install_private_go() {
    local go_root=$1
    local host_os host_arch archive url

    case "$(uname -s)" in
        Linux) host_os=linux ;;
        Darwin) host_os=darwin ;;
        *) die "Automatic Go install is unsupported on $(uname -s). Install Go >= ${MIN_GO_VERSION} or provide Docker." ;;
    esac

    case "$(uname -m)" in
        x86_64|amd64) host_arch=amd64 ;;
        aarch64|arm64) host_arch=arm64 ;;
        *) die "Automatic Go install is unsupported on $(uname -m). Install Go >= ${MIN_GO_VERSION} or provide Docker." ;;
    esac

    archive="${TMP_ROOT}/downloads/go${MIN_GO_VERSION}.${host_os}-${host_arch}.tar.gz"
    url="https://go.dev/dl/go${MIN_GO_VERSION}.${host_os}-${host_arch}.tar.gz"

    mkdir -p "$(dirname "${archive}")" "${go_root}" || die "Failed to create Go download directory"
    info "Downloading Go ${MIN_GO_VERSION} to the temporary directory"
    if ! download_file "${url}" "${archive}"; then
        die "Failed to download ${url}. Install Go >= ${MIN_GO_VERSION}, install curl/wget, or provide Docker with ${DOCKER_GO_IMAGE}."
    fi

    info "Installing private Go ${MIN_GO_VERSION}"
    tar -C "${go_root}" -xzf "${archive}" || die "Failed to extract ${archive}"
    [[ -x "${go_root}/go/bin/go" ]] || die "Private Go install did not produce ${go_root}/go/bin/go"
    echo "${go_root}/go/bin/go"
    return 0
}

select_builder() {
    local private_go_root=$1
    local version

    if command_exists go; then
        if version=$(go_version_string "$(command -v go)") && version_ge "${version}" "${MIN_GO_VERSION}"; then
            echo "local-go|$(command -v go)"
            return 0
        fi
        info "Ignoring go from PATH because it is ${version:-unknown}; need >= ${MIN_GO_VERSION}"
    fi

    if command_exists docker; then
        if docker info >/dev/null 2>&1 && docker run --rm "${DOCKER_GO_IMAGE}" go version >/dev/null 2>&1; then
            echo "docker|${DOCKER_GO_IMAGE}"
            return 0
        fi
        info "Docker is installed but ${DOCKER_GO_IMAGE} could not be run; falling back to a private Go install"
    fi

    echo "local-go|$(install_private_go "${private_go_root}")"
    return 0
}

go_with_env() {
    local cache_dir=$1
    shift

    env \
        "GOTOOLCHAIN=local" \
        "GOCACHE=${cache_dir}/go-build" \
        "GOMODCACHE=${cache_dir}/gomod" \
        "GOPATH=${cache_dir}/gopath" \
        "$@"
    return
}

local_go() {
    local go_bin=$1
    local source_dir=$2
    local cache_dir=$3
    shift 3

    (
        cd "${source_dir}" || return 1
        go_with_env "${cache_dir}" "${go_bin}" "$@"
    )
    return $?
}

docker_go() {
    local image=$1
    local tmp_root=$2
    local workdir=$3
    shift 3

    local rel_workdir
    rel_workdir=${workdir#"${tmp_root}/"}

    docker run --rm \
        --user "$(id -u):$(id -g)" \
        -e GOTOOLCHAIN=local \
        -e HOME=/work/home \
        -e "CGO_ENABLED=${CGO_ENABLED:-}" \
        -e "GOOS=${GOOS:-}" \
        -e "GOARCH=${GOARCH:-}" \
        -e GOCACHE=/work/cache/go-build \
        -e GOMODCACHE=/work/cache/gomod \
        -e GOPATH=/work/cache/gopath \
        -v "${tmp_root}:/work" \
        -w "/work/${rel_workdir}" \
        "${image}" \
        go "$@"
    return
}

go_cmd() {
    local builder_type=$1
    local builder_value=$2
    local source_dir=$3
    local cache_dir=$4
    shift 4

    case "${builder_type}" in
        local-go)
            local_go "${builder_value}" "${source_dir}" "${cache_dir}" "$@"
            ;;
        docker)
            docker_go "${builder_value}" "${TMP_ROOT}" "${source_dir}" "$@"
            ;;
        *)
            die "Internal error: unknown builder type ${builder_type}"
            ;;
    esac
    return
}

discover_build_package() {
    local builder_type=$1
    local builder_value=$2
    local source_dir=$3
    local cache_dir=$4
    local package_override=${WARP_BUILD_PACKAGE:-}
    local root_name candidates line count list_log

    if [[ -n "${package_override}" ]]; then
        echo "${package_override}"
        return 0
    fi

    list_log="${TMP_ROOT}/go-list.log"
    root_name=$(go_cmd "${builder_type}" "${builder_value}" "${source_dir}" "${cache_dir}" list -f '{{.Name}}' . 2>"${list_log}" ||:)
    if [[ "${root_name}" == "main" ]]; then
        echo "."
        return 0
    fi

    candidates=$(go_cmd "${builder_type}" "${builder_value}" "${source_dir}" "${cache_dir}" list -f '{{if eq .Name "main"}}{{.ImportPath}}{{end}}' ./... 2>>"${list_log}" | sed '/^$/d' ||:)
    if [[ -z "${candidates}" ]]; then
        echo "Error: Could not find a Go main package to build." >&2
        if [[ -s "${list_log}" ]]; then
            echo "--- go list output ---" >&2
            tail -80 "${list_log}" >&2 ||:
        fi
        die "Set WARP_BUILD_PACKAGE to the correct package path."
    fi

    while IFS= read -r line; do
        if [[ "${line}" == */cmd/warp || "${line}" == */warp ]]; then
            echo "${line}"
            return 0
        fi
    done <<< "${candidates}"

    count=$(wc -l <<< "${candidates}" | tr -d ' ')
    if [[ "${count}" == "1" ]]; then
        echo "${candidates}"
        return 0
    fi

    echo "Error: Found multiple Go main packages and could not identify Warp:" >&2
    while IFS= read -r line; do
        echo "  ${line}" >&2
    done <<< "${candidates}"
    die "Set WARP_BUILD_PACKAGE to the package to build, for example './cmd/warp'."
}

discover_module_path() {
    local builder_type=$1
    local builder_value=$2
    local source_dir=$3
    local cache_dir=$4
    local module_log module_path

    module_log="${TMP_ROOT}/go-list-module.log"
    module_path=$(go_cmd "${builder_type}" "${builder_value}" "${source_dir}" "${cache_dir}" list -m 2>"${module_log}") || {
        echo "Error: Could not determine Go module path." >&2
        if [[ -s "${module_log}" ]]; then
            echo "--- go list -m output ---" >&2
            tail -80 "${module_log}" >&2 ||:
        fi
        die "Set WARP_BUILD_PACKAGE only after verifying the Warp source checkout has a go.mod file."
    }

    [[ -n "${module_path}" ]] || die "Go module path is empty"
    echo "${module_path}"
    return 0
}

build_warp_ldflags() {
    local module_path=$1
    local pkg_path="${module_path}/pkg"
    local ldflags

    ldflags="-s -w"
    ldflags+=" -X ${pkg_path}.Version=${WARP_BUILD_VERSION}"
    ldflags+=" -X ${pkg_path}.ReleaseTag=${WARP_RELEASE_TAG}"
    ldflags+=" -X ${pkg_path}.ReleaseTime=${WARP_RELEASE_TIME}"
    ldflags+=" -X ${pkg_path}.CommitID=${WARP_COMMIT_ID}"
    ldflags+=" -X ${pkg_path}.ShortCommitID=${WARP_SHORT_COMMIT_ID}"
    ldflags+=" -X main.version=${WARP_BUILD_VERSION}"
    ldflags+=" -X main.commit=${WARP_COMMIT_ID}"
    ldflags+=" -X main.date=${WARP_RELEASE_TIME}"
    echo "${ldflags}"
    return 0
}

build_one_arch() {
    local builder_type=$1
    local builder_value=$2
    local source_dir=$3
    local cache_dir=$4
    local package=$5
    local goarch=$6
    local output_path=$7
    local ldflags=$8
    local build_output_path=${output_path}

    if [[ "${builder_type}" == "docker" ]]; then
        build_output_path="/work/${output_path#"${TMP_ROOT}/"}"
    fi

    info "Building ${output_path##*/} for linux/${goarch}"
    if ! CGO_ENABLED=0 GOOS=linux GOARCH="${goarch}" \
        go_cmd "${builder_type}" "${builder_value}" "${source_dir}" "${cache_dir}" \
            build -trimpath -ldflags="${ldflags}" -o "${build_output_path}" "${package}" \
            >"${BUILD_LOG}" 2>&1; then
        echo "Error: Failed to build linux/${goarch} Warp binary." >&2
        echo "Build log: ${BUILD_LOG}" >&2
        echo "--- log tail ---" >&2
        tail -100 "${BUILD_LOG}" >&2 ||:
        exit 1
    fi

    [[ -s "${output_path}" ]] || die "Build did not create ${output_path}"
    chmod 0755 "${output_path}" || die "Failed to mark ${output_path} executable"
    return 0
}

install_binary() {
    local source_path=$1
    local dest_path=$2
    local tmp_dest

    tmp_dest="${dest_path}.tmp.$$"
    cp "${source_path}" "${tmp_dest}" || die "Failed to copy ${source_path} to ${tmp_dest}"
    chmod 0755 "${tmp_dest}" || die "Failed to chmod ${tmp_dest}"
    mv "${tmp_dest}" "${dest_path}" || die "Failed to install ${dest_path}"
    info "Installed ${dest_path}"
    return 0
}

main() {
    local input_url
    local requested_ref

    if [[ $# -eq 0 ]]; then
        input_url="${DEFAULT_WARP_REPO_URL}"
        requested_ref="${DEFAULT_WARP_REF}"
        info "No source args supplied; using ${input_url} ${requested_ref}"
    elif [[ $# -eq 1 && ( "${1}" == "-h" || "${1}" == "--help" ) ]]; then
        usage
        exit 0
    elif [[ $# -eq 2 ]]; then
        input_url=$1
        requested_ref=$2
    else
        usage >&2
        exit 2
    fi

    local clone_url source_dir output_dir cache_dir builder builder_type builder_value package
    local module_path ldflags
    local go_version

    command_exists git || die "git is required"

    TMP_ROOT=$(make_temp_root) || die "Failed to create temp directory"
    BUILD_LOG="${TMP_ROOT}/build.log"
    source_dir="${TMP_ROOT}/src"
    output_dir="${TMP_ROOT}/out"
    cache_dir="${TMP_ROOT}/cache"
    mkdir -p "${output_dir}" "${cache_dir}" "${TMP_ROOT}/home" || die "Failed to create temp build directories"

    clone_url=$(normalize_clone_url "${input_url}")
    info "Using clone URL ${clone_url}"
    info "Temporary build directory: ${TMP_ROOT}"

    init_checkout "${clone_url}" "${requested_ref}" "${source_dir}"

    builder=$(select_builder "${TMP_ROOT}/private-go")
    builder_type=${builder%%|*}
    builder_value=${builder#*|}
    case "${builder_type}" in
        local-go)
            go_version=$(go_version_string "${builder_value}" || echo "unknown")
            info "Using Go ${go_version} at ${builder_value}"
            ;;
        docker)
            info "Using Docker image ${builder_value}"
            ;;
    esac

    package=$(discover_build_package "${builder_type}" "${builder_value}" "${source_dir}" "${cache_dir}")
    info "Building Go package ${package}"
    module_path=$(discover_module_path "${builder_type}" "${builder_value}" "${source_dir}" "${cache_dir}")
    compute_warp_build_metadata "${source_dir}"
    ldflags=$(build_warp_ldflags "${module_path}")

    build_one_arch "${builder_type}" "${builder_value}" "${source_dir}" "${cache_dir}" "${package}" amd64 "${output_dir}/warp" "${ldflags}"
    build_one_arch "${builder_type}" "${builder_value}" "${source_dir}" "${cache_dir}" "${package}" arm64 "${output_dir}/warp.aarch64" "${ldflags}"

    install_binary "${output_dir}/warp" "${UTILS_DIR}/warp"
    install_binary "${output_dir}/warp.aarch64" "${UTILS_DIR}/warp.aarch64"

    info "Warp build complete"
    return 0
}

main "$@"
