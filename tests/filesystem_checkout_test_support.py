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

"""Shared local checkout for filesystem batch and override shell tests."""

from pathlib import Path
import shlex
import shutil


def make_filesystem_checkout(parent: Path, repo_root: Path) -> Path:
    """Copy real launchers and seed a bounded SSH workload with a fake binary."""
    base = parent / "checkout's space"
    base.mkdir()
    shutil.copytree(repo_root / "lib", base / "lib")
    shutil.copytree(repo_root / "storage-tests/fs", base / "storage-tests/fs")
    (base / "utils").mkdir()
    (base / "utils/elbencho").write_text("#!/usr/bin/env bash\nexit 0\n")
    (base / "utils/elbencho").chmod(0o755)
    (base / "hosts").write_text("first,second\n")
    text = (repo_root / "env.sh.template").read_text()
    overrides = f"""
export EXECUTION_SUBSTRATE=ssh
export SSH_HOST_LIST={shlex.quote(str(base / 'hosts'))}
export SSH_USER=tester
export RESULTS_DIR={shlex.quote(str(base / 'results'))}
export LOGS_DIR={shlex.quote(str(base / 'logs'))}
TEST_DIRS=(["/data/one"]=1)
export ELBENCHO_SCALE_THREAD_LIST=(1)
export ELBENCHO_SCALE_IO_SIZES=(4K)
export ELBENCHO_IODEPTH_LIST=(1)
export MDTEST_BRANCH_FACTOR=1 MDTEST_ITEMS_PER_DIR=2 MDTEST_ITERATIONS=1
"""
    (base / "env.sh").write_text(
        text.replace("# STORAGE_SCALE_TEST_INTEGRATION_OVERRIDES", overrides)
    )
    return base
