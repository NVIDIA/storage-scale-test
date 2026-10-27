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

"""Execution coordinates and coordinate keys from execution definitions.

Pure functions that turn one ``executions/NNNN.sh`` definition (plus the
group's ``env_used.yaml`` settings) into the ``coordinates`` object and the
``coordinate_key`` of the machine-readable results document. The IO and
metadata extractors use them for executions with results; the assembler uses
them for executions that have no fragment entry (failed, pending, not run).

This module must stay import-light (standard library only): it is imported by
code that runs without PyYAML.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import shlex
from typing import Any, Mapping

KIND_IO = "io"
KIND_MDTEST = "mdtest"

PHASE_WRITE = "write"
PHASE_READ = "read"

PLAN_WRITE_READ = "write_read"
PLAN_WRITE_ONLY = "write_only"
PLAN_WRITE_NO_READ = "write_no_read"
PLAN_READ_FROM = "read_from"

ACCESS_RANDOM = "random"
ACCESS_SEQUENTIAL = "sequential"
LAYOUT_DEFAULT = "worker-directories"
ENV_SWEEP_READ_FROM = "ELBENCHO_SWEEP_READ_FROM"

_COORDS_RE = re.compile(
    r"^# Auto-generated; do not edit\. Coords: "
    r"nodes=(\d+) io_size=(\S+) thread_count=(\d+) io_depth=(\d+)$",
    re.MULTILINE,
)
_LAYOUT_RE = re.compile(
    r"^export ELBENCHO_FILE_LAYOUT=(['\"]?)([a-z-]+)\1$", re.MULTILINE
)
_EXPORT_LINE_RE = re.compile(r"^export ([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.MULTILINE)
_MDTEST_KIND_RE = re.compile(r"^export ELBENCHO_EXECUTION_KIND=mdtest$", re.MULTILINE)
_SIZE_UNIT_SHIFTS = {"K": 10, "M": 20, "G": 30, "T": 40, "P": 50, "E": 60}


@dataclass(frozen=True)
class IoDefinition:
    """The coordinates and exports captured in one IO execution definition."""

    nodes: int
    io_size: str
    threads: int
    io_depth: int
    configured_layout: str
    exports: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class MdtestDefinition:
    """The coordinates captured in one metadata execution definition."""

    nodes: int
    tasks: int
    iterations: int | None
    branch_factor: int | None
    items_per_dir: int | None


def ledger_exports(content: str) -> dict[str, str]:
    """Parse the shell ``export NAME=value`` lines of one NNNN.sh (last wins)."""
    exports: dict[str, str] = {}
    for name, raw in _EXPORT_LINE_RE.findall(content):
        try:
            parsed = shlex.split(raw)
        except ValueError:
            continue
        exports[name] = " ".join(parsed)
    return exports


def parse_io_definition(content: str) -> IoDefinition | None:
    """Read an IO execution definition, or None without its coordinates line."""
    match = _COORDS_RE.search(content)
    if not match:
        return None
    layout = _LAYOUT_RE.search(content)
    return IoDefinition(
        nodes=int(match.group(1)),
        io_size=match.group(2),
        threads=int(match.group(3)),
        io_depth=int(match.group(4)),
        configured_layout=layout.group(2) if layout else "",
        exports=ledger_exports(content),
    )


def elbencho_size_bytes(token: str) -> int | None:
    """Convert an elbencho block size such as 4K or 1M to bytes.

    Verified against tmp/elbencho-src/source/toolkits/UnitTk.cpp
    (numHumanToBytesBinary): the K/M/G/T/P/E suffixes are binary (powers of
    1024), case-insensitive, and a value without suffix is plain bytes.
    """
    match = re.fullmatch(r"([0-9]+)([KMGTPEkmgtpe]?)", token.strip())
    if not match:
        return None
    return int(match.group(1)) << _SIZE_UNIT_SHIFTS.get(match.group(2).upper(), 0)


def _split_size_spec(io_size: str, use_random: bool) -> tuple[tuple, tuple]:
    """Return ((token, random) for write, (token, random) for read)."""
    write_part, _, read_part = io_size.partition(",")
    read_part = read_part or write_part
    return tuple(  # type: ignore[return-value]
        (part.removeprefix("r"), use_random or part.startswith("r"))
        for part in (write_part, read_part)
    )


def export_flag(exports: Mapping[str, str], name: str) -> bool:
    """True when the exported flag is exactly 1."""
    return exports.get(name, "0") == "1"


def phase_plan(exports: Mapping[str, str]) -> str:
    """The phase plan an execution's exports select."""
    if export_flag(exports, "ELBENCHO_SWEEP_WRITE_ONLY"):
        return PLAN_WRITE_ONLY
    if export_flag(exports, "ELBENCHO_SWEEP_WRITE_NO_READ"):
        return PLAN_WRITE_NO_READ
    if exports.get(ENV_SWEEP_READ_FROM):
        return PLAN_READ_FROM
    return PLAN_WRITE_READ


def plan_runs_phase(plan: str, phase: str) -> bool:
    """Whether the plan runs the write or read phase."""
    if phase == PHASE_WRITE:
        return plan != PLAN_READ_FROM
    return plan in (PLAN_WRITE_READ, PLAN_READ_FROM)


