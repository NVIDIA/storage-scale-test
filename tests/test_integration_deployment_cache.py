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

"""Regression tests for content-addressed integration deployments."""

import importlib.util
import json
import os
import subprocess
import sys
import tarfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PATH = _REPO_ROOT / "integration-tests" / "lib" / "deployment_cache.py"
_SPEC = importlib.util.spec_from_file_location(
    "integration_deployment_cache_under_test", _MODULE_PATH
)
assert _SPEC and _SPEC.loader
_CACHE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _CACHE
_SPEC.loader.exec_module(_CACHE)


class _Runner:
    """Run local commands and record deployment-builder invocations."""

    def __init__(self):
        self.builder_calls = 0

    def run(self, arguments, *, cwd=None, timeout=None):
        """Run one command using the integration runner's result shape."""
        command = [str(argument) for argument in arguments]
        if command[0].endswith("build_tarball.sh"):
            self.builder_calls += 1
        result = subprocess.run(
            command,
            cwd=cwd,
            timeout=timeout,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(
                f"command failed ({result.returncode}): {command}\n{result.stderr}"
            )
        return SimpleNamespace(stdout=result.stdout, stderr=result.stderr)


def _write(path: Path, content: str, mode: int = 0o644) -> None:
    """Write one fixture file with a controlled mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal tracked tree with a compatible archive builder."""
    repository = tmp_path / "repository"
    repository.mkdir()
    _write(repository / "NOTICE", "notice\n")
    _write(repository / "payload.txt", "first\n")
    _write(
        repository / "utils" / "build_tarball.sh",
        """#!/usr/bin/env bash
set -eu
[[ $# -eq 0 ]]
printf 'generated\n' > utils/generated-tool
tar -czf ../storage-scale-test.tar.gz .
""",
        0o755,
    )
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    binary = tmp_path / "elbencho"
    _write(binary, "#!/usr/bin/env bash\nexit 0\n", 0o755)
    return repository, binary


def _request(tmp_path: Path, repository: Path, binary: Path):
    """Return a standard request for one fixture deployment."""
    return _CACHE.DeploymentCacheRequest(
        repo_root=repository,
        cache_root=tmp_path / "cache",
        architecture="x86_64",
        binary=binary,
        binary_name="elbencho",
    )


def _manifest(deployment) -> dict[str, object]:
    """Read a cached deployment's published manifest."""
    return json.loads(deployment.manifest.read_text(encoding="utf-8"))


def test_reuses_verified_archive_for_identical_inputs(tmp_path):
    """A second request reuses the archive built from the same snapshot."""
    repository, binary = _repository(tmp_path)
    runner = _Runner()
    request = _request(tmp_path, repository, binary)

    first = _CACHE.get_or_build_deployment(runner, request)
    second = _CACHE.get_or_build_deployment(runner, request)

    assert not first.cache_hit
    assert second.cache_hit
    assert first.archive == second.archive
    assert runner.builder_calls == 1
    document = _manifest(first)
    assert document["input_digest"] == first.key
    assert document["archive"]["sha256"]
    assert document["identity"]["binary"]["path"] == "utils/elbencho"


def test_snapshot_uses_current_tracked_content_and_excludes_untracked(tmp_path):
    """The archive reflects tracked edits without admitting untracked files."""
    repository, binary = _repository(tmp_path)
    runner = _Runner()
    request = _request(tmp_path, repository, binary)
    first = _CACHE.get_or_build_deployment(runner, request)
    _write(repository / "payload.txt", "modified\n")
    _write(repository / "untracked.txt", "do not package\n")

    second = _CACHE.get_or_build_deployment(runner, request)

    assert first.key != second.key
    with tarfile.open(second.archive, "r:gz") as archive:
        names = archive.getnames()
        payload = archive.extractfile("./payload.txt")
        assert payload is not None
        assert payload.read() == b"modified\n"
        assert "./utils/generated-tool" in names
    assert "./untracked.txt" not in names


def test_snapshot_honors_tracked_working_tree_deletions(tmp_path):
    """A tracked file deleted locally is absent from the archive and identity."""
    repository, binary = _repository(tmp_path)
    runner = _Runner()
    request = _request(tmp_path, repository, binary)
    first = _CACHE.get_or_build_deployment(runner, request)
    (repository / "payload.txt").unlink()

    second = _CACHE.get_or_build_deployment(runner, request)

    assert first.key != second.key
    with tarfile.open(second.archive, "r:gz") as archive:
        assert "./payload.txt" not in archive.getnames()
    paths = [
        entry["path"] for entry in _manifest(second)["identity"]["source"]["entries"]
    ]
    assert "payload.txt" not in paths


def test_file_mode_participates_in_cache_identity(tmp_path):
    """Changing a tracked mode invalidates an otherwise identical snapshot."""
    repository, binary = _repository(tmp_path)
    runner = _Runner()
    request = _request(tmp_path, repository, binary)
    first = _CACHE.get_or_build_deployment(runner, request)
    (repository / "payload.txt").chmod(0o600)

    second = _CACHE.get_or_build_deployment(runner, request)

    assert first.key != second.key
    entries = _manifest(second)["identity"]["source"]["entries"]
    payload = next(entry for entry in entries if entry["path"] == "payload.txt")
    assert payload["mode"] == 0o600


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("architecture", "aarch64"),
        ("recipe", 2),
    ),
)
def test_build_identity_inputs_invalidate_cache(tmp_path, field, value):
    """Architecture and builder recipe are cache-key inputs."""
    repository, binary = _repository(tmp_path)
    runner = _Runner()
    request = _request(tmp_path, repository, binary)
    first = _CACHE.get_or_build_deployment(runner, request)

    second = _CACHE.get_or_build_deployment(runner, replace(request, **{field: value}))

    assert first.key != second.key
    assert runner.builder_calls == 2


