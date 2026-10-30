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

"""Helpers shared by the report section builders: units, sweeps, chart specs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, NamedTuple, Sequence

from lib.elbencho_live_steady import STEADY_REASON_TEXT, SteadyWindow
from lib.filesystem_report_options import LIVE_DETAIL_LEVELS
from lib.report_html.model import CATEGORICAL, slug
from lib.reporting_common import natural_key
from lib.results_document import (
    LoadedDocument,
    effective_live_detail,
    recorded_options,
)
from lib.setting_relevance import is_relevant, run_context

# Keeps an execution: ``keep(group, execution)``.
KeepExecution = Callable[[dict, dict], bool]
LIVE_NONE = LIVE_DETAIL_LEVELS[0]
LIVE_FULL = LIVE_DETAIL_LEVELS[-1]

GB = 1e9
MB = 1e6
GIB = float(1 << 30)
MS_PER_S = 1000.0
US_PER_MS = 1000.0
IO_PHASES = ("write", "read")
GBS = "GB/s"
FMT_GBS = "gbs"
FMT_MS = "ms"
FMT_NUM = "num"
LINEAR = "linear"
LOG = "log"
CATEGORY = "category"
ELAPSED_S = "elapsed (s)"
LATENCY_MS = "latency (ms)"
NO_VALUE = "–"
MIN = "min"
MAX = "max"
P50_KEY = "p50"
P99_KEY = "p99"
PHASE_WRITE = "write"
# Results document keys used throughout.
K_COORDS = "coordinates"
K_NODES = "nodes"
K_THREADS = "threads_per_node"
K_DEPTH = "io_depth"
_QD = "qd"
K_PHASE = "phase"
K_MIX = "mix_type"
K_OCCURRENCE = "occurrence"
K_LIVE = "live"
K_CLIENT = "client"
# Peak record metrics.
K_GBS = "gbs"
K_IOPS = "iops"
K_OPS = "ops"

# Dash by lap through the categorical colours: (plain, alternate).
_LAP_DASHES = ((False, True), ("dot", "dashdot"))
# Configurations one chart tells apart by colour and dash.
MAX_CONFIGS = len(CATEGORICAL) * len(_LAP_DASHES)

# Swept dimensions in x-axis preference order: (coordinate, axis label, suffix).
IO_DIMS = (
    (K_NODES, K_NODES, "n"),
    (K_THREADS, "threads/node", "t"),
    (K_DEPTH, "IO depth", _QD),
)
MD_DIMS = ((K_NODES, K_NODES, "n"), ("tasks_per_node", "tasks/node", "t"))

_SIZE_UNITS = ((1 << 40, "TiB", "t"), (1 << 30, "GiB", "g"), (1 << 20, "MiB", "m"))
_SIZE_UNITS += ((1 << 10, "KiB", "k"),)

# Group settings shown in each section, in display order.
SETTING_LABELS = (
    ("Target", "STORAGE_TARGET_NAME"),
    ("Substrate", "EXECUTION_SUBSTRATE"),
    ("Nodes", "nodes_spec"),
    ("Threads/node", "ELBENCHO_SCALE_THREAD_LIST"),
    ("Tasks/node", "tasks_spec"),
    ("IO sizes", "ELBENCHO_SCALE_IO_SIZES"),
    ("IO depths", "ELBENCHO_IODEPTH_LIST"),
    ("IO mode", "dio_or_bio"),
    ("Read from", "sweep_read_from"),
    ("File layout", "ELBENCHO_FILE_LAYOUT"),
    ("Files/node", "ELBENCHO_FILES_PER_NODE"),
    ("File size", "ELBENCHO_FILE_SIZE"),
    ("Single big file size", "ELBENCHO_SINGLE_BIG_FILE_SIZE"),
    ("Duration (s)", "ELBENCHO_SCALE_READ_WRITE_DURATION"),
    ("Run to completion", "run_to_completion_option"),
    ("Branch factor", "MDTEST_BRANCH_FACTOR"),
    ("Items/dir", "MDTEST_ITEMS_PER_DIR"),
    ("Iterations", "MDTEST_ITERATIONS"),
    ("Test dirs", "TEST_DIRS"),
    ("Image", "KUBECTL_ELBENCHO_IMAGE"),
    ("Namespace", "KUBECTL_NAMESPACE"),
)


@dataclass(frozen=True)
class GroupContext:
    """One group of one document, as the section builders see it."""

    loaded: LoadedDocument
    document: int
    prefix: str
    group: dict
    live_detail: str = LIVE_FULL
    # Metadata rates are scaled to this many nodes (``--normalize-to``).
    normalize_to: int | None = None

    @property
    def group_id(self) -> str:
        """The group's ID."""
        return str(self.group.get("group_id") or "0000")

    @property
    def target(self) -> str:
        """The storage target name, or a placeholder."""
        return str((self.group.get("target") or {}).get("name") or "(unnamed)")

    def anchor(self, shape: str) -> str:
        """Section anchor for a shape of this group."""
        return f"{self.prefix}g{slug(self.group_id)}-{shape}"


