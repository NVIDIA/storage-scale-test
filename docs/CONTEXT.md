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

# NVIDIA Storage Scale Test — Repository Context

This document records the non-obvious state and invariants of the repository.
It describes what exists and how it behaves; it is not a changelog or an
implementation journal.

Use the following documents for their narrower authoritative scopes:

- [README.md](../README.md): user setup and operation.
- [docs/REQUIREMENTS.md](REQUIREMENTS.md): normative requirements and status.
- [docs/DESIGN.md](DESIGN.md): detailed design and interfaces.
- [docs/ARCHITECTURE_DIAGRAMS.md](ARCHITECTURE_DIAGRAMS.md): deployment and
  network relationships.
- [docs/CODING_STANDARDS.md](CODING_STANDARDS.md): coding and validation rules.
- [SECURITY.md](../SECURITY.md): vulnerability reporting and public security
  policy.
- [ROADMAP.md](../ROADMAP.md): incomplete and proposed work.

## Project boundary

NVIDIA Storage Scale Test is Bash and Python tooling for distributed storage
benchmarks. It covers:

- filesystem data IO with elbencho;
- filesystem metadata create/stat/delete with elbencho;
- S3-compatible object storage with Warp and an in-tree s3test connectivity
  utility; and
- storage-network traffic with elbencho netbench.

The lowercase repository, package, tarball, and directory identifier is
`storage-scale-test`. Shell configuration uses `SCALE_TEST_BASE` as the
repository or extracted deployment root.

Elbencho and Warp run as separate third-party processes. Their source and
binaries are not vendored in the repository. The repository publishes source
only: NVIDIA and the project do not publish or deliver benchmark binaries or
prepared deployment tarballs. Users may create a deployment tarball locally
and are responsible for every binary they place in it.

Slurm is the default execution substrate; `SSH_HOST_LIST` selects passwordless
SSH. Kubernetes benchmark execution is not implemented. The `integration-tests/`
fixture provisions three kind nodes, RWX storage, two SSH workers, and Slinky
Slurm. Its `nfs` backend uses loop-backed NFSv4 and NFS CSI; `sbx-shared` uses
static volumes over a repository-shared path. Both test the same storage contract.

One budget drives PVC capacity and the growable 4 GiB NFS image. Setup publishes
image tags transactionally, grows retained filesystems, checks fixture and Docker
backing capacity, and reconciles eight NFS workers. Teardown restores recorded
NFS active, enabled, and worker-count states. SBX pins kind 0.30 and
Kubernetes/kubectl 1.34; setup replaces clusters whose kubelets do not match the
node-image profile.

Lifecycle actions run as an ordinary user, store state under
`tmp/integration-state`, and use `sudo` only for NFS host operations. Ownership
markers protect dedicated state and export leaves. A symlink-safe root-owned
global lock and owner record protect fixed NFS configuration. Cached upstream
images must match their pinned digest and runner architecture. NFS CSI misses try
`registry.k8s.io` and `gcr.io/k8s-staging-sig-storage`; its chart tags exist only
inside kind. Kind node and other image misses use bounded host-Docker retries.
Interrupted private aliases are reconciled; MariaDB and Slinky's Alpine helpers
use preloaded fixture-private tags. Cleanup recovers partial bootstrap, removes
only owned resources, restores prior NFS state, verifies unmounts, and does not
depend on writable diagnostics.

The test CLI selects substrates and scenarios independently. Its planner batches
shared-home SSH cases behind a crash-recoverable transition; separate homes are
canonical. Pod work uses `tester` UID/GID 2000, matching the all-squashed NFS
export and Slurm account.

The harness builds the ordinary deployment archive from an immutable tracked
snapshot, caches it by snapshot, architecture, fixed recipe, and seeded
Elbencho/runtime identity, and extracts isolated scenario workspaces. Real cases
cover baseline and default I/O, failure/resume, retained data, live capture,
Cartesian sweeps, single-file and weighted-root behavior, shared SSH homes, and
Slurm scheduling. Fast tests cover parsing, precedence, path and workload safety,
sizing, scheduler boundaries, failure contracts, and reporting. CI runs the full
NFS-backed catalog concurrently on amd64 and arm64 with repeatable-teardown
headroom; SBX is a supported local backend.