def test_binary_and_runtime_content_invalidate_cache(tmp_path):
    """External executable and runtime identities participate in the key."""
    repository, binary = _repository(tmp_path)
    runtime = tmp_path / "runtime"
    _write(runtime / "lib" / "loader.so", "first runtime\n")
    runner = _Runner()
    request = replace(_request(tmp_path, repository, binary), runtime=runtime)
    first = _CACHE.get_or_build_deployment(runner, request)
    _write(binary, "#!/usr/bin/env bash\necho changed\n", 0o755)

    second = _CACHE.get_or_build_deployment(runner, request)
    _write(runtime / "lib" / "loader.so", "second runtime\n")
    third = _CACHE.get_or_build_deployment(runner, request)

    assert len({first.key, second.key, third.key}) == 3


def test_runtime_directory_mode_participates_in_cache_identity(tmp_path):
    """Runtime directory layout and modes cannot alias one cache entry."""
    repository, binary = _repository(tmp_path)
    runtime = tmp_path / "runtime"
    (runtime / "empty").mkdir(parents=True)
    (runtime / "empty").chmod(0o755)
    runner = _Runner()
    request = replace(_request(tmp_path, repository, binary), runtime=runtime)
    first = _CACHE.get_or_build_deployment(runner, request)
    (runtime / "empty").chmod(0o700)

    second = _CACHE.get_or_build_deployment(runner, request)

    assert first.key != second.key


def test_unsafe_binary_name_is_rejected(tmp_path):
    """A configured binary name cannot escape the snapshot's utils directory."""
    repository, binary = _repository(tmp_path)
    request = replace(
        _request(tmp_path, repository, binary), binary_name="../../outside"
    )

    with pytest.raises(_CACHE.DeploymentCacheError, match="not a basename"):
        _CACHE.get_or_build_deployment(_Runner(), request)


def test_corrupt_archive_is_rebuilt_under_same_key(tmp_path):
    """Checksum validation prevents reuse of a damaged cached archive."""
    repository, binary = _repository(tmp_path)
    runner = _Runner()
    request = _request(tmp_path, repository, binary)
    first = _CACHE.get_or_build_deployment(runner, request)
    first.archive.write_bytes(b"corrupt")

    rebuilt = _CACHE.get_or_build_deployment(runner, request)

    assert rebuilt.key == first.key
    assert not rebuilt.cache_hit
    assert runner.builder_calls == 2
    assert rebuilt.archive.read_bytes() != b"corrupt"
    assert not any(
        path.name.startswith(".invalid-") for path in request.cache_root.iterdir()
    )


def test_builder_receives_snapshot_not_working_tree(tmp_path):
    """The existing builder executes only from the manifested snapshot."""
    repository, binary = _repository(tmp_path)

    class SnapshotRunner(_Runner):
        """Record the working directory used for the archive builder."""

        def __init__(self):
            super().__init__()
            self.builder_cwd = None
            self.builder_arguments = None

        def run(self, arguments, *, cwd=None, timeout=None):
            if str(arguments[0]).endswith("build_tarball.sh"):
                self.builder_cwd = Path(cwd)
                self.builder_arguments = [str(argument) for argument in arguments]
                assert self.builder_cwd != repository
                assert self.builder_cwd.name == "source"
            return super().run(arguments, cwd=cwd, timeout=timeout)

    runner = SnapshotRunner()

    _CACHE.get_or_build_deployment(runner, _request(tmp_path, repository, binary))

    assert runner.builder_cwd is not None
    assert runner.builder_arguments == [
        str(runner.builder_cwd / "utils" / "build_tarball.sh")
    ]


def test_unsafe_tracked_symlink_is_rejected(tmp_path):
    """A tracked symbolic link cannot escape the immutable snapshot."""
    repository, binary = _repository(tmp_path)
    os.symlink("../outside", repository / "escape")
    subprocess.run(["git", "add", "escape"], cwd=repository, check=True)

    with pytest.raises(_CACHE.DeploymentCacheError, match="unsafe tracked symbolic"):
        _CACHE.get_or_build_deployment(
            _Runner(), _request(tmp_path, repository, binary)
        )
