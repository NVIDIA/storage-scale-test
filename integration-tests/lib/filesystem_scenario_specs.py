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

"""Declarative workload contracts for filesystem integration scenarios.

The driver owns path rendering, substrate setup, and semantic assertions.  This
module deliberately describes only stable workload intent.  Template fields in
environment lines, command arguments, and support files are resolved from the
scenario workspace by the driver.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import product
from typing import Iterable, Mapping


class ScenarioSpecError(ValueError):
    """A scenario specification is internally inconsistent."""


class CommandKind(StrEnum):
    """Kinds of sweep command represented by a scenario step."""

    SWEEP = "sweep"
    RESUME = "resume"
    DELETE = "delete"


class DatasetExpectation(StrEnum):
    """Expected state of scenario-owned benchmark data after a step."""

    CLEANED = "cleaned"
    PRESERVED = "preserved"
    REMOVED = "removed"


class ExecutionStatus(StrEnum):
    """Expected terminal or interrupted state of one reified execution."""

    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"


class WorkloadPhase(StrEnum):
    """Semantic phases whose evidence must be present for a step."""

    DIRECTORY_CREATE = "directory-create"
    WRITE = "write"
    TREE_SCAN = "tree-scan"
    READ = "read"
    REMOVE_FILES = "remove-files"
    DELETE_PATH = "delete-path"


class FailureInjection(StrEnum):
    """Integration-only overlays used by deliberately failing scenarios."""

    NONE = "none"
    FAIL_AFTER_WRITE_ONCE = "fail-after-write-once"


@dataclass(frozen=True, order=True)
class ExecutionCoordinate:
    """One expected point in an Elbencho sweep."""

    nodes: int
    io_size: str
    threads: int
    io_depth: int


@dataclass(frozen=True)
class ExpectedExecution:
    """Expected state of one execution after a command finishes."""

    coordinate: ExecutionCoordinate
    status: ExecutionStatus = ExecutionStatus.SUCCESS


@dataclass(frozen=True)
class SupportFile:
    """A small text file rendered relative to the scenario workspace."""

    relative_path: str
    content: str


@dataclass(frozen=True)
class GeneratedInput:
    """A bounded input file the harness creates without storing its contents."""

    relative_path: str
    size_bytes: int


@dataclass(frozen=True)
class ScenarioStep:
    """One command and its stable semantic expectations."""

    name: str
    kind: CommandKind
    arguments: tuple[str, ...]
    env_lines: tuple[str, ...]
    support_files: tuple[SupportFile, ...]
    generated_inputs: tuple[GeneratedInput, ...]
    timeout_seconds: int
    executions: tuple[ExpectedExecution, ...]
    required_phases: tuple[WorkloadPhase, ...]
    dataset: DatasetExpectation
    requires: tuple[str, ...] = ()
    exports: tuple[str, ...] = ()
    failure_injection: FailureInjection = FailureInjection.NONE
    preserve_failure_staging: bool = False

    def render_arguments(self, values: Mapping[str, str]) -> tuple[str, ...]:
        """Render driver-controlled placeholders in command arguments."""
        return tuple(argument.format_map(values) for argument in self.arguments)

    def render_env(self, values: Mapping[str, str]) -> str:
        """Render the environment override block for this step."""
        return "\n".join(line.format_map(values) for line in self.env_lines) + "\n"

    def render_support_files(
        self, values: Mapping[str, str]
    ) -> tuple[SupportFile, ...]:
        """Render paths and contents of small scenario support files."""
        return tuple(
            SupportFile(
                item.relative_path.format_map(values), item.content.format_map(values)
            )
            for item in self.support_files
        )


@dataclass(frozen=True)
class FilesystemScenarioSpec:
    """All command steps for one named real-infrastructure scenario."""

    name: str
    substrates: frozenset[str]
    steps: tuple[ScenarioStep, ...]


_SHARED_ENV = (
    "unset TEST_DIRS",
    'declare -A TEST_DIRS=(["{test_root}"]=1)',
    'export ELBENCHO_FILE_LAYOUT="shared-directory"',
    "export ELBENCHO_FILES_PER_NODE=1",
    'export ELBENCHO_FILE_SIZE="16M"',
    'export ELBENCHO_SCALE_THREAD_LIST=("1")',
    'export ELBENCHO_SCALE_IO_SIZES=("4K")',
    'export ELBENCHO_IODEPTH_LIST=("1")',
    "export ELBENCHO_SCALE_READ_WRITE_DURATION=1",
    "export ELBENCHO_LIVE_CSV_EXTENDED=0",
    "export ELBENCHO_SINGLE_BIG_FILE=0",
)

_WORKER_ENV = (
    "unset TEST_DIRS",
    'declare -A TEST_DIRS=(["{test_root}"]=2)',
    'export ELBENCHO_FILE_LAYOUT="worker-directories"',
    "export ELBENCHO_FILES_PER_NODE=",
    "export ELBENCHO_FILE_SIZE=",
    "export ELBENCHO_FILE_SIZE_MULTIPLIER=4096",
    'export ELBENCHO_SCALE_THREAD_LIST=("1")',
    'export ELBENCHO_SCALE_IO_SIZES=("4K")',
    'export ELBENCHO_IODEPTH_LIST=("1")',
    "export ELBENCHO_SCALE_READ_WRITE_DURATION=1",
    "export ELBENCHO_LIVE_CSV_EXTENDED=0",
    "export ELBENCHO_SINGLE_BIG_FILE=0",
)

_NORMAL_PHASES = (
    WorkloadPhase.DIRECTORY_CREATE,
    WorkloadPhase.WRITE,
    WorkloadPhase.READ,
    WorkloadPhase.REMOVE_FILES,
)

_BOTH_SUBSTRATES = frozenset({"ssh", "slurm"})
_BASELINE_SUBSTRATES = frozenset({"ssh", "slurm", "kubectl"})
_KUBECTL_ONLY = frozenset({"kubectl"})
_SSH_ONLY = frozenset({"ssh"})
_SLURM_ONLY = frozenset({"slurm"})


def _coordinates(
    nodes: Iterable[int],
    io_sizes: Iterable[str],
    threads: Iterable[int],
    depths: Iterable[int],
    *,
    statuses: Mapping[ExecutionCoordinate, ExecutionStatus] | None = None,
) -> tuple[ExpectedExecution, ...]:
    """Create expected coordinates in the sweep's documented nesting order."""
    status_by_coordinate = statuses or {}
    return tuple(
        ExpectedExecution(
            coordinate,
            status_by_coordinate.get(coordinate, ExecutionStatus.SUCCESS),
        )
        for values in product(nodes, io_sizes, threads, depths)
        for coordinate in (ExecutionCoordinate(*values),)
    )


