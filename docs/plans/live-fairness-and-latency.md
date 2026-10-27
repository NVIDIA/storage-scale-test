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

# Plan: steady-state client fairness and fleet latency from live CSVs

Status: proposal, not implemented.

The plan replaces the slow-client analysis behind both outputs of elbencho live
CSVs:

- the text and plot reports (`--per-client-plots` and the always-on aggregate
  plot), which predate `results.json`;
- the `live` objects in `results.json`.

Both share one analysis module, `lib/elbencho_live_report.py`, and this plan
keeps it that way.

## Problem

### The existing slow-client detection

- **The rule.** An interval's rate for each client comes from `DoneBytes`
  deltas. A client underperforms in an interval when its z-score against the
  fleet mean and population stddev is below `-2.0`
  (`--client-outlier-threshold`). A client is selected for the plots after one
  such interval (`--client-min-underperform-segments`, default 1), and missing
  or invalid samples count toward that threshold too.
- **What it gets wrong.**
  - **No steady window.** Ramp-up and tail intervals are included. Clients that
    finished keep reporting a flat counter, which reads as a zero rate, so they
    look slow.
  - **No minimum shortfall.** When clients are even, the stddev is tiny and a
    0.5% dip scores below -2. In the calibration batch this flagged 55 of 64
    clients whose whole-phase rates were within 0.5% of the median.
  - **Small fleets can never trigger it.** With the client included in its own
    population stddev, the lowest reachable z is -sqrt(N-1), so fleets of 5 or
    fewer clients never cross -2. That includes every multi-node run of up to 5
    hosts.
  - **Two different measures.** The z-score flags intervals, but the heatmap
    colors by percent of the fleet median, so flagged cells and dark cells
    disagree.
  - **Single-interval noise.** One-interval rates carry counting noise and short
    turn-taking that average out within seconds (see Calibration).
  - **Details.**
    - Heatmap values clip at 100%, so faster-than-median clients cannot be seen.
    - The first heatmap column is always blank (no baseline).
    - Rank sorts as a string, so 10 comes before 2.
    - The `line_N_invalid` diagnostic creates one counter key per bad line.
    - The docs name none of the artifacts and do not explain the method.

### The missing fairness and latency views

- **No fairness index.** No report has a fairness index. A whole-phase Jain was
  prototyped in `results.json` and removed before merge: in a real 15-group
  batch every phase scored >= 0.9999, while per-interval Jain sat at 0.85-0.93
  in steady state.
- **Unused latency.** Each Total row carries a fleet IO latency for every
  interval, and no report uses it.

## Existing outputs on `main` (inventory)

All outputs are written to the report directory, under the stem
`<stem>-<phase slug>-`.

| Output | When | Content |
| --- | --- | --- |
| `aggregate-timeseries.png` | always | Total MiB/s (left axis) and IOPS (right axis) against time. A missing value plots as 0; gaps in the series are broken. |
| `client-summary.csv` / `.txt` | `--per-client-plots` | Every client: mean and P2-median MiB/s, mean percent of the fleet median, worst z-score and its time, underperforming intervals, longest streak, missing samples, and skipped samples. Sorted by problem count, then worst z. |
| `client-timeseries.png` | `--per-client-plots` | Fleet p10-p90 band, fleet median, and up to 10 selected client lines (`--client-max-timeseries-lines`) at one-interval resolution. |
| `underperformance-heatmap.png` | `--per-client-plots` | Clients by intervals. Each cell is 100 x rate / fleet p50, clipped at 0-100% (`YlOrBr_r`); missing cells are blue. Up to 50 rows (`--client-max-heatmap-rows`): selected clients first, then evenly spaced representatives. |
| stderr | always | Analyzed and generated paths, and the diagnostic counters. |

There is no live section in `report.txt`. `extract-filesystem.py` forwards
explicit options only. Tests pin `select_clients`, `select_heatmap_clients`,
the summary CSV columns and values, and `ClientSummary` field names
(`tests/test_extract_elbencho_live_csv.py`). The integration live-capture
scenario checks the summary CSV structure.

