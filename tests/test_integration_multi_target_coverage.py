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

"""Coverage-gap contracts for the multi-target integration scenarios.

They pin the pure logic added so the scenarios prove what they claim: the
sequencer actually chained the targets, a restricted identity is refused, the
mixed batch records storage-target provenance, and same-namespace targets use
one coordinator Job.
"""

# This module intentionally tests private integration-driver boundaries.
# pylint: disable=protected-access

import hashlib
import json
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.integration_scenario_test_helpers import (
    load_integration_driver,
    load_integration_lib,
)

_DRIVER = load_integration_driver()
_FILESYSTEM = load_integration_lib("filesystem_integration")
fixture_storage = load_integration_lib("fixture_storage")
_SPECS = _FILESYSTEM.SCENARIO_SPECS_BY_NAME
_Error = _FILESYSTEM.IntegrationTestError

_SEQUENCER = "docker.io/library/storage-scale-integration-kubectl@sha256:" + "b" * 64
_FIRST = fixture_storage.KubectlStorageTarget("ns-one", "pv-one", "storage-test-rwx")
_SECOND = fixture_storage.KubectlStorageTarget("ns-two", "pv-two", "storage-test-rwx")
_SAME = fixture_storage.KubectlStorageTarget("ns-one", "pv-three", "storage-test-rwx-2")
_CONFIG = SimpleNamespace(kubeconfig=Path("kubeconfig"), namespace="ns-one")
_ATTEMPT = "0123abcd"


