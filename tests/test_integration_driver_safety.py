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

# This module intentionally tests private integration-driver boundaries.
# pylint: disable=protected-access

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
import yaml

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
_acquire_lock = getattr(_DRIVER, "_acquire_lock")
_check_docker_capacity = getattr(_DRIVER, "_check_docker_capacity")
_check_host_capacity = getattr(_DRIVER, "_check_host_capacity")
_collect_diagnostics = getattr(_DRIVER, "_collect_diagnostics")
_configure_nfs = getattr(_DRIVER, "_configure_nfs")
_create_cluster = getattr(_DRIVER, "_create_cluster")
_claim_nfs_host_owner = getattr(_DRIVER, "_claim_nfs_host_owner")
_configure_user_tool_path = getattr(_DRIVER, "_configure_user_tool_path")
_ensure_apt_packages = getattr(_DRIVER, "_ensure_apt_packages")
_ensure_export_marker = getattr(_DRIVER, "_ensure_export_marker")
_ensure_nfs_image_capacity = getattr(_DRIVER, "_ensure_nfs_image_capacity")
_observed_kubernetes_version = getattr(_DRIVER, "_observed_kubernetes_version")
_ensure_slurm_workload_account = getattr(_DRIVER, "_ensure_slurm_workload_account")
_export_paths = getattr(_DRIVER, "_export_paths")
_kind_clusters = getattr(_DRIVER, "_kind_clusters")
_login_pod = getattr(_DRIVER, "_login_pod")
_load_state = getattr(_FILESYSTEM, "_load_state")
_publish_temporary_file = getattr(_FILESYSTEM, "_publish_temporary_file")
_inspect_ssh_home_pool = getattr(_DRIVER, "_inspect_ssh_home_pool")
_migrate_export_data_to_image = getattr(_DRIVER, "_migrate_export_data_to_image")
_prepare_host_dependencies = getattr(_DRIVER, "_prepare_host_dependencies")
_prepare_sbx_shared = getattr(_DRIVER, "_prepare_sbx_shared")
_record_nfs_service_state = getattr(_DRIVER, "_record_nfs_service_state")
_nfs_host_lock = getattr(_DRIVER, "_nfs_host_lock")
_delete_nfs_firewall_rule = getattr(_DRIVER, "_delete_nfs_firewall_rule")
_write_bytes = getattr(_DRIVER, "_write_bytes")
_write_text = getattr(_DRIVER, "_write_text")
_nfs_host_owner_document = getattr(_DRIVER, "_nfs_host_owner_document")
_retained_cluster_matches_profile = getattr(
    _DRIVER, "_retained_cluster_matches_profile"
)
_fixture_profile = getattr(_DRIVER, "_fixture_profile")
_prepare_kubectl_prerequisite_image = getattr(
    _DRIVER, "_prepare_kubectl_prerequisite_image"
)
_validate_kindnet_profile = getattr(_DRIVER, "_validate_kindnet_profile")
_render_calico_manifest = getattr(_DRIVER, "_render_calico_manifest")
_calico_image = getattr(_DRIVER, "_calico_image")
_stop_owned_nfs = getattr(_DRIVER, "_stop_owned_nfs")
_driver_pods_with_container = getattr(_DRIVER, "_pods_with_container")
_remove_nfs_configuration = getattr(_DRIVER, "_remove_nfs_configuration")
_remove_sbx_shared = getattr(_DRIVER, "_remove_sbx_shared")
_restore_nfs_service_state = getattr(_DRIVER, "_restore_nfs_service_state")
_restore_preexisting_nfs_threads = getattr(_DRIVER, "_restore_preexisting_nfs_threads")
_select_storage_backend = getattr(_DRIVER, "_select_storage_backend")
_set_live_nfs_threads = getattr(_DRIVER, "_set_live_nfs_threads")
_storage_backend_document = getattr(_DRIVER, "_storage_backend_document")
_unmount_temporary_filesystem = getattr(_DRIVER, "_unmount_temporary_filesystem")
_validate_nfs_filesystem_capacity = getattr(
    _DRIVER, "_validate_nfs_filesystem_capacity"
)
_validate_teardown_ownership = getattr(_DRIVER, "_validate_teardown_ownership")
_validate_sbx_teardown_ownership = getattr(_DRIVER, "_validate_sbx_teardown_ownership")
_validate_lifecycle_paths = getattr(_DRIVER, "_validate_lifecycle_paths")
_invalidate_setup_summary = getattr(_DRIVER, "_invalidate_setup_summary")
_validate_ssh_storage = getattr(_DRIVER, "_validate_ssh_storage")
_teardown_environment_locked = getattr(_DRIVER, "_teardown_environment_locked")
_begin_image_build = getattr(_DRIVER, "_begin_image_build")
_build_owned_image = getattr(_DRIVER, "_build_owned_image")
_acquire_pinned_image = getattr(_DRIVER, "_acquire_pinned_image")
_ensure_pinned_image = getattr(_DRIVER, "_ensure_pinned_image")
_install_slurm = getattr(_DRIVER, "_install_slurm")
_render_resource_text = getattr(_DRIVER, "_render_resource_text")
_transient_image_pull_failure = getattr(_DRIVER, "_transient_image_pull_failure")
_expected_kubernetes_version = getattr(_DRIVER, "_expected_kubernetes_version")
_image_ownership_state = getattr(_DRIVER, "_image_ownership_state")
_image_id = getattr(_DRIVER, "_image_id")
_inspect_pinned_image = getattr(_DRIVER, "_inspect_pinned_image")
_prepare_csi_images = getattr(_DRIVER, "_prepare_csi_images")
_load_image_into_nodes = getattr(_DRIVER, "_load_image_into_nodes")
_remove_harness_images = getattr(_DRIVER, "_remove_harness_images")
_write_image_ownership_state = getattr(_DRIVER, "_write_image_ownership_state")
_wait_for_kube_api = getattr(_DRIVER, "_wait_for_kube_api")
_wait_for_no_ssh_pods = getattr(_DRIVER, "_wait_for_no_ssh_pods")
_wait_for_slurm = getattr(_DRIVER, "_wait_for_slurm")
_wait_for_ssh = getattr(_DRIVER, "_wait_for_ssh")
_assert_execution_contract = getattr(_FILESYSTEM, "_assert_execution_contract")
_assert_ordered_workers = getattr(_FILESYSTEM, "_assert_ordered_workers")
_ensure_elbencho = getattr(_FILESYSTEM, "_ensure_elbencho")
_prepare_scenario_data = getattr(_FILESYSTEM, "_prepare_scenario_data")
_cleanup_scenario_storage = getattr(_FILESYSTEM, "_cleanup_scenario_storage")
_cleanup_ssh_remote_results = getattr(_FILESYSTEM, "_cleanup_ssh_remote_results")
_run_step = getattr(_FILESYSTEM, "_run_step")
_pod_command = getattr(_FILESYSTEM, "_pod_command")
_test_command = getattr(_FILESYSTEM, "_test_command")
_record_secondary_cleanup_failure = getattr(
    _FILESYSTEM, "_record_secondary_cleanup_failure"
)
_slurm_run_time = getattr(_FILESYSTEM, "_slurm_run_time")
_preserve_scenario_failure_diagnostics = getattr(
    _FILESYSTEM, "_preserve_scenario_failure_diagnostics"
)
_remote_staging_operations = getattr(_FILESYSTEM, "_remote_staging_operations")
_reset_result_base = getattr(_FILESYSTEM, "_reset_result_base")
_pods_with_container = getattr(_FILESYSTEM, "_pods_with_container")
_require_pods = getattr(_FILESYSTEM, "_require_pods")
_kubectl_output_value = getattr(_FILESYSTEM, "_kubectl_output_value")
_kubectl_submission = getattr(_FILESYSTEM, "_kubectl_submission")
_kubectl_lifecycle_state = getattr(_FILESYSTEM, "_kubectl_lifecycle_state")
_create_generated_inputs = getattr(_FILESYSTEM, "_create_generated_inputs")
_kubectl_utility_manifest = getattr(_FILESYSTEM, "_kubectl_utility_manifest")
_kubectl_execution_node = getattr(_FILESYSTEM, "_kubectl_execution_node")
_override_block = getattr(_FILESYSTEM, "_override_block")
_require_kubectl_nodes = getattr(_FILESYSTEM, "_require_kubectl_nodes")
_cleanup_kubectl_attempt = getattr(_FILESYSTEM, "_cleanup_kubectl_attempt")
_cleanup_scenario_storage = getattr(_FILESYSTEM, "_cleanup_scenario_storage")
_make_scenario_runtime = getattr(_FILESYSTEM, "_make_scenario_runtime")
_run_kubectl_command = getattr(_FILESYSTEM, "_run_kubectl_command")
_wait_for_kubectl_terminal_state = getattr(
    _FILESYSTEM, "_wait_for_kubectl_terminal_state"
)
IntegrationTestError = getattr(_FILESYSTEM, "IntegrationTestError")


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


def test_integration_timeouts_control_command_process_groups(tmp_path):
    """Remote and local bounds do not leave descendants outside timeout."""
    config = _config(tmp_path / "state", tmp_path / "export")
    fixture = SimpleNamespace(login_pod="login", login_container="login")

    pod = _pod_command(config, "login", "login", "true")
    ssh = _test_command(config, fixture, "ssh", "true", 30)

    assert "--foreground" not in pod
    assert "--foreground" not in ssh


def _kubectl_fixture() -> object:
    """Return only the fixture fields used by Kubernetes-only helpers."""
    return SimpleNamespace(
        architecture="x86_64",
        kubectl_nodes=("worker-a", "worker-b"),
        kubectl_namespace="test-namespace",
        kubectl_pv="storage-pv",
        kubectl_pvc="storage-test-rwx",
        kubectl_image="docker.io/example/elbencho@sha256:" + "a" * 64,
    )


def test_kubectl_renderer_uses_logical_roots_and_private_fixture_values():
    """Kubernetes rendering cannot inherit SSH or Slurm configuration by fallthrough."""
    overrides, support = _override_block(
        "kubectl", "/tmp/workspace", _kubectl_fixture(), timeout_seconds=120
    )

    assert "declare -A TEST_DIRS=([primary]=1)" in overrides
    assert "KUBECTL_NAMESPACE=test-namespace" in overrides
    assert "KUBECTL_PV=storage-pv" in overrides
    assert "KUBECTL_PVC=storage-test-rwx" in overrides
    assert "KUBECTL_NODE_SELECTOR=storage-scale-test/target=true" in overrides
    assert "KUBECTL_IMAGE_PULL_POLICY=Never" in overrides
    assert "unset SSH_HOST_LIST SSH_USER SSH_HOMEDIR_SHARED" in overrides
    assert "unset SLURM_NODE_INCLUDES SLURM_NODE_IGNORES" in overrides
    assert support == {}

    overrides, _ = _override_block(
        "kubectl",
        "/tmp/workspace",
        _kubectl_fixture(),
        logical_test_root="integration-regression/run-1-baseline-kubectl/primary",
    )
    assert (
        "declare -A TEST_DIRS=([integration-regression/run-1-baseline-kubectl/primary]=1)"
        in overrides
    )


def test_kubectl_lifecycle_output_is_strict_and_machine_readable():
    """The adapter accepts one safe stable value for every product handoff."""
    output = "\n".join(
        (
            "STORAGE_SCALE_TEST_RESULTS_DIR=/tmp/results/elbencho-1",
            "STORAGE_SCALE_TEST_ATTEMPT_ID=1234abcd",
            "STORAGE_SCALE_TEST_KUBECTL_STATE=RUNNING",
        )
    )

    root, attempt = _kubectl_submission(output)
    assert root == Path("/tmp/results/elbencho-1")
    assert attempt == "1234abcd"
    assert _kubectl_lifecycle_state(output) == "RUNNING"
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="one safe"):
        _kubectl_output_value(
            output + "\nSTORAGE_SCALE_TEST_ATTEMPT_ID=deadbeef",
            "STORAGE_SCALE_TEST_ATTEMPT_ID",
        )
    assert (
        _kubectl_output_value(
            "STORAGE_SCALE_TEST_RESULTS_DIR=/tmp/result with spaces",
            "STORAGE_SCALE_TEST_RESULTS_DIR",
        )
        == "/tmp/result with spaces"
    )


def test_kubectl_utility_manifest_is_nonroot_tokenless_and_uses_private_image():
    """PVC utility Pods do not borrow LoginSet identity or registry pulls."""
    document = json.loads(
        _kubectl_utility_manifest("utility-1234abcd", _kubectl_fixture())
    )
    spec = document["spec"]
    container = spec["containers"][0]

    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsUser"] == 2000
    assert container["imagePullPolicy"] == "Never"
    assert container["image"] == _kubectl_fixture().kubectl_image
    assert container["volumeMounts"][0]["mountPath"] == "/mnt/storage-scale-test"
    assert spec["nodeSelector"] == {"storage-scale-test/login": "true"}

    pinned = json.loads(
        _kubectl_utility_manifest("utility-1234abcd", _kubectl_fixture(), "worker-a")
    )["spec"]
    assert pinned["nodeName"] == "worker-a"
    assert "nodeSelector" not in pinned


def test_kubectl_retained_probe_requires_owned_worker_evidence(tmp_path):
    """A retained-data probe cannot schedule from untrusted result metadata."""
    result = tmp_path / "result"
    (result / "executions").mkdir(parents=True)
    workers = result / "executions" / "0001.workers.tsv"
    workers.write_text(
        "worker-a\tworker-pod\t01234567-89ab-cdef-0123-456789abcdef\t10.0.0.2\n",
        encoding="utf-8",
    )
    fixture = _kubectl_fixture()
    fixture.kubectl_nodes = ("worker-a", "worker-b")
    assert _kubectl_execution_node(fixture, result) == "worker-a"

    workers.write_text(
        "control-plane\tworker-pod\t01234567-89ab-cdef-0123-456789abcdef\t10.0.0.2\n",
        encoding="utf-8",
    )
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="malformed"):
        _kubectl_execution_node(fixture, result)


def test_kubectl_target_discovery_rejects_platform_drift():
    """A private pinned image is never sent to targets of another architecture."""
    nodes = [
        {
            "metadata": {"name": name, "labels": {"storage-scale-test/target": "true"}},
            "status": {"nodeInfo": {"architecture": "amd64"}},
        }
        for name in ("worker-a", "worker-b")
    ]

    assert _require_kubectl_nodes(nodes, "x86_64") == ("worker-a", "worker-b")
    nodes[1]["status"]["nodeInfo"]["architecture"] = "arm64"
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="architecture mismatch"):
        _require_kubectl_nodes(nodes, "x86_64")


def test_kubectl_fixture_preflight_needs_no_login_or_ssh_pods():
    """Kubernetes-only diagnosis remains possible during SSH/Slurm outages."""
    login, ssh = _require_pods([], {"kubectl"})

    assert login is None
    assert ssh == []


def test_kubectl_cleanup_uses_idempotent_product_lifecycle_operations(tmp_path):
    """Interrupted work is stopped through product cancel/collect, not raw deletes."""
    config = _config(tmp_path / "state", tmp_path / "export")
    runtime = SimpleNamespace(
        workspace=str(tmp_path / "workspace"),
        values={"kubectl_result_root": "/tmp/results with spaces"},
    )
    commands = []

    class _Runner:
        def run(self, arguments, **_kwargs):
            commands.append([str(item) for item in arguments])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    _cleanup_kubectl_attempt(_Runner(), config, runtime)

    assert [command[-2:] for command in commands] == [
        ["--cancel", "/tmp/results with spaces"],
        ["--collect", "/tmp/results with spaces"],
    ]
    assert all(f"KUBECONFIG={config.kubeconfig}" in command for command in commands)


