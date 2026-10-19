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

"""Shell contracts for the POSIX kubectl coordinator-Job sequencer."""

import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import textwrap

import pytest

_REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
_SEQUENCER = (
    _REPOSITORY_ROOT
    / "storage-tests"
    / "fs"
    / "kubectl"
    / "_nv-elbencho-kubectl-sequencer.sh"
)
_SHELL = shutil.which("dash") or shutil.which("sh") or "/bin/sh"
_SEQUENCER_NAME = "storage-scale-test-sequencer"
_POLL_ENV = "KUBECTL_SEQUENCER_POLL_SECONDS"
_JSONPATH = (
    'jsonpath={.metadata.uid}{"|"}{.spec.suspend}{"|"}'
    '{range .status.conditions[?(@.status=="True")]}{.type}{","}{end}'
)
_COMPLETE = "Complete"
_FAILED = "Failed"
_SUSPENDED = "Suspended,"
_PATCH = "patch"
_NS_A = "bench-a"
_NS_B = "bench-b"
_CALLS_LOG = "calls.log"
_EVENTS_LOG = "events.log"
_RUN_TIMEOUT_SECONDS = 30
# Never inherit a real Pod's service environment into the fake cluster.
_IN_CLUSTER_ENV = (
    "KUBERNETES_SERVICE_HOST",
    "KUBERNETES_SERVICE_PORT",
    "KUBECTL_SEQUENCER_SERVICE_ACCOUNT_DIR",
    "KUBECONFIG",
)

# Fake kubectl: logs argv, serves per-Job state files, honors the UID test op,
# and moves an unsuspended Job to its outcome after "delay" observations.
_FAKE_KUBECTL = textwrap.dedent("""\
    #!/bin/sh
    root=$FAKE_KUBECTL_ROOT
    printf '%s\\n' "$*" >>"$root/calls.log"
    printf '%s\\n' "${KUBECONFIG:-}" >>"$root/kubeconfig.log"
    while [ $# -gt 0 ]; do
        case $1 in
        --request-timeout=*) shift ;;
        -n) ns=$2; shift 2 ;;
        *) break ;;
        esac
    done
    verb=$1 name=$3
    shift 3
    job=$root/state/${ns}__$name
    if [ ! -f "$job.uid" ]; then
        echo "Error from server (NotFound): jobs.batch \\"$name\\" not found" >&2
        exit 1
    fi
    uid=$(cat "$job.uid")
    if [ "$verb" = patch ]; then
        case $3 in
        *"\\"value\\":\\"$uid\\""*) ;;
        *) echo "jsonpatch test operation does not apply" >&2; exit 1 ;;
        esac
        echo false >"$job.suspend"
        echo "PATCH $ns/$name" >>"$root/events.log"
        echo "job.batch/$name patched"
        exit 0
    fi
    if [ -f "$job.getfail" ]; then
        cat "$job.getfail" >&2
        rm -f "$job.getfail"
        exit 1
    fi
    suspend=$(cat "$job.suspend")
    conditions=$(cat "$job.conditions")
    case $suspend:$conditions in
    true:* | *Complete* | *Failed*) ;;
    *)
        left=$(($(cat "$job.delay") - 1))
        echo "$left" >"$job.delay"
        if [ "$left" -le 0 ]; then
            conditions="$(cat "$job.outcome"),"
            echo "$conditions" >"$job.conditions"
        fi
        ;;
    esac
    echo "GET $ns/$name $uid|$suspend|$conditions" >>"$root/events.log"
    printf '%s|%s|%s' "$uid" "$suspend" "$conditions"
""")


def _cluster(tmp_path):
    """Create the fake kubectl and its empty state directory."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    kubectl = bin_dir / "kubectl"
    kubectl.write_text(_FAKE_KUBECTL, encoding="utf-8")
    kubectl.chmod(0o755)
    (tmp_path / "state").mkdir()
    return tmp_path


def _add_job(root, target, **state):
    """Store one Job's state; target is (namespace, name, uid)."""
    namespace, name, uid = target
    values = {
        "uid": uid,
        "suspend": "true",
        "conditions": _SUSPENDED,
        "outcome": _COMPLETE,
        "delay": "1",
    }
    values.update(state)
    prefix = root / "state" / f"{namespace}__{name}"
    for key, value in values.items():
        Path(f"{prefix}.{key}").write_text(f"{value}\n", encoding="utf-8")