def _fixture(**overrides):
    values = {
        "architecture": "x86_64",
        "kubectl_nodes": ("worker-a",),
        "kubectl_namespace": "ns-one",
        "kubectl_pv": "pv-one",
        "kubectl_pvc": "storage-test-rwx",
        "kubectl_image": "docker.io/example/elbencho:test",
        "kubectl_targets": (_FIRST, _SECOND),
        "kubectl_sequencer_image": _SEQUENCER,
        "kubectl_same_namespace_target": _SAME,
        "slurm_nodes": (),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


# --- fixture identities and environment generation --------------------------


def test_same_namespace_claim_is_a_distinct_claim_with_its_own_directory():
    """The third claim shares the primary namespace but nothing else."""
    assert fixture_storage.SAME_NAMESPACE_CLAIM != fixture_storage.STORAGE_CLAIM
    assert (
        fixture_storage.SBX_SAME_NAMESPACE_DATA_DIRECTORY
        in fixture_storage.SBX_SHARED_DIRECTORIES
    )
    assert len(set(fixture_storage.SBX_SHARED_DIRECTORIES)) == len(
        fixture_storage.SBX_SHARED_DIRECTORIES
    )


def test_same_namespace_override_block_names_one_namespace_and_the_image(tmp_path):
    """One-namespace targets name only the default namespace in env.sh, plus
    the preloaded sequencer image the default validation checks."""
    block, _ = _FILESYSTEM._override_block(  # pylint: disable=protected-access
        "kubectl",
        str(tmp_path),
        _fixture(),
        multi_target=True,
        same_namespace=True,
    )
    assert "export KUBECTL_NAMESPACE=ns-one" in block
    assert f"export KUBECTL_SEQUENCER_IMAGE={_SEQUENCER}" in block
    assert "ns-two" not in block
    for removed in ("KUBECTL_PV", "KUBECTL_PVC", "KUBECTL_STORAGE_TARGETS"):
        assert removed not in block


def test_cross_namespace_override_block_requires_a_pinned_sequencer_image(tmp_path):
    """Only a batch spanning namespaces needs the image, and it must be pinned."""
    for image in ("docker.io/example/kubectl:1", ""):
        with pytest.raises(
            fixture_storage.StorageTargetError, match="digest-qualified"
        ):
            _FILESYSTEM._override_block(  # pylint: disable=protected-access
                "kubectl",
                str(tmp_path),
                _fixture(kubectl_sequencer_image=image),
                multi_target=True,
            )
    block, _ = _FILESYSTEM._override_block(  # pylint: disable=protected-access
        "kubectl", str(tmp_path), _fixture(), multi_target=True
    )
    assert f"export KUBECTL_SEQUENCER_IMAGE={_SEQUENCER}" in block


def test_same_namespace_mode_requires_the_third_claim(tmp_path):
    """A fixture set up before the third claim names the remedy."""
    with pytest.raises(_Error, match="re-run"):
        _FILESYSTEM._override_block(  # pylint: disable=protected-access
            "kubectl",
            str(tmp_path),
            _fixture(kubectl_same_namespace_target=None),
            multi_target=True,
            same_namespace=True,
        )


def test_scenario_storage_targets_follow_the_scenarios_layout():
    """The same-namespace scenario pairs the primary claim with the third one."""
    same = SimpleNamespace(scenario=_SPECS["kubectl-same-namespace-targets"])
    cross = SimpleNamespace(scenario=_SPECS["kubectl-multi-target"])
    pick = _FILESYSTEM._scenario_storage_targets  # pylint: disable=protected-access
    assert pick(_fixture(), same) == (_FIRST, _SAME)
    assert pick(_fixture(), cross) == (_FIRST, _SECOND)


class _ClaimRunner:
    def __init__(self, stdout):
        self.stdout = stdout
        self.commands = []

    def run(self, arguments, **_kwargs):
        self.commands.append([str(item) for item in arguments])
        return SimpleNamespace(returncode=0, stdout=self.stdout, stderr="")


def test_same_namespace_target_is_read_from_the_primary_namespace():
    """The third claim is looked up beside the primary one, tolerating absence."""
    config = SimpleNamespace(kubeconfig=Path("kubeconfig"), namespace="fixture")
    found = json.dumps({"status": {"phase": "Bound"}, "spec": {"volumeName": "pvc-3"}})
    runner = _ClaimRunner(found)
    target = (
        _FILESYSTEM._same_namespace_storage_target(  # pylint: disable=protected-access
            runner, config
        )
    )
    assert target == fixture_storage.KubectlStorageTarget(
        "fixture", "pvc-3", fixture_storage.SAME_NAMESPACE_CLAIM
    )
    assert "fixture" in runner.commands[0]
    assert fixture_storage.SAME_NAMESPACE_CLAIM in runner.commands[0]
    assert (
        _FILESYSTEM._same_namespace_storage_target(  # pylint: disable=protected-access
            _ClaimRunner(""), config
        )
        is None
    )


def _documents(name, **replacements):
    text = _DRIVER._render_resource_text(  # pylint: disable=protected-access
        name,
        {"STORAGE_TEST_CAPACITY": "2Gi", "SSH_HOME_CAPACITY": "64Mi", **replacements},
    )
    return [document for document in yaml.safe_load_all(text) if document]


def test_nfs_manifest_adds_a_second_claim_in_the_primary_namespace():
    """The StorageClass subDir differs by claim name, so the claims never share."""
    documents = _documents(
        "manifests/nfs-storage.yaml.tmpl",
        NAMESPACE="ns-one",
        NAMESPACE_2="ns-two",
        NFS_SERVER="10.0.0.1",
    )
    claims = [
        (item["metadata"]["namespace"], item["metadata"]["name"])
        for item in documents
        if item["kind"] == "PersistentVolumeClaim"
    ]
    assert ("ns-one", fixture_storage.SAME_NAMESPACE_CLAIM) in claims
    assert len(claims) == len(set(claims))


def test_sbx_manifest_binds_the_third_claim_to_its_own_pv_and_directory():
    """A static PV with a claimRef and its own backing directory."""
    documents = _documents(
        "manifests/sbx-storage.yaml.tmpl", NAMESPACE="ns-one", NAMESPACE_2="ns-two"
    )
    volume = next(
        item
        for item in documents
        if item["kind"] == "PersistentVolume"
        and item["metadata"]["name"] == fixture_storage.SBX_SAME_NAMESPACE_DATA_PV
    )
    assert volume["spec"]["claimRef"] == {
        "namespace": "ns-one",
        "name": fixture_storage.SAME_NAMESPACE_CLAIM,
    }
    assert (
        Path(volume["spec"]["hostPath"]["path"]).name
        == fixture_storage.SBX_SAME_NAMESPACE_DATA_DIRECTORY
    )
    claim = next(
        item
        for item in documents
        if item["kind"] == "PersistentVolumeClaim"
        and item["metadata"]["name"] == fixture_storage.SAME_NAMESPACE_CLAIM
    )
    assert claim["metadata"]["namespace"] == "ns-one"
    assert claim["spec"]["volumeName"] == fixture_storage.SBX_SAME_NAMESPACE_DATA_PV


# --- scenario specifications -------------------------------------------------


def test_same_namespace_scenario_is_one_small_cell_without_a_sequencer_env():
    """One 4K cell per group: a --batch of two target files and --nodes 1."""
    spec = _SPECS["kubectl-same-namespace-targets"]
    (prepare,) = spec.steps
    assert prepare.arguments == (
        "--batch",
        "--target",
        "targets/agg.env",
        "--target",
        "targets/single.env",
        "--run-to-completion",
        "--nodes",
        "1",
    )
    assert [item.coordinate.nodes for item in prepare.executions] == [1]
    assert prepare.executions[0].coordinate.io_size == "4K"
    assert prepare.exports == ("batch_results_dir",)
    assert not any("SEQUENCER" in line for line in prepare.env_lines)
    assert spec.same_namespace and spec.storage_targets == 2


def test_same_namespace_target_files_hold_only_target_settings():
    """agg.env and single.env set TEST_DIRS alone; workload stays in env.sh."""
    (prepare,) = _SPECS["kubectl-same-namespace-targets"].steps
    files = {item.relative_path: item.content for item in prepare.support_files}
    assert files == {
        "targets/agg.env": (
            'TEST_DIRS=(["{test_key}"]=2 ["{test_key_claim2_secondary}"]=1)\n'
        ),
        "targets/single.env": 'TEST_DIRS=(["{test_key_claim2}"]=1)\n',
    }


def test_target_files_never_carry_workload_settings():
    """Every scenario target file sets only target settings (TEST_DIRS, name)."""
    allowed = {
        "TEST_DIRS",
        "STORAGE_TARGET_NAME",
        "FS_MAX_AGG_THROUGHPUT",
        "FS_MAX_NODE_THROUGHPUT_GBPS",
        "FS_MAX_NODE_IOPS",
    }
    for spec in _SPECS.values():
        for step in spec.steps:
            arguments = step.arguments
            targets = [
                arguments[index + 1]
                for index, argument in enumerate(arguments[:-1])
                if argument == "--target"
            ]
            files = {item.relative_path: item for item in step.support_files}
            for target in targets:
                names = {
                    line.partition("=")[0] for line in files[target].content.split("\n")
                } - {""}
                assert names <= allowed, (spec.name, target, names)


def test_no_override_file_carries_a_target_setting():
    """--env-override files hold workload settings only, on every substrate."""
    forbidden = ("TEST_DIRS", "STORAGE_TARGET_NAME", "FS_MAX_")
    for spec in _SPECS.values():
        for step in spec.steps:
            arguments = step.arguments
            overrides = [
                arguments[index + 1]
                for index, argument in enumerate(arguments[:-1])
                if argument == "--env-override"
            ]
            for item in step.support_files:
                if item.relative_path in overrides:
                    assert not any(name in item.content for name in forbidden), (
                        spec.name,
                        item.relative_path,
                    )


def test_same_namespace_requires_exactly_two_targets():
    """The layout flag is meaningless for one target."""
    module = load_integration_lib("filesystem_scenario_specs")
    spec = module.SCENARIO_SPECS_BY_NAME["kubectl-same-namespace-targets"]
    with pytest.raises(module.ScenarioSpecError, match="storage target count"):
        module.validate_scenario_specs((replace(spec, storage_targets=1),))


def test_sequenced_scenario_uses_a_short_sequencer_poll():
    """Hand-offs wait on the sequencer's poll, so the scenario shortens it."""
    first, metadata = _SPECS["kubectl-multi-target"].steps
    assert "export KUBECTL_SEQUENCER_POLL_SECONDS=2" in first.env_lines
    assert first.env_lines == metadata.env_lines


# --- sequencer proof ---------------------------------------------------------

_NAMESPACES = ("ns-one", "ns-two")
_GOOD_LOG = (
    "sequencer: starting segment 0 (Job ns-one/sst-elb-x-sweep-t0)\n"
    "sequencer: segment 0 (Job ns-one/sst-elb-x-sweep-t0) complete\n"
    "sequencer: starting segment 1 (Job ns-two/sst-elb-x-sweep-t1)\n"
    "sequencer: segment 1 (Job ns-two/sst-elb-x-sweep-t1) complete\n"
    "sequencer: all segments complete\n"
)


def _jobs(*conditions, count=1):
    items = [
        {"status": {"conditions": [{"type": kind, "status": status}]}}
        for kind, status in conditions
    ]
    return {"items": items or [{}] * count}


@pytest.mark.parametrize(
    "document, state",
    (
        ({"items": []}, "MISSING"),
        (_jobs(), "RUNNING"),
        (_jobs(("Complete", "True")), "COMPLETE"),
        (_jobs(("Failed", "True")), "FAILED"),
        (_jobs(("Complete", "False")), "RUNNING"),
        (_jobs(("Suspended", "True")), "RUNNING"),
    ),
)
def test_sequencer_job_state_reads_true_conditions(document, state):
    """Only a True Complete or Failed condition ends the Job."""
    assert _FILESYSTEM.sequencer_job_state(document) == state


def test_sequencer_job_state_rejects_several_jobs():
    """One attempt has exactly one sequencer."""
    with pytest.raises(_Error, match="one sequencer Job"):
        _FILESYSTEM.sequencer_job_state({"items": [{}, {}]})


def test_sequencer_log_must_show_every_hand_off_in_order():
    """Start and completion of each segment, then all of them, in sequence."""
    _FILESYSTEM.assert_sequencer_log(_GOOD_LOG, _NAMESPACES)
    for broken, message in (
        (_GOOD_LOG.replace("starting segment 1", "starting segment 9"), "segment 1"),
        (_GOOD_LOG.replace("all segments complete", "stopped"), "all segments"),
        (_GOOD_LOG.replace("ns-two/", "ns-one/"), "segment 1"),
        ("\n".join(reversed(_GOOD_LOG.splitlines())), "in order"),
        ("", "start of segment 0"),
    ):
        with pytest.raises(_Error, match=message):
            _FILESYSTEM.assert_sequencer_log(broken, _NAMESPACES)


class _Cluster:
    """Answer the sequencer Job list, its log, and diagnostic listings."""

    def __init__(self, jobs, log=_GOOD_LOG, log_returncode=0):
        self.jobs = list(jobs)
        self.log = log
        self.log_returncode = log_returncode
        self.commands = []

    def run(self, arguments, **_kwargs):
        command = [str(item) for item in arguments]
        self.commands.append(command)
        if "logs" in command and "--tail=-1" in command:
            return SimpleNamespace(
                returncode=self.log_returncode, stdout=self.log, stderr=""
            )
        if "logs" in command:
            return SimpleNamespace(returncode=0, stdout="tail of log", stderr="")
        if "-o" in command and "json" in command:
            document = self.jobs.pop(0) if len(self.jobs) > 1 else self.jobs[0]
            return SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr="")
        return SimpleNamespace(returncode=0, stdout="job/x 0/1 Running", stderr="")


def _quiet_time(monkeypatch):
    monkeypatch.setattr(_FILESYSTEM.time, "sleep", lambda _seconds: None)