GitHub Actions runs concurrent compliance, ShellCheck, Black, and Pylint checks
alongside Python 3.12 unit tests for pull requests and pushes to `main`. Python
3.14 unit tests run weekly and on manual request.

## Repository map

| Path | Responsibility |
|---|---|
| `storage-tests/fs/` | Filesystem IO and metadata entry points, Slurm jobs, and SSH scriptlets |
| `storage-tests/object/` | Warp object-storage entry point and substrate-specific dispatchers |
| `storage-tests/network/` | Elbencho netbench entry point and substrate-specific dispatchers |
| `lib/env_base.sh` | Derived configuration, substrate selection, executable paths, and Slurm option construction |
| `lib/env_functions.sh` | Shared orchestration, validation, SSH, Slurm, resume, and environment-snapshot helpers |
| `lib/_platform_functions.sh` | GNU/Linux and Homebrew coreutils command adapters shared by shell libraries |
| `lib/_elbencho_functions.sh` | Filesystem IO, metadata, execution reification, and workload-completion logic |
| `lib/_warp_functions.sh` | Warp client lifecycle and object benchmark logic |
| `lib/_netbench_functions.sh` | Netbench service, grouping, and traffic logic |
| `lib/*.py` | Shared analysis, report, live-data, and metadata helpers |
| `utils/extract-*.sh` | Analysis entry points that prepare the repository virtual environment |
| `utils/extract-*.py` | Parsers, aggregators, tables, and plots |
| `utils/build_tarball.sh` | User-local deployment-tarball builder |
| `utils/build/` | Helpers for building Warp and the in-tree s3test program |
| `tests/` | Python and shell-behavior regression tests collected by `pytest` |
| `integration-tests/` | Single-host kind, RWX storage, SSH, and Slinky fixture |

The checked-in benchmark entry points are:

| Benchmark | Entry point | Analyzer | Slurm dispatch unit | SSH dispatch unit |
|---|---|---|---|---|
| Filesystem IO | `storage-tests/fs/nv-elbencho-sweep.sh` | `utils/extract-elbencho.sh` | One maximum-sized coordinator allocation for the sweep | One local sequential dispatcher for the sweep |
| Filesystem metadata | `storage-tests/fs/nv-mdtest-elbencho.sh` | `utils/extract-mdtest-elbencho.sh` | One job per node/task pair | One remote invocation per node/task pair |
| Object storage | `storage-tests/object/nv-warp-sweep.sh` | `utils/extract-warp.sh` | One job per node count | One remote invocation per node count |
| Network | `storage-tests/network/nv-netbench.sh` | `utils/extract-netbench.sh` | One job per node count | One remote invocation per node count |

## Configuration and substrate selection

Users copy `env.sh.template` to `env.sh`. Test entry points source `env.sh`,
which sources `lib/env_base.sh`; entry points also source the shared functions
they need. `validate_env.sh` checks the resolved configuration before expensive
work begins.

Important configuration relationships:

- A non-empty `SSH_HOST_LIST` enables SSH and disables Slurm. The referenced
  file accepts comma- or whitespace-separated hosts and ignores comment lines.
- Without `SSH_HOST_LIST`, `env_base.sh` enables Slurm. `SLURM_NODE_INCLUDES`
  and `SLURM_NODE_IGNORES` point to optional files containing valid Slurm
  hostlists, including compressed forms.
- `ORDER_NODES=1` makes SSH selection use the first requested hosts. With a
  Slurm include list, it makes jobs use the first requested expanded nodes.
  Netbench still shuffles its selected nodes when forming traffic groups.
- `SLURM_EXCLUSIVE_USER=1` selects `--exclusive=user`. Because that mode does
  not imply all CPUs, setup queries the target node CPU count and adds
  `--cpus-per-task` when it can resolve the count. The default is bare
  `--exclusive`.
