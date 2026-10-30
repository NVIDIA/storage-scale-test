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

"""Text tables, PNGs and live outputs render from the results document.

Each fixture is reported from its raw result files, which writes the
document, and again with ``--from-json`` from that document: the text, the
PNG names and the client summaries must match. The reporters run as
subprocesses, with real plots. All values are invented.
"""

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from lib.elbencho_live_report import LIVE_CSV_COLUMNS
from lib.filesystem_report_options import LIVE_DETAIL_LEVELS
from tests.test_extract_elbencho_results_json import _DS, _ENV_USED, _write_result
from tests.test_extract_elbencho_resume import _write_execution_metadata
from tests.test_extract_mdtest_elbencho import _DENSE_CSV, _DENSE_OUT
from tests.test_extract_oldest_result_layouts import (
    write_prebatch_io_dir,
    write_prebatch_mdtest_dir,
)
from tests.test_filesystem_results_end_to_end import _build_batch, _publish_results

REPO = Path(__file__).resolve().parent.parent
IO = REPO / "utils" / "extract-elbencho.py"
MDTEST = REPO / "utils" / "extract-mdtest-elbencho.py"
FILESYSTEM = REPO / "utils" / "extract-filesystem.py"
FROM_JSON = "--from-json"
OUTPUT_DIR = "--output-dir"
RESULTS = "results.json"
REPORT = "report.txt"
REPORTED_BY = "Reported by"
LIVE_DETAIL = "--live-detail"
SHORTFALL = "--client-shortfall-percent"
MIB = 1048576


def _run(script, *arguments, check=True):
    done = subprocess.run(
        [sys.executable, str(script), *map(str, arguments)],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if check:
        assert done.returncode == 0, done.stderr
    return done


def _texts(root, replace=()):
    """Every text output under ``root`` by relative path, paths normalized."""
    texts = {}
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".txt", ".csv") or "results.live" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for old, new in replace:
            text = text.replace(old, new)
        texts[str(path.relative_to(root))] = text
    return texts


def _pngs(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*.png"))


def _outputs(root):
    """Every output file name but the document, its sidecars, report.html
    and a batch's hidden bookkeeping."""
    return sorted(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
        and path.name not in (RESULTS, "report.html")
        and not path.name.startswith(".")
        and "results.live" not in path.parts
    )


def _render_both(tmp_path, script, inputs, *extra, replay=None):
    """Report raw inputs, then the document they wrote; return both runs.

    The replay takes ``replay`` instead of ``extra`` when given.
    """
    raw_dir, rendered_dir = tmp_path / "raw", tmp_path / "rendered"
    raw = _run(script, *inputs, OUTPUT_DIR, raw_dir, *extra)
    rendered = _run(
        script,
        FROM_JSON,
        raw_dir / RESULTS,
        OUTPUT_DIR,
        rendered_dir,
        *(extra if replay is None else replay),
    )
    return (raw, raw_dir), (rendered, rendered_dir)


def _assert_same_report(raw, rendered):
    (raw_run, raw_dir), (rendered_run, rendered_dir) = raw, rendered
    assert rendered_run.stdout == raw_run.stdout
    replace = ((str(raw_dir), "OUT"), (str(rendered_dir), "OUT"))
    assert _texts(rendered_dir, replace) == _texts(raw_dir, replace)
    assert _pngs(rendered_dir) == _pngs(raw_dir)
    assert (rendered_dir / "report.html").is_file()
    assert not (rendered_dir / RESULTS).exists()
    assert _outputs(rendered_dir) == _outputs(raw_dir)


def _live_row(phase, step, rank, values):
    row = dict.fromkeys(LIVE_CSV_COLUMNS, "")
    row.update(
        {
            "ISO Date": f"2026-10-26T12:{step // 60:02d}:{step % 60:02d}.000+00:00",
            "Phase": phase,
            "RuntimeMS": str(step * 1000),
            "Rank": str(rank),
            "Service": "" if rank == "Total" else f"host{rank:03d}",
        }
    )
    row.update(values)
    return row


def _live_phase(phase, seconds, rates):
    """Rows of one phase: a Total row and one row per client each second."""
    rows = []
    done = [0] * len(rates)
    for step in range(1, seconds + 1):
        for client, rate in enumerate(rates):
            done[client] += int(rate * MIB * (0.9 + 0.2 * ((step + client) % 3) / 2))
        total = sum(done)
        rows.append(
            _live_row(
                phase,
                step,
                "Total",
                {
                    "DoneBytes": str(total),
                    "MiB/s": str(sum(rates)),
                    "IOPS": str(sum(rates)),
                    "Lat IO us": str(400 + 10 * (step % 4)),
                },
            )
        )
        rows.extend(
            _live_row(phase, step, client, {"DoneBytes": str(value)})
            for client, value in enumerate(done)
        )
    return rows


def _long_live_csv(path, phases):
    """A live CSV with a steady window: (phase, seconds) in order."""
    rates = [100, 100, 100, 70]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, LIVE_CSV_COLUMNS)
        writer.writeheader()
        for phase, seconds in phases:
            writer.writerows(
                _live_phase(phase, seconds, [0] * 4 if phase == "SYNC" else rates)
            )


