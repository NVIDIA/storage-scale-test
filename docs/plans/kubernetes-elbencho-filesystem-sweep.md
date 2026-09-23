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

# Kubernetes Substrate for the Elbencho Filesystem Sweep

## Purpose

Add `kubectl` as an explicit third execution substrate for the Elbencho
filesystem scale sweep in `storage-tests/fs/nv-elbencho-sweep.sh`.

Kubernetes runs are asynchronous. The executing host validates and reifies the
sweep, starts one Elbencho service Pod per selected node, freezes their Pod IPv4
addresses, uploads the control bundle, and starts one Job for all pending cells.
The original `kubectl` session is not required afterward.

An existing namespace, PersistentVolume (PV), and PersistentVolumeClaim (PVC)
are prerequisites and are never provisioned or deleted. The PVC holds control
state and completed-cell results; active-cell output stays in Pod-local scratch
and moves to the PVC only between measured cells.

One Kubernetes submission is one **whole-pending-sweep attempt**, not one cell.

Extend the existing three-node kind/RWX/SSH/Slinky fixture, scenario catalog,
deployment cache, diagnostics, cleanup, and amd64/arm64 CI; do not add another
harness or lifecycle entrypoint.

## Goals and Non-Goals

### Goals

- Require explicit `slurm | ssh | kubectl` selection with no default.
- Preserve sweep behavior, result compatibility, and successful-cell resume.
- Continue after the submitting Teleport session expires.
- Use the upstream image and one Pod-IP-addressed service per eligible node.
- Persist each completed cell before starting the next.
- Add no template engine, `jq`, Python package, or custom controller dependency.
- Give the in-cluster Job no Kubernetes API credentials or RBAC.
- Support asynchronous status, cancellation, collection, cleanup, and resume.
- Exercise Kubernetes beside SSH and Slurm in the existing real-fixture
  scenario catalog and fast contract-test suite.

### Non-goals

- Provisioning cluster, namespace, node, or storage resources.
- Selecting or refreshing Teleport credentials.
- Concurrent Kubernetes filesystem sweeps; overlapping benchmark IO would
  invalidate isolation.
- Supporting other benchmarks in this initial change.
- Persisting active-cell scratch after coordinator-Pod loss.
- Vendoring Elbencho or publishing an Elbencho image.

The namespace is never deleted. Attempt IDs, nonces, labels, and object UIDs
identify owned resources. A PVC-wide lock rejects concurrent sweeps, and a
per-attempt lock rejects duplicate coordinators.

## User-visible Configuration

### Explicit substrate selection

Replace the current implicit selection rule with one required setting:

```bash
export EXECUTION_SUBSTRATE=slurm  # slurm, ssh, or kubectl
```

Unset, empty, and unsupported values fail. `lib/env_base.sh` derives exactly
one of:

```text
SLURM_ENABLED=1
SSH_ENABLED=1
KUBECTL_ENABLED=1
```

Existing Slurm and SSH settings retain their meanings. `SSH_HOST_LIST` no
longer selects the substrate; it is required only when
`EXECUTION_SUBSTRATE=ssh`.

Other entrypoints must reject `kubectl` with a benchmark-specific unsupported
message.

### Kubernetes settings

Add the following settings to `env.sh.template`:

```bash
export KUBECTL_NAMESPACE=
export KUBECTL_PV=
export KUBECTL_PVC=
export KUBECTL_NODE_SELECTOR=
export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.1-11
export KUBECTL_IMAGE_PULL_POLICY=IfNotPresent
export KUBECTL_RUN_AS_USER=2000
export KUBECTL_RUN_AS_GROUP=2000
```

Their contracts are:

- `KUBECTL_NAMESPACE` is an existing namespace containing the configured PVC.
- `KUBECTL_PV` is the exact PV expected to back the claim.
- `KUBECTL_PVC` is an existing, bound, filesystem-mode claim usable
  concurrently by all worker and coordinator Pods.
- `KUBECTL_NODE_SELECTOR` is a required, nonempty comma-separated list of
  equality selectors,
  for example `storage-test=true,role=client`. Set-based selector syntax is
  not supported in the first implementation because the same value must be
  converted into the DaemonSet and Job scheduling constraints without a YAML
  parser.
- `KUBECTL_ELBENCHO_IMAGE` is configurable and defaults to
  `breuner/elbencho:v3.1-11`. A digest-qualified reference is recommended.
- `KUBECTL_IMAGE_PULL_POLICY` accepts `Always`, `IfNotPresent`, or `Never` and
  defaults to `IfNotPresent`. `Never` supports images preloaded by the
  integration fixture or cluster administrator.
- `KUBECTL_RUN_AS_USER` and `KUBECTL_RUN_AS_GROUP` are positive numeric IDs
  shared by worker, coordinator, and transfer Pods. The defaults match the
  integration workload identity; operators may select IDs compatible with
  their existing PVC.

Validation proves the image tools and PVC access under this identity; it never
assumes a custom image matches the default image.

### Pod-network prerequisites

Kubernetes mode uses ordinary Pod networking, not Node addresses. The cluster
must provide:

- Cross-node Pod-to-Pod TCP connectivity and Pod IPv4 addresses. IPv6-only
  clusters are initially unsupported.
- Admission, CNI, policy, and service-mesh settings that permit direct
  coordinator-to-worker TCP 1611 within `KUBECTL_NAMESPACE`.
- A homogeneous `kubernetes.io/arch` value across selected nodes and a
  compatible image manifest for that architecture.

Attempt-owned NetworkPolicies permit coordinator egress and worker ingress.
They support namespaced default-deny policy but cannot override cluster-wide
policy, admission, or mesh behavior, so a cross-node probe remains authoritative.

No Pod IP needs to be reachable from the executing host. Do not create a
Service, NodePort, host port, or `hostNetwork` Pod for Elbencho coordination;
the in-cluster coordinator consumes numeric Pod IPv4 addresses and needs no DNS
for worker discovery.

### Kubernetes mapping of `TEST_DIRS`

The PVC will always be mounted inside Pods at:

```text
/mnt/storage-scale-test
```

In Kubernetes mode, treat every `TEST_DIRS` key as a PVC-relative logical path
and prepend the mount root internally; users never type the container prefix:

```text
Configured TEST_DIRS key       Kubernetes Pod path
/bench/fs1                     /mnt/storage-scale-test/bench/fs1
bench/fs2                      /mnt/storage-scale-test/bench/fs2
```

The mapping must:

- Strip all leading slashes before joining.
- Collapse redundant slash separators.
- Preserve the configured `TEST_DIRS` weights.
- Reject empty results, `.` components, `..` components, NULs, and any mapping
  that could escape the mount root.
