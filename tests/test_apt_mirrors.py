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

"""Behavior of the fixture's optional package-mirror build configuration."""

import functools
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_LIB = _ROOT / "integration-tests/lib"
_SPEC = importlib.util.spec_from_file_location("apt_mirrors", _LIB / "apt_mirrors.py")
_MIRRORS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MIRRORS)

ARCHIVE = "https://archive-cache.example/ubuntu"
SECURITY = "https://security-cache.example/ubuntu"
PORTS = "https://ports-cache.example/ubuntu-ports"
DEB822 = """Types: deb
URIs: {archive}
Suites: noble noble-updates
Components: main
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg

Types: deb
URIs: {security}
Suites: noble-security
Components: main
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
"""
PUBLIC_AMD64 = (
    "http://archive.ubuntu.com/ubuntu/",
    "http://security.ubuntu.com/ubuntu/",
)
PUBLIC_ARM64 = ("http://ports.ubuntu.com/ubuntu-ports/",) * 2


def _ca_file(tmp_path):
    path = tmp_path / "ca.crt"
    path.write_text("-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----\n")
    return path


def test_no_mirrors_means_no_build_arguments():
    """Local runs keep the base image's public repositories."""
    assert _MIRRORS.apt_build_arguments({}) == []


def test_mirrors_and_ca_secret_are_forwarded(tmp_path):
    """Every configured mirror reaches the build, with the CA as a secret."""
    ca = _ca_file(tmp_path)
    environ = {
        _MIRRORS.ARCHIVE_ENV: ARCHIVE,
        _MIRRORS.SECURITY_ENV: SECURITY,
        _MIRRORS.PORTS_ENV: PORTS,
        _MIRRORS.CA_BUNDLE_ENV: str(ca),
    }
    assert _MIRRORS.apt_build_arguments(environ) == [
        "--build-arg",
        f"APT_ARCHIVE_MIRROR={ARCHIVE}",
        "--build-arg",
        f"APT_SECURITY_MIRROR={SECURITY}",
        "--build-arg",
        f"APT_PORTS_MIRROR={PORTS}",
        "--secret",
        f"id=apt_ca,src={ca}",
    ]


@pytest.mark.parametrize(
    "value",
    [
        "ftp://mirror.example/ubuntu",
        "https://user:pw@mirror.example/ubuntu",
        "https://mirror.example/ubuntu; rm -rf /",
        "https://mirror.example/ubuntu|x",
        "https://mirror.example/$(id)",
    ],
)
def test_unsafe_mirror_urls_are_rejected(value):
    """Configurable URLs cannot smuggle shell or sed syntax into a build."""
    with pytest.raises(ValueError, match=_MIRRORS.ARCHIVE_ENV):
        _MIRRORS.apt_build_arguments({_MIRRORS.ARCHIVE_ENV: value})


def test_https_mirror_requires_readable_ca_bundle(tmp_path):
    """Missing or non-PEM trust material is a clear configuration error."""
    environ = {_MIRRORS.ARCHIVE_ENV: ARCHIVE}
    with pytest.raises(ValueError, match=_MIRRORS.CA_BUNDLE_ENV):
        _MIRRORS.apt_build_arguments(environ)
    environ[_MIRRORS.CA_BUNDLE_ENV] = str(tmp_path / "missing")
    with pytest.raises(ValueError, match="cannot read"):
        _MIRRORS.apt_build_arguments(environ)
    empty = tmp_path / "empty"
    empty.write_text("not a certificate")
    environ[_MIRRORS.CA_BUNDLE_ENV] = str(empty)
    with pytest.raises(ValueError, match="no PEM"):
        _MIRRORS.apt_build_arguments(environ)


def test_http_mirror_needs_no_ca_bundle():
    """Plain-HTTP mirrors do not require trust bootstrap."""
    assert _MIRRORS.apt_build_arguments(
        {_MIRRORS.ARCHIVE_ENV: "http://m.example/ubuntu"}
    )


def _fake_root(tmp_path, sources, legacy=None):
    root = tmp_path / "root"
    apt = root / "etc/apt"
    (apt / "sources.list.d").mkdir(parents=True)
    (apt / "sources.list.d/ubuntu.sources").write_text(sources)
    if legacy is not None:
        (apt / "sources.list").write_text(legacy)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "apt.log"
    fake = bin_dir / "apt-get"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$APT_LOG"\n'
        'if [[ "$*" == *update* && -n "${FAIL_UPDATE:-}" ]]; then exit 100; fi\n'
        'if [[ "$*" == *update* && -n "${UPDATE_OUTPUT:-}" ]]; then\n'
        '  printf "%s\\n" "$UPDATE_OUTPUT"; fi\n'
    )
    fake.chmod(0o755)
    return root, bin_dir, log


