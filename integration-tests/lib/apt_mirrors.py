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

"""Optional Ubuntu package mirrors for the fixture's Docker image builds.

CI points package-installing fixture builds at runner-local repository caches
through environment variables. Without them, builds keep the base image's own
public repositories, so local runs are unchanged.
"""

import re
from collections.abc import Mapping
from pathlib import Path

ARCHIVE_ENV = "INTEGRATION_APT_ARCHIVE_MIRROR"
SECURITY_ENV = "INTEGRATION_APT_SECURITY_MIRROR"
PORTS_ENV = "INTEGRATION_APT_PORTS_MIRROR"
CA_BUNDLE_ENV = "INTEGRATION_APT_CA_BUNDLE"
CA_SECRET_ID = "apt_ca"

# Environment variable -> Dockerfile build argument consumed by apt-build.sh.
_MIRRORS = (
    (ARCHIVE_ENV, "APT_ARCHIVE_MIRROR"),
    (SECURITY_ENV, "APT_SECURITY_MIRROR"),
    (PORTS_ENV, "APT_PORTS_MIRROR"),
)
# Kept in step with URL_PATTERN in apt-build.sh, which re-validates in the image.
_MIRROR_URL = re.compile(
    r"https?://[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?"
)


def _mirror_value(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "").strip()
    if value and not _MIRROR_URL.fullmatch(value):
        raise ValueError(f"{name} must be a plain http(s) URL without credentials")
    return value


def _ca_bundle(environ: Mapping[str, str]) -> Path:
    name = environ.get(CA_BUNDLE_ENV, "").strip()
    if not name:
        raise ValueError(
            f"{CA_BUNDLE_ENV} is required when an https package mirror is set"
        )
    if "," in name:
        raise ValueError(f"{CA_BUNDLE_ENV} must not contain a comma")
    path = Path(name)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        raise ValueError(f"cannot read {CA_BUNDLE_ENV} {path}: {error}") from error
    if "BEGIN CERTIFICATE" not in text:
        raise ValueError(f"{CA_BUNDLE_ENV} {path} contains no PEM certificates")
    return path


def apt_build_arguments(environ: Mapping[str, str]) -> list[str]:
    """Return docker build arguments that route APT through configured mirrors.

    No mirrors configured yields no arguments. HTTPS mirrors additionally need
    a CA bundle, passed as a BuildKit secret so it is never stored in an image.
    """
    arguments: list[str] = []
    secure = False
    for variable, build_argument in _MIRRORS:
        value = _mirror_value(environ, variable)
        if value:
            arguments.extend(("--build-arg", f"{build_argument}={value}"))
            secure = secure or value.startswith("https://")
    if secure:
        bundle = _ca_bundle(environ)
        arguments.extend(("--secret", f"id={CA_SECRET_ID},src={bundle}"))
    return arguments
