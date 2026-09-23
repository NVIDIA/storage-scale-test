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

# Single-host integration environment

`bin/integration-test.py` provisions the lightweight, three-node kind fixture
used to exercise Kubernetes, SSH, and Slurm storage substrates on one Linux
server. The control-plane node is also the Slinky login node and negative
control for the storage-worker label. The two worker nodes run storage clients.

The current setup target is Ubuntu 24.04 on x86-64 or ARM64 with at least two
CPUs, 8 GiB total RAM, 6 GiB available RAM, and 20 GiB free on the selected
backend's filesystem. Python 3.12 and an accessible rootful Docker daemon are
prerequisites. The driver installs its other host packages and pinned client
tools when needed. It also builds small derived Slinky login and compute images
containing the fixed integration workload account; the login image additionally
provides the standard `file` package required by `validate_env.sh`.

Run setup (or its exact synonym, start) as the ordinary test user. The NFS
profile invokes passwordless `sudo` itself only for package installation and
the dedicated export, loop-device, firewall, and systemd operations:

```bash
sudo -v
integration-tests/bin/integration-test.py setup
```

`--storage-backend auto` is the default. It selects `nfs` when the host has the
required loop, mount, systemd, and kernel NFS facilities. It selects
`sbx-shared` only when a recognized capability needed by that profile is
absent. An explicit backend never falls back, and setup records the selection;
changing it requires teardown first. Arbitrary download, image, Kubernetes,
manifest, storage-visibility, SSH, and Slurm failures remain fatal.

The `nfs` backend uses a loop-backed NFSv4 export and NFS CSI. The
`sbx-shared` backend is specifically for Docker SBX: it mounts one
repository-backed directory into every kind node and binds static RWX claims to
separate test-data and shared-home subdirectories. Both implement the same
in-scope integration contract: cross-node and host read/write visibility,
shared-home behavior, and successful SSH and Slurm filesystem sweeps. NFS and
CSI provisioning themselves are infrastructure details outside this
repository's test scope; the backend difference is environmental fidelity, not
repository feature coverage.

Select Docker SBX explicitly with:

```bash
integration-tests/bin/integration-test.py \
  --storage-backend sbx-shared setup
```

The SBX profile requires Docker's private engine to bind-mount the checked-out
repository path. It uses the tested kind v0.30.0/Kubernetes v1.34.0 profile and
maps `/dev/null` to `/dev/kmsg` in kind nodes only when the SBX environment
lacks that device. The nested SBX kernel cannot run Kindnet's nftables policy
path, so this backend uses checksum-verified Calico with digest-pinned images
preloaded through host Docker. The NFS profile retains kind's pinned Kindnet.
When Docker SBX exposes its proxy CA, setup installs that CA in the disposable
kind nodes. The default shared root is `tmp/integration-sbx-shared`; an
alternate path may be set with `--sbx-shared-root`, but must remain below the
repository's `tmp/` directory. This profile never invokes `sudo`; required host
packages and an accessible Docker engine must already be present. Its two
disposable backing directories deliberately use non-sticky mode `0777`. Docker
SBX can map the host caller and UID 2000 workloads to different owners, so
omitting the sticky bit lets either side create and remove scenario data. This
is safe only for these marker-owned, disposable leaves below the repository's
`tmp/` directory.

Setup also preloads the pinned upstream Elbencho image under a fixture-private
node reference and runs a temporary Kubernetes prerequisite probe. It requires
one non-root service Pod on each worker, direct Pod-IPv4 access from a
coordinator, denial from an unrelated Pod, and bidirectional PVC visibility.
The probe uses no Service, host networking, host ports, service-account token,
or external pull from kind nodes, and removes its objects and storage afterward.

Every lifecycle action runs as the ordinary test account and refuses root.
Kubeconfig, keys, downloaded clients, cached deployments, rendered manifests,
logs, and test runs remain user-owned from creation under the default
`tmp/integration-state` directory. Setup never recursively changes that
tree's ownership. It verifies that the caller can use Docker, kind, kubectl,
and the state directory after provisioning.

