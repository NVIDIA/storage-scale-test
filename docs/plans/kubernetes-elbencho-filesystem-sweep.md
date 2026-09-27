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

# Kubernetes Elbencho Filesystem Sweep: Implemented Design Record

## Status and authority

The design described here is implemented. This document records the problem,
the major decisions, and their consequences; it is not an implementation plan
or a source of future work.

Current interfaces and operator instructions live in [README.md](../../README.md)
and [env.sh.template](../../env.sh.template). The exact lifecycle state
machines, invariants, fault boundaries, diagnostic obligations, and supported
recovery behavior are normative in the
[Kubernetes Elbencho Lifecycle and Fault Contract](../KUBERNETES_ELBENCHO_LIFECYCLE.md).
[DESIGN.md](../DESIGN.md) and
[ARCHITECTURE_DIAGRAMS.md](../ARCHITECTURE_DIAGRAMS.md) describe the current
repository architecture. If this historical record conflicts with those
current documents or the implementation, it is not authoritative.

The remaining release gates in the normative lifecycle contract are the
external-cluster acceptance run and its subsequent final adversarial review.
Repository-controlled behavior is covered by the kind/RWX integration fixture
and fast contract tests; a representative Teleport-mediated cluster is still
required to validate real credential expiry, its CNI and policy behavior, and
its RWX storage implementation.

## Problem that the design solved

The filesystem scale sweep originally had two synchronous execution
substrates: passwordless SSH and Slurm. Kubernetes required the same ordered
Elbencho cells, worker-service lifecycle, results, and resume semantics, but
the control environment was materially different:

- `kubectl` access could be mediated by short-lived Teleport credentials.
- Nodes did not provide SSH access and should not expose Elbencho through host
  networking or host ports.
- The cluster already owned scheduling and Pod replacement.
- Benchmark storage was an existing RWX claim that the tool must use but never
  provision or delete.
- A complete sweep could outlive the submitting shell and its credentials.

The accepted design therefore made one whole pending sweep an asynchronous
Kubernetes attempt. The host performs bounded validation and submission; a
credential-free in-cluster Job executes the cells; the PVC holds durable
control state and completed results; later commands inspect, cancel, collect,
or resume the attempt.

## User-visible contract

### Selection and prerequisites

`EXECUTION_SUBSTRATE` has no default and accepts `slurm`, `ssh`, or `kubectl`.
Kubernetes support is limited to the Elbencho filesystem sweep; metadata,
object, and network entrypoints reject it.

Kubernetes mode assumes that plain `kubectl` is already authorized for the
intended cluster. Users provide these values in `env.sh`:

```bash
export EXECUTION_SUBSTRATE=kubectl
export KUBECTL_NAMESPACE=storage-scale-test
export KUBECTL_PV=storage-scale-test-pv
export KUBECTL_PVC=storage-scale-test-pvc
export KUBECTL_NODE_SELECTOR='storage-scale-test/worker=true'
export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.1-11
export KUBECTL_IMAGE_PULL_POLICY=IfNotPresent
export KUBECTL_RUN_AS_USER=2000
export KUBECTL_RUN_AS_GROUP=2000
```

The namespace, PV, bound filesystem-mode RWX PVC, matching worker nodes, and
image access must already exist. The tool neither provisions nor deletes the
cluster, namespace, PV, PVC, storage class, or registry credentials.

The selector is a nonempty comma-separated list of equality requirements. The
selected Ready, schedulable nodes must have one architecture, be able to mount
the PVC, and support direct cross-node Pod IPv4 connectivity on Elbencho's TCP
port 1611. IPv6-only clusters and set-based selectors are unsupported.

`KUBECTL_IMAGE_PULL_POLICY` accepts `Always`, `IfNotPresent`, or `Never`.
`Always` requires a digest-qualified image so a coordinator cannot run a
different build from its workers. The configured numeric UID and GID are used
by validation, transfer, worker, and coordinator Pods and must be permitted by
the PVC and cluster policy.

### Logical test paths

The PVC is mounted in every sweep Pod at:

```text
/mnt/storage-scale-test
```

