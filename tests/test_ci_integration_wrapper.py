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

"""Fault-path tests for the bounded integration lifecycle wrapper."""

import os
import platform
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WRAPPER = _REPO_ROOT / "integration-tests" / "bin" / "ci-integration.sh"


def _fixture(tmp_path: Path) -> tuple[dict[str, str], Path]:
    driver = tmp_path / "fake-driver.py"
    driver.write_text(
        """#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

root = Path(__file__).parent
action = sys.argv[-1]
with (root / "calls.log").open("a", encoding="utf-8") as handle:
    handle.write(f"{action} {os.getpid()}\\n")
if os.environ.get("INTEGRATION_EXPECT_ROOT_REJECTION"):
    raise SystemExit(1)
if action == "test" and (root / "block-test").exists():
    (root / "test.pid").write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(60)
if action == "teardown" and (root / "fail-teardown").exists():
    raise SystemExit(9)
""",
        encoding="utf-8",
    )
    driver.chmod(0o755)
    privilege = tmp_path / "fake-sudo"
    privilege.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    privilege.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "INTEGRATION_DRIVER": str(driver),
            "INTEGRATION_STATE_DIR": str(tmp_path / "state"),
            "INTEGRATION_DIAGNOSTICS": str(tmp_path / "diagnostics"),
            "INTEGRATION_PYTHON": sys.executable,
            "INTEGRATION_PRIVILEGE_COMMAND": str(privilege),
        }
    )
    return environment, driver.parent / "calls.log"


def test_timeout_signals_child_and_runs_teardown(tmp_path):
    """A process-group timeout cannot orphan the active lifecycle child."""
    environment, calls = _fixture(tmp_path)
    (tmp_path / "block-test").touch()
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]

    result = subprocess.run(
        [
            "timeout",
            "--kill-after=5s",
            "1s",
            _WRAPPER,
            architecture,
            "sbx-shared",
        ],
        cwd=_REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 124, result.stderr
    records = calls.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("teardown ") for line in records) == 2
    child_pid = int((tmp_path / "test.pid").read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"timed-out lifecycle child {child_pid} survived")


def test_teardown_failure_makes_successful_lifecycle_fail(tmp_path):
    """The EXIT handler propagates cleanup failure over lifecycle success."""
    environment, calls = _fixture(tmp_path)
    environment.pop("INTEGRATION_PYTHON")
    (tmp_path / "fail-teardown").touch()
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]

    result = subprocess.run(
        [_WRAPPER, architecture, "sbx-shared"],
        cwd=_REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode != 0
    assert (
        sum(
            line.startswith("teardown ")
            for line in calls.read_text(encoding="utf-8").splitlines()
        )
        == 2
    )


def test_unwritable_diagnostics_do_not_prevent_teardown(tmp_path):
    """Cleanup runs twice even when no diagnostics log can be opened."""
    environment, calls = _fixture(tmp_path)
    diagnostics = tmp_path / "diagnostics-file"
    diagnostics.write_text("not a directory\n", encoding="utf-8")
    environment["INTEGRATION_DIAGNOSTICS"] = str(diagnostics)
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]

    result = subprocess.run(
        [_WRAPPER, architecture, "sbx-shared"],
        cwd=_REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    records = calls.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("teardown ") for line in records) == 2
    assert "diagnostics path is unavailable" in result.stderr


def test_unwritable_teardown_logs_do_not_prevent_teardown(tmp_path):
    """Log-open failures fall back to inherited output for both attempts."""
    environment, calls = _fixture(tmp_path)
    diagnostics = tmp_path / "diagnostics"
    diagnostics.mkdir()
    (diagnostics / "teardown-1.log").mkdir()
    (diagnostics / "teardown-2.log").mkdir()
    environment["INTEGRATION_DIAGNOSTICS"] = str(diagnostics)
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]

    result = subprocess.run(
        [_WRAPPER, architecture, "sbx-shared"],
        cwd=_REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    records = calls.read_text(encoding="utf-8").splitlines()
    assert sum(line.startswith("teardown ") for line in records) == 2
    assert result.stderr.count("cannot write teardown log") == 2
