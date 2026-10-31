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

"""Shared filesystem analysis options, help, and workload-aware forwarding."""

import argparse
import copy
from dataclasses import dataclass, field
import math
import os
import re
from typing import Any, Mapping

from lib.storage_target import ALLOW_MIXED_TARGETS_FLAG


@dataclass(frozen=True)
class ReportOption:
    """One public option, its workload scope, and argparse declaration."""

    flag: str
    scope: str
    help: str
    settings: dict[str, Any] = field(default_factory=dict)
    # The results.json generator option a --from-json replay defaults to.
    recorded: str | None = None
    # report.html reads this option's effect precomputed from the document,
    # so a replay cannot change it.
    fixed: bool = False

    @property
    def destination(self) -> str:
        """Match argparse's destination for long options."""
        return self.flag[2:].replace("-", "_")

    @property
    def default(self) -> Any:
        """The value of the option when not given (and not replayed)."""
        flag = self.settings.get("action") == "store_true"
        return self.settings.get("default", False if flag else None)


REQUIRE_COMPLETE_FLAG = "--require-complete"
LIVE_DETAIL_FLAG = "--live-detail"
SHORTFALL_FLAG = "--client-shortfall-percent"
SHORTFALL_LIMIT_PERCENT = 50
RESULTS_FRAGMENT_FLAG = "--results-fragment"
RESULTS_LIVE_DIR_FLAG = "--results-live-dir"
BATCH_GROUP_FLAG = "--batch-group"
FROM_JSON_FLAG = "--from-json"
COMPARISONS_FLAG = "--comparisons"
# Several --from-json documents render into <output-dir>/d1, d2, ...; the
# report model labels them the same way.
DOCUMENT_DIR_PREFIX = "d"
# Mirrors lib.filesystem_results.LIVE_DETAIL_LEVELS, which this dependency-free
# module cannot import.
LIVE_DETAIL_LEVELS = ("none", "summary", "selected", "full")
# Flags the batch router consumes itself instead of forwarding to engines.
BATCH_CONSUMED_FLAGS = (REQUIRE_COMPLETE_FLAG,)

REPORT_OPTIONS = (
    ReportOption(
        ALLOW_MIXED_TARGETS_FLAG,
        "common",
        "Combine results from different storage targets in one report, "
        "labelled 'mixed: ...', instead of refusing them. A batch never needs "
        "this: it reports each group separately.",
        {"action": "store_true"},
    ),
    ReportOption(
        "--only-threads",
        "common",
        "Thread counts: comma lists or ranges.",
        {"metavar": "THREADS"},
    ),
    ReportOption(
        "--only-nodes",
        "common",
        "Node counts: comma lists or ranges.",
        {"metavar": "NODES"},
    ),
    ReportOption(
        "--test-parse",
        "common",
        "Inspect one raw result without reporting.",
        {"metavar": "FILE"},
    ),
    ReportOption(
        REQUIRE_COMPLETE_FLAG,
        "common",
        "After writing results.json, exit 3 if any execution is not complete.",
        {"action": "store_true"},
    ),
    ReportOption(
        "--only-sizes",
        "io",
        "IO sizes; repeat or use ';'. Commas stay within compound sizes.",
        {"action": "append", "metavar": "SIZE"},
    ),
    ReportOption(
        "--only-iodepths",
        "io",
        "IO depths: comma lists or ranges.",
        {"metavar": "IODEPTHS"},
    ),
    ReportOption(
        "--no-dual-y-axis",
        "io",
        "Use separate throughput/latency axes.",
        {"action": "store_true"},
        recorded="no_dual_y_axis",
    ),
    ReportOption(
        SHORTFALL_FLAG,
        "io",
        "Slow client shortfall below the fleet median, percent.",
        {"type": float, "default": 5.0, "metavar": "PCT"},
        recorded="shortfall_percent",
        fixed=True,
    ),
    ReportOption(
        "--client-min-slow-windows",
        "io",
        "Minimum 5 s windows at twice the shortfall for a transient client.",
        {"type": int, "default": 3, "metavar": "N"},
        recorded="min_slow_windows",
        fixed=True,
    ),
    ReportOption(
        "--client-max-timeseries-lines",
        "io",
        "Maximum client lines per time-series plot.",
        {"type": int, "default": 10, "metavar": "N"},
        recorded="client_max_timeseries_lines",
    ),
    ReportOption(
        "--client-max-heatmap-rows",
        "io",
        "Maximum clients per heatmap.",
        {"type": int, "default": 50, "metavar": "N"},
        recorded="client_max_heatmap_rows",
    ),
    ReportOption(
        "--live-detail",
        "io",
        "Live CSV detail to keep: none, summary, selected, or full (default). "
        "Below full, per-client plots and charts are omitted.",
        {"choices": LIVE_DETAIL_LEVELS, "default": "full", "metavar": "LEVEL"},
        recorded="live_detail",
    ),
    ReportOption(
        "--normalize-to",
        "mdtest",
        "Scale metadata rates/stddev to N nodes; not latency.",
        {"type": int, "metavar": "N"},
        recorded="normalize_to",
    ),
)


