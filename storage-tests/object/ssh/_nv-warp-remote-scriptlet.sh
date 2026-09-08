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

# This file will run on the first node in the SSH_NODELIST.

# It will run the warp main command with the service hosts in a list.

echo "warp remote args: $*"

export output_dir="$1"
export obj_size="$2"
export put_duration="$3"
export put_min_objs="$4"
export max_get_time="$5"
export multipart="$6"
export ranged="$7"
export SSH_NODELIST="$8"
export WARP_RPS_BUDGET_GET="${9}"
export WARP_RPS_BUDGET_PUT="${10}"
export WARP_RANGE_OBJ_SIZE="${11}"
export WARP_PREFIXES="${12}"
export OBJ_BUCKET="${13}"
export OBJ_REGION="${14}"
export OBJ_HOST="${15}"
export OBJ_HOST_PORT="${16}"
export S3_EXPRESS="${17}"
# shellcheck disable=SC2034  # s3_express is read by _build_warp_conn_args in _warp_functions.sh
s3_express="${S3_EXPRESS:-false}"

shift 17
export thread_list=("$@")

export WARP=./warp
export S3TEST=./s3test

# Source the credentials from a file that was previously copied over.
# Export explicitly in case the auth file uses bare assignments without "export".
# shellcheck disable=SC1091
. .obj_auth
export WARP_ACCESS_KEY WARP_SECRET_KEY
export AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-$WARP_ACCESS_KEY}"
export AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-$WARP_SECRET_KEY}"

# shellcheck disable=SC1091
source "_warp_functions.sh"
run_warp_io_sweep_iteration
