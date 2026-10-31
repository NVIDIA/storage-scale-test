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

"""Machine-readable filesystem results: helpers, writers, and assembly.

Engines (``extract-elbencho.py``, ``extract-mdtest-elbencho.py``) describe the
results they parsed as a *fragment*. This module merges fragments with the
committed batch manifest and execution ledger into one versioned document, and
writes documents and live-client sidecars deterministically and atomically.
See ``docs/MACHINE_READABLE_RESULTS.md``.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
from typing import Any, Callable, Mapping, Sequence

from lib.filesystem_coordinates import definition_coordinates
from lib.filesystem_report_options import validate_integer_filter
from lib.parse_only_sizes import parse_only_sizes_filter
from lib.reporting_common import natural_key

# Document and fragment keys used in several places.
K_EXECUTION_ID = "execution_id"
K_RESULT_STATE = "result_state"
K_LEDGER_STATE = "ledger_state"
K_PATH = "path"
K_RESULTS = "results"
K_EXECUTIONS = "executions"
K_RESULT_FILES = "result_files"
K_LOG = "log"
K_KIND = "kind"
K_DEFINITION = "definition_sha256"
K_GROUP_ID = "group_id"
K_DEFINITION_PATH = "definition_path"
K_LIVE_DOMAINS = "additional_live_domains"
K_UNPAIRED_LIVE = "unpaired_live"

SCHEMA = "storage-scale-test/filesystem-batch-results"
SCHEMA_VERSION = "1.1"
LIVE_SCHEMA = "storage-scale-test/live-client-series"
LIVE_SCHEMA_VERSION = "1.1"
FRAGMENT_VERSION = 1
LIVE_DETAIL_LEVELS = ("none", "summary", "selected", "full")
REPORT_HTML_FILENAME = "report.html"
TOOL_NAME = "storage-scale-test"
ZSTD_LEVEL = 3

LEDGER_STATES = (
    "success",
    "failed",
    "pending",
    "running",
    "cancelled",
    "interrupted",
    "unknown",
)
RESULT_STATES = (
    "complete",
    "partial",
    "missing",
    "unparseable",
    "not_run",
    "not_reported",
)
GENERATOR_OPTION_KEYS = (
    "live_detail",
    "shortfall_percent",
    "min_slow_windows",
    "client_max_timeseries_lines",
    "client_max_heatmap_rows",
    "no_dual_y_axis",
    "normalize_to",
    "only_nodes",
    "only_threads",
    "only_sizes",
    "only_iodepths",
    "groups",
    K_KIND,
)

PHASE_MISSING = "phase_missing"
PHASE_UNPARSEABLE = "phase_unparseable"
PERCENTILES_MISSING = "percentiles_missing"
RATES_MISSING = "rates_missing"
COMPLETION_INCOMPLETE = "completion_incomplete"
ITERATION_INCOMPLETE = "iteration_incomplete"
ITERATIONS_MISSING = "iterations_missing"
LIVE_CSV_UNREADABLE = "live_csv_unreadable"
EXECUTION_UNMATCHED = "execution_unmatched"
REPORT_FAILED = "report_failed"
NO_EXECUTION_LEDGER = "no_execution_ledger"
DEFINITION_UNREADABLE = "definition_unreadable"


_SUCCESS = "success"
_UNKNOWN = "unknown"
_NOT_REPORTED = "not_reported"
_ORDINARY_GROUP_ID = "0000"
_EXECUTIONS_DIR = K_EXECUTIONS
_DATESTAMP_SUFFIX = re.compile(r"([0-9]{8}Z[0-9]{6})$")
_MANIFEST_VERSION = 1
_CHUNK = 1 << 20


# -- numeric helpers ---------------------------------------------------------


def _finite(value: Any) -> bool:
    return value is not None and math.isfinite(value)


def mib_s_to_bytes_per_s(mib_s: float | None) -> int | None:
    """Convert elbencho's MiB/s to whole bytes per second."""
    return round(mib_s * 1048576) if _finite(mib_s) else None


def round_bytes_per_s(value: float | None) -> int | None:
    """Round a byte rate to a whole number."""
    return round(value) if _finite(value) else None


def round_ops(value: float | None) -> float | None:
    """Round an operation rate to 0.01 ops/s."""
    return round(float(value), 2) if _finite(value) else None


def round_latency_us(value: float | None) -> float | None:
    """Round a latency to 0.1 microseconds."""
    return round(float(value), 1) if _finite(value) else None


