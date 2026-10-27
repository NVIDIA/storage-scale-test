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

"""JSON results export of extract-mdtest-elbencho (fragment and ordinary document)."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_extract_mdtest_elbencho import (
    _DENSE_CSV,
    _DENSE_OUT,
    _EXTRACT_MOD,
)

_DATESTAMP = "20260812Z120000"
_ENV_USED = "MDTEST_BRANCH_FACTOR: 7\nMDTEST_ITEMS_PER_DIR: 100\nMDTEST_ITERATIONS: 3\n"
_FAST_RATE = "20000"
_SLOW_RATE = "16962"


def _stem(nodes: int, tasks: int, iteration: int) -> str:
    return f"mdtest-elbencho-c_{nodes:03d}-t_{tasks:03d}_{_DATESTAMP}_iter{iteration}"


def _truncated_csv() -> str:
    """The CSV of an iteration that stopped before the RMFILES phase."""
    return "\n".join(_DENSE_CSV.splitlines()[:3]) + "\n"


def _truncated_out() -> str:
    """The OUT file of an iteration that stopped after the WRITE phase."""
    return _DENSE_OUT.split("---")[0]


class MdtestResultsJsonCase(unittest.TestCase):
    """Builds synthetic result directories and runs the extractor on them."""

    def setUp(self) -> None:
        # pylint: disable=consider-using-with
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.results = self.root / "results"
        self.results.mkdir()
        (self.results / "env_used.yaml").write_text(_ENV_USED, encoding="utf-8")

    def add_execution(self, execution_id, nodes, tasks, status="SUCCESS") -> None:
        executions = self.results / "executions"
        executions.mkdir(exist_ok=True)
        (executions / f"{execution_id}.sh").write_text(
            "export ELBENCHO_EXECUTION_KIND=mdtest\n"
            f"export nodes={nodes}\nexport tasks_per_node={tasks}\n"
            "export MDTEST_BRANCH_FACTOR=7\nexport MDTEST_ITEMS_PER_DIR=100\n"
            "export MDTEST_ITERATIONS=3\n",
            encoding="utf-8",
        )
        (executions / f"{execution_id}.status").write_text(
            f"{status}\n", encoding="utf-8"
        )

    def add_iteration(
        self, nodes, tasks, iteration, csv_text=None, out_text=None
    ) -> None:
        stem = _stem(nodes, tasks, iteration)
        if iteration == 2 and csv_text is None:
            csv_text = _DENSE_CSV.replace(_SLOW_RATE, _FAST_RATE)
        (self.results / f"{stem}.csv").write_text(
            _DENSE_CSV if csv_text is None else csv_text, encoding="utf-8"
        )
        (self.results / f"{stem}.out").write_text(
            _DENSE_OUT if out_text is None else out_text, encoding="utf-8"
        )

    def add_complete(self, execution_id="0001", nodes=1, tasks=8) -> None:
        self.add_execution(execution_id, nodes, tasks)
        for iteration in (1, 2, 3):
            self.add_iteration(nodes, tasks, iteration)

    def run_main(self, *arguments):
        """Run main(); return (exit code, stdout, stderr)."""
        out_bytes = io.BytesIO()
        out = io.TextIOWrapper(out_bytes, encoding="utf-8", write_through=True)
        err = io.StringIO()
        code = 0
        with (
            mock.patch.object(
                sys, "argv", ["extract-mdtest-elbencho.py", *map(str, arguments)]
            ),
            mock.patch.object(_EXTRACT_MOD, "plot_all"),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                _EXTRACT_MOD.main()
            except SystemExit as exit_request:
                code = exit_request.code
        out.flush()
        return code, out_bytes.getvalue().decode("utf-8"), err.getvalue()

    def fragment(self, *extra) -> dict:
        path = self.root / "fragment.json"
        code, _, err = self.run_main(self.results, "--results-fragment", path, *extra)
        self.assertIn(code, (0, None), err)
        return json.loads(path.read_text(encoding="utf-8"))

    def by_id(self, fragment: dict) -> dict:
        return {entry["execution_id"]: entry for entry in fragment["executions"]}


class TestFragment(MdtestResultsJsonCase):
    """The fragment engines write for batch assembly."""

    def test_complete_execution(self) -> None:
        self.add_complete()
        fragment = self.fragment()
        self.assertEqual(fragment["kind"], "mdtest")
        self.assertEqual(fragment["fragment_version"], 1)
        entry = self.by_id(fragment)["0001"]
        self.assertEqual(entry["result_state"], "complete")
        self.assertEqual(entry["coordinate_key"], "md/c001/t008/b7/i100")
        self.assertEqual(
            entry["coordinates"],
            {
                "nodes": 1,
                "tasks_per_node": 8,
                "branch_factor": 7,
                "items_per_dir": 100,
                "iterations": 3,
            },
        )
        self.assertIsNone(entry["dataset"])
        self.assertEqual(entry["problems"], [])
        self.assertEqual(
            [record["path"] for record in entry["result_files"]],
            sorted(
                f"{_stem(1, 8, number)}.{extension}"
                for number in (1, 2, 3)
                for extension in ("csv", "out")
            ),
        )
        iterations = entry["results"]["iterations"]
        self.assertEqual([item["iteration"] for item in iterations], [1, 2, 3])
        self.assertTrue(all(item["state"] == "complete" for item in iterations))
        create = iterations[0]["phases"][0]
        self.assertEqual(create["phase"], "create")
        self.assertEqual(create["ops_per_s"], 16962.0)
        self.assertEqual(create["elapsed_ms"], 588)
        self.assertEqual(
            create["latency_us"], {"min": 108.0, "avg": 469.0, "max": 14539.0}
        )
        self.assertEqual(
            create["histogram_us"],
            {
                "buckets_us": [256.0, 512.0, 1024.0, 16384.0],
                "counts": [500, 4000, 5000, 500],
            },
        )
        self.assertEqual(
            [phase["phase"] for phase in iterations[0]["phases"]],
            ["create", "stat", "delete"],
        )

    def test_aggregate_uses_validated_iterations(self) -> None:
        self.add_complete()
        aggregate = self.by_id(self.fragment())["0001"]["results"]["aggregate"]
        self.assertEqual(aggregate["iterations_expected"], 3)
        self.assertEqual(aggregate["iterations_used"], 3)
        create = aggregate["phases"][0]
        self.assertEqual(create["phase"], "create")
        self.assertEqual(create["ops_per_s"]["min"], 16962.0)
        self.assertEqual(create["ops_per_s"]["max"], 20000.0)
        self.assertAlmostEqual(create["ops_per_s"]["mean"], 17974.67)
        self.assertAlmostEqual(create["ops_per_s"]["stddev"], 1753.99, places=2)
        self.assertEqual(create["mean_elapsed_ms"], 588)
        self.assertEqual(
            create["latency_us"],
            {
                "p0": 256.0,
                "p50": 1024.0,
                "p90": 1024.0,
                "p99": 16384.0,
                "p100": 16384.0,
            },
        )
        self.assertEqual(create["latency_method"], "pooled_histogram_bucket")

    def test_incomplete_iteration_is_kept_and_excluded_from_aggregate(self) -> None:
        self.add_execution("0001", 1, 8)
        self.add_iteration(1, 8, 1)
        self.add_iteration(1, 8, 2, _truncated_csv(), _truncated_out())
        self.add_iteration(1, 8, 3)
        entry = self.by_id(self.fragment())["0001"]
        self.assertEqual(entry["result_state"], "partial")
        second = entry["results"]["iterations"][1]
        self.assertEqual(second["state"], "incomplete")
        self.assertEqual(second["problems"][0]["code"], "iteration_incomplete")
        self.assertIn("RMFILES", second["problems"][0]["detail"])
        self.assertEqual([p["phase"] for p in second["phases"]], ["create", "stat"])
        self.assertEqual(entry["results"]["aggregate"]["iterations_used"], 2)

    def test_incomplete_iteration_is_still_excluded_from_text_report(self) -> None:
        self.add_execution("0001", 1, 8)
        self.add_iteration(1, 8, 1)
        self.add_iteration(1, 8, 2, _truncated_csv(), _truncated_out())
        code, out, _ = self.run_main(self.results, "--markdown")
        self.assertIn(code, (0, None))
        self.assertIn("| **Total Iterations** | 1 |", out)

    def test_missing_iteration(self) -> None:
        self.add_execution("0001", 1, 8)
        self.add_iteration(1, 8, 1)
        self.add_iteration(1, 8, 3)
        entry = self.by_id(self.fragment())["0001"]
        self.assertEqual(entry["result_state"], "partial")
        self.assertEqual(
            [problem["code"] for problem in entry["problems"]], ["iterations_missing"]
        )
        self.assertIn("2", entry["problems"][0]["detail"])

    def test_execution_without_results_is_missing(self) -> None:
        self.add_execution("0001", 1, 8)
        entry = self.by_id(self.fragment())["0001"]
        self.assertEqual(entry["result_state"], "missing")
        self.assertIsNone(entry["results"])
        self.assertEqual(entry["result_files"], [])

    def test_unparseable_results(self) -> None:
        self.add_execution("0001", 1, 8)
        for iteration in (1, 2, 3):
            self.add_iteration(1, 8, iteration, "", "")
        entry = self.by_id(self.fragment())["0001"]
        self.assertEqual(entry["result_state"], "unparseable")

    def test_filtered_execution_is_not_reported(self) -> None:
        self.add_complete("0001", 1, 8)
        self.add_complete("0002", 2, 8)
        by_id = self.by_id(self.fragment("--only-nodes", "2"))
        self.assertEqual(by_id["0002"]["result_state"], "complete")
        self.assertEqual(by_id["0001"]["result_state"], "not_reported")
        self.assertIsNone(by_id["0001"]["results"])
        thread_filtered = self.by_id(self.fragment("--only-threads", "16"))
        self.assertEqual(thread_filtered["0001"]["result_state"], "not_reported")

    def test_unmatched_result_files_get_a_null_execution_id(self) -> None:
        self.add_complete("0001", 1, 8)
        for iteration in (1, 2, 3):
            self.add_iteration(3, 4, iteration)
        fragment = self.fragment()
        unmatched = [e for e in fragment["executions"] if e["execution_id"] is None]
        self.assertEqual(len(unmatched), 1)
        self.assertEqual(unmatched[0]["coordinate_key"], "md/c003/t004/b7/i100")
        self.assertEqual(unmatched[0]["result_state"], "complete")

    def test_non_mdtest_ledger_entries_are_ignored(self) -> None:
        self.add_complete()
        (self.results / "executions" / "0002.sh").write_text(
            "export ELBENCHO_EXECUTION_KIND=io\nexport nodes=1\n", encoding="utf-8"
        )
        self.assertEqual(list(self.by_id(self.fragment())), ["0001"])

    def test_fragment_is_deterministic(self) -> None:
        self.add_complete()
        self.add_complete("0002", 2, 8)
        first = self.fragment()
        second = self.fragment()
        self.assertEqual(
            json.dumps(first, sort_keys=False), json.dumps(second, sort_keys=False)
        )
        self.assertEqual(
            [entry["execution_id"] for entry in first["executions"]], ["0001", "0002"]
        )


class TestOrdinaryDocument(MdtestResultsJsonCase):
    """--to-json on an ordinary result directory."""

    def test_default_path_is_results_json_in_output_dir(self) -> None:
        self.add_complete()
        code, _, err = self.run_main(self.results, "--to-json")
        self.assertIn(code, (0, None), err)
        document = json.loads((self.results / "results.json").read_text("utf-8"))
        self.assertEqual(
            document["schema"], "storage-scale-test/filesystem-batch-results"
        )
        self.assertEqual(document["executions"][0]["result_state"], "complete")
        self.assertEqual(document["executions"][0]["ledger_state"], "success")
        self.assertTrue(document["batch_outcome"]["complete"])

    def test_output_dir_receives_the_document(self) -> None:
        self.add_complete()
        output = self.root / "out"
        code, _, err = self.run_main(self.results, "--to-json", "--output-dir", output)
        self.assertIn(code, (0, None), err)
        self.assertTrue((output / "results.json").is_file())
        self.assertFalse((self.results / "results.json").exists())

    def test_to_json_takes_no_path(self) -> None:
        self.add_complete()
        code, _, _ = self.run_main(f"--to-json={self.root / 'doc.json'}", self.results)
        self.assertEqual(code, 2)

    def test_requires_exactly_one_input_dir(self) -> None:
        self.add_complete()
        other = self.root / "other"
        other.mkdir()
        code, _, err = self.run_main(self.results, other, "--to-json")
        self.assertEqual(code, 2, err)

    def test_require_complete_without_to_json_is_a_usage_error(self) -> None:
        self.add_complete()
        code, _, _ = self.run_main(self.results, "--require-complete")
        self.assertEqual(code, 2)

    def test_non_finite_elapsed_makes_the_iteration_incomplete(self) -> None:
        self.add_execution("0001", 1, 8)
        for iteration in (1, 2):
            self.add_iteration(1, 8, iteration)
        rows = _DENSE_CSV.splitlines()
        fields = rows[1].split(",")
        fields[18] = "nan"  # time ms [last] of the WRITE (create) row
        rows[1] = ",".join(fields)
        self.add_iteration(1, 8, 3, csv_text="\n".join(rows) + "\n")
        code, _, err = self.run_main(self.results, "--to-json")
        self.assertIn(code, (0, None), err)
        document = json.loads((self.results / "results.json").read_text("utf-8"))
        iteration = document["executions"][0]["results"]["iterations"][2]
        self.assertEqual(iteration["state"], "incomplete")
        create = next(p for p in iteration["phases"] if p["phase"] == "create")
        self.assertIsNone(create["elapsed_ms"])

    def test_require_complete_passes_for_complete_results(self) -> None:
        self.add_complete()
        code, _, err = self.run_main(self.results, "--to-json", "--require-complete")
        self.assertIn(code, (0, None), err)

    def test_require_complete_exits_3_after_writing_partial_results(self) -> None:
        self.add_execution("0001", 1, 8)
        self.add_iteration(1, 8, 1)
        code, _, _ = self.run_main(self.results, "--to-json", "--require-complete")
        self.assertEqual(code, 3)
        document = json.loads((self.results / "results.json").read_text("utf-8"))
        self.assertFalse(document["batch_outcome"]["complete"])

    def test_documents_differ_only_in_generation_time(self) -> None:
        self.add_complete()
        documents = []
        target = self.results / "results.json"
        for _ in range(2):
            self.run_main(self.results, "--to-json")
            document = json.loads(target.read_text("utf-8"))
            document["generator"].pop("generated_at", None)
            documents.append(document)
        self.assertEqual(documents[0], documents[1])


if __name__ == "__main__":
    unittest.main()
