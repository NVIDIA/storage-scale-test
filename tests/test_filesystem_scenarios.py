#!/usr/bin/env python3

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

"""Tests for filesystem integration scenario selection and scheduling."""

import importlib.util
import itertools
import sys
from dataclasses import replace
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PATH = _REPO_ROOT / "integration-tests" / "lib" / "scenario_planner.py"
_SPEC = importlib.util.spec_from_file_location(
    "filesystem_scenarios_under_test", _MODULE_PATH
)
assert _SPEC and _SPEC.loader
_SCENARIOS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _SCENARIOS
_SPEC.loader.exec_module(_SCENARIOS)

Scenario = _SCENARIOS.Scenario
ScenarioPlanningError = _SCENARIOS.ScenarioPlanningError
SchedulePhase = _SCENARIOS.SchedulePhase
SshHomeMode = _SCENARIOS.SshHomeMode
SshHomeTransition = _SCENARIOS.SshHomeTransition
Substrate = _SCENARIOS.Substrate
WorkItem = _SCENARIOS.WorkItem
plan_scenarios = _SCENARIOS.plan_scenarios
format_scenario_listing = _SCENARIOS.format_scenario_listing
scenario_metadata = _SCENARIOS.scenario_metadata
select_scenarios = _SCENARIOS.select_scenarios


def _step_identity(step):
    """Return a compact identity for a plan step."""
    if isinstance(step, WorkItem):
        return ("work", step.scenario.name, step.substrate.value)
    return ("transition", step.target.value)


def _plan_identity(**kwargs):
    """Return compact identities for a scenario plan."""
    kwargs.setdefault("registry", _SCENARIOS.SCENARIO_CATALOG)
    return tuple(_step_identity(step) for step in plan_scenarios(**kwargs))


def test_catalog_exposes_stable_machine_metadata():
    """Listing metadata describes selection without freezing prose."""
    metadata = scenario_metadata(_SCENARIOS.SCENARIO_CATALOG)

    assert [item["name"] for item in metadata] == [
        "baseline",
        "default-dio",
        "failure-resume",
        "retained-lifecycle",
        "live-capture",
        "slurm-cartesian",
        "ssh-single-big-file",
        "ssh-weighted-roots",
        "ssh-shared-home",
        "kubectl-retained-read",
        "kubectl-cancel",
        "kubectl-coordinator-loss",
        "kubectl-endpoint-drift",
        "slurm-scheduling",
    ]
    shared = next(item for item in metadata if item["name"] == "ssh-shared-home")
    assert shared["substrates"] == ["ssh"]
    assert shared["ssh_home_mode"] == "shared"
    assert shared["schedule_phase"] == "shared_home"


def test_human_listing_exposes_names_and_substrate_compatibility():
    """Human listing includes stable selection facts without freezing prose."""
    rows = {
        fields[0]: fields[1]
        for line in format_scenario_listing(_SCENARIOS.SCENARIO_CATALOG).splitlines()
        if (fields := line.split("\t", 2))
    }

    assert rows["baseline"] == "kubectl,slurm,ssh"
    assert rows["ssh-shared-home"] == "ssh"
    assert rows["slurm-scheduling"] == "slurm"


def test_default_plan_expands_substrates_and_batches_shared_home():
    """The full plan has one bounded shared-home SSH transition."""
    plan = _plan_identity()

    shared_start = plan.index(("transition", "shared"))
    shared_stop = plan.index(("transition", "separate"))
    assert plan[shared_start + 1 : shared_stop] == (("work", "ssh-shared-home", "ssh"),)
    assert plan[shared_stop + 1 :] == (
        ("work", "baseline", "kubectl"),
        ("work", "default-dio", "kubectl"),
        ("work", "failure-resume", "kubectl"),
        ("work", "live-capture", "kubectl"),
        ("work", "kubectl-retained-read", "kubectl"),
        ("work", "kubectl-cancel", "kubectl"),
        ("work", "kubectl-coordinator-loss", "kubectl"),
        ("work", "kubectl-endpoint-drift", "kubectl"),
        ("work", "slurm-scheduling", "slurm"),
    )
    assert all(
        substrate != "ssh"
        for kind, _name, substrate in plan[:shared_start]
        if kind == "work" and _name == "ssh-shared-home"
    )


def test_ssh_selection_excludes_slurm_work():
    """An SSH plan contains only SSH work and still restores home mode."""
    plan = plan_scenarios(substrate="ssh", registry=_SCENARIOS.SCENARIO_CATALOG)

    work = [step for step in plan if isinstance(step, WorkItem)]
    assert work
    assert {item.substrate for item in work} == {Substrate.SSH}
    assert isinstance(plan[-1], SshHomeTransition)
    assert plan[-1].target is SshHomeMode.SEPARATE


