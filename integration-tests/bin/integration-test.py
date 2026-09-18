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

"""Provision the single-host storage-scale integration environment."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import platform
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

KIND_VERSION = "v0.33.0"
KUBECTL_VERSION = "v1.37.0"
HELM_VERSION = "v3.22.0"
KIND_NODE_IMAGE = (
    "kindest/node:v1.37.0@"
    "sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
)
NFS_CSI_VERSION = "4.13.4"
NFS_CSI_SOURCE_SHA256 = (
    "ded6ffba8b1600d4c723ce1ecb1fd91721ef48e732ce7ca30c0efeeecbb0b900"
)
NFS_CSI_CHART_SHA256 = (
    "815ac441a2dd0e48c82fa92d043e96caac4dd8ac422fbba91ed76892ed32da54"
)
SLINKY_VERSION = "1.2.0"
SSH_IMAGE = "storage-scale-integration-ssh:ubuntu-24.04"
STATE_SCHEMA = 1
TARGET_LABEL = "storage-scale-test/target=true"
LOGIN_LABEL = "storage-scale-test/login=true"
NFS_UID = 2000
NFS_GID = 2000
GIB = 1024**3
DEFAULT_STATE_DIR = Path("/var/lib/storage-scale-test-integration")
DEFAULT_EXPORT_DIR = Path("/srv/storage-scale-test-integration")
LOG = logging.getLogger("storage-scale-integration")

CSI_IMAGES = (
    ("csi-node-driver-registrar", "v2.17.0"),
    ("csi-provisioner", "v6.3.0"),
    ("csi-resizer", "v2.2.0"),
    ("livenessprobe", "v2.19.0"),
    ("nfsplugin", "v4.13.4"),
)


class ProvisionError(RuntimeError):
    """An actionable provisioning failure."""


@dataclass(frozen=True)
class Config:
    """Resolved integration environment configuration."""

    cluster_name: str
    namespace: str
    state_dir: Path
    export_dir: Path
    ssh_home_mode: str
    verbose: bool

    @property
    def kubeconfig(self) -> Path:
        """Return the private kubeconfig path."""
        return self.state_dir / "kubeconfig"

    @property
    def manifests_dir(self) -> Path:
        """Return the rendered-manifest state directory."""
        return self.state_dir / "manifests"

    @property
    def keys_dir(self) -> Path:
        """Return the persistent test-key directory."""
        return self.state_dir / "keys"


class Runner:
    """Run commands with bounded execution and consistent diagnostics."""

    def run(
        self,
        args: Sequence[str | Path],
        *,
        timeout: int = 300,
        check: bool = True,
        sensitive: bool = False,
        stdin: IO[bytes] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run *args* and return its completed process."""
        command = [str(item) for item in args]
        display = (
            command[0] + " [redacted arguments]" if sensitive else shlex.join(command)
        )
        LOG.debug("Running: %s", display)
        result = subprocess.run(
            command,
            check=False,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=stdin is None,
            timeout=timeout,
        )
        stdout = _output_text(result.stdout)
        stderr = _output_text(result.stderr)
        if stdout and not sensitive:
            LOG.debug("stdout from %s:\n%s", command[0], stdout.rstrip())
        if stderr and not sensitive:
            LOG.debug("stderr from %s:\n%s", command[0], stderr.rstrip())
        if check and result.returncode:
            detail = _failure_detail(stdout, stderr, sensitive)
            raise ProvisionError(
                f"command failed ({result.returncode}): {display}{detail}"
            )
        return result


def _output_text(output: str | bytes | None) -> str:
    """Return subprocess output as text."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    return output


def _failure_detail(stdout: str, stderr: str, sensitive: bool) -> str:
    """Return bounded non-secret command failure output."""
    if sensitive:
        return " (output redacted)"
    detail = stderr.strip() or stdout.strip()
    if not detail:
        return ""
    return f"\n{detail[-8000:]}"


def _repository_root() -> Path:
    """Return the repository root from this script location."""
    return Path(__file__).resolve().parents[2]


def _resource_path(name: str) -> Path:
    """Return a checked-in integration resource path."""
    return _repository_root() / "integration-tests" / name


def _sudo_prefix() -> list[str]:
    """Return a sudo prefix when the caller is not root."""
    return [] if os.geteuid() == 0 else ["sudo"]


def _bootstrap_state_dir(config: Config) -> None:
    """Create the operator-owned state directory before file logging."""
    command = [
        *_sudo_prefix(),
        "install",
        "-d",
        "-m",
        "0750",
        "-o",
        f"+{os.getuid()}",
        "-g",
        f"+{os.getgid()}",
        config.state_dir,
        config.manifests_dir,
        config.keys_dir,
        config.state_dir / "logs",
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        raise ProvisionError(
            f"cannot create state directory {config.state_dir}: {result.stderr.strip()}"
        )


def _configure_logging(config: Config, action: str) -> Path:
    """Configure console and timestamped file logging."""
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log_path = config.state_dir / "logs" / f"{action}-{timestamp}.log"
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if config.verbose else logging.INFO)
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    LOG.handlers.clear()
    LOG.setLevel(logging.DEBUG)
    LOG.addHandler(console)
    LOG.addHandler(file_handler)
    return log_path


def _write_text(path: Path, text: str, mode: int = 0o640) -> None:
    """Atomically write *text* with an explicit file mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    temporary.chmod(mode)
    temporary.replace(path)


def _write_bytes(path: Path, content: bytes, mode: int = 0o640) -> None:
    """Atomically write binary *content* with an explicit file mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.chmod(mode)
    temporary.replace(path)


def _render_resource(config: Config, name: str, replacements: dict[str, str]) -> Path:
    """Render a checked-in template into protected state."""
    source = _resource_path(name)
    text = source.read_text(encoding="utf-8")
    for token, value in replacements.items():
        text = text.replace(f"@@{token}@@", value)
    unresolved = [word for word in text.split() if "@@" in word]
    if unresolved:
        raise ProvisionError(f"unresolved template token in {source}: {unresolved[0]}")
    destination = config.manifests_dir / source.name.removesuffix(".tmpl")
    _write_text(destination, text)
    return destination


def _acquire_lock(config: Config) -> IO[str]:
    """Acquire the exclusive lifecycle lock."""
    lock_path = config.state_dir / "lifecycle.lock"
    handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise ProvisionError(
            f"another integration lifecycle command holds {lock_path}"
        ) from error
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return handle


def _require_python() -> None:
    """Require the repository's supported Python baseline."""
    if sys.version_info < (3, 12):
        raise ProvisionError("integration-test.py requires Python 3.12 or newer")


