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

"""Safety and rollout regression tests for the integration driver."""

import importlib.util
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DRIVER_PATH = _REPO_ROOT / "integration-tests" / "bin" / "integration-test.py"
_SPEC = importlib.util.spec_from_file_location(
    "integration_driver_under_test", _DRIVER_PATH
)
assert _SPEC and _SPEC.loader
_DRIVER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _DRIVER
_SPEC.loader.exec_module(_DRIVER)
_FILESYSTEM = sys.modules["filesystem_integration"]
_ensure_export_marker = getattr(_DRIVER, "_ensure_export_marker")
_login_pod = getattr(_DRIVER, "_login_pod")
_select_storage_backend = getattr(_DRIVER, "_select_storage_backend")
_storage_backend_document = getattr(_DRIVER, "_storage_backend_document")
_validate_lifecycle_paths = getattr(_DRIVER, "_validate_lifecycle_paths")
_ensure_elbencho = getattr(_FILESYSTEM, "_ensure_elbencho")
_pods_with_container = getattr(_FILESYSTEM, "_pods_with_container")


def test_scenario_listing_short_circuits_before_privileged_state(monkeypatch, capsys):
    """Listing scenarios needs no account, state, fixture, or root exception."""
    monkeypatch.setattr(
        sys,
        "argv",
        [str(_DRIVER_PATH), "test", "--list-scenarios"],
    )
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        _DRIVER,
        "_config",
        lambda _arguments: pytest.fail("scenario listing resolved an account"),
    )

    assert _DRIVER.main() == 0
    assert capsys.readouterr().out.startswith("baseline\t")


def _config(state_dir: Path, export_dir: Path) -> object:
    """Return a minimal real driver configuration."""
    return _DRIVER.Config(
        cluster_name="test-cluster",
        namespace="test-namespace",
        state_dir=state_dir,
        export_dir=export_dir,
        storage_backend="sbx-shared",
        sbx_shared_root=_REPO_ROOT / "tmp" / "test-shared",
        test_user="tester",
        test_uid=2000,
        test_gid=2000,
        verbose=False,
    )


def test_existing_state_requires_ownership_marker(tmp_path):
    """Bootstrap cannot adopt an arbitrary existing directory."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = _config(state_dir, tmp_path / "export")

    with pytest.raises(_DRIVER.ProvisionError, match="unowned setup state"):
        _validate_lifecycle_paths(config)


def test_existing_state_accepts_matching_ownership_marker(tmp_path):
    """A correctly marked state directory remains reusable."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    marker = state_dir / _DRIVER.STATE_MARKER
    marker.write_text(
        json.dumps({"schema": _DRIVER.STATE_SCHEMA, "cluster_name": "test-cluster"}),
        encoding="utf-8",
    )
    config = _config(state_dir, tmp_path / "export")

    _validate_lifecycle_paths(config)


def _write_backend_state(config, backend):
    """Write the retained backend identity used by repeated setup."""
    path = config.state_dir / "storage-backend.json"
    path.write_text(
        json.dumps(_storage_backend_document(config, backend)), encoding="utf-8"
    )


def _write_completed_state(config, backend):
    """Write the immutable subset of a successful setup summary."""
    state = {
        "schema": _DRIVER.STATE_SCHEMA,
        "cluster_name": config.cluster_name,
        "namespace": config.namespace,
        "export_dir": str(config.export_dir),
        "storage_backend": backend,
    }
    (config.state_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")


def test_retained_setup_rejects_changed_namespace(tmp_path):
    """Repeated setup cannot create a second namespace workload stack."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    original = replace(_config(state_dir, tmp_path / "export"), storage_backend="nfs")
    _write_completed_state(original, "nfs")
    _write_backend_state(original, "nfs")
    changed = replace(original, namespace="other-namespace")

    with pytest.raises(_DRIVER.ProvisionError, match="teardown first"):
        _select_storage_backend(changed)


def test_retained_nfs_setup_rejects_changed_export(tmp_path):
    """Repeated setup cannot mount retained NFS data at a second path."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    original = replace(_config(state_dir, tmp_path / "export"), storage_backend="nfs")
    _write_completed_state(original, "nfs")
    _write_backend_state(original, "nfs")
    changed = replace(original, export_dir=tmp_path / "other-export")

    with pytest.raises(_DRIVER.ProvisionError, match="teardown first"):
        _select_storage_backend(changed)


def test_system_directory_cannot_be_used_as_state(tmp_path):
    """An existing broad system directory cannot be marked during setup."""
    config = _config(Path("/usr"), tmp_path / "export")

    with pytest.raises(_DRIVER.ProvisionError, match="unowned setup state"):
        _validate_lifecycle_paths(config)


class _UnownedExportRunner:
    """Record commands while presenting an existing unmarked export."""

    def __init__(self):
        self.commands = []

    def run(self, arguments, **_kwargs):
        """Answer ownership probes without executing privileged commands."""
        command = [str(item) for item in arguments]
        self.commands.append(command)
        if "test" in command and "-e" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "cat" in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="missing")
        raise AssertionError(f"unexpected mutation command: {command}")


