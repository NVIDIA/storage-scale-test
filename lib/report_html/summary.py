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

"""Build the report model from results documents.

Groups sort by ID, sections by shape and executions by coordinates, so the
same documents always give the same model.
"""

from __future__ import annotations

from collections import Counter
import shlex
from typing import Any, Sequence

from lib.report_html import model
from lib.report_html.compare import build_comparisons
from lib.report_html.common import (
    GB,
    GBS,
    GIB,
    K_GBS,
    K_IOPS,
    K_OPS,
    NO_VALUE,
    PHASE_WRITE,
    GroupContext,
    KeepExecution,
    group_contexts,
    settings_rows,
    value_text,
)
from lib.report_html.mdtest import MD_OPS, build_md_sections
from lib.report_html.model import (
    Card,
    Column,
    Link,
    Measure,
    Provenance,
    Report,
    Section,
    Table,
)
from lib.report_html.reduce import sig
from lib.report_html.sweep import build_io_sections
from lib.project_version import project_version
from lib.reporting_common import natural_key
from lib.results_document import LoadedDocument

RENDERER = "storage-scale-test"
MAX_DIAGNOSTICS = 200
_COMPLETE = "complete"
_BATCH = "batch"
_EXECUTIONS = "executions"
_CARD_METRICS = (
    ("Peak read", "read", K_GBS, GBS),
    ("Peak write", PHASE_WRITE, K_GBS, GBS),
    ("Peak read IOPS", "read", K_IOPS, "IOPS"),
    ("Peak write IOPS", PHASE_WRITE, K_IOPS, "IOPS"),
) + tuple((f"Peak {op}", op, K_OPS, "ops/s") for op in MD_OPS)
_TLDR_KEYS = (("read", K_GBS), (PHASE_WRITE, K_GBS)) + tuple(
    (op, K_OPS) for op in MD_OPS
)


def _number(value) -> str:
    if value is None:
        return NO_VALUE
    value = sig(value)
    return f"{value:,.0f}" if abs(value) >= 1000 else f"{value:g}"


def _group_sections(ctx: GroupContext, executions: list[dict]) -> list[Section]:
    if ctx.group.get("kind") == "mdtest":
        sections = build_md_sections(ctx, executions)
    else:
        sections = build_io_sections(ctx, executions)
    if not sections:
        sections = [
            Section(
                anchor=ctx.anchor("none"),
                title="No results",
                target=ctx.target,
                group_id=ctx.group_id,
                document=ctx.document,
                settings=[],
            )
        ]
    states = Counter(
        str(e.get("result_state"))
        for e in executions
        if e.get("result_state") not in (_COMPLETE, "partial")
    )
    note = ""
    if states:
        counts = ", ".join(f"{state} {n}" for state, n in sorted(states.items()))
        note = f"Executions without results in this group: {counts}. See Provenance."
    for section in sections:
        section.settings = settings_rows(ctx.group, executions)
        section.settings_text = ctx.group.get("settings_text")
        if note:
            section.notes.append(note)
    return sections


def _tldr_row(section: Section, record: dict, several: bool) -> list:
    if K_OPS in record:
        performance = Measure(record[K_OPS], f"{_number(record['ops'])} ops/s")
    else:
        text = f"{_number(record['gbs'])} GB/s · {_number(record['iops'])} IOPS"
        performance = Measure(record[K_GBS], text)
    group = f"g{section.group_id}"
    if several:
        group = f"d{section.document} {group}"
    return [
        Link(record["test"], f"#{section.anchor}"),
        section.target,
        record["io_size"],
        record["threads"],
        record["depth"],
        record["nodes"],
        performance,
        record["p99_ms"],
        group,
    ]


def tldr_table(sections: Sequence[Section], several: bool) -> Table:
    """Best result per section and phase."""
    rows = [
        _tldr_row(section, section.best[key], several)
        for section in sections
        for key in _TLDR_KEYS
        if key in section.best
    ]
    columns = [
        Column("test", numeric=False),
        Column("storage target", numeric=False),
        Column("IO size", numeric=False),
        Column("threads/node"),
        Column("IO depth"),
        Column("nodes"),
        Column("performance"),
        Column("p99 ms", digits=3),
        Column("group", numeric=False, extra=True),
    ]
    return Table(
        model.TLDR_TABLE,
        model.TLDR_TABLE,
        "Best result per section: peak GB/s (IO) or ops/s (metadata).",
        columns,
        rows,
    )


