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
import ipaddress
import json
import logging
import os
import platform
import pwd
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

INTEGRATION_LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(INTEGRATION_LIB))

from filesystem_integration import (  # pylint: disable=wrong-import-position
    IntegrationTestError,
    run_filesystem_tests,
)
from fixture_capacity import (  # pylint: disable=wrong-import-position
    GIB,
    NFS_BUDGET_BYTES,
    NFS_IMAGE_BYTES,
    SSH_HOME_CAPACITY,
    STORAGE_TEST_CAPACITY,
)
from fixture_images import (  # pylint: disable=wrong-import-position
    ELBENCHO_FIXTURE,
    ELBENCHO_FIXTURE_IMAGE,
)
from scenario_planner import (  # pylint: disable=wrong-import-position
    SUBSTRATES,
    format_scenario_listing,
)
from ssh_home_transition import (  # pylint: disable=wrong-import-position
    CANONICAL_HOME_MODE,
    SHARED_HOME_MODE,
    PoolObservation,
    SshHomeTransitionError,
    SshHomeTransitionManager,
    statefulset_checksum,
)

KIND_VERSION = "v0.33.0"
SBX_KIND_VERSION = "v0.30.0"
KUBERNETES_VERSION = "v1.37.0"
SBX_KUBERNETES_VERSION = "v1.34.0"
KUBECTL_VERSION = "v1.37.0"
SBX_KUBECTL_VERSION = "v1.34.0"
HELM_VERSION = "v3.22.0"
KIND_NODE_IMAGE = (
    f"kindest/node:{KUBERNETES_VERSION}@"
    "sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
)
SBX_KIND_NODE_IMAGE = (
    f"kindest/node:{SBX_KUBERNETES_VERSION}@"
    "sha256:7416a61b42b1662ca6ca89f02028ac133a309a2a30ba309614e8ec94d976dc5a"
)
CALICO_VERSION = "v3.32.2"
CALICO_MANIFEST_SHA256 = (
    "a8c828a06a87c629a282ebbc424895b77f3a030251993e41ea400a743675bb02"
)
CALICO_MANIFEST_URL = (
    f"https://raw.githubusercontent.com/projectcalico/calico/{CALICO_VERSION}/"
    "manifests/calico.yaml"
)
CALICO_POD_SUBNET = "192.168.0.0/16"
CALICO_SBX_RECIPE = "iptables-without-securityfs-v2"
CALICO_IMAGES = (
    (
        "cni",
        "sha256:0ef740bc587f25565905adf1d1f61a7faff0d571c449c6bdd789feed743d3ef7",
    ),
    (
        "kube-controllers",
        "sha256:7870b67ebb13fabc3005252b44fe6e78b21635649bd3072b80afa1684b6565d0",
    ),
    (
        "node",
        "sha256:99b03fe91e8bfbcb153ae65ef4b701b24ce541ffdd74ff314eb041096008f7fd",
    ),
)
KINDNET_IMAGE = "docker.io/kindest/kindnetd:v20260820-69b56db7"
KUBECTL_PROBE_NAME = "storage-scale-kubectl-probe"
KUBECTL_PROBE_WORKERS = f"{KUBECTL_PROBE_NAME}-workers"
KUBECTL_PROBE_COORDINATOR = f"{KUBECTL_PROBE_NAME}-coordinator"
KUBECTL_PROBE_DENIED = f"{KUBECTL_PROBE_NAME}-denied"
KUBECTL_PROBE_STORAGE = "/mnt/storage-scale-test/.kubectl-prerequisite-probe"
NFS_CSI_VERSION = "4.13.4"
NFS_CSI_SOURCE_SHA256 = (
    "ded6ffba8b1600d4c723ce1ecb1fd91721ef48e732ce7ca30c0efeeecbb0b900"
)
NFS_CSI_CHART_SHA256 = (
    "815ac441a2dd0e48c82fa92d043e96caac4dd8ac422fbba91ed76892ed32da54"
)
SLINKY_VERSION = "1.2.0"
SLINKY_LOGIN_BASE_IMAGE = (
    "ghcr.io/slinkyproject/login:26.05-ubuntu26.04@"
    "sha256:9578f6773891a9eee75395c60c7f5acad1385ceb6e715c8c76f606ccfd89968d"
)
SLINKY_LOGIN_IMAGE = "storage-scale-integration-login:slinky-26.05-user"
SLINKY_SLURMD_BASE_IMAGE = (
    "ghcr.io/slinkyproject/slurmd:26.05-ubuntu26.04@"
    "sha256:f06ab7b1ce18b3d59c54c8c6f6b03698a28973d4d8fb30985bd0dfd673bd6dd9"
)
SLINKY_SLURMD_IMAGE = "storage-scale-integration-slurmd:slinky-26.05-user"
MARIADB_BASE_IMAGE = (
    "mariadb:11.4@"
    "sha256:70cc072b29b4a89ae07abb2d4da2c64678a7f2dfe092751bb51c87d67dc1338b"
)
MARIADB_IMAGE = "storage-scale-integration-mariadb:11.4"
SLINKY_HELPER_BASE_IMAGE = (
    "alpine:3.22@"
    "sha256:5291449c3df73caf6ed85e649dec1b9e818b39a5d8c871e97afc13e9cd5e8fa8"
)
SLINKY_HELPER_IMAGE_REPOSITORY = "storage-scale-integration-alpine"
SLINKY_HELPER_IMAGE_TAG = "3.22"
SLINKY_HELPER_IMAGE = f"{SLINKY_HELPER_IMAGE_REPOSITORY}:{SLINKY_HELPER_IMAGE_TAG}"
SSH_IMAGE = "storage-scale-integration-ssh:ubuntu-24.04"
SSH_BASE_IMAGE = (
    "ubuntu:24.04@"
    "sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3"
)
STATE_SCHEMA = 1
TARGET_LABEL = "storage-scale-test/target=true"
LOGIN_LABEL = "storage-scale-test/login=true"
WORKLOAD_USER = "tester"
WORKLOAD_UID = 2000
WORKLOAD_GID = 2000
WORKLOAD_ACCOUNT = "storage-test"
NFS_UID = WORKLOAD_UID
NFS_GID = WORKLOAD_GID
NFS_SERVER_THREADS = 8
NFS_THREADS_PATH = Path("/proc/fs/nfsd/threads")
NFS_SERVICE_STATE_SCHEMA = 2
STORAGE_BACKENDS = ("nfs", "sbx-shared")
SYSTEM_ADMIN_PATHS = ("/usr/local/sbin", "/usr/sbin", "/sbin")
SBX_SHARED_DIRECTORY_MODE = 0o777
SSH_STORAGE_VISIBILITY_TIMEOUT_SECONDS = 30
TEMPORARY_UNMOUNT_ATTEMPTS = 10
TEMPORARY_UNMOUNT_RETRY_SECONDS = 1
IMAGE_PULL_ATTEMPTS = 4
IMAGE_PULL_TIMEOUT_SECONDS = 60
IMAGE_PULL_DEADLINE_SECONDS = 240
IMAGE_PULL_INITIAL_BACKOFF_SECONDS = 10
IMAGE_PULL_JITTER_MILLISECONDS = 5000
IMAGE_INSPECT_TIMEOUT_SECONDS = 30
DEFAULT_STATE_DIR = Path(__file__).resolve().parents[2] / "tmp" / "integration-state"
DEFAULT_EXPORT_DIR = Path("/srv/storage-scale-test-integration")
DEFAULT_SBX_SHARED_ROOT = (
    Path(__file__).resolve().parents[2] / "tmp" / "integration-sbx-shared"
)
NFS_EXPORT_CONFIG = Path("/etc/exports.d/storage-scale-test-integration.exports")
NFS_DAEMON_CONFIG = Path("/etc/nfs.conf.d/storage-scale-test-integration.conf")
NFS_HOST_LOCK_DIR = Path("/run/lock/storage-scale-test-integration")
NFS_HOST_LOCK = NFS_HOST_LOCK_DIR / "nfs.lock"
NFS_HOST_OWNER = Path("/var/lib/storage-scale-test-integration-owner.json")
EXPORT_MARKER = ".storage-scale-test-integration.json"
STATE_MARKER = "state-owner.json"
SSH_HOME_ANNOTATION = "storage-scale-test/ssh-home-mode"
SSH_CONFIG_ANNOTATION = "storage-scale-test/ssh-config-checksum"
UFW_COMMENT = "storage-scale-test integration NFSv4"
LOG = logging.getLogger("storage-scale-integration")

# Digests identify upstream multi-architecture indexes. Docker selects and the
# harness separately verifies the runner's amd64 or arm64 platform image.
CSI_IMAGES = (
    (
        "csi-node-driver-registrar",
        "v2.17.0",
        "sha256:f9de845b170155199f2a2a3f9531cf13d78e31235e9db6b6582a8b0db0a50dad",
    ),
    (
        "csi-provisioner",
        "v6.3.0",
        "sha256:a4b0b1a37605b7b04a293e136edf7006ec1786a8eb3f4e5a945f81d667dcc371",
    ),
    (
        "csi-resizer",
        "v2.2.0",
        "sha256:a2d40c1c3ccb0c48b467125a6652c4dd5dcbf0d295641c9989581cfc690f6cf3",
    ),
    (
        "livenessprobe",
        "v2.19.0",
        "sha256:06da0d5b8908072f2e4522692aee8dc119fba7247a9658497e1153992cd777e9",
    ),
    (
        "nfsplugin",
        "v4.13.4",
        "sha256:1eb5a85180a4ad0193a31d319b163f35c8c1857794ebaac71d8abcdd5a0516d3",
    ),
)
CSI_REPOSITORIES = (
    "registry.k8s.io/sig-storage",
    "gcr.io/k8s-staging-sig-storage",
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
        timeout: float = 300,
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
    """Create user-owned state before file logging or privileged work."""
    create_marker = not config.state_dir.exists()
    config.state_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    config.state_dir.chmod(0o750)
    for directory in (
        config.manifests_dir,
        config.keys_dir,
        config.state_dir / "logs",
        config.state_dir / "bin",
        config.state_dir / "tool-state" / "helm" / "cache",
        config.state_dir / "tool-state" / "helm" / "config",
        config.state_dir / "tool-state" / "helm" / "data",
    ):
        directory.mkdir(mode=0o750, parents=True, exist_ok=True)
        directory.chmod(0o750)
    if create_marker:
        _write_text(
            config.state_dir / STATE_MARKER,
            json.dumps(
                {"schema": STATE_SCHEMA, "cluster_name": config.cluster_name},
                sort_keys=True,
            )
            + "\n",
        )


def _configure_user_tool_path(config: Config) -> None:
    """Confine installed clients and their mutable state to user-owned paths."""
    tool_dir = str(config.state_dir / "bin")
    current = os.environ.get("PATH", "")
    entries = [entry for entry in current.split(os.pathsep) if entry]
    entries = [entry for entry in entries if entry != tool_dir]
    entries.insert(0, tool_dir)
    entries.extend(path for path in SYSTEM_ADMIN_PATHS if path not in entries)
    os.environ["PATH"] = os.pathsep.join(entries)
    helm_root = config.state_dir / "tool-state" / "helm"
    os.environ.update(
        {
            "HELM_CACHE_HOME": str(helm_root / "cache"),
            "HELM_CONFIG_HOME": str(helm_root / "config"),
            "HELM_DATA_HOME": str(helm_root / "data"),
        }
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


def _render_resource_text(name: str, replacements: dict[str, str]) -> str:
    """Render and validate one checked-in template as text."""
    source = _resource_path(name)
    text = source.read_text(encoding="utf-8")
    for token, value in replacements.items():
        text = text.replace(f"@@{token}@@", value)
    unresolved = [word for word in text.split() if "@@" in word]
    if unresolved:
        raise ProvisionError(f"unresolved template token in {source}: {unresolved[0]}")
    return text


def _render_resource(config: Config, name: str, replacements: dict[str, str]) -> Path:
    """Render a checked-in template into protected state."""
    text = _render_resource_text(name, replacements)
    source = _resource_path(name)
    destination = config.manifests_dir / source.name.removesuffix(".tmpl")
    _write_text(destination, text)
    return destination


def _acquire_lock(config: Config) -> IO[str]:
    """Acquire the exclusive lifecycle lock."""
    lock_path = _lifecycle_lock_path(config)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise ProvisionError(
            f"cannot safely open lifecycle lock {lock_path}"
        ) from error
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
        os.close(descriptor)
        raise ProvisionError(f"refusing unsafe lifecycle lock {lock_path}")
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        handle.close()
        raise ProvisionError(
            f"another integration lifecycle command holds {lock_path}"
        ) from error
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return handle


def _lifecycle_lock_path(config: Config) -> Path:
    """Return a stable user lock outside the teardown-owned state tree."""
    runtime_parent = Path(os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir()))
    lock_root = runtime_parent / f"storage-scale-test-integration-{os.geteuid()}"
    try:
        lock_root.mkdir(mode=0o700, exist_ok=True)
    except OSError as error:
        raise ProvisionError(
            f"cannot create lifecycle lock root {lock_root}"
        ) from error
    metadata = lock_root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise ProvisionError(f"refusing unsafe lifecycle lock root {lock_root}")
    identity = hashlib.sha256(str(config.state_dir.resolve()).encode()).hexdigest()[:20]
    return lock_root / f"{identity}.lock"


@contextmanager
def _nfs_host_lock(runner: Runner):
    """Serialize access to host-global NFS files across state directories."""
    directory_probe = runner.run(
        [*_sudo_prefix(), "test", "-e", NFS_HOST_LOCK_DIR], check=False
    )
    if directory_probe.returncode == 1:
        symlink_probe = runner.run(
            [*_sudo_prefix(), "test", "-L", NFS_HOST_LOCK_DIR], check=False
        )
        if symlink_probe.returncode == 0:
            raise ProvisionError(
                f"refusing symlinked NFS lock directory {NFS_HOST_LOCK_DIR}"
            )
        if symlink_probe.returncode != 1:
            raise ProvisionError(
                f"cannot inspect NFS lock directory {NFS_HOST_LOCK_DIR}"
            )
        runner.run([*_sudo_prefix(), "mkdir", "--", NFS_HOST_LOCK_DIR])
        runner.run([*_sudo_prefix(), "chown", "root:root", NFS_HOST_LOCK_DIR])
        runner.run([*_sudo_prefix(), "chmod", "0755", NFS_HOST_LOCK_DIR])
    elif directory_probe.returncode != 0:
        raise ProvisionError(f"cannot inspect NFS lock directory {NFS_HOST_LOCK_DIR}")
    runner.run([*_sudo_prefix(), "test", "-d", NFS_HOST_LOCK_DIR])
    runner.run([*_sudo_prefix(), "test", "!", "-L", NFS_HOST_LOCK_DIR])
    directory_stat = runner.run(
        [*_sudo_prefix(), "stat", "-c", "%u:%g:%a", NFS_HOST_LOCK_DIR]
    ).stdout.strip()
    if directory_stat != "0:0:755":
        raise ProvisionError(
            f"refusing unsafe NFS lock directory {NFS_HOST_LOCK_DIR}: "
            f"{directory_stat!r}"
        )
    lock_probe = runner.run([*_sudo_prefix(), "test", "-e", NFS_HOST_LOCK], check=False)
    if lock_probe.returncode == 1:
        symlink_probe = runner.run(
            [*_sudo_prefix(), "test", "-L", NFS_HOST_LOCK], check=False
        )
        if symlink_probe.returncode == 0:
            raise ProvisionError(f"refusing symlinked NFS host lock {NFS_HOST_LOCK}")
        if symlink_probe.returncode != 1:
            raise ProvisionError(f"cannot inspect NFS host lock {NFS_HOST_LOCK}")
        runner.run(
            [
                *_sudo_prefix(),
                "install",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0666",
                "/dev/null",
                NFS_HOST_LOCK,
            ]
        )
    elif lock_probe.returncode != 0:
        raise ProvisionError(f"cannot inspect NFS host lock {NFS_HOST_LOCK}")
    runner.run([*_sudo_prefix(), "test", "-f", NFS_HOST_LOCK])
    runner.run([*_sudo_prefix(), "test", "!", "-L", NFS_HOST_LOCK])
    lock_stat = runner.run(
        [*_sudo_prefix(), "stat", "-c", "%u:%g:%a", NFS_HOST_LOCK]
    ).stdout.strip()
    if lock_stat != "0:0:666":
        raise ProvisionError(
            f"refusing unsafe NFS host lock {NFS_HOST_LOCK}: {lock_stat!r}"
        )
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(NFS_HOST_LOCK, flags)
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o666
    ):
        os.close(descriptor)
        raise ProvisionError(f"NFS host lock changed while opening: {NFS_HOST_LOCK}")
    handle = os.fdopen(descriptor, "r+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ProvisionError(
                "another integration lifecycle owns the host-global NFS lock"
            ) from error
        yield
    finally:
        handle.close()


