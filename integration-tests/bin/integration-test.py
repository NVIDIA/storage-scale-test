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
import pwd
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

INTEGRATION_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(INTEGRATION_LIB))

from filesystem_integration import (  # pylint: disable=wrong-import-position
    IntegrationTestError,
    TEST_SELECTORS,
    run_filesystem_tests,
)

KIND_VERSION = "v0.33.0"
SBX_KIND_VERSION = "v0.30.0"
KUBECTL_VERSION = "v1.37.0"
SBX_KUBECTL_VERSION = "v1.34.0"
HELM_VERSION = "v3.22.0"
KIND_NODE_IMAGE = (
    "kindest/node:v1.37.0@"
    "sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
)
SBX_KIND_NODE_IMAGE = (
    "kindest/node:v1.34.0@"
    "sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a"
)
NFS_CSI_VERSION = "4.13.4"
NFS_CSI_SOURCE_SHA256 = (
    "ded6ffba8b1600d4c723ce1ecb1fd91721ef48e732ce7ca30c0efeeecbb0b900"
)
NFS_CSI_CHART_SHA256 = (
    "815ac441a2dd0e48c82fa92d043e96caac4dd8ac422fbba91ed76892ed32da54"
)
SLINKY_VERSION = "1.2.0"
SLINKY_LOGIN_BASE_IMAGE = "ghcr.io/slinkyproject/login:26.05-ubuntu26.04"
SLINKY_LOGIN_IMAGE = "storage-scale-integration-login:slinky-26.05-file"
SSH_IMAGE = "storage-scale-integration-ssh:ubuntu-24.04"
STATE_SCHEMA = 1
TARGET_LABEL = "storage-scale-test/target=true"
LOGIN_LABEL = "storage-scale-test/login=true"
NFS_UID = 2000
NFS_GID = 2000
NFS_IMAGE_BYTES = 128 * 1024 * 1024
GIB = 1024**3
DEFAULT_STATE_DIR = Path("/var/lib/storage-scale-test-integration")
DEFAULT_EXPORT_DIR = Path("/srv/storage-scale-test-integration")
DEFAULT_SBX_SHARED_ROOT = (
    Path(__file__).resolve().parents[2] / "tmp" / "integration-sbx-shared"
)
NFS_EXPORT_CONFIG = Path("/etc/exports.d/storage-scale-test-integration.exports")
NFS_DAEMON_CONFIG = Path("/etc/nfs.conf.d/storage-scale-test-integration.conf")
EXPORT_MARKER = ".storage-scale-test-integration.json"
STATE_MARKER = "state-owner.json"
UFW_COMMENT = "storage-scale-test integration NFSv4"
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
    storage_backend: str
    sbx_shared_root: Path
    ssh_home_mode: str
    test_user: str
    test_uid: int
    test_gid: int
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

    @property
    def nfs_image(self) -> Path:
        """Return the sparse backing image for the dedicated NFS export."""
        return self.state_dir / "nfs-export.ext4"


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
        cwd: Path | None = None,
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
            cwd=cwd,
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
    create_marker = not config.state_dir.exists()
    command = [
        *_sudo_prefix(),
        "install",
        "-d",
        "-m",
        "0750",
        "-o",
        f"+{config.test_uid}",
        "-g",
        f"+{config.test_gid}",
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
    if create_marker:
        _write_text(
            config.state_dir / STATE_MARKER,
            json.dumps(
                {"schema": STATE_SCHEMA, "cluster_name": config.cluster_name},
                sort_keys=True,
            )
            + "\n",
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


def _check_host_capacity(disk_path: Path) -> None:
    """Fail before provisioning an undersized host."""
    cpu_count = os.cpu_count() or 0
    memory = _meminfo()
    disk = shutil.disk_usage(disk_path)
    failures: list[str] = []
    if cpu_count < 2:
        failures.append(f"need at least 2 CPUs; found {cpu_count}")
    if memory.get("MemTotal", 0) < 8 * GIB:
        failures.append("need at least 8 GiB total memory")
    if memory.get("MemAvailable", 0) < 6 * GIB:
        failures.append("need at least 6 GiB available memory")
    if disk.free < 20 * GIB:
        failures.append(f"need at least 20 GiB free on {disk_path}")
    if failures:
        raise ProvisionError("host capacity check failed: " + "; ".join(failures))
    LOG.info(
        "Host capacity accepted: %s CPUs, %.1f GiB available RAM, %.1f GiB "
        "free on %s",
        cpu_count,
        memory["MemAvailable"] / GIB,
        disk.free / GIB,
        disk_path,
    )


def _nfs_capability_failures() -> list[str]:
    """Return recognized reasons that the full NFS backend cannot run."""
    failures = []
    for path in (Path("/dev/kmsg"), Path("/dev/loop-control")):
        if not path.exists():
            failures.append(f"{path} is absent")
    # exportfs is supplied by nfs-kernel-server, which setup installs after
    # selection; its pre-setup absence is not a host capability failure.
    for command in ("losetup", "mount", "systemctl"):
        if not shutil.which(command):
            failures.append(f"{command} is unavailable")
    return failures


def _storage_backend_document(config: Config, backend: str) -> dict[str, object]:
    """Return the persistent storage-backend selection document."""
    document: dict[str, object] = {
        "schema": STATE_SCHEMA,
        "cluster_name": config.cluster_name,
        "backend": backend,
    }
    if backend == "sbx-shared":
        document["shared_root"] = str(config.sbx_shared_root)
    return document


def _validate_retained_state_summary(config: Config) -> None:
    """Reject immutable option changes after a setup has completed."""
    path = config.state_dir / "state.json"
    if not path.exists():
        return
    state = json.loads(path.read_text(encoding="utf-8"))
    backend = state.get("storage_backend")
    if backend not in {"nfs", "sbx-shared"}:
        raise ProvisionError(f"invalid retained setup state: {path}")
    expected: dict[str, object] = {
        "schema": STATE_SCHEMA,
        "cluster_name": config.cluster_name,
        "namespace": config.namespace,
    }
    if backend == "nfs":
        expected["export_dir"] = str(config.export_dir)
    mismatches = [name for name, value in expected.items() if state.get(name) != value]
    backend_changed = config.storage_backend not in {"auto", backend}
    if mismatches or backend_changed:
        changed = ", ".join(mismatches or ["storage_backend"])
        raise ProvisionError(
            f"retained setup differs in immutable fields ({changed}); "
            "use the original options to teardown first"
        )


def _select_storage_backend(config: Config) -> str:
    """Select once, persist, and validate the integration storage backend."""
    _validate_retained_state_summary(config)
    path = config.state_dir / "storage-backend.json"
    if path.exists():
        document = json.loads(path.read_text(encoding="utf-8"))
        backend = str(document.get("backend", ""))
        expected = _storage_backend_document(config, backend)
        if backend not in {"nfs", "sbx-shared"} or document != expected:
            raise ProvisionError(f"invalid retained storage backend state: {path}")
        if config.storage_backend not in {"auto", backend}:
            raise ProvisionError(
                f"retained setup uses storage backend {backend}; teardown is "
                f"required before selecting {config.storage_backend}"
            )
        return backend

    failures = _nfs_capability_failures()
    backend = config.storage_backend
    if backend == "auto":
        backend = "sbx-shared" if failures else "nfs"
    if backend == "nfs" and failures:
        raise ProvisionError(
            "NFS storage backend requirements are unavailable: " + "; ".join(failures)
        )
    _write_text(
        path,
        json.dumps(_storage_backend_document(config, backend), sort_keys=True) + "\n",
    )
    if backend == "sbx-shared":
        LOG.info(
            "Using the Docker SBX shared-path backend for the required RWX "
            "storage contract"
        )
    else:
        LOG.info("Using full-fidelity NFS CSI storage backend")
    return backend


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


def _ensure_apt_packages(runner: Runner, backend: str) -> None:
    """Install the narrow tested Ubuntu/Debian package set when absent."""
    packages = [
        "ca-certificates",
        "curl",
        "file",
        "jq",
        "openssh-client",
        "openssl",
        "python3-venv",
    ]
    if backend == "nfs":
        packages.extend(("e2fsprogs", "nfs-common", "nfs-kernel-server"))
    elif not shutil.which("kind"):
        packages.append("kind")
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


def _ensure_client_tools(
    runner: Runner, architecture: str, storage_backend: str
) -> None:
    """Install checksum-verified kind, kubectl, and Helm when versions differ."""
    expected = {
        "kind": KIND_VERSION,
        "kubectl": (
            SBX_KUBECTL_VERSION if storage_backend == "sbx-shared" else KUBECTL_VERSION
        ),
        "helm": HELM_VERSION,
    }
    for command, version in expected.items():
        if command == "kind" and storage_backend == "sbx-shared":
            installed = _command_version(runner, command)
            if SBX_KIND_VERSION not in installed:
                raise ProvisionError(
                    "sbx-shared compatibility profile requires kind "
                    f"{SBX_KIND_VERSION}; found {installed.strip() or 'nothing'}"
                )
            LOG.info(
                "Using Docker SBX compatibility profile with %s", installed.strip()
            )
            continue
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


def _render_kind_config(config: Config, backend: str) -> Path:
    """Render the immutable three-node topology."""
    if backend == "sbx-shared":
        kmsg_mount = ""
        if not Path("/dev/kmsg").exists():
            kmsg_mount = "\n".join(
                ("      - hostPath: /dev/null", "        containerPath: /dev/kmsg")
            )
        return _render_resource(
            config,
            "manifests/kind-sbx-shared.yaml.tmpl",
            {
                "CLUSTER_NAME": config.cluster_name,
                "SHARED_ROOT": str(config.sbx_shared_root),
                "KMSG_MOUNT": kmsg_mount,
            },
        )
    return _render_resource(
        config,
        "manifests/kind.yaml.tmpl",
        {"CLUSTER_NAME": config.cluster_name},
    )


def _create_cluster(runner: Runner, config: Config, backend: str) -> None:
    """Create a new owned kind cluster."""
    manifest = _render_kind_config(config, backend)
    node_image = SBX_KIND_NODE_IMAGE if backend == "sbx-shared" else KIND_NODE_IMAGE
    LOG.info("Creating three-node kind cluster %s", config.cluster_name)
    runner.run(
        [
            "kind",
            "create",
            "cluster",
            "--name",
            config.cluster_name,
            "--image",
            node_image,
            "--config",
            manifest,
            "--kubeconfig",
            config.kubeconfig,
            "--wait",
            "180s",
        ],
        timeout=600,
    )


def _validate_sbx_shared_root(config: Config) -> None:
    """Require the Docker SBX root to be a narrow path inside repository tmp."""
    allowed_parent = (_repository_root() / "tmp").resolve()
    root = config.sbx_shared_root
    if root == allowed_parent or allowed_parent not in root.parents:
        raise ProvisionError(
            f"Docker SBX shared root must be below {allowed_parent}: {root}"
        )


def _sbx_shared_marker(config: Config) -> dict[str, object]:
    """Return the exact marker for the repository-backed shared root."""
    return {
        **_owner_document(config),
        "backend": "sbx-shared",
        "shared_root": str(config.sbx_shared_root),
    }


def _prepare_sbx_shared(runner: Runner, config: Config) -> None:
    """Create and prove the repository-backed Docker-shared directory."""
    _validate_sbx_shared_root(config)
    root = config.sbx_shared_root
    marker = root / EXPORT_MARKER
    if root.exists() and not marker.exists() and any(root.iterdir()):
        raise ProvisionError(f"refusing nonempty unowned SBX shared root: {root}")
    root.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        document = json.loads(marker.read_text(encoding="utf-8"))
        if document != _sbx_shared_marker(config):
            raise ProvisionError(f"SBX shared marker does not match: {marker}")
    else:
        _write_text(
            marker,
            json.dumps(_sbx_shared_marker(config), sort_keys=True) + "\n",
        )
    for directory in (root / "storage-test", root / "ssh-home"):
        directory.mkdir(exist_ok=True)
        directory.chmod(0o777)
    token = secrets.token_hex(16)
    source = root / "agent-probe"
    source.write_text(token + "\n", encoding="utf-8")
    try:
        runner.run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                "--mount",
                f"type=bind,src={root},dst=/probe",
                SBX_KIND_NODE_IMAGE,
                "-ec",
                'test "$(cat /probe/agent-probe)" = "$1"; '
                'printf "%s\\n" "$1" >/probe/engine-probe',
                "sbx-shared-probe",
                token,
            ],
            timeout=300,
        )
        if (root / "engine-probe").read_text(encoding="utf-8").strip() != token:
            raise ProvisionError("Docker shared-path probe returned the wrong token")
    finally:
        source.unlink(missing_ok=True)
        (root / "engine-probe").unlink(missing_ok=True)