- `_SBATCH_OPTIONS_BASE` and `_SRUN_OPTIONS_BASE` contain the structured base
  options. `build_sbatch_cmd` and `build_srun_cmd` append the
  `SLURM_EXTRA_ARGS` array without losing embedded spaces. The public
  `SBATCH_OPTIONS` and `SRUN_OPTIONS` strings include flattened extra arguments
  for best-effort interactive use.
- `parse_range_specification()` accepts comma-separated values, ascending
  inclusive ranges, and `+step` increments. Entry points validate the complete
  expanded list before dispatch.
- `TEST_DIRS` is an associative array of filesystem test roots and weights.
  `validate_env.sh` compares each path's `stat -c %d` device number with `/`
  through both dispatch interfaces; this establishes distinct backing storage,
  not that the configured path is itself the exact mountpoint.
  Object tests use a dedicated `OBJ_BUCKET`, endpoint settings, and credentials
  sourced from `OBJ_AUTH_FILE`.

`env.sh`, `OBJ_AUTH_FILE`, filesystem resume snapshots, and reified execution
definitions are sourced as Bash. They are executable trusted inputs, not passive
configuration data.

## Execution architecture

### Shared benchmark logic

Slurm and SSH wrappers establish a node context and call the same benchmark
functions from `lib/_elbencho_functions.sh`, `lib/_warp_functions.sh`, or
`lib/_netbench_functions.sh`. Sourced libraries use caller-provided variables;
their ShellCheck annotations document that boundary.

SSH nodes do not need the repository or results filesystem mounted. The launch
host copies the required binary and libraries into a remote working directory,
then sends Bash through `ssh host /bin/bash -s`. Metadata, object, network, and
filesystem delete-only operations use checked-in remote scriptlets. Filesystem
IO sweep cells build an inline scriptlet from a saved execution definition and
an SSH-specific tail. Results return as a compressed `tar | tar` stream.

### Filesystem IO reification and resume

`nv-elbencho-sweep.sh` expands the Cartesian product of node counts, IO-size
entries, thread counts, and IO depths before dispatch. Each cell has stable,
1-indexed files under `OUTPUT_DIR/executions/`:

- `NNNN.sh`: sourceable parameters and generated paths;
- `NNNN.status`: atomically written `PENDING`, `RUNNING`, `SUCCESS`, or `FAILED`;
- `NNNN.log`: cell output;
- `NNNN.exitcode`: command status; and
- `NNNN.jobid`: Slurm coordinator job ID when applicable.

Each cell receives a stable `-${DS}-e${execution_id}` test-path suffix. Retries
therefore reuse that cell's paths without colliding with another cell.

In Slurm mode, `dispatch_slurm_executions` allocates the largest node count
needed by any non-successful cell and submits
`storage-tests/fs/sbatch/_nv-elbencho-coordinator.sh`. The coordinator starts
elbencho services once across the allocation and processes cells sequentially,
using the first requested number of allocation hosts for each cell. Initial
service health failure preserves its log and gets one bounded restart attempt;
phase-level checks can also restart unhealthy services. Signals cancel and
wait for the exact allocation before restoring the caller's traps. Cancellation
is armed immediately after submission, before dispatch-lock handoff; an
unverified cancellation retains the lock.

In SSH mode, `dispatch_ssh_executions` starts services once across the usable
host pool. It selects a fresh host subset for each cell, runs cells sequentially,
and retrieves that cell's artifacts before continuing.

Both modes stop after the first failed cell. `--resume <results_dir>` is mutually
exclusive with other CLI modes, sources the saved `env_used.sh`, resets stale
`RUNNING` cells to `PENDING`, skips `SUCCESS`, and runs the rest in order. A
dispatch lock prevents concurrent coordinators or resume loops from operating
on the same execution directory. Slurm resume refuses to reset work while the
saved coordinator may still be active; if `squeue` no longer knows the job,
terminal state must be confirmed through Slurm accounting.