def _run_script(tmp_path, root, bin_dir, log, extra_env, *packages):
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "APT_ROOT": str(root),
        "APT_LOG": str(log),
        "APT_ATTEMPTS": "2",
        **extra_env,
    }
    return subprocess.run(
        ["bash", str(_LIB / "apt-build.sh"), *(packages or ("pkg",))],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )


def test_amd64_sources_use_archive_and_security_mirrors(tmp_path):
    """Only URI fields change; suites, components and keys are preserved."""
    original = DEB822.format(archive=PUBLIC_AMD64[0], security=PUBLIC_AMD64[1])
    root, bin_dir, log = _fake_root(tmp_path, original)
    env = {
        "APT_ARCHIVE_MIRROR": ARCHIVE,
        "APT_SECURITY_MIRROR": SECURITY,
        "APT_PORTS_MIRROR": PORTS,
    }
    result = _run_script(tmp_path, root, bin_dir, log, env)
    assert result.returncode == 0, result.stderr
    rewritten = (root / "etc/apt/sources.list.d/ubuntu.sources").read_text()
    assert rewritten == DEB822.format(archive=ARCHIVE + "/", security=SECURITY + "/")
    calls = log.read_text().splitlines()
    assert calls[0].endswith("update")
    assert calls[1].endswith("install -y --no-install-recommends pkg")


def test_arm64_ports_source_uses_ports_mirror_for_both_suites(tmp_path):
    """arm64 images use the ports cache; the amd64 mirrors are unused."""
    original = DEB822.format(archive=PUBLIC_ARM64[0], security=PUBLIC_ARM64[1])
    root, bin_dir, log = _fake_root(tmp_path, original)
    env = {"APT_ARCHIVE_MIRROR": ARCHIVE, "APT_PORTS_MIRROR": PORTS}
    assert _run_script(tmp_path, root, bin_dir, log, env).returncode == 0
    rewritten = (root / "etc/apt/sources.list.d/ubuntu.sources").read_text()
    assert rewritten == DEB822.format(archive=PORTS + "/", security=PORTS + "/")


def test_legacy_list_sources_are_rewritten(tmp_path):
    """Images that still use sources.list get the same URI replacement."""
    legacy = "deb http://archive.ubuntu.com/ubuntu/ noble main\n"
    root, bin_dir, log = _fake_root(tmp_path, "", legacy)
    env = {"APT_ARCHIVE_MIRROR": ARCHIVE}
    assert _run_script(tmp_path, root, bin_dir, log, env).returncode == 0
    assert (root / "etc/apt/sources.list").read_text() == f"deb {ARCHIVE}/ noble main\n"


def test_unset_mirrors_leave_sources_untouched(tmp_path):
    """Without configuration the image's public sources are preserved."""
    original = DEB822.format(archive=PUBLIC_AMD64[0], security=PUBLIC_AMD64[1])
    root, bin_dir, log = _fake_root(tmp_path, original)
    assert _run_script(tmp_path, root, bin_dir, log, {}).returncode == 0
    assert (root / "etc/apt/sources.list.d/ubuntu.sources").read_text() == original


def test_unmatched_sources_fail_instead_of_using_public_repositories(tmp_path):
    """A configured mirror that cannot be applied is a build error."""
    root, bin_dir, log = _fake_root(tmp_path, "URIs: https://other.example/ubuntu\n")
    result = _run_script(tmp_path, root, bin_dir, log, {"APT_ARCHIVE_MIRROR": ARCHIVE})
    assert result.returncode != 0
    assert "no Ubuntu repository source matched" in result.stderr
    assert not log.exists()


def test_shell_metacharacters_in_mirror_are_refused(tmp_path):
    """The in-image validation holds even if Python validation is bypassed."""
    original = DEB822.format(archive=PUBLIC_AMD64[0], security=PUBLIC_AMD64[1])
    root, bin_dir, log = _fake_root(tmp_path, original)
    env = {"APT_ARCHIVE_MIRROR": "https://m.example/ubuntu|;touch pwned"}
    result = _run_script(tmp_path, root, bin_dir, log, env)
    assert result.returncode != 0
    assert "refusing it" in result.stderr
    assert not (tmp_path / "pwned").exists()


