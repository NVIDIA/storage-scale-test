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

"""Exercise bootstrapping with real virtual environments and offline wheels."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

from lib import python_bootstrap as bootstrap

_PACKAGE = "bootstrap_fixture"
_HELPER = Path(bootstrap.__file__).resolve()


def _write_wheel(directory, version):
    """Create a small valid local wheel without build tooling or index access."""
    metadata_dir = f"{_PACKAGE}-{version}.dist-info"
    files = {
        f"{_PACKAGE}/__init__.py": f"VERSION = '{version}'\n",
        f"{metadata_dir}/METADATA": (
            f"Metadata-Version: 2.1\nName: {_PACKAGE}\nVersion: {version}\n"
        ),
        f"{metadata_dir}/WHEEL": (
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    files[f"{metadata_dir}/RECORD"] = "".join(f"{name},,\n" for name in files)
    wheel = directory / f"{_PACKAGE}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


@pytest.fixture(name="offline_repo")
def offline_repo_fixture(tmp_path, monkeypatch):
    """Run real pip locally, including paths with spaces and no usable index."""
    repo = tmp_path / "checkout with spaces"
    repo.mkdir()
    wheels = tmp_path / "local wheels"
    wheels.mkdir()
    _write_wheel(wheels, "1.0")
    (repo / "requirements.txt").write_text(f"{_PACKAGE}==1.0\n", encoding="utf-8")
    monkeypatch.setenv("PIP_NO_INDEX", "1")
    monkeypatch.setenv("PIP_FIND_LINKS", wheels.as_uri())
    return repo, wheels


def _metadata_version(python):
    return subprocess.check_output(
        [
            str(python),
            "-c",
            f"import importlib.metadata as m; print(m.version('{_PACKAGE}'))",
        ],
        text=True,
    ).strip()


def test_install_reuse_and_repair_missing_package(offline_repo, capfd):
    """Matching stamps cannot hide missing packages; ready calls do no installs."""
    repo, _ = offline_repo
    python = bootstrap.ensure_environment(repo)
    stamp = repo / ".venv" / bootstrap.STAMP_NAME
    assert _metadata_version(python) == "1.0"
    assert (
        stamp.read_text().strip()
        == hashlib.sha256((repo / "requirements.txt").read_bytes()).hexdigest()
    )
    capfd.readouterr()
    assert bootstrap.ensure_environment(repo) == python
    assert "Installing required packages" not in capfd.readouterr().err
    subprocess.run(
        [str(python), "-m", "pip", "uninstall", "-y", _PACKAGE],
        check=True,
        capture_output=True,
    )
    assert stamp.exists()
    bootstrap.ensure_environment(repo)
    assert _metadata_version(python) == "1.0"
    assert "Installing required packages" in capfd.readouterr().err


def test_failed_install_invalidates_stamp_and_retry_succeeds(offline_repo):
    """A failed requirements update is never advertised as a ready environment."""
    repo, wheels = offline_repo
    python = bootstrap.ensure_environment(repo)
    requirements = repo / "requirements.txt"
    requirements.write_text(f"{_PACKAGE}==1.1\n", encoding="utf-8")
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.ensure_environment(repo)
    assert not (repo / ".venv" / bootstrap.STAMP_NAME).exists()
    _write_wheel(wheels, "1.1")
    bootstrap.ensure_environment(repo)
    assert _metadata_version(python) == "1.1"
    assert (repo / ".venv" / bootstrap.STAMP_NAME).exists()


def test_concurrent_processes_install_once(offline_repo):
    """Real advisory locks serialize first-run venv creation and publication."""
    repo, _ = offline_repo
    command = [sys.executable, str(_HELPER), str(repo)]
    processes = [
        subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    outputs = [process.communicate(timeout=90) for process in processes]
    assert [process.returncode for process in processes] == [0, 0]
    assert all(
        stdout.strip() == str(repo / ".venv/bin/python") for stdout, _ in outputs
    )
    diagnostics = "".join(stderr for _, stderr in outputs)
    assert diagnostics.count("Creating virtual environment...") == 1
    assert diagnostics.count("Installing required packages") == 1


def test_running_environment_repairs_its_missing_interpreter(offline_repo):
    """Rebuild from base Python when the running venv's own alias is broken."""
    repo, _ = offline_repo
    python = bootstrap.ensure_environment(repo)
    lock = repo / ".venv/.bootstrap.lock"
    original_lock_inode = lock.stat().st_ino
    code = (
        "import pathlib, sys; "
        f"sys.path.insert(0, {str(_HELPER.parent.parent)!r}); "
        "from lib.python_bootstrap import ensure_environment; "
        f"pathlib.Path({str(python)!r}).unlink(); "
        f"print(ensure_environment(pathlib.Path({str(repo)!r})))"
    )
    result = subprocess.run(
        [str(python), "-c", code], check=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == str(python)
    assert "Creating virtual environment..." in result.stderr
    assert _metadata_version(python) == "1.0"
    assert lock.stat().st_ino == original_lock_inode


def test_existing_environment_without_pip_is_bootstrapped(offline_repo):
    """Repair environments created by uv or Python with --without-pip."""
    repo, _ = offline_repo
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(repo / ".venv")],
        check=True,
        capture_output=True,
    )
    python = bootstrap.ensure_environment(repo)
    assert _metadata_version(python) == "1.0"