def _require_python() -> None:
    """Require the repository's supported Python baseline."""
    if sys.version_info < (3, 12):
        raise ProvisionError("integration-test.py requires Python 3.12 or newer")


def _nearest_existing_parent(path: Path) -> Path:
    """Return the path itself or its nearest existing parent."""
    candidate = path.resolve(strict=False)
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate


def _check_disk_capacity(disk_path: Path, label: str) -> None:
    """Require free space on the filesystem backing one concrete path."""
    disk = shutil.disk_usage(disk_path)
    if disk.free < 20 * GIB:
        raise ProvisionError(
            f"host capacity check failed: need at least 20 GiB free on "
            f"{label} ({disk_path})"
        )
    LOG.info(
        "Host capacity accepted: %.1f GiB free on %s (%s)",
        disk.free / GIB,
        label,
        disk_path,
    )


def _check_host_capacity(disk_path: Path) -> None:
    """Fail before provisioning an undersized host."""
    cpu_count = os.cpu_count() or 0
    memory = _meminfo()
    failures: list[str] = []
    if cpu_count < 2:
        failures.append(f"need at least 2 CPUs; found {cpu_count}")
    if memory.get("MemTotal", 0) < 8 * GIB:
        failures.append("need at least 8 GiB total memory")
    if memory.get("MemAvailable", 0) < 6 * GIB:
        failures.append("need at least 6 GiB available memory")
    if failures:
        raise ProvisionError("host capacity check failed: " + "; ".join(failures))
    LOG.info(
        "Host capacity accepted: %s CPUs, %.1f GiB available RAM",
        cpu_count,
        memory["MemAvailable"] / GIB,
    )
    _check_disk_capacity(_nearest_existing_parent(disk_path), "fixture storage")


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
        "namespace": config.namespace,
        "backend": backend,
        "profile": _fixture_profile(config, backend),
    }
    if backend == "sbx-shared":
        document["shared_root"] = str(config.sbx_shared_root)
    else:
        document["export_dir"] = str(config.export_dir)
    return document


def _storage_backend_identity(config: Config, backend: str) -> dict[str, object]:
    """Return immutable lifecycle identity without the replaceable profile."""
    document = _storage_backend_document(config, backend)
    document.pop("profile")
    return document


def _nfs_host_owner_document(config: Config) -> dict[str, object]:
    """Return ownership identity for fixed host-global NFS artifacts."""
    return {
        "schema": STATE_SCHEMA,
        "state_dir": str(config.state_dir),
        "cluster_name": config.cluster_name,
        "namespace": config.namespace,
        "export_dir": str(config.export_dir),
    }


def _read_nfs_host_owner(runner: Runner) -> dict[str, object] | None:
    """Read the root-owned host-global NFS owner record."""
    text = _read_system_file(runner, NFS_HOST_OWNER)
    if text is None:
        return None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProvisionError(
            f"invalid NFS host ownership record: {NFS_HOST_OWNER}"
        ) from error
    if not isinstance(document, dict):
        raise ProvisionError(f"invalid NFS host ownership record: {NFS_HOST_OWNER}")
    return document


def _claim_nfs_host_owner(runner: Runner, config: Config) -> None:
    """Claim fixed NFS paths before their first mutation."""
    expected = _nfs_host_owner_document(config)
    current = _read_nfs_host_owner(runner)
    if current is not None:
        if current != expected:
            raise ProvisionError(
                f"host-global NFS artifacts are owned by another fixture: {current}"
            )
        return
    occupied = [
        path
        for path in (NFS_EXPORT_CONFIG, NFS_DAEMON_CONFIG)
        if _read_system_file(runner, path) is not None
    ]
    if occupied:
        raise ProvisionError(
            "refusing to overwrite unowned host NFS configuration: "
            + ", ".join(str(path) for path in occupied)
        )
    source = config.manifests_dir / "nfs-host-owner.json"
    _write_text(source, json.dumps(expected, sort_keys=True) + "\n", mode=0o600)
    runner.run([*_sudo_prefix(), "install", "-m", "0600", source, NFS_HOST_OWNER])


def _validate_nfs_host_owner(runner: Runner, config: Config) -> bool:
    """Validate the fixed owner record when it exists."""
    current = _read_nfs_host_owner(runner)
    if current is None:
        return False
    expected = _nfs_host_owner_document(config)
    if current != expected:
        raise ProvisionError(
            f"refusing host NFS cleanup with mismatched owner: {NFS_HOST_OWNER}"
        )
    return True


def _validate_retained_state_summary(config: Config) -> None:
    """Reject immutable option changes after a setup has completed."""
    path = config.state_dir / "state.json"
    if not path.exists():
        return
    state = json.loads(path.read_text(encoding="utf-8"))
    backend = state.get("storage_backend")
    if backend not in STORAGE_BACKENDS:
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
        if backend not in STORAGE_BACKENDS:
            raise ProvisionError(f"invalid retained storage backend state: {path}")
        retained_identity = dict(document)
        retained_identity.pop("profile", None)
        expected_identity = _storage_backend_identity(config, backend)
        if retained_identity != expected_identity:
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