Users continue to configure logical `TEST_DIRS` keys. Kubernetes mode prepends
the mount root internally; users do not include it:

```text
TEST_DIRS key      Pod path
/bench/fs1         /mnt/storage-scale-test/bench/fs1
bench/fs2          /mnt/storage-scale-test/bench/fs2
```

Absolute and relative logical paths therefore describe the same PVC-relative
location. Empty components, `.` and `..`, control characters, and overlap with
the reserved `.storage-scale-test` subtree are rejected. The submission and
coordinator also resolve live PVC paths to reject symlink escapes, including
generated targets and treefile-cache paths.

### Asynchronous commands

The ordinary invocation submits the whole pending sweep and returns after the
attempt is durable and its Job has been created:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --nodes 1,2,4
```

Submission prints the result directory and copy-pasteable status, cancel, and
collect commands. Later operations address that saved result directory:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --status  RESULTS_DIR
./storage-tests/fs/nv-elbencho-sweep.sh --cancel  RESULTS_DIR
./storage-tests/fs/nv-elbencho-sweep.sh --collect RESULTS_DIR
./storage-tests/fs/nv-elbencho-sweep.sh --resume  RESULTS_DIR
```

`--status` inspects the durable attempt and may perform bounded reconciliation
when exact identity evidence makes that safe. A successful query exits zero
regardless of whether the benchmark succeeded, failed, or was cancelled;
query or reconciliation failure exits nonzero.

`--cancel` stops only the exact journaled attempt and preserves its durable
state. It is retryable when visibility or credentials fail.

`--collect` requires terminal durable state and proof that the exact Job is
inactive or absent under the contract's recovery rules. It verifies and
atomically imports completed results, snapshots, and diagnostics, then removes
only exact attempt-owned resources. Collection is retryable after interruption.
A failed or cancelled benchmark returns nonzero after publishing its partial
results, so automation must read the collected state rather than interpreting
that exit status as a transfer failure.

`--resume` is collection-gated. It uses the collected ledger to create a new
attempt containing non-successful cells and compare-and-swap publishes that
attempt only if the collected predecessor is still current. It never reopens
the previous attempt.

Lifecycle commands require currently valid client credentials until cleanup
has completed, but the running Job does not. Concurrent mutating commands for
one results directory and concurrent sweeps on one PVC are rejected.

## Implemented execution flow

### Submission from the user host

The launcher:

1. parses and validates the sweep with the shared filesystem logic;
2. reifies the complete Cartesian product into ordered execution definitions;
3. validates Kubernetes configuration, API access, namespace/PV/PVC identity,
   logical workload paths, candidate nodes, and requested capacity;
4. creates an eight-character attempt ID and a 128-bit ownership nonce;
5. atomically publishes the local `PREPARED` attempt before external mutation;
6. acquires the PVC-wide reservation and creates the attempt policies and
   worker DaemonSet;
7. freezes node, Pod, address, architecture, and image evidence after every
   worker is Ready;
8. uploads one verified control bundle and the execution definitions to the PVC;
9. creates and UID-journals the coordinator Job; and
10. transitions the local attempt to `SUBMITTED`.

`validate_env.sh` separately uses a short-lived validation Job to prove the
configured image, non-root identity, command tools, and actual PVC
write/read/remove behavior before a sweep is attempted. The coordinator probes
cross-node worker connectivity before it publishes remote `RUNNING`.

Preparation is rollback-capable. A failed submission reaches the terminal
local `SUBMISSION_FAILED` state, retains bounded diagnostics, and does not
pretend that results are available to collect.

### Worker services and networking

The DaemonSet schedules one Elbencho service Pod on every eligible selected
node. The coordinator uses frozen numeric Pod IPv4 addresses; no Kubernetes
Service, DNS dependency, NodePort, host port, or `hostNetwork` is involved.

Attempt-scoped NetworkPolicies admit coordinator-to-worker traffic on TCP
1611. They work with namespaced default-deny policy but cannot override
cluster-wide CNI, admission, or service-mesh restrictions. The live connectivity
probe is therefore authoritative.