def _run(root, args, env=None, via_dash_c=False):
    """Run the sequencer with the fake kubectl first on PATH."""
    run_env = {
        key: value
        for key, value in os.environ.items()
        if key not in (_POLL_ENV, *_IN_CLUSTER_ENV)
    }
    run_env.update(
        {
            "PATH": f"{root / 'bin'}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_KUBECTL_ROOT": str(root),
            _POLL_ENV: "1",
        }
    )
    run_env.update(env or {})
    if via_dash_c:
        script = _SEQUENCER.read_text(encoding="utf-8")
        command = [_SHELL, "-c", script, _SEQUENCER_NAME, *args]
    else:
        command = [_SHELL, str(_SEQUENCER), *args]
    return subprocess.run(
        command,
        check=False,
        env=run_env,
        text=True,
        capture_output=True,
        timeout=_RUN_TIMEOUT_SECONDS,
    )


def _lines(root, name):
    path = root / name
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _patch_calls(root):
    """Return argv lists of every patch invocation, in order."""
    calls = [line.split(" ") for line in _lines(root, _CALLS_LOG)]
    return [argv for argv in calls if _PATCH in argv]


def _patched_jobs(root):
    return [argv[5] for argv in _patch_calls(root)]


def _event_index(root, wanted):
    return _lines(root, _EVENTS_LOG).index(wanted)


def _complete_index(root, target, suspend="false"):
    """Index of the first observation reporting the target Job Complete."""
    namespace, name, uid = target
    return _event_index(root, f"GET {namespace}/{name} {uid}|{suspend}|{_COMPLETE},")


def _patch_index(root, target):
    return _event_index(root, f"PATCH {target[0]}/{target[1]}")


def _triples(targets):
    return [word for target in targets for word in target]


_ORDERED = (
    (_NS_A, "coord-0", "uid-0"),
    (_NS_B, "coord-1", "uid-1"),
    (_NS_A, "coord-2", "uid-2"),
)


def _expected_patch(namespace, name, uid):
    body = (
        '[{"op":"test","path":"/metadata/uid","value":"' + uid + '"},'
        '{"op":"replace","path":"/spec/suspend","value":false}]'
    )
    return [
        "--request-timeout=30s",
        "-n",
        namespace,
        _PATCH,
        "job",
        name,
        "--type=json",
        "-p",
        body,
    ]


@pytest.mark.parametrize("via_dash_c", (False, True))
def test_unsuspends_targets_in_order_after_each_completes(tmp_path, via_dash_c):
    root = _cluster(tmp_path)
    for target in _ORDERED:
        _add_job(root, target)
    result = _run(root, _triples(_ORDERED), via_dash_c=via_dash_c)
    assert result.returncode == 0, result.stderr
    assert "all segments complete" in result.stdout
    assert _patch_calls(root) == [_expected_patch(*target) for target in _ORDERED]
    for previous, current in zip(_ORDERED, _ORDERED[1:]):
        assert _patch_index(root, current) > _complete_index(root, previous)


def test_observe_uses_exact_jsonpath_argv(tmp_path):
    root = _cluster(tmp_path)
    target = (_NS_A, "coord-0", "uid-0")
    _add_job(root, target, suspend="false", conditions=f"{_COMPLETE},")
    result = _run(root, list(target))
    assert result.returncode == 0, result.stderr
    first_call = _lines(root, _CALLS_LOG)[0]
    assert first_call == " ".join(
        ["--request-timeout=30s", "-n", _NS_A, "get", "job", "coord-0", "-o", _JSONPATH]
    )


def test_restart_skips_complete_and_running_jobs(tmp_path):
    root = _cluster(tmp_path)
    done, running, waiting = _ORDERED
    _add_job(root, done, suspend="false", conditions=f"SuccessCriteriaMet,{_COMPLETE},")
    _add_job(root, running, suspend="", conditions="", delay="2")
    _add_job(root, waiting)
    result = _run(root, _triples(_ORDERED))
    assert result.returncode == 0, result.stderr
    assert _patched_jobs(root) == [waiting[1]]
    running_done = _complete_index(root, running, suspend="")
    assert _patch_index(root, waiting) > running_done