def test_prove_sequencer_waits_with_kubectl_only_and_checks_the_log(
    monkeypatch, tmp_path
):
    """No --status is ever run; the Job completes, then its log is checked."""
    _quiet_time(monkeypatch)
    cluster = _Cluster([_jobs(), _jobs(), _jobs(("Complete", "True"))])
    _FILESYSTEM._prove_sequencer(  # pylint: disable=protected-access
        cluster, _CONFIG, _NAMESPACES, _ATTEMPT, tmp_path, 60
    )
    assert all("--status" not in command for command in cluster.commands)
    watched = [command for command in cluster.commands if "-o" in command]
    assert len(watched) == 3
    assert all(
        command[command.index("-n") + 1] == "ns-one" for command in cluster.commands
    )
    selector = next(command for command in cluster.commands if "-l" in command)
    assert "app.kubernetes.io/component=sequencer" in selector[selector.index("-l") + 1]
    assert (tmp_path / "kubectl-sequencer.log").read_text() == _GOOD_LOG


def test_failed_sequencer_reports_job_states_and_the_pod_log(monkeypatch, tmp_path):
    """A failing sequencer is reported at once, with evidence."""
    _quiet_time(monkeypatch)
    cluster = _Cluster([_jobs(("Failed", "True"))])
    with pytest.raises(_Error, match="sequencer Job is FAILED") as caught:
        _FILESYSTEM._prove_sequencer(  # pylint: disable=protected-access
            cluster, _CONFIG, _NAMESPACES, _ATTEMPT, tmp_path, 60
        )
    assert "tail of log" in str(caught.value)
    assert "Jobs and Pods in ns-two" in str(caught.value)


def test_missing_or_slow_sequencer_is_an_error(monkeypatch, tmp_path):
    """No sequencer Job, or one that never completes, fails the scenario."""
    _quiet_time(monkeypatch)
    with pytest.raises(_Error, match="MISSING"):
        _FILESYSTEM._prove_sequencer(  # pylint: disable=protected-access
            _Cluster([{"items": []}]), _CONFIG, _NAMESPACES, _ATTEMPT, tmp_path, 60
        )
    ticks = iter(range(0, 1000, 30))
    monkeypatch.setattr(_FILESYSTEM.time, "monotonic", lambda: next(ticks))
    with pytest.raises(_Error, match="did not complete within 60 seconds"):
        _FILESYSTEM._prove_sequencer(  # pylint: disable=protected-access
            _Cluster([_jobs()]), _CONFIG, _NAMESPACES, _ATTEMPT, tmp_path, 60
        )


def test_complete_sequencer_with_an_incomplete_log_fails_with_evidence(
    monkeypatch, tmp_path
):
    """A Job that finished without the hand-offs does not prove the sequencer."""
    _quiet_time(monkeypatch)
    cluster = _Cluster([_jobs(("Complete", "True"))], log=_GOOD_LOG.splitlines()[0])
    with pytest.raises(_Error, match="lacks the completion of segment 0") as caught:
        _FILESYSTEM._prove_sequencer(  # pylint: disable=protected-access
            cluster, _CONFIG, _NAMESPACES, _ATTEMPT, tmp_path, 60
        )
    assert "Jobs and Pods in ns-one" in str(caught.value)


def test_unreadable_sequencer_log_fails(monkeypatch, tmp_path):
    """A log that cannot be read cannot prove anything."""
    _quiet_time(monkeypatch)
    cluster = _Cluster([_jobs(("Complete", "True"))], log="", log_returncode=1)
    with pytest.raises(_Error, match="could not read the sequencer log"):
        _FILESYSTEM._prove_sequencer(  # pylint: disable=protected-access
            cluster, _CONFIG, _NAMESPACES, _ATTEMPT, tmp_path, 60
        )


def test_sequencer_log_accepts_segment_wording_too():
    """The unit of a hand-off is a namespace; either word names it."""
    log = _GOOD_LOG.replace("target", "segment")
    _FILESYSTEM.assert_sequencer_log(log, _NAMESPACES)


# --- restricted identity -----------------------------------------------------

_DENIAL_OUTPUT = "\n".join(
    [
        "Error: sequencer prerequisites are missing:",
        *(
            f"  - permission to {verb} {resource} in namespace {namespace}"
            for verb in ("create", "delete")
            for resource, namespace in (
                ("serviceaccounts", "ns-one"),
                ("roles.rbac.authorization.k8s.io", "ns-one"),
                ("rolebindings.rbac.authorization.k8s.io", "ns-one"),
            )
        ),
        "  Fallback: keep each batch in one namespace (it then needs no sequencer "
        "or its RBAC), or run one batch per namespace",
    ]
)


def test_restricted_manifest_grants_identity_access_but_no_rbac_management():
    """The identity can run Jobs and hold Leases but never manage RBAC objects."""
    manifest = _FILESYSTEM.restricted_identity_manifest("sst-x", ("ns-one", "ns-two"))
    kinds = [
        (item["kind"], item["metadata"].get("namespace")) for item in manifest["items"]
    ]
    assert kinds == [
        ("ServiceAccount", "ns-one"),
        ("Role", "ns-one"),
        ("RoleBinding", "ns-one"),
        ("Role", "ns-two"),
        ("RoleBinding", "ns-two"),
        ("ClusterRole", None),
        ("ClusterRoleBinding", None),
    ]
    resources = {
        (group, resource)
        for item in manifest["items"]
        for rule in item.get("rules", ())
        for group in rule["apiGroups"]
        for resource in rule["resources"]
    }
    assert ("batch", "jobs") in resources
    assert ("coordination.k8s.io", "leases") in resources
    assert not any(group == "rbac.authorization.k8s.io" for group, _ in resources)
    assert ("", "serviceaccounts") not in resources
    for item in manifest["items"]:
        assert item["metadata"]["labels"] == {
            "storage-scale-test/fixture-utility": "true"
        }
        assert "managed-by" not in json.dumps(item["metadata"]["labels"])
        for subject in item.get("subjects", ()):
            assert subject == {
                "kind": "ServiceAccount",
                "name": "sst-x",
                "namespace": "ns-one",
            }


def test_token_kubeconfig_keeps_the_cluster_and_replaces_the_credential():
    """Cluster trust is preserved; only the one user's credential changes."""
    document = {
        "clusters": [{"name": "c", "cluster": {"server": "https://k"}}],
        "users": [{"name": "u", "user": {"client-certificate-data": "x"}}],
    }
    rendered = _FILESYSTEM.token_kubeconfig_document(document, "tok")
    assert rendered["clusters"] == document["clusters"]
    assert rendered["users"] == [{"name": "u", "user": {"token": "tok"}}]
    assert document["users"][0]["user"] == {"client-certificate-data": "x"}
    with pytest.raises(_Error, match="exactly one user"):
        _FILESYSTEM.token_kubeconfig_document({"users": []}, "tok")


def test_rbac_denial_output_accepts_a_failure_naming_every_missing_permission():
    """--sequencer fails, naming each missing permission and the fallback."""
    _FILESYSTEM.assert_rbac_denial_output(1, _DENIAL_OUTPUT, ("ns-one",))


def test_rbac_denial_output_requires_a_failing_exit_status():
    """The user asked for the sequencer, so a gap must fail validation."""
    with pytest.raises(_Error, match="exited 0"):
        _FILESYSTEM.assert_rbac_denial_output(0, _DENIAL_OUTPUT, ("ns-one",))


def test_rbac_denial_output_checks_every_namespace_it_is_given():
    """Roles and RoleBindings are required in each validated namespace."""
    with pytest.raises(_Error, match="namespace ns-two"):
        _FILESYSTEM.assert_rbac_denial_output(1, _DENIAL_OUTPUT, ("ns-one", "ns-two"))