def _record_storage_backend_profile(config: Config, backend: str) -> None:
    """Persist the exact profile after cluster reconciliation succeeds."""
    _write_text(
        config.state_dir / "storage-backend.json",
        json.dumps(_storage_backend_document(config, backend), sort_keys=True) + "\n",
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
    if backend == "sbx-shared":
        raise ProvisionError(
            "sbx-shared requires its host packages to be preinstalled and "
            "never invokes sudo; missing: " + ", ".join(missing)
        )
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


def _prepare_host_dependencies(runner: Runner, config: Config, backend: str) -> None:
    """Install host packages after capturing service ownership."""
    if backend == "nfs":
        _record_nfs_service_state(runner, config)
    _ensure_apt_packages(runner, backend)


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


def _check_docker_capacity(runner: Runner) -> None:
    """Check Docker's data filesystem when it is visible to this process."""
    result = runner.run(
        ["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=60
    )
    docker_root = Path(result.stdout.strip())
    if not result.stdout.strip() or not docker_root.exists():
        LOG.info(
            "Docker data root %s is not visible locally; skipping host-side "
            "free-space validation for it",
            docker_root if result.stdout.strip() else "<unknown>",
        )
        return
    _check_disk_capacity(docker_root, "Docker data root")


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
    runner: Runner,
    config: Config,
    architecture: str,
    storage_backend: str,
) -> None:
    """Install checksum-verified clients into the user-owned state tree."""
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
        _install_client_tool(runner, config, command, version, architecture)


def _install_client_tool(
    runner: Runner,
    config: Config,
    command: str,
    version: str,
    architecture: str,
) -> None:
    """Download, verify, and install one user-local client binary."""
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
            ["install", "-m", "0755", binary, config.state_dir / "bin" / command]
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
    if not shutil.which("kind"):
        LOG.info("kind is unavailable; relying on Docker cluster discovery")
        return set()
    result = runner.run(["kind", "get", "clusters"], check=False, timeout=30)
    if result.returncode:
        raise ProvisionError(
            "kind could not list clusters: "
            f"{(result.stderr or result.stdout).strip()}"
        )
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
    name, replacements = _kind_config_inputs(config, backend)
    return _render_resource(config, name, replacements)


def _kind_config_inputs(config: Config, backend: str) -> tuple[str, dict[str, str]]:
    """Return the exact kind template and interpolation inputs."""
    if backend != "sbx-shared":
        return "manifests/kind.yaml.tmpl", {"CLUSTER_NAME": config.cluster_name}
    kmsg_mount = ""
    if not Path("/dev/kmsg").exists():
        kmsg_mount = "\n".join(
            ("      - hostPath: /dev/null", "        containerPath: /dev/kmsg")
        )
    return (
        "manifests/kind-sbx-shared.yaml.tmpl",
        {
            "CLUSTER_NAME": config.cluster_name,
            "SHARED_ROOT": str(config.sbx_shared_root),
            "KMSG_MOUNT": kmsg_mount,
        },
    )


def _fixture_profile(config: Config, backend: str) -> dict[str, str]:
    """Return the cluster inputs that require disposable-cluster replacement."""
    name, replacements = _kind_config_inputs(config, backend)
    rendered = _render_resource_text(name, replacements)
    profile = {
        "kind_version": SBX_KIND_VERSION if backend == "sbx-shared" else KIND_VERSION,
        "node_image": (
            SBX_KIND_NODE_IMAGE if backend == "sbx-shared" else KIND_NODE_IMAGE
        ),
        "kind_config_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
    }
    if backend != "sbx-shared":
        return {**profile, "cni": "kindnet", "cni_image": KINDNET_IMAGE}
    return {
        **profile,
        "cni": "calico",
        "cni_version": CALICO_VERSION,
        "cni_manifest_sha256": CALICO_MANIFEST_SHA256,
        "cni_recipe": CALICO_SBX_RECIPE,
        "cni_pod_subnet": CALICO_POD_SUBNET,
        "cni_images_sha256": hashlib.sha256(
            json.dumps(CALICO_IMAGES, sort_keys=True).encode()
        ).hexdigest(),
    }


def _create_cluster(runner: Runner, config: Config, backend: str) -> None:
    """Create a new owned kind cluster."""
    manifest = _render_kind_config(config, backend)
    node_image = SBX_KIND_NODE_IMAGE if backend == "sbx-shared" else KIND_NODE_IMAGE
    _ensure_pinned_image(runner, node_image)
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
    _ensure_pinned_image(runner, SBX_KIND_NODE_IMAGE)
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
        # Docker SBX can remap one pod's file owner when the same bind mount is
        # observed from a replacement pod. A sticky directory would then stop
        # the fixed workload identity from deleting its own prior files.
        directory.chmod(SBX_SHARED_DIRECTORY_MODE)
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


def _expected_kubernetes_version(backend: str) -> str:
    """Return the kubelet version selected by one fixture profile."""
    return SBX_KUBERNETES_VERSION if backend == "sbx-shared" else KUBERNETES_VERSION


def _validate_kindnet_profile(runner: Runner, config: Config) -> None:
    """Require the exact healthy Kindnet bundled by the NFS node profile."""
    document = json.loads(
        runner.run(
            _kubectl(
                config,
                "-n",
                "kube-system",
                "get",
                "daemonset/kindnet",
                "-o",
                "json",
            ),
            timeout=30,
        ).stdout
    )
    status = document.get("status", {})
    containers = (
        document.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    observed = containers[0].get("image") if len(containers) == 1 else None
    if (
        status.get("desiredNumberScheduled") != 3
        or status.get("numberReady") != 3
        or status.get("numberAvailable") != 3
        or observed != KINDNET_IMAGE
    ):
        raise ProvisionError(
            "Kindnet does not match the healthy NFS fixture profile: "
            f"image={observed}, status={status}"
        )


def _calico_image(component: str) -> tuple[str, str]:
    """Return the digest-qualified upstream image and fixture-private alias."""
    digest = dict(CALICO_IMAGES)[component]
    upstream = f"docker.io/calico/{component}:{CALICO_VERSION}@{digest}"
    destination = (
        "docker.io/storage-scale-test-integration/"
        f"calico-{component}:{CALICO_VERSION}"
    )
    return upstream, destination


def _observed_calico_images(runner: Runner, config: Config) -> dict[str, str]:
    """Return images from the healthy Calico node and controller workloads."""
    result = runner.run(
        _kubectl(
            config, "-n", "kube-system", "get", "daemonset/calico-node", "-o", "json"
        ),
        timeout=30,
    )
    document = json.loads(result.stdout)
    status = document.get("status", {})
    desired = status.get("desiredNumberScheduled")
    ready = status.get("numberReady")
    available = status.get("numberAvailable")
    pod_spec = document.get("spec", {}).get("template", {}).get("spec", {})
    containers = pod_spec.get("containers", [])
    init_containers = pod_spec.get("initContainers", [])
    if desired != 3 or ready != 3 or available != 3:
        raise ProvisionError(
            "Calico is not healthy on all fixture nodes: "
            f"desired={desired}, ready={ready}, available={available}, "
            f"containers={len(containers)}"
        )
    controller = json.loads(
        runner.run(
            _kubectl(
                config,
                "-n",
                "kube-system",
                "get",
                "deployment/calico-kube-controllers",
                "-o",
                "json",
            ),
            timeout=30,
        ).stdout
    )
    controller_status = controller.get("status", {})
    if controller_status.get("availableReplicas") != 1:
        raise ProvisionError("Calico kube-controllers is not available")
    observed: dict[str, str] = {}
    for container in [*containers, *init_containers]:
        name = str(container.get("name", ""))
        image = container.get("image")
        if name in {
            "calico-node",
            "upgrade-ipam",
            "install-cni",
            "mount-bpffs",
            "ebpf-bootstrap",
        }:
            if isinstance(image, str):
                observed[name] = image
    controller_containers = (
        controller.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    if controller_containers:
        observed["calico-kube-controllers"] = str(
            controller_containers[0].get("image", "")
        )
    return observed


def _validate_calico_profile(runner: Runner, config: Config) -> None:
    """Require the exact preloaded Calico images selected by the fixture."""
    observed = _observed_calico_images(runner, config)
    expected = {
        "calico-node": _calico_image("node")[1],
        "ebpf-bootstrap": _calico_image("node")[1],
        "upgrade-ipam": _calico_image("cni")[1],
        "install-cni": _calico_image("cni")[1],
        "calico-kube-controllers": _calico_image("kube-controllers")[1],
    }
    if observed != expected:
        raise ProvisionError(
            f"Calico images do not match the fixture profile: {observed} != {expected}"
        )


def _validate_network_policy_profile(
    runner: Runner, config: Config, backend: str
) -> str:
    """Validate and name the backend-specific policy-capable CNI."""
    if backend == "sbx-shared":
        _validate_calico_profile(runner, config)
        return f"Calico {CALICO_VERSION}"
    _validate_kindnet_profile(runner, config)
    return KINDNET_IMAGE


def _observed_kubernetes_version(runner: Runner, config: Config) -> str:
    """Return the one kubelet version observed on every fixture node."""
    nodes = json.loads(
        runner.run(_kubectl(config, "get", "nodes", "-o", "json")).stdout
    ).get("items", [])
    versions = {
        node.get("status", {}).get("nodeInfo", {}).get("kubeletVersion")
        for node in nodes
    }
    versions.discard(None)
    if len(nodes) != 3 or len(versions) != 1:
        raise ProvisionError(
            "retained cluster does not have three nodes at one kubelet version: "
            f"nodes={len(nodes)}, versions={sorted(versions)}"
        )
    return str(versions.pop())


def _retained_cluster_matches_profile(
    runner: Runner, config: Config, backend: str
) -> bool:
    """Return whether a running retained cluster matches the selected profile."""
    state_path = config.state_dir / "storage-backend.json"
    if not state_path.exists():
        LOG.warning("Replacing retained kind cluster without a fixture profile")
        return False
    retained = json.loads(state_path.read_text(encoding="utf-8"))
    if retained.get("profile") != _fixture_profile(config, backend):
        LOG.warning("Replacing retained kind cluster after fixture profile drift")
        return False
    _wait_for_kube_api(runner, config)
    observed = _observed_kubernetes_version(runner, config)
    expected = _expected_kubernetes_version(backend)
    if observed != expected:
        LOG.warning(
            "Replacing retained kind cluster with kubelet %s; profile requires %s",
            observed,
            expected,
        )
        return False
    try:
        _validate_network_policy_profile(runner, config, backend)
    except (
        ProvisionError,
        subprocess.CalledProcessError,
        json.JSONDecodeError,
    ) as error:
        LOG.warning("Replacing retained kind cluster after CNI drift: %s", error)
        return False
    return True


def _wait_for_kube_api(runner: Runner, config: Config) -> None:
    """Wait for the Kubernetes API to become usable."""
    deadline = time.monotonic() + 90
    while True:
        request_timeout, process_timeout = _poll_timeouts(deadline, 5)
        if process_timeout <= 0:
            break
        probe = runner.run(
            _kubectl(
                config,
                "get",
                "nodes",
                f"--request-timeout={request_timeout}s",
            ),
            check=False,
            timeout=process_timeout,
        )
        if probe.returncode == 0:
            return
        LOG.info("Waiting for the Kubernetes API")
        time.sleep(min(3, max(0, deadline - time.monotonic())))
    raise ProvisionError("Kubernetes API did not become usable within 90 seconds")


def _poll_timeouts(deadline: float, request_limit: int) -> tuple[int, float]:
    """Return API and process bounds that cannot cross an overall deadline."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0, 0
    request_timeout = max(1, min(request_limit, int(remaining)))
    return request_timeout, min(request_timeout + 5, remaining)


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
    export_probe = runner.run(
        [*_sudo_prefix(), "test", "-e", config.export_dir],
        check=False,
        timeout=30,
    )
    if export_probe.returncode not in {0, 1}:
        raise ProvisionError(
            f"could not determine whether export exists: {config.export_dir}"
        )
    export_exists = export_probe.returncode == 0
    existing = _read_system_file(runner, marker_path)
    if export_exists and existing is None:
        raise ProvisionError(
            f"refusing to modify unowned export directory: {config.export_dir}"
        )
    if not export_exists:
        parent_exists = runner.run(
            [*_sudo_prefix(), "test", "-d", config.export_dir.parent],
            check=False,
            timeout=30,
        )
        if parent_exists.returncode not in {0, 1}:
            raise ProvisionError(
                f"could not inspect export parent: {config.export_dir.parent}"
            )
        if parent_exists.returncode == 1:
            raise ProvisionError(
                "export directory must be a leaf below an existing directory: "
                f"{config.export_dir}"
            )
    if existing is not None and existing.strip() != expected:
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
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1:
        return ""
    raise ProvisionError(
        f"findmnt could not inspect {config.export_dir}: "
        f"{(result.stderr or result.stdout).strip()}"
    )


def _unmount_temporary_filesystem(runner: Runner, mountpoint: Path) -> None:
    """Unmount a temporary filesystem and verify it is no longer mounted."""
    last_result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(1, TEMPORARY_UNMOUNT_ATTEMPTS + 1):
        last_result = runner.run([*_sudo_prefix(), "umount", mountpoint], check=False)
        mounted = runner.run(
            [
                "findmnt",
                "--noheadings",
                "--mountpoint",
                mountpoint,
            ],
            check=False,
        )
        if mounted.returncode == 1:
            return
        if mounted.returncode != 0:
            raise ProvisionError(
                f"could not verify temporary mount was removed at {mountpoint}: "
                f"findmnt exited {mounted.returncode}"
            )
        if attempt < TEMPORARY_UNMOUNT_ATTEMPTS:
            LOG.warning(
                "Temporary mount %s remains active after unmount attempt %d/%d; "
                "retrying",
                mountpoint,
                attempt,
                TEMPORARY_UNMOUNT_ATTEMPTS,
            )
            time.sleep(TEMPORARY_UNMOUNT_RETRY_SECONDS)

    detail = (last_result.stderr or last_result.stdout).strip() if last_result else ""
    suffix = f": {detail}" if detail else ""
    raise ProvisionError(
        f"temporary mount remains active at {mountpoint} after "
        f"{TEMPORARY_UNMOUNT_ATTEMPTS} unmount attempts{suffix}"
    )


def _loop_devices_for_file(runner: Runner, path: Path) -> list[str]:
    """Return loop devices associated with one exact backing file."""
    if not path.exists():
        return []
    result = runner.run([*_sudo_prefix(), "losetup", "--associated", path], check=False)
    if result.returncode:
        raise ProvisionError(
            f"losetup could not inspect {path}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return [line.split(":", maxsplit=1)[0] for line in result.stdout.splitlines()]


def _migrate_export_data_to_image(runner: Runner, config: Config) -> None:
    """Transactionally create an NFS image containing existing export data."""
    LOG.info("Creating sparse %s-byte NFS backing filesystem", NFS_IMAGE_BYTES)
    temporary_image = config.nfs_image.with_suffix(".ext4.new")
    stale_loops = _loop_devices_for_file(runner, temporary_image)
    if stale_loops:
        raise ProvisionError(
            f"temporary NFS image remains attached to {', '.join(stale_loops)}; "
            "unmount it before retrying setup"
        )
    temporary_image.unlink(missing_ok=True)
    runner.run(["truncate", "--size", str(NFS_IMAGE_BYTES), temporary_image])
    runner.run(["/usr/sbin/mkfs.ext4", "-F", "-q", "-m", "0", temporary_image])

    migration_mount = Path(
        tempfile.mkdtemp(prefix="nfs-migration-", dir=config.state_dir)
    )
    try:
        try:
            runner.run(
                [
                    *_sudo_prefix(),
                    "mount",
                    "-o",
                    "loop",
                    temporary_image,
                    migration_mount,
                ]
            )
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
            # A failed mount command can still leave a live mount. Always
            # verify unmount before the path or temporary image can be removed.
            _unmount_temporary_filesystem(runner, migration_mount)
        check = runner.run(
            [*_sudo_prefix(), "e2fsck", "-pf", temporary_image],
            check=False,
            timeout=300,
        )
        if check.returncode not in {0, 1}:
            raise ProvisionError(
                "temporary NFS filesystem validation failed with exit "
                f"{check.returncode}: {(check.stderr or check.stdout).strip()}"
            )
        temporary_image.replace(config.nfs_image)
    except Exception:
        mounted = runner.run(
            ["findmnt", "--noheadings", "--mountpoint", migration_mount],
            check=False,
        )
        if mounted.returncode == 1 and not _loop_devices_for_file(
            runner, temporary_image
        ):
            temporary_image.unlink(missing_ok=True)
            migration_mount.rmdir()
        raise
    migration_mount.rmdir()


def _ensure_nfs_image_capacity(
    runner: Runner, config: Config, loop_device: str | None = None
) -> None:
    """Grow the sparse NFS image and its ext4 filesystem when required."""
    current_size = config.nfs_image.stat().st_size
    if current_size < NFS_IMAGE_BYTES:
        LOG.info(
            "Growing sparse NFS backing image from %s to %s bytes",
            current_size,
            NFS_IMAGE_BYTES,
        )
        runner.run(["truncate", "--size", str(NFS_IMAGE_BYTES), config.nfs_image])
    if loop_device:
        runner.run(
            [*_sudo_prefix(), "losetup", "--set-capacity", loop_device],
            timeout=60,
        )
    runner.run(
        [*_sudo_prefix(), "resize2fs", loop_device or config.nfs_image],
        timeout=300,
    )


def _validate_nfs_filesystem_capacity(runner: Runner, config: Config) -> None:
    """Require the mounted export to satisfy the shared fixture budget."""
    result = runner.run(
        [
            *_sudo_prefix(),
            "stat",
            "-f",
            "-c",
            "%b %S %a",
            config.export_dir,
        ],
        timeout=30,
    )
    fields = result.stdout.split()
    if len(fields) != 3 or not all(field.isdigit() for field in fields):
        raise ProvisionError(
            f"could not parse NFS filesystem capacity: {result.stdout!r}"
        )
    blocks, fragment_size, available_blocks = map(int, fields)
    total_bytes = blocks * fragment_size
    available_bytes = available_blocks * fragment_size
    if total_bytes < NFS_BUDGET_BYTES or available_bytes < NFS_BUDGET_BYTES:
        raise ProvisionError(
            "NFS backing filesystem is below the fixture capacity budget: "
            f"total={total_bytes}, available={available_bytes}, "
            f"required_available={NFS_BUDGET_BYTES}"
        )


def _ensure_export_filesystem(runner: Runner, config: Config) -> None:
    """Mount a sized persistent filesystem for realistic mount validation."""
    _ensure_export_marker(runner, config)
    mounted_type = _export_mount_type(runner, config)
    if mounted_type:
        if mounted_type != "ext4":
            raise ProvisionError(
                f"refusing non-ext4 mount at dedicated export {config.export_dir}: "
                f"{mounted_type}"
            )
        loop_device = _verified_export_loop(runner, config)
        _ensure_nfs_image_capacity(runner, config, loop_device)
        _validate_nfs_filesystem_capacity(runner, config)
        return

    if not config.nfs_image.exists():
        _migrate_export_data_to_image(runner, config)
    _ensure_nfs_image_capacity(runner, config)
    runner.run([*_sudo_prefix(), "exportfs", "-u", config.export_dir], check=False)
    runner.run(
        [*_sudo_prefix(), "mount", "-o", "loop", config.nfs_image, config.export_dir]
    )
    _ensure_export_marker(runner, config)
    _validate_nfs_filesystem_capacity(runner, config)


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
    desired = (
        (NFS_EXPORT_CONFIG, export_source, export_line),
        (
            NFS_DAEMON_CONFIG,
            nfs_source,
            f"[nfsd]\nvers3 = n\nvers4 = y\nthreads = {NFS_SERVER_THREADS}\n",
        ),
    )
    for entry in desired:
        installed, source = entry[:2]
        retained = source.read_text(encoding="utf-8") if source.exists() else None
        existing = _read_system_file(runner, installed)
        if existing is not None and existing != retained:
            raise ProvisionError(
                f"refusing to overwrite changed host NFS configuration: {installed}"
            )
    for entry in desired:
        source, content = entry[1:]
        _write_text(source, content)
    runner.run([*_sudo_prefix(), "install", "-d", "/etc/exports.d", "/etc/nfs.conf.d"])
    for entry in desired:
        installed, source = entry[:2]
        runner.run(
            [
                *_sudo_prefix(),
                "install",
                "-m",
                "0644",
                source,
                installed,
            ]
        )
    _ensure_nfs_firewall(runner, config, subnet)
    runner.run([*_sudo_prefix(), "exportfs", "-rav"])
    runner.run([*_sudo_prefix(), "systemctl", "enable", "--now", "nfs-server"])
    _set_live_nfs_threads(runner, NFS_SERVER_THREADS)
    state = {"subnet": subnet, "gateway": gateway}
    _write_text(config.state_dir / "network.json", json.dumps(state, indent=2) + "\n")


def _live_nfs_threads(runner: Runner) -> int | None:
    """Return the active kernel NFS worker count when it is available."""
    result = runner.run(
        [*_sudo_prefix(), "cat", NFS_THREADS_PATH],
        check=False,
        timeout=30,
    )
    value = result.stdout.strip()
    if result.returncode or not value.isdigit() or int(value) < 1:
        return None
    return int(value)


def _set_live_nfs_threads(runner: Runner, count: int) -> None:
    """Apply and verify a kernel NFS worker count without restarting NFS."""
    current = _live_nfs_threads(runner)
    if current == count:
        return
    rpc_nfsd = shutil.which("rpc.nfsd")
    if not rpc_nfsd:
        raise ProvisionError("rpc.nfsd is unavailable; cannot set NFS worker count")
    runner.run([*_sudo_prefix(), rpc_nfsd, str(count)], timeout=60)
    actual = _live_nfs_threads(runner)
    if actual != count:
        raise ProvisionError(
            f"NFS worker reconciliation requested {count}, found {actual!r}"
        )


def _systemd_unit_exists(runner: Runner, unit: str) -> bool:
    """Return whether systemd currently knows about one unit."""
    result = runner.run(
        [*_sudo_prefix(), "systemctl", "show", "--property=LoadState", "--value", unit],
        check=False,
    )
    if result.returncode:
        raise ProvisionError(
            f"systemctl could not inspect {unit}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout.strip() not in {"", "not-found"}


def _systemd_unit_state(runner: Runner, unit: str) -> tuple[bool, bool, bool]:
    """Return unit existence plus independent active and enabled states."""
    exists = _systemd_unit_exists(runner, unit)
    if not exists:
        return False, False, False
    active = (
        runner.run(
            [*_sudo_prefix(), "systemctl", "is-active", unit], check=False
        ).returncode
        == 0
    )
    enabled = (
        runner.run(
            [*_sudo_prefix(), "systemctl", "is-enabled", unit], check=False
        ).returncode
        == 0
    )
    return True, active, enabled


def _record_nfs_service_state(runner: Runner, config: Config) -> None:
    """Remember independent NFS installation, runtime, and boot states."""
    path = config.state_dir / "nfs-service.json"
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("schema") != NFS_SERVICE_STATE_SCHEMA:
            raise ProvisionError(
                "retained NFS service state predates independent active/enabled "
                "tracking; run teardown before setup"
            )
        return
    existed, active, enabled = _systemd_unit_state(runner, "nfs-server")
    state: dict[str, object] = {
        "schema": NFS_SERVICE_STATE_SCHEMA,
        "service_existed": existed,
        "was_active": active,
        "was_enabled": enabled,
        "previous_threads": None,
    }
    if active:
        previous_threads = _live_nfs_threads(runner)
        if previous_threads is None:
            raise ProvisionError(
                "cannot record the pre-existing NFS worker count before setup"
            )
        state["previous_threads"] = previous_threads
    _write_text(path, json.dumps(state, sort_keys=True) + "\n")


def _ensure_nfs_firewall(runner: Runner, config: Config, subnet: str) -> None:
    """Allow NFS only from kind when UFW is active."""
    state_path = config.state_dir / "ufw-rule.json"
    previous: dict[str, object] = {}
    if state_path.exists():
        previous = json.loads(state_path.read_text(encoding="utf-8"))
    if not shutil.which("ufw"):
        LOG.warning("ufw is absent; verify an equivalent TCP-2049 restriction")
        return
    status = runner.run([*_sudo_prefix(), "ufw", "status"])
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
        state_path.unlink(missing_ok=True)
        previous = {}
        status = runner.run([*_sudo_prefix(), "ufw", "status"])
    rule_exists = _nfs_firewall_rule_exists(runner, subnet)
    added_by_harness = bool(previous.get("added_by_harness"))
    if rule_exists:
        LOG.info("The dedicated UFW NFS rule is already present")
    else:
        _write_text(
            state_path,
            json.dumps({"added_by_harness": True, "subnet": subnet, "status": "adding"})
            + "\n",
        )
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
        json.dumps(
            {
                "added_by_harness": added_by_harness,
                "subnet": subnet,
                "status": "present" if added_by_harness else "external",
            }
        )
        + "\n",
    )


def _delete_nfs_firewall_rule(runner: Runner, subnet: str) -> None:
    """Idempotently delete and verify the exact harness UFW rule."""
    if not _nfs_firewall_rule_exists(runner, subnet):
        return
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
    )
    if _nfs_firewall_rule_exists(runner, subnet):
        raise ProvisionError(f"UFW rule remains configured for {subnet}")


def _nfs_firewall_rule_exists(runner: Runner, subnet: str) -> bool:
    """Check UFW's stored rules even when its runtime firewall is inactive."""
    added = runner.run([*_sudo_prefix(), "ufw", "show", "added"])
    return any(
        subnet in line and "2049" in line and "tcp" in line and UFW_COMMENT in line
        for line in added.stdout.splitlines()
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


def _image_id(runner: Runner, image: str) -> str | None:
    """Return a local Docker image ID, or None when its tag is absent."""
    result = runner.run(
        ["docker", "image", "inspect", "--format", "{{.Id}}", image], check=False
    )
    if result.returncode == 0:
        return result.stdout.strip()
    detail = (result.stderr or result.stdout).lower()
    if result.returncode == 1 and (
        "no such image" in detail or "no such object" in detail
    ):
        return None
    raise ProvisionError(
        f"Docker could not inspect image {image}: "
        f"{(result.stderr or result.stdout).strip()}"
    )


def _inspect_pinned_image(
    runner: Runner,
    reference: str,
    digest: str,
    architecture: str,
    timeout: float = IMAGE_INSPECT_TIMEOUT_SECONDS,
) -> bool:
    """Return whether one local reference has the required digest and platform."""
    try:
        result = runner.run(
            ["docker", "image", "inspect", "--format", "{{json .}}", reference],
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ProvisionError(
            f"Docker timed out inspecting image {reference} after {timeout:.1f} seconds"
        ) from error
    if result.returncode:
        detail = (result.stderr or result.stdout).lower()
        if result.returncode == 1 and (
            "no such image" in detail or "no such object" in detail
        ):
            return False
        raise ProvisionError(
            f"Docker could not inspect image {reference}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ProvisionError(
            f"Docker returned invalid metadata for {reference}"
        ) from error
    repo_digests = document.get("RepoDigests", [])
    if document.get("Architecture") != architecture or not any(
        isinstance(item, str) and item.endswith(f"@{digest}") for item in repo_digests
    ):
        return False
    return True


def _transient_image_pull_failure(detail: str) -> bool:
    """Return whether a registry failure is suitable for bounded retry."""
    lowered = detail.lower()
    phrases = (
        "too many requests",
        "toomanyrequests",
        "timeout",
        "timed out",
        "deadline exceeded",
        "connection",
        "network is unreachable",
        "no route to host",
        "temporary failure",
        "temporarily unavailable",
        "unexpected eof",
    )
    status_failure = re.search(
        r"\b(?:http(?: status)?|status(?: code)?|response code)"
        r"[ :=]+(?:429|5\d\d)\b",
        lowered,
    ) or re.search(
        r"\b5\d\d\s+(?:internal server error|bad gateway|service unavailable|gateway timeout)\b",
        lowered,
    )
    return any(phrase in lowered for phrase in phrases) or bool(status_failure)


def _pull_pinned_image(
    runner: Runner,
    reference: str,
    digest: str,
    architecture: str,
    deadline: float,
) -> str | None:
    """Pull and verify one image, returning its final error when unavailable."""
    for attempt in range(1, IMAGE_PULL_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "image pull retry budget expired"
        pull_timeout = min(float(IMAGE_PULL_TIMEOUT_SECONDS), remaining)
        try:
            pulled = runner.run(
                [
                    "docker",
                    "pull",
                    "--platform",
                    f"linux/{architecture}",
                    reference,
                ],
                check=False,
                timeout=pull_timeout,
            )
            if pulled.returncode == 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return (
                        f"image acquisition exceeded "
                        f"{IMAGE_PULL_DEADLINE_SECONDS} seconds"
                    )
                if _inspect_pinned_image(
                    runner,
                    reference,
                    digest,
                    architecture,
                    timeout=min(float(IMAGE_INSPECT_TIMEOUT_SECONDS), remaining),
                ):
                    return None
                raise ProvisionError(
                    "Docker pulled the wrong digest or architecture for " f"{reference}"
                )
            detail = (pulled.stderr or pulled.stdout).strip() or (
                f"docker pull exited {pulled.returncode}"
            )
        except subprocess.TimeoutExpired:
            detail = f"timed out after {pull_timeout:.1f} seconds"
        if attempt == IMAGE_PULL_ATTEMPTS or not _transient_image_pull_failure(detail):
            return detail
        remaining = deadline - time.monotonic()
        delay = IMAGE_PULL_INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)) + (
            secrets.randbelow(IMAGE_PULL_JITTER_MILLISECONDS) / 1000
        )
        if delay >= remaining:
            return (
                f"{detail}; insufficient retry time within the "
                "image acquisition deadline"
            )
        LOG.warning(
            "Image pull attempt %d/%d failed for %s: %s; retrying in %.2fs",
            attempt,
            IMAGE_PULL_ATTEMPTS,
            reference,
            detail,
            delay,
        )
        time.sleep(delay)
    raise AssertionError("image pull retry loop exhausted without a result")


def _acquire_pinned_image(
    runner: Runner, references: Sequence[str], digest: str
) -> str:
    """Reuse or pull one verified platform image from ordered references."""
    architecture = _check_platform()
    deadline = time.monotonic() + IMAGE_PULL_DEADLINE_SECONDS
    for reference in references:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProvisionError(
                f"image acquisition exceeded {IMAGE_PULL_DEADLINE_SECONDS} seconds"
            )
        if _inspect_pinned_image(
            runner,
            reference,
            digest,
            architecture,
            timeout=min(float(IMAGE_INSPECT_TIMEOUT_SECONDS), remaining),
        ):
            return reference
    pull_references: list[str] = []
    for reference in references:
        repository, separator, reference_digest = reference.rpartition("@")
        if not separator:
            continue
        if not repository or reference_digest != digest:
            raise ProvisionError(
                f"fixture image reference does not match {digest}: {reference}"
            )
        pull_references.append(reference)
    failures: list[str] = []
    reserve_per_reference = IMAGE_PULL_TIMEOUT_SECONDS + IMAGE_INSPECT_TIMEOUT_SECONDS
    for index, reference in enumerate(pull_references):
        later_references = len(pull_references) - index - 1
        reference_deadline = deadline - (later_references * reserve_per_reference)
        failure = _pull_pinned_image(
            runner,
            reference,
            digest,
            architecture,
            reference_deadline,
        )
        if failure is None:
            return reference
        failures.append(f"{reference}: {failure}")
    raise ProvisionError("could not obtain pinned image; " + " | ".join(failures))


def _ensure_pinned_image(runner: Runner, reference: str) -> str:
    """Reuse or pull one digest-qualified image for the current architecture."""
    repository, separator, digest = reference.rpartition("@")
    if not separator or not repository or not digest.startswith("sha256:"):
        raise ProvisionError(f"fixture base image is not digest-qualified: {reference}")
    return _acquire_pinned_image(runner, (reference,), digest)


def _image_ownership_state(config: Config) -> dict[str, dict[str, str | None]]:
    """Read the journal for fixture-owned Docker tags."""
    state_path = config.state_dir / "built-images.json"
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))


def _write_image_ownership_state(
    config: Config, state: dict[str, dict[str, str | None]]
) -> None:
    """Atomically persist the Docker-tag ownership journal."""
    state_path = config.state_dir / "built-images.json"
    _write_text(state_path, json.dumps(state, indent=2, sort_keys=True) + "\n")


def _recover_pending_image_build(
    runner: Runner,
    config: Config,
    image: str,
    ownership: dict[str, str | None],
) -> None:
    """Reconcile an interrupted uniquely tagged build before reuse."""
    temporary_tag = ownership.get("pending_tag")
    if not temporary_tag:
        return
    pending_id = ownership.get("pending_id") or _image_id(runner, temporary_tag)
    current = _image_id(runner, image)
    allowed = {
        ownership.get("previous_id"),
        ownership.get("built_id"),
        pending_id,
        None,
    }
    if current not in allowed:
        raise ProvisionError(
            f"refusing to alter Docker tag changed outside the fixture: {image}"
        )
    if pending_id and current == pending_id:
        ownership["built_id"] = pending_id
    temporary_id = _image_id(runner, temporary_tag)
    if temporary_id is not None:
        if pending_id is not None and temporary_id != pending_id:
            raise ProvisionError(
                f"refusing to remove changed pending Docker tag: {temporary_tag}"
            )
        runner.run(["docker", "image", "rm", temporary_tag])
    ownership["pending_tag"] = None
    ownership["pending_id"] = None
    state = _image_ownership_state(config)
    state[image] = ownership
    _write_image_ownership_state(config, state)


def _begin_image_build(runner: Runner, config: Config, image: str) -> str:
    """Journal and return a unique tag without mutating the published tag."""
    state = _image_ownership_state(config)
    ownership = state.get(image)
    if ownership is None:
        ownership = {
            "previous_id": _image_id(runner, image),
            "built_id": None,
            "pending_tag": None,
            "pending_id": None,
        }
        state[image] = ownership
    elif ownership.get("pending_tag"):
        _recover_pending_image_build(runner, config, image, ownership)
        state = _image_ownership_state(config)
        ownership = state[image]
    current = _image_id(runner, image)
    if current not in {ownership.get("previous_id"), ownership.get("built_id"), None}:
        raise ProvisionError(
            f"refusing to overwrite Docker tag changed outside the fixture: {image}"
        )
    temporary_tag = f"{image}-pending-{secrets.token_hex(6)}"
    ownership["pending_tag"] = temporary_tag
    ownership["pending_id"] = None
    _write_image_ownership_state(config, state)
    return temporary_tag


def _publish_image_build(
    runner: Runner, config: Config, image: str, temporary_tag: str
) -> None:
    """Publish a completed temporary image and finish its ownership journal."""
    state = _image_ownership_state(config)
    ownership = state[image]
    if ownership.get("pending_tag") != temporary_tag:
        raise ProvisionError(f"Docker build journal changed unexpectedly for {image}")
    pending_id = _image_id(runner, temporary_tag)
    if pending_id is None:
        raise ProvisionError(f"Docker build did not produce {temporary_tag}")
    ownership["pending_id"] = pending_id
    _write_image_ownership_state(config, state)
    current = _image_id(runner, image)
    if current not in {ownership.get("previous_id"), ownership.get("built_id"), None}:
        raise ProvisionError(
            f"refusing to overwrite Docker tag changed outside the fixture: {image}"
        )
    runner.run(["docker", "image", "tag", temporary_tag, image])
    ownership["built_id"] = pending_id
    _write_image_ownership_state(config, state)
    runner.run(["docker", "image", "rm", temporary_tag])
    ownership["pending_tag"] = None
    ownership["pending_id"] = None
    _write_image_ownership_state(config, state)


def _build_owned_image(
    runner: Runner,
    config: Config,
    image: str,
    dockerfile: Path,
    base_image: str,
    build_arguments: Sequence[str] = (),
) -> None:
    """Build one fixture image without exposing an unjournaled fixed tag."""
    _ensure_pinned_image(runner, base_image)
    temporary_tag = _begin_image_build(runner, config, image)
    runner.run(
        [
            "docker",
            "build",
            "--tag",
            temporary_tag,
            *build_arguments,
            "--file",
            dockerfile,
            _resource_path("."),
        ],
        timeout=600,
    )
    _publish_image_build(runner, config, image, temporary_tag)


def _prepare_csi_images(runner: Runner, config: Config) -> None:
    """Reuse or pull pinned CSI images and load fixture-only node tags."""
    LOG.info("Preparing pinned NFS CSI images")
    nodes = _kind_containers(runner, config, running_only=True)
    if len(nodes) != 3:
        raise ProvisionError(
            f"cannot preload CSI images: expected 3 nodes, found {len(nodes)}"
        )
    _remove_stale_csi_host_aliases(runner)
    _remove_stale_node_image_aliases(
        runner,
        nodes,
        "docker.io/storage-scale-integration-csi/",
    )
    for name, tag, digest in CSI_IMAGES:
        destination = f"registry.k8s.io/sig-storage/{name}:{tag}"
        repositories = tuple(f"{repository}/{name}" for repository in CSI_REPOSITORIES)
        candidates = (
            destination,
            *(f"{repository}@{digest}" for repository in repositories),
        )
        selected = _acquire_pinned_image(runner, candidates, digest)
        temporary_prefix = f"storage-scale-integration-csi/{name}:{tag}-"
        temporary = f"{temporary_prefix}{secrets.token_hex(6)}"
        runner.run(["docker", "image", "tag", selected, temporary])
        try:
            _load_image_into_nodes(runner, temporary, nodes, destination=destination)
        except (ProvisionError, subprocess.SubprocessError, OSError):
            try:
                runner.run(["docker", "image", "rm", temporary])
            except (ProvisionError, subprocess.SubprocessError, OSError):
                LOG.exception(
                    "Could not remove temporary CSI alias %s after staging failed",
                    temporary,
                )
            raise
        else:
            runner.run(["docker", "image", "rm", temporary])


def _remove_stale_host_image_aliases(runner: Runner, prefix: str) -> None:
    """Remove exact fixture-private Docker aliases left by interruption."""
    result = runner.run(
        [
            "docker",
            "image",
            "ls",
            "--format",
            "{{.Repository}}:{{.Tag}}",
            "--filter",
            f"reference={prefix}*",
        ]
    )
    aliases = sorted(set(result.stdout.splitlines()))
    if any(not alias.startswith(prefix) for alias in aliases):
        raise ProvisionError(
            f"Docker returned an unexpected temporary alias: {aliases}"
        )
    for alias in aliases:
        runner.run(["docker", "image", "rm", alias])


def _remove_stale_csi_host_aliases(runner: Runner) -> None:
    """Remove all fixture-private CSI aliases left by interrupted setup."""
    _remove_stale_host_image_aliases(runner, "storage-scale-integration-csi/")


def _remove_stale_node_image_aliases(
    runner: Runner, nodes: list[str], prefix: str
) -> None:
    """Remove exact fixture-private containerd aliases left by interruption."""
    for node in nodes:
        result = runner.run(
            [
                "docker",
                "exec",
                node,
                "ctr",
                "-n",
                "k8s.io",
                "images",
                "list",
                "--quiet",
            ],
            timeout=60,
        )
        aliases = sorted(
            alias
            for alias in set(result.stdout.splitlines())
            if alias.startswith(prefix)
        )
        for alias in aliases:
            runner.run(
                [
                    "docker",
                    "exec",
                    node,
                    "ctr",
                    "-n",
                    "k8s.io",
                    "images",
                    "remove",
                    alias,
                ],
                timeout=60,
            )


def _load_image_into_nodes(
    runner: Runner,
    image: str,
    nodes: list[str],
    *,
    destination: str | None = None,
) -> None:
    """Import an image and optionally publish a node-local chart tag."""
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
            if destination:
                registry = image.split("/", maxsplit=1)[0]
                source = (
                    image
                    if "." in registry or ":" in registry or registry == "localhost"
                    else f"docker.io/{image}"
                )
                runner.run(
                    [
                        "docker",
                        "exec",
                        node,
                        "ctr",
                        "-n",
                        "k8s.io",
                        "images",
                        "tag",
                        "--force",
                        source,
                        destination,
                    ],
                    timeout=60,
                )
                runner.run(
                    [
                        "docker",
                        "exec",
                        node,
                        "ctr",
                        "-n",
                        "k8s.io",
                        "images",
                        "remove",
                        source,
                    ],
                    timeout=60,
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
        {
            "NAMESPACE": config.namespace,
            "NFS_SERVER": gateway,
            "STORAGE_TEST_CAPACITY": STORAGE_TEST_CAPACITY,
            "SSH_HOME_CAPACITY": SSH_HOME_CAPACITY,
        },
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
        {
            "NAMESPACE": config.namespace,
            "STORAGE_TEST_CAPACITY": STORAGE_TEST_CAPACITY,
            "SSH_HOME_CAPACITY": SSH_HOME_CAPACITY,
        },
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
    _build_owned_image(
        runner,
        config,
        SSH_IMAGE,
        _resource_path("ssh-image.Dockerfile"),
        SSH_BASE_IMAGE,
        ("--build-arg", f"BASE_IMAGE={SSH_BASE_IMAGE}"),
    )
    nodes = _kind_containers(runner, config, running_only=True)
    _load_image_into_nodes(runner, SSH_IMAGE, nodes)
    _ensure_file_secret(
        runner,
        config,
        "storage-ssh-identity",
        {"id_ed25519": private_key, "authorized_keys": public_key},
    )
    _ensure_ssh_home_mode(
        runner,
        config,
        CANONICAL_HOME_MODE,
        run_id="setup",
        scenario_id="setup-preflight",
    )


def _ssh_home_volume(mode: str) -> str:
    """Return the manifest fragment for one supported SSH home mode."""
    if mode == SHARED_HOME_MODE:
        return "persistentVolumeClaim:\n            claimName: ssh-home-rwx"
    if mode == CANONICAL_HOME_MODE:
        return "emptyDir:\n            sizeLimit: 64Mi"
    raise ProvisionError(f"unsupported SSH home mode: {mode}")


def _render_ssh_workers(config: Config, mode: str) -> tuple[Path, str]:
    """Render one canonical SSH StatefulSet form and return its checksum."""
    source = _resource_path("manifests/ssh-workers.yaml.tmpl")
    rendered = source.read_text(encoding="utf-8")
    replacements = {
        "NAMESPACE": config.namespace,
        "SSH_HOME_MODE": mode,
        "SSH_HOME_VOLUME": _ssh_home_volume(mode),
    }
    for token, value in replacements.items():
        rendered = rendered.replace(f"@@{token}@@", value)
    checksum = statefulset_checksum(rendered.replace("@@SSH_CONFIG_CHECKSUM@@", ""))
    rendered = rendered.replace("@@SSH_CONFIG_CHECKSUM@@", checksum)
    unresolved = [word for word in rendered.split() if "@@" in word]
    if unresolved:
        raise ProvisionError(
            f"unresolved SSH manifest token in {source}: {unresolved[0]}"
        )
    destination = config.manifests_dir / f"ssh-workers-{mode}.yaml"
    _write_text(destination, rendered)
    return destination, checksum


def _inspect_ssh_home_pool(runner: Runner, config: Config) -> PoolObservation:
    """Return the live SSH StatefulSet form and rollout state."""
    statefulset = runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "get",
            "statefulset/ssh-worker",
            "-o",
            "json",
        ),
        check=False,
        timeout=30,
    )
    if statefulset.returncode:
        return PoolObservation("absent", "", 0, 0, statefulset.stderr.strip())
    document = json.loads(statefulset.stdout)
    metadata = document.get("metadata", {})
    annotations = metadata.get("annotations", {})
    status = document.get("status", {})
    generation = metadata.get("generation")
    observed_generation = status.get("observedGeneration")
    revision_ready = (
        isinstance(generation, int)
        and isinstance(observed_generation, int)
        and observed_generation >= generation
        and status.get("currentRevision") == status.get("updateRevision")
        and status.get("updatedReplicas", 0) == 2
        and status.get("readyReplicas", 0) == 2
    )
    pods = _ssh_pods(runner, config)
    terminating = sum(
        bool(pod.get("metadata", {}).get("deletionTimestamp")) for pod in pods
    )
    ready = sum(
        _pod_ready(pod) and not pod.get("metadata", {}).get("deletionTimestamp")
        for pod in pods
    )
    names = ", ".join(str(pod.get("metadata", {}).get("name", "?")) for pod in pods)
    details = (
        f"pods=[{names}], generation={generation}, "
        f"observedGeneration={observed_generation}, "
        f"currentRevision={status.get('currentRevision')}, "
        f"updateRevision={status.get('updateRevision')}"
    )
    return PoolObservation(
        str(annotations.get(SSH_HOME_ANNOTATION, "unknown")),
        str(annotations.get(SSH_CONFIG_ANNOTATION, "")),
        ready if revision_ready else 0,
        terminating,
        details,
    )


def _pod_ready(pod: dict[str, object]) -> bool:
    """Return whether every container in a running pod is ready."""
    status = pod.get("status", {})
    if not isinstance(status, dict) or status.get("phase") != "Running":
        return False
    containers = status.get("containerStatuses", [])
    return bool(containers) and all(
        isinstance(item, dict) and item.get("ready") for item in containers
    )


def _wait_for_no_ssh_pods(runner: Runner, config: Config) -> None:
    """Wait until the host-network SSH port has no owning pod."""
    deadline = time.monotonic() + 180
    pods: list[dict[str, object]] = []
    while True:
        request_timeout, process_timeout = _poll_timeouts(deadline, 10)
        if process_timeout <= 0:
            break
        pods = _ssh_pods(
            runner,
            config,
            request_timeout,
            process_timeout=process_timeout,
        )
        if not pods:
            return
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    names = [str(pod["metadata"]["name"]) for pod in pods]
    raise ProvisionError(f"SSH pods did not terminate before transition: {names}")


def _reconcile_ssh_home_pool(
    runner: Runner, config: Config, mode: str, expected_checksum: str
) -> None:
    """Replace only the SSH StatefulSet with one validated home mode."""
    manifest, checksum = _render_ssh_workers(config, mode)
    if checksum != expected_checksum:
        raise ProvisionError(
            "SSH StatefulSet checksum changed during reconciliation: "
            f"expected {expected_checksum}, rendered {checksum}"
        )
    existing = runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "get",
            "statefulset/ssh-worker",
        ),
        check=False,
        timeout=30,
    )
    if existing.returncode == 0:
        _scale_ssh(runner, config, replicas=0)
        _wait_for_no_ssh_pods(runner, config)
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
    _scale_ssh(runner, config, replicas=2)
    _wait_for_ssh(runner, config)
    _validate_ssh_workers(runner, config, config.keys_dir / "id_ed25519", mode)


