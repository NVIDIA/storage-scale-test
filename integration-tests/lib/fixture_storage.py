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

"""Shared identities of the fixture's storage targets.

The fixture's primary RWX claim lives in the integration namespace. Multi-target
Kubernetes scenarios also need a second claim in a second namespace, and the
aggregate scenario a third claim beside the primary one; setup, teardown, and
the scenario driver must agree on every name below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?")
MAX_DNS_LABEL = 63
STORAGE_CLAIM = "storage-test-rwx"
SECOND_NAMESPACE_SUFFIX = "-target-2"
# A second claim in the primary namespace, for targets that share a namespace.
SAME_NAMESPACE_CLAIM = "storage-test-rwx-2"

SBX_DATA_PV = "storage-scale-sbx-data"
SBX_SECOND_DATA_PV = "storage-scale-sbx-data-2"
SBX_DATA_DIRECTORY = "storage-test"
SBX_SECOND_DATA_DIRECTORY = "storage-test-2"
SBX_SAME_NAMESPACE_DATA_PV = "storage-scale-sbx-data-3"
SBX_SAME_NAMESPACE_DATA_DIRECTORY = "storage-test-3"
SBX_SHARED_DIRECTORIES = (
    SBX_DATA_DIRECTORY,
    SBX_SECOND_DATA_DIRECTORY,
    SBX_SAME_NAMESPACE_DATA_DIRECTORY,
    "ssh-home",
)

# A scenario-owned claim that can never bind: its volumeName names no PV.
UNBOUND_CLAIM = "storage-test-unbound"
UNBOUND_PV = "storage-scale-unbound-pv"


class StorageTargetError(ValueError):
    """A storage target identity is malformed."""


@dataclass(frozen=True)
class KubectlStorageTarget:
    """One Kubernetes namespace, bound PV, and claim."""

    namespace: str
    pv: str
    pvc: str

    def __post_init__(self) -> None:
        for field in (self.namespace, self.pv, self.pvc):
            if not field or "/" in field or any(char.isspace() for char in field):
                raise StorageTargetError(f"invalid storage target field: {field!r}")

    def key(self, root: str, *, with_namespace: bool = False) -> str:
        """Return the TEST_DIRS key naming *root* on this claim.

        Without the namespace the key relies on KUBECTL_NAMESPACE.
        """
        prefix = f"{self.namespace}/" if with_namespace else ""
        return f"{prefix}{self.pvc}:{root}"

    def default_name(self, root: str) -> str:
        """Return the product's default STORAGE_TARGET_NAME for one root."""
        return self.key(root, with_namespace=True)


def second_namespace(namespace: str) -> str:
    """Return the fixture's second storage namespace for *namespace*."""
    candidate = f"{namespace}{SECOND_NAMESPACE_SUFFIX}"
    if len(candidate) > MAX_DNS_LABEL or not DNS_LABEL.fullmatch(candidate):
        raise StorageTargetError(
            f"cannot derive a DNS-1123 second namespace from {namespace!r}"
        )
    return candidate


def kubectl_target_env_lines(
    namespace: str, sequencer_image: str = ""
) -> tuple[str, ...]:
    """Return the Kubernetes lines of a scenario's env.sh.

    TEST_DIRS keys name their own claim, so the environment selects only the
    default namespace for keys without one. It never sets KUBECTL_PVC or
    KUBECTL_PV, which the product rejects since v2.0.0. A batch
    whose target files span several namespaces needs the sequencer, and with it
    its digest-pinned image; one namespace runs without either, so the caller
    passes no image.
    """
    if not DNS_LABEL.fullmatch(namespace):
        raise StorageTargetError(f"invalid namespace: {namespace!r}")
    lines = (f"export KUBECTL_NAMESPACE={namespace}",)
    if sequencer_image:
        if not re.search(r"@sha256:[0-9a-f]{64}$", sequencer_image):
            raise StorageTargetError(
                "the sequencer image must be digest-qualified: " + repr(sequencer_image)
            )
        lines += (f"export KUBECTL_SEQUENCER_IMAGE={sequencer_image}",)
    return lines
