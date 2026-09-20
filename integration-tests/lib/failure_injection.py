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

"""Scenario-owned Elbencho failure injection for integration tests.

The generated wrapper is deliberately independent of production deployment
code. Callers stage its real-binary delegate through injected operations, set
``ELBENCHO`` to the wrapper for a scenario, run the failed attempt and resume
inside one :func:`staged_failure_injection` context, and copy diagnostics before
leaving that outer context.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEFAULT_INJECTED_EXIT_CODE = 97
MAX_SCENARIO_ID_LENGTH = 64
_SCENARIO_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")


class FailureInjectionError(RuntimeError):
    """A failure-injection plan is unsafe or internally inconsistent."""


@dataclass(frozen=True)
class FailureInjectionTarget:
    """One machine or shared filesystem on which artifacts are staged.

    ``endpoint`` is opaque to this module. An SSH adapter can use a hostname,
    while a Slurm adapter can name its shared login/compute filesystem.
    ``install_wrapper`` is false for SSH workers because production code copies
    the selected coordinator wrapper to them; they only need its delegate and
    optional runtime at the same absolute paths.
    """

    endpoint: str
    install_wrapper: bool


@dataclass(frozen=True)
class FailureInjectionLayout:
    """Exact POSIX paths used on every staging endpoint."""

    root: PurePosixPath
    wrapper: PurePosixPath
    delegate: PurePosixPath
    runtime: PurePosixPath
    marker: PurePosixPath


@dataclass(frozen=True)
class FailureInjectionPlan:
    """Complete immutable staging and wrapper plan for one scenario."""

    scenario_id: str
    source_binary: Path
    source_runtime: Path | None
    target_argument: str
    injected_exit_code: int
    layout: FailureInjectionLayout
    targets: tuple[FailureInjectionTarget, ...]
    wrapper_script: str


MakeDirectory = Callable[[str, PurePosixPath], None]
CopyFile = Callable[[Path, str, PurePosixPath, int], None]
CopyTree = Callable[[Path, str, PurePosixPath], None]
WriteText = Callable[[str, PurePosixPath, str, int], None]
RemoveTree = Callable[[str, PurePosixPath], None]


@dataclass(frozen=True)
class FailureInjectionOperations:
    """Caller-provided local, SSH, or Slurm staging operations."""

    make_directory: MakeDirectory
    copy_file: CopyFile
    copy_tree: CopyTree
    write_text: WriteText
    remove_tree: RemoveTree


def _validated_root(root: str | PurePosixPath, scenario_id: str) -> PurePosixPath:
    """Return a safe scenario-specific absolute staging root."""
    if (
        not scenario_id
        or len(scenario_id) > MAX_SCENARIO_ID_LENGTH
        or _SCENARIO_ID.fullmatch(scenario_id) is None
    ):
        raise FailureInjectionError(f"invalid scenario identifier: {scenario_id!r}")
    base = PurePosixPath(root)
    if not base.is_absolute() or base == PurePosixPath("/") or ".." in base.parts:
        raise FailureInjectionError(
            f"failure-injection root must be a dedicated absolute path: {root!s}"
        )
    return base / scenario_id / "failure-injection"


def _layout(root: PurePosixPath) -> FailureInjectionLayout:
    """Derive fixed wrapper, delegate, runtime, and marker paths."""
    delegate_root = root / "delegate"
    return FailureInjectionLayout(
        root=root,
        wrapper=root / "elbencho-inject",
        delegate=delegate_root / "elbencho",
        runtime=delegate_root / "elbencho-runtime",
        marker=root / "failure-injected.marker",
    )


def _validate_targets(targets: Sequence[FailureInjectionTarget]) -> None:
    """Require unique endpoints and exactly one wrapper-capable coordinator."""
    if not targets:
        raise FailureInjectionError("failure-injection plan has no staging targets")
    endpoints = [target.endpoint for target in targets]
    if any(not endpoint or "\n" in endpoint for endpoint in endpoints):
        raise FailureInjectionError("failure-injection endpoint is empty or multiline")
    if len(set(endpoints)) != len(endpoints):
        raise FailureInjectionError("failure-injection endpoints must be unique")
    wrappers = sum(target.install_wrapper for target in targets)
    if wrappers != 1:
        raise FailureInjectionError(
            "failure-injection plan requires exactly one coordinator wrapper target"
        )


def _wrapper_script(
    layout: FailureInjectionLayout,
    target_argument: str,
    injected_exit_code: int,
) -> str:
    """Generate the exact Bash wrapper staged for one scenario."""
    delegate = shlex.quote(str(layout.delegate))
    marker = shlex.quote(str(layout.marker))
    target = shlex.quote(target_argument)
    return f"""#!/usr/bin/env bash
set -euo pipefail

readonly delegate={delegate}
readonly marker={marker}
readonly trace=$(dirname "$marker")/invocations.log
readonly target_argument={target}
readonly injected_exit_code={injected_exit_code}

service_mode=0
target_invocation=0
for argument in "$@"; do
    [[ "$argument" == "--service" ]] && service_mode=1
    [[ "$argument" == *"$target_argument"* ]] && target_invocation=1
done

if (( service_mode )); then
    exec "$delegate" "$@"
fi
printf '%q ' "$@" >>"$trace"
printf '\n' >>"$trace"
if (( ! target_invocation )); then
    exec "$delegate" "$@"
fi
if ! mkdir -- "$marker" 2>/dev/null; then
    exec "$delegate" "$@"
fi

child_pid=""
forward_signal() {{
    local signal=$1
    [[ -z "$child_pid" ]] || kill -s "$signal" "$child_pid" 2>/dev/null || true
}}
trap 'forward_signal HUP' HUP
trap 'forward_signal INT' INT
trap 'forward_signal TERM' TERM

