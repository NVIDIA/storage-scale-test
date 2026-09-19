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

# Elbencho filesystem sweep integration coverage gaps

## Scope and method

This note compares the integration harness as of 2026-09-19 with the complete
filesystem sweep and reporting interfaces implemented by:

- `env.sh.template` and `lib/env_base.sh`;
- `storage-tests/fs/nv-elbencho-sweep.sh`;
- `lib/env_functions.sh` and `lib/_elbencho_functions.sh`; and
- `utils/extract-elbencho.sh` and `utils/extract-elbencho.py`.

The focus is functional integration coverage. Unit and shell tests reduce risk,
but tests with a mocked Elbencho or dispatcher are not counted as end-to-end SSH
or Slurm coverage.

The configuration space is not a simple Cartesian product. Several variables
select a branch and make other values invalid, ignored, or operationally inert.
This note therefore distinguishes:

- **covered**: the value changes behavior that the integration test executes
  and meaningfully asserts;
- **executed but weakly asserted**: the code runs, but weak postconditions allow
  a semantically wrong result to pass;
- **set but inert**: the harness sets a value that the selected workload does
  not consume; and
- **not covered**: no integration case exercises the behavior.

## Executive conclusion

The harness covers deployment packaging, environment validation,
service/coordinator startup, one- and two-node worker selection, result
retrieval, and basic reporting over SSH and Slurm on amd64 and arm64. It does
**not** broadly cover sweep workloads. Every integration sweep is one buffered,
sequential, generated `shared-directory` workload with one 16 MiB file per node,
one thread, one I/O depth, and the default write/read/delete lifecycle. The
default `worker-directories` and direct-I/O path is absent, as are
failure/resume, retained or staged datasets, single-big-file mode, random and
split I/O, live CSV capture, weighted targets, and nearly all reporting options.

The current report assertion is especially permissive: a successful command
with a `Nodes` header and rows beginning with `1` and `2` passes. It does not
prove that WRITE and READ were both reported, metrics are correct, workload
metadata survived extraction, or expected plots were created.

## What the integration suite tests now

The environment generator in
`integration-tests/lib/filesystem_integration.py:577-638` fixes one bounded
configuration. `_run_substrate()` invokes only:

```text
./storage-tests/fs/nv-elbencho-sweep.sh -b --nodes 1,2
```

The resulting effective matrix is:

| Axis | Covered value |
|---|---|
| Execution substrate | Passwordless SSH and Slurm |
| CI architecture | amd64 and arm64 |
| Node count | Literal list `1,2` |
| Node selection | `ORDER_NODES=1`; first node, then both nodes |
| Access mode | Buffered I/O (`-b`) |
| I/O pattern | Sequential |
| Lifecycle | Generated mkdir, write, read, distributed delete |
| Layout | `ELBENCHO_FILE_LAYOUT=shared-directory` |
| Targets | One `TEST_DIRS` root with weight 1 |
| Dataset | One 16 MiB file per node |
| Sweep dimensions | One I/O size (`4K`), thread count (`1`), and depth (`1`) |
| Live capture | Disabled |
| Single-big-file mode | Disabled |
| SSH identity | Explicit user `tester` |
| Slurm selection | Two explicit include nodes, empty ignore list |
| Slurm allocation | CPU client, `partition=all`, bare `--exclusive` |
| Reporting | One raw result directory at a time, `--markdown` |

The harness meaningfully verifies:

- the deployment tarball and `env.sh.template` can be used from an extracted
  deployment;
- `validate_env.sh` succeeds in both environments;
- the sweep dispatches through SSH and through a real Slurm coordinator;
- the one-node cell selects the first configured worker and the two-node cell
  selects both workers;
- exactly two execution status files exist and both end in `SUCCESS` with exit
  code zero;
- each execution has a log and workload record;
- dataset totals are one file/16777216 bytes and two files/33554432 bytes;
- at least two nonempty aggregate CSV and human-readable output files exist;
- generated target directories are removed after the default lifecycle; and
- the report wrapper exits successfully and emits a node header plus apparent
  one- and two-node rows.

The workflow also checks setup idempotence, stop/start, rejection of root test
execution, teardown idempotence, and both architecture jobs. These checks do not
expand the sweep matrix. CI uses separate SSH homes; shared homes are optional
in the harness but not a CI axis.

