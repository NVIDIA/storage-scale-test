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

# Filesystem testing

Configure and validate your launcher first using the
[getting-started workflow](../README.md#getting-started). Commands below run
from the repository root or unpacked deployment directory. Use dedicated test
paths: these tests create, overwrite, and delete data.

Start with [IO](#first-io-sweep) or [metadata](#metadata-sweeps), or assemble a
[mixed batch](#prepared-filesystem-batches); vary workloads without editing
`env.sh` using [override files](#workload-override-files). Kubernetes users
should also read the
[asynchronous workflow](#kubernetes-run-inspect-and-collect). Finish with
[reporting](#filesystem-reporting); tuned settings live in the
[benchmark recipes](../BENCHMARK_RECIPES_FILESYSTEM.md).

SSH and Slurm require an elbencho binary for each client architecture under
`utils/` and filesystems mounted at the same paths on every client. Kubernetes
uses the configured image and logical PVC-relative test roots instead; see
[configuration](../README.md#kubernetes). For SSH or Slurm, configure weighted
roots:

```bash
declare -A TEST_DIRS=(
    ["/mnt/fs1/scaletest"]=4
    ["/mnt/fs2/scaletest"]=1
)
```

Weights distribute load between unequal targets. Tune `FS_MAX_AGG_THROUGHPUT`,
`FS_MAX_NODE_THROUGHPUT_GBPS`, and `FS_MAX_NODE_IOPS` when using multiple
targets because they determine generated file counts and bounded write time.

## First IO sweep

Set `TEST_DIRS` and the elbencho variables in `env.sh`, then run
`validate_env.sh`; to vary a workload without editing `env.sh`, use an
[override file](#workload-override-files). `ELBENCHO_SCALE_IO_SIZES`,
`ELBENCHO_SCALE_THREAD_LIST`, and `ELBENCHO_IODEPTH_LIST` form a Cartesian
product for every requested node count. An IO-size entry may be `4K`, `r4K`,
or a write/read pair such as `1M,r4K`; see `env.sh.template`. For a short
smoke test, use a small product:

```bash
export ELBENCHO_SCALE_IO_SIZES=("r4K" "1M")
export ELBENCHO_SCALE_THREAD_LIST=("1")
export ELBENCHO_IODEPTH_LIST=("1")
export ELBENCHO_SCALE_READ_WRITE_DURATION=10
```

Ten seconds checks operation, not sustained performance. For timed direct IO,
60 seconds suits exploration; 300 seconds or more is a typical starting point
for publishable runs.

Start with one node, analyze it, then retain only useful thread, size, and I/O
depth values for the scale sweep:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --nodes 1
# Set RUN to the complete result-directory path printed by the sweep.
# Kubernetes: collect terminal results before reporting (see below).
./utils/extract-filesystem.sh "$RUN"
./storage-tests/fs/nv-elbencho-sweep.sh --nodes 1,2,4,8
```

All sweep scripts accept comma-separated positive integers and ascending
inclusive ranges: `X`, `X-Y`, or `X-Y+Z`; order is preserved. The endpoint is
always included: `3-10+2` expands to `3,5,7,9,10`. Run a script with `--help`
for its full mode and argument contract.

The default lifecycle is mkdir, write, read, and cleanup.
`--write-no-read` skips the read and still cleans up; the
[retained-dataset modes](#retained-datasets) keep, reread, or delete a dataset.
Slurm uses one coordinator allocation sized to the largest node count; Slurm
and SSH run cells sequentially. Size Slurm `run_time` for the complete product
and all phases, startup, pauses, and cleanup.

Ordinary sweeps default to direct IO; `-b/--bio` selects buffered IO.
`-r/--rand` makes write and read access random; `r`-prefixed IO-size entries
can instead select random access per phase. Random IO is incompatible with
single-shared-file mode. A sole `TEST_DIRS` root with weight other than 1 is
rejected.

Worker-directory phases are timed or run to completion as shown below;
generated shared-directory and single-file datasets always run to completion.

| Configuration | Phase behavior |
|---|---|
| One weight-1 root, direct IO | Timed by default |
| Buffered IO, any root count | Complete writes and reads |
| Multiple distinct roots, direct IO | Complete writes and reads |
| `--run-to-completion` | Complete writes and reads |

Completion mode processes each requested phase's finite dataset once, without
a benchmark time limit. Buffered IO completes to avoid repeatedly measuring
the same warm page-cache data; this does not guarantee cold caches. Staged
`--read-from` reads follow the same rule: direct IO without
`--run-to-completion` remains time-limited. Write-only modes still skip reads.

### Finite dataset sizing

For a sized finite workload, prefer an explicit per-node file-count budget and
file size:

```bash
TEST_DIRS=(["/path/to/test"]=1)
export ELBENCHO_FILES_PER_NODE=8
export ELBENCHO_FILE_SIZE=1G
export ELBENCHO_SCALE_THREAD_LIST=("1" "4")
# Worker-directory layout; each cell writes and reads nodes * 8 GiB.
./storage-tests/fs/nv-elbencho-sweep.sh --run-to-completion --nodes 1,2
```

In worker-directory mode, each thread visits every weighted target. With
`G = threads * sum(TEST_DIRS weights)`, the per-node count is the requested
budget rounded to the nearest multiple of G (ties up, minimum G); for example,
budget 1,000, 16 threads, and weights 2:1 produce 1,008 files per node, split
672:336. Each cell prints requested and effective counts, which can vary with
the thread count.

Explicit counts in worker-directory mode require completion mode. Generated
shared-directory mode is always completion-based and keeps exact counts: one
root with weight 1, with the count at least and divisible by every thread
count. Neither setting redefines a staged read dataset. `ELBENCHO_FILE_SIZE`
fixes each generated file's size; otherwise, size is the write block size
times `ELBENCHO_FILE_SIZE_MULTIPLIER`.

With the count unset, the file count is sized from `FS_MAX_AGG_THROUGHPUT`,
`FS_MAX_NODE_THROUGHPUT_GBPS`, `FS_MAX_NODE_IOPS`, file/block sizes, topology,
and `ELBENCHO_SCALE_READ_WRITE_DURATION`, with at least one file per thread per
target. In completion mode that duration sizes the dataset but sets no
deadline, so a large dataset can take a long time; in timed direct IO mode,
reads retain the time limit.

## Kubernetes: run, inspect, and collect

`TEST_DIRS` paths are relative to the PVC (see
[Kubernetes prerequisites](#kubernetes-prerequisites)); path components
accept letters, digits, `.`, `_`, and `-`. Durable state and completed-cell
data live in `.storage-scale-test` under the lexically first root, so do not
use that name as a component. A per-PVC Lease blocks concurrent sweeps on the
same claim, even with different test roots.

A Kubernetes invocation submits one asynchronous Job for the whole sweep and
prints commands to query, cancel, and collect it:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --nodes 1,2,4
# Set RUN to the complete printed result-directory path.
./storage-tests/fs/nv-elbencho-sweep.sh --status "$RUN"
# Wait for NEXT_ACTION=COLLECT before collecting.
./storage-tests/fs/nv-elbencho-sweep.sh --collect "$RUN"
```

The metadata launcher offers the same lifecycle commands. The Job runs without
your credentials, but lifecycle commands need current `kubectl` access.

- `--status` exits 0 whenever the query succeeds, whatever the benchmark
  outcome.
- `--cancel "$RUN"` stops an active attempt; collect it afterward.
- `--collect` is required after every terminal attempt: it copies results and
  diagnostics locally and cleans up. For a failed or cancelled attempt it
  publishes partial results, then exits nonzero.
- `--resume "$RUN"` requires collection first and, with the same Kubernetes
  configuration, reruns only unfinished cells.
- A failure mid-cell can lose that cell's partial output, but never records it
  as successful.

The [Kubernetes lifecycle contract](KUBERNETES_ELBENCHO_LIFECYCLE.md) covers
the details.

## Resume after failure

The sweep records each cell separately and stops at the first failure. After
correcting the cause, continue it with:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --resume "$RUN"
```

`--resume` must be the only argument. It restores the original environment and
CLI modes from the result directory, skips successful cells, resets stale
running cells, and dispatches the remainder in their original order; on
Kubernetes, [collect first](#kubernetes-run-inspect-and-collect). Do not run
concurrent resumes. A Slurm resume also refuses to reset work while its prior
coordinator may still be active. Resume sources shell files in the result
directory, so use only a trusted, unmodified result directory.

## Retained datasets

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
In Kubernetes, do not read the canonical test root itself or a directory
containing its reserved `.storage-scale-test` control subtree.

## Shared-directory checkpoint files

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
ignore duration and verify exact file and byte counts. Normal and
`--write-no-read` runs delete with distributed elbencho workers;
`--write-only` retains the dataset. `ELBENCHO_FILE_SIZE` is optional; otherwise
the write block size and `ELBENCHO_FILE_SIZE_MULTIPLIER` determine it. Direct or
random I/O requires the file size to be divisible by the effective block size.

With `--read-from <directory>`, the scanned tree defines aggregate file and
byte counts; the configured layout and file count do not repartition it. Direct
IO staged reads are time-limited by default; buffered IO or
`--run-to-completion` reads the scanned dataset to completion.

See Recipe 5 in [BENCHMARK_RECIPES_FILESYSTEM.md](../BENCHMARK_RECIPES_FILESYSTEM.md).

## Single shared file

This mode benchmarks sequential I/O to one regular file. It requires one
`TEST_DIRS` entry, `ELBENCHO_SINGLE_BIG_FILE=1`, and
`ELBENCHO_SINGLE_BIG_FILE_SIZE` for generated data. Random I/O is rejected.

By default, elbencho partitions the file into non-overlapping host ranges.
`ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=1` makes every host process the complete
file, transferring `nodes * file_size` bytes. The default is direct I/O; pass
`-b` for buffered I/O.

With `--read-from`, pass a file, not a directory. Its metadata supplies the
extent, so `ELBENCHO_SINGLE_BIG_FILE_SIZE` is optional. Direct reads repeat
until the time limit; buffered IO or `--run-to-completion` reads the file once,
to completion (see the [IO policy](#first-io-sweep)). Host assignment rotates
between read cells to reduce cross-run client-cache reuse.

`utils/extract-elbencho.sh` handles these results normally. See Recipe 4 in
[BENCHMARK_RECIPES_FILESYSTEM.md](../BENCHMARK_RECIPES_FILESYSTEM.md).

## Metadata sweeps

This is the recommended metadata test. It runs create, stat, and delete phases
without MPI and rotates host assignment between phases to reduce client-cache
reuse. Configure `MDTEST_BRANCH_FACTOR`, `MDTEST_ITEMS_PER_DIR`, and
`MDTEST_ITERATIONS`, then run:

```bash
./storage-tests/fs/nv-mdtest-elbencho.sh --nodes 1,2,4,8 --tasks 64,128
# Set RUN to this sweep's complete printed result-directory path.
# Kubernetes: collect terminal results before reporting.
./utils/extract-filesystem.sh "$RUN"
```

Each `(nodes, tasks)` pair is saved as a numbered execution. SSH and Slurm
run these executions in order and can continue after interruption:

```bash
./storage-tests/fs/nv-mdtest-elbencho.sh --resume "$RUN"
```

With `EXECUTION_SUBSTRATE=kubectl`, submission returns while a coordinator Job
runs the sweep. Use `--status`, `--cancel`, and `--collect` with the same result
directory. Collect a terminal attempt before using `--resume`.

### Dense single-directory runs

The default layout creates a branched tree. To measure contention in one flat
directory, pass `--single-dir-file-target <count>`:

```bash
./storage-tests/fs/nv-mdtest-elbencho.sh --nodes 2 --tasks 64 --single-dir-file-target 1000000
```

Dense mode requires one node count, one task count, and one generated target.
Every worker creates, stats, and deletes uniquely named zero-byte files in that
directory. The actual total is `workers * max(1, round(target / workers))`,
where `workers = nodes * tasks` and ties round up: target 1 with two nodes and
64 tasks creates 128 files. Requested and actual counts are recorded.

## Prepared filesystem batches

Assemble IO and metadata sweeps before running them. Each invocation saves one
**group**, including its workload settings and CLI flags:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --batch --nodes 1,2
# Set BATCH to the printed STORAGE_SCALE_TEST_BATCH_RESULTS path.
# Vary each group's workload with an override file (see the next section).
./storage-tests/fs/nv-mdtest-elbencho.sh --append "$BATCH" \
    --env-override overrides/md-deep-tree.env --nodes 2,4 --tasks 4,8
./storage-tests/fs/nv-elbencho-sweep.sh --status "$BATCH"
./storage-tests/fs/nv-mdtest-elbencho.sh --start "$BATCH"
# Kubernetes: wait for terminal status, then collect before reporting.
# After a failure, collect first on Kubernetes, then resume:
# ./storage-tests/fs/nv-elbencho-sweep.sh --resume "$BATCH"
# Once results are local:
./utils/extract-filesystem.sh "$BATCH"
```

Either launcher can start or resume the complete batch on SSH, Slurm, or
Kubernetes. `--resume` also starts a draft batch. First start permanently seals
the execution set: append is rejected thereafter, even if submission fails.
Existing ordinary runs cannot be converted to batches. Execution stops at the
first failure; resume skips successful cells and sizes resources for the
remaining work. Initial start acquires capacity for the entire batch's maximum
node requirement. `--batch` creates a new directory and takes no directory
argument; only `--append` adds work to an existing draft.

`--status` prints `KEY=value` lines. Wait for `NEXT_ACTION=COLLECT` before
collecting; `STATE=UNKNOWN` is not success.

| Key | Meaning |
| --- | --- |
| `BATCH` | `DRAFT` until first start, then `SEALED` |
| `ATTEMPT` | Kubernetes only: current attempt ID |
| `STATE` | `BETWEEN_EXECUTIONS`: work pending, no cell running; `AWAITING_COMPLETION`: no cells left, outcome not yet committed; `UNKNOWN`: executor ownership unknown |
| `EXECUTION_SCOPE` | `BATCH`: full SSH/Slurm or draft ledger; `CURRENT_ATTEMPT`: current Kubernetes attempt, excluding cells completed before resume |
| `PROGRESS_SOURCE` | Ledger read: `LOCAL` or `PVC` |
| `EXECUTIONS_{TOTAL,PENDING,RUNNING,SUCCEEDED,FAILED}` | Cell counts within that scope |
| `RESULT_COLLECTION` | `PENDING` (results remote), `CLEANUP_PENDING` (published, cleanup needed), `COMPLETE`; SSH/Slurm: `NOT_REQUIRED` |
| `NEXT_ACTION` | `START`, `WAIT`, `COLLECT`, `RESUME`, `INSPECT`, or `NONE` |

Groups may use different test roots, weights, IO modes, sizes, durations, and
metadata layouts. The substrate, executable identity, architecture, ordering,
host pool, and selected substrate's connection/allocation settings are frozen;
append names mismatched fields. Start and resume use saved settings, not the
current `env.sh`. Kubernetes credentials remain those of the current client.
Its durable control directory is chosen from the union of all group test roots.

## Workload override files

`--env-override <file>` varies a workload without editing `env.sh`. Either
launcher, IO or metadata, accepts one file on a new run, `--batch`, or
`--append`, on any substrate. Each appended group keeps its own override, so a
directory of small numbered files and a short script can build a mixed batch:

```bash
# overrides/10-small-random.env
ELBENCHO_SCALE_IO_SIZES=("r4K" "r16K")
ELBENCHO_IODEPTH_LIST=(1 16)
ELBENCHO_SCALE_THREAD_LIST+=(512)   # extends the list from env.sh
```

```bash
#!/usr/bin/env bash
set -euo pipefail
IO=./storage-tests/fs/nv-elbencho-sweep.sh
MD=./storage-tests/fs/nv-mdtest-elbencho.sh
BATCH=$("$IO" --batch --env-override overrides/10-small-random.env \
    --nodes 1,2,4,8 | sed -n 's/^STORAGE_SCALE_TEST_BATCH_RESULTS=//p')
test -n "$BATCH"
"$IO" --append "$BATCH" --env-override overrides/20-large-seq.env --nodes 1,8
"$MD" --append "$BATCH" --env-override overrides/30-deep-tree.env \
    --nodes 2,4 --tasks 64,128
"$IO" --start "$BATCH"
```

The file is trusted Bash, sourced after `env.sh` in an isolated shell. It sees
the exported environment and the current values of the allowed settings, so
`+=` and `$((MDTEST_ITERATIONS + 1))` build on `env.sh`; unexported `env.sh`
helper variables are not visible. `unset NAME` gives a setting its usual
default. A relative file path resolves from the current directory. The file
must finish with status 0 and must not call `exit`.

Both launchers accept every allowed setting, so IO and metadata groups can
share a file:

- `TEST_DIRS`: associative array.
- Indexed arrays, one value per element (no whitespace):
  `ELBENCHO_SCALE_THREAD_LIST`, `ELBENCHO_SCALE_IO_SIZES`,
  `ELBENCHO_IODEPTH_LIST`.
- Scalars: `FS_MAX_{AGG_THROUGHPUT,NODE_THROUGHPUT_GBPS,NODE_IOPS}`,
  `MDTEST_{BRANCH_FACTOR,ITEMS_PER_DIR,ITERATIONS}`,
  `ELBENCHO_{FILE_SIZE_MULTIPLIER,FILE_LAYOUT,FILES_PER_NODE,FILE_SIZE}`,
  `ELBENCHO_{SCALE_READ_WRITE_DURATION,READ_AFTER_WRITE_PAUSE}`,
  `ELBENCHO_{LIVE_CSV_EXTENDED,LIVEINT,ALL_NODES_ACCESS_ALL_DATA}`,
  `ELBENCHO_SINGLE_BIG_FILE{,_BASENAME,_SIZE}`.

Changing any other variable (a typo, a new uppercase name, or a lowercase name
that `env.sh` or the launcher already defines) is an error, and the message
lists every allowed name. A value of the wrong type is also an error. Either
fails before a results directory is created or a group is appended. New
lowercase variables, such as loop counters, are discarded.

Each submission prints
`Env override: <path> (sha256 <hash>): <changed variables | no settings changed>`
and records the same in `env_used.sh`
(`STORAGE_SCALE_TEST_ENV_OVERRIDE_{FILE,SHA256,VARIABLES}`) and `env_used.yaml`
(`env_override`). `--start`, `--resume`, `--status`, `--cancel`, `--collect`,
and `--delete-only` reject the flag; lifecycle commands use the saved values,
so later edits to the file have no effect.

## Filesystem reporting

Use `./utils/extract-filesystem.sh "$RUN"` as the unified front door for
ordinary IO or metadata runs and mixed batches. It dispatches each kind to
its analyzer without combining unlike metrics.

Each group has separate artifacts and reports, so repeated coordinates never
overwrite or average across groups. `reports/index.md` links the group reports
and lists cell states. Report only selected groups with `--groups 0001,0003`,
choose `--kind io|mdtest|all`, or redirect reports with `--output-dir PATH`.
Reports include successful cells only; Kubernetes results must first be
collected. Both existing specialized reporters accept batches and select their
own workload kind.

The unified reporter also accepts the established analysis options. Common
options such as `--only-nodes` and `--markdown` apply to both kinds;
`--normalize-to` applies only to metadata and `--per-client-plots` only to IO.
An option with no matching selected group is an error. Filtered runs update
selected reports without dropping other groups from the index:

```bash
./utils/extract-filesystem.sh --normalize-to 1 --only-nodes 2,4 "$BATCH"
./utils/extract-filesystem.sh --kind io --per-client-plots "$BATCH"
```

Use `--help` for all options. Cached CSV input and single-file parsing require
`--kind io|mdtest` and are not batch-reporting modes.

| Scope | Option | Purpose |
| --- | --- | --- |
| Both | `--only-nodes LIST`, `--only-threads LIST` | Select counts with comma lists or ranges |
| Both | `--to-csv`, `--from-csv FILE` | Export/reload analyzed metrics |
| Both | `--markdown` | Markdown on stdout; progress on stderr |
| Both | `--test-parse FILE` | Inspect one raw result without reporting |
| IO | `--only-sizes SIZE` | Repeat or separate with `;`; preserve compound-size commas |
| IO | `--only-iodepths LIST` | Select depths with comma lists or ranges |
| IO | `--no-dual-y-axis` | Separate throughput and latency axes |
| IO | `--per-client-plots` | Analyze extended live client capture |
| IO | `--client-outlier-threshold Z` | Underperformance z-score magnitude (default 2.0) |
| IO | `--client-min-underperform-segments N` | Require N underperforming live intervals (default 1) |
| IO | `--client-max-timeseries-lines N` | Limit client lines per plot (default 10) |
| IO | `--client-max-heatmap-rows N` | Limit clients per heatmap (default 50) |
| Metadata | `--normalize-to N` | Scale rates/stddev to N nodes, not latency |

For several result directories at once, use `extract-elbencho.sh` or
`extract-mdtest-elbencho.sh`, which accept the same filters, CSV
export/import, and `--markdown`. IO reports show IOPS or throughput and
latency by operation, size, thread count, node count, and I/O depth. Metadata
reports show create/stat/delete rates, elapsed times, latency distributions,
variance, and scaling efficiency.

For per-client filesystem I/O diagnostics, set
`ELBENCHO_LIVE_CSV_EXTENDED=1` before the run and analyze with
`utils/extract-elbencho.sh --per-client-plots`. Extended capture can generate
large files at scale; tune `ELBENCHO_LIVEINT` deliberately.

## Advanced node selection and Slurm options

Use tuned clients with enough aggregate bandwidth to saturate the target.
A starting node count is `1.1 * target_Gbps / measured_per_client_Gbps`.

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
expanded nodes.

Smaller bins add sweep points; larger bins provide more balancing freedom.

`SLURM_JOB_NAME_PREFIX` prefixes job names. `SLURM_EXTRA_ARGS` is a Bash array
appended after generated sbatch/srun options, so later duplicate options can
override earlier ones while arguments with spaces remain intact:

```bash
SLURM_EXTRA_ARGS=("--constraint=ib" "--comment=storage validation run")
```

`SLURM_EXCLUSIVE_USER=1` uses `--exclusive=user` and derives
`--cpus-per-task` when the target CPU count is available; the default uses
`--exclusive`.

## Kubernetes prerequisites

The tool provisions no cluster, namespace, PV, or PVC. After setting the
[README Kubernetes configuration](../README.md#kubernetes), confirm:

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
- Admission policy allows `Unconfined` seccomp for elbencho validation, worker,
  and coordinator Pods; Linux AIO needs it. These Pods still run as the
  configured non-root UID/GID, disable privilege escalation, drop all
  capabilities, and mount no API token. Helper Pods keep more restrictive
  settings.
- The submitting identity can inspect nodes, the namespace, PV/PVC, workload
  resources and events; create, get, and delete Jobs, Pods, DaemonSets,
  ConfigMaps, NetworkPolicies, and namespaced Leases; and read Pod logs and
  exec into Pods. The coordinator itself has no Kubernetes API credentials.
- The image supports your nodes and is usable under the pull policy with any
  required registry credentials. `Never` requires preloading; `Always`
  requires a digest-qualified reference.

`validate_env.sh` then runs a temporary Job to check the image, workload
identity, PVC access, and seccomp mode.

## Kubernetes access and troubleshooting

`validate_env.sh` also lists, informationally, any Jobs, DaemonSets, Pods,
NetworkPolicies and Leases labeled `app.kubernetes.io/name=storage-scale-test`
(all namespaces, falling back to `KUBECTL_NAMESPACE` if cluster-wide listing is
forbidden). Existing objects never fail validation, and the label does not
prove ownership: they may belong to an active or uncollected attempt. Find the
result directory whose `kubernetes/attempts/<run-id>/` matches the listed RUN
and use `--status`, `--collect`, or `--cancel`. Delete manually only after the
object's ownership annotation matches the nonce recorded in that attempt.

On failure, read the diagnostic bundle path and safe next action printed by
validation or the sweep. Common causes are:

| Evidence | Check |
| --- | --- |
| Image inspect/pull failure | Fully qualified image, architecture, pull policy, registry credentials |
| PVC permission failure | Test-root ownership and configured workload UID/GID |
| `FailedMount` | Node-side CSI/storage setup; changing the benchmark image cannot fix a pre-container mount failure |
| Admission rejection | Namespace policy and requested resource/security settings |
| Service readiness failure | Direct Pod IPv4 connectivity on TCP 1611, CNI policy and service logs |
| Interrupted collection | Retry `--collect` to complete exact cleanup |

Keep the saved local state and remote resources while investigating. Do not
bypass ownership checks by deleting the Lease or editing the ledger.
The [Kubernetes lifecycle contract](KUBERNETES_ELBENCHO_LIFECYCLE.md)
defines supported recovery, diagnostics, and remaining acceptance gates.
