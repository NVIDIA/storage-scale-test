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
    deployment = tmp_path / "deploy'ment"
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
            KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
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


_ROW = "ns1|sst-elb-ab12cd34-sweep|ab12cd34|coordinator|2026-01-02T03:04:05Z"


def _run_inventory(tmp_path, fake_body):
    """Run check_kubectl_owned_resources against a fake kubectl on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "kubectl"
    fake.write_text("#!/usr/bin/env bash\n" + fake_body, encoding="utf-8")
    fake.chmod(0o755)
    source = (_ROOT / "validate_env.sh").read_text(encoding="utf-8")
    inventory = source.split("# Informational, read-only inventory", 1)[1].split(
        "check_kubectl_filesystem_prerequisites() {", 1
    )[0]
    functions = _ROOT / "storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    script = textwrap.dedent(f"""\
        source {str(functions)!r}
        register_error() {{ printf 'ERROR:%s\\n' "$1"; }}
        register_warning() {{ printf 'WARNING:%s\\n' "$1"; }}
        KUBECTL_NAMESPACE=myns
        KUBECTL_OBSERVATION_ATTEMPTS=1
        {inventory}
        check_kubectl_owned_resources
        """)
    return subprocess.run(
        [_BASH, "-c", script],
        check=False,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
    )


def test_owned_resources_none_found(tmp_path):
    """An empty cluster is reported informationally."""
    result = _run_inventory(tmp_path, "exit 0\n")
    assert result.returncode == 0
    assert "none found (all namespaces)" in result.stdout
    assert "ERROR:" not in result.stdout


def test_owned_resources_listed_without_error(tmp_path):
    """Existing objects are tabulated with run id and never register errors."""
    body = f'[[ " $* " == *" get jobs "* ]] && echo "{_ROW}"\nexit 0\n'
    result = _run_inventory(tmp_path, body)
    assert result.returncode == 0
    assert "1 found (all namespaces)" in result.stdout
    assert ["Job", "ns1"] in [line.split()[:2] for line in result.stdout.splitlines()]
    assert "ab12cd34" in result.stdout
    assert "sst-elb-ab12cd34-sweep" in result.stdout
    assert "coordinator" in result.stdout
    assert "kubernetes/attempts/<RUN>/" in result.stdout
    assert "ERROR:" not in result.stdout
    assert "WARNING:" not in result.stdout


def test_owned_resources_forbidden_all_namespaces_falls_back(tmp_path):
    """RBAC denial of -A falls back to the configured namespace."""
    body = textwrap.dedent(f"""\
        if [[ " $* " == *" -A "* ]]; then
            echo 'Error from server (Forbidden): cannot list resource' >&2
            exit 1
        fi
        [[ " $* " == *" -n myns "* && " $* " == *" get jobs "* ]] \\
            && echo "{_ROW}"
        exit 0
        """)
    result = _run_inventory(tmp_path, body)
    assert result.returncode == 0
    assert (
        "namespace myns where cluster-wide listing is forbidden: Job, DaemonSet"
        in result.stdout
    )
    assert "ab12cd34" in result.stdout
    assert "ERROR:" not in result.stdout
    assert "WARNING:" not in result.stdout


def test_owned_resources_probe_failure_warns(tmp_path):
    """API failures downgrade to a warning, not an error."""
    body = "echo 'connection refused' >&2\nexit 1\n"
    result = _run_inventory(tmp_path, body)
    assert result.returncode == 0
    assert "WARNING:Could not list Kubernetes objects" in result.stdout
    assert "ERROR:" not in result.stdout


def test_owned_resources_ignore_kubectl_stderr_warnings(tmp_path):
    """Version-skew warnings on stderr never become inventory rows."""
    body = textwrap.dedent(f"""\
        echo 'Warning: version difference between client (1.36) and server (1.34)' >&2
        [[ " $* " == *" get pods "* ]] && echo "{_ROW}"
        exit 0
        """)
    result = _run_inventory(tmp_path, body)
    assert result.returncode == 0
    assert "1 found (all namespaces)" in result.stdout
    assert ["Pod", "ns1"] in [line.split()[:2] for line in result.stdout.splitlines()]
    assert "version difference" not in result.stdout
    assert "WARNING:" not in result.stdout