def _env_name(line: str) -> str:
    """Return the variable named by one supported environment statement."""
    statement = line.strip()
    for prefix in ("export ", "unset ", "declare -A "):
        if statement.startswith(prefix):
            statement = statement.removeprefix(prefix)
            break
    return statement.partition("=")[0].strip()


def _override_env(
    lines: tuple[str, ...], replacements: Mapping[str, str]
) -> tuple[str, ...]:
    """Replace uniquely named variables without depending on shell formatting."""
    compound = {
        name
        for name, replacement in replacements.items()
        if "\n" in replacement or "\r" in replacement
    }
    if compound:
        raise ScenarioSpecError(
            "environment overrides require one statement: "
            + ", ".join(sorted(compound))
        )
    indexes: dict[str, list[int]] = {}
    for index, line in enumerate(lines):
        indexes.setdefault(_env_name(line), []).append(index)
    invalid = {
        name: indexes.get(name, [])
        for name in replacements
        if len(indexes.get(name, [])) != 1
    }
    if invalid:
        details = ", ".join(
            f"{name} ({len(matches)} matches)"
            for name, matches in sorted(invalid.items())
        )
        raise ScenarioSpecError(f"environment overrides require one match: {details}")
    mismatched = {
        name: _env_name(replacement)
        for name, replacement in replacements.items()
        if _env_name(replacement) != name
    }
    if mismatched:
        details = ", ".join(
            f"{name} names {replacement_name or '<empty>'}"
            for name, replacement_name in sorted(mismatched.items())
        )
        raise ScenarioSpecError(f"environment override name mismatch: {details}")
    rendered = list(lines)
    for name, replacement in replacements.items():
        rendered[indexes[name][0]] = replacement
    return tuple(rendered)


