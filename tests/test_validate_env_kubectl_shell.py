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

"""User-facing kubectl diagnostics from validate_env.sh."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import textwrap
import time

import pytest

from tests.kubectl_segment_test_helpers import DEFAULT_SEQUENCER_IMAGE

_ROOT = Path(__file__).resolve().parent.parent
_BASH = shutil.which("bash") or "/bin/bash"


def test_macos_kubectl_prerequisite_failure_names_tools_and_formulae(tmp_path):
    """Darwin validation identifies every missing prefixed launcher tool."""
    source = (_ROOT / "validate_env.sh").read_text(encoding="utf-8")
    function = source.split("check_macos_launcher_prerequisites() {", 1)[1].split(
        "\n}", 1
    )[0]
    script = textwrap.dedent(f"""\
        register_error() {{ printf 'ERROR:%s\\n' "$1"; }}
        uname() {{ printf 'Darwin\\n'; }}
        EXECUTION_SUBSTRATE=kubectl
        check_macos_launcher_prerequisites() {{{function}
        }}
        check_macos_launcher_prerequisites
        """)
    result = subprocess.run(
        [_BASH, "-c", script],
        check=False,
        env={**os.environ, "PATH": str(tmp_path)},
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    for command in ("grealpath", "gstat", "gtimeout", "gtar", "flock"):
        assert command in result.stdout
    assert "brew install bash coreutils gnu-tar flock" in result.stdout


def _kubectl_deployment(tmp_path):
    """A copy of the validator with a kubectl env.sh that has no TEST_DIRS."""
    deployment = tmp_path / "deploy'ment"
    functions_dir = deployment / "storage-tests" / "fs" / "kubectl"
    functions_dir.mkdir(parents=True)
    shutil.copy2(_ROOT / "validate_env.sh", deployment / "validate_env.sh")
    (deployment / "lib").mkdir()
    shutil.copy2(_ROOT / "lib/project_version.sh", deployment / "lib")
    shutil.copy2(_ROOT / "lib/_kubectl_claims.sh", deployment / "lib")
    shutil.copy2(
        _ROOT
        / "storage-tests"
        / "fs"
        / "kubectl"
        / "_nv-elbencho-kubectl-functions.sh",
        functions_dir / "_nv-elbencho-kubectl-functions.sh",
    )
    results = tmp_path / "results"
    logs = tmp_path / "logs"
    results.mkdir()
    logs.mkdir()
    (deployment / "env.sh").write_text(
        textwrap.dedent(f"""\
            RESULTS_DIR={str(results)!r}
            LOGS_DIR={str(logs)!r}
            KUBECTL_ENABLED=1
            SLURM_ENABLED=
            SSH_ENABLED=
            FS_ENABLED=
            OBJ_ENABLED=
            declare -A TEST_DIRS=()
            KUBECTL_NAMESPACE=pawan-lustre
            KUBECTL_PV=pawan-lustre-pv
            KUBECTL_PVC=lustre-pvc-example
            KUBECTL_NODE_SELECTOR=lustre-host=true
            KUBECTL_ELBENCHO_IMAGE=breuner/elbencho:v3.2-1
            KUBECTL_IMAGE_PULL_POLICY=IfNotPresent
            KUBECTL_RUN_AS_USER=0
            KUBECTL_RUN_AS_GROUP=0
            validate_integer_array() {{ :; }}
            validate_elbencho_io_sizes() {{ :; }}
            validate_elbencho_file_workload_env() {{ :; }}
            validate_elbencho_duration() {{ :; }}
            validate_elbencho_live_csv() {{ :; }}
            validate_elbencho_single_big_file_env() {{ :; }}
            """),
        encoding="utf-8",
    )
    return deployment


def test_root_workload_identity_and_missing_test_dirs_are_actionable(tmp_path):
    """The validator reports kubectl configuration errors without irrelevant notes."""
    deployment = _kubectl_deployment(tmp_path)
    result = subprocess.run(
        [deployment / "validate_env.sh"],
        check=False,
        cwd=deployment,
        env={**os.environ, "SHELL": _BASH},
        text=True,
        capture_output=True,
    )

    assert result.returncode != 0
    assert "KUBECTL_RUN_AS_USER=0 must be a positive numeric UID" in result.stdout
    assert "KUBECTL_RUN_AS_GROUP=0 must be a positive numeric GID" in result.stdout
    assert "requires at least one nonempty TEST_DIRS entry" in result.stdout
    assert "neither Slurm nor SSH" not in result.stdout
    assert "FS not enabled" not in result.stdout
    assert "OBJ not enabled" not in result.stdout
    assert "invalid Kubernetes filesystem sweep configuration" not in result.stdout


@pytest.mark.parametrize("option", ["-v", "--verbose"])
def test_verbose_options_are_accepted(tmp_path, option):
    """-v and --verbose are known arguments (an unknown one exits 2)."""
    deployment = _kubectl_deployment(tmp_path)
    result = subprocess.run(
        [deployment / "validate_env.sh", option],
        check=False,
        cwd=deployment,
        env={**os.environ, "SHELL": _BASH},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    assert "unknown argument" not in result.stderr
    assert "requires at least one nonempty TEST_DIRS entry" in result.stdout


_ROW = "ns1|sst-elb-ab12cd34-sweep|ab12cd34|coordinator|2026-01-02T03:04:05Z"


# The validator's helpers start at its validate_detail definition, which
# precedes the checks the tests below extract.
_CHECKS_START = "# validate_detail FORMAT"


def _run_inventory(tmp_path, fake_body, verbose=True):
    """Run check_kubectl_owned_resources against a fake kubectl on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "kubectl"
    fake.write_text("#!/usr/bin/env bash\n" + fake_body, encoding="utf-8")
    fake.chmod(0o755)
    source = (_ROOT / "validate_env.sh").read_text(encoding="utf-8")
    inventory = source.split(_CHECKS_START, 1)[1].split(
        "check_kubectl_filesystem_prerequisites() {", 1
    )[0]
    functions = _ROOT / "storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
    script = textwrap.dedent(f"""\
        source {str(functions)!r}
        register_error() {{ printf 'ERROR:%s\\n' "$1"; }}
        register_warning() {{ printf 'WARNING:%s\\n' "$1"; }}
        KUBECTL_NAMESPACE=myns
        KUBECTL_OBSERVATION_ATTEMPTS=1
        VALIDATE_VERBOSE={"1" if verbose else ""}
        #{inventory}
        check_kubectl_owned_resources
        """)
    return subprocess.run(
        [_BASH, "-c", script],
        check=False,
        env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
    )


def test_owned_resources_none_found(tmp_path):
    """An empty cluster is reported informationally."""
    result = _run_inventory(tmp_path, "exit 0\n")
    assert result.returncode == 0
    assert "none found (all namespaces)" in result.stdout
    assert "ERROR:" not in result.stdout


