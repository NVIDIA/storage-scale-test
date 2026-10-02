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


REPORT_OPTIONS = (
    ReportOption(
        "--to-csv", "common", "Export metrics to CSV.", {"action": "store_true"}
    ),
    ReportOption(
        "--from-csv",
        "common",
        "Read cached metrics instead of raw results.",
        {"metavar": "FILE"},
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
        "Analyze live client imbalance and outliers.",
        {"action": "store_true"},
    ),
    ReportOption(
        "--client-outlier-threshold",
        "io",
        "Underperforming-client z-score magnitude.",
        {"type": float, "default": 2.0, "metavar": "Z"},
    ),
    ReportOption(
        "--client-min-underperform-segments",
        "io",
        "Minimum underperforming live intervals.",
        {"type": int, "default": 1, "metavar": "N"},
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
        "--normalize-to",
        "mdtest",
        "Scale metadata rates/stddev to N nodes; not latency.",
        {"type": int, "metavar": "N"},
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
        for option in REPORT_OPTIONS:
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
        "--output-dir", help="Report destination (batches: RESULTS_DIR/reports)."
    )


def validate_analysis_arguments(args: argparse.Namespace) -> None:
    """Reject invalid numeric controls before creating any reports."""
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
