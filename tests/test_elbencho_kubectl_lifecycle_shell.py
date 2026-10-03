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

"""Fault-oriented contracts for Kubernetes sweep lifecycle primitives."""

from pathlib import Path
import hashlib
import os
import pty
import shutil
import subprocess
import tarfile
import textwrap

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_FUNCTIONS = _ROOT / "storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
_BASH = shutil.which("bash") or "/bin/bash"


def _bash(body: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            _BASH,
            "-c",
            textwrap.dedent(f"""\
                source {_FUNCTIONS!s}
                KUBECTL_CONTROL_LOGICAL_ROOT=benchmark
                KUBECTL_CONTROL_TEST_ROOT="$KUBECTL_SWEEP_MOUNT_ROOT/benchmark"
                KUBECTL_CONTROL_ROOT="$KUBECTL_CONTROL_TEST_ROOT/.storage-scale-test"
                {body}
                """),
        ],
        cwd=_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _identity(root: Path) -> str:
    return f"""
        root={str(root)!r}
        kubectl_local_lock_acquire "$root" fd
        kubectl_attempt_create_identity "$root" "$fd" 1234abcd \\
          0123456789abcdef0123456789abcdef test-ns namespace-uid \\
          test-pv pv-uid test-pvc pvc-uid
    """


