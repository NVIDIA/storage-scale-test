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

"""Small real-binary probes for shared-directory elbencho semantics."""

import json
import platform
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ELBENCHO = _REPO_ROOT / "tmp" / "elbencho.aarch64"
_FUNCTIONS = _REPO_ROOT / "lib" / "_elbencho_functions.sh"
_CAN_RUN_BINARY = platform.machine() == "aarch64" and _ELBENCHO.is_file()
_SVCSTATS = "--svcstats=elapsed,lat,percent,minmax"


def _parse_phase_json(path: Path, phase: str) -> str:
    """The shell completion parser's canonical output for one phase JSON."""
    script = (
        f"source {shlex.quote(str(_FUNCTIONS))}; "
        f"_elbencho_parse_phase_json {shlex.quote(str(path))} {phase}"
    )
    result = subprocess.run(
        ["bash", "-c", script], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@unittest.skipUnless(_CAN_RUN_BINARY, "requires the local aarch64 probe binary")
class TestElbenchoSharedDirectoryBinary(unittest.TestCase):
    """Verify the native behavior relied on by the shell translation."""

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(_ELBENCHO), *args],
            cwd=_REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_zero_directory_mkdir_writes_one_phase_object(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            json_path = root / "mkdir.json"
            result = self._run(
                "--mkdirs",
                "--dirs=0",
                "--files=1",
                "--threads=1",
                "--size=4K",
                "--block=4K",
                f"--jsonfile={json_path}",
                _SVCSTATS,
                "--nolive",
                str(target),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(target.iterdir()), [])
            rows = json_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                [json.loads(row)["phase_type"] for row in rows], ["MKDIRS"]
            )

    def test_one_worker_writes_four_flat_files_with_exact_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            json_path = root / "write.json"
            result = self._run(
                "--write",
                "--sync",
                "--dirs=0",
                "--files=4",
                "--threads=1",
                "--size=4K",
                "--block=4K",
                f"--jsonfile={json_path}",
                _SVCSTATS,
                "--nolive",
                str(target),
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(
                _parse_phase_json(json_path, "WRITE"), r"^4\t16384\t[0-9]+$"
            )
            self.assertEqual(
                sorted(path.name for path in target.iterdir()),
                ["r0-f0", "r0-f1", "r0-f2", "r0-f3"],
            )
            self.assertTrue(
                all(path.stat().st_size == 4096 for path in target.iterdir())
            )
            records = [
                json.loads(line)
                for line in json_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            writes = [record for record in records if record["phase_type"] == "WRITE"]
            self.assertEqual(len(writes), 1)
            self.assertEqual(writes[0]["last_done"]["entries"], "4")
            self.assertEqual(writes[0]["last_done"]["bytes"], "16384")
            self.assertTrue(
                all(
                    record["phase_type"] == "SYNC"
                    for record in records
                    if record not in writes
                )
            )

            delete_json = root / "delete.json"
            delete_result = self._run(
                "--delfiles",
                "--dirs=0",
                "--files=4",
                "--threads=1",
                f"--jsonfile={delete_json}",
                _SVCSTATS,
                "--nolive",
                str(target),
            )
            self.assertEqual(delete_result.returncode, 0, delete_result.stderr)
            self.assertRegex(
                _parse_phase_json(delete_json, "RMFILES"), r"^4\tnull\t[0-9]+$"
            )
            self.assertEqual(list(target.iterdir()), [])
            delete_records = [
                json.loads(line)
                for line in delete_json.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(delete_records), 1)
            self.assertEqual(delete_records[0]["phase_type"], "RMFILES")
            last_done = delete_records[0]["last_done"]
            self.assertEqual(last_done["entries"], "4")
            self.assertRegex(last_done["elapsed_time_ms"], r"^(?:0|[1-9][0-9]*)$")
            self.assertNotIn("bytes", last_done)


if __name__ == "__main__":
    unittest.main()
