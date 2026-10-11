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

"""Contracts for the multi-target Kubernetes integration fixture and scenarios."""

import hashlib
import json
import re
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.integration_scenario_test_helpers import (
    load_integration_driver,
    load_integration_lib,
)
from tests.kubectl_segment_test_helpers import DEFAULT_SEQUENCER_IMAGE

_DRIVER = load_integration_driver()
_FILESYSTEM = load_integration_lib("filesystem_integration")
fixture_images = load_integration_lib("fixture_images")
fixture_storage = load_integration_lib("fixture_storage")

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SPECS = _FILESYSTEM.SCENARIO_SPECS_BY_NAME
_DIGEST_SUFFIX = re.compile(r"@sha256:[0-9a-f]{64}$")
_SEQUENCER = "docker.io/library/storage-scale-integration-kubectl@sha256:" + "b" * 64
_FIRST = fixture_storage.KubectlStorageTarget("ns-one", "pv-one", "storage-test-rwx")
_SECOND = fixture_storage.KubectlStorageTarget("ns-two", "pv-two", "storage-test-rwx")
_SAME = fixture_storage.KubectlStorageTarget("ns-one", "pv-three", "storage-test-rwx-2")


def _config(tmp_path):
    """Return a minimal real driver configuration."""
    return _DRIVER.Config(
        cluster_name="test-cluster",
        namespace="test-namespace",
        state_dir=tmp_path / "state",
        export_dir=tmp_path / "export",
        storage_backend="sbx-shared",
        sbx_shared_root=_REPO_ROOT / "tmp" / "test-shared",
        test_user="tester",
        test_uid=42424,
        test_gid=43434,
        verbose=False,
    )