def _step(
    name: str,
    arguments: tuple[str, ...],
    env_lines: tuple[str, ...],
    executions: tuple[ExpectedExecution, ...],
    phases: tuple[WorkloadPhase, ...],
    dataset: DatasetExpectation = DatasetExpectation.CLEANED,
    *,
    timeout_seconds: int = 300,
    requires: tuple[str, ...] = (),
    exports: tuple[str, ...] = (),
    failure_injection: FailureInjection = FailureInjection.NONE,
    preserve_failure_staging: bool = False,
) -> ScenarioStep:
    """Build a sweep step with bounded defaults."""
    return ScenarioStep(
        name=name,
        kind=CommandKind.SWEEP,
        arguments=arguments,
        env_lines=env_lines,
        support_files=(),
        generated_inputs=(),
        timeout_seconds=timeout_seconds,
        executions=executions,
        required_phases=phases,
        dataset=dataset,
        requires=requires,
        exports=exports,
        failure_injection=failure_injection,
        preserve_failure_staging=preserve_failure_staging,
    )


def _baseline() -> FilesystemScenarioSpec:
    return FilesystemScenarioSpec(
        "baseline",
        _BASELINE_SUBSTRATES,
        (
            _step(
                "buffered-sweep",
                ("--bio", "--nodes", "1,2"),
                _SHARED_ENV,
                _coordinates((1, 2), ("4K",), (1,), (1,)),
                _NORMAL_PHASES,
            ),
        ),
    )


def _default_dio() -> FilesystemScenarioSpec:
    return FilesystemScenarioSpec(
        "default-dio",
        _BASELINE_SUBSTRATES,
        (
            _step(
                "direct-worker-directories",
                ("--nodes", "1,2"),
                _WORKER_ENV,
                _coordinates((1, 2), ("4K",), (1,), (1,)),
                (
                    WorkloadPhase.DIRECTORY_CREATE,
                    WorkloadPhase.WRITE,
                    WorkloadPhase.TREE_SCAN,
                    WorkloadPhase.READ,
                    WorkloadPhase.REMOVE_FILES,
                ),
            ),
        ),
    )


def _failure_resume() -> FilesystemScenarioSpec:
    coordinates = tuple(
        execution.coordinate
        for execution in _coordinates((1, 2), ("4K", "8K"), (1,), (1,))
    )
    statuses = {
        coordinates[1]: ExecutionStatus.FAILED,
        coordinates[2]: ExecutionStatus.PENDING,
        coordinates[3]: ExecutionStatus.PENDING,
    }
    initial = _coordinates((1, 2), ("4K", "8K"), (1,), (1,), statuses=statuses)
    env_lines = _override_env(
        _SHARED_ENV,
        {"ELBENCHO_SCALE_IO_SIZES": ('export ELBENCHO_SCALE_IO_SIZES=("4K" "8K")')},
    )
    first = _step(
        "inject-one-failure",
        ("--write-no-read", "--nodes", "1,2"),
        env_lines,
        initial,
        (
            WorkloadPhase.DIRECTORY_CREATE,
            WorkloadPhase.WRITE,
            WorkloadPhase.REMOVE_FILES,
        ),
        exports=("failed_results_dir",),
        failure_injection=FailureInjection.FAIL_AFTER_WRITE_ONCE,
        preserve_failure_staging=True,
    )
    resume = ScenarioStep(
        name="resume",
        kind=CommandKind.RESUME,
        arguments=("--resume", "{failed_results_dir}"),
        env_lines=(),
        support_files=(),
        generated_inputs=(),
        timeout_seconds=300,
        executions=_coordinates((1, 2), ("4K", "8K"), (1,), (1,)),
        required_phases=(WorkloadPhase.WRITE, WorkloadPhase.REMOVE_FILES),
        dataset=DatasetExpectation.CLEANED,
        requires=("failed_results_dir",),
        preserve_failure_staging=True,
    )
    return FilesystemScenarioSpec(
        "failure-resume", _BASELINE_SUBSTRATES, (first, resume)
    )


