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

"""Content-addressed deployment archives for the integration harness."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

CACHE_SCHEMA = 1
DEFAULT_RECIPE = 1
ARCHIVE_NAME = "storage-scale-test.tar.gz"
MANIFEST_NAME = "manifest.json"
BUILD_LOG_NAME = "build-tarball.log"


class DeploymentCacheError(RuntimeError):
    """A deployment snapshot or cache entry is unsafe or inconsistent."""


@dataclass(frozen=True)
class DeploymentCacheRequest:
    """Inputs needed to build one content-addressed deployment archive."""

    repo_root: Path
    cache_root: Path
    architecture: str
    binary: Path
    binary_name: str
    runtime: Path | None = None
    recipe: int = DEFAULT_RECIPE
    build_timeout: int = 600


@dataclass(frozen=True)
class CachedDeployment:
    """One verified archive returned from the deployment cache."""

    key: str
    archive: Path
    manifest: Path
    build_log: Path
    cache_hit: bool


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of one regular file."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(document: object) -> str:
    """Hash one JSON-compatible document in a stable representation."""
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_relative(name: str) -> Path:
    """Validate and convert a Git-provided repository-relative path."""
    relative = PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise DeploymentCacheError(f"unsafe tracked path from git: {name!r}")
    return Path(*relative.parts)


def _tracked_names(runner: Any, repo_root: Path) -> tuple[str, ...]:
    """Return tracked paths in a deterministic order."""
    result = runner.run(
        ["git", "ls-files", "-z", "--cached"], cwd=repo_root, timeout=30
    )
    names = tuple(name for name in result.stdout.split("\0") if name)
    if not names:
        raise DeploymentCacheError(f"repository has no tracked files: {repo_root}")
    return tuple(sorted(names))


def _copy_tracked_file(source: Path, target: Path) -> None:
    """Copy one regular file or safe relative symbolic link."""
    mode = source.lstat().st_mode
    target.parent.mkdir(parents=True, exist_ok=True)
    if stat.S_ISREG(mode):
        shutil.copy2(source, target, follow_symlinks=False)
        return
    if stat.S_ISLNK(mode):
        link = os.readlink(source)
        link_path = PurePosixPath(link)
        if link_path.is_absolute() or ".." in link_path.parts:
            raise DeploymentCacheError(
                f"unsafe tracked symbolic link: {source} -> {link}"
            )
        target.symlink_to(link)
        return
    raise DeploymentCacheError(
        f"deployment snapshot requires a regular file or relative symlink: {source}"
    )


def _manifest_entry(root: Path, path: Path) -> dict[str, object]:
    """Describe one file-system entry below a snapshot root."""
    mode = path.lstat().st_mode
    relative = path.relative_to(root).as_posix()
    if stat.S_ISREG(mode):
        kind = "file"
        digest = _sha256(path)
    elif stat.S_ISDIR(mode):
        kind = "directory"
        digest = hashlib.sha256(b"").hexdigest()
    elif stat.S_ISLNK(mode):
        kind = "symlink"
        digest = hashlib.sha256(os.readlink(path).encode("utf-8")).hexdigest()
    else:
        raise DeploymentCacheError(f"unsupported snapshot entry: {path}")
    return {
        "path": relative,
        "mode": stat.S_IMODE(mode),
        "type": kind,
        "sha256": digest,
    }


def _manifest_for_paths(root: Path, paths: tuple[Path, ...]) -> dict[str, object]:
    """Describe an explicit ordered set of paths."""
    entries = [_manifest_entry(root, path) for path in paths]
    document: dict[str, object] = {"entries": entries}
    document["sha256"] = _canonical_digest(entries)
    return document


def _copy_tracked_snapshot(
    runner: Any, repo_root: Path, destination: Path
) -> dict[str, object]:
    """Materialize and describe the exact tracked working-tree snapshot."""
    names = _tracked_names(runner, repo_root)
    destination.mkdir(parents=True)
    copied: list[Path] = []
    for name in names:
        relative = _safe_relative(name)
        source = repo_root / relative
        if not source.exists() and not source.is_symlink():
            continue
        target = destination / relative
        _copy_tracked_file(source, target)
        copied.append(target)
    for directory in sorted(
        (path for path in destination.rglob("*") if stat.S_ISDIR(path.lstat().st_mode)),
        reverse=True,
    ):
        directory.chmod(0o755)
    destination.chmod(0o755)
    return _manifest_for_paths(destination, tuple(copied))


def _tree_paths(root: Path) -> tuple[Path, ...]:
    """Return all entries in a tree without following symbolic links."""
    paths: list[Path] = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        linked_directories = [name for name in dirnames if (base / name).is_symlink()]
        dirnames[:] = sorted(
            name for name in dirnames if name not in linked_directories
        )
        paths.extend(base / name for name in sorted(linked_directories))
        paths.extend(base / name for name in dirnames)
        paths.extend(base / name for name in sorted(filenames))
    return tuple(sorted(paths, key=lambda item: item.relative_to(root).as_posix()))


def _stage_binary_and_runtime(
    request: DeploymentCacheRequest, snapshot: Path
) -> tuple[dict[str, object], dict[str, object] | None]:
    """Seed external build inputs and return their staged identities."""
    if not request.binary.is_file() or request.binary.is_symlink():
        raise DeploymentCacheError(
            f"deployment binary is not a regular file: {request.binary}"
        )
    binary_name = PurePosixPath(request.binary_name)
    if (
        binary_name.is_absolute()
        or len(binary_name.parts) != 1
        or binary_name.name in ("", ".", "..")
    ):
        raise DeploymentCacheError(
            f"deployment binary name is not a basename: {request.binary_name!r}"
        )
    binary_target = snapshot / "utils" / binary_name.name
    binary_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(request.binary, binary_target)
    binary_target.chmod(0o755)
    binary_identity = _manifest_entry(snapshot, binary_target)

    if request.runtime is None:
        return binary_identity, None
    if not request.runtime.is_dir() or request.runtime.is_symlink():
        raise DeploymentCacheError(
            f"deployment runtime is not a directory: {request.runtime}"
        )
    runtime_target = snapshot / "utils" / "elbencho-runtime"
    shutil.copytree(request.runtime, runtime_target)
    return binary_identity, _manifest_for_paths(
        runtime_target, _tree_paths(runtime_target)
    )


def _identity_document(
    request: DeploymentCacheRequest,
    source_manifest: dict[str, object],
    binary_identity: dict[str, object],
    runtime_identity: dict[str, object] | None,
) -> dict[str, object]:
    """Build the cache-key document for one immutable snapshot."""
    return {
        "schema": CACHE_SCHEMA,
        "recipe": request.recipe,
        "architecture": request.architecture,
        "source": source_manifest,
        "binary": binary_identity,
        "runtime": runtime_identity,
    }


def _entry_paths(cache_root: Path, key: str) -> tuple[Path, Path, Path, Path]:
    """Return the entry and its fixed files."""
    entry = cache_root / key
    return (
        entry,
        entry / ARCHIVE_NAME,
        entry / MANIFEST_NAME,
        entry / BUILD_LOG_NAME,
    )


def _load_json(path: Path) -> dict[str, object] | None:
    """Read a JSON object, returning None for malformed input."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _valid_entry(cache_root: Path, key: str, identity: dict[str, object]) -> bool:
    """Return whether a cache entry exactly matches its expected identity."""
    entry, archive, manifest_path, build_log = _entry_paths(cache_root, key)
    if not entry.is_dir() or entry.is_symlink():
        return False
    if any(
        path.is_symlink() or not path.is_file()
        for path in (archive, manifest_path, build_log)
    ):
        return False
    manifest = _load_json(manifest_path)
    if manifest is None or manifest.get("identity") != identity:
        return False
    archive_record = manifest.get("archive")
    if not isinstance(archive_record, dict) or archive.stat().st_size <= 0:
        return False
    return (
        archive_record.get("bytes") == archive.stat().st_size
        and archive_record.get("sha256") == _sha256(archive)
        and manifest.get("input_digest") == key
    )


