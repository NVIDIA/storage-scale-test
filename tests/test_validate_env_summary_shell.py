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

"""The default (summary) output of validate_env.sh on SSH and Slurm.

The real validate_env.sh runs against stand-ins for the remote-execution
layer (SSH fan-out, Slurm commands) and for ``stat``, ``file`` and ``uname``,
so each test sees the output a passing or failing configuration produces.
"""

import os
import platform
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_BASH = shutil.which("bash") or "/bin/bash"
_SUCCESS = "All validation checks passed successfully"

# validate_env.sh's own check_ssh_with calls these four env_functions.sh
# helpers; here they run the command on this host and record its result.
_SSH_FAKES = textwrap.dedent("""\
    SSH_ENABLED=1
    SSH_ALL_HOSTS=(host1 host2 host3)
    choose_N_ssh_hosts() { :; }
    spawn_N_ssh() {
        local dir="$1" scriptlet="$3"
        shift 3
        if [[ -z "$scriptlet" ]]; then
            bash -c "$1" > "$dir/stdout" 2>&1
        elif [[ "$scriptlet" == @* ]]; then
            bash "${scriptlet#@}" "$@" > "$dir/stdout" 2>&1
        else
            bash -c "$scriptlet" _ "$@" > "$dir/stdout" 2>&1
        fi
        echo $? > "$dir/rc"
        echo 4242
    }
    gather_N_ssh() { :; }
    results_N_ssh_pid() {
        case "$3" in
            hostname) echo host1 ;;
            *) cat "$1/$3" ;;
        esac
    }
    """)

# Slurm commands that run the submitted script or command on this host.
_SLURM_FAKES = textwrap.dedent("""\
    SLURM_ENABLED=1
    partition=gpuq
    client_type=cpu
    SBATCH_OPTIONS=-N1
    SRUN_OPTIONS=-N1
    _slurm_job_poll_interval() { :; }
    build_sbatch_cmd() { local -n _cmd=$1; _cmd=(sbatch); }
    build_srun_validate_scale_cmd() { local -n _cmd=$1; _cmd=(srun); }
    make_sbatch_job_name() { echo "$1"; }
    """)

_COMMON_ENV = textwrap.dedent("""\
    KUBECTL_ENABLED=
    SLURM_ENABLED=
    SSH_ENABLED=
    FS_ENABLED=
    OBJ_ENABLED=
    client_arch=x86_64
    validate_integer_array() { :; }
    validate_elbencho_io_sizes() { :; }
    validate_elbencho_file_workload_env() { :; }
    validate_elbencho_duration() { :; }
    validate_elbencho_live_csv() { :; }
    validate_elbencho_single_big_file_env() { :; }
    validate_storage_target_name() { :; }
    """)


def _write_executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def _install_commands(bindir: Path) -> None:
    """Stand-ins that make this host look like a healthy x86_64 cluster."""
    real_uname = shutil.which("uname")
    _write_executable(
        bindir / "uname",
        f'#!/bin/sh\n[ "$1" = -m ] && echo x86_64 || exec {real_uname} "$@"\n',
    )
    _write_executable(bindir / "file", "#!/bin/sh\necho 'ELF x86-64 aarch64'\n")
    _write_executable(
        bindir / "stat",
        textwrap.dedent("""\
            #!/bin/sh
            shift 2
            [ "${1-}" != -- ] || shift
            if [ "${SAME_DEVICE-}" = 1 ] || [ "$1" = / ]; then echo 11; else echo 22; fi
            """),
    )
    _write_executable(bindir / "grealpath", "#!/bin/sh\n")
    _write_executable(bindir / "gstat", "#!/bin/sh\n")
    _write_executable(
        bindir / "sinfo",
        textwrap.dedent("""\
            #!/bin/sh
            case "$*" in *%G*) echo 'node1 (null)' ;; esac
            """),
    )
    _write_executable(
        bindir / "scontrol", "#!/bin/sh\necho 'JobState=COMPLETED ExitCode=0:0'\n"
    )
    _write_executable(
        bindir / "sbatch",
        textwrap.dedent("""\
            #!/bin/bash
            while [[ "${1:-}" == -* ]]; do shift; done
            script="$1"
            shift
            bash "$script" "$@" > slurm-7.out 2>&1
            echo "Submitted batch job 7"
            """),
    )
    _write_executable(bindir / "srun", '#!/bin/sh\nshift\nexec "$@"\n')


def _deployment(tmp_path: Path, env_text: str) -> Path:
    """A copy of the validator whose env.sh is ENV_TEXT, with its directories."""
    deployment = tmp_path / "deployment"
    (deployment / "lib").mkdir(parents=True)
    shutil.copy2(_ROOT / "validate_env.sh", deployment / "validate_env.sh")
    shutil.copy2(_ROOT / "lib/project_version.sh", deployment / "lib")
    (tmp_path / "results").mkdir()
    (tmp_path / "logs").mkdir()
    (deployment / "env.sh").write_text(
        f"RESULTS_DIR={str(tmp_path / 'results')!r}\n"
        f"LOGS_DIR={str(tmp_path / 'logs')!r}\n{_COMMON_ENV}{env_text}",
        encoding="utf-8",
    )
    return deployment


def _run(tmp_path: Path, deployment: Path, *arguments: str, **environment: str):
    bindir = tmp_path / "bin"
    _install_commands(bindir)
    return subprocess.run(
        [_BASH, deployment / "validate_env.sh", *arguments],
        check=False,
        cwd=deployment,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", **environment},
        text=True,
        capture_output=True,
        timeout=120,
    )


