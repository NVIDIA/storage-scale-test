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


def test_collection_merges_manifest_declared_execution_ledgers(tmp_path: Path) -> None:
    """Collection publishes exit-code and worker evidence beside cell status."""
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
    result = _bash(f"_kubectl_merge_collected_results {str(state)!r} {str(results)!r}")
    assert result.returncode == 0, result.stderr
    assert (results / "executions/0001.status").read_text(encoding="utf-8") == (
        "SUCCESS\n"
    )
    assert (results / "executions/0001.exitcode").read_text(encoding="utf-8") == ("0\n")
    assert "node-a" in (results / "executions/0001.workers.tsv").read_text(
        encoding="utf-8"
    )


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
    result = _bash(f"_kubectl_merge_collected_results {str(state)!r} {str(results)!r}")
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
          printf 'node-a\\tpod-a\\tpoduid-a\\t10.0.0.1\\tTrue\\tsha256:one\\t\\n'
          printf 'node-b\\tpod-old\\tpoduid-old\\t10.0.0.2\\tTrue\\tsha256:one\\t2026-01-01T00:00:00Z\\n'
          printf 'node-b\\tpod-new\\tpoduid-new\\t10.0.0.3\\tTrue\\tsha256:one\\t\\n'
        }}
        kubectl_discover_worker_endpoints test-ns 1234abcd {str(nodes)!r} {str(output)!r}
        ! grep -F pod-old {str(output)!r}
        grep -F $'node-b\\tuid-b\\tpod-new\\tpoduid-new\\t10.0.0.3' {str(output)!r}
        """)
    assert result.returncode == 0, result.stderr


def test_terminal_job_evidence_preserves_empty_active_field() -> None:
    """A failed Job with no active count reaches coordinator recovery."""
    result = _bash("""
        kubectl_verify_object_identity() { :; }
        kubectl_run_bounded() { printf '|1|True'; }
        kubectl_job_is_terminal_failure Job sweep test-ns \
          0123456789abcdef0123456789abcdef 1234abcd uid-1
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
        kubectl_run_bounded() {{ printf %s {malformed!r}; }}
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
    """Frozen worker identities reject changed addresses but tolerate same-IP replacement."""
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
            printf %b {rows!r}
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
            if [[ -e {str(invocation)!r} ]]; then
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
        kubectl_run_bounded() {{ printf '{row}\\n'; }}
        ! kubectl_discover_worker_endpoints test-ns 1234abcd \
          {str(nodes)!r} {str(output)!r}
        """)
    assert result.returncode == 0, result.stderr
    assert not output.exists()


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


def test_helper_creation_returns_uid_to_common_caller_variable_names() -> None:
    """Nested Bash output variables must not be shadowed by helper locals."""
    result = _bash("""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.1-11
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
        kubectl_attempt_remote_root() {{ printf %s /mnt/storage-scale-test/.storage-scale-test/runs/1234abcd; }}
        kubectl_populate_sweep_control_bundle {str(bundle)!r} {str(results)!r} \\
            {str(endpoints)!r}
        test -x {str(bundle / 'failure-overlay.sh')!r}
        grep -F '/mnt/storage-scale-test/.storage-scale-test/runs/1234abcd/control/failure-overlay.sh' \\
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


@pytest.mark.parametrize("failed_stage", ("release", "cleanup", "remove"))
def test_collection_recovery_retries_every_cleanup_stage(
    tmp_path: Path, failed_stage: str
) -> None:
    """A durable import remains recoverable across each cleanup failure."""
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
        kubectl_attempt_transition() {{ printf transition- >> "$events"; }}
        kubectl_emit_state() {{ printf '%s\\n' "$1"; }}
        ! kubectl_collect_attempt {str(results)!r} {str(results / 'kubernetes')!r} \
            9 1234abcd
        kubectl_collect_attempt {str(results)!r} {str(results / 'kubernetes')!r} \
            9 1234abcd
        grep -F transition- "$events"
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
        kubectl_attempt_journal_remote_reservation() {{ printf journal >> "$events"; }}
        kubectl_reserve_remote_attempt() {{ printf reserve >> "$events"; }}
        kubectl_attempt_mark_remote_reservation_acquired() {{ printf acquired >> "$events"; }}
        kubectl_initialize_remote_control_tree() {{ printf initialize >> "$events"; }}
        kubectl_create_attempt_policies() {{ printf policy >> "$events"; }}
        kubectl_create_worker_daemonset() {{ printf workers >> "$events"; }}
        kubectl_wait_worker_endpoints() {{ printf endpoints >> "$events"; : > "$4"; }}
        attempt=
        kubectl_prepare_attempt_lifecycle attempt {str(tmp_path)!r} 2 mapped
        [[ "$attempt" =~ ^[0-9a-f]{{8}}$ ]]
        test -f {str(tmp_path)!r}/kubernetes/attempts/"$attempt"/identity.sh
        test -f {str(tmp_path)!r}/kubernetes/attempts/"$attempt"/configuration.sh
        [[ $(cat "$events") == nodeshelperpathsjournalreserveacquiredinitializepolicyworkersendpoints ]]
        """)
    assert result.returncode == 0, result.stderr


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
        kubectl_emit_state() {{ printf '%s\n' "$1"; }}
        kubectl_lifecycle_operation status {str(tmp_path / 'results')!r}
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]
        [[ $(cat "$events") == create-release-remove-cleanup- ]]
        """)
    assert result.returncode == 0, result.stderr
