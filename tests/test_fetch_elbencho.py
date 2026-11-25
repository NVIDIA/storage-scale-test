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

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

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
    for library in ("project_version.sh", "_platform_functions.sh"):
        shutil.copy2(ROOT / "lib" / library, tree / "lib" / library)
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


def _binary(path: Path, version: str | None) -> None:
    """A runnable stand-in for elbencho that reports VERSION."""
    line = f" * Version: {version}\n" if version else "nothing\n"
    path.write_text(f"#!/bin/sh\ncat <<'EOF'\nelbencho\n{line}EOF\n")
    path.chmod(0o755)


def _stamp(path: Path, tag: str, sha: str | None = None) -> None:
    sha = sha or hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(path.name + ".version").write_text(f"{tag} {sha}\n")


def test_existing_binaries_at_the_floor_are_kept_without_downloading(tree):
    root, stubs = tree
    for name in BINARIES:
        _binary(root / "utils" / name, "3.4-1")
    before = {n: (root / "utils" / n).read_bytes() for n in BINARIES}
    result = _fetch(root, stubs)
    assert result.returncode == 0, result.stderr
    assert "add --force-download to override" in result.stdout
    assert "version 3.4-1" in result.stdout
    assert not _curl_calls(root)
    assert before == {n: (root / "utils" / n).read_bytes() for n in BINARIES}


def test_newer_binary_is_kept(tree):
    root, stubs = tree
    for name in BINARIES:
        _binary(root / "utils" / name, "3.10-1")
    assert _fetch(root, stubs).returncode == 0
    assert not _curl_calls(root)


def test_binary_below_the_floor_is_replaced(tree):
    root, stubs = tree
    _binary(root / "utils/elbencho", "3.2-1")
    _binary(root / "utils/elbencho.aarch64", "3.4-1")
    result = _fetch(root, stubs)
    assert "replacing elbencho amd64: version 3.2-1, below" in result.stdout
    assert [c for c in _curl_calls(root) if "x86_64" in c]
    assert not [c for c in _curl_calls(root) if "aarch64" in c]


@pytest.mark.parametrize("report", [None, "banana"])
def test_binary_without_a_usable_version_is_replaced(tree, report):
    root, stubs = tree
    _binary(root / "utils/elbencho", report)
    (root / "utils/elbencho.aarch64").write_bytes(b"custom build")
    result = _fetch(root, stubs)
    assert "version unknown, below" in result.stdout or "banana" in result.stdout
    assert len(_curl_calls(root)) == 2


def test_binary_that_cannot_run_is_kept_on_a_matching_current_stamp(tree):
    root, stubs = tree
    for name in BINARIES:
        (root / "utils" / name).write_bytes(b"\x7fELF foreign")
        (root / "utils" / name).chmod(0o755)
        _stamp(root / "utils" / name, "v3.4-1")
    result = _fetch(root, stubs)
    assert result.returncode == 0, result.stderr
    assert "version v3.4-1" in result.stdout
    assert not _curl_calls(root)


@pytest.mark.parametrize("stamped", ["old tag", "stale sha", "no stamp"])
def test_binary_that_cannot_run_is_replaced_without_a_current_stamp(tree, stamped):
    root, stubs = tree
    for name in BINARIES:
        (root / "utils" / name).write_bytes(b"\x7fELF foreign")
        (root / "utils" / name).chmod(0o755)
        if stamped == "old tag":
            _stamp(root / "utils" / name, "v3.2-1")
        elif stamped == "stale sha":
            _stamp(root / "utils" / name, "v3.4-1", "0" * 64)
    _fetch(root, stubs)
    assert len(_curl_calls(root)) == 2


def test_force_download_replaces_a_current_binary(tree):
    root, stubs = tree
    for name in BINARIES:
        _binary(root / "utils" / name, "3.4-1")
    _fetch(root, stubs, "--force-download")
    assert len(_curl_calls(root)) == 2


def _release_archive(tmp_path: Path) -> tuple[Path, str]:
    """A tarball holding a fake elbencho at the floor, and its SHA-256."""
    source = tmp_path / "release"
    source.mkdir()
    _binary(source / "elbencho", "3.4-1")
    archive = tmp_path / "release.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(source / "elbencho", arcname="elbencho")
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


def test_download_installs_the_binary_and_stamps_it(tree, tmp_path):
    root, stubs = tree
    archive, sha = _release_archive(tmp_path)
    script = root / "utils/build/fetch_elbencho.sh"
    text = script.read_text()
    for name in ("X86_64", "AARCH64"):
        text = text.replace(
            f'readonly ELBENCHO_SHA256_{name}="',
            f'readonly ELBENCHO_SHA256_{name}="{sha}"\n: "',
        )
    script.write_text(text)
    (stubs / "curl").write_text(
        f'#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n'
        f'    if [ "$1" = -o ]; then cp {archive} "$2"; fi\n    shift\ndone\n'
    )
    tag = [
        line.split("'")[1]
        for line in text.splitlines()
        if line.startswith("readonly ELBENCHO_VERSION_TAG=")
    ][0]
    result = _fetch(root, stubs)
    assert result.returncode == 0, result.stderr
    for name in BINARIES:
        binary = root / "utils" / name
        assert binary.stat().st_mode & 0o111
        stamp = binary.with_name(name + ".version").read_text().split()
        assert stamp == [tag, hashlib.sha256(binary.read_bytes()).hexdigest()]
    # The stamped, current binaries are now kept.
    again = _fetch(root, stubs)
    assert "using existing" in again.stdout


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
    for library in ("project_version.sh", "_platform_functions.sh"):
        shutil.copy2(ROOT / "lib" / library, root / "lib" / library)
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
