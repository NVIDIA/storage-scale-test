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
    """A publication cannot redefine identity, executions, or destinations."""
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


def test_collected_manifest_accepts_complete_success_evidence(tmp_path: Path) -> None:
    """A complete cell ledger and required workload output are collectible."""
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
        f"! _kubectl_merge_collected_results {str(state)!r} {str(results)!r}"
    )
    assert result.returncode == 0, result.stderr
    assert not (outside / "output.txt").exists()


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
    """A failed Job with no active count reaches coordinator recovery."""
    result = _bash("""
        kubectl_verify_object_identity() { :; }
        kubectl_run_bounded() { printf '|0|1||True'; }
        kubectl_job_is_terminal_failure Job sweep test-ns \
          0123456789abcdef0123456789abcdef 1234abcd uid-1
        """)
    assert result.returncode == 0, result.stderr


def test_collection_stream_uses_its_operation_sized_deadline(tmp_path: Path) -> None:
    """A large archive never inherits the short status-command deadline."""
    archive = tmp_path / "attempt.tar"
    result = _bash(f"""
        export KUBECTL_COLLECTION_TIMEOUT_SECONDS=123
        kubectl_pvc_exec() {{
          printf '%s\t%s\n' "$KUBECTL_REQUEST_TIMEOUT_SECONDS" \
            "$KUBECTL_PROCESS_TIMEOUT_SECONDS"
        }}
        kubectl_stream_remote_attempt test-ns collector 1234abcd {str(archive)!r}
        test "$(cat {str(archive)!r})" = $'123\t183'
        """)
    assert result.returncode == 0, result.stderr


def test_collection_byte_limit_is_enforced_while_streaming(tmp_path: Path) -> None:
    """The receiver stops an oversized stream before it fills local storage."""
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


def test_collection_capacity_checks_the_results_filesystem(tmp_path: Path) -> None:
    """Collection fails before transfer when its local working set will not fit."""
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
    """A retry removes stale working trees without following hostile links."""
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
    assert "timeout --kill-after=5s" in body
    assert "timeout --foreground" not in body


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
    """Node recreation, architecture, and image drift all fail closed."""
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