def test_slurm_selection_needs_no_home_transition():
    """A Slurm-only plan cannot mutate the SSH worker pool."""
    plan = plan_scenarios(
        substrate=Substrate.SLURM, registry=_SCENARIOS.SCENARIO_CATALOG
    )

    assert plan
    assert all(isinstance(step, WorkItem) for step in plan)
    assert {step.substrate for step in plan} == {Substrate.SLURM}


def test_kubectl_selection_has_no_ssh_transition():
    """Kubernetes work is independently selectable and has no SSH side effect."""
    plan = plan_scenarios(
        substrate=Substrate.KUBECTL, registry=_SCENARIOS.SCENARIO_CATALOG
    )

    assert all(isinstance(step, WorkItem) for step in plan)
    assert all(step.substrate is Substrate.KUBECTL for step in plan)
    assert [step.scenario.name for step in plan] == [
        "baseline",
        "default-dio",
        "failure-resume",
        "live-capture",
        "kubectl-retained-read",
        "kubectl-cancel",
        "kubectl-coordinator-loss",
        "kubectl-endpoint-drift",
    ]


def test_requested_scenarios_are_order_independent():
    """Repeatable CLI selections do not determine execution order."""
    names = ("slurm-scheduling", "baseline", "ssh-shared-home")
    expected = _plan_identity(requested=names)

    for permutation in itertools.permutations(names):
        assert _plan_identity(requested=permutation) == expected


def test_registry_order_does_not_determine_execution_order():
    """Registry refactoring cannot silently change execution order."""
    expected = _plan_identity()

    assert _plan_identity(registry=reversed(_SCENARIOS.SCENARIO_CATALOG)) == expected


@pytest.mark.parametrize("substrate", ("ssh", "slurm", "kubectl"))
def test_explicit_incompatible_scenario_is_rejected(substrate):
    """An explicit scenario/substrate mismatch is actionable."""
    scenario = {
        "ssh": "slurm-cartesian",
        "slurm": "ssh-shared-home",
        "kubectl": "slurm-cartesian",
    }[substrate]

    with pytest.raises(ScenarioPlanningError, match="incompatible"):
        plan_scenarios(
            substrate=substrate,
            requested=(scenario,),
            registry=_SCENARIOS.SCENARIO_CATALOG,
        )


def test_duplicate_and_unknown_requests_are_rejected():
    """Typos and duplicate repeatable options do not produce surprising work."""
    with pytest.raises(ScenarioPlanningError, match="duplicate requested"):
        select_scenarios(requested=("baseline", "baseline"))
    with pytest.raises(ScenarioPlanningError, match="unknown scenario"):
        select_scenarios(requested=("not-a-scenario",))


def test_duplicate_registry_names_are_rejected():
    """The registry has one authoritative definition per name."""
    duplicate = replace(_SCENARIOS.SCENARIO_CATALOG[0], description="Duplicate")

    with pytest.raises(ScenarioPlanningError, match="duplicate scenario in registry"):
        scenario_metadata((*_SCENARIOS.SCENARIO_CATALOG, duplicate))


def test_shared_home_metadata_constraints_are_enforced():
    """Shared mode cannot leak outside one SSH-only scheduling phase."""
    invalid = Scenario(
        "invalid-shared",
        "Invalid",
        frozenset({Substrate.SSH, Substrate.SLURM}),
        1,
        phase=SchedulePhase.SHARED_HOME,
        ssh_home_mode=SshHomeMode.SHARED,
    )

    with pytest.raises(ScenarioPlanningError, match="must be SSH-only"):
        scenario_metadata((invalid,))


def test_selected_scenarios_follow_explicit_priority_and_tiebreaker():
    """Same-phase scenarios use order then stable name as tie-breakers."""
    later_name = Scenario(
        "zeta",
        "Zeta",
        frozenset({Substrate.SLURM}),
        5,
    )
    earlier_name = replace(later_name, name="alpha", description="Alpha")

    selected = select_scenarios(registry=(later_name, earlier_name))

    assert [scenario.name for scenario in selected] == ["alpha", "zeta"]


def test_all_selector_filters_no_scenarios():
    """The all selector is accepted only as a request, not scenario metadata."""
    selected = select_scenarios(substrate=Substrate.ALL)

    assert len(selected) == len(_SCENARIOS.SCENARIOS)
    invalid = Scenario(
        "all-substrate",
        "Invalid",
        frozenset({Substrate.ALL}),
        1,
    )
    with pytest.raises(ScenarioPlanningError, match="concrete substrates"):
        scenario_metadata((invalid,))


def test_unknown_substrate_is_actionable():
    """Planner wraps invalid substrate values in its public error type."""
    with pytest.raises(ScenarioPlanningError, match="unknown substrate"):
        plan_scenarios(substrate="kubernetes")
