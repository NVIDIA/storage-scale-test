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
readonly -a LINT_CHECKS=(compliance shellcheck black pylint)
SETUP_STARTED="$SECONDS"

available_cpu_count() {
    local cpu_count
    if [[ "$(uname -s)" == "Darwin" ]]; then
        cpu_count=$(sysctl -n hw.logicalcpu 2>/dev/null) || cpu_count=""
    else
        cpu_count=$(getconf _NPROCESSORS_ONLN 2>/dev/null) || cpu_count=""
    fi
    if [[ ! "$cpu_count" =~ ^[1-9][0-9]*$ ]]; then
        cpu_count=4
    fi
    printf '%s\n' "$cpu_count"
}

CHECK_JOBS="${CI_CHECK_JOBS:-$(available_cpu_count)}"

usage() {
    echo "Usage: $0 [all|lint|compliance|shellcheck|black|pylint|pytest]" >&2
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

if [[ "$TARGET" != "all" && "$TARGET" != "lint" && \
      "$TARGET" != "compliance" && \
      "$TARGET" != "shellcheck" && "$TARGET" != "black" && \
      "$TARGET" != "pylint" && "$TARGET" != "pytest" ]]; then
    usage
    exit 2
fi

if [[ ! "$CHECK_JOBS" =~ ^[1-9][0-9]*$ ]]; then
    echo "CI_CHECK_JOBS must be a positive integer" >&2
    exit 2
fi

cd "$REPO_ROOT"

if [[ "$TARGET" == "all" || "$TARGET" == "pytest" ]]; then
    check_macos_test_prerequisites
fi

if [[ "$BOOTSTRAP" == "1" ]]; then
    VENV_DIR="${CI_VENV_DIR:-$REPO_ROOT/.venv-ci}"
    VENV_STATE="reused"
    DEPENDENCY_STATE="current"
    if [[ ! -x "$VENV_DIR/bin/python" ]]; then
        VENV_STATE="rebuilt"
        if command -v uv >/dev/null 2>&1; then
            # A venv copied from another host may contain broken interpreter
            # symlinks. Rebuild this dedicated CI environment without prompting.
            uv venv --clear --python "$PYTHON_BIN" "$VENV_DIR"
        else
            "$PYTHON_BIN" -m venv --clear "$VENV_DIR"
        fi
    fi
    REQUIREMENTS_STAMP="$VENV_DIR/.requirements"
    if [[ ! -f "$REQUIREMENTS_STAMP" ]] || \
       ! cmp -s <(cat requirements.txt requirements-ci.txt) "$REQUIREMENTS_STAMP"; then
        DEPENDENCY_STATE="updated"
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
    printf '\n== environment setup (success, %ss) ==\n' \
        "$((SECONDS - SETUP_STARTED))"
    printf 'Virtual environment %s; dependencies %s.\n' \
        "$VENV_STATE" "$DEPENDENCY_STATE"
elif [[ "$BOOTSTRAP" != "0" ]]; then
    echo "CI_BOOTSTRAP must be 0 or 1" >&2
    exit 2
fi

run_compliance() {
    "$PYTHON_BIN" utils/check_license_headers.py
}

run_shellcheck() {
    local jobs="${1:-$CHECK_JOBS}"
    git ls-files -z '*.sh' \
        | xargs -0 -P "$jobs" -n 8 "$SHELLCHECK_BIN"
}

run_black() {
    local jobs="${1:-$CHECK_JOBS}"
    git ls-files -z '*.py' \
        | xargs -0 "$PYTHON_BIN" -m black --workers "$jobs" --check --diff
}

run_pylint() {
    local jobs="${1:-$CHECK_JOBS}"
    git ls-files -z '*.py' | xargs -0 "$PYTHON_BIN" -m pylint -j "$jobs"
}

run_pytest() {
    local jobs="${1:-$CHECK_JOBS}"
    "$PYTHON_BIN" -m pytest -n "$jobs"
}

run_captured_check() {
    local check_name="$1"
    local jobs="$2"
    local output_file="$3"
    local result_file="$4"
    local started="$SECONDS"
    local check_rc

    # Do not toggle errexit here. Nested captured checks would otherwise
    # re-enable it in this shell and prevent an outer result from being
    # recorded when the nested aggregate returns nonzero.
    if "run_$check_name" "$jobs" >"$output_file" 2>&1; then
        check_rc=0
    else
        check_rc=$?
    fi
    printf '%s %s\n' "$check_rc" "$((SECONDS - started))" >"$result_file"
}

print_lint_result() {
    local check_name="$1"
    local output_file="$2"
    local check_rc="$3"
    local elapsed="$4"
    local outcome="success"
    [[ "$check_rc" -eq 0 ]] || outcome="failure"

    if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
        echo "::group::$check_name ($outcome, ${elapsed}s)"
    else
        printf '\n== %s (%s, %ss) ==\n' "$check_name" "$outcome" "$elapsed"
    fi
    cat "$output_file"
    if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
        echo "::endgroup::"
    fi
    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
        printf '| %s | %s | %s |\n' "$check_name" "$outcome" "$elapsed" \
            >>"$GITHUB_STEP_SUMMARY"
    fi
}

run_lint() {
    local total_jobs="${1:-$CHECK_JOBS}"
    local lint_started="$SECONDS"
    local lint_dir
    lint_dir=$(mktemp -d "${TMPDIR:-/tmp}/storage-scale-test-lint.XXXXXX") \
        || return 1
    local -a pids=()
    local check_name
    local shellcheck_jobs=1
    local pylint_jobs=1
    local black_jobs=1
    local aggregate_rc=0
    local check_rc elapsed

    if ((total_jobs >= 4)); then
        shellcheck_jobs=$((total_jobs / 2))
        pylint_jobs=$(((total_jobs - shellcheck_jobs) / 2))
        black_jobs=$((total_jobs - shellcheck_jobs - pylint_jobs))
    fi

    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
        printf '### Lint checks\n\n| Check | Result | Seconds |\n' \
            >>"$GITHUB_STEP_SUMMARY"
        printf '|---|---|---:|\n' >>"$GITHUB_STEP_SUMMARY"
    fi

    for check_name in "${LINT_CHECKS[@]}"; do
        local jobs=1
        case "$check_name" in
            shellcheck) jobs="$shellcheck_jobs" ;;
            black) jobs="$black_jobs" ;;
            pylint) jobs="$pylint_jobs" ;;
        esac
        if ((total_jobs >= 4)); then
            run_captured_check "$check_name" "$jobs" \
                "$lint_dir/$check_name.log" "$lint_dir/$check_name.result" &
            pids+=("$!")
        else
            run_captured_check "$check_name" "$jobs" \
                "$lint_dir/$check_name.log" "$lint_dir/$check_name.result"
        fi
    done
    for pid in "${pids[@]}"; do
        wait "$pid" || true
    done

    local lint_elapsed="$((SECONDS - lint_started))"
    printf '\n== concurrent lint (completed, %ss wall) ==\n' "$lint_elapsed"
    echo "Buffered check output follows."

    for check_name in "${LINT_CHECKS[@]}"; do
        if [[ -f "$lint_dir/$check_name.result" ]]; then
            read -r check_rc elapsed <"$lint_dir/$check_name.result"
        else
            check_rc=1
            elapsed=0
            printf 'Check did not record a result.\n' >"$lint_dir/$check_name.log"
        fi
        print_lint_result "$check_name" "$lint_dir/$check_name.log" \
            "$check_rc" "$elapsed"
        [[ "$check_rc" -eq 0 ]] || aggregate_rc=1
    done

    if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
        local lint_outcome="success"
        [[ "$aggregate_rc" -eq 0 ]] || lint_outcome="failure"
        printf '| **Concurrent lint wall time** | **%s** | **%s** |\n' \
            "$lint_outcome" "$lint_elapsed" >>"$GITHUB_STEP_SUMMARY"
    fi

    rm -rf -- "$lint_dir"
    return "$aggregate_rc"
}