def _run_builder(
    runner: Any, request: DeploymentCacheRequest, staging: Path, snapshot: Path
) -> tuple[Path, str]:
    """Invoke the existing builder from an exact disposable snapshot copy."""
    build_parent = staging / "builder"
    build_tree = build_parent / "source"
    shutil.copytree(snapshot, build_tree, symlinks=True)
    builder = build_tree / "utils" / "build_tarball.sh"
    if not builder.is_file() or builder.is_symlink():
        raise DeploymentCacheError(f"deployment builder is absent: {builder}")
    result = runner.run([builder], cwd=build_tree, timeout=request.build_timeout)
    archive = build_parent / ARCHIVE_NAME
    if not archive.is_file() or archive.is_symlink() or archive.stat().st_size <= 0:
        raise DeploymentCacheError(f"deployment builder did not create {archive}")
    return archive, result.stdout + result.stderr


def _publish(
    cache_root: Path,
    key: str,
    identity: dict[str, object],
    archive: Path,
    build_log: str,
) -> bool:
    """Atomically publish one completed entry; return whether ours won."""
    publish: Path | None = cache_root / f".publish-{key}-{uuid.uuid4().hex}"
    publish.mkdir(mode=0o700)
    try:
        published_archive = publish / ARCHIVE_NAME
        shutil.copy2(archive, published_archive)
        (publish / BUILD_LOG_NAME).write_text(build_log, encoding="utf-8")
        manifest = {
            "schema": CACHE_SCHEMA,
            "input_digest": key,
            "identity": identity,
            "archive": {
                "bytes": published_archive.stat().st_size,
                "sha256": _sha256(published_archive),
            },
        }
        (publish / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        if _valid_entry(cache_root, key, identity):
            return False
        _replace_invalid_entry(cache_root, key, publish)
        publish = None
        return True
    finally:
        if publish is not None:
            shutil.rmtree(publish, ignore_errors=True)


def _replace_invalid_entry(cache_root: Path, key: str, publish: Path) -> None:
    """Replace an invalid entry while keeping the new entry atomically visible."""
    target = cache_root / key
    quarantine = cache_root / f".invalid-{key}-{uuid.uuid4().hex}"
    moved_invalid = False
    if target.exists() or target.is_symlink():
        os.replace(target, quarantine)
        moved_invalid = True
    try:
        os.replace(publish, target)
    except OSError:
        if moved_invalid and not target.exists():
            os.replace(quarantine, target)
        raise
    finally:
        if moved_invalid:
            if quarantine.is_dir() and not quarantine.is_symlink():
                shutil.rmtree(quarantine, ignore_errors=True)
            else:
                quarantine.unlink(missing_ok=True)


def get_or_build_deployment(
    runner: Any, request: DeploymentCacheRequest
) -> CachedDeployment:
    """Return a verified cached deployment built from one exact snapshot."""
    request.cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".deployment-staging-", dir=request.cache_root
    ) as temporary:
        staging = Path(temporary)
        snapshot = staging / "source"
        source_manifest = _copy_tracked_snapshot(runner, request.repo_root, snapshot)
        binary_identity, runtime_identity = _stage_binary_and_runtime(request, snapshot)
        build_input_manifest = _manifest_for_paths(snapshot, _tree_paths(snapshot))
        identity = _identity_document(
            request, source_manifest, binary_identity, runtime_identity
        )
        key = _canonical_digest(identity)
        entry, archive, manifest, build_log = _entry_paths(request.cache_root, key)
        if _valid_entry(request.cache_root, key, identity):
            return CachedDeployment(key, archive, manifest, build_log, True)

        built_archive, output = _run_builder(runner, request, staging, snapshot)
        if _manifest_for_paths(snapshot, _tree_paths(snapshot)) != build_input_manifest:
            raise DeploymentCacheError(
                "deployment builder modified its immutable source snapshot"
            )
        published = _publish(request.cache_root, key, identity, built_archive, output)
        if not _valid_entry(request.cache_root, key, identity):
            raise DeploymentCacheError(
                f"published deployment cache is invalid: {entry}"
            )
        return CachedDeployment(key, archive, manifest, build_log, not published)
