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

"""Shared loader and optional-dependency stubs for extract-elbencho tests."""

import importlib
import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _install_yaml_stub_if_unavailable() -> None:
    """Use installed PyYAML when available and a minimal stand-in otherwise."""
    if "yaml" in sys.modules:
        return
    try:
        importlib.import_module("yaml")
    except ImportError:
        stub_yaml = types.ModuleType("yaml")
        stub_yaml.safe_load = lambda _value: {}
        sys.modules["yaml"] = stub_yaml


def install_extract_heavy_dependency_stubs() -> None:
    """Install minimal plotting stubs needed to import extract-elbencho quickly."""
    _install_yaml_stub_if_unavailable()
    if "matplotlib" not in sys.modules:
        stub_matplotlib = types.ModuleType("matplotlib")
        stub_pyplot = types.ModuleType("matplotlib.pyplot")
        stub_ticker = types.ModuleType("matplotlib.ticker")
        stub_ticker.FuncFormatter = lambda *_args, **_kwargs: None
        sys.modules["matplotlib"] = stub_matplotlib
        sys.modules["matplotlib.pyplot"] = stub_pyplot
        sys.modules["matplotlib.ticker"] = stub_ticker
    if "numpy" not in sys.modules:
        stub_numpy = types.ModuleType("numpy")

        class _NdArray:
            """Placeholder for PlotMetadata type annotations."""

        stub_numpy.ndarray = _NdArray
        sys.modules["numpy"] = stub_numpy


def load_extract_elbencho_module(module_name: str) -> ModuleType:
    """Load extract-elbencho under a test-specific module name."""
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    install_extract_heavy_dependency_stubs()
    extract_path = _REPO_ROOT / "utils" / "extract-elbencho.py"
    spec = importlib.util.spec_from_file_location(module_name, extract_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module