def _configure_sbx_node_trust(runner: Runner, config: Config) -> None:
    """Install Docker SBX's proxy CA in kind nodes when it is present."""
    source = Path("/usr/local/share/ca-certificates/proxy-ca.crt")
    if not source.is_file():
        LOG.info("Docker SBX proxy CA is absent; leaving kind trust unchanged")
        return
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    destination = "/usr/local/share/ca-certificates/docker-sbx-proxy-ca.crt"
    nodes = _kind_containers(runner, config, running_only=True)
    if len(nodes) != 3:
        raise ProvisionError(
            f"cannot configure Docker SBX trust: expected 3 nodes, found {len(nodes)}"
        )
    for node in nodes:
        current = runner.run(
            ["docker", "exec", node, "sha256sum", destination], check=False
        )
        if current.returncode == 0 and current.stdout.split()[0] == expected:
            continue
        runner.run(["docker", "cp", source, f"{node}:{destination}"])
        runner.run(["docker", "exec", node, "update-ca-certificates"])
        runner.run(["docker", "exec", node, "systemctl", "restart", "containerd"])
        runner.run(["docker", "exec", node, "systemctl", "restart", "kubelet"])
    LOG.info("Configured kind nodes to trust the Docker SBX proxy CA")


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
    marker_path = config.export_dir / EXPORT_MARKER
    expected = json.dumps(
        {"schema": STATE_SCHEMA, "cluster_name": config.cluster_name}, sort_keys=True
    )
    export_exists = (
        runner.run(
            [*_sudo_prefix(), "test", "-e", config.export_dir],
            check=False,
            timeout=30,
        ).returncode
        == 0
    )
    existing = runner.run(
        [*_sudo_prefix(), "cat", marker_path], check=False, timeout=30
    )
    if export_exists and existing.returncode != 0:
        raise ProvisionError(
            f"refusing to modify unowned export directory: {config.export_dir}"
        )
    if not export_exists:
        parent_exists = runner.run(
            [*_sudo_prefix(), "test", "-d", config.export_dir.parent],
            check=False,
            timeout=30,
        )
        if parent_exists.returncode:
            raise ProvisionError(
                "export directory must be a leaf below an existing directory: "
                f"{config.export_dir}"
            )
    if existing.returncode == 0 and existing.stdout.strip() != expected:
        raise ProvisionError(
            f"refusing export with mismatched ownership marker: {marker_path}"
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


def _export_mount_type(runner: Runner, config: Config) -> str:
    """Return the export mount filesystem type, or an empty string."""
    result = runner.run(
        [
            "findmnt",
            "--noheadings",
            "--output",
            "FSTYPE",
            "--mountpoint",
            config.export_dir,
        ],
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _ensure_export_filesystem(runner: Runner, config: Config) -> None:
    """Mount a small persistent filesystem for realistic mount validation."""
    _ensure_export_marker(runner, config)
    mounted_type = _export_mount_type(runner, config)
    if mounted_type:
        if mounted_type != "ext4":
            raise ProvisionError(
                f"refusing non-ext4 mount at dedicated export {config.export_dir}: "
                f"{mounted_type}"
            )
        loops = runner.run(
            [*_sudo_prefix(), "losetup", "--associated", config.nfs_image],
            check=False,
        ).stdout
        if not loops.strip():
            raise ProvisionError(
                f"mounted export {config.export_dir} is not backed by "
                f"{config.nfs_image}"
            )
        return

    if not config.nfs_image.exists():
        LOG.info("Creating sparse %s-byte NFS backing filesystem", NFS_IMAGE_BYTES)
        temporary_image = config.nfs_image.with_suffix(".ext4.new")
        temporary_image.unlink(missing_ok=True)
        runner.run(["truncate", "--size", str(NFS_IMAGE_BYTES), temporary_image])
        runner.run(["/usr/sbin/mkfs.ext4", "-F", "-q", "-m", "0", temporary_image])
        temporary_image.replace(config.nfs_image)
        with tempfile.TemporaryDirectory(dir=config.state_dir) as directory:
            migration_mount = Path(directory)
            runner.run(
                [
                    *_sudo_prefix(),
                    "mount",
                    "-o",
                    "loop",
                    config.nfs_image,
                    migration_mount,
                ]
            )
            try:
                runner.run(
                    [
                        *_sudo_prefix(),
                        "cp",
                        "-a",
                        f"{config.export_dir}/.",
                        f"{migration_mount}/",
                    ]
                )
            finally:
                runner.run([*_sudo_prefix(), "umount", migration_mount], check=False)
    runner.run([*_sudo_prefix(), "exportfs", "-u", config.export_dir], check=False)
    runner.run(
        [*_sudo_prefix(), "mount", "-o", "loop", config.nfs_image, config.export_dir]
    )
    _ensure_export_marker(runner, config)


def _configure_nfs(runner: Runner, config: Config, subnet: str, gateway: str) -> None:
    """Reconcile the narrow NFSv4 export and firewall rule."""
    LOG.info("Configuring NFSv4 export for kind subnet %s", subnet)
    _record_nfs_service_state(runner, config)
    _ensure_export_filesystem(runner, config)
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
            NFS_EXPORT_CONFIG,
        ]
    )
    runner.run(
        [
            *_sudo_prefix(),
            "install",
            "-m",
            "0644",
            nfs_source,
            NFS_DAEMON_CONFIG,
        ]
    )
    _ensure_nfs_firewall(runner, config, subnet)
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


