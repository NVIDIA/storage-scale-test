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

"""Validated prepared-batch membership and isolated filesystem reporting."""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import Executor
import contextlib
from dataclasses import dataclass, field
from functools import partial
import hashlib
import io
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import traceback
from typing import Any, Callable

from lib.filesystem_report_options import (
    BATCH_CONSUMED_FLAGS,
    BATCH_GROUP_FLAG,
    COMPARISONS_FLAG,
    FROM_JSON_FLAG,
    LIVE_DETAIL_FLAG,
    REPORT_OPTIONS,
    RESULTS_FRAGMENT_FLAG,
    RESULTS_LIVE_DIR_FLAG,
    document_output_dir,
    options_by_kind,
)
from lib.filesystem_snapshot_summary import format_io_snapshot_summary
from lib.report_pool import load_engine, report_pool, report_workers, run_command
from lib.report_provenance import VERSION_SUFFIX
from lib.stdout_report_file import REPORT_TXT_FILENAME
from lib.storage_target import (
    UNKNOWN_TARGET,
    StorageTarget,
    storage_target_from_env,
)

MANIFEST_FILENAME = "batch-manifest.tsv"
SEAL_FILENAME = "batch-sealed.sha256"
BATCH_MARKERS = (
    MANIFEST_FILENAME,
    "batch-profile.tsv",
    SEAL_FILENAME,
    "batch-sealed-environment.sha256",
)
ENV_SHELL = "env_used.sh"
ENV_YAML = "env_used.yaml"
SUCCESS = "SUCCESS"
REPORT_IDENTITY_FILENAME = ".report-identity"
COMBINED_REPORT_FILENAME = "reports.txt"
RESULTS_FILENAME = "results.json"
RESULTS_FRAGMENT_FILENAME = "results.fragment.json"
RESULTS_LIVE_DIRNAME = "results.live"
GROUPS_DIRNAME = "groups"
EXIT_INCOMPLETE = 3
# A group outcome for a report this run generated.
REPORTED = "Reported"
NO_MATCHING_RESULTS_TEXT = "No results matched the report filters.\n"
KINDS = ("all", "io", "mdtest")
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_DATESTAMP_RE = re.compile(r"[0-9]{8}Z[0-9]{6}")


@dataclass(frozen=True)
class BatchGroup:
    """One immutable workload snapshot and its result destination."""

    group_id: str
    kind: str
    output_relative: str
    shell_digest: str
    yaml_digest: str


@dataclass(frozen=True)
class BatchExecution:
    """One execution in global insertion order."""

    execution_id: str
    group_id: str
    definition_digest: str


@dataclass(frozen=True)
class BatchManifest:
    """Validated committed membership; mutable statuses are deliberately absent."""

    root: Path
    revision: int
    datestamp: str
    groups: tuple[BatchGroup, ...]
    executions: tuple[BatchExecution, ...]
    digest: str

    def group_path(self, group: BatchGroup) -> Path:
        """Resolve a group's contained output path."""
        return _contained_path(self.root, group.output_relative)

    def group_executions(self, group_id: str) -> tuple[BatchExecution, ...]:
        """Return committed executions for one group in global order."""
        return tuple(item for item in self.executions if item.group_id == group_id)


def is_batch_directory(path: str | Path) -> bool:
    """Recognize batch roots without downgrading damaged manifests to legacy input."""
    root = Path(path)
    return any(
        (root / marker).exists() or (root / marker).is_symlink()
        for marker in BATCH_MARKERS
    )


def reject_batch_descendant_inputs(paths: list[str]) -> None:
    """Prevent raw group inputs from bypassing the authoritative root ledger."""
    for path in paths:
        candidate = Path(path).resolve()
        for ancestor in candidate.parents:
            if not is_batch_directory(ancestor):
                continue
            relative = candidate.relative_to(ancestor)
            group_option = ""
            if len(relative.parts) >= 2 and relative.parts[0] == GROUPS_DIRNAME:
                identifier = relative.parts[1]
                if re.fullmatch(r"[0-9]{4}", identifier):
                    group_option = f" --groups {identifier}"
            raise ValueError(
                f"Batch descendants cannot be raw reporting inputs: {path}. "
                f"Report the batch root {ancestor}{group_option} to use its "
                "authoritative execution ledger."
            )


def _contained_path(root: Path, relative: str) -> Path:
    parts = Path(relative).parts
    if (
        not parts
        or Path(relative).is_absolute()
        or any(p in (".", "..") for p in parts)
    ):
        raise ValueError(f"Invalid batch-relative path: {relative}")
    candidate = root
    for part in parts:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError(f"Symlink in batch-owned path: {candidate}")
    if not candidate.resolve().is_relative_to(root):
        raise ValueError(f"Path escapes batch root: {relative}")
    return candidate


def _read_regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected nonsymlink batch file: {path}")
    return path.read_bytes()


def _verify_digest(path: Path, expected: str) -> bytes:
    if not _DIGEST_RE.fullmatch(expected):
        raise ValueError(f"Invalid SHA256 digest for {path}")
    data = _read_regular(path)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"Batch digest mismatch: {path}")
    return data


def _parse_records(
    data: bytes,
) -> tuple[dict[str, str], list[BatchGroup], list[BatchExecution]]:
    headers: dict[str, str] = {}
    groups = []
    executions = []
    for line in data.decode("utf-8").splitlines():
        fields = line.split("\t")
        tag = fields[0]
        if tag in ("version", "revision", "datestamp") and len(fields) == 2:
            if tag in headers:
                raise ValueError(f"Duplicate batch header: {tag}")
            headers[tag] = fields[1]
        elif tag == "group" and len(fields) == 6:
            groups.append(BatchGroup(*fields[1:]))
        elif tag == "execution" and len(fields) == 4:
            executions.append(BatchExecution(*fields[1:]))
        else:
            raise ValueError(f"Invalid batch manifest record: {line}")
    return headers, groups, executions