def test_repeated_helper_loading_preserves_constants_and_active_locks(tmp_path):
    """Preflight followed by dispatch must neither warn nor reset ownership."""
    result = _bash(f"""
        set -euo pipefail
        root={str(tmp_path / 'kubernetes')!r}
        kubectl_local_lock_acquire "$root" fd
        saved_root=${{KUBECTL_LOCAL_LOCK_ROOTS[$fd]}}
        KUBECTL_BATCH_VALIDATION_PATHS=(one two)
        source {_FUNCTIONS!s}
        [[ "$KUBECTL_SWEEP_MOUNT_ROOT" == /mnt/storage-scale-test ]]
        [[ "${{KUBECTL_LOCAL_LOCK_ROOTS[$fd]}}" == "$saved_root" ]]
        [[ "${{KUBECTL_BATCH_VALIDATION_PATHS[*]}}" == 'one two' ]]
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


@pytest.mark.parametrize("remote_state", ["PREPARED", "RUNNING", "FAILED", "SUCCESS"])
def test_remote_execution_progress_counts_only_current_attempt(tmp_path, remote_state):
    """Read live cells, not uncollected local states or a predecessor's cells."""
    run = tmp_path / "run"
    definitions = run / "control/executions"
    ledger = run / "state/executions"
    definitions.mkdir(parents=True)
    ledger.mkdir(parents=True)
    for execution, status in zip(
        ["0002", "0004", "0006", "0008"],
        ["PENDING", "RUNNING", "SUCCESS", "FAILED"],
    ):
        (definitions / f"{execution}.sh").write_text("definition\n")
        (ledger / f"{execution}.status").write_text(status + "\n")
    (ledger / "0001.status").write_text("SUCCESS\n")
    result = _bash(f"""
        kubectl_attempt_remote_root() {{ printf '%s' {str(run)!r}; }}
        kubectl_remote_tree_guard_script() {{ printf 'run=$1; attempt=$2; run_real=$1\n'; }}
        kubectl_pvc_exec() {{ shift 2; "$@"; }}
        kubectl_read_remote_execution_progress test-ns helper 1234abcd {remote_state}
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1 1 1 1\n"


@pytest.mark.parametrize(
    "fault",
    ["startup", "missing", "invalid", "symlink-file", "symlink-directory", "no-cells"],
)
def test_remote_execution_progress_handles_startup_and_rejects_invalid_state(
    tmp_path, fault
):
    """Only PREPARED can have an as-yet uninitialized execution ledger."""
    run = tmp_path / "run"
    definitions = run / "control/executions"
    ledger = run / "state/executions"
    definitions.mkdir(parents=True)
    ledger.mkdir(parents=True)
    (definitions / "0001.sh").write_text("definition\n")
    status = ledger / "0001.status"
    if fault == "invalid":
        status.write_text("nonsense\n")
    elif fault == "symlink-file":
        target = tmp_path / "external.status"
        target.write_text("SUCCESS\n")
        status.symlink_to(target)
    elif fault == "symlink-directory":
        ledger.rmdir()
        target = tmp_path / "external"
        target.mkdir()
        (target / "0001.status").write_text("SUCCESS\n")
        ledger.symlink_to(target)
    elif fault == "no-cells":
        (definitions / "0001.sh").unlink()
    elif fault == "startup":
        ledger.rmdir()
    remote_state = "PREPARED" if fault == "startup" else "RUNNING"
    result = _bash(f"""
        kubectl_attempt_remote_root() {{ printf '%s' {str(run)!r}; }}
        kubectl_remote_tree_guard_script() {{ printf 'run=$1; attempt=$2; run_real=$1\n'; }}
        kubectl_pvc_exec() {{ shift 2; "$@"; }}
        kubectl_read_remote_execution_progress test-ns helper 1234abcd {remote_state}
        """)
    assert (result.returncode == 0) is (fault == "startup"), result.stderr
    if fault == "startup":
        assert result.stdout == "1 0 0 0\n"
    else:
        assert not result.stdout


@pytest.mark.parametrize(
    "missing_evidence", ["none", "lease", "remote", "resource", "intent", "predecessor"]
)
def test_failed_batch_submission_retry_requires_complete_rollback(
    tmp_path: Path, missing_evidence: str
) -> None:
    """Only a clean initial failure may create a replacement batch attempt."""
    root = tmp_path / "kubernetes"
    setup = ""
    if missing_evidence != "lease":
        setup += (
            'kubectl_attempt_journal_step "$root" "$fd" 1234abcd release-pvc-lease\n'
        )
    if missing_evidence == "remote":
        setup += 'printf "reservation\\n" > "$root/attempts/1234abcd/remote-reservation.sh"\n'
    if missing_evidence == "resource":
        setup += (
            'printf "resource\\n" > "$root/attempts/1234abcd/resources/workers.sh"\n'
        )
    if missing_evidence == "intent":
        setup += 'printf "intent\\n" > "$root/attempts/1234abcd/creation-intents/worker.sh"\n'
    if missing_evidence == "predecessor":
        setup += (
            'printf "aaaabbbb\\n" > "$root/attempts/1234abcd/predecessor-attempt"\n'
        )
    result = _bash(_identity(root) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_state "$root" "$fd" 1234abcd SUBMISSION_FAILED
        mkdir -p "$root/attempts/1234abcd/resources" "$root/attempts/1234abcd/creation-intents"
        {setup}
        _kubectl_batch_failed_submission_retryable "$root" 1234abcd
        """)
    assert (result.returncode == 0) is (missing_evidence == "none"), result.stderr


def test_runtime_validation_does_not_require_a_results_directory() -> None:
    """Ordinary environment validation works before any sweep is prepared."""
    result = _bash("""
        set -u
        unset results_dir
        declare -gA TEST_DIRS=([benchmark]=1)
        KUBECTL_NODE_SELECTOR=benchmark=true
        kubectl_validate_cluster_identity() { return 0; }
        kubectl_discover_candidate_nodes() { printf 'reached-discovery\\n'; return 1; }
        if kubectl_validate_runtime_pod; then exit 1; fi
        """)
    assert result.returncode == 0, result.stderr
    assert "reached-discovery" in result.stdout
    assert "unbound variable" not in result.stderr


def test_ordinary_pvc_exec_does_not_attach_interactive_terminal(
    tmp_path: Path,
) -> None:
    """A human terminal cannot make a non-streaming PVC command wait on stdin."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "kubectl"
    fake.write_text(
        "#!/bin/bash\n"
        'for arg in "$@"; do\n'
        '  if [[ "$arg" == -i ]]; then read -r _; fi\n'
        "done\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    master, slave = pty.openpty()
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    environment["KUBECTL_PROCESS_TIMEOUT_SECONDS"] = "1"
    process = subprocess.Popen(
        [
            _BASH,
            "-c",
            f"source {_FUNCTIONS!s}; kubectl_pvc_exec test-ns helper true",
        ],
        cwd=_ROOT,
        stdin=slave,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    os.close(slave)
    try:
        stdout, stderr = process.communicate(timeout=3)
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()
    assert process.returncode == 0, f"stdout={stdout!r} stderr={stderr!r}"


def test_streaming_pvc_exec_is_the_only_adapter_that_attaches_stdin(
    tmp_path: Path,
) -> None:
    """Finite control-bundle input reaches the explicitly streaming adapter."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "kubectl"
    fake.write_text(
        "#!/bin/bash\n"
        "saw_i=0\n"
        'for arg in "$@"; do [[ "$arg" != -i ]] || saw_i=1; done\n'
        '[[ "$saw_i" == 1 ]] || exit 64\n'
        "cat\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    environment = os.environ.copy()
    environment["PATH"] = f"{fake_bin}:{environment['PATH']}"
    result = subprocess.run(
        [
            _BASH,
            "-c",
            f"source {_FUNCTIONS!s}; kubectl_pvc_exec_stdin test-ns helper cat",
        ],
        cwd=_ROOT,
        input="control-bundle\n",
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "control-bundle\n"


def test_identity_is_complete_atomic_and_path_bound(tmp_path: Path) -> None:
    """[S-02] [R-19] Partial identity cannot become an owned attempt."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_PVC_LEASE_NAME" =~ ^sst-elb-pvc-[0-9a-f]{32}$ ]]
        mv "$root/attempts/1234abcd" "$root/attempts/abcdef12"
        ! kubectl_attempt_load_metadata "$root/attempts/abcdef12"
        mkdir "$root/attempts/deadbeef"
        printf 'KUBECTL_ATTEMPT_SCHEMA=1\n' > "$root/attempts/deadbeef/identity.sh"
        ! kubectl_attempt_load_identity "$root/attempts/deadbeef"
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr


def test_attempt_configuration_freezes_the_canonical_control_root(
    tmp_path: Path,
) -> None:
    """Lifecycle retries use saved placement rather than a changed env.sh."""
    result = _bash(_identity(tmp_path / "state") + """
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=docker.io/breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never
        export KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        declare -A mapped=([/mnt/storage-scale-test/zeta]=1 \
          [/mnt/storage-scale-test/alpha]=1)
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_configuration "$root" "$fd" 1234abcd mapped '' \
          alpha /mnt/storage-scale-test/alpha
        source "$root/attempts/1234abcd/configuration.sh"
        kubectl_validate_saved_control_layout
        [[ "$KUBECTL_CONTROL_ROOT" == \
          /mnt/storage-scale-test/alpha/.storage-scale-test ]]
        KUBECTL_CONTROL_ROOT=/mnt/storage-scale-test/zeta/.storage-scale-test
        ! kubectl_validate_saved_control_layout
        kubectl_local_lock_release "$fd"
    """)
    assert result.returncode == 0, result.stderr


def test_local_lifecycle_state_graph_and_symlink_state_are_rejected(
    tmp_path: Path,
) -> None:
    """State cannot skip transitions or make a lock traverse a symlink."""
    state = tmp_path / "state"
    result = _bash(_identity(state) + """
        ! kubectl_attempt_write_state "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        ! kubectl_attempt_transition "$root" "$fd" 1234abcd COLLECTED
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-state"
    link.symlink_to(target, target_is_directory=True)
    result = _bash(f"! kubectl_local_lock_acquire {str(link)!r} fd")
    assert result.returncode == 0, result.stderr


def test_local_path_validator_checks_suffix_after_missing_component(
    tmp_path: Path,
) -> None:
    """A missing prefix cannot hide a later parent-directory component."""
    requested = tmp_path / "future" / ".." / "outside"
    result = _bash(f"! _kubectl_validate_local_directory_path {str(requested)!r}")
    assert result.returncode == 0, result.stderr


def test_attempt_templates_are_yaml_and_restrict_security_surface() -> None:
    """The checked-in resources have no API token, host networking, or Service."""
    result = _bash("""
        selector=$(kubectl_render_node_selector 'storage-test=true,role=client')
        nodes=$(kubectl_render_node_affinity_values node-a node-b)
        kubectl_render_attempt_template storage-tests/fs/kubectl/templates/worker-daemonset.yaml.tmpl \\
          NAMESPACE=test-ns RESOURCE_NAME=sst-elb-1234abcd-workers ATTEMPT_ID=1234abcd \\
          OWNERSHIP_NONCE=0123456789abcdef0123456789abcdef IMAGE=breuner/elbencho:v3.2-1 \\
          IMAGE_PULL_POLICY=Never RUN_AS_USER=2000 RUN_AS_GROUP=2000 PVC_NAME=test-pvc \\
          "NODE_SELECTOR_BLOCK=$selector" "NODE_AFFINITY_VALUES=$nodes"
        """)
    assert result.returncode == 0, result.stderr
    daemonset = yaml.safe_load(result.stdout)
    pod_spec = daemonset["spec"]["template"]["spec"]
    assert pod_spec["automountServiceAccountToken"] is False
    assert "hostNetwork" not in pod_spec
    assert pod_spec["nodeSelector"] == {"storage-test": "true", "role": "client"}
    assert pod_spec["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]
    expression = pod_spec["affinity"]["nodeAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]["nodeSelectorTerms"][0]["matchExpressions"][0]
    assert expression == {
        "key": "kubernetes.io/hostname",
        "operator": "In",
        "values": ["node-a", "node-b"],
    }
    assert (
        pod_spec["containers"][0]["securityContext"]["allowPrivilegeEscalation"]
        is False
    )
    assert pod_spec["securityContext"]["seccompProfile"] == {"type": "Unconfined"}
    for template in (_ROOT / "storage-tests/fs/kubectl/templates").glob("*.yaml.tmpl"):
        assert "@@" in template.read_text(encoding="utf-8") or template.name.endswith(
            "network-policy.yaml.tmpl"
        )


def test_sweep_job_exposes_coordinator_identity_with_downward_api() -> None:
    """The phase-six coordinator gets its exact Pod identity without API access."""
    result = _bash("""
        kubectl_render_attempt_template storage-tests/fs/kubectl/templates/sweep-job.yaml.tmpl \\
          NAMESPACE=test-ns RESOURCE_NAME=sst-elb-1234abcd-sweep ATTEMPT_ID=1234abcd \\
          OWNERSHIP_NONCE=0123456789abcdef0123456789abcdef IMAGE=breuner/elbencho:v3.2-1 \\
          IMAGE_PULL_POLICY=Never RUN_AS_USER=2000 RUN_AS_GROUP=2000 PVC_NAME=test-pvc \\
          COORDINATOR_NODE=node-a \
          REMOTE_RUN_DIRECTORY=/mnt/storage-scale-test/benchmark/.storage-scale-test/runs/1234abcd
        """)
    assert result.returncode == 0, result.stderr
    job = yaml.safe_load(result.stdout)
    assert job["spec"]["template"]["spec"]["securityContext"]["seccompProfile"] == {
        "type": "Unconfined"
    }
    fields = {
        item["name"]: item["valueFrom"]["fieldRef"]["fieldPath"]
        for item in job["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert fields == {
        "KUBECTL_COORDINATOR_POD_NODE": "spec.nodeName",
        "KUBECTL_COORDINATOR_POD_NAME": "metadata.name",
        "KUBECTL_COORDINATOR_POD_UID": "metadata.uid",
        "KUBECTL_COORDINATOR_POD_IP": "status.podIP",
    }


@pytest.mark.parametrize("address", ("10.1.2.3", "255.255.255.255", "0.0.0.0"))
def test_pod_ipv4_validation_accepts_real_octets(address: str) -> None:
    result = _bash(f"kubectl_validate_ipv4 {address!r}")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("address", ("10.1.2.999", "1.2.3", "1.2.3.-1", "01.2.3.4"))
def test_pod_ipv4_validation_rejects_invalid_addresses(address: str) -> None:
    result = _bash(f"kubectl_validate_ipv4 {address!r}")
    assert result.returncode != 0


def test_hostile_collection_archives_never_extract(tmp_path: Path) -> None:
    """[R-07] Traversal and link members fail before local publication."""
    archive = tmp_path / "hostile.tar"
    with tarfile.open(archive, "w") as stream:
        info = tarfile.TarInfo("1234abcd/../../outside")
        info.size = 1
        stream.addfile(info, fileobj=__import__("io").BytesIO(b"x"))
    output = tmp_path / ".kubernetes-collect-1234abcd.test"
    result = _bash(
        f"! kubectl_extract_attempt_archive {str(archive)!r} 1234abcd {str(tmp_path)!r} {str(output)!r}"
    )
    assert result.returncode == 0, result.stderr
    assert not output.exists()


def test_collection_accepts_only_regular_files_and_directories(tmp_path: Path) -> None:
    """A valid attempt archive is staged below a caller-owned result directory."""
    source = tmp_path / "1234abcd"
    (source / "state").mkdir(parents=True)
    (source / "state" / "run.status").write_text("SUCCESS\n", encoding="utf-8")
    archive = tmp_path / "valid.tar"
    with tarfile.open(archive, "w") as stream:
        stream.add(source, arcname="1234abcd")
    output = tmp_path / ".kubernetes-collect-1234abcd.test"
    result = _bash(
        f"kubectl_extract_attempt_archive {str(archive)!r} 1234abcd {str(tmp_path)!r} {str(output)!r}; "
        f'test "$(cat {str(output / "1234abcd/state/run.status")!r})" = SUCCESS'
    )
    assert result.returncode == 0, result.stderr


def test_collection_archive_metadata_is_streamed_and_member_bounded(
    tmp_path: Path,
) -> None:
    """[R-06] Archive validation streams metadata and enforces member limits."""
    source = tmp_path / "1234abcd"
    source.mkdir()
    (source / "one").write_text("one\n", encoding="utf-8")
    archive = tmp_path / "valid.tar"
    with tarfile.open(archive, "w") as stream:
        stream.add(source, arcname="1234abcd")
    unusable_tmp = tmp_path / "not-a-directory"
    unusable_tmp.write_text("occupied\n", encoding="utf-8")
    result = _bash(f"""
        TMPDIR={str(unusable_tmp)!r} kubectl_validate_attempt_archive \
            {str(archive)!r} 1234abcd
        ! _kubectl_validate_archive_names_stream 1234abcd 2 \
            <<< $'1234abcd\n1234abcd/one\n1234abcd/two'
        _kubectl_validate_archive_types_stream 2 \
            <<< $'-rw-r--r-- file\ndrwxr-xr-x directory'
        ! _kubectl_validate_archive_types_stream 2 \
            <<< $'-rw-r--r-- one\n-rw-r--r-- two\n-rw-r--r-- three'
        """)
    assert result.returncode == 0, result.stderr


def test_collection_staging_cannot_escape_results_root(tmp_path: Path) -> None:
    """A valid archive still cannot cause local extraction outside results."""
    source = tmp_path / "1234abcd"
    source.mkdir()
    archive = tmp_path / "valid.tar"
    with tarfile.open(archive, "w") as stream:
        stream.add(source, arcname="1234abcd")
    results = tmp_path / "results"
    results.mkdir()
    outside = tmp_path / ".kubernetes-collect-1234abcd.test"
    result = _bash(
        f"! kubectl_extract_attempt_archive {str(archive)!r} 1234abcd "
        f"{str(results)!r} {str(outside)!r}"
    )
    assert result.returncode == 0, result.stderr
    assert not outside.exists()


def test_collected_manifest_paths_cannot_escape_staged_state(tmp_path: Path) -> None:
    """Manifest source paths cannot read adjacent staged or host files."""
    state = tmp_path / "attempt" / "state"
    state.mkdir(parents=True)
    (state / "run.status").write_text("SUCCESS\n", encoding="utf-8")
    outside = state.parent / "outside"
    outside.write_text("not collected\n", encoding="utf-8")
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    (definitions / "0001.sh").write_text("export nodes=1\n", encoding="utf-8")
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    (state / "publication-manifest.tsv").write_text(
        "schema\t1\n"
        "attempt_id\t1234abcd\n"
        f"ledger\t../outside\toutside\t{outside.stat().st_size}\t{digest}\n",
        encoding="utf-8",
    )
    result = _bash(
        f"! _kubectl_validate_collected_publication {str(state)!r} "
        f"1234abcd terminal {str(definitions)!r}"
    )
    assert result.returncode == 0, result.stderr


def test_collected_manifest_rejects_duplicate_semantic_rows(tmp_path: Path) -> None:
    """[R-08] A manifest cannot redefine identity, cells, or destinations."""
    state = tmp_path / "state"
    state.mkdir()
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    (definitions / "0001.sh").write_text("export nodes=1\n", encoding="utf-8")
    (state / "run.status").write_text("SUCCESS\n", encoding="utf-8")
    manifest = state / "publication-manifest.tsv"
    for duplicate in (
        "schema\t1",
        "attempt_id\t1234abcd",
        "execution\t0001\tSUCCESS",
    ):
        manifest.write_text(
            "schema\t1\n"
            "attempt_id\t1234abcd\n"
            "execution\t0001\tSUCCESS\n"
            f"{duplicate}\n",
            encoding="utf-8",
        )
        result = _bash(
            f"! _kubectl_validate_collected_publication {str(state)!r} "
            f"1234abcd terminal {str(definitions)!r}"
        )
        assert result.returncode == 0, result.stderr


def test_collected_manifest_requires_complete_terminal_evidence(tmp_path: Path) -> None:
    """Collection cannot delete remote state after accepting an omission."""
    state = tmp_path / "state"
    (state / "executions").mkdir(parents=True)
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    (definitions / "0001.sh").write_text("export nodes=1\n", encoding="utf-8")
    (state / "run.status").write_text("SUCCESS\n", encoding="utf-8")
    (state / "publication-manifest.tsv").write_text(
        "schema\t1\nattempt_id\t1234abcd\n", encoding="utf-8"
    )
    result = _bash(
        f"! _kubectl_validate_collected_publication {str(state)!r} "
        f"1234abcd terminal {str(definitions)!r}"
    )
    assert result.returncode == 0, result.stderr


def test_fresh_collection_uses_immutable_local_execution_definitions() -> None:
    """PVC content cannot redefine the execution set used by validation."""
    source = _FUNCTIONS.read_text(encoding="utf-8")
    body = source.split("kubectl_collect_attempt() {", 1)[1].split(
        "\n}\n\nkubectl_resume_collected_sweep", 1
    )[0]
    assert '"$metadata_dir/control-bundle/executions"' in body
    assert '"$staging/$attempt_id/control/executions"' not in body


def test_resume_interprets_collected_state_with_current_coordinator() -> None:
    """Bug fixes apply when resuming an attempt created by older code."""
    source = _FUNCTIONS.read_text(encoding="utf-8")
    body = source.split("kubectl_resume_collected_sweep() {", 1)[1].split("\n}\n", 1)[0]
    assert '"$BASH" "$coordinator_source"' in body
    assert "control-bundle/coordinator.sh" not in body


def _complete_publication_fixture(tmp_path: Path):
    """Build complete cell and manifest evidence for collection tests."""
    state = tmp_path / "state"
    executions = state / "executions"
    result_dir = state / "results/0001/executions"
    executions.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    (definitions / "0001.sh").write_text(
        "export nodes=1\n"
        "export ELBENCHO_FILE_LAYOUT=shared-directory\n"
        "export ELBENCHO_FILES_PER_NODE=1\n",
        encoding="utf-8",
    )
    files = {
        "run.status": "SUCCESS\n",
        "run-summary.tsv": "run_status\tSUCCESS\n",
        "env_used.sh": "export TEST=1\n",
        "env_used.yaml": "TEST: 1\n",
        "executions/0001.status": "SUCCESS\n",
        "executions/0001.exitcode": "0\n",
        "executions/0001.workers.tsv": "node-a\tpod-a\tuid-a\t10.0.0.1\n",
        "results/0001/executions/0001.workload.tsv": "schema\t1\n",
    }
    for relative, contents in files.items():
        path = state / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    roles = {
        "run.status": "ledger",
        "run-summary.tsv": "ledger",
        "env_used.sh": "snapshot",
        "env_used.yaml": "snapshot",
        "executions/0001.status": "ledger",
        "executions/0001.exitcode": "ledger",
        "executions/0001.workers.tsv": "ledger",
        "results/0001/executions/0001.workload.tsv": "result",
    }
    destinations = {
        "results/0001/executions/0001.workload.tsv": "executions/0001.workload.tsv"
    }
    rows = ["schema\t1", "attempt_id\t1234abcd", "execution\t0001\tSUCCESS"]
    for relative, role in roles.items():
        path = state / relative
        rows.append(
            f"{role}\t{relative}\t{destinations.get(relative, relative)}\t"
            f"{path.stat().st_size}\t{hashlib.sha256(path.read_bytes()).hexdigest()}"
        )
    (state / "publication-manifest.tsv").write_text(
        "\n".join(rows) + "\n", encoding="utf-8"
    )
    return state, definitions


def test_collected_manifest_accepts_complete_success_evidence(tmp_path: Path) -> None:
    """A complete cell ledger and required workload output are collectible."""
    state, definitions = _complete_publication_fixture(tmp_path)
    result = _bash(
        f"_kubectl_validate_collected_publication {str(state)!r} "
        f"1234abcd terminal {str(definitions)!r} >/dev/null; [[ $terminal == SUCCESS ]]"
    )
    assert result.returncode == 0, result.stderr


def test_collected_manifest_rejects_cross_execution_workload_substitution(
    tmp_path: Path,
) -> None:
    """One cell's artifact cannot satisfy another cell's completion contract."""
    state = tmp_path / "state"
    executions = state / "executions"
    result_dir = state / "results/0002/executions"
    executions.mkdir(parents=True)
    result_dir.mkdir(parents=True)
    definitions = tmp_path / "definitions"
    definitions.mkdir()
    (definitions / "0001.sh").write_text(
        "export ELBENCHO_FILE_LAYOUT=shared-directory\n"
        "export ELBENCHO_FILES_PER_NODE=1\n",
        encoding="utf-8",
    )
    files = {
        "run.status": "SUCCESS\n",
        "run-summary.tsv": "run_status\tSUCCESS\n",
        "env_used.sh": "export TEST=1\n",
        "env_used.yaml": "TEST: 1\n",
        "executions/0001.status": "SUCCESS\n",
        "executions/0001.exitcode": "0\n",
        "executions/0001.workers.tsv": "node-a\tpod-a\tuid-a\t10.0.0.1\n",
        "results/0002/executions/0002.workload.tsv": "schema\t1\n",
    }
    rows = ["schema\t1", "attempt_id\t1234abcd", "execution\t0001\tSUCCESS"]
    for relative, contents in files.items():
        path = state / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        role = "snapshot" if relative.startswith("env_used.") else "ledger"
        destination = relative
        if relative.startswith("results/"):
            role = "result"
            destination = "executions/0001.workload.tsv"
        rows.append(
            f"{role}\t{relative}\t{destination}\t{path.stat().st_size}\t"
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}"
        )
    (state / "publication-manifest.tsv").write_text(
        "\n".join(rows) + "\n", encoding="utf-8"
    )
    result = _bash(
        f"! _kubectl_validate_collected_publication {str(state)!r} "
        f"1234abcd terminal {str(definitions)!r}"
    )
    assert result.returncode == 0, result.stderr


def test_legacy_execution_does_not_require_workload_metadata(tmp_path: Path) -> None:
    """The collector follows the existing legacy completion contract."""
    definition = tmp_path / "0001.sh"
    definition.write_text(
        "export ELBENCHO_FILE_LAYOUT=worker-directories\n",
        encoding="utf-8",
    )
    result = _bash(
        f"! _kubectl_execution_requires_workload_metadata {str(definition)!r}"
    )
    assert result.returncode == 0, result.stderr


def test_collection_merges_manifest_declared_execution_ledgers(tmp_path: Path) -> None:
    """[R-11] Merge publishes only manifest-declared cell evidence."""
    state = tmp_path / "state"
    executions = state / "executions"
    executions.mkdir(parents=True)
    (executions / "0001.status").write_text("SUCCESS\n", encoding="utf-8")
    (executions / "0001.exitcode").write_text("0\n", encoding="utf-8")
    (executions / "0001.workers.tsv").write_text(
        "node-a\tpod-a\tuid-a\t10.0.0.1\n", encoding="utf-8"
    )
    results = tmp_path / "results"
    (results / "executions").mkdir(parents=True)
    stale = results / "executions" / ".0001.exitcode.kubectl-collect-1234abcd.tmp"
    stale.write_text("interrupted\n", encoding="utf-8")
    manifest = state / "publication-manifest.tsv"
    rows = []
    for name in ("0001.status", "0001.exitcode", "0001.workers.tsv"):
        source = executions / name
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        rows.append(
            f"ledger\texecutions/{name}\texecutions/{name}\t"
            f"{source.stat().st_size}\t{digest}"
        )
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    result = _bash(
        f"_kubectl_merge_collected_results {str(state)!r} {str(results)!r} 1234abcd reason; "
        f"[[ $reason == LEDGER_INCONSISTENT ]]"
    )
    assert result.returncode == 0, result.stderr
    assert (results / "executions/0001.status").read_text(encoding="utf-8") == (
        "SUCCESS\n"
    )
    assert (results / "executions/0001.exitcode").read_text(encoding="utf-8") == ("0\n")
    assert "node-a" in (results / "executions/0001.workers.tsv").read_text(
        encoding="utf-8"
    )
    assert not stale.exists()


def test_collection_never_follows_result_parent_symlinks(tmp_path: Path) -> None:
    """Manifest destinations cannot redirect publication outside results."""
    state = tmp_path / "state"
    source = state / "results/0001/output.txt"
    source.parent.mkdir(parents=True)
    source.write_text("collected\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    (state / "publication-manifest.tsv").write_text(
        f"result\tresults/0001/output.txt\tlinked/output.txt\t"
        f"{source.stat().st_size}\t{digest}\n",
        encoding="utf-8",
    )
    results = tmp_path / "results"
    outside = tmp_path / "outside"
    results.mkdir()
    outside.mkdir()
    (results / "linked").symlink_to(outside, target_is_directory=True)
    result = _bash(
        f"! _kubectl_merge_collected_results {str(state)!r} {str(results)!r} "
        "1234abcd reason; [[ $reason == LEDGER_INCONSISTENT ]]"
    )
    assert result.returncode == 0, result.stderr
    assert not (outside / "output.txt").exists()


def test_collection_classifies_parent_creation_failure_as_local_io(
    tmp_path: Path,
) -> None:
    """A failed local mkdir is not mislabeled as a corrupt remote ledger."""
    state = tmp_path / "state"
    source = state / "results/0001/output.txt"
    source.parent.mkdir(parents=True)
    source.write_text("collected\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    (state / "publication-manifest.tsv").write_text(
        f"result\tresults/0001/output.txt\tnew/output.txt\t"
        f"{source.stat().st_size}\t{digest}\n",
        encoding="utf-8",
    )
    results = tmp_path / "results"
    results.mkdir()
    result = _bash(f"""
        mkdir() {{ return 1; }}
        ! _kubectl_merge_collected_results {str(state)!r} {str(results)!r} \
            1234abcd reason
        [[ "$reason" == LOCAL_IO ]]
        """)
    assert result.returncode == 0, result.stderr


def test_resume_cleanup_never_follows_result_parent_symlinks(tmp_path: Path) -> None:
    """Superseded-artifact cleanup cannot unlink through a redirected parent."""
    results = tmp_path / "results"
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "output.txt"
    victim.write_text("prior\n", encoding="utf-8")
    results.mkdir()
    (results / "linked").symlink_to(outside, target_is_directory=True)
    prior = results / "kubernetes/attempts/11111111/collected-state"
    prior.mkdir(parents=True)
    digest = hashlib.sha256(victim.read_bytes()).hexdigest()
    (prior / "publication-manifest.tsv").write_text(
        f"result\tresults/0001/output.txt\tlinked/output.txt\t"
        f"{victim.stat().st_size}\t{digest}\n",
        encoding="utf-8",
    )
    state = tmp_path / "state"
    state.mkdir()
    (state / "publication-manifest.tsv").write_text(
        "execution\t0001\tSUCCESS\n", encoding="utf-8"
    )
    result = _bash(
        f"! _kubectl_remove_superseded_collection_paths {str(state)!r} "
        f"{str(results)!r}"
    )
    assert result.returncode == 0, result.stderr
    assert victim.read_text(encoding="utf-8") == "prior\n"


def test_resume_replaces_only_digest_verified_non_success_artifacts(
    tmp_path: Path,
) -> None:
    """Resume replaces collected failed-cell evidence without touching success."""
    results = tmp_path / "results"
    executions = results / "executions"
    executions.mkdir(parents=True)
    old_failed = results / "old-failed.out"
    old_failed.write_text("old failure\n", encoding="utf-8")
    retained_success = results / "success.out"
    retained_success.write_text("success\n", encoding="utf-8")
    (executions / "0001.status").write_text("SUCCESS\n", encoding="utf-8")
    (executions / "0002.status").write_text("FAILED\n", encoding="utf-8")
    (executions / "0002.exitcode").write_text("97\n", encoding="utf-8")
    (results / "run.status").write_text("FAILED\n", encoding="utf-8")
    previous = results / "kubernetes/attempts/11111111/collected-state"
    previous.mkdir(parents=True)

    def row(kind: str, remote: str, local_path: str, path: Path) -> str:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return f"{kind}\t{remote}\t{local_path}\t{path.stat().st_size}\t{digest}"

    (previous / "publication-manifest.tsv").write_text(
        "\n".join(
            (
                row(
                    "result",
                    "results/0001/success.out",
                    "success.out",
                    retained_success,
                ),
                row("result", "results/0002/old.out", "old-failed.out", old_failed),
                row(
                    "ledger",
                    "executions/0002.exitcode",
                    "executions/0002.exitcode",
                    executions / "0002.exitcode",
                ),
                row("ledger", "run.status", "run.status", results / "run.status"),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    state = tmp_path / "new-state"
    new_executions = state / "executions"
    new_results = state / "results/0002"
    new_executions.mkdir(parents=True)
    new_results.mkdir(parents=True)
    (new_executions / "0002.status").write_text("SUCCESS\n", encoding="utf-8")
    (new_executions / "0002.exitcode").write_text("0\n", encoding="utf-8")
    replacement = new_results / "new.out"
    replacement.write_text("new success\n", encoding="utf-8")
    (state / "run.status").write_text("SUCCESS\n", encoding="utf-8")
    manifest_rows = ["execution\t0002\tSUCCESS"]
    for kind, remote, local_path, path in (
        ("result", "results/0002/new.out", "new.out", replacement),
        (
            "ledger",
            "executions/0002.exitcode",
            "executions/0002.exitcode",
            new_executions / "0002.exitcode",
        ),
        ("ledger", "run.status", "run.status", state / "run.status"),
    ):
        manifest_rows.append(row(kind, remote, local_path, path))
    (state / "publication-manifest.tsv").write_text(
        "\n".join(manifest_rows) + "\n", encoding="utf-8"
    )
    result = _bash(
        f"_kubectl_merge_collected_results {str(state)!r} {str(results)!r} "
        "1234abcd reason"
    )
    assert result.returncode == 0, result.stderr
    assert retained_success.read_text(encoding="utf-8") == "success\n"
    assert not old_failed.exists()
    assert (results / "new.out").read_text(encoding="utf-8") == "new success\n"
    assert (executions / "0002.status").read_text(encoding="utf-8") == "SUCCESS\n"
    assert (executions / "0002.exitcode").read_text(encoding="utf-8") == "0\n"
    assert (results / "run.status").read_text(encoding="utf-8") == "SUCCESS\n"


def test_fake_kubectl_discovers_only_ready_nonterminating_workers(
    tmp_path: Path,
) -> None:
    """Endpoint discovery ignores a terminating Pod beside its replacement."""
    nodes = tmp_path / "nodes.tsv"
    nodes.write_text("node-a\tuid-a\tamd64\nnode-b\tuid-b\tamd64\n", encoding="utf-8")
    output = tmp_path / "workers.tsv"
    result = _bash(f"""
        kubectl_run_bounded() {{
          if [[ "$*" == *"get nodes"* ]]; then
            printf 'node-a\\tuid-a\\tamd64\\tTrue\\t\\nnode-b\\tuid-b\\tamd64\\tTrue\\t\\n'
          else
            printf 'node-a\\tpod-a\\tpoduid-a\\t10.0.0.1\\tTrue\\tsha256:one\\t\\n'
            printf 'node-b\\tpod-old\\tpoduid-old\\t10.0.0.2\\tTrue\\tsha256:one\\t2026-01-01T00:00:00Z\\n'
            printf 'node-b\\tpod-new\\tpoduid-new\\t10.0.0.3\\tTrue\\tsha256:one\\t\\n'
          fi
        }}
        kubectl_discover_worker_endpoints test-ns 1234abcd {str(nodes)!r} {str(output)!r}
        ! grep -F pod-old {str(output)!r}
        grep -F $'node-b\\tuid-b\\tpod-new\\tpoduid-new\\t10.0.0.3' {str(output)!r}
        """)
    assert result.returncode == 0, result.stderr


def test_terminal_job_evidence_preserves_empty_active_field() -> None:
    """[T-03] Terminal PVC evidence still waits for Job quiescence."""
    result = _bash("""
        kubectl_verify_object_identity() { :; }
        kubectl_run_bounded() { printf '|0|1||True'; }
        kubectl_job_is_terminal_failure Job sweep test-ns \
          0123456789abcdef0123456789abcdef 1234abcd uid-1
        """)
    assert result.returncode == 0, result.stderr


def test_collection_stream_uses_its_operation_sized_deadline(tmp_path: Path) -> None:
    """[R-04] A large archive never inherits the status-command deadline."""
    archive = tmp_path / "attempt.tar"
    result = _bash(f"""
        export KUBECTL_COLLECTION_TIMEOUT_SECONDS=123
        kubectl_pvc_exec() {{
          printf '%s\t%s\n' "$KUBECTL_REQUEST_TIMEOUT_SECONDS" \
            "$KUBECTL_PROCESS_TIMEOUT_SECONDS"
        }}
        kubectl_stream_remote_attempt test-ns collector 1234abcd {str(archive)!r}
        read -r request process < {str(archive)!r}
        [[ "$request" -gt 0 && "$request" -le 123 && "$process" == "$request" ]]
        """)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "error",
    [
        "error: unexpected EOF",
        "read tcp: connection reset by peer",
        "error: HTTP 429 Too Many Requests",
        "error: HTTP 503 Service Unavailable",
        "error: stream error: INTERNAL_ERROR",
        "tar: 1234abcd/state/results/0001/report.out: file changed as we read it\n"
        "command terminated with exit code 1",
        "tar: 1234abcd/state: file changed as we read it",
    ],
)
def test_collection_retries_transient_transfer_and_publishes_complete_results(
    tmp_path: Path, error: str
) -> None:
    """[R-03] A retry restarts receipt and still verifies the full publication."""
    state, definitions = _complete_publication_fixture(tmp_path)
    payload = tmp_path / "remote.tar"
    with tarfile.open(payload, "w") as archive:
        archive.add(state, arcname="1234abcd/state")
    received = tmp_path / "received.tar"
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (tmp_path / "published").mkdir()
    calls = tmp_path / "calls"
    staging = tmp_path / ".kubernetes-collect-1234abcd.A1"
    result = _bash(f"""
        set -euo pipefail
        KUBECTL_COLLECTION_RETRY_BACKOFF_SECONDS=0
        kubectl_pvc_exec() {{
            printf 'call\\n' >> {str(calls)!r}
            if [[ $(wc -l < {str(calls)!r}) -eq 1 ]]; then
                printf partial
                printf '%s\\n' "$injected_error" >&2
                return 1
            fi
            test ! -e {str(received)!r} || [[ ! -s {str(received)!r} ]]
            cat {str(payload)!r}
        }}
        injected_error=$(printf '%b' {error!r})
        kubectl_stream_remote_attempt test-ns collector 1234abcd \
            {str(received)!r} {str(metadata / 'state.sh')!r}
        kubectl_extract_attempt_archive {str(received)!r} 1234abcd \
            {str(tmp_path)!r} {str(staging)!r}
        _kubectl_validate_collected_publication {str(staging / '1234abcd/state')!r} \
            1234abcd terminal {str(definitions)!r} >/dev/null
        [[ "$terminal" == SUCCESS ]]
        _kubectl_merge_collected_results {str(staging / '1234abcd/state')!r} \
            {str(tmp_path / 'published')!r} 1234abcd reason
        """)
    assert result.returncode == 0, result.stderr
    assert len(calls.read_text().splitlines()) == 2
    assert received.read_bytes() == payload.read_bytes()
    assert (tmp_path / "published/executions/0001.status").read_text() == "SUCCESS\n"
    bundles = list((metadata / "diagnostics").glob("collection-stream.*"))
    assert len(bundles) == 1
    assert (bundles[0] / "transfer-1.stderr").read_text().strip() == error
    assert "attempt_id\t1234abcd" in (bundles[0] / "bundle.tsv").read_text()
    assert "\t1\t0\t" in (bundles[0] / "transfers.tsv").read_text()
    assert "\t0\t0\tSUCCESS" in (bundles[0] / "transfers.tsv").read_text()
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=" not in result.stderr


@pytest.mark.parametrize(
    "error,rc,reason",
    [
        ("Unauthorized: token expired; timeout", 1, "AUTH"),
        ("Error: collector not found", 1, "IDENTITY_MISMATCH"),
        ("Error: path escapes PVC mount", 1, "PATH_REJECTED"),
        ("tar: unexpected EOF in archive", 1, "TRANSFER_FAILED"),
        (
            "tar: 1234abcd/state/run.status: file changed as we read it\n"
            "tar: read error: Permission denied",
            1,
            "TRANSFER_FAILED",
        ),
        (
            "tar: other/state/run.status: file changed as we read it",
            1,
            "TRANSFER_FAILED",
        ),
        (
            "tar: 1234abcd/control/executions/0002.sh: file changed as we read it",
            1,
            "TRANSFER_FAILED",
        ),
        (
            "tar: 1234abcd/state/../control: file changed as we read it",
            1,
            "TRANSFER_FAILED",
        ),
        (
            "tar: 1234abcd/state/run.status: file changed as we read it",
            2,
            "TRANSFER_FAILED",
        ),
        ("command terminated with exit code 2: timeout", 1, "TRANSFER_FAILED"),
        ("unknown remote failure", 1, "TRANSFER_FAILED"),
        ("unknown remote timeout", 1, "TRANSFER_FAILED"),
        ("", 137, "TRANSFER_FAILED"),
    ],
)
def test_collection_never_retries_permanent_or_unknown_remote_errors(
    tmp_path: Path, error: str, rc: int, reason: str
) -> None:
    """[R-03] Transport retries never hide auth, safety, or remote failures."""
    archive = tmp_path / "attempt.tar"
    calls = tmp_path / "calls"
    result = _bash(f"""
        KUBECTL_COLLECTION_RETRY_BACKOFF_SECONDS=0
        kubectl_pvc_exec() {{
            printf 'call\\n' >> {str(calls)!r}
            printf partial
            printf '%b\\n' {error!r} >&2
            return {rc}
        }}
        ! kubectl_stream_remote_attempt test-ns collector 1234abcd {str(archive)!r}
        test ! -e {str(archive)!r}
        """)
    assert result.returncode == 0, result.stderr
    assert len(calls.read_text().splitlines()) == 1
    assert f"STORAGE_SCALE_TEST_DIAGNOSTIC_REASON={reason}" in result.stderr
    assert f"producer_rc={rc} consumer_rc=0" in result.stderr


def test_collection_stream_reads_only_published_state(tmp_path: Path) -> None:
    """Unused control files and locks cannot make terminal receipt fail."""
    attempt = tmp_path / "1234abcd"
    state = attempt / "state"
    state.mkdir(parents=True)
    (state / "run.status").write_text("SUCCESS\n")
    control = attempt / "control/executions"
    control.mkdir(parents=True)
    (control / "0002.sh").write_text("immutable definition\n")
    (attempt / "coordinator.lock").write_text("lock\n")
    archive = tmp_path / "received.tar"
    result = _bash(f"""
        kubectl_attempt_remote_root() {{ printf '%s\\n' {str(attempt)!r}; }}
        kubectl_remote_tree_guard_script() {{ printf 'run="$1"\\n'; }}
        kubectl_pvc_exec() {{ shift 2; "$@"; }}
        kubectl_stream_remote_attempt test-ns collector 1234abcd {str(archive)!r}
        """)
    assert result.returncode == 0, result.stderr
    with tarfile.open(archive) as received:
        assert set(received.getnames()) == {
            "1234abcd/state",
            "1234abcd/state/run.status",
        }


def test_changed_source_retry_still_rejects_content_corruption(tmp_path: Path) -> None:
    """[R-07] Clean tar completion cannot override publication digests."""
    state, definitions = _complete_publication_fixture(tmp_path)
    (state / "results/0001/executions/0001.workload.tsv").write_text("changed\n")
    payload = tmp_path / "corrupt.tar"
    with tarfile.open(payload, "w") as archive:
        archive.add(state, arcname="1234abcd/state")
    received = tmp_path / "received.tar"
    calls = tmp_path / "calls"
    staging = tmp_path / ".kubernetes-collect-1234abcd.A1"
    result = _bash(f"""
        set -euo pipefail
        KUBECTL_COLLECTION_RETRY_BACKOFF_SECONDS=0
        kubectl_pvc_exec() {{
            printf 'call\\n' >> {str(calls)!r}
            if [[ $(wc -l < {str(calls)!r}) -eq 1 ]]; then
                printf partial
                echo 'tar: 1234abcd/state/run.status: file changed as we read it' >&2
                return 1
            fi
            cat {str(payload)!r}
        }}
        kubectl_stream_remote_attempt test-ns collector 1234abcd {str(received)!r}
        kubectl_extract_attempt_archive {str(received)!r} 1234abcd \
            {str(tmp_path)!r} {str(staging)!r}
        ! _kubectl_validate_collected_publication {str(staging / '1234abcd/state')!r} \
            1234abcd terminal {str(definitions)!r}
        """)
    assert result.returncode == 0, result.stderr
    assert len(calls.read_text().splitlines()) == 2


def test_tar_changed_file_warning_can_be_metadata_only(tmp_path: Path) -> None:
    """A real tar warning does not prove that benchmark bytes were modified."""
    source = tmp_path / "report.out"
    source.write_bytes(b"unchanged content\n" * 65536)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    archive = tmp_path / "metadata-change.tar"
    result = _bash(f"""
        export TAR_SOURCE={str(source)!r}
        LC_ALL=C _kubectl_local_tar --checkpoint=1 \
            --checkpoint-action='exec=touch -m -t 200001010000 "$TAR_SOURCE"' \
            -cf {str(archive)!r} -C {str(tmp_path)!r} report.out
        """)
    assert result.returncode == 1, result.stderr
    assert "file changed as we read it" in result.stderr
    assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
    with tarfile.open(archive) as received:
        assert (
            hashlib.sha256(received.extractfile("report.out").read()).hexdigest()
            == digest
        )


def test_collection_changed_source_exhaustion_preserves_remote_data(
    tmp_path: Path,
) -> None:
    """[R-03] Persistently changing source never becomes a valid receipt."""
    archive = tmp_path / "attempt.tar"
    calls = tmp_path / "calls"
    result = _bash(f"""
        KUBECTL_COLLECTION_RETRY_BACKOFF_SECONDS=0
        kubectl_pvc_exec() {{
            printf 'call\\n' >> {str(calls)!r}
            printf partial
            echo 'tar: 1234abcd/state/run.status: file changed as we read it' >&2
            return 1
        }}
        ! kubectl_stream_remote_attempt test-ns collector 1234abcd {str(archive)!r}
        test ! -e {str(archive)!r}
        """)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().count("call") == 3
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=TRANSFER_FAILED" in result.stderr
    assert r"remote\ results\ were\ retained" in result.stderr


def test_collection_retry_limit_keeps_bounded_error_history(tmp_path: Path) -> None:
    """[R-03] Exhaustion preserves each failed transfer without partial data."""
    archive = tmp_path / "attempt.tar"
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    result = _bash(f"""
        KUBECTL_COLLECTION_RETRY_BACKOFF_SECONDS=0
        kubectl_pvc_exec() {{
            printf partial
            printf 'error: unexpected EOF\\n' >&2
            head -c 2097152 /dev/zero | tr '\\0' E >&2
            return 1
        }}
        ! kubectl_stream_remote_attempt test-ns collector 1234abcd \
            {str(archive)!r} {str(metadata / 'state.sh')!r}
        test ! -e {str(archive)!r}
        """)
    assert result.returncode == 0, result.stderr
    bundle = next((metadata / "diagnostics").glob("collection-stream.*"))
    errors = list(bundle.glob("transfer-*.stderr"))
    assert len(errors) == 3
    assert all(path.stat().st_size <= 131072 for path in errors)
    assert len((bundle / "transfers.tsv").read_text().splitlines()) == 3
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_PATH=" in result.stderr


def test_collection_retries_share_one_deadline(tmp_path: Path) -> None:
    """[R-04] Backoff cannot extend the collection operation's deadline."""
    calls = tmp_path / "calls"
    result = _bash(f"""
        KUBECTL_COLLECTION_TIMEOUT_SECONDS=30
        KUBECTL_COLLECTION_RETRY_BACKOFF_SECONDS=60
        kubectl_pvc_exec() {{
            printf 'call\\n' >> {str(calls)!r}
            printf 'error: unexpected EOF\\n' >&2
            return 1
        }}
        sleep() {{ echo 'unexpected sleep' >&2; return 99; }}
        ! kubectl_stream_remote_attempt test-ns collector 1234abcd \
            {str(tmp_path / 'attempt.tar')!r}
        """)
    assert result.returncode == 0, result.stderr
    assert len(calls.read_text().splitlines()) == 1
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=TIMEOUT" in result.stderr
    assert "unexpected sleep" not in result.stderr


@pytest.mark.parametrize(
    "consumer_rc,reason", [(65, "ARCHIVE_INVALID"), (74, "LOCAL_IO")]
)
def test_collection_does_not_retry_consumer_failures(
    tmp_path: Path, consumer_rc: int, reason: str
) -> None:
    """A transient-looking producer error cannot override local receive failure."""
    calls = tmp_path / "calls"
    result = _bash(f"""
        kubectl_pvc_exec() {{
            printf 'call\\n' >> {str(calls)!r}
            printf 'error: HTTP 503 Service Unavailable\\n' >&2
            return 1
        }}
        _kubectl_write_bounded_collection_stream() {{ return {consumer_rc}; }}
        ! kubectl_stream_remote_attempt test-ns collector 1234abcd \
            {str(tmp_path / 'attempt.tar')!r}
        """)
    assert result.returncode == 0, result.stderr
    assert len(calls.read_text().splitlines()) == 1
    assert f"STORAGE_SCALE_TEST_DIAGNOSTIC_REASON={reason}" in result.stderr
    assert f"consumer_rc={consumer_rc}" in result.stderr


def test_collection_retry_diagnostics_are_bounded_and_keep_current_bundle(
    tmp_path: Path,
) -> None:
    """Equal timestamps cannot prune the error currently being captured."""
    metadata = tmp_path / "metadata"
    root = metadata / "diagnostics"
    root.mkdir(parents=True)
    for number in range(10):
        (root / f"z-old-{number}").mkdir()
    result = _bash(f"""
        kubectl_pvc_exec() {{ printf 'Forbidden\\n' >&2; return 1; }}
        _kubectl_local_path_mtime() {{ printf '0\\n'; }}
        ! kubectl_stream_remote_attempt test-ns collector 1234abcd \
            {str(tmp_path / 'attempt.tar')!r} {str(metadata / 'state.sh')!r}
        """)
    assert result.returncode == 0, result.stderr
    assert len(list(root.iterdir())) == 8
    bundle = next(root.glob("collection-stream.*"))
    assert (bundle / "transfer-1.stderr").read_text() == "Forbidden\n"


def test_collection_retry_uses_remaining_deadline_and_tolerates_diagnostic_failure(
    tmp_path: Path,
) -> None:
    """Retries do not restart the clock or depend on a writable diagnostic path."""
    metadata = tmp_path / "metadata"
    metadata.mkdir()
    (metadata / "diagnostics").write_text("not a directory")
    calls = tmp_path / "calls"
    archive = tmp_path / "attempt.tar"
    result = _bash(f"""
        KUBECTL_COLLECTION_TIMEOUT_SECONDS=30
        KUBECTL_COLLECTION_RETRY_BACKOFF_SECONDS=0
        sleep() {{ SECONDS=$((SECONDS + 5)); }}
        kubectl_pvc_exec() {{
            printf '%s\\n' "$KUBECTL_PROCESS_TIMEOUT_SECONDS" >> {str(calls)!r}
            if [[ $(wc -l < {str(calls)!r}) == 1 ]]; then
                printf partial
                printf 'error: unexpected EOF\\n' >&2
                return 1
            fi
            printf complete
        }}
        kubectl_stream_remote_attempt test-ns collector 1234abcd \
            {str(archive)!r} {str(metadata / 'state.sh')!r}
        """)
    assert result.returncode == 0, result.stderr
    deadlines = [int(value) for value in calls.read_text().splitlines()]
    assert len(deadlines) == 2
    assert 0 < deadlines[1] <= deadlines[0] <= 10
    assert archive.read_text() == "complete"
    assert "diagnostics could not be saved" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=" not in result.stderr


def test_collection_hung_first_transfer_leaves_budget_for_recovery(tmp_path):
    """A producer timeout must not consume the entire recovery deadline."""
    calls = tmp_path / "calls"
    archive = tmp_path / "received.tar"
    result = _bash(f"""
        KUBECTL_COLLECTION_TIMEOUT_SECONDS=30
        KUBECTL_COLLECTION_RETRY_BACKOFF_SECONDS=0
        _kubectl_stream_attempt_once() {{
            local -n result="$9"
            printf '%s\\n' "$8" >> {str(calls)!r}
            : > "$5"
            if [[ $(wc -l < {str(calls)!r}) == 1 ]]; then
                SECONDS=$((SECONDS + $8))
                printf partial > "$4"
                result=(124 0)
            else
                printf complete > "$4"
                result=(0 0)
            fi
        }}
        kubectl_stream_remote_attempt test-ns collector 1234abcd {str(archive)!r}
        """)
    assert result.returncode == 0, result.stderr
    assert len(calls.read_text().splitlines()) == 2
    assert archive.read_text() == "complete"


def test_collection_retry_after_producer_timeout_uses_remaining_deadline(tmp_path):
    """A slow archive cut short by the reserve cap is not capped again."""
    calls = tmp_path / "calls"
    archive = tmp_path / "received.tar"
    result = _bash(f"""
        KUBECTL_COLLECTION_TIMEOUT_SECONDS=30
        KUBECTL_COLLECTION_RETRY_BACKOFF_SECONDS=0
        _kubectl_stream_attempt_once() {{
            local -n result="$9"
            printf '%s\\n' "$8" >> {str(calls)!r}
            : > "$5"
            if (($8 < 15)); then
                SECONDS=$((SECONDS + $8))
                printf partial > "$4"
                result=(124 0)
            else
                printf complete > "$4"
                result=(0 0)
            fi
        }}
        kubectl_stream_remote_attempt test-ns collector 1234abcd {str(archive)!r}
        """)
    assert result.returncode == 0, result.stderr
    deadlines = [int(value) for value in calls.read_text().splitlines()]
    assert deadlines[0] == 10
    assert 15 <= deadlines[1] <= 20
    assert archive.read_text() == "complete"


@pytest.mark.parametrize(
    "fault",
    ["eof", "noisy-eof", "lost-ack", "corrupt", "forbidden", "different-existing"],
)
def test_control_upload_atomic_publication_and_safe_retry(tmp_path, fault):
    """Real helper scripts never publish partial controls or overwrite a retry."""
    source = tmp_path / "bundle"
    source.mkdir()
    (source / "env_used.sh").write_text("verified controls\n")
    remote = tmp_path / "remote"
    (remote / "control/executions").mkdir(parents=True)
    if fault == "different-existing":
        (remote / "control/env_used.sh").write_text("original controls\n")
    calls = tmp_path / "calls"
    result = _bash(f"""
        sleep() {{ :; }}
        kubectl_attempt_remote_root() {{ printf '%s\\n' {str(remote)!r}; }}
        kubectl_remote_tree_guard_script() {{ printf 'run=$1; run_real=$run;'; }}
        kubectl_pvc_exec_stdin() {{
            printf 'call\\n' >> {str(calls)!r}
            shift 2
            if [[ $(wc -l < {str(calls)!r}) == 1 ]]; then
                case {fault!r} in
                    eof) echo 'error: unexpected EOF' >&2; return 1 ;;
                    noisy-eof) echo 'error: unexpected EOF' >&2; printf 'padding%.0s' {{1..4096}} >&2; return 1 ;;
                    forbidden) echo Forbidden >&2; return 1 ;;
                    corrupt) head -c 5 | "$@"; return $? ;;
                    lost-ack) "$@" || return $?; echo 'error: unexpected EOF' >&2; return 1 ;;
                esac
            fi
            "$@"
        }}
        kubectl_upload_control_bundle test-ns transfer 1234abcd {str(source)!r}
        """)
    expected_success = fault in {"eof", "noisy-eof", "lost-ack"}
    assert (result.returncode == 0) == expected_success, result.stderr
    assert len(calls.read_text().splitlines()) == (2 if expected_success else 1)
    if expected_success:
        assert (remote / "control/env_used.sh").read_bytes() == (
            source / "env_used.sh"
        ).read_bytes()
    elif fault == "different-existing":
        assert (remote / "control/env_used.sh").read_text() == "original controls\n"
    else:
        assert list((remote / "control/executions").iterdir()) == []
    assert not list(remote.glob(".control-upload.*"))
    assert len(result.stderr) < 9000


@pytest.mark.parametrize("source_mode", [0o755, 0o777])
def test_control_upload_preserves_private_root_and_nested_executable(
    tmp_path: Path, source_mode: int
) -> None:
    """Archive root metadata must not replace the private publication root."""
    source = tmp_path / "bundle"
    executions = source / "executions"
    executions.mkdir(parents=True)
    source.chmod(source_mode)
    executable = executions / "0001.sh"
    executable.write_text("#!/bin/bash\nprintf 'verified executable\\n'\n")
    executable.chmod(0o700)
    remote = tmp_path / "remote"
    (remote / "control/executions").mkdir(parents=True)
    result = _bash(f"""
        umask 022
        kubectl_attempt_remote_root() {{ printf '%s\\n' {str(remote)!r}; }}
        kubectl_remote_tree_guard_script() {{ printf 'run=$1; run_real=$run;'; }}
        kubectl_pvc_exec_stdin() {{ shift 2; "$@"; }}
        kubectl_upload_control_bundle test-ns transfer 1234abcd {str(source)!r}
        """)
    assert result.returncode == 0, result.stderr
    published = remote / "control"
    assert published.stat().st_mode & 0o777 == 0o700
    published_executable = published / "executions/0001.sh"
    assert published_executable.read_bytes() == executable.read_bytes()
    assert published_executable.stat().st_mode & 0o777 == 0o700
    invocation = subprocess.run(
        [str(published_executable)], capture_output=True, text=True, check=False
    )
    assert invocation.returncode == 0, invocation.stderr
    assert invocation.stdout == "verified executable\n"
    assert not list(remote.glob(".control-upload.*"))


def test_collection_stream_failure_removes_partial_and_reports_auth(
    tmp_path: Path,
) -> None:
    """[R-03] Failed API stream keeps remote truth and no partial archive."""
    archive = tmp_path / "attempt.tar"
    result = _bash(f"""
        KUBECTL_NAMESPACE=test-ns
        KUBECTL_LIFECYCLE_STATE=TERMINAL
        kubectl_pvc_exec() {{
            printf partial
            printf 'Unauthorized: expired token\n' >&2
            return 1
        }}
        ! kubectl_stream_remote_attempt test-ns collector 1234abcd \
            {str(archive)!r} /local/state.sh /remote/state/run.status \
            Job sweep test-ns job-uid ACTIVE
        test ! -e {str(archive)!r}
        """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=AUTH" in result.stderr
    assert "remote\\ results\\ were\\ retained" in result.stderr
    assert (
        "STORAGE_SCALE_TEST_DIAGNOSTIC_LOCAL_STATE_PATH=/local/state.sh"
        in result.stderr
    )
    assert (
        "STORAGE_SCALE_TEST_DIAGNOSTIC_REMOTE_STATE_PATH=/remote/state/run.status"
        in result.stderr
    )
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_NAME=sweep" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_JOB_EVIDENCE=ACTIVE" in result.stderr


def test_collection_stream_bounds_producer_error_capture(tmp_path: Path) -> None:
    """A noisy failed exec cannot fill local storage through diagnostic stderr."""
    archive = tmp_path / "attempt.tar"
    observed = tmp_path / "observed-bytes"
    result = _bash(f"""
        kubectl_pvc_exec() {{
            head -c 2097152 /dev/zero | tr '\\0' E >&2
            return 1
        }}
        _kubectl_classify_observation_failure() {{
            printf '%s\n' "${{#3}}" > {str(observed)!r}
            printf -v "$1" API_UNAVAILABLE
        }}
        ! kubectl_stream_remote_attempt test-ns collector 1234abcd \
            {str(archive)!r}
        [[ $(cat {str(observed)!r}) -le 131072 ]]
        test ! -e {str(archive)!r}
        """)
    assert result.returncode == 0, result.stderr


def test_noisy_successful_collection_stream_is_not_killed(tmp_path: Path) -> None:
    """Discarded diagnostic overflow cannot SIGPIPE an otherwise valid stream."""
    archive = tmp_path / "attempt.tar"
    result = _bash(f"""
        kubectl_pvc_exec() {{
            printf archive-data
            head -c 2097152 /dev/zero | tr '\\0' E >&2
        }}
        kubectl_stream_remote_attempt test-ns collector 1234abcd \
            {str(archive)!r}
        [[ $(cat {str(archive)!r}) == archive-data ]]
        """)
    assert result.returncode == 0, result.stderr


def test_integration_collection_hold_is_an_explicit_pre_stream_boundary(
    tmp_path: Path,
) -> None:
    """The real-fixture interruption handshake precedes archive receipt."""
    work = tmp_path / ".kubernetes-collect-work-1234abcd.A1"
    work.mkdir()
    archive = work / "attempt.tar"
    hold = tmp_path / ".integration-kubectl-collection-ready"
    result = _bash(f"""
        STORAGE_SCALE_TEST_INTEGRATION=1
        KUBECTL_INTEGRATION_COLLECTION_HOLD_FILE={str(hold)!r}
        kubectl_pvc_exec() {{ printf archive-data; }}
        kubectl_stream_remote_attempt test-ns collector 1234abcd \
            {str(archive)!r} &
        pid=$!
        for _ in $(seq 1 100); do
            [[ -f {str(hold)!r} ]] && break
            sleep 0.01
        done
        [[ -f {str(hold)!r} && ! -e {str(archive)!r} ]]
        rm -- {str(hold)!r}
        wait "$pid"
        [[ $(cat {str(archive)!r}) == archive-data ]]
        """)
    assert result.returncode == 0, result.stderr


def test_collection_byte_limit_is_enforced_while_streaming(tmp_path: Path) -> None:
    """[R-05] Receive stops an oversized stream before filling local storage."""
    archive = tmp_path / "attempt.tar"
    result = _bash(f"""
        printf 12345678 | _kubectl_write_bounded_collection_stream \
            {str(archive)!r} 8
        test "$(cat {str(archive)!r})" = 12345678
        rm -- {str(archive)!r}

        ! printf 123456789 | _kubectl_write_bounded_collection_stream \
            {str(archive)!r} 8
        test ! -e {str(archive)!r}
        """)
    assert result.returncode == 0, result.stderr
    assert "exceeded its byte limit while streaming" in result.stderr


def test_collection_byte_count_accepts_bsd_wc_padding(tmp_path: Path) -> None:
    """Collection accepts the leading padding emitted by BSD wc."""
    archive = tmp_path / "attempt.tar"
    result = _bash(f"""
        wc() {{ printf '       8\n'; }}
        printf 12345678 | _kubectl_write_bounded_collection_stream \
            {str(archive)!r} 8
        test "$(cat {str(archive)!r})" = 12345678
        """)
    assert result.returncode == 0, result.stderr


def test_collection_capacity_checks_the_results_filesystem(tmp_path: Path) -> None:
    """[R-09] Collection checks capacity on its actual local filesystem."""
    result = _bash(f"""
        calls={str(tmp_path / 'df-calls')!r}
        df() {{
            printf x >> "$calls"
            printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
            if [[ "$capacity_mode" == low ]]; then
                printf 'fixture 1000 999 1 99%% /fixture\\n'
            else
                printf 'fixture 100000 1 99999 1%% /fixture\\n'
            fi
        }}
        capacity_mode=low
        ! _kubectl_require_collection_capacity {str(tmp_path)!r} 1024
        capacity_mode=enough
        _kubectl_require_collection_capacity {str(tmp_path)!r} 1024
        test "$(cat "$calls")" = xx
        """)
    assert result.returncode == 0, result.stderr
    assert "insufficient local free space" in result.stderr
    assert f"available below {tmp_path}" in result.stderr


def test_collection_scavenges_only_owned_staging_paths(tmp_path: Path) -> None:
    """[R-10] Retry removes owned staging without following hostile links."""
    stale = tmp_path / ".kubernetes-collect-1234abcd.A1"
    stale_work = tmp_path / ".kubernetes-collect-work-1234abcd.B2"
    unrelated = tmp_path / ".kubernetes-collect-not-ours"
    outside = tmp_path / "outside"
    stale.mkdir()
    stale_work.mkdir()
    unrelated.mkdir()
    outside.mkdir()
    result = _bash(f"""
        _kubectl_scavenge_collection_staging {str(tmp_path)!r}
        test ! -e {str(stale)!r}
        test ! -e {str(stale_work)!r}
        test -d {str(unrelated)!r}

        ln -s {str(outside)!r} {str(stale)!r}
        ! _kubectl_scavenge_collection_staging {str(tmp_path)!r}
        test -L {str(stale)!r}
        test -d {str(outside)!r}
        """)
    assert result.returncode == 0, result.stderr
    assert "unsafe stale Kubernetes collection staging path" in result.stderr


def test_bounded_kubectl_owns_the_child_process_group() -> None:
    """A wedged exec cannot evade timeout through foreground mode."""
    source = _FUNCTIONS.read_text(encoding="utf-8")
    body = source.split("kubectl_run_bounded() {", 1)[1].split("\n}", 1)[0]
    assert "_kubectl_local_timeout --kill-after=5s" in body
    assert "timeout --foreground" not in body


def test_macos_uses_homebrew_timeout_and_tar_commands() -> None:
    """Darwin launchers use GNU tools under Homebrew's prefixed names."""
    result = _bash("""
        uname() { printf 'Darwin\n'; }
        gtimeout() { printf 'timeout:%s\n' "$*"; }
        gtar() { printf 'tar:%s\n' "$*"; }
        timeout() { printf 'wrong timeout\n'; return 91; }
        tar() { printf 'wrong tar\n'; return 92; }
        timeout_output=$(_kubectl_local_timeout --kill-after=5s 30s true)
        tar_output=$(_kubectl_local_tar -tf archive.tar)
        [[ "$timeout_output" == 'timeout:--kill-after=5s 30s true' ]]
        [[ "$tar_output" == 'tar:-tf archive.tar' ]]
        """)
    assert result.returncode == 0, result.stderr


def test_collection_waits_for_exact_journaled_job_quiescence(
    tmp_path: Path,
) -> None:
    """Terminal ledger state alone is not sufficient collection evidence."""
    result = _bash(f"""
        calls={str(tmp_path / 'calls')!r}
        kubectl_attempt_load_resource() {{
          KUBECTL_RESOURCE_KIND=Job
          KUBECTL_RESOURCE_NAME=sweep
          KUBECTL_RESOURCE_NAMESPACE=test-ns
          KUBECTL_RESOURCE_NONCE=0123456789abcdef0123456789abcdef
          KUBECTL_RESOURCE_UID=job-uid
        }}
        kubectl_attempt_step_done() {{ return 1; }}
        kubectl_run_bounded() {{ printf '%s' job-uid; }}
        kubectl_job_terminal_state() {{
          printf x >> "$calls"
          if [[ $(wc -c < "$calls") -eq 1 ]]; then
            return 2
          fi
          printf -v "$1" '%s' COMPLETE
        }}
        sleep() {{ :; }}
        kubectl_wait_journaled_job_quiescent {str(tmp_path)!r} 1234abcd 2
        test "$(cat "$calls")" = xx
        """)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "malformed",
    (
        "node-a\\tpod-a\\tpoduid-a\\t10.0.0.999\\tTrue\\tsha256:one\\t\\n",
        "node-a\\tbad/pod\\tpoduid-a\\t10.0.0.1\\tTrue\\tsha256:one\\t\\n",
        "node-a\\tpod-a\\tbad uid\\t10.0.0.1\\tTrue\\tsha256:one\\t\\n",
    ),
)
def test_fake_kubectl_rejects_malformed_worker_endpoint_fields(
    tmp_path: Path, malformed: str
) -> None:
    """Malformed Pod evidence cannot bypass endpoint discovery validation."""
    nodes = tmp_path / "nodes.tsv"
    nodes.write_text("node-a\\tuid-a\\tamd64\\n", encoding="utf-8")
    output = tmp_path / "workers.tsv"
    result = _bash(f"""
        kubectl_run_bounded() {{
          if [[ "$*" == *"get nodes"* ]]; then
            printf 'node-a\\tuid-a\\tamd64\\tTrue\\t\\n'
          else
            printf %s {malformed!r}
          fi
        }}
        ! kubectl_discover_worker_endpoints test-ns 1234abcd \\
            {str(nodes)!r} {str(output)!r}
        """)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("rows", "expected_rc", "expected_status"),
    (
        (
            "node-a\tpod-new\tuid-new\t10.0.0.1\tTrue\tsha256:one\t\n",
            0,
            "REPLACED_SAME_IP",
        ),
        (
            "node-a\tpod-new\tuid-new\t10.0.0.9\tTrue\tsha256:one\t\n",
            2,
            "IP_DRIFT",
        ),
    ),
)
def test_worker_endpoint_comparison_reports_replacement_and_ip_drift(
    tmp_path: Path, rows: str, expected_rc: int, expected_status: str
) -> None:
    """[C-10] Frozen workers tolerate only same-IP semantic replacement."""
    nodes = tmp_path / "nodes.tsv"
    nodes.write_text("node-a\tuid-node\tamd64\n", encoding="utf-8")
    frozen = tmp_path / "frozen.tsv"
    frozen.write_text(
        "node-a\tuid-node\tpod-old\tuid-old\t10.0.0.1\tamd64\tsha256:one\n",
        encoding="utf-8",
    )
    report = tmp_path / "endpoint-drift.tsv"
    result = _bash(f"""
        export KUBECTL_REQUEST_TIMEOUT_SECONDS=2 KUBECTL_PROCESS_TIMEOUT_SECONDS=5
        kubectl_run_bounded() {{
            if [[ "$*" == *"get nodes"* ]]; then
                printf 'node-a\\tuid-node\\tamd64\\tTrue\\t\\n'
            else
                printf %b {rows!r}
            fi
        }}
        kubectl_compare_worker_endpoints test-ns 1234abcd {str(nodes)!r} \\
            {str(frozen)!r} {str(report)!r}
        rc=$?
        test "$rc" -eq {expected_rc}
        grep -F $'node-a\\tpod-old\\tuid-old\\t10.0.0.1\\tpod-new\\tuid-new' {str(report)!r}
        grep -F $'\\t{expected_status}' {str(report)!r}
        """)
    assert result.returncode == 0, result.stderr


def test_worker_endpoint_comparison_refreshes_existing_evidence(tmp_path: Path) -> None:
    """Repeated status checks atomically replace their prior endpoint report."""
    nodes = tmp_path / "nodes.tsv"
    nodes.write_text("node-a\tuid-node\tamd64\n", encoding="utf-8")
    frozen = tmp_path / "frozen.tsv"
    frozen.write_text(
        "node-a\tuid-node\tpod-old\tuid-old\t10.0.0.1\tamd64\tsha256:one\n",
        encoding="utf-8",
    )
    report = tmp_path / "endpoint-drift.tsv"
    invocation = tmp_path / "invocation"
    result = _bash(f"""
        kubectl_run_bounded() {{
            if [[ "$*" == *"get nodes"* ]]; then
                printf 'node-a\\tuid-node\\tamd64\\tTrue\\t\\n'
            elif [[ -e {str(invocation)!r} ]]; then
                printf 'node-a\\tpod-new\\tuid-new\\t10.0.0.9\\tTrue\\tsha256:one\\t\\n'
            else
                : > {str(invocation)!r}
                printf 'node-a\\tpod-old\\tuid-old\\t10.0.0.1\\tTrue\\tsha256:one\\t\\n'
            fi
        }}
        kubectl_compare_worker_endpoints test-ns 1234abcd {str(nodes)!r} \\
            {str(frozen)!r} {str(report)!r}
        kubectl_compare_worker_endpoints test-ns 1234abcd {str(nodes)!r} \\
            {str(frozen)!r} {str(report)!r} || rc=$?
        test "${{rc:-0}}" -eq 2
        grep -F $'\\tIP_DRIFT' {str(report)!r}
        """)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("node_row", "pod_image", "expected_status"),
    (
        ("node-a\tuid-recreated\tamd64\tTrue\t\n", "sha256:one", "NODE_DRIFT"),
        ("node-a\tuid-node\tarm64\tTrue\t\n", "sha256:one", "ARCH_DRIFT"),
        ("node-a\tuid-node\tamd64\tTrue\t\n", "sha256:two", "IMAGE_DRIFT"),
    ),
)
def test_worker_endpoint_comparison_checks_all_frozen_identity_fields(
    tmp_path: Path, node_row: str, pod_image: str, expected_status: str
) -> None:
    """[C-11] Node recreation, architecture, and image drift fail closed."""
    nodes = tmp_path / "nodes.tsv"
    nodes.write_text("node-a\tuid-node\tamd64\n", encoding="utf-8")
    frozen = tmp_path / "frozen.tsv"
    frozen.write_text(
        "node-a\tuid-node\tpod-a\tpod-uid\t10.0.0.1\tamd64\tsha256:one\n",
        encoding="utf-8",
    )
    report = tmp_path / "endpoint-drift.tsv"
    result = _bash(f"""
        kubectl_run_bounded() {{
          if [[ "$*" == *"get nodes"* ]]; then
            printf %b {node_row!r}
          else
            printf 'node-a\\tpod-a\\tpod-uid\\t10.0.0.1\\tTrue\\t{pod_image}\\t\\n'
          fi
        }}
        kubectl_compare_worker_endpoints test-ns 1234abcd {str(nodes)!r} \
          {str(frozen)!r} {str(report)!r} || rc=$?
        test "${{rc:-0}}" -eq 2
        grep -F $'\\t{expected_status}' {str(report)!r}
        """)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "row",
    (
        "node-a\\tpod-a\\tpoduid-a\\t999.0.0.1\\tTrue\\tsha256:one\\t",
        "node-a\\tBad_Pod\\tpoduid-a\\t10.0.0.1\\tTrue\\tsha256:one\\t",
        "node-a\\tpod-a\\tbad/uid\\t10.0.0.1\\tTrue\\tsha256:one\\t",
    ),
)
def test_worker_discovery_rejects_each_malformed_endpoint_field(
    tmp_path: Path, row: str
) -> None:
    """A malformed IP, Pod name, or Pod UID cannot pass a compound guard."""
    nodes = tmp_path / "nodes.tsv"
    nodes.write_text("node-a\tuid-a\tamd64\n", encoding="utf-8")
    output = tmp_path / "workers.tsv"
    result = _bash(f"""
        kubectl_run_bounded() {{
          if [[ "$*" == *"get nodes"* ]]; then
            printf 'node-a\\tuid-a\\tamd64\\tTrue\\t\\n'
          else
            printf '{row}\\n'
          fi
        }}
        ! kubectl_discover_worker_endpoints test-ns 1234abcd \
          {str(nodes)!r} {str(output)!r}
        """)
    assert result.returncode == 0, result.stderr
    assert not output.exists()


def test_helper_template_is_rendered_and_removed_when_readiness_fails() -> None:
    """[R-02] An unready collector helper is removed with diagnosis."""
    result = _bash("""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        captured=$(mktemp)
        kubectl_create_owned_object() { printf '%s' "$7" > "$captured"; printf -v "$1" uid-1; }
        kubectl_wait_owned_ready_pod() { return 1; }
        kubectl_delete_owned_object() { printf deleted; }
        ! kubectl_create_helper_pod uid transfer test-ns sst-elb-1234abcd-upload-1234 \\
          0123456789abcdef0123456789abcdef 1234abcd node-a 1234
        [[ $(cat "$captured") != *@@* ]]
        grep -q 'storage-scale-test.nvidia.com/operation: 1234' "$captured"
        grep -q 'KUBECTL_REMOTE_RUN_DIRECTORY' "$captured"
        grep -q '/mnt/storage-scale-test/benchmark/.storage-scale-test/runs/1234abcd' "$captured"
        grep -q 'automountServiceAccountToken: false' "$captured"
        """)
    assert result.returncode == 0, result.stderr
    assert "deleted" in result.stdout


def test_helper_creation_returns_uid_to_common_caller_variable_names() -> None:
    """Nested Bash output variables must not be shadowed by helper locals."""
    result = _bash("""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        kubectl_run_bounded() { :; }
        kubectl_verify_object_identity() { printf 'uid-1\n'; }
        kubectl_wait_owned_ready_pod() { return 0; }
        kubectl_create_owned_object uid Pod helper test-ns \
          0123456789abcdef0123456789abcdef 1234abcd manifest
        [[ "$uid" == uid-1 ]]
        kubectl_create_helper_pod helper_uid transfer test-ns \
          sst-elb-1234abcd-upload-1234 \
          0123456789abcdef0123456789abcdef 1234abcd node-a 1234
        [[ "$helper_uid" == uid-1 ]]
        """)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("variable", "invalid"),
    (
        ("KUBECTL_NAMESPACE", "Bad_Name"),
        ("KUBECTL_PV", "bad/pv"),
        ("KUBECTL_PVC", "bad/pvc"),
        ("KUBECTL_NODE_SELECTOR", "bad selector"),
        ("KUBECTL_ELBENCHO_IMAGE", "bad image"),
        ("KUBECTL_IMAGE_PULL_POLICY", "Sometimes"),
        ("KUBECTL_RUN_AS_USER", "0"),
        ("KUBECTL_RUN_AS_GROUP", "group"),
    ),
)
def test_runtime_configuration_rejects_every_invalid_field(
    variable: str, invalid: str
) -> None:
    """[S-01] Every independent runtime configuration field is validated."""
    result = _bash(f"""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV='bad/pv' KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        export KUBECTL_PV=test-pv
        export {variable}={invalid!r}
        ! kubectl_validate_runtime_configuration
        """)
    assert result.returncode == 0, result.stderr
    assert f"Error: {variable}" in result.stderr
    assert "invalid Kubernetes filesystem sweep configuration" not in result.stderr


def test_runtime_configuration_reports_every_missing_field() -> None:
    """[S-01] One validation run identifies every missing Kubernetes setting."""
    variables = (
        "KUBECTL_NAMESPACE",
        "KUBECTL_PV",
        "KUBECTL_PVC",
        "KUBECTL_NODE_SELECTOR",
        "KUBECTL_ELBENCHO_IMAGE",
        "KUBECTL_IMAGE_PULL_POLICY",
        "KUBECTL_RUN_AS_USER",
        "KUBECTL_RUN_AS_GROUP",
    )
    result = _bash("""
        unset KUBECTL_NAMESPACE KUBECTL_PV KUBECTL_PVC KUBECTL_NODE_SELECTOR
        unset KUBECTL_ELBENCHO_IMAGE KUBECTL_IMAGE_PULL_POLICY
        unset KUBECTL_RUN_AS_USER KUBECTL_RUN_AS_GROUP
        ! kubectl_validate_runtime_configuration
        """)
    assert result.returncode == 0, result.stderr
    for variable in variables:
        assert f"Error: {variable} is required" in result.stderr


def test_cluster_storage_contract_reports_expected_and_observed_values() -> None:
    """Storage validation identifies the mismatched PVC attribute and values."""
    result = _bash("""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never
        export KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        kubectl() { :; }
        kubectl_run_observational() {
            case "$*" in
                version) return 0 ;;
                auth\\ can-i\\ *\\ leases.coordination.k8s.io\\ -n\\ test-ns)
                    printf 'yes' ;;
                'get namespace test-ns -o jsonpath={.metadata.uid}')
                    printf 'namespace-uid' ;;
                'get pv test-pv -o jsonpath={.metadata.uid}') printf 'pv-uid' ;;
                '-n test-ns get pvc test-pvc -o jsonpath={.metadata.uid}')
                    printf 'pvc-uid' ;;
                '-n test-ns get pvc test-pvc -o jsonpath='*)
                    printf 'another-pv\tPending\tBlock\tReadWriteOnce' ;;
                *) printf 'unexpected command: %s\n' "$*" >&2; return 90 ;;
            esac
        }
        ! kubectl_validate_cluster_identity
        """)
    assert result.returncode == 0, result.stderr
    assert "PVC test-ns/test-pvc does not satisfy" in result.stderr
    assert "expected: volumeName=test-pv" in result.stderr
    assert "observed: volumeName=another-pv phase=Pending" in result.stderr


def test_cluster_identity_reports_denied_lease_permission() -> None:
    """[S-01] An authoritative RBAC denial names the missing permission."""
    result = _bash(r"""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never
        export KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        kubectl() { :; }
        kubectl_run_observational() {
          case "$*" in
            version) return 0 ;;
            auth\ can-i\ get\ leases.coordination.k8s.io\ -n\ test-ns)
              printf no
              return 1 ;;
            *) return 90 ;;
          esac
        }
        ! kubectl_validate_cluster_identity
    """)
    assert result.returncode == 0, result.stderr
    assert "require permission to get leases.coordination.k8s.io" in result.stderr
    assert "could not verify Kubernetes Lease permission" not in result.stderr


def test_prepare_failure_classification_preserves_local_capacity_and_path_causes() -> (
    None
):
    """[S-03] Preparation reports the actual local, capacity, and path boundary."""
    result = _bash("""
        kubectl_classify_prepare_failure reason local-identity 1 ''
        [[ "$reason" == LOCAL_IO ]]
        kubectl_classify_prepare_failure reason node-capacity 1 \
          'required=2 eligible=1'
        [[ "$reason" == INSUFFICIENT_CAPACITY ]]
        kubectl_classify_prepare_failure reason pvc-paths 1 \
          'path escapes PVC mount through a symlink'
        [[ "$reason" == PATH_REJECTED ]]
        [[ $(_kubectl_observation_safe_action PATH_REJECTED) == \
          *'correct the configured PVC path'* ]]
        [[ $(_kubectl_observation_safe_action INSUFFICIENT_CAPACITY) == \
          *'add eligible worker capacity'* ]]
        """)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("mode", "required", "phase", "reason"),
    (
        ("capacity", 2, "node-capacity", "INSUFFICIENT_CAPACITY"),
        ("path", 1, "pvc-paths", "PATH_REJECTED"),
    ),
)
def test_prepare_reports_specific_capacity_and_path_failures(
    tmp_path: Path, mode: str, required: int, phase: str, reason: str
) -> None:
    """Preparation preserves actionable phase and cause through rollback."""
    result = _bash(f"""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000
        export KUBECTL_RUN_AS_GROUP=2000
        declare -A mapped=([/mnt/storage-scale-test/bench]=1)
        mode={mode!r}
        kubectl_validate_cluster_identity() {{
            printf 'namespace-uid\tpv-uid\tpvc-uid\n'
        }}
        kubectl_discover_candidate_nodes() {{
            printf 'node-a\tuid-a\tamd64\n' > "$2"
        }}
        kubectl_choose_coordinator_node() {{ printf node-a; }}
        kubectl_create_helper_pod() {{ printf -v "$1" uid-transfer; }}
        kubectl_validate_pvc_paths() {{
            [[ "$mode" != path ]] || {{
                printf 'path escapes PVC mount through a symlink\n' >&2
                return 1
            }}
        }}
        kubectl_validate_test_root_access() {{ :; }}
        kubectl_acquire_pvc_lease() {{ :; }}
        kubectl_preserve_attempt_diagnostics() {{ :; }}
        kubectl_cleanup_journaled_resources() {{ :; }}
        ! kubectl_prepare_attempt_lifecycle attempt {str(tmp_path)!r} \
          {required} mapped '' benchmark "$KUBECTL_CONTROL_TEST_ROOT"
    """)
    assert result.returncode == 0, result.stderr
    assert f"STORAGE_SCALE_TEST_DIAGNOSTIC_PHASE={phase}" in result.stderr
    assert f"STORAGE_SCALE_TEST_DIAGNOSTIC_REASON={reason}" in result.stderr


def test_r20_cluster_uid_mismatch_emits_structured_diagnosis() -> None:
    """[R-20] A replacement cluster is refused with exact UID evidence."""
    result = _bash("""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_NAMESPACE_UID=namespace-old
        export KUBECTL_PV=test-pv KUBECTL_PV_UID=pv-old
        export KUBECTL_PVC=test-pvc KUBECTL_PVC_UID=pvc-old
        export KUBECTL_LIFECYCLE_STATE=SUBMITTED
        kubectl_validate_cluster_identity() {
            printf 'namespace-new\tpv-old\tpvc-old\n'
        }
        ! _kubectl_verify_saved_cluster_identity /tmp/kubernetes 1234abcd
        """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=IDENTITY_MISMATCH" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_KIND=Namespace" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_EXPECTED_UID=namespace-old" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_OBSERVED_UID=namespace-new" in result.stderr
    assert "do\\ not\\ adopt" in result.stderr


def test_c15_permanent_pvc_loss_is_detected_without_adoption() -> None:
    """[C-15] Missing durable storage fails closed with identity diagnosis."""
    result = _bash("""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_NAMESPACE_UID=namespace-old
        export KUBECTL_PV=test-pv KUBECTL_PV_UID=pv-old
        export KUBECTL_PVC=test-pvc KUBECTL_PVC_UID=pvc-old
        export KUBECTL_LIFECYCLE_STATE=SUBMITTED
        kubectl_validate_cluster_identity() {
            printf 'persistentvolumeclaim "test-pvc" not found\n' >&2
            return 1
        }
        ! _kubectl_verify_saved_cluster_identity /tmp/kubernetes 1234abcd
        """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=IDENTITY_MISMATCH" in result.stderr
    assert (
        "STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_KIND=PersistentVolumeClaim"
        in result.stderr
    )
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_EXPECTED_UID=pvc-old" in result.stderr


def test_always_pull_policy_requires_an_immutable_image_reference() -> None:
    """A coordinator repull cannot diverge from the frozen worker image."""
    result = _bash("""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Always KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        ! kubectl_validate_runtime_configuration
        export KUBECTL_ELBENCHO_IMAGE='breuner/elbencho:v3.2-1@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        kubectl_validate_runtime_configuration
        """)
    assert result.returncode == 0, result.stderr


def test_pvc_path_validator_defends_reserved_tree_and_future_parent() -> None:
    """PVC workload paths cannot enter the nested orchestration subtree."""
    result = _bash("""
        kubectl_pvc_exec() { printf '%s' "$5"; }
        captured=$(kubectl_validate_pvc_paths test-ns helper \
          /mnt/storage-scale-test/workload/future)
        [[ "$captured" == *'nearest existing parent'* ]]
        [[ "$captured" == *'workload path overlaps orchestration state'* ]]
        [[ "$captured" != *'orchestration state overlaps workload path'* ]]
        """)
    assert result.returncode == 0, result.stderr


def test_remote_control_tree_guard_checks_realpath_and_symlink_containment() -> None:
    """Every remote mutation shares the same reserved-run containment guard."""
    result = _bash("""
        guard=$(kubectl_remote_tree_guard_script)
        [[ "$guard" == *'realpath -e -- "$mount"'* ]]
        [[ "$guard" == *'[[ -d "$run" && ! -L "$run" ]]'* ]]
        [[ "$guard" == *'[[ "$run_real" == "$root_real/runs/$attempt" ]]'* ]]
        """)
    assert result.returncode == 0, result.stderr
    source = _FUNCTIONS.read_text(encoding="utf-8")
    for function in (
        "kubectl_initialize_remote_control_tree",
        "kubectl_upload_control_bundle",
        "kubectl_record_remote_status",
        "kubectl_stream_remote_attempt",
        "kubectl_recover_lost_coordinator",
        "kubectl_finalize_cancelled_attempt",
    ):
        body = source.split(f"{function}() {{", 1)[1].split("\n}", 1)[0]
        assert "kubectl_remote_tree_guard_script" in body, function
    assert source.count('-f "$run/owner"') >= 1
    assert source.count('! -L "$run/owner"') >= 1


def test_runtime_preflight_covers_complete_coordinator_command_contract() -> None:
    """The runtime image check names every external coordinator dependency."""
    source = _FUNCTIONS.read_text(encoding="utf-8")
    body = source.split("kubectl_validate_runtime_pod() {", 1)[1].split(
        "\n}\n\nkubectl_discover_candidate_nodes", 1
    )[0] + (
        _ROOT / "storage-tests/fs/kubectl/templates/validation-job.yaml.tmpl"
    ).read_text(
        encoding="utf-8"
    )
    for command in (
        "elbencho",
        "bash",
        "tar",
        "realpath",
        "timeout",
        "sha256sum",
        "awk",
        "find",
        "grep",
        "mkdir",
        "cp",
        "cmp",
        "date",
        "shuf",
        "mv",
        "wc",
        "cat",
        "sed",
        "rm",
        "sort",
        "basename",
        "dirname",
        "sleep",
        "mktemp",
        "tr",
        "xargs",
        "rmdir",
        "id",
        "cut",
        "od",
    ):
        assert command in body, command


def test_runtime_preflight_job_is_bounded_and_self_cleaning() -> None:
    """Interrupted validation cannot leave an unbounded sleeping helper."""
    result = _bash("""
        roots=$(kubectl_render_test_root_arguments \
          /mnt/storage-scale-test/alpha /mnt/storage-scale-test/benchmark)
        kubectl_render_attempt_template storage-tests/fs/kubectl/templates/validation-job.yaml.tmpl \
          NAMESPACE=test-ns RESOURCE_NAME=sst-elb-1234abcd-validation ATTEMPT_ID=1234abcd \
          OWNERSHIP_NONCE=0123456789abcdef0123456789abcdef IMAGE=breuner/elbencho:v3.2-1 \
          IMAGE_PULL_POLICY=Never RUN_AS_USER=2000 RUN_AS_GROUP=2000 PVC_NAME=test-pvc \
          NODE_NAME=node-a "TEST_ROOT_ARGUMENTS=$roots"
        """)
    assert result.returncode == 0, result.stderr
    job = yaml.safe_load(result.stdout)
    assert job["kind"] == "Job"
    assert job["spec"]["activeDeadlineSeconds"] == 300
    assert job["spec"]["ttlSecondsAfterFinished"] == 60
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    assert pod["securityContext"]["seccompProfile"] == {"type": "Unconfined"}
    body = pod["containers"][0]["args"][0]
    assert "type -P stat" in body
    assert "type -P gstat" in body
    assert "type -P pkill" in body
    assert "type -P killall" in body
    assert "missing required command" in body
    assert "cannot write TEST_DIRS root" in body
    assert "could not read back its PVC probe" in body
    assert "/mnt/storage-scale-test/alpha" in body
    assert "/mnt/storage-scale-test/benchmark" in body
    for phase in (
        "identity",
        "seccomp",
        "required-tools",
        "pvc-permissions",
        "pvc-create",
        "pvc-write",
        "pvc-read",
        "complete",
    ):
        assert f"STORAGE_SCALE_TEST_VALIDATION_PHASE={phase}" in body
    assert "required Unconfined seccomp profile" in body


def test_runtime_validation_captures_live_pod_before_controller_deadline() -> None:
    """The host diagnostic deadline precedes deletion by activeDeadlineSeconds."""
    functions = _FUNCTIONS.read_text(encoding="utf-8")
    body = functions.split("kubectl_validate_runtime_pod() {", 1)[1].split(
        "\n}\n\nkubectl_discover_candidate_nodes", 1
    )[0]
    assert "SECONDS + 180" in body
    assert "_kubectl_print_runtime_validation_diagnostics" in body
    template = (
        _ROOT / "storage-tests/fs/kubectl/templates/validation-job.yaml.tmpl"
    ).read_text(encoding="utf-8")
    assert "activeDeadlineSeconds: 300" in template


def test_runtime_validation_diagnostics_resolve_symlinked_tmpdir(
    tmp_path: Path,
) -> None:
    """Validation diagnostics use a physical path when TMPDIR is a symlink."""
    physical_temp = tmp_path / "physical-temp"
    physical_temp.mkdir()
    linked_temp = tmp_path / "linked-temp"
    linked_temp.symlink_to(physical_temp, target_is_directory=True)
    result = _bash(f"""
        export TMPDIR={str(linked_temp)!r}
        kubectl_capture_resource_diagnostics() {{
            local metadata_dir="$1" bundle
            case "$metadata_dir" in
                {str(physical_temp)!r}/*) ;;
                *) printf 'unexpected diagnostic path: %s\n' "$metadata_dir" >&2; return 1 ;;
            esac
            bundle="$metadata_dir/diagnostics/runtime-validation"
            mkdir -p -- "$bundle"
            printf 'captured validation evidence\n' > "$bundle/resource.describe"
            printf '%s\n' "$bundle"
        }}
        _kubectl_print_runtime_validation_diagnostics \
          test-ns sst-elb-1234abcd-validation 1234abcd
        """)
    assert result.returncode == 0, result.stderr
    assert "captured validation evidence" in result.stderr
    assert "may not traverse a symbolic link" not in result.stderr


def test_control_bundle_stages_phase_six_contract_once(tmp_path: Path) -> None:
    """The trusted helper, coordinator, and frozen run metadata stay together."""
    coordinator = tmp_path / "coordinator-source.sh"
    coordinator.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    result = _bash(_identity(tmp_path / "state") + f"""
        chmod() {{
            [[ " $* " != *' -- '* ]] || return 64
            command chmod "$@"
        }}
        STORAGE_SCALE_TEST_VERSION=v1.2.3-rc.1
        kubectl_prepare_control_bundle bundle "$root" "$fd" 1234abcd \\
          elbencho-20260922Z123456 {str(coordinator)!r}
        test -x "$bundle/coordinator.sh"
        test -f "$bundle/_nv-elbencho-kubectl-functions.sh"
        test -f "$bundle/bundle-manifest.tsv"
        grep -Eq '^[0-9a-f]{{64}}'$'\t''[^[:space:]]+$' "$bundle/bundle-manifest.tsv"
        ! grep -Fq '\\t' "$bundle/bundle-manifest.tsv"
        grep -Fx 'attempt_id\t1234abcd' "$bundle/run-metadata.tsv"
        grep -Fx 'output_basename\telbencho-20260922Z123456' "$bundle/run-metadata.tsv"
        grep -Fx 'project_version\tv1.2.3-rc.1' "$bundle/run-metadata.tsv"
        ! kubectl_prepare_control_bundle other "$root" "$fd" 1234abcd \\
          elbencho-20260922Z123456 {str(coordinator)!r}
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr


def test_sweep_bundle_stages_failure_overlay_only_for_first_attempt(
    tmp_path: Path,
) -> None:
    """The integration overlay is copied into control and omitted on resume."""
    bundle = tmp_path / "bundle"
    resume_bundle = tmp_path / "resume-bundle"
    results = tmp_path / "results"
    executions = results / "executions"
    bundle.mkdir()
    resume_bundle.mkdir()
    executions.mkdir(parents=True)
    overlay = tmp_path / "fail-once"
    overlay.write_text("#!/usr/bin/env bash\nexit 97\n", encoding="utf-8")
    overlay.chmod(0o700)
    (results / "env_used.sh").write_text(
        f"export KUBECTL_INTEGRATION_FAILURE_OVERLAY={overlay}\n",
        encoding="utf-8",
    )
    (results / "env_used.yaml").write_text("schema: test\n", encoding="utf-8")
    (executions / "0001.sh").write_text("export nodes=1\n", encoding="utf-8")
    endpoints = tmp_path / "endpoints.tsv"
    endpoints.write_text(
        "node-a\\tuid-a\\tpod-a\\tpoduid-a\\t10.0.0.1\\tamd64\\tsha256:one\\n",
        encoding="utf-8",
    )
    result = _bash(f"""
        kubectl_attempt_remote_root() {{ printf %s /mnt/storage-scale-test/benchmark/.storage-scale-test/runs/1234abcd; }}
        kubectl_populate_sweep_control_bundle {str(bundle)!r} {str(results)!r} \\
            {str(endpoints)!r}
        test -x {str(bundle / 'failure-overlay.sh')!r}
        grep -F '/mnt/storage-scale-test/benchmark/.storage-scale-test/runs/1234abcd/control/failure-overlay.sh' \\
            {str(bundle / 'env_used.sh')!r}
        printf 0001\\n > {str(tmp_path / 'selection')!r}
        kubectl_populate_sweep_control_bundle {str(resume_bundle)!r} {str(results)!r} \\
            {str(endpoints)!r} {str(tmp_path / 'selection')!r}
        test ! -e {str(resume_bundle / 'failure-overlay.sh')!r}
        grep -F 'unset KUBECTL_INTEGRATION_FAILURE_OVERLAY' {str(resume_bundle / 'env_used.sh')!r}
        """)
    assert result.returncode == 0, result.stderr


def test_resume_bundle_keeps_only_collected_non_success_cells(tmp_path: Path) -> None:
    """A collected resume never sends a previously successful cell back to a Job."""
    results = tmp_path / "elbencho-20260922Z123456"
    executions = results / "executions"
    executions.mkdir(parents=True)
    for name in ("env_used.sh", "env_used.yaml"):
        (results / name).write_text("# snapshot\n", encoding="utf-8")
    (executions / "0001.sh").write_text("export nodes=1\n", encoding="utf-8")
    (executions / "0002.sh").write_text("export nodes=2\n", encoding="utf-8")
    endpoints = tmp_path / "endpoints.tsv"
    endpoints.write_text(
        "node-a\tuid-a\tpod-a\tpoduid-a\t10.0.0.1\tamd64\tsha256:x\n",
        encoding="utf-8",
    )
    selection = tmp_path / "resume.tsv"
    selection.write_text("0002\n", encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    coordinator = _ROOT / "storage-tests/fs/kubectl/_nv-elbencho-kubectl-coordinator.sh"
    result = _bash(f"""
        cp {str(_FUNCTIONS)!r} {str(bundle / '_nv-elbencho-kubectl-functions.sh')!r}
        cp {str(coordinator)!r} {str(bundle / 'coordinator.sh')!r}
        chmod 700 {str(bundle / 'coordinator.sh')!r}
        printf 'attempt_id\\t1234abcd\\noutput_basename\\telbencho-20260922Z123456\\n' > {str(bundle / 'run-metadata.tsv')!r}
        kubectl_populate_sweep_control_bundle {str(bundle)!r} {str(results)!r} \\
          {str(endpoints)!r} {str(selection)!r}
        test ! -e {str(bundle / 'executions/0001.sh')!r}
        test -f {str(bundle / 'executions/0002.sh')!r}
        grep -F $'\\texecutions/0002.sh' {str(bundle / 'bundle-manifest.tsv')!r}
        ! grep -Fq 'executions/0001.sh' {str(bundle / 'bundle-manifest.tsv')!r}
        """)
    assert result.returncode == 0, result.stderr


def test_resource_journal_distinguishes_absence_from_corruption(tmp_path: Path) -> None:
    """Cleanup must not silently skip an existing but malformed ownership record."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        mkdir -p "$root/attempts/1234abcd/resources"
        printf 'not shell metadata\n' > "$root/attempts/1234abcd/resources/workers.sh"
        kubectl_run_bounded() { return 0; }
        ! kubectl_cleanup_journaled_resources "$root" "$fd" 1234abcd
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr


def test_generic_cleanup_recovers_dynamic_inspector_journals(tmp_path: Path) -> None:
    """Killed status and collection clients leave helpers generic cleanup removes."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd \\
          status-deadbeef Pod sst-elb-1234abcd-status-deadbeef test-ns \\
          pod-uid 0123456789abcdef0123456789abcdef
        deleted=0
        kubectl_delete_owned_object() { deleted=$((deleted + 1)); }
        kubectl_cleanup_journaled_resources "$root" "$fd" 1234abcd
        [[ "$deleted" -eq 1 ]]
        kubectl_attempt_step_done "$root" 1234abcd delete-status-deadbeef
        kubectl_cleanup_journaled_resources "$root" "$fd" 1234abcd
        [[ "$deleted" -eq 1 ]]
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr


def test_remote_reservation_release_is_journaled_in_retryable_steps(
    tmp_path: Path,
) -> None:
    """Run-tree removal is durably recorded as one retryable step."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_journal_remote_reservation "$root" "$fd" 1234abcd \\
          0123456789abcdef0123456789abcdef
        calls=0
        kubectl_pvc_exec() { calls=$((calls + 1)); return 0; }
        kubectl_release_journaled_remote_attempt "$root" "$fd" test-ns helper 1234abcd
        [[ "$calls" -eq 1 ]]
        kubectl_attempt_step_done "$root" 1234abcd release-remote-run
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr


def test_pvc_lease_name_is_stable_for_storage_identity() -> None:
    """One PV/PVC identity has one lock name regardless of workload paths."""
    result = _bash("""
        first=$(kubectl_pvc_lease_name namespace-uid pv-uid pvc-uid)
        second=$(kubectl_pvc_lease_name namespace-uid pv-uid pvc-uid)
        changed=$(kubectl_pvc_lease_name namespace-uid pv-uid other-pvc-uid)
        [[ "$first" == "$second" && "$first" != "$changed" ]]
        [[ "$first" =~ ^sst-elb-pvc-[0-9a-f]{32}$ ]]
    """)
    assert result.returncode == 0, result.stderr


def test_pvc_lease_is_create_only_and_has_no_time_expiry() -> None:
    """[S-05] The durable PVC lock is an exact owned, non-expiring Lease."""
    result = _bash("""
        KUBECTL_NAMESPACE_UID=namespace-uid
        KUBECTL_PV_UID=pv-uid
        KUBECTL_PVC_UID=pvc-uid
        manifest=$(kubectl_render_pvc_lease sst-elb-pvc-0123456789abcdef0123456789abcdef \
          test-ns 1234abcd 0123456789abcdef0123456789abcdef)
        grep -F 'kind: Lease' <<< "$manifest"
        grep -F 'holderIdentity: "1234abcd/0123456789abcdef0123456789abcdef"' \
          <<< "$manifest"
        ! grep -Fq leaseDurationSeconds <<< "$manifest"
        source=$(cat storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh)
        body=${source#*kubectl_acquire_pvc_lease() \\{}
        body=${body%%$'\n}\n\nkubectl_release_pvc_lease'*}
        grep -F 'create -f -' <<< "$body"
        ! grep -Eq 'apply|replace|patch' <<< "$body"
    """)
    assert result.returncode == 0, result.stderr


def test_pvc_lease_observation_populates_caller_variable_named_observed() -> None:
    """Lease observation survives Bash dynamic scoping at real call sites."""
    result = _bash("""
        kubectl_run_observational() {
          printf '%s' $'lease-uid\tnonce\t1234abcd\tnamespace-uid\tpv-uid\tpvc-uid\t1234abcd/nonce'
        }
        observed=
        kubectl_observe_pvc_lease observed test-ns \
          sst-elb-pvc-0123456789abcdef0123456789abcdef
        [[ "$observed" == $'lease-uid\tnonce\t1234abcd\tnamespace-uid\tpv-uid\tpvc-uid\t1234abcd/nonce' ]]
    """)
    assert result.returncode == 0, result.stderr


def test_lifecycle_commands_explain_missing_attempt_identity(tmp_path: Path) -> None:
    """A pre-identity submission failure has an explicit, actionable error."""
    results = tmp_path / "results"
    results.mkdir()
    result = _bash(f"""
        ! kubectl_lifecycle_operation status {str(results)!r}
        ! kubectl_resume_collected_sweep {str(results)!r}
    """)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("has no valid current Kubernetes attempt") == 2
    assert "cannot be inspected, collected, or cancelled" in result.stderr
    assert "cannot be resumed" in result.stderr


def test_pvc_lease_release_uses_uid_precondition_and_is_journaled(
    tmp_path: Path,
) -> None:
    """Lock release deletes only its exact UID and commits an idempotent marker."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd pvc-lease \
          Lease sst-elb-pvc-0123456789abcdef0123456789abcdef test-ns lease-uid \
          0123456789abcdef0123456789abcdef
        observations=0
        kubectl_observe_pvc_lease() {
          observations=$((observations + 1))
          if [[ "$observations" -eq 1 ]]; then
            printf -v "$1" '%s' $'lease-uid\t0123456789abcdef0123456789abcdef\t1234abcd\tnamespace-uid\tpv-uid\tpvc-uid\t1234abcd/0123456789abcdef0123456789abcdef'
          else
            printf -v "$1" ''
          fi
        }
        captured=$(mktemp)
        kubectl_run_bounded() { cat > "$captured"; }
        kubectl_release_pvc_lease "$root" "$fd" 1234abcd
        grep -F '"uid":"lease-uid"' "$captured"
        kubectl_attempt_step_done "$root" 1234abcd release-pvc-lease
        kubectl_local_lock_release "$fd"
    """)
    assert result.returncode == 0, result.stderr


def test_pvc_lease_release_accepts_successor_after_exact_uid_delete(
    tmp_path: Path,
) -> None:
    """A successor may safely reacquire the deterministic name after deletion."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd pvc-lease \
          Lease "$KUBECTL_PVC_LEASE_NAME" test-ns lease-uid \
          0123456789abcdef0123456789abcdef
        observations=0
        kubectl_observe_pvc_lease() {
          observations=$((observations + 1))
          if [[ "$observations" -eq 1 ]]; then
            printf -v "$1" '%s' $'lease-uid\t0123456789abcdef0123456789abcdef\t1234abcd\tnamespace-uid\tpv-uid\tpvc-uid\t1234abcd/0123456789abcdef0123456789abcdef'
          else
            printf -v "$1" '%s' $'successor-uid\tsuccessor-nonce\tsuccessor\tnamespace-uid\tpv-uid\tpvc-uid\tsuccessor/successor-nonce'
          fi
        }
        kubectl_run_bounded() { cat >/dev/null; }
        kubectl_release_pvc_lease "$root" "$fd" 1234abcd
        kubectl_attempt_step_done "$root" 1234abcd release-pvc-lease
        kubectl_local_lock_release "$fd"
    """)
    assert result.returncode == 0, result.stderr


def test_interrupted_pvc_lease_creation_is_reconciled_by_exact_identity(
    tmp_path: Path,
) -> None:
    """[S-06] An accepted Lease is recovered from its pre-create intent."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_creation_intent "$root" "$fd" 1234abcd pvc-lease \
          Lease "$KUBECTL_PVC_LEASE_NAME" test-ns \
          0123456789abcdef0123456789abcdef
        kubectl_run_observational() {
          if [[ "$*" == *'jsonpath={.metadata.uid}' ]]; then
            printf lease-uid
          else
            printf '%s' $'Lease\t'$KUBECTL_PVC_LEASE_NAME$'\tlease-uid\t0123456789abcdef0123456789abcdef\t1234abcd'
          fi
        }
        kubectl_delete_owned_object() { :; }
        kubectl_release_pvc_lease "$root" "$fd" 1234abcd
        kubectl_attempt_step_done "$root" 1234abcd release-pvc-lease
        test ! -e "$root/attempts/1234abcd/creation-intents/pvc-lease.sh"
        kubectl_local_lock_release "$fd"
    """)
    assert result.returncode == 0, result.stderr


def test_delayed_pvc_lease_creation_uses_common_ambiguity_wait(
    tmp_path: Path,
) -> None:
    """[S-07] [S-08] Rollback rechecks a temporarily invisible Lease."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_creation_intent "$root" "$fd" 1234abcd pvc-lease \
          Lease "$KUBECTL_PVC_LEASE_NAME" test-ns \
          0123456789abcdef0123456789abcdef
        calls=$(mktemp)
        kubectl_run_observational() {
          printf x >> "$calls"
          case $(wc -c < "$calls") in
            1) return 0 ;;
            2) printf lease-uid ;;
            *) printf '%s' $'Lease\t'$KUBECTL_PVC_LEASE_NAME$'\tlease-uid\t0123456789abcdef0123456789abcdef\t1234abcd' ;;
          esac
        }
        sleep() { :; }
        stat() { printf '1\n'; }
        date() { printf '1000\n'; }
        kubectl_delete_owned_object() { printf deleted > "$root/deleted"; }
        kubectl_release_pvc_lease "$root" "$fd" 1234abcd
        [[ -f "$root/deleted" ]]
        [[ $(wc -c < "$calls") -eq 3 ]]
        [[ ! -e "$root/attempts/1234abcd/creation-intents/pvc-lease.sh" ]]
        kubectl_attempt_step_done "$root" 1234abcd release-pvc-lease
        kubectl_local_lock_release "$fd"
    """)
    assert result.returncode == 0, result.stderr


def test_pvc_lease_release_clears_redundant_matching_creation_intent(
    tmp_path: Path,
) -> None:
    """A crash after UID publication leaves no stale Lease intent."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_creation_intent "$root" "$fd" 1234abcd pvc-lease \
          Lease "$KUBECTL_PVC_LEASE_NAME" test-ns \
          0123456789abcdef0123456789abcdef
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd pvc-lease \
          Lease "$KUBECTL_PVC_LEASE_NAME" test-ns lease-uid \
          0123456789abcdef0123456789abcdef
        observations=0
        kubectl_observe_pvc_lease() {
          observations=$((observations + 1))
          if [[ "$observations" -eq 1 ]]; then
            printf -v "$1" '%s' $'lease-uid\t0123456789abcdef0123456789abcdef\t1234abcd\tnamespace-uid\tpv-uid\tpvc-uid\t1234abcd/0123456789abcdef0123456789abcdef'
          else
            printf -v "$1" ''
          fi
        }
        kubectl_run_bounded() { cat >/dev/null; }
        kubectl_release_pvc_lease "$root" "$fd" 1234abcd
        test ! -e "$root/attempts/1234abcd/creation-intents/pvc-lease.sh"
        kubectl_attempt_step_done "$root" 1234abcd release-pvc-lease
        kubectl_local_lock_release "$fd"
    """)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("successor_lease", (False, True))
