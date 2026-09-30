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

"""Shell contracts for explicit execution-substrate selection."""

import os
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_ENV_BASE = _REPOSITORY_ROOT / "lib" / "env_base.sh"
_BASH = shutil.which("bash") or "/bin/bash"


def _run_bash(body):
    return subprocess.run(
        [_BASH, "-c", textwrap.dedent(body)],
        check=False,
        cwd=_REPOSITORY_ROOT,
        text=True,
        capture_output=True,
    )


def _env_base_script(tmp_path, selection, *, host_list=""):
    return f"""
        SCALE_TEST_BASE={str(_REPOSITORY_ROOT)!r}
        RESULTS_DIR={str(tmp_path / 'results')!r}
        LOGS_DIR={str(tmp_path / 'logs')!r}
        OBJ_BUCKET=
        OBJ_AUTH_FILE={str(tmp_path / 'missing-auth')!r}
        EXECUTION_SUBSTRATE={selection!r}
        SSH_HOST_LIST={host_list!r}
        account=test-account
        reservation=
        partition=
        run_time=00:05:00
        MODULES=()
        SLURM_EXTRA_ARGS=()
        client_type=cpu
        client_arch=x86_64
        declare -A TEST_DIRS=()
        source {_ENV_BASE!s}
    """


@pytest.mark.parametrize("selection", ("", "invalid"))
def test_missing_or_invalid_selection_fails_before_directory_creation(
    tmp_path, selection
):
    """Invalid selection has no local setup side effects."""
    result = _run_bash(_env_base_script(tmp_path, selection))
    assert result.returncode != 0
    assert "EXECUTION_SUBSTRATE" in result.stderr
    assert not (tmp_path / "results").exists()
    assert not (tmp_path / "logs").exists()


def test_ssh_requires_a_host_list_before_directory_creation(tmp_path):
    """SSH selection cannot fall through to a different substrate."""
    result = _run_bash(_env_base_script(tmp_path, "ssh"))
    assert result.returncode != 0
    assert "SSH_HOST_LIST is required" in result.stderr
    assert not (tmp_path / "results").exists()


def test_explicit_slurm_ignores_a_populated_ssh_host_list(tmp_path):
    """The host-list setting no longer has substrate-selection precedence."""
    hosts = tmp_path / "hosts"
    hosts.write_text("host-a\n", encoding="utf-8")
    result = _run_bash(_env_base_script(tmp_path, "slurm", host_list=str(hosts)) + """
        [[ "$SLURM_ENABLED" == 1 ]]
        [[ -z "$SSH_ENABLED" && -z "$KUBECTL_ENABLED" ]]
        """)
    assert result.returncode == 0, result.stderr


