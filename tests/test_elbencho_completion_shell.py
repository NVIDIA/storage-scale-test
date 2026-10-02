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


"""Fast finite-dataset sizing, phase-policy, and native-completion tests."""

import json
import platform
import shlex
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_FUNCTIONS = _ROOT / "lib/_elbencho_functions.sh"
_BINARY = _ROOT / "utils/elbencho.aarch64"


def _run(script):
    return subprocess.run(
        ["bash", "-c", script],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _harness(
    root, *, access="dio", counts="explicit", mode="default", run_to_completion=1
):
    second = ",$root/second" if counts in {"weighted", "explicit-weighted"} else ""
    files = "8" if counts in {"explicit", "explicit-weighted"} else ""
    return f"""
set -e
source {shlex.quote(str(_FUNCTIONS))}
root={shlex.quote(str(root))}
mkdir -p "$root/elbencho-DS"
output_dir="$root/elbencho-DS"
CAPTURE="$root/calls"
test_dirs_csv="$root/target{second}"
io_size=4K thread_count=2 io_depth=1 dio_or_bio={access} use_random=0
run_to_completion={run_to_completion}
ELBENCHO_FILE_SIZE=4K
ELBENCHO_FILES_PER_NODE={files}
ELBENCHO_FILE_LAYOUT=worker-directories
ELBENCHO_LIVE_CSV_EXTENDED=0
ELBENCHO_SINGLE_BIG_FILE=0
ELBENCHO_SCALE_READ_WRITE_DURATION=999
ELBENCHO_SWEEP_READ_FROM=
ELBENCHO_SWEEP_WRITE_ONLY={int(mode == "write-only")}
ELBENCHO_SWEEP_WRITE_NO_READ={int(mode == "write-no-read")}
_elbencho_resolve_run_context() {{ node_count=1; remote_output_dir="$output_dir"; hosts_csv=; }}
compute_target_file_count_per_thread() {{ printf '3\\n'; }}
_elbencho_cleanup_stale_many_files_sweep_test_dirs() {{ return 0; }}
_elbencho_cleanup_many_files_sweep_test_dirs() {{ printf 'cleanup\\n' >>"$CAPTURE"; }}
_elbencho_run_service_health_hook() {{ return 0; }}
_elbencho_maybe_pause_before_read() {{ return 0; }}
run_an_elbencho() {{
    printf '%s\\n' "$*" >>"$CAPTURE"
    [[ "$*" != *--write* || "${{FAIL_WRITE:-0}}" == 0 ]] || return "$FAIL_WRITE"
}}
"""


@pytest.mark.parametrize("access", ["dio", "bio"])
@pytest.mark.parametrize(
    "counts", ["explicit", "explicit-weighted", "automatic", "weighted"]
)
@pytest.mark.parametrize("mode", ["default", "write-only", "write-no-read"])
def test_run_to_completion_phases_have_finite_counts_and_no_repeat_or_deadline(
    tmp_path, access, counts, mode
):
    result = _run(
        _harness(tmp_path, access=access, counts=counts, mode=mode)
        + "run_elbencho_io_sweep_iteration\n"
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    phases = [line for line in calls if line.startswith("--")]
    assert len(phases) == (3 if mode == "default" else 2)
    expected = {"explicit": 4, "explicit-weighted": 2}.get(counts, 3)
    for line in phases:
        assert f"--files={expected}" in line
        assert "--size=4K" in line
        assert "--timelimit" not in line
        assert "--infloop" not in line
        assert f"--dirs={2 if 'weighted' in counts else 1}" in line
    assert ("cleanup" in calls) == (mode != "write-only")


@pytest.mark.parametrize("access", ["dio", "bio"])
def test_saved_timed_mode_keeps_its_original_read_policy(tmp_path, access):
    result = _run(
        _harness(tmp_path, access=access, counts="automatic", run_to_completion=0)
        + "run_elbencho_io_sweep_iteration\n"
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    write = next(line for line in calls if line.startswith("--write"))
    read = next(line for line in calls if "--read " in line)
    assert "--timelimit=999" in write and "--infloop" in write
    assert "--timelimit=999" in read
    assert ("--infloop" in read) == (access == "dio")


@pytest.mark.parametrize(
    "access,explicit,roots,expected",
    [
        ("dio", 0, "[/one]=1", "0"),
        ("dio", 1, "[/one]=1", "1"),
        ("bio", 0, "[/one]=1", "1"),
        ("bio", 1, "[/one]=1", "1"),
        ("dio", 0, "[/one]=1 [/two]=1", "1"),
        ("dio", 0, "[/one]=2 [/two]=1", "1"),
        ("bio", 0, "[/one]=2 [/two]=1", "1"),
        ("dio", 0, "[/one]=2", "0"),
    ],
)
def test_completion_mode_resolves_access_flag_and_distinct_roots(
    access, explicit, roots, expected
):
    result = _run(f"""
set -e
source {shlex.quote(str(_FUNCTIONS))}
declare -A TEST_DIRS=({roots})
resolve_elbencho_completion_mode {access} {explicit}
""")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


@pytest.mark.parametrize(
    "access,roots",
    [("bio", "[/one]=1"), ("dio", "[/one]=2 [/two]=1")],
)
def test_implicit_completion_allows_explicit_dataset_budget(access, roots):
    result = _run(f"""
set -e
source {shlex.quote(str(_FUNCTIONS))}
declare -A TEST_DIRS=({roots})
ELBENCHO_FILE_LAYOUT=worker-directories ELBENCHO_FILES_PER_NODE=7
ELBENCHO_FILE_SIZE=4K ELBENCHO_SCALE_THREAD_LIST=(2 4)
ELBENCHO_SCALE_IO_SIZES=(4K) ELBENCHO_SINGLE_BIG_FILE=0
completion=$(resolve_elbencho_completion_mode {access} 0)
validate_elbencho_sweep_workload_mode {access} 0 '' "$completion"
""")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "access,counts,roots",
    [
        ("bio", "explicit", "[/one]=1"),
        ("dio", "explicit-weighted", "[/one]=1 [/two]=1"),
        ("dio", "weighted", "[/one]=2 [/two]=1"),
    ],
)
def test_implicit_completion_runs_finite_write_and_read_phases(
    tmp_path, access, counts, roots
):
    result = _run(
        _harness(tmp_path, access=access, counts=counts, run_to_completion=0) + f"""
declare -A TEST_DIRS=({roots})
run_to_completion=$(resolve_elbencho_completion_mode {access} 0)
run_elbencho_io_sweep_iteration
"""
    )
    assert result.returncode == 0, result.stderr
    phases = [
        line
        for line in (tmp_path / "calls").read_text().splitlines()
        if line.startswith("--")
    ]
    assert len(phases) == 3
    assert any("--write" in phase for phase in phases)
    assert any("--read" in phase for phase in phases)
    for phase in phases:
        assert "--files=" in phase and "--size=4K" in phase
        assert "--timelimit" not in phase and "--infloop" not in phase


def test_run_to_completion_preserves_write_failure_and_skips_read(tmp_path):
    result = _run(
        _harness(tmp_path) + "FAIL_WRITE=17\nrun_elbencho_io_sweep_iteration\n"
    )
    assert result.returncode == 17
    calls = (tmp_path / "calls").read_text()
    assert "--read " not in calls
    assert "cleanup" in calls


def test_automatic_count_uses_effective_block_when_file_is_smaller_than_block():
    result = _run(f"""
set -e
source {shlex.quote(str(_FUNCTIONS))}
FS_MAX_NODE_THROUGHPUT_GBPS=1 FS_MAX_AGG_THROUGHPUT=1 FS_MAX_NODE_IOPS=8
compute_target_file_count_per_thread 1M 4K 1 2 1
""")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "4"


def test_automatic_count_budget_includes_all_weighted_targets():
    result = _run(f"""
set -e
source {shlex.quote(str(_FUNCTIONS))}
FS_MAX_NODE_THROUGHPUT_GBPS=1 FS_MAX_AGG_THROUGHPUT=1 FS_MAX_NODE_IOPS=24
compute_target_file_count_per_thread 4K 4K 1 2 1 3
""")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "4"


def test_automatic_count_does_not_overflow_the_weighted_divisor():
    result = _run(f"""
set -e
source {shlex.quote(str(_FUNCTIONS))}
FS_MAX_NODE_THROUGHPUT_GBPS=1 FS_MAX_AGG_THROUGHPUT=1 FS_MAX_NODE_IOPS=24
compute_target_file_count_per_thread 4K 4K 1 2 1 4611686018427387904
""")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


@pytest.mark.parametrize(
    "requested,threads,targets,expected",
    [
        (8, 2, 1, 4),
        (7, 2, 1, 4),
        (7, "02", 1, 4),
        (1000, 16, 3, 21),
        (14, 2, 3, 2),
        (15, 2, 3, 3),
        (1, 64, 2, 1),
        (2**63 - 1, 1, 1, 2**63 - 1),
        (2**63 - 1, 2, 1, 2**62),
    ],
)
def test_per_node_budget_rounds_once_across_threads_and_weighted_targets(
    requested, threads, targets, expected
):
    result = _run(f"""
source {shlex.quote(str(_FUNCTIONS))}
_elbencho_run_to_completion_files_per_worker {requested} {threads} {targets}
""")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(expected)


@pytest.mark.parametrize("access", ["dio", "bio"])
def test_staged_run_to_completion_uses_same_read_policy_without_changing_extent(access):
    result = _run(f"""
set -e
source {shlex.quote(str(_FUNCTIONS))}
dio_or_bio={access} run_to_completion=1 node_count=1
ELBENCHO_SCALE_READ_WRITE_DURATION=999
args=()
_elbencho_staged_build_read_args args 4K /cache cache_hit /dataset 0 --threads=2
printf '%s\\n' "${{args[@]}}"
""")
    assert result.returncode == 0, result.stderr
    assert "--treefile\n/cache" in result.stdout
    assert "--timelimit" not in result.stdout and "--infloop" not in result.stdout
    assert "--size" not in result.stdout


def test_staged_buffered_read_implicitly_completes_existing_dataset():
    result = _run(f"""
set -e
source {shlex.quote(str(_FUNCTIONS))}
declare -A TEST_DIRS=([/one]=1)
dio_or_bio=bio node_count=1 ELBENCHO_SCALE_READ_WRITE_DURATION=999
run_to_completion=$(resolve_elbencho_completion_mode "$dio_or_bio" 0)
args=()
_elbencho_staged_build_read_args args 4K /cache cache_hit /dataset 0 --threads=2
printf '%s\\n' "${{args[@]}}"
""")
    assert result.returncode == 0, result.stderr
    assert "--treefile\n/cache" in result.stdout
    assert "--timelimit" not in result.stdout and "--infloop" not in result.stdout
    assert "--size" not in result.stdout


@pytest.mark.parametrize(
    "termination,limit,completion",
    [
        ("completion", "null", "not_applicable"),
        ("time_bounded_repeat", "999", "not_applicable_time_based"),
    ],
)
def test_staged_workload_metadata_matches_the_effective_read_policy(
    termination, limit, completion
):
    result = _run(f"""
set -e
source {shlex.quote(str(_FUNCTIONS))}
node_count=2 thread_count=4 io_depth=1
ELBENCHO_SCALE_READ_WRITE_DURATION=999
_elbencho_workload_begin() {{ return 0; }}
_elbencho_workload_write() {{ return 0; }}
_elbencho_workload_set() {{ printf '%s=%s\\n' "$1" "$2"; }}
_elbencho_staged_workload_begin /workload cache_hit reused {termination}
""")
    assert result.returncode == 0, result.stderr
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert fields["termination_mode"] == termination
    assert fields["effective_timelimit_seconds"] == limit
    assert fields["read_completion_state"] == completion


@pytest.mark.parametrize("access", ["dio", "bio"])
def test_staged_single_file_run_to_completion_reads_the_existing_extent_once(
    tmp_path, access
):
    dataset = tmp_path / "existing-file"
    dataset.write_bytes(b"x" * 4096)
    result = _run(_harness(tmp_path, access=access, counts="automatic") + f"""
ELBENCHO_SWEEP_READ_FROM={shlex.quote(str(dataset))}
ELBENCHO_SINGLE_BIG_FILE_SIZE=
_elbencho_finish_sweep_read_from_only() {{ return 0; }}
run_elbencho_io_sweep_iteration_single_big_file
""")
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert len(calls) == 1
    assert "--read" in calls[0] and str(dataset) in calls[0]
    assert "--timelimit" not in calls[0] and "--infloop" not in calls[0]
    assert "--size" not in calls[0]
    assert dataset.read_bytes() == b"x" * 4096


@pytest.mark.parametrize(
    "extra,expected",
    [
        ("run_to_completion=0", "requires completion mode"),
        ("TEST_DIRS=([/one]=0)", "weight"),
        ("ELBENCHO_FILES_PER_NODE=0", "positive integers"),
        (
            "ELBENCHO_SCALE_THREAD_LIST=(9223372036854775807); TEST_DIRS=([/one]=1 [/two]=1)",
            "threads times weighted targets",
        ),
        ("ELBENCHO_SINGLE_BIG_FILE=1", "not applicable"),
    ],
)
def test_explicit_run_to_completion_sizing_rejects_invalid_inputs(extra, expected):
    result = _run(f"""
source {shlex.quote(str(_FUNCTIONS))}
declare -A TEST_DIRS=([/one]=1)
ELBENCHO_FILE_LAYOUT=worker-directories ELBENCHO_FILES_PER_NODE=8
ELBENCHO_FILE_SIZE=4K ELBENCHO_SCALE_THREAD_LIST=(2)
ELBENCHO_SCALE_IO_SIZES=(4K) ELBENCHO_SINGLE_BIG_FILE=0 run_to_completion=1
{extra}
validate_elbencho_sweep_workload_mode dio 0 '' "$run_to_completion"
""")
    assert result.returncode != 0
    assert expected in result.stderr


def test_explicit_run_to_completion_accepts_multiple_weighted_filesystems():
    result = _run(f"""
source {shlex.quote(str(_FUNCTIONS))}
declare -A TEST_DIRS=([/one]=2 [/two]=1)
ELBENCHO_FILE_LAYOUT=worker-directories ELBENCHO_FILES_PER_NODE=7
ELBENCHO_FILE_SIZE=4K ELBENCHO_SCALE_THREAD_LIST=(2 4)
ELBENCHO_SCALE_IO_SIZES=(4K) ELBENCHO_SINGLE_BIG_FILE=0
validate_elbencho_sweep_workload_mode dio 0 '' 1
""")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "weights,expected",
    [
        ("[/one]=2 [/two]=1", "3"),
        ("[/one]=0", "weight"),
        ("[/one]=9223372036854775808", "weight"),
        ("[/one]=9223372036854775807 [/two]=1", "summed TEST_DIRS"),
    ],
)
def test_weighted_target_count_is_validated_without_arithmetic_overflow(
    weights, expected
):
    result = _run(f"""
source {shlex.quote(str(_FUNCTIONS))}
declare -A TEST_DIRS=({weights})
_elbencho_run_to_completion_target_count
""")
    if expected == "3":
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected
    else:
        assert result.returncode != 0
        assert expected in result.stderr


def _native_harness(root):
    return _harness(root) + f"""
run_an_elbencho() {{
    local phase=mkdir arg
    for arg in "$@"; do
        [[ "$arg" != --write ]] || phase=write
        [[ "$arg" != --read ]] || phase=read
    done
    {shlex.quote(str(_BINARY))} "$@" --jsonfile="$root/$phase.json"
}}
"""


def _assert_native_totals(root, total_files):
    for phase in ("write", "read"):
        records = [
            json.loads(line)
            for line in (root / f"{phase}.json").read_text().splitlines()
        ]
        record = next(item for item in records if item["phase_type"] == phase.upper())
        assert int(record["last_done"]["entries"]) == total_files
        assert int(record["last_done"]["bytes"]) == total_files * 4096


_NATIVE_ONLY = pytest.mark.skipif(
    platform.system() != "Linux"
    or platform.machine() != "aarch64"
    or not _BINARY.is_file(),
    reason="requires the cached Linux aarch64 elbencho probe binary",
)


@_NATIVE_ONLY
@pytest.mark.parametrize("random_io", [0, 1])
def test_native_run_to_completion_write_and_read_complete_exact_dataset(
    tmp_path, random_io
):
    result = _run(
        _native_harness(tmp_path)
        + f"use_random={random_io}\nrun_elbencho_io_sweep_iteration\n"
    )
    assert result.returncode == 0, result.stderr
    _assert_native_totals(tmp_path, 8)


@_NATIVE_ONLY
@pytest.mark.parametrize("requested,effective", [(17, 18), (14, 12), (1, 6)])
def test_native_weighted_run_to_completion_preserves_total_budget_and_filesystem_ratio(
    tmp_path, requested, effective
):
    result = _run(_native_harness(tmp_path) + f"""
mkdir -p "$root/fs-a" "$root/fs-b"
test_dirs_csv="$root/fs-a/one,$root/fs-a/two,$root/fs-b/one"
ELBENCHO_FILES_PER_NODE={requested}
run_elbencho_io_sweep_iteration
""")
    assert result.returncode == 0, result.stderr
    _assert_native_totals(tmp_path, effective)
    assert f"requested={requested} effective={effective}" in result.stdout
    for filesystem, expected in (
        ("fs-a", effective * 2 // 3),
        ("fs-b", effective // 3),
    ):
        files = [path for path in (tmp_path / filesystem).rglob("*") if path.is_file()]
        assert len(files) == expected