def _check_host_capacity() -> None:
    """Fail before provisioning an undersized host."""
    cpu_count = os.cpu_count() or 0
    memory = _meminfo()
    disk = shutil.disk_usage("/")
    failures: list[str] = []
    if cpu_count < 2:
        failures.append(f"need at least 2 CPUs; found {cpu_count}")
    if memory.get("MemTotal", 0) < 8 * GIB:
        failures.append("need at least 8 GiB total memory")
    if memory.get("MemAvailable", 0) < 6 * GIB:
        failures.append("need at least 6 GiB available memory")
    if disk.free < 20 * GIB:
        failures.append("need at least 20 GiB free on /")
    if failures:
        raise ProvisionError("host capacity check failed: " + "; ".join(failures))
    LOG.info(
        "Host capacity accepted: %s CPUs, %.1f GiB available RAM, %.1f GiB free disk",
        cpu_count,
        memory["MemAvailable"] / GIB,
        disk.free / GIB,
    )


def _meminfo() -> dict[str, int]:
    """Read selected Linux memory counters in bytes."""
    result: dict[str, int] = {}
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        name, value = line.split(":", maxsplit=1)
        result[name] = int(value.strip().split()[0]) * 1024
    return result


def _check_platform() -> str:
    """Validate Linux and return the download architecture."""
    if sys.platform != "linux":
        raise ProvisionError(
            "the integration environment currently supports Linux only"
        )
    architectures = {"x86_64": "amd64", "aarch64": "arm64"}
    try:
        return architectures[platform.machine()]
    except KeyError as error:
        raise ProvisionError(
            f"unsupported architecture: {platform.machine()}"
        ) from error


def _ensure_apt_packages(runner: Runner) -> None:
    """Install the narrow tested Ubuntu/Debian package set when absent."""
    packages = (
        "ca-certificates",
        "curl",
        "jq",
        "nfs-common",
        "nfs-kernel-server",
        "openssh-client",
        "openssl",
    )
    missing = [
        package
        for package in packages
        if runner.run(
            ["dpkg-query", "-W", "-f=${db:Status-Abbrev}", package], check=False
        ).stdout.strip()
        != "ii"
    ]
    if not missing:
        LOG.info("Required operating-system packages are already installed")
        return
    if not shutil.which("apt-get"):
        raise ProvisionError(f"missing packages and apt-get is unavailable: {missing}")
    LOG.info("Installing required packages: %s", ", ".join(missing))
    runner.run([*_sudo_prefix(), "apt-get", "update"], timeout=600)
    runner.run(
        [
            *_sudo_prefix(),
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            *missing,
        ],
        timeout=600,
    )


def _ensure_docker(runner: Runner) -> None:
    """Require a working rootful Docker daemon."""
    if not shutil.which("docker"):
        raise ProvisionError(
            "Docker is required but absent; install a supported rootful Docker Engine first"
        )
    result = runner.run(
        ["docker", "info", "--format", "{{json .SecurityOptions}}"], timeout=60
    )
    if "rootless" in result.stdout.lower():
        raise ProvisionError(
            "rootless Docker is not supported by this integration fixture"
        )
    LOG.info("Rootful Docker is available")


def _command_version(runner: Runner, command: str) -> str:
    """Return normalized version output, or an empty string if unavailable."""
    if not shutil.which(command):
        return ""
    arguments = {
        "kind": ["kind", "version"],
        "kubectl": ["kubectl", "version", "--client"],
        "helm": ["helm", "version", "--short"],
    }[command]
    return runner.run(arguments, check=False, timeout=30).stdout


def _ensure_client_tools(runner: Runner, architecture: str) -> None:
    """Install checksum-verified kind, kubectl, and Helm when versions differ."""
    expected = {
        "kind": KIND_VERSION,
        "kubectl": KUBECTL_VERSION,
        "helm": HELM_VERSION,
    }
    for command, version in expected.items():
        if version in _command_version(runner, command):
            LOG.info("Using %s %s", command, version)
            continue
        _install_client_tool(runner, command, version, architecture)


def _install_client_tool(
    runner: Runner, command: str, version: str, architecture: str
) -> None:
    """Download, verify, and install one client binary."""
    LOG.info("Installing %s %s", command, version)
    with tempfile.TemporaryDirectory(prefix="storage-scale-tool-") as directory:
        target = Path(directory)
        if command == "kind":
            binary = _download_kind(runner, target, version, architecture)
        elif command == "kubectl":
            binary = _download_kubectl(runner, target, version, architecture)
        else:
            binary = _download_helm(runner, target, version, architecture)
        runner.run(
            [*_sudo_prefix(), "install", "-m", "0755", binary, "/usr/local/bin/"]
        )


def _curl(runner: Runner, url: str, destination: Path) -> None:
    """Download one URL with bounded retries."""
    runner.run(
        [
            "curl",
            "--fail",
            "--location",
            "--retry",
            "3",
            "--retry-all-errors",
            "--output",
            destination,
            url,
        ],
        timeout=300,
    )


def _verify_sha256(path: Path, expected: str) -> None:
    """Verify one downloaded file against an expected SHA-256."""
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected.lower():
        raise ProvisionError(
            f"SHA-256 mismatch for {path.name}: {actual} != {expected}"
        )


def _download_kind(
    runner: Runner, directory: Path, version: str, architecture: str
) -> Path:
    """Download and verify kind."""
    name = f"kind-linux-{architecture}"
    base = f"https://github.com/kubernetes-sigs/kind/releases/download/{version}"
    binary = directory / "kind"
    checksum = directory / "kind.sha256sum"
    _curl(runner, f"{base}/{name}", binary)
    _curl(runner, f"{base}/{name}.sha256sum", checksum)
    _verify_sha256(binary, checksum.read_text(encoding="utf-8").split()[0])
    return binary


def _download_kubectl(
    runner: Runner, directory: Path, version: str, architecture: str
) -> Path:
    """Download and verify kubectl."""
    base = f"https://dl.k8s.io/release/{version}/bin/linux/{architecture}/kubectl"
    binary = directory / "kubectl"
    checksum = directory / "kubectl.sha256"
    _curl(runner, base, binary)
    _curl(runner, f"{base}.sha256", checksum)
    _verify_sha256(binary, checksum.read_text(encoding="utf-8").strip())
    return binary