@pytest.mark.parametrize(
    "output, message",
    (
        (
            _DENIAL_OUTPUT.replace(
                "delete roles.rbac.authorization.k8s.io in namespace ns-one", "y"
            ),
            "delete roles.rbac.authorization.k8s.io in namespace ns-one",
        ),
        (
            _DENIAL_OUTPUT.replace("create serviceaccounts", "x"),
            "create serviceaccounts in namespace ns-one",
        ),
        (
            _DENIAL_OUTPUT
            + "\n  - permission to create jobs.batch in namespace ns-one",
            "denied a granted permission",
        ),
        ("All validation checks passed successfully", "did not report missing"),
        (_DENIAL_OUTPUT.replace("Fallback:", "Hint:"), "Fallback: keep each batch"),
    ),
)
def test_rbac_denial_output_rejects_other_outcomes(output, message):
    """A silent pass or a blanket denial is not an accurate RBAC report."""
    with pytest.raises(_Error, match=message):
        _FILESYSTEM.assert_rbac_denial_output(1, output, ("ns-one",))


def test_rbac_phrase_is_one_adjustable_constant():
    """The validate_env.sh wording lives in one place."""
    assert _FILESYSTEM.RBAC_PERMISSION_PHRASE.format(
        verb="create", resource="roles", namespace="n"
    ) == ("permission to create roles in namespace n")


class _RestrictedRunner:
    """Record kubectl calls of the restricted-identity step."""

    def __init__(self, validate_output):
        self.validate_output = validate_output
        self.commands = []

    def run(self, arguments, **_kwargs):
        command = [str(item) for item in arguments]
        self.commands.append(command)
        if command[0] == "env":
            return SimpleNamespace(returncode=1, stdout=self.validate_output, stderr="")
        if "token" in command:
            return SimpleNamespace(returncode=0, stdout="tok\n", stderr="")
        if "can-i" in command:
            return SimpleNamespace(returncode=0, stdout="yes\n", stderr="")
        if "view" in command:
            document = {"users": [{"name": "u", "user": {}}], "clusters": []}
            return SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


@pytest.mark.parametrize("output, passes", ((_DENIAL_OUTPUT, True), ("nope", False)))
def test_restricted_validation_step_always_deletes_its_objects(
    tmp_path, output, passes
):
    """The temporary identity is created, used with its own kubeconfig, and deleted."""
    runner = _RestrictedRunner(output)
    runtime = SimpleNamespace(workspace=str(tmp_path))
    step = (
        _FILESYSTEM._assert_validation_denied_without_rbac
    )  # pylint: disable=protected-access
    if passes:
        step(runner, _CONFIG, runtime, _NAMESPACES, tmp_path)
    else:
        with pytest.raises(_Error, match="restricted-identity validation"):
            step(runner, _CONFIG, runtime, _NAMESPACES, tmp_path)
    manifests = [command for command in runner.commands if "-f" in command]
    assert manifests[0][3] == "create" and manifests[-1][3] == "delete"
    assert "--ignore-not-found" in manifests[-1]
    assert manifests[-1][-2:] == ["-f", "-"]
    validation = next(command for command in runner.commands if command[0] == "env")
    assert validation[1].startswith("KUBECONFIG=")
    assert validation[1] != f"KUBECONFIG={_CONFIG.kubeconfig}"
    assert validation[2].endswith("/validate_env.sh")
    assert validation[3:] == ["--sequencer"]
    assert (tmp_path / "kubectl-rbac-denied-validate-env.log").read_text() == output


# --- validation output -------------------------------------------------------


class _ValidateRunner:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout
        self.commands = []

    def run(self, arguments, **kwargs):
        self.commands.append(([str(item) for item in arguments], kwargs))
        return SimpleNamespace(
            returncode=self.returncode, stdout=self.stdout, stderr=""
        )


def test_step_validation_returns_the_validator_output(tmp_path):
    """Callers can inspect what validate_env.sh printed."""
    text = f"some check\n{_FILESYSTEM.VALIDATION_SUCCESS}\n"
    runtime = SimpleNamespace(
        selector="kubectl",
        workspace=str(tmp_path),
        scenario=SimpleNamespace(name="s"),
    )
    step = SimpleNamespace(name="prepare-io", validate_env=True)
    output = _FILESYSTEM._validate_step_environment(  # pylint: disable=protected-access
        _ValidateRunner(0, text), _CONFIG, None, runtime, step, tmp_path
    )
    assert output == text
    assert (tmp_path / "kubectl-prepare-io-validate-env.log").read_text() == text
    runner = _ValidateRunner(0, text)
    for sequencer, options in ((False, []), (True, ["--sequencer"])):
        _FILESYSTEM._validate_step_environment(  # pylint: disable=protected-access
            runner, _CONFIG, None, runtime, step, tmp_path, sequencer=sequencer
        )
        assert runner.commands[-1][0][3:] == options
    with pytest.raises(_Error, match="validation failed"):
        _FILESYSTEM._validate_step_environment(  # pylint: disable=protected-access
            _ValidateRunner(1, text), _CONFIG, None, runtime, step, tmp_path
        )


def test_an_unmarked_step_skips_validation(tmp_path):
    """Only a step marked validate_env runs validate_env.sh."""
    runtime = SimpleNamespace(
        selector="kubectl", workspace=str(tmp_path), scenario=SimpleNamespace(name="s")
    )
    runner = _ValidateRunner(1, "")
    step = SimpleNamespace(name="resume", validate_env=False)
    output = _FILESYSTEM._validate_step_environment(  # pylint: disable=protected-access
        runner, _CONFIG, None, runtime, step, tmp_path, sequencer=True
    )
    assert output == ""
    assert not runner.commands
    assert not list(tmp_path.iterdir())


class _Prepare:
    """Record the order of preparation operations."""

    def __init__(self, monkeypatch):
        self.events = []
        names = ("_reset_result_base", "_sync_step_runtime")
        for name in names:
            monkeypatch.setattr(
                _FILESYSTEM, name, lambda *_a, _n=name, **_k: self.events.append(_n)
            )

        def validate(*_args, sequencer=False, **_kwargs):
            self.events.append("validate+sequencer" if sequencer else "validate")
            return "validated"

        def command(*_args, **_kwargs):
            self.events.append("command")
            return "STORAGE_SCALE_TEST_BATCH_RESULTS=/results/batch\n"

        monkeypatch.setattr(_FILESYSTEM, "_validate_step_environment", validate)
        monkeypatch.setattr(_FILESYSTEM, "_mixed_batch_command", command)
        monkeypatch.setattr(
            _FILESYSTEM, "assert_batch_submission_output", lambda *_a: None
        )


def _prepare(validate, sequencer=False):
    runtime = SimpleNamespace(
        scenario=_SPECS["kubectl-multi-target"],
        workspace="/w",
        values={},
    )
    return _FILESYSTEM._multi_target_prepare(  # pylint: disable=protected-access
        None,
        None,
        None,
        runtime,
        Path("t"),
        Path("l"),
        (),
        validate=validate,
        sequencer=sequencer,
    )


def test_prepare_validates_once_before_the_first_batch_command(monkeypatch):
    """validate_env.sh runs once, on the first step's environment."""
    recorder = _Prepare(monkeypatch)
    root, validation = _prepare(True)
    assert (root, validation) == ("/results/batch", "validated")
    assert recorder.events == [
        "_reset_result_base",
        "_sync_step_runtime",
        "validate",
        "command",
        "_sync_step_runtime",
        "command",
    ]


def test_prepare_checks_the_sequencer_only_when_asked(monkeypatch):
    """Only the cross-namespace scenario runs validate_env.sh --sequencer."""
    recorder = _Prepare(monkeypatch)
    _prepare(True, sequencer=True)
    assert recorder.events.count("validate+sequencer") == 1
    assert "validate" not in recorder.events


def test_prepare_does_not_validate_unless_asked(monkeypatch):
    """The unbound variant's claim cannot pass validation, so it never runs it."""
    recorder = _Prepare(monkeypatch)
    assert _prepare(False) == ("/results/batch", "")
    assert "validate" not in recorder.events


# --- mixed batch target provenance -------------------------------------------

_MIXED = _SPECS["mixed-batch"]
_NAME = _FILESYSTEM.MIXED_BATCH_TARGET_NAME
_TARGET_FILE = _FILESYSTEM.MIXED_BATCH_TARGET_FILE
_METADATA_OVERRIDE = "env-overrides/mixed-metadata.env"
_KUBECTL_ROOT = "integration-regression/x/primary"


