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

"""Multi-segment (several namespaces, several claims) Kubernetes lifecycle.

A segment is one namespace: its Pods mount every claim of the namespace, and
every claim has its own Lease. The attempt in these tests has two segments:
ns-a with the claims pvc-a and pvc-a2 (an aggregate target) and ns-b with the
claim pvc-b. These tests drive the real lifecycle functions against a fake
cluster made of function overrides that log every external effect, in the
style of test_elbencho_kubectl_lifecycle_shell.py.
"""

from pathlib import Path
import subprocess
import textwrap

import yaml

from tests.kubectl_segment_test_helpers import (
    ATTEMPT as _ATTEMPT,
    HELPER as _HELPER,
    NONCE as _NONCE,
    REPOSITORY_ROOT as _ROOT,
    SEQUENCER_IMAGE as _SEQUENCER_IMAGE,
    bash_with_layout,
    events as _events,
    fake_cluster as _fake_cluster,
    keys as _keys,
    local_state as _local_state,
    publication as _publication,
    steps as _steps,
)

_UID0 = "00000000-0000-0000-0000-000000000000"
# ":" sorts after "2", so pvc-a2 holds the lexically first root of ns-a.
_ROOT_A = "ns-a/pvc-a2:benchmark"
_ROOT_B = "ns-b/pvc-b:benchmark"
_CLAIM_TUPLES = (
    "ns-a nsuid-a pv-a pvuid-a pvc-a pvcuid-a "
    "ns-a nsuid-a pv-a2 pvuid-a2 pvc-a2 pvcuid-a2 "
    "ns-b nsuid-b pv-b pvuid-b pvc-b pvcuid-b"
)


def _bash(body: str) -> subprocess.CompletedProcess[str]:
    """Run BODY after loading the lifecycle helpers and segment 0's layout."""
    return bash_with_layout(body, _ROOT_A)


def _two_segment_attempt(root: Path, transitions: str = "SUBMITTED") -> str:
    """Publish a submitted two-segment attempt with every journal it owns."""
    return textwrap.dedent(f"""\
        root={str(root)!r}
        kubectl_local_lock_acquire "$root" fd
        kubectl_attempt_create_identity "$root" "$fd" {_ATTEMPT} {_NONCE} \\
            {_CLAIM_TUPLES}
        kubectl_attempt_write_state "$root" "$fd" {_ATTEMPT} PREPARED
        for next in {transitions}; do
            kubectl_attempt_transition "$root" "$fd" {_ATTEMPT} "$next"
        done
        kubectl_attempt_write_current "$root" "$fd" {_ATTEMPT}
        metadata="$root/attempts/{_ATTEMPT}"
        {{
            printf 'KUBECTL_CONTROL_LOGICAL_ROOT={_ROOT_A}\\n'
            printf 'KUBECTL_CONTROL_TEST_ROOT=/mnt/storage-scale-test/pvc-a2/benchmark\\n'
            printf 'KUBECTL_CONTROL_ROOT=/mnt/storage-scale-test/pvc-a2/benchmark/.storage-scale-test\\n'
            printf 'declare -ga KUBECTL_SEGMENT_CONTROL_ROOTS=( {_ROOT_A} {_ROOT_B} )\\n'
        }} > "$metadata/configuration.sh"
        KUBECTL_SEGMENT_CONTROL_ROOTS=({_ROOT_A} {_ROOT_B})
        printf 'node-a\\tuid-a\\tamd64\\n' > "$metadata/nodes.tsv"
        journal() {{
            kubectl_attempt_journal_resource "$root" "$fd" {_ATTEMPT} "$1" "$2" "$3" \\
                "$4" "uid-$3" {_NONCE}
        }}
        journal sweep Job sst-elb-{_ATTEMPT}-sweep ns-a
        journal sweep-s1 Job sst-elb-{_ATTEMPT}-s1-sweep ns-b
        journal workers DaemonSet sst-elb-{_ATTEMPT}-workers ns-a
        journal workers-s1 DaemonSet sst-elb-{_ATTEMPT}-s1-workers ns-b
        journal worker-net NetworkPolicy sst-elb-{_ATTEMPT}-worker-net ns-a
        journal coord-net NetworkPolicy sst-elb-{_ATTEMPT}-coord-net ns-a
        journal worker-net-s1 NetworkPolicy sst-elb-{_ATTEMPT}-s1-worker-net ns-b
        journal coord-net-s1 NetworkPolicy sst-elb-{_ATTEMPT}-s1-coord-net ns-b
        journal sequencer-sa ServiceAccount sst-elb-{_ATTEMPT}-sequencer ns-a
        journal sequencer-role-s0 Role sst-elb-{_ATTEMPT}-sequencer ns-a
        journal sequencer-binding-s0 RoleBinding sst-elb-{_ATTEMPT}-sequencer ns-a
        journal sequencer-role-s1 Role sst-elb-{_ATTEMPT}-s1-sequencer ns-b
        journal sequencer-binding-s1 RoleBinding sst-elb-{_ATTEMPT}-s1-sequencer ns-b
        journal sequencer Job sst-elb-{_ATTEMPT}-sequencer ns-a
        for claim in 0 1 2; do
            key=$(kubectl_claim_lease_key "$claim")
            kubectl_attempt_journal_resource "$root" "$fd" {_ATTEMPT} "$key" Lease \\
                "${{KUBECTL_CLAIM_LEASE_NAMES[claim]}}" \\
                "${{KUBECTL_CLAIM_NAMESPACES[claim]}}" "uid-$key" {_NONCE}
        done
        for segment in 0 1; do
            kubectl_with_segment "$segment" kubectl_attempt_journal_remote_reservation \\
                "$root" "$fd" {_ATTEMPT} {_NONCE}
            kubectl_with_segment "$segment" kubectl_attempt_mark_remote_reservation_acquired \\
                "$root" "$fd" {_ATTEMPT}
        done
        mkdir -p "$metadata/control-bundle/executions" "$metadata/control-bundle-s1/executions"
        for id in 0001 0003; do printf 'export nodes=1\\n' > "$metadata/control-bundle/executions/$id.sh"; done
        for id in 0002 0004; do printf 'export nodes=1\\n' > "$metadata/control-bundle-s1/executions/$id.sh"; done
        kubectl_local_lock_release "$fd"
        results={str(root.parent)!r}
        """)


def _write_plan(plan: Path) -> None:
    """The segment plan kubectl_plan_segments writes for the two namespaces."""
    plan.mkdir()
    (plan / "segments.tsv").write_text(
        f"0\tns-a\t{_ROOT_A}\tpvc-a,pvc-a2\n1\tns-b\t{_ROOT_B}\tpvc-b\n",
        encoding="utf-8",
    )
    roots = {
        0: [
            "/mnt/storage-scale-test/pvc-a/benchmark",
            "/mnt/storage-scale-test/pvc-a2/benchmark",
        ],
        1: ["/mnt/storage-scale-test/pvc-b/benchmark"],
    }
    for segment, paths in roots.items():
        text = "\n".join(paths) + "\n"
        (plan / f"segment-{segment}.roots").write_text(text, encoding="utf-8")
        (plan / f"segment-{segment}.paths").write_text(text, encoding="utf-8")


def _prepare(tmp_path: Path, environment: str = "", extra: str = ""):
    results = tmp_path / "results"
    results.mkdir()
    plan = tmp_path / "plan"
    _write_plan(plan)
    body = (
        _fake_cluster(tmp_path)
        + f"results={str(results)!r}\nroot=$results/kubernetes\n"
        + environment
        + "attempt=\n"
        + f'kubectl_prepare_attempt_lifecycle attempt "$results" 2 {str(plan)!r} ""\n'
        + extra
    )
    return _bash(body), results / "kubernetes"


def _upload(attempt: str = _ATTEMPT, segment: int = 0) -> str:
    infix = "" if segment == 0 else f"-s{segment}"
    return f"sst-elb-{attempt}{infix}-upload-{_HELPER}"


