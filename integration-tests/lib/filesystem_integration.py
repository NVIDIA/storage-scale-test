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
import json
import logging
import os
import platform
import re
import shlex
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

LOG = logging.getLogger("storage-scale-integration")

ELBENCHO_VERSION = "v3.1-11"
ELBENCHO_RELEASE_API = (
    "https://api.github.com/repos/breuner/elbencho/releases/tags/" + ELBENCHO_VERSION
)
ELBENCHO_CONTAINER = (
    "breuner/elbencho:v3.1-11@"
    "sha256:719fba92cab57c773ddf7a2776414b358aeb8126a15fbc8e3c52469ce3a5b8b2"
)
SBX_ELBENCHO_BUNDLE_RECIPE = 1
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_DEPLOYMENT_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_DEPLOYMENT_FILES = 10_000
MAX_DEPLOYMENT_CONTENT_BYTES = 512 * 1024 * 1024
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
VALIDATION_SUCCESS = "All validation checks passed successfully"
TEST_SELECTORS = ("all", "filesystem", "ssh", "slurm")


class IntegrationTestError(RuntimeError):
    """An actionable filesystem integration test failure."""


@dataclass(frozen=True)
class Fixture:
    """Live fixture details discovered from Kubernetes and Slurm."""

    login_pod: str
    login_container: str
    ssh_addresses: tuple[str, str]
    slurm_nodes: tuple[str, str]
    slurm_addresses: tuple[str, str]
    architecture: str
    ssh_home_mode: str
    storage_backend: str


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
            "--foreground",
            "--kill-after=10s",
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
    return state


def _require_nodes(runner: Any, config: Any) -> None:
    """Require the exact ready three-node topology and labels."""
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


def _require_storage(runner: Any, config: Any) -> None:
    """Require both integration PVCs to be bound."""
    result = runner.run(
        _kubectl(config, "-n", config.namespace, "get", "pvc", "-o", "json"),
        timeout=30,
    )
    claims = {
        item["metadata"]["name"]: item.get("status", {}).get("phase")
        for item in json.loads(result.stdout)["items"]
    }
    expected = {"storage-test-rwx", "ssh-home-rwx"}
    if any(claims.get(name) != "Bound" for name in expected):
        raise IntegrationTestError(f"integration PVCs are not Bound: {claims}")


def _pod_inventory(runner: Any, config: Any) -> list[dict[str, Any]]:
    """Return the namespace pod inventory."""
    result = runner.run(
        _kubectl(config, "-n", config.namespace, "get", "pods", "-o", "json"),
        timeout=30,
    )
    return json.loads(result.stdout)["items"]


