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

"""Tests for lib.parse_only_sizes."""

import sys
import unittest
from pathlib import Path

# Repo root: .../storage-scale-test/tests/ -> parent
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.parse_only_sizes import (  # pylint: disable=wrong-import-position
    parse_only_sizes_arg,
)


class TestParseOnlySizesArg(unittest.TestCase):
    """parse_only_sizes_arg behavior."""

    def test_none_returns_none(self):
        self.assertIsNone(parse_only_sizes_arg(None))

    def test_empty_list_returns_none(self):
        self.assertIsNone(parse_only_sizes_arg([]))

    def test_compound_comma_single_token(self):
        self.assertEqual(
            parse_only_sizes_arg(["1M,r64K"]),
            {"1M,r64K"},
        )

    def test_semicolon_two_entries_one_arg(self):
        self.assertEqual(
            parse_only_sizes_arg(["4K;1M"]),
            {"4K", "1M"},
        )

    def test_multiple_append_flags(self):
        self.assertEqual(
            parse_only_sizes_arg(["1M,r64K", "4K"]),
            {"1M,r64K", "4K"},
        )

    def test_semicolon_with_compound(self):
        self.assertEqual(
            parse_only_sizes_arg(["1M,r64K;4K,8K"]),
            {"1M,r64K", "4K,8K"},
        )

    def test_whitespace_trimmed(self):
        self.assertEqual(
            parse_only_sizes_arg(["  4K  ;  1M  "]),
            {"4K", "1M"},
        )

    def test_all_blank_returns_none(self):
        self.assertIsNone(parse_only_sizes_arg(["", "   "]))


if __name__ == "__main__":
    unittest.main()
