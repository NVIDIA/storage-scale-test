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

"""Fast regression tests for verified, retryable Helm chart acquisition."""

import hashlib
import importlib.util
import io
import json
import sys
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LIB = _REPO_ROOT / "integration-tests" / "lib"
sys.path.insert(0, str(_LIB))
_MODULE_PATH = _LIB / "chart_acquisition.py"
_SPEC = importlib.util.spec_from_file_location(
    "integration_chart_acquisition_under_test", _MODULE_PATH
)
assert _SPEC and _SPEC.loader
_CHARTS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _CHARTS
_SPEC.loader.exec_module(_CHARTS)

REFERENCE = "oci://ghcr.io/slinkyproject/charts/slurm-operator"
CHART_NAME = "slurm-operator"
VERSION = "1.2.0"


def _chart_bytes(name=CHART_NAME, version=VERSION):
    """Build a tiny readable Helm archive without invoking Helm or the network."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        content = f"apiVersion: v2\nname: {name}\nversion: {version}\n".encode()
        member = tarfile.TarInfo(f"{name}/Chart.yaml")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    return buffer.getvalue()


def _archive_with_members(members):
    """Build a tar archive from explicit member names, types, and content."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, kind, content in members:
            member = tarfile.TarInfo(name)
            if kind == "symlink":
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
            else:
                member.size = len(content)
            archive.addfile(member, io.BytesIO(content) if kind == "file" else None)
    return buffer.getvalue()


class _Runner:
    """Fake Helm runner supporting injected failures and chart downloads."""

    def __init__(self, failures=(), *, chart_bytes=None):
        self.failures = list(failures)
        self.chart_bytes = chart_bytes or _chart_bytes()
        self.calls = []
        self.pull_count = 0

    def run(self, arguments, *, check=True, timeout=None, cwd=None, sensitive=False):
        command = [str(argument) for argument in arguments]
        self.calls.append((command, check, timeout, cwd, sensitive))
        if command[1] == "pull":
            self.pull_count += 1
            if self.failures:
                failure = self.failures.pop(0)
                if isinstance(failure, BaseException):
                    raise failure
                return SimpleNamespace(returncode=1, stdout="", stderr=failure)
            destination = Path(command[command.index("--destination") + 1])
            destination.mkdir(parents=True, exist_ok=True)
            (destination / f"{CHART_NAME}-{VERSION}.tgz").write_bytes(self.chart_bytes)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {command}")


def _ensure(runner, state_dir):
    return _CHARTS.ensure_chart(runner, state_dir, REFERENCE, VERSION)


def _patch_delays(monkeypatch):
    """Make bounded retry waits immediate and deterministic."""
    if hasattr(_CHARTS, "time"):
        monkeypatch.setattr(_CHARTS.time, "sleep", lambda _delay: None)
    if hasattr(_CHARTS, "random"):
        monkeypatch.setattr(_CHARTS.random, "uniform", lambda _low, _high: 0)


@pytest.mark.parametrize(
    "failure",
    [
        "502 Bad Gateway",
        "Bad Gateway",
        "429 Too Many Requests",
        subprocess.TimeoutExpired(["helm", "pull"], 60),
    ],
)
def test_transient_pull_errors_retry_then_publish_verified_chart(
    tmp_path, monkeypatch, failure
):
    """Transient registry failures recover and return a locally validated chart."""
    _patch_delays(monkeypatch)
    runner = _Runner([failure])

    result = _ensure(runner, tmp_path / "state")

    assert (
        result == tmp_path / "state" / "charts" / "oci" / f"{CHART_NAME}-{VERSION}.tgz"
    )
    assert result.is_file()
    assert runner.pull_count == 2
    assert all(call[1] is False for call in runner.calls)
    pull = next(call[0] for call in runner.calls if call[0][1] == "pull")
    assert REFERENCE in pull
    assert VERSION in pull