def test_collection_recovers_lease_delete_before_release_journal(
    tmp_path: Path, successor_lease: bool
) -> None:
    """Exact cleanup evidence closes deletion with no or a successor Lease."""
    results = tmp_path / "results"
    result = _bash(_identity(results / "kubernetes") + f"""
        successor_lease={int(successor_lease)}
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_transition "$root" "$fd" 1234abcd TERMINAL
        kubectl_attempt_transition "$root" "$fd" 1234abcd COLLECTION_IN_PROGRESS
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd pvc-lease \
          Lease "$KUBECTL_PVC_LEASE_NAME" test-ns lease-uid \
          0123456789abcdef0123456789abcdef
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd sweep \
          Job sweep-job test-ns sweep-uid \
          0123456789abcdef0123456789abcdef
        kubectl_attempt_journal_step "$root" "$fd" 1234abcd release-remote-run
        kubectl_attempt_journal_step "$root" "$fd" 1234abcd delete-sweep
        mkdir -p "$root/attempts/1234abcd/collected-state" \
          "$root/attempts/1234abcd/control-bundle/executions"
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        kubectl_local_lock_release "$fd"

        _kubectl_load_saved_attempt() {{
          kubectl_attempt_load_metadata "$1/kubernetes/attempts/1234abcd"
          kubectl_attempt_load_identity "$1/kubernetes/attempts/1234abcd"
          printf -v "$2" 1234abcd
        }}
        kubectl_cleanup_ephemeral_helpers() {{ :; }}
        _kubectl_verify_saved_cluster_identity() {{ :; }}
        _kubectl_validate_collected_publication() {{ printf -v "$3" SUCCESS; }}
        kubectl_observe_pvc_lease() {{
          if (( successor_lease )); then
            printf -v "$1" '%s' $'successor-uid\tsuccessor-nonce\tsuccessor\tnamespace-uid\tpv-uid\tpvc-uid\tsuccessor/successor-nonce'
          else
            printf -v "$1" ''
          fi
        }}
        _kubectl_create_inspector() {{
          echo 'unexpected inspector creation' >&2
          return 1
        }}
        output=$(kubectl_lifecycle_operation collect {str(results)!r})
        [[ "$output" == *STATE=SUCCESS* ]]
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_LIFECYCLE_STATE" == COLLECTED ]]
        kubectl_attempt_step_done "$root" 1234abcd release-pvc-lease
    """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=" not in result.stderr


def test_collection_does_not_reconcile_lease_before_exact_cleanup(
    tmp_path: Path,
) -> None:
    """An absent Lease is not enough without all preceding cleanup evidence."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_transition "$root" "$fd" 1234abcd TERMINAL
        kubectl_attempt_transition "$root" "$fd" 1234abcd COLLECTION_IN_PROGRESS
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd pvc-lease \
          Lease "$KUBECTL_PVC_LEASE_NAME" test-ns lease-uid \
          0123456789abcdef0123456789abcdef
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd sweep \
          Job sweep-job test-ns sweep-uid \
          0123456789abcdef0123456789abcdef
        kubectl_attempt_journal_step "$root" "$fd" 1234abcd release-remote-run
        mkdir -p "$root/attempts/1234abcd/collected-state" \
          "$root/attempts/1234abcd/control-bundle/executions"
        _kubectl_validate_collected_publication() { printf -v "$3" SUCCESS; }
        kubectl_observe_pvc_lease() { printf -v "$1" ''; }
        ! kubectl_reconcile_pvc_lease_release_after_collection_cleanup \
            "$root" "$fd" 1234abcd
        ! kubectl_attempt_step_done "$root" 1234abcd release-pvc-lease
        kubectl_local_lock_release "$fd"
    """)
    assert result.returncode == 0, result.stderr


