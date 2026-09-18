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

# Single-host integration environment feasibility and handoff

## Outcome

A functional Kubernetes, passwordless-SSH, shared-storage, and Slurm fixture is
feasible on one Linux server. The implemented harness uses rootful Docker and
kind to create three logical Kubernetes nodes:

| Node | Label | Fixture roles |
|---|---|---|
| Control plane | `storage-scale-test/login=true` | Kubernetes control plane, negative scheduling control, Slinky LoginSet, MariaDB, and `slurmdbd` |
| Worker 1 | `storage-scale-test/target=true` | SSH worker and one `slurmd` |
| Worker 2 | `storage-scale-test/target=true` | SSH worker and one `slurmd` |

The control-plane taint is removed so its lack of the target label is the
negative-selection test. A fourth node is unnecessary: the lightweight Slinky
LoginSet is schedulable on the control plane and is not a compute node.

This fixture validates orchestration and substrate behavior. Logical nodes on
one server share physical resources, so it is not suitable for performance,
scaling, failure-domain, or network-isolation measurements.

## Implemented lifecycle

The executable driver is `integration-tests/bin/integration-test.py` and
supports four actions:

- `setup` installs missing host dependencies, creates or reconciles the
  environment, and validates every substrate.
- `start` is an exact synonym for `setup`.
- `stop` is disposable: it deletes only the marker-owned kind cluster and then
  stops the NFS service only when the harness started it and no unrelated
  exports exist.
- `test` requires an already-running setup and runs selected bounded filesystem
  regression cases without reconciling the environment.

Stop preserves installed packages and tools, downloaded charts, Docker image
caches, generated SSH and database credentials, rendered state, and external
NFS data. It intentionally discards Kubernetes objects, kind containers, and
MariaDB's node-local storage. A later start creates and validates a fresh
cluster, so it takes longer than setup against an already-running cluster.
The disposable stop and subsequent fresh start were exercised end to end, as
was a repeated stop with the cluster already absent.

Deleting the cluster avoids relying on resumed kind container addresses.
Kubernetes Service DNS stabilizes application endpoints inside the cluster but
cannot make the node-container underlay stable, and the host cannot normally
route cluster-local DNS names. A fresh cluster is consequently simpler and more
reliable than restoring persisted CNI, kube-proxy, and host-network state.

Lifecycle operations are serialized by a state-directory lock. The driver
uses a private kubeconfig on every kind, kubectl, and Helm command. A persistent
ownership marker prevents adoption or deletion of an unrelated same-named
cluster or export directory.

## Pinned software

The initial harness pins:

- kind 0.33.0;
- Kubernetes node image and kubectl 1.37.0;
- Helm 3.22.0;
- NFS CSI chart 4.13.4 and its sidecar versions;
- Slinky charts 1.2.0; and
- a digest-pinned MariaDB 11.4 image.

Client binaries are downloaded with their published checksums. The NFS CSI
chart is extracted from a versioned source archive after verifying both the
source archive and embedded chart SHA-256 values. CSI images use the production
Kubernetes registry with the official staging registry as a bounded fallback.

The setup path currently targets Linux on x86-64 or ARM64, Python 3.12 or newer,
an accessible rootful Docker daemon, at least two CPUs, 8 GiB total memory,
6 GiB available memory, and 20 GiB free space. These are admission limits for
the fixture, not a benchmark sizing recommendation.

## Shared storage

The host runs a narrowly configured NFSv4.1 server with two server threads.
Its export is backed by a persistent 128 MiB sparse ext4 image. This gives the
project's mount validation a filesystem distinct from the host root while
keeping disk consumption bounded and retaining data across disposable stops.
Provisioning discovers the kind Docker network's IPv4 subnet and gateway; it
does not assume a fixed bridge address. The export:

- is restricted to the discovered kind subnet;
- uses `all_squash` and maps requests to numeric UID/GID 2000;
- is installed through dedicated files in `/etc/exports.d` and
  `/etc/nfs.conf.d` without replacing unrelated configuration; and
- opens TCP 2049 only for the kind subnet when UFW is already active.

Numeric ownership is applied as `+2000:+2000` to avoid accidental name-service
resolution of numeric-looking account names.

The upstream NFS CSI driver dynamically provisions two independent
`ReadWriteMany` claims:

- `storage-test-rwx` is mounted at `/mnt/storage-test` by the SSH workers,
  Slinky LoginSet, and Slurm workers.
