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
#
# Reconstruct env_used.yaml for legacy elbencho IO sweep result directories by
# parsing *.out / *.log (stdlib only). See write_elbencho_env_used in lib/env_functions.sh.
# rand_option follows global -r (sweep IO Pattern summary and sbatch argv), not per-size r* IO sizes.

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import os
import re
import sys
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCRIPT_NAME = os.path.basename(__file__)

_DS_RE = re.compile(r"(\d{8}Z\d{6})")
_RUNNER_JOB_RE = re.compile(r"^[ \t]*(\d+) node / (\S+) job ID:", re.MULTILINE)
_SBATCH_TAIL_RE = re.compile(
    r"sbatch/_nv-elbencho-size-threads-sweep\.sh\s+"
    r"(\S+)\s+"  # output dir on submit host
    r"(dio|bio)\s+"
    r"([01])\s+"
    r"([01])\s+"
    r"(\S+)\s+"
    r"([01])\s+"
    r"([01])\s*"
    r"(.*?)\s*$"
)
_SBATCH_TAIL_RE_LEGACY = re.compile(
    r"sbatch/_nv-elbencho-size-threads-sweep\.sh\s+"
    r"(\S+)\s+"
    r"(dio|bio)\s+"
    r"([01])\s+"
    r"([01])\s+"
    r"(\S+)\s+"
    r"([01])\s*"
    r"(.*?)\s*$"
)
_RE_WRITE_ONLY_DATA_DIR = re.compile(
    r"^ELBENCHO_WRITE_ONLY_DATA_DIR=(.+)$", re.MULTILINE
)
_RE_SWEEP_READ_FROM = re.compile(
    r"^(?:ELBENCHO_SWEEP_READ_FROM=|Read-from:\s*)(.+)$", re.MULTILINE
)
_WR_IO_DURATION_LOG_RE = re.compile(
    r"(?:W/R IO Duration|Read IO Duration|Write IO Duration):\s*(\d+)"
)
_RE_FILE_SIZE_MULT = re.compile(r"File Size Mult:\s*(\d+)")
_RE_TEST_DIRS_SUMMARY = re.compile(r"^\s*TEST_DIRS:\s+(.+)$", re.MULTILINE)
_RE_THREADS_SUMMARY = re.compile(
    r"^Running the io-size/threads sweep on \d+ nodes for thread counts (.+)$",
    re.MULTILINE,
)
_RE_THREADS_LINE = re.compile(r"^\s*Threads:\s+(.+)$", re.MULTILINE)
_RE_IO_DEPTHS = re.compile(r"^\s*IO Depths:\s+(.+)$", re.MULTILINE)
_RE_IO_SIZES_SUMMARY = re.compile(r"^\s*IO Sizes:\s+(.+)$", re.MULTILINE)
_RE_IO_TYPE = re.compile(r"^\s*IO Type:\s+(DIO|BIO)\s*$", re.MULTILINE)
_RE_IO_PATTERN_SUMMARY = re.compile(r"^\s*IO Pattern:\s+(.+)$", re.MULTILINE)
_RE_FORCE_SINGLE_SUMMARY = re.compile(r"^\s*Force Single:\s+(Yes|No)\s*$", re.MULTILINE)
_RE_FORCE_SINGLE_ITER = re.compile(
    r"^\s*Force Single Run:\s+(Yes|No)\s*$", re.MULTILINE
)
_RE_WRITE_NO_READ_SUMMARY = re.compile(r"^\s*Write-no-read:\s*Yes\s*$", re.MULTILINE)
_RE_DATESTAMP_LINE = re.compile(r"^\s*Datestamp:\s+(\d{8}Z\d{6})\s*$", re.MULTILINE)
_RE_SINGLE_BIG_FILE_LINE = re.compile(r"^Single big file:\s+(.+)$", re.MULTILINE)
_RE_FILE_SIZE_SBF = re.compile(r"^File Size \(-s\):\s+(\S+)", re.MULTILINE)
_RE_FILE_SIZE_STD = re.compile(r"^File Size:\s+(\S+)", re.MULTILINE)
_RE_ALL_NODES = re.compile(
    r"^ELBENCHO_ALL_NODES_ACCESS_ALL_DATA:\s*([01])\s*$", re.MULTILINE
)
_RE_NOSVCSHARE = re.compile(r"--nosvcshare\b")