def _ssh_transition_manager(runner: Runner, config: Config) -> SshHomeTransitionManager:
    """Return a transition manager backed by the live Kubernetes fixture."""
    return SshHomeTransitionManager(
        config.state_dir,
        lambda: _inspect_ssh_home_pool(runner, config),
        lambda mode, checksum: _reconcile_ssh_home_pool(runner, config, mode, checksum),
    )


def _ensure_ssh_home_mode(
    runner: Runner,
    config: Config,
    mode: str,
    *,
    run_id: str,
    scenario_id: str,
) -> None:
    """Enter a validated SSH home mode through persistent transition state."""
    separate_checksum = _render_ssh_workers(config, CANONICAL_HOME_MODE)[1]
    shared_checksum = _render_ssh_workers(config, SHARED_HOME_MODE)[1]
    manager = _ssh_transition_manager(runner, config)
    if mode == CANONICAL_HOME_MODE:
        manager.preflight(separate_checksum, run_id=run_id, scenario_id=scenario_id)
        return
    if mode == SHARED_HOME_MODE:
        manager.enter_shared(
            run_id=run_id,
            scenario_id=scenario_id,
            separate_checksum=separate_checksum,
            shared_checksum=shared_checksum,
        )
        return
    raise ProvisionError(f"unsupported SSH home mode: {mode}")


