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
        [_BASH, "-c", textwrap.dedent(f"source {_FUNCTIONS!s}\n{body}")],
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


def test_identity_is_complete_atomic_and_path_bound(tmp_path: Path) -> None:
    """A partial or moved identity cannot be mistaken for an owned attempt."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_write_state "$root" "$fd" 1234abcd PREPARED
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        mv "$root/attempts/1234abcd" "$root/attempts/abcdef12"
        ! kubectl_attempt_load_metadata "$root/attempts/abcdef12"
        mkdir "$root/attempts/deadbeef"
        printf 'KUBECTL_ATTEMPT_SCHEMA=1\n' > "$root/attempts/deadbeef/identity.sh"
        ! kubectl_attempt_load_identity "$root/attempts/deadbeef"
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


def test_attempt_templates_are_yaml_and_restrict_security_surface() -> None:
    """The checked-in resources have no API token, host networking, or Service."""
    result = _bash("""
        selector=$(kubectl_render_node_selector 'storage-test=true,role=client')
        nodes=$(kubectl_render_node_affinity_values node-a node-b)
        kubectl_render_attempt_template storage-tests/fs/kubectl/templates/worker-daemonset.yaml.tmpl \\
          NAMESPACE=test-ns RESOURCE_NAME=sst-elb-1234abcd-workers ATTEMPT_ID=1234abcd \\
          OWNERSHIP_NONCE=0123456789abcdef0123456789abcdef IMAGE=breuner/elbencho:v3.1-11 \\
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
    assert (
        pod_spec["containers"][0]["securityContext"]["allowPrivilegeEscalation"]
        is False
    )
    for template in (_ROOT / "storage-tests/fs/kubectl/templates").glob("*.yaml.tmpl"):
        assert "@@" in template.read_text(encoding="utf-8") or template.name.endswith(
            "network-policy.yaml.tmpl"
        )


def test_sweep_job_exposes_coordinator_identity_with_downward_api() -> None:
    """The phase-six coordinator gets its exact Pod identity without API access."""
    result = _bash("""
        kubectl_render_attempt_template storage-tests/fs/kubectl/templates/sweep-job.yaml.tmpl \\
          NAMESPACE=test-ns RESOURCE_NAME=sst-elb-1234abcd-sweep ATTEMPT_ID=1234abcd \\
          OWNERSHIP_NONCE=0123456789abcdef0123456789abcdef IMAGE=breuner/elbencho:v3.1-11 \\
          IMAGE_PULL_POLICY=Never RUN_AS_USER=2000 RUN_AS_GROUP=2000 PVC_NAME=test-pvc \\
          COORDINATOR_NODE=node-a
        """)
    assert result.returncode == 0, result.stderr
    job = yaml.safe_load(result.stdout)
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
    """Traversal and symbolic-link tar members fail before local publication."""
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


def test_fake_kubectl_discovers_only_ready_nonterminating_workers(
    tmp_path: Path,
) -> None:
    """Endpoint discovery rejects a terminating rollout overlap and bad IPv4."""
    nodes = tmp_path / "nodes.tsv"
    nodes.write_text("node-a\tuid-a\tamd64\nnode-b\tuid-b\tamd64\n", encoding="utf-8")
    output = tmp_path / "workers.tsv"
    result = _bash(f"""
        kubectl_run_bounded() {{
          printf 'node-a\\tpod-a\\tpoduid-a\\t10.0.0.1\\t\\tTrue\\tsha256:one\\n'
          printf 'node-b\\tpod-old\\tpoduid-old\\t10.0.0.2\\t2026-01-01T00:00:00Z\\tTrue\\tsha256:one\\n'
        }}
        ! kubectl_discover_worker_endpoints test-ns 1234abcd {str(nodes)!r} {str(output)!r}
        """)
    assert result.returncode == 0, result.stderr


def test_helper_template_is_rendered_and_removed_when_readiness_fails() -> None:
    """A helper cannot leak after an unsuccessful readiness wait."""
    result = _bash("""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.1-11
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
        grep -q '/mnt/storage-scale-test/.storage-scale-test/runs/1234abcd' "$captured"
        grep -q 'automountServiceAccountToken: false' "$captured"
        """)
    assert result.returncode == 0, result.stderr
    assert "deleted" in result.stdout


def test_runtime_configuration_rejects_every_invalid_field() -> None:
    """Validation must not let a bad later field bypass a valid namespace."""
    result = _bash("""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV='bad/pv' KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.1-11
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        ! kubectl_validate_runtime_configuration
        """)
    assert result.returncode == 0, result.stderr


