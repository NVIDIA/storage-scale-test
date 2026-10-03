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

"""Acquire pinned OCI charts before Helm mutates the integration cluster."""

import gzip
import hashlib
import json
import logging
import re
import subprocess
import tarfile
import tempfile
import time
import zlib
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml

from image_acquisition import (
    AcquisitionError,
    retry_delay,
    sanitize_acquisition_evidence,
    transient_acquisition_failure,
)

LOG = logging.getLogger("storage-scale-integration")
CHART_PULL_ATTEMPTS = 4
CHART_PULL_TIMEOUT_SECONDS = 60
CHART_PULL_DEADLINE_SECONDS = 240
MAX_CHART_BYTES = 64 * 1024 * 1024
MAX_CHART_CONTENT_BYTES = 128 * 1024 * 1024
MAX_CHART_MEMBERS = 20_000
MAX_CHART_METADATA_BYTES = 256 * 1024
CACHE_SCHEMA = 1


def _identity(reference: str, version: str) -> dict[str, Any]:
    """Reject ambiguous references and names that could escape owned storage."""
    try:
        parsed = urlsplit(reference)
    except ValueError as error:
        raise AcquisitionError("invalid OCI chart reference or version") from error
    name = parsed.path.rsplit("/", 1)[-1]
    if (
        len(reference) > 1024
        or parsed.scheme != "oci"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[a-z][a-z0-9-]{0,127}", name)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,127}", version)
    ):
        raise AcquisitionError("invalid OCI chart reference or version")
    return {
        "schema": CACHE_SCHEMA,
        "source": reference,
        "name": name,
        "version": version,
    }


def _safe_cache_directory(state_dir: Path) -> Path:
    """Keep chart publication away from symlinked paths."""
    cache = state_dir / "charts" / "oci"
    if any(path.is_symlink() for path in (cache, *cache.parents)):
        raise AcquisitionError(f"refusing symlinked chart cache: {cache}")
    cache.mkdir(mode=0o750, parents=True, exist_ok=True)
    return cache


def _regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise AcquisitionError(f"chart cache requires a regular file: {path}")


def _digest(path: Path) -> str:
    _regular_file(path)
    if path.stat().st_size > MAX_CHART_BYTES:
        raise AcquisitionError(f"chart archive exceeds its size limit: {path}")
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def _validate_member(member: tarfile.TarInfo, name: str) -> None:
    path = PurePosixPath(member.name)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not path.parts
        or path.parts[0] != name
        or not (member.isfile() or member.isdir())
    ):
        raise AcquisitionError(f"unsafe chart archive member: {member.name}")


def _read_chart_metadata(path: Path, name: str) -> dict[str, Any]:
    """Validate bounded archive structure without extracting untrusted paths."""
    metadata = None
    metadata_seen = False
    expanded = 0
    with tarfile.open(path, "r:gz") as archive:
        for count, member in enumerate(archive, 1):
            _validate_member(member, name)
            expanded += member.size
            if count > MAX_CHART_MEMBERS or expanded > MAX_CHART_CONTENT_BYTES:
                raise AcquisitionError(f"chart contents exceed their limits: {path}")
            if member.name != f"{name}/Chart.yaml":
                continue
            if metadata_seen or member.size > MAX_CHART_METADATA_BYTES:
                raise AcquisitionError(f"invalid or duplicate chart metadata: {path}")
            metadata_seen = True
            stream = archive.extractfile(member)
            if stream is None:
                raise AcquisitionError(f"missing chart metadata: {path}")
            with stream:
                metadata = yaml.safe_load(stream.read())
    if not isinstance(metadata, dict):
        raise AcquisitionError(f"missing or invalid chart metadata: {path}")
    return metadata


def _validate_chart(path: Path, identity: dict[str, Any]) -> str:
    checksum = _digest(path)
    try:
        _validate_gzip(path)
        metadata = _read_chart_metadata(path, identity["name"])
    except (
        tarfile.TarError,
        EOFError,
        gzip.BadGzipFile,
        zlib.error,
        yaml.YAMLError,
    ) as error:
        raise AcquisitionError(f"invalid chart archive {path}: {error}") from error
    if any(metadata.get(key) != identity[key] for key in ("name", "version")):
        raise AcquisitionError(f"chart name/version does not match request: {path}")
    return checksum


def _validate_gzip(path: Path) -> None:
    """Read through EOF to verify gzip trailers, not just tar end markers."""
    expanded = 0
    with gzip.open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            expanded += len(chunk)
            if expanded > MAX_CHART_CONTENT_BYTES:
                raise AcquisitionError(f"chart contents exceed their limits: {path}")


