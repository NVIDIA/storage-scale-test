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

"""Project versions from Git and stamped trees, as commands and reports see them."""

import os
import subprocess
import shutil
import sys

import pytest

from lib.project_version import (
    ROOT,
    exit_for_information,
    resolve_version,
    stamp_source,
)
from lib.report_provenance import (
    ResultVersions,
    result_version,
    selected_versions,
    with_versions,
)
from lib.source_release import VERSIONED_PYTHON_COMMANDS, VERSIONED_SHELL_COMMANDS

RESOLVER = ROOT / "lib/project_version.sh"


def git(root, *arguments):
    """Run fixture Git commands with a local author identity."""
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Version Test",
            "-c",
            "user.email=version@example.invalid",
            *arguments,
        ],
        text=True,
    ).strip()


def resolve(root):
    """Resolve through the shell command, keeping its diagnostics."""
    return subprocess.run(
        ["bash", str(RESOLVER), "resolve", str(root)],
        check=True,
        text=True,
        capture_output=True,
    )


@pytest.fixture(name="repository")
def repository_fixture(tmp_path):
    root = tmp_path / "source with 'apostrophe'"
    root.mkdir()
    git(root, "init", "-q")
    (root / "payload.txt").write_text("first\n")
    (root / ".gitignore").write_text("ignored\nVERSION\nSOURCE_SHA256\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "Initial")
    return root


def commit(root):
    """Append one tracked fixture commit."""
    with (root / "payload.txt").open("a") as stream:
        stream.write("next\n")
    git(root, "commit", "-qam", "Next")


@pytest.mark.parametrize("tag", ["v1.2.3", "v1.2.3-rc.1", "v0.0.0", "v2.0.0+build.7"])
def test_release_tag_and_later_commits(repository, tag):
    git(repository, "tag", "-a", tag, "-m", tag)
    assert resolve_version(repository) == tag
    commit(repository)
    sha = git(repository, "rev-parse", "--short=12", "HEAD")
    assert resolve_version(repository) == f"{tag}-1-g{sha}"


@pytest.mark.parametrize(
    "tag",
    ["1.2.3", "v01.2.3", "v1.2", "v1.2.3-01", "v1.2.3-", "v1.2.3.4", "v1.2.3-modified"],
)
def test_non_release_tags_are_ignored(repository, tag):
    git(repository, "tag", "-a", tag, "-m", tag)
    assert resolve_version(repository).startswith("untagged-1-g")


def test_lightweight_tag_and_version_file_are_ignored_in_git(repository):
    git(repository, "tag", "v1.2.3")
    (repository / "VERSION").write_text("v9.9.9\n")
    assert resolve_version(repository).startswith("untagged-1-g")


def test_nearest_tag_follows_history_not_creation_order(repository):
    git(repository, "tag", "-a", "v1.0.0", "-m", "old")
    initial = git(repository, "rev-parse", "HEAD")
    commit(repository)
    git(repository, "tag", "-a", "v2.0.0", "-m", "near")
    commit(repository)
    git(repository, "tag", "-a", "v9.0.0", initial, "-m", "new but distant")
    assert resolve_version(repository).startswith("v2.0.0-1-g")


@pytest.mark.parametrize("state", ["unstaged", "staged", "deleted", "untracked"])
def test_local_changes_are_modified(repository, state):
    git(repository, "tag", "-a", "v1.2.3", "-m", "release")
    path = repository / "payload.txt"
    if state == "deleted":
        path.unlink()
    elif state == "untracked":
        (repository / "extra").write_text("untracked")
    else:
        path.write_text("edited")
        if state == "staged":
            git(repository, "add", ".")
    assert resolve_version(repository) == "v1.2.3-modified"


def test_ignored_files_are_not_modifications(repository):
    git(repository, "tag", "-a", "v1.2.3", "-m", "release")
    for name in ("ignored", "VERSION", "SOURCE_SHA256"):
        (repository / name).write_text("generated")
    assert resolve_version(repository) == "v1.2.3"


def test_worktree_and_unrelated_enclosing_repository(repository, tmp_path):
    git(repository, "tag", "-a", "v1.2.3", "-m", "release")
    worktree = tmp_path / "worktree"
    git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
    assert resolve_version(worktree) == "v1.2.3"
    extracted = repository / "ignored"
    extracted.mkdir()
    (extracted / "payload.txt").write_text("archived")
    stamp_source(extracted, "v2.3.4")
    assert resolve_version(extracted) == "v2.3.4"


def test_shallow_clone_trusts_only_an_exact_tag(repository, tmp_path):
    git(repository, "tag", "-a", "v1.2.3", "-m", "release")
    commit(repository)
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth=1", repository.as_uri(), str(clone)], check=True
    )
    assert resolve_version(clone).startswith("shallow-g")
    git(clone, "tag", "-a", "v2.0.0-rc.1", "-m", "exact")
    assert resolve_version(clone) == "v2.0.0-rc.1"