- `ssh-home-rwx` optionally supplies the shared `/home/tester` mode.

Both claims use deterministic namespace/PVC subdirectories and retain their
external NFS data when the disposable Kubernetes cluster is deleted. Requested
PVC sizes are metadata rather than NFS server quotas. MariaDB uses kind's local
`ReadWriteOnce` provisioner and is deliberately disposable.

## SSH fixture

The SSH substrate is a two-replica StatefulSet with required pod anti-affinity
and a target-node selector. Each pod uses host networking and listens on its
kind worker's bridge address, making both workers reachable from the Linux host
without a NodePort or LAN-facing host port.

The image provides a dedicated `tester` user with UID/GID 2000. Setup generates
one Ed25519 fixture key, stores it in protected state, and creates a Kubernetes
Secret only when absent. An init container copies key material into the home
directory with strict ownership and modes. Password and root authentication are
disabled, while `/root` remains on local overlay storage.

Two home modes are supported:

- `separate` mounts a per-pod `emptyDir` at `/home/tester`.
- `shared` mounts the dedicated NFS RWX home claim at `/home/tester` in both
  workers.

The host discovers both live worker addresses, writes a strict known-hosts file
and an `ssh_hosts` list into protected state, and proves public-key access to
both. Validation also proves root rejection, bidirectional worker SSH, distinct
placement, the selected home visibility semantics, shared RWX visibility, and
local `/root` storage.

## Slurm fixture

Slinky installs CRDs, the operator, and the Slurm custom resources in that
order. The conservative profile disables cert-manager, monitoring, accelerator
support, container plug-ins, high availability, and external load balancers. It
uses:

- one LoginSet on the login-labeled control plane;
- one DaemonSet-mode NodeSet selecting the two target workers;
- one partition containing that NodeSet;
- controller persistence disabled for the disposable cluster; and
- one directly managed MariaDB instance plus `slurmdbd` for accounting.

The `slurmd` containers have a small CPU request but no CPU limit. A limit can
cause effective CPU discovery to reach zero on constrained hosts. Other
components retain modest requests and limits. SSH workers are temporarily
scaled to zero during the heaviest Slinky reconciliation and restored before
setup succeeds.

Slinky 1.2.0 renders a new self-signed webhook CA during a same-values operator
upgrade when cert-manager is disabled. The driver therefore skips releases that
are already deployed at the pinned chart version. A real operator upgrade
restarts and waits for the webhook before applying Slurm custom resources.

Slurm validation discovers pods by their actual workload container names rather
than relying on generic labels that the operator does not publish. It verifies:

- the LoginSet runs on the control plane;
- exactly two `slurmd` pods run on the two target nodes;
- the LoginSet sees the NFS mount;
- a two-node `srun` reaches two distinct Slurm nodes; and
- a two-node `sbatch --wait` is reported by `sacct` as `COMPLETED|0:0`.

Commands are submitted from the LoginSet, matching the intended login/submit
execution point.

## Filesystem regression cases

The `test` action accepts `all`, `filesystem`, `ssh`, and `slurm` selectors.
`all` and `filesystem` run both substrate cases; `ssh` and `slurm` can run
individually or together. Test execution deliberately refuses to run without a
matching saved setup and a healthy live three-node fixture.

The harness first copies only tracked working-tree files to an isolated staging
tree and invokes `utils/build_tarball.sh` there. It validates the resulting
archive's paths, types, size, required files, and packaged benchmark executable
before extraction. Each case renders `env.sh` from the packaged user-facing
`env.sh.template`, injects only bounded fixture overrides, and executes
`validate_env.sh` before the benchmark.

The SSH case runs both validation and `nv-elbencho-sweep.sh` on the test host;
the checked-in SSH implementation copies and launches its worker payloads in
the two pods. The Slurm case streams the same deployment archive to the
LoginSet, extracts it in the shared NFS filesystem, and runs both commands from
that extracted tree. The sweep uses one 4 KiB file, one thread, queue depth one,
buffered I/O, and one-node and two-node dimensions. Substrates run sequentially.
This covers deployment packaging, environment validation, sweep reification,
SSH and Slurm dispatch, service startup, write/read/delete phases, result
recording, and cleanup while writing only a few KiB per execution.

