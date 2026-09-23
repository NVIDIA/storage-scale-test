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

"""Run bounded filesystem integration tests against the provisioned fixture."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from deployment_cache import (
    DeploymentCacheError,
    DeploymentCacheRequest,
    get_or_build_deployment,
)
from failure_injection import (
    FailureInjectionOperations,
    build_slurm_failure_injection_plan,
    build_ssh_failure_injection_plan,
    staged_failure_injection,
)
from fixture_images import ELBENCHO_UPSTREAM_IMAGE
from fixture_images import ELBENCHO_FIXTURE_IMAGE
from fixture_capacity import (
    MAX_DEPLOYMENT_CONTENT_BYTES,
    MAX_LIVE_CAPTURE_DATASET_BYTES,
)
from filesystem_scenario_specs import (
    SCENARIO_SPECS_BY_NAME,
    CommandKind,
    DatasetExpectation,
    ExecutionStatus,
    FilesystemScenarioSpec,
    ScenarioStep,
    WorkloadPhase,
)
from scenario_planner import (
    ScenarioPlanningError,
    SshHomeTransition,
    WorkItem,
    plan_scenarios,
)

LOG = logging.getLogger("storage-scale-integration")

ELBENCHO_VERSION = "v3.1-11"
ELBENCHO_RELEASE_API = (
    "https://api.github.com/repos/breuner/elbencho/releases/tags/" + ELBENCHO_VERSION
)
ELBENCHO_CONTAINER = ELBENCHO_UPSTREAM_IMAGE
SBX_ELBENCHO_BUNDLE_RECIPE = 1
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_DEPLOYMENT_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_DEPLOYMENT_FILES = 10_000
SLURM_CLEANUP_MARGIN_SECONDS = 120
ELBENCHO_ARCHIVES = {
    "x86_64": (
        "elbencho-static-x86_64.tar.gz",
        "8d7cf885481dbd8f39908b7f4ff588d9e80cbc0fd26eeaba0b764c77587884d2",
        "elbencho",
    ),
    "aarch64": (
        "elbencho-static-aarch64.tar.gz",
        "a744c82ab4e15d8cf4023f7f5053c2008e148f77349e2ba53d2352c4f7508683",
        "elbencho.aarch64",
    ),
}
REMOTE_BASE = "/mnt/storage-test/integration-regression"
SSH_FAILURE_STAGING_BASE = "/tmp/storage-scale-test-integration-failure"
KUBECTL_UTILITY_PREFIX = "storage-scale-test-utility"
KUBECTL_TARGET_SELECTOR = "storage-scale-test/target=true"
KUBECTL_STORAGE_PVC = "storage-test-rwx"
KUBECTL_STORAGE_MOUNT = "/mnt/storage-scale-test"
KUBERNETES_DNS_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
KUBERNETES_UID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
KUBECTL_TERMINAL_STATES = frozenset({"SUCCESS", "FAILED", "CANCELLED"})
KUBECTL_STATUS_STATES = KUBECTL_TERMINAL_STATES | frozenset(
    {"PREPARED", "SUBMITTED", "RUNNING"}
)
KUBECTL_SUPPORTED_SCENARIOS = frozenset(
    {
        "baseline",
        "default-dio",
        "failure-resume",
        "live-capture",
        "kubectl-retained-read",
        "kubectl-cancel",
        "kubectl-coordinator-loss",
        "kubectl-endpoint-drift",
    }
)
VALIDATION_SUCCESS = "All validation checks passed successfully"
WORKLOAD_USER = "tester"
WORKLOAD_UID = 2000
WORKLOAD_GID = 2000


class IntegrationTestError(RuntimeError):
    """An actionable filesystem integration test failure."""


@dataclass(frozen=True)
class Fixture:
    """Live fixture details discovered from Kubernetes and Slurm."""

    login_pod: str | None
    login_container: str | None
    ssh_addresses: tuple[str, ...]
    slurm_nodes: tuple[str, ...]
    slurm_addresses: tuple[str, ...]
    architecture: str
    ssh_home_mode: str
    storage_backend: str
    kubectl_nodes: tuple[str, ...]
    kubectl_namespace: str
    kubectl_pv: str
    kubectl_pvc: str
    kubectl_image: str


@dataclass
class ScenarioRuntime:
    """Mutable state shared by a scenario's ordered command steps."""

    scenario: FilesystemScenarioSpec
    selector: str
    workspace: str
    local_workspace: Path
    artifact_root: Path
    data_root: str
    values: dict[str, str]
    copied_results: list[Path]


@dataclass(frozen=True)
class StepOutcome:
    """Result locations and output for one scenario command."""

    output: str
    remote_result: str | None
    local_result: Path | None


def _kubectl(config: Any, *arguments: str | Path) -> list[str | Path]:
    """Build a kubectl command using the fixture's private kubeconfig."""
    return ["kubectl", "--kubeconfig", config.kubeconfig, *arguments]


def _pod_command(
    config: Any,
    pod: str,
    container: str,
    command: str,
    *,
    as_user: str | None = None,
    timeout: int = 600,
    kill_after: int = 10,
) -> list[str | Path]:
    """Build a remotely bounded command for one fixture pod."""
    prefix: list[str | Path] = _kubectl(
        config,
        "-n",
        config.namespace,
        "exec",
        pod,
        "-c",
        container,
        "--",
    )
    if as_user:
        prefix.extend(("runuser", "-u", as_user, "--"))
    prefix.extend(
        (
            "timeout",
            f"--kill-after={kill_after}s",
            f"{timeout}s",
            "bash",
            "-lc",
            command,
        )
    )
    return prefix


def _ready(pod: dict[str, Any]) -> bool:
    """Return whether all containers in a running pod are ready."""
    status = pod.get("status", {})
    containers = status.get("containerStatuses", [])
    return (
        status.get("phase") == "Running"
        and bool(containers)
        and all(item.get("ready", False) for item in containers)
    )


def _pods_with_container(
    pods: list[dict[str, Any]], container: str
) -> list[dict[str, Any]]:
    """Return ready pods that contain *container*."""
    return [
        pod
        for pod in pods
        if _ready(pod)
        and not pod.get("metadata", {}).get("deletionTimestamp")
        and container
        in {item["name"] for item in pod.get("spec", {}).get("containers", [])}
    ]


def _load_state(config: Any) -> dict[str, Any]:
    """Load and validate the successful-setup marker."""
    path = config.state_dir / "state.json"
    if not path.is_file():
        raise IntegrationTestError(
            f"setup state is absent at {path}; run integration-test.py setup first"
        )
    state = json.loads(path.read_text(encoding="utf-8"))
    expected = {
        "cluster_name": config.cluster_name,
        "namespace": config.namespace,
        "export_dir": str(config.export_dir),
        "test_user": config.test_user,
        "test_uid": config.test_uid,
        "test_gid": config.test_gid,
    }
    mismatches = [
        f"{name}={state.get(name)!r} (expected {value!r})"
        for name, value in expected.items()
        if state.get(name) != value
    ]
    if mismatches:
        raise IntegrationTestError(
            "setup state does not match the requested fixture: " + ", ".join(mismatches)
        )
    mode = state.get("ssh_home_mode")
    if mode not in {"separate", "shared"}:
        raise IntegrationTestError(f"invalid ssh_home_mode in {path}: {mode!r}")
    backend = state.get("storage_backend")
    if backend not in {"nfs", "sbx-shared"}:
        raise IntegrationTestError(f"invalid storage_backend in {path}: {backend!r}")
    requested_backend = getattr(config, "storage_backend", "auto")
    if requested_backend not in {"auto", backend}:
        raise IntegrationTestError(
            f"requested storage backend {requested_backend} does not match "
            f"retained backend {backend}"
        )
    if backend == "sbx-shared":
        expected_root = str(config.sbx_shared_root)
        if state.get("sbx_shared_root") != expected_root:
            raise IntegrationTestError(
                "retained SBX shared root does not match the requested fixture: "
                f"{state.get('sbx_shared_root')!r} != {expected_root!r}"
            )
    return state


def _require_nodes(runner: Any, config: Any) -> list[dict[str, Any]]:
    """Return the exact ready three-node topology and labels."""
    clusters = runner.run(["kind", "get", "clusters"], timeout=30).stdout.split()
    if config.cluster_name not in clusters:
        raise IntegrationTestError(
            f"kind cluster {config.cluster_name!r} is not running; run setup first"
        )
    result = runner.run(_kubectl(config, "get", "nodes", "-o", "json"), timeout=30)
    nodes = json.loads(result.stdout)["items"]
    if len(nodes) != 3:
        raise IntegrationTestError(f"expected 3 Kubernetes nodes; found {len(nodes)}")
    if not all(
        any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in node.get("status", {}).get("conditions", [])
        )
        for node in nodes
    ):
        raise IntegrationTestError("all three Kubernetes nodes must be Ready")
    target = sum(
        node["metadata"].get("labels", {}).get("storage-scale-test/target") == "true"
        for node in nodes
    )
    login = sum(
        node["metadata"].get("labels", {}).get("storage-scale-test/login") == "true"
        for node in nodes
    )
    if (target, login) != (2, 1):
        raise IntegrationTestError(
            f"node label invariant failed: target={target}, login={login}"
        )
    return nodes


def _require_storage(runner: Any, config: Any) -> tuple[str, str]:
    """Require bound integration PVCs and return the data PV/PVC names."""
    result = runner.run(
        _kubectl(config, "-n", config.namespace, "get", "pvc", "-o", "json"),
        timeout=30,
    )
    claims = {
        item["metadata"]["name"]: item for item in json.loads(result.stdout)["items"]
    }
    expected = {"storage-test-rwx", "ssh-home-rwx"}
    if any(
        claims.get(name, {}).get("status", {}).get("phase") != "Bound"
        for name in expected
    ):
        phases = {
            name: item.get("status", {}).get("phase") for name, item in claims.items()
        }
        raise IntegrationTestError(f"integration PVCs are not Bound: {phases}")
    pvc = claims[KUBECTL_STORAGE_PVC]
    pv = str(pvc.get("spec", {}).get("volumeName", ""))
    if not pv:
        raise IntegrationTestError(f"{KUBECTL_STORAGE_PVC} has no bound PV")
    return pv, KUBECTL_STORAGE_PVC


def _pod_inventory(runner: Any, config: Any) -> list[dict[str, Any]]:
    """Return the namespace pod inventory."""
    result = runner.run(
        _kubectl(config, "-n", config.namespace, "get", "pods", "-o", "json"),
        timeout=30,
    )
    return json.loads(result.stdout)["items"]


def _require_pods(
    pods: list[dict[str, Any]],
    substrates: set[str],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Require only the live workloads needed by selected substrates."""
    login = _pods_with_container(pods, "login")
    ssh = sorted(
        _pods_with_container(pods, "sshd"), key=lambda item: item["metadata"]["name"]
    )
    slurmd = _pods_with_container(pods, "slurmd")
    login_required = bool({"ssh", "slurm"} & substrates)
    invalid = login_required and len(login) != 1
    invalid = invalid or ("ssh" in substrates and len(ssh) != 2)
    invalid = invalid or ("slurm" in substrates and len(slurmd) != 2)
    if invalid:
        raise IntegrationTestError(
            "fixture workloads are incomplete: "
            f"required={sorted(substrates)}, login={len(login)}, ssh={len(ssh)}, "
            f"slurmd={len(slurmd)}; run setup"
        )
    return (login[0] if login_required else None), ssh


def _require_kubectl_nodes(
    nodes: list[dict[str, Any]], host_architecture: str
) -> tuple[str, ...]:
    """Return exact selected worker nodes after platform compatibility checks."""
    targets = sorted(
        str(item["metadata"]["name"])
        for item in nodes
        if item.get("metadata", {}).get("labels", {}).get("storage-scale-test/target")
        == "true"
    )
    if len(targets) != 2:
        raise IntegrationTestError(
            f"expected two Kubernetes target nodes; found {targets}"
        )
    expected = {"x86_64": "amd64", "aarch64": "arm64"}.get(host_architecture)
    observed = {
        str(item["metadata"]["name"]): str(
            item.get("status", {}).get("nodeInfo", {}).get("architecture", "")
        )
        for item in nodes
    }
    if expected is None or any(observed.get(name) != expected for name in targets):
        raise IntegrationTestError(
            "Kubernetes target architecture mismatch: "
            + ", ".join(f"{name}={observed.get(name)!r}" for name in targets)
        )
    return tuple(targets)


def _probe_pod(
    runner: Any,
    config: Any,
    pod: str,
    container: str,
    command: str,
    *,
    as_user: str | None = None,
) -> str:
    """Run a short fixture probe and return stripped stdout."""
    result = runner.run(
        _pod_command(
            config,
            pod,
            container,
            command,
            as_user=as_user,
            timeout=30,
        ),
        timeout=45,
    )
    return result.stdout.strip()


def _kubectl_utility_manifest(
    name: str, fixture: Fixture, node_name: str | None = None
) -> str:
    """Render one short-lived, non-root PVC utility Pod manifest."""
    document = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "labels": {"storage-scale-test/fixture-utility": "true"},
        },
        "spec": {
            "automountServiceAccountToken": False,
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 1,
            **(
                {"nodeName": node_name}
                if node_name
                else {"nodeSelector": {"storage-scale-test/login": "true"}}
            ),
            "securityContext": {
                "runAsNonRoot": True,
                "runAsUser": WORKLOAD_UID,
                "runAsGroup": WORKLOAD_GID,
                "seccompProfile": {"type": "RuntimeDefault"},
            },
            "containers": [
                {
                    "name": "utility",
                    "image": fixture.kubectl_image,
                    "imagePullPolicy": "Never",
                    "command": ["/bin/bash", "-ceu", "--"],
                    "args": ['test "$(id -u)" = 2000; exec sleep infinity'],
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [
                        {"name": "storage", "mountPath": KUBECTL_STORAGE_MOUNT}
                    ],
                }
            ],
            "volumes": [
                {
                    "name": "storage",
                    "persistentVolumeClaim": {"claimName": fixture.kubectl_pvc},
                }
            ],
        },
    }
    return json.dumps(document)


def _wait_for_kubectl_utility(runner: Any, config: Any, name: str) -> None:
    """Wait a bounded interval for an exact utility Pod to become Ready."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = runner.run(
            _kubectl(config, "-n", config.namespace, "get", "pod", name, "-o", "json"),
            check=False,
            timeout=20,
        )
        if result.returncode == 0:
            pod = json.loads(result.stdout)
            if _ready(pod) and not pod.get("metadata", {}).get("deletionTimestamp"):
                return
        time.sleep(1)
    raise IntegrationTestError(f"storage utility Pod {name} did not become Ready")


def _storage_utility_shell(
    runner: Any,
    config: Any,
    fixture: Fixture,
    command: str,
    *,
    timeout: int = 60,
    node_name: str | None = None,
) -> str:
    """Run one storage command without depending on a Slinky LoginSet Pod."""
    name = f"{KUBECTL_UTILITY_PREFIX}-{secrets.token_hex(4)}"
    manifest = _kubectl_utility_manifest(name, fixture, node_name)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as stream:
        stream.write(manifest)
        stream.flush()
        stream.seek(0)
        runner.run(
            _kubectl(config, "-n", config.namespace, "create", "-f", "-"),
            stdin=stream,
            timeout=60,
        )
    try:
        _wait_for_kubectl_utility(runner, config, name)
        result = runner.run(
            [
                *_kubectl(
                    config,
                    "-n",
                    config.namespace,
                    "exec",
                    name,
                    "-c",
                    "utility",
                    "--",
                ),
                "timeout",
                "--kill-after=10s",
                f"{timeout}s",
                "bash",
                "-lc",
                command,
            ],
            timeout=135,
        )
        return result.stdout
    finally:
        result = runner.run(
            _kubectl(
                config,
                "-n",
                config.namespace,
                "delete",
                "pod",
                name,
                "--ignore-not-found",
                "--wait=false",
            ),
            check=False,
            timeout=30,
        )
        if result.returncode:
            LOG.warning(
                "could not delete storage utility Pod %s: %s",
                name,
                (result.stderr or result.stdout).strip(),
            )


def _storage_shell(
    runner: Any,
    config: Any,
    fixture: Fixture,
    command: str,
    *,
    timeout: int = 60,
    node_name: str | None = None,
) -> str:
    """Run a PVC command through LoginSet when present, otherwise a utility Pod."""
    if (
        node_name is None
        and fixture.login_pod is not None
        and fixture.login_container is not None
    ):
        return _login_shell(runner, config, fixture, command, timeout=timeout)
    return _storage_utility_shell(
        runner, config, fixture, command, timeout=timeout, node_name=node_name
    )


