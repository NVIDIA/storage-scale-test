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

"""Network fallback must preserve the integration harness's binary checks."""

# These tests exercise the integration driver's private acquisition boundaries.
# pylint: disable=protected-access

import hashlib
import io
import sys
import tarfile
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPOSITORY_ROOT / "integration-tests" / "lib"))

import filesystem_integration as _INTEGRATION  # pylint: disable=wrong-import-position

_CI_VARIABLE = "GITHUB_ACTIONS"
_BINARY_CONTENT = b"static upstream binary\n"
_ARCHITECTURES = ("x86_64", "aarch64")
_CONTAINER_HELPER = "_ensure_container_elbencho"
_ASSET_URL = "https://upstream/asset"


@pytest.mark.parametrize("architecture", _ARCHITECTURES)
@pytest.mark.parametrize("github_actions", [False, True])
def test_initial_network_failure_falls_back_only_outside_ci(
    tmp_path, monkeypatch, architecture, github_actions
):
    """A fresh NFS run recovers automatically, while CI proves the download."""
    if github_actions:
        monkeypatch.setenv(_CI_VARIABLE, "true")
    else:
        monkeypatch.delenv(_CI_VARIABLE, raising=False)
    failure = _INTEGRATION.UpstreamDownloadError("HTTP Error 403: rate limited")
    config = SimpleNamespace(state_dir=tmp_path)
    runner = object()
    calls = []
    binary_name = _INTEGRATION.ELBENCHO_ARCHIVES[architecture][2]
    container_result = (tmp_path / "wrapper", binary_name, tmp_path / "runtime")

    def fail_download(*_args):
        raise failure

    def container_fallback(*args):
        calls.append(args)
        return container_result

    monkeypatch.setattr(_INTEGRATION, "_download_archive", fail_download)
    monkeypatch.setattr(_INTEGRATION, _CONTAINER_HELPER, container_fallback)
    if github_actions:
        with pytest.raises(_INTEGRATION.UpstreamDownloadError) as caught:
            _INTEGRATION._ensure_elbencho(runner, config, architecture, "nfs")
        assert caught.value is failure
        assert calls == []
    else:
        result = _INTEGRATION._ensure_elbencho(runner, config, architecture, "nfs")
        assert result == container_result
        assert calls == [(runner, tmp_path / "test-cache", architecture, binary_name)]


def _serve_archive(monkeypatch, content):
    """Serve one bounded archive response without contacting the network."""
    monkeypatch.setattr(_INTEGRATION, "_asset_url", lambda _name: _ASSET_URL)

    def response(*_args):
        stream = io.BytesIO(content)
        stream.headers = {"Content-Length": str(len(content))}
        return stream

    monkeypatch.setattr(_INTEGRATION, "_request", response)


@pytest.mark.parametrize("architecture", _ARCHITECTURES)
def test_checksum_failure_never_uses_container_fallback(
    tmp_path, monkeypatch, architecture
):
    """A corrupt upstream response cannot be hidden by successful Docker access."""
    monkeypatch.delenv(_CI_VARIABLE, raising=False)
    _serve_archive(monkeypatch, b"corrupt archive")
    calls = []
    monkeypatch.setattr(
        _INTEGRATION, _CONTAINER_HELPER, lambda *args: calls.append(args)
    )
    config = SimpleNamespace(state_dir=tmp_path)

    with pytest.raises(_INTEGRATION.IntegrationTestError, match="checksum mismatch"):
        _INTEGRATION._ensure_elbencho(object(), config, architecture, "nfs")

    assert calls == []
    assert not any((tmp_path / "test-cache").iterdir())


@pytest.mark.parametrize("architecture", _ARCHITECTURES)
def test_successful_static_download_stays_a_static_binary(
    tmp_path, monkeypatch, architecture
):
    """The normal NFS path keeps its verified executable and needs no runtime bundle."""
    monkeypatch.delenv(_CI_VARIABLE, raising=False)
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
        member = tarfile.TarInfo("elbencho")
        member.size = len(_BINARY_CONTENT)
        archive.addfile(member, io.BytesIO(_BINARY_CONTENT))
    content = archive_bytes.getvalue()
    archive_name, _, binary_name = _INTEGRATION.ELBENCHO_ARCHIVES[architecture]
    monkeypatch.setitem(
        _INTEGRATION.ELBENCHO_ARCHIVES,
        architecture,
        (archive_name, hashlib.sha256(content).hexdigest(), binary_name),
    )
    _serve_archive(monkeypatch, content)
    calls = []
    monkeypatch.setattr(
        _INTEGRATION, _CONTAINER_HELPER, lambda *args: calls.append(args)
    )

    binary, staged_name, runtime = _INTEGRATION._ensure_elbencho(
        object(), SimpleNamespace(state_dir=tmp_path), architecture, "nfs"
    )

    assert binary.read_bytes() == _BINARY_CONTENT
    assert binary.stat().st_mode & 0o777 == 0o755
    assert staged_name == binary_name
    assert runtime is None
    assert calls == []


def test_exhausted_network_retries_are_classified_for_fallback(monkeypatch):
    """The real request boundary exposes exhausted network failures distinctly."""
    attempts = []

    def blocked(request, *, timeout):
        attempts.append((request.full_url, timeout))
        raise urllib.error.URLError("proxy refused connection")

    monkeypatch.setattr(_INTEGRATION.urllib.request, "urlopen", blocked)
    monkeypatch.setattr(_INTEGRATION.time, "sleep", lambda _seconds: None)

    with pytest.raises(_INTEGRATION.UpstreamDownloadError, match="after 3 attempts"):
        _INTEGRATION._request(_ASSET_URL)

    assert attempts == [(_ASSET_URL, 60)] * 3


def test_container_bundle_cache_cannot_reuse_or_overwrite_static_binary(
    tmp_path, monkeypatch
):
    """Static archives and portable container wrappers retain separate identities."""
    architecture = "x86_64"
    binary_name = _INTEGRATION.ELBENCHO_ARCHIVES[architecture][2]
    basename = f"{_INTEGRATION.ELBENCHO_VERSION}-{binary_name}"
    static = tmp_path / basename
    static.write_bytes(_BINARY_CONTENT)
    (tmp_path / f"{basename}.runtime").mkdir()
    _INTEGRATION._write_sbx_bundle_marker(
        tmp_path / f"{basename}.bundle.json",
        _INTEGRATION._sbx_bundle_document(architecture, binary_name),
    )
    calls = []
    wrapper_content = b"#!/bin/bash\n# container runtime wrapper\n"

    def extract(_runner, cache, binary, runtime, arch):
        calls.append((cache, arch))
        binary.write_bytes(wrapper_content)
        runtime.mkdir()

    monkeypatch.setattr(_INTEGRATION, "_extract_container_elbencho", extract)
    runner = object()
    result = _INTEGRATION._ensure_container_elbencho(
        runner, tmp_path, architecture, binary_name
    )
    reused = _INTEGRATION._ensure_container_elbencho(
        runner, tmp_path, architecture, binary_name
    )

    binary, staged_name, runtime = result
    assert reused == result
    assert calls == [(tmp_path / "container", architecture)]
    assert binary == tmp_path / "container" / basename
    assert binary.read_bytes() == wrapper_content
    assert runtime == tmp_path / "container" / f"{basename}.runtime"
    assert runtime.is_dir()
    assert staged_name == binary_name
    assert static.read_bytes() == _BINARY_CONTENT