def group_contexts(
    documents: Sequence[LoadedDocument],
    keep_execution: KeepExecution | None = None,
    live_detail: str | None = None,
    normalize_to: int | None = None,
) -> list[tuple[GroupContext, list[dict]]]:
    """Every group of every document, by group ID, with its executions.

    Anchors carry a ``d<N>-`` prefix only when there are several documents.
    ``keep_execution(group, execution)`` drops executions; a group it empties
    is left out, and so is a group without executions unless
    ``keep_execution(group, {})``. ``live_detail`` caps each document's live
    detail level. Metadata executions come with rates scaled to
    ``normalize_to`` nodes, else to each document's recorded value.
    """
    if live_detail is not None and live_detail not in LIVE_DETAIL_LEVELS:
        raise ValueError(
            f"live detail {live_detail!r} is not one of "
            f"{', '.join(LIVE_DETAIL_LEVELS)}"
        )
    several = len(documents) > 1
    contexts = []
    for index, loaded in enumerate(documents, 1):
        prefix = f"d{index}-" if several else ""
        level = effective_live_detail(loaded.data, live_detail)
        normalize = normalize_to or recorded_options(loaded.data).get("normalize_to")
        by_group: dict[str, list[dict]] = {}
        for execution in loaded.data.get("executions") or []:
            by_group.setdefault(str(execution.get("group_id")), []).append(execution)
        groups = loaded.data.get("groups") or []
        for group in sorted(groups, key=lambda g: str(g.get("group_id"))):
            md = group.get("kind") == "mdtest"
            ctx = GroupContext(
                loaded, index, prefix, group, level, normalize if md else None
            )
            executions = by_group.get(ctx.group_id, [])
            if ctx.normalize_to:
                executions = [normalized(e, ctx.normalize_to) for e in executions]
            if keep_execution is not None:
                kept = [e for e in executions if keep_execution(group, e)]
                # A group without executions stays only if the group is kept.
                if not kept and (executions or not keep_execution(group, {})):
                    continue
                executions = kept
            contexts.append((ctx, executions))
    return contexts


def normalized(execution: dict, nodes_to: int) -> dict:
    """A copy of a metadata execution whose aggregate rates are scaled to
    ``nodes_to`` nodes, as ``--normalize-to`` scales text (not latency)."""
    nodes = (execution.get(K_COORDS) or {}).get(K_NODES)
    aggregate = results_of(execution).get("aggregate")
    if not nodes or not aggregate:
        return execution
    scale = nodes_to / nodes
    phases = [
        {
            **phase,
            "ops_per_s": {
                key: value * scale if isinstance(value, (int, float)) else value
                for key, value in (phase.get("ops_per_s") or {}).items()
            },
        }
        for phase in aggregate.get("phases") or []
    ]
    results = {**execution["results"], "aggregate": {**aggregate, "phases": phases}}
    return {**execution, "results": results}


def normalized_text(nodes_to: int) -> str:
    """``normalized to 1 node``."""
    return f"normalized to {nodes_to} node{'' if nodes_to == 1 else 's'}"


def results_of(execution: dict) -> dict:
    """An execution's results, or an empty dict."""
    results = execution.get("results")
    return results if isinstance(results, dict) else {}


def phase_of(execution: dict, name: str) -> dict | None:
    """The named IO phase of an execution, if it ran."""
    for phase in results_of(execution).get("phases") or []:
        if phase.get("phase") == name:
            return phase
    return None


