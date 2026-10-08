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

"""Stamped deployment tarballs, release source archives, and release publication."""

import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from lib.project_version import ROOT, resolve_version
from lib.source_release import (
    build_source_archive,
    changelog_notes,
    publish_release,
    release_commit,
    smoke_source_archive,
)
from tests.test_project_version import git

CHANGELOG = """# Changelog

## [Unreleased]

## [1.2.3] - 2026-10-03

### Fixed

- Something.

## [1.2.2] - 2026-09-01

- Older.
"""


def _deployment_source(tmp_path):
    """Build an isolated checkout with user-provided binaries and no downloads."""
    root = tmp_path / "tree with 'spaces'"
    (root / "utils").mkdir(parents=True)
    (root / "lib").mkdir()
    shutil.copy2(ROOT / "utils/build_tarball.sh", root / "utils/build_tarball.sh")
    shutil.copy2(ROOT / "lib/project_version.sh", root / "lib/project_version.sh")
    (root / "NOTICE").write_text("notice\n")
    (root / "source.txt").write_text("source\n")
    (root / ".gitignore").write_text("utils/elbencho*\nutils/warp*\nutils/s3test*\n")
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", "Source")
    git(root, "tag", "-a", "v1.2.3", "-m", "release")
    for name in ("elbencho", "elbencho.aarch64", "warp", "warp.aarch64"):
        (root / "utils" / name).write_bytes(b"custom binary " + name.encode())
    for name in ("s3test", "s3test.aarch64"):
        (root / "utils" / name).write_bytes(b"custom binary")
    for binary in (root / "utils").glob("[ews]*"):
        binary.chmod(0o755)
    return root


