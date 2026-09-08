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
# It runs elbencho netbench commands with the service hosts in a list.

export output_dir="$1"
export bidirectional="$2"
export SSH_NODELIST="$3"
export NETBENCH_PORT="$4"
export NETBENCH_BLOCKSIZE="$5"
export NETBENCH_RESPSIZE="$6"
export NETBENCH_HOST_NIC_GBPS="$7"
export NETBENCH_TARGET_RUNTIME="$8"
export NETBENCH_ITERATIONS="$9"
# Thread list is passed as comma-separated string in $10
threads_str="${10}"

export ELBENCHO=./elbencho

# Parse threads CSV string into array
IFS=',' read -ra NETBENCH_THREADS <<< "$threads_str"
export NETBENCH_THREADS

# shellcheck disable=SC1091
source "_netbench_functions.sh"

# Run the benchmark loop: iterations → thread counts
for ((iteration = 1; iteration <= NETBENCH_ITERATIONS; iteration++)); do
    echo "=== Iteration $iteration of $NETBENCH_ITERATIONS ==="

    for thread_count in "${NETBENCH_THREADS[@]}"; do
        echo "  Threads: $thread_count"

        # Export per-invocation variables
        export thread_count iteration

        run_netbench_half_half
    done
done

echo "Netbench sweep complete."
