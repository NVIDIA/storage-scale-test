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

# Changelog

Notable changes in each release. Versions follow [Semantic Versioning](https://semver.org/);
see [docs/VERSIONING.md](docs/VERSIONING.md). Each release's notes are its
section below.

## [Unreleased]

Planned as v2.0.0; contains breaking changes.

### Breaking

- On Kubernetes, every `TEST_DIRS` key must name its claim:
  `[namespace/]claim:path`. `KUBECTL_PVC` and `KUBECTL_PV` are removed.
- Collect any in-progress v1.x Kubernetes attempt with v1.8.0 first.
- `--env-override` may no longer set `TEST_DIRS`, `STORAGE_TARGET_NAME`, or
  `FS_MAX_*`; use `--target`.
- `extract-*.py` result tools refuse inputs from different storage targets
  unless given `--allow-mixed-targets`.
- Each claim is mounted in the Pod at `/mnt/storage-scale-test/<claim>`.

Migrating a Kubernetes configuration:

```bash
# v1.8.0: KUBECTL_PVC=scale-test  TEST_DIRS=(["data"]=1)
# v2.0.0: TEST_DIRS=(["scale-test:data"]=1)
# a file that sets TEST_DIRS: --env-override fs-a.env  becomes  --target fs-a.env
```

See [Upgrading from v1.8.0](docs/FILESYSTEM_TESTING.md#upgrading-from-v180).

### Added

- Spread load across several filesystems with weighted `TEST_DIRS` roots, now
  also across Kubernetes claims
  ([#16](https://github.com/NVIDIA/storage-scale-test/issues/16)). See
  [Testing several filesystems](docs/FILESYSTEM_TESTING.md#testing-several-filesystems).
- Compare filesystems in one batch with one `--target` file each. See
  [Storage target reference](docs/FILESYSTEM_TESTING.md#storage-target-reference).
- Run one Kubernetes batch across several namespaces via an in-cluster
  sequencer (pinned default image; `validate_env.sh` warns if it is unusable and
  `--sequencer` runs the full probe). See
  [Several Kubernetes namespaces](docs/FILESYSTEM_TESTING.md#several-kubernetes-namespaces)
  and [Kubernetes lifecycle](docs/KUBERNETES_ELBENCHO_LIFECYCLE.md#segments-and-the-sequencer).
- Reports name their storage target and refuse to mix targets. See
  [Reporting per target](docs/FILESYSTEM_TESTING.md#reporting-per-target).
- Repeat `--env-override` to stack workload files. See
  [Workload override files](docs/FILESYSTEM_TESTING.md#workload-override-files).

### Changed

- Snapshots also record target and override files and, on Kubernetes, the
  namespace and Pod paths instead of `KUBECTL_PVC`/`KUBECTL_PV`; older
  snapshots still load.
- CSV exports gain a `storage_target_name` column; older CSVs still load, but
  older checkouts cannot read the new column.
- `KUBECTL_ELBENCHO_IMAGE` defaults to the pinned elbencho image when unset.
- `validate_env.sh` is faster, especially on busy Slurm clusters, and prints a
  short summary unless `--verbose`. Errors name the node and the likely cause
  (such as a filesystem that cannot mount or is not writable).

## [1.8.0] - 2026-10-09

### Added

- Kubernetes (`EXECUTION_SUBSTRATE=kubectl`) runs filesystem IO and
  metadata sweeps alongside SSH and Slurm. Sweeps run in the cluster; use
  `--status`, `--collect`, `--cancel`, and `--resume` to follow them. See
  [Kubernetes](README.md#kubernetes).
- Prepared filesystem batches: build a set of IO and metadata sweeps with
  `--batch` and `--append`, run them together with `--start`, and report
  them with `utils/extract-filesystem.py`, which also saves one combined
  `reports.txt`. See
  [Prepared filesystem batches](docs/FILESYSTEM_TESTING.md#prepared-filesystem-batches).
- Workload override files: `--env-override FILE` changes selected workload
  settings for one run or batch group without editing `env.sh`. See
  [Workload override files](docs/FILESYSTEM_TESTING.md#workload-override-files).
- Metadata sweeps can be resumed with `--resume`, like IO sweeps. See
  [Metadata sweeps](docs/FILESYSTEM_TESTING.md#metadata-sweeps).
- Completion-based IO sweeps: buffered IO, multiple `TEST_DIRS` roots, or
  `--run-to-completion` process a finite dataset once instead of running
  for a fixed time. See [First IO sweep](docs/FILESYSTEM_TESTING.md#first-io-sweep).
- Project versions: commands accept `--version`, results record the
  version that produced them, and reports list it. See
  [docs/VERSIONING.md](docs/VERSIONING.md).
- Deployment tarballs and release source archives carry their version
  and detect later edits.

### Changed

- Result tools are executable Python (`utils/extract-*.py`,
  `utils/summarize-elbencho.py`) that set up `.venv` on first use; the
  `.sh` wrappers are gone, so update scripts that call them.
- elbencho v3.2-1 is the pinned download and default Kubernetes image.
- Deployment tarballs leave out object storage credentials. See
  [Prepare the tools](docs/OBJECT_STORAGE_TESTING.md#prepare-the-tools).