- Reject overlap with the reserved orchestration directory.
- Apply only in Kubernetes mode; Slurm and SSH continue using the configured
  path verbatim.

Treat the saved paths as logical values throughout local reification. Before a
cell runs, map every path-bearing field derived from them: `TEST_DIRS`,
`ELBENCHO_SWEEP_READ_FROM`, generated target roots and CSVs, and treefile-cache
paths. A read-from path must be contained by exactly one logical `TEST_DIRS`
root; map its relative suffix under the corresponding container root. Recompute
generated paths from mapped roots where possible instead of rewriting strings.
Never map result, scratch, or control paths through this rule.

The local mapping helper is lexical validation only; it cannot prove PVC
containment in the presence of symlinks. In phase 5, a validation Pod must
mount the claim and canonicalize the mount root plus every existing mapped
path, or the nearest existing parent for a path that will be created. Follow
symlinks with the image's `realpath`/coreutils and require each canonical path
to remain boundary-contained below the canonical mount root. Apply the check
to `TEST_DIRS`, read-from paths, generated-target parents, treefile-cache
paths, and the reserved orchestration parent. Reject any symlink escape before
creating resources or benchmark data; do not describe lexical normalization
as protection against filesystem-level aliasing.

Reserve this PVC-relative tree for orchestration state:

```text
.storage-scale-test/
├── locks/kubernetes-elbencho-sweep/  atomic global run lock
└── runs/<run-id>/
```

Generated workload targets and operator-provided staged-read paths must not
resolve inside that tree. The orchestration tree must not resolve inside a
generated workload target.

## Asynchronous Command Lifecycle

Refactor argument handling into an explicit operation selector before doing
environment-dependent work. Exactly one operation is active:

| Operation | Invocation | Substrates | Creates a new attempt? |
|---|---|---|---|
| Submit | workload flags plus `--nodes` | Slurm, SSH, Kubernetes | Kubernetes only |
| Resume | `--resume <results_dir>` | Slurm, SSH, Kubernetes | Kubernetes only |
| Status | `--status <results_dir>` | Kubernetes | No |
| Cancel | `--cancel <results_dir>` | Kubernetes | No |
| Collect | `--collect <results_dir>` | Kubernetes | No |
| Delete-only | `--delete-only <path>` | Slurm and SSH initially | No |

Parse the operation before sourcing `env.sh` or performing environment checks.
Submit and delete-only source the current configuration. Resume, status,
cancel, and collect load their versioned, trusted local snapshot instead;
Kubernetes lifecycle operations use the caller's current `kubectl` credentials.
They require the saved namespace, PV, and PVC names and UIDs to match the live
objects. Save context and API-server strings for diagnostics, but do not use
either as the sole cluster identity because an authentication proxy can expose
the same server URL for multiple clusters. Kubernetes `--delete-only` fails as
unsupported in this initial change.

Reject missing values, incompatible or repeated options, positional arguments,
and workload flags with status, cancel, collect, or resume. Preserve SSH and
Slurm resume behavior. New snapshots record `EXECUTION_SUBSTRATE`; a legacy
snapshot requires `EXECUTION_SUBSTRATE=ssh|slurm` in the invoking environment
and may not become Kubernetes. Keep existing submit validation and state that
an attempt covers the pending sweep.

### Submit

The normal invocation remains the submission command:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh [workload flags] --nodes <node_spec>
```

In Kubernetes mode it will:

1. Validate configuration and the workload.
2. Create the local result directory and snapshots.
3. Reify the complete Cartesian product into the local execution ledger.
4. Generate and persist a Kubernetes attempt ID.
5. Create the transfer Pod, atomically acquire the PVC-wide run lock and reserve
   the run directory, then keep the Pod available for the later upload.
6. Create the attempt NetworkPolicies and worker DaemonSet.
7. Wait for one Ready worker on every recorded node; discover and verify each
   worker's Pod IPv4 address.
8. Write the frozen endpoint mapping into the control bundle, upload that
   bundle once through the transfer Pod, and delete the transfer Pod.
9. Create the sweep Job, persist every created object's UID locally, and return
   after the API accepts it.

Submission does not wait for the sweep. Its final output prints stable
`STORAGE_SCALE_TEST_RESULTS_DIR=<path>` and
`STORAGE_SCALE_TEST_ATTEMPT_ID=<id>` records plus copy-pasteable `--status`,
`--cancel`, and `--collect` commands. Human prose is not an integration-test
interface.

If reservation, policy creation, DaemonSet readiness, endpoint discovery, or
upload fails before Job creation, delete only the attempt's resources and PVC
directory, release its verified global lock, and mark it `SUBMISSION_FAILED`.
If Job creation returns an ambiguous error, query its exact name and ownership
nonce before deciding whether submission succeeded or rollback is safe.

### Status

Add:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --status <results_dir>
```

`--status` never mutates the ledger, results, Job, or DaemonSet. It may create
and delete an attempt-owned, read-only inspector Pod when no attempt Pod can be
executed.

It reports local and remote identities, namespace/PVC, Job and Pod state, active
cell, durable cell counts, last update, terminal outcome, and allowed next
action. It compares current worker Pod UIDs and IPs with the frozen mapping and
flags replacement or endpoint drift.

While the Job container is running, status may use `kubectl exec` to read the
PVC-backed summary. If the container is unavailable or terminal, create a
short-lived, read-only inspector Pod that mounts the PVC, reads the same
summary, and is deleted before status returns. A durable `SUCCESS`, `FAILED`,
or `CANCELLED` record is terminal even if the Job was removed externally; Job
conditions corroborate it. A missing Job without a durable terminal record is
ambiguous and requires cancellation to quiesce work and publish `CANCELLED`.
If API and ledger evidence disagree, report both and refuse to claim success.

On a successful query, status exits zero regardless of active or failed sweep
state and emits exactly one stable `STORAGE_SCALE_TEST_KUBECTL_STATE=<state>`
record, where state is `PREPARED`, `SUBMITTED`, `RUNNING`, `SUCCESS`, `FAILED`,
`CANCELLED`, `SUBMISSION_FAILED`, or `COLLECTED`. Query, identity, ambiguous,
or consistency errors return nonzero rather than another state value. After
collection, status uses the verified local terminal record without loading
credentials or contacting the cluster.

### Collect