def _ensure_nfs_firewall(runner: Runner, config: Config, subnet: str) -> None:
    """Allow NFS only from kind when UFW is active."""
    state_path = config.state_dir / "ufw-rule.json"
    previous: dict[str, object] = {}
    if state_path.exists():
        previous = json.loads(state_path.read_text(encoding="utf-8"))
    if not shutil.which("ufw"):
        LOG.warning("ufw is absent; verify an equivalent TCP-2049 restriction")
        return
    status = runner.run([*_sudo_prefix(), "ufw", "status"], check=False)
    if not status.stdout.startswith("Status: active"):
        LOG.info("ufw is inactive; exportfs remains restricted to %s", subnet)
        if not state_path.exists():
            _write_text(
                state_path,
                json.dumps({"added_by_harness": False, "subnet": subnet}) + "\n",
            )
        return
    if previous.get("added_by_harness") and previous.get("subnet") != subnet:
        _delete_nfs_firewall_rule(runner, str(previous["subnet"]))
        previous = {}
        status = runner.run([*_sudo_prefix(), "ufw", "status"], check=False)
    rule_exists = any(
        subnet in line and UFW_COMMENT in line for line in status.stdout.splitlines()
    )
    added_by_harness = bool(previous.get("added_by_harness"))
    if rule_exists:
        LOG.info("The dedicated UFW NFS rule is already present")
    else:
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
                UFW_COMMENT,
            ]
        )
        added_by_harness = True
    _write_text(
        state_path,
        json.dumps({"added_by_harness": added_by_harness, "subnet": subnet}) + "\n",
    )


