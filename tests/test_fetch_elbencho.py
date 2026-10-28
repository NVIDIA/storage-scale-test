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

"""utils/build/fetch_elbencho.sh without network access."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from lib.project_version import ROOT

BINARIES = ("elbencho", "elbencho.aarch64")


@pytest.fixture(name="tree")
def tree_fixture(tmp_path):
    """A minimal tree holding the script, with curl replaced by a stub."""
    tree = tmp_path / "tree"
    (tree / "utils/build").mkdir(parents=True)
    (tree / "lib").mkdir()
    shutil.copy2(
        ROOT / "utils/build/fetch_elbencho.sh", tree / "utils/build/fetch_elbencho.sh"
    )
    shutil.copy2(ROOT / "lib/project_version.sh", tree / "lib/project_version.sh")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    # Writes an archive that cannot match the pinned checksum and logs the call.
    (stubs / "curl").write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$CURL_LOG"\n'
        'while [ "$#" -gt 0 ]; do\n'
        '    if [ "$1" = -o ]; then printf "not elbencho" > "$2"; fi\n'
        "    shift\n"
        "done\n"
    )
    (stubs / "curl").chmod(0o755)
    return tree, stubs


def _fetch(tree, stubs, *arguments):
    environment = dict(os.environ)
    environment["PATH"] = f"{stubs}:{environment['PATH']}"
    environment["CURL_LOG"] = str(tree.parent / "curl.log")
    return subprocess.run(
        ["bash", str(tree / "utils/build/fetch_elbencho.sh"), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )


def _curl_calls(tree) -> list[str]:
    log = tree.parent / "curl.log"
    return log.read_text().splitlines() if log.exists() else []


def test_existing_binaries_are_kept_without_downloading(tree):
    root, stubs = tree
    for name in BINARIES:
        (root / "utils" / name).write_bytes(b"custom build")
    result = _fetch(root, stubs)
    assert result.returncode == 0, result.stderr
    assert "add --force-download to override" in result.stdout
    assert not _curl_calls(root)
    assert all((root / "utils" / n).read_bytes() == b"custom build" for n in BINARIES)


def test_bad_download_keeps_existing_binary_and_fails(tree):
    root, stubs = tree
    for name in BINARIES:
        (root / "utils" / name).write_bytes(b"custom build")
    result = _fetch(root, stubs, "--force-download")
    assert result.returncode == 1
    assert "SHA-256 verification failed" in result.stderr
    assert len(_curl_calls(root)) == 2
    assert all(
        "releases/download/v" in call and "elbencho-static-" in call
        for call in _curl_calls(root)
    )
    assert all((root / "utils" / n).read_bytes() == b"custom build" for n in BINARIES)
    assert not list((root / "utils").glob("*.download.*"))


def test_unknown_option_is_a_usage_error(tree):
    root, stubs = tree
    result = _fetch(root, stubs, "--force")
    assert result.returncode == 2
    assert "Unknown option: --force" in result.stderr
    assert not _curl_calls(root)


def test_tarball_builds_with_a_warning_when_the_fetch_fails(tmp_path):
    root = tmp_path / "source"
    (root / "utils/build").mkdir(parents=True)
    (root / "lib").mkdir()
    for path in ("utils/build_tarball.sh", "utils/build/fetch_elbencho.sh"):
        shutil.copy2(ROOT / path, root / path)
    shutil.copy2(ROOT / "lib/project_version.sh", root / "lib/project_version.sh")
    (root / "NOTICE").write_text("notice\n")
    for name in ("warp", "warp.aarch64", "s3test", "s3test.aarch64"):
        (root / "utils" / name).write_bytes(b"custom")
        (root / "utils" / name).chmod(0o755)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    (stubs / "curl").write_text("#!/bin/sh\nexit 22\n")
    (stubs / "curl").chmod(0o755)
    environment = {
        key: value for key, value in os.environ.items() if key != "OBJ_AUTH_FILE"
    }
    environment["PATH"] = f"{stubs}:{environment['PATH']}"
    result = subprocess.run(
        ["bash", str(root / "utils/build_tarball.sh")],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Failed to download elbencho amd64" in result.stderr
    assert "lack the elbencho binaries" in result.stderr
    assert Path(tmp_path / "storage-scale-test.tar.gz").is_file()
