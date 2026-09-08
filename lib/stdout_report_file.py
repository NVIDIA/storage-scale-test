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

"""Mirror printed reports to `report.txt` alongside stdout (for benchmark extract scripts)."""

from __future__ import annotations

import contextlib
import os
import sys
from typing import Any, Callable, List, TextIO

REPORT_TXT_FILENAME = "report.txt"


class TeeTextIO:
    """Duplicate writes to several text streams (stdout + report file)."""

    def __init__(self, *streams: TextIO) -> None:
        self._streams: List[TextIO] = list(streams)

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()

    @property
    def encoding(self) -> str:
        return getattr(self._streams[0], "encoding", "utf-8")

    @property
    def mode(self) -> str:
        return getattr(self._streams[0], "mode", "w")


def _ensure_parent_dir(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)


def mirror_stdout_to_file(
    path: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
) -> None:
    """Run ``fn(*args, **kwargs)`` with stdout teed to ``path`` (UTF-8) and the real stdout."""
    _ensure_parent_dir(path)
    with open(path, "w", encoding="utf-8") as out_file:
        with contextlib.redirect_stdout(TeeTextIO(sys.stdout, out_file)):
            fn(*args, **kwargs)


class AppendableStdoutReportFile:
    """Lazily create one ``report.txt`` and tee every mirrored print block into it."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._out_file = None

    def mirror(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Mirror one ``fn`` invocation to stdout and the shared report file."""
        if self._out_file is None:
            _ensure_parent_dir(self._path)
            # Session lifetime; closed in close() — not a single-block with block.
            self._out_file = open(  # pylint: disable=consider-using-with
                self._path, "w", encoding="utf-8"
            )
        with contextlib.redirect_stdout(TeeTextIO(sys.stdout, self._out_file)):
            fn(*args, **kwargs)

    def close(self) -> None:
        if self._out_file is not None:
            self._out_file.close()
            self._out_file = None

    def __enter__(self) -> AppendableStdoutReportFile:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