## Facts from elbencho 3.2-1 (`tmp/elbencho-src`)

- **Timing origin.** `RuntimeMS` counts from `phaseStartT`, the same origin as
  `time ms [first]`/`[last]` (Statistics.cpp:3120). A two-node capture agrees:
  WRITE `[first]` = 320 ms, the first service's `Active` drops to 0 at the
  330 ms row, and the last row is at `[last]` = 430 ms.
- **`[first]`** is the elapsed time of the first worker with work to finish
  (Statistics.cpp:1722-1732). With `--timelimit`, `[first]` is about `[last]`.
- **No warmup marker.** There is no warmup option and no per-worker time to
  first IO.
- **Total-row `Lat IO us`** is an op-weighted mean for the interval, read and
  reset each interval (LiveLatency.h:62-67). An interval without ops prints 0.
- **Per-host latency in `--hosts` mode.** Each service sends its interval
  latency sum and count with every status poll (`--svcupint`, default 500 ms).
  The master keeps the latest values per host in `RemoteWorker::liveLatency`.
  It keeps the old value when a poll had no ops, and never prints them per
  host.
- **Per-worker rows are blank on purpose.** Their rates and latency are empty
  because reading them would reset counters that the on-screen display and the
  Total row use (Statistics.cpp:3202, Worker.h:138/167).
- **Per-worker histograms exist but are merged** before the final results
  (Statistics.cpp:1761). The only per-worker or per-host final outputs are
  elapsed times (`--allelapsed`, `--svcelapsed`).

## Calibration (batch 20261016Z175940)

The prototype covered 108 live CSVs (216 WRITE/READ phases) at 1 s intervals,
with 64 KiB and 1 MiB blocks, in about 4 s.

- **Warmup.** Every client was progressing by the second row in 215 phases.
  How many phases had their warmup pushed past the second row by the ramp
  threshold:

  | Threshold | Phases |
  | --- | --- |
  | 50% | 0 |
  | 75% | 2 (5-6 s) |
  | 80% | 3 |
  | 90% | 11 (3-7 s; real but gentle ramps such as 711 -> 903 MiB/s over 8 s) |

- **Tail.** `([last] - [first]) / [last]` had a median of 11%, a p90 of 29%,
  and a maximum of 51%.
- **Window sensitivity.** Median of each phase's Jain p50:

  | Clients | 1 s | 5 s | 30 s | 60 s |
  | --- | --- | --- | --- | --- |
  | 32 threads | 0.928 | 0.977 | 0.996 | 0.998 |
  | 64 threads | 0.931 | 0.981 | 0.995 | 0.996 |
  | 128 threads | 0.984 | 0.995 | 0.999 | 1.000 |
  | 2 services | 1.000 | 1.000 | 1.000 | 1.000 |

  The worst phase was 1M, 64 threads, `iodepth` 8, WRITE. Its 5 s Jain had a
  minimum of 0.55 and was below 0.95 in 76% of windows, yet its whole-phase
  Jain was 0.9999. Imbalance in this batch is mostly turn-taking, not
  persistently slow clients.
- **Noise floor.** At 5 s windows the expected floor was >= 0.9966 everywhere.
- **Latency.** Interval latency max/p50 reached 2.3 in the steady window.
  Fairness dips tracked latency rises (Spearman rho < -0.3) in about a quarter
  of phases, mostly at `iodepth` 1, with rho down to -0.85. At `iodepth` 4-8
  there was no relation.
- **Multi-node runs** report one client per service, so imbalance between the
  threads within a host is invisible.

## Design: shared analysis

### Steady window

For each live domain matched to an aggregate CSV phase:

- **End:** `time ms [first]`. This is when the first client finished its fixed
  load, so it also excludes the stale per-host latency of finished services.
- **Start:** the first live row that:
  1. is not the first row;
  2. comes after every client has `DoneBytes > 0`;
  3. is where aggregate MiB/s reaches 75% of the median aggregate MiB/s between
     that point and the end.

  The 75% is a constant, recalibrated if reports keep flagging unfairness in
  the first windows of runs.
