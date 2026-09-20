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

"""Crash-recoverable state for temporary SSH shared-home transitions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

CANONICAL_HOME_MODE = "separate"
SHARED_HOME_MODE = "shared"
UNKNOWN_HOME_MODE = "unknown"
STATE_FILENAME = "ssh-home-transition.json"
STATE_SCHEMA = 1
EXPECTED_POD_COUNT = 2
MAX_FAILURE_DIAGNOSTICS = 8000

HomeMode = Literal["separate", "shared"]
PoolMode = Literal["separate", "shared", "unknown"]
InspectPool = Callable[[], "PoolObservation"]
ReconcilePool = Callable[[HomeMode, str], None]
Clock = Callable[[], str]

_PHASES = {
    "applying-shared",
    "shared-ready",
    "restoring-separate",
    "unhealthy",
}
_HOME_MODES = {CANONICAL_HOME_MODE, SHARED_HOME_MODE}
_POOL_MODES = {*_HOME_MODES, UNKNOWN_HOME_MODE}


class SshHomeTransitionError(RuntimeError):
    """An actionable SSH home-mode transition failure."""


@dataclass(frozen=True)
class PoolObservation:
    """Relevant live state of the SSH worker StatefulSet and its pods."""

    home_mode: str
    configuration_checksum: str
    ready_nonterminating_pods: int
    terminating_pods: int
    diagnostics: str = ""

    def matches(self, mode: HomeMode, checksum: str) -> bool:
        """Return whether the live pool is ready in the requested form."""
        return (
            self.home_mode == mode
            and self.configuration_checksum == checksum
            and self.ready_nonterminating_pods == EXPECTED_POD_COUNT
            and self.terminating_pods == 0
        )


@dataclass(frozen=True)
class TransitionState:
    """Persistent identity and progress for one SSH pool transition."""

    schema: int
    run_id: str
    scenario_id: str
    prior_mode: PoolMode
    requested_mode: HomeMode
    expected_statefulset_checksum: str
    phase: str
    updated_at: str
    failure_diagnostics: str = ""

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "TransitionState":
        """Validate and decode a persistent transition document."""
        required = {
            "schema",
            "run_id",
            "scenario_id",
            "prior_mode",
            "requested_mode",
            "expected_statefulset_checksum",
            "phase",
            "updated_at",
            "failure_diagnostics",
        }
        if set(document) != required:
            raise SshHomeTransitionError(
                "invalid SSH home transition state fields: "
                f"expected {sorted(required)}, found {sorted(document)}"
            )
        state = cls(**document)
        state.validate()
        return state

    def validate(self) -> None:
        """Reject a state document that cannot safely drive reconciliation."""
        if self.schema != STATE_SCHEMA:
            raise SshHomeTransitionError(
                f"unsupported SSH home transition schema: {self.schema!r}"
            )
        if self.prior_mode not in _POOL_MODES or self.requested_mode not in _HOME_MODES:
            raise SshHomeTransitionError("invalid SSH home mode in transition state")
        if self.phase not in _PHASES:
            raise SshHomeTransitionError(
                f"invalid SSH home transition phase: {self.phase!r}"
            )
        text_fields = {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "expected_statefulset_checksum": self.expected_statefulset_checksum,
            "updated_at": self.updated_at,
        }
        empty = [name for name, value in text_fields.items() if not _valid_text(value)]
        if empty:
            raise SshHomeTransitionError(
                "empty or invalid SSH home transition fields: " + ", ".join(empty)
            )


class TransitionStore:
    """Atomically persist the SSH transition record in an existing state dir."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.path = state_dir / STATE_FILENAME

    def load(self) -> TransitionState | None:
        """Return validated transition state, if present."""
        if not self.path.exists():
            return None
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SshHomeTransitionError(
                f"cannot read SSH home transition state {self.path}: {error}"
            ) from error
        if not isinstance(document, dict):
            raise SshHomeTransitionError(
                f"invalid SSH home transition state in {self.path}: expected object"
            )
        try:
            return TransitionState.from_document(document)
        except (TypeError, SshHomeTransitionError) as error:
            raise SshHomeTransitionError(
                f"invalid SSH home transition state in {self.path}: {error}"
            ) from error

    def save(self, state: TransitionState) -> None:
        """Atomically replace the transition document."""
        state.validate()
        self._require_state_dir()
        payload = json.dumps(asdict(state), indent=2, sort_keys=True) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.state_dir, prefix=f".{STATE_FILENAME}."
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            _sync_directory(self.state_dir)
        finally:
            temporary.unlink(missing_ok=True)

    def clear(self) -> None:
        """Remove a completed transition record durably."""
        if not self.path.exists():
            return
        self.path.unlink()
        _sync_directory(self.state_dir)

    def _require_state_dir(self) -> None:
        """Refuse to create or adopt lifecycle state implicitly."""
        if not self.state_dir.is_dir():
            raise SshHomeTransitionError(
                f"integration state directory is absent: {self.state_dir}"
            )