## Configuration precedence and inactive-value semantics

These relationships are important when selecting future cases. Merely changing
a value does not necessarily exercise it.

1. A nonempty `SSH_HOST_LIST` selects SSH and disables Slurm. Slurm account,
   reservation, partition, runtime, module, include/ignore, exclusivity, and
   extra-argument settings cannot affect that run. This selection happens in
   `lib/env_base.sh:90-105`.
2. `TEST_DIR` is a compatibility fallback only when `TEST_DIRS` is unset or
   empty. A populated `TEST_DIRS` always wins (`lib/env_base.sh:58-67`).
3. `ORDER_NODES=1`, `yes`, or `true` selects deterministic prefixes. Other
   values select the normal behavior. For Slurm, deterministic prefixing only
   has an ordered list to use when `SLURM_NODE_INCLUDES` is nonempty
   (`lib/env_base.sh:81-88,221-230`).
4. `ELBENCHO_FILE_SIZE` overrides size derivation from the write block size and
   `ELBENCHO_FILE_SIZE_MULTIPLIER`. The integration harness sets both, so the
   multiplier is inert (`lib/_elbencho_functions.sh:737-763`).
5. Generated `shared-directory` work is exact-completion work. Its duration is
   reported as not applicable and it does not use `--timelimit` or `--infloop`.
   The integration's one-second duration is therefore inert for its measured
   phases (`lib/_elbencho_functions.sh:2901-2947`).
6. `-s/--single` is active only for a generated legacy
   `worker-directories` workload. It is intentionally inactive for
   `shared-directory`, `--read-from`, and single-big-file runs.
7. Multiple generated target paths, whether from multiple `TEST_DIRS` roots or
   a weight above one, take the same computed fixed-file-count branch as
   `--single`. That is where the `FS_MAX_*` estimates matter
   (`lib/_elbencho_functions.sh:3161-3194`).
8. `--read-from` selects a staged-dataset reader before either generated
   many-file layout. Current layout, files-per-node, generated file size, and
   multiplier do not define the staged dataset. The exact treefile defines its
   file and byte totals (`lib/_elbencho_functions.sh:2952-2968,2610-2643`).
9. `ELBENCHO_SINGLE_BIG_FILE=1` takes precedence over staged-directory and
   generated-layout dispatch. It requires one root and sequential I/O and is
   incompatible with `shared-directory`. With `--read-from <file>`, the file
   supplies the extent and `ELBENCHO_SINGLE_BIG_FILE_SIZE` is optional and
   unused for that read (`lib/env_functions.sh:3360-3387`).
10. `ELBENCHO_ALL_NODES_ACCESS_ALL_DATA` is valid only in single-big-file mode;
    value `1` adds Elbencho `--nosvcshare`. It is invalid elsewhere
    (`lib/_elbencho_functions.sh:785-797,1100-1148`).
11. `-r/--rand` makes every applicable phase random. An `r` prefix inside one
    `ELBENCHO_SCALE_IO_SIZES` component can independently force only WRITE or
    READ random. There is no per-size marker that cancels a global `-r`.