def test_kubectl_adapter_drives_submit_status_and_collect_handoffs(tmp_path):
    """The adapter uses the public submit, status, and collect operations."""
    config = _config(tmp_path / "state", tmp_path / "export")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    runtime = SimpleNamespace(workspace=str(workspace))
    calls = []

    class _Runner:
        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            calls.append(command)
            if "--status" in command:
                output = "STORAGE_SCALE_TEST_KUBECTL_STATE=SUCCESS\n"
            elif "--collect" in command:
                output = "STORAGE_SCALE_TEST_KUBECTL_STATE=SUCCESS\n"
            else:
                output = "\n".join(
                    (
                        "STORAGE_SCALE_TEST_RESULTS_DIR=/tmp/results/elbencho-1",
                        "STORAGE_SCALE_TEST_ATTEMPT_ID=1234abcd",
                    )
                )
            return SimpleNamespace(returncode=0, stdout=output, stderr="")

    runner = _Runner()
    submit = _run_kubectl_command(
        runner, config, runtime, "submit", ("--nodes", "1,2"), log_dir, 30
    )
    result_root, attempt = _kubectl_submission(submit)
    assert result_root == Path("/tmp/results/elbencho-1")
    assert attempt == "1234abcd"
    state = _wait_for_kubectl_terminal_state(
        runner, config, runtime, result_root, log_dir, 30
    )
    assert state == "SUCCESS"
    collected = _run_kubectl_command(
        runner,
        config,
        runtime,
        "collect",
        ("--collect", str(result_root)),
        log_dir,
        30,
    )
    assert _kubectl_lifecycle_state(collected) == "SUCCESS"
    assert any("--status" in command for command in calls)
    assert any("--collect" in command for command in calls)


def test_kubectl_status_poll_retries_transient_command_failure(tmp_path, monkeypatch):
    """One failed API observation does not fail an otherwise healthy attempt."""
    config = _config(tmp_path / "state", tmp_path / "export")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    runtime = SimpleNamespace(workspace=str(workspace))
    calls = 0

    class _Runner:
        def run(self, _arguments, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="timeout")
            return SimpleNamespace(
                returncode=0,
                stdout="STORAGE_SCALE_TEST_KUBECTL_STATE=SUCCESS\n",
                stderr="",
            )

    monkeypatch.setattr(_FILESYSTEM.time, "sleep", lambda _seconds: None)
    state = _wait_for_kubectl_terminal_state(
        _Runner(), config, runtime, Path("/tmp/results/attempt"), log_dir, 30
    )
    assert state == "SUCCESS"
    assert calls == 2


def test_kubectl_command_accepts_only_declared_terminal_failure(tmp_path):
    """Failed collect is accepted only with its machine-readable state."""
    config = _config(tmp_path / "state", tmp_path / "export")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    runtime = SimpleNamespace(workspace=str(workspace))

    class _Runner:
        def run(self, _arguments, **_kwargs):
            return SimpleNamespace(
                returncode=1,
                stdout="STORAGE_SCALE_TEST_KUBECTL_STATE=FAILED\n",
                stderr="",
            )

    output = _run_kubectl_command(
        _Runner(),
        config,
        runtime,
        "collect",
        ("--collect", "/tmp/results"),
        logs,
        30,
        accepted_states=frozenset({"FAILED"}),
    )
    assert _kubectl_lifecycle_state(output) == "FAILED"


def test_kubectl_command_rejects_zero_exit_for_declared_failure(tmp_path):
    """A failed durable ledger must still propagate a failing CLI status."""
    config = _config(tmp_path / "state", tmp_path / "export")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    runtime = SimpleNamespace(workspace=str(workspace))

    class _Runner:
        def run(self, _arguments, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout="STORAGE_SCALE_TEST_KUBECTL_STATE=FAILED\n",
                stderr="",
            )

    with pytest.raises(IntegrationTestError, match="must return nonzero"):
        _run_kubectl_command(
            _Runner(),
            config,
            runtime,
            "collect",
            ("--collect", "/tmp/results"),
            logs,
            30,
            accepted_states=frozenset({"FAILED"}),
        )


def test_failed_kubectl_submit_arms_exact_lifecycle_cleanup(tmp_path):
    """A retained PREPARED submit is discoverable before workspace cleanup."""
    config = _config(tmp_path / "state", tmp_path / "export")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()
    result_base = workspace / "results" / "baseline"
    attempt_root = result_base / "elbencho-test"
    (attempt_root / "kubernetes").mkdir(parents=True)
    (attempt_root / "kubernetes/current-attempt").write_text(
        "1234abcd\n", encoding="utf-8"
    )
    runtime = SimpleNamespace(
        workspace=str(workspace),
        values={"kubectl_result_base": str(result_base)},
    )

    class _Runner:
        def run(self, _arguments, **_kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="submit failed")

    with pytest.raises(IntegrationTestError, match="failed with exit code"):
        _run_kubectl_command(
            _Runner(), config, runtime, "baseline", ("--nodes", "1"), logs, 30
        )
    assert runtime.values["kubectl_result_root"] == str(attempt_root)


def test_kubectl_generated_inputs_are_created_under_the_storage_mount(monkeypatch):
    """Generated input paths use logical roots only after PVC mapping."""
    commands = []
    monkeypatch.setattr(
        _FILESYSTEM,
        "_storage_utility_shell",
        lambda _runner, _config, _fixture, command, **_kwargs: commands.append(command),
    )
    generated = SimpleNamespace(relative_path="staged/input", size_bytes=4096)
    step = SimpleNamespace(generated_inputs=(generated,))
    runtime = SimpleNamespace(
        selector="kubectl", values={"test_root": "integration-regression/run/primary"}
    )
    fixture = SimpleNamespace(login_pod=None, login_container=None)
    _create_generated_inputs(object(), object(), fixture, runtime, step)
    assert "/mnt/storage-scale-test/integration-regression/run/primary" in commands[0]


def test_kubectl_cleanup_reports_lifecycle_failure(tmp_path):
    """An incomplete attempt cannot silently pass when cleanup fails."""
    config = _config(tmp_path / "state", tmp_path / "export")
    runtime = SimpleNamespace(
        workspace=str(tmp_path / "workspace"),
        values={"kubectl_result_root": "/tmp/results/elbencho-1"},
    )
    commands = []

    class _Runner:
        def run(self, arguments, **_kwargs):
            commands.append([str(item) for item in arguments])
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="cleanup --cancel"):
        _cleanup_kubectl_attempt(_Runner(), config, runtime)
    assert len(commands) == 1


def test_kubectl_cleanup_skips_already_collected_attempt(tmp_path):
    """Successful collection makes later scenario cleanup a no-op."""
    config = _config(tmp_path / "state", tmp_path / "export")
    runtime = SimpleNamespace(
        workspace=str(tmp_path / "workspace"),
        values={
            "kubectl_result_root": "/tmp/results/elbencho-1",
            "kubectl_collected": "1",
        },
    )

    class _Runner:
        def run(self, *_args, **_kwargs):
            pytest.fail("collected Kubernetes attempt was operated on again")

    _cleanup_kubectl_attempt(_Runner(), config, runtime)


def test_kubectl_scenario_runtime_owns_a_unique_logical_storage_root(tmp_path):
    """Kubernetes scenarios do not share the benchmark root between runs."""
    fixture = SimpleNamespace(slurm_nodes=())
    scenario = _FILESYSTEM.SCENARIO_SPECS_BY_NAME["baseline"]
    runtime = _make_scenario_runtime(
        fixture,
        scenario,
        "kubectl",
        tmp_path / "build",
        tmp_path / "artifacts",
        "run-1",
    )

    assert runtime.data_root.startswith("integration-regression/")
    assert runtime.data_root != "primary"
    assert runtime.values["test_root"] == f"{runtime.data_root}/primary"


def test_kubectl_storage_cleanup_targets_only_owned_root(tmp_path, monkeypatch):
    """Kubernetes cleanup removes only the scenario's mapped PVC subtree."""
    config = _config(tmp_path / "state", tmp_path / "export")
    runtime = SimpleNamespace(
        selector="kubectl",
        scenario=SimpleNamespace(name="baseline"),
        data_root="integration-regression/run-1-baseline-kubectl",
    )
    fixture = _kubectl_fixture()
    fixture.login_pod = "login"
    fixture.login_container = "login"
    commands = []
    monkeypatch.setattr(
        _FILESYSTEM,
        "_storage_utility_shell",
        lambda _runner, _config, _fixture, command, **_kwargs: commands.append(command),
    )

    _cleanup_scenario_storage(object(), config, fixture, runtime)
    assert any(
        "/mnt/storage-scale-test/integration-regression/run-1-baseline-kubectl"
        in command
        for command in commands
    )

    runtime.data_root = "primary"
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="unexpected"):
        _cleanup_scenario_storage(object(), config, fixture, runtime)


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


def test_setup_invalidates_prior_success_marker_before_retry(tmp_path):
    """A failed retry cannot leave an old state.json authorizing tests."""
    config = _config(tmp_path / "state", tmp_path / "export")
    config.state_dir.mkdir()
    summary = config.state_dir / "state.json"
    summary.write_text('{"storage_backend":"sbx-shared"}\n', encoding="utf-8")

    _invalidate_setup_summary(config)

    assert not summary.exists()


def test_lifecycle_lock_survives_state_tree_removal(tmp_path):
    """Teardown cannot replace the inode locked by its active invocation."""
    config = _config(tmp_path / "state", tmp_path / "export")
    config.state_dir.mkdir()
    first = _acquire_lock(config)
    try:
        shutil.rmtree(config.state_dir)
        config.state_dir.mkdir()
        with pytest.raises(_DRIVER.ProvisionError, match="another integration"):
            second = _acquire_lock(config)
            second.close()
    finally:
        first.close()


