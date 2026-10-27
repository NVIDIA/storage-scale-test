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

"""Helpers, writers, and assembly for the filesystem results document."""

import hashlib
import json
import math
from pathlib import Path
from unittest import mock

import pytest

from lib import filesystem_results as results
from lib.filesystem_batch import SUCCESS, read_batch_manifest, report_batch
from lib.report_cli import extract_filesystem_parser
from tests.test_filesystem_batch_reporting import _create_batch
from tests.test_filesystem_results_schema import (
    validate_results_document,
    validate_sidecar_lines,
)

GENERATOR = {
    "tool": "t",
    "version": "1",
    "generated_at": "z",
    "command": [],
    "options": {},
}


def _execution(identifier, state="complete", **extra):
    entry = {
        "execution_id": identifier,
        "coordinates": {"nodes": 1},
        "coordinate_key": f"k/{identifier}",
        "dataset": None,
        "result_state": state,
        "problems": [],
        "result_files": [
            {"path": f"f{identifier}.csv", "sha256": "0" * 64, "size_bytes": 1}
        ],
        "results": {"phases": []},
    }
    entry.update(extra)
    return entry


def _fragment(kind, *entries, diagnostics=()):
    return {
        "fragment_version": results.FRAGMENT_VERSION,
        "kind": kind,
        "executions": list(entries),
        "diagnostics": list(diagnostics),
    }


def _set_status(root, identifier, status):
    (Path(root) / "executions" / f"{identifier}.status").write_text(
        status, encoding="utf-8"
    )


def _assemble(manifest, fragments, selected=None, failed=(), live_dir=None):
    selected = manifest.groups if selected is None else selected
    return results.assemble_batch_document(
        manifest,
        selected,
        fragments,
        set(failed),
        live_dir,
        GENERATOR,
    )


def _by_id(document):
    return {item["execution_id"]: item for item in document["executions"]}


# -- helpers ---------------------------------------------------------------


def test_numeric_helpers_round_and_pass_none():
    assert results.mib_s_to_bytes_per_s(1.5) == 1572864
    assert results.mib_s_to_bytes_per_s(None) is None
    assert results.round_bytes_per_s(10.6) == 11
    assert results.round_ops(1.005 + 1) == 2.0
    assert results.round_ops(3.14159) == 3.14
    assert results.round_latency_us(1.26) == 1.3
    assert results.seconds_to_us(0.0000123) == 12.3
    for function in (
        results.round_bytes_per_s,
        results.round_ops,
        results.round_latency_us,
        results.seconds_to_us,
        results.mib_s_to_bytes_per_s,
    ):
        assert function(None) is None
        assert function(math.nan) is None


def test_histogram_columnar_sorts_and_scales():
    assert results.histogram_columnar({}) == {"buckets_us": [], "counts": []}
    assert results.histogram_columnar({0.002: 5, 0.001: 3}, 1e6) == {
        "buckets_us": [1000.0, 2000.0],
        "counts": [3, 5],
    }


def test_problem_and_file_record(tmp_path):
    assert results.problem("c", "d") == {"code": "c", "detail": "d", "phase": None}
    path = tmp_path / "a.bin"
    path.write_bytes(b"abc")
    assert results.file_record(str(path), "rel/a.bin") == {
        "path": "rel/a.bin",
        "sha256": hashlib.sha256(b"abc").hexdigest(),
        "size_bytes": 3,
    }


# -- writers ---------------------------------------------------------------


def test_write_document_is_compact_and_deterministic(tmp_path):
    document = {"b": 1, "a": ["é", None], "c": 1.5}
    plain = tmp_path / "out" / "r.json"
    results.write_document(document, str(plain))
    first = plain.read_bytes()
    assert first.decode("utf-8") == '{"b":1,"a":["é",null],"c":1.5}\n'
    results.write_document(document, str(plain))
    assert plain.read_bytes() == first
    assert [p.name for p in plain.parent.iterdir()] == ["r.json"]


def test_write_document_failure_keeps_previous_file(tmp_path):
    path = tmp_path / "r.json"
    results.write_document({"ok": True}, str(path))
    with pytest.raises(ValueError):
        results.write_document({"bad": math.nan}, str(path))
    assert json.loads(path.read_text()) == {"ok": True}
    assert [p.name for p in tmp_path.iterdir()] == ["r.json"]


