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

# Install packages inside an integration-fixture Docker build.
#
# Usage: apt-build.sh PACKAGE...
#
# Optional repository mirrors replace only the URI of the image's own Ubuntu
# sources (deb822 .sources and legacy .list), so suites, components, signing
# keys and architecture are preserved. Unset mirrors leave the image's public
# configuration untouched. Environment:
#   APT_ARCHIVE_MIRROR   replaces http(s)://[*.]archive.ubuntu.com/ubuntu
#   APT_SECURITY_MIRROR  replaces http(s)://security.ubuntu.com/ubuntu
#   APT_PORTS_MIRROR     replaces http(s)://ports.ubuntu.com/ubuntu-ports
#   APT_CA_FILE          CA bundle used for HTTPS during this build only
#                        (default: the apt_ca BuildKit secret, when mounted;
#                        never written into the image)
#   APT_ROOT             filesystem root to configure (default /; for tests)
#   APT_BUDGET_SECONDS   total wall-clock budget for update + install
#   APT_ATTEMPTS         attempts per phase
#
# When any mirror is configured, every public Ubuntu URI in the image's sources
# must be covered by one, each checked on its own even when a line lists
# several (so a partial configuration cannot leave a public route), and
# a mirror that cannot be applied or reached fails the build; it never silently
# falls back to the public repositories. APT's own finite timeouts bound each
# stall; the budget only ends the whole build, so slow but progressing
# downloads are never killed mid-install to be restarted.
#
# apt-get update exits 0 when a source fails, so its output is inspected: the
# update fails (and is retried) only when a failed index belongs to an Ubuntu
# source (public archive/security/ports.ubuntu.com or a configured mirror
# host). Failures of non-Ubuntu sources the base image carries, such as the
# Slinky login image's Kubernetes repository, are tolerated, as bare apt-get
# does.

set -euo pipefail

readonly URL_PATTERN='^https?://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$'
root=${APT_ROOT:-}
root=${root%/}
budget=${APT_BUDGET_SECONDS:-540}
attempts=${APT_ATTEMPTS:-3}
started=$(date +%s)
rewrites=0
# Hosts whose index failures matter; third-party sources a base image may carry
# (for example a Kubernetes repository) are tolerated, as bare apt-get does.
ubuntu_hosts='([A-Za-z0-9-]+\.)*(archive|security|ports)\.ubuntu\.com'

die() {
    echo "apt-build: $*" >&2
    exit 1
}

validate_mirror() {
    local name=$1 value=$2
    [[ -z "$value" || "$value" =~ $URL_PATTERN ]] ||
        die "$name is not a plain http(s) URL: refusing it"
}