def test_owned_resources_none_found_is_silent_unless_verbose(tmp_path):
    """No objects is not worth a line by default."""
    result = _run_inventory(tmp_path, "exit 0\n", verbose=False)
    assert result.returncode == 0
    assert result.stdout == ""


def test_owned_resources_found_are_listed_even_without_verbose(tmp_path):
    """Objects that exist are actionable, so the table is always printed."""
    body = f'[[ " $* " == *" get jobs "* ]] && echo "{_ROW}"\nexit 0\n'
    result = _run_inventory(tmp_path, body, verbose=False)
    assert result.returncode == 0
    assert "1 found (all namespaces)" in result.stdout
    assert "sst-elb-ab12cd34-sweep" in result.stdout
    assert "kubernetes/attempts/<RUN>/" in result.stdout


def test_owned_resources_listed_without_error(tmp_path):
    """Existing objects are tabulated with run id and never register errors."""
    body = f'[[ " $* " == *" get jobs "* ]] && echo "{_ROW}"\nexit 0\n'
    result = _run_inventory(tmp_path, body)
    assert result.returncode == 0
    assert "1 found (all namespaces)" in result.stdout
    assert ["Job", "ns1"] in [line.split()[:2] for line in result.stdout.splitlines()]
    assert "ab12cd34" in result.stdout
    assert "sst-elb-ab12cd34-sweep" in result.stdout
    assert "coordinator" in result.stdout
    assert "kubernetes/attempts/<RUN>/" in result.stdout
    assert "ERROR:" not in result.stdout
    assert "WARNING:" not in result.stdout


def test_owned_resources_forbidden_all_namespaces_falls_back(tmp_path):
    """RBAC denial of -A falls back to the configured namespace."""
    body = textwrap.dedent(f"""\
        if [[ " $* " == *" -A "* ]]; then
            echo 'Error from server (Forbidden): cannot list resource' >&2
            exit 1
        fi
        [[ " $* " == *" -n myns "* && " $* " == *" get jobs "* ]] \\
            && echo "{_ROW}"
        exit 0
        """)
    result = _run_inventory(tmp_path, body)
    assert result.returncode == 0
    assert (
        "namespace myns where cluster-wide listing is forbidden: Job, DaemonSet"
        in result.stdout
    )
    assert "ab12cd34" in result.stdout
    assert "ERROR:" not in result.stdout
    assert "WARNING:" not in result.stdout


def test_owned_resources_probe_failure_warns(tmp_path):
    """API failures downgrade to a warning, not an error."""
    body = "echo 'connection refused' >&2\nexit 1\n"
    result = _run_inventory(tmp_path, body)
    assert result.returncode == 0
    assert "WARNING:Could not list Kubernetes objects" in result.stdout
    assert "ERROR:" not in result.stdout


def test_owned_resources_ignore_kubectl_stderr_warnings(tmp_path):
    """Version-skew warnings on stderr never become inventory rows."""
    body = textwrap.dedent(f"""\
        echo 'Warning: version difference between client (1.36) and server (1.34)' >&2
        [[ " $* " == *" get pods "* ]] && echo "{_ROW}"
        exit 0
        """)
    result = _run_inventory(tmp_path, body)
    assert result.returncode == 0
    assert "1 found (all namespaces)" in result.stdout
    assert ["Pod", "ns1"] in [line.split()[:2] for line in result.stdout.splitlines()]
    assert "version difference" not in result.stdout
    assert "WARNING:" not in result.stdout


_SEQUENCER_IMAGE = "registry.example/sequencer@sha256:" + "b" * 64
_MIRROR = f"KUBECTL_SEQUENCER_IMAGE={_SEQUENCER_IMAGE}"
_FALLBACK = (
    "keep each batch in one namespace (it then needs no sequencer or its RBAC), "
    "or run one batch per namespace"
)

# Fake kubectl for the checks validate_env.sh itself issues: the context, the
# namespace label (absent for "old-*" namespaces), the server version
# (FAKE_MINOR) and `auth can-i` (denied for "<verb> <resource> <namespace>"
# entries in the comma-separated FAKE_DENY). FAKE_DELAY lists comma-separated
# "<verb> <resource>=<seconds>" (or "version=<seconds>") pauses, and a
# FAKE_BARRIER_DIR makes every version and can-i call wait until ten of them are
# running at once, which only concurrent calls can satisfy.
_FAKE_PREREQUISITE_KUBECTL = textwrap.dedent("""\
    fake_pause() {
        local entry
        if [[ -n "${FAKE_BARRIER_DIR:-}" ]]; then
            : > "$FAKE_BARRIER_DIR/$$"
            for _ in {1..50}; do
                (( $(ls "$FAKE_BARRIER_DIR" | wc -l) >= 10 )) && break
                sleep 0.1
            done
            (( $(ls "$FAKE_BARRIER_DIR" | wc -l) >= 10 )) \
                || echo "not concurrent: $1" >> "$FAKE_BARRIER_DIR.timeouts"
        fi
        IFS=, read -ra entries <<< "${FAKE_DELAY:-}"
        for entry in ${entries[@]+"${entries[@]}"}; do
            [[ "${entry%=*}" != "$1" ]] || sleep "${entry#*=}"
        done
    }
    [[ "$1" != --request-timeout=* ]] || shift
    case "$1" in
        config) echo test-context ;;
        get) [[ "$2" != namespace || "$3" == old-* ]] || printf '%s' "$3" ;;
        version)
            fake_pause version
            printf '{"clientVersion": {"major": "1", "minor": "33"},\n'
            printf ' "serverVersion": {"major": "1", "minor": "%s+"}}\n' "${FAKE_MINOR:-30}" ;;
        auth)
            fake_pause "$3 $4"
            if [[ ",${FAKE_DENY:-}," == *",$3 $4 $6,"* ]]; then echo no; exit 1; fi
            echo yes ;;
        *) exit 1 ;;
    esac
    """)


