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

"""Record and verify which integration work items each CI shard ran.

The integration workflow runs one fixture per architecture and substrate.
Each shard writes a small manifest; the status job requires the manifests to
cover exactly the unsharded plan. Standard library only, so the status job
needs no runtime dependencies.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parent))
# pylint: disable-next=wrong-import-position
from scenario_planner import (
    ScenarioPlanningError,
    Substrate,
    WorkItem,
    plan_scenarios,
)

WORK_ITEMS_FILENAME = "work-items.json"
MANIFEST_SCHEMA = 1
SHARD_SUBSTRATES = tuple(item.value for item in Substrate if item is not Substrate.ALL)


def _label(item: WorkItem) -> str:
    return f"{item.scenario.name}/{item.substrate.value}"


def planned_work(substrate: str) -> list[str]:
    """Return the ordered scenario/substrate work items for one selector."""
    plan = plan_scenarios(substrate=substrate)
    return [_label(step) for step in plan if isinstance(step, WorkItem)]


class WorkItemRecorder:
    """Rewrite a test run's planned and finished work items after each item."""

    def __init__(self, path: Path, planned: Iterable[WorkItem]) -> None:
        self._path = path
        self._planned = [_label(item) for item in planned]
        self._items: list[dict[str, object]] = []
        self._write()

    def record(self, item: WorkItem, outcome: str, started: float) -> None:
        """Record one finished work item; outcome is "passed" or "failed"."""
        self._items.append(
            {
                "work_item": _label(item),
                "outcome": outcome,
                "seconds": round(time.monotonic() - started, 1),
            }
        )
        self._write()

    def _write(self) -> None:
        document = {"planned": self._planned, "items": self._items}
        temporary = self._path.with_name(self._path.name + ".tmp")
        temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)


def _read_timings(path: Path | None) -> list[dict[str, object]]:
    """Read ci-integration.sh's "step<TAB>seconds<TAB>exit-code" lines."""
    if path is None or not path.is_file():
        return []
    timings = []
    for line in path.read_text(encoding="utf-8").splitlines():
        step, seconds, exit_code = line.split("\t")
        timings.append(
            {"step": step, "seconds": int(seconds), "exit_code": int(exit_code)}
        )
    return timings


def _completed_work(work_items: Path | None) -> list[str]:
    """Return the passed work items, or none if the test action never started."""
    if work_items is None or not work_items.is_file():
        return []
    document = json.loads(work_items.read_text(encoding="utf-8"))
    return [
        item["work_item"] for item in document["items"] if item["outcome"] == "passed"
    ]


def _boot_id() -> str:
    try:
        return (
            Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        )
    except OSError:
        return "unknown"


def write_manifest(arguments: argparse.Namespace) -> None:
    """Write one shard's manifest; it must exist even when setup failed."""
    document = {
        "schema": MANIFEST_SCHEMA,
        "source_sha": arguments.source_sha,
        "architecture": arguments.architecture,
        "backend": arguments.backend,
        "substrate": arguments.substrate,
        "boot_id": _boot_id(),
        "exit_code": arguments.exit_code,
        "planned": planned_work(arguments.substrate),
        "completed": _completed_work(arguments.work_items),
        "timings": _read_timings(arguments.timings),
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")


def _shard_errors(
    shard: dict[str, object], expected_sha: str, backend: str
) -> list[str]:
    name = f"{shard.get('architecture')}/{shard.get('substrate')}"
    errors = []
    if shard.get("schema") != MANIFEST_SCHEMA:
        errors.append(f"{name}: unknown manifest schema")
    if shard.get("source_sha") != expected_sha:
        errors.append(f"{name}: tested {shard.get('source_sha')}, not {expected_sha}")
    if shard.get("backend") != backend:
        errors.append(f"{name}: used backend {shard.get('backend')}, not {backend}")
    if shard.get("exit_code") != 0:
        errors.append(f"{name}: lifecycle exit code {shard.get('exit_code')}")
    planned = shard.get("planned")
    if planned != planned_work(str(shard.get("substrate"))):
        errors.append(f"{name}: planned work differs from the scenario catalog")
    if Counter(shard.get("completed") or []) != Counter(planned or []):
        errors.append(f"{name}: completed work differs from planned work")
    return errors


def verify_manifests(
    shards: list[dict[str, object]],
    *,
    expected_sha: str,
    backend: str,
    architectures: Iterable[str],
) -> list[str]:
    """Return every reason the shard set is not a complete, successful run."""
    expected = {(arch, sub) for arch in architectures for sub in SHARD_SUBSTRATES}
    identities = Counter((s.get("architecture"), s.get("substrate")) for s in shards)
    errors = [
        f"duplicate manifest: {key}" for key, count in identities.items() if count > 1
    ]
    errors += [f"missing manifest: {key}" for key in sorted(expected - set(identities))]
    errors += [f"unexpected manifest: {key}" for key in set(identities) - expected]
    for shard in shards:
        errors += _shard_errors(shard, expected_sha, backend)
    full_plan = Counter(planned_work(Substrate.ALL.value))
    for architecture in sorted({arch for arch, _ in expected}):
        covered = Counter()
        for shard in shards:
            if shard.get("architecture") == architecture:
                covered.update(shard.get("completed") or [])
        if covered != full_plan:
            errors.append(f"{architecture}: shards do not cover the full plan once")
    return errors


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    plan = actions.add_parser("plan", help="print a selector's planned work items")
    plan.add_argument("substrate", choices=[item.value for item in Substrate])
    write = actions.add_parser("write", help="write one shard manifest")
    write.add_argument("--output", type=Path, required=True)
    write.add_argument("--source-sha", required=True)
    write.add_argument("--architecture", required=True)
    write.add_argument("--backend", required=True)
    write.add_argument("--substrate", required=True)
    write.add_argument("--exit-code", type=int, required=True)
    write.add_argument("--timings", type=Path)
    write.add_argument("--work-items", type=Path)
    verify = actions.add_parser("verify", help="verify a workflow's shard manifests")
    verify.add_argument("--manifests", type=Path, required=True)
    verify.add_argument("--source-sha", required=True)
    verify.add_argument("--backend", required=True)
    verify.add_argument("--architectures", required=True, help="comma-separated")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point used by ci-integration.sh and the workflow."""
    arguments = _parser().parse_args(argv)
    try:
        if arguments.action == "plan":
            print("\n".join(planned_work(arguments.substrate)))
            return 0
        if arguments.action == "write":
            write_manifest(arguments)
            return 0
        shards = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(arguments.manifests.glob("*.json"))
        ]
        errors = verify_manifests(
            shards,
            expected_sha=arguments.source_sha,
            backend=arguments.backend,
            architectures=arguments.architectures.split(","),
        )
    except (OSError, ValueError, KeyError, ScenarioPlanningError) as error:
        print(f"shard manifest error: {error}", file=sys.stderr)
        return 1
    for error in errors:
        print(f"shard manifest error: {error}", file=sys.stderr)
    if not errors:
        print(f"verified {len(shards)} shard manifests for {arguments.source_sha}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