def _fixture(**overrides):
    """Return the fixture fields the env generator and scenario adapters read."""
    values = {
        "architecture": "x86_64",
        "kubectl_nodes": ("worker-a", "worker-b"),
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


# --- pinned sequencer image -------------------------------------------------


@pytest.mark.parametrize("backend", ("nfs", "sbx-shared"))
def test_sequencer_image_is_digest_pinned_per_backend(backend):
    """The product requires a digest-qualified sequencer image reference."""
    image = fixture_images.kubectl_sequencer_fixture(backend)
    assert _DIGEST_SUFFIX.search(image.upstream)
    assert _DIGEST_SUFFIX.search(image.fixture)
    assert image.upstream.rpartition("@")[2] == image.fixture.rpartition("@")[2]
    assert image.upstream.startswith("alpine/kubectl:")
    assert image.component == "kubectl-sequencer"


def test_sequencer_image_is_the_product_default_for_every_profile():
    """Both profiles preload the product's default image; unknown ones fail."""
    nfs = fixture_images.kubectl_sequencer_fixture("nfs")
    sbx = fixture_images.kubectl_sequencer_fixture("sbx-shared")
    assert nfs == sbx
    assert f"docker.io/{nfs.upstream}" == DEFAULT_SEQUENCER_IMAGE
    with pytest.raises(ValueError, match="no sequencer image"):
        fixture_images.kubectl_sequencer_fixture("other")


def test_sequencer_image_is_preloaded_under_its_digest_alias(tmp_path, monkeypatch):
    """Host Docker verifies the image and every node holds the digest alias."""
    config = _config(tmp_path)
    commands, loaded = [], []

    class _Runner:
        def run(self, arguments, **_kwargs):
            commands.append([str(item) for item in arguments])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    wanted = fixture_images.kubectl_sequencer_fixture("sbx-shared")
    monkeypatch.setattr(
        _DRIVER, "_kind_containers", lambda *_a, **_k: ["control", "w1", "w2"]
    )
    monkeypatch.setattr(
        _DRIVER,
        "_ensure_pinned_image",
        lambda _runner, image: (
            image
            if image == wanted.upstream
            else pytest.fail(f"unexpected image {image}")
        ),
    )
    monkeypatch.setattr(
        _DRIVER,
        "_load_image_into_nodes",
        lambda _runner, image, nodes, *, destination=None: loaded.append(
            (image, nodes, destination)
        ),
    )

    _DRIVER._prepare_kubectl_sequencer_image(  # pylint: disable=protected-access
        _Runner(), config, "sbx-shared"
    )

    assert [(nodes, alias) for _image, nodes, alias in loaded] == [
        (["control", "w1", "w2"], wanted.fixture)
    ]
    assert _DIGEST_SUFFIX.search(wanted.fixture)


# --- second storage target identities ---------------------------------------


def test_second_namespace_is_a_valid_distinct_label():
    """Both namespaces must be DNS-1123 labels and differ."""
    namespace = fixture_storage.second_namespace("storage-scale-integration")
    assert namespace == "storage-scale-integration-target-2"
    with pytest.raises(fixture_storage.StorageTargetError):
        fixture_storage.second_namespace("x" * 60)
    with pytest.raises(fixture_storage.StorageTargetError):
        fixture_storage.second_namespace("Not_Valid")


def test_storage_target_keys_and_names_match_the_product_defaults():
    """A root names its claim; the default target name adds the namespace."""
    assert _FIRST.key("data/primary") == "storage-test-rwx:data/primary"
    assert (
        _FIRST.key("data/primary", with_namespace=True)
        == "ns-one/storage-test-rwx:data/primary"
    )
    assert _FIRST.default_name("data/primary") == "ns-one/storage-test-rwx:data/primary"
    with pytest.raises(fixture_storage.StorageTargetError):
        fixture_storage.KubectlStorageTarget("ns/x", "pv", "pvc")


def _evaluate(lines, *names):
    """Evaluate env lines in Bash and print the named variables."""
    script = (
        "\n".join(lines) + "\n" + "".join(f'echo "{name}=${name}"\n' for name in names)
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def test_env_lines_select_only_the_default_namespace():
    """Roots name their claims, so env.sh never sets a PV, claim, or target list."""
    lines = fixture_storage.kubectl_target_env_lines("ns-one")
    assert lines == ("export KUBECTL_NAMESPACE=ns-one",)
    assert _evaluate(lines, "KUBECTL_NAMESPACE") == {"KUBECTL_NAMESPACE": "ns-one"}
    for removed in ("KUBECTL_PV", "KUBECTL_PVC", "KUBECTL_STORAGE_TARGETS"):
        assert not any(removed in line for line in lines)


def test_env_lines_add_the_sequencer_image_only_when_given():
    """The image is for batches spanning namespaces and must be digest-pinned."""
    lines = fixture_storage.kubectl_target_env_lines("ns-one", _SEQUENCER)
    assert lines[0] == "export KUBECTL_NAMESPACE=ns-one"
    assert _evaluate(lines, "KUBECTL_SEQUENCER_IMAGE") == {
        "KUBECTL_SEQUENCER_IMAGE": _SEQUENCER
    }


@pytest.mark.parametrize(
    "namespace, image, message",
    (
        ("ns-one", "docker.io/example/kubectl:1", "digest-qualified"),
        ("Not_Valid", "", "invalid namespace"),
        ("", "", "invalid namespace"),
    ),
)
def test_env_lines_reject_invalid_selections(namespace, image, message):
    """A scenario cannot emit a namespace or image the product would reject."""
    with pytest.raises(fixture_storage.StorageTargetError, match=message):
        fixture_storage.kubectl_target_env_lines(namespace, image)


def test_override_block_selects_the_sequencer_image_for_every_scenario(tmp_path):
    """Every kubectl env names the preloaded alias so validate_env.sh's image
    check pulls it under the Never pull policy, single-target or not."""
    single, _ = _FILESYSTEM._override_block(  # pylint: disable=protected-access
        "kubectl", str(tmp_path), _fixture()
    )
    multi, _ = _FILESYSTEM._override_block(  # pylint: disable=protected-access
        "kubectl", str(tmp_path), _fixture(), multi_target=True
    )
    for block in (single, multi):
        assert "export KUBECTL_NAMESPACE=ns-one" in block
        assert f"export KUBECTL_SEQUENCER_IMAGE={_SEQUENCER}" in block
        assert "export KUBECTL_IMAGE_PULL_POLICY=Never" in block
        assert "KUBECTL_PV" not in block
        assert "KUBECTL_STORAGE_TARGETS" not in block
        assert "declare -A TEST_DIRS=([storage-test-rwx:primary]=1)" in block


def test_every_kubectl_scenario_env_sets_the_sequencer_image(tmp_path):
    """No kubectl scenario relies on the product's default (unloaded) image."""
    scenarios = [spec for spec in _SPECS.values() if "kubectl" in spec.substrates]
    assert len(scenarios) > 2
    for spec in scenarios:
        block, _ = _FILESYSTEM._override_block(  # pylint: disable=protected-access
            "kubectl",
            str(tmp_path),
            _fixture(),
            multi_target=spec.storage_targets > 1,
            same_namespace=spec.same_namespace,
        )
        assert f"export KUBECTL_SEQUENCER_IMAGE={_SEQUENCER}" in block, spec.name


def test_kubectl_env_without_a_fixture_sequencer_image_is_rejected(tmp_path):
    """A single-target environment also needs the preloaded image."""
    with pytest.raises(fixture_storage.StorageTargetError, match="digest-qualified"):
        _FILESYSTEM._override_block(  # pylint: disable=protected-access
            "kubectl", str(tmp_path), _fixture(kubectl_sequencer_image="")
        )


def test_multi_target_mode_requires_a_second_target(tmp_path):
    """A fixture set up before multi-target support names the remedy."""
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="re-run"):
        _FILESYSTEM._override_block(  # pylint: disable=protected-access
            "kubectl",
            str(tmp_path),
            _fixture(kubectl_targets=(_FIRST,)),
            multi_target=True,
        )


def test_scenario_storage_targets_default_to_the_primary_claim():
    """Single-target scenarios keep addressing the primary claim implicitly."""
    runtime = SimpleNamespace(scenario=_SPECS["baseline"])
    assert _FILESYSTEM._scenario_storage_targets(  # pylint: disable=protected-access
        _fixture(), runtime
    ) == (None,)
    multi = SimpleNamespace(scenario=_SPECS["kubectl-multi-target"])
    targets = _FILESYSTEM._scenario_storage_targets(  # pylint: disable=protected-access
        _fixture(), multi
    )
    assert targets == (_FIRST, _SECOND)
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="re-run"):
        _FILESYSTEM._scenario_storage_targets(  # pylint: disable=protected-access
            _fixture(kubectl_targets=(_FIRST,)), multi
        )


class _ClaimRunner:
    """Answer the second namespace's claim query."""

    def __init__(self, stdout):
        self.stdout = stdout
        self.commands = []

    def run(self, arguments, **_kwargs):
        self.commands.append([str(item) for item in arguments])
        return SimpleNamespace(returncode=0, stdout=self.stdout, stderr="")


def _claim(phase, volume):
    return json.dumps({"status": {"phase": phase}, "spec": {"volumeName": volume}})


def test_second_storage_target_is_read_from_its_bound_claim():
    """PV names come from the bound claim, as for the primary target."""
    config = SimpleNamespace(
        kubeconfig=Path("kubeconfig"), namespace="storage-scale-integration"
    )
    runner = _ClaimRunner(_claim("Bound", "pvc-123"))
    target = _FILESYSTEM._second_storage_target(  # pylint: disable=protected-access
        runner, config
    )
    assert target == fixture_storage.KubectlStorageTarget(
        "storage-scale-integration-target-2", "pvc-123", "storage-test-rwx"
    )
    assert "storage-scale-integration-target-2" in runner.commands[0]