def _validate_headers(headers: dict[str, str], group_count: int) -> tuple[int, str]:
    if (
        set(headers) != {"version", "revision", "datestamp"}
        or headers.get("version") != "1"
    ):
        raise ValueError("Unsupported or incomplete batch manifest headers")
    revision = headers["revision"]
    if not re.fullmatch(r"[1-9][0-9]*", revision) or int(revision) != group_count:
        raise ValueError("Batch revision must equal the committed group count")
    datestamp = headers["datestamp"]
    if not _DATESTAMP_RE.fullmatch(datestamp):
        raise ValueError("Invalid batch datestamp")
    return int(revision), datestamp


def _export_value(content: str, name: str) -> str:
    values = re.findall(rf"^export {re.escape(name)}=(.*)$", content, re.MULTILINE)
    if not values:
        raise ValueError(f"Missing execution field: {name}")
    parsed = shlex.split(values[-1])
    if len(parsed) != 1:
        raise ValueError(f"Invalid execution field: {name}")
    return parsed[0]


def _validate_groups(root: Path, groups: list[BatchGroup], datestamp: str) -> None:
    for sequence, group in enumerate(groups, 1):
        if group.group_id != f"{sequence:04d}" or group.kind not in KINDS[1:]:
            raise ValueError("Invalid or unordered batch group membership")
        basename = "elbencho" if group.kind == "io" else "mdtest-elbencho"
        expected = f"groups/{group.group_id}/{basename}-{datestamp}"
        if group.output_relative != expected:
            raise ValueError(f"Invalid group output path: {group.output_relative}")
        output = _contained_path(root, group.output_relative)
        _verify_digest(
            _contained_path(root, f"{group.output_relative}/{ENV_SHELL}"),
            group.shell_digest,
        )
        _verify_digest(
            _contained_path(root, f"{group.output_relative}/{ENV_YAML}"),
            group.yaml_digest,
        )
        if not output.is_dir():
            raise ValueError(f"Missing group output directory: {output}")


def _validate_executions(
    root: Path, groups: list[BatchGroup], executions: list[BatchExecution]
) -> None:
    group_map = {group.group_id: group for group in groups}
    membership = Counter()
    previous_group = ""
    for sequence, execution in enumerate(executions, 1):
        if sequence > 9999 or execution.execution_id != f"{sequence:04d}":
            raise ValueError("Invalid or unordered global execution IDs")
        group = group_map.get(execution.group_id)
        if group is None or execution.group_id < previous_group:
            raise ValueError("Invalid or unordered execution group membership")
        previous_group = execution.group_id
        membership[execution.group_id] += 1
        path = _contained_path(root, f"executions/{execution.execution_id}.sh")
        content = _verify_digest(path, execution.definition_digest).decode("utf-8")
        expected = {
            "ELBENCHO_EXECUTION_KIND": group.kind,
            "ELBENCHO_BATCH_GROUP_ID": group.group_id,
            "ELBENCHO_BATCH_OUTPUT_RELATIVE": group.output_relative,
        }
        for name, value in expected.items():
            if _export_value(content, name) != value:
                raise ValueError(
                    f"Execution {execution.execution_id} has incorrect {name}"
                )
    if set(membership) != set(group_map):
        raise ValueError("Every committed batch group must contain executions")


def read_batch_manifest(path: str | Path) -> BatchManifest:
    """Read committed membership, validate provenance and any permanent seal."""
    root = Path(path).resolve()
    if root.is_file():
        root = root.parent
    data = _read_regular(root / MANIFEST_FILENAME)
    headers, groups, executions = _parse_records(data)
    revision, datestamp = _validate_headers(headers, len(groups))
    _validate_groups(root, groups, datestamp)
    _validate_executions(root, groups, executions)
    digest = hashlib.sha256(data).hexdigest()
    seal = root / SEAL_FILENAME
    if (seal.exists() or seal.is_symlink()) and _read_regular(seal).decode(
        "utf-8"
    ).strip() != digest:
        raise ValueError(f"Sealed batch manifest digest mismatch: {root}")
    return BatchManifest(
        root, revision, datestamp, tuple(groups), tuple(executions), digest
    )


def execution_status(manifest: BatchManifest, execution: BatchExecution) -> str:
    """Read one execution's ledger status; invalid or missing is UNKNOWN."""
    path = _contained_path(manifest.root, f"executions/{execution.execution_id}.status")
    if not path.exists():
        return "UNKNOWN"
    value = _read_regular(path).decode("utf-8").strip()
    if value not in {
        "PENDING",
        "RUNNING",
        SUCCESS,
        "FAILED",
        "CANCELLED",
        "INTERRUPTED",
    }:
        return "UNKNOWN"
    return value


def _positive_coordinate(content: str, name: str) -> int:
    value = _export_value(content, name)
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise ValueError(f"Invalid positive execution coordinate: {name}")
    return int(value)


def _execution_stems(
    manifest: BatchManifest, group: BatchGroup, execution: BatchExecution
) -> list[str]:
    definition = _contained_path(
        manifest.root, f"executions/{execution.execution_id}.sh"
    )
    content = _read_regular(definition).decode("utf-8")
    nodes = _positive_coordinate(content, "nodes")
    if group.kind == "mdtest":
        tasks = _positive_coordinate(content, "tasks_per_node")
        iterations = _positive_coordinate(content, "MDTEST_ITERATIONS")
        prefix = f"mdtest-elbencho-c_{nodes:03d}-t_{tasks:03d}_{manifest.datestamp}"
        return [f"{prefix}_iter{iteration}" for iteration in range(1, iterations + 1)]
    size = _export_value(content, "io_size")
    if not re.fullmatch(r"[r0-9a-zA-Z,]+", size):
        raise ValueError("Invalid IO size in batch execution")
    threads = _positive_coordinate(content, "thread_count")
    depth = _positive_coordinate(content, "io_depth")
    return [
        f"elbencho-{size}-c_{nodes:03d}-s_{threads:03d}-d_{depth:03d}_{manifest.datestamp}"
    ]


def _copy_published(source: Path, destination: Path) -> None:
    data = _read_regular(source)
    if not data:
        raise ValueError(f"Empty published batch artifact: {source}")
    destination.write_bytes(data)


