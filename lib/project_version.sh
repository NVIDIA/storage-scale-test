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

# Project version resolution for every Bash and Python entry point; see
# docs/VERSIONING.md. VERSION and SOURCE_SHA256 are read as data, never sourced.
# Also runs as a command: project_version.sh resolve ROOT | stamp ROOT VERSION

# True for a release tag: vMAJOR.MINOR.PATCH without leading zeros.
_project_release_tag() {
    local numeric='(0|[1-9][0-9]*)'
    [[ "$1" =~ ^v${numeric}\.${numeric}\.${numeric}$ ]]
}

# True for any version this file can produce.
_project_valid_version() {
    local value="${1%-modified}"
    [[ "$value" == unknown || "$value" =~ ^(untagged-[0-9]+|shallow)-g[0-9a-f]{12,40}$ ]] \
        && return 0
    if [[ "$value" =~ ^(v.+)-[0-9]+-g[0-9a-f]{12,40}$ ]]; then
        value=${BASH_REMATCH[1]}
    fi
    _project_release_tag "$value"
}

_project_sha256sum() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$@"
    else
        shasum -a 256 "$@"
    fi
}

# Version of a Git checkout, from the nearest reachable annotated release tag.
_project_git_version() {
    local root="$1" top sha shallow version='' tag status
    local matches=()
    if ! top=$(git -C "$root" rev-parse --show-toplevel 2>&1); then
        printf 'Warning: cannot read Git metadata in %s: %s\n' "$root" "${top%%$'\n'*}" >&2
        return 1
    fi
    [[ "$top" == "$root" ]] || return 1
    sha=$(git -C "$root" rev-parse --short=12 HEAD) || return 1
    # Without --tags, describe already ignores lightweight tags.
    while IFS= read -r tag; do
        ! _project_release_tag "$tag" || matches+=(--match "$tag")
    done < <(git -C "$root" tag --list 'v*')
    shallow=$(git -C "$root" rev-parse --is-shallow-repository)
    if [[ ${#matches[@]} -gt 0 ]]; then
        if [[ "$shallow" == true ]]; then
            # Truncated history cannot count commits, so only an exact tag counts.
            matches+=(--exact-match)
        fi
        version=$(git -C "$root" describe --abbrev=12 "${matches[@]}" HEAD 2>/dev/null) || version=''
    fi
    if [[ -z "$version" && "$shallow" == true ]]; then
        version="shallow-g$sha"
    elif [[ -z "$version" ]]; then
        version="untagged-$(git -C "$root" rev-list --count HEAD)-g$sha"
    fi
    status=$(git --no-optional-locks -C "$root" status --porcelain --untracked-files=normal) \
        || status=unreadable
    if [[ -n "$status" ]]; then
        version+=-modified
    fi
    printf '%s\n' "$version"
}

# Version of a stamped deployment or source archive.
_project_archive_version() {
    local root="$1" version
    if [[ ! -f "$root/VERSION" ]]; then
        printf 'Warning: %s has neither Git metadata nor a VERSION file\n' "$root" >&2
        printf 'unknown\n'
        return 0
    fi
    version=$(< "$root/VERSION")
    if ! _project_valid_version "$version"; then
        printf 'Warning: malformed %s/VERSION\n' "$root" >&2
        printf 'unknown\n'
        return 0
    fi
    if ! (cd "$root" && _project_sha256sum --check --status SOURCE_SHA256) >/dev/null 2>&1; then
        printf 'Warning: files in %s do not match its SOURCE_SHA256\n' "$root" >&2
        version="${version%-modified}-modified"
    fi
    printf '%s\n' "$version"
}

# Print the version of the project tree at ROOT.
project_version() {
    local root
    root=$(cd "$1" 2>/dev/null && pwd -P) || {
        printf 'unknown\n'
        return 0
    }
    # Only the tree's own repository counts; an enclosing one never does.
    if [[ -e "$root/.git" ]] && _project_git_version "$root"; then
        return 0
    fi
    _project_archive_version "$root"
}

# Resolve once for this process and export the result for the helpers that
# record it beside each benchmark result (see lib/_platform_functions.sh).
project_version_export() {
    STORAGE_SCALE_TEST_VERSION=$(project_version "$1")
    export STORAGE_SCALE_TEST_VERSION
}

# Command-line helper: print ROOT's version and exit when --version appears
# among the remaining arguments (before any "--").
project_version_option() {
    local root="$1" argument
    shift
    for argument in "$@"; do
        case "$argument" in
            --) return 0 ;;
            --version)
                project_version "$root"
                exit 0
                ;;
        esac
    done
    return 0
}

# Information-only help must precede configuration and reporting setup.
project_help_requested() {
    local argument
    for argument in "$@"; do
        case "$argument" in
            --) break ;;
            -h|--help) return 0 ;;
        esac
    done
    return 1
}

# Files a deployment is expected to add or replace: site configuration,
# credentials, separately obtained benchmark binaries, and runtime output.
_project_unstamped() {
    case "$1" in
        VERSION|SOURCE_SHA256|env.sh|.obj_auth|utils/elbencho*|utils/warp*|utils/s3test*|utils/mdtest*) return 0 ;;
        .venv*|tmp/*|results/*|__pycache__/*|*/__pycache__/*) return 0 ;;
    esac
    return 1
}

# Write VERSION and SOURCE_SHA256 into a Git-free copy of the project.
project_stamp() {
    local root="$1" version="$2" relative
    local files=()
    if ! _project_valid_version "$version"; then
        printf 'Error: invalid project version: %s\n' "$version" >&2
        return 1
    fi
    while IFS= read -r -d '' relative; do
        relative=${relative#./}
        ! _project_unstamped "$relative" || continue
        if [[ "$relative" == *[$'\n\\']* ]]; then
            printf 'Error: cannot checksum file name: %q\n' "$relative" >&2
            return 1
        fi
        files+=("$relative")
    done < <(cd "$root" && find . -type f -print0 | LC_ALL=C sort -z)
    if [[ ${#files[@]} -eq 0 ]]; then
        printf 'Error: no files to stamp in %s\n' "$root" >&2
        return 1
    fi
    (cd "$root" && _project_sha256sum -- "${files[@]}" > SOURCE_SHA256) || return 1
    printf '%s\n' "$version" > "$root/VERSION"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    case "${1:-}" in
        resolve) project_version "${2:?usage: project_version.sh resolve ROOT}" ;;
        stamp) project_stamp "${2:?usage: project_version.sh stamp ROOT VERSION}" "${3:?}" ;;
        *)
            printf 'Usage: project_version.sh resolve ROOT | stamp ROOT VERSION\n' >&2
            exit 1
            ;;
    esac
fi