def test_two_segment_prepare_orders_claims_leases_run_trees_and_workers(tmp_path):
    """Prepare walks every segment in order through each phase, one Lease per claim."""
    result, state = _prepare(tmp_path)
    assert result.returncode == 0, result.stderr
    events = _events(tmp_path)
    paths_a = (
        "/mnt/storage-scale-test/pvc-a/benchmark "
        "/mnt/storage-scale-test/pvc-a2/benchmark"
    )
    paths_b = "/mnt/storage-scale-test/pvc-b/benchmark"
    assert events == [
        "identity ns-a pvc-a pvc-a2",
        "identity ns-b pvc-b",
        "nodes",
        f"create Pod ns-a/{_upload()} transfer",
        f"paths ns-a {_upload()} {paths_a}",
        f"access ns-a {_upload()} {paths_a}",
        f"create Pod ns-b/{_upload(segment=1)} transfer-s1",
        f"paths ns-b {_upload(segment=1)} {paths_b}",
        f"access ns-b {_upload(segment=1)} {paths_b}",
        "lease ns-a/pvc-a pvc-lease",
        "lease ns-a/pvc-a2 pvc-lease-c1",
        "lease ns-b/pvc-b pvc-lease-c2",
        f"reserve ns-a {_upload()} {_ROOT_A}",
        f"control ns-a {_upload()}",
        f"reserve ns-b {_upload(segment=1)} {_ROOT_B}",
        f"control ns-b {_upload(segment=1)}",
        f"create NetworkPolicy ns-a/sst-elb-{_ATTEMPT}-worker-net worker-net",
        f"create NetworkPolicy ns-a/sst-elb-{_ATTEMPT}-coord-net coord-net",
        f"create DaemonSet ns-a/sst-elb-{_ATTEMPT}-workers workers",
        f"create NetworkPolicy ns-b/sst-elb-{_ATTEMPT}-s1-worker-net worker-net-s1",
        f"create NetworkPolicy ns-b/sst-elb-{_ATTEMPT}-s1-coord-net coord-net-s1",
        f"create DaemonSet ns-b/sst-elb-{_ATTEMPT}-s1-workers workers-s1",
        "endpoints ns-a",
        "endpoints ns-b",
    ]
    metadata = state / "attempts" / _ATTEMPT
    for name in (
        "worker-endpoints.tsv",
        "worker-endpoints-s1.tsv",
        "remote-reservation.sh",
        "remote-reservation-s1.sh",
    ):
        assert (metadata / name).exists(), name
    assert "KUBECTL_REMOTE_TEST_ROOT=/mnt/storage-scale-test/pvc-b/benchmark" in (
        metadata / "remote-reservation-s1.sh"
    ).read_text(encoding="utf-8")
    identity = yaml.safe_load((metadata / "identity.yaml").read_text(encoding="utf-8"))
    assert identity["schema"] == 4
    assert [segment["namespace"] for segment in identity["segments"]] == [
        "ns-a",
        "ns-b",
    ]
    assert [claim["claim"] for claim in identity["segments"][0]["claims"]] == [
        "pvc-a",
        "pvc-a2",
    ]
    assert identity["segments"][0]["claims"][1]["pv"] == "pv-a2"
    configuration = (metadata / "configuration.sh").read_text(encoding="utf-8")
    assert f"KUBECTL_SEGMENT_CONTROL_ROOTS=( {_ROOT_A} {_ROOT_B} )" in configuration
    assert "KUBECTL_PVC" not in configuration
    assert _local_state(state) == "PREPARED"
    manifests = tmp_path / "manifests"
    daemonset = yaml.safe_load(
        (manifests / f"DaemonSet.ns-a.sst-elb-{_ATTEMPT}-workers.yaml").read_text()
    )
    pod = daemonset["spec"]["template"]["spec"]
    assert [v["persistentVolumeClaim"]["claimName"] for v in pod["volumes"]] == [
        "pvc-a",
        "pvc-a2",
    ]
    assert pod["containers"][0]["volumeMounts"] == [
        {"name": "claim-0", "mountPath": "/mnt/storage-scale-test/pvc-a"},
        {"name": "claim-1", "mountPath": "/mnt/storage-scale-test/pvc-a2"},
    ]
    other = yaml.safe_load(
        (manifests / f"DaemonSet.ns-b.sst-elb-{_ATTEMPT}-s1-workers.yaml").read_text()
    )
    assert (
        other["spec"]["selector"]["matchLabels"][
            "storage-scale-test.nvidia.com/segment"
        ]
        == "1"
    )
    volumes = other["spec"]["template"]["spec"]["volumes"]
    assert [v["persistentVolumeClaim"]["claimName"] for v in volumes] == ["pvc-b"]
    transfer = yaml.safe_load(
        (manifests / f"Pod.ns-a.{_upload()}.yaml").read_text(encoding="utf-8")
    )
    assert [
        v["persistentVolumeClaim"]["claimName"] for v in transfer["spec"]["volumes"]
    ] == [
        "pvc-a",
        "pvc-a2",
    ]
    assert transfer["spec"]["containers"][0]["env"][0]["value"] == (
        f"/mnt/storage-scale-test/pvc-a2/benchmark/.storage-scale-test/runs/{_ATTEMPT}"
    )


def _assert_rolled_back(state: Path) -> set[str]:
    """Every journaled object was deleted and every Lease released."""
    metadata = state / "attempts" / _ATTEMPT
    steps = _steps(state)
    for resource in (metadata / "resources").glob("*.sh"):
        key = resource.stem
        expected = f"release-{key}" if key.startswith("pvc-lease") else f"delete-{key}"
        assert expected in steps, (key, sorted(steps))
    assert {
        "release-pvc-lease",
        "release-pvc-lease-c1",
        "release-pvc-lease-c2",
    } <= steps
    assert _local_state(state) == "SUBMISSION_FAILED"
    return steps


def test_unready_target_rolls_back_both_namespaces_and_leases_in_reverse(tmp_path):
    """[S-21] One segment's workers never become Ready: nothing remains anywhere."""
    result, state = _prepare(tmp_path, "UNREADY_NAMESPACE=ns-b\n", "! true\n")
    assert result.returncode != 0
    events = _events(tmp_path)
    tail = events[events.index("endpoints ns-b") + 1 :]
    assert tail[:3] == [
        f"delete DaemonSet ns-b/sst-elb-{_ATTEMPT}-s1-workers",
        f"delete DaemonSet ns-a/sst-elb-{_ATTEMPT}-workers",
        "diagnostics",
    ]
    assert f"exec ns-a {_upload()}" in tail
    assert f"exec ns-b {_upload(segment=1)}" in tail
    assert (
        tail.index("lease-delete pvc-lease-c2")
        < tail.index("lease-delete pvc-lease-c1")
        < tail.index("lease-delete pvc-lease")
    )
    for namespace in ("ns-a", "ns-b"):
        assert any(line.startswith(f"delete Pod {namespace}/") for line in tail)
        assert any(
            line.startswith(f"delete NetworkPolicy {namespace}/") for line in tail
        )
    steps = _assert_rolled_back(state)
    assert {"release-remote-run", "release-remote-run-s1"} <= steps
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_PHASE=worker-ready" in result.stderr


def test_competitor_on_a_later_lease_releases_only_earlier_leases(tmp_path):
    """[S-20] A competitor holds the last claim's Lease: only earlier ones roll back."""
    result, state = _prepare(tmp_path, "HELD_LEASE=pvc-b\n")
    assert result.returncode != 0
    events = _events(tmp_path)
    assert "lease ns-a/pvc-a pvc-lease" in events
    assert "lease ns-a/pvc-a2 pvc-lease-c1" in events
    assert "lease-held ns-b/pvc-b" in events
    assert events.index("lease-delete pvc-lease-c1") < events.index(
        "lease-delete pvc-lease"
    )
    assert "lease-delete pvc-lease-c2" not in events
    assert not any(line.startswith(("reserve ", "control ")) for line in events)
    _assert_rolled_back(state)


