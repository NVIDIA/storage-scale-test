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
from pathlib import Path, PurePosixPath
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
_bootstrap_state_dir = getattr(_DRIVER, "_bootstrap_state_dir")
_collect_diagnostics = getattr(_DRIVER, "_collect_diagnostics")
_configure_user_tool_path = getattr(_DRIVER, "_configure_user_tool_path")
_ensure_apt_packages = getattr(_DRIVER, "_ensure_apt_packages")
_ensure_export_marker = getattr(_DRIVER, "_ensure_export_marker")
_ensure_slurm_workload_account = getattr(_DRIVER, "_ensure_slurm_workload_account")
_export_paths = getattr(_DRIVER, "_export_paths")
_kind_clusters = getattr(_DRIVER, "_kind_clusters")
_login_pod = getattr(_DRIVER, "_login_pod")
_inspect_ssh_home_pool = getattr(_DRIVER, "_inspect_ssh_home_pool")
_prepare_host_dependencies = getattr(_DRIVER, "_prepare_host_dependencies")
_prepare_sbx_shared = getattr(_DRIVER, "_prepare_sbx_shared")
_driver_pods_with_container = getattr(_DRIVER, "_pods_with_container")
_remove_nfs_configuration = getattr(_DRIVER, "_remove_nfs_configuration")
_select_storage_backend = getattr(_DRIVER, "_select_storage_backend")
_storage_backend_document = getattr(_DRIVER, "_storage_backend_document")
_validate_teardown_ownership = getattr(_DRIVER, "_validate_teardown_ownership")
_validate_lifecycle_paths = getattr(_DRIVER, "_validate_lifecycle_paths")
_validate_ssh_storage = getattr(_DRIVER, "_validate_ssh_storage")
_wait_for_ssh = getattr(_DRIVER, "_wait_for_ssh")
_assert_execution_contract = getattr(_FILESYSTEM, "_assert_execution_contract")
_assert_ordered_workers = getattr(_FILESYSTEM, "_assert_ordered_workers")
_ensure_elbencho = getattr(_FILESYSTEM, "_ensure_elbencho")
_prepare_scenario_data = getattr(_FILESYSTEM, "_prepare_scenario_data")
_remote_staging_operations = getattr(_FILESYSTEM, "_remote_staging_operations")
_reset_result_base = getattr(_FILESYSTEM, "_reset_result_base")
_pods_with_container = getattr(_FILESYSTEM, "_pods_with_container")
_require_pods = getattr(_FILESYSTEM, "_require_pods")


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


def test_root_lifecycle_is_rejected_before_state_resolution(monkeypatch):
    """Every mutating or executing lifecycle action rejects root up front."""
    monkeypatch.setattr(sys, "argv", [str(_DRIVER_PATH), "setup"])
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        _DRIVER,
        "_config",
        lambda _arguments: pytest.fail("root lifecycle resolved setup state"),
    )

    assert _DRIVER.main() == 1


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
        test_uid=42424,
        test_gid=43434,
        verbose=False,
    )


def test_state_bootstrap_is_user_owned_and_idempotent(tmp_path, monkeypatch):
    """State starts under the caller and repeated bootstrap preserves it."""
    config = _config(tmp_path / "state", tmp_path / "export")
    monkeypatch.setenv("PATH", "/usr/bin")

    _bootstrap_state_dir(config)
    _configure_user_tool_path(config)
    first_marker = (config.state_dir / _DRIVER.STATE_MARKER).read_text(encoding="utf-8")
    _bootstrap_state_dir(config)

    assert config.state_dir.stat().st_uid == os.getuid()
    assert (config.state_dir.stat().st_mode & 0o777) == 0o750
    assert (config.state_dir / _DRIVER.STATE_MARKER).read_text(
        encoding="utf-8"
    ) == first_marker
    assert os.environ["PATH"].split(os.pathsep)[0] == str(config.state_dir / "bin")
    assert all(
        path in os.environ["PATH"].split(os.pathsep)
        for path in _DRIVER.SYSTEM_ADMIN_PATHS
    )
    assert os.environ["HELM_CACHE_HOME"] == str(
        config.state_dir / "tool-state" / "helm" / "cache"
    )


