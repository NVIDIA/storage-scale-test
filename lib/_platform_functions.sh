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
