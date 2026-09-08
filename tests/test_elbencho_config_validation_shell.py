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

"""Shell-level tests for configuration-independent elbencho validation."""

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_BASE = _REPO_ROOT / "lib" / "env_base.sh"
_ENV_FUNCTIONS = _REPO_ROOT / "lib" / "env_functions.sh"


def _run_bash(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", body],
        check=False,
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
    )


class TestElbenchoConfigValidationShell(unittest.TestCase):
    """The shared validator rejects invalid raw configuration early."""

    def test_env_base_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            script = f"""
            set -e
            SCALE_TEST_BASE={str(_REPO_ROOT)!r}
            RESULTS_DIR={str(Path(tmp) / "results")!r}
            LOGS_DIR={str(Path(tmp) / "logs")!r}
            OBJ_AUTH_FILE={str(Path(tmp) / "missing-auth")!r}
            SSH_HOST_LIST=/dev/null
            client_type=cpu
            client_arch=x86_64
            declare -A TEST_DIRS=()
            unset ELBENCHO_FILE_LAYOUT ELBENCHO_FILES_PER_NODE ELBENCHO_FILE_SIZE
            source "{_ENV_BASE}"
            [[ "$ELBENCHO_FILE_LAYOUT" == worker-directories ]]
            [[ -z "$ELBENCHO_FILES_PER_NODE" ]]
            [[ -z "$ELBENCHO_FILE_SIZE" ]]
            [[ "${{WARP_THREAD_LIST[*]}}" == "8 32 64 128 256" ]]
            [[ "${{WARP_OBJ_SIZES[*]}}" == "4MiB 8MiB 16MiB 32MiB 64MiB" ]]
            [[ "$WARP_PUT_DURATION" == 3m ]]
            [[ "$WARP_PUT_MIN_FILES_PER_CLIENT" == 10000 ]]
            [[ "$WARP_GET_DURATION" == 5m ]]
            """
            result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_accepts_defaults_and_valid_shared_directory_values(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        unset ELBENCHO_FILE_LAYOUT ELBENCHO_FILES_PER_NODE ELBENCHO_FILE_SIZE
        validate_elbencho_file_workload_env
        ELBENCHO_FILE_LAYOUT=shared-directory
        ELBENCHO_FILES_PER_NODE=8
        ELBENCHO_FILE_SIZE=64G
        validate_elbencho_file_workload_env
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rejects_invalid_layout_count_pairing_and_size(self) -> None:
        cases = (
            ("invalid", "", "", "ELBENCHO_FILE_LAYOUT"),
            ("worker-directories", "1", "", "requires ELBENCHO_FILE_LAYOUT"),
            ("shared-directory", "0", "", "canonical positive"),
            ("shared-directory", "01", "", "canonical positive"),
            ("shared-directory", "+1", "", "canonical positive"),
            ("shared-directory", "1", "64g", "ELBENCHO_FILE_SIZE"),
            ("shared-directory", "1", "0G", "ELBENCHO_FILE_SIZE"),
        )
        for layout, count, size, expected in cases:
            with self.subTest(layout=layout, count=count, size=size):
                script = f"""
                source "{_ENV_FUNCTIONS}"
                ELBENCHO_FILE_LAYOUT={layout!r}
                ELBENCHO_FILES_PER_NODE={count!r}
                ELBENCHO_FILE_SIZE={size!r}
                validate_elbencho_file_workload_env
                """
                result = _run_bash(textwrap.dedent(script))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_accepts_maximum_shell_integer_and_rejects_larger_count(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        ELBENCHO_FILE_LAYOUT=shared-directory
        ELBENCHO_FILE_SIZE=
        maximum=$(_elbencho_shell_signed_integer_max)
        ELBENCHO_FILES_PER_NODE="$maximum"
        validate_elbencho_file_workload_env
        ELBENCHO_FILES_PER_NODE="${{maximum}}0"
        if error_output=$(validate_elbencho_file_workload_env 2>&1); then
            exit 1
        fi
        grep -q "exceeds this shell's signed integer maximum" <<< "$error_output"
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_integer_arrays_reject_zero_without_overflowing_large_values(self) -> None:
        script = f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        valid=(1 0001 999999999999999999999999999999999999999)
        validate_integer_array valid
        invalid=(1 0 2)
        if error_output=$(validate_integer_array invalid 2>&1); then
            exit 1
        fi
        grep -Fq "invalid[1]='0' is not a positive integer" <<< "$error_output"
        all_zero=(000)
        ! validate_integer_array all_zero >/dev/null 2>&1
        """
        result = _run_bash(textwrap.dedent(script))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
