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

"""Contract tests for declarative filesystem integration workloads."""

import importlib.util
from dataclasses import replace
from pathlib import Path
import sys

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "integration-tests"
    / "lib"
    / "filesystem_scenario_specs.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "filesystem_scenario_specs", _MODULE_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
_SCENARIOS = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _SCENARIOS
_SPEC.loader.exec_module(_SCENARIOS)

_PLANNER_PATH = _MODULE_PATH.with_name("scenario_planner.py")
_PLANNER_SPEC = importlib.util.spec_from_file_location(
    "filesystem_spec_scenario_planner", _PLANNER_PATH
)
assert _PLANNER_SPEC is not None and _PLANNER_SPEC.loader is not None
_PLANNER = importlib.util.module_from_spec(_PLANNER_SPEC)
sys.modules[_PLANNER_SPEC.name] = _PLANNER
_PLANNER_SPEC.loader.exec_module(_PLANNER)

CommandKind = _SCENARIOS.CommandKind
DatasetExpectation = _SCENARIOS.DatasetExpectation
ExecutionStatus = _SCENARIOS.ExecutionStatus
FailureInjection = _SCENARIOS.FailureInjection
ScenarioSpecError = _SCENARIOS.ScenarioSpecError
WorkloadPhase = _SCENARIOS.WorkloadPhase
validate_scenario_specs = _SCENARIOS.validate_scenario_specs


EXPECTED_NAMES = {
    "baseline",
    "default-dio",
    "failure-resume",
    "retained-lifecycle",
    "live-capture",
    "slurm-cartesian",
    "ssh-single-big-file",
    "ssh-weighted-roots",
    "ssh-shared-home",
    "slurm-scheduling",
}


def _scenario(name):
    return _SCENARIOS.SCENARIO_SPECS_BY_NAME[name]


def test_catalog_defines_every_planned_real_scenario():
    """The execution catalog and workload catalog have matching stable names."""
    assert set(_SCENARIOS.SCENARIO_SPECS_BY_NAME) == EXPECTED_NAMES
    assert {scenario.name for scenario in _PLANNER.SCENARIO_CATALOG} == EXPECTED_NAMES
    validate_scenario_specs()


def test_each_step_is_bounded_and_has_semantic_expectations():
    """Real workloads remain bounded without freezing incidental artifacts."""
    for scenario in _SCENARIOS.SCENARIO_SPECS:
        for step in scenario.steps:
            assert 0 < step.timeout_seconds <= 600
            assert step.required_phases
            assert step.dataset in DatasetExpectation
            assert not hasattr(step, "expected_filenames")
            assert not hasattr(step, "expected_native_arguments")


@pytest.mark.parametrize("name", ("baseline", "default-dio", "ssh-shared-home"))
def test_basic_real_sweeps_cover_one_and_two_nodes(name):
    """Transport smokes exercise node subset changes on their real substrates."""
    step = _scenario(name).steps[0]
    assert {execution.coordinate.nodes for execution in step.executions} == {1, 2}
    assert all(
        execution.status is ExecutionStatus.SUCCESS for execution in step.executions
    )


def test_default_dio_uses_worker_layout_and_derived_file_size():
    """The default-path scenario does not accidentally reuse the BIO fixture."""
    step = _scenario("default-dio").steps[0]
    environment = "\n".join(step.env_lines)

    assert step.arguments == ("--nodes", "1,2")
    assert 'ELBENCHO_FILE_LAYOUT="worker-directories"' in environment
    assert "ELBENCHO_FILE_SIZE=" in environment
    assert "ELBENCHO_FILE_SIZE_MULTIPLIER=4096" in environment
    assert WorkloadPhase.TREE_SCAN in step.required_phases


def test_failure_resume_preserves_overlay_through_resume():
    """One atomic injected failure is not restaged between attempts."""
    initial, resume = _scenario("failure-resume").steps
    statuses = [execution.status for execution in initial.executions]

    assert statuses == [
        ExecutionStatus.SUCCESS,
        ExecutionStatus.FAILED,
        ExecutionStatus.PENDING,
        ExecutionStatus.PENDING,
    ]
    assert initial.failure_injection is FailureInjection.FAIL_AFTER_WRITE_ONCE
    assert initial.preserve_failure_staging
    assert resume.kind is CommandKind.RESUME
    assert resume.requires == ("failed_results_dir",)
    assert resume.preserve_failure_staging
    assert all(
        execution.status is ExecutionStatus.SUCCESS for execution in resume.executions
    )


def test_retained_lifecycle_expresses_cache_miss_hit_and_deletion():
    """The chained dataset contract survives reads and is deleted last."""
    steps = _scenario("retained-lifecycle").steps

    assert [step.name for step in steps] == [
        "write-only",
        "read-cache-miss",
        "read-cache-hit",
        "delete-only",
    ]
    assert [step.dataset for step in steps] == [
        DatasetExpectation.PRESERVED,
        DatasetExpectation.PRESERVED,
        DatasetExpectation.PRESERVED,
        DatasetExpectation.REMOVED,
    ]
    assert WorkloadPhase.TREE_SCAN in steps[1].required_phases
    assert WorkloadPhase.TREE_SCAN not in steps[2].required_phases
    assert steps[-1].kind is CommandKind.DELETE
    assert not steps[-1].executions