def _mixed_runtime(selector):
    """Return a mixed-batch runtime whose roots render like the driver's."""
    if selector == "kubectl":
        values = {
            "test_root": f"{_KUBECTL_ROOT}/",
            "test_key": f"storage-test-rwx:{_KUBECTL_ROOT}",
            "test_key_secondary": "storage-test-rwx:integration-regression/x/secondary",
            "kubectl_claim": "storage-test-rwx",
        }
    else:
        values = {
            "test_root": "/mnt/storage-test/x/primary/",
            "test_key": "/mnt/storage-test/x/primary",
            "test_key_secondary": "/mnt/storage-test/x/secondary",
        }
    return SimpleNamespace(scenario=_MIXED, selector=selector, values=values)


def test_mixed_batch_expects_one_explicit_and_two_derived_names():
    """Only the metadata group's target file names its target."""
    rows = _FILESYSTEM._mixed_batch_group_rows(  # pylint: disable=protected-access
        _fixture(), _mixed_runtime("ssh")
    )
    derived = "/mnt/storage-test/x/primary"
    assert rows == (
        ("0001", "io", derived),
        ("0002", "mdtest", _NAME),
        ("0003", "io", derived),
    )
    kubectl = _FILESYSTEM._mixed_batch_group_rows(  # pylint: disable=protected-access
        _fixture(), _mixed_runtime("kubectl")
    )
    qualified = f"ns-one/storage-test-rwx:{_KUBECTL_ROOT}"
    assert [row[2] for row in kubectl] == [qualified, _NAME, qualified]


def test_mixed_batch_passes_its_target_with_target_and_workload_with_override():
    """The metadata append has one --target file and one workload --env-override."""
    metadata = _MIXED.steps[1]
    assert _FILESYSTEM._step_target_files(
        metadata
    ) == [  # pylint: disable=protected-access
        _TARGET_FILE
    ]
    assert _FILESYSTEM._step_override_files(
        metadata
    ) == [  # pylint: disable=protected-access
        _METADATA_OVERRIDE
    ]
    for step in (_MIXED.steps[0], _MIXED.steps[2]):
        assert not _FILESYSTEM._step_target_files(
            step
        )  # pylint: disable=protected-access


def test_mixed_batch_target_file_renders_a_qualified_kubernetes_root():
    """On Kubernetes the target root names its claim; elsewhere it is a path."""
    for selector, root in (
        ("kubectl", "storage-test-rwx:integration-regression/x/secondary"),
        ("ssh", "/mnt/storage-test/x/secondary"),
    ):
        files = (
            _FILESYSTEM._target_file_expectation(  # pylint: disable=protected-access
                _MIXED.steps[1], _mixed_runtime(selector).values
            )
        )
        assert files == {
            _TARGET_FILE: f'STORAGE_TARGET_NAME={_NAME}\nTEST_DIRS=(["{root}"]=1)\n'
        }


def _publication(group, name, *, groups=False, claim=False, legacy=False):
    lines = [f"STORAGE_SCALE_TEST_BATCH_GROUP={group}"]
    lines.append(f"STORAGE_SCALE_TEST_BATCH_GROUP_{group}_TARGET={name}")
    if legacy:
        lines.append(f"STORAGE_SCALE_TEST_BATCH_GROUP_TARGET={name}")
    if groups:
        lines.append(f"STORAGE_SCALE_TEST_BATCH_GROUPS={group}")
    if claim:
        lines.append(f"STORAGE_SCALE_TEST_BATCH_GROUP_{group}_CLAIM=a/b/c")
    return "\n".join(lines) + "\n"


def test_batch_group_publication_requires_its_own_target_and_no_claim():
    """A one-group append prints its target and no removed key, list or claim."""
    good = _publication("0002", _NAME)
    _FILESYSTEM.assert_batch_group_publication(good, "0002", _NAME)
    for output, message in (
        (good.replace("0002", "0003"), "did not name 0002"),
        (good.replace(_NAME, "other"), "printed group targets"),
        (_publication("0002", _NAME, claim=True), "claims"),
        (_publication("0002", _NAME, groups=True), "_GROUPS"),
        (_publication("0002", _NAME, legacy=True), "removed GROUP_TARGET"),
        ("", "lacks one safe"),
    ):
        with pytest.raises(_Error, match=message):
            _FILESYSTEM.assert_batch_group_publication(output, "0002", _NAME)


def test_group_status_keys_cover_every_group():
    """Kind and target for each group, from the rows rather than a claim object."""
    rows = (("0001", "io", "/a"), ("0002", "mdtest", _NAME))
    output = (
        "STATE=NOT_STARTED\nGROUP_0001_KIND=io\nGROUP_0001_TARGET=/a\n"
        f"GROUP_0002_KIND=mdtest\nGROUP_0002_TARGET={_NAME}\n"
    )
    _FILESYSTEM.assert_group_status_keys(output, rows)
    with pytest.raises(_Error, match="status group 0001"):
        _FILESYSTEM.assert_group_status_keys(output.replace("/a", "/b"), rows)
    with pytest.raises(_Error, match="GROUP_0002_KIND"):
        _FILESYSTEM.assert_group_status_keys(output.split("GROUP_0002")[0], rows)


_INDEX = (
    "# Filesystem batch reports\n\n"
    "| Group | Kind | Target | Saved settings | Cell states | Reports |\n"
    "| --- | --- | --- | --- | --- | --- |\n"
    "| 0001 | io | /a | x | SUCCESS: 1 | y |\n"
    f"| 0002 | mdtest | {_NAME} | x | SUCCESS: 1 | y |\n"
)


def test_report_index_must_name_each_groups_target():
    """The Target column is checked row by row, not by substring."""
    rows = (("0001", "io", "/a"), ("0002", "mdtest", _NAME))
    _FILESYSTEM.assert_report_index_targets(_INDEX, rows)
    swapped = (("0001", "io", _NAME), ("0002", "mdtest", "/a"))
    with pytest.raises(_Error, match="no row for group 0001"):
        _FILESYSTEM.assert_report_index_targets(_INDEX, swapped)
    with pytest.raises(_Error, match="Target column"):
        _FILESYSTEM.assert_report_index_targets("| Group | Kind |\n", rows)


def _digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _snapshot(name, files, target=None):
    """Return a parsed env_used.yaml: workload override files and a target file."""
    document = {"STORAGE_TARGET_NAME": name, "env_override": None, "target_file": None}
    if files:
        document["env_override"] = {
            "file": files[-1][0],
            "sha256": "0",
            "variables": [],
            "files": [
                {"file": path, "sha256": "0", "variables": variables}
                for path, variables in files
            ],
        }
    if target:
        path, content, variables = target
        document["target_file"] = {
            "file": path,
            "sha256": _digest(content),
            "variables": variables,
        }
    return yaml.safe_load(yaml.safe_dump(document))


def test_override_files_are_listed_in_order_and_by_suffix():
    """Recorded absolute paths must end with the relative files, in order."""
    snapshot = _snapshot(
        _NAME,
        [
            ("/w/env-overrides/mixed-metadata.env", ["MDTEST_ITERATIONS"]),
            ("/w/env-overrides/other.env", ["MDTEST_BRANCH_FACTOR"]),
        ],
    )
    expected = ["env-overrides/mixed-metadata.env", "env-overrides/other.env"]
    _FILESYSTEM.assert_env_override_files(snapshot, expected, "group")
    with pytest.raises(_Error, match="in that order"):
        _FILESYSTEM.assert_env_override_files(snapshot, expected[::-1], "group")
    with pytest.raises(_Error, match="in that order"):
        _FILESYSTEM.assert_env_override_files(snapshot, expected[:1], "group")
    _FILESYSTEM.assert_env_override_files(_snapshot("/a", []), [], "group")
    with pytest.raises(_Error, match="in that order"):
        _FILESYSTEM.assert_env_override_files(
            _snapshot("/a", []), expected[:1], "group"
        )


