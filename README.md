# NVIDIA Storage Scale Test

Shell and Python tooling for running distributed storage benchmarks and
turning their output into tables and plots.

Tests are supported for:

* Filesystems: data and metadata operations with elbencho
* S3-compatible object storage with Warp
* Storage networks with elbencho netbench (beta)

## Warning: These Tests Destroy Data

These tests **create, overwrite, and delete files and objects**. Use only
dedicated test paths and empty buckets. Root access is neither required nor
recommended.

## Resources

* [Getting Started](#getting-started) — quickstart
* [Benchmark recipes](BENCHMARK_RECIPES_FILESYSTEM.md) — filesystem `env.sh`
  settings by use case
* [Roadmap](ROADMAP.md) — planned work
* Design & requirements: [docs/DESIGN.md](docs/DESIGN.md),
  [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md),
  [docs/ARCHITECTURE_DIAGRAMS.md](docs/ARCHITECTURE_DIAGRAMS.md)
* [Contributing](CONTRIBUTING.md) and [Security policy](SECURITY.md)
* Licensing: [LICENSE](LICENSE) (Apache-2.0) and [NOTICE](NOTICE) — third-party
  benchmark tools carry their own licenses, including GPL-3.0 and AGPL-3.0

## Global Prerequisites

* Linux on the orchestration host and on every client. Benchmark execution
  relies on bash 4.3 or newer and a Linux userland. macOS can run static checks
  and unit tests that do not require benchmark binaries, but it cannot run the
  storage benchmarks. In particular, elbencho is not available for Apple
  silicon; binary-dependent tests are skipped when it is unavailable. Running
  the developer unit tests on macOS requires a newer Bash and GNU coreutils,
  which can be installed with `brew install bash coreutils`.
* Python 3.12 or newer is required on the host that runs the analysis and
  reporting wrappers. The pinned current NumPy release establishes this minimum.
* Filesystem clients must be available through exactly one configured
  execution substrate: Slurm, passwordless SSH, or an authorized Kubernetes
  cluster. Slurm users need the account, reservation, and partition required
  to submit jobs.
* Use tuned clients with enough aggregate network bandwidth to saturate the
  target. A starting count is
  `1.1 * target_Gbps / measured_per_client_Gbps`.

## Getting Started

The fastest path from a fresh checkout to a first result is a single-node
filesystem sweep. The same pattern (configure `env.sh` → validate → sweep →
report) applies to every test type.

1. **Build a tarball and copy it to a client host.** From your workstation:

   ```bash
   ./utils/build_tarball.sh
   ```

   This produces `storage-scale-test.tar.gz`. See
   [Deployment Tarballs and Benchmark Binaries](#deployment-tarballs-and-benchmark-binaries)
   for what it includes and the binaries you must supply. Copy and unpack it on
   a host that can reach your client nodes (a Slurm login node, or any host with
   passwordless SSH to the clients).

2. **Configure `env.sh`.** Copy the template and edit it for your environment:

   ```bash
   cp env.sh.template env.sh
   ```

   At minimum, set how clients are reached and what to test:

   ```bash
   # --- How to reach the client nodes (pick ONE) ---
   # Slurm mode: set your account / reservation / partition (see env.sh.template).
   # SSH mode: point at a host list file (SLURM_* vars are then ignored).
   # export SSH_HOST_LIST=/absolute/path/to/host_list

   # --- Filesystem test target (mounted on every client) ---
   declare -A TEST_DIRS=(["/mnt/fs1/scaletest"]=1)

   # --- A small, fast sweep to start with ---
   export ELBENCHO_SCALE_IO_SIZES=("r4K" "1M")
   export ELBENCHO_SCALE_THREAD_LIST=("1" "32" "64" "128")
   export ELBENCHO_SCALE_READ_WRITE_DURATION=60
   ```

3. **Validate the environment.** Iterate until this runs cleanly:

   ```bash
   ./validate_env.sh
   ```

4. **Run a single-node sweep.**

   ```bash
   ./storage-tests/fs/nv-elbencho-sweep.sh --nodes 1
   ```

5. **Generate a report** from the results directory it prints:

   ```bash
   ./utils/extract-elbencho.sh "$RESULTS_DIR"/elbencho-<datestamp>/
   ```

From here, pick the thread counts that saturated a single node and move on to a
multi-node sweep. For tuned, use-case-specific parameter sets (peak-finding,
requirements confirmation, post-maintenance validation, single-large-file
testing), see [BENCHMARK_RECIPES_FILESYSTEM.md](BENCHMARK_RECIPES_FILESYSTEM.md).

## Deployment Tarballs and Benchmark Binaries

This repository does not ship benchmark binaries. `utils/build_tarball.sh`
creates a user-local `storage-scale-test.tar.gz` with the available tools:

* **elbencho:** reuses `utils/elbencho{,.aarch64}` or downloads pinned upstream
  static releases when missing or when `--force-download` is set. Download
  failure does not prevent tarball creation.
* **Warp:** includes existing `utils/warp{,.aarch64}` only. The suggested
  `utils/build/build_warp_from_source.sh` defaults to
  [NVIDIA/warp-minio](https://github.com/NVIDIA/warp-minio), a fork of
  [MinIO Warp](https://github.com/minio/warp), and accepts another GitHub/GitLab
  URL plus a tag, branch, or commit.
* **s3test:** builds `utils/s3test{,.aarch64}` from the in-tree C source with
  local compilers or Docker/Buildx when missing or stale.

Review third-party licenses and provenance — see [NOTICE](NOTICE), which records
that elbencho is GPL-3.0 and Warp is AGPL-3.0. Verify that the tarball contains
working binaries for every target architecture; NVIDIA and this project do not
publish or distribute the resulting tarball or its binaries.

## Execution Modes

Set exactly one `EXECUTION_SUBSTRATE` value in `env.sh`: `slurm`, `ssh`, or
`kubectl`. There is intentionally no default, and `SSH_HOST_LIST` configures
SSH mode but does not select it. The selected substrate applies to the
filesystem sweep; its configuration must be complete before
`validate_env.sh` is run.

### Passwordless SSH

The host-list file accepts comma- or whitespace-separated hosts on multiple
lines and ignores comment lines:

```text
192.0.2.10,192.0.2.11
# comment
node1 node2 node3
```

Configure SSH mode in `env.sh`:

```bash
export SSH_HOST_LIST=/absolute/path/to/host_list
# export SSH_USER=ubuntu             # otherwise the user or SSH config wins
# export SSH_HOMEDIR_SHARED=1        # any non-empty value
```

Setting `SSH_HOST_LIST` disables Slurm settings. Authentication must be
non-interactive. Run `validate_env.sh` to verify connectivity.

### Slurm options

`SLURM_JOB_NAME_PREFIX` prefixes job names. `SLURM_EXTRA_ARGS` is a Bash array
appended after generated sbatch/srun options, so later duplicate options can
override earlier ones while arguments with spaces remain intact:

```bash
SLURM_EXTRA_ARGS=("--constraint=ib" "--comment=storage validation run")
```

`SLURM_EXCLUSIVE_USER=1` uses `--exclusive=user` and derives
`--cpus-per-task` when the target CPU count is available; the default uses
`--exclusive`.

### Kubernetes filesystem sweeps

Kubernetes mode requires an already authorized `kubectl` context. The tool
does not provision a cluster, namespace, PV, or PVC. Set all of the following
in `env.sh`:

```bash
export EXECUTION_SUBSTRATE=kubectl
export KUBECTL_NAMESPACE=storage-scale-test
export KUBECTL_PV=storage-scale-test-pv
export KUBECTL_PVC=storage-scale-test-pvc
export KUBECTL_NODE_SELECTOR='storage-scale-test/worker=true'
export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.1-11
```

The namespace and the named PV/PVC must already exist and the PVC must be
bound to that PV. The node selector must identify enough schedulable worker
nodes for the requested sweep. The cluster CNI must provide direct Pod IPv4
connectivity between the coordinator and worker Pods and enforce the
attempt-scoped network policies; the selected nodes must be able to mount the
PVC. The configured benchmark image must be usable under the configured pull
policy, and any registry credentials required by the cluster are a user
responsibility.

`TEST_DIRS` remains a logical filesystem configuration in Kubernetes mode.
The sweep prepends `/mnt/storage-scale-test/` when it constructs Pod-side
paths, so users must not add that prefix themselves. The PVC is mounted at
that path. The tool reserves `.storage-scale-test` below the mount for its
control, ownership, and completed-cell data; do not use that name in a test
directory.

A Kubernetes invocation submits one asynchronous Job for the whole sweep.
Submission stages the verified control bundle and execution definitions on
the PVC, starts the Job, and prints commands for querying and collecting the
attempt:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --nodes 1,2,4
./storage-tests/fs/nv-elbencho-sweep.sh --status "$RESULTS_DIR"/elbencho-<datestamp>/
./storage-tests/fs/nv-elbencho-sweep.sh --collect "$RESULTS_DIR"/elbencho-<datestamp>/
```

The Job runs without depending on the submitting process's later `kubectl`
credentials. `--status` reads durable remote state and may report a running,
successful, failed, or cancelled attempt. `--cancel` stops the exact saved
attempt and preserves its durable state. `--collect` is required after a
terminal attempt: it copies completed results, snapshots, and diagnostics
from the PVC into the local results directory and performs owned-resource
cleanup. Collection of a failed or cancelled attempt returns a failure status
after publishing the partial results, so callers must inspect the collected
state before deciding whether to continue.

Kubernetes `--resume` is collection-gated. After collecting a failed attempt,
run `--resume <results-dir>` with the same Kubernetes configuration to submit
only the uncompleted cells; successful cells and their results are retained.
Do not run concurrent operations on one result directory or concurrent
resumes. Active measured output is Pod-local scratch, while completed-cell
publication and the control ledger are copied to the PVC between cells. A
failure before publication can lose that cell's partial output, but cannot
silently claim it succeeded.

## Heterogeneous Client Fleets

Random subsets from mixed-throughput clients can make adjacent sweep points
incomparable. `ORDER_NODES` and `utils/slurm/group_into_bins.py` create an
ordered host list with balanced cumulative throughput.

Create an input CSV:

```csv
InstanceName,Gbps
node-01,100
node-02,50
node-03,100
node-04,50
```

Generate the ordered list on stdout and a suggested descending `--nodes` list
on stderr:

```bash
python3 utils/slurm/group_into_bins.py --bin-size 25 nodes.csv \
    > ordered_nodes
```

Use `ordered_nodes` as `SSH_HOST_LIST` or `SLURM_NODE_INCLUDES`, set
`ORDER_NODES=1`, and pass the emitted node counts to a sweep. SSH then takes the
first `N` hosts; Slurm with an include list requests exactly its first `N`
expanded nodes. Netbench still shuffles its selected nodes into traffic groups.

Smaller bins add sweep points; larger bins provide more balancing freedom.

## Filesystem Tests

A working elbencho binary for each client architecture must be available under
`utils/`. Mount and tune each filesystem at the same path on every client, then
configure weighted test roots:

```bash
declare -A TEST_DIRS=(
    ["/mnt/fs1/scaletest"]=4
    ["/mnt/fs2/scaletest"]=1
)
```

Weights distribute load between unequal targets. Tune `FS_MAX_AGG_THROUGHPUT`,
`FS_MAX_NODE_THROUGHPUT_GBPS`, and `FS_MAX_NODE_IOPS` when using multiple
targets because they determine generated file counts and bounded write time.

### Filesystem data I/O

Set `TEST_DIRS`, tune the elbencho variables in `env.sh`, and run
`validate_env.sh`. `ELBENCHO_SCALE_IO_SIZES`, `ELBENCHO_SCALE_THREAD_LIST`,
and `ELBENCHO_IODEPTH_LIST` form a Cartesian product for every requested node
count. An IO-size entry may be `4K`, `r4K`, or a write/read pair such as
`1M,r4K`; see `env.sh.template`.

Start with one node, analyze it, then retain only useful thread, size, and I/O
depth values for the scale sweep:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --nodes 1
./utils/extract-elbencho.sh "$RESULTS_DIR"/elbencho-<datestamp>/
./storage-tests/fs/nv-elbencho-sweep.sh --nodes 1,2,4,8
```

All sweep scripts accept comma-separated positive integers and ascending
inclusive ranges: `X`, `X-Y`, or `X-Y+Z`. The endpoint is included even when
the step does not land on it; for example, `3-10+2` expands to
`3,5,7,9,10`. The order is preserved. Run a script with `--help` for its full
mode and argument contract.

The default worker-directory workload is time-based. Set
`ELBENCHO_SCALE_READ_WRITE_DURATION` long enough to measure sustained I/O;
60 seconds is useful for exploration, while 300 seconds or more is a typical
starting point for publishable runs. Generated shared-directory workloads are
completion-based instead.

The default lifecycle is mkdir, write, read, and cleanup.
`--write-no-read` skips the read and still cleans up; the staged-data modes
below retain, reread, or delete an operator-selected dataset.

Slurm uses one coordinator allocation sized to the largest node count and runs
all cells sequentially; SSH also runs cells sequentially. Size Slurm
`run_time` for the complete product and all phases, startup, pauses, and
cleanup.

#### Resuming an interrupted filesystem sweep

The sweep records each cell separately and stops at the first failure. After
correcting the cause, continue it with:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --resume \
    "$RESULTS_DIR"/elbencho-<datestamp>/
```

`--resume` must be the only argument. It restores the original environment and
CLI modes from the result directory, skips successful cells, resets stale
running cells, and dispatches the remainder in their original order in either
Slurm or SSH mode. Do not run concurrent resumes. A Slurm resume also refuses
to reset work while its prior coordinator may still be active. Resume sources
shell files in the result directory, so use only a trusted, unmodified result
directory created by a resume-capable revision.

#### Staged dataset workflow

Use `--write-only`, `--read-from`, and `--delete-only` to create a dataset once,
run repeated read sweeps, and remove it without rewriting it for each read.

1. Configure one value in each sweep dimension and run `--write-only`. Each
   successful cell retains a unique target and prints its
   `ELBENCHO_WRITE_ONLY_DATA_DIR`.
2. Run `--read-from <path> --nodes <spec>` as needed. A many-file read caches
   its scan in `<path>/.storage-scale-test-elbencho-treefile.txt`; remove that
   file after changing the dataset.
3. Run `--delete-only <path>`. The path must be a strict descendant of a
   configured `TEST_DIRS` root.

To build a mixed-size tree, move retained targets below one common directory
under the configured test root, read that parent, then delete it once.

#### Shared-Directory Checkpoint Files (elbencho)

This layout models ranks writing distinct checkpoint files into one flat shared
directory. Configure one `TEST_DIRS` entry with weight `1`:

```bash
export ELBENCHO_SINGLE_BIG_FILE=0
export ELBENCHO_FILE_LAYOUT=shared-directory
export ELBENCHO_FILES_PER_NODE=1
export ELBENCHO_FILE_SIZE=64G
export ELBENCHO_SCALE_THREAD_LIST=("1")
export ELBENCHO_IODEPTH_LIST=("1")
```

For `N` nodes, `F` files per node, and `T` threads per node:

```text
total_files = N * F
files_per_worker = F / T
```

`F` must be at least and evenly divisible by every `T`; the harness does not
round. Each worker owns complete files. I/O depth controls outstanding block
I/O per worker, not the number of writers. To model one file per single-threaded
saving rank, set both `F` and `T` to the rank count per node and use I/O depth
`1`.

Generated shared-directory phases are completion-based. They make one pass,
ignore duration and `-s`, and verify exact file and byte counts. Normal and
`--write-no-read` runs delete with distributed elbencho workers;
`--write-only` retains the dataset. `ELBENCHO_FILE_SIZE` is optional; otherwise
the write block size and `ELBENCHO_FILE_SIZE_MULTIPLIER` determine it. Direct or
random I/O requires the file size to be divisible by the effective block size.

With `--read-from <directory>`, the scanned tree defines aggregate file and
byte counts; the configured layout and file count do not repartition it.
Staged reads remain time-based.

See Recipe 5 in [BENCHMARK_RECIPES_FILESYSTEM.md](BENCHMARK_RECIPES_FILESYSTEM.md).

#### Single Shared File (elbencho)

This mode benchmarks sequential I/O to one regular file. It requires one
`TEST_DIRS` entry, `ELBENCHO_SINGLE_BIG_FILE=1`, and
`ELBENCHO_SINGLE_BIG_FILE_SIZE` for generated data. Random I/O is rejected.

By default, elbencho partitions the file into non-overlapping host ranges.
`ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=1` makes every host process the complete
file, transferring `nodes * file_size` bytes. The default is direct I/O; pass
`-b` for buffered I/O.

With `--read-from`, pass a file, not a directory. Its metadata supplies the
extent, so `ELBENCHO_SINGLE_BIG_FILE_SIZE` is optional. Direct reads repeat
until the time limit; buffered reads make at most one logical pass to avoid
re-reading warm page cache. Host assignment rotates between read cells to
reduce cross-run client-cache reuse.

`utils/extract-elbencho.sh` handles these results normally. See Recipe 4 in
[BENCHMARK_RECIPES_FILESYSTEM.md](BENCHMARK_RECIPES_FILESYSTEM.md).

#### mdtest-elbencho (Metadata Testing with Elbencho)

This is the recommended metadata test. It runs create, stat, and delete phases
without MPI and rotates host assignment between phases to reduce client-cache
reuse. Configure `MDTEST_BRANCH_FACTOR`, `MDTEST_ITEMS_PER_DIR`, and
`MDTEST_ITERATIONS`, then run:

```bash
./storage-tests/fs/nv-mdtest-elbencho.sh \
    --nodes 1,2,4,8 --tasks 64,128
./utils/extract-mdtest-elbencho.sh \
    "$RESULTS_DIR"/mdtest-elbencho-<datestamp>/
```

**Dense single-directory runs (`--single-dir-file-target`):**

The default layout creates a branched tree. To measure contention in one flat
directory, pass `--single-dir-file-target <count>`:

```bash
./storage-tests/fs/nv-mdtest-elbencho.sh --nodes 2 --tasks 64 --single-dir-file-target 1000000
```

Dense mode requires one node count, one task count, and one generated target.
Every worker creates, stats, and deletes uniquely named zero-byte files in that
directory. Because elbencho assigns an integer count to each worker, the actual
total is `nodes * tasks * round(target / (nodes * tasks))`; requested and
actual counts are recorded.

### Filesystem reporting

Both analysis wrappers accept one or more result directories, filters, CSV
export/import, and `--markdown`. Elbencho reports IOPS or throughput and latency
by operation, size, thread count, node count, and I/O depth. Metadata reports
create/stat/delete rates, elapsed times, latency distributions, variance, and
scaling efficiency.

For per-client filesystem I/O diagnostics, set
`ELBENCHO_LIVE_CSV_EXTENDED=1` before the run and analyze with
`utils/extract-elbencho.sh --per-client-plots`. Extended capture can generate
large files at scale; tune `ELBENCHO_LIVEINT` deliberately.

## Network Tests

Network testing is **beta** and needs more work before it is ready for
at-scale usage. The available netbench test still provides useful TCP
throughput and latency measurements between small sets of clients.

It requires elbencho and at least two nodes. Permit peer traffic on
`NETBENCH_PORT` and that port plus 1000; `--bidirectional` also uses both ports
plus 1. Configure NIC speed, target runtime, block/response sizes, threads, and
iterations in `env.sh`, then run:

```bash
./storage-tests/network/nv-netbench.sh --nodes 2,4,8
./storage-tests/network/nv-netbench.sh --nodes 2,4,8 --bidirectional
```

Both modes split nodes into groups A and B without localhost traffic. The
default runs A→B and B→A sequentially; `--bidirectional` runs them
concurrently. Transfer size is scaled by thread count so a saturated NIC runs
for approximately `NETBENCH_TARGET_RUNTIME`.

Analyze `$RESULTS_DIR/netbench-{half|bidir}-<datestamp>/` with:

```bash
./utils/extract-netbench.sh "$RESULTS_DIR"/netbench-half-<datestamp>/
```

The analyzer reports throughput, latency distributions, variance, and scaling
efficiency in terminal tables and plots. It also supports Markdown, CSV
export/import, and scale filters; see `--help`.

## Object Storage Tests

Set `OBJ_BUCKET`, `OBJ_REGION`, `OBJ_HOST`, `OBJ_HOST_PORT`, and an absolute
`OBJ_AUTH_FILE` path in `env.sh`. The bucket must already exist and must contain
no valuable objects: the test cleans it and deletes its benchmark objects.
Store credentials in a mode-`0400` file:

```bash
export WARP_ACCESS_KEY="..."
export WARP_SECRET_KEY="..."
```

Configure object sizes, threads, PUT/GET durations, and the minimum object count
with `WARP_*` variables, run `validate_env.sh`, then establish a single-node
baseline before scaling:

```bash
./storage-tests/object/nv-warp-sweep.sh --nodes 1
./utils/extract-warp.sh "$RESULTS_DIR"/warp-<datestamp>/
./storage-tests/object/nv-warp-sweep.sh --nodes 1,2,4,8
```

For each node count and object size, the test cleans the bucket, PUTs until at
least `nodes * WARP_PUT_MIN_FILES_PER_CLIENT` objects exist, sweeps GET thread
counts, and performs final DELETE cleanup. Each node count is a separate Slurm
job or SSH invocation.

Optional modes are `--multipart`, `--ranged`, and `--s3-express`. Ranged reads
PUT one `WARP_RANGE_OBJ_SIZE` object per node and use `WARP_OBJ_SIZES` as range
sizes.
Aggregate PUT and GET request-rate budgets are available through
`WARP_RPS_BUDGET_PUT` and `WARP_RPS_BUDGET_GET`. `WARP_PREFIXES` selects static
prefixes and requires [NVIDIA/warp-minio](https://github.com/NVIDIA/warp-minio).

### Object storage reporting

`extract-warp.sh` generates terminal tables and PNG plots for throughput, TTFB
latency, and scaling efficiency. It supports size/thread filters,
`--per-client-plots`, `--to-json`, and `--from-json`. Repeat `--only-sizes` or
separate several sizes with `;`; commas are not split. Markdown output is not
implemented.

## Contributing

This project is not currently accepting external code contributions or pull
requests. Bug reports, documentation corrections, and feature suggestions are
welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) for details.

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
