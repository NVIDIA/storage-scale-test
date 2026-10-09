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

# Prepared batches for Elbencho filesystem sweeps

Implemented design record, not an outstanding implementation plan. There were
no scope deviations. Current interfaces are documented in the
[filesystem guide](../FILESYSTEM_TESTING.md#prepared-filesystem-batches) and
launcher help. The [design](../DESIGN.md#45-prepared-filesystem-batches) describes
the durable layout; the [Kubernetes lifecycle contract](../KUBERNETES_ELBENCHO_LIFECYCLE.md)
remains authoritative for attempt recovery.

## User workflow

Users assemble IO and metadata sweeps in one results directory before starting
work. Creation and append use the current configuration; execution uses saved
configuration. Either filesystem launcher can start or resume the mixed batch.

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --batch --nodes 1,2
# Save the printed STORAGE_SCALE_TEST_BATCH_RESULTS as batch_dir.

# Edit env.sh for the next workload, then append it.
./storage-tests/fs/nv-mdtest-elbencho.sh \
    --append "$batch_dir" --nodes 1,2 --tasks 4,8

./storage-tests/fs/nv-mdtest-elbencho.sh --start "$batch_dir"
./storage-tests/fs/nv-elbencho-sweep.sh --status "$batch_dir"
# For Kubernetes, collect the terminal attempt before resuming.
./storage-tests/fs/nv-elbencho-sweep.sh --resume "$batch_dir"
./utils/extract-filesystem.py "$batch_dir"
```

Kubernetes remains asynchronous and requires terminal collection before resume.
Preparation creates no remote resources. Once sealed, the execution set stays
immutable even if submission fails; append never becomes available again.

## Decisions and rationale

### Prepare first, then seal

The original live-append proposal was replaced with a preparation-only queue.
This avoids allocation rollover, remote queue synchronization, and races between
an append and a coordinator deciding that its work is complete.

Atomic manifest replacement commits each group. Unreferenced staging and
definitions cannot become runnable work. Start rechecks the manifest revision
after non-mutating preflight, then seals it before external mutation. Preparation
and sealing reuse dispatch-lock ownership and stale-owner checks rather than
introducing another recovery policy.

### One environment, multiple workloads

Execution identity and substrate settings are frozen at creation. Workload
parameters, roots, and weights belong to immutable group snapshots. This keeps
resource acquisition coherent while allowing meaningful workload comparisons.
Saved list-file contents prevent later edits from changing the worker pool.
Credentials remain external to the batch.

One global ledger preserves insertion order and stable execution IDs. Group
output directories isolate repeated coordinates, retries, completion evidence,
and configuration provenance. Cell contexts restore their own settings rather
than inheriting the preceding workload's configuration.

### Reuse all three substrates

SSH remains host-driven; Slurm uses one allocation; Kubernetes uses one durable
attempt. Resource sizing considers both workload types and uses the maximum
pending node requirement, including distributed metadata services. Resume skips
successful cells and sizes resources for the remaining work.

Kubernetes freezes the canonical control root from the normalized union of test
roots. Its PVC-wide Lease, exact resource ownership, credential-free coordinator,
collection gate, and existing supported-fault scope remain unchanged. A sealed
batch can retry an initial submission failure only when all cells remain pending
and exact rollback evidence proves no active or ambiguous predecessor remains.

### Unified reporting, separate comparisons

The filesystem reporting front door delegates to the established IO and metadata
engines. It produces separate group reports and an index, never averages groups,
and consults the authoritative success ledger. Identical coordinates need not
mean identical workloads. The specialized reporters remain compatible with
ordinary runs and cached imports; Kubernetes reporting uses collected local
artifacts without implicit cluster access.

## Validation approach

Fast tests cover assembly and seal interruption, concurrent append/start,
immutable provenance, saved-environment isolation, resource sizing, group-owned
artifacts, collection retries, success filtering, reporting, and legacy behavior.
This keeps fault permutations out of the expensive real fixture.

The added real `mixed-batch` scenario runs IO → metadata → IO on SSH, Slurm, and
Kubernetes. It proves preparation does not dispatch, starts through the other
launcher, injects failure, preserves successful work through resume, and verifies
group provenance, reporting, collection, and cleanup. The complete Docker SBX
catalog and pinned source checks validate compatibility with existing behavior.
The existing manual dual-architecture NFS workflow remains the additional
architecture/storage validation path; this record does not claim that a remote
workflow was run by the local implementation session.

## Deliberately deferred

Live append, conversion of ordinary runs into batches, automatic allocation
rollover, and batched deletion are outside this feature.

The [dataset-reference follow-on](elbencho-batch-dataset-references.md) builds on
stable group/execution IDs, retained planned targets and workload modes, and a
shared manifest reader. Bookkeeping remains separate from metric extraction so
future cleanup cells can produce status and logs without benchmark measurements.
