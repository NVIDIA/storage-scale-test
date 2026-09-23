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

"""Fast decision-partition tests for the filesystem sweep contract."""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_BASE = _REPO_ROOT / "lib" / "env_base.sh"
_ENV_FUNCTIONS = _REPO_ROOT / "lib" / "env_functions.sh"
_ELBENCHO_FUNCTIONS = _REPO_ROOT / "lib" / "_elbencho_functions.sh"
_SWEEP = _REPO_ROOT / "storage-tests" / "fs" / "nv-elbencho-sweep.sh"
_BASH = shutil.which("bash") or "bash"


def _run_bash(script: str) -> subprocess.CompletedProcess[str]:
    """Run a dedented Bash contract harness."""
    return subprocess.run(
        [_BASH, "-c", textwrap.dedent(script)],
        check=False,
        cwd=_REPO_ROOT,
        text=True,
        capture_output=True,
    )


def test_node_specification_equivalence_classes_and_boundaries():
    """Lists and stepped ranges retain order while malformed forms fail."""
    result = _run_bash(f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        diff -u <(printf '3\n5\n7\n9\n10\n13\n16\n8\n8\n') \
            <(parse_range_specification '3-10+2,13,16,8,8')
        for invalid in '' ',1' '1,' '1,,2' '0' '3-1' '1-3+0' '1-x'; do
            ! parse_range_specification "$invalid" >/dev/null 2>&1
        done
        """)
    assert result.returncode == 0, result.stderr


def test_cartesian_reification_has_stable_order_and_rotation(tmp_path):
    """Every coordinate is unique and numbered in documented loop order."""
    result = _run_bash(f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        source "{_ELBENCHO_FUNCTIONS}"
        declare -A TEST_DIRS=(["{tmp_path}/data"]=1)
        generate_fs_test_directories() {{ printf '%s/target\n' "{tmp_path}"; }}
        DS=20260920Z010203
        nodes_spec='2,1-3+2'
        rand_option=0
        ELBENCHO_SCALE_IO_SIZES=(4K r8K)
        ELBENCHO_SCALE_THREAD_LIST=(1 2)
        ELBENCHO_IODEPTH_LIST=(1 4)
        ELBENCHO_SCALE_READ_WRITE_DURATION=1
        ELBENCHO_LIVE_CSV_EXTENDED=0
        ELBENCHO_FILE_SIZE_MULTIPLIER=1
        ELBENCHO_FILE_LAYOUT=worker-directories
        ELBENCHO_FILES_PER_NODE=
        ELBENCHO_FILE_SIZE=1M
        ELBENCHO_READ_AFTER_WRITE_PAUSE=0
        FS_MAX_AGG_THROUGHPUT=1
        FS_MAX_NODE_THROUGHPUT_GBPS=1
        FS_MAX_NODE_IOPS=1
        ELBENCHO_SINGLE_BIG_FILE=0
        ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0
        mkdir -p "{tmp_path}/result"
        reify_all_elbencho_executions \
            "{tmp_path}/result" "$nodes_spec" dio 0 0 0 0 ''
        [[ $(find "{tmp_path}/result/executions" -name '*.sh' | wc -l) -eq 24 ]]
        [[ $(find "{tmp_path}/result/executions" -name '*.status' | wc -l) -eq 24 ]]
        source "{tmp_path}/result/executions/0001.sh"
        [[ "$nodes:$io_size:$thread_count:$io_depth:$ELBENCHO_READ_HOST_ROTATE_STEPS" == \
            '2:4K:1:1:0' ]]
        source "{tmp_path}/result/executions/0008.sh"
        [[ "$nodes:$io_size:$thread_count:$io_depth:$ELBENCHO_READ_HOST_ROTATE_STEPS" == \
            '2:r8K:2:4:7' ]]
        source "{tmp_path}/result/executions/0024.sh"
        [[ "$nodes:$io_size:$thread_count:$io_depth:$ELBENCHO_READ_HOST_ROTATE_STEPS" == \
            '3:r8K:2:4:23' ]]
        [[ $(sort -u "{tmp_path}/result/executions/"*.status) == PENDING ]]
        """)
    assert result.returncode == 0, result.stderr


