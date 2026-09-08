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

"""
Group nodes into fixed-size bins, balancing total Gbps per bin as evenly as possible.
Uses round-robin interleaving + local swap refinement.
"""

import csv
import sys
import argparse


def read_nodes(path: str) -> list[tuple[str, int]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [(row["InstanceName"], int(row["Gbps"])) for row in reader]


def _build_snake_bins(
    sorted_nodes: list[tuple[str, int]], n_bins: int
) -> list[list[tuple[str, int]]]:
    """Distribute nodes into bins using snake (zigzag) round-robin."""
    bins: list[list[tuple[str, int]]] = [[] for _ in range(n_bins)]
    for i, node in enumerate(sorted_nodes):
        cycle = i // n_bins
        pos = i % n_bins
        bin_idx = pos if cycle % 2 == 0 else (n_bins - 1 - pos)
        bins[bin_idx].append(node)
    return bins


def _find_best_swap(
    bin_a: list[tuple[str, int]],
    bin_b: list[tuple[str, int]],
    sum_a: int,
    sum_b: int,
) -> tuple[int, int] | None:
    """Find the node swap between two bins that most reduces their sum difference."""
    best_gain = 0
    best_swap = None
    for ai, node_a in enumerate(bin_a):
        for bi, node_b in enumerate(bin_b):
            delta = node_a[1] - node_b[1]
            new_max = sum_a - delta
            new_min = sum_b + delta
            old_range = sum_a - sum_b
            new_range = abs(new_max - new_min)
            gain = old_range - new_range
            if gain > best_gain:
                best_gain = gain
                best_swap = (ai, bi)
    return best_swap


def partition(
    nodes: list[tuple[str, int]], bin_size: int
) -> list[list[tuple[str, int]]]:
    """
    1. Sort nodes descending by Gbps.
    2. Assign via snake (zigzag) round-robin across bins — naturally balances heavy/light nodes.
    3. Local swap pass: repeatedly swap one node between the max-sum and min-sum bins
       if it reduces variance, until no improving swap exists.
    """
    n_bins = (len(nodes) + bin_size - 1) // bin_size
    sorted_nodes = sorted(nodes, key=lambda x: -x[1])
    bins = _build_snake_bins(sorted_nodes, n_bins)

    full_bins = (
        list(range(n_bins - 1)) if len(nodes) % bin_size != 0 else list(range(n_bins))
    )

    improved = True
    while improved:
        improved = False
        sums = [sum(g for _, g in b) for b in bins]
        max_idx = max(full_bins, key=lambda i, s=sums: s[i])
        min_idx = min(full_bins, key=lambda i, s=sums: s[i])
        if max_idx == min_idx:
            break

        best_swap = _find_best_swap(
            bins[max_idx], bins[min_idx], sums[max_idx], sums[min_idx]
        )
        if best_swap:
            ai, bi = best_swap
            bins[max_idx][ai], bins[min_idx][bi] = (
                bins[min_idx][bi],
                bins[max_idx][ai],
            )
            improved = True

    return bins


def compute_target(nodes: list[tuple[str, int]], bin_size: int) -> int:
    total = sum(g for _, g in nodes)
    n_bins = len(nodes) // bin_size
    return round(total / n_bins)


def print_summary(
    nodes: list[tuple[str, int]], bins: list[list[tuple[str, int]]], bin_size: int
) -> None:
    total_nodes = len(nodes)
    total_gbps = sum(g for _, g in nodes)
    target = compute_target(nodes, bin_size)
    n_full = total_nodes // bin_size
    n_partial = 1 if total_nodes % bin_size else 0

    col_w = max(len(str(len(bins))), 3)

    print("=" * 60, file=sys.stderr)
    print("Node Grouping Summary", file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"  Total nodes   : {total_nodes}", file=sys.stderr)
    print(f"  Total Gbps    : {total_gbps:,}", file=sys.stderr)
    print(f"  Bin size      : {bin_size}", file=sys.stderr)
    print(f"  Full bins     : {n_full}", file=sys.stderr)
    if n_partial:
        print(f"  Partial bin   : 1  ({total_nodes % bin_size} nodes)", file=sys.stderr)
    print(f"  Target Gbps   : {target:,} per bin", file=sys.stderr)
    print("-" * 60, file=sys.stderr)

    bin_gbps = [sum(g for _, g in b) for b in bins]
    full_gbps = bin_gbps[:-1] if n_partial else bin_gbps
    min_g, max_g = min(full_gbps), max(full_gbps)
    delta = max_g - min_g

    header = f"  {'Bin':>{col_w}}  {'Nodes':>5}  {'Gbps':>8}  {'vs Target':>10}  {'Cumul Gbps':>12}  {'Cumul Ideal':>12}"
    print(header, file=sys.stderr)
    print("  " + "-" * (len(header) - 2), file=sys.stderr)

    cumulative = 0
    for i, (b, g) in enumerate(zip(bins, bin_gbps)):
        is_partial = n_partial and i == len(bins) - 1
        diff = g - target
        diff_str = f"{diff:+,}" if not is_partial else "  (partial)"
        tag = "  [partial]" if is_partial else ""
        cumulative += g
        ideal_cumul = target * (i + 1)
        print(
            f"  {i+1:>{col_w}}  {len(b):>5}  {g:>8,}  {diff_str:>10}  {cumulative:>12,}  {ideal_cumul:>12,}{tag}",
            file=sys.stderr,
        )

    print("-" * 60, file=sys.stderr)
    if len(full_gbps) > 1:
        print(
            f"  Full-bin Gbps range : {min_g:,} – {max_g:,}  (delta: {delta:,})",
            file=sys.stderr,
        )
    print(f"  Full-bin totals     : min={min_g:,}  max={max_g:,}", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    node_counts = list(range(bin_size, total_nodes, bin_size))
    if not node_counts or node_counts[-1] != total_nodes:
        node_counts.append(total_nodes)
    node_counts.sort(reverse=True)
    nodes_str = ",".join(str(n) for n in node_counts)
    print(f"  --nodes {nodes_str}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="group_into_bins.py",
        description=(
            "Partition nodes from a CSV into fixed-size groups, balancing\n"
            "total Gbps per group as evenly as possible.\n\n"
            "Input CSV must have columns: InstanceName, Gbps\n\n"
            "Output (stdout): single comma-separated line of node names in group order.\n"
            "Output (stderr): per-bin summary table and overall statistics."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s node_gbps_weights.csv\n"
            "  %(prog)s --bin-size 40 node_gbps_weights.csv\n"
            "  %(prog)s node_gbps_weights.csv > groups.csv\n"
            "  %(prog)s node_gbps_weights.csv 2>/dev/null   # stdout only\n"
        ),
    )
    parser.add_argument(
        "input_csv",
        metavar="INPUT_CSV",
        help="Path to input CSV file with InstanceName and Gbps columns.",
    )
    parser.add_argument(
        "--bin-size",
        type=int,
        default=25,
        metavar="N",
        help="Number of nodes per group (default: 25).",
    )
    return parser.parse_args()


def reorder_bins_for_smooth_cumulative(
    bins: list[list[tuple[str, int]]], target: int
) -> list[list[tuple[str, int]]]:
    """
    Reorder bins (excluding any partial last bin) so the cumulative Gbps sum
    stays as close to the ideal linear ramp as possible.

    Strategy: greedy — at each position, pick the remaining bin whose selection
    minimizes |cumulative_so_far - ideal_at_this_step|.
    """
    has_partial = len({len(b) for b in bins}) > 1
    full_bins = bins[:-1] if has_partial else bins[:]
    partial = [bins[-1]] if has_partial else []

    remaining = list(full_bins)
    ordered = []
    cumulative = 0

    for step in range(1, len(full_bins) + 1):
        ideal = target * step
        best = None
        best_dev = float("inf")
        for b in remaining:
            dev = abs(cumulative + sum(g for _, g in b) - ideal)
            if dev < best_dev:
                best_dev = dev
                best = b
        ordered.append(best)
        cumulative += sum(g for _, g in best)
        remaining.remove(best)

    return ordered + partial


def main():
    args = parse_args()
    nodes = read_nodes(args.input_csv)

    if len(nodes) < args.bin_size:
        print(
            f"Error: only {len(nodes)} node(s) but bin size is {args.bin_size}."
            " Nothing to partition.",
            file=sys.stderr,
        )
        sys.exit(1)

    bins = partition(nodes, bin_size=args.bin_size)
    target = compute_target(nodes, args.bin_size)
    bins = reorder_bins_for_smooth_cumulative(bins, target)
    print_summary(nodes, bins, args.bin_size)
    print(",".join(n for b in bins for n, _ in b))


if __name__ == "__main__":
    main()