def test_standard_admin_path_resolves_nfs_tools(tmp_path, monkeypatch):
    """Ordinary-user setup finds tools installed outside its initial PATH."""
    admin_path = tmp_path / "usr-sbin"
    admin_path.mkdir()
    losetup = admin_path / "losetup"
    losetup.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    losetup.chmod(0o755)
    config = _config(tmp_path / "state", tmp_path / "export")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(_DRIVER, "SYSTEM_ADMIN_PATHS", (str(admin_path),))

    _configure_user_tool_path(config)

    assert _DRIVER.shutil.which("losetup") == str(losetup)


def test_default_state_bootstrap_creates_missing_tmp_parent(tmp_path, monkeypatch):
    """A clean checkout need not contain the ignored default tmp directory."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    state_dir = checkout / "tmp" / "integration-state"
    monkeypatch.setattr(_DRIVER, "DEFAULT_STATE_DIR", state_dir)
    config = _config(state_dir, tmp_path / "export")

    _validate_lifecycle_paths(config)
    _bootstrap_state_dir(config)

    assert state_dir.is_dir()
    assert (state_dir / _DRIVER.STATE_MARKER).is_file()


def test_custom_state_still_requires_an_existing_parent(tmp_path):
    """Parent creation is limited to the repository's known default path."""
    state_dir = tmp_path / "missing-parent" / "state"
    config = _config(state_dir, tmp_path / "export")

    with pytest.raises(_DRIVER.ProvisionError, match="leaf below an existing"):
        _validate_lifecycle_paths(config)


class _RecordingRunner:
    """Record commands and return an empty successful process."""

    def __init__(self):
        self.commands = []

    def run(self, arguments, **_kwargs):
        """Record one command without executing it."""
        command = [str(item) for item in arguments]
        self.commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_nfs_service_ownership_precedes_package_install(tmp_path, monkeypatch):
    """Package activation cannot obscure who started the NFS service."""
    events = []
    config = _config(tmp_path / "state", tmp_path / "export")
    runner = _RecordingRunner()
    monkeypatch.setattr(
        _DRIVER,
        "_record_nfs_service_state",
        lambda *_args: events.append("record"),
    )
    monkeypatch.setattr(
        _DRIVER,
        "_ensure_apt_packages",
        lambda *_args: events.append("packages"),
    )

    _prepare_host_dependencies(runner, config, "nfs")

    assert events == ["record", "packages"]


def test_missing_kind_is_an_empty_cluster_listing(monkeypatch):
    """Repeated teardown does not require a deleted private kind client."""

    class _UnexpectedRunner:
        def run(self, *_args, **_kwargs):
            pytest.fail("kind was invoked after its private client was removed")

    monkeypatch.setattr(_DRIVER.shutil, "which", lambda _command: None)

    assert _kind_clusters(_UnexpectedRunner()) == set()


def test_missing_exportfs_is_an_empty_export_listing(monkeypatch):
    """Cleanup after an interrupted package install needs no NFS client tool."""

    class _UnexpectedRunner:
        def run(self, *_args, **_kwargs):
            pytest.fail("exportfs was invoked when it is unavailable")

    monkeypatch.setattr(_DRIVER.shutil, "which", lambda _command: None)

    assert _export_paths(_UnexpectedRunner()) == []