@pytest.mark.parametrize(
    "failure",
    [
        "401 unauthorized",
        "403 forbidden",
        "404 not found",
        "invalid reference format",
        "no space left on device",
        "digest mismatch",
    ],
)
def test_permanent_pull_errors_do_not_retry(tmp_path, monkeypatch, failure):
    """Permanent and integrity errors stop immediately with useful context."""
    _patch_delays(monkeypatch)
    runner = _Runner([failure, "502 Bad Gateway"])

    with pytest.raises(_CHARTS.AcquisitionError, match=failure):
        _ensure(runner, tmp_path / "state")

    assert runner.pull_count == 1


def test_retry_exhaustion_reports_final_stderr_and_is_bounded(tmp_path, monkeypatch):
    """Retry count and command duration stay within the acquisition budget."""
    _patch_delays(monkeypatch)
    attempts = _CHARTS.CHART_PULL_ATTEMPTS
    runner = _Runner(["502 Bad Gateway"] * attempts)

    with pytest.raises(_CHARTS.AcquisitionError, match="502 Bad Gateway"):
        _ensure(runner, tmp_path / "state")

    pulls = [call for call in runner.calls if call[0][1] == "pull"]
    assert len(pulls) == attempts
    assert all(call[2] <= _CHARTS.CHART_PULL_TIMEOUT_SECONDS for call in pulls)
    assert all(call[4] for call in pulls)


def test_pull_evidence_redacts_secret_urls_and_requests_sensitive_runner(
    tmp_path, monkeypatch, caplog
):
    """Registry evidence is sanitized and the runner suppresses command output."""
    _patch_delays(monkeypatch)
    evidence = (
        "502 Bad Gateway at https://alice:password@ghcr.io/v2/chart?"
        "token=query-secret Authorization: Bearer bearer-secret"
    )
    runner = _Runner([evidence] * _CHARTS.CHART_PULL_ATTEMPTS)

    with pytest.raises(_CHARTS.AcquisitionError) as caught:
        _ensure(runner, tmp_path / "state")

    assert "alice" not in str(caught.value)
    assert "password" not in str(caught.value)
    assert "query-secret" not in str(caught.value)
    assert "bearer-secret" not in str(caught.value)
    assert "ghcr.io" in str(caught.value)
    assert "password" not in caplog.text
    pulls = [call for call in runner.calls if call[0][1] == "pull"]
    assert all(call[4] for call in pulls)


