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

"""Stamped, reproducible source archives for release tags, and their publication.

Standard library only: the release workflow runs this with the token that can
publish, so it must not import third-party packages.
"""

import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

from lib.project_version import stamp_source

ARCHIVE_ROOT = "storage-scale-test"
# User-facing commands; each must print exactly the archive's VERSION.
VERSIONED_SHELL_COMMANDS = (
    "validate_env.sh",
    "storage-tests/*/nv-*.sh",
    "utils/build_tarball.sh",
    "utils/build/build_*.sh",
)
VERSIONED_PYTHON_COMMANDS = (
    "utils/extract-*.py",
    "utils/summarize-elbencho.py",
    "utils/reconstruct_elbencho_env_used.py",
    "utils/slurm/*.py",
)


def _git(root, *arguments) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def release_commit(root, tag) -> str:
    """Return the commit of an annotated vMAJOR.MINOR.PATCH tag."""
    release_tag = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1" && _project_release_tag "$2"',
            "release-tag",
            str(Path(root) / "lib/project_version.sh"),
            tag,
        ],
        check=False,
    )
    if release_tag.returncode:
        raise ValueError(f"not a release tag (vMAJOR.MINOR.PATCH): {tag}")
    if _git(root, "cat-file", "-t", f"refs/tags/{tag}") != "tag":
        raise ValueError(f"release tags must be annotated: {tag}")
    return _git(root, "rev-parse", f"refs/tags/{tag}^{{commit}}")


def changelog_notes(changelog: str, tag: str) -> str:
    """Return the CHANGELOG.md section for tag, which must exist and be nonempty."""
    version = re.escape(tag.removeprefix("v"))
    heading = re.search(rf"^## \[v?{version}\][^\n]*\n", changelog, re.MULTILINE)
    if heading is None:
        raise ValueError(f"CHANGELOG.md has no section for {tag}")
    following = re.search(r"^## ", changelog[heading.end() :], re.MULTILINE)
    end = heading.end() + following.start() if following else len(changelog)
    notes = changelog[heading.end() : end].strip()
    if not notes:
        raise ValueError(f"CHANGELOG.md section for {tag} is empty")
    return notes


def release_notes(root, tag) -> str:
    """Return the tagged commit's CHANGELOG.md section for tag."""
    commit = release_commit(root, tag)
    return changelog_notes(_git(root, "show", f"{commit}:CHANGELOG.md"), tag)


def build_source_archive(root, output, tag):
    """Archive the tagged tree, stamped with the tag, plus its .sha256 file."""
    root, output = Path(root), Path(output)
    commit = release_commit(root, tag)
    release_notes(root, tag)
    tree = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", commit],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout
    output.mkdir(parents=True, exist_ok=True)
    archive = output / f"{ARCHIVE_ROOT}-{tag}-source.tar.gz"
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary) / ARCHIVE_ROOT
        with tarfile.open(fileobj=io.BytesIO(tree)) as source:
            source.extractall(staging, filter="data")
        stamp_source(staging, tag)
        archive.write_bytes(_reproducible_archive(staging))
    return archive, _write_checksum(archive)


def _write_checksum(archive: Path) -> Path:
    checksum = archive.with_name(archive.name + ".sha256")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return checksum


