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

"""Regression tests for crash-recoverable SSH home transitions."""

import json
import sys
from pathlib import Path

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPOSITORY_ROOT / "integration-tests" / "lib"))

from ssh_home_transition import (  # pylint: disable=wrong-import-position
    STATE_FILENAME,
    PoolObservation,
    SshHomeTransitionError,
    SshHomeTransitionManager,
    TransitionState,
    TransitionStore,
    statefulset_checksum,
)

SEPARATE_CHECKSUM = "separate-checksum"
SHARED_CHECKSUM = "shared-checksum"
FIXED_TIME = "2026-09-19T00:00:00+00:00"


def _observation(
    mode="separate",
    checksum=SEPARATE_CHECKSUM,
    ready=2,
    terminating=0,
    diagnostics="",
):
    """Return one compact live-pool observation."""
    return PoolObservation(mode, checksum, ready, terminating, diagnostics)


class _Controller:
    """Injectable live pool with observable reconciliation calls."""

    def __init__(self, state_dir, observed=None):
        self.state_dir = state_dir
        self.observed = observed or _observation()
        self.calls = []
        self.fail = None
        self.state_during_reconcile = None

    def inspect(self):
        """Return the current simulated StatefulSet and pod state."""
        return self.observed

    def reconcile(self, mode, checksum):
        """Record and apply one simulated StatefulSet reconciliation."""
        self.calls.append((mode, checksum))
        state_path = self.state_dir / STATE_FILENAME
        self.state_during_reconcile = json.loads(state_path.read_text(encoding="utf-8"))
        if self.fail:
            raise self.fail
        self.observed = _observation(mode, checksum)


def _manager(tmp_path, controller):
    """Return a deterministic transition manager."""
    return SshHomeTransitionManager(
        tmp_path,
        controller.inspect,
        controller.reconcile,
        clock=lambda: FIXED_TIME,
    )


def _state(
    *,
    phase="applying-shared",
    requested_mode="shared",
    checksum=SHARED_CHECKSUM,
):
    """Return one valid retained state document."""
    return TransitionState(
        schema=1,
        run_id="run-1",
        scenario_id="ssh-shared-home",
        prior_mode="separate",
        requested_mode=requested_mode,
        expected_statefulset_checksum=checksum,
        phase=phase,
        updated_at=FIXED_TIME,
    )


def test_clean_canonical_preflight_does_not_roll_out_workers(tmp_path):
    """A healthy canonical pool is accepted without replacing SSH pods."""
    controller = _Controller(tmp_path)

    observed = _manager(tmp_path, controller).preflight(
        SEPARATE_CHECKSUM, run_id="setup", scenario_id="preflight"
    )

    assert observed.matches("separate", SEPARATE_CHECKSUM)
    assert controller.calls == []
    assert not (tmp_path / STATE_FILENAME).exists()


@pytest.mark.parametrize(
    "observed",
    [
        _observation("shared", SHARED_CHECKSUM),
        _observation("separate", "stale-separate-checksum"),
        _observation("separate", SEPARATE_CHECKSUM, terminating=1),
        _observation("unknown", "partial", ready=1),
    ],
)
def test_preflight_reconciles_noncanonical_live_state(tmp_path, observed):
    """Setup and SSH preflight restore any partial or shared pool."""
    controller = _Controller(tmp_path, observed)

    result = _manager(tmp_path, controller).preflight(
        SEPARATE_CHECKSUM, run_id="run-2", scenario_id="preflight"
    )

    assert result.matches("separate", SEPARATE_CHECKSUM)
    assert controller.calls == [("separate", SEPARATE_CHECKSUM)]
    assert controller.state_during_reconcile["phase"] == "restoring-separate"
    assert not (tmp_path / STATE_FILENAME).exists()


def test_interrupted_transition_forces_validation_rollout(tmp_path):
    """Retained intent forces reconciliation even if pods look canonical."""
    TransitionStore(tmp_path).save(_state())
    controller = _Controller(tmp_path)

    _manager(tmp_path, controller).preflight(
        SEPARATE_CHECKSUM, run_id="new-run", scenario_id="preflight"
    )

    assert controller.calls == [("separate", SEPARATE_CHECKSUM)]
    assert controller.state_during_reconcile["run_id"] == "run-1"
    assert not (tmp_path / STATE_FILENAME).exists()


def test_enter_shared_persists_intent_before_rollout(tmp_path):
    """A crash during rollout leaves enough state for the next preflight."""
    controller = _Controller(tmp_path)

    observed = _manager(tmp_path, controller).enter_shared(
        run_id="run-3",
        scenario_id="ssh-shared-home",
        separate_checksum=SEPARATE_CHECKSUM,
        shared_checksum=SHARED_CHECKSUM,
    )

    assert observed.matches("shared", SHARED_CHECKSUM)
    assert controller.calls == [("shared", SHARED_CHECKSUM)]
    assert controller.state_during_reconcile["phase"] == "applying-shared"
    retained = TransitionStore(tmp_path).load()
    assert retained is not None
    assert retained.phase == "shared-ready"
    assert retained.run_id == "run-3"