def test_env_precedence_selects_test_dirs_ssh_and_order_aliases(tmp_path):
    """Populated TEST_DIRS wins and SSH disables otherwise valid Slurm state."""
    hosts = tmp_path / "hosts"
    hosts.write_text("# comment\nhost-a, host-b\n\nhost-a\n", encoding="utf-8")
    result = _run_bash(f"""
        set -e
        SCALE_TEST_BASE="{_REPO_ROOT}"
        RESULTS_DIR="{tmp_path}/results"
        LOGS_DIR="{tmp_path}/logs"
        TEST_DIR="{tmp_path}/legacy"
        declare -A TEST_DIRS=(["{tmp_path}/preferred"]=2)
        OBJ_BUCKET=
        OBJ_AUTH_FILE="{tmp_path}/missing-auth"
        EXECUTION_SUBSTRATE=ssh
        SSH_HOST_LIST="{hosts}"
        SSH_USER=tester
        ORDER_NODES=YeS
        client_type=cpu
        client_arch=x86_64
        source "{_ENV_BASE}"
        [[ "${{#TEST_DIRS[@]}}" -eq 1 ]]
        [[ "${{TEST_DIRS[{tmp_path}/preferred]}}" == 2 ]]
        [[ -z "${{TEST_DIRS[{tmp_path}/legacy]+x}}" ]]
        [[ "$SSH_ENABLED" == 1 && -z "$SLURM_ENABLED" ]]
        [[ "$ORDER_NODES_ENABLED" == 1 ]]
        [[ "${{SSH_ALL_HOSTS[*]}}" == 'host-a host-b host-a' ]]
        """)
    assert result.returncode == 0, result.stderr


def test_env_fallbacks_apply_only_when_primary_values_are_empty(tmp_path):
    """Legacy directory and throughput names fill gaps without overriding values."""
    hosts = tmp_path / "hosts"
    hosts.write_text("host-a\n", encoding="utf-8")
    result = _run_bash(f"""
        set -e
        SCALE_TEST_BASE="{_REPO_ROOT}"
        RESULTS_DIR="{tmp_path}/results"
        LOGS_DIR="{tmp_path}/logs"
        TEST_DIR="{tmp_path}/legacy"
        declare -A TEST_DIRS=()
        OBJ_BUCKET=
        OBJ_AUTH_FILE="{tmp_path}/missing-auth"
        EXECUTION_SUBSTRATE=ssh
        SSH_HOST_LIST="{hosts}"
        ORDER_NODES=off
        client_type=cpu
        client_arch=x86_64
        IOR_FS_MAX_AGG_THROUGHPUT=27
        FS_MAX_AGG_THROUGHPUT=19
        source "{_ENV_BASE}"
        [[ "${{#TEST_DIRS[@]}}" -eq 1 ]]
        [[ "${{TEST_DIRS[{tmp_path}/legacy]}}" == 1 ]]
        [[ -z "$ORDER_NODES_ENABLED" ]]
        [[ "$FS_MAX_AGG_THROUGHPUT" == 19 ]]
        """)
    assert result.returncode == 0, result.stderr


def test_ssh_selection_contract_covers_ordered_and_unordered_modes():
    """Ordered selection is a prefix; unordered selection is unique and bounded."""
    result = _run_bash(f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        SSH_ENABLED=1
        SSH_ALL_HOSTS=(host-a host-b host-c host-d)
        ORDER_NODES_ENABLED=1
        choose_N_ssh_hosts 2
        [[ "$SSH_NODELIST" == host-a,host-b ]]
        ORDER_NODES_ENABLED=
        RANDOM=7
        choose_N_ssh_hosts 3
        IFS=, read -ra selected <<<"$SSH_NODELIST"
        [[ "${{#selected[@]}}" -eq 3 ]]
        [[ $(printf '%s\n' "${{selected[@]}}" | sort -u | wc -l) -eq 3 ]]
        for host in "${{selected[@]}}"; do
            [[ " ${{SSH_ALL_HOSTS[*]}} " == *" $host "* ]]
        done
        ! choose_N_ssh_hosts 0 >/dev/null 2>&1
        ! choose_N_ssh_hosts 5 >/dev/null 2>&1
        """)
    assert result.returncode == 0, result.stderr


def test_read_and_delete_path_guards_resolve_symlinks(tmp_path):
    """Read permits the root, while deletion requires a real strict descendant."""
    root = tmp_path / "root"
    child = root / "child"
    outside = tmp_path / "outside"
    child.mkdir(parents=True)
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    result = _run_bash(f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        declare -A TEST_DIRS=(["{root}"]=1)
        validate_elbencho_sweep_read_from_path "{root}"
        validate_elbencho_sweep_read_from_path "{child}"
        validate_elbencho_sweep_delete_only_path "{child}"
        ! validate_elbencho_sweep_delete_only_path "{root}" >/dev/null 2>&1
        ! validate_elbencho_sweep_delete_only_path "{outside}" >/dev/null 2>&1
        ! validate_elbencho_sweep_delete_only_path \
            "{root}/escape/victim" >/dev/null 2>&1
        ! validate_elbencho_sweep_read_from_path \
            "{root}/escape/input" >/dev/null 2>&1
        """)
    assert result.returncode == 0, result.stderr


