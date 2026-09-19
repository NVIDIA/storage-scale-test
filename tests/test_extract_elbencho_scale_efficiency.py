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

"""Regression tests for Elbencho scaling-efficiency calculations."""

from tests.extract_elbencho_test_support import load_extract_elbencho_module

_MODULE = load_extract_elbencho_module("extract_elbencho_scale_efficiency_under_test")
_scale_efficiency = getattr(_MODULE, "_scale_efficiency")


def test_scale_efficiency_preserves_relative_values():
    """The fastest per-unit result is the 100-percent reference."""
    assert _scale_efficiency([2.0, 1.0]) == [100.0, 50.0]


def test_scale_efficiency_accepts_all_zero_results():
    """Valid rounded-zero reports do not divide by zero."""
    assert _scale_efficiency([0.0, 0.0]) == [0.0, 0.0]