class SshHomeTransitionManager:
    """Drive and recover SSH home transitions through injected live hooks.

    ``reconcile_pool`` must apply the requested StatefulSet form, wait for two
    ready nonterminating pods, regenerate known-host data, and validate SSH and
    home semantics. Callers remain responsible for serializing fixture actions.
    """

    def __init__(
        self,
        state_dir: Path,
        inspect_pool: InspectPool,
        reconcile_pool: ReconcilePool,
        *,
        clock: Clock | None = None,
    ):
        self.store = TransitionStore(state_dir)
        self.inspect_pool = inspect_pool
        self.reconcile_pool = reconcile_pool
        self.clock = clock or _utc_now

    def preflight(
        self,
        separate_checksum: str,
        *,
        run_id: str,
        scenario_id: str,
    ) -> PoolObservation:
        """Ensure canonical separate-home state before an SSH setup or test."""
        retained = self.store.load()
        try:
            observed = self.inspect_pool()
        except Exception as error:
            self._save_unhealthy(
                retained,
                run_id=run_id,
                scenario_id=scenario_id,
                prior_mode=UNKNOWN_HOME_MODE,
                requested_mode=CANONICAL_HOME_MODE,
                checksum=separate_checksum,
                error=error,
            )
            raise self._blocked_error(error) from error
        if retained is None and observed.matches(
            CANONICAL_HOME_MODE, separate_checksum
        ):
            return observed
        identity = retained or _new_state(
            run_id,
            scenario_id,
            _known_mode(observed.home_mode),
            CANONICAL_HOME_MODE,
            separate_checksum,
            "restoring-separate",
            self.clock(),
        )
        return self._reconcile_separate(identity, separate_checksum)

    def enter_shared(
        self,
        *,
        run_id: str,
        scenario_id: str,
        separate_checksum: str,
        shared_checksum: str,
    ) -> PoolObservation:
        """Enter shared-home mode after first recovering canonical state."""
        self.preflight(separate_checksum, run_id=run_id, scenario_id=scenario_id)
        state = _new_state(
            run_id,
            scenario_id,
            CANONICAL_HOME_MODE,
            SHARED_HOME_MODE,
            shared_checksum,
            "applying-shared",
            self.clock(),
        )
        self.store.save(state)
        try:
            self.reconcile_pool(SHARED_HOME_MODE, shared_checksum)
            observed = self.inspect_pool()
            _require_observation(observed, SHARED_HOME_MODE, shared_checksum)
        except Exception as error:
            self._save_unhealthy_from_state(state, error)
            raise self._blocked_error(error) from error
        self.store.save(replace(state, phase="shared-ready", updated_at=self.clock()))
        return observed

    def restore_separate(
        self,
        separate_checksum: str,
        *,
        run_id: str,
        scenario_id: str,
    ) -> PoolObservation:
        """Restore canonical state after the complete shared-home batch."""
        retained = self.store.load()
        identity = retained or _new_state(
            run_id,
            scenario_id,
            SHARED_HOME_MODE,
            CANONICAL_HOME_MODE,
            separate_checksum,
            "restoring-separate",
            self.clock(),
        )
        return self._reconcile_separate(identity, separate_checksum)

    def _reconcile_separate(
        self, identity: TransitionState, checksum: str
    ) -> PoolObservation:
        state = replace(
            identity,
            requested_mode=CANONICAL_HOME_MODE,
            expected_statefulset_checksum=checksum,
            phase="restoring-separate",
            updated_at=self.clock(),
            failure_diagnostics="",
        )
        self.store.save(state)
        try:
            self.reconcile_pool(CANONICAL_HOME_MODE, checksum)
            observed = self.inspect_pool()
            _require_observation(observed, CANONICAL_HOME_MODE, checksum)
        except Exception as error:
            self._save_unhealthy_from_state(state, error)
            raise self._blocked_error(error) from error
        self.store.clear()
        return observed

    def _save_unhealthy_from_state(
        self, state: TransitionState, error: Exception
    ) -> None:
        self.store.save(
            replace(
                state,
                phase="unhealthy",
                updated_at=self.clock(),
                failure_diagnostics=_failure_text(error),
            )
        )

    def _save_unhealthy(
        self,
        retained: TransitionState | None,
        *,
        run_id: str,
        scenario_id: str,
        prior_mode: PoolMode,
        requested_mode: HomeMode,
        checksum: str,
        error: Exception,
    ) -> None:
        state = (
            replace(
                retained,
                requested_mode=requested_mode,
                expected_statefulset_checksum=checksum,
            )
            if retained
            else _new_state(
                run_id,
                scenario_id,
                prior_mode,
                requested_mode,
                checksum,
                "unhealthy",
                self.clock(),
            )
        )
        self._save_unhealthy_from_state(state, error)

    def _blocked_error(self, error: Exception) -> SshHomeTransitionError:
        return SshHomeTransitionError(
            "SSH worker pool reconciliation failed; further SSH scenarios are "
            f"blocked. Retry setup to reconcile separate-home mode or teardown "
            f"the fixture. State and diagnostics: {self.store.path}: {error}"
        )


