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

"""elbencho --jsonfile final results: the reader, and that extraction from a
.json gives what the .csv/.out of the same run give."""

# pylint: disable=protected-access

import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from lib.elbencho_json_results import parse_phase_results, read_phase_results
from tests import elbencho_v34_samples as samples
from tests.extract_elbencho_test_support import load_extract_elbencho_module
from tests.test_extract_mdtest_elbencho import _EXTRACT_MOD as _MDTEST
from tests.test_filesystem_results_schema import validate_results_document

_EXTRACT = load_extract_elbencho_module("extract_elbencho_json_results")
_FUNCTIONS = Path(__file__).resolve().parents[1] / "lib" / "_elbencho_functions.sh"
_HOSTS = ["c4svc1:1611", "c4svc2:1611"]


def _write(directory, stem, **contents):
    for extension, text in contents.items():
        (directory / f"{stem}.{extension}").write_text(text, encoding="utf-8")
    return str(directory / stem)


def _io_pair(tmp_path):
    """(legacy metrics, JSON metrics) of the same real run."""
    legacy, current = tmp_path / "legacy", tmp_path / "json"
    legacy.mkdir()
    current.mkdir()
    stem = samples.IO_STEM
    old = _write(legacy, stem, csv=samples.IO_CSV, out=samples.IO_OUT)
    new = _write(current, stem, json=samples.IO_JSON, out=samples.IO_OUT)
    return _EXTRACT.parse_elbencho_files(old), _EXTRACT.parse_elbencho_files(new)


# -- reader ---------------------------------------------------------------------


def test_reader_decodes_quoted_values_and_omitted_counters():
    phases = parse_phase_results(samples.IO_JSON)
    assert [phase.phase_type for phase in phases] == [
        "SYNC",
        "MKDIRS",
        "SYNC",
        "SYNC",
        "WRITE",
        "SYNC",
        "SYNC",
        "READ",
        "SYNC",
    ]
    mkdirs, write = phases[1], phases[4]
    assert mkdirs.first.iops is None and mkdirs.first.bytes_per_s is None
    assert mkdirs.io_latency is None and mkdirs.entries_latency is not None
    assert write.version == "3.4-1"
    assert write.direct_io is False and write.random_offsets is False
    assert write.file_size == 2097152 and write.block_size == 65536
    assert write.first.bytes_per_s == 7697736178.0
    assert write.last.elapsed_ms == 5
    assert set(write.io_latency.percentiles_us) == {"1", "50", "75", "99"}


def test_histogram_buckets_are_keyed_by_upper_bound_like_the_out_file():
    record = {
        "phase_type": "WRITE",
        "last_done": {
            "latency": {
                "IO": {
                    "histogram": {
                        "buckets": [
                            {"range": "0-1us", "count": "2"},
                            {"range": "23-24us", "count": "28"},
                            {"range": "1040-1056us", "count": "1"},
                            {"range": "35184372088832+us", "count": "3"},
                            {"range": "bogus", "count": "9"},
                        ]
                    }
                }
            }
        },
    }
    phase = parse_phase_results(json.dumps(record))[0]
    assert phase.io_latency.histogram_us == {
        1.0: 2,
        24.0: 28,
        1056.0: 1,
        35184372088832.0: 3,
    }
    assert phase.io_latency.min_us is None
    assert phase.first.elapsed_ms is None


def test_reader_accepts_adjacent_objects_and_blank_lines(tmp_path):
    path = tmp_path / "r.json"
    path.write_text(
        '\n{"phase_type":"WRITE"}{"phase_type":"READ"}\n\n{"phase_type":"SYNC"}\n',
        encoding="utf-8",
    )
    assert [p.phase_type for p in read_phase_results(str(path))] == [
        "WRITE",
        "READ",
        "SYNC",
    ]
    with pytest.raises(ValueError):
        parse_phase_results('{"phase_type":"WRITE"}\n[1]\n')
    with pytest.raises(ValueError):
        parse_phase_results('{"phase_type":"WRITE"\n')


