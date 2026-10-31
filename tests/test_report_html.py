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

"""Tests for the results document loader and the HTML report library."""

import base64
from collections import Counter
import copy
import json
import math
import os
from pathlib import Path
import re
import stat

import pytest

from lib.filesystem_results import ReportSelection
from lib.report_html import (
    CHART_JS_INTEGRITY,
    CHART_JS_URL,
    render_report,
    write_report,
)
from lib.report_html.common import config_parts, sort_executions
from lib.report_html.model import TEMPLATES
from lib.report_html.reduce import (
    CLIENT_TABLE_MAX_ROWS,
    HEATMAP_MAX_COLS,
    HEATMAP_MAX_ROWS,
    HEATMAP_MISSING,
    MAX_POINTS,
    decimate,
    heatmap_cell,
    sig,
)
from lib.report_html.render import ASSET_DIR, card_number_text, exact_text
from lib.report_html.summary import build_report
from lib.results_document import (
    ResultsDocumentError,
    document_from_data,
    load_document,
    read_client_series,
    sidecar_path,
)
from tests import report_html_test_support as docs
from tests.test_filesystem_results_schema import validate_results_document

HOSTILE = '</script><img src=x onerror="alert(1)">'
CLIENTS = 300
INTERVALS = 400
SECTION = "g0001-1m-seq-dio"
OPS = ("create", "stat", "delete")
LIVE_EXECUTION = "0003"
_BLOB = re.compile(
    r'<script type="application/json" id="report-charts">(.*?)</script>', re.S
)


def _io_executions(sidecar_ref=None, clients=None):
    """A node x thread sweep; one execution carries live data."""
    executions = []
    number = 0
    for threads in (4, 8):
        for nodes in (1, 2, 4):
            number += 1
            execution_id = f"{number:04d}"
            live = None
            if execution_id == LIVE_EXECUTION and clients:
                live = docs.live_object(clients, INTERVALS, sidecar_ref)
            phases = [
                docs.io_phase("write", nodes * threads * 0.25, live=live),
                docs.io_phase("read", nodes * threads * 0.5),
            ]
            coords = docs.io_coordinates(nodes=nodes, threads=threads)
            executions.append(
                docs.io_execution(execution_id, coords, phases, group_id="0001")
            )
    return executions


def _full_document(base: Path, target="fast-pool", full=True, client0=None) -> dict:
    clients = docs.client_names(CLIENTS)
    clients[0] = client0 or clients[0]
    ref = None
    if full:
        ref = docs.write_sidecar(
            base, f"{LIVE_EXECUTION}.write.jsonl.zst", clients, INTERVALS
        )
    groups = [
        docs.group("0001", "io", target),
        docs.group("0002", "mdtest", "meta-pool"),
    ]
    executions = _io_executions(ref, clients) + [
        docs.md_execution("0007", 1, 8, 1000.0, group_id="0002"),
        docs.md_execution("0008", 2, 8, 1800.0, group_id="0002"),
    ]
    return docs.document(groups, executions)


def _write(base: Path, document: dict) -> Path:
    path = base / "results.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _render(base: Path, document: dict) -> str:
    return render_report([load_document(_write(base, document))])


def _blob(page: str) -> dict:
    return json.loads(_BLOB.search(page).group(1))


def _duplicate_ids(page: str) -> list[str]:
    ids = re.findall(r'\sid="([^"]+)"', page)
    return [name for name, count in Counter(ids).items() if count > 1]


@pytest.fixture(name="full_page")
def _full_page(tmp_path):
    return _render(tmp_path, _full_document(tmp_path))


def test_builder_documents_are_schema_valid(tmp_path):
    validate_results_document(_full_document(tmp_path))


def test_only_external_url_is_the_pinned_chart_js(full_page):
    assert set(re.findall(r"https?://[^\s\"'<>)]+", full_page)) == {CHART_JS_URL}
    tag = re.search(r'<script src="https://[^>]*>', full_page).group(0)
    assert f'integrity="{CHART_JS_INTEGRITY}"' in tag
    assert 'crossorigin="anonymous"' in tag
    assert CHART_JS_URL.endswith("/npm/chart.js@4.4.1/dist/chart.umd.js")


def test_document_strings_are_escaped(tmp_path):
    document = _full_document(tmp_path, target=HOSTILE, client0=HOSTILE)
    document["batch"]["root"] = f"/mnt/test/{HOSTILE}"
    document["generator"]["command"] = ["extract-filesystem.py", HOSTILE]
    document["groups"][0]["settings"]["TEST_DIRS"] = {"a": HOSTILE}
    document["groups"][0]["settings_text"] = f"TEST_DIRS: {HOSTILE}\n"
    page = _render(tmp_path, document)

    assert "<img" not in page
    assert 'onerror="' not in page
    assert page.count("</script>") == 3  # data blob, Chart.js, report script
    assert "&lt;/script&gt;&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in page
    assert "<" not in _BLOB.search(page).group(1)
    heat = _blob(page)[f"{SECTION}-run-heatmap-r{LIVE_EXECUTION}-write"]
    assert HOSTILE in heat["heatmap"]["rows"]  # survives the blob round trip


