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
`env.sh` using [override files](#workload-override-files), and measure several
filesystems or compare them in [testing several filesystems](#testing-several-filesystems).
Kubernetes users should also read the
[asynchronous workflow](#kubernetes-run-inspect-and-collect). Finish with
[reporting](#filesystem-reporting); tuned settings live in the
[benchmark recipes](../BENCHMARK_RECIPES_FILESYSTEM.md).

SSH and Slurm require an elbencho binary for each client architecture under
`utils/` and filesystems mounted at the same paths on every client. Kubernetes
uses the configured image and claim-qualified test roots instead; see
[Kubernetes setup](#kubernetes-setup). For SSH or Slurm, configure weighted
roots:

```bash
declare -A TEST_DIRS=(
    ["/mnt/fs1/scaletest"]=4
    ["/mnt/fs2/scaletest"]=1
)
```

Weights distribute load between unequal roots of one target; to measure
filesystems separately, see
[testing several filesystems](#testing-several-filesystems). Tune `FS_MAX_AGG_THROUGHPUT`,
`FS_MAX_NODE_THROUGHPUT_GBPS`, and `FS_MAX_NODE_IOPS` when using multiple
roots because they determine generated file counts and bounded write time.

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
./utils/extract-filesystem.py "$RUN"
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

Worker-directory phases are timed or run to completion as shown below.
Generated shared-directory and single-file datasets always run to completion.

| Configuration | Phase behavior |
|---|---|
| One weight-1 root, direct IO | Timed by default |
| Buffered IO, any root count | Complete writes and reads |
| Multiple distinct roots, direct IO | Complete writes and reads |
| `--run-to-completion` | Complete writes and reads |

Completion mode processes each requested phase's finite dataset once, without
a benchmark time limit. Buffered IO completes to avoid repeatedly measuring
the same warm page-cache data; this does not guarantee cold caches. Only staged
`--read-from` reads can still be time limited (direct IO without
`--run-to-completion`). Write-only modes still skip reads.

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

`TEST_DIRS` keys are `[namespace/]claim:path`, with the path relative to
the claim (see [claims and test roots](#claims-and-test-roots)); path
components accept letters, digits, `.`, `_`, and `-`. Durable state and
completed-cell data live in `.storage-scale-test` under the lexically first
root (bytewise, as `namespace/claim:path`), so do not use that name as a
component. Each claim is locked with a Kubernetes Lease object, so concurrent
sweeps on the same claim are blocked even with different test roots.

A Kubernetes invocation submits one asynchronous Job for the whole sweep and
prints commands to query, cancel, and collect it:

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --nodes 1,2,4
# Set RUN to the complete printed result-directory path.
./storage-tests/fs/nv-elbencho-sweep.sh --status "$RUN"
# Collect when --status prints NEXT_ACTION=COLLECT.
./storage-tests/fs/nv-elbencho-sweep.sh --collect "$RUN"
```

`--status` prints the same `KEY=value` lines for plain runs and batches; see the
[key table](#prepared-filesystem-batches).

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
3. Run `--delete-only <path>` (SSH and Slurm only). The path must be a strict
   descendant of the one `TEST_DIRS` root, from `env.sh` or one `--target FILE`.

To build a mixed-size tree, move retained targets below one common directory
under the configured test root, read that parent, then delete it once.
In Kubernetes, pass `--read-from` as `[namespace/]claim:path` below its
`TEST_DIRS` key (the namespace may be left out when it is
`KUBECTL_NAMESPACE`), and do not read the canonical test root itself or a
directory containing its reserved `.storage-scale-test` control subtree.

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

`utils/extract-elbencho.py` handles these results normally. See Recipe 4 in
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
./utils/extract-filesystem.py "$RUN"
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

Assemble IO and metadata sweeps before running them. Each `--batch` or
`--append` adds one **group** per `--target` (one group without `--target`),
including its workload settings and CLI flags:

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
./utils/extract-filesystem.py "$BATCH"
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
| `SEGMENT_COUNT` | Kubernetes, [several namespaces](#several-kubernetes-namespaces) only; one segment per namespace in the batch: the number of segments in this attempt. After a resume it can be smaller, because a namespace with nothing left to run is left out |
| `SEGMENT_CURRENT` | Several namespaces only: the index `i` (from 0, in run order) of the namespace running now, or of the last one that ran. While a cancel is incomplete, the one being cancelled |
| `SEGMENT_<i>_NAMESPACE` | Several namespaces only: that namespace's name |
| `SEGMENT_<i>_STATE` | Several namespaces only: `SUCCESS`, `FAILED`, `CANCELLED`, `RUNNING`, `PENDING` (prepared, its Job not yet started), or `NOT_STARTED` (waiting, or never run because an earlier one failed or was cancelled) |
| `GROUP_<NNNN>_KIND` | Batches only, one pair per committed group: `io` or `mdtest` |
| `GROUP_<NNNN>_TARGET` | Batches only: the group's recorded storage target name, shell-quoted (`unknown` when none) |

`--batch` and `--append` print these keys:

| Key | Meaning |
| --- | --- |
| `STORAGE_SCALE_TEST_BATCH_RESULTS` | The batch directory |
| `STORAGE_SCALE_TEST_BATCH_GROUP` | The new group's ID (the first new group's when there are several) |
| `STORAGE_SCALE_TEST_BATCH_GROUPS` | Several new groups only (one per `--target`): every new group ID, comma-separated, in command-line order |
| `STORAGE_SCALE_TEST_BATCH_GROUP_<gid>_TARGET` | Every new group: its storage target name, shell-quoted |
| `STORAGE_SCALE_TEST_BATCH_GROUP_TARGET` | Legacy; only for a single-group submission (same value as its `_<gid>_TARGET`) |
| `STORAGE_SCALE_TEST_BATCH_EXECUTIONS` | The new execution IDs, `<first>-<last>`, across every new group |

The keys are printed once the groups are committed. If the refresh of the
batch's combined environment fails after that, a warning goes to stderr and the
command still exits 0 with all keys printed; do not repeat the `--append`.
`--status` or `--start` repairs the batch.

Groups may differ in target, IO mode, sizes, durations, and metadata layout; to
compare filesystems in one batch, see
[testing several filesystems](#testing-several-filesystems). Everything else
about how the batch connects and allocates is frozen for the whole batch, and
`--append` names mismatched fields: substrate, executable identity,
architecture, ordering, host pool, and connection/allocation settings (on
Kubernetes, `KUBECTL_NAMESPACE` (the default namespace), the worker settings,
and the sequencer image, frozen as resolved at preparation). The claims and namespaces in each target's keys
may still differ. Start and resume use saved settings, not the current
`env.sh`; Kubernetes credentials remain the current client's. The durable
control directory is chosen from the group test roots (on Kubernetes, the
lexically first root of each namespace across its groups).

## Workload override files

`--env-override <file>` varies a workload without editing `env.sh`; it changes
how to test, never what to test (that is
[`--target`](#testing-several-filesystems)). Either launcher, IO or metadata,
accepts the flag on a new run, `--batch`, or `--append`, on any substrate. Each
appended group keeps its own override list, so a directory of small numbered
files and a short script can build a mixed batch:

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

The flag may repeat. The first file builds on `env.sh`; each later file builds on
the one before, so a later `ELBENCHO_SCALE_THREAD_LIST+=(4)` extends a list an
earlier file set, and a later assignment to the same scalar wins. Each group's list is independent of
the others. Giving the same file twice, even through a symlink or a different
spelling of its path, is an error: it would repeat every relative edit.

Each file is trusted Bash, sourced after `env.sh` in an isolated shell. It sees
the exported environment and the current values of the allowed settings, so
`+=` and `$((MDTEST_ITERATIONS + 1))` build on `env.sh`; unexported `env.sh`
helper variables are not visible. `unset NAME` gives a setting its usual
default. A relative file path resolves from the current directory. The file
must finish with status 0 and must not call `exit`.

Both launchers accept every allowed setting, so IO and metadata groups can
share a file:

- Indexed arrays, one value per element (no whitespace):
  `ELBENCHO_SCALE_THREAD_LIST`, `ELBENCHO_SCALE_IO_SIZES`,
  `ELBENCHO_IODEPTH_LIST`.
- Scalars: `MDTEST_{BRANCH_FACTOR,ITEMS_PER_DIR,ITERATIONS}`,
  `ELBENCHO_{FILE_SIZE_MULTIPLIER,FILE_LAYOUT,FILES_PER_NODE,FILE_SIZE}`,
  `ELBENCHO_{SCALE_READ_WRITE_DURATION,READ_AFTER_WRITE_PAUSE}`,
  `ELBENCHO_{LIVE_CSV_EXTENDED,LIVEINT,ALL_NODES_ACCESS_ALL_DATA}`,
  `ELBENCHO_SINGLE_BIG_FILE{,_BASENAME,_SIZE}`.

Setting `TEST_DIRS`, `STORAGE_TARGET_NAME`, or `FS_MAX_*` is an error that names
the file and the variable and says to pass the file with `--target` (it works
there as-is if it sets only those). Changing any other variable (a typo, a new
uppercase name, or a lowercase name that `env.sh` or the launcher already
defines) is an error, and the message lists every allowed name. A value of the
wrong type is also an error. Either fails before a results directory is created
or a group is appended. New lowercase variables, such as loop counters, are
discarded.

Each submission prints one line per file,
`Env override: <path> (sha256 <hash>): <changed variables | no settings changed>`;
with more than one file the prefix is `Env override <i>/<N>:`. The snapshots
record the same, in application order. In `env_used.sh`, the arrays
`STORAGE_SCALE_TEST_ENV_OVERRIDE_{FILES,SHA256S,FILE_VARIABLES}` hold one entry
per file (each `FILE_VARIABLES` entry is a space-separated list of names), and
the scalars `STORAGE_SCALE_TEST_ENV_OVERRIDE_{FILE,SHA256,VARIABLES}` keep their
single-file meaning: the last file applied, its hash, and the sorted union of
changed names. In `env_used.yaml`, `env_override` is `null` without an override;
otherwise `file`, `sha256`, and `variables` describe the last file and the
union, and `files` lists `{file, sha256, variables}` for every file in order.
Older snapshots with only the scalars read as a one-file list. Batch groups
record the operator's paths, not the staged copies, and the snapshot summary
shows the file basenames in order (`(+K more)` when the row would overflow),
beside the [target file](#target-files). `--start`, `--resume`, `--status`,
`--cancel`, `--collect`, and `--delete-only` reject the flag; lifecycle commands
use the saved values, so later edits to the files have no effect.

## Testing several filesystems

**What to test comes from `env.sh` or `--target`; how to test comes from
`env.sh` or `--env-override`.**

Terms: **root**: one `TEST_DIRS` key. **Target**: one `TEST_DIRS` map, the
filesystems one group measures. **Target file**: sets one target; passed with
`--target`. **Group**: one target's work in a batch. **Claim**: a Kubernetes
PersistentVolumeClaim. **Cell**: one node count x thread count x IO size x IO
depth combination. **Workload**: the settings from `env.sh` or `--env-override`.

| You want to | Configure | You get |
|---|---|---|
| Test one filesystem | `TEST_DIRS` with one root | one group |
| Spread load across several filesystems | `TEST_DIRS` with several weighted roots | one group, load split by weight |
| Compare several filesystems | one target file each, passed with `--target` | one group per target |

### Example: spread load 2:1 across lustre-a and lustre-b

In `env.sh` (size `FS_MAX_*` for both filesystems combined):

```bash
# SSH or Slurm: paths mounted on every client
TEST_DIRS=(["/mnt/lustre-a/scale-test"]=2 ["/mnt/lustre-b/scale-test"]=1)

# Kubernetes, instead: claim:path, both claims in KUBECTL_NAMESPACE
KUBECTL_NAMESPACE=validation
TEST_DIRS=(["lustre-a:scale-test"]=2 ["lustre-b:scale-test"]=1)
```

```bash
./storage-tests/fs/nv-elbencho-sweep.sh --nodes 1,2,4
```

You get:

```text
one run, one target: 2/3 of each cell's files on lustre-a, 1/3 on lustre-b
one report, target "/mnt/lustre-a/scale-test,/mnt/lustre-b/scale-test"
  (Kubernetes: "validation/lustre-a:scale-test,validation/lustre-b:scale-test")
```

### Example: compare vendor-a and vendor-b

One target file per filesystem, plus one workload file for both:

```bash
# targets/vendor-a.env  (targets/vendor-b.env: the same with vendor-b)
STORAGE_TARGET_NAME=vendor-a
TEST_DIRS=(["/mnt/vendor-a/scale-test"]=1)   # SSH or Slurm
#TEST_DIRS=(["vendor-a:scale-test"]=1)       # Kubernetes: claim in KUBECTL_NAMESPACE

# recipes/rand-4k.env
ELBENCHO_SCALE_IO_SIZES=("r4K")
ELBENCHO_IODEPTH_LIST=(1 16)
```

```bash
IO=./storage-tests/fs/nv-elbencho-sweep.sh
"$IO" --batch --target targets/vendor-a.env --target targets/vendor-b.env \
    --env-override recipes/rand-4k.env --nodes 1,2,4
# Set BATCH to the printed STORAGE_SCALE_TEST_BATCH_RESULTS path.
"$IO" --start "$BATCH"
# Kubernetes only: repeat until it prints NEXT_ACTION=COLLECT, then collect.
"$IO" --status "$BATCH"
"$IO" --collect "$BATCH"
./utils/extract-filesystem.py "$BATCH"
```

You get:

```text
group 0001  target vendor-a  workload rand-4k
group 0002  target vendor-b  workload rand-4k
reports/groups/0001/report.txt, reports/groups/0002/report.txt,
reports/index.md (with a Target column), and reports.txt
```

### Rules

- A target file sets only `TEST_DIRS` (required), `STORAGE_TARGET_NAME`, and
  `FS_MAX_*`; an override file sets only the
  [supported workload settings](#workload-override-files). Substrate,
  connection, and allocation settings stay in `env.sh`. A setting in the wrong
  file is an error that names the flag to use.
- A target file replaces `env.sh`'s `TEST_DIRS`; any `FS_MAX_*` it leaves
  unset comes from `env.sh`. The target's name comes from its own file or
  roots, never `env.sh`.
- Override files apply to every target in the submission. For a different
  workload per target, `--append` another submission.
- Each target gets its own reports; the extractors refuse to mix targets
  unless given `--allow-mixed-targets`.
- Several `--target` flags need `--batch` or `--append`. Groups run one at a
  time in the order added (on Kubernetes,
  [grouped by namespace](#several-kubernetes-namespaces)), and a failure stops
  the batch, so use one batch per target when one target's failure must not
  stop the others.

> **Kubernetes notes**
>
> - Roots are `[namespace/]claim:path`; the namespace defaults to
>   `KUBECTL_NAMESPACE`. See [claims and test roots](#claims-and-test-roots).
> - All roots of one target are in one namespace; claims in two namespaces are
>   two targets. Targets in one namespace need nothing extra.
> - Targets in several namespaces in one batch need permission for the tool
>   to create an in-cluster sequencer and its RBAC objects; see
>   [several Kubernetes namespaces](#several-kubernetes-namespaces). To avoid
>   that, prepare one batch per namespace.

Reference: [target files](#target-files), [naming](#naming-a-target),
[several namespaces](#several-kubernetes-namespaces),
[reporting per target](#reporting-per-target), and the batch
[status and output keys](#prepared-filesystem-batches).

## Storage target reference

### Target files

- **Settings.** `TEST_DIRS` (associative array, required),
  `STORAGE_TARGET_NAME`, `FS_MAX_AGG_THROUGHPUT`,
  `FS_MAX_NODE_THROUGHPUT_GBPS`, and `FS_MAX_NODE_IOPS`. Changing any other
  setting is an error that names the variable and says to use
  `--env-override`. The file is trusted Bash, loaded like an
  [override file](#workload-override-files).
- **Order, per group.** `env.sh`'s `STORAGE_TARGET_NAME` is cleared, the target
  file is applied over `env.sh`, then the `--env-override` files apply in
  order. A target is therefore named by its own file or its roots, never by
  `env.sh`'s name.
- **Where.** Both launchers accept `--target`:
  - Plain run: at most one (more is rejected, and the error prints the
    equivalent `--batch` and `--start` commands).
  - `--batch` and `--append`: any number; N flags add N groups, one each, in
    command-line order, committed together. The same file twice is an error.
  - `--start`, `--resume`, `--status`, `--cancel`, and `--collect` reject it;
    `--delete-only` takes at most one, to locate `TEST_DIRS`.
  - Without `--target`, the one target comes from `env.sh`; keep a valid
    `TEST_DIRS` there for `validate_env.sh`.
- **Mounts.** On SSH and Slurm, a batch uses one host pool, so every target's
  roots must be mounted on every client. On Kubernetes, each target's keys
  are checked when it is submitted and its claims when the batch starts.
- **Provenance.** `env_used.sh` records `STORAGE_SCALE_TEST_TARGET_FILE`,
  `STORAGE_SCALE_TEST_TARGET_FILE_SHA256`, and
  `STORAGE_SCALE_TEST_TARGET_FILE_VARIABLES` (empty without a target file);
  `env_used.yaml` records `target_file` as `null` or
  `{file, sha256, variables}`. A batch records your original path, and the
  snapshot summary shows the target file beside the override files.

### Naming a target

Every saved run records the name of its storage target in `env_used.yaml` and
`env_used.sh`. Set `STORAGE_TARGET_NAME` in `env.sh` or a target file to
choose it. When it is empty, the name is derived when the sweep starts: the
`TEST_DIRS` roots, sorted and joined with `,`.

| Substrate | Each root as |
| --- | --- |
| SSH, Slurm | The path, trailing `/` removed |
| Kubernetes | `namespace/claim:path`, with the namespace filled in |

Kubernetes results saved by v1.x keep their `<namespace>/<pvc>` name. An
explicit name is 1-128 characters from letters, digits, and `._:@+/-`,
starting with a letter or digit; `unknown` is reserved for results whose
target cannot be identified. An invalid name is rejected before any results
directory is created.

The name is metadata only. It never changes a path, directory, or group name;
it appears in report headers, the batch index, `--status`, and the CSV
exports (see [reporting per target](#reporting-per-target)).

### Several Kubernetes namespaces

Targets in one namespace share one set of workers and one coordinator that
mount all of their claims, and their groups run in the order they were added.
A batch whose targets are in more than one namespace runs one namespace at a
time, in the order each namespace first appears in the batch, and each
namespace's groups in the order they were added. For groups `0001` (ns-a),
`0002` (ns-b), and `0003` (ns-a), the order is `0001`, `0003`, `0002`.

- **How.** `--start` checks every claim, prepares every namespace's workers and
  a suspended coordinator Job, and rolls everything back if a claim cannot
  mount on a selected node. A **sequencer** Job in the first namespace then
  starts each coordinator when the previous one finishes; its ServiceAccount
  can only get and patch that attempt's coordinator Jobs, and its Pod is the
  only one in a sweep that mounts an API token. If it is lost, `--status`
  starts the next coordinator with your credentials.
- **Prerequisites.** Kubernetes 1.24 or newer; the sequencer image, by default
  a pinned `docker.io/alpine/kubectl` (set `KUBECTL_SEQUENCER_IMAGE` only to use
  a mirror: any digest-pinned image providing `sh`, `kubectl`, `grep`, and
  `sleep`); permission to create and delete a ServiceAccount in the first namespace,
  and Roles, RoleBindings, and Jobs (and to patch Jobs) in every namespace;
  and each namespace labeled `kubernetes.io/metadata.name` (automatic since
  Kubernetes 1.21). An attempt spans at most 16 namespaces and
  32 claims; `validate_env.sh` and `--start` reject more before creating
  anything. `--resume` checks only the namespaces that still have cells.
- **Validation.** `validate_env.sh` checks the RBAC, the server version, and
  that the sequencer image pulls and provides its tools (a short-lived Job, no
  API call) in `env.sh`'s one namespace; problems are warnings only, since a
  batch in one namespace needs none of it. `validate_env.sh --sequencer` instead
  runs the full probe of the real sequencer and treats problems as errors
  (raise `KUBECTL_SEQUENCER_PROBE_TIMEOUT_SECONDS`, default 120, for a slow
  pull). A set `KUBECTL_SEQUENCER_IMAGE` must be digest-pinned even when unused.
- **Progress.** `--status` adds `SEGMENT_*` keys, one set per namespace (see
  [prepared batches](#prepared-filesystem-batches)). `EXECUTIONS_*` counts
  span every namespace.
- **Failures.** A failed cell stops the batch: later namespaces show
  `NOT_STARTED` and `NEXT_ACTION` becomes `COLLECT`. Worker drift fails the
  attempt the same way. `--cancel` stops the sequencer and the running
  namespace, and never overwrites a namespace that already finished.
- **Results.** One `--collect` gathers every namespace and cleans up.
  `run.status` and `run-summary.tsv` cover the whole attempt: `SUCCESS` only
  when every namespace succeeded, otherwise the unsuccessful outcome (`FAILED`
  after worker drift, not `CANCELLED`); `run-summary.tsv` adds `segments` and
  `segment` rows, one per namespace. Each namespace also has its own
  `run-<namespace>.status`, and `run-summary-`, `startup-error-`, and
  `coordinator-loss-` files named the same way.
- **Resume.** `--resume` skips successful cells. A namespace with nothing
  left gets no workers, Job, or Lease, and with one namespace left no
  sequencer is used.

The [Kubernetes lifecycle contract](KUBERNETES_ELBENCHO_LIFECYCLE.md) has the
details.

### Reporting per target

Reports are per target; there is no combined cross-target report.

- **Batches.** Each group has its own report, and groups are single-target, so
  reports never mix targets. `reports/index.md` has a `Target` column, and the
  combined `reports.txt` heads every group `Group 0001 (io) target: "<name>"`.
  Current snapshots always record the effective name, so `(derived)` appears
  only beside results saved before this feature, which never recorded one.
  Index cells are escaped, so odd characters in a name show literally.
- **Report headers.** IO and metadata reports print a `Storage target:` line
  after the version lines, and the IO settings summary leads its Targets row
  with `name=` (a long row is shortened with an ellipsis or `(+K more)`).
- **Mixing guard.** The three extractors (`extract-elbencho.py`,
  `extract-mdtest-elbencho.py`, `extract-filesystem.py`) exit 1 before writing
  anything when their inputs record different target names, because IO tables
  would merge and metadata results would be averaged. Derived names count:
  different `TEST_DIRS` roots are different targets unless they share a
  `STORAGE_TARGET_NAME`. `--allow-mixed-targets` overrides with a warning and a
  `Storage target: mixed: "a", "b"` header, and aggregate tables still average
  across targets. Unknown-target inputs (older results) beside a known one only
  warn.
- **Stitching.** `--to-csv` adds a `storage_target_name` column (empty, a name,
  or `mixed: ...`), so per-target CSVs can be concatenated and filtered outside
  the tool. Reloading a concatenated CSV with `--from-csv` is a mixed report and
  needs `--allow-mixed-targets`. Older checkouts cannot read CSVs with the
  column.

## Filesystem reporting

Use `./utils/extract-filesystem.py "$RUN"` as the unified front door for
ordinary IO or metadata runs and mixed batches. It dispatches each kind to
its analyzer without combining unlike metrics.

Each group has separate artifacts and reports, so repeated coordinates never
overwrite or average across groups. All tables for one group are saved in
`reports/groups/<group-id>/report.txt`; individual tables do not have separate
text files. `reports/index.md` links the group reports and lists cell states.
Report only selected groups with `--groups 0001,0003`, choose
`--kind io|mdtest|all`, or redirect reports with `--output-dir PATH`.
Reports include successful cells only; Kubernetes results must first be
collected. Both existing specialized reporters accept batches and select their
own workload kind.

When at least two groups have valid reports, the reporter also writes
`reports.txt` at the batch root and links it from `reports/index.md`. This file
collects the groups' tables in manifest order without combining their metrics.
Each IO group begins with one compact summary of its effective saved settings,
targeting about six lines. It covers the runner, targets, configured sweep,
file layout and sizing, lifecycle, and a `Setting files:` row naming the
target and override files; inactive optional settings are omitted. The summary
abbreviates: long names and lines are shortened and long lists end in
`(+N more)`. To check that groups used equivalent settings, compare their
complete `env_used.yaml` snapshots, which the `Saved settings` path and
`reports/index.md` link.
Table titles remain adjacent to their tables. Metadata reports include the raw
saved YAML once before the entire workload group report, keeping configuration,
rates, elapsed times, latency tables, and commands together. Individual group
`report.txt` files remain unchanged. Both plain-text and `--markdown` reports
are supported. With `--output-dir PATH`,
the combined file is `PATH/reports.txt`, alongside `PATH/index.md` and
`PATH/groups/`.

The unified reporter also accepts the established analysis options. Common
options such as `--only-nodes` and `--markdown` apply to both kinds;
`--normalize-to` applies only to metadata and `--per-client-plots` only to IO.
An option with no matching selected group is an error. Filtered runs update
selected reports and rebuild the index and combined file, retaining valid
reports from other groups in the same batch. Failed, stale, or unowned reports
are excluded; when fewer than two valid group reports remain, `reports.txt`
is removed:

```bash
./utils/extract-filesystem.py --normalize-to 1 --only-nodes 2,4 "$BATCH"
./utils/extract-filesystem.py --kind io --per-client-plots "$BATCH"
```

Results from different storage targets are never combined silently; see
[reporting per target](#reporting-per-target).

Use `--help` for all options. Cached CSV input and single-file parsing require
`--kind io|mdtest` and are not batch-reporting modes.

| Scope | Option | Purpose |
| --- | --- | --- |
| Both | `--only-nodes LIST`, `--only-threads LIST` | Select counts with comma lists or ranges |
| Both | `--to-csv`, `--from-csv FILE` | Export/reload analyzed metrics |
| Both | `--markdown` | Markdown on stdout; progress on stderr |
| Both | `--test-parse FILE` | Inspect one raw result without reporting |
| Both | `--allow-mixed-targets` | Report inputs from different [storage targets](#reporting-per-target) anyway |
| IO | `--only-sizes SIZE` | Repeat or separate with `;`; preserve compound-size commas |
| IO | `--only-iodepths LIST` | Select depths with comma lists or ranges |
| IO | `--no-dual-y-axis` | Separate throughput and latency axes |
| IO | `--per-client-plots` | Analyze extended live client capture |
| IO | `--client-outlier-threshold Z` | Underperformance z-score magnitude (default 2.0) |
| IO | `--client-min-underperform-segments N` | Require N underperforming live intervals (default 1) |
| IO | `--client-max-timeseries-lines N` | Limit client lines per plot (default 10) |
| IO | `--client-max-heatmap-rows N` | Limit clients per heatmap (default 50) |
| Metadata | `--normalize-to N` | Scale rates/stddev to N nodes, not latency |

For several result directories at once, use `extract-elbencho.py` or
`extract-mdtest-elbencho.py`, which accept the same filters, CSV
export/import, and `--markdown`. IO reports show IOPS or throughput and
latency by operation, size, thread count, node count, and I/O depth. Metadata
reports show create/stat/delete rates, elapsed times, latency distributions,
variance, and scaling efficiency.

For per-client filesystem I/O diagnostics, set
`ELBENCHO_LIVE_CSV_EXTENDED=1` before the run and analyze with
`utils/extract-elbencho.py --per-client-plots`. Extended capture can generate
large files at scale; tune `ELBENCHO_LIVEINT` deliberately.

## Advanced node selection and Slurm options

Use tuned clients with enough aggregate bandwidth to saturate the target.
A starting node count is `1.1 * 8 * FS_MAX_AGG_THROUGHPUT / measured_per_client_Gbps`
(`FS_MAX_AGG_THROUGHPUT` is in GB/s; the 8 converts it to Gbps).

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
`ORDER_NODES=1`, and pass the suggested `--nodes` list from stderr to a sweep. SSH then takes the
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

## Kubernetes setup

The tool provisions no cluster, namespace, PV, or PVC. Set the
[README Kubernetes configuration](../README.md#kubernetes), then check the
claims and the cluster below.

### Claims and test roots

Each `TEST_DIRS` key names its claim: `[namespace/]claim:path`.

```bash
KUBECTL_NAMESPACE=validation
# Use one of:
TEST_DIRS=(["scale-test:data"]=1)                # claim scale-test
TEST_DIRS=(["lustre-a:scale-test"]=2 ["lustre-b:scale-test"]=1)  # two, 2:1
TEST_DIRS=(["vendor-b/data:scale-test"]=1)       # claim data in vendor-b
```

- `claim` is a PersistentVolumeClaim; `path` is a directory below the
  claim's root. A key without `namespace/` uses `KUBECTL_NAMESPACE`; leave
  that empty only if every key names its namespace.
- All roots of one target share one namespace; a map that spans namespaces is
  rejected. Compare namespaces with [`--target`](#testing-several-filesystems).
- Every Pod mounts each claim at `/mnt/storage-scale-test/<claim>`, so
  `claim:path` is `/mnt/storage-scale-test/<claim>/<path>` inside the Pod. Do
  not write that prefix in `TEST_DIRS`.

### Cluster prerequisites

- Each claim exists in its namespace and is Bound, ReadWriteMany, and
  Filesystem mode. The tool records the PV it is bound to and fails if that
  changes during an attempt.
- The selector matches enough Ready, schedulable nodes of the intended
  architecture for your largest node count. Inspect labels with
  `kubectl get nodes --show-labels`; comma-separated equality labels are ANDed,
  for example `storage-scale-test/worker=true,storage-tier=lustre`.
- Every selected node can mount every claim. Each `TEST_DIRS` path already
  exists and is writable by the configured positive UID/GID; `2000:2000` is an
  example, not a required identity. The claim's root need not be writable.
- The CNI provides cross-node Pod IPv4 connectivity on TCP 1611 and enforces
  NetworkPolicies.
- Admission policy allows `Unconfined` seccomp for elbencho validation, worker,
  and coordinator Pods; Linux AIO needs it. These Pods still run as the
  configured non-root UID/GID, disable privilege escalation, drop all
  capabilities, and mount no API token. Helper Pods keep more restrictive
  settings. The one exception is the sequencer Pod used across
  [several namespaces](#several-kubernetes-namespaces).
- The submitting identity can:
  - inspect nodes, the namespace, PV/PVC, workload resources, and events;
  - create, get, and delete Jobs, Pods, DaemonSets, ConfigMaps,
    NetworkPolicies, and namespaced Leases;
  - read Pod logs and exec into Pods.

  The coordinator itself has no Kubernetes API credentials.
- The image supports your nodes and is usable under the pull policy with any
  required registry credentials. `Never` requires preloading; `Always`
  requires a digest-pinned reference.

`validate_env.sh` then checks each claim and runs a temporary Job that mounts
them all, to check the image, workload identity, write access to each root,
and seccomp mode.

### Upgrading from v1.8.0

```bash
# v1.8.0
export KUBECTL_PVC=scale-test
TEST_DIRS=(["data"]=1)
# v2.0.0: the claim moves into the key; delete KUBECTL_PVC and KUBECTL_PV
TEST_DIRS=(["scale-test:data"]=1)
```

- If `KUBECTL_PVC` or `KUBECTL_PV` is still set, or a key has no `claim:`,
  `validate_env.sh` and the launchers stop with an error that prints the
  replacement `TEST_DIRS` line: `KUBECTL_PVC` supplies the claim, otherwise a
  `<claim>` placeholder stands in. Nothing is printed when only `KUBECTL_PV` is
  set and the keys are qualified.
- v2.0.0 cannot inspect, cancel, collect, or resume Kubernetes runs started
  by v1.x. Before upgrading, wait for or `--cancel` each in-progress run and
  `--collect` it with v1.x. Collected results still report.
- On every substrate, an `--env-override` file that sets `TEST_DIRS`,
  `STORAGE_TARGET_NAME`, or `FS_MAX_*` is now rejected: pass that file with
  `--target` instead, and move any workload settings in it to an override
  file. On Kubernetes, also qualify its keys as above.

## Kubernetes access and troubleshooting

`validate_env.sh` also lists, informationally, any Jobs, DaemonSets, Pods,
NetworkPolicies and Leases labeled `app.kubernetes.io/name=storage-scale-test`
(all namespaces, falling back to `KUBECTL_NAMESPACE` if cluster-wide listing is
forbidden), and prints nothing for this when there are none unless `--verbose`.
Existing objects never fail validation, and the label does not prove
ownership: they may belong to an active or uncollected attempt. Find the
result directory whose `kubernetes/attempts/<run-id>/` matches the listed RUN
and use `--status`, `--collect`, or `--cancel`. Delete manually only after the
object's ownership annotation matches the nonce recorded in that attempt.

On failure, read the diagnostic bundle path and safe next action printed by
validation or the sweep. Common causes are:

| Evidence | Check |
| --- | --- |
| Image inspect/pull failure | Fully qualified image, architecture, pull policy, registry credentials |
| Claim permission failure | Test-root ownership and configured workload UID/GID |
| `FailedMount` | Node-side CSI/storage setup; changing the benchmark image cannot fix a pre-container mount failure |
| Admission rejection | Namespace policy and requested resource/security settings |
| Service readiness failure | Direct Pod IPv4 connectivity on TCP 1611, CNI policy and service logs |
| Interrupted collection | Retry `--collect` to complete exact cleanup |

Keep the saved local state and remote resources while investigating. Do not
bypass ownership checks by deleting the Lease or editing the ledger.
The [Kubernetes lifecycle contract](KUBERNETES_ELBENCHO_LIFECYCLE.md)
defines supported recovery, diagnostics, and remaining acceptance gates.
