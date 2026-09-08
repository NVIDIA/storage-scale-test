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

# This file runs on the first node in the SSH_NODELIST.
# It runs elbencho metadata operations with the service hosts in a list.

export output_dir="$1"
export tasks_per_node="$2"
export SSH_NODELIST="$3"
export test_dirs_csv="$4"
export MDTEST_BRANCH_FACTOR="$5"
export MDTEST_ITEMS_PER_DIR="$6"
export MDTEST_ITERATIONS="$7"
single_dir_target_files="${8:-}"
single_dir_files_per_worker="${9:-}"

export ELBENCHO=./elbencho

# shellcheck disable=SC1091
source "_elbencho_functions.sh"
_mdtest_export_layout_env "$single_dir_target_files" "$single_dir_files_per_worker"
run_elbencho_metadata_benchmark