def _require_pods(
    pods: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Require one LoginSet and two SSH worker pods."""
    login = _pods_with_container(pods, "login")
    ssh = sorted(
        _pods_with_container(pods, "sshd"), key=lambda item: item["metadata"]["name"]
    )
    slurmd = _pods_with_container(pods, "slurmd")
    if len(login) != 1 or len(ssh) != 2 or len(slurmd) != 2:
        raise IntegrationTestError(
            "fixture workloads are incomplete: "
            f"login={len(login)}, ssh={len(ssh)}, slurmd={len(slurmd)}; run setup"
        )
    return login[0], ssh


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


def _require_fixture(runner: Any, config: Any) -> Fixture:
    """Validate setup without reconciling or installing anything."""
    state = _load_state(config)
    required_host_tools = (
        "bash",
        "file",
        "find",
        "git",
        "ssh-add",
        "ssh-agent",
        "tar",
        "timeout",
    )
    missing_host_tools = [
        tool for tool in required_host_tools if shutil.which(tool) is None
    ]
    if missing_host_tools:
        raise IntegrationTestError(
            "required host tools are absent: " + ", ".join(missing_host_tools)
        )
    if not config.kubeconfig.is_file():
        raise IntegrationTestError(
            f"private kubeconfig is absent at {config.kubeconfig}; run setup first"
        )
    _require_nodes(runner, config)
    _require_storage(runner, config)
    login, ssh = _require_pods(_pod_inventory(runner, config))
    login_name = str(login["metadata"]["name"])
    ssh_name = str(ssh[0]["metadata"]["name"])
    addresses = tuple(str(item["status"]["podIP"]) for item in ssh)
    if len(set(addresses)) != 2:
        raise IntegrationTestError(f"SSH workers lack distinct addresses: {addresses}")
    tools = (
        "for tool in bash file find tar timeout; do "
        'command -v "$tool" >/dev/null || exit 1; done'
    )
    _probe_pod(runner, config, login_name, "login", tools)
    _probe_pod(runner, config, ssh_name, "sshd", tools, as_user="tester")
    mount_probe = "test -w /mnt/storage-test"
    if state["storage_backend"] == "nfs":
        mount_probe += (
            " && case $(stat -f -c %T /mnt/storage-test) in "
            "nfs|nfs4) true;; *) false;; esac"
        )
    _probe_pod(runner, config, login_name, "login", mount_probe)
    _probe_pod(runner, config, ssh_name, "sshd", mount_probe, as_user="tester")
    login_arch = _probe_pod(runner, config, login_name, "login", "uname -m")
    ssh_arch = _probe_pod(
        runner, config, ssh_name, "sshd", "uname -m", as_user="tester"
    )
    host_arch = platform.machine()
    if login_arch != ssh_arch or login_arch != host_arch:
        raise IntegrationTestError(
            "fixture architecture mismatch: "
            f"host={host_arch}, login={login_arch}, ssh={ssh_arch}"
        )
    if host_arch not in ELBENCHO_ARCHIVES:
        raise IntegrationTestError(f"unsupported fixture architecture: {host_arch}")
    slurm_output = _probe_pod(
        runner,
        config,
        login_name,
        "login",
        "sinfo -N -h -o %N | sort -u",
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
    )
    slurm_addresses = tuple(line for line in slurm_address_output.splitlines() if line)
    if len(slurm_addresses) != 2 or len(set(slurm_addresses)) != 2:
        raise IntegrationTestError(
            f"Slurm workers lack distinct IPv4 addresses: {slurm_addresses}"
        )
    return Fixture(
        login_pod=login_name,
        login_container="login",
        ssh_addresses=(addresses[0], addresses[1]),
        slurm_nodes=(slurm_nodes[0], slurm_nodes[1]),
        slurm_addresses=(slurm_addresses[0], slurm_addresses[1]),
        architecture=host_arch,
        ssh_home_mode=str(state["ssh_home_mode"]),
        storage_backend=str(state["storage_backend"]),
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


def _override_block(
    selector: str, remote_root: str, fixture: Fixture
) -> tuple[str, dict[str, str]]:
    """Return template overrides and small support-file contents."""
    data = "/mnt/storage-test"
    lines = [
        "# Bounded integration regression overrides.",
        f"export RESULTS_DIR={_shell(remote_root + '/results')}",
        f"export LOGS_DIR={_shell(remote_root + '/logs')}",
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
    else:
        include_file = f"{remote_root}/slurm-nodes"
        ignore_file = f"{remote_root}/slurm-ignore"
        lines.extend(
            (
                "unset SSH_HOST_LIST SSH_USER SSH_HOMEDIR_SHARED",
                'account=""',
                'reservation=""',
                'partition="all"',
                'run_time="00:05:00"',
                "SLURM_EXCLUSIVE_USER=0",
                f"export SLURM_NODE_INCLUDES={_shell(include_file)}",
                f"export SLURM_NODE_IGNORES={_shell(ignore_file)}",
            )
        )
        support["slurm-nodes"] = "\n".join(fixture.slurm_nodes) + "\n"
        support["slurm-ignore"] = ""
    return "\n".join(lines) + "\n", support


def _render_env(
    template: Path, selector: str, remote_root: str, fixture: Fixture
) -> tuple[str, dict[str, str]]:
    """Render one runtime env from the repository's real user template."""
    text = template.read_text(encoding="utf-8")
    anchor = 'source "${SCALE_TEST_BASE}/lib/env_base.sh"'
    if text.count(anchor) != 1:
        raise IntegrationTestError(
            f"expected exactly one env_base source anchor in {template}"
        )
    overrides, support = _override_block(selector, remote_root, fixture)
    rendered = text.replace(anchor, overrides + "\n" + anchor)
    return rendered, support


def _copy_tracked_snapshot(runner: Any, repo_root: Path, destination: Path) -> None:
    """Copy only tracked working-tree files into an isolated packaging tree."""
    required = (repo_root / "utils" / "build_tarball.sh", repo_root / "NOTICE")
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise IntegrationTestError(
            "deployment tarball sources are absent: " + ", ".join(missing)
        )
    result = runner.run(
        ["git", "ls-files", "-z", "--cached"], cwd=repo_root, timeout=30
    )
    destination.mkdir(parents=True)
    for name in result.stdout.split("\0"):
        if not name:
            continue
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise IntegrationTestError(f"unsafe tracked path from git: {name!r}")
        source = repo_root / Path(*relative.parts)
        target = destination / Path(*relative.parts)
        if not source.is_file() or source.is_symlink():
            raise IntegrationTestError(
                f"deployment snapshot requires a regular tracked file: {source}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


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
    repo_root: Path,
    build_root: Path,
    binary: Path,
    binary_name: str,
    architecture: str,
    runtime: Path | None,
) -> tuple[Path, Path]:
    """Build and inspect one filesystem-only deployment tarball."""
    snapshot = build_root / "source"
    _copy_tracked_snapshot(runner, repo_root, snapshot)
    seeded_binary = snapshot / "utils" / binary_name
    shutil.copy2(binary, seeded_binary)
    seeded_binary.chmod(0o755)
    if runtime is not None:
        shutil.copytree(runtime, snapshot / "utils" / "elbencho-runtime")
    LOG.info("Building deployment tarball from a tracked-files-only snapshot")
    result = runner.run(
        [
            snapshot / "utils" / "build_tarball.sh",
            "--arch",
            architecture,
            "--skip-object-tools",
        ],
        cwd=snapshot,
        timeout=600,
    )
    (build_root / "build-tarball.log").write_text(
        result.stdout + result.stderr, encoding="utf-8"
    )
    archive = build_root / "storage-scale-test.tar.gz"
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
) -> None:
    """Add the generated environment and support files to a deployment."""
    rendered, support = _render_env(
        template or workspace / "env.sh.template", selector, runtime_root, fixture
    )
    (workspace / "env.sh").write_text(rendered, encoding="utf-8")
    (workspace / "env.sh").chmod(0o640)
    for name, content in support.items():
        (workspace / name).write_text(content, encoding="utf-8")
        (workspace / name).chmod(0o640)