def test_second_storage_target_tolerates_older_fixtures_but_not_unbound_claims():
    """An absent claim means older setup; an unbound one is a broken fixture."""
    config = SimpleNamespace(kubeconfig=Path("kubeconfig"), namespace="fixture")
    assert (
        _FILESYSTEM._second_storage_target(  # pylint: disable=protected-access
            _ClaimRunner(""), config
        )
        is None
    )
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="not Bound"):
        _FILESYSTEM._second_storage_target(  # pylint: disable=protected-access
            _ClaimRunner(_claim("Pending", "")), config
        )


# --- fixture manifests -------------------------------------------------------


def _documents(name, **replacements):
    text = _DRIVER._render_resource_text(  # pylint: disable=protected-access
        name,
        {"STORAGE_TEST_CAPACITY": "2Gi", "SSH_HOME_CAPACITY": "64Mi", **replacements},
    )
    return [document for document in yaml.safe_load_all(text) if document]


def test_nfs_manifest_binds_a_same_named_claim_in_the_second_namespace():
    """The StorageClass subDir isolates the claim by namespace and name."""
    documents = _documents(
        "manifests/nfs-storage.yaml.tmpl",
        NAMESPACE="ns-one",
        NAMESPACE_2="ns-two",
        NFS_SERVER="10.0.0.1",
    )
    claims = {
        (item["metadata"]["namespace"], item["metadata"]["name"])
        for item in documents
        if item["kind"] == "PersistentVolumeClaim"
    }
    assert ("ns-one", "storage-test-rwx") in claims
    assert ("ns-two", "storage-test-rwx") in claims
    storage_class = next(item for item in documents if item["kind"] == "StorageClass")
    assert storage_class["parameters"]["subDir"] == (
        "${pvc.metadata.namespace}/${pvc.metadata.name}"
    )


def test_sbx_manifest_adds_a_second_static_pv_bound_to_the_second_claim():
    """The second target has its own PV, directory, and claim binding."""
    documents = _documents(
        "manifests/sbx-storage.yaml.tmpl", NAMESPACE="ns-one", NAMESPACE_2="ns-two"
    )
    volumes = {
        item["metadata"]["name"]: item
        for item in documents
        if item["kind"] == "PersistentVolume"
    }
    second = volumes["storage-scale-sbx-data-2"]
    assert second["spec"]["claimRef"] == {
        "namespace": "ns-two",
        "name": "storage-test-rwx",
    }
    paths = {item["spec"]["hostPath"]["path"] for item in volumes.values()}
    assert len(paths) == len(volumes)
    claim = next(
        item
        for item in documents
        if item["kind"] == "PersistentVolumeClaim"
        and item["metadata"]["namespace"] == "ns-two"
    )
    assert claim["spec"]["volumeName"] == "storage-scale-sbx-data-2"
    directory = Path(second["spec"]["hostPath"]["path"]).name
    assert directory in fixture_storage.SBX_SHARED_DIRECTORIES


def test_every_sbx_pv_directory_is_created_and_allowed_at_teardown():
    """Setup creates, and teardown tolerates, each PV's backing directory."""
    documents = _documents(
        "manifests/sbx-storage.yaml.tmpl", NAMESPACE="ns-one", NAMESPACE_2="ns-two"
    )
    directories = {
        Path(item["spec"]["hostPath"]["path"]).name
        for item in documents
        if item["kind"] == "PersistentVolume"
    }
    assert directories <= set(fixture_storage.SBX_SHARED_DIRECTORIES)


# --- scenario specifications -------------------------------------------------


@pytest.mark.parametrize("name", sorted(_FILESYSTEM.KUBECTL_MULTI_TARGET_SCENARIOS))
def test_multi_target_scenarios_are_kubectl_only_and_request_two_targets(name):
    """Only Kubernetes scenarios may select several storage targets."""
    spec = _SPECS[name]
    assert spec.substrates == frozenset({"kubectl"})
    assert spec.storage_targets == 2
    assert all(
        other.storage_targets == 1
        for other in _SPECS.values()
        if other.name not in _FILESYSTEM.KUBECTL_MULTI_TARGET_SCENARIOS
    )
    assert spec.same_namespace is (name == "kubectl-same-namespace-targets")


def test_scenario_validation_rejects_invalid_target_counts():
    """Zero, too many, or non-Kubernetes multi-target specs are inconsistent."""
    module = load_integration_lib("filesystem_scenario_specs")
    spec = module.SCENARIO_SPECS_BY_NAME["kubectl-multi-target"]
    for broken in (
        replace(spec, storage_targets=0),
        replace(spec, storage_targets=3),
        replace(spec, substrates=frozenset({"kubectl", "ssh"})),
    ):
        with pytest.raises(module.ScenarioSpecError, match="storage target count"):
            module.validate_scenario_specs((broken,))
    module.validate_scenario_specs(
        (spec, module.SCENARIO_SPECS_BY_NAME["kubectl-multi-target-unbound"])
    )


def test_multi_target_steps_batch_then_append_with_target_files():
    """Time-limited IO is prepared, then a small mdtest is appended.

    Both submissions pass the same two target files and no workload override.
    """
    first, metadata = _SPECS["kubectl-multi-target"].steps
    targets = ("--target", "targets/a.env", "--target", "targets/b.env")
    assert first.arguments == ("--batch", *targets, "--nodes", "1,2")
    assert metadata.arguments[:2] == ("--append", "{batch_results_dir}")
    assert metadata.arguments[2:6] == targets
    assert "--env-override" not in metadata.arguments
    assert metadata.workload_kind == "mdtest"
    assert first.env_lines == metadata.env_lines
    assert "--run-to-completion" not in first.arguments
    assert 'export ELBENCHO_FILE_SIZE="256K"' in first.env_lines
    assert [item.coordinate.nodes for item in first.executions] == [1, 2]
    for step in (first, metadata):
        assert [item.relative_path for item in step.support_files] == [
            "targets/a.env",
            "targets/b.env",
        ]


