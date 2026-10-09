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

"""Executable report entrypoints and dependency-free informational CLI contracts."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENTRYPOINTS = (
    "extract-filesystem.py",
    "extract-elbencho.py",
    "extract-mdtest-elbencho.py",
    "extract-netbench.py",
    "extract-warp.py",
    "summarize-elbencho.py",
    "slurm/sinfo_to_node_gbps_csv.py",
)


@pytest.fixture(name="cli_checkout")
def cli_checkout_fixture(tmp_path):
    """Copy entrypoints without an environment or importable third-party packages."""
    checkout = tmp_path / "checkout with spaces"
    shutil.copytree(
        _REPO_ROOT / "lib",
        checkout / "lib",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for relative in _ENTRYPOINTS:
        target = checkout / "utils" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO_ROOT / "utils" / relative, target)
    (checkout / "env.sh").write_text("exit 73\n", encoding="utf-8")
    (checkout / "requirements.txt").write_text(
        "invalid requirement!\n", encoding="utf-8"
    )
    return checkout


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
@pytest.mark.parametrize("option", ("--help", "--version"))
def test_informational_options_need_no_site_config_or_dependencies(
    tmp_path, cli_checkout, entrypoint, option
):
    completed = subprocess.run(
        [sys.executable, "-S", str(cli_checkout / "utils" / entrypoint), option],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    expected = (
        "usage:" if option == "--help" else "storage-scale-test unversioned source"
    )
    assert expected in completed.stdout
    assert not completed.stderr
    assert not (cli_checkout / ".venv").exists()


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_python_entrypoint_is_executable_and_replaces_its_wrapper(entrypoint):
    path = _REPO_ROOT / "utils" / entrypoint
    assert os.access(path, os.X_OK)
    assert path.read_text(encoding="utf-8").startswith("#!/usr/bin/env python3\n")
    assert not path.with_suffix(".sh").exists()


@pytest.mark.parametrize("entrypoint", _ENTRYPOINTS)
def test_importing_entrypoints_does_not_bootstrap(entrypoint):
    code = """
import importlib.util
import sys
from unittest import mock
sys.path.insert(0, sys.argv[2])
spec = importlib.util.spec_from_file_location("imported_cli_contract", sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
with mock.patch("lib.report_cli.ensure_runtime", side_effect=AssertionError("bootstrap")):
    spec.loader.exec_module(module)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            str(_REPO_ROOT / "utils" / entrypoint),
            str(_REPO_ROOT),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.fixture(name="sinfo_checkout")
def sinfo_checkout_fixture(cli_checkout, tmp_path):
    binary_directory = tmp_path / "binaries"
    binary_directory.mkdir()
    binary = binary_directory / "sinfo"
    binary.write_text(
        "#!/bin/sh\n"
        '[ "${SINFO_SITE_MARKER:-}" = loaded ] || exit 42\n'
        'if [ "$5" = idle ]; then printf \'%s-node\\n\' "$3"; fi\n',
        encoding="utf-8",
    )
    binary.chmod(0o755)
    environment_config = cli_checkout / "env.sh"
    environment_config.write_text(
        f"PATH='{binary_directory}'\n"
        "export SINFO_SITE_MARKER=loaded\n"
        "echo noisy site config\n"
        "partition='config'\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
    }
    environment.pop("PARTITION", None)
    script = cli_checkout / "utils/slurm/sinfo_to_node_gbps_csv.py"
    return cli_checkout, script, environment


@pytest.mark.parametrize("source", ("argument", "environment", "config"))
def test_sinfo_partition_precedence_and_csv_output(sinfo_checkout, source):
    _, script, environment = sinfo_checkout
    arguments = [str(script)]
    if source in ("argument", "environment"):
        environment["PARTITION"] = "environment"
    if source == "argument":
        arguments.extend(("--partition", "argument"))
    arguments.append(f"{source},40")
    completed = subprocess.run(
        arguments, env=environment, check=False, capture_output=True, text=True
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["InstanceName,Gbps", f"{source}-node,40"]


def test_sinfo_invalid_site_config_is_reported(sinfo_checkout):
    checkout, script, environment = sinfo_checkout
    (checkout / "env.sh").write_text("exit 73\n", encoding="utf-8")
    completed = subprocess.run(
        [str(script), "node,40"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    assert "failed to load" in completed.stderr
    assert not completed.stdout