def _stream_to_login(
    runner: Any,
    config: Any,
    fixture: Fixture,
    source: Path,
    command: list[str | Path],
    timeout: int = 180,
) -> None:
    """Stream one local archive to a command in the Slinky LoginSet."""
    with source.open("rb") as stream:
        runner.run(
            [
                *_kubectl(
                    config,
                    "-n",
                    config.namespace,
                    "exec",
                    "-i",
                    fixture.login_pod,
                    "-c",
                    fixture.login_container,
                    "--",
                ),
                *command,
            ],
            stdin=stream,
            timeout=timeout,
        )


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
                "bash",
                "-ec",
                "rm -rf -- /home/tester/elbencho-runtime && "
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
) -> str:
    """Stage a packaged deployment for host SSH or LoginSet Slurm execution."""
    if selector == "ssh":
        workspace = build_root / "ssh" / "storage-scale-test"
        shutil.copytree(extracted, workspace)
        _write_runtime_files(workspace, selector, str(workspace), fixture)
        return str(workspace)

    remote_base = f"{REMOTE_BASE}/{selector}"
    remote_root = f"{remote_base}/storage-scale-test"
    reset = f"rm -rf -- {_shell(remote_base)} && mkdir -p -- {_shell(remote_base)}"
    runner.run(
        _pod_command(
            config,
            fixture.login_pod,
            fixture.login_container,
            reset,
            timeout=60,
        ),
        timeout=75,
    )
    extract_command = [
        "tar",
        "--no-same-owner",
        "--no-same-permissions",
        "-xzf",
        "-",
        "-C",
        remote_base,
    ]
    _stream_to_login(runner, config, fixture, archive, extract_command)
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
        "tar",
        "--no-same-owner",
        "--no-same-permissions",
        "-xf",
        "-",
        "-C",
        remote_root,
    ]
    _stream_to_login(runner, config, fixture, support_archive, support_command)
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
            "--foreground",
            "--kill-after=10s",
            f"{timeout}s",
            "bash",
            "-c",
            host_command,
        ]
    return _pod_command(
        config,
        fixture.login_pod,
        fixture.login_container,
        command,
        timeout=timeout,
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
) -> str:
    """Run one bounded substrate step and preserve diagnostic output."""
    LOG.info("Running %s filesystem step: %s", selector, name)
    result = runner.run(
        _test_command(config, fixture, selector, command, timeout),
        check=False,
        timeout=timeout + 30,
    )
    output = result.stdout + result.stderr
    log_path = log_dir / f"{selector}-{name}.log"
    log_path.write_text(output, encoding="utf-8")
    log_path.chmod(0o640)
    if result.returncode:
        detail = output.strip()[-8000:]
        raise IntegrationTestError(
            f"{selector} {name} failed with exit code {result.returncode}; "
            f"full output: {log_path}\n{detail}"
        )
    LOG.info("Passed %s filesystem step: %s", selector, name)
    return result.stdout