def seconds_to_us(seconds: float | None) -> float | None:
    """Convert seconds to microseconds rounded to 0.1."""
    return round(seconds * 1e6, 1) if _finite(seconds) else None


def histogram_columnar(
    hist: Mapping[float, int], scale_to_us: float = 1.0
) -> dict[str, list]:
    """Encode a histogram as ascending ``buckets_us`` and matching ``counts``."""
    items = sorted(
        (float(key) * scale_to_us, int(count)) for key, count in hist.items()
    )
    return {
        "buckets_us": [round(key, 1) for key, _ in items],
        "counts": [count for _, count in items],
    }


def command_argv(command: str) -> list[str]:
    """Split a recorded command line into argv, tolerating unbalanced quotes."""
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def problem(code: str, detail: str, phase: str | None = None) -> dict[str, Any]:
    """Build a ``problems`` entry; ``phase`` is always present."""
    return {"code": code, "detail": detail, "phase": phase}


def sha256_file(path: str | os.PathLike) -> tuple[str, int]:
    """Return the SHA-256 digest and size of a file, read in chunks."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def file_record(path_on_disk: str, relative_path: str) -> dict[str, Any]:
    """Describe a result file by relative path, digest, and size."""
    digest, size = sha256_file(path_on_disk)
    return {K_PATH: relative_path, "sha256": digest, "size_bytes": size}


# -- writers ------------------------------------------------------------------


def serialize(document: Any) -> str:
    """Deterministic compact JSON text with a trailing newline."""
    return (
        json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        + "\n"
    )


def _zstd():
    # Deferred: only document and sidecar writers need the dependency.
    import zstandard  # pylint: disable=import-outside-toplevel

    return zstandard


def _umask() -> int:
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("Umask:"):
                    return int(line.split()[1], 8)
    except (OSError, ValueError, IndexError):
        pass
    # Elsewhere the umask can only be read by setting it; restore it at once.
    mask = os.umask(0o022)
    os.umask(mask)
    return mask


def set_published_mode(descriptor: int) -> None:
    """Give a ``mkstemp`` file (0600) the mode a plain ``open`` would."""
    os.fchmod(descriptor, 0o666 & ~_umask())


def _atomic_write(path: str | os.PathLike, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".results-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            set_published_mode(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_document(document: dict, path: str | os.PathLike) -> None:
    """Write a document atomically as plain JSON."""
    _atomic_write(path, serialize(document).encode("utf-8"))


def write_fragment(fragment: dict, path: str | os.PathLike) -> None:
    """Write an engine fragment atomically as plain JSON."""
    _atomic_write(path, serialize(fragment).encode("utf-8"))


def clear_live_sidecars(live_dir: Path) -> None:
    """Remove sidecars of an earlier document; this run rewrites the document."""
    if live_dir.is_dir():
        for stale in live_dir.glob("*.jsonl.zst"):
            stale.unlink(missing_ok=True)


def load_comparisons(path: str | os.PathLike | None) -> list[dict] | None:
    """The validated comparison file at ``path``; ValueError when invalid."""
    if not path:
        return None
    # Deferred: only HTML reports need the renderer (and PyYAML).
    from lib.report_html import (  # pylint: disable=import-outside-toplevel
        load_comparison_file,
    )

    return load_comparison_file(path)


def _integers(flag: str, value: str | None) -> frozenset[int] | None:
    return frozenset(validate_integer_filter(flag, value) or ()) or None


@dataclass(frozen=True)
class ReportSelection:  # pylint: disable=too-many-instance-attributes
    """What a report shows: ``--kind``, ``--groups``, ``--only-*``,
    ``--live-detail`` and ``--normalize-to``, the same for text and
    ``report.html``.

    ``None`` selects everything (and keeps each document's live detail and
    normalization).
    Sizes and IO depths apply to IO executions; mdtest threads are tasks.
    """

    kinds: frozenset[str] | None = None
    groups: frozenset[str] | None = None
    nodes: frozenset[int] | None = None
    threads: frozenset[int] | None = None
    sizes: frozenset[str] | None = None
    iodepths: frozenset[int] | None = None
    live_detail: str | None = None
    normalize_to: int | None = None

    @classmethod
    def from_args(
        cls, args: Any, kind: str = "all", groups: str | None = None
    ) -> "ReportSelection":
        """The selection of parsed report arguments; ValueError when invalid."""
        sizes = parse_only_sizes_filter(getattr(args, "only_sizes", None))
        return cls(
            kinds=None if kind == "all" else frozenset([kind]),
            groups=frozenset(groups.split(",")) if groups else None,
            nodes=_integers("--only-nodes", getattr(args, "only_nodes", None)),
            threads=_integers("--only-threads", getattr(args, "only_threads", None)),
            sizes=frozenset(sizes) if sizes else None,
            iodepths=_integers("--only-iodepths", getattr(args, "only_iodepths", None)),
            live_detail=getattr(args, "live_detail", None),
            normalize_to=getattr(args, "normalize_to", None),
        )

    def _coordinates(self, kind: str) -> tuple[tuple[Any, str], ...]:
        if kind == "mdtest":
            return ((self.nodes, "nodes"), (self.threads, "tasks_per_node"))
        return (
            (self.nodes, "nodes"),
            (self.threads, "threads_per_node"),
            (self.sizes, "io_size_spec"),
            (self.iodepths, "io_depth"),
        )

    def keeps(self, group: Mapping[str, Any], execution: Mapping[str, Any]) -> bool:
        """True when ``execution`` of ``group`` is selected."""
        if self.kinds and group[K_KIND] not in self.kinds:
            return False
        if self.groups is not None and group[K_GROUP_ID] not in self.groups:
            return False
        coordinates = execution.get("coordinates") or {}
        return all(
            not allowed or coordinates.get(key) in allowed
            for allowed, key in self._coordinates(group[K_KIND])
        )

    def keep_execution(self) -> Callable[[dict, dict], bool] | None:
        """``keeps``, or None when every execution is selected."""
        filters = (self.kinds, self.nodes, self.threads, self.sizes, self.iodepths)
        if self.groups is None and not any(filters):
            return None
        return self.keeps


def publish_document(
    document: dict,
    path: str | os.PathLike,
    comparisons: list[dict] | None = None,
    selection: ReportSelection | None = None,
) -> Path:
    """Write the document, then ``report.html`` beside it; return the HTML path.

    Both are written to a temporary name and renamed.
    """
    # Deferred: only publishing needs the loader.
    from lib.results_document import (  # pylint: disable=import-outside-toplevel
        load_document,
    )

    write_document(document, path)
    print(f"Results document saved as {path}", file=sys.stderr)
    for item in document.get("diagnostics", []):
        if item.get("code") == EXECUTION_UNMATCHED:
            print(f"Warning: not reported: {item['detail']}", file=sys.stderr)
    try:
        return publish_report_html(
            [load_document(path)], Path(path).parent, comparisons, selection
        )
    except ValueError:
        # A comparison naming an unknown section shows only once the results
        # are parsed; an earlier page must not outlive the new document.
        (Path(path).parent / REPORT_HTML_FILENAME).unlink(missing_ok=True)
        raise


def publish_report_html(
    documents: Sequence[Any],
    directory: str | os.PathLike,
    comparisons: list[dict] | None = None,
    selection: ReportSelection | None = None,
) -> Path:
    """Write ``report.html`` for loaded documents into ``directory``,
    showing what ``selection`` selects.

    An invalid comparison (``ValueError``) is raised before anything is written.
    """
    # Deferred: only publishing needs the renderer.
    from lib.report_html import (  # pylint: disable=import-outside-toplevel
        render_report,
        write_report,
    )
    from lib.results_document import (  # pylint: disable=import-outside-toplevel
        effective_live_detail,
    )

    selection = selection or ReportSelection()
    html = Path(directory) / REPORT_HTML_FILENAME
    page = render_report(
        list(documents),
        comparisons,
        keep_execution=selection.keep_execution(),
        live_detail=selection.live_detail,
        normalize_to=selection.normalize_to,
    )
    write_report(html, page)
    for loaded in documents:
        live_detail = effective_live_detail(loaded.data, selection.live_detail)
        if live_detail != LIVE_DETAIL_LEVELS[-1]:
            print(
                f"Live detail is {live_detail}, not full: report.html omits "
                "per-client charts.",
                file=sys.stderr,
            )
    print(f"HTML report saved as {html}", file=sys.stderr)
    return html


def read_fragment(path: str | os.PathLike) -> dict:
    """Read and sanity-check an engine fragment."""
    with open(path, "r", encoding="utf-8") as stream:
        fragment = json.load(stream)
    if (
        not isinstance(fragment, dict)
        or fragment.get("fragment_version") != FRAGMENT_VERSION
        or not isinstance(fragment.get(K_EXECUTIONS), list)
    ):
        raise ValueError(f"Unsupported results fragment: {path}")
    fragment.setdefault("diagnostics", [])
    return fragment


class _HashingFile:
    """File wrapper that counts and hashes the bytes actually stored."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data) -> int:
        """Store and account for ``data``."""
        self.digest.update(data)
        self.size += len(data)
        return self._stream.write(data)

    def flush(self) -> None:
        """Flush the underlying file."""
        self._stream.flush()