def test_unreadable_git_metadata_is_reported(tmp_path):
    (tmp_path / ".git").write_text("gitdir: /nonexistent/storage-scale-test\n")
    result = resolve(tmp_path)
    assert result.stdout.strip() == "unknown"
    assert "cannot read Git metadata" in result.stderr


def _stamped(tmp_path):
    root = tmp_path / "deployment"
    (root / "lib").mkdir(parents=True)
    (root / "lib/payload.txt").write_text("source")
    (root / "utils").mkdir()
    (root / "utils/elbencho").write_text("binary")
    (root / "env.sh").write_text("site configuration")
    stamp_source(root, "v1.2.3")
    return root


def test_stamp_lists_source_but_not_site_files(tmp_path):
    root = _stamped(tmp_path)
    assert (root / "VERSION").read_text() == "v1.2.3\n"
    listed = (root / "SOURCE_SHA256").read_text()
    assert listed.endswith("  lib/payload.txt\n") and listed.count("\n") == 1
    for name in ("utils/elbencho", "env.sh", "extra.txt"):
        (root / name).write_text("replaced")
    assert resolve_version(root) == "v1.2.3"


@pytest.mark.parametrize("change", ["edit", "delete", "garbage", "missing"])
def test_changed_archive_is_modified(tmp_path, change):
    root = _stamped(tmp_path)
    checksums = root / "SOURCE_SHA256"
    if change == "edit":
        (root / "lib/payload.txt").write_text("changed")
    elif change == "delete":
        (root / "lib/payload.txt").unlink()
    elif change == "garbage":
        checksums.write_text("$(touch executed)\n")
    else:
        checksums.unlink()
    result = resolve(root)
    assert result.stdout.strip() == "v1.2.3-modified"
    assert "do not match" in result.stderr
    assert not (root / "executed").exists()


def test_restamping_modified_source_keeps_modified(tmp_path):
    root = _stamped(tmp_path)
    (root / "lib/payload.txt").write_text("changed")
    stamp_source(root, resolve_version(root))
    assert (root / "VERSION").read_text() == "v1.2.3-modified\n"
    assert resolve_version(root) == "v1.2.3-modified"


@pytest.mark.parametrize("content", ["$(touch executed)", "v1.2.3-modified-modified"])
def test_malformed_version_file_is_unknown(tmp_path, content):
    (tmp_path / "VERSION").write_text(content)
    assert resolve_version(tmp_path) == "unknown"
    assert not (tmp_path / "executed").exists()


@pytest.mark.parametrize("version", ["1.2.3", "v1.2.3-modified-modified", ""])
def test_stamp_rejects_invalid_versions(tmp_path, version):
    (tmp_path / "file").write_text("source")
    with pytest.raises(subprocess.CalledProcessError):
        stamp_source(tmp_path, version)
    assert not (tmp_path / "VERSION").exists()


def test_version_option_scans_arguments_up_to_double_dash(tmp_path):
    (tmp_path / "VERSION").write_text("v1.2.3\n")
    script = f"""
        source {str(RESOLVER)!r}
        project_version_option {str(tmp_path)!r} -- --version
        echo continued
        project_version_option {str(tmp_path)!r} --nodes 2 --version
        echo not reached
    """
    result = subprocess.run(
        ["bash", "-c", script], check=True, text=True, capture_output=True
    )
    assert result.stdout.split() == ["continued", "v1.2.3-modified"]


def test_result_versions_are_recorded_beside_results(tmp_path):
    result = tmp_path / "elbencho-r4K.out"
    script = f"""
        source {str(ROOT / "lib/_platform_functions.sh")!r}
        STORAGE_SCALE_TEST_VERSION=v1.2.3 _write_result_version \\
            "$(_elbencho_resfile_from_args --threads 4 --resfile {str(result)!r})"
        _write_result_version {str(tmp_path / "missing/x.out")!r}
        _write_result_version ""
    """
    run = subprocess.run(
        ["bash", "-c", script], check=False, text=True, capture_output=True
    )
    assert run.returncode == 0
    assert "could not record" in run.stderr
    assert result_version(result) == "v1.2.3"