def test_restore_clears_state_only_after_canonical_validation(tmp_path):
    """The transition record remains present while restoration runs."""
    TransitionStore(tmp_path).save(_state(phase="shared-ready"))
    controller = _Controller(tmp_path, _observation("shared", SHARED_CHECKSUM))

    observed = _manager(tmp_path, controller).restore_separate(
        SEPARATE_CHECKSUM, run_id="run-3", scenario_id="ssh-shared-home"
    )

    assert observed.matches("separate", SEPARATE_CHECKSUM)
    assert controller.state_during_reconcile["phase"] == "restoring-separate"
    assert not (tmp_path / STATE_FILENAME).exists()


def test_failed_reconciliation_persists_actionable_unhealthy_state(tmp_path):
    """A rollout failure blocks SSH work and survives another invocation."""
    controller = _Controller(tmp_path, _observation("shared", SHARED_CHECKSUM))
    controller.fail = RuntimeError("rollout timed out")

    with pytest.raises(SshHomeTransitionError, match="further SSH scenarios"):
        _manager(tmp_path, controller).preflight(
            SEPARATE_CHECKSUM, run_id="run-4", scenario_id="preflight"
        )

    retained = TransitionStore(tmp_path).load()
    assert retained is not None
    assert retained.phase == "unhealthy"
    assert retained.prior_mode == "shared"
    assert retained.requested_mode == "separate"
    assert "rollout timed out" in retained.failure_diagnostics


def test_later_preflight_retries_unhealthy_reconciliation(tmp_path):
    """A later setup can recover an SSH fixture marked unhealthy."""
    TransitionStore(tmp_path).save(
        _state(
            phase="unhealthy",
            requested_mode="separate",
            checksum=SEPARATE_CHECKSUM,
        )
    )
    controller = _Controller(tmp_path)

    _manager(tmp_path, controller).preflight(
        SEPARATE_CHECKSUM, run_id="run-5", scenario_id="preflight"
    )

    assert controller.calls == [("separate", SEPARATE_CHECKSUM)]
    assert not (tmp_path / STATE_FILENAME).exists()


def test_inspection_failure_retains_canonical_recovery_target(tmp_path):
    """A failed preflight still records that separate mode must be restored."""

    def failed_inspection():
        raise RuntimeError("Kubernetes API unavailable")

    manager = SshHomeTransitionManager(
        tmp_path,
        failed_inspection,
        lambda _mode, _checksum: None,
        clock=lambda: FIXED_TIME,
    )

    with pytest.raises(SshHomeTransitionError, match="blocked"):
        manager.preflight(SEPARATE_CHECKSUM, run_id="run-6", scenario_id="preflight")

    retained = TransitionStore(tmp_path).load()
    assert retained is not None
    assert retained.phase == "unhealthy"
    assert retained.prior_mode == "unknown"
    assert retained.requested_mode == "separate"
    assert retained.expected_statefulset_checksum == SEPARATE_CHECKSUM
    assert "Kubernetes API unavailable" in retained.failure_diagnostics


def test_post_reconcile_observation_must_be_ready_and_nonterminating(tmp_path):
    """A callback cannot claim success while rollout overlap remains."""
    controller = _Controller(tmp_path, _observation("shared", SHARED_CHECKSUM))

    def incomplete_reconcile(_mode, _checksum):
        controller.observed = _observation(
            "separate", SEPARATE_CHECKSUM, ready=2, terminating=1
        )

    manager = SshHomeTransitionManager(
        tmp_path,
        controller.inspect,
        incomplete_reconcile,
        clock=lambda: FIXED_TIME,
    )

    with pytest.raises(SshHomeTransitionError, match="blocked"):
        manager.preflight(SEPARATE_CHECKSUM, run_id="run-6", scenario_id="preflight")

    retained = TransitionStore(tmp_path).load()
    assert retained is not None
    assert retained.phase == "unhealthy"
    assert "terminating=1" in retained.failure_diagnostics


def test_invalid_state_is_not_silently_replaced(tmp_path):
    """Malformed ownership state requires diagnosis instead of blind cleanup."""
    (tmp_path / STATE_FILENAME).write_text('{"schema": 99}\n', encoding="utf-8")
    controller = _Controller(tmp_path)

    with pytest.raises(SshHomeTransitionError, match="invalid SSH home transition"):
        _manager(tmp_path, controller).preflight(
            SEPARATE_CHECKSUM, run_id="run-7", scenario_id="preflight"
        )

    assert controller.calls == []


def test_state_store_requires_existing_state_directory(tmp_path):
    """Transition code never creates or adopts lifecycle state directories."""
    absent = tmp_path / "absent"

    with pytest.raises(SshHomeTransitionError, match="state directory is absent"):
        TransitionStore(absent).save(_state())


def test_statefulset_checksum_uses_rendered_bytes():
    """Configuration identities change with the rendered StatefulSet form."""
    assert statefulset_checksum("emptyDir: {}") == statefulset_checksum(b"emptyDir: {}")
    assert statefulset_checksum("emptyDir: {}") != statefulset_checksum(
        "persistentVolumeClaim: {}"
    )
