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


"""Coordinates and coordinate keys derived from execution definitions."""

import pytest

from lib import filesystem_coordinates as coordinates

IO_DEFINITION = (
    "# Auto-generated; do not edit. Coords: nodes=4 io_size=1M,r4K "
    "thread_count=32 io_depth=16\n"
    "export ELBENCHO_EXECUTION_KIND=io\n"
    "export dio_or_bio=bio\n"
    "export use_random=0\n"
    "export ELBENCHO_FILE_LAYOUT='shared-directory'\n"
    "export ELBENCHO_SWEEP_WRITE_ONLY=0\n"
)
MDTEST_DEFINITION = (
    "export ELBENCHO_EXECUTION_KIND=mdtest\n"
    "export nodes=2\nexport tasks_per_node=8\nexport MDTEST_ITEMS_PER_DIR=100\n"
)


def test_io_definition_to_coordinates_and_key():
    found = coordinates.definition_coordinates("io", IO_DEFINITION, {})
    assert found is not None
    values, key = found
    assert key == "io/c004/s032/d016/w1M-seq/r4K-rand/bio"
    assert values["write_block_size_bytes"] == 1 << 20
    assert values["read_block_size_bytes"] == 4096
    assert values["direct_io"] is False
    assert values["file_layout"] == "shared-directory"


@pytest.mark.parametrize(
    "export, key",
    [
        ("ELBENCHO_SWEEP_WRITE_ONLY=1", "io/c004/s032/d016/w1M-seq/rnone/bio"),
        ("ELBENCHO_SWEEP_WRITE_NO_READ=1", "io/c004/s032/d016/w1M-seq/rnone/bio"),
        ("ELBENCHO_SWEEP_READ_FROM=/data", "io/c004/s032/d016/wnone/r4K-rand/bio"),
    ],
)
def test_phase_plans_blank_the_phases_that_do_not_run(export, key):
    content = IO_DEFINITION + f"export {export}\n"
    assert coordinates.definition_coordinates("io", content, {})[1] == key


def test_mdtest_definition_falls_back_to_env_settings():
    env = {"MDTEST_BRANCH_FACTOR": 7, "MDTEST_ITERATIONS": "3"}
    values, key = coordinates.definition_coordinates("mdtest", MDTEST_DEFINITION, env)
    assert key == "md/c002/t008/b7/i100"
    assert values["iterations"] == 3
    assert (
        coordinates.mdtest_coordinates(
            coordinates.parse_mdtest_definition(MDTEST_DEFINITION, {})
        )[0]["iterations"]
        == 0
    )


def test_unparseable_definitions_return_none():
    assert coordinates.definition_coordinates("io", "export nodes=1\n", {}) is None
    assert coordinates.definition_coordinates("mdtest", "export nodes=1\n", {}) is None
    assert coordinates.is_mdtest_definition(MDTEST_DEFINITION)
    assert not coordinates.is_mdtest_definition(IO_DEFINITION)


def test_size_tokens_use_binary_units():
    assert coordinates.elbencho_size_bytes("4k") == 4096
    assert coordinates.elbencho_size_bytes("512") == 512
    assert coordinates.elbencho_size_bytes("1x") is None


def test_module_stays_import_light():
    source = coordinates.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "import yaml" not in text and "zstandard" not in text
