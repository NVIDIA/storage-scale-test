# NVIDIA Storage Scale Test

Shell and Python tools for running distributed storage benchmarks and turning
their results into tables and plots.

- [Filesystem testing](docs/FILESYSTEM_TESTING.md): data IO and metadata with elbencho.
- [Object storage testing](docs/OBJECT_STORAGE_TESTING.md): S3-compatible storage with Warp.
- [Network testing](docs/NETWORK_TESTING.md): peer throughput and latency with elbencho (beta).

## Warning: these tests destroy data

Tests **create, overwrite, and delete files and objects**. Use dedicated test
directories and empty, disposable buckets. Root access is neither required nor
recommended.

## Getting started

The workflow is **configure → validate → run → report**. Run commands from the
repository root or an unpacked deployment directory.

1. Prepare a host that can reach your workers: a Slurm login host, a
   passwordless-SSH launcher, or a workstation authorized for your Kubernetes
   cluster. See [prerequisites and deployment](#prerequisites-and-deployment).
2. Create the configuration:

   ```bash
   cp env.sh.template env.sh
   ```

3. Edit `env.sh`: [select an execution substrate](#configure-the-execution-substrate),
   configure connection settings, and choose workload targets.
   `RESULTS_DIR` sets the parent directory for local results.
4. [Validate](#validate-and-correct-the-configuration), fix reported problems,
   and repeat until it passes:

   ```bash
   ./validate_env.sh
   ```

5. Follow the [filesystem](docs/FILESYSTEM_TESTING.md#first-io-sweep),
   [object](docs/OBJECT_STORAGE_TESTING.md#configure-the-target-and-run-a-sweep),
   or [network](docs/NETWORK_TESTING.md#configure-and-run) guide to run and report.

Each run creates its own directory below `RESULTS_DIR`. Use that complete
printed directory, not its parent, for reporting and lifecycle commands.
Filesystem launchers can also vary workload settings per run with
[`--env-override <file>`](docs/FILESYSTEM_TESTING.md#workload-override-files)
instead of edits to `env.sh`.
Kubernetes runs asynchronously: collect terminal results before reporting.

## Prerequisites and deployment

- Benchmark processes require Linux. Slurm orchestration also requires Linux.
  macOS may initiate SSH and kubectl filesystem sweeps with Bash 4.3+ and GNU
  coreutils; kubectl also requires GNU tar and `flock`:

  ```bash
  brew install bash coreutils gnu-tar flock
  ```

  Put Homebrew's Bash first on `PATH`. Validation checks prefixed Homebrew
  tools; benchmarks still run on remote Linux hosts or in Linux Pods.
- Reporting requires Python 3.12+. The extraction wrappers set up their Python
  dependencies.
- SSH/Slurm workers need benchmark binaries for each client architecture.
  Kubernetes uses the configured workload image instead.

Use a prepared checkout or create a deployment archive:

```bash
./utils/build_tarball.sh
```

This creates `storage-scale-test.tar.gz`. Unpack it on your launcher and
configure its `env.sh`. Read linked guides from the source checkout or
repository browser; deployment archives omit `docs/`. The builder reuses or
downloads pinned elbencho binaries, includes existing Warp binaries, and
builds `s3test`. See
[object-tool preparation](docs/OBJECT_STORAGE_TESTING.md#prepare-the-tools)
if you need Warp. Existing Elbencho binaries, including custom builds, are
preserved unless `--force-download` is given.

Download failure does not prevent archive creation: check warnings and verify
tools for every target architecture. The repository does not ship benchmark
binaries, and NVIDIA does not distribute your generated archive. Review
third-party provenance and licenses in [NOTICE](NOTICE), including elbencho's
GPL-3.0 and Warp's AGPL-3.0.

## Configure the execution substrate

Set `EXECUTION_SUBSTRATE` explicitly in `env.sh`; there is no default.
`SSH_HOST_LIST` configures SSH but does not select it.

| Substrate | Filesystem | Object storage | Network |
| --- | --- | --- | --- |
| `ssh` | Supported | Supported | Beta |
| `slurm` | Supported | Supported | Beta |
| `kubectl` | Supported | Not supported | Not supported |

### Passwordless SSH

```bash
export EXECUTION_SUBSTRATE=ssh
export SSH_HOST_LIST=/absolute/path/to/host_list
# export SSH_USER=ubuntu       # otherwise your user/SSH configuration applies
# export SSH_HOMEDIR_SHARED=1  # set only if workers share a home directory
```

The host list accepts comma- or whitespace-separated hosts across multiple
lines and ignores comment lines. Authentication must be non-interactive.
Slurm settings are ignored in SSH mode. Filesystem paths must exist on every
selected worker:

```bash
declare -A TEST_DIRS=(["/mnt/fs/scaletest"]=1)
```

### Slurm

```bash
export EXECUTION_SUBSTRATE=slurm
account="your-account"
partition="your-partition"
reservation=""                 # set if your site requires one
```

Configure site modules, `client_type`, and `client_arch` in `env.sh`.
Filesystem `TEST_DIRS` use worker-side paths as above. Set `run_time` for
the complete filesystem sweep or batch, including startup and cleanup.
See [advanced node selection and Slurm options](docs/FILESYSTEM_TESTING.md#advanced-node-selection-and-slurm-options).

### Kubernetes

Authorize `kubectl` for the intended context first. The tool does not
provision a cluster, namespace, PV, or PVC:

```bash
export EXECUTION_SUBSTRATE=kubectl
export KUBECTL_NAMESPACE=storage-scale-test
export KUBECTL_PV=storage-scale-test-pv
export KUBECTL_PVC=storage-scale-test-pvc
export KUBECTL_NODE_SELECTOR='storage-scale-test/worker=true'
export KUBECTL_ELBENCHO_IMAGE=docker.io/breuner/elbencho:v3.2-1
export KUBECTL_IMAGE_PULL_POLICY=IfNotPresent
export KUBECTL_RUN_AS_USER=2000
export KUBECTL_RUN_AS_GROUP=2000
declare -A TEST_DIRS=(["scale-test"]=1)
```

Before validating, confirm:

- The namespace exists and the RWX PVC is bound to the named PV.
- The selector matches enough Ready, schedulable nodes of the intended
  architecture for your largest node count. Inspect labels with
  `kubectl get nodes --show-labels`; comma-separated equality labels are ANDed,
  for example `storage-scale-test/worker=true,storage-tier=lustre`.
- Every selected node can mount the PVC. Each logical `TEST_DIRS` path already
  exists and is writable by the configured positive UID/GID; `2000:2000` is an
  example, not a required identity. Do not prepend `/mnt/storage-scale-test/`:
  the tool adds that Pod-side prefix. The PVC mount root need not be writable.
- The CNI provides cross-node Pod IPv4 connectivity on TCP 1611 and enforces
  NetworkPolicies.
- Admission policy allows the Elbencho Pods' `Unconfined` seccomp profile,
  needed for Linux AIO.
- Your identity has the [required resource permissions](docs/FILESYSTEM_TESTING.md#kubernetes-access-and-troubleshooting),
  including `get`, `create`, and `delete` on namespaced Leases.
- The image supports your nodes and is usable under the pull policy with any
  required registry credentials. `Never` requires preloading; `Always`
  requires a digest-qualified reference.

Validation creates a temporary Job to check the image, workload identity,
PVC access, and seccomp mode. For lifecycle commands, durable storage layout,
and troubleshooting, see the [filesystem guide](docs/FILESYSTEM_TESTING.md#kubernetes-run-inspect-and-collect).

## Validate and correct the configuration

Run `./validate_env.sh` after configuration changes. It checks the selected
substrate and enabled workloads: nonempty `TEST_DIRS` enables filesystem
checks; a configured `OBJ_BUCKET` enables object checks.

Validation performs access probes, not just syntax checks. It checks writable
filesystem paths and object access, rejects a nonempty test bucket, and
creates temporary Kubernetes validation resources when applicable. For
Kubernetes it also lists existing objects from this toolset, informationally
(see the
[troubleshooting notes](docs/FILESYSTEM_TESTING.md#kubernetes-access-and-troubleshooting)).
Read detailed errors and diagnostic paths above the final summary.
Disabled-test notes are not failures.

Fix configuration errors in `env.sh`. For mount, admission, registry, or CNI
failures, use the reported evidence with your cluster/storage administrator.
Repeat until it prints “All validation checks passed successfully.”

Validation cannot prove every workload or performance limit. Start with a
small run before scaling.

## Further reading

- [Filesystem benchmark recipes](BENCHMARK_RECIPES_FILESYSTEM.md): tuned settings by use case.
- [Design](docs/DESIGN.md), [requirements](docs/REQUIREMENTS.md), and [architecture diagrams](docs/ARCHITECTURE_DIAGRAMS.md).
- [Kubernetes lifecycle contract](docs/KUBERNETES_ELBENCHO_LIFECYCLE.md): authoritative recovery and fault boundaries.
- [Roadmap](ROADMAP.md), [security policy](SECURITY.md), [LICENSE](LICENSE), and [NOTICE](NOTICE).

External code contributions are not currently accepted. Bug reports,
documentation corrections, and suggestions are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Copyright

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

This project will download and install additional third-party open source software projects. Review the license terms of these open source projects before use.