def _require_fixture(
    runner: Any,
    config: Any,
    substrates: set[str],
    ssh_home_mode: str = "separate",
) -> Fixture:
    """Validate setup without reconciling or installing anything."""
    state = _load_state(config)
    required_host_tools = {
        "bash",
        "file",
        "find",
        "git",
        "tar",
        "timeout",
    }
    if "ssh" in substrates:
        required_host_tools.update(("ssh-add", "ssh-agent"))
    if "kubectl" in substrates:
        required_host_tools.add("kubectl")
    missing_host_tools = [
        tool for tool in sorted(required_host_tools) if shutil.which(tool) is None
    ]
    if missing_host_tools:
        raise IntegrationTestError(
            "required host tools are absent: " + ", ".join(missing_host_tools)
        )
    if not config.kubeconfig.is_file():
        raise IntegrationTestError(
            f"private kubeconfig is absent at {config.kubeconfig}; run setup first"
        )
    nodes = _require_nodes(runner, config)
    storage_pv, storage_pvc = _require_storage(runner, config)
    login, ssh = _require_pods(_pod_inventory(runner, config), substrates)
    login_name = str(login["metadata"]["name"]) if login is not None else None
    addresses: tuple[str, ...] = ()
    if "ssh" in substrates:
        addresses = tuple(str(item["status"]["podIP"]) for item in ssh)
        if len(set(addresses)) != 2:
            raise IntegrationTestError(
                f"SSH workers lack distinct addresses: {addresses}"
            )
    tools = (
        "for tool in bash file find tar timeout; do "
        'command -v "$tool" >/dev/null || exit 1; done'
    )
    if login_name is not None:
        _probe_pod(
            runner,
            config,
            login_name,
            "login",
            tools,
            as_user=WORKLOAD_USER,
        )
    if "ssh" in substrates:
        _probe_pod(
            runner,
            config,
            str(ssh[0]["metadata"]["name"]),
            "sshd",
            tools,
            as_user="tester",
        )
    mount_probe = "test -w /mnt/storage-test"
    if state["storage_backend"] == "nfs":
        mount_probe += (
            " && case $(stat -f -c %T /mnt/storage-test) in "
            "nfs|nfs4) true;; *) false;; esac"
        )
    if login_name is not None:
        _probe_pod(
            runner,
            config,
            login_name,
            "login",
            mount_probe,
            as_user=WORKLOAD_USER,
        )
    if "ssh" in substrates:
        _probe_pod(
            runner,
            config,
            str(ssh[0]["metadata"]["name"]),
            "sshd",
            mount_probe,
            as_user="tester",
        )
    host_arch = platform.machine()
    fixture_architectures = {"host": host_arch}
    if login_name is not None:
        fixture_architectures["login"] = _probe_pod(
            runner,
            config,
            login_name,
            "login",
            "uname -m",
            as_user=WORKLOAD_USER,
        )
    if "ssh" in substrates:
        fixture_architectures["ssh"] = _probe_pod(
            runner,
            config,
            str(ssh[0]["metadata"]["name"]),
            "sshd",
            "uname -m",
            as_user="tester",
        )
    if len(set(fixture_architectures.values())) != 1:
        raise IntegrationTestError(
            "fixture architecture mismatch: "
            + ", ".join(
                f"{name}={architecture}"
                for name, architecture in fixture_architectures.items()
            )
        )
    if host_arch not in ELBENCHO_ARCHIVES:
        raise IntegrationTestError(f"unsupported fixture architecture: {host_arch}")
    kubectl_nodes: tuple[str, ...] = ()
    if "kubectl" in substrates:
        kubectl_nodes = _require_kubectl_nodes(nodes, host_arch)
    slurm_nodes: tuple[str, ...] = ()
    slurm_addresses: tuple[str, ...] = ()
    if "slurm" in substrates:
        if login_name is None:
            raise IntegrationTestError("Slurm fixture requires one LoginSet pod")
        slurm_output = _probe_pod(
            runner,
            config,
            login_name,
            "login",
            "sinfo -N -h -o %N | sort -u",
            as_user=WORKLOAD_USER,
        )
        slurm_nodes = tuple(line for line in slurm_output.splitlines() if line)
        if len(slurm_nodes) != 2:
            raise IntegrationTestError(
                f"expected two Slurm compute nodes; found {slurm_nodes}"
            )
        slurm_address_output = _probe_pod(
            runner,
            config,
            login_name,
            "login",
            "for node in "
            + " ".join(shlex.quote(node) for node in slurm_nodes)
            + "; do getent ahostsv4 \"$node\" | awk 'NR == 1 {print $1}'; done",
            as_user=WORKLOAD_USER,
        )
        slurm_addresses = tuple(
            line for line in slurm_address_output.splitlines() if line
        )
        if len(slurm_addresses) != 2 or len(set(slurm_addresses)) != 2:
            raise IntegrationTestError(
                f"Slurm workers lack distinct IPv4 addresses: {slurm_addresses}"
            )
    return Fixture(
        login_pod=login_name,
        login_container="login" if login_name is not None else None,
        ssh_addresses=addresses,
        slurm_nodes=slurm_nodes,
        slurm_addresses=slurm_addresses,
        architecture=host_arch,
        ssh_home_mode=ssh_home_mode,
        storage_backend=str(state["storage_backend"]),
        kubectl_nodes=kubectl_nodes,
        kubectl_namespace=str(config.namespace),
        kubectl_pv=storage_pv,
        kubectl_pvc=storage_pvc,
        kubectl_image=ELBENCHO_FIXTURE_IMAGE,
    )


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of *path*."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _request(url: str, accept: str = "application/vnd.github+json") -> Any:
    """Open a bounded upstream request with retries."""
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "storage-scale-test-integration",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    last_error: Exception | None = None
    for attempt in range(1, 4):
        try:
            return urllib.request.urlopen(request, timeout=60)
        except (OSError, urllib.error.URLError) as error:
            last_error = error
            if attempt < 3:
                time.sleep(attempt * 2)
    raise IntegrationTestError(f"download failed after 3 attempts: {url}: {last_error}")


def _asset_url(name: str) -> str:
    """Resolve a pinned release asset through the public GitHub API."""
    with _request(ELBENCHO_RELEASE_API) as response:
        release = json.load(response)
    matches = [
        asset for asset in release.get("assets", []) if asset.get("name") == name
    ]
    if len(matches) != 1:
        raise IntegrationTestError(
            f"expected one {name!r} asset in {ELBENCHO_VERSION}; found {len(matches)}"
        )
    return str(matches[0]["url"])


def _download_archive(destination: Path, name: str, expected: str) -> None:
    """Download and verify one pinned elbencho archive atomically."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = _asset_url(name)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            with _request(url, "application/octet-stream") as response:
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_ARCHIVE_BYTES:
                    raise IntegrationTestError(
                        f"refusing oversized archive {name}: {content_length} bytes"
                    )
                copied = 0
                while chunk := response.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > MAX_ARCHIVE_BYTES:
                        raise IntegrationTestError(
                            f"refusing archive {name} larger than "
                            f"{MAX_ARCHIVE_BYTES} bytes"
                        )
                    handle.write(chunk)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    actual = _sha256(temporary)
    if actual != expected:
        temporary.unlink(missing_ok=True)
        raise IntegrationTestError(
            f"checksum mismatch for {name}: expected {expected}, got {actual}"
        )
    temporary.chmod(0o640)
    temporary.replace(destination)


def _extract_container_elbencho(
    runner: Any, cache: Path, binary: Path, runtime: Path, architecture: str
) -> None:
    """Build a portable wrapper from the digest-pinned upstream image."""
    runner.run(["docker", "pull", ELBENCHO_CONTAINER], timeout=300)
    container = runner.run(
        ["docker", "create", "--entrypoint", "sleep", ELBENCHO_CONTAINER, "infinity"]
    ).stdout.strip()
    if not container:
        raise IntegrationTestError("Docker did not return an elbencho container ID")
    archive = cache / f".{binary.name}.runtime.tar"
    try:
        bundle = """
set -eu
rm -rf /tmp/elbencho-runtime /tmp/elbencho-runtime.tar
mkdir -p /tmp/elbencho-runtime
libraries=$(ldd /usr/bin/elbencho | awk '$3 ~ /^\\// {print $3} $1 ~ /^\\// {print $1}')
loader=$(ldd /usr/bin/elbencho | awk '{for (i=1; i<=NF; i++) if ($i ~ /^\\/.*ld-linux.*\\.so/) {print $i; exit}}')
test -n "$loader"
cp -L --parents /usr/bin/elbencho $libraries /tmp/elbencho-runtime
printf '%s\n' "$loader" > /tmp/elbencho-runtime/.loader-path
cd /tmp/elbencho-runtime
tar -cf /tmp/elbencho-runtime.tar .
""".strip()
        runner.run(["docker", "start", container])
        runner.run(["docker", "exec", container, "sh", "-ec", bundle])
        runner.run(["docker", "cp", f"{container}:/tmp/elbencho-runtime.tar", archive])
        temporary_runtime = cache / f".{runtime.name}.new"
        shutil.rmtree(temporary_runtime, ignore_errors=True)
        temporary_runtime.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(temporary_runtime, filter="data")
        loader_marker = temporary_runtime / ".loader-path"
        loader_path = Path(loader_marker.read_text(encoding="utf-8").strip())
        if not loader_path.is_absolute() or ".." in loader_path.parts:
            raise IntegrationTestError(
                f"container reported an invalid dynamic loader path: {loader_path}"
            )
        loader_relative = loader_path.relative_to("/")
        if not (temporary_runtime / loader_relative).is_file():
            raise IntegrationTestError(
                f"container runtime is missing its dynamic loader: {loader_path}"
            )
        loader_marker.unlink()
        shutil.rmtree(runtime, ignore_errors=True)
        temporary_runtime.replace(runtime)
        library_arch = (
            "aarch64-linux-gnu" if architecture == "aarch64" else "x86_64-linux-gnu"
        )
        wrapper = "\n".join(
            (
                "#!/usr/bin/env bash",
                "set -euo pipefail",
                'runtime_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/'
                'elbencho-runtime" && pwd)',
                f'exec "$runtime_dir/{loader_relative.as_posix()}" \\',
                f'    --library-path "$runtime_dir/usr/lib/{library_arch}:'
                f'$runtime_dir/lib/{library_arch}" \\',
                '    "$runtime_dir/usr/bin/elbencho" "$@"',
                "",
            )
        )
        binary.write_text(wrapper, encoding="utf-8")
        binary.chmod(0o755)
    finally:
        archive.unlink(missing_ok=True)
        runner.run(["docker", "rm", "--force", container], check=False)


def _sbx_bundle_document(architecture: str, binary_name: str) -> dict[str, object]:
    """Return the exact recipe identity for a cached SBX Elbencho bundle."""
    return {
        "schema": 1,
        "recipe": SBX_ELBENCHO_BUNDLE_RECIPE,
        "container": ELBENCHO_CONTAINER,
        "architecture": architecture,
        "binary_name": binary_name,
    }


def _write_sbx_bundle_marker(path: Path, document: dict[str, object]) -> None:
    """Atomically record a successfully built SBX Elbencho bundle."""
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(document, handle, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.chmod(0o640)
    temporary.replace(path)


def _sbx_bundle_is_current(
    binary: Path,
    runtime: Path,
    marker: Path,
    expected: dict[str, object],
) -> bool:
    """Return whether all cached bundle artifacts match the current recipe."""
    if not binary.is_file() or not runtime.is_dir() or not marker.is_file():
        return False
    try:
        document = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return document == expected


def _ensure_elbencho(
    runner: Any, config: Any, architecture: str, storage_backend: str
) -> tuple[Path, str, Path | None]:
    """Return a verified, extracted pinned elbencho binary and staged name."""
    archive_name, expected, binary_name = ELBENCHO_ARCHIVES[architecture]
    cache = config.state_dir / "test-cache"
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / f"{ELBENCHO_VERSION}-{binary_name}"
    if storage_backend == "sbx-shared":
        runtime = cache / f"{ELBENCHO_VERSION}-{binary_name}.runtime"
        marker = cache / f"{ELBENCHO_VERSION}-{binary_name}.bundle.json"
        bundle_document = _sbx_bundle_document(architecture, binary_name)
        if not _sbx_bundle_is_current(binary, runtime, marker, bundle_document):
            LOG.info("Extracting pinned elbencho %s container", ELBENCHO_VERSION)
            _extract_container_elbencho(runner, cache, binary, runtime, architecture)
            _write_sbx_bundle_marker(marker, bundle_document)
        else:
            LOG.info("Using cached elbencho from the pinned upstream container")
        return binary, binary_name, runtime
    archive = cache / f"{ELBENCHO_VERSION}-{archive_name}"
    if not archive.is_file() or _sha256(archive) != expected:
        LOG.info(
            "Downloading pinned elbencho %s for %s", ELBENCHO_VERSION, architecture
        )
        _download_archive(archive, archive_name, expected)
    else:
        LOG.info("Using cached pinned elbencho archive for %s", architecture)
    with tarfile.open(archive, "r:gz") as tar:
        members = [
            member
            for member in tar.getmembers()
            if member.isfile() and Path(member.name).name == "elbencho"
        ]
        if len(members) != 1:
            raise IntegrationTestError(
                f"expected one elbencho binary in {archive}; found {len(members)}"
            )
        source = tar.extractfile(members[0])
        if source is None:
            raise IntegrationTestError(f"cannot extract elbencho from {archive}")
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as handle:
            temporary = Path(handle.name)
            shutil.copyfileobj(source, handle)
    temporary.chmod(0o755)
    temporary.replace(binary)
    return binary, binary_name, None


def _shell(value: str | Path) -> str:
    """Quote one value for the generated Bash environment."""
    return shlex.quote(str(value))


def _slurm_run_time(timeout_seconds: int) -> str:
    """Return an allocation limit that outlives the harness deadline."""
    total = timeout_seconds + SLURM_CLEANUP_MARGIN_SECONDS
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _override_block(
    selector: str,
    remote_root: str,
    fixture: Fixture,
    *,
    results_dir: str | None = None,
    logs_dir: str | None = None,
    extra_env: str = "",
    timeout_seconds: int = 300,
    logical_test_root: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Return template overrides and small support-file contents."""
    data = logical_test_root if selector == "kubectl" else "/mnt/storage-test"
    if selector == "kubectl" and not data:
        data = "primary"
    lines = [
        "# Bounded integration regression overrides.",
        f"export EXECUTION_SUBSTRATE={_shell(selector)}",
        f"export RESULTS_DIR={_shell(results_dir or remote_root + '/results')}",
        f"export LOGS_DIR={_shell(logs_dir or remote_root + '/logs')}",
        "ORDER_NODES=1",
        'client_type="cpu"',
        f"client_arch={_shell(fixture.architecture)}",
        "unset TEST_DIRS",
        f"declare -A TEST_DIRS=([{_shell(data)}]=1)",
        "export FS_MAX_AGG_THROUGHPUT=1",
        "export FS_MAX_NODE_THROUGHPUT_GBPS=1",
        "export FS_MAX_NODE_IOPS=100",
        'export ELBENCHO_SCALE_THREAD_LIST=("1")',
        "export ELBENCHO_FILE_SIZE_MULTIPLIER=1",
        'export ELBENCHO_FILE_LAYOUT="shared-directory"',
        "export ELBENCHO_FILES_PER_NODE=1",
        'export ELBENCHO_FILE_SIZE="16M"',
        'export ELBENCHO_SCALE_IO_SIZES=("4K")',
        'export ELBENCHO_IODEPTH_LIST=("1")',
        "export ELBENCHO_SCALE_READ_WRITE_DURATION=1",
        "export ELBENCHO_READ_AFTER_WRITE_PAUSE=0",
        "export ELBENCHO_LIVE_CSV_EXTENDED=0",
        "export ELBENCHO_SINGLE_BIG_FILE=0",
        'export OBJ_BUCKET=""',
    ]
    support: dict[str, str] = {}
    if selector == "ssh":
        host_file = f"{remote_root}/ssh-hosts"
        lines.extend(
            (
                f"export SSH_HOST_LIST={_shell(host_file)}",
                'export SSH_USER="tester"',
                "unset SLURM_NODE_INCLUDES SLURM_NODE_IGNORES",
            )
        )
        if fixture.ssh_home_mode == "shared":
            lines.append("export SSH_HOMEDIR_SHARED=1")
        else:
            lines.append("unset SSH_HOMEDIR_SHARED")
        support["ssh-hosts"] = "\n".join(fixture.ssh_addresses) + "\n"
    elif selector == "slurm":
        include_file = f"{remote_root}/slurm-nodes"
        ignore_file = f"{remote_root}/slurm-ignore"
        lines.extend(
            (
                "unset SSH_HOST_LIST SSH_USER SSH_HOMEDIR_SHARED",
                'account="storage-test"',
                'reservation=""',
                'partition="all"',
                f"run_time={_shell(_slurm_run_time(timeout_seconds))}",
                "SLURM_EXCLUSIVE_USER=0",
                f"export SLURM_NODE_INCLUDES={_shell(include_file)}",
                f"export SLURM_NODE_IGNORES={_shell(ignore_file)}",
            )
        )
        support["slurm-nodes"] = "\n".join(fixture.slurm_nodes) + "\n"
        support["slurm-ignore"] = ""
    elif selector == "kubectl":
        if not fixture.kubectl_nodes:
            raise IntegrationTestError(
                "Kubernetes fixture has no selected worker nodes"
            )
        lines.extend(
            (
                "unset SSH_HOST_LIST SSH_USER SSH_HOMEDIR_SHARED",
                "unset SLURM_NODE_INCLUDES SLURM_NODE_IGNORES",
                f"export KUBECTL_NAMESPACE={_shell(fixture.kubectl_namespace)}",
                f"export KUBECTL_PV={_shell(fixture.kubectl_pv)}",
                f"export KUBECTL_PVC={_shell(fixture.kubectl_pvc)}",
                f"export KUBECTL_NODE_SELECTOR={_shell(KUBECTL_TARGET_SELECTOR)}",
                f"export KUBECTL_ELBENCHO_IMAGE={_shell(fixture.kubectl_image)}",
                "export KUBECTL_IMAGE_PULL_POLICY=Never",
                f"export KUBECTL_RUN_AS_USER={WORKLOAD_UID}",
                f"export KUBECTL_RUN_AS_GROUP={WORKLOAD_GID}",
            )
        )
    else:
        raise IntegrationTestError(f"unsupported integration selector: {selector}")
    if extra_env:
        lines.extend(("# Scenario-specific integration overrides.", extra_env.rstrip()))
    return "\n".join(lines) + "\n", support