def test_report_versions_read_sidecars_and_tolerate_bad_ones(tmp_path):
    out = tmp_path / "warp-GET-1MiB.out"
    (tmp_path / "warp-GET-1MiB.out.project-version").write_text("v1.2.3\n")
    assert result_version(out) == "v1.2.3"
    assert result_version(tmp_path / "warp-GET-1MiB.json.zst") == "v1.2.3"
    assert result_version(tmp_path / "legacy.out") == "unknown"
    (tmp_path / "bad.out.project-version").write_text("v1 \x1b[31m")
    assert result_version(tmp_path / "bad.out") == "unknown"


def test_report_header_sorts_versions_naturally(capsys):
    with_versions(print, ["v10.0.0", "v2.0.0", "unknown", "v2.0.0"])("table")
    with_versions(print, [], markdown=True)("table")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "Produced by storage-scale-test: unknown, v2.0.0, v10.0.0"
    assert lines[1].startswith("Reported by storage-scale-test: ")
    assert lines[3] == "table"
    assert lines[4] == "- Produced by storage-scale-test: unknown"


def test_versions_follow_metric_objects_not_copies():
    first, second, copy = object(), object(), object()
    versions = ResultVersions()
    versions.add([first], "v1.0.0")
    versions.add([second], "v2.0.0")
    assert versions.of([second, copy, first]) == ["v2.0.0", "unknown", "v1.0.0"]
    recorded = [((1, 4), "v1.0.0"), ((2, 4), "v2.0.0")]
    assert selected_versions(recorded, [(2, 4)], lambda key: key) == ["v2.0.0"]


@pytest.mark.parametrize(
    "command",
    sorted(
        path.relative_to(ROOT).as_posix()
        for pattern in VERSIONED_SHELL_COMMANDS + VERSIONED_PYTHON_COMMANDS
        for path in ROOT.glob(pattern)
    ),
)
def test_user_facing_commands_print_the_version(command):
    interpreter = [sys.executable] if command.endswith(".py") else ["bash"]
    result = subprocess.run(
        [*interpreter, str(ROOT / command), "--version"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert result.stdout.strip() == resolve_version(ROOT)


REPORT_COMMANDS = (
    "extract-elbencho",
    "extract-mdtest-elbencho",
    "extract-netbench",
    "extract-warp",
    "extract-filesystem",
    "summarize-elbencho",
)
INFORMATION_COMMANDS = tuple(f"utils/{name}.py" for name in REPORT_COMMANDS) + (
    "storage-tests/network/nv-netbench.sh",
    "storage-tests/object/nv-warp-sweep.sh",
)


@pytest.fixture(name="information_tree")
def information_tree_fixture(tmp_path):
    """A stamped tree without dependencies, configuration, credentials or workers."""
    root = tmp_path / "source tree's files"
    shutil.copytree(
        ROOT / "lib", root / "lib", ignore=shutil.ignore_patterns("__pycache__")
    )
    for command in INFORMATION_COMMANDS:
        target = root / command
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / command, target)
    stamp_source(root, "v1.2.3")
    return root


@pytest.mark.parametrize("command", INFORMATION_COMMANDS)
@pytest.mark.parametrize("option", ["-h", "--help", "--version"])
@pytest.mark.parametrize("configuration", ["missing", "broken"])
def test_information_paths_need_no_setup(
    information_tree, command, option, configuration
):
    if configuration == "broken":
        (information_tree / "env.sh").write_text(
            "echo configuration-was-loaded >&2\nexit 77\n"
        )
    interpreter = [sys.executable, "-S"] if command.endswith(".py") else ["bash"]
    result = subprocess.run(
        [*interpreter, str(information_tree / command), option],
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
        env={
            **os.environ,
            "SLURM_JOB_ID": "123",
            "KUBECTL_ENABLED": "1",
        },
    )
    assert not result.stderr
    if option == "--version":
        assert result.stdout == "v1.2.3\n"
    else:
        assert "usage:" in result.stdout.lower()
        assert "--version" in result.stdout
    assert not (information_tree / ".venv").exists()


def test_information_shortcut_stops_at_double_dash(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["report.py", "--", "--help", "--version"])
    exit_for_information(
        lambda: pytest.fail("literal arguments must reach normal execution")
    )