class SidecarWriter:
    """Streaming, atomic, zstd-compressed JSON Lines writer for live series."""

    def __init__(self, path: str | os.PathLike, header: dict) -> None:
        self.path = Path(path)
        self.size_bytes: int | None = None
        self.sha256: str | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, self._temporary = tempfile.mkstemp(
            prefix=".sidecar-", dir=self.path.parent
        )
        self._file = os.fdopen(descriptor, "wb")
        self._hashing = _HashingFile(self._file)
        self._writer = (
            _zstd()
            .ZstdCompressor(level=ZSTD_LEVEL)
            .stream_writer(self._hashing, closefd=False)
        )
        self._closed = False
        self._write_line(header)

    def _write_line(self, document: dict) -> None:
        self._writer.write(serialize(document).encode("utf-8"))

    def write_interval(
        self,
        elapsed_ms: int,
        bytes_per_s: list[int | None],
        done_bytes_delta: list[int | None] | None = None,
    ) -> None:
        """Append one interval line."""
        line: dict[str, Any] = {
            "elapsed_ms": elapsed_ms,
            "bytes_per_s": list(bytes_per_s),
        }
        if done_bytes_delta is not None:
            line["done_bytes_delta"] = list(done_bytes_delta)
        self._write_line(line)

    def close(self) -> None:
        """Finish the zstd frame and atomically publish the file."""
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.close()
            self._file.flush()
            os.fsync(self._file.fileno())
            set_published_mode(self._file.fileno())
            self._file.close()
            os.replace(self._temporary, self.path)
            self.size_bytes = self._hashing.size
            self.sha256 = self._hashing.digest.hexdigest()
        finally:
            self._discard()

    def abort(self) -> None:
        """Drop the partial file."""
        self._closed = True
        self._discard()

    def _discard(self) -> None:
        if not self._file.closed:
            self._file.close()
        Path(self._temporary).unlink(missing_ok=True)

    def __enter__(self) -> "SidecarWriter":
        return self

    def __exit__(self, exc_type, _exc, _traceback) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()


