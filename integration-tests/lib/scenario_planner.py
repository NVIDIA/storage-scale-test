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

"""Declare and schedule filesystem integration scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Iterable, TypeAlias


class ScenarioPlanningError(ValueError):
    """An invalid scenario selection or registry definition."""


class Substrate(StrEnum):
    """Supported integration execution substrates."""

    ALL = "all"
    SSH = "ssh"
    SLURM = "slurm"


SUBSTRATES = tuple(item.value for item in Substrate)


class SshHomeMode(StrEnum):
    """SSH worker home configurations used by scenarios."""

    SEPARATE = "separate"
    SHARED = "shared"


class SchedulePhase(IntEnum):
    """Stable phases surrounding the single shared-home SSH batch."""

    BEFORE_SHARED_HOME = 10
    SHARED_HOME = 20
    AFTER_SHARED_HOME = 30


@dataclass(frozen=True)
class Scenario:
    """Stable metadata for one filesystem integration scenario."""

    name: str
    description: str
    substrates: frozenset[Substrate]
    order: int
    phase: SchedulePhase = SchedulePhase.BEFORE_SHARED_HOME
    ssh_home_mode: SshHomeMode | None = None

    def metadata(self) -> dict[str, object]:
        """Return the stable machine-readable scenario metadata."""
        return {
            "name": self.name,
            "substrates": sorted(item.value for item in self.substrates),
            "ssh_home_mode": (
                self.ssh_home_mode.value if self.ssh_home_mode is not None else None
            ),
            "schedule_phase": self.phase.name.lower(),
            "order": self.order,
        }


@dataclass(frozen=True)
class WorkItem:
    """One scenario execution on one concrete substrate."""

    scenario: Scenario
    substrate: Substrate


@dataclass(frozen=True)
class SshHomeTransition:
    """A required transition of the singleton SSH worker pool."""

    target: SshHomeMode


PlanStep: TypeAlias = WorkItem | SshHomeTransition


SCENARIO_CATALOG = (
    Scenario(
        "baseline",
        "Shared-directory buffered-I/O baseline and report extraction",
        frozenset({Substrate.SSH, Substrate.SLURM}),
        10,
        ssh_home_mode=SshHomeMode.SEPARATE,
    ),
    Scenario(
        "default-dio",
        "Default worker-directory direct-I/O lifecycle",
        frozenset({Substrate.SSH, Substrate.SLURM}),
        20,
        ssh_home_mode=SshHomeMode.SEPARATE,
    ),
    Scenario(
        "failure-resume",
        "Real failure, cleanup, and resume lifecycle",
        frozenset({Substrate.SSH, Substrate.SLURM}),
        30,
        ssh_home_mode=SshHomeMode.SEPARATE,
    ),
    Scenario(
        "retained-lifecycle",
        "Write-only, repeated read-from, and delete-only lifecycle",
        frozenset({Substrate.SSH, Substrate.SLURM}),
        40,
        ssh_home_mode=SshHomeMode.SEPARATE,
    ),
    Scenario(
        "live-capture",
        "Extended live-data collection and reporting",
        frozenset({Substrate.SSH, Substrate.SLURM}),
        50,
        ssh_home_mode=SshHomeMode.SEPARATE,
    ),
    Scenario(
        "slurm-cartesian",
        "Representative multidimensional Slurm sweep",
        frozenset({Substrate.SLURM}),
        60,
    ),
    Scenario(
        "ssh-single-big-file",
        "Cooperative and staged single-file SSH workloads",
        frozenset({Substrate.SSH}),
        70,
        ssh_home_mode=SshHomeMode.SEPARATE,
    ),
    Scenario(
        "ssh-weighted-roots",
        "Weighted-root SSH workload with active sizing",
        frozenset({Substrate.SSH}),
        80,
        ssh_home_mode=SshHomeMode.SEPARATE,
    ),
    Scenario(
        "ssh-shared-home",
        "SSH deployment and sweep using a shared worker home",
        frozenset({Substrate.SSH}),
        90,
        phase=SchedulePhase.SHARED_HOME,
        ssh_home_mode=SshHomeMode.SHARED,
    ),
    Scenario(
        "slurm-scheduling",
        "Slurm allocation, include/exclude, and exclusive-user behavior",
        frozenset({Substrate.SLURM}),
        100,
        phase=SchedulePhase.AFTER_SHARED_HOME,
    ),
)

# The harness refactor initially preserves the real baseline. Later commits
# enable catalog entries as their substrate implementations land.
SCENARIOS = SCENARIO_CATALOG[:1]


def _concrete_substrates(substrate: Substrate) -> frozenset[Substrate]:
    """Return concrete substrates selected by *substrate*."""
    if substrate is Substrate.ALL:
        return frozenset({Substrate.SSH, Substrate.SLURM})
    return frozenset({substrate})


def _validate_scenario(scenario: Scenario) -> None:
    """Validate one registry entry."""
    if not scenario.name or scenario.name.strip() != scenario.name:
        raise ScenarioPlanningError(f"invalid scenario name: {scenario.name!r}")
    if not scenario.substrates or Substrate.ALL in scenario.substrates:
        raise ScenarioPlanningError(
            f"scenario {scenario.name!r} must declare concrete substrates"
        )
    supports_ssh = Substrate.SSH in scenario.substrates
    if supports_ssh != (scenario.ssh_home_mode is not None):
        raise ScenarioPlanningError(
            f"scenario {scenario.name!r} must declare an SSH home mode exactly "
            "when it supports SSH"
        )
    if scenario.ssh_home_mode is SshHomeMode.SHARED:
        if scenario.substrates != frozenset({Substrate.SSH}):
            raise ScenarioPlanningError(
                f"shared-home scenario {scenario.name!r} must be SSH-only"
            )
        if scenario.phase is not SchedulePhase.SHARED_HOME:
            raise ScenarioPlanningError(
                f"shared-home scenario {scenario.name!r} must use the shared phase"
            )
    elif supports_ssh and scenario.phase is not SchedulePhase.BEFORE_SHARED_HOME:
        raise ScenarioPlanningError(
            f"separate-home scenario {scenario.name!r} must run before shared home"
        )
    elif scenario.phase is SchedulePhase.SHARED_HOME:
        raise ScenarioPlanningError(
            f"scenario {scenario.name!r} cannot enter the shared phase"
        )


def _registry_by_name(registry: Iterable[Scenario]) -> dict[str, Scenario]:
    """Validate *registry* and index it by scenario name."""
    indexed: dict[str, Scenario] = {}
    for scenario in registry:
        _validate_scenario(scenario)
        if scenario.name in indexed:
            raise ScenarioPlanningError(
                f"duplicate scenario in registry: {scenario.name!r}"
            )
        indexed[scenario.name] = scenario
    return indexed


def _requested_names(requested: Iterable[str]) -> tuple[str, ...]:
    """Validate requested scenario names while preserving diagnostics."""
    names = tuple(requested)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ScenarioPlanningError(
            "duplicate requested scenario(s): " + ", ".join(duplicates)
        )
    return names


def select_scenarios(
    *,
    substrate: Substrate | str = Substrate.ALL,
    requested: Iterable[str] = (),
    registry: Iterable[Scenario] = SCENARIOS,
) -> tuple[Scenario, ...]:
    """Return selected scenarios in deterministic metadata order."""
    try:
        selected_substrate = Substrate(substrate)
    except ValueError as error:
        choices = ", ".join(item.value for item in Substrate)
        raise ScenarioPlanningError(
            f"unknown substrate {substrate!r}; choose one of: {choices}"
        ) from error
    indexed = _registry_by_name(registry)
    names = _requested_names(requested)
    unknown = sorted(set(names) - set(indexed))
    if unknown:
        raise ScenarioPlanningError("unknown scenario(s): " + ", ".join(unknown))
    candidates = [indexed[name] for name in names] if names else list(indexed.values())
    concrete = _concrete_substrates(selected_substrate)
    incompatible = [
        scenario.name
        for scenario in candidates
        if names and scenario.substrates.isdisjoint(concrete)
    ]
    if incompatible:
        raise ScenarioPlanningError(
            f"scenario(s) incompatible with {selected_substrate.value}: "
            + ", ".join(sorted(incompatible))
        )
    selected = [
        scenario
        for scenario in candidates
        if not scenario.substrates.isdisjoint(concrete)
    ]
    return tuple(sorted(selected, key=lambda item: (item.phase, item.order, item.name)))


def _work_items(
    scenarios: Iterable[Scenario], substrate: Substrate
) -> tuple[WorkItem, ...]:
    """Expand scenarios across selected concrete substrates."""
    selected_substrates = _concrete_substrates(substrate)
    items = [
        WorkItem(scenario, concrete)
        for scenario in scenarios
        for concrete in scenario.substrates & selected_substrates
    ]
    return tuple(
        sorted(
            items,
            key=lambda item: (
                item.scenario.phase,
                item.scenario.order,
                item.scenario.name,
                item.substrate.value,
            ),
        )
    )


def plan_scenarios(
    *,
    substrate: Substrate | str = Substrate.ALL,
    requested: Iterable[str] = (),
    registry: Iterable[Scenario] = SCENARIOS,
) -> tuple[PlanStep, ...]:
    """Build a deterministic execution plan with one shared-home batch."""
    scenarios = select_scenarios(
        substrate=substrate, requested=requested, registry=registry
    )
    selected_substrate = Substrate(substrate)
    items = _work_items(scenarios, selected_substrate)
    before = [
        item
        for item in items
        if item.scenario.phase is SchedulePhase.BEFORE_SHARED_HOME
    ]
    shared = [
        item for item in items if item.scenario.phase is SchedulePhase.SHARED_HOME
    ]
    after = [
        item for item in items if item.scenario.phase is SchedulePhase.AFTER_SHARED_HOME
    ]
    steps: list[PlanStep] = list(before)
    if shared:
        steps.append(SshHomeTransition(SshHomeMode.SHARED))
        steps.extend(shared)
        steps.append(SshHomeTransition(SshHomeMode.SEPARATE))
    steps.extend(after)
    return tuple(steps)


def scenario_metadata(
    registry: Iterable[Scenario] = SCENARIOS,
) -> tuple[dict[str, object], ...]:
    """Return deterministic machine-readable metadata for scenario listing."""
    indexed = _registry_by_name(registry)
    return tuple(
        scenario.metadata()
        for scenario in sorted(
            indexed.values(), key=lambda item: (item.phase, item.order, item.name)
        )
    )


def format_scenario_listing(registry: Iterable[Scenario] = SCENARIOS) -> str:
    """Format a concise human-readable scenario listing."""
    indexed = _registry_by_name(registry)
    scenarios = sorted(
        indexed.values(), key=lambda item: (item.phase, item.order, item.name)
    )
    return "\n".join(
        f"{scenario.name}\t{','.join(sorted(item.value for item in scenario.substrates))}"
        f"\t{scenario.description}"
        for scenario in scenarios
    )
