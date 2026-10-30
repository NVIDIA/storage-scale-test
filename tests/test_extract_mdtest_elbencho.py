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

"""Parser coverage for dense (single flat directory) mdtest-elbencho results.

The dense layout changes only what elbencho is told to do on disk, not the shape
of its result files, so the metadata analysis must handle them without changes.
The fixture below comes from a real dense run (elbencho 3.1-2, "-n 0 -N 1250"
against one flat directory), with paths anonymized and latency histogram buckets
rounded to whole microseconds so the expected percentiles are easy to follow.
"""

import contextlib
import csv
import importlib
import importlib.util
import io
import shutil
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _stub_submodule(name: str, **attributes) -> types.ModuleType:
    """Register a placeholder submodule of an already-stubbed parent package."""
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent_name, _, leaf = name.rpartition(".")
    setattr(sys.modules[parent_name], leaf, module)
    return module


def _install_extract_heavy_dep_stubs() -> None:
    """Stubs so extract-mdtest-elbencho.py loads without optional packages.

    Sibling test modules stub matplotlib with only the submodules they need, and
    whichever module pytest imports first wins, so fill in any piece this
    extractor imports that is missing rather than assuming a bare interpreter.
    """
    try:
        importlib.import_module("matplotlib.axes")
        importlib.import_module("matplotlib.lines")
        importlib.import_module("matplotlib.pyplot")
        importlib.import_module("matplotlib.ticker")
    except ImportError:
        if "matplotlib" not in sys.modules:
            sys.modules["matplotlib"] = types.ModuleType("matplotlib")
        # A stub parent has no __path__, which import needs to see a package.
        if not hasattr(sys.modules["matplotlib"], "__path__"):
            sys.modules["matplotlib"].__path__ = []

        class _Placeholder:
            """Stand-in for a matplotlib class used only in type hints."""

        if "matplotlib.pyplot" not in sys.modules:
            _stub_submodule("matplotlib.pyplot")
        if "matplotlib.axes" not in sys.modules:
            _stub_submodule("matplotlib.axes", Axes=_Placeholder)
        if "matplotlib.lines" not in sys.modules:
            _stub_submodule("matplotlib.lines", Line2D=_Placeholder)
        if "matplotlib.ticker" not in sys.modules:
            _stub_submodule("matplotlib.ticker", FuncFormatter=lambda *_a, **_k: None)

    if "numpy" not in sys.modules:
        stub_np = types.ModuleType("numpy")

        class _NdArray:
            """Placeholder for plotting type hints."""

        stub_np.ndarray = _NdArray
        sys.modules["numpy"] = stub_np


_install_extract_heavy_dep_stubs()

_EXTRACT_PATH = _REPO_ROOT / "utils" / "extract-mdtest-elbencho.py"
_EXTRACT_SPEC = importlib.util.spec_from_file_location(
    "csp_extract_mdtest_elbencho", _EXTRACT_PATH
)
assert _EXTRACT_SPEC and _EXTRACT_SPEC.loader
_EXTRACT_MOD = importlib.util.module_from_spec(_EXTRACT_SPEC)
sys.modules["csp_extract_mdtest_elbencho"] = _EXTRACT_MOD
_EXTRACT_SPEC.loader.exec_module(_EXTRACT_MOD)

_DATESTAMP = "20260812Z120000"
_STEM = f"mdtest-elbencho-c_001-t_008_{_DATESTAMP}_iter1"
_BENCH_PATH = "/mnt/fs1/mdtest-elbencho-target-1-20260812Z120000"
_MDTEST_CONFIGURATION = {
    "MDTEST_BRANCH_FACTOR": "7",
    "MDTEST_ITEMS_PER_DIR": "100",
    "MDTEST_ITERATIONS": "3",
}

_CSV_FIELDS = [
    "ISO date",
    "label",
    "path type",
    "paths",
    "hosts",
    "threads",
    "dirs",
    "files",
    "file size",
    "block size",
    "direct IO",
    "random",
    "random aligned",
    "IO depth",
    "shared paths",
    "truncate",
    "operation",
    "time ms [first]",
    "time ms [last]",
    "entries/s [first]",
    "entries/s [last]",
    "IOPS [first]",
    "IOPS [last]",
    "MiB/s [first]",
    "MiB/s [last]",
    "CPU% [first]",
    "CPU% [last]",
    "entries [first]",
    "entries [last]",
    "MiB [first]",
    "MiB [last]",
    "Ent lat us [min]",
    "Ent lat us [avg]",
    "Ent lat us [max]",
    "IO lat us [min]",
    "IO lat us [avg]",
    "IO lat us [max]",
    "version",
    "command",
]

