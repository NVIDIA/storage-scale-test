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

"""Regression tests for legacy elbencho environment reconstruction."""

import importlib.util
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "utils" / "reconstruct_elbencho_env_used.py"
_SPEC = importlib.util.spec_from_file_location("reconstruct_elbencho_env_used", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


class TestReconstructElbenchoEnvUsed(unittest.TestCase):
    """Both current and legacy sbatch tails preserve sweep mode semantics."""

    def test_current_sbatch_tail_parses_write_no_read(self) -> None:
        text = "sbatch/_nv-elbencho-size-threads-sweep.sh /tmp/results dio 0 0 1M 0 1"

        rows = _MODULE.parse_sbatch_tail(text)

        self.assertEqual(
            rows,
            [("/tmp/results", "dio", 0, 0, "1M", 0, 1, "")],
        )

    def test_legacy_sbatch_tail_defaults_write_no_read_off(self) -> None:
        text = (
            "sbatch/_nv-elbencho-size-threads-sweep.sh "
            "/tmp/results bio 1 0 r4K 0 /mnt/existing"
        )

        rows = _MODULE.parse_sbatch_tail(text)

        self.assertEqual(
            rows,
            [("/tmp/results", "bio", 1, 0, "r4K", 0, 0, "/mnt/existing")],
        )

    def test_yaml_contains_write_no_read(self) -> None:
        rendered = _MODULE.build_yaml(
            {"sweep_write_no_read": 1},
            source_files=[],
            warnings=[],
        )

        self.assertIn("sweep_write_no_read: 1\n", rendered)


if __name__ == "__main__":
    unittest.main()