def _render_env(
    template: Path,
    selector: str,
    remote_root: str,
    fixture: Fixture,
    *,
    results_dir: str | None = None,
    logs_dir: str | None = None,
    extra_env: str = "",
    timeout_seconds: int = 300,
    logical_test_root: str | None = None,
) -> tuple[str, dict[str, str]]:
    """Render one runtime env from the repository's real user template."""
    text = template.read_text(encoding="utf-8")
    anchor = "# STORAGE_SCALE_TEST_INTEGRATION_OVERRIDES"
    if text.count(anchor) != 1:
        raise IntegrationTestError(
            f"expected exactly one integration override marker in {template}"
        )
    overrides, support = _override_block(
        selector,
        remote_root,
        fixture,
        results_dir=results_dir,
        logs_dir=logs_dir,
        extra_env=extra_env,
        timeout_seconds=timeout_seconds,
        logical_test_root=logical_test_root,
    )
    rendered = text.replace(anchor, overrides + "\n" + anchor)
    return rendered, support


def _validate_deployment_archive(
    archive: Path,
    destination: Path,
    binary_name: str,
    binary_digest: str,
    architecture: str,
    runner: Any,
    bundled_runtime: bool,
) -> Path:
    """Validate and safely extract the deployment tarball."""
    if not archive.is_file() or archive.stat().st_size > MAX_DEPLOYMENT_ARCHIVE_BYTES:
        raise IntegrationTestError(
            f"deployment archive is absent or oversized: {archive}"
        )
    required = {
        "storage-scale-test/NOTICE",
        "storage-scale-test/env.sh.template",
        "storage-scale-test/validate_env.sh",
        "storage-scale-test/lib/env_base.sh",
        "storage-scale-test/storage-tests/fs/nv-elbencho-sweep.sh",
        f"storage-scale-test/utils/{binary_name}",
    }
    if bundled_runtime:
        required.add("storage-scale-test/utils/elbencho-runtime/usr/bin/elbencho")
    names: set[str] = set()
    content_bytes = 0
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        if len(members) > MAX_DEPLOYMENT_FILES:
            raise IntegrationTestError(
                f"deployment archive has too many members: {len(members)}"
            )
        for member in members:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or not path.parts
                or path.parts[0] != "storage-scale-test"
                or ".." in path.parts
                or member.name in names
                or member.issym()
                or member.islnk()
                or member.isdev()
                or member.isfifo()
                or not (member.isfile() or member.isdir())
            ):
                raise IntegrationTestError(
                    f"unsafe deployment archive member: {member.name!r}"
                )
            if "env.sh" == path.name or ".obj_auth" in path.parts:
                raise IntegrationTestError(
                    f"unexpected private deployment member: {member.name!r}"
                )
            names.add(member.name)
            content_bytes += member.size
            if content_bytes > MAX_DEPLOYMENT_CONTENT_BYTES:
                raise IntegrationTestError("deployment archive content is oversized")
        absent = sorted(required - names)
        if absent:
            raise IntegrationTestError(
                "deployment archive lacks required files: " + ", ".join(absent)
            )
        destination.mkdir(parents=True)
        tar.extractall(destination, filter="data")
    root = destination / "storage-scale-test"
    packaged_binary = root / "utils" / binary_name
    if (
        not packaged_binary.is_file()
        or packaged_binary.is_symlink()
        or not os.access(packaged_binary, os.X_OK)
        or _sha256(packaged_binary) != binary_digest
    ):
        raise IntegrationTestError(
            f"packaged elbencho failed identity checks: {packaged_binary}"
        )
    inspected_binary = packaged_binary
    if bundled_runtime:
        inspected_binary = root / "utils" / "elbencho-runtime" / "usr/bin/elbencho"
    description = runner.run(["file", inspected_binary], timeout=30).stdout
    expected_arch = "x86-64" if architecture == "x86_64" else "aarch64"
    if expected_arch not in description:
        raise IntegrationTestError(
            f"packaged elbencho architecture mismatch: {description.strip()}"
        )
    runner.run([packaged_binary, "--help"], timeout=30)
    return root


def _build_deployment_archive(
    runner: Any,
    config: Any,
    repo_root: Path,
    build_root: Path,
    binary: Path,
    binary_name: str,
    architecture: str,
    runtime: Path | None,
) -> tuple[Path, Path]:
    """Reuse or build and inspect one filesystem-only deployment tarball."""
    request = DeploymentCacheRequest(
        repo_root=repo_root,
        cache_root=config.state_dir / "test-cache" / "deployments",
        architecture=architecture,
        binary=binary,
        binary_name=binary_name,
        runtime=runtime,
    )
    try:
        cached = get_or_build_deployment(runner, request)
    except DeploymentCacheError as error:
        raise IntegrationTestError(str(error)) from error
    LOG.info(
        "%s deployment cache entry %s",
        "Reusing" if cached.cache_hit else "Built",
        cached.key,
    )
    shutil.copy2(cached.build_log, build_root / "build-tarball.log")
    archive = cached.archive
    extracted = _validate_deployment_archive(
        archive,
        build_root / "extracted",
        binary_name,
        _sha256(binary),
        architecture,
        runner,
        runtime is not None,
    )
    return archive, extracted


def _write_runtime_files(
    workspace: Path,
    selector: str,
    runtime_root: str,
    fixture: Fixture,
    *,
    template: Path | None = None,
    results_dir: str | None = None,
    logs_dir: str | None = None,
    extra_env: str = "",
    extra_support: dict[str, str] | None = None,
    timeout_seconds: int = 300,
    logical_test_root: str | None = None,
) -> None:
    """Add the generated environment and support files to a deployment."""
    rendered, support = _render_env(
        template or workspace / "env.sh.template",
        selector,
        runtime_root,
        fixture,
        results_dir=results_dir,
        logs_dir=logs_dir,
        extra_env=extra_env,
        timeout_seconds=timeout_seconds,
        logical_test_root=logical_test_root,
    )
    (workspace / "env.sh").write_text(rendered, encoding="utf-8")
    (workspace / "env.sh").chmod(0o640)
    for name, content in support.items():
        (workspace / name).write_text(content, encoding="utf-8")
        (workspace / name).chmod(0o640)
    for name, content in (extra_support or {}).items():
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o640)


def _stream_to_login(
    runner: Any,
    config: Any,
    fixture: Fixture,
    source: Path,
    command: list[str | Path],
    *,
    as_user: str | None = None,
    timeout: int = 180,
) -> None:
    """Stream one local archive to a command in the Slinky LoginSet."""
    with source.open("rb") as stream:
        pod_command: list[str | Path] = _kubectl(
            config,
            "-n",
            config.namespace,
            "exec",
            "-i",
            fixture.login_pod,
            "-c",
            fixture.login_container,
            "--",
        )
        if as_user:
            pod_command.extend(("runuser", "-u", as_user, "--"))
        pod_command.extend(command)
        runner.run(pod_command, stdin=stream, timeout=timeout)


def _stage_ssh_runtime(
    runner: Any, config: Any, runtime: Path, pods: list[dict[str, Any]]
) -> None:
    """Install the container-derived runtime beside each SSH wrapper target."""
    ssh_pods = sorted(
        _pods_with_container(pods, "sshd"),
        key=lambda item: item["metadata"]["name"],
    )
    with tempfile.NamedTemporaryFile(suffix=".tar") as stream:
        with tarfile.open(fileobj=stream, mode="w") as archive:
            archive.add(runtime, arcname="elbencho-runtime")
        stream.flush()
        for pod in ssh_pods:
            stream.seek(0)
            command = [
                *_kubectl(
                    config,
                    "-n",
                    config.namespace,
                    "exec",
                    "-i",
                    pod["metadata"]["name"],
                    "-c",
                    "sshd",
                    "--",
                ),
                "runuser",
                "-u",
                WORKLOAD_USER,
                "--",
                "bash",
                "-ec",
                "umask 0007; rm -rf -- /home/tester/elbencho-runtime && "
                "tar --no-same-owner --no-same-permissions -xf - -C /home/tester",
            ]
            runner.run(command, stdin=stream, timeout=180)


def _stage_workspace(
    runner: Any,
    config: Any,
    fixture: Fixture,
    selector: str,
    archive: Path,
    extracted: Path,
    build_root: Path,
    workspace_id: str,
) -> str:
    """Stage a packaged deployment for SSH, Slurm, or kubectl execution."""
    if selector == "ssh":
        workspace = build_root / "ssh" / "storage-scale-test"
        shutil.copytree(extracted, workspace)
        _write_runtime_files(workspace, selector, str(workspace), fixture)
        return str(workspace)
    if selector == "kubectl":
        workspace = build_root / "kubectl" / "storage-scale-test"
        shutil.copytree(extracted, workspace)
        _write_runtime_files(workspace, selector, str(workspace), fixture)
        return str(workspace)
    if selector != "slurm":
        raise IntegrationTestError(f"unsupported integration selector: {selector}")

    remote_base = f"{REMOTE_BASE}/workspaces/{workspace_id}"
    remote_root = f"{remote_base}/storage-scale-test"
    reset = (
        f"umask 0007; rm -rf -- {_shell(remote_base)} && "
        f"mkdir -p -- {_shell(remote_base)}"
    )
    runner.run(
        _pod_command(
            config,
            fixture.login_pod,
            fixture.login_container,
            reset,
            as_user=WORKLOAD_USER,
            timeout=60,
        ),
        timeout=75,
    )
    extract_command = [
        "bash",
        "-ec",
        "umask 0007; tar --no-same-owner --no-same-permissions "
        f"-xzf - -C {_shell(remote_base)}",
    ]
    _stream_to_login(
        runner,
        config,
        fixture,
        archive,
        extract_command,
        as_user=WORKLOAD_USER,
    )
    support_stage = build_root / "slurm-runtime"
    support_stage.mkdir()
    _write_runtime_files(
        support_stage,
        selector,
        remote_root,
        fixture,
        template=extracted / "env.sh.template",
    )
    support_archive = build_root / "slurm-runtime.tar"
    with tarfile.open(support_archive, "w") as tar:
        for child in sorted(support_stage.iterdir()):
            tar.add(child, arcname=child.name)
    support_command = [
        "bash",
        "-ec",
        "umask 0007; tar --no-same-owner --no-same-permissions "
        f"-xf - -C {_shell(remote_root)}",
    ]
    _stream_to_login(
        runner,
        config,
        fixture,
        support_archive,
        support_command,
        as_user=WORKLOAD_USER,
    )
    return remote_root


def _test_command(
    config: Any, fixture: Fixture, selector: str, command: str, timeout: int
) -> list[str | Path]:
    """Build one substrate test command."""
    if selector == "ssh":
        private_key = config.keys_dir / "id_ed25519"
        host_command = "\n".join(
            (
                "set -euo pipefail",
                'eval "$(ssh-agent -s)" >/dev/null',
                "trap 'ssh-agent -k >/dev/null 2>&1 || true' EXIT",
                f"ssh-add -- {_shell(private_key)} >/dev/null 2>&1",
                command,
            )
        )
        return [
            "timeout",
            "--kill-after=10s",
            f"{timeout}s",
            "bash",
            "-c",
            host_command,
        ]
    if selector == "slurm":
        if fixture.login_pod is None or fixture.login_container is None:
            raise IntegrationTestError("Slurm test command requires a LoginSet pod")
        return _pod_command(
            config,
            fixture.login_pod,
            fixture.login_container,
            command,
            as_user=WORKLOAD_USER,
            timeout=timeout,
            kill_after=105,
        )
    raise IntegrationTestError(
        f"selector {selector!r} requires the Kubernetes lifecycle adapter"
    )


def _run_step(
    runner: Any,
    config: Any,
    fixture: Fixture,
    selector: str,
    name: str,
    command: str,
    log_dir: Path,
    timeout: int,
    *,
    expected_failure: bool = False,
) -> str:
    """Run one bounded substrate step and preserve diagnostic output."""
    LOG.info("Running %s filesystem step: %s", selector, name)
    if selector == "slurm":
        outer_grace = 120
    elif selector == "ssh":
        outer_grace = 30
    else:
        raise IntegrationTestError(
            f"selector {selector!r} requires the Kubernetes lifecycle adapter"
        )
    result = runner.run(
        _test_command(config, fixture, selector, command, timeout),
        check=False,
        timeout=timeout + outer_grace,
    )
    output = result.stdout + result.stderr
    log_path = log_dir / f"{selector}-{name}.log"
    log_path.write_text(output, encoding="utf-8")
    log_path.chmod(0o640)
    if expected_failure and result.returncode == 0:
        raise IntegrationTestError(
            f"{selector} {name} unexpectedly succeeded; full output: {log_path}"
        )
    if result.returncode and not expected_failure:
        detail = output.strip()[-8000:]
        raise IntegrationTestError(
            f"{selector} {name} failed with exit code {result.returncode}; "
            f"full output: {log_path}\n{detail}"
        )
    LOG.info(
        "%s %s filesystem step: %s",
        "Observed expected failure for" if expected_failure else "Passed",
        selector,
        name,
    )
    return output


def _ordered_worker_evidence(selector: str, sweep_output: str, result: Path) -> str:
    """Return substrate-appropriate durable worker-selection evidence."""
    if selector == "ssh":
        return sweep_output
    if selector != "slurm":
        raise IntegrationTestError(
            f"selector {selector!r} does not use legacy worker-log evidence"
        )
    logs = sorted((result / "executions").glob("*.log"))
    logs.extend(sorted(result.glob("coordinator-*.log")))
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace") for path in logs
    )


def _assert_ordered_workers(
    fixture: Fixture, selector: str, sweep_output: str, result: Path
) -> None:
    """Prove increasing cells use the configured workers in prefix order."""
    if selector == "ssh":
        workers = fixture.ssh_addresses
    elif selector == "slurm":
        workers = fixture.slurm_addresses
    else:
        raise IntegrationTestError(
            f"selector {selector!r} requires durable Kubernetes worker evidence"
        )
    expected = {
        "1": workers[0],
        "2": ",".join(workers),
    }
    selected: dict[str, str] = {}
    evidence = _ordered_worker_evidence(selector, sweep_output, result)
    for line in evidence.splitlines():
        match = re.search(r"starting execution \d+:.*nodes=(\d+).*hosts=([^ ]+)", line)
        if match:
            selected[match.group(1)] = match.group(2)
    if selected != expected:
        raise IntegrationTestError(
            f"{selector} ordered worker selection was {selected!r}; "
            f"expected {expected!r}"
        )