def test_failed_target_stops_later_targets(tmp_path):
    root = _cluster(tmp_path)
    first, failing, later = _ORDERED
    _add_job(root, first)
    _add_job(root, failing, outcome=_FAILED)
    _add_job(root, later)
    result = _run(root, _triples(_ORDERED))
    assert result.returncode == 0, result.stderr
    assert "segment 1 (Job bench-b/coord-1) failed" in result.stdout
    assert _patched_jobs(root) == [first[1], failing[1]]


def test_uid_mismatch_exits_without_patch(tmp_path):
    root = _cluster(tmp_path)
    _add_job(root, (_NS_A, "coord-0", "uid-replacement"))
    result = _run(root, [_NS_A, "coord-0", "uid-original"])
    assert result.returncode == 4
    assert "identity mismatch" in result.stderr
    assert not _patch_calls(root)


def test_missing_job_exits_not_found(tmp_path):
    root = _cluster(tmp_path)
    result = _run(root, [_NS_A, "coord-0", "uid-0"])
    assert result.returncode == 3
    assert "Job bench-a/coord-0 no longer exists" in result.stderr


def test_transient_observe_failure_is_retried(tmp_path):
    root = _cluster(tmp_path)
    target = (_NS_A, "coord-0", "uid-0")
    _add_job(root, target, getfail="dial tcp 10.0.0.1:443: connection refused")
    result = _run(root, list(target))
    assert result.returncode == 0, result.stderr
    assert "connection refused" in result.stderr
    assert _patched_jobs(root) == ["coord-0"]


@pytest.mark.parametrize(
    "args",
    (
        [],
        [_NS_A, "coord-0"],
        [_NS_A, "coord-0", "uid-0", _NS_B],
        ["Bench_A", "coord-0", "uid-0"],
        ["a" * 64, "coord-0", "uid-0"],
        [_NS_A, "-coord", "uid-0"],
        [_NS_A, "coord-0", 'uid"},{"op":"remove'],
        [_NS_A, "coord-0", ""],
    ),
)
def test_invalid_arguments_exit_2_without_kubectl(tmp_path, args):
    root = _cluster(tmp_path)
    result = _run(root, args)
    assert result.returncode == 2
    assert "sequencer:" in result.stderr
    assert not _lines(root, _CALLS_LOG)


@pytest.mark.parametrize(
    "env",
    (
        {_POLL_ENV: "0"},
        {_POLL_ENV: "abc"},
        {_POLL_ENV: "010"},
        {_POLL_ENV: "3601"},
    ),
)
def test_invalid_environment_exits_2_without_kubectl(tmp_path, env):
    root = _cluster(tmp_path)
    result = _run(root, [_NS_A, "coord-0", "uid-0"], env=env)
    assert result.returncode == 2
    assert not _lines(root, _CALLS_LOG)


