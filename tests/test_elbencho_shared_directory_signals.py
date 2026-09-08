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

"""Signal and identity tests for generated-target cleanup."""

import os
import signal
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FUNCTIONS = _REPO_ROOT / "lib" / "_elbencho_functions.sh"


def _identity_script(root: Path, target: Path, execution_id: str = "0001") -> str:
    return textwrap.dedent(f"""
        source "{_FUNCTIONS}"
        ELBENCHO_FILE_LAYOUT=shared-directory
        ELBENCHO_FILES_PER_NODE=1
        ELBENCHO_SWEEP_READ_FROM=
        ELBENCHO_RUN_EXECUTION_ID={execution_id}
        ELBENCHO_RUN_GENERATED_TEST_ROOT={root!s}
        ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV={target!s}
        ELBENCHO_RUN_TEST_DIR_SUFFIX=-DS-e{execution_id}
        """)


class TestElbenchoSharedDirectorySignals(unittest.TestCase):
    """Cleanup is safe, idempotent, and signal-aware."""

    def test_cleanup_identity_rejects_root_staged_and_wrong_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            root.mkdir()
            cases = (
                (root, "0001", ""),
                (root / "target-DS-e0001", "0001", str(root / "staged")),
                (root / "target-DS-e0002", "0001", ""),
            )
            for target, execution_id, staged in cases:
                target.mkdir(exist_ok=True)
                script = _identity_script(root, target, execution_id)
                script += f"ELBENCHO_SWEEP_READ_FROM={staged!s}\n"
                script += "! _elbencho_remove_captured_shared_target\n"
                result = subprocess.run(
                    ["bash", "-c", script],
                    cwd=_REPO_ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(target.is_dir())

    def test_cleanup_is_idempotent_for_exact_captured_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            target = root / "target-DS-e0001"
            target.mkdir(parents=True)
            script = _identity_script(root, target)
            script += "_elbencho_remove_captured_shared_target\n"
            script += "_elbencho_remove_captured_shared_target\n"
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=_REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(target.exists())

    def test_disarm_does_not_activate_inherited_exit_trap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record = Path(temp_dir) / "exit-record"
            script = textwrap.dedent(f"""
                source "{_FUNCTIONS}"
                trap 'printf "owner\\n" >> "$RECORD"' EXIT
                (
                    _elbencho_shared_arm_cleanup
                    _elbencho_shared_disarm_cleanup
                )
                [[ ! -e "$RECORD" ]]
                """)
            env = os.environ.copy()
            env["RECORD"] = str(record)
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=_REPO_ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(record.read_text(encoding="utf-8"), "owner\n")

    def test_term_runs_cleanup_and_preserves_signal_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            target = root / "target-DS-e0001"
            target.mkdir(parents=True)
            script = _identity_script(root, target)
            script += textwrap.dedent("""
                _elbencho_shared_arm_cleanup
                printf 'ready\n'
                while :; do sleep 1; done
                """)
            with subprocess.Popen(
                ["bash", "-c", script],
                cwd=_REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as process:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                process.send_signal(signal.SIGTERM)
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 143, stderr)
            self.assertFalse(target.exists())

    def test_sigkill_leaves_retry_cleanup_as_backstop(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "root"
            target = root / "target-DS-e0001"
            target.mkdir(parents=True)
            script = _identity_script(root, target)
            script += textwrap.dedent("""
                _elbencho_shared_arm_cleanup
                printf 'ready\n'
                while :; do sleep 1; done
                """)
            with subprocess.Popen(
                ["bash", "-c", script],
                cwd=_REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            ) as process:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)
                self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertTrue(target.is_dir())

            retry = subprocess.run(
                [
                    "bash",
                    "-c",
                    _identity_script(root, target)
                    + "_elbencho_remove_captured_shared_target\n",
                ],
                cwd=_REPO_ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
