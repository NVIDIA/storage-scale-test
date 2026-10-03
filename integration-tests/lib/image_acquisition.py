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

"""Verified host-Docker acquisition with bounded transient registry retries."""

import json
import logging
import re
import secrets
import subprocess
import time
from collections.abc import Sequence
from typing import Any

LOG = logging.getLogger("storage-scale-integration")
IMAGE_PULL_ATTEMPTS = 4
IMAGE_PULL_TIMEOUT_SECONDS = 60
IMAGE_PULL_DEADLINE_SECONDS = 240
IMAGE_PULL_INITIAL_BACKOFF_SECONDS = 10
IMAGE_PULL_JITTER_MILLISECONDS = 5000
IMAGE_INSPECT_TIMEOUT_SECONDS = 30
MAX_ERROR_DETAIL = 8000


class AcquisitionError(RuntimeError):
    """A pinned image or chart could not be safely acquired."""


def sanitize_acquisition_evidence(detail: str | bytes | None) -> str:
    """Retain bounded registry evidence without credentials or signed URL tokens."""
    output = detail or ""
    if isinstance(output, bytes):
        output = output.decode("utf-8", errors="replace")
    output = re.sub(
        r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+",
        _sanitize_url,
        output,
    )
    output = re.sub(
        r"""(?im)(["']?authorization["']?\s*[:=]\s*)[^\r\n]+""",
        r"\1[redacted]",
        output,
    )
    output = re.sub(
        r"(?i)(\bbearer\s+)[^\s\"',;]+",
        r"\1[redacted]",
        output,
    )
    return output.strip()[-MAX_ERROR_DETAIL:]


def _sanitize_url(match: re.Match) -> str:
    """Keep registry host/path while removing userinfo, query, and fragment."""
    scheme, remainder = match.group().split("://", 1)
    remainder = remainder.split("?", 1)[0].split("#", 1)[0]
    authority, separator, path = remainder.partition("/")
    authority = authority.rpartition("@")[2]
    return f"{scheme}://{authority}{separator}{path}"


def _error_detail(stdout: str | bytes | None, stderr: str | bytes | None) -> str:
    """Sanitize captured command evidence without losing the final error."""
    return sanitize_acquisition_evidence(stderr or stdout)


def retry_delay(attempt: int) -> float:
    """Return exponential delay with jitter after a failed acquisition attempt."""
    return IMAGE_PULL_INITIAL_BACKOFF_SECONDS * (2 ** (attempt - 1)) + (
        secrets.randbelow(IMAGE_PULL_JITTER_MILLISECONDS) / 1000
    )


def inspect_pinned_image(
    runner: Any,
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
            sensitive=True,
        )
    except subprocess.TimeoutExpired as error:
        raise AcquisitionError(
            f"Docker timed out inspecting image {reference} after {timeout:.1f} seconds: "
            f"{_error_detail(error.stdout, error.stderr)}"
        ) from error
    if result.returncode:
        detail = (result.stderr or result.stdout).lower()
        if result.returncode == 1 and (
            "no such image" in detail or "no such object" in detail
        ):
            return False
        raise AcquisitionError(
            f"Docker could not inspect image {reference}: "
            f"{_error_detail(result.stdout, result.stderr)}"
        )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise AcquisitionError(
            f"Docker returned invalid metadata for {reference}"
        ) from error
    if not isinstance(document, dict) or not isinstance(
        document.get("RepoDigests", []), list
    ):
        raise AcquisitionError(f"Docker returned invalid metadata for {reference}")
    repo_digests = document.get("RepoDigests", [])
    if document.get("Architecture") != architecture or not any(
        isinstance(item, str) and item.endswith(f"@{digest}") for item in repo_digests
    ):
        return False
    return True


def transient_acquisition_failure(detail: str) -> bool:
    """Return whether a registry failure is suitable for bounded retry."""
    lowered = detail.lower()
    if any(
        phrase in lowered
        for phrase in (
            "unauthorized",
            "authentication required",
            "access denied",
            "permission denied",
            "forbidden",
            "manifest unknown",
            "not found",
            "no space left",
            "digest mismatch",
            "invalid reference",
            "invalid checksum",
            "pull access denied",
            "certificate",
            "x509:",
            "unsupported protocol",
        )
    ) or re.search(r"\b(?:401|403|404)\b", lowered):
        return False
    phrases = (
        "bad gateway",
        "service unavailable",
        "too many requests",
        "toomanyrequests",
        "timeout",
        "timed out",
        "deadline exceeded",
        "connection reset",
        "connection refused",
        "connection aborted",
        "connection closed",
        "connection timed out",
        "failed to connect",
        "network is unreachable",
        "no route to host",
        "temporary failure",
        "server misbehaving",
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
    runner: Any,
    reference: str,
    digest: str,
    architecture: str,
    deadline: float,
) -> str | None:
    """Pull and verify one image, returning its final error when unavailable."""
    started = time.monotonic()
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
                sensitive=True,
            )
            if pulled.returncode == 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return (
                        f"image acquisition exceeded "
                        f"{IMAGE_PULL_DEADLINE_SECONDS} seconds"
                    )
                if inspect_pinned_image(
                    runner,
                    reference,
                    digest,
                    architecture,
                    timeout=min(float(IMAGE_INSPECT_TIMEOUT_SECONDS), remaining),
                ):
                    return None
                raise AcquisitionError(
                    "Docker pulled the wrong digest or architecture for " f"{reference}"
                )
            detail = _error_detail(pulled.stdout, pulled.stderr) or (
                f"docker pull exited {pulled.returncode}"
            )
        except subprocess.TimeoutExpired as error:
            detail = (
                f"timed out after {pull_timeout:.1f} seconds: "
                f"{_error_detail(error.stdout, error.stderr)}"
            )
        if attempt == IMAGE_PULL_ATTEMPTS or not transient_acquisition_failure(detail):
            return detail
        remaining = deadline - time.monotonic()
        delay = retry_delay(attempt)
        if delay >= remaining:
            return (
                f"{detail}; insufficient retry time within the "
                "image acquisition deadline"
            )
        LOG.warning(
            "Image pull attempt %d/%d failed for %s after %.2fs: %s; retrying in %.2fs",
            attempt,
            IMAGE_PULL_ATTEMPTS,
            reference,
            time.monotonic() - started,
            detail[-MAX_ERROR_DETAIL:],
            delay,
        )
        time.sleep(delay)
    raise AssertionError("image pull retry loop exhausted without a result")


def acquire_pinned_image(
    runner: Any, references: Sequence[str], digest: str, architecture: str
) -> str:
    """Reuse or pull one verified platform image from ordered references."""
    deadline = time.monotonic() + IMAGE_PULL_DEADLINE_SECONDS
    for reference in references:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AcquisitionError(
                f"image acquisition exceeded {IMAGE_PULL_DEADLINE_SECONDS} seconds"
            )
        if inspect_pinned_image(
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
            raise AcquisitionError(
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
        failures.append(f"{reference}: {failure[-MAX_ERROR_DETAIL:]}")
    raise AcquisitionError("could not obtain pinned image; " + " | ".join(failures))


def ensure_pinned_image(runner: Any, reference: str, architecture: str) -> str:
    """Reuse or pull one digest-qualified image for the current architecture."""
    repository, separator, digest = reference.rpartition("@")
    if not separator or not repository or not digest.startswith("sha256:"):
        raise AcquisitionError(
            f"fixture base image is not digest-qualified: {reference}"
        )
    return acquire_pinned_image(runner, (reference,), digest, architecture)