- **Unavailable:** `null` with a reason when the phase has no `[first]`
  (`no_first_time`), when the window is shorter than 2 x 5 s (`too_short`), or
  when warmup would take more than half the span (`no_steady_state`).

### Sliding windows

Windows are fixed in seconds and convert to intervals through
`ELBENCHO_LIVEINT`: one interval, 5 s, and 30 s. Each slides by one interval
and lies wholly inside the steady window.

For window W ending at interval t, client i's share is
`b_i = DoneBytes_i(t) - DoneBytes_i(t - W)`. Clients missing either sample
are left out, and the window records how many clients it used. Each window
yields:

- **Jain** over the `b_i`.
- **Ratio per client:** `b_i / median(b)`. This is the measure for slow
  clients, and the heatmap's cell value.
- **`min_over_median`:** the lowest client ratio. It compares across client
  counts, where Jain (lower bound 1/n) does not.
- **Expected floor:** with k = mean(b) / phase block size, floor = k / (k + 1).
  It is approximate; completions at `iodepth` > 1 are bursty.

A ring of the last 30 s of `DoneBytes` per client keeps memory at about
30 x clients integers. Everything else stays O(intervals), in the existing
single pass. The steady bounds come from the aggregate CSV, which is parsed
before the live analysis.

### Slow-client classification (replaces the z-score rule)

The rule is measured against the median, with a minimum shortfall S
(`--client-shortfall-percent`, default 5):

| Class | Rule |
| --- | --- |
| `persistent` | Over the steady window, `b_i / median(b) < 1 - S`, or in at least half of the 30 s windows. |
| `transient` | Not persistent, and below `1 - 2S` in at least 3 of the 5 s windows (`--client-min-slow-windows`, default 3). |
| `ok` | Neither. |

Missing and invalid samples are reported in their own columns and no longer
count toward selection. Because the rule uses ratios, a 2-host run classifies
the same way a 128-thread run does. The defaults are calibrated on the
reference batch in step 2 and recorded here.

### Fleet latency

- **Parsing.** Parse `Lat IO us` from Total rows, and `Lat Ent us` where
  entries are reported. A 0 with 0 IOPS is missing; a 0 with IOPS > 0 is kept.
  The column is optional, for older captures.
- **Steady summary.**
  - `p50`, `p90`, `max`, and `max_over_p50` (the spike signal).
  - `cv`.
  - The steady op-weighted mean beside the final `.out` average, as a check.
- **Fairness link.** Report Spearman rho over the steady 5 s windows, between
  Jain and mean latency and between latency and throughput. It is a number
  rather than a headline, because the link held in only about a quarter of
  phases.

## Design: text reports and plots

These replace the outputs in the inventory. Analysis is cheap, so summaries are
always produced when live CSVs are present; plots and per-client files stay
behind `--per-client-plots`.

| Output | Change |
| --- | --- |
| `report.txt` | New "Live fairness" lines per phase (always). Steady span; Jain p50/min at 1 s, 5 s, and 30 s; `min_over_median` p10 at 5 s; persistent and transient client counts; latency p50, p90, and max/p50. |
| `aggregate-timeseries.png` | Shade warmup and tail, and mark `[first]` and `[last]`. Add a fleet latency panel below on a shared time axis. Plot missing values as gaps, not 0. |
| `fairness-timeseries.png` (new) | Jain for the three windows over time, with the steady band shaded, the expected-floor line, and a `min_over_median` (5 s) trace. Always produced with live CSVs, since it needs no per-client selection. |
| `client-timeseries.png` | Plot 5 s windowed rates instead of one-interval rates. The fleet band becomes the 5 s p10-p90 with the median. Shade non-steady time. Choose lines by class: persistent first, then transient, ranked by worst ratio. |
| `underperformance-heatmap.png` | Rename to `client-ratio-heatmap.png`. Each cell is the 5 s window ratio to the fleet median on a diverging map centered at 100% (50-150%, clipped beyond), so fast clients show too. Mark non-steady columns. Sort rows by steady-window ratio, with representatives filling the remaining rows. Ranks sort numerically. |
| `client-summary.csv` / `.txt` | Replace the z-score columns. New columns: steady mean, steady ratio to the fleet median, worst 5 s and 30 s ratios and when they occurred, the share of 5 s windows below `1 - S`, the class, missing samples, and invalid samples. Sort by class, then steady ratio. |
| Options | Remove `--client-outlier-threshold` and `--client-min-underperform-segments`; add `--client-shortfall-percent` and `--client-min-slow-windows`; keep the two max-count options. A breaking CHANGELOG entry covers it. `extract-filesystem.py` routing and the option-registration tests follow. |
| Diagnostics | Cap the per-line invalid counters, keeping only a count and the first few line numbers. |
| Docs | `FILESYSTEM_TESTING.md` names every artifact, explains the steady window, the windows, and the classes, and says multi-node clients are hosts. |