def _delete_nfs_firewall_rule(
    runner: Runner, subnet: str, *, check: bool = False
) -> None:
    """Delete the exact UFW rule installed by this harness."""
    runner.run(
        [
            *_sudo_prefix(),
            "ufw",
            "delete",
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
            UFW_COMMENT,
        ],
        check=check,
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


def _image_id(runner: Runner, image: str) -> str | None:
    """Return a local Docker image ID, or None when its tag is absent."""
    result = runner.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image], check=False
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _record_image_build_start(runner: Runner, config: Config, image: str) -> None:
    """Remember a fixed tag's original owner before the first local build."""
    state_path = config.state_dir / "built-images.json"
    state: dict[str, dict[str, str | None]] = {}
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
    if image not in state:
        state[image] = {"previous_id": _image_id(runner, image), "built_id": None}
        _write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _record_image_build_complete(runner: Runner, config: Config, image: str) -> None:
    """Record the exact image ID produced by a successful local build."""
    state_path = config.state_dir / "built-images.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    built_id = _image_id(runner, image)
    if built_id is None:
        raise ProvisionError(f"Docker build did not produce expected image tag {image}")
    state[image]["built_id"] = built_id
    _write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")


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


def _install_sbx_shared_storage(runner: Runner, config: Config) -> None:
    """Bind the shared kind-node paths to the fixture's stable RWX claims."""
    _ensure_namespace(runner, config)
    storage = _render_resource(
        config,
        "manifests/sbx-storage.yaml.tmpl",
        {"NAMESPACE": config.namespace},
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
            "--timeout=60s",
        ),
        timeout=90,
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
    _record_image_build_start(runner, config, SSH_IMAGE)
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
    _record_image_build_complete(runner, config, SSH_IMAGE)
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
        else "emptyDir:\n            sizeLimit: 64Mi"
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
                "IdentitiesOnly=yes",
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
            "IdentitiesOnly=yes",
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
    token = secrets.token_hex(16)
    _pod_exec(
        runner,
        config,
        names[0],
        "touch /home/tester/.integration-home-probe; "
        f"printf '%s\\n' {shlex.quote(token)} "
        ">/mnt/storage-test/.integration-rwx-probe",
    )
    home_probe = _pod_exec(
        runner,
        config,
        names[1],
        "test -e /home/tester/.integration-home-probe",
        check=False,
    )
    storage_check = (
        f'test "$(cat /mnt/storage-test/.integration-rwx-probe)" = '
        f"{shlex.quote(token)}"
    )
    if _select_storage_backend(config) == "nfs":
        storage_check += (
            " && case $(stat -f -c %T /mnt/storage-test) in "
            "nfs|nfs4) true;; *) false;; esac"
        )
    rwx_probe = _pod_exec(
        runner,
        config,
        names[1],
        storage_check,
        check=False,
    )
    if _select_storage_backend(config) == "sbx-shared":
        host_probe = config.sbx_shared_root / "storage-test" / ".integration-rwx-probe"
        if (
            not host_probe.is_file()
            or host_probe.read_text(encoding="utf-8").strip() != token
        ):
            raise ProvisionError("SBX shared data is not visible from the agent")
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
    _prepare_slinky_login_image(runner, config)
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
    _restart_slinky_login(runner, config)
    _validate_slurm(runner, config)


def _prepare_slinky_login_image(runner: Runner, config: Config) -> None:
    """Build and preload the login image with declared test prerequisites."""
    _record_image_build_start(runner, config, SLINKY_LOGIN_IMAGE)
    runner.run(
        [
            "docker",
            "build",
            "--tag",
            SLINKY_LOGIN_IMAGE,
            "--build-arg",
            f"BASE_IMAGE={SLINKY_LOGIN_BASE_IMAGE}",
            "--file",
            _resource_path("slinky-login-image.Dockerfile"),
            _resource_path("."),
        ],
        timeout=600,
    )
    _record_image_build_complete(runner, config, SLINKY_LOGIN_IMAGE)
    _load_image_into_nodes(
        runner,
        SLINKY_LOGIN_IMAGE,
        [f"{config.cluster_name}-control-plane"],
    )


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
        _install_slinky_release(runner, arguments, release)
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


def _install_slinky_release(
    runner: Runner, arguments: list[str | Path], release: str
) -> None:
    """Install a release, retrying the operator webhook startup race once."""
    for attempt in range(2):
        result = runner.run(arguments, check=False, timeout=600)
        if result.returncode == 0:
            return
        detail = result.stdout + result.stderr
        webhook_race = "failed calling webhook" in detail
        if attempt == 0 and webhook_race:
            LOG.warning(
                "Slinky release %s reached its webhook before it was responsive; "
                "retrying once",
                release,
            )
            time.sleep(10)
            continue
        raise ProvisionError(
            f"Helm failed to install Slinky release {release!r}"
            f"{_failure_detail(result.stdout, result.stderr, False)}"
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
    """Wait for and return the single ready, nonterminating LoginSet pod."""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        pods = [
            pod
            for pod in _pods_with_container(_namespace_pods(runner, config), "login")
            if not pod["metadata"].get("deletionTimestamp")  # type: ignore[index]
            and _pod_is_ready(pod)
        ]
        if len(pods) == 1:
            return str(pods[0]["metadata"]["name"])  # type: ignore[index]
        LOG.info(
            "Waiting for one ready, nonterminating LoginSet pod; found %s", len(pods)
        )
        time.sleep(3)
    raise ProvisionError(
        "LoginSet did not converge to one ready pod within 180 seconds"
    )


def _restart_slinky_login(runner: Runner, config: Config) -> None:
    """Restart the configless login client after accounting is available."""
    runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "rollout",
            "restart",
            "deployment/slurm-login-test",
        )
    )
    runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "rollout",
            "status",
            "deployment/slurm-login-test",
            "--timeout=180s",
        ),
        timeout=210,
    )