def stage_group_inputs(
    manifest: BatchManifest,
    group: BatchGroup,
    destination: Path,
    successful: tuple[BatchExecution, ...],
) -> int:
    """Snapshot SUCCESS inputs privately; the durable root ledger remains authoritative."""
    source = manifest.group_path(group)
    destination.mkdir(parents=True)
    ledger = destination / "executions"
    ledger.mkdir()
    for filename in (ENV_SHELL, ENV_YAML):
        _copy_published(source / filename, destination / filename)
    seen = set()
    membership = set(manifest.group_executions(group.group_id))
    for execution in successful:
        if execution not in membership:
            raise ValueError("Reporting input execution belongs to another group")
        stems = _execution_stems(manifest, group, execution)
        if seen.intersection(stems):
            raise ValueError(
                f"Repeated coordinates within batch group {group.group_id}"
            )
        seen.update(stems)
        _stage_execution(manifest, group, execution, destination, stems)
    return len(seen)


def _stage_execution(
    manifest: BatchManifest,
    group: BatchGroup,
    execution: BatchExecution,
    destination: Path,
    stems: list[str],
) -> None:
    source = manifest.group_path(group)
    ledger = destination / "executions"
    for stem in stems:
        for extension in ("csv", "out"):
            _copy_published(
                source / f"{stem}.{extension}", destination / f"{stem}.{extension}"
            )
        live = source / f"{stem}.live.csv"
        if live.exists():
            _copy_published(live, destination / live.name)
        # Optional: reports show a missing or unusable version as unknown.
        version = source / f"{stem}.out{VERSION_SUFFIX}"
        if version.is_file() and not version.is_symlink():
            (destination / version.name).write_bytes(version.read_bytes())
    definition = manifest.root / "executions" / f"{execution.execution_id}.sh"
    _copy_published(definition, ledger / definition.name)
    # These statuses exist only in this private, immutable reporting snapshot.
    (ledger / f"{execution.execution_id}.status").write_text(SUCCESS, encoding="utf-8")
    workload = _contained_path(
        manifest.root,
        f"{group.output_relative}/executions/{execution.execution_id}.workload.tsv",
    )
    if workload.exists():
        _copy_published(workload, ledger / workload.name)
    for suffix in ("log", "workers.tsv"):
        _stage_ledger_file(
            manifest, group, f"{execution.execution_id}.{suffix}", ledger
        )


def _stage_ledger_file(
    manifest: BatchManifest, group: BatchGroup, name: str, ledger: Path
) -> None:
    """Copy an optional ledger file, from the batch root or the group output.

    Kubernetes collection publishes ``executions/<id>.workers.tsv`` (the
    Pod/node map the live export joins) and logs in the batch root ledger.
    """
    for relative in (
        f"executions/{name}",
        f"{group.output_relative}/executions/{name}",
    ):
        source = _contained_path(manifest.root, relative)
        if source.exists():
            _copy_published(source, ledger / name)
            return


def _selected_groups(
    manifest: BatchManifest, kind: str, groups: str | None
) -> tuple[BatchGroup, ...]:
    if kind not in KINDS:
        raise ValueError(f"Unknown filesystem workload kind: {kind}")
    selected_ids = None if groups is None else set(groups.split(","))
    known_ids = {group.group_id for group in manifest.groups}
    if selected_ids is not None and (not selected_ids or not selected_ids <= known_ids):
        raise ValueError(f"Unknown batch groups: {groups}")
    return tuple(
        group
        for group in manifest.groups
        if (kind == "all" or group.kind == kind)
        and (selected_ids is None or group.group_id in selected_ids)
    )


@dataclass(frozen=True)
class ResultsRequest:
    """How a batch report writes its results document and report.html."""

    require_complete: bool
    options: dict
    command: tuple[str, ...]
    comparisons: list[dict] | None = None
    # A lib.filesystem_results.ReportSelection; None shows everything.
    selection: Any = None


def _option_default(flag: str):
    for option in REPORT_OPTIONS:
        if option.flag == flag:
            return option.settings.get("default")
    return None


def _results_request(
    args: argparse.Namespace | None, kind: str, groups: str | None
) -> ResultsRequest:
    """Describe the document from parsed arguments, or defaults without them."""
    options = {
        key: getattr(args, key, None)
        for key in ("only_nodes", "only_threads", "only_sizes", "only_iodepths")
    }
    options["live_detail"] = getattr(args, "live_detail", None) or _option_default(
        LIVE_DETAIL_FLAG
    )
    for key in ("shortfall_percent", "min_slow_windows"):
        flag = "--client-" + key.replace("_", "-")
        options[key] = getattr(args, f"client_{key}", None) or _option_default(flag)
    for key in ("client_max_timeseries_lines", "client_max_heatmap_rows"):
        flag = "--" + key.replace("_", "-")
        options[key] = getattr(args, key, None) or _option_default(flag)
    options["no_dual_y_axis"] = bool(getattr(args, "no_dual_y_axis", False))
    options["normalize_to"] = getattr(args, "normalize_to", None)
    options.update(groups=groups, kind=kind)
    return ResultsRequest(
        bool(getattr(args, "require_complete", False)),
        options,
        tuple(sys.argv),
        _load_comparisons(args),
        _report_selection(args, kind, groups),
    )


def _load_comparisons(args: argparse.Namespace | None) -> list[dict] | None:
    """The parsed ``--comparisons`` file (ValueError when invalid), if any."""
    # Deferred so --help and --version work without the reporting dependencies.
    from lib import filesystem_results  # pylint: disable=import-outside-toplevel

    return filesystem_results.load_comparisons(getattr(args, "comparisons", None))


def _report_selection(args: argparse.Namespace | None, kind: str, groups: str | None):
    """What report.html shows: the groups, kind and filters of the text."""
    # Deferred so --help and --version work without the reporting dependencies.
    from lib import filesystem_results  # pylint: disable=import-outside-toplevel

    return filesystem_results.ReportSelection.from_args(args, kind, groups)