def test_aliased_claims_fail_at_the_second_run_tree_reservation(tmp_path):
    """[S-22] A segment whose control root reaches an earlier one's run tree is named."""
    result, state = _prepare(tmp_path, "ALIASED_NAMESPACE=ns-b\n")
    assert result.returncode != 0
    assert "may alias the storage of an earlier segment's claim" in result.stderr
    assert f"control root {_ROOT_B} of segment 1 (namespace ns-b)" in result.stderr
    events = _events(tmp_path)
    assert f"control ns-b {_upload(segment=1)}" not in events
    assert f"exec ns-a {_upload()}" in events
    assert f"exec ns-b {_upload(segment=1)}" in events
    _assert_rolled_back(state)


def test_exit_between_leases_is_rolled_back_by_a_later_status(tmp_path):
    """[S-19] A submitter killed after the first Lease leaves a recoverable attempt."""
    state = tmp_path / "results" / "kubernetes"
    result = _bash(_fake_cluster(tmp_path) + textwrap.dedent(f"""\
            root={str(state)!r}
            mkdir -p "$root"
            kubectl_local_lock_acquire "$root" fd
            kubectl_attempt_create_identity "$root" "$fd" {_ATTEMPT} {_NONCE} \\
                {_CLAIM_TUPLES}
            kubectl_attempt_write_state "$root" "$fd" {_ATTEMPT} PREPARED
            kubectl_attempt_write_current "$root" "$fd" {_ATTEMPT}
            {{
                printf 'KUBECTL_CONTROL_LOGICAL_ROOT={_ROOT_A}\\n'
                printf 'KUBECTL_CONTROL_TEST_ROOT=/mnt/storage-scale-test/pvc-a2/benchmark\\n'
                printf 'KUBECTL_CONTROL_ROOT=/mnt/storage-scale-test/pvc-a2/benchmark/.storage-scale-test\\n'
                printf 'declare -ga KUBECTL_SEGMENT_CONTROL_ROOTS=( {_ROOT_A} {_ROOT_B} )\\n'
            }} > "$root/attempts/{_ATTEMPT}/configuration.sh"
            kubectl_attempt_journal_resource "$root" "$fd" {_ATTEMPT} transfer Pod \\
                {_upload()} ns-a uid-upload {_NONCE}
            kubectl_attempt_journal_resource "$root" "$fd" {_ATTEMPT} transfer-s1 Pod \\
                {_upload(segment=1)} ns-b uid-upload-s1 {_NONCE}
            kubectl_with_claim 0 kubectl_acquire_pvc_lease "$root" "$fd" {_ATTEMPT}
            kubectl_local_lock_release "$fd"
            : > "$events"
            kubectl_lifecycle_operation status {str(state.parent)!r}
            """))
    assert result.returncode == 0, result.stderr
    assert "STATE=SUBMISSION_FAILED" in result.stdout
    events = _events(tmp_path)
    assert events[:2] == ["verify-identity ns-a", "verify-identity ns-b"]
    assert f"delete Pod ns-a/{_upload()}" in events
    assert f"delete Pod ns-b/{_upload(segment=1)}" in events
    assert events[-1] == "lease-delete pvc-lease"
    _assert_rolled_back(state)


_GROUPS = (
    ("0001", "io", "elbencho", "ns-a", '["pvc-a:benchmark"]=2 ["pvc-a2:benchmark"]=1'),
    ("0002", "io", "elbencho", "ns-a", '["ns-b/pvc-b:benchmark"]=1'),
    ("0003", "mdtest", "mdtest-elbencho", "ns-a", '["pvc-a:benchmark"]=1'),
    ("0004", "mdtest", "mdtest-elbencho", "ns-b", '["pvc-b:benchmark"]=1'),
)


def _write_batch(results: Path) -> None:
    """A sealed batch: groups 0001/0003 in ns-a (0001 an aggregate over two
    claims), groups 0002/0004 in ns-b. Group 0002 names its namespace in its
    key; group 0004 relies on its KUBECTL_NAMESPACE."""
    rows = ["version\t1", "revision\t4", "datestamp\t20260101Z000000"]
    (results / "executions").mkdir()
    for group, kind, basename, namespace, roots in _GROUPS:
        relative = f"groups/{group}/{basename}-20260101Z000000"
        (results / relative).mkdir(parents=True)
        (results / relative / "env_used.sh").write_text(
            f"export KUBECTL_NAMESPACE={namespace}\n"
            f"unset TEST_DIRS\ndeclare -gA TEST_DIRS=({roots})\n",
            encoding="utf-8",
        )
        rows.append(f"group\t{group}\t{kind}\t{relative}\tx\ty")
        first = roots.split('"')[1]
        (results / "executions" / f"{group}.sh").write_text(
            "export nodes=1\n"
            f"export ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV={first}/{basename}-target-1\n",
            encoding="utf-8",
        )
    for group, *_ in _GROUPS:
        rows.append(f"execution\t{group}\t{group}\th")
    (results / "batch-manifest.tsv").write_text("\n".join(rows) + "\n")


_SUBMIT = """
    KUBECTL_NAMESPACE=ns-a
    declare -A TEST_DIRS=(
        [ns-a/pvc-a:benchmark]=1 [ns-a/pvc-a2:benchmark]=1 [ns-b/pvc-b:benchmark]=1
    )
    kubectl_populate_sweep_control_bundle() {
        event "populate ${1##*/} $(paste -sd, "$5") ${6//$'\\t'/|}"
        printf x > "$1/payload"
    }
    kubectl_upload_control_bundle() { event "upload $1 $2 ${4##*/}"; }
"""


def _submit(tmp_path: Path, environment: str = ""):
    results = tmp_path / "filesystem-batch-20260101Z000000"
    results.mkdir()
    _write_batch(results)
    return _submit_existing(tmp_path, results, environment)


def _submit_existing(tmp_path: Path, results: Path, environment: str = ""):
    body = (
        _fake_cluster(tmp_path)
        + f"results={str(results)!r}\nroot=$results/kubernetes\n"
        + environment
        + _SUBMIT
        + 'kubectl_submit_sweep "$results" 1\n'
    )
    return _bash(body), results / "kubernetes"


def test_batch_segments_follow_namespaces_in_ledger_order(tmp_path):
    """One segment per namespace, ordered by first execution; claims pooled."""
    results = tmp_path / "filesystem-batch-20260101Z000000"
    results.mkdir()
    _write_batch(results)
    plan = tmp_path / "plan"
    plan.mkdir()
    result = _bash(
        _fake_cluster(tmp_path)
        + f"kubectl_plan_segments {str(results)!r} {str(plan)!r}\n"
    )
    assert result.returncode == 0, result.stderr
    assert (plan / "segments.tsv").read_text() == (
        f"0\tns-a\t{_ROOT_A}\tpvc-a,pvc-a2\n1\tns-b\t{_ROOT_B}\tpvc-b\n"
    )
    assert (plan / "segment-0.ids").read_text() == "0001\n0003\n"
    assert (plan / "segment-1.ids").read_text() == "0002\n0004\n"
    assert (plan / "segment-0.roots").read_text().splitlines() == [
        "/mnt/storage-scale-test/pvc-a/benchmark",
        "/mnt/storage-scale-test/pvc-a2/benchmark",
    ]
    assert (
        "/mnt/storage-scale-test/pvc-b/benchmark/elbencho-target-1"
        in (plan / "segment-1.paths").read_text().splitlines()
    )


