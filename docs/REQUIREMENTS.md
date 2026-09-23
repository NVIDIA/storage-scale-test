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

# NVIDIA Storage Scale Test — Requirements

**Last Updated**: 2026-09-23\
**Status**: Accurate as of last update.

---

## Table of Contents

1. [Use-Case Requirements](#1-use-case-requirements)
2. [Execution Environment Requirements](#2-execution-environment-requirements)
3. [Configuration & Validation Requirements](#3-configuration--validation-requirements)
4. [Benchmarking Requirements](#4-benchmarking-requirements)
5. [Reporting & Analysis Requirements](#5-reporting--analysis-requirements)
6. [Packaging & Deployment Requirements](#6-packaging--deployment-requirements)

---

## 1. Use-Case Requirements

These requirements are driven by the primary use cases of an **NVIDIA Storage Scale Test runner** operating on cloud or HPC infrastructure.

### UC-1: Multi-Node Network Scale

> *As an NVIDIA Storage Scale Test runner, I need to verify the network throughput between storage test clients (network filesystem and object storage both traverse over and depend on a well-functioning network) with various thread counts. In the general case, the test executor does not have access to the backend storage servers, so network testing is limited to client-to-client traffic.*

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| UC-1.1 | The tool shall measure TCP network throughput (Gbps) between storage test clients. | Yes |
| UC-1.2 | The tool shall measure TCP network latency (ms, with percentile distribution) between storage test clients. | Yes |
| UC-1.3 | The tool shall support sweeping across multiple thread counts for network benchmarks. | Yes |
| UC-1.4 | The tool shall support sweeping across multiple node counts for network benchmarks. | Yes |
| UC-1.5 | The tool shall support unidirectional (half-and-half) network testing where clients are split into two groups, testing both directions sequentially. | Yes |
| UC-1.6 | The tool shall support bidirectional (full-duplex) network testing where both directions are tested simultaneously. | Yes |
| UC-1.7 | The tool shall normalize per-thread transfer sizes to keep runtime approximately constant as thread counts vary. | Yes |
| UC-1.8 | The tool shall shuffle group assignments between iterations to avoid systematic bias. | Yes |

### UC-2: Single-Node Filesystem Scale

> *As an NVIDIA Storage Scale Test runner, I need to characterize a single node's scaling against a filesystem in terms of throughput and latency with various IO sizes, thread counts, and IO depth values.*

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| UC-2.1 | The tool shall benchmark a single node's filesystem read and write throughput. | Yes |
| UC-2.2 | The tool shall benchmark a single node's filesystem read and write latency. | Yes |
| UC-2.3 | The tool shall sweep across configurable IO sizes (e.g., 4K, 16K, 64K, 1M). | Yes |
| UC-2.4 | The tool shall sweep across configurable thread counts (e.g., 1, 2, 4, ..., 256). | Yes |
| UC-2.5 | The tool shall sweep across configurable IO depth values. | Yes |
| UC-2.6 | The tool shall support both sequential and random IO patterns, configurable per IO size. | Yes |
| UC-2.7 | The tool shall support both direct IO and buffered IO modes. | Yes |
| UC-2.8 | The tool shall support asymmetric write/read configurations (e.g., sequential 1M writes with random 4K reads in the same run). | Yes |
| UC-2.9 | Filesystem IO phase termination shall match the selected workload: legacy generated and staged-read modes support a configurable time limit, while generated shared-directory and generated single-shared-file modes process a finite configured dataset. | Yes |

### UC-3: Single-Node Filesystem Metadata

> *As an NVIDIA Storage Scale Test runner, I need to characterize a single node's scaling against a filesystem in terms of metadata ops per second and latency with various thread count values.*

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| UC-3.1 | The tool shall benchmark metadata operations (create, stat, delete) on a single node. | Yes |
| UC-3.2 | The tool shall sweep across configurable thread (task) counts for metadata benchmarks. | Yes |
| UC-3.3 | The tool shall report metadata operation rates (ops/sec) with statistical aggregation across iterations (mean, stddev, min, max). | Yes |
| UC-3.4 | The tool shall report metadata operation latency percentiles (p0, p50, p90, p99, p100). | Yes |
| UC-3.5 | The tool shall run multiple iterations per configuration and aggregate results statistically. | Yes |

### UC-4: Single-Node Object Scale

> *As an NVIDIA Storage Scale Test runner, I need to characterize a single node's scaling against an object storage system in terms of throughput and latency with various object sizes and thread count values.*

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| UC-4.1 | The tool shall benchmark a single node's object storage PUT (write) and GET (read) throughput. | Yes |
| UC-4.2 | The tool shall benchmark a single node's object storage GET TTFB (Time To First Byte) latency. | Yes |
| UC-4.3 | The tool shall sweep across configurable object sizes (e.g., 4MiB, 8MiB, ..., 64MiB). | Yes |
| UC-4.4 | The tool shall sweep across configurable thread counts. | Yes |
| UC-4.5 | The tool shall target any S3-compatible object storage system. | Yes |
| UC-4.6 | The tool shall support ranged read testing (reading byte-range subsets of larger objects). | Yes |
| UC-4.7 | The tool shall support multipart uploads. | Yes |

### UC-5: Multi-Node Filesystem Scale

> *As an NVIDIA Storage Scale Test runner, I need to characterize many nodes' scaling against a filesystem in terms of throughput and latency with various IO size, node count, thread count, and IO depth values.*

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| UC-5.1 | The tool shall benchmark filesystem throughput across varying numbers of nodes. | Yes |
| UC-5.2 | The tool shall support flexible node count specifications (single values, ranges with increments, comma-separated lists). | Yes |
| UC-5.3 | The filesystem IO sweep shall reify every `(node count, IO size, thread count, IO depth)` cell before dispatch. Slurm mode shall execute pending cells sequentially in one coordinator allocation sized to the largest pending node count; SSH mode shall dispatch them sequentially with fresh host selection for each cell. | Yes |
| UC-5.4 | The tool shall support testing against one or more filesystems simultaneously, with configurable weight-based load balancing across them. | Yes |
| UC-5.5 | The tool shall support a generated shared-directory many-file workload with a configurable file count per node and optional exact per-file size. | Yes |
| UC-5.6 | The tool shall support generated and pre-existing single-shared-file workloads, including partitioned access and an option for every participating node to access the full file. | Yes |
| UC-5.7 | The filesystem IO sweep shall support retaining generated datasets, repeatedly reading staged datasets, safely deleting retained datasets, and resuming incomplete sweep executions from persisted state. | Yes |

### UC-6: Multi-Node Filesystem Metadata

> *As an NVIDIA Storage Scale Test runner, I need to characterize many nodes' scaling against a filesystem in terms of metadata ops per second and latency with various node count and thread count values.*

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| UC-6.1 | The tool shall benchmark metadata operation rates across multiple nodes. | Yes |
| UC-6.2 | The tool shall perform a double parameter sweep over node counts and tasks-per-node. | Yes |
| UC-6.3 | The tool shall rotate host assignments between benchmark phases (create, stat, delete) so that stat and delete operations target files created by different nodes. | Yes |
| UC-6.4 | The tool shall pre-create directory structures to distribute metadata load across filesystem metadata servers (if applicable). | Yes |
| UC-6.5 | The tool shall support a dense metadata workload in which all workers create, stat, and delete uniquely named files in one shared flat directory, with the achievable file total reported when worker quantization differs from the requested target. | Yes |

### UC-7: Multi-Node Object Scale

> *As an NVIDIA Storage Scale Test runner, I need to characterize many nodes' scaling against an object storage system in terms of throughput and latency with various object size, node count, and thread count values.*

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| UC-7.1 | The tool shall benchmark object storage throughput and latency across varying numbers of nodes. | Yes |
| UC-7.2 | The tool shall support multipart uploads via a command-line flag. | Yes |
| UC-7.3 | The tool shall support range-read testing via a command-line flag. | Yes |
| UC-7.4 | The tool shall support configurable static S3 key prefixes (warp defaults to random per-thread prefixes) to explore how prefix layout affects performance. | Yes |
| UC-7.5 | The tool shall support configurable RPS (requests per second) budget limits for PUT and GET, to respect object storage rate limits. | Yes |

### UC-8: Reporting

> *As an NVIDIA Storage Scale Test runner, I need to generate a textual or Markdown report and associated PNG plots from any prior scale test run's results.*

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| UC-8.1 | The tool shall generate textual summary tables formatted for a fixed-width font on the terminal. | Yes |
| UC-8.2 | The tool shall generate PNG plot images for throughput, latency, and scaling metrics. | Yes |
| UC-8.3 | The tool shall support Markdown report output for elbencho filesystem results. | Yes |
| UC-8.4 | The tool shall support Markdown report output for mdtest-elbencho metadata results. | Yes |
| UC-8.5 | The tool shall support Markdown report output for netbench network results. | Yes |
| UC-8.7 | The tool shall support re-analysis from cached/serialized intermediate data (CSV or JSON), avoiding re-parsing raw benchmark output. | Yes |
| UC-8.8 | The tool shall support filtering reports by node count, thread count, IO size, IO depth, or object size. | Yes |
| UC-8.9 | Reporting shall clearly separate and label write vs. read results, sequential vs. random IO, and direct IO vs. buffered IO in all tables and plots. | Yes |
| UC-8.10 | Plot colors shall use colorblind-accessible palettes. | Yes |

### UC-9: Minimal Environment

> *As an NVIDIA Storage Scale Test runner, I may only have access to very basic infrastructure: a set of benchmark hosts I can SSH into. I still need to be able to run all scale tests on this infrastructure.*

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| UC-9.1 | The tool shall support execution on a set of hosts reachable via passwordless SSH, with no job scheduler required. | Yes |
| UC-9.2 | The tool shall support execution on Slurm-managed clusters as an alternative. | Yes |
| UC-9.3 | All benchmark orchestration scripts shall support both SSH and Slurm modes from a single entry point. | Yes |
| UC-9.4 | The SSH mode shall transmit all necessary code to remote nodes as self-contained scriptlets (no requirement for remote nodes to have the repository). | Yes |
| UC-9.5 | The tool shall support environments where remote nodes share a home directory (e.g., NFS) and environments where they do not. | Yes |
| UC-9.6 | Filesystem sweeps shall support an explicitly selected Kubernetes substrate using an already authorized cluster, a pre-existing bound PVC, and a configured node selector. | Yes |
| UC-9.7 | Kubernetes sweeps shall submit one asynchronous attempt for the whole sweep and shall support durable status, cancellation, collection, and collection-gated resume operations. | Yes |

---

## 2. Execution Environment Requirements

These requirements address the infrastructure and runtime constraints the tool must accommodate.

### EE-1: Non-Root Execution

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| EE-1.1 | All scale tests shall be runnable as a non-root user. No operation shall require root privileges or `sudo`. | Yes |

### EE-2: Architecture Support

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| EE-2.1 | The tool shall support x86_64 (amd64) architectures. | Yes |
| EE-2.2 | The tool shall support aarch64 (ARM64) architectures. | Yes |
| EE-2.3 | Architecture detection shall occur at runtime and select the appropriate benchmark binaries automatically. | Yes |
| EE-2.4 | Helper scripts to fetch/build OSS tools attempt to do so for both architectures using the repository's suffix convention. | Yes |

### EE-3: Distributed Benchmark Coordination

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| EE-3.1 | Multi-node benchmarks shall use a coordinator/service pattern: services run on all participating nodes and a coordinator drives the test. Bidirectional netbench may use two coordinators, one per direction. | Yes |
| EE-3.2 | In SSH mode, services and the coordinator shall be started and stopped by the orchestration scripts; the user shall not need to manage them manually. | Yes |
| EE-3.3 | Results produced by an SSH remote coordinator shall be streamed back to the executing host; Slurm results shall be written to the configured shared results location. | Yes |
| EE-3.4 | Kubernetes filesystem sweeps shall run one coordinator Job and one service Pod per selected worker over ordinary Pod networking, with the PVC mounted at the fixed Pod path `/mnt/storage-scale-test`. | Yes |

### EE-4: Python Environment

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| EE-4.1 | Python analysis scripts shall be invoked through shell wrappers that automatically create and manage a Python virtual environment. | Yes |
| EE-4.2 | The virtual environment shall be cached to avoid re-creating it on every invocation. | Yes |
| EE-4.3 | Required Python dependencies (matplotlib, numpy, etc.) shall be installed automatically by the shell wrapper. | Yes |

## 3. Configuration & Validation Requirements

### CV-1: Central Configuration

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| CV-1.1 | All test parameters shall be centrally configured in a single configuration file (`env.sh`). | Yes |
| CV-1.2 | A template configuration file (`env.sh.template`) shall be provided with documented defaults and comments explaining every parameter. | Yes |
| CV-1.3 | The configuration file shall support Slurm-specific settings (account, reservation, partition, timeout). | Yes |
| CV-1.4 | The configuration file shall support SSH-specific settings (host list file, SSH user, shared homedir flag). | Yes |
| CV-1.5 | Benchmark parameters shall support configurable sweep lists (IO sizes, thread counts, IO depths, and object sizes), while node and task counts shall be supplied through the entry-point CLI. | Yes |
| CV-1.6 | The tool shall support backward compatibility when new configuration variables are introduced (fall back to old variable names or hardcoded defaults). | Yes |
| CV-1.7 | The configuration shall support Kubernetes namespace, PV, PVC, node-selector, benchmark-image, image-pull-policy, and workload UID/GID settings when `EXECUTION_SUBSTRATE=kubectl`. | Yes |

### CV-2: Configuration Validation

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| CV-2.1 | A validation script (`validate_env.sh`) shall check the configuration and report all errors before any benchmarks are run. | Yes |
| CV-2.2 | Validation shall verify that required directories (results, logs) exist. | Yes |
| CV-2.3 | Validation shall verify that benchmark binaries are present, executable, and compiled for the correct architecture. | Yes |
| CV-2.4 | Validation shall verify Slurm connectivity (if Slurm mode is enabled): partition access, account, reservation, sbatch and srun functionality. | Yes |
| CV-2.5 | Validation shall verify SSH connectivity (if SSH mode is enabled): ability to run commands and scriptlets on remote hosts. | Yes |
| CV-2.6 | Validation shall verify that filesystem test paths reside on a filesystem device distinct from `/` and are writable on compute nodes. | Yes |
| CV-2.7 | Validation shall verify S3 object storage credentials and bucket accessibility (if object tests are enabled). | Yes |
| CV-2.8 | Validation shall warn if the target S3 bucket contains existing objects (warp deletes all objects). | Yes |
| CV-2.9 | Validation shall validate elbencho configuration parameters (thread list contains integers, IO sizes are valid, duration is valid). | Yes |
| CV-2.10 | Validation shall accumulate all errors and report them together at the end, rather than stopping at the first error. | Yes |
| CV-2.11 | Kubernetes validation shall verify API access, namespace and PV/PVC identity, PVC binding and mount usability, selected Ready-node compatibility, and benchmark runtime prerequisites. Submission shall verify requested capacity, service readiness, and coordinator connectivity before executing a cell. | Yes |

### CV-3: Slurm Advanced Configuration

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| CV-3.1 | The tool shall support an optional job name prefix for all Slurm jobs, to help identify jobs in shared clusters. | Yes |
| CV-3.2 | The tool shall support additional arbitrary sbatch arguments via a configuration array (`SLURM_EXTRA_ARGS`). | Yes |
| CV-3.3 | The tool shall support a node ignore/exclude list for Slurm jobs. | Yes |

---

## 4. Benchmarking Requirements

### BM-1: Filesystem IO Benchmarks

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| BM-1.1 | The default single-target worker-directory write and read phases and staged many-file reads shall honor a configurable time limit. The legacy computed-count or multiple-target branch shall complete its derived write file count while retaining the read time limit. Direct-IO reads may repeat until their limit; buffered reads perform at most one pass and may finish earlier. | Yes |
| BM-1.2 | Results shall be written to dated output directories with a consistent naming convention. | Yes |
| BM-1.3 | The tool shall support a configurable pause between write and read phases. | Yes |
| BM-1.4 | Generated shared-directory data and distributed file-removal phases shall run without a time limit. The harness shall verify exact completed file and byte counts for data phases and exact completed file counts for removal before advancing. | Yes |
| BM-1.5 | Staged many-file reads shall derive file and byte totals from the scanned tree, independently of the current reader-node topology, and may reuse an explicitly documented cached treefile. | Yes |
| BM-1.6 | Single-shared-file workloads shall be sequential-only. Direct-IO reads of a pre-existing file may repeat until the configured time limit; buffered reads shall make at most one logical pass and may finish before that limit. | Yes |
| BM-1.7 | Generated single-shared-file write and read phases shall process the explicitly configured finite file extent without a time limit. | Yes |

### BM-2: Filesystem Metadata Benchmarks

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| BM-2.1 | Metadata benchmarks shall test create, stat, and delete operations as separate phases. | Yes |
| BM-2.2 | Host assignments shall be rotated between phases to avoid client-side cache effects. | Yes |
| BM-2.3 | Multiple iterations shall be supported with per-iteration output files. | Yes |

### BM-3: Object Storage Benchmarks

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| BM-3.1 | Each run shall consist of a PUT phase followed by a GET phase. | Yes |
| BM-3.2 | PUT and GET phases shall have independently configurable durations. | Yes |
| BM-3.3 | A minimum number of files per client shall be configurable for the PUT phase. | Yes |
| BM-3.4 | The tool shall clean up (delete) objects from the bucket between test configurations. | Yes |
| BM-3.5 | The tool shall support multipart uploads. | Yes |
| BM-3.6 | The tool shall support range reads with a configurable base object size. | Yes |

### BM-4: Network Benchmarks

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| BM-4.1 | Block size (bytes sent per request) and response size (bytes returned per response) shall be configurable. | Yes |
| BM-4.2 | A minimum of 2 nodes shall be required for all network benchmarks. | Yes |
| BM-4.3 | The tool shall support multiple iterations with node group shuffling between iterations. | Yes |

### BM-5: Parameterized Sweep Framework

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| BM-5.1 | All benchmark scripts shall support multi-dimensional parameter sweeps (combinations of node count, thread count, IO size, IO depth, object size, etc.). | Yes |
| BM-5.2 | Node count and task count specifications shall support flexible formats: single values, comma-separated lists, ranges with optional increment (`X-Y+Z`). | Yes |
| BM-5.3 | Flexible specifications shall support arbitrary ordering, including descending sequences. | Yes |
| BM-5.4 | Filesystem IO, filesystem metadata, and network sweep entry points shall require explicit `--nodes` (and `--tasks` where applicable). The Warp sweep shall retain its documented legacy positional node-count syntax. | Yes |
| BM-5.5 | The Warp sweep shall produce a clear error when `--nodes` is mixed with its legacy positional arguments. Other sweep entry points shall reject unexpected positional arguments. | Yes |
| BM-5.6 | All sweep scripts shall provide a usage message accessible via `-h` or `--help`. | Yes |
| BM-5.7 | The filesystem sweep shall support Kubernetes submission, status, cancellation, collection, and collection-gated resume while preserving successful cells and publishing completed results from durable PVC state. | Yes |

---

## 5. Reporting & Analysis Requirements

### RA-1: General Reporting

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| RA-1.1 | Each benchmark type shall have a dedicated analysis/reporting script. | Yes |
| RA-1.2 | Analysis scripts shall be invoked through shell wrappers that manage the Python environment. | Yes |
| RA-1.3 | Terminal output shall include summary tables formatted for fixed-width fonts. | Yes |
| RA-1.4 | Analysis scripts shall support filtering output by relevant dimensions (nodes, threads, sizes, IO depths, modes). | Yes |

### RA-2: Markdown Reports

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| RA-2.1 | An analyzer that supports the `--markdown` flag shall output a Markdown-formatted report to stdout (with progress on stderr), suitable for pasting into Google Docs. | Yes |
| RA-2.2 | Markdown output shall be supported for elbencho filesystem reports. | Yes |
| RA-2.3 | Markdown output shall be supported for mdtest-elbencho metadata reports. | Yes |
| RA-2.4 | Markdown output shall be supported for netbench network reports. | Yes |

### RA-3: Plot Generation

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| RA-3.1 | Plots shall be saved as PNG images in the results or output directory. | Yes |
| RA-3.2 | For filesystem benchmarks: separate throughput (MB/s or GB/s) and IOPS plots shall be generated, grouped by IO size ranges. | Yes |
| RA-3.3 | For filesystem benchmarks: latency plots shall be generated. | Yes |
| RA-3.4 | For filesystem benchmarks: single-node plots shall use thread count on the x-axis; multi-node plots shall use node count. | Yes |
| RA-3.5 | For filesystem benchmarks: an option to generate separate single-axis plots instead of dual y-axis (throughput + latency) shall be provided (`--no-dual-y-axis`). | Yes |
| RA-3.6 | For object storage benchmarks: throughput, TTFB latency, and scaling efficiency plots shall be generated. | Yes |
| RA-3.7 | For object storage benchmarks: TTFB latency histogram plots shall be generated when JSON data with histogram buckets is available. | Yes |
| RA-3.8 | For metadata benchmarks: candlestick rate plots (mean ± stddev, min–max) and scaling efficiency plots shall be generated. | Yes |
| RA-3.9 | For metadata benchmarks: latency histogram plots per operation type (create, stat, delete) shall be generated. | Yes |
| RA-3.10 | For network benchmarks: throughput plots with error bars, scaling efficiency plots, and latency histogram plots shall be generated. | Yes |
| RA-3.11 | Plotters shall use their documented analyzer-specific, colorblind-oriented palette strategy and deterministic color reuse. | Yes |
| RA-3.12 | Plot titles shall include complete context: operation, IO size, IO order (Rand/Seq), IO mode (DirectIO/BufferedIO), and metric. | Yes |

### RA-4: Per-Client Analysis (Object Storage)

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| RA-4.1 | The tool shall support opt-in per-client performance analysis for multi-node warp runs (`--per-client-plots`). | Yes |
| RA-4.2 | Per-client analysis shall identify underperforming clients using z-score based outlier detection with a configurable threshold. | Yes |
| RA-4.3 | Per-client analysis shall generate client throughput distribution histograms, underperformer bar charts, and TTFB box plots. | Yes |
| RA-4.4 | Per-client analysis shall generate time-series throughput plots showing percentile bands and individual outlier traces. | Yes |
| RA-4.5 | Per-client analysis shall generate underperformance heatmaps (clients × time segments). | Yes |
| RA-4.6 | Per-client analysis shall generate cross-run persistent underperformer detection with bar charts and text reports. | Yes |
| RA-4.7 | The minimum number of underperforming segments required to flag a client shall be configurable (`--client-min-underperform-segments`). | Yes |

---

## 6. Packaging & Deployment Requirements

### PD-1: Deployment Tarball

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| PD-1.1 | The tool shall create a single tarball for transfer to a Slurm cluster or SSH client host. | Yes |
| PD-1.2 | The tarball shall include all runtime shell scripts, Python analysis scripts, templates, and any user-provided or helper-built benchmark binaries present at packaging time. | Yes |
| PD-1.3 | The tarball creation process shall not require NVIDIA-distributed pre-built benchmark binaries. | Yes |
| PD-1.4 | A `utils/build_tarball.sh` script shall automate tarball creation, assist with optional binary preparation where possible, and continue with clear warnings when binaries needed for some tests are missing. | Yes |

### PD-2: Binary Preparation Helpers

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| PD-2.1 | `utils/build_tarball.sh` shall reuse existing `utils/elbencho` and `utils/elbencho.aarch64` binaries or attempt to download pinned upstream elbencho static release binaries when needed. | Yes |
| PD-2.2 | `utils/build_tarball.sh` shall not download or bundle NVIDIA-provided Warp binaries; when Warp is missing it shall warn and point users to the Warp source build helper. | Yes |
| PD-2.3 | `utils/build/build_warp_from_source.sh` shall build `utils/warp` and `utils/warp.aarch64` from its default NVIDIA Warp fork/ref when called without arguments, or from a user-selected upstream GitHub/GitLab HTTPS or SSH repository URL plus tag, branch, or commit SHA when both arguments are supplied. | Yes |
| PD-2.4 | The Warp source build helper shall enforce a hard-coded minimum Go version for CVE exposure control, superseding lower upstream build versions. | Yes |
| PD-2.5 | `utils/build_tarball.sh` shall build `utils/s3test` and `utils/s3test.aarch64` from in-tree source when either binary is missing or stale, using local compilers or Docker/Buildx where available. | Yes |
| PD-2.6 | The s3test build helper shall use a current Alpine container for static OpenSSL/zlib builds, suppress debug info, and strip resulting binaries. | Yes |

### PD-3: User Binary Responsibility

This repository does not include or distribute third-party benchmark binaries. Users are responsible for selecting, building, and validating any binaries they include in a prepared tarball.

| ID | Requirement | Satisfied? |
|----|-------------|:----------:|
| PD-3.1 | The project documentation shall state that NVIDIA does not distribute pre-built benchmark binaries from this repository. | Yes |
| PD-3.2 | The project documentation shall state that users are responsible for binary provenance, license compliance, vulnerability scanning, and any required source-offer or SBOM artifacts for tarballs they create. | Yes |
| PD-3.3 | Tarball creation shall continue when optional binary helper steps fail, while warning which test families will not work without the missing binaries. | Yes |
| PD-3.4 | Users shall be able to inspect and replace benchmark binaries in `utils/` before creating the tarball. | Yes |

---

## Appendix A: Requirement Cross-Reference to Components

| Component | Requirements Addressed |
|-----------|----------------------|
| `storage-tests/fs/nv-elbencho-sweep.sh` | UC-2, UC-5, UC-9, BM-1, BM-5 |
| `storage-tests/fs/nv-mdtest-elbencho.sh` | UC-3, UC-6, UC-9, BM-2, BM-5 |
| `storage-tests/object/nv-warp-sweep.sh` | UC-4, UC-7, UC-9, BM-3, BM-5 |
| `storage-tests/network/nv-netbench.sh` | UC-1, UC-9, BM-4, BM-5 |
| `utils/extract-elbencho.py` | UC-8, RA-1, RA-2, RA-3 |
| `utils/extract-warp.py` | UC-8, RA-1, RA-3, RA-4 |
| `utils/extract-mdtest-elbencho.py` | UC-8, RA-1, RA-2, RA-3 |
| `utils/extract-netbench.py` | UC-8, RA-1, RA-2, RA-3 |
| `validate_env.sh` | EE-2, CV-2 |
| `env.sh` / `env.sh.template` | EE-2, CV-1 |
| `lib/env_base.sh` | EE-2, CV-1 |
| `lib/env_functions.sh` | UC-9, EE-3, EE-4, CV-3 |
| `storage-tests/fs/kubectl/` | UC-5, UC-9, EE-3, CV-1, CV-2, BM-1, BM-5 |
| `lib/_elbencho_functions.sh` | BM-1, BM-2, EE-3 |
| `lib/_warp_functions.sh` | BM-3, EE-3 |
| `lib/_netbench_functions.sh` | BM-4, EE-3 |
| `utils/build_tarball.sh` | EE-2, PD-1, PD-2, PD-3 |
| `utils/build/build_warp_from_source.sh` | EE-2, PD-2 |
| `utils/build/build_s3test_from_source.sh` | EE-2, PD-2 |
| README and docs | PD-3 |

## Appendix B: Glossary

| Term | Definition |
|------|-----------|
| **TTFB** | Time To First Byte — latency from request to first byte of response (object storage) |
| **IO depth** | Number of outstanding IO requests per thread |
| **DIO / DirectIO** | Direct IO — bypasses OS page cache |
| **BIO / BufferedIO** | Buffered IO — uses OS page cache |
| **SN** | Single-Node |
| **MN** | Multi-Node |
| **warp** | S3-compatible object storage benchmarking tool by MinIO |
| **elbencho** | Distributed storage benchmarking tool for filesystems and network |
| **s3test** | Simple in-tree S3 connectivity validation tool that can be helper-built from source |