## Design: `results.json`

`results.json` 1.1 adds optional fields:

- `live.steady`: `start_ms`, `end_ms`, `end_source` (`elbencho_first`), and
  `reason`.
- `live.fairness`: for each window (`interval`, `5s`, `30s`), Jain `min`, `p10`,
  `p50`, `min_at_ms`, the share below 0.95, `min_over_median` p10, and the floor
  p50. Also a columnar series of `end_ms`, `clients`, and Jain for each window
  over the steady span. It is O(intervals), so it is included at `summary`.
- `live.latency`: the steady summary and the rho values. The per-interval
  values go in `aggregate_series.io_latency_avg_us`.
- `distribution`: `persistent_clients` and `transient_clients`.
- `clients_summary`: the new summary columns.

The 1.0 document already exposes the z-score results:
`distribution.underperforming_clients` and the `clients_summary` columns
`underperform_intervals`, `longest_underperform_streak`, `worst_z`, and
`worst_elapsed_ms`. Removing them later is a major (2.0) change; see Open
questions.

## Steps

1. **Fleet latency.** Parse the Total latency columns. Export them in
   `aggregate_series`, plot the latency panel, and draw gaps instead of zeros.
   Test with real captures from `tmp/integration-diagnostics` and the reference
   batch.
2. **Steady window and sliding windows.** Add the steady window and the
   sliding-window computation to `analyze_live_csv`. Test completion-bound,
   time-limited, too-short, late-starter, and multi-node phases. Calibrate S
   and the slow-window count against the reference batch, and record the
   result here.
3. **Classification.** Replace `select_clients`, the summary columns, and the
   options. Update the pinned tests in `tests/test_extract_elbencho_live_csv.py`,
   the CLI and routing tests, and the integration CSV check.
4. **Plots.** Add the fairness plot, then rework the client time series and the
   heatmap. Add smoke tests: each plot is written, and its data helpers are unit
   tested.
5. **Text summary.** Add the `report.txt` live fairness lines.
6. **`results.json` 1.1.** Add the new fields, update the schema, and update
   `MACHINE_READABLE_RESULTS.md` fields and jq recipes.
7. **Docs and CHANGELOG.** Update `FILESYSTEM_TESTING.md` and add a breaking
   entry for the options.

## Upstream elbencho (issue only, never vendored)

A feature request drafted separately (to be filed as a GitHub issue, related to
elbencho #102, which asks for an interval max latency) asks for:

- per-service interval latency in extended live CSV rows;
- an opt-in per-service latency summary, and optionally histograms in
  `--jsonfile`;
- optionally, a per-service cumulative IO count.

The parser should accept non-empty per-client latency and IOPS cells if they
appear. They would add per-host latency series and a host-level latency
attribution to the slow-client classes.

## Open questions

- **Before merge:** should the 1.0 document drop the z-score fields listed
  above, as was done for whole-phase Jain? Then their replacements arrive as
  1.1 additions instead of a 2.0.
- **Review:** the classification changes the existing `--per-client-plots`
  outputs. Review it with that code's author before changing defaults.
- **Gating:** should `live.fairness` or the persistent-client count feed any
  gate, or stay informational until it has history?
