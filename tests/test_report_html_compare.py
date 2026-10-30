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

"""Tests for automatic and forced comparison pairing in the HTML report."""

import json
import re

import pytest

from lib.report_html import ComparisonFileError, load_comparison_file, render_report
from lib.report_html.common import group_contexts
from lib.report_html.pairing import (
    MAX_SERIES,
    TARGET,
    comparison_sets,
    size_bytes,
)
from lib.results_document import document_from_data, load_document
from tests import report_html_test_support as docs

_BLOB = re.compile(
    r'<script type="application/json" id="report-charts">(.*?)</script>', re.S
)
_CMP = re.compile(r'<div class="cmp" id="([^"]+)"><h3>([^<]*)')
SHAPE = "1m-seq-dio"


def _settings(target: str, **changes) -> dict:
    settings = {
        "EXECUTION_SUBSTRATE": "ssh",
        "STORAGE_TARGET_NAME": target,
        "TEST_DIRS": {f"/mnt/{target}/data/": "host[001-004]"},
        "FS_MAX_AGG_THROUGHPUT": 100,
        "ELBENCHO_SCALE_THREAD_LIST": ["8"],
        "ELBENCHO_SCALE_IO_SIZES": ["1M"],
        "ELBENCHO_IODEPTH_LIST": ["1"],
        "ELBENCHO_SCALE_READ_WRITE_DURATION": 60,
        "run_to_completion_option": 0,
        "ELBENCHO_FILE_LAYOUT": "worker-directories",
        "ELBENCHO_FILE_SIZE": "",
        "ELBENCHO_FILE_SIZE_MULTIPLIER": 1024,
        "ELBENCHO_FILES_PER_NODE": "",
        "dio_or_bio": "dio",
        "sweep_read_from": "",
        "nodes_spec": "1,2,4",
    }
    settings.update(changes)
    return settings


def _executions(
    group_id, scale=1.0, nodes=(1, 2, 4), direct=True, live=None, threads=8
):
    executions = []
    for count in nodes:
        coords = docs.io_coordinates(nodes=count, threads=threads)
        coords["direct_io"] = direct
        phases = [
            docs.io_phase("write", count * scale, live=live),
            docs.io_phase("read", count * scale * 2),
        ]
        execution_id = f"{group_id[-2:]}{count:02d}{int(direct)}"
        executions.append(docs.io_execution(execution_id, coords, phases, group_id))
    return executions


def _document(groups: list[tuple[str, str, dict, list]]) -> dict:
    """Groups of (group_id, target, settings, executions)."""
    records = [
        docs.group(gid, "io", target, settings) for gid, target, settings, _ in groups
    ]
    executions = [e for *_, items in groups for e in items]
    return docs.document(records, executions)


def _group(group_id, target, scale=1.0, **changes):
    return (
        group_id,
        target,
        _settings(target, **changes),
        _executions(group_id, scale),
    )


def _loaded(tmp_path, *documents):
    return [document_from_data(document, tmp_path) for document in documents]


def _page(tmp_path, *documents, comparisons=None):
    return render_report(_loaded(tmp_path, *documents), comparisons)


def _blob(page: str) -> dict:
    return json.loads(_BLOB.search(page).group(1))


def _members(page: str, anchor: str) -> str:
    """The member links paragraph of one comparison block."""
    block = page.split(f'<div class="cmp" id="{anchor}">')[1]
    return block.split('<p class="meta">')[1].split("</p>")[0]


def _sets(tmp_path, *documents, comparisons=None):
    loaded = _loaded(tmp_path, *documents)
    return comparison_sets(loaded, group_contexts(loaded), comparisons)


def test_targets_pair_within_a_batch(tmp_path):
    document = _document(
        [
            _group("0001", "pool-a"),
            _group("0002", "pool-b", 2.0),
            _group("0003", "pool-c", 3.0),
        ]
    )
    page = _page(tmp_path, document)
    blocks = _CMP.findall(page)
    assert len(blocks) == 1
    anchor, title = blocks[0]
    assert anchor.startswith("cmp-target-")
    assert title.strip() == "Storage target — 1 MiB seq · DirectIO"
    members = _members(page, anchor)
    assert members.startswith("Differs only in storage target:")
    assert '<a href="#g0002-1m-seq-dio">pool-b (g0002)</a>' in members
    specs = _blob(page)
    read = specs[f"{anchor}-throughput-read"]
    assert read["x"]["label"] == "nodes"
    assert [s["name"] for s in read["series"]] == ["pool-a", "pool-b", "pool-c"]
    assert read["series"][1]["points"] == [[1, 4], [2, 8], [4, 16]]
    assert specs[f"{anchor}-share-write"]["series"][2]["points"][0] == [1, 3]
    assert f"{anchor}-p99-read" in specs
    assert 'data-template="comparison-chart"' in page
    assert '<span class="num">Figure 2.1</span> Write throughput vs nodes' in page
    assert _page(tmp_path, document) == page