def test_remote_release_executes_guards_before_owner_read_and_deletion(
    tmp_path: Path,
) -> None:
    """Release rejects symlinked owners while allowing an owned tree to clean up."""
    mount = tmp_path / "mount"
    result = _bash(f"""
        mount={str(mount)!r}
        test_root="$mount/benchmark"
        control_root="$test_root/.storage-scale-test"
        KUBECTL_CONTROL_TEST_ROOT=/mnt/storage-scale-test/benchmark
        KUBECTL_CONTROL_ROOT="$KUBECTL_CONTROL_TEST_ROOT/.storage-scale-test"
        run="$control_root/runs/1234abcd"
        nonce=0123456789abcdef0123456789abcdef
        mkdir -p "$run"
        printf '1234abcd\\t%s\\n' "$nonce" > "$run/owner"
        kubectl_pvc_exec() {{
            script="$5"
            script=$(sed "s#/mnt/storage-scale-test#$mount#g" <<< "$script")
            run_arg=$(sed "s#/mnt/storage-scale-test#$mount#g" <<< "$7")
            root_arg=$(sed "s#/mnt/storage-scale-test#$mount#g" <<< "${{10}}")
            test_arg=$(sed "s#/mnt/storage-scale-test#$mount#g" <<< "${{11}}")
            bash -ceu "$script" bash "$run_arg" "$8" "$9" \
                "$root_arg" "$test_arg"
        }}
        find() {{
            local argument
            for argument; do
                [[ "$argument" != -printf ]] || return 2
            done
            command find "$@"
        }}
        kubectl_release_remote_attempt test-ns helper 1234abcd "$nonce"
        [[ ! -e "$run" ]]

        mkdir -p "$control_root/runs"
        pending="$control_root/runs/.1234abcd.pending.$nonce"
        mkdir "$pending"
        kubectl_release_remote_attempt test-ns helper 1234abcd "$nonce"
        [[ ! -e "$pending" ]]

        mkdir "$pending"
        printf 'unexpected\n' > "$pending/not-owned"
        ! kubectl_release_remote_attempt test-ns helper 1234abcd "$nonce"
        [[ -f "$pending/not-owned" ]]
        rm -rf -- "$pending"

        mkdir -p "$run" "$mount/outside"
        ln -s "$mount/outside/owner" "$run/owner"
        ! kubectl_release_remote_attempt test-ns helper 1234abcd "$nonce"
        [[ -L "$run/owner" && -d "$run" ]]
        """)
    assert result.returncode == 0, result.stderr