`env_used.sh` is the executable resume snapshot. `env_used.yaml` is the
human- and tool-readable run snapshot. Protect the result directory from
untrusted modification because resume sources both `env_used.sh` and each
`executions/NNNN.sh`.

## Filesystem IO workload models

All filesystem IO models use the same reified sweep and result layout, but
their termination and cleanup contracts differ.

### Worker directories

`ELBENCHO_FILE_LAYOUT=worker-directories` is the default many-file workload.
In the usual single-target path, directory creation is separate; write uses
`--infloop` plus `--timelimit`; direct reads use the same combination; buffered
reads make at most one logical pass with the time limit as a ceiling to avoid
measuring repeated page-cache hits. The `-s/--single` or multiple-target branch
derives a fixed file count for writes while reads remain time-bounded.

Unless `ELBENCHO_FILE_SIZE` is set, generated file size is the write block size
times `ELBENCHO_FILE_SIZE_MULTIPLIER`.

The entry point supports these lifecycle modes:

- default: mkdir, write, optional pause, read, cleanup;
- `--write-only`: mkdir and write, retain each cell's dataset, and print
  `ELBENCHO_WRITE_ONLY_DATA_DIR`;
- `--write-no-read`: mkdir, write, cleanup;
- `--read-from <directory>`: read an operator-provided dataset using a scanned
  or cached treefile; and
- `--delete-only <directory>`: delete once on one compute node after validating
  that the path is a strict descendant of the configured test root.

A directory staged with `--read-from` may contain
`.storage-scale-test-elbencho-treefile.txt`. A cache miss scans into a temporary
file in the dataset parent and publishes it atomically only after a successful
read; later reads reuse it without rescanning. The operator must remove the cache
after changing the dataset. Staged directory reads remain duration-driven
regardless of `ELBENCHO_FILE_LAYOUT`, and their aggregate file and byte totals
come from the exact treefile rather than the current reader topology or
`ELBENCHO_FILES_PER_NODE`.

### Generated shared directory

`ELBENCHO_FILE_LAYOUT=shared-directory` models a bounded checkpoint-like
dataset in one flat shared directory. It requires one `TEST_DIRS` root with
weight 1 and a positive `ELBENCHO_FILES_PER_NODE`. Each configured thread count
must divide files per node exactly:

```text
files_per_worker = ELBENCHO_FILES_PER_NODE / threads
total_files = nodes * ELBENCHO_FILES_PER_NODE
```

Each worker owns complete files. IO depth controls outstanding block IO; it does
not create extra writers or file descriptors for one file. Multiple workers
sharing a single file are not represented by this layout.

`ELBENCHO_FILE_SIZE` sets the exact per-file size; otherwise the write block size
and multiplier derive it. Direct or random IO requires exact divisibility by
the effective block size. Buffered sequential IO may finish with a short final
block.

Generated mkdir, write, read, and distributed `RMFILES` phases are finite
exact-completion operations: they do not use `--timelimit` or `--infloop`.
Mkdir must exit successfully, and write/read JSON must report the expected file
and byte counts before the cell can advance or succeed. Delete JSON must report
the expected entry count and last-worker elapsed time. After distributed
deletion, the runner verifies the target identity and removes the empty
directory with `rmdir`; recursive removal is reserved for identity-checked
failure and signal cleanup.

Per-cell `NNNN.write.json`, `NNNN.read.json`, `NNNN.delete.json`, and atomic
`NNNN.workload.tsv` files preserve completion and timing evidence. Default runs
remove the proven-empty generated target after read. `--write-only` retains it;
`--write-no-read` performs the verified distributed deletion. Controlled
failure retains the original error even if best-effort cleanup also has a
problem.

### One shared large file

`ELBENCHO_SINGLE_BIG_FILE=1` selects regular-file path mode and permits only
sequential IO. Generated write/default runs require
`ELBENCHO_SINGLE_BIG_FILE_SIZE` and process one exact extent. Elbencho normally
partitions that file among services; `ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=1`
adds `--nosvcshare` so each node accesses the complete file.