def test_direct_and_buffered_pair_across_documents(tmp_path):
    direct = _document([_group("0001", "pool-a")])
    buffered_group = (
        "0001",
        "pool-a",
        _settings("pool-a", dio_or_bio="bio", ELBENCHO_LIVEINT=1000),
        _executions("0001", 0.5, direct=False),
    )
    buffered = _document([buffered_group])
    page = _page(tmp_path, direct, buffered)
    (anchor, title), *rest = _CMP.findall(page)
    assert not rest
    assert anchor.startswith("cmp-io-mode-")
    assert title.strip() == "IO mode — 1 MiB seq"
    members = _members(page, anchor)
    assert 'href="#d1-g0001-1m-seq-dio">DirectIO (d1 g0001)' in members
    assert 'href="#d2-g0001-1m-seq-bio">Buffered (d2 g0001)' in members


def test_direct_and_buffered_pair_within_a_group(tmp_path):
    group_id, target, settings, executions = _group("0001", "pool-a")
    executions += _executions(group_id, 0.5, direct=False)
    sets = _sets(tmp_path, _document([(group_id, target, settings, executions)]))
    assert [s.dimension for s in sets] == ["io_mode"]
    assert [m.anchor for m in sets[0].members] == [f"g0001-{SHAPE}", "g0001-1m-seq-bio"]


def _io_mode_pair(tmp_path, plan="write_read", big=False, both=None, **changes):
    """Sets for DirectIO vs buffered groups that also differ in ``changes``;
    ``both`` settings and the coordinates apply to both groups."""
    both = both or {}
    groups = [
        ("0001", "pool-a", _settings("pool-a", **both), _executions("0001")),
        (
            "0002",
            "pool-a",
            _settings("pool-a", dio_or_bio="bio", **{**both, **changes}),
            _executions("0002", 0.5, direct=False),
        ),
    ]
    for *_, executions in groups:
        for execution in executions:
            execution["coordinates"]["phase_plan"] = plan
            execution["coordinates"]["single_big_file"] = big
            execution["coordinates"]["run_to_completion"] = bool(
                both.get("run_to_completion_option")
            )
    return _sets(tmp_path, _document(groups))


def _pairs(sets) -> bool:
    return [s.dimension for s in sets] == ["io_mode"]


def test_unused_single_big_file_settings_do_not_block_pairing(tmp_path):
    for key, value in (
        ("ELBENCHO_SINGLE_BIG_FILE_SIZE", "700G"),
        ("ELBENCHO_SINGLE_BIG_FILE_BASENAME", "other"),
    ):
        assert _pairs(_io_mode_pair(tmp_path, **{key: value}))
        assert not _io_mode_pair(tmp_path, big=True, **{key: value})


def test_read_from_sections_ignore_writer_file_settings(tmp_path):
    for key, value in (
        ("ELBENCHO_FILE_SIZE", "4G"),
        ("ELBENCHO_FILE_SIZE_MULTIPLIER", 64),
        ("ELBENCHO_FILES_PER_NODE", 32),
        ("ELBENCHO_SINGLE_BIG_FILE_SIZE", "700G"),
    ):
        big = key == "ELBENCHO_SINGLE_BIG_FILE_SIZE"
        assert not _io_mode_pair(tmp_path, big=big, **{key: value})
        assert _pairs(
            _io_mode_pair(tmp_path, plan="read_from", big=big, **{key: value})
        )


def test_single_big_file_ignores_per_file_settings(tmp_path):
    for key, value in (
        ("ELBENCHO_FILE_SIZE", "4G"),
        ("ELBENCHO_FILE_SIZE_MULTIPLIER", 64),
        ("ELBENCHO_FILES_PER_NODE", 32),
    ):
        assert not _io_mode_pair(tmp_path, **{key: value})
        assert _pairs(_io_mode_pair(tmp_path, big=True, **{key: value}))