def test_unbound_scenario_prepares_a_single_io_submission_with_target_files():
    """The rejected start needs only one submission to expand per target file."""
    (prepare,) = _SPECS["kubectl-multi-target-unbound"].steps
    assert prepare.arguments == (
        "--batch",
        "--target",
        "targets/a.env",
        "--target",
        "targets/b.env",
        "--nodes",
        "1",
    )
    assert prepare.exports == ("batch_results_dir",)
    assert "{test_key_ns2}" in prepare.support_files[1].content


# --- target files and expected groups ---------------------------------------


def _runtime(name):
    """Return a Kubernetes runtime whose values the scenario templates render."""
    fixture = _fixture()
    return SimpleNamespace(
        scenario=_SPECS[name],
        values=_FILESYSTEM._make_scenario_runtime(  # pylint: disable=protected-access
            fixture, _SPECS[name], "kubectl", Path("b"), Path("a"), "run-1"
        ).values,
    )


def _expected(name="kubectl-multi-target"):
    runtime = _runtime(name)
    return _FILESYSTEM.expected_multi_target_groups(
        runtime.scenario.steps, runtime.values, "ns-one"
    )


_ROOT = "integration-regression/run-1-kubectl-multi-target-kubectl/primary"


def test_scenario_keys_name_each_claim_and_namespace():
    """Kubernetes roots are ``[namespace/]claim:path``; other roots stay paths."""
    values = _runtime("kubectl-multi-target").values
    assert values["test_key"] == f"storage-test-rwx:{_ROOT}"
    assert values["test_key_ns1"] == f"ns-one/storage-test-rwx:{_ROOT}"
    assert values["test_key_ns2"] == f"ns-two/storage-test-rwx:{_ROOT}"
    assert values["test_key_claim2"] == f"storage-test-rwx-2:{_ROOT}"
    assert values["kubectl_claim"] == "storage-test-rwx"
    plain = _FILESYSTEM._scenario_keys(  # pylint: disable=protected-access
        _fixture(), "ssh", "/mnt/storage-test/x"
    )
    assert plain == {
        "test_key": "/mnt/storage-test/x/primary",
        "test_key_secondary": "/mnt/storage-test/x/secondary",
    }


def test_target_files_render_the_documented_two_namespace_contract():
    """a.env and b.env each hold one root on the fixture's claim in a namespace."""
    first = _SPECS["kubectl-multi-target"].steps[0]
    rendered = {
        item.relative_path: item.content
        for item in first.render_support_files(_runtime("kubectl-multi-target").values)
    }
    assert rendered == {
        "targets/a.env": f'TEST_DIRS=(["ns-one/storage-test-rwx:{_ROOT}"]=1)\n',
        "targets/b.env": f'TEST_DIRS=(["ns-two/storage-test-rwx:{_ROOT}"]=1)\n',
    }


def test_target_file_parser_reads_weights_names_and_default_namespace():
    """The parser applies the product's naming rule to a rendered file."""
    parse = _FILESYSTEM.parse_target_file
    target = parse('TEST_DIRS=(["a:x"]=2 ["ns-one/b:y/z"]=1)\n', "ns-one")
    assert [(r.namespace, r.claim, r.path, r.weight) for r in target.roots] == [
        ("ns-one", "a", "x", 2),
        ("ns-one", "b", "y/z", 1),
    ]
    assert target.name == "ns-one/a:x,ns-one/b:y/z"
    assert target.dirs_per_claim() == {"a": 2, "b": 1}
    assert target.claims == ("a", "b")
    named = parse('STORAGE_TARGET_NAME=vendor\nTEST_DIRS=(["a:x"]=1)\n', "ns-one")
    assert named.name == "vendor"
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="at least one"):
        parse("STORAGE_TARGET_NAME=vendor\n", "ns-one")
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="span namespaces"):
        parse('TEST_DIRS=(["n1/a:x"]=1 ["n2/a:x"]=1)\n', "ns-one")


def test_expected_groups_follow_submission_then_target_file_order():
    """Two submissions passing two files give groups 0001-0004 and cells 0001-0006."""
    groups = _expected()
    assert [(g.group_id, g.kind, g.target.namespace) for g in groups] == [
        ("0001", "io", "ns-one"),
        ("0002", "io", "ns-two"),
        ("0003", "mdtest", "ns-one"),
        ("0004", "mdtest", "ns-two"),
    ]
    assert [[identity for identity, _ in g.executions] for g in groups] == [
        ["0001", "0002"],
        ["0003", "0004"],
        ["0005"],
        ["0006"],
    ]
    assert [g.target.name for g in groups] == [
        f"ns-one/storage-test-rwx:{_ROOT}",
        f"ns-two/storage-test-rwx:{_ROOT}",
        f"ns-one/storage-test-rwx:{_ROOT}",
        f"ns-two/storage-test-rwx:{_ROOT}",
    ]
    assert _FILESYSTEM.segment_namespaces(groups) == ("ns-one", "ns-two")


def test_aggregate_scenario_expects_a_two_to_one_split_over_two_claims():
    """The aggregate target weights its claims 2:1; the other group has one root."""
    aggregate, single = _expected("kubectl-same-namespace-targets")
    base = "integration-regression/run-1-kubectl-same-namespace-targets-kubectl"
    assert aggregate.target.dirs_per_claim() == {
        "storage-test-rwx": 2,
        "storage-test-rwx-2": 1,
    }
    assert aggregate.target.name == ",".join(
        (
            f"ns-one/storage-test-rwx-2:{base}/secondary",
            f"ns-one/storage-test-rwx:{base}/primary",
        )
    )
    assert single.target.dirs_per_claim() == {"storage-test-rwx-2": 1}
    assert single.target.name == f"ns-one/storage-test-rwx-2:{base}/primary"
    assert [g.target_file for g in (aggregate, single)] == [
        "targets/agg.env",
        "targets/single.env",
    ]
    assert _FILESYSTEM.segment_namespaces((aggregate, single)) == ("ns-one",)


