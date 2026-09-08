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

# NVIDIA Storage Scale Test — Architecture

## Product Architectural Specification

NVIDIA Storage Scale Test is a benchmark orchestration tool packaged by the user as a single tarball for transfer into a test environment. This repository does not distribute pre-built benchmark binaries; users are responsible for obtaining, building, validating, and complying with license/security requirements for the binaries they include. The architecture has four components:

1. **Orchestration layer.** Shell scripts on an executing host (login node or workstation) drive benchmark execution. A single configuration file (`env.sh`) parameterizes all behavior. The orchestration layer supports two mutually exclusive dispatch mechanisms:
   - **Slurm:** Jobs are submitted via `sbatch` to a Slurm controller, which allocates compute nodes and executes benchmark workloads within job allocations.
   - **SSH:** Benchmark code is transmitted as self-contained scriptlets to remote nodes over passwordless SSH. Results are streamed back through SSH, normally as tar archives.

2. **Benchmark clients.** One or more nodes run required benchmark and connectivity binaries (for example elbencho, Warp, and s3test) that were supplied by the user or produced by helper build scripts before the tarball was taken into the test environment. These nodes issue IO against the systems under test and write result files to local or shared storage.

3. **Systems under test.** The targets of benchmark IO:
   - POSIX filesystems mounted on the benchmark clients.
   - S3-compatible object storage endpoints accessed over HTTPS.
   - Network paths between benchmark clients (TCP throughput/latency).

4. **Analysis tools.** Python scripts (invoked through shell wrappers that auto-manage a virtualenv) parse result files and produce terminal tables, PNG plots, and optional Markdown reports.

The tool has no always-running daemon or server-side control plane. It does
intentionally persist run artifacts under `RESULTS_DIR`, including result data,
configuration snapshots, and reified filesystem-sweep executions with their
status sentinels. Optional treefile caches are stored alongside staged datasets.
Together these artifacts support report regeneration, dataset reuse, and
`nv-elbencho-sweep.sh --resume` after an interrupted run.

No component requires an inbound connection from outside the cluster or other
operator-controlled benchmark environment. Within that boundary, benchmark
clients receive SSH connections from the executing host, and elbencho/Warp
benchmark services open temporary listeners for their coordinators and peer
clients.
Outbound connections to configured services such as an S3-compatible endpoint
may cross the cluster boundary. Infrastructure administrators can enforce this
model with network access policies and firewalls that deny outside-initiated
connections while permitting the required internal benchmark traffic and
explicitly configured outbound traffic.

The diagrams below illustrate the two execution modes.

---

## SSH Mode (Passwordless SSH Cluster)

```mermaid
%%{init: {"flowchart": {"curve": "linear"}}}%%
flowchart TB
  %% SSH Mode: orchestration on an executing host, remote execution over SSH, results copied back.

  subgraph OP["Operator Environment"]
    U["Operator (human)"]
  end

  subgraph EX["Executing Host"]
    T["storage-scale-test tarball<br/>(shell scripts + user-supplied/helper-built binaries)"]
    ORCH["Orchestrator scripts<br/>(storage-tests/*/nv-*.sh)"]
    SSHRUN["SSH: run remote scriptlets<br/>(ssh)"]
    ENV["Configuration<br/>(env.sh)"]
    COPYBACK["stream results back<br/>(tar archive over SSH)"]
    RESX["Results directory on executing host<br/>($RESULTS_DIR/...)"]
    STATE["Persistent run artifacts<br/>(results, env snapshots,<br/>execution status)"]
    PARSE["read/parse results<br/>(local file I/O)"]
    ANA["Analysis tools (optional)<br/>(utils/extract-*.sh -> Python venv -> extract-*.py)"]
  end

  subgraph CL["Benchmark Clients"]
    CLIENTS["Benchmark clients (N nodes)<br/>required binaries: elbencho/warp/s3test"]
    WRITEOUT["write benchmark outputs<br/>(remote coordinator filesystem)"]
    RESN["Remote execution results<br/>(coordinator-side output directory)"]
  end

  subgraph SUT["Systems Under Test"]
    FS["Filesystem Under Test<br/>(POSIX mount paths in TEST_DIRS;<br/>optional staged treefile cache)"]
    S3["S3-Compatible Object Storage Endpoint<br/>(OBJ_HOST/OBJ_REGION/OBJ_BUCKET)"]
  end

  COPY["copy prepared tarball"]
  U --> COPY --> T
  ENV --> ORCH
  T --> ORCH

  ORCH --> SSHRUN --> CLIENTS

  POSIXIO["POSIX I/O<br/>(read/write/metadata)"]
  CLIENTS --> POSIXIO --> FS

  S3API["S3 API (PUT/GET/DELETE)<br/>(HTTPS)"]
  CLIENTS --> S3API --> S3

  CLIENTS --> WRITEOUT --> RESN

  RESN --> COPYBACK --> RESX --> STATE

  RESX --> PARSE --> ANA
```