def test_multiplier_is_ignored_with_an_explicit_file_size(tmp_path):
    sized = {"ELBENCHO_FILE_SIZE": "1G"}
    assert _pairs(_io_mode_pair(tmp_path, both=sized, ELBENCHO_FILE_SIZE_MULTIPLIER=64))
    assert not _io_mode_pair(tmp_path, ELBENCHO_FILE_SIZE_MULTIPLIER=64)


def test_read_pause_needs_write_and_read_phases(tmp_path):
    pause = {"ELBENCHO_READ_AFTER_WRITE_PAUSE": 30}
    both = {"ELBENCHO_READ_AFTER_WRITE_PAUSE": 0}
    assert not _io_mode_pair(tmp_path, both=both, **pause)
    assert _pairs(_io_mode_pair(tmp_path, plan="write_only", both=both, **pause))


def test_kubernetes_settings_apply_only_on_kubectl(tmp_path):
    image = {"KUBECTL_ELBENCHO_IMAGE": "registry.test/elbencho:2"}
    both = {"KUBECTL_ELBENCHO_IMAGE": "registry.test/elbencho:1"}
    assert _pairs(_io_mode_pair(tmp_path, both=both, **image))
    kubectl = {**both, "EXECUTION_SUBSTRATE": "kubectl"}
    assert not _io_mode_pair(tmp_path, both=kubectl, **image)


def test_kubernetes_settings_do_not_form_a_test_directory_set(tmp_path):
    for key, a, b in (
        ("KUBECTL_NAMESPACE", "team-a", "team-b"),
        ("KUBECTL_MAPPED_TEST_DIRS", {"/data/a": "x"}, {"/data/b": "x"}),
    ):
        groups = [
            ("0001", "pool-a", _settings("pool-a", **{key: a}), _executions("0001")),
            ("0002", "pool-a", _settings("pool-a", **{key: b}), _executions("0002")),
        ]
        assert not _sets(tmp_path, _document(groups))
        for _, _, settings, _ in groups:
            settings["EXECUTION_SUBSTRATE"] = "kubectl"
        sets = _sets(tmp_path, _document(groups))
        assert [s.dimension for s in sets] == ["test_dir"]
        # They do not block an IO-mode pair on SSH either.
        assert _pairs(_io_mode_pair(tmp_path, both={key: a}, **{key: b}))
        kubectl = {key: a, "EXECUTION_SUBSTRATE": "kubectl"}
        assert not _io_mode_pair(tmp_path, both=kubectl, **{key: b})


def test_kubernetes_mapped_read_from_applies_only_on_kubectl(tmp_path):
    key = "KUBECTL_MAPPED_READ_FROM"
    both = {"sweep_read_from": "/mnt/test/data", key: "/data/a"}
    changes = {key: "/data/b"}
    assert _pairs(_io_mode_pair(tmp_path, "read_from", both=both, **changes))
    both["EXECUTION_SUBSTRATE"] = "kubectl"
    assert not _io_mode_pair(tmp_path, "read_from", both=both, **changes)


def _fixed_threads(tmp_path, first, second, nodes=((1, 2, 4), (1, 2, 4))):
    """Sets for two targets whose runs used the given thread counts."""
    groups = [
        (
            f"000{index}",
            f"pool-{index}",
            _settings(f"pool-{index}", ELBENCHO_SCALE_THREAD_LIST=["4", "8"]),
            _executions(f"000{index}", nodes=count, threads=threads),
        )
        for index, (threads, count) in enumerate(zip((first, second), nodes), 1)
    ]
    return _sets(tmp_path, _document(groups))


def test_fixed_coordinates_must_match(tmp_path):
    # Same configured thread list, but different runs succeeded.
    assert not _fixed_threads(tmp_path, 4, 8)
    assert [s.dimension for s in _fixed_threads(tmp_path, 4, 4)] == [TARGET]
    # Different swept points still overlay.
    sets = _fixed_threads(tmp_path, 4, 4, nodes=((1, 2, 4), (1, 2)))
    assert [s.dimension for s in sets] == [TARGET]


