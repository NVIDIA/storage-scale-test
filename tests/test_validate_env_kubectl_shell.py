#!/usr/bin/env python3

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

"""User-facing kubectl diagnostics from validate_env.sh."""

import os
from pathlib import Path
import shutil
import subprocess
import textwrap

_ROOT = Path(__file__).resolve().parent.parent
_BASH = shutil.which("bash") or "/bin/bash"


def test_macos_kubectl_prerequisite_failure_names_tools_and_formulae(tmp_path):
    """Darwin validation identifies every missing prefixed launcher tool."""
    source = (_ROOT / "validate_env.sh").read_text(encoding="utf-8")
    function = source.split("check_macos_launcher_prerequisites() {", 1)[1].split(
        "\n}", 1
    )[0]
    script = textwrap.dedent(f"""\
        register_error() {{ printf 'ERROR:%s\\n' "$1"; }}
        uname() {{ printf 'Darwin\\n'; }}
        EXECUTION_SUBSTRATE=kubectl
        check_macos_launcher_prerequisites() {{{function}
        }}
        check_macos_launcher_prerequisites
        """)
    result = subprocess.run(
        [_BASH, "-c", script],
        check=False,
        env={**os.environ, "PATH": str(tmp_path)},
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    for command in ("grealpath", "gstat", "gtimeout", "gtar", "flock"):
        assert command in result.stdout
    assert "brew install bash coreutils gnu-tar flock" in result.stdout


def test_root_workload_identity_and_missing_test_dirs_are_actionable(tmp_path):
    """The validator reports kubectl configuration errors without irrelevant notes."""
    deployment = tmp_path / "deployment"
    functions_dir = deployment / "storage-tests" / "fs" / "kubectl"
    functions_dir.mkdir(parents=True)
    shutil.copy2(_ROOT / "validate_env.sh", deployment / "validate_env.sh")
    shutil.copy2(
        _ROOT
        / "storage-tests"
        / "fs"
        / "kubectl"
        / "_nv-elbencho-kubectl-functions.sh",
        functions_dir / "_nv-elbencho-kubectl-functions.sh",
    )
    results = tmp_path / "results"
    logs = tmp_path / "logs"
    results.mkdir()
    logs.mkdir()
    (deployment / "env.sh").write_text(
        textwrap.dedent(f"""\
            RESULTS_DIR={str(results)!r}
            LOGS_DIR={str(logs)!r}
            KUBECTL_ENABLED=1
            SLURM_ENABLED=
            SSH_ENABLED=
            FS_ENABLED=
            OBJ_ENABLED=
            declare -A TEST_DIRS=()
            KUBECTL_NAMESPACE=pawan-lustre
            KUBECTL_PV=pawan-lustre-pv
            KUBECTL_PVC=lustre-pvc-example
            KUBECTL_NODE_SELECTOR=lustre-host=true
            KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.1-11
            KUBECTL_IMAGE_PULL_POLICY=IfNotPresent
            KUBECTL_RUN_AS_USER=0
            KUBECTL_RUN_AS_GROUP=0
            validate_integer_array() {{ :; }}
            validate_elbencho_io_sizes() {{ :; }}
            validate_elbencho_file_workload_env() {{ :; }}
            validate_elbencho_duration() {{ :; }}
            validate_elbencho_live_csv() {{ :; }}
            validate_elbencho_single_big_file_env() {{ :; }}
            """),
        encoding="utf-8",
    )
    result = subprocess.run(
        [deployment / "validate_env.sh"],
        check=False,
        cwd=deployment,
        env={**os.environ, "SHELL": _BASH},
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "KUBECTL_RUN_AS_USER=0 must be a positive numeric UID" in result.stdout
    assert "KUBECTL_RUN_AS_GROUP=0 must be a positive numeric GID" in result.stdout
    assert "requires at least one nonempty TEST_DIRS entry" in result.stdout
    assert "neither Slurm nor SSH" not in result.stdout
    assert "FS not enabled" not in result.stdout
    assert "OBJ not enabled" not in result.stdout
    assert "invalid Kubernetes filesystem sweep configuration" not in result.stdout