Add:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --collect <results_dir>
```

`--collect` refuses active or ambiguous work. A corroborated terminal Job or
durable `SUCCESS`, `FAILED`, or `CANCELLED` record with no active Job is
collectible. A temporary collector streams and validates the exact remote run,
uses the publication manifest to atomically merge it into the local result
layout, removes owned remote state, and records collection. The detailed
verification and cleanup protocol appears below.

A failed sweep is collectible. Collection preserves every completed cell and
the failed cell's available evidence. It returns nonzero when the remote sweep
failed, but the local result directory is left ready for `--resume`.

Transfer or verification failure must leave the remote directory, DaemonSet,
Job, and local pre-collection state intact. Publication is journaled and
idempotent: if it is interrupted after individual renames begin, retry verifies
already-published files and completes the merge. Cleanup never begins until the
entire local publication is verified.

If the attempt is already `COLLECTED`, revalidate the local publication and
return its recorded terminal outcome without recreating Kubernetes resources.

### Cancel

Add:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --cancel <results_dir>
```

Cancellation is the bounded recovery path for an active or wedged attempt. It
verifies saved object UIDs, deletes the exact Job with foreground propagation,
waits for its Pods to stop, deletes the attempt's worker DaemonSet, and uses a
temporary PVC-mounted Pod to atomically record `CANCELLED` when the coordinator
could not publish a terminal state. It does not delete the remote run
directory, lock, or results; the next required action is `--collect`. Repeated
cancellation is idempotent.

If saved ownership evidence proves that no Job was ever created and no
coordinator state exists, cancellation instead completes pre-Job rollback,
removes the partial run directory and owned lock, and records
`SUBMISSION_FAILED`; there are no benchmark results to collect. An ambiguous
Job create must be resolved by exact name and ownership nonce before choosing
either path.

Never infer cancellation targets from a broad namespace query: use identities
saved before submission and verify kind, name, UID, nonce, and labels.

### Resume

The existing command remains:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --resume <results_dir>
```

For Kubernetes, resume is allowed only after the prior terminal attempt has
been collected. This requirement exists because the asynchronous Job updates
the authoritative execution ledger on the PVC, not in the disconnected local
result directory.

Resume will:

1. Reject a still-running or terminal-but-uncollected attempt.
2. Load the collected configuration and execution ledger.
3. Preserve `SUCCESS`, reset stale `RUNNING` to `PENDING`, and include `FAILED`
   and `PENDING` in the next attempt.
4. Run Submit steps 4–9 with a new attempt ID.

The collection-before-resume rule is intentional: until collection, the PVC
ledger is authoritative and the disconnected local ledger cannot safely know
which cells completed. Help and error messages must state the required next
command, not merely reject resume.

The documented state machine is:

```text
submit → status (repeat while running)
  ├─ success → collect → done
  ├─ failure → collect partial results → resume → new attempt
  └─ active/wedged → cancel → collect partial results → resume or stop
```

## Persistent and Local State

### Attempt identity

Generate an eight-character lowercase hexadecimal attempt ID. Resource names
use the prefix:

```text
sst-elb-<attempt-id>
```

Use these stable names for durable resources and prefixes for temporary
helpers:

```text
sst-elb-<id>-workers     DaemonSet
sst-elb-<id>-sweep       Job
sst-elb-<id>-worker-net  worker-ingress NetworkPolicy
sst-elb-<id>-coord-net   coordinator-egress NetworkPolicy
sst-elb-<id>-upload-<op> initial transfer Pod
sst-elb-<id>-status-<op> temporary inspector Pod
sst-elb-<id>-collect-<op> collection Pod
```

Apply the following labels to every created object:

```yaml
app.kubernetes.io/name: storage-scale-test
app.kubernetes.io/instance: sst-elb-<id>
app.kubernetes.io/component: <workers|coordinator|transfer|network-policy>
app.kubernetes.io/managed-by: storage-scale-test
storage-scale-test.nvidia.com/benchmark: elbencho
storage-scale-test.nvidia.com/run: <id>
```

Generate a separate 128-bit ownership nonce before mutation and place it in a
`storage-scale-test.nvidia.com/ownership` annotation on every object and in the
PVC reservation. The short attempt ID is for names and diagnostics; the nonce
disambiguates an `AlreadyExists` result or lost create response and is required
in addition to labels and UIDs.

Generate a short random operation token for each helper Pod. Record its exact
name and UID before use so an interrupted or concurrent status or collection
command cannot adopt another helper. Mutating lifecycle commands also acquire
an attempt-local lock in the local metadata directory and reject concurrent
operations; status remains read-only.

Generate a candidate ID, reject an existing named object or PVC run directory,
and retry on collision. Use create-only API operations; never adopt or mutate a
pre-existing object with the candidate name. Kubernetes creation and atomic
PVC-directory creation remain authoritative if a check/create race occurs.

### Local metadata

Alongside `env_used.yaml`, `env_used.sh`, and `executions/`, store one immutable,
schema-versioned metadata directory per attempt plus an atomic pointer to the
current attempt. Before the first remote mutation, record the attempt ID;
context and API server; namespace, PV, and PVC names and UIDs; requested image
and pull policy; numeric workload identity; selector, mapped paths, remote path,
and intended resource names. Save the ownership nonce locally. Journal each
created object's UID immediately after creation, then add frozen endpoints,
timestamps, outcome, and lifecycle state (`PREPARED`, `SUBMITTED`,
`CANCEL_REQUESTED`, `TERMINAL`,
`COLLECTION_IN_PROGRESS`, `COLLECTED`, or `SUBMISSION_FAILED`). Keep a
human-readable snapshot and the
minimal sourceable sidecar needed by lifecycle commands. Never store kubeconfig
credentials or overwrite prior attempts when resuming.

These executable/resumable files receive the existing trusted-input warnings.

### Remote PVC layout

The transfer Pod atomically creates
`.storage-scale-test/locks/kubernetes-elbencho-sweep/`, records the attempt ID,
ownership nonce, namespace/PV/PVC names and UIDs, and timestamp, then atomically
creates the run directory.
An existing lock blocks submission and is never stolen. Upload one bundle:

```text
/mnt/storage-scale-test/.storage-scale-test/runs/<id>/
├── control/
│   ├── bundle-manifest.tsv
│   ├── env_used.yaml
│   ├── env_used.sh
│   ├── coordinator.sh
│   ├── _platform_functions.sh
│   ├── _elbencho_functions.sh
│   ├── required shared libraries
│   ├── worker-endpoints.tsv
│   └── executions/
│       └── NNNN.sh
├── state/
│   ├── coordinator.lock/       created atomically by the coordinator
│   ├── run.status
│   ├── run-summary.tsv
│   ├── publication-manifest.tsv
│   ├── env_used.yaml            durable working snapshot
│   ├── env_used.sh              durable working snapshot
│   └── executions/
│       ├── NNNN.status
│       ├── NNNN.workers.tsv   selected node/Pod/UID/IP rows
│       └── NNNN.exitcode
└── results/
    ├── executions/
    │   ├── NNNN.log
    │   ├── NNNN.core
    │   ├── NNNN.write.json
    │   ├── NNNN.read.json
    │   ├── NNNN.delete.json
    │   └── NNNN.workload.tsv
    └── normal Elbencho result, CSV, live-CSV, and tree files