def test_live_capture_options_never_block(tmp_path):
    both = {"ELBENCHO_LIVEINT": 1000, "ELBENCHO_LIVE_CSV_EXTENDED": 1}
    live = {"ELBENCHO_LIVEINT": 500, "ELBENCHO_LIVE_CSV_EXTENDED": 0}
    assert _pairs(_io_mode_pair(tmp_path, both=both, **live))


def test_swept_dimension_list_does_not_block(tmp_path):
    assert _pairs(_io_mode_pair(tmp_path, nodes_spec="1-8"))
    assert not _io_mode_pair(tmp_path, ELBENCHO_IODEPTH_LIST=["1", "4"])


def test_durations_of_runs_to_completion_are_not_noted(tmp_path):
    complete = {"run_to_completion_option": 1}
    duration = {"ELBENCHO_SCALE_READ_WRITE_DURATION": 30}
    (compare_set,) = _io_mode_pair(tmp_path, both=complete, **duration)
    assert not compare_set.durations_differ
    assert compare_set.members[0].duration_text == "run to completion"
    (compare_set,) = _io_mode_pair(tmp_path, **duration)
    assert compare_set.durations_differ


@pytest.mark.parametrize(
    "changes",
    [
        {"ELBENCHO_SCALE_THREAD_LIST": ["8", "16"]},
        {"run_to_completion_option": 1},
        {"ELBENCHO_FILE_SIZE": "4G"},
    ],
)
def test_two_differences_block_pairing(tmp_path, changes):
    test_dir = {"TEST_DIRS": {"/mnt/other/data": "host[001-004]"}}
    document = _document(
        [
            _group("0001", "pool-a"),
            _group("0002", "pool-a", **test_dir, **changes),
        ]
    )
    page = _page(tmp_path, document)
    assert 'id="comparisons"' not in page
    assert "comparison-chart" not in page


def test_one_difference_and_ignored_settings_pair(tmp_path):
    document = _document(
        [
            _group("0001", "pool-a", ELBENCHO_FILE_SIZE="1G"),
            _group("0002", "pool-a", 2.0, ELBENCHO_FILE_SIZE="2048M"),
            _group(
                "0003", "pool-a", 3.0, ELBENCHO_FILE_SIZE="4G", FS_MAX_AGG_THROUGHPUT=7
            ),
        ]
    )
    del document["groups"][2]["settings"]["nodes_spec"]  # older run: not recorded
    sets = _sets(tmp_path, document)
    assert [(s.dimension, len(s.members)) for s in sets] == [("file_size", 3)]
    assert size_bytes("2048M") == size_bytes("2G") == 2 << 30


def test_more_than_eight_members_split_evenly(tmp_path):
    groups = [_group(f"{i:04d}", f"pool-{i:02d}", float(i)) for i in range(1, 10)]
    page = _page(tmp_path, _document(groups))
    titles = [title.strip() for _, title in _CMP.findall(page)]
    assert titles == [
        "Storage target — 1 MiB seq · DirectIO (part 1 of 2)",
        "Storage target — 1 MiB seq · DirectIO (part 2 of 2)",
    ]
    anchors = [anchor for anchor, _ in _CMP.findall(page)]
    specs = _blob(page)
    names = [
        [s["name"] for s in specs[f"{anchor}-throughput-read"]["series"]]
        for anchor in anchors
    ]
    assert names == [
        [f"pool-{i:02d}" for i in range(1, 6)],
        [f"pool-{i:02d}" for i in range(6, 10)],
    ]
    assert all(len(n) <= MAX_SERIES for n in names)


def test_single_points_use_bars(tmp_path):
    groups = [
        (
            "0001",
            "pool-a",
            _settings("pool-a", nodes_spec="2"),
            _executions("0001", 1.0, (2,)),
        ),
        (
            "0002",
            "pool-b",
            _settings("pool-b", nodes_spec="2"),
            _executions("0002", 2.0, (2,)),
        ),
    ]
    page = _page(tmp_path, _document(groups))
    ((anchor, _),) = _CMP.findall(page)
    specs = _blob(page)
    throughput = specs[f"{anchor}-throughput"]
    assert throughput["kind"] == "bar"
    assert throughput["x"]["categories"] == ["pool-a", "pool-b"]
    assert [b["name"] for b in throughput["series"]] == ["write", "read"]
    assert throughput["series"][1]["values"] == [4, 8]
    assert f"{anchor}-p99" in specs
    assert f"{anchor}-share" not in specs


