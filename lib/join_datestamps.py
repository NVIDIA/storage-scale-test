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

"""Join run datestamps for titles and filesystem-safe plot/CSV basenames.

Plot PNG and CSV basenames are a single path component and must stay within
NAME_MAX (255 on ext4/APFS/XFS). Joining every run datestamp into that
component fails when many result directories are combined. Use ``max_len`` (or
``join_datestamps_for_filename``) for path components; keep the full join for
titles and report text.
"""

from typing import Optional, Set

# Single path-component limit on ext4/APFS/XFS (NAME_MAX).
MAX_FILENAME_LEN = 255


def join_datestamps(
    datestamps: Set[str],
    *,
    sep: str = "-",
    max_len: Optional[int] = None,
) -> str:
    """Join sorted datestamps; optionally abbreviate to fit ``max_len``.

    When ``max_len`` is set and the full join would exceed it, return
    ``{earliest}-to-{latest}-n{count}`` (stamps sort as chronological for the
    ``YYYYMMDDZHHMISS`` form used in this repo).
    """
    if not datestamps:
        return ""
    ordered = sorted(datestamps)
    joined = sep.join(ordered)
    if max_len is None or len(joined) <= max_len:
        return joined
    abbreviated = f"{ordered[0]}-to-{ordered[-1]}-n{len(ordered)}"
    if len(abbreviated) <= max_len:
        return abbreviated
    # Extreme fallback if even the abbreviation cannot fit the budget.
    return f"n{len(ordered)}"[:max_len]


def join_datestamps_for_filename(
    datestamps: Set[str],
    *,
    prefix: str,
    suffix: str = ".png",
    sep: str = "-",
    max_filename_len: int = MAX_FILENAME_LEN,
) -> str:
    """Join datestamps so ``prefix + result + suffix`` stays within NAME_MAX."""
    budget = max_filename_len - len(prefix) - len(suffix)
    if budget < 1:
        raise ValueError(
            f"prefix {prefix!r} and suffix {suffix!r} leave no room for datestamps "
            f"within {max_filename_len} characters"
        )
    return join_datestamps(datestamps, sep=sep, max_len=budget)