`--read-from <file>` takes the extent from file metadata and does not use a
treescan or treefile. Direct reads repeat until the configured time limit.
Buffered reads make one logical pass with the time limit as a ceiling.

### IO-size and cache semantics

IO-size entries encode both transfer size and access order:

- `4K`: sequential 4 KiB write and read;
- `r4K`: random 4 KiB write and read; and
- `1M,r4K`: sequential 1 MiB write followed by random 4 KiB read.

Multi-node reads rotate the host list between phases and reified cells so a
reader does not retain the same writer-to-file or writer-to-slice assignment.
This reduces client-cache reuse but does not replace direct IO when page-cache
effects are outside the intended workload.

All phases for one cell append to one CSV. The first emitted phase writes the
header and later phases use `--nocsvlabels`. Read-only runs write a header.
`extract-elbencho.py` also recognizes headerless elbencho CSV by locating the
operation column and using the upstream CSV field offsets; this compatibility
path derives direct/random flags from the command field when needed.

## Other benchmark contracts

### Filesystem metadata

`nv-mdtest-elbencho.sh` runs separate create, stat, and delete elbencho
invocations for each iteration. Host assignment rotates between phases to
reduce client metadata-cache reuse. Files are zero length so the reported work
isolates metadata operations.

The default layout pre-creates a wide branched tree and gives each thread
`MDTEST_BRANCH_FACTOR²` directories. `--single-dir-file-target <count>` instead
uses elbencho `-n 0`, placing uniquely named worker files directly in one flat
directory. Dense mode requires one node count, one task count, and one generated
target. Elbencho assigns a uniform integer file count to every worker, so the
actual total is the closest achievable whole-worker total; both requested and
actual values are recorded.

The analyzer aggregates a result pair only when its CSV contains complete
`WRITE`, `STAT`, and `RMFILES` records and its `.out` file contains all three
latency histograms. It reports rates, last-worker phase elapsed times,
percentiles, variance, scaling efficiency, and configuration provenance across
iterations. Conflicting provenance values are displayed together with a
warning rather than silently selecting one.

### Object storage

`nv-warp-sweep.sh` performs bucket cleanup, PUT until the configured aggregate
minimum object count, a GET sweep over thread counts, and final DELETE cleanup
for each node-count/object-size configuration. `WARP_PUT_MIN_FILES_PER_CLIENT`
is multiplied by node count. The bucket is destructive benchmark state and
must contain no valuable objects.

Optional behavior includes multipart uploads, ranged reads, S3 Express endpoint
handling, aggregate PUT/GET RPS budgets, and `WARP_PREFIXES`. Static prefixes
require support from the NVIDIA Warp fork at
`https://github.com/NVIDIA/warp-minio`; Warp deletes its objects but does not
perform a separate operation to remove the logical prefixes represented by
their names.

The Warp analyzer prefers JSON or JSON.zst but can parse text output. It treats
request segments as optional/partial, supports both `single_sized_requests` and
`multi_sized_requests`, and combines multi-size TTFB histograms using Warp's
stored sums and squared sums before recomputing aggregate percentiles. Its
analyzed-JSON envelope is recognized by content rather than filename. Optional
per-client plots and reports cover fairness, persistent underperformance, and
segment-level behavior for multi-client runs.

### Network

Network testing is beta and needs more work before at-scale use.
`nv-netbench.sh` splits selected nodes into groups A and B and uses elbencho
netbench without localhost traffic. The default half-and-half mode runs A→B and
B→A sequentially. `--bidirectional` starts two services per node on adjacent
ports and drives both directions concurrently with separate coordinators.

Per-thread transfer size is inversely proportional to thread count so a
saturated NIC should run for approximately `NETBENCH_TARGET_RUNTIME`. Group
assignments shuffle between iterations to reduce topology bias. The analyzer
combines iterations and directions, reporting Gbps/Tbps throughput,
latency percentiles, scaling efficiency, and log-scale histograms.

Pairwise-all traffic is not implemented. Elbencho netbench has no native
timeout, so external execution limits remain an operator concern.