# -- generator ----------------------------------------------------------------


def generator_block(command: list[str], options: Mapping[str, Any]) -> dict:
    """Describe the tool run; missing option keys are recorded as null."""
    from lib.project_version import (  # pylint: disable=import-outside-toplevel
        project_version,
    )

    return {
        "tool": TOOL_NAME,
        "version": project_version(),
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "command": list(command),
        "options": {key: options.get(key) for key in GENERATOR_OPTION_KEYS},
    }


# -- assembly -----------------------------------------------------------------


class _GroupSpec:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    """Everything assembly needs to know about one group."""

    def __init__(self, **fields: Any) -> None:
        self.group_id: str = fields[K_GROUP_ID]
        self.kind: str = fields[K_KIND]
        self.output_relative: str = fields["output_relative"]
        self.source: str | None = fields.get("source")
        self.prefix: str = fields.get("prefix", "")
        self.settings: dict = fields.get("settings") or {}
        self.settings_text: str | None = fields.get("settings_text")
        self.settings_sha256: dict = fields["settings_sha256"]
        self.ledger: list[dict] = fields["ledger"]
        self.fragment: dict | None = fields.get("fragment")
        self.selected: bool = fields.get("selected", True)
        self.report_failed: bool = fields.get("report_failed", False)
        self.has_ledger: bool = fields.get("has_ledger", True)


def _target_block(settings: Mapping) -> dict:
    from lib.storage_target import (  # pylint: disable=import-outside-toplevel
        storage_target_from_env,
    )

    target = storage_target_from_env(settings)
    return {"name": target.name, "recorded": target.recorded}


def _unpaired_live(spec: _GroupSpec, live_dir: Path | None) -> list[dict]:
    """Live objects of the group's live CSVs that have no result pair."""
    objects = copy.deepcopy((spec.fragment or {}).get(K_UNPAIRED_LIVE) or [])
    return [_rewrite_live(live, spec.prefix, live_dir) for live in objects]


def _group_record(spec: _GroupSpec, live_dir: Path | None = None) -> dict:
    return {
        K_GROUP_ID: spec.group_id,
        K_KIND: spec.kind,
        "label": None,
        "output_relative": spec.output_relative,
        "source": spec.source,
        "target": _target_block(spec.settings),
        "settings_sha256": spec.settings_sha256,
        "env_override": spec.settings.get("env_override"),
        "settings": spec.settings,
        "settings_text": spec.settings_text,
        "execution_ids": [item[K_EXECUTION_ID] for item in spec.ledger],
        K_UNPAIRED_LIVE: _unpaired_live(spec, live_dir),
    }