def statefulset_checksum(manifest: str | bytes) -> str:
    """Return a stable digest for one rendered StatefulSet configuration."""
    data = manifest.encode("utf-8") if isinstance(manifest, str) else manifest
    return hashlib.sha256(data).hexdigest()


def _new_state(
    run_id: str,
    scenario_id: str,
    prior_mode: PoolMode,
    requested_mode: HomeMode,
    checksum: str,
    phase: str,
    updated_at: str,
) -> TransitionState:
    """Build and validate one transition state value."""
    state = TransitionState(
        schema=STATE_SCHEMA,
        run_id=run_id,
        scenario_id=scenario_id,
        prior_mode=prior_mode,
        requested_mode=requested_mode,
        expected_statefulset_checksum=checksum,
        phase=phase,
        updated_at=updated_at,
    )
    state.validate()
    return state


def _known_mode(value: str) -> PoolMode:
    """Use a safe prior mode when live inspection finds a partial rollout."""
    if value == CANONICAL_HOME_MODE:
        return CANONICAL_HOME_MODE
    if value == SHARED_HOME_MODE:
        return SHARED_HOME_MODE
    return UNKNOWN_HOME_MODE


def _require_observation(
    observed: PoolObservation, mode: HomeMode, checksum: str
) -> None:
    """Require reconciliation to produce the exact healthy target form."""
    if observed.matches(mode, checksum):
        return
    detail = f"; {observed.diagnostics}" if observed.diagnostics else ""
    raise SshHomeTransitionError(
        "SSH worker pool did not reach the requested state: "
        f"mode={observed.home_mode!r}, checksum="
        f"{observed.configuration_checksum!r}, ready="
        f"{observed.ready_nonterminating_pods}, terminating="
        f"{observed.terminating_pods}{detail}"
    )


def _failure_text(error: Exception) -> str:
    """Return bounded persistent diagnostics for a reconciliation failure."""
    text = f"{type(error).__name__}: {error}".strip()
    return text[-MAX_FAILURE_DIAGNOSTICS:]


def _valid_text(value: object) -> bool:
    """Return whether a required document field is a nonempty string."""
    return isinstance(value, str) and bool(value.strip())


def _utc_now() -> str:
    """Return an ISO-8601 timestamp for persistent diagnostics."""
    return datetime.now(timezone.utc).isoformat()


def _sync_directory(directory: Path) -> None:
    """Best-effort fsync of a directory after atomic state mutation."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
