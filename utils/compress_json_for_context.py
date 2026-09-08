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

"""Generate a concise structural summary of JSON for AI context."""

import json
import sys
from typing import Any, Set, Tuple


def get_structure_key(obj: Any) -> Tuple:
    """Get a hashable key representing the structure of an object."""
    if isinstance(obj, dict):
        return ("dict", tuple(sorted(obj.keys())))
    if isinstance(obj, list):
        if not obj:
            return ("list", "empty")
        # Structure key based on first element type
        return ("list", get_structure_key(obj[0]))
    return ("value", type(obj).__name__)


def truncate_string(s: str, max_len: int = 80) -> str:
    """Truncate long strings intelligently."""
    if len(s) <= max_len:
        return s
    # For command lines and URLs, show beginning and end
    if any(prefix in s for prefix in ["http", "://", "--", "/"]):
        return s[: max_len - 3] + "..."
    return s[:max_len] + "..."


def format_value(value: Any, max_len: int = 60) -> str:
    """Format a value for display."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        # Redact before truncating so long credentials cannot leak a prefix.
        if any(key in value.lower() for key in ["secret", "password", "key", "token"]):
            return '"*REDACTED*"'
        # Truncate long strings
        if len(value) > max_len:
            return f'"{truncate_string(value, max_len)}"'
        return f'"{value}"'
    return str(value)


def summarize_json(
    obj: Any, indent: int = 0, seen_structures: Set[Tuple] = None, depth: int = 0
) -> str:
    """
    Generate a concise structural summary of JSON.

    Args:
        obj: The JSON object to summarize
        indent: Current indentation level
        seen_structures: Set of structure keys we've already expanded
        depth: Current depth in the tree
    """
    if seen_structures is None:
        seen_structures = set()

    ind = " " * indent
    max_depth = 10  # Prevent infinite recursion

    if depth > max_depth:
        return f"{ind}..."

    if obj is None or isinstance(obj, (bool, int, float, str)):
        return format_value(obj)

    if isinstance(obj, dict):
        if not obj:
            return "{}"

        struct_key = get_structure_key(obj)
        is_new_structure = struct_key not in seen_structures

        # Count items
        item_count = len(obj)
        result = [f"{{{item_count} items"]

        # At shallow depths (0-2), always expand regardless of whether we've seen the structure
        # At deeper depths (3+), only expand if it's a new structure
        if is_new_structure or depth <= 2:
            # Mark as seen (but only enforce this check at depth > 2)
            if depth > 2:
                seen_structures.add(struct_key)

            # Decide how many items to show based on depth
            # At shallow depths, show more; at deep depths, show less
            items_to_show = list(obj.items())

            result.append("")  # Newline after opening brace

            for key, value in items_to_show:
                value_struct = get_structure_key(value)

                # Format key
                key_str = f'{ind}  "{key}":'

                # Handle different value types
                if isinstance(value, dict):
                    if not value:
                        result.append(f"{key_str}{{}}")
                    elif value_struct in seen_structures and depth > 3:
                        # Seen this structure before at deep levels only, collapse it but show ALL keys
                        keys_preview = ", ".join(value.keys())
                        result.append(f"{key_str}{{{keys_preview}}}{len(value)} items")
                    else:
                        # Expand the dict (be generous at shallow depths)
                        summary = summarize_json(
                            value, indent + 2, seen_structures, depth + 1
                        )
                        result.append(f"{key_str}{summary}")

                elif isinstance(value, list):
                    if not value:
                        result.append(f"{key_str}[]")
                    else:
                        list_struct = get_structure_key(value)
                        # At shallow depths (0-3), always show structure; deeper, can collapse
                        if list_struct in seen_structures and depth > 3:
                            # Seen this list structure before at deep levels
                            result.append(f"{key_str}[...]{len(value)} items")
                        else:
                            # Show first element (or more at shallow depths)
                            seen_structures.add(list_struct)
                            if len(value) == 1:
                                elem_summary = summarize_json(
                                    value[0], indent + 2, seen_structures, depth + 1
                                )
                                result.append(f"{key_str}[{elem_summary}]")
                            elif depth <= 1 and len(value) == 2:
                                # At very shallow depths, show both elements if there are only 2
                                elem1 = summarize_json(
                                    value[0], indent + 2, seen_structures, depth + 1
                                )
                                elem2 = summarize_json(
                                    value[1], indent + 2, seen_structures, depth + 1
                                )
                                result.append(f"{key_str}[{elem1}, {elem2}]")
                            else:
                                elem_summary = summarize_json(
                                    value[0], indent + 2, seen_structures, depth + 1
                                )
                                result.append(
                                    f"{key_str}[{elem_summary}, ...]{len(value)} items"
                                )

                else:
                    # Scalar value
                    result.append(f"{key_str}{format_value(value)}")

            result.append(f"{ind}}}")

        else:
            # We've seen this structure before - show ALL keys
            keys_preview = ", ".join(obj.keys())
            result.append(f"\n{ind}  (keys: {keys_preview})\n{ind}}}")

        return "\n".join(result)

    if isinstance(obj, list):
        if not obj:
            return "[]"

        struct_key = get_structure_key(obj)
        item_count = len(obj)

        if struct_key in seen_structures and depth > 0:
            return f"[...]{item_count} items"

        seen_structures.add(struct_key)

        # For arrays, show only first 1-2 elements
        if item_count == 1:
            elem_summary = summarize_json(
                obj[0], indent + 2, seen_structures, depth + 1
            )
            return f"[\n{ind}  {elem_summary}\n{ind}]"
        else:
            elem_summary = summarize_json(
                obj[0], indent + 2, seen_structures, depth + 1
            )
            return f"[\n{ind}  {elem_summary},\n{ind}  ...\n{ind}]{item_count} items"

    return str(obj)


def main():
    """Main entry point."""
    try:
        data = json.load(sys.stdin)
        summary = summarize_json(data)
        print(summary)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    except (IOError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
