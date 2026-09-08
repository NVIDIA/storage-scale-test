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

"""Tests for elbencho live CSV shell argument and validation helpers."""

import subprocess
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ELBENCHO_FUNCTIONS = _REPO_ROOT / "lib" / "_elbencho_functions.sh"
_ENV_FUNCTIONS = _REPO_ROOT / "lib" / "env_functions.sh"


def _run_bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", script],
        check=False,
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
    )


def _common_args_script(extra_env: str = "") -> str:
    return f"""
source "{_ELBENCHO_FUNCTIONS}"
thread_count=8
io_depth=4
dio_or_bio=dio
ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0
{extra_env}
_elbencho_io_build_common_args \
    /tmp/result.out /tmp/result.csv /tmp/result.live.csv 1G
printf '%s\\n' "${{common_args[@]}}"
"""


class TestElbenchoLiveCsvShell(unittest.TestCase):
    """Live CSV naming and argv gating."""

    def test_disabled_keeps_existing_argv(self):
        result = _run_bash(_common_args_script())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "--threads=8",
                "--size=1G",
                "--sync",
                "--lat",
                "--lathisto",
                "--latpercent",
                "--nolive",
                "--resfile=/tmp/result.out",
                "--csvfile=/tmp/result.csv",
                "--iodepth=4",
                "--direct",
            ],
        )

    def test_extended_capture_uses_configured_interval(self):
        result = _run_bash(
            _common_args_script("ELBENCHO_LIVE_CSV_EXTENDED=1\nELBENCHO_LIVEINT=750")
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        args = result.stdout.splitlines()
        self.assertIn("--livecsv=/tmp/result.live.csv", args)
        self.assertIn("--livecsvex", args)
        self.assertIn("--liveint=750", args)

    def test_extended_capture_defaults_to_one_second(self):
        result = _run_bash(_common_args_script("ELBENCHO_LIVE_CSV_EXTENDED=1"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--liveint=1000", result.stdout.splitlines())
        self.assertIn("--livecsvex", result.stdout.splitlines())

    def test_live_csv_filename_matches_result_siblings(self):
        script = f"""
source "{_ELBENCHO_FUNCTIONS}"
_elbencho_io_set_livecsvfile /results r64K 12 8 4 20260729Z120000
printf '%s\\n' "$livecsvfile"
"""
        result = _run_bash(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "/results/elbencho-r64K-c_012-s_008-d_004_20260729Z120000.live.csv",
        )

    def test_validation_rejects_nonpositive_interval(self):
        script = f"""
source "{_ENV_FUNCTIONS}"
ELBENCHO_LIVE_CSV_EXTENDED=1
ELBENCHO_LIVEINT=0
validate_elbencho_live_csv
"""
        result = _run_bash(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be a positive integer", result.stderr)

    def test_validation_accepts_unset_defaults(self):
        result = _run_bash(f'source "{_ENV_FUNCTIONS}"; validate_elbencho_live_csv')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