def _posix_join(prefix: str, path: str) -> str:
    return f"{prefix}/{path}" if prefix and prefix != "." else path


def _rewrite_live(live: Any, prefix: str, live_dir: Path | None) -> Any:
    if not isinstance(live, dict):
        return live
    # Replace rather than edit nested dicts: a live CSV's domains may share
    # one source dict, which must not be prefixed once per domain.
    source = live.get("source")
    if isinstance(source, dict) and isinstance(source.get(K_PATH), str):
        live["source"] = {**source, K_PATH: _posix_join(prefix, source[K_PATH])}
    series = live.get("client_series")
    if isinstance(series, dict) and "sha256" in series and live_dir is not None:
        # Sidecars sit beside the document; paths resolve from its directory.
        path = f"{live_dir.name}/{series[K_PATH]}"
        live["client_series"] = {**series, K_PATH: path}
    return live


def _rewrite_results(results: Any, prefix: str, live_dir: Path | None) -> Any:
    if not isinstance(results, dict):
        return results
    results = copy.deepcopy(results)
    for phase in results.get("phases") or []:
        if "live" in phase:
            phase["live"] = _rewrite_live(phase["live"], prefix, live_dir)
    for live in results.get(K_LIVE_DOMAINS) or []:
        _rewrite_live(live, prefix, live_dir)
    return results


def _rewrite_files(files: list[dict], prefix: str) -> list[dict]:
    return [{**item, K_PATH: _posix_join(prefix, item[K_PATH])} for item in files]


def _execution_record(
    spec: _GroupSpec, ledger: dict, fragment: Mapping[str, Any]
) -> dict:
    """Build one document execution, in schema key order."""
    return {
        K_EXECUTION_ID: ledger[K_EXECUTION_ID],
        K_GROUP_ID: spec.group_id,
        K_KIND: spec.kind,
        K_DEFINITION: ledger[K_DEFINITION],
        K_LEDGER_STATE: ledger[K_LEDGER_STATE],
        K_RESULT_STATE: fragment[K_RESULT_STATE],
        "problems": list(fragment.get("problems") or []),
        "coordinates": fragment.get("coordinates"),
        "coordinate_key": fragment.get("coordinate_key"),
        "dataset": fragment.get("dataset"),
        "producer_version": fragment.get("producer_version"),
        K_RESULT_FILES: fragment.get(K_RESULT_FILES) or [],
        K_LOG: ledger[K_LOG],
        K_RESULTS: fragment.get(K_RESULTS),
    }


def _absent_state(spec: _GroupSpec, ledger: dict) -> dict:
    """Result state and problems for a committed execution with no fragment entry."""
    if not spec.selected:
        return {K_RESULT_STATE: _NOT_REPORTED}
    if ledger[K_LEDGER_STATE] != _SUCCESS:
        return {K_RESULT_STATE: "not_run"}
    if spec.report_failed:
        return {
            K_RESULT_STATE: "missing",
            "problems": [problem(REPORT_FAILED, "the group report failed")],
        }
    return {K_RESULT_STATE: "missing"}


def _definition_fields(spec: _GroupSpec, ledger: dict) -> dict:
    """Coordinates and key from the execution definition (the dataset needs results)."""
    path = ledger.get(K_DEFINITION_PATH)
    derived = None
    try:
        if path is not None:
            derived = definition_coordinates(
                spec.kind, Path(path).read_text(encoding="utf-8"), spec.settings
            )
    except (OSError, UnicodeDecodeError, ValueError):
        derived = None
    if derived is None:
        detail = f"execution {ledger[K_EXECUTION_ID]}: definition cannot be parsed"
        return {"problems": [problem(DEFINITION_UNREADABLE, detail)]}
    return {"coordinates": derived[0], "coordinate_key": derived[1]}


def _absent_fragment(spec: _GroupSpec, ledger: dict) -> dict:
    """Fragment-shaped entry for a committed execution with no fragment entry."""
    absent = _absent_state(spec, ledger)
    fields = _definition_fields(spec, ledger)
    absent["problems"] = [*absent.get("problems", []), *fields.pop("problems", [])]
    absent.update(fields)
    return absent