def _cached_chart(path: Path, identity: dict[str, Any]) -> bool:
    manifest = path.with_suffix(".json")
    if path.is_symlink() or manifest.is_symlink():
        raise AcquisitionError(f"refusing symlinked chart cache entry: {path}")
    if not path.exists() or not manifest.exists():
        return False
    _regular_file(manifest)
    if manifest.stat().st_size > MAX_CHART_METADATA_BYTES:
        raise AcquisitionError(
            f"chart cache manifest exceeds its size limit: {manifest}"
        )
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise AcquisitionError(f"invalid chart cache manifest: {manifest}") from error
    if not isinstance(document, dict) or any(
        document.get(key) != value for key, value in identity.items()
    ):
        raise AcquisitionError(f"chart cache identity mismatch: {path}")
    if _validate_chart(path, identity) != document.get("sha256"):
        raise AcquisitionError(f"chart cache checksum mismatch: {path}")
    return True


def _output_text(output: str | bytes | None) -> str:
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output or ""


def _pull_once(
    runner: Any, identity: dict[str, Any], directory: Path, timeout: float
) -> tuple[int, str]:
    try:
        result = runner.run(
            [
                "helm",
                "pull",
                identity["source"],
                "--version",
                identity["version"],
                "--destination",
                directory,
            ],
            check=False,
            timeout=timeout,
            sensitive=True,
        )
        detail = result.stderr.strip() or result.stdout.strip()
        return result.returncode, sanitize_acquisition_evidence(detail)
    except subprocess.TimeoutExpired as error:
        detail = (_output_text(error.stderr) or _output_text(error.stdout)).strip()
        return (
            -1,
            f"timed out after {timeout:.1f}s; {sanitize_acquisition_evidence(detail)}",
        )


def _publish_chart(source: Path, destination: Path, document: dict[str, Any]) -> None:
    """A manifest is the commit marker; incomplete publication is a cache miss."""
    manifest = source.with_suffix(".json")
    manifest.write_text(json.dumps(document, sort_keys=True) + "\n", encoding="utf-8")
    source.chmod(0o640)
    manifest.chmod(0o640)
    source.replace(destination)
    manifest.replace(destination.with_suffix(".json"))


def _download_chart(runner: Any, identity: dict[str, Any], destination: Path) -> Path:
    started = time.monotonic()
    deadline = started + CHART_PULL_DEADLINE_SECONDS
    last_error = "chart acquisition deadline expired"
    for attempt in range(1, CHART_PULL_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        with tempfile.TemporaryDirectory(
            prefix=".chart-pull-", dir=destination.parent
        ) as temporary:
            directory = Path(temporary)
            code, detail = _pull_once(
                runner, identity, directory, min(CHART_PULL_TIMEOUT_SECONDS, remaining)
            )
            if code == 0:
                source = directory / destination.name
                checksum = _validate_chart(source, identity)
                if time.monotonic() >= deadline:
                    raise AcquisitionError(
                        "chart verification exceeded the acquisition deadline"
                    )
                _publish_chart(source, destination, {**identity, "sha256": checksum})
                LOG.info(
                    "Acquired chart %s version %s on attempt %d/%d in %.1fs",
                    identity["source"],
                    identity["version"],
                    attempt,
                    CHART_PULL_ATTEMPTS,
                    time.monotonic() - started,
                )
                return destination
        last_error = detail or f"helm pull exited {code}"
        LOG.warning(
            "Chart acquisition %d/%d: source=%s version=%s rc=%d elapsed=%.1fs: %s",
            attempt,
            CHART_PULL_ATTEMPTS,
            identity["source"],
            identity["version"],
            code,
            time.monotonic() - started,
            last_error,
        )
        if attempt == CHART_PULL_ATTEMPTS or not transient_acquisition_failure(
            last_error
        ):
            break
        delay = retry_delay(attempt)
        if delay >= deadline - time.monotonic():
            last_error += "; insufficient retry time within the acquisition deadline"
            break
        LOG.warning("Retrying chart acquisition in %.2fs", delay)
        time.sleep(delay)
    raise AcquisitionError(
        f"could not acquire chart {identity['source']} version {identity['version']}: {last_error}"
    )


def ensure_chart(runner: Any, state_dir: Path, reference: str, version: str) -> Path:
    """Reuse a verified chart or retry acquisition before publishing it locally."""
    identity = _identity(reference, version)
    try:
        cache = _safe_cache_directory(state_dir)
        destination = cache / f"{identity['name']}-{version}.tgz"
        if _cached_chart(destination, identity):
            LOG.info("Using verified cached chart %s version %s", reference, version)
            return destination
        return _download_chart(runner, identity, destination)
    except OSError as error:
        raise AcquisitionError(f"chart acquisition local IO failed: {error}") from error