def _validate_slurm(runner: Runner, config: Config) -> None:
    """Validate LoginSet placement, storage, two-node fan-out, and accounting."""
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
    backend = _select_storage_backend(config)
    token = secrets.token_hex(16)
    storage_probe = (
        f"printf '%s\\n' {shlex.quote(token)} >/mnt/storage-test/.login-probe"
    )
    if backend == "nfs":
        storage_probe += (
            "; case $(stat -f -c %T /mnt/storage-test) in "
            "nfs|nfs4) true;; *) false;; esac"
        )
    runner.run([*prefix, "bash", "-c", storage_probe])
    if backend == "sbx-shared":
        host_probe = config.sbx_shared_root / "storage-test" / ".login-probe"
        if (
            not host_probe.is_file()
            or host_probe.read_text(encoding="utf-8").strip() != token
        ):
            raise ProvisionError("LoginSet SBX data is not visible from the agent")
    runner.run([*prefix, "rm", "-f", "/mnt/storage-test/.login-probe"])
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


def _write_state_summary(
    config: Config, backend: str, subnet: str = "", gateway: str = ""
) -> None:
    """Persist non-secret desired state for later diagnostics."""
    state = {
        "schema": STATE_SCHEMA,
        "cluster_name": config.cluster_name,
        "namespace": config.namespace,
        "export_dir": str(config.export_dir),
        "ssh_home_mode": config.ssh_home_mode,
        "storage_backend": backend,
        "test_user": config.test_user,
        "test_uid": config.test_uid,
        "test_gid": config.test_gid,
        "kind_version": (SBX_KIND_VERSION if backend == "sbx-shared" else KIND_VERSION),
        "kubectl_version": (
            SBX_KUBECTL_VERSION if backend == "sbx-shared" else KUBECTL_VERSION
        ),
        "kubernetes_version": (
            SBX_KUBECTL_VERSION if backend == "sbx-shared" else KUBECTL_VERSION
        ),
        "nfs_csi_version": NFS_CSI_VERSION if backend == "nfs" else None,
        "slinky_version": SLINKY_VERSION,
        "kind_subnet": subnet,
        "kind_gateway": gateway,
    }
    _write_text(config.state_dir / "state.json", json.dumps(state, indent=2) + "\n")


def setup_environment(runner: Runner, config: Config) -> None:
    """Idempotently provision and validate the complete fixture."""
    architecture = _check_platform()
    backend = _select_storage_backend(config)
    capacity_path = _repository_root() if backend == "sbx-shared" else Path("/")
    _check_host_capacity(capacity_path)
    _ensure_apt_packages(runner, backend)
    _ensure_docker(runner)
    _ensure_client_tools(runner, architecture, backend)
    running_clusters = _kind_clusters(runner)
    containers = _kind_containers(runner, config, running_only=False)
    running_containers = _kind_containers(runner, config, running_only=True)
    cluster_exists = config.cluster_name in running_clusters or bool(containers)
    _ensure_cluster_ownership(config, cluster_exists)
    if backend == "nfs" and cluster_exists and not _export_mount_type(runner, config):
        LOG.warning(
            "Replacing the disposable cluster before initializing the NFS "
            "backing filesystem"
        )
        _delete_cluster(runner, config)
        running_clusters = set()
        running_containers = set()
        cluster_exists = False
    if backend == "nfs":
        _ensure_export_filesystem(runner, config)
    else:
        _prepare_sbx_shared(runner, config)
    if config.cluster_name in running_clusters and len(running_containers) == 3:
        _export_kubeconfig(runner, config)
    elif cluster_exists:
        LOG.warning("Replacing incomplete or stopped disposable kind cluster")
        _delete_cluster(runner, config)
        _create_cluster(runner, config, backend)
    else:
        _create_cluster(runner, config, backend)
    if backend == "sbx-shared":
        _configure_sbx_node_trust(runner, config)
    _wait_for_cluster(runner, config)
    subnet = ""
    gateway = ""
    if backend == "nfs":
        subnet, gateway = _kind_ipv4_network(runner)
        _configure_nfs(runner, config, subnet, gateway)
        _probe_nfs(runner, config, gateway)
        _install_nfs_csi(runner, config, gateway)
    else:
        _install_sbx_shared_storage(runner, config)
    _install_ssh_workers(runner, config)
    _scale_ssh(runner, config, replicas=0)
    _install_slurm(runner, config)
    _scale_ssh(runner, config, replicas=2)
    _wait_for_ssh(runner, config)
    private_key = config.keys_dir / "id_ed25519"
    _validate_ssh_workers(runner, config, private_key)
    _write_state_summary(config, backend, subnet, gateway)
    LOG.info("Integration environment is provisioned and running")


def _grant_test_user_access(runner: Runner, config: Config) -> None:
    """Give the non-root test identity access to its private setup state."""
    marker = config.state_dir / STATE_MARKER
    if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != (
        _owner_document(config)
    ):
        raise ProvisionError(
            f"refusing to change ownership of unverified setup state: {config.state_dir}"
        )
    runner.run(
        [
            *_sudo_prefix(),
            "chown",
            "-R",
            f"{config.test_uid}:{config.test_gid}",
            config.state_dir,
        ]
    )


def _test_user_command(config: Config, *command: str | Path) -> list[str | Path]:
    """Build a command that executes as the configured non-root test user."""
    if os.geteuid() != 0:
        return list(command)
    return ["runuser", "-u", config.test_user, "--", *command]


def _verify_test_user_access(runner: Runner, config: Config) -> None:
    """Verify the test identity can use the provisioned cluster and state."""
    probe = config.state_dir / "test-runs" / ".access-probe"
    runner.run(_test_user_command(config, "mkdir", "-p", probe.parent))
    runner.run(_test_user_command(config, "touch", probe))
    runner.run(_test_user_command(config, "rm", "--", probe))
    runner.run(_test_user_command(config, "docker", "info"), timeout=60)
    runner.run(_test_user_command(config, "kind", "get", "clusters"), timeout=30)
    runner.run(
        _test_user_command(
            config,
            "kubectl",
            "--kubeconfig",
            config.kubeconfig,
            "get",
            "nodes",
        ),
        timeout=30,
    )
    LOG.info("Verified integration test access for non-root user %s", config.test_user)


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
    backend = _select_storage_backend(config)
    if backend == "nfs":
        _stop_owned_nfs(runner, config)
    LOG.info(
        "Integration environment stopped; host packages, caches, keys, and %s "
        "data were preserved",
        "NFS" if backend == "nfs" else "SBX shared",
    )


