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

"""Build the stamped source archive for a release tag; optionally publish it."""

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# pylint: disable=wrong-import-position
from lib.source_release import (
    build_source_archive,
    publish_release,
    release_commit,
    release_notes,
    smoke_source_archive,
)


def main():
    """Script entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="Annotated release tag.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "tmp/source-release")
    parser.add_argument(
        "--smoke", action="store_true", help="Verify the extracted archive."
    )
    parser.add_argument(
        "--publish",
        metavar="OWNER/REPO",
        help="Publish the GitHub release with the GitHub CLI.",
    )
    args = parser.parse_args()
    try:
        archive, checksum = build_source_archive(ROOT, args.output_dir, args.tag)
        if args.smoke:
            smoke_source_archive(archive)
        if args.publish:
            publish_release(
                args.publish,
                args.tag,
                release_commit(ROOT, args.tag),
                (archive, checksum),
                release_notes(ROOT, args.tag),
            )
    except ValueError as error:
        parser.exit(1, f"Error: {error}\n")
    print(archive)
    print(checksum)


if __name__ == "__main__":
    main()
