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

"""Contracts for the release source scenario's elbencho and version checks."""

import subprocess
import sys
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPOSITORY_ROOT / "integration-tests" / "lib"))

import filesystem_integration as _INTEGRATION  # pylint: disable=wrong-import-position

_require = _INTEGRATION._require_release_reports  # pylint: disable=protected-access
_fetch = _INTEGRATION._fetch_release_elbencho  # pylint: disable=protected-access


class _FetchRunner:  # pylint: disable=too-few-public-methods
    """Record fetch_elbencho.sh runs and answer with one return code."""

    def __init__(self, returncode: int, downloaded: bytes | None = None) -> None:
        self.returncode = returncode
        self.downloaded = downloaded
        self.checks: list[bool] = []

    def run(self, args, *, timeout, check=True):
        del timeout
        if args[0] == "file":
            assert Path(args[1]).is_file()
            return subprocess.CompletedProcess(args, 0, "ELF x86-64 executable", "")
        if len(args) > 1 and args[1] == "--help":
            return subprocess.CompletedProcess(args, 0, "elbencho help", "")
        self.checks.append(check)
        if self.downloaded is not None:
            binary = Path(args[0]).parent.parent / "elbencho"
            binary.write_bytes(self.downloaded)
            binary.chmod(0o755)
        if check and self.returncode:
            raise subprocess.CalledProcessError(self.returncode, args, "out\n", "503\n")
        return subprocess.CompletedProcess(args, self.returncode, "out\n", "503\n")


def _fetch_into(tmp_path: Path, runner: _FetchRunner) -> Path:
    (tmp_path / "release" / "utils").mkdir(parents=True)
    binary = tmp_path / "harness-elbencho"
    binary.write_bytes(b"harness elbencho")
    _fetch(runner, tmp_path / "release", tmp_path, binary, "elbencho")
    return tmp_path / "release" / "utils" / "elbencho"