def _copy_result_for_reporting(
    runner: Any,
    config: Any,
    fixture: Fixture,
    selector: str,
    result_dir: str,
    destination: Path,
) -> Path:
    """Bring one completed result tree to the host for report validation."""
    destination.mkdir(parents=True, exist_ok=True)
    if selector == "ssh":
        shutil.copytree(result_dir, destination / Path(result_dir).name)
    elif selector == "slurm":
        if fixture.login_pod is None or fixture.login_container is None:
            raise IntegrationTestError("Slurm result copy requires a LoginSet pod")
        runner.run(
            [
                *_kubectl(config, "-n", config.namespace, "cp"),
                "-c",
                fixture.login_container,
                f"{fixture.login_pod}:{result_dir}",
                destination / Path(result_dir).name,
            ],
            timeout=120,
        )
    else:
        raise IntegrationTestError(
            f"selector {selector!r} publishes collected results locally"
        )
    return destination / Path(result_dir).name


def _login_shell(
    runner: Any,
    config: Any,
    fixture: Fixture,
    command: str,
    *,
    timeout: int = 60,
) -> str:
    """Run a storage-inspection command in the PVC-mounted login pod."""
    return runner.run(
        _pod_command(
            config,
            fixture.login_pod,
            fixture.login_container,
            command,
            as_user=WORKLOAD_USER,
            timeout=timeout,
        ),
        timeout=timeout + 15,
    ).stdout


def _prepare_scenario_data(
    runner: Any,
    config: Any,
    fixture: Fixture,
    data_root: str,
) -> None:
    """Reset one marker-owned scenario subtree on the shared test storage."""
    command = f"""
set -euo pipefail
umask 0007
rm -rf -- {_shell(data_root)}
mkdir -p -- {_shell(data_root + '/primary')} {_shell(data_root + '/secondary')}
printf 'scenario-owned\n' > {_shell(data_root + '/primary/.integration-sentinel')}
printf 'scenario-owned\n' > {_shell(data_root + '/secondary/.integration-sentinel')}
""".strip()
    _storage_shell(runner, config, fixture, command)


def _prepare_kubectl_scenario_data(
    runner: Any,
    config: Any,
    fixture: Fixture,
    data_root: str,
) -> None:
    """Reset one marker-owned scenario subtree through the PVC utility Pod."""
    command = f"""
set -euo pipefail
umask 0007
rm -rf -- {_shell(data_root)}
mkdir -p -- {_shell(data_root + '/primary')} {_shell(data_root + '/secondary')}
printf 'scenario-owned\n' > {_shell(data_root + '/primary/.integration-sentinel')}
printf 'scenario-owned\n' > {_shell(data_root + '/secondary/.integration-sentinel')}
""".strip()
    _storage_utility_shell(runner, config, fixture, command)


def _sync_step_runtime(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    step: ScenarioStep,
    template: Path,
    result_base: str,
) -> None:
    """Render and install one step's env and support files."""
    extra_env = step.render_env(runtime.values)
    if runtime.selector == "kubectl" and runtime.scenario.name == "failure-resume":
        # The product copies this integration-only executable into the control
        # bundle before creating the Job.  It fails exactly one reified cell;
        # the resume operation deliberately unsets the hook while selecting
        # only unfinished cells.
        target_ids = [
            execution_id
            for execution_id, execution in enumerate(step.executions)
            if execution.status is ExecutionStatus.FAILED
        ]
        if target_ids:
            target_id = f"{target_ids[0] + 1:04d}"
            overlay = Path(runtime.workspace) / "kubectl-failure-overlay.sh"
            overlay.write_text(
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f"if [[ ${{1:-}} == {target_id!r} "
                "&& ${4:-} == after-benchmark ]]; then\n"
                '  marker="$3/.integration-failure-injected"\n'
                '  if mkdir -- "$marker" 2>/dev/null; then\n'
                "    exit 97\n"
                "  fi\n"
                "fi\n",
                encoding="utf-8",
            )
            overlay.chmod(0o700)
            extra_env += (
                "\nexport STORAGE_SCALE_TEST_INTEGRATION=1\n"
                "export KUBECTL_INTEGRATION_FAILURE_OVERLAY="
                f"{_shell(overlay)}\n"
            )
    elif runtime.selector == "kubectl" and runtime.scenario.name in {
        "kubectl-cancel",
        "kubectl-coordinator-loss",
        "kubectl-endpoint-drift",
    }:
        # Hold the first cell at a deterministic RUNNING boundary. This avoids
        # racing a tiny benchmark while still exercising the real Job,
        # DaemonSet, PVC, and product lifecycle around the mutation.
        overlay = Path(runtime.workspace) / "kubectl-running-overlay.sh"
        overlay.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            "[[ ${4:-} == before-benchmark ]] || exit 0\n"
            "trap 'exit 143' INT TERM\n"
            "sleep 90\n",
            encoding="utf-8",
        )
        overlay.chmod(0o700)
        extra_env += (
            "\nexport STORAGE_SCALE_TEST_INTEGRATION=1\n"
            "export KUBECTL_INTEGRATION_FAILURE_OVERLAY="
            f"{_shell(overlay)}\n"
        )
    extra_support = {
        item.relative_path: item.content
        for item in step.render_support_files(runtime.values)
    }
    if runtime.selector == "ssh":
        _write_runtime_files(
            Path(runtime.workspace),
            runtime.selector,
            runtime.workspace,
            fixture,
            template=template,
            results_dir=result_base,
            logs_dir=f"{runtime.workspace}/logs/{step.name}",
            extra_env=extra_env,
            extra_support=extra_support,
            timeout_seconds=step.timeout_seconds,
        )
        return
    if runtime.selector == "kubectl":
        _write_runtime_files(
            Path(runtime.workspace),
            runtime.selector,
            runtime.workspace,
            fixture,
            template=template,
            results_dir=result_base,
            logs_dir=f"{runtime.workspace}/logs/{step.name}",
            extra_env=extra_env,
            extra_support=extra_support,
            timeout_seconds=step.timeout_seconds,
            logical_test_root=f"{runtime.data_root}/primary",
        )
        return
    if runtime.selector != "slurm":
        raise IntegrationTestError(
            f"unsupported integration selector: {runtime.selector}"
        )
    stage = runtime.local_workspace / f"runtime-{step.name}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir()
    _write_runtime_files(
        stage,
        runtime.selector,
        runtime.workspace,
        fixture,
        template=template,
        results_dir=result_base,
        logs_dir=f"{runtime.workspace}/logs/{step.name}",
        extra_env=extra_env,
        extra_support=extra_support,
        timeout_seconds=step.timeout_seconds,
    )
    archive_path = runtime.local_workspace / f"runtime-{step.name}.tar"
    with tarfile.open(archive_path, "w") as archive:
        for child in sorted(stage.rglob("*")):
            archive.add(child, arcname=child.relative_to(stage))
    _stream_to_login(
        runner,
        config,
        fixture,
        archive_path,
        [
            "bash",
            "-ec",
            "umask 0007; tar --no-same-owner --no-same-permissions "
            f"-xf - -C {_shell(runtime.workspace)}",
        ],
        as_user=WORKLOAD_USER,
    )


def _create_generated_inputs(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    step: ScenarioStep,
) -> None:
    """Create bounded scenario inputs through the shared PVC mount."""
    for generated in step.generated_inputs:
        path = f"{runtime.values['test_root']}/{generated.relative_path}"
        storage_path = (
            f"{KUBECTL_STORAGE_MOUNT}/{path}" if runtime.selector == "kubectl" else path
        )
        command = f"""
set -euo pipefail
umask 0007
mkdir -p -- {_shell(str(PurePosixPath(storage_path).parent))}
truncate -s {generated.size_bytes} -- {_shell(storage_path)}
test "$(stat -c %s -- {_shell(storage_path)})" -eq {generated.size_bytes}
""".strip()
        _storage_shell(runner, config, fixture, command)


def _discover_result(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    step: ScenarioStep,
    result_base: str,
    log_dir: Path,
) -> str:
    """Find the single normal sweep result below an invocation-owned base."""
    output = _run_step(
        runner,
        config,
        fixture,
        runtime.selector,
        f"{step.name}-discover",
        f"find {_shell(result_base)} -mindepth 1 -maxdepth 1 -type d "
        "-name 'elbencho-*' -printf '%p\\n'",
        log_dir,
        30,
    )
    directories = [line for line in output.splitlines() if line.strip()]
    if len(directories) != 1:
        raise IntegrationTestError(
            f"{runtime.selector} {step.name} produced {len(directories)} "
            f"normal result directories under {result_base}: {directories}"
        )
    return directories[0]


def _copy_scenario_result(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    step: ScenarioStep,
    remote_result: str,
) -> Path:
    """Copy one immutable result snapshot into the retained test-run log."""
    destination = runtime.artifact_root / "copied-results" / step.name
    destination.mkdir(parents=True, exist_ok=True)
    if runtime.selector == "ssh":
        target = destination / Path(remote_result).name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(remote_result, target)
    elif runtime.selector == "slurm":
        target = _copy_result_for_reporting(
            runner,
            config,
            fixture,
            runtime.selector,
            remote_result,
            destination,
        )
    else:
        raise IntegrationTestError(
            f"selector {runtime.selector!r} publishes collected results locally"
        )
    runtime.copied_results.append(target)
    return target


def _shell_assignments(path: Path) -> dict[str, str]:
    """Read simple exported scalar assignments from a reified execution."""
    assignments: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"export ([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not match:
            continue
        values = shlex.split(match.group(2), posix=True)
        assignments[match.group(1)] = values[0] if values else ""
    return assignments


def _coordinate_from_execution(path: Path) -> tuple[int, str, int, int]:
    """Return a reified execution's stable sweep coordinate."""
    values = _shell_assignments(path)
    try:
        return (
            int(values["nodes"]),
            values["io_size"],
            int(values["thread_count"]),
            int(values["io_depth"]),
        )
    except (KeyError, ValueError) as error:
        raise IntegrationTestError(f"invalid execution metadata in {path}") from error


def _workload_values(path: Path) -> dict[str, str]:
    """Load a workload TSV while rejecting duplicate or malformed keys."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or fields[0] in values:
            raise IntegrationTestError(f"malformed workload metadata: {path}")
        values[fields[0]] = fields[1]
    return values


def _expected_dataset_totals(
    scenario: str, step: ScenarioStep, nodes: int
) -> tuple[int, int] | None:
    """Return exact bounded totals for workloads with a metadata contract."""
    if scenario in {"baseline", "ssh-shared-home", "failure-resume"}:
        return nodes, nodes * 16 * 1024 * 1024
    if scenario == "live-capture":
        return nodes * 2, nodes * (MAX_LIVE_CAPTURE_DATASET_BYTES // 2)
    if scenario == "slurm-cartesian":
        return nodes * 2, nodes * 2 * 1024 * 1024
    if (
        scenario in {"retained-lifecycle", "kubectl-retained-read"}
        and step.kind is not CommandKind.DELETE
    ):
        return 1, 16 * 1024 * 1024
    return None


def _assert_phase_artifacts(
    execution_root: Path,
    execution_id: str,
    step: ScenarioStep,
    status: ExecutionStatus,
) -> None:
    """Require phase evidence without freezing complete native arguments."""
    phase_files = {
        WorkloadPhase.WRITE: "write.json",
        WorkloadPhase.READ: "read.json",
        WorkloadPhase.REMOVE_FILES: "delete.json",
    }
    for phase, suffix in phase_files.items():
        if phase not in step.required_phases:
            continue
        if status is ExecutionStatus.FAILED and phase is WorkloadPhase.REMOVE_FILES:
            continue
        path = execution_root / f"{execution_id}.{suffix}"
        if not path.is_file() or path.stat().st_size == 0:
            raise IntegrationTestError(f"missing {phase.value} evidence: {path}")


def _assert_execution_contract(
    scenario: FilesystemScenarioSpec,
    step: ScenarioStep,
    result: Path,
) -> None:
    """Validate exact coordinates, states, totals, and required phases."""
    execution_root = result / "executions"
    scripts = sorted(execution_root.glob("[0-9][0-9][0-9][0-9].sh"))
    if len(scripts) != len(step.executions):
        raise IntegrationTestError(
            f"{scenario.name}/{step.name}: expected {len(step.executions)} "
            f"executions, found {len(scripts)}"
        )
    actual_coordinates = [_coordinate_from_execution(path) for path in scripts]
    expected_coordinates = [
        (
            item.coordinate.nodes,
            item.coordinate.io_size,
            item.coordinate.threads,
            item.coordinate.io_depth,
        )
        for item in step.executions
    ]
    if actual_coordinates != expected_coordinates:
        raise IntegrationTestError(
            f"{scenario.name}/{step.name}: coordinates {actual_coordinates!r} "
            f"do not match {expected_coordinates!r}"
        )
    for index, expected in enumerate(step.executions, start=1):
        execution_id = f"{index:04d}"
        status_path = execution_root / f"{execution_id}.status"
        status = status_path.read_text(encoding="utf-8").strip()
        if status != expected.status.value:
            raise IntegrationTestError(
                f"{scenario.name}/{step.name}/{execution_id}: expected "
                f"{expected.status.value}, found {status}"
            )
        if expected.status is ExecutionStatus.PENDING:
            continue
        exitcode = (
            (execution_root / f"{execution_id}.exitcode")
            .read_text(encoding="utf-8")
            .strip()
        )
        expected_exit = "97" if expected.status is ExecutionStatus.FAILED else "0"
        if exitcode != expected_exit:
            raise IntegrationTestError(
                f"{scenario.name}/{step.name}/{execution_id}: expected exit "
                f"{expected_exit}, found {exitcode}"
            )
        if scenario.name not in {
            "default-dio",
            "ssh-single-big-file",
            "ssh-weighted-roots",
        } and not (
            (
                scenario.name == "retained-lifecycle"
                and step.name.startswith("read-cache-")
            )
            or (scenario.name == "kubectl-retained-read" and step.name == "read-from")
        ):
            _assert_phase_artifacts(execution_root, execution_id, step, expected.status)
        workload_path = execution_root / f"{execution_id}.workload.tsv"
        totals = _expected_dataset_totals(
            scenario.name, step, expected.coordinate.nodes
        )
        if totals is not None:
            if not workload_path.is_file():
                raise IntegrationTestError(
                    f"{scenario.name}/{step.name}/{execution_id}: missing "
                    f"required workload metadata {workload_path}"
                )
            workload = _workload_values(workload_path)
            if workload.get("dataset_files_total") != str(totals[0]) or workload.get(
                "dataset_bytes_total"
            ) != str(totals[1]):
                raise IntegrationTestError(
                    f"{scenario.name}/{step.name}/{execution_id}: invalid "
                    f"dataset totals in {workload_path}"
                )
            valid_completion = {"completed"}
            if (
                scenario.name == "retained-lifecycle"
                and step.name.startswith("read-cache-")
            ) or (
                scenario.name == "kubectl-retained-read" and step.name == "read-from"
            ):
                valid_completion.add("not_applicable_time_based")
            if (
                expected.status is ExecutionStatus.SUCCESS
                and workload.get("completion_state") not in valid_completion
            ):
                raise IntegrationTestError(
                    f"{scenario.name}/{step.name}/{execution_id}: workload did "
                    "not complete"
                )


def _execution_targets(result: Path) -> tuple[str, ...]:
    """Return every exact generated target recorded by a result tree."""
    targets: list[str] = []
    for script in sorted((result / "executions").glob("[0-9][0-9][0-9][0-9].sh")):
        value = _shell_assignments(script).get(
            "ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV", ""
        )
        targets.extend(item for item in value.split(",") if item)
    return tuple(targets)


def _assert_dataset_state(
    runner: Any,
    config: Any,
    fixture: Fixture,
    step: ScenarioStep,
    result: Path | None,
    retained_path: str | None,
    storage_prefix: str = "",
    storage_node: str | None = None,
) -> None:
    """Validate cleanup or retention only within scenario-owned paths."""

    def storage_path(path: str) -> str:
        if storage_prefix and not PurePosixPath(path).is_absolute():
            return f"{storage_prefix}/{path}"
        return path

    if step.dataset is DatasetExpectation.PRESERVED:
        if retained_path:
            retained_storage_path = storage_path(retained_path)
            retained_parent = str(PurePosixPath(retained_storage_path).parent)
            _storage_shell(
                runner,
                config,
                fixture,
                "for _ in $(seq 1 30); do "
                f"test -e {_shell(retained_storage_path)} && exit 0; sleep 1; done; "
                "{ "
                f"echo 'missing retained dataset: ' {_shell(retained_storage_path)} >&2; "
                f"find {_shell(retained_parent)} -maxdepth 2 -printf '%y %p %s\\n' "
                "2>&1 >&2 || true; exit 1; }",
                node_name=storage_node,
            )
        return
    paths = list(_execution_targets(result)) if result is not None else []
    if step.dataset is DatasetExpectation.REMOVED and retained_path:
        paths.append(retained_path)
    if not paths:
        return
    probes = "\n".join(f"test ! -e {_shell(storage_path(path))}" for path in paths)
    _storage_shell(runner, config, fixture, f"set -euo pipefail\n{probes}")


def _assert_semantic_flags(scenario: str, step: ScenarioStep, result: Path) -> None:
    """Check required command semantics while allowing future optional flags."""
    command_lines: list[str] = []
    for path in result.glob("executions/*.log"):
        command_lines.extend(
            line
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.startswith("# elbencho ")
        )
    # Kubernetes publishes Elbencho's own result files rather than a
    # transport-specific coordinator log. Its COMMAND LINE record is native
    # evidence of the same effective arguments and remains useful if the
    # shared runner's human-facing log wording changes.
    if not command_lines:
        for path in result.glob("*.out"):
            command_lines.extend(
                line
                for line in path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
                if line.startswith("COMMAND LINE:")
            )
    evidence = "\n".join(command_lines)
    if not evidence:
        raise IntegrationTestError(
            f"{scenario}/{step.name}: execution logs omitted native commands"
        )
    required: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()
    if scenario == "baseline" or scenario == "ssh-shared-home":
        required = ("--norandalign",)
    elif scenario == "default-dio":
        required, forbidden = ("--direct",), ("--norandalign",)
    elif scenario == "live-capture":
        required = ("--livecsv", "--livecsvex", "--liveint=10")
    elif scenario == "ssh-single-big-file" and step.name == "inferred-extent-read":
        required, forbidden = ("--nosvcshare", "--read"), (
            "--size",
            "--treescan",
            "--treefile",
        )
    elif scenario == "ssh-weighted-roots":
        required = ("--files=1",)
    missing = [flag for flag in required if flag not in evidence]
    present = [
        flag
        for flag in forbidden
        if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(flag)}(?:=|\b)", evidence)
    ]
    if missing or present:
        raise IntegrationTestError(
            f"{scenario}/{step.name}: native command flag mismatch; "
            f"missing={missing}, forbidden-present={present}"
        )


def _assert_scenario_report(
    runner: Any,
    report_workspace: Path,
    result: Path,
    runtime: ScenarioRuntime,
    step: ScenarioStep,
    log_dir: Path,
) -> None:
    """Exercise reporting and require semantic rows and plot families."""
    report_dir = log_dir / f"report-{step.name}"
    report_dir.mkdir()
    command: list[str | Path] = [
        report_workspace / "utils" / "extract-elbencho.sh",
        "--markdown",
    ]
    if runtime.scenario.name == "live-capture":
        command.extend(
            (
                "--per-client-plots",
                "--client-min-underperform-segments",
                "2",
                "--client-max-timeseries-lines",
                "2",
                "--client-max-heatmap-rows",
                "2",
            )
        )
    command.append(result)
    completed = runner.run(
        command, cwd=report_dir, timeout=step.timeout_seconds, check=False
    )
    output = completed.stdout + completed.stderr
    log_path = report_dir / "extract-elbencho.log"
    log_path.write_text(output, encoding="utf-8")
    if completed.returncode:
        raise IntegrationTestError(
            f"{runtime.scenario.name}/{runtime.selector}/{step.name}: reporting "
            f"failed; full output: {log_path}\n{output[-8000:]}"
        )
    node_counts = sorted({item.coordinate.nodes for item in step.executions})
    missing_rows = [
        nodes
        for nodes in node_counts
        if not re.search(rf"^\|\s*{nodes}\s*\|", output, re.MULTILINE)
    ]
    required_operations = []
    if WorkloadPhase.WRITE in step.required_phases:
        required_operations.append("WRITE Operation")
    if WorkloadPhase.READ in step.required_phases:
        required_operations.append("READ Operation")
    missing_operations = [item for item in required_operations if item not in output]
    if missing_rows or missing_operations:
        raise IntegrationTestError(
            f"{runtime.scenario.name}/{runtime.selector}/{step.name}: report "
            f"missing node rows {missing_rows} or operations {missing_operations}"
        )
    if not any(path.stat().st_size for path in result.glob("*.png")):
        raise IntegrationTestError(
            f"{runtime.scenario.name}/{runtime.selector}/{step.name}: no "
            "nonempty report plot was generated"
        )


def _reset_result_base(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    result_base: str,
) -> None:
    """Create an empty invocation-owned result base."""
    if runtime.selector == "ssh":
        path = Path(result_base)
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        return
    if runtime.selector == "kubectl":
        path = Path(result_base)
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
        return
    _login_shell(
        runner,
        config,
        fixture,
        f"umask 0007; rm -rf -- {_shell(result_base)} && "
        f"mkdir -p -- {_shell(result_base)}",
    )


def _validate_step_environment(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    step: ScenarioStep,
    log_dir: Path,
) -> None:
    """Run the repository's validator for a rendered scenario environment."""
    if runtime.selector == "kubectl":
        result = runner.run(
            [
                "env",
                f"KUBECONFIG={config.kubeconfig}",
                Path(runtime.workspace) / "validate_env.sh",
            ],
            cwd=Path(runtime.workspace),
            check=False,
            timeout=180,
        )
        output = result.stdout + result.stderr
        path = log_dir / f"kubectl-{step.name}-validate-env.log"
        path.write_text(output, encoding="utf-8")
        if result.returncode or VALIDATION_SUCCESS not in output:
            raise IntegrationTestError(
                f"{runtime.scenario.name}/{runtime.selector}/{step.name}: "
                f"environment validation failed; full output: {path}"
            )
        return
    output = _run_step(
        runner,
        config,
        fixture,
        runtime.selector,
        f"{step.name}-validate-env",
        f"cd -- {_shell(runtime.workspace)} && ./validate_env.sh",
        log_dir,
        180,
    )
    if VALIDATION_SUCCESS not in output:
        raise IntegrationTestError(
            f"{runtime.scenario.name}/{runtime.selector}/{step.name}: "
            "validate_env.sh omitted its success marker"
        )