def _merge_execution(
    spec: _GroupSpec,
    ledger: dict,
    frag: Mapping[str, Any] | None,
    live_dir: Path | None,
) -> dict:
    if frag is None:
        return _execution_record(spec, ledger, _absent_fragment(spec, ledger))
    merged = dict(frag)
    merged[K_RESULT_FILES] = _rewrite_files(frag.get(K_RESULT_FILES) or [], spec.prefix)
    merged[K_RESULTS] = _rewrite_results(frag.get(K_RESULTS), spec.prefix, live_dir)
    # Fragments carry only SUCCESS executions in batches; an ordinary directory
    # whose ledger state is merely unknown keeps the results it parsed.
    if ledger[K_LEDGER_STATE] not in (_SUCCESS, _UNKNOWN):
        merged[K_RESULT_STATE] = "not_run"
        merged[K_RESULTS] = None
    return _execution_record(spec, ledger, merged)


def _diagnostic(spec: _GroupSpec, item: Mapping[str, Any]) -> dict:
    return problem(
        item["code"],
        f"group {spec.group_id}: {item['detail']}",
        item.get("phase"),
    )


def _unmatched_diagnostic(spec: _GroupSpec, entry: Mapping[str, Any]) -> dict:
    paths = ", ".join(
        _posix_join(spec.prefix, item[K_PATH]) for item in entry.get(K_RESULT_FILES, [])
    )
    return problem(
        EXECUTION_UNMATCHED,
        f"group {spec.group_id}: result files match no ledger execution: {paths}",
    )


def _merge_group(
    spec: _GroupSpec, live_dir: Path | None
) -> tuple[list[dict], list[dict]]:
    """Merge one group's ledger and fragment into executions and diagnostics."""
    fragment = spec.fragment
    entries = fragment[K_EXECUTIONS] if fragment else []
    by_id = {e[K_EXECUTION_ID]: e for e in entries if e.get(K_EXECUTION_ID)}
    unmatched = [e for e in entries if not e.get(K_EXECUTION_ID)]
    executions = [
        _merge_execution(spec, item, by_id.get(item[K_EXECUTION_ID]), live_dir)
        for item in spec.ledger
    ]
    diagnostics = [
        _diagnostic(spec, d) for d in (fragment or {}).get("diagnostics", [])
    ]
    if not spec.has_ledger:
        diagnostics.append(
            problem(
                NO_EXECUTION_LEDGER,
                f"group {spec.group_id}: no executions/ ledger; ledger state unknown",
            )
        )
        for entry in unmatched:
            ledger = {
                K_EXECUTION_ID: None,
                K_DEFINITION: None,
                K_LEDGER_STATE: _UNKNOWN,
                K_LOG: None,
            }
            executions.append(_merge_execution(spec, ledger, entry, live_dir))
    else:
        diagnostics.extend(_unmatched_diagnostic(spec, e) for e in unmatched)
    return executions, diagnostics


def _execution_sort_key(execution: Mapping[str, Any]) -> tuple:
    identifier = execution[K_EXECUTION_ID]
    return (
        execution[K_GROUP_ID],
        identifier is None,
        identifier or "",
        natural_key(execution["coordinate_key"] or ""),
    )


def _batch_outcome(executions: list[dict], specs: list[_GroupSpec]) -> dict[str, Any]:
    ledger = Counter(item[K_LEDGER_STATE] for item in executions)
    results = Counter(item[K_RESULT_STATE] for item in executions)
    reported = [s.group_id for s in specs if s.fragment is not None]
    complete = bool(executions) and all(
        item[K_LEDGER_STATE] == _SUCCESS and item[K_RESULT_STATE] == "complete"
        for item in executions
    )
    return {
        "complete": complete,
        K_EXECUTIONS: {
            "total": len(executions),
            **{state: ledger[state] for state in LEDGER_STATES},
        },
        K_RESULTS: {state: results[state] for state in RESULT_STATES},
        "groups_reported": sorted(reported),
        "groups_not_reported": sorted(s.group_id for s in specs if s.fragment is None),
    }


def _compose(
    batch: dict,
    specs: list[_GroupSpec],
    generator: dict,
    live_dir: Path | None,
) -> dict:
    executions: list[dict] = []
    diagnostics: list[dict] = []
    for spec in specs:
        merged, notes = _merge_group(spec, live_dir)
        executions.extend(merged)
        diagnostics.extend(notes)
    executions.sort(key=_execution_sort_key)
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "generator": generator,
        "batch": batch,
        "batch_outcome": _batch_outcome(executions, specs),
        "groups": [_group_record(s, live_dir) for s in specs],
        K_EXECUTIONS: executions,
        "diagnostics": diagnostics,
    }


