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

"""Which recorded sweep settings (``env_used.yaml``) apply to a run.

Single source of truth for setting relevance; report code hides, and
comparison pairing ignores, settings that do not apply. Classes:

- ``IGNORED``: provenance, reference limits and live-capture options; they
  cannot affect results.
- ``CONDITIONS``: settings that apply only when every listed condition holds,
  e.g. single-big-file size only with a single big file, writer file sizes
  only when the run generates its data (not read-from runs), the read pause
  only with write and read phases, Kubernetes settings only on kubectl.
- ``SWEPT_LISTS``: the value list of the swept x dimension (``nodes_spec``
  when x is nodes) does not apply to one point; ignored when the context
  names that dimension.
- Everything else, unknown keys included, always applies.

Flags that select a mode (``ELBENCHO_SINGLE_BIG_FILE``, ``sweep_read_from``,
``run_to_completion_option``) always apply. Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from lib.filesystem_coordinates import (
    KIND_IO,
    KIND_MDTEST,
    PLAN_READ_FROM,
    PLAN_WRITE_NO_READ,
    PLAN_WRITE_ONLY,
    PLAN_WRITE_READ,
)

_TRUE = {"1", "true", "yes"}


@dataclass(frozen=True)
class RunContext:
    """What decides relevance: kind (``io``/``mdtest``), substrate, IO phase
    plan, single big file, run to completion, and the swept x coordinate
    (``nodes``, ``threads_per_node``, ``io_depth``, ``tasks_per_node``) when
    known."""

    kind: str = KIND_IO
    substrate: str = ""
    phase_plan: str = PLAN_WRITE_READ
    single_big_file: bool = False
    run_to_completion: bool = False
    swept: str | None = None


def _flag(value: Any) -> bool:
    return str(value).strip().lower() in _TRUE


def _plan(settings: Mapping) -> str:
    if _flag(settings.get("sweep_write_only")):
        return PLAN_WRITE_ONLY
    if _flag(settings.get("sweep_write_no_read")):
        return PLAN_WRITE_NO_READ
    if str(settings.get("sweep_read_from") or "").strip():
        return PLAN_READ_FROM
    return PLAN_WRITE_READ


def _coordinate(coordinates: Iterable[Mapping], name: str) -> Any:
    """The first recorded value of a coordinate, else None."""
    return next((c[name] for c in coordinates if c.get(name) is not None), None)


def run_context(
    kind: str | None,
    settings: Mapping | None,
    coordinates: Iterable[Mapping | None] = (),
    swept: str | None = None,
) -> RunContext:
    """A run's context from its group kind and settings; execution
    ``coordinates``, when given, take precedence over the settings."""
    settings = settings or {}
    coords = [c for c in coordinates if c]
    plan = _coordinate(coords, "phase_plan") or _plan(settings)
    big = _coordinate(coords, "single_big_file")
    if big is None:
        big = _flag(settings.get("ELBENCHO_SINGLE_BIG_FILE"))
    complete = _coordinate(coords, "run_to_completion")
    if complete is None:
        complete = _flag(settings.get("run_to_completion_option"))
    return RunContext(
        kind=KIND_MDTEST if kind == KIND_MDTEST else KIND_IO,
        substrate=str(settings.get("EXECUTION_SUBSTRATE") or "").strip(),
        phase_plan=plan,
        single_big_file=bool(big),
        run_to_completion=bool(complete),
        swept=swept,
    )


_Condition = Callable[[RunContext, Mapping], bool]


def _generates_data(context: RunContext, _settings: Mapping) -> bool:
    return context.kind == KIND_IO and context.phase_plan != PLAN_READ_FROM


def _single_big_file(context: RunContext, _settings: Mapping) -> bool:
    return context.single_big_file


def _many_files(context: RunContext, _settings: Mapping) -> bool:
    return not context.single_big_file


def _no_explicit_size(_context: RunContext, settings: Mapping) -> bool:
    return not str(settings.get("ELBENCHO_FILE_SIZE") or "").strip()


def _writes_then_reads(context: RunContext, _settings: Mapping) -> bool:
    # mdtest pauses before its stat phase.
    return context.kind == KIND_MDTEST or context.phase_plan == PLAN_WRITE_READ


def _reads_from(context: RunContext, _settings: Mapping) -> bool:
    return context.phase_plan == PLAN_READ_FROM


def _time_limited(context: RunContext, _settings: Mapping) -> bool:
    return context.kind == KIND_IO and not context.run_to_completion


def _kubectl(context: RunContext, _settings: Mapping) -> bool:
    return context.substrate == "kubectl"


IGNORED = frozenset(
    {
        "batch",
        "env_override",
        "target_file",
        "treefile_cache",
        "FS_MAX_AGG_THROUGHPUT",
        "FS_MAX_NODE_THROUGHPUT_GBPS",
        "FS_MAX_NODE_IOPS",
        "ELBENCHO_LIVEINT",
        "ELBENCHO_LIVE_CSV_EXTENDED",
        "KUBECTL_IMAGE_PULL_POLICY",
    }
)
_GENERATED_FILES = (_generates_data, _many_files)
_GENERATED_BIG_FILE = (_generates_data, _single_big_file)
CONDITIONS: dict[str, tuple[_Condition, ...]] = {
    "ELBENCHO_FILE_SIZE": _GENERATED_FILES,
    "ELBENCHO_FILE_SIZE_MULTIPLIER": _GENERATED_FILES + (_no_explicit_size,),
    "ELBENCHO_FILES_PER_NODE": _GENERATED_FILES,
    "ELBENCHO_SINGLE_BIG_FILE_SIZE": _GENERATED_BIG_FILE,
    "ELBENCHO_SINGLE_BIG_FILE_BASENAME": _GENERATED_BIG_FILE,
    "ELBENCHO_READ_AFTER_WRITE_PAUSE": (_writes_then_reads,),
    "ELBENCHO_SCALE_READ_WRITE_DURATION": (_time_limited,),
    "KUBECTL_NAMESPACE": (_kubectl,),
    "KUBECTL_NODE_SELECTOR": (_kubectl,),
    "KUBECTL_ELBENCHO_IMAGE": (_kubectl,),
    "KUBECTL_RUN_AS_USER": (_kubectl,),
    "KUBECTL_RUN_AS_GROUP": (_kubectl,),
    "KUBECTL_MAPPED_TEST_DIRS": (_kubectl,),
    "KUBECTL_MAPPED_READ_FROM": (_kubectl, _reads_from),
}
SWEPT_LISTS = {
    "nodes": "nodes_spec",
    "threads_per_node": "ELBENCHO_SCALE_THREAD_LIST",
    "io_depth": "ELBENCHO_IODEPTH_LIST",
    "tasks_per_node": "tasks_spec",
}


def is_relevant(key: str, settings: Mapping | None, context: RunContext) -> bool:
    """Whether setting ``key`` of ``settings`` applies to a run in ``context``."""
    if key in IGNORED or key == SWEPT_LISTS.get(context.swept or ""):
        return False
    settings = settings or {}
    return all(check(context, settings) for check in CONDITIONS.get(key, ()))


def relevant_settings(settings: Mapping | None, context: RunContext) -> dict:
    """The settings that apply to a run in ``context``."""
    settings = settings or {}
    return {
        key: value
        for key, value in settings.items()
        if is_relevant(str(key), settings, context)
    }
