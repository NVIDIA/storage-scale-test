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

"""Hand-built results documents for HTML report tests (invented values)."""

from __future__ import annotations

from pathlib import Path

from lib import filesystem_results as results
from lib.elbencho_live_results import CLIENT_COLUMNS

MIB = 1 << 20
INTERVAL_MS = 1000
_ZERO_SHA = "0" * 64


def _rate(gbs: float, block: int, elapsed_ms: int = 60000) -> dict:
    bytes_per_s = round(gbs * 1e9)
    return {
        "ops_per_s": round(bytes_per_s / block, 2),
        "bytes_per_s": bytes_per_s,
        "elapsed_ms": elapsed_ms,
    }


def io_phase(name: str, gbs: float, block: int = MIB, live: dict | None = None):
    """One IO phase with rates, latency and a small histogram."""
    return {
        "phase": name,
        "completion_state": None,
        "elbencho_first": _rate(gbs, block),
        "elbencho_last": _rate(gbs * 0.98, block, 61000),
        "latency_us": {
            "min": 40.0,
            "avg": 600.0,
            "max": 9000.0,
            "p1": 80.0,
            "p50": 500.0,
            "p75": 700.0,
            "p99": 3000.0,
        },
        "histogram_us": {"buckets_us": [64.0, 512.0, 4096.0], "counts": [5, 900, 3]},
        "work": None,
        "configured_time_limit_s": 60,
        "command": ["elbencho", f"--{name}", "/mnt/test/data"],
        "live": live if live is not None else {"reason": "not_captured"},
    }


def io_coordinates(nodes=1, threads=4, depth=1, block=MIB, access="sequential"):
    """IO coordinates for a write/read execution."""
    return {
        "nodes": nodes,
        "threads_per_node": threads,
        "io_depth": depth,
        "io_size_spec": "1M",
        "phase_plan": "write_read",
        "write_block_size_bytes": block,
        "write_access": access,
        "read_block_size_bytes": block,
        "read_access": access,
        "direct_io": True,
        "run_to_completion": False,
        "file_layout": "worker-directories",
        "single_big_file": False,
        "all_nodes_all_data": False,
    }


def _key(coords: dict) -> str:
    return (
        f"io/c{coords['nodes']:03d}/s{coords['threads_per_node']:03d}/"
        f"d{coords['io_depth']:03d}/w1M-seq/r1M-seq/dio"
    )


def io_execution(execution_id, coords: dict, phases: list, group_id="0000"):
    """A complete IO execution."""
    return {
        "execution_id": execution_id,
        "group_id": group_id,
        "kind": "io",
        "definition_sha256": _ZERO_SHA,
        "ledger_state": "success",
        "result_state": "complete",
        "problems": [],
        "coordinates": coords,
        "coordinate_key": _key(coords),
        "dataset": None,
        "result_files": [],
        "log": None,
        "results": {"phases": phases, "workload": None},
    }


def md_execution(execution_id, nodes, tasks, rate, group_id="0001"):
    """A complete metadata execution with one iteration."""
    latency = {"p0": 10.0, "p50": 200.0, "p90": 400.0, "p99": 900.0, "p100": 2000.0}
    phases = [
        {
            "phase": op,
            "ops_per_s": {
                "mean": rate * factor,
                "stddev": 10.0,
                "min": rate * factor - 5,
                "max": rate * factor + 5,
            },
            "mean_elapsed_ms": 5000.0,
            "latency_us": latency,
            "latency_method": "pooled_histogram_bucket",
        }
        for op, factor in (("create", 1.0), ("stat", 3.0), ("delete", 2.0))
    ]
    coords = {
        "nodes": nodes,
        "tasks_per_node": tasks,
        "branch_factor": 4,
        "items_per_dir": 100,
        "iterations": 1,
    }
    return {
        "execution_id": execution_id,
        "group_id": group_id,
        "kind": "mdtest",
        "definition_sha256": _ZERO_SHA,
        "ledger_state": "success",
        "result_state": "complete",
        "problems": [],
        "coordinates": coords,
        "coordinate_key": f"md/c{nodes:03d}/t{tasks:03d}/b4/i100",
        "dataset": None,
        "result_files": [],
        "log": None,
        "results": {
            "iterations": [],
            "aggregate": {
                "iterations_expected": 1,
                "iterations_used": 1,
                "phases": phases,
            },
        },
    }


