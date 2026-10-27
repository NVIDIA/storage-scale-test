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
from dataclasses import dataclass, field
import math
from typing import Any

from lib.storage_target import ALLOW_MIXED_TARGETS_FLAG


@dataclass(frozen=True)
class ReportOption:
    """One public option, its workload scope, and argparse declaration."""

    flag: str
    scope: str
    help: str
    settings: dict[str, Any] = field(default_factory=dict)

    @property
    def destination(self) -> str:
        """Match argparse's destination for long options."""
        return self.flag[2:].replace("-", "_")


TO_JSON_FLAG = "--to-json"
REQUIRE_COMPLETE_FLAG = "--require-complete"
LIVE_DETAIL_FLAG = "--live-detail"
SHORTFALL_FLAG = "--client-shortfall-percent"
SHORTFALL_LIMIT_PERCENT = 50
RESULTS_FRAGMENT_FLAG = "--results-fragment"
RESULTS_LIVE_DIR_FLAG = "--results-live-dir"
# Mirrors lib.filesystem_results.LIVE_DETAIL_LEVELS, which this dependency-free
# module cannot import.
LIVE_DETAIL_LEVELS = ("none", "summary", "selected", "full")
# Flags the batch router consumes itself instead of forwarding to engines.
BATCH_CONSUMED_FLAGS = (TO_JSON_FLAG, REQUIRE_COMPLETE_FLAG)

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
        "--markdown",
        "common",
        "Markdown on stdout; progress on stderr.",
        {"action": "store_true"},
    ),
    ReportOption(
        "--test-parse",
        "common",
        "Inspect one raw result without reporting.",
        {"metavar": "FILE"},
    ),
    ReportOption(
        TO_JSON_FLAG,
        "common",
        "Also write the machine-readable results document to "
        "<output-dir>/results.json.",
        {"action": "store_true"},
    ),
    ReportOption(
        REQUIRE_COMPLETE_FLAG,
        "common",
        "After writing the --to-json document, exit 3 if any execution is "
        "not complete.",
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
    ),
    ReportOption(
        "--per-client-plots",
        "io",
        "Write per-client live summaries and plots.",
        {"action": "store_true"},
    ),
    ReportOption(
        SHORTFALL_FLAG,
        "io",
        "Slow client shortfall below the fleet median, percent.",
        {"type": float, "default": 5.0, "metavar": "PCT"},
    ),
    ReportOption(
        "--client-min-slow-windows",
        "io",
        "Minimum 5 s windows at twice the shortfall for a transient client.",
        {"type": int, "default": 3, "metavar": "N"},
    ),
    ReportOption(
        "--client-max-timeseries-lines",
        "io",
        "Maximum client lines per time-series plot.",
        {"type": int, "default": 10, "metavar": "N"},
    ),
    ReportOption(
        "--client-max-heatmap-rows",
        "io",
        "Maximum clients per heatmap.",
        {"type": int, "default": 50, "metavar": "N"},
    ),
    ReportOption(
        "--live-detail",
        "io",
        "Live CSV detail in the --to-json document (summary by default).",
        {"choices": LIVE_DETAIL_LEVELS, "default": "summary", "metavar": "LEVEL"},
    ),
    ReportOption(
        "--normalize-to",
        "mdtest",
        "Scale metadata rates/stddev to N nodes; not latency.",
        {"type": int, "metavar": "N"},
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
            if explicit_only:
                settings["default"] = argparse.SUPPRESS
            group.add_argument(option.flag, help=option.help, **settings)


def add_report_destination_arguments(parser: argparse.ArgumentParser) -> None:
    """Share batch selection and output help across filesystem reporters."""
    parser.add_argument("--groups", help="Comma-separated prepared batch group IDs.")
    parser.add_argument(
        "--output-dir", help="Report destination (batches: <batch_dir>/reports)."
    )


def validate_analysis_arguments(args: argparse.Namespace) -> None:
    """Reject invalid numeric controls before creating any reports."""
    if getattr(args, "require_complete", False) and not getattr(args, "to_json", False):
        raise ValueError(f"{REQUIRE_COMPLETE_FLAG} requires {TO_JSON_FLAG}")
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


def options_by_kind(args: argparse.Namespace, kinds: set[str]) -> dict[str, list[str]]:
    """Forward explicitly supplied options only to matching selected workloads."""
    validate_analysis_arguments(args)
    result = {kind: [] for kind in kinds}
    for option in REPORT_OPTIONS:
        if not hasattr(args, option.destination):
            continue
        value = getattr(args, option.destination)
        matching = kinds if option.scope == "common" else kinds & {option.scope}
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
