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

"""Summarize nv-elbencho-sweep result directories from env_used.yaml (stdout).

Emits three lines per directory: basename, W/R duration, nodes, optional TEST_DIRS, then two indented lines.

Compares env_used.yaml to elbencho-*-c_*-s_*-d_*_<DS>.out files in the directory;
if any expected benchmark outputs are missing, all three lines are flagged INCOMPLETE.

Requires PyYAML (e.g. run via ./utils/summarize-elbencho.sh which sets up a venv).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# pylint: disable=wrong-import-position
from lib.env_used_yaml import load_env_used_yaml

_DS_TAIL_RE = re.compile(r"^elbencho-(\d{8}Z\d{6})$")
_BENCH_OUT_BASENAME_RE = re.compile(
    r"^elbencho-(.+)-c_(\d+)-s_(\d+)-d_(\d+)_(\d{8}Z\d{6})\.out$"
)
_NODES_ELEM_RANGE = re.compile(r"^(\d+)-(\d+)(\+(\d+))?$")
_NODES_ELEM_SINGLE = re.compile(r"^\d+$")
_DEFAULT_FS_MUL = 1024
_INCOMPLETE_TAG = "*** INCOMPLETE RUN ***"
_INCOMPLETE_LINE23_PREFIX = "[INCOMPLETE]"
_MULTIPLE_TEST_DIRS_SUFFIX = "(multiple TEST_DIRs)"


def _load_workload_metadata(result_dir: str) -> List[Dict[str, str]]:
    """Load valid key/value workload TSV files for explicit mode reporting."""
    rows: List[Dict[str, str]] = []
    for path in sorted((Path(result_dir) / "executions").glob("*.workload.tsv")):
        record: Dict[str, str] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                key, value = line.split("\t", 1)
                if not key or key in record:
                    raise ValueError("invalid or duplicate workload key")
                record[key] = value
        except (OSError, ValueError):
            continue
        rows.append(record)
    return rows


def _workload_values(rows: Sequence[Dict[str, str]], key: str) -> str:
    """Format the distinct non-null values for one workload metadata key."""
    values = sorted(
        {row[key] for row in rows if row.get(key) not in (None, "null", "pending")},
        key=lambda value: (len(value), value),
    )
    if not values:
        return ""
    return values[0] if len(values) == 1 else f"[{','.join(values)}]"


def _eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def _coerce_01(val: Any) -> int:
    if val in (1, "1", True):
        return 1
    return 0


def _comma_paren(items: Sequence[Any]) -> str:
    inner = ", ".join(str(x) for x in items)
    return f"({inner})"


def _dio_upper(env: Dict[str, Any]) -> str:
    raw = str(env.get("dio_or_bio") or "dio").strip().lower()
    return "DIO" if raw == "dio" else "BIO"


def _is_mdtest_elbencho_env(env: Dict[str, Any]) -> bool:
    if "tasks_spec" in env:
        return True
    return "MDTEST_BRANCH_FACTOR" in env


def _missing_io_sweep_shape(env: Dict[str, Any]) -> bool:
    if env.get("dio_or_bio") is not None:
        return False
    if env.get("ELBENCHO_SCALE_IO_SIZES") is not None:
        return False
    return True


def _non_empty_str_list(env: Dict[str, Any], key: str) -> bool:
    val = env.get(key)
    if not isinstance(val, list) or not val:
        return False
    return True


def _validate_io_sweep_env(env: Dict[str, Any]) -> Optional[str]:
    if _is_mdtest_elbencho_env(env):
        return "env_used.yaml looks like mdtest-elbencho, not nv-elbencho IO sweep"
    if _missing_io_sweep_shape(env):
        return (
            "missing dio_or_bio and ELBENCHO_SCALE_IO_SIZES (not an IO sweep snapshot)"
        )
    ns = env.get("nodes_spec")
    if ns is None or not str(ns).strip():
        return "missing or empty nodes_spec"
    for key in (
        "ELBENCHO_SCALE_IO_SIZES",
        "ELBENCHO_SCALE_THREAD_LIST",
        "ELBENCHO_IODEPTH_LIST",
    ):
        if not _non_empty_str_list(env, key):
            return f"missing or empty {key}"
    return None


def _maybe_warn_unexpected_basename(basename: str, display_path: str) -> None:
    if _DS_TAIL_RE.match(basename):
        return
    _eprint(
        f"Warning: {display_path}: basename {basename!r} does not match "
        f"elbencho-<YYYYMMDD>Z<HHMMSS> (expected from nv-elbencho-sweep.sh)"
    )


def _file_size_multiplier(env: Dict[str, Any], display_path: str) -> int:
    v = env.get("ELBENCHO_FILE_SIZE_MULTIPLIER")
    if v is None:
        _eprint(
            f"Warning: {display_path}: ELBENCHO_FILE_SIZE_MULTIPLIER missing; "
            f"using {_DEFAULT_FS_MUL}"
        )
        return _DEFAULT_FS_MUL
    return int(v)


def _expand_one_nodes_element(element: str) -> List[int]:
    """One fragment of nodes_spec (e.g. 3-10+2 or 8). Raises ValueError if invalid."""
    elem = element.strip()
    if not elem:
        raise ValueError("empty element")
    m = _NODES_ELEM_RANGE.match(elem)
    if m:
        start, stop = int(m.group(1)), int(m.group(2))
        inc_part = m.group(4)
        step = int(inc_part) if inc_part else 1
        if start < 1 or stop < 1 or step < 1 or start > stop:
            raise ValueError(elem)
        out = [start]
        if start == stop:
            return out
        cur = start + step
        while cur < stop:
            out.append(cur)
            cur += step
        out.append(stop)
        return out
    if _NODES_ELEM_SINGLE.match(elem):
        v = int(elem)
        if v < 1:
            raise ValueError(elem)
        return [v]
    raise ValueError(elem)


def _expand_nodes_spec(spec: str) -> Optional[List[int]]:
    """Expand nodes_spec like parse_range_specification in lib/env_functions.sh."""
    s = spec.strip()
    if not s or s.startswith(",") or s.endswith(",") or ",," in s:
        return None
    out: List[int] = []
    try:
        for part in s.split(","):
            if not part.strip():
                return None
            out.extend(_expand_one_nodes_element(part))
    except ValueError:
        return None
    return out


def _datestamp_from_basename(basename: str) -> Optional[str]:
    m = _DS_TAIL_RE.match(basename)
    return m.group(1) if m else None


def _infer_datestamp_from_bench_outs(result_dir: str) -> Optional[str]:
    dss = set()
    try:
        names = os.listdir(result_dir)
    except OSError:
        return None
    for fn in names:
        m = _BENCH_OUT_BASENAME_RE.match(fn)
        if m:
            dss.add(m.group(5))
    if len(dss) == 1:
        return next(iter(dss))
    return None


def _expected_bench_out_basenames(
    env: Dict[str, Any], ds: str, nodes: Sequence[int]
) -> List[str]:
    ios = [str(x) for x in env["ELBENCHO_SCALE_IO_SIZES"]]
    threads = [int(x) for x in env["ELBENCHO_SCALE_THREAD_LIST"]]
    depths = [int(x) for x in env["ELBENCHO_IODEPTH_LIST"]]
    names: List[str] = []
    for nc in nodes:
        for io_sz in ios:
            for tc in threads:
                for idepth in depths:
                    names.append(
                        f"elbencho-{io_sz}-c_{nc:03d}-s_{tc:03d}-d_{idepth:03d}_{ds}.out"
                    )
    return names


def _bench_out_basenames_present(result_dir: str, ds: str) -> List[str]:
    found: List[str] = []
    try:
        names = os.listdir(result_dir)
    except OSError:
        return found
    for fn in names:
        m = _BENCH_OUT_BASENAME_RE.match(fn)
        if m and m.group(5) == ds:
            found.append(fn)
    return found


def _execution_status_incomplete_note(result_dir: str) -> str:
    """Return an incomplete banner for non-SUCCESS reified executions."""
    executions = Path(result_dir) / "executions"
    definitions = sorted(
        path for path in executions.glob("[0-9]*.sh") if path.stem.isdigit()
    )
    if not definitions:
        return ""
    incomplete = 0
    for definition in definitions:
        status_path = definition.with_suffix(".status")
        try:
            status = status_path.read_text(encoding="utf-8").strip()
        except OSError:
            status = ""
        if status != "SUCCESS":
            incomplete += 1
    if incomplete == 0:
        return ""
    return (
        f"{_INCOMPLETE_TAG} {incomplete}/{len(definitions)} executions " "not SUCCESS"
    )


def _completeness_note(
    result_dir: str, basename: str, env: Dict[str, Any], display_path: str
) -> str:
    """Return a short banner if benchmark .out files are missing; else ''."""
    status_note = _execution_status_incomplete_note(result_dir)
    if status_note:
        return status_note
    ds = _datestamp_from_basename(basename)
    if ds is None:
        ds = _infer_datestamp_from_bench_outs(result_dir)
    if ds is None:
        _eprint(
            f"Warning: {display_path}: cannot resolve datestamp for completeness check "
            "(expected elbencho-<DS> dirname or consistent *_<DS>.out files)"
        )
        return ""
    nodes = _expand_nodes_spec(str(env.get("nodes_spec", "")).strip())
    if nodes is None:
        _eprint(
            f"Warning: {display_path}: nodes_spec parse failed; skipping completeness check"
        )
        return ""
    expected = _expected_bench_out_basenames(env, ds, nodes)
    exp_set = set(expected)
    found_set = set(_bench_out_basenames_present(result_dir, ds))
    missing = exp_set - found_set
    if not missing:
        return ""
    n_exp, n_miss = len(exp_set), len(missing)
    return f"{_INCOMPLETE_TAG} missing {n_miss}/{n_exp} elbencho-*-c_*-s_*-d_*_{ds}.out"


def _test_dirs_line_suffix(env: Dict[str, Any]) -> str:
    """Append single TEST_DIRS path, or a multiple-dirs marker, to line 1."""
    td = env.get("TEST_DIRS")
    if not isinstance(td, dict) or not td:
        return ""
    keys = [str(k) for k in td.keys()]
    if len(keys) == 1:
        return f" {keys[0]}"
    if len(keys) > 1:
        return f" {_MULTIPLE_TEST_DIRS_SUFFIX}"
    return ""


def _shorten_rf_under_test_dir(rf_raw: str, env: Dict[str, Any]) -> str:
    """If rf path is under a TEST_DIRS key, show path relative to that key."""
    stripped = str(rf_raw).strip()
    if not stripped:
        return ""
    td = env.get("TEST_DIRS")
    if not isinstance(td, dict) or not td:
        return stripped
    rf_norm = os.path.normpath(stripped)
    for base in sorted((str(k) for k in td.keys()), key=len, reverse=True):
        b_norm = os.path.normpath(base)
        if rf_norm == b_norm:
            return ""
        if rf_norm.startswith(b_norm + os.sep):
            rel = os.path.relpath(rf_norm, b_norm)
            return "" if rel == "." else rel
    return stripped


def _format_three_lines(
    basename: str,
    env: Dict[str, Any],
    display_path: str,
    result_dir: str,
) -> Tuple[str, str, str]:
    nodes_spec = str(env.get("nodes_spec", "")).strip()
    comp = _completeness_note(result_dir, basename, env, display_path)
    dur_sec = int(env.get("ELBENCHO_SCALE_READ_WRITE_DURATION", 0))
    rf = env.get("sweep_read_from")
    has_rf = rf is not None and str(rf).strip()
    layout = str(env.get("ELBENCHO_FILE_LAYOUT", "worker-directories"))
    files_per_node = str(env.get("ELBENCHO_FILES_PER_NODE", "")).strip()
    generated_shared = layout == "shared-directory" and files_per_node and not has_rf
    duration = (
        f"configured_dur: {dur_sec}s (inactive; completion-based)"
        if generated_shared
        else f"dur: {dur_sec}s"
    )
    line1 = (
        f"{basename} {duration} nodes: {nodes_spec}" f"{_test_dirs_line_suffix(env)}"
    )
    if comp:
        line1 = f"{comp} {line1}"
    if has_rf:
        fs_mul_str = "N/A"
    else:
        fs_mul_str = str(_file_size_multiplier(env, display_path))
    io_sizes = env["ELBENCHO_SCALE_IO_SIZES"]
    threads = env["ELBENCHO_SCALE_THREAD_LIST"]
    depths = env["ELBENCHO_IODEPTH_LIST"]
    inc23 = f"{_INCOMPLETE_LINE23_PREFIX} " if comp else ""
    line2 = (
        "    "
        f"{inc23}"
        f"{_dio_upper(env)} "
        f"io_size{_comma_paren(io_sizes)} "
        f"threads{_comma_paren(threads)} "
        f"io_depth{_comma_paren(depths)}"
    )
    line3_parts = [
        f"fs_mul={fs_mul_str}",
        f"layout={'staged-tree' if has_rf else layout}",
        f"single_big={_coerce_01(env.get('ELBENCHO_SINGLE_BIG_FILE'))}",
        f"wro={_coerce_01(env.get('sweep_write_only'))}",
        f"wnr={_coerce_01(env.get('sweep_write_no_read', 0))}",
    ]
    workload_rows = _load_workload_metadata(result_dir)
    requested_files = _workload_values(workload_rows, "requested_files_per_node")
    effective_files = _workload_values(workload_rows, "effective_files_per_node")
    if generated_shared:
        line3_parts.extend(
            (
                f"requested_files_per_node={requested_files or files_per_node}",
                f"effective_files_per_node={effective_files or files_per_node}",
            )
        )
        for key in (
            "write_elapsed_time_ms",
            "read_elapsed_time_ms",
            "delete_completed_files",
            "delete_elapsed_time_ms",
            "write_delete_elapsed_time_ms",
            "lifecycle_elapsed_time_ms",
            "delete_completion_state",
        ):
            value = _workload_values(workload_rows, key)
            if value:
                line3_parts.append(f"{key}={value}")
    else:
        line3_parts.append("files_per_node=N/A")
    if has_rf:
        dataset_files = _workload_values(workload_rows, "dataset_files_total")
        dataset_bytes = _workload_values(workload_rows, "dataset_bytes_total")
        if dataset_files:
            line3_parts.append(f"dataset_files={dataset_files}")
        if dataset_bytes:
            line3_parts.append(f"dataset_bytes={dataset_bytes}")
        for key in ("reader_nodes", "reader_threads_per_node", "reader_iodepth"):
            value = _workload_values(workload_rows, key)
            if value:
                line3_parts.append(f"{key}={value}")
        line3_parts.append("files_per_reader_node=N/A")
    if has_rf:
        rf_disp = _shorten_rf_under_test_dir(str(rf), env)
        if rf_disp:
            line3_parts.append(f"rf={rf_disp}")
    line3 = "    " + inc23 + " ".join(line3_parts)
    return (line1, line2, line3)


def _sort_key(path: str) -> str:
    return os.path.basename(os.path.normpath(path))


def _summarize_directory(path: str) -> Optional[Tuple[str, str, str]]:
    display = path
    if not os.path.isdir(path):
        _eprint(f"Warning: {display}: not a directory; skipping")
        return None
    abspath = os.path.abspath(path)
    basename = os.path.basename(os.path.normpath(abspath))
    env = load_env_used_yaml(abspath)
    if not env:
        _eprint(f"Warning: {display}: missing or unreadable env_used.yaml; skipping")
        return None
    _maybe_warn_unexpected_basename(basename, display)
    reason = _validate_io_sweep_env(env)
    if reason:
        _eprint(f"Warning: {display}: {reason}; skipping")
        return None
    return _format_three_lines(basename, env, display, abspath)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Print a three-line summary per nv-elbencho-sweep result directory "
            "(reads env_used.yaml)."
        )
    )
    parser.add_argument(
        "directories",
        nargs="+",
        help="One or more result directories (e.g. elbencho-<DS> under RESULTS_DIR)",
    )
    args = parser.parse_args(argv)
    sorted_dirs = sorted(args.directories, key=_sort_key)
    blocks: List[str] = []
    for path in sorted_dirs:
        triple = _summarize_directory(path)
        if not triple:
            continue
        line1, line2, line3 = triple
        blocks.append(f"{line1}\n{line2}\n{line3}")
    if blocks:
        print("\n".join(blocks))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
