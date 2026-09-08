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

# Silence stdout from env.sh (it may echo SLURM hints, CSV samples, etc.); keep stderr for errors.
if ! source_output=$("$SHELL" -c ". '${SCRIPT_DIR}/../env.sh'" 2>&1 >/dev/null); then
    printf "%s\n\nFailed to source env.sh; fix ^^^^^^^^^^\n" "$source_output"
    exit 1
fi
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/../env.sh" >/dev/null

export PARTITION="${partition:-}"

PYTHON_SCRIPT="${SCRIPT_DIR}/sinfo_to_node_gbps_csv.py"

echo "Running sinfo_to_node_gbps_csv..." >&2
python3 "$PYTHON_SCRIPT" "$@" || error_exit "Failed to generate node Gbps CSV"
