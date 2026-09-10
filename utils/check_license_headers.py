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

"""Check the NVIDIA Apache-2.0 notice on every Git-tracked text file."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_EXEMPT_PATHS = {Path("LICENSE")}
_MARKDOWN_SUFFIXES = {".md"}
_C_SUFFIXES = {".c", ".h"}
_NOTICE_LINES = (
    "SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.",
    "SPDX-License-Identifier: Apache-2.0",
    "",
    'Licensed under the Apache License, Version 2.0 (the "License");',
    "you may not use this file except in compliance with the License.",
    "You may obtain a copy of the License at",
    "",
    "http://www.apache.org/licenses/LICENSE-2.0",
    "",
    "Unless required by applicable law or agreed to in writing, software",
    'distributed under the License is distributed on an "AS IS" BASIS,',
    "WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.",
    "See the License for the specific language governing permissions and",
    "limitations under the License.",
)


def _hash_notice() -> str:
    return "\n".join("#" if not line else f"# {line}" for line in _NOTICE_LINES)


def _markdown_notice() -> str:
    return "<!--\n" + "\n".join(_NOTICE_LINES) + "\n-->"


def _c_notice() -> str:
    body = "\n".join(" *" if not line else f" * {line}" for line in _NOTICE_LINES)
    return f"/*\n{body}\n */"


def _readme_notice() -> str:
    return "\n".join("" if not line else f"    {line}" for line in _NOTICE_LINES)


def expected_notice(path: Path) -> str:
    """Return the required notice rendered for *path*."""
    if path == Path("README.md"):
        return _readme_notice()
    if path == Path("NOTICE"):
        return "\n".join(
            "    " + line if line.startswith("http://") else line
            for line in _NOTICE_LINES
        )
    if path.suffix.lower() in _MARKDOWN_SUFFIXES:
        return _markdown_notice()
    if path.suffix.lower() in _C_SUFFIXES:
        return _c_notice()
    return _hash_notice()


def check_text(path: Path, text: str) -> str | None:
    """Return an error for invalid *text*, or ``None`` when it is compliant."""
    if path in _EXEMPT_PATHS:
        return None
    notice = expected_notice(path)
    position = text.find(notice)
    if position < 0:
        return "missing or malformed NVIDIA Apache-2.0 notice"
    line_number = text.count("\n", 0, position) + 1
    if path == Path("README.md"):
        if line_number <= max(1, len(text.splitlines()) - 40):
            return "README notice must remain in its Copyright section at the bottom"
    elif line_number > 5:
        return "notice must begin within the first five lines"
    return None


def tracked_paths(repo_root: Path = _REPO_ROOT) -> list[Path]:
    """Return repository-relative paths tracked by Git."""
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )
    return [Path(item.decode()) for item in result.stdout.split(b"\0") if item]


def find_violations(
    paths: list[Path], repo_root: Path = _REPO_ROOT
) -> list[tuple[Path, str]]:
    """Return all header violations among *paths*, skipping binary files."""
    violations: list[tuple[Path, str]] = []
    for path in paths:
        absolute_path = repo_root / path
        if absolute_path.is_symlink():
            continue
        data = absolute_path.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        error = check_text(path, text)
        if error:
            violations.append((path, error))
    return violations


def main() -> int:
    """Check all tracked text files and report every violation."""
    violations = find_violations(tracked_paths())
    if not violations:
        print("All Git-tracked text files have the required license notice.")
        return 0
    for path, error in violations:
        print(f"{path}: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
