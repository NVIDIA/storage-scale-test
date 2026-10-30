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

"""The reporting pipeline around the results document.

Schema 1.0 documents, report.html selection, batch documents through the
engines, input dedupe, exit statuses and file modes. The reporters run as
subprocesses; all values are invented.
"""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from lib import filesystem_batch, filesystem_results
from lib.filesystem_results import ReportSelection
from tests.test_extract_elbencho_results_json import _DS, _ENV_USED, _write_result
from tests.test_extract_mdtest_elbencho import _EXTRACT_MOD as _MDTEST
from tests.test_extract_oldest_result_layouts import (
    write_prebatch_io_dir,
    write_prebatch_mdtest_dir,
)
from tests.test_filesystem_results_end_to_end import _build_batch, _publish_results
from tests.test_report_from_document import (  # noqa: F401 pylint: disable=unused-import
    single_run_fixture,
)

REPO = Path(__file__).resolve().parent.parent
IO = REPO / "utils" / "extract-elbencho.py"
MDTEST = REPO / "utils" / "extract-mdtest-elbencho.py"
FILESYSTEM = REPO / "utils" / "extract-filesystem.py"
FROM_JSON = "--from-json"
OUTPUT_DIR = "--output-dir"
ONLY_NODES = "--only-nodes"
REQUIRE_COMPLETE = "--require-complete"
RESULTS = "results.json"
REPORT = "report.txt"
HTML = "report.html"
NO_TEXT = "No text report produced"
CHART_DATA = re.compile(r'<script type="application/json"[^>]*>(.*?)</script>', re.S)


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