def test_live_overlay_uses_each_sections_best_run(tmp_path):
    live = docs.live_object(docs.client_names(4), 20, None)
    groups = [
        ("0001", "pool-a", _settings("pool-a"), _executions("0001", live=live)),
        ("0002", "pool-b", _settings("pool-b"), _executions("0002", 2.0)),
    ]
    specs = _blob(_page(tmp_path, _document(groups)))
    overlay = [spec for name, spec in specs.items() if name.endswith("-live-write")]
    assert len(overlay) == 1
    assert [s["name"] for s in overlay[0]["series"]] == ["pool-a · 4n·8t·qd1"]
    assert not [name for name in specs if name.endswith("-live-read")]


def _blocked_pair(tmp_path):
    """Two groups that differ in two settings, written as a document file."""
    document = _document(
        [
            _group("0001", "pool-a"),
            _group("0002", "pool-a", ELBENCHO_SCALE_THREAD_LIST=["8", "16"]),
        ]
    )
    path = tmp_path / "results.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_document(path), path


def test_comparison_file_forces_a_pair(tmp_path):
    loaded, path = _blocked_pair(tmp_path)
    assert not comparison_sets([loaded], group_contexts([loaded]))
    spec = tmp_path / "compare.yaml"
    spec.write_text(
        f"- title: Few vs many threads\n  sections: ['1:0001', '{path}:2:{SHAPE}']\n",
        encoding="utf-8",
    )
    page = render_report([loaded], load_comparison_file(spec))
    ((anchor, title),) = _CMP.findall(page)
    assert anchor.startswith("cmp-few-vs-many-threads-")
    assert title.strip() == "Few vs many threads"
    assert "Chosen by the comparison file:" in page
    names = [s["name"] for s in _blob(page)[f"{anchor}-throughput-read"]["series"]]
    assert names == ["g0001", "g0002"]


def test_forced_set_replaces_the_same_automatic_one(tmp_path):
    document = _document([_group("0001", "pool-a"), _group("0002", "pool-b")])
    forced = [{"title": "Pools", "sections": ["1:0002", "1:0001"]}]
    sets = _sets(tmp_path, document, comparisons=forced)
    assert [(s.title, s.dimension) for s in sets] == [("Pools", None)]
    assert _sets(tmp_path, document)[0].dimension == TARGET


@pytest.mark.parametrize(
    "data, message",
    [
        ({"title": "x"}, "expected a list of {title, sections} entries"),
        (["x"], "entry 1: expected a mapping"),
        (
            [{"title": "x", "sections": ["1:1", "1:2"], "kind": 1}],
            "unknown key(s): kind",
        ),
        ([{"title": " ", "sections": ["1:1", "1:2"]}], "'title' must be a non-empty"),
        ([{"title": "x", "sections": ["1:1"]}], "at least two sections"),
        ([{"title": "x", "sections": ["1:1", 2]}], "section 2 is not"),
        ([{"title": "x", "sections": ["1:1", "1:1"]}], "lists a section twice"),
        (
            [{"title": "x", "sections": ["1:0001", "3:0002"]}],
            "no such document (1 loaded",
        ),
        (
            [{"title": "x", "sections": ["1:0001", "1:0009"]}],
            "group 0009 of document 1 has no",
        ),
        (
            [{"title": "x", "sections": ["1:0001", "1:0002:4k-rand-dio"]}],
            f"has no section '4k-rand-dio'; available: {SHAPE}",
        ),
        ([{"title": "x", "sections": ["1:0001", "1:0002:a:b"]}], "is not '<doc>"),
    ],
)
def test_comparison_file_errors(tmp_path, data, message):
    loaded, _ = _blocked_pair(tmp_path)
    with pytest.raises(ComparisonFileError, match=re.escape(message)):
        render_report([loaded], data)


def test_comparison_file_needs_a_shape_when_a_group_has_several(tmp_path):
    group_id, target, settings, executions = _group("0001", "pool-a")
    executions += _executions(group_id, 0.5, direct=False)
    document = _document(
        [(group_id, target, settings, executions), _group("0002", "pool-b")]
    )
    forced = [{"title": "x", "sections": ["1:0001", "1:0002"]}]
    with pytest.raises(ComparisonFileError, match="add one of: 1m-seq-dio, 1m-seq-bio"):
        _page(tmp_path, document, comparisons=forced)