def _retained_lifecycle() -> FilesystemScenarioSpec:
    coordinate = _coordinates((1,), ("4K",), (1,), (1,))
    write = _step(
        "write-only",
        ("--write-only", "--nodes", "1"),
        _SHARED_ENV,
        coordinate,
        (WorkloadPhase.DIRECTORY_CREATE, WorkloadPhase.WRITE),
        DatasetExpectation.PRESERVED,
        exports=("retained_data_dir",),
    )
    read_miss = _step(
        "read-cache-miss",
        ("--read-from", "{retained_data_dir}", "--nodes", "1"),
        _SHARED_ENV,
        coordinate,
        (WorkloadPhase.TREE_SCAN, WorkloadPhase.READ),
        DatasetExpectation.PRESERVED,
        requires=("retained_data_dir",),
    )
    read_hit = _step(
        "read-cache-hit",
        ("--read-from", "{retained_data_dir}", "--nodes", "1"),
        _SHARED_ENV,
        coordinate,
        (WorkloadPhase.READ,),
        DatasetExpectation.PRESERVED,
        requires=("retained_data_dir",),
    )
    delete = ScenarioStep(
        name="delete-only",
        kind=CommandKind.DELETE,
        arguments=("--delete-only", "{retained_data_dir}"),
        env_lines=_SHARED_ENV,
        support_files=(),
        generated_inputs=(),
        timeout_seconds=180,
        executions=(),
        required_phases=(WorkloadPhase.DELETE_PATH,),
        dataset=DatasetExpectation.REMOVED,
        requires=("retained_data_dir",),
    )
    return FilesystemScenarioSpec(
        "retained-lifecycle", _BOTH_SUBSTRATES, (write, read_miss, read_hit, delete)
    )


def _live_capture() -> FilesystemScenarioSpec:
    env_lines = _override_env(
        _SHARED_ENV,
        {
            "ELBENCHO_SCALE_READ_WRITE_DURATION": (
                "export ELBENCHO_SCALE_READ_WRITE_DURATION=3"
            ),
            "ELBENCHO_FILES_PER_NODE": "export ELBENCHO_FILES_PER_NODE=2",
            "ELBENCHO_LIVE_CSV_EXTENDED": "export ELBENCHO_LIVE_CSV_EXTENDED=1",
        },
    ) + ("export ELBENCHO_LIVEINT=10",)
    return FilesystemScenarioSpec(
        "live-capture",
        _BASELINE_SUBSTRATES,
        (
            _step(
                "extended-live-csv",
                ("--nodes", "2"),
                env_lines,
                _coordinates((2,), ("4K",), (1,), (1,)),
                (
                    WorkloadPhase.DIRECTORY_CREATE,
                    WorkloadPhase.WRITE,
                    WorkloadPhase.READ,
                    WorkloadPhase.REMOVE_FILES,
                ),
            ),
        ),
    )


def _kubectl_retained_read() -> FilesystemScenarioSpec:
    """Exercise retained data across independently collected attempts."""
    coordinate = _coordinates((1,), ("4K",), (1,), (1,))
    write = _step(
        "write-only",
        ("--write-only", "--nodes", "1"),
        _SHARED_ENV,
        coordinate,
        (WorkloadPhase.DIRECTORY_CREATE, WorkloadPhase.WRITE),
        DatasetExpectation.PRESERVED,
        exports=("retained_data_dir",),
    )
    read = _step(
        "read-from",
        ("--read-from", "{retained_data_dir}", "--nodes", "1"),
        _SHARED_ENV,
        coordinate,
        (WorkloadPhase.TREE_SCAN, WorkloadPhase.READ),
        DatasetExpectation.PRESERVED,
        requires=("retained_data_dir",),
    )
    return FilesystemScenarioSpec("kubectl-retained-read", _KUBECTL_ONLY, (write, read))


def _kubectl_cancel() -> FilesystemScenarioSpec:
    """Describe a long enough run for the adapter to cancel while active."""
    env_lines = _override_env(
        _SHARED_ENV,
        {
            "ELBENCHO_SCALE_READ_WRITE_DURATION": (
                "export ELBENCHO_SCALE_READ_WRITE_DURATION=30"
            ),
        },
    )
    step = _step(
        "cancel-running",
        ("--write-no-read", "--nodes", "2"),
        env_lines,
        _coordinates(
            (2,),
            ("4K",),
            (1,),
            (1,),
            statuses={(ExecutionCoordinate(2, "4K", 1, 1)): ExecutionStatus.PENDING},
        ),
        (WorkloadPhase.DIRECTORY_CREATE, WorkloadPhase.WRITE),
        DatasetExpectation.CLEANED,
        timeout_seconds=300,
    )
    return FilesystemScenarioSpec("kubectl-cancel", _KUBECTL_ONLY, (step,))