def document_is_complete(doc: Mapping[str, Any]) -> bool:
    """True when every committed execution succeeded with complete results."""
    return bool(doc.get("batch_outcome", {}).get("complete"))


def _load_settings(directory: Path) -> dict:
    from lib.env_used_yaml import (  # pylint: disable=import-outside-toplevel
        load_env_used_yaml,
    )

    settings = load_env_used_yaml(str(directory))
    return settings if isinstance(settings, dict) else {}


def _settings_text(directory: Path) -> str | None:
    """The verbatim env_used.yaml text, or None when it cannot be read."""
    try:
        return (directory / "env_used.yaml").read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _optional_sha(path: Path) -> str | None:
    return sha256_file(path)[0] if path.is_file() else None


def _log_relative(root: Path, relative: str, identifier: str) -> str | None:
    for candidate in (
        f"{_EXECUTIONS_DIR}/{identifier}.log",
        _posix_join(relative, f"{_EXECUTIONS_DIR}/{identifier}.log"),
    ):
        if (root / candidate).is_file():
            return candidate
    return None


def _batch_specs(
    manifest: Any,
    selected_ids: set[str],
    fragments: Mapping[str, dict | None],
    report_failed: set[str],
) -> list[_GroupSpec]:
    from lib.filesystem_batch import (  # pylint: disable=import-outside-toplevel
        execution_status,
    )

    specs = []
    for group in manifest.groups:
        ledger = [
            {
                K_EXECUTION_ID: item.execution_id,
                K_DEFINITION: item.definition_digest,
                K_LEDGER_STATE: execution_status(manifest, item).lower(),
                K_LOG: _log_relative(
                    manifest.root, group.output_relative, item.execution_id
                ),
                K_DEFINITION_PATH: manifest.root
                / _EXECUTIONS_DIR
                / f"{item.execution_id}.sh",
            }
            for item in manifest.group_executions(group.group_id)
        ]
        specs.append(
            _GroupSpec(
                group_id=group.group_id,
                kind=group.kind,
                output_relative=group.output_relative,
                prefix=group.output_relative,
                source=str(manifest.group_path(group)),
                settings=_load_settings(manifest.group_path(group)),
                settings_text=_settings_text(manifest.group_path(group)),
                settings_sha256={
                    "env_used_sh": group.shell_digest,
                    "env_used_yaml": group.yaml_digest,
                },
                ledger=ledger,
                fragment=fragments.get(group.group_id),
                selected=group.group_id in selected_ids,
                report_failed=group.group_id in report_failed,
            )
        )
    return specs


def assemble_batch_document(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    manifest: Any,
    selected_groups: Any,
    fragments: Mapping[str, dict | None],
    report_failed: set[str],
    live_dir: Path | None,
    generator: dict,
) -> dict:
    """Merge a prepared batch's manifest, ledger, settings, and group fragments."""
    selected_ids = {group.group_id for group in selected_groups}
    specs = _batch_specs(manifest, selected_ids, fragments, report_failed)
    root = Path(manifest.root)
    substrate = specs[0].settings.get("EXECUTION_SUBSTRATE") if specs else None
    batch = {
        "root": str(root),
        "datestamp": manifest.datestamp,
        "manifest_version": _MANIFEST_VERSION,
        "manifest_sha256": manifest.digest,
        "sealed": (root / "batch-sealed.sha256").is_file(),
        "substrate": None if substrate is None else str(substrate),
    }
    return _compose(batch, specs, generator, live_dir)


def _ledger_definitions(result_dir: str | os.PathLike) -> set[str]:
    return {
        path.stem
        for path in (Path(result_dir) / _EXECUTIONS_DIR).glob("[0-9]*.sh")
        if path.stem.isdigit()
    }


def has_execution_ledger(result_dir: str | os.PathLike) -> bool:
    """True when executions/ holds a definition; an empty one is no ledger."""
    return bool(_ledger_definitions(result_dir))


