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

"""Project versions shown in reports: those that produced the results, and ours.

Benchmark runs record their version beside each native result file as
``<name>.out.project-version``. Versions stay out of metric models, so metrics
loaded from reporter CSV or Warp JSON caches report an unknown version.
"""

from pathlib import Path
import re
from typing import Callable, Iterable, List

from lib.project_version import UNKNOWN, project_version

VERSION_SUFFIX = ".project-version"
_CACHE_SUFFIXES = (".json.zst", ".json")
# Display guard only; lib/project_version.sh defines version syntax.
_DISPLAYABLE = re.compile(r"[0-9A-Za-z.+-]{1,128}")


def result_version(result_file) -> str:
    """Return the version recorded beside a native result file."""
    path = Path(result_file)
    name = path.name
    for suffix in _CACHE_SUFFIXES:
        if name.endswith(suffix):
            name = name.removesuffix(suffix) + ".out"
            break
    try:
        with path.with_name(name + VERSION_SUFFIX).open(encoding="utf-8") as stream:
            value = stream.read(256).strip()
    except (OSError, UnicodeError):
        return UNKNOWN
    return value if _DISPLAYABLE.fullmatch(value) else UNKNOWN


class ResultVersions:
    """Remember the recorded version of each parsed metric object."""

    def __init__(self) -> None:
        self._by_id: dict = {}

    def add(self, metrics: Iterable, version: str) -> None:
        """Associate metrics parsed from one result file with its version."""
        for metric in metrics:
            self._by_id[id(metric)] = (metric, version)

    def of(self, metrics: Iterable) -> List[str]:
        """Versions of the given metrics; copies and cached metrics are unknown."""
        versions = []
        for metric in metrics:
            entry = self._by_id.get(id(metric))
            versions.append(entry[1] if entry and entry[0] is metric else UNKNOWN)
        return versions


def selected_versions(
    recorded: Iterable[tuple], selected: Iterable, key: Callable
) -> List[str]:
    """Versions of recorded (metric, version) pairs whose key a selected metric shares."""
    keys = {key(metric) for metric in selected}
    return [version for metric, version in recorded if key(metric) in keys]


def _natural_key(version: str) -> list:
    return [
        int(part) if part.isdigit() else part for part in re.split(r"(\d+)", version)
    ]


def with_versions(
    printer: Callable, versions: Iterable[str], markdown: bool = False
) -> Callable:
    """Wrap a report printer so the report starts with its project versions."""
    produced = ", ".join(sorted(set(versions), key=_natural_key)) or UNKNOWN
    prefix = "- " if markdown else ""

    def print_report(*args, **kwargs):
        print(f"{prefix}Produced by storage-scale-test: {produced}")
        print(f"{prefix}Reported by storage-scale-test: {project_version()}")
        print()
        printer(*args, **kwargs)

    return print_report