def test_two_segment_submit_creates_suspended_jobs_then_the_sequencer(tmp_path):
    """Segment bundles, suspended Jobs, and the sequencer with exact Job UIDs."""
    result, state = _submit(tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"STORAGE_SCALE_TEST_ATTEMPT_ID={_ATTEMPT}" in result.stdout
    events = _events(tmp_path)
    assert events[:2] == ["identity ns-a pvc-a pvc-a2", "identity ns-b pvc-b"]
    submit = events[events.index("endpoints ns-b") + 1 :]
    sequencer = f"sst-elb-{_ATTEMPT}-sequencer"
    assert submit == [
        "populate control-bundle 0001,0003 0|ns-a",
        f"upload ns-a {_upload()} control-bundle",
        f"delete Pod ns-a/{_upload()}",
        "populate control-bundle-s1 0002,0004 1|ns-b",
        f"upload ns-b {_upload(segment=1)} control-bundle-s1",
        f"delete Pod ns-b/{_upload(segment=1)}",
        f"create Job ns-a/sst-elb-{_ATTEMPT}-sweep sweep",
        f"create Job ns-b/sst-elb-{_ATTEMPT}-s1-sweep sweep-s1",
        f"create ServiceAccount ns-a/{sequencer} sequencer-sa",
        f"create Role ns-a/{sequencer} sequencer-role-s0",
        f"create RoleBinding ns-a/{sequencer} sequencer-binding-s0",
        f"create Role ns-b/sst-elb-{_ATTEMPT}-s1-sequencer sequencer-role-s1",
        f"create RoleBinding ns-b/sst-elb-{_ATTEMPT}-s1-sequencer sequencer-binding-s1",
        f"create Job ns-a/{sequencer} sequencer",
    ]
    assert _local_state(state) == "SUBMITTED"
    manifests = tmp_path / "manifests"
    for namespace, name, claims, root in (
        ("ns-a", f"sst-elb-{_ATTEMPT}-sweep", ["pvc-a", "pvc-a2"], "pvc-a2/benchmark"),
        ("ns-b", f"sst-elb-{_ATTEMPT}-s1-sweep", ["pvc-b"], "pvc-b/benchmark"),
    ):
        job = yaml.safe_load((manifests / f"Job.{namespace}.{name}.yaml").read_text())
        assert job["spec"]["suspend"] is True
        pod = job["spec"]["template"]["spec"]
        assert [
            v["persistentVolumeClaim"]["claimName"]
            for v in pod["volumes"]
            if "persistentVolumeClaim" in v
        ] == claims
        assert (
            f"/mnt/storage-scale-test/{root}/.storage-scale-test/runs/{_ATTEMPT}"
            in (pod["containers"][0]["args"][0])
        )
    role = yaml.safe_load(
        (manifests / f"Role.ns-b.sst-elb-{_ATTEMPT}-s1-sequencer.yaml").read_text()
    )
    assert role["rules"] == [
        {
            "apiGroups": ["batch"],
            "resources": ["jobs"],
            "verbs": ["get", "patch"],
            "resourceNames": [f"sst-elb-{_ATTEMPT}-s1-sweep"],
        }
    ]
    binding = yaml.safe_load(
        (
            manifests / f"RoleBinding.ns-b.sst-elb-{_ATTEMPT}-s1-sequencer.yaml"
        ).read_text()
    )
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": sequencer, "namespace": "ns-a"}
    ]
    job = yaml.safe_load((manifests / f"Job.ns-a.{sequencer}.yaml").read_text())
    assert job["spec"]["template"]["spec"]["containers"][0]["args"] == [
        "ns-a",
        f"sst-elb-{_ATTEMPT}-sweep",
        f"uid-sst-elb-{_ATTEMPT}-sweep",
        "ns-b",
        f"sst-elb-{_ATTEMPT}-s1-sweep",
        f"uid-sst-elb-{_ATTEMPT}-s1-sweep",
    ]
    assert job["spec"]["template"]["spec"]["automountServiceAccountToken"] is True


def test_one_namespace_batch_has_one_segment_and_no_sequencer(tmp_path):
    """[S-27] Several targets in one namespace share one segment: no sequencer."""
    results = tmp_path / "filesystem-batch-20260101Z000000"
    results.mkdir()
    _write_batch(results)
    for relative in (
        "groups/0002/elbencho-20260101Z000000/env_used.sh",
        "groups/0004/mdtest-elbencho-20260101Z000000/env_used.sh",
    ):
        path = results / relative
        path.write_text(
            "export KUBECTL_NAMESPACE=ns-a\nunset TEST_DIRS\n"
            'declare -gA TEST_DIRS=(["pvc-a2:other"]=1)\n',
            encoding="utf-8",
        )
    for group in ("0002", "0004"):
        path = results / "executions" / f"{group}.sh"
        path.write_text(
            path.read_text()
            .replace("ns-b/pvc-b:benchmark", "pvc-a2:other")
            .replace("pvc-b:benchmark", "pvc-a2:other")
        )
    result, state = _submit_existing(
        tmp_path, results, "export KUBECTL_SEQUENCER_IMAGE=\n"
    )
    assert result.returncode == 0, result.stderr
    events = _events(tmp_path)
    assert events[0] == "identity ns-a pvc-a pvc-a2"
    assert "populate control-bundle 0001,0002,0003,0004 0|ns-a" in events
    assert f"create Job ns-a/sst-elb-{_ATTEMPT}-sweep sweep" in events
    assert not any("sequencer" in line or "-s1-" in line for line in events)
    job = yaml.safe_load(
        (tmp_path / "manifests" / f"Job.ns-a.sst-elb-{_ATTEMPT}-sweep.yaml").read_text()
    )
    assert job["spec"]["suspend"] is False
    identity = (state / "attempts" / _ATTEMPT / "identity.sh").read_text()
    assert "KUBECTL_SEGMENT_COUNT=1\n" in identity
    assert "KUBECTL_CLAIM_COUNT=2\n" in identity


def test_sequencer_rbac_denial_rolls_back_every_namespace(tmp_path):
    """[S-23] A Role the cluster refuses rolls the whole submission back."""
    result, state = _submit(tmp_path, "FAIL_CREATE=Role/ns-b\n")
    assert result.returncode != 0
    events = _events(tmp_path)
    denied = events.index(f"create-denied Role ns-b/sst-elb-{_ATTEMPT}-s1-sequencer")
    rollback = events[denied + 1 :]
    assert rollback[:4] == [
        f"delete Job ns-a/sst-elb-{_ATTEMPT}-sweep",
        f"delete Job ns-b/sst-elb-{_ATTEMPT}-s1-sweep",
        f"delete DaemonSet ns-b/sst-elb-{_ATTEMPT}-s1-workers",
        f"delete DaemonSet ns-a/sst-elb-{_ATTEMPT}-workers",
    ]
    assert "inspector ns-a status" in rollback
    assert "inspector ns-b status" in rollback
    assert f"delete ServiceAccount ns-a/sst-elb-{_ATTEMPT}-sequencer" in rollback
    assert f"delete RoleBinding ns-a/sst-elb-{_ATTEMPT}-sequencer" in rollback
    assert (
        rollback.index("lease-delete pvc-lease-c2")
        < rollback.index("lease-delete pvc-lease-c1")
        < rollback.index("lease-delete pvc-lease")
    )
    _assert_rolled_back(state)


def test_single_segment_submission_keeps_todays_attempt(tmp_path):
    """One remaining namespace is an unsuspended Job with no sequencer."""
    results = tmp_path / "filesystem-batch-20260101Z000000"
    results.mkdir()
    _write_batch(results)
    selection = tmp_path / "selection"
    selection.write_text("0002\n0004\n", encoding="utf-8")
    result, _ = _submit_existing(
        tmp_path,
        results,
        f"export KUBECTL_SUBMIT_EXECUTIONS_FILE={str(selection)!r}\n",
    )
    assert result.returncode == 0, result.stderr
    events = _events(tmp_path)
    assert events[:2] == ["identity ns-b pvc-b", "nodes"]
    assert "populate control-bundle 0002,0004 0|ns-b" in events
    assert f"create Job ns-b/sst-elb-{_ATTEMPT}-sweep sweep" in events
    assert not any("sequencer" in line for line in events)
    job = yaml.safe_load(
        (tmp_path / "manifests" / f"Job.ns-b.sst-elb-{_ATTEMPT}-sweep.yaml").read_text()
    )
    assert job["spec"]["suspend"] is False
    identity = (results / "kubernetes/attempts" / _ATTEMPT / "identity.sh").read_text()
    assert "KUBECTL_SEGMENT_COUNT=1\n" in identity
    assert "KUBECTL_SEGMENT_NAMESPACES=( ns-b )" in identity