def _engine_results_options(report_dir: Path, live_dir: Path, kind: str) -> list[str]:
    options = [f"{RESULTS_FRAGMENT_FLAG}={report_dir / RESULTS_FRAGMENT_FILENAME}"]
    if kind == "io":
        options.append(f"{RESULTS_LIVE_DIR_FLAG}={live_dir}")
    return options


def _without_consumed(options: list[str]) -> list[str]:
    """Drop the flags the batch router consumes; engines never see them."""
    return [
        item for item in options if item.split("=", 1)[0] not in BATCH_CONSUMED_FLAGS
    ]


@dataclass
class _GroupReports:
    """What a batch's group reports produced, by group ID."""

    outcomes: dict[str, str] = field(default_factory=dict)
    fragments: dict[str, dict | None] = field(default_factory=dict)
    report_failed: set[str] = field(default_factory=set)
    failed: bool = False

    def record(self, group_id: str, finished: tuple[str, bool, bool], output: Path):
        """Keep one finished group's outcome and fragment."""
        result, generated, group_failed = finished
        self.outcomes[group_id] = result
        fragment, broken = _collect_fragment(_group_dir(output, group_id), generated)
        self.fragments[group_id] = fragment
        if group_failed or broken:
            self.report_failed.add(group_id)
        self.failed = self.failed or group_failed or broken


def _group_dir(output: Path, group_id: str) -> Path:
    return output / GROUPS_DIRNAME / group_id


def _routed_options(
    analysis_args: argparse.Namespace | None,
    kinds: set[str],
    routed: dict[str, list[str]] | None = None,
) -> dict[str, list[str]]:
    """Each kind's engine options from parsed arguments, if any; ``routed``
    are already validated against a wider selection of ``kinds``."""
    if analysis_args is None:
        return {}
    if routed is None:
        routed = options_by_kind(analysis_args, kinds)
    return {kind: _without_consumed(options) for kind, options in routed.items()}


def _announce_workers(groups: int) -> None:
    workers = report_workers()
    if workers > 1 and groups > 1:
        print(
            f"Reporting {groups} groups with {workers} worker processes; "
            "each group's output prints once it and earlier groups finish.",
            file=sys.stderr,
            flush=True,
        )


def _run_group_reports(
    manifest: BatchManifest,
    selected: tuple[BatchGroup, ...],
    output: Path,
    options_for: Callable[[BatchGroup], list[str]],
) -> _GroupReports:
    """Report every selected group on one pool, printing them in order."""
    reports = _GroupReports()
    live_dir = output / RESULTS_LIVE_DIRNAME
    _announce_workers(len(selected))
    with report_pool() as pool:
        # Start every group, then submit every group's rendering, before
        # finishing any, so the pool stays full.
        started = []
        for group in selected:
            options = [
                *options_for(group),
                *_engine_results_options(
                    _group_dir(output, group.group_id), live_dir, group.kind
                ),
            ]
            sink, prepare = _start_group_report(
                manifest, group, output, options, pool=pool
            )
            started.append((group, sink, prepare))
        prepared = [(group, sink, prepare()) for group, sink, prepare in started]
        for group, sink, finish in prepared:
            finished = finish()
            sink.emit()
            reports.record(group.group_id, finished, output)
    return reports


def report_batch(
    path: str | Path,
    kind: str = "all",
    groups: str | None = None,
    output_dir: str | Path | None = None,
    engine_options: list[str] | None = None,
    *,
    analysis_args: argparse.Namespace | None = None,
    results_args: argparse.Namespace | None = None,
) -> int:
    """Generate independent group reports, the results document and report.html.

    Partial successes are retained. ``results_args`` names a parsed namespace
    whose document options apply when ``engine_options`` are forwarded verbatim.
    """
    manifest = read_batch_manifest(path)
    selected = _selected_groups(manifest, kind, groups)
    if not selected:
        raise ValueError("No batch groups match --kind and --groups")
    if analysis_args is not None and hasattr(analysis_args, "test_parse"):
        raise ValueError("--test-parse cannot report a prepared batch")
    request = _results_request(results_args or analysis_args, kind, groups)
    routed = _routed_options(analysis_args, {group.kind for group in selected})
    output = Path(output_dir).resolve() if output_dir else manifest.root / "reports"
    output.mkdir(parents=True, exist_ok=True)
    _clear_live_sidecars(output / RESULTS_LIVE_DIRNAME)
    reports = _run_group_reports(
        manifest,
        selected,
        output,
        lambda group: routed.get(group.kind, engine_options or []),
    )
    combined = (output if output_dir else manifest.root) / COMBINED_REPORT_FILENAME
    failed = reports.failed
    try:
        _write_combined_report(manifest, output, reports.outcomes, combined)
    except (OSError, ValueError) as error:
        print(f"Combined report: {error}", file=sys.stderr)
        failed = True
    complete = _write_results_document(manifest, selected, reports, output, request)
    for group in selected:
        # The document now holds every fragment.
        (_group_dir(output, group.group_id) / RESULTS_FRAGMENT_FILENAME).unlink(
            missing_ok=True
        )
    # With a document, nothing successful yet is incompleteness, not failure.
    if failed or complete is None:
        return 1
    return EXIT_INCOMPLETE if request.require_complete and not complete else 0


def _document_groups(document: dict, kind: str, groups: str | None) -> list[dict]:
    """The document groups --kind and --groups select, in document order."""
    if kind not in KINDS:
        raise ValueError(f"Unknown filesystem workload kind: {kind}")
    selected_ids = None if groups is None else set(groups.split(","))
    known_ids = {group["group_id"] for group in document["groups"]}
    if selected_ids is not None and (not selected_ids or not selected_ids <= known_ids):
        raise ValueError(f"Unknown document groups: {groups}")
    selected = [
        group
        for group in document["groups"]
        if (kind == "all" or group["kind"] == kind)
        and (selected_ids is None or group["group_id"] in selected_ids)
    ]
    if not selected:
        raise ValueError("No document groups match --kind and --groups")
    return selected