def _assert_results(
    runner: Any,
    config: Any,
    fixture: Fixture,
    selector: str,
    remote_root: str,
    log_dir: Path,
) -> str:
    """Assert the two execution records and bounded-data cleanup."""
    results = f"{remote_root}/results"
    discover = (
        f"find {_shell(results)} -mindepth 1 -maxdepth 1 -type d "
        "-name 'elbencho-*' -printf '%p\\n'"
    )
    output = _run_step(
        runner,
        config,
        fixture,
        selector,
        "discover-results",
        discover,
        log_dir,
        30,
    )
    directories = [line for line in output.splitlines() if line.strip()]
    if len(directories) != 1:
        raise IntegrationTestError(
            f"expected one {selector} results directory; found {directories}"
        )
    result_dir = directories[0]
    if selector == "ssh":
        quoted_addresses = " ".join(
            _shell(address) for address in fixture.ssh_addresses
        )
        remote_cleanup = (
            'test -z "$(find /mnt/storage-test -mindepth 1 -maxdepth 1 '
            "-type d -name 'elbencho-sweep-target-*' -print -quit)\""
        )
        cleanup_probe = f"""
for host in {quoted_addresses}; do
    ssh -T -o BatchMode=yes -o ConnectTimeout=15 \\
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \\
        -o PreferredAuthentications=publickey -o LogLevel=ERROR \\
        "tester@$host" {_shell(remote_cleanup)}
done
""".strip()
    else:
        cleanup_probe = (
            'test -z "$(find /mnt/storage-test -mindepth 1 -maxdepth 1 '
            "-type d -name 'elbencho-sweep-target-*' -print -quit)\""
        )
    assertion = f"""
set -euo pipefail
result={_shell(result_dir)}
test -s "$result/env_used.yaml"
test -s "$result/env_used.sh"
test "$(find "$result/executions" -maxdepth 1 -name '*.status' | wc -l)" -eq 2
for id in 0001 0002; do
    grep -qx SUCCESS "$result/executions/$id.status"
    grep -qx 0 "$result/executions/$id.exitcode"
    test -s "$result/executions/$id.log"
    test -s "$result/executions/$id.workload.tsv"
done
grep -qx $'dataset_files_total\t1' "$result/executions/0001.workload.tsv"
grep -qx $'dataset_files_total\t2' "$result/executions/0002.workload.tsv"
grep -qx $'dataset_bytes_total\t16777216' "$result/executions/0001.workload.tsv"
grep -qx $'dataset_bytes_total\t33554432' "$result/executions/0002.workload.tsv"
grep -qx $'completion_state\tcompleted' "$result/executions/0001.workload.tsv"
grep -qx $'completion_state\tcompleted' "$result/executions/0002.workload.tsv"
test "$(find "$result" -type f -name '*.csv' -size +0c | wc -l)" -ge 2
test "$(find "$result" -type f -name '*.out' -size +0c | wc -l)" -ge 2
{cleanup_probe}
""".strip()
    _run_step(
        runner,
        config,
        fixture,
        selector,
        "assert-results",
        assertion,
        log_dir,
        60,
    )
    return result_dir


def _assert_ordered_workers(fixture: Fixture, selector: str, sweep_output: str) -> None:
    """Prove increasing cells use the configured workers in prefix order."""
    workers = fixture.ssh_addresses if selector == "ssh" else fixture.slurm_addresses
    expected = {
        "1": workers[0],
        "2": ",".join(workers),
    }
    selected: dict[str, str] = {}
    for line in sweep_output.splitlines():
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
    destination.mkdir(parents=True)
    if selector == "ssh":
        shutil.copytree(result_dir, destination / Path(result_dir).name)
    else:
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
    return destination / Path(result_dir).name


