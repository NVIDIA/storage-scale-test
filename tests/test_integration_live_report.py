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

"""Contracts for the live-capture integration report checks."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPOSITORY_ROOT / "integration-tests" / "lib"))

import filesystem_integration as _INTEGRATION  # pylint: disable=wrong-import-position
import filesystem_scenario_specs as _SPECS  # pylint: disable=wrong-import-position

_problems = _INTEGRATION._live_object_problems  # pylint: disable=protected-access


def _live(**changes):
    live = {
        "client_mode": "service",
        "clients": 2,
        "configured_interval_ms": 10,
        "steady": {"end_source": "elbencho_first", "reason": "too_short"},
        "aggregate_series": {"io_latency_avg_us": [None, 41.5]},
    }
    live.update(changes)
    return live


def _document(write_live, read_live=None):
    phases = [{"phase": "write", "live": write_live}]
    phases.append({"phase": "read", "live": read_live or _live()})
    return {"executions": [{"execution_id": "0001", "results": {"phases": phases}}]}


def test_two_service_capture_passes():
    assert not _problems(_document(_live()))


def test_each_broken_contract_is_named():
    cases = {
        "no live analysis": {"reason": "not_captured"},
        "client_mode": _live(client_mode="worker"),
        "clients": _live(clients=1),
        "configured_interval_ms": _live(configured_interval_ms=None),
        "steady": _live(steady={"end_source": None, "reason": "no_first_time"}),
        "Lat IO us": _live(aggregate_series={"io_latency_avg_us": [None]}),
    }
    for expected, write_live in cases.items():
        problems = _problems(_document(write_live))
        assert len(problems) == 1 and expected in problems[0], (expected, problems)


def test_empty_document_is_a_problem():
    assert _problems({}) == ["no executions"]


def test_rendered_environment_captures_by_default(tmp_path):
    """The fixture's explicit 0 must not survive into the live-capture run."""
    fixture = SimpleNamespace(
        architecture="x86_64",
        ssh_home_mode="separate",
        ssh_addresses=("worker-a", "worker-b"),
        slurm_nodes=("compute-0", "compute-1"),
    )
    step = _SPECS.SCENARIO_SPECS_BY_NAME["live-capture"].steps[0]
    source_base = 'source "${SCALE_TEST_BASE}/lib/env_base.sh"'
    for selector in ("ssh", "slurm"):
        rendered, _ = _INTEGRATION._render_env(  # pylint: disable=protected-access
            _REPOSITORY_ROOT / "env.sh.template",
            selector,
            str(tmp_path),
            fixture,
            extra_env=step.render_env({"test_key": str(tmp_path)}),
        )
        assert rendered.count(source_base) == 1
        script = rendered.replace(
            source_base,
            f'source "{_REPOSITORY_ROOT}/lib/env_functions.sh"\n'
            "apply_filesystem_workload_defaults\n"
            'echo "$ELBENCHO_LIVE_CSV_EXTENDED $ELBENCHO_LIVEINT"',
        )
        result = subprocess.run(
            ["bash", "-c", script], check=False, text=True, capture_output=True
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines()[-1] == "1 10", selector
