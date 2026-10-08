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

"""Compact saved IO configuration remains readable and faithful to its snapshot."""

import json

import pytest

from lib.filesystem_snapshot_summary import format_io_snapshot_summary


@pytest.fixture(name="snapshot")
def snapshot_fixture():
    """Representative sequential characterization snapshot with override provenance."""
    return {
        "EXECUTION_SUBSTRATE": "kubectl",
        "ORDER_NODES": "0",
        "KUBECTL_NAMESPACE": "dgxc-system",
        "KUBECTL_PVC": "grtmk04-local-pvc",
        "KUBECTL_NODE_SELECTOR": "nodeGroup=customer-cpu",
        "KUBECTL_ELBENCHO_IMAGE": "docker.io/breuner/elbencho:v3.1-11",
        "TEST_DIRS": {"scale-test": 1},
        "nodes_spec": "1",
        "dio_or_bio": "dio",
        "rand_option": 0,
        "ELBENCHO_SCALE_IO_SIZES": ["1M"],
        "ELBENCHO_SCALE_THREAD_LIST": ["1", "32", "64", "128", "256"],
        "ELBENCHO_IODEPTH_LIST": ["1", "8", "32"],
        "ELBENCHO_FILE_LAYOUT": "worker-directories",
        "ELBENCHO_FILES_PER_NODE": "",
        "ELBENCHO_FILE_SIZE": "1G",
        "ELBENCHO_FILE_SIZE_MULTIPLIER": 1024,
        "ELBENCHO_SINGLE_BIG_FILE": 0,
        "ELBENCHO_ALL_NODES_ACCESS_ALL_DATA": 0,
        "FS_MAX_AGG_THROUGHPUT": 50,
        "FS_MAX_NODE_THROUGHPUT_GBPS": 40,
        "FS_MAX_NODE_IOPS": 10000,
        "ELBENCHO_SCALE_READ_WRITE_DURATION": 60,
        "run_to_completion_option": 0,
        "sweep_write_only": 0,
        "sweep_write_no_read": 0,
        "sweep_read_from": "",
        "ELBENCHO_READ_AFTER_WRITE_PAUSE": 0,
        "ELBENCHO_LIVE_CSV_EXTENDED": 0,
        "ELBENCHO_LIVEINT": 1000,
        "env_override": {
            "file": "/a/long/location/01-sequential-characterization.env",
            "sha256": "a" * 64,
            "variables": ["ELBENCHO_FILE_SIZE", "ELBENCHO_SCALE_THREAD_LIST"],
        },
    }


def test_representative_summary_is_six_readable_lines_with_units(snapshot):
    summary = format_io_snapshot_summary(snapshot)
    lines = summary.splitlines()
    assert len(lines) == 6
    assert max(map(len, lines)) <= 132
    assert [line.split(":", 1)[0] for line in lines] == [
        "Runner",
        "Targets",
        "Sweep",
        "Files/sizing",
        "Lifecycle",
        "Override",
    ]
    for value in ("kubectl", "dgxc-system", "grtmk04-local-pvc", "scale-test", "1G"):
        assert value in summary
    assert "nodeGroup=customer-cpu" in summary
    for unit_value in ("50 GB/s", "40 Gbps", "10000 IOPS", "60 s"):
        assert unit_value in summary
    assert "01-sequential-characterization.env" in summary
    assert "/a/long/location" not in summary
    assert "a" * 64 not in summary
    assert "ELBENCHO_FILE_SIZE" not in summary
    assert "size_multiplier" not in summary
    for inactive in (
        "single_big=0",
        "read_pause=0",
        "run_to_completion=0",
        "node_order=0",
    ):
        assert inactive not in summary


@pytest.mark.parametrize("missing", [None, "absent"])
def test_missing_core_settings_remain_unknown_instead_of_current_defaults(missing):
    saved = (
        {}
        if missing == "absent"
        else {
            "ELBENCHO_FILE_LAYOUT": None,
            "ELBENCHO_FILES_PER_NODE": None,
            "ELBENCHO_FILE_SIZE": None,
            "ELBENCHO_SCALE_READ_WRITE_DURATION": None,
        }
    )
    summary = format_io_snapshot_summary(saved)
    assert len(summary.splitlines()) == 6
    assert "files/node=unknown" in summary
    assert "configured_duration=unknown" in summary
    assert "file_size=unknown" in summary
    assert "worker-directories" not in summary
    assert "1024" not in summary
    assert "auto" not in summary


def test_explicit_empty_sizing_uses_auto_count_and_recorded_multiplier(snapshot):
    snapshot.update(ELBENCHO_FILE_SIZE="", ELBENCHO_FILES_PER_NODE="")
    summary = format_io_snapshot_summary(snapshot)
    assert "files/node=auto" in summary
    assert "1024" in summary
    assert "file_size=unknown" not in summary


@pytest.mark.parametrize("value", [1, "1", True])
def test_enabled_single_file_flags_preserve_name_size_and_shared_access(
    snapshot, value
):
    snapshot.update(
        ELBENCHO_SINGLE_BIG_FILE=value,
        ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=value,
        ELBENCHO_SINGLE_BIG_FILE_BASENAME="checkpoint.data",
        ELBENCHO_SINGLE_BIG_FILE_SIZE="64G",
    )
    summary = format_io_snapshot_summary(snapshot)
    assert "single-big-file" in summary
    assert "checkpoint.data" in summary and "64G" in summary
    assert "all_nodes_all_data=1" in summary
    assert "worker-directories" not in summary
    assert "files/node" not in summary