def teardown_environment(runner: Runner, config: Config) -> None:
    """Stop the fixture and remove all harness-owned data and host config."""
    if _select_storage_backend(config) == "sbx-shared":
        state_owned, setup_owned, shared_owned = _validate_sbx_teardown_ownership(
            runner, config
        )
        stop_environment(runner, config)
        if setup_owned:
            _remove_harness_images(runner, config)
        if shared_owned:
            _remove_sbx_shared(runner, config)
        _remove_owned_directories(
            runner, config, remove_state=state_owned, remove_export=False
        )
        LOG.info(
            "Docker SBX integration environment torn down; installed host "
            "packages and client tools were preserved"
        )
        return
    state_owned, setup_owned, export_owned, nfs_configured = (
        _validate_teardown_ownership(runner, config)
    )
    stop_environment(runner, config)
    if nfs_configured:
        _remove_nfs_configuration(runner, config)
    if export_owned:
        _unmount_export_filesystem(runner, config)
    if setup_owned:
        _remove_harness_images(runner, config)
    _remove_owned_directories(
        runner, config, remove_state=state_owned, remove_export=export_owned
    )
    LOG.info(
        "Integration environment torn down; installed host packages and client "
        "tools were preserved"
    )


def _validate_sbx_teardown_ownership(
    runner: Runner, config: Config
) -> tuple[bool, bool, bool]:
    """Validate owned state and shared paths before Docker SBX cleanup."""
    _validate_cleanup_paths(config)
    _validate_sbx_shared_root(config)
    expected_owner = _owner_document(config)
    state_marker = config.state_dir / STATE_MARKER
    state_owned = state_marker.is_file()
    if (
        state_owned
        and json.loads(state_marker.read_text(encoding="utf-8")) != expected_owner
    ):
        raise ProvisionError(
            f"refusing teardown with mismatched ownership marker: {state_marker}"
        )
    if config.state_dir.exists() and not state_owned:
        raise ProvisionError(
            f"refusing to remove unowned setup state: {config.state_dir}"
        )

    cluster_marker = config.state_dir / "cluster-owner.json"
    setup_owned = cluster_marker.is_file()
    if (
        setup_owned
        and json.loads(cluster_marker.read_text(encoding="utf-8")) != expected_owner
    ):
        raise ProvisionError(
            f"refusing teardown with mismatched ownership marker: {cluster_marker}"
        )

    root = config.sbx_shared_root
    shared_owned = root.exists()
    if shared_owned:
        marker = root / EXPORT_MARKER
        if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != (
            _sbx_shared_marker(config)
        ):
            raise ProvisionError(
                f"refusing teardown of unowned SBX shared root: {root}"
            )
        allowed = {EXPORT_MARKER, "storage-test", "ssh-home"}
        unexpected = sorted(
            path.name for path in root.iterdir() if path.name not in allowed
        )
        if unexpected:
            raise ProvisionError(
                f"refusing unexpected entries in SBX shared root: {unexpected}"
            )
    if setup_owned:
        _validate_image_ownership(runner, config)
    return state_owned, setup_owned, shared_owned


def _remove_sbx_shared(runner: Runner, config: Config) -> None:
    """Remove only the validated marker-owned Docker SBX shared root."""
    root = config.sbx_shared_root
    runner.run(["find", root, "-xdev", "-depth", "-delete"], timeout=120)
    if root.exists():
        raise ProvisionError(f"cleanup did not remove SBX shared root: {root}")


def _owner_document(config: Config) -> dict[str, object]:
    """Return the exact ownership document used by persistent markers."""
    return {"schema": STATE_SCHEMA, "cluster_name": config.cluster_name}


def _read_system_file(runner: Runner, path: Path) -> str | None:
    """Read a root-owned file, returning None when it is absent."""
    result = runner.run([*_sudo_prefix(), "cat", path], check=False, timeout=30)
    return result.stdout if result.returncode == 0 else None


def _validate_teardown_ownership(
    runner: Runner, config: Config
) -> tuple[bool, bool, bool, bool]:
    """Validate every persistent artifact before destructive cleanup."""
    _validate_cleanup_paths(config)
    expected_owner = _owner_document(config)
    state_marker = config.state_dir / STATE_MARKER
    state_owned = False
    if state_marker.exists():
        if json.loads(state_marker.read_text(encoding="utf-8")) != expected_owner:
            raise ProvisionError(
                f"refusing teardown with mismatched ownership marker: {state_marker}"
            )
        state_owned = True
    cluster_marker = config.state_dir / "cluster-owner.json"
    cluster_owned = False
    if cluster_marker.exists():
        if json.loads(cluster_marker.read_text(encoding="utf-8")) != expected_owner:
            raise ProvisionError(
                f"refusing teardown with mismatched ownership marker: {cluster_marker}"
            )
        cluster_owned = True
        state_owned = True
    elif config.state_dir.exists() and not state_owned:
        raise ProvisionError(
            f"refusing to remove unowned setup state: {config.state_dir}"
        )

    export_marker = config.export_dir / EXPORT_MARKER
    marker_text = _read_system_file(runner, export_marker)
    export_owned = False
    if marker_text is not None:
        try:
            marker = json.loads(marker_text)
        except json.JSONDecodeError as error:
            raise ProvisionError(
                f"refusing teardown with invalid export marker: {export_marker}"
            ) from error
        if marker != expected_owner:
            raise ProvisionError(
                f"refusing teardown with mismatched export marker: {export_marker}"
            )
        export_owned = True
    elif (
        runner.run(
            [*_sudo_prefix(), "test", "-d", config.export_dir], check=False
        ).returncode
        == 0
    ):
        contents = runner.run(
            [
                *_sudo_prefix(),
                "find",
                config.export_dir,
                "-mindepth",
                "1",
                "-maxdepth",
                "1",
                "-print",
            ]
        ).stdout.strip()
        if contents:
            raise ProvisionError(
                f"refusing to remove nonempty unowned export: {config.export_dir}"
            )

    export_config = _read_system_file(runner, NFS_EXPORT_CONFIG)
    daemon_config = _read_system_file(runner, NFS_DAEMON_CONFIG)
    _validate_installed_config(
        export_config,
        config.manifests_dir / "storage-scale-test.exports",
        NFS_EXPORT_CONFIG,
    )
    _validate_installed_config(
        daemon_config,
        config.manifests_dir / "storage-scale-test-nfs.conf",
        NFS_DAEMON_CONFIG,
    )
    service_state = config.state_dir / "nfs-service.json"
    nfs_configured = (
        export_config is not None or daemon_config is not None or service_state.exists()
    )
    if (export_owned or nfs_configured) and not cluster_owned:
        raise ProvisionError(
            "refusing to remove NFS artifacts without the matching setup state marker"
        )
    if (export_config is not None or daemon_config is not None) and not export_owned:
        raise ProvisionError(
            "refusing to remove NFS configuration without the matching export marker"
        )
    if _export_mount_type(runner, config):
        if not export_owned:
            raise ProvisionError(
                f"refusing to unmount unowned export directory: {config.export_dir}"
            )
        _verified_export_loop(runner, config)
    _validate_loop_associations(runner, config)
    if cluster_owned:
        _validate_image_ownership(runner, config)

    if nfs_configured:
        unrelated = [
            path for path in _export_paths(runner) if path != str(config.export_dir)
        ]
        if unrelated:
            raise ProvisionError(
                "refusing to stop and disable nfs-server while unrelated exports "
                "exist: " + ", ".join(unrelated)
            )
    return state_owned, cluster_owned, export_owned, nfs_configured