def test_sbx_shared_directories_allow_replacement_pod_cleanup(tmp_path, monkeypatch):
    """SBX UID remapping cannot make prior pod files sticky and undeletable."""
    repository = tmp_path / "repository"
    (repository / "tmp").mkdir(parents=True)
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"),
        sbx_shared_root=repository / "tmp" / "shared",
    )

    class _SbxProbeRunner:
        def run(self, arguments, **_kwargs):
            token = str(arguments[-1])
            (config.sbx_shared_root / "engine-probe").write_text(
                token + "\n", encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_DRIVER, "_repository_root", lambda: repository)

    _prepare_sbx_shared(_SbxProbeRunner(), config)

    for name in ("storage-test", "ssh-home"):
        mode = (config.sbx_shared_root / name).stat().st_mode & 0o7777
        assert mode == _DRIVER.SBX_SHARED_DIRECTORY_MODE == 0o777


def test_sbx_diagnostics_never_invoke_privileged_nfs_tools(tmp_path, monkeypatch):
    """SBX failure reporting remains entirely within its unprivileged profile."""
    config = _config(tmp_path / "state", tmp_path / "export")
    runner = _RecordingRunner()
    monkeypatch.setattr(_DRIVER.shutil, "which", lambda _command: "/bin/tool")

    _collect_diagnostics(runner, config)

    rendered = "\n".join(" ".join(command) for command in runner.commands)
    assert "sudo" not in rendered
    assert "systemctl" not in rendered
    assert "exportfs" not in rendered


def test_sbx_missing_packages_fail_without_sudo():
    """The SBX profile reports host prerequisites instead of escalating."""
    runner = _RecordingRunner()

    with pytest.raises(_DRIVER.ProvisionError, match="never invokes sudo"):
        _ensure_apt_packages(runner, "sbx-shared")

    assert runner.commands
    assert all(command[0] == "dpkg-query" for command in runner.commands)


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

    with pytest.raises(_DRIVER.ProvisionError, match="not owned by the current user"):
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


def test_driver_worker_discovery_ignores_terminating_ready_pod():
    """Slinky worker rollout waits cannot count terminating pods as ready."""
    pods = [
        {
            "metadata": {"name": "old", "deletionTimestamp": "now"},
            "spec": {"containers": [{"name": "slurmd"}]},
        },
        {
            "metadata": {"name": "new"},
            "spec": {"containers": [{"name": "slurmd"}]},
        },
    ]

    selected = _driver_pods_with_container(pods, "slurmd")

    assert [pod["metadata"]["name"] for pod in selected] == ["new"]


def test_slurm_workload_account_reconciliation_is_idempotent(tmp_path, monkeypatch):
    """Repeated setup does not add an existing Slurm account or association."""

    class _AccountingRunner:
        def __init__(self):
            self.accounts = set()
            self.associations = set()
            self.users = {}
            self.mutations = []

        def run(self, command, **_kwargs):
            arguments = [str(item) for item in command]
            index = arguments.index("sacctmgr")
            operation = arguments[index + 1 :]
            if "show" in operation:
                entity = operation[operation.index("show") + 1]
                rows = {
                    "account": ((account,) for account in self.accounts),
                    "association": iter(self.associations),
                    "user": ((user, account) for user, account in self.users.items()),
                }[entity]
                output = "".join("|".join(row) + "|\n" for row in rows)
                return SimpleNamespace(stdout=output, returncode=0)
            self.mutations.append(operation)
            action = operation[1]
            if action == "add" and operation[2] == "account":
                self.accounts.add(operation[3])
            elif action == "add" and operation[2] == "user":
                user = operation[3]
                account = operation[4].removeprefix("Account=")
                self.associations.add((user, account))
                self.users[user] = operation[5].removeprefix("DefaultAccount=")
            elif action == "modify":
                user = operation[operation.index("where") + 1].removeprefix("Name=")
                self.users[user] = operation[-1].removeprefix("DefaultAccount=")
            return SimpleNamespace(stdout="", returncode=0)

    runner = _AccountingRunner()
    config = _config(tmp_path / "state", tmp_path / "export")
    monkeypatch.setattr(_DRIVER, "_login_pod", lambda *_args: "login")

    _ensure_slurm_workload_account(runner, config)
    first_mutations = list(runner.mutations)
    _ensure_slurm_workload_account(runner, config)

    assert len(first_mutations) == 2
    assert runner.mutations == first_mutations