Replacement worker Pods are accepted only when the frozen node identity,
architecture, image, and address still match. Changed addressing or node
identity fails the attempt rather than silently retargeting benchmark traffic.

### In-cluster coordinator

The Job mounts the PVC and an `emptyDir` scratch volume, disables automatic
service-account token mounting, and needs no Kubernetes API access. It acquires
the per-attempt coordinator lock, verifies the control bundle and frozen
worker evidence, then runs pending cells sequentially in reified order.

For each cell, the coordinator selects the required prefix of the frozen
worker list, starts the appropriate Elbencho phase through the shared workload
logic, and records the result. The per-cell lifecycle is
`PENDING -> RUNNING -> SUCCESS|FAILED`; unstarted cells stay `PENDING` after an
attempt failure.

Active output remains in Pod-local scratch so persistence traffic cannot
contaminate a measured workload. Between cells, the coordinator copies terminal
artifacts to the PVC, verifies them, and atomically publishes a manifest as the
cell's commit marker. Only published cells are reported as durable successes.

### Durability and recovery

The reserved PVC subtree contains the attempt ledger, ownership records, locks,
frozen endpoints, bundle, cell publications, and completed result material.
The user's filesystem targets remain outside that control tree. The local
result directory contains the immutable configuration snapshot, execution
ledger, current-attempt pointer, collection journal, and imported results.

The design deliberately accepts loss of an active cell's scratch output after
hard Pod or node loss. A subsequent lifecycle command uses the exact saved Job
and PVC evidence to classify coordinator loss, publishes a failed terminal
attempt when justified, and leaves uncommitted cells resumable. Ambiguous
identity, corruption, or API state fails closed rather than adopting or
deleting resources.

The precise local, PVC-run, and per-cell transitions and their linearization
points are defined only in the normative lifecycle contract.

## Ownership and cleanup boundaries

Kubernetes names are conveniences, not proof of ownership. Every attempt
records its nonce plus exact resource kind, name, namespace, UID, and expected
labels/annotations. Namespace, PV, and PVC UIDs freeze the target cluster and
storage identity. Create-intent and cleanup journals make interrupted creation
or deletion retryable.

Cleanup deletes an object only after all recorded identity fields match. It
never uses a broad label query as deletion authority, and it never deletes the
namespace, PV, PVC, storage contents outside the reserved attempt tree,
unrelated workloads, or retained benchmark datasets. Remote release and exact
resource cleanup begin only after verified local publication; `COLLECTED` is
published only after both complete.

## Failure and diagnostic boundary

The implementation distinguishes failures that are common enough to recover
automatically, rare failures that must be diagnosed and left retryable, and
corruption or disaster outside the recovery contract. Errors do not create an
authoritative `UNKNOWN` lifecycle state: the last proven state remains durable
while the command reports uncertainty.

Every public failure path is expected to identify the operation and normalized
reason, state whether work may remain active, retain bounded evidence, and give
the next safe command. Diagnostic capture is best-effort and cannot replace the
primary error. It includes exact retained identities, relevant resource and Pod
descriptions, bounded logs and events, and validated PVC ledger/publication
evidence when available.

The complete supported fault matrix and explicit unsupported situations belong
to the normative lifecycle contract. Notable unsupported cases include
automatic same-attempt benchmark retry, more than one sweep per PVC,
reconstructing lost local identity from untrusted cluster names, recovery from
PVC ledger corruption or permanent PVC loss, heterogeneous worker
architectures, and credential refresh by this tool.

## Regression and acceptance coverage

The feature extended the existing integration framework instead of creating a
Kubernetes-only harness. The same immutable deployment archive, scenario
registry, result assertions, diagnostics, and lifecycle entrypoint exercise
SSH, Slurm, and kubectl.

The real kind/RWX fixture covers Kubernetes baseline and direct I/O,
failure/collection/resume, live capture, retained read data, cancellation,
coordinator loss, endpoint replacement, and interrupted collection. It also
reruns the established SSH and Slurm scenarios so Kubernetes changes cannot
silently regress those substrates. The full catalog runs on amd64 and arm64 in
CI; Docker SBX supports local iteration with the same scenario entrypoint.

