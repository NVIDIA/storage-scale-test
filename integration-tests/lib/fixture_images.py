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

from dataclasses import dataclass


@dataclass(frozen=True)
class FixtureImage:
    """One upstream image and its fixture-private containerd alias."""

    upstream: str
    fixture: str
    component: str


ELBENCHO_UPSTREAM_IMAGE = (
    "breuner/elbencho:v3.2-1@"
    "sha256:8177018a31ae5963c6886a28beafa780fa382b6e3935817800189abae9cad6db"
)
ELBENCHO_FIXTURE_IMAGE = "docker.io/library/storage-scale-integration-elbencho:v3.2-1"
ELBENCHO_FIXTURE = FixtureImage(
    upstream=ELBENCHO_UPSTREAM_IMAGE,
    fixture=ELBENCHO_FIXTURE_IMAGE,
    component="elbencho",
)