def test_render_is_deterministic_and_order_independent(tmp_path):
    document = _full_document(tmp_path)
    first = _render(tmp_path, document)
    assert _render(tmp_path, document) == first
    shuffled = copy.deepcopy(document)
    shuffled["executions"].reverse()
    shuffled["groups"].reverse()
    in_order = render_report([document_from_data(document, tmp_path)])
    assert render_report([document_from_data(shuffled, tmp_path)]) == in_order


def test_anchors_templates_and_numbering(full_page):
    assert not _duplicate_ids(full_page)
    used = set(re.findall(r'data-template="([^"]+)"', full_page))
    assert used == set(TEMPLATES) - {"comparison-chart"}
    for template in ("sweep-throughput", "sweep-efficiency"):
        assert f'id="{SECTION}-{template}"' in full_page
    for phase in ("write", "read"):
        assert f'id="{SECTION}-results-table-{phase}"' in full_page
        assert f'id="{SECTION}-sweep-latency-{phase}"' in full_page
    run = f"-r{LIVE_EXECUTION}-write"
    for template in ("run-aggregate", "run-heatmap", "run-client-table"):
        assert f'id="{SECTION}-{template}{run}"' in full_page
    assert 'id="g0002-md-b4-i100-md-rates"' in full_page
    assert '<span class="num">Table 1.1</span>' in full_page
    assert '<span class="num">Figure 2.1</span> Throughput vs nodes' in full_page
    assert f'href="#{SECTION}-r{LIVE_EXECUTION}"' in full_page


def test_chart_series_layout(full_page):
    specs = _blob(full_page)
    throughput = specs[f"{SECTION}-sweep-throughput"]
    assert throughput["x"]["label"] == "nodes"
    names = [s["name"] for s in throughput["series"]]
    assert names == ["4t write", "4t read", "8t write", "8t read"]
    assert throughput["series"][1]["points"] == [[1, 2], [2, 4], [4, 8]]
    efficiency = specs[f"{SECTION}-sweep-efficiency"]
    assert efficiency["series"][0]["points"] == [[1, 100], [2, 100], [4, 100]]


def _many_configurations_document(threads_values=(4, 8), tasks_values=(4, 8)):
    """Nodes x threads x depths: twelve combinations besides the swept
    nodes by default, more than there are categorical colours; and a
    metadata sweep over nodes x tasks."""
    executions = []
    for nodes in (1, 2, 4):
        for threads in threads_values:
            for depth in (1, 2, 4, 8, 16, 32):
                coords = docs.io_coordinates(nodes=nodes, threads=threads, depth=depth)
                scale = nodes * threads * depth / 8
                phases = [
                    docs.io_phase("write", scale * 0.25),
                    docs.io_phase("read", scale * 0.5),
                ]
                number = len(executions) + 1
                executions.append(
                    docs.io_execution(f"{number:04d}", coords, phases, group_id="0001")
                )
    for nodes in (1, 2, 4):
        for tasks in tasks_values:
            number = len(executions) + 1
            rate = 500.0 * nodes * tasks
            executions.append(
                docs.md_execution(f"{number:04d}", nodes, tasks, rate, group_id="0002")
            )
    groups = [docs.group("0001", "io", "pool"), docs.group("0002", "mdtest", "meta")]
    return docs.document(groups, executions)


def test_no_two_series_of_a_chart_look_alike(tmp_path, full_page):
    page = _render(tmp_path, _many_configurations_document())
    for text in (page, full_page):
        assert not docs.duplicate_styles(_blob(text))


def test_charts_beyond_twenty_configurations_split_into_parts(tmp_path):
    document = _many_configurations_document((4, 8, 16, 32), tuple(range(1, 25)))
    page = _render(tmp_path, document)
    specs = _blob(page)
    assert not docs.duplicate_styles(specs)
    assert not _duplicate_ids(page)
    names = [f"{SECTION}-sweep-{kind}" for kind in ("throughput", "iops")]
    names += [f"{SECTION}-sweep-{kind}" for kind in ("efficiency", "share")]
    names += [f"{SECTION}-sweep-latency-{phase}" for phase in ("write", "read")]
    for name in names:
        assert name not in specs
        for part in (1, 2):  # 12 configurations x (two phases or percentiles)
            assert len(specs[f"{name}-part{part}"]["series"]) == 2 * 12
    md_base = "g0002-md-b4-i100"
    for op in ("create", "delete", "stat"):
        for kind, lines in (("rates", 12), ("latency", 2 * 12)):
            for part in (1, 2):
                spec = specs[f"{md_base}-md-{kind}-{op}-part{part}"]
                assert len(spec["series"]) == lines
    assert page.count("(part 1 of 2)") == page.count("(part 2 of 2)") > 0
    assert "Colours repeat after ten configurations" in page


def test_config_parts_are_balanced_and_bounded():
    assert [len(p.indices) for p in config_parts(20)] == [20]
    assert [len(p.indices) for p in config_parts(21)] == [11, 10]
    assert [len(p.indices) for p in config_parts(41)] == [14, 14, 13]
    assert [p.tail for p in config_parts(24)] == ["-part1", "-part2"]
    assert [p.suffix for p in config_parts(24)] == [" (part 1 of 2)", " (part 2 of 2)"]
    assert config_parts(20)[0].tail == config_parts(20)[0].suffix == ""
    assert [p.indices for p in config_parts(24)] == [range(12), range(12, 24)]