def test_aggregate_scenario_validates_both_claims_from_env_sh():
    """env.sh carries the aggregate map too, so validate_env.sh checks both claims."""
    (prepare,) = _SPECS["kubectl-same-namespace-targets"].steps
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
    assert any(
        '["{test_key}"]=2 ["{test_key_claim2_secondary}"]=1' in line
        for line in prepare.env_lines
    )
    assert 'export ELBENCHO_FILE_LAYOUT="worker-directories"' in prepare.env_lines
    assert not any("SEQUENCER" in line for line in prepare.env_lines)


# --- submission and status output --------------------------------------------


def _submission_output(groups, *, claims=False):
    lines = ["STORAGE_SCALE_TEST_BATCH_RESULTS=/results/batch"]
    lines.append(f"STORAGE_SCALE_TEST_BATCH_GROUP={groups[0].group_id}")
    if len(groups) > 1:
        lines.append(
            "STORAGE_SCALE_TEST_BATCH_GROUPS=" + ",".join(g.group_id for g in groups)
        )
    for group in groups:
        prefix = f"STORAGE_SCALE_TEST_BATCH_GROUP_{group.group_id}"
        lines.append(f"{prefix}_TARGET={group.target.name}")
        if claims:
            lines.append(f"{prefix}_CLAIM=ns/pv/pvc")
    if len(groups) == 1:
        lines.append(f"STORAGE_SCALE_TEST_BATCH_GROUP_TARGET={groups[0].target.name}")
    return "\n".join(lines) + "\n"


def test_submission_output_must_list_each_groups_target():
    """Each submission prints one group per target file, in command-line order."""
    expected = _expected()
    first, metadata = _SPECS["kubectl-multi-target"].steps
    good = _submission_output(expected[:2])
    _FILESYSTEM.assert_batch_submission_output(good, expected, first)
    swapped = good.replace(
        f"GROUP_0001_TARGET={expected[0].target.name}",
        f"GROUP_0001_TARGET={expected[1].target.name}",
    )
    assert swapped != good
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="printed group"):
        _FILESYSTEM.assert_batch_submission_output(swapped, expected, first)
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="batch groups"):
        _FILESYSTEM.assert_batch_submission_output(good, expected, metadata)
    _FILESYSTEM.assert_batch_submission_output(
        _submission_output(expected[2:]), expected, metadata
    )


def test_submission_output_rejects_claim_keys_and_a_legacy_key_for_several_groups():
    """The contract has no _CLAIM key, and _GROUP_TARGET only for one group."""
    expected = _expected()
    first = _SPECS["kubectl-multi-target"].steps[0]
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="group claims"):
        _FILESYSTEM.assert_batch_submission_output(
            _submission_output(expected[:2], claims=True), expected, first
        )
    legacy = (
        _submission_output(expected[:2])
        + f"STORAGE_SCALE_TEST_BATCH_GROUP_TARGET={expected[0].target.name}\n"
    )
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="single-group"):
        _FILESYSTEM.assert_batch_submission_output(legacy, expected, first)
    no_list = _submission_output(expected[:2]).replace(
        "STORAGE_SCALE_TEST_BATCH_GROUPS=0001,0002\n", ""
    )
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="omitted the _GROUPS"):
        _FILESYSTEM.assert_batch_submission_output(no_list, expected, first)


def test_group_keys_reject_malformed_values():
    """A target key must be one shell word; the legacy key is not a group key."""
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="malformed.*target"):
        _FILESYSTEM.batch_group_target_names(
            "STORAGE_SCALE_TEST_BATCH_GROUP_0001_TARGET=two words\n"
        )
    assert _FILESYSTEM.batch_group_target_names(
        "STORAGE_SCALE_TEST_BATCH_GROUP_0001_TARGET=a/b:c\n"
        "STORAGE_SCALE_TEST_BATCH_GROUP_TARGET=ignored\n"
    ) == [("0001", "a/b:c")]


def _status_output(groups, *, target_of=lambda group: group.target.name):
    lines = ["STATE=SUCCESS", "NEXT_ACTION=COLLECT"]
    for group in groups:
        lines.append(f"GROUP_{group.group_id}_KIND={group.kind}")
        lines.append(f"GROUP_{group.group_id}_TARGET={target_of(group)}")
    return "\n".join(lines) + "\n"


def test_status_keys_must_match_each_groups_kind_and_target():
    """Batch status names every group's workload kind and storage target."""
    expected = _expected()
    _FILESYSTEM.assert_batch_status_groups(_status_output(expected), expected)
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="status group 0002"):
        _FILESYSTEM.assert_batch_status_groups(
            _status_output(
                expected,
                target_of=lambda group: (
                    "ns-one/x:y" if group.group_id == "0002" else group.target.name
                ),
            ),
            expected,
        )
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="GROUP_0001_KIND"):
        _FILESYSTEM.assert_batch_status_groups("STATE=SUCCESS\n", expected)


# --- collected result assertions ---------------------------------------------


def _generated_csv(group):
    """Return a group's generated directories: one per unit of weight per root."""
    return ",".join(
        f"{root.claim}:{root.path}/elbencho-sweep-target-{index}-ds-e0001"
        for root in group.target.roots
        for index in range(1, root.weight + 1)
    )


