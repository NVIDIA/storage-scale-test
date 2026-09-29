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

"""Tests for netbench histogram cache integrity."""

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "utils" / "extract-netbench.py"
_SPEC = importlib.util.spec_from_file_location(
    "extract_netbench_histogram", _MODULE_PATH
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


def test_cached_histogram_rejects_duplicate_normalized_buckets():
    """CSV cache entries cannot silently replace an earlier bucket count."""
    try:
        _MODULE.decode_histogram("2:5;02:7")
    except ValueError as error:
        assert "Duplicate histogram bucket" in str(error)
    else:
        raise AssertionError("duplicate histogram bucket was accepted")
