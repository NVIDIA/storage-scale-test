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

"""One capacity budget for the single-host integration fixture."""

MIB = 1024**2
GIB = 1024**3

STORAGE_TEST_CAPACITY_BYTES = 2 * GIB
SSH_HOME_CAPACITY_BYTES = 64 * MIB
MAX_DEPLOYMENT_CONTENT_BYTES = 512 * MIB
MAX_LIVE_CAPTURE_DATASET_BYTES = 64 * MIB
RESULT_AND_REPORT_HEADROOM_BYTES = 448 * MIB

NFS_BUDGET_BYTES = (
    STORAGE_TEST_CAPACITY_BYTES
    + SSH_HOME_CAPACITY_BYTES
    + MAX_DEPLOYMENT_CONTENT_BYTES
    + MAX_LIVE_CAPTURE_DATASET_BYTES
    + RESULT_AND_REPORT_HEADROOM_BYTES
)
NFS_IMAGE_BYTES = ((NFS_BUDGET_BYTES + GIB - 1) // GIB) * GIB

STORAGE_TEST_CAPACITY = f"{STORAGE_TEST_CAPACITY_BYTES // GIB}Gi"
SSH_HOME_CAPACITY = f"{SSH_HOME_CAPACITY_BYTES // MIB}Mi"