def _document_group_report(
    path: Path, group: dict, output: Path, options: list[str], pool: Executor
) -> tuple[GroupOutput, Callable[[], Callable[[], bool]]]:
    """Start rendering one group of a document into ``groups/<id>``.

    Calling the result submits the rendering; calling what that returns
    finishes it and says whether it succeeded.
    """
    sink = GroupOutput()
    report_dir = _group_dir(output, group["group_id"])

    def failed(error: Exception) -> bool:
        sink.stderr += f"Group {group['group_id']}: {error}\n"
        return False

    def finish(wait: Callable[[], None]) -> bool:
        try:
            wait()
            report = report_dir / REPORT_TXT_FILENAME
            if not report.exists():
                _publish_report_text(report, NO_MATCHING_RESULTS_TEXT)
        except _REPORT_ERRORS as error:
            return failed(error)
        return True

    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        # The engine replaces a previous report.txt once its request is valid.
        argv = [
            FROM_JSON_FLAG,
            str(path),
            "--groups",
            group["group_id"],
            "--output-dir",
            str(report_dir),
            BATCH_GROUP_FLAG,
            *options,
        ]
        with sink.capture():
            stage = _captured_stages(
                _start_engine(group["kind"], argv, pool), sink, lambda: None
            )
    except _REPORT_ERRORS as error:
        return sink, partial(partial, failed, error)

    def prepare() -> Callable[[], bool]:
        try:
            return partial(finish, stage())
        except _REPORT_ERRORS as error:
            return partial(failed, error)

    return sink, prepare


def _document_combined_report(document: dict, groups: list[dict], output: Path) -> None:
    """reports.txt for the rendered groups of a document (two or more)."""
    destination = output / COMBINED_REPORT_FILENAME
    destination.unlink(missing_ok=True)
    reports = [
        (group, _group_dir(output, group["group_id"]) / REPORT_TXT_FILENAME)
        for group in groups
    ]
    reports = [(group, report) for group, report in reports if report.is_file()]
    if len(reports) < 2:
        return
    sections = []
    for group, report in reports:
        target = group["target"]
        sections.append(
            _combined_section(
                (group["group_id"], group["kind"]),
                StorageTarget(target["name"], target["recorded"]),
                (
                    report,
                    Path(group.get("source") or document["batch"]["root"]) / ENV_YAML,
                ),
                group.get("settings_text") or "",
                group.get("settings") or {},
            )
        )
    _publish_report_text(destination, "".join(sections) + "\n")


def report_batch_document(
    path: str | Path,
    kind: str = "all",
    groups: str | None = None,
    output_dir: str | Path | None = None,
    *,
    analysis_args: argparse.Namespace | None = None,
    publish_html: bool = True,
    routed: dict[str, list[str]] | None = None,
) -> int:
    """Render a batch results document like a batch report: group reports,
    ``reports.txt`` and ``report.html`` under ``output_dir``.

    ``publish_html=False`` leaves ``report.html`` to the caller, which renders
    several documents into one and passes their ``routed`` options, validated
    against every document's groups.
    """
    # Deferred so --help and --version work without the reporting dependencies.
    from lib import filesystem_results  # pylint: disable=import-outside-toplevel
    from lib.results_document import (  # pylint: disable=import-outside-toplevel
        load_document,
    )

    path = Path(path)
    loaded = load_document(path)
    selected = _document_groups(loaded.data, kind, groups)
    if output_dir is None:
        raise ValueError(f"--output-dir is required with {FROM_JSON_FLAG}")
    output = Path(output_dir).resolve()
    comparisons = _load_comparisons(analysis_args)
    routed = _routed_options(
        analysis_args, {group["kind"] for group in selected}, routed
    )
    if publish_html:
        # First, so an invalid comparison fails before any output is written.
        filesystem_results.publish_report_html(
            [loaded],
            output,
            comparisons,
            _report_selection(analysis_args, kind, groups),
        )
    output.mkdir(parents=True, exist_ok=True)
    failed = False
    with report_pool() as pool:
        started = [
            _document_group_report(
                path, group, output, routed.get(group["kind"], []), pool
            )
            for group in selected
        ]
        prepared = [(sink, prepare()) for sink, prepare in started]
        for sink, finish in prepared:
            failed = not finish() or failed
            sink.emit()
    _document_combined_report(loaded.data, selected, output)
    if failed:
        return 1
    complete = filesystem_results.document_is_complete(loaded.data)
    require = bool(getattr(analysis_args, "require_complete", False))
    return EXIT_INCOMPLETE if require and not complete else 0


def is_batch_document(document: dict) -> bool:
    """True for the results document of a prepared batch."""
    return document["batch"].get("manifest_version") is not None


def _selected_document_ids(
    document: dict, kind: str, wanted: set[str] | None
) -> list[str]:
    return [
        group["group_id"]
        for group in document["groups"]
        if kind in ("all", group["kind"])
        and (wanted is None or group["group_id"] in wanted)
    ]


def _document_selections(
    documents: list, kind: str, groups: str | None
) -> list[tuple[int, Any, str | None]]:
    """(number, document, --groups for it) of the documents with anything
    selected by ``kind`` and ``groups``; an ID known to no document is an error."""
    wanted = set(groups.split(",")) if groups else None
    known = {group["group_id"] for doc in documents for group in doc.data["groups"]}
    if wanted and not wanted <= known:
        raise ValueError(f"Unknown document groups: {','.join(sorted(wanted - known))}")
    selections = []
    for number, loaded in enumerate(documents, 1):
        ids = _selected_document_ids(loaded.data, kind, wanted)
        if ids:
            selections.append((number, loaded, ",".join(ids) if wanted else None))
    if not selections:
        raise ValueError("No document groups match --kind and --groups")
    return selections


def _render_batch_document(
    args: argparse.Namespace,
    kind: str,
    loaded: Any,
    output_dir: str,
    groups: str | None,
    routed: dict[str, list[str]] | None,
) -> tuple[int, bool]:
    """Render a batch document without report.html; (status, any text)."""
    status = report_batch_document(
        loaded.path,
        kind,
        groups,
        output_dir,
        analysis_args=args,
        publish_html=False,
        routed=routed,
    )
    output = Path(output_dir).resolve()
    produced = any(
        (_group_dir(output, group["group_id"]) / REPORT_TXT_FILENAME).is_file()
        for group in _document_groups(loaded.data, kind, groups)
    )
    return status, produced