def test_reader_service_groups():
    write = parse_phase_results(samples.IO_JSON)[4]
    services = write.services
    assert sorted(item.host for item in services.instances) == _HOSTS
    assert all(item.elapsed_ms is not None for item in services.instances)
    assert all(item.io_latency.max_us for item in services.instances)
    assert services.iops.min_host in _HOSTS and services.iops.max_host in _HOSTS
    assert services.bytes_per_s.min <= services.bytes_per_s.avg
    assert services.bytes_per_s.avg <= services.bytes_per_s.max

    net = parse_phase_results(samples.NETBENCH_JSON)[0]
    assert net.services is None
    assert set(net.service_groups) == {"clients", "servers"}
    assert net.first.rwmix_read_bytes_per_s is not None


@pytest.mark.parametrize(
    ("lines", "phase", "expected"),
    [
        (slice(3, 6), "WRITE", "16\t33554432\t5"),
        (slice(6, 9), "READ", "16\t33554432\t1"),
    ],
)
def test_shell_completion_parser_reads_real_phase_json(
    tmp_path, lines, phase, expected
):
    """The awk parser of shared-directory runs on v3.4-1 output with
    --svcstats services, percentiles and histograms (SYNC around the phase)."""
    path = tmp_path / "phase.json"
    path.write_text(
        "\n".join(samples.IO_JSON.splitlines()[lines]) + "\n", encoding="utf-8"
    )
    script = f'source "{_FUNCTIONS}"; _elbencho_parse_phase_json "{path}" {phase}'
    result = subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


# -- IO sweeps: .json equals .csv/.out ---------------------------------------------


def _assert_rate_close(legacy, current):
    """The CSV rounds MiB/s to whole numbers; the JSON has exact bytes/s."""
    assert abs(current - legacy) <= 1.0


def test_json_metrics_equal_csv_and_out_metrics_of_the_same_run(tmp_path):
    legacy, current = _io_pair(tmp_path)
    assert [m.operation for m in legacy] == ["WRITE", "READ"]
    assert [m.operation for m in current] == ["WRITE", "READ"]
    exact = (
        "iops",
        "iops_first",
        "iops_last",
        "min_lat_sec",
        "avg_lat_sec",
        "max_lat_sec",
        "lat_pct_1",
        "lat_pct_50",
        "lat_pct_75",
        "lat_pct_99",
        "lat_pct_found",
        "histogram",
        "time_ms_first",
        "time_ms_last",
        "phase_wall_duration_ms",
        "phase_first_duration_ms",
        "direct_io",
        "random_io",
        "file_size_bytes",
        "io_duration_sec",
        "io_size",
        "nodes",
        "threads",
        "io_depth",
        "datestamp",
    )
    for old, new in zip(legacy, current):
        for name in exact:
            assert getattr(new, name) == getattr(old, name), (old.operation, name)
        assert new.histogram and new.lat_pct_found == frozenset({"1", "50", "75", "99"})
        for name in ("throughput_mib_s", "throughput_mib_s_first"):
            _assert_rate_close(getattr(old, name), getattr(new, name))
        _assert_rate_close(old.throughput_mib_s_last, new.throughput_mib_s_last)
        # The csv module drops the quotes around the first command word.
        assert shlex.split(new.raw_command) == shlex.split(old.raw_command)
        assert shlex.split(new.command) == shlex.split(old.command)
        assert old.elbencho_version is None and old.services is None
        assert new.elbencho_version == "3.4-1"


def test_json_rates_are_exact(tmp_path):
    _, current = _io_pair(tmp_path)
    write = current[0]
    assert round(write.throughput_mib_s_first * 1048576) == 7697736178
    assert write.random_io == 0 and current[1].random_io == 1


def test_json_services_object(tmp_path):
    _, current = _io_pair(tmp_path)
    services = current[0].services
    assert sorted(item["host"] for item in services["instances"]) == _HOSTS
    instance = services["instances"][0]
    assert set(instance["latency_us"]) == {
        "min",
        "avg",
        "max",
        "p1",
        "p50",
        "p75",
        "p99",
    }
    assert isinstance(instance["elapsed_ms"], int)
    for rates in (services["ops_per_s"], services["bytes_per_s"]):
        assert set(rates) == {"min", "avg", "max", "min_host", "max_host"}
        assert rates["min"] <= rates["avg"] <= rates["max"]


def test_without_json_the_csv_and_out_are_read(tmp_path):
    legacy, _ = _io_pair(tmp_path)
    assert legacy[0].histogram and legacy[0].lat_pct_found
    assert legacy[0].throughput_mib_s_first == 7341.0