def _ready_pod(name, container):
    """Return one minimal ready, nonterminating fixture pod."""
    return {
        "metadata": {"name": name},
        "spec": {"containers": [{"name": container}]},
        "status": {"phase": "Running", "containerStatuses": [{"ready": True}]},
    }


def test_ssh_rollout_waits_for_statefulset_revision(monkeypatch, tmp_path):
    """Pod readiness is checked only after Kubernetes finishes its rollout."""
    runner = _RecordingRunner()
    config = _config(tmp_path / "state", tmp_path / "export")
    pods = [_ready_pod("ssh-worker-0", "sshd"), _ready_pod("ssh-worker-1", "sshd")]
    monkeypatch.setattr(_DRIVER, "_ssh_pods", lambda *_args: pods)

    _wait_for_ssh(runner, config)

    assert runner.commands[0][-4:] == [
        "rollout",
        "status",
        "statefulset/ssh-worker",
        "--timeout=180s",
    ]


def test_ssh_pool_inspection_rejects_unobserved_revision(tmp_path):
    """Ready old pods cannot satisfy a newly annotated StatefulSet form."""
    config = _config(tmp_path / "state", tmp_path / "export")
    pods = [_ready_pod("ssh-worker-0", "sshd"), _ready_pod("ssh-worker-1", "sshd")]
    statefulset = {
        "metadata": {
            "generation": 2,
            "annotations": {
                _DRIVER.SSH_HOME_ANNOTATION: "separate",
                _DRIVER.SSH_CONFIG_ANNOTATION: "new-checksum",
            },
        },
        "status": {
            "observedGeneration": 1,
            "currentRevision": "old-revision",
            "updateRevision": "new-revision",
            "updatedReplicas": 0,
            "readyReplicas": 2,
        },
    }

    class _PoolRunner:
        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            document = (
                statefulset if "statefulset/ssh-worker" in command else {"items": pods}
            )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(document),
                stderr="",
            )

    observed = _inspect_ssh_home_pool(_PoolRunner(), config)

    assert not observed.matches("separate", "new-checksum")
    assert observed.ready_nonterminating_pods == 0
    assert "old-revision" in observed.diagnostics