def test_single_big_file_validation_partitions(tmp_path):
    """Single-file mode enforces one root, sequential I/O, and extent rules."""
    result = _run_bash(f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        declare -A TEST_DIRS=(["{tmp_path}"]=1)
        ELBENCHO_SINGLE_BIG_FILE=1
        ELBENCHO_SCALE_IO_SIZES=(4K '1M,4K')
        ELBENCHO_SINGLE_BIG_FILE_SIZE=16M
        validate_elbencho_single_big_file_env ''
        ELBENCHO_SINGLE_BIG_FILE_SIZE=
        validate_elbencho_single_big_file_env "{tmp_path}/existing-file"
        ! validate_elbencho_single_big_file_env '' >/dev/null 2>&1
        ELBENCHO_SINGLE_BIG_FILE_SIZE=16M
        ELBENCHO_SCALE_IO_SIZES=(r4K)
        ! validate_elbencho_single_big_file_env '' >/dev/null 2>&1
        ELBENCHO_SCALE_IO_SIZES=(4K)
        TEST_DIRS["{tmp_path}/second"]=1
        ! validate_elbencho_single_big_file_env '' >/dev/null 2>&1
        """)
    assert result.returncode == 0, result.stderr


def test_workload_precedence_and_incompatible_values(tmp_path):
    """Only active branch inputs affect sizing and workload validation."""
    result = _run_bash(f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        source "{_ELBENCHO_FUNCTIONS}"
        declare -A TEST_DIRS=(["{tmp_path}"]=1)
        ELBENCHO_SCALE_THREAD_LIST=(1)
        ELBENCHO_SCALE_IO_SIZES=('6K,r4K')
        ELBENCHO_FILE_LAYOUT=worker-directories
        ELBENCHO_FILES_PER_NODE=
        ELBENCHO_FILE_SIZE=6K
        ELBENCHO_FILE_SIZE_MULTIPLIER=1024
        ELBENCHO_SINGLE_BIG_FILE=0
        ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0
        sweep_write_only=0
        sweep_write_no_read=0
        [[ $(_elbencho_resolve_generated_file_size 4K) == 6K ]]
        ! validate_elbencho_sweep_workload_mode bio 0 '' >/dev/null 2>&1
        sweep_write_only=1
        validate_elbencho_sweep_workload_mode bio 0 ''
        ELBENCHO_FILE_SIZE=
        [[ $(_elbencho_resolve_generated_file_size 4K) == 4M ]]
        sweep_write_only=0
        ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=1
        ! validate_elbencho_sweep_workload_mode dio 0 '' >/dev/null 2>&1
        ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0
        ELBENCHO_FILE_LAYOUT=shared-directory
        ELBENCHO_FILES_PER_NODE=3
        ELBENCHO_SCALE_THREAD_LIST=(2)
        ELBENCHO_SCALE_IO_SIZES=(4K)
        ELBENCHO_FILE_SIZE=4K
        ! validate_elbencho_sweep_workload_mode dio 0 '' >/dev/null 2>&1
        ELBENCHO_FILES_PER_NODE=4
        validate_elbencho_sweep_workload_mode dio 0 ''
        TEST_DIRS["{tmp_path}/second"]=1
        ! validate_elbencho_sweep_workload_mode dio 0 '' >/dev/null 2>&1
        """)
    assert result.returncode == 0, result.stderr


def test_weighted_sizing_uses_the_tightest_capacity_limit():
    """IOPS and aggregate bandwidth limits independently bound file counts."""
    result = _run_bash(f"""
        set -e
        source "{_ELBENCHO_FUNCTIONS}"
        FS_MAX_NODE_THROUGHPUT_GBPS=8
        FS_MAX_AGG_THROUGHPUT=100
        FS_MAX_NODE_IOPS=512
        [[ $(compute_target_file_count_per_thread 4K 1M 2 1 1) -eq 2 ]]
        FS_MAX_NODE_IOPS=999999999
        FS_MAX_AGG_THROUGHPUT=1
        [[ $(compute_target_file_count_per_thread 4K 1M 2 1 1) -eq 512 ]]
        """)
    assert result.returncode == 0, result.stderr


