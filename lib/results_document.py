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

"""Read filesystem results documents and their live sidecars.

A document (``results.json``) follows ``docs/MACHINE_READABLE_RESULTS.md``.
Any schema 1.x is accepted; another major version fails clearly. Sidecar
paths resolve relative to the document's directory and are streamed, so a
renderer never holds every client's series at once.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping

from lib.filesystem_report_options import LIVE_DETAIL_LEVELS

DOCUMENT_SCHEMA = "storage-scale-test/filesystem-batch-results"
SIDECAR_SCHEMA = "storage-scale-test/live-client-series"
SUPPORTED_MAJOR = 1


class ResultsDocumentError(ValueError):
    """A results document or sidecar cannot be used."""


@dataclass(frozen=True)
class LoadedDocument:
    """One parsed document, where it came from, and its directory."""

    data: dict
    path: Path | None
    base_dir: Path
    sha256: str | None = None


@dataclass(frozen=True)
class ClientSeries:
    """A sidecar's clients and a one-pass iterator of (elapsed_ms, values)."""

    clients: list[str]
    rows: Iterator[tuple[int, list]]


def _major(version: Any) -> int | None:
    if not isinstance(version, str):
        return None
    head = version.split(".", 1)[0]
    return int(head) if head.isdigit() else None


def check_schema(data: Any, schema: str, source: str) -> None:
    """Raise unless ``data`` names ``schema`` at a supported major version."""
    if not isinstance(data, dict) or data.get("schema") != schema:
        raise ResultsDocumentError(f"{source}: not a {schema} document")
    version = data.get("schema_version")
    if _major(version) != SUPPORTED_MAJOR:
        raise ResultsDocumentError(
            f"{source}: schema_version {version!r} is not supported "
            f"(this tool reads {SUPPORTED_MAJOR}.x)"
        )


def recorded_options(data: Mapping[str, Any]) -> dict:
    """The rendering options a document records (``generator.options``)."""
    return (data.get("generator") or {}).get("options") or {}


def effective_live_detail(data: Mapping[str, Any], requested: str | None) -> str:
    """The live detail a document renders at, in text and ``report.html``:
    the lower of its recorded level (unknown counts as full) and
    ``requested`` (None keeps the document's)."""
    known = [
        LIVE_DETAIL_LEVELS.index(level)
        for level in (recorded_options(data).get("live_detail"), requested)
        if level in LIVE_DETAIL_LEVELS
    ]
    return LIVE_DETAIL_LEVELS[min(known, default=len(LIVE_DETAIL_LEVELS) - 1)]


def document_from_data(data: dict, base_dir: str | Path) -> LoadedDocument:
    """Wrap an in-memory document whose sidecars live under ``base_dir``."""
    check_schema(data, DOCUMENT_SCHEMA, "results document")
    return LoadedDocument(data=data, path=None, base_dir=Path(base_dir))


def load_document(path: str | Path) -> LoadedDocument:
    """Load and check a results document; keep its directory for sidecars."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ResultsDocumentError(f"{path}: {error.strerror or error}") from error
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ResultsDocumentError(f"{path}: not valid JSON ({error})") from error
    check_schema(data, DOCUMENT_SCHEMA, str(path))
    return LoadedDocument(
        data=data,
        path=path,
        base_dir=path.resolve().parent,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def sidecar_path(loaded: LoadedDocument, ref: dict) -> Path:
    """Resolve a ``client_series`` reference; refuse paths leaving the base."""
    relative = ref.get("path") if isinstance(ref, dict) else None
    if not isinstance(relative, str) or not relative:
        raise ResultsDocumentError("client_series has no sidecar path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts:
        raise ResultsDocumentError(f"sidecar path must stay relative: {relative}")
    return loaded.base_dir.joinpath(*pure.parts)


def _sidecar_lines(path: Path) -> Iterator[dict]:
    """Parsed JSON lines; any read or decode failure is a ResultsDocumentError."""
    import zstandard  # pylint: disable=import-outside-toplevel

    try:
        with open(path, "rb") as compressed:
            reader = zstandard.ZstdDecompressor().stream_reader(compressed)
            with io.TextIOWrapper(reader, encoding="utf-8") as text:
                for line in text:
                    if line.strip():
                        yield json.loads(line)
    except (OSError, ValueError, zstandard.ZstdError) as error:
        raise ResultsDocumentError(f"{path}: unreadable sidecar ({error})") from error


def _rows(lines: Iterator[dict], path: Path, name: str) -> Iterator[tuple[int, list]]:
    for line in lines:
        try:
            yield int(line["elapsed_ms"]), list(line[name])
        except (KeyError, TypeError, ValueError) as error:
            raise ResultsDocumentError(f"{path}: malformed sidecar row") from error


def read_client_series(loaded: LoadedDocument, ref: dict) -> ClientSeries:
    """Open a ``full`` sidecar and stream its interval rows in source order."""
    return open_client_series(sidecar_path(loaded, ref))


def open_client_series(path: Path, values: str = "bytes_per_s") -> ClientSeries:
    """Stream a sidecar's (elapsed_ms, ``values`` list) rows in source order.

    ``values`` is ``bytes_per_s`` or ``done_bytes_delta`` (schema 1.1).
    """
    lines = _sidecar_lines(path)
    header = next(lines, None)
    try:
        check_schema(header, SIDECAR_SCHEMA, str(path))
    except ResultsDocumentError:
        lines.close()
        raise
    clients = header.get("clients")
    if not isinstance(clients, list):
        lines.close()
        raise ResultsDocumentError(f"{path}: sidecar header has no clients")
    if values == "done_bytes_delta" and header.get("schema_version") == "1.0":
        lines.close()
        raise ResultsDocumentError(
            f"{path}: schema 1.0 sidecars hold no DoneBytes; report the raw "
            "results again for client CSVs and PNGs"
        )
    return ClientSeries(
        clients=[str(c) for c in clients], rows=_rows(lines, path, values)
    )