def _write_group_cell(result, group, identity, execution, status, csv):
    """Create one batch cell's definition, status, and evidence files."""
    coordinate = execution.coordinate
    relative = f"groups/{group.group_id}/out"
    output = result / relative
    (output / "executions").mkdir(parents=True, exist_ok=True)
    (result / "executions").mkdir(exist_ok=True)
    (result / "executions" / f"{identity}.sh").write_text(
        f"export ELBENCHO_EXECUTION_KIND={group.kind}\n"
        f"export ELBENCHO_BATCH_GROUP_ID={group.group_id}\n"
        f"export ELBENCHO_BATCH_OUTPUT_RELATIVE={relative}\n"
        f"export nodes={coordinate.nodes}\n"
        f"export ELBENCHO_RUN_GENERATED_TEST_DIRS_CSV={csv}\n",
        encoding="utf-8",
    )
    (result / "executions" / f"{identity}.status").write_text(
        status + "\n", encoding="utf-8"
    )
    if status != "SUCCESS":
        return
    (output / "result.csv").write_text("x\n", encoding="utf-8")
    (output / "result.out").write_text("x\n", encoding="utf-8")
    if group.kind == "mdtest":
        stem = f"mdtest-elbencho-c_{coordinate.nodes:03d}-t_001_x_iter1"
        for suffix in ("csv", "out"):
            (output / f"{stem}.{suffix}").write_text("x\n", encoding="utf-8")
        marker = output / "executions" / f"{identity}.mdtest.complete"
        marker.write_text("COMPLETE\n", encoding="utf-8")
        return
    for suffix in ("write", "read", "delete"):
        (output / "executions" / f"{identity}.{suffix}.json").write_text(
            "{}\n", encoding="utf-8"
        )


def _write_snapshot(output, group, namespace, *, extra_shell="", record=True):
    """Write one group's env_used.sh and env_used.yaml."""
    mapped = " ".join(
        f"/mnt/storage-scale-test/{root.claim}/{root.path}"
        for root in group.target.roots
    )
    (output / "env_used.sh").write_text(
        f"export KUBECTL_NAMESPACE={namespace}\n"
        f"export KUBECTL_MAPPED_TEST_DIRS='{mapped}'\n{extra_shell}",
        encoding="utf-8",
    )
    document = {
        "KUBECTL_NAMESPACE": namespace,
        "STORAGE_TARGET_NAME": group.target.name,
        "env_override": None,
        "target_file": (
            {
                "file": f"/w/{group.target_file}",
                "sha256": hashlib.sha256(
                    group.target_file_content.encode()
                ).hexdigest(),
                "variables": ["TEST_DIRS"],
            }
            if record
            else None
        ),
    }
    (output / "env_used.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")


def _write_result(
    root,
    expected,
    status,
    *,
    namespace_of=lambda group: None,
    csv_of=_generated_csv,
    **snapshot_options,
):
    """Build a collected multi-target batch result tree."""
    result = root / "batch"
    manifest = ["version\t1", "revision\t4", "datestamp\tx"]
    for group in expected:
        relative = f"groups/{group.group_id}/out"
        manifest.append(f"group\t{group.group_id}\t{group.kind}\t{relative}\ta\tb")
        output = result / relative
        output.mkdir(parents=True, exist_ok=True)
        _write_snapshot(
            output,
            group,
            namespace_of(group) or group.target.namespace,
            **snapshot_options,
        )
        for identity, execution in group.executions:
            manifest.append(f"execution\t{identity}\t{group.group_id}\tc")
            _write_group_cell(result, group, identity, execution, status, csv_of(group))
    (result / "batch-manifest.tsv").write_text(
        "\n".join(manifest) + "\n", encoding="utf-8"
    )
    return result


def _assert_result(*arguments):
    """Run the driver's collected-result assertion."""
    _FILESYSTEM._assert_multi_target_result(  # pylint: disable=protected-access
        *arguments
    )


def test_collected_result_passes_when_every_group_has_its_snapshot_and_evidence(
    tmp_path,
):
    """All four groups carry their own namespace, target file, and SUCCESS cells."""
    expected = _expected()
    result = _write_result(tmp_path, expected, "SUCCESS")
    _assert_result(result, expected, _FILESYSTEM.ExecutionStatus.SUCCESS)


def test_collected_aggregate_result_passes_with_its_two_to_one_split(tmp_path):
    """The aggregate group's directories split 2:1; the single group's are one."""
    expected = _expected("kubectl-same-namespace-targets")
    result = _write_result(tmp_path, expected, "SUCCESS")
    _assert_result(result, expected, _FILESYSTEM.ExecutionStatus.SUCCESS)


def test_collected_aggregate_result_rejects_the_wrong_split(tmp_path):
    """A one-to-one split over the same two claims is not the configured 2:1."""
    expected = _expected("kubectl-same-namespace-targets")

    def evenly(group):
        claims = [root.claim for root in group.target.roots]
        return ",".join(f"{claim}:p/d-{index}" for index, claim in enumerate(claims))

    result = _write_result(tmp_path, expected, "SUCCESS", csv_of=evenly)
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="group 0001 cell 0001"):
        _assert_result(result, expected, _FILESYSTEM.ExecutionStatus.SUCCESS)


def test_generated_directory_counts_accept_logical_and_pod_paths():
    """Each entry names its claim as ``claim:path``, ``ns/claim:path``, or a mount."""
    counts = _FILESYSTEM.generated_dirs_per_claim(
        "a:x/d1,ns/a:x/d2,/mnt/storage-scale-test/b/y/d1,"
    )
    assert counts == {"a": 2, "b": 1}
    assert _FILESYSTEM.generated_dirs_per_claim("") == {}
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="claim-qualified"):
        _FILESYSTEM.generated_dirs_per_claim("unqualified/path")