def test_latency_charts_split_by_phase(tmp_path):
    specs = _blob(_render(tmp_path, _many_configurations_document()))
    for phase in ("write", "read"):
        latency = specs[f"{SECTION}-sweep-latency-{phase}"]
        assert len(latency["series"]) == 2 * 12
        assert not any(phase in item["name"] for item in latency["series"])
    assert f"{SECTION}-sweep-latency" not in specs
    md_latency = sorted(name for name in specs if "md-latency" in name)
    assert md_latency == [
        f"g0002-md-b4-i100-md-latency-{op}" for op in ("create", "delete", "stat")
    ]


def test_results_tables_write_then_read(full_page):
    write_id = f'id="{SECTION}-results-table-write"'
    read_id = f'id="{SECTION}-results-table-read"'
    assert full_page.index(write_id) < full_page.index(read_id)
    between = full_page[full_page.index(write_id) : full_page.index(read_id)]
    assert "<figure" not in between
    write = full_page.split(write_id)[1].split("</table>")[0]
    read = full_page.split(read_id)[1].split("</table>")[0]
    assert "Write results per configuration" in write
    assert "Read results per configuration" in read
    assert 'scope="col">op<' not in write + read
    assert write.count("<tr>") == read.count("<tr>") == 7  # head + six runs
    # The run ID is an extra column; it links where per-run detail exists.
    assert '<th class="txt x" scope="col">run</th>' in write
    link = f'<a href="#{SECTION}-r{LIVE_EXECUTION}">{LIVE_EXECUTION}</a>'
    assert f'<td class="txt x">{link}</td>' in write
    assert '<td class="txt x">0001</td>' in write


def test_reduction_limits(full_page):
    specs = _blob(full_page)
    for spec in specs.values():
        for item in spec.get("series", []):
            assert len(item.get("points", [])) <= MAX_POINTS
    run = f"-r{LIVE_EXECUTION}-write"
    aggregate = specs[f"{SECTION}-run-aggregate{run}"]
    assert 0 < len(aggregate["series"][0]["points"]) <= MAX_POINTS
    heat = specs[f"{SECTION}-run-heatmap{run}"]["heatmap"]
    assert len(heat["rows"]) == HEATMAP_MAX_ROWS
    assert heat["rows"][0] == "host000 [rank 0]"  # slowest first
    assert heat["cols"] <= HEATMAP_MAX_COLS
    cells = base64.b64decode(heat["data"])
    assert len(cells) == HEATMAP_MAX_ROWS * heat["cols"]
    table = full_page.split(f'id="{SECTION}-run-client-table{run}"')[1]
    table = table.split("</table>")[0]
    assert table.count("<tr>") == CLIENT_TABLE_MAX_ROWS + 1
    assert f"{CLIENT_TABLE_MAX_ROWS} slowest of {CLIENTS} clients" in table


def test_live_below_full_is_noted(tmp_path):
    page = _render(tmp_path, _full_document(tmp_path, full=False))
    assert "need live detail full" in page
    assert "run-heatmap" not in page
    assert f'id="{SECTION}-run-aggregate-r{LIVE_EXECUTION}-write"' in page


def test_mdtest_only_document(tmp_path):
    executions = [
        docs.md_execution("0001", nodes, 8, 500.0 * nodes, group_id="0000")
        for nodes in (1, 2, 4)
    ]
    document = docs.document([docs.group("0000", "mdtest", "meta-pool")], executions)
    page = _render(tmp_path, document)
    specs = _blob(page)
    rates = specs["g0000-md-b4-i100-md-rates"]
    assert [s["name"] for s in rates["series"]] == ["create", "stat", "delete"]
    assert rates["series"][0]["points"] == [[1, 500], [2, 1000], [4, 2000]]
    assert "g0000-md-b4-i100-md-latency" in specs
    assert "Peak stat" in page and "6,000" in page


def test_mdtest_producer_versions_are_listed(tmp_path):
    executions = [
        docs.md_execution(f"000{number}", number, 8, 500.0, group_id="0000")
        for number in (1, 2)
    ]
    for execution, versions in zip(executions, (["3.4.1", None], ["3.4.0", "3.4.1"])):
        execution["results"]["iterations"] = [
            {"iteration": n, "state": "complete", "problems": [], "phases": []}
            | {"producer_version": version}
            for n, version in enumerate(versions, 1)
        ]
    document = docs.document([docs.group("0000", "mdtest", "meta-pool")], executions)
    validate_results_document(document)
    page = _render(tmp_path, document)
    assert "Produced by" in page and "3.4.0, 3.4.1" in page


