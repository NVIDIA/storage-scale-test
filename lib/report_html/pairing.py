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

"""Comparison sets: report sections that differ in exactly one setting.

Every section (group and shape) of every loaded document is a candidate.
Candidates are comparable when they share kind, shape (IO: write and read
block sizes and access patterns; metadata: branch factor and items per
directory), swept dimension (the first of nodes, threads, IO depth or
tasks that varies; none for a single point) and fixed coordinates (the
other dimensions, node count included for a single point, take the same
set of values or value combinations; the swept values may differ),
metadata normalization basis (``--normalize-to``, so rates are on one
scale), and exactly one compared setting differs. Only settings that apply to the
candidate's run count (``lib.setting_relevance``: e.g. writer file sizes
not for read-from runs,
single-big-file settings only with a single big file, Kubernetes settings
only on kubectl, the swept dimension's own value list never). Every
compared setting below is read from those only (absent or not applying is
empty):

- IO mode: the section's ``coordinates.direct_io``.
- Read-from path: setting ``sweep_read_from`` (else the executions'
  ``dataset.read_from``) with ``KUBECTL_MAPPED_READ_FROM``; trailing ``/``
  removed; empty is generated data.
- Storage target: ``groups[].target.name``. A different target also
  explains different test directories and read-from paths (they live on the
  target), so those may differ too in a target comparison.
- Test directory: setting ``TEST_DIRS`` (paths without trailing ``/``, with
  their node specs), ``KUBECTL_MAPPED_TEST_DIRS`` and ``KUBECTL_NAMESPACE``.
- File size: ``ELBENCHO_FILE_SIZE``, ``ELBENCHO_SINGLE_BIG_FILE_SIZE``
  (``K``/``M``/``G``/``T`` sizes compare as binary bytes) and
  ``ELBENCHO_FILE_SIZE_MULTIPLIER``; read-from sections compare equal, the
  read-from path naming the dataset.
- File layout: coordinates ``file_layout``, ``single_big_file`` and
  ``all_nodes_all_data`` (else ``ELBENCHO_FILE_LAYOUT``, default
  ``worker-directories``, ``ELBENCHO_SINGLE_BIG_FILE`` and
  ``ELBENCHO_ALL_NODES_ACCESS_ALL_DATA``) with
  ``ELBENCHO_SINGLE_BIG_FILE_BASENAME``; metadata: ``mdtest_layout`` and
  ``single_dir_*``.

Every other setting that applies to both runs must match (scalars compare
as stripped text, containers as canonical JSON); a setting only one side
records or applies is not compared (where the condition itself differs, its
own setting or dimension differs), so older runs pair with newer ones.
``_SHAPE_SETTINGS`` never block: the section shape already matches.
``_NOTED`` settings (IO run duration, ``ELBENCHO_SCALE_READ_WRITE_DURATION``,
else the executions' uniform phase ``configured_time_limit_s``; none for
runs to completion) never block either; sets whose members differ in one are
reported as such. Candidates differing in the same setting form one set,
split deterministically into charts of at most ``MAX_SERIES`` members. A
comparison file forces sets regardless of settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Sequence

import yaml

from lib.filesystem_coordinates import PLAN_READ_FROM
from lib.report_html.common import (
    IO_DIMS,
    K_COORDS,
    MD_DIMS,
    GroupContext,
    sort_executions,
    sweep_axes,
)
from lib.report_html.mdtest import MdShape, md_shapes
from lib.report_html.model import slug
from lib.report_html.sweep import IoShape, io_shapes
from lib.reporting_common import natural_key
from lib.results_document import LoadedDocument
from lib.setting_relevance import RunContext, relevant_settings, run_context

MAX_SERIES = 8
IO_MODE = "io_mode"
READ_FROM = "read_from"
TARGET = "target"
TEST_DIR = "test_dir"
FILE_SIZE = "file_size"
FILE_LAYOUT = "file_layout"
DIMENSIONS = (IO_MODE, READ_FROM, TARGET, TEST_DIR, FILE_SIZE, FILE_LAYOUT)
DIMENSION_TITLES = {
    IO_MODE: "IO mode",
    READ_FROM: "Read-from path",
    TARGET: "Storage target",
    TEST_DIR: "Test directory",
    FILE_SIZE: "File size",
    FILE_LAYOUT: "File layout",
}
# Dimensions a difference in the key dimension also explains.
_IMPLIED = {TARGET: (TEST_DIR, READ_FROM)}
# Settings read by a dimension, so not compared as "other" settings.
_DIMENSION_SETTINGS = {
    "dio_or_bio",
    "sweep_read_from",
    "KUBECTL_MAPPED_READ_FROM",
    "STORAGE_TARGET_NAME",
    "TEST_DIRS",
    "KUBECTL_MAPPED_TEST_DIRS",
    "KUBECTL_NAMESPACE",
    "ELBENCHO_FILE_SIZE",
    "ELBENCHO_SINGLE_BIG_FILE_SIZE",
    "ELBENCHO_FILE_SIZE_MULTIPLIER",
    "ELBENCHO_FILE_LAYOUT",
    "ELBENCHO_SINGLE_BIG_FILE",
    "ELBENCHO_SINGLE_BIG_FILE_BASENAME",
    "ELBENCHO_ALL_NODES_ACCESS_ALL_DATA",
    "mdtest_layout",
    "single_dir_target_files",
    "single_dir_files_per_worker",
    "single_dir_actual_files",
}
# Lists the section shape already matches.
_SHAPE_SETTINGS = {"ELBENCHO_SCALE_IO_SIZES", "rand_option"}
# Settings that never block pairing but are reported when they differ.
DURATION_SETTING = "ELBENCHO_SCALE_READ_WRITE_DURATION"
_NOTED = {DURATION_SETTING}
_DEFAULT_LAYOUT = "worker-directories"
_DURATION = re.compile(r"^\s*(\d+)\s*([smh]?)\s*$")
_DURATION_FACTORS = {"": 1, "s": 1, "m": 60, "h": 3600}
UNKNOWN_DURATION = "duration unknown"
COMPLETION = "run to completion"
_SIZE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtp]?)(?:i?b)?\s*$", re.I)
_SIZE_POWERS = {"": 0, "k": 1, "m": 2, "g": 3, "t": 4, "p": 5}
_TRUE = {"1", "true", "yes"}
_ENTRY_KEYS = {"title", "sections"}
_SETTINGS = "settings"


class ComparisonFileError(ValueError):
    """A comparison file is malformed or names sections that do not exist."""


@dataclass(frozen=True)
class Value:
    """One candidate's value of a compared setting."""

    key: str
    label: str
    order: tuple