def _download_helm(
    runner: Runner, directory: Path, version: str, architecture: str
) -> Path:
    """Download and verify Helm 3."""
    archive_name = f"helm-{version}-linux-{architecture}.tar.gz"
    base = f"https://get.helm.sh/{archive_name}"
    archive = directory / archive_name
    checksum = directory / f"{archive_name}.sha256sum"
    _curl(runner, base, archive)
    _curl(runner, f"{base}.sha256sum", checksum)
    _verify_sha256(archive, checksum.read_text(encoding="utf-8").split()[0])
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.getmember(f"linux-{architecture}/helm")
        member.name = "helm"
        tar.extract(member, directory, filter="data")
    return directory / "helm"


def _kubectl(config: Config, *arguments: str | Path) -> list[str | Path]:
    """Build a kubectl command using the private kubeconfig."""
    return ["kubectl", "--kubeconfig", config.kubeconfig, *arguments]


def _kind_clusters(runner: Runner) -> set[str]:
    """Return running kind cluster names."""
    result = runner.run(["kind", "get", "clusters"], check=False, timeout=30)
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and "No kind clusters" not in line
    }


def _kind_containers(runner: Runner, config: Config, running_only: bool) -> list[str]:
    """Return kind node container IDs owned by this cluster."""
    arguments = ["docker", "ps"]
    if not running_only:
        arguments.append("--all")
    arguments.extend(
        [
            "--filter",
            f"label=io.x-k8s.kind.cluster={config.cluster_name}",
            "--format",
            "{{.ID}}",
        ]
    )
    output = runner.run(arguments, timeout=30).stdout
    return [line for line in output.splitlines() if line]


def _render_kind_config(config: Config) -> Path:
    """Render the immutable three-node topology."""
    return _render_resource(
        config,
        "manifests/kind.yaml.tmpl",
        {"CLUSTER_NAME": config.cluster_name},
    )


def _create_cluster(runner: Runner, config: Config) -> None:
    """Create a new owned kind cluster."""
    manifest = _render_kind_config(config)
    LOG.info("Creating three-node kind cluster %s", config.cluster_name)
    runner.run(
        [
            "kind",
            "create",
            "cluster",
            "--name",
            config.cluster_name,
            "--image",
            KIND_NODE_IMAGE,
            "--config",
            manifest,
            "--kubeconfig",
            config.kubeconfig,
            "--wait",
            "180s",
        ],
        timeout=600,
    )


def _ensure_cluster_ownership(config: Config, cluster_exists: bool) -> None:
    """Claim a new cluster name or validate its persistent ownership marker."""
    marker = config.state_dir / "cluster-owner.json"
    expected = {"schema": STATE_SCHEMA, "cluster_name": config.cluster_name}
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8")) != expected:
            raise ProvisionError(f"cluster ownership marker does not match: {marker}")
        return
    if cluster_exists:
        raise ProvisionError(
            f"refusing to adopt existing unowned kind cluster {config.cluster_name}; "
            f"use another --cluster-name or restore {marker}"
        )
    _write_text(marker, json.dumps(expected, sort_keys=True) + "\n")


def _export_kubeconfig(runner: Runner, config: Config) -> None:
    """Refresh the private kubeconfig for an existing cluster."""
    runner.run(
        [
            "kind",
            "export",
            "kubeconfig",
            "--name",
            config.cluster_name,
            "--kubeconfig",
            config.kubeconfig,
        ],
        timeout=60,
    )


def _delete_cluster(runner: Runner, config: Config) -> None:
    """Delete the exact marker-owned disposable kind cluster."""
    LOG.info("Deleting disposable kind cluster %s", config.cluster_name)
    runner.run(
        ["kind", "delete", "cluster", "--name", config.cluster_name], timeout=300
    )
    leftovers = _kind_containers(runner, config, running_only=False)
    if leftovers:
        raise ProvisionError(
            "kind reported successful deletion, but cluster containers remain: "
            + ", ".join(leftovers)
        )


def _wait_for_cluster(runner: Runner, config: Config) -> None:
    """Wait for exactly three Ready nodes and enforce fixture labels."""
    _wait_for_kube_api(runner, config)
    runner.run(
        _kubectl(
            config,
            "wait",
            "--for=condition=Ready",
            "nodes",
            "--all",
            "--timeout=180s",
        ),
        timeout=210,
    )
    runner.run(
        _kubectl(
            config,
            "taint",
            "nodes",
            f"{config.cluster_name}-control-plane",
            "node-role.kubernetes.io/control-plane:NoSchedule-",
        ),
        check=False,
    )
    nodes = json.loads(
        runner.run(_kubectl(config, "get", "nodes", "-o", "json")).stdout
    )
    if len(nodes["items"]) != 3:
        raise ProvisionError(
            f"expected exactly 3 Kubernetes nodes; found {len(nodes['items'])}"
        )
    _validate_node_labels(nodes)


def _wait_for_kube_api(runner: Runner, config: Config) -> None:
    """Wait for the Kubernetes API to become usable."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        probe = runner.run(
            _kubectl(config, "get", "nodes", "--request-timeout=5s"), check=False
        )
        if probe.returncode == 0:
            return
        LOG.info("Waiting for the Kubernetes API")
        time.sleep(3)
    raise ProvisionError("Kubernetes API did not become usable within 90 seconds")


def _validate_node_labels(nodes: dict[str, object]) -> None:
    """Validate two targets and one target-negative login node."""
    target_count = 0
    login_count = 0
    for node in nodes["items"]:  # type: ignore[index]
        labels = node["metadata"]["labels"]  # type: ignore[index]
        target_count += labels.get(TARGET_LABEL.split("=")[0]) == "true"
        login_count += labels.get(LOGIN_LABEL.split("=")[0]) == "true"
    if target_count != 2 or login_count != 1:
        raise ProvisionError(
            f"node label invariant failed: target nodes={target_count}, login nodes={login_count}"
        )


def _kind_ipv4_network(runner: Runner) -> tuple[str, str]:
    """Return the kind Docker network's IPv4 subnet and gateway."""
    data = json.loads(runner.run(["docker", "network", "inspect", "kind"]).stdout)
    for entry in data[0]["IPAM"]["Config"]:
        subnet = entry.get("Subnet", "")
        if "." in subnet:
            return subnet, entry["Gateway"]
    raise ProvisionError("kind Docker network has no IPv4 IPAM entry")