# Engine-only options: batch assembly passes them; users never see them.
INTERNAL_OPTIONS = (
    ReportOption(
        RESULTS_FRAGMENT_FLAG,
        "common",
        argparse.SUPPRESS,
        {"metavar": "PATH"},
    ),
    ReportOption(
        RESULTS_LIVE_DIR_FLAG,
        "io",
        argparse.SUPPRESS,
        {"metavar": "DIR"},
    ),
    # A document reported beside others (a batch group, or one kind of mixed
    # directories): nothing to report is not an error, and when rendering
    # documents the caller writes report.html.
    ReportOption(
        BATCH_GROUP_FLAG,
        "common",
        argparse.SUPPRESS,
        {"action": "store_true"},
    ),
)


def add_analysis_arguments(
    parser: argparse.ArgumentParser, kind: str, *, explicit_only: bool = False
) -> None:
    """Register identical option/help definitions on all filesystem front doors."""
    for scope, title in (
        ("common", "Common analysis"),
        ("io", "IO analysis"),
        ("mdtest", "Metadata analysis"),
    ):
        if scope not in ("common", kind) and kind != "all":
            continue
        group = parser.add_argument_group(title)
        # Engines (a concrete kind) also accept the internal options.
        internal = INTERNAL_OPTIONS if kind != "all" else ()
        for option in (*REPORT_OPTIONS, *internal):
            if option.scope != scope:
                continue
            settings = dict(option.settings)
            text = option.help
            if option.recorded and not explicit_only and option.default:
                # What ArgumentDefaultsHelpFormatter shows without SUPPRESS.
                text = f"{text} (default: {option.default})"
            if explicit_only or option.recorded:
                # Unset until resolve_report_options, so that a --from-json
                # replay can tell an option given from one left out.
                settings["default"] = argparse.SUPPRESS
            group.add_argument(option.flag, help=text, **settings)


def resolve_report_options(
    args: argparse.Namespace, kind: str, recorded: Mapping[str, Any] | None = None
) -> argparse.Namespace:
    """A copy of ``args`` with each ``kind`` option not given set to its value
    in ``recorded`` (a results document's generator options), else its default.

    The ``--only-*`` filters and ``--groups`` stay render-time selections.
    """
    resolved = copy.copy(args)
    for option in REPORT_OPTIONS:
        if (
            option.recorded is None
            or option.scope not in ("common", kind)
            or hasattr(args, option.destination)
        ):
            continue
        value = (recorded or {}).get(option.recorded)
        if value is None:
            value = option.default
        setattr(resolved, option.destination, value)
    return resolved


def check_replay_options(
    args: argparse.Namespace, recorded: list[Mapping[str, Any]]
) -> None:
    """Refuse a ``fixed`` option given with a value other than the one a
    document records (each of ``recorded`` is one document's options)."""
    for option in REPORT_OPTIONS:
        given = getattr(args, option.destination, None)
        if not option.fixed or given is None:
            continue
        for options in recorded:
            value = options.get(option.recorded)
            if value is not None and value != given:
                raise ValueError(
                    f"{option.flag} {given}: the results document records "
                    f"{value}; re-run from the raw results to change it"
                )


def add_report_destination_arguments(parser: argparse.ArgumentParser) -> None:
    """Share batch selection and output help across filesystem reporters."""
    parser.add_argument(
        "--groups", help="Comma-separated batch or results document group IDs."
    )
    parser.add_argument(
        "--output-dir",
        help="Where to write the report (default: the input directory, or "
        "<batch>/reports). Required, and not an input, with several inputs or "
        "--from-json.",
    )
    parser.add_argument(
        FROM_JSON_FLAG,
        metavar="PATH",
        nargs="+",
        action="extend",
        help="Render again from saved results.json documents instead of raw "
        "results; options not given keep each document's recorded values. "
        "Several documents share one report.html, with document N's text and "
        f"PNGs in <output-dir>/{DOCUMENT_DIR_PREFIX}N/.",
    )
    parser.add_argument(
        COMPARISONS_FLAG,
        metavar="FILE",
        help="YAML file of sections report.html must compare: a list of "
        "{title, sections: ['<doc>:<group>[:<shape>]', ...]}. See "
        "docs/FILESYSTEM_TESTING.md#comparisons.",
    )


INTEGER_FILTER_FLAGS = ("--only-nodes", "--only-threads", "--only-iodepths")
_INTEGER_ITEM = re.compile(r"[0-9]+(-[0-9]+)?")


def parse_integer_filter(value: str) -> set[int]:
    """The integers of a comma list of numbers and inclusive ranges.

    A filter is a request, not a hint: a malformed, empty or reversed item
    raises ValueError instead of being dropped, which would widen the report.
    """
    if not value.strip():
        raise ValueError("filter must not be empty")
    result: set[int] = set()
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            raise ValueError("empty filter item")
        if not _INTEGER_ITEM.fullmatch(item):
            raise ValueError(f"invalid integer or range: {item}")
        start, _, end = item.partition("-")
        if end and int(start) > int(end):
            raise ValueError(f"range start exceeds end: {item}")
        result.update(range(int(start), int(end or start) + 1))
    return result