def test_single_configuration_uses_bars(tmp_path):
    coords = docs.io_coordinates(nodes=2, threads=8)
    phases = [docs.io_phase("write", 1.5), docs.io_phase("read", 3.0)]
    execution = docs.io_execution("0001", coords, phases)
    document = docs.document([docs.group("0000", "io", "pool")], [execution])
    specs = _blob(_render(tmp_path, document))
    throughput = specs["g0000-1m-seq-dio-sweep-throughput"]
    assert throughput["kind"] == "bar"
    assert throughput["x"]["categories"] == ["2n·8t·qd1"]
    assert "g0000-1m-seq-dio-sweep-efficiency" not in specs


def test_several_documents_prefix_anchors(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    loaded = [
        load_document(_write(first, _full_document(first, full=False))),
        load_document(_write(second, _full_document(second, full=False))),
    ]
    page = render_report(loaded)
    assert f'id="d1-{SECTION}-sweep-throughput"' in page
    assert f'id="d2-{SECTION}-sweep-throughput"' in page
    assert "Filesystem results from 2 documents" in page


@pytest.mark.parametrize(
    "change, message",
    [
        ({"schema_version": "2.0"}, "schema_version '2.0' is not supported"),
        ({"schema": "other"}, "not a storage-scale-test/filesystem-batch-results"),
    ],
)
def test_unsupported_document_fails_clearly(tmp_path, change, message):
    document = docs.document([], [])
    document.update(change)
    with pytest.raises(ResultsDocumentError, match=re.escape(message)):
        load_document(_write(tmp_path, document))


def test_minor_versions_and_unknown_fields_load(tmp_path):
    document = docs.document([], [])
    document["schema_version"] = "1.7"
    document["future_field"] = {"x": 1}
    loaded = load_document(_write(tmp_path, document))
    assert loaded.base_dir == tmp_path.resolve()
    assert len(loaded.sha256) == 64


def test_invalid_json_fails_clearly(tmp_path):
    path = tmp_path / "results.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ResultsDocumentError, match="not valid JSON"):
        load_document(path)


def test_sidecar_streams_and_rejects_escaping_paths(tmp_path):
    ref = docs.write_sidecar(tmp_path, "0001.read.jsonl.zst", ["a", "b"], 3)
    loaded = load_document(_write(tmp_path, docs.document([], [])))
    series = read_client_series(loaded, ref)
    assert series.clients == ["a", "b"]
    assert list(series.rows) == [
        (1000, [10_000_000, 20_000_000]),
        (2000, [None, 20_000_000]),
        (3000, [10_000_000, 20_000_000]),
    ]
    for path in ("../x.jsonl.zst", "/etc/passwd"):
        with pytest.raises(ResultsDocumentError):
            sidecar_path(loaded, {"path": path})


def test_unreadable_sidecar_becomes_a_note(tmp_path):
    document = _full_document(tmp_path)
    (tmp_path / "results.live" / f"{LIVE_EXECUTION}.write.jsonl.zst").write_bytes(
        b"not zstd"
    )
    page = _render(tmp_path, document)
    assert "Per-client data unavailable" in page
    assert f'id="{SECTION}-run-client-table-r{LIVE_EXECUTION}-write"' in page


def test_write_report_replaces_atomically(tmp_path):
    target = tmp_path / "out" / "report.html"
    write_report(target, "<p>one</p>")
    previous = os.umask(0o027)
    try:
        write_report(target, "<p>two</p>")
    finally:
        os.umask(previous)
    assert target.read_text(encoding="utf-8") == "<p>two</p>"
    assert [p.name for p in target.parent.iterdir()] == ["report.html"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_decimate_keeps_extremes_and_limit():
    xs = list(range(1000))
    ys = [1.0] * 1000
    ys[500], ys[700] = 9.0, -9.0
    points = decimate(xs, ys, 100)
    assert len(points) <= 100
    assert [500, 9] in points and [700, -9] in points
    assert decimate([1, 2, 3], [1.23456, None, 3]) == [[1, 1.235], [3, 3]]
    assert decimate([1, 2, 3], [1, math.nan, 3]) == [[1, 1], [3, 3]]
    for limit in range(4):
        assert len(decimate(xs, ys, limit)) <= limit
    assert decimate(xs, ys, 1) == [[500, 9]]


def test_heatmap_cell_treats_nan_as_missing():
    assert heatmap_cell([math.nan]) == HEATMAP_MISSING
    assert heatmap_cell([math.nan, 1.0]) == heatmap_cell([1.0])


def test_location_hash_decoding_is_guarded():
    script = (ASSET_DIR / "report.js").read_text(encoding="utf-8")
    assert "try { id = decodeURIComponent(id); } catch" in script


def test_charts_are_built_for_printing():
    script = (ASSET_DIR / "report.js").read_text(encoding="utf-8")
    assert 'addEventListener("beforeprint", prepareForPrint)' in script
    assert 'matchMedia("print")' in script


def test_sig_rounds_to_significant_figures():
    assert sig(123456.7) == 123500
    assert sig(0.00123456) == 0.001235
    assert sig(42) == 42
    assert sig(float("nan")) is None
    assert sig(None) is None


def _one_group(*executions) -> dict:
    return docs.document([docs.group("0001", "io", "pool")], list(executions))


def _shape_executions() -> list[dict]:
    """Write+read, read-only and write-only executions of one size."""
    both = docs.io_coordinates()
    read_only = docs.io_coordinates()
    read_only.update(write_block_size_bytes=None, write_access=None)
    write_only = docs.io_coordinates()
    write_only.update(read_block_size_bytes=None, read_access=None)
    phases = [docs.io_phase("write", 1.0), docs.io_phase("read", 2.0)]
    return [
        docs.io_execution("0001", both, phases, "0001"),
        docs.io_execution("0002", read_only, [docs.io_phase("read", 3.0)], "0001"),
        docs.io_execution("0003", write_only, [docs.io_phase("write", 4.0)], "0001"),
    ]


def test_phase_plans_get_distinct_sections(tmp_path):
    document = _one_group(*_shape_executions())
    page = render_report([document_from_data(document, tmp_path)])
    assert not _duplicate_ids(page)
    for shape in ("1m-seq-dio", "1m-seq-ro-dio", "1m-seq-wo-dio"):
        assert f'id="g0001-{shape}"' in page
    assert "1 MiB seq, read only · DirectIO" in page
    series = _blob(page)["g0001-1m-seq-ro-dio-sweep-throughput"]["series"]
    assert [s["name"] for s in series] == ["read"]


def _settings_box(tmp_path, settings: dict, **coordinates) -> str:
    coords = docs.io_coordinates()
    coords.update(coordinates)
    phases = [docs.io_phase("write", 1.0), docs.io_phase("read", 2.0)]
    group = docs.group("0001", "io", "pool", settings)
    execution = docs.io_execution("0001", coords, phases, "0001")
    document = docs.document([group], [execution])
    page = render_report([document_from_data(document, tmp_path)])
    return page.split('<details class="settings">')[1].split("</details>")[0]


def test_settings_box_hides_settings_that_do_not_apply(tmp_path):
    settings = {
        "ELBENCHO_FILE_SIZE": "4G",
        "ELBENCHO_FILES_PER_NODE": 32,
        "ELBENCHO_SINGLE_BIG_FILE_SIZE": "1T",
        "ELBENCHO_SCALE_READ_WRITE_DURATION": 60,
        "run_to_completion_option": 0,
        "sweep_read_from": "/mnt/test/data",
    }
    box = _settings_box(tmp_path, settings)
    assert "<dt>File size</dt><dd>4G</dd>" in box
    assert "<dt>Files/node</dt><dd>32</dd>" in box
    assert "Single big file size" not in box
    assert "<dt>Duration (s)</dt><dd>60</dd>" in box
    box = _settings_box(
        tmp_path,
        {**settings, "run_to_completion_option": 1},
        phase_plan="read_from",
        single_big_file=True,
        run_to_completion=True,
    )
    assert "<dt>Read from</dt><dd>/mnt/test/data</dd>" in box
    assert "<dt>Run to completion</dt><dd>1</dd>" in box
    for label in ("File size", "Files/node", "Single big file size", "Duration"):
        assert f"<dt>{label}" not in box
    box = _settings_box(tmp_path, settings, single_big_file=True)
    assert "<dt>Single big file size</dt><dd>1T</dd>" in box
    assert "<dt>File size</dt>" not in box


def test_peaks_use_each_phases_block_size_and_access(tmp_path):
    coords = docs.io_coordinates()
    coords.update(read_block_size_bytes=4096, read_access="random")
    phases = [docs.io_phase("write", 1.0), docs.io_phase("read", 0.5, block=4096)]
    document = _one_group(docs.io_execution("0001", coords, phases, "0001"))
    best = build_report([document_from_data(document, tmp_path)]).sections[0].best
    assert best[("write", "gbs")]["test"] == "Seq write · DirectIO"
    assert best[("write", "gbs")]["io_size"] == "1 MiB"
    assert best[("read", "gbs")]["test"] == "Rand read · DirectIO"
    assert best[("read", "iops")]["io_size"] == "4 KiB"


def test_missing_dimensions_and_nan_rates_render(tmp_path):
    executions = []
    for number, (nodes, depth) in enumerate(((1, 1), (2, None), (None, 1), (4, 1)), 1):
        coords = docs.io_coordinates(nodes=nodes or 8, depth=depth or 1)
        phases = [docs.io_phase("write", float(number)), docs.io_phase("read", 1.0)]
        executions.append(docs.io_execution(f"{number:04d}", coords, phases, "0001"))
        coords.update(nodes=nodes, io_depth=depth)  # an older document
    executions[3]["results"]["phases"][1]["elbencho_first"]["bytes_per_s"] = math.nan
    metadata = [
        docs.md_execution("0011", 1, 8, 100.0, "0002"),
        docs.md_execution("0012", 2, 8, 1.0, "0002"),
    ]
    metadata[1]["coordinates"]["nodes"] = None
    metadata[1]["results"]["aggregate"]["phases"][0]["ops_per_s"]["mean"] = math.nan
    document = docs.document(
        [docs.group("0001", "io", "pool"), docs.group("0002", "mdtest", "pool")],
        executions + metadata,
    )
    page = render_report([document_from_data(document, tmp_path)])
    specs = _blob(page)
    series = specs["g0001-1m-seq-dio-sweep-throughput"]["series"]
    for item in series:
        assert all(None not in point for point in item["points"])
    assert "qd? write" in [item["name"] for item in series]  # unknown depth
    assert "None" not in page
    assert "Peak create" in page


def test_efficiency_is_relative_to_each_series_first_point(tmp_path):
    executions = []
    for threads, node_counts in ((4, (1, 2, 4)), (8, (2, 4))):
        for nodes in node_counts:
            coords = docs.io_coordinates(nodes=nodes, threads=threads)
            coords.update(read_block_size_bytes=None, read_access=None)
            phases = [docs.io_phase("write", nodes * 0.5)]
            executions.append(
                docs.io_execution(f"{threads}{nodes:03d}", coords, phases, "0001")
            )
    page = render_report([document_from_data(_one_group(*executions), tmp_path)])
    efficiency = _blob(page)["g0001-1m-seq-wo-dio-sweep-efficiency"]["series"]
    assert efficiency[1]["points"] == [[2, 100], [4, 100]]
    assert "relative to each series&#x27; run with the fewest nodes" in page


def test_keep_execution_filters_every_part(tmp_path):
    loaded = [document_from_data(_full_document(tmp_path), tmp_path)]

    def keep(group, execution):
        threads = execution["coordinates"].get("threads_per_node")
        return group["kind"] == "io" and threads == 8

    page = render_report(loaded, keep_execution=keep)
    specs = _blob(page)
    names = [s["name"] for s in specs[f"{SECTION}-sweep-throughput"]["series"]]
    assert names == ["write", "read"]
    assert 'id="g0002-md-b4-i100"' not in page
    assert "Peak create" not in page
    assert f"{SECTION}-r{LIVE_EXECUTION}" not in page  # its live run is dropped
    assert "1 sections · 3 executions" in page


def test_selection_applies_to_groups_without_executions(tmp_path):
    # A result directory with nothing parseable is a group with no executions.
    execution = docs.io_execution(
        "0001", docs.io_coordinates(), [docs.io_phase("write", 1.0)], "0001"
    )
    document = docs.document(
        [docs.group("0001", "io", "pool-a"), docs.group("0002", "io", "pool-b")],
        [execution],
    )
    loaded = [document_from_data(document, tmp_path)]
    assert 'id="g0002-none"' in render_report(loaded)
    for selection, shown in (
        (ReportSelection(groups=frozenset(["0001"])), False),
        (ReportSelection(kinds=frozenset(["mdtest"])), False),
        (ReportSelection(nodes=frozenset([1])), False),
        (ReportSelection(groups=frozenset(["0002"])), True),
    ):
        page = render_report(loaded, keep_execution=selection.keep_execution())
        assert ('id="g0002-none"' in page) is shown, selection


def test_sources_are_the_result_directories_not_their_parent(tmp_path):
    groups = [docs.group("0001", "io", "pool"), docs.group("0002", "io", "pool")]
    for item, name in zip(groups, ("run-a", "run-b")):
        item["source"] = f"/mnt/test/results/{name}"
    document = docs.document(groups, [])
    report = build_report([document_from_data(document, tmp_path)])
    assert report.sources == ["/mnt/test/results/run-a", "/mnt/test/results/run-b"]
    document["batch"]["manifest_sha256"] = "0" * 64
    report = build_report([document_from_data(document, tmp_path)])
    assert report.sources == ["/mnt/test/results"]


@pytest.mark.parametrize("level", ["summary", "selected"])
def test_live_detail_caps_the_documents_level(tmp_path, level):
    loaded = [document_from_data(_full_document(tmp_path), tmp_path)]
    page = render_report(loaded, live_detail=level)
    assert "need live detail full" in page
    assert "run-heatmap" not in page and "run-client-table" not in page
    assert f'id="{SECTION}-run-aggregate-r{LIVE_EXECUTION}-write"' in page
    assert render_report(loaded, live_detail="full") == render_report(loaded)


def test_live_detail_none_omits_live_detail(tmp_path):
    loaded = [document_from_data(_full_document(tmp_path), tmp_path)]
    page = render_report(loaded, live_detail="none")
    assert f"-r{LIVE_EXECUTION}" not in page
    assert "fairness-table" not in page
    with pytest.raises(ValueError, match="live detail 'some'"):
        render_report(loaded, live_detail="some")


def test_documents_own_live_detail_applies(tmp_path):
    document = _full_document(tmp_path)
    document["generator"]["options"]["live_detail"] = "summary"
    page = render_report([document_from_data(document, tmp_path)])
    assert "need live detail full" in page and "run-heatmap" not in page


def test_sync_phases_are_left_out(tmp_path):
    document = _full_document(tmp_path)
    execution = [
        e for e in document["executions"] if e["execution_id"] == LIVE_EXECUTION
    ][0]
    sync = copy.deepcopy(execution["results"]["phases"][0]["live"])
    sync["domain"]["phase"] = "SYNC"
    execution["results"]["additional_live_domains"] = [sync]
    page = _render(tmp_path, document)
    assert "fairness-table-sync" not in page
    assert f"-r{LIVE_EXECUTION}-sync" not in page
    assert f"-r{LIVE_EXECUTION}-write" in page


def _rejected_steady_page(tmp_path):
    """Every live domain has a recorded but rejected steady window."""
    document = _full_document(tmp_path)
    for execution in document["executions"]:
        for phase in execution["results"].get("phases") or []:
            if phase.get("live"):
                phase["live"]["steady"] = {
                    "start_ms": 2001,
                    "end_ms": 2001,
                    "end_source": "elbencho_first",
                    "reason": "too_short",
                }
    return _render(tmp_path, document)


def test_rejected_steady_window_is_unavailable(tmp_path):
    page = _rejected_steady_page(tmp_path)
    specs = _blob(page)
    run = f"-r{LIVE_EXECUTION}-write"
    for template in ("aggregate", "spread", "clients"):
        assert not specs[f"{SECTION}-run-{template}{run}"].get("shade")
    assert "Shaded: steady window" not in page
    assert f"{SECTION}-run-fairness{run}" not in specs  # Jain is steady-only
    table = page.split(f'id="{SECTION}-fairness-table-write"')[1].split("</table>")[0]
    row = [r for r in table.split("<tr>") if LIVE_EXECUTION in r][0]
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row)
    assert cells[4] == "none (phase too short)"
    assert set(cells[5:14]) == {"–"}
    clients = page.split(f'id="{SECTION}-run-client-table{run}"')[1]
    body = clients.split("</table>")[0].split("</thead>")[1]
    assert "persistent" not in body and "transient" not in body
    assert "Jain –" in page and "persistent," not in page


def test_fairness_has_a_table_per_phase(tmp_path):
    document = _full_document(tmp_path)
    execution = [
        e for e in document["executions"] if e["execution_id"] == LIVE_EXECUTION
    ][0]
    write, read = execution["results"]["phases"]
    read["live"] = copy.deepcopy(write["live"])
    read["live"]["domain"]["phase"] = "read"
    page = _render(tmp_path, document)
    tables = re.findall(rf'id="{SECTION}-fairness-table(-[a-z]+)?"', page)
    assert tables == ["-write", "-read"]
    assert "Read live fairness" in page


def test_fairness_tables_put_write_first_when_read_comes_first(tmp_path):
    document = _full_document(tmp_path)
    live = [e for e in document["executions"] if e["execution_id"] == LIVE_EXECUTION]
    read_live = copy.deepcopy(live[0]["results"]["phases"][0]["live"])
    read_live["domain"]["phase"] = "read"
    for execution in document["executions"]:
        if execution["execution_id"] in ("0001", "0004"):
            execution["results"]["phases"][1]["live"] = read_live
    page = _render(tmp_path, document)
    tables = re.findall(rf'id="{SECTION}-fairness-table(-[a-z]+)?"', page)
    assert tables == ["-write", "-read"]


def test_executions_sort_by_number_not_text():
    keys = ["io/c1000/s064", "io/c500/s064", "io/c500/s008"]
    ordered = sort_executions({"coordinate_key": key} for key in keys)
    assert [e["coordinate_key"] for e in ordered] == [
        "io/c500/s008",
        "io/c500/s064",
        "io/c1000/s064",
    ]


def test_usable_steady_window_still_shades(full_page):
    specs = _blob(full_page)
    run = f"-r{LIVE_EXECUTION}-write"
    assert specs[f"{SECTION}-run-aggregate{run}"]["shade"]
    assert "Shaded: steady window" in full_page
    assert f"{SECTION}-run-fairness{run}" in specs


def _thread_sweep(block: int, seconds: int = 60, nodes: int = 1) -> dict:
    """A thread sweep at a fixed node count."""
    executions = []
    for number, threads in enumerate((1, 4, 16), 1):
        phases = [
            docs.io_phase(name, threads * 0.1, block=block)
            for name in ("write", "read")
        ]
        for phase in phases:
            phase["configured_time_limit_s"] = seconds
        coords = docs.io_coordinates(nodes=nodes, threads=threads, block=block)
        executions.append(docs.io_execution(f"{number:04d}", coords, phases))
    return docs.document([docs.group("0000", "io", "pool")], executions)


@pytest.mark.parametrize(
    "block, slug, iops_first", [(64 << 10, "64k", True), (1 << 20, "1m", False)]
)
def test_small_io_sizes_lead_with_linear_iops(tmp_path, block, slug, iops_first):
    page = _render(tmp_path, _thread_sweep(block))
    section = f"g0000-{slug}-seq-dio"
    iops, throughput = (
        page.index(f'id="{section}-sweep-{name}"') for name in ("iops", "throughput")
    )
    grid = page.index('<div class="grid">', page.index(f'id="{section}"'))
    # The leading chart is full width, before the grid of small ones.
    assert (iops < grid < throughput) == iops_first
    assert (throughput < grid < iops) == (not iops_first)
    scale = _blob(page)[f"{section}-sweep-iops"]["y"]["type"]
    assert scale == ("linear" if iops_first else "log")


def test_section_titles_name_shared_nodes_and_long_phases(tmp_path):
    page = _render(tmp_path, _thread_sweep(1 << 20, seconds=1800))
    assert "1 MiB seq · DirectIO · single node · 30 min per phase" in page
    table = page.split('id="g0000-1m-seq-dio-results-table-write"')[1]
    assert 'scope="col">nodes<' not in table.split("</table>")[0]
    page = _render(tmp_path, _thread_sweep(1 << 20, seconds=300, nodes=4))
    assert "1 MiB seq · DirectIO · 4 nodes <" in page
    # A node sweep keeps its nodes column and names no node count.
    assert 'scope="col">nodes<' in _render(tmp_path, _full_document(tmp_path))


def test_cards_shorten_millions():
    assert card_number_text(109_000_000) == "109M"
    assert card_number_text(14_160_000) == "14.16M"
    assert card_number_text(7630) == "7,630"


def test_histograms_draw_lines_without_markers(full_page):
    assert _blob(full_page)[f"{SECTION}-sweep-histogram"]["markers"] is False
    script = (ASSET_DIR / "report.js").read_text(encoding="utf-8")
    assert "spec.markers === false" in script


def test_sources_and_diagnostics_start_collapsed(tmp_path):
    groups = [docs.group("0001", "io", "pool"), docs.group("0002", "io", "pool")]
    for item, name in zip(groups, ("run-a", "run-b")):
        item["source"] = f"/mnt/test/results/{name}"
    document = docs.document(groups, [])
    document["diagnostics"] = [
        {"code": "no_execution_ledger", "detail": f"group {g}"}
        for g in ("0001", "0002")
    ]
    page = _render(tmp_path, document)
    assert '<details class="fold"><summary>Sources (2)</summary>' in page
    assert "<li>/mnt/test/results/run-a</li>" in page
    assert '<details class="fold"><summary>Diagnostics (2)</summary>' in page
    assert "<li>no_execution_ledger: group 0001</li>" in page


def test_metadata_results_split_by_operation(tmp_path):
    executions = [
        docs.md_execution(f"000{nodes}", nodes, 8, 500.0 * nodes, group_id="0000")
        for nodes in (1, 2, 4)
    ]
    document = docs.document([docs.group("0000", "mdtest", "meta-pool")], executions)
    page = _render(tmp_path, document)
    starts = [page.index(f'id="g0000-md-b4-i100-results-table-{op}"') for op in OPS]
    assert starts == sorted(starts)
    # Every chart comes before the tables.
    charts = [m.start() for m in re.finditer(r'<figure class="fig"', page)]
    assert charts and max(charts) < starts[0]
    for op, start in zip(OPS, starts):
        table = page[start:].split("</table>")[0]
        assert f"{op.capitalize()} results per configuration" in table
        assert table.count("<tr>") == 4  # head + three runs
        assert 'scope="col">op<' not in table


def test_peaks_compare_unrounded_values(tmp_path):
    groups = [docs.group(g, "io", "pool") for g in ("0001", "0002")]
    executions = []
    for group_id, iops in (("0001", 109_235_641.23), ("0002", 109_249_000.0)):
        phases = [docs.io_phase("read", 1.0)]
        phases[0]["elbencho_first"]["ops_per_s"] = iops
        coords = docs.io_coordinates()
        executions.append(docs.io_execution(group_id, coords, phases, group_id))
    page = _render(tmp_path, docs.document(groups, executions))
    card = page.split("Peak read IOPS")[0].rsplit('<a class="card"', 1)[1]
    assert 'href="#g0002-1m-seq-dio"' in card
    assert 'title="109,249,000">109.2M' in page


def test_run_buttons_only_with_per_run_detail(tmp_path, full_page):
    nav = full_page.split('<nav class="toc"')[1].split("</nav>")[0]
    assert "Expand all runs" in nav and "Collapse all runs" in nav
    page = _render(tmp_path, _thread_sweep(1 << 20))  # no live data
    nav = page.split('<nav class="toc"')[1].split("</nav>")[0]
    assert "data-runs" not in nav and "Light/dark" in nav


def test_metadata_rates_follow_normalization(tmp_path):
    executions = [
        docs.md_execution(f"000{nodes}", nodes, 8, 1000.0, group_id="0000")
        for nodes in (1, 2)
    ]
    document = docs.document([docs.group("0000", "mdtest", "meta-pool")], executions)
    document["generator"]["options"]["normalize_to"] = 1
    page = _render(tmp_path, document)
    table = page.split('id="g0000-md-b4-i100-results-table-create"')[1]
    rows = table.split("</table>")[0].split("<tr>")[2:]
    # 1,000 ops/s on two nodes is 500 per node; one node is unchanged.
    assert 'data-v="1000"' in rows[0] and 'data-v="500"' in rows[1]
    assert "Ops/s are normalized to 1 node" in page
    assert '<span class="sub">normalized to 1 node</span>' in page
    loaded = load_document(_write(tmp_path, document))
    again = render_report([loaded], normalize_to=2)
    assert "normalized to 2 nodes" in again


def test_exact_text_keeps_fractions():
    assert exact_text(109_235_641.23) == "109,235,641.23"
    assert exact_text(109_249_000.0) == "109,249,000"