def _ensure_export_marker(runner: Runner, config: Config) -> None:
    """Create or validate ownership of the dedicated host export."""
    marker_path = config.export_dir / ".storage-scale-test-integration.json"
    expected = json.dumps(
        {"schema": STATE_SCHEMA, "cluster_name": config.cluster_name}, sort_keys=True
    )
    existing = runner.run(
        [*_sudo_prefix(), "cat", marker_path], check=False, timeout=30
    )
    if existing.returncode == 0 and existing.stdout.strip() != expected:
        raise ProvisionError(
            f"refusing export with mismatched ownership marker: {marker_path}"
        )
    if existing.returncode != 0:
        probe = runner.run(
            [
                *_sudo_prefix(),
                "find",
                config.export_dir,
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
            ],
            check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            raise ProvisionError(
                f"refusing nonempty unowned export directory: {config.export_dir}"
            )
    runner.run([*_sudo_prefix(), "install", "-d", "-m", "0770", config.export_dir])
    runner.run([*_sudo_prefix(), "chown", f"+{NFS_UID}:+{NFS_GID}", config.export_dir])
    marker_source = config.state_dir / "export-marker.json"
    _write_text(marker_source, expected + "\n")
    runner.run(
        [
            *_sudo_prefix(),
            "install",
            "-m",
            "0644",
            marker_source,
            marker_path,
        ]
    )


def _configure_nfs(runner: Runner, config: Config, subnet: str, gateway: str) -> None:
    """Reconcile the narrow NFSv4 export and firewall rule."""
    LOG.info("Configuring NFSv4 export for kind subnet %s", subnet)
    _record_nfs_service_state(runner, config)
    _ensure_export_marker(runner, config)
    export_line = (
        f"{config.export_dir} {subnet}(rw,sync,no_subtree_check,fsid=0,"
        f"all_squash,anonuid={NFS_UID},anongid={NFS_GID})\n"
    )
    export_source = config.manifests_dir / "storage-scale-test.exports"
    nfs_source = config.manifests_dir / "storage-scale-test-nfs.conf"
    _write_text(export_source, export_line)
    _write_text(nfs_source, "[nfsd]\nvers3 = n\nvers4 = y\nthreads = 2\n")
    runner.run([*_sudo_prefix(), "install", "-d", "/etc/exports.d", "/etc/nfs.conf.d"])
    runner.run(
        [
            *_sudo_prefix(),
            "install",
            "-m",
            "0644",
            export_source,
            "/etc/exports.d/storage-scale-test-integration.exports",
        ]
    )
    runner.run(
        [
            *_sudo_prefix(),
            "install",
            "-m",
            "0644",
            nfs_source,
            "/etc/nfs.conf.d/storage-scale-test-integration.conf",
        ]
    )
    _ensure_nfs_firewall(runner, subnet)
    runner.run([*_sudo_prefix(), "exportfs", "-rav"])
    runner.run([*_sudo_prefix(), "systemctl", "enable", "--now", "nfs-server"])
    state = {"subnet": subnet, "gateway": gateway}
    _write_text(config.state_dir / "network.json", json.dumps(state, indent=2) + "\n")


def _record_nfs_service_state(runner: Runner, config: Config) -> None:
    """Remember whether this harness was responsible for starting NFS."""
    path = config.state_dir / "nfs-service.json"
    if path.exists():
        return
    active = (
        runner.run(
            [*_sudo_prefix(), "systemctl", "is-active", "nfs-server"], check=False
        ).returncode
        == 0
    )
    _write_text(path, json.dumps({"started_by_harness": not active}) + "\n")


def _ensure_nfs_firewall(runner: Runner, subnet: str) -> None:
    """Allow NFS only from kind when UFW is active."""
    if not shutil.which("ufw"):
        LOG.warning("ufw is absent; verify an equivalent TCP-2049 restriction")
        return
    status = runner.run([*_sudo_prefix(), "ufw", "status"], check=False)
    if not status.stdout.startswith("Status: active"):
        LOG.info("ufw is inactive; exportfs remains restricted to %s", subnet)
        return
    runner.run(
        [
            *_sudo_prefix(),
            "ufw",
            "allow",
            "from",
            subnet,
            "to",
            "any",
            "port",
            "2049",
            "proto",
            "tcp",
            "comment",
            "storage-scale-test integration NFSv4",
        ]
    )


def _probe_nfs(runner: Runner, config: Config, gateway: str) -> None:
    """Prove a kind node can mount and write the export."""
    node = f"{config.cluster_name}-control-plane"
    script = (
        "set -eu; mkdir -p /tmp/storage-scale-nfs-probe; "
        f"mount -t nfs4 -o vers=4.1 {gateway}:/ /tmp/storage-scale-nfs-probe; "
        "touch /tmp/storage-scale-nfs-probe/.provisioner-probe; "
        "rm /tmp/storage-scale-nfs-probe/.provisioner-probe; "
        "umount /tmp/storage-scale-nfs-probe"
    )
    runner.run(["docker", "exec", node, "sh", "-c", script], timeout=90)


def _image_exists(runner: Runner, image: str) -> bool:
    """Return whether Docker has *image*."""
    return (
        runner.run(["docker", "image", "inspect", image], check=False).returncode == 0
    )


def _prepare_csi_images(runner: Runner, config: Config) -> None:
    """Pull CSI images with an official staging fallback and load all nodes."""
    LOG.info("Preparing pinned NFS CSI images")
    destinations: list[str] = []
    for name, tag in CSI_IMAGES:
        destination = f"registry.k8s.io/sig-storage/{name}:{tag}"
        destinations.append(destination)
        if not _image_exists(runner, destination):
            pull = runner.run(["docker", "pull", destination], check=False, timeout=180)
            if pull.returncode:
                source = f"gcr.io/k8s-staging-sig-storage/{name}:{tag}"
                LOG.warning(
                    "Production registry failed for %s; using official staging", name
                )
                runner.run(["docker", "pull", source], timeout=300)
                runner.run(["docker", "tag", source, destination])
    nodes = _kind_containers(runner, config, running_only=True)
    if len(nodes) != 3:
        raise ProvisionError(
            f"cannot preload CSI images: expected 3 nodes, found {len(nodes)}"
        )
    for image in destinations:
        _load_image_into_nodes(runner, image, nodes)


def _load_image_into_nodes(runner: Runner, image: str, nodes: list[str]) -> None:
    """Import one amd64/arm64 Docker image into each kind containerd store."""
    with tempfile.TemporaryDirectory(prefix="storage-scale-image-") as directory:
        archive_path = Path(directory) / "image.tar"
        runner.run(["docker", "save", "--output", archive_path, image], timeout=300)
        for node in nodes:
            with archive_path.open("rb") as archive:
                runner.run(
                    [
                        "docker",
                        "exec",
                        "-i",
                        node,
                        "ctr",
                        "-n",
                        "k8s.io",
                        "images",
                        "import",
                        "--snapshotter=overlayfs",
                        "-",
                    ],
                    stdin=archive,
                    timeout=300,
                )


def _install_nfs_csi(runner: Runner, config: Config, gateway: str) -> None:
    """Install NFS CSI and bind the two RWX claims."""
    _prepare_csi_images(runner, config)
    chart = _ensure_nfs_csi_chart(runner, config)
    values = _resource_path("manifests/nfs-csi-values.yaml")
    runner.run(
        [
            "helm",
            "upgrade",
            "--install",
            "csi-driver-nfs",
            chart,
            "--namespace",
            "kube-system",
            "--values",
            values,
            "--wait",
            "--timeout",
            "5m",
            "--kubeconfig",
            config.kubeconfig,
        ],
        timeout=420,
    )
    _ensure_namespace(runner, config)
    storage = _render_resource(
        config,
        "manifests/nfs-storage.yaml.tmpl",
        {"NAMESPACE": config.namespace, "NFS_SERVER": gateway},
    )
    runner.run(_kubectl(config, "apply", "-f", storage))
    runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "wait",
            "--for=jsonpath={.status.phase}=Bound",
            "pvc/storage-test-rwx",
            "pvc/ssh-home-rwx",
            "--timeout=180s",
        ),
        timeout=210,
    )


