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


# The in-cluster sequencer (multi-target Kubernetes attempts) needs an image
# with `kubectl` and `sh`. Both storage profiles preload the product's default
# sequencer image (KUBECTL_DEFAULT_SEQUENCER_IMAGE in
# storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh, written with its
# docker.io/ prefix there; a unit test keeps the two equal). Its kubectl minor
# can be newer than a profile's cluster: the sequencer's only API calls (get
# job -o jsonpath, patch job --type=json) are stable across versions. The
# digest is the multi-architecture image index digest, as for the Elbencho
# image.
SEQUENCER_UPSTREAM_IMAGE = (
    "alpine/kubectl:1.37.0@"
    "sha256:954b65dea0eb140219b440413ede17c2eca205866df350d3c1fbc84d28af14ae"
)
SEQUENCER_BACKENDS = ("nfs", "sbx-shared")
SEQUENCER_FIXTURE_REPOSITORY = "docker.io/library/storage-scale-integration-kubectl"


def kubectl_sequencer_fixture(backend: str) -> FixtureImage:
    """Return the sequencer image identity for one storage backend profile.

    Every profile uses the product's default image. KUBECTL_SEQUENCER_IMAGE
    must be digest-qualified, so the fixture-private node alias is itself a
    digest reference to the pinned index digest. The nodes hold it under that
    exact name; no pull is ever attempted.
    """
    if backend not in SEQUENCER_BACKENDS:
        raise ValueError(f"no sequencer image for storage backend {backend!r}")
    digest = SEQUENCER_UPSTREAM_IMAGE.rpartition("@")[2]
    return FixtureImage(
        upstream=SEQUENCER_UPSTREAM_IMAGE,
        fixture=f"{SEQUENCER_FIXTURE_REPOSITORY}@{digest}",
        component="kubectl-sequencer",
    )