def _render_document(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    args: argparse.Namespace,
    kind: str,
    loaded: Any,
    output_dir: str,
    groups: str | None,
    publish: bool,
    routed: dict[str, list[str]] | None = None,
) -> tuple[int, bool]:
    """Render one document into ``output_dir``: (exit status, whether text was
    written). Without ``publish`` the caller writes report.html. ``routed``
    are the options by kind, validated against all of several documents'
    groups; one document validates its own."""
    if is_batch_document(loaded.data):
        if not publish:
            return _render_batch_document(
                args, kind, loaded, output_dir, groups, routed
            )
        status = report_batch_document(
            loaded.path, kind, groups, output_dir, analysis_args=args
        )
        return status, True
    if kind == "all":
        kind = min((group["kind"] for group in loaded.data["groups"]), default="io")
    engine = "extract-mdtest-elbencho.py" if kind == "mdtest" else _IO_ENGINE
    command = [
        sys.executable,
        str(_REPO_ROOT / "utils" / engine),
        FROM_JSON_FLAG,
        str(loaded.path),
        "--output-dir",
        output_dir,
    ]
    if groups:
        command.extend(["--groups", groups])
    if not publish:
        # Nothing to report in one document is fine among several.
        command.append(BATCH_GROUP_FLAG)
    elif args.comparisons:
        command.extend([COMPARISONS_FLAG, args.comparisons])
    options = (routed or options_by_kind(args, {kind}))[kind]
    # The engine replaces the previous report once its request is valid, so
    # a usage error keeps it and a report here is this run's.
    status = subprocess.run([*command, *options], check=False).returncode
    return status, (Path(output_dir) / REPORT_TXT_FILENAME).is_file()


def report_documents(documents: list, args: argparse.Namespace, kind: str) -> int:
    """Render loaded ``--from-json`` documents into ``args.output_dir``.

    One renders through its engine or, for a batch, like a batch report;
    several render into per-document subdirectories sharing one report.html.
    """
    # Deferred so --help and --version work without the reporting dependencies.
    from lib import filesystem_results  # pylint: disable=import-outside-toplevel

    if len(documents) == 1:
        return _render_document(
            args, kind, documents[0], args.output_dir, args.groups, True
        )[0]
    selections = _document_selections(documents, kind, args.groups)
    # An option must match a group of some document, not of each.
    routed = options_by_kind(
        args,
        {
            group["kind"]
            for _, loaded, groups in selections
            for group in _document_groups(loaded.data, kind, groups)
        },
    )
    # Before any rendering, so an invalid comparison writes nothing else.
    filesystem_results.publish_report_html(
        documents,
        args.output_dir,
        _load_comparisons(args),
        _report_selection(args, kind, args.groups),
    )
    rendered = [
        _render_document(
            args,
            kind,
            loaded,
            document_output_dir(args.output_dir, number, len(documents)),
            groups,
            False,
            routed,
        )
        for number, loaded, groups in selections
    ]
    statuses = [status for status, _ in rendered]
    produced = any(text for _, text in rendered)
    if not produced:
        print("No text report produced", file=sys.stderr)
    # A failure outranks incompleteness (exit 3), which outranks no text.
    failure = next((s for s in statuses if s not in (0, EXIT_INCOMPLETE)), None)
    if failure is not None:
        return failure
    if not produced:
        return EXIT_INCOMPLETE if EXIT_INCOMPLETE in statuses else 1
    return max(statuses)


@dataclass
class GroupOutput:
    """One group's captured stdout and stderr, printed later in group order."""

    stdout: str = ""
    stderr: str = ""

    @contextlib.contextmanager
    def capture(self):
        """Capture this process's stdout and stderr into the group."""
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                yield
        finally:
            self.stdout += stdout.getvalue()
            self.stderr += stderr.getvalue()

    def emit(self) -> None:
        """Print the captured stderr, then stdout, so the tables end the group."""
        sys.stderr.write(self.stderr)
        sys.stderr.flush()
        sys.stdout.write(self.stdout)
        sys.stdout.flush()


def _clear_live_sidecars(live_dir: Path) -> None:
    from lib.filesystem_results import (  # pylint: disable=import-outside-toplevel
        clear_live_sidecars,
    )

    clear_live_sidecars(live_dir)


def _collect_fragment(report_dir: Path, generated: bool) -> tuple[dict | None, bool]:
    """Read a group's fragment; the flag is True if one was owed but unusable."""
    from lib.filesystem_results import (  # pylint: disable=import-outside-toplevel
        read_fragment,
    )

    if not generated:
        return None, False
    try:
        return read_fragment(report_dir / RESULTS_FRAGMENT_FILENAME), False
    except (OSError, ValueError) as error:
        print(f"Group {report_dir.name}: results fragment: {error}", file=sys.stderr)
        return None, True


def _write_results_document(
    manifest: BatchManifest,
    selected: tuple[BatchGroup, ...],
    reports: _GroupReports,
    output: Path,
    request: ResultsRequest,
) -> bool | None:
    """Write the document and report.html; return completeness, None on failure."""
    from lib import filesystem_results  # pylint: disable=import-outside-toplevel

    destination = output / RESULTS_FILENAME
    try:
        document = filesystem_results.assemble_batch_document(
            manifest,
            selected,
            reports.fragments,
            reports.report_failed,
            output / RESULTS_LIVE_DIRNAME,
            filesystem_results.generator_block(list(request.command), request.options),
        )
        filesystem_results.publish_document(
            document, destination, request.comparisons, request.selection
        )
    except (OSError, ValueError) as error:
        print(f"Results document: {error}", file=sys.stderr)
        return None
    return filesystem_results.document_is_complete(document)


_REPORT_ERRORS = (OSError, ValueError, subprocess.CalledProcessError)


GroupFinish = Callable[[], tuple[str, bool, bool]]
# An engine's first stage submits its rendering and returns its second stage.
EngineStage = Callable[[], Callable[[], None] | None]