def test_resume_plans_segments_only_for_namespaces_with_remaining_cells(tmp_path):
    """[R-23] Resume builds segments only for namespaces with remaining cells."""
    results = tmp_path / "filesystem-batch-20260101Z000000"
    results.mkdir()
    _write_batch(results)
    plan = tmp_path / "plan"
    plan.mkdir()
    selection = tmp_path / "selection"
    selection.write_text("0004\n0002\n", encoding="utf-8")
    result = _bash(
        _fake_cluster(tmp_path)
        + f"export KUBECTL_SUBMIT_EXECUTIONS_FILE={str(selection)!r}\n"
        + f"kubectl_plan_segments {str(results)!r} {str(plan)!r}\n"
    )
    assert result.returncode == 0, result.stderr
    assert (plan / "segments.tsv").read_text() == f"0\tns-b\t{_ROOT_B}\tpvc-b\n"
    assert (plan / "segment-0.ids").read_text() == "0002\n0004\n"
    assert not (plan / "segment-1.ids").exists()


def test_group_roots_spanning_namespaces_are_rejected_before_mutation(tmp_path):
    """[S-18] A group whose roots span namespaces never reaches the cluster."""
    results = tmp_path / "filesystem-batch-20260101Z000000"
    results.mkdir()
    _write_batch(results)
    group = results / "groups/0004/mdtest-elbencho-20260101Z000000/env_used.sh"
    group.write_text(
        "export KUBECTL_NAMESPACE=ns-b\nunset TEST_DIRS\n"
        'declare -gA TEST_DIRS=(["pvc-b:x"]=1 ["ns-c/pvc-c:y"]=1)\n',
        encoding="utf-8",
    )
    result, _ = _submit_existing(tmp_path, results)
    assert result.returncode != 0
    assert "TEST_DIRS roots span Kubernetes namespaces" in result.stderr
    assert "batch group 0004 has no valid claim-qualified TEST_DIRS" in result.stderr
    assert not (results / "kubernetes").exists()
    assert _events(tmp_path) == []


def test_invalid_group_root_is_rejected_before_mutation(tmp_path):
    """[S-18] A root that names no claim is rejected, naming the group."""
    results = tmp_path / "filesystem-batch-20260101Z000000"
    results.mkdir()
    _write_batch(results)
    group = results / "groups/0003/mdtest-elbencho-20260101Z000000/env_used.sh"
    group.write_text(
        "export KUBECTL_NAMESPACE=ns-a\nunset TEST_DIRS\n"
        "declare -gA TEST_DIRS=([/bench/fs1]=1)\n",
        encoding="utf-8",
    )
    result, _ = _submit_existing(tmp_path, results)
    assert result.returncode != 0
    assert "must name its claim as [namespace/]claim:path" in result.stderr
    assert "batch group 0003" in result.stderr
    assert not (results / "kubernetes").exists()
    assert _events(tmp_path) == []


def test_segment_bundle_holds_only_its_executions_namespace_and_endpoints(tmp_path):
    """A segment bundle carries its executions, segment.tsv and 4-column endpoints."""
    results = tmp_path / "elbencho-20260101Z000000"
    (results / "executions").mkdir(parents=True)
    for name in ("env_used.sh", "env_used.yaml"):
        (results / name).write_text("# snapshot\n", encoding="utf-8")
    for number in range(1, 5):
        (results / "executions" / f"{number:04d}.sh").write_text("export nodes=1\n")
    endpoints = tmp_path / "endpoints.tsv"
    endpoints.write_text(
        "node-a\tuid-a\tpod-a\tpoduid-a\t10.0.0.1\tamd64\tsha256:x\n", encoding="utf-8"
    )
    ids = tmp_path / "segment.ids"
    ids.write_text("0002\n0004\n", encoding="utf-8")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    result = _bash(f"""
        set -e
        kubectl_populate_sweep_control_bundle {str(bundle)!r} {str(results)!r} \\
            {str(endpoints)!r} '' {str(ids)!r} $'1\\tns-b'
        ! kubectl_populate_sweep_control_bundle {str(tmp_path / 'other')!r} \\
            {str(results)!r} {str(endpoints)!r} '' {str(ids)!r} $'1\\tns-b\\tpv-b\\tpvc-b'
        """)
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in (bundle / "executions").iterdir()) == [
        "0002.sh",
        "0004.sh",
    ]
    assert (bundle / "segment.tsv").read_text() == "1\tns-b\n"
    assert (bundle / "worker-endpoints.tsv").read_text() == (
        "node-a\tpod-a\tpoduid-a\t10.0.0.1\n"
    )
    manifest = (bundle / "bundle-manifest.tsv").read_text()
    assert "\tsegment.tsv\n" in manifest
    assert "executions/0001.sh" not in manifest


def _status(tmp_path: Path, setup: str, operation: str = "status"):
    state = tmp_path / "results" / "kubernetes"
    state.mkdir(parents=True)
    body = (
        _fake_cluster(tmp_path)
        + _two_segment_attempt(state)
        + setup
        + ': > "$events"\n'
        + f'kubectl_lifecycle_operation {operation} "$results"\n'
    )
    return _bash(body), state


def test_status_restarts_a_lost_sequencers_next_segment(tmp_path):
    """[T-11] Segment 0 Complete, Job 1 still suspended: --status starts it."""
    result, _ = _status(
        tmp_path,
        """
        set_fake job ns-a COMPLETE; set_fake job ns-b SUSPENDED
        set_fake remote ns-b PREPARED; set_fake progress ns-b '2 0 0 0'
        """,
    )
    assert result.returncode == 0, result.stderr
    keys = _keys(result.stdout)
    assert keys["STATE"] == "BETWEEN_EXECUTIONS"
    assert keys["NEXT_ACTION"] == "WAIT"
    assert (keys["EXECUTIONS_TOTAL"], keys["EXECUTIONS_SUCCEEDED"]) == ("4", "2")
    assert keys["EXECUTIONS_PENDING"] == "2"
    assert keys["SEGMENT_COUNT"] == "2"
    assert keys["SEGMENT_CURRENT"] == "1"
    assert (keys["SEGMENT_0_NAMESPACE"], keys["SEGMENT_1_NAMESPACE"]) == (
        "ns-a",
        "ns-b",
    )
    assert (keys["SEGMENT_0_STATE"], keys["SEGMENT_1_STATE"]) == ("SUCCESS", "PENDING")
    assert not any(key.startswith("TARGET_") for key in keys)
    events = _events(tmp_path)
    assert events[:5] == [
        "verify-identity ns-a",
        "verify-identity ns-b",
        "verify-lease ns-a/pvc-a",
        "verify-lease ns-a/pvc-a2",
        "verify-lease ns-b/pvc-b",
    ]
    unsuspend = (
        f"unsuspend ns-b/sst-elb-{_ATTEMPT}-s1-sweep uid-sst-elb-{_ATTEMPT}-s1-sweep"
    )
    assert events.index(unsuspend) < events.index("inspector ns-b status")
    assert "inspector ns-a status" not in events


def test_failed_segment_permits_collect_and_never_starts_later_targets(tmp_path):
    """[T-12] A failed segment ends the attempt; later Jobs stay suspended."""
    result, _ = _status(
        tmp_path,
        """
        set_fake job ns-a FAILED; set_fake job ns-b SUSPENDED
        set_fake remote ns-a FAILED; set_fake progress ns-a '0 0 1 1'
        """,
    )
    assert result.returncode == 0, result.stderr
    keys = _keys(result.stdout)
    assert keys["STATE"] == "FAILED"
    assert keys["NEXT_ACTION"] == "COLLECT"
    assert (keys["SEGMENT_0_STATE"], keys["SEGMENT_1_STATE"]) == (
        "FAILED",
        "NOT_STARTED",
    )
    assert (keys["EXECUTIONS_PENDING"], keys["EXECUTIONS_FAILED"]) == ("2", "1")
    assert not any(line.startswith("unsuspend") for line in _events(tmp_path))