def test_intended_reservation_does_not_touch_competing_pvc_owner(
    tmp_path: Path,
) -> None:
    """[S-05] A losing create-only Lease acquisition leaves its winner intact."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_run_bounded() { printf AlreadyExists >&2; return 1; }
        kubectl_run_observational() {
            printf '%s\t%s\t%s\t%s\t%s\t%s\t%s' lease-foreign \
              11111111111111111111111111111111 deadbeef \
              namespace-uid pv-uid pvc-uid \
              deadbeef/11111111111111111111111111111111
        }
        ! kubectl_acquire_pvc_lease "$root" "$fd" 1234abcd
        test ! -e "$root/attempts/1234abcd/creation-intents/pvc-lease.sh"
        test ! -e "$root/attempts/1234abcd/resources/pvc-lease.sh"
        kubectl_local_lock_release "$fd"
    """)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("failed_stage", ("release", "cleanup", "remove", "lease"))
def test_collection_recovery_retries_every_cleanup_stage(
    tmp_path: Path, failed_stage: str
) -> None:
    """[R-12] [R-13] Durable import survives each cleanup interruption."""
    results = tmp_path / "results"
    metadata = results / "kubernetes" / "attempts" / "1234abcd"
    (metadata / "collected-state").mkdir(parents=True)
    result = _bash(f"""
        export KUBECTL_NAMESPACE=test-ns
        events={str(tmp_path / 'events')!r}
        failed={failed_stage!r}
        failed_once=0
        kubectl_attempt_load_metadata() {{ KUBECTL_LIFECYCLE_STATE=COLLECTION_IN_PROGRESS; }}
        _kubectl_validate_collected_publication() {{ printf -v "$3" SUCCESS; }}
        _kubectl_create_inspector() {{
            printf -v "$1" collector-pod
            printf -v "$2" collector-uid
            printf create- >> "$events"
        }}
        fail_once() {{
            if [[ "$failed" == "$1" && "$failed_once" -eq 0 ]]; then
                failed_once=1
                return 1
            fi
        }}
        kubectl_release_journaled_remote_attempt() {{
            printf release- >> "$events"
            fail_once release
        }}
        kubectl_cleanup_journaled_resources() {{
            printf cleanup- >> "$events"
            fail_once cleanup
        }}
        _kubectl_remove_inspector() {{
            printf remove- >> "$events"
            fail_once remove
        }}
        kubectl_release_pvc_lease() {{
            printf lease- >> "$events"
            fail_once lease
        }}
        kubectl_attempt_transition() {{ printf transition- >> "$events"; }}
        kubectl_emit_state() {{ printf '%s\\n' "$1"; }}
        ! kubectl_collect_attempt {str(results)!r} {str(results / 'kubernetes')!r} \
            9 1234abcd
        kubectl_collect_attempt {str(results)!r} {str(results / 'kubernetes')!r} \
            9 1234abcd
        grep -F lease-transition- "$events"
        """)
    assert result.returncode == 0, result.stderr