def test_each_selection_derives_exactly_one_flag(tmp_path):
    """SSH, Slurm, and kubectl selections are mutually exclusive."""
    hosts = tmp_path / "hosts"
    hosts.write_text("host-a\n", encoding="utf-8")
    expected = {
        "ssh": "0:1:0",
        "slurm": "1:0:0",
        "kubectl": "0:0:1",
    }
    for selection, flags in expected.items():
        case_root = tmp_path / selection
        result = _run_bash(
            _env_base_script(case_root, selection, host_list=str(hosts)) + """
            printf '%d:%d:%d\n' \
                "$([[ -n "$SLURM_ENABLED" ]] && echo 1 || echo 0)" \
                "$([[ -n "$SSH_ENABLED" ]] && echo 1 || echo 0)" \
                "$([[ -n "$KUBECTL_ENABLED" ]] && echo 1 || echo 0)"
            """
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().endswith(flags)


@pytest.mark.parametrize(
    ("relative_script", "program"),
    (
        ("storage-tests/fs/nv-mdtest-elbencho.sh", "nv-mdtest-elbencho.sh"),
        ("storage-tests/network/nv-netbench.sh", "nv-netbench.sh"),
        ("storage-tests/object/nv-warp-sweep.sh", "nv-warp-sweep.sh"),
    ),
)
def test_other_benchmarks_reject_kubectl_before_creating_results(
    tmp_path, relative_script, program
):
    """Only the filesystem IO sweep may grow a kubectl implementation."""
    fake_root = tmp_path / "deployment"
    destination = fake_root / relative_script
    destination.parent.mkdir(parents=True)
    shutil.copy2(_REPOSITORY_ROOT / relative_script, destination)
    (fake_root / "env.sh").write_text(
        textwrap.dedent(f"""
            SCALE_TEST_BASE={str(_REPOSITORY_ROOT)!r}
            RESULTS_DIR={str(tmp_path / 'results')!r}
            EXECUTION_SUBSTRATE=kubectl
            KUBECTL_ENABLED=1
            SSH_ENABLED=
            SLURM_ENABLED=
        """),
        encoding="utf-8",
    )
    result = subprocess.run(
        [destination],
        check=False,
        cwd=fake_root,
        env={**os.environ, "SHELL": _BASH},
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert f"kubectl execution is not supported by {program}" in result.stderr
    assert not (tmp_path / "results").exists()


def test_filesystem_sweep_selects_kubectl_before_dispatch(tmp_path):
    """Kubernetes selection reaches common sweep validation, not a stub."""
    fake_root = tmp_path / "deployment"
    destination = fake_root / "storage-tests" / "fs" / "nv-elbencho-sweep.sh"
    destination.parent.mkdir(parents=True)
    shutil.copy2(
        _REPOSITORY_ROOT / "storage-tests" / "fs" / "nv-elbencho-sweep.sh",
        destination,
    )
    kubectl_dir = destination.parent / "kubectl"
    kubectl_dir.mkdir()
    shutil.copy2(
        _REPOSITORY_ROOT / "storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh",
        kubectl_dir / "_nv-elbencho-kubectl-functions.sh",
    )
    (fake_root / "env.sh").write_text(
        textwrap.dedent(f"""
            SCALE_TEST_BASE={str(_REPOSITORY_ROOT)!r}
            RESULTS_DIR={str(tmp_path / 'results')!r}
            EXECUTION_SUBSTRATE=kubectl
            KUBECTL_ENABLED=1
            SSH_ENABLED=
            SLURM_ENABLED=
        """),
        encoding="utf-8",
    )
    result = subprocess.run(
        [destination, "--nodes", "1"],
        check=False,
        cwd=fake_root,
        env={**os.environ, "SHELL": _BASH},
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "Filesystem testing is not enabled" in result.stderr
    assert not (tmp_path / "results").exists()


def _run_resume_selection_case(tmp_path, current, saved_line):
    """Run resume far enough to validate current and saved substrate identity."""
    fake_root = tmp_path / "deployment"
    script_dir = fake_root / "storage-tests" / "fs"
    lib_dir = fake_root / "lib"
    result_dir = tmp_path / "results" / "elbencho-20260922Z000000"
    script_dir.mkdir(parents=True)
    lib_dir.mkdir()
    (result_dir / "executions").mkdir(parents=True)
    destination = script_dir / "nv-elbencho-sweep.sh"
    shutil.copy2(
        _REPOSITORY_ROOT / "storage-tests" / "fs" / "nv-elbencho-sweep.sh",
        destination,
    )
    (lib_dir / "_elbencho_functions.sh").write_text("", encoding="utf-8")
    (fake_root / "env.sh").write_text(
        textwrap.dedent(f"""
            SCALE_TEST_BASE={str(fake_root)!r}
            EXECUTION_SUBSTRATE={current}
            SLURM_ENABLED={'1' if current == 'slurm' else ''}
            SSH_ENABLED={'1' if current == 'ssh' else ''}
            KUBECTL_ENABLED={'1' if current == 'kubectl' else ''}
        """),
        encoding="utf-8",
    )
    (result_dir / "env_used.sh").write_text(
        textwrap.dedent(f"""
            {saved_line}
            export dio_or_bio=dio
            export rand_option=0
            export single_option=0
            export sweep_write_only=0
            export sweep_write_no_read=0
            export sweep_read_from=
            export nodes_spec=1
        """),
        encoding="utf-8",
    )
    return subprocess.run(
        [destination, "--resume", result_dir],
        check=False,
        cwd=fake_root,
        env={**os.environ, "SHELL": _BASH},
        text=True,
        capture_output=True,
    )


def test_resume_rejects_a_saved_substrate_mismatch(tmp_path):
    """A current environment cannot redirect a saved attempt to Slurm."""
    result = _run_resume_selection_case(
        tmp_path, "slurm", "export EXECUTION_SUBSTRATE=ssh"
    )
    assert result.returncode != 0
    assert "does not match current env.sh" in result.stderr


def test_legacy_resume_cannot_be_reinterpreted_as_kubectl(tmp_path):
    """Snapshots predating explicit selection remain SSH/Slurm-only."""
    result = _run_resume_selection_case(tmp_path, "kubectl", "")
    assert result.returncode != 0
    assert (
        "legacy resume snapshots require inherited EXECUTION_SUBSTRATE=ssh or slurm"
        in result.stderr
    )
