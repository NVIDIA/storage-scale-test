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
    RESULTS_FILENAME,
    is_batch_directory,
    reject_batch_descendant_inputs,
    report_batch,
    report_documents,
    report_mixed_directories,
)
from lib.filesystem_report_options import (  # pylint: disable=wrong-import-position
    COMPARISONS_FLAG,
    FROM_JSON_FLAG,
    check_replay_options,
    options_by_kind,
    validate_from_json,
    validate_report_inputs,
)
from lib.filesystem_results import (  # pylint: disable=wrong-import-position
    load_comparisons,
)
from lib.results_document import (  # pylint: disable=wrong-import-position
    load_document,
    recorded_options,
)


def main(argv: list[str] | None = None) -> int:
    """Select independent groups, or delegate ordinary directories unchanged."""
    parser = extract_filesystem_parser()
    # Result directories may come before, between or after options.
    args = parser.parse_intermixed_args(argv)
    try:
        load_comparisons(args.comparisons)
        if args.from_json:
            return _report_document(args)
        return _report_directories(parser, args)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 1


def _report_directories(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> int:
    """Report one batch or one or more ordinary result directories."""
    directories = args.results_dirs
    reject_batch_descendant_inputs(directories)
    if len(directories) > 1:
        _validate_several(directories, args.output_dir)
    elif directories and is_batch_directory(directories[0]):
        return report_batch(
            directories[0],
            args.kind,
            args.groups,
            args.output_dir,
            analysis_args=args,
        )
    if args.groups:
        parser.error("--groups requires a prepared batch input")
    return _report_ordinary(parser, args)


def _validate_several(directories: list[str], output_dir: str | None) -> None:
    """Several inputs must be ordinary result directories with their own output."""
    validate_report_inputs(directories, output_dir)
    for directory in directories:
        if is_batch_directory(directory):
            raise ValueError(
                f"{directory} is a batch; batches cannot be combined with other "
                "inputs. Report each batch separately, then combine them with "
                f"{FROM_JSON_FLAG} <batch>/reports/{RESULTS_FILENAME} for each"
            )


def _report_document(args: argparse.Namespace) -> int:
    """Render results documents (see ``report_documents``)."""
    validate_from_json(args.from_json, args.results_dirs, args.output_dir)
    documents = [load_document(path) for path in args.from_json]
    check_replay_options(args, [recorded_options(loaded.data) for loaded in documents])
    return report_documents(documents, args, args.kind)


def _named_kind(directory: str) -> str | None:
    """``io`` or ``mdtest`` from a result directory's name; None if renamed."""
    name = Path(directory).resolve().name
    if name.startswith("mdtest-elbencho-"):
        return "mdtest"
    return "io" if name.startswith("elbencho-") else None


def _kinds(directories: list[str], kind: str) -> dict[str, list[str]]:
    """The directories by workload, IO first. ``all`` reads each kind from
    its name (renamed ones are IO); an explicit kind takes every directory
    not named as the other kind."""
    if kind != "all":
        return {kind: [d for d in directories if _named_kind(d) in (kind, None)]}
    kinds: dict[str, list[str]] = {"io": [], "mdtest": []}
    for item in directories:
        kinds[_named_kind(item) or "io"].append(item)
    return {name: items for name, items in kinds.items() if items}


def _report_ordinary(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    """Delegate existing raw and parse-only interfaces without rewriting metrics."""
    test_parse = getattr(args, "test_parse", None)
    if not args.results_dirs and not test_parse:
        parser.error("RESULTS_DIR is required unless using --test-parse")
    if test_parse and args.kind == "all":
        parser.error("--test-parse requires --kind io or mdtest")
    kinds = _kinds(args.results_dirs, args.kind)
    if len(kinds) > 1:
        return report_mixed_directories(kinds, args)
    kind, directories = next(iter(kinds.items()), (args.kind, []))
    if args.results_dirs and not directories:
        parser.error(f"--kind {kind}: no {kind} result directories among the inputs")
    options = options_by_kind(args, {kind})[kind]
    engine = "extract-mdtest-elbencho.py" if kind == "mdtest" else "extract-elbencho.py"
    command = [sys.executable, str(_REPO_ROOT / "utils" / engine)]
    command.extend(directories)
    if args.output_dir:
        command.extend(["--output-dir", args.output_dir])
    if args.comparisons:
        command.extend([COMPARISONS_FLAG, args.comparisons])
    return subprocess.run([*command, *options], check=False).returncode


if __name__ == "__main__":
    sys.exit(main())