def _roots(tmp_path: Path) -> list[str]:
    """Two existing writable directories, in an order a sort must fix."""
    roots = [tmp_path / "mnt" / "b", tmp_path / "mnt" / "a"]
    for root in roots:
        root.mkdir(parents=True)
    return [str(root) for root in roots]


def _ssh_env(tmp_path: Path) -> str:
    binary = _write_executable(tmp_path / "elbencho", "#!/bin/sh\n")
    roots = " ".join(f"[{root!r}]=1" for root in _roots(tmp_path))
    return (
        f"{_SSH_FAKES}FS_ENABLED=1\nELBENCHO={str(binary)!r}\n"
        f"declare -A TEST_DIRS=({roots})\n"
    )


def test_ssh_success_prints_only_a_short_summary(tmp_path):
    """Three summary lines state what passed; no per-check detail is printed."""
    result = _run(tmp_path, _deployment(tmp_path, _ssh_env(tmp_path)))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    assert result.stdout.splitlines() == [
        "SSH: remote commands work (checked one of 3 host(s)): OK",
        "Architecture x86_64, binaries (elbencho): OK",
        f"Filesystem roots ({tmp_path}/mnt/a, {tmp_path}/mnt/b): OK",
        _SUCCESS,
    ]


def test_ssh_verbose_adds_only_the_skipped_check_note(tmp_path):
    """SSH has no per-check detail lines; -v and --verbose add the skip NOTE."""
    deployment = _deployment(tmp_path, _ssh_env(tmp_path))
    quiet = _run(tmp_path, deployment)
    note = "  (NOTE: OBJ not enabled; not checking object testability)\n\n"
    for option in ("-v", "--verbose"):
        verbose = _run(tmp_path, deployment, option)
        assert verbose.returncode == 0, verbose.stdout + verbose.stderr
        assert verbose.stdout == note + quiet.stdout


def test_ssh_failure_lists_errors_and_omits_the_failed_summary(tmp_path):
    """A root on the same device as / is an error and has no OK line."""
    deployment = _deployment(tmp_path, _ssh_env(tmp_path))
    result = _run(tmp_path, deployment, SAME_DEVICE="1")
    assert result.returncode == 1
    assert "Validation failed with the following errors:" in result.stdout
    assert "is not on a filesystem distinct from /" in result.stdout
    assert _SUCCESS not in result.stdout
    assert "SSH: remote commands work (checked one of 3 host(s)): OK" in result.stdout
    assert "Filesystem roots" not in result.stdout


@pytest.mark.skipif(
    platform.system() == "Darwin", reason="macOS may launch only SSH or kubectl"
)
def test_slurm_success_prints_only_a_short_summary(tmp_path):
    """Slurm, architecture, every binary, the roots and the bucket: one line each."""
    binaries = {
        name: _write_executable(tmp_path / name, "#!/bin/sh\n")
        for name in ("elbencho", "warp")
    }
    s3test = _write_executable(tmp_path / "s3test", "#!/bin/sh\necho 0\n")
    auth = tmp_path / "obj_auth"
    auth.write_text("", encoding="utf-8")
    roots = " ".join(f"[{root!r}]=1" for root in _roots(tmp_path))
    deployment = _deployment(
        tmp_path,
        f"{_SLURM_FAKES}FS_ENABLED=1\nOBJ_ENABLED=1\n"
        f"ELBENCHO={str(binaries['elbencho'])!r}\nWARP={str(binaries['warp'])!r}\n"
        f"S3TEST={str(s3test)!r}\nOBJ_AUTH_FILE={str(auth)!r}\nOBJ_BUCKET=bkt\n"
        f"declare -A TEST_DIRS=({roots})\n",
    )
    _write_executable(deployment / "utils" / "s3test", "#!/bin/sh\n")
    result = _run(tmp_path, deployment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "Slurm: partition gpuq, client type cpu, sbatch and srun: OK",
        "Architecture x86_64, binaries (elbencho, warp, s3test): OK",
        f"Filesystem roots ({tmp_path}/mnt/a, {tmp_path}/mnt/b): OK",
        "Object storage: bucket bkt reachable and empty: OK",
        _SUCCESS,
    ]


def test_a_skipped_check_is_not_a_summary_line(tmp_path):
    """Neither OBJ nor FS testing is claimed OK; only --verbose notes the skip."""
    deployment = _deployment(tmp_path, _SSH_FAKES + "FS_ENABLED=\nELBENCHO=\n")
    quiet = _run(tmp_path, deployment)
    verbose = _run(tmp_path, deployment, "--verbose")
    for result in (quiet, verbose):
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Filesystem roots" not in result.stdout
        assert "Object storage" not in result.stdout
        assert result.stdout.splitlines()[-1] == _SUCCESS
    assert "NOTE" not in quiet.stdout
    assert (
        "(NOTE: FS not enabled; not checking filesystem testability)" in verbose.stdout
    )
    assert "(NOTE: OBJ not enabled; not checking object testability)" in verbose.stdout


def test_the_help_documents_verbose():
    """--help names both spellings of the verbose option."""
    result = subprocess.run(
        [_BASH, _ROOT / "validate_env.sh", "--help"],
        check=False,
        cwd=_ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "[-v|--verbose]" in result.stdout
    assert "-v, --verbose" in result.stdout