def test_all_complete_segments_report_success_without_an_inspector(tmp_path):
    """Every Job Complete: SUCCESS from local segment counts, ready to collect."""
    result, _ = _status(
        tmp_path, "set_fake job ns-a COMPLETE; set_fake job ns-b COMPLETE\n"
    )
    assert result.returncode == 0, result.stderr
    keys = _keys(result.stdout)
    assert (keys["STATE"], keys["NEXT_ACTION"]) == ("SUCCESS", "COLLECT")
    assert keys["EXECUTIONS_SUCCEEDED"] == "4"
    assert keys["SEGMENT_CURRENT"] == "1"
    assert not any(line.startswith("inspector") for line in _events(tmp_path))


def test_drift_in_a_later_target_fails_the_attempt(tmp_path):
    """[T-13] Worker drift on an unfinished segment stops every segment."""
    result, state = _status(
        tmp_path,
        """
        set_fake job ns-a ACTIVE; set_fake job ns-b SUSPENDED
        set_fake remote ns-a RUNNING; set_fake progress ns-a '1 1 0 0'
        DRIFT_NAMESPACE=ns-b
        """,
    )
    assert result.returncode == 0, result.stderr
    keys = _keys(result.stdout)
    assert keys["STATE"] == "FAILED"
    assert (keys["SEGMENT_0_STATE"], keys["SEGMENT_1_STATE"]) == (
        "FAILED",
        "NOT_STARTED",
    )
    events = _events(tmp_path)
    assert events.index("endpoints-check ns-a") < events.index("endpoints-check ns-b")
    sequencer = events.index(f"delete Job ns-a/sst-elb-{_ATTEMPT}-sequencer")
    current = events.index(f"delete Job ns-a/sst-elb-{_ATTEMPT}-sweep")
    later = events.index(f"delete Job ns-b/sst-elb-{_ATTEMPT}-s1-sweep")
    assert sequencer < current < events.index("recover ns-a") < later
    steps = _steps(state)
    assert {
        "delete-sequencer",
        "delete-sweep",
        "segment-not-started-s1",
        "delete-sweep-s1",
        "stopped-by-drift",
    } <= steps
    assert _local_state(state) == "TERMINAL"