def _reproducible_archive(staging: Path) -> bytes:
    """Tar and gzip with sorted paths and fixed ownership, modes, and times."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in [staging, *sorted(staging.rglob("*"))]:
            info = tar.gettarinfo(str(path), str(path.relative_to(staging.parent)))
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            executable = path.is_dir() or path.stat().st_mode & 0o111
            info.mode = 0o755 if executable else 0o644
            if info.isreg():
                with path.open("rb") as stream:
                    tar.addfile(info, stream)
            else:
                tar.addfile(info)
    return gzip.compress(buffer.getvalue(), mtime=0)


def smoke_source_archive(archive):
    """Check that the extracted archive verifies and its commands report VERSION."""
    with tempfile.TemporaryDirectory() as temporary:
        with tarfile.open(archive) as tar:
            tar.extractall(temporary, filter="data")
        root = Path(temporary) / ARCHIVE_ROOT
        expected = (root / "VERSION").read_text(encoding="utf-8").strip()
        checks = [["bash", str(root / "lib/project_version.sh"), "resolve", str(root)]]
        for interpreter, patterns in (
            ("bash", VERSIONED_SHELL_COMMANDS),
            (sys.executable, VERSIONED_PYTHON_COMMANDS),
        ):
            checks += [
                [interpreter, str(command), "--version"]
                for pattern in patterns
                for command in sorted(root.glob(pattern))
            ]
        for check in checks:
            result = subprocess.run(check, check=False, text=True, capture_output=True)
            if result.returncode or result.stdout.strip() != expected:
                raise ValueError(
                    f"{Path(check[1]).relative_to(root)} printed "
                    f"{result.stdout.strip()!r}, not {expected!r}: {result.stderr.strip()}"
                )


def publish_release(repository, tag, commit, assets, notes, run=subprocess.run):
    """Create the GitHub release, or finish a rerun without changing published bytes."""

    def gh(*arguments, check=True):
        return run(["gh", *arguments], check=check, text=True, capture_output=True)

    _require_merged_tag(gh, repository, tag, commit)
    archive, checksum = assets
    release = _existing_release(gh, repository, tag)
    if release is None:
        with tempfile.TemporaryDirectory() as temporary:
            notes_file = Path(temporary) / "notes.md"
            notes_file.write_text(notes + _ARCHIVE_NOTE, encoding="utf-8")
            gh(
                "release",
                "create",
                tag,
                str(archive),
                str(checksum),
                "--repo",
                repository,
                "--verify-tag",
                "--title",
                tag,
                "--notes-file",
                str(notes_file),
            )
        return
    published = _published_assets(gh, repository, release["assets"], assets)
    _adopt_published_assets(gh, repository, tag, published, archive, checksum)
    for asset in (archive, checksum):
        if asset.name not in published:
            gh("release", "upload", tag, str(asset), "--repo", repository)
    if release["isDraft"]:
        gh("release", "edit", tag, "--repo", repository, "--draft=false")


def _require_merged_tag(gh, repository, tag, commit):
    """The pushed tag must be this commit, already on the default branch."""
    remote = gh("api", f"repos/{repository}/commits/{tag}", "--jq", ".sha")
    if remote.stdout.strip() != commit:
        raise ValueError(f"{repository} tag {tag} does not identify {commit}")
    branch = gh("api", f"repos/{repository}", "--jq", ".default_branch")
    compare = f"repos/{repository}/compare/{branch.stdout.strip()}...{commit}"
    # "behind" or "identical": the default branch already contains the commit.
    status = gh("api", compare, "--jq", ".status").stdout.strip()
    if status not in ("behind", "identical"):
        raise ValueError(f"{tag} is not on the default branch of {repository}")


_ARCHIVE_NOTE = (
    "\n\n---\n\nThe attached source archive is stamped with VERSION and "
    "SOURCE_SHA256. GitHub's automatic source archives are not. Benchmark "
    "binaries are not included.\n"
)


def _existing_release(gh, repository, tag):
    """Return the release (including a draft) for tag, or None if there is none."""
    fields = "isDraft,assets"
    found = gh(
        "release", "view", tag, "--repo", repository, "--json", fields, check=False
    )
    if found.returncode == 0:
        return json.loads(found.stdout)
    # Only a definite 404 means absent; never create a release after other errors.
    lookup = gh("api", f"repos/{repository}/releases/tags/{tag}", check=False)
    if "HTTP 404" not in lookup.stderr:
        raise ValueError(f"cannot determine whether release {tag} exists")
    return None


def _published_assets(gh, repository, remote_assets, assets):
    """Names of our fully uploaded assets, after deleting failed-upload placeholders.

    A failed upload (for example HTTP 502) can leave an empty asset in state
    "starter" that blocks re-uploading its name; GitHub documents that it can be
    deleted. Delete it by its exact asset id. Never delete an uploaded asset, and
    refuse any other state rather than guess.
    """
    ours = {asset.name for asset in assets}
    published = set()
    for remote in remote_assets:
        name, state = remote["name"], remote.get("state")
        if name not in ours:
            continue
        if state == "uploaded":
            published.add(name)
        elif state == "starter":
            gh("api", _asset_endpoint(repository, remote), "--method", "DELETE")
        else:
            raise ValueError(f"published {name} is in unexpected state {state!r}")
    return published


def _asset_endpoint(repository, remote):
    """REST endpoint of one asset, from the numeric id at the end of its apiUrl."""
    found = re.search(r"/releases/assets/(\d+)$", remote.get("apiUrl", ""))
    if found is None:
        raise ValueError(f"cannot identify the asset id of {remote['name']}")
    return f"repos/{repository}/releases/assets/{found.group(1)}"


def _adopt_published_assets(gh, repository, tag, published, archive, checksum):
    """Keep already-published bytes; refuse if they hold different source."""
    with tempfile.TemporaryDirectory() as temporary:
        for asset in (archive, checksum):
            if asset.name not in published:
                continue
            gh(
                "release",
                "download",
                tag,
                "--repo",
                repository,
                "--pattern",
                asset.name,
                "--dir",
                temporary,
            )
            remote = (Path(temporary) / asset.name).read_bytes()
            if asset == archive:
                # Compare content: gzip bytes may differ across zlib versions.
                if gzip.decompress(remote) != gzip.decompress(archive.read_bytes()):
                    raise ValueError(f"published {asset.name} holds different source")
                archive.write_bytes(remote)
                _write_checksum(archive)
            elif remote != checksum.read_bytes():
                raise ValueError(f"published {asset.name} does not match the archive")
