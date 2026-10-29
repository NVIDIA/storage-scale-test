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
import signal
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

import yaml

from deployment_cache import (
    DeploymentCacheError,
    DeploymentCacheRequest,
    build_release_source_archive,
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
from fixture_images import kubectl_sequencer_fixture
from fixture_storage import (
    SAME_NAMESPACE_CLAIM,
    STORAGE_CLAIM,
    UNBOUND_CLAIM,
    UNBOUND_PV,
    KubectlStorageTarget,
    StorageTargetError,
    kubectl_target_env_lines,
    second_namespace,
)
from image_acquisition import AcquisitionError, ensure_pinned_image
from fixture_capacity import (
    MAX_DEPLOYMENT_CONTENT_BYTES,
    MAX_LIVE_CAPTURE_DATASET_BYTES,
)
from filesystem_scenario_specs import (
    MIXED_BATCH_TARGET_FILE,
    MIXED_BATCH_TARGET_NAME,
    SCENARIO_SPECS_BY_NAME,
    SMALL_DIO_FILE_BYTES,
    SMALL_DIO_FILE_SIZE,
    CommandKind,
    DatasetExpectation,
    ExecutionCoordinate,
    ExecutionStatus,
    ExpectedExecution,
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
from shard_manifest import WORK_ITEMS_FILENAME, WorkItemRecorder

LOG = logging.getLogger("storage-scale-integration")

# One batch group's expectation: (group ID, workload kind, storage target name).
GroupRow = tuple[str, str, str]

ELBENCHO_VERSION = "v3.2-1"
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
        "c32f9f905e879b7efed8c2f83ea9be7a362dceae43155bb46ddab3d5d6c23210",
        "elbencho",
    ),
    "aarch64": (
        "elbencho-static-aarch64.tar.gz",
        "3af09f8110586863b35da019e748c8062fcf57cc0f32e446b1c054df4500eba3",
        "elbencho.aarch64",
    ),
}
REMOTE_BASE = "/mnt/storage-test/integration-regression"
SSH_FAILURE_STAGING_BASE = "/tmp/storage-scale-test-integration-failure"
KUBECTL_UTILITY_PREFIX = "storage-scale-test-utility"
KUBECTL_TARGET_SELECTOR = "storage-scale-test/target=true"
KUBECTL_STORAGE_PVC = "storage-test-rwx"
KUBECTL_STORAGE_MOUNT = "/mnt/storage-scale-test"
KUBECTL_CONTROL_DIRECTORY = ".storage-scale-test"
KUBERNETES_DNS_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
KUBERNETES_UID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
KUBECTL_TERMINAL_STATES = frozenset({"SUCCESS", "FAILED", "CANCELLED"})
KUBECTL_STATUS_STATES = KUBECTL_TERMINAL_STATES | frozenset(
    {
        "PREPARED",
        "SUBMITTED",
        "PREPARING",
        "RUNNING",
        "CANCEL_REQUESTED",
        "COLLECTED",
    }
)
KUBECTL_MULTI_TARGET_SCENARIOS = frozenset(
    {
        "kubectl-multi-target",
        "kubectl-multi-target-unbound",
        "kubectl-same-namespace-targets",
    }
)
KUBECTL_SUPPORTED_SCENARIOS = KUBECTL_MULTI_TARGET_SCENARIOS | frozenset(
    {
        "baseline",
        "mdtest-sweep",
        "default-dio",
        "failure-resume",
        "mixed-batch",
        "live-capture",
        "kubectl-retained-read",
        "kubectl-cancel",
        "kubectl-coordinator-loss",
        "kubectl-endpoint-drift",
    }
)
KUBECTL_OWNER_SELECTOR = "app.kubernetes.io/managed-by=storage-scale-test"
KUBECTL_OWNED_KINDS = (
    "pods,jobs.batch,daemonsets.apps,networkpolicies.networking.k8s.io,"
    "serviceaccounts,roles.rbac.authorization.k8s.io,"
    "rolebindings.rbac.authorization.k8s.io,leases.coordination.k8s.io"
)
VALIDATION_SUCCESS = "All validation checks passed successfully"
# The product's wording and keys that the multi-target assertions depend on.
# Each is named once here so a product rename is a one-line change.
#
# validate_env.sh reports a missing sequencer permission as
# "permission to <verb> <resource> in namespace <namespace>".
RBAC_PERMISSION_PHRASE = "permission to {verb} {resource} in namespace {namespace}"
# validate_env.sh --sequencer follows sequencer problems with this advice.
SEQUENCER_FALLBACK_PHRASE = "Fallback: keep each batch in one namespace"
# The validate_env.sh option that checks the sequencer (otherwise skipped).
VALIDATE_SEQUENCER_OPTION = "--sequencer"
# The sequencer log names each namespace segment it starts and finishes
# ("segment 0") and then all of them.
SEQUENCER_UNIT_PATTERN = "segment"
# --status prints SEGMENT_COUNT, SEGMENT_<i>_NAMESPACE and SEGMENT_<i>_STATE for
# several namespaces only; i counts from this base in segment order.
SEGMENT_INDEX_BASE = 0
# Lease, Job and DaemonSet labels (app.kubernetes.io/component).
LEASE_COMPONENT = "pvc-lock"
COORDINATOR_COMPONENT = "coordinator"
WORKER_COMPONENT = "workers"
WORKLOAD_USER = "tester"
WORKLOAD_UID = 2000
WORKLOAD_GID = 2000


class IntegrationTestError(RuntimeError):
    """An actionable filesystem integration test failure."""


class UpstreamDownloadError(IntegrationTestError):
    """An upstream request failed, before any archive could be verified."""


def _runtime_workload_kind(runtime: Any) -> str:
    """Return a scenario's workload kind, defaulting legacy test doubles to FS."""
    scenario = getattr(runtime, "scenario", None)
    steps = getattr(scenario, "steps", ())
    return getattr(steps[0], "workload_kind", "filesystem") if steps else "filesystem"


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
    # Every storage target in list order (the primary claim first), when the
    # fixture has more than one; empty on fixtures set up before multi-target
    # support, which the multi-target scenarios reject with a re-setup hint.
    kubectl_targets: tuple[KubectlStorageTarget, ...] = ()
    kubectl_sequencer_image: str = ""
    # A second claim in the primary namespace, which the same-namespace
    # scenario pairs with the primary claim; None on older fixtures.
    kubectl_same_namespace_target: KubectlStorageTarget | None = None


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


def _bound_claim_target(
    runner: Any, config: Any, namespace: str, claim_name: str
) -> KubectlStorageTarget | None:
    """Return a bound claim as a storage target, or None when it is absent."""
    result = runner.run(
        _kubectl(
            config,
            "-n",
            namespace,
            "get",
            "pvc",
            claim_name,
            "--ignore-not-found",
            "-o",
            "json",
        ),
        timeout=30,
    )
    if not result.stdout.strip():
        return None
    claim = json.loads(result.stdout)
    phase = claim.get("status", {}).get("phase")
    pv = str(claim.get("spec", {}).get("volumeName", ""))
    if phase != "Bound" or not pv:
        raise IntegrationTestError(
            f"{namespace}/{claim_name} is not Bound to a PV "
            f"(phase={phase!r}); run setup"
        )
    return KubectlStorageTarget(namespace, pv, claim_name)


def _second_storage_target(runner: Any, config: Any) -> KubectlStorageTarget | None:
    """Return the second storage target, or None on a pre-multi-target fixture."""
    return _bound_claim_target(
        runner, config, second_namespace(config.namespace), KUBECTL_STORAGE_PVC
    )


def _same_namespace_storage_target(
    runner: Any, config: Any
) -> KubectlStorageTarget | None:
    """Return the primary namespace's second claim, or None on an older fixture."""
    return _bound_claim_target(
        runner, config, str(config.namespace), SAME_NAMESPACE_CLAIM
    )


def _kubectl_storage_targets(
    runner: Any, config: Any, primary: KubectlStorageTarget
) -> tuple[KubectlStorageTarget, ...]:
    """Return the primary target plus the second one when the fixture has it."""
    second = _second_storage_target(runner, config)
    return (primary,) if second is None else (primary, second)


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


KUBECTL_LOCATION = re.compile(
    r"(?:(?P<namespace>[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)/)?"
    r"(?P<claim>[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?):(?P<path>[^:]*)"
)


def kubectl_claim_mount(claim: str) -> str:
    """Return where every Pod mounts *claim*: one directory per claim."""
    return f"{KUBECTL_STORAGE_MOUNT}/{claim}"


def parse_kubectl_location(value: str) -> tuple[str | None, str, str]:
    """Split a logical ``[namespace/]claim:path`` or a Pod path into its parts.

    Returns the namespace (None when absent), the claim, and the path relative
    to the claim. A Pod path is ``<mount>/<claim>/<path>``.
    """
    prefix = f"{KUBECTL_STORAGE_MOUNT}/"
    if value.startswith(prefix):
        claim, _, path = value.removeprefix(prefix).partition("/")
        if claim:
            return None, claim, path
    match = KUBECTL_LOCATION.fullmatch(value)
    if match is None or ".." in PurePosixPath(match["path"]).parts:
        raise IntegrationTestError(
            f"not a claim-qualified Kubernetes location: {value!r}"
        )
    return match["namespace"], match["claim"], match["path"]


def kubectl_storage_path(value: str) -> str:
    """Map a logical ``claim:path`` (or a Pod path) to its Pod mount path."""
    _, claim, path = parse_kubectl_location(value)
    mount = kubectl_claim_mount(claim)
    return f"{mount}/{path}" if path else mount


def _kubectl_utility_manifest(
    name: str,
    fixture: Fixture,
    node_name: str | None = None,
    claims: Sequence[str] | None = None,
) -> str:
    """Render one short-lived, non-root utility Pod mounting claims.

    Each claim is mounted where the product mounts it, so a Pod path the
    product recorded is valid here unchanged. By default the fixture's primary
    claim is mounted.
    """
    claim_names = tuple(claims or (fixture.kubectl_pvc,))
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
                        {
                            "name": f"storage-{index}",
                            "mountPath": kubectl_claim_mount(claim),
                        }
                        for index, claim in enumerate(claim_names)
                    ],
                }
            ],
            "volumes": [
                {
                    "name": f"storage-{index}",
                    "persistentVolumeClaim": {"claimName": claim},
                }
                for index, claim in enumerate(claim_names)
            ],
        },
    }
    return json.dumps(document)


def _wait_for_kubectl_utility(
    runner: Any, config: Any, name: str, namespace: str | None = None
) -> None:
    """Wait a bounded interval for an exact utility Pod to become Ready."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = runner.run(
            _kubectl(
                config,
                "-n",
                namespace or config.namespace,
                "get",
                "pod",
                name,
                "-o",
                "json",
            ),
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
    target: KubectlStorageTarget | None = None,
    claims: Sequence[str] | None = None,
) -> str:
    """Run one storage command without depending on a Slinky LoginSet Pod.

    The Pod mounts each claim at ``<mount>/<claim>``. *target* selects another
    storage target's namespace and claim, and *claims* several claims of that
    namespace; by default the fixture's primary claim is used.
    """
    name = f"{KUBECTL_UTILITY_PREFIX}-{secrets.token_hex(4)}"
    namespace = target.namespace if target is not None else config.namespace
    if claims is None and target is not None:
        claims = (target.pvc,)
    manifest = _kubectl_utility_manifest(name, fixture, node_name, claims)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as stream:
        stream.write(manifest)
        stream.flush()
        stream.seek(0)
        runner.run(
            _kubectl(config, "-n", namespace, "create", "-f", "-"),
            stdin=stream,
            timeout=60,
        )
    try:
        _wait_for_kubectl_utility(runner, config, name, namespace)
        result = runner.run(
            [
                *_kubectl(
                    config,
                    "-n",
                    namespace,
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
                namespace,
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
    kubectl_targets: tuple[KubectlStorageTarget, ...] = ()
    same_namespace_target: KubectlStorageTarget | None = None
    if "kubectl" in substrates:
        kubectl_targets = _kubectl_storage_targets(
            runner,
            config,
            KubectlStorageTarget(str(config.namespace), storage_pv, storage_pvc),
        )
        same_namespace_target = _same_namespace_storage_target(runner, config)
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
        kubectl_targets=kubectl_targets,
        kubectl_sequencer_image=kubectl_sequencer_fixture(
            str(state["storage_backend"])
        ).fixture,
        kubectl_same_namespace_target=same_namespace_target,
    )


def _sha256(path: Path) -> str:
    """Return the SHA-256 digest of *path*."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _in_github_actions() -> bool:
    """GitHub Actions must prove upstream downloads; elsewhere they may be blocked."""
    return os.environ.get("GITHUB_ACTIONS") == "true"


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
    raise UpstreamDownloadError(
        f"download failed after 3 attempts: {url}: {last_error}"
    )


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


def _publish_temporary_file(temporary: Path, destination: Path, mode: int) -> None:
    """Publish one temporary file and remove it after every failure mode."""
    try:
        temporary.chmod(mode)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _download_archive(destination: Path, name: str, expected: str) -> None:
    """Download and verify one pinned elbencho archive atomically."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = _asset_url(name)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
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
        actual = _sha256(temporary)
        if actual != expected:
            raise IntegrationTestError(
                f"checksum mismatch for {name}: expected {expected}, got {actual}"
            )
        _publish_temporary_file(temporary, destination, 0o640)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _extract_container_elbencho(
    runner: Any, cache: Path, binary: Path, runtime: Path, architecture: str
) -> None:
    """Build a portable wrapper from the digest-pinned upstream image."""
    docker_architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(architecture)
    if docker_architecture is None:
        raise IntegrationTestError(f"unsupported elbencho architecture: {architecture}")
    try:
        ensure_pinned_image(runner, ELBENCHO_CONTAINER, docker_architecture)
    except AcquisitionError as error:
        raise IntegrationTestError(str(error)) from error
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
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(document, handle, sort_keys=True)
            handle.write("\n")
        _publish_temporary_file(temporary, path, 0o640)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


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


def _ensure_container_elbencho(
    runner: Any, cache: Path, architecture: str, binary_name: str
) -> tuple[Path, str, Path | None]:
    """Reuse the pinned container runtime without overwriting the static binary."""
    cache = cache / "container"
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / f"{ELBENCHO_VERSION}-{binary_name}"
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


def _ensure_elbencho(
    runner: Any, config: Any, architecture: str, storage_backend: str
) -> tuple[Path, str, Path | None]:
    """Acquire pinned elbencho, permitting a container backup outside CI."""
    archive_name, expected, binary_name = ELBENCHO_ARCHIVES[architecture]
    cache = config.state_dir / "test-cache"
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / f"{ELBENCHO_VERSION}-{binary_name}"
    if storage_backend == "sbx-shared":
        return _ensure_container_elbencho(runner, cache, architecture, binary_name)
    archive = cache / f"{ELBENCHO_VERSION}-{archive_name}"
    if not archive.is_file() or _sha256(archive) != expected:
        LOG.info(
            "Downloading pinned elbencho %s for %s", ELBENCHO_VERSION, architecture
        )
        try:
            _download_archive(archive, archive_name, expected)
        except UpstreamDownloadError as error:
            if _in_github_actions():
                raise
            LOG.warning(
                "Could not download the harness's elbencho archive: %s; "
                "using the pinned upstream container instead",
                error,
            )
            return _ensure_container_elbencho(runner, cache, architecture, binary_name)
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
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(dir=cache, delete=False) as handle:
                temporary = Path(handle.name)
                shutil.copyfileobj(source, handle)
            _publish_temporary_file(temporary, binary, 0o755)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
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


def _same_namespace_targets(fixture: Fixture) -> tuple[KubectlStorageTarget, ...]:
    """Return the primary claim and the second claim in its namespace."""
    extra = getattr(fixture, "kubectl_same_namespace_target", None)
    if extra is None:
        raise IntegrationTestError(
            "the Kubernetes fixture has no second claim in its primary "
            "namespace; re-run integration-test.py setup to provision it"
        )
    primary = KubectlStorageTarget(
        fixture.kubectl_namespace, fixture.kubectl_pv, fixture.kubectl_pvc
    )
    return (primary, extra)


def _kubectl_target_env_lines(
    fixture: Fixture, multi_target: bool, same_namespace: bool = False
) -> tuple[str, ...]:
    """Select the default namespace and the fixture's sequencer image.

    TEST_DIRS keys name their claims and the scenarios' target files name
    their namespaces, so env.sh carries only KUBECTL_NAMESPACE and
    KUBECTL_SEQUENCER_IMAGE. Every Kubernetes scenario names the fixture's
    preloaded private alias, so validate_env.sh's default image check pulls it
    under the fixture's Never pull policy; only a batch spanning namespaces
    runs the sequencer.
    """
    if same_namespace:
        _same_namespace_targets(fixture)
    elif multi_target and len(tuple(getattr(fixture, "kubectl_targets", ()))) < 2:
        raise IntegrationTestError(
            "the Kubernetes fixture has no second storage target; re-run "
            "integration-test.py setup to provision it"
        )
    if not fixture.kubectl_sequencer_image:
        raise StorageTargetError(
            "the sequencer image must be digest-qualified: the fixture has none"
        )
    return kubectl_target_env_lines(
        fixture.kubectl_namespace, fixture.kubectl_sequencer_image
    )


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
    multi_target: bool = False,
    same_namespace: bool = False,
) -> tuple[str, dict[str, str]]:
    """Return template overrides and small support-file contents."""
    data = logical_test_root if selector == "kubectl" else "/mnt/storage-test"
    if selector == "kubectl":
        # Kubernetes roots name their claim: ``claim:path``.
        data = f"{fixture.kubectl_pvc}:{data or 'primary'}"
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
        # Notice finished Slurm jobs within a second instead of 15.
        "export SLURM_JOB_POLL_INTERVAL_SECONDS=1",
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
                *_kubectl_target_env_lines(fixture, multi_target, same_namespace),
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
    multi_target: bool = False,
    same_namespace: bool = False,
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
        multi_target=multi_target,
        same_namespace=same_namespace,
    )
    rendered = text.replace(anchor, overrides + "\n" + anchor)
    return rendered, support


_REQUIRED_SOURCE_FILES = frozenset(
    {
        "storage-scale-test/NOTICE",
        "storage-scale-test/env.sh.template",
        "storage-scale-test/validate_env.sh",
        "storage-scale-test/lib/env_base.sh",
        "storage-scale-test/storage-tests/fs/nv-elbencho-sweep.sh",
        "storage-scale-test/storage-tests/fs/nv-mdtest-elbencho.sh",
    }
)


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
    required = {*_REQUIRED_SOURCE_FILES, f"storage-scale-test/utils/{binary_name}"}
    if bundled_runtime:
        required.add("storage-scale-test/utils/elbencho-runtime/usr/bin/elbencho")
    root = _extract_checked_archive(archive, destination, required)
    _check_packaged_elbencho(
        root, binary_name, binary_digest, architecture, runner, bundled_runtime
    )
    return root


def _extract_checked_archive(
    archive: Path, destination: Path, required: set[str] | frozenset[str]
) -> Path:
    """Safely extract a storage-scale-test archive holding the required files."""
    if not archive.is_file() or archive.stat().st_size > MAX_DEPLOYMENT_ARCHIVE_BYTES:
        raise IntegrationTestError(
            f"deployment archive is absent or oversized: {archive}"
        )
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
    return destination / "storage-scale-test"


def _check_packaged_elbencho(
    root: Path,
    binary_name: str,
    binary_digest: str,
    architecture: str,
    runner: Any,
    bundled_runtime: bool,
) -> None:
    """Require the tree's elbencho to be the pinned binary for this host."""
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
    _check_elbencho_executable(root, binary_name, architecture, runner, bundled_runtime)