The pinned elbencho release archive is architecture-selected, capped during
download, checksum-verified, and cached in protected state. Its verified native
binary is supplied to the isolated tree as the deployment builder's documented
local cache, so the integration test does not vendor a benchmark binary. A
minimal derived Slinky login image installs the standard `file` package required
by `validate_env.sh`; the test does not replace that prerequisite with a
test-specific implementation.

Every run retains host-side logs beneath `test-runs/`. Success requires two
successful execution records, zero exit codes, workload manifests, environment
snapshots, nonempty CSV and text output, and no remaining generated benchmark
directory at the NFS root. Performance values are not asserted.

## Idempotency and failure behavior

Setup reuses generated credentials, existing claims, cached downloads, and
already-current Slinky releases. It uses declarative Kubernetes apply and Helm
upgrade/install for resources that require reconciliation. Re-running setup on
a healthy cluster repeats validation without rotating credentials.

If setup finds marker-owned stopped or incomplete kind containers, it deletes
that disposable partial cluster and creates a fresh one. An unowned same-named
cluster or nonempty export directory is a hard error with remediation text.

Every external command and readiness gate has a timeout. Downloads use bounded
retries and checksum verification. Secret-bearing commands are redacted from
logs. On setup failure, diagnostics include bounded host capacity, filesystem,
Docker, kind, NFS service/export, Kubernetes node/pod, and sorted event output;
Kubernetes Secrets are never dumped.

Stop is idempotent. Repeated stop calls succeed when the cluster and owned NFS
service are already absent or inactive. It never stops Docker globally and
leaves a pre-existing or shared NFS service running.

## Provisioning sequence

An implementation or future refactor should preserve this ordering:

1. Acquire the lifecycle lock and initialize protected logging.
2. Validate the platform and conservative capacity floor.
3. Install the narrow host package set and verify rootful Docker.
4. Install checksum-verified kind, kubectl, and Helm clients as needed.
5. Validate cluster ownership, replacing only an owned partial cluster.
6. Create and validate the three-node kind topology and labels.
7. Create or mount the bounded sparse ext4 backing image, then configure and
   probe the subnet-scoped NFSv4.1 export.
8. Preload pinned CSI images, install NFS CSI, and bind both RWX claims.
9. Build, preload, deploy, and validate the two SSH workers.
10. Scale SSH down, reconcile MariaDB and Slinky, and validate Slurm.
11. Restore and revalidate the SSH workers.
12. Persist a non-secret state summary and report success.

For disposable stop:

1. Acquire the lifecycle lock.
2. Discover the exact kind cluster and containers.
3. Require the matching ownership marker before deletion.
4. Delete the named kind cluster and verify its containers are gone.
5. Stop NFS only when it is harness-owned and has no unrelated exports.
6. Preserve all host packages, caches, keys, rendered state, and NFS data.

## Checked-in implementation artifacts

| Path | Purpose |
|---|---|
| `integration-tests/bin/integration-test.py` | Setup/start/stop driver, validation, logging, and diagnostics |
| `integration-tests/lib/filesystem_integration.py` | Filesystem test selection, staging, execution, and result assertions |
| `integration-tests/manifests/kind.yaml.tmpl` | Three-node kind topology and neutral role labels |
| `integration-tests/manifests/nfs-csi-values.yaml` | Low-footprint NFS CSI deployment values |
| `integration-tests/manifests/nfs-storage.yaml.tmpl` | StorageClass and two RWX claims |
| `integration-tests/ssh-image.Dockerfile` | Ubuntu/OpenSSH worker image with non-root fixture user |
| `integration-tests/slinky-login-image.Dockerfile` | Slinky login image with the standard `file` prerequisite |
| `integration-tests/manifests/ssh-workers.yaml.tmpl` | Two host-networked SSH workers and selectable home volume |
| `integration-tests/manifests/mariadb-accounting.yaml.tmpl` | Disposable local accounting database |
| `integration-tests/manifests/slinky-operator-values.yaml` | Low-footprint operator and webhook configuration |
| `integration-tests/manifests/slinky-slurm-values.yaml` | LoginSet, NodeSet, shared storage, partition, and accounting |

The checked-in files are the authoritative associated artifact contents. They
should be changed together with this handoff whenever topology, versions,
resource policy, or lifecycle semantics change.

## Remaining work

The implemented first pass covers the basic filesystem sweep through SSH and
Slurm. Future cases can add metadata, write-only/read-from/resume, failure
recovery, shared-home mode, and Kubernetes dispatch coverage while preserving
the same bounded-data and no-performance-assertion policy.