run_all() {
    local all_started="$SECONDS"
    local all_dir
    all_dir=$(mktemp -d "${TMPDIR:-/tmp}/storage-scale-test-all.XXXXXX") \
        || return 1
    local lint_jobs=1
    local pytest_jobs=1
    local lint_rc pytest_rc lint_elapsed pytest_elapsed

    if ((CHECK_JOBS >= 2)); then
        pytest_jobs=$(((CHECK_JOBS + 1) / 2))
        lint_jobs=$((CHECK_JOBS - pytest_jobs))
        run_captured_check lint "$lint_jobs" \
            "$all_dir/lint.log" "$all_dir/lint.result" &
        local lint_pid="$!"
        # Keep pytest in the foreground. Bash background jobs inherit ignored
        # SIGINT/SIGQUIT dispositions, which invalidates signal-handling tests.
        # Redirected output still prevents pytest and lint from interleaving.
        run_captured_check pytest "$pytest_jobs" \
            "$all_dir/pytest.log" "$all_dir/pytest.result"
        wait "$lint_pid" || true
    else
        run_captured_check lint 1 "$all_dir/lint.log" "$all_dir/lint.result"
        run_captured_check pytest 1 \
            "$all_dir/pytest.log" "$all_dir/pytest.result"
    fi

    read -r lint_rc lint_elapsed <"$all_dir/lint.result"
    read -r pytest_rc pytest_elapsed <"$all_dir/pytest.result"
    printf '\n== all checks (completed, %ss wall; %s-way budget) ==\n' \
        "$((SECONDS - all_started))" "$CHECK_JOBS"
    echo "Buffered output follows."
    cat "$all_dir/lint.log"
    printf '\n== pytest (%s, %ss; %s workers) ==\n' \
        "$([[ "$pytest_rc" -eq 0 ]] && echo success || echo failure)" \
        "$pytest_elapsed" "$pytest_jobs"
    cat "$all_dir/pytest.log"

    rm -rf -- "$all_dir"
    [[ "$lint_rc" -eq 0 && "$pytest_rc" -eq 0 ]]
}

if [[ "$TARGET" == "all" ]]; then
    run_all
else
    "run_$TARGET"
fi