def _document(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _chart_data(output):
    """The page's chart data: everything but provenance text."""
    return CHART_DATA.search((output / HTML).read_text(encoding="utf-8")).group(1)


# -- schema 1.0 ----------------------------------------------------------------

# Keys that schema 1.1 added, by the object that holds them.
_V11_KEYS = {
    "execution": ("producer_version",),
    "results": ("datestamp", "additional_live_domains"),
    "phase": ("file_size_bytes",),
    "dataset": ("count_source", "files_per_node", "reader", "first_file_bytes"),
    "workload": ("write_elapsed_ms", "read_elapsed_ms", "write_delete_elapsed_ms"),
    "live": ("phase_context",),
    "steady": ("interval_ms",),
    "aggregate_series": ("start_time", "clock_skew_ms", "entries_latency_avg_us"),
    "group": ("settings_text", "source", "unpaired_live"),
    "iteration": ("producer_version",),
    "options": (
        "client_max_heatmap_rows",
        "client_max_timeseries_lines",
        "no_dual_y_axis",
        "normalize_to",
    ),
}


def _drop(item, kind):
    for key in _V11_KEYS[kind]:
        if isinstance(item, dict):
            item.pop(key, None)


def _strip_live(live):
    if isinstance(live, dict):
        _drop(live, "live")
        _drop(live.get("steady"), "steady")
        _drop(live.get("aggregate_series"), "aggregate_series")


def _strip_results(results):
    _drop(results, "results")
    _drop(results.get("workload"), "workload")
    for phase in results.get("phases") or []:
        _drop(phase, "phase")
        _strip_live(phase.get("live"))
    for iteration in results.get("iterations") or []:
        _drop(iteration, "iteration")
        for phase in iteration["phases"]:
            phase.pop("command", None)


def _as_schema_1_0(source, destination, **settings):
    """Rewrite a document as the released schema 1.0 writer would have;
    ``settings`` are added to each group's saved settings."""
    document = _document(source)
    document["schema_version"] = "1.0"
    _drop(document["generator"]["options"], "options")
    for group in document["groups"]:
        _drop(group, "group")
        group["settings"].update(settings)
    for execution in document["executions"]:
        _drop(execution, "execution")
        _drop(execution.get("dataset"), "dataset")
        if execution.get("results"):
            _strip_results(execution["results"])
    destination.write_text(json.dumps(document), encoding="utf-8")
    return destination


def _table_rows(text):
    return [line for line in text.splitlines() if line[:1].isdigit()]


def test_io_schema_1_0_document_renders(tmp_path, single_run):
    written = tmp_path / "written"
    _run(IO, single_run, OUTPUT_DIR, written, "--live-detail", "summary")
    old = _as_schema_1_0(
        written / RESULTS, tmp_path / "old.json", ELBENCHO_FILE_SIZE="1G"
    )
    rendered = tmp_path / "rendered"
    done = _run(IO, FROM_JSON, old, OUTPUT_DIR, rendered)
    text = (written / REPORT).read_text(encoding="utf-8")
    assert _table_rows(done.stdout) == _table_rows(text)
    # The datestamp comes from the result file names, the file size from
    # the saved settings; unrecorded workload fields read null.
    assert f"File Size: 1G - WRITE - Seq - DirectIO - 60s - {_DS}" in done.stdout
    assert "write_elapsed_ms=null" in done.stdout
    assert (rendered / HTML).is_file()


def _as_sidecar_1_0(document, base):
    """Rewrite each ``full`` sidecar as the schema 1.0 writer would have
    (rates only), updating its reference."""
    import zstandard  # pylint: disable=import-outside-toplevel

    refs = [
        phase["live"]["client_series"]
        for execution in document["executions"]
        for phase in (execution.get("results") or {}).get("phases") or []
        if isinstance((phase.get("live") or {}).get("client_series"), dict)
        and "sha256" in phase["live"]["client_series"]
    ]
    for ref in refs:
        path = base / ref["path"]
        with zstandard.open(path, "rt", encoding="utf-8") as lines:
            rows = [json.loads(line) for line in lines]
        rows[0]["schema_version"] = "1.0"
        for row in rows[1:]:
            row.pop("done_bytes_delta")
        raw = zstandard.ZstdCompressor().compress(
            "".join(json.dumps(row) + "\n" for row in rows).encode()
        )
        path.write_bytes(raw)
        ref.update(sha256=hashlib.sha256(raw).hexdigest(), size_bytes=len(raw))
    return refs


def test_schema_1_0_sidecars_fall_back_with_a_clear_message(tmp_path, single_run):
    written = tmp_path / "written"
    _run(IO, single_run, OUTPUT_DIR, written)
    old = _as_schema_1_0(written / RESULTS, written / "old.json")
    document = _document(old)
    assert _as_sidecar_1_0(document, written)
    old.write_text(json.dumps(document), encoding="utf-8")
    rendered = tmp_path / "rendered"
    done = _run(IO, FROM_JSON, old, OUTPUT_DIR, rendered)
    assert "schema 1.0 sidecars hold no DoneBytes" in done.stderr
    assert "malformed" not in done.stderr
    assert _table_rows(done.stdout) == _table_rows(
        (written / REPORT).read_text(encoding="utf-8")
    )
    assert (rendered / HTML).is_file()


def test_mdtest_and_batch_schema_1_0_documents_render(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    written = tmp_path / "md"
    _run(MDTEST, write_prebatch_mdtest_dir(source), OUTPUT_DIR, written)
    old = _as_schema_1_0(written / RESULTS, tmp_path / "md.json")
    done = _run(MDTEST, FROM_JSON, old, OUTPUT_DIR, tmp_path / "md-rendered")
    assert _table_rows(done.stdout) == _table_rows(
        (written / REPORT).read_text(encoding="utf-8")
    )

    batch = tmp_path / "batch"
    _publish_results(_build_batch(batch))
    _run(FILESYSTEM, batch, OUTPUT_DIR, tmp_path / "batch-report")
    old = _as_schema_1_0(tmp_path / "batch-report" / RESULTS, tmp_path / "b.json")
    output = tmp_path / "batch-rendered"
    _run(FILESYSTEM, FROM_JSON, old, OUTPUT_DIR, output)
    assert (output / "reports.txt").is_file()
    for group in ("0001", "0002", "0003"):
        assert (output / "groups" / group / REPORT).is_file()


# -- report.html selection ----------------------------------------------------


@pytest.fixture(name="sweep", scope="module")
def sweep_fixture(tmp_path_factory):
    """A node sweep without a ledger, and its unfiltered document."""
    root = tmp_path_factory.mktemp("sweep")
    result = root / f"elbencho-{_DS}"
    result.mkdir()
    (result / "env_used.yaml").write_text(_ENV_USED, encoding="utf-8")
    for nodes in (1, 2, 4):
        _write_result(result, nodes=nodes, threads=4)
    _run(IO, result, OUTPUT_DIR, root / "full")
    return result, root / "full" / RESULTS


def test_from_json_html_honours_filters_like_raw_input(tmp_path, sweep):
    result, document = sweep
    raw = tmp_path / "raw"
    _run(IO, result, OUTPUT_DIR, raw, ONLY_NODES, "2")
    rendered = tmp_path / "rendered"
    _run(IO, FROM_JSON, document, OUTPUT_DIR, rendered, ONLY_NODES, "2")
    assert _chart_data(rendered) == _chart_data(raw)
    assert _chart_data(rendered) != _chart_data(document.parent)
    # The raw run's own document renders the same page with the same flags.
    again = tmp_path / "again"
    _run(IO, FROM_JSON, raw / RESULTS, OUTPUT_DIR, again, ONLY_NODES, "2")
    assert (again / HTML).read_bytes() == (raw / HTML).read_bytes()


def test_front_door_html_honours_groups_and_live_detail(tmp_path, single_run):
    raw = tmp_path / "raw"
    _run(IO, single_run, OUTPUT_DIR, raw, "--live-detail", "summary")
    full = tmp_path / "full"
    _run(IO, single_run, OUTPUT_DIR, full)
    rendered = tmp_path / "rendered"
    done = _run(
        FILESYSTEM,
        FROM_JSON,
        full / RESULTS,
        OUTPUT_DIR,
        rendered,
        "--live-detail",
        "summary",
    )
    assert "Live detail is summary, not full" in done.stderr
    assert _chart_data(rendered) == _chart_data(raw)
    assert _chart_data(rendered) != _chart_data(full)


def test_batch_document_html_honours_groups(tmp_path):
    batch = tmp_path / "batch"
    _publish_results(_build_batch(batch))
    _run(FILESYSTEM, batch, OUTPUT_DIR, tmp_path / "all")
    _run(FILESYSTEM, batch, OUTPUT_DIR, tmp_path / "raw", "--groups", "0001")
    rendered = tmp_path / "rendered"
    _run(
        FILESYSTEM,
        FROM_JSON,
        tmp_path / "all" / RESULTS,
        OUTPUT_DIR,
        rendered,
        "--groups",
        "0001",
    )
    assert _chart_data(rendered) == _chart_data(tmp_path / "raw")
    assert _chart_data(rendered) != _chart_data(tmp_path / "all")
    # The batch document holds every fragment; none are left behind.
    assert not list(
        (tmp_path / "all").rglob(filesystem_batch.RESULTS_FRAGMENT_FILENAME)
    )


def test_selection_keeps_matching_executions():
    io_group = {"group_id": "0001", "kind": "io"}
    md_group = {"group_id": "0002", "kind": "mdtest"}
    io = {"coordinates": {"nodes": 2, "threads_per_node": 4, "io_size_spec": "1M"}}
    md = {"coordinates": {"nodes": 2, "tasks_per_node": 8}}
    assert ReportSelection().keep_execution() is None
    selection = ReportSelection(nodes=frozenset([2]), sizes=frozenset(["1M"]))
    keep = selection.keep_execution()
    assert keep(io_group, io) and keep(md_group, md)
    assert not keep(io_group, {"coordinates": None})
    assert not ReportSelection(threads=frozenset([4])).keeps(md_group, md)
    assert not ReportSelection(groups=frozenset(["0001"])).keeps(md_group, md)
    assert not ReportSelection(kinds=frozenset(["io"])).keeps(md_group, md)


# -- raw input without a ledger ------------------------------------------------


@pytest.mark.parametrize("script", [IO, MDTEST])
def test_ledgerless_filters_record_every_execution(tmp_path, script):
    source = tmp_path / "source"
    source.mkdir()
    writer = write_prebatch_io_dir if script == IO else write_prebatch_mdtest_dir
    result = writer(source)
    _run(script, result, OUTPUT_DIR, tmp_path / "all")
    _run(script, result, OUTPUT_DIR, tmp_path / "some", ONLY_NODES, "2")
    every = _document(tmp_path / "all" / RESULTS)["executions"]
    some = _document(tmp_path / "some" / RESULTS)["executions"]
    assert [e["coordinate_key"] for e in some] == [e["coordinate_key"] for e in every]
    states = {e["coordinates"]["nodes"]: e["result_state"] for e in some}
    assert states.pop(2) == "complete"
    assert states and set(states.values()) == {"not_reported"}


# -- batch documents through the engines ----------------------------------------


@pytest.fixture(name="batch_document", scope="module")
def batch_document_fixture(tmp_path_factory):
    root = tmp_path_factory.mktemp("batch-document")
    batch = root / "batch"
    _publish_results(_build_batch(batch))
    _run(FILESYSTEM, batch, OUTPUT_DIR, root / "report")
    return root / "report" / RESULTS


@pytest.mark.parametrize(
    "script, groups", [(IO, ("0001", "0002")), (MDTEST, ("0003",))]
)
def test_engine_renders_a_batch_document_in_batch_layout(
    tmp_path, batch_document, script, groups
):
    output = tmp_path / "out"
    done = _run(script, FROM_JSON, batch_document, OUTPUT_DIR, output, check=False)
    assert done.returncode == 0, done.stderr
    rendered = sorted(path.parent.name for path in output.glob(f"groups/*/{REPORT}"))
    assert rendered == list(groups)
    assert (output / HTML).is_file()
    assert not (output / REPORT).exists()


# -- inputs, exit statuses and success -----------------------------------------


@pytest.mark.parametrize("script", [IO, MDTEST])
def test_same_input_spelled_twice_is_one_input(tmp_path, script):
    source = tmp_path / "source"
    source.mkdir()
    writer = write_prebatch_io_dir if script == IO else write_prebatch_mdtest_dir
    result = writer(source)
    _run(script, result, f"{result}/", OUTPUT_DIR, tmp_path / "out")
    groups = _document(tmp_path / "out" / RESULTS)["groups"]
    assert [group["group_id"] for group in groups] == ["0000"]


@pytest.mark.parametrize(
    "script, kind", [(IO, "io"), (MDTEST, "mdtest"), (FILESYSTEM, "io")]
)
def test_several_documents_without_text_exit_consistently(tmp_path, script, kind):
    """Nothing to report: 1, or 3 when --require-complete finds incompleteness
    (without a ledger no execution is known to have succeeded)."""
    source = tmp_path / "source"
    source.mkdir()
    writer = write_prebatch_io_dir if kind == "io" else write_prebatch_mdtest_dir
    engine = IO if kind == "io" else MDTEST
    _run(engine, writer(source), OUTPUT_DIR, tmp_path / "doc")
    document = tmp_path / "doc" / RESULTS
    arguments = [FROM_JSON, document, document, ONLY_NODES, "64"]
    plain = _run(script, *arguments, OUTPUT_DIR, tmp_path / "plain", check=False)
    assert plain.returncode == 1, plain.stderr
    assert NO_TEXT in plain.stderr
    strict = _run(
        script, *arguments, REQUIRE_COMPLETE, OUTPUT_DIR, tmp_path / "s", check=False
    )
    assert strict.returncode == 3, strict.stderr


def test_stale_text_does_not_count_as_this_runs_report(tmp_path, sweep):
    _, document = sweep
    output = tmp_path / "out"
    (output / "d1").mkdir(parents=True)
    (output / "d1" / REPORT).write_text("stale\n", encoding="utf-8")
    done = _run(
        FILESYSTEM,
        FROM_JSON,
        document,
        document,
        OUTPUT_DIR,
        output,
        ONLY_NODES,
        "64",
        check=False,
    )
    assert done.returncode == 1
    assert NO_TEXT in done.stderr


# -- pool parsing and file modes -------------------------------------------------


def test_mdtest_parses_each_execution_in_a_pool_task(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    result = str(write_prebatch_mdtest_dir(source))
    submitted = []

    class Recording(_MDTEST.InlineExecutor):
        """Inline executor that records each task."""

        def submit(self, fn, /, *args, **kwargs):
            submitted.append(args[0].__name__)
            return super().submit(fn, *args, **kwargs)

    env = {"MDTEST_ITERATIONS": 2}
    fragment = _MDTEST.start_results_fragment(result, env, (None, None), Recording())()
    assert fragment == _MDTEST.build_results_fragment(result, env)
    assert submitted == ["_slot_entry"] * len(fragment["executions"]) != []


def test_published_files_get_the_umask_mode(tmp_path):
    previous = os.umask(0o027)
    try:
        filesystem_results.write_document({"a": 1}, tmp_path / RESULTS)
        with filesystem_results.SidecarWriter(tmp_path / "s.jsonl.zst", {"h": 1}):
            pass
        # pylint: disable-next=protected-access
        filesystem_batch._publish_report_text(tmp_path / "reports.txt", "text\n")
    finally:
        os.umask(previous)
    for name in (RESULTS, "s.jsonl.zst", "reports.txt"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o640, name