@pytest.fixture(name="single_run")
def single_run_fixture(tmp_path):
    """One execution with workload metadata, live data and an unpaired CSV."""
    result = tmp_path / f"elbencho-{_DS}"
    result.mkdir()
    (result / "env_used.yaml").write_text(_ENV_USED, encoding="utf-8")
    _write_execution_metadata(result, "0001", nodes=1, io_size="1M", threads=4)
    base = _write_result(result, nodes=1, io_size="1M", threads=4)
    _long_live_csv(f"{base}.live.csv", [("WRITE", 40), ("SYNC", 3), ("READ", 40)])
    unpaired = result / f"elbencho-1M-c_001-s_008-d_001_{_DS}.live.csv"
    _long_live_csv(unpaired, [("WRITE", 20)])
    return result


def _identical_runs(tmp_path):
    """Two directories holding identically named runs with live data."""
    runs = []
    for name in ("a", "b"):
        result = tmp_path / name / f"elbencho-{_DS}"
        result.mkdir(parents=True)
        (result / "env_used.yaml").write_text(_ENV_USED, encoding="utf-8")
        _write_execution_metadata(result, "0001", nodes=1, io_size="1M", threads=4)
        base = _write_result(result, nodes=1, io_size="1M", threads=4)
        _long_live_csv(f"{base}.live.csv", [("WRITE", 40), ("SYNC", 3), ("READ", 40)])
        runs.append(result)
    return runs


def test_identically_named_runs_keep_their_live_exports_apart(tmp_path):
    runs = _identical_runs(tmp_path)
    unpaired = f"elbencho-1M-c_001-s_008-d_001_{_DS}"
    for result in runs:
        _long_live_csv(result / f"{unpaired}.live.csv", [("WRITE", 20)])
    raw, rendered = _render_both(tmp_path, IO, runs)
    _assert_same_report(raw, rendered)
    stem = f"elbencho-1M-c_001-s_004-d_001_{_DS}"
    names = {path.name for path in raw[1].glob(f"*{stem}-*-client-summary.csv")}
    assert names == {
        f"{group}.{stem}-{phase}-client-summary.csv"
        for group in ("0001", "0002")
        for phase in ("write", "sync", "read")
    }
    assert len(list(raw[1].glob(f"*{stem}-*-client-summary.txt"))) == 6
    for group in ("0001", "0002"):
        assert (raw[1] / f"{group}.{stem}-write-client-ratio-heatmap.png").is_file()
    # An unpaired live CSV has no table row: its line names the group.
    report = (raw[1] / REPORT).read_text(encoding="utf-8")
    assert f"0001.{unpaired} WRITE: steady" in report
    assert f"0002.{unpaired} WRITE: steady" in report
    # One group of the document keeps its names.
    alone = tmp_path / "alone"
    _run(IO, FROM_JSON, raw[1] / RESULTS, OUTPUT_DIR, alone, "--groups", "0002")
    assert (alone / f"0002.{stem}-write-client-summary.csv").is_file()
    assert not list(alone.glob(f"0001.{stem}-*"))