def test_pvc_path_validator_defends_reserved_tree_and_future_parent() -> None:
    """PVC workload paths cannot include or contain orchestration state."""
    result = _bash("""
        kubectl_pvc_exec() { printf '%s' "$5"; }
        captured=$(kubectl_validate_pvc_paths test-ns helper \
          /mnt/storage-scale-test/workload/future)
        [[ "$captured" == *'nearest existing parent'* ]]
        [[ "$captured" == *'workload path overlaps orchestration state'* ]]
        [[ "$captured" == *'orchestration state overlaps workload path'* ]]
        """)
    assert result.returncode == 0, result.stderr


def test_control_bundle_stages_phase_six_contract_once(tmp_path: Path) -> None:
    """The trusted helper, coordinator, and frozen run metadata stay together."""
    coordinator = tmp_path / "coordinator-source.sh"
    coordinator.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    result = _bash(_identity(tmp_path / "state") + f"""
        kubectl_prepare_control_bundle bundle "$root" "$fd" 1234abcd \\
          elbencho-20260922Z123456 {str(coordinator)!r}
        test -x "$bundle/coordinator.sh"
        test -f "$bundle/_nv-elbencho-kubectl-functions.sh"
        test -f "$bundle/bundle-manifest.tsv"
        grep -Eq '^[0-9a-f]{{64}}'$'\t''[^[:space:]]+$' "$bundle/bundle-manifest.tsv"
        ! grep -Fq '\\t' "$bundle/bundle-manifest.tsv"
        grep -Fx 'attempt_id\t1234abcd' "$bundle/run-metadata.tsv"
        grep -Fx 'output_basename\telbencho-20260922Z123456' "$bundle/run-metadata.tsv"
        ! kubectl_prepare_control_bundle other "$root" "$fd" 1234abcd \\
          elbencho-20260922Z123456 {str(coordinator)!r}
        kubectl_local_lock_release "$fd"
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


def test_remote_reservation_release_is_journaled_in_retryable_steps(
    tmp_path: Path,
) -> None:
    """Run removal is durably recorded before the separately owned lock removal."""
    result = _bash(_identity(tmp_path / "state") + """
        kubectl_attempt_journal_remote_reservation "$root" "$fd" 1234abcd \\
          0123456789abcdef0123456789abcdef
        calls=0
        kubectl_pvc_exec() { calls=$((calls + 1)); return 0; }
        kubectl_release_journaled_remote_attempt "$root" "$fd" test-ns helper 1234abcd
        [[ "$calls" -eq 2 ]]
        kubectl_attempt_step_done "$root" 1234abcd release-remote-run
        kubectl_attempt_step_done "$root" 1234abcd release-remote-lock
        kubectl_local_lock_release "$fd"
        """)
    assert result.returncode == 0, result.stderr


def test_fake_pre_job_lifecycle_orders_identity_reservation_and_workers(
    tmp_path: Path,
) -> None:
    """The pre-Job dispatcher is a testable sequence, not disconnected helpers."""
    result = _bash(f"""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.1-11
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        declare -A mapped=([/mnt/storage-scale-test/bench]=1)
        events={str(tmp_path / 'events')!r}
        kubectl_validate_cluster_identity() {{ printf 'namespace-uid\\tpv-uid\\tpvc-uid\\n'; }}
        kubectl_discover_candidate_nodes() {{ printf 'node-a\\tuid-a\\tamd64\\nnode-b\\tuid-b\\tamd64\\n' > "$2"; printf nodes >> "$events"; }}
        kubectl_choose_coordinator_node() {{ printf 'node-a\\n'; }}
        kubectl_create_helper_pod() {{ printf -v "$1" uid-transfer; printf helper >> "$events"; }}
        kubectl_validate_pvc_paths() {{ printf paths >> "$events"; }}
        kubectl_reserve_remote_attempt() {{ printf reserve >> "$events"; }}
        kubectl_initialize_remote_control_tree() {{ printf initialize >> "$events"; }}
        kubectl_create_attempt_policies() {{ printf policy >> "$events"; }}
        kubectl_create_worker_daemonset() {{ printf workers >> "$events"; }}
        kubectl_wait_worker_endpoints() {{ printf endpoints >> "$events"; : > "$4"; }}
        attempt=
        kubectl_prepare_attempt_lifecycle attempt {str(tmp_path)!r} 2 mapped
        [[ "$attempt" =~ ^[0-9a-f]{{8}}$ ]]
        test -f {str(tmp_path)!r}/kubernetes/attempts/"$attempt"/identity.sh
        test -f {str(tmp_path)!r}/kubernetes/attempts/"$attempt"/configuration.sh
        [[ $(cat "$events") == nodeshelperpathsreserveinitializepolicyworkersendpoints ]]
        test -f {str(tmp_path)!r}/kubernetes/attempts/"$attempt"/remote-reservation.sh
        """)
    assert result.returncode == 0, result.stderr
