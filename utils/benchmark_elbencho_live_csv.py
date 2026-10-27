#!/usr/bin/env python3

# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Generate and analyze a deterministic large elbencho live CSV."""

from __future__ import annotations

import argparse
import csv
import resource
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.elbencho_live_report import (  # pylint: disable=wrong-import-position
    MIB,
    LIVE_CSV_COLUMNS,
    LiveClientId,
    LiveDomainKey,
    LiveFileMetadata,
    analyze_live_csv,
    write_client_summaries,
)
from lib.elbencho_live_plots import (  # pylint: disable=wrong-import-position
    plot_aggregate_timeseries,
    plot_client_ratio_heatmap,
    plot_client_timeseries,
    plot_fairness_timeseries,
)
from lib.elbencho_live_steady import (  # pylint: disable=wrong-import-position
    CLASS_PERSISTENT,
    PhaseContext,
    WindowedClientDetail,
    analyze_steady,
    classified_clients,
    collect_windowed_client_detail,
    select_heatmap_rows,
)

BASE_RATE_MIB_S = 100
PERSISTENT_RATE_MIB_S = 50
TRANSIENT_RATE_MIB_S = 40
ANOMALY_GROUP_SIZE = 10
MIN_BENCHMARK_CLIENTS = 5 * ANOMALY_GROUP_SIZE + 1
BASE_TIMESTAMP = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)


def generate_live_csv(path: Path, client_count: int, interval_count: int) -> None:
    """Stream the deterministic benchmark CSV to disk."""
    counters = [0] * client_count
    aggregate_done = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(LIVE_CSV_COLUMNS)
        for interval in range(interval_count):
            runtime_ms = (interval + 1) * 1000
            iso_timestamp = (
                BASE_TIMESTAMP + timedelta(milliseconds=runtime_ms)
            ).isoformat(timespec="milliseconds")
            rates = [
                _rate_for_client(client, interval, interval_count)
                for client in range(client_count)
            ]
            aggregate_rate = sum(
                rate
                for client, rate in enumerate(rates)
                if not _sample_is_missing(client, interval)
            )
            aggregate_done += aggregate_rate * MIB
            writer.writerow(
                _live_row(
                    runtime_ms,
                    iso_timestamp,
                    "Total",
                    aggregate_done,
                    "",
                    aggregate_rate,
                    aggregate_rate * 16,
                )
            )
            for client, rate in enumerate(rates):
                if _sample_is_missing(client, interval):
                    continue
                if _sample_resets(client, interval, interval_count):
                    counters[client] = 0
                counters[client] += rate * MIB
                writer.writerow(
                    _live_row(
                        runtime_ms,
                        iso_timestamp,
                        client,
                        counters[client],
                        f"client-{client:05d}",
                        "",
                        "",
                    )
                )


def run_analysis(
    path: Path,
    output_dir: Path,
    client_count: int,
    create_plots: bool,
) -> None:
    """Run the production analyzer and validate known synthetic behavior."""
    metadata = LiveFileMetadata(
        path=str(path),
        io_size="r64K",
        nodes=client_count,
        threads=1,
        io_depth=1,
        datestamp="20260729Z120000",
    )
    started = time.perf_counter()
    analysis = analyze_live_csv(metadata)
    pass_one_seconds = time.perf_counter() - started
    domain = analysis.domains[LiveDomainKey("READ", "")]
    # The synthetic clients all run to the last interval, so [first] is the
    # last interval's runtime; block size matches the r64K file name.
    end_ms = int(domain.interval_times[-1] * 1000)
    context = PhaseContext(end_ms, end_ms, 64 * 1024, None)
    steady = analyze_steady(analysis, domain, context, 1000)
    selected = classified_clients(steady, 50)
    heatmap_rows = select_heatmap_rows(steady, 50)
    union = list(dict.fromkeys(selected + heatmap_rows))
    detail = collect_windowed_client_detail(analysis, domain, steady, union)
    _validate_results(domain, steady, selected, client_count)
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = list(write_client_summaries(metadata, domain, steady, str(output_dir)))
    if create_plots:
        line_detail = _subset_detail(detail, selected[:10])
        heatmap_detail = _subset_detail(detail, heatmap_rows)
        outputs.extend(
            path
            for path in (
                plot_aggregate_timeseries(metadata, domain, steady, str(output_dir)),
                plot_fairness_timeseries(metadata, domain, steady, str(output_dir)),
                plot_client_timeseries(
                    metadata, domain, steady, line_detail, str(output_dir)
                ),
                plot_client_ratio_heatmap(
                    metadata, domain, steady, heatmap_detail, str(output_dir)
                ),
            )
            if path
        )
    print(f"pass_one_seconds={pass_one_seconds:.3f}")
    print(f"total_reporting_seconds={time.perf_counter() - started:.3f}")
    print(f"peak_rss_mib={_peak_rss_mib():.3f}")
    print(f"input_bytes={path.stat().st_size}")
    print(f"output_bytes={sum(Path(output).stat().st_size for output in outputs)}")
    print(f"clients_analyzed={len(domain.clients)}")
    print(f"intervals_analyzed={len(domain.interval_times)}")
    print(f"retained_fleet_points={len(domain.fleet_points)}")
    print(f"selected_clients={','.join(client.label for client in selected[:10])}")