def _package(root, *arguments):
    result = subprocess.run(
        ["bash", str(root / "utils/build_tarball.sh"), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archive = root.parent / "storage-scale-test.tar.gz"
    extracted = root.parent / f"extracted-{len(list(root.parent.iterdir()))}"
    with tarfile.open(archive) as tar:
        tar.extractall(extracted, filter="data")
    return extracted / "storage-scale-test"


def test_deployment_tarball_is_stamped_and_keeps_custom_binaries(tmp_path):
    root = _deployment_source(tmp_path)
    deployment = _package(root)
    assert not (root / "VERSION").exists()
    assert resolve_version(deployment) == "v1.2.3"
    listed = (deployment / "SOURCE_SHA256").read_text()
    assert "  source.txt\n" in listed and "utils/elbencho" not in listed
    assert (deployment / "utils/elbencho").read_bytes() == b"custom binary elbencho"
    (deployment / "utils/elbencho").write_bytes(b"replaced by the user")
    assert resolve_version(deployment) == "v1.2.3"
    (deployment / "source.txt").write_text("edited\n")
    assert resolve_version(deployment) == "v1.2.3-modified"
    # Deployments omit the builder; repackaging an edited one stays modified.
    shutil.copy2(root / "utils/build_tarball.sh", deployment / "utils/build_tarball.sh")
    repackaged = _package(deployment)
    assert (repackaged / "VERSION").read_text() == "v1.2.3-modified\n"
    assert resolve_version(repackaged) == "v1.2.3-modified"


LOCAL_ONLY = (
    ".obj_auth",
    "env.sh",
    "overrides/workload.env",
    "results/run/output.txt",
    ".claude/settings.json",
    ".cursor/rules",
    "utils/build/s3test.log",
)


def test_deployment_tarball_leaves_out_credentials_and_local_state(tmp_path):
    root = _deployment_source(tmp_path)
    for name in LOCAL_ONLY:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("export WARP_SECRET_KEY=do-not-ship\n")
    deployment = _package(root)
    shipped = {str(path.relative_to(deployment)) for path in deployment.rglob("*")}
    assert not shipped & set(LOCAL_ONLY)
    assert not {"overrides", "results", ".claude", ".cursor"} & shipped
    assert "source.txt" in shipped
    _assert_custom_binaries(root, deployment)


BINARY_NAMES = (
    "elbencho",
    "elbencho.aarch64",
    "warp",
    "warp.aarch64",
    "s3test",
    "s3test.aarch64",
)
CREDENTIAL_MARKER = b"unique-packaging-credential-do-not-ship"


def _assert_custom_binaries(source, deployment):
    for name in BINARY_NAMES:
        assert (deployment / "utils" / name).read_bytes() == (
            source / "utils" / name
        ).read_bytes()


def _assert_no_credential_payloads(root):
    """Inspect file and hardlink payloads, not just member names."""
    with tarfile.open(root.parent / "storage-scale-test.tar.gz") as archive:
        for member in archive.getmembers():
            if member.isreg() or member.islnk():
                assert CREDENTIAL_MARKER not in archive.extractfile(member).read()


@pytest.mark.parametrize("declaration", ["flag", "environment"])
@pytest.mark.parametrize("location", ["inside", "outside", "symlink-parent"])
def test_deployment_excludes_declared_credentials_and_aliases(
    tmp_path, monkeypatch, declaration, location
):
    root = _deployment_source(tmp_path)
    monkeypatch.delenv("OBJ_AUTH_FILE", raising=False)
    parent = tmp_path if location == "outside" else root
    credential = parent / "private [keys]*?" / "auth file.env"
    credential.parent.mkdir()
    credential.write_bytes(CREDENTIAL_MARKER)
    if location == "symlink-parent":
        (root / "linked keys").symlink_to(
            credential.parent.relative_to(root), target_is_directory=True
        )
        declared = "linked keys/auth file.env"
    else:
        declared = (
            str(credential.relative_to(root))
            if location == "inside"
            else str(credential)
        )
    (root / "credential hardlink.env").hardlink_to(credential)
    (root / "credential symlink.env").symlink_to(credential)
    # A wildcard interpretation of the declared path would match this neighbor.
    neighbor = root / "private keys" / "auth file.env"
    neighbor.parent.mkdir()
    neighbor.write_bytes(b"ordinary source")
    (root / "env.sh").write_text('export OBJ_AUTH_FILE="never-execute-this-config"\n')
    if declaration == "environment":
        monkeypatch.setenv("OBJ_AUTH_FILE", declared)
        arguments = ()
    else:
        arguments = ("--obj-auth-file", declared)
    deployment = _package(root, *arguments)
    assert not (deployment / "credential hardlink.env").exists()
    assert not (deployment / "credential symlink.env").is_symlink()
    if location == "inside":
        assert not (deployment / credential.relative_to(root)).exists()
    assert (
        deployment / neighbor.relative_to(root)
    ).read_bytes() == neighbor.read_bytes()
    assert credential.read_bytes() == CREDENTIAL_MARKER
    _assert_no_credential_payloads(root)
    _assert_custom_binaries(root, deployment)


@pytest.mark.parametrize("git_metadata", [True, False])
def test_deployment_excludes_default_aliases_and_multiple_declared_files(
    tmp_path, git_metadata
):
    root = _deployment_source(tmp_path)
    if not git_metadata:
        shutil.rmtree(root / ".git")
    credentials = [root / ".obj_auth", root / "one auth.env", root / "two auth.env"]
    for index, credential in enumerate(credentials):
        credential.write_bytes(CREDENTIAL_MARKER)
        (root / f"alias-{index}.env").hardlink_to(credential)
    (root / "default link.env").symlink_to(credentials[0])
    deployment = _package(
        root, "--obj-auth-file", "one auth.env", "--obj-auth-file", "two auth.env"
    )
    for credential in credentials:
        assert not (deployment / credential.name).exists()
    assert not list(deployment.glob("alias-*.env"))
    assert not (deployment / "default link.env").is_symlink()
    _assert_no_credential_payloads(root)
    _assert_custom_binaries(root, deployment)


# An inherited OBJ_AUTH_FILE that does not exist here has nothing to exclude.
@pytest.mark.parametrize("configuration", ["absent", "present"])
@pytest.mark.parametrize("inherited", [None, "", "missing.env", "/no/such/auth"])
def test_no_argument_deployment_protects_default_without_executing_configuration(
    tmp_path, monkeypatch, configuration, inherited
):
    root = _deployment_source(tmp_path)
    monkeypatch.delenv("OBJ_AUTH_FILE", raising=False)
    if inherited is not None:
        monkeypatch.setenv("OBJ_AUTH_FILE", inherited)
    sentinel = tmp_path / "configuration-executed"
    if configuration == "present":
        (root / "env.sh").write_text(
            f"export OBJ_AUTH_FILE=\"$(touch '{sentinel}')\"\n"
        )
    (root / ".obj_auth").write_bytes(CREDENTIAL_MARKER)
    (root / "default alias.env").hardlink_to(root / ".obj_auth")
    (root / "default link.env").symlink_to(root / ".obj_auth")
    deployment = _package(root)
    assert not sentinel.exists()
    assert not (deployment / "default alias.env").exists()
    assert not (deployment / "default link.env").is_symlink()
    _assert_no_credential_payloads(root)
    _assert_custom_binaries(root, deployment)


@pytest.mark.parametrize(
    "arguments, inherited",
    [
        (("--obj-auth-file", "missing.env"), None),
        (("--obj-auth-file", "utils"), None),
        (("--obj-auth-file", ""), None),
        (("--obj-auth-file",), None),
        ((), "utils"),
    ],
)
def test_credential_preflight_fails_before_building_or_replacing_archive(
    tmp_path, monkeypatch, arguments, inherited
):
    root = _deployment_source(tmp_path)
    monkeypatch.delenv("OBJ_AUTH_FILE", raising=False)
    if inherited is not None:
        monkeypatch.setenv("OBJ_AUTH_FILE", inherited)
    sentinel = tmp_path / "configuration-executed"
    (root / "env.sh").write_text(f"export OBJ_AUTH_FILE=\"$(touch '{sentinel}')\"\n")
    (root / ".obj_auth").write_bytes(CREDENTIAL_MARKER)
    archive = root.parent / "storage-scale-test.tar.gz"
    archive.write_bytes(b"previous deployment archive")
    side_effect = tmp_path / "download-or-build-executed"
    commands = tmp_path / "commands"
    commands.mkdir()
    for name in ("curl", "cc", "gcc", "aarch64-linux-gnu-gcc", "docker"):
        command = commands / name
        command.write_text(
            '#!/bin/sh\nprintf touched > "$PACKAGING_SIDE_EFFECT"\nexit 99\n'
        )
        command.chmod(0o755)
    monkeypatch.setenv("PACKAGING_SIDE_EFFECT", str(side_effect))
    monkeypatch.setenv("PATH", str(commands), prepend=":")
    for name in BINARY_NAMES:
        (root / "utils" / name).unlink()
    result = subprocess.run(
        ["bash", str(root / "utils/build_tarball.sh"), *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert archive.read_bytes() == b"previous deployment archive"
    assert not sentinel.exists()
    assert not side_effect.exists()
    assert not any((root / "utils" / name).exists() for name in BINARY_NAMES)


@pytest.mark.parametrize("symlink", [False, True])
def test_deployment_rejects_an_output_directory_without_changing_it(
    tmp_path, monkeypatch, symlink
):
    root = _deployment_source(tmp_path)
    monkeypatch.delenv("OBJ_AUTH_FILE", raising=False)
    output = root.parent / "storage-scale-test.tar.gz"
    target = tmp_path / "existing output directory" if symlink else output
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_bytes(b"existing directory contents")
    if symlink:
        output.symlink_to(target, target_is_directory=True)
    result = subprocess.run(
        ["bash", str(root / "utils/build_tarball.sh")],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "directory" in result.stderr.lower()
    assert output.is_dir()
    assert output.is_symlink() == symlink
    assert list(target.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"existing directory contents"


def _release_repository(tmp_path, name="release"):
    """A minimal checkout whose commands report the project version."""
    root = tmp_path / name
    (root / "lib").mkdir(parents=True)
    (root / "utils").mkdir()
    for library in ("__init__.py", "project_version.py", "project_version.sh"):
        shutil.copy2(ROOT / "lib" / library, root / "lib" / library)
    (root / "validate_env.sh").write_text(
        '#!/usr/bin/env bash\nsource "$(dirname "$0")/lib/project_version.sh"\n'
        'project_version_option "$(dirname "$0")" "$@"\nexit 2\n'
    )
    (root / "utils/extract-elbencho.py").write_text(
        "import argparse, sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1]))\n"
        "from lib.project_version import add_version_argument\n"
        "parser = argparse.ArgumentParser()\nadd_version_argument(parser)\n"
        "parser.parse_args()\nsys.exit(2)\n"
    )
    (root / "CHANGELOG.md").write_text(CHANGELOG)
    git(root, "init", "-q")
    git(root, "add", ".")
    git(root, "commit", "-qm", "Source")
    git(root, "tag", "-a", "v1.2.3", "-m", "release")
    return root


def test_release_archive_is_reproducible_stamped_and_verified(tmp_path):
    source = _release_repository(tmp_path)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", source.as_uri(), str(clone)], check=True)
    (clone / "untracked.txt").write_text("not released")
    first, checksum = build_source_archive(source, tmp_path / "first", "v1.2.3")
    second, _ = build_source_archive(clone, tmp_path / "second", "v1.2.3")
    assert first.name == "storage-scale-test-v1.2.3-source.tar.gz"
    assert first.read_bytes() == second.read_bytes()
    assert checksum.read_text().endswith(f"  {first.name}\n")
    with tarfile.open(first) as tar:
        names = tar.getnames()
        assert tar.extractfile("storage-scale-test/VERSION").read() == b"v1.2.3\n"
        assert {member.mtime for member in tar.getmembers()} == {0}
    assert "storage-scale-test/untracked.txt" not in names
    smoke_source_archive(first)


@pytest.mark.parametrize(
    "command, body",
    [
        ("validate_env.sh", "echo v9.9.9\n"),
        ("utils/extract-elbencho.py", "print('v9.9.9')\n"),
    ],
)
def test_smoke_test_rejects_a_command_with_the_wrong_version(tmp_path, command, body):
    root = _release_repository(tmp_path)
    (root / command).write_text(body)
    git(root, "commit", "-qam", "Break")
    git(root, "tag", "-a", "v1.2.3", "-f", "-m", "release")
    archive, _ = build_source_archive(root, tmp_path / "out", "v1.2.3")
    with pytest.raises(ValueError, match=f"{command} printed 'v9.9.9'"):
        smoke_source_archive(archive)


@pytest.mark.parametrize(
    "tag, message",
    [
        ("v1.2.4", "annotated"),
        ("v1.2.3+build.1", "not a release tag"),
        ("v1.2.5", "no section"),
    ],
)
def test_release_tags_must_be_annotated_releases_with_notes(tmp_path, tag, message):
    root = _release_repository(tmp_path)
    if tag == "v1.2.4":
        git(root, "tag", tag)
    else:
        git(root, "tag", "-a", tag, "-m", tag)
    with pytest.raises(ValueError, match=message):
        build_source_archive(root, tmp_path / "out", tag)


def test_changelog_notes_are_the_tag_section():
    assert changelog_notes(CHANGELOG, "v1.2.3") == "### Fixed\n\n- Something."
    assert changelog_notes(CHANGELOG, "v1.2.2") == "- Older."
    with pytest.raises(ValueError, match="empty"):
        changelog_notes("## [1.0.0]\n\n## [0.9.0]\n- x\n", "v1.0.0")


def test_release_commit_is_the_tagged_commit(tmp_path):
    root = _release_repository(tmp_path)
    assert release_commit(root, "v1.2.3") == git(root, "rev-parse", "HEAD")


class GitHub:
    """Answer the GitHub CLI calls publish_release makes, without network access."""

    def __init__(self, *, release=None, assets=None, tag_commit="abc", status="behind"):
        self.release = release
        self.assets = assets or {}
        # Asset name -> state; names not listed were uploaded successfully.
        self.states = {}
        self.tag_commit = tag_commit
        self.status = status
        self.lookup_error = "HTTP 404: Not Found"
        self.delete_fails = False
        self.calls = []

    def __call__(self, arguments, check=False, **_kwargs):
        self.calls.append(arguments[1:])
        output, error, status = self._answer(arguments[1:])
        if check and status:
            raise subprocess.CalledProcessError(status, arguments, output, error)
        return SimpleNamespace(returncode=status, stdout=output, stderr=error)

    def _asset_list(self):
        return [
            {
                "name": name,
                "state": self.states.get(name, "uploaded"),
                "id": f"RA_node{index}",
                "apiUrl": f"https://api.github.com/repos/Example/Project/"
                f"releases/assets/{100 + index}",
            }
            for index, name in enumerate(self.assets)
        ]

    def _delete(self, endpoint):
        if self.delete_fails:
            return "", "HTTP 500: Server Error", 1
        index = int(endpoint.rsplit("/", 1)[1]) - 100
        name = list(self.assets)[index]
        del self.assets[name]
        self.states.pop(name, None)
        return "", "", 0

    def _answer(self, arguments):
        if arguments[0] == "api":
            endpoint = arguments[1]
            if "--method" in arguments:
                return self._delete(endpoint)
            if "/commits/" in endpoint:
                return self.tag_commit, "", 0
            if "/compare/" in endpoint:
                return self.status, "", 0
            if "/releases/tags/" in endpoint:
                return "", self.lookup_error, 1
            return "main", "", 0
        if arguments[:2] == ["release", "view"]:
            if self.release is None:
                return "", "release not found", 1
            assets = self._asset_list()
            return json.dumps({**self.release, "assets": assets}), "", 0
        if arguments[:2] == ["release", "download"]:
            name = arguments[arguments.index("--pattern") + 1]
            directory = Path(arguments[arguments.index("--dir") + 1])
            (directory / name).write_bytes(self.assets[name])
        return "", "", 0

    def operations(self):
        """Release subcommands in call order."""
        return [call[1] for call in self.calls if call[0] == "release"]

    def deletions(self):
        """Endpoints deleted through the REST API, in call order."""
        return [
            call[1] for call in self.calls if call[:1] == ["api"] and "DELETE" in call
        ]

    def uploads(self):
        """Local files uploaded to an existing release, in call order."""
        return [call[3] for call in self.calls if call[:2] == ["release", "upload"]]


@pytest.fixture(name="assets")
def assets_fixture(tmp_path):
    archive = tmp_path / "storage-scale-test-v1.2.3-source.tar.gz"
    archive.write_bytes(gzip.compress(b"tar contents", mtime=0))
    checksum = tmp_path / (archive.name + ".sha256")
    checksum.write_text("digest  archive\n")
    return archive, checksum


def _publish(github, assets, tag="v1.2.3"):
    publish_release("example/project", tag, "abc", assets, "notes", run=github)


@pytest.mark.parametrize("tag", ["v1.2.3", "v1.2.3-rc.1"])
def test_new_release_is_created_with_assets_and_notes(assets, tag):
    github = GitHub()
    _publish(github, assets, tag)
    create = next(call for call in github.calls if call[:2] == ["release", "create"])
    assert [str(path) for path in assets] == create[3:5]
    assert ("--prerelease" in create) == ("-" in tag)
    assert github.operations() == ["view", "create"]


@pytest.mark.parametrize(
    "github, message",
    [
        (GitHub(tag_commit="def"), "does not identify"),
        (GitHub(status="diverged"), "not on the default branch"),
        (GitHub(status="ahead"), "not on the default branch"),
    ],
)
def test_release_requires_the_merged_tagged_commit(assets, github, message):
    with pytest.raises(ValueError, match=message):
        _publish(github, assets)
    assert "create" not in github.operations()


def test_release_lookup_failure_never_creates_a_release(assets):
    github = GitHub()
    github.lookup_error = "HTTP 401: Bad credentials"
    with pytest.raises(ValueError, match="cannot determine"):
        _publish(github, assets)
    assert "create" not in github.operations()


def test_rerun_keeps_published_archive_and_finishes_a_draft(assets):
    archive, checksum = assets
    # Same tar content, different gzip bytes (another zlib): keep the published one.
    published = gzip.compress(b"tar contents", compresslevel=1, mtime=0)
    assert published != archive.read_bytes()
    draft = {"isDraft": True, "isPrerelease": False}
    github = GitHub(release=draft, assets={archive.name: published})
    _publish(github, assets)
    assert archive.read_bytes() == published
    digest = hashlib.sha256(published).hexdigest()
    assert checksum.read_text() == f"{digest}  {archive.name}\n"
    assert github.uploads() == [str(checksum)]
    assert not github.deletions()
    assert github.operations()[-1] == "edit"


@pytest.mark.parametrize("different", ["archive", "checksum", "prerelease"])
def test_rerun_refuses_different_published_content(assets, different):
    archive, checksum = assets
    published = {archive.name: archive.read_bytes()}
    release = {"isDraft": False, "isPrerelease": different == "prerelease"}
    if different == "archive":
        published[archive.name] = gzip.compress(b"other source", mtime=0)
    elif different == "checksum":
        published[checksum.name] = b"0000  other\n"
    github = GitHub(release=release, assets=published)
    with pytest.raises(ValueError):
        _publish(github, assets)
    assert not {"upload", "edit", "create"} & set(github.operations())
    assert not github.deletions()


DRAFT = {"isDraft": True, "isPrerelease": False}
ASSET_ENDPOINT = "repos/example/project/releases/assets/{}"


@pytest.mark.parametrize("failed", ["archive", "checksum"])
def test_rerun_replaces_a_failed_upload_placeholder(assets, failed):
    """An HTTP 502 upload leaves an empty "starter" asset; replace only that one."""
    archive, checksum = assets
    original = archive.read_bytes()
    published = {archive.name: original}
    if failed == "archive":
        published[archive.name] = b""
    else:
        published[checksum.name] = b""
    github = GitHub(release=DRAFT, assets=published)
    github.states[archive.name if failed == "archive" else checksum.name] = "starter"
    _publish(github, assets)
    index = 0 if failed == "archive" else 1
    assert github.deletions() == [ASSET_ENDPOINT.format(100 + index)]
    expected = [archive, checksum] if failed == "archive" else [checksum]
    assert github.uploads() == [str(path) for path in expected]
    # The successfully uploaded archive is still downloaded and validated.
    assert ("download" in github.operations()) == (failed == "checksum")
    assert archive.read_bytes() == original
    assert github.operations()[-1] == "edit"


@pytest.mark.parametrize("problem", ["delete fails", "unknown state"])
def test_rerun_stops_when_a_placeholder_cannot_be_replaced(assets, problem):
    archive, _ = assets
    github = GitHub(release=DRAFT, assets={archive.name: b""})
    if problem == "delete fails":
        github.states[archive.name] = "starter"
        github.delete_fails = True
        error = subprocess.CalledProcessError
    else:
        github.states[archive.name] = "open"
        error = ValueError
    with pytest.raises(error):
        _publish(github, assets)
    assert not {"upload", "edit", "create"} & set(github.operations())