```

All Pods mount this PVC, so libraries and execution definitions are uploaded
once, not copied per Pod. Elbencho comes from the image and is never uploaded.

`bundle-manifest.tsv` is immutable and hashes the uploaded control files.
`publication-manifest.tsv` is the atomically replaced index of durable state
and results available for collection. Each versioned row gives a role, remote
relative path, intended local relative path, size, and SHA-256 digest. It
defines how the remote trees merge into the ordinary local result layout;
collection does not copy their shape blindly. Temporary commit and transfer
files are excluded.

Initialize the working snapshots from the control copies. When existing
treefile-cache logic changes `env_used.yaml` or `env_used.sh`, publish the new
snapshots and their manifest rows between cells. Collection maps the durable
working copies back to the local result root so a later resume sees the same
cache history as the coordinator.

`run.status` uses `PREPARED`, `RUNNING`, `SUCCESS`, `FAILED`, or `CANCELLED`;
only the last three are terminal. Per-execution status uses `PENDING`,
`RUNNING`, `SUCCESS`, or `FAILED`.

## Kubernetes Resource Design

### Manifest rendering

Add checked-in templates for the transfer, status, and collector Pods, worker
DaemonSet, sweep Job, and two NetworkPolicies. Render only a narrow, validated
set of placeholders:

- Namespace
- Attempt ID, operation token, ownership nonce, labels, and annotations
- Image, pull policy, and numeric user/group
- PVC name
- Node-selector key/value pairs
- Recorded worker node names
- Coordinator node name
- Remote run directory

Perform rendering in Bash and submit through `kubectl create -f -`. Do not add
a template engine. Validate every replacement before interpolation so values
cannot change YAML structure or indentation. Treat `AlreadyExists` as a
collision or ambiguous-create result, fetch the object, and never overwrite it.
After each create, record its UID. Before later inspection or deletion, require
the saved name, UID, ownership nonce, attempt labels, and expected kind to
match.

### Node discovery

Use `kubectl get nodes` with the configured selector and Kubernetes-supported
JSONPath or custom-column output. Do not require `jq`.

For every candidate node:

1. Require the Node Ready condition to be true.
2. Require one common `kubernetes.io/arch` value and record it.
3. Record its Kubernetes node name and UID.

Sort the names deterministically and require at least the maximum node count
among pending executions. All matching Ready nodes form the worker pool; do not
silently truncate it.

Choose and persist the coordinator node: the first ordered node when
`ORDER_NODES=1`, or one randomized candidate when ordering is disabled. After
the DaemonSet is Ready, list its nonterminating Pods by exact attempt labels.
Require one Pod on every recorded node, a distinct Pod UID, and a Ready
condition. Select one IPv4 address from each Pod's `status.podIPs`, reject
missing or duplicate addresses, require a common resolved container image ID,
and write node name/UID, Pod name/UID/IPv4, architecture, and image ID in
deterministic node order to `control/worker-endpoints.tsv`. The Job uses this
frozen mapping and has no Kubernetes API dependency.

### Worker DaemonSet

Create one DaemonSet for the attempt:

- Use `KUBECTL_ELBENCHO_IMAGE`.
- Use `KUBECTL_IMAGE_PULL_POLICY` and the configured numeric user and group.
- Constrain scheduling to the configured selector.
- Add required node affinity with `matchFields: metadata.name In [...]` for the
  recorded Ready nodes, so the DaemonSet cannot later expand onto an unrecorded
  matching node.
- Run exactly one Pod on every recorded node.
- Use the normal Pod network and `ClusterFirst` DNS policy; do not set
  `hostNetwork` or `hostPort`.
- Declare container port 1611 for clarity; no Service exposes it.
- Mount the configured PVC at `/mnt/storage-scale-test`.
- Run `elbencho --service --foreground` as the main container process.
- Add HTTP readiness and liveness probes for `/status` on port 1611, with a
  startup probe or equivalent delay so liveness cannot kill a slow startup.
- Use the Kubernetes restart policy inherited by DaemonSet Pods to restart a
  failed service container.
- Disable service-account token mounting, privilege escalation, and Linux
  capabilities, and use `RuntimeDefault` seccomp. Apply the same security
  context to every attempt Pod. Do not set `fsGroup` or recursively change PVC
  ownership; validation must prove the configured identity already has access.

A container restart within the same Pod retains its endpoint and needs no host
upload. Pod replacement changes its UID and may change its IP. Status reports
either drift; if the IP changes, the coordinator's endpoint probes fail the
attempt rather than silently changing the worker set. A same-IP replacement
may continue if Elbencho is healthy.

After applying the DaemonSet, verify that Ready Pods correspond one-for-one
with recorded nodes. On timeout, do not create the Job; perform the pre-Job
rollback defined under Submit. The Job repeats a bounded endpoint check.

### Asynchronous sweep Job

Create one Job for all pending executions:

- Use `KUBECTL_ELBENCHO_IMAGE`.
- Use the same requested image, pull policy, workload identity, and hardened
  security context as the workers. The coordinator verifies the expected
  Elbencho version before changing durable state; digest-qualified production
  references and the integration fixture's private preloaded tag prevent image
  drift more strongly than a mutable tag can.
- Override the image entrypoint with Bash and execute the coordinator script
  from the PVC control bundle.
- Mount the PVC at `/mnt/storage-scale-test`.
- Mount an `emptyDir` at a fixed scratch path such as
  `/tmp/storage-scale-test`.
- Use the normal Pod network and `ClusterFirst` DNS policy; do not set
  `hostNetwork` or `hostPort`.
- Set `automountServiceAccountToken: false`.
- Create no ServiceAccount, Role, RoleBinding, or ClusterRole.
- Restrict scheduling to the configured candidate pool.
- Pin the Job Pod to the saved coordinator node so single-node cells preserve
  direct execution without a service hop.
- Set `restartPolicy: Never`.
- Set `backoffLimit: 0` to disable controller retries after a failed Pod.
- Do not set a TTL that deletes the Job or Pod before status and collection.

The Job needs only the PVC, image, worker mapping, and cluster networking, so it
survives Teleport expiry. It has no Kubernetes API authority; probes handle
worker-process failure and collection cleans up. Because Kubernetes can rarely
start a nominally single-run Job twice, the coordinator acquires a PVC lock
before touching state; a second coordinator exits.

### Coordination NetworkPolicies

Create the two policies before their Pods. Match both ends by attempt and
component labels, allow only TCP 1611, and avoid `ipBlock` with ephemeral Pod
addresses. These allow rules make the workload compatible with namespaced
default-deny policy; they cannot override stronger administrator or mesh
controls. The submit-time connectivity probe is authoritative because the API
does not report when policy enforcement has converged.

## Sweep Coordinator Behavior

### Startup

The coordinator will:

1. Load the saved run configuration and libraries from `control/`.
2. Validate that the attempt ID and remote paths match its manifest-provided
   values.
3. Atomically acquire `state/coordinator.lock`; fail without changing the
   ledger if another coordinator owns it.
4. Load the fixed node/Pod/IPv4 mapping.
5. Check every expected `IP:1611` endpoint using the Elbencho HTTP status
   protocol.
6. Retry within a fixed deadline and fail before any benchmark if
   the service fleet does not become healthy.
7. Atomically write sweep state `RUNNING` and the initial summary.

Do not reset a `RUNNING` cell inside an attempt: it is evidence that the prior
coordinator may have died. Collection imports that state, and resume resets it
locally before creating a new attempt. The lock records Pod identity and start
time and remains until collection; a new attempt uses a new run directory.

### Execution ordering and worker selection

Iterate execution IDs in existing `NNNN` order and skip `SUCCESS`.

- With `ORDER_NODES`, select the first `N` recorded Pod IPv4 addresses.
- Without `ORDER_NODES`, persist a random `N`-worker sample for each multi-node
  cell before marking it `RUNNING`.
- A one-node execution always runs directly in the coordinator Pod, without
  `--hosts`; the coordinator node was chosen according to the ordering mode at
  submission. This is the explicit single-node exception to per-cell sampling.
- A multi-node execution passes its selected address CSV through
  `ELBENCHO_RUN_HOSTS_CSV`.

Probe the selected endpoints before each multi-node cell. If an endpoint is
gone, do not refresh in place: fail the attempt, collect completed cells, and
resume into a new DaemonSet and mapping. A failure during measured IO fails that
cell normally. Abort the remaining sequence after the first failure.

### Common-runner refactor

Make the common workload runner accept an explicit run context rather than
inferring Slurm or SSH from ambient variables. The context contains:

- Execution ID
- Node count
- Worker endpoint CSV
- Container-mapped test-directory CSV
- Scratch result directory
- Durable state/result directory
- IO size, threads, depth, and saved workload flags from `NNNN.sh`
- Substrate-specific service-health and result-publication hooks

Keep reification and workload behavior shared. Move Slurm service checks out of
substrate-neutral phase code and into its adapter. SSH retains its watchdog;
Kubernetes uses DaemonSet probes and precomputed Pod endpoints.

### Per-cell durable commit

For each non-successful execution:

1. Source `control/executions/NNNN.sh`.
2. Translate all saved logical workload paths to container paths.
3. Atomically persist selected worker rows, write durable
   `NNNN.status=RUNNING`, and update the summary before measured IO.
4. Create a clean execution-specific directory in `emptyDir`.
5. Direct Elbencho result files, logs, completion JSON/TSV, treefiles, and core
   dumps exclusively to scratch.
6. Run the existing workload phases and cleanup rules against the mapped PVC
   benchmark targets.
7. Wait for all benchmark activity and cleanup activity to finish.
8. Copy each existing scratch artifact to a temporary file under the durable
   remote results tree.
9. Atomically rename every temporary artifact into its final name.
10. Persist the execution exit code and any updated working snapshots.
11. Write `NNNN.status=SUCCESS` or `FAILED` after its artifacts, then update the
    summary.
12. Atomically replace the publication manifest last; this is the cell's commit
    point. Collection treats terminal files absent from that manifest as
    uncommitted `RUNNING` evidence.

No result-artifact copy to the PVC may run concurrently with an Elbencho phase.
The small pre-cell status write and post-cell durable commit are explicit
boundaries outside measured benchmark activity.

If a benchmark phase fails, preserve its original return code, run the existing
best-effort workload cleanup, durably publish available evidence, mark the cell
failed, and stop the sweep.

If the coordinator Pod disappears during a cell, only scratch for that cell is
lost. The durable state remains `RUNNING`; collection preserves it and a later
resume resets it to `PENDING`.

### Sweep termination

On normal completion, write durable sweep status `SUCCESS`. On a controlled
cell failure, write `FAILED` with the failed execution ID and original return
code. Signal handlers should publish `FAILED` when possible without claiming
durability for incomplete scratch artifacts.

The worker DaemonSet remains running after either outcome because the Job has
no Kubernetes API authority. Status and documentation must make the required
later collection/cleanup visible.

## Collection and Cleanup

### Collector Pod

Create a temporary collector Pod in the configured namespace using the selected
image and PVC mount. Override the entrypoint with an idle command, wait for the
Pod, and stream a tar archive of exactly the remote attempt directory through
`kubectl exec`. Compression is optional and must use a codec available both in
the image and locally.

Use an explicit tar stream rather than relying on broad `kubectl cp` behavior.
Both mechanisms require `tar` in the container, so validate this image
capability before submission.

Write the stream to a temporary archive adjacent to the result directory.
Before extraction, inspect its member paths and types; reject absolute paths,
`..` traversal, links, devices, unexpected run IDs, and anything outside the
expected layout. Enforce configured byte and member-count bounds while
streaming and inspecting. Extract only after validation into a new staging
directory.

### Verification and publication

Before publishing any file, verify:

- Attempt identity and namespace/PVC metadata.
- Saved configuration snapshots.
- Every expected `NNNN.sh` definition.
- Exactly one valid status value per execution.
- Numeric exit codes for terminal executions.
- All workload-specific artifacts required for each `SUCCESS` cell.
- Result filenames and paths are contained by the intended output directory.

After verification, write a collection journal, then merge remote results and
statuses using temporary files and atomic per-file renames. For an existing
destination, require identical content or preserve it as a conflict and stop.
Preserve submission logs and append collection metadata. Mark publication
complete only after a second full verification of the local result tree.

### Cleanup ordering

Only after successful local publication:

1. Verify the saved UIDs and delete `sst-elb-<id>-workers`; wait for its Pods.
2. Verify the saved UIDs and delete `sst-elb-<id>-sweep` and its Pods.
3. Delete the known transfer/inspector Pods and NetworkPolicies by recorded
   kind, name, and UID, excluding the active collector. Do not discover
   deletion targets by an open-ended namespace query.
4. Through the collector, delete only
   `.storage-scale-test/runs/<id>` after revalidating the exact normalized path.
5. Remove the global run lock only after verifying that its owner is `<id>`.
6. Delete the collector Pod.
7. Mark the local attempt `COLLECTED` and record its terminal sweep outcome.
8. Release any remaining local dispatch ownership state.

Cleanup is retryable. Persist each completed step so a later `--collect` can
finish cleanup without retransferring or republishing identical results.
Missing objects are idempotent success only when the saved UID is no longer
present; a same-name object with another UID is unrelated and must survive.

Never delete the configured namespace, PV, PVC, storage class, unrelated Pods,
or data retained by `--write-only`.

If any verification, local publication, or cleanup precondition fails, report
the exact retained Kubernetes resources and remote directory. Do not remove
the only durable copy of the results.

## Validation Changes

Extend `validate_env.sh` for Kubernetes without changing the assumption that
plain `kubectl` already targets the correct cluster.

Validation must check:

- `kubectl` reaches the API; print/save context and server, then record the
  namespace, PV, and PVC UIDs used for later identity checks.
- The namespace exists and `kubectl auth can-i` permits required operations,
  including node list/get; PV get; PVC get; Job, DaemonSet, NetworkPolicy, and
  Pod create/get/list/delete; event list; and Pod exec and logs.
- The PVC is in the configured namespace, is `Bound`, has filesystem volume
  mode, advertises `ReadWriteMany`, and names `KUBECTL_PV` in
  `spec.volumeName`.
- The PV exists, the selector uses supported equality syntax, enough Ready
  nodes match it, and they report one supported architecture.
- No active labeled attempt exists in the namespace or reserved PVC tree.
- A validation Pod starts the image with the configured pull policy and numeric
  identity, finds Elbencho/Bash/tar/coreutils, mounts the PVC, and atomically
  creates/removes a unique probe under the reserved root.
- Mapped benchmark paths are writable and do not overlap orchestration state.
- A temporary pinned worker DaemonSet receives distinct Pod IPv4 addresses, and
  a coordinator Pod reaches every worker's Elbencho `/status` endpoint on TCP
  1611, including across nodes. Apply the attempt-style NetworkPolicies so the
  probe exercises the actual policy path.

Validation must remove only its own labeled probe resources and files.
Every `kubectl` invocation has both a client request timeout and a local process
timeout bounded by its operation deadline.

Treat `kubectl auth can-i` as an early diagnostic, not proof that admission,
quota, policy, scheduling, image pull, networking, or storage mount will
succeed. Runtime probes remain mandatory and errors must identify the failed
resource and command.

## Regression Framework Integration

### Reuse the existing fixture

Reuse these existing fixture resources:

- Three kind nodes: one control-plane/login node and two workers labeled
  `storage-scale-test/target=true`.
- The fixture namespace and its bound `storage-test-rwx` claim, backed by either
  loop-backed NFS or the Docker SBX shared-path backend.
- The ordinary test account, user-owned state directory, kubeconfig, pinned
  client tools, lifecycle ownership checks, process-group timeouts, repeated
  teardown, and post-cleanup diagnostics.
- The digest-pinned upstream Elbencho container used to seed Docker SBX test
  deployments; NFS-backed runs currently seed the same version from verified
  release archives.

Only the fixture provisions namespace, PV, PVC, nodes, and credentials; they
remain product prerequisites. Derive `KUBECTL_PV` from the fixture PVC's
`spec.volumeName`, select `storage-scale-test/target=true`, and keep logical
`TEST_DIRS` in the scenario subtree. Kubernetes mounts the claim at
`/mnt/storage-scale-test`; SSH and Slurm mounts do not change.

Generalize the driver's existing verified image acquisition and kind-import
helpers for the integration-pinned Elbencho index. Accept a cache hit only when
its digest and architecture match, fail closed on Docker errors, pull only when
absent, and import through a temporary fixture tag. Publish workload aliases
only inside kind's containerd; never overwrite host tags. Repeated setup
revalidates the pin. The product image remains configurable; this digest is a
test input.

Include immutable CNI configuration in the retained fixture-profile
fingerprint. Existing profile drift replaces the disposable kind cluster; a
changed workload-image pin can instead be revalidated and reloaded without
rebuilding unrelated SSH, Slurm, or storage state.

Probe NetworkPolicy enforcement in both fixture profiles. If the current CNI
cannot enforce it, rebuild the disposable cluster with a digest-pinned,
multi-architecture policy-capable CNI and include its kind/CNI configuration in
the profile fingerprint. Preload and await it. A real probe must show
coordinator access and denial from an unrelated Pod without disrupting SSH or
Slurm; applying policy YAML alone is not coverage.

Make fixture validation substrate-aware: Kubernetes tests require nodes, PVC,
image, and network but not healthy SSH or Slinky Pods. Make login/SSH/Slurm
fields optional in the discovered fixture rather than retaining the current
unconditional login-Pod dependency. Use a short-lived utility Pod for storage
preparation and inspection instead of the Slinky login Pod.

### Extend the scenario model instead of adding another runner

Add concrete substrate `kubectl`; `all` expands to all three only after its
adapter and baseline scenario work. The current planner sorts by scenario phase,
order, name, and substrate, so merely adding an enum would run Kubernetes in
the pre-transition batch. Change planning explicitly: run selected non-Kubernetes
pre-shared work, perform at most one shared-home SSH transition and restoration,
then run Kubernetes work, followed by existing post-shared work. Kubernetes-only
scenarios have no SSH home mode. Expose compatibility and schedule phase through
`test --list-scenarios` metadata.

Keep the current planner and workload-spec catalogs in exact name/substrate
parity, with planner tests covering schedule metadata. Continue caching the
zero-argument `build_tarball.sh` output by tracked snapshot manifest,
architecture, fixed integration recipe, and seeded Elbencho/runtime identity.
Each Kubernetes scenario runs packaged validation and sweep entrypoints from an
isolated extraction; Pods use its uploaded libraries and image-provided
Elbencho.

Add a Kubernetes adapter to `filesystem_integration.py`; do not fork the common
workload assertions. The adapter must:

1. Render a scenario-specific `env.sh` with explicit substrate, fixture
   namespace, observed PV/PVC, target selector, private pinned image, pull
   policy `Never`, workload UID/GID, and logical test roots. Supply the private
   fixture kubeconfig through `KUBECONFIG`; product configuration still assumes
   plain `kubectl` uses the active credentials.
2. Run packaged validation and submit the sweep as the non-root test user.
3. Capture the local results directory and attempt metadata from stable command
   output, then poll `--status` with a scenario deadline and per-call timeout.
4. Exercise `--collect` after terminal state, accepting its documented nonzero
   result for a failed or cancelled attempt while requiring successful local
   publication.
5. Feed the collected tree into the existing execution, workload, dataset, and
   reporting assertions. Read attempt identity, endpoints, and worker selection
   from durable sidecars, not console prose or generated Pod names.
6. Preserve status, Job/Pod descriptions, events, worker and coordinator logs,
   and the durable PVC summary before cleanup on failure.

For `failure-resume`, stage the failure-once wrapper only in the first attempt's
control bundle. It delegates ordinary coordinator invocations to the image
binary, uses an atomic marker for the injected invocation, and is absent from
the resumed attempt; worker DaemonSet commands always use the real image
binary. Run packaged environment validation against the real image before
rendering the integration-only wrapper override.

Create a cleanup-capable scenario identity before the first mutation. Cleanup
first uses the product lifecycle: cancel active work, collect durable results,
and verify that recorded objects, the remote run directory, and global lock are
gone. A final fixture-only path may delete exact recorded kinds, names, UIDs,
and labels and remove a marker-verified scenario directory. Attempt every
cleanup, preserve the primary scenario error, and report cleanup errors
secondarily. Copy diagnostics outside disposable state before CI teardown.

Use the framework's process-group timeout for every local child. On timeout or
signal, capture diagnostics, cancel the exact Kubernetes attempt, collect when
possible, and return to the CI wrapper without an orphaned child or Job. Keep
the protected harness lock outside teardown-owned state. Common-runner changes
must preserve Slurm's exact-allocation cancellation and inherited traps.

### Real-scenario allocation

Do not duplicate shared Cartesian combinations merely because a third
substrate exists. Add Kubernetes where it exercises a real transport,
scheduler, persistence, or lifecycle boundary. Lifecycle-injection scenarios
use bounded workloads long enough to observe durable `RUNNING`; the harness
waits for that state instead of sleeping for a fixed interval.

| Scenario | Kubernetes coverage |
|---|---|
| `baseline` | One- and two-node submit, running status, Pod-IP coordination, successful collect, report extraction, and exact resource cleanup. |
| `default-dio` | Default direct-I/O worker-directory behavior and logical-to-container path mapping. |
| `failure-resume` | Failed Job, nonzero collect with partial results, a new attempt, skipped successes, and successful recollection. |
| `live-capture` | Live artifacts in Pod scratch followed by between-cell durable publication and reporting. |
| `kubectl-retained-read` | Kubernetes-only write-only/collect followed by read-from in a new attempt; fixture cleanup replaces unsupported product delete-only. |
| `kubectl-cancel` | Observe `RUNNING`, cancel twice, collect twice, and verify durable evidence plus absence of active work. |
| `kubectl-coordinator-loss` | Delete a coordinator Pod during a bounded cell, verify terminal Job plus durable `RUNNING` evidence, collect, and resume. |
| `kubectl-endpoint-drift` | Replace one worker after endpoint freeze and verify UID/IP drift reporting; changed IP must fail the attempt, while a same-IP replacement may continue only after health succeeds. |

Keep `slurm-cartesian`, `slurm-scheduling`, and the SSH single-file,
weighted-root, and shared-home scenarios on their current substrates. Cover the
full combinatorics, invalid modes, and replacement-state branches with fast
tests. Real endpoint-drift assertions must branch on the observed replacement
IP rather than assume the CNI will or will not reuse it.

### CI and local execution

No-option `test` must pick up Kubernetes through the substrate registry; do not
add a CI script or job. `--substrate kubectl` supports retained-fixture
iteration. CI keeps NFS and concurrent amd64/arm64 coverage. Its wrapper still
propagates lifecycle or teardown failure, attempts teardown twice, and uploads
post-cleanup diagnostics. Docker SBX uses the same wrapper with `sbx-shared`.

Give each Kubernetes scenario its own deadline. Measure the expanded
dual-architecture run before changing the current lifecycle and job bounds; if
they change, retain generous headroom for repeated teardown and artifact
upload. A scenario timeout follows the cancel, diagnostic, collection, and
wrapper-cleanup path above.

## Implementation Sequence

1. Before product changes, use one retained fixture to prove the pinned image
   under the configured non-root identity, DaemonSet placement, direct
   cross-node Pod-IP coordination, PVC access, and NetworkPolicy behavior on
   both backends. Decide and pin any required CNI, include its rendered
   configuration in the fixture fingerprint, and rerun the existing SSH/Slurm
   catalog after the fixture change.
2. Add explicit `EXECUTION_SUBSTRATE` selection and validation in
   `env.sh.template`, `lib/env_base.sh`, `validate_env.sh`, and every entrypoint;
   update the integration environment renderer in the same change. Add isolated
   precedence, legacy-resume, and unsupported-benchmark tests, then rerun the
   existing real catalog.
3. Give `lib/_elbencho_functions.sh` an explicit cell-run context. Keep
   reification and workloads shared, move Slurm/SSH lifecycle policy into their
   adapters, and preserve all existing fast and real scenarios.
4. Refactor `nv-elbencho-sweep.sh` so it parses operations before configuration
   loading. Add versioned attempt metadata, stable command output, path mapping,
   create-only rendering, identity/ownership checks, and fake-`kubectl` tests;
   keep Kubernetes dispatch unreachable until its state machine is complete.
5. Add bounded Kubernetes validation, reservation, image and Pod discovery,
   transfer, status, cancellation, collection, and cleanup helpers plus checked-in
   templates under `storage-tests/fs/kubectl/`. Persist immutable identity before
   mutation and journal every transition. Test ambiguous creates, signals, hostile
   archives, and interrupted cleanup without a real cluster.
6. Implement the locked Job coordinator, scratch-to-PVC commit protocol,
   first-failure stop, durable summary, collected resume, and integration-only
   failure overlay. Land fake-Elbencho tests for each transition and rerun all
   SSH/Slurm regression scenarios.
7. Generalize architecture-aware image import, add substrate-aware fixture
   discovery and the storage utility Pod, then add the Kubernetes enum, adapter,
   planner scheduling, and `baseline` scenario in one change. This avoids a
   revision where no-option `test` selects an unimplemented substrate. Run the
   baseline on retained NFS and Docker SBX fixtures.
8. Add the remaining Kubernetes scenarios incrementally, keeping planner/spec
   parity and all existing assertions. Use focused
   `--substrate kubectl --scenario ...` runs against one retained fixture, then
   run the complete Docker SBX lifecycle.
9. Run the full NFS-backed catalog on amd64 and arm64, perform external-cluster
   acceptance, adjust only measured deadlines, and update user, requirement,
   design, architecture, integration, roadmap, and context documentation.

Every phase lands with passing tests and leaves SSH and Slurm usable. Every new
text file carries the NVIDIA Apache-2.0 header.

## Test Plan

### Fast contract and fault tests

Keep tests that need no real cluster under `tests/`, using fake `kubectl`, fake
Elbencho, and temporary control/result trees. Cover equivalence classes and
boundary interactions, not the Cartesian product of every setting:

- Tri-state substrate selection; CLI operation exclusivity; saved-attempt
  restoration; unsupported delete-only and benchmark combinations; and
  list-scenario behavior without fixture state.
- Logical path mapping and weights, multiple roots, traversal and reserved-root
  rejection, read-from mapping, and unchanged SSH/Slurm paths.
- Namespace/PV/PVC/auth/image validation; selector parsing; Ready-node and
  Pod-IP discovery; capacity failure; safe template rendering; labels; and the
  no-RBAC/no-token manifest invariants.
- Attempt-ID and lock collision, ambiguous creation, every pre-Job rollback
  boundary, duplicate coordinator exclusion, and exact ownership checks.
- Running and terminal status through both Pod and inspector paths, Job/ledger
  disagreement, endpoint UID/IP drift, same-Pod restart, and same- versus
  changed-IP replacement decisions.
- Cancel before and during a cell, repeated cancel, timeout/signal behavior,
  partial coordinator state, exact-attempt cleanup, no orphan local process or
  Job, and refusal to delete mismatched resources.
- Collection gates, hostile tar members, interrupted download/publication,
  conflicting local files, failed/cancelled partial results, cleanup journaling,
  and retry after every cleanup boundary.
- Execution order, deterministic and randomized worker subsets, one-node local
  execution, multi-node `--hosts`, phase exit codes, first-failure stop,
  scratch isolation, durable commit ordering, and coordinator loss.
- Resume only after collection, preservation of `SUCCESS`, reset of `RUNNING`,
  new attempt identity and endpoints, and no repeated failure injection.
- Exact scenario-catalog/spec substrate parity and semantic result/report
  assertions without freezing timestamps, Pod names, log prose, or optional
  future artifacts.
- Verified cached-image reuse, wrong-digest and wrong-architecture rejection,
  missing-versus-daemon-error handling, fixture-private import tags, retained
  profile drift, and repeated setup without unnecessary pulls.
- CI-wrapper signal and teardown-failure propagation with a fake driver,
  including timeout during a Kubernetes work item and both teardown attempts.

### Real fixture tests

Run the scenarios in the table above through the existing three-node fixture
with both storage backends during development and the NFS backend in
dual-architecture CI. Require:

- One Ready service Pod on each labeled worker and coordinator connectivity to
  both Pod IPv4 addresses without a Service, host networking, or host ports.
- Enforced attempt NetworkPolicies: the coordinator succeeds and an unrelated
  probe is denied.
- Shared-PVC visibility, correct logical path mapping, no measured overlap with
  result publication, analyzer-compatible collected results, and persistence
  across Job/Pod loss.
- Correct async return points and transitions for submit, status, cancel,
  collect, and resume, including nonzero failure propagation.
- Preservation of namespace, PV, PVC, unrelated fixture Pods, and retained
  write-only data; removal only of exact attempt objects, locks, and run trees.
- Successful repeated setup, stop/start, full catalog, teardown twice, and no
  residual Jobs, kind containers, product run locks, or marker-owned state; the
  external harness lock is released rather than deleted by teardown.

Run the complete repository checks after focused and full fixture validation:

```bash
./utils/run_ci_checks.sh
```

### External-cluster acceptance

Kind proves repository-controlled Kubernetes behavior but not every production
CNI, admission policy, storage driver, or Teleport session. Before declaring
the feature operationally supported, run the same baseline, failure/resume,
cancel, and coordinator-loss cases on a representative Teleport-mediated
cluster with its real RWX claim. Let the original credential expire, then
reauthenticate from a new shell for status and collection. Confirm the saved API
context/server values aid diagnostics while namespace/PV/PVC UID checks prevent
collection from the wrong cluster. Confirm that no cluster-owned namespace, PV,
or PVC is modified.

## Documentation Requirements

Document substrate selection, prerequisites, automatic `TEST_DIRS` prefixing,
the asynchronous lifecycle, Teleport reauthentication, durability, unsupported
concurrency/delete-only, the credential-free Job, required collection/cleanup,
the external image, and Pod-network requirements. Explain the frozen mapping
and the effects of container restart and same- or changed-IP Pod replacement.

Errors for terminal-but-uncollected resume must explain that collection imports
the authoritative PVC ledger and partial results before a new attempt can be
constructed. Submission output must print copy-pasteable status and collection
commands, plus cancellation for an active attempt.

Include the asynchronous lifecycle and the separation among the external
launcher, sweep Job, worker DaemonSet, Pod-IP mapping, NetworkPolicies, PVC
control/results area, and filesystem targets in the architecture diagrams.

## Validated External Assumptions

Revalidate these version-sensitive facts during implementation. At review time,
the integration pin
`breuner/elbencho:v3.1-11@sha256:719fba92cab57c773ddf7a2776414b358aeb8126a15fbc8e3c52469ce3a5b8b2`
was an OCI index with Linux amd64 and arm64 manifests. The image contained
Elbencho, Bash, tar, and coreutils; its service ran as UID/GID 2000 and returned
JSON from `/status` on port 1611. The upstream example passes worker Pod IPs to
an in-cluster coordinator. Kubernetes documents direct Pod networking,
NetworkPolicy segmentation, DaemonSet placement, possible duplicate Job
starts, token-mount opt-out, and tar-based transfer; these drive the design.

Primary references: the Kubernetes documentation for
[DaemonSets](https://kubernetes.io/docs/concepts/workloads/controllers/daemonset/),
[Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/),
[the network model](https://kubernetes.io/docs/concepts/services-networking/),
[NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/),
[Pods](https://kubernetes.io/docs/concepts/workloads/pods/),
[service accounts](https://kubernetes.io/docs/concepts/security/service-accounts/),
and [kubectl file transfer](https://kubernetes.io/docs/reference/kubectl/quick-reference/),
plus the upstream [Elbencho Kubernetes example](https://github.com/breuner/elbencho/blob/master/docs/k8s-examples.md)
and [container documentation](https://hub.docker.com/r/breuner/elbencho).