def _card(label: str, key: tuple, unit: str, sections: Sequence[Section]):
    candidates = [
        (s.best[key][key[1]], s)
        for s in sections
        if key in s.best and s.best[key][key[1]] is not None
    ]
    if not candidates:
        return None
    value, section = max(candidates, key=lambda item: item[0])
    record = section.best[key]
    parts = [section.target]
    if record["io_size"] != NO_VALUE:
        parts.append(record["io_size"])
    parts.append(record["config"])
    subtitle = record.get("note", "")
    if unit == GBS:
        subtitle = f"{sig(value * GB / GIB, 3)} GiB/s"
    return Card(label, value, unit, " · ".join(parts), f"#{section.anchor}", subtitle)


def header_cards(sections: Sequence[Section]) -> list[Card]:
    """Peak read/write GB/s and IOPS, and peak metadata rates."""
    cards = []
    for label, phase, metric, unit in _CARD_METRICS:
        card = _card(label, (phase, metric), unit, sections)
        if card:
            cards.append(card)
    return cards


def _counts(counts: dict | None) -> str:
    items = [(k, v) for k, v in (counts or {}).items() if v and k != "total"]
    return ", ".join(f"{k} {v}" for k, v in items) or NO_VALUE


def _options(options: dict | None) -> str:
    items = [(k, v) for k, v in (options or {}).items() if v not in (None, [], "")]
    return "; ".join(f"{k} = {value_text(v)}" for k, v in items) or NO_VALUE


def _producers(data: dict) -> str:
    """Producer versions: per execution (IO), per mdtest iteration (metadata)."""
    versions = set()
    for execution in data.get(_EXECUTIONS) or []:
        versions.add(execution.get("producer_version"))
        iterations = (execution.get("results") or {}).get("iterations") or []
        versions.update(item.get("producer_version") for item in iterations)
    return ", ".join(sorted(str(version) for version in versions if version))


def _entries(loaded: LoadedDocument) -> list[tuple[str, str]]:
    data = loaded.data
    generator = data.get("generator") or {}
    batch = data.get(_BATCH) or {}
    outcome = data.get("batch_outcome") or {}
    command = generator.get("command") or []
    entries = [
        ("Document", str(loaded.path) if loaded.path else ""),
        ("Document sha256", loaded.sha256 or ""),
        ("Schema version", str(data.get("schema_version") or "")),
        ("Exported by", _exporter(generator)),
        ("Rendered by", _renderer()),
        ("Produced by", _producers(data)),
        ("Generated", str(generator.get("generated_at") or "")),
        ("Command", shlex.join(str(part) for part in command)),
        ("Options", _options(generator.get("options"))),
        ("Root", str(batch.get("root") or "")),
        ("Datestamp", str(batch.get("datestamp") or "")),
        ("Manifest sha256", str(batch.get("manifest_sha256") or "")),
        ("Sealed", value_text(batch.get("sealed"))),
        ("Substrate", str(batch.get("substrate") or "")),
        ("Executions", _counts(outcome.get(_EXECUTIONS))),
        ("Results", _counts(outcome.get("results"))),
    ]
    return [(label, text.strip()) for label, text in entries if text.strip()]


def _problem_text(problem: dict) -> str:
    text = str(problem.get("code"))
    if problem.get("phase"):
        text += f" ({problem['phase']})"
    if problem.get("detail"):
        text += f": {problem['detail']}"
    return text


def _diagnostics(data: dict) -> list[str]:
    lines = [_problem_text(p) for p in data.get("diagnostics") or []]
    for execution in data.get(_EXECUTIONS) or []:
        state = execution.get("result_state")
        problems = execution.get("problems") or []
        if state == _COMPLETE and not problems:
            continue
        head = (
            f"group {execution.get('group_id')} execution "
            f"{execution.get('execution_id') or execution.get('coordinate_key')}: "
            f"{execution.get('ledger_state')}/{state}"
        )
        details = "; ".join(_problem_text(p) for p in problems)
        lines.append(f"{head} — {details}" if details else head)
    return lines


