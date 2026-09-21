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

set -euo pipefail

expected_arch=${1:?usage: ci-integration.sh <amd64|arm64> [nfs|sbx-shared]}
storage_backend=${2:-nfs}
case "${expected_arch}:$(uname -m)" in
    amd64:x86_64|arm64:aarch64) ;;
    *)
        echo "runner architecture mismatch: expected $expected_arch, found $(uname -m)" >&2
        exit 1
        ;;
esac
case "$storage_backend" in
    nfs|sbx-shared) ;;
    *)
        echo "unsupported integration storage backend: $storage_backend" >&2
        exit 1
        ;;
esac

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)
readonly repo_root
readonly driver=${INTEGRATION_DRIVER:-$repo_root/integration-tests/bin/integration-test.py}
readonly state_dir=${INTEGRATION_STATE_DIR:-$repo_root/tmp/integration-state}
readonly diagnostics=${INTEGRATION_DIAGNOSTICS:-$repo_root/tmp/integration-diagnostics}
python_bin=${INTEGRATION_PYTHON:-}
if [[ -z "$python_bin" ]]; then
    python_bin=$(command -v python3) || {
        echo "python3 is required for the integration lifecycle" >&2
        exit 1
    }
fi
readonly python_bin
readonly privilege_command=${INTEGRATION_PRIVILEGE_COMMAND:-sudo}
cleanup_started=0

run_teardown() {
    local log_path=${1:-}
    local teardown_rc=0
    local teardown_log_fd
    if [[ -n "$log_path" ]]; then
        if exec {teardown_log_fd}>"$log_path"; then
            "$python_bin" "$driver" --storage-backend "$storage_backend" teardown \
                >&"$teardown_log_fd" 2>&1 || teardown_rc=$?
            exec {teardown_log_fd}>&-
            return "$teardown_rc"
        fi
        echo "Warning: cannot write teardown log $log_path; using stderr" >&2
    fi
    "$python_bin" "$driver" --storage-backend "$storage_backend" teardown \
        || teardown_rc=$?
    return "$teardown_rc"
}

cleanup() {
    local original_rc=$1
    local first_rc=0
    local second_rc=0
    local diagnostics_ready=0
    local first_log=""
    local second_log=""
    (( cleanup_started )) && return "$original_rc"
    cleanup_started=1
    trap - EXIT INT TERM
    set +e
    if mkdir -p -- "$diagnostics" && [[ -d "$diagnostics" ]]; then
        diagnostics_ready=1
        first_log="$diagnostics/teardown-1.log"
        second_log="$diagnostics/teardown-2.log"
    else
        echo "Warning: diagnostics path is unavailable: $diagnostics" >&2
    fi
    if (( diagnostics_ready )) && [[ -d "$state_dir/logs" ]]; then
        cp -a -- "$state_dir/logs" "$diagnostics/pre-teardown-logs" \
            || echo "Warning: could not preserve pre-teardown logs" >&2
    fi
    if (( diagnostics_ready )) && [[ -d "$state_dir/test-runs" ]]; then
        cp -a -- "$state_dir/test-runs" "$diagnostics/test-runs" \
            || echo "Warning: could not preserve test-run diagnostics" >&2
    fi
    run_teardown "$first_log" || first_rc=$?
    run_teardown "$second_log" || second_rc=$?
    if (( original_rc == 0 && (first_rc != 0 || second_rc != 0) )); then
        return 1
    fi
    return "$original_rc"
}

finish() {
    local original_rc=$?
    local final_rc
    trap - EXIT INT TERM
    cleanup "$original_rc" || final_rc=$?
    final_rc=${final_rc:-0}
    exit "$final_rc"
}

trap finish EXIT
trap 'exit 124' TERM
trap 'exit 130' INT

cd -- "$repo_root"
"$python_bin" "$driver" --storage-backend "$storage_backend" setup
"$python_bin" "$driver" --storage-backend "$storage_backend" setup
"$python_bin" "$driver" --storage-backend "$storage_backend" stop
"$python_bin" "$driver" --storage-backend "$storage_backend" start
for action in setup test; do
    if "$privilege_command" env INTEGRATION_EXPECT_ROOT_REJECTION=1 \
            "$python_bin" "$driver" --storage-backend "$storage_backend" "$action"; then
        echo "integration $action action unexpectedly accepted root" >&2
        exit 1
    fi
done
"$python_bin" "$driver" --storage-backend "$storage_backend" test