def test_nfs_global_lock_rejects_unowned_directory_without_mutating_it():
    """A pre-created host-global lock path is never chmodded or adopted."""

    class _UnsafeLockRunner:
        def __init__(self):
            self.commands = []

        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            self.commands.append(command)
            if "stat" in command:
                return SimpleNamespace(
                    returncode=0, stdout="1001:1001:755\n", stderr=""
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = _UnsafeLockRunner()
    with pytest.raises(_DRIVER.ProvisionError, match="unsafe NFS lock directory"):
        with _nfs_host_lock(runner):
            pytest.fail("unsafe lock directory was accepted")

    assert not any(
        "chmod" in command or "touch" in command for command in runner.commands
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


def test_capacity_check_uses_fixture_paths_nearest_existing_parent(
    tmp_path, monkeypatch
):
    """A configurable state path is checked on its actual backing filesystem."""
    existing = tmp_path / "existing"
    existing.mkdir()
    requested = existing / "not-yet-created" / "state"
    observed = []
    monkeypatch.setattr(_DRIVER.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(
        _DRIVER,
        "_meminfo",
        lambda: {"MemTotal": 16 * _DRIVER.GIB, "MemAvailable": 12 * _DRIVER.GIB},
    )
    monkeypatch.setattr(
        _DRIVER.shutil,
        "disk_usage",
        lambda path: observed.append(Path(path))
        or SimpleNamespace(free=30 * _DRIVER.GIB),
    )

    _check_host_capacity(requested)

    assert observed == [existing]


def test_docker_capacity_checks_visible_data_root(tmp_path, monkeypatch):
    """Docker storage is checked separately when its data root is host-visible."""
    observed = []
    runner = SimpleNamespace(
        run=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=f"{tmp_path}\n", stderr=""
        )
    )
    monkeypatch.setattr(
        _DRIVER.shutil,
        "disk_usage",
        lambda path: observed.append(Path(path))
        or SimpleNamespace(free=30 * _DRIVER.GIB),
    )

    _check_docker_capacity(runner)

    assert observed == [tmp_path]


class _TemporaryUnmountRunner:
    """Model a temporary mount that disappears after a chosen attempt."""

    def __init__(self, unmounted_after):
        self.unmounted_after = unmounted_after
        self.unmount_attempts = 0

    def run(self, arguments, **_kwargs):
        """Return deterministic umount and findmnt outcomes."""
        command = [str(item) for item in arguments]
        if "umount" in command:
            self.unmount_attempts += 1
            return SimpleNamespace(
                returncode=0 if self.unmount_attempts >= self.unmounted_after else 1,
                stdout="",
                stderr=(
                    ""
                    if self.unmount_attempts >= self.unmounted_after
                    else "target is busy"
                ),
            )
        assert command[0] == "findmnt"
        is_mounted = self.unmount_attempts < self.unmounted_after
        return SimpleNamespace(
            returncode=0 if is_mounted else 1,
            stdout="ext4\n" if is_mounted else "",
            stderr="",
        )


class _MigrationRunner(_TemporaryUnmountRunner):
    """Model image creation plus the temporary migration mount."""

    def run(self, arguments, **kwargs):
        """Create the sparse placeholder and model privileged operations."""
        command = [str(item) for item in arguments]
        if command[0] == "truncate":
            Path(command[-1]).touch()
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "umount" in command or command[0] == "findmnt":
            return super().run(arguments, **kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_temporary_unmount_retries_until_mount_disappears(tmp_path, monkeypatch):
    """A transient busy mount is retried and verified before cleanup continues."""
    runner = _TemporaryUnmountRunner(unmounted_after=3)
    sleeps = []
    monkeypatch.setattr(_DRIVER.time, "sleep", sleeps.append)

    _unmount_temporary_filesystem(runner, tmp_path / "migration-mount")

    assert runner.unmount_attempts == 3
    assert sleeps == [
        _DRIVER.TEMPORARY_UNMOUNT_RETRY_SECONDS,
        _DRIVER.TEMPORARY_UNMOUNT_RETRY_SECONDS,
    ]


def test_temporary_unmount_rejects_persistent_mount(tmp_path, monkeypatch):
    """Setup fails safely instead of deleting a still-mounted temporary path."""
    runner = _TemporaryUnmountRunner(
        unmounted_after=_DRIVER.TEMPORARY_UNMOUNT_ATTEMPTS + 1
    )
    monkeypatch.setattr(_DRIVER.time, "sleep", lambda _seconds: None)

    with pytest.raises(_DRIVER.ProvisionError, match="mount remains active"):
        _unmount_temporary_filesystem(runner, tmp_path / "migration-mount")

    assert runner.unmount_attempts == _DRIVER.TEMPORARY_UNMOUNT_ATTEMPTS


def test_nfs_migration_retains_verified_active_mountpoint(tmp_path, monkeypatch):
    """Failed unmount verification cannot trigger recursive mount cleanup."""
    state_dir = tmp_path / "state"
    export_dir = tmp_path / "export"
    state_dir.mkdir()
    export_dir.mkdir()
    mountpoint = state_dir / "known-migration-mount"

    def _make_mountpoint(**_kwargs):
        mountpoint.mkdir()
        return str(mountpoint)

    runner = _MigrationRunner(unmounted_after=_DRIVER.TEMPORARY_UNMOUNT_ATTEMPTS + 1)
    monkeypatch.setattr(_DRIVER.tempfile, "mkdtemp", _make_mountpoint)
    monkeypatch.setattr(_DRIVER.time, "sleep", lambda _seconds: None)

    with pytest.raises(_DRIVER.ProvisionError, match="mount remains active"):
        _migrate_export_data_to_image(runner, _config(state_dir, export_dir))

    assert mountpoint.is_dir()
    assert not (state_dir / "nfs-export.ext4").exists()
    assert (state_dir / "nfs-export.ext4.new").exists()
    assert runner.unmount_attempts == _DRIVER.TEMPORARY_UNMOUNT_ATTEMPTS


def test_nfs_migration_removes_mountpoint_only_after_verified_unmount(
    tmp_path, monkeypatch
):
    """Successful migration removes its now-unmounted temporary leaf."""
    state_dir = tmp_path / "state"
    export_dir = tmp_path / "export"
    state_dir.mkdir()
    export_dir.mkdir()
    mountpoint = state_dir / "known-migration-mount"

    def _make_mountpoint(**_kwargs):
        mountpoint.mkdir()
        return str(mountpoint)

    runner = _MigrationRunner(unmounted_after=1)
    monkeypatch.setattr(_DRIVER.tempfile, "mkdtemp", _make_mountpoint)

    _migrate_export_data_to_image(runner, _config(state_dir, export_dir))

    assert not mountpoint.exists()
    assert (state_dir / "nfs-export.ext4").exists()
    assert not (state_dir / "nfs-export.ext4.new").exists()
    assert runner.unmount_attempts == 1


class _CopyFailureMigrationRunner(_MigrationRunner):
    """Fail while copying retained export data into the new image."""

    def run(self, arguments, **kwargs):
        command = [str(item) for item in arguments]
        if "cp" in command:
            raise _DRIVER.ProvisionError("copy failed")
        return super().run(arguments, **kwargs)


def test_nfs_migration_does_not_publish_a_failed_image(tmp_path, monkeypatch):
    """A migration failure cannot leave an incomplete image at the final path."""
    state_dir = tmp_path / "state"
    export_dir = tmp_path / "export"
    state_dir.mkdir()
    export_dir.mkdir()
    mountpoint = state_dir / "known-migration-mount"

    def _make_mountpoint(**_kwargs):
        mountpoint.mkdir()
        return str(mountpoint)

    monkeypatch.setattr(_DRIVER.tempfile, "mkdtemp", _make_mountpoint)

    with pytest.raises(_DRIVER.ProvisionError, match="copy failed"):
        _migrate_export_data_to_image(
            _CopyFailureMigrationRunner(unmounted_after=1),
            _config(state_dir, export_dir),
        )

    assert not (state_dir / "nfs-export.ext4").exists()
    assert not (state_dir / "nfs-export.ext4.new").exists()
    assert not mountpoint.exists()


def test_nfs_server_configures_eight_workers(tmp_path, monkeypatch):
    """Rendered NFS state requests enough workers for concurrent clients."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    config.manifests_dir.mkdir(parents=True)
    runner = _RecordingRunner()
    for name in (
        "_record_nfs_service_state",
        "_ensure_export_filesystem",
        "_ensure_export_marker",
        "_ensure_nfs_firewall",
        "_set_live_nfs_threads",
    ):
        monkeypatch.setattr(_DRIVER, name, lambda *_args: None)
    monkeypatch.setattr(_DRIVER, "_read_system_file", lambda *_args: None)

    _configure_nfs(runner, config, "172.18.0.0/16", "172.18.0.1")

    daemon_config = (config.manifests_dir / "storage-scale-test-nfs.conf").read_text(
        encoding="utf-8"
    )
    assert f"threads = {_DRIVER.NFS_SERVER_THREADS}\n" in daemon_config
    assert _DRIVER.NFS_SERVER_THREADS == 8


def test_nfs_setup_rejects_another_active_v4_root(tmp_path):
    """A second NFSv4 root cannot silently redirect fixture mounts."""
    # pylint: disable=protected-access

    class _ExportRunner:
        def run(self, _arguments, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "/srv/unrelated\n"
                    "\t\t172.18.0.0/16(sync,fsid=root,rw)\n"
                    f"{tmp_path / 'expected'}\n"
                    "\t\t172.18.0.0/16(sync,fsid=0,rw)\n"
                ),
                stderr="",
            )

    with pytest.raises(_DRIVER.ProvisionError, match="redirect CSI mounts"):
        _DRIVER._assert_no_conflicting_nfs_v4_root(
            _ExportRunner(), tmp_path / "expected"
        )


def test_nfs_setup_accepts_its_own_active_v4_root(tmp_path):
    """Repeated setup accepts the already-active owned root export."""
    # pylint: disable=protected-access

    class _ExportRunner:
        def run(self, _arguments, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"{tmp_path / 'expected'}\n" "\t\t172.18.0.0/16(sync,fsid=0,rw)\n"
                ),
                stderr="",
            )

    _DRIVER._assert_no_conflicting_nfs_v4_root(_ExportRunner(), tmp_path / "expected")


@pytest.mark.parametrize("root_option", ("fsid=0", "fsid=root"))
def test_nfs_setup_rejects_same_line_active_v4_root(tmp_path, root_option):
    """The common one-line exportfs format cannot hide another v4 root."""

    class _ExportRunner:
        def run(self, _arguments, **_kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"{tmp_path / 'unrelated'} 172.18.0.0/16"
                    f"(sync,{root_option},rw)\n"
                ),
                stderr="",
            )

    with pytest.raises(_DRIVER.ProvisionError, match="redirect CSI mounts"):
        _DRIVER._assert_no_conflicting_nfs_v4_root(
            _ExportRunner(), tmp_path / "expected"
        )


def test_nfs_reconciliation_validates_previous_subnet_before_update(
    tmp_path, monkeypatch
):
    """A recreated kind subnet can replace its unchanged owned export file."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    config.manifests_dir.mkdir(parents=True)
    old_subnet = "172.18.0.0/16"
    new_subnet = "172.19.0.0/16"
    old_export = (
        f"{config.export_dir} {old_subnet}(rw,sync,no_subtree_check,fsid=0,"
        f"all_squash,anonuid={_DRIVER.NFS_UID},anongid={_DRIVER.NFS_GID})\n"
    )
    daemon = f"[nfsd]\nvers3 = n\nvers4 = y\nthreads = {_DRIVER.NFS_SERVER_THREADS}\n"
    export_source = config.manifests_dir / "storage-scale-test.exports"
    daemon_source = config.manifests_dir / "storage-scale-test-nfs.conf"
    export_source.write_text(old_export, encoding="utf-8")
    daemon_source.write_text(daemon, encoding="utf-8")
    runner = _RecordingRunner()
    for name in (
        "_record_nfs_service_state",
        "_ensure_export_filesystem",
        "_ensure_export_marker",
        "_ensure_nfs_firewall",
        "_set_live_nfs_threads",
    ):
        monkeypatch.setattr(_DRIVER, name, lambda *_args: None)
    monkeypatch.setattr(
        _DRIVER,
        "_read_system_file",
        lambda _runner, path: (
            old_export if path == _DRIVER.NFS_EXPORT_CONFIG else daemon
        ),
    )

    _configure_nfs(runner, config, new_subnet, "172.19.0.1")

    assert new_subnet in export_source.read_text(encoding="utf-8")


def test_firewall_cleanup_removes_stored_rule_while_ufw_is_inactive():
    """Teardown consults UFW configuration instead of runtime status output."""

    class _InactiveUfwRunner:
        def __init__(self):
            self.rule_exists = True
            self.commands = []

        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            self.commands.append(command)
            if command[-2:] == ["show", "added"]:
                output = ""
                if self.rule_exists:
                    output = (
                        "ufw allow from 172.18.0.0/16 to any port 2049 "
                        f"proto tcp comment '{_DRIVER.UFW_COMMENT}'\n"
                    )
                return SimpleNamespace(returncode=0, stdout=output, stderr="")
            if "delete" in command:
                self.rule_exists = False
            return SimpleNamespace(returncode=0, stdout="Status: inactive\n", stderr="")

    runner = _InactiveUfwRunner()
    _delete_nfs_firewall_rule(runner, "172.18.0.0/16")

    assert not runner.rule_exists
    assert any("delete" in command for command in runner.commands)


class _ImageCapacityRunner:
    """Apply sparse truncation while recording filesystem growth commands."""

    def __init__(self):
        self.commands = []

    def run(self, arguments, **_kwargs):
        """Record a command and enact only the harmless sparse resize."""
        command = [str(item) for item in arguments]
        self.commands.append(command)
        if command[0] == "truncate":
            os.truncate(command[-1], int(command[-2]))
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_existing_nfs_image_grows_online_to_advertised_capacity(tmp_path):
    """Repeated setup expands both a retained sparse image and loop device."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    config.state_dir.mkdir()
    config.nfs_image.touch()
    os.truncate(config.nfs_image, 128 * 1024 * 1024)
    runner = _ImageCapacityRunner()

    _ensure_nfs_image_capacity(runner, config, "/dev/loop9")

    assert config.nfs_image.stat().st_size == _DRIVER.NFS_IMAGE_BYTES
    assert _DRIVER.NFS_IMAGE_BYTES == 4 * _DRIVER.GIB
    rendered = [" ".join(command) for command in runner.commands]
    assert any("losetup --set-capacity /dev/loop9" in line for line in rendered)
    assert any("resize2fs /dev/loop9" in line for line in rendered)


def test_fixture_capacity_budget_drives_storage_declarations():
    """The backing image and manifests share one explicit capacity budget."""
    assert _DRIVER.NFS_IMAGE_BYTES >= _DRIVER.NFS_BUDGET_BYTES
    assert _DRIVER.NFS_BUDGET_BYTES > 2 * _DRIVER.GIB
    for name in ("nfs-storage.yaml.tmpl", "sbx-storage.yaml.tmpl"):
        text = (_REPO_ROOT / "integration-tests" / "manifests" / name).read_text(
            encoding="utf-8"
        )
        assert "@@STORAGE_TEST_CAPACITY@@" in text
        assert "@@SSH_HOME_CAPACITY@@" in text


def test_mounted_nfs_capacity_must_satisfy_the_runtime_budget(tmp_path):
    """A retained image cannot pass based on sparse-file length alone."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    runner = SimpleNamespace(
        run=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=f"{_DRIVER.NFS_BUDGET_BYTES // 4096} 4096 1\n",
            stderr="",
        )
    )

    with pytest.raises(_DRIVER.ProvisionError, match="capacity budget"):
        _validate_nfs_filesystem_capacity(runner, config)


class _NfsThreadRunner:
    """Model a live pre-existing NFS service and its mutable worker count."""

    def __init__(self, threads, *, active=True, enabled=False):
        self.threads = threads
        self.active = active
        self.enabled = enabled
        self.commands = []

    def run(self, arguments, **_kwargs):
        """Return and update the modeled live NFS worker count."""
        command = [str(item) for item in arguments]
        self.commands.append(command)
        if "show" in command:
            return SimpleNamespace(returncode=0, stdout="loaded\n", stderr="")
        if "is-active" in command:
            return SimpleNamespace(
                returncode=0 if self.active else 3,
                stdout="active\n" if self.active else "inactive\n",
                stderr="",
            )
        if "is-enabled" in command:
            return SimpleNamespace(
                returncode=0 if self.enabled else 1,
                stdout="enabled\n" if self.enabled else "disabled\n",
                stderr="",
            )
        if str(_DRIVER.NFS_THREADS_PATH) in command:
            return SimpleNamespace(returncode=0, stdout=f"{self.threads}\n", stderr="")
        if any(Path(item).name == "rpc.nfsd" for item in command):
            self.threads = int(command[-1])
        if "systemctl" in command and "stop" in command:
            self.active = False
        if "systemctl" in command and "enable" in command:
            self.enabled = True
        if "systemctl" in command and "disable" in command:
            self.enabled = False
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_preexisting_nfs_worker_count_is_reconciled_and_restored(tmp_path, monkeypatch):
    """Setup applies eight workers and teardown restores the host's prior count."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    config.state_dir.mkdir()
    runner = _NfsThreadRunner(threads=2)
    monkeypatch.setattr(_DRIVER.shutil, "which", lambda _name: "/usr/sbin/rpc.nfsd")

    _record_nfs_service_state(runner, config)
    _set_live_nfs_threads(runner, _DRIVER.NFS_SERVER_THREADS)
    _record_nfs_service_state(runner, config)

    state = json.loads(
        (config.state_dir / "nfs-service.json").read_text(encoding="utf-8")
    )
    assert state == {
        "previous_threads": 2,
        "schema": _DRIVER.NFS_SERVICE_STATE_SCHEMA,
        "service_existed": True,
        "was_active": True,
        "was_enabled": False,
    }
    assert runner.threads == 8

    _restore_nfs_service_state(runner, config)

    assert runner.threads == 2
    assert any("disable" in command for command in runner.commands)


def test_nfs_service_records_active_and_enabled_states_independently(
    tmp_path, monkeypatch
):
    """Runtime ownership cannot be inferred from systemd boot policy."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    config.state_dir.mkdir()
    runner = _NfsThreadRunner(threads=3, enabled=True)
    monkeypatch.setattr(_DRIVER.shutil, "which", lambda _name: "/usr/sbin/rpc.nfsd")

    _record_nfs_service_state(runner, config)

    state = json.loads(
        (config.state_dir / "nfs-service.json").read_text(encoding="utf-8")
    )
    assert state["service_existed"] is True
    assert state["was_active"] is True
    assert state["was_enabled"] is True
    assert state["previous_threads"] == 3


def test_enabled_inactive_nfs_service_is_stopped_but_remains_enabled(
    tmp_path, monkeypatch
):
    """Teardown restores runtime and boot states without conflating them."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    config.state_dir.mkdir()
    runner = _NfsThreadRunner(threads=0, active=False, enabled=True)
    monkeypatch.setattr(_DRIVER.shutil, "which", lambda _name: "/usr/sbin/rpc.nfsd")
    _record_nfs_service_state(runner, config)
    runner.active = True

    _stop_owned_nfs(runner, config)
    _restore_nfs_service_state(runner, config)

    assert runner.active is False
    assert runner.enabled is True


def test_observed_kubernetes_version_requires_three_matching_nodes(tmp_path):
    """Retained cluster identity comes from kubelets rather than configured pins."""
    config = _config(tmp_path / "state", tmp_path / "export")

    class _NodeRunner:
        def __init__(self, versions):
            self.versions = versions

        def run(self, _arguments, **_kwargs):
            items = [
                {"status": {"nodeInfo": {"kubeletVersion": version}}}
                for version in self.versions
            ]
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"items": items}), stderr=""
            )

    assert (
        _observed_kubernetes_version(
            _NodeRunner(["v1.34.0", "v1.34.0", "v1.34.0"]), config
        )
        == "v1.34.0"
    )
    with pytest.raises(_DRIVER.ProvisionError, match="one kubelet version"):
        _observed_kubernetes_version(
            _NodeRunner(["v1.34.0", "v1.34.0", "v1.33.0"]), config
        )


def test_expected_kubernetes_version_is_independent_of_kubectl(monkeypatch):
    """A client-only kubectl upgrade does not invalidate a retained cluster."""
    monkeypatch.setattr(_DRIVER, "KUBECTL_VERSION", "v9.9.9")
    monkeypatch.setattr(_DRIVER, "SBX_KUBECTL_VERSION", "v8.8.8")

    assert _expected_kubernetes_version("nfs") == _DRIVER.KUBERNETES_VERSION
    assert _expected_kubernetes_version("sbx-shared") == _DRIVER.SBX_KUBERNETES_VERSION


class _DeadlineClock:
    """Deterministic monotonic clock for overall-deadline tests."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        """Return the simulated time."""
        return self.now

    def sleep(self, seconds):
        """Advance by one requested sleep."""
        self.now += seconds


class _DeadlineRunner:
    """Consume each subprocess timeout without crossing the overall bound."""

    def __init__(self, clock, deadline, *, pods=False):
        self.clock = clock
        self.deadline = deadline
        self.pods = pods
        self.calls = 0

    def run(self, _arguments, *, timeout=None, **_kwargs):
        """Model a subprocess that consumes its complete allowed timeout."""
        assert timeout is not None
        assert self.clock.now + timeout <= self.deadline
        self.clock.now += timeout
        self.calls += 1
        items = [{"metadata": {"name": "ssh-worker-0"}}] if self.pods else []
        return SimpleNamespace(
            returncode=1 if not self.pods else 0,
            stdout=json.dumps({"items": items}),
            stderr="unavailable",
        )


def test_kubernetes_api_poll_respects_overall_deadline(tmp_path, monkeypatch):
    """One wedged kubectl call cannot exceed the readiness deadline."""
    clock = _DeadlineClock()
    runner = _DeadlineRunner(clock, 90)
    config = _config(tmp_path / "state", tmp_path / "export")
    monkeypatch.setattr(_DRIVER.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(_DRIVER.time, "sleep", clock.sleep)

    with pytest.raises(_DRIVER.ProvisionError, match="within 90 seconds"):
        _wait_for_kube_api(runner, config)

    assert runner.calls > 1
    assert clock.now == 90


def test_ssh_disappearance_poll_respects_overall_deadline(tmp_path, monkeypatch):
    """SSH transition timeout performs no extra post-deadline pod query."""
    clock = _DeadlineClock()
    runner = _DeadlineRunner(clock, 180, pods=True)
    config = _config(tmp_path / "state", tmp_path / "export")
    monkeypatch.setattr(_DRIVER.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(_DRIVER.time, "sleep", clock.sleep)

    with pytest.raises(_DRIVER.ProvisionError, match="did not terminate"):
        _wait_for_no_ssh_pods(runner, config)

    assert runner.calls > 1
    assert clock.now == 180


def test_slurm_wait_reports_cleanly_when_deadline_precedes_first_poll(
    tmp_path, monkeypatch
):
    """Process suspension before the first query still yields a useful error."""
    times = iter((0.0, 301.0))
    monkeypatch.setattr(_DRIVER.time, "monotonic", lambda: next(times))
    config = _config(tmp_path / "state", tmp_path / "export")

    with pytest.raises(_DRIVER.ProvisionError, match="'slurmd': 0"):
        _wait_for_slurm(None, config)


def test_retained_cluster_profile_uses_observed_kubelet_version(tmp_path, monkeypatch):
    """A pin change marks an otherwise-running disposable cluster stale."""
    config = _config(tmp_path / "state", tmp_path / "export")
    monkeypatch.setattr(_DRIVER, "_wait_for_kube_api", lambda *_args: None)
    monkeypatch.setattr(
        _DRIVER, "_observed_kubernetes_version", lambda *_args: "v1.33.0"
    )

    assert not _retained_cluster_matches_profile(
        SimpleNamespace(), config, "sbx-shared"
    )


def test_retained_cluster_replaces_a_missing_cni(tmp_path, monkeypatch):
    """A missing retained CNI is profile drift rather than an uncaught error."""
    config = _config(tmp_path / "state", tmp_path / "export")
    config.state_dir.mkdir()
    (config.state_dir / "storage-backend.json").write_text(
        json.dumps({"profile": _fixture_profile(config, "sbx-shared")}),
        encoding="utf-8",
    )
    monkeypatch.setattr(_DRIVER, "_wait_for_kube_api", lambda *_args: None)
    monkeypatch.setattr(
        _DRIVER,
        "_observed_kubernetes_version",
        lambda *_args: _DRIVER.SBX_KUBERNETES_VERSION,
    )
    monkeypatch.setattr(
        _DRIVER,
        "_validate_network_policy_profile",
        lambda *_args: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["kubectl", "get", "daemonset"])
        ),
    )

    assert not _retained_cluster_matches_profile(
        SimpleNamespace(), config, "sbx-shared"
    )


def test_fixture_profile_pins_backend_specific_policy_cni(tmp_path):
    """Retained clusters include their exact policy-capable CNI identity."""
    config = _config(tmp_path / "state", tmp_path / "export")

    nfs = _fixture_profile(config, "nfs")
    sbx = _fixture_profile(config, "sbx-shared")

    assert nfs["cni"] == "kindnet"
    assert nfs["cni_image"] == _DRIVER.KINDNET_IMAGE
    assert sbx["cni"] == "calico"
    assert sbx["cni_version"] == _DRIVER.CALICO_VERSION
    assert sbx["cni_manifest_sha256"] == _DRIVER.CALICO_MANIFEST_SHA256
    assert sbx["cni_recipe"] == _DRIVER.CALICO_SBX_RECIPE
    assert sbx["cni_pod_subnet"] == _DRIVER.CALICO_POD_SUBNET


def test_kindnet_profile_requires_all_fixture_nodes_and_exact_image(tmp_path):
    """A retained CNI must be healthy and match its pinned node profile."""
    config = _config(tmp_path / "state", tmp_path / "export")
    document = {
        "spec": {
            "template": {"spec": {"containers": [{"image": _DRIVER.KINDNET_IMAGE}]}}
        },
        "status": {
            "desiredNumberScheduled": 3,
            "numberReady": 3,
            "numberAvailable": 3,
        },
    }

    class _KindnetRunner:
        def run(self, _arguments, **_kwargs):
            return SimpleNamespace(stdout=json.dumps(document))

    runner = _KindnetRunner()
    assert _validate_kindnet_profile(runner, config) is None
    document["status"]["numberReady"] = 2
    with pytest.raises(_DRIVER.ProvisionError, match="does not match"):
        _validate_kindnet_profile(runner, config)


def test_sbx_calico_render_uses_only_preloaded_images_and_omits_securityfs(
    tmp_path, monkeypatch
):
    """The SBX recipe changes only images, pulls, and its optional mount."""
    config = _config(tmp_path / "state", tmp_path / "export")
    config.manifests_dir.mkdir(parents=True)
    source = tmp_path / "calico.yaml"
    securityfs_mount = """            # Felix reads /sys/kernel/security/lockdown to detect kernel
            # lockdown=confidentiality; under it, ftrace is disabled and Felix
            # loads trace-printk-free BPF program variants. securityfs is a
            # separate filesystem from /sys/fs, so it needs its own mount.
            - name: sys-kernel-security
              mountPath: /sys/kernel/security
              readOnly: true
"""
    securityfs_volume = """        # securityfs, read by Felix to detect kernel lockdown=confidentiality.
        # No type set (like nodeproc below) so nodes without securityfs still
        # start; Felix treats an unreadable lockdown file as "not locked down".
        - name: sys-kernel-security
          hostPath:
            path: /sys/kernel/security
"""
    images = "\n".join(
        [
            f"image: quay.io/calico/cni:{_DRIVER.CALICO_VERSION}",
            f"image: quay.io/calico/cni:{_DRIVER.CALICO_VERSION}",
            f"image: quay.io/calico/node:{_DRIVER.CALICO_VERSION}",
            f"image: quay.io/calico/node:{_DRIVER.CALICO_VERSION}",
            f"image: quay.io/calico/kube-controllers:{_DRIVER.CALICO_VERSION}",
        ]
    )
    source.write_text(
        f"{images}\nimagePullPolicy: IfNotPresent\n"
        "          env:\n"
        "            # Use Kubernetes API as the backing datastore.\n"
        f"{securityfs_mount}{securityfs_volume}",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        _DRIVER, "_ensure_calico_manifest", lambda _runner, _config: source
    )

    rendered = _render_calico_manifest(None, config).read_text(encoding="utf-8")

    assert "quay.io/calico/" not in rendered
    assert "imagePullPolicy: Never" in rendered
    assert "/sys/kernel/security" not in rendered
    assert 'name: FELIX_BPFENABLED\n              value: "false"' in rendered
    for component in dict(_DRIVER.CALICO_IMAGES):
        expected = 2 if component in {"cni", "node"} else 1
        assert rendered.count(_calico_image(component)[1]) == expected


def test_local_scenario_failure_diagnostics_survive_workspace_cleanup(tmp_path):
    """SSH logs and partial results are copied outside disposable workspace."""
    workspace = tmp_path / "workspace"
    (workspace / "logs").mkdir(parents=True)
    (workspace / "results").mkdir()
    (workspace / "logs" / "service.log").write_text("service failed\n")
    (workspace / "results" / "coordinator.log").write_text("probe failed\n")
    scenario = SimpleNamespace(name="example")
    runtime = _FILESYSTEM.ScenarioRuntime(
        scenario=scenario,
        selector="ssh",
        workspace=str(workspace),
        local_workspace=tmp_path,
        artifact_root=tmp_path / "artifacts",
        data_root="/mnt/storage-test/example",
        values={},
        copied_results=[],
    )
    log_dir = tmp_path / "retained"
    log_dir.mkdir()

    _preserve_scenario_failure_diagnostics(
        _RecordingRunner(),
        SimpleNamespace(),
        SimpleNamespace(),
        runtime,
        log_dir,
        RuntimeError("scenario failed"),
    )
    shutil.rmtree(workspace)

    diagnostics = log_dir / "failure-diagnostics"
    assert (diagnostics / "logs" / "service.log").read_text() == "service failed\n"
    assert (diagnostics / "results" / "coordinator.log").read_text() == "probe failed\n"
    assert "scenario failed" in (diagnostics / "failure.txt").read_text()


def _cleanup_runtime(tmp_path):
    """Return one marker-safe scenario runtime for cleanup tests."""
    return _FILESYSTEM.ScenarioRuntime(
        scenario=SimpleNamespace(name="example"),
        selector="ssh",
        workspace=str(tmp_path / "workspace"),
        local_workspace=tmp_path,
        artifact_root=tmp_path / "artifacts",
        data_root=f"{_FILESYSTEM.REMOTE_BASE}/test-data/example",
        values={},
        copied_results=[],
    )


def test_scenario_storage_cleanup_failure_is_fatal(tmp_path):
    """A successful workload cannot pass while isolated storage remains."""
    runner = SimpleNamespace(
        run=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="permission denied"
        )
    )
    fixture = SimpleNamespace(login_pod="login", login_container="login")

    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="permission denied"):
        _cleanup_scenario_storage(
            runner,
            _config(tmp_path / "state", tmp_path / "export"),
            fixture,
            _cleanup_runtime(tmp_path),
        )


def test_expected_failure_step_rejects_zero_exit(tmp_path, monkeypatch):
    """Failure injection must propagate failure through the sweep entry point."""
    monkeypatch.setattr(_FILESYSTEM, "_test_command", lambda *_args: ["sweep"])
    runner = SimpleNamespace(
        run=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="recorded FAILED state\n", stderr=""
        )
    )

    with pytest.raises(
        _FILESYSTEM.IntegrationTestError, match="unexpectedly succeeded"
    ):
        _run_step(
            runner,
            object(),
            object(),
            "slurm",
            "injected-attempt",
            "true",
            tmp_path,
            30,
            expected_failure=True,
        )


def test_ssh_home_cleanup_failure_is_fatal(tmp_path):
    """Failed worker-home cleanup cannot silently contaminate later scenarios."""
    pod = {
        "metadata": {"name": "ssh-worker-0"},
        "spec": {"containers": [{"name": "sshd"}]},
        "status": {
            "phase": "Running",
            "containerStatuses": [{"name": "sshd", "ready": True}],
        },
    }

    class _SshCleanupRunner:
        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            if "get" in command and "pods" in command:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps({"items": [pod]}),
                    stderr="",
                )
            return SimpleNamespace(
                returncode=1, stdout="", stderr="remote cleanup failed"
            )

    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="remote cleanup"):
        _cleanup_ssh_remote_results(
            _SshCleanupRunner(),
            _config(tmp_path / "state", tmp_path / "export"),
        )


def test_secondary_cleanup_failure_is_retained_without_replacing_primary(tmp_path):
    """Cleanup diagnostics survive beside an already-recorded workload error."""
    _record_secondary_cleanup_failure(tmp_path, RuntimeError("cleanup failed"))

    error = tmp_path / "failure-diagnostics" / "cleanup-error.txt"
    assert error.read_text(encoding="utf-8") == "RuntimeError: cleanup failed\n"


def test_slurm_allocation_outlives_slowest_scenario():
    """The scheduler cannot expire before a declared harness deadline."""
    assert _slurm_run_time(600) == "00:12:00"


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

    events = []

    class _SbxProbeRunner:
        def run(self, arguments, **_kwargs):
            events.append(("run", str(arguments[0])))
            token = str(arguments[-1])
            (config.sbx_shared_root / "engine-probe").write_text(
                token + "\n", encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_DRIVER, "_repository_root", lambda: repository)
    monkeypatch.setattr(
        _DRIVER,
        "_ensure_pinned_image",
        lambda _runner, image: events.append(("acquire", image)),
    )

    _prepare_sbx_shared(_SbxProbeRunner(), config)

    assert events[:2] == [
        ("acquire", _DRIVER.SBX_KIND_NODE_IMAGE),
        ("run", "docker"),
    ]
    for name in ("storage-test", "ssh-home"):
        mode = (config.sbx_shared_root / name).stat().st_mode & 0o7777
        assert mode == _DRIVER.SBX_SHARED_DIRECTORY_MODE == 0o777


@pytest.mark.parametrize("child_kind", ("symlink", "file"))
def test_sbx_shared_setup_rejects_unsafe_child_paths(tmp_path, monkeypatch, child_kind):
    """SBX setup never chmods or binds a symlink or non-directory child."""
    repository = tmp_path / "repository"
    (repository / "tmp").mkdir(parents=True)
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"),
        sbx_shared_root=repository / "tmp" / "shared",
    )
    config.sbx_shared_root.mkdir(parents=True)
    (config.sbx_shared_root / _DRIVER.EXPORT_MARKER).write_text(
        json.dumps(_DRIVER._sbx_shared_marker(config)), encoding="utf-8"
    )
    child = config.sbx_shared_root / "storage-test"
    if child_kind == "symlink":
        target = tmp_path / "outside"
        target.mkdir()
        child.symlink_to(target, target_is_directory=True)
    else:
        child.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(_DRIVER, "_repository_root", lambda: repository)
    monkeypatch.setattr(_DRIVER, "_ensure_pinned_image", lambda *_args: None)

    with pytest.raises(_DRIVER.ProvisionError, match="unsafe SBX shared child path"):
        _prepare_sbx_shared(_RecordingRunner(), config)


def test_sbx_shared_setup_rejects_symlinked_root(tmp_path, monkeypatch):
    """SBX setup cannot bind a repository path that redirects elsewhere."""
    repository = tmp_path / "repository"
    (repository / "tmp").mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    root = repository / "tmp" / "shared"
    root.symlink_to(target, target_is_directory=True)
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"),
        sbx_shared_root=root,
    )
    monkeypatch.setattr(_DRIVER, "_repository_root", lambda: repository)

    with pytest.raises(_DRIVER.ProvisionError, match="unsafe Docker SBX shared root"):
        _prepare_sbx_shared(_RecordingRunner(), config)


def test_sbx_shared_setup_migrates_legacy_marker(tmp_path, monkeypatch):
    """A marker from the previous driver version is upgraded in place."""
    repository = tmp_path / "repository"
    (repository / "tmp").mkdir(parents=True)
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"),
        sbx_shared_root=repository / "tmp" / "shared",
    )
    config.sbx_shared_root.mkdir(parents=True)
    legacy_marker = {
        **_DRIVER._owner_document(config),
        "backend": "sbx-shared",
        "shared_root": str(config.sbx_shared_root),
    }
    (config.sbx_shared_root / _DRIVER.EXPORT_MARKER).write_text(
        json.dumps(legacy_marker), encoding="utf-8"
    )

    class _SbxProbeRunner:
        def run(self, arguments, **_kwargs):
            (config.sbx_shared_root / "engine-probe").write_text(
                str(arguments[-1]) + "\n", encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_DRIVER, "_repository_root", lambda: repository)
    monkeypatch.setattr(_DRIVER, "_ensure_pinned_image", lambda *_args: None)

    _prepare_sbx_shared(_SbxProbeRunner(), config)

    assert json.loads(
        (config.sbx_shared_root / _DRIVER.EXPORT_MARKER).read_text(encoding="utf-8")
    ) == _DRIVER._sbx_shared_marker(config)


def test_sbx_shared_root_journal_recovers_interrupted_root_creation(
    tmp_path, monkeypatch
):
    """A crash before the root marker can reclaim only the journaled empty root."""
    repository = tmp_path / "repository"
    (repository / "tmp").mkdir(parents=True)
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"),
        sbx_shared_root=repository / "tmp" / "shared",
    )
    config.state_dir.mkdir()
    owner_document = json.dumps(_DRIVER._owner_document(config))
    (config.state_dir / _DRIVER.STATE_MARKER).write_text(
        owner_document, encoding="utf-8"
    )
    (config.state_dir / "cluster-owner.json").write_text(
        owner_document, encoding="utf-8"
    )
    original_write = _DRIVER._write_text

    def fail_root_marker(path, text, mode=0o640):
        if path == config.sbx_shared_root / _DRIVER.EXPORT_MARKER:
            raise OSError("simulated shared marker failure")
        original_write(path, text, mode)

    monkeypatch.setattr(_DRIVER, "_repository_root", lambda: repository)
    monkeypatch.setattr(_DRIVER, "_ensure_pinned_image", lambda *_args: None)
    monkeypatch.setattr(_DRIVER, "_write_text", fail_root_marker)
    with pytest.raises(OSError, match="simulated shared marker failure"):
        _prepare_sbx_shared(_RecordingRunner(), config)
    assert config.sbx_shared_root.is_dir()
    assert list(config.sbx_shared_root.iterdir()) == []
    assert (config.state_dir / _DRIVER.SBX_SHARED_JOURNAL).is_file()

    monkeypatch.setattr(_DRIVER, "_validate_image_ownership", lambda *_args: None)
    assert _validate_sbx_teardown_ownership(_RecordingRunner(), config) == (
        True,
        True,
        True,
    )


def test_sbx_shared_teardown_rejects_root_owned_by_another_state(tmp_path, monkeypatch):
    """A shared-root marker cannot authorize a different state directory."""
    repository = tmp_path / "repository"
    (repository / "tmp").mkdir(parents=True)
    owner = replace(
        _config(tmp_path / "owner-state", tmp_path / "owner-export"),
        sbx_shared_root=repository / "tmp" / "shared",
    )
    other = replace(owner, state_dir=tmp_path / "other-state")
    owner.state_dir.mkdir()
    other.state_dir.mkdir()
    for config in (owner, other):
        owner_document = json.dumps(_DRIVER._owner_document(config))
        (config.state_dir / _DRIVER.STATE_MARKER).write_text(
            owner_document, encoding="utf-8"
        )
        (config.state_dir / "cluster-owner.json").write_text(
            owner_document, encoding="utf-8"
        )
    owner.sbx_shared_root.mkdir(parents=True)
    (owner.sbx_shared_root / _DRIVER.EXPORT_MARKER).write_text(
        json.dumps(_DRIVER._sbx_shared_marker(owner)), encoding="utf-8"
    )
    (owner.sbx_shared_root / "storage-test").mkdir()
    (owner.sbx_shared_root / "ssh-home").mkdir()
    monkeypatch.setattr(_DRIVER, "_repository_root", lambda: repository)

    with pytest.raises(_DRIVER.ProvisionError, match="unowned SBX shared root"):
        _validate_sbx_teardown_ownership(_RecordingRunner(), other)


def test_sbx_shared_teardown_requires_state_and_cluster_ownership(
    tmp_path, monkeypatch
):
    """A matching shared marker is insufficient without setup ownership."""
    repository = tmp_path / "repository"
    (repository / "tmp").mkdir(parents=True)
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"),
        sbx_shared_root=repository / "tmp" / "shared",
    )
    config.state_dir.mkdir()
    (config.state_dir / _DRIVER.STATE_MARKER).write_text(
        json.dumps(_DRIVER._owner_document(config)), encoding="utf-8"
    )
    config.sbx_shared_root.mkdir(parents=True)
    (config.sbx_shared_root / _DRIVER.EXPORT_MARKER).write_text(
        json.dumps(_DRIVER._sbx_shared_marker(config)), encoding="utf-8"
    )
    (config.sbx_shared_root / "storage-test").mkdir()
    (config.sbx_shared_root / "ssh-home").mkdir()
    monkeypatch.setattr(_DRIVER, "_repository_root", lambda: repository)

    with pytest.raises(
        _DRIVER.ProvisionError, match="matching state and cluster ownership"
    ):
        _validate_sbx_teardown_ownership(_RecordingRunner(), config)


@pytest.mark.parametrize("marker_name", (_DRIVER.STATE_MARKER, "cluster-owner.json"))
def test_sbx_shared_teardown_rejects_directory_ownership_marker(
    tmp_path, monkeypatch, marker_name
):
    """A directory cannot impersonate either durable SBX owner marker."""
    repository = tmp_path / "repository"
    (repository / "tmp").mkdir(parents=True)
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"),
        sbx_shared_root=repository / "tmp" / "shared",
    )
    config.state_dir.mkdir()
    (config.state_dir / marker_name).mkdir()
    other = (
        "cluster-owner.json"
        if marker_name == _DRIVER.STATE_MARKER
        else _DRIVER.STATE_MARKER
    )
    (config.state_dir / other).write_text(
        json.dumps(_DRIVER._owner_document(config)), encoding="utf-8"
    )
    monkeypatch.setattr(_DRIVER, "_repository_root", lambda: repository)

    with pytest.raises(_DRIVER.ProvisionError, match="unsafe ownership marker"):
        _validate_sbx_teardown_ownership(_RecordingRunner(), config)


def test_sbx_shared_teardown_migrates_legacy_marker(tmp_path, monkeypatch):
    """Teardown upgrades a legacy marker only after both ownership checks."""
    repository = tmp_path / "repository"
    (repository / "tmp").mkdir(parents=True)
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"),
        sbx_shared_root=repository / "tmp" / "shared",
    )
    config.state_dir.mkdir()
    owner_document = json.dumps(_DRIVER._owner_document(config))
    (config.state_dir / _DRIVER.STATE_MARKER).write_text(
        owner_document, encoding="utf-8"
    )
    (config.state_dir / "cluster-owner.json").write_text(
        owner_document, encoding="utf-8"
    )
    config.sbx_shared_root.mkdir(parents=True)
    legacy_marker = {
        **_DRIVER._owner_document(config),
        "backend": "sbx-shared",
        "shared_root": str(config.sbx_shared_root),
    }
    (config.sbx_shared_root / _DRIVER.EXPORT_MARKER).write_text(
        json.dumps(legacy_marker), encoding="utf-8"
    )
    (config.sbx_shared_root / "storage-test").mkdir()
    (config.sbx_shared_root / "ssh-home").mkdir()
    monkeypatch.setattr(_DRIVER, "_repository_root", lambda: repository)

    assert _validate_sbx_teardown_ownership(_RecordingRunner(), config) == (
        True,
        True,
        True,
    )
    assert json.loads(
        (config.sbx_shared_root / _DRIVER.EXPORT_MARKER).read_text(encoding="utf-8")
    ) == _DRIVER._sbx_shared_marker(config)


def test_lifecycle_rejects_symlinked_state_marker(tmp_path):
    """A symlink cannot impersonate the state ownership marker."""
    config = _config(tmp_path / "state", tmp_path / "export")
    config.state_dir.mkdir()
    target = tmp_path / "outside-marker"
    target.write_text(json.dumps(_DRIVER._owner_document(config)), encoding="utf-8")
    (config.state_dir / _DRIVER.STATE_MARKER).symlink_to(target)

    with pytest.raises(_DRIVER.ProvisionError, match="unowned setup state"):
        _validate_lifecycle_paths(config)


@pytest.mark.parametrize("path_name", ("state_dir", "export_dir", "nfs_image"))
def test_lifecycle_rejects_symlinked_path_roots(tmp_path, path_name):
    """Lifecycle paths cannot redirect setup or cleanup to another inode."""
    state_dir = tmp_path / "state"
    export_dir = tmp_path / "export"
    config = _config(state_dir, export_dir)
    target = tmp_path / f"{path_name}-target"
    target.mkdir()
    if path_name == "state_dir":
        state_dir.symlink_to(target, target_is_directory=True)
    elif path_name == "export_dir":
        export_dir.symlink_to(target, target_is_directory=True)
    else:
        state_dir.mkdir()
        (state_dir / "nfs-export.ext4").symlink_to(target)

    with pytest.raises(_DRIVER.ProvisionError, match="symlinked"):
        _validate_lifecycle_paths(config)


@pytest.mark.parametrize("scope", ("root", "parent"))
def test_config_preserves_cli_symlinks_for_rejection(tmp_path, monkeypatch, scope):
    """CLI normalization stays absolute without hiding symlink components."""
    target = tmp_path / "target"
    target.mkdir()
    if scope == "root":
        state_argument = tmp_path / "state-link"
        state_argument.symlink_to(target, target_is_directory=True)
    else:
        state_parent = tmp_path / "parent-link"
        state_parent.symlink_to(target, target_is_directory=True)
        state_argument = state_parent / "nested"
    export_argument = tmp_path / "export"
    monkeypatch.setattr(
        _DRIVER,
        "_test_account",
        lambda: SimpleNamespace(
            pw_name="tester", pw_uid=os.getuid(), pw_gid=os.getgid()
        ),
    )
    arguments = _DRIVER._parser().parse_args(
        [
            "--state-dir",
            str(state_argument),
            "--export-dir",
            str(export_argument),
            "setup",
        ]
    )

    config = _DRIVER._config(arguments)

    assert config.state_dir == Path(os.path.abspath(state_argument))
    with pytest.raises(_DRIVER.ProvisionError, match="symlinked state path"):
        _validate_lifecycle_paths(config)


def test_cluster_ownership_rejects_symlinked_marker(tmp_path):
    """Cluster setup cannot trust an ownership marker outside state."""
    config = _config(tmp_path / "state", tmp_path / "export")
    config.state_dir.mkdir()
    target = tmp_path / "outside-cluster-marker"
    target.write_text(
        json.dumps(
            {"schema": _DRIVER.STATE_SCHEMA, "cluster_name": config.cluster_name}
        ),
        encoding="utf-8",
    )
    (config.state_dir / "cluster-owner.json").symlink_to(target)

    with pytest.raises(_DRIVER.ProvisionError, match="symlinked cluster ownership"):
        _DRIVER._ensure_cluster_ownership(config, cluster_exists=False)


def test_cluster_ownership_rejects_directory_marker(tmp_path):
    """Cluster setup requires its ownership marker to be a regular file."""
    config = _config(tmp_path / "state", tmp_path / "export")
    (config.state_dir / "cluster-owner.json").mkdir(parents=True)

    with pytest.raises(_DRIVER.ProvisionError, match="unsafe cluster ownership"):
        _DRIVER._ensure_cluster_ownership(config, cluster_exists=False)


def test_sbx_cleanup_preacquires_its_container_image(tmp_path, monkeypatch):
    """SBX teardown cannot bypass verified image acquisition either."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"),
        sbx_shared_root=tmp_path / "shared",
    )
    config.sbx_shared_root.mkdir()
    (config.sbx_shared_root / _DRIVER.EXPORT_MARKER).touch()
    events = []

    class _CleanupRunner:
        def run(self, arguments, **_kwargs):
            events.append(("run", str(arguments[0])))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        _DRIVER,
        "_ensure_pinned_image",
        lambda _runner, image: events.append(("acquire", image)),
    )

    _remove_sbx_shared(_CleanupRunner(), config)

    assert events == [
        ("acquire", _DRIVER.SBX_KIND_NODE_IMAGE),
        ("run", "docker"),
    ]


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
    """Bootstrap cannot adopt a nonempty existing directory."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "foreign.txt").write_text("do not adopt", encoding="utf-8")
    config = _config(state_dir, tmp_path / "export")

    with pytest.raises(_DRIVER.ProvisionError, match="unowned setup state"):
        _validate_lifecycle_paths(config)


def test_empty_unmarked_state_recovers_after_marker_write_failure(
    tmp_path, monkeypatch
):
    """An interrupted first marker write can safely bootstrap on retry."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = _config(state_dir, tmp_path / "export")
    original_write = _DRIVER._write_text
    failed = True

    def fail_once(path, text, mode=0o640):
        nonlocal failed
        if failed and path == state_dir / _DRIVER.STATE_MARKER:
            failed = False
            raise OSError("simulated marker write failure")
        original_write(path, text, mode)

    monkeypatch.setattr(_DRIVER, "_write_text", fail_once)
    _validate_lifecycle_paths(config)
    with pytest.raises(OSError, match="simulated marker write failure"):
        _bootstrap_state_dir(config)
    assert not (state_dir / _DRIVER.STATE_MARKER).exists()

    _validate_lifecycle_paths(config)
    _bootstrap_state_dir(config)
    assert (state_dir / _DRIVER.STATE_MARKER).is_file()