def _ordinary_ledger(
    result_dir: Path, relative: str, fragment: Mapping[str, Any]
) -> list[dict]:
    # The ledger is every executions/NNNN.sh, not just those the engine could
    # parse: an unreadable definition must still count against completeness.
    defined = _ledger_definitions(result_dir)
    parsed = {
        e[K_EXECUTION_ID] for e in fragment[K_EXECUTIONS] if e.get(K_EXECUTION_ID)
    }
    ledger = []
    for identifier in sorted(defined | parsed):
        status = result_dir / _EXECUTIONS_DIR / f"{identifier}.status"
        state = _UNKNOWN
        if status.is_file():
            value = status.read_text(encoding="utf-8").strip().lower()
            state = value if value in LEDGER_STATES[:-1] else _UNKNOWN
        log = f"{_EXECUTIONS_DIR}/{identifier}.log"
        ledger.append(
            {
                K_EXECUTION_ID: identifier,
                K_DEFINITION: _optional_sha(
                    result_dir / _EXECUTIONS_DIR / f"{identifier}.sh"
                ),
                K_LEDGER_STATE: state,
                K_LOG: (
                    _posix_join(relative, log) if (result_dir / log).is_file() else None
                ),
                K_DEFINITION_PATH: result_dir / _EXECUTIONS_DIR / f"{identifier}.sh",
            }
        )
    return ledger


def _ordinary_spec(
    result_dir: Path, fragment: dict, group_id: str, relative: str
) -> _GroupSpec:
    """One ordinary result directory as a group whose paths start at ``relative``."""
    has_ledger = has_execution_ledger(result_dir)
    return _GroupSpec(
        group_id=group_id,
        kind=fragment[K_KIND],
        output_relative=relative,
        prefix=relative,
        source=str(result_dir),
        settings=_load_settings(result_dir),
        settings_text=_settings_text(result_dir),
        settings_sha256={
            "env_used_sh": _optional_sha(result_dir / "env_used.sh"),
            "env_used_yaml": _optional_sha(result_dir / "env_used.yaml"),
        },
        ledger=_ordinary_ledger(result_dir, relative, fragment) if has_ledger else [],
        fragment=fragment,
        has_ledger=has_ledger,
    )


def _shared(values: list[Any]) -> Any:
    """The value every item has, or None when they differ."""
    return values[0] if values and all(v == values[0] for v in values) else None


def assemble_inputs_document(
    inputs: Sequence[tuple[str | os.PathLike, dict]],
    live_dir: Path | None,
    generator: dict,
) -> dict:
    """Build one document with a group per ordinary result directory.

    One input is group 0000 rooted at itself. Several are groups 0001, 0002,
    ... in input order, rooted at their common parent directory.
    """
    dirs = [Path(directory).resolve() for directory, _ in inputs]
    if len(dirs) == 1:
        root, ids = dirs[0], [_ORDINARY_GROUP_ID]
    else:
        root = Path(os.path.commonpath(dirs))
        ids = [f"{number:04d}" for number in range(1, len(dirs) + 1)]
    specs = [
        _ordinary_spec(
            directory,
            fragment,
            group_id,
            Path(os.path.relpath(directory, root)).as_posix(),
        )
        for directory, (_, fragment), group_id in zip(dirs, inputs, ids)
    ]
    datestamps = [_DATESTAMP_SUFFIX.search(directory.name) for directory in dirs]
    substrate = _shared([spec.settings.get("EXECUTION_SUBSTRATE") for spec in specs])
    batch = {
        "root": str(root),
        "datestamp": _shared([m.group(1) if m else None for m in datestamps]),
        "manifest_version": None,
        "manifest_sha256": None,
        "sealed": None,
        "substrate": None if substrate is None else str(substrate),
    }
    return _compose(batch, specs, generator, live_dir)


def load_from_json_inputs(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> tuple[list[Any], list[dict] | None, frozenset[str] | None]:
    """The ``--from-json`` documents, the parsed ``--comparisons`` file and
    the ``--groups`` selection (each ID must exist in some document).

    Any problem, including an option the documents fix
    (``check_replay_options``), is a usage error, before anything is written.
    """
    # Deferred: only rendering from documents needs these.
    from lib.filesystem_report_options import (  # pylint: disable=import-outside-toplevel
        check_replay_options,
        validate_from_json,
    )
    from lib.results_document import (  # pylint: disable=import-outside-toplevel
        load_document,
        recorded_options,
    )

    try:
        validate_from_json(args.from_json, args.input_dirs, args.output_dir)
        documents = [load_document(path) for path in args.from_json]
        check_replay_options(
            args, [recorded_options(loaded.data) for loaded in documents]
        )
        comparisons = load_comparisons(args.comparisons)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if not args.groups:
        return documents, comparisons, None
    selected = frozenset(args.groups.split(","))
    known = {group["group_id"] for doc in documents for group in doc.data["groups"]}
    if selected - known:
        parser.error(
            f"unknown groups in {' '.join(args.from_json)}: "
            f"{','.join(sorted(selected - known))}"
        )
    return documents, comparisons, selected