def test_update_failure_is_retried_then_propagated(tmp_path):
    """An incomplete index update never counts as success and never installs."""
    original = DEB822.format(archive=PUBLIC_AMD64[0], security=PUBLIC_AMD64[1])
    root, bin_dir, log = _fake_root(tmp_path, original)
    result = _run_script(tmp_path, root, bin_dir, log, {"FAIL_UPDATE": "1"})
    assert result.returncode != 0
    assert "apt-get update failed after 2 attempts" in result.stderr
    calls = log.read_text().splitlines()
    assert len(calls) == 2 and not any("install" in call for call in calls)


def test_ca_secret_is_used_for_https_without_being_persisted(tmp_path):
    """The trust bundle is passed to apt on the command line only."""
    original = DEB822.format(archive=PUBLIC_AMD64[0], security=PUBLIC_AMD64[1])
    root, bin_dir, log = _fake_root(tmp_path, original)
    ca = _ca_file(tmp_path)
    env = {
        "APT_ARCHIVE_MIRROR": ARCHIVE,
        "APT_SECURITY_MIRROR": SECURITY,
        "APT_CA_FILE": str(ca),
    }
    assert _run_script(tmp_path, root, bin_dir, log, env).returncode == 0
    assert f"Acquire::https::CaInfo={ca}" in log.read_text()
    assert not list((root / "etc/apt").rglob("*.conf"))
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    root, bin_dir, log = _fake_root(fresh, original)
    missing = _run_script(
        fresh, root, bin_dir, log, {**env, "APT_CA_FILE": str(tmp_path / "none")}
    )
    assert missing.returncode != 0 and "missing or empty" in missing.stderr


THIRD_PARTY_ERR = "Err:3 https://pkgs.k8s.io/core:/stable:/v1.37/deb InRelease"


def test_third_party_source_failure_does_not_fail_the_build(tmp_path):
    """apt-get exits 0 on a failed non-Ubuntu source; the build keeps going."""
    original = DEB822.format(archive=PUBLIC_AMD64[0], security=PUBLIC_AMD64[1])
    root, bin_dir, log = _fake_root(tmp_path, original)
    env = {"UPDATE_OUTPUT": THIRD_PARTY_ERR}
    result = _run_script(tmp_path, root, bin_dir, log, env)
    assert result.returncode == 0, result.stderr
    assert len(log.read_text().splitlines()) == 2


@pytest.mark.parametrize(
    "line",
    [
        "Err:1 http://archive.ubuntu.com/ubuntu noble InRelease",
        "W: Failed to fetch http://security.ubuntu.com/ubuntu/dists/noble/InRelease",
    ],
)
def test_incomplete_ubuntu_index_fails_even_though_apt_exits_zero(tmp_path, line):
    """A failed public Ubuntu index is detected from apt's output, not its exit."""
    original = DEB822.format(archive=PUBLIC_AMD64[0], security=PUBLIC_AMD64[1])
    root, bin_dir, log = _fake_root(tmp_path, original)
    result = _run_script(tmp_path, root, bin_dir, log, {"UPDATE_OUTPUT": line})
    assert result.returncode != 0
    assert "apt-get update failed after 2 attempts" in result.stderr
    assert not any("install" in call for call in log.read_text().splitlines())


def test_incomplete_index_on_a_configured_mirror_fails(tmp_path):
    """Mirror hosts are treated as Ubuntu sources whatever their names."""
    original = DEB822.format(archive=PUBLIC_AMD64[0], security=PUBLIC_AMD64[1])
    root, bin_dir, log = _fake_root(tmp_path, original)
    env = {
        "APT_ARCHIVE_MIRROR": "http://cache.example/ubuntu",
        "APT_SECURITY_MIRROR": "http://cache.example/ubuntu",
        "UPDATE_OUTPUT": "Err:1 http://cache.example/ubuntu noble InRelease",
    }
    result = _run_script(tmp_path, root, bin_dir, log, env)
    assert result.returncode != 0
    assert "update failed after 2 attempts" in result.stderr


def test_partial_mirror_configuration_cannot_leave_a_public_route(tmp_path):
    """Covering only archive while security stays public is a build error."""
    original = DEB822.format(archive=PUBLIC_AMD64[0], security=PUBLIC_AMD64[1])
    root, bin_dir, log = _fake_root(tmp_path, original)
    result = _run_script(tmp_path, root, bin_dir, log, {"APT_ARCHIVE_MIRROR": ARCHIVE})
    assert result.returncode != 0
    assert "not covered by a configured mirror" in result.stderr
    assert not log.exists()