def _start_group_report(  # pylint: disable=too-many-arguments
    manifest: BatchManifest,
    group: BatchGroup,
    output: Path,
    options: list[str],
    *,
    pool: Executor,
) -> tuple[GroupOutput, Callable[[], GroupFinish]]:
    """Start one group's report.

    Calling the result submits the group's rendering; calling what that
    returns finishes it. Failures stay within the group. Its output and
    errors go to the returned GroupOutput.
    """
    sink = GroupOutput()

    def failed(error: Exception) -> tuple[str, bool, bool]:
        sink.stderr += f"Group {group.group_id}: {error}\n"
        return "Reporting failed", False, True

    # A stale fragment must never stand in for this run's evidence.
    (_group_dir(output, group.group_id) / RESULTS_FRAGMENT_FILENAME).unlink(
        missing_ok=True
    )
    successful = tuple(
        execution
        for execution in manifest.group_executions(group.group_id)
        if execution_status(manifest, execution) == SUCCESS
    )
    if not successful:
        return sink, lambda: lambda: ("No successful results", False, False)
    report_dir = _group_dir(output, group.group_id)
    report = report_dir / REPORT_TXT_FILENAME
    try:
        report_dir.mkdir(parents=True, exist_ok=True)
        # Invalidate prior evidence before an engine can partially overwrite it.
        (report_dir / REPORT_IDENTITY_FILENAME).unlink(missing_ok=True)
        report.unlink(missing_ok=True)
        pending = _report_group(
            manifest, group, successful, report_dir, options, sink=sink, pool=pool
        )
    except _REPORT_ERRORS as error:
        return sink, partial(partial, failed, error)

    def finish(rendering: Callable[[], None] | None) -> tuple[str, bool, bool]:
        try:
            if rendering is not None:
                rendering()
            if (
                not report.exists()
                and (report_dir / RESULTS_FRAGMENT_FILENAME).exists()
            ):
                # An engine whose filters matched nothing exits 0 with only a
                # fragment; the group still needs a report to bind its identity to.
                _publish_report_text(report, NO_MATCHING_RESULTS_TEXT)
            _publish_report_text(
                report_dir / REPORT_IDENTITY_FILENAME,
                _report_identity(manifest, group, report),
            )
        except _REPORT_ERRORS as error:
            return failed(error)
        return REPORTED, True, False

    def prepare() -> GroupFinish:
        try:
            rendering = pending() if pending is not None else None
        except _REPORT_ERRORS as error:
            return partial(failed, error)
        return partial(finish, rendering)

    return sink, prepare


def _report_identity(manifest: BatchManifest, group: BatchGroup, report: Path) -> str:
    """Bind a report to its canonical batch, immutable group, and exact content."""
    fields = [
        str(manifest.root),
        group.group_id,
        group.kind,
        group.output_relative,
        group.shell_digest,
        group.yaml_digest,
    ]
    for execution in manifest.group_executions(group.group_id):
        fields.extend((execution.execution_id, execution.definition_digest))
    owner = hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()
    content = hashlib.sha256(_read_regular(report)).hexdigest()
    return f"{owner}\n{content}\n"


def _has_owned_report(manifest: BatchManifest, group: BatchGroup, report: Path) -> bool:
    """Missing, foreign, corrupt, or partially overwritten evidence is not retained."""
    try:
        saved = _read_regular(report.parent / REPORT_IDENTITY_FILENAME)
        return saved.decode("ascii") == _report_identity(manifest, group, report)
    except (OSError, ValueError):
        return False


def _single_line(text: str) -> str:
    return text.replace("\r", " ").replace("\n", " ")


def _quoted_target_label(target: StorageTarget) -> str:
    """``"name"``, with a ``(derived)`` marker kept outside the quotes."""
    if target.name is None:
        return f'"{UNKNOWN_TARGET}"'
    quoted = f'"{_single_line(target.name)}"'
    return quoted if target.recorded else f"{quoted} (derived)"


def _group_storage_target(manifest: BatchManifest, group: BatchGroup) -> StorageTarget:
    """The storage target saved with the group; unknown when unreadable."""
    source = manifest.group_path(group) / ENV_YAML
    try:
        environment = _verify_digest(source, group.yaml_digest).decode("utf-8")
        snapshot = _parse_report_snapshot(environment, source)
    except (OSError, ValueError):
        return StorageTarget(None, False)
    return storage_target_from_env(snapshot)


def _write_combined_report(
    manifest: BatchManifest,
    output: Path,
    outcomes: dict[str, str],
    destination: Path,
) -> None:
    """Rebuild the omnibus from owned reports, including untouched selected kinds."""
    # Never leave a previous aggregate presenting failed or foreign evidence.
    destination.unlink(missing_ok=True)
    eligible = []
    for group in manifest.groups:
        report = _group_dir(output, group.group_id) / REPORT_TXT_FILENAME
        result = outcomes.get(group.group_id)
        if result is not None and result != REPORTED:
            continue
        if _has_owned_report(manifest, group, report):
            eligible.append((group, report))
    if len(eligible) < 2:
        return
    sections = []
    for group, report in eligible:
        source = manifest.group_path(group) / ENV_YAML
        environment = _verify_digest(source, group.yaml_digest).decode("utf-8")
        sections.append(
            _combined_section(
                (group.group_id, group.kind),
                _group_storage_target(manifest, group),
                (report, source),
                environment,
                (
                    None
                    if group.kind == "mdtest"
                    else (_parse_report_snapshot(environment, source))
                ),
            )
        )
    _publish_report_text(destination, "".join(sections) + "\n")


def _combined_section(
    identity: tuple[str, str],
    target: StorageTarget,
    paths: tuple[Path, Path],
    environment: str,
    snapshot: dict | None,
) -> str:
    """One group of reports.txt: (group ID, kind), (report, saved settings)."""
    group_id, kind = identity
    report, source = paths
    content = _read_regular(report).decode("utf-8")
    if kind == "mdtest":
        settings_label = f"Group {group_id} ({kind}) — Saved settings: {source}"
        annotated = _report_settings_block(environment, settings_label) + content
    else:
        annotated = format_io_snapshot_summary(snapshot or {}) + "\n" + content
    return (
        f"\n\n{'=' * 80}\nGroup {group_id} ({kind}) "
        f"target: {_quoted_target_label(target)}\n"
        f"Report: {report}\nSaved settings: {source}\n\n" + annotated
    )


