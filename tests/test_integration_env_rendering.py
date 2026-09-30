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

"""Contracts for integration environment rendering."""

import sys
from pathlib import Path
from types import SimpleNamespace

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPOSITORY_ROOT / "integration-tests" / "lib"))

import filesystem_integration as _INTEGRATION  # pylint: disable=wrong-import-position


def test_runtime_environment_selects_each_substrate_explicitly(tmp_path):
    """Fixture environments never rely on SSH host-list inference."""
    fixture = SimpleNamespace(
        architecture="x86_64",
        ssh_home_mode="separate",
        ssh_addresses=("worker-a", "worker-b"),
        slurm_nodes=("compute-0", "compute-1"),
    )
    for substrate in ("ssh", "slurm"):
        rendered, _ = _INTEGRATION._override_block(  # pylint: disable=protected-access
            substrate, str(tmp_path), fixture
        )
        assert f"export EXECUTION_SUBSTRATE={substrate}" in rendered
