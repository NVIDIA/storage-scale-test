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

"""Tests for bounded datestamp joins used in output filenames."""

import unittest

from lib.join_datestamps import (
    join_datestamps,
    join_datestamps_for_filename,
)


class TestJoinDatestamps(unittest.TestCase):
    """Datestamp titles stay complete while path components stay bounded."""

    def test_full_join_is_sorted(self) -> None:
        stamps = {"20260802Z000000", "20260801Z000000"}

        self.assertEqual(
            join_datestamps(stamps),
            "20260801Z000000-20260802Z000000",
        )

    def test_long_join_is_abbreviated(self) -> None:
        stamps = {f"202608{day:02d}Z000000" for day in range(1, 10)}

        joined = join_datestamps(stamps, max_len=60)

        self.assertEqual(
            joined,
            "20260801Z000000-to-20260809Z000000-n9",
        )
        self.assertLessEqual(len(joined), 60)

    def test_filename_join_respects_component_limit(self) -> None:
        stamps = {f"202608{day:02d}Z000000" for day in range(1, 10)}
        prefix = "elbencho-mn-read-r64K-dio-throughput-"

        joined = join_datestamps_for_filename(
            stamps,
            prefix=prefix,
            max_filename_len=100,
        )

        self.assertLessEqual(len(prefix + joined + ".png"), 100)

    def test_impossible_filename_budget_raises(self) -> None:
        with self.assertRaises(ValueError):
            join_datestamps_for_filename(
                {"20260801Z000000"},
                prefix="x" * 255,
            )


if __name__ == "__main__":
    unittest.main()