def _kubectl_coordinator_loss() -> FilesystemScenarioSpec:
    """Describe coordinator loss with a collected, resumable attempt."""
    env_lines = _override_env(
        _SHARED_ENV,
        {
            "ELBENCHO_SCALE_READ_WRITE_DURATION": (
                "export ELBENCHO_SCALE_READ_WRITE_DURATION=30"
            ),
        },
    )
    coordinate = ExecutionCoordinate(2, "4K", 1, 1)
    interrupted = _step(
        "delete-coordinator",
        ("--write-no-read", "--nodes", "2"),
        env_lines,
        (ExpectedExecution(coordinate, ExecutionStatus.RUNNING),),
        (WorkloadPhase.DIRECTORY_CREATE, WorkloadPhase.WRITE),
        DatasetExpectation.CLEANED,
        timeout_seconds=300,
        exports=("failed_results_dir",),
    )
    resume = ScenarioStep(
        name="resume",
        kind=CommandKind.RESUME,
        arguments=("--resume", "{failed_results_dir}"),
        env_lines=(),
        support_files=(),
        generated_inputs=(),
        timeout_seconds=300,
        executions=(ExpectedExecution(coordinate),),
        required_phases=(WorkloadPhase.WRITE, WorkloadPhase.REMOVE_FILES),
        dataset=DatasetExpectation.CLEANED,
        requires=("failed_results_dir",),
    )
    return FilesystemScenarioSpec(
        "kubectl-coordinator-loss", _KUBECTL_ONLY, (interrupted, resume)
    )


def _kubectl_endpoint_drift() -> FilesystemScenarioSpec:
    """Describe replacement of a worker after endpoint freezing."""
    env_lines = _override_env(
        _SHARED_ENV,
        {
            "ELBENCHO_SCALE_READ_WRITE_DURATION": (
                "export ELBENCHO_SCALE_READ_WRITE_DURATION=15"
            ),
        },
    )
    coordinate = ExecutionCoordinate(2, "4K", 1, 1)
    step = _step(
        "replace-worker",
        ("--write-no-read", "--nodes", "2"),
        env_lines,
        (ExpectedExecution(coordinate, ExecutionStatus.SUCCESS),),
        (WorkloadPhase.DIRECTORY_CREATE, WorkloadPhase.WRITE),
        DatasetExpectation.CLEANED,
        timeout_seconds=300,
    )
    return FilesystemScenarioSpec("kubectl-endpoint-drift", _KUBECTL_ONLY, (step,))


def _slurm_cartesian() -> FilesystemScenarioSpec:
    env_lines = _override_env(
        _SHARED_ENV,
        {
            "ELBENCHO_FILES_PER_NODE": "export ELBENCHO_FILES_PER_NODE=2",
            "ELBENCHO_FILE_SIZE": 'export ELBENCHO_FILE_SIZE="1M"',
            "ELBENCHO_SCALE_THREAD_LIST": (
                'export ELBENCHO_SCALE_THREAD_LIST=("1" "2")'
            ),
            "ELBENCHO_SCALE_IO_SIZES": (
                'export ELBENCHO_SCALE_IO_SIZES=("4K" "4K,r8K")'
            ),
            "ELBENCHO_IODEPTH_LIST": ('export ELBENCHO_IODEPTH_LIST=("1" "2")'),
        },
    )
    return FilesystemScenarioSpec(
        "slurm-cartesian",
        _SLURM_ONLY,
        (
            _step(
                "representative-matrix",
                ("--nodes", "1,2"),
                env_lines,
                _coordinates((1, 2), ("4K", "4K,r8K"), (1, 2), (1, 2)),
                _NORMAL_PHASES,
                timeout_seconds=600,
            ),
        ),
    )