def test_live_capture_has_stable_series_sampling_window():
    """The real live-data case requests several intervals without a huge run."""
    step = _scenario("live-capture").steps[0]
    environment = "\n".join(step.env_lines)

    assert "ELBENCHO_SCALE_READ_WRITE_DURATION=3" in environment
    assert "ELBENCHO_LIVE_CSV_EXTENDED=1" in environment
    assert "ELBENCHO_LIVEINT=10" in environment
    assert {execution.coordinate.nodes for execution in step.executions} == {2}


def test_representative_cartesian_sweep_has_all_sixteen_coordinates():
    """The real Slurm matrix is representative rather than exhaustive."""
    scenario = _scenario("slurm-cartesian")
    coordinates = [execution.coordinate for execution in scenario.steps[0].executions]

    assert scenario.substrates == {"slurm"}
    assert len(coordinates) == 16
    assert {coordinate.nodes for coordinate in coordinates} == {1, 2}
    assert {coordinate.io_size for coordinate in coordinates} == {"4K", "4K,r8K"}
    assert {coordinate.threads for coordinate in coordinates} == {1, 2}
    assert {coordinate.io_depth for coordinate in coordinates} == {1, 2}


def test_single_file_sequence_uses_inferred_read_extent():
    """A retained cooperative file feeds the all-node staged read."""
    generated, read, delete = _scenario("ssh-single-big-file").steps
    read_environment = "\n".join(read.env_lines)

    assert generated.dataset is DatasetExpectation.CLEANED
    assert "{test_root}/staged-input/integration-bigfile" in read.arguments
    assert 'ELBENCHO_SINGLE_BIG_FILE_SIZE="16M"' in read_environment
    assert "export ELBENCHO_SINGLE_BIG_FILE_SIZE=" in read_environment
    assert "ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=1" in read_environment
    assert read.dataset is DatasetExpectation.PRESERVED
    assert delete.dataset is DatasetExpectation.REMOVED


def test_weighted_roots_activates_single_sizing_with_bounded_limits():
    """The SSH sizing case uses both weighted roots and deliberately low limits."""
    step = _scenario("ssh-weighted-roots").steps[0]
    environment = "\n".join(step.env_lines)

    assert step.arguments == ("--bio", "--single", "--nodes", "1")
    assert '["{test_root}"]=1' in environment
    assert '["{test_root_secondary}"]=2' in environment
    assert "FS_MAX_NODE_IOPS=100" in environment


def test_slurm_scheduling_renders_include_and_ignore_files():
    """The scheduling scenario constrains a real one-node allocation."""
    step = _scenario("slurm-scheduling").steps[0]
    rendered = step.render_support_files(
        {
            "slurm_node_1": "compute-0",
            "slurm_node_2": "compute-1",
            "workspace": "/tmp/work",
            "test_root": "/mnt/test",
        }
    )

    assert step.arguments == ("--nodes", "1")
    assert rendered[0].content == "compute-0\ncompute-1\n"
    assert rendered[1].content == "compute-1\n"
    assert "SLURM_EXCLUSIVE_USER=1" in step.env_lines
    assert 'SLURM_JOB_NAME_PREFIX="itest-"' in step.env_lines


def test_template_rendering_resolves_driver_owned_paths():
    """The driver can render env and argument templates without shell discovery."""
    step = _scenario("retained-lifecycle").steps[1]
    values = {"test_root": "/mnt/test", "retained_data_dir": "/mnt/test/data"}

    assert step.render_arguments(values) == (
        "--read-from",
        "/mnt/test/data",
        "--nodes",
        "1",
    )
    assert '["/mnt/test"]=1' in step.render_env(values)


def test_validation_rejects_missing_sequence_dependency():
    """A chained command cannot consume an artifact no prior step exported."""
    scenario = _scenario("retained-lifecycle")
    broken_first = replace(scenario.steps[0], exports=())
    broken = replace(scenario, steps=(broken_first, *scenario.steps[1:]))

    with pytest.raises(ScenarioSpecError, match="unavailable values"):
        validate_scenario_specs((broken,))


def test_validation_rejects_duplicate_coordinates():
    """One command cannot claim two result records for the same coordinate."""
    scenario = _scenario("baseline")
    step = scenario.steps[0]
    broken_step = replace(step, executions=(step.executions[0],) * 2)
    broken = replace(scenario, steps=(broken_step,))

    with pytest.raises(ScenarioSpecError, match="duplicate execution"):
        validate_scenario_specs((broken,))


def test_validation_rejects_unbounded_timeout():
    """Every real command has an explicit upper wall-time bound."""
    scenario = _scenario("baseline")
    broken_step = replace(scenario.steps[0], timeout_seconds=601)
    broken = replace(scenario, steps=(broken_step,))

    with pytest.raises(ScenarioSpecError, match="timeout"):
        validate_scenario_specs((broken,))
