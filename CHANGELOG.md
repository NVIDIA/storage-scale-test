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

### Added

- Kubernetes (`EXECUTION_SUBSTRATE=kubectl`) runs filesystem IO and
  metadata sweeps alongside SSH and Slurm. Sweeps run in the cluster; use
  `--status`, `--collect`, `--cancel`, and `--resume` to follow them. See
  [Kubernetes](README.md#kubernetes).
- Prepared filesystem batches: build a set of IO and metadata sweeps with
  `--batch` and `--append`, run them together with `--start`, and report
  them with `utils/extract-filesystem.sh`. See
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

- elbencho v3.2-1 is the pinned download and default Kubernetes image.
- Deployment tarballs leave out object storage credentials. See
  [Prepare the tools](docs/OBJECT_STORAGE_TESTING.md#prepare-the-tools).
