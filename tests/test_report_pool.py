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

"""Tests for the shared report process pool."""

# pylint: disable=protected-access

import contextlib
import io
import sys
from concurrent.futures import ProcessPoolExecutor
from unittest import mock

import pytest

from lib import report_pool


@pytest.mark.parametrize(
    ("cpus", "memory_gib", "expected"),
    [(12, None, 8), (4, None, 4), (12, 3, 4), (12, 0.1, 1)],
)
def test_report_workers_respect_cpus_and_memory(
    monkeypatch, cpus, memory_gib, expected
):
    monkeypatch.setattr(report_pool, "MAX_REPORT_WORKERS", 8)
    memory = None if memory_gib is None else int(memory_gib * (1 << 30))
    with mock.patch.object(
        report_pool.os, "sched_getaffinity", return_value=set(range(cpus)), create=True
    ), mock.patch.object(
        report_pool, "_available_memory_bytes", return_value=memory
    ), mock.patch.object(
        report_pool, "_cgroup_memory_bytes", return_value=None
    ):
        assert report_pool.report_workers() == expected


def _cgroups(tmp_path, monkeypatch, membership, files):
    """A fake /proc/self/cgroup and cgroup tree."""
    proc = tmp_path / "cgroup"
    proc.write_text(membership, encoding="ascii")
    root = tmp_path / "sys"
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="ascii")
    monkeypatch.setattr(report_pool, "_PROC_CGROUP", proc)
    monkeypatch.setattr(report_pool, "_CGROUP_ROOT", root)


def test_a_parent_cgroup_v2_limit_caps_workers(tmp_path, monkeypatch):
    """A 1 GiB slice leaves room for one worker even on a large host."""
    _cgroups(
        tmp_path,
        monkeypatch,
        "0::/capped.slice/report.service\n",
        {
            "capped.slice/memory.max": str(1 << 30),
            "capped.slice/memory.current": str(200 << 20),
            "capped.slice/report.service/memory.max": "max",
            "capped.slice/report.service/memory.current": str(100 << 20),
        },
    )
    assert report_pool._cgroup_memory_bytes() == (1 << 30) - (200 << 20)
    monkeypatch.setattr(report_pool, "MAX_REPORT_WORKERS", 8)
    with mock.patch.object(
        report_pool, "_available_memory_bytes", return_value=64 << 30
    ):
        assert report_pool.report_workers() == 1


def test_cgroup_v1_limits_count_and_unlimited_is_ignored(tmp_path, monkeypatch):
    _cgroups(
        tmp_path,
        monkeypatch,
        "5:cpu:/job\n4:memory:/job\n",
        {
            "memory/job/memory.limit_in_bytes": str(4 << 30),
            "memory/job/memory.usage_in_bytes": str(1 << 30),
            "memory/memory.limit_in_bytes": str(0x7FFFFFFFFFFFF000),
            "memory/memory.usage_in_bytes": str(8 << 30),
        },
    )
    assert report_pool._cgroup_memory_bytes() == 3 << 30


def test_no_cgroup_limit_leaves_memory_unknown(tmp_path, monkeypatch):
    _cgroups(tmp_path, monkeypatch, "0::/\n", {"memory.stat": ""})
    assert report_pool._cgroup_memory_bytes() is None


def test_one_worker_runs_tasks_inline():
    """Inline futures hold the result or the exception, like a real pool's."""
    pool = report_pool.report_pool()
    assert isinstance(pool, report_pool.InlineExecutor)
    assert pool.submit(sum, [1, 2]).result() == 3
    with pytest.raises(ZeroDivisionError):
        pool.submit(divmod, 1, 0).result()


def test_captured_output_replays_where_the_result_is_used(capsys):
    """A task's prints are held until replay writes them to this process."""

    def task(value):
        print("table")
        print("progress", file=sys.stderr)
        return value * 2

    future = report_pool.InlineExecutor().submit(report_pool.captured, task, 21)
    assert capsys.readouterr().out == ""
    assert report_pool.replay(future) == 42
    assert capsys.readouterr() == ("table\n", "progress\n")


def test_spawned_workers_run_reporter_tasks(monkeypatch):
    """Real workers load the reporter, so its functions pickle by module name."""
    monkeypatch.setattr(report_pool, "MAX_REPORT_WORKERS", 2)
    with mock.patch.object(report_pool, "report_workers", return_value=2):
        pool = report_pool.report_pool()
    assert isinstance(pool, ProcessPoolExecutor)
    with contextlib.redirect_stderr(io.StringIO()):
        engine = report_pool.load_engine()
    with pool:
        future = pool.submit(report_pool.captured, engine.format_with_sig_figs, 1.2345)
        assert future.result(timeout=120) == ("1.23", "", "")
