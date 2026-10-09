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

"""Standard-library bootstrap for repository Python command-line tools.

Call ``ensure_runtime(__file__)`` before third-party imports, only when executing
an entry point. Informational argument handling belongs before that call. Imports
of analysis modules for tests and library use must not bootstrap an environment.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

MINIMUM_PYTHON = (3, 12)
STAMP_NAME = ".requirements.sha256"
_LOCK_NAME = ".bootstrap.lock"
_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*#.*)?$")
_PROBE = """import importlib.metadata as metadata, importlib.util, json, sys
pins = json.loads(sys.argv[1])
versions = {}
for name in pins:
    try:
        versions[name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        versions[name] = None
print(json.dumps([list(sys.version_info[:2]), versions,
                  importlib.util.find_spec('pip') is not None]))
"""


def _pins(requirements: bytes) -> dict[str, str]:
    """Check exact top-level pins while leaving other pip syntax to pip itself."""
    pins = {}
    for line in requirements.decode("utf-8").splitlines():
        match = _PIN_RE.fullmatch(line.strip())
        if match:
            pins[match[1]] = match[2]
    return pins


def _installed_versions(pins: dict[str, str]) -> dict[str, str | None]:
    versions = {}
    for name in pins:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _probe(python: Path, pins: dict[str, str]) -> tuple[bool, bool, bool]:
    """Return supported interpreter, correct pinned packages, and available pip."""
    try:
        result = subprocess.run(
            [str(python), "-c", _PROBE, json.dumps(pins)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            return False, False, False
        version, versions, has_pip = json.loads(result.stdout)
        return tuple(version) >= MINIMUM_PYTHON, versions == pins, bool(has_pip)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False, False, False


def _select_python() -> str:
    """Prefer the running supported interpreter, then supported PATH candidates."""
    if sys.version_info[:2] >= MINIMUM_PYTHON:
        # The venv alias may be replaced during repair, so run its base Python.
        candidate = getattr(sys, "_base_executable", sys.executable)
        if os.access(candidate, os.X_OK):
            return candidate
    names = ["python3", "python3.15", "python3.14", "python3.13", "python3.12"]
    for name in names:
        candidate = shutil.which(name)
        if candidate and _probe(Path(candidate), {})[0]:
            return candidate
    raise RuntimeError(
        "Python 3.12 or newer is required; install it and add it to PATH"
    )


@contextmanager
def _environment_lock(venv_dir: Path):
    """Serialize preparation across processes using a persistent Unix lock file."""
    venv_dir.mkdir(parents=True, exist_ok=True)
    with (venv_dir / _LOCK_NAME).open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _run(command: list[str]) -> None:
    """Keep pip/venv diagnostics off report stdout and propagate their failures."""
    subprocess.run(command, stdout=sys.stderr, stderr=sys.stderr, check=True)


def _create_environment(venv_dir: Path) -> None:
    """Repair interpreter aliases without deleting the held lock or user files."""
    python = _select_python()
    bin_dir = venv_dir / "bin"
    if bin_dir.exists():
        for alias in bin_dir.iterdir():
            if re.fullmatch(r"python(?:3(?:\.\d+)?)?", alias.name):
                if alias.is_file() or alias.is_symlink():
                    alias.unlink()
    print("Creating virtual environment...", file=sys.stderr)
    _run([python, "-m", "venv", str(venv_dir)])


def _write_stamp(stamp: Path, digest: str) -> None:
    """Publish readiness atomically after installation and verification succeed."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=stamp.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(digest + "\n")
        temporary.replace(stamp)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _stamp_matches(stamp: Path, digest: str) -> bool:
    try:
        return stamp.read_text(encoding="utf-8").strip() == digest
    except FileNotFoundError:
        return False


def _environment_state(venv_dir: Path, pins: dict[str, str]) -> tuple[bool, bool, bool]:
    python = venv_dir / "bin" / "python"
    if not os.access(python, os.X_OK):
        return False, False, False
    if Path(sys.prefix).resolve() == venv_dir.resolve():
        # No child interpreter or re-exec is needed inside the ready environment.
        return (
            sys.version_info[:2] >= MINIMUM_PYTHON,
            _installed_versions(pins) == pins,
            importlib.util.find_spec("pip") is not None,
        )
    return _probe(python, pins)


def ensure_environment(repo_root: Path, requirements_file: Path | None = None) -> Path:
    """Create/reuse repo-root .venv, repairing stale pins and interrupted installs."""
    repo_root = Path(repo_root).resolve()
    requirements_file = Path(requirements_file or repo_root / "requirements.txt")
    requirements = requirements_file.read_bytes()
    digest = hashlib.sha256(requirements).hexdigest()
    pins = _pins(requirements)
    venv_dir = repo_root / ".venv"
    stamp = venv_dir / STAMP_NAME
    python = venv_dir / "bin" / "python"
    with _environment_lock(venv_dir):
        supported, packages_current, has_pip = _environment_state(venv_dir, pins)
        if supported and packages_current and _stamp_matches(stamp, digest):
            return python
        # A failed update must not leave a marker advertising the old environment.
        stamp.unlink(missing_ok=True)
        if not supported:
            _create_environment(venv_dir)
            has_pip = True
        if not has_pip:
            _run([str(python), "-m", "ensurepip", "--upgrade"])
        print(
            f"Installing required packages from {requirements_file}...", file=sys.stderr
        )
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(requirements_file),
            ]
        )
        if not _probe(python, pins)[1]:
            raise RuntimeError("Installed packages do not match the requirements pins")
        _write_stamp(stamp, digest)
    return python


def ensure_runtime(script_path: str) -> None:
    """Prepare dependencies, then continue or replace this process in repo .venv."""
    repo_root = next(
        (
            parent
            for parent in Path(script_path).resolve().parents
            if (parent / "requirements.txt").is_file()
        ),
        None,
    )
    if repo_root is None:
        raise SystemExit("Error: cannot find repository requirements.txt")
    try:
        python = ensure_environment(repo_root)
        os.environ["VIRTUAL_ENV"] = str(python.parent.parent)
        bin_path = str(python.parent)
        current_path = os.environ.get("PATH", "")
        if current_path.split(os.pathsep)[0] != bin_path:
            os.environ["PATH"] = bin_path + os.pathsep + current_path
        os.environ.pop("PYTHONHOME", None)
        if (
            sys.version_info[:2] < MINIMUM_PYTHON
            or Path(sys.prefix).resolve() != python.parent.parent.resolve()
        ):
            os.execv(str(python), [str(python), script_path, *sys.argv[1:]])
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(
            f"Error: could not prepare Python environment: {error}"
        ) from error


def main() -> int:
    """Provide the same setup facility to remaining shell-only consumers."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo_root", type=Path)
    parser.add_argument("--requirements", type=Path)
    args = parser.parse_args()
    try:
        print(ensure_environment(args.repo_root, args.requirements))
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Error: could not prepare Python environment: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