def _validate_cleanup_paths(config: Config) -> None:
    """Reject broad or overlapping lifecycle paths."""
    forbidden = {Path("/"), Path("/var"), Path("/srv"), Path("/etc")}
    if config.state_dir in forbidden or config.export_dir in forbidden:
        raise ProvisionError(
            "refusing lifecycle action with a broad state or export path"
        )
    if config.state_dir == config.export_dir:
        raise ProvisionError("state and export directories must be different")
    if config.state_dir in config.export_dir.parents:
        raise ProvisionError("export directory must not be inside the state directory")
    if config.export_dir in config.state_dir.parents:
        raise ProvisionError("state directory must not be inside the export directory")


def _validate_lifecycle_paths(config: Config) -> None:
    """Validate state ownership before bootstrap can mutate its path."""
    _validate_cleanup_paths(config)
    state_dir = config.state_dir
    if not state_dir.exists():
        if not state_dir.parent.is_dir():
            raise ProvisionError(
                f"state directory must be a leaf below an existing directory: {state_dir}"
            )
        return
    if not state_dir.is_dir():
        raise ProvisionError(f"state path is not a directory: {state_dir}")
    marker = state_dir / STATE_MARKER
    if not marker.is_file():
        raise ProvisionError(f"refusing to modify unowned setup state: {state_dir}")
    try:
        owner = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProvisionError(
            f"invalid setup state ownership marker: {marker}"
        ) from error
    if owner != _owner_document(config):
        raise ProvisionError(f"setup state ownership marker does not match: {marker}")


def _validate_installed_config(
    installed: str | None, source: Path, destination: Path
) -> None:
    """Require a host config file to match its harness-rendered source."""
    if installed is None:
        return
    if not source.exists() or installed != source.read_text(encoding="utf-8"):
        raise ProvisionError(
            f"refusing to remove modified or unowned host configuration: {destination}"
        )


def _export_paths(runner: Runner) -> list[str]:
    """Return currently exported local paths."""
    exports = runner.run([*_sudo_prefix(), "exportfs", "-v"], check=False).stdout
    return [line.split()[0] for line in exports.splitlines() if line.startswith("/")]


def _verified_export_loop(runner: Runner, config: Config) -> str:
    """Return the export loop device after verifying its exact backing image."""
    mounted = runner.run(
        [
            "findmnt",
            "--noheadings",
            "--output",
            "SOURCE,FSTYPE",
            "--mountpoint",
            config.export_dir,
        ]
    ).stdout.split()
    if (
        len(mounted) != 2
        or mounted[1] != "ext4"
        or not mounted[0].startswith("/dev/loop")
    ):
        raise ProvisionError(
            f"refusing unexpected mount at dedicated export {config.export_dir}"
        )
    backing = runner.run(
        [
            *_sudo_prefix(),
            "losetup",
            "--noheadings",
            "--output",
            "BACK-FILE",
            mounted[0],
        ]
    ).stdout.strip()
    if Path(backing).resolve() != config.nfs_image.resolve():
        raise ProvisionError(
            f"refusing loop device {mounted[0]} backed by unexpected file {backing}"
        )
    return mounted[0]


def _associated_loop_devices(runner: Runner, config: Config) -> list[str]:
    """Return loop devices associated with the exact NFS backing image."""
    if not config.nfs_image.exists():
        return []
    result = runner.run(
        [*_sudo_prefix(), "losetup", "--associated", config.nfs_image], check=False
    )
    return [line.split(":", maxsplit=1)[0] for line in result.stdout.splitlines()]


def _validate_loop_associations(runner: Runner, config: Config) -> None:
    """Reject an owned loop device mounted anywhere except the export path."""
    for device in _associated_loop_devices(runner, config):
        mounts = runner.run(
            ["findmnt", "--noheadings", "--output", "TARGET", "--source", device],
            check=False,
        ).stdout.splitlines()
        unexpected = [
            target for target in mounts if Path(target).resolve() != config.export_dir
        ]
        if unexpected:
            raise ProvisionError(
                f"refusing loop device {device} mounted outside the fixture: "
                + ", ".join(unexpected)
            )


def _validate_image_ownership(runner: Runner, config: Config) -> None:
    """Reject fixture tags that no longer identify images built by setup."""
    state_path = config.state_dir / "built-images.json"
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for image, ownership in state.items():
        previous_id = ownership.get("previous_id")
        if previous_id and _image_id(runner, previous_id) != previous_id:
            raise ProvisionError(
                f"cannot restore prior Docker image for fixture tag {image}: "
                f"{previous_id} is absent"
            )
        current = _image_id(runner, image)
        allowed = {previous_id, ownership.get("built_id"), None}
        if current not in allowed:
            raise ProvisionError(
                f"refusing to alter Docker tag changed outside the fixture: {image}"
            )


def _remove_nfs_configuration(runner: Runner, config: Config) -> None:
    """Unexport storage, disable NFS, and remove exact host configuration."""
    LOG.info("Removing the dedicated NFS export and host configuration")
    export_source = config.manifests_dir / "storage-scale-test.exports"
    if export_source.exists():
        client = export_source.read_text(encoding="utf-8").split()[1].split("(", 1)[0]
        runner.run(
            [
                *_sudo_prefix(),
                "exportfs",
                "-u",
                f"{client}:{config.export_dir}",
            ],
            check=False,
        )
    for path in (NFS_EXPORT_CONFIG, NFS_DAEMON_CONFIG):
        runner.run([*_sudo_prefix(), "rm", "--force", "--", path])
    runner.run([*_sudo_prefix(), "exportfs", "-ra"])
    if str(config.export_dir) in _export_paths(runner):
        raise ProvisionError(f"NFS export is still active: {config.export_dir}")
    runner.run([*_sudo_prefix(), "systemctl", "disable", "--now", "nfs-server"])
    firewall_state = config.state_dir / "ufw-rule.json"
    if firewall_state.exists():
        state = json.loads(firewall_state.read_text(encoding="utf-8"))
        if state.get("added_by_harness"):
            _delete_nfs_firewall_rule(runner, str(state["subnet"]), check=True)