def _retained_path(result: Path) -> str:
    """Return the sole generated path from a one-execution retained result."""
    targets = _execution_targets(result)
    if len(targets) != 1:
        raise IntegrationTestError(
            f"expected one retained generated target, found {targets!r}"
        )
    return targets[0]


def _kubectl_execution_node(fixture: Fixture, result: Path) -> str:
    """Return the selected worker node recorded for a one-cell attempt."""
    workers = result / "executions" / "0001.workers.tsv"
    lines = workers.read_text(encoding="utf-8").splitlines()
    if len(lines) != 1:
        raise IntegrationTestError(
            f"expected one selected Kubernetes worker in {workers}, found {lines!r}"
        )
    fields = lines[0].split("\t")
    node, pod, uid, address = fields if len(fields) == 4 else ("", "", "", "")
    try:
        valid_address = ipaddress.ip_address(address).version == 4
    except ValueError:
        valid_address = False
    if (
        node not in fixture.kubectl_nodes
        or KUBERNETES_DNS_NAME.fullmatch(node) is None
        or KUBERNETES_DNS_NAME.fullmatch(pod) is None
        or KUBERNETES_UID.fullmatch(uid) is None
        or not valid_address
    ):
        raise IntegrationTestError(f"malformed Kubernetes worker evidence: {workers}")
    return node


def _kubectl_logical_path(path: str) -> str:
    """Convert a collected container path back to the public logical path."""
    prefix = f"{KUBECTL_STORAGE_MOUNT}/"
    if path.startswith(prefix):
        return path.removeprefix(prefix)
    logical = PurePosixPath(path)
    if (
        logical.is_absolute()
        or not path
        or any(part in {"", ".", ".."} for part in logical.parts)
    ):
        raise IntegrationTestError(
            f"Kubernetes retained path is not on the PVC: {path}"
        )
    return path


def _assert_read_cache(step: ScenarioStep, result: Path) -> None:
    """Validate the cache miss/hit contract for retained read invocations."""
    if step.name not in {"read-cache-miss", "read-cache-hit"}:
        return
    workload = _workload_values(result / "executions" / "0001.workload.tsv")
    expected = (
        ("cache_miss_scan", "created")
        if step.name == "read-cache-miss"
        else ("cache_hit", "reused")
    )
    actual = (
        workload.get("treefile_source"),
        workload.get("treefile_cache_publish_outcome"),
    )
    if actual != expected:
        raise IntegrationTestError(
            f"{step.name}: expected treefile cache {expected!r}, found {actual!r}"
        )


def _assert_live_capture(result: Path) -> None:
    """Require a stable live counter series without comparing performance."""
    files = [path for path in result.rglob("*.live.csv") if path.stat().st_size]
    if not files:
        raise IntegrationTestError(f"live-capture result has no live CSV: {result}")
    stable_series = False
    for path in files:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) < 3:
            continue
        header = lines[0]
        if "DoneBytes" not in header:
            raise IntegrationTestError(f"live CSV lacks DoneBytes column: {path}")
        stable_series = True
    if not stable_series:
        raise IntegrationTestError(
            "live capture has no service/phase series with at least two samples"
        )


def _assert_slurm_scheduling(
    runner: Any,
    config: Any,
    fixture: Fixture,
    result: Path,
) -> None:
    """Verify the real allocation's name, node constraint, CPUs, and state."""
    job_id = (result / "executions" / "0001.jobid").read_text(encoding="utf-8").strip()
    command = (
        f"sacct -X -j {_shell(job_id)} -n -P "
        "--format=JobName,NodeList,AllocCPUS,State"
    )
    output = _login_shell(runner, config, fixture, command)
    records = [line.split("|") for line in output.splitlines() if line.strip()]
    matches = [record for record in records if len(record) >= 4 and record[0]]
    if not matches:
        raise IntegrationTestError(f"Slurm accounting omitted job {job_id}")
    name, nodes, cpus, state = matches[0][:4]
    if (
        not name.startswith("itest-elbencho-")
        or fixture.slurm_nodes[0] not in nodes
        or fixture.slurm_nodes[1] in nodes
        or not cpus.isdigit()
        or int(cpus) < 1
        or not state.startswith("COMPLETED")
    ):
        raise IntegrationTestError(
            f"unexpected Slurm scheduling evidence for {job_id}: {matches[0]!r}"
        )


def _assert_step_specials(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    step: ScenarioStep,
    result: Path,
) -> None:
    """Apply focused assertions unique to individual real scenarios."""
    _assert_read_cache(step, result)
    if runtime.scenario.name == "live-capture":
        _assert_live_capture(result)
    if runtime.scenario.name == "slurm-scheduling":
        _assert_slurm_scheduling(runner, config, fixture, result)
    if runtime.scenario.name == "ssh-single-big-file" and step.name == (
        "inferred-extent-read"
    ):
        path = f"{runtime.values['test_root']}/staged-input/integration-bigfile"
        output = _login_shell(
            runner,
            config,
            fixture,
            f"stat -c %s -- {_shell(path)} && sha256sum -- {_shell(path)}",
        )
        if not output.startswith(f"{16 * 1024 * 1024}\n"):
            raise IntegrationTestError("staged single-file input changed size")


def _run_regular_step(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    step: ScenarioStep,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
) -> StepOutcome:
    """Render, execute, collect, and validate one ordinary scenario step."""
    if step.kind is CommandKind.RESUME:
        raise IntegrationTestError("resume steps require the failure scenario runner")
    result_base = f"{runtime.workspace}/results/{step.name}"
    _reset_result_base(runner, config, fixture, runtime, result_base)
    _sync_step_runtime(
        runner,
        config,
        fixture,
        runtime,
        step,
        template,
        result_base,
    )
    _create_generated_inputs(runner, config, fixture, runtime, step)
    _validate_step_environment(runner, config, fixture, runtime, step, log_dir)
    arguments = shlex.join(step.render_arguments(runtime.values))
    command = (
        f"cd -- {_shell(runtime.workspace)} && "
        f"./storage-tests/fs/nv-elbencho-sweep.sh {arguments}"
    )
    output = _run_step(
        runner,
        config,
        fixture,
        runtime.selector,
        step.name,
        command,
        log_dir,
        step.timeout_seconds,
    )
    if step.kind is CommandKind.DELETE:
        _assert_dataset_state(
            runner,
            config,
            fixture,
            step,
            None,
            runtime.values.get("retained_data_dir")
            or f"{runtime.values['test_root']}/staged-input",
        )
        return StepOutcome(output, None, None)
    remote_result = _discover_result(
        runner,
        config,
        fixture,
        runtime,
        step,
        result_base,
        log_dir,
    )
    local_result = _copy_scenario_result(
        runner, config, fixture, runtime, step, remote_result
    )
    _assert_execution_contract(runtime.scenario, step, local_result)
    _assert_semantic_flags(runtime.scenario.name, step, local_result)
    if "retained_data_dir" in step.exports:
        runtime.values["retained_data_dir"] = _retained_path(local_result)
    _assert_dataset_state(
        runner,
        config,
        fixture,
        step,
        local_result,
        runtime.values.get("retained_data_dir"),
    )
    _assert_step_specials(runner, config, fixture, runtime, step, local_result)
    _assert_scenario_report(
        runner,
        report_workspace,
        local_result,
        runtime,
        step,
        log_dir,
    )
    if {item.coordinate.nodes for item in step.executions} == {1, 2}:
        _assert_ordered_workers(fixture, runtime.selector, output, local_result)
    return StepOutcome(output, remote_result, local_result)


def _make_scenario_runtime(
    fixture: Fixture,
    scenario: FilesystemScenarioSpec,
    selector: str,
    build_root: Path,
    artifact_root: Path,
    run_id: str,
) -> ScenarioRuntime:
    """Create cleanup-capable scenario identity before remote mutation."""
    workspace_id = f"{run_id}-{scenario.name}-{selector}"
    if selector == "ssh":
        workspace = str(build_root / "ssh" / "storage-scale-test")
    elif selector == "kubectl":
        workspace = str(build_root / "kubectl" / "storage-scale-test")
    elif selector == "slurm":
        workspace = f"{REMOTE_BASE}/workspaces/{workspace_id}/storage-scale-test"
    else:
        raise IntegrationTestError(f"unsupported integration selector: {selector}")
    data_root = (
        f"integration-regression/{workspace_id}"
        if selector == "kubectl"
        else f"{REMOTE_BASE}/test-data/{workspace_id}"
    )
    values = {
        "workspace": workspace,
        "test_root": f"{data_root}/primary",
        "test_root_secondary": f"{data_root}/secondary",
        "slurm_node_1": fixture.slurm_nodes[0] if fixture.slurm_nodes else "",
        "slurm_node_2": fixture.slurm_nodes[1] if fixture.slurm_nodes else "",
    }
    return ScenarioRuntime(
        scenario,
        selector,
        workspace,
        build_root,
        artifact_root,
        data_root,
        values,
        [],
    )


def _prepare_scenario_runtime(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    archive: Path,
    extracted: Path,
) -> None:
    """Stage deployment and data after cleanup identity exists."""
    workspace_id = PurePosixPath(runtime.data_root).name
    workspace = _stage_workspace(
        runner,
        config,
        fixture,
        runtime.selector,
        archive,
        extracted,
        runtime.local_workspace,
        workspace_id,
    )
    if workspace != runtime.workspace:
        raise IntegrationTestError(
            f"scenario workspace drifted: expected {runtime.workspace}, found {workspace}"
        )
    if runtime.selector == "kubectl":
        _prepare_kubectl_scenario_data(
            runner,
            config,
            fixture,
            f"{KUBECTL_STORAGE_MOUNT}/{runtime.data_root}",
        )
    else:
        _prepare_scenario_data(runner, config, fixture, runtime.data_root)


def _run_regular_scenario(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
) -> None:
    """Run all ordered steps for a non-failure scenario."""
    for step in runtime.scenario.steps:
        _run_regular_step(
            runner,
            config,
            fixture,
            runtime,
            step,
            template,
            report_workspace,
            log_dir,
        )


