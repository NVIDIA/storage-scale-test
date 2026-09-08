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

"""Regression tests for JSON context redaction."""

import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# pylint: disable=wrong-import-position
from utils.compress_json_for_context import format_value


class TestFormatValue(unittest.TestCase):
    """Sensitive values must be redacted before presentation transforms."""

    def test_long_sensitive_value_is_fully_redacted(self) -> None:
        value = "Authorization: Bearer token=" + "a" * 120

        self.assertEqual(format_value(value), '"*REDACTED*"')

    def test_short_sensitive_value_is_fully_redacted(self) -> None:
        self.assertEqual(format_value("password=hunter2"), '"*REDACTED*"')

    def test_long_non_sensitive_value_is_truncated(self) -> None:
        formatted = format_value("a" * 120)

        self.assertNotEqual(formatted, '"*REDACTED*"')
        self.assertTrue(formatted.endswith('..."'))


if __name__ == "__main__":
    unittest.main()
