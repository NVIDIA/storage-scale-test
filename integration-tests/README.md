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
tools when needed. It also builds a small derived Slinky login image containing
the standard `file` package required by `validate_env.sh`.

Run setup (or its exact synonym, start) with:

```bash
sudo -v
sudo integration-tests/bin/integration-test.py setup
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
sudo integration-tests/bin/integration-test.py \
  --storage-backend sbx-shared setup
```

The SBX profile requires Docker's private engine to bind-mount the checked-out
repository path. It uses the tested kind v0.30.0/Kubernetes v1.34.0 profile and
maps `/dev/null` to `/dev/kmsg` in kind nodes only when the SBX environment
lacks that device. When Docker SBX exposes its proxy CA, setup installs that CA
in the disposable kind nodes so containerd can pull the fixture images. The
default shared root is `tmp/integration-sbx-shared`; an alternate path may be
set with `--sbx-shared-root`, but must remain below the repository's `tmp/`
directory.

Setup records the invoking pre-sudo account, gives that non-root account
access to the private kubeconfig, key, and test-run workspace, and verifies it
can use Docker, kind, and kubectl. A root shell without `SUDO_USER` must name
the account explicitly with `--test-user USER`.

SSH workers normally use separate `emptyDir` homes. A scenario that requires
the RWX shared-home claim owns a bounded StatefulSet transition and restores
separate homes afterward. Setup and SSH test preflight recover an interrupted
transition before allowing more SSH work; the kind and Slurm fixtures remain
running throughout.

The generated host SSH key, strict known-hosts file, and two worker addresses
are kept under `/var/lib/storage-scale-test-integration/`. Re-running setup
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

Each substrate runs one 4 KiB buffered execution on one node and one on two
nodes through the real filesystem sweep entry point. The harness materializes
one immutable tracked-source snapshot and builds a real deployment archive from
it with `utils/build_tarball.sh`. It caches the validated archive by snapshot
manifest, architecture, builder options, and seeded Elbencho/runtime identity.
Targeted reruns extract that artifact into isolated workspaces instead of
rebuilding it. The SSH case launches `validate_env.sh` and
`nv-elbencho-sweep.sh` on
the host and reaches the two worker pods over SSH. The Slurm case streams the
same archive to the LoginSet, extracts it in the shared storage filesystem, and
launches both commands there.

The NFS profile uses a size-limited, checksum-verified upstream benchmark
archive. Docker SBX, where GitHub release assets may be unavailable, extracts
the binary and runtime libraries from the digest-pinned upstream
`breuner/elbencho:v3.1-11` image and includes them only in the generated test
deployment. Timestamped build and step logs are retained below the state
directory's `test-runs/` directory. The test also requires successful execution
records, exact one- and two-node workload totals, ordered worker selection,
nonempty benchmark output, environment snapshots, and cleanup of its generated
data directories. It then runs `utils/extract-elbencho.sh` on a host-side copy
of each result and requires the report to contain both node counts.

## On-demand CI

The `Filesystem integration` GitHub Actions workflow runs independent amd64
and arm64 jobs concurrently. Each job runs setup twice, stops and restarts the
fixture, proves that root test execution is rejected, runs `test` as the
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
also removes the dedicated export and configuration, disables and stops
`nfs-server`, removes any harness-owned UFW rule, and unmounts the verified
loop-backed filesystem. For Docker SBX it removes only the marker-owned shared
root and never invokes NFS, systemd, firewall, loop, or mount operations. It
refuses destructive cleanup when the applicable ownership and path checks do
not match the fixture. Operating-system packages, kind, kubectl, Helm, and
reusable upstream Docker image layers are not uninstalled. If a locally built
tag existed before setup, teardown restores that exact prior image ID instead
of deleting it.