# Per-phase values from a real dense run: entries/s, entry latency, and the
# command line, whose "-n 0" and "-N 1250" are what make the run dense.
_DENSE_PHASES = [
    {
        "operation": "WRITE",
        "flag": "-w",
        "elapsed_ms": 586,
        "rate": 16962,
        "lat": (108, 469, 14539),
    },
    {
        "operation": "STAT",
        "flag": "--stat",
        "elapsed_ms": 8,
        "rate": 1140625,
        "lat": (1, 6, 312),
    },
    {
        "operation": "RMFILES",
        "flag": "-F",
        "elapsed_ms": 477,
        "rate": 20877,
        "lat": (96, 382, 3612),
    },
]


def _phase_command(flag: str) -> str:
    return (
        f'"/usr/local/bin/elbencho" "--svcwait" "120" "{flag}" "-t" "8" '
        f'"-n" "0" "-N" "1250" "-s" "0" "-b" "0" "--lat" "--lathisto" '
        f'"--latpercent" "--nolive" "{_BENCH_PATH}"'
    )


def _dense_csv() -> str:
    """Build the elbencho CSV result file for the three dense phases."""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for phase in _DENSE_PHASES:
        lat_min, lat_avg, lat_max = phase["lat"]
        writer.writerow(
            {
                "ISO date": "2026-08-12T14:21:31.562-0400",
                "path type": "dir",
                "paths": 1,
                "hosts": 1,
                "threads": 8,
                "dirs": 0,
                "files": 1250,
                "file size": 0,
                "block size": 0,
                "IO depth": 1,
                "truncate": 0,
                "operation": phase["operation"],
                "time ms [first]": phase["elapsed_ms"],
                "time ms [last]": phase["elapsed_ms"] + 2,
                "entries/s [first]": phase["rate"],
                "entries/s [last]": phase["rate"] + 16,
                "entries [first]": 9949,
                "entries [last]": 10000,
                "Ent lat us [min]": lat_min,
                "Ent lat us [avg]": lat_avg,
                "Ent lat us [max]": lat_max,
                "version": "3.1-2",
                "command": _phase_command(phase["flag"]),
            }
        )
    return buffer.getvalue()


_DENSE_CSV = _dense_csv()

_DENSE_OUT = f"""ISO DATE start: 2026-08-12T14:21:31-0400
COMMAND LINE: "/usr/local/bin/elbencho" "-w" "-t" "8" "-n" "0" "-N" "1250" \
"{_BENCH_PATH}"

OPERATION   RESULT TYPE         FIRST DONE   LAST DONE
=========== ================    ==========   =========
WRITE       Elapsed time     :       586ms       588ms
            Files/s          :       16962       16978
            Files total      :        9949       10000
            Files latency    : [ min=108us avg=469us max=14.5ms ]
            Files lat % us   : [ 1%<=215 50%<=512 75%<=609 99%<=1024 ]
            Files lat hist   : [ 256: 500, 512: 4000, 1024: 5000, 16384: 500 ]
---

ISO DATE end  : 2026-08-12T14:21:32-0400
ISO DATE start: 2026-08-12T14:22:10-0400
COMMAND LINE: "/usr/local/bin/elbencho" "--stat" "-t" "8" "-n" "0" "-N" "1250" \
"{_BENCH_PATH}"

OPERATION   RESULT TYPE         FIRST DONE   LAST DONE
=========== ================    ==========   =========
STAT        Elapsed time     :         8ms         8ms
            Files/s          :     1140625     1142465
            Files total      :        9636       10000
            Files latency    : [ min=1us avg=6us max=312us ]
            Files lat % us   : [ 1%<=1.2 50%<=6.7 75%<=8.0 99%<=11 ]
            Files lat hist   : [ 5: 1000, 7: 8000, 11: 900, 362: 100 ]
---

ISO DATE end  : 2026-08-12T14:22:10-0400
ISO DATE start: 2026-08-12T14:22:10-0400
COMMAND LINE: "/usr/local/bin/elbencho" "-F" "-t" "8" "-n" "0" "-N" "1250" \
"{_BENCH_PATH}"

OPERATION   RESULT TYPE         FIRST DONE   LAST DONE
=========== ================    ==========   =========
RMFILES     Elapsed time     :       477ms       479ms
            Files/s          :       20877       20869
            Files total      :        9967       10000
            Files latency    : [ min=96us avg=382us max=3.61ms ]
            Files lat % us   : [ 1%<=152 50%<=362 75%<=512 99%<=1024 ]
            Files lat hist   : [ 152: 200, 362: 6000, 512: 3000, 4096: 800 ]
---

ISO DATE end  : 2026-08-12T14:22:10-0400
"""