# Slurm step log: [<prefix>]elbencho-<DS>-<max>-<n>-<io>-<jobid>.out (basename only)
_SLURM_OUT_BASENAME_RE = re.compile(
    r"elbencho-(\d{8}Z\d{6})-(\d+)-(\d+)-(.+)-(\d+)\.out$"
)
# Benchmark: elbencho-<size>-c_<nodes>-s_<threads>[-d_<depth>]_<DS>.out
_BENCH_OUT_RE = re.compile(
    r"elbencho-([^-]+)-c_(\d+)-s_(\d+)(?:-d_(\d+))?_(\d{8}Z\d{6})\.out$"
)


def _eprint(*args: Any, **kwargs: Any) -> None:
    print(*args, file=sys.stderr, **kwargs)


def extract_ds_from_dirname(basename: str) -> Optional[str]:
    if not basename.startswith("elbencho-"):
        return None
    m = _DS_RE.search(basename)
    return m.group(1) if m else None


def infer_ds_from_logs(combined_text: str, paths: Sequence[str]) -> Optional[str]:
    """When dirname lacks elbencho-<DS>, recover DS from log bodies or filenames."""
    m = _RE_DATESTAMP_LINE.search(combined_text)
    if m:
        return m.group(1)
    for p in paths:
        bn = os.path.basename(p)
        m2 = re.search(r"elbencho-sweep-(\d{8}Z\d{6})-runner\.log$", bn)
        if m2:
            return m2.group(1)
        m3 = _BENCH_OUT_RE.search(bn)
        if m3:
            return m3.group(5)
        m4 = _SLURM_OUT_BASENAME_RE.search(bn)
        if m4:
            return m4.group(1)
    return None


def _yaml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _yaml_quote(s: str) -> str:
    return f'"{_yaml_escape(s)}"'


def _flow_seq(items: Sequence[str]) -> str:
    if not items:
        return "[]"
    inner = ", ".join(_yaml_quote(x) for x in items)
    return f"[{inner}]"


def collect_log_paths(result_dir: str) -> List[str]:
    paths: List[str] = []
    for pat in ("*.out", "*.log"):
        paths.extend(glob.glob(os.path.join(result_dir, pat)))
    return sorted(set(paths))


def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def parse_runner_jobs(text: str) -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    for m in _RUNNER_JOB_RE.finditer(text):
        out.append((int(m.group(1)), m.group(2).strip()))
    return out


def parse_sbatch_tail(
    text: str,
) -> List[Tuple[str, str, int, int, str, int, int, str]]:
    """output_dir, dio_or_bio, rand, single, io_size, write_only, write_no_read, read_from"""
    found: List[Tuple[str, str, int, int, str, int, int, str]] = []
    for line in text.splitlines():
        m = _SBATCH_TAIL_RE.search(line)
        if m:
            read_from = (m.group(8) or "").strip()
            found.append(
                (
                    m.group(1),
                    m.group(2),
                    int(m.group(3)),
                    int(m.group(4)),
                    m.group(5).strip(),
                    int(m.group(6)),
                    int(m.group(7)),
                    read_from,
                )
            )
            continue
        m = _SBATCH_TAIL_RE_LEGACY.search(line)
        if m:
            read_from = (m.group(7) or "").strip()
            found.append(
                (
                    m.group(1),
                    m.group(2),
                    int(m.group(3)),
                    int(m.group(4)),
                    m.group(5).strip(),
                    int(m.group(6)),
                    0,
                    read_from,
                )
            )
    return found


def _split_ws_list(s: str) -> List[str]:
    return [p for p in s.split() if p]


def _sweep_level_io_pattern_description(text: str) -> str:
    """First IO Pattern value from print_elbencho_sweep_summary (excludes per-iteration write=... lines)."""
    for m in _RE_IO_PATTERN_SUMMARY.finditer(text):
        frag = m.group(1).strip()
        if "=" in frag:
            continue
        return frag
    return ""