def steady_span(live: dict) -> tuple[int, int] | None:
    """The live object's usable steady window (start_ms, end_ms), or None.

    Usable exactly as in the text report (``SteadyWindow.available``): a
    window with a rejection reason, such as a too-short phase, is not one even
    when it records bounds.
    """
    steady = live.get("steady") or {}
    window = SteadyWindow(
        start_ms=steady.get("start_ms"),
        end_ms=steady.get("end_ms"),
        reason=steady.get("reason"),
    )
    if not window.available or window.end_ms is None:
        return None
    return window.start_ms, window.end_ms


def steady_missing_text(live: dict) -> str:
    """Why the live object has no usable steady window, in the text report's words."""
    reason = (live.get("steady") or {}).get("reason")
    return STEADY_REASON_TEXT.get(reason, reason or "unavailable")


def size_text(size: int | None) -> str:
    """Binary size for titles: ``1 MiB``."""
    if not size:
        return NO_VALUE
    for unit, name, _ in _SIZE_UNITS:
        if size >= unit and size % unit == 0:
            return f"{size // unit} {name}"
    return f"{size} B"


def size_slug(size: int | None) -> str:
    """Binary size for anchors: ``1m``."""
    if not size:
        return "none"
    for unit, _, suffix in _SIZE_UNITS:
        if size >= unit and size % unit == 0:
            return f"{size // unit}{suffix}"
    return f"{size}b"