def _run_kubectl_command(
    runner: Any,
    config: Any,
    runtime: ScenarioRuntime,
    name: str,
    arguments: tuple[str, ...],
    log_dir: Path,
    timeout: int,
    *,
    expected_failure: bool = False,
    accepted_states: frozenset[str] = frozenset(),
) -> str:
    """Run one local kubectl-sweep command with the fixture kubeconfig."""
    command: list[str | Path] = [
        "env",
        f"KUBECONFIG={config.kubeconfig}",
        "timeout",
        "--kill-after=15s",
        f"{timeout}s",
        Path(runtime.workspace) / "storage-tests" / "fs" / "nv-elbencho-sweep.sh",
        *arguments,
    ]
    LOG.info("Running kubectl filesystem step: %s", name)
    result = runner.run(
        command,
        cwd=Path(runtime.workspace),
        check=False,
        timeout=timeout + 30,
    )
    output = result.stdout + result.stderr
    log_path = log_dir / f"kubectl-{name}.log"
    log_path.write_text(output, encoding="utf-8")
    log_path.chmod(0o640)
    if expected_failure and result.returncode == 0:
        raise IntegrationTestError(
            f"kubectl {name} unexpectedly succeeded; full output: {log_path}"
        )
    if result.returncode and not expected_failure:
        if accepted_states:
            try:
                if _kubectl_lifecycle_state(output) in accepted_states:
                    return output
            except IntegrationTestError:
                pass
        raise IntegrationTestError(
            f"kubectl {name} failed with exit code {result.returncode}; full "
            f"output: {log_path}\n{output.strip()[-8000:]}"
        )
    return output


def _kubectl_output_value(output: str, key: str) -> str:
    """Read one exact stable lifecycle key from product command output."""
    values = [
        line.removeprefix(f"{key}=")
        for line in output.splitlines()
        if line.startswith(f"{key}=")
    ]
    if (
        len(values) != 1
        or not values[0]
        or any(char in values[0] for char in "\r\n\x00")
    ):
        raise IntegrationTestError(f"kubectl lifecycle output lacks one safe {key}")
    return values[0]


def _kubectl_submission(output: str) -> tuple[Path, str]:
    """Return the local results root and attempt ID from a successful submit."""
    result = Path(_kubectl_output_value(output, "STORAGE_SCALE_TEST_RESULTS_DIR"))
    attempt_id = _kubectl_output_value(output, "STORAGE_SCALE_TEST_ATTEMPT_ID")
    if not attempt_id or not re.fullmatch(r"[0-9a-f]{8}", attempt_id):
        raise IntegrationTestError(f"invalid kubectl attempt identity: {attempt_id!r}")
    return result, attempt_id


def _kubectl_lifecycle_state(output: str) -> str:
    """Read and validate one stable lifecycle state from status output."""
    state = _kubectl_output_value(output, "STORAGE_SCALE_TEST_KUBECTL_STATE")
    if state not in KUBECTL_STATUS_STATES:
        raise IntegrationTestError(f"invalid kubectl lifecycle state: {state!r}")
    return state


def _wait_for_kubectl_terminal_state(
    runner: Any,
    config: Any,
    runtime: ScenarioRuntime,
    result_root: Path,
    log_dir: Path,
    timeout: int,
) -> str:
    """Poll the product status command until one documented terminal state."""
    deadline = time.monotonic() + timeout
    last_command_error: IntegrationTestError | None = None
    while time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))
        try:
            output = _run_kubectl_command(
                runner,
                config,
                runtime,
                "status",
                ("--status", str(result_root)),
                log_dir,
                min(120, remaining),
            )
        except IntegrationTestError as error:
            last_command_error = error
            LOG.warning("Transient kubectl status failure; retrying: %s", error)
            time.sleep(min(2, max(0, deadline - time.monotonic())))
            continue
        state = _kubectl_lifecycle_state(output)
        if state in KUBECTL_TERMINAL_STATES:
            return state
        time.sleep(2)
    detail = f"; last status error: {last_command_error}" if last_command_error else ""
    raise IntegrationTestError(
        f"kubectl attempt at {result_root} did not reach a terminal state within "
        f"{timeout} seconds{detail}"
    )


def _collected_result_root(result_root: Path) -> Path:
    """Find the sole collected result without relying on timestamp spelling."""
    if (result_root / "executions").is_dir():
        return result_root
    candidates = (
        sorted(
            path
            for path in result_root.iterdir()
            if path.is_dir() and (path / "executions").is_dir()
        )
        if result_root.is_dir()
        else []
    )
    if len(candidates) != 1:
        raise IntegrationTestError(
            f"kubectl collection produced {len(candidates)} result trees under "
            f"{result_root}"
        )
    return candidates[0]


def _assert_kubectl_ordered_workers(fixture: Fixture, result: Path) -> None:
    """Validate direct Pod-IP worker evidence rather than controller log prose."""
    execution_root = result / "executions"
    two_node = execution_root / "0002.workers.tsv"
    if not two_node.is_file():
        raise IntegrationTestError(
            f"missing durable Kubernetes worker evidence: {two_node}"
        )
    rows = [
        line.split("\t") for line in two_node.read_text(encoding="utf-8").splitlines()
    ]
    if len(rows) != 2 or any(len(row) != 4 for row in rows):
        raise IntegrationTestError(f"invalid Kubernetes worker evidence: {two_node}")
    nodes = tuple(row[0] for row in rows)
    addresses = tuple(row[3] for row in rows)
    expected_nodes = tuple(sorted(fixture.kubectl_nodes))
    try:
        addresses_are_ipv4 = all(
            ipaddress.ip_address(address).version == 4 for address in addresses
        )
    except ValueError:
        addresses_are_ipv4 = False
    if nodes != expected_nodes or not addresses_are_ipv4:
        raise IntegrationTestError(
            f"Kubernetes ordered worker evidence was nodes={nodes}, addresses={addresses}; "
            f"expected nodes={expected_nodes}"
        )


def _run_kubectl_baseline(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
) -> None:
    """Exercise submit/status/collect through the public kubectl lifecycle CLI."""
    if runtime.scenario.name != "baseline":
        raise IntegrationTestError(
            f"kubectl integration adapter has no scenario implementation for "
            f"{runtime.scenario.name}"
        )
    step = runtime.scenario.steps[0]
    result_base = str(Path(runtime.workspace) / "results" / step.name)
    _reset_result_base(runner, config, fixture, runtime, result_base)
    _sync_step_runtime(runner, config, fixture, runtime, step, template, result_base)
    _validate_step_environment(runner, config, fixture, runtime, step, log_dir)
    submission = _run_kubectl_command(
        runner,
        config,
        runtime,
        step.name,
        step.render_arguments(runtime.values),
        log_dir,
        step.timeout_seconds,
    )
    result_root, attempt_id = _kubectl_submission(submission)
    runtime.values["kubectl_result_root"] = str(result_root)
    LOG.info("Submitted Kubernetes baseline attempt %s", attempt_id)
    terminal = _wait_for_kubectl_terminal_state(
        runner, config, runtime, result_root, log_dir, step.timeout_seconds
    )
    if terminal != "SUCCESS":
        raise IntegrationTestError(f"kubectl baseline ended in {terminal}")
    _run_kubectl_command(
        runner,
        config,
        runtime,
        "collect",
        ("--collect", str(result_root)),
        log_dir,
        180,
    )
    runtime.values["kubectl_collected"] = "1"
    result = _collected_result_root(result_root)
    _assert_execution_contract(runtime.scenario, step, result)
    _assert_semantic_flags(runtime.scenario.name, step, result)
    _assert_kubectl_ordered_workers(fixture, result)
    _assert_dataset_state(
        runner, config, fixture, step, result, None, KUBECTL_STORAGE_MOUNT
    )
    _assert_scenario_report(runner, report_workspace, result, runtime, step, log_dir)


def _kubectl_collect_result(
    runner: Any,
    config: Any,
    runtime: ScenarioRuntime,
    result_root: Path,
    log_dir: Path,
    *,
    terminal_states: frozenset[str] = frozenset(),
) -> Path:
    """Collect one terminal attempt and return its published result tree."""
    _run_kubectl_command(
        runner,
        config,
        runtime,
        "collect",
        ("--collect", str(result_root)),
        log_dir,
        180,
        accepted_states=terminal_states,
    )
    return _collected_result_root(result_root)


def _kubectl_run_step(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    step: ScenarioStep,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
) -> Path | None:
    """Run one ordinary Kubernetes step through submit/status/collect."""
    if step.kind is CommandKind.DELETE:
        raise IntegrationTestError(
            f"Kubernetes adapter does not execute delete-only step {step.name}"
        )
    result_base = str(Path(runtime.workspace) / "results" / step.name)
    _reset_result_base(runner, config, fixture, runtime, result_base)
    _sync_step_runtime(runner, config, fixture, runtime, step, template, result_base)
    _create_generated_inputs(runner, config, fixture, runtime, step)
    _validate_step_environment(runner, config, fixture, runtime, step, log_dir)
    output = _run_kubectl_command(
        runner,
        config,
        runtime,
        step.name,
        step.render_arguments(runtime.values),
        log_dir,
        step.timeout_seconds,
    )
    result_root, attempt_id = _kubectl_submission(output)
    runtime.values["kubectl_result_root"] = str(result_root)
    LOG.info("Submitted Kubernetes %s attempt %s", step.name, attempt_id)
    terminal = _wait_for_kubectl_terminal_state(
        runner, config, runtime, result_root, log_dir, step.timeout_seconds
    )
    if terminal != "SUCCESS":
        raise IntegrationTestError(f"kubectl {step.name} ended in {terminal}")
    result = _kubectl_collect_result(runner, config, runtime, result_root, log_dir)
    runtime.values["kubectl_collected"] = "1"
    _assert_execution_contract(runtime.scenario, step, result)
    _assert_semantic_flags(runtime.scenario.name, step, result)
    _assert_dataset_state(
        runner, config, fixture, step, result, None, KUBECTL_STORAGE_MOUNT
    )
    _assert_step_specials(runner, config, fixture, runtime, step, result)
    if {item.coordinate.nodes for item in step.executions} == {1, 2}:
        _assert_kubectl_ordered_workers(fixture, result)
    if "retained_data_dir" in step.exports:
        retained = _retained_path(result)
        runtime.values["retained_data_dir"] = _kubectl_logical_path(retained)
        _assert_dataset_state(
            runner,
            config,
            fixture,
            step,
            result,
            runtime.values["retained_data_dir"],
            KUBECTL_STORAGE_MOUNT,
            _kubectl_execution_node(fixture, result),
        )
    _assert_scenario_report(runner, report_workspace, result, runtime, step, log_dir)
    return result


def _run_kubectl_success_scenario(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
) -> None:
    """Run successful Kubernetes steps while retaining shared assertions."""
    for step in runtime.scenario.steps:
        _kubectl_run_step(
            runner,
            config,
            fixture,
            runtime,
            step,
            template,
            report_workspace,
            log_dir,
        )


def _kubectl_wait_for_state(
    runner: Any,
    config: Any,
    runtime: ScenarioRuntime,
    result_root: Path,
    log_dir: Path,
    expected: frozenset[str],
    timeout: int,
) -> str:
    """Poll status until one of the requested stable states is observed."""
    deadline = time.monotonic() + timeout
    observed = ""
    last_command_error: IntegrationTestError | None = None
    while time.monotonic() < deadline:
        remaining = max(1, int(deadline - time.monotonic()))
        try:
            output = _run_kubectl_command(
                runner,
                config,
                runtime,
                "status",
                ("--status", str(result_root)),
                log_dir,
                min(120, remaining),
            )
            observed = _kubectl_lifecycle_state(output)
            if observed in expected:
                return observed
            if observed in KUBECTL_TERMINAL_STATES:
                raise IntegrationTestError(
                    f"kubectl attempt reached unexpected terminal state {observed}"
                )
        except IntegrationTestError as error:
            last_command_error = error
            if observed in KUBECTL_TERMINAL_STATES:
                raise
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    detail = f"; last status error: {last_command_error}" if last_command_error else ""
    raise IntegrationTestError(
        f"kubectl attempt at {result_root} did not reach {sorted(expected)}{detail}"
    )


def _run_kubectl_failure_resume(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
) -> None:
    """Collect a failed attempt, then resume it with a new attempt identity."""
    first, resume = runtime.scenario.steps
    result_base = str(Path(runtime.workspace) / "results" / first.name)
    _reset_result_base(runner, config, fixture, runtime, result_base)
    _sync_step_runtime(runner, config, fixture, runtime, first, template, result_base)
    _create_generated_inputs(runner, config, fixture, runtime, first)
    _validate_step_environment(runner, config, fixture, runtime, first, log_dir)
    submission = _run_kubectl_command(
        runner,
        config,
        runtime,
        first.name,
        first.render_arguments(runtime.values),
        log_dir,
        first.timeout_seconds,
    )
    result_root, first_attempt = _kubectl_submission(submission)
    runtime.values["kubectl_result_root"] = str(result_root)
    terminal = _wait_for_kubectl_terminal_state(
        runner, config, runtime, result_root, log_dir, first.timeout_seconds
    )
    failed_result = _kubectl_collect_result(
        runner,
        config,
        runtime,
        result_root,
        log_dir,
        terminal_states=(
            frozenset({"FAILED", "CANCELLED"}) if terminal != "SUCCESS" else frozenset()
        ),
    )
    runtime.values["failed_results_dir"] = str(result_root)
    runtime.values["kubectl_collected"] = "1"
    if terminal not in {"FAILED", "CANCELLED"}:
        raise IntegrationTestError(
            f"failure-resume first attempt unexpectedly ended in {terminal}"
        )
    _assert_execution_contract(runtime.scenario, first, failed_result)
    preserved = _hash_execution_contract(failed_result, "0001")
    _assert_dataset_state(
        runner, config, fixture, first, failed_result, None, KUBECTL_STORAGE_MOUNT
    )

    # The first attempt is collected, but the resumed attempt is active again;
    # scenario cleanup must therefore remain armed for the new attempt.
    runtime.values["kubectl_collected"] = "0"
    resume_output = _run_kubectl_command(
        runner,
        config,
        runtime,
        resume.name,
        resume.render_arguments(runtime.values),
        log_dir,
        resume.timeout_seconds,
    )
    resumed_root, second_attempt = _kubectl_submission(resume_output)
    if second_attempt == first_attempt:
        raise IntegrationTestError("kubectl resume reused the failed attempt ID")
    runtime.values["kubectl_result_root"] = str(resumed_root)
    terminal = _wait_for_kubectl_terminal_state(
        runner, config, runtime, resumed_root, log_dir, resume.timeout_seconds
    )
    if terminal != "SUCCESS":
        raise IntegrationTestError(f"kubectl resume ended in {terminal}")
    resumed_result = _kubectl_collect_result(
        runner, config, runtime, resumed_root, log_dir
    )
    runtime.values["kubectl_collected"] = "1"
    _assert_execution_contract(runtime.scenario, resume, resumed_result)
    if _hash_execution_contract(resumed_result, "0001") != preserved:
        raise IntegrationTestError("kubectl resume replaced successful execution 0001")
    _assert_dataset_state(
        runner, config, fixture, resume, resumed_result, None, KUBECTL_STORAGE_MOUNT
    )
    _assert_scenario_report(
        runner, report_workspace, resumed_result, runtime, resume, log_dir
    )


def _kubectl_delete_coordinator_pod(runner: Any, config: Any, attempt_id: str) -> None:
    """Delete exactly the owned coordinator Pod for a submitted attempt."""
    result = runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "get",
            "pods",
            "-l",
            f"storage-scale-test.nvidia.com/run={attempt_id},"
            "app.kubernetes.io/component=coordinator",
            "-o",
            "json",
        ),
        timeout=30,
    )
    pods = json.loads(result.stdout).get("items", [])
    if len(pods) != 1:
        raise IntegrationTestError(
            f"expected exactly one coordinator Pod for {attempt_id}, found {len(pods)}"
        )
    pod = pods[0]
    name = str(pod.get("metadata", {}).get("name", ""))
    observed = str(
        pod.get("metadata", {})
        .get("labels", {})
        .get("storage-scale-test.nvidia.com/run", "")
    )
    if not name or observed != attempt_id:
        raise IntegrationTestError("coordinator Pod ownership evidence is invalid")
    runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "delete",
            "pod",
            name,
            "--wait=true",
            "--timeout=60s",
        ),
        timeout=75,
    )