The host account and in-cluster workload account are deliberately independent.
LoginSet coordination, Slurm jobs, SSH workers, and shared-storage staging run
as `tester` with UID/GID 2000; setup verifies that identity on the coordinator
and on both real `srun` tasks. This matches the NFS export's anonymous mapping,
so clients create scenario data directly with a restrictive umask and never
attempt to change ownership through an all-squashed mount.

SSH workers normally use separate `emptyDir` homes. A scenario that requires
the RWX shared-home claim owns a bounded StatefulSet transition and restores
separate homes afterward. Setup and SSH test preflight recover an interrupted
transition before allowing more SSH work; the kind and Slurm fixtures remain
running throughout.

The generated host SSH key, strict known-hosts file, and two worker addresses
are kept under `tmp/integration-state/`. Re-running setup
reconciles and validates the environment without replacing those credentials
or retained backend data.

After setup succeeds, run the bounded filesystem regression cases with:

```bash
integration-tests/bin/integration-test.py test
integration-tests/bin/integration-test.py test --substrate ssh
integration-tests/bin/integration-test.py test --scenario baseline
integration-tests/bin/integration-test.py test --list-scenarios
```

With no options, `test` runs every available scenario on each applicable
substrate. `--substrate` accepts `all`, `ssh`, or `slurm`; repeatable
`--scenario` options select named cases independently. Scenario listing needs
no setup state or privileges. Actual tests refuse root execution, require the
saved non-root identity, validate the live topology, generate environments
from the packaged `env.sh.template`, and run `validate_env.sh` before a sweep.

The real scenario catalog covers buffered and direct I/O, one- and two-node
selection, failure and resume, retained write/read/delete data, extended live
CSV capture, and result reporting on both SSH and Slurm. Focused cases add a
multidimensional Slurm sweep, Slurm include/exclude and exclusive-user
allocation behavior, SSH weighted roots, generated and staged single-file
work, and shared SSH homes. Workloads stay deliberately small; assertions
check execution coordinates and state transitions, phase and workload
evidence, dataset totals, required native flags, relevant scheduling evidence,
and semantic report rows and plot families without treating incidental output
or performance values as contracts.

The Kubernetes substrate runs the same filesystem sweep as one asynchronous
cluster Job. `submit` returns after staging the control bundle and creating
the attempt; `status` reads its durable state, `collect` copies completed
cell results into the local result directory, and `cancel` stops the exact
attempt while preserving collected data. `--resume` is collection-gated:
collect first, then resume the local partial result tree. The whole sweep,
not an individual node count, is the asynchronous unit. The remote Job does
not depend on later kubectl credentials; its control ledger and completed
cells live under a reserved subtree on the configured PVC. Active benchmark
output is scratch data, published only between cells.

Kubernetes requires an authorized context plus an existing namespace, PV,
PVC, and selector-matching worker nodes. It discovers selected nodes and
freezes their Pod addresses, starts one Elbencho service Pod per node, and
uses ordinary Pod networking with an attempt-scoped NetworkPolicy. It does
not use host networking, host ports, or a Service. The coordinator and
short-lived validation helpers use the configured Elbencho image and PVC
mount; all remote resources carry the attempt ownership identity and are
removed only after identity validation. A real external cluster acceptance
run must still verify its CNI, Pod-to-Pod policy, storage behavior, and
credential lifetime; the local SBX fixture proves the repository lifecycle
and networking contract on its supported kind profile.

The K-specific regression cases cover retained read-after-collection,
cancellation, coordinator loss before and during execution, and worker
endpoint replacement. The common baseline, direct-I/O, failure/resume, and
live-capture cases also run through kubectl; the planner keeps substrate-only
cases separate from the SSH and Slurm catalog.

With `EXECUTION_SUBSTRATE=kubectl` in `env.sh`, the lifecycle commands are:

```bash
storage-tests/fs/nv-elbencho-sweep.sh --nodes 1,2
storage-tests/fs/nv-elbencho-sweep.sh --status "$RESULTS_DIR/elbencho-<run>"
storage-tests/fs/nv-elbencho-sweep.sh --collect "$RESULTS_DIR/elbencho-<run>"
storage-tests/fs/nv-elbencho-sweep.sh --resume "$RESULTS_DIR/elbencho-<run>"
```

