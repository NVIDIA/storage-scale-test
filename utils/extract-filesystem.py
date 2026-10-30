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

# Bootstrap must precede runtime dependencies, including imports for plotting.
# pylint: disable=wrong-import-position,wrong-import-order
from lib.report_cli import extract_filesystem_parser, prepare_report_runtime

if __name__ == "__main__":
    prepare_report_runtime(__file__, extract_filesystem_parser)

from lib.filesystem_batch import (  # pylint: disable=wrong-import-position
    is_batch_directory,
    reject_batch_descendant_inputs,
    report_batch,
    report_documents,
)
from lib.filesystem_report_options import (  # pylint: disable=wrong-import-position
    COMPARISONS_FLAG,
    check_replay_options,
    options_by_kind,
    validate_from_json,
)
from lib.filesystem_results import (  # pylint: disable=wrong-import-position
    load_comparisons,
)
from lib.results_document import (  # pylint: disable=wrong-import-position
    load_document,
    recorded_options,
)


def main(argv: list[str] | None = None) -> int:
    """Select independent groups, or delegate an ordinary directory unchanged."""
    parser = extract_filesystem_parser()
    args = parser.parse_args(argv)
    try:
        load_comparisons(args.comparisons)
        if args.from_json:
            return _report_document(args)
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


def _report_document(args: argparse.Namespace) -> int:
    """Render results documents (see ``report_documents``)."""
    validate_from_json(
        args.from_json, [args.results_dir] if args.results_dir else [], args.output_dir
    )
    documents = [load_document(path) for path in args.from_json]
    check_replay_options(args, [recorded_options(loaded.data) for loaded in documents])
    return report_documents(documents, args, args.kind)


def _report_ordinary(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Delegate existing raw and parse-only interfaces without rewriting metrics."""
    test_parse = getattr(args, "test_parse", None)
    if not args.results_dir and not test_parse:
        parser.error("results_dir is required unless using --test-parse")
    kind = args.kind
    if kind == "all":
        if test_parse:
            parser.error("--test-parse requires --kind io or mdtest")
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
    if args.comparisons:
        command.extend([COMPARISONS_FLAG, args.comparisons])
    return subprocess.run([*command, *options], check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
