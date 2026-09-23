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

"""Behavioral tests for the Kubernetes coordinator's endpoint probe."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import socketserver
import subprocess
import threading
import time
from typing import Iterator

import pytest
import yaml

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_COORDINATOR = (
    _REPOSITORY_ROOT
    / "storage-tests"
    / "fs"
    / "kubectl"
    / "_nv-elbencho-kubectl-coordinator.sh"
)
_PREREQUISITE_MANIFEST = (
    _REPOSITORY_ROOT
    / "integration-tests"
    / "manifests"
    / "kubectl-prerequisite-probe.yaml.tmpl"
)
_BASH = "/bin/bash"


@contextmanager
def _probe_server(response: bytes | None, delay: float = 0) -> Iterator[None]:
    """Serve one status response or hold the connection open for a timeout."""

    class _Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.recv(4096)
            if delay:
                time.sleep(delay)
            elif response is not None:
                self.request.sendall(response)

    class _Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with _Server(("127.0.0.1", 1611), _Handler) as server:
        acceptor = threading.Thread(target=server.serve_forever, daemon=True)
        acceptor.start()
        try:
            yield
        finally:
            server.shutdown()
            acceptor.join(timeout=1)


@pytest.mark.parametrize(
    ("response", "expected_rc"),
    (
        (b"HTTP/1.0 200 OK\r\n\r\n", 0),
        (b"HTTP/1.0 503 Busy\r\n\r\n", 1),
        (b"garbage 200 text\r\n", 1),
    ),
)
def test_probe_endpoint_uses_real_http_result(
    response: bytes, expected_rc: int
) -> None:
    """The file-resident child accepts 200 and rejects non-200 responses."""
    with _probe_server(response):
        result = subprocess.run(
            [_BASH, str(_COORDINATOR), "--probe-endpoint", "127.0.0.1"],
            cwd=_REPOSITORY_ROOT,
            env={"PATH": "/usr/bin:/bin"},
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
    assert result.returncode == expected_rc, result.stderr


def test_probe_endpoint_times_out_a_tarpit_connection() -> None:
    """The child probe is bounded when the worker never sends a status line."""
    with _probe_server(None, delay=20):
        started = time.monotonic()
        result = subprocess.run(
            [
                "/usr/bin/timeout",
                "--kill-after=2s",
                "5s",
                _BASH,
                str(_COORDINATOR),
                "--probe-endpoint",
                "127.0.0.1",
            ],
            cwd=_REPOSITORY_ROOT,
            env={"PATH": "/usr/bin:/bin"},
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        elapsed = time.monotonic() - started
    assert result.returncode != 0
    assert elapsed < 10


def test_coordinator_and_denied_probe_clients_share_one_script_contract() -> None:
    """The policy fixture's two clients cannot silently drift apart."""
    rendered = _PREREQUISITE_MANIFEST.read_text(encoding="utf-8")
    for placeholder, value in {
        "@@NAMESPACE@@": "fixture",
        "@@ELBENCHO_IMAGE@@": "fixture/image:tag",
        "@@PROBE_TOKEN@@": "token",
    }.items():
        rendered = rendered.replace(placeholder, value)

    documents = list(yaml.safe_load_all(rendered))
    scripts = {
        document["metadata"]["labels"]["app.kubernetes.io/component"]: document["spec"][
            "containers"
        ][0]["args"][0]
        for document in documents
        if document["metadata"]["labels"].get("app.kubernetes.io/component")
        in {"coordinator", "denied"}
    }
    assert set(scripts) == {"coordinator", "denied"}
    assert scripts["coordinator"] == scripts["denied"]