def test_fake_pre_job_lifecycle_orders_identity_reservation_and_workers(
    tmp_path: Path,
) -> None:
    """The pre-Job dispatcher is a testable sequence, not disconnected helpers."""
    result = _bash(f"""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        declare -A mapped=([/mnt/storage-scale-test/bench]=1)
        events={str(tmp_path / 'events')!r}
        kubectl_validate_cluster_identity() {{ printf 'namespace-uid\\tpv-uid\\tpvc-uid\\n'; }}
        kubectl_discover_candidate_nodes() {{
            test -s {str(tmp_path)!r}/kubernetes/current-attempt
            printf current >> "$events"
            printf 'node-a\\tuid-a\\tamd64\\nnode-b\\tuid-b\\tamd64\\n' > "$2"
            printf nodes >> "$events"
        }}
        kubectl_choose_coordinator_node() {{ printf 'node-a\\n'; }}
        kubectl_create_helper_pod() {{ printf -v "$1" uid-transfer; printf helper >> "$events"; }}
        kubectl_validate_pvc_paths() {{ printf paths >> "$events"; }}
        kubectl_validate_test_root_access() {{ printf access >> "$events"; }}
        kubectl_acquire_pvc_lease() {{ printf lease >> "$events"; }}
        kubectl_attempt_journal_remote_reservation() {{ printf journal >> "$events"; }}
        kubectl_reserve_remote_attempt() {{ printf reserve >> "$events"; }}
        kubectl_attempt_mark_remote_reservation_acquired() {{ printf acquired >> "$events"; }}
        kubectl_initialize_remote_control_tree() {{ printf initialize >> "$events"; }}
        kubectl_create_attempt_policies() {{ printf policy >> "$events"; }}
        kubectl_create_worker_daemonset() {{ printf workers >> "$events"; }}
        kubectl_wait_worker_endpoints() {{ printf endpoints >> "$events"; : > "$4"; }}
        attempt=
        kubectl_prepare_attempt_lifecycle attempt {str(tmp_path)!r} 2 mapped \
          '' benchmark "$KUBECTL_CONTROL_TEST_ROOT"
        [[ "$attempt" =~ ^[0-9a-f]{{8}}$ ]]
        test -f {str(tmp_path)!r}/kubernetes/attempts/"$attempt"/identity.sh
        test -f {str(tmp_path)!r}/kubernetes/attempts/"$attempt"/configuration.sh
        [[ $(cat "$events") == currentnodeshelperpathsaccessleasejournalreserveacquiredinitializepolicyworkersendpoints ]]
        """)
    assert result.returncode == 0, result.stderr


def test_prepare_failure_terminalizes_only_after_successful_rollback(
    tmp_path: Path,
) -> None:
    """[S-03] [S-14] Failed handoff terminalizes after exact cleanup."""
    result = _bash(f"""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        declare -A mapped=([/mnt/storage-scale-test/bench]=1)
        events={str(tmp_path / 'events')!r}
        kubectl_validate_cluster_identity() {{ printf 'namespace-uid\\tpv-uid\\tpvc-uid\\n'; }}
        kubectl_discover_candidate_nodes() {{ printf 'node-a\\tuid-a\\tamd64\\n' > "$2"; }}
        kubectl_choose_coordinator_node() {{ printf node-a; }}
        kubectl_create_helper_pod() {{ printf -v "$1" uid-transfer; }}
        kubectl_validate_pvc_paths() {{ :; }}
        kubectl_validate_test_root_access() {{ :; }}
        kubectl_acquire_pvc_lease() {{ :; }}
        kubectl_attempt_journal_remote_reservation() {{ : > "$1/attempts/$3/remote-reservation.sh"; }}
        kubectl_reserve_remote_attempt() {{ :; }}
        kubectl_attempt_mark_remote_reservation_acquired() {{ :; }}
        kubectl_initialize_remote_control_tree() {{ :; }}
        kubectl_create_attempt_policies() {{ printf policies >> "$events"; return 1; }}
        kubectl_release_journaled_remote_attempt() {{ printf release >> "$events"; }}
        kubectl_cleanup_journaled_resources() {{ printf cleanup >> "$events"; }}
        kubectl_release_pvc_lease() {{ printf lease-release >> "$events"; }}
        attempt=
        ! kubectl_prepare_attempt_lifecycle attempt {str(tmp_path)!r} 1 mapped \
          '' benchmark "$KUBECTL_CONTROL_TEST_ROOT"
        attempt=$(cat {str(tmp_path)!r}/kubernetes/current-attempt)
        kubectl_attempt_load_metadata {str(tmp_path)!r}/kubernetes/attempts/"$attempt"
        [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]
        [[ $(cat "$events") == policiesreleasecleanuplease-release ]]
    """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_OPERATION=prepare" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_PHASE=network-policy" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=API_UNAVAILABLE" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_MAY_STILL_BE_RUNNING=no" in result.stderr
    # The report reflects the durable state just written, not the stale
    # PREPARED value loaded before the rollback transition.
    assert (
        "STORAGE_SCALE_TEST_DIAGNOSTIC_LOCAL_STATE=SUBMISSION_FAILED" in result.stderr
    )


def test_prepare_rollback_failure_keeps_prepared_attempt_recoverable(
    tmp_path: Path,
) -> None:
    """A failed release leaves the durable PREPARED pointer for retry."""
    result = _bash(f"""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        declare -A mapped=([/mnt/storage-scale-test/bench]=1)
        events={str(tmp_path / 'events')!r}
        kubectl_validate_cluster_identity() {{ printf 'namespace-uid\\tpv-uid\\tpvc-uid\\n'; }}
        kubectl_discover_candidate_nodes() {{ printf 'node-a\\tuid-a\\tamd64\\n' > "$2"; }}
        kubectl_choose_coordinator_node() {{ printf node-a; }}
        kubectl_create_helper_pod() {{ printf -v "$1" uid-transfer; }}
        kubectl_validate_pvc_paths() {{ :; }}
        kubectl_validate_test_root_access() {{ :; }}
        kubectl_acquire_pvc_lease() {{ :; }}
        kubectl_attempt_journal_remote_reservation() {{ : > "$1/attempts/$3/remote-reservation.sh"; }}
        kubectl_reserve_remote_attempt() {{ :; }}
        kubectl_attempt_mark_remote_reservation_acquired() {{ :; }}
        kubectl_initialize_remote_control_tree() {{ :; }}
        kubectl_create_attempt_policies() {{ printf policies >> "$events"; return 1; }}
        kubectl_release_journaled_remote_attempt() {{ printf release >> "$events"; return 1; }}
        kubectl_cleanup_journaled_resources() {{ printf cleanup >> "$events"; }}
        attempt=
        ! kubectl_prepare_attempt_lifecycle attempt {str(tmp_path)!r} 1 mapped \
          '' benchmark "$KUBECTL_CONTROL_TEST_ROOT"
        attempt=$(cat {str(tmp_path)!r}/kubernetes/current-attempt)
        kubectl_attempt_load_metadata {str(tmp_path)!r}/kubernetes/attempts/"$attempt"
        [[ "$KUBECTL_LIFECYCLE_STATE" == PREPARED ]]
        [[ $(cat "$events") == policiesreleasecleanup ]]
        """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_LOCAL_STATE=PREPARED" in result.stderr


def test_submission_failure_terminalizes_only_after_successful_rollback(
    tmp_path: Path,
) -> None:
    """Submit rollback records SUBMISSION_FAILED only after cleanup succeeds."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        kubectl_local_lock_release "$fd"
        events={str(tmp_path / 'events')!r}
        declare -A TEST_DIRS=([benchmark]=1)
        kubectl_validate_runtime_configuration() {{ :; }}
        kubectl_map_test_dirs() {{ :; }}
        kubectl_prepare_attempt_lifecycle() {{ printf -v "$1" 1234abcd; }}
        kubectl_prepare_control_bundle() {{ return 1; }}
        kubectl_cleanup_journaled_resources() {{ printf cleanup >> "$events"; }}
        kubectl_release_pvc_lease() {{ printf lease >> "$events"; }}
        ! kubectl_submit_sweep {str(results)!r} 1
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]
        [[ $(cat "$events") == cleanuplease ]]
        """)
    assert result.returncode == 0, result.stderr


def test_submission_rollback_failure_keeps_prepared_attempt_recoverable(
    tmp_path: Path,
) -> None:
    """A failed submit cleanup leaves current-attempt recoverable as PREPARED."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        kubectl_local_lock_release "$fd"
        events={str(tmp_path / 'events')!r}
        declare -A TEST_DIRS=([benchmark]=1)
        kubectl_validate_runtime_configuration() {{ :; }}
        kubectl_map_test_dirs() {{ :; }}
        kubectl_prepare_attempt_lifecycle() {{ printf -v "$1" 1234abcd; }}
        kubectl_prepare_control_bundle() {{ return 1; }}
        kubectl_cleanup_journaled_resources() {{ printf cleanup >> "$events"; return 1; }}
        ! kubectl_submit_sweep {str(results)!r} 1
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_LIFECYCLE_STATE" == PREPARED ]]
        [[ $(cat "$events") == cleanup ]]
        [[ $(kubectl_attempt_current_id "$root") == 1234abcd ]]
        """)
    assert result.returncode == 0, result.stderr


def test_interrupted_bundle_upload_never_starts_job_and_rolls_back(
    tmp_path: Path,
) -> None:
    """[S-13] Partial control transfer cannot start a coordinator."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        : > "$root/attempts/1234abcd/configuration.sh"
        : > "$root/attempts/1234abcd/remote-reservation.sh"
        kubectl_local_lock_release "$fd"
        events={str(tmp_path / 'events')!r}
        declare -A TEST_DIRS=([benchmark]=1)
        kubectl_validate_runtime_configuration() {{ :; }}
        kubectl_map_test_dirs() {{ :; }}
        kubectl_prepare_attempt_lifecycle() {{ printf -v "$1" 1234abcd; }}
        kubectl_prepare_control_bundle() {{ printf -v "$1" {str(bundle)!r}; }}
        kubectl_populate_sweep_control_bundle() {{ :; }}
        kubectl_attempt_load_resource() {{
            KUBECTL_RESOURCE_KIND=Pod
            KUBECTL_RESOURCE_NAME=transfer
            KUBECTL_RESOURCE_NAMESPACE=test-ns
            KUBECTL_RESOURCE_NONCE=0123456789abcdef0123456789abcdef
            KUBECTL_RESOURCE_UID=transfer-uid
        }}
        kubectl_upload_control_bundle() {{ printf upload- >> "$events"; return 1; }}
        kubectl_create_owned_object() {{ printf job- >> "$events"; return 1; }}
        kubectl_quiesce_prepared_workloads() {{ printf quiesce- >> "$events"; }}
        kubectl_preserve_attempt_diagnostics() {{ printf diagnostics- >> "$events"; }}
        _kubectl_create_inspector() {{
            printf -v "$1" helper
            printf -v "$2" helper-uid
        }}
        kubectl_release_journaled_remote_attempt() {{ printf release- >> "$events"; }}
        _kubectl_remove_inspector() {{ :; }}
        kubectl_cleanup_journaled_resources() {{ printf cleanup- >> "$events"; }}
        kubectl_release_pvc_lease() {{ printf lease- >> "$events"; }}
        ! kubectl_submit_sweep {str(results)!r} 1
        [[ $(cat "$events") == upload-quiesce-diagnostics-release-cleanup-lease- ]]
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]
    """)
    assert result.returncode == 0, result.stderr


def test_prepared_recovery_quiesces_workloads_before_remote_release(
    tmp_path: Path,
) -> None:
    """A pre-handoff Job cannot race removal of its PVC control tree."""
    result = _bash(_identity(tmp_path / "state") + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd sweep \
          Job sweep test-ns job-uid 0123456789abcdef0123456789abcdef
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd workers \
          DaemonSet workers test-ns workers-uid 0123456789abcdef0123456789abcdef
        kubectl_attempt_journal_remote_reservation "$root" "$fd" 1234abcd \
          0123456789abcdef0123456789abcdef
        events={str(tmp_path / 'events')!r}
        kubectl_delete_owned_object() {{ printf '%s-' "$2" >> "$events"; }}
        _kubectl_create_inspector() {{
          printf helper- >> "$events"
          printf -v "$1" helper
          printf -v "$2" helper-uid
        }}
        kubectl_release_journaled_remote_attempt() {{ printf release- >> "$events"; }}
        _kubectl_remove_inspector() {{ printf remove- >> "$events"; }}
        kubectl_cleanup_journaled_resources() {{ printf cleanup- >> "$events"; }}
        kubectl_release_pvc_lease() {{ printf lease- >> "$events"; }}
        kubectl_attempt_restore_predecessor() {{ :; }}
        kubectl_recover_prepared_attempt "$root" "$fd" 1234abcd
        [[ $(cat "$events") == sweep-workers-helper-release-remove-cleanup- ]]
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr


def test_prepared_recovery_retains_remote_state_until_job_is_quiescent(
    tmp_path: Path,
) -> None:
    """A failed exact Job delete leaves the PVC run and lock recoverable."""
    result = _bash(_identity(tmp_path / "state") + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd sweep \
          Job sweep test-ns job-uid 0123456789abcdef0123456789abcdef
        kubectl_attempt_journal_remote_reservation "$root" "$fd" 1234abcd \
          0123456789abcdef0123456789abcdef
        events={str(tmp_path / 'events')!r}
        kubectl_delete_owned_object() {{ printf delete- >> "$events"; return 1; }}
        kubectl_release_journaled_remote_attempt() {{ printf release- >> "$events"; }}
        kubectl_cleanup_journaled_resources() {{ printf cleanup- >> "$events"; }}
        ! kubectl_recover_prepared_attempt "$root" "$fd" 1234abcd
        [[ $(cat "$events") == delete-cleanup- ]]
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_LIFECYCLE_STATE" == PREPARED ]]
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr


def test_clean_failed_resume_restores_collected_predecessor_pointer(
    tmp_path: Path,
) -> None:
    """[R-18] Failed successor rollback restores the collected predecessor."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_transition "$root" "$fd" 1234abcd TERMINAL
        kubectl_attempt_transition "$root" "$fd" 1234abcd COLLECTION_IN_PROGRESS
        kubectl_attempt_transition "$root" "$fd" 1234abcd COLLECTED
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        kubectl_local_lock_release "$fd"
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        declare -A mapped=([/mnt/storage-scale-test/bench]=1)
        kubectl_generate_attempt_id() {{ printf aaaabbbb; }}
        kubectl_generate_ownership_nonce() {{ printf 11111111111111111111111111111111; }}
        kubectl_validate_cluster_identity() {{ printf 'namespace-uid\\tpv-uid\\tpvc-uid\\n'; }}
        kubectl_discover_candidate_nodes() {{ printf 'node-a\\tuid-a\\tamd64\\n' > "$2"; }}
        kubectl_choose_coordinator_node() {{ printf node-a; }}
        kubectl_create_helper_pod() {{ printf -v "$1" uid-transfer; }}
        kubectl_validate_pvc_paths() {{ :; }}
        kubectl_attempt_journal_remote_reservation() {{ :; }}
        kubectl_reserve_remote_attempt() {{ :; }}
        kubectl_attempt_mark_remote_reservation_acquired() {{ :; }}
        kubectl_initialize_remote_control_tree() {{ :; }}
        kubectl_create_attempt_policies() {{ return 1; }}
        kubectl_release_journaled_remote_attempt() {{ :; }}
        kubectl_cleanup_journaled_resources() {{ :; }}
        kubectl_release_pvc_lease() {{ :; }}
        first=
        ! kubectl_prepare_attempt_lifecycle first {str(results)!r} 1 mapped \
          '' benchmark "$KUBECTL_CONTROL_TEST_ROOT" 1234abcd
        [[ $(kubectl_attempt_current_id "$root") == 1234abcd ]]
        kubectl_attempt_load_metadata "$root/attempts/aaaabbbb"
        [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]
        """)
    assert result.returncode == 0, result.stderr


def test_deferred_prepared_recovery_restores_collected_predecessor_pointer(
    tmp_path: Path,
) -> None:
    """Later PREPARED recovery also restores A after B rollback eventually succeeds."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_transition "$root" "$fd" 1234abcd TERMINAL
        kubectl_attempt_transition "$root" "$fd" 1234abcd COLLECTION_IN_PROGRESS
        kubectl_attempt_transition "$root" "$fd" 1234abcd COLLECTED
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        kubectl_local_lock_release "$fd"
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        declare -A mapped=([/mnt/storage-scale-test/bench]=1)
        kubectl_generate_attempt_id() {{ printf aaaabbbb; }}
        kubectl_generate_ownership_nonce() {{ printf 11111111111111111111111111111111; }}
        kubectl_validate_cluster_identity() {{ printf 'namespace-uid\\tpv-uid\\tpvc-uid\\n'; }}
        kubectl_discover_candidate_nodes() {{ printf 'node-a\\tuid-a\\tamd64\\n' > "$2"; }}
        kubectl_choose_coordinator_node() {{ printf node-a; }}
        kubectl_create_helper_pod() {{ printf -v "$1" uid-transfer; }}
        kubectl_validate_pvc_paths() {{ :; }}
        kubectl_validate_test_root_access() {{ :; }}
        kubectl_acquire_pvc_lease() {{ :; }}
        kubectl_attempt_journal_remote_reservation() {{ :; }}
        kubectl_reserve_remote_attempt() {{ :; }}
        kubectl_attempt_mark_remote_reservation_acquired() {{ :; }}
        kubectl_initialize_remote_control_tree() {{ :; }}
        kubectl_create_attempt_policies() {{ return 1; }}
        kubectl_release_journaled_remote_attempt() {{ return 1; }}
        kubectl_cleanup_journaled_resources() {{ :; }}
        kubectl_release_pvc_lease() {{ :; }}
        first=
        ! kubectl_prepare_attempt_lifecycle first {str(results)!r} 1 mapped \
          '' benchmark "$KUBECTL_CONTROL_TEST_ROOT" 1234abcd
        [[ $(kubectl_attempt_current_id "$root") == aaaabbbb ]]
        kubectl_local_lock_acquire "$root" fd2
        kubectl_recover_prepared_attempt "$root" "$fd2" aaaabbbb
        [[ $(kubectl_attempt_current_id "$root") == 1234abcd ]]
        kubectl_attempt_load_metadata "$root/attempts/aaaabbbb"
        [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]
        kubectl_local_lock_release "$fd2"
        """)
    assert result.returncode == 0, result.stderr