def _ensure_nfs_csi_chart(runner: Runner, config: Config) -> Path:
    """Cache the pinned NFS CSI chart from a checksum-verified source archive."""
    chart = config.state_dir / "charts" / f"csi-driver-nfs-{NFS_CSI_VERSION}.tgz"
    if chart.exists():
        _verify_sha256(chart, NFS_CSI_CHART_SHA256)
        return chart
    url = (
        "https://codeload.github.com/kubernetes-csi/csi-driver-nfs/tar.gz/"
        f"refs/tags/v{NFS_CSI_VERSION}"
    )
    with tempfile.TemporaryDirectory(prefix="storage-scale-nfs-chart-") as directory:
        source = Path(directory) / "source.tar.gz"
        _curl(runner, url, source)
        _verify_sha256(source, NFS_CSI_SOURCE_SHA256)
        member_name = (
            f"csi-driver-nfs-{NFS_CSI_VERSION}/charts/latest/"
            f"csi-driver-nfs-{NFS_CSI_VERSION}.tgz"
        )
        with tarfile.open(source, "r:gz") as archive:
            member = archive.extractfile(member_name)
            if member is None:
                raise ProvisionError(f"NFS CSI chart is absent from {source.name}")
            content = member.read()
        if hashlib.sha256(content).hexdigest() != NFS_CSI_CHART_SHA256:
            raise ProvisionError("SHA-256 mismatch for embedded NFS CSI chart")
        _write_bytes(chart, content)
    return chart


def _ensure_namespace(runner: Runner, config: Config) -> None:
    """Create the integration namespace when absent."""
    probe = runner.run(
        _kubectl(config, "get", "namespace", config.namespace), check=False
    )
    if probe.returncode:
        runner.run(_kubectl(config, "create", "namespace", config.namespace))


def _ensure_ssh_key(runner: Runner, config: Config) -> tuple[Path, Path]:
    """Create or reuse the dedicated integration-test SSH key."""
    private_key = config.keys_dir / "id_ed25519"
    public_key = config.keys_dir / "id_ed25519.pub"
    if private_key.exists() and public_key.exists():
        return private_key, public_key
    if private_key.exists() or public_key.exists():
        raise ProvisionError(f"incomplete SSH identity in {config.keys_dir}")
    runner.run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "storage-scale-integration",
            "-f",
            private_key,
        ],
        sensitive=True,
    )
    private_key.chmod(0o600)
    public_key.chmod(0o644)
    return private_key, public_key


def _ensure_file_secret(
    runner: Runner,
    config: Config,
    name: str,
    files: dict[str, Path],
) -> None:
    """Create an immutable-input file Secret when absent."""
    probe = runner.run(
        _kubectl(config, "-n", config.namespace, "get", "secret", name), check=False
    )
    if probe.returncode == 0:
        return
    arguments: list[str | Path] = [
        *_kubectl(config, "-n", config.namespace, "create", "secret", "generic", name)
    ]
    arguments.extend(f"--from-file={key}={value}" for key, value in files.items())
    runner.run(arguments, sensitive=True)


def _install_ssh_workers(runner: Runner, config: Config) -> None:
    """Build, deploy, and validate the two SSH workers."""
    private_key, public_key = _ensure_ssh_key(runner, config)
    runner.run(
        [
            "docker",
            "build",
            "--tag",
            SSH_IMAGE,
            "--file",
            _resource_path("ssh-image.Dockerfile"),
            _resource_path("."),
        ],
        timeout=600,
    )
    nodes = _kind_containers(runner, config, running_only=True)
    _load_image_into_nodes(runner, SSH_IMAGE, nodes)
    _ensure_file_secret(
        runner,
        config,
        "storage-ssh-identity",
        {"id_ed25519": private_key, "authorized_keys": public_key},
    )
    home_volume = (
        "persistentVolumeClaim:\n            claimName: ssh-home-rwx"
        if config.ssh_home_mode == "shared"
        else "emptyDir:\n            sizeLimit: 16Mi"
    )
    manifest = _render_resource(
        config,
        "manifests/ssh-workers.yaml.tmpl",
        {"NAMESPACE": config.namespace, "SSH_HOME_VOLUME": home_volume},
    )
    runner.run(
        _kubectl(
            config,
            "apply",
            "--server-side",
            "--force-conflicts",
            "--field-manager=storage-scale-integration",
            "-f",
            manifest,
        )
    )
    _wait_for_ssh(runner, config)
    _validate_ssh_workers(runner, config, private_key)


def _ssh_pods(runner: Runner, config: Config) -> list[dict[str, object]]:
    """Return SSH pod objects in ordinal order."""
    result = runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "get",
            "pods",
            "-l",
            "app.kubernetes.io/name=storage-ssh-worker",
            "-o",
            "json",
        )
    )
    return sorted(
        json.loads(result.stdout)["items"], key=lambda item: item["metadata"]["name"]
    )


