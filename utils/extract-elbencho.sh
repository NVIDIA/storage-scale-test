#!/usr/bin/env bash
set -euo pipefail

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

# Validate script directory
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd) || {
    echo "Error: Failed to determine script directory" >&2
    exit 1
}
if [[ ! -d "${SCRIPT_DIR}" ]]; then
    echo "Error: Script directory '${SCRIPT_DIR}' does not exist" >&2
    exit 1
fi
readonly SCRIPT_DIR

# Batch reporting uses saved group settings, even when current env.sh is invalid.
for input_path in "$@"; do
    [[ -d "$input_path" ]] || continue
    input_root=$(cd "$input_path" && pwd -P) || exit 1
    while [[ -n "$input_root" && "$input_root" != / ]]; do
        batch_marker_found=0
        for batch_marker in batch-manifest.tsv batch-profile.tsv \
                batch-sealed.sha256 batch-sealed-environment.sha256; do
            if [[ -e "$input_root/$batch_marker" || -L "$input_root/$batch_marker" ]]; then
                batch_marker_found=1
                break
            fi
        done
        if [[ "$batch_marker_found" == 1 ]]; then
            SCALE_TEST_BASE=$(cd "${SCRIPT_DIR}/.." && pwd)
            export SCALE_TEST_BASE
            # shellcheck source=lib/env_functions.sh
            # shellcheck disable=SC1091
            source "${SCALE_TEST_BASE}/lib/env_functions.sh"
            python_path=$(setup_python_venv) || exit 1
            exec "$python_path" "${SCRIPT_DIR}/extract-elbencho.py" "$@"
        fi
        input_root=${input_root%/*}
    done
done

if ! source_output=$("$BASH" -c "source \"\$1\"" env-loader \
        "${SCRIPT_DIR}/../env.sh" 2>&1); then
    printf "%s\n\nFailed to source env.sh; fix ^^^^^^^^^^\n" "$source_output"
    exit 1
fi
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/../env.sh"

PYTHON_SCRIPT="${SCRIPT_DIR}/extract-elbencho.py"

python_path=$(setup_python_venv) || exit 1

echo "Running extraction and plotting..." >&2
echo "$python_path" "$PYTHON_SCRIPT" "$@" >&2
"$python_path" "$PYTHON_SCRIPT" "$@" || error_exit "Failed to extract data and generate plots"