def _summary_implies_global_random(io_pattern: str) -> bool:
    return io_pattern.strip().startswith("Random")


def _summary_implies_global_sequential(io_pattern: str) -> bool:
    return io_pattern.strip().startswith("Sequential")


def _infer_rand_option(io_pattern_summary: str) -> int:
    """Match nv-elbencho-sweep: rand_option is -r, not r-prefix in ELBENCHO_SCALE_IO_SIZES."""
    if _summary_implies_global_random(io_pattern_summary):
        return 1
    if _summary_implies_global_sequential(io_pattern_summary):
        return 0
    return 0


def _parse_summary_threads(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    m = _RE_THREADS_SUMMARY.search(text)
    if m:
        out["ELBENCHO_SCALE_THREAD_LIST"] = _split_ws_list(m.group(1).strip())
        return out
    m = _RE_THREADS_LINE.search(text)
    if m:
        out["ELBENCHO_SCALE_THREAD_LIST"] = _split_ws_list(m.group(1).strip())
    return out


def _parse_summary_depths_and_sizes(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    m = _RE_IO_DEPTHS.search(text)
    if m:
        out["ELBENCHO_IODEPTH_LIST"] = _split_ws_list(m.group(1).strip())
    m = _RE_IO_SIZES_SUMMARY.search(text)
    if m:
        out["_summary_io_sizes"] = _split_ws_list(m.group(1).strip())
    m = _RE_FILE_SIZE_MULT.search(text)
    if m:
        out["ELBENCHO_FILE_SIZE_MULTIPLIER"] = int(m.group(1))
    m = _WR_IO_DURATION_LOG_RE.search(text)
    if m:
        out["ELBENCHO_SCALE_READ_WRITE_DURATION"] = int(m.group(1))
    m = _RE_TEST_DIRS_SUMMARY.search(text)
    if m:
        out["_test_dir_keys"] = _split_ws_list(m.group(1).strip())
    m = _RE_IO_TYPE.search(text)
    if m:
        out["dio_or_bio"] = "dio" if m.group(1) == "DIO" else "bio"
    return out


def _parse_summary_single_option_flags(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    m = _RE_FORCE_SINGLE_SUMMARY.search(text)
    if m:
        out["single_option"] = 1 if m.group(1) == "Yes" else 0
    m = _RE_FORCE_SINGLE_ITER.search(text)
    if m and "single_option" not in out:
        out["single_option"] = 1 if m.group(1) == "Yes" else 0
    if _RE_WRITE_NO_READ_SUMMARY.search(text):
        out["sweep_write_no_read"] = 1
    return out


def parse_summary_block(text: str) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    d.update(_parse_summary_threads(text))
    d.update(_parse_summary_depths_and_sizes(text))
    iop = _sweep_level_io_pattern_description(text)
    if iop:
        d["_io_pattern_summary"] = iop
    d.update(_parse_summary_single_option_flags(text))
    return d


def parse_iteration_signals(text: str) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    if _RE_SINGLE_BIG_FILE_LINE.search(text):
        d["ELBENCHO_SINGLE_BIG_FILE"] = 1
        m = _RE_SINGLE_BIG_FILE_LINE.search(text)
        if m:
            path = m.group(1).strip()
            d["_single_big_file_path"] = path
            d["ELBENCHO_SINGLE_BIG_FILE_BASENAME"] = os.path.basename(path)
    m = _RE_FILE_SIZE_SBF.search(text)
    if m:
        d["ELBENCHO_SINGLE_BIG_FILE_SIZE"] = m.group(1).strip()
        d["ELBENCHO_SINGLE_BIG_FILE"] = 1
    m = _RE_FILE_SIZE_STD.search(text)
    if m and "ELBENCHO_SINGLE_BIG_FILE_SIZE" not in d:
        # Tree mode file size; only fill SBF size if SBF already detected
        if d.get("ELBENCHO_SINGLE_BIG_FILE") == 1:
            d["ELBENCHO_SINGLE_BIG_FILE_SIZE"] = m.group(1).strip()
    m = _RE_ALL_NODES.search(text)
    if m:
        d["ELBENCHO_ALL_NODES_ACCESS_ALL_DATA"] = int(m.group(1))
    if _RE_NOSVCSHARE.search(text):
        d["ELBENCHO_ALL_NODES_ACCESS_ALL_DATA"] = 1
    wo = _RE_WRITE_ONLY_DATA_DIR.search(text)
    if wo:
        d["sweep_write_only"] = 1
        d["_write_only_data_dir"] = wo.group(1).strip()
    rf = _RE_SWEEP_READ_FROM.search(text)
    if rf:
        d["sweep_read_from"] = rf.group(1).strip()
    return d


def dirname_hints(basename: str) -> Dict[str, Any]:
    h: Dict[str, Any] = {}
    lower = basename.lower()
    if "write-only" in lower:
        h["_dirname_write_only_hint"] = 1
    # e.g. -16G-write-only or -1023M-write-only
    m = re.search(r"-(\d+[KMGT]i?)-write-only", basename, re.I)
    if m:
        h["_dirname_sbf_size_hint"] = m.group(1).upper().replace("I", "i")
    return h


def parse_slurm_out_filenames(paths: Iterable[str], ds: str) -> Dict[str, Any]:
    ios: List[str] = []
    nodes_order: List[int] = []
    for p in paths:
        base = os.path.basename(p)
        m = _SLURM_OUT_BASENAME_RE.search(base)
        if not m or m.group(1) != ds:
            continue
        n, io = m.group(3), m.group(4)
        ios.append(io)
        nodes_order.append(int(n))
    d: Dict[str, Any] = {}
    if ios:
        d["_slurm_io_sizes_seen"] = ios
    if nodes_order:
        d["_slurm_nodes_seen_order"] = nodes_order
    return d


def _parse_one_benchmark_basename(
    base: str, ds: str
) -> Optional[Tuple[str, Optional[str]]]:
    """Return (thread, depth) for a matching benchmark out basename, else None."""
    m = _BENCH_OUT_RE.search(base)
    if not m:
        return None
    if m.group(5) != ds:
        return None
    thread = str(int(m.group(3)))
    dep = m.group(4)
    depth: Optional[str] = str(int(dep)) if dep is not None else None
    return (thread, depth)


def parse_benchmark_filenames(paths: Iterable[str], ds: str) -> Dict[str, Any]:
    threads: List[str] = []
    depths: List[str] = []
    for p in paths:
        base = os.path.basename(p)
        found = _parse_one_benchmark_basename(base, ds)
        if not found:
            continue
        t_name, d_val = found
        threads.append(t_name)
        if d_val is not None:
            depths.append(d_val)
    d: Dict[str, Any] = {}
    if threads:
        d["_bench_threads"] = _unique_preserve(threads)
    if depths:
        d["_bench_depths"] = _unique_preserve(depths)
    return d


def _unique_preserve(seq: Sequence[Any]) -> List[Any]:
    seen = set()
    out: List[Any] = []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# Keys merged from per-file iteration log lines (not leading-underscore internal keys).
_ITERATION_MERGE_KEYS = frozenset(
    {
        "ELBENCHO_SINGLE_BIG_FILE",
        "ELBENCHO_SINGLE_BIG_FILE_BASENAME",
        "ELBENCHO_SINGLE_BIG_FILE_SIZE",
        "ELBENCHO_ALL_NODES_ACCESS_ALL_DATA",
        "sweep_write_only",
        "sweep_write_no_read",
        "sweep_read_from",
    }
)


SbatchRow = Tuple[str, str, int, int, str, int, int, str]


def _merge_apply_runner(merged: Dict[str, Any], combined_text: str) -> None:
    runner_pairs = parse_runner_jobs(combined_text)
    if not runner_pairs:
        return
    nodes_u = _unique_preserve([p[0] for p in runner_pairs])
    merged["nodes_spec"] = ",".join(str(n) for n in nodes_u)
    ios_u = _unique_preserve([p[1] for p in runner_pairs])
    merged["ELBENCHO_SCALE_IO_SIZES"] = ios_u


def _merge_apply_sbatch(
    merged: Dict[str, Any], sb_rows: Sequence[SbatchRow], warn: List[str]
) -> None:
    if not sb_rows:
        return
    first = sb_rows[0]
    dio, rand, single = first[1], first[2], first[3]
    wo, wnr, rf = first[5], first[6], first[7]
    merged.setdefault("dio_or_bio", dio)
    merged.setdefault("rand_option", rand)
    merged.setdefault("single_option", single)
    if wo:
        merged["sweep_write_only"] = 1
    if wnr:
        merged["sweep_write_no_read"] = 1
    if rf:
        merged["sweep_read_from"] = rf
    f2, f3, f5, f6 = first[2], first[3], first[5], first[6]
    for row in sb_rows[1:]:
        if (row[2], row[3], row[5], row[6]) == (f2, f3, f5, f6):
            continue
        warn.append(
            "Conflicting sbatch sweep args across submissions "
            f"(first {first[2:7]}, saw {row[2:7]})"
        )
        return


def _merge_summary_block_into(
    merged: Dict[str, Any], combined_text: str, warn: List[str]
) -> None:
    summ = parse_summary_block(combined_text)
    for k, v in summ.items():
        if k.startswith("_"):
            merged[k] = v
            continue
        if k == "single_option" and k in merged and merged[k] != v:
            warn.append(
                f"single_option: summary {v} vs earlier {merged[k]} (keeping summary)"
            )
        merged[k] = v


def _merge_iteration_files_into_merged(
    merged: Dict[str, Any], per_file: Sequence[Tuple[str, str]], warn: List[str]
) -> None:
    for _, text in per_file:
        it = parse_iteration_signals(text)
        for k, v in it.items():
            if k.startswith("_"):
                merged[k] = v
                continue
            if k not in _ITERATION_MERGE_KEYS:
                continue
            if k in merged and merged[k] != v:
                warn.append(
                    f"{k}: overriding {merged[k]!r} with {v!r} from iteration text"
                )
            merged[k] = v


def _merge_datestamp_parsed_names(
    merged: Dict[str, Any], per_file: Sequence[Tuple[str, str]], ds: str
) -> None:
    paths = [p for p, _ in per_file]
    merged.update(parse_slurm_out_filenames(paths, ds))
    merged.update(parse_benchmark_filenames(paths, ds))


def _apply_dirname_write_only_hint(
    merged: Dict[str, Any],
    combined_text: str,
    sb_rows: Sequence[SbatchRow],
    warn: List[str],
) -> None:
    if (
        not merged.get("_dirname_write_only_hint")
        or merged.get("sweep_write_only") == 1
    ):
        return
    if sb_rows and sb_rows[0][5] == 1:
        merged["sweep_write_only"] = 1
        return
    if (
        "ELBENCHO_WRITE_ONLY_DATA_DIR=" in combined_text
        or _RE_WRITE_ONLY_DATA_DIR.search(combined_text)
    ):
        merged["sweep_write_only"] = 1
        return
    warn.append(
        "Directory name suggests write-only but logs did not confirm; "
        "not setting sweep_write_only"
    )


def _reconcile_io_sizes(merged: Dict[str, Any], warn: List[str]) -> None:
    summ_ios = merged.get("_summary_io_sizes")
    runner_ios = merged.get("ELBENCHO_SCALE_IO_SIZES")
    if not summ_ios:
        return
    if not runner_ios:
        merged["ELBENCHO_SCALE_IO_SIZES"] = list(summ_ios)
        return
    if summ_ios != runner_ios:
        warn.append(
            f"ELBENCHO_SCALE_IO_SIZES runner {runner_ios} != summary {summ_ios} (keeping runner order)"
        )


def _fill_nodes_spec_from_slurm_order(merged: Dict[str, Any]) -> None:
    if "nodes_spec" in merged or not merged.get("_slurm_nodes_seen_order"):
        return
    merged["nodes_spec"] = ",".join(
        str(x) for x in _unique_preserve(merged["_slurm_nodes_seen_order"])
    )


def _fill_thread_depth_from_benchmark(merged: Dict[str, Any]) -> None:
    if "ELBENCHO_SCALE_THREAD_LIST" not in merged and merged.get("_bench_threads"):
        merged["ELBENCHO_SCALE_THREAD_LIST"] = merged["_bench_threads"]
    if "ELBENCHO_IODEPTH_LIST" not in merged and merged.get("_bench_depths"):
        merged["ELBENCHO_IODEPTH_LIST"] = merged["_bench_depths"]


def _apply_rand_option_final(merged: Dict[str, Any], warn: List[str]) -> None:
    pat = merged.get("_io_pattern_summary") or ""
    if "rand_option" not in merged:
        merged["rand_option"] = _infer_rand_option(pat)
        return
    inferred = _infer_rand_option(pat)
    if inferred == int(merged["rand_option"]):
        return
    r_opt = int(merged["rand_option"])
    if _summary_implies_global_sequential(pat) and r_opt == 1:
        warn.append(
            "rand_option: sbatch 1 but sweep summary is Sequential; keeping sbatch"
        )
        return
    if _summary_implies_global_random(pat) and r_opt == 0:
        warn.append("rand_option: sbatch 0 but sweep summary is Random; keeping sbatch")


def _default_single_big_file_fields(merged: Dict[str, Any]) -> None:
    if merged.get("ELBENCHO_SINGLE_BIG_FILE") != 1:
        merged["ELBENCHO_SINGLE_BIG_FILE"] = 0
        if "ELBENCHO_SINGLE_BIG_FILE_BASENAME" not in merged:
            merged["ELBENCHO_SINGLE_BIG_FILE_BASENAME"] = "elbencho-bigfile"
        if "ELBENCHO_SINGLE_BIG_FILE_SIZE" not in merged:
            merged["ELBENCHO_SINGLE_BIG_FILE_SIZE"] = ""
    else:
        if "ELBENCHO_SINGLE_BIG_FILE_BASENAME" not in merged:
            merged["ELBENCHO_SINGLE_BIG_FILE_BASENAME"] = "elbencho-bigfile"
        if "ELBENCHO_SINGLE_BIG_FILE_SIZE" not in merged:
            merged["ELBENCHO_SINGLE_BIG_FILE_SIZE"] = (
                merged.get("_dirname_sbf_size_hint") or ""
            )


def _default_merged_scalars(merged: Dict[str, Any]) -> None:
    if "ELBENCHO_ALL_NODES_ACCESS_ALL_DATA" not in merged:
        merged["ELBENCHO_ALL_NODES_ACCESS_ALL_DATA"] = 0
    if "sweep_write_only" not in merged:
        merged["sweep_write_only"] = 0
    if "sweep_write_no_read" not in merged:
        merged["sweep_write_no_read"] = 0
    if "sweep_read_from" not in merged:
        merged["sweep_read_from"] = ""
    if "single_option" not in merged:
        merged["single_option"] = 0
    if "dio_or_bio" not in merged:
        merged["dio_or_bio"] = "dio"
    if "ELBENCHO_READ_AFTER_WRITE_PAUSE" not in merged:
        merged["ELBENCHO_READ_AFTER_WRITE_PAUSE"] = 0


def _set_test_dirs_placeholder(merged: Dict[str, Any]) -> None:
    test_keys: List[str] = list(merged.get("_test_dir_keys") or [])
    if not test_keys and merged.get("_write_only_data_dir"):
        parent = os.path.dirname(merged["_write_only_data_dir"])
        if parent:
            test_keys = [parent]
    merged["TEST_DIRS"] = dict.fromkeys(test_keys, 1)


def merge_fields(
    combined_text: str,
    per_file: Sequence[Tuple[str, str]],
    result_dir: str,
    ds: Optional[str],
    warn: List[str],
) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    basename = os.path.basename(result_dir.rstrip(os.sep))
    merged.update(dirname_hints(basename))
    _merge_apply_runner(merged, combined_text)
    sb_rows: List[SbatchRow] = parse_sbatch_tail(combined_text)
    _merge_apply_sbatch(merged, sb_rows, warn)
    _merge_summary_block_into(merged, combined_text, warn)
    _merge_iteration_files_into_merged(merged, per_file, warn)
    if ds:
        _merge_datestamp_parsed_names(merged, per_file, ds)
    _apply_dirname_write_only_hint(merged, combined_text, sb_rows, warn)
    _reconcile_io_sizes(merged, warn)
    _fill_nodes_spec_from_slurm_order(merged)
    _fill_thread_depth_from_benchmark(merged)
    _apply_rand_option_final(merged, warn)
    _default_single_big_file_fields(merged)
    _default_merged_scalars(merged)
    _set_test_dirs_placeholder(merged)
    return merged


def _flow_seq_for_merged_key(merged: Dict[str, Any], key: str) -> str:
    val = merged.get(key) or []
    if isinstance(val, list):
        return _flow_seq([str(x) for x in val])
    return _flow_seq([str(val)])


def _build_yaml_preamble(
    now: str, warnings: Sequence[str], source_files: Sequence[str]
) -> List[str]:
    lines: List[str] = [
        "# env_used.yaml - elbencho sweep configuration snapshot",
        f"# Generated: {now}",
        "#",
        f"# WARNING: RECONSTRUCTED by {SCRIPT_NAME} (not from write_elbencho_env_used).",
        "# Values may be wrong or incomplete. Not recoverable from logs alone: "
        "TEST_DIRS numeric tiers, FS_MAX_* limits, exact nodes_spec range syntax, "
        "ELBENCHO_READ_AFTER_WRITE_PAUSE (often defaulted to 0).",
    ]
    if warnings:
        lines.append("# Parser warnings:")
        for w in warnings:
            lines.append(f"#   - {w}")
    if not source_files:
        lines.append("")
        return lines
    show = source_files[:25]
    lines.append("# Sources (sample):" if len(source_files) > 25 else "# Sources:")
    for p in show:
        lines.append(f"#   - {p}")
    if len(source_files) > 25:
        lines.append(f"#   - ... and {len(source_files) - 25} more")
    lines.append("")
    return lines


def _build_yaml_test_dirs_lines(merged: Dict[str, Any]) -> List[str]:
    td = merged.get("TEST_DIRS") or {}
    out = ["TEST_DIRS:"]
    if not td:
        out.append("  {}")
        return out
    for path in sorted(td.keys()):
        out.append(f"  {_yaml_quote(path)}: {td[path]}")
    return out


def _build_yaml_elbencho_value_lines(merged: Dict[str, Any]) -> List[str]:
    g = merged.get
    sbf_base = g("ELBENCHO_SINGLE_BIG_FILE_BASENAME")
    sbf_basename = str(sbf_base) if sbf_base else "elbencho-bigfile"
    sbf_size = g("ELBENCHO_SINGLE_BIG_FILE_SIZE")
    sbf_size_q = _yaml_quote("" if sbf_size is None else str(sbf_size))
    return [
        f"ELBENCHO_SCALE_THREAD_LIST: "
        f"{_flow_seq_for_merged_key(merged, 'ELBENCHO_SCALE_THREAD_LIST')}",
        f"ELBENCHO_SCALE_IO_SIZES: "
        f"{_flow_seq_for_merged_key(merged, 'ELBENCHO_SCALE_IO_SIZES')}",
        f"ELBENCHO_IODEPTH_LIST: "
        f"{_flow_seq_for_merged_key(merged, 'ELBENCHO_IODEPTH_LIST')}",
        f"ELBENCHO_FILE_SIZE_MULTIPLIER: {g('ELBENCHO_FILE_SIZE_MULTIPLIER', 1024)}",
        f"ELBENCHO_SCALE_READ_WRITE_DURATION: "
        f"{g('ELBENCHO_SCALE_READ_WRITE_DURATION', 0)}",
        f"ELBENCHO_READ_AFTER_WRITE_PAUSE: {g('ELBENCHO_READ_AFTER_WRITE_PAUSE', 0)}",
        "",
        f"ELBENCHO_SINGLE_BIG_FILE: {g('ELBENCHO_SINGLE_BIG_FILE', 0)}",
        f"ELBENCHO_SINGLE_BIG_FILE_BASENAME: {_yaml_quote(sbf_basename)}",
        f"ELBENCHO_SINGLE_BIG_FILE_SIZE: {sbf_size_q}",
        f"ELBENCHO_ALL_NODES_ACCESS_ALL_DATA: {g('ELBENCHO_ALL_NODES_ACCESS_ALL_DATA', 0)}",
        "",
        f'dio_or_bio: {_yaml_quote(str(g("dio_or_bio") or "dio"))}',
        f"rand_option: {int(g('rand_option', 0))}",
        f"single_option: {int(g('single_option', 0))}",
        f"sweep_write_only: {int(g('sweep_write_only', 0))}",
        f"sweep_write_no_read: {int(g('sweep_write_no_read', 0))}",
        f'sweep_read_from: {_yaml_quote(str(g("sweep_read_from") or ""))}',
        f'nodes_spec: {_yaml_quote(str(g("nodes_spec") or ""))}',
        "",
    ]


def build_yaml(
    merged: Dict[str, Any],
    source_files: Sequence[str],
    warnings: Sequence[str],
) -> str:
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out = _build_yaml_preamble(now, warnings, source_files)
    out.extend(_build_yaml_test_dirs_lines(merged))
    out.append("")
    out.append(
        "# FS_MAX_AGG_THROUGHPUT / FS_MAX_NODE_THROUGHPUT_GBPS / FS_MAX_NODE_IOPS omitted "
        "(not present in sweep logs)."
    )
    out.append("")
    out.extend(_build_yaml_elbencho_value_lines(merged))
    return "\n".join(out)


def atomic_write(path: str, data: str) -> None:
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".env_used.", suffix=".yaml.tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def process_dir(result_dir: str, index: int, total: int) -> None:
    result_dir = os.path.abspath(result_dir)
    out_path = os.path.join(result_dir, "env_used.yaml")
    _eprint(f"[{index}/{total}] Processing {result_dir}")

    if os.path.isfile(out_path):
        _eprint(f"Notice: skipping {result_dir} (env_used.yaml already exists)")
        return

    base = os.path.basename(result_dir)
    if not base.startswith("elbencho-"):
        _eprint(f"Warning: {base} does not start with elbencho-; parsing anyway")

    paths = collect_log_paths(result_dir)
    if not paths:
        _eprint(f"Error: no *.out or *.log in {result_dir}; skipping write")
        return

    per_file: List[Tuple[str, str]] = []
    for p in paths:
        try:
            per_file.append((p, read_text(p)))
        except OSError as e:
            _eprint(f"Warning: could not read {p}: {e}")

    combined = "\n\n".join(t for _p, t in per_file)

    ds = extract_ds_from_dirname(base)
    if not ds:
        ds = infer_ds_from_logs(combined, [os.path.basename(p) for p, _ in per_file])
    if not ds:
        _eprint(
            f"Warning: could not parse datestamp from directory name {base!r} or logs"
        )
    warn_list: List[str] = []
    merged = merge_fields(combined, per_file, result_dir, ds, warn_list)

    if not merged.get("ELBENCHO_SCALE_THREAD_LIST"):
        warn_list.append("ELBENCHO_SCALE_THREAD_LIST empty — check logs")
    if not merged.get("ELBENCHO_SCALE_IO_SIZES"):
        warn_list.append("ELBENCHO_SCALE_IO_SIZES empty — check logs")
    if not merged.get("ELBENCHO_IODEPTH_LIST"):
        warn_list.append("ELBENCHO_IODEPTH_LIST empty — check logs")
    if not merged.get("nodes_spec"):
        warn_list.append("nodes_spec empty — check logs")

    for w in warn_list:
        _eprint(f"Warning: {w}")

    yaml_out = build_yaml(merged, [os.path.basename(p) for p, _ in per_file], warn_list)
    atomic_write(out_path, yaml_out)
    _eprint(f"Wrote {out_path}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reconstruct env_used.yaml for legacy elbencho sweep result directories "
            "by parsing *.out and *.log (stdlib only)."
        )
    )
    parser.add_argument(
        "directories",
        nargs="+",
        help="One or more elbencho-<DS> result directories",
    )
    args = parser.parse_args(argv)

    dirs = args.directories
    total = len(dirs)
    for i, d in enumerate(dirs, start=1):
        if not os.path.isdir(d):
            _eprint(f"Error: not a directory: {d}")
            continue
        process_dir(d, i, total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
