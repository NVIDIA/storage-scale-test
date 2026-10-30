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

"""Single-file HTML filesystem report rendered from results documents.

``render_report`` turns one or more loaded documents
(``lib.results_document``) into a self-contained page; ``write_report``
writes it atomically. The same documents always render byte-identical HTML.
Sections that differ in one setting are paired automatically
(``pairing``); ``load_comparison_file`` reads a YAML file forcing more.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Any, Sequence

from lib.report_html.common import KeepExecution

from lib.report_html.pairing import ComparisonFileError, load_comparison_file
from lib.report_html.render import CHART_JS_INTEGRITY, CHART_JS_URL, render_html
from lib.report_html.summary import build_report
from lib.results_document import LoadedDocument

__all__ = [
    "CHART_JS_INTEGRITY",
    "CHART_JS_URL",
    "ComparisonFileError",
    "load_comparison_file",
    "render_report",
    "write_report",
]


def render_report(
    documents: Sequence[LoadedDocument],
    comparisons: Any = None,
    *,
    keep_execution: KeepExecution | None = None,
    live_detail: str | None = None,
    normalize_to: int | None = None,
) -> str:
    """The HTML report of the given documents, in the given order.

    ``comparisons`` is a parsed comparison file (a list of ``{title,
    sections}``, e.g. from ``load_comparison_file``); invalid entries or
    unknown sections raise ``ComparisonFileError``.
    ``keep_execution(group, execution)`` returning False leaves an execution
    out of every part of the report but provenance. ``live_detail`` (``none``,
    ``summary``, ``selected`` or ``full``) renders documents with more live
    detail as if they had that level; None uses each document's own.
    ``normalize_to`` scales metadata rates to that many nodes, as
    ``--normalize-to`` does in text; None uses each document's recorded value.
    """
    return render_html(
        build_report(
            list(documents),
            comparisons,
            keep_execution=keep_execution,
            live_detail=live_detail,
            normalize_to=normalize_to,
        )
    )


def write_report(path: str | os.PathLike, page: str) -> None:
    """Write ``page`` to ``path`` via a temporary file and rename; the file
    gets the usual ``0o666 & ~umask`` mode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".report-", dir=path.parent)
    umask = os.umask(0)
    os.umask(umask)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(page)
            stream.flush()
            os.fsync(stream.fileno())
            # mkstemp creates 0600; match the mode of other outputs.
            os.fchmod(stream.fileno(), 0o666 & ~umask)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
