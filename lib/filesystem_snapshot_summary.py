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

UNKNOWN = "unknown"
NONE_RECORDED = "none recorded"
FIELD_SEPARATOR = "; "
READ_FROM_FIELD = "sweep_read_from"


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
                ("KUBECTL_PVC", "PVC"),
                ("KUBECTL_ELBENCHO_IMAGE", "image"),
            ),
        )
    return text


def _targets(snapshot: dict) -> str:
    text = _field(snapshot, "TEST_DIRS", "roots")
    if snapshot.get("KUBECTL_NODE_SELECTOR"):
        text += "; " + _field(snapshot, "KUBECTL_NODE_SELECTOR", "selector")
    if snapshot.get("KUBECTL_MAPPED_TEST_DIRS"):
        text += "; " + _field(snapshot, "KUBECTL_MAPPED_TEST_DIRS", "mapped_roots")
    return text


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


def _override(snapshot: dict) -> str:
    provenance = snapshot.get("env_override")
    if "env_override" in snapshot and provenance is None:
        return NONE_RECORDED
    if not isinstance(provenance, dict):
        return UNKNOWN
    filename = provenance.get("file")
    if filename is None:
        return UNKNOWN
    return _display(Path(str(filename)).name) if filename else NONE_RECORDED


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
        ("Targets", _targets(snapshot)),
        ("Sweep", sweep),
        ("Files/sizing", _files(snapshot)),
        ("Lifecycle", _lifecycle(snapshot)),
        ("Override", _override(snapshot)),
    )
    return "".join(f"{label}: {value}\n" for label, value in rows)
