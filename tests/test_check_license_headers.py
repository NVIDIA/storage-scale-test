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

"""Tests for the repository license-notice compliance check."""

import tempfile
import unittest
from pathlib import Path

from utils.check_license_headers import check_text, expected_notice, find_violations


class TestCheckText(unittest.TestCase):
    """Validate every supported notice form and placement rule."""

    def test_hash_notice_after_shebang(self) -> None:
        path = Path("script.py")
        text = f"#!/usr/bin/env python3\n\n{expected_notice(path)}\n"

        self.assertIsNone(check_text(path, text))

    def test_markdown_and_c_notice_styles(self) -> None:
        for path in (Path("docs/guide.md"), Path("utils/helper.c")):
            with self.subTest(path=path):
                self.assertIsNone(check_text(path, expected_notice(path)))

    def test_license_is_exempt(self) -> None:
        self.assertIsNone(check_text(Path("LICENSE"), "Apache License\n"))

    def test_malformed_notice_fails(self) -> None:
        path = Path("tool.py")
        text = expected_notice(path).replace("Apache-2.0", "MIT")

        self.assertIn("malformed", check_text(path, text))

    def test_notice_too_late_fails(self) -> None:
        path = Path("tool.py")
        text = "\n".join(["# preamble"] * 5) + "\n" + expected_notice(path)

        self.assertIn("first five lines", check_text(path, text))

    def test_readme_notice_is_required_near_bottom(self) -> None:
        path = Path("README.md")
        notice = expected_notice(path)

        self.assertIsNone(check_text(path, f"# Project\n\n## Copyright\n\n{notice}\n"))
        self.assertIn("bottom", check_text(path, notice + "\n" + "body\n" * 41))


class TestFindViolations(unittest.TestCase):
    """Validate aggregate reporting and binary-file filtering."""

    def test_reports_all_text_failures_and_skips_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            paths = [Path("one.py"), Path("two.md"), Path("image.bin")]
            (root / paths[0]).write_text("print('missing')\n", encoding="utf-8")
            (root / paths[1]).write_text("# Missing\n", encoding="utf-8")
            (root / paths[2]).write_bytes(b"\x00binary")

            violations = find_violations(paths, root)

        self.assertEqual([path for path, _error in violations], paths[:2])


if __name__ == "__main__":
    unittest.main()