def _kubectl_delete_worker_pod(runner: Any, config: Any, attempt_id: str) -> None:
    """Delete one exact owned worker Pod after endpoint publication."""
    result = runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "get",
            "pods",
            "-l",
            f"storage-scale-test.nvidia.com/run={attempt_id},"
            "app.kubernetes.io/component=workers",
            "-o",
            "json",
        ),
        timeout=30,
    )
    pods = json.loads(result.stdout).get("items", [])
    candidates = [
        pod for pod in pods if not pod.get("metadata", {}).get("deletionTimestamp")
    ]
    if not candidates:
        raise IntegrationTestError(
            "no nonterminating worker Pod is available to replace"
        )
    pod = sorted(candidates, key=lambda item: str(item["metadata"]["name"]))[0]
    name = str(pod["metadata"]["name"])
    if (
        pod.get("metadata", {})
        .get("labels", {})
        .get("storage-scale-test.nvidia.com/run")
        != attempt_id
    ):
        raise IntegrationTestError("worker Pod ownership evidence is invalid")
    runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "delete",
            "pod",
            name,
            "--wait=true",
            "--timeout=60s",
        ),
        timeout=75,
    )


def _run_kubectl_cancel(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    _report_workspace: Path,
    log_dir: Path,
) -> None:
    """Cancel a running attempt twice and collect its durable cancellation."""
    step = runtime.scenario.steps[0]
    result_base = str(Path(runtime.workspace) / "results" / step.name)
    _reset_result_base(runner, config, fixture, runtime, result_base)
    _sync_step_runtime(runner, config, fixture, runtime, step, template, result_base)
    _validate_step_environment(runner, config, fixture, runtime, step, log_dir)
    output = _run_kubectl_command(
        runner,
        config,
        runtime,
        step.name,
        step.render_arguments(runtime.values),
        log_dir,
        step.timeout_seconds,
    )
    result_root, _attempt_id = _kubectl_submission(output)
    runtime.values["kubectl_result_root"] = str(result_root)
    _kubectl_wait_for_state(
        runner,
        config,
        runtime,
        result_root,
        log_dir,
        frozenset({"RUNNING"}),
        step.timeout_seconds,
    )
    for index in (1, 2):
        _run_kubectl_command(
            runner,
            config,
            runtime,
            f"cancel-{index}",
            ("--cancel", str(result_root)),
            log_dir,
            120,
        )
    result = _kubectl_collect_result(
        runner,
        config,
        runtime,
        result_root,
        log_dir,
        terminal_states=frozenset({"CANCELLED"}),
    )
    runtime.values["kubectl_collected"] = "1"
    _run_kubectl_command(
        runner,
        config,
        runtime,
        "collect-again",
        ("--collect", str(result_root)),
        log_dir,
        120,
        accepted_states=frozenset({"CANCELLED"}),
    )
    status_path = result / "executions" / "0001.status"
    execution_status = status_path.read_text(encoding="utf-8").strip()
    if execution_status != "FAILED":
        raise IntegrationTestError(
            "cancelled Kubernetes execution was not finalized as failed: "
            f"{execution_status}"
        )
    exit_code = (
        (result / "executions" / "0001.exitcode").read_text(encoding="utf-8").strip()
    )
    if not exit_code.isdigit() or int(exit_code) == 0:
        raise IntegrationTestError(
            f"cancelled Kubernetes execution has invalid exit code {exit_code!r}"
        )
    collected_status = (
        result
        / "kubernetes"
        / "attempts"
        / _attempt_id
        / "collected-state"
        / "run.status"
    )
    if collected_status.read_text(encoding="utf-8").strip() != "CANCELLED":
        raise IntegrationTestError("collected Kubernetes cancellation is not durable")
    _assert_dataset_state(
        runner, config, fixture, step, result, None, KUBECTL_STORAGE_MOUNT
    )


def _run_kubectl_mutated_attempt(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
    mutate: Any,
) -> None:
    """Run one attempt, mutate its exact resource, and collect the outcome."""
    step = runtime.scenario.steps[0]
    result_base = str(Path(runtime.workspace) / "results" / step.name)
    _reset_result_base(runner, config, fixture, runtime, result_base)
    _sync_step_runtime(runner, config, fixture, runtime, step, template, result_base)
    _validate_step_environment(runner, config, fixture, runtime, step, log_dir)
    output = _run_kubectl_command(
        runner,
        config,
        runtime,
        step.name,
        step.render_arguments(runtime.values),
        log_dir,
        step.timeout_seconds,
    )
    result_root, attempt_id = _kubectl_submission(output)
    runtime.values["kubectl_result_root"] = str(result_root)
    _kubectl_wait_for_state(
        runner,
        config,
        runtime,
        result_root,
        log_dir,
        frozenset({"RUNNING"}),
        step.timeout_seconds,
    )
    mutate(attempt_id)
    terminal = _kubectl_wait_for_state(
        runner,
        config,
        runtime,
        result_root,
        log_dir,
        KUBECTL_TERMINAL_STATES,
        step.timeout_seconds,
    )
    accepted = frozenset({terminal}) if terminal != "SUCCESS" else frozenset()
    result = _kubectl_collect_result(
        runner,
        config,
        runtime,
        result_root,
        log_dir,
        terminal_states=accepted,
    )
    runtime.values["kubectl_collected"] = "1"
    if terminal == "SUCCESS":
        _assert_execution_contract(runtime.scenario, step, result)
        _assert_semantic_flags(runtime.scenario.name, step, result)
        _assert_dataset_state(
            runner, config, fixture, step, result, None, KUBECTL_STORAGE_MOUNT
        )
        _assert_scenario_report(
            runner, report_workspace, result, runtime, step, log_dir
        )
        return
    execution_status = (
        (result / "executions" / "0001.status").read_text(encoding="utf-8").strip()
    )
    if execution_status != "FAILED":
        raise IntegrationTestError(
            f"mutated Kubernetes execution ended in {execution_status}, not FAILED"
        )
    exit_code = (
        (result / "executions" / "0001.exitcode").read_text(encoding="utf-8").strip()
    )
    if not exit_code.isdigit() or int(exit_code) == 0:
        raise IntegrationTestError(
            f"mutated Kubernetes execution has invalid exit code {exit_code!r}"
        )
    attempt_dir = result / "kubernetes" / "attempts" / attempt_id
    if runtime.scenario.name == "kubectl-coordinator-loss":
        evidence = result / "coordinator-loss.tsv"
        required = {"observed_status\tRUNNING", "recovered_status\tFAILED"}
        observed = (
            set(evidence.read_text(encoding="utf-8").splitlines())
            if evidence.is_file()
            else set()
        )
        if not required <= observed:
            raise IntegrationTestError(
                f"coordinator-loss evidence is incomplete: {sorted(observed)}"
            )
    elif runtime.scenario.name == "kubectl-endpoint-drift":
        evidence = attempt_dir / "endpoint-drift.tsv"
        if not evidence.is_file() or "\tIP_DRIFT" not in evidence.read_text(
            encoding="utf-8"
        ):
            raise IntegrationTestError("changed worker Pod IP lacks drift evidence")
    if len(runtime.scenario.steps) < 2:
        return
    resume = runtime.scenario.steps[1]
    runtime.values["failed_results_dir"] = str(result_root)
    runtime.values["kubectl_collected"] = "0"
    output = _run_kubectl_command(
        runner,
        config,
        runtime,
        resume.name,
        resume.render_arguments(runtime.values),
        log_dir,
        resume.timeout_seconds,
    )
    resumed_root, resumed_attempt = _kubectl_submission(output)
    if resumed_attempt == attempt_id:
        raise IntegrationTestError("Kubernetes resume reused the failed attempt ID")
    runtime.values["kubectl_result_root"] = str(resumed_root)
    if (
        _wait_for_kubectl_terminal_state(
            runner,
            config,
            runtime,
            resumed_root,
            log_dir,
            resume.timeout_seconds,
        )
        != "SUCCESS"
    ):
        raise IntegrationTestError("Kubernetes resume did not succeed")
    resumed = _kubectl_collect_result(runner, config, runtime, resumed_root, log_dir)
    runtime.values["kubectl_collected"] = "1"
    _assert_execution_contract(runtime.scenario, resume, resumed)
    _assert_scenario_report(runner, report_workspace, resumed, runtime, resume, log_dir)


def _run_kubectl_coordinator_loss(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
) -> None:
    """Exercise terminal collection after deleting the exact coordinator Pod."""
    _run_kubectl_mutated_attempt(
        runner,
        config,
        fixture,
        runtime,
        template,
        report_workspace,
        log_dir,
        lambda attempt: _kubectl_delete_coordinator_pod(runner, config, attempt),
    )


def _run_kubectl_endpoint_drift(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
) -> None:
    """Exercise worker replacement after the attempt endpoint freeze."""
    _run_kubectl_mutated_attempt(
        runner,
        config,
        fixture,
        runtime,
        template,
        report_workspace,
        log_dir,
        lambda attempt: _kubectl_delete_worker_pod(runner, config, attempt),
    )


def _cleanup_kubectl_attempt(
    runner: Any, config: Any, runtime: ScenarioRuntime
) -> None:
    """Ask the product lifecycle to stop and collect an interrupted attempt."""
    result_root = runtime.values.get("kubectl_result_root")
    if not result_root:
        return
    # A successful collect has already released the remote Job, worker set,
    # lock, and transfer helpers.  Do not make success depend on how the
    # product treats a second cancel/collect against a COLLECTED attempt.
    if runtime.values.get("kubectl_collected") == "1":
        return
    command_root = Path(runtime.workspace) / "storage-tests" / "fs"
    for operation in ("--cancel", "--collect"):
        result = runner.run(
            [
                "env",
                f"KUBECONFIG={config.kubeconfig}",
                "timeout",
                "--kill-after=15s",
                "90s",
                command_root / "nv-elbencho-sweep.sh",
                operation,
                result_root,
            ],
            cwd=Path(runtime.workspace),
            check=False,
            timeout=120,
        )
        if result.returncode:
            output = result.stdout + result.stderr
            if operation == "--collect":
                try:
                    terminal = _kubectl_lifecycle_state(output)
                except IntegrationTestError:
                    terminal = ""
                if terminal in {"FAILED", "CANCELLED"}:
                    continue
            detail = (result.stderr or result.stdout).strip()
            raise IntegrationTestError(
                f"kubectl scenario cleanup {operation} failed for {result_root}: {detail}"
            )


def _cleanup_ssh_remote_results(runner: Any, config: Any) -> None:
    """Remove retrieved per-scenario output trees from bounded SSH homes."""
    failures = []
    for pod in _pods_with_container(_pod_inventory(runner, config), "sshd"):
        result = runner.run(
            [
                *_kubectl(
                    config,
                    "-n",
                    config.namespace,
                    "exec",
                    pod["metadata"]["name"],
                    "-c",
                    "sshd",
                    "--",
                ),
                "runuser",
                "-u",
                WORKLOAD_USER,
                "--",
                "bash",
                "-c",
                "rm -rf -- /home/tester/elbencho-[0-9]*",
            ],
            check=False,
            timeout=60,
        )
        if result.returncode:
            failures.append(
                f"{pod['metadata']['name']}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
    if failures:
        raise IntegrationTestError(
            "could not clean SSH scenario results: " + "; ".join(failures)
        )


def _preserve_scenario_failure_diagnostics(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    log_dir: Path,
    error: Exception,
) -> None:
    """Copy bounded scenario logs before its disposable workspace is removed."""
    destination = log_dir / "failure-diagnostics"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "failure.txt").write_text(f"{error!r}\n", encoding="utf-8")
    failures: list[str] = []
    for name in ("logs", "results"):
        source = f"{runtime.workspace}/{name}"
        target = destination / name
        try:
            if runtime.selector in {"ssh", "kubectl"}:
                local_source = Path(source)
                if local_source.exists():
                    shutil.copytree(local_source, target, dirs_exist_ok=True)
                else:
                    failures.append(f"missing local diagnostic path: {source}")
                continue
            result = runner.run(
                [
                    *_kubectl(config, "-n", config.namespace, "cp"),
                    "-c",
                    fixture.login_container,
                    f"{fixture.login_pod}:{source}",
                    target,
                ],
                check=False,
                timeout=180,
            )
            if result.returncode:
                detail = (result.stderr or result.stdout).strip()
                failures.append(
                    f"could not copy {source} (exit {result.returncode}): {detail}"
                )
        except (OSError, subprocess.SubprocessError) as diagnostic_error:
            failures.append(f"could not copy {source}: {diagnostic_error!r}")
    if failures:
        (destination / "collection-errors.txt").write_text(
            "\n".join(failures) + "\n", encoding="utf-8"
        )
        LOG.warning(
            "Some %s/%s failure diagnostics could not be retained; see %s",
            runtime.scenario.name,
            runtime.selector,
            destination / "collection-errors.txt",
        )


def _cleanup_scenario_storage(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
) -> None:
    """Remove one scenario's isolated PVC data and Slurm deployment."""
    if runtime.selector == "kubectl":
        logical_root = PurePosixPath(runtime.data_root)
        expected_parent = PurePosixPath("integration-regression")
        if logical_root.parent != expected_parent or not logical_root.name:
            raise IntegrationTestError(
                f"refusing to remove unexpected Kubernetes scenario data: "
                f"{logical_root}"
            )
        _storage_utility_shell(
            runner,
            config,
            fixture,
            f"rm -rf -- {_shell(KUBECTL_STORAGE_MOUNT + '/' + str(logical_root))}",
            timeout=120,
        )
        return
    if runtime.selector not in {"ssh", "slurm"}:
        raise IntegrationTestError(
            f"unsupported integration selector: {runtime.selector}"
        )
    data_root = PurePosixPath(runtime.data_root)
    data_base = PurePosixPath(REMOTE_BASE) / "test-data"
    targets = [data_root]
    if runtime.selector == "slurm":
        workspace_root = PurePosixPath(runtime.workspace).parent
        workspace_base = PurePosixPath(REMOTE_BASE) / "workspaces"
        if workspace_root.parent != workspace_base:
            raise IntegrationTestError(
                f"refusing to remove unexpected Slurm workspace: {workspace_root}"
            )
        targets.append(workspace_root)
    if data_root.parent != data_base:
        raise IntegrationTestError(
            f"refusing to remove unexpected scenario data path: {data_root}"
        )
    command = "rm -rf -- " + " ".join(_shell(path) for path in targets)
    result = runner.run(
        _pod_command(
            config,
            fixture.login_pod,
            fixture.login_container,
            command,
            as_user=WORKLOAD_USER,
            timeout=120,
        ),
        check=False,
        timeout=135,
    )
    if result.returncode:
        raise IntegrationTestError(
            "could not clean scenario-owned remote storage for "
            f"{runtime.scenario.name}/{runtime.selector}: "
            f"{(result.stderr or result.stdout).strip()}"
        )


def _cleanup_scenario_resources(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
) -> None:
    """Attempt every scenario cleanup and report all failures together."""
    failures = []
    operations = []
    if runtime.selector == "ssh":
        operations.append(lambda: _cleanup_ssh_remote_results(runner, config))
    if runtime.selector == "kubectl":
        operations.append(lambda: _cleanup_kubectl_attempt(runner, config, runtime))
    operations.append(
        lambda: _cleanup_scenario_storage(runner, config, fixture, runtime)
    )
    operations.append(lambda: _cleanup_local_scenario_workspace(config, runtime))
    for cleanup in operations:
        try:
            cleanup()
        except Exception as error:  # pylint: disable=broad-exception-caught
            failures.append(f"{type(error).__name__}: {error}")
    if failures:
        raise IntegrationTestError("; ".join(failures))


def _cleanup_local_scenario_workspace(config: Any, runtime: ScenarioRuntime) -> None:
    """Remove only a scenario workspace below the harness test-run root."""
    workspace = runtime.local_workspace
    test_runs = (config.state_dir / "test-runs").resolve()
    resolved = workspace.resolve(strict=False)
    if workspace.is_symlink() or test_runs not in resolved.parents:
        raise IntegrationTestError(
            f"refusing to remove unexpected local scenario workspace: {workspace}"
        )
    if workspace.exists():
        shutil.rmtree(workspace)


def _record_secondary_cleanup_failure(log_dir: Path, error: Exception) -> None:
    """Retain cleanup failure without replacing the scenario's primary error."""
    try:
        destination = log_dir / "failure-diagnostics"
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / "cleanup-error.txt"
        path.write_text(f"{type(error).__name__}: {error}\n", encoding="utf-8")
        LOG.error("Scenario cleanup also failed; retained details in %s", path)
    except OSError as diagnostic_error:
        LOG.error(
            "Scenario cleanup also failed (%r), and its diagnostic could not "
            "be retained: %r",
            error,
            diagnostic_error,
        )


