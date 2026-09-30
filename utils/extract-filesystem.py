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


def main() -> int:
    """Select independent groups, or delegate an ordinary directory unchanged."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results_dir")
    parser.add_argument("--kind", choices=KINDS, default="all")
    parser.add_argument("--groups", help="Comma-separated four-digit group IDs")
    parser.add_argument("--output-dir", help="Default: RESULTS_DIR/reports for batches")
    args = parser.parse_args()
    try:
        reject_batch_descendant_inputs([args.results_dir])
        if is_batch_directory(args.results_dir):
            return report_batch(
                args.results_dir, args.kind, args.groups, args.output_dir
            )
        if args.groups:
            parser.error("--groups requires a prepared batch input")
        kind = args.kind
        if kind == "all":
            kind = (
                "mdtest"
                if Path(args.results_dir).resolve().name.startswith("mdtest-elbencho-")
                else "io"
            )
        engine = (
            "extract-mdtest-elbencho.py" if kind == "mdtest" else "extract-elbencho.py"
        )
        command = [sys.executable, str(_REPO_ROOT / "utils" / engine), args.results_dir]
        if args.output_dir:
            command.extend(["--output-dir", args.output_dir])
        return subprocess.run(command, check=False).returncode
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 1


if __name__ == "__main__":
    sys.exit(main())
