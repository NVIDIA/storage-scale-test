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

# Machine-readable results for filesystem reports

## Summary

Status: proposed, not implemented.

Automation that runs a
[prepared filesystem batch](../FILESYSTEM_TESTING.md#prepared-filesystem-batches)
and gates infrastructure on the results has no stable result format to read.
This plan:

1. **Removes** the reporters' save-and-reload caches. These are
   `--to-csv`/`--from-csv` in the filesystem and netbench extractors and
   `--to-json`/`--from-json` in the Warp extractor.
   - They have no known users.
   - They cost compatibility shims and tests.
   - Left in place, they would sit confusingly next to the new results
     document.
2. **Adds** one versioned JSON document per report. It covers every
   committed execution of every group, IO and metadata:
   - intended parameters;
   - ledger state;
   - parsed results;
   - an optional elbencho live CSV analysis, with per-client detail in
     compressed sidecar files.

Goals:

- One file says what was supposed to run, what ran, and what it measured. The
  consumer does no joins.
- Units are in field names.
- Every absence is `null` plus a reason.
- A published JSON Schema governs the document, and minor versions only add.
- Live detail scales to many gigabytes without making the main document hard
  to load.

Non-goals:

- Pass/fail evaluation against targets.
- Comparison across batches.
- Changes to how batches run.
- A results document for netbench or Warp.

## Step 1: remove the reporter caches

Nothing in `integration-tests/`, `.github/`, `utils/run_ci_checks.sh`, or the
launchers uses `--to-csv`, `--from-csv`, `--to-json`, or `--from-json`.

- **Filesystem options.**
  - Delete `--to-csv` and `--from-csv` from `lib/filesystem_report_options.py`.
  - Delete their checks and help text in `lib/report_cli.py`,
    `utils/extract-filesystem.py` (`_report_ordinary`), and
    `lib/filesystem_batch.py` (`report_batch`, `route_batch_report`).
  - `--test-parse` stays.
- **`utils/extract-elbencho.py`.**
  - Delete `write_csv` and `read_csv`.
  - Delete the read-side helpers: backward-compatibility defaults, type
    coercion, and histogram-cell parsing.
  - Delete `_TREESCAN_CSV_KEYS`, `_WORKLOAD_STRING_CSV_KEYS`, and
    `_ELBENCHO_METRIC_FIELD_TYPES`.
  - Delete the cache branches in `main()`.
  - Delete the `storage_target_name` field, which only the cache reader sets,
    and simplify `_target_sources`.
  - Reword the "kept for CSV compatibility" comments. `throughput_mb_s` stays,
    because the tables use it.
- **`utils/extract-mdtest-elbencho.py`.**
  - Delete `write_csv_export`, `read_csv_import`, `_read_histogram_json`, and
    the `storage_target_name` field.
  - Delete the cache branches in `main()`, un-nesting the raw-input path.
- **`lib/storage_target.py`.**
  - Delete `cached_target_sources`, `exported_target_cell`,
    `parse_target_cell`, `_parse_mixed_items`, `_QUOTED_NAME`, and
    `has_unverified_source`.
  - Keep the live mixed-target guard.
- **`utils/extract-netbench.py`.**
  - Delete `write_csv_export`, `read_csv_import`, `encode_histogram`, and
    `decode_histogram`.
  - Delete the cache branches in `main()`. Input directories become required.
  - Delete `--to-csv FILE` from `lib/report_cli.py`.
  - Delete `--from-csv` from `add_common_report_arguments` in
    `lib/reporting_common.py`, which only netbench calls.
- **`utils/extract-warp.py`** (the `<datestamp>-analyzed.json.zst` cache).
  - Delete `write_json`, `read_json`, `find_analyzed_file`, and
    `_decode_cached_histogram`.
  - Delete the `main()` branches and their directory, relative-path, and
    datestamp lookup. The input directory becomes required.
  - Delete `--to-json`, `--from-json`, their usage examples, and the
    `--from-json` wording in the other help text in `lib/report_cli.py`.
  - Keep a one-line filename skip for `*-analyzed.json[.zst]`, so directories
    that hold an old cache file report without warnings. Delete the
    structure-sniffing skip in `parse_warp_json`.
- **What stays.**
  - Raw parsing: elbencho `--csvfile` and `.live.csv` files, and Warp
    `.json`/`.json.zst` results. The `csv` and `zstandard` imports go with
    them.
  - `lib/join_datestamps.py`, which plots and titles use. Drop its `suffix`
    parameter if nothing else passes one.
- **Tests.**
  - Delete the cache round-trip and cached-target tests in:
    - `tests/test_extract_elbencho_cli_contracts.py`;
    - `tests/test_extract_mdtest_elbencho.py`;
    - `tests/test_storage_target.py`;
    - `tests/test_filesystem_batch_reporting.py`.
  - Delete the cached-histogram tests in
    `tests/test_extract_netbench_histogram.py` and
    `tests/test_extract_warp_histogram.py`.
  - Delete the CSV round trip at the end of the completion-field test in
    `tests/test_extract_elbencho_resume.py`.
  - Rewrite tests that used a cache only to observe something else (filter
    validation, target naming, batch option routing).
- **Docs.**
  - Remove the cache option rows from `docs/FILESYSTEM_TESTING.md` and
    `docs/OBJECT_STORAGE_TESTING.md`, plus the stitching bullet and the
    cached-input notes from `docs/FILESYSTEM_TESTING.md`.
  - Delete the cache sentences from `docs/DESIGN.md` and keep its filter
    sentence.
  - Mark UC-8.7 in `docs/REQUIREMENTS.md` as removed, without renumbering.
  - Add one `CHANGELOG.md` entry.

## Steps 2–6: the results document

### Options

```bash
./utils/extract-filesystem.py "$BATCH" --to-json --live-detail full
# -> $BATCH/reports/results.json and $BATCH/reports/results.live/*.jsonl.zst
```

The options go in the shared `REPORT_OPTIONS` table, so both extractors
accept them and batch routing forwards them.

| Option | Meaning |
| --- | --- |
| `--to-json [PATH]` | Write the document. The default is `<output-dir>/results.json`, which for a batch is `<batch>/reports/results.json`. A `.zst` suffix compresses it. `-` writes to stdout and suppresses other stdout output. |
| `--live-detail LEVEL` | `none`, `summary` (default), `selected`, or `full`. |
| `--require-complete` | After writing the document, exit 3 if `batch_outcome.complete` is false. |

Exit status is 0 when the document is written, 1 for reporting errors, 2 for
usage errors, and 3 only as above. Gates read the document, not the exit
status.

Live sidecars are always zstd-compressed and written as a stream.
`zstandard` is already a dependency. The main document is plain JSON unless
its path ends in `.zst`. Every recorded `sha256` and `size_bytes` describes
the stored bytes.

### Conventions

- **Units** are suffixes: `_bytes`, `_bytes_per_s`, `_ops_per_s`, `_us`
  (latency), `_ms` (measured times), and `_s` (configured durations).
- **Throughput** is converted from elbencho's MiB/s (× 1048576).
- **Latency** keeps elbencho's microseconds.
- **Rounding:**
  - throughput to whole bytes per second;
  - operation rates to 0.01 ops/s;
  - latencies to 0.1 µs.
- **Determinism:**
  - Keys appear in schema order.
  - Groups and executions sort by ID, phases appear in run order, and
    histogram buckets are ascending.
  - Two exports of the same inputs differ only in `generator.generated_at`.
- **`null`** means not measured or not applicable, and the enclosing
  `problems` list says why. Zero is always a measured zero.
- **Enumerations** are lowercase `snake_case`. Consumers tolerate unknown
  values and ignore unknown fields.
- **Paths:**
  - `batch.root` is the batch directory, absolute and symlink-resolved.
  - Other paths are relative to it.
  - Paths to data outside the batch, such as a `--read-from` dataset, are
    recorded as the run recorded them.
- **Histograms** are columnar: `{"buckets_us": [...], "counts": [...]}`.
- **Per-phase values** are separate fields prefixed `write_` or `read_`, and
  are `null` when that phase does not run.

### Top level

The document is execution-centric. Groups hold shared settings once.

```json
{
  "schema": "storage-scale-test/filesystem-batch-results",
  "schema_version": "1.0",
  "generator": {
    "tool": "storage-scale-test", "version": "2.1.0",
    "generated_at": "2026-10-26T18:04:11Z",
    "command": ["utils/extract-filesystem.py", "<batch>", "--to-json"],
    "options": {"live_detail": "summary", "z_threshold": 2.0,
                "only_nodes": null, "only_threads": null}
  },
  "batch": {
    "root": "/results/batch-20261026Z120000",
    "datestamp": "20261026Z120000",
    "manifest_version": 1, "manifest_sha256": "…",
    "sealed": true, "substrate": "slurm"
  },
  "batch_outcome": {
    "complete": false,
    "executions": {"total": 48, "success": 46, "failed": 1, "pending": 1,
                   "running": 0, "cancelled": 0, "interrupted": 0, "unknown": 0},
    "results": {"complete": 45, "partial": 1, "missing": 0, "unparseable": 0,
                "not_run": 2, "not_reported": 0},
    "groups_reported": ["0001", "0002", "0003"],
    "groups_not_reported": []
  },
  "groups": [ … ],
  "executions": [ … ],
  "diagnostics": []
}
```

`batch_outcome.complete` is true only when every committed execution has
ledger state `success` and result state `complete`. It is a gate's first
check.

Report filters (`--only-*`, `--groups`, `--kind`) never drop executions. A
filtered execution is listed as `not_reported`, so a filtered export cannot
look complete. `generator.options` records the filters.

### Groups

```json
{
  "group_id": "0002",
  "kind": "io",
  "label": null,
  "output_relative": "groups/0002/elbencho-20261026Z120000",
  "target": {"name": "lustre-a", "verified": true},
  "settings_sha256": {"env_used_sh": "…", "env_used_yaml": "…"},
  "env_override": {"files": [{"file": "overrides/10-small-random.env",
                              "sha256": "…",
                              "variables": ["ELBENCHO_SCALE_IO_SIZES"]}]},
  "settings": { "...": "env_used.yaml, as parsed" },
  "execution_ids": ["0013", "0014", "…"]
}
```

`label` stays `null` until the group labels proposed in
[dataset references](elbencho-batch-dataset-references.md#machine-readable-output-and-listing)
exist.

### Executions

```json
{
  "execution_id": "0014",
  "group_id": "0002",
  "kind": "io",
  "definition_sha256": "…",
  "ledger_state": "success",
  "result_state": "complete",
  "problems": [],
  "coordinates": { … },
  "coordinate_key": "io/c004/s032/d016/w1M-seq/r4K-rand/dio",
  "dataset": { … },
  "result_files": [{"path": "groups/0002/…/elbencho-1M,r4K-c_004-s_032-d_016_….csv",
                    "sha256": "…", "size_bytes": 18233}],
  "log": "executions/0014.log",
  "results": { … }
}
```

- **`ledger_state`** is the lowercased `.status` value: `pending`,
  `running`, `success`, `failed`, `cancelled`, `interrupted`, or `unknown`.
- **`result_state`:**

  | Value | Meaning |
  | --- | --- |
  | `complete` | Every expected phase or iteration parsed and validated. |
  | `partial` | Some were dropped; `problems` says which. |
  | `missing` | No result files. |
  | `unparseable` | Result files exist, but none parse. |
  | `not_reported` | Excluded by a report filter. |
  | `not_run` | Ledger state is not `success`. |

- **`problems`** holds `{"code", "detail", "phase"}` objects. Codes are
  stable, for example `iteration_incomplete`, `phase_missing`,
  `completion_incomplete`, and `live_csv_unreadable`. `detail` is free text.
- **`coordinate_key`** identifies the test point across batches. Execution
  IDs are unique only within a batch.
  - IO keys are built from nodes, threads, depth, each phase's size and
    access, and direct or buffered IO.
  - Metadata keys look like `md/c002/t064/b4/i1000`.
  - Keys exclude the target, which is a group property.

#### IO coordinates

These come from `executions/NNNN.sh`, not from file names.

```json
"coordinates": {
  "nodes": 4, "threads_per_node": 32, "io_depth": 16,
  "io_size_spec": "1M,r4K",
  "phase_plan": "write_read",
  "write_block_size_bytes": 1048576, "write_access": "sequential",
  "read_block_size_bytes": 4096, "read_access": "random",
  "direct_io": true,
  "run_to_completion": false,
  "file_layout": "worker-directories",
  "single_big_file": false,
  "all_nodes_all_data": false
}
```

- **`io_size_spec`** is kept for provenance only. The four `write_*`/`read_*`
  fields are its parsed form:
  - One value applies to both phases.
  - `a,b` gives write `a` and read `b`.
  - An `r` prefix, or the global `-r` flag, makes a phase random.
- **`direct_io`** is one field, because `-b` (buffered IO) applies to the
  whole sweep.
- **`phase_plan`:**

  | Value | Sweep flag | Phases |
  | --- | --- | --- |
  | `write_read` | none | write, read, and delete if the workload records one |
  | `write_only` | `--write-only` | write; data kept |
  | `write_no_read` | `--write-no-read` | write, delete |
  | `read_from` | `--read-from PATH` | read of an existing dataset |

  A phase expected by the plan but absent from the results produces a
  `phase_missing` problem.

#### IO dataset

```json
"dataset": {
  "source": "read_from",
  "read_from": "/lustre/a/datasets/imagenet-like",
  "kubernetes_mapped_read_from": null,
  "write_only_data_dir": null,
  "layout": "staged-tree",
  "files_total": 1281167, "bytes_total": 146900000000,
  "treefile_source": "cache_hit",
  "scanned_file_sizes": {"count": 1281167, "avg_bytes": 114662,
                         "min_bytes": 1024, "max_bytes": 15728640}
}
```

- **`source`** is `generated` or `read_from`.
- **Where each field comes from:**
  - `read_from` and `kubernetes_mapped_read_from` come from the execution
    definition's `ELBENCHO_SWEEP_READ_FROM` and `KUBECTL_MAPPED_READ_FROM`.
    Today's extractor scrapes `read_from` from logs instead.
  - `write_only_data_dir` comes from the sweep's
    `ELBENCHO_WRITE_ONLY_DATA_DIR=` line.
  - The rest come from `NNNN.workload.tsv`.
- **`scanned_file_sizes`** replaces the `treescan_*` fields.
  - Elbencho's `--treescan` walks a directory and records its entries in a
    treefile that the run then uses. The sweep does this on `--read-from` of
    a directory, and on reads that build a treefile.
  - The sweep then summarizes the treefile's file sizes, which describe the
    files actually read.
  - It is `null` when no scan ran, including single-big-file runs.

#### Metadata coordinates

```json
"coordinates": {"nodes": 2, "tasks_per_node": 64,
                "branch_factor": 4, "items_per_dir": 1000, "iterations": 3}
```

### IO results

There is one object per phase that ran. `elbencho_first` and `elbencho_last`
are elbencho's `[first]` and `[last]` columns: aggregate results from phase
start until the fastest and the slowest worker thread finished. Today's
reports use `[first]`.

```json
"results": {
  "phases": [
    {
      "phase": "write",
      "completion_state": "completed",
      "elbencho_first": {"ops_per_s": 812345.0, "bytes_per_s": 3327131648,
                         "elapsed_ms": 59874},
      "elbencho_last":  {"ops_per_s": 799112.0, "bytes_per_s": 3273654272,
                         "elapsed_ms": 60311},
      "latency_us": {"min": 41.0, "avg": 627.4, "max": 18233.0,
                     "p1": 88.0, "p50": 512.0, "p75": 704.0, "p99": 3072.0},
      "histogram_us": {"buckets_us": [64, 128, …], "counts": [13, 9921, …]},
      "work": {"expected_files": 131072, "completed_files": 131072,
               "expected_bytes": 549755813888, "completed_bytes": 549755813888},
      "configured_time_limit_s": 60,
      "command": ["elbencho", "-w", "…"],
      "live": { … }
    },
    {"phase": "read", "…": "…"}
  ],
  "workload": {
    "termination_mode": "time_bounded_repeat",
    "completion_state": "completed",
    "failure_cleanup_state": "not_needed",
    "lifecycle_elapsed_ms": 241002
  }
}
```

- Percentiles come from the `.out` file. A missing percentile is `null`, not
  0.
- `command` is the recorded argv.
- `workload` carries the `NNNN.workload.tsv` enums. It is `null` for layouts
  that write no such file, such as legacy worker directories.

### Metadata results

Every iteration is listed. The aggregate uses only validated iterations, and
says how many.

```json
"results": {
  "iterations": [
    {"iteration": 1, "datestamp": "20261026Z120000", "state": "complete",
     "problems": [],
     "phases": [
       {"phase": "create", "ops_per_s": 184220.0, "elapsed_ms": 5212,
        "latency_us": {"min": 120.0, "avg": 1380.2, "max": 40211.0},
        "histogram_us": {"buckets_us": […], "counts": […]}},
       {"phase": "stat", "…": "…"},
       {"phase": "delete", "…": "…"}
     ]},
    {"iteration": 2, "state": "incomplete",
     "problems": [{"code": "iteration_incomplete",
                   "detail": "partial CSV operations: RMFILES"}],
     "phases": [ … ]}
  ],
  "aggregate": {
    "iterations_expected": 3, "iterations_used": 2,
    "phases": [
      {"phase": "create",
       "ops_per_s": {"mean": 183100.0, "stddev": 1580.0,
                     "min": 181980.0, "max": 184220.0},
       "mean_elapsed_ms": 5240,
       "latency_us": {"p0": 120.0, "p50": 1024.0, "p90": 2048.0,
                      "p99": 8192.0, "p100": 40211.0},
       "latency_method": "pooled_histogram_bucket"}
    ]
  }
}
```

An incomplete iteration makes the execution `partial`. The text and Markdown
reports keep their current behavior.

### Live CSV analysis

#### Source rows

Verified against elbencho 3.2-1, the Kubernetes default image
(`tmp/elbencho-src`, `Statistics::printLiveStatsCSV`).

`ELBENCHO_LIVE_CSV_EXTENDED=1` adds `--livecsv --livecsvex`. Each interval
writes one `Rank=Total` row plus one row per entry in elbencho's worker list.
Per-client rows carry cumulative `DoneBytes` but no rate.

| Mode | Each client row is | `Rank` | `Service` |
| --- | --- | --- | --- |
| Standalone (no `--hosts`) | a local worker thread | thread index | empty |
| Distributed (`--hosts`) | a remote service, with `Active` threads and CPU | service index | the `--hosts` entry |

`run_an_elbencho` passes `--hosts` only for executions with more than one
node, on every substrate:

| Execution | SSH | Slurm | Kubernetes |
| --- | --- | --- | --- |
| 1 node | threads on the host | threads on the node | threads in the coordinator Pod |
| > 1 node | one row per host name | one row per node name | one row per worker Pod IPv4 |

- `client_mode` records `worker` (threads) or `service`.
- Kubernetes rows are joined to their node and Pod names through the
  collected `executions/<id>.workers.tsv`.
- The `env.sh.template` comment claiming single-node SSH rows are services
  is wrong and gets corrected.
- Live CSVs exist only for IO sweeps.

`lib/elbencho_live_report.py` already analyzes each file in one streaming
pass. The export reuses that pass. It keeps bounded state:

- the aggregate series;
- per-interval fleet statistics;
- per-client summaries;
- diagnostics.

#### Detail levels

A domain is one execution and phase.

| Level | Per domain | Grows with |
| --- | --- | --- |
| `none` | `live: null`, reason `not_requested` | nothing |
| `summary` | aggregate and fleet series, distribution statistics, every client's summary, diagnostics | intervals + clients |
| `selected` | `summary`, plus inline rates for the clients the existing selector picks (bounded by `--max-timeseries-lines` and `--max-heatmap-rows`) | intervals × a few dozen |
| `full` | `summary`, plus every client's rates in a sidecar | intervals × clients |

Without an analysis, `live` is `null` with reason `not_captured`,
`not_requested`, or `unreadable`.

#### Live object

```json
"live": {
  "source": {"path": "groups/0002/…/elbencho-….live.csv", "sha256": "…",
             "size_bytes": 63021944},
  "configured_interval_ms": 1000,
  "client_mode": "service",
  "domain": {"phase": "write", "mix_type": "", "occurrence": 0},
  "intervals": 61,
  "clients": 256,
  "aggregate_series": {"elapsed_ms": [1000, 2000, …],
                       "bytes_per_s": […], "ops_per_s": […]},
  "fleet_series": {"elapsed_ms": […], "client_count": […],
                   "bytes_per_s_p10": […], "bytes_per_s_p50": […],
                   "bytes_per_s_p90": […], "bytes_per_s_mean": […],
                   "bytes_per_s_stddev": […]},
  "distribution": {
    "client_mean_bytes_per_s": {"min": …, "p10": …, "p50": …, "p90": …,
                                "max": …, "cv": 0.041},
    "min_over_median": 0.83, "max_over_median": 1.07,
    "jain_fairness": 0.998,
    "final_done_bytes": {"min": …, "p50": …, "max": …, "cv": 0.039},
    "finish_elapsed_ms": {"min": …, "p50": …, "max": …},
    "underperforming_clients": 3
  },
  "clients_summary": {
    "columns": ["client", "rank", "service", "kubernetes_node",
                "kubernetes_pod", "mean_bytes_per_s", "median_bytes_per_s",
                "mean_ratio_to_fleet_median", "final_done_bytes",
                "finish_elapsed_ms", "underperform_intervals",
                "longest_underperform_streak", "worst_z", "worst_elapsed_ms",
                "valid_samples", "missing_samples", "invalid_samples"],
    "rows": [["host017 [rank 16]", 16, "host017", null, null, …], …]
  },
  "client_series": null,
  "diagnostics": {"missing_samples": 0, "counter_resets": 0,
                  "non_adjacent_samples": 0}
}
```

- Series and `clients_summary` are columnar, because repeating keys per row
  roughly triples the size.
- `distribution` lets a gate check imbalance without per-client data, using
  the coefficient of variation, min/median, max/median, and Jain's fairness
  index.
- Two new O(clients) derivations go beyond `ClientSummary`:
  - `final_done_bytes` is each client's last cumulative `DoneBytes`, its work
    share in time-bounded runs.
  - `finish_elapsed_ms` is the first interval at which a client reached that
    value. It is the straggler signal in completion-bound runs, where work is
    equal and imbalance shows up as finish time.
- `client_series` is `null` at `summary`, inline at `selected`, and a sidecar
  reference at `full`:

```json
"client_series": {"path": "reports/results.live/0014.write.jsonl.zst",
                  "sha256": "…", "size_bytes": 412337,
                  "clients": 256, "intervals": 61}
```

#### Sidecar format

There is one zstd-compressed JSON Lines file per domain. A header line lists
the clients, followed by one line per interval, in the source's order.
Writing and reading therefore need memory proportional to the number of
clients only. `null` marks a missing or invalid sample.

```json
{"schema": "storage-scale-test/live-client-series", "schema_version": "1.0",
 "execution_id": "0014", "phase": "write", "clients": ["host001 [rank 0]", …]}
{"elapsed_ms": 1000, "bytes_per_s": [null, 1320000000, 1290000000, …]}
```

#### Size estimates

The estimates assume:

- about 11 bytes per per-client sample;
- about 350 bytes per client-summary row;
- about 150 bytes per interval of series;
- two domains per execution.

The raw `.live.csv` is about 200 bytes per row. "Clients" means threads in
single-node runs.

| Scenario | Raw live CSV | `summary` | `full`, before zstd |
| --- | --- | --- | --- |
| 16 clients, 2 × 60 s at 1 s | 0.4 MB | 30 KB | 21 KB |
| 256 clients, 2 × 300 s at 1 s | 31 MB | 270 KB | 1.7 MB |
| 1024 clients, 2 × 300 s at 1 s | 123 MB | 810 KB | 6.8 MB |
| 1024 clients, 2 × 3600 s at 1 s | 1.5 GB | 1.8 MB | 81 MB |
| 1024 clients, 2 × 3600 s at 250 ms | 5.9 GB | 5.1 MB | 324 MB |

Multiplied across 200 executions:

| Case | Summaries in `results.json` | Sidecars, before zstd |
| --- | --- | --- |
| 256 clients, 300 s per phase | ~54 MB | ~340 MB |
| 1024 clients, one hour per phase | ~360 MB | ~16 GB |

The sidecar compression ratio will be measured on real captures and published
in the user docs. Without live data, an execution costs about 5 to 10 KB (IO)
or 10 to 20 KB (three-iteration metadata).

### Architecture

1. **Fragments.**
   - With `--to-json`, each per-group extractor run writes
     `<group report dir>/results.fragment.json`, keyed by execution ID.
   - `extract-elbencho.py` already joins metrics to the staged
     `executions/NNNN.sh` (`load_execution_workloads`). The metadata
     extractor gains the same join on `(nodes, tasks)`.
   - Staging also copies `executions/<id>.workers.tsv`.
   - Fragments are internal.
2. **Assembly.**
   - `report_batch` merges the fragments with the manifest, group settings,
     and ledger.
   - It adds entries for executions without fragment data and computes
     `batch_outcome`.
   - It writes `results.json` atomically, next to `index.md`.
3. **Ordinary runs.**
   - A non-batch directory uses the same schema, with `batch.root` set to
     that directory and null manifest fields.
   - It has one synthetic group, `"0000"`, built from its `env_used.yaml`
     and `executions/`.

New `lib/filesystem_results.py` holds:

- schema constants;
- conversion and rounding;
- the encoders;
- the deterministic and sidecar writers;
- the merge.

Both extractors build from their existing dataclasses and `DomainAnalysis`.
The only new raw-file reading is the `[last]` columns.

### Schema and documentation

- **Schemas.** Ship `docs/schemas/filesystem-batch-results-1.schema.json` and
  `docs/schemas/live-client-series-1.schema.json` (JSON Schema 2020-12).
  Fixture documents are validated against them.
- **Versioning.** `schema_version` is `MAJOR.MINOR`.
  - A minor version only adds optional fields or enum values.
  - A major version renames, removes, or changes the meaning or unit of a
    field.
- **Documentation.**
  - A new `docs/MACHINE_READABLE_RESULTS.md` holds the consumer contract:
    - field reference;
    - states and problem codes;
    - live levels and sidecars;
    - size guidance;
    - a short Python gate example.
  - `docs/FILESYSTEM_TESTING.md` gets only the `--to-json` option row and a
    one-line pointer.
  - `AGENTS.md`'s docs list gains the new file.

## Decisions

These were resolved by the maintainer on 2026-10-27:

1. **Failed executions are not parsed.** Only `success` executions
   contribute results, as batch reporting does today. Others are `not_run`.
2. **Kubernetes reports collected evidence only.** There are no cluster
   queries and no `--status` attempt or segment keys. Live CSVs are collected
   like all result files.
3. **Host names stay**, in client rows and commands, so operators can find
   slow nodes. Redaction can come later.
4. **No flat one-record-per-metric companion file.**
5. **Rounding as in [Conventions](#conventions)**, for byte-identical
   re-exports. Full precision was preferred but is not required.
6. **No inline `full` live detail.** Sidecars only. An inline option can be
   added later.

## Implementation order

There is one commit per step on the current branch:

1. Remove the reporter caches ([Step 1](#step-1-remove-the-reporter-caches)).
2. IO fragment and batch document.
   - `lib/filesystem_results.py` and the schemas.
   - The IO fragment without live data, including `[last]` columns, `null`
     percentiles, `phase_plan`, the `write_*`/`read_*` coordinates, and
     `dataset`.
   - Batch assembly with `batch_outcome`.
3. Metadata fragment, with per-iteration results and incomplete iterations.
4. Live `summary`.
   - The new derivations and the distribution statistics.
   - The `workers.tsv` join.
   - The `env.sh.template` comment fix.
5. Live `selected` and `full`, with the streaming sidecar writer.
6. Ordinary-directory export, `--require-complete`, `.zst` main output, the
   docs, and an integration test that reports a kind-based batch and
   validates the document.

Tests:

- **Schema validation** of fixtures covering:
  - mixed IO and metadata groups;
  - buffered and direct IO;
  - a split spec such as `1M,r4K`;
  - `--write-only` and `--read-from`;
  - an incomplete metadata iteration;
  - failed and pending cells;
  - each live level, with thread and service clients.
- **Determinism.**
- **No large-scale or memory-bound sidecar test.** Streaming bounds memory
  by design, and an out-of-memory report would be handled if one occurs.