def test_prepared_result_requires_pending_cells(tmp_path):
    """A prepared batch holds the same ledger with every cell still PENDING."""
    expected = _expected()
    result = _write_result(tmp_path, expected, "PENDING")
    _assert_result(result, expected, _FILESYSTEM.ExecutionStatus.PENDING)
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="expected SUCCESS"):
        _assert_result(result, expected, _FILESYSTEM.ExecutionStatus.SUCCESS)


def test_collected_result_rejects_a_group_recording_the_wrong_namespace(tmp_path):
    """Group 0002 must record the second namespace, not the first."""
    expected = _expected()
    result = _write_result(
        tmp_path,
        expected,
        "SUCCESS",
        namespace_of=lambda group: "ns-one" if group.group_id == "0002" else None,
    )
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="group 0002"):
        _assert_result(result, expected, _FILESYSTEM.ExecutionStatus.SUCCESS)


def test_collected_result_rejects_a_snapshot_that_still_records_a_pv(tmp_path):
    """KUBECTL_PV and KUBECTL_PVC no longer exist in a group's snapshot."""
    expected = _expected()
    result = _write_result(
        tmp_path,
        expected,
        "SUCCESS",
        extra_shell="export KUBECTL_PVC=storage-test-rwx\n",
    )
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="PV or PVC"):
        _assert_result(result, expected, _FILESYSTEM.ExecutionStatus.SUCCESS)


def test_collected_result_requires_the_target_file_record(tmp_path):
    """Every group of these scenarios came from a target file, so records it."""
    expected = _expected()
    result = _write_result(tmp_path, expected, "SUCCESS", record=False)
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="no target_file"):
        _assert_result(result, expected, _FILESYSTEM.ExecutionStatus.SUCCESS)


def test_collected_result_rejects_a_missing_group(tmp_path):
    """The ledger must hold exactly one group per submission and target file."""
    expected = _expected()
    result = _write_result(tmp_path, expected[:3], "SUCCESS")
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="ledger"):
        _assert_result(result, expected, _FILESYSTEM.ExecutionStatus.SUCCESS)


# --- namespaces as segments ---------------------------------------------------

_SEGMENTS = (
    "SEGMENT_COUNT=2\n"
    "SEGMENT_0_NAMESPACE=ns-one\nSEGMENT_0_STATE=SUCCESS\n"
    "SEGMENT_1_NAMESPACE=ns-two\nSEGMENT_1_STATE=SUCCESS\n"
)


def test_segment_states_are_read_from_status_keys():
    """SEGMENT_<i>_NAMESPACE and _STATE follow segment order, with the count."""
    _FILESYSTEM.assert_segment_states(
        _SEGMENTS, ("ns-one", "ns-two"), ("SUCCESS", "SUCCESS")
    )
    for broken, message in (
        (_SEGMENTS.replace("1_STATE=SUCCESS", "1_STATE=PENDING"), "segments are"),
        (_SEGMENTS.replace("COUNT=2", "COUNT=3"), "segments are"),
        (_SEGMENTS.replace("ns-two", "ns-one"), "segments are"),
        (_SEGMENTS.replace("SEGMENT_1_STATE", "TARGET_1_STATE"), "SEGMENT_1_STATE"),
    ):
        with pytest.raises(_FILESYSTEM.IntegrationTestError, match=message):
            _FILESYSTEM.assert_segment_states(
                broken, ("ns-one", "ns-two"), ("SUCCESS", "SUCCESS")
            )
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="one expected state"):
        _FILESYSTEM.assert_segment_states(_SEGMENTS, ("ns-one",), ())


def test_status_prints_no_target_keys_and_no_segments_for_one_namespace():
    """SEGMENT_* keys exist for several namespaces only; TARGET_<i>_* are gone."""
    _FILESYSTEM.assert_no_segment_keys("STATE=SUCCESS\nNEXT_ACTION=COLLECT\n")
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="SEGMENT_COUNT"):
        _FILESYSTEM.assert_no_segment_keys("STATE=SUCCESS\nSEGMENT_COUNT=1\n")
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="TARGET_0_STATE"):
        _FILESYSTEM.assert_no_segment_keys("STATE=SUCCESS\nTARGET_0_STATE=SUCCESS\n")
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="TARGET_COUNT"):
        _FILESYSTEM.assert_segment_states(
            _SEGMENTS + "TARGET_COUNT=2\n",
            ("ns-one", "ns-two"),
            ("SUCCESS", "SUCCESS"),
        )


# --- cleanup and polling -----------------------------------------------------


class _ObjectRunner:
    """Return scripted owned-object listings per namespace."""

    def __init__(self, listings):
        self.listings = list(listings)
        self.commands = []

    def run(self, arguments, **_kwargs):
        command = [str(item) for item in arguments]
        self.commands.append(command)
        namespace = command[command.index("-n") + 1]
        script = self.listings[0] if len(self.listings) == 1 else self.listings.pop(0)
        return SimpleNamespace(
            returncode=0, stdout="\n".join(script.get(namespace, [])), stderr=""
        )


_CONFIG = SimpleNamespace(kubeconfig=Path("kubeconfig"))


def test_cleanup_assertion_queries_every_owned_kind_in_both_namespaces():
    """Leases, Jobs, DaemonSets, policies, Pods, and RBAC are all inspected."""
    runner = _ObjectRunner([{}])
    _FILESYSTEM._assert_no_owned_kubectl_objects(  # pylint: disable=protected-access
        runner, _CONFIG, ("ns-one", "ns-two")
    )
    assert [command[command.index("-n") + 1] for command in runner.commands] == [
        "ns-one",
        "ns-two",
    ]
    kinds = runner.commands[0][runner.commands[0].index("get") + 1].split(",")
    for kind in (
        "pods",
        "jobs.batch",
        "daemonsets.apps",
        "networkpolicies.networking.k8s.io",
        "serviceaccounts",
        "roles.rbac.authorization.k8s.io",
        "rolebindings.rbac.authorization.k8s.io",
        "leases.coordination.k8s.io",
    ):
        assert kind in kinds
    assert "app.kubernetes.io/managed-by=storage-scale-test" in runner.commands[0]