@pytest.mark.parametrize(
    ("writer", "content"),
    ((_write_text, "content\n"), (_write_bytes, b"content\n")),
)
def test_atomic_writer_removes_temporary_file_after_publish_failure(
    tmp_path, monkeypatch, writer, content
):
    """A failed atomic publish does not poison later ownership recovery."""
    destination = tmp_path / "state" / "owner"
    original_replace = Path.replace

    def fail_destination_replace(path, target):
        if Path(target) == destination:
            raise OSError("injected publish failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_destination_replace)
    with pytest.raises(OSError, match="injected publish failure"):
        writer(destination, content)

    assert destination.parent.is_dir()
    assert not any(destination.parent.iterdir())


def test_cached_file_publish_removes_temporary_file_after_failure(
    tmp_path, monkeypatch
):
    """Cached downloads and extracted binaries cannot leave stale candidates."""
    temporary = tmp_path / "temporary"
    destination = tmp_path / "published"
    temporary.write_bytes(b"content")

    def fail_replace(_path, _target):
        raise OSError("injected cache publish failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="injected cache publish failure"):
        _publish_temporary_file(temporary, destination, 0o640)

    assert not temporary.exists()
    assert not destination.exists()


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


def test_test_state_rejects_explicit_backend_mismatch(tmp_path):
    """A test action cannot silently ignore its explicit backend selection."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    retained = replace(_config(state_dir, tmp_path / "export"), storage_backend="nfs")
    _write_completed_state(retained, "nfs")
    requested = replace(retained, storage_backend="sbx-shared")

    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="does not match"):
        _load_state(requested)


def test_system_directory_cannot_be_used_as_state(tmp_path):
    """An existing broad system directory cannot be marked during setup."""
    config = _config(Path("/usr"), tmp_path / "export")

    with pytest.raises(_DRIVER.ProvisionError, match="not owned by the current user"):
        _validate_lifecycle_paths(config)


class _UnownedExportRunner:
    """Record commands while presenting an existing unmarked export."""

    def __init__(self, export_dir):
        self.commands = []
        self.export_dir = str(export_dir)

    def run(self, arguments, **_kwargs):
        """Answer ownership probes without executing privileged commands."""
        command = [str(item) for item in arguments]
        self.commands.append(command)
        if "test" in command and "-L" in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        if "test" in command and "-e" in command:
            exists = command[-1] == self.export_dir
            return SimpleNamespace(returncode=0 if exists else 1, stdout="", stderr="")
        if "cat" in command:
            return SimpleNamespace(returncode=1, stdout="", stderr="missing")
        raise AssertionError(f"unexpected mutation command: {command}")


def test_existing_export_requires_marker_before_mutation(tmp_path):
    """An unmarked export is rejected before install or chown runs."""
    config = _config(tmp_path / "state", tmp_path / "export")
    runner = _UnownedExportRunner(config.export_dir)

    with pytest.raises(_DRIVER.ProvisionError, match="unowned export directory"):
        _ensure_export_marker(runner, config)

    assert not any(
        "install" in command or "chown" in command for command in runner.commands
    )


def test_export_ownership_journal_precedes_directory_mutation(tmp_path, monkeypatch):
    """Export creation journals ownership before install or chown can run."""
    config = _config(tmp_path / "state", tmp_path / "export")
    config.state_dir.mkdir()
    events = []

    class _NewExportRunner:
        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            if "test" in command and "-e" in command:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            if "test" in command and "-d" in command:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            events.append(command[1] if command[0] == "sudo" else command[0])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_DRIVER, "_read_system_file", lambda *_args: None)
    monkeypatch.setattr(
        _DRIVER,
        "_write_text",
        lambda path, _text, **_kwargs: events.append(f"journal:{path.name}"),
    )

    _ensure_export_marker(_NewExportRunner(), config)

    assert events.index("journal:export-marker.json") < events.index("install")
    assert events.index("journal:export-marker.json") < events.index("chown")


def test_system_file_reader_rejects_symlinked_ownership_file(tmp_path):
    """Fixed host ownership files cannot redirect reads through a symlink."""

    class _SymlinkRunner:
        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            if "test" in command and "-L" in command:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            pytest.fail(f"symlink probe did not stop system-file read: {command}")

    with pytest.raises(_DRIVER.ProvisionError, match="symlinked system path"):
        _DRIVER._read_system_file(_SymlinkRunner(), tmp_path / "owner.json")


def test_host_nfs_claim_rejects_unowned_fixed_configuration(tmp_path, monkeypatch):
    """Setup cannot replace administrator-owned files at fixed NFS paths."""
    config = _config(tmp_path / "state", tmp_path / "export")
    config.manifests_dir.mkdir(parents=True)
    monkeypatch.setattr(
        _DRIVER,
        "_read_system_file",
        lambda _runner, path: (
            "admin configuration\n" if path == _DRIVER.NFS_EXPORT_CONFIG else None
        ),
    )

    with pytest.raises(_DRIVER.ProvisionError, match="unowned host NFS"):
        _claim_nfs_host_owner(object(), config)


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
    monkeypatch.setattr(_DRIVER, "_ssh_pods", lambda *_args, **_kwargs: pods)

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

    def pod_exec(_runner, _config, pod, script, *, check=True, timeout=60):
        nonlocal storage_attempts
        commands.append((pod, script, check, timeout))
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
    assert all("unique-token" in script for _pod, script, _check, _timeout in commands)
    cleanup = [
        script
        for _pod, script, _check, _timeout in commands
        if script.startswith("rm -f")
    ]
    assert len(cleanup) == 2


def test_ssh_storage_visibility_bounds_fractional_remaining_time(tmp_path, monkeypatch):
    """The second probe cannot begin after a fractional overall deadline."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    pods = [_ready_pod("ssh-worker-0", "sshd"), _ready_pod("ssh-worker-1", "sshd")]
    clock = _DeadlineClock()
    probe_timeouts = []

    def pod_exec(_runner, _config, _pod, script, *, check=True, timeout=60):
        del check
        if script.startswith("test -e ") or script.startswith('test "$(cat '):
            assert clock.now + timeout <= 0.5
            probe_timeouts.append(timeout)
            clock.now += timeout
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_DRIVER, "_pod_exec", pod_exec)
    monkeypatch.setattr(_DRIVER, "_select_storage_backend", lambda _config: "nfs")
    monkeypatch.setattr(_DRIVER.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(_DRIVER, "SSH_STORAGE_VISIBILITY_TIMEOUT_SECONDS", 0.5)

    with pytest.raises(_DRIVER.ProvisionError, match="did not converge"):
        _validate_ssh_storage(None, config, pods, "separate")

    assert probe_timeouts == [0.5]
    assert clock.now == 0.5


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
        _DRIVER.NFS_HOST_OWNER: json.dumps(
            _nfs_host_owner_document(config), sort_keys=True
        )
        + "\n",
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


def test_teardown_recovers_empty_export_after_marker_install_crash(
    tmp_path, monkeypatch
):
    """A pre-marker export crash remains attributable and removable."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    config.state_dir.mkdir()
    config.export_dir.mkdir()
    owner = json.dumps(
        {"schema": _DRIVER.STATE_SCHEMA, "cluster_name": config.cluster_name}
    )
    (config.state_dir / _DRIVER.STATE_MARKER).write_text(owner, encoding="utf-8")
    (config.state_dir / "cluster-owner.json").write_text(owner, encoding="utf-8")
    (config.state_dir / "export-marker.json").write_text(owner, encoding="utf-8")
    monkeypatch.setattr(_DRIVER, "_read_system_file", lambda *_args: None)
    monkeypatch.setattr(_DRIVER, "_export_mount_type", lambda *_args: "")
    monkeypatch.setattr(_DRIVER, "_validate_loop_associations", lambda *_args: None)
    monkeypatch.setattr(_DRIVER, "_validate_nfs_host_owner", lambda *_args: True)

    ownership = _validate_teardown_ownership(_RecordingRunner(), config)

    assert ownership == (True, True, True, True)


@pytest.mark.parametrize("marker_name", (_DRIVER.STATE_MARKER, "cluster-owner.json"))
def test_nfs_teardown_rejects_directory_ownership_marker(tmp_path, marker_name):
    """A directory cannot impersonate either durable NFS owner marker."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    config.state_dir.mkdir()
    (config.state_dir / marker_name).mkdir()

    with pytest.raises(_DRIVER.ProvisionError, match="unsafe ownership marker"):
        _validate_teardown_ownership(_RecordingRunner(), config)


def test_nfs_teardown_rejects_directory_export_journal(tmp_path, monkeypatch):
    """A directory cannot impersonate the interrupted-export journal."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    _bootstrap_state_dir(config)
    (config.state_dir / "export-marker.json").mkdir()
    monkeypatch.setattr(_DRIVER, "_read_system_file", lambda *_args: None)

    with pytest.raises(_DRIVER.ProvisionError, match="unsafe export journal"):
        _validate_teardown_ownership(_RecordingRunner(), config)


def test_teardown_accepts_service_only_partial_nfs_bootstrap(tmp_path, monkeypatch):
    """An early setup failure remains recoverable before cluster ownership."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    _bootstrap_state_dir(config)
    (config.state_dir / "nfs-service.json").write_text(
        json.dumps(
            {
                "schema": _DRIVER.NFS_SERVICE_STATE_SCHEMA,
                "service_existed": True,
                "was_active": True,
                "was_enabled": False,
                "previous_threads": 2,
            }
        ),
        encoding="utf-8",
    )
    host_owner = json.dumps(_nfs_host_owner_document(config))
    monkeypatch.setattr(
        _DRIVER,
        "_read_system_file",
        lambda _runner, path: host_owner if path == _DRIVER.NFS_HOST_OWNER else None,
    )
    monkeypatch.setattr(_DRIVER, "_export_mount_type", lambda *_args: "")
    monkeypatch.setattr(_DRIVER, "_validate_loop_associations", lambda *_args: None)

    class _PartialRunner:
        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            if "test" in command and "-d" in command:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert _validate_teardown_ownership(_PartialRunner(), config) == (
        True,
        False,
        False,
        True,
    )


def test_teardown_ignores_preexisting_unowned_fixed_nfs_files(tmp_path, monkeypatch):
    """A rejected setup can remove local state without touching admin NFS files."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    _bootstrap_state_dir(config)
    monkeypatch.setattr(
        _DRIVER,
        "_read_system_file",
        lambda _runner, path: (
            "admin configuration\n" if path == _DRIVER.NFS_EXPORT_CONFIG else None
        ),
    )
    monkeypatch.setattr(_DRIVER, "_export_mount_type", lambda *_args: "")
    monkeypatch.setattr(_DRIVER, "_validate_loop_associations", lambda *_args: None)

    class _ExternalConfigRunner:
        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            if "test" in command and "-d" in command:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    assert _validate_teardown_ownership(_ExternalConfigRunner(), config) == (
        True,
        False,
        False,
        False,
    )


def test_service_only_partial_bootstrap_tears_down_twice_and_restores_host(
    tmp_path, monkeypatch
):
    """Early NFS mutation is fully reversible without a cluster owner marker."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    _bootstrap_state_dir(config)
    (config.state_dir / "nfs-service.json").write_text(
        json.dumps(
            {
                "schema": _DRIVER.NFS_SERVICE_STATE_SCHEMA,
                "service_existed": True,
                "was_active": True,
                "was_enabled": False,
                "previous_threads": 2,
            }
        ),
        encoding="utf-8",
    )

    class _EarlyFailureRunner:
        def __init__(self):
            self.host_owner = True
            self.active = True
            self.enabled = False
            self.threads = 8
            self.exports = ["/srv/unrelated-export"]

        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            if "test" in command and "-d" in command:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            if "is-active" in command:
                return SimpleNamespace(
                    returncode=0 if self.active else 3, stdout="", stderr=""
                )
            if "is-enabled" in command:
                return SimpleNamespace(
                    returncode=0 if self.enabled else 1, stdout="", stderr=""
                )
            if "--property=LoadState" in command:
                return SimpleNamespace(returncode=0, stdout="loaded\n", stderr="")
            if str(_DRIVER.NFS_THREADS_PATH) in command:
                return SimpleNamespace(
                    returncode=0, stdout=f"{self.threads}\n", stderr=""
                )
            if any(Path(item).name == "rpc.nfsd" for item in command):
                self.threads = int(command[-1])
            if "systemctl" in command and "enable" in command:
                self.enabled = True
            if "systemctl" in command and "disable" in command:
                self.enabled = False
            if command[-1] == str(_DRIVER.NFS_HOST_OWNER) and "rm" in command:
                self.host_owner = False
            stdout = ""
            if (
                any(Path(item).name == "exportfs" for item in command)
                and "-v" in command
            ):
                stdout = "/srv/unrelated-export 10.0.0.0/24(options)\n"
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    runner = _EarlyFailureRunner()
    owner = json.dumps(_nfs_host_owner_document(config)) + "\n"

    def read_system(_runner, path):
        if path == _DRIVER.NFS_HOST_OWNER and runner.host_owner:
            return owner
        return None

    def remove_owned(_runner, _config_value, *, remove_state, remove_export):
        assert not remove_export
        if remove_state and config.state_dir.exists():
            shutil.rmtree(config.state_dir)

    monkeypatch.setattr(_DRIVER, "_read_system_file", read_system)
    monkeypatch.setattr(_DRIVER, "_export_mount_type", lambda *_args: "")
    monkeypatch.setattr(_DRIVER, "_export_paths", lambda _runner: runner.exports)
    monkeypatch.setattr(_DRIVER, "_remove_owned_directories", remove_owned)
    monkeypatch.setattr(_DRIVER.shutil, "which", lambda command: f"/usr/sbin/{command}")

    _teardown_environment_locked(runner, config, "nfs")
    _teardown_environment_locked(runner, config, "nfs")

    assert runner.active
    assert not runner.enabled
    assert runner.threads == 2
    assert runner.exports == ["/srv/unrelated-export"]
    assert not runner.host_owner
    assert not config.state_dir.exists()


class _NfsRemovalRunner:
    """Record NFS cleanup while reporting one unrelated active export."""

    def __init__(self, threads=None):
        self.commands = []
        self.threads = threads

    def run(self, arguments, **_kwargs):
        """Record one cleanup command and return stable export state."""
        command = [str(item) for item in arguments]
        self.commands.append(command)
        if "is-active" in command:
            return SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        if str(_DRIVER.NFS_THREADS_PATH) in command:
            return SimpleNamespace(returncode=0, stdout=f"{self.threads}\n", stderr="")
        if any(Path(item).name == "rpc.nfsd" for item in command):
            self.threads = int(command[-1])
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

    assert not any(
        "systemctl" in command
        and any(action in command for action in ("enable", "disable", "stop"))
        for command in runner.commands
    )
    assert any(
        any(Path(item).name == "exportfs" for item in command) and "-u" in command
        for command in runner.commands
    )


def test_nfs_cleanup_restores_preexisting_worker_count(tmp_path, monkeypatch):
    """Teardown restores the live count after removing fixture configuration."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    config.manifests_dir.mkdir(parents=True)
    (config.state_dir / "nfs-service.json").write_text(
        json.dumps({"started_by_harness": False, "previous_threads": 2}),
        encoding="utf-8",
    )
    runner = _NfsRemovalRunner(threads=8)
    monkeypatch.setattr(
        _DRIVER.shutil,
        "which",
        lambda command: f"/usr/sbin/{command}",
    )

    _remove_nfs_configuration(runner, config)

    assert runner.threads == 2
    assert any(
        any(Path(item).name == "rpc.nfsd" for item in command) and command[-1] == "2"
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


class _DockerTagRunner:
    """Model only the Docker tag operations used by the ownership journal."""

    def __init__(self, tags=None):
        self.tags = dict(tags or {})
        self.commands = []

    def run(self, arguments, **_kwargs):
        command = [str(item) for item in arguments]
        self.commands.append(command)
        if command[:5] == [
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
        ]:
            image = command[-1]
            image_id = self.tags.get(image)
            return SimpleNamespace(
                returncode=0 if image_id else 1,
                stdout=f"{image_id}\n" if image_id else "",
                stderr="" if image_id else f"Error: No such image: {image}\n",
            )
        if command[:3] == ["docker", "image", "tag"]:
            self.tags[command[-1]] = self.tags[command[-2]]
        elif command[:3] == ["docker", "image", "rm"]:
            self.tags.pop(command[-1], None)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_docker_image_publish_recovers_interrupted_fixed_tag_update(tmp_path):
    """A crash after publishing remains owned and recoverable on retry."""
    config = _config(tmp_path / "state", tmp_path / "export")
    config.state_dir.mkdir()
    image = "fixture:test"
    runner = _DockerTagRunner({image: "sha256:original"})
    temporary = _begin_image_build(runner, config, image)
    runner.tags[temporary] = "sha256:built"
    state = _image_ownership_state(config)
    state[image]["pending_id"] = "sha256:built"
    _write_image_ownership_state(config, state)
    runner.tags[image] = "sha256:built"

    replacement = _begin_image_build(runner, config, image)

    ownership = _image_ownership_state(config)[image]
    assert ownership["built_id"] == "sha256:built"
    assert ownership["pending_tag"] == replacement
    assert temporary not in runner.tags


def test_docker_image_build_rejects_external_fixed_tag_change(tmp_path):
    """Repeated setup never silently overwrites a fixture tag retagged by a user."""
    config = _config(tmp_path / "state", tmp_path / "export")
    config.state_dir.mkdir()
    image = "fixture:test"
    runner = _DockerTagRunner({image: "sha256:original"})
    temporary = _begin_image_build(runner, config, image)
    runner.tags.pop(temporary, None)
    state = _image_ownership_state(config)
    state[image]["pending_tag"] = None
    _write_image_ownership_state(config, state)
    runner.tags[image] = "sha256:external"

    with pytest.raises(_DRIVER.ProvisionError, match="outside the fixture"):
        _begin_image_build(runner, config, image)


def test_docker_image_absence_is_distinct_from_daemon_failure():
    """Ownership decisions fail closed on Docker transport errors."""
    absent = SimpleNamespace(
        run=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="Error: No such image: fixture:test"
        )
    )
    broken = SimpleNamespace(
        run=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Cannot connect to the Docker daemon",
        )
    )

    assert _image_id(absent, "fixture:test") is None
    with pytest.raises(_DRIVER.ProvisionError, match="could not inspect"):
        _image_id(broken, "fixture:test")
    with pytest.raises(_DRIVER.ProvisionError, match="could not inspect"):
        _inspect_pinned_image(broken, "fixture@test", "sha256:test", "amd64")


def test_pinned_image_requires_matching_platform_and_index_digest():
    """A cached image for another architecture is not accepted offline."""
    metadata = {
        "Architecture": "arm64",
        "RepoDigests": ["registry.example/fixture@sha256:index"],
    }
    runner = SimpleNamespace(
        run=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=json.dumps(metadata), stderr=""
        )
    )

    assert not _inspect_pinned_image(
        runner, "registry.example/fixture@sha256:index", "sha256:index", "amd64"
    )
    assert not _inspect_pinned_image(
        runner, "registry.example/fixture@sha256:other", "sha256:other", "arm64"
    )


def test_pinned_fixture_base_uses_cache_before_network(monkeypatch):
    """A valid digest-qualified base is pulled only when genuinely absent."""
    reference = "registry.example/fixture:1@sha256:index"

    class _PinnedRunner:
        def __init__(self, present):
            self.present = present
            self.commands = []

        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            self.commands.append(command)
            if command[:5] == [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
            ]:
                if not self.present:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="Error: No such image",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "Architecture": "amd64",
                            "RepoDigests": ["registry.example/fixture@sha256:index"],
                        }
                    ),
                    stderr="",
                )
            if command[:2] == ["docker", "pull"]:
                self.present = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(_DRIVER, "_check_platform", lambda: "amd64")
    cached = _PinnedRunner(True)
    absent = _PinnedRunner(False)

    _ensure_pinned_image(cached, reference)
    _ensure_pinned_image(absent, reference)

    assert not any(command[:2] == ["docker", "pull"] for command in cached.commands)
    assert sum(command[:2] == ["docker", "pull"] for command in absent.commands) == 1


@pytest.mark.parametrize(
    "detail",
    (
        "429 Too Many Requests",
        "toomanyrequests: unauthenticated pull rate limit",
        "request timed out",
        "context deadline exceeded",
        "connection reset by peer",
        "unexpected status from registry: 503 Service Unavailable",
    ),
)
def test_transient_image_pull_failures_are_retryable(detail):
    """Only temporary registry and transport failures enter the retry loop."""
    assert _transient_image_pull_failure(detail)


def test_pinned_image_pull_retries_rate_limit_then_verifies(monkeypatch):
    """Host Docker retries a temporary quota response before accepting an image."""
    reference = "docker.io/library/fixture:1@sha256:index"

    class _RetryRunner:
        def __init__(self):
            self.present = False
            self.pull_attempts = 0

        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            if command[:5] == [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
            ]:
                if not self.present:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="Error: No such image",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "Architecture": "amd64",
                            "RepoDigests": ["fixture@sha256:index"],
                        }
                    ),
                    stderr="",
                )
            if command[:2] == ["docker", "pull"]:
                self.pull_attempts += 1
                if self.pull_attempts < 3:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="429 Too Many Requests",
                    )
                self.present = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    sleeps = []
    runner = _RetryRunner()
    monkeypatch.setattr(_DRIVER, "_check_platform", lambda: "amd64")
    monkeypatch.setattr(_DRIVER.secrets, "randbelow", lambda _limit: 0)
    monkeypatch.setattr(_DRIVER.time, "sleep", sleeps.append)

    assert _ensure_pinned_image(runner, reference) == reference
    assert runner.pull_attempts == 3
    assert sleeps == [10.0, 20.0]


def test_pinned_image_pull_retries_command_timeout(monkeypatch):
    """A bounded host-Docker timeout receives the same transient retry."""
    reference = "docker.io/library/fixture:1@sha256:index"

    class _TimeoutRunner:
        def __init__(self):
            self.present = False
            self.pull_attempts = 0

        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            if command[:5] == [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
            ]:
                if not self.present:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="Error: No such image",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "Architecture": "amd64",
                            "RepoDigests": ["fixture@sha256:index"],
                        }
                    ),
                    stderr="",
                )
            self.pull_attempts += 1
            if self.pull_attempts == 1:
                raise _DRIVER.subprocess.TimeoutExpired(
                    command, _DRIVER.IMAGE_PULL_TIMEOUT_SECONDS
                )
            self.present = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = _TimeoutRunner()
    monkeypatch.setattr(_DRIVER, "_check_platform", lambda: "amd64")
    monkeypatch.setattr(_DRIVER.secrets, "randbelow", lambda _limit: 0)
    monkeypatch.setattr(_DRIVER.time, "sleep", lambda _seconds: None)

    assert _ensure_pinned_image(runner, reference) == reference
    assert runner.pull_attempts == 2


def test_pinned_image_pull_reports_final_registry_failure(monkeypatch):
    """Exhausted retries retain the final actionable registry diagnostic."""
    reference = "docker.io/library/fixture:1@sha256:index"

    class _LimitedRunner:
        def __init__(self):
            self.pull_attempts = 0

        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            if command[:5] == [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
            ]:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="Error: No such image",
                )
            self.pull_attempts += 1
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="429 Too Many Requests: quota exhausted",
            )

    runner = _LimitedRunner()
    monkeypatch.setattr(_DRIVER, "_check_platform", lambda: "amd64")
    monkeypatch.setattr(_DRIVER.secrets, "randbelow", lambda _limit: 0)
    monkeypatch.setattr(_DRIVER.time, "sleep", lambda _seconds: None)

    with pytest.raises(
        _DRIVER.ProvisionError, match="429 Too Many Requests: quota exhausted"
    ):
        _ensure_pinned_image(runner, reference)

    assert runner.pull_attempts == _DRIVER.IMAGE_PULL_ATTEMPTS


def test_pinned_image_pull_respects_overall_deadline(monkeypatch):
    """Slow retries cannot exceed the shared image-acquisition deadline."""
    reference = "docker.io/library/fixture:1@sha256:index"

    class _Clock:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    class _ImageDeadlineRunner:
        def __init__(self, clock):
            self.clock = clock
            self.pull_attempts = 0

        def run(self, arguments, *, timeout=None, **_kwargs):
            command = [str(item) for item in arguments]
            if command[:5] == [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
            ]:
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="Error: No such image",
                )
            self.pull_attempts += 1
            assert timeout is not None
            assert self.clock.now + timeout <= _DRIVER.IMAGE_PULL_DEADLINE_SECONDS
            self.clock.now += timeout
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="429 Too Many Requests",
            )

    clock = _Clock()
    runner = _ImageDeadlineRunner(clock)
    monkeypatch.setattr(_DRIVER, "_check_platform", lambda: "amd64")
    monkeypatch.setattr(_DRIVER.secrets, "randbelow", lambda _limit: 0)
    monkeypatch.setattr(_DRIVER.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(_DRIVER.time, "sleep", clock.sleep)

    with pytest.raises(_DRIVER.ProvisionError, match="acquisition deadline"):
        _ensure_pinned_image(runner, reference)

    assert runner.pull_attempts == 3
    assert clock.now < _DRIVER.IMAGE_PULL_DEADLINE_SECONDS


def test_pinned_image_pull_reserves_fallback_attempt(monkeypatch):
    """A hanging primary leaves one full pull and inspect window per fallback."""
    digest = "sha256:index"
    primary = f"registry.example/fixture@{digest}"
    fallback = f"mirror.example/fixture@{digest}"

    class _Clock:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    class _FallbackRunner:
        def __init__(self, clock):
            self.clock = clock
            self.present = set()
            self.fallback_timeouts = []

        def run(self, arguments, *, timeout=None, **_kwargs):
            command = [str(item) for item in arguments]
            reference = command[-1]
            if command[:5] == [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
            ]:
                if reference not in self.present:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="Error: No such image",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "Architecture": "amd64",
                            "RepoDigests": [f"fixture@{digest}"],
                        }
                    ),
                    stderr="",
                )
            assert command[:2] == ["docker", "pull"]
            assert timeout is not None
            if reference == primary:
                self.clock.now += timeout
                raise _DRIVER.subprocess.TimeoutExpired(command, timeout)
            self.fallback_timeouts.append(timeout)
            self.present.add(reference)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    clock = _Clock()
    runner = _FallbackRunner(clock)
    monkeypatch.setattr(_DRIVER, "_check_platform", lambda: "amd64")
    monkeypatch.setattr(_DRIVER.secrets, "randbelow", lambda _limit: 0)
    monkeypatch.setattr(_DRIVER.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(_DRIVER.time, "sleep", clock.sleep)

    selected = _acquire_pinned_image(runner, (primary, fallback), digest)

    assert selected == fallback
    assert runner.fallback_timeouts == [float(_DRIVER.IMAGE_PULL_TIMEOUT_SECONDS)]
    assert clock.now < _DRIVER.IMAGE_PULL_DEADLINE_SECONDS


@pytest.mark.parametrize(
    ("backend", "expected"),
    (
        ("nfs", _DRIVER.KIND_NODE_IMAGE),
        ("sbx-shared", _DRIVER.SBX_KIND_NODE_IMAGE),
    ),
)
def test_cluster_creation_preacquires_pinned_node_image(
    tmp_path, monkeypatch, backend, expected
):
    """kind cannot bypass verified host-Docker image acquisition."""
    config = _config(tmp_path / "state", tmp_path / "export")
    events = []

    class _KindRunner:
        def run(self, arguments, **_kwargs):
            events.append(("run", [str(item) for item in arguments]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        _DRIVER,
        "_render_kind_config",
        lambda _config, _backend: tmp_path / "kind.yaml",
    )
    monkeypatch.setattr(
        _DRIVER,
        "_ensure_pinned_image",
        lambda _runner, image: events.append(("acquire", image)),
    )

    _create_cluster(_KindRunner(), config, backend)

    assert events[0] == ("acquire", expected)
    assert events[1][0] == "run"
    assert events[1][1][:3] == ["kind", "create", "cluster"]
    assert events[1][1][events[1][1].index("--image") + 1] == expected


def test_fixture_build_does_not_force_registry_refresh(tmp_path, monkeypatch):
    """Digest-qualified cached bases are not defeated by docker build --pull."""
    runner = _RecordingRunner()
    config = _config(tmp_path / "state", tmp_path / "export")
    base = "registry.example/base:1@sha256:index"
    checked = []
    monkeypatch.setattr(
        _DRIVER, "_ensure_pinned_image", lambda _runner, image: checked.append(image)
    )
    monkeypatch.setattr(_DRIVER, "_begin_image_build", lambda *_args: "fixture:pending")
    monkeypatch.setattr(_DRIVER, "_publish_image_build", lambda *_args: None)

    _build_owned_image(
        runner,
        config,
        "fixture:latest",
        tmp_path / "Dockerfile",
        base,
    )

    build = next(
        command for command in runner.commands if command[:2] == ["docker", "build"]
    )
    assert checked == [base]
    assert "--pull" not in build


def test_slurm_install_preloads_verified_mariadb_and_helper_images(
    tmp_path, monkeypatch
):
    """Slurm workload images use host Docker and the deployed private tag."""
    config = _config(tmp_path / "state", tmp_path / "export")
    _bootstrap_state_dir(config)

    class _WorkloadImageRunner:
        def __init__(self):
            self.commands = []
            self.present = set()

        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            self.commands.append(command)
            if command[:5] == [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
            ]:
                reference = command[-1]
                if reference not in self.present:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="Error: No such image",
                    )
                digest = reference.rpartition("@")[2]
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "Architecture": "amd64",
                            "RepoDigests": [f"fixture@{digest}"],
                        }
                    ),
                    stderr="",
                )
            if command[:2] == ["docker", "pull"]:
                self.present.add(command[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = _WorkloadImageRunner()
    published = []
    loaded = []
    monkeypatch.setattr(_DRIVER, "_check_platform", lambda: "amd64")
    monkeypatch.setattr(
        _DRIVER,
        "_begin_image_build",
        lambda _runner, _config, image: f"{image}-pending",
    )
    monkeypatch.setattr(
        _DRIVER,
        "_publish_image_build",
        lambda _runner, _config, image, temporary: published.append((image, temporary)),
    )
    monkeypatch.setattr(
        _DRIVER,
        "_load_image_into_nodes",
        lambda _runner, image, nodes: loaded.append((image, nodes)),
    )
    monkeypatch.setattr(_DRIVER, "_build_slinky_image", lambda *_args, **_kwargs: None)
    for name in (
        "_helm_slinky",
        "_wait_for_slurm",
        "_restart_slinky_login",
        "_ensure_slurm_workload_account",
        "_validate_slurm",
    ):
        monkeypatch.setattr(_DRIVER, name, lambda *_args, **_kwargs: None)

    _install_slurm(runner, config)

    pulls = {
        command[-1] for command in runner.commands if command[:2] == ["docker", "pull"]
    }
    assert pulls == {
        _DRIVER.MARIADB_BASE_IMAGE,
        _DRIVER.SLINKY_HELPER_BASE_IMAGE,
    }
    control_plane = f"{config.cluster_name}-control-plane"
    assert (_DRIVER.MARIADB_IMAGE, [control_plane]) in loaded
    assert (
        _DRIVER.SLINKY_HELPER_IMAGE,
        [
            control_plane,
            f"{config.cluster_name}-worker",
            f"{config.cluster_name}-worker2",
        ],
    ) in loaded
    assert {image for image, _temporary in published} == {
        _DRIVER.MARIADB_IMAGE,
        _DRIVER.SLINKY_HELPER_IMAGE,
    }
    apply = next(command for command in runner.commands if "apply" in command)
    manifest = Path(apply[-1]).read_text(encoding="utf-8")
    assert f"image: {_DRIVER.MARIADB_IMAGE}" in manifest
    assert "imagePullPolicy: Never" in manifest
    assert _DRIVER.MARIADB_BASE_IMAGE not in manifest


def test_kubectl_probe_image_is_verified_and_imported_privately(tmp_path, monkeypatch):
    """The Elbencho probe uses a verified index and only a node-local alias."""
    config = _config(tmp_path / "state", tmp_path / "export")
    commands = []
    loaded = []

    class _ProbeImageRunner:
        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            commands.append(command)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        _DRIVER,
        "_kind_containers",
        lambda *_args, **_kwargs: ["control", "worker", "worker2"],
    )
    monkeypatch.setattr(
        _DRIVER,
        "_ensure_pinned_image",
        lambda _runner, image: (
            image
            if image == _DRIVER.ELBENCHO_FIXTURE.upstream
            else pytest.fail(f"unexpected image {image}")
        ),
    )
    monkeypatch.setattr(
        _DRIVER,
        "_load_image_into_nodes",
        lambda _runner, image, nodes, *, destination=None: loaded.append(
            (image, nodes, destination)
        ),
    )

    _prepare_kubectl_prerequisite_image(_ProbeImageRunner(), config)

    tag = next(
        command for command in commands if command[:3] == ["docker", "image", "tag"]
    )
    temporary = tag[-1]
    assert tag[-2] == _DRIVER.ELBENCHO_FIXTURE.upstream
    assert loaded == [
        (
            temporary,
            ["control", "worker", "worker2"],
            _DRIVER.ELBENCHO_FIXTURE_IMAGE,
        )
    ]
    assert ["docker", "image", "rm", temporary] in commands


def test_kubectl_prerequisite_manifest_matches_product_constraints():
    """The real-cluster probe uses Pod IPs, non-root identity, RWX, and policy."""
    rendered = _render_resource_text(
        "manifests/kubectl-prerequisite-probe.yaml.tmpl",
        {
            "NAMESPACE": "fixture",
            "ELBENCHO_IMAGE": _DRIVER.ELBENCHO_FIXTURE_IMAGE,
            "PROBE_TOKEN": "0123456789abcdef",
        },
    )
    documents = list(yaml.safe_load_all(rendered))
    policy_rendered = _render_resource_text(
        "manifests/kubectl-prerequisite-policy.yaml.tmpl",
        {"NAMESPACE": "fixture"},
    )
    policies = list(yaml.safe_load_all(policy_rendered))
    daemonset, coordinator, denied = documents
    ingress, egress = policies
    pod_specs = [
        daemonset["spec"]["template"]["spec"],
        coordinator["spec"],
        denied["spec"],
    ]

    assert [document["kind"] for document in documents] == [
        "DaemonSet",
        "Pod",
        "Pod",
    ]
    assert [document["kind"] for document in policies] == [
        "NetworkPolicy",
        "NetworkPolicy",
    ]
    assert daemonset["spec"]["template"]["spec"]["nodeSelector"] == {
        "storage-scale-test/target": "true"
    }
    assert coordinator["spec"]["nodeSelector"] == {"storage-scale-test/login": "true"}
    assert denied["spec"]["nodeSelector"] == {"storage-scale-test/login": "true"}
    for pod_spec in pod_specs:
        assert pod_spec["automountServiceAccountToken"] is False
        assert pod_spec["securityContext"]["runAsNonRoot"] is True
        assert pod_spec["securityContext"]["runAsUser"] == 2000
        assert pod_spec["securityContext"]["runAsGroup"] == 2000
        assert pod_spec["securityContext"]["seccompProfile"] == {
            "type": "RuntimeDefault"
        }
        assert "hostNetwork" not in pod_spec
        for container in pod_spec["containers"]:
            assert container["image"] == _DRIVER.ELBENCHO_FIXTURE_IMAGE
            assert container["imagePullPolicy"] == "Never"
            assert container["securityContext"]["allowPrivilegeEscalation"] is False
            assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
            assert "hostPort" not in str(container)
    for client in (coordinator, denied):
        client_script = client["spec"]["containers"][0]["args"][0]
        assert "command -v timeout >/dev/null" in client_script
        assert "perl -MIO::Socket::INET -e 1" in client_script
        assert "probe_root=/tmp/storage-scale-kubectl-probe" in client_script
        assert "timeout --kill-after=1s 5s" in client_script
        assert "use IO::Socket::INET" in client_script
        assert "PeerPort=>1611" in client_script
        assert r"\AHTTP\/[0-9.]+ 200" in client_script
        assert "=~ /200/" not in client_script
        assert client["spec"]["containers"][0]["readinessProbe"]["exec"]
    worker = daemonset["spec"]["template"]["spec"]["containers"][0]
    worker_script = worker["args"][0]
    assert (
        "/mnt/storage-scale-test/.kubectl-prerequisite-probe/"
        "0123456789abcdef/$NODE_NAME" in worker_script
    )
    assert "'0123456789abcdef' \"$NODE_NAME\"" in worker_script
    assert worker["ports"] == [
        {"name": "status", "containerPort": 1611, "protocol": "TCP"}
    ]
    readiness_script = worker["readinessProbe"]["exec"]["command"][-1]
    assert "read -r -t 2 response" in readiness_script
    assert "cat <&3" not in readiness_script
    assert daemonset["spec"]["template"]["spec"]["volumes"] == [
        {
            "name": "storage",
            "persistentVolumeClaim": {"claimName": "storage-test-rwx"},
        }
    ]
    assert (
        ingress["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"]
        == "worker"
    )
    assert (
        ingress["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"][
            "app.kubernetes.io/component"
        ]
        == "coordinator"
    )
    assert ingress["spec"]["ingress"][0]["ports"][0]["port"] == 1611
    assert (
        egress["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/component"]
        == "coordinator"
    )
    assert (
        egress["spec"]["egress"][0]["to"][0]["podSelector"]["matchLabels"][
            "app.kubernetes.io/component"
        ]
        == "worker"
    )


def test_kubectl_probe_status_detaches_network_io_from_exec(monkeypatch, tmp_path):
    """Cross-Pod socket lifetime is not coupled to a kubectl exec stream."""
    # pylint: disable=protected-access
    config = _config(tmp_path / "state", tmp_path / "export")
    calls = []
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "0\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )

    def _probe_exec(_runner, _config_value, pod, container, arguments, **kwargs):
        calls.append((pod, container, tuple(arguments), kwargs))
        return next(responses)

    monkeypatch.setattr(
        _DRIVER.secrets, "token_hex", lambda _length: "0123456789abcdef"
    )
    monkeypatch.setattr(_DRIVER, "_kubectl_probe_exec", _probe_exec)

    result = _DRIVER._kubectl_probe_status(
        object(), config, "coordinator", "coordinator", "10.244.1.2"
    )

    assert result.returncode == 0
    assert len(calls) == 4
    launch = calls[0][2]
    assert launch[:2] == ("bash", "-ceu")
    assert "/dev/tcp" not in launch[2]
    assert launch[4:7] == (
        "/tmp/storage-scale-kubectl-probe",
        "0123456789abcdef",
        "10.244.1.2",
    )
    assert calls[-1][2] == (
        "rm",
        "-rf",
        "--",
        "/tmp/storage-scale-kubectl-probe/0123456789abcdef",
        "/tmp/storage-scale-kubectl-probe/0123456789abcdef.request",
        "/tmp/storage-scale-kubectl-probe/0123456789abcdef.request.tmp",
    )


def test_kubectl_probe_status_preserves_detached_failure(monkeypatch, tmp_path):
    """A denied detached request remains an ordinary nonzero probe result."""
    # pylint: disable=protected-access
    config = _config(tmp_path / "state", tmp_path / "export")
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "124\n", ""),
            subprocess.CompletedProcess([], 0, "timed out\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )
    monkeypatch.setattr(
        _DRIVER,
        "_kubectl_probe_exec",
        lambda *_args, **_kwargs: next(responses),
    )

    result = _DRIVER._kubectl_probe_status(
        object(),
        config,
        "denied",
        "denied",
        "10.244.2.2",
        check=False,
    )

    assert result.returncode == 124
    assert result.stderr == "timed out\n"


def test_kubectl_probe_status_clamps_every_exec_to_one_deadline(monkeypatch, tmp_path):
    """Trigger, polling, diagnostics, and cleanup share one time budget."""
    # pylint: disable=protected-access
    config = _config(tmp_path / "state", tmp_path / "export")
    clock = [100.0]
    calls = []
    responses = iter(
        [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "0\n", ""),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "", ""),
        ]
    )

    def _monotonic():
        return clock[0]

    def _probe_exec(*_args, timeout, **_kwargs):
        calls.append((clock[0], timeout))
        clock[0] += 0.2
        return next(responses)

    monkeypatch.setattr(_DRIVER.time, "monotonic", _monotonic)
    monkeypatch.setattr(_DRIVER, "_kubectl_probe_exec", _probe_exec)

    result = _DRIVER._kubectl_probe_status(
        object(),
        config,
        "coordinator",
        "coordinator",
        "10.244.1.2",
        deadline=101.0,
    )

    assert result.returncode == 0
    assert len(calls) == 4
    assert all(timeout <= 101.0 - started for started, timeout in calls)
    assert clock[0] < 101.0


def test_kubectl_probe_waits_pass_their_overall_deadline(monkeypatch, tmp_path):
    """Nested access and policy probes cannot reset the outer wait budget."""
    # pylint: disable=protected-access
    config = _config(tmp_path / "state", tmp_path / "export")
    clock = [200.0]
    deadlines = []
    results = iter((0, 1, 0))

    def _probe_status(*_args, deadline, **_kwargs):
        deadlines.append(deadline)
        clock[0] += 1
        return subprocess.CompletedProcess([], next(results), "", "")

    monkeypatch.setattr(_DRIVER.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(_DRIVER, "_kubectl_probe_status", _probe_status)

    _DRIVER._wait_for_kubectl_probe_denial(
        object(), config, "coordinator", "denied", "10.244.1.2"
    )

    assert deadlines == [230.0, 230.0, 230.0]


def _yaml_image_references(value):
    """Yield scalar or structured image references from nested fixture YAML."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "image" and isinstance(item, str):
                yield item
            elif key == "image" and isinstance(item, dict) and item.get("repository"):
                reference = str(item["repository"])
                if item.get("tag"):
                    reference += f":{item['tag']}"
                if item.get("digest"):
                    reference += f"@{item['digest']}"
                yield reference
            yield from _yaml_image_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _yaml_image_references(item)


def _is_docker_hub_reference(reference):
    """Return whether Docker resolves an image reference through Docker Hub."""
    first_component = reference.split("/", maxsplit=1)[0]
    if "/" not in reference:
        return True
    if first_component in {"docker.io", "index.docker.io", "registry-1.docker.io"}:
        return True
    return (
        "." not in first_component
        and ":" not in first_component
        and first_component != "localhost"
    )


def test_workload_image_contract_has_no_mutable_or_unstaged_dockerhub_images():
    """Checked-in workload values cannot restore direct mutable Hub pulls."""
    mariadb = _render_resource_text(
        "manifests/mariadb-accounting.yaml.tmpl",
        {
            "MARIADB_IMAGE": _DRIVER.MARIADB_IMAGE,
            "NAMESPACE": "fixture",
        },
    )
    slinky = _render_resource_text(
        "manifests/slinky-slurm-values.yaml.tmpl",
        {
            "SLINKY_HELPER_IMAGE_REPOSITORY": (_DRIVER.SLINKY_HELPER_IMAGE_REPOSITORY),
            "SLINKY_HELPER_IMAGE_TAG": _DRIVER.SLINKY_HELPER_IMAGE_TAG,
        },
    )
    ssh = _render_resource_text(
        "manifests/ssh-workers.yaml.tmpl",
        {
            "NAMESPACE": "fixture",
            "SSH_CONFIG_CHECKSUM": "checksum",
            "SSH_HOME_MODE": "separate",
            "SSH_HOME_VOLUME": "emptyDir: {}",
        },
    )
    slinky_values = yaml.safe_load(slinky)
    documents = [
        *yaml.safe_load_all(mariadb),
        slinky_values,
        *yaml.safe_load_all(ssh),
    ]
    references = {
        reference
        for document in documents
        for reference in _yaml_image_references(document)
    }
    preloaded = {
        _DRIVER.MARIADB_IMAGE,
        _DRIVER.SLINKY_HELPER_IMAGE,
        _DRIVER.SLINKY_LOGIN_IMAGE,
        _DRIVER.SLINKY_SLURMD_IMAGE,
        _DRIVER.SSH_IMAGE,
    }
    dockerhub = {
        reference for reference in references if _is_docker_hub_reference(reference)
    }

    assert _is_docker_hub_reference("bitnami/example:1")
    assert not _is_docker_hub_reference("registry.example.com/example:1")
    assert ":latest" not in "\n".join(references)
    assert dockerhub <= preloaded
    helper_containers = (
        slinky_values["controller"]["logfile"],
        slinky_values["loginsets"]["test"]["initconf"],
        slinky_values["nodesets"]["storage"]["logfile"],
    )
    assert all(
        container["image"]["repository"] == _DRIVER.SLINKY_HELPER_IMAGE_REPOSITORY
        and container["image"]["tag"] == _DRIVER.SLINKY_HELPER_IMAGE_TAG
        and container["image"]["digest"] is None
        and container["imagePullPolicy"] == "Never"
        for container in helper_containers
    )
    assert slinky_values["loginsets"]["test"]["login"]["imagePullPolicy"] == "Never"
    assert slinky_values["nodesets"]["storage"]["slurmd"]["imagePullPolicy"] == "Never"


def test_cached_csi_images_avoid_network_and_shared_host_tags(tmp_path, monkeypatch):
    """Verified cached indexes load through temporary fixture-only tags."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )

    class _CachedCsiRunner:
        def __init__(self):
            self.commands = []

        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            self.commands.append(command)
            if command[:5] == [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
            ]:
                reference = command[-1]
                image = next(
                    entry for entry in _DRIVER.CSI_IMAGES if entry[0] in reference
                )
                repository = reference.split(":", maxsplit=1)[0]
                document = {
                    "Architecture": "amd64",
                    "RepoDigests": [f"{repository}@{image[2]}"],
                }
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps(document), stderr=""
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = _CachedCsiRunner()
    loaded = []
    monkeypatch.setattr(_DRIVER, "_check_platform", lambda: "amd64")
    monkeypatch.setattr(
        _DRIVER,
        "_kind_containers",
        lambda *_args, **_kwargs: ["control", "worker", "worker2"],
    )
    monkeypatch.setattr(
        _DRIVER,
        "_load_image_into_nodes",
        lambda _runner, image, nodes, *, destination=None: loaded.append(
            (image, nodes, destination)
        ),
    )

    _prepare_csi_images(runner, config)

    assert len(loaded) == len(_DRIVER.CSI_IMAGES)
    assert not any(command[:2] == ["docker", "pull"] for command in runner.commands)
    tag_commands = [
        command
        for command in runner.commands
        if command[:3] == ["docker", "image", "tag"]
    ]
    assert all(
        "storage-scale-integration-csi/" in command[-1] for command in tag_commands
    )


def test_csi_pull_uses_verified_staging_fallback(tmp_path, monkeypatch):
    """A primary-registry outage falls back to the established staging mirror."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    image = ("nfsplugin", "v-test", "sha256:index")

    class _FallbackRunner:
        def __init__(self):
            self.commands = []
            self.fallback_present = False

        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            self.commands.append(command)
            if command[:5] == [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
            ]:
                reference = command[-1]
                if self.fallback_present and "k8s-staging-sig-storage" in reference:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "Architecture": "amd64",
                                "RepoDigests": [
                                    "gcr.io/k8s-staging-sig-storage/"
                                    "nfsplugin@sha256:index"
                                ],
                            }
                        ),
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="Error: No such image",
                )
            if command[:2] == ["docker", "pull"]:
                if "registry.k8s.io" in command[-1]:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="registry unavailable",
                    )
                self.fallback_present = True
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = _FallbackRunner()
    monkeypatch.setattr(_DRIVER, "CSI_IMAGES", (image,))
    monkeypatch.setattr(_DRIVER, "_check_platform", lambda: "amd64")
    monkeypatch.setattr(
        _DRIVER,
        "_kind_containers",
        lambda *_args, **_kwargs: ["control", "worker", "worker2"],
    )
    monkeypatch.setattr(
        _DRIVER, "_load_image_into_nodes", lambda *_args, **_kwargs: None
    )

    _prepare_csi_images(runner, config)

    pulls = [
        command[-1] for command in runner.commands if command[:2] == ["docker", "pull"]
    ]
    assert pulls == [
        "registry.k8s.io/sig-storage/nfsplugin@sha256:index",
        "gcr.io/k8s-staging-sig-storage/nfsplugin@sha256:index",
    ]
    assert all("k8s-artifacts-prod" not in item for item in pulls)