def _unmount_export_filesystem(runner: Runner, config: Config) -> None:
    """Unmount and detach only the verified fixture backing filesystem."""
    if _export_mount_type(runner, config):
        _verified_export_loop(runner, config)
        runner.run([*_sudo_prefix(), "umount", config.export_dir])
    _validate_loop_associations(runner, config)
    for device in _associated_loop_devices(runner, config):
        runner.run([*_sudo_prefix(), "losetup", "--detach", device])
    if _export_mount_type(runner, config):
        raise ProvisionError(f"export remains mounted: {config.export_dir}")
    if _associated_loop_devices(runner, config):
        raise ProvisionError(f"loop devices remain attached to {config.nfs_image}")


def _remove_harness_images(runner: Runner, config: Config) -> None:
    """Remove owned image tags or restore the tags that setup replaced."""
    state_path = config.state_dir / "built-images.json"
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for image, ownership in state.items():
        built_id = ownership.get("built_id")
        previous_id = ownership.get("previous_id")
        if built_id is None or _image_id(runner, image) != built_id:
            continue
        if previous_id:
            runner.run(["docker", "image", "tag", previous_id, image])
        else:
            runner.run(["docker", "image", "rm", image])


def _remove_owned_directories(
    runner: Runner,
    config: Config,
    *,
    remove_state: bool,
    remove_export: bool,
) -> None:
    """Remove the validated export and state directories without crossing mounts."""
    paths = [config.state_dir] if remove_state else []
    if remove_export:
        paths.insert(0, config.export_dir)
    for path in paths:
        probe = runner.run([*_sudo_prefix(), "test", "-e", path], check=False)
        if probe.returncode:
            continue
        runner.run(
            [*_sudo_prefix(), "find", path, "-xdev", "-depth", "-delete"],
            timeout=120,
        )
        if (
            runner.run([*_sudo_prefix(), "test", "-e", path], check=False).returncode
            == 0
        ):
            raise ProvisionError(f"cleanup did not remove {path}")


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
    export_paths = _export_paths(runner)
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
        if not shutil.which(str(command[0])):
            LOG.error("diagnostic command is unavailable: %s", command[0])
            continue
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
        "--storage-backend",
        choices=("auto", "nfs", "sbx-shared"),
        default="auto",
    )
    parser.add_argument(
        "--sbx-shared-root",
        type=Path,
        default=DEFAULT_SBX_SHARED_ROOT,
    )
    parser.add_argument(
        "--test-user",
        help=(
            "non-root account that runs tests; required for root setup unless "
            "SUDO_USER identifies it"
        ),
    )
    parser.add_argument(
        "--ssh-home-mode", choices=("separate", "shared"), default="separate"
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "action", choices=("setup", "start", "stop", "teardown", "test")
    )
    parser.add_argument(
        "tests",
        nargs="*",
        metavar="TEST",
        help="test selectors for the test action: " + ", ".join(TEST_SELECTORS),
    )
    return parser


def _config(arguments: argparse.Namespace) -> Config:
    """Convert parsed arguments into immutable configuration."""
    account = _test_account(arguments.test_user)
    return Config(
        cluster_name=arguments.cluster_name,
        namespace=arguments.namespace,
        state_dir=arguments.state_dir.resolve(),
        export_dir=arguments.export_dir.resolve(),
        storage_backend=arguments.storage_backend,
        sbx_shared_root=arguments.sbx_shared_root.resolve(),
        ssh_home_mode=arguments.ssh_home_mode,
        test_user=account.pw_name,
        test_uid=account.pw_uid,
        test_gid=account.pw_gid,
        verbose=arguments.verbose,
    )


def _test_account(explicit_user: str | None) -> pwd.struct_passwd:
    """Resolve the non-root account that owns and runs integration tests."""
    requested = explicit_user
    if os.geteuid() == 0 and requested is None:
        requested = os.environ.get("SUDO_USER")
    if requested is None:
        try:
            requested = pwd.getpwuid(os.getuid()).pw_name
        except KeyError as error:
            raise ProvisionError(
                f"current uid has no password-database entry: {os.getuid()}"
            ) from error
    try:
        account = pwd.getpwnam(requested)
    except KeyError as error:
        raise ProvisionError(
            f"integration test user does not exist: {requested}"
        ) from error
    if account.pw_uid == 0:
        raise ProvisionError(
            "integration tests require a non-root account; use sudo from that "
            "account or pass --test-user"
        )
    if os.geteuid() != 0 and account.pw_uid != os.getuid():
        raise ProvisionError(
            f"non-root caller cannot provision tests for another user: {requested}"
        )
    return account


def main() -> int:
    """Run one integration environment lifecycle action."""
    _require_python()
    arguments = _parser().parse_args()
    try:
        if arguments.action == "test" and os.geteuid() == 0:
            raise ProvisionError(
                "refusing to run integration sweeps as root; rerun the test "
                "action as the account provisioned by setup"
            )
        config = _config(arguments)
        _validate_lifecycle_paths(config)
        if arguments.action != "test" and arguments.tests:
            raise ProvisionError("test selectors are valid only with the test action")
        if arguments.action == "test" and not config.state_dir.is_dir():
            raise ProvisionError(
                f"setup state directory is absent at {config.state_dir}; run setup first"
            )
        _bootstrap_state_dir(config)
        log_path = _configure_logging(config, arguments.action)
        LOG.info("Detailed log: %s", log_path)
        with _acquire_lock(config):
            runner = Runner()
            if arguments.action == "stop":
                stop_environment(runner, config)
            elif arguments.action == "teardown":
                teardown_environment(runner, config)
            elif arguments.action == "test":
                run_filesystem_tests(
                    runner, config, _repository_root(), arguments.tests
                )
            else:
                setup_environment(runner, config)
                _grant_test_user_access(runner, config)
                _verify_test_user_access(runner, config)
        return 0
    except (
        ProvisionError,
        IntegrationTestError,
        OSError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ) as error:
        LOG.error("%s", error)
        if (
            "runner" in locals()
            and "config" in locals()
            and arguments.action not in ("stop", "teardown")
        ):
            _collect_diagnostics(runner, config)
        return 1
    except KeyboardInterrupt:
        LOG.error("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
