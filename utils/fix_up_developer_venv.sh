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

# Create or refresh the repository .venv with the pinned dependencies used by
# Python analysis tools. This is a convenience for local development and IDEs;
# runtime wrappers call the same setup_python_venv helper automatically.

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" &>/dev/null && pwd) || {
    echo "Error: Failed to determine script directory" >&2
    exit 1
}
if [[ ! -d "${SCRIPT_DIR}" ]]; then
    echo "Error: Script directory '${SCRIPT_DIR}' does not exist" >&2
    exit 1
fi
readonly SCRIPT_DIR

if ! source_output=$("$SHELL" -c ". '${SCRIPT_DIR}/../env.sh'" 2>&1); then
    printf '%s\n\nFailed to source env.sh; fix ^^^^^^^^^^\n' "$source_output"
    exit 1
fi
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/../env.sh"

python_path=$(setup_python_venv) || exit 1

echo "Developer virtual environment ready." >&2
echo "Interpreter: ${python_path}" >&2
echo "Configure your editor or IDE to use the interpreter above." >&2