def test_submission_failed_status_retries_predecessor_restore(tmp_path: Path) -> None:
    """A terminal failed resume still repairs current-attempt on status."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_transition "$root" "$fd" 1234abcd TERMINAL
        kubectl_attempt_transition "$root" "$fd" 1234abcd COLLECTION_IN_PROGRESS
        kubectl_attempt_transition "$root" "$fd" 1234abcd COLLECTED
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        kubectl_local_lock_release "$fd"
        kubectl_local_lock_acquire "$root" fd2
        kubectl_attempt_create_identity "$root" "$fd2" aaaabbbb \\
          11111111111111111111111111111111 test-ns namespace-uid \\
          test-pv pv-uid test-pvc pvc-uid
        kubectl_attempt_write_state "$root" "$fd2" aaaabbbb PREPARED
        kubectl_attempt_transition "$root" "$fd2" aaaabbbb SUBMISSION_FAILED
        printf '1234abcd\\n' > "$root/attempts/aaaabbbb/predecessor-attempt"
        : > "$root/attempts/aaaabbbb/configuration.sh"
        kubectl_attempt_write_current "$root" "$fd2" aaaabbbb
        kubectl_local_lock_release "$fd2"
        # Even if cluster identity validation is unavailable, local recovery
        # must repair the pointer before returning the error.
        _kubectl_verify_saved_cluster_identity() {{ return 1; }}
        kubectl_cleanup_ephemeral_helpers() {{ :; }}
        kubectl_emit_state() {{ printf '%s\\n' "$1"; }}
        ! kubectl_lifecycle_operation status {str(results)!r}
        [[ $(cat "$root/current-attempt") == 1234abcd ]]
        """)
    assert result.returncode == 0, result.stderr


def test_creation_intent_cleans_object_left_before_resource_journal(
    tmp_path: Path,
) -> None:
    """[S-07] Rollback removes an object left before UID journaling."""
    result = _bash(
        _identity(tmp_path / "state")
        + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_creation_intent "$root" "$fd" 1234abcd transfer \
          Pod helper test-ns 0123456789abcdef0123456789abcdef
        events="""
        + str(tmp_path / "events")
        + """
        kubectl_run_bounded() { printf uid-1; }
        kubectl_verify_object_identity() { :; }
        kubectl_delete_owned_object() { printf delete >> "$events"; }
        kubectl_cleanup_journaled_resources "$root" "$fd" 1234abcd
        [[ $(cat "$events") == delete ]]
        [[ ! -e "$root/attempts/1234abcd/creation-intents/transfer.sh" ]]
        ln -s /missing "$root/attempts/1234abcd/creation-intents/tampered.sh"
        ! kubectl_cleanup_journaled_resources "$root" "$fd" 1234abcd
        [[ -L "$root/attempts/1234abcd/creation-intents/tampered.sh" ]]
        kubectl_local_lock_release "$fd"
        """
    )
    assert result.returncode == 0, result.stderr


def test_repeated_owned_delete_treats_proven_absence_quietly() -> None:
    """Idempotent cleanup does not misreport an absent object as UID drift."""
    result = _bash("""
        kubectl_run_bounded() { :; }
        kubectl_delete_owned_object Job sweep test-ns \
            0123456789abcdef0123456789abcdef 1234abcd job-uid
    """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=" not in result.stderr


def test_creation_intent_rechecks_absence_after_create_deadline(tmp_path: Path) -> None:
    """[S-08] Delayed create gets a second bounded absence observation."""
    calls = tmp_path / "calls"
    events = tmp_path / "events"
    result = _bash(_identity(tmp_path / "state") + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_creation_intent "$root" "$fd" 1234abcd transfer \
          Pod helper test-ns 0123456789abcdef0123456789abcdef
        calls={str(calls)!r}
        events={str(events)!r}
        kubectl_run_bounded() {{
            printf x >> "$calls"
            [[ $(wc -c < "$calls") -eq 1 ]] || printf uid-delayed
        }}
        sleep() {{ :; }}
        kubectl_verify_object_identity() {{ [[ "$6" == uid-delayed ]]; }}
        kubectl_delete_owned_object() {{ printf delete > "$events"; }}
        kubectl_cleanup_creation_intent "$root" "$fd" 1234abcd transfer
        [[ $(cat "$calls") == xx && $(cat "$events") == delete ]]
        [[ ! -e "$root/attempts/1234abcd/creation-intents/transfer.sh" ]]
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr


def test_creation_absence_retains_possible_late_object_identity(
    tmp_path: Path,
) -> None:
    """[S-09] Ambiguous create retains its possible late identity."""
    state = tmp_path / "state"
    result = _bash(_identity(state) + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_creation_intent "$root" "$fd" 1234abcd transfer \
          Pod helper test-ns 0123456789abcdef0123456789abcdef
        kubectl_run_bounded() { :; }
        sleep() { :; }
        stat() { printf '1\n'; }
        date() { printf '1000\n'; }
        kubectl_cleanup_creation_intent "$root" "$fd" 1234abcd transfer
        tombstone="$root/attempts/1234abcd/ambiguous-absence/transfer.sh"
        [[ -f "$tombstone" && ! -L "$tombstone" ]]
        grep -F 'KUBECTL_INTENT_NAME=helper' "$tombstone"
        [[ ! -e "$root/attempts/1234abcd/creation-intents/transfer.sh" ]]
        kubectl_local_lock_release "$fd"
    """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=OWNERSHIP_AMBIGUOUS" in result.stderr


_CREATE_TIMEOUT = (
    "error: context deadline exceeded (Client.Timeout or context "
    "cancellation while reading body)"
)
_CREATE_REPLIES = {
    "ok": "return 0",
    "timeout": f"printf '%s\\n' {_CREATE_TIMEOUT!r}; return 1",
    "exists": (
        "printf '%s\\n' 'Error from server (AlreadyExists): error when "
        'creating "STDIN": pods "helper" already exists\'; return 1'
    ),
    "forbidden": (
        "printf '%s\\n' 'Error from server (Forbidden): pods \"helper\" is "
        "forbidden: exceeded quota'; return 1"
    ),
}


def _fake_create_api(
    tmp_path: Path, creates: tuple[str, ...], gets: tuple[str, ...]
) -> str:
    """Return Bash that scripts create replies and exact-name GET results.

    GET results are ``absent``, ``ours``, ``foreign``, or ``fail``; the last
    entry repeats. Every API call and intent refresh is appended to ``$log``.
    """
    replies = "\n".join(
        f"    {index}) {_CREATE_REPLIES[reply]} ;;"
        for index, reply in enumerate(creates, start=1)
    )
    return _identity(tmp_path / "state") + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        log={str(tmp_path / 'log')!r}
        manifests={str(tmp_path / 'manifests')!r}
        : > "$log"
        gets=({' '.join(gets)})
        sleep() {{ :; }}
        touch() {{ printf 'touch %s\\n' "$*" >> "$log"; command touch "$@"; }}
        kubectl_delete_owned_object() {{ printf 'delete\\n' >> "$log"; }}
        _fake_object() {{
          local nonce=0123456789abcdef0123456789abcdef
          case "$1" in
            absent) return 0 ;;
            fail) printf 'connection refused\\n' >&2; return 1 ;;
            foreign) nonce=ffffffffffffffffffffffffffffffff ;;
          esac
          if [[ "$2" == *'jsonpath={{.metadata.uid}}'* ]]; then
            printf uid-1
          else
            printf 'Pod\\thelper\\tuid-1\\t%s\\t1234abcd' "$nonce"
          fi
        }}
        kubectl_run_bounded() {{
          local n
          if [[ " $* " == *' create '* ]]; then
            printf 'create\\n' >> "$log"
            {{ cat; printf '\\n'; }} >> "$manifests"
            n=$(grep -c '^create$' "$log")
            case "$n" in
{replies}
              *) return 1 ;;
            esac
          fi
          printf 'get\\n' >> "$log"
          n=$(grep -c '^get$' "$log")
          (( n <= ${{#gets[@]}} )) || n=${{#gets[@]}}
          _fake_object "${{gets[n - 1]}}" "$*"
        }}
        intent="$root/attempts/1234abcd/creation-intents/transfer.sh"
        create() {{
          kubectl_create_owned_object uid Pod helper test-ns \\
            0123456789abcdef0123456789abcdef 1234abcd manifest-body \\
            "$root" "$fd" transfer
        }}
        uid=
        """


def _api_calls(tmp_path: Path) -> list[str]:
    lines = (tmp_path / "log").read_text(encoding="utf-8").splitlines()
    return ["touch" if line.startswith("touch ") else line for line in lines]


def test_create_retries_absent_object_after_lost_response(tmp_path: Path) -> None:
    """[S-17] A lost create response with proven absence re-sends one create."""
    result = _bash(
        _fake_create_api(tmp_path, ("timeout", "ok"), ("absent", "ours")) + """
        create || exit 2
        [[ "$uid" == uid-1 && -f "$intent" ]] || exit 3
        grep -Fqx "touch -c -- $intent" "$log" || exit 4
        """
    )
    assert result.returncode == 0, result.stderr
    assert _api_calls(tmp_path) == ["create", "get", "touch", "create", "get"]
    manifests = (tmp_path / "manifests").read_text(encoding="utf-8")
    assert manifests == "manifest-body\nmanifest-body\n"
    assert "retrying create of Pod/helper (attempt 2/3)" in result.stderr


def test_create_adopts_object_accepted_before_lost_response(tmp_path: Path) -> None:
    """[S-07] [S-17] A visible owned object is adopted without a second create."""
    result = _bash(_fake_create_api(tmp_path, ("timeout",), ("ours",)) + """
        create || exit 2
        [[ "$uid" == uid-1 ]] || exit 3
        """)
    assert result.returncode == 0, result.stderr
    assert _api_calls(tmp_path) == ["create", "get", "get"]


def test_create_retry_adopts_own_object_on_already_exists(tmp_path: Path) -> None:
    """[S-17] A late original and its retry collide; our nonce is adopted."""
    result = _bash(
        _fake_create_api(tmp_path, ("timeout", "exists"), ("absent", "ours")) + """
        create || exit 2
        [[ "$uid" == uid-1 ]] || exit 3
        """
    )
    assert result.returncode == 0, result.stderr
    assert _api_calls(tmp_path) == ["create", "get", "touch", "create", "get"]


@pytest.mark.parametrize("first_create", ("exists", "timeout"))
def test_create_never_adopts_or_deletes_foreign_object(
    tmp_path: Path, first_create: str
) -> None:
    """[S-17] A same-name object with another nonce fails closed untouched."""
    result = _bash(_fake_create_api(tmp_path, (first_create,), ("foreign",)) + """
        create && exit 2
        [[ -z "$uid" && -f "$intent" ]] || exit 3
        """)
    assert result.returncode == 0, result.stderr
    assert "delete" not in _api_calls(tmp_path)
    assert _api_calls(tmp_path).count("create") == 1
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=IDENTITY_MISMATCH" in result.stderr


def test_create_never_retries_permanent_api_rejection(tmp_path: Path) -> None:
    """[S-17] Forbidden, invalid, or quota errors get one verification only."""
    result = _bash(_fake_create_api(tmp_path, ("forbidden",), ("absent",)) + """
        create && exit 2
        [[ -f "$intent" ]] || exit 3
        """)
    assert result.returncode == 0, result.stderr
    assert _api_calls(tmp_path) == ["create", "get"]


def test_create_observation_failure_is_not_absence(tmp_path: Path) -> None:
    """[S-17] A failed exact GET after a lost response never re-creates."""
    result = _bash(_fake_create_api(tmp_path, ("timeout", "ok"), ("fail",)) + """
        KUBECTL_OBSERVATION_ATTEMPTS=2 create && exit 2
        [[ -f "$intent" ]] || exit 3
        """)
    assert result.returncode == 0, result.stderr
    assert _api_calls(tmp_path) == ["create", "get", "get"]


def test_create_retry_exhaustion_retains_creation_intent(tmp_path: Path) -> None:
    """[S-17] Bounded retries end with the intent kept for S-08/S-09 rollback."""
    result = _bash(
        _fake_create_api(tmp_path, ("timeout", "timeout", "timeout"), ("absent",)) + """
        create && exit 2
        [[ -z "$uid" && -f "$intent" ]] || exit 3
        grep -Fqx 'KUBECTL_INTENT_NAME=helper' "$intent" || exit 4
        """
    )
    assert result.returncode == 0, result.stderr
    assert _api_calls(tmp_path).count("create") == 3
    assert "failed transiently 3 times; its creation intent is retained" in (
        result.stderr
    )


def test_create_attempt_limit_is_validated_before_any_intent(tmp_path: Path) -> None:
    """An invalid retry bound fails before any durable intent or request."""
    result = _bash(_fake_create_api(tmp_path, ("ok",), ("ours",)) + """
        KUBECTL_CREATE_ATTEMPTS=0 create && exit 2
        [[ ! -e "$intent" && ! -s "$log" ]] || exit 3
        KUBECTL_CREATE_ATTEMPTS=1 create || exit 4
        [[ "$uid" == uid-1 ]] || exit 5
        """)
    assert result.returncode == 0, result.stderr
    assert "KUBECTL_CREATE_ATTEMPTS must be a positive integer" in result.stderr


def test_creation_ambiguity_horizon_covers_kubectl_process_timeout(
    tmp_path: Path,
) -> None:
    """[S-08] The absence recheck waits out the longest create deadline."""
    result = _bash(_identity(tmp_path / "state") + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        horizon() {{ kubectl_creation_ambiguity_seconds; }}
        [[ $(horizon) == 30 ]] || exit 2
        [[ $(KUBECTL_PROCESS_TIMEOUT_SECONDS=10 horizon) == 30 ]] || exit 3
        [[ $(KUBECTL_PROCESS_TIMEOUT_SECONDS=90 horizon) == 90 ]] || exit 4
        KUBECTL_PROCESS_TIMEOUT_SECONDS=abc horizon && exit 5
        sleeps={str(tmp_path / 'sleeps')!r}
        kubectl_run_bounded() {{ :; }}
        sleep() {{ printf '%s\\n' "$1" >> "$sleeps"; }}
        stat() {{ printf '1000\\n'; }}
        date() {{ printf '1050\\n'; }}
        for key in short long; do
          kubectl_attempt_write_creation_intent "$root" "$fd" 1234abcd "$key" \\
            Pod "helper-$key" test-ns 0123456789abcdef0123456789abcdef
        done
        kubectl_cleanup_creation_intent "$root" "$fd" 1234abcd short || exit 6
        KUBECTL_PROCESS_TIMEOUT_SECONDS=90 \\
          kubectl_cleanup_creation_intent "$root" "$fd" 1234abcd long || exit 7
        [[ $(paste -sd, "$sleeps") == 2,40,2 ]] || exit 8
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr


def test_ephemeral_cleanup_reconciles_intent_only_helpers(tmp_path: Path) -> None:
    """Status retries remove a helper killed between API create and UID journal."""
    result = _bash(
        _identity(tmp_path / "state")
        + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_creation_intent "$root" "$fd" 1234abcd status-deadbeef \
          Pod status-helper test-ns 0123456789abcdef0123456789abcdef
        kubectl_attempt_write_creation_intent "$root" "$fd" 1234abcd transfer \
          Pod transfer-helper test-ns 0123456789abcdef0123456789abcdef
        events="""
        + str(tmp_path / "events")
        + """
        kubectl_run_bounded() { printf uid-1; }
        kubectl_verify_object_identity() { :; }
        kubectl_delete_owned_object() { printf delete >> "$events"; }
        kubectl_cleanup_ephemeral_helpers "$root" "$fd" 1234abcd
        [[ $(cat "$events") == delete ]]
        [[ ! -e "$root/attempts/1234abcd/creation-intents/status-deadbeef.sh" ]]
        [[ -f "$root/attempts/1234abcd/creation-intents/transfer.sh" ]]
        kubectl_local_lock_release "$fd"
        """
    )
    assert result.returncode == 0, result.stderr


def test_cleanup_step_journal_is_safe_and_idempotent(tmp_path: Path) -> None:
    """Lifecycle retries accept committed steps but reject unsafe markers."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_journal_step "$root" "$fd" 1234abcd delete-sweep
        kubectl_attempt_journal_step "$root" "$fd" 1234abcd delete-sweep
        marker="$root/attempts/1234abcd/cleanup/delete-sweep"
        rm -f -- "$marker"
        ln -s /dev/null "$marker"
        ! kubectl_attempt_journal_step "$root" "$fd" 1234abcd delete-sweep
        rm -f -- "$marker"
        mkdir "$marker"
        ! kubectl_attempt_journal_step "$root" "$fd" 1234abcd delete-sweep
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr


def test_remote_run_tree_is_published_only_after_ownership() -> None:
    """PVC run-tree publication follows durable attempt ownership."""
    source = _FUNCTIONS.read_text(encoding="utf-8")
    body = source.split("kubectl_reserve_remote_attempt() {", 1)[1].split(
        "\n}\n\nkubectl_attempt_journal_remote_reservation", 1
    )[0]
    assert 'printf "%s\\\\t%s\\\\n" "$attempt" "$nonce" > "$pending/owner"' in body
    assert body.index('> "$pending/owner"') < body.index(
        'mv -T -n -- "$pending" "$run"'
    )


def test_cancel_retries_after_job_delete_was_already_journaled(
    tmp_path: Path,
) -> None:
    """[T-08] Cancellation resumes after exact Job deletion was journaled."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_transition "$root" "$fd" 1234abcd CANCEL_REQUESTED
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd sweep \
          Job sweep test-ns job-uid 0123456789abcdef0123456789abcdef
        kubectl_attempt_journal_step "$root" "$fd" 1234abcd delete-sweep
        kubectl_read_remote_status() { printf RUNNING; }
        kubectl_delete_owned_object() { :; }
        kubectl_finalize_cancelled_attempt() { :; }
        [[ $(kubectl_cancel_journaled_attempt "$root" "$fd" test-ns helper \
          1234abcd 0123456789abcdef0123456789abcdef) == CANCELLED ]]
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_LIFECYCLE_STATE" == TERMINAL ]]
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr


def test_cancel_rejects_missing_exact_job_journal(tmp_path: Path) -> None:
    """Cancellation cannot finalize while an unowned Job may still be active."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_read_remote_status() { printf RUNNING; }
        finalized=0
        kubectl_finalize_cancelled_attempt() { finalized=1; }
        ! kubectl_cancel_journaled_attempt "$root" "$fd" test-ns helper \
          1234abcd 0123456789abcdef0123456789abcdef
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_LIFECYCLE_STATE" == CANCEL_REQUESTED ]]
        [[ "$finalized" -eq 0 ]]
        kubectl_attempt_journal_step "$root" "$fd" 1234abcd delete-sweep
        ! kubectl_cancel_journaled_attempt "$root" "$fd" test-ns helper \
          1234abcd 0123456789abcdef0123456789abcdef
        [[ "$finalized" -eq 0 ]]
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=LEDGER_INCONSISTENT" in result.stderr
    assert "do\\ not\\ cancel\\ by\\ label" in result.stderr


def test_collection_reports_missing_pvc_lease_journal(tmp_path: Path) -> None:
    """A corrupt recovery ledger fails closed with an actionable diagnostic."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_transition "$root" "$fd" 1234abcd TERMINAL
        kubectl_attempt_transition "$root" "$fd" 1234abcd COLLECTION_IN_PROGRESS
        ! kubectl_reconcile_pvc_lease_release_after_collection_cleanup \
            "$root" "$fd" 1234abcd
        kubectl_local_lock_release "$fd"
    """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=LEDGER_INCONSISTENT" in result.stderr
    assert "missing\\ or\\ corrupt\\ Lease\\ journal" in result.stderr


def test_collection_reports_conflicting_pvc_lease_journal(tmp_path: Path) -> None:
    """A conflicting recovery journal fails closed with its exact reason."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_transition "$root" "$fd" 1234abcd TERMINAL
        kubectl_attempt_transition "$root" "$fd" 1234abcd COLLECTION_IN_PROGRESS
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd pvc-lease \
          Job conflicting-name test-ns conflicting-uid \
          0123456789abcdef0123456789abcdef
        ! kubectl_reconcile_pvc_lease_release_after_collection_cleanup \
            "$root" "$fd" 1234abcd
        kubectl_local_lock_release "$fd"
    """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=LEDGER_INCONSISTENT" in result.stderr
    assert "conflicting\\ Lease\\ journal" in result.stderr


def test_no_clobber_journals_remove_unpublished_temporary_files(
    tmp_path: Path,
) -> None:
    """A no-clobber collision fails without leaving attacker-shaped debris."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_predecessor "$root" "$fd" 1234abcd aaaabbbb
        ! kubectl_attempt_write_predecessor "$root" "$fd" 1234abcd ccccdddd
        ! compgen -G "$root/attempts/1234abcd/predecessor-attempt.tmp.*" >/dev/null

        mv() {
          if [[ "${*: -1}" == "$root/attempts/1234abcd/creation-intents/transfer.sh" ]]; then
            return 0
          fi
          command mv "$@"
        }
        ! kubectl_attempt_write_creation_intent "$root" "$fd" 1234abcd transfer \
          Pod helper test-ns 0123456789abcdef0123456789abcdef
        ! compgen -G \
          "$root/attempts/1234abcd/creation-intents/transfer.sh.tmp.*" >/dev/null
        """)
    assert result.returncode == 0, result.stderr


def test_stale_resume_contender_cannot_replace_successful_attempt(
    tmp_path: Path,
) -> None:
    """[R-17] Resume is a compare-and-swap against its predecessor."""
    state = tmp_path / "results" / "kubernetes"
    results = tmp_path / "results"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_transition "$root" "$fd" 1234abcd TERMINAL
        kubectl_attempt_transition "$root" "$fd" 1234abcd COLLECTION_IN_PROGRESS
        kubectl_attempt_transition "$root" "$fd" 1234abcd COLLECTED
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        kubectl_local_lock_release "$fd"
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        declare -A mapped=([/mnt/storage-scale-test/bench]=1)
        kubectl_generate_attempt_id() {{ printf aaaabbbb; }}
        kubectl_generate_ownership_nonce() {{ printf 11111111111111111111111111111111; }}
        kubectl_validate_cluster_identity() {{ printf 'namespace-uid\tpv-uid\tpvc-uid\n'; }}
        kubectl_attempt_write_configuration() {{ : > "$1/attempts/$3/configuration.sh"; }}
        kubectl_discover_candidate_nodes() {{ printf 'node-a\tuid-a\tamd64\n' > "$2"; }}
        kubectl_choose_coordinator_node() {{ printf node-a; }}
        kubectl_create_helper_pod() {{ printf -v "$1" uid-transfer; }}
        kubectl_attempt_journal_resource() {{ :; }}
        kubectl_validate_pvc_paths() {{ :; }}
        kubectl_validate_test_root_access() {{ :; }}
        kubectl_acquire_pvc_lease() {{ :; }}
        kubectl_attempt_journal_remote_reservation() {{ :; }}
        kubectl_reserve_remote_attempt() {{ :; }}
        kubectl_attempt_mark_remote_reservation_acquired() {{ :; }}
        kubectl_initialize_remote_control_tree() {{ :; }}
        kubectl_create_attempt_policies() {{ :; }}
        kubectl_create_worker_daemonset() {{ :; }}
        kubectl_wait_worker_endpoints() {{ : > "$4"; }}
        first=
        kubectl_prepare_attempt_lifecycle first {str(results)!r} 1 mapped \
          '' benchmark "$KUBECTL_CONTROL_TEST_ROOT" 1234abcd
        test "$first" = aaaabbbb
        second=
        ! kubectl_prepare_attempt_lifecycle second {str(results)!r} 1 mapped \
          '' benchmark "$KUBECTL_CONTROL_TEST_ROOT" 1234abcd
        test "$(kubectl_attempt_current_id "$root")" = aaaabbbb
        """)
    assert result.returncode == 0, result.stderr


def test_submission_prints_status_cancel_and_collect_commands(tmp_path: Path) -> None:
    """[S-15] Every lifecycle operation is copy-pasteable after submit."""
    results = tmp_path / "results with spaces"
    results.mkdir()
    result = _bash(f"kubectl_emit_lifecycle_commands {str(results)!r}")
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_STATUS_COMMAND=" in result.stdout
    assert "STORAGE_SCALE_TEST_CANCEL_COMMAND=" in result.stdout
    assert "STORAGE_SCALE_TEST_COLLECT_COMMAND=" in result.stdout


@pytest.mark.parametrize("terminal", ("FAILED", "CANCELLED"))
def test_collected_terminal_status_is_a_successful_query(
    tmp_path: Path, terminal: str
) -> None:
    """[R-15] Collected status is local, repeatable, and outcome-neutral."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        for next in SUBMITTED TERMINAL COLLECTION_IN_PROGRESS COLLECTED; do
            kubectl_attempt_transition "$root" "$fd" 1234abcd "$next"
        done
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        mkdir -p "$root/attempts/1234abcd/collected-state" \
          "$root/attempts/1234abcd/control-bundle/executions"
        : > "$root/attempts/1234abcd/configuration.sh"
        kubectl_local_lock_release "$fd"
        kubectl_cleanup_ephemeral_helpers() {{ :; }}
        _kubectl_validate_collected_publication() {{
            printf 'execution\t0001\tFAILED\n' > "$1/publication-manifest.tsv"
            printf -v "$3" {terminal}
        }}
        _kubectl_verify_saved_cluster_identity() {{ return 99; }}
        kubectl_lifecycle_operation status {str(results)!r}
        """)
    assert result.returncode == 0, result.stderr
    assert f"STATE={terminal}" in result.stdout


