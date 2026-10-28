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

"""One process pool for the expensive report work (live CSVs and plots).

A batch submits every group's tasks up front, so the pool stays full across
group boundaries while each group's output is still printed in group order.
Workers load the elbencho reporter under ENGINE_MODULE, the name its tasks
pickle as.
"""

from __future__ import annotations

from concurrent.futures import Executor, Future, ProcessPoolExecutor
import contextlib
import importlib.util
import io
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import Any, Callable

MAX_REPORT_WORKERS = 8
# Peak RSS of a report process is matplotlib, not live CSV size (the analysis
# streams and the per-client plots are capped): 544 MiB measured for a
# 130-client, 12-execution group; this budget leaves headroom over that.
REPORT_WORKER_BYTES = 768 << 20
ENGINE_MODULE = "extract_elbencho"
_PROC_CGROUP = Path("/proc/self/cgroup")
_CGROUP_ROOT = Path("/sys/fs/cgroup")
# cgroup v1 reports "no limit" as a huge page-rounded number.
_CGROUP_V1_UNLIMITED = 1 << 60
_ENGINE_PATH = Path(__file__).resolve().parent.parent / "utils" / "extract-elbencho.py"


def load_engine() -> ModuleType:
    """The elbencho reporter, imported once as ENGINE_MODULE."""
    loaded = sys.modules.get(ENGINE_MODULE)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(ENGINE_MODULE, _ENGINE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[ENGINE_MODULE] = module
    spec.loader.exec_module(module)
    return module


def _available_memory_bytes() -> int | None:
    """MemAvailable from /proc/meminfo, or None where it is unknown."""
    try:
        with open("/proc/meminfo", encoding="ascii") as meminfo:
            for line in meminfo:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def _cgroup_headroom(limit_file: Path, usage_file: Path) -> int | None:
    """Limit minus usage for one cgroup, or None when it has no limit."""
    try:
        limit = limit_file.read_text(encoding="ascii").strip()
        if limit == "max" or int(limit) >= _CGROUP_V1_UNLIMITED:
            return None
        return max(0, int(limit) - int(usage_file.read_text(encoding="ascii")))
    except (OSError, ValueError):
        return None


def _cgroup_memory_bytes() -> int | None:
    """Memory this process's cgroups still allow, or None when unlimited.

    Limits on any ancestor cgroup (a container or a systemd slice) count.
    """
    try:
        lines = _PROC_CGROUP.read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    headrooms = []
    for line in lines:
        _, controllers, path = line.split(":", 2)
        if not controllers:
            root, files = _CGROUP_ROOT, ("memory.max", "memory.current")
        elif "memory" in controllers.split(","):
            root = _CGROUP_ROOT / "memory"
            files = ("memory.limit_in_bytes", "memory.usage_in_bytes")
        else:
            continue
        directory = root / path.lstrip("/")
        for candidate in (directory, *directory.parents):
            headroom = _cgroup_headroom(candidate / files[0], candidate / files[1])
            if headroom is not None:
                headrooms.append(headroom)
            if candidate == root:
                break
    return min(headrooms, default=None)


def report_workers() -> int:
    """Worker processes: CPUs and available memory permitting."""
    try:
        cpus = len(os.sched_getaffinity(0))
    except AttributeError:
        cpus = os.cpu_count() or 1
    workers = min(cpus, MAX_REPORT_WORKERS)
    known = [
        memory
        for memory in (_available_memory_bytes(), _cgroup_memory_bytes())
        if memory is not None
    ]
    if known:
        workers = min(workers, min(known) // REPORT_WORKER_BYTES)
    return max(1, workers)


class InlineExecutor(Executor):
    """Runs each task when submitted; used when only one worker fits."""

    def submit(self, fn, /, *args, **kwargs) -> Future:
        future: Future = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except Exception as error:  # pylint: disable=broad-exception-caught
            future.set_exception(error)
        return future


def report_pool() -> Executor:
    """The pool for one report run; workers start only as tasks arrive."""
    workers = report_workers()
    if workers == 1:
        return InlineExecutor()
    return ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=load_engine,
    )


def captured(function: Callable[..., Any], *args: Any) -> tuple[Any, str, str]:
    """Run a task, returning its result with the stdout and stderr it wrote."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = function(*args)
    return result, stdout.getvalue(), stderr.getvalue()


def replay(future: Future) -> Any:
    """Wait for a captured task, write its output here, and return its result."""
    result, stdout, stderr = future.result()
    sys.stdout.write(stdout)
    sys.stderr.write(stderr)
    return result


def run_command(command: list[str]) -> subprocess.CompletedProcess:
    """Run a reporter subprocess, capturing its output."""
    return subprocess.run(command, check=False, capture_output=True, text=True)