def _assert_report(
    runner: Any,
    report_workspace: Path,
    result_dir: Path,
    selector: str,
    log_dir: Path,
) -> None:
    """Run the supported report wrapper and verify both sweep sizes appear."""
    result = runner.run(
        [
            report_workspace / "utils" / "extract-elbencho.sh",
            "--markdown",
            result_dir,
        ],
        cwd=report_workspace,
        timeout=600,
        check=False,
    )
    output = result.stdout + result.stderr
    report_log = log_dir / f"{selector}-extract-elbencho.log"
    report_log.write_text(output, encoding="utf-8")
    report_log.chmod(0o640)
    if result.returncode:
        raise IntegrationTestError(
            f"{selector} result reporting failed with exit code {result.returncode}; "
            f"full output: {report_log}\n{output.strip()[-8000:]}"
        )
    missing = []
    if "| Nodes" not in output:
        missing.append("Nodes header")
    for node_count in (1, 2):
        if not re.search(rf"^\|\s*{node_count}\s*\|", output, re.MULTILINE):
            missing.append(f"{node_count}-node row")
    if missing:
        raise IntegrationTestError(
            f"{selector} report omitted expected node-count rows {missing}; "
            f"full output: {report_log}"
        )


def _run_substrate(
    runner: Any,
    config: Any,
    fixture: Fixture,
    selector: str,
    archive: Path,
    extracted: Path,
    build_root: Path,
    report_workspace: Path,
    log_dir: Path,
) -> None:
    """Stage, validate, run, and inspect one filesystem substrate."""
    remote_root = _stage_workspace(
        runner,
        config,
        fixture,
        selector,
        archive,
        extracted,
        build_root,
    )
    prefix = f"cd -- {_shell(remote_root)} && "
    validation = _run_step(
        runner,
        config,
        fixture,
        selector,
        "validate-env",
        prefix + "./validate_env.sh",
        log_dir,
        180,
    )
    if VALIDATION_SUCCESS not in validation:
        raise IntegrationTestError(
            f"{selector} validate_env.sh omitted its success marker"
        )
    sweep = prefix + "./storage-tests/fs/nv-elbencho-sweep.sh -b --nodes 1,2"
    sweep_output = _run_step(
        runner,
        config,
        fixture,
        selector,
        "filesystem-sweep",
        sweep,
        log_dir,
        600,
    )
    _assert_ordered_workers(fixture, selector, sweep_output)
    result_dir = _assert_results(
        runner, config, fixture, selector, remote_root, log_dir
    )
    local_result = _copy_result_for_reporting(
        runner,
        config,
        fixture,
        selector,
        result_dir,
        build_root / f"{selector}-report-input",
    )
    _assert_report(runner, report_workspace, local_result, selector, log_dir)


def _selected_tests(selectors: list[str]) -> tuple[str, ...]:
    """Normalize public selectors to ordered substrate names."""
    requested = selectors or ["all"]
    unknown = sorted(set(requested) - set(TEST_SELECTORS))
    if unknown:
        raise IntegrationTestError(
            f"unknown test selector(s): {', '.join(unknown)}; "
            f"choose from {', '.join(TEST_SELECTORS)}"
        )
    duplicates = sorted(name for name in set(requested) if requested.count(name) > 1)
    if duplicates:
        raise IntegrationTestError(
            f"duplicate test selector(s): {', '.join(duplicates)}"
        )
    if "all" in requested and len(requested) > 1:
        raise IntegrationTestError("test selector 'all' cannot be combined")
    if "filesystem" in requested and len(requested) > 1:
        raise IntegrationTestError("test selector 'filesystem' cannot be combined")
    if requested in (["all"], ["filesystem"]):
        return ("ssh", "slurm")
    return tuple(name for name in ("ssh", "slurm") if name in requested)


def run_filesystem_tests(
    runner: Any, config: Any, repo_root: Path, selectors: list[str]
) -> None:
    """Run selected filesystem regression cases against an existing setup."""
    selected = _selected_tests(selectors)
    LOG.info("Requiring an already-running integration setup")
    fixture = _require_fixture(runner, config)
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
        _write_runtime_files(
            report_workspace,
            "ssh",
            str(report_workspace),
            fixture,
            template=extracted / "env.sh.template",
        )
        for selector in selected:
            _run_substrate(
                runner,
                config,
                fixture,
                selector,
                archive,
                extracted,
                build_root,
                report_workspace,
                log_dir,
            )
    LOG.info("Filesystem integration tests passed: %s", ", ".join(selected))
