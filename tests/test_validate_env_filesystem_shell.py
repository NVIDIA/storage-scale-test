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

"""validate_env's remote filesystem checks: the shared probe and its results.

The probe scriptlet runs here (under ``bash -s`` as SSH and sbatch run it, and
``sh -c`` as srun does) against a fake ``stat``; the parser and the messages
run on canned output; check_fs runs against stubbed transports.
"""

import os
import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VALIDATE_ENV = _REPO_ROOT / "validate_env.sh"
_BASH = shutil.which("bash") or "bash"
_SH = shutil.which("sh") or "/bin/sh"


def _between(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin)]


def _probe_functions() -> str:
    """The probe, parser, message and check_fs functions, without main."""
    source = _VALIDATE_ENV.read_text(encoding="utf-8")
    return (
        _between(source, "validate_probe_roots() {", "check_sbatch_with() {")
        + _between(source, "check_fs() {", "check_obj() {")
        + _between(source, "_validate_join_list() {", "\n}\n")
        + "\n}\n"
    )


def _write_fake_stat(directory: Path) -> None:
    """A stat whose device number is 11 for / (and SAME_DEVICE roots), else 22."""
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
            case "$1" in
                *hang*) exit 124 ;;
            esac
            if [ "${SAME_DEVICE-}" = 1 ] || [ "$1" = / ]; then
                printf '11\\n'
            else
                printf '22\\n'
            fi
            """),
        encoding="utf-8",
    )
    command.chmod(0o755)


def _bash(tmp_path: Path, body: str, **environment: str):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    _write_fake_stat(fake_bin)
    script = _probe_functions() + textwrap.dedent(f"""
        set -u
        FAKE_BIN={shlex.quote(str(fake_bin))}
        declare -a captured_errors=() captured_warnings=()
        register_error() {{ captured_errors+=("$1"); }}
        register_warning() {{ captured_warnings+=("$1"); }}
        """) + textwrap.dedent(body)
    return subprocess.run(
        [_BASH, "-c", script],
        check=False,
        cwd=tmp_path,
        env={**os.environ, **environment},
        text=True,
        capture_output=True,
        timeout=60,
    )


def _probe_output(tmp_path: Path, shell: str, roots, **environment) -> str:
    """Run the probe scriptlet as SSH/sbatch (bash -s) or srun (sh -c) would."""
    script = "PATH=$FAKE_BIN:$PATH " + (
        'bash -s -- 1 "$@" <<< "$(validate_probe_scriptlet)"'
        if shell == "bash"
        else f'{shlex.quote(_SH)} -c "$(validate_probe_scriptlet)" _ 1 "$@"'
    )
    arguments = " ".join(shlex.quote(str(root)) for root in roots)
    result = _bash(tmp_path, f"set -- {arguments}\n{script}\n", **environment)
    assert result.returncode == 0, result.stderr
    return result.stdout


def _fields(output: str) -> list[list[str]]:
    return [line.split("\t")[1:] for line in output.splitlines()]


@pytest.mark.parametrize("shell", ("bash", "sh"))
def test_the_probe_reports_each_root_by_index(tmp_path, shell):
    """Distinct, same-device, missing and odd-named roots each get one result."""
    odd = tmp_path / 'it\'s a "dir"\nwith a newline'
    good = tmp_path / "good dir"
    for directory in (odd, good):
        directory.mkdir()
    fields = _fields(
        _probe_output(tmp_path, shell, (good, tmp_path / "missing", odd, "/"))
    )
    assert fields[0][0] == "begin" and fields[0][1]
    assert fields[1] == ["echo", "ABCDEF"]
    assert fields[2] == ["arch", os.uname().machine]
    assert fields[3:] == [
        ["fs", "1", "ok"],
        ["fs", "2", "notdir"],
        ["fs", "3", "ok"],
        ["fs", "4", "samedev"],
        ["end", "0"],
    ]


def test_the_probe_reports_an_unwritable_root(tmp_path):
    """A read-only root is reported as not writable."""
    if os.geteuid() == 0:
        pytest.skip("root can write a read-only directory")
    root = tmp_path / "read-only"
    root.mkdir()
    root.chmod(0o555)
    try:
        assert _fields(_probe_output(tmp_path, "sh", (root,)))[3] == [
            "fs",
            "1",
            "nowrite",
        ]
    finally:
        root.chmod(0o755)


def test_the_probe_reports_a_hung_root_as_a_timeout(tmp_path):
    """A stat that times out (status 124) marks only that root."""
    hung = tmp_path / "hang"
    hung.mkdir()
    fields = _fields(_probe_output(tmp_path, "bash", (hung, tmp_path)))
    assert fields[3:5] == [["fs", "1", "timeout"], ["fs", "2", "ok"]]


def test_the_probe_with_no_roots_reports_only_the_node(tmp_path):
    """Zero roots is valid: begin, echo, arch, end."""
    fields = _fields(_probe_output(tmp_path, "sh", ()))
    assert [field[0] for field in fields] == ["begin", "echo", "arch", "end"]


def test_the_parser_ignores_noise_and_keeps_the_first_value(tmp_path):
    """Prolog noise is skipped, duplicates do not overwrite, results go by index."""
    result = _bash(
        tmp_path,
        r"""
        output=$'module loaded\nVE1\tbegin\tnode7\nVE1\techo\tABCDEF\n'
        output+=$'VE1\tarch\taarch64\nVE1\tarch\tx86_64\nVE1\tfs\t2\tsamedev\n'
        output+=$'VE1\tfs\t2\tok\nVE1\tfs\t0\tok\nnoise\tfs\t1\tok\nVE1\tend\t0\n'
        parse_validate_probe sbatch "$output"
        printf '%s|%s|%s|%s|%s|%s\n' "$PROBE_sbatch_HOST" "$PROBE_sbatch_ECHO" \
            "$PROBE_sbatch_ARCH" "$PROBE_sbatch_END" "${PROBE_sbatch_FS[2]}" \
            "${!PROBE_sbatch_FS[*]}"
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "node7|ABCDEF|aarch64|1|samedev|2\n"


