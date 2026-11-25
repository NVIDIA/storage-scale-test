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

"""The elbencho version floor: parsing, comparison and the binary check."""

import hashlib
from pathlib import Path
import re
import shutil
import subprocess

import pytest

from lib.project_version import ROOT

_LIBRARY = ROOT / "lib/_platform_functions.sh"
_TEMPLATE = ROOT / "storage-tests/fs/kubectl/templates/validation-job.yaml.tmpl"
_VERSION_OUTPUT = (
    "elbencho\n * Version: {version}\n * Net protocol version: 3.4.1\n"
    " * Build date: Nov 20 2026 16:54:54\n"
)


def _bash(script: str, *arguments: str):
    return subprocess.run(
        [shutil.which("bash") or "bash", "-c", script, "bash", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _library(body: str, *arguments: str):
    return _bash(f"source {_LIBRARY}\n{body}", *arguments)


def _floor() -> str:
    result = _library('printf %s "$ELBENCHO_MIN_VERSION"')
    assert result.returncode == 0, result.stderr
    return result.stdout


def _fake_binary(path: Path, version: str) -> Path:
    path.write_text(
        "#!/bin/sh\ncat <<'EOF'\n" + _VERSION_OUTPUT.format(version=version) + "EOF\n"
    )
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    "text, expected",
    [
        ("v3.4-1", "3 4 1"),
        ("3.4-1", "3 4 1"),
        ("3.4.1", "3 4 1"),
        ("v3.4.1", "3 4 1"),
        ("3.10-1", "3 10 1"),
        ("03.04-01", "3 4 1"),
    ],
)
def test_all_version_forms_parse_to_integer_tuples(text, expected):
    result = _library('elbencho_version_tuple "$1"', text)
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "text",
    ["", "garbage", "3.4", "3-4-1", "v3.4-1-rc", "3.4-1 extra", "a.b-c", "3.4.x"],
)
def test_unparseable_versions_are_rejected(text):
    assert _library('elbencho_version_tuple "$1"', text).returncode == 1
    assert _library('elbencho_version_at_least "$1" 3.4-1', text).returncode == 2
    assert _library('elbencho_version_at_least 3.4-1 "$1"', text).returncode == 2


@pytest.mark.parametrize(
    "found, minimum, status",
    [
        ("3.4-1", "3.4-1", 0),
        ("3.4-1", "3.4.1", 0),
        ("v3.4.1", "v3.4-1", 0),
        ("3.4-2", "3.4-1", 0),
        ("3.10-1", "3.4-1", 0),
        ("4.0-1", "3.4-1", 0),
        ("3.2-1", "3.4-1", 1),
        ("3.4-1", "3.10-1", 1),
        ("3.4-1", "3.4-2", 1),
        ("2.99-9", "3.0-0", 1),
    ],
)
def test_versions_compare_numerically(found, minimum, status):
    result = _library('elbencho_version_at_least "$1" "$2"', found, minimum)
    assert result.returncode == status


def test_version_comes_from_the_version_line_of_version_output():
    result = _library(
        'elbencho_version_from_output <<< "$1"',
        _VERSION_OUTPUT.format(version="3.4-1"),
    )
    assert result.stdout == "3.4-1\n"
    assert (
        _library('elbencho_version_from_output <<< "$1"', "no version\n").returncode
        == 1
    )


def test_floor_is_at_least_the_fetch_script_pin():
    script = (ROOT / "utils/build/fetch_elbencho.sh").read_text()
    tag = re.search(r"readonly ELBENCHO_VERSION_TAG='([^']+)'", script)
    assert tag
    result = _library('elbencho_version_at_least "$1" "$ELBENCHO_MIN_VERSION"', tag[1])
    assert result.returncode == 0, f"pin {tag[1]} is below the floor {_floor()}"


def test_the_default_image_matches_the_fetch_script_pin():
    script = (ROOT / "utils/build/fetch_elbencho.sh").read_text()
    tag = re.search(r"readonly ELBENCHO_VERSION_TAG='([^']+)'", script)
    assert tag
    functions = (ROOT / "lib/env_functions.sh").read_text()
    default = f"KUBECTL_DEFAULT_ELBENCHO_IMAGE=docker.io/breuner/elbencho:{tag[1]}\n"
    assert default in functions


