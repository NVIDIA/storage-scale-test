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

"""Dependency-free command lines for self-bootstrapping report entrypoints."""

from __future__ import annotations

import argparse
from collections.abc import Callable

from lib.python_bootstrap import ensure_runtime

from lib.filesystem_report_options import (
    add_analysis_arguments,
    add_report_destination_arguments,
)
from lib.project_version import add_version_argument, exit_for_information
from lib.reporting_common import add_common_report_arguments


def prepare_report_runtime(
    script_path: str, parser_factory: Callable[[], argparse.ArgumentParser]
) -> None:
    """Serve informational options before provisioning any runtime dependencies."""
    exit_for_information(parser_factory)
    ensure_runtime(script_path)


def extract_filesystem_parser() -> argparse.ArgumentParser:
    """Build the extract-filesystem command line without runtime dependencies."""
    parser = argparse.ArgumentParser(
        usage="%(prog)s [OPTIONS] [RESULTS_DIR ...]",
        description="Report filesystem results; options apply only to matching groups.",
        epilog="--test-parse needs --kind io or --kind mdtest; not valid for batches.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "results_dirs",
        metavar="RESULTS_DIR",
        nargs="*",
        help="One batch, or one or more result directories: IO, metadata, or "
        "both (several make one report and need --output-dir).",
    )
    parser.add_argument(
        "--kind",
        choices=("all", "io", "mdtest"),
        default="all",
        help="Report only IO or only metadata results (default: all).",
    )
    add_report_destination_arguments(parser)
    add_analysis_arguments(parser, "all", explicit_only=True)
    add_version_argument(parser)
    return parser


def extract_elbencho_parser() -> argparse.ArgumentParser:
    """Build the extract-elbencho command line without runtime dependencies."""
    parser = argparse.ArgumentParser(
        description=(
            "Analyze elbencho results. Internal engine of extract-filesystem.py; "
            "run that instead."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "input_dirs",
        nargs="*",
        help="Directories containing elbencho output files",
    )
    add_analysis_arguments(parser, "io")
    add_report_destination_arguments(parser)
    add_version_argument(parser)
    return parser


def extract_mdtest_elbencho_parser() -> argparse.ArgumentParser:
    """Build the extract-mdtest-elbencho command line without runtime dependencies."""
    parser = argparse.ArgumentParser(
        description=(
            "Analyze mdtest-elbencho metadata benchmark results. Internal engine "
            "of extract-filesystem.py; run that instead."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "input_dirs",
        nargs="*",
        help="Directories containing mdtest-elbencho result files",
    )
    add_analysis_arguments(parser, "mdtest")
    add_report_destination_arguments(parser)
    add_version_argument(parser)
    return parser


def extract_netbench_parser() -> argparse.ArgumentParser:
    """Build the extract-netbench command line without runtime dependencies."""
    parser = argparse.ArgumentParser(
        description="Analyze elbencho netbench benchmark results.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "input_dirs",
        nargs="*",
        help="Directories containing netbench result files",
    )
    add_common_report_arguments(parser)
    parser.add_argument(
        "--only-mode",
        metavar="MODE",
        choices=["bidir", "half"],
        help="Only include this mode (bidir or half)",
    )
    parser.add_argument(
        "--test-parse",
        metavar="FILE",
        help="Test parsing a single file pair (provide path without extension)",
    )

    add_version_argument(parser)
    return parser


def extract_warp_parser() -> argparse.ArgumentParser:
    """Build the extract-warp command line without runtime dependencies."""
    parser = argparse.ArgumentParser(
        description="""
    Analyze Warp benchmark results.

    Usage:
      # Parse benchmark files in a directory
      %(prog)s /path/to/results

      # Specify different output directory for plots
      %(prog)s /path/to/results --output-dir /path/to/plots
    """,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "directory",
        help="Directory containing Warp benchmark files.",
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        help="Directory to save plots and reports (default: the input directory).",
    )
    parser.add_argument(
        "--only-sizes",
        action="append",
        metavar="SIZE",
        help=(
            "Only include benchmarks with these object sizes (repeat flag for multiple). "
            "Commas are not split; use ';' inside one argument for several sizes "
            "(e.g. '1MiB;1GiB') or pass --only-sizes multiple times. "
            "Former comma-separated lists must use ';' or multiple --only-sizes flags."
        ),
    )
    parser.add_argument(
        "--only-threads",
        help="Comma-separated list of thread counts to include (e.g., 1,2,4)",
    )
    parser.add_argument(
        "--per-client-plots",
        action="store_true",
        help="Generate per-client comparison plots for multi-node runs. "
        "Identifies underperforming clients and visualizes performance clustering.",
    )
    parser.add_argument(
        "--client-outlier-threshold",
        type=float,
        default=2.0,
        help="Z-score threshold for identifying underperforming clients. "
        "Default: 2.0 (95%% confidence, ~2.5%% of normal distribution). "
        "Lower values (e.g., 1.5) detect more outliers but may include random variance. "
        "Higher values (e.g., 3.0) detect only severe outliers (99.7%% confidence).",
    )
    parser.add_argument(
        "--client-min-underperform-segments",
        type=int,
        default=1,
        help="Minimum number of time segments a client must underperform to be flagged. "
        "Default: 1 (flag if underperforms in any segment). "
        "Higher values (e.g., 10) require persistent underperformance.",
    )
    add_version_argument(parser)
    return parser


def summarize_elbencho_parser() -> argparse.ArgumentParser:
    """Build the summarize-elbencho command line without runtime dependencies."""
    parser = argparse.ArgumentParser(
        description=(
            "Print a three-line summary per nv-elbencho-sweep result directory "
            "(reads env_used.yaml)."
        )
    )
    parser.add_argument(
        "directories",
        nargs="+",
        help="One or more result directories (e.g. elbencho-<DS> under RESULTS_DIR)",
    )
    add_version_argument(parser)
    return parser
