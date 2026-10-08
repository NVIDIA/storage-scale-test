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

"""Reporters print the versions recorded beside the results they report."""

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

from tests.extract_elbencho_test_support import install_extract_heavy_dependency_stubs
from tests.test_extract_elbencho_cli_contracts import _EXTRACT, _metric

_UTILS = Path(__file__).resolve().parents[1] / "utils"
_DATESTAMP = "20260101Z000000"


def _load(script):
    install_extract_heavy_dependency_stubs()
    spec = importlib.util.spec_from_file_location(
        f"report_versions_{script.replace('-', '_')}", _UTILS / f"{script}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _results(directory, versions, name):
    """Write empty result pairs, with a version file where one is given."""
    for nodes, version in versions.items():
        stem = directory / name.format(nodes=nodes)
        stem.with_name(stem.name + ".out").write_text("", encoding="utf-8")
        stem.with_name(stem.name + ".csv").write_text("", encoding="utf-8")
        if version:
            stem.with_name(stem.name + ".out.project-version").write_text(
                version + "\n", encoding="utf-8"
            )


def _header(output):
    return [line for line in output.splitlines() if "by storage-scale-test" in line]


def test_elbencho_report_lists_versions_of_selected_results(
    monkeypatch, tmp_path, capsys
):
    _results(
        tmp_path,
        {1: "v1.10.0", 2: "v1.9.0", 4: None},
        f"elbencho-4K-c_{{nodes:03d}}-s_001-d_001_{_DATESTAMP}",
    )
    nodes_of = {f"c_{nodes:03d}": nodes for nodes in (1, 2, 4)}

    def parse(base):
        return [replace(_metric(), nodes=nodes_of[Path(base).name.split("-")[2]])]

    monkeypatch.setattr(_EXTRACT, "parse_elbencho_files", parse)
    monkeypatch.setattr(_EXTRACT, "print_markdown_table", lambda *_args: None)
    monkeypatch.setattr(_EXTRACT, "plot_metrics", lambda *_args: None)
    monkeypatch.setattr(
        sys, "argv", ["extract-elbencho.py", str(tmp_path), "--markdown"]
    )
    _EXTRACT.main()
    monkeypatch.setattr(
        sys,
        "argv",
        ["extract-elbencho.py", str(tmp_path), "--markdown", "--only-nodes", "1,4"],
    )
    _EXTRACT.main()
    produced = [line for line in _header(capsys.readouterr().out) if "Produced" in line]
    assert produced == [
        "- Produced by storage-scale-test: unknown, v1.9.0, v1.10.0",
        "- Produced by storage-scale-test: unknown, v1.10.0",
    ]


def test_warp_report_lists_versions(monkeypatch, tmp_path, capsys):
    warp = _load("extract-warp")
    for nodes, version in ((1, "v1.2.3"), (2, None)):
        out = tmp_path / f"warp-GET-1MiB-c_{nodes:03d}-s_004_{_DATESTAMP}.out"
        out.write_text("", encoding="utf-8")
        if version:
            out.with_name(out.name + ".project-version").write_text(version)

    def parse(path):
        return SimpleNamespace(nodes=int(path.split("-c_")[1][:3]))

    monkeypatch.setattr(warp, "parse_warp_file", parse)
    monkeypatch.setattr(warp, "plot_metrics", lambda *_args: None)
    monkeypatch.setattr(warp, "print_table", lambda *_args: None)
    monkeypatch.setattr(sys, "argv", ["extract-warp.py", str(tmp_path)])
    warp.main()
    assert _header(capsys.readouterr().out)[0] == (
        "Produced by storage-scale-test: unknown, v1.2.3"
    )


def test_netbench_report_lists_versions_of_selected_groups(
    monkeypatch, tmp_path, capsys
):
    netbench = _load("extract-netbench")
    _results(
        tmp_path,
        {1: "v1.2.3", 2: "v2.0.0"},
        f"netbench-half-c_{{nodes:03d}}-t_004_{_DATESTAMP}_iter1_AtoB",
    )

    def parse(_csv, _out, mode, nodes, threads, *_rest):
        return SimpleNamespace(mode=mode, node_count=nodes, thread_count=threads)

    def aggregate(iterations):
        return {(it.mode, it.node_count, it.thread_count): it for it in iterations}

    monkeypatch.setattr(netbench, "parse_file_pair", parse)
    monkeypatch.setattr(netbench, "aggregate_metrics", aggregate)
    monkeypatch.setattr(netbench, "print_terminal_table", lambda *_args: None)
    monkeypatch.setattr(netbench, "plot_all", lambda *_args: None)
    monkeypatch.setattr(
        sys, "argv", ["extract-netbench.py", str(tmp_path), "--only-nodes", "2"]
    )
    netbench.main()
    assert _header(capsys.readouterr().out)[0] == (
        "Produced by storage-scale-test: v2.0.0"
    )


def test_warp_report_lists_versions_of_unfiltered_detail_tables(
    monkeypatch, tmp_path, capsys
):
    """--only-sizes filters only the summary; detail tables show every size."""
    warp = _load("extract-warp")
    for size, version in (("1MiB", "v1.0.0"), ("2MiB", "v2.0.0")):
        out = tmp_path / f"warp-GET-{size}-c_001-s_004_{_DATESTAMP}.out"
        out.write_text("", encoding="utf-8")
        out.with_name(out.name + ".project-version").write_text(version)

    def parse(path):
        return SimpleNamespace(
            nodes=1, threads=4, obj_size=Path(path).name.split("-")[2]
        )

    reported = []
    monkeypatch.setattr(warp, "parse_warp_file", parse)
    monkeypatch.setattr(warp, "plot_metrics", lambda *_args: None)
    monkeypatch.setattr(
        warp, "print_table", lambda metrics, *_args: reported.extend(metrics)
    )
    monkeypatch.setattr(
        sys, "argv", ["extract-warp.py", str(tmp_path), "--only-sizes", "1MiB"]
    )
    warp.main()
    assert sorted(m.obj_size for m in reported) == ["1MiB", "2MiB"]
    assert _header(capsys.readouterr().out)[0] == (
        "Produced by storage-scale-test: v1.0.0, v2.0.0"
    )