def _prerequisites_script(tmp_path, settings, epilogue=""):
    """The script that runs check_kubectl_filesystem_prerequisites, then EPILOGUE.

    The identity, node discovery, inventory and runtime Job helpers are
    replaced by recorders (appending to tmp_path/calls.log), so the test
    observes the namespace and claims each one saw. FAKE_<PHASE>_DELAY pauses
    the IMAGE, PROBE, RUNTIME and INVENTORY fakes for that many seconds;
    FAKE_RENDEZVOUS_DIR makes the IMAGE and RUNTIME fakes each wait for the
    other to be running too and record OVERLAP or ALONE. REAL_JOBS skips the
    image, probe and runtime fakes so the library's own Job code runs against
    the fake object API in the test's settings. Unless VALIDATE_VERBOSE is set
    (even to ""), the script runs as ``validate_env.sh --verbose`` would, so
    the detail lines can be asserted; set it to "" for the default output.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "kubectl"
    fake.write_text(
        "#!/usr/bin/env bash\n" + _FAKE_PREREQUISITE_KUBECTL, encoding="utf-8"
    )
    fake.chmod(0o755)
    source = (_ROOT / "validate_env.sh").read_text(encoding="utf-8")
    checks = source.split(_CHECKS_START, 1)[1].split(
        "# Determine architecture & check for binaries", 1
    )[0]
    calls = tmp_path / "calls.log"
    return textwrap.dedent(f"""\
        set -u
        SCALE_TEST_BASE={str(_ROOT)!r}
        source "$SCALE_TEST_BASE/storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
        source "$SCALE_TEST_BASE/lib/_kubectl_claims.sh"
        register_error() {{ printf 'ERROR:%s\\n' "$1"; }}
        register_warning() {{ printf 'WARNING:%s\\n' "$1"; }}
        register_summary() {{ printf 'SUMMARY:%s\\n' "$1"; }}
        KUBECTL_OBSERVATION_ATTEMPTS=1
        EXECUTION_SUBSTRATE=kubectl
        STORAGE_TARGET_NAME=
        KUBECTL_NODE_SELECTOR=benchmark=true
        KUBECTL_ELBENCHO_IMAGE=registry.example/elbencho:1
        KUBECTL_IMAGE_PULL_POLICY=IfNotPresent
        KUBECTL_RUN_AS_USER=2000
        KUBECTL_RUN_AS_GROUP=2000
        KUBECTL_NAMESPACE=ns-a KUBECTL_SEQUENCER_IMAGE=
        VALIDATE_SEQUENCER="${{VALIDATE_SEQUENCER:-}}"
        VALIDATE_VERBOSE="${{VALIDATE_VERBOSE-1}}"
        declare -A TEST_DIRS=(["pvc-a:data"]=1)
        {settings}
        #{checks}
        kubectl_validate_cluster_identity() {{
            local namespace claim
            local -a claims=()
            kubectl_test_dirs_segment namespace claims || return 1
            printf 'IDENTITY %s %s\\n' "$namespace" "${{claims[*]}}" >> {str(calls)!r}
            [[ "$namespace" != bad-* ]] || {{ echo "Error: cannot read" >&2; return 1; }}
            [[ -z "${{FAKE_IDENTITY_WARNING:-}}" ]] || echo "$FAKE_IDENTITY_WARNING" >&2
            for claim in "${{claims[@]}}"; do
                printf '%s\\tuid-%s\\tpv-%s\\tpv-uid-%s\\t%s\\tpvc-uid-%s\\n' "$namespace" \\
                    "$namespace" "$claim" "$claim" "$claim" "$claim"
            done
        }}
        kubectl_discover_candidate_nodes() {{
            echo DISCOVERY >> {str(calls)!r}
            [[ -z "${{FAKE_DISCOVERY_ERROR:-}}" ]] || {{ echo "$FAKE_DISCOVERY_ERROR" >&2; return 1; }}
            printf 'node-1\\tuid-1\\tamd64\\n' > "$2"
        }}
        fake_phase() {{
            local delay="FAKE_$1_DELAY" _
            [[ -z "${{!delay:-}}" ]] || sleep "${{!delay}}"
            [[ -n "${{FAKE_RENDEZVOUS_DIR:-}}" && "$1" != INVENTORY ]] || return 0
            : > "$FAKE_RENDEZVOUS_DIR/$1"
            for _ in {{1..50}}; do
                if (( $(ls "$FAKE_RENDEZVOUS_DIR" | wc -l) >= 2 )); then
                    printf 'OVERLAP %s\\n' "$1" >> {str(calls)!r}
                    return 0
                fi
                sleep 0.1
            done
            printf 'ALONE %s\\n' "$1" >> {str(calls)!r}
        }}
        if [[ -z "${{REAL_JOBS:-}}" ]]; then
        kubectl_validate_runtime_pod() {{
            local namespace
            local -a claims=()
            kubectl_test_dirs_segment namespace claims || return 1
            printf 'RUNTIME %s %s\\n' "$namespace" "${{claims[*]}}" >> {str(calls)!r}
            fake_phase RUNTIME
            # Kill the calling branch (this fake runs in its background child).
            local self=$BASHPID
            [[ -z "${{FAKE_KILL_CLUSTER_BRANCH:-}}" ]] \\
                || kill -KILL "$(ps -o ppid= -p "$self")"
            [[ -z "${{FAKE_RUNTIME_ERROR:-}}" ]] || {{ echo "$FAKE_RUNTIME_ERROR" >&2; return 1; }}
        }}
        kubectl_validate_sequencer_image() {{
            printf 'IMAGE %s\\n' "$KUBECTL_NAMESPACE" >> {str(calls)!r}
            fake_phase IMAGE
            [[ -z "${{FAKE_IMAGE_ERROR:-}}" ]] || {{ echo "$FAKE_IMAGE_ERROR" >&2; return 1; }}
        }}
        kubectl_validate_sequencer_probe() {{
            printf 'PROBE %s%s\\n' "$KUBECTL_NAMESPACE" "${{1:+:$1}}" >> {str(calls)!r}
            fake_phase PROBE
            [[ -z "${{FAKE_PROBE_ERROR:-}}" ]] || {{ echo "$FAKE_PROBE_ERROR" >&2; return 1; }}
        }}
        fi
        check_kubectl_owned_resources() {{
            fake_phase INVENTORY
            printf 'INVENTORY %s\\n' "$*" >> {str(calls)!r}
            printf 'INVENTORY %s\\n' "$*"
            [[ -z "${{FAKE_INVENTORY_WARNING:-}}" ]] || register_warning "$FAKE_INVENTORY_WARNING"
        }}
        check_kubectl_filesystem_prerequisites
        {epilogue}
        """)


def _pad_wc(bindir):
    """Install a wc that pads its count like BSD wc (macOS) does."""
    real_wc = shutil.which("wc")
    fake = bindir / "wc"
    fake.write_text(
        f'#!/usr/bin/env bash\nout=$({real_wc!r} "$@")\nprintf \'%8s\\n\' "$out"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)


def _run_prerequisites(tmp_path, settings, *, padded_wc=False, **environment):
    """Run the prerequisites script to completion; return its result and calls."""
    script = _prerequisites_script(tmp_path, settings)
    bindir = tmp_path / "bin"
    if padded_wc:
        _pad_wc(bindir)
    calls = tmp_path / "calls.log"
    result = subprocess.run(
        [_BASH, "-c", script],
        check=False,
        env={
            **os.environ,
            "PATH": f"{bindir}:{os.environ['PATH']}",
            **environment,
        },
        text=True,
        capture_output=True,
    )
    log = calls.read_text().splitlines() if calls.exists() else []
    return result, log


_AGGREGATE = (
    'declare -A TEST_DIRS=(["lustre-b:scale-test"]=1 ["lustre-a:scale-test"]=2)'
)
# validate_env.sh --sequencer sets VALIDATE_SEQUENCER=1.
_ASKED = {"VALIDATE_SEQUENCER": "1"}
_SUMMARY_LINE = (
    "  Kubernetes sequencer checks only warn: they matter only for batches "
    "spanning namespaces; validate_env.sh --sequencer runs the full probe and "
    "treats problems as errors"
)
_IMAGE_PASSED = (
    f"  Kubernetes sequencer image {DEFAULT_SEQUENCER_IMAGE}: pulls and provides "
    "sh, kubectl, grep, sleep"
)
_PREREQUISITES_PASSED = (
    "  Kubernetes sequencer prerequisites: server version and sequencer "
    "permissions passed"
)
_PREREQUISITES_MISSING = (
    "WARNING:Kubernetes sequencer prerequisites (needed only for batches "
    "spanning namespaces) are missing:"
)


def test_every_claim_of_the_target_is_checked_in_one_namespace(tmp_path):
    """Identity names every claim; one runtime Pod mounts them all."""
    result, log = _run_prerequisites(tmp_path, _AGGREGATE)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ERROR:" not in result.stdout
    # The identity check runs first, then the inventory alone (so it never
    # lists this run's own temporary Jobs); the sequencer and cluster branches
    # then run concurrently, and the cluster branch runs discovery first.
    assert log[0] == "IDENTITY ns-a lustre-a lustre-b"
    assert log[1] == "INVENTORY ns-a"
    assert sorted(log[2:]) == [
        "DISCOVERY",
        "IMAGE ns-a",
        "RUNTIME ns-a lustre-a lustre-b",
    ]
    assert log.index("DISCOVERY") < log.index("RUNTIME ns-a lustre-a lustre-b")
    lines = result.stdout.splitlines()
    assert "  Kubernetes target: namespace ns-a, claim(s) lustre-a,lustre-b" in lines
    for claim in ("lustre-a", "lustre-b"):
        assert (
            f"  Kubernetes claim ns-a/{claim}: Bound to PV pv-{claim} (namespace UID "
            f"uid-ns-a, PV UID pv-uid-{claim}, PVC UID pvc-uid-{claim})"
        ) in lines
    assert "INVENTORY ns-a" in lines
    assert (
        "  Kubernetes runtime in namespace ns-a: image, UID/GID, required tools, and "
        "read/write on every root passed"
    ) in lines
    assert _SUMMARY_LINE in lines
    assert _IMAGE_PASSED in lines
    assert _PREREQUISITES_PASSED in lines


_SKEW_WARNING = (
    "Warning: version difference between client (1.36) and server (1.34) exceeds "
    "the supported minor version skew of +/-1"
)
_CLAIM_LINE = (
    "  Kubernetes claim ns-a/pvc-a: Bound to PV pv-pvc-a (namespace UID uid-ns-a, "
    "PV UID pv-uid-pvc-a, PVC UID pvc-uid-pvc-a)"
)


def test_a_kubectl_warning_on_stderr_is_a_warning_not_a_claim(tmp_path):
    """Version skew advice is registered; only the stdout rows become claims."""
    result, errors, warnings = _run_registering(
        tmp_path, FAKE_IDENTITY_WARNING=_SKEW_WARNING
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert errors == []
    assert warnings == [f"kubectl: {_SKEW_WARNING}"]
    lines = result.stdout.splitlines()
    assert _CLAIM_LINE in lines
    assert [line for line in lines if line.startswith("  Kubernetes claim ")] == [
        _CLAIM_LINE
    ]
    assert "Warning" not in result.stdout
    assert ", claim pvc-a," in _registered_summaries(tmp_path)[0]


def test_the_node_count_is_not_padded_like_bsd_wc_pads_it(tmp_path):
    """macOS `wc -l` pads its count; neither the detail line nor the summary may."""
    result, errors, _ = _run_registering(tmp_path, padded_wc=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert errors == []
    assert (
        "  Kubernetes worker selector: benchmark=true (1 Ready amd64 node(s): node-1)"
    ) in result.stdout.splitlines()
    assert _registered_summaries(tmp_path)[0].endswith(", 1 Ready amd64 node: OK")


def test_an_unpadded_node_count_is_also_reported_plainly(tmp_path):
    """The default (GNU wc) path prints the same unpadded count."""
    result, _, _ = _run_registering(tmp_path)
    assert (
        "  Kubernetes worker selector: benchmark=true (1 Ready amd64 node(s): node-1)"
    ) in result.stdout.splitlines()


def test_an_explicit_namespace_in_the_keys_is_the_target_namespace(tmp_path):
    """A key that names its namespace overrides KUBECTL_NAMESPACE."""
    result, log = _run_prerequisites(
        tmp_path, 'declare -A TEST_DIRS=(["ns-b/pvc-b:data"]=1)'
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert log[0] == "IDENTITY ns-b pvc-b"
    assert "RUNTIME ns-b pvc-b" in log


def test_a_failing_identity_names_the_namespace_and_skips_the_runtime(tmp_path):
    """A storage failure is registered and no runtime Pod is started."""
    result, log = _run_prerequisites(
        tmp_path, 'declare -A TEST_DIRS=(["bad-b/pvc-b:data"]=1)'
    )
    assert (
        "ERROR:Kubernetes API or storage validation failed for namespace bad-b"
        in result.stdout
    )
    assert log == ["IDENTITY bad-b pvc-b"]


def test_a_namespace_without_the_metadata_name_label_is_an_error(tmp_path):
    """Kubernetes 1.21+ sets kubernetes.io/metadata.name on every namespace."""
    result, log = _run_prerequisites(
        tmp_path, 'declare -A TEST_DIRS=(["old-b/pvc-b:data"]=1)'
    )
    assert "ERROR:Kubernetes namespace old-b lacks the label" in result.stdout
    assert not [line for line in log if line.startswith("RUNTIME")]


@pytest.mark.parametrize(
    "settings,message",
    [
        (
            "KUBECTL_PVC=scale-test; declare -A TEST_DIRS=([data]=1)",
            'TEST_DIRS=(["scale-test:data"]=1)',
        ),
        (
            "declare -A TEST_DIRS=([data]=1)",
            "must name their claim as [namespace/]claim:path",
        ),
        (
            'declare -A TEST_DIRS=(["a:x"]=1 ["ns-b/b:y"]=1)',
            "pass each file with --target",
        ),
    ],
)
def test_invalid_test_dirs_are_registered_before_contacting_the_cluster(
    tmp_path, settings, message
):
    """Migration and namespace errors are reported without any cluster call."""
    result, log = _run_prerequisites(tmp_path, settings)
    assert "ERROR:Error: " in result.stdout
    assert message in result.stdout
    assert not log


def test_test_dirs_and_workload_errors_are_reported_together(tmp_path):
    """One run names both the roots and the workload identity problems."""
    result, log = _run_prerequisites(
        tmp_path, "declare -A TEST_DIRS=([data]=1); KUBECTL_RUN_AS_USER=0"
    )
    assert "must name their claim" in result.stdout
    assert "KUBECTL_RUN_AS_USER=0 must be a positive numeric UID" in result.stdout
    assert not log


def test_default_sequencer_checks_only_warn_and_never_fail(tmp_path):
    """[S-28] Without --sequencer, RBAC, version and image problems are warnings."""
    result, log = _run_prerequisites(
        tmp_path,
        "",
        FAKE_DENY="create serviceaccounts ns-a",
        FAKE_MINOR="23",
        FAKE_IMAGE_ERROR="the Pod cannot start the image: ErrImagePull",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ERROR:" not in result.stdout
    assert _PREREQUISITES_MISSING in result.stdout
    assert "  - Kubernetes server 1.23 is older than 1.24" in result.stdout
    assert "  - permission to create serviceaccounts in namespace ns-a" in result.stdout
    assert f"Fallback: {_FALLBACK}" in result.stdout
    assert (
        f"WARNING:Kubernetes sequencer image {DEFAULT_SEQUENCER_IMAGE} (needed only "
        "for batches spanning namespaces) failed its check in namespace ns-a: "
        "the Pod cannot start the image: ErrImagePull"
    ) in result.stdout
    assert "Hint: set KUBECTL_SEQUENCER_IMAGE to a digest-pinned image" in result.stdout
    assert _SUMMARY_LINE in result.stdout.splitlines()
    assert _IMAGE_PASSED not in result.stdout
    assert "IMAGE ns-a" in log
    assert not [line for line in log if line.startswith("PROBE")]
    assert "RUNTIME ns-a pvc-a" in log


@pytest.mark.parametrize(
    ("settings", "image"),
    [("", DEFAULT_SEQUENCER_IMAGE), (_MIRROR, _SEQUENCER_IMAGE)],
)
def test_default_image_check_runs_the_effective_image_in_the_target_namespace(
    tmp_path, settings, image
):
    """Without --sequencer the image check passes quietly and runs no probe."""
    result, log = _run_prerequisites(tmp_path, settings)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARNING:" not in result.stdout and "ERROR:" not in result.stdout
    lines = result.stdout.splitlines()
    assert (
        "  Kubernetes sequencer (batches spanning namespaces): checking image "
        f"{image} in namespace ns-a"
    ) in lines
    assert (
        f"  Kubernetes sequencer image {image}: pulls and provides sh, kubectl, "
        "grep, sleep"
    ) in lines
    assert [line for line in log if line.startswith("IMAGE")] == ["IMAGE ns-a"]
    assert not [line for line in log if line.startswith("PROBE")]


def test_default_image_failure_alone_is_a_warning_without_rbac_problems(tmp_path):
    """The RBAC and image checks are independent warnings."""
    result, _ = _run_prerequisites(tmp_path, "", FAKE_IMAGE_ERROR="timed out")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _PREREQUISITES_PASSED in result.stdout.splitlines()
    assert _PREREQUISITES_MISSING not in result.stdout
    assert "failed its check in namespace ns-a: timed out" in result.stdout


@pytest.mark.parametrize(
    ("settings", "image"),
    [("", DEFAULT_SEQUENCER_IMAGE), (_MIRROR, _SEQUENCER_IMAGE)],
)
def test_sequencer_probe_runs_in_the_target_namespace_and_passes(
    tmp_path, settings, image
):
    """With --sequencer the probe runs once, in the target namespace, with the
    default image unless KUBECTL_SEQUENCER_IMAGE overrides it."""
    result, log = _run_prerequisites(tmp_path, settings, **_ASKED)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ERROR:" not in result.stdout
    assert [line for line in log if line.startswith("PROBE")] == ["PROBE ns-a"]
    lines = result.stdout.splitlines()
    assert _SUMMARY_LINE not in lines
    assert not [line for line in log if line.startswith("IMAGE")]
    assert (
        "  Kubernetes sequencer (batches spanning namespaces): checking image "
        f"{image} in namespace ns-a"
    ) in lines
    assert (
        "  Kubernetes sequencer prerequisites: server version and sequencer "
        "permissions passed"
    ) in lines
    assert (
        "  Kubernetes sequencer probe in namespace ns-a: image, kubectl, token and "
        "API access passed"
    ) in lines


@pytest.mark.parametrize(
    "environment,problem",
    [
        (
            {"FAKE_DENY": "patch jobs.batch ns-a"},
            "permission to patch jobs.batch in namespace ns-a",
        ),
        (
            {"FAKE_DENY": "create serviceaccounts ns-a"},
            "permission to create serviceaccounts in namespace ns-a",
        ),
        (
            {"FAKE_DENY": "delete roles.rbac.authorization.k8s.io ns-a"},
            "permission to delete roles.rbac.authorization.k8s.io in namespace ns-a",
        ),
        (
            {"FAKE_DENY": "delete rolebindings.rbac.authorization.k8s.io ns-a"},
            "permission to delete rolebindings.rbac.authorization.k8s.io "
            "in namespace ns-a",
        ),
        ({"FAKE_MINOR": "23"}, "Kubernetes server 1.23 is older than 1.24"),
    ],
)
def test_sequencer_prerequisite_failures_are_errors_with_the_fallback(
    tmp_path, environment, problem
):
    """Asked for, RBAC denial or an old server is an error naming the fallback."""
    result, log = _run_prerequisites(tmp_path, "", **_ASKED, **environment)
    assert "WARNING:" not in result.stdout
    assert (
        "ERROR:Kubernetes sequencer prerequisites (needed only for batches "
        "spanning namespaces) are missing:"
    ) in result.stdout
    assert f"  - {problem}" in result.stdout
    assert f"Fallback: {_FALLBACK}" in result.stdout
    assert "RUNTIME ns-a pvc-a" in log
    assert not [line for line in log if line.startswith(("PROBE", "IMAGE"))]


def test_sequencer_probe_failure_is_an_error_with_the_fallback(tmp_path):
    """[S-23] A sequencer that cannot reach the API is an error with the fallback."""
    result, log = _run_prerequisites(
        tmp_path, "", FAKE_PROBE_ERROR="sequencer probe failed: Forbidden", **_ASKED
    )
    assert "WARNING:" not in result.stdout
    assert (
        "ERROR:Kubernetes sequencer probe (needed only for batches spanning "
        "namespaces) failed in namespace ns-a: sequencer probe failed: Forbidden"
    ) in result.stdout
    assert f"Fallback: {_FALLBACK}" in result.stdout
    assert "RUNTIME ns-a pvc-a" in log


@pytest.mark.parametrize("option", ["-h", "--help"])
def test_help_describes_the_sequencer_option(option):
    """--help documents --sequencer before reading env.sh."""
    result = subprocess.run(
        [_BASH, _ROOT / "validate_env.sh", option],
        check=False,
        cwd=_ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("Usage: validate_env.sh [--sequencer]")
    assert "--sequencer" in result.stdout.split("\n", 1)[1]


def test_an_unknown_argument_is_rejected_with_the_usage():
    """Arguments are no longer silently ignored."""
    result = subprocess.run(
        [_BASH, _ROOT / "validate_env.sh", "--sequenser"],
        check=False,
        cwd=_ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2
    assert "Error: unknown argument: --sequenser" in result.stderr
    assert "Usage: validate_env.sh" in result.stderr


def test_an_invalid_sequencer_image_is_an_error(tmp_path):
    """A configured sequencer image must be digest-pinned, even when unused."""
    result, log = _run_prerequisites(
        tmp_path, "KUBECTL_SEQUENCER_IMAGE=registry.example/kubectl:latest"
    )
    assert (
        "KUBECTL_SEQUENCER_IMAGE=registry.example/kubectl:latest must be a "
        "digest-qualified container image reference"
    ) in result.stdout
    assert not log


def _shell_lines_between(text, first, last):
    """The lines of TEXT from the one starting FIRST up to the one starting LAST."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(first))
    stop = next(i for i, line in enumerate(lines) if line.startswith(last))
    return lines[start:stop]


