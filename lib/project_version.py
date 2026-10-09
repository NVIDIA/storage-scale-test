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

"""Project version for Python commands, resolved by lib/project_version.sh."""

import argparse
from collections.abc import Callable
from functools import cache
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
UNKNOWN = "unknown"
_RESOLVER = ROOT / "lib" / "project_version.sh"


def resolve_version(root: Path = ROOT) -> str:
    """Return the version of the project tree at root."""
    try:
        result = subprocess.run(
            ["bash", str(_RESOLVER), "resolve", str(root)],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"Warning: cannot resolve project version: {error}", file=sys.stderr)
        return UNKNOWN
    return result.stdout.strip() or UNKNOWN


@cache
def project_version() -> str:
    """Return this checkout's version, resolved at most once per process."""
    return resolve_version()


def stamp_source(root: Path, version: str) -> None:
    """Write VERSION and SOURCE_SHA256 into a Git-free copy of the project."""
    subprocess.run(["bash", str(_RESOLVER), "stamp", str(root), version], check=True)


class _VersionAction(argparse.Action):
    """Like argparse's version action, but resolves only when requested."""

    def __init__(self, option_strings, dest=argparse.SUPPRESS, **kwargs):
        kwargs.setdefault("help", "show the project version and exit")
        super().__init__(
            option_strings, dest, nargs=0, default=argparse.SUPPRESS, **kwargs
        )

    def __call__(self, parser, namespace, values, option_string=None):
        print(project_version())
        parser.exit()


def add_version_argument(parser: argparse.ArgumentParser) -> None:
    """Add the standard --version option."""
    parser.add_argument("--version", action=_VersionAction)


def exit_for_information(parser_factory: Callable[[], argparse.ArgumentParser]) -> None:
    """Handle help/version with the real parser before optional imports."""
    for argument in sys.argv[1:]:
        if argument == "--":
            break
        if argument in ("-h", "--help", "--version"):
            parser_factory().parse_args()
            break