def _validate_ssh_workers(runner: Runner, config: Config, private_key: Path) -> None:
    """Validate placement, host SSH, home semantics, and RWX visibility."""
    pods = _ssh_pods(runner, config)
    if len(pods) != 2:
        raise ProvisionError(f"expected 2 SSH pods; found {len(pods)}")
    nodes = {pod["spec"]["nodeName"] for pod in pods}
    if len(nodes) != 2 or f"{config.cluster_name}-control-plane" in nodes:
        raise ProvisionError(f"SSH placement invariant failed: {sorted(nodes)}")
    known_hosts = config.state_dir / "ssh_known_hosts"
    scan_lines: list[str] = []
    addresses: list[str] = []
    for pod in pods:
        address = str(pod["status"]["podIP"])
        addresses.append(address)
        scan = runner.run(["ssh-keyscan", "-T", "10", str(address)], timeout=30)
        scan_lines.extend(scan.stdout.splitlines())
    _write_text(known_hosts, "\n".join(scan_lines) + "\n", mode=0o600)
    _write_text(config.state_dir / "ssh_hosts", "\n".join(addresses) + "\n")
    for address in addresses:
        runner.run(
            [
                "ssh",
                "-i",
                private_key,
                "-o",
                "BatchMode=yes",
                "-o",
                f"UserKnownHostsFile={known_hosts}",
                f"tester@{address}",
                "test $(id -u) -eq 2000 && test $(stat -f -c %T /root) = overlayfs",
            ],
            timeout=30,
        )
    root_probe = runner.run(
        [
            "ssh",
            "-i",
            private_key,
            "-o",
            "BatchMode=yes",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            f"root@{addresses[0]}",
            "true",
        ],
        check=False,
        timeout=30,
    )
    if root_probe.returncode == 0:
        raise ProvisionError("SSH root-login rejection validation failed")
    for pod in pods:
        _pod_exec(
            runner,
            config,
            str(pod["metadata"]["name"]),
            "rm -f /home/tester/.ssh/known_hosts",
        )
    for source, destination in ((0, 1), (1, 0)):
        _pod_exec(
            runner,
            config,
            str(pods[source]["metadata"]["name"]),
            "runuser -u tester -- ssh -o BatchMode=yes "
            "-o StrictHostKeyChecking=accept-new "
            f"tester@{addresses[destination]} true",
        )
    _validate_ssh_storage(runner, config, pods)


