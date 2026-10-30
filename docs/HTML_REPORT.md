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

# HTML report

Every filesystem report writes `report.html`: one file for a single run, a
sweep, a batch, or several inputs. It is rendered from
[`results.json`](MACHINE_READABLE_RESULTS.md); see
[filesystem reporting](FILESYSTEM_TESTING.md#filesystem-reporting) for how to
produce it. Open it in a browser.

- Tables are plain HTML and work offline.
- Charts use Chart.js, loaded from a pinned CDN URL with an integrity hash.
  Offline, chart boxes say so.
- Charts draw when scrolled into view or when a run is expanded. Heatmaps
  show the 64 worst clients and client tables the 200 slowest.
- Light and dark mode follow the browser; the Light/dark button switches.
  Tables sort by column; "Show all columns" reveals the rest. The contents
  sidebar has a filter.

## Page structure

1. Summary: sources (collapsed), headline cards, and the TL;DR table (best
   result per section).
2. Comparisons, when sections pair (see
   [comparisons](FILESYSTEM_TESTING.md#comparisons)).
3. One section per group and IO shape (block sizes, access, IO mode): the
   settings that apply to its runs, sweep charts, results tables, and the
   live fairness table; metadata sections show rates and latency. The title
   also names a node count all runs share ("single node"; the tables then
   omit the nodes column) and phases of 10 minutes or more ("30 min per
   phase").
4. Per-run live detail inside each section, collapsed on load. elbencho's
   `SYNC` and `DROPCACHE` phases move no data and are left out
   (`results.json` keeps them).
5. Provenance, per document: command, versions, source paths and hashes, and
   diagnostics (collapsed).

## Referring to a part

- **Figure N.M, Table N.M:** N is the chapter (1 Summary, then Comparisons
  if present, then one per section, last Provenance); M counts within it.
  Numbers shift between reports.
- **Hover `#`:** each heading, figure and table shows a permalink with its
  template name. The anchor is derived from content, so it is stable:
  `g<group>-<shape>-<template>`, for example
  `g0003-1m-seq-dio-sweep-throughput`.
  - Per-run detail adds `-r<execution>-<phase>` after the template, for
    example `g0003-1m-seq-dio-run-heatmap-r0001-write`.
  - Templates split per phase or operation end in `-write`, `-read`,
    `-create`, and so on.
  - A chart of more than 20 configurations splits into balanced parts, each
    ending in `-part<N>` after any other tail (`...-latency-write-part2`).
  - With several documents, anchors start with `d<N>-`.
  - Comparisons use `cmp-<setting>-<hash>-<suffix>`.
- Feedback that names a template ("make `run-heatmap` taller") applies to
  every instance; a figure number or an anchor names one.

## Templates

| Template | Content |
| --- | --- |
| `header-cards` | Summary cards: peak read/write GB/s and IOPS, peak create/stat/delete ops/s. Millions show as `109M`; hover for the exact value. |
| `tldr-table` | Best result per section: test, target, IO size, threads, depth, nodes, performance, p99. |
| `comparison-chart` | Overlay of paired sections: throughput, per-node share and p99 vs the swept dimension (bars for one point), plus aggregate time series. |
| `sweep-throughput` | Aggregate GB/s vs the swept dimension; full width unless the IO size is 64 KiB or less. |
| `sweep-iops` | Aggregate IOPS: full width and linear for IO sizes of 64 KiB or less, else log scale. |
| `sweep-efficiency` | Scaling efficiency vs nodes (node sweeps). |
| `sweep-share` | GB/s per node vs nodes (node sweeps). |
| `sweep-latency` | p50 and p99 latency, log scale; one chart per phase (`-write`, `-read`). |
| `sweep-histogram` | Latency histogram at peak throughput. |
| `results-table` | One row per execution: a table per phase, write then read (IO; `-write`, `-read`), or per operation (metadata; `-create`, `-stat`, `-delete`). The `run` ID column is behind "Show all columns" and links to the run's detail. |
| `fairness-table` | Live fairness in the steady window: a table per phase (`-write`, `-read`), one row per execution; `none (reason)` when the window was rejected (e.g. phase too short). |
| `md-rates` | Metadata ops/s vs the swept dimension; one chart per operation when several configurations are swept. |
| `md-latency` | Metadata p50 and p99 latency; split per operation like `md-rates`. |
| `run-aggregate` | One run's aggregate throughput and latency over time. |
| `run-spread` | Per-client p10-p90 band and median over time. |
| `run-fairness` | Jain index over time. |
| `run-clients` | The slowest clients, median and fastest client over time. |
| `run-heatmap` | Clients by interval, each as a share of the fleet median. |
| `run-client-table` | Per-client summary, slowest first. |
| `provenance` | Command, versions, sources, diagnostics. |

The swept dimension goes on x (nodes, then threads, then IO depth), with one
series per remaining combination. Per-client templates (`run-clients`,
`run-heatmap`, `run-client-table`) need
[`--live-detail full`](FILESYSTEM_TESTING.md#live-detail-levels).
The template names are `TEMPLATES` in `lib/report_html/model.py`.