def test_unreadable_json_is_unparseable_even_with_a_csv(tmp_path):
    base = _write(
        tmp_path,
        samples.IO_STEM,
        csv=samples.IO_CSV,
        out=samples.IO_OUT,
        json="{not json",
    )
    assert not _EXTRACT.parse_elbencho_files(base)


def test_document_records_version_services_and_exact_rates(tmp_path, monkeypatch):
    monkeypatch.setattr(_EXTRACT, "plot_metrics", lambda *_args: None)
    monkeypatch.setattr(_EXTRACT, "print_terminal_table", lambda *_a, **_k: None)
    result = tmp_path / "elbencho-20261124Z000000"
    result.mkdir()
    (result / "env_used.yaml").write_text("EXECUTION_SUBSTRATE: ssh\n", "utf-8")
    _write(result, samples.IO_STEM, json=samples.IO_JSON, out=samples.IO_OUT)
    output = tmp_path / "report"
    monkeypatch.setattr(
        sys,
        "argv",
        ["extract-elbencho.py", str(result), "--output-dir", str(output)],
    )
    _EXTRACT.main()
    document = json.loads((output / "results.json").read_text(encoding="utf-8"))
    validate_results_document(document)

    (execution,) = document["executions"]
    assert execution["elbencho_version"] == "3.4-1"
    assert [f["path"].rsplit(".", 1)[1] for f in execution["result_files"]] == [
        "json",
        "out",
    ]
    write = execution["results"]["phases"][0]
    assert write["elbencho_first"]["bytes_per_s"] == 7697736178
    assert write["latency_us"]["p99"] is not None
    assert sorted(i["host"] for i in write["services"]["instances"]) == _HOSTS

    metrics, _, _ = _EXTRACT.document_io_metrics(
        document, document["groups"], _EXTRACT.ReportFilters()
    )
    metric = next(item for item in metrics if item.operation == "WRITE")
    assert metric.services == write["services"]
    assert metric.elbencho_version == "3.4-1"


# -- mdtest: .json equals .csv/.out --------------------------------------------------


def test_mdtest_json_equals_csv_and_out(tmp_path):
    stem = samples.MDTEST_STEM
    base = _write(
        tmp_path,
        stem,
        csv=samples.MDTEST_CSV,
        out=samples.MDTEST_OUT,
        json=samples.MDTEST_JSON,
    )
    rows = _MDTEST.parse_csv_file(base + ".csv")
    histograms = _MDTEST.parse_out_file(base + ".out")
    json_rows, json_histograms, version = _MDTEST.parse_json_file(base + ".json")

    assert version == "3.4-1"
    assert set(json_rows) == set(rows) == {"WRITE", "STAT", "RMFILES"}
    assert json_histograms == histograms
    assert all(json_histograms.values())
    for operation, row in rows.items():
        json_row = json_rows[operation]
        assert shlex.split(json_row.pop("command")) == shlex.split(row.pop("command"))
        assert json_row == row, operation
        assert row["complete"] and row["lat_valid"]


def test_mdtest_json_iteration_is_complete_without_a_csv(tmp_path):
    stem = samples.MDTEST_STEM
    _write(tmp_path, stem, json=samples.MDTEST_JSON, out=samples.MDTEST_OUT)
    (files,) = _MDTEST._scan_result_files(str(tmp_path))[(2, 2)]
    loaded = _MDTEST._load_iteration(files)
    assert loaded.reasons == []
    assert loaded.elbencho_version == "3.4-1"
    entry = _MDTEST._iteration_entry(files, loaded)
    assert entry["state"] == "complete"
    assert entry["elbencho_version"] == "3.4-1"
    assert [phase["phase"] for phase in entry["phases"]] == [
        "create",
        "stat",
        "delete",
    ]


def test_mdtest_json_missing_phase_is_incomplete(tmp_path):
    stem = samples.MDTEST_STEM
    lines = [
        line
        for line in samples.MDTEST_JSON.splitlines()
        if '"phase_type":"RMFILES"' not in line
    ]
    _write(tmp_path, stem, json="\n".join(lines) + "\n")
    (files,) = _MDTEST._scan_result_files(str(tmp_path))[(2, 2)]
    loaded = _MDTEST._load_iteration(files)
    assert loaded.reasons == [
        "missing JSON operations: RMFILES; missing JSON histograms: RMFILES"
    ]