@dataclass
class Candidate:  # pylint: disable=too-many-instance-attributes
    """One report section that may join a comparison."""

    ctx: GroupContext
    kind: str
    shape: IoShape | MdShape
    executions: list[dict]
    x: str | None
    others_vary: bool
    match: tuple
    context: RunContext = field(default_factory=RunContext)
    relevant: dict = field(default_factory=dict)
    values: dict[str, Value] = field(default_factory=dict)
    settings: dict[str, str] = field(default_factory=dict)
    duration_s: float | None = None

    @property
    def duration_key(self) -> float | str | None:
        """What run durations compare by: seconds, completion, or None."""
        return COMPLETION if self.context.run_to_completion else self.duration_s

    @property
    def duration_text(self) -> str:
        """The run duration as text, ``run to completion`` or ``duration
        unknown``."""
        if self.context.run_to_completion:
            return COMPLETION
        if self.duration_s is None:
            return UNKNOWN_DURATION
        return f"{self.duration_s:g} s"

    @property
    def anchor(self) -> str:
        """The anchor of this candidate's section."""
        return self.ctx.anchor(self.shape.slug())

    @property
    def dims(self) -> tuple:
        """Swept dimensions of this kind: (coordinate, label, suffix)."""
        return MD_DIMS if self.kind == "mdtest" else IO_DIMS

    def ref(self, several: bool) -> str:
        """Short reference: ``g0003`` or, with several documents, ``d2 g0003``."""
        group = f"g{self.ctx.group_id}"
        return f"d{self.ctx.document} {group}" if several else group

    def sort_key(self) -> tuple:
        """Document, group, then shape order."""
        return (self.ctx.document, self.ctx.group_id, self.shape.sort_key())


@dataclass
class CompareSet:
    """Candidates shown together; ``dimension`` is None for a forced set."""

    members: list[Candidate]
    dimension: str | None = None
    title: str | None = None
    part: tuple[int, int] = (1, 1)

    @property
    def durations_differ(self) -> bool:
        """Whether the members' run durations are not all equal (unknown
        differs from known)."""
        return len({m.duration_key for m in self.members}) > 1

    def anchor(self) -> str:
        """Content-derived anchor: the setting or title plus a member hash."""
        names = "\n".join(member.anchor for member in self.members)
        digest = hashlib.sha256(names.encode("utf-8")).hexdigest()[:8]
        head = slug(self.dimension) if self.dimension else slug(self.title)[:40]
        return f"cmp-{head}-{digest}"


