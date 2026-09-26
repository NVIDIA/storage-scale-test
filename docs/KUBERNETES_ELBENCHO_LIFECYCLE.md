<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# Kubernetes Elbencho Lifecycle and Fault Contract

This document is the normative lifecycle and failure contract for the
asynchronous kubectl substrate of the Elbencho filesystem sweep. It freezes
the states, legal transitions, invariants, linearization points, supported
fault boundaries, and explicit non-goals against which implementation and
review are evaluated. The implementation plan explains how the feature was
built; this document defines the behavior that must remain true.

## Failure policy and boundary

Failures have three support classes:

| Class | Expected frequency | Required behavior |
|---|---|---|
| Required recovery | Medium or high | Reconcile or retry automatically within a bounded deadline. If that fails, retain recoverable state and return actionable diagnostics. |
| Required diagnosis | Low | Fail closed, preserve authoritative evidence, identify the failed invariant or operation, and give the next safe action. Bespoke automatic recovery is not required. |
| Unsupported disaster or corruption | Very low or outside the trust model | Never claim success or delete ambiguously owned resources. Detect and report the inconsistency when possible, but make no recovery promise. |

Interruption safety applies immediately before and after the external-effect
boundaries listed under [Linearization points](#linearization-points). It does
not promise recovery from a kill between every pair of shell statements.
`TERM`, `INT`, command failures, and API failures are handled in the current
process. `SIGKILL`, Pod eviction, node loss, and host loss are reconciled by a
later lifecycle command.

An error never becomes a durable `ERROR`, `UNKNOWN`, or `AMBIGUOUS` lifecycle
state. Those are observations, not proven attempt facts. The command retains
the last proven state, exits nonzero, and reports the uncertainty.

## State machines

The lifecycle comprises three authoritative state machines. Kubernetes Job
state is corroborating evidence, not a fourth source of lifecycle truth.

### Local attempt lifecycle

The local metadata controls which CLI operations may mutate an attempt:

```text
new -> PREPARED -> SUBMITTED -> TERMINAL -> COLLECTION_IN_PROGRESS -> COLLECTED
          |             |
          |             `-> CANCEL_REQUESTED -> TERMINAL
          `-> SUBMISSION_FAILED
```

The complete legal transition set is:

```text
new -> PREPARED
PREPARED -> SUBMITTED
PREPARED -> SUBMISSION_FAILED
SUBMITTED -> CANCEL_REQUESTED
SUBMITTED -> TERMINAL
CANCEL_REQUESTED -> TERMINAL
TERMINAL -> COLLECTION_IN_PROGRESS
COLLECTION_IN_PROGRESS -> COLLECTED
```

No transition leaves `COLLECTED` or `SUBMISSION_FAILED`. Resume creates a new
attempt; it never reopens its predecessor. Local `TERMINAL` means that the
remote outcome is durably terminal and measured I/O has ended. The exact Job
must additionally be terminal or verified absent before collection may
publish or remove remote state.

### PVC run lifecycle

The PVC is authoritative for benchmark progress until collection:

```text
PREPARED -> RUNNING -> SUCCESS
    |          |
    |          `----> FAILED
    `---------------> CANCELLED
```

`PREPARED` may also become `FAILED`; `RUNNING` may become `CANCELLED`.
`SUCCESS`, `FAILED`, and `CANCELLED` are immutable terminal outcomes. Recovery
may reconstruct a missing terminal summary or publication manifest from a
consistent ledger, but may not change one terminal outcome into another.

### Per-cell lifecycle

Each reified execution has this lifecycle:

```text
PENDING -> RUNNING -> SUCCESS
                  `-> FAILED
```

A cell never returns to `PENDING` within an attempt. Coordinator loss while a
cell is `RUNNING` turns it into a collectible failure; later cells remain
`PENDING`. Resume creates another attempt containing the cells that did not
succeed. Collected successful cells and their results remain immutable.

### Kubernetes Job evidence

The exact Job may be observed as:

```text
NOT_CREATED, CREATE_AMBIGUOUS, ACTIVE, COMPLETE, FAILED, or DELETED
```

Only the saved kind, name, namespace, UID, ownership nonce, and attempt labels
establish identity. A broad label query never establishes ownership. A failed
or externally deleted exact Job with a nonterminal PVC ledger is bounded
coordinator-loss evidence: a lifecycle command may recover the run to a
collectible failure when the saved identity and durable control state agree.

### Public status projection

| Local state | Public status behavior |
|---|---|
| `PREPARED` | Report `PREPARED`, or reconcile a provably failed submission to `SUBMISSION_FAILED`. |
| `SUBMISSION_FAILED` | Report `SUBMISSION_FAILED`. |
| `SUBMITTED` | Report the validated PVC state: `PREPARED`, `RUNNING`, `SUCCESS`, `FAILED`, or `CANCELLED`. |
| `CANCEL_REQUESTED` | Report pending cancellation and direct the user to retry `--cancel`. |
| `TERMINAL` | Report the validated terminal PVC outcome. |
| `COLLECTION_IN_PROGRESS` | Report the terminal outcome and resumable collection context. |
| `COLLECTED` | Report the locally verified terminal outcome without cluster access. |

Identity, authentication, query, and consistency failures return nonzero
instead of inventing another state value.

## Invariants

1. Attempt ID, ownership nonce, namespace/PV/PVC identity, configuration,
   image, workload identity, and execution definitions are immutable.
2. One attempt is current per result tree. The current pointer is atomic;
   historical attempt metadata remains immutable.
3. One mutating lifecycle command may operate on a result tree. Concurrent
   commands are rejected rather than queued.
4. One active sweep may own the configured PVC. Its global reservation is
   never stolen.
5. Every external create follows `durable intent -> create -> exact identity
   observation -> durable UID journal -> intent removal`.
6. Destructive operations require exact kind, name, namespace, UID, ownership
   nonce, and attempt identity. Operational lookup failure is not absence.
7. `PREPARED` may contain partial resources, but every possible resource is
   represented by a creation intent or exact resource journal.
8. `SUBMITTED` has an exact UID-journaled sweep Job.
9. The PVC coordinator lock gives one Job Pod authority to mutate the ledger;
   duplicate Job Pods exit first.
10. Node UID, architecture, resolved image identity, and Pod IPv4 address are
    frozen. A worker Pod name or UID may change only when the same Node and IP
    remain healthy; all other drift fails rather than retargeting the attempt.
11. A cell commits only when its artifacts, exit code, terminal cell state,
    summary, and publication manifest agree.
12. Scratch-to-PVC result publication never overlaps measured benchmark I/O.
13. A terminal run is collectible only when its ledger, summary, publication
    manifest, and exact Job quiescence agree.
14. Remote cleanup begins only after the complete local publication verifies.
15. `COLLECTED` means the local publication verifies, the remote run and PVC
    lock are gone, exact owned resources are gone, and no benchmark work remains.
16. Resume never reruns a collected successful cell.
17. Traversing, linked, contradictory, corrupt, or identity-mismatched state
    fails closed.
18. Supplemental diagnostic failure does not block safe cleanup or replace the
    primary error.

## Linearization points

| Operation | Linearization point |
|---|---|
| Attempt identity exists | Atomic rename of the complete attempt metadata directory. |
| Attempt becomes current and recoverable | Atomic replacement of `current-attempt` before external mutation. |
| Local transition | Atomic replacement of `state.sh`. |
| PVC ownership | Same-filesystem rename of an initialized pending lock directory to the canonical lock. |
| A Kubernetes resource may exist | Creation intent is durable before `kubectl create`. |
| Kubernetes ownership is locally established | Exact UID-bearing resource journal is atomically published. |
| Submission succeeds | Exact Job UID is journaled and local state becomes `SUBMITTED`. |
| Coordinator ownership | Atomic creation of `coordinator.lock`; owner-file completion is recoverable initialization. |
| Run becomes active | Atomic `run.status=RUNNING` after bundle, snapshots, ledger, and service validation. |
| Cell begins | Atomic `NNNN.status=RUNNING`. |
| Cell commits | Atomic replacement of the publication manifest after artifacts, exit code, terminal cell status, and summary. |
| Run commits its outcome | Terminal summary and status plus a consistent publication manifest. |
| Cancellation begins | `SUBMITTED -> CANCEL_REQUESTED` before exact Job deletion. |
| Collection publication begins | `TERMINAL -> COLLECTION_IN_PROGRESS` after bounded archive receipt and before extraction or result publication. The unpublished archive may exist while local state remains `TERMINAL`. |
| Local import checkpoint | Verified state is atomically installed as `collected-state` after result merging. |
| Collection completes | Remote and resource cleanup completes, then local state becomes `COLLECTED`. |
| Resume succeeds | A new attempt becomes current under compare-and-swap against the collected predecessor. |

## Fault matrix

`Covered` means implementation and focused regression evidence exist.
`Acceptance pending` means local and kind coverage exists but the named
external environment must still be exercised. A future change that weakens a
covered row is a regression. Faults outside this matrix require an explicit
contract change rather than silently expanding the release boundary.

### Submission

| ID | Fault boundary | Class | Required result | Status |
|---|---|---|---|---|
| S-01 | Invalid configuration, path, PVC, selector, identity, or image | Required recovery | Reject before mutation and identify the field. | Covered |
| S-02 | Exit before unpublished identity installation | Required recovery | Ignore or remove opaque staging; no current attempt exists. | Covered |
| S-03 | Exit after current `PREPARED` publication | Required recovery | Later lifecycle command rolls forward or records `SUBMISSION_FAILED`. | Covered |
| S-04 | Concurrent command for one result tree | Required recovery | One lock owner; loser exits without mutation. | Covered |
| S-05 | Competing result trees reserve one PVC | Required recovery | One owner; loser rolls back without touching it. | Covered |
| S-06 | Exit around pending PVC-lock publication | Required recovery | Reconcile exact owner/intent; never steal the lock. | Covered |
| S-07 | API accepts create but response is lost | Required recovery | Resolve deterministic identity, then journal or delete the exact object. | Covered |
| S-08 | API visibility is delayed within the documented ambiguity horizon | Required recovery | Wait and perform a later linearizable GET. | Covered |
| S-09 | Object appears after the ambiguity horizon | Required diagnosis | Retain possible identity and print exact inspection/cleanup guidance. | Covered diagnostically; automatic recovery unsupported |
| S-10 | API 429, 5xx, timeout, or disconnect during observation | Required recovery | Bounded retry with jitter, then safe retry guidance. | Covered |
| S-11 | Credentials expire or network fails during submission | Required recovery | Preserve recoverable state; roll back only from exact evidence. | Covered locally; external acceptance pending |
| S-12 | Transfer Pod, policy, or worker cannot become Ready | Required recovery | Bounded rollback plus descriptions, logs, and events. | Covered |
| S-13 | Bundle upload is interrupted | Required recovery | Do not start the Job; remove the exact partial run. | Covered |
| S-14 | Exit after Job acceptance but before `SUBMITTED` | Required recovery | Quiesce exact Job and workers before releasing remote state. | Covered |
| S-15 | Submitter exits after `SUBMITTED` but before printing commands | Required diagnosis | Retain discoverable state under the supplied result directory. | Covered; lost stdout is accepted |
| S-16 | Original Teleport credential expires after handoff | Required recovery | Job continues; later client reports authentication and safely retries after login. | Acceptance pending |

### Coordinator and benchmark

| ID | Fault boundary | Class | Required result | Status |
|---|---|---|---|---|
| C-01 | Kubernetes starts a duplicate Job Pod | Required recovery | One PVC lock owner; duplicate exits without ledger mutation. | Covered |
| C-02 | Coordinator exits before or during startup initialization | Required recovery | Exact Job evidence converts the attempt into collectible failure while unstarted cells remain `PENDING`. | Covered |
| C-03 | Service or policy convergence is delayed | Required recovery | Retry within a fixed deadline, then publish startup failure and diagnostics. | Covered |
| C-04 | Benchmark cell exits nonzero | Required recovery | Preserve exit/evidence, stop later cells, permit collect and resume. | Covered |
| C-05 | Coordinator receives `TERM` or `INT` | Required recovery | Best-effort terminal publication; later command reconciles hard loss. | Covered |
| C-06 | Coordinator is killed or evicted during a cell | Required recovery | Lose only active scratch; preserve committed cells and recover interrupted cell. | Covered |
| C-07 | Exit during artifact, cell-state, summary, or manifest publication | Required recovery | Manifest identifies committed evidence; recovery repairs bounded gaps. | Covered |
| C-08 | Exit after terminal status but before terminal manifest | Required recovery | Repair only from a consistent durable ledger. | Covered |
| C-09 | Worker service dies during measured I/O | Required recovery | Fail the current cell without transparent benchmark retry. | Covered |
| C-10 | Worker container restart or same-Node/same-IP Pod replacement | Required recovery | Continue only after frozen semantic identity and health validate. | Covered |
| C-11 | Worker IP, Node UID, architecture, or image changes | Required recovery | Fail, collect, and resume under a new attempt. | Covered |
| C-12 | Worker DaemonSet is externally deleted | Required diagnosis | Refuse silent recreation; retain state and identify the missing exact object. | Covered |
| C-13 | PVC operation is temporarily unavailable | Required diagnosis | Fail safely, retain resources, and identify the PVC operation. | Covered |
| C-14 | PVC fills during benchmark or control publication | Required diagnosis | Report ENOSPC with exact Job/PVC evidence and the affected phase. Exact kernel-level attribution to workload versus ledger I/O is not guaranteed. | Covered |
| C-15 | PVC is permanently lost or corrupt | Unsupported | Fail closed without reconstruction. | Covered detection; automatic repair unsupported |

### Status and cancellation

| ID | Fault boundary | Class | Required result | Status |
|---|---|---|---|---|
| T-01 | Transient status API failure | Required recovery | Bounded retry; do not mutate without exact evidence. | Covered |
| T-02 | Client credentials expired | Required recovery | Report authentication and direct the user to reauthenticate and retry. | Covered locally; external acceptance pending |
| T-03 | PVC becomes terminal before Job controller convergence | Required recovery | Report terminal PVC state; collection still waits for exact quiescence. | Covered |
| T-04 | Exact Job fails or disappears while PVC is nonterminal | Required recovery | Recover coordinator loss from verified control and PVC state. | Covered |
| T-05 | Job says Complete but ledger is nonterminal | Required diagnosis | Refuse success and report both observations. | Covered |
| T-06 | User externally deletes exact Job | Required recovery | Treat as bounded coordinator loss when saved identity and PVC evidence agree. | Covered |
| T-07 | Cancel active work | Required recovery | Journal intent, delete exact Job, publish cancellation, retain collection data. | Covered |
| T-08 | Client exits during cancellation | Required recovery | Repeated `--cancel` resumes exact deletion and finalization. | Covered |
| T-09 | Status observes `CANCEL_REQUESTED` | Required recovery | Tell the user to retry `--cancel`; never report ordinary running state. | Covered |
| T-10 | Same resource name has another UID or nonce | Required diagnosis | Refuse deletion and report expected and observed identity. | Covered |

### Collection and resume

| ID | Fault boundary | Class | Required result | Status |
|---|---|---|---|---|
| R-01 | Collect is requested while exact Job is active | Required recovery | Refuse without publication or cleanup and print the next action. | Covered |
| R-02 | Collector cannot schedule or mount PVC | Required recovery | Preserve remote state, capture diagnostics, and permit retry. | Covered |
| R-03 | Authentication or network fails during archive stream | Required recovery | Remove partial local archive, retain remote source, and permit retry. | Covered |
| R-04 | Transfer exceeds collection deadline | Required recovery | Stop without remote cleanup and permit retry. | Covered |
| R-05 | Stream exceeds archive byte limit | Required diagnosis | Stop while receiving, delete partial local data, and preserve remote source. | Covered |
| R-06 | Received archive exceeds member limit | Required diagnosis | Stop while streaming archive metadata before extraction and preserve remote source. | Covered |
| R-07 | Archive has traversal, links, devices, or another attempt | Required diagnosis | Reject before extraction. | Covered |
| R-08 | Manifest is incomplete, conflicting, or cross-cell | Required diagnosis | Refuse publication and identify the row, cell, and path. | Covered |
| R-09 | Local receive, extraction, or merge hits ENOSPC | Required recovery | Preserve remote data, identify the local path, and permit retry after remediation. | Covered |
| R-10 | Client exits before local merge | Required recovery | Keep remote state authoritative and discard stale staging on retry. | Covered |
| R-11 | Client exits during per-file merge | Required recovery | Remove attempt-named incomplete files, accept only identical published digests, and continue. | Covered |
| R-12 | Client exits after `collected-state` during cleanup | Required recovery | Resume journaled cleanup without retransferring. | Covered |
| R-13 | Exact cleanup call fails transiently | Required recovery | Remain `COLLECTION_IN_PROGRESS`, report retained identities, and retry. | Covered |
| R-14 | Client exits after cleanup but before `COLLECTED` | Required recovery | Status reports the publication locally; collect finishes idempotent cleanup and marks collected. | Covered |
| R-15 | Collect is repeated after `COLLECTED` | Required recovery | Validate local publication without cluster credentials. | Covered |
| R-16 | Resume is requested before collection | Required recovery | Reject and print the required collect command. | Covered |
| R-17 | Concurrent resumes | Required recovery | At most one successor becomes current; reject the loser safely. | Covered |
| R-18 | Resume exits after publishing a new `PREPARED` attempt | Required recovery | Roll it back and restore the collected predecessor. | Covered |
| R-19 | Local attempt metadata or collected state is lost or corrupt | Unsupported | Refuse mutation; do not reconstruct ownership from labels. | Covered detection; automatic repair unsupported |
| R-20 | Cluster is replaced while PVC survives | Required diagnosis | Detect UID mismatch and refuse adoption or cleanup. | Covered |

### Regression evidence map

The matrix is enforced at the following layers. A `Covered` label requires an
assertion at the appropriate layer; merely reaching the branch is not evidence.

| Matrix IDs | Named primary evidence |
|---|---|
| S-01 | `test_runtime_configuration_rejects_every_invalid_field` |
| S-02, R-19 | `test_identity_is_complete_atomic_and_path_bound` |
| S-03, S-14 | `test_prepare_failure_terminalizes_only_after_successful_rollback` |
| S-04 | `test_s04_local_lifecycle_lock_rejects_a_concurrent_mutator` |
| S-05 | `test_intended_reservation_does_not_touch_competing_pvc_owner` |
| S-06 | `test_remote_lock_is_published_only_after_ownership` |
| S-07, S-08, S-09 | `test_creation_intent_cleans_object_left_before_resource_journal`, `test_creation_intent_rechecks_absence_after_create_deadline`, `test_creation_absence_retains_possible_late_object_identity`, `test_create_only_verifies_exact_identity_before_returning_uid` |
| S-10, T-01 | `test_observational_calls_retry_only_transient_api_failures` |
| S-11, T-02 | `test_exhausted_observation_emits_actionable_diagnostic_envelope` |
| S-12 | `test_worker_readiness_timeout_captures_daemonset_diagnostics` |
| S-13 | `test_interrupted_bundle_upload_never_starts_job_and_rolls_back` |
| S-15 | `test_submission_prints_status_cancel_and_collect_commands` |
| C-01 | `test_duplicate_coordinator_cannot_mutate_an_owned_attempt` |
| C-02 | `test_pre_running_crash_boundaries_recover_to_collectable_failure` |
| C-03 | `test_startup_endpoint_failure_publishes_a_collectible_terminal_ledger` |
| C-04 | `test_failure_overlay_preserves_first_failure_and_stops_later_cells` |
| C-05 | `test_signal_publishes_terminal_failure_without_erasing_scratch_evidence` |
| C-06 | `test_lost_coordinator_recovery_publishes_failed_running_cell` |
| C-07 | `test_crash_boundaries_never_advertise_uncommitted_terminal_cells` |
| C-08 | `test_lost_coordinator_repairs_terminal_status_manifest_window` |
| C-09 | `test_worker_service_failure_stops_current_cell_without_retry` |
| C-10 | `test_worker_endpoint_comparison_reports_replacement_and_ip_drift` |
| C-11 | `test_worker_endpoint_comparison_checks_all_frozen_identity_fields` |
| C-12, T-10 | `test_missing_worker_daemonset_emits_identity_diagnostics` |
| C-13 | `test_control_state_diagnostics_use_guarded_bounded_pvc_reads` |
| C-14 | `test_remote_pvc_enospc_has_distinct_diagnostics` |
| C-15 | `test_c15_permanent_pvc_loss_is_detected_without_adoption` |
| T-03 | `test_terminal_job_evidence_preserves_empty_active_field` |
| T-04, T-06 | `test_public_status_recovers_coordinator_loss_before_running` |
| T-05 | `test_completed_job_with_nonterminal_ledger_is_diagnosed` |
| T-07 | `test_cancel_recovery_publishes_collectable_terminal_ledger` |
| T-08 | `test_cancel_retries_after_job_delete_was_already_journaled` |
| T-09 | `test_status_reports_incomplete_cancellation_as_retryable_failure` |
| R-02 | `test_helper_template_is_rendered_and_removed_when_readiness_fails` |
| R-03, R-04, R-05 | `test_collection_stream_uses_its_operation_sized_deadline`, `test_collection_stream_failure_removes_partial_and_reports_auth`, `test_collection_byte_limit_is_enforced_while_streaming` |
| R-06, R-07, R-08 | `test_collection_archive_metadata_is_streamed_and_member_bounded`, `test_hostile_collection_archives_never_extract`, `test_collected_manifest_rejects_duplicate_semantic_rows`, `test_collected_manifest_requires_complete_terminal_evidence`, `test_collected_manifest_rejects_cross_execution_workload_substitution` |
| R-09, R-10, R-11 | `test_collection_capacity_checks_the_results_filesystem`, `test_collection_scavenges_only_owned_staging_paths`, `test_collection_merges_manifest_declared_execution_ledgers`, `test_collection_classifies_parent_creation_failure_as_local_io` |
| R-01 | `test_collect_active_attempt_reports_terminal_gate_and_next_action` |
| R-12, R-13 | `test_collection_recovery_retries_every_cleanup_stage` |
| R-14 | `test_status_projects_published_collection_after_remote_cleanup` |
| R-15 | `test_collected_terminal_status_is_a_successful_query` |
| R-16 | `test_resume_before_collection_prints_exact_collect_command` |
| R-17 | `test_stale_resume_contender_cannot_replace_successful_attempt` |
| R-18 | `test_clean_failed_resume_restores_collected_predecessor_pointer` |
| R-20 | `test_r20_cluster_uid_mismatch_emits_structured_diagnosis` |
| S-16 | The dated external acceptance record described below; still pending |

Real tests validate cross-component behavior without reproducing every fast
branch. A matrix row may say `Covered` only while a focused assertion exercises
its decision boundary; new rows must add named evidence. Unsupported rows need
detection tests where the condition can be created safely, not recovery tests.

## Diagnostic contract

Every failure at a supported matrix boundary must identify, when known:

```text
operation, phase, attempt ID, last proven local state,
last proven remote state, exact Job evidence, normalized reason,
resource kind/name/namespace, expected and observed UID,
local and remote state paths, whether work may still be running,
and the next safe action
```

Normalized reasons are `AUTH`, `TIMEOUT`, `API_THROTTLED`,
`API_UNAVAILABLE`, `IDENTITY_MISMATCH`, `POD_UNSCHEDULABLE`, `IMAGE_PULL`,
`PVC_MOUNT`, `PVC_IO`, `ENOSPC`, `LOCAL_IO`, `PATH_REJECTED`,
`INSUFFICIENT_CAPACITY`, `ARCHIVE_INVALID`,
`LEDGER_INCONSISTENT`, `OWNERSHIP_AMBIGUOUS`, and
`CANCELLATION_INCOMPLETE`. `INVALID_LIFECYCLE_OPERATION` identifies a valid
request made from a state in which that operation is not legal; its diagnostic
must print the exact prerequisite command or next lifecycle action.

For required-recovery faults, capture bounded Job, Pod, and DaemonSet
descriptions; relevant container logs and namespace events; durable PVC state;
the publication manifest; and exact retained resource identities. Store the
schema-versioned, bounded diagnostic bundle in the attempt's local Kubernetes
metadata, not in
the benchmark result/reporting contract. If diagnostic capture fails, retain
the primary error, continue safe cleanup, print copy-pasteable inspection
commands, and state whether data and resources were retained.

## Unsupported situations

The initial contract explicitly excludes:

- cooperative execution of concurrent mutating lifecycle commands;
- more than one active sweep on one PVC;
- automatic retry of a failed cell within the same attempt;
- transparent worker, endpoint, node, architecture, or image retargeting;
- automatic Kubernetes credential renewal or Teleport login;
- preservation of active-cell `emptyDir` scratch after hard Pod or node loss;
- automatic adoption after API-create visibility exceeds the ambiguity horizon;
- reconstruction after local attempt metadata loss or corruption;
- recovery after permanent PVC loss or corruption;
- recovery after namespace, PV, or PVC UID replacement;
- power-loss durability beyond the atomic-rename guarantees of the filesystems;
- malicious mutation of trusted local snapshots or PVC state;
- a compromised administrator, CNI, admission controller, service mesh, or
  storage driver that violates validated prerequisites;
- IPv6-only networking, heterogeneous selected architectures, set-based node
  selectors, and Kubernetes delete-only.

Unsupported does not mean silent. Detectable violations fail closed and name
the violated prerequisite or invariant.

## External credential-expiry acceptance

Run this gate on a representative Teleport-mediated cluster using its real RWX
claim. It cannot be replaced by kind or fake-`kubectl` coverage.

1. Record cluster, namespace, PV, and PVC UIDs and the active Teleport session
   expiry. Preserve the invocation, tool version, image digest, and node/CNI
   facts with the test evidence.
2. Submit `baseline`, wait for durable `RUNNING`, and let the original client
   credential expire without touching the Job.
3. Prove the Job continues and reaches a durable terminal state while the
   submitting shell can no longer query the API.
4. From the expired shell, run status and collection. Require `AUTH`, the saved
   attempt identity, `may_still_be_running=unknown`, and an exact
   reauthentication-and-retry action; neither command may mutate local or
   remote state.
5. Reauthenticate in a new shell, rerun status and collection, validate the
   ordinary result/report contract, and prove exact resource cleanup.
6. Repeat credential expiry during a running cancellation and during archive
   collection. Reauthenticate and prove that repeated `--cancel` and
   `--collect` finish the journaled operations without unrelated deletion or
   retransferring verified publication.
7. Run `failure-resume`, `kubectl-cancel`, and
   `kubectl-coordinator-loss`; require the same recovery semantics established
   by the real fixture.
8. Point the saved results directory at credentials for a different cluster
   exposing the same object names. Namespace/PV/PVC UID validation must reject
   it before mutation.
9. Confirm that no namespace, PV, PVC, unrelated workload, retained benchmark
   dataset, or unrelated NetworkPolicy was changed.

Record this gate by date and environment in the release or PR evidence. Do not
claim the external acceptance criterion from simulated token expiry alone.

## Closure criteria

Lifecycle hardening is complete when:

1. The three authoritative state machines and exact transitions above are
   normative.
2. Fast tests execute every legal local edge and reject every other state pair.
3. The fault boundaries in this matrix are frozen.
4. Every required-recovery row has named before/after fault evidence proving no false
   success, unrelated deletion, hidden active attempt, or unrecoverable retry
   state, and proving eventual completion or collectible failure.
5. Every required-diagnosis row verifies its normalized reason and next action.
6. Real fixtures cover baseline, benchmark failure/resume, cancel, coordinator
   loss, endpoint drift, and interrupted collection.
7. External acceptance covers real credential expiry and the representative
   CNI/RWX environment.
8. No open defect violates an invariant or supported matrix row.
9. One final adversarial review is performed against this frozen contract.

Findings outside this contract become backlog or explicit design proposals;
they do not silently extend the release boundary.

As of 2026-09-26, repository-controlled criteria 1–6 and 8 have focused test
evidence, and the complete Docker SBX lifecycle passes every SSH, Slurm, and
kubectl scenario. Criterion 7 requires the real Teleport-mediated CNI/RWX
environment. Criterion 9 is the final review after that external gate.
