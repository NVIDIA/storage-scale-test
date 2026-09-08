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

# One-shot remote delete for nv-elbencho-sweep --delete-only (first SSH host).
# Argument: path to delete (strict subdirectory under TEST_DIRS).
#
# `ensure_all_ssh_nodes_can_elbencho` copies `_elbencho_functions.sh` into the remote
# login directory; match the normal sweep by anchoring there before sourcing.

cd "${HOME:-.}" || {
    echo "Error: cd HOME failed" >&2
    exit 1
}

delete_path="$1"
export ELBENCHO=./elbencho

# shellcheck disable=SC1091
source "_elbencho_functions.sh"
delete_path_parallel_elbencho_sweep "$delete_path"
