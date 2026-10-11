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

"""Shared expected catalog and module loading for integration scenario tests.

Integration modules must be loaded once per process: enum members only compare
equal within one load, so every test module obtains the same module objects
through these helpers rather than reloading them under the same name.
"""

import importlib
import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_LIB = _REPO_ROOT / "integration-tests" / "lib"
_DRIVER_PATH = _REPO_ROOT / "integration-tests" / "bin" / "integration-test.py"
_DRIVER_NAME = "integration_driver_shared"


def load_integration_lib(name):
    """Return the process-wide integration-tests/lib module *name*."""
    if str(_INTEGRATION_LIB) not in sys.path:
        sys.path.insert(0, str(_INTEGRATION_LIB))
    return importlib.import_module(name)


def load_integration_driver():
    """Return the process-wide integration driver module, loading it once."""
    if _DRIVER_NAME in sys.modules:
        return sys.modules[_DRIVER_NAME]
    load_integration_lib("filesystem_scenario_specs")
    spec = importlib.util.spec_from_file_location(_DRIVER_NAME, _DRIVER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_DRIVER_NAME] = module
    spec.loader.exec_module(module)
    return module


EXPECTED_SCENARIO_NAMES = (
    "baseline",
    "mdtest-sweep",
    "default-dio",
    "failure-resume",
    "mixed-batch",
    "retained-lifecycle",
    "live-capture",
    "slurm-cartesian",
    "ssh-single-big-file",
    "ssh-weighted-roots",
    "ssh-shared-home",
    "kubectl-retained-read",
    "kubectl-cancel",
    "kubectl-coordinator-loss",
    "kubectl-endpoint-drift",
    "kubectl-multi-target",
    "kubectl-multi-target-unbound",
    "kubectl-same-namespace-targets",
    "slurm-scheduling",
)
