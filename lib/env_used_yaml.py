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

"""Load env_used.yaml from result directories.

Written by ``write_elbencho_env_used`` (IO sweep) or
``write_mdtest_elbencho_env_used`` (mdtest-elbencho sweep) in
``lib/env_functions.sh``."""

import os
import sys
from typing import Any, List

import yaml


def _eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def load_env_used_yaml(result_dir: str) -> dict:
    """Load env_used.yaml from a results directory. Returns empty dict if missing."""
    path = os.path.join(result_dir, "env_used.yaml")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except (OSError, yaml.YAMLError) as exc:
        _eprint(f"Warning: Failed to load {path}: {exc}")
        return {}


def _snapshot_string(env: dict, key: str) -> str:
    """Return one raw snapshot scalar as a displayable string."""
    value = env.get(key)
    return "" if value is None else str(value)


def apply_env_used_to_metrics(env: dict, metrics: List[Any]) -> None:
    """Set sweep-related flags on metrics from env_used.yaml data."""
    if not env or not metrics:
        return
    sbf = env.get("ELBENCHO_SINGLE_BIG_FILE")
    anad = env.get("ELBENCHO_ALL_NODES_ACCESS_ALL_DATA")
    single_opt = env.get("single_option")
    is_sbf = sbf in (1, "1")
    is_anad = anad in (1, "1")
    is_single_combined_run = single_opt in (1, "1")
    for metric in metrics:
        metric.configured_file_layout = _snapshot_string(env, "ELBENCHO_FILE_LAYOUT")
        metric.configured_files_per_node = _snapshot_string(
            env, "ELBENCHO_FILES_PER_NODE"
        )
        metric.configured_file_size = _snapshot_string(env, "ELBENCHO_FILE_SIZE")
        if is_sbf:
            metric.is_single_big_file = True
        if is_anad:
            metric.all_nodes_all_data = True
        if is_single_combined_run:
            metric.sweep_single_option = True