def group(group_id: str, kind: str, target: str, settings: dict | None = None):
    """A group record."""
    return {
        "group_id": group_id,
        "kind": kind,
        "label": None,
        "output_relative": f"groups/{group_id}",
        "target": {"name": target, "recorded": True},
        "settings_sha256": {"env_used_sh": _ZERO_SHA, "env_used_yaml": _ZERO_SHA},
        "env_override": None,
        "settings": settings or {"STORAGE_TARGET_NAME": target},
        "execution_ids": [],
    }


def document(groups: list, executions: list, root: str = "/mnt/test/results"):
    """A schema 1.0 document around groups and executions."""
    for item in groups:
        item["execution_ids"] = [
            e["execution_id"]
            for e in executions
            if e["group_id"] == item["group_id"] and e["execution_id"]
        ]
    generator = results.generator_block(["extract-filesystem.py", root], {})
    generator["generated_at"] = "2026-10-26T12:00:00Z"
    generator["version"] = "v0.0.0"
    ledger = {state: 0 for state in results.LEDGER_STATES}
    ledger["success"] = len(executions)
    states = {state: 0 for state in results.RESULT_STATES}
    states["complete"] = len(executions)
    return {
        "schema": results.SCHEMA,
        "schema_version": results.SCHEMA_VERSION,
        "generator": generator,
        "batch": {
            "root": root,
            "datestamp": "20261026Z120000",
            "manifest_version": None,
            "manifest_sha256": None,
            "sealed": None,
            "substrate": "ssh",
        },
        "batch_outcome": {
            "complete": True,
            "executions": {"total": len(executions), **ledger},
            "results": states,
            "groups_reported": [g["group_id"] for g in groups],
            "groups_not_reported": [],
        },
        "groups": groups,
        "executions": executions,
        "diagnostics": [],
    }


def client_names(count: int) -> list[str]:
    """Invented client names."""
    return [f"host{i:03d} [rank {i}]" for i in range(count)]


def write_sidecar(base: Path, name: str, clients: list[str], intervals: int) -> dict:
    """Write a full sidecar where client i runs at (i + 1) * 10 MB/s."""
    path = base / "results.live" / name
    header = {
        "schema": results.LIVE_SCHEMA,
        "schema_version": results.LIVE_SCHEMA_VERSION,
        "execution_id": name.split(".")[0],
        "phase": name.split(".")[1],
        "clients": clients,
    }
    with results.SidecarWriter(path, header) as writer:
        for step in range(1, intervals + 1):
            values = [(i + 1) * 10_000_000 for i in range(len(clients))]
            values[0] = None if step == 2 else values[0]
            writer.write_interval(step * INTERVAL_MS, values)
    return {
        "path": f"results.live/{name}",
        "sha256": writer.sha256,
        "size_bytes": writer.size_bytes,
        "clients": len(clients),
        "intervals": intervals,
    }


def _summary(clients: list[str]) -> dict:
    """Every client's summary; client i has share (i + 1) / (N / 2)."""
    columns = list(CLIENT_COLUMNS)
    middle = len(clients) / 2
    rows = []
    for index, name in enumerate(clients):
        rate = (index + 1) * 10_000_000
        ratio = round((index + 1) / middle, 4)
        record = {
            "client": name,
            "rank": index,
            "class": "ok" if ratio > 0.9 else "persistent",
            "mean_bytes_per_s": rate,
            "median_bytes_per_s": rate,
            "mean_ratio_to_fleet_median": ratio,
            "steady_bytes_per_s": rate,
            "steady_ratio": ratio,
            "worst_5s_ratio": ratio,
            "worst_30s_ratio": ratio,
            "finish_elapsed_ms": 10000,
            "valid_samples": 10,
            "missing_samples": 0,
            "invalid_samples": 0,
        }
        rows.append([record.get(column) for column in columns])
    return {"columns": columns, "rows": rows}