def test_repeated_collect_preserves_failed_benchmark_exit_semantics(
    tmp_path: Path,
) -> None:
    """Only status is outcome-neutral; collect still signals a failed run."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        for next in SUBMITTED TERMINAL COLLECTION_IN_PROGRESS COLLECTED; do
            kubectl_attempt_transition "$root" "$fd" 1234abcd "$next"
        done
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        mkdir -p "$root/attempts/1234abcd/collected-state" \
          "$root/attempts/1234abcd/control-bundle/executions"
        : > "$root/attempts/1234abcd/configuration.sh"
        kubectl_local_lock_release "$fd"
        kubectl_cleanup_ephemeral_helpers() {{ :; }}
        _kubectl_validate_collected_publication() {{
            printf 'execution\t0001\tFAILED\n' > "$1/publication-manifest.tsv"
            printf -v "$3" FAILED
        }}
        ! kubectl_lifecycle_operation collect {str(results)!r}
        """)
    assert result.returncode == 0, result.stderr
    assert "STATE=FAILED" in result.stdout


def test_status_projects_published_collection_after_remote_cleanup(
    tmp_path: Path,
) -> None:
    """[R-14] Post-publication status names the cleanup state and next command."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        for next in SUBMITTED TERMINAL COLLECTION_IN_PROGRESS; do
            kubectl_attempt_transition "$root" "$fd" 1234abcd "$next"
        done
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        mkdir -p "$root/attempts/1234abcd/collected-state" \
          "$root/attempts/1234abcd/control-bundle/executions"
        : > "$root/attempts/1234abcd/configuration.sh"
        kubectl_local_lock_release "$fd"
        kubectl_cleanup_ephemeral_helpers() {{ :; }}
        _kubectl_validate_collected_publication() {{
            printf 'execution\t0001\tFAILED\n' > "$1/publication-manifest.tsv"
            printf -v "$3" FAILED
        }}
        _kubectl_verify_saved_cluster_identity() {{ return 99; }}
        kubectl_lifecycle_operation status {str(results)!r}
        """)
    assert result.returncode == 0, result.stderr
    assert "STATE=FAILED" in result.stdout
    assert "RESULT_COLLECTION=CLEANUP_PENDING" in result.stdout
    assert "NEXT_ACTION=COLLECT" in result.stdout


def test_collect_active_attempt_reports_terminal_gate_and_next_action(
    tmp_path: Path,
) -> None:
    """[R-01] Active collection refusal names the terminal prerequisite."""
    result = _bash(f"""
        export KUBECTL_NAMESPACE=test-ns
        kubectl_attempt_load_metadata() {{
            KUBECTL_LIFECYCLE_STATE=SUBMITTED
            KUBECTL_ATTEMPT_ID=1234abcd
        }}
        _kubectl_scavenge_collection_staging() {{ :; }}
        _kubectl_create_inspector() {{ printf -v "$1" helper; printf -v "$2" uid; }}
        kubectl_read_remote_status() {{ printf RUNNING; }}
        kubectl_attempt_load_resource() {{
            KUBECTL_RESOURCE_KIND=Job KUBECTL_RESOURCE_NAME=sweep
            KUBECTL_RESOURCE_NAMESPACE=test-ns KUBECTL_RESOURCE_UID=job-uid
            KUBECTL_RESOURCE_NONCE=0123456789abcdef0123456789abcdef
        }}
        kubectl_job_terminal_state() {{ return 2; }}
        _kubectl_remove_inspector() {{ :; }}
        ! kubectl_collect_attempt {str(tmp_path)!r} {str(tmp_path / 'kubernetes')!r} \
          9 1234abcd
        """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_PHASE=terminal-gate" in result.stderr
    assert (
        "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=INVALID_LIFECYCLE_OPERATION"
        in result.stderr
    )
    assert r"retry\ --collect" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_MAY_STILL_BE_RUNNING=yes" in result.stderr


def test_resume_before_collection_prints_exact_collect_command(tmp_path: Path) -> None:
    """[R-16] Resume rejection prints the exact collection prerequisite."""
    results = tmp_path / "results with spaces"
    state = results / "kubernetes"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        : > "$root/attempts/1234abcd/configuration.sh"
        kubectl_local_lock_release "$fd"
        ! kubectl_resume_collected_sweep {str(results)!r}
        """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_COLLECT_COMMAND=" in result.stdout
    assert "--collect" in result.stdout
    assert str(results).replace(" ", "\\ ") in result.stdout
    assert (
        "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=INVALID_LIFECYCLE_OPERATION"
        in result.stderr
    )


def test_prepared_attempt_recovery_releases_intended_reservation(
    tmp_path: Path,
) -> None:
    """An interrupted pre-Job reservation needs no initialized control tree."""
    state = tmp_path / "results" / "kubernetes"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        : > "$root/attempts/1234abcd/configuration.sh"
        kubectl_attempt_journal_remote_reservation "$root" "$fd" 1234abcd \
          0123456789abcdef0123456789abcdef
        kubectl_local_lock_release "$fd"
        events={str(tmp_path / 'events')!r}
        _kubectl_verify_saved_cluster_identity() {{ :; }}
        kubectl_cleanup_ephemeral_helpers() {{ :; }}
        _kubectl_create_inspector() {{
            printf -v "$1" helper
            printf -v "$2" uid
            printf create- >> "$events"
        }}
        kubectl_release_journaled_remote_attempt() {{ printf release- >> "$events"; }}
        _kubectl_remove_inspector() {{ printf remove- >> "$events"; }}
        kubectl_cleanup_journaled_resources() {{ printf cleanup- >> "$events"; }}
        kubectl_release_pvc_lease() {{ printf lease- >> "$events"; }}
        kubectl_emit_state() {{ printf '%s\n' "$1"; }}
        kubectl_lifecycle_operation status {str(tmp_path / 'results')!r}
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]
        [[ $(cat "$events") == create-release-remove-cleanup-lease- ]]
        """)
    assert result.returncode == 0, result.stderr


def test_public_status_recovers_coordinator_loss_before_running(
    tmp_path: Path,
) -> None:
    """[T-04] [T-06] Exact Job loss makes PREPARED state collectible."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    calls = tmp_path / "status-calls"
    events = tmp_path / "events"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        cat > "$root/attempts/1234abcd/configuration.sh" <<'EOF'
export KUBECTL_NAMESPACE=test-ns
export KUBECTL_PVC=test-pvc
EOF
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd sweep \
          Job sweep test-ns job-uid 0123456789abcdef0123456789abcdef
        kubectl_local_lock_release "$fd"
        calls={str(calls)!r}
        events={str(events)!r}
        kubectl_cleanup_ephemeral_helpers() {{ :; }}
        _kubectl_verify_saved_cluster_identity() {{ :; }}
        kubectl_verify_journaled_pvc_lease() {{ :; }}
        _kubectl_create_inspector() {{ printf -v "$1" helper; printf -v "$2" uid; }}
        kubectl_read_remote_status() {{
            printf x >> "$calls"
            [[ $(wc -c < "$calls") -eq 1 ]] && printf PREPARED || printf FAILED
        }}
        kubectl_job_terminal_state() {{ printf -v "$1" FAILED; }}
        _kubectl_verify_saved_worker_endpoints() {{ :; }}
        kubectl_preserve_storage_failure_diagnostics() {{ printf diagnose- >> "$events"; }}
        kubectl_recover_lost_coordinator() {{ printf recover- >> "$events"; }}
        kubectl_read_remote_execution_progress() {{
            [[ "$4" == FAILED ]] || return 1
            printf progress- >> "$events"
            printf '1 0 0 0\n'
        }}
        _kubectl_remove_inspector() {{ printf remove- >> "$events"; }}
        kubectl_emit_state() {{ printf 'STATE=%s\n' "$1"; }}
        kubectl_lifecycle_operation status {str(results)!r}
        [[ $(cat "$events") == diagnose-recover-progress-remove- ]]
    """)
    assert result.returncode == 0, result.stderr
    assert "STATE=FAILED" in result.stdout
    assert "EXECUTIONS_PENDING=1" in result.stdout


@pytest.mark.parametrize(
    "failure,reason",
    [
        (None, None),
        ("return 1", "LEDGER_INCONSISTENT"),
        ("echo Forbidden >&2; return 1", "AUTH"),
        ("return 124", "TIMEOUT"),
        (
            "echo 'invalid Kubernetes execution status' >&2; return 1",
            "LEDGER_INCONSISTENT",
        ),
    ],
)
@pytest.mark.parametrize("warning", [False, True])
def test_public_status_reads_progress_before_removing_inspector(
    tmp_path, failure, reason, warning
):
    """Live progress uses the existing helper and leaves client cells untouched."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    result = _bash(_identity(state) + f"""
        set -e
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        printf 'export KUBECTL_NAMESPACE=test-ns\n' > "$root/attempts/1234abcd/configuration.sh"
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd sweep \
          Job sweep test-ns job-uid 0123456789abcdef0123456789abcdef
        kubectl_local_lock_release "$fd"
        mkdir -p {str(results / 'executions')!r}
        printf PENDING > {str(results / 'executions/0001.status')!r}
        events={str(tmp_path / 'events')!r}
        kubectl_cleanup_ephemeral_helpers() {{ :; }}
        _kubectl_verify_saved_cluster_identity() {{ :; }}
        kubectl_verify_journaled_pvc_lease() {{ :; }}
        _kubectl_create_inspector() {{ printf -v "$1" helper; printf -v "$2" uid; }}
        kubectl_read_remote_status() {{ printf RUNNING; }}
        kubectl_job_terminal_state() {{ return 2; }}
        _kubectl_verify_saved_worker_endpoints() {{ :; }}
        kubectl_read_remote_execution_progress() {{
            [[ "$1:$2:$3:$4" == test-ns:helper:1234abcd:RUNNING ]] || return 2
            printf progress- >> "$events"
            {"echo 'Warning: version difference between client and server' >&2" if warning else ":"}
            {failure or "printf '0 1 0 0\\n'"}
        }}
        _kubectl_remove_inspector() {{ printf remove- >> "$events"; }}
        outcome=0
        kubectl_lifecycle_operation status {str(results)!r} || outcome=$?
        [[ "$outcome" == {1 if failure else 0} ]]
        [[ $(cat "$events") == progress-remove- ]]
        [[ $(cat {str(results / 'executions/0001.status')!r}) == PENDING ]]
        """)
    assert result.returncode == 0, result.stderr
    if failure:
        assert "STORAGE_SCALE_TEST_DIAGNOSTIC_PHASE=execution-progress" in result.stderr
        assert f"STORAGE_SCALE_TEST_DIAGNOSTIC_REASON={reason}" in result.stderr
        assert "EXECUTIONS_RUNNING=" not in result.stdout
    else:
        assert "STATE=RUNNING" in result.stdout
        assert "EXECUTIONS_RUNNING=1" in result.stdout
        assert "Warning:" not in result.stdout
        if warning:
            assert "Warning: version difference" in result.stderr


@pytest.mark.parametrize(
    "outcome,counts,collection,expected,next_action",
    [
        ("PREPARED", "30 0 0 0", "PENDING", "PREPARING", "WAIT"),
        ("RUNNING", "8 1 21 0", "PENDING", "RUNNING", "WAIT"),
        ("RUNNING", "8 0 22 0", "PENDING", "BETWEEN_EXECUTIONS", "WAIT"),
        ("RUNNING", "0 0 30 0", "PENDING", "AWAITING_COMPLETION", "WAIT"),
        ("RUNNING", "29 0 0 1", "PENDING", "BETWEEN_EXECUTIONS", "WAIT"),
        ("SUCCESS", "0 0 30 0", "PENDING", "SUCCESS", "COLLECT"),
        ("FAILED", "1 0 1 1", "CLEANUP_PENDING", "FAILED", "COLLECT"),
        ("FAILED", "1 0 1 1", "COMPLETE", "FAILED", "RESUME"),
        ("CANCELLED", "0 0 2 0", "COMPLETE", "CANCELLED", "NONE"),
        ("SUCCESS", "0 0 2 0", "COMPLETE", "SUCCESS", "NONE"),
    ],
)
def test_status_view_separates_execution_progress_from_collection(
    tmp_path, outcome, counts, collection, expected, next_action
):
    """One scoped progress view; zero running cells never proves collectibility."""
    result = _bash(f"""
        kubectl_emit_status_view {str(tmp_path)!r} 1234abcd {outcome} \
            PVC {collection} '{counts}'
        """)
    assert result.returncode == 0, result.stderr
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert fields["STATE"] == expected
    assert fields["NEXT_ACTION"] == next_action
    assert fields["EXECUTION_SCOPE"] == "CURRENT_ATTEMPT"
    assert fields["EXECUTIONS_TOTAL"] == str(sum(map(int, counts.split())))
    assert fields["RESULT_COLLECTION"] == collection
    assert not any(key.startswith("STORAGE_SCALE_TEST_") for key in fields)


def test_status_refreshes_outcome_and_counts_after_completion(tmp_path):
    """The final state read wins over the earlier RUNNING observation."""
    events = tmp_path / "events"
    result = _bash(f"""
        events={str(events)!r}
        kubectl_read_remote_execution_progress() {{
            printf '%s\n' "$4" >> "$events"
            [[ "$4" == RUNNING ]] && printf '0 1 29 0' || printf '0 0 30 0'
        }}
        kubectl_read_remote_status() {{ printf SUCCESS; }}
        progress=$(kubectl_read_refreshed_execution_progress ns pod 1234abcd RUNNING)
        [[ "$progress" == 'SUCCESS 0 0 30 0' ]]
        kubectl_emit_status_view {str(tmp_path)!r} 1234abcd "${{progress%% *}}" \
            PVC PENDING "${{progress#* }}"
        """)
    assert result.returncode == 0, result.stderr
    assert events.read_text().splitlines() == ["RUNNING", "SUCCESS"]
    assert "STATE=SUCCESS\n" in result.stdout
    assert "EXECUTIONS_RUNNING=0\n" in result.stdout
    assert "NEXT_ACTION=COLLECT\n" in result.stdout


def test_status_reports_incomplete_cancellation_as_retryable_failure(
    tmp_path: Path,
) -> None:
    """[T-09] Status reports interrupted cancellation with retry guidance."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_transition "$root" "$fd" 1234abcd CANCEL_REQUESTED
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd sweep \
          Job sweep test-ns job-uid 0123456789abcdef0123456789abcdef
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        cat > "$root/attempts/1234abcd/configuration.sh" <<'EOF'
export KUBECTL_NAMESPACE=test-ns
EOF
        kubectl_local_lock_release "$fd"
        kubectl_cleanup_ephemeral_helpers() {{ :; }}
        _kubectl_verify_saved_cluster_identity() {{ :; }}
        kubectl_verify_journaled_pvc_lease() {{ :; }}
        ! kubectl_lifecycle_operation status {str(results)!r}
    """)
    assert result.returncode == 0, result.stderr
    assert "STATE=CANCEL_REQUESTED" in result.stdout
    assert (
        "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=CANCELLATION_INCOMPLETE" in result.stderr
    )
    assert "retry\\ --cancel" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_NAME=sweep" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_EXPECTED_UID=job-uid" in result.stderr


def test_status_rejects_cancellation_without_exact_job_journal(
    tmp_path: Path,
) -> None:
    """A corrupt cancellation cannot fall back to label-based ownership."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_transition "$root" "$fd" 1234abcd CANCEL_REQUESTED
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        cat > "$root/attempts/1234abcd/configuration.sh" <<'EOF'
export KUBECTL_NAMESPACE=test-ns
EOF
        kubectl_local_lock_release "$fd"
        kubectl_cleanup_ephemeral_helpers() {{ :; }}
        _kubectl_verify_saved_cluster_identity() {{ :; }}
        kubectl_verify_journaled_pvc_lease() {{ :; }}
        ! kubectl_lifecycle_operation status {str(results)!r}
    """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=LEDGER_INCONSISTENT" in result.stderr
    assert "do\\ not\\ cancel\\ by\\ label" in result.stderr


@pytest.mark.parametrize("terminal_on_refresh", [False, True])
def test_completed_job_with_nonterminal_ledger_is_diagnosed(
    tmp_path: Path,
    terminal_on_refresh: bool,
) -> None:
    """[T-05] Complete Job plus nonterminal PVC state is diagnosed."""
    results = tmp_path / "results"
    state = results / "kubernetes"
    result = _bash(_identity(state) + f"""
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_transition "$root" "$fd" 1234abcd SUBMITTED
        kubectl_attempt_write_current "$root" "$fd" 1234abcd
        cat > "$root/attempts/1234abcd/configuration.sh" <<'EOF'
export KUBECTL_NAMESPACE=test-ns
EOF
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd sweep \
          Job sweep test-ns job-uid 0123456789abcdef0123456789abcdef
        kubectl_local_lock_release "$fd"
        kubectl_cleanup_ephemeral_helpers() {{ :; }}
        _kubectl_verify_saved_cluster_identity() {{ :; }}
        kubectl_verify_journaled_pvc_lease() {{ :; }}
        _kubectl_create_inspector() {{ printf -v "$1" helper; printf -v "$2" uid; }}
        status_calls={str(tmp_path / 'calls')!r}
        kubectl_read_remote_status() {{
            printf x >> "$status_calls"
            if [[ $(wc -c < "$status_calls") -gt 1 && {1 if terminal_on_refresh else 0} == 1 ]]; then
                printf SUCCESS
            else
                printf RUNNING
            fi
        }}
        kubectl_job_terminal_state() {{ printf -v "$1" COMPLETE; }}
        kubectl_read_remote_execution_progress() {{ printf '0 0 1 0\n'; }}
        _kubectl_verify_saved_worker_endpoints() {{ :; }}
        _kubectl_remove_inspector() {{ :; }}
        {"" if terminal_on_refresh else "! "}kubectl_lifecycle_operation status {str(results)!r}
    """)
    assert result.returncode == 0, result.stderr
    if terminal_on_refresh:
        assert "STATE=SUCCESS\n" in result.stdout
        assert "NEXT_ACTION=COLLECT\n" in result.stdout
        assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=" not in result.stderr
    else:
        assert (
            "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=LEDGER_INCONSISTENT" in result.stderr
        )
        assert "STORAGE_SCALE_TEST_DIAGNOSTIC_EXPECTED_UID=job-uid" in result.stderr


def test_missing_worker_daemonset_emits_identity_diagnostics(tmp_path: Path) -> None:
    """[C-12] [T-10] Missing or replaced workers emit identity evidence."""
    state = tmp_path / "results" / "kubernetes"
    result = _bash(_identity(state) + """
        kubectl_attempt_journal_resource "$root" "$fd" 1234abcd workers \
          DaemonSet sst-elb-1234abcd-workers test-ns worker-uid \
          0123456789abcdef0123456789abcdef
        kubectl_verify_object_identity() { return 1; }
        kubectl_capture_resource_diagnostics() { printf /tmp/worker-identity; }
        ! _kubectl_verify_saved_worker_endpoints "$root" 1234abcd
        """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=IDENTITY_MISMATCH" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_RESOURCE_KIND=DaemonSet" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_EXPECTED_UID=worker-uid" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_PATH=/tmp/worker-identity" in result.stderr