"$delegate" "$@" &
child_pid=$!
set +e
while true; do
    wait "$child_pid"
    delegate_rc=$?
    if ! kill -0 "$child_pid" 2>/dev/null; then
        break
    fi
done
set -e
trap - HUP INT TERM

if (( delegate_rc != 0 )); then
    rm -rf -- "$marker"
    exit "$delegate_rc"
fi
printf '%s\n' injected >"$marker/state"
exit "$injected_exit_code"
"""


def build_failure_injection_plan(
    *,
    scenario_id: str,
    staging_root: str | PurePosixPath,
    source_binary: Path,
    source_runtime: Path | None,
    target_argument: str,
    targets: Sequence[FailureInjectionTarget],
    injected_exit_code: int = DEFAULT_INJECTED_EXIT_CODE,
) -> FailureInjectionPlan:
    """Build a substrate-neutral failure-injection plan.

    ``target_argument`` must be a distinctive argument fragment unique to the
    cell that should fail, normally its execution-specific JSON path. Service
    startup is handled separately before fragment matching.
    """
    if not target_argument or "\x00" in target_argument:
        raise FailureInjectionError(
            "target argument must be nonempty and contain no NUL"
        )
    if not 1 <= injected_exit_code <= 255:
        raise FailureInjectionError("injected exit code must be between 1 and 255")
    _validate_targets(targets)
    plan_layout = _layout(_validated_root(staging_root, scenario_id))
    return FailureInjectionPlan(
        scenario_id=scenario_id,
        source_binary=source_binary,
        source_runtime=source_runtime,
        target_argument=target_argument,
        injected_exit_code=injected_exit_code,
        layout=plan_layout,
        targets=tuple(targets),
        wrapper_script=_wrapper_script(
            plan_layout, target_argument, injected_exit_code
        ),
    )


def build_ssh_failure_injection_plan(
    *,
    scenario_id: str,
    staging_root: str | PurePosixPath,
    source_binary: Path,
    source_runtime: Path | None,
    target_argument: str,
    coordinator_endpoint: str,
    worker_endpoints: Sequence[str],
    injected_exit_code: int = DEFAULT_INJECTED_EXIT_CODE,
) -> FailureInjectionPlan:
    """Build a plan that stages delegates on the coordinator and SSH workers."""
    targets = [FailureInjectionTarget(coordinator_endpoint, True)]
    targets.extend(
        FailureInjectionTarget(endpoint, False) for endpoint in worker_endpoints
    )
    return build_failure_injection_plan(
        scenario_id=scenario_id,
        staging_root=staging_root,
        source_binary=source_binary,
        source_runtime=source_runtime,
        target_argument=target_argument,
        targets=targets,
        injected_exit_code=injected_exit_code,
    )


def build_slurm_failure_injection_plan(
    *,
    scenario_id: str,
    staging_root: str | PurePosixPath,
    source_binary: Path,
    source_runtime: Path | None,
    target_argument: str,
    shared_endpoint: str,
    injected_exit_code: int = DEFAULT_INJECTED_EXIT_CODE,
) -> FailureInjectionPlan:
    """Build a plan whose artifacts are visible to login and compute nodes."""
    return build_failure_injection_plan(
        scenario_id=scenario_id,
        staging_root=staging_root,
        source_binary=source_binary,
        source_runtime=source_runtime,
        target_argument=target_argument,
        targets=(FailureInjectionTarget(shared_endpoint, True),),
        injected_exit_code=injected_exit_code,
    )


def stage_failure_injection(
    plan: FailureInjectionPlan, operations: FailureInjectionOperations
) -> None:
    """Stage a plan without assuming local, SSH, or Slurm command transport."""
    if not plan.source_binary.is_file() or plan.source_binary.is_symlink():
        raise FailureInjectionError(
            f"real Elbencho delegate is not a regular file: {plan.source_binary}"
        )
    if plan.source_runtime is not None and (
        not plan.source_runtime.is_dir() or plan.source_runtime.is_symlink()
    ):
        raise FailureInjectionError(
            f"Elbencho runtime is not a directory: {plan.source_runtime}"
        )
    for target in plan.targets:
        operations.make_directory(target.endpoint, plan.layout.root)
        operations.make_directory(target.endpoint, plan.layout.delegate.parent)
        operations.copy_file(
            plan.source_binary, target.endpoint, plan.layout.delegate, 0o755
        )
        if plan.source_runtime is not None:
            operations.copy_tree(
                plan.source_runtime, target.endpoint, plan.layout.runtime
            )
        if target.install_wrapper:
            operations.write_text(
                target.endpoint,
                plan.layout.wrapper,
                plan.wrapper_script,
                0o755,
            )


def cleanup_failure_injection(
    plan: FailureInjectionPlan, operations: FailureInjectionOperations
) -> None:
    """Remove all scenario-owned staging, including the persistent marker."""
    for target in reversed(plan.targets):
        operations.remove_tree(target.endpoint, plan.layout.root)


@contextmanager
def staged_failure_injection(
    plan: FailureInjectionPlan, operations: FailureInjectionOperations
) -> Iterator[FailureInjectionPlan]:
    """Stage once and clean only after the whole failed-attempt/resume scenario.

    Callers should execute the initial expected failure, copy intermediate
    diagnostics, execute resume, and copy final diagnostics inside the context.
    Cleanup also runs if the overall scenario aborts.
    """
    stage_failure_injection(plan, operations)
    try:
        yield plan
    finally:
        cleanup_failure_injection(plan, operations)