def test_github_actions_must_download_elbencho(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    runner = _FetchRunner(0)
    assert not _fetch_into(tmp_path, runner).exists()
    assert runner.checks == [True]


def test_blocked_download_falls_back_outside_github_actions(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    runner = _FetchRunner(1)
    copied = _fetch_into(tmp_path, runner)
    assert runner.checks == [False]
    assert copied.read_bytes() == b"harness elbencho"
    assert copied.stat().st_mode & 0o777 == 0o755
    assert (tmp_path / "fetch-elbencho.log").read_text(encoding="utf-8") == "out\n503\n"


def test_successful_download_is_kept_outside_github_actions(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    downloaded = _fetch_into(tmp_path, _FetchRunner(0, b"downloaded static binary"))
    assert downloaded.read_bytes() == b"downloaded static binary"


def _harness_runtime(tmp_path: Path) -> tuple[Path, Path]:
    binary = tmp_path / "harness-elbencho"
    binary.write_bytes(b"harness runtime wrapper")
    runtime = tmp_path / "harness-runtime"
    (runtime / "usr/bin").mkdir(parents=True)
    (runtime / "usr/bin/elbencho").write_bytes(b"dynamic elbencho")
    (runtime / "lib").mkdir()
    (runtime / "lib/loader.so").write_bytes(b"runtime loader")
    return binary, runtime


def test_failed_ci_download_does_not_copy_container_reference(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    root = tmp_path / "release"
    (root / "utils").mkdir(parents=True)
    binary, runtime = _harness_runtime(tmp_path)
    runner = _FetchRunner(1)

    with pytest.raises(subprocess.CalledProcessError):
        _fetch(runner, root, tmp_path, binary, "elbencho", runtime)

    assert runner.checks == [True]
    assert not (root / "utils/elbencho").exists()
    assert not (root / "utils/elbencho-runtime").exists()


@pytest.mark.parametrize("fetch_status", [0, 1])
def test_runtime_is_copied_only_after_failed_download(
    tmp_path, monkeypatch, fetch_status
):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    root = tmp_path / "release"
    (root / "utils").mkdir(parents=True)
    binary, runtime = _harness_runtime(tmp_path)
    runner = _FetchRunner(
        fetch_status, b"downloaded static binary" if fetch_status == 0 else None
    )

    copied = _fetch(runner, root, tmp_path, binary, "elbencho", runtime)

    assert copied is bool(fetch_status)
    assert runner.checks == [False]
    installed = root / "utils/elbencho"
    assert installed.stat().st_mode & 0o777 == 0o755
    bundled = root / "utils/elbencho-runtime"
    if fetch_status:
        assert installed.read_bytes() == binary.read_bytes()
        assert (bundled / "usr/bin/elbencho").read_bytes() == b"dynamic elbencho"
        assert (bundled / "lib/loader.so").read_bytes() == b"runtime loader"
        assert (runtime / "lib/loader.so").is_file()
    else:
        assert installed.read_bytes() == b"downloaded static binary"
        assert not bundled.exists()
    assert (tmp_path / "fetch-elbencho.log").read_text(encoding="utf-8") == "out\n503\n"


@pytest.mark.parametrize(
    ("download", "fetch_status"), [(True, 1), (True, 0), (False, 1)]
)
def test_release_tree_download_policy_with_container_reference(
    tmp_path, monkeypatch, download, fetch_status
):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    root = tmp_path / "release"
    (root / "utils").mkdir(parents=True)
    binary, runtime = _harness_runtime(tmp_path)
    archive = tmp_path / "source.tar.gz"
    monkeypatch.setattr(
        _INTEGRATION, "build_release_source_archive", lambda *_: archive
    )
    monkeypatch.setattr(_INTEGRATION, "_extract_checked_archive", lambda *_: root)
    monkeypatch.setattr(_INTEGRATION, "_require_release_version", lambda *_: "v1.2.3")
    runner = _FetchRunner(
        fetch_status, b"downloaded static binary" if fetch_status == 0 else None
    )

    prepared = (
        _INTEGRATION._prepare_release_source_tree(  # pylint: disable=protected-access
            runner,
            _REPOSITORY_ROOT,
            tmp_path / "build",
            tmp_path,
            binary,
            "elbencho",
            "x86_64",
            runtime,
            download=download,
        )
    )

    assert prepared == root
    assert runner.checks == ([False] if download else [])
    copied = not download or fetch_status != 0
    assert (root / "utils/elbencho-runtime").exists() is copied
    expected = binary.read_bytes() if copied else b"downloaded static binary"
    assert (root / "utils/elbencho").read_bytes() == expected


def _write_log(log_dir: Path, produced: str, reported: str, prefix: str = "") -> None:
    log = log_dir / "report-0001" / "extract-elbencho.log"
    log.parent.mkdir(parents=True)
    log.write_text(
        f"{prefix}Produced by storage-scale-test: {produced}\n"
        f"{prefix}Reported by storage-scale-test: {reported}\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("prefix", ["", "- "])
def test_exact_release_versions_pass(tmp_path, prefix):
    _write_log(tmp_path, "v1.2.3", "v1.2.3", prefix)
    _require(tmp_path, "v1.2.3")


@pytest.mark.parametrize(
    ("produced", "reported"),
    [
        ("v1.2.3-modified", "v1.2.3"),
        ("v1.2.3", "v1.2.30"),
        ("v1.2.3, v1.2.4", "v1.2.3"),
    ],
)
def test_other_versions_fail(tmp_path, produced, reported):
    _write_log(tmp_path, produced, reported)
    with pytest.raises(_INTEGRATION.IntegrationTestError, match="not release v1.2.3"):
        _require(tmp_path, "v1.2.3")


def test_missing_reports_fail(tmp_path):
    with pytest.raises(_INTEGRATION.IntegrationTestError, match="wrote no reports"):
        _require(tmp_path, "v1.2.3")