def test_missing_end_marker_names_the_roots_without_results(tmp_path):
    """Output cut short is an error naming each root never reported."""
    result = _bash(
        tmp_path,
        r"""
        VALIDATE_PROBE_ROOTS=(/a /b /c)
        parse_validate_probe srun $'VE1\tbegin\tn1\nVE1\tfs\t2\tok\n'
        _validate_probe_complete srun && exit 9
        printf '%s\n' "${captured_errors[@]}"
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "srun: probe output incomplete (no end marker); "
        "results missing for roots: /a, /c\n"
    )


@pytest.mark.parametrize(
    ("status", "message"),
    (
        ("ok", None),
        ("samedev", "is not on a filesystem distinct from / on compute node n1"),
        (
            "notdir",
            "is not on a filesystem distinct from / on compute node n1 "
            "(it is not a directory)",
        ),
        (
            "statfail",
            "is not on a filesystem distinct from / on compute node n1 (stat failed)",
        ),
        ("nowrite", "is not writable on compute node n1"),
        ("timeout", "did not respond within 30 s on compute node n1"),
        ("weird", "unexpected probe result 'weird' on compute node n1"),
    ),
)
def test_each_probe_result_maps_to_its_message(tmp_path, status, message):
    """The label prefixes each message; ok registers nothing."""
    result = _bash(
        tmp_path,
        rf"""
        VALIDATE_PROBE_ROOTS=(/x /data)
        parse_validate_probe ssh $'VE1\tbegin\tn1\nVE1\tfs\t2\t{status}\nVE1\tend\t0\n'
        check_fs_probe_result ssh /data
        printf '%s\n' "${{captured_errors[@]}}"
        """,
    )
    assert result.returncode == 0, result.stderr
    expected = "\n" if message is None else f"ssh: /data {message}\n"
    if status == "weird":
        expected = f"ssh: /data: {message}\n"
    assert result.stdout == expected


_TRANSPORTS = r"""
VALIDATE_PROBE_ROOTS=("$GOOD" "$SAME")
fake_probe() { printf 'probe %s\n' "$1" >> trace; shift; PATH="$FAKE_BIN:$PATH" bash -s -- "$@" <<< "$(validate_probe_scriptlet)"; }
check_ssh_with() { fake_probe ssh "${@:2}"; }
"""


def _roots(tmp_path: Path) -> dict[str, str]:
    good = tmp_path / "good"
    good.mkdir()
    return {"GOOD": str(good), "SAME": "/"}


def test_ssh_checks_every_root_and_the_architecture_in_one_session(tmp_path):
    """One SSH session serves the architecture and every root."""
    result = _bash(
        tmp_path,
        _TRANSPORTS + r"""
        SSH_ENABLED=1
        check_fs 1 "$GOOD"
        check_fs 1 "$SAME"
        run_ssh_validation_probe
        printf '%s\n' "$PROBE_ssh_ARCH" "${captured_errors[@]}"
        """,
        **_roots(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == os.uname().machine
    assert lines[1].startswith("ssh: / is not on a filesystem distinct from /")
    assert len(lines) == 2
    assert (tmp_path / "trace").read_text() == "probe ssh\n"


def test_a_failed_ssh_probe_reports_each_root_unchecked(tmp_path):
    """A session that fails before reporting leaves every root unchecked."""
    result = _bash(
        tmp_path,
        r"""
        VALIDATE_PROBE_ROOTS=(/a)
        SSH_ENABLED=1
        check_ssh_with() { echo 'ssh: connect refused'; return 255; }
        check_fs 1 /a
        printf '%s\n' "${captured_errors[@]}"
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ssh: /a was not checked: the validation probe failed\n"


def test_slurm_roots_come_from_both_probes_without_a_launch(tmp_path):
    """check_fs translates the sbatch and srun results; it launches nothing."""
    result = _bash(
        tmp_path,
        _TRANSPORTS + r"""
        check_sbatch_with() { echo launched >> trace; }
        check_srun_with() { echo launched >> trace; }
        parse_validate_probe sbatch "$(fake_probe sbatch 1 "$GOOD" "$SAME")"
        parse_validate_probe srun "$(fake_probe srun 1 "$GOOD" "$SAME")"
        : > trace
        check_fs 0 "$GOOD"
        check_fs 0 "$SAME"
        printf '%s\n' "${captured_errors[@]}"
        """,
        **_roots(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert [line.split(":")[0] for line in lines] == ["sbatch", "srun"]
    assert all("/ is not on a filesystem distinct from /" in line for line in lines)
    assert (tmp_path / "trace").read_text() == ""


def test_a_skipped_check_still_warns_without_slurm_or_ssh(tmp_path):
    """No usable substrate: the root is skipped with a warning, as before."""
    result = _bash(
        tmp_path,
        r"""
        SSH_ENABLED=
        check_fs 1 /a
        printf '%s\n' "${captured_warnings[@]}"
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Filesystem check skipped for:\n/a\n")


_FILE_ARCH = {"x86_64": "x86-64", "aarch64": "aarch64"}


def _arch_problem(binary: Path, arch: str) -> str:
    source = _VALIDATE_ENV.read_text(encoding="utf-8")
    function = _between(source, "binary_arch_problem() {", "check_arch_and_binaries")
    result = subprocess.run(
        [_BASH, "-c", f'{function}\nbinary_arch_problem "$1" "$2"', "_", binary, arch],
        check=False,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.skipif(os.uname().machine not in _FILE_ARCH, reason="unsupported host")
def test_binary_architecture_ignores_the_path_and_spares_scripts(tmp_path):
    """A path naming the architecture cannot pass; a wrapper script is unchecked."""
    host = _FILE_ARCH[os.uname().machine]
    other = next(arch for arch in _FILE_ARCH.values() if arch != host)
    native = tmp_path / f"elbencho.{other}"
    shutil.copy2(shutil.which("ls") or "/bin/ls", native)
    script = tmp_path / f"elbencho.{host}"
    script.write_text("#!/usr/bin/env bash\nexec true\n", encoding="utf-8")
    assert _arch_problem(native, host) == ""
    assert _arch_problem(native, other) == "mismatch"
    assert _arch_problem(script, host) == "script"
