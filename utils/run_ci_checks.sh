#!/usr/bin/env bash

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

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET="${1:-all}"
BOOTSTRAP="${CI_BOOTSTRAP:-1}"
PYTHON_BIN="${CI_PYTHON:-python3}"
SHELLCHECK_BIN="${CI_SHELLCHECK:-shellcheck}"

usage() {
    echo "Usage: $0 [all|compliance|shellcheck|black|pylint|pytest]" >&2
}

check_macos_test_prerequisites() {
    [[ "$(uname -s)" == "Darwin" ]] || return 0
    if ((BASH_VERSINFO[0] < 4 || \
         (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 3))); then
        echo "Error: macOS developer tests require Bash 4.3 or newer; /bin/bash is too old." >&2
        echo "Install the prerequisites with: brew install bash coreutils" >&2
        echo "Then ensure Homebrew's bin directory precedes /bin on PATH." >&2
        return 1
    fi
    local command_name
    for command_name in grealpath gstat gtimeout; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            echo "Error: macOS developer tests require Homebrew coreutils ($command_name is missing)." >&2
            echo "Install it with: brew install coreutils" >&2
            return 1
        fi
    done
}

if [[ "$TARGET" != "all" && "$TARGET" != "compliance" && \
      "$TARGET" != "shellcheck" && "$TARGET" != "black" && \
      "$TARGET" != "pylint" && "$TARGET" != "pytest" ]]; then
    usage
    exit 2
fi

cd "$REPO_ROOT"

if [[ "$TARGET" == "all" || "$TARGET" == "pytest" ]]; then
    check_macos_test_prerequisites
fi

if [[ "$BOOTSTRAP" == "1" ]]; then
    VENV_DIR="${CI_VENV_DIR:-$REPO_ROOT/.venv-ci}"
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        if command -v uv >/dev/null 2>&1; then
            # A venv copied from another host may contain broken interpreter
            # symlinks. Rebuild this dedicated CI environment without prompting.
            uv venv --clear --python "$PYTHON_BIN" "$VENV_DIR"
        else
            "$PYTHON_BIN" -m venv "$VENV_DIR"
        fi
    fi
    REQUIREMENTS_STAMP="$VENV_DIR/.requirements"
    if [[ ! -f "$REQUIREMENTS_STAMP" ]] || \
       ! cmp -s <(cat requirements.txt requirements-ci.txt) "$REQUIREMENTS_STAMP"; then
        if command -v uv >/dev/null 2>&1; then
            uv pip install --python "$VENV_DIR/bin/python" \
                -r requirements.txt -r requirements-ci.txt
        else
            "$VENV_DIR/bin/python" -m pip install --disable-pip-version-check \
                --no-compile -r requirements.txt -r requirements-ci.txt
        fi
        cat requirements.txt requirements-ci.txt > "$REQUIREMENTS_STAMP"
    fi
    PYTHON_BIN="$VENV_DIR/bin/python"
    SHELLCHECK_BIN="$VENV_DIR/bin/shellcheck"
elif [[ "$BOOTSTRAP" != "0" ]]; then
    echo "CI_BOOTSTRAP must be 0 or 1" >&2
    exit 2
fi

run_compliance() {
    "$PYTHON_BIN" utils/check_license_headers.py
}

run_shellcheck() {
    git ls-files -z '*.sh' | xargs -0 "$SHELLCHECK_BIN"
}

run_black() {
    git ls-files -z '*.py' | xargs -0 "$PYTHON_BIN" -m black --check --diff
}

run_pylint() {
    git ls-files -z '*.py' | xargs -0 "$PYTHON_BIN" -m pylint -j 1
}

run_pytest() {
    "$PYTHON_BIN" -m pytest
}

if [[ "$TARGET" == "all" ]]; then
    run_compliance
    run_shellcheck
    run_black
    run_pylint
    run_pytest
else
    "run_$TARGET"
fi