def io_coordinates(definition: IoDefinition) -> tuple[dict, str]:
    """Build the coordinates object and coordinate_key of one IO execution."""
    exports = definition.exports
    plan = phase_plan(exports)
    direct = exports.get("dio_or_bio", "dio") == "dio"
    sides = dict(
        zip(
            (PHASE_WRITE, PHASE_READ),
            _split_size_spec(definition.io_size, export_flag(exports, "use_random")),
        )
    )
    values: dict[str, Any] = {}
    key_parts = []
    for phase, (token, is_random) in sides.items():
        if plan_runs_phase(plan, phase):
            values[f"{phase}_block_size_bytes"] = elbencho_size_bytes(token)
            values[f"{phase}_access"] = (
                ACCESS_RANDOM if is_random else ACCESS_SEQUENTIAL
            )
            key_parts.append(f"{token}-{'rand' if is_random else 'seq'}")
        else:
            values[f"{phase}_block_size_bytes"] = None
            values[f"{phase}_access"] = None
            key_parts.append("none")
    coordinates = {
        "nodes": definition.nodes,
        "threads_per_node": definition.threads,
        "io_depth": definition.io_depth,
        "io_size_spec": definition.io_size,
        "phase_plan": plan,
        "write_block_size_bytes": values["write_block_size_bytes"],
        "write_access": values["write_access"],
        "read_block_size_bytes": values["read_block_size_bytes"],
        "read_access": values["read_access"],
        "direct_io": direct,
        "run_to_completion": export_flag(exports, "run_to_completion"),
        "file_layout": exports.get("ELBENCHO_FILE_LAYOUT")
        or definition.configured_layout
        or LAYOUT_DEFAULT,
        "single_big_file": export_flag(exports, "ELBENCHO_SINGLE_BIG_FILE"),
        "all_nodes_all_data": export_flag(
            exports, "ELBENCHO_ALL_NODES_ACCESS_ALL_DATA"
        ),
    }
    key = (
        f"io/c{definition.nodes:03d}/s{definition.threads:03d}"
        f"/d{definition.io_depth:03d}"
        f"/w{key_parts[0]}/r{key_parts[1]}/{'dio' if direct else 'bio'}"
    )
    return coordinates, key


def optional_int(value: Any) -> int | None:
    """Return a non-negative integer parsed from text, else None."""
    text = str(value).strip() if value is not None else ""
    return int(text) if re.fullmatch(r"[0-9]+", text) else None


def _shell_export(content: str, name: str) -> str | None:
    """Return the last exported shell value of a name, or None when unusable."""
    values = re.findall(rf"^export {re.escape(name)}=(.*)$", content, re.MULTILINE)
    if not values:
        return None
    try:
        parsed = shlex.split(values[-1])
    except ValueError:
        return None
    return parsed[0] if len(parsed) == 1 else None


def _first_int(content: str, env: Mapping[str, Any], name: str) -> int | None:
    """Read a setting from the execution definition, else from env_used.yaml."""
    value = optional_int(_shell_export(content, name))
    return value if value is not None else optional_int(env.get(name))


def is_mdtest_definition(content: str) -> bool:
    """True when the execution definition belongs to a metadata workload."""
    return bool(_MDTEST_KIND_RE.search(content))


def parse_mdtest_definition(
    content: str, env: Mapping[str, Any]
) -> MdtestDefinition | None:
    """Read a metadata execution definition (None if its coordinates are invalid)."""
    nodes = optional_int(_shell_export(content, "nodes"))
    tasks = optional_int(_shell_export(content, "tasks_per_node"))
    if nodes is None or tasks is None:
        return None
    return MdtestDefinition(
        nodes,
        tasks,
        _first_int(content, env, "MDTEST_ITERATIONS"),
        _first_int(content, env, "MDTEST_BRANCH_FACTOR"),
        _first_int(content, env, "MDTEST_ITEMS_PER_DIR"),
    )


def mdtest_coordinates(
    definition: MdtestDefinition, found_iterations: int = 0
) -> tuple[dict, str]:
    """Build the coordinates object and coordinate_key of one metadata execution."""
    branch = definition.branch_factor
    items = definition.items_per_dir
    coordinates = {
        "nodes": definition.nodes,
        "tasks_per_node": definition.tasks,
        "branch_factor": branch,
        "items_per_dir": items,
        "iterations": definition.iterations or found_iterations,
    }
    key = (
        f"md/c{definition.nodes:03d}/t{definition.tasks:03d}"
        f"/b{'none' if branch is None else branch}"
        f"/i{'none' if items is None else items}"
    )
    return coordinates, key


def definition_coordinates(
    kind: str, content: str, env: Mapping[str, Any]
) -> tuple[dict, str] | None:
    """Coordinates and key from an execution definition, None when unparseable."""
    if kind == KIND_MDTEST:
        md_definition = parse_mdtest_definition(content, env)
        return None if md_definition is None else mdtest_coordinates(md_definition)
    io_definition = parse_io_definition(content)
    return None if io_definition is None else io_coordinates(io_definition)