def _ssh_single_big_file() -> FilesystemScenarioSpec:
    env_lines = (
        "unset TEST_DIRS",
        'declare -A TEST_DIRS=(["{test_root}"]=1)',
        'export ELBENCHO_FILE_LAYOUT="worker-directories"',
        "export ELBENCHO_FILES_PER_NODE=",
        "export ELBENCHO_FILE_SIZE=",
        'export ELBENCHO_SCALE_THREAD_LIST=("1")',
        'export ELBENCHO_SCALE_IO_SIZES=("4K")',
        'export ELBENCHO_IODEPTH_LIST=("1")',
        "export ELBENCHO_SCALE_READ_WRITE_DURATION=1",
        "export ELBENCHO_SINGLE_BIG_FILE=1",
        'export ELBENCHO_SINGLE_BIG_FILE_BASENAME="integration-bigfile"',
        'export ELBENCHO_SINGLE_BIG_FILE_SIZE="16M"',
        "export ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0",
    )
    coordinate = _coordinates((2,), ("4K",), (1,), (1,))
    generated = _step(
        "cooperative-readwrite",
        ("--bio", "--nodes", "1,2"),
        env_lines,
        _coordinates((1, 2), ("4K",), (1,), (1,)),
        (WorkloadPhase.WRITE, WorkloadPhase.READ, WorkloadPhase.REMOVE_FILES),
    )
    read_env = _override_env(
        env_lines,
        {
            "ELBENCHO_ALL_NODES_ACCESS_ALL_DATA": (
                "export ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=1"
            )
        },
    )
    read = ScenarioStep(
        name="inferred-extent-read",
        kind=CommandKind.SWEEP,
        arguments=(
            "--read-from",
            "{test_root}/staged-input/integration-bigfile",
            "--bio",
            "--nodes",
            "2",
        ),
        env_lines=read_env,
        support_files=(),
        generated_inputs=(
            GeneratedInput("staged-input/integration-bigfile", 16 * 1024 * 1024),
        ),
        timeout_seconds=300,
        executions=coordinate,
        required_phases=(WorkloadPhase.READ,),
        dataset=DatasetExpectation.PRESERVED,
    )
    delete = ScenarioStep(
        name="delete-retained-directory",
        kind=CommandKind.DELETE,
        arguments=("--delete-only", "{test_root}/staged-input"),
        env_lines=env_lines,
        support_files=(),
        generated_inputs=(),
        timeout_seconds=180,
        executions=(),
        required_phases=(WorkloadPhase.DELETE_PATH,),
        dataset=DatasetExpectation.REMOVED,
    )
    return FilesystemScenarioSpec(
        "ssh-single-big-file", _SSH_ONLY, (generated, read, delete)
    )


def _ssh_weighted_roots() -> FilesystemScenarioSpec:
    env_lines = (
        "unset TEST_DIRS",
        'declare -A TEST_DIRS=(["{test_root}"]=1 ["{test_root_secondary}"]=2)',
        'export ELBENCHO_FILE_LAYOUT="worker-directories"',
        "export ELBENCHO_FILES_PER_NODE=",
        'export ELBENCHO_FILE_SIZE="1M"',
        'export ELBENCHO_SCALE_THREAD_LIST=("1")',
        'export ELBENCHO_SCALE_IO_SIZES=("4K")',
        'export ELBENCHO_IODEPTH_LIST=("1")',
        "export ELBENCHO_SCALE_READ_WRITE_DURATION=1",
        "export FS_MAX_AGG_THROUGHPUT=1",
        "export FS_MAX_NODE_THROUGHPUT_GBPS=1",
        "export FS_MAX_NODE_IOPS=100",
        "export ELBENCHO_SINGLE_BIG_FILE=0",
    )
    return FilesystemScenarioSpec(
        "ssh-weighted-roots",
        _SSH_ONLY,
        (
            _step(
                "active-single-sizing",
                ("--bio", "--single", "--nodes", "1"),
                env_lines,
                _coordinates((1,), ("4K",), (1,), (1,)),
                _NORMAL_PHASES,
            ),
        ),
    )


def _ssh_shared_home() -> FilesystemScenarioSpec:
    return FilesystemScenarioSpec(
        "ssh-shared-home",
        _SSH_ONLY,
        (
            _step(
                "shared-home-smoke",
                ("--bio", "--nodes", "1,2"),
                _SHARED_ENV + ("export SSH_HOMEDIR_SHARED=1",),
                _coordinates((1, 2), ("4K",), (1,), (1,)),
                _NORMAL_PHASES,
            ),
        ),
    )


