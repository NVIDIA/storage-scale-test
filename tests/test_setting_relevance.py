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

"""Tests for which recorded sweep settings apply to a run."""

import pytest

from lib.setting_relevance import (
    RunContext,
    is_relevant,
    relevant_settings,
    run_context,
)

GENERATED = RunContext()
READ_FROM = RunContext(phase_plan="read_from")
BIG = RunContext(single_big_file=True)
WRITE_ONLY = RunContext(phase_plan="write_only")
COMPLETE = RunContext(run_to_completion=True)
KUBECTL = RunContext(substrate="kubectl")
MDTEST = RunContext(kind="mdtest")


@pytest.mark.parametrize(
    "key, applies, does_not_apply",
    [
        ("ELBENCHO_FILE_SIZE", GENERATED, READ_FROM),
        ("ELBENCHO_FILE_SIZE", GENERATED, BIG),
        ("ELBENCHO_FILE_SIZE_MULTIPLIER", GENERATED, READ_FROM),
        ("ELBENCHO_FILES_PER_NODE", GENERATED, BIG),
        ("ELBENCHO_FILES_PER_NODE", GENERATED, READ_FROM),
        ("ELBENCHO_SINGLE_BIG_FILE_SIZE", BIG, GENERATED),
        ("ELBENCHO_SINGLE_BIG_FILE_SIZE", BIG, RunContext("io", "", "read_from", True)),
        ("ELBENCHO_SINGLE_BIG_FILE_BASENAME", BIG, GENERATED),
        ("ELBENCHO_READ_AFTER_WRITE_PAUSE", GENERATED, WRITE_ONLY),
        ("ELBENCHO_READ_AFTER_WRITE_PAUSE", MDTEST, READ_FROM),
        ("ELBENCHO_SCALE_READ_WRITE_DURATION", GENERATED, COMPLETE),
        ("KUBECTL_ELBENCHO_IMAGE", KUBECTL, GENERATED),
        ("KUBECTL_MAPPED_READ_FROM", RunContext("io", "kubectl", "read_from"), KUBECTL),
        ("nodes_spec", RunContext(swept="threads_per_node"), RunContext(swept="nodes")),
        ("ELBENCHO_SCALE_THREAD_LIST", GENERATED, RunContext(swept="threads_per_node")),
        ("ELBENCHO_IODEPTH_LIST", GENERATED, RunContext(swept="io_depth")),
        ("tasks_spec", MDTEST, RunContext("mdtest", swept="tasks_per_node")),
    ],
)
def test_conditional_settings(key, applies, does_not_apply):
    assert is_relevant(key, {}, applies)
    assert not is_relevant(key, {}, does_not_apply)


def test_multiplier_applies_only_without_an_explicit_size():
    key = "ELBENCHO_FILE_SIZE_MULTIPLIER"
    assert is_relevant(key, {"ELBENCHO_FILE_SIZE": ""}, GENERATED)
    assert not is_relevant(key, {"ELBENCHO_FILE_SIZE": "4G"}, GENERATED)


def test_ignored_flags_and_unknown_settings():
    settings = {
        "ELBENCHO_LIVEINT": 1000,
        "ELBENCHO_LIVE_CSV_EXTENDED": 1,
        "FS_MAX_NODE_IOPS": 100,
        "env_override": None,
        "ELBENCHO_SINGLE_BIG_FILE": 0,
        "ELBENCHO_SINGLE_BIG_FILE_SIZE": "1T",
        "run_to_completion_option": 1,
        "sweep_read_from": "",
        "SOME_FUTURE_SETTING": "x",
    }
    assert relevant_settings(settings, GENERATED) == {
        "ELBENCHO_SINGLE_BIG_FILE": 0,
        "run_to_completion_option": 1,
        "sweep_read_from": "",
        "SOME_FUTURE_SETTING": "x",
    }
    assert relevant_settings(None, GENERATED) == {}


@pytest.mark.parametrize(
    "settings, plan",
    [
        ({}, "write_read"),
        ({"sweep_write_only": 1}, "write_only"),
        ({"sweep_write_no_read": "1"}, "write_no_read"),
        ({"sweep_read_from": "/mnt/test/data"}, "read_from"),
    ],
)
def test_context_from_settings(settings, plan):
    assert run_context("io", settings).phase_plan == plan


def test_context_prefers_coordinates():
    settings = {
        "EXECUTION_SUBSTRATE": "kubectl",
        "ELBENCHO_SINGLE_BIG_FILE": 0,
        "run_to_completion_option": 0,
    }
    coords = [None, {"phase_plan": "read_from", "single_big_file": True}]
    coords.append({"run_to_completion": True})
    context = run_context("io", settings, coords, "nodes")
    assert context == RunContext("io", "kubectl", "read_from", True, True, "nodes")
    assert run_context("mdtest", None).kind == "mdtest"
    assert run_context(None, {"ELBENCHO_SINGLE_BIG_FILE": "1"}).single_big_file