def test_binary_below_the_floor_fails_with_the_fix(tmp_path):
    binary = _fake_binary(tmp_path / "elbencho", "3.2-1")
    result = _library('elbencho_check_binary_version "$1"', str(binary))
    assert result.returncode == 1
    assert "elbencho 3.2-1" in result.stderr
    assert f"{_floor()} or newer is required" in result.stderr
    assert "utils/build/fetch_elbencho.sh --force-download" in result.stderr


@pytest.mark.parametrize("version", ["3.4-1", "3.4-5", "3.10-1", "v4.0.0"])
def test_binary_at_or_above_the_floor_passes(tmp_path, version):
    binary = _fake_binary(tmp_path / "elbencho", version)
    result = _library('elbencho_check_binary_version "$1"', str(binary))
    assert result.returncode == 0, result.stderr


def test_binary_with_unparseable_version_fails(tmp_path):
    binary = _fake_binary(tmp_path / "elbencho", "banana")
    result = _library('elbencho_check_binary_version "$1"', str(binary))
    assert result.returncode == 1
    assert "banana" in result.stderr or "cannot determine" in result.stderr


def _foreign_binary(path: Path, stamp: str | None):
    """A file that cannot run here, with an optional stamp beside it."""
    path.write_bytes(b"\x7fELF not for this host")
    path.chmod(0o755)
    if stamp is not None:
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        path.with_name(path.name + ".version").write_text(stamp.format(sha=sha) + "\n")
    return path


def test_binary_that_cannot_run_uses_its_matching_stamp(tmp_path):
    binary = _foreign_binary(tmp_path / "elbencho.aarch64", "v3.4-1 {sha}")
    result = _library('elbencho_check_binary_version "$1"', str(binary))
    assert result.returncode == 0, result.stderr


def test_stamp_below_the_floor_fails(tmp_path):
    binary = _foreign_binary(tmp_path / "elbencho.aarch64", "v3.2-1 {sha}")
    result = _library('elbencho_check_binary_version "$1"', str(binary))
    assert result.returncode == 1
    assert "elbencho v3.2-1" in result.stderr


@pytest.mark.parametrize("stamp", [None, "v3.4-1 " + "0" * 64, "v3.4-1", "garbage"])
def test_binary_that_cannot_run_without_a_matching_stamp_fails(tmp_path, stamp):
    binary = _foreign_binary(tmp_path / "elbencho.aarch64", stamp)
    result = _library('elbencho_check_binary_version "$1"', str(binary))
    assert result.returncode == 1
    assert "cannot determine the version" in result.stderr


def _template_checker() -> str:
    """The validation Job's check function, as written in the template."""
    text = _TEMPLATE.read_text()
    start = text.index("check_elbencho_version() {")
    end = text.index("\n              }\n", start) + len("\n              }\n")
    return "\n".join(line.strip() for line in text[start:end].splitlines())


@pytest.mark.parametrize(
    "version, expected_status",
    [
        ("3.4-1", 0),
        ("v3.4.1", 0),
        ("3.10-1", 0),
        ("3.2-1", 1),
        ("3.4-0", 1),
        ("banana", 1),
    ],
)
def test_the_validation_job_applies_the_same_rule(tmp_path, version, expected_status):
    binary = _fake_binary(tmp_path / "elbencho", version)
    floor = _floor()
    pod = _bash(
        f'{_template_checker()}\ncheck_elbencho_version "$1" "$2"', str(binary), floor
    )
    library = _library('elbencho_check_binary_version "$1"', str(binary))
    assert pod.returncode == library.returncode == expected_status, pod.stderr
    if expected_status:
        assert f"{floor} or newer is required" in pod.stderr
        assert "KUBECTL_ELBENCHO_IMAGE" in pod.stderr


def test_the_validation_job_rejects_a_binary_without_a_version(tmp_path):
    binary = tmp_path / "elbencho"
    binary.write_text("#!/bin/sh\necho nothing\n")
    binary.chmod(0o755)
    result = _bash(
        f'{_template_checker()}\ncheck_elbencho_version "$1" 3.4-1', str(binary)
    )
    assert result.returncode == 1
    assert "cannot determine" in result.stderr
