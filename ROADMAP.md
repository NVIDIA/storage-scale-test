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

# Roadmap Items (last updated 2026-10-08)

## Completed

- Add kubectl as the filesystem Elbencho execution substrate. It uses an
  existing authorized cluster, namespace, and bound RWX PVC; submits a
  durable asynchronous sweep, and supports status, cancel, collect, and
  collection-gated resume.
- Add `--env-override` workload files to the filesystem launchers.
- Add prepared filesystem batches (`--batch`, `--append`, `--start`) that
  accumulate sweep executions and run them from one resumable batch.
- Add completion-based (finite dataset) filesystem sweeps
  (`--run-to-completion`).
- Make metadata (mdtest) sweeps resumable on SSH, Kubernetes, and Slurm.
- Shard filesystem integration CI by substrate.

## P0

- Integrate with AI Cloud Validation
(https://github.com/NVIDIA/ai-cloud-validation).
    - Exercise the implemented kubectl substrate in the validation suite.
    - Add sweep across multiple storage targets.
    - Drive prepared batches (`--batch`/`--append`/`--start`) from the
      validation suite's orchestrator.
    - Watch out for (current) 10m AI Cloud Validation test timeout--figure
      out the appropriate enhancement for that test suite to allow storage
      tests that may take some time to complete.
- High-scale timing-workers to facilitate at-scale resilience testing.

## P1

- Many-uid high-scale metadata load driving (probe for LDAP integration
  scaling issues on NFS systems).
- Add storage fault-injection tests to AI Cloud Validation.

## P2

- Improve scalability of SSH execution mode.
- Expand unit and regression tests. Audit test coverage and improve it.
- Reduce the differences between filesystem and object testing execution
  & reporting.
- Block storage (without filesystems) if necessary
- Add support for GDS?