MULTI_URI_DEB822 = (
    "Types: deb\n"
    "URIs: http://archive.ubuntu.com/ubuntu/ http://security.ubuntu.com/ubuntu/\n"
    "Suites: noble noble-security\n"
    "Components: main\n"
)
# One-line format: the public security URI is the real source; the trailing
# comment gains the archive mirror once rewritten.
MIXED_LEGACY = (
    "deb [signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg] "
    "http://security.ubuntu.com/ubuntu/ noble-security main"
    " # was http://archive.ubuntu.com/ubuntu/\n"
)


@pytest.mark.parametrize(
    ("sources", "legacy"),
    [(MULTI_URI_DEB822, None), ("", MIXED_LEGACY)],
    ids=["deb822-multi-uri", "one-line-mixed"],
)
def test_public_uri_beside_a_mirror_is_not_covered(tmp_path, sources, legacy):
    """Each URI is checked; a mirrored neighbour cannot cover a public one."""
    root, bin_dir, log = _fake_root(tmp_path, sources, legacy)
    result = _run_script(tmp_path, root, bin_dir, log, {"APT_ARCHIVE_MIRROR": ARCHIVE})
    assert result.returncode != 0
    assert "not covered by a configured mirror" in result.stderr
    assert "http://security.ubuntu.com/ubuntu/" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize(
    ("sources", "legacy", "path", "expected"),
    [
        (
            MULTI_URI_DEB822,
            None,
            "sources.list.d/ubuntu.sources",
            MULTI_URI_DEB822.replace(PUBLIC_AMD64[0], ARCHIVE + "/").replace(
                PUBLIC_AMD64[1], SECURITY + "/"
            ),
        ),
        (
            "",
            MIXED_LEGACY,
            "sources.list",
            MIXED_LEGACY.replace(PUBLIC_AMD64[0], ARCHIVE + "/").replace(
                PUBLIC_AMD64[1], SECURITY + "/"
            ),
        ),
    ],
    ids=["deb822-multi-uri", "one-line-mixed"],
)
def test_every_uri_on_a_line_mirrored_passes(tmp_path, sources, legacy, path, expected):
    """A line listing several URIs passes once each one is mirrored."""
    root, bin_dir, log = _fake_root(tmp_path, sources, legacy)
    env = {"APT_ARCHIVE_MIRROR": ARCHIVE, "APT_SECURITY_MIRROR": SECURITY}
    result = _run_script(tmp_path, root, bin_dir, log, env)
    assert result.returncode == 0, result.stderr
    assert (root / "etc/apt" / path).read_text() == expected
    calls = log.read_text().splitlines()
    assert calls[0].endswith("update")
    assert calls[1].endswith("install -y --no-install-recommends pkg")


def test_mirror_on_a_public_ubuntu_host_still_covers_its_uris(tmp_path):
    """A mirror that is itself a public Ubuntu host remains accepted."""
    original = DEB822.format(archive=PUBLIC_AMD64[0], security=PUBLIC_AMD64[0])
    root, bin_dir, log = _fake_root(tmp_path, original)
    env = {"APT_ARCHIVE_MIRROR": "https://archive.ubuntu.com/ubuntu"}
    result = _run_script(tmp_path, root, bin_dir, log, env)
    assert result.returncode == 0, result.stderr


def _sleeping_apt(bin_dir, phase, seconds):
    fake = bin_dir / "apt-get"
    fake.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$APT_LOG"\n'
        f'[[ "$*" == *{phase}* ]] && sleep {seconds}\n'
        "exit 0\n"
    )


def test_slow_successful_phase_is_not_killed_and_retried(tmp_path):
    """Useful work within the total budget is never cut off by a phase cap."""
    original = DEB822.format(archive=PUBLIC_AMD64[0], security=PUBLIC_AMD64[1])
    root, bin_dir, log = _fake_root(tmp_path, original)
    _sleeping_apt(bin_dir, "update", 2)
    env = {"APT_BUDGET_SECONDS": "30"}
    assert _run_script(tmp_path, root, bin_dir, log, env).returncode == 0
    assert len(log.read_text().splitlines()) == 2