def _restore_ssh_home_mode(
    runner: Runner, config: Config, *, run_id: str, scenario_id: str
) -> None:
    """Restore canonical SSH homes after a shared-home scenario batch."""
    checksum = _render_ssh_workers(config, CANONICAL_HOME_MODE)[1]
    _ssh_transition_manager(runner, config).restore_separate(
        checksum, run_id=run_id, scenario_id=scenario_id
    )


def _ssh_pods(
    runner: Runner,
    config: Config,
    request_timeout: int = 10,
    *,
    process_timeout: float | None = None,
) -> list[dict[str, object]]:
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
            f"--request-timeout={request_timeout}s",
        ),
        timeout=process_timeout or request_timeout + 5,
    )
    return sorted(
        json.loads(result.stdout)["items"], key=lambda item: item["metadata"]["name"]
    )


def _validate_ssh_workers(
    runner: Runner, config: Config, private_key: Path, home_mode: str
) -> None:
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
    _validate_ssh_storage(runner, config, pods, home_mode)


def _pod_exec(
    runner: Runner,
    config: Config,
    pod: str,
    script: str,
    *,
    check: bool = True,
    timeout: float = 60,
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
        timeout=timeout,
    )


def _validate_ssh_storage(
    runner: Runner,
    config: Config,
    pods: list[dict[str, object]],
    home_mode: str,
) -> None:
    """Validate the selected home mode and shared storage claim."""
    names = [str(pod["metadata"]["name"]) for pod in pods]
    token = secrets.token_hex(16)
    home_probe_path = f"/home/tester/.integration-home-probe-{token}"
    storage_probe_path = f"/mnt/storage-test/.integration-rwx-probe-{token}"
    storage_check = f'test "$(cat {shlex.quote(storage_probe_path)})" = ' + shlex.quote(
        token
    )
    backend = _select_storage_backend(config)
    if backend == "nfs":
        storage_check += (
            " && case $(stat -f -c %T /mnt/storage-test) in "
            "nfs|nfs4) true;; *) false;; esac"
        )
    expected_home_rc = 0 if home_mode == "shared" else 1
    cleanup = f"rm -f {shlex.quote(home_probe_path)} {shlex.quote(storage_probe_path)}"
    host_visible = backend != "sbx-shared"
    home_rc: int | None = None
    rwx_rc: int | None = None
    try:
        _pod_exec(
            runner,
            config,
            names[0],
            f"touch {shlex.quote(home_probe_path)}; "
            f"printf '%s\\n' {shlex.quote(token)} "
            f">{shlex.quote(storage_probe_path)}",
        )
        deadline = time.monotonic() + SSH_STORAGE_VISIBILITY_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            home_probe = _pod_exec(
                runner,
                config,
                names[1],
                f"test -e {shlex.quote(home_probe_path)}",
                check=False,
                timeout=min(10, remaining),
            )
            home_rc = home_probe.returncode
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            rwx_probe = _pod_exec(
                runner,
                config,
                names[1],
                storage_check,
                check=False,
                timeout=min(10, remaining),
            )
            rwx_rc = rwx_probe.returncode
            if backend == "sbx-shared":
                host_probe = (
                    config.sbx_shared_root
                    / "storage-test"
                    / Path(storage_probe_path).name
                )
                host_visible = (
                    host_probe.is_file()
                    and host_probe.read_text(encoding="utf-8").strip() == token
                )
            if (
                home_probe.returncode == expected_home_rc
                and rwx_probe.returncode == 0
                and host_visible
            ):
                return
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise ProvisionError(
            "SSH home or RWX visibility did not converge: "
            f"home_rc={home_rc}, expected_home_rc={expected_home_rc}, "
            f"rwx_rc={rwx_rc}, host_visible={host_visible}"
        )
    finally:
        for name in names:
            _pod_exec(runner, config, name, cleanup, check=False)


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
    _prepare_slurm_images(runner, config)
    _ensure_mariadb_secret(runner, config)
    mariadb = _render_resource(
        config,
        "manifests/mariadb-accounting.yaml.tmpl",
        {
            "MARIADB_IMAGE": MARIADB_IMAGE,
            "NAMESPACE": config.namespace,
        },
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
    _ensure_slurm_workload_account(runner, config)
    _validate_slurm(runner, config)


def _build_slinky_image(
    runner: Runner,
    config: Config,
    *,
    image: str,
    base_image: str,
    dockerfile: str,
    nodes: list[str],
) -> None:
    """Build, record, and preload one fixture-owned Slinky image."""
    _build_owned_image(
        runner,
        config,
        image,
        _resource_path(dockerfile),
        base_image,
        (
            "--build-arg",
            f"BASE_IMAGE={base_image}",
        ),
    )
    _load_image_into_nodes(runner, image, nodes)


def _stage_pinned_fixture_image(
    runner: Runner,
    config: Config,
    *,
    upstream: str,
    image: str,
    nodes: list[str],
) -> None:
    """Acquire, transactionally tag, and preload one pinned workload image."""
    selected = _ensure_pinned_image(runner, upstream)
    temporary_tag = _begin_image_build(runner, config, image)
    runner.run(["docker", "image", "tag", selected, temporary_tag])
    _publish_image_build(runner, config, image, temporary_tag)
    _load_image_into_nodes(runner, image, nodes)


def _stage_pinned_node_alias(
    runner: Runner,
    upstream: str,
    destination: str,
    nodes: list[str],
    component: str,
) -> None:
    """Import one verified image under a node-local fixture alias."""
    selected = _ensure_pinned_image(runner, upstream)
    host_prefix = f"storage-scale-test-integration/{component}-import:"
    node_prefix = f"docker.io/{host_prefix}"
    _remove_stale_host_image_aliases(runner, host_prefix)
    _remove_stale_node_image_aliases(runner, nodes, node_prefix)
    temporary = f"{host_prefix}{secrets.token_hex(6)}"
    runner.run(["docker", "image", "tag", selected, temporary])
    try:
        _load_image_into_nodes(
            runner,
            temporary,
            nodes,
            destination=destination,
        )
    finally:
        runner.run(["docker", "image", "rm", temporary], check=False)


def _prepare_fixture_node_image(
    runner: Runner, config: Config, fixture_image: Any
) -> None:
    """Load one architecture-verified image under a fixture-private node alias."""
    nodes = _kind_containers(runner, config, running_only=True)
    if len(nodes) != 3:
        raise ProvisionError(
            f"expected three running kind nodes before image import; found {nodes}"
        )
    _stage_pinned_node_alias(
        runner,
        fixture_image.upstream,
        fixture_image.fixture,
        nodes,
        fixture_image.component,
    )


def _prepare_kubectl_prerequisite_image(runner: Runner, config: Config) -> None:
    """Load the verified Elbencho image under its fixture-private node alias."""
    _prepare_fixture_node_image(runner, config, ELBENCHO_FIXTURE)


def _ensure_calico_manifest(runner: Runner, config: Config) -> Path:
    """Return the checksum-verified Calico manifest cached in fixture state."""
    source = config.state_dir / "downloads" / f"calico-{CALICO_VERSION}.yaml"
    if source.exists():
        _verify_sha256(source, CALICO_MANIFEST_SHA256)
        return source
    source.parent.mkdir(parents=True, exist_ok=True)
    temporary = source.with_suffix(".yaml.new")
    temporary.unlink(missing_ok=True)
    try:
        _curl(runner, CALICO_MANIFEST_URL, temporary)
        _verify_sha256(temporary, CALICO_MANIFEST_SHA256)
        temporary.replace(source)
    finally:
        temporary.unlink(missing_ok=True)
    return source


def _render_calico_manifest(runner: Runner, config: Config) -> Path:
    """Render Calico with only preloaded fixture-private image aliases."""
    source = _ensure_calico_manifest(runner, config)
    text = source.read_text(encoding="utf-8")
    for component in dict(CALICO_IMAGES):
        upstream_tag = f"quay.io/calico/{component}:{CALICO_VERSION}"
        destination = _calico_image(component)[1]
        expected = 2 if component in {"cni", "node"} else 1
        if text.count(upstream_tag) != expected:
            raise ProvisionError(
                f"Calico manifest image count changed for {upstream_tag}"
            )
        text = text.replace(upstream_tag, destination)
    if "quay.io/calico/" in text:
        raise ProvisionError("Calico manifest retains an unstaged workload image")
    calico_node_env = """          env:
            # Use Kubernetes API as the backing datastore.
"""
    explicit_iptables_env = """          env:
            # Docker SBX cannot provide the kernel facilities for Calico eBPF mode.
            - name: FELIX_BPFENABLED
              value: "false"
            # Use Kubernetes API as the backing datastore.
"""
    if text.count(calico_node_env) != 1:
        raise ProvisionError("Calico manifest node environment changed")
    text = text.replace(calico_node_env, explicit_iptables_env)
    # Docker SBX exposes the kind node's sysfs read-only. The upstream mount is
    # used only to let Felix distinguish a confidential kernel lockdown before
    # selecting eBPF programs; in this iptables profile Felix documents a
    # missing/unreadable lockdown file as not locked down. Removing only this
    # optional mount avoids asking the nested runtime to create a read-only
    # hostPath while retaining every networking and policy volume.
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
    for fragment in (securityfs_mount, securityfs_volume):
        if text.count(fragment) != 1:
            raise ProvisionError(
                "Calico manifest securityfs compatibility fragment changed"
            )
        text = text.replace(fragment, "")
    text = text.replace("imagePullPolicy: IfNotPresent", "imagePullPolicy: Never")
    destination = config.manifests_dir / f"calico-{CALICO_VERSION}.yaml"
    _write_text(destination, text)
    return destination


def _install_sbx_calico(runner: Runner, config: Config) -> None:
    """Install the pinned iptables Calico profile required by Docker SBX."""
    existing = runner.run(
        _kubectl(
            config,
            "-n",
            "kube-system",
            "get",
            "daemonset/calico-node",
            "--ignore-not-found",
            "-o",
            "name",
        ),
        check=False,
        timeout=30,
    )
    if existing.returncode == 0 and existing.stdout.strip():
        _validate_calico_profile(runner, config)
        return
    nodes = _kind_containers(runner, config, running_only=True)
    if len(nodes) != 3:
        raise ProvisionError(
            f"expected three running kind nodes before Calico import; found {nodes}"
        )
    for component in dict(CALICO_IMAGES):
        upstream, destination = _calico_image(component)
        _stage_pinned_node_alias(
            runner, upstream, destination, nodes, f"calico-{component}"
        )
    manifest = _render_calico_manifest(runner, config)
    runner.run(_kubectl(config, "apply", "-f", manifest), timeout=180)
    runner.run(
        _kubectl(
            config,
            "-n",
            "kube-system",
            "rollout",
            "status",
            "daemonset/calico-node",
            "--timeout=300s",
        ),
        timeout=330,
    )
    runner.run(
        _kubectl(
            config,
            "-n",
            "kube-system",
            "rollout",
            "status",
            "deployment/calico-kube-controllers",
            "--timeout=300s",
        ),
        timeout=330,
    )


def _kubectl_probe_exec(
    runner: Runner,
    config: Config,
    pod: str,
    container: str,
    arguments: Sequence[str],
    *,
    check: bool = True,
    timeout: float = 30,
) -> subprocess.CompletedProcess[str]:
    """Execute one bounded prerequisite-probe command."""
    return runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "exec",
            pod,
            "-c",
            container,
            "--",
            *arguments,
        ),
        check=check,
        timeout=timeout,
    )


