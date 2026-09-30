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
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
from urllib.parse import quote

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
            if len(relative.parts) >= 2 and relative.parts[0] == "groups":
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


def _execution_status(manifest: BatchManifest, execution: BatchExecution) -> str:
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
    log = _contained_path(manifest.root, f"executions/{execution.execution_id}.log")
    if not log.exists():
        log = _contained_path(
            manifest.root,
            f"{group.output_relative}/executions/{execution.execution_id}.log",
        )
    if log.exists():
        _copy_published(log, ledger / log.name)


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


def _markdown_link(label: str, target: Path, output: Path) -> str:
    return f"[{label}]({quote(os.path.relpath(target, output), safe='/')})"


def report_batch(
    path: str | Path,
    kind: str = "all",
    groups: str | None = None,
    output_dir: str | Path | None = None,
    engine_options: list[str] | None = None,
) -> int:
    """Generate independent group reports and an index; retain partial successes."""
    manifest = read_batch_manifest(path)
    selected = _selected_groups(manifest, kind, groups)
    output = Path(output_dir).resolve() if output_dir else manifest.root / "reports"
    output.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Filesystem batch reports",
        "",
        f"Batch: `{manifest.root.name}`",
        "",
        "| Group | Kind | Saved settings | Cell states | Reports |",
        "| --- | --- | --- | --- | --- |",
    ]
    generated = 0
    failed = False
    for group in selected:
        executions = manifest.group_executions(group.group_id)
        statuses = {
            execution.execution_id: _execution_status(manifest, execution)
            for execution in executions
        }
        counts = Counter(statuses.values())
        successful = tuple(
            execution
            for execution in executions
            if statuses[execution.execution_id] == SUCCESS
        )
        report_dir = output / "groups" / group.group_id
        result = "No successful results"
        if successful:
            try:
                report_dir.mkdir(parents=True, exist_ok=True)
                _report_group(
                    manifest, group, successful, report_dir, engine_options or []
                )
                generated += 1
                result = _markdown_link("report", report_dir / "report.txt", output)
            except (OSError, ValueError, subprocess.CalledProcessError) as error:
                failed = True
                result = "Reporting failed"
                print(f"Group {group.group_id}: {error}", file=sys.stderr)
        source = manifest.group_path(group)
        settings = " / ".join(
            _markdown_link(name, source / name, output)
            for name in (ENV_SHELL, ENV_YAML)
        )
        states = ", ".join(
            f"{state}: {count}" for state, count in sorted(counts.items())
        )
        lines.append(
            f"| {group.group_id} | {group.kind} | {settings} | {states} | {result} |"
        )
    (output / "index.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return int(failed or not generated)


def _report_group(
    manifest: BatchManifest,
    group: BatchGroup,
    successful: tuple[BatchExecution, ...],
    output: Path,
    engine_options: list[str],
) -> None:
    with tempfile.TemporaryDirectory(prefix="filesystem-report-") as temporary:
        inputs = Path(temporary) / manifest.group_path(group).name
        stage_group_inputs(manifest, group, inputs, successful)
        engine = (
            "extract-elbencho.py"
            if group.kind == "io"
            else "extract-mdtest-elbencho.py"
        )
        command = [
            sys.executable,
            str(_REPO_ROOT / "utils" / engine),
            str(inputs),
            "--output-dir",
            str(output),
            *engine_options,
        ]
        if "--markdown" in engine_options:
            completed = subprocess.run(
                command, check=True, stdout=subprocess.PIPE, text=True
            )
            (output / "report.txt").write_text(completed.stdout, encoding="utf-8")
            print(completed.stdout, end="")
        else:
            subprocess.run(command, check=True)


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
    if len(args.input_dirs) != 1 or args.from_csv:
        raise ValueError("A prepared batch must be the sole raw reporting input")
    options = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in ("--groups", "--output-dir"):
            index += 2
            continue
        if (
            argument.startswith(("--groups=", "--output-dir="))
            or argument == batches[0]
        ):
            index += 1
            continue
        options.append(argument)
        index += 1
    raise SystemExit(
        report_batch(batches[0], kind, args.groups, args.output_dir, options)
    )