def test_several_directories_keep_live_rows_under_their_tables(tmp_path):
    """A live CSV's domains share one source path, prefixed once per group."""
    runs = _identical_runs(tmp_path)
    output = tmp_path / "out"
    _run(IO, *runs, OUTPUT_DIR, output)
    document = json.loads((output / RESULTS).read_text(encoding="utf-8"))
    for group, execution in zip(document["groups"], document["executions"]):
        results = execution["results"]
        lives = [p["live"] for p in results["phases"] if p.get("live")]
        lives += results["additional_live_domains"]
        name = f"elbencho-1M-c_001-s_004-d_001_{_DS}.live.csv"
        expected = f"{group['output_relative']}/{name}"
        assert {live["source"]["path"] for live in lives} == {expected}
    report = (output / REPORT).read_text(encoding="utf-8")
    assert report.count("Live fairness (steady window only") == 2
    # Only the SYNC domains, which have no table row, get lines.
    assert " WRITE: steady" not in report and " READ: steady" not in report


def test_single_run_renders_the_same_from_its_document(tmp_path, single_run):
    raw, rendered = _render_both(tmp_path, IO, [single_run])
    _assert_same_report(raw, rendered)
    report = (raw[1] / REPORT).read_text(encoding="utf-8")
    assert "Workload: layout=shared-directory" in report
    assert "Live fairness (steady window only" in report
    # The unpaired live CSV and the SYNC domain get lines below the table.
    assert f"elbencho-1M-c_001-s_008-d_001_{_DS} WRITE: steady" in report
    names = _pngs(raw[1])
    assert any(name.endswith("-write-client-ratio-heatmap.png") for name in names)
    assert any(name.endswith("-sync-aggregate-timeseries.png") for name in names)
    summary = f"elbencho-1M-c_001-s_004-d_001_{_DS}-read-client-summary.csv"
    assert (raw[1] / summary).is_file()


def test_sweep_and_render_time_filters(tmp_path):
    result = tmp_path / f"elbencho-{_DS}"
    result.mkdir()
    for nodes in (1, 2, 4):
        _write_result(result, nodes=nodes, threads=4)
        _write_result(
            result, nodes=nodes, io_size="1M,r4K", threads=8, rand_phases=("READ",)
        )
    _write_result(result, nodes=2, threads=16, direct=False, phases=("WRITE",))
    raw, rendered = _render_both(tmp_path, IO, [result])
    _assert_same_report(raw, rendered)
    assert "=== IO Size: 1M,r4K - File Size: 1G - READ - Rand" in raw[0].stdout

    filtered = tmp_path / "filtered"
    done = _run(
        IO,
        FROM_JSON,
        raw[1] / RESULTS,
        OUTPUT_DIR,
        filtered,
        "--only-nodes",
        "2",
        "--only-sizes",
        "1M,r4K",
    )
    rows = [line for line in done.stdout.splitlines() if line[:1].isdigit()]
    assert rows and all(line.split("|")[0].strip() == "2" for line in rows)
    assert "IO Size: 1M -" not in done.stdout


