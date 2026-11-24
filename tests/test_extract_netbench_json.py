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

"""extract-netbench.py reads elbencho 3.4 --jsonfile results; the .json of a
run gives what its .csv/.out give."""

import importlib.util
import re
import shlex
from pathlib import Path

from lib.reporting_common import parse_integer_histogram
from tests import elbencho_v34_samples as samples
from tests.extract_elbencho_test_support import install_extract_heavy_dependency_stubs

_STEM = samples.NETBENCH_STEM
_KEY = ("bidir", 2, 2, "20261124Z000000", 1, "AtoB")


def _load():
    install_extract_heavy_dependency_stubs()
    path = Path(__file__).resolve().parents[1] / "utils" / "extract-netbench.py"
    spec = importlib.util.spec_from_file_location("extract_netbench_json", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_NETBENCH = _load()


def _write(directory, **contents):
    for extension, text in contents.items():
        (directory / f"{_STEM}.{extension}").write_text(text, encoding="utf-8")
    return str(directory / _STEM)


def _pre_34(text):
    """The sample as elbencho before 3.4 named the phase and histogram."""
    return text.replace(",NET/tcp,", ",NET,").replace(
        "IO send lat hist", "IO wr lat hist"
    )


def _parse(base, extension):
    return _NETBENCH.parse_file_pair(f"{base}.{extension}", f"{base}.out", *_KEY)


def test_json_results_equal_the_csv_and_out_of_the_same_run(tmp_path):
    current = _parse(_write(tmp_path, json=samples.NETBENCH_JSON), "json")
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    legacy = _parse(
        _write(
            legacy_dir,
            csv=_pre_34(samples.NETBENCH_CSV),
            out=_pre_34(samples.NETBENCH_OUT),
        ),
        "csv",
    )

    assert current.histogram and current.histogram == legacy.histogram
    hist = re.search(r"IO send lat hist\s*:\s*\[\s*([^\]]+)\]", samples.NETBENCH_OUT)
    assert current.histogram == parse_integer_histogram(hist.group(1))
    assert (current.lat_min_sec, current.lat_max_sec) == (
        legacy.lat_min_sec,
        legacy.lat_max_sec,
    )
    # The CSV rounds MiB/s to whole numbers; the JSON has exact bytes/s.
    assert abs(current.mib_s_write - legacy.mib_s_write) <= 1.0
    assert abs(current.mib_s_total - legacy.mib_s_total) <= 2.0
    assert current.mib_s_total > current.mib_s_write > 0
    assert shlex.split(current.command) == shlex.split(legacy.command)


def test_discovery_pairs_the_json_with_the_out(tmp_path):
    _write(
        tmp_path,
        json=samples.NETBENCH_JSON,
        csv=samples.NETBENCH_CSV,
        out=samples.NETBENCH_OUT,
    )
    pairs = _NETBENCH.discover_files([str(tmp_path)])
    assert pairs == {
        _KEY: (str(tmp_path / f"{_STEM}.json"), str(tmp_path / f"{_STEM}.out"))
    }


def test_unreadable_json_is_skipped(tmp_path):
    base = _write(tmp_path, json="{", out=samples.NETBENCH_OUT)
    assert _parse(base, "json") is None