def _subset_detail(detail: WindowedClientDetail, clients) -> WindowedClientDetail:
    """Restrict a windowed detail to ``clients`` (kept in the given order)."""
    return WindowedClientDetail(
        list(clients),
        {client: detail.rates_mib_s[client] for client in clients},
        {client: detail.ratios[client] for client in clients},
    )


def _rate_for_client(client: int, interval: int, interval_count: int) -> int:
    if client < ANOMALY_GROUP_SIZE:
        return PERSISTENT_RATE_MIB_S
    transient_start = ANOMALY_GROUP_SIZE
    transient_end = 2 * ANOMALY_GROUP_SIZE
    if transient_start <= client < transient_end:
        if interval_count // 3 <= interval < interval_count // 2:
            return TRANSIENT_RATE_MIB_S
    return BASE_RATE_MIB_S


def _sample_is_missing(client: int, interval: int) -> bool:
    missing_start = 2 * ANOMALY_GROUP_SIZE
    missing_end = 3 * ANOMALY_GROUP_SIZE
    return missing_start <= client < missing_end and interval > 0 and interval % 17 == 0


def _sample_resets(client: int, interval: int, interval_count: int) -> bool:
    reset_start = 3 * ANOMALY_GROUP_SIZE
    reset_end = 4 * ANOMALY_GROUP_SIZE
    return reset_start <= client < reset_end and interval == interval_count // 2


def _live_row(
    runtime_ms: int,
    iso_timestamp: str,
    rank: object,
    done_bytes: int,
    service: str,
    mib_s: object,
    iops: object,
) -> List[object]:
    return [
        iso_timestamp,
        "",
        "READ",
        runtime_ms,
        rank,
        "",
        0,
        done_bytes,
        mib_s,
        iops,
        0,
        "",
        "",
        "",
        "",
        "",
        service,
    ]


def _validate_results(domain, steady, selected, client_count: int) -> None:
    if len(domain.clients) != client_count:
        raise RuntimeError(
            f"Expected {client_count} clients, analyzed {len(domain.clients)}"
        )
    persistent = LiveClientId("0", "client-00000")
    normal_rank = min(client_count - 1, 4 * ANOMALY_GROUP_SIZE)
    normal = LiveClientId(str(normal_rank), f"client-{normal_rank:05d}")
    if persistent not in selected:
        raise RuntimeError("Persistent synthetic underperformer was not selected")
    if steady.clients[persistent].classification != CLASS_PERSISTENT:
        raise RuntimeError("Persistent synthetic underperformer was misclassified")
    if abs(domain.clients[persistent].mean_mib_s - PERSISTENT_RATE_MIB_S) >= 0.01:
        raise RuntimeError("Persistent synthetic throughput was derived incorrectly")
    if abs(domain.clients[normal].mean_mib_s - BASE_RATE_MIB_S) >= 0.01:
        raise RuntimeError("Normal synthetic throughput was derived incorrectly")


def _peak_rss_mib() -> float:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    bytes_per_unit = 1 if sys.platform == "darwin" else 1024
    return raw * bytes_per_unit / (1024 * 1024)


def _benchmark_filename(client_count: int) -> str:
    return f"elbencho-r64K-c_{client_count:03d}-s_001-d_001_" "20260729Z120000.live.csv"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate and analyze a scalable synthetic elbencho live CSV",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "Full-scale acceptance target: run the default 10,000-client, "
            "3,600-interval workload in a Linux container limited to 512 MiB "
            "of RAM. Provide enough disk space for the generated multi-gigabyte "
            "CSV; the memory limit applies to the container, not the host."
        ),
    )
    parser.add_argument("--clients", type=int, default=10_000)
    parser.add_argument("--intervals", type=int, default=3_600)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--analyze-only", action="store_true")
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args()
    if args.clients < MIN_BENCHMARK_CLIENTS:
        parser.error(f"--clients must be at least {MIN_BENCHMARK_CLIENTS}")
    if args.intervals < 6:
        parser.error("--intervals must be at least 6")
    return args


def main() -> None:
    """Command entry point."""
    args = _parse_args()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    live_path = args.work_dir / _benchmark_filename(args.clients)
    if not args.analyze_only:
        generate_started = time.perf_counter()
        generate_live_csv(live_path, args.clients, args.intervals)
        print(f"generation_seconds={time.perf_counter() - generate_started:.3f}")
    if not live_path.is_file():
        raise FileNotFoundError(f"Benchmark input does not exist: {live_path}")
    run_analysis(
        live_path,
        args.work_dir / "report",
        args.clients,
        create_plots=not args.no_plots,
    )


if __name__ == "__main__":
    main()
