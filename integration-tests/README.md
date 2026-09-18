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
CPUs, 8 GiB total RAM, 6 GiB available RAM, and 20 GiB free on `/`. Python
3.12 and an accessible rootful Docker daemon are prerequisites. The driver
installs its other host packages and pinned client tools when needed. It also
builds a small derived Slinky login image containing the standard `file`
package required by `validate_env.sh`.

Run setup (or its exact synonym, start) with:

```bash
sudo -v
sudo integration-tests/bin/integration-test.py setup
```

Setup records the invoking pre-sudo account, gives that non-root account
access to the private kubeconfig, key, and test-run workspace, and verifies it
can use Docker, kind, and kubectl. A root shell without `SUDO_USER` must name
the account explicitly with `--test-user USER`.

The default SSH homes are separate `emptyDir` volumes. To mount the dedicated
RWX NFS claim at `/home/tester` in both workers instead, use:

```bash
integration-tests/bin/integration-test.py --ssh-home-mode shared setup
```

The generated host SSH key, strict known-hosts file, and two worker addresses
are kept under `/var/lib/storage-scale-test-integration/`. Re-running setup
reconciles and validates the environment without replacing those credentials
or retained NFS data.

After setup succeeds, run the bounded filesystem regression cases with:

```bash
integration-tests/bin/integration-test.py test all
```

`all` and `filesystem` select both substrates. `ssh` and `slurm` may be used
individually, or together as two arguments. The `test` action never installs
or reconciles the fixture, and it refuses to run as root. It requires the saved
setup state to match the current non-root account, verifies that the live
topology is healthy, generates each test environment from the packaged
`env.sh.template`, and runs `validate_env.sh` before the sweep.

Each substrate runs one 4 KiB buffered execution on one node and one on two
nodes through the real filesystem sweep entry point. The harness builds a real
deployment archive from a tracked-files-only snapshot with
`utils/build_tarball.sh`, validates its contents, and runs from the extracted
archive. The SSH case launches `validate_env.sh` and `nv-elbencho-sweep.sh` on
the host and reaches the two worker pods over SSH. The Slurm case streams the
same archive to the LoginSet, extracts it in the shared NFS filesystem, and
launches both commands there.

The pinned benchmark archive is size-limited, checksum-verified, and cached
outside the repository before the binary is included in the user-built
deployment archive. Timestamped build and step logs are retained below the
state directory's `test-runs/` directory. The test also requires successful
execution records, exact one- and two-node workload totals, ordered worker
selection, nonempty benchmark output, environment snapshots, and cleanup of
its generated data directories. It then runs `utils/extract-elbencho.sh` on a
host-side copy of each result and requires the report to contain both node
counts.

## On-demand CI

The `Filesystem integration` GitHub Actions workflow runs independent amd64
and arm64 jobs concurrently. Each job runs setup twice, stops and restarts the
fixture, proves that root test execution is rejected, runs `test all` as the
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

Delete the disposable kind cluster and, when owned exclusively by the harness,
stop NFS with:

```bash
integration-tests/bin/integration-test.py stop
```

Stop preserves packages, downloaded charts, Docker images, generated keys and
passwords, rendered state, and external NFS data. Kind containers, Kubernetes
objects, and MariaDB's node-local volume are disposable and are deleted. A
subsequent start therefore creates and validates a fresh cluster and takes
longer than an idempotent setup against an already-running cluster. Timestamped
logs and rendered manifests are retained in the state directory. Add
`--verbose` for command-level logging. The failure path captures host, Docker,
NFS, Kubernetes node, pod, and event diagnostics without printing Kubernetes
Secrets.

For CI workers or any host where retained fixture data is not wanted, run:

```bash
integration-tests/bin/integration-test.py teardown
```

Teardown is idempotent. It performs the disposable stop, removes the dedicated
NFS export and configuration, disables and stops `nfs-server`, removes any UFW
rule that the harness added, unmounts the verified loop-backed filesystem, and
deletes the fixture's generated data, keys, logs, and locally built image tags.
It refuses destructive cleanup when ownership markers, rendered host
configuration, mount backing, or unrelated NFS exports do not match the
fixture. Operating-system packages, kind, kubectl, Helm, and reusable upstream
Docker image layers are not uninstalled. If a locally built tag existed before
setup, teardown restores that exact prior image ID instead of deleting it.