def _pod_exec(
    runner: Runner, config: Config, pod: str, script: str, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a bounded shell command in an SSH worker pod."""
    return runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "exec",
            pod,
            "--",
            "bash",
            "-c",
            script,
        ),
        check=check,
        timeout=60,
    )


def _validate_ssh_storage(
    runner: Runner, config: Config, pods: list[dict[str, object]]
) -> None:
    """Validate the selected home mode and shared storage claim."""
    names = [str(pod["metadata"]["name"]) for pod in pods]
    _pod_exec(
        runner,
        config,
        names[0],
        "touch /home/tester/.integration-home-probe /mnt/storage-test/.integration-rwx-probe",
    )
    home_probe = _pod_exec(
        runner,
        config,
        names[1],
        "test -e /home/tester/.integration-home-probe",
        check=False,
    )
    rwx_probe = _pod_exec(
        runner,
        config,
        names[1],
        "test -e /mnt/storage-test/.integration-rwx-probe && "
        "case $(stat -f -c %T /mnt/storage-test) in nfs|nfs4) true;; *) false;; esac",
        check=False,
    )
    expected_home_rc = 0 if config.ssh_home_mode == "shared" else 1
    if home_probe.returncode != expected_home_rc or rwx_probe.returncode:
        raise ProvisionError("SSH home or RWX visibility validation failed")
    cleanup = (
        "rm -f /home/tester/.integration-home-probe "
        "/mnt/storage-test/.integration-rwx-probe"
    )
    _pod_exec(runner, config, names[0], cleanup)


def _load_or_create_db_credentials(config: Config) -> dict[str, str]:
    """Return stable protected MariaDB credentials."""
    path = config.keys_dir / "mariadb.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    credentials = {
        "password": secrets.token_hex(24),
        "root_password": secrets.token_hex(24),
    }
    _write_text(path, json.dumps(credentials) + "\n", mode=0o600)
    return credentials


def _ensure_mariadb_secret(runner: Runner, config: Config) -> None:
    """Create the stable MariaDB Secret when absent."""
    name = "mariadb-password"
    probe = runner.run(
        _kubectl(config, "-n", config.namespace, "get", "secret", name), check=False
    )
    if probe.returncode == 0:
        return
    credentials = _load_or_create_db_credentials(config)
    runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "create",
            "secret",
            "generic",
            name,
            f"--from-literal=password={credentials['password']}",
            f"--from-literal=root-password={credentials['root_password']}",
        ),
        sensitive=True,
    )


def _install_slurm(runner: Runner, config: Config) -> None:
    """Install MariaDB, Slinky, and the two-node Slurm fixture."""
    _ensure_mariadb_secret(runner, config)
    mariadb = _render_resource(
        config,
        "manifests/mariadb-accounting.yaml.tmpl",
        {"NAMESPACE": config.namespace},
    )
    runner.run(_kubectl(config, "apply", "-f", mariadb))
    runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "rollout",
            "status",
            "statefulset/mariadb-accounting",
            "--timeout=240s",
        ),
        timeout=270,
    )
    _helm_slinky(runner, config)
    _wait_for_slurm(runner, config)
    _validate_slurm(runner, config)


def _helm_slinky(runner: Runner, config: Config) -> None:
    """Reconcile the three pinned Slinky releases."""
    releases = (
        (
            "slurm-operator-crds",
            "slurm-operator-crds",
            None,
        ),
        (
            "slurm-operator",
            "slurm-operator",
            _resource_path("manifests/slinky-operator-values.yaml"),
        ),
        (
            "slurm",
            "slurm",
            _resource_path("manifests/slinky-slurm-values.yaml"),
        ),
    )
    for release, chart, values in releases:
        if _helm_release_current(runner, config, release, chart):
            LOG.info("Slinky release %s is already at %s", release, SLINKY_VERSION)
            continue
        arguments: list[str | Path] = [
            "helm",
            "upgrade",
            "--install",
            release,
            f"oci://ghcr.io/slinkyproject/charts/{chart}",
            "--version",
            SLINKY_VERSION,
            "--namespace",
            config.namespace,
            "--kubeconfig",
            config.kubeconfig,
            "--wait",
            "--timeout",
            "8m",
        ]
        if values:
            arguments.extend(["--values", values])
        runner.run(arguments, timeout=600)
        if release == "slurm-operator":
            runner.run(
                _kubectl(
                    config,
                    "-n",
                    config.namespace,
                    "rollout",
                    "restart",
                    "deployment/slurm-operator-webhook",
                )
            )
            runner.run(
                _kubectl(
                    config,
                    "-n",
                    config.namespace,
                    "rollout",
                    "status",
                    "deployment/slurm-operator-webhook",
                    "--timeout=180s",
                ),
                timeout=210,
            )


def _helm_release_current(
    runner: Runner, config: Config, release: str, chart: str
) -> bool:
    """Return whether a healthy release already has the pinned chart version."""
    result = runner.run(
        [
            "helm",
            "list",
            "--namespace",
            config.namespace,
            "--all",
            "--output",
            "json",
            "--kubeconfig",
            config.kubeconfig,
        ],
        check=False,
        timeout=60,
    )
    if result.returncode:
        return False
    expected_chart = f"{chart}-{SLINKY_VERSION}"
    return any(
        item.get("name") == release
        and item.get("chart") == expected_chart
        and item.get("status") == "deployed"
        for item in json.loads(result.stdout)
    )


def _wait_for_slurm(runner: Runner, config: Config) -> None:
    """Wait for Slinky child resources not covered by Helm's wait."""
    expected = {"slurmdbd": 1, "slurmctld": 1, "slurmrestd": 1, "login": 1, "slurmd": 2}
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        pods = _namespace_pods(runner, config)
        ready = {
            container: sum(
                _pod_is_ready(pod) for pod in _pods_with_container(pods, container)
            )
            for container in expected
        }
        if ready == expected:
            return
        LOG.info("Waiting for Slinky child pods: ready=%s expected=%s", ready, expected)
        time.sleep(5)
    raise ProvisionError(f"Slinky child pods did not become ready: {ready}")


def _namespace_pods(runner: Runner, config: Config) -> list[dict[str, object]]:
    """Return all pod objects in the integration namespace."""
    result = runner.run(
        _kubectl(config, "-n", config.namespace, "get", "pods", "-o", "json")
    )
    return json.loads(result.stdout)["items"]


def _pods_with_container(
    pods: list[dict[str, object]], container: str
) -> list[dict[str, object]]:
    """Select pods containing a named Slinky workload container."""
    return [
        pod
        for pod in pods
        if container
        in {entry["name"] for entry in pod["spec"]["containers"]}  # type: ignore[index]
    ]


def _pod_is_ready(pod: dict[str, object]) -> bool:
    """Return whether a pod is running with all containers ready."""
    status = pod["status"]  # type: ignore[index]
    containers = status.get("containerStatuses", [])
    return (
        status.get("phase") == "Running"
        and bool(containers)
        and all(container.get("ready", False) for container in containers)
    )


def _login_pod(runner: Runner, config: Config) -> str:
    """Return the single Slinky LoginSet pod name."""
    pods = _pods_with_container(_namespace_pods(runner, config), "login")
    if len(pods) != 1:
        raise ProvisionError(f"expected one LoginSet pod; found {len(pods)}")
    return str(pods[0]["metadata"]["name"])  # type: ignore[index]


def _validate_slurm(runner: Runner, config: Config) -> None:
    """Validate LoginSet placement, NFS, two-node fan-out, and accounting."""
    login = _login_pod(runner, config)
    login_node = runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "get",
            "pod",
            login,
            "-o",
            "jsonpath={.spec.nodeName}",
        )
    ).stdout.strip()
    expected_login_node = f"{config.cluster_name}-control-plane"
    if login_node != expected_login_node:
        raise ProvisionError(
            f"LoginSet placement failed: {login} is on {login_node}, "
            f"expected {expected_login_node}"
        )
    pods = _namespace_pods(runner, config)
    workers = _pods_with_container(pods, "slurmd")
    worker_nodes = {str(pod["spec"]["nodeName"]) for pod in workers}  # type: ignore[index]
    target_result = runner.run(
        _kubectl(
            config,
            "get",
            "nodes",
            "-l",
            TARGET_LABEL,
            "-o",
            "jsonpath={.items[*].metadata.name}",
        )
    )
    target_nodes = set(target_result.stdout.split())
    if worker_nodes != target_nodes or len(worker_nodes) != 2:
        raise ProvisionError(
            f"Slurm worker placement failed: pods={sorted(worker_nodes)}, "
            f"targets={sorted(target_nodes)}"
        )
    prefix = _kubectl(config, "-n", config.namespace, "exec", login, "--")
    runner.run(
        [
            *prefix,
            "bash",
            "-c",
            "case $(stat -f -c %T /mnt/storage-test) in "
            "nfs|nfs4) true;; *) false;; esac",
        ]
    )
    fanout = runner.run(
        [
            *prefix,
            "srun",
            "-p",
            "all",
            "-N2",
            "-n2",
            "--ntasks-per-node=1",
            "hostname",
        ],
        timeout=120,
    ).stdout.splitlines()
    if len(set(fanout)) != 2:
        raise ProvisionError(
            f"Slurm fan-out did not reach two distinct nodes: {fanout}"
        )
    job = runner.run(
        [
            *prefix,
            "sbatch",
            "--wait",
            "--parsable",
            "-p",
            "all",
            "-N2",
            "-n2",
            "--ntasks-per-node=1",
            "--output=/mnt/storage-test/integration-accounting-%j.out",
            "--wrap=srun hostname",
        ],
        timeout=180,
    ).stdout.strip()
    accounting = runner.run(
        [
            *prefix,
            "sacct",
            "-X",
            "-j",
            job,
            "--format=State,ExitCode",
            "-n",
            "-P",
        ]
    ).stdout
    if "COMPLETED|0:0" not in accounting:
        raise ProvisionError(
            f"Slurm accounting validation failed for job {job}: {accounting}"
        )


def _write_state_summary(config: Config, subnet: str, gateway: str) -> None:
    """Persist non-secret desired state for later diagnostics."""
    state = {
        "schema": STATE_SCHEMA,
        "cluster_name": config.cluster_name,
        "namespace": config.namespace,
        "export_dir": str(config.export_dir),
        "ssh_home_mode": config.ssh_home_mode,
        "kind_version": KIND_VERSION,
        "kubernetes_version": KUBECTL_VERSION,
        "nfs_csi_version": NFS_CSI_VERSION,
        "slinky_version": SLINKY_VERSION,
        "kind_subnet": subnet,
        "kind_gateway": gateway,
    }
    _write_text(config.state_dir / "state.json", json.dumps(state, indent=2) + "\n")