source_files() {
    local file
    for file in "$root"/etc/apt/sources.list.d/*.sources \
        "$root"/etc/apt/sources.list.d/*.list "$root"/etc/apt/sources.list; do
        [[ -f "$file" ]] && printf '%s\n' "$file"
    done
}

# rewrite_kind MIRROR HOST_REGEX PATH_PREFIX: swap one public URI prefix.
rewrite_kind() {
    local mirror=$1 host=$2 prefix=$3 file pattern
    [[ -n "$mirror" ]] || return 0
    mirror=${mirror%/}
    pattern="https?://${host}${prefix}"
    while IFS= read -r file; do
        if grep -Eq "$pattern" "$file"; then
            sed -E -i "s|${pattern}|${mirror}|g" "$file"
            rewrites=$((rewrites + 1))
        fi
    done < <(source_files)
}

# uncovered_public_uri URI MIRROR...: true when URI names a public Ubuntu host
# and does not start with a configured mirror (which may itself be public).
uncovered_public_uri() {
    local uri=$1 mirror
    shift
    [[ "$uri" =~ ^https?://([A-Za-z0-9-]+\.)*(archive|security|ports)\.ubuntu\.com ]] ||
        return 1
    for mirror in "$@"; do
        mirror=${mirror%/}
        # Match at a path boundary so a mirror cannot cover a longer host.
        [[ -z "$mirror" || ( "$uri" != "$mirror" && "$uri" != "$mirror/"* ) ]] ||
            return 1
    done
    return 0
}

# With any mirror configured, a public Ubuntu URI left in a source file means a
# download could bypass the cache. Every URI is checked on its own, because one
# deb822 URIs: field (or one line) may list several repositories.
reject_uncovered_public_sources() {
    local file uri
    while IFS= read -r file; do
        while IFS= read -r uri; do
            if uncovered_public_uri "$uri" "$@"; then
                die "public Ubuntu repository $uri in $file is not covered by a configured mirror"
            fi
        done < <(grep -oE 'https?://[^][:space:]"'\''<>]+' "$file" || true)
    done < <(source_files)
}

configure_mirrors() {
    local archive=${APT_ARCHIVE_MIRROR:-} security=${APT_SECURITY_MIRROR:-}
    local ports=${APT_PORTS_MIRROR:-}
    validate_mirror APT_ARCHIVE_MIRROR "$archive"
    validate_mirror APT_SECURITY_MIRROR "$security"
    validate_mirror APT_PORTS_MIRROR "$ports"
    [[ -n "$archive$security$ports" ]] || return 0
    local mirror host
    for mirror in "$archive" "$security" "$ports"; do
        host=${mirror#*://}
        host=${host%%[/:]*}
        [[ -z "$host" ]] || ubuntu_hosts+="|${host//./\\.}"
    done
    rewrite_kind "$archive" '([A-Za-z0-9-]+\.)*archive\.ubuntu\.com' '/ubuntu'
    rewrite_kind "$security" 'security\.ubuntu\.com' '/ubuntu'
    rewrite_kind "$ports" 'ports\.ubuntu\.com' '/ubuntu-ports'
    ((rewrites > 0)) ||
        die "no Ubuntu repository source matched the configured mirrors"
    reject_uncovered_public_sources "$archive" "$security" "$ports"
    echo "apt-build: using configured mirrors for $rewrites source rewrite(s)"
}

remaining() {
    echo $((budget - ($(date +%s) - started)))
}

# ubuntu_index_ok OUTPUT_FILE: apt-get update exits 0 when a source fails to
# download, so an incomplete Ubuntu index must be detected from its output.
ubuntu_index_ok() {
    ! grep -Eq "^(Err:|[EW]: Failed to fetch).*https?://(${ubuntu_hosts})([/: ]|$)" "$1"
}

# bounded LABEL VALIDATOR COMMAND...: retry failures within the time budget; a
# command only succeeds if it exits 0 and VALIDATOR accepts its output file.
bounded() {
    local label=$1 validator=$2 attempt left output
    shift 2
    output=$(mktemp)
    for ((attempt = 1; attempt <= attempts; attempt++)); do
        left=$(remaining)
        ((left > 0)) || die "$label: build time budget of ${budget}s exhausted"
        if timeout "$left" "$@" 2>&1 | tee "$output" &&
            "$validator" "$output"; then
            rm -f "$output"
            return 0
        fi
        echo "apt-build: $label attempt $attempt/$attempts failed" >&2
    done
    die "$label failed after $attempts attempts (mirrors: ${APT_ARCHIVE_MIRROR:-public} ${APT_SECURITY_MIRROR:-public} ${APT_PORTS_MIRROR:-public})"
}

# APT downloads as its _apt sandbox user, which must be able to read the CA.
require_ca_readable_by_apt() {
    local file=$1
    [[ "$(id -u)" -eq 0 ]] && id -u _apt >/dev/null 2>&1 || return 0
    command -v runuser >/dev/null 2>&1 || return 0
    runuser -u _apt -- test -r "$file" ||
        die "CA bundle $file is not readable by _apt; mount the secret with mode=0444"
}

main() {
    (($# > 0)) || die "usage: apt-build.sh PACKAGE..."
    configure_mirrors
    local ca_file=${APT_CA_FILE:-/run/secrets/apt_ca}
    local -a options=(
        -o Acquire::Retries=2
        -o Acquire::http::Timeout=30
        -o Acquire::https::Timeout=30
    )
    if [[ -n "${APT_CA_FILE:-}" && ! -s "$APT_CA_FILE" ]]; then
        die "CA bundle $APT_CA_FILE is missing or empty"
    fi
    if [[ -s "$ca_file" ]]; then
        require_ca_readable_by_apt "$ca_file"
        options+=(-o "Acquire::https::CaInfo=$ca_file")
    fi
    export DEBIAN_FRONTEND=noninteractive
    bounded "apt-get update" ubuntu_index_ok apt-get "${options[@]}" update
    bounded "apt-get install" true apt-get "${options[@]}" install -y \
        --no-install-recommends "$@"
}

main "$@"
