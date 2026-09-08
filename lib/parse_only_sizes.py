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

"""Parse --only-sizes CLI values (append + optional semicolon lists)."""

from typing import List, Optional, Set

_SIZE_LIST_SEP = ";"


def _parts_from_append_token(token: str) -> List[str]:
    stripped = token.strip()
    if not stripped:
        return []
    if _SIZE_LIST_SEP in stripped:
        return [p.strip() for p in stripped.split(_SIZE_LIST_SEP) if p.strip()]
    return [stripped]


def parse_only_sizes_arg(values: Optional[List[str]]) -> Optional[Set[str]]:
    """
    Build a set of size filter strings from argparse append values.

    Each --only-sizes argument is one token. Commas are not split (so elbencho
    compound sizes like 1M,r64K stay one entry). Within a single argument,
    semicolons separate multiple sizes: '1M,r64K;4K'.

    Args:
        values: List from argparse with action='append', or None.

    Returns:
        Non-empty set of size strings, or None if values is None/empty/all-blank.
    """
    if not values:
        return None

    result: Set[str] = set()
    for raw in values:
        for part in _parts_from_append_token(raw):
            result.add(part)

    return result if result else None