def _kubectl_probe_status(
    runner: Runner,
    config: Config,
    pod: str,
    container: str,
    address: str,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Query one Elbencho service directly by numeric Pod IPv4 address."""
    ipaddress.IPv4Address(address)
    script = (
        'response=$(exec 3<>/dev/tcp/"$1"/1611; '
        "printf 'GET /status HTTP/1.0\\r\\nHost: %s:1611\\r\\n\\r\\n' "
        '"$1" >&3; cat <&3); exec 3>&-; [[ "$response" == *"200"* ]]'
    )
    return _kubectl_probe_exec(
        runner,
        config,
        pod,
        container,
        ("timeout", "5s", "bash", "-ceu", script, "bash", address),
        check=check,
        timeout=15,
    )


def _wait_for_kubectl_probe_denial(
    runner: Runner,
    config: Config,
    coordinator: str,
    denied: str,
    address: str,
) -> None:
    """Wait for policy propagation while continuously proving the allowed path."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        _kubectl_probe_status(runner, config, coordinator, "coordinator", address)
        denied_result = _kubectl_probe_status(
            runner,
            config,
            denied,
            "denied",
            address,
            check=False,
        )
        if denied_result.returncode:
            _kubectl_probe_status(runner, config, coordinator, "coordinator", address)
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise ProvisionError(
        "the policy CNI did not enforce worker ingress isolation from the unrelated "
        f"probe Pod for {address}"
    )


def _wait_for_kubectl_probe_access(
    runner: Runner,
    config: Config,
    pod: str,
    container: str,
    address: str,
) -> None:
    """Wait for one pre-policy cross-node path to become usable."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = _kubectl_probe_status(
            runner, config, pod, container, address, check=False
        )
        if result.returncode == 0:
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise ProvisionError(
        f"kubectl prerequisite Pod {pod} could not reach worker {address} "
        "before NetworkPolicy creation"
    )


def _kubectl_probe_pods(runner: Runner, config: Config) -> list[dict[str, object]]:
    """Return all nonterminating prerequisite-probe Pods."""
    result = runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "get",
            "pods",
            "-l",
            f"app.kubernetes.io/name={KUBECTL_PROBE_NAME}",
            "-o",
            "json",
        ),
        timeout=30,
    )
    return [
        pod
        for pod in json.loads(result.stdout).get("items", [])
        if not pod.get("metadata", {}).get("deletionTimestamp")
    ]


def _validate_kubectl_probe_inventory(
    runner: Runner, config: Config
) -> tuple[list[dict[str, object]], dict[str, object], dict[str, object]]:
    """Validate probe placement, readiness, identity, and Pod IPv4 addresses."""
    pods = _kubectl_probe_pods(runner, config)
    workers = [
        pod
        for pod in pods
        if pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        == "worker"
    ]
    clients = {
        str(
            pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        ): pod
        for pod in pods
        if pod.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")
        in {"coordinator", "denied"}
    }
    target_output = runner.run(
        _kubectl(
            config,
            "get",
            "nodes",
            "-l",
            TARGET_LABEL,
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ),
        timeout=30,
    ).stdout
    target_nodes = set(target_output.split())
    worker_nodes = {str(pod.get("spec", {}).get("nodeName")) for pod in workers}
    login_node = f"{config.cluster_name}-control-plane"
    invalid = len(workers) != 2 or set(clients) != {"coordinator", "denied"}
    invalid = invalid or target_nodes != worker_nodes or len(target_nodes) != 2
    invalid = invalid or any(
        pod.get("spec", {}).get("nodeName") != login_node for pod in clients.values()
    )
    invalid = invalid or any(not _pod_is_ready(pod) for pod in pods)
    if invalid:
        raise ProvisionError(
            "kubectl prerequisite probe placement or readiness is invalid: "
            f"pods={len(pods)}, workers={sorted(worker_nodes)}, "
            f"targets={sorted(target_nodes)}, clients={sorted(clients)}"
        )
    addresses = [str(pod.get("status", {}).get("podIP", "")) for pod in workers]
    try:
        parsed_addresses = {ipaddress.IPv4Address(address) for address in addresses}
    except ipaddress.AddressValueError as error:
        raise ProvisionError(
            f"kubectl prerequisite workers lack valid Pod IPv4 addresses: {addresses}"
        ) from error
    if len(parsed_addresses) != 2:
        raise ProvisionError(
            f"kubectl prerequisite workers lack distinct Pod IPv4 addresses: {addresses}"
        )
    image_ids = {
        str(status.get("imageID"))
        for pod in workers
        for status in pod.get("status", {}).get("containerStatuses", [])
    }
    if len(image_ids) != 1 or image_ids == {"None"}:
        raise ProvisionError(
            f"kubectl prerequisite workers do not share one image ID: {image_ids}"
        )
    return workers, clients["coordinator"], clients["denied"]


def _verify_kubectl_probe_storage(
    runner: Runner,
    config: Config,
    workers: Sequence[dict[str, object]],
    coordinator: dict[str, object],
    token: str,
) -> None:
    """Prove bidirectional shared-PVC visibility under the workload identity."""
    coordinator_name = str(coordinator["metadata"]["name"])  # type: ignore[index]
    worker_nodes = sorted(str(pod["spec"]["nodeName"]) for pod in workers)  # type: ignore[index]
    expected = " ".join(shlex.quote(node) for node in worker_nodes)
    probe_path = f"{KUBECTL_PROBE_STORAGE}/{token}"
    script = (
        f"deadline=$((SECONDS + 30)); for node in {expected}; do "
        f"expected={shlex.quote(token)}' '$node; "
        f"while [[ $(cat {shlex.quote(probe_path)}/$node 2>/dev/null || true) "
        '!= "$expected" ]]; do (( SECONDS < deadline )) || exit 1; '
        "sleep 1; done; done; "
        f"printf '%s\\n' {shlex.quote(token)} >"
        f"{shlex.quote(probe_path)}/coordinator"
    )
    _kubectl_probe_exec(
        runner,
        config,
        coordinator_name,
        "coordinator",
        ("bash", "-ceu", script),
        timeout=45,
    )
    for worker in workers:
        _kubectl_probe_exec(
            runner,
            config,
            str(worker["metadata"]["name"]),  # type: ignore[index]
            "elbencho",
            (
                "bash",
                "-ceu",
                f"[[ $(cat {shlex.quote(probe_path)}/coordinator) == "
                f"{shlex.quote(token)} ]]",
            ),
        )


def _cleanup_kubectl_prerequisite_probe(
    runner: Runner, config: Config, manifests: Sequence[Path]
) -> list[str]:
    """Attempt all exact probe cleanup and return secondary diagnostics."""
    failures: list[str] = []
    try:
        candidates = []
        for item in _kubectl_probe_pods(runner, config):
            component = (
                item.get("metadata", {})
                .get("labels", {})
                .get("app.kubernetes.io/component")
            )
            if component == "coordinator":
                candidates.append(
                    (str(item.get("metadata", {}).get("name")), "coordinator")
                )
            elif component == "worker":
                candidates.append(
                    (str(item.get("metadata", {}).get("name")), "elbencho")
                )
    except (ProvisionError, subprocess.SubprocessError, OSError, json.JSONDecodeError):
        candidates = []
        failures.append("probe Pod discovery for storage cleanup failed")
    for candidate, container in candidates:
        try:
            result = _kubectl_probe_exec(
                runner,
                config,
                candidate,
                container,
                ("rm", "-rf", "--", KUBECTL_PROBE_STORAGE),
                check=False,
            )
            if result.returncode:
                failures.append(f"storage cleanup through {candidate} failed")
        except (ProvisionError, subprocess.SubprocessError, OSError) as error:
            failures.append(f"storage cleanup through {candidate} failed: {error}")
    for manifest in manifests:
        deletion = runner.run(
            _kubectl(
                config,
                "delete",
                "-f",
                manifest,
                "--ignore-not-found=true",
                "--wait=true",
                "--timeout=90s",
            ),
            check=False,
            timeout=120,
        )
        if deletion.returncode:
            failures.append(f"probe resource deletion failed for {manifest.name}")
    return failures


def _validate_kubectl_prerequisites(
    runner: Runner, config: Config, backend: str
) -> None:
    """Prove the retained fixture can support the planned kubectl substrate."""
    cni_profile = _validate_network_policy_profile(runner, config, backend)
    _prepare_kubectl_prerequisite_image(runner, config)
    token = secrets.token_hex(16)
    workload_manifest = _render_resource(
        config,
        "manifests/kubectl-prerequisite-probe.yaml.tmpl",
        {
            "NAMESPACE": config.namespace,
            "ELBENCHO_IMAGE": ELBENCHO_FIXTURE_IMAGE,
            "PROBE_TOKEN": token,
        },
    )
    policy_manifest = _render_resource(
        config,
        "manifests/kubectl-prerequisite-policy.yaml.tmpl",
        {"NAMESPACE": config.namespace},
    )
    manifests = (policy_manifest, workload_manifest)
    cleanup_failures = _cleanup_kubectl_prerequisite_probe(runner, config, manifests)
    if cleanup_failures:
        raise ProvisionError(
            "stale kubectl prerequisite probe cleanup failed: "
            + "; ".join(cleanup_failures)
        )
    primary_error: BaseException | None = None
    try:
        runner.run(_kubectl(config, "create", "-f", workload_manifest), timeout=60)
        runner.run(
            _kubectl(
                config,
                "-n",
                config.namespace,
                "rollout",
                "status",
                f"daemonset/{KUBECTL_PROBE_WORKERS}",
                "--timeout=180s",
            ),
            timeout=210,
        )
        runner.run(
            _kubectl(
                config,
                "-n",
                config.namespace,
                "wait",
                "--for=condition=Ready",
                f"pod/{KUBECTL_PROBE_COORDINATOR}",
                f"pod/{KUBECTL_PROBE_DENIED}",
                "--timeout=180s",
            ),
            timeout=210,
        )
        workers, coordinator, denied = _validate_kubectl_probe_inventory(runner, config)
        coordinator_name = str(coordinator["metadata"]["name"])  # type: ignore[index]
        denied_name = str(denied["metadata"]["name"])  # type: ignore[index]
        # Establish the negative probe's working cross-node path before policy
        # exists; otherwise an unrelated network failure could look like
        # successful isolation.
        for worker in workers:
            address = str(worker["status"]["podIP"])  # type: ignore[index]
            _wait_for_kubectl_probe_access(
                runner, config, coordinator_name, "coordinator", address
            )
            _wait_for_kubectl_probe_access(
                runner, config, denied_name, "denied", address
            )
        runner.run(_kubectl(config, "create", "-f", policy_manifest), timeout=60)
        for worker in workers:
            address = str(worker["status"]["podIP"])  # type: ignore[index]
            _wait_for_kubectl_probe_denial(
                runner, config, coordinator_name, denied_name, address
            )
        _verify_kubectl_probe_storage(runner, config, workers, coordinator, token)
        LOG.info(
            "Validated kubectl prerequisites with %s and direct Pod networking",
            cni_profile,
        )
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_failures = _cleanup_kubectl_prerequisite_probe(
            runner, config, manifests
        )
        if cleanup_failures:
            detail = "; ".join(cleanup_failures)
            if primary_error is None:
                raise ProvisionError(
                    f"kubectl prerequisite probe cleanup failed: {detail}"
                )
            LOG.error("Secondary kubectl prerequisite cleanup failure: %s", detail)


def _prepare_slurm_images(runner: Runner, config: Config) -> None:
    """Build or acquire every image that the Slurm fixture runs in kind."""
    control_plane = f"{config.cluster_name}-control-plane"
    worker_nodes = [
        f"{config.cluster_name}-worker",
        f"{config.cluster_name}-worker2",
    ]
    all_nodes = [control_plane, *worker_nodes]
    _stage_pinned_fixture_image(
        runner,
        config,
        upstream=MARIADB_BASE_IMAGE,
        image=MARIADB_IMAGE,
        nodes=[control_plane],
    )
    _stage_pinned_fixture_image(
        runner,
        config,
        upstream=SLINKY_HELPER_BASE_IMAGE,
        image=SLINKY_HELPER_IMAGE,
        nodes=all_nodes,
    )
    _build_slinky_image(
        runner,
        config,
        image=SLINKY_LOGIN_IMAGE,
        base_image=SLINKY_LOGIN_BASE_IMAGE,
        dockerfile="slinky-login-image.Dockerfile",
        nodes=[control_plane],
    )
    _build_slinky_image(
        runner,
        config,
        image=SLINKY_SLURMD_IMAGE,
        base_image=SLINKY_SLURMD_BASE_IMAGE,
        dockerfile="slinky-slurmd-image.Dockerfile",
        nodes=worker_nodes,
    )


def _helm_slinky(runner: Runner, config: Config) -> None:
    """Reconcile the three pinned Slinky releases."""
    slurm_values = _render_resource(
        config,
        "manifests/slinky-slurm-values.yaml.tmpl",
        {
            "SLINKY_HELPER_IMAGE_REPOSITORY": SLINKY_HELPER_IMAGE_REPOSITORY,
            "SLINKY_HELPER_IMAGE_TAG": SLINKY_HELPER_IMAGE_TAG,
        },
    )
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
            slurm_values,
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
    ready = {container: 0 for container in expected}
    deadline = time.monotonic() + 300
    while True:
        request_timeout, process_timeout = _poll_timeouts(deadline, 10)
        if process_timeout <= 0:
            break
        pods = _namespace_pods(
            runner,
            config,
            request_timeout,
            process_timeout=process_timeout,
        )
        ready = {
            container: sum(
                _pod_is_ready(pod) for pod in _pods_with_container(pods, container)
            )
            for container in expected
        }
        if ready == expected:
            return
        LOG.info("Waiting for Slinky child pods: ready=%s expected=%s", ready, expected)
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise ProvisionError(f"Slinky child pods did not become ready: {ready}")


def _namespace_pods(
    runner: Runner,
    config: Config,
    request_timeout: int = 10,
    *,
    process_timeout: float | None = None,
) -> list[dict[str, object]]:
    """Return all pod objects in the integration namespace."""
    result = runner.run(
        _kubectl(
            config,
            "-n",
            config.namespace,
            "get",
            "pods",
            "-o",
            "json",
            f"--request-timeout={request_timeout}s",
        ),
        timeout=process_timeout or request_timeout + 5,
    )
    return json.loads(result.stdout)["items"]


def _pods_with_container(
    pods: list[dict[str, object]], container: str
) -> list[dict[str, object]]:
    """Select nonterminating pods containing a Slinky workload container."""
    return [
        pod
        for pod in pods
        if not pod["metadata"].get("deletionTimestamp")  # type: ignore[index]
        and container
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
    while True:
        request_timeout, process_timeout = _poll_timeouts(deadline, 10)
        if process_timeout <= 0:
            break
        pods = [
            pod
            for pod in _pods_with_container(
                _namespace_pods(
                    runner,
                    config,
                    request_timeout,
                    process_timeout=process_timeout,
                ),
                "login",
            )
            if not pod["metadata"].get("deletionTimestamp")  # type: ignore[index]
            and _pod_is_ready(pod)
        ]
        if len(pods) == 1:
            return str(pods[0]["metadata"]["name"])  # type: ignore[index]
        LOG.info(
            "Waiting for one ready, nonterminating LoginSet pod; found %s", len(pods)
        )
        time.sleep(min(3, max(0, deadline - time.monotonic())))
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


def _sacctmgr_rows(
    runner: Runner,
    prefix: list[str | Path],
    entity: str,
    fields: tuple[str, ...],
) -> set[tuple[str, ...]]:
    """Return exact pipe-delimited rows from one Slurm accounting query."""
    result = runner.run(
        [
            *prefix,
            "sacctmgr",
            "--noheader",
            "--parsable2",
            "show",
            entity,
            f"format={','.join(fields)}",
        ]
    )
    return {
        tuple(line.removesuffix("|").split("|"))
        for line in result.stdout.splitlines()
        if line.strip()
    }


def _ensure_slurm_workload_account(runner: Runner, config: Config) -> None:
    """Reconcile the fixed non-root workload identity in Slurm accounting."""
    login = _login_pod(runner, config)
    prefix = _kubectl(config, "-n", config.namespace, "exec", login, "--")
    if (WORKLOAD_ACCOUNT,) not in _sacctmgr_rows(
        runner, prefix, "account", ("Account",)
    ):
        runner.run(
            [
                *prefix,
                "sacctmgr",
                "--immediate",
                "add",
                "account",
                WORKLOAD_ACCOUNT,
                "Description=storage scale integration workloads",
                "Organization=storage-scale-test",
            ]
        )
    expected_association = (WORKLOAD_USER, WORKLOAD_ACCOUNT)
    if expected_association not in _sacctmgr_rows(
        runner, prefix, "association", ("User", "Account")
    ):
        runner.run(
            [
                *prefix,
                "sacctmgr",
                "--immediate",
                "add",
                "user",
                WORKLOAD_USER,
                f"Account={WORKLOAD_ACCOUNT}",
                f"DefaultAccount={WORKLOAD_ACCOUNT}",
            ]
        )
    expected_user = (WORKLOAD_USER, WORKLOAD_ACCOUNT)
    if expected_user not in _sacctmgr_rows(
        runner, prefix, "user", ("User", "DefaultAccount")
    ):
        runner.run(
            [
                *prefix,
                "sacctmgr",
                "--immediate",
                "modify",
                "user",
                "where",
                f"Name={WORKLOAD_USER}",
                "set",
                f"DefaultAccount={WORKLOAD_ACCOUNT}",
            ]
        )
    account_ready = (WORKLOAD_ACCOUNT,) in _sacctmgr_rows(
        runner, prefix, "account", ("Account",)
    )
    association_ready = expected_association in _sacctmgr_rows(
        runner, prefix, "association", ("User", "Account")
    )
    user_ready = expected_user in _sacctmgr_rows(
        runner, prefix, "user", ("User", "DefaultAccount")
    )
    if not account_ready or not association_ready or not user_ready:
        raise ProvisionError(
            "Slurm workload accounting reconciliation did not produce the "
            f"required {WORKLOAD_USER}/{WORKLOAD_ACCOUNT} association"
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
    workload_prefix = [*prefix, "runuser", "-u", WORKLOAD_USER, "--"]
    coordinator_uid = runner.run([*workload_prefix, "id", "-u"]).stdout.strip()
    if coordinator_uid != str(WORKLOAD_UID) or coordinator_uid == "0":
        raise ProvisionError(
            "Slurm coordinator workload identity is invalid: "
            f"expected {WORKLOAD_UID}, found {coordinator_uid!r}"
        )
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
    runner.run([*workload_prefix, "bash", "-c", storage_probe])
    if backend == "sbx-shared":
        host_probe = config.sbx_shared_root / "storage-test" / ".login-probe"
        if (
            not host_probe.is_file()
            or host_probe.read_text(encoding="utf-8").strip() != token
        ):
            raise ProvisionError("LoginSet SBX data is not visible from the agent")
    runner.run([*workload_prefix, "rm", "-f", "/mnt/storage-test/.login-probe"])
    fanout = runner.run(
        [
            *workload_prefix,
            "srun",
            "--account",
            WORKLOAD_ACCOUNT,
            "-p",
            "all",
            "-N2",
            "-n2",
            "--ntasks-per-node=1",
            "sh",
            "-c",
            'printf "%s %s\\n" "$(hostname)" "$(id -u)"',
        ],
        timeout=120,
    ).stdout.splitlines()
    identities = [line.split() for line in fanout if line.strip()]
    hostnames = {fields[0] for fields in identities if len(fields) == 2}
    task_uids = {fields[1] for fields in identities if len(fields) == 2}
    if len(identities) != 2 or len(hostnames) != 2 or task_uids != {str(WORKLOAD_UID)}:
        raise ProvisionError(
            "Slurm fan-out did not reach two distinct nodes as the fixed "
            f"workload identity {WORKLOAD_UID}: {fanout}"
        )
    job = runner.run(
        [
            *workload_prefix,
            "sbatch",
            "--account",
            WORKLOAD_ACCOUNT,
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
            *workload_prefix,
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
    config: Config,
    backend: str,
    kubernetes_version: str,
    subnet: str = "",
    gateway: str = "",
) -> None:
    """Persist non-secret desired state for later diagnostics."""
    state = {
        "schema": STATE_SCHEMA,
        "cluster_name": config.cluster_name,
        "namespace": config.namespace,
        "export_dir": str(config.export_dir),
        "ssh_home_mode": "separate",
        "storage_backend": backend,
        "sbx_shared_root": (
            str(config.sbx_shared_root) if backend == "sbx-shared" else None
        ),
        "test_user": config.test_user,
        "test_uid": config.test_uid,
        "test_gid": config.test_gid,
        "workload_user": WORKLOAD_USER,
        "workload_uid": WORKLOAD_UID,
        "workload_gid": WORKLOAD_GID,
        "workload_account": WORKLOAD_ACCOUNT,
        "kind_version": (SBX_KIND_VERSION if backend == "sbx-shared" else KIND_VERSION),
        "kubectl_version": (
            SBX_KUBECTL_VERSION if backend == "sbx-shared" else KUBECTL_VERSION
        ),
        "kubernetes_version": kubernetes_version,
        "nfs_csi_version": NFS_CSI_VERSION if backend == "nfs" else None,
        "slinky_version": SLINKY_VERSION,
        "kind_subnet": subnet,
        "kind_gateway": gateway,
        "cni": "calico" if backend == "sbx-shared" else "kindnet",
        "cni_version": CALICO_VERSION if backend == "sbx-shared" else None,
        "cni_image": None if backend == "sbx-shared" else KINDNET_IMAGE,
        "kubectl_probe_image": ELBENCHO_FIXTURE_IMAGE,
    }
    _write_text(config.state_dir / "state.json", json.dumps(state, indent=2) + "\n")


def setup_environment(runner: Runner, config: Config) -> None:
    """Provision while serializing host-global NFS ownership."""
    architecture = _check_platform()
    backend = _select_storage_backend(config)
    if backend == "nfs":
        with _nfs_host_lock(runner):
            _claim_nfs_host_owner(runner, config)
            _setup_environment_locked(runner, config, architecture, backend)
        return
    _setup_environment_locked(runner, config, architecture, backend)


def _setup_environment_locked(
    runner: Runner, config: Config, architecture: str, backend: str
) -> None:
    """Idempotently provision after backend ownership is established."""
    capacity_path = (
        config.sbx_shared_root if backend == "sbx-shared" else config.state_dir
    )
    _check_host_capacity(capacity_path)
    _prepare_host_dependencies(runner, config, backend)
    _ensure_docker(runner)
    _check_docker_capacity(runner)
    _ensure_client_tools(runner, config, architecture, backend)
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
        if not _retained_cluster_matches_profile(runner, config, backend):
            _delete_cluster(runner, config)
            _create_cluster(runner, config, backend)
    elif cluster_exists:
        LOG.warning("Replacing incomplete or stopped disposable kind cluster")
        _delete_cluster(runner, config)
        _create_cluster(runner, config, backend)
    else:
        _create_cluster(runner, config, backend)
    if backend == "sbx-shared":
        _configure_sbx_node_trust(runner, config)
        _install_sbx_calico(runner, config)
    _wait_for_cluster(runner, config)
    kubernetes_version = _observed_kubernetes_version(runner, config)
    _record_storage_backend_profile(config, backend)
    subnet = ""
    gateway = ""
    if backend == "nfs":
        subnet, gateway = _kind_ipv4_network(runner)
        _configure_nfs(runner, config, subnet, gateway)
        _probe_nfs(runner, config, gateway)
        _install_nfs_csi(runner, config, gateway)
    else:
        _install_sbx_shared_storage(runner, config)
    _validate_kubectl_prerequisites(runner, config, backend)
    _install_ssh_workers(runner, config)
    _scale_ssh(runner, config, replicas=0)
    _install_slurm(runner, config)
    _scale_ssh(runner, config, replicas=2)
    _wait_for_ssh(runner, config)
    private_key = config.keys_dir / "id_ed25519"
    _validate_ssh_workers(runner, config, private_key, "separate")
    _write_state_summary(config, backend, kubernetes_version, subnet, gateway)
    LOG.info("Integration environment is provisioned and running")


def _verify_user_access(runner: Runner, config: Config) -> None:
    """Verify the current user can use the provisioned cluster and state."""
    probe = config.state_dir / "test-runs" / ".access-probe"
    runner.run(["mkdir", "-p", probe.parent])
    runner.run(["touch", probe])
    runner.run(["rm", "--", probe])
    runner.run(["docker", "info"], timeout=60)
    runner.run(["kind", "get", "clusters"], timeout=30)
    runner.run(
        [
            "kubectl",
            "--kubeconfig",
            config.kubeconfig,
            "get",
            "nodes",
        ],
        timeout=30,
    )
    LOG.info("Verified integration access for current user %s", config.test_user)


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
    """Wait for exactly two ready, nonterminating SSH workers."""
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
    deadline = time.monotonic() + 180
    pods: list[dict[str, object]] = []
    while True:
        request_timeout, process_timeout = _poll_timeouts(deadline, 10)
        if process_timeout <= 0:
            break
        pods = _ssh_pods(
            runner,
            config,
            request_timeout,
            process_timeout=process_timeout,
        )
        ready = [
            pod
            for pod in pods
            if _pod_ready(pod) and not pod.get("metadata", {}).get("deletionTimestamp")
        ]
        terminating = [
            pod for pod in pods if pod.get("metadata", {}).get("deletionTimestamp")
        ]
        if len(ready) == 2 and not terminating and len(pods) == 2:
            return
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    details = [
        {
            "name": pod.get("metadata", {}).get("name"),
            "ready": _pod_ready(pod),
            "terminating": bool(pod.get("metadata", {}).get("deletionTimestamp")),
        }
        for pod in pods
    ]
    raise ProvisionError(f"SSH worker rollout did not become healthy: {details}")


def _stop_environment_locked(runner: Runner, config: Config, backend: str) -> None:
    """Delete the disposable cluster and stop owned host services."""
    if (config.state_dir / "cluster-owner.json").exists():
        clusters = _kind_clusters(runner)
        containers = _kind_containers(runner, config, running_only=False)
        cluster_exists = config.cluster_name in clusters or bool(containers)
        if cluster_exists:
            _ensure_cluster_ownership(config, cluster_exists=True)
            _delete_cluster(runner, config)
        else:
            LOG.info(
                "Disposable kind cluster %s is already absent", config.cluster_name
            )
    else:
        LOG.info("No owned disposable kind cluster was established")
    if backend == "nfs":
        _stop_owned_nfs(runner, config)
    LOG.info(
        "Integration environment stopped; host packages, caches, keys, and %s "
        "data were preserved",
        "NFS" if backend == "nfs" else "SBX shared",
    )


def stop_environment(runner: Runner, config: Config) -> None:
    """Stop the fixture while serializing host-global NFS state."""
    backend = _select_storage_backend(config)
    if backend == "nfs":
        with _nfs_host_lock(runner):
            host_owned = _validate_nfs_host_owner(runner, config)
            if (config.state_dir / "nfs-service.json").exists() and not host_owned:
                raise ProvisionError(
                    "refusing to alter NFS service without the host-global owner record"
                )
            _stop_environment_locked(runner, config, backend)
        return
    _stop_environment_locked(runner, config, backend)


def teardown_environment(runner: Runner, config: Config) -> None:
    """Remove the fixture while serializing host-global NFS state."""
    backend = _select_storage_backend(config)
    if backend == "nfs":
        with _nfs_host_lock(runner):
            _teardown_environment_locked(runner, config, backend)
        return
    _teardown_environment_locked(runner, config, backend)


def _teardown_environment_locked(runner: Runner, config: Config, backend: str) -> None:
    """Stop and remove all validated harness-owned resources."""
    if backend == "sbx-shared":
        state_owned, setup_owned, shared_owned = _validate_sbx_teardown_ownership(
            runner, config
        )
        _stop_environment_locked(runner, config, backend)
        if setup_owned:
            _remove_harness_images(runner, config)
        if shared_owned:
            _remove_sbx_shared(runner, config)
        _remove_owned_directories(
            runner, config, remove_state=state_owned, remove_export=False
        )
        LOG.info(
            "Docker SBX integration environment torn down; installed host "
            "packages were preserved"
        )
        return
    state_owned, setup_owned, export_owned, nfs_configured = (
        _validate_teardown_ownership(runner, config)
    )
    host_owned = _validate_nfs_host_owner(runner, config)
    _stop_environment_locked(runner, config, backend)
    if nfs_configured:
        _remove_nfs_configuration(runner, config)
    if export_owned:
        _unmount_export_filesystem(runner, config)
    if setup_owned:
        _remove_harness_images(runner, config)
    _remove_owned_directories(
        runner, config, remove_state=state_owned, remove_export=export_owned
    )
    if host_owned:
        runner.run([*_sudo_prefix(), "rm", "--force", "--", NFS_HOST_OWNER])
    LOG.info(
        "Integration environment torn down; installed host packages were preserved"
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
    _ensure_pinned_image(runner, SBX_KIND_NODE_IMAGE)
    runner.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "--mount",
            f"type=bind,src={root},dst=/owned",
            SBX_KIND_NODE_IMAGE,
            "-ec",
            f"find /owned -mindepth 1 -xdev -depth ! -name {EXPORT_MARKER} -delete",
        ],
        timeout=120,
    )
    (root / EXPORT_MARKER).unlink()
    root.rmdir()
    if root.exists():
        raise ProvisionError(f"cleanup did not remove SBX shared root: {root}")


def _owner_document(config: Config) -> dict[str, object]:
    """Return the exact ownership document used by persistent markers."""
    return {"schema": STATE_SCHEMA, "cluster_name": config.cluster_name}


def _read_system_file(runner: Runner, path: Path) -> str | None:
    """Read a root-owned file, returning None when it is absent."""
    probe = runner.run([*_sudo_prefix(), "test", "-e", path], check=False, timeout=30)
    if probe.returncode == 1:
        return None
    if probe.returncode != 0:
        raise ProvisionError(f"could not inspect system path: {path}")
    return runner.run([*_sudo_prefix(), "cat", path], timeout=30).stdout


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
    export_directory: subprocess.CompletedProcess[str] | None = None
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
    else:
        export_directory = runner.run(
            [*_sudo_prefix(), "test", "-d", config.export_dir], check=False
        )
        if export_directory.returncode not in {0, 1}:
            raise ProvisionError(
                f"could not inspect export directory: {config.export_dir}"
            )
    if export_directory is not None and export_directory.returncode == 0:
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
    service_state = config.state_dir / "nfs-service.json"
    host_owned = _validate_nfs_host_owner(runner, config)
    if not host_owned and (
        service_state.exists() or export_owned or config.nfs_image.exists()
    ):
        raise ProvisionError(
            "refusing to remove NFS artifacts without the host-global owner record"
        )
    nfs_configured = host_owned
    if host_owned:
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
        if (
            export_config is not None or daemon_config is not None
        ) and not export_owned:
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
        if not state_dir.parent.is_dir() and state_dir != DEFAULT_STATE_DIR:
            raise ProvisionError(
                f"state directory must be a leaf below an existing directory: {state_dir}"
            )
        return
    if not state_dir.is_dir():
        raise ProvisionError(f"state path is not a directory: {state_dir}")
    if state_dir.stat().st_uid != os.getuid():
        raise ProvisionError(
            f"setup state is not owned by the current user: {state_dir}"
        )
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
    if not shutil.which("exportfs"):
        return []
    exports = runner.run([*_sudo_prefix(), "exportfs", "-v"]).stdout
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
    return _loop_devices_for_file(runner, config.nfs_image)


def _validate_loop_associations(runner: Runner, config: Config) -> None:
    """Reject an owned loop device mounted anywhere except the export path."""
    for device in _associated_loop_devices(runner, config):
        result = runner.run(
            ["findmnt", "--noheadings", "--output", "TARGET", "--source", device],
            check=False,
        )
        if result.returncode == 1:
            continue
        if result.returncode != 0:
            raise ProvisionError(
                f"findmnt could not inspect loop device {device}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        mounts = result.stdout.splitlines()
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
        pending_id = ownership.get("pending_id")
        pending_tag = ownership.get("pending_tag")
        if pending_tag:
            temporary_id = _image_id(runner, pending_tag)
            if pending_id is not None and temporary_id not in {pending_id, None}:
                raise ProvisionError(
                    f"pending Docker tag changed outside the fixture: {pending_tag}"
                )
        allowed = {previous_id, ownership.get("built_id"), pending_id, None}
        if current not in allowed:
            raise ProvisionError(
                f"refusing to alter Docker tag changed outside the fixture: {image}"
            )


def _remove_nfs_configuration(runner: Runner, config: Config) -> None:
    """Unexport storage and remove only the fixture's host configuration."""
    LOG.info("Removing the dedicated NFS export and host configuration")
    exportfs = shutil.which("exportfs")
    export_source = config.manifests_dir / "storage-scale-test.exports"
    if exportfs and export_source.exists():
        client = export_source.read_text(encoding="utf-8").split()[1].split("(", 1)[0]
        runner.run(
            [
                *_sudo_prefix(),
                exportfs,
                "-u",
                f"{client}:{config.export_dir}",
            ],
            check=False,
        )
    for path in (NFS_EXPORT_CONFIG, NFS_DAEMON_CONFIG):
        runner.run([*_sudo_prefix(), "rm", "--force", "--", path])
    if exportfs:
        runner.run([*_sudo_prefix(), exportfs, "-ra"])
    else:
        LOG.info("exportfs is unavailable; skipping partial-bootstrap reload")
    if str(config.export_dir) in _export_paths(runner):
        raise ProvisionError(f"NFS export is still active: {config.export_dir}")
    _restore_nfs_service_state(runner, config)
    firewall_state = config.state_dir / "ufw-rule.json"
    if firewall_state.exists():
        state = json.loads(firewall_state.read_text(encoding="utf-8"))
        if state.get("added_by_harness"):
            _delete_nfs_firewall_rule(runner, str(state["subnet"]))
            firewall_state.unlink(missing_ok=True)


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
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.exists()
        else {}
    )
    for image, ownership in state.items():
        built_id = ownership.get("built_id")
        pending_id = ownership.get("pending_id")
        pending_tag = ownership.get("pending_tag")
        previous_id = ownership.get("previous_id")
        current = _image_id(runner, image)
        if current in {built_id, pending_id} and current is not None:
            if previous_id:
                runner.run(["docker", "image", "tag", previous_id, image])
            else:
                runner.run(["docker", "image", "rm", image])
        if pending_tag:
            temporary_id = _image_id(runner, pending_tag)
            if temporary_id is not None:
                if pending_id is not None and temporary_id != pending_id:
                    raise ProvisionError(
                        f"refusing to remove changed pending Docker tag: {pending_tag}"
                    )
                runner.run(["docker", "image", "rm", pending_tag])
    _remove_stale_csi_host_aliases(runner)