def setup_environment(runner: Runner, config: Config) -> None:
    """Idempotently provision and validate the complete fixture."""
    architecture = _check_platform()
    _check_host_capacity()
    _ensure_apt_packages(runner)
    _ensure_docker(runner)
    _ensure_client_tools(runner, architecture)
    running_clusters = _kind_clusters(runner)
    containers = _kind_containers(runner, config, running_only=False)
    running_containers = _kind_containers(runner, config, running_only=True)
    cluster_exists = config.cluster_name in running_clusters or bool(containers)
    _ensure_cluster_ownership(config, cluster_exists)
    if config.cluster_name in running_clusters and len(running_containers) == 3:
        _export_kubeconfig(runner, config)
    elif cluster_exists:
        LOG.warning("Replacing incomplete or stopped disposable kind cluster")
        _delete_cluster(runner, config)
        _create_cluster(runner, config)
    else:
        _create_cluster(runner, config)
    _wait_for_cluster(runner, config)
    subnet, gateway = _kind_ipv4_network(runner)
    _configure_nfs(runner, config, subnet, gateway)
    _probe_nfs(runner, config, gateway)
    _install_nfs_csi(runner, config, gateway)
    _install_ssh_workers(runner, config)
    _scale_ssh(runner, config, replicas=0)
    _install_slurm(runner, config)
    _scale_ssh(runner, config, replicas=2)
    _wait_for_ssh(runner, config)
    private_key = config.keys_dir / "id_ed25519"
    _validate_ssh_workers(runner, config, private_key)
    _write_state_summary(config, subnet, gateway)
    LOG.info("Integration environment is provisioned and running")


def _scale_ssh(runner: Runner, config: Config, replicas: int) -> None:
    """Scale SSH workers to conserve host capacity between checks."""
    runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "scale",
            "statefulset/ssh-worker",
            f"--replicas={replicas}",
        )
    )


def _wait_for_ssh(runner: Runner, config: Config) -> None:
    """Wait for both SSH workers after restoring the running fixture."""
    runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "rollout",
            "status",
            "statefulset/ssh-worker",
            "--timeout=180s",
        ),
        timeout=210,
    )


def stop_environment(runner: Runner, config: Config) -> None:
    """Delete the disposable cluster and stop owned host services."""
    clusters = _kind_clusters(runner)
    containers = _kind_containers(runner, config, running_only=False)
    cluster_exists = config.cluster_name in clusters or bool(containers)
    if cluster_exists:
        _ensure_cluster_ownership(config, cluster_exists=True)
        _delete_cluster(runner, config)
    else:
        LOG.info("Disposable kind cluster %s is already absent", config.cluster_name)
    _stop_owned_nfs(runner, config)
    LOG.info(
        "Integration environment stopped; host packages, caches, keys, and NFS data "
        "were preserved"
    )


def _stop_owned_nfs(runner: Runner, config: Config) -> None:
    """Stop NFS only when this harness started the otherwise-dedicated service."""
    state_path = config.state_dir / "nfs-service.json"
    if not state_path.exists():
        LOG.info("NFS ownership state is absent; leaving nfs-server unchanged")
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not state.get("started_by_harness", False):
        LOG.info("nfs-server predated this fixture; leaving it running")
        return
    exports = runner.run([*_sudo_prefix(), "exportfs", "-v"], check=False).stdout
    export_paths = [
        line.split()[0] for line in exports.splitlines() if line.startswith("/")
    ]
    unrelated = [path for path in export_paths if path != str(config.export_dir)]
    if unrelated:
        LOG.warning(
            "Leaving nfs-server running because unrelated exports exist: %s", unrelated
        )
        return
    service = runner.run(
        [*_sudo_prefix(), "systemctl", "is-active", "nfs-server"], check=False
    )
    if service.returncode == 0:
        runner.run([*_sudo_prefix(), "systemctl", "stop", "nfs-server"])
        LOG.info("Stopped harness-owned nfs-server")
    else:
        LOG.info("nfs-server is already stopped")


def _collect_diagnostics(runner: Runner, config: Config) -> None:
    """Collect bounded troubleshooting state after a setup failure."""
    LOG.error("Collecting troubleshooting diagnostics")
    commands: tuple[Sequence[str | Path], ...] = (
        ("free", "-h"),
        ("df", "-h", "/"),
        ("docker", "ps", "--all"),
        ("kind", "get", "clusters"),
        (*_sudo_prefix(), "systemctl", "status", "nfs-server", "--no-pager"),
        (*_sudo_prefix(), "exportfs", "-v"),
    )
    for command in commands:
        result = runner.run(command, check=False, timeout=30)
        output = (result.stdout + result.stderr).strip()
        if output:
            LOG.error("diagnostic %s:\n%s", command[0], output[-12000:])
    if config.kubeconfig.exists():
        for arguments in (
            ("get", "nodes", "-o", "wide"),
            ("get", "pods", "--all-namespaces", "-o", "wide"),
            ("get", "events", "--all-namespaces", "--sort-by=.lastTimestamp"),
        ):
            result = runner.run(_kubectl(config, *arguments), check=False, timeout=30)
            output = (result.stdout + result.stderr).strip()
            if output:
                LOG.error("kubectl diagnostic:\n%s", output[-16000:])


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-name", default="storage-scale-integration")
    parser.add_argument("--namespace", default="storage-scale-integration")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--export-dir", type=Path, default=DEFAULT_EXPORT_DIR)
    parser.add_argument(
        "--ssh-home-mode", choices=("separate", "shared"), default="separate"
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("action", choices=("setup", "start", "stop"))
    return parser


def _config(arguments: argparse.Namespace) -> Config:
    """Convert parsed arguments into immutable configuration."""
    return Config(
        cluster_name=arguments.cluster_name,
        namespace=arguments.namespace,
        state_dir=arguments.state_dir.resolve(),
        export_dir=arguments.export_dir.resolve(),
        ssh_home_mode=arguments.ssh_home_mode,
        verbose=arguments.verbose,
    )


def main() -> int:
    """Run one integration environment lifecycle action."""
    _require_python()
    arguments = _parser().parse_args()
    config = _config(arguments)
    try:
        _bootstrap_state_dir(config)
        log_path = _configure_logging(config, arguments.action)
        LOG.info("Detailed log: %s", log_path)
        with _acquire_lock(config):
            runner = Runner()
            if arguments.action == "stop":
                stop_environment(runner, config)
            else:
                setup_environment(runner, config)
        return 0
    except (
        ProvisionError,
        OSError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as error:
        LOG.error("%s", error)
        if "runner" in locals() and arguments.action != "stop":
            _collect_diagnostics(runner, config)
        return 1
    except KeyboardInterrupt:
        LOG.error("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