_CONTENT = f'STORAGE_TARGET_NAME={_NAME}\nTEST_DIRS=(["/m/secondary"]=1)\n'
_VARIABLES = ["STORAGE_TARGET_NAME", "TEST_DIRS"]
_RECORD = ("/w/targets/mixed-target.env", _CONTENT, _VARIABLES)
_EXPECTED_RECORD = (_TARGET_FILE, _CONTENT, {"STORAGE_TARGET_NAME", "TEST_DIRS"})


def test_target_file_record_matches_file_digest_and_variables():
    """The recorded path, SHA-256 and changed variables must all be the target's."""
    check = _FILESYSTEM.assert_target_file_record
    check(_snapshot(_NAME, [], _RECORD), _EXPECTED_RECORD, "group")
    check(_snapshot("/a", []), None, "group")
    for bad, message in (
        (_snapshot(_NAME, []), "no target_file"),
        (_snapshot(_NAME, [], ("/w/targets/other.env", _CONTENT, _VARIABLES)), "file"),
        (_snapshot(_NAME, [], (_RECORD[0], "changed", _VARIABLES)), "sha256"),
        (_snapshot(_NAME, [], (_RECORD[0], _CONTENT, ["TEST_DIRS"])), "variables"),
    ):
        with pytest.raises(_Error, match=message):
            check(bad, _EXPECTED_RECORD, "group")
    with pytest.raises(_Error, match="expected null"):
        check(_snapshot(_NAME, [], _RECORD), None, "group")


def _write_mixed_result(root, names, snapshots):
    manifest = ["version\t1"]
    for index, (name, snapshot) in enumerate(zip(names, snapshots), start=1):
        relative = f"groups/{index:04d}/out"
        manifest.append(f"group\t{index:04d}\tio\t{relative}\ta\tb")
        directory = root / relative
        directory.mkdir(parents=True)
        document = dict(snapshot, STORAGE_TARGET_NAME=name)
        (directory / "env_used.yaml").write_text(
            yaml.safe_dump(document), encoding="utf-8"
        )
    (root / "batch-manifest.tsv").write_text("\n".join(manifest) + "\n")


def _mixed_snapshots(runtime, names, *, target_file=True, metadata_files=None):
    """Return each group's snapshot: only group 0002 has a target file."""
    content = _FILESYSTEM._target_file_expectation(  # pylint: disable=protected-access
        _MIXED.steps[1], runtime.values
    )[_TARGET_FILE]
    target = ("/w/" + _TARGET_FILE, content, _VARIABLES) if target_file else None
    workload = metadata_files or [
        ("/w/env-overrides/mixed-metadata.env", ["MDTEST_ITERATIONS"])
    ]
    return [
        _snapshot(names[0], []),
        _snapshot(names[1], workload, target),
        _snapshot(
            names[2],
            [("/w/env-overrides/mixed-repeated-io.env", ["ELBENCHO_FILE_SIZE"])],
        ),
    ]


def test_mixed_batch_snapshots_record_names_target_file_and_workload_overrides(
    tmp_path,
):
    """Group 0002 records its explicit name, its target file, and its workload file."""
    runtime = _mixed_runtime("ssh")
    rows = _FILESYSTEM._mixed_batch_group_rows(  # pylint: disable=protected-access
        _fixture(), runtime
    )
    names = [row[2] for row in rows]
    check = _FILESYSTEM._assert_mixed_batch_targets  # pylint: disable=protected-access
    _write_mixed_result(tmp_path, names, _mixed_snapshots(runtime, names))
    check(tmp_path, runtime, rows)
    for label, kwargs, message in (
        ("name", {}, "group 0002 env_used.yaml records"),
        ("no-target", {"target_file": False}, "no target_file"),
        (
            "target-as-override",
            {
                "target_file": False,
                "metadata_files": [
                    ("/w/targets/mixed-target.env", ["TEST_DIRS"]),
                    ("/w/env-overrides/mixed-metadata.env", ["MDTEST_ITERATIONS"]),
                ],
            },
            "in that order",
        ),
    ):
        broken = tmp_path / label
        broken.mkdir()
        recorded = list(names)
        if label == "name":
            recorded[1] = "other"
        _write_mixed_result(
            broken, recorded, _mixed_snapshots(runtime, names, **kwargs)
        )
        with pytest.raises(_Error, match=message):
            check(broken, runtime, rows)


def test_mixed_batch_legacy_override_scalar_is_the_workload_file(tmp_path):
    """The saved scalar names the workload recipe; a target file is not an override."""
    metadata = _MIXED.steps[1]
    assert _FILESYSTEM._step_override_files(
        metadata
    ) == [  # pylint: disable=protected-access
        "env-overrides/mixed-metadata.env",
    ]
    definition = tmp_path / "executions"
    definition.mkdir()
    (definition / "0002.sh").write_text(
        "STORAGE_SCALE_TEST_ENV_OVERRIDE_FILE=/w/env-overrides/mixed-metadata.env\n"
    )
    check = (
        _FILESYSTEM._assert_mixed_batch_overrides
    )  # pylint: disable=protected-access
    check(tmp_path, [["execution", "0002"]], (metadata,))
    (definition / "0002.sh").write_text(
        "STORAGE_SCALE_TEST_ENV_OVERRIDE_FILE=/w/targets/mixed-target.env\n"
    )
    with pytest.raises(_Error, match="recorded override"):
        check(tmp_path, [["execution", "0002"]], (metadata,))


# --- one-namespace attempt objects ----------------------------------------


def _pod(claims, *, init=0, mounts=None, extra_volume=True):
    """Return a Pod spec mounting each claim in its own directory."""
    volumes = [
        {"name": f"v{index}", "persistentVolumeClaim": {"claimName": claim}}
        for index, claim in enumerate(claims)
    ]
    if extra_volume:
        volumes.append({"name": "scratch", "emptyDir": {}})
    mounts = mounts or [
        {"name": f"v{index}", "mountPath": f"/mnt/storage-scale-test/{claim}"}
        for index, claim in enumerate(claims)
    ]
    return {
        "initContainers": [{"name": f"i{n}"} for n in range(init)],
        "containers": [{"volumeMounts": mounts}],
        "volumes": volumes,
    }


def _workload(pod):
    return {"items": [{"spec": {"template": {"spec": pod}}}]}


_CLAIMS = ("storage-test-rwx", "storage-test-rwx-2")


def test_one_coordinator_job_mounts_each_claim_in_its_own_directory():
    """A plain Job: no init containers, one container, a directory per claim."""
    _FILESYSTEM.assert_single_coordinator_job(_workload(_pod(_CLAIMS)), _CLAIMS)
    _FILESYSTEM.assert_single_worker_daemonset(_workload(_pod(_CLAIMS)), _CLAIMS)


@pytest.mark.parametrize(
    "document, message",
    (
        ({"items": []}, "found 0"),
        ({"items": [{}, {}]}, "found 2"),
        (_workload(_pod(_CLAIMS, init=1)), "1 init containers"),
        (_workload(_pod(_CLAIMS[:1])), "mounts claims"),
        (
            _workload(
                _pod(
                    _CLAIMS,
                    mounts=[
                        {"name": "v0", "mountPath": "/mnt/storage-scale-test"},
                        {"name": "v1", "mountPath": "/mnt/storage-scale-test/x"},
                    ],
                )
            ),
            "mounts claims",
        ),
        (_workload(_pod((*_CLAIMS, "storage-test-extra"))), "mounts claims"),
    ),
)
def test_coordinator_job_shapes_that_do_not_mount_every_claim_fail(document, message):
    """A chain of init containers or a claim left at the old mount is rejected."""
    with pytest.raises(_Error, match=message):
        _FILESYSTEM.assert_single_coordinator_job(document, _CLAIMS)