def test_comparison_file_rejects_mixed_kinds(tmp_path):
    document = _document([_group("0001", "pool-a")])
    document["groups"].append(docs.group("0002", "mdtest", "pool-a"))
    document["executions"].append(docs.md_execution("0201", 1, 8, 100.0, "0002"))
    forced = [{"title": "x", "sections": ["1:1", "1:2"]}]
    with pytest.raises(ComparisonFileError, match="mixes IO and metadata"):
        _page(tmp_path, document, comparisons=forced)


def test_unquoted_section_references_fail_clearly(tmp_path):
    path = tmp_path / "comparisons.yaml"
    path.write_text("- title: x\n  sections: [1:3, '1:4']\n", encoding="utf-8")
    with pytest.raises(ComparisonFileError, match="section 63 is not a string; quote"):
        load_comparison_file(path)


def test_kept_executions_decide_comparisons(tmp_path):
    document = _document([_group("0001", "pool-a"), _group("0002", "pool-b")])
    loaded = _loaded(tmp_path, document)
    assert _CMP.findall(render_report(loaded))
    page = render_report(
        loaded, keep_execution=lambda group, _: group["group_id"] == "0001"
    )
    assert not _CMP.findall(page)
    assert 'id="g0002-1m-seq-dio"' not in page


def test_comparison_file_read_errors(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("- title: [unclosed\n", encoding="utf-8")
    with pytest.raises(ComparisonFileError, match="not valid YAML"):
        load_comparison_file(bad)
    with pytest.raises(ComparisonFileError, match="missing.yaml"):
        load_comparison_file(tmp_path / "missing.yaml")
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_comparison_file(empty) == []


def test_metadata_sections_pair(tmp_path):
    groups = [
        docs.group("0001", "mdtest", "pool-a"),
        docs.group("0002", "mdtest", "pool-b"),
    ]
    executions = [
        docs.md_execution(f"0{g}{n}", n, 8, 100.0 * n * g, f"000{g}")
        for g in (1, 2)
        for n in (1, 2)
    ]
    specs = _blob(_page(tmp_path, docs.document(groups, executions)))
    rates = [name for name in specs if name.startswith("cmp-") and "-rate-" in name]
    assert sorted(rates)[0].endswith("-rate-create")
    assert len(rates) == 3
    create = next(specs[n] for n in rates if n.endswith("create"))
    assert [s["name"] for s in create["series"]] == ["pool-a", "pool-b"]
    assert create["series"][1]["points"] == [[1, 200], [2, 400]]


def _duration_document(buffered_settings: dict, direct_settings=None):
    """A direct group and a buffered group that may differ in duration."""
    direct = _settings("pool-a", **(direct_settings or {}))
    buffered = _settings("pool-a", dio_or_bio="bio", **buffered_settings)
    return _document(
        [
            ("0001", "pool-a", direct, _executions("0001")),
            ("0002", "pool-a", buffered, _executions("0002", 0.5, direct=False)),
        ]
    )


def _lead(page: str, anchor: str) -> str:
    """Text of a comparison block's lead paragraph, without the links."""
    return re.sub(r"<[^>]+>", "", _members(page, anchor))


def _drop_duration_setting(document: dict) -> None:
    for group in document["groups"]:
        del group["settings"]["ELBENCHO_SCALE_READ_WRITE_DURATION"]


def test_duration_differences_pair_and_are_labelled(tmp_path):
    document = _duration_document(
        {"ELBENCHO_SCALE_READ_WRITE_DURATION": "3m"},
        {"ELBENCHO_SCALE_READ_WRITE_DURATION": 300},
    )
    page = _page(tmp_path, document)
    ((anchor, _),) = _CMP.findall(page)
    assert anchor.startswith("cmp-io-mode-")
    lead = _lead(page, anchor)
    assert "Durations differ: 300 s (DirectIO), 180 s (Buffered)." in lead
    members = _members(page, anchor)
    assert ">DirectIO · 300 s (g0001)</a>" in members
    assert ">Buffered · 180 s (g0002)</a>" in members
    specs = [s for name, s in _blob(page).items() if name.startswith(anchor)]
    assert specs
    for spec in specs:
        names = {series["name"] for series in spec["series"]}
        assert names <= {"DirectIO · 300 s", "Buffered · 180 s"}
    block = page.split(f'<div class="cmp" id="{anchor}">')[1]
    captions = re.findall(r'<p class="cap">([^<]*)</p>', block)
    assert captions[0].endswith("Durations differ; see series labels.")


def test_equal_durations_show_nothing_extra(tmp_path):
    page = _page(tmp_path, _duration_document({}))
    ((anchor, _),) = _CMP.findall(page)
    assert _lead(page, anchor).startswith("Differs only in IO mode:")
    assert "uration" not in _members(page, anchor)
    assert "Durations differ" not in page
    names = [s["name"] for s in _blob(page)[f"{anchor}-throughput-read"]["series"]]
    assert names == ["DirectIO", "Buffered"]


def test_duration_falls_back_to_phase_time_limits(tmp_path):
    document = _duration_document({})
    _drop_duration_setting(document)
    for execution in document["executions"]:
        if execution["group_id"] == "0002":
            for phase in execution["results"]["phases"]:
                phase["configured_time_limit_s"] = 120
    page = _page(tmp_path, document)
    ((anchor, _),) = _CMP.findall(page)
    assert "60 s (DirectIO), 120 s (Buffered)" in _lead(page, anchor)


def test_unknown_duration_is_labelled(tmp_path):
    document = _duration_document({})
    _drop_duration_setting(document)
    for execution in document["executions"]:
        if execution["group_id"] == "0002":
            for index, phase in enumerate(execution["results"]["phases"]):
                phase["configured_time_limit_s"] = 60 + index
    page = _page(tmp_path, document)
    ((anchor, _),) = _CMP.findall(page)
    assert "60 s (DirectIO), duration unknown (Buffered)" in _lead(page, anchor)
    assert ">Buffered · duration unknown (g0002)</a>" in _members(page, anchor)


def test_forced_comparison_notes_differing_durations(tmp_path):
    document = _document(
        [
            _group("0001", "pool-a"),
            _group("0002", "pool-a", 2.0, ELBENCHO_SCALE_READ_WRITE_DURATION=30),
        ]
    )
    forced = [{"title": "Short vs long", "sections": ["1:0001", "1:0002"]}]
    page = _page(tmp_path, document, comparisons=forced)
    anchor = _CMP.findall(page)[0][0]
    assert anchor.startswith("cmp-short-vs-long-")
    assert "Durations differ: 60 s (g0001), 30 s (g0002)." in _lead(page, anchor)
    names = [s["name"] for s in _blob(page)[f"{anchor}-throughput-read"]["series"]]
    assert names == ["g0001 · 60 s", "g0002 · 30 s"]
    assert "Durations differ; see series labels." in page


def test_forced_comparison_with_equal_durations_is_unchanged(tmp_path):
    document = _document([_group("0001", "pool-a"), _group("0002", "pool-b")])
    forced = [{"title": "Pools", "sections": ["1:0001", "1:0002"]}]
    page = _page(tmp_path, document, comparisons=forced)
    anchor = _CMP.findall(page)[0][0]
    assert _lead(page, anchor).startswith("Chosen by the comparison file:")
    assert "Durations differ" not in page


def _normalized_document(target: str, normalize_to: int | None) -> dict:
    """One metadata group on ``target``, recorded with ``normalize_to``."""
    executions = [docs.md_execution(f"000{n}", n, 8, 1000.0, "0001") for n in (2, 4)]
    document = docs.document([docs.group("0001", "mdtest", target)], executions)
    document["generator"]["options"]["normalize_to"] = normalize_to
    return document


def test_metadata_pairs_need_one_normalization_basis(tmp_path):
    same = [_normalized_document(t, 1) for t in ("pool-a", "pool-b")]
    assert len(_sets(tmp_path, *same)) == 1
    mixed = [_normalized_document("pool-a", 1), _normalized_document("pool-b", 8)]
    assert not _sets(tmp_path, *mixed)
    forced = [{"title": "Bases", "sections": ["1:0001", "2:0001"]}]
    with pytest.raises(ComparisonFileError, match="different node counts"):
        _sets(tmp_path, *mixed, comparisons=forced)