Use `--cancel` instead of `--status` when stopping an active attempt. The
submit command is intentionally asynchronous; `--collect` is the operation
that transfers terminal results from the PVC to the host.

The harness materializes one immutable tracked-source snapshot and builds a
real deployment archive from it with the zero-argument
`utils/build_tarball.sh`. It caches the validated archive by snapshot manifest,
architecture, fixed integration recipe, and seeded Elbencho/runtime identity.
Every scenario extracts that artifact into an isolated workspace, adds only
its own environment and inputs, and cleans its remote data afterward; host-side
results and diagnostics remain under the state directory. The SSH cases launch
`validate_env.sh` and
`nv-elbencho-sweep.sh` on the host and reach the two worker pods over SSH. The
Slurm cases stream the same archive to the LoginSet, extract it in shared
storage, and launch both commands there.

The NFS profile uses a size-limited, checksum-verified upstream benchmark
archive. Docker SBX, where GitHub release assets may be unavailable, extracts
the binary and runtime libraries from the digest-pinned upstream
`breuner/elbencho:v3.1-11` image and includes them only in the generated test
deployment. Timestamped build and step logs are retained below the state
directory's `test-runs/` directory. The test also requires successful execution
records, exact one- and two-node workload totals, ordered worker selection,
nonempty benchmark output, environment snapshots, and cleanup of generated
data directories. It runs `utils/extract-elbencho.sh` on host-side result
copies and checks the semantic report content applicable to each scenario.

## On-demand CI

The `Filesystem integration` GitHub Actions workflow runs independent amd64
and arm64 jobs concurrently. Each job runs setup twice, stops and restarts the
fixture, proves that root lifecycle execution is rejected, runs `test` as the
ordinary runner account, and tears down twice. A final status job requires both
architectures to pass. The workflow is deliberately absent from ordinary
pull-request and default-branch events.

For a pull request, use the repository's existing PR authorization control—the
same control used to start the regular PR checks. Authorization copies the
reviewed PR commit to the trusted `pull-request/<PR-number>` branch. A push to
that narrowly matched branch starts the integration workflow. Updating a PR
requires authorizing its new head before a new integration run can start. An
existing run can instead be repeated with **Re-run jobs** in GitHub Actions.

Before this workflow file is present on the default branch, that authorized PR
branch is the way to run it. After the workflow is merged, a maintainer can also
open **Actions**, choose **Filesystem integration**, select **Run workflow**,
and choose an authorized branch or the default branch.

Delete the disposable kind cluster and, for the NFS backend when owned
exclusively by the harness, stop NFS with:

```bash
integration-tests/bin/integration-test.py stop
```

Stop preserves packages, downloaded charts, Docker images, generated keys and
passwords, rendered state, and backend data. Kind containers, Kubernetes
objects, and MariaDB's node-local volume are disposable and are deleted. A
subsequent start therefore creates and validates a fresh cluster and takes
longer than an idempotent setup against an already-running cluster. Timestamped
logs and rendered manifests are retained in the state directory. Add
`--verbose` for command-level logging. The failure path captures host, Docker,
backend, Kubernetes node, pod, and event diagnostics without printing
Kubernetes Secrets.

For CI workers or any host where retained fixture data is not wanted, run:

```bash
integration-tests/bin/integration-test.py teardown
```

Teardown is idempotent. It performs the disposable stop and deletes the
fixture's generated data, keys, logs, and locally built image tags. For NFS it
also removes the dedicated export and configuration, removes any harness-owned
UFW rule, and unmounts the verified loop-backed filesystem. It disables and
stops `nfs-server` only when setup started it and no unrelated exports remain;
a pre-existing service is left running. For Docker SBX it removes only the
marker-owned shared root and never invokes NFS, systemd, firewall, loop, or
mount operations. It refuses destructive cleanup when the applicable ownership
and path checks do not match the fixture. Operating-system packages,
pre-existing client tools, and reusable upstream Docker image layers are not
uninstalled. Checksum-verified client copies that the harness downloaded into
its own state tree are removed with that tree. If a locally built tag existed
before setup, teardown restores that exact prior image ID instead of deleting
it.
