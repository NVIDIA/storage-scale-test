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

"""Shared digest-pinned image identities for the integration fixture."""

ELBENCHO_UPSTREAM_IMAGE = (
    "breuner/elbencho:v3.1-11@"
    "sha256:719fba92cab57c773ddf7a2776414b358aeb8126a15fbc8e3c52469ce3a5b8b2"
)
ELBENCHO_FIXTURE_IMAGE = "docker.io/library/storage-scale-integration-elbencho:v3.1-11"
