#!/bin/bash

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
#
# One-node batch job for nv-elbencho-sweep --delete-only (SLURM).
# Argument: path to delete (strict subdirectory under TEST_DIRS).

if [[ -z "${SLURM_JOB_ID}" ]]; then
    echo "This script should be run via slurm."
    exit 1
fi

# Find SCALE_TEST_BASE from submit dir (same pattern as _nv-elbencho-coordinator.sh).
dir="${SLURM_SUBMIT_DIR}"
while [[ "$dir" != "/" && ! -d "$dir/storage-tests" ]]; do
    dir="$(dirname "$dir")"
done
[[ "$dir" == "/" ]] && {
    echo "Error: Could not find SCALE_TEST_BASE" >&2
    exit 1
}
SCALE_TEST_BASE="$dir"

if ! source_output=$("$SHELL" -c ". ${SCALE_TEST_BASE}/env.sh" 2>&1); then
    printf "%s\n\nFailed to source env.sh; fix ^^^^^^^^^^\n" "$source_output"
    exit 1
fi

# shellcheck disable=SC1091
source "${SCALE_TEST_BASE}/env.sh"

delete_path="${1:-}"
if [[ -z "$delete_path" ]]; then
    echo "ERROR: first argument must be the path to delete" >&2
    exit 1
fi

# shellcheck disable=SC1091
source "${SCALE_TEST_BASE}/lib/_elbencho_functions.sh"
delete_path_parallel_elbencho_sweep "$delete_path"