def provenance(documents: Sequence[LoadedDocument]) -> list[Provenance]:
    """One provenance block per document."""
    blocks = []
    for index, loaded in enumerate(documents, 1):
        title = f"Document {index}" if len(documents) > 1 else "Source"
        lines = _diagnostics(loaded.data)
        shown = lines[:MAX_DIAGNOSTICS]
        blocks.append(
            Provenance(title, _entries(loaded), shown, len(lines) - len(shown))
        )
    return blocks


def _sources(loaded: LoadedDocument) -> list[str]:
    """A batch's root, else each group's result directory: several result
    directories are rooted at their parent, which holds other results too."""
    batch = loaded.data.get(_BATCH) or {}
    groups = loaded.data.get("groups") or []
    if not batch.get("manifest_sha256") and groups:
        if all(group.get("source") for group in groups):
            return [str(group["source"]) for group in groups]
    return [str(batch.get("root") or loaded.path or "")]


def _title(documents: Sequence[LoadedDocument]) -> str:
    if len(documents) > 1:
        return f"Filesystem results from {len(documents)} documents"
    batch = documents[0].data.get(_BATCH) or {}
    kind = "batch" if batch.get("manifest_sha256") else "results"
    datestamp = batch.get("datestamp")
    return f"Filesystem {kind} {datestamp}" if datestamp else f"Filesystem {kind}"


def _subtitle(
    documents: Sequence[LoadedDocument], sections: list[Section], executions: int
) -> str:
    targets = sorted({s.target for s in sections}, key=natural_key)
    substrates = sorted(
        {str((d.data.get(_BATCH) or {}).get("substrate")) for d in documents} - {"None"}
    )
    parts = [f"{len(sections)} sections", f"{executions} executions"]
    if substrates:
        parts.append(f"substrate {', '.join(substrates)}")
    if targets:
        parts.append(f"targets: {', '.join(targets)}")
    return " · ".join(parts)


def _exporter(generator: dict) -> str:
    return f"{generator.get('tool', '')} {generator.get('version', '')}".strip()


def _renderer() -> str:
    return f"{RENDERER} {project_version()}"


def _tool(documents: Sequence[LoadedDocument]) -> str:
    """Header phrase: the rendering version, plus the exporters unless every
    document was exported by this version."""
    generators = [d.data.get("generator") or {} for d in documents]
    rendered = f"Rendered by {_renderer()}"
    if all(g.get("version") == project_version() for g in generators):
        return rendered
    exporters = sorted({_exporter(g) for g in generators} - {""})
    return f"{rendered}; results exported by {', '.join(exporters)}"


def build_report(
    documents: Sequence[LoadedDocument],
    comparisons: Any = None,
    *,
    keep_execution: KeepExecution | None = None,
    live_detail: str | None = None,
    normalize_to: int | None = None,
) -> Report:
    """The report model of one or more documents.

    ``comparisons`` is a parsed comparison file (``pairing.check_comparisons``)
    whose sets come before the automatic ones. ``keep_execution`` and
    ``live_detail`` and ``normalize_to`` are as in ``common.group_contexts``.
    """
    if not documents:
        raise ValueError("no results documents to report")
    several = len(documents) > 1
    contexts = group_contexts(documents, keep_execution, live_detail, normalize_to)
    sections = []
    for ctx, executions in contexts:
        sections.extend(_group_sections(ctx, executions))
    executions = sum(len(items) for _, items in contexts)
    sources = [source for loaded in documents for source in _sources(loaded)]
    return Report(
        title=_title(documents),
        subtitle=_subtitle(documents, sections, executions),
        sources=[s for s in sources if s],
        tool=_tool(documents),
        cards=header_cards(sections),
        tldr=tldr_table(sections, several),
        sections=sections,
        provenance=provenance(documents),
        comparisons=build_comparisons(documents, contexts, comparisons),
    )
