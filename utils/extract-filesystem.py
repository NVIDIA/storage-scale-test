#!/usr/bin/env python3

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

"""Report prepared filesystem batches through the established analyzers."""

import argparse
from pathlib import Path
import subprocess
import sys

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.filesystem_batch import (  # pylint: disable=wrong-import-position
    KINDS,
    is_batch_directory,
    reject_batch_descendant_inputs,
    report_batch,
)
from lib.filesystem_report_options import (  # pylint: disable=wrong-import-position
    add_analysis_arguments,
    add_report_destination_arguments,
    options_by_kind,
)


def main(argv: list[str] | None = None) -> int:
    """Select independent groups, or delegate an ordinary directory unchanged."""
    parser = argparse.ArgumentParser(
        usage="%(prog)s [OPTIONS] [RESULTS_DIR]",
        description="Report filesystem results; options apply only to matching groups.",
        epilog="Cached input or parse-only mode requires --kind io|mdtest; not valid for batches.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "results_dir", nargs="?", help="Batch or ordinary results directory."
    )
    parser.add_argument(
        "--kind",
        choices=KINDS,
        default="all",
        help="Workload selection (default: all).",
    )
    add_report_destination_arguments(parser)
    add_analysis_arguments(parser, "all", explicit_only=True)
    args = parser.parse_args(argv)
    try:
        if args.results_dir:
            reject_batch_descendant_inputs([args.results_dir])
        if args.results_dir and is_batch_directory(args.results_dir):
            return report_batch(
                args.results_dir,
                args.kind,
                args.groups,
                args.output_dir,
                analysis_args=args,
            )
        if args.groups:
            parser.error("--groups requires a prepared batch input")
        return _report_ordinary(parser, args)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 1


def _report_ordinary(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Delegate existing raw, cached, and parse-only interfaces without rewriting metrics."""
    from_csv = getattr(args, "from_csv", None)
    test_parse = getattr(args, "test_parse", None)
    if from_csv and (args.results_dir or test_parse):
        parser.error("--from-csv cannot be combined with raw inputs or --test-parse")
    if not args.results_dir and not (from_csv or test_parse):
        parser.error("results_dir is required unless using --from-csv or --test-parse")
    kind = args.kind
    if kind == "all":
        if from_csv or test_parse:
            parser.error("--from-csv/--test-parse requires --kind io or mdtest")
        kind = (
            "mdtest"
            if Path(args.results_dir).resolve().name.startswith("mdtest-elbencho-")
            else "io"
        )
    options = options_by_kind(args, {kind})[kind]
    engine = "extract-mdtest-elbencho.py" if kind == "mdtest" else "extract-elbencho.py"
    command = [sys.executable, str(_REPO_ROOT / "utils" / engine)]
    if args.results_dir:
        command.append(args.results_dir)
    if args.output_dir:
        command.extend(["--output-dir", args.output_dir])
    return subprocess.run([*command, *options], check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