def _canonical(value: Any) -> Any:
    """Scalars as stripped text (None is empty); containers recursively."""
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return "" if value is None else str(value).strip()


def canonical_text(value: Any) -> str:
    """Canonical JSON text of a setting value, for equality."""
    return json.dumps(_canonical(value), sort_keys=True, ensure_ascii=False)


def _value(key: Any, label: str, number: float | None = None) -> Value:
    order = (0, number, ()) if number is not None else (1, 0, natural_key(label))
    return Value(canonical_text(key), label, order)


def _path(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    return text.rstrip("/") or text


def _flag(value: Any) -> bool:
    return str(value).strip().lower() in _TRUE


def size_bytes(value: Any) -> int | str:
    """Bytes of a ``10G``-style size (binary units), else the stripped text."""
    text = "" if value is None else str(value).strip()
    match = _SIZE.match(text)
    if not match:
        return text
    power = _SIZE_POWERS[match.group(2).lower()]
    return int(float(match.group(1)) * 1024**power)


def _io_mode(candidate: Candidate) -> Value:
    if not isinstance(candidate.shape, IoShape):
        return _value("", "")
    direct = candidate.shape.direct_io
    return _value(direct, candidate.shape.mode_title(), 0 if direct else 1)


def _read_from(candidate: Candidate) -> Value:
    settings = candidate.relevant
    path = settings.get("sweep_read_from")
    if path is None:
        paths = {
            _path((e.get("dataset") or {}).get("read_from"))
            for e in candidate.executions
        }
        path = ", ".join(sorted(paths))
    path = _path(path)
    key = [path, _path(settings.get("KUBECTL_MAPPED_READ_FROM"))]
    return _value(key, path or "generated data")


def _target(candidate: Candidate) -> Value:
    name = str((candidate.ctx.group.get("target") or {}).get("name") or "").strip()
    return _value(name, name or "(unnamed)")


def _test_dir(candidate: Candidate) -> Value:
    settings = candidate.relevant
    dirs = settings.get("TEST_DIRS")
    if isinstance(dirs, dict):
        dirs = {_path(path): spec for path, spec in dirs.items()}
        label = ", ".join(sorted(dirs))
    else:
        label = _path(dirs)
    key = [dirs, settings.get("KUBECTL_MAPPED_TEST_DIRS")]
    key.append(settings.get("KUBECTL_NAMESPACE"))
    return _value(key, label or "(none)")


def _file_size(candidate: Candidate) -> Value:
    if candidate.context.phase_plan == PLAN_READ_FROM:
        # The read-from path names the dataset; writer sizes do not apply.
        return _value("read-from", "read-from dataset")
    settings = candidate.relevant
    size = size_bytes(settings.get("ELBENCHO_FILE_SIZE"))
    big = size_bytes(settings.get("ELBENCHO_SINGLE_BIG_FILE_SIZE"))
    multiplier = settings.get("ELBENCHO_FILE_SIZE_MULTIPLIER")
    labels = []
    if size:
        labels.append(str(settings["ELBENCHO_FILE_SIZE"]).strip())
    if big:
        labels.append(
            f"single big file {str(settings['ELBENCHO_SINGLE_BIG_FILE_SIZE']).strip()}"
        )
    if multiplier not in (None, ""):
        labels.append(f"IO size × {multiplier}")
    number = next((v for v in (size, big) if isinstance(v, int) and v), None)
    return _value([size, big, multiplier], ", ".join(labels) or "default", number)


def _io_layout(candidate: Candidate) -> Value:
    settings = candidate.relevant
    coords = candidate.executions[0].get(K_COORDS) or {}
    layout = coords.get("file_layout") or settings.get("ELBENCHO_FILE_LAYOUT")
    layout = str(layout or _DEFAULT_LAYOUT).strip()
    big = candidate.context.single_big_file
    shared = coords.get("all_nodes_all_data")
    if shared is None:
        shared = _flag(settings.get("ELBENCHO_ALL_NODES_ACCESS_ALL_DATA"))
    basename = settings.get("ELBENCHO_SINGLE_BIG_FILE_BASENAME")
    words = [layout]
    words += ["single big file"] if big else []
    words += ["all nodes all data"] if shared else []
    return _value([layout, big, bool(shared), basename], " · ".join(words))


def _md_layout(candidate: Candidate) -> Value:
    settings = candidate.relevant
    keys = ("single_dir_target_files", "single_dir_files_per_worker")
    keys += ("single_dir_actual_files",)
    layout = str(settings.get("mdtest_layout") or "standard").strip()
    files = settings.get("single_dir_target_files")
    label = f"{layout} ({files} files)" if files else layout
    return _value([layout] + [settings.get(key) for key in keys], label)


def _file_layout(candidate: Candidate) -> Value:
    if candidate.kind == "mdtest":
        return _md_layout(candidate)
    return _io_layout(candidate)


_READERS: dict[str, Callable[[Candidate], Value]] = {
    IO_MODE: _io_mode,
    READ_FROM: _read_from,
    TARGET: _target,
    TEST_DIR: _test_dir,
    FILE_SIZE: _file_size,
    FILE_LAYOUT: _file_layout,
}


def _other_settings(relevant: dict) -> dict[str, str]:
    skip = _DIMENSION_SETTINGS | _SHAPE_SETTINGS | _NOTED
    return {
        str(key): canonical_text(value)
        for key, value in relevant.items()
        if key not in skip
    }


def _duration_seconds(value: Any) -> int | None:
    """Seconds of a ``300``, ``5m`` or ``1h`` duration setting, else None."""
    match = _DURATION.match("" if value is None else str(value))
    if not match:
        return None
    return int(match.group(1)) * _DURATION_FACTORS[match.group(2)]


def _phase_limit(executions: list[dict]) -> float | None:
    """The executions' phase time limit when they all share one."""
    limits = set()
    for execution in executions:
        for phase in (execution.get("results") or {}).get("phases") or []:
            limit = phase.get("configured_time_limit_s")
            if isinstance(limit, (int, float)) and not isinstance(limit, bool):
                limits.add(limit)
    return limits.pop() if len(limits) == 1 else None


def _duration(candidate: Candidate) -> float | None:
    """IO run duration: the group setting, else the phases' time limit."""
    if candidate.kind != "io" or candidate.context.run_to_completion:
        return None
    seconds = _duration_seconds(candidate.relevant.get(DURATION_SETTING))
    if seconds is None:
        return _phase_limit(candidate.executions)
    return seconds


def _fixed_coordinates(coords: list[dict], dims, x: str | None) -> frozenset:
    """The value combinations of the dimensions other than the swept ``x``."""
    names = [name for name, _, _ in dims if name != x]
    return frozenset(tuple(c.get(name) for name in names) for c in coords)


def _candidate(ctx: GroupContext, shape, executions: list[dict]) -> Candidate:
    kind = "mdtest" if isinstance(shape, MdShape) else "io"
    executions = sort_executions(executions)
    dims = MD_DIMS if kind == "mdtest" else IO_DIMS
    coords = [e[K_COORDS] for e in executions]
    x, others = sweep_axes(coords, dims)
    if isinstance(shape, IoShape):
        layout = (shape.write_block, shape.write_access)
        layout += (shape.read_block, shape.read_access)
    else:
        layout = (shape.branch_factor, shape.items_per_dir)
    # Rates normalized to different node counts are not on one scale.
    match = (kind, x) + layout + (_fixed_coordinates(coords, dims, x),)
    match += (ctx.normalize_to,)
    candidate = Candidate(ctx, kind, shape, executions, x, bool(others), match)
    settings = ctx.group.get(_SETTINGS) or {}
    candidate.context = run_context(kind, settings, coords, x)
    candidate.relevant = relevant_settings(settings, candidate.context)
    candidate.values = {name: read(candidate) for name, read in _READERS.items()}
    candidate.settings = _other_settings(candidate.relevant)
    candidate.duration_s = _duration(candidate)
    return candidate


def candidates(contexts: Sequence[tuple[GroupContext, list[dict]]]) -> list[Candidate]:
    """One candidate per report section, in report order."""
    out = []
    for ctx, executions in contexts:
        if ctx.group.get("kind") == "mdtest":
            shapes = md_shapes(executions)
        else:
            shapes = io_shapes(executions)
        out.extend(_candidate(ctx, shape, items) for shape, items in shapes)
    return out


def _compatible(first: Candidate, second: Candidate) -> bool:
    shared = first.settings.keys() & second.settings.keys()
    return all(first.settings[key] == second.settings[key] for key in shared)


def _clusters(members: list[Candidate]) -> list[list[Candidate]]:
    """Greedy, order-preserving clusters whose other settings all agree."""
    clusters: list[list[Candidate]] = []
    for member in members:
        for cluster in clusters:
            if all(_compatible(member, other) for other in cluster):
                cluster.append(member)
                break
        else:
            clusters.append([member])
    return clusters


def _member_order(dimension: str) -> Callable[[Candidate], tuple]:
    def key(member: Candidate) -> tuple:
        return member.values[dimension].order + member.sort_key()

    return key


def split(members: list[Candidate], dimension: str | None, title=None):
    """Members in balanced chunks of at most MAX_SERIES, sorted by value
    (forced sets keep their file order)."""
    ordered = sorted(members, key=_member_order(dimension)) if dimension else members
    parts = math.ceil(len(ordered) / MAX_SERIES)
    size = math.ceil(len(ordered) / parts)
    chunks = [ordered[i : i + size] for i in range(0, len(ordered), size)]
    return [
        CompareSet(chunk, dimension, title, (index, len(chunks)))
        for index, chunk in enumerate(chunks, 1)
    ]


def _dimension_sets(members: list[Candidate], dimension: str) -> list[CompareSet]:
    free = (dimension,) + _IMPLIED.get(dimension, ())
    kept = [name for name in DIMENSIONS if name not in free]
    partitions: dict[tuple, list[Candidate]] = {}
    for member in members:
        key = tuple(member.values[name].key for name in kept)
        partitions.setdefault(key, []).append(member)
    sets = []
    for partition in partitions.values():
        for cluster in _clusters(partition):
            if len({m.values[dimension].key for m in cluster}) > 1:
                sets.extend(split(cluster, dimension))
    return sets


def automatic_sets(found: Sequence[Candidate]) -> list[CompareSet]:
    """Sets of candidates that differ in exactly one compared setting."""
    by_match: dict[tuple, list[Candidate]] = {}
    for candidate in found:
        by_match.setdefault(candidate.match, []).append(candidate)
    sets = []
    for members in by_match.values():
        if len(members) > 1:
            for dimension in DIMENSIONS:
                sets.extend(_dimension_sets(members, dimension))
    return sets


# ----- comparison file ------------------------------------------------------


def _fail(index: int, message: str) -> ComparisonFileError:
    return ComparisonFileError(f"comparison file entry {index}: {message}")


def _check_entry(index: int, entry: Any) -> None:
    if not isinstance(entry, dict):
        raise _fail(index, "expected a mapping with 'title' and 'sections'")
    unknown = sorted(str(key) for key in entry.keys() - _ENTRY_KEYS)
    if unknown:
        raise _fail(index, f"unknown key(s): {', '.join(unknown)}")
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        raise _fail(index, "'title' must be a non-empty string")
    sections = entry.get("sections")
    if not isinstance(sections, list) or len(sections) < 2:
        raise _fail(index, "'sections' must list at least two sections")
    for reference in sections:
        if not isinstance(reference, str):
            raise _fail(
                index,
                f"section {reference!r} is not a string; quote section "
                "references, e.g. '1:0003' (YAML reads unquoted 1:3 as 63)",
            )
        if ":" not in reference:
            raise _fail(
                index, f"section {reference!r} is not '<doc>:<group>[:<shape>]'"
            )
    if len(set(sections)) < len(sections):
        raise _fail(index, "'sections' lists a section twice")


def check_comparisons(data: Any) -> list[dict]:
    """Validate the structure of a parsed comparison file; return its entries.

    The file is a YAML list of ``{title, sections}`` mappings; each section
    is ``"<doc>:<group>[:<shape>]"``: the 1-based document index or exact
    document path, the group ID, and the section shape (its anchor part,
    e.g. ``1m-seq-dio``) when the group has several.
    """
    if data is None:
        return []
    if not isinstance(data, list):
        raise ComparisonFileError(
            "comparison file: expected a list of {title, sections} entries"
        )
    for index, entry in enumerate(data, 1):
        _check_entry(index, entry)
    return data


def load_comparison_file(path: str | Path) -> list[dict]:
    """Read and validate a YAML comparison file."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise ComparisonFileError(f"{path}: {error.strerror or error}") from error
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ComparisonFileError(f"{path}: not valid YAML ({error})") from error
    return check_comparisons(data)


def _document_names(documents: Sequence[LoadedDocument]) -> list[tuple[str, int]]:
    """(name, index) pairs a reference may start with, longest name first."""
    names = []
    for index, loaded in enumerate(documents, 1):
        names.append((str(index), index))
        if loaded.path:
            names.append((str(loaded.path), index))
            names.append((str(Path(loaded.path).resolve()), index))
    return sorted(set(names), key=lambda item: (-len(item[0]), item[1]))


def _split_reference(reference: str, names) -> tuple[int | None, list[str]]:
    for name, index in names:
        if reference.startswith(f"{name}:"):
            return index, reference[len(name) + 1 :].split(":")
    return None, []


def _same_group(wanted: str, group_id: str) -> bool:
    if wanted.isdigit() and group_id.isdigit():
        return int(wanted) == int(group_id)
    return wanted == group_id


def _pick_shape(found: list[Candidate], shape: str | None, where: str) -> Candidate:
    shapes = [c.shape.slug() for c in found]
    if shape is None:
        if len(found) == 1:
            return found[0]
        raise ComparisonFileError(
            f"{where} has several sections; add one of: {', '.join(shapes)}"
        )
    if shape in shapes:
        return found[shapes.index(shape)]
    raise ComparisonFileError(
        f"{where} has no section {shape!r}; available: {', '.join(shapes)}"
    )


def resolve_reference(
    reference: str, documents: Sequence[LoadedDocument], found: list[Candidate]
) -> Candidate:
    """The candidate named by ``<doc>:<group>[:<shape>]``."""
    document, rest = _split_reference(reference, _document_names(documents))
    if document is None:
        raise ComparisonFileError(
            f"section {reference!r}: no such document ({len(documents)} loaded; "
            "use a 1-based index or the exact path)"
        )
    if len(rest) > 2 or not rest[0]:
        raise ComparisonFileError(
            f"section {reference!r} is not '<doc>:<group>[:<shape>]'"
        )
    where = f"section {reference!r}: group {rest[0]} of document {document}"
    in_group = [
        c
        for c in found
        if c.ctx.document == document and _same_group(rest[0], c.ctx.group_id)
    ]
    if not in_group:
        raise ComparisonFileError(f"{where} has no reported sections")
    return _pick_shape(in_group, rest[1] if len(rest) > 1 else None, where)


def _check_forced(title: str, members: list[Candidate]) -> None:
    kinds = {m.kind for m in members}
    if len(kinds) > 1:
        raise ComparisonFileError(f"{title!r}: mixes IO and metadata sections")
    sweeps = {m.x for m in members}
    if len(sweeps) > 1:
        names = ", ".join(sorted(str(x or "single point") for x in sweeps))
        raise ComparisonFileError(
            f"{title!r}: sections sweep different dimensions ({names})"
        )
    if len({m.ctx.normalize_to for m in members}) > 1:
        raise ComparisonFileError(
            f"{title!r}: sections are normalized to different node counts"
        )
    if len({m.anchor for m in members}) < len(members):
        raise ComparisonFileError(f"{title!r}: lists one section twice")


def forced_sets(
    entries: Sequence[dict],
    documents: Sequence[LoadedDocument],
    found: list[Candidate],
) -> list[CompareSet]:
    """Sets named by a validated comparison file, in file order."""
    sets = []
    for index, entry in enumerate(entries, 1):
        title = entry["title"].strip()
        try:
            members = [
                resolve_reference(r, documents, found) for r in entry["sections"]
            ]
            _check_forced(title, members)
        except ComparisonFileError as error:
            raise _fail(index, str(error)) from None
        sets.extend(split(members, None, title))
    return sets


def comparison_sets(
    documents: Sequence[LoadedDocument],
    contexts: Sequence[tuple[GroupContext, list[dict]]],
    comparisons: Any = None,
) -> list[CompareSet]:
    """Forced sets (file order) then automatic ones not already forced."""
    found = candidates(contexts)
    forced = forced_sets(check_comparisons(comparisons), documents, found)
    seen = {frozenset(m.anchor for m in s.members) for s in forced}
    automatic = [
        s
        for s in automatic_sets(found)
        if frozenset(m.anchor for m in s.members) not in seen
    ]
    return forced + automatic