## Slurm Mode (Login Node + Slurm Controller + Compute Nodes)

```mermaid
%%{init: {"flowchart": {"curve": "linear"}}}%%
flowchart TB
  %% Slurm Mode: orchestration from a login node, jobs dispatched via Slurm.
  %% Note: Many Slurm environments have a shared homedir (e.g., NFS $HOME) distinct from the filesystem under test.

  subgraph OP["Operator Environment"]
    U["Operator (human)"]
  end

  subgraph LN["Slurm Login Node"]
    ORCH["Orchestrator scripts<br/>(storage-tests/\*/nv-\*.sh)"]
    SBATCH["submit jobs<br/>(sbatch)"]
    ANA["Analysis tools (optional)<br/>(utils/extract-\*.sh -> Python venv -> extract-\*.py)"]
  end

  subgraph HOME["Shared Home Directory"]
    CODE["storage-scale-test directory<br/>(tarball unpacked; scripts + user-supplied/helper-built binaries)"]
    ENV["Configuration<br/>(env.sh)"]
    RES["Results directory<br/>($RESULTS_DIR/...)"]
    STATE["Persistent run artifacts<br/>(results, env snapshots,<br/>execution status)"]
  end

  subgraph SL["Slurm Control Plane"]
    SC["Slurm Controller / Scheduler<br/>(slurmctld + slurmdbd optional)"]
  end

  subgraph CN["Slurm Compute Nodes"]
    READCODE["read scripts/binaries<br/>(shared homedir filesystem I/O)"]
    C1["Compute node(s)<br/>required binaries: elbencho/warp/s3test"]
    WRITEOUT["write benchmark outputs<br/>(shared homedir filesystem I/O)"]
  end

  subgraph ST["Filesystem &nbsp;Under Test"]
    FS["Filesystem Under Test<br/>(POSIX mount paths in TEST_DIRS;<br/>optional staged treefile cache)"]
  end

  subgraph SUT["External Service (optional)"]
    S3["S3-Compatible Object Storage Endpoint<br/>(OBJ_HOST/OBJ_REGION/OBJ_BUCKET)"]
  end

  COPY["copy prepared tarball"]
  U --> COPY --> CODE
  CODE --> ORCH
  ENV --> ORCH

  CODE --> READCODE --> C1
  SRUN["dispatch allocation + start job steps<br/>(srun within allocation)"]
  ORCH --> SBATCH --> SC
  SC --> SRUN --> READCODE

  POSIXIO["POSIX I/O<br/>(read/write/metadata)"]
  S3API["S3 API (PUT/GET/DELETE)<br/>(HTTPS)"]
  C1 --> POSIXIO --> FS
  C1 --> S3API --> S3

  C1 --> WRITEOUT --> RES --> STATE

  RES --> ANA
```