def _check_elbencho_executable(
    root: Path,
    binary_name: str,
    architecture: str,
    runner: Any,
    bundled_runtime: bool,
) -> None:
    """Check an already verified binary's executable bit and host architecture."""
    packaged_binary = root / "utils" / binary_name
    if (
        not packaged_binary.is_file()
        or packaged_binary.is_symlink()
        or not os.access(packaged_binary, os.X_OK)
    ):
        raise IntegrationTestError(
            f"packaged elbencho is not executable: {packaged_binary}"
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


# The SSH baseline runs from a release source archive instead of the
# deployment archive, as a user who downloads a release would.
RELEASE_SOURCE_SCENARIO = ("baseline", "ssh")


def _prepare_release_source_tree(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    runner: Any,
    repo_root: Path,
    build_root: Path,
    log_dir: Path,
    binary: Path,
    binary_name: str,
    architecture: str,
    runtime: Path | None,
    *,
    download: bool = True,
) -> Path:
    """Unpack a release source archive and add elbencho as a user would.

    NFS always attempts utils/build/fetch_elbencho.sh, even when the harness
    itself needed the container backup. SBX copies its container runtime.
    """
    try:
        archive = build_release_source_archive(
            runner, repo_root, build_root / "release-source"
        )
    except DeploymentCacheError as error:
        raise IntegrationTestError(str(error)) from error
    root = _extract_checked_archive(
        archive,
        build_root / "release-source" / "extracted",
        {*_REQUIRED_SOURCE_FILES, "storage-scale-test/VERSION"},
    )
    if download:
        copied = _fetch_release_elbencho(
            runner, root, log_dir, binary, binary_name, runtime
        )
    else:
        _copy_harness_elbencho(root, binary, binary_name, runtime)
        copied = True
    if copied or runtime is None:
        _check_packaged_elbencho(
            root,
            binary_name,
            _sha256(binary),
            architecture,
            runner,
            copied and runtime is not None,
        )
    else:
        # fetch_elbencho.sh verified the static archive's pinned checksum.
        # It is intentionally a different binary from our container backup.
        _check_elbencho_executable(root, binary_name, architecture, runner, False)
    _require_release_version(runner, root)
    LOG.info("Prepared release source tree %s", archive.name)
    return root


def _fetch_release_elbencho(
    runner: Any,
    root: Path,
    log_dir: Path,
    binary: Path,
    binary_name: str,
    runtime: Path | None = None,
) -> bool:
    """Download elbencho into a release tree with utils/build/fetch_elbencho.sh.

    GitHub Actions must download it. Elsewhere a network that blocks GitHub
    release assets falls back to the harness's own binary, so the scenario
    still runs. Return whether the harness binary was copied.
    """
    required = _in_github_actions()
    fetched = runner.run(
        [root / "utils/build/fetch_elbencho.sh"], timeout=600, check=required
    )
    log = log_dir / "fetch-elbencho.log"
    log.write_text(fetched.stdout + fetched.stderr, encoding="utf-8")
    if fetched.returncode:
        LOG.warning(
            "fetch_elbencho.sh could not download elbencho (see %s); "
            "copying the harness's binary into the release tree instead",
            log,
        )
        _copy_harness_elbencho(root, binary, binary_name, runtime)
        return True
    return False


def _copy_harness_elbencho(
    root: Path, binary: Path, binary_name: str, runtime: Path | None
) -> None:
    """Copy the harness binary together with any libraries its wrapper needs."""
    shutil.copy2(binary, root / "utils" / binary_name)
    (root / "utils" / binary_name).chmod(0o755)
    if runtime is not None:
        shutil.copytree(runtime, root / "utils" / "elbencho-runtime")


def _require_release_version(runner: Any, root: Path) -> str:
    """Require the tree to report its stamped version, and an edit to show."""
    version = (root / "VERSION").read_text(encoding="utf-8").strip()
    resolver = root / "lib" / "project_version.sh"
    resolved = runner.run(["bash", resolver, "resolve", root], timeout=60)
    if resolved.stdout.strip() != version:
        raise IntegrationTestError(
            f"release source tree reports {resolved.stdout.strip()!r}, "
            f"not its stamped {version!r}: {resolved.stderr.strip()}"
        )
    with tempfile.TemporaryDirectory(dir=root.parent) as temporary:
        edited = Path(temporary) / root.name
        shutil.copytree(root, edited, symlinks=True)
        with (edited / "NOTICE").open("a", encoding="utf-8") as notice:
            notice.write("edited\n")
        reported = runner.run(["bash", resolver, "resolve", edited], timeout=60)
    expected = version.removesuffix("-modified") + "-modified"
    if (
        reported.stdout.strip() != expected
        or "do not match its SOURCE_SHA256" not in reported.stderr
    ):
        raise IntegrationTestError(
            f"an edited release source tree was not reported as modified: "
            f"{reported.stdout.strip()!r} {reported.stderr.strip()!r}"
        )
    return version


def _require_release_reports(log_dir: Path, version: str) -> None:
    """Require the scenario's reports to name the release as producer and reporter."""
    logs = sorted(log_dir.glob("report-*/extract-elbencho.log"))
    if not logs:
        raise IntegrationTestError(
            f"release source scenario wrote no reports: {log_dir}"
        )
    for log in logs:
        text = log.read_text(encoding="utf-8")
        for role in ("Produced", "Reported"):
            # Compare whole values: a mixed or suffixed version must fail.
            values = re.findall(
                rf"^(?:- )?{role} by storage-scale-test: (.*)$", text, re.MULTILINE
            )
            if not values or any(value != version for value in values):
                raise IntegrationTestError(
                    f"{log}: report says it was {role.lower()} by "
                    f"{values or 'no version'}, not release {version}"
                )


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
    multi_target: bool = False,
    same_namespace: bool = False,
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
        multi_target=multi_target,
        same_namespace=same_namespace,
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


def _scenario_storage_targets(
    fixture: Fixture, runtime: ScenarioRuntime
) -> tuple[KubectlStorageTarget | None, ...]:
    """Return the storage targets whose claims a Kubernetes scenario touches.

    Single-target scenarios return one None, meaning the primary claim, which
    keeps their commands identical to before multi-target support.
    """
    scenario = getattr(runtime, "scenario", None)
    wanted = getattr(scenario, "storage_targets", 1)
    if wanted <= 1:
        return (None,)
    if getattr(scenario, "same_namespace", False):
        return _same_namespace_targets(fixture)
    targets = tuple(getattr(fixture, "kubectl_targets", ()))
    if len(targets) < wanted:
        raise IntegrationTestError(
            f"{runtime.scenario.name} needs {wanted} Kubernetes storage targets "
            f"but the fixture has {len(targets)}; re-run integration-test.py setup"
        )
    return targets[:wanted]


def for_each_target(action: Callable[[Any], object], targets: Iterable[Any]) -> None:
    """Call *action* on every storage target concurrently and wait for all.

    Each call is an independent utility Pod (often in another namespace), so
    running them together costs one Pod start instead of one per target. The
    first failure, in target order, is raised once every call has finished.
    """
    targets = tuple(targets)
    if len(targets) <= 1:
        for target in targets:
            action(target)
        return
    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = [pool.submit(action, target) for target in targets]
    for future in futures:
        future.result()


def _prepare_kubectl_scenario_data(
    runner: Any,
    config: Any,
    fixture: Fixture,
    data_root: str,
    target: KubectlStorageTarget | None = None,
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
    _storage_utility_shell(runner, config, fixture, command, target=target)


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
    if runtime.selector == "kubectl":
        runtime.values["kubectl_result_base"] = result_base
    extra_env = step.render_env(runtime.values)
    if runtime.selector == "kubectl" and runtime.scenario.name in {
        "failure-resume",
        "mdtest-sweep",
        "mixed-batch",
    }:
        # The product copies this integration-only executable into the control
        # bundle before creating the Job.  It fails exactly one reified cell;
        # the resume operation deliberately unsets the hook while selecting
        # only unfinished cells.
        target_ids = [
            execution_id
            for execution_id, execution in enumerate(step.executions)
            if execution.status is ExecutionStatus.FAILED
        ]
        if target_ids or runtime.scenario.name == "mixed-batch":
            target_id = (
                "0002"
                if runtime.scenario.name == "mixed-batch"
                else f"{target_ids[0] + 1:04d}"
            )
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
            multi_target=getattr(runtime.scenario, "storage_targets", 1) > 1,
            same_namespace=getattr(runtime.scenario, "same_namespace", False),
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
        storage_path = path
        if runtime.selector == "kubectl":
            claim = runtime.values.get("kubectl_claim", STORAGE_CLAIM)
            storage_path = f"{kubectl_claim_mount(claim)}/{path}"
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
        f"-name '{'mdtest-elbencho' if step.workload_kind == 'mdtest' else 'elbencho'}-*' "
        "-printf '%p\\n'",
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
    if runtime.selector in {"ssh", "kubectl"}:
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


def _mdtest_coordinate_from_execution(path: Path) -> tuple[str, int, int]:
    """Return the explicit workload kind, node count, and task count."""
    values = _shell_assignments(path)
    try:
        return (
            values["ELBENCHO_EXECUTION_KIND"],
            int(values["nodes"]),
            int(values["tasks_per_node"]),
        )
    except (KeyError, ValueError) as error:
        raise IntegrationTestError(
            f"invalid MD execution metadata in {path}"
        ) from error


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
    """Return exact bounded totals for workloads with a metadata contract.

    File sizes come from the scenario definitions, never from the results.
    """
    if scenario in {"baseline", "ssh-shared-home"}:
        return nodes, nodes * 16 * 1024 * 1024
    if scenario == "failure-resume":
        return nodes, nodes * SMALL_DIO_FILE_BYTES
    if scenario == "live-capture":
        return nodes * 2, nodes * (MAX_LIVE_CAPTURE_DATASET_BYTES // 2)
    if scenario == "slurm-cartesian":
        return nodes * 2, nodes * 2 * SMALL_DIO_FILE_BYTES
    if (
        scenario in {"retained-lifecycle", "kubectl-retained-read"}
        and step.kind is not CommandKind.DELETE
    ):
        return 1, SMALL_DIO_FILE_BYTES
    return None


def _assert_mdtest_result_artifacts(
    result: Path, execution_root: Path, execution_id: str, nodes: int, tasks: int
) -> None:
    """Require a completed MD cell to publish raw data and its commit marker."""
    for suffix in ("csv", "out"):
        matches = sorted(
            result.glob(f"mdtest-elbencho-c_{nodes:03d}-t_{tasks:03d}_*_iter1.{suffix}")
        )
        if (
            len(matches) != 1
            or not matches[0].is_file()
            or not matches[0].stat().st_size
        ):
            raise IntegrationTestError(
                f"missing unique nonempty MD {suffix} artifact for "
                f"nodes={nodes}, tasks={tasks}: {matches} ({execution_root})"
            )
    marker = execution_root / f"{execution_id}.mdtest.complete"
    if not marker.is_file() or marker.read_text(encoding="utf-8") != "COMPLETE\n":
        raise IntegrationTestError(f"missing MD completion marker: {marker}")


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
    if getattr(step, "workload_kind", "filesystem") == "mdtest":
        actual_coordinates = [
            _mdtest_coordinate_from_execution(path) for path in scripts
        ]
        expected_coordinates = [
            ("mdtest", item.coordinate.nodes, item.coordinate.tasks_per_node)
            for item in step.executions
        ]
    else:
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
        if getattr(step, "workload_kind", "filesystem") == "mdtest":
            if expected.status is ExecutionStatus.SUCCESS:
                _assert_mdtest_result_artifacts(
                    result,
                    execution_root,
                    execution_id,
                    expected.coordinate.nodes,
                    expected.coordinate.tasks_per_node,
                )
            continue
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
    """Validate cleanup or retention only within scenario-owned paths.

    On Kubernetes (*storage_prefix* set) paths are claim-qualified logical paths
    or Pod paths; the check runs in a utility Pod mounting every claim they name.
    """

    def storage_path(path: str) -> str:
        return kubectl_storage_path(path) if storage_prefix else path

    def storage_shell(command: str, paths: list[str], **kwargs: Any) -> str:
        if not storage_prefix:
            return _storage_shell(runner, config, fixture, command, **kwargs)
        claims = sorted({parse_kubectl_location(path)[1] for path in paths})
        return _storage_utility_shell(
            runner, config, fixture, command, claims=claims, **kwargs
        )

    if step.dataset is DatasetExpectation.PRESERVED:
        if retained_path:
            retained_storage_path = storage_path(retained_path)
            retained_parent = str(PurePosixPath(retained_storage_path).parent)
            storage_shell(
                "for _ in $(seq 1 30); do "
                f"test -e {_shell(retained_storage_path)} && exit 0; sleep 1; done; "
                "{ "
                f"echo 'missing retained dataset: ' {_shell(retained_storage_path)} >&2; "
                f"find {_shell(retained_parent)} -maxdepth 2 -printf '%y %p %s\\n' "
                "2>&1 >&2 || true; exit 1; }",
                [retained_path],
                node_name=storage_node,
            )
        return
    paths = list(_execution_targets(result)) if result is not None else []
    if step.dataset is DatasetExpectation.REMOVED and retained_path:
        paths.append(retained_path)
    if not paths:
        return
    probes = "\n".join(f"test ! -e {_shell(storage_path(path))}" for path in paths)
    storage_shell(f"set -euo pipefail\n{probes}", paths)


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
        required = ("--direct", "--dirs=2", "--write", "--read")
        forbidden = ("--norandalign", "--timelimit", "--infloop")
        if step.name == "explicit-completion-based":
            required += ("--files=3", f"--size={SMALL_DIO_FILE_SIZE}")
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
    extractor = (
        "extract-mdtest-elbencho.py"
        if step.workload_kind == "mdtest"
        else "extract-elbencho.py"
    )
    command: list[str | Path] = [
        report_workspace / "utils" / extractor,
        "--markdown",
    ]
    if runtime.scenario.name == "live-capture":
        command.extend(
            (
                "--client-min-slow-windows",
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
    if step.workload_kind == "mdtest":
        missing_rows = [
            (item.coordinate.nodes, item.coordinate.tasks_per_node)
            for item in step.executions
            if not re.search(
                rf"^\|\s*{item.coordinate.nodes}\s*\|\s*"
                rf"{item.coordinate.tasks_per_node}\s*\|",
                output,
                re.MULTILINE,
            )
        ]
        missing_operations = [
            heading
            for heading in (
                "| Nodes | Threads | Iters | Create/s | Stat/s | Delete/s |",
                "Create Latency Histogram",
                "Stat Latency Histogram",
                "Delete Latency Histogram",
            )
            if heading not in output
        ]
    else:
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
        missing_operations = [
            item for item in required_operations if item not in output
        ]
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
    if runtime.scenario.name == "live-capture":
        _assert_live_report(runner, report_workspace, result, report_dir, output)


def _assert_live_report(
    runner: Any, report_workspace: Path, result: Path, report_dir: Path, output: str
) -> None:
    """Require live reports from real elbencho output, without judging values.

    Two nodes run as two elbencho services with a 10 ms live interval.
    """
    problems = []
    if LIVE_FAIRNESS_HEADING not in output:
        problems.append(f"report lacks {LIVE_FAIRNESS_HEADING!r}")
    for phase in LIVE_PHASES:
        for suffix in ("aggregate-timeseries.png", "client-summary.csv"):
            if not list(result.glob(f"*-{phase}-{suffix}")):
                problems.append(f"no {phase} {suffix}")
    json_dir = report_dir / "json"
    completed = runner.run(
        [
            report_workspace / "utils/extract-filesystem.py",
            result,
            "--output-dir",
            json_dir,
            "--to-json",
        ],
        timeout=300,
        check=False,
    )
    (report_dir / "extract-filesystem.log").write_text(
        completed.stdout + completed.stderr, encoding="utf-8"
    )
    if completed.returncode:
        problems.append("extract-filesystem.py --to-json failed")
    else:
        document = json.loads((json_dir / "results.json").read_text("utf-8"))
        problems.extend(_live_object_problems(document))
    if problems:
        raise IntegrationTestError("live-capture report: " + "; ".join(problems))


def _live_object_problems(document: dict[str, Any]) -> list[str]:
    """Problems with the write and read live objects of every execution."""
    problems = []
    for execution in document.get("executions", ()):
        phases = {
            phase.get("phase"): phase.get("live")
            for phase in (execution.get("results") or {}).get("phases", ())
        }
        for name in LIVE_PHASES:
            label = f"execution {execution.get('execution_id')} {name}"
            live = phases.get(name)
            if not isinstance(live, dict) or "steady" not in live:
                problems.append(f"{label}: no live analysis ({live!r})")
                continue
            # ELBENCHO_LIVEINT reaches the report through the env snapshot.
            expected = {
                "client_mode": "service",
                "clients": 2,
                "configured_interval_ms": 10,
            }
            for key, value in expected.items():
                if live.get(key) != value:
                    problems.append(f"{label}: {key} {live.get(key)!r}")
            # No end_source means elbencho's [first] time was not found.
            if live["steady"].get("end_source") != "elbencho_first":
                problems.append(f"{label}: steady {live['steady']!r}")
            latency = live.get("aggregate_series", {}).get("io_latency_avg_us", ())
            if not any(isinstance(value, (int, float)) for value in latency):
                problems.append(f"{label}: no Lat IO us samples")
    if not document.get("executions"):
        problems.append("no executions")
    return problems


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


def _run_kubectl_validate_env(
    runner: Any, runtime: ScenarioRuntime, kubeconfig: Path, *, sequencer: bool = False
) -> Any:
    """Run the scenario workspace's validate_env.sh as the kubeconfig's identity.

    With *sequencer* it also checks the sequencer (``--sequencer``).
    """
    return runner.run(
        [
            "env",
            f"KUBECONFIG={kubeconfig}",
            Path(runtime.workspace) / "validate_env.sh",
            *((VALIDATE_SEQUENCER_OPTION,) if sequencer else ()),
        ],
        cwd=Path(runtime.workspace),
        check=False,
        timeout=180,
    )


def _validate_step_environment(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    step: ScenarioStep,
    log_dir: Path,
    *,
    sequencer: bool = False,
) -> str:
    """Run the repository's validator for a rendered scenario environment.

    Only a step marked ``validate_env`` (one per configuration the validator
    treats differently) runs it; for any other step this returns "". With
    *sequencer* (Kubernetes only) it also checks the sequencer. Returns the
    validator's output.
    """
    if not step.validate_env:
        return ""
    if runtime.selector == "kubectl":
        result = _run_kubectl_validate_env(
            runner, runtime, config.kubeconfig, sequencer=sequencer
        )
        output = result.stdout + result.stderr
        path = log_dir / f"kubectl-{step.name}-validate-env.log"
        path.write_text(output, encoding="utf-8")
        if result.returncode or VALIDATION_SUCCESS not in output:
            raise IntegrationTestError(
                f"{runtime.scenario.name}/{runtime.selector}/{step.name}: "
                f"environment validation failed; full output: {path}"
            )
        return output
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
    return output


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
    """Convert a collected path to the public ``[namespace/]claim:path`` form.

    Single-root modes (``--read-from``, ``--delete-only``) take claim-qualified
    logical paths on Kubernetes. This is the one place that decides that form.
    """
    namespace, claim, relative = parse_kubectl_location(path)
    prefix = f"{namespace}/" if namespace else ""
    return f"{prefix}{claim}:{relative}"


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


LIVE_FAIRNESS_HEADING = "Live fairness"
LIVE_PHASES = ("write", "read")


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
    is_mdtest_resume = (
        step.workload_kind == "mdtest" and step.kind is CommandKind.RESUME
    )
    if is_mdtest_resume:
        result_base = runtime.values["mdtest_results_dir"]
    else:
        if step.kind is CommandKind.RESUME:
            raise IntegrationTestError(
                "filesystem resume steps require the failure scenario runner"
            )
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
    sweep_script = (
        "nv-mdtest-elbencho.sh"
        if step.workload_kind == "mdtest"
        else "nv-elbencho-sweep.sh"
    )
    command = (
        f"cd -- {_shell(runtime.workspace)} && "
        f"./storage-tests/fs/{sweep_script} {arguments}"
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
    remote_result = (
        result_base
        if is_mdtest_resume
        else _discover_result(
            runner,
            config,
            fixture,
            runtime,
            step,
            result_base,
            log_dir,
        )
    )
    if step.workload_kind == "mdtest" and step.kind is CommandKind.SWEEP:
        runtime.values["mdtest_results_dir"] = remote_result
    local_result = _copy_scenario_result(
        runner, config, fixture, runtime, step, remote_result
    )
    _assert_execution_contract(runtime.scenario, step, local_result)
    if step.workload_kind != "mdtest":
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


def _scenario_keys(fixture: Fixture, selector: str, data_root: str) -> dict[str, str]:
    """Return the TEST_DIRS keys scenario templates render.

    SSH and Slurm roots are plain paths. Kubernetes roots name their claim as
    ``claim:path`` (``namespace/claim:path`` for the second namespace's claim),
    and the second claim of the primary namespace is addressed the same way.
    """
    primary = f"{data_root}/primary"
    secondary = f"{data_root}/secondary"
    if selector != "kubectl":
        return {"test_key": primary, "test_key_secondary": secondary}
    claim = getattr(fixture, "kubectl_pvc", STORAGE_CLAIM)
    keys = {
        "kubectl_claim": claim,
        "test_key": f"{claim}:{primary}",
        "test_key_secondary": f"{claim}:{secondary}",
        "test_key_claim2": f"{SAME_NAMESPACE_CLAIM}:{primary}",
        "test_key_claim2_secondary": f"{SAME_NAMESPACE_CLAIM}:{secondary}",
    }
    namespace = getattr(fixture, "kubectl_namespace", "")
    if namespace:
        keys["test_key_ns1"] = f"{namespace}/{claim}:{primary}"
    targets = tuple(getattr(fixture, "kubectl_targets", ()))
    if len(targets) > 1:
        second = targets[1]
        keys["test_key_ns2"] = f"{second.namespace}/{second.pvc}:{primary}"
    return keys


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
        **_scenario_keys(fixture, selector, data_root),
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

        def prepare(target: KubectlStorageTarget | None) -> None:
            claim = target.pvc if target is not None else fixture.kubectl_pvc
            _prepare_kubectl_scenario_data(
                runner,
                config,
                fixture,
                f"{kubectl_claim_mount(claim)}/{runtime.data_root}",
                target,
            )

        for_each_target(prepare, _scenario_storage_targets(fixture, runtime))
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
    kubeconfig: Path | None = None,
    workload_kind: str | None = None,
) -> str:
    """Run one local kubectl-sweep command with the fixture kubeconfig."""
    command: list[str | Path] = [
        "env",
        f"KUBECONFIG={kubeconfig or config.kubeconfig}",
        "timeout",
        "--kill-after=15s",
        f"{timeout}s",
        Path(runtime.workspace)
        / "storage-tests"
        / "fs"
        / (
            "nv-mdtest-elbencho.sh"
            if (workload_kind or _runtime_workload_kind(runtime)) == "mdtest"
            else "nv-elbencho-sweep.sh"
        ),
        *arguments,
    ]
    LOG.info("Running kubectl filesystem step: %s", name)
    try:
        result = runner.run(
            command,
            cwd=Path(runtime.workspace),
            check=False,
            timeout=timeout + 30,
        )
    except (OSError, subprocess.SubprocessError):
        if not any(
            argument in {"--status", "--cancel", "--collect", "--resume"}
            for argument in arguments
        ):
            _arm_incomplete_kubectl_submit_cleanup(runtime)
        raise
    output = result.stdout + result.stderr
    log_path = log_dir / f"kubectl-{name}.log"
    log_path.write_text(output, encoding="utf-8")
    log_path.chmod(0o640)
    if accepted_states:
        try:
            observed_state = _kubectl_lifecycle_state(output)
        except IntegrationTestError as error:
            raise IntegrationTestError(
                f"kubectl {name} did not report an accepted terminal state; "
                f"full output: {log_path}"
            ) from error
        if observed_state not in accepted_states or result.returncode == 0:
            raise IntegrationTestError(
                f"kubectl {name} must return nonzero with one of "
                f"{sorted(accepted_states)}, got exit code {result.returncode} and "
                f"state {observed_state}; full output: {log_path}"
            )
        return output
    if expected_failure and result.returncode == 0:
        raise IntegrationTestError(
            f"kubectl {name} unexpectedly succeeded; full output: {log_path}"
        )
    if result.returncode and not expected_failure:
        if not any(
            argument in {"--status", "--cancel", "--collect", "--resume"}
            for argument in arguments
        ):
            _arm_incomplete_kubectl_submit_cleanup(runtime)
        raise IntegrationTestError(
            f"kubectl {name} failed with exit code {result.returncode}; full "
            f"output: {log_path}\n{output.strip()[-8000:]}"
        )
    return output


def _arm_incomplete_kubectl_submit_cleanup(runtime: ScenarioRuntime) -> None:
    """Retain the exact local lifecycle root after an interrupted submit."""
    values = getattr(runtime, "values", None)
    if not isinstance(values, dict) or values.get("kubectl_result_root"):
        return
    result_base = values.get("kubectl_result_base")
    if not result_base:
        return
    base = Path(result_base)
    result_prefix = (
        "mdtest-elbencho-*"
        if _runtime_workload_kind(runtime) == "mdtest"
        else "elbencho-*"
    )
    candidates = [
        path
        for path in base.glob(result_prefix)
        if path.is_dir()
        and not path.is_symlink()
        and (path / "kubernetes" / "current-attempt").is_file()
    ]
    if len(candidates) == 1:
        values["kubectl_result_root"] = str(candidates[0])


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
    state = _kubectl_output_value(output, "STATE")
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


def _kubectl_attempt_digest(result_root: Path, attempt_id: str) -> str:
    """Hash durable attempt metadata, excluding supplemental diagnostics."""
    attempt = result_root / "kubernetes" / "attempts" / attempt_id
    digest = hashlib.sha256()
    for path in sorted(attempt.rglob("*")):
        relative = path.relative_to(attempt)
        if relative.parts and relative.parts[0] == "diagnostics":
            continue
        if path.is_symlink():
            raise IntegrationTestError(f"unexpected lifecycle symlink: {path}")
        if path.is_file():
            digest.update(str(relative).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def kubectl_control_root(roots: Sequence[str]) -> str:
    """Return the Pod path of the control root for an attempt's roots.

    The control root lives on the claim of the lexically first normalized root
    (``namespace/claim:path``; give every root the same form) of the attempt's
    namespace, as ``<claim mount>/<path>/.storage-scale-test``.
    """
    _, claim, path = parse_kubectl_location(min(roots))
    root = kubectl_claim_mount(claim)
    if path:
        root = f"{root}/{path}"
    return f"{root}/{KUBECTL_CONTROL_DIRECTORY}"


def _kubectl_remote_control_root(runtime: ScenarioRuntime) -> str:
    """Return the deterministic control root of the scenario's attempt.

    A scenario whose roots span claims records the root in
    ``kubectl_control_root``; the others use their single claim's roots.
    """
    recorded = runtime.values.get("kubectl_control_root")
    if recorded:
        return recorded
    claim = runtime.values.get("kubectl_claim", STORAGE_CLAIM)
    return kubectl_control_root(
        [
            f"{claim}:{runtime.values[key]}"
            for key in ("test_root", "test_root_secondary")
            if runtime.values.get(key)
        ]
    )


def _assert_kubectl_pvc_lease(
    runner: Any,
    config: Any,
    result_root: Path,
    attempt_id: str,
    *,
    present: bool,
    claims: int = 1,
) -> None:
    """Verify an attempt's per-claim Leases exist only while it owns them.

    Leases are found by the attempt's label rather than by its identity file,
    so the check does not depend on that file's layout.
    """
    del result_root
    observed = runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "get",
            "leases.coordination.k8s.io",
            "-l",
            _attempt_selector(attempt_id, LEASE_COMPONENT),
            "-o",
            'jsonpath={range .items[*]}{.metadata.uid}{"\\n"}{end}',
        ),
        check=True,
        timeout=30,
    ).stdout.split()
    if present and len(observed) != claims:
        raise IntegrationTestError(
            f"kubectl PVC Leases are incomplete: {observed!r}, expected {claims}"
        )
    if not present and observed:
        raise IntegrationTestError(f"kubectl collection retained PVC Lease: {observed}")


def token_kubeconfig_document(document: dict[str, Any], token: str) -> dict[str, Any]:
    """Return a kubeconfig document whose one user authenticates with *token*.

    The cluster, context, and certificate authority are kept, so the new
    identity reaches the same API server.
    """
    users = document.get("users")
    if not isinstance(users, list) or len(users) != 1:
        raise IntegrationTestError(
            "fixture kubeconfig does not select exactly one user"
        )
    return {**document, "users": [{**users[0], "user": {"token": token}}]}


def _write_token_kubeconfig(
    runner: Any, config: Any, destination: Path, token: str
) -> None:
    """Write a private kubeconfig that keeps cluster trust but uses *token*."""
    result = runner.run(
        _kubectl(
            config, "config", "view", "--raw", "--minify", "--flatten", "-o", "json"
        ),
        check=True,
        timeout=30,
    )
    document = token_kubeconfig_document(json.loads(result.stdout), token)
    destination.write_text(json.dumps(document), encoding="utf-8")
    destination.chmod(0o600)


def _make_unauthorized_kubeconfig(runner: Any, config: Any, destination: Path) -> None:
    """Preserve cluster trust while replacing the client credential."""
    _write_token_kubeconfig(
        runner, config, destination, "storage-scale-test-intentionally-invalid"
    )


def _assert_expired_kubectl_status_is_observational(
    runner: Any,
    config: Any,
    runtime: ScenarioRuntime,
    result_root: Path,
    attempt_id: str,
    log_dir: Path,
) -> None:
    """Simulated expired credentials fail consistently without local mutation."""
    bad_kubeconfig = log_dir / "kubectl-expired-credential.json"
    _make_unauthorized_kubeconfig(runner, config, bad_kubeconfig)
    before = _kubectl_attempt_digest(result_root, attempt_id)
    envelopes: list[tuple[str, str, str]] = []
    for index in (1, 2):
        output = _run_kubectl_command(
            runner,
            config,
            runtime,
            f"expired-status-{index}",
            ("--status", str(result_root)),
            log_dir,
            60,
            expected_failure=True,
            kubeconfig=bad_kubeconfig,
        )
        fields = tuple(
            _kubectl_output_value(output, key)
            for key in (
                "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON",
                "STORAGE_SCALE_TEST_DIAGNOSTIC_OPERATION",
                "STORAGE_SCALE_TEST_DIAGNOSTIC_SAFE_NEXT_ACTION",
            )
        )
        if fields[0] != "AUTH":
            raise IntegrationTestError(
                f"expired kubectl credential was classified as {fields[0]}"
            )
        envelopes.append(fields)
    if envelopes[0] != envelopes[1]:
        raise IntegrationTestError("expired-credential diagnostics were not stable")
    if _kubectl_attempt_digest(result_root, attempt_id) != before:
        raise IntegrationTestError(
            "failed authenticated status mutated attempt metadata"
        )


def _interrupt_kubectl_collection(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    result_root: Path,
    attempt_id: str,
    log_dir: Path,
) -> None:
    """Interrupt one real collector at its explicit pre-stream test boundary."""
    environment = os.environ.copy()
    environment["KUBECONFIG"] = str(config.kubeconfig)
    environment["STORAGE_SCALE_TEST_INTEGRATION"] = "1"
    hold_file = result_root / ".integration-kubectl-collection-ready"
    environment["KUBECTL_INTEGRATION_COLLECTION_HOLD_FILE"] = str(hold_file)
    interrupted_log = log_dir / "kubectl-collect-interrupted.log"
    command = [
        "timeout",
        "--foreground",
        "--kill-after=15s",
        "180s",
        str(Path(runtime.workspace) / "storage-tests/fs/nv-elbencho-sweep.sh"),
        "--collect",
        str(result_root),
    ]
    collection: subprocess.Popen[str] | None = None
    hold_file.unlink(missing_ok=True)
    with interrupted_log.open("w", encoding="utf-8") as output:
        try:
            collection = subprocess.Popen(
                command,
                cwd=runtime.workspace,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                env=environment,
                start_new_session=True,
            )
            deadline = time.monotonic() + 90
            while not hold_file.is_file() and collection.poll() is None:
                if time.monotonic() >= deadline:
                    raise IntegrationTestError(
                        "collector did not reach the pre-stream interruption boundary"
                    )
                time.sleep(0.1)
            if not hold_file.is_file() or collection.poll() is not None:
                raise IntegrationTestError(
                    "collector exited before the interruption boundary"
                )
            os.killpg(collection.pid, signal.SIGINT)
            hold_file.unlink(missing_ok=True)
            try:
                returncode = collection.wait(timeout=30)
            except subprocess.TimeoutExpired as error:
                os.killpg(collection.pid, signal.SIGKILL)
                collection.wait(timeout=10)
                raise IntegrationTestError(
                    "interrupted collector did not honor SIGINT"
                ) from error
            if returncode == 0:
                raise IntegrationTestError("interrupted collection reported success")
        finally:
            if collection is not None and collection.poll() is None:
                os.killpg(collection.pid, signal.SIGINT)
                try:
                    collection.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    os.killpg(collection.pid, signal.SIGKILL)
                    collection.wait(timeout=10)
            hold_file.unlink(missing_ok=True)
    interrupted_log.chmod(0o640)
    state_path = result_root / "kubernetes" / "attempts" / attempt_id / "state.sh"
    if state_path.read_text(encoding="utf-8").strip() != (
        "KUBECTL_LIFECYCLE_STATE=TERMINAL"
    ):
        raise IntegrationTestError(
            "interrupted collection did not retain the TERMINAL checkpoint"
        )
    if (state_path.parent / "collected-state").exists():
        raise IntegrationTestError(
            "interrupted pre-stream collection published collected state"
        )
    remote_run = f"{_kubectl_remote_control_root(runtime)}/runs/{attempt_id}"
    _storage_utility_shell(
        runner,
        config,
        fixture,
        f"test -d {shlex.quote(remote_run)}",
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
    _assert_kubectl_pvc_lease(runner, config, result_root, attempt_id, present=True)
    _assert_expired_kubectl_status_is_observational(
        runner, config, runtime, result_root, attempt_id, log_dir
    )
    _interrupt_kubectl_collection(
        runner, config, fixture, runtime, result_root, attempt_id, log_dir
    )
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
    _assert_kubectl_pvc_lease(runner, config, result_root, attempt_id, present=False)
    stale_staging = sorted(result_root.glob(".kubernetes-collect-*"))
    if stale_staging:
        raise IntegrationTestError(
            f"collection retry left staging paths: {stale_staging}"
        )
    helpers = runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "get",
            "pods",
            "-l",
            f"storage-scale-test.nvidia.com/run={attempt_id},"
            "app.kubernetes.io/component=transfer",
            "-o",
            "name",
        ),
        check=True,
        timeout=30,
    )
    if helpers.stdout.strip():
        raise IntegrationTestError(
            f"collection retry left a transfer helper: {helpers.stdout.strip()}"
        )
    result = _collected_result_root(result_root)
    _assert_execution_contract(runtime.scenario, step, result)
    if step.workload_kind != "mdtest":
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
    is_mdtest_resume = (
        step.workload_kind == "mdtest" and step.kind is CommandKind.RESUME
    )
    if is_mdtest_resume:
        result_base = runtime.values["mdtest_results_dir"]
    else:
        if step.kind is CommandKind.RESUME:
            raise IntegrationTestError("Kubernetes only supports MD resume here")
        result_base = str(Path(runtime.workspace) / "results" / step.name)
        _reset_result_base(runner, config, fixture, runtime, result_base)
        _sync_step_runtime(
            runner, config, fixture, runtime, step, template, result_base
        )
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
    if is_mdtest_resume and _kubectl_lifecycle_state(output) == "COLLECTED":
        result_root = Path(result_base)
        result = _collected_result_root(result_root)
        runtime.values["kubectl_result_root"] = str(result_root)
        runtime.values["kubectl_collected"] = "1"
        _assert_execution_contract(runtime.scenario, step, result)
        _assert_dataset_state(
            runner, config, fixture, step, result, None, KUBECTL_STORAGE_MOUNT
        )
        _assert_scenario_report(
            runner, report_workspace, result, runtime, step, log_dir
        )
        return result
    result_root, attempt_id = _kubectl_submission(output)
    runtime.values["kubectl_result_root"] = str(result_root)
    if step.workload_kind == "mdtest" and step.kind is CommandKind.SWEEP:
        runtime.values["mdtest_results_dir"] = str(result_root)
    LOG.info("Submitted Kubernetes %s attempt %s", step.name, attempt_id)
    terminal = _wait_for_kubectl_terminal_state(
        runner, config, runtime, result_root, log_dir, step.timeout_seconds
    )
    if terminal != "SUCCESS":
        raise IntegrationTestError(f"kubectl {step.name} ended in {terminal}")
    result = _kubectl_collect_result(runner, config, runtime, result_root, log_dir)
    runtime.values["kubectl_collected"] = "1"
    _assert_execution_contract(runtime.scenario, step, result)
    if step.workload_kind != "mdtest":
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
    runtime.values["mdtest_results_dir"] = str(result_root)
    runtime.values["kubectl_collected"] = "1"
    if terminal not in {"FAILED", "CANCELLED"}:
        raise IntegrationTestError(
            f"failure-resume first attempt unexpectedly ended in {terminal}"
        )
    _assert_execution_contract(runtime.scenario, first, failed_result)
    preserved = _hash_execution_contract(failed_result, "0001")
    if first.workload_kind != "mdtest":
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


def _kubectl_assert_coordinator_identity(
    pod: dict[str, Any], identity: tuple[str, str, str]
) -> None:
    """Reject replacement Pods or ownership changes before any mutation."""
    name, uid, attempt_id = identity
    metadata = pod.get("metadata", {})
    labels = metadata.get("labels", {})
    if (
        not name
        or not uid
        or metadata.get("name") != name
        or metadata.get("uid") != uid
        or labels.get("storage-scale-test.nvidia.com/run") != attempt_id
        or labels.get("app.kubernetes.io/component") != "coordinator"
    ):
        raise IntegrationTestError("coordinator Pod ownership evidence is invalid")


def _kubectl_coordinator_identity(
    runner: Any, config: Any, attempt_id: str
) -> tuple[str, str, str]:
    """Resolve one exact owned coordinator Pod, including its immutable UID."""
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
    metadata = pods[0].get("metadata", {})
    identity = (
        str(metadata.get("name", "")),
        str(metadata.get("uid", "")),
        attempt_id,
    )
    _kubectl_assert_coordinator_identity(pods[0], identity)
    return identity


def _kubectl_coordinator_runtime_identity(
    runner: Any, config: Any, identity: tuple[str, str, str]
) -> tuple[str, str]:
    """Resolve the container only inside an ownership-verified fixture node."""
    result = runner.run(
        _kubectl(
            config, "-n", config.namespace, "get", "pod", identity[0], "-o", "json"
        ),
        timeout=20,
    )
    pod = json.loads(result.stdout)
    _kubectl_assert_coordinator_identity(pod, identity)
    node = pod.get("spec", {}).get("nodeName", "")
    container_ids = [
        container.get("containerID", "")
        for container in pod.get("status", {}).get("containerStatuses", [])
        if container.get("name") == "coordinator"
    ]
    if (
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", node)
        or len(container_ids) != 1
        or not re.fullmatch(r"containerd://[0-9a-f]{64}", container_ids[0])
    ):
        raise IntegrationTestError("coordinator runtime identity is invalid")
    labels = json.loads(
        runner.run(
            [
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                "{{json .Config.Labels}}",
                node,
            ],
            timeout=20,
        ).stdout
    )
    if labels.get("io.x-k8s.kind.cluster") != config.cluster_name:
        raise IntegrationTestError("coordinator node belongs to another kind fixture")
    container_id = container_ids[0].removeprefix("containerd://")
    container = json.loads(
        runner.run(
            [
                "docker",
                "exec",
                node,
                "ctr",
                "-n",
                "k8s.io",
                "containers",
                "info",
                container_id,
            ],
            timeout=20,
        ).stdout
    )
    _kubectl_assert_coordinator_container(container, config, identity, container_id)
    return node, container_id


def _kubectl_assert_coordinator_container(
    container: dict[str, Any],
    config: Any,
    identity: tuple[str, str, str],
    container_id: str,
) -> None:
    """Match runtime container metadata to the exact Kubernetes Pod identity."""
    expected_labels = {
        "io.kubernetes.pod.name": identity[0],
        "io.kubernetes.pod.uid": identity[1],
        "io.kubernetes.pod.namespace": config.namespace,
        "io.kubernetes.container.name": "coordinator",
    }
    labels = container.get("Labels", {})
    if container.get("ID") != container_id or any(
        labels.get(key) != value for key, value in expected_labels.items()
    ):
        raise IntegrationTestError("coordinator runtime container ownership is invalid")


def _kubectl_kill_coordinator_pid1(
    runner: Any, config: Any, identity: tuple[str, str, str]
) -> None:
    """SIGKILL the verified container init, so no child can checkpoint TERM."""
    name, uid, attempt_id = identity
    node, container_id = _kubectl_coordinator_runtime_identity(runner, config, identity)
    command = """
[[ ${KUBECTL_COORDINATOR_POD_UID:-} == "$1" \
    && ${KUBECTL_COORDINATOR_POD_NAME:-} == "$2" ]] || exit 64
mapfile -d '' -t coordinator_argv < /proc/1/cmdline || exit 64
[[ ${#coordinator_argv[@]} == 6 \
    && ${coordinator_argv[1]} == */"$3"/control/coordinator.sh \
    && ${coordinator_argv[2]} == "${coordinator_argv[1]%/coordinator.sh}" \
    && ${coordinator_argv[3]} == "${coordinator_argv[2]%/control}/state" \
    && ${coordinator_argv[5]} == "$3" ]] || exit 64
""".strip()
    result = runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "exec",
            name,
            "-c",
            "coordinator",
            "--",
            "/bin/bash",
            "-ceu",
            command,
            "coordinator-loss",
            uid,
            name,
            attempt_id,
        ),
        check=False,
        timeout=20,
    )
    if result.returncode:
        raise IntegrationTestError("coordinator process ownership evidence is invalid")
    # Linux namespace init ignores SIGKILL from its own PID namespace. The
    # fixture node's runtime delivers it from an ancestor namespace instead.
    runner.run(
        [
            "docker",
            "exec",
            node,
            "ctr",
            "-n",
            "k8s.io",
            "tasks",
            "kill",
            "--signal",
            "SIGKILL",
            container_id,
        ],
        check=False,
        timeout=20,
    )
    # A runtime exec may disconnect during container death. Its result never
    # proves the kill: require the same Pod UID's terminal exit below.


def _kubectl_coordinator_has_sigkill_evidence(pod: dict[str, Any]) -> bool:
    """Distinguish a verified abrupt exit from running or graceful termination."""
    for container in pod.get("status", {}).get("containerStatuses", []):
        if container.get("name") != "coordinator":
            continue
        terminated = container.get("state", {}).get("terminated")
        if terminated is not None:
            if terminated.get("exitCode") != 137:
                raise IntegrationTestError(
                    f"coordinator terminated without SIGKILL evidence: {terminated}"
                )
            return True
    return False


def _kubectl_wait_coordinator_sigkill(
    runner: Any,
    config: Any,
    identity: tuple[str, str, str],
    timeout: float = 30,
) -> None:
    """Require bounded, Kubernetes-observed exit 137 for the exact selected Pod."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = runner.run(
            _kubectl(
                config,
                "-n",
                config.namespace,
                "get",
                "pod",
                identity[0],
                "-o",
                "json",
            ),
            check=False,
            timeout=min(10, max(0.1, deadline - time.monotonic())),
        )
        if result.returncode:
            raise IntegrationTestError(
                "cannot prove coordinator SIGKILL termination: "
                + (result.stderr or result.stdout).strip()
            )
        pod = json.loads(result.stdout)
        _kubectl_assert_coordinator_identity(pod, identity)
        if _kubectl_coordinator_has_sigkill_evidence(pod):
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise IntegrationTestError("coordinator SIGKILL termination evidence timed out")


def _kubectl_delete_coordinator_pod(runner: Any, config: Any, attempt_id: str) -> None:
    """Crash the exact owned coordinator, prove abrupt loss, then delete its Pod."""
    identity = _kubectl_coordinator_identity(runner, config, attempt_id)
    _kubectl_kill_coordinator_pid1(runner, config, identity)
    _kubectl_wait_coordinator_sigkill(runner, config, identity)
    runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "delete",
            "pod",
            identity[0],
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
                command_root
                / (
                    "nv-mdtest-elbencho.sh"
                    if _runtime_workload_kind(runtime) == "mdtest"
                    else "nv-elbencho-sweep.sh"
                ),
                operation,
                result_root,
            ],
            cwd=Path(runtime.workspace),
            check=False,
            timeout=120,
        )
        if result.returncode:
            output = result.stdout + result.stderr
            if "STATE=SUBMISSION_FAILED" in output.splitlines():
                # Submission rollback is terminal and leaves no benchmark
                # results or remote resources to collect.
                return
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
                "rm -rf -- /home/tester/elbencho-[0-9]* "
                "/home/tester/filesystem-batch-[0-9]*",
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
    if runtime.selector == "kubectl":
        _preserve_kubectl_failure_diagnostics(
            runner, config, runtime, destination, failures
        )
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


def _preserve_kubectl_failure_diagnostics(
    runner: Any,
    config: Any,
    runtime: ScenarioRuntime,
    destination: Path,
    failures: list[str],
) -> None:
    """Capture bounded exact-attempt Kubernetes evidence before cleanup."""
    result_root_value = runtime.values.get("kubectl_result_root")
    if not result_root_value:
        return
    current = Path(result_root_value) / "kubernetes" / "current-attempt"
    try:
        attempt = current.read_text(encoding="utf-8").strip()
    except OSError as error:
        failures.append(f"could not read kubectl attempt identity: {error!r}")
        return
    if not re.fullmatch(r"[0-9a-f]{8}", attempt):
        failures.append(f"invalid kubectl diagnostic attempt identity: {attempt!r}")
        return
    label = f"storage-scale-test.nvidia.com/run={attempt}"
    commands = {
        "objects.yaml": _kubectl(
            config,
            "-n",
            config.namespace,
            "get",
            "job,pod,daemonset,networkpolicy,lease",
            "-l",
            label,
            "-o",
            "yaml",
        ),
        "describe.txt": _kubectl(
            config,
            "-n",
            config.namespace,
            "describe",
            "job,pod,daemonset,lease",
            "-l",
            label,
        ),
        "logs.txt": _kubectl(
            config,
            "-n",
            config.namespace,
            "logs",
            "-l",
            label,
            "--all-containers=true",
            "--prefix=true",
            "--tail=2000",
        ),
        "events.yaml": _kubectl(
            config,
            "-n",
            config.namespace,
            "get",
            "events",
            "-o",
            "yaml",
        ),
    }
    target = destination / "kubernetes"
    target.mkdir(parents=True, exist_ok=True)
    for filename, command in commands.items():
        try:
            result = runner.run(command, check=False, timeout=30)
            output = result.stdout + result.stderr
            (target / filename).write_text(output, encoding="utf-8")
            if result.returncode:
                failures.append(
                    f"kubectl diagnostic {filename} exited {result.returncode}"
                )
        except (OSError, subprocess.SubprocessError) as error:
            failures.append(f"kubectl diagnostic {filename} failed: {error!r}")
    try:
        pods = runner.run(
            _kubectl(
                config,
                "-n",
                config.namespace,
                "get",
                "pods",
                "-l",
                label,
                "-o",
                "json",
            ),
            check=False,
            timeout=30,
        )
        items = json.loads(pods.stdout).get("items", []) if not pods.returncode else []
        names = sorted(
            item.get("metadata", {}).get("name", "")
            for item in items
            if not item.get("metadata", {}).get("deletionTimestamp")
            and item.get("status", {}).get("phase") == "Running"
            and any(
                status.get("ready")
                for status in item.get("status", {}).get("containerStatuses", [])
            )
        )
        names = [name for name in names if name]
        if names:
            remote = f"{_kubectl_remote_control_root(runtime)}/runs/{attempt}/state"
            result = runner.run(
                _kubectl(
                    config,
                    "-n",
                    config.namespace,
                    "exec",
                    names[0],
                    "--",
                    "/bin/bash",
                    "-ceu",
                    "for file in run.status run-summary.tsv publication-manifest.tsv; do "
                    'printf "===== %s =====\\n" "$file"; '
                    '[[ ! -f "$1/$file" ]] || cat -- "$1/$file"; done',
                    "diagnostics",
                    remote,
                ),
                check=False,
                timeout=30,
            )
            (target / "durable-state.txt").write_text(
                result.stdout + result.stderr, encoding="utf-8"
            )
            if result.returncode:
                failures.append(
                    f"kubectl durable-state diagnostic exited {result.returncode}"
                )
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError) as error:
        failures.append(f"kubectl durable-state diagnostic failed: {error!r}")


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

        def remove(target: KubectlStorageTarget | None) -> None:
            claim = target.pvc if target is not None else fixture.kubectl_pvc
            command = (
                f"rm -rf -- {_shell(f'{kubectl_claim_mount(claim)}/{logical_root}')}"
            )
            _storage_utility_shell(
                runner, config, fixture, command, timeout=120, target=target
            )

        for_each_target(remove, _scenario_storage_targets(fixture, runtime))
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

    wrapper_digests: dict[str, str] = {}

    def _atomic_write(destination: PurePosixPath, digest: str, mode: int) -> str:
        return (
            f"destination={_shell(destination)}; "
            'temporary=$(mktemp "${destination}.staging.XXXXXXXX"); '
            "trap 'rm -f -- \"$temporary\"' EXIT; "
            'cat > "$temporary"; '
            f'[[ "$(sha256sum "$temporary" | cut -d " " -f 1)" == {digest} ]]; '
            f'chmod {mode:o} -- "$temporary"; '
            'mv -f -- "$temporary" "$destination"'
        )

    def _container(_endpoint: str) -> str:
        return fixture.login_container if selector == "slurm" else "sshd"

    def _command(endpoint: str, command: str, *, stdin: Any = None) -> None:
        if endpoint == "local":
            return
        arguments = [
            *_kubectl(
                config,
                "-n",
                config.namespace,
                "exec",
                *(("-i",) if stdin is not None else ()),
                endpoint,
                "-c",
                _container(endpoint),
                "--",
            ),
            "timeout",
            "--kill-after=5s",
            "50s",
            "runuser",
            "-u",
            WORKLOAD_USER,
            "--",
            "bash",
            "-ec",
            f"umask 0007; {command}",
        ]
        for attempt in range(3):
            if stdin is not None:
                stdin.seek(0)
            try:
                runner.run(arguments, stdin=stdin, timeout=60)
                return
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                if attempt == 2 or not _remote_staging_failure_is_transient(error):
                    raise
                LOG.warning(
                    "Transient staging failure on %s; retrying: %s", endpoint, error
                )
                time.sleep(attempt + 1)

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
            temporary = local_destination.with_name(
                f".{local_destination.name}.{secrets.token_hex(8)}"
            )
            try:
                shutil.copy2(source, temporary)
                temporary.chmod(mode)
                temporary.replace(local_destination)
            finally:
                temporary.unlink(missing_ok=True)
            return
        with tempfile.TemporaryFile() as stream:
            stream.write(source.read_bytes())
            stream.seek(0)
            _command(
                endpoint,
                _atomic_write(destination, _sha256(source), mode),
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
                    archive.add(
                        child, arcname=child.relative_to(source), recursive=False
                    )
            stream.seek(0)
            digest = hashlib.sha256(stream.read()).hexdigest()
            stream.seek(0)
            _command(
                endpoint,
                f"destination={_shell(destination)}; "
                'temporary=$(mktemp -d "${destination}.staging.XXXXXXXX"); '
                "trap 'rm -rf -- \"$temporary\"' EXIT; "
                'cat > "$temporary/archive.tar"; '
                f'[[ "$(sha256sum "$temporary/archive.tar" | cut -d " " -f 1)" == {digest} ]]; '
                'mkdir "$temporary/tree"; tar -xf "$temporary/archive.tar" -C "$temporary/tree"; '
                'chmod -R u+rwX,g+rX,o-rwx -- "$temporary/tree"; '
                'rm -rf -- "$destination"; mv -T -- "$temporary/tree" "$destination"',
                stdin=stream,
            )

    def write_text(
        endpoint: str, destination: PurePosixPath, content: str, mode: int
    ) -> None:
        wrapper_digests[endpoint] = hashlib.sha256(content.encode()).hexdigest()
        if endpoint == "local":
            if local_wrapper is None:
                raise IntegrationTestError("local failure wrapper path is absent")
            temporary = local_wrapper.with_name(
                f".{local_wrapper.name}.{secrets.token_hex(8)}"
            )
            try:
                temporary.write_text(content, encoding="utf-8")
                temporary.chmod(mode)
                temporary.replace(local_wrapper)
            finally:
                temporary.unlink(missing_ok=True)
            return
        if remote_wrapper is not None:
            destination = remote_wrapper
        with tempfile.TemporaryFile() as stream:
            stream.write(content.encode())
            stream.seek(0)
            _command(
                endpoint,
                _atomic_write(destination, wrapper_digests[endpoint], mode),
                stdin=stream,
            )

    def remove_tree(endpoint: str, path: PurePosixPath) -> None:
        if endpoint == "local":
            if (
                endpoint in wrapper_digests
                and local_wrapper is not None
                and restore_binary is not None
            ):
                copy_file(restore_binary, endpoint, PurePosixPath(local_wrapper), 0o755)
            shutil.rmtree(Path(path), ignore_errors=True)
            return
        if remote_wrapper is not None and endpoint in wrapper_digests:
            if restore_binary is None:
                raise IntegrationTestError(
                    "verified local restoration binary is absent"
                )
            # Restoration never trusts a remotely staged delegate. Also reconcile
            # a wrapper publication whose kubectl acknowledgement was lost.
            original_digest = _sha256(restore_binary)
            with tempfile.TemporaryFile() as stream:
                stream.write(restore_binary.read_bytes())
                stream.seek(0)
                _command(
                    endpoint,
                    f"actual=$(sha256sum {_shell(remote_wrapper)} | cut -d ' ' -f 1); "
                    f'if [[ "$actual" == {original_digest} ]]; then cat > /dev/null; '
                    f'elif [[ "$actual" == {wrapper_digests[endpoint]} ]]; then '
                    f"{_atomic_write(remote_wrapper, original_digest, 0o755)}; "
                    "else echo 'refusing to restore an unexpected wrapper' >&2; exit 1; fi",
                    stdin=stream,
                )
        _command(endpoint, f"rm -rf -- {_shell(path)}")

    return FailureInjectionOperations(
        make_directory, copy_file, copy_tree, write_text, remove_tree
    )


def _remote_staging_failure_is_transient(error: BaseException) -> bool:
    """Retry transport failures and deadlines, never auth or remote script errors."""
    if isinstance(error, subprocess.TimeoutExpired):
        return True
    detail = str(error).lower()
    if re.search(
        r"unauthorized|forbidden|permission denied|command terminated|checksum|no such file|unsafe",
        detail,
    ):
        return False
    return bool(
        re.search(
            r"unexpected eof|connection reset|connection refused|i/o timeout|tls handshake timeout|too many requests|service unavailable|bad gateway|gateway timeout",
            detail,
        )
    )


def _failure_target_argument(runtime: ScenarioRuntime) -> str:
    """Select the intended cell without changing the wrapper protocol."""
    if runtime.scenario.name == "mixed-batch":
        return "mdtest-elbencho-c_002-t_001"
    if _runtime_workload_kind(runtime) == "mdtest":
        return "mdtest-elbencho-c_001-t_002"
    return "executions/0002.write.json"


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
        target_argument=_failure_target_argument(runtime),
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
        target_argument=_failure_target_argument(runtime),
        shared_endpoint=fixture.login_pod,
    )
    operations = _remote_staging_operations(
        runner,
        config,
        fixture,
        runtime.selector,
        None,
        source_binary,
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
    script_name = (
        "nv-mdtest-elbencho.sh"
        if _runtime_workload_kind(runtime) == "mdtest"
        else "nv-elbencho-sweep.sh"
    )
    with staged_failure_injection(plan, operations):
        arguments = shlex.join(first.render_arguments(runtime.values))
        command = (
            f"cd -- {_shell(runtime.workspace)} && "
            f"./storage-tests/fs/{script_name} {arguments}"
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
        runtime.values["mdtest_results_dir"] = remote_result
        if first.workload_kind != "mdtest":
            _assert_dataset_state(runner, config, fixture, first, failed_result, None)
        preserved = _hash_execution_contract(failed_result, "0001")
        resume_command = (
            f"cd -- {_shell(runtime.workspace)} && "
            f"./storage-tests/fs/{script_name} "
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


def _mixed_batch_command(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    step: ScenarioStep,
    log_dir: Path,
    *,
    expected_failure: bool = False,
) -> str:
    """Use each group's launcher, including the opposite launcher at start."""
    arguments = step.render_arguments(runtime.values)
    if runtime.selector == "kubectl":
        return _run_kubectl_command(
            runner,
            config,
            runtime,
            step.name,
            arguments,
            log_dir,
            step.timeout_seconds,
            expected_failure=expected_failure,
            workload_kind=step.workload_kind,
        )
    script = (
        "nv-mdtest-elbencho.sh"
        if step.workload_kind == "mdtest"
        else "nv-elbencho-sweep.sh"
    )
    return _run_step(
        runner,
        config,
        fixture,
        runtime.selector,
        step.name,
        f"cd -- {_shell(runtime.workspace)} && "
        f"./storage-tests/fs/{script} {shlex.join(arguments)}",
        log_dir,
        step.timeout_seconds,
        expected_failure=expected_failure,
    )


def _mixed_batch_membership(result: Path) -> tuple[list[list[str]], list[list[str]]]:
    """Read committed membership, never orphaned definition files."""
    rows = [
        line.split("\t")
        for line in (result / "batch-manifest.tsv")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    return (
        [row for row in rows if row[0] == "group"],
        [row for row in rows if row[0] == "execution"],
    )


def _assert_mixed_batch_cell(
    result: Path,
    group: list[str],
    execution: list[str],
    step: ScenarioStep,
    status: ExecutionStatus,
) -> None:
    """Check provenance and result evidence for one owning group."""
    _assert_batch_cell(
        result, group, execution, step, step.executions[0].coordinate, status
    )


def _assert_batch_cell(
    result: Path,
    group: list[str],
    execution: list[str],
    step: ScenarioStep,
    coordinate: ExecutionCoordinate,
    status: ExecutionStatus,
) -> None:
    """Check provenance and result evidence for one cell of a batch group."""
    identity = execution[1]
    definition = result / "executions" / f"{identity}.sh"
    assignments = _shell_assignments(definition)
    kind = "mdtest" if step.workload_kind == "mdtest" else "io"
    if (
        group[2] != kind
        or assignments.get("ELBENCHO_EXECUTION_KIND") != kind
        or assignments.get("ELBENCHO_BATCH_GROUP_ID") != group[1]
        or execution[2] != group[1]
        or assignments.get("ELBENCHO_BATCH_OUTPUT_RELATIVE") != group[3]
        or int(assignments.get("nodes", "0")) != coordinate.nodes
    ):
        raise IntegrationTestError(
            f"mixed batch cell {identity} has incorrect provenance"
        )
    output = result / group[3]
    actual_status = (result / "executions" / f"{identity}.status").read_text().strip()
    if actual_status != status.value:
        raise IntegrationTestError(
            f"mixed batch cell {identity}: expected {status.value}, got {actual_status}"
        )
    if status is not ExecutionStatus.SUCCESS:
        return
    if kind == "mdtest":
        _assert_mdtest_result_artifacts(
            output,
            output / "executions",
            identity,
            coordinate.nodes,
            coordinate.tasks_per_node,
        )
    elif assignments.get("ELBENCHO_FILE_LAYOUT") != "worker-directories":
        # Only the generated shared-directory layout writes per-phase JSON;
        # worker-directory cells (e.g. a weighted multi-root target) prove
        # completion through their CSV/OUT results below.
        _assert_phase_artifacts(output / "executions", identity, step, status)
    for suffix in ("csv", "out"):
        if not any(path.stat().st_size for path in output.glob(f"*.{suffix}")):
            raise IntegrationTestError(
                f"mixed batch group {group[1]} lacks {suffix} evidence"
            )


def _env_override_provenance(path: Path) -> str:
    """Return the --env-override file recorded in a reified snapshot."""
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"STORAGE_SCALE_TEST_ENV_OVERRIDE_FILE=(.*)", line)
        if match:
            values = shlex.split(match.group(1), posix=True)
            return values[0] if values else ""
    raise IntegrationTestError(f"{path} lacks --env-override provenance")


def _step_option_files(step: ScenarioStep, option: str) -> list[str]:
    """Return a step's *option* file arguments in command-line order."""
    return [
        step.arguments[index + 1]
        for index, argument in enumerate(step.arguments[:-1])
        if argument == option
    ]


def _step_override_files(step: ScenarioStep) -> list[str]:
    """Return a step's --env-override (workload) files in command-line order."""
    return _step_option_files(step, "--env-override")


def _step_target_files(step: ScenarioStep) -> list[str]:
    """Return a step's --target files in command-line order."""
    return _step_option_files(step, "--target")


def _assert_mixed_batch_overrides(
    result: Path, executions: list[list[str]], steps: tuple[ScenarioStep, ...]
) -> None:
    """Each cell records exactly the override its own append command used."""
    for execution, step in zip(executions, steps, strict=True):
        # The legacy scalar records the last override applied.
        overrides = _step_override_files(step)
        expected = overrides[-1] if overrides else ""
        recorded = _env_override_provenance(
            result / "executions" / f"{execution[1]}.sh"
        )
        if (recorded != "") != bool(expected) or not recorded.endswith(
            f"/{expected}" if expected else ""
        ):
            raise IntegrationTestError(
                f"mixed batch cell {execution[1]} recorded override "
                f"{recorded!r}, expected {expected or 'none'}"
            )


def _assert_mixed_batch(
    result: Path,
    runtime: ScenarioRuntime,
    statuses: tuple[ExecutionStatus, ...],
) -> None:
    """Require the ordered mixed ledger and different saved workload settings."""
    groups, executions = _mixed_batch_membership(result)
    if len(groups) != len(runtime.scenario.steps) or len(executions) != len(groups):
        raise IntegrationTestError(
            "mixed batch must commit exactly its three prepared groups"
        )
    if [row[1] for row in executions] != ["0001", "0002", "0003"]:
        raise IntegrationTestError(
            "mixed batch execution IDs are not global and sequential"
        )
    for group, execution, step, status in zip(
        groups, executions, runtime.scenario.steps, statuses, strict=True
    ):
        _assert_mixed_batch_cell(result, group, execution, step, status)
    _assert_mixed_batch_overrides(result, executions, runtime.scenario.steps)
    settings = [
        _shell_assignments(result / "executions" / f"{row[1]}.sh") for row in executions
    ]
    if (
        settings[0].get("ELBENCHO_FILE_SIZE") != "16M"
        or settings[2].get("ELBENCHO_FILE_SIZE") != "8M"
        or settings[1].get("MDTEST_ITERATIONS") != "2"
        or runtime.values["test_root_secondary"]
        not in (result / "executions/0002.sh").read_text()
    ):
        raise IntegrationTestError(
            "mixed batch did not preserve different group settings and roots"
        )


def _mixed_batch_group_rows(
    fixture: Fixture, runtime: ScenarioRuntime
) -> tuple[GroupRow, ...]:
    """Return each mixed-batch group's ID, kind, and expected storage target name.

    Only the metadata group's target file names its target. The others derive
    the name from their settings: the sorted TEST_DIRS roots, which here is the
    one primary root, normalized to ``namespace/claim:path`` on Kubernetes.
    """
    derived = runtime.values["test_root"].rstrip("/")
    if runtime.selector == "kubectl":
        claim = runtime.values.get("kubectl_claim", fixture.kubectl_pvc)
        derived = f"{fixture.kubectl_namespace}/{claim}:{derived}"
    return tuple(
        (
            f"{index:04d}",
            "mdtest" if step.workload_kind == "mdtest" else "io",
            (
                MIXED_BATCH_TARGET_NAME
                if MIXED_BATCH_TARGET_FILE in _step_target_files(step)
                else derived
            ),
        )
        for index, step in enumerate(runtime.scenario.steps, start=1)
    )


def _env_used_snapshot(path: Path) -> dict[str, Any]:
    """Parse one group's env_used.yaml."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise IntegrationTestError(f"{path} is not a YAML mapping")
    return document


def _target_file_expectation(
    step: ScenarioStep, values: dict[str, str]
) -> dict[str, str]:
    """Map each --target file a step passes to the content the driver wrote."""
    rendered = {
        item.relative_path: item.content for item in step.render_support_files(values)
    }
    return {name: rendered[name] for name in _step_target_files(step)}


def assert_target_file_record(
    snapshot: dict[str, Any],
    expected: tuple[str, str, set[str]] | None,
    label: str,
) -> None:
    """Require env_used.yaml's ``target_file`` to record the target file used.

    *expected* is None for a group without a target file (``target_file:
    null``), else the file's relative name, its content, and the variables it
    must record as set. The recorded path is the operator's original, absolute
    path, so it must end with the relative name.
    """
    record = snapshot.get("target_file")
    if expected is None:
        if record is not None:
            raise IntegrationTestError(
                f"{label} env_used.yaml records target_file {record!r}, expected null"
            )
        return
    name, content, variables = expected
    if not isinstance(record, dict):
        raise IntegrationTestError(
            f"{label} env_used.yaml records no target_file, expected {name}"
        )
    problems = []
    if not str(record.get("file", "")).endswith(f"/{name}"):
        problems.append(f"file {record.get('file')!r}")
    if record.get("sha256") != hashlib.sha256(content.encode("utf-8")).hexdigest():
        problems.append(f"sha256 {record.get('sha256')!r}")
    recorded = {str(item) for item in record.get("variables") or ()}
    if not variables <= recorded:
        problems.append(f"variables {sorted(recorded)}")
    if problems:
        raise IntegrationTestError(
            f"{label} env_used.yaml target_file does not match {name}: "
            + ", ".join(problems)
        )


def _assert_mixed_batch_targets(
    result: Path, runtime: ScenarioRuntime, rows: tuple[GroupRow, ...]
) -> None:
    """Each group's snapshot records its name, target file, and override files.

    Override files are workload only; the target file is recorded separately.
    """
    groups, _ = _mixed_batch_membership(result)
    for group, (group_id, _, name), step in zip(
        groups, rows, runtime.scenario.steps, strict=True
    ):
        snapshot = _env_used_snapshot(result / group[3] / "env_used.yaml")
        if snapshot.get("STORAGE_TARGET_NAME") != name:
            raise IntegrationTestError(
                f"group {group_id} env_used.yaml records STORAGE_TARGET_NAME="
                f"{snapshot.get('STORAGE_TARGET_NAME')!r}, expected {name!r}"
            )
        assert_env_override_files(
            snapshot, _step_override_files(step), f"group {group_id}"
        )
        files = _target_file_expectation(step, runtime.values)
        expected = None
        if files:
            ((file_name, content),) = files.items()
            expected = (file_name, content, {"STORAGE_TARGET_NAME", "TEST_DIRS"})
        assert_target_file_record(snapshot, expected, f"group {group_id}")


def _assert_mixed_batch_prepared(result: Path, runtime: ScenarioRuntime) -> None:
    """Preparation must leave no dispatch or workload evidence."""
    _assert_mixed_batch(result, runtime, (ExecutionStatus.PENDING,) * 3)
    dispatched = [
        path
        for path in result.rglob("*")
        if path.suffix
        in {"log", ".log", ".jobid", ".exitcode", ".csv", ".out", ".json"}
    ]
    if (
        dispatched
        or (result / "batch-sealed.sha256").exists()
        or (result / "kubernetes/current-attempt").exists()
    ):
        raise IntegrationTestError(
            f"mixed batch preparation dispatched work: {dispatched}"
        )


def _hash_mixed_batch_success(result: Path) -> dict[str, str]:
    """Include the first group's artifacts in successful-cell preservation."""
    groups, _ = _mixed_batch_membership(result)
    hashes = _hash_execution_contract(result, "0001")
    for path in sorted((result / groups[0][3]).rglob("*")):
        if path.is_file():
            hashes[str(path.relative_to(result))] = _sha256(path)
    return hashes


def _mixed_batch_report(
    runner: Any,
    report_workspace: Path,
    result: Path,
    log_dir: Path,
    fixture: Fixture,
    runtime: ScenarioRuntime,
) -> None:
    """Exercise the unified front door and keep repeated coordinates separate.

    The report index must also name each group's storage target.
    """
    rows = _mixed_batch_group_rows(fixture, runtime)
    reports = _batch_report(
        runner,
        report_workspace,
        result,
        log_dir,
        tuple(row[0] for row in rows),
        results_json=True,
    )
    assert_report_index_targets((reports / "index.md").read_text("utf-8"), rows)
    _assert_batch_results_json(reports / "results.json", rows)


def _assert_batch_results_json(path: Path, rows: tuple[GroupRow, ...]) -> None:
    """The machine-readable document must show a complete, fully successful batch.

    The integration environment has no ``jsonschema``, so this checks only the
    contract's essentials with plain Python.
    """
    if not path.is_file():
        raise IntegrationTestError("mixed batch reporting wrote no results.json")
    document = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    if document.get("schema") != "storage-scale-test/filesystem-batch-results":
        problems.append(f"schema {document.get('schema')!r}")
    if document.get("schema_version") != "1.0":
        problems.append(f"schema_version {document.get('schema_version')!r}")
    if document.get("batch_outcome", {}).get("complete") is not True:
        problems.append("batch_outcome.complete is not true")
    groups = sorted(group["group_id"] for group in document.get("groups", ()))
    if groups != sorted(row[0] for row in rows):
        problems.append(f"groups {groups}")
    io_groups = {row[0] for row in rows if row[1] == "io"}
    for execution in document.get("executions", ()):
        label = f"execution {execution.get('execution_id')}"
        if execution.get("ledger_state") != "success":
            problems.append(f"{label} ledger_state {execution.get('ledger_state')!r}")
        if execution.get("result_state") != "complete":
            problems.append(f"{label} result_state {execution.get('result_state')!r}")
        if execution.get("group_id") in io_groups and not _has_elbencho_rates(
            execution
        ):
            problems.append(f"{label} lacks elbencho_first/elbencho_last numbers")
    if not document.get("executions"):
        problems.append("no executions")
    if problems:
        raise IntegrationTestError(
            "mixed batch results.json is not a complete success: " + "; ".join(problems)
        )


def _has_elbencho_rates(execution: dict[str, Any]) -> bool:
    """True if every transfer phase carries numeric elbencho first/last rates."""
    phases = [
        phase
        for phase in (execution.get("results") or {}).get("phases", ())
        if "elbencho_first" in phase
    ]
    return bool(phases) and all(
        isinstance(phase.get(key), dict)
        and any(isinstance(value, (int, float)) for value in phase[key].values())
        for phase in phases
        for key in ("elbencho_first", "elbencho_last")
    )


def _batch_report(
    runner: Any,
    report_workspace: Path,
    result: Path,
    log_dir: Path,
    identities: tuple[str, ...],
    output_name: str = "reports-mixed-batch",
    *,
    results_json: bool = False,
) -> Path:
    """Run the unified report front door and require one report per group.

    *results_json* also writes ``results.json`` into the report directory.
    """
    reports = log_dir / output_name
    completed = runner.run(
        [
            report_workspace / "utils/extract-filesystem.py",
            result,
            "--output-dir",
            reports,
            *(("--to-json",) if results_json else ()),
        ],
        timeout=600,
        check=False,
    )
    (log_dir / "extract-filesystem.log").write_text(
        completed.stdout + completed.stderr, encoding="utf-8"
    )
    if completed.returncode or not (reports / "index.md").is_file():
        raise IntegrationTestError(
            "mixed batch reporting failed; see extract-filesystem.log"
        )
    index = (reports / "index.md").read_text(encoding="utf-8")
    for identity in identities:
        report = reports / "groups" / identity / "report.txt"
        if identity not in index or not report.is_file() or not report.stat().st_size:
            raise IntegrationTestError(
                f"mixed batch group {identity} lacks its separate report"
            )
        # Every result must have carried its version record back from the run.
        produced = re.search(
            r"^Produced by storage-scale-test: (.+)$",
            report.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        if not produced or "unknown" in produced.group(1):
            raise IntegrationTestError(
                f"mixed batch group {identity} report lacks result versions"
            )
    return reports


def _assert_mixed_batch_kubectl_progress(
    output: str, *, failed: bool, collected: bool = False
) -> None:
    """Current-attempt counts must reflect remote cells before collection."""
    if (
        _kubectl_output_value(output, "EXECUTION_SCOPE") != "CURRENT_ATTEMPT"
        or _kubectl_output_value(output, "PROGRESS_SOURCE")
        != ("LOCAL" if collected else "PVC")
        or _kubectl_output_value(output, "RESULT_COLLECTION")
        != ("COMPLETE" if collected else "PENDING")
    ):
        raise IntegrationTestError(
            "mixed batch status did not distinguish ledger sources"
        )
    expected = (1, 0, 1, 1) if failed else (0, 0, 2, 0)
    for state, count in zip(
        ("PENDING", "RUNNING", "SUCCEEDED", "FAILED"), expected, strict=True
    ):
        observed = _kubectl_output_value(output, f"EXECUTIONS_{state}")
        if observed != str(count):
            raise IntegrationTestError(
                f"mixed batch attempt {state}: expected {count}, got {observed}"
            )


def _mixed_batch_collect(
    runner: Any,
    config: Any,
    runtime: ScenarioRuntime,
    root: str,
    step: ScenarioStep,
    log_dir: Path,
    *,
    failed: bool,
) -> None:
    """Wait and collect before a Kubernetes batch can be resumed."""
    terminal = _wait_for_kubectl_terminal_state(
        runner, config, runtime, Path(root), log_dir, step.timeout_seconds
    )
    expected = "FAILED" if failed else "SUCCESS"
    if terminal != expected:
        raise IntegrationTestError(f"mixed batch expected {expected}, got {terminal}")
    output = _run_kubectl_command(
        runner,
        config,
        runtime,
        "mixed-batch-progress",
        ("--status", root),
        log_dir,
        step.timeout_seconds,
        workload_kind=step.workload_kind,
    )
    _assert_mixed_batch_kubectl_progress(output, failed=failed)
    _kubectl_collect_result(
        runner,
        config,
        runtime,
        Path(root),
        log_dir,
        terminal_states=frozenset({"FAILED"}) if failed else frozenset(),
    )
    runtime.values["kubectl_collected"] = "1"
    output = _run_kubectl_command(
        runner,
        config,
        runtime,
        "mixed-batch-collected-progress",
        ("--status", root),
        log_dir,
        step.timeout_seconds,
        workload_kind=step.workload_kind,
    )
    _assert_mixed_batch_kubectl_progress(output, failed=failed, collected=True)


def _run_mixed_batch(
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
    """Prepare, fail a mixed batch's second cell, collect, and resume it."""
    context = nullcontext()
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
        context = staged_failure_injection(plan, operations)
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
        context = staged_failure_injection(plan, operations)
    with context:
        _execute_mixed_batch(
            runner, config, fixture, runtime, template, report_workspace, log_dir
        )


def _execute_mixed_batch(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
) -> None:
    """Run the finite scenario while its failure marker remains staged."""
    result_base = f"{runtime.workspace}/results/mixed-batch"
    _reset_result_base(runner, config, fixture, runtime, result_base)
    rows = _mixed_batch_group_rows(fixture, runtime)
    for step, (group_id, _, name) in zip(runtime.scenario.steps, rows, strict=True):
        _sync_step_runtime(
            runner, config, fixture, runtime, step, template, result_base
        )
        output = _mixed_batch_command(runner, config, fixture, runtime, step, log_dir)
        assert_batch_group_publication(output, group_id, name)
        root = _kubectl_output_value(output, "STORAGE_SCALE_TEST_BATCH_RESULTS")
        if runtime.values.get("batch_results_dir", root) != root:
            raise IntegrationTestError("mixed batch append changed the results root")
        runtime.values["batch_results_dir"] = root
    prepared = _copy_scenario_result(
        runner,
        config,
        fixture,
        runtime,
        replace(runtime.scenario.steps[0], name="prepared-batch"),
        root,
    )
    _assert_mixed_batch_prepared(prepared, runtime)
    _assert_mixed_batch_targets(prepared, runtime, rows)
    status = _mixed_batch_command(
        runner,
        config,
        fixture,
        runtime,
        replace(
            runtime.scenario.steps[0],
            name="prepared-status",
            arguments=("--status", root),
        ),
        log_dir,
    )
    assert_group_status_keys(status, rows)
    _finish_mixed_batch(
        runner, config, fixture, runtime, root, report_workspace, log_dir
    )


def _finish_mixed_batch(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    root: str,
    report_workspace: Path,
    log_dir: Path,
) -> None:
    """Start through metadata and resume through IO, preserving first success."""
    start = replace(
        runtime.scenario.steps[1], name="start-mixed-batch", arguments=("--start", root)
    )
    output = _mixed_batch_command(
        runner,
        config,
        fixture,
        runtime,
        start,
        log_dir,
        expected_failure=runtime.selector != "kubectl",
    )
    if runtime.selector == "kubectl":
        _, attempt = _kubectl_submission(output)
        runtime.values["kubectl_result_root"] = root
        _mixed_batch_collect(runner, config, runtime, root, start, log_dir, failed=True)
        runtime.values["batch_first_attempt"] = attempt
    failed = _copy_scenario_result(runner, config, fixture, runtime, start, root)
    _assert_mixed_batch(
        failed,
        runtime,
        (ExecutionStatus.SUCCESS, ExecutionStatus.FAILED, ExecutionStatus.PENDING),
    )
    preserved = _hash_mixed_batch_success(failed)
    resume = replace(
        runtime.scenario.steps[0],
        name="resume-mixed-batch",
        arguments=("--resume", root),
    )
    runtime.values["kubectl_collected"] = "0"
    output = _mixed_batch_command(runner, config, fixture, runtime, resume, log_dir)
    if runtime.selector == "kubectl":
        _, attempt = _kubectl_submission(output)
        if attempt == runtime.values["batch_first_attempt"]:
            raise IntegrationTestError("mixed batch resume reused its failed attempt")
        _mixed_batch_collect(
            runner, config, runtime, root, resume, log_dir, failed=False
        )
    result = _copy_scenario_result(runner, config, fixture, runtime, resume, root)
    _assert_mixed_batch(result, runtime, (ExecutionStatus.SUCCESS,) * 3)
    if _hash_mixed_batch_success(result) != preserved:
        raise IntegrationTestError(
            "mixed batch resume replaced successful first-group evidence"
        )
    _assert_dataset_state(
        runner,
        config,
        fixture,
        resume,
        result,
        None,
        KUBECTL_STORAGE_MOUNT if runtime.selector == "kubectl" else "",
    )
    _mixed_batch_report(runner, report_workspace, result, log_dir, fixture, runtime)


@dataclass(frozen=True)
class TargetRoot:
    """One TEST_DIRS root of a target file."""

    namespace: str
    claim: str
    path: str
    weight: int

    @property
    def normalized(self) -> str:
        """Return the root's ``namespace/claim:path`` form."""
        return f"{self.namespace}/{self.claim}:{self.path}"


@dataclass(frozen=True)
class ExpectedTarget:
    """What one target file must produce: its roots and effective name."""

    roots: tuple[TargetRoot, ...]
    name: str

    @property
    def namespace(self) -> str:
        """Return the one namespace every root of the target shares."""
        return self.roots[0].namespace

    @property
    def claims(self) -> tuple[str, ...]:
        """Return the target's claims, sorted and unique."""
        return tuple(sorted({root.claim for root in self.roots}))

    def dirs_per_claim(self) -> dict[str, int]:
        """Return the generated directories each claim must hold.

        The product generates one directory per unit of weight for every root.
        """
        counts: dict[str, int] = {}
        for root in self.roots:
            counts[root.claim] = counts.get(root.claim, 0) + root.weight
        return counts


@dataclass(frozen=True)
class ExpectedGroup:
    """One group a multi-target batch must contain, in submission order."""

    group_id: str
    kind: str
    target: ExpectedTarget
    target_file: str
    target_file_content: str
    step: ScenarioStep
    executions: tuple[tuple[str, ExpectedExecution], ...]


TEST_DIRS_ENTRY = re.compile(r'\["([^"]+)"\]=(\d+)')
TARGET_NAME_LINE = re.compile(r"^STORAGE_TARGET_NAME=(\S+)$", re.MULTILINE)


def parse_target_file(content: str, default_namespace: str) -> ExpectedTarget:
    """Read a scenario's target file the way the product will.

    Roots are ``[namespace/]claim:path`` keys (the namespace defaults to
    *default_namespace*). The name is the file's STORAGE_TARGET_NAME or else the
    normalized roots, sorted and joined with commas.
    """
    entries = TEST_DIRS_ENTRY.findall(content)
    if not entries:
        raise IntegrationTestError("a target file must set at least one TEST_DIRS root")
    roots = []
    for key, weight in entries:
        namespace, claim, path = parse_kubectl_location(key)
        roots.append(
            TargetRoot(namespace or default_namespace, claim, path, int(weight))
        )
    if len({root.namespace for root in roots}) != 1:
        raise IntegrationTestError(f"target file roots span namespaces: {entries}")
    explicit = TARGET_NAME_LINE.search(content)
    name = (
        explicit.group(1)
        if explicit
        else ",".join(sorted(root.normalized for root in roots))
    )
    return ExpectedTarget(tuple(roots), name)


def expected_multi_target_groups(
    steps: tuple[ScenarioStep, ...],
    values: dict[str, str],
    default_namespace: str,
) -> tuple[ExpectedGroup, ...]:
    """Expand each submission into one group per --target file, in order.

    Group IDs and execution IDs are global and sequential across the batch, and
    a submission's groups are contiguous, so two IO-then-mdtest submissions
    passing two target files give groups 0001/0002 (IO) and 0003/0004 (mdtest).
    """
    groups: list[ExpectedGroup] = []
    next_execution = 1
    for step in steps:
        kind = "mdtest" if step.workload_kind == "mdtest" else "io"
        for file_name, content in _target_file_expectation(step, values).items():
            executions = []
            for execution in step.executions:
                executions.append((f"{next_execution:04d}", execution))
                next_execution += 1
            groups.append(
                ExpectedGroup(
                    f"{len(groups) + 1:04d}",
                    kind,
                    parse_target_file(content, default_namespace),
                    file_name,
                    content,
                    step,
                    tuple(executions),
                )
            )
    return tuple(groups)


def segment_namespaces(expected: tuple[ExpectedGroup, ...]) -> tuple[str, ...]:
    """Return the attempt's segments (namespaces) in first-appearance order."""
    return tuple(dict.fromkeys(group.target.namespace for group in expected))


_BATCH_PREFIX = "STORAGE_SCALE_TEST_BATCH_"
_BATCH_GROUP_KEY = re.compile(
    rf"^{_BATCH_PREFIX}GROUP_(?P<group>\d{{4}})_(?P<suffix>[A-Z]+)=(?P<value>.*)$"
)


def _batch_group_values(output: str, suffix: str) -> list[tuple[str, str]]:
    """Return (group, raw value) pairs for STORAGE_SCALE_TEST_BATCH_GROUP_<gid>_<suffix>."""
    rows = []
    for line in output.splitlines():
        match = _BATCH_GROUP_KEY.match(line)
        if match and match.group("suffix") == suffix:
            rows.append((match.group("group"), match.group("value")))
    return rows


def batch_group_target_names(output: str) -> list[tuple[str, str]]:
    """Return (group, effective target name) pairs from the GROUP_<gid>_TARGET keys."""
    rows = []
    for group, value in _batch_group_values(output, "TARGET"):
        names = shlex.split(value)
        if len(names) != 1:
            raise IntegrationTestError(
                f"malformed batch group target: {group}={value!r}"
            )
        rows.append((group, names[0]))
    return rows


def _has_output_key(output: str, key: str) -> bool:
    """Return whether a product output line starts with exactly ``key=``."""
    return any(line.startswith(f"{key}=") for line in output.splitlines())


def _assert_publication_keys(output: str, group_ids: list[str], label: str) -> None:
    """Require the submission keys that depend on how many groups it published.

    ``_GROUPS`` lists the IDs only for several groups, ``_GROUP`` names the
    first, and neither ``_GROUP_TARGET`` nor ``_GROUP_<gid>_CLAIM`` exists.
    """
    several = len(group_ids) > 1
    if _has_output_key(output, f"{_BATCH_PREFIX}GROUPS") != several:
        raise IntegrationTestError(
            f"{label} {'omitted' if several else 'printed'} the _GROUPS key for "
            f"{len(group_ids)} group(s)"
        )
    if several and _kubectl_output_value(output, f"{_BATCH_PREFIX}GROUPS").split(
        ","
    ) != list(group_ids):
        raise IntegrationTestError(
            f"{label} printed batch groups "
            f"{_kubectl_output_value(output, f'{_BATCH_PREFIX}GROUPS')}, expected "
            + ",".join(group_ids)
        )
    if _has_output_key(output, f"{_BATCH_PREFIX}GROUP_TARGET"):
        raise IntegrationTestError(f"{label} printed the removed GROUP_TARGET key")
    if _kubectl_output_value(output, f"{_BATCH_PREFIX}GROUP") != group_ids[0]:
        raise IntegrationTestError(f"{label} did not name {group_ids[0]} as its group")
    claims = _batch_group_values(output, "CLAIM")
    if claims:
        raise IntegrationTestError(f"{label} printed group claims {claims}")


def assert_batch_submission_output(
    output: str, expected: tuple[ExpectedGroup, ...], step: ScenarioStep
) -> None:
    """Require the submission's group IDs and each group's target name."""
    groups = [group for group in expected if group.step is step]
    _assert_publication_keys(
        output, [group.group_id for group in groups], f"{step.name}"
    )
    observed = batch_group_target_names(output)
    wanted = [(group.group_id, group.target.name) for group in groups]
    if observed != wanted:
        raise IntegrationTestError(
            f"{step.name} printed group targets {observed}, expected {wanted}"
        )


def assert_batch_status_groups(
    output: str, expected: tuple[ExpectedGroup, ...]
) -> None:
    """Require each group's GROUP_<NNNN>_KIND and GROUP_<NNNN>_TARGET keys."""
    assert_group_status_keys(
        output,
        tuple((group.group_id, group.kind, group.target.name) for group in expected),
    )


def assert_group_status_keys(output: str, rows: tuple[GroupRow, ...]) -> None:
    """Require each row's GROUP_<NNNN>_KIND and GROUP_<NNNN>_TARGET keys."""
    for group_id, kind, name in rows:
        prefix = f"GROUP_{group_id}"
        observed_kind = _kubectl_output_value(output, f"{prefix}_KIND")
        names = shlex.split(_kubectl_output_value(output, f"{prefix}_TARGET"))
        if observed_kind != kind or names != [name]:
            raise IntegrationTestError(
                f"batch status group {group_id} is {observed_kind}/{names}, "
                f"expected {kind}/{name}"
            )


def assert_batch_group_publication(output: str, group_id: str, name: str) -> None:
    """Require a one-group submission to publish its own target name, no claim.

    A single group prints GROUP_<gid>_TARGET (and no ``_GROUPS`` list or
    ``_CLAIM`` key) on every substrate.
    """
    _assert_publication_keys(output, [group_id], f"group {group_id}")
    observed = batch_group_target_names(output)
    if observed != [(group_id, name)]:
        raise IntegrationTestError(
            f"submission printed group targets {observed}, expected "
            f"{[(group_id, name)]}"
        )


def assert_report_index_targets(index: str, rows: tuple[GroupRow, ...]) -> None:
    """Require the batch report index's Target column to name each group's target.

    Target names here contain no Markdown-escaped characters, so each row begins
    ``| <group> | <kind> | <name> |``.
    """
    if "| Group | Kind | Target |" not in index:
        raise IntegrationTestError("report index lacks its Target column")
    for group_id, kind, name in rows:
        row = re.compile(
            rf"^\| {re.escape(group_id)} \| {re.escape(kind)} \| "
            rf"{re.escape(name)} \|",
            re.MULTILINE,
        )
        if not row.search(index):
            raise IntegrationTestError(
                f"report index has no row for group {group_id} ({kind}) naming "
                f"target {name!r}"
            )


def _override_entries(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the ``env_override.files`` entries of a parsed env_used.yaml."""
    override = snapshot.get("env_override")
    if override is None:
        return []
    files = override.get("files") if isinstance(override, dict) else None
    if not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
        raise IntegrationTestError(f"malformed env_override record: {override!r}")
    return files


def assert_env_override_files(
    snapshot: dict[str, Any], expected: list[str], label: str
) -> None:
    """Require the snapshot to list exactly *expected* override files in order.

    Recorded paths are absolute, so each must end with its relative path.
    """
    recorded = [str(entry.get("file", "")) for entry in _override_entries(snapshot)]
    if len(recorded) != len(expected) or not all(
        path.endswith(f"/{name}") for path, name in zip(recorded, expected)
    ):
        raise IntegrationTestError(
            f"{label} env_used.yaml records override files {recorded}, "
            f"expected {expected} in that order"
        )


def _assert_multi_target_membership(
    result: Path,
    expected: tuple[ExpectedGroup, ...],
) -> tuple[list[list[str]], list[list[str]]]:
    """Require the committed ledger to hold exactly the expected groups and cells."""
    groups, executions = _mixed_batch_membership(result)
    wanted_executions = [
        (identity, group.group_id)
        for group in expected
        for identity, _execution in group.executions
    ]
    if [(row[1], row[2]) for row in groups] != [
        (group.group_id, group.kind) for group in expected
    ] or [(row[1], row[2]) for row in executions] != wanted_executions:
        raise IntegrationTestError(
            "multi-target batch ledger does not hold one group per submission "
            f"and target: groups={groups}, executions={executions}"
        )
    return groups, executions


def generated_dirs_per_claim(csv: str) -> dict[str, int]:
    """Count a reified execution's generated directories on each claim.

    Entries are the logical ``[namespace/]claim:path`` directories of
    ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV (a Pod path names its claim too).
    """
    counts: dict[str, int] = {}
    for entry in (item for item in csv.split(",") if item):
        claim = parse_kubectl_location(entry)[1]
        counts[claim] = counts.get(claim, 0) + 1
    return counts


def _assert_group_generated_dirs(result: Path, expected: ExpectedGroup) -> None:
    """Every cell of a group spreads its directories over the claims by weight.

    The reified execution definitions already hold the generated directories,
    so the split of an aggregate target needs no cluster access.
    """
    wanted = expected.target.dirs_per_claim()
    for identity, _ in expected.executions:
        csv = _shell_assignments(result / "executions" / f"{identity}.sh").get(
            "ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV", ""
        )
        observed = generated_dirs_per_claim(csv)
        if observed != wanted:
            raise IntegrationTestError(
                f"group {expected.group_id} cell {identity} generated directories "
                f"{observed} per claim, expected {wanted}"
            )


def _assert_group_target_snapshot(
    result: Path, group_row: list[str], expected: ExpectedGroup
) -> None:
    """Each group's snapshot records its namespace, name, and target file.

    ``KUBECTL_PV``/``KUBECTL_PVC`` are gone, ``KUBECTL_NAMESPACE`` is the
    group's resolved namespace, and ``KUBECTL_MAPPED_TEST_DIRS`` holds the Pod
    path of every root.
    """
    output = result / group_row[3]
    target = expected.target
    label = f"group {expected.group_id}"
    shell = (output / "env_used.sh").read_text(encoding="utf-8")
    if re.search(r"^(?:export )?KUBECTL_PV(?:C)?=", shell, re.MULTILINE):
        raise IntegrationTestError(f"{label} env_used.sh still records a PV or PVC")
    for root in target.roots:
        pod_path = f"{kubectl_claim_mount(root.claim)}/{root.path}"
        if pod_path not in shell:
            raise IntegrationTestError(
                f"{label} env_used.sh does not map {root.normalized} to {pod_path}"
            )
    snapshot = _env_used_snapshot(output / "env_used.yaml")
    for key, value in (
        ("KUBECTL_NAMESPACE", target.namespace),
        ("STORAGE_TARGET_NAME", target.name),
    ):
        if snapshot.get(key) != value:
            raise IntegrationTestError(
                f"{label} env_used.yaml records {key}={snapshot.get(key)!r}, "
                f"expected {value!r}"
            )
    assert_target_file_record(
        snapshot,
        (expected.target_file, expected.target_file_content, {"TEST_DIRS"}),
        label,
    )


def _assert_multi_target_result(
    result: Path,
    expected: tuple[ExpectedGroup, ...],
    status: ExecutionStatus,
) -> None:
    """Check ledger, per-group tuple, and (when SUCCESS) result evidence."""
    groups, executions = _assert_multi_target_membership(result, expected)
    by_execution = {row[1]: row for row in executions}
    for group_row, group in zip(groups, expected, strict=True):
        _assert_group_target_snapshot(result, group_row, group)
        _assert_group_generated_dirs(result, group)
        for identity, execution in group.executions:
            _assert_batch_cell(
                result,
                group_row,
                by_execution[identity],
                group.step,
                execution.coordinate,
                status,
            )


def _owned_kubectl_objects(runner: Any, config: Any, namespace: str) -> list[str]:
    """List the tool-owned objects of every lifecycle kind in one namespace."""
    result = runner.run(
        _kubectl(
            config,
            "-n",
            namespace,
            "get",
            KUBECTL_OWNED_KINDS,
            "-l",
            KUBECTL_OWNER_SELECTOR,
            "-o",
            "name",
        ),
        timeout=60,
    )
    return sorted(line for line in result.stdout.splitlines() if line.strip())


def _assert_no_owned_kubectl_objects(
    runner: Any,
    config: Any,
    namespaces: tuple[str, ...],
    *,
    settle_seconds: int = 90,
) -> None:
    """Require that no Lease, Job, DaemonSet, policy, Pod, or RBAC object remains.

    Deletions finish asynchronously, so a bounded interval lets terminating
    Pods disappear; whatever remains afterward is a leak in some namespace.
    """
    deadline = time.monotonic() + settle_seconds
    while True:
        leftovers = {
            namespace: objects
            for namespace in namespaces
            if (objects := _owned_kubectl_objects(runner, config, namespace))
        }
        if not leftovers:
            return
        if time.monotonic() >= deadline:
            raise IntegrationTestError(
                f"tool-owned Kubernetes objects remain after cleanup: {leftovers}"
            )
        time.sleep(2)


def _count_attempt_objects(
    runner: Any,
    config: Any,
    namespace: str,
    kinds: str,
    attempt_id: str,
    component: str,
) -> int:
    """Count one attempt's objects of *kinds* with one lifecycle component."""
    result = runner.run(
        _kubectl(
            config,
            "-n",
            namespace,
            "get",
            kinds,
            "-l",
            f"storage-scale-test.nvidia.com/run={attempt_id},"
            f"app.kubernetes.io/component={component}",
            "-o",
            "name",
        ),
        timeout=60,
    )
    return len([line for line in result.stdout.splitlines() if line.strip()])


def _assert_multi_target_objects(
    runner: Any,
    config: Any,
    expected: tuple[ExpectedGroup, ...],
    attempt_id: str,
) -> None:
    """Before collection: a Lease per claim and a coordinator Job per namespace.

    A sequencer Job exists only for several namespaces and runs in the first
    one.
    """
    namespaces = segment_namespaces(expected)
    for index, namespace in enumerate(namespaces):
        claims = {
            claim
            for group in expected
            if group.target.namespace == namespace
            for claim in group.target.claims
        }
        expectations = [
            ("leases.coordination.k8s.io", LEASE_COMPONENT, len(claims)),
            ("jobs.batch", COORDINATOR_COMPONENT, 1),
            (
                "jobs.batch",
                SEQUENCER_COMPONENT,
                1 if index == 0 and len(namespaces) > 1 else 0,
            ),
        ]
        for kinds, component, count in expectations:
            observed = _count_attempt_objects(
                runner, config, namespace, kinds, attempt_id, component
            )
            if observed != count:
                raise IntegrationTestError(
                    f"{namespace} holds {observed} {component} "
                    f"{kinds} objects for attempt {attempt_id}, expected {count}"
                )


def _assert_claim_run_trees(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    targets: tuple[KubectlStorageTarget, ...],
    attempt_id: str,
    *,
    present: bool,
) -> None:
    """Require the attempt's run tree on every given claim, or on none.

    The run tree sits under the control root, which the caller chose with
    ``kubectl_control_root`` or which the scenario's single claim defines.
    """
    remote_run = f"{_kubectl_remote_control_root(runtime)}/runs/{attempt_id}"
    command = f"test {'-d' if present else '! -e'} {shlex.quote(remote_run)}"
    for_each_target(
        lambda target: _storage_utility_shell(
            runner, config, fixture, command, target=target
        ),
        targets,
    )


def _kubectl_wait_for_next_action(
    runner: Any,
    config: Any,
    runtime: ScenarioRuntime,
    result_root: Path,
    log_dir: Path,
    expected: str,
    timeout: int,
) -> str:
    """Poll ``--status`` until NEXT_ACTION names the expected action.

    This proves nothing about the in-cluster sequencer: for a multi-target
    attempt, ``--status`` itself starts the next suspended coordinator Job
    when the sequencer is lost (a recovery path), so polling it can mask a
    broken sequencer. A caller that must prove the sequencer chained the Jobs
    waits for its Job first (``_prove_sequencer``). Same-namespace targets run
    in one Job with no sequencer, so polling alone is exact there. Returns the
    final status output.
    """
    deadline = time.monotonic() + timeout
    last_error: IntegrationTestError | None = None
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
            action = _kubectl_output_value(output, "NEXT_ACTION")
            state = _kubectl_lifecycle_state(output)
        except IntegrationTestError as error:
            last_error = error
        else:
            if action == expected:
                return output
            if state in KUBECTL_TERMINAL_STATES:
                raise IntegrationTestError(
                    f"attempt is {state} but NEXT_ACTION={action}, "
                    f"expected {expected}"
                )
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    detail = f"; last status error: {last_error}" if last_error else ""
    raise IntegrationTestError(
        f"attempt at {result_root} did not reach NEXT_ACTION={expected} within "
        f"{timeout} seconds{detail}"
    )


def _stale_target_keys(output: str) -> list[str]:
    """Return the removed ``TARGET_*`` status keys a product output still prints."""
    return sorted(
        line.partition("=")[0]
        for line in output.splitlines()
        if line.startswith("TARGET_")
    )


def assert_segment_states(
    output: str, namespaces: tuple[str, ...], states: tuple[str, ...]
) -> None:
    """Require the status keys of an attempt that spans several namespaces.

    ``SEGMENT_COUNT`` and, for each segment in order, ``SEGMENT_<i>_NAMESPACE``
    and ``SEGMENT_<i>_STATE`` must equal *namespaces* and *states*, and the
    ``TARGET_<i>_*`` keys they replace must be gone.
    """
    if len(namespaces) != len(states):
        raise IntegrationTestError("one expected state is needed per namespace")
    count = _kubectl_output_value(output, "SEGMENT_COUNT")
    indexes = range(SEGMENT_INDEX_BASE, SEGMENT_INDEX_BASE + len(namespaces))
    observed = tuple(
        (
            _kubectl_output_value(output, f"SEGMENT_{index}_NAMESPACE"),
            _kubectl_output_value(output, f"SEGMENT_{index}_STATE"),
        )
        for index in indexes
    )
    if count != str(len(namespaces)) or observed != tuple(zip(namespaces, states)):
        raise IntegrationTestError(
            f"segments are {count} {observed}, expected {len(namespaces)} "
            f"{tuple(zip(namespaces, states))}"
        )
    if stale := _stale_target_keys(output):
        raise IntegrationTestError(f"status still prints removed keys: {stale}")


def assert_no_segment_keys(output: str) -> None:
    """Require a single-namespace status to carry no SEGMENT_* or TARGET_* keys."""
    keys = sorted(
        line.partition("=")[0]
        for line in output.splitlines()
        if line.startswith("SEGMENT_")
    ) + _stale_target_keys(output)
    if keys:
        raise IntegrationTestError(
            f"a single-namespace attempt printed segment or target keys: {keys}"
        )


def _attempt_selector(attempt_id: str, component: str) -> str:
    """Return the label selector of one attempt's objects of one component."""
    return (
        f"storage-scale-test.nvidia.com/run={attempt_id},"
        f"app.kubernetes.io/component={component}"
    )


def sequencer_job_state(document: dict[str, Any]) -> str:
    """Classify a Job list's sequencer Job: MISSING, RUNNING, COMPLETE, or FAILED."""
    items = document.get("items") or []
    if not items:
        return "MISSING"
    if len(items) != 1:
        raise IntegrationTestError(f"expected one sequencer Job, found {len(items)}")
    conditions = items[0].get("status", {}).get("conditions") or []
    true_types = {
        item.get("type") for item in conditions if item.get("status") == "True"
    }
    for state, condition in (("COMPLETE", "Complete"), ("FAILED", "Failed")):
        if condition in true_types:
            return state
    return "RUNNING"


def assert_sequencer_log(log: str, namespaces: tuple[str, ...]) -> None:
    """Require the sequencer to have started and finished each segment in order.

    A segment is a namespace. Each segment's Job must be named in that
    segment's own namespace, which is what the cross-namespace RoleBinding makes
    possible for later segments.
    """
    unit = SEQUENCER_UNIT_PATTERN
    milestones: list[tuple[str, str]] = []
    for index, namespace in enumerate(namespaces):
        job = rf"\(Job {re.escape(namespace)}/\S+\)"
        milestones.append(
            (rf"starting {unit} {index} {job}", f"start of segment {index}")
        )
        milestones.append(
            (rf"{unit} {index} {job} complete", f"completion of segment {index}")
        )
    milestones.append((rf"all {unit}s complete", "completion of all segments"))
    position = 0
    for pattern, description in milestones:
        match = re.compile(pattern).search(log, position)
        if match is None:
            raise IntegrationTestError(
                f"sequencer log lacks the {description}, in order"
            )
        position = match.end()


def _best_effort_kubectl(runner: Any, config: Any, title: str, *arguments: str) -> str:
    """Return one kubectl command's output for an error message, never raising."""
    try:
        result = runner.run(_kubectl(config, *arguments), check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as error:
        return f"--- {title}: {error!r}"
    return f"--- {title} (exit {result.returncode}) ---\n{result.stdout}{result.stderr}"


def _multi_target_failure_detail(
    runner: Any,
    config: Any,
    namespaces: tuple[str, ...],
    attempt_id: str,
) -> str:
    """Return every namespace's Job and Pod states plus the sequencer log."""
    sections = [
        _best_effort_kubectl(
            runner,
            config,
            f"Jobs and Pods in {namespace}",
            "-n",
            namespace,
            "get",
            "jobs.batch,pods",
            "-l",
            f"storage-scale-test.nvidia.com/run={attempt_id}",
            "-o",
            "wide",
        )
        for namespace in dict.fromkeys(namespaces)
    ]
    sections.append(
        _best_effort_kubectl(
            runner,
            config,
            "sequencer Pod log",
            "-n",
            namespaces[0],
            "logs",
            "-l",
            _attempt_selector(attempt_id, SEQUENCER_COMPONENT),
            "--tail=200",
        )
    )
    return "\n".join(sections)[-12000:]


def _wait_for_sequencer_job(
    runner: Any,
    config: Any,
    namespaces: tuple[str, ...],
    attempt_id: str,
    timeout: int,
) -> None:
    """Wait, with kubectl alone, for the attempt's sequencer Job to be Complete.

    Raises with the Job and Pod states and the sequencer log if the Job fails,
    vanishes, or does not complete in time.
    """
    command = _kubectl(
        config,
        "-n",
        namespaces[0],
        "get",
        "jobs.batch",
        "-l",
        _attempt_selector(attempt_id, SEQUENCER_COMPONENT),
        "-o",
        "json",
    )
    deadline = time.monotonic() + timeout
    problem = ""
    while not problem:
        result = runner.run(command, check=False, timeout=60)
        if result.returncode == 0:
            state = sequencer_job_state(json.loads(result.stdout))
            if state == "COMPLETE":
                return
            if state != "RUNNING":
                problem = f"the sequencer Job is {state}"
        if not problem and time.monotonic() >= deadline:
            problem = f"the sequencer Job did not complete within {timeout} seconds"
        if not problem:
            time.sleep(2)
    detail = _multi_target_failure_detail(runner, config, namespaces, attempt_id)
    raise IntegrationTestError(f"attempt {attempt_id}: {problem}\n{detail}")


def _prove_sequencer(
    runner: Any,
    config: Any,
    namespaces: tuple[str, ...],
    attempt_id: str,
    log_dir: Path,
    timeout: int,
) -> None:
    """Prove the in-cluster sequencer chained the segments, without ``--status``.

    ``--status`` starts the next segment itself when the sequencer is lost, so
    nothing may call it until the sequencer Job has completed and its log shows
    every hand-off.
    """
    _wait_for_sequencer_job(runner, config, namespaces, attempt_id, timeout)
    result = runner.run(
        _kubectl(
            config,
            "-n",
            namespaces[0],
            "logs",
            "-l",
            _attempt_selector(attempt_id, SEQUENCER_COMPONENT),
            "--tail=-1",
        ),
        check=False,
        timeout=60,
    )
    log = result.stdout + result.stderr
    (log_dir / "kubectl-sequencer.log").write_text(log, encoding="utf-8")
    try:
        if result.returncode:
            raise IntegrationTestError(
                f"could not read the sequencer log (exit {result.returncode})"
            )
        assert_sequencer_log(log, namespaces)
    except IntegrationTestError as error:
        detail = _multi_target_failure_detail(runner, config, namespaces, attempt_id)
        raise IntegrationTestError(
            f"attempt {attempt_id}: {error}\n{detail}"
        ) from error


SEQUENCER_COMPONENT = "sequencer"
RBAC_API_GROUP = "rbac.authorization.k8s.io"
SERVICE_ACCOUNT_KIND = "ServiceAccount"
RESTRICTED_NAMESPACE_RULES = (
    {
        "apiGroups": ["coordination.k8s.io"],
        "resources": ["leases"],
        "verbs": ["get", "create", "delete"],
    },
    {"apiGroups": [""], "resources": ["persistentvolumeclaims"], "verbs": ["get"]},
    {
        "apiGroups": ["batch"],
        "resources": ["jobs"],
        "verbs": ["get", "list", "create", "patch", "delete"],
    },
)
RESTRICTED_CLUSTER_RULES = (
    {
        "apiGroups": [""],
        "resources": ["namespaces", "persistentvolumes"],
        "verbs": ["get"],
    },
)
RESTRICTED_FIXTURE_LABELS = {"storage-scale-test/fixture-utility": "true"}


def _restricted_object(
    kind: str, name: str, namespace: str | None, **body: Any
) -> dict[str, Any]:
    """Return one labelled RBAC or identity object of the restricted manifest."""
    metadata: dict[str, Any] = {"name": name, "labels": RESTRICTED_FIXTURE_LABELS}
    if namespace:
        metadata["namespace"] = namespace
    api = "v1" if kind == SERVICE_ACCOUNT_KIND else f"{RBAC_API_GROUP}/v1"
    return {"apiVersion": api, "kind": kind, "metadata": metadata, **body}


def restricted_identity_manifest(
    name: str, namespaces: tuple[str, ...]
) -> dict[str, Any]:
    """Return a List creating a ServiceAccount that may not manage RBAC.

    It may read the claims, namespaces and PVs, hold Leases, and run Jobs in
    each namespace, which is what a storage target's identity checks and its
    coordinator Jobs need. It may not create or delete ServiceAccounts, Roles,
    or RoleBindings, which a multi-target batch needs for its sequencer.
    """
    subject = {"kind": SERVICE_ACCOUNT_KIND, "name": name, "namespace": namespaces[0]}
    items = [_restricted_object(SERVICE_ACCOUNT_KIND, name, namespaces[0])]
    for namespace in namespaces:
        items.append(
            _restricted_object(
                "Role", name, namespace, rules=list(RESTRICTED_NAMESPACE_RULES)
            )
        )
        items.append(
            _restricted_object(
                "RoleBinding",
                name,
                namespace,
                roleRef={
                    "apiGroup": RBAC_API_GROUP,
                    "kind": "Role",
                    "name": name,
                },
                subjects=[subject],
            )
        )
    items.append(
        _restricted_object(
            "ClusterRole", name, None, rules=list(RESTRICTED_CLUSTER_RULES)
        )
    )
    items.append(
        _restricted_object(
            "ClusterRoleBinding",
            name,
            None,
            roleRef={
                "apiGroup": RBAC_API_GROUP,
                "kind": "ClusterRole",
                "name": name,
            },
            subjects=[subject],
        )
    )
    return {"apiVersion": "v1", "kind": "List", "items": items}


def assert_rbac_denial_output(
    returncode: int, output: str, namespaces: tuple[str, ...]
) -> None:
    """Require validate_env.sh --sequencer to have failed on a restricted
    identity's RBAC gaps.

    *namespaces* are the ones validate_env.sh sees, which come from env.sh alone.
    It must exit non-zero, name each missing create/delete permission on
    ServiceAccounts (the first namespace) and Roles and RoleBindings (every
    namespace) with the fallback advice, and not complain about the Job
    permissions the identity was given.
    """
    problems = []
    if returncode == 0:
        problems.append("it exited 0 although --sequencer asked for the sequencer")
    if SEQUENCER_FALLBACK_PHRASE not in output:
        problems.append(f"it did not print {SEQUENCER_FALLBACK_PHRASE!r}")
    denied = [("serviceaccounts", namespaces[0])]
    for namespace in namespaces:
        denied.append(("roles.rbac.authorization.k8s.io", namespace))
        denied.append(("rolebindings.rbac.authorization.k8s.io", namespace))
    for resource, namespace in denied:
        for verb in ("create", "delete"):
            phrase = RBAC_PERMISSION_PHRASE.format(
                verb=verb, resource=resource, namespace=namespace
            )
            if phrase not in output:
                problems.append(f"it did not report missing {phrase!r}")
    for namespace in namespaces:
        phrase = RBAC_PERMISSION_PHRASE.format(
            verb="create", resource="jobs.batch", namespace=namespace
        )
        if phrase in output:
            problems.append(f"it denied a granted permission: {phrase!r}")
    if problems:
        raise IntegrationTestError(
            "restricted-identity validation: "
            + "; ".join(problems)
            + f"\n{output.strip()[-6000:]}"
        )


def _run_kubectl_manifest(
    runner: Any,
    config: Any,
    verb: tuple[str, ...],
    document: dict[str, Any],
    *,
    check: bool = True,
) -> Any:
    """Run ``kubectl <verb> -f -`` with *document* on stdin."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as stream:
        stream.write(json.dumps(document))
        stream.flush()
        stream.seek(0)
        return runner.run(
            _kubectl(config, *verb, "-f", "-"),
            stdin=stream,
            check=check,
            timeout=60,
        )


def _wait_for_restricted_grants(
    runner: Any, kubeconfig: Path, namespaces: tuple[str, ...]
) -> None:
    """Wait until the new identity can run Jobs in every namespace."""
    for namespace in namespaces:
        deadline = time.monotonic() + 20
        while True:
            result = runner.run(
                [
                    "kubectl",
                    "--kubeconfig",
                    kubeconfig,
                    "-n",
                    namespace,
                    "auth",
                    "can-i",
                    "create",
                    "jobs.batch",
                ],
                check=False,
                timeout=30,
            )
            if result.stdout.strip() == "yes":
                break
            if time.monotonic() >= deadline:
                raise IntegrationTestError(
                    f"the restricted identity was never granted Job access in {namespace}"
                )
            time.sleep(1)


def _assert_validation_denied_without_rbac(
    runner: Any,
    config: Any,
    runtime: ScenarioRuntime,
    namespaces: tuple[str, ...],
    log_dir: Path,
) -> None:
    """Run validate_env.sh --sequencer as an identity that cannot manage RBAC.

    It must fail, reporting each missing sequencer permission in the environment's
    namespace (the first of *namespaces*); the identity is granted Job access in
    all of them. The temporary ServiceAccount, Roles, RoleBindings, ClusterRole,
    and ClusterRoleBinding are always deleted.
    """
    name = f"sst-itest-restricted-{secrets.token_hex(3)}"
    manifest = restricted_identity_manifest(name, namespaces)
    try:
        _run_kubectl_manifest(runner, config, ("create",), manifest)
        token = runner.run(
            _kubectl(
                config,
                "-n",
                namespaces[0],
                "create",
                "token",
                name,
                "--duration=600s",
            ),
            timeout=30,
        ).stdout.strip()
        with tempfile.TemporaryDirectory(prefix="sst-restricted-") as directory:
            kubeconfig = Path(directory) / "kubeconfig"
            _write_token_kubeconfig(runner, config, kubeconfig, token)
            _wait_for_restricted_grants(runner, kubeconfig, namespaces)
            result = _run_kubectl_validate_env(
                runner, runtime, kubeconfig, sequencer=True
            )
        output = result.stdout + result.stderr
        (log_dir / "kubectl-rbac-denied-validate-env.log").write_text(
            output, encoding="utf-8"
        )
        assert_rbac_denial_output(result.returncode, output, namespaces[:1])
    finally:
        deleted = _run_kubectl_manifest(
            runner,
            config,
            ("delete", "--ignore-not-found", "--wait=false"),
            manifest,
            check=False,
        )
        if deleted.returncode:
            LOG.warning(
                "could not delete restricted identity %s: %s",
                name,
                (deleted.stderr or deleted.stdout).strip(),
            )


def assert_pod_mounts_claims(
    pod: dict[str, Any], claims: tuple[str, ...], label: str
) -> None:
    """Require a Pod spec to mount every claim in its own directory.

    Each claim's volume must be mounted at ``<mount>/<claim>`` and nothing else
    may be mounted from a claim.
    """
    claim_of = {
        volume.get("name"): volume["persistentVolumeClaim"].get("claimName")
        for volume in pod.get("volumes") or []
        if "persistentVolumeClaim" in volume
    }
    mounted = {
        claim_of[mount.get("name")]: mount.get("mountPath")
        for container in pod.get("containers") or []
        for mount in container.get("volumeMounts") or []
        if mount.get("name") in claim_of
    }
    wanted = {claim: kubectl_claim_mount(claim) for claim in claims}
    if mounted != wanted:
        raise IntegrationTestError(
            f"the {label} Pod mounts claims {mounted}, expected {wanted}"
        )


def _pod_template_spec(item: dict[str, Any]) -> dict[str, Any]:
    """Return a Job's or DaemonSet's Pod spec."""
    return item.get("spec", {}).get("template", {}).get("spec", {})


def assert_single_coordinator_job(
    document: dict[str, Any], claims: tuple[str, ...]
) -> None:
    """Require one plain coordinator Job whose Pod mounts every claim.

    One namespace shares one Pod: no init containers, one container, and each
    claim in its own directory.
    """
    items = document.get("items") or []
    if len(items) != 1:
        raise IntegrationTestError(
            f"expected exactly one coordinator Job, found {len(items)}"
        )
    pod = _pod_template_spec(items[0])
    init_containers = pod.get("initContainers") or []
    containers = pod.get("containers") or []
    if init_containers or len(containers) != 1:
        raise IntegrationTestError(
            f"the coordinator Pod has {len(init_containers)} init containers and "
            f"{len(containers)} containers, expected 0 and 1"
        )
    assert_pod_mounts_claims(pod, claims, "coordinator")


def assert_single_worker_daemonset(
    document: dict[str, Any], claims: tuple[str, ...]
) -> None:
    """Require one worker DaemonSet whose Pods mount every claim."""
    items = document.get("items") or []
    if len(items) != 1:
        raise IntegrationTestError(
            f"expected exactly one worker DaemonSet, found {len(items)}"
        )
    assert_pod_mounts_claims(_pod_template_spec(items[0]), claims, "worker")


SEQUENCER_RBAC_PREFIXES = (
    "serviceaccount/",
    "role.rbac.authorization.k8s.io/",
    "rolebinding.rbac.authorization.k8s.io/",
)


def sequencer_rbac_objects(objects: list[str]) -> list[str]:
    """Return the owned objects only a sequenced attempt creates (RBAC kinds)."""
    return [name for name in objects if name.startswith(SEQUENCER_RBAC_PREFIXES)]


def _attempt_object_document(
    runner: Any,
    config: Any,
    namespace: str,
    kinds: str,
    attempt_id: str,
    component: str,
) -> dict[str, Any]:
    """Return one attempt's objects of *kinds* with one component as JSON."""
    result = runner.run(
        _kubectl(
            config,
            "-n",
            namespace,
            "get",
            kinds,
            "-l",
            _attempt_selector(attempt_id, component),
            "-o",
            "json",
        ),
        timeout=60,
    )
    return json.loads(result.stdout)


def _assert_same_namespace_objects(
    runner: Any,
    config: Any,
    expected: tuple[ExpectedGroup, ...],
    attempt_id: str,
) -> None:
    """One coordinator Job and one worker set mount every claim; no sequencer."""
    namespace = segment_namespaces(expected)[0]
    claims = tuple(
        sorted({claim for group in expected for claim in group.target.claims})
    )
    assert_single_coordinator_job(
        _attempt_object_document(
            runner, config, namespace, "jobs.batch", attempt_id, COORDINATOR_COMPONENT
        ),
        claims,
    )
    assert_single_worker_daemonset(
        _attempt_object_document(
            runner, config, namespace, "daemonsets.apps", attempt_id, WORKER_COMPONENT
        ),
        claims,
    )
    sequencers = _count_attempt_objects(
        runner, config, namespace, "jobs.batch", attempt_id, SEQUENCER_COMPONENT
    )
    leftovers = sequencer_rbac_objects(
        _owned_kubectl_objects(runner, config, namespace)
    )
    if sequencers or leftovers:
        raise IntegrationTestError(
            f"one-namespace targets created {sequencers} sequencer Jobs and "
            f"sequencer objects {leftovers}, expected none"
        )


def _multi_target_prepare(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    log_dir: Path,
    expected: tuple[ExpectedGroup, ...],
    *,
    validate: bool = False,
    sequencer: bool = False,
) -> tuple[str, str]:
    """Run the batch and append submissions.

    Returns the batch results root and, when *validate* is set, the output of
    validate_env.sh run once with the first step's environment (else ""),
    checking the sequencer too when *sequencer* is set.
    """
    result_base = f"{runtime.workspace}/results/multi-target"
    _reset_result_base(runner, config, fixture, runtime, result_base)
    root = ""
    validation = ""
    for step in runtime.scenario.steps:
        _sync_step_runtime(
            runner, config, fixture, runtime, step, template, result_base
        )
        if validate and not validation:
            validation = _validate_step_environment(
                runner, config, fixture, runtime, step, log_dir, sequencer=sequencer
            )
        output = _mixed_batch_command(runner, config, fixture, runtime, step, log_dir)
        assert_batch_submission_output(output, expected, step)
        root = _kubectl_output_value(output, "STORAGE_SCALE_TEST_BATCH_RESULTS")
        if runtime.values.get("batch_results_dir", root) != root:
            raise IntegrationTestError("append changed the batch results root")
        runtime.values["batch_results_dir"] = root
    return root, validation


def _expected_groups(
    fixture: Fixture, runtime: ScenarioRuntime
) -> tuple[ExpectedGroup, ...]:
    """Return the groups the scenario's submissions must publish."""
    return expected_multi_target_groups(
        runtime.scenario.steps, runtime.values, str(fixture.kubectl_namespace)
    )


def _run_kubectl_multi_target(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
) -> None:
    """Run IO and mdtest groups on two namespaces' claims in one sequenced attempt.

    The environment is validated with --sequencer (the real sequencer probe
    included) and a restricted identity's RBAC gaps must fail that validation
    before the attempt starts. After
    ``--start`` the sequencer Job must complete and log each hand-off before
    anything polls ``--status``, which would otherwise start the next segment
    itself.
    """
    targets = _scenario_storage_targets(fixture, runtime)
    expected = _expected_groups(fixture, runtime)
    namespaces = segment_namespaces(expected)
    root, _ = _multi_target_prepare(
        runner,
        config,
        fixture,
        runtime,
        template,
        log_dir,
        expected,
        validate=True,
        sequencer=True,
    )
    timeout = runtime.scenario.steps[0].timeout_seconds
    prepared = _copy_scenario_result(
        runner,
        config,
        fixture,
        runtime,
        replace(runtime.scenario.steps[0], name="prepared-batch"),
        root,
    )
    _assert_multi_target_result(prepared, expected, ExecutionStatus.PENDING)
    prepared_status = _run_kubectl_command(
        runner, config, runtime, "prepared-status", ("--status", root), log_dir, 120
    )
    assert_batch_status_groups(prepared_status, expected)
    _assert_validation_denied_without_rbac(runner, config, runtime, namespaces, log_dir)

    # Start through the metadata launcher, as mixed-batch does, to prove either
    # launcher can start a batch holding both kinds.
    start = replace(
        runtime.scenario.steps[1],
        name="start-multi-target",
        arguments=("--start", root),
    )
    runtime.values["kubectl_result_root"] = root
    runtime.values["kubectl_collected"] = "0"
    output = _mixed_batch_command(runner, config, fixture, runtime, start, log_dir)
    _, attempt_id = _kubectl_submission(output)
    _prove_sequencer(runner, config, namespaces, attempt_id, log_dir, timeout)
    final_status = _kubectl_wait_for_next_action(
        runner, config, runtime, Path(root), log_dir, "COLLECT", timeout
    )
    if _kubectl_lifecycle_state(final_status) != "SUCCESS":
        raise IntegrationTestError(
            f"multi-target attempt ended in {_kubectl_lifecycle_state(final_status)}"
        )
    assert_batch_status_groups(final_status, expected)
    assert_segment_states(final_status, namespaces, ("SUCCESS",) * len(namespaces))
    _assert_multi_target_objects(runner, config, expected, attempt_id)
    _assert_claim_run_trees(
        runner, config, fixture, runtime, targets, attempt_id, present=True
    )
    _kubectl_collect_result(runner, config, runtime, Path(root), log_dir)
    runtime.values["kubectl_collected"] = "1"
    _assert_no_owned_kubectl_objects(runner, config, namespaces)
    _assert_claim_run_trees(
        runner, config, fixture, runtime, targets, attempt_id, present=False
    )
    _finish_multi_target(
        runner, config, fixture, runtime, report_workspace, log_dir, root, expected
    )


def _finish_multi_target(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    report_workspace: Path,
    log_dir: Path,
    root: str,
    expected: tuple[ExpectedGroup, ...],
    *,
    reports: bool = True,
    check_cleanup: bool = False,
) -> None:
    """Check collected status keys, per-group results, and per-group reports.

    With *check_cleanup*, every generated directory of every group must be gone
    from all of the namespace's claims.
    """
    collected_status = _run_kubectl_command(
        runner, config, runtime, "collected-status", ("--status", root), log_dir, 120
    )
    assert_batch_status_groups(collected_status, expected)
    result = _copy_scenario_result(
        runner,
        config,
        fixture,
        runtime,
        replace(runtime.scenario.steps[0], name="collected-batch"),
        root,
    )
    _assert_multi_target_result(result, expected, ExecutionStatus.SUCCESS)
    if check_cleanup:
        _assert_dataset_state(
            runner,
            config,
            fixture,
            runtime.scenario.steps[0],
            result,
            None,
            KUBECTL_STORAGE_MOUNT,
        )
    if not reports:
        return
    report_root = _batch_report(
        runner,
        report_workspace,
        result,
        log_dir,
        tuple(group.group_id for group in expected),
        "reports-multi-target",
    )
    assert_report_index_targets(
        (report_root / "index.md").read_text(encoding="utf-8"),
        tuple((group.group_id, group.kind, group.target.name) for group in expected),
    )


def _run_kubectl_same_namespace_targets(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
) -> None:
    """Run an aggregate target and a separate target in one namespace's attempt.

    There is no sequencer to prove, so ``--status`` may be polled freely. The
    attempt must use one plain coordinator Job and one worker DaemonSet, each
    mounting both claims, and create no sequencer, ServiceAccount, Role, or
    RoleBinding. The aggregate group's directories must split 2:1 over its
    claims, every cell must succeed, and both claims must be clean afterward.
    """
    targets = _scenario_storage_targets(fixture, runtime)
    expected = _expected_groups(fixture, runtime)
    roots = [root.normalized for group in expected for root in group.target.roots]
    # The control root sits on the claim of the lexically first root.
    runtime.values["kubectl_control_root"] = kubectl_control_root(roots)
    control_claim = parse_kubectl_location(min(roots))[1]
    control_targets = tuple(target for target in targets if target.pvc == control_claim)
    root, _ = _multi_target_prepare(
        runner, config, fixture, runtime, template, log_dir, expected, validate=True
    )
    timeout = runtime.scenario.steps[0].timeout_seconds
    start = replace(
        runtime.scenario.steps[0],
        name="start-same-namespace",
        arguments=("--start", root),
    )
    runtime.values["kubectl_result_root"] = root
    runtime.values["kubectl_collected"] = "0"
    output = _mixed_batch_command(runner, config, fixture, runtime, start, log_dir)
    _, attempt_id = _kubectl_submission(output)
    _assert_same_namespace_objects(runner, config, expected, attempt_id)
    _assert_multi_target_objects(runner, config, expected, attempt_id)
    final_status = _kubectl_wait_for_next_action(
        runner, config, runtime, Path(root), log_dir, "COLLECT", timeout
    )
    if _kubectl_lifecycle_state(final_status) != "SUCCESS":
        raise IntegrationTestError(
            f"same-namespace attempt ended in {_kubectl_lifecycle_state(final_status)}"
        )
    assert_no_segment_keys(final_status)
    assert_batch_status_groups(final_status, expected)
    _assert_claim_run_trees(
        runner, config, fixture, runtime, control_targets, attempt_id, present=True
    )
    _kubectl_collect_result(runner, config, runtime, Path(root), log_dir)
    runtime.values["kubectl_collected"] = "1"
    _assert_no_owned_kubectl_objects(runner, config, segment_namespaces(expected))
    _assert_claim_run_trees(
        runner, config, fixture, runtime, control_targets, attempt_id, present=False
    )
    _finish_multi_target(
        runner,
        config,
        fixture,
        runtime,
        report_workspace,
        log_dir,
        root,
        expected,
        reports=False,
        check_cleanup=True,
    )


def _unbound_claim_manifest(target: KubectlStorageTarget) -> str:
    """Render a scenario-owned claim whose volumeName names no PV, so it never binds."""
    return json.dumps(
        {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": {
                "name": target.pvc,
                "namespace": target.namespace,
                "labels": {"storage-scale-test/fixture-utility": "true"},
            },
            "spec": {
                "accessModes": ["ReadWriteMany"],
                "storageClassName": "",
                "volumeName": target.pv,
                "resources": {"requests": {"storage": "1Mi"}},
            },
        }
    )


def _create_unbound_claim(
    runner: Any, config: Any, target: KubectlStorageTarget
) -> None:
    """Create the unbound claim through kubectl."""
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as stream:
        stream.write(_unbound_claim_manifest(target))
        stream.flush()
        stream.seek(0)
        runner.run(
            _kubectl(config, "-n", target.namespace, "create", "-f", "-"),
            stdin=stream,
            timeout=60,
        )


def _delete_unbound_claim(
    runner: Any, config: Any, target: KubectlStorageTarget
) -> None:
    """Delete the scenario-owned unbound claim, tolerating its absence."""
    result = runner.run(
        _kubectl(
            config,
            "-n",
            target.namespace,
            "delete",
            "pvc",
            target.pvc,
            "--ignore-not-found",
            "--wait=false",
        ),
        check=False,
        timeout=60,
    )
    if result.returncode:
        LOG.warning(
            "could not delete unbound claim %s/%s: %s",
            target.namespace,
            target.pvc,
            (result.stderr or result.stdout).strip(),
        )


def _assert_no_run_trees(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    targets: tuple[KubectlStorageTarget, ...],
) -> None:
    """Require that no attempt left a run tree on any given claim."""
    runs = f"{_kubectl_remote_control_root(runtime)}/runs"
    command = f'test -z "$(ls -A -- {shlex.quote(runs)} 2>/dev/null)"'
    for_each_target(
        lambda target: _storage_utility_shell(
            runner, config, fixture, command, target=target
        ),
        targets,
    )


def _run_kubectl_multi_target_unbound(
    runner: Any,
    config: Any,
    fixture: Fixture,
    runtime: ScenarioRuntime,
    template: Path,
    report_workspace: Path,
    log_dir: Path,
) -> None:
    """A start with an unbound second claim fails and leaves nothing behind."""
    del report_workspace
    real = _scenario_storage_targets(fixture, runtime)
    unbound = KubectlStorageTarget(real[1].namespace, UNBOUND_PV, UNBOUND_CLAIM)
    step = runtime.scenario.steps[0]
    # The second target file names the unbound claim instead of the real one.
    runtime.values["test_key_ns2"] = unbound.key(
        f"{runtime.data_root}/primary", with_namespace=True
    )
    expected = _expected_groups(fixture, runtime)
    _create_unbound_claim(runner, config, unbound)
    try:
        root, _ = _multi_target_prepare(
            runner, config, fixture, runtime, template, log_dir, expected
        )
        runtime.values["kubectl_result_root"] = root
        runtime.values["kubectl_collected"] = "0"
        start = replace(step, name="start-unbound", arguments=("--start", root))
        _mixed_batch_command(
            runner,
            config,
            fixture,
            runtime,
            start,
            log_dir,
            expected_failure=True,
        )
        _assert_no_owned_kubectl_objects(
            runner, config, tuple(target.namespace for target in real)
        )
        _assert_no_run_trees(runner, config, fixture, runtime, real)
        # The rolled-back start created no attempt, so scenario cleanup has
        # nothing to cancel or collect.
        runtime.values["kubectl_collected"] = "1"
    finally:
        _delete_unbound_claim(runner, config, unbound)


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
    # CI shard manifests compare the planned and passed work items.
    work_items = WorkItemRecorder(log_dir / WORK_ITEMS_FILENAME, work)
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
        release_tree = release_report = None
        if any(
            (step.scenario.name, step.substrate.value) == RELEASE_SOURCE_SCENARIO
            for step in work
        ):
            release_tree = _prepare_release_source_tree(
                runner,
                repo_root,
                build_root,
                log_dir,
                binary,
                binary_name,
                fixture.architecture,
                runtime,
                download=fixture.storage_backend != "sbx-shared",
            )
            # Its reports set up their own .venv, as a release user's would.
            release_report = build_root / "release-report-workspace"
            shutil.copytree(release_tree, release_report)
            _write_runtime_files(
                release_report,
                "ssh",
                str(release_report),
                report_fixture,
                template=release_tree / "env.sh.template",
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
                started = time.monotonic()
                scenario_root = build_root / f"{scenario}-{step.substrate.value}"
                scenario_root.mkdir()
                scenario_logs = log_dir / f"{scenario}-{step.substrate.value}"
                scenario_logs.mkdir()
                specification = SCENARIO_SPECS_BY_NAME[scenario]
                release = (scenario, step.substrate.value) == RELEASE_SOURCE_SCENARIO
                source_tree = release_tree if release else extracted
                reports = release_report if release else report_workspace
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
                        source_tree,
                    )
                    if scenario == "mixed-batch":
                        _run_mixed_batch(
                            runner,
                            config,
                            fixture,
                            runtime_state,
                            source_tree / "env.sh.template",
                            reports,
                            scenario_logs,
                            binary_name,
                            binary,
                            runtime,
                        )
                    elif step.substrate.value == "kubectl":
                        if scenario not in KUBECTL_SUPPORTED_SCENARIOS:
                            raise IntegrationTestError(
                                f"Kubernetes scenario has no explicit adapter: {scenario}"
                            )
                        kubectl_args = (
                            runner,
                            config,
                            fixture,
                            runtime_state,
                            source_tree / "env.sh.template",
                            reports,
                            scenario_logs,
                        )
                        if scenario == "kubectl-multi-target":
                            _run_kubectl_multi_target(*kubectl_args)
                        elif scenario == "kubectl-multi-target-unbound":
                            _run_kubectl_multi_target_unbound(*kubectl_args)
                        elif scenario == "kubectl-same-namespace-targets":
                            _run_kubectl_same_namespace_targets(*kubectl_args)
                        elif scenario in {"failure-resume", "mdtest-sweep"}:
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
                    elif scenario in {"failure-resume", "mdtest-sweep"}:
                        _run_failure_resume(
                            runner,
                            config,
                            fixture,
                            runtime_state,
                            source_tree / "env.sh.template",
                            reports,
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
                            source_tree / "env.sh.template",
                            reports,
                            scenario_logs,
                        )
                    if release:
                        _require_release_reports(
                            scenario_logs,
                            (source_tree / "VERSION")
                            .read_text(encoding="utf-8")
                            .strip(),
                        )
                except Exception as error:
                    primary_error = error
                    work_items.record(step, "failed", started)
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
                work_items.record(step, "passed", started)
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
