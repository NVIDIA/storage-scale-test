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

"""Bounded, host-local evidence for NFS and kernel I/O stalls.

This child is separately time-bounded by the driver and never accesses the
potentially stalled PVC. Without privilege, inaccessible proc files are logged.
"""

from pathlib import Path


def _read(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            return stream.read(16384)
    except OSError as error:
        return f"unavailable: {error}\n"


def main() -> None:
    """Print kernel pressure, NFS queues/counters, and up to 24 blocked stacks."""
    for name in (
        "meminfo",
        "pressure/memory",
        "pressure/io",
        "net/rpc/nfs",
        "net/rpc/nfsd",
        "fs/nfsd/threads",
        "fs/nfsd/pool_stats",
        "net/tcp",
    ):
        path = Path("/proc") / name
        print(f"== {path} ==\n{_read(path)}", flush=True)
    blocked = 0
    for process in sorted(Path("/proc").iterdir()):
        if not process.name.isdigit():
            continue
        status = _read(process / "status")
        if "State:\tD " not in status:
            continue
        print(f"== blocked PID {process.name} ==\n{status[:1024]}", flush=True)
        print(_read(process / "wchan"), _read(process / "stack"), flush=True)
        blocked += 1
        if blocked >= 24:
            break


if __name__ == "__main__":
    main()