def test_worker_daemonset_must_mount_every_claim():
    """The workers need both claims too."""
    with pytest.raises(_Error, match="worker DaemonSet"):
        _FILESYSTEM.assert_single_worker_daemonset({"items": []}, _CLAIMS)
    with pytest.raises(_Error, match="worker Pod mounts claims"):
        _FILESYSTEM.assert_single_worker_daemonset(
            _workload(_pod(_CLAIMS[:1])), _CLAIMS
        )


def test_only_sequenced_attempts_create_rbac_objects():
    """ServiceAccounts, Roles, and RoleBindings are the sequencer's alone."""
    owned = [
        "job.batch/sst-elb-x-sweep",
        "lease.coordination.k8s.io/x",
        "serviceaccount/sst-elb-x-sequencer",
        "role.rbac.authorization.k8s.io/x-t0",
        "rolebinding.rbac.authorization.k8s.io/x-t0",
    ]
    assert _FILESYSTEM.sequencer_rbac_objects(owned) == owned[2:]
    assert not _FILESYSTEM.sequencer_rbac_objects(owned[:2])


class _SameRunner:
    def __init__(self, coordinators, workers, owned, sequencers=""):
        self.coordinators = coordinators
        self.workers = workers
        self.owned = owned
        self.sequencers = sequencers

    def run(self, arguments, **_kwargs):
        command = [str(item) for item in arguments]
        selector = command[command.index("-l") + 1]
        if "component=coordinator" in selector:
            document = self.coordinators
        elif "component=workers" in selector:
            document = self.workers
        elif "component=sequencer" in selector:
            return SimpleNamespace(returncode=0, stdout=self.sequencers, stderr="")
        else:
            return SimpleNamespace(
                returncode=0, stdout="\n".join(self.owned), stderr=""
            )
        return SimpleNamespace(returncode=0, stdout=json.dumps(document), stderr="")


def _aggregate_groups():
    runtime = SimpleNamespace(
        scenario=_SPECS["kubectl-same-namespace-targets"],
        values=_FILESYSTEM._make_scenario_runtime(  # pylint: disable=protected-access
            _fixture(),
            _SPECS["kubectl-same-namespace-targets"],
            "kubectl",
            Path("b"),
            Path("a"),
            "run-1",
        ).values,
    )
    return _FILESYSTEM.expected_multi_target_groups(
        runtime.scenario.steps, runtime.values, "ns-one"
    )


def test_same_namespace_attempt_has_one_job_one_daemonset_and_no_sequencer():
    """The attempt's objects are checked against the one-namespace layout."""
    check = (
        _FILESYSTEM._assert_same_namespace_objects
    )  # pylint: disable=protected-access
    good = _workload(_pod(_CLAIMS))
    groups = _aggregate_groups()
    check(_SameRunner(good, good, ["job.batch/x"]), _CONFIG, groups, _ATTEMPT)
    with pytest.raises(_Error, match="sequencer"):
        check(_SameRunner(good, good, ["serviceaccount/x"]), _CONFIG, groups, _ATTEMPT)
    with pytest.raises(_Error, match="sequencer"):
        check(
            _SameRunner(good, good, ["job.batch/x"], sequencers="job.batch/seq"),
            _CONFIG,
            groups,
            _ATTEMPT,
        )
    with pytest.raises(_Error, match="exactly one coordinator"):
        check(_SameRunner({"items": []}, good, []), _CONFIG, groups, _ATTEMPT)
    with pytest.raises(_Error, match="worker Pod mounts claims"):
        check(
            _SameRunner(good, _workload(_pod(_CLAIMS[:1])), []),
            _CONFIG,
            groups,
            _ATTEMPT,
        )


class _CountRunner:
    """Answer each ``get <kinds> -l <selector>`` with a scripted object count."""

    def __init__(self, counts):
        self.counts = counts
        self.queries = []

    def run(self, arguments, **_kwargs):
        command = [str(item) for item in arguments]
        namespace = command[command.index("-n") + 1]
        component = command[command.index("-l") + 1].rsplit("=", 1)[1]
        self.queries.append((namespace, component))
        count = self.counts.get((namespace, component), 0)
        names = "\n".join(f"object/{index}" for index in range(count))
        return SimpleNamespace(returncode=0, stdout=names, stderr="")


def test_attempt_objects_expect_a_lease_per_claim_and_a_coordinator_per_namespace():
    """The aggregate namespace holds two Leases, one Job, and no sequencer."""
    check = _FILESYSTEM._assert_multi_target_objects  # pylint: disable=protected-access
    groups = _aggregate_groups()
    good = {("ns-one", "pvc-lock"): 2, ("ns-one", "coordinator"): 1}
    check(_CountRunner(good), _CONFIG, groups, _ATTEMPT)
    with pytest.raises(_Error, match="1 pvc-lock"):
        check(
            _CountRunner({**good, ("ns-one", "pvc-lock"): 1}), _CONFIG, groups, _ATTEMPT
        )
    with pytest.raises(_Error, match="1 sequencer"):
        check(
            _CountRunner({**good, ("ns-one", "sequencer"): 1}),
            _CONFIG,
            groups,
            _ATTEMPT,
        )


def test_sequenced_attempt_objects_put_the_sequencer_in_the_first_namespace():
    """Two namespaces: a Lease and Job each, and one sequencer in the first."""
    runtime = SimpleNamespace(
        scenario=_SPECS["kubectl-multi-target"],
        values=_FILESYSTEM._make_scenario_runtime(  # pylint: disable=protected-access
            _fixture(),
            _SPECS["kubectl-multi-target"],
            "kubectl",
            Path("b"),
            Path("a"),
            "run-1",
        ).values,
    )
    groups = _FILESYSTEM.expected_multi_target_groups(
        runtime.scenario.steps, runtime.values, "ns-one"
    )
    check = _FILESYSTEM._assert_multi_target_objects  # pylint: disable=protected-access
    good = {
        ("ns-one", "pvc-lock"): 1,
        ("ns-one", "coordinator"): 1,
        ("ns-one", "sequencer"): 1,
        ("ns-two", "pvc-lock"): 1,
        ("ns-two", "coordinator"): 1,
    }
    check(_CountRunner(good), _CONFIG, groups, _ATTEMPT)
    with pytest.raises(_Error, match="ns-two holds 1 sequencer"):
        check(
            _CountRunner({**good, ("ns-two", "sequencer"): 1}),
            _CONFIG,
            groups,
            _ATTEMPT,
        )


# --- claim-qualified locations and the control root --------------------------


@pytest.mark.parametrize(
    "value, parts",
    (
        ("storage-test-rwx:a/b", (None, "storage-test-rwx", "a/b")),
        ("ns-two/storage-test-rwx:a", ("ns-two", "storage-test-rwx", "a")),
        ("/mnt/storage-scale-test/claim-2/a/b", (None, "claim-2", "a/b")),
        ("/mnt/storage-scale-test/claim-2", (None, "claim-2", "")),
    ),
)
def test_kubectl_locations_parse_logical_and_pod_forms(value, parts):
    """The one parser accepts ``[ns/]claim:path`` and ``<mount>/<claim>/<path>``."""
    assert _FILESYSTEM.parse_kubectl_location(value) == parts


@pytest.mark.parametrize("value", ("plain/path", "claim:a/../b", "Claim:a", ""))
def test_kubectl_locations_reject_unqualified_or_escaping_paths(value):
    """A path with no claim, or one leaving the claim, is not a location."""
    with pytest.raises(_Error, match="claim-qualified"):
        _FILESYSTEM.parse_kubectl_location(value)