def _remove_owned_directories(
    runner: Runner,
    config: Config,
    *,
    remove_state: bool,
    remove_export: bool,
) -> None:
    """Remove validated user and privileged roots without crossing mounts."""
    paths: list[tuple[Path, bool]] = []
    if remove_state:
        paths.append((config.state_dir, False))
    if remove_export:
        paths.insert(0, (config.export_dir, True))
    for path, privileged in paths:
        prefix = _sudo_prefix() if privileged else []
        marker_name = EXPORT_MARKER if privileged else STATE_MARKER
        probe = runner.run([*prefix, "test", "-e", path], check=False)
        if probe.returncode == 1:
            continue
        if probe.returncode != 0:
            raise ProvisionError(f"could not inspect cleanup path: {path}")
        runner.run(
            [
                *prefix,
                "find",
                path,
                "-mindepth",
                "1",
                "-xdev",
                "-depth",
                "!",
                "-name",
                marker_name,
                "-delete",
            ],
            timeout=120,
        )
        runner.run([*prefix, "rm", "--force", "--", path / marker_name])
        runner.run([*prefix, "rmdir", "--", path])
        removed = runner.run([*prefix, "test", "-e", path], check=False)
        if removed.returncode == 0:
            raise ProvisionError(f"cleanup did not remove {path}")
        if removed.returncode != 1:
            raise ProvisionError(f"could not verify cleanup of {path}")