def _remote_staging_operations(
    runner: Any,
    config: Any,
    fixture: Fixture,
    selector: str,
    local_wrapper: Path | None,
    restore_binary: Path | None,
    remote_wrapper: PurePosixPath | None = None,
) -> FailureInjectionOperations:
    """Return concrete PVC or SSH-pod operations for failure staging."""

    def _container(_endpoint: str) -> str:
        return fixture.login_container if selector == "slurm" else "sshd"

    def _command(endpoint: str, command: str, *, stdin: Any = None) -> None:
        if endpoint == "local":
            return
        runner.run(
            [
                *_kubectl(
                    config,
                    "-n",
                    config.namespace,
                    "exec",
                    "-i",
                    endpoint,
                    "-c",
                    _container(endpoint),
                    "--",
                ),
                "runuser",
                "-u",
                WORKLOAD_USER,
                "--",
                "bash",
                "-ec",
                f"umask 0007; {command}",
            ],
            stdin=stdin,
            timeout=180,
        )

    def make_directory(endpoint: str, path: PurePosixPath) -> None:
        if endpoint == "local":
            Path(path).mkdir(parents=True, exist_ok=True)
            return
        _command(
            endpoint,
            f"mkdir -p -- {_shell(path)}",
        )

    def copy_file(
        source: Path, endpoint: str, destination: PurePosixPath, mode: int
    ) -> None:
        if endpoint == "local":
            local_destination = Path(destination)
            local_destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, local_destination)
            local_destination.chmod(mode)
            return
        with tempfile.TemporaryFile() as stream:
            stream.write(source.read_bytes())
            stream.seek(0)
            _command(
                endpoint,
                f"cat > {_shell(destination)} && chmod {mode:o} -- "
                f"{_shell(destination)}",
                stdin=stream,
            )

    def copy_tree(source: Path, endpoint: str, destination: PurePosixPath) -> None:
        if endpoint == "local":
            local_destination = Path(destination)
            if local_destination.exists():
                shutil.rmtree(local_destination)
            shutil.copytree(source, local_destination)
            return
        if selector == "ssh":
            _command(
                endpoint,
                f"rm -rf -- {_shell(destination)} && ln -s -- "
                f"/home/tester/elbencho-runtime {_shell(destination)}",
            )
            return
        with tempfile.TemporaryFile() as stream:
            with tarfile.open(fileobj=stream, mode="w") as archive:
                for child in sorted(source.rglob("*")):
                    archive.add(child, arcname=child.relative_to(source))
            stream.seek(0)
            _command(
                endpoint,
                f"rm -rf -- {_shell(destination)} && mkdir -p -- "
                f"{_shell(destination)} && tar -xf - -C {_shell(destination)} "
                f"&& chmod -R u+rwX,g+rX,o-rwx -- {_shell(destination)}",
                stdin=stream,
            )

    def write_text(
        endpoint: str, destination: PurePosixPath, content: str, mode: int
    ) -> None:
        if endpoint == "local":
            if local_wrapper is None:
                raise IntegrationTestError("local failure wrapper path is absent")
            local_wrapper.write_text(content, encoding="utf-8")
            local_wrapper.chmod(mode)
            return
        if remote_wrapper is not None:
            destination = remote_wrapper
        with tempfile.TemporaryFile() as stream:
            stream.write(content.encode())
            stream.seek(0)
            _command(
                endpoint,
                f"cat > {_shell(destination)} && chmod {mode:o} -- "
                f"{_shell(destination)}",
                stdin=stream,
            )

    def remove_tree(endpoint: str, path: PurePosixPath) -> None:
        if endpoint == "local":
            if local_wrapper is not None and restore_binary is not None:
                shutil.copy2(restore_binary, local_wrapper)
            shutil.rmtree(Path(path), ignore_errors=True)
            return
        if remote_wrapper is not None:
            _command(
                endpoint,
                f"cp -- {_shell(path / 'delegate' / 'elbencho')} "
                f"{_shell(remote_wrapper)}",
            )
        _command(endpoint, f"rm -rf -- {_shell(path)}")

    return FailureInjectionOperations(
        make_directory, copy_file, copy_tree, write_text, remove_tree
    )


def _failure_plan(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    binary_name: str,
    source_binary: Path,
    bundled_runtime: Path | None,
) -> tuple[Any, FailureInjectionOperations]:
    """Build a feasible substrate-specific failure staging plan."""
    packaged_binary = (
        Path(runtime.workspace) / "utils" / binary_name
        if runtime.selector == "ssh"
        else runtime.local_workspace / "source-elbencho"
    )
    if runtime.selector == "slurm":
        raise IntegrationTestError("internal Slurm failure source was not prepared")
    pods = sorted(
        _pods_with_container(_pod_inventory(runner, config), "sshd"),
        key=lambda item: item["metadata"]["name"],
    )
    plan = build_ssh_failure_injection_plan(
        scenario_id=f"{runtime.scenario.name}-{os.getpid()}",
        staging_root=SSH_FAILURE_STAGING_BASE,
        source_binary=source_binary,
        source_runtime=bundled_runtime,
        target_argument="executions/0002.write.json",
        coordinator_endpoint="local",
        worker_endpoints=[item["metadata"]["name"] for item in pods],
    )
    operations = _remote_staging_operations(
        runner,
        config,
        fixture,
        runtime.selector,
        packaged_binary,
        source_binary,
    )
    return plan, operations


def _slurm_failure_plan(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    source_binary: Path,
    binary_name: str,
    bundled_runtime: Path | None,
) -> tuple[Any, FailureInjectionOperations]:
    """Build failure staging on the Slurm deployment's shared PVC."""
    plan = build_slurm_failure_injection_plan(
        scenario_id=f"{runtime.scenario.name}-{os.getpid()}",
        staging_root=f"{runtime.workspace}/.storage-scale-test-failure",
        source_binary=source_binary,
        source_runtime=bundled_runtime,
        target_argument="executions/0002.write.json",
        shared_endpoint=fixture.login_pod,
    )
    operations = _remote_staging_operations(
        runner,
        config,
        fixture,
        runtime.selector,
        None,
        None,
        PurePosixPath(runtime.workspace) / "utils" / binary_name,
    )
    return plan, operations


def _hash_execution_contract(result: Path, execution_id: str) -> dict[str, str]:
    """Hash stable evidence that resume must not replace for successful cells."""
    evidence: dict[str, str] = {}
    for path in sorted((result / "executions").glob(f"{execution_id}.*")):
        if path.is_file():
            evidence[path.name] = _sha256(path)
    return evidence


def _capture_failure_diagnostics(
    runner: Any,
    config: Any,
    fixture: Fixture,
    plan: Any,
    selector: str,
    log_dir: Path,
) -> None:
    """Copy wrapper invocation traces before scenario-level cleanup."""
    chunks: list[str] = []
    container = fixture.login_container if selector == "slurm" else "sshd"
    for target in plan.targets:
        if target.endpoint == "local":
            continue
        result = runner.run(
            [
                *_kubectl(
                    config,
                    "-n",
                    config.namespace,
                    "exec",
                    target.endpoint,
                    "-c",
                    container,
                    "--",
                ),
                "bash",
                "-c",
                f"cat -- {_shell(plan.layout.root / 'invocations.log')} "
                "2>/dev/null || true",
            ],
            check=False,
            timeout=30,
        )
        chunks.append(f"## {target.endpoint}\n{result.stdout}")
    (log_dir / "failure-wrapper-invocations.log").write_text(
        "\n".join(chunks), encoding="utf-8"
    )


def _run_failure_resume(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
    binary_name: str,
    source_binary: Path,
    bundled_runtime: Path | None,
) -> None:
    """Run one real injected failure and resume without restaging the marker."""
    first, resume = runtime.scenario.steps
    if runtime.selector == "ssh":
        plan, operations = _failure_plan(
            runner,
            config,
            fixture,
            runtime,
            binary_name,
            source_binary,
            bundled_runtime,
        )
        injected_first = first
    elif runtime.selector == "slurm":
        plan, operations = _slurm_failure_plan(
            runner,
            config,
            fixture,
            runtime,
            source_binary,
            binary_name,
            bundled_runtime,
        )
        injected_first = first
    else:
        raise IntegrationTestError(
            f"failure-resume has no adapter for {runtime.selector!r}"
        )
    result_base = f"{runtime.workspace}/results/{first.name}"
    _reset_result_base(runner, config, fixture, runtime, result_base)
    _sync_step_runtime(
        runner,
        config,
        fixture,
        runtime,
        injected_first,
        template,
        result_base,
    )
    _validate_step_environment(
        runner, config, fixture, runtime, injected_first, log_dir
    )
    with staged_failure_injection(plan, operations):
        arguments = shlex.join(first.render_arguments(runtime.values))
        command = (
            f"cd -- {_shell(runtime.workspace)} && "
            f"./storage-tests/fs/nv-elbencho-sweep.sh {arguments}"
        )
        _run_step(
            runner,
            config,
            fixture,
            runtime.selector,
            first.name,
            command,
            log_dir,
            first.timeout_seconds,
            expected_failure=True,
        )
        remote_result = _discover_result(
            runner,
            config,
            fixture,
            runtime,
            first,
            result_base,
            log_dir,
        )
        failed_result = _copy_scenario_result(
            runner, config, fixture, runtime, first, remote_result
        )
        _capture_failure_diagnostics(
            runner, config, fixture, plan, runtime.selector, log_dir
        )
        _assert_execution_contract(runtime.scenario, first, failed_result)
        runtime.values["failed_results_dir"] = remote_result
        _assert_dataset_state(runner, config, fixture, first, failed_result, None)
        preserved = _hash_execution_contract(failed_result, "0001")
        resume_command = (
            f"cd -- {_shell(runtime.workspace)} && "
            "./storage-tests/fs/nv-elbencho-sweep.sh "
            f"{shlex.join(resume.render_arguments(runtime.values))}"
        )
        _run_step(
            runner,
            config,
            fixture,
            runtime.selector,
            resume.name,
            resume_command,
            log_dir,
            resume.timeout_seconds,
        )
        resumed_result = _copy_scenario_result(
            runner, config, fixture, runtime, resume, remote_result
        )
        _assert_execution_contract(runtime.scenario, resume, resumed_result)
        if _hash_execution_contract(resumed_result, "0001") != preserved:
            raise IntegrationTestError("resume replaced successful execution 0001")
        _assert_dataset_state(runner, config, fixture, resume, resumed_result, None)
        _assert_scenario_report(
            runner,
            report_workspace,
            resumed_result,
            runtime,
            resume,
            log_dir,
        )


def run_filesystem_tests(
    runner: Any,
    config: Any,
    repo_root: Path,
    substrate: str,
    scenarios: list[str],
    transition_ssh_home: Any | None = None,
) -> None:
    """Run selected filesystem regression cases against an existing setup."""
    try:
        plan = plan_scenarios(substrate=substrate, requested=scenarios)
    except ScenarioPlanningError as error:
        raise IntegrationTestError(str(error)) from error
    work = [step for step in plan if isinstance(step, WorkItem)]
    selected = {step.substrate.value for step in work}
    if "ssh" in selected:
        if transition_ssh_home is None:
            raise IntegrationTestError("SSH scenarios require a home transition hook")
        transition_ssh_home("separate", "preflight")
    LOG.info("Requiring an already-running integration setup")
    fixture = _require_fixture(runner, config, selected)
    binary, binary_name, runtime = _ensure_elbencho(
        runner, config, fixture.architecture, fixture.storage_backend
    )
    if runtime is not None and "ssh" in selected:
        LOG.info("Staging the pinned Elbencho container runtime in SSH worker homes")
        _stage_ssh_runtime(runner, config, runtime, _pod_inventory(runner, config))
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}"
    log_dir = config.state_dir / "test-runs" / run_id
    log_dir.mkdir(parents=True, exist_ok=False)
    LOG.info("Filesystem integration artifacts: %s", log_dir)
    scratch_parent = config.state_dir / "test-runs"
    with tempfile.TemporaryDirectory(dir=scratch_parent) as temporary:
        build_root = Path(temporary)
        archive, extracted = _build_deployment_archive(
            runner,
            config,
            repo_root,
            build_root,
            binary,
            binary_name,
            fixture.architecture,
            runtime,
        )
        shutil.copy2(build_root / "build-tarball.log", log_dir / "build-tarball.log")
        report_workspace = build_root / "report-workspace"
        shutil.copytree(extracted, report_workspace)
        report_fixture = fixture
        if not report_fixture.ssh_addresses:
            report_fixture = replace(
                report_fixture, ssh_addresses=report_fixture.slurm_addresses
            )
        _write_runtime_files(
            report_workspace,
            "ssh",
            str(report_workspace),
            report_fixture,
            template=extracted / "env.sh.template",
        )
        home_mode = "separate"
        suite_error: Exception | None = None
        try:
            for step in plan:
                if isinstance(step, SshHomeTransition):
                    transition_ssh_home(step.target.value, "ssh-shared-home")
                    home_mode = step.target.value
                    fixture = _require_fixture(
                        runner, config, selected, ssh_home_mode=home_mode
                    )
                    if runtime is not None:
                        _stage_ssh_runtime(
                            runner,
                            config,
                            runtime,
                            _pod_inventory(runner, config),
                        )
                    continue
                scenario = step.scenario.name
                scenario_root = build_root / f"{scenario}-{step.substrate.value}"
                scenario_root.mkdir()
                scenario_logs = log_dir / f"{scenario}-{step.substrate.value}"
                scenario_logs.mkdir()
                specification = SCENARIO_SPECS_BY_NAME[scenario]
                runtime_state = _make_scenario_runtime(
                    fixture,
                    specification,
                    step.substrate.value,
                    scenario_root,
                    scenario_logs,
                    run_id,
                )
                primary_error: Exception | None = None
                try:
                    _prepare_scenario_runtime(
                        runner,
                        config,
                        fixture,
                        runtime_state,
                        archive,
                        extracted,
                    )
                    if step.substrate.value == "kubectl":
                        if scenario not in KUBECTL_SUPPORTED_SCENARIOS:
                            raise IntegrationTestError(
                                f"Kubernetes scenario has no explicit adapter: {scenario}"
                            )
                        kubectl_args = (
                            runner,
                            config,
                            fixture,
                            runtime_state,
                            extracted / "env.sh.template",
                            report_workspace,
                            scenario_logs,
                        )
                        if scenario == "failure-resume":
                            _run_kubectl_failure_resume(*kubectl_args)
                        elif scenario in {
                            "kubectl-cancel",
                            "cancel",
                        }:
                            _run_kubectl_cancel(*kubectl_args)
                        elif scenario in {
                            "kubectl-coordinator-loss",
                            "coordinator-loss",
                        }:
                            _run_kubectl_coordinator_loss(*kubectl_args)
                        elif scenario in {
                            "kubectl-endpoint-drift",
                            "endpoint-drift",
                        }:
                            _run_kubectl_endpoint_drift(*kubectl_args)
                        else:
                            _run_kubectl_success_scenario(*kubectl_args)
                    elif scenario == "failure-resume":
                        _run_failure_resume(
                            runner,
                            config,
                            fixture,
                            runtime_state,
                            extracted / "env.sh.template",
                            report_workspace,
                            scenario_logs,
                            binary_name,
                            binary,
                            runtime,
                        )
                    else:
                        _run_regular_scenario(
                            runner,
                            config,
                            fixture,
                            runtime_state,
                            extracted / "env.sh.template",
                            report_workspace,
                            scenario_logs,
                        )
                except Exception as error:
                    primary_error = error
                    try:
                        _preserve_scenario_failure_diagnostics(
                            runner,
                            config,
                            fixture,
                            runtime_state,
                            scenario_logs,
                            error,
                        )
                    except (OSError, subprocess.SubprocessError) as diagnostic_error:
                        LOG.warning(
                            "Could not retain %s/%s failure diagnostics: %r",
                            scenario,
                            step.substrate.value,
                            diagnostic_error,
                        )
                    raise
                finally:
                    try:
                        _cleanup_scenario_resources(
                            runner, config, fixture, runtime_state
                        )
                    # Cleanup must never replace an active scenario exception.
                    # pylint: disable-next=broad-exception-caught
                    except Exception as cleanup_error:
                        if primary_error is None:
                            raise
                        _record_secondary_cleanup_failure(scenario_logs, cleanup_error)
        except Exception as error:
            suite_error = error
            raise
        finally:
            if home_mode == "shared" and transition_ssh_home is not None:
                try:
                    transition_ssh_home("separate", "ssh-shared-home-restore")
                # pylint: disable-next=broad-exception-caught
                except Exception as restore_error:
                    if suite_error is None:
                        raise
                    _record_secondary_cleanup_failure(log_dir, restore_error)
    names = ", ".join(f"{step.scenario.name}/{step.substrate.value}" for step in work)
    LOG.info("Filesystem integration tests passed: %s", names)