def test_ssh_storage_visibility_retries_unique_probes(tmp_path, monkeypatch):
    """Transient NFS visibility cannot fail a healthy home-mode transition."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    pods = [_ready_pod("ssh-worker-0", "sshd"), _ready_pod("ssh-worker-1", "sshd")]
    commands = []
    storage_attempts = 0

    def pod_exec(_runner, _config, pod, script, *, check=True):
        nonlocal storage_attempts
        commands.append((pod, script, check))
        returncode = 0
        if script.startswith("test -e /home/tester/"):
            returncode = 1
        elif script.startswith('test "$(cat /mnt/storage-test/'):
            storage_attempts += 1
            returncode = 1 if storage_attempts == 1 else 0
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")

    monkeypatch.setattr(_DRIVER, "_pod_exec", pod_exec)
    monkeypatch.setattr(_DRIVER, "_select_storage_backend", lambda _config: "nfs")
    monkeypatch.setattr(_DRIVER.secrets, "token_hex", lambda _length: "unique-token")
    monkeypatch.setattr(_DRIVER.time, "sleep", lambda _seconds: None)

    _validate_ssh_storage(runner=None, config=config, pods=pods, home_mode="separate")

    assert storage_attempts == 2
    assert all("unique-token" in script for _pod, script, _check in commands)
    cleanup = [
        script for _pod, script, _check in commands if script.startswith("rm -f")
    ]
    assert len(cleanup) == 2


def test_slurm_only_fixture_does_not_require_ssh_workers():
    """Slurm diagnosis remains available while the SSH pool is unhealthy."""
    pods = [
        _ready_pod("login", "login"),
        _ready_pod("slurmd-1", "slurmd"),
        _ready_pod("slurmd-2", "slurmd"),
    ]

    login, ssh = _require_pods(pods, {"slurm"})

    assert login["metadata"]["name"] == "login"
    assert ssh == []
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match=r"required=\['ssh'\]"):
        _require_pods(pods, {"ssh"})


def test_pod_storage_commands_use_only_the_workload_identity(tmp_path):
    """Conspicuous host IDs never leak into pod-side storage operations."""

    class _CaptureRunner:
        def __init__(self):
            self.commands = []

        def run(self, command, **_kwargs):
            self.commands.append([str(item) for item in command])
            return SimpleNamespace(stdout="", returncode=0)

    runner = _CaptureRunner()
    config = _config(tmp_path / "state", tmp_path / "export")
    fixture = SimpleNamespace(login_pod="login", login_container="login")
    runtime = SimpleNamespace(selector="slurm")
    _prepare_scenario_data(
        runner,
        config,
        fixture,
        "/mnt/storage-test/integration-regression/test-data/sentinel",
    )
    _reset_result_base(
        runner,
        config,
        fixture,
        runtime,
        "/mnt/storage-test/integration-regression/results/sentinel",
    )
    operations = _remote_staging_operations(
        runner,
        config,
        fixture,
        "slurm",
        None,
        None,
    )
    operations.make_directory(
        "login",
        PurePosixPath("/mnt/storage-test/integration-regression/failure/sentinel"),
    )

    rendered = "\n".join(" ".join(command) for command in runner.commands)
    assert "42424" not in rendered
    assert "43434" not in rendered
    assert "chown" not in rendered
    assert "runuser -u tester --" in rendered
    assert "umask 0007" in rendered


def test_workload_totals_require_resume_metadata(tmp_path, monkeypatch):
    """Expected dataset totals cannot silently pass without workload metadata."""
    execution_root = tmp_path / "result" / "executions"
    execution_root.mkdir(parents=True)
    (execution_root / "0001.sh").write_text("coordinates\n", encoding="utf-8")
    (execution_root / "0001.status").write_text("SUCCESS\n", encoding="utf-8")
    (execution_root / "0001.exitcode").write_text("0\n", encoding="utf-8")
    coordinate = SimpleNamespace(nodes=1, io_size="4K", threads=1, io_depth=1)
    expected = SimpleNamespace(
        coordinate=coordinate,
        status=_FILESYSTEM.ExecutionStatus.SUCCESS,
    )
    step = SimpleNamespace(name="bounded", executions=(expected,), required_phases=())
    scenario = SimpleNamespace(name="baseline")
    monkeypatch.setattr(
        _FILESYSTEM,
        "_coordinate_from_execution",
        lambda _path: (1, "4K", 1, 1),
    )

    with pytest.raises(
        _FILESYSTEM.IntegrationTestError, match="missing required workload metadata"
    ):
        _assert_execution_contract(scenario, step, tmp_path / "result")


def test_slurm_ordering_uses_copied_execution_logs(tmp_path):
    """Asynchronous Slurm ordering comes from durable result evidence."""
    result = tmp_path / "result"
    executions = result / "executions"
    executions.mkdir(parents=True)
    (executions / "0001.log").write_text(
        "[coordinator] starting execution 1: nodes=1 hosts=10.0.0.1 io_size=4K\n",
        encoding="utf-8",
    )
    (executions / "0002.log").write_text(
        "[coordinator] starting execution 2: "
        "nodes=2 hosts=10.0.0.1,10.0.0.2 io_size=4K\n",
        encoding="utf-8",
    )
    fixture = SimpleNamespace(slurm_addresses=("10.0.0.1", "10.0.0.2"))

    _assert_ordered_workers(
        fixture,
        "slurm",
        "submission output contains no coordinator execution lines",
        result,
    )

    for path in executions.iterdir():
        path.unlink()
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match=r"was \{\}"):
        _assert_ordered_workers(fixture, "slurm", "", result)


def test_teardown_validation_allows_unrelated_nfs_exports(tmp_path, monkeypatch):
    """Owned fixture cleanup is valid while an unrelated export remains active."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    config.state_dir.mkdir()
    config.manifests_dir.mkdir()
    owner = json.dumps(
        {"schema": _DRIVER.STATE_SCHEMA, "cluster_name": config.cluster_name}
    )
    (config.state_dir / _DRIVER.STATE_MARKER).write_text(owner, encoding="utf-8")
    (config.state_dir / "cluster-owner.json").write_text(owner, encoding="utf-8")
    (config.state_dir / "nfs-service.json").write_text(
        json.dumps({"started_by_harness": False}), encoding="utf-8"
    )
    export_config = "owned export\n"
    daemon_config = "owned daemon config\n"
    (config.manifests_dir / "storage-scale-test.exports").write_text(
        export_config, encoding="utf-8"
    )
    (config.manifests_dir / "storage-scale-test-nfs.conf").write_text(
        daemon_config, encoding="utf-8"
    )
    installed = {
        config.export_dir / _DRIVER.EXPORT_MARKER: owner,
        _DRIVER.NFS_EXPORT_CONFIG: export_config,
        _DRIVER.NFS_DAEMON_CONFIG: daemon_config,
    }
    monkeypatch.setattr(
        _DRIVER, "_read_system_file", lambda _runner, path: installed.get(path)
    )
    monkeypatch.setattr(_DRIVER, "_export_mount_type", lambda *_args: "")
    monkeypatch.setattr(_DRIVER, "_validate_loop_associations", lambda *_args: None)
    monkeypatch.setattr(_DRIVER, "_validate_image_ownership", lambda *_args: None)
    monkeypatch.setattr(
        _DRIVER, "_export_paths", lambda _runner: ["/srv/unrelated-export"]
    )

    ownership = _validate_teardown_ownership(object(), config)

    assert ownership == (True, True, True, True)