def live_object(clients: list[str], intervals: int, ref: dict | None) -> dict:
    """An analyzed live object; ``ref`` is a sidecar reference or None."""
    elapsed = [step * INTERVAL_MS for step in range(1, intervals + 1)]
    total = sum((i + 1) * 10_000_000 for i in range(len(clients)))
    jain = {"end_ms": elapsed, "clients": [len(clients)] * intervals}
    jain["jain"] = [0.99] * intervals
    window = {
        "window_ms": 5000,
        "windows": intervals,
        "jain": {"min": 0.98, "p10": 0.985, "p50": 0.99},
        "jain_min_at_ms": 2000,
        "share_below_0_95": 0.0,
        "min_over_median_p10": 0.9,
        "floor_p50": 0.95,
        "series": jain,
    }
    return {
        "source": {"path": "x.live.csv", "sha256": _ZERO_SHA, "size_bytes": 1},
        "configured_interval_ms": INTERVAL_MS,
        "client_mode": "service",
        "domain": {"phase": "write", "mix_type": "", "occurrence": 0},
        "intervals": intervals,
        "clients": len(clients),
        "steady": {
            "start_ms": INTERVAL_MS,
            "end_ms": intervals * INTERVAL_MS,
            "end_source": "elbencho_first",
            "reason": None,
        },
        "aggregate_series": {
            "elapsed_ms": elapsed,
            "bytes_per_s": [total] * intervals,
            "ops_per_s": [100.0] * intervals,
            "io_latency_avg_us": [500.0] * intervals,
        },
        "fleet_series": {
            "elapsed_ms": elapsed,
            "client_count": [len(clients)] * intervals,
            "bytes_per_s_p10": [10_000_000] * intervals,
            "bytes_per_s_p50": [20_000_000] * intervals,
            "bytes_per_s_p90": [30_000_000] * intervals,
            "bytes_per_s_mean": [20_000_000] * intervals,
            "bytes_per_s_stddev": [1_000_000] * intervals,
        },
        "fairness": {"interval": None, "5s": window, "30s": None},
        "latency": None,
        "distribution": {
            "client_mean_bytes_per_s": {
                "min": 10_000_000,
                "p10": 30_000_000,
                "p50": 150_000_000,
                "p90": 270_000_000,
                "max": 300_000_000,
                "cv": 0.5,
            },
            "min_over_median": 0.07,
            "max_over_median": 2.0,
            "final_done_bytes": {"min": 1, "p50": 2, "max": 3, "cv": 0.5},
            "finish_elapsed_ms": {"min": 1000, "p50": 2000, "max": 3000},
            "persistent_clients": 1,
            "transient_clients": 0,
        },
        "clients_summary": _summary(clients),
        "client_series": ref,
        "diagnostics": {
            "missing_samples": 0,
            "counter_resets": 0,
            "non_adjacent_samples": 0,
        },
    }


def duplicate_styles(specs: dict) -> list[tuple[str, str, tuple]]:
    """(chart, name, style) of line series sharing colour, dash and axis with
    an earlier series of the same chart: indistinguishable in the legend."""
    clashes = []
    for anchor, spec in specs.items():
        seen: set = set()
        for item in spec.get("series", []) if spec.get("kind") == "line" else []:
            style = (item["color"], item["dash"], item.get("axis"))
            if style in seen:
                clashes.append((anchor, item["name"], style))
            seen.add(style)
    return clashes
