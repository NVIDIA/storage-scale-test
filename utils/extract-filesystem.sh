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
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
SCALE_TEST_BASE=$(cd "${SCRIPT_DIR}/.." && pwd)
export SCALE_TEST_BASE
# shellcheck source=lib/env_functions.sh
# shellcheck disable=SC1091
source "${SCALE_TEST_BASE}/lib/env_functions.sh"
python_path=$(setup_python_venv) || exit 1
"$python_path" "${SCRIPT_DIR}/extract-filesystem.py" "$@"