Fast tests cover configuration and path validation, template contracts,
ownership and exact deletion, every legal lifecycle edge, rejection of illegal
edges, fault-boundary evidence, command deadlines, collection validation,
diagnostic classification, and resume concurrency. The normative fault matrix
maps each repository-covered row to focused evidence.

Kind cannot validate a production CNI, admission stack, storage driver, or
Teleport session. The remaining external gate deliberately expires the
submitting credential while work runs, reauthenticates for status and
collection, interrupts cancellation and transfer, and proves cluster/PVC
identity rejection against a different context. Its exact procedure is kept in
the lifecycle contract rather than duplicated here.

## Consequences and rejected alternatives

- **Whole-sweep asynchronous Job:** avoids requiring long-lived client
  credentials and repeated scheduling, at the cost of an explicit lifecycle
  and later collection.
- **Pod IPs instead of node IPs:** keeps Elbencho coordination inside the CNI
  and avoids host ports. Frozen identities and endpoint-drift checks compensate
  for Pod replacement.
- **PVC control plane plus local scratch:** preserves every completed cell
  without adding benchmark-time persistence I/O. Active partial output is not
  promised after hard failure.
- **DaemonSet workers:** naturally gives one service per eligible node and
  supports scheduling constraints without a custom controller.
- **No Kubernetes Service:** numeric frozen endpoints match Elbencho's
  coordinator interface and avoid load balancing or DNS changing worker
  identity.
- **No in-cluster Kubernetes client:** the coordinator survives credential
  expiry and has no RBAC, but host-side lifecycle commands own reconciliation
  and resource cleanup.
- **Existing PVC instead of provisioned storage:** keeps storage provisioning
  outside the benchmark tool's scope and makes destructive ownership boundaries
  explicit.
- **Shell-rendered fixed templates:** adds no template-engine dependency;
  strict placeholder validation and checked-in templates bound the format.
- **Collection-gated resume:** ensures the host has the authoritative partial
  ledger and results before it constructs another attempt.

## Implementation map

| Area | Location |
|---|---|
| User entrypoint and CLI dispatch | `storage-tests/fs/nv-elbencho-sweep.sh` |
| Host-side lifecycle and Kubernetes adapters | `storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh` |
| In-cluster coordinator | `storage-tests/fs/kubectl/_nv-elbencho-kubectl-coordinator.sh` |
| Kubernetes object templates | `storage-tests/fs/kubectl/templates/` |
| Shared environment and sweep logic | `lib/env_base.sh`, `lib/env_functions.sh`, `lib/_elbencho_functions.sh` |
| Environment validation | `validate_env.sh` |
| Real fixture and scenario catalog | `integration-tests/` |
| Fast contract and lifecycle tests | `tests/test_elbencho_kubectl_*.py` |

## Historical upstream assumptions

The implementation was based on the upstream Elbencho container and example,
and on Kubernetes' documented Pod networking, DaemonSet placement, Job
semantics, NetworkPolicy, service-account token opt-out, and tar-based transfer
behavior. Review-time validation established that the pinned integration image
supported Linux amd64 and arm64 and contained the runtime tools used by the
coordinator and workers.

Primary references:

- Kubernetes [DaemonSets](https://kubernetes.io/docs/concepts/workloads/controllers/daemonset/)
- Kubernetes [Jobs](https://kubernetes.io/docs/concepts/workloads/controllers/job/)
- Kubernetes [network model](https://kubernetes.io/docs/concepts/services-networking/)
- Kubernetes [NetworkPolicy](https://kubernetes.io/docs/concepts/services-networking/network-policies/)
- Kubernetes [Pods](https://kubernetes.io/docs/concepts/workloads/pods/)
- Kubernetes [service accounts](https://kubernetes.io/docs/concepts/security/service-accounts/)
- Kubernetes [kubectl quick reference](https://kubernetes.io/docs/reference/kubectl/quick-reference/)
- Upstream [Elbencho Kubernetes example](https://github.com/breuner/elbencho/blob/master/docs/k8s-examples.md)
- Upstream [Elbencho container documentation](https://hub.docker.com/r/breuner/elbencho)