def _first_index(lines, prefix):
    return next(i for i, line in enumerate(lines) if line.startswith(prefix))


def _run_timed(tmp_path, settings, **environment):
    started = time.monotonic()
    result, log = _run_prerequisites(tmp_path, settings, **environment)
    return result, log, time.monotonic() - started


def test_the_sequencer_and_cluster_branches_run_at_the_same_time(tmp_path):
    """Two fake Jobs that each take 1.5 s finish in well under the 3 s sum."""
    result, log, elapsed = _run_timed(
        tmp_path, "", FAKE_IMAGE_DELAY="1.5", FAKE_RUNTIME_DELAY="1.5"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert {"IMAGE ns-a", "RUNTIME ns-a pvc-a"} <= set(log)
    assert elapsed < 3.0


@pytest.mark.parametrize(("environment", "phase"), [({}, "IMAGE"), (_ASKED, "PROBE")])
def test_the_sequencer_job_and_the_runtime_job_are_running_together(
    tmp_path, environment, phase
):
    """Each fake Job waits for the other to start, which only overlap allows."""
    rendezvous = tmp_path / "rendezvous"
    rendezvous.mkdir()
    result, log = _run_prerequisites(
        tmp_path, "", FAKE_RENDEZVOUS_DIR=str(rendezvous), **environment
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"OVERLAP {phase}" in log
    assert "OVERLAP RUNTIME" in log
    assert not [line for line in log if line.startswith("ALONE")]


@pytest.mark.parametrize(("environment", "phase"), [({}, "IMAGE"), (_ASKED, "PROBE")])
def test_the_inventory_finishes_before_any_validation_job_starts(
    tmp_path, environment, phase
):
    """A slow inventory never lists this run's own temporary Jobs."""
    result, log = _run_prerequisites(
        tmp_path, "", FAKE_INVENTORY_DELAY="0.5", **environment
    )
    assert result.returncode == 0, result.stdout + result.stderr
    first_job = next(
        index for index, line in enumerate(log) if line.startswith((phase, "RUNTIME"))
    )
    assert log.index("INVENTORY ns-a") < first_job


@pytest.mark.parametrize(
    ("environment", "delays"),
    [
        ({}, {"FAKE_IMAGE_DELAY": "0.8"}),
        ({}, {"FAKE_RUNTIME_DELAY": "0.8", "FAKE_INVENTORY_DELAY": "0.4"}),
        (_ASKED, {"FAKE_PROBE_DELAY": "0.8"}),
        (_ASKED, {"FAKE_RUNTIME_DELAY": "0.8"}),
    ],
)
def test_the_output_order_never_depends_on_which_branch_finishes_first(
    tmp_path, environment, delays
):
    """Sequencer lines, then the inventory, the worker selector and the runtime."""
    result, _ = _run_prerequisites(tmp_path, "", **environment, **delays)
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    sequencer_end = (
        "  Kubernetes sequencer probe" if environment else _SUMMARY_LINE[:30]
    )
    markers = [
        "  Kubernetes claim ns-a/pvc-a",
        "  Kubernetes sequencer (batches spanning namespaces)",
        "  Kubernetes sequencer prerequisites",
        sequencer_end,
        "INVENTORY ns-a",
        "  Kubernetes worker selector",
        "  Kubernetes runtime in namespace ns-a",
    ]
    positions = [_first_index(lines, marker) for marker in markers]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)


def _slow_sequencer_fixture(tmp_path):
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    return barrier, {
        "FAKE_BARRIER_DIR": str(barrier),
        "FAKE_MINOR": "23",
        "FAKE_DENY": (
            "create serviceaccounts ns-a,patch jobs.batch ns-a,delete jobs.batch ns-a"
        ),
        # The first problems in the list are the slowest to be answered.
        "FAKE_DELAY": "version=0.8,create serviceaccounts=0.6,patch jobs.batch=0.3",
    }


def test_the_prerequisite_calls_run_concurrently_and_report_in_a_fixed_order(
    tmp_path,
):
    """Ten API reads are in flight at once; problems keep the checks' order."""
    barrier, environment = _slow_sequencer_fixture(tmp_path)
    result, _ = _run_prerequisites(tmp_path, "", **environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "barrier.timeouts").exists()
    assert len(list(barrier.iterdir())) == 10
    problems = [line for line in result.stdout.splitlines() if line.startswith("  - ")]
    assert problems == [
        "  - Kubernetes server 1.23 is older than 1.24, which suspended Jobs require",
        "  - permission to create serviceaccounts in namespace ns-a",
        "  - permission to patch jobs.batch in namespace ns-a",
        "  - permission to delete jobs.batch in namespace ns-a",
    ]


def _real_registration(tmp_path):
    """Settings that use validate_env.sh's own register_error/register_warning.

    Returns the shell settings and the epilogue that writes each registered
    message, NUL-terminated, to errors.out, warnings.out and summaries.out in
    tmp_path.
    """
    source = (_ROOT / "validate_env.sh").read_text(encoding="utf-8")
    registration = source.split("# Initialize error accumulation system", 1)[1].split(
        "# Initialize the error tracking system", 1
    )[0]
    epilogue = textwrap.dedent(f"""\
        printf 'RC:%s\\n' "$?"
        all_registered_errors registered_errors
        all_registered_warnings registered_warnings
        all_registered_summaries registered_summaries
        printf '%s\\0' ${{registered_summaries[@]+"${{registered_summaries[@]}}"}} \\
            > {str(tmp_path / "summaries.out")!r}
        printf '%s\\0' ${{registered_errors[@]+"${{registered_errors[@]}}"}} \\
            > {str(tmp_path / "errors.out")!r}
        printf '%s\\0' ${{registered_warnings[@]+"${{registered_warnings[@]}}"}} \\
            > {str(tmp_path / "warnings.out")!r}
        """)
    return registration + "\ninit_error_tracking\n", epilogue


def _run_registering(tmp_path, *, settings="", padded_wc=False, **environment):
    """Run with the real registration; return stdout, errors and warnings.

    SETTINGS is extra shell that follows the registration (like a test's own
    TEST_DIRS)."""
    registration, epilogue = _real_registration(tmp_path)
    settings = registration + settings
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    script = _prerequisites_script(tmp_path, settings, epilogue)
    if padded_wc:
        _pad_wc(tmp_path / "bin")
    result = subprocess.run(
        [_BASH, "-c", script],
        check=False,
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "TMPDIR": str(scratch),
            **environment,
        },
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert not list(scratch.iterdir()), "validation left temporary files behind"
    errors = _read_records(tmp_path / "errors.out")
    warnings = _read_records(tmp_path / "warnings.out")
    return result, errors, warnings


def _read_records(path):
    """The NUL-terminated records of PATH (the epilogue writes one empty record
    when nothing was registered)."""
    return [record for record in path.read_text().split("\0") if record]


def _registered_summaries(tmp_path):
    """The summary lines of the last _run_registering, in registration order."""
    return _read_records(tmp_path / "summaries.out")


_RUNTIME_FAILED = (
    "Kubernetes workload image or PVC runtime validation failed in namespace "
    "ns-a: runtime boom"
)


def test_errors_and_warnings_of_both_branches_are_all_registered_in_order(tmp_path):
    """Default mode: A's warnings, then B's warning and its runtime error."""
    result, errors, warnings = _run_registering(
        tmp_path,
        FAKE_DENY="create serviceaccounts ns-a",
        FAKE_IMAGE_ERROR="image boom",
        FAKE_RUNTIME_ERROR="runtime boom",
        FAKE_INVENTORY_WARNING="inventory warning",
        FAKE_IMAGE_DELAY="0.5",
    )
    assert "RC:1" in result.stdout.splitlines()
    assert errors == [_RUNTIME_FAILED]
    assert len(warnings) == 3
    assert warnings[0].startswith("Kubernetes sequencer prerequisites")
    assert "  - permission to create serviceaccounts in namespace ns-a" in warnings[0]
    assert warnings[1].startswith("Kubernetes sequencer image")
    assert "failed its check in namespace ns-a: image boom" in warnings[1]
    assert warnings[2] == "inventory warning"


_KUBERNETES_SUMMARY = (
    "Kubernetes: context test-context, namespace ns-a, claim pvc-a, "
    "1 Ready amd64 node: OK"
)
_SEQUENCER_SUMMARY = "Kubernetes sequencer (only for batches spanning namespaces): OK"


def test_default_output_has_no_detail_lines_and_two_summary_lines(tmp_path):
    """Without --verbose a passing run states what was validated and nothing else."""
    result, errors, warnings = _run_registering(tmp_path, VALIDATE_VERBOSE="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (errors, warnings) == ([], [])
    assert [line for line in result.stdout.splitlines() if line.startswith("  ")] == []
    assert _registered_summaries(tmp_path) == [_KUBERNETES_SUMMARY, _SEQUENCER_SUMMARY]


def test_verbose_output_keeps_every_detail_line(tmp_path):
    """--verbose prints each check's detail line, as before the summary existed."""
    result, _, _ = _run_registering(tmp_path)
    lines = result.stdout.splitlines()
    for prefix in (
        "  Kubernetes target: namespace ns-a, claim(s) pvc-a",
        "  Kubernetes context: test-context",
        "  Kubernetes claim ns-a/pvc-a",
        "  Kubernetes sequencer (batches spanning namespaces)",
        "  Kubernetes sequencer prerequisites",
        "  Kubernetes sequencer image",
        "  Kubernetes sequencer checks only warn",
        "  Kubernetes worker selector",
        "  Kubernetes runtime in namespace ns-a",
    ):
        assert any(line.startswith(prefix) for line in lines), prefix


def test_the_summary_names_the_sequencer_probe_when_it_was_asked_for(tmp_path):
    """--sequencer states that the full probe passed."""
    result, _, _ = _run_registering(
        tmp_path, VALIDATE_VERBOSE="", VALIDATE_SEQUENCER="1"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert _registered_summaries(tmp_path)[1] == "Kubernetes sequencer (full probe): OK"


def test_the_summary_names_every_claim(tmp_path):
    """Several claims are listed, comma-separated."""
    result, _, _ = _run_registering(tmp_path, settings=_AGGREGATE, VALIDATE_VERBOSE="")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _registered_summaries(tmp_path)[0] == (
        "Kubernetes: context test-context, namespace ns-a, claims lustre-a,lustre-b, "
        "1 Ready amd64 node: OK"
    )


def test_a_failed_sequencer_check_is_a_warning_and_not_a_summary_line(tmp_path):
    """Only passed checks are summarized; the problem stays in the warnings."""
    result, errors, warnings = _run_registering(
        tmp_path,
        VALIDATE_VERBOSE="",
        FAKE_DENY="create serviceaccounts ns-a",
        FAKE_IMAGE_ERROR="image boom",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert errors == []
    assert len(warnings) == 2
    assert _registered_summaries(tmp_path) == [_KUBERNETES_SUMMARY]


def test_a_failed_runtime_check_is_an_error_and_not_a_summary_line(tmp_path):
    """No Kubernetes OK line is printed when the runtime validation failed."""
    _, errors, _ = _run_registering(
        tmp_path, VALIDATE_VERBOSE="", FAKE_RUNTIME_ERROR="runtime boom"
    )
    assert errors == [_RUNTIME_FAILED]
    assert _registered_summaries(tmp_path) == [_SEQUENCER_SUMMARY]


_SKEW = "Warning: version difference between client (1.36) and server (1.34)"
_RUNTIME_DUMP = (
    f"{_SKEW}\n"
    "Error: the runtime validation Job did not finish within 180 seconds on node "
    "n1 (a claim's volume could not be mounted there)\n"
    "Pod event FailedMount: mount failed\nIs the MGS running?\n"
    "\n===== Kubernetes validation diagnostic: pods.yaml =====\nkind: List"
)
_RUNTIME_HEAD = (
    "Kubernetes workload image or PVC runtime validation failed in namespace "
    "ns-a: the runtime validation Job did not finish within 180 seconds on node "
    "n1 (a claim's volume could not be mounted there)\n"
    "Pod event FailedMount: mount failed\nIs the MGS running?"
)


def test_a_runtime_failure_names_its_cause_and_saves_the_dump_under_logs(tmp_path):
    """Default mode: the cause stays in the error, the dump goes to a log file,
    and kubectl's skew advisory is a warning, not part of the error."""
    logs = tmp_path / "logs"
    logs.mkdir()
    _, errors, warnings = _run_registering(
        tmp_path,
        VALIDATE_VERBOSE="",
        LOGS_DIR=str(logs),
        FAKE_RUNTIME_ERROR=_RUNTIME_DUMP,
    )
    assert warnings == [f"kubectl: {_SKEW}"]
    [error] = errors
    head, saved = error.split("\nFull Pod and Job diagnostics: ")
    assert head == _RUNTIME_HEAD
    path = Path(saved.removesuffix(" (or rerun with --verbose)"))
    assert path.parent == logs
    assert path.name.startswith("validate_env-kubectl-runtime-ns-a-")
    assert path.read_text(encoding="utf-8") == (
        "===== Kubernetes validation diagnostic: pods.yaml =====\nkind: List\n"
    )


def test_verbose_keeps_the_runtime_dump_inline(tmp_path):
    """--verbose: the dump follows the cause in the error; no file is written."""
    logs = tmp_path / "logs"
    logs.mkdir()
    _, errors, warnings = _run_registering(
        tmp_path, LOGS_DIR=str(logs), FAKE_RUNTIME_ERROR=_RUNTIME_DUMP
    )
    assert warnings == [f"kubectl: {_SKEW}"]
    assert errors == [
        _RUNTIME_HEAD
        + "\n\n===== Kubernetes validation diagnostic: pods.yaml =====\nkind: List"
    ]
    assert not list(logs.iterdir())


def test_an_identical_warning_is_reported_once(tmp_path):
    """Concurrent checks that see the same kubectl advisory print it once."""
    registration, _ = _real_registration(tmp_path)
    result = subprocess.run(
        [
            _BASH,
            "-c",
            registration + textwrap.dedent("""\
                register_warning same
                register_warning other
                register_warning same
                all_registered_warnings warnings
                printf '%s|' "${warnings[@]}"
                """),
        ],
        check=False,
        env={**os.environ, "TMPDIR": str(tmp_path)},
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "same|other|"


def test_a_killed_cluster_branch_fails_validation(tmp_path):
    """A branch that dies before registering anything is an error, not a pass,
    and the temporary files it held are still removed."""
    result, errors, _ = _run_registering(
        tmp_path, VALIDATE_VERBOSE="", FAKE_KILL_CLUSTER_BRANCH="1"
    )
    assert "RC:0" not in result.stdout.splitlines()
    assert errors == [
        "The Kubernetes worker and runtime checks ended unexpectedly (status 137)"
    ]
    assert _registered_summaries(tmp_path) == [_SEQUENCER_SUMMARY]


def test_sequencer_and_runtime_errors_are_registered_in_branch_order(tmp_path):
    """--sequencer: the probe's error precedes the runtime error."""
    result, errors, warnings = _run_registering(
        tmp_path,
        VALIDATE_SEQUENCER="1",
        FAKE_PROBE_ERROR="probe boom",
        FAKE_RUNTIME_ERROR="runtime boom",
        FAKE_INVENTORY_WARNING="inventory warning",
        FAKE_RUNTIME_DELAY="0.1",
        FAKE_PROBE_DELAY="0.5",
    )
    assert "RC:1" in result.stdout.splitlines()
    assert len(errors) == 2
    assert errors[0].startswith("Kubernetes sequencer probe")
    assert "failed in namespace ns-a: probe boom" in errors[0]
    assert errors[1] == _RUNTIME_FAILED
    assert warnings == ["inventory warning"]


def test_a_failing_node_discovery_skips_the_runtime_but_not_the_sequencer(tmp_path):
    """Branch B stops at the discovery error as before; branch A still reports."""
    result, log = _run_prerequisites(
        tmp_path, "", FAKE_DISCOVERY_ERROR="no Ready nodes"
    )
    assert result.returncode == 1
    assert "DISCOVERY" in log
    assert not [line for line in log if line.startswith("RUNTIME")]
    lines = result.stdout.splitlines()
    assert "ERROR:Kubernetes worker selector validation failed: no Ready nodes" in lines
    assert _IMAGE_PASSED in lines
    assert "INVENTORY ns-a" in lines


def _fake_object_api(events):
    """Settings: a Job/ServiceAccount/Role API that records creates and deletes.

    A Job never finishes, so each wait polls until the check is stopped.
    """
    return textwrap.dedent(f"""\
        kubectl_create_owned_object() {{
            printf 'create %s %s\\n' "$2" "$3" >> {str(events)!r}
            printf -v "$1" 'uid-%s' "$3"
        }}
        kubectl_delete_owned_object() {{
            printf 'delete %s %s\\n' "$1" "$2" >> {str(events)!r}
        }}
        kubectl_job_terminal_state() {{
            printf 'waiting %s\\n' "$2" >> {str(events)!r}
            return 2
        }}
        """)


def _wait_for_both_jobs_to_wait(events, process):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        lines = events.read_text().splitlines() if events.exists() else []
        waiting = {line.split()[1] for line in lines if line.startswith("waiting")}
        if any(name.endswith("-validation") for name in waiting) and any(
            name.endswith(("-sequencer-image", "-sequencer-probe")) for name in waiting
        ):
            return
        assert process.poll() is None, lines
        time.sleep(0.05)
    raise AssertionError("both Jobs did not start waiting")


@pytest.mark.parametrize("environment", [{}, _ASKED])
def test_term_during_validation_deletes_both_jobs_and_removes_temporary_files(
    tmp_path, environment
):
    """TERM stops both branches; each cleans up its objects; the script exits 143."""
    events = tmp_path / "events"
    settings, epilogue = _real_registration(tmp_path)
    script = _prerequisites_script(
        tmp_path, settings + _fake_object_api(events), epilogue
    )
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with subprocess.Popen(
        [_BASH, "-c", script],
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "TMPDIR": str(scratch),
            "REAL_JOBS": "1",
            **environment,
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as process:
        _wait_for_both_jobs_to_wait(events, process)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=60) == 143
    lines = events.read_text().splitlines()
    created = [line.split(maxsplit=1)[1] for line in lines if line.startswith("create")]
    deleted = [line.split(maxsplit=1)[1] for line in lines if line.startswith("delete")]
    assert len(created) == (4 if environment else 1) + 1
    assert sorted(created) == sorted(deleted)
    assert any(line.startswith("Job sst-elb-") for line in deleted)
    for name in {line.split()[1] for line in lines if line.startswith("waiting")}:
        after_delete = lines[lines.index(f"delete Job {name}") + 1 :]
        assert not [line for line in after_delete if line == f"waiting {name}"]
    assert not list(scratch.iterdir()), "temporary files were left behind"
    assert not (tmp_path / "errors.out").exists()


def test_term_during_the_prerequisite_calls_starts_no_sequencer_job(tmp_path):
    """TERM while the can-i calls are running exits 143 and starts no image Job."""
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    settings, epilogue = _real_registration(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with subprocess.Popen(
        [_BASH, "-c", _prerequisites_script(tmp_path, settings, epilogue)],
        env={
            **os.environ,
            "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
            "TMPDIR": str(scratch),
            "FAKE_BARRIER_DIR": str(barrier),
            "FAKE_DELAY": "version=2",
        },
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as process:
        deadline = time.monotonic() + 60
        while len(list(barrier.iterdir())) < 10 and time.monotonic() < deadline:
            time.sleep(0.02)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=60) == 143
    assert "IMAGE ns-a" not in (tmp_path / "calls.log").read_text().splitlines()
    assert not list(scratch.iterdir()), "temporary files were left behind"