def test_sidecar_writer_hashes_stored_bytes_and_is_atomic(tmp_path):
    path = tmp_path / "live" / "x.jsonl.zst"
    header = {
        "schema": results.LIVE_SCHEMA,
        "schema_version": "1.0",
        "execution_id": None,
        "phase": "read",
        "clients": ["a"],
    }
    writer = results.SidecarWriter(path, header)
    writer.write_interval(1000, [7])
    assert not path.exists()
    writer.close()
    stored = path.read_bytes()
    assert writer.size_bytes == len(stored)
    assert writer.sha256 == hashlib.sha256(stored).hexdigest()
    lines = validate_sidecar_lines(path)
    assert lines[1] == {"elapsed_ms": 1000, "bytes_per_s": [7]}
    assert [p.name for p in path.parent.iterdir()] == ["x.jsonl.zst"]


def test_sidecar_writer_abort_leaves_nothing(tmp_path):
    path = tmp_path / "x.jsonl.zst"
    with pytest.raises(RuntimeError):
        with results.SidecarWriter(path, {"h": 1}) as writer:
            writer.write_interval(1, [1])
            raise RuntimeError("boom")
    assert list(tmp_path.iterdir()) == []


def test_fragment_round_trip_and_rejection(tmp_path):
    fragment = _fragment("io", _execution("0001"))
    path = tmp_path / "f.json"
    results.write_fragment(fragment, path)
    assert results.read_fragment(path) == fragment
    path.write_text('{"fragment_version": 2, "executions": []}', encoding="utf-8")
    with pytest.raises(ValueError):
        results.read_fragment(path)


def test_generator_block_records_missing_options_as_null():
    block = results.generator_block(["a", "b"], {"live_detail": "full"})
    assert block["tool"] == results.TOOL_NAME
    assert block["command"] == ["a", "b"]
    assert block["options"]["live_detail"] == "full"
    assert block["options"]["only_nodes"] is None
    assert list(block["options"]) == list(results.GENERATOR_OPTION_KEYS)


# -- batch assembly ----------------------------------------------------------


def _five_group_batch(tmp_path):
    manifest = _create_batch(
        tmp_path / "batch", kinds=("io", "mdtest", "io", "io", "mdtest")
    )
    for identifier, status in zip(
        ("0001", "0002", "0003", "0004", "0005"),
        (SUCCESS, "FAILED", SUCCESS, SUCCESS, SUCCESS),
    ):
        _set_status(manifest.root, identifier, status)
    return read_batch_manifest(manifest.root)


def test_assembly_covers_every_state_and_validates(tmp_path):
    manifest = _five_group_batch(tmp_path)
    fragments = {
        "0001": _fragment("io", _execution("0001")),
        "0003": _fragment(
            "io",
            _execution(
                "0003",
                "partial",
                problems=[results.problem(results.PHASE_MISSING, "no read", "read")],
            ),
            diagnostics=[results.problem(results.REPORT_FAILED, "odd")],
        ),
        "0004": _fragment("io", _execution("0004", "unparseable", results=None)),
        "0005": None,
    }
    document = _assemble(manifest, fragments, failed={"0005"})
    validate_results_document(document)
    states = {
        i: (e["ledger_state"], e["result_state"]) for i, e in _by_id(document).items()
    }
    assert states == {
        "0001": ("success", "complete"),
        "0002": ("failed", "not_run"),
        "0003": ("success", "partial"),
        "0004": ("success", "unparseable"),
        "0005": ("success", "missing"),
    }
    assert _by_id(document)["0005"]["problems"][0]["code"] == results.REPORT_FAILED
    outcome = document["batch_outcome"]
    assert outcome["complete"] is False
    assert outcome["executions"] == {
        "total": 5,
        "success": 4,
        "failed": 1,
        "pending": 0,
        "running": 0,
        "cancelled": 0,
        "interrupted": 0,
        "unknown": 0,
    }
    assert outcome["results"]["complete"] == 1 and outcome["results"]["not_run"] == 1
    assert outcome["groups_reported"] == ["0001", "0003", "0004"]
    assert outcome["groups_not_reported"] == ["0002", "0005"]
    assert document["diagnostics"][0]["code"] == results.REPORT_FAILED
    assert document["diagnostics"][0]["detail"].startswith("group 0003: ")
    assert not results.document_is_complete(document)
    assert [g["group_id"] for g in document["groups"]] == [
        "0001",
        "0002",
        "0003",
        "0004",
        "0005",
    ]
    assert document["batch"]["datestamp"] == manifest.datestamp
    assert document["batch"]["manifest_sha256"] == manifest.digest
    assert document["batch"]["sealed"] is False