class _NfsRemovalRunner:
    """Record NFS cleanup while reporting one unrelated active export."""

    def __init__(self):
        self.commands = []

    def run(self, arguments, **_kwargs):
        """Record one cleanup command and return stable export state."""
        command = [str(item) for item in arguments]
        self.commands.append(command)
        stdout = (
            "/srv/unrelated-export 10.0.0.0/24(options)\n" if "-v" in command else ""
        )
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def test_nfs_cleanup_preserves_preexisting_service(tmp_path, monkeypatch):
    """Removing owned NFS configuration never disables a pre-existing service."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    config.manifests_dir.mkdir(parents=True)
    (config.manifests_dir / "storage-scale-test.exports").write_text(
        f"{config.export_dir} 10.0.0.0/24(options)\n", encoding="utf-8"
    )
    (config.state_dir / "nfs-service.json").write_text(
        json.dumps({"started_by_harness": False}), encoding="utf-8"
    )
    runner = _NfsRemovalRunner()
    monkeypatch.setattr(
        _DRIVER.shutil,
        "which",
        lambda command: f"/usr/sbin/{command}",
    )

    _remove_nfs_configuration(runner, config)

    assert not any("systemctl" in command for command in runner.commands)
    assert any(
        any(Path(item).name == "exportfs" for item in command) and "-u" in command
        for command in runner.commands
    )


def test_nfs_cleanup_survives_missing_exportfs(tmp_path, monkeypatch):
    """Interrupted package bootstrap leaves teardown able to remove state."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    config.manifests_dir.mkdir(parents=True)
    (config.state_dir / "nfs-service.json").write_text(
        json.dumps({"started_by_harness": True}), encoding="utf-8"
    )
    runner = _NfsRemovalRunner()
    monkeypatch.setattr(_DRIVER.shutil, "which", lambda _command: None)

    _remove_nfs_configuration(runner, config)

    rendered = [" ".join(command) for command in runner.commands]
    assert not any("exportfs" in command for command in rendered)
    assert sum("rm --force" in command for command in rendered) == 2


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