def test_existing_export_requires_marker_before_mutation(tmp_path):
    """An unmarked export is rejected before install or chown runs."""
    config = _config(tmp_path / "state", tmp_path / "export")
    runner = _UnownedExportRunner()

    with pytest.raises(_DRIVER.ProvisionError, match="unowned export directory"):
        _ensure_export_marker(runner, config)

    assert not any(
        "install" in command or "chown" in command for command in runner.commands
    )


class _PodRunner:
    """Return one terminating and one active ready login pod."""

    def run(self, _arguments, **_kwargs):
        """Return a fixed rollout-overlap pod list."""
        container = {"name": "login"}
        ready = {"phase": "Running", "containerStatuses": [{"ready": True}]}
        items = [
            {
                "metadata": {
                    "name": "old-login",
                    "deletionTimestamp": "2026-09-19T00:00:00Z",
                },
                "spec": {"containers": [container]},
                "status": ready,
            },
            {
                "metadata": {"name": "new-login"},
                "spec": {"containers": [container]},
                "status": ready,
            },
        ]
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"items": items}), stderr=""
        )


def test_login_selection_ignores_terminating_rollout_pod(tmp_path):
    """A terminating predecessor does not look like a second login node."""
    config = _config(tmp_path / "state", tmp_path / "export")

    assert _login_pod(_PodRunner(), config) == "new-login"


def test_fixture_discovery_ignores_terminating_ready_pod():
    """Test preflight observes only ready pods that are not terminating."""
    container = {"name": "login"}
    ready = {"phase": "Running", "containerStatuses": [{"ready": True}]}
    pods = [
        {
            "metadata": {"name": "old", "deletionTimestamp": "now"},
            "spec": {"containers": [container]},
            "status": ready,
        },
        {
            "metadata": {"name": "new"},
            "spec": {"containers": [container]},
            "status": ready,
        },
    ]

    selected = _pods_with_container(pods, "login")

    assert [pod["metadata"]["name"] for pod in selected] == ["new"]


def test_markerless_sbx_elbencho_bundle_is_rebuilt(tmp_path, monkeypatch):
    """A pre-recipe cached wrapper cannot survive a repository update."""
    cache = tmp_path / "test-cache"
    cache.mkdir()
    binary_name = "elbencho.aarch64"
    binary = cache / f"v3.1-11-{binary_name}"
    runtime = cache / f"v3.1-11-{binary_name}.runtime"
    binary.write_text("stale wrapper\n", encoding="utf-8")
    runtime.mkdir()
    calls = []

    def fake_extract(_runner, _cache, target_binary, target_runtime, architecture):
        calls.append(architecture)
        target_binary.write_text("current wrapper\n", encoding="utf-8")
        target_runtime.mkdir(exist_ok=True)

    monkeypatch.setattr(_FILESYSTEM, "_extract_container_elbencho", fake_extract)
    config = SimpleNamespace(state_dir=tmp_path)

    _ensure_elbencho(object(), config, "aarch64", "sbx-shared")
    _ensure_elbencho(object(), config, "aarch64", "sbx-shared")

    assert calls == ["aarch64"]
    marker = cache / f"v3.1-11-{binary_name}.bundle.json"
    document = json.loads(marker.read_text(encoding="utf-8"))
    assert document["container"] == _FILESYSTEM.ELBENCHO_CONTAINER
    assert document["recipe"] == _FILESYSTEM.SBX_ELBENCHO_BUNDLE_RECIPE