def test_real_reexec_and_correct_interpreter_skips_reexec(offline_repo, tmp_path):
    """Preserve args, cwd, stdout and environment through exactly one re-exec."""
    repo, _ = offline_repo
    library = repo / "lib"
    library.mkdir()
    shutil.copy2(_HELPER, library / _HELPER.name)
    script = repo / "report.py"
    script.write_text(
        "import json, os, pathlib, sys\n"
        "from lib.python_bootstrap import ensure_runtime\n"
        "counter = pathlib.Path(__file__).with_suffix('.starts')\n"
        "with counter.open('a') as handle: handle.write('start\\n')\n"
        "ensure_runtime(__file__)\n"
        "print(json.dumps([sys.prefix, sys.argv[1:], os.getcwd(), "
        "os.environ['VIRTUAL_ENV'], os.environ['PATH'].split(os.pathsep)[0]]))\n",
        encoding="utf-8",
    )
    arguments = ["path with spaces", "--option=literal", "$(no shell)"]
    first = subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    expected = [
        str(repo / ".venv"),
        arguments,
        str(tmp_path),
        str(repo / ".venv"),
        str(repo / ".venv/bin"),
    ]
    assert json.loads(first.stdout) == expected
    assert script.with_suffix(".starts").read_text().splitlines() == ["start", "start"]
    second = subprocess.run(
        [str(repo / ".venv/bin/python"), str(script), *arguments],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(second.stdout) == expected
    assert script.with_suffix(".starts").read_text().splitlines() == ["start"] * 3
    assert "Installing required packages" not in second.stderr


def test_old_interpreter_reexecs_after_same_prefix_upgrade(tmp_path, monkeypatch):
    """A matching prefix alone cannot make an obsolete running Python usable."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    (repo / "requirements.txt").write_text("", encoding="utf-8")
    script = repo / "report.py"
    python = repo / ".venv/bin/python"
    monkeypatch.setattr(bootstrap, "ensure_environment", lambda _root: python)
    monkeypatch.setattr(sys, "prefix", str(repo / ".venv"))
    monkeypatch.setattr(sys, "version_info", (3, 11, 0))
    monkeypatch.setattr(sys, "argv", [str(script), "--path", "has spaces"])
    monkeypatch.setenv("PATH", bootstrap.os.environ.get("PATH", ""))
    monkeypatch.setenv("VIRTUAL_ENV", "previous environment")
    monkeypatch.setenv("PYTHONHOME", "obsolete Python home")
    calls = []
    monkeypatch.setattr(
        bootstrap.os, "execv", lambda executable, args: calls.append((executable, args))
    )
    bootstrap.ensure_runtime(str(script))
    assert calls == [(str(python), [str(python), str(script), "--path", "has spaces"])]
    assert "PYTHONHOME" not in bootstrap.os.environ


def test_unsupported_launcher_selects_supported_path_python(tmp_path, monkeypatch):
    """Ignore an older python3 when a supported versioned interpreter exists."""
    # pylint: disable=protected-access
    old_python = tmp_path / "python3"
    old_python.write_text("#!/bin/sh\nprintf '%s\\n' '[[3,10],{},true]'\n")
    old_python.chmod(0o755)
    supported = tmp_path / "python3.12"
    supported.symlink_to(sys.executable)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(sys, "version_info", (3, 10, 0))
    assert bootstrap._select_python() == str(supported)
    supported.unlink()
    with pytest.raises(RuntimeError, match="Python 3.12 or newer"):
        bootstrap._select_python()


def test_unversioned_deployment_version_has_no_bootstrap(tmp_path):
    assert bootstrap.project_version(tmp_path) == "unversioned source"
    assert not (tmp_path / ".venv").exists()
