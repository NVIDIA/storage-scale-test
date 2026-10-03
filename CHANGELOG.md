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

- Project versions. User-facing commands accept `--version`.
- Each benchmark result records the version that produced it. Reports list
  those versions and the reporter's own version.
- Deployment tarballs and release source archives carry their version and
  detect later edits.
