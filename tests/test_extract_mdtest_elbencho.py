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
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path

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
        pairs = _EXTRACT_MOD.discover_files([str(self.results)])
        self.assertEqual(list(pairs), [(1, 8, _DATESTAMP, 1)])

    def test_mdtest_configuration_is_loaded_from_env_used_yaml(self) -> None:
        env_used = self.results / "env_used.yaml"
        env_used.write_text(
            "".join(
                f"{key}: {value}\n" for key, value in _MDTEST_CONFIGURATION.items()
            ),
            encoding="utf-8",
        )

        configuration = _EXTRACT_MOD.load_mdtest_configuration([str(self.results)])

        self.assertEqual(configuration, _MDTEST_CONFIGURATION)

    def _iteration_metrics(self):
        pairs = _EXTRACT_MOD.discover_files([str(self.results)])
        csv_path, out_path = next(iter(pairs.values()))
        return _EXTRACT_MOD.parse_file_pair(csv_path, out_path, 1, 8, _DATESTAMP, 1)

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
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            metrics = self._iteration_metrics()

        self.assertIsNone(metrics)
        warning = stderr.getvalue()
        self.assertIn("Skipping incomplete or in-flight result", warning)
        self.assertIn("missing CSV operations: RMFILES", warning)

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

    def test_markdown_report_renders_dense_run(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _EXTRACT_MOD.print_markdown_report(
                [self._aggregated_metrics()],
                test_configuration=_MDTEST_CONFIGURATION,
            )

        report = buffer.getvalue()
        self.assertIn("MDTest-Elbencho Benchmark Results", report)
        self.assertIn("-n 0", report)
        self.assertIn("16,962", report)
        for key, value in _MDTEST_CONFIGURATION.items():
            self.assertIn(f"| `{key}` | {value} |", report)
        self.assertIn("### 3.2 Average Phase Elapsed Times", report)
        self.assertIn("| 1 | 8 | 1 | 588ms | 10ms | 479ms |", report)

    def test_terminal_report_renders_phase_elapsed_times(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            _EXTRACT_MOD.print_terminal_table(
                [self._aggregated_metrics()],
                test_configuration=_MDTEST_CONFIGURATION,
            )

        report = buffer.getvalue()
        self.assertIn("=== Average Phase Elapsed Times ===", report)
        self.assertIn("last worker completion", report)
        self.assertIn("588ms", report)
        self.assertIn("10ms", report)
        self.assertIn("479ms", report)
        self.assertIn("=== MDTest Configuration (env_used.yaml) ===", report)
        for key, value in _MDTEST_CONFIGURATION.items():
            self.assertRegex(report, rf"{key}\s+{value}")

    def test_csv_export_preserves_metrics_and_configuration(self) -> None:
        csv_path = self.results / "aggregated.csv"
        combined_configuration = {
            "MDTEST_BRANCH_FACTOR": "7, 9",
            "MDTEST_ITEMS_PER_DIR": "100, 200",
            "MDTEST_ITERATIONS": "3, 5",
        }
        _EXTRACT_MOD.write_csv_export(
            csv_path,
            [self._aggregated_metrics()],
            combined_configuration,
        )

        imported, imported_configuration = _EXTRACT_MOD.read_csv_import(csv_path)

        self.assertEqual(len(imported), 1)
        self.assertAlmostEqual(imported[0].create_elapsed_avg_sec, 0.588)
        self.assertAlmostEqual(imported[0].stat_elapsed_avg_sec, 0.010)
        self.assertAlmostEqual(imported[0].delete_elapsed_avg_sec, 0.479)
        self.assertEqual(imported_configuration, combined_configuration)


if __name__ == "__main__":
    unittest.main()