def _document(*directories):
    """The results document a report writes for these result directories."""
    fragments = [
        (
            str(directory),
            _EXTRACT_MOD.build_results_fragment(
                str(directory), _EXTRACT_MOD.load_env_used_yaml(str(directory))
            ),
        )
        for directory in directories
    ]
    return _EXTRACT_MOD.results_json.assemble_inputs_document(
        fragments, None, {"options": {}}
    )


def _iterations(*directories):
    """The complete iterations a report renders from those directories."""
    document = _document(*directories)
    return _EXTRACT_MOD.document_iterations(document, document["groups"])


def _keys(iterations):
    return [
        (metrics.node_count, metrics.thread_count, metrics.datestamp, metrics.iteration)
        for metrics, _ in iterations
    ]


class TestDenseResultsParse(unittest.TestCase):
    """A dense result pair parses end to end with no extractor changes."""

    def setUp(self) -> None:
        # pylint: disable=consider-using-with
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        results = Path(self._tmp.name)
        (results / f"{_STEM}.csv").write_text(_DENSE_CSV, encoding="utf-8")
        (results / f"{_STEM}.out").write_text(_DENSE_OUT, encoding="utf-8")
        self.results = results

    def test_file_pair_is_discovered(self) -> None:
        self.assertEqual(_keys(_iterations(self.results)), [(1, 8, _DATESTAMP, 1)])

    def test_discovery_does_not_join_success_and_failed_runs(self) -> None:
        """A successful CSV cannot authorize an OUT file from a failed run."""
        success = self.results / "success"
        failed = self.results / "failed"
        for directory, status in ((success, "SUCCESS"), (failed, "FAILED")):
            executions = directory / "executions"
            executions.mkdir(parents=True)
            (executions / "0001.sh").write_text(
                "export ELBENCHO_EXECUTION_KIND=mdtest\n"
                "export nodes=1\nexport tasks_per_node=8\n",
                encoding="utf-8",
            )
            (executions / "0001.status").write_text(f"{status}\n", encoding="utf-8")
        (self.results / f"{_STEM}.csv").rename(success / f"{_STEM}.csv")
        (self.results / f"{_STEM}.out").rename(failed / f"{_STEM}.out")

        self.assertEqual(_iterations(success, failed), [])

    def test_reified_mdtest_ledger_filters_non_successful_coordinates(self) -> None:
        executions = self.results / "executions"
        executions.mkdir()
        for execution_id, nodes, tasks, status in (
            ("0001", 1, 8, "FAILED"),
            ("0002", 2, 8, "SUCCESS"),
            ("0003", 1, 16, "PENDING"),
        ):
            (executions / f"{execution_id}.sh").write_text(
                "export ELBENCHO_EXECUTION_KIND=mdtest\n"
                f"export nodes={nodes}\n"
                f"export tasks_per_node={tasks}\n",
                encoding="utf-8",
            )
            (executions / f"{execution_id}.status").write_text(
                f"{status}\n", encoding="utf-8"
            )
        (executions / "0004.sh").write_text(
            "export ELBENCHO_EXECUTION_KIND=io\nexport nodes=1\n",
            encoding="utf-8",
        )
        (executions / "0004.status").write_text("SUCCESS\n", encoding="utf-8")
        successful_stem = f"mdtest-elbencho-c_002-t_008_{_DATESTAMP}_iter1"
        (self.results / f"{successful_stem}.csv").write_text(
            _DENSE_CSV, encoding="utf-8"
        )
        (self.results / f"{successful_stem}.out").write_text(
            _DENSE_OUT, encoding="utf-8"
        )

        with tempfile.TemporaryDirectory() as alias_directory:
            aliases = Path(alias_directory)
            direct_link = aliases / "results"
            direct_link.symlink_to(self.results, target_is_directory=True)
            parent_link = aliases / "parent"
            parent_link.symlink_to(self.results.parent, target_is_directory=True)
            inputs = (self.results, direct_link, parent_link / self.results.name)
            for input_dir in inputs:
                with self.subTest(input_dir=input_dir):
                    self.assertEqual(
                        _keys(_iterations(input_dir)), [(2, 8, _DATESTAMP, 1)]
                    )

    def test_non_mdtest_ledger_excludes_unowned_metadata_results(self) -> None:
        executions = self.results / "executions"
        executions.mkdir()
        (executions / "0001.sh").write_text(
            "export ELBENCHO_EXECUTION_KIND=io\n", encoding="utf-8"
        )
        (executions / "0001.status").write_text("FAILED\n", encoding="utf-8")

        self.assertEqual(_iterations(self.results), [])

    def test_mdtest_configuration_is_loaded_from_env_used_yaml(self) -> None:
        env_used = self.results / "env_used.yaml"
        env_used.write_text(
            "".join(
                f"{key}: {value}\n" for key, value in _MDTEST_CONFIGURATION.items()
            ),
            encoding="utf-8",
        )

        configuration = _EXTRACT_MOD.document_configuration(
            _document(self.results)["groups"]
        )

        self.assertEqual(configuration, _MDTEST_CONFIGURATION)

    def _iteration_metrics(self):
        found = _iterations(self.results)
        return found[0][0] if found else None

    def _aggregated_metrics(self):
        aggregated = _EXTRACT_MOD.aggregate_metrics([self._iteration_metrics()])
        self.assertEqual(list(aggregated), [(1, 8)])
        return aggregated[(1, 8)]

    def test_iteration_metrics_carry_all_three_phase_rates(self) -> None:
        metrics = self._iteration_metrics()

        self.assertIsNotNone(metrics)
        self.assertAlmostEqual(metrics.create_rate, 16962.0)
        self.assertAlmostEqual(metrics.stat_rate, 1140625.0)
        self.assertAlmostEqual(metrics.delete_rate, 20877.0)

    def test_iteration_metrics_use_last_worker_elapsed_times(self) -> None:
        metrics = self._iteration_metrics()

        self.assertAlmostEqual(metrics.create_elapsed_sec, 0.588)
        self.assertAlmostEqual(metrics.stat_elapsed_sec, 0.010)
        self.assertAlmostEqual(metrics.delete_elapsed_sec, 0.479)

    def test_in_flight_iteration_is_skipped(self) -> None:
        csv_path = self.results / f"{_STEM}.csv"
        incomplete_lines = [
            line for line in _DENSE_CSV.splitlines() if ",RMFILES," not in line
        ]
        csv_path.write_text("\n".join(incomplete_lines) + "\n", encoding="utf-8")
        self.assertIsNone(self._iteration_metrics())
        iteration = _document(self.results)["executions"][0]["results"]["iterations"][0]
        self.assertEqual(iteration["state"], "incomplete")
        self.assertIn(
            "missing CSV operations: RMFILES", iteration["problems"][0]["detail"]
        )

    def test_dense_flags_survive_into_representative_commands(self) -> None:
        metrics = self._iteration_metrics()

        for command in (
            metrics.create_command,
            metrics.stat_command,
            metrics.delete_command,
        ):
            stripped = _EXTRACT_MOD.strip_command(command)
            self.assertIn("-n 0", stripped)
            self.assertIn("-N 1250", stripped)
            self.assertNotIn(" -d ", f" {stripped} ")
            self.assertNotIn(" -D ", f" {stripped} ")

    def test_histograms_are_read_for_every_phase(self) -> None:
        metrics = self._iteration_metrics()

        self.assertEqual(sum(metrics.create_histogram.values()), 10000)
        self.assertEqual(sum(metrics.stat_histogram.values()), 10000)
        self.assertEqual(sum(metrics.delete_histogram.values()), 10000)

    def test_decimal_latency_buckets_are_reported(self) -> None:
        out_path = self.results / f"{_STEM}.out"
        out_path.write_text(
            _DENSE_OUT.replace(
                "Files lat hist   : [ 5: 1000, 7: 8000, 11: 900, 362: 100 ]",
                "Files lat hist   : [ 2.4: 5000, 4.8: 5000 ]",
            ),
            encoding="utf-8",
        )

        metrics = self._iteration_metrics()
        aggregated = self._aggregated_metrics()

        self.assertEqual(metrics.stat_histogram, {2.4: 5000, 4.8: 5000})
        self.assertAlmostEqual(aggregated.stat_lat_p50, 0.0024)
        self.assertAlmostEqual(aggregated.stat_lat_p100, 0.0048)

    def test_repeated_source_buckets_preserve_all_counts(self) -> None:
        histogram = _EXTRACT_MOD.parse_latency_histogram("2.4: 5, 2.40: 7, 3: 2")

        self.assertEqual(histogram, {2.4: 12, 3.0: 2})

    def test_aggregation_produces_percentiles(self) -> None:
        result = self._aggregated_metrics()

        self.assertEqual(result.node_count, 1)
        self.assertEqual(result.thread_count, 8)
        self.assertEqual(result.iteration_count, 1)
        self.assertAlmostEqual(result.create_rate_avg, 16962.0)

        # Bucket boundaries are microseconds; percentiles are milliseconds.
        # Create histogram: 500 @256us, 4000 @512us, 5000 @1024us, 500 @16384us.
        self.assertAlmostEqual(result.create_lat_p50, 1.024, places=3)
        self.assertAlmostEqual(result.create_lat_p100, 16.384, places=3)
        self.assertAlmostEqual(result.stat_lat_p50, 0.007, places=3)
        self.assertAlmostEqual(result.delete_lat_p50, 0.362, places=3)

    def test_aggregation_averages_phase_elapsed_times(self) -> None:
        first = self._iteration_metrics()
        second = replace(
            first,
            iteration=2,
            create_elapsed_sec=1.412,
            stat_elapsed_sec=0.190,
            delete_elapsed_sec=0.521,
        )

        result = _EXTRACT_MOD.aggregate_metrics([first, second])[(1, 8)]

        self.assertAlmostEqual(result.create_elapsed_avg_sec, 1.0)
        self.assertAlmostEqual(result.stat_elapsed_avg_sec, 0.1)
        self.assertAlmostEqual(result.delete_elapsed_avg_sec, 0.5)

    def test_percentiles_increase_monotonically(self) -> None:
        result = self._aggregated_metrics()

        for prefix in ("create", "stat", "delete"):
            percentiles = [
                getattr(result, f"{prefix}_lat_p{tag}")
                for tag in ("0", "50", "90", "99", "100")
            ]
            self.assertEqual(percentiles, sorted(percentiles), prefix)

    def test_terminal_report_renders_phase_elapsed_times(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _EXTRACT_MOD.print_terminal_table(
                [self._aggregated_metrics()],
                test_configuration=_MDTEST_CONFIGURATION,
            )

        report = buffer.getvalue()
        self.assertIn("-n 0", report)
        self.assertIn("16,962", report)
        self.assertIn("=== Average Phase Elapsed Times ===", report)
        self.assertIn("last worker completion", report)
        self.assertIn("588ms", report)
        self.assertIn("10ms", report)
        self.assertIn("479ms", report)
        self.assertIn("=== MDTest Configuration (env_used.yaml) ===", report)
        for key, value in _MDTEST_CONFIGURATION.items():
            self.assertRegex(report, rf"{key}\s+{value}")


_OTHER_DATESTAMP = "20260813Z120000"
_TARGET_LINE = "Storage target: "
_ALLOW_MIXED = "--allow-mixed-targets"


class TestStorageTargetGuard(unittest.TestCase):
    """Results from different storage targets are refused unless allowed."""

    def setUp(self) -> None:
        # pylint: disable=consider-using-with
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.first = self._results("a", _DATESTAMP, "lustre-a")
        self.second = self._results("b", _OTHER_DATESTAMP, "weka-b")

    def _results(self, name, datestamp, target):
        directory = self.root / name
        directory.mkdir()
        stem = _STEM.replace(_DATESTAMP, datestamp)
        (directory / f"{stem}.csv").write_text(_DENSE_CSV, encoding="utf-8")
        (directory / f"{stem}.out").write_text(_DENSE_OUT, encoding="utf-8")
        if target is not None:
            (directory / "env_used.yaml").write_text(
                f"STORAGE_TARGET_NAME: {target}\n", encoding="utf-8"
            )
        return directory

    def _run(self, *arguments):
        """Run main; return (exit code, stdout, stderr, plot calls)."""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with (
            mock.patch.object(
                sys, "argv", ["extract-mdtest-elbencho.py", *map(str, arguments)]
            ),
            mock.patch.object(_EXTRACT_MOD, "plot_all") as plot,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                _EXTRACT_MOD.main()
            except SystemExit as exit_request:
                code = exit_request.code
        return code, out.getvalue(), err.getvalue(), plot.call_count

    def _target_lines(self, output):
        return [line for line in output.splitlines() if _TARGET_LINE in line]

    def test_mixed_targets_exit_before_anything_is_written(self) -> None:
        output = self.root / "out"
        code, _, err, plots = self._run(
            self.first, self.second, f"--output-dir={output}"
        )
        self.assertEqual(code, 1)
        for needle in ('"lustre-a": ', str(self.first), '"weka-b": ', str(self.second)):
            self.assertIn(needle, err)
        self.assertIn(_ALLOW_MIXED, err)
        self.assertFalse(output.exists())
        self.assertEqual(plots, 0)
        self.assertEqual(list(self.root.rglob("report.txt")), [])

    def test_allowed_mixed_targets_warn_and_label(self) -> None:
        output = self.root / "out"
        code, out, err, plots = self._run(
            self.first,
            self.second,
            _ALLOW_MIXED,
            f"--output-dir={output}",
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            self._target_lines(out), ['Storage target: mixed: "lustre-a", "weka-b"']
        )
        self.assertIn("WARNING: report mixes storage targets", err)
        self.assertEqual(plots, 1)

    def test_one_target_is_named_on_stdout_and_in_report_txt(self) -> None:
        third = self._results("c", "20260814Z120000", "lustre-a")
        output = self.root / "out"
        code, out, _, _ = self._run(self.first, third, f"--output-dir={output}")
        self.assertEqual(code, 0)
        self.assertEqual(self._target_lines(out), ["Storage target: lustre-a"])
        report = (output / "report.txt").read_text(encoding="utf-8")
        self.assertIn("Storage target: lustre-a\n", report)

    def test_known_and_unverifiable_inputs_warn_without_failing(self) -> None:
        third = self._results("c", "20260814Z120000", None)
        code, out, err, _ = self._run(
            self.first, third, f"--output-dir={self.root / 'out'}"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self._target_lines(out), ["Storage target: lustre-a"])
        self.assertIn(f"cannot verify storage target of 1 input(s): {third}", err)

    def test_unknown_targets_are_silent(self) -> None:
        unnamed = self._results("c", "20260814Z120000", None)
        code, out, err, _ = self._run(unnamed)
        self.assertEqual(code, 0)
        self.assertEqual(self._target_lines(out), ["Storage target: unknown"])
        self.assertNotIn("WARNING", err)

    def test_same_coordinates_in_two_targets_are_kept_when_mixing_is_allowed(
        self,
    ) -> None:
        clash = self._results("c", _DATESTAMP, "weka-b")
        code, _, err, _ = self._run(
            self.first, clash, _ALLOW_MIXED, f"--output-dir={self.root / 'out'}"
        )
        self.assertEqual(code, 0)
        self.assertNotIn("Duplicate result pair", err)
        self.assertIn("Found 2 complete iterations", err)

    def test_same_coordinates_inside_one_target_are_still_ambiguous(self) -> None:
        for target, extra in (
            ("lustre-a", ()),
            (None, ()),
            ("lustre-a", (_ALLOW_MIXED,)),
        ):
            with self.subTest(target=target, extra=extra):
                clash = self._results(f"d{target}{extra}", _DATESTAMP, target)
                code, _, err, _ = self._run(
                    self.first, clash, *extra, f"--output-dir={self.root / 'out'}"
                )
                # No pair is usable: the run fails, but the document remains.
                self.assertEqual(code, 1)
                self.assertIn("No text report produced", err)
                self.assertIn("Duplicate result pair", err)
                self.assertIn("skipping both", err)
                shutil.rmtree(clash)

    def test_same_coordinates_are_kept_per_target_only_by_target(self) -> None:
        clash = self._results("c", _DATESTAMP, "weka-b")
        document = _document(self.first, clash)
        by_target = _EXTRACT_MOD.document_iterations(
            document, document["groups"], by_target=True
        )
        self.assertEqual(_keys(by_target), [(1, 8, _DATESTAMP, 1)] * 2)
        with contextlib.redirect_stderr(io.StringIO()):
            shared = _EXTRACT_MOD.document_iterations(document, document["groups"])
        self.assertEqual(shared, [])
        self.assertEqual(len(_iterations(self.first)), 1)


if __name__ == "__main__":
    unittest.main()