def validate_integer_filter(flag: str, value: str | None) -> set[int] | None:
    """The integers ``flag`` selects (None when not given); ValueError names
    the flag when malformed."""
    if not value:
        return None
    try:
        return parse_integer_filter(value)
    except ValueError as error:
        raise ValueError(f"invalid {flag} filter: {error}") from error


def validate_analysis_arguments(args: argparse.Namespace) -> None:
    """Reject invalid numeric controls and filters before creating reports."""
    for flag in INTEGER_FILTER_FLAGS:
        validate_integer_filter(flag, getattr(args, flag[2:].replace("-", "_"), None))
    for option in REPORT_OPTIONS:
        value = getattr(args, option.destination, None)
        if (
            option.settings.get("type") in (int, float)
            and value is not None
            and (value <= 0 or (isinstance(value, float) and not math.isfinite(value)))
        ):
            if option.flag == "--normalize-to":
                raise ValueError("--normalize-to must be a positive integer")
            raise ValueError(f"{option.flag} must be greater than zero")
    shortfall = getattr(args, SHORTFALL_FLAG[2:].replace("-", "_"), None)
    if shortfall is not None and shortfall >= SHORTFALL_LIMIT_PERCENT:
        raise ValueError(
            f"{SHORTFALL_FLAG} must be less than {SHORTFALL_LIMIT_PERCENT}"
        )


def unique_inputs(input_dirs: list[str]) -> list[str]:
    """Input directories without repeats (``dir`` and ``dir/`` are one), in
    order, each as first spelled."""
    unique: dict[str, str] = {}
    for directory in input_dirs:
        unique.setdefault(os.path.realpath(directory), directory)
    return list(unique.values())


def validate_report_inputs(input_dirs: list[str], output_dir: str | None) -> None:
    """Inputs must exist; several need their own ``--output-dir``.

    Several inputs make one document, which must not overwrite an input's
    own ``results.json``; a subdirectory of an input is fine.
    """
    for directory in input_dirs:
        if not os.path.isdir(directory):
            raise ValueError(f"Input directory not found: {directory}")
    if len(unique_inputs(input_dirs)) < 2:
        return
    if not output_dir:
        raise ValueError("--output-dir is required with several input directories")
    output = os.path.realpath(output_dir)
    if any(os.path.realpath(directory) == output for directory in input_dirs):
        raise ValueError(
            f"--output-dir {output_dir} is an input directory; "
            "choose a separate directory"
        )


def validate_from_json(
    documents: list[str], input_dirs: list[str], output_dir: str | None
) -> None:
    """``--from-json`` replaces inputs and needs a separate ``--output-dir``.

    A subdirectory of a document's directory is fine, unless it is where
    another document's own rendering would go.
    """
    if input_dirs:
        raise ValueError(
            f"{FROM_JSON_FLAG} replaces input directories; do not combine them"
        )
    if not output_dir:
        raise ValueError(f"--output-dir is required with {FROM_JSON_FLAG}")
    for document in documents:
        if os.path.isdir(document):
            raise ValueError(
                f"{FROM_JSON_FLAG} replaces input directories and takes only "
                f"results documents: {document} is a directory"
            )
        if not os.path.isfile(document):
            raise ValueError(f"Results document not found: {document}")
    sources = {os.path.dirname(os.path.realpath(document)) for document in documents}
    targets = {
        os.path.realpath(document_output_dir(output_dir, number, len(documents)))
        for number in range(1, len(documents) + 1)
    }
    if os.path.realpath(output_dir) in sources or targets & sources:
        raise ValueError(
            f"--output-dir {output_dir} is a results document's directory; "
            "choose a separate directory"
        )


def document_output_dir(output_dir: str, number: int, count: int) -> str:
    """Where the 1-based ``number``-th of ``count`` documents renders."""
    if count == 1:
        return output_dir
    return os.path.join(output_dir, f"{DOCUMENT_DIR_PREFIX}{number}")


def options_by_kind(args: argparse.Namespace, kinds: set[str]) -> dict[str, list[str]]:
    """Forward explicitly supplied options only to matching selected workloads."""
    validate_analysis_arguments(args)
    result = {kind: [] for kind in kinds}
    for option in REPORT_OPTIONS:
        value = getattr(args, option.destination, None)
        # Unset (an engine's namespace holds None or False for those).
        if value is None or value is False:
            continue
        matching = kinds if option.scope == "common" else kinds & {option.scope}
        # Restating the default (e.g. --live-detail) changes nothing elsewhere.
        if not matching and value == option.settings.get("default", object()):
            continue
        if not matching:
            raise ValueError(
                f"{option.flag} applies to no selected {option.scope} group"
            )
        for kind in matching:
            result[kind].extend(_option_tokens(option, value))
    return result


def _option_tokens(option: ReportOption, value: Any) -> list[str]:
    """Keep repeatable values intact, including commas, spaces, and leading dashes."""
    if option.settings.get("action") == "store_true":
        return [option.flag] if value else []
    values = value if isinstance(value, list) else [value]
    return [f"{option.flag}={item}" for item in values]
