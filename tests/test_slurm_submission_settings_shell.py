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

"""Invalid Slurm monitoring settings are rejected before any job is submitted."""

import os
from pathlib import Path
import shutil
import subprocess
import textwrap

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FUNCTIONS = _REPO_ROOT / "lib" / "env_functions.sh"
_INVALID_INTERVALS = ("0", "-1", "1.5", "abc")


def _scheduler_stubs(tmp_path: Path) -> tuple[Path, Path]:
    """Return a PATH directory of Slurm stubs and the sbatch call log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "sbatch.calls"
    (bin_dir / "sbatch").write_text(
        f'#!/usr/bin/env bash\necho "$*" >> {str(calls)!r}\necho 4242\n',
        encoding="utf-8",
    )
    for command in ("sinfo", "squeue", "sacct", "scontrol", "scancel", "srun"):
        (bin_dir / command).write_text("#!/usr/bin/env bash\nexit 0\n", "utf-8")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)
    return bin_dir, calls


def _sbatch_calls(calls: Path) -> int:
    return len(calls.read_text(encoding="utf-8").splitlines()) if calls.exists() else 0


@pytest.mark.parametrize("interval", [*_INVALID_INTERVALS, "1"])
def test_run_sbatch_job_validates_the_poll_interval_first(tmp_path, interval):
    """Every monitored submission goes through run_sbatch_job."""
    bin_dir, calls = _scheduler_stubs(tmp_path)
    script = textwrap.dedent(f"""
        source "{_ENV_FUNCTIONS}"
        sbatch_cmd=(sbatch)
        g_sbatch_opts=()
        log_files=()
        JOBID=
        SLURM_JOB_POLL_INTERVAL_SECONDS={interval}
        run_sbatch_job 1 job "{tmp_path}/%j.out" Test batch.sh
    """)
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        check=False,
        capture_output=True,
        text=True,
    )
    if interval == "1":
        assert result.returncode == 0, result.stderr
        assert _sbatch_calls(calls) == 1
    else:
        assert result.returncode != 0
        assert "must be a positive integer" in result.stderr
        assert _sbatch_calls(calls) == 0


@pytest.mark.parametrize(
    "launcher, arguments",
    [
        ("storage-tests/network/nv-netbench.sh", ("--nodes", "2")),
        ("storage-tests/object/nv-warp-sweep.sh", ("--nodes", "1")),
    ],
)
@pytest.mark.parametrize("interval", ["0", "abc"])
def test_launchers_submit_nothing_with_an_invalid_poll_interval(
    tmp_path, launcher, arguments, interval
):
    """Unchanged launchers once submitted jobs, then rejected the setting."""
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    for directory in ("lib", "storage-tests"):
        shutil.copytree(_REPO_ROOT / directory, deployment / directory)
    template = (_REPO_ROOT / "env.sh.template").read_text(encoding="utf-8")
    overrides = textwrap.dedent(f"""
        export EXECUTION_SUBSTRATE=slurm
        export RESULTS_DIR={tmp_path / "results"}
        export LOGS_DIR={tmp_path / "logs"}
        export NETBENCH_HOST_NIC_GBPS=100
        export SLURM_JOB_POLL_INTERVAL_SECONDS={interval}
    """)
    (deployment / "env.sh").write_text(
        template.replace("# STORAGE_SCALE_TEST_INTEGRATION_OVERRIDES", overrides),
        encoding="utf-8",
    )
    (deployment / ".obj_auth").touch()
    bin_dir, calls = _scheduler_stubs(tmp_path)

    result = subprocess.run(
        ["bash", str(deployment / launcher), *arguments],
        cwd=deployment,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert "must be a positive integer" in result.stdout + result.stderr
    assert _sbatch_calls(calls) == 0