def _parse_report_snapshot(environment: str, source: Path) -> dict:
    """Parse the digest-verified saved YAML without substituting current defaults."""
    # Deferred so --help and --version work without the reporting dependencies.
    import yaml  # pylint: disable=import-outside-toplevel

    try:
        snapshot = yaml.safe_load(environment)
    except yaml.YAMLError as error:
        raise ValueError(f"Invalid saved YAML: {source}") from error
    if not isinstance(snapshot, dict):
        raise ValueError(f"Expected saved YAML mapping: {source}")
    return snapshot


def _report_settings_block(environment: str, settings_label: str = "") -> str:
    """Render the exact saved YAML with a fence safe for its contents."""
    longest_run = max((len(run) for run in re.findall(r"`+", environment)), default=0)
    yaml_fence = "`" * max(3, longest_run + 1)
    trailing_newline = "" if environment.endswith("\n") else "\n"
    settings_label = settings_label or f"Saved settings ({ENV_YAML}):"
    return (
        f"\n\n{'-' * 80}\n{settings_label}\n"
        f"{yaml_fence}yaml\n{environment}{trailing_newline}{yaml_fence}\n\n"
    )


def _publish_report_text(destination: Path, content: str) -> None:
    """Publish complete report evidence without leaving partial files on interruption."""
    # Deferred so --help and --version work without the reporting dependencies.
    from lib import filesystem_results  # pylint: disable=import-outside-toplevel

    descriptor, temporary = tempfile.mkstemp(prefix=".index-", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            filesystem_results.set_published_mode(stream.fileno())
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _report_group(  # pylint: disable=too-many-arguments
    manifest: BatchManifest,
    group: BatchGroup,
    successful: tuple[BatchExecution, ...],
    output: Path,
    engine_options: list[str],
    *,
    sink: GroupOutput,
    pool: Executor,
) -> EngineStage:
    """Stage one group's inputs and start its reporter on the pool.

    Calling the result submits the reporter's rendering; calling what that
    returns waits for the reporter. Both capture its output into ``sink``.
    """
    temporary = tempfile.mkdtemp(prefix="filesystem-report-")
    try:
        inputs = Path(temporary) / manifest.group_path(group).name
        stage_group_inputs(manifest, group, inputs, successful)
        argv = [str(inputs), "--output-dir", str(output), *engine_options]
        with sink.capture():
            stage = _start_engine(group.kind, argv, pool)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return _captured_stages(stage, sink, partial(shutil.rmtree, temporary, True))


def _captured_stages(
    stage: EngineStage, sink: GroupOutput, cleanup: Callable[[], Any]
) -> EngineStage:
    """Run an engine's two stages inside ``sink``; ``cleanup`` follows either."""

    def prepare() -> Callable[[], None]:
        try:
            with sink.capture():
                wait = stage()
        except BaseException:
            cleanup()
            raise

        def finish() -> None:
            try:
                if wait is not None:
                    with sink.capture():
                        wait()
            finally:
                cleanup()

        return finish

    return prepare


_IO_ENGINE = "extract-elbencho.py"


def _in_process_engine(call: Callable[[], Any]) -> Any:
    """Run elbencho reporter code; a nonzero exit or a crash fails the group."""
    try:
        return call()
    except SystemExit as stop:
        if stop.code in (None, 0):
            return None
        raise ValueError(f"{_IO_ENGINE} exited with status {stop.code}") from None
    except Exception as error:  # pylint: disable=broad-exception-caught
        traceback.print_exc()
        raise ValueError(f"{_IO_ENGINE} failed: {error}") from error


def _start_engine(kind: str, argv: list[str], pool: Executor) -> EngineStage:
    """Start a group's reporter: elbencho in this process, mdtest as a task.

    Calling the result submits the rendering (elbencho); calling what it
    returns waits for the reporter.
    """
    if kind == "io":
        render = _in_process_engine(lambda: load_engine().start_report(argv, pool))

        def prepare_io() -> Callable[[], None]:
            finish = _in_process_engine(render) if render is not None else None

            def wait_io() -> None:
                status = _in_process_engine(finish) if finish is not None else None
                if status:
                    raise ValueError(f"{_IO_ENGINE} exited with status {status}")

            return wait_io

        return prepare_io
    command = [
        sys.executable,
        str(_REPO_ROOT / "utils" / "extract-mdtest-elbencho.py"),
        *argv,
    ]
    future = pool.submit(run_command, command)

    def wait_mdtest() -> None:
        completed = future.result()
        sys.stdout.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        completed.check_returncode()

    return lambda: wait_mdtest


def route_batch_report(args: argparse.Namespace, kind: str, argv: list[str]) -> bool:
    """Route a specialized reporter's batch inputs; ordinary imports stay unchanged."""
    reject_batch_descendant_inputs(args.input_dirs)
    batches = [
        directory for directory in args.input_dirs if is_batch_directory(directory)
    ]
    if not batches:
        if args.groups:
            raise ValueError("--groups requires a prepared batch input")
        return False
    if len(args.input_dirs) != 1:
        raise ValueError("A prepared batch must be the sole raw reporting input")
    if getattr(args, "test_parse", None):
        raise ValueError("--test-parse cannot report a prepared batch")
    options = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in ("--groups", "--output-dir", COMPARISONS_FLAG):
            index += 2
            continue
        if argument.startswith(
            ("--groups=", "--output-dir=", f"{COMPARISONS_FLAG}=")
        ) or argument in (
            *BATCH_CONSUMED_FLAGS,
            batches[0],
        ):
            index += 1
            continue
        options.append(argument)
        index += 1
    raise SystemExit(
        report_batch(
            batches[0], kind, args.groups, args.output_dir, options, results_args=args
        )
    )