def test_script_is_portable_and_yaml_embeddable():
    text = _SEQUENCER.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert os.access(_SEQUENCER, os.X_OK)
    for forbidden in ("\t", "@@", "[[", "local ", "function "):
        assert forbidden not in text, forbidden
    assert not re.search(r"(^|\s)\(\(", text, re.MULTILINE)
    checkbashisms = shutil.which("checkbashisms")
    if checkbashisms:
        result = subprocess.run(
            [checkbashisms, str(_SEQUENCER)],
            check=False,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def _tool_bin(tmp_path, *tools):
    """A PATH directory holding only TOOLS, linked from the host PATH."""
    bin_dir = tmp_path / "tools"
    bin_dir.mkdir()
    for tool in tools:
        (bin_dir / tool).symlink_to(shutil.which(tool))
    return bin_dir


def test_missing_kubectl_exits_5_not_job_gone(tmp_path):
    """No kubectl on PATH is a missing command (5), never a deleted Job (3)."""
    bin_dir = _tool_bin(tmp_path, "grep", "sleep")
    result = subprocess.run(
        [_SHELL, str(_SEQUENCER), _NS_A, "coord-0", "uid-0"],
        check=False,
        env={"PATH": str(bin_dir), _POLL_ENV: "1"},
        text=True,
        capture_output=True,
        timeout=_RUN_TIMEOUT_SECONDS,
    )
    assert result.returncode == 5
    assert "required command missing: kubectl" in result.stderr
    assert "no longer exists" not in result.stderr


def test_kubectl_that_cannot_run_exits_5_not_job_gone(tmp_path):
    """A shell "kubectl: not found" (exit 127) is not the API's NotFound."""
    root = _cluster(tmp_path)
    (root / "bin" / "kubectl").write_text(
        "#!/bin/sh\necho 'sh: kubectl: not found' >&2\nexit 127\n", encoding="utf-8"
    )
    result = _run(root, [_NS_A, "coord-0", "uid-0"])
    assert result.returncode == 5
    assert "no longer exists" not in result.stderr


def test_non_api_not_found_message_is_retried(tmp_path):
    """Only "Error from server (NotFound)" means the Job is gone."""
    root = _cluster(tmp_path)
    target = (_NS_A, "coord-0", "uid-0")
    _add_job(root, target, getfail="error: the server could not find: not found")
    result = _run(root, list(target))
    assert result.returncode == 0, result.stderr
    assert "cannot observe" in result.stderr
    assert _patched_jobs(root) == ["coord-0"]


def _service_account(tmp_path, files=("token", "ca.crt")):
    """Create a fake mounted ServiceAccount directory and a writable HOME."""
    account = tmp_path / "serviceaccount"
    account.mkdir()
    for name in files:
        (account / name).write_text(f"{name}-data\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    return account, home


def _in_cluster_env(account, home, host="10.96.0.1"):
    return {
        "KUBERNETES_SERVICE_HOST": host,
        "KUBERNETES_SERVICE_PORT": "443",
        "KUBECTL_SEQUENCER_SERVICE_ACCOUNT_DIR": str(account),
        "HOME": str(home),
    }


@pytest.mark.parametrize(
    "host,server",
    [("10.96.0.1", "https://10.96.0.1:443"), ("fd00::1", "https://[fd00::1]:443")],
)
def test_in_cluster_run_uses_an_explicit_service_account_kubeconfig(
    tmp_path, host, server
):
    """--request-timeout disables kubectl's in-cluster fallback, so the
    sequencer must hand kubectl an explicit kubeconfig for its ServiceAccount."""
    root = _cluster(tmp_path)
    target = (_NS_A, "coord-0", "uid-0")
    _add_job(root, target)
    account, home = _service_account(tmp_path)
    result = _run(root, list(target), env=_in_cluster_env(account, home, host))
    assert result.returncode == 0, result.stderr
    config = home / ".kube" / "sequencer-config"
    assert set(_lines(root, "kubeconfig.log")) == {str(config)}
    text = config.read_text(encoding="utf-8")
    assert f'server: "{server}"' in text
    assert f'tokenFile: "{account}/token"' in text
    assert f'certificate-authority: "{account}/ca.crt"' in text
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert all("--request-timeout=30s" in line for line in _lines(root, _CALLS_LOG))


@pytest.mark.parametrize("missing", ["token", "ca.crt"])
def test_in_cluster_run_without_service_account_credentials_exits_5(tmp_path, missing):
    root = _cluster(tmp_path)
    target = (_NS_A, "coord-0", "uid-0")
    _add_job(root, target)
    present = tuple(name for name in ("token", "ca.crt") if name != missing)
    account, home = _service_account(tmp_path, present)
    result = _run(root, list(target), env=_in_cluster_env(account, home))
    assert result.returncode == 5
    assert f"credentials missing: {account}/{missing}" in result.stderr
    assert not _lines(root, _CALLS_LOG)


def test_outside_a_pod_the_callers_kubeconfig_is_kept(tmp_path):
    root = _cluster(tmp_path)
    target = (_NS_A, "coord-0", "uid-0")
    _add_job(root, target)
    result = _run(root, list(target), env={"KUBECONFIG": "/operator/config"})
    assert result.returncode == 0, result.stderr
    assert set(_lines(root, "kubeconfig.log")) == {"/operator/config"}