def _mdtest_result(tmp_path):
    result = tmp_path / f"mdtest-elbencho-{_DS}"
    result.mkdir()
    (result / "env_used.yaml").write_text(
        "MDTEST_BRANCH_FACTOR: 7\nMDTEST_ITEMS_PER_DIR: 100\nMDTEST_ITERATIONS: 2\n",
        encoding="utf-8",
    )
    for nodes in (1, 2):
        for iteration in (1, 2):
            stem = f"mdtest-elbencho-c_{nodes:03d}-t_008_{_DS}_iter{iteration}"
            (result / f"{stem}.csv").write_text(_DENSE_CSV, encoding="utf-8")
            (result / f"{stem}.out").write_text(_DENSE_OUT, encoding="utf-8")
    return result


def test_mdtest_renders_the_same_from_its_document(tmp_path):
    result = _mdtest_result(tmp_path)
    raw, rendered = _render_both(tmp_path, MDTEST, [result])
    _assert_same_report(raw, rendered)
    assert "=== Performance Rates (ops/sec) ===" in raw[0].stdout
    assert "MDTEST_BRANCH_FACTOR  7" in raw[0].stdout


@pytest.mark.parametrize("kind", ["io", "mdtest"])
def test_oldest_layouts_render_the_same_from_their_document(tmp_path, kind):
    source = tmp_path / "source"
    source.mkdir()
    if kind == "io":
        result, script = write_prebatch_io_dir(source), IO
    else:
        result, script = write_prebatch_mdtest_dir(source), MDTEST
    raw, rendered = _render_both(tmp_path, script, [result])
    _assert_same_report(raw, rendered)
    assert REPORTED_BY in raw[0].stdout


def test_batch_renders_the_same_from_its_document(tmp_path):
    batch = tmp_path / "batch"
    _publish_results(_build_batch(batch))
    raw, rendered = _render_both(tmp_path, FILESYSTEM, [batch])
    _assert_same_report(raw, rendered)
    for group in ("0001", "0002", "0003"):
        assert (rendered[1] / "groups" / group / REPORT).is_file()
    assert (rendered[1] / "reports.txt").is_file()


@pytest.mark.parametrize("level", LIVE_DETAIL_LEVELS)
def test_replay_at_a_lower_live_detail_matches_a_raw_run(tmp_path, single_run, level):
    full = tmp_path / "full"
    _run(IO, single_run, OUTPUT_DIR, full)
    raw = tmp_path / "raw"
    raw_run = _run(IO, single_run, OUTPUT_DIR, raw, LIVE_DETAIL, level)
    rendered = tmp_path / "rendered"
    rendered_run = _run(
        IO, FROM_JSON, full / RESULTS, OUTPUT_DIR, rendered, LIVE_DETAIL, level
    )
    _assert_same_report((raw_run, raw), (rendered_run, rendered))


def test_replay_keeps_the_recorded_options(tmp_path, single_run):
    options = (SHORTFALL, "40", "--no-dual-y-axis")
    raw, rendered = _render_both(tmp_path, IO, [single_run], *options, replay=())
    _assert_same_report(raw, rendered)
    default = tmp_path / "default"
    _run(IO, single_run, OUTPUT_DIR, default)
    # Both options show in the text and the PNG names.
    assert _texts(raw[1]) != _texts(default)
    assert _pngs(raw[1]) != _pngs(default)
    # Restating a recorded option is fine; changing a fixed one is not.
    again = (SHORTFALL, "40.0", "--client-max-heatmap-rows", "2")
    _run(IO, FROM_JSON, raw[1] / RESULTS, OUTPUT_DIR, tmp_path / "again", *again)
    done = _run(
        IO,
        FROM_JSON,
        raw[1] / RESULTS,
        OUTPUT_DIR,
        tmp_path / "changed",
        SHORTFALL,
        "5",
        check=False,
    )
    assert done.returncode == 2
    assert f"{SHORTFALL} 5.0: the results document records 40.0" in done.stderr
    assert not (tmp_path / "changed").exists()


def test_mdtest_replay_keeps_normalize_to(tmp_path):
    result = _mdtest_result(tmp_path)
    raw, rendered = _render_both(
        tmp_path, MDTEST, [result], "--normalize-to", "4", replay=()
    )
    _assert_same_report(raw, rendered)
    assert "normalized to 4 nodes" in rendered[0].stdout