def test_retry_delays_include_exponential_backoff_and_jitter(tmp_path, monkeypatch):
    """Retry timing grows exponentially and includes deterministic jitter."""

    class FakeClock:
        now = 0.0
        delays = []

        def monotonic(self):
            return self.now

        def sleep(self, delay):
            self.delays.append(delay)
            self.now += delay

    clock = FakeClock()
    monkeypatch.setattr(_CHARTS.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(_CHARTS.time, "sleep", clock.sleep)
    image_module = sys.modules["image_acquisition"]
    monkeypatch.setattr(image_module, "IMAGE_PULL_INITIAL_BACKOFF_SECONDS", 2)
    monkeypatch.setattr(image_module, "IMAGE_PULL_JITTER_MILLISECONDS", 1000)
    monkeypatch.setattr(image_module.secrets, "randbelow", lambda _limit: 500)
    runner = _Runner(["502 Bad Gateway"] * 3)

    _ensure(runner, tmp_path / "state")

    assert clock.delays == [2.5, 4.5, 8.5]


def test_retry_delay_is_clamped_by_overall_deadline(tmp_path, monkeypatch):
    """A delay that cannot fit the deadline is skipped; command timeouts shrink."""

    class FakeClock:
        now = 0.0
        delays = []

        def monotonic(self):
            return self.now

        def sleep(self, delay):
            self.delays.append(delay)
            self.now += delay

    clock = FakeClock()
    monkeypatch.setattr(_CHARTS.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(_CHARTS.time, "sleep", clock.sleep)
    monkeypatch.setattr(_CHARTS, "CHART_PULL_DEADLINE_SECONDS", 6.5)
    monkeypatch.setattr(_CHARTS, "retry_delay", lambda attempt: (2**attempt) + 0.5)
    runner = _Runner(["502 Bad Gateway"] * 3)

    with pytest.raises(_CHARTS.AcquisitionError, match="502 Bad Gateway"):
        _ensure(runner, tmp_path / "state")

    pulls = [call for call in runner.calls if call[0][1] == "pull"]
    assert [call[2] for call in pulls] == [6.5, 4.0]
    assert clock.delays == [2.5]


def test_timeout_stderr_is_retained_for_retry_exhaustion(tmp_path, monkeypatch):
    """The final timeout includes bounded diagnostic text from Helm stderr."""
    _patch_delays(monkeypatch)
    timeout = subprocess.TimeoutExpired(["helm", "pull"], 60, stderr=b"Bad Gateway")
    runner = _Runner([timeout] * _CHARTS.CHART_PULL_ATTEMPTS)

    with pytest.raises(_CHARTS.AcquisitionError, match="Bad Gateway"):
        _ensure(runner, tmp_path / "state")

    assert runner.pull_count == _CHARTS.CHART_PULL_ATTEMPTS


def test_cache_hit_verifies_manifest_digest_and_skips_network(tmp_path):
    """A valid archive with matching identity and digest is reused offline."""
    state = tmp_path / "state"
    first = _ensure(_Runner(), state)
    runner = _Runner()

    second = _ensure(runner, state)

    assert second == first
    assert runner.pull_count == 0
    manifest = json.loads(first.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["source"] == REFERENCE
    assert manifest["version"] == VERSION
    assert manifest["name"] == CHART_NAME
    assert manifest["sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "damage", ["checksum", "identity", "archive", "chart-identity"]
)
def test_invalid_cache_entry_is_rejected(tmp_path, damage):
    """Corrupt content or mismatched metadata is never silently reused."""
    state = tmp_path / "state"
    cached = _ensure(_Runner(), state)
    manifest_path = cached.with_suffix(".json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if damage == "checksum":
        cached.write_bytes(b"tampered")
    elif damage == "identity":
        manifest["source"] = "oci://other.invalid/chart"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif damage == "chart-identity":
        cached.write_bytes(_chart_bytes(name="unexpected", version="9.9.9"))
        manifest["sha256"] = hashlib.sha256(cached.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        cached.write_bytes(b"not a tar archive")
    runner = _Runner()

    with pytest.raises(_CHARTS.AcquisitionError):
        _ensure(runner, state)
    assert runner.pull_count == 0


def test_successful_pull_with_missing_archive_is_fatal(tmp_path):
    """A nominally successful Helm command cannot publish absent output."""

    class MissingArchiveRunner(_Runner):
        def run(
            self, arguments, *, check=True, timeout=None, cwd=None, sensitive=False
        ):
            command = [str(argument) for argument in arguments]
            self.calls.append((command, check, timeout, cwd, sensitive))
            self.pull_count += 1
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner = MissingArchiveRunner()
    with pytest.raises(_CHARTS.AcquisitionError):
        _ensure(runner, tmp_path / "state")
    assert runner.pull_count == 1


def test_downloaded_chart_metadata_must_match_requested_identity(tmp_path):
    """A valid archive for a different chart or version is rejected."""
    runner = _Runner(chart_bytes=_chart_bytes(name="unexpected", version="9.9.9"))

    with pytest.raises(_CHARTS.AcquisitionError):
        _ensure(runner, tmp_path / "state")

    assert runner.pull_count == 1
    assert not list((tmp_path / "state" / "charts" / "oci").glob("*.tgz"))


def test_symlinked_cache_archive_is_rejected(tmp_path):
    """An attacker-controlled cache symlink cannot redirect validation."""
    state = tmp_path / "state"
    target = tmp_path / "outside.tgz"
    target.write_bytes(_chart_bytes())
    cache = state / "charts" / "oci"
    cache.mkdir(parents=True)
    archive = cache / f"{CHART_NAME}-{VERSION}.tgz"
    archive.symlink_to(target)
    runner = _Runner()

    with pytest.raises(_CHARTS.AcquisitionError):
        _ensure(runner, state)
    assert runner.pull_count == 0


def test_interrupted_publication_does_not_create_usable_cache(tmp_path):
    """Unpublished temporary artifacts are ignored on the next acquisition."""
    state = tmp_path / "state"
    cache = state / "charts" / "oci"
    cache.mkdir(parents=True)
    (cache / f".{CHART_NAME}-{VERSION}.partial").write_bytes(_chart_bytes())
    runner = _Runner()

    result = _ensure(runner, state)

    assert result.is_file()
    assert runner.pull_count == 1


def test_archive_without_manifest_is_treated_as_incomplete_publication(tmp_path):
    """Archive-only final state is redownloaded because its commit marker is absent."""
    state = tmp_path / "state"
    cache = state / "charts" / "oci"
    cache.mkdir(parents=True)
    (cache / f"{CHART_NAME}-{VERSION}.tgz").write_bytes(_chart_bytes())
    runner = _Runner()

    result = _ensure(runner, state)

    assert result.is_file()
    assert result.with_suffix(".json").is_file()
    assert runner.pull_count == 1


@pytest.mark.parametrize("damage", ["crc", "truncated-trailer", "deflate"])
def test_gzip_crc_or_truncated_trailer_is_rejected(tmp_path, damage):
    """The gzip stream must be fully consumed and its trailer verified."""
    archive = bytearray(_chart_bytes())
    if damage == "crc":
        archive[-8] ^= 0xFF
    elif damage == "truncated-trailer":
        del archive[-4:]
    else:
        # Gzip header followed by an invalid DEFLATE block (reserved BTYPE).
        archive = bytearray(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff\x07")
    runner = _Runner(chart_bytes=bytes(archive))

    with pytest.raises(_CHARTS.AcquisitionError):
        _ensure(runner, tmp_path / "state")

    assert runner.pull_count == 1
    cache = tmp_path / "state" / "charts" / "oci"
    assert not list(cache.glob("*.tgz"))


@pytest.mark.parametrize(
    "members",
    [
        [
            ("../escape", "file", b"x"),
            (
                f"{CHART_NAME}/Chart.yaml",
                "file",
                b"name: slurm-operator\nversion: 1.2.0\n",
            ),
        ],
        [
            (
                "other/Chart.yaml",
                "file",
                b"name: slurm-operator\nversion: 1.2.0\n",
            )
        ],
        [
            (f"{CHART_NAME}/link", "symlink", b""),
            (
                f"{CHART_NAME}/Chart.yaml",
                "file",
                b"name: slurm-operator\nversion: 1.2.0\n",
            ),
        ],
        [
            (
                f"{CHART_NAME}/Chart.yaml",
                "file",
                b"name: slurm-operator\nversion: 1.2.0\n",
            ),
            (
                f"{CHART_NAME}/Chart.yaml",
                "file",
                b"name: slurm-operator\nversion: 1.2.0\n",
            ),
        ],
        [
            (f"{CHART_NAME}/Chart.yaml", "file", b"null\n"),
            (
                f"{CHART_NAME}/Chart.yaml",
                "file",
                b"name: slurm-operator\nversion: 1.2.0\n",
            ),
        ],
        [(f"{CHART_NAME}/Chart.yaml", "file", b"name: [broken\n")],
    ],
    ids=[
        "traversal",
        "wrong-root",
        "symlink",
        "duplicate-metadata",
        "null-then-duplicate-metadata",
        "malformed-yaml",
    ],
)
def test_unsafe_or_malformed_archive_is_rejected_before_publication(tmp_path, members):
    """Unsafe paths, links, duplicate metadata, and invalid YAML are fatal."""
    runner = _Runner(chart_bytes=_archive_with_members(members))

    with pytest.raises(_CHARTS.AcquisitionError):
        _ensure(runner, tmp_path / "state")

    assert runner.pull_count == 1
    cache = tmp_path / "state" / "charts" / "oci"
    assert not list(cache.glob("*.tgz"))