def test_always_pull_policy_requires_an_immutable_image_reference() -> None:
    """A coordinator repull cannot diverge from the frozen worker image."""
    result = _bash("""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.1-11
        export KUBECTL_IMAGE_PULL_POLICY=Always KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        ! kubectl_validate_runtime_configuration
        export KUBECTL_ELBENCHO_IMAGE='breuner/elbencho:v3.1-11@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        kubectl_validate_runtime_configuration
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
    assert source.count('-f "$lock/owner"') >= 2
    assert source.count('! -L "$lock/owner"') >= 2


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
        kubectl_render_attempt_template storage-tests/fs/kubectl/templates/validation-job.yaml.tmpl \
          NAMESPACE=test-ns RESOURCE_NAME=sst-elb-1234abcd-validation ATTEMPT_ID=1234abcd \
          OWNERSHIP_NONCE=0123456789abcdef0123456789abcdef IMAGE=breuner/elbencho:v3.1-11 \
          IMAGE_PULL_POLICY=Never RUN_AS_USER=2000 RUN_AS_GROUP=2000 PVC_NAME=test-pvc \
          NODE_NAME=node-a
        """)
    assert result.returncode == 0, result.stderr
    job = yaml.safe_load(result.stdout)
    assert job["kind"] == "Job"
    assert job["spec"]["activeDeadlineSeconds"] == 180
    assert job["spec"]["ttlSecondsAfterFinished"] == 60
    pod = job["spec"]["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["restartPolicy"] == "Never"
    body = pod["containers"][0]["args"][0]
    assert "type -P stat" in body
    assert "type -P gstat" in body
    assert "type -P pkill" in body
    assert "type -P killall" in body


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


def test_remote_release_executes_guards_before_owner_read_and_deletion(
    tmp_path: Path,
) -> None:
    """Release rejects symlinked owners while allowing an owned tree to clean up."""
    mount = tmp_path / "mount"
    result = _bash(f"""
        mount={str(mount)!r}
        root="$mount/.storage-scale-test"
        lock="$root/locks/kubernetes-elbencho-sweep"
        run="$root/runs/1234abcd"
        nonce=0123456789abcdef0123456789abcdef
        mkdir -p "$lock" "$run"
        printf '1234abcd\\t%s\\n' "$nonce" > "$lock/owner"
        kubectl_pvc_exec() {{
            script="$5"
            script=$(sed "s#/mnt/storage-scale-test#$mount#g" <<< "$script")
            lock_arg=$(sed "s#/mnt/storage-scale-test#$mount#g" <<< "$7")
            run_arg=$(sed "s#/mnt/storage-scale-test#$mount#g" <<< "$8")
            bash -ceu "$script" bash "$lock_arg" "$run_arg" "$9" "${{10}}" \
                "${{11}}"
        }}
        kubectl_release_remote_attempt test-ns helper 1234abcd "$nonce"
        [[ ! -e "$lock" && ! -e "$run" ]]

        pending="$lock.pending.1234abcd.$nonce"
        mkdir "$pending"
        kubectl_release_remote_attempt test-ns helper 1234abcd "$nonce"
        [[ ! -e "$pending" ]]

        mkdir "$pending"
        printf 'unexpected\n' > "$pending/not-owned"
        ! kubectl_release_remote_attempt test-ns helper 1234abcd "$nonce"
        [[ -f "$pending/not-owned" ]]
        rm -rf -- "$pending"

        mkdir -p "$lock" "$run" "$mount/outside"
        ln -s "$mount/outside/owner" "$lock/owner"
        ! kubectl_release_remote_attempt test-ns helper 1234abcd "$nonce"
        [[ -L "$lock/owner" && -d "$run" ]]
        """)
    assert result.returncode == 0, result.stderr


def test_intended_reservation_does_not_touch_competing_pvc_owner(
    tmp_path: Path,
) -> None:
    """A losing reservation rolls back without waiting for or deleting its winner."""
    mount = tmp_path / "mount"
    result = _bash(_identity(tmp_path / "state") + f"""
        mount={str(mount)!r}
        storage_root="$mount/.storage-scale-test"
        canonical="$storage_root/locks/kubernetes-elbencho-sweep"
        losing_run="$storage_root/runs/1234abcd"
        mkdir -p "$canonical" "$storage_root/runs"
        printf 'deadbeef\t11111111111111111111111111111111\n' > "$canonical/owner"
        kubectl_attempt_journal_remote_reservation "$root" "$fd" 1234abcd \
          0123456789abcdef0123456789abcdef
        kubectl_pvc_exec() {{
            script="$5"
            script=$(sed "s#/mnt/storage-scale-test#$mount#g" <<< "$script")
            lock_arg=$(sed "s#/mnt/storage-scale-test#$mount#g" <<< "$7")
            run_arg=$(sed "s#/mnt/storage-scale-test#$mount#g" <<< "$8")
            bash -ceu "$script" bash "$lock_arg" "$run_arg" "$9" "${{10}}" \
                "${{11}}"
        }}
        kubectl_release_journaled_remote_attempt "$root" "$fd" test-ns helper \
            1234abcd
        [[ $(cat "$canonical/owner") == \
            $'deadbeef\t11111111111111111111111111111111' ]]
        [[ ! -e "$losing_run" ]]
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
        kubectl_discover_candidate_nodes() {{
            test -s {str(tmp_path)!r}/kubernetes/current-attempt
            printf current >> "$events"
            printf 'node-a\\tuid-a\\tamd64\\nnode-b\\tuid-b\\tamd64\\n' > "$2"
            printf nodes >> "$events"
        }}
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
        [[ $(cat "$events") == currentnodeshelperpathsjournalreserveacquiredinitializepolicyworkersendpoints ]]
        """)
    assert result.returncode == 0, result.stderr


def test_prepare_failure_terminalizes_only_after_successful_rollback(
    tmp_path: Path,
) -> None:
    """A failed pre-Job handoff becomes SUBMISSION_FAILED after exact cleanup."""
    result = _bash(f"""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.1-11
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        declare -A mapped=([/mnt/storage-scale-test/bench]=1)
        events={str(tmp_path / 'events')!r}
        kubectl_validate_cluster_identity() {{ printf 'namespace-uid\\tpv-uid\\tpvc-uid\\n'; }}
        kubectl_discover_candidate_nodes() {{ printf 'node-a\\tuid-a\\tamd64\\n' > "$2"; }}
        kubectl_choose_coordinator_node() {{ printf node-a; }}
        kubectl_create_helper_pod() {{ printf -v "$1" uid-transfer; }}
        kubectl_validate_pvc_paths() {{ :; }}
        kubectl_attempt_journal_remote_reservation() {{ :; }}
        kubectl_reserve_remote_attempt() {{ :; }}
        kubectl_attempt_mark_remote_reservation_acquired() {{ :; }}
        kubectl_initialize_remote_control_tree() {{ :; }}
        kubectl_create_attempt_policies() {{ printf policies >> "$events"; return 1; }}
        kubectl_release_journaled_remote_attempt() {{ printf release >> "$events"; }}
        kubectl_cleanup_journaled_resources() {{ printf cleanup >> "$events"; }}
        attempt=
        ! kubectl_prepare_attempt_lifecycle attempt {str(tmp_path)!r} 1 mapped
        attempt=$(cat {str(tmp_path)!r}/kubernetes/current-attempt)
        kubectl_attempt_load_metadata {str(tmp_path)!r}/kubernetes/attempts/"$attempt"
        [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]
        [[ $(cat "$events") == policiesreleasecleanup ]]
        """)
    assert result.returncode == 0, result.stderr


def test_prepare_rollback_failure_keeps_prepared_attempt_recoverable(
    tmp_path: Path,
) -> None:
    """A failed release leaves the durable PREPARED pointer for retry."""
    result = _bash(f"""
        export KUBECTL_NAMESPACE=test-ns KUBECTL_PV=test-pv KUBECTL_PVC=test-pvc
        export KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.1-11
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000 KUBECTL_RUN_AS_GROUP=2000
        declare -A mapped=([/mnt/storage-scale-test/bench]=1)
        events={str(tmp_path / 'events')!r}
        kubectl_validate_cluster_identity() {{ printf 'namespace-uid\\tpv-uid\\tpvc-uid\\n'; }}
        kubectl_discover_candidate_nodes() {{ printf 'node-a\\tuid-a\\tamd64\\n' > "$2"; }}
        kubectl_choose_coordinator_node() {{ printf node-a; }}
        kubectl_create_helper_pod() {{ printf -v "$1" uid-transfer; }}
        kubectl_validate_pvc_paths() {{ :; }}
        kubectl_attempt_journal_remote_reservation() {{ :; }}
        kubectl_reserve_remote_attempt() {{ :; }}
        kubectl_attempt_mark_remote_reservation_acquired() {{ :; }}
        kubectl_initialize_remote_control_tree() {{ :; }}
        kubectl_create_attempt_policies() {{ printf policies >> "$events"; return 1; }}
        kubectl_release_journaled_remote_attempt() {{ printf release >> "$events"; return 1; }}
        kubectl_cleanup_journaled_resources() {{ printf cleanup >> "$events"; }}
        attempt=
        ! kubectl_prepare_attempt_lifecycle attempt {str(tmp_path)!r} 1 mapped
        attempt=$(cat {str(tmp_path)!r}/kubernetes/current-attempt)
        kubectl_attempt_load_metadata {str(tmp_path)!r}/kubernetes/attempts/"$attempt"
        [[ "$KUBECTL_LIFECYCLE_STATE" == PREPARED ]]
        [[ $(cat "$events") == policiesreleasecleanup ]]
        """)
    assert result.returncode == 0, result.stderr


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
        kubectl_validate_runtime_configuration() {{ :; }}
        kubectl_map_test_dirs() {{ :; }}
        kubectl_prepare_attempt_lifecycle() {{ printf -v "$1" 1234abcd; }}
        kubectl_prepare_control_bundle() {{ return 1; }}
        kubectl_cleanup_journaled_resources() {{ printf cleanup >> "$events"; }}
        ! kubectl_submit_sweep {str(results)!r} 1
        kubectl_attempt_load_metadata "$root/attempts/1234abcd"
        [[ "$KUBECTL_LIFECYCLE_STATE" == SUBMISSION_FAILED ]]
        [[ $(cat "$events") == cleanup ]]
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
    """A partial control transfer is removed before any coordinator can run."""
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
        ! kubectl_submit_sweep {str(results)!r} 1
        [[ $(cat "$events") == upload-quiesce-diagnostics-release-cleanup- ]]
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
    """A clean B rollback leaves collected A discoverable for the next resume."""
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
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.1-11
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
        first=
        ! kubectl_prepare_attempt_lifecycle first {str(results)!r} 1 mapped '' 1234abcd
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
        export KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.1-11
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
        kubectl_release_journaled_remote_attempt() {{ return 1; }}
        kubectl_cleanup_journaled_resources() {{ :; }}
        first=
        ! kubectl_prepare_attempt_lifecycle first {str(results)!r} 1 mapped '' 1234abcd
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
    """Rollback discovers and deletes an object whose UID journal was interrupted."""
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
    """A delayed accepted create remains discoverable before intent removal."""
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
    """Rollback keeps diagnostic identity after its bounded absence proof."""
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


def test_remote_lock_is_published_only_after_ownership() -> None:
    """The canonical PVC lock never exists without its recovery identity."""
    source = _FUNCTIONS.read_text(encoding="utf-8")
    body = source.split("kubectl_reserve_remote_attempt() {", 1)[1].split(
        "\n}\n\nkubectl_attempt_journal_remote_reservation", 1
    )[0]
    assert 'printf "%s\\\\t%s\\\\n" "$attempt" "$nonce" > "$pending/owner"' in body
    assert body.index('> "$pending/owner"') < body.index(
        'mv -T -n -- "$pending" "$lock"'
    )


def test_cancel_retries_after_job_delete_was_already_journaled(
    tmp_path: Path,
) -> None:
    """A crash after delete-sweep publication cannot strand cancellation."""
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
    """Resume publication is a compare-and-swap against the collected attempt."""
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
        kubectl_attempt_journal_remote_reservation() {{ :; }}
        kubectl_reserve_remote_attempt() {{ :; }}
        kubectl_attempt_mark_remote_reservation_acquired() {{ :; }}
        kubectl_initialize_remote_control_tree() {{ :; }}
        kubectl_create_attempt_policies() {{ :; }}
        kubectl_create_worker_daemonset() {{ :; }}
        kubectl_wait_worker_endpoints() {{ : > "$4"; }}
        first=
        kubectl_prepare_attempt_lifecycle first {str(results)!r} 1 mapped '' 1234abcd
        test "$first" = aaaabbbb
        second=
        ! kubectl_prepare_attempt_lifecycle second {str(results)!r} 1 mapped '' 1234abcd
        test "$(kubectl_attempt_current_id "$root")" = aaaabbbb
        """)
    assert result.returncode == 0, result.stderr


def test_submission_prints_status_cancel_and_collect_commands(tmp_path: Path) -> None:
    """Every asynchronous control operation is copy-pasteable after submit."""
    results = tmp_path / "results with spaces"
    results.mkdir()
    result = _bash(f"kubectl_emit_lifecycle_commands {str(results)!r}")
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_STATUS_COMMAND=" in result.stdout
    assert "STORAGE_SCALE_TEST_CANCEL_COMMAND=" in result.stdout
    assert "STORAGE_SCALE_TEST_COLLECT_COMMAND=" in result.stdout


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


def test_public_status_recovers_coordinator_loss_before_running(
    tmp_path: Path,
) -> None:
    """A failed exact Job makes a PREPARED PVC ledger collectible."""
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
        _kubectl_create_inspector() {{ printf -v "$1" helper; printf -v "$2" uid; }}
        kubectl_read_remote_status() {{
            printf x >> "$calls"
            [[ $(wc -c < "$calls") -eq 1 ]] && printf PREPARED || printf FAILED
        }}
        kubectl_job_terminal_state() {{ printf -v "$1" FAILED; }}
        _kubectl_verify_saved_worker_endpoints() {{ :; }}
        kubectl_preserve_storage_failure_diagnostics() {{ printf diagnose- >> "$events"; }}
        kubectl_recover_lost_coordinator() {{ printf recover- >> "$events"; }}
        _kubectl_remove_inspector() {{ printf remove- >> "$events"; }}
        kubectl_emit_state() {{ printf 'STATE=%s\n' "$1"; }}
        kubectl_lifecycle_operation status {str(results)!r}
        [[ $(cat "$events") == diagnose-recover-remove- ]]
    """)
    assert result.returncode == 0, result.stderr
    assert "STATE=FAILED" in result.stdout


def test_completed_job_with_nonterminal_ledger_is_diagnosed(
    tmp_path: Path,
) -> None:
    """Contradictory Job and PVC evidence never degrades to a generic error."""
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
        _kubectl_create_inspector() {{ printf -v "$1" helper; printf -v "$2" uid; }}
        kubectl_read_remote_status() {{ printf RUNNING; }}
        kubectl_job_terminal_state() {{ printf -v "$1" COMPLETE; }}
        _kubectl_verify_saved_worker_endpoints() {{ :; }}
        _kubectl_remove_inspector() {{ :; }}
        ! kubectl_lifecycle_operation status {str(results)!r}
    """)
    assert result.returncode == 0, result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=LEDGER_INCONSISTENT" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_EXPECTED_UID=job-uid" in result.stderr


def test_missing_worker_daemonset_emits_identity_diagnostics(tmp_path: Path) -> None:
    """External worker deletion fails closed with an actionable diagnosis."""
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