def _slurm_scheduling() -> FilesystemScenarioSpec:
    support = (
        SupportFile("slurm-scenario-includes", "{slurm_node_1}\n{slurm_node_2}\n"),
        SupportFile("slurm-scenario-ignores", "{slurm_node_2}\n"),
    )
    env_lines = _override_env(
        _SHARED_ENV,
        {"ELBENCHO_FILE_SIZE": 'export ELBENCHO_FILE_SIZE="4M"'},
    ) + (
        "SLURM_EXCLUSIVE_USER=1",
        'SLURM_JOB_NAME_PREFIX="itest-"',
        'export SLURM_NODE_INCLUDES="{workspace}/slurm-scenario-includes"',
        'export SLURM_NODE_IGNORES="{workspace}/slurm-scenario-ignores"',
    )
    step = ScenarioStep(
        name="allocation-options",
        kind=CommandKind.SWEEP,
        arguments=("--nodes", "1"),
        env_lines=env_lines,
        support_files=support,
        generated_inputs=(),
        timeout_seconds=300,
        executions=_coordinates((1,), ("4K",), (1,), (1,)),
        required_phases=_NORMAL_PHASES,
        dataset=DatasetExpectation.CLEANED,
    )
    return FilesystemScenarioSpec("slurm-scheduling", _SLURM_ONLY, (step,))


SCENARIO_SPECS = (
    _baseline(),
    _default_dio(),
    _failure_resume(),
    _retained_lifecycle(),
    _live_capture(),
    _kubectl_retained_read(),
    _kubectl_cancel(),
    _kubectl_coordinator_loss(),
    _kubectl_endpoint_drift(),
    _slurm_cartesian(),
    _ssh_single_big_file(),
    _ssh_weighted_roots(),
    _ssh_shared_home(),
    _slurm_scheduling(),
)

SCENARIO_SPECS_BY_NAME = {spec.name: spec for spec in SCENARIO_SPECS}


def validate_scenario_specs(
    specifications: Iterable[FilesystemScenarioSpec] = SCENARIO_SPECS,
) -> None:
    """Reject inconsistent catalog entries before a real fixture is touched."""
    specs = tuple(specifications)
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise ScenarioSpecError("scenario specification names must be unique")
    for spec in specs:
        _validate_spec(spec)


def _validate_spec(spec: FilesystemScenarioSpec) -> None:
    """Validate one scenario and its sequence dependencies."""
    if not spec.name or not spec.steps:
        raise ScenarioSpecError("scenario names and step sequences must be nonempty")
    allowed_substrates = _BOTH_SUBSTRATES | {"kubectl"}
    if not spec.substrates or not spec.substrates <= allowed_substrates:
        raise ScenarioSpecError(f"{spec.name}: invalid substrates")
    available: set[str] = set()
    step_names: set[str] = set()
    for step in spec.steps:
        if step.name in step_names:
            raise ScenarioSpecError(f"{spec.name}: duplicate step {step.name}")
        step_names.add(step.name)
        missing = set(step.requires) - available
        if missing:
            raise ScenarioSpecError(
                f"{spec.name}/{step.name}: unavailable values: {sorted(missing)}"
            )
        _validate_step(spec.name, step)
        available.update(step.exports)


def _validate_step(scenario_name: str, step: ScenarioStep) -> None:
    """Validate bounded execution and expectation invariants for one step."""
    label = f"{scenario_name}/{step.name}"
    if not 1 <= step.timeout_seconds <= 600:
        raise ScenarioSpecError(f"{label}: timeout must be between 1 and 600 seconds")
    coordinates = [execution.coordinate for execution in step.executions]
    if len(coordinates) != len(set(coordinates)):
        raise ScenarioSpecError(f"{label}: duplicate execution coordinates")
    if step.kind is CommandKind.DELETE and step.executions:
        raise ScenarioSpecError(f"{label}: delete steps cannot reify executions")
    if step.kind is not CommandKind.DELETE and not step.executions:
        raise ScenarioSpecError(f"{label}: sweep and resume steps require executions")
    if step.failure_injection is not FailureInjection.NONE:
        if not any(
            execution.status is ExecutionStatus.FAILED for execution in step.executions
        ):
            raise ScenarioSpecError(f"{label}: failure injection lacks failed state")
    if len(step.required_phases) != len(set(step.required_phases)):
        raise ScenarioSpecError(f"{label}: duplicate required phases")


validate_scenario_specs()