def test_cancel_stops_the_sequencer_and_deletes_suspended_jobs(tmp_path):
    """Cancel stops the sequencer first, then deletes the running and later Jobs."""
    result, state = _status(
        tmp_path,
        """
        set_fake job ns-a ACTIVE; set_fake job ns-b SUSPENDED
        set_fake remote ns-a RUNNING; set_fake progress ns-a '0 0 1 1'
        """,
        "cancel",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "STATE=CANCELLED"
    events = _events(tmp_path)
    sequencer = f"delete Job ns-a/sst-elb-{_ATTEMPT}-sequencer"
    expected = [
        sequencer,
        "observe ns-b",
        "observe ns-a",
        "inspector ns-a status",
        "observe ns-a",
        f"delete Job ns-a/sst-elb-{_ATTEMPT}-sweep",
        "finalize-cancelled ns-a inspector-ns-a",
        "remove inspector-ns-a",
        "observe ns-b",
        f"delete Job ns-b/sst-elb-{_ATTEMPT}-s1-sweep",
    ]
    assert events[events.index(sequencer) :] == expected
    assert _local_state(state) == "TERMINAL"
    steps = _steps(state)
    assert {"delete-sequencer", "delete-sweep", "delete-sweep-s1"} <= steps
    assert "segment-not-started-s1" in steps
    # Workers and Leases remain for collection.
    assert not any(step.startswith(("delete-workers", "release-")) for step in steps)
    status = _bash(
        _fake_cluster(tmp_path)
        + f"results={str(state.parent)!r}\n"
        + "set_fake progress ns-a '0 0 1 1'\n"
        + 'kubectl_lifecycle_operation status "$results"\n'
    )
    assert status.returncode == 0, status.stderr
    keys = _keys(status.stdout)
    assert (keys["STATE"], keys["NEXT_ACTION"]) == ("CANCELLED", "COLLECT")
    assert (keys["SEGMENT_0_STATE"], keys["SEGMENT_1_STATE"]) == (
        "CANCELLED",
        "NOT_STARTED",
    )


_COLLECT = f"""
    for segment in 0 1; do
        rm -f "$metadata/control-bundle$(kubectl_segment_suffix $segment)/executions"/*.sh
    done
    printf 'export nodes=1\\n' > "$metadata/control-bundle/executions/0001.sh"
    printf 'export nodes=1\\n' > "$metadata/control-bundle-s1/executions/0002.sh"
    set_fake job ns-a COMPLETE; set_fake job ns-b COMPLETE
    set_fake remote ns-a SUCCESS; set_fake remote ns-b SUCCESS
    kubectl_remote_attempt_apparent_bytes() {{ printf -v "$4" 1024; }}
    kubectl_stream_remote_attempt() {{
        event "stream $1 $2"
        tar -C "$fake/tree-$1" -cf "$4" {_ATTEMPT}
    }}
"""


def _collect(
    tmp_path: Path,
    environment: str = "",
    repeat: int = 1,
    after: str = "",
    publications: bool = True,
):
    state = tmp_path / "results" / "kubernetes"
    state.mkdir(parents=True)
    if publications:
        _publication(tmp_path / "fake" / "tree-ns-a", "0001", "segment\t0\n")
        _publication(tmp_path / "fake" / "tree-ns-b", "0002", "segment\t1\n")
    runs = "\n".join(
        [
            'outcome=0; kubectl_lifecycle_operation collect "$results" || outcome=$?',
            'printf "COLLECT_EXIT=%s\\n" "$outcome"',
        ]
        * repeat
    )
    body = (
        _fake_cluster(tmp_path)
        + _two_segment_attempt(state)
        + environment
        + _COLLECT
        + after
        + ': > "$events"\n'
        + runs
        + "\n"
    )
    return _bash(body), state


def test_collect_merges_every_segment_then_cleans_both_namespaces(tmp_path):
    """Gate, stream, import per segment; release both run trees; Leases last-first."""
    result, state = _collect(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "COLLECT_EXIT=0" in result.stdout
    assert "STATE=SUCCESS" in result.stdout
    results = state.parent
    assert (results / "result-0001.txt").read_text() == "result 0001\n"
    assert (results / "result-0002.txt").read_text() == "result 0002\n"
    assert (results / "executions/0001.status").read_text() == "SUCCESS\n"
    assert (results / "executions/0002.status").read_text() == "SUCCESS\n"
    # Each segment's attempt-level ledger keeps its own results-root path,
    # named by its namespace; run.status and run-summary.tsv are the aggregate.
    assert (results / "run-summary-ns-a.tsv").read_text() == "segment\t0\n"
    assert (results / "run-summary-ns-b.tsv").read_text() == "segment\t1\n"
    assert (results / "run-ns-a.status").read_text() == "SUCCESS\n"
    assert (results / "run-ns-b.status").read_text() == "SUCCESS\n"
    assert (results / "run.status").read_text() == "SUCCESS\n"
    summary = (results / "run-summary.tsv").read_text()
    assert "scope\tattempt\n" in summary
    assert "run_status\tSUCCESS\n" in summary
    assert "segments\t2\n" in summary
    assert "segment\t0\tns-a\tSUCCESS\n" in summary
    assert "segment\t1\tns-b\tSUCCESS\n" in summary
    assert "reason\t" not in summary
    metadata = state / "attempts" / _ATTEMPT
    assert (metadata / "collected-state/executions/0001.status").exists()
    assert (metadata / "collected-state-s1/executions/0002.status").exists()
    events = _events(tmp_path)
    order = [
        "inspector ns-a collector",
        "inspector ns-b collector",
        f"delete Job ns-a/sst-elb-{_ATTEMPT}-sequencer",
        "stream ns-a inspector-ns-a",
        "stream ns-b inspector-ns-b",
        "exec ns-a inspector-ns-a",
        "exec ns-b inspector-ns-b",
        f"delete ServiceAccount ns-a/sst-elb-{_ATTEMPT}-sequencer",
        "lease-delete pvc-lease-c2",
        "lease-delete pvc-lease-c1",
        "lease-delete pvc-lease",
    ]
    assert [event for event in events if event in order] == order
    for kind, namespace, name in (
        ("Role", "ns-a", "sequencer"),
        ("Role", "ns-b", "s1-sequencer"),
        ("RoleBinding", "ns-b", "s1-sequencer"),
        ("DaemonSet", "ns-b", "s1-workers"),
        ("Job", "ns-b", "s1-sweep"),
    ):
        assert f"delete {kind} {namespace}/sst-elb-{_ATTEMPT}-{name}" in events
    steps = _steps(state)
    assert {
        "release-remote-run",
        "release-remote-run-s1",
        "collection-published",
    } <= steps
    assert _local_state(state) == "COLLECTED"
    status = _bash(
        f"results={str(results)!r}\n"
        + 'kubectl_lifecycle_operation status "$results"\n'
    )
    assert status.returncode == 0, status.stderr
    keys = _keys(status.stdout)
    assert (keys["STATE"], keys["RESULT_COLLECTION"], keys["NEXT_ACTION"]) == (
        "SUCCESS",
        "COMPLETE",
        "NONE",
    )
    assert (keys["SEGMENT_0_STATE"], keys["SEGMENT_1_STATE"]) == ("SUCCESS", "SUCCESS")


def test_collect_interrupted_between_per_target_cleanups_resumes(tmp_path):
    """[R-21] A failed run-tree release resumes on that segment only, then finishes."""
    result, state = _collect(tmp_path, "FAIL_EXEC_ONCE=ns-b\n", repeat=2)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("COLLECT_EXIT=") == 2
    assert "COLLECT_EXIT=1" in result.stdout
    assert result.stdout.strip().endswith("COLLECT_EXIT=0")
    events = _events(tmp_path)
    failed = events.index("exec-failed ns-b inspector-ns-b")
    retry = events[failed + 1 :]
    streams = [event for event in retry if event.startswith("stream")]
    assert streams == []
    assert "inspector ns-a collector" not in retry
    assert "inspector ns-b collector" in retry
    assert "exec ns-a inspector-ns-a" not in retry
    assert (
        retry.index("lease-delete pvc-lease-c2")
        < retry.index("lease-delete pvc-lease-c1")
        < retry.index("lease-delete pvc-lease")
    )
    assert _local_state(state) == "COLLECTED"


def test_collect_refuses_between_segments(tmp_path):
    """Segment 0 succeeded while segment 1 waits: collection is not yet allowed."""
    result, state = _status(
        tmp_path,
        _COLLECT
        + "set_fake job ns-b SUSPENDED; set_fake remote ns-b PREPARED\n"
        + "kubectl_unsuspend_segment_job() { :; }\n",
        "collect",
    )
    assert result.returncode != 0
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_PHASE=terminal-gate" in result.stderr
    assert "STORAGE_SCALE_TEST_DIAGNOSTIC_REASON=INVALID_LIFECYCLE_OPERATION" in (
        result.stderr
    )
    assert _local_state(state) == "SUBMITTED"
    events = _events(tmp_path)
    assert not any(line.startswith(("stream", "delete")) for line in events)
    assert "remove inspector-ns-b" in events


def test_v1_attempt_is_refused_with_the_collect_with_v1_message(tmp_path):
    """[R-22] An attempt started by v1.x is never inspected, collected or changed."""
    state = tmp_path / "results" / "kubernetes"
    metadata = state / "attempts" / _ATTEMPT
    metadata.mkdir(parents=True)
    (metadata / "identity.sh").write_text(
        "# Trusted storage-scale-test Kubernetes attempt metadata.\n"
        f"KUBECTL_ATTEMPT_SCHEMA=3\nKUBECTL_ATTEMPT_ID={_ATTEMPT}\n"
        f"KUBECTL_OWNERSHIP_NONCE={_NONCE}\nKUBECTL_NAMESPACE=test-ns\n",
        encoding="utf-8",
    )
    (metadata / "identity.yaml").write_text("schema: 3\n", encoding="utf-8")
    (metadata / "state.sh").write_text("KUBECTL_LIFECYCLE_STATE=TERMINAL\n")
    (state / "current-attempt").write_text(f"{_ATTEMPT}\n")
    before = sorted(str(path) for path in state.rglob("*"))
    for operation in ("status", "collect", "cancel"):
        result = _bash(
            _fake_cluster(tmp_path)
            + f"kubectl_lifecycle_operation {operation} {str(state.parent)!r}\n"
        )
        assert result.returncode != 0
        assert "was started by storage-scale-test v1.x" in result.stderr
        assert "Collect this attempt with storage-scale-test v1.x" in result.stderr
    assert _events(tmp_path) == []
    after = sorted(str(path) for path in state.rglob("*") if path.name != ".lock")
    assert [path for path in after if path in before] == [
        path for path in before if path in after
    ]
    assert (metadata / "state.sh").read_text() == "KUBECTL_LIFECYCLE_STATE=TERMINAL\n"


def test_resume_selection_skips_successful_segments_and_runs_unstarted_ones(tmp_path):
    """Resume reruns failed and unstarted segments' cells, never succeeded ones."""
    state = tmp_path / "results" / "kubernetes"
    state.mkdir(parents=True)
    result = _bash(
        _fake_cluster(tmp_path) + _two_segment_attempt(state) + textwrap.dedent(f"""\
            mkdir -p "$metadata/collected-state/executions"
            printf 'SUCCESS\\n' > "$metadata/collected-state/run.status"
            printf 'SUCCESS\\n' > "$metadata/collected-state/executions/0001.status"
            printf 'SUCCESS\\n' > "$metadata/collected-state/executions/0003.status"
            KUBECTL_SEGMENT_COUNT=2
            _kubectl_select_multi_segment_resume "$root" {_ATTEMPT} unused
            """)
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "0001\tSKIP",
        "0003\tSKIP",
        "0002\tRUN",
        "0004\tRUN",
    ]


def test_sequencer_probe_creates_scoped_rbac_and_always_cleans_up(tmp_path):
    """The validation probe uses a Role on one absent Job and deletes it all."""
    result = _bash(_fake_cluster(tmp_path) + textwrap.dedent("""\
            export KUBECTL_NAMESPACE=ns-a
            kubectl_job_terminal_state() { printf -v "$1" "${PROBE_RESULT:-COMPLETE}"; }
            kubectl_validate_sequencer_probe
            PROBE_RESULT=FAILED
            _kubectl_print_runtime_validation_diagnostics() { event probe-diagnostics; }
            ! kubectl_validate_sequencer_probe
            """))
    assert result.returncode == 0, result.stderr
    name = f"sst-elb-{_ATTEMPT}-sequencer"
    one_run = [
        f"create ServiceAccount ns-a/{name} ",
        f"create Role ns-a/{name} ",
        f"create RoleBinding ns-a/{name} ",
        f"create Job ns-a/{name}-probe ",
        f"delete Job ns-a/{name}-probe",
        f"delete RoleBinding ns-a/{name}",
        f"delete Role ns-a/{name}",
        f"delete ServiceAccount ns-a/{name}",
    ]
    assert (
        _events(tmp_path) == one_run + one_run[:4] + ["probe-diagnostics"] + one_run[4:]
    )
    role = yaml.safe_load(
        (tmp_path / "manifests" / f"Role.ns-a.{name}.yaml").read_text()
    )
    assert role["rules"][0]["resourceNames"] == [f"sst-elb-{_ATTEMPT}-absent"]
    job = yaml.safe_load(
        (tmp_path / "manifests" / f"Job.ns-a.{name}-probe.yaml").read_text()
    )
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == name
    assert pod["automountServiceAccountToken"] is True
    assert pod["containers"][0]["image"] == _SEQUENCER_IMAGE
    assert pod["containers"][0]["args"] == ["ns-a", f"sst-elb-{_ATTEMPT}-absent", _UID0]
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["activeDeadlineSeconds"] == 180


def test_reservation_script_reports_a_run_tree_reached_through_another_claim(
    tmp_path,
):
    """[S-22] The second reservation on aliased storage fails without debris."""
    mount = tmp_path / "mount"
    (mount / "pvc-a2/benchmark").mkdir(parents=True)
    runs = mount / "pvc-a2/benchmark/.storage-scale-test/runs"
    result = _bash(f"""
        set -e
        mount={str(mount)!r}
        kubectl_pvc_exec() {{
            local script argument
            local -a arguments=()
            script=$(sed "s#/mnt/storage-scale-test#$mount#g" <<< "$5")
            for argument in "${{@:7}}"; do
                arguments+=("$(sed "s#/mnt/storage-scale-test#$mount#g" <<< "$argument")")
            done
            bash -ceu "$script" bash "${{arguments[@]}}"
        }}
        kubectl_reserve_remote_attempt ns-a helper-a {_ATTEMPT} {_NONCE}
        ! kubectl_reserve_remote_attempt ns-b helper-b {_ATTEMPT} {_NONCE} \\
            2> {str(tmp_path / 'error')!r}
        """)
    assert result.returncode == 0, result.stderr
    assert "attempt run tree already exists" in (tmp_path / "error").read_text()
    assert sorted(path.name for path in runs.iterdir()) == [_ATTEMPT]
    assert (runs / _ATTEMPT / "owner").read_text() == f"{_ATTEMPT}\t{_NONCE}\n"


_LOST_COORDINATORS = """
    set_fake job ns-a FAILED; set_fake job ns-b FAILED
    set_fake remote ns-b FAILED
    kubectl_local_lock_acquire "$root" fd
    kubectl_attempt_journal_step "$root" "$fd" 1234abcd segment-succeeded
    kubectl_local_lock_release "$fd"
"""


def _lost_coordinator_collect(tmp_path: Path):
    """Collect two segments whose coordinators were both lost and recovered."""
    trees = tmp_path / "fake"
    _publication(
        trees / "tree-ns-a",
        "0001",
        "segment\t0\n",
        diagnostics=("coordinator-loss.tsv",),
    )
    _publication(
        trees / "tree-ns-b",
        "0002",
        "segment\t1\n",
        outcome="FAILED",
        diagnostics=("coordinator-loss.tsv", "startup-error.txt"),
    )
    return _collect(tmp_path, after=_LOST_COORDINATORS, publications=False)


def test_lost_coordinators_in_two_segments_collect_with_both_diagnostics(tmp_path):
    """[R-24] Two segments' attempt-level diagnostics land at distinct paths."""
    result, state = _lost_coordinator_collect(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "COLLECT_EXIT=1" in result.stdout
    assert "STATE=FAILED" in result.stdout
    assert _local_state(state) == "COLLECTED"
    results = state.parent
    expected = {
        "coordinator-loss-ns-a.tsv": "coordinator-loss.tsv of segment\t0\n",
        "coordinator-loss-ns-b.tsv": "coordinator-loss.tsv of segment\t1\n",
        "startup-error-ns-b.txt": "startup-error.txt of segment\t1\n",
        "run-ns-a.status": "SUCCESS\n",
        "run-ns-b.status": "FAILED\n",
        "run-summary-ns-a.tsv": "segment\t0\n",
        "run-summary-ns-b.tsv": "segment\t1\n",
        # The attempt's aggregate outcome.
        "run.status": "FAILED\n",
    }
    assert {name: (results / name).read_text() for name in expected} == expected
    assert "run_status\tFAILED\n" in (results / "run-summary.tsv").read_text()
    assert not (results / "startup-error.txt").exists()
    assert not (results / "coordinator-loss.tsv").exists()
    assert (results / "executions/0002.status").read_text() == "FAILED\n"
    events = _events(tmp_path)
    assert "recover ns-a" in events and "recover ns-b" in events
    status = _bash(
        f"results={str(results)!r}\n"
        + 'kubectl_lifecycle_operation status "$results"\n'
    )
    assert status.returncode == 0, status.stderr
    keys = _keys(status.stdout)
    assert (keys["STATE"], keys["NEXT_ACTION"]) == ("FAILED", "RESUME")
    assert (keys["SEGMENT_0_STATE"], keys["SEGMENT_1_STATE"]) == ("SUCCESS", "FAILED")


def test_resume_after_two_lost_coordinators_reruns_and_merges_cleanly(tmp_path):
    """[R-24] Resume reruns the failed segment and supersedes its diagnostics."""
    result, state = _lost_coordinator_collect(tmp_path)
    assert result.returncode == 0, result.stderr
    results = state.parent
    coordinator = _ROOT / "storage-tests/fs/kubectl/_nv-elbencho-kubectl-coordinator.sh"
    resumed = "5678ef01"
    staging = tmp_path / "resumed"
    _publication(
        staging,
        "0002",
        "resumed\n",
        diagnostics=("coordinator-loss.tsv",),
        attempt=resumed,
    )
    selection = _bash(f"""
        KUBECTL_SEGMENT_COUNT=2
        _kubectl_select_multi_segment_resume {str(state)!r} {_ATTEMPT} \\
            {str(coordinator)!r}
        """)
    assert selection.returncode == 0, selection.stderr
    assert selection.stdout.splitlines() == ["0001\tSKIP", "0002\tRUN"]
    merge = _bash(f"""
        set -e
        _kubectl_merge_collected_results {str(staging / resumed / 'state')!r} \\
            {str(results)!r} {resumed} reason
        """)
    assert merge.returncode == 0, merge.stderr
    # The resumed one-segment attempt replaces every earlier attempt-level
    # ledger it rewrites, every segment's run.status/run-summary, and the
    # earlier attempt's aggregate copies of both.
    assert (results / "coordinator-loss.tsv").read_text() == (
        "coordinator-loss.tsv of resumed\n"
    )
    assert (results / "run-summary.tsv").read_text() == "resumed\n"
    assert (results / "run.status").read_text() == "SUCCESS\n"
    for name in ("run-ns-a.status", "run-ns-b.status", "run-summary-ns-a.tsv"):
        assert not (results / name).exists(), name
    assert not (results / "run-summary-ns-b.tsv").exists()
    assert (results / "coordinator-loss-ns-a.tsv").exists()
    assert (results / "coordinator-loss-ns-b.tsv").exists()
    assert (results / "executions/0001.status").read_text() == "SUCCESS\n"
    assert (results / "executions/0002.status").read_text() == "SUCCESS\n"
    assert (results / "result-0002.txt").read_text() == "result 0002\n"


def test_status_continues_after_a_segment_recovered_to_success(tmp_path):
    """[T-14] A coordinator lost after its last cell does not stall later segments."""
    result, state = _status(
        tmp_path,
        """
        set_fake job ns-a FAILED; set_fake job ns-b SUSPENDED
        set_fake remote ns-a RUNNING; set_fake progress ns-a '0 0 2 0'
        set_fake remote ns-b PREPARED; set_fake progress ns-b '2 0 0 0'
        RECOVER_TO=SUCCESS
        """,
    )
    assert result.returncode == 0, result.stderr
    keys = _keys(result.stdout)
    assert keys["STATE"] == "BETWEEN_EXECUTIONS"
    assert (keys["SEGMENT_0_STATE"], keys["SEGMENT_1_STATE"]) == ("SUCCESS", "PENDING")
    assert keys["SEGMENT_CURRENT"] == "1"
    events = _events(tmp_path)
    assert events.index("recover ns-a") < events.index(
        f"unsuspend ns-b/sst-elb-{_ATTEMPT}-s1-sweep uid-sst-elb-{_ATTEMPT}-s1-sweep"
    )
    assert "segment-succeeded" in _steps(state)