## Results and analysis

Runs write UTC-datestamped directories below `RESULTS_DIR`, including
`elbencho-<datestamp>`, `mdtest-elbencho-<datestamp>`,
`warp-<datestamp>`, and `netbench-<mode>-<datestamp>`.

The analysis wrappers call `setup_python_venv()` and install the pinned root
`requirements.txt` only when its hash differs from
`.venv/.requirements.sha256`. Python 3.12 or newer is required; pinned NumPy
sets that floor. Run analyzers through their shell wrappers so dependency setup
and argument forwarding remain consistent.

Shared Python behavior lives in `lib/`:

- `reporting_common.py`: result-pair discovery, histogram parsing, latency
  formatting, scale filters, shared CLI flags, and histogram axis bounds;
- `env_used_yaml.py`: environment snapshot loading and application;
- `join_datestamps.py`: display and filename-safe datestamp joining;
- `parse_only_sizes.py`: `--only-sizes` parsing without splitting compound
  entries such as `1M,r64K`;
- `stdout_report_file.py`: mirrored terminal report output; and
- `elbencho_live_report.py`: live CSV aggregation and per-client analysis.

Reports preserve operation, size, random/sequential order, direct/buffered IO,
node count, thread count, and IO depth as separate dimensions. Plot series use
precomputed key-to-color mappings so non-contiguous equivalent configurations
receive the same color. Palette and marker behavior is analyzer-specific:

- filesystem IO and Warp use `tableau-colorblind10` for up to 10 series,
  `tab20` for 11–20, and `cividis` above 20; both provide 12 markers;
- metadata plots cycle `tableau-colorblind10`, and multi-node rate plots cycle
  eight markers; and
- netbench plots cycle a fixed seven-color palette and use circle markers.

Highly populated plots can reuse colors or marker shapes. The project generates
comparisons and reports but does not automatically decide whether a performance
change is a regression.

For large JSON or JSON.zst inspection without printing complete datasets, use
`utils/compress_json_for_context.py`:

```bash
zstd -dc result.json.zst | python3 utils/compress_json_for_context.py
python3 utils/compress_json_for_context.py < result.json
```

## Deployment helpers and supply chain

`utils/build_tarball.sh` creates `storage-scale-test.tar.gz` for a user to move
into a benchmark environment. It:

- downloads pinned upstream elbencho `v3.1-11` static archives for x86_64 and
  aarch64 when needed and verifies architecture-specific SHA-256 values;
- builds stripped static s3test binaries from `utils/build/s3-test.c` when
  missing or stale; and
- includes existing Warp binaries but does not download or automatically build
  them, warning when an architecture is missing.

`utils/build/build_s3test_from_source.sh` tries suitable local compilers, Docker,
and Docker Buildx. Failure to produce one architecture warns and permits tarball
creation, but object testing on that architecture will be unavailable.

`utils/build/build_warp_from_source.sh` builds x86_64 and aarch64 Warp from a
GitHub/GitLab HTTPS or SSH URL plus a tag, branch, or commit. With no arguments,
it uses `https://github.com/NVIDIA/warp-minio` at `nv-main-oss`. It requires Go
1.26.5 or newer and tries an adequate local Go, Docker, or a user-local temporary
Go installation. User-supplied source requires both URL and ref. Version metadata
is injected into the resulting binaries.

These helpers are convenience tooling, not a project-operated binary supply
chain. Users own source and toolchain selection, provenance, vulnerability
assessment, licenses, source-offer obligations, SBOMs, and validation of the
deployment tarball they create.

## Security and trust model

The tool assumes an operator-controlled Slurm deployment or passwordless-SSH
fleet. It does not create an authorization boundary between mutually untrusted
users. Test filesystem paths and object buckets must be dedicated and
disposable.

No inbound connection from outside the cluster or trusted benchmark environment
is required. Administrators can enforce that boundary with firewalls, security
groups, or network policies. Inside the boundary:

