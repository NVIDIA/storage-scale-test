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

"""Driver wiring tests for local chart acquisition and webhook recovery."""

# This module intentionally tests private integration-driver boundaries.
# pylint: disable=protected-access

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INTEGRATION_LIB = _REPO_ROOT / "integration-tests" / "lib"
_DRIVER_PATH = _REPO_ROOT / "integration-tests" / "bin" / "integration-test.py"
sys.path.insert(0, str(_INTEGRATION_LIB))
_SPEC = importlib.util.spec_from_file_location(
    "integration_slinky_chart_wiring_under_test", _DRIVER_PATH
)
assert _SPEC and _SPEC.loader
_DRIVER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _DRIVER
_SPEC.loader.exec_module(_DRIVER)


class _Runner:
    """Record driver commands and return configured Helm results."""

    def __init__(self, results=(), events=None):
        self.results = list(results)
        self.calls = []
        self.events = events if events is not None else []

    def run(self, arguments, *, check=True, timeout=None, cwd=None):
        command = [str(argument) for argument in arguments]
        self.calls.append((command, check, timeout, cwd))
        self.events.append(("command", command))
        if self.results:
            return self.results.pop(0)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_all_slinky_charts_are_acquired_before_local_chart_installs(
    tmp_path, monkeypatch
):
    """All OCI acquisition finishes before the first cluster-changing upgrade."""
    events = []
    paths = {}

    def ensure_chart(_runner, state_dir, reference, version):
        chart_name = reference.rsplit("/", 1)[-1]
        path = tmp_path / "cached-charts" / f"{chart_name}-{version}.tgz"
        paths[chart_name] = path
        events.append(("acquire", chart_name, state_dir, reference, version))
        return path

    monkeypatch.setattr(_DRIVER, "ensure_chart", ensure_chart)
    monkeypatch.setattr(
        _DRIVER, "_render_resource", lambda *_args: tmp_path / "values.yaml"
    )
    monkeypatch.setattr(
        _DRIVER, "_resource_path", lambda *_args: tmp_path / "operator.yaml"
    )
    monkeypatch.setattr(_DRIVER, "_kubectl", lambda _config, *args: ["kubectl", *args])
    runner = _Runner(events=events)
    config = SimpleNamespace(
        state_dir=tmp_path / "state",
        namespace="fixture",
        kubeconfig=tmp_path / "kubeconfig",
    )

    _DRIVER._helm_slinky(runner, config)

    upgrades = [event for event in runner.calls if event[0][:2] == ["helm", "upgrade"]]
    acquisitions = [event for event in events if event[0] == "acquire"]
    assert [event[1] for event in acquisitions] == [
        "slurm-operator-crds",
        "slurm-operator",
        "slurm",
    ]
    first_upgrade_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "command" and event[1][:2] == ["helm", "upgrade"]
    )
    assert first_upgrade_index == 3
    assert len(upgrades) == 3
    assert all(call[0][4] == str(paths[call[0][3]]) for call in upgrades)
    assert all("oci://" not in " ".join(call[0]) for call in upgrades)


def test_second_chart_acquisition_failure_prevents_all_helm_upgrades(
    tmp_path, monkeypatch
):
    """The release loop starts only after every chart has been acquired."""
    attempts = []

    def fail_second_chart(_runner, _state_dir, reference, _version):
        chart_name = reference.rsplit("/", 1)[-1]
        attempts.append(chart_name)
        if len(attempts) == 2:
            raise _DRIVER.image_acquisition.AcquisitionError("chart pull failed")
        return tmp_path / f"{chart_name}.tgz"

    monkeypatch.setattr(_DRIVER, "ensure_chart", fail_second_chart)
    monkeypatch.setattr(
        _DRIVER, "_render_resource", lambda *_args: tmp_path / "values.yaml"
    )
    monkeypatch.setattr(
        _DRIVER, "_resource_path", lambda *_args: tmp_path / "operator.yaml"
    )
    runner = _Runner()
    config = SimpleNamespace(
        state_dir=tmp_path / "state",
        namespace="fixture",
        kubeconfig=tmp_path / "kubeconfig",
    )

    try:
        _DRIVER._helm_slinky(runner, config)
    except _DRIVER.ProvisionError as error:
        assert "chart pull failed" in str(error)
    else:
        raise AssertionError("chart acquisition failure was not reported")

    assert attempts == ["slurm-operator-crds", "slurm-operator"]
    assert not any(call[0][:2] == ["helm", "upgrade"] for call in runner.calls)


def test_webhook_startup_retry_remains_available(monkeypatch):
    """The targeted one-time retry for an unready webhook is preserved."""
    monkeypatch.setattr(_DRIVER.time, "sleep", lambda _delay: None)
    runner = _Runner(
        [
            SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="failed calling webhook: connection refused",
            ),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
    )

    _DRIVER._install_slinky_release(runner, ["helm", "upgrade"], "slurm")

    assert len(runner.calls) == 2
    assert all(call[0] == ["helm", "upgrade"] for call in runner.calls)