def test_batch_replay_keeps_recorded_options_and_caps_live_detail(tmp_path):
    batch = tmp_path / "batch"
    _publish_results(_build_batch(batch))
    options = (SHORTFALL, "40", "--no-dual-y-axis", "--normalize-to", "4")
    raw, rendered = _render_both(
        tmp_path / "options", FILESYSTEM, [batch], *options, replay=()
    )
    _assert_same_report(raw, rendered)
    done = _run(
        FILESYSTEM,
        FROM_JSON,
        raw[1] / RESULTS,
        OUTPUT_DIR,
        tmp_path / "changed",
        SHORTFALL,
        "5",
        check=False,
    )
    assert done.returncode == 2
    assert "the results document records 40.0" in done.stderr
    below = tmp_path / "none"
    below_run = _run(
        FILESYSTEM, batch, OUTPUT_DIR, below, *options, LIVE_DETAIL, "none"
    )
    capped = tmp_path / "capped"
    capped_run = _run(
        FILESYSTEM, FROM_JSON, raw[1] / RESULTS, OUTPUT_DIR, capped, LIVE_DETAIL, "none"
    )
    _assert_same_report((below_run, below), (capped_run, capped))


def test_below_full_live_detail_keeps_the_fairness_table(tmp_path, single_run):
    output = tmp_path / "summary"
    done = _run(IO, single_run, OUTPUT_DIR, output, "--live-detail", "summary")
    assert "live client plots and summaries are omitted" in done.stderr
    assert "Live fairness (steady window only" in done.stdout
    assert not list(output.glob("*client-summary*"))
    names = _pngs(output)
    assert any(name.endswith("-write-aggregate-timeseries.png") for name in names)
    assert not any("heatmap" in name for name in names)
    full = tmp_path / "full"
    _run(IO, single_run, OUTPUT_DIR, full)
    table = [line for line in done.stdout.splitlines() if " of " in line]
    assert table == [
        line
        for line in (full / REPORT).read_text(encoding="utf-8").splitlines()
        if " of " in line
    ]


def _document_dir(tmp_path, single_run):
    output = tmp_path / "document"
    _run(IO, single_run, OUTPUT_DIR, output, "--live-detail", "none")
    return output


@pytest.mark.parametrize(
    "arguments, message",
    [
        ([], "--output-dir is required with --from-json"),
        ([OUTPUT_DIR, "{document}"], "is a results document's directory"),
        (["{input}", OUTPUT_DIR, "{out}"], "--from-json replaces input directories"),
        (["--groups", "0009", OUTPUT_DIR, "{out}"], "unknown groups"),
    ],
)
def test_from_json_usage_errors(tmp_path, single_run, arguments, message):
    document = _document_dir(tmp_path, single_run)
    values = {"document": document, "input": single_run, "out": tmp_path / "out"}
    done = _run(
        IO,
        FROM_JSON,
        document / RESULTS,
        *(argument.format(**values) for argument in arguments),
        check=False,
    )
    assert done.returncode == 2
    assert message in done.stderr


def test_from_json_accepts_a_subdirectory_and_refuses_another_major(
    tmp_path, single_run
):
    document = _document_dir(tmp_path, single_run)
    _run(IO, FROM_JSON, document / RESULTS, OUTPUT_DIR, document / "again")
    assert (document / "again" / REPORT).is_file()
    data = json.loads((document / RESULTS).read_text(encoding="utf-8"))
    data["schema_version"] = "2.0"
    newer = tmp_path / "newer.json"
    newer.write_text(json.dumps(data), encoding="utf-8")
    done = _run(IO, FROM_JSON, newer, OUTPUT_DIR, tmp_path / "out", check=False)
    assert done.returncode == 2
    assert "schema_version '2.0' is not supported" in done.stderr