def _stop_owned_nfs(runner: Runner, config: Config) -> None:
    """Restore an initially inactive NFS service to its runtime state."""
    if not (config.state_dir / "nfs-service.json").exists():
        LOG.info("NFS ownership state is absent; leaving nfs-server unchanged")
        return
    if not _nfs_service_started_by_harness(config):
        LOG.info("nfs-server predated this fixture; leaving it running")
        return
    service = runner.run(
        [*_sudo_prefix(), "systemctl", "is-active", "nfs-server"], check=False
    )
    if service.returncode == 0:
        runner.run([*_sudo_prefix(), "systemctl", "stop", "nfs-server"])
        LOG.info("Stopped harness-owned nfs-server")
    else:
        LOG.info("nfs-server is already stopped")


def _nfs_service_started_by_harness(config: Config) -> bool:
    """Return whether setup recorded ownership of the NFS service lifecycle."""
    state_path = config.state_dir / "nfs-service.json"
    if not state_path.exists():
        return False
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("schema") == NFS_SERVICE_STATE_SCHEMA:
        return not bool(state.get("was_active"))
    return bool(state.get("started_by_harness", False))


def _restore_preexisting_nfs_threads(runner: Runner, config: Config) -> None:
    """Restore the worker count of an NFS service that predates the fixture."""
    state_path = config.state_dir / "nfs-service.json"
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    previous = state.get("previous_threads")
    was_active = (
        bool(state.get("was_active"))
        if state.get("schema") == NFS_SERVICE_STATE_SCHEMA
        else not bool(state.get("started_by_harness"))
    )
    if not was_active or not isinstance(previous, int):
        return
    active = runner.run(
        [*_sudo_prefix(), "systemctl", "is-active", "nfs-server"],
        check=False,
    )
    if active.returncode:
        LOG.info("Pre-existing nfs-server is inactive; worker restoration skipped")
        return
    _set_live_nfs_threads(runner, previous)
    LOG.info("Restored pre-existing NFS worker count to %d", previous)


def _restore_nfs_service_state(runner: Runner, config: Config) -> None:
    """Restore the pre-setup NFS runtime and boot-policy state."""
    state_path = config.state_dir / "nfs-service.json"
    if not state_path.exists():
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    _restore_preexisting_nfs_threads(runner, config)
    if not _systemd_unit_exists(runner, "nfs-server"):
        LOG.info("nfs-server is unavailable; no boot policy needs restoration")
        return
    if state.get("schema") != NFS_SERVICE_STATE_SCHEMA:
        if state.get("started_by_harness") and not _export_paths(runner):
            runner.run([*_sudo_prefix(), "systemctl", "disable", "nfs-server"])
        return
    was_enabled = bool(state.get("was_enabled"))
    action = "enable" if was_enabled else "disable"
    runner.run([*_sudo_prefix(), "systemctl", action, "nfs-server"])


def _diagnostic_backend(config: Config) -> str | None:
    """Return the configured or retained backend without probing or mutation."""
    if config.storage_backend != "auto":
        return config.storage_backend
    path = config.state_dir / "storage-backend.json"
    if not path.is_file():
        return None
    try:
        backend = json.loads(path.read_text(encoding="utf-8")).get("backend")
    except (OSError, json.JSONDecodeError):
        return None
    return backend if backend in STORAGE_BACKENDS else None


def _collect_diagnostics(runner: Runner, config: Config) -> None:
    """Collect bounded troubleshooting state after a setup failure."""
    LOG.error("Collecting troubleshooting diagnostics")
    commands: list[Sequence[str | Path]] = [
        ("free", "-h"),
        ("df", "-h", "/"),
        ("docker", "ps", "--all"),
        ("kind", "get", "clusters"),
    ]
    if _diagnostic_backend(config) == "nfs":
        commands.extend(
            (
                (
                    *_sudo_prefix(),
                    "systemctl",
                    "status",
                    "nfs-server",
                    "--no-pager",
                ),
                (*_sudo_prefix(), "exportfs", "-v"),
            )
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
        choices=("auto", *STORAGE_BACKENDS),
        default="auto",
    )
    parser.add_argument(
        "--sbx-shared-root",
        type=Path,
        default=DEFAULT_SBX_SHARED_ROOT,
    )
    parser.add_argument("--verbose", action="store_true")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("setup", "start", "stop", "teardown"):
        actions.add_parser(action)
    test_parser = actions.add_parser("test")
    test_parser.add_argument(
        "--substrate",
        choices=SUBSTRATES,
        default="all",
        help="execution substrate to test (default: all)",
    )
    test_parser.add_argument(
        "--scenario",
        dest="scenarios",
        action="append",
        default=[],
        metavar="NAME",
        help="scenario to run; repeat to select more than one",
    )
    test_parser.add_argument(
        "--list-scenarios",
        action="store_true",
        help="list scenarios without inspecting or changing fixture state",
    )
    return parser


def _config(arguments: argparse.Namespace) -> Config:
    """Convert parsed arguments into immutable configuration."""
    account = _test_account()
    return Config(
        cluster_name=arguments.cluster_name,
        namespace=arguments.namespace,
        state_dir=arguments.state_dir.resolve(),
        export_dir=arguments.export_dir.resolve(),
        storage_backend=arguments.storage_backend,
        sbx_shared_root=arguments.sbx_shared_root.resolve(),
        test_user=account.pw_name,
        test_uid=account.pw_uid,
        test_gid=account.pw_gid,
        verbose=arguments.verbose,
    )


def _test_account() -> pwd.struct_passwd:
    """Resolve the ordinary account running every lifecycle action."""
    try:
        account = pwd.getpwuid(os.getuid())
    except KeyError as error:
        raise ProvisionError(
            f"current uid has no password-database entry: {os.getuid()}"
        ) from error
    if account.pw_uid == 0:
        raise ProvisionError(
            "integration lifecycle actions must run as an ordinary user; "
            "the driver invokes sudo only for narrow host-system operations"
        )
    return account


def _run_filesystem_action(
    runner: Runner, config: Config, arguments: argparse.Namespace
) -> None:
    """Run selected scenarios with crash-recoverable SSH home transitions."""
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{os.getpid()}"

    def transition(mode: str, scenario_id: str) -> None:
        if mode == CANONICAL_HOME_MODE and scenario_id != "preflight":
            _restore_ssh_home_mode(
                runner, config, run_id=run_id, scenario_id=scenario_id
            )
            return
        _ensure_ssh_home_mode(
            runner,
            config,
            mode,
            run_id=run_id,
            scenario_id=scenario_id,
        )

    run_filesystem_tests(
        runner,
        config,
        _repository_root(),
        arguments.substrate,
        arguments.scenarios,
        transition,
    )


def main() -> int:
    """Run one integration environment lifecycle action."""
    _require_python()
    arguments = _parser().parse_args()
    if arguments.action == "test" and arguments.list_scenarios:
        print(format_scenario_listing())
        return 0
    try:
        if os.geteuid() == 0:
            raise ProvisionError(
                "refusing to run the integration lifecycle as root; rerun as "
                "an ordinary user with sudo available for NFS host operations"
            )
        config = _config(arguments)
        _validate_lifecycle_paths(config)
        if arguments.action == "test" and not config.state_dir.is_dir():
            raise ProvisionError(
                f"setup state directory is absent at {config.state_dir}; run setup first"
            )
        _bootstrap_state_dir(config)
        _configure_user_tool_path(config)
        log_path = _configure_logging(config, arguments.action)
        LOG.info("Detailed log: %s", log_path)
        with _acquire_lock(config):
            runner = Runner()
            if arguments.action == "stop":
                stop_environment(runner, config)
            elif arguments.action == "teardown":
                teardown_environment(runner, config)
            elif arguments.action == "test":
                _run_filesystem_action(runner, config, arguments)
            else:
                setup_environment(runner, config)
                _verify_user_access(runner, config)
        return 0
    except (
        ProvisionError,
        IntegrationTestError,
        SshHomeTransitionError,
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
