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

"""Pure compact displays of the effective settings in saved IO snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from lib.storage_target import StorageTarget, storage_target_from_env

UNKNOWN = "unknown"
NONE_RECORDED = "none recorded"
FIELD_SEPARATOR = "; "
READ_FROM_FIELD = "sweep_read_from"
FILE_KEY = "file"
SETTING_FILES_LABEL = "Setting files"
TARGET_PREFIX = "target "
OVERRIDES_PREFIX = "overrides "
LINE_LIMIT = 132
ELLIPSIS = "\u2026"
TARGETS_LABEL = "Targets"
NAME_LIMIT = 48
ROOTS_FIELD = "TEST_DIRS"
MAPPED_FIELD = "KUBECTL_MAPPED_TEST_DIRS"


def _display(value: Any) -> str:
    """Keep paths and compound sweep coordinates unambiguous on one line."""
    if value is None:
        return UNKNOWN
    if isinstance(value, dict):
        return (
            "{"
            + ", ".join(
                f"{_display(path)} (weight={_display(weight)})"
                for path, weight in value.items()
            )
            + "}"
        )
    if isinstance(value, list):
        return "[" + ", ".join(_display(item) for item in value) + "]"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _field(
    snapshot: dict, key: str, label: str, unit: str = "", empty: str = ""
) -> str:
    value = snapshot.get(key)
    rendered = empty if value == "" and empty else _display(value)
    suffix = f" {unit}" if unit and value is not None else ""
    return f"{label}={rendered}{suffix}"


def _fields(snapshot: dict, specifications: tuple) -> str:
    return FIELD_SEPARATOR.join(_field(snapshot, *spec) for spec in specifications)


def _active(value: Any) -> bool:
    return value in (1, "1", True)


def _runner(snapshot: dict) -> str:
    text = _field(snapshot, "EXECUTION_SUBSTRATE", "substrate")
    if _active(snapshot.get("ORDER_NODES")):
        text += "; node_order=1"
    if snapshot.get("EXECUTION_SUBSTRATE") == "kubectl":
        text += "; " + _fields(
            snapshot,
            (
                ("KUBECTL_NAMESPACE", "namespace"),
                ("KUBECTL_ELBENCHO_IMAGE", "image"),
            ),
        )
    return text


def _display_fit(value: str, limit: int) -> str:
    """Quote a string like ``_display``, shortening its text with an ellipsis."""
    text = _display(value)
    keep = len(value)
    while len(text) > limit and keep > 0:
        keep -= 1
        text = _display(value[:keep] + ELLIPSIS)
    return text


def _target_name(target: StorageTarget) -> str:
    """Quote a known name, marking one derived rather than recorded."""
    if target.name is None:
        return f"name={UNKNOWN}"
    suffix = "" if target.recorded else " (derived)"
    return f"name={_display_fit(target.name, NAME_LIMIT)}{suffix}"


def _fit_mapping(snapshot: dict, key: str, label: str, limit: int) -> str:
    """A path-weight field, ending "(+K more)" so it fits within limit when listed."""
    value = snapshot.get(key)
    if not isinstance(value, dict) or len(value) < 2:
        return _field(snapshot, key, label)
    entries = [_display({path: weight}) for path, weight in value.items()]
    for shown in range(len(entries), 0, -1):
        text = f"{label}={{" + ", ".join(entry[1:-1] for entry in entries[:shown]) + "}"
        if shown < len(entries):
            text += f" (+{len(entries) - shown} more)"
        if len(text) <= limit or shown == 1:
            return text
    return f"{label}={UNKNOWN}"


def _targets(snapshot: dict) -> str:
    """Name, roots and optional selectors; the path lists shrink to fit the line."""
    parts = [_target_name(storage_target_from_env(snapshot))]
    if snapshot.get("KUBECTL_NODE_SELECTOR"):
        parts.append(_field(snapshot, "KUBECTL_NODE_SELECTOR", "selector"))
    budget = LINE_LIMIT - len(f"{TARGETS_LABEL}: ") - len(FIELD_SEPARATOR.join(parts))
    budget -= len(FIELD_SEPARATOR)
    roots = _field(snapshot, ROOTS_FIELD, "roots")
    if not snapshot.get(MAPPED_FIELD):
        parts.insert(1, _fit_mapping(snapshot, ROOTS_FIELD, "roots", budget))
        return FIELD_SEPARATOR.join(parts)
    budget -= len(FIELD_SEPARATOR)
    mapped = _fit_mapping(
        snapshot, MAPPED_FIELD, "mapped_roots", max(budget // 2, budget - len(roots))
    )
    parts.insert(1, _fit_mapping(snapshot, ROOTS_FIELD, "roots", budget - len(mapped)))
    parts.append(mapped)
    return FIELD_SEPARATOR.join(parts)


def _files(snapshot: dict) -> str:
    if snapshot.get(READ_FROM_FIELD):
        mode = (
            "single-big-file"
            if _active(snapshot.get("ELBENCHO_SINGLE_BIG_FILE"))
            else "existing-data"
        )
        text = mode + FIELD_SEPARATOR + _field(snapshot, READ_FROM_FIELD, "read_from")
        if _active(snapshot.get("ELBENCHO_ALL_NODES_ACCESS_ALL_DATA")):
            text += "; all_nodes_all_data=1"
        return text
    if _active(snapshot.get("ELBENCHO_SINGLE_BIG_FILE")):
        text = "single-big-file; " + _fields(
            snapshot,
            (
                ("ELBENCHO_SINGLE_BIG_FILE_BASENAME", "name"),
                ("ELBENCHO_SINGLE_BIG_FILE_SIZE", "size", "", "unspecified"),
            ),
        )
    else:
        text = _fields(
            snapshot,
            (
                ("ELBENCHO_FILE_LAYOUT", "layout"),
                ("ELBENCHO_FILES_PER_NODE", "files/node", "", "auto"),
            ),
        )
        size = snapshot.get("ELBENCHO_FILE_SIZE")
        if size:
            text += "; " + _field(snapshot, "ELBENCHO_FILE_SIZE", "file_size")
        elif size is None:
            text += "; file_size=unknown"
        else:
            text += "; " + _field(
                snapshot, "ELBENCHO_FILE_SIZE_MULTIPLIER", "size_multiplier"
            )
    if _active(snapshot.get("ELBENCHO_ALL_NODES_ACCESS_ALL_DATA")):
        text += "; all_nodes_all_data=1"
    return (
        text
        + "; hints: "
        + _fields(
            snapshot,
            (
                ("FS_MAX_AGG_THROUGHPUT", "aggregate", "GB/s"),
                ("FS_MAX_NODE_THROUGHPUT_GBPS", "node", "Gbps"),
                ("FS_MAX_NODE_IOPS", "node", "IOPS"),
            ),
        )
    )


def _phase(snapshot: dict) -> str:
    if snapshot.get(READ_FROM_FIELD):
        return "read-from"
    if _active(snapshot.get("sweep_write_only")):
        return "write-only"
    if _active(snapshot.get("sweep_write_no_read")):
        return "write-no-read"
    if any(
        snapshot.get(key) is None for key in ("sweep_write_only", "sweep_write_no_read")
    ):
        return UNKNOWN
    return "write/read"


def _lifecycle(snapshot: dict) -> str:
    parts = [
        "phases=" + _phase(snapshot),
        _field(
            snapshot, "ELBENCHO_SCALE_READ_WRITE_DURATION", "configured_duration", "s"
        ),
    ]
    if _active(snapshot.get("run_to_completion_option")):
        parts.append("run_to_completion=1")
    pause = snapshot.get("ELBENCHO_READ_AFTER_WRITE_PAUSE")
    if pause not in (None, "", 0, "0"):
        parts.append(
            _field(snapshot, "ELBENCHO_READ_AFTER_WRITE_PAUSE", "read_pause", "s")
        )
    if _active(snapshot.get("ELBENCHO_LIVE_CSV_EXTENDED")):
        parts.append("extended_capture=1")
    interval = snapshot.get("ELBENCHO_LIVEINT")
    if interval not in (None, "", 1000, "1000"):
        parts.append(_field(snapshot, "ELBENCHO_LIVEINT", "capture_interval", "ms"))
    if snapshot.get("KUBECTL_MAPPED_READ_FROM"):
        parts.append(_field(snapshot, "KUBECTL_MAPPED_READ_FROM", "mapped_read_from"))
    return FIELD_SEPARATOR.join(parts)


def _override_basenames(provenance: dict) -> list[str]:
    """Plain basenames of the applied override files, in application order."""
    files = provenance.get("files")
    if not isinstance(files, list):
        return []
    return [
        Path(str(entry[FILE_KEY])).name
        for entry in files
        if isinstance(entry, dict) and entry.get(FILE_KEY)
    ]


def _fit_names(names: list[str], limit: int) -> str:
    """Quote and join names, ending with "(+K more)", within limit characters.

    Whole names are dropped from the end first; if even the first name does
    not fit with its count, that name is shortened with an ellipsis inside
    its quotes.
    """
    for shown in range(len(names), 0, -1):
        suffix = f" (+{len(names) - shown} more)" if shown < len(names) else ""
        text = ", ".join(_display(name) for name in names[:shown]) + suffix
        if len(text) <= limit:
            return text
    suffix = f" (+{len(names) - 1} more)" if len(names) > 1 else ""
    return _display_fit(names[0], limit - len(suffix)) + suffix


def _override(snapshot: dict, budget: int) -> str:
    """The workload override files: whole names, "(+K more)" within budget."""
    provenance = snapshot.get("env_override")
    if "env_override" in snapshot and provenance is None:
        return NONE_RECORDED
    if not isinstance(provenance, dict):
        return UNKNOWN
    names = _override_basenames(provenance)
    if names:
        return _fit_names(names, budget)
    filename = provenance.get(FILE_KEY)
    if filename is None:
        return UNKNOWN
    if not filename:
        return NONE_RECORDED
    return _fit_names([Path(str(filename)).name], budget)


def _target_file(snapshot: dict) -> str:
    """The quoted basename of the --target file, if one was used."""
    provenance = snapshot.get("target_file")
    if "target_file" in snapshot and provenance is None:
        return NONE_RECORDED
    if not isinstance(provenance, dict) or not provenance.get(FILE_KEY):
        return UNKNOWN
    return _display_fit(Path(str(provenance[FILE_KEY])).name, NAME_LIMIT)


def _setting_files(snapshot: dict) -> str:
    """The target file, then the override files, each by file name."""
    target = TARGET_PREFIX + _target_file(snapshot)
    budget = LINE_LIMIT - len(f"{SETTING_FILES_LABEL}: {target}{FIELD_SEPARATOR}")
    budget -= len(OVERRIDES_PREFIX)
    return target + FIELD_SEPARATOR + OVERRIDES_PREFIX + _override(snapshot, budget)


def _row(label: str, value: str) -> str:
    """One summary line, shortened with an ellipsis to the line limit."""
    line = f"{label}: {value}"
    if len(line) > LINE_LIMIT:
        line = line[: LINE_LIMIT - 1] + ELLIPSIS
    return line + "\n"


def format_io_snapshot_summary(snapshot: dict) -> str:
    """Describe the snapshot in six lines without supplying execution defaults."""
    sweep = _fields(
        snapshot,
        (
            ("nodes_spec", "nodes"),
            ("dio_or_bio", "IO_mode"),
            ("ELBENCHO_SCALE_IO_SIZES", "IO_sizes"),
            ("ELBENCHO_SCALE_THREAD_LIST", "threads"),
            ("ELBENCHO_IODEPTH_LIST", "IO_depths"),
        ),
    )
    if _active(snapshot.get("rand_option")):
        sweep += "; random_flag=1"
    rows = (
        ("Runner", _runner(snapshot)),
        (TARGETS_LABEL, _targets(snapshot)),
        ("Sweep", sweep),
        ("Files/sizing", _files(snapshot)),
        ("Lifecycle", _lifecycle(snapshot)),
        (SETTING_FILES_LABEL, _setting_files(snapshot)),
    )
    return "".join(_row(label, value) for label, value in rows)
