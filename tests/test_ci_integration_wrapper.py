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

import json
import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_WRAPPER = _REPO_ROOT / "integration-tests" / "bin" / "ci-integration.sh"


def test_integration_workflow_bootstraps_its_lifecycle_interpreter():
    """Fresh runners cannot borrow runtime packages from the unit-test venv."""
    workflow = yaml.safe_load(
        (_REPO_ROOT / ".github/workflows/integration.yml").read_text(encoding="utf-8")
    )
    job = workflow["jobs"]["integration"]
    assert job["env"]["INTEGRATION_PYTHON"] == (
        "${{ github.workspace }}/.venv/bin/python"
    )
    steps = job["steps"]
    bootstrap_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Install integration Python dependencies"
    )
    lifecycle_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Run the bounded integration lifecycle"
    )
    assert bootstrap_index < lifecycle_index
    commands = [
        shlex.split(line) for line in steps[bootstrap_index]["run"].splitlines()
    ]
    assert commands == [
        ["python3", "-m", "venv", ".venv"],
        ["$INTEGRATION_PYTHON", "-m", "pip", "install", "-r", "requirements.txt"],
        ["$INTEGRATION_PYTHON", "integration-tests/bin/integration-test.py", "--help"],
    ]
    assert "INTEGRATION_PYTHON" not in steps[lifecycle_index].get("env", {})


def _fixture(tmp_path: Path) -> tuple[dict[str, str], Path]:
    driver = tmp_path / "fake-driver.py"
    driver.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

root = Path(__file__).parent
arguments = sys.argv[sys.argv.index("--storage-backend") + 2 :]
action = arguments[0]
with (root / "calls.log").open("a", encoding="utf-8") as handle:
    handle.write(" ".join([*arguments, str(os.getpid())]) + "\\n")
if os.environ.get("INTEGRATION_EXPECT_ROOT_REJECTION"):
    raise SystemExit(1)
if action == "setup" and (root / "fail-setup").exists():
    raise SystemExit(5)
if action == "test":
    run = Path(os.environ["INTEGRATION_STATE_DIR"]) / "test-runs" / "99991231T235959Z-1"
    run.mkdir(parents=True, exist_ok=True)
    planned = (root / "planned").read_text(encoding="utf-8").split()
    items = [{"work_item": item, "outcome": "passed", "seconds": 1.0} for item in planned]
    (run / "work-items.json").write_text(
        json.dumps({"planned": planned, "items": items}), encoding="utf-8"
    )
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
            "INTEGRATION_MANIFEST_DIR": str(tmp_path / "manifests"),
            "INTEGRATION_PYTHON": sys.executable,
            "INTEGRATION_PRIVILEGE_COMMAND": str(privilege),
        }
    )
    (tmp_path / "planned").write_text("baseline/ssh\n", encoding="utf-8")
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


def _architecture() -> str:
    return {"x86_64": "amd64", "aarch64": "arm64"}[platform.machine()]


def _run_wrapper(environment, *arguments):
    return subprocess.run(
        [_WRAPPER, _architecture(), "sbx-shared", *arguments],
        cwd=_REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_substrate_shard_keeps_the_full_lifecycle_and_writes_a_manifest(tmp_path):
    """A shard runs every lifecycle call and tests only its substrate."""
    environment, calls = _fixture(tmp_path)
    planned = subprocess.run(
        [sys.executable, _REPO_ROOT / "integration-tests/lib/shard_manifest.py"]
        + ["plan", "kubectl"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (tmp_path / "planned").write_text(planned, encoding="utf-8")

    result = _run_wrapper(environment, "kubectl")

    assert result.returncode == 0, result.stderr
    actions = [line.split()[:-1] for line in calls.read_text().splitlines()]
    assert actions == [
        ["setup"],
        ["setup"],
        ["stop"],
        ["start"],
        ["setup"],
        ["test"],
        ["test", "--substrate", "kubectl"],
        ["teardown"],
        ["teardown"],
    ]
    manifest = json.loads(
        (tmp_path / "manifests" / f"{_architecture()}-kubectl.json").read_text()
    )
    assert manifest["architecture"] == _architecture()
    assert manifest["backend"] == "sbx-shared"
    assert manifest["substrate"] == "kubectl"
    assert manifest["exit_code"] == 0
    assert manifest["planned"] == planned.split()
    assert manifest["completed"] == planned.split()
    assert [timing["step"] for timing in manifest["timings"]] == [
        "setup-1",
        "setup-2",
        "stop",
        "start",
        "root-setup",
        "root-test",
        "test",
        "teardown-1",
        "teardown-2",
    ]


def test_default_selector_tests_all_substrates(tmp_path):
    """The unsharded interface is unchanged."""
    environment, calls = _fixture(tmp_path)
    result = _run_wrapper(environment)
    assert result.returncode == 0, result.stderr
    assert "test --substrate all" in calls.read_text()
    assert (tmp_path / "manifests" / f"{_architecture()}-all.json").is_file()


def test_unknown_substrate_is_rejected_before_provisioning(tmp_path):
    """A mistyped shard selector cannot fall back to testing everything."""
    environment, calls = _fixture(tmp_path)
    result = _run_wrapper(environment, "nfs-ssh")
    assert result.returncode == 1
    assert "unsupported integration substrate" in result.stderr
    assert not calls.exists()


def test_failed_setup_still_records_the_attempted_shard(tmp_path):
    """The status job can tell an attempted, failed shard from a missing one."""
    environment, calls = _fixture(tmp_path)
    (tmp_path / "fail-setup").touch()
    result = _run_wrapper(environment, "slurm")
    assert result.returncode == 5
    assert (
        sum(line.startswith("teardown ") for line in calls.read_text().splitlines())
        == 2
    )
    manifest = json.loads(
        (tmp_path / "manifests" / f"{_architecture()}-slurm.json").read_text()
    )
    assert manifest["exit_code"] == 5
    assert manifest["completed"] == []
    assert manifest["planned"]