def test_cleanup_assertion_waits_for_terminating_objects_then_fails(monkeypatch):
    """Objects that disappear within the window pass; permanent ones fail."""
    monkeypatch.setattr(_FILESYSTEM.time, "sleep", lambda _seconds: None)
    settling = _ObjectRunner([{"ns-two": ["pod/old"]}, {"ns-two": ["pod/old"]}, {}])
    _FILESYSTEM._assert_no_owned_kubectl_objects(  # pylint: disable=protected-access
        settling, _CONFIG, ("ns-one", "ns-two")
    )
    leaking = _ObjectRunner([{"ns-two": ["lease.coordination.k8s.io/x"]}])
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="ns-two"):
        _FILESYSTEM._assert_no_owned_kubectl_objects(  # pylint: disable=protected-access
            leaking, _CONFIG, ("ns-one", "ns-two"), settle_seconds=0
        )


def _poller(monkeypatch, outputs):
    """Patch status polling to return scripted outputs."""
    remaining = list(outputs)
    monkeypatch.setattr(_FILESYSTEM.time, "sleep", lambda _seconds: None)
    commands = []

    def fake(_runner, _config, _runtime, name, arguments, *_args):
        commands.append((name, arguments))
        value = remaining.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(_FILESYSTEM, "_run_kubectl_command", fake)
    return commands


def _wait(timeout=30):
    return (
        _FILESYSTEM._kubectl_wait_for_next_action(  # pylint: disable=protected-access
            None, None, None, Path("/results"), Path("/logs"), "COLLECT", timeout
        )
    )


def test_polling_only_reads_status_until_collect(monkeypatch):
    """No advance command is ever issued while waiting for NEXT_ACTION=COLLECT."""
    transient = _FILESYSTEM.IntegrationTestError("status blip")
    commands = _poller(
        monkeypatch,
        [
            "STATE=RUNNING\nNEXT_ACTION=WAIT\n",
            transient,
            "STATE=BETWEEN_EXECUTIONS\nNEXT_ACTION=WAIT\n",
            "STATE=SUCCESS\nNEXT_ACTION=COLLECT\n",
        ],
    )
    output = _wait()
    assert "NEXT_ACTION=COLLECT" in output
    assert {name for name, _ in commands} == {"status"}
    assert all(arguments[0] == "--status" for _, arguments in commands)


def test_polling_fails_fast_on_a_terminal_state_without_collect(monkeypatch):
    """A terminal attempt that does not offer COLLECT is not waited out."""
    _poller(monkeypatch, ["STATE=FAILED\nNEXT_ACTION=INSPECT\n"])
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="FAILED"):
        _wait()


def test_polling_times_out_with_the_last_error(monkeypatch):
    """A stuck attempt reports the timeout rather than looping forever."""
    _poller(monkeypatch, ["STATE=RUNNING\nNEXT_ACTION=WAIT\n"] * 5)
    ticks = iter(range(0, 1000, 20))
    monkeypatch.setattr(_FILESYSTEM.time, "monotonic", lambda: next(ticks))
    with pytest.raises(_FILESYSTEM.IntegrationTestError, match="NEXT_ACTION=COLLECT"):
        _wait(timeout=30)


# --- unbound-claim variant ---------------------------------------------------


def test_unbound_claim_manifest_can_never_bind():
    """The scenario-owned claim names no PV and disables provisioning."""
    target = fixture_storage.KubectlStorageTarget(
        "ns-two", fixture_storage.UNBOUND_PV, fixture_storage.UNBOUND_CLAIM
    )
    # pylint: disable-next=protected-access
    document = json.loads(_FILESYSTEM._unbound_claim_manifest(target))
    assert document["metadata"]["namespace"] == "ns-two"
    assert document["spec"]["storageClassName"] == ""
    assert document["spec"]["volumeName"] == fixture_storage.UNBOUND_PV
    assert fixture_storage.UNBOUND_CLAIM != "storage-test-rwx"


def test_catalog_registers_both_scenarios_for_kubectl_only():
    """Both scenarios are plannable on Kubernetes and nowhere else."""
    for substrate in ("ssh", "slurm"):
        names = {
            step.scenario.name
            for step in _FILESYSTEM.plan_scenarios(substrate=substrate)
            if hasattr(step, "scenario")
        }
        assert not names & set(_FILESYSTEM.KUBECTL_MULTI_TARGET_SCENARIOS)
    kubectl = {
        step.scenario.name for step in _FILESYSTEM.plan_scenarios(substrate="kubectl")
    }
    assert set(_FILESYSTEM.KUBECTL_MULTI_TARGET_SCENARIOS) <= kubectl
    assert set(_FILESYSTEM.KUBECTL_MULTI_TARGET_SCENARIOS) <= (
        _FILESYSTEM.KUBECTL_SUPPORTED_SCENARIOS
    )


def test_each_target_runs_concurrently_and_the_first_failure_is_raised():
    """Per-target utility Pods start together; every call finishes first."""
    barrier = threading.Barrier(3, timeout=10)
    finished = []

    def action(target):
        barrier.wait()
        finished.append(target)
        if target != "a":
            raise ValueError(target)

    with pytest.raises(ValueError, match="^b$"):
        _FILESYSTEM.for_each_target(action, ("a", "b", "c"))
    assert sorted(finished) == ["a", "b", "c"]


def test_a_single_target_runs_inline():
    """One target needs no thread pool."""
    calls = []
    _FILESYSTEM.for_each_target(calls.append, (None,))
    assert calls == [None]
