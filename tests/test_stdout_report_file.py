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

"""Unit tests for lib.stdout_report_file."""

import io
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.stdout_report_file import (  # pylint: disable=wrong-import-position
    AppendableStdoutReportFile,
    REPORT_TXT_FILENAME,
    mirror_stdout_to_file,
)


def _sample_report() -> None:
    print("line one")
    print("line two")


def _block_a() -> None:
    print("block_a")


def _block_b() -> None:
    print("block_b")


class TestStdoutReportFile(unittest.TestCase):
    """Tests for tee helpers."""

    def test_mirror_stdout_to_file_matches_stdout(self) -> None:
        buf = io.StringIO()
        with tempfile.NamedTemporaryFile(
            mode="w+", encoding="utf-8", delete=False
        ) as tmp:
            path = tmp.name
        try:
            with unittest.mock.patch("sys.stdout", buf):
                mirror_stdout_to_file(path, _sample_report)

            with open(path, encoding="utf-8") as f:
                file_content = f.read()

            self.assertEqual(file_content, buf.getvalue())
            self.assertIn("line one", file_content)
            self.assertIn("line two", file_content)
        finally:
            os.unlink(path)

    def test_appendable_mirror_multiple_blocks(self) -> None:
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, REPORT_TXT_FILENAME)
            session = AppendableStdoutReportFile(path)
            try:
                with unittest.mock.patch("sys.stdout", buf):
                    session.mirror(_block_a)
                    session.mirror(_block_b)
            finally:
                session.close()

            with open(path, encoding="utf-8") as f:
                combined = f.read()

            self.assertEqual(combined, buf.getvalue())
            self.assertIn("block_a", combined)
            self.assertIn("block_b", combined)