def value_text(value: Any) -> str:
    """Setting value as display text."""
    if isinstance(value, dict):
        return "; ".join(f"{k} = {value_text(v)}" for k, v in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return ", ".join(value_text(v) for v in value)
    return "" if value is None else str(value)


def settings_rows(
    group: dict, executions: Sequence[dict] = ()
) -> list[tuple[str, str]]:
    """Display rows for the settings a reader most often needs, without
    those that do not apply to the group's runs."""
    settings = group.get("settings") or {}
    coords = [e.get(K_COORDS) for e in executions]
    context = run_context(group.get("kind"), settings, coords)
    rows = []
    for label, key in SETTING_LABELS:
        text = value_text(settings.get(key))
        if text and is_relevant(key, settings, context):
            rows.append((label, text))
    override = group.get("env_override")
    if override:
        rows.append(("Override", value_text(override)))
    return rows


def execution_ids(executions: Sequence[dict]) -> dict[int, str]:
    """Display ID per execution (by ``id()``); unmatched ones get ``uNN``."""
    ids = {}
    unmatched = 0
    for execution in executions:
        raw = execution.get("execution_id")
        if raw:
            ids[id(execution)] = str(raw)
        else:
            unmatched += 1
            ids[id(execution)] = f"u{unmatched:02d}"
    return ids


def sort_executions(executions: Iterable[dict]) -> list[dict]:
    """Order by coordinates, then ID, so output never depends on input order."""
    return sorted(
        executions,
        key=lambda e: (
            natural_key(str(e.get("coordinate_key") or "")),
            str(e.get("execution_id") or ""),
        ),
    )


def sweep_axes(coordinates: Sequence[dict], dims) -> tuple[str | None, list[str]]:
    """The x dimension (first that varies) and the other varying dimensions."""
    varying = [
        name for name, _, _ in dims if len({c.get(name) for c in coordinates}) > 1
    ]
    return (varying[0] if varying else None), varying[1:]


def dim_label(name: str | None, dims) -> str:
    """Axis label of a dimension."""
    return next((label for key, label, _ in dims if key == name), "configuration")


def combo_label(coords: dict, names: Sequence[str], dims) -> str:
    """Short label of the non-x dimensions: ``8t·qd16`` (``?`` if unknown)."""
    parts = []
    for key, _, suffix in dims:
        if key in names:
            value = coords.get(key)
            value = "?" if value is None else value
            parts.append(f"qd{value}" if suffix == _QD else f"{value}{suffix}")
    return "·".join(parts)


def none_last(values: Sequence) -> tuple:
    """Sort key for tuples that may hold None (older documents): None last."""
    return tuple((value is None, 0 if value is None else value) for value in values)


def config_label(coords: dict, dims) -> str:
    """Short label of every dimension: ``4n·8t·qd16``."""
    return combo_label(coords, [key for key, _, _ in dims], dims)


def node_title(coordinates: Sequence[dict]) -> str:
    """``single node`` or ``N nodes`` when every execution shares a node
    count, else empty."""
    counts = {coords.get(K_NODES) for coords in coordinates}
    if len(counts) != 1 or None in counts:
        return ""
    count = counts.pop()
    return "single node" if count == 1 else f"{count} nodes"


def drop_column(columns: list, rows: list[list], label: str) -> None:
    """Remove the column labelled ``label`` and its cells, in place."""
    index = next(i for i, column in enumerate(columns) if column.label == label)
    del columns[index]
    for row in rows:
        del row[index]


def axis_type(values: Iterable) -> str:
    """Log only for roughly geometric sweeps (1, 2, 4, ... or 1, 32, 64, ...)."""
    ordered = sorted({v for v in values if isinstance(v, (int, float)) and v > 0})
    if len(ordered) < 3 or ordered[-1] / ordered[0] < 8:
        return LINEAR
    ratios = [b / a for a, b in zip(ordered, ordered[1:])]
    return LOG if sum(ratios) / len(ratios) >= 1.5 else LINEAR


def lap_dash(index: int, alternate: bool = False) -> bool | str:
    """Line dash for categorical series ``index``: colours repeat every
    ``len(CATEGORICAL)`` series, so each repeat takes a different dash
    (``alternate`` picks the second dash of the lap, e.g. write or p50).
    Charts hold at most ``MAX_CONFIGS`` series (see ``config_parts``)."""
    lap = min(index // len(CATEGORICAL), len(_LAP_DASHES) - 1)
    return _LAP_DASHES[lap][alternate]


class Part(NamedTuple):
    """One chart of a configuration list: its ``indices`` into the list, the
    anchor ``tail`` and the title ``suffix`` ("" unless the list was split)."""

    indices: range
    tail: str = ""
    suffix: str = ""


WHOLE = Part(range(0))


def config_parts(count: int) -> list[Part]:
    """Split ``count`` configurations into balanced charts of at most
    ``MAX_CONFIGS`` (24 -> 12 + 12); a list that fits is one unmarked part.
    A part numbers its own series from 0, so colours match across charts."""
    parts = max(1, -(-count // MAX_CONFIGS))
    if parts == 1:
        return [Part(range(count))]
    size, extra = divmod(count, parts)
    result, start = [], 0
    for number in range(1, parts + 1):
        stop = start + size + (number <= extra)
        result.append(
            Part(range(start, stop), f"-part{number}", f" (part {number} of {parts})")
        )
        start = stop
    return result


def series(name: str, color: str, points: list, dash: bool | str = False, y_axis="y"):
    """One line series of [x, y] points, on axis ``y`` or ``y2``.

    ``dash`` is False (solid), True (dashed), ``"dot"`` or ``"dashdot"``."""
    return {
        "name": name,
        "color": color,
        "dash": dash,
        "axis": y_axis,
        "points": points,
    }


def sweep_axis(label: str, kind: str) -> dict:
    """A swept-dimension x axis; its values are counts, so ticks are integers."""
    return {"label": label, "type": kind, "fmt": FMT_NUM, "integer": True}


def axis(label: str, kind: str = LINEAR, fmt: str = FMT_NUM, **limits) -> dict:
    """An axis description for a chart spec."""
    spec = {"label": label, "type": kind, "fmt": fmt}
    spec.update({k: v for k, v in limits.items() if v is not None})
    return spec


def line_spec(x: dict, y: dict, line_series: list, **extra) -> dict:
    """A line chart spec."""
    spec = {"kind": "line", "x": x, "y": y, "series": line_series}
    spec.update({k: v for k, v in extra.items() if v is not None})
    return spec


def bar_spec(categories: list[str], x_label: str, y: dict, bars: list) -> dict:
    """A bar chart spec; each bar series has one value per category."""
    return {
        "kind": "bar",
        "x": {"label": x_label, "type": CATEGORY, "categories": categories},
        "y": y,
        "series": bars,
    }


def has_points(chart_series: list) -> bool:
    """Whether any series has a value to draw."""
    for item in chart_series:
        if item.get("points"):
            return True
        if any(value is not None for value in item.get("values") or []):
            return True
    return False