@pytest.mark.parametrize(
    "flag,phase",
    [("sweep_write_only", "write-only"), ("sweep_write_no_read", "write-no-read")],
)
@pytest.mark.parametrize("value", [1, "1"])
def test_active_phases_and_completion_are_visible_for_numeric_or_string_flags(
    snapshot, flag, phase, value
):
    snapshot.update(
        {flag: value, "run_to_completion_option": value, "rand_option": value}
    )
    summary = format_io_snapshot_summary(snapshot)
    assert f"phases={phase}" in summary
    assert "run_to_completion=1" in summary
    assert "random_flag=1" in summary


def test_staged_reads_hide_inherited_generated_file_controls(snapshot):
    snapshot.update(
        sweep_read_from="/data/prepared tree",
        KUBECTL_MAPPED_READ_FROM="/pod/prepared tree",
        ELBENCHO_FILE_LAYOUT="shared-directory",
        ELBENCHO_FILES_PER_NODE="987654",
        ELBENCHO_FILE_SIZE="123G",
    )
    summary = format_io_snapshot_summary(snapshot)
    assert "existing-data" in summary and "phases=read-from" in summary
    assert "/data/prepared tree" in summary and "/pod/prepared tree" in summary
    for inactive in (
        "shared-directory",
        "987654",
        "123G",
        "size_multiplier",
        "hints:",
    ):
        assert inactive not in summary


@pytest.mark.parametrize("value", [1, "1"])
def test_read_from_single_big_file_retains_shared_access_without_generated_counts(
    snapshot, value
):
    snapshot.update(
        sweep_read_from="/data/checkpoint.data",
        ELBENCHO_SINGLE_BIG_FILE=value,
        ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=value,
        ELBENCHO_SINGLE_BIG_FILE_BASENAME="ignored-generated-name",
        ELBENCHO_FILES_PER_NODE="987654",
        ELBENCHO_FILE_SIZE="123G",
    )
    summary = format_io_snapshot_summary(snapshot)
    assert "single-big-file" in summary
    assert "/data/checkpoint.data" in summary
    assert "all_nodes_all_data=1" in summary
    assert "phases=read-from" in summary
    for inactive in ("ignored-generated-name", "987654", "123G", "files/node"):
        assert inactive not in summary


@pytest.mark.parametrize("substrate", ["ssh", "slurm"])
def test_non_kubernetes_runner_does_not_display_inherited_pod_settings(
    snapshot, substrate
):
    snapshot["EXECUTION_SUBSTRATE"] = substrate
    summary = format_io_snapshot_summary(snapshot)
    assert substrate in summary
    for inherited in ("dgxc-system", "grtmk04-local-pvc", "docker.io/breuner/elbencho"):
        assert inherited not in summary


@pytest.mark.parametrize("value", ["0", False])
def test_inactive_numeric_string_and_boolean_flags_have_the_same_compact_summary(
    snapshot, value
):
    expected = format_io_snapshot_summary(snapshot)
    for key in (
        "ORDER_NODES",
        "rand_option",
        "ELBENCHO_SINGLE_BIG_FILE",
        "ELBENCHO_ALL_NODES_ACCESS_ALL_DATA",
        "run_to_completion_option",
        "sweep_write_only",
        "sweep_write_no_read",
        "ELBENCHO_LIVE_CSV_EXTENDED",
    ):
        snapshot[key] = value
    assert format_io_snapshot_summary(snapshot) == expected


def test_shared_layout_count_weighted_targets_and_compound_sizes_are_unambiguous(
    snapshot,
):
    snapshot.update(
        ELBENCHO_FILE_LAYOUT="shared-directory",
        ELBENCHO_FILES_PER_NODE="8",
        ELBENCHO_FILE_SIZE="64G",
        ELBENCHO_SCALE_IO_SIZES=["1M,r4K", "r64K"],
        TEST_DIRS={"/data/one": 1, "/data/two": 3},
        KUBECTL_MAPPED_TEST_DIRS={"/pod/one": 1, "/pod/two": 3},
    )
    summary = format_io_snapshot_summary(snapshot)
    assert '"1M,r4K"' in summary and '"r64K"' in summary
    assert '"1M", "r4K"' not in summary
    assert "shared-directory" in summary and 'files/node="8"' in summary
    for target in ("/data/one", "/data/two", "/pod/one", "/pod/two"):
        assert target in summary
    assert "weight=1" in summary and "weight=3" in summary


def test_special_characters_and_long_provenance_stay_on_six_physical_lines(snapshot):
    path = '/data/a "quoted" path\nwith newline'
    snapshot.update(
        TEST_DIRS={path: 2},
        ELBENCHO_READ_AFTER_WRITE_PAUSE=5,
        ELBENCHO_LIVE_CSV_EXTENDED="1",
        ELBENCHO_LIVEINT=250,
        env_override={
            "file": "/" + "long-parent/" * 100 + "special\nname.env",
            "sha256": "b" * 64,
            "variables": ["SETTING"] * 100,
        },
    )
    summary = format_io_snapshot_summary(snapshot)
    assert len(summary.splitlines()) == 6
    assert json.dumps(path) in summary
    assert '"special\\nname.env"' in summary
    assert "long-parent" not in summary and "b" * 64 not in summary
    assert "SETTING" not in summary
    assert "5 s" in summary and "250 ms" in summary
    assert "extended_capture=1" in summary


@pytest.mark.parametrize(
    "provenance,expected", [(None, "none recorded"), ({}, "unknown")]
)
def test_explicit_null_and_incomplete_provenance_remain_distinct(
    snapshot, provenance, expected
):
    snapshot["env_override"] = provenance
    assert (
        format_io_snapshot_summary(snapshot).splitlines()[-1] == f"Override: {expected}"
    )
    del snapshot["env_override"]
    assert format_io_snapshot_summary(snapshot).splitlines()[-1] == "Override: unknown"