def test_logical_and_storage_paths_use_the_per_claim_mount():
    """Single-root modes take ``claim:path``; the utility Pod sees the mount path."""
    assert (
        _FILESYSTEM._kubectl_logical_path(  # pylint: disable=protected-access
            "/mnt/storage-scale-test/storage-test-rwx/a/b"
        )
        == "storage-test-rwx:a/b"
    )
    assert (
        _FILESYSTEM._kubectl_logical_path(
            "ns-one/c:x/y"
        )  # pylint: disable=protected-access
        == "ns-one/c:x/y"
    )
    assert _FILESYSTEM.kubectl_storage_path("c:x/y") == "/mnt/storage-scale-test/c/x/y"
    assert (
        _FILESYSTEM.kubectl_storage_path("/mnt/storage-scale-test/c/x")
        == "/mnt/storage-scale-test/c/x"
    )
    with pytest.raises(_Error, match="claim-qualified"):
        _FILESYSTEM._kubectl_logical_path("x/y")  # pylint: disable=protected-access


def test_control_root_is_on_the_claim_of_the_lexically_first_root():
    """``claim-2`` sorts before ``claim`` because ``-`` precedes ``:``."""
    roots = [
        "ns/storage-test-rwx:run/primary",
        "ns/storage-test-rwx-2:run/secondary",
        "ns/storage-test-rwx-2:run/primary",
    ]
    assert _FILESYSTEM.kubectl_control_root(roots) == (
        "/mnt/storage-scale-test/storage-test-rwx-2/run/primary/.storage-scale-test"
    )
    assert _FILESYSTEM.kubectl_control_root(roots[:1]) == (
        "/mnt/storage-scale-test/storage-test-rwx/run/primary/.storage-scale-test"
    )


def test_single_claim_control_root_uses_the_primary_root_unless_recorded():
    """Single-claim scenarios derive it; the aggregate scenario records its own."""
    runtime = SimpleNamespace(
        values={"test_root": "run/primary", "test_root_secondary": "run/secondary"}
    )
    derive = (
        _FILESYSTEM._kubectl_remote_control_root
    )  # pylint: disable=protected-access
    assert derive(runtime) == (
        "/mnt/storage-scale-test/storage-test-rwx/run/primary/.storage-scale-test"
    )
    runtime.values["kubectl_control_root"] = "/mnt/storage-scale-test/c/.x"
    assert derive(runtime) == "/mnt/storage-scale-test/c/.x"


class _LeaseRunner:
    def __init__(self, stdout):
        self.stdout = stdout
        self.commands = []

    def run(self, arguments, **_kwargs):
        self.commands.append([str(item) for item in arguments])
        return SimpleNamespace(returncode=0, stdout=self.stdout, stderr="")


def test_lease_check_finds_leases_by_the_attempts_label_not_its_identity_file():
    """Present means one Lease per claim; absent means none, from the label alone."""
    check = _FILESYSTEM._assert_kubectl_pvc_lease  # pylint: disable=protected-access
    runner = _LeaseRunner("uid-1\nuid-2\n")
    check(runner, _CONFIG, Path("/nonexistent"), _ATTEMPT, present=True, claims=2)
    selector = runner.commands[0][runner.commands[0].index("-l") + 1]
    assert selector.endswith("component=pvc-lock")
    assert _ATTEMPT in selector
    with pytest.raises(_Error, match="incomplete"):
        check(runner, _CONFIG, Path("/x"), _ATTEMPT, present=True, claims=1)
    with pytest.raises(_Error, match="retained"):
        check(runner, _CONFIG, Path("/x"), _ATTEMPT, present=False)
    check(_LeaseRunner(""), _CONFIG, Path("/x"), _ATTEMPT, present=False)


def test_dataset_cleanup_runs_in_a_pod_mounting_every_claim_the_paths_name(
    monkeypatch,
):
    """Generated directories on two claims are probed in one two-claim utility Pod."""
    calls = []
    monkeypatch.setattr(
        _FILESYSTEM,
        "_storage_utility_shell",
        lambda _r, _c, _f, command, **kwargs: calls.append((command, kwargs)) or "",
    )
    result = Path("/results")
    monkeypatch.setattr(
        _FILESYSTEM,
        "_execution_targets",
        lambda _result: ("storage-test-rwx:a/d-1", "storage-test-rwx-2:b/d-1"),
    )
    step = SimpleNamespace(dataset=_FILESYSTEM.DatasetExpectation.CLEANED)
    _FILESYSTEM._assert_dataset_state(  # pylint: disable=protected-access
        None, None, _fixture(), step, result, None, "/mnt/storage-scale-test"
    )
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert kwargs["claims"] == ["storage-test-rwx", "storage-test-rwx-2"]
    assert "test ! -e /mnt/storage-scale-test/storage-test-rwx/a/d-1" in command
    assert "test ! -e /mnt/storage-scale-test/storage-test-rwx-2/b/d-1" in command


def test_retained_dataset_is_probed_at_its_qualified_path(monkeypatch):
    """A retained ``claim:path`` is probed under that claim's mount."""
    calls = []
    monkeypatch.setattr(
        _FILESYSTEM,
        "_storage_utility_shell",
        lambda _r, _c, _f, command, **kwargs: calls.append((command, kwargs)) or "",
    )
    step = SimpleNamespace(dataset=_FILESYSTEM.DatasetExpectation.PRESERVED)
    _FILESYSTEM._assert_dataset_state(  # pylint: disable=protected-access
        None,
        None,
        _fixture(),
        step,
        None,
        "storage-test-rwx:run/d-1",
        "/mnt/storage-scale-test",
        "worker-a",
    )
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert kwargs == {"claims": ["storage-test-rwx"], "node_name": "worker-a"}
    assert "/mnt/storage-scale-test/storage-test-rwx/run/d-1" in command


def test_same_namespace_scenario_is_registered_for_kubectl_only():
    """It is plannable on Kubernetes and has an explicit adapter."""
    name = "kubectl-same-namespace-targets"
    assert name in _FILESYSTEM.KUBECTL_MULTI_TARGET_SCENARIOS
    assert name in _FILESYSTEM.KUBECTL_SUPPORTED_SCENARIOS
    for substrate in ("ssh", "slurm"):
        planned = {
            step.scenario.name
            for step in _FILESYSTEM.plan_scenarios(substrate=substrate)
            if hasattr(step, "scenario")
        }
        assert name not in planned
    planned = [
        step.scenario.name for step in _FILESYSTEM.plan_scenarios(substrate="kubectl")
    ]
    assert planned.count(name) == 1
    assert planned.index(name) > planned.index("kubectl-multi-target-unbound")


# --- migration errors (product shell, once the claims module exists) ---------

_CLAIMS_MODULE = Path(__file__).resolve().parent.parent / "lib" / "_kubectl_claims.sh"


def _validate_test_dirs(script):
    """Run the product's validator over an env snippet; return (status, output)."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{_CLAIMS_MODULE}"\n{script}\nkubectl_validate_test_dirs',
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return result.returncode, result.stdout + result.stderr


@pytest.mark.skipif(
    not _CLAIMS_MODULE.is_file(), reason="lib/_kubectl_claims.sh is not present"
)
def test_kubectl_pvc_is_rejected_with_the_exact_replacement_line():
    """KUBECTL_PVC fails before anything runs and prints the qualified root."""
    status, output = _validate_test_dirs(
        "KUBECTL_NAMESPACE=ns; KUBECTL_PVC=scale-test; KUBECTL_PV=pv-1\n"
        'declare -A TEST_DIRS=(["data"]=1)'
    )
    assert status != 0
    assert 'TEST_DIRS=(["scale-test:data"]=1)' in output


@pytest.mark.skipif(
    not _CLAIMS_MODULE.is_file(), reason="lib/_kubectl_claims.sh is not present"
)
def test_unqualified_key_is_rejected_and_a_qualified_key_is_accepted():
    """A key without ``claim:`` fails; ``claim:path`` passes."""
    status, _ = _validate_test_dirs(
        'KUBECTL_NAMESPACE=ns\ndeclare -A TEST_DIRS=(["data"]=1)'
    )
    assert status != 0
    status, output = _validate_test_dirs(
        'KUBECTL_NAMESPACE=ns\ndeclare -A TEST_DIRS=(["scale-test:data"]=1)'
    )
    assert status == 0, output