def test_exhausted_budget_fails_the_build_clearly(tmp_path):
    """An install that outlives the whole budget stops the build."""
    original = DEB822.format(archive=PUBLIC_AMD64[0], security=PUBLIC_AMD64[1])
    root, bin_dir, log = _fake_root(tmp_path, original)
    _sleeping_apt(bin_dir, "install", 30)
    result = _run_script(tmp_path, root, bin_dir, log, {"APT_BUDGET_SECONDS": "2"})
    assert result.returncode != 0
    assert "budget of 2s exhausted" in result.stderr


def _secret_mount_options(dockerfile):
    text = (_ROOT / "integration-tests" / dockerfile).read_text()
    return re.search(r"--mount=type=secret,id=apt_ca[^ \\]*", text).group(0)


# CI sets this to make Docker-backed regressions fail instead of skipping.
REQUIRE_DOCKER_ENV = "CI_REQUIRE_DOCKER"
# The fixture driver's digest-pinned base for each Dockerfile it builds.
_DOCKERFILE_BASES = {
    "ssh-image.Dockerfile": "SSH_BASE_IMAGE",
    "slinky-login-image.Dockerfile": "SLINKY_LOGIN_BASE_IMAGE",
}


@functools.cache
def _fixture_driver():
    path = _ROOT / "integration-tests/bin/integration-test.py"
    spec = importlib.util.spec_from_file_location("apt_mirrors_fixture_driver", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _DockerRunner:  # pylint: disable=too-few-public-methods
    """The minimal runner interface the fixture's image acquisition needs."""

    @staticmethod
    def run(arguments, check, timeout, sensitive):
        """Run one Docker command and capture its text output."""
        del sensitive
        return subprocess.run(
            arguments, capture_output=True, text=True, check=check, timeout=timeout
        )


def _docker_unavailable(reason):
    if os.environ.get(REQUIRE_DOCKER_ENV) == "1":
        pytest.fail(f"{reason}, but {REQUIRE_DOCKER_ENV}=1 requires Docker tests")
    pytest.skip(f"{reason}; set {REQUIRE_DOCKER_ENV}=1 to pull and require it")


def _pinned_base_image(dockerfile):
    """Return the fixture's verified, digest-pinned base for one Dockerfile."""
    driver = _fixture_driver()
    base = getattr(driver, _DOCKERFILE_BASES[dockerfile])
    _, _, digest = base.rpartition("@")
    assert digest.startswith("sha256:"), base
    if shutil.which("docker") is None:
        _docker_unavailable("docker is unavailable")
    server = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Arch}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if server.returncode:
        _docker_unavailable("the Docker daemon is unreachable")
    architecture = server.stdout.strip()
    acquisition = driver.image_acquisition
    if os.environ.get(REQUIRE_DOCKER_ENV) == "1":
        acquisition.ensure_pinned_image(_DockerRunner(), base, architecture)
    elif not acquisition.inspect_pinned_image(
        _DockerRunner(), base, digest, architecture
    ):
        _docker_unavailable(f"pinned base {base} is not present locally")
    return base


@pytest.mark.parametrize("name", sorted(_DOCKERFILE_BASES))
def test_ca_secret_mount_is_readable_by_the_apt_sandbox_user(tmp_path, name):
    """A real BuildKit mount with the Dockerfile's own options is _apt-readable.

    _apt performs the HTTPS downloads, and a default secret mount is root-only.
    Each Dockerfile is exercised on the same digest-pinned base the fixture
    builds it from, never on whatever mutable tag a host happens to cache.
    """
    base = _pinned_base_image(name)
    ca = _ca_file(tmp_path)
    (tmp_path / "Dockerfile").write_text(
        f"FROM {base}\n"
        f"RUN {_secret_mount_options(name)} \\\n"
        "    runuser -u _apt -- test -r /run/secrets/apt_ca\n"
    )
    result = subprocess.run(
        ["docker", "build", "--no-cache", "--secret", f"id=apt_ca,src={ca}", tmp_path],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "DOCKER_BUILDKIT": "1"},
    )
    assert result.returncode == 0, result.stderr[-2000:]


@pytest.mark.parametrize(
    "name", ["ssh-image.Dockerfile", "slinky-login-image.Dockerfile"]
)
def test_package_dockerfiles_configure_mirrors_before_first_apt_use(name):
    """Package-installing images route through the wrapper, not bare apt-get."""
    text = (_ROOT / "integration-tests" / name).read_text()
    assert "apt-get" not in text
    assert "bash /tmp/apt-build.sh" in text
    assert "ARG APT_ARCHIVE_MIRROR=" in text
