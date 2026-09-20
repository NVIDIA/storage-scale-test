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

"""Shell-level regressions for validate_env filesystem identity checks."""

import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VALIDATE_ENV = _REPO_ROOT / "validate_env.sh"
_BASH = shutil.which("bash") or "bash"


def _filesystem_functions() -> str:
    """Extract the production filesystem-check functions without running main."""
    source = _VALIDATE_ENV.read_text(encoding="utf-8")
    start = source.index("check_fs() {")
    end = source.index("check_obj() {", start)
    return source[start:end]


def _write_fake_stat(directory: Path) -> None:
    """Install a stat whose statfs value would collide but st_dev does not."""
    command = directory / "stat"
    command.write_text(
        textwrap.dedent("""\
            #!/bin/sh
            if [ "$1" != -c ] || [ "$2" != %d ]; then
                printf 'unexpected stat arguments: %s\\n' "$*" >&2
                exit 90
            fi
            shift 2
            [ "${1-}" != -- ] || shift
            if [ "${SAME_DEVICE-}" = 1 ] || [ "$1" = / ]; then
                printf '11\\n'
            else
                printf '22\\n'
            fi
            """),
        encoding="utf-8",
    )
    command.chmod(0o755)


def _run_check(
    tmp_path: Path, substrate: str, *, same_device: bool
) -> subprocess.CompletedProcess[str]:
    """Run both validation paths for one substrate against the fake stat."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_stat(fake_bin)
    target = tmp_path / "test directory"
    target.mkdir()
    trace = tmp_path / "trace"
    script = _filesystem_functions() + textwrap.dedent(f"""
        declare -a captured_errors=()
        FAKE_BIN={shlex.quote(str(fake_bin))}
        TRACE={shlex.quote(str(trace))}
        TARGET={shlex.quote(str(target))}
        export FAKE_BIN TRACE
        {"export SAME_DEVICE=1" if same_device else "unset SAME_DEVICE"}

        register_error() {{
            captured_errors+=("$1")
        }}
        run_scriptlet() {{
            local scriptlet="$1"
            shift
            PATH="$FAKE_BIN:$PATH" bash -s -- "$@" <<< "$scriptlet"
        }}
        check_sbatch_with() {{
            local scriptlet="$1"
            shift
            printf 'sbatch\\n' >> "$TRACE"
            run_scriptlet "$scriptlet" "$@"
        }}
        check_srun_with() {{
            local scriptlet="$1"
            shift
            printf 'srun\\n' >> "$TRACE"
            PATH="$FAKE_BIN:$PATH" bash -c "$scriptlet" "$@"
        }}
        check_ssh_with() {{
            local scriptlet="$1"
            shift
            printf 'ssh\\n' >> "$TRACE"
            if [[ -n "$scriptlet" ]]; then
                run_scriptlet "$scriptlet" "$@"
            else
                PATH="$FAKE_BIN:$PATH" bash -c "$1"
            fi
        }}

        if [[ {shlex.quote(substrate)} == slurm ]]; then
            check_fs 0 "$TARGET"
            [[ $(grep -c '^sbatch$' "$TRACE") -eq 1 ]]
            [[ $(grep -c '^srun$' "$TRACE") -eq 1 ]]
        else
            SSH_ENABLED=1
            check_fs 1 "$TARGET"
            [[ $(grep -c '^ssh$' "$TRACE") -eq 2 ]]
        fi
        printf '%s\\n' "${{captured_errors[@]}}"
        """)
    return subprocess.run(
        [_BASH, "-c", script],
        check=False,
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
    )


@pytest.mark.parametrize("substrate", ("ssh", "slurm"))
def test_distinct_device_passes_every_remote_validation_path(tmp_path, substrate):
    """SSH, sbatch, and srun compare st_dev rather than statfs free inodes."""
    result = _run_check(tmp_path, substrate, same_device=False)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "\n"


@pytest.mark.parametrize("substrate", ("ssh", "slurm"))
def test_root_device_is_rejected_by_every_substrate(tmp_path, substrate):
    """A test directory sharing root's device remains a validation error."""
    result = _run_check(tmp_path, substrate, same_device=True)

    assert result.returncode == 0, result.stderr
    assert "not on a filesystem distinct from /" in result.stdout
