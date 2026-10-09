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

"""Emit CSV mapping idle Slurm node names to Gbps from prefix rules."""

import argparse
import csv
import json
import math
import os
from pathlib import Path
import subprocess
import sys
from typing import List, Optional, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.project_version import (  # pylint: disable=wrong-import-position
    add_version_argument,
)

PARTITION_ENV_VAR = "PARTITION"
SINFO_BIN = "sinfo"
SINFO_OUTPUT_FORMAT = "%N"
# Idle includes nodes with no jobs; IDLE+DRAIN nodes still match -t idle — exclude via -t drain.
# Slurm 25.11 sinfo: --states drain matches DRAINING and DRAINED (see sinfo(1) DRAIN description).
SINFO_STATE_IDLE = "idle"
SINFO_STATE_DRAIN = "drain"
CSV_COL_INSTANCE = "InstanceName"
CSV_COL_GBPS = "Gbps"
_CONFIG_ENVIRONMENT_PROBE = (
    "import json, os, sys; json.dump([dict(os.environ), sys.argv[1]], sys.stdout)"
)


def _eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)


def _parse_prefix_gbps_arg(raw: str) -> Tuple[str, float]:
    if "," not in raw:
        raise argparse.ArgumentTypeError(
            f"expected 'PREFIX,{CSV_COL_GBPS}' with a comma, got {raw!r}"
        )
    prefix, gbps_str = raw.split(",", 1)
    prefix = prefix.strip()
    gbps_str = gbps_str.strip()
    if not prefix:
        raise argparse.ArgumentTypeError("prefix must be non-empty")
    try:
        gbps = float(gbps_str)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid Gbps value {gbps_str!r}: {exc}"
        ) from exc
    return prefix, gbps


def _gbps_to_csv_int(gbps: float) -> int:
    """Floor Gbps to an integer for CSV output."""
    return int(math.floor(gbps))


def _sinfo_node_lines(
    partition: str, states: str, environment: dict[str, str]
) -> List[str]:
    """Return node names (one per line) for nodes in partition matching Slurm state(s)."""
    cmd = [
        SINFO_BIN,
        "-N",
        "-p",
        partition,
        "-t",
        states,
        "-h",
        "-o",
        SINFO_OUTPUT_FORMAT,
    ]
    try:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
    except OSError as exc:
        _eprint(f"failed to run {SINFO_BIN}: {exc}")
        sys.exit(1)
    if result.returncode != 0:
        _eprint(
            f"{SINFO_BIN} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
        sys.exit(1)
    lines = [ln.strip() for ln in result.stdout.splitlines()]
    return [ln for ln in lines if ln]


def _load_idle_nodes(partition: str, environment: dict[str, str]) -> List[str]:
    """Idle nodes with no jobs, excluding drain/draining/drained (still reported as idle)."""
    idle = {*_sinfo_node_lines(partition, SINFO_STATE_IDLE, environment)}
    drain = {*_sinfo_node_lines(partition, SINFO_STATE_DRAIN, environment)}
    return sorted(idle - drain)


def _longest_matching_gbps(
    node: str, pairs_longest_first: Sequence[Tuple[str, float]]
) -> Optional[float]:
    """pairs_longest_first must be sorted by descending prefix length."""
    for prefix, gbps in pairs_longest_first:
        if node.startswith(prefix):
            return gbps
    return None


def _write_csv(nodes: Sequence[str], pairs: Sequence[Tuple[str, float]], out) -> None:
    writer = csv.writer(out)
    writer.writerow([CSV_COL_INSTANCE, CSV_COL_GBPS])
    for node in nodes:
        gbps = _longest_matching_gbps(node, pairs)
        if gbps is not None:
            writer.writerow([node, _gbps_to_csv_int(gbps)])


def _partition_from_env() -> str:
    return os.environ.get(PARTITION_ENV_VAR, "").strip()


def _configured_environment() -> tuple[dict[str, str], str]:
    """Load site exports and partition once, including module-provided commands."""
    config_path = _REPO_ROOT / "env.sh"
    if not config_path.is_file():
        return dict(os.environ), ""
    command = [
        "bash",
        "-c",
        'readonly _sst_sinfo_config="$1" _sst_sinfo_python="$2" '
        '_sst_sinfo_probe="$3"; '
        'source "$_sst_sinfo_config" >/dev/null || exit; '
        '"$_sst_sinfo_python" -E -S -c "$_sst_sinfo_probe" "${partition:-}"',
        "sinfo-config",
        str(config_path),
        sys.executable,
        _CONFIG_ENVIRONMENT_PROBE,
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as error:
        _eprint(f"failed to load {config_path}: {error}")
        sys.exit(1)
    if result.returncode:
        _eprint(f"failed to load {config_path}: {result.stderr.strip()}")
        sys.exit(1)
    if result.stderr:
        _eprint(result.stderr, end="")
    try:
        environment, partition = json.loads(result.stdout)
    except ValueError:
        _eprint(
            f"failed to load {config_path}: missing or invalid environment snapshot"
        )
        sys.exit(1)
    return environment, partition


def _build_arg_parser() -> argparse.ArgumentParser:
    desc = (
        "List idle nodes in the Slurm partition from "
        f"--partition, ${PARTITION_ENV_VAR}, or env.sh, excluding drain/draining/drained "
        "nodes, match each node name to the "
        "longest PREFIX that is a prefix of the name, and print CSV "
        f"({CSV_COL_INSTANCE},{CSV_COL_GBPS}) to stdout."
    )
    parser = argparse.ArgumentParser(description=desc)
    add_version_argument(parser)
    parser.add_argument(
        "--partition", help="Slurm partition; overrides PARTITION and env.sh."
    )
    parser.add_argument(
        "prefix_gbps",
        nargs="+",
        metavar="PREFIX,GBPS",
        type=_parse_prefix_gbps_arg,
        help="One or more prefix/Gbps pairs (comma-separated, prefix first)",
    )
    return parser


def main() -> None:
    parser = _build_arg_parser()
    args = parser.parse_args()
    # Preserve caller selection before site configuration changes its exports.
    partition = args.partition or _partition_from_env()
    environment, config_partition = _configured_environment()
    partition = (partition or config_partition).strip()
    if not partition:
        _eprint(
            f"error: no partition selected; use --partition, set {PARTITION_ENV_VAR}, "
            "or configure partition in env.sh"
        )
        sys.exit(1)
    nodes = sorted({*_load_idle_nodes(partition, environment)})
    pairs_sorted = sorted(args.prefix_gbps, key=lambda item: len(item[0]), reverse=True)
    _write_csv(nodes, pairs_sorted, sys.stdout)


if __name__ == "__main__":
    main()