- the launch host must reach SSH servers in SSH mode;
- elbencho and Warp workers open temporary benchmark listeners;
- coordinators connect to those listeners;
- bidirectional netbench uses two service ports and associated data ports; and
- object tests connect outward to the configured S3-compatible endpoint.

Benchmark listeners are not authentication boundaries and must not be exposed
to untrusted networks. Build helpers may make internet connections when fetching
source, toolchains, images, or upstream binaries.

`OBJ_AUTH_FILE` should be mode `0400`. SSH mode copies it under a restrictive
umask into the remote work area, where the Warp scriptlet sources it. Treat
remote work directories as credential-bearing and remove them according to the
operator's policy. SSH uses the user's existing key configuration; Slurm uses
the deployment's existing authentication.

Paths supplied to `--read-from` and `--delete-only` are trusted operator input.
Delete-only additionally enforces a strict descendant relationship to the
configured test root. Generated filesystem targets use identity checks before
recursive failure cleanup to avoid deleting an unrelated path.

Public accepted-risk records live in `.security-triage.yaml` and cover:

- operator-selected Warp source, Go toolchain, and build provenance;
- conventional S3 credential lengths;
- isolated ephemeral SSH fleets; and
- trusted resume directories as executable configuration.

## Development invariants

- Use elbencho behavior from source version 3.0.37 or newer when reasoning
  about CSV or execution semantics. Prefer a local `tmp/elbencho-src` checkout
  when present; `tmp/` is ignored and is not part of this repository.
- Do not vendor third-party benchmark code or binaries.
- Keep benchmark behavior shared between Slurm and SSH in `lib/`; substrate
  wrappers should establish context and transport, not fork core semantics.
- Keep filesystem exact-completion checks tied to explicit JSON counters and
  workload records. A zero elbencho exit status alone does not prove that a
  time-limited phase completed its intended finite dataset.
- Preserve write/read order separately for compound IO sizes. A single
  `random_io` value cannot describe `1M,r4K`.
- Build plot color mappings before plotting. Stateful color advancement fails
  when the same semantic key appears non-contiguously.
- Production duplication belongs in shared `lib/` modules. Repeated test setup
  belongs in a shared test helper.
- Every text file carries the NVIDIA Apache-2.0 header except `LICENSE`; the
  README header is intentionally at the bottom. See `AGENTS.md` for the exact
  rule.
- Shell tests use the adapters in `lib/_platform_functions.sh` to select
  unprefixed GNU coreutils on Linux and Homebrew's `g`-prefixed coreutils on
  macOS. Scale-test execution remains shell-only and does not acquire a Python
  dependency.

## Required validation

`pytest.ini` limits default collection to `tests/`, excluding ignored checkouts
under `tmp/`.

Run the repository checks from the root:

```bash
./utils/run_ci_checks.sh
```

The `lint` target runs the four static checks concurrently by default, buffers
their output, and reports all failures. `CI_CHECK_JOBS` controls the shared
concurrency budget. Local `all` runs divide the host's logical CPUs between the
lint checks and pytest, with pytest distributing tests through `pytest-xdist`.
Pull-request and `main` CI explicitly use four workers to match the runner CPU
count.

After Python changes, run Black 25.9.0 or newer and Pylint. `.pylintrc` is the
canonical configuration and the required score is 10.00/10. It gates Pylint's
enabled fatal/error/warning checks plus exactly these C/R checks:

- `C0200` (`consider-using-enumerate`)
- `C0411` (`wrong-import-order`)
- `R0801` (`duplicate-code`)
- `R1704` (`redefined-argument-from-local`)

All other C/R categories are disabled. `E0401` is disabled because dependency
availability is handled by wrapper setup and runtime validation; `W0511` allows
maintainer TODO/FIXME comments. `E0106` and `W1502` remain at Pylint's disabled
optional-extension defaults.

Shell changes must pass ShellCheck across the repository. Bash-specific
conditionals use `[[ ... ]]`; sourced libraries use `# shellcheck shell=bash`;
remote scriptlets use a Bash shebang; variables and arrays remain quoted; and
functions use explicit return status where the surrounding library follows that
convention.