def test_csi_setup_reconciles_interrupted_temporary_aliases(tmp_path, monkeypatch):
    """Repeated setup removes fixture-private host and node aliases after SIGKILL."""
    config = replace(
        _config(tmp_path / "state", tmp_path / "export"), storage_backend="nfs"
    )
    image = ("nfsplugin", "v-test", "sha256:index")
    host_alias = "storage-scale-integration-csi/nfsplugin:v-old-stale"
    node_alias = f"docker.io/{host_alias}"

    class _StaleAliasRunner:
        def __init__(self):
            self.commands = []

        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            self.commands.append(command)
            if command[:5] == [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
            ]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "Architecture": "amd64",
                            "RepoDigests": [
                                "registry.k8s.io/sig-storage/nfsplugin@sha256:index"
                            ],
                        }
                    ),
                    stderr="",
                )
            if command[:4] == ["docker", "image", "ls", "--format"]:
                return SimpleNamespace(
                    returncode=0, stdout=f"{host_alias}\n", stderr=""
                )
            if command[-3:] == ["images", "list", "--quiet"]:
                return SimpleNamespace(
                    returncode=0, stdout=f"{node_alias}\n", stderr=""
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = _StaleAliasRunner()
    monkeypatch.setattr(_DRIVER, "CSI_IMAGES", (image,))
    monkeypatch.setattr(_DRIVER, "_check_platform", lambda: "amd64")
    monkeypatch.setattr(
        _DRIVER,
        "_kind_containers",
        lambda *_args, **_kwargs: ["control", "worker", "worker2"],
    )
    monkeypatch.setattr(
        _DRIVER, "_load_image_into_nodes", lambda *_args, **_kwargs: None
    )

    _prepare_csi_images(runner, config)

    assert ["docker", "image", "rm", host_alias] in runner.commands
    for node in ("control", "worker", "worker2"):
        assert [
            "docker",
            "exec",
            node,
            "ctr",
            "-n",
            "k8s.io",
            "images",
            "remove",
            node_alias,
        ] in runner.commands


def test_teardown_removes_interrupted_csi_host_alias(tmp_path, monkeypatch):
    """Teardown cleans a private CSI tag even when setup never reruns."""
    config = _config(tmp_path / "state", tmp_path / "export")
    alias = "storage-scale-integration-csi/nfsplugin:v-old-interrupted"

    class _InterruptedAliasRunner:
        def __init__(self):
            self.commands = []

        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            self.commands.append(command)
            if command[:4] == ["docker", "image", "ls", "--format"]:
                return SimpleNamespace(returncode=0, stdout=f"{alias}\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = _InterruptedAliasRunner()
    monkeypatch.setattr(
        _DRIVER, "CSI_IMAGES", (("nfsplugin", "v-test", "sha256:index"),)
    )

    _remove_harness_images(runner, config)

    assert ["docker", "image", "rm", alias] in runner.commands


def test_csi_alias_cleanup_does_not_mask_staging_failure(tmp_path, monkeypatch):
    """A secondary temporary-tag failure preserves the node-import error."""
    config = _config(tmp_path / "state", tmp_path / "export")

    class _CleanupFailureRunner:
        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            if command[:5] == [
                "docker",
                "image",
                "inspect",
                "--format",
                "{{json .}}",
            ]:
                return SimpleNamespace(
                    returncode=0,
                    stdout=json.dumps(
                        {
                            "Architecture": "amd64",
                            "RepoDigests": [
                                "registry.k8s.io/sig-storage/nfsplugin@sha256:index"
                            ],
                        }
                    ),
                    stderr="",
                )
            if command[:4] == ["docker", "image", "ls", "--format"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if command[-3:] == ["images", "list", "--quiet"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if command[:3] == ["docker", "image", "rm"]:
                raise _DRIVER.ProvisionError("alias cleanup failed")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        _DRIVER, "CSI_IMAGES", (("nfsplugin", "v-test", "sha256:index"),)
    )
    monkeypatch.setattr(_DRIVER, "_check_platform", lambda: "amd64")
    monkeypatch.setattr(
        _DRIVER,
        "_kind_containers",
        lambda *_args, **_kwargs: ["control", "worker", "worker2"],
    )

    def fail_node_import(*_args, **_kwargs):
        raise _DRIVER.ProvisionError("node import failed")

    monkeypatch.setattr(_DRIVER, "_load_image_into_nodes", fail_node_import)

    with pytest.raises(_DRIVER.ProvisionError, match="node import failed"):
        _prepare_csi_images(_CleanupFailureRunner(), config)


def test_csi_chart_tag_is_published_only_inside_kind_nodes():
    """The imported private tag is aliased in containerd, not host Docker."""

    class _ImageLoadRunner:
        def __init__(self):
            self.commands = []

        def run(self, arguments, **_kwargs):
            command = [str(item) for item in arguments]
            self.commands.append(command)
            if command[:3] == ["docker", "save", "--output"]:
                Path(command[3]).write_bytes(b"archive")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = _ImageLoadRunner()
    private = "storage-scale-integration-csi/nfsplugin:v4.13.4-42"
    destination = "registry.k8s.io/sig-storage/nfsplugin:v4.13.4"

    _load_image_into_nodes(runner, private, ["kind-worker"], destination=destination)

    tag = next(command for command in runner.commands if "tag" in command)
    assert tag[-2:] == [f"docker.io/{private}", destination]
    assert [
        "docker",
        "exec",
        "kind-worker",
        "ctr",
        "-n",
        "k8s.io",
        "images",
        "remove",
        f"docker.io/{private}",
    ] in runner.commands
    assert not any(
        command[:3] == ["docker", "image", "tag"] for command in runner.commands
    )


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