12. Buffered timed reads omit `--infloop`; direct timed reads add `--direct` and
    `--infloop` (`lib/_elbencho_functions.sh:3140-3159`). Upstream defines
    `--infloop` as restarting completed worker workloads and `--direct` as
    avoiding buffering/caching; see the upstream [changelog](https://github.com/breuner/elbencho/blob/master/CHANGELOG.md)
    and [generated large-file help](https://github.com/breuner/elbencho/blob/master/docs/usage/help-large.md).
13. `--write-only`, `--write-no-read`, `--read-from`, and `--delete-only` are
    mutually exclusive. A positive read-after-write pause is irrelevant to
    write-only, write-no-read, and read-from operations.
14. `--resume` is exclusive with every other CLI flag. It restores the original
    environment and CLI choices from `env_used.sh`; the caller's current sweep
    values are not supposed to redefine the remaining cells.

## Environment-variable coverage gaps

### Dispatch, placement, and deployment variables

| Variable or group | Current coverage | Gap |
|---|---|---|
| `RESULTS_DIR`, `LOGS_DIR` | Nondefault absolute fixture paths work. | Relative, unusual, and unwritable paths are not tested. |
| `SSH_HOST_LIST` | A simple newline-delimited two-IP file selects SSH. | Comma-separated and whitespace-separated entries, comments, blank lines, hostnames, duplicates, an empty file, unavailable hosts, and setting SSH alongside nonempty Slurm values are not integrated. |
| `SSH_USER` | Explicit `tester`. | Default/SSH-config identity and a wrong user are not tested. |
| `SSH_HOMEDIR_SHARED` | The harness can provision either mode, but CI uses separate homes. | No required two-mode matrix proves the different copy/staging behavior and a complete sweep in both modes. |
| `ORDER_NODES` | Only numeric true with Slurm includes and an SSH host list. | Disabled/random SSH selection, true/false aliases, Slurm without includes, changing subsets, and include ordering are absent. |
| `account`, `reservation`, `partition`, `run_time` | Empty account/reservation, `partition=all`, five-minute limit in Slurm only. | Explicit account/reservation, alternate or empty partition, time-limit propagation, and proof that all are ignored in SSH mode are absent. |
| `MODULES` | No custom module is required; defaults are attempted only if present. | Loading a real configured module, missing-module behavior, and SSH-mode irrelevance are not tested. |
| `SLURM_NODE_INCLUDES` | Simple two-line node list. | Unset/empty behavior, compressed hostlists, multiple entries per line, malformed names, ordering disabled, and capacity shorter than the requested node count are absent. |
| `SLURM_NODE_IGNORES` | Present but empty. | Real exclusions, compressed hostlists, overlap/conflict with includes, and resulting capacity checks are absent. |
| `SLURM_EXTRA_ARGS` | Empty array. | Multiple elements, an element containing spaces, duplicate/overriding scheduler options, invalid options, and consistent `sbatch`/`srun` propagation are absent. |
| `SLURM_EXCLUSIVE_USER` | `0`, producing bare `--exclusive`. | `1`/`yes`/`true`, target CPU discovery, `--cpus-per-task`, discovery failure, and execution inside an existing allocation are absent. |
| `SLURM_JOB_NAME_PREFIX` | Empty. | Nonempty naming and scheduler-safe unusual characters are absent. |
| `client_type` | `cpu`. | `gpu`, its four-GPU request, and GPU-only partition behavior are absent. |
| `client_arch` | amd64 and arm64 in separate CI jobs. | Configured/probed mismatch and wrong-binary validation are not integration cases. |

### Filesystem target and workload variables

| Variable or group | Current coverage | Gap and consequence |
|---|---|---|
| `TEST_DIRS` | One root, weight 1. | Multiple roots, weights above one, mixed weights, empty/invalid weights, and special-mode rejection are absent. These cases change targets and select computed file counts. |
| Legacy `TEST_DIR` | Not used. | Fallback when `TEST_DIRS` is empty and non-override when it is populated are not integrated. |
| `FS_MAX_AGG_THROUGHPUT` | Set to 1 but inert. | Aggregate-bandwidth limiting of computed file counts is absent. The compatibility fallback from `IOR_FS_MAX_AGG_THROUGHPUT` is also absent. |
| `FS_MAX_NODE_THROUGHPUT_GBPS` | Set to 1 but inert. | Per-node bandwidth limiting, scale with node count, and rounding are absent. |
| `FS_MAX_NODE_IOPS` | Set to 100 but inert. | IOPS-limited computed counts and the choice of IOPS versus bandwidth limit are absent. |
| `ELBENCHO_SCALE_THREAD_LIST` | One value, `1`. | Multiple values, Cartesian ordering, high counts, invalid/zero values, shared-file divisibility, and files-per-worker behavior are absent. |
| `ELBENCHO_SCALE_IO_SIZES` | One sequential `4K`. | Multiple values, `rSIZE`, split `WRITE,READ`, independently random components, malformed values, exact block divisibility, and Cartesian ordering are absent. |
| `ELBENCHO_IODEPTH_LIST` | One value, `1`. | Multiple depths, depth greater than one, invalid/zero values, and interaction with thread count and shared files are absent. |
| `ELBENCHO_SCALE_READ_WRITE_DURATION` | Set to 1 but inert in generated shared-directory mode. | Time-bounded worker-directory write/read, staged reads, single-file reads, validation, and actual timeout behavior are absent. |
| `ELBENCHO_READ_AFTER_WRITE_PAUSE` | Zero. | Positive pause, ordering around the pause, and interruption during the pause are absent. |
| `ELBENCHO_FILE_LAYOUT` | Only `shared-directory`. | The default `worker-directories` implementation, invalid values, and the layout's special interactions with read-from and single-file mode are absent. |
| `ELBENCHO_FILES_PER_NODE` | `1`. | Unset default, nontrivial counts, counts smaller than threads, indivisible counts, large boundary values, and rejection outside shared-directory are absent. |
| `ELBENCHO_FILE_SIZE` | Explicit `16M`. | Unset/derived size, alternate exact sizes, direct/random block divisibility, and invalid values are absent. |
| `ELBENCHO_FILE_SIZE_MULTIPLIER` | Set to 1 but shadowed by explicit file size. | Derived sizing and multiplier effects are entirely absent. |
| `ELBENCHO_LIVE_CSV_EXTENDED` | `0`. | Native live CSV production, retrieval, aggregate/service rows, large-artifact behavior, and report discovery are absent. |
| `ELBENCHO_LIVEINT` | Default value is parsed but live capture is off. | A nondefault interval, the below-250 ms warning, invalid values, and cadence in real output are absent. |
| `ELBENCHO_SINGLE_BIG_FILE` | `0`. | The whole generated and staged single-file branch is absent. |
| `ELBENCHO_SINGLE_BIG_FILE_BASENAME` | Default but inert. | Custom basename, path construction, cleanup, and unsafe/unusual names are absent. |
| `ELBENCHO_SINGLE_BIG_FILE_SIZE` | Empty and inert. | Required generated extent, exact size behavior, and optional/ignored read-from extent are absent. |
| `ELBENCHO_ALL_NODES_ACCESS_ALL_DATA` | `0` and inert outside single-file mode. | Both values and `--nosvcshare` propagation are absent. |

Existing shell tests cover many validators, exact counters, cleanup traps,
treefile helpers, and single-file helpers with mocked calls. The integration
gap is entrypoint-to-substrate composition with the real Elbencho binary.

## Sweep command-line coverage gaps

The parser is defined in `storage-tests/fs/nv-elbencho-sweep.sh:53-205`. Only
`-b` and the literal `--nodes 1,2` form have integration coverage.

| CLI interface | Semantics | Missing integration coverage |
|---|---|---|
| No `-b` (default DIO) | Adds `--direct` and, for timed reads, `--infloop`. | The default access path, alignment/exactness, direct-I/O failures, and report labeling. |
| `-b`, `--bio` | Adds `--norandalign` and omits read `--infloop`. | Short form is used, but native argv and report label are not asserted; the long alias is not tested. |
| `-r`, `--rand` | Makes applicable WRITE and READ phases random. | Both aliases, phase argv, interaction with per-size `r`, BIO/DIO, and rejection in single-big-file mode. |
| `-s`, `--single` | Forces computed file counts for one generated legacy target. | Active worker-directory behavior, `FS_MAX_*` sizing, cleanup, and documented no-op branches. |
| `--nodes X` | One node count. | Zero, negative, and nonnumeric rejection is absent. |
| `--nodes X-Y` | Inclusive ascending range. | Expansion, execution ordering, allocation size, and bad descending range. |
| `--nodes X-Y+Z` | Stepped range that always includes the stop. | Non-dividing steps, steps larger than the range, zero step, and ordering. |
| Comma node list | Preserves specified order, including a descending list. | Only `1,2`; mixed ranges, descending lists, duplicates, empty elements, and maximum-capacity rejection are absent. |
| `--write-only` | Retains one unique dataset per cell and emits its path. | Retention, emitted path, absence of READ/delete, safe later reuse/deletion, special one-root/weight rule, and both substrates. |
| `--write-no-read` | Writes then deletes without READ. | Missing-READ artifact/report semantics, distributed RMFILES, cleanup evidence, and both substrates. |
| `--read-from <directory>` | Reads an operator dataset via scan or cached treefile. | Real cache miss, atomic publication, cache hit, stale-cache operator contract, mount-root fallback, exact totals, no mutation/deletion, random reads, and both substrates. |
| `--read-from <file>` with single-file mode | Reads file extent from metadata. | Optional size, no treescan, sequential enforcement, and both substrates. |
| `--delete-only <path>` | Deletes one strict descendant on one compute node; `--nodes` is irrelevant. | Successful SSH/Slurm deletion, preservation of root/siblings, root and outside-root rejection, symlink/realpath safety, and accepted-but-irrelevant `-b`/`-r`/`-s`/`--nodes`. |
| `--resume <result-dir>` | Restores saved settings, resets stale RUNNING cells, skips SUCCESS, and retries remaining cells in order. | A real interrupted or failed sweep, mixed statuses, snapshot fidelity, lock ownership, Slurm live-job/accounting checks, SSH host reselection, repeated resume, malformed snapshots, and successful reporting of retry artifacts. |
| `-h`, `--help` | Prints usage without environment work. | Both aliases and stable documented interface. |
| Invalid CLI | Missing values, unknown options, positional arguments, missing `--nodes`, conflicting path modes, or resume plus another flag must fail. | No integration/entrypoint-level contract matrix. Many are cheap tests that do not require a live cluster. |
| Invocation inside Slurm | Direct invocation with `SLURM_JOB_ID` and no SSH is rejected. | Rejection behavior and the SSH-enabled exception. |

The current two-cell run also does not exercise the full reification product.
Multiple node counts, I/O sizes, threads, and depths should demonstrate stable
execution numbering, unique test suffixes, read-host rotation, and result
association across more than one varying axis.

## Failure, recovery, and lifecycle gaps

Successful cells cover only the easiest state transition:
`PENDING -> RUNNING -> SUCCESS`. No real integration scenario covers:

- a benchmark phase returning nonzero;
- incomplete exact counters despite process exit zero;
- stop-on-first-failure with later cells left pending;
- cleanup after mkdir, WRITE, READ, or RMFILES failure;
- a worker service dying between phases and Slurm service restart;
- an SSH worker failing startup and being pruned from the usable pool;
- signal handling while a shared-directory target is active;
- stale `RUNNING` recovery;
- dispatch-lock contention or stale lock recovery;
- Slurm coordinator disappearance versus a still-live allocation;
- result retrieval failing after remote benchmark success; or
- a successful resume that preserves earlier successes and removes duplicate
  or stale artifacts from the retried cell.

Several of these have focused shell tests in
`tests/test_elbencho_dispatch_shell.py` and
`tests/test_elbencho_shared_directory_signals.py`. Those tests use stubs and
synthetic sentinels. They do not establish the end-to-end contract among the
entrypoint, service processes, remote files, scheduler state, and reporter.

## Reporting CLI coverage gaps

The reporting parser is at `utils/extract-elbencho.py:4410-4507`. The only
wrapper-level integration call is `--markdown <one-result-dir>`.

| Argument or input mode | Current status | Gap |
|---|---|---|
| Positional `input_dirs` | One directory per invocation. | Multiple directories, duplicate coordinates/datestamps, relative paths, empty/missing/unwritable directories, and first-directory output selection. |
| `--markdown` | Command succeeds; header and node-like rows are checked. | WRITE/READ sections, operation/config labels, metrics, workload metadata, representative command, image references, and exact row cardinality are not asserted. |
| Default terminal mode | Not invoked end to end. | Terminal table content, stdout mirroring to `report.txt`, and consistency with Markdown. |
| `--to-csv` | Not invoked through the CLI. | Creation of `elbencho-metrics.csv`, complete schema, histogram serialization, errors, and round-trip fidelity. |
| `--from-csv FILE` | Not invoked through the CLI. | CSV-only reports/plots, legacy optional columns, malformed required fields, filters, Markdown, and output-directory choice. |
| `--from-csv FILE` plus input dirs | Not covered and semantically unclear. | Help says “instead of” raw parsing, but implementation currently combines CSV metrics with parsed directories, risking duplicate rows. The intended contract needs a test or a validation error. |
| `--only-threads` | Not invoked. | Exact values, comma lists, inclusive ranges, malformed tokens, and empty matches. |
| `--only-nodes` | Not invoked. | Same, including proof that aggregate and live inputs are filtered consistently. |
| `--only-sizes` | Parser helper has unit tests; CLI is not invoked. | Repeated flags, semicolon lists, compound sizes such as `1M,r64K`, malformed input, and exact matching. |
| `--only-iodepths` | Not invoked. | Exact/list/range/error and empty-match behavior. |
| `--test-parse FILE` | Not invoked. | Base, `.csv`, and `.out` paths; missing partner; malformed files; histogram output; and intentional precedence over otherwise supplied report options. |
| `--no-dual-y-axis` | Default false path may generate plots, but no files are asserted. | Expected single-axis versus dual-axis filenames and graph content. |
| `--per-client-plots` | No live input exists. | CLI discovery, second-pass client selection, summary CSV/text, time series, heatmaps, missing clients, counter resets, and failover. |
| `--client-outlier-threshold` | Only the default is parsed; effect is inactive. | Nondefault threshold and zero/negative rejection through the CLI. |
| `--client-min-underperform-segments` | Only the default is parsed; effect is inactive. | Nondefault selection and zero/negative rejection. |
| `--client-max-timeseries-lines` | Only the default is parsed; effect is inactive. | Truncation/selection and zero/negative rejection. |
| `--client-max-heatmap-rows` | Only the default is parsed; effect is inactive. | Row limiting and zero/negative rejection. |

The report also lacks integration inputs for every sweep branch absent above:
worker directories, direct/random or split I/O, multiple dimensions,
write-only, no-read, staged directory and file reads, single-big-file,
all-nodes-all-data, and resumed attempts. Those modes affect grouping, labels,
duration interpretation, deduplication, and workload metadata.

Existing unit tests substantially reduce parser risk:

- `tests/test_extract_elbencho_resume.py` covers workload joins, resumed CSV
  deduplication, and last output sections;
- `tests/test_extract_elbencho_terminal_subgroups.py` covers subgroup keys and
  some mode-sensitive display rules;
- `tests/test_extract_elbencho_treescan_scan.py` covers treescan artifact
  matching;
- `tests/test_extract_elbencho_live_csv.py` covers the live-analysis library;
  and
- `tests/test_parse_only_sizes.py` covers the size-filter parser.

They mostly call Python functions directly. They do not cover `main()` argument
composition, shell-wrapper virtual-environment setup, real result discovery,
or end-to-end output artifacts for those options.

## False-pass weaknesses in the current integration assertions

The present assertions are useful smoke checks, but several regressions could
pass:

1. Two successful statuses and plausible dataset totals do not prove the
   intended native Elbencho argv. A regression could drop `--norandalign`, use
   the wrong block size/depth/thread count, or omit host rotation.
2. The workload checks validate only total files, total bytes, and overall
   completion. They do not assert WRITE, READ, and delete completion states and
   counters, cleanup timing, files per worker, or layout/source fields.
3. `find ... | wc -l` requires at least two `.csv` and `.out` files, not the
   exact expected set associated with execution IDs 0001 and 0002.
4. `env_used.yaml` and `env_used.sh` need only be nonempty. Snapshot omissions,
   wrong CLI flags, wrong arrays, wrong `TEST_DIRS`, or values leaking from the
   current `env.sh` would not be detected before a resume is attempted.
5. Ordered-worker checking parses “starting execution” log lines. It does not
   independently inspect the actual SSH or `srun` command, and later duplicate
   lines for a node count overwrite earlier entries in its dictionary.
6. Cleanup checks only top-level names matching
   `elbencho-sweep-target-*`. A wrongly named leaked dataset or a retained
   single-file artifact would escape the check.
7. The reporter needs only emit a `Nodes` header and any rows that begin with 1
   and 2. It could omit an operation, report wrong values, duplicate stale
   metrics, lose workload metadata, or fail to create plots and still pass.
8. No deliberate bad artifact is injected to verify that result and report
   assertions fail rather than accepting partial output.
9. The reporter logs a bad benchmark pair and continues. One valid pair can
   therefore hide incomplete parsing.
10. Invalid integer filter tokens are skipped, and an empty aggregate filter
    set is treated as no filter. Filtering all aggregate metrics can also exit
    successfully with “No metrics to display.”
11. `--from-csv` plus input directories combines both sources despite help text
    that says CSV is used “instead,” allowing unnoticed duplicate metrics.
12. Missing optional metadata can reduce report detail without violating the
    current node-row assertion.

## Stack-ranked gaps to close

The ranking below considers expected frequency, consequence of silent error,
amount of code unique to the branch, existing lower-level coverage, and the
cost of adding a case. It is a value ranking, not a recommendation to create a
full cross product.

1. **Default `worker-directories` plus direct I/O on SSH and Slurm.** This is
   the shipped default and executes a large branch that the integration suite
   currently bypasses: timed mkdir/write/read, derived file size, tree scan,
   host rotation, and recursive cleanup. A small DIO case should assert exact
   argv/metadata and both WRITE and READ report rows.
2. **Strengthen the existing result and report oracle.** Before multiplying
   cases, require the current case to prove exact artifact names, snapshot
   values, phase counters/states, expected metric count and values, BIO label,
   both operations, workload metadata, and plot artifacts. This closes the
   largest “tests run but can pass incorrectly” risk at modest runtime cost.
3. **Real failure followed by `--resume` on both substrates.** Long sweeps are
   expensive, making partial-run recovery operationally critical. Exercise
   stop-on-first-failure, preserved SUCCESS, stale or FAILED retry, lock
   behavior, artifact replacement/deduplication, and final reporting.
4. **`--write-only` -> `--read-from` -> `--delete-only` lifecycle.** One chained
   scenario can validate retained path publication, a real cache miss and hit,
   read-only preservation, safe explicit deletion, and report annotations.
   Root/outside-root rejection should be a mandatory negative check because
   delete-only is destructive.
5. **`--write-no-read` and failure cleanup.** This verifies absence of READ,
   distributed RMFILES evidence, no leaked data, and correct reporting of a
   write-only metric without conflating the mode with retained write-only data.
6. **Single-big-file mode at one and two nodes.** Cover cooperative slicing,
   `ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=1`, custom basename/size, generated
   cleanup, staged file read with inferred extent, and random/shared-layout
   rejection. Bugs here can silently change how much of a shared file each
   service accesses.
7. **A small multi-dimensional random/split-I/O matrix.** Vary at least two I/O
   sizes (including independently random WRITE/READ), two threads, and two I/O
   depths in one bounded run. Assert Cartesian numbering, phase argv, host
   rotation, result association, report grouping, and filters.
8. **Weighted/multiple roots and active `-s` sizing.** Exercise the default
   compatibility model where `FS_MAX_*` selects file counts, including one
   bandwidth-limited and one IOPS-limited case. This is materially different
   from exact shared-directory completion.
9. **Extended live CSV through reporting.** Generate real live files on SSH and
   Slurm, then run aggregate live reporting and `--per-client-plots` with
   nondefault limits. Live capture can become very large and is an important
   diagnostic path, but its analysis library already has substantial unit
   coverage.
10. **Slurm option variants with scheduling consequences.** Test
    `SLURM_EXCLUSIVE_USER=1` and CPU derivation first, then an extra argument
    containing spaces, include/exclude interaction, and a nonempty job prefix.
    GPU GRES should follow when a suitable runner exists.
11. **Reporting persistence and output modes.** Add CLI round-trip coverage for
    `--to-csv`/`--from-csv`, default terminal/report.txt output,
    `--no-dual-y-axis`, exact plot sets, and multiple result directories.
12. **SSH parsing, selection, and shared homes.** Make separate and shared home
    modes explicit CI cases or a sequential subcase, test host-file syntax, and
    cover `ORDER_NODES=0`. This matters, but core SSH orchestration already has
    a real two-worker happy path.
13. **CLI and validation error matrix.** Help, missing arguments, bad ranges,
    conflicting modes, random single-file rejection, and invalid filters are
    cheap and should be exhaustive. Most do not need the expensive kind
    fixture and belong in entrypoint-level tests rather than the full workflow.
14. **Less common deployment variants.** Explicit account/reservation/module
    settings, architecture mismatch, GPU client type, unusual paths, and
    scheduler capacity failures are valuable environment-compatibility checks
    but are less portable and lower value than the workload and recovery gaps
    above.

## Suggested coverage strategy

A full Cartesian product would be expensive and redundant:

1. Run one short real case for each workload/lifecycle branch on both
   substrates, plus one multi-dimensional case for reification and grouping.
2. Put parsing, invalid combinations, ranges, and snapshot-schema contracts in
   fast tests that stub only final dispatch.
3. Isolate process, service, retrieval, and scheduler failures in a focused
   fault-injection suite.
4. Reuse real workload artifacts across reporting modes. Each case should
   assert its branch, native command, phases, artifacts, snapshot, cleanup or
   retention contract, and semantic report content.