@pytest.mark.parametrize(
    "status, expected",
    [
        ("PENDING", "pending"),
        ("RUNNING", "running"),
        ("CANCELLED", "cancelled"),
        ("INTERRUPTED", "interrupted"),
        ("garbage", "unknown"),
        (None, "unknown"),
    ],
)
def test_every_ledger_state_is_reported_and_not_run(tmp_path, status, expected):
    manifest = _create_batch(tmp_path / "batch", kinds=("io",))
    if status is None:
        (manifest.root / "executions" / "0001.status").unlink()
    else:
        _set_status(manifest.root, "0001", status)
    document = _assemble(manifest, {"0001": None})
    execution = document["executions"][0]
    assert (execution["ledger_state"], execution["result_state"]) == (
        expected,
        "not_run",
    )
    assert document["batch_outcome"]["executions"][expected] == 1
    validate_results_document(document)


def test_executions_without_fragment_entries_carry_definition_coordinates(tmp_path):
    manifest = _create_batch(tmp_path / "batch")
    definition = manifest.root / "executions" / "0001.sh"
    definition.write_text(
        "# Auto-generated; do not edit. Coords: nodes=1 io_size=1M,r4K "
        "thread_count=1 io_depth=1\n" + definition.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    document = _assemble(manifest, {"0001": None, "0002": None, "0003": None})
    validate_results_document(document)
    by_id = _by_id(document)
    assert by_id["0001"]["coordinate_key"] == "io/c001/s001/d001/w1M-seq/r4K-rand/dio"
    assert by_id["0001"]["coordinates"]["io_size_spec"] == "1M,r4K"
    assert by_id["0002"]["coordinate_key"] == "md/c002/t004/bnone/inone"
    assert by_id["0002"]["coordinates"]["iterations"] == 1
    assert all(by_id[i]["dataset"] is None for i in by_id)
    # An IO definition without its coordinates line cannot be parsed.
    assert by_id["0003"]["coordinates"] is None
    assert by_id["0003"]["coordinate_key"] is None
    assert [p["code"] for p in by_id["0003"]["problems"]] == [
        results.DEFINITION_UNREADABLE
    ]


def test_fragment_of_unsuccessful_execution_becomes_not_run_keeping_files(tmp_path):
    manifest = _create_batch(tmp_path / "batch", kinds=("io",))
    _set_status(manifest.root, "0001", "FAILED")
    entry = _execution("0001", "complete", problems=[results.problem("x", "y")])
    execution = _assemble(manifest, {"0001": _fragment("io", entry)})["executions"][0]
    assert execution["result_state"] == "not_run"
    assert execution["results"] is None
    assert execution["result_files"] and execution["problems"]


def test_unselected_groups_are_not_reported(tmp_path):
    manifest = _five_group_batch(tmp_path)
    selected = tuple(g for g in manifest.groups if g.group_id == "0001")
    document = _assemble(
        manifest, {"0001": _fragment("io", _execution("0001"))}, selected
    )
    states = {i: e["result_state"] for i, e in _by_id(document).items()}
    assert states["0001"] == "complete"
    assert states["0002"] == states["0003"] == "not_reported"
    assert document["batch_outcome"]["results"]["not_reported"] == 4
    assert document["batch_outcome"]["complete"] is False
    validate_results_document(document)


def test_complete_batch_and_paths_are_rewritten(tmp_path):
    manifest = _create_batch(tmp_path / "batch", kinds=("io", "mdtest"))
    for identifier in ("0001", "0002"):
        _set_status(manifest.root, identifier, SUCCESS)
    live = {
        "source": {"path": "x.live.csv", "sha256": "1" * 64, "size_bytes": 5},
        "configured_interval_ms": 1000,
        "client_mode": "worker",
        "domain": {"phase": "write", "mix_type": "", "occurrence": 0},
        "intervals": 1,
        "clients": 1,
        "steady": {
            "start_ms": None,
            "end_ms": None,
            "end_source": None,
            "reason": "no_first_time",
        },
        "aggregate_series": {
            "elapsed_ms": [1],
            "bytes_per_s": [1],
            "ops_per_s": [1.0],
            "io_latency_avg_us": [None],
        },
        "fleet_series": {"elapsed_ms": [1], "client_count": [1]},
        "fairness": None,
        "latency": None,
        "distribution": {"persistent_clients": None, "transient_clients": None},
        "clients_summary": {"columns": [], "rows": []},
        "client_series": {
            "path": "0001.write.jsonl.zst",
            "sha256": "2" * 64,
            "size_bytes": 3,
            "clients": 1,
            "intervals": 1,
        },
        "diagnostics": {},
    }
    first = _execution("0001", results={"phases": [{"phase": "write", "live": live}]})
    live_dir = manifest.root / "reports" / "results.live"
    fragments = {
        "0001": _fragment("io", first),
        "0002": _fragment("mdtest", _execution("0002", results={"iterations": []})),
    }
    document = _assemble(manifest, fragments, live_dir=live_dir)
    validate_results_document(document)
    assert document["batch_outcome"]["complete"] is True
    prefix = manifest.groups[0].output_relative
    execution = _by_id(document)["0001"]
    assert execution["result_files"][0]["path"] == f"{prefix}/f0001.csv"
    rewritten = execution["results"]["phases"][0]["live"]
    assert rewritten["source"]["path"] == f"{prefix}/x.live.csv"
    assert rewritten["client_series"]["path"] == "results.live/0001.write.jsonl.zst"
    assert first["result_files"][0]["path"] == "f0001.csv"  # input untouched
    assert (
        fragments["0001"]["executions"][0]["results"]["phases"][0]["live"]["source"][
            "path"
        ]
        == "x.live.csv"
    )


def test_logs_are_located_at_batch_root_or_group_output(tmp_path):
    manifest = _create_batch(tmp_path / "batch", kinds=("io", "io"))
    (manifest.root / "executions" / "0001.log").write_text("l")
    group_two = manifest.group_path(manifest.groups[1])
    (group_two / "executions" / "0002.log").write_text("l")
    document = _assemble(manifest, {})
    logs = {i: e["log"] for i, e in _by_id(document).items()}
    assert logs == {
        "0001": "executions/0001.log",
        "0002": f"{manifest.groups[1].output_relative}/executions/0002.log",
    }


def test_unmatched_results_become_diagnostics(tmp_path):
    manifest = _create_batch(tmp_path / "batch", kinds=("io",))
    _set_status(manifest.root, "0001", SUCCESS)
    stray = _execution(None, "unparseable", results=None)
    document = _assemble(manifest, {"0001": _fragment("io", _execution("0001"), stray)})
    assert len(document["executions"]) == 1
    diagnostic = document["diagnostics"][0]
    assert diagnostic["code"] == results.EXECUTION_UNMATCHED
    assert manifest.groups[0].output_relative in diagnostic["detail"]
    validate_results_document(document)


def test_assembly_is_deterministic(tmp_path):
    manifest = _five_group_batch(tmp_path)
    fragments = {"0001": _fragment("io", _execution("0001"))}
    assert json.dumps(_assemble(manifest, fragments)) == json.dumps(
        _assemble(manifest, fragments)
    )


def test_group_settings_target_and_override(tmp_path):
    snapshot = "STORAGE_TARGET_NAME: lustre-a\nEXECUTION_SUBSTRATE: slurm\nenv_override:\n  files: []\n"
    manifest = _create_batch(
        tmp_path / "batch", kinds=("io",), snapshots={"0001": snapshot}
    )
    document = _assemble(manifest, {})
    group = document["groups"][0]
    assert group["target"] == {"name": "lustre-a", "recorded": True}
    assert group["env_override"] == {"files": []}
    assert group["settings_sha256"]["env_used_yaml"] == manifest.groups[0].yaml_digest
    assert document["batch"]["substrate"] == "slurm"


# -- ordinary directories ---------------------------------------------------


def _ordinary_dir(tmp_path, ledger):
    root = tmp_path / "elbencho-20260930Z120000"
    root.mkdir()
    (root / "env_used.yaml").write_text(
        "EXECUTION_SUBSTRATE: ssh\nTEST_ROOT: /x\n", encoding="utf-8"
    )
    if ledger:
        (root / "executions").mkdir()
        (root / "executions" / "0001.sh").write_text("export a=1\n")
        (root / "executions" / "0001.status").write_text("SUCCESS")
    return root


def test_ordinary_document_with_ledger(tmp_path):
    root = _ordinary_dir(tmp_path, ledger=True)
    fragment = _fragment("io", _execution("0001"))
    document = results.assemble_ordinary_document(root, fragment, None, GENERATOR)
    validate_results_document(document)
    assert document["batch"] == {
        "root": str(root.resolve()),
        "datestamp": "20260930Z120000",
        "manifest_version": None,
        "manifest_sha256": None,
        "sealed": None,
        "substrate": "ssh",
    }
    group = document["groups"][0]
    assert (group["group_id"], group["output_relative"], group["label"]) == (
        "0000",
        ".",
        None,
    )
    execution = document["executions"][0]
    assert execution["ledger_state"] == "success"
    assert execution["definition_sha256"] == hashlib.sha256(b"export a=1\n").hexdigest()
    assert execution["result_files"][0]["path"] == "f0001.csv"
    assert results.document_is_complete(document)


def test_ordinary_document_without_ledger_is_never_complete(tmp_path):
    root = _ordinary_dir(tmp_path, ledger=False)
    fragment = _fragment("io", _execution(None))
    document = results.assemble_ordinary_document(root, fragment, None, GENERATOR)
    validate_results_document(document)
    execution = document["executions"][0]
    assert execution["execution_id"] is None
    assert execution["ledger_state"] == "unknown"
    assert execution["result_state"] == "complete"
    assert execution["results"] is not None
    assert [d["code"] for d in document["diagnostics"]] == [results.NO_EXECUTION_LEDGER]
    assert not results.document_is_complete(document)


# -- batch integration --------------------------------------------------------


def _engine(fragment_for):
    """A fake group engine that writes the requested fragment and a report."""

    def run(manifest, group, _successful, output, options, **_kwargs):
        (output / "report.txt").write_text(group.kind, encoding="utf-8")
        values = dict(o.split("=", 1) for o in options if o.startswith("--results-"))
        assert "--to-json" not in " ".join(options)
        assert "--require-complete" not in options
        fragment = fragment_for(manifest, group)
        if fragment is not None:
            results.write_fragment(fragment, values["--results-fragment"])

    return run


def _complete_batch(tmp_path):
    manifest = _create_batch(tmp_path / "batch", kinds=("io", "mdtest"))
    for identifier in ("0001", "0002"):
        _set_status(manifest.root, identifier, SUCCESS)
    return read_batch_manifest(manifest.root)


def _full_fragments(_manifest, group):
    return _fragment(group.kind, _execution(group.group_id))


def _run(manifest, arguments, engine):
    args = extract_filesystem_parser().parse_args([str(manifest.root), *arguments])
    with mock.patch("lib.filesystem_batch._report_group", side_effect=engine):
        return report_batch(
            args.results_dir,
            args.kind,
            args.groups,
            args.output_dir,
            analysis_args=args,
        )


def test_batch_writes_default_document(tmp_path):
    manifest = _complete_batch(tmp_path)
    assert (
        _run(manifest, ["--to-json", "--require-complete"], _engine(_full_fragments))
        == 0
    )
    document = json.loads((manifest.root / "reports" / "results.json").read_text())
    validate_results_document(document)
    assert results.document_is_complete(document)
    assert document["generator"]["options"]["live_detail"] == "summary"


def test_batch_forwards_fragment_and_live_dir_options(tmp_path):
    manifest = _complete_batch(tmp_path)
    seen = {}

    def engine(_manifest, group, _successful, output, options):
        seen[group.kind] = options
        (output / "report.txt").write_text("x", encoding="utf-8")
        results.write_fragment(
            _full_fragments(None, group), output / "results.fragment.json"
        )

    _run(manifest, ["--to-json", "--live-detail", "full"], engine)
    reports = manifest.root / "reports"
    assert seen["io"] == [
        "--live-detail=full",
        f"--results-fragment={reports / 'groups' / '0001' / 'results.fragment.json'}",
        f"--results-live-dir={reports / 'results.live'}",
    ]
    assert seen["mdtest"] == [
        f"--results-fragment={reports / 'groups' / '0002' / 'results.fragment.json'}"
    ]


def test_batch_document_follows_output_dir(tmp_path):
    manifest = _complete_batch(tmp_path)
    output = tmp_path / "elsewhere"
    arguments = ["--to-json", "--output-dir", str(output)]
    assert _run(manifest, arguments, _engine(_full_fragments)) == 0
    validate_results_document(json.loads((output / "results.json").read_text()))
    assert not (manifest.root / "reports" / "results.json").exists()


def test_batch_to_json_takes_no_path(tmp_path):
    manifest = _complete_batch(tmp_path)
    with pytest.raises(SystemExit):
        _run(manifest, [f"--to-json={tmp_path / 'doc.json'}"], _engine(_full_fragments))


def test_require_complete_exit_status_and_precedence(tmp_path):
    manifest = _complete_batch(tmp_path)
    partial = lambda _m, g: _fragment(g.kind, _execution(g.group_id, "partial"))
    assert _run(manifest, ["--to-json"], _engine(partial)) == 0
    assert _run(manifest, ["--to-json", "--require-complete"], _engine(partial)) == 3
    document = json.loads((manifest.root / "reports" / "results.json").read_text())
    assert not results.document_is_complete(document)
    # A reporting failure (1) wins over incomplete (3).
    engine = mock.Mock(side_effect=ValueError("bad"))
    assert _run(manifest, ["--to-json", "--require-complete"], engine) == 1
    document = json.loads((manifest.root / "reports" / "results.json").read_text())
    assert {e["problems"][0]["code"] for e in document["executions"]} == {
        results.REPORT_FAILED
    }


def test_pending_batch_writes_document_and_exits_by_completeness(tmp_path):
    manifest = _create_batch(tmp_path / "batch", kinds=("io", "mdtest"))
    engine = mock.Mock(side_effect=AssertionError("no group has results"))
    assert _run(manifest, ["--to-json"], engine) == 0
    assert _run(manifest, ["--to-json", "--require-complete"], engine) == 3
    document = json.loads((manifest.root / "reports" / "results.json").read_text())
    validate_results_document(document)
    assert document["batch_outcome"]["executions"]["pending"] == 2
    assert not results.document_is_complete(document)
    # Without a document, an empty report is still a failure.
    assert _run(manifest, [], engine) == 1


def test_missing_fragment_from_successful_engine_fails_the_run(tmp_path):
    manifest = _complete_batch(tmp_path)
    assert _run(manifest, ["--to-json"], _engine(lambda *_: None)) == 1


def test_stale_fragment_is_not_reused(tmp_path):
    manifest = _complete_batch(tmp_path)
    stale = manifest.root / "reports" / "groups" / "0001"
    stale.mkdir(parents=True)
    (stale / "results.fragment.json").write_text("{}")
    assert (
        _run(
            manifest,
            ["--to-json"],
            _engine(
                lambda m, g: None if g.group_id == "0001" else _full_fragments(m, g)
            ),
        )
        == 1
    )


def test_require_complete_without_to_json_is_a_usage_error(tmp_path):
    manifest = _complete_batch(tmp_path)
    args = extract_filesystem_parser().parse_args(
        [str(manifest.root), "--require-complete"]
    )
    with pytest.raises(ValueError, match="requires --to-json"):
        report_batch(args.results_dir, analysis_args=args)


def test_without_to_json_nothing_changes(tmp_path):
    manifest = _complete_batch(tmp_path)
    seen = []

    def engine(_m, _group, _s, output, options):
        seen.append(options)
        (output / "report.txt").write_text("x", encoding="utf-8")

    assert _run(manifest, [], engine) == 0
    assert seen == [[], []]
    assert not (manifest.root / "reports" / "results.json").exists()