def test_slurm_command_boundaries_preserve_spaced_arguments(tmp_path):
    """Scheduler arrays retain reservations, GPU requests, and one spaced value."""
    for name in ("sbatch", "srun"):
        executable = tmp_path / name
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    result = _run_bash(f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        PATH="{tmp_path}:$PATH"
        _SBATCH_OPTIONS_BASE='-A account-a -p batch --exclusive=user'
        _SRUN_OPTIONS_BASE='-A account-a -p batch --exclusive=user'
        SLURM_EXTRA_ARGS=(--qos=normal '--comment=integration scenario')
        declare -a batch run
        build_sbatch_cmd batch
        build_srun_cmd run
        [[ "${{batch[-1]}}" == '--comment=integration scenario' ]]
        [[ "${{run[-1]}}" == '--comment=integration scenario' ]]
        [[ "${{batch[-2]}}" == --qos=normal ]]
        account=account-a
        partition=gpu
        run_time=00:05:00
        reservation=reserved-a
        SLURM_GPUS_PER_NODE_OPT=--gpus-per-node=4
        build_srun_validate_scale_cmd run
        [[ " ${{run[*]}} " == *' --reservation reserved-a '* ]]
        [[ " ${{run[*]}} " == *' --gpus-per-node=4 '* ]]
        [[ "${{run[-1]}}" == '--comment=integration scenario' ]]
        SLURM_JOB_NAME_PREFIX='project-'
        [[ $(make_sbatch_job_name elbencho 20260920Z010203 2-1) == \
            project-elbencho-20260920Z010203-2-1 ]]
        """)
    assert result.returncode == 0, result.stderr


def test_exclusive_user_cpu_discovery_uses_smallest_target():
    """Exclusive-user scheduling requests a capacity valid on every target."""
    result = _run_bash(f"""
        set -e
        source "{_ENV_FUNCTIONS}"
        scontrol() {{ printf 'node-a\nnode-b\n'; }}
        sinfo() {{ printf '16\n4\n8\n'; }}
        SLURM_INCLUDE_COUNT=2
        SLURM_INCLUDES=--nodelist=node-a,node-b
        [[ $(get_slurm_target_node_cpus) == 4 ]]
        sinfo() {{ printf 'unknown\n'; }}
        ! get_slurm_target_node_cpus >/dev/null 2>&1
        """)
    assert result.returncode == 0, result.stderr


def _make_sweep_fixture(tmp_path: Path) -> Path:
    """Create a minimal deployment for parser-only sweep invocations."""
    script_dir = tmp_path / "storage-tests" / "fs"
    script_dir.mkdir(parents=True)
    sweep = script_dir / _SWEEP.name
    shutil.copy2(_SWEEP, sweep)
    kubectl_dir = script_dir / "kubectl"
    kubectl_dir.mkdir()
    shutil.copy2(
        _REPO_ROOT / "storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh",
        kubectl_dir / "_nv-elbencho-kubectl-functions.sh",
    )
    shutil.copy2(
        _REPO_ROOT / "storage-tests/fs/kubectl/_nv-elbencho-kubectl-coordinator.sh",
        kubectl_dir / "_nv-elbencho-kubectl-coordinator.sh",
    )
    root = tmp_path / "data"
    root.mkdir()
    env = tmp_path / "env.sh"
    env.write_text(
        textwrap.dedent(f"""
            export SCALE_TEST_BASE={_REPO_ROOT!s}
            export RESULTS_DIR={tmp_path / 'results'!s}
            export LOGS_DIR={tmp_path / 'logs'!s}
            declare -A TEST_DIRS=([{root!s}]=1)
            ELBENCHO_SCALE_THREAD_LIST=(1)
            ELBENCHO_SCALE_IO_SIZES=(4K)
            ELBENCHO_IODEPTH_LIST=(1)
            export ELBENCHO_SCALE_READ_WRITE_DURATION=1
            export ELBENCHO_FILE_LAYOUT=worker-directories
            export ELBENCHO_FILES_PER_NODE=
            export ELBENCHO_FILE_SIZE=1M
            export ELBENCHO_FILE_SIZE_MULTIPLIER=1
            export ELBENCHO_LIVE_CSV_EXTENDED=0
            export ELBENCHO_SINGLE_BIG_FILE=0
            export ELBENCHO_ALL_NODES_ACCESS_ALL_DATA=0
            export FS_MAX_AGG_THROUGHPUT=1
            export FS_MAX_NODE_THROUGHPUT_GBPS=1
            export FS_MAX_NODE_IOPS=1
            export EXECUTION_SUBSTRATE=slurm
            export SSH_ENABLED=
            export SLURM_ENABLED=
            source "$SCALE_TEST_BASE/lib/env_functions.sh"
            """),
        encoding="utf-8",
    )
    return sweep


@pytest.mark.parametrize(
    "arguments, message",
    [
        (("--nodes",), "requires an argument"),
        (("--read-from",), "requires a path argument"),
        (("--delete-only",), "requires a path argument"),
        (("--resume",), "requires a path argument"),
        (("--status",), "requires a results directory"),
        (("--cancel",), "requires a results directory"),
        (("--collect",), "requires a results directory"),
        (("--unknown",), "Unknown option"),
        (("positional",), "Unexpected positional argument"),
        ((), "--nodes is required"),
        (("--nodes", "3-1"), "Invalid node specification"),
        (("--nodes", "1,invalid"), "Invalid node specification"),
        (
            ("--write-only", "--write-no-read", "--nodes", "1"),
            "Use at most one",
        ),
        (("--resume", "/missing", "--bio"), "mutually exclusive"),
        (("--status", "/missing", "--nodes", "1"), "mutually exclusive"),
        (("--status", "/missing", "--cancel", "/missing"), "mutually exclusive"),
        (("--status", "/one", "--status", "/two"), "only once"),
    ],
)
def test_sweep_cli_rejects_invalid_equivalence_classes(tmp_path, arguments, message):
    """Parser and mode errors fail before dispatching or creating a run."""
    sweep = _make_sweep_fixture(tmp_path)
    result = subprocess.run(
        [str(sweep), *arguments],
        check=False,
        cwd=tmp_path,
        env={**os.environ, "SHELL": _BASH},
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert message in result.stdout + result.stderr
    assert not (tmp_path / "results").exists()


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_sweep_help_is_environment_independent(tmp_path, flag):
    """Both help aliases exit before workload validation or dispatch."""
    sweep = _make_sweep_fixture(tmp_path)
    result = subprocess.run(
        [str(sweep), flag],
        check=False,
        cwd=tmp_path,
        env={**os.environ, "SHELL": _BASH},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout
    assert "--resume" in result.stdout


@pytest.mark.parametrize("operation", ("status", "cancel", "collect"))
def test_kubectl_lifecycle_parses_without_current_environment(tmp_path, operation):
    """Saved-attempt operations do not consult a changed or missing env.sh."""
    sweep = _make_sweep_fixture(tmp_path)
    (tmp_path / "env.sh").unlink()
    result = subprocess.run(
        [str(sweep), f"--{operation}", str(tmp_path / "prior-results")],
        check=False,
        cwd=tmp_path,
        env={**os.environ, "SHELL": _BASH},
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "Failed to source env.sh" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("saved_line", "inherited", "message"),
    (
        ("", "", "legacy resume snapshots require inherited"),
        (
            "export EXECUTION_SUBSTRATE=ssh\n",
            "slurm",
            "does not match inherited",
        ),
        (
            "export EXECUTION_SUBSTRATE=invalid\n",
            "",
            "saved EXECUTION_SUBSTRATE is unsupported",
        ),
    ),
)
def test_resume_rejects_saved_identity_before_sourcing_current_env(
    tmp_path, saved_line, inherited, message
):
    """Current configuration cannot reinterpret an invalid saved attempt."""
    sweep = _make_sweep_fixture(tmp_path)
    marker = tmp_path / "env-sourced"
    with (tmp_path / "env.sh").open("a", encoding="utf-8") as stream:
        stream.write(f"printf sourced > {marker!s}\n")
    result_dir = tmp_path / "results" / "elbencho-saved"
    (result_dir / "executions").mkdir(parents=True)
    (result_dir / "env_used.sh").write_text(saved_line, encoding="utf-8")
    environment = {**os.environ, "SHELL": _BASH}
    if inherited:
        environment["EXECUTION_SUBSTRATE"] = inherited
    else:
        environment.pop("EXECUTION_SUBSTRATE", None)
    result = subprocess.run(
        [str(sweep), "--resume", str(result_dir)],
        check=False,
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert message in result.stderr
    assert not marker.exists()


def test_kubectl_resume_gates_from_saved_snapshot_without_current_env(tmp_path):
    """The saved selector is authoritative before current configuration."""
    sweep = _make_sweep_fixture(tmp_path)
    (tmp_path / "env.sh").unlink()
    result_dir = tmp_path / "results" / "elbencho-saved"
    (result_dir / "executions").mkdir(parents=True)
    (result_dir / "env_used.sh").write_text(
        "export EXECUTION_SUBSTRATE=kubectl\n", encoding="utf-8"
    )
    result = subprocess.run(
        [str(sweep), "--resume", str(result_dir)],
        check=False,
        cwd=tmp_path,
        env={**os.environ, "SHELL": _BASH},
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "Failed to source env.sh" not in result.stdout + result.stderr
