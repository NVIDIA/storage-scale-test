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

"""Contracts for validate_env.sh's Kubernetes sequencer probe.

kubectl_validate_sequencer_probe runs against a small fake API made of
function overrides: every object lives in one file under api/, so cleanup can
be checked object by object, including objects whose create response was
lost and foreign objects that only share a deterministic name.
"""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import textwrap
import time

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
_FUNCTIONS = _ROOT / "storage-tests/fs/kubectl/_nv-elbencho-kubectl-functions.sh"
_BASH = shutil.which("bash") or "/bin/bash"
_ATTEMPT = "1234abcd"
_NONCE = "0123456789abcdef0123456789abcdef"
_SEQUENCER_IMAGE = "registry.example/kubectl@sha256:" + "a" * 64
_NAME = f"sst-elb-{_ATTEMPT}-sequencer"
_NAME_T1 = f"sst-elb-{_ATTEMPT}-s1-sequencer"
_JOB = f"{_NAME}-probe"
_ABSENT = f"sst-elb-{_ATTEMPT}-absent"
_ABSENT_UID = "00000000-0000-0000-0000-000000000000"
_TIMEOUT_VARIABLE = "KUBECTL_SEQUENCER_PROBE_TIMEOUT_SECONDS"
_DELETE_SA = f"delete ServiceAccount ns-a/{_NAME}"
_DELETE_ROLE = f"delete Role ns-a/{_NAME}"
_DELETE_BINDING = f"delete RoleBinding ns-a/{_NAME}"
_DELETE_JOB = f"delete Job ns-a/{_JOB}"
_CROSS_NAMESPACE_DELETES = (
    _DELETE_JOB,
    f"delete RoleBinding ns-b/{_NAME_T1}",
    f"delete Role ns-b/{_NAME_T1}",
    _DELETE_BINDING,
    _DELETE_ROLE,
    _DELETE_SA,
)


def _prelude(tmp_path: Path) -> str:
    """Load the helpers and replace the API with file-backed fakes.

    STATES (space separated) scripts kubectl_job_terminal_state: a number is
    returned as its exit status, a word is the terminal state; when the list
    is exhausted the Job is still running (status 2). LOST_RESPONSE lists
    KIND/NAMESPACE creates the API accepts but whose response is lost.
    """
    return textwrap.dedent(f"""\
        source {str(_FUNCTIONS)!r}
        events={str(tmp_path / 'events')!r}
        api={str(tmp_path / 'api')!r}
        manifests={str(tmp_path / 'manifests')!r}
        states_file={str(tmp_path / 'states')!r}
        mkdir -p "$api" "$manifests"
        printf "%s\\n" "${{STATES-COMPLETE}}" > "$states_file"
        export KUBECTL_NAMESPACE=ns-a KUBECTL_NODE_SELECTOR=storage-test=true
        export KUBECTL_IMAGE_PULL_POLICY=Never KUBECTL_RUN_AS_USER=2000
        export KUBECTL_RUN_AS_GROUP=2000 KUBECTL_SEQUENCER_IMAGE={_SEQUENCER_IMAGE}
        event() {{ printf '%s\\n' "$*" >> "$events"; }}
        object_file() {{ printf '%s/%s.%s.%s' "$api" "$1" "$2" "$3"; }}
        kubectl_generate_attempt_id() {{ printf '{_ATTEMPT}\\n'; }}
        kubectl_generate_ownership_nonce() {{ printf '{_NONCE}\\n'; }}
        kubectl_create_owned_object() {{
            local file
            file=$(object_file "$2" "$4" "$3")
            printf '%s' "$7" > "$manifests/$2.$4.$3.yaml"
            if [[ -e "$file" ]]; then
                event "create-exists $2 $4/$3"
                return 1
            fi
            printf 'uid-%s\\t%s\\t%s\\n' "$3" "$5" "$6" > "$file"
            event "create $2 $4/$3"
            [[ ",${{LOST_RESPONSE:-}}," != *",$2/$4,"* ]] || return 1
            printf -v "$1" 'uid-%s' "$3"
        }}
        kubectl_run_observational() {{
            local file uid nonce run
            file=$(object_file "$4" "$2" "$5")
            [[ -e "$file" ]] || return 0
            IFS=$'\\t' read -r uid nonce run < "$file"
            if [[ "${{!#}}" == *ownership* ]]; then
                printf '%s\\t%s\\t%s\\t%s\\t%s' "$4" "$5" "$uid" "$nonce" "$run"
            else
                printf '%s' "$uid"
            fi
        }}
        kubectl_run_bounded() {{
            event "delete $4 $2/$5"
            rm -f -- "$(object_file "$4" "$2" "$5")"
        }}
        kubectl_job_terminal_state() {{
            local next rest
            read -r next rest < "$states_file" || :
            printf '%s' "$rest" > "$states_file"
            case "$next" in
                '') event "waiting $BASHPID"; return 2 ;;
                [0-9]) event "observe $next"; return "$next" ;;
                *) event "observe $next"; printf -v "$1" '%s' "$next" ;;
            esac
        }}
        _kubectl_print_runtime_validation_diagnostics() {{ event diagnostics; }}
        """)


def _run_probe(tmp_path: Path, body: str = "kubectl_validate_sequencer_probe", **env):
    """Run BODY after the fake API prelude with extra environment ENV."""
    return subprocess.run(
        [_BASH, "-c", _prelude(tmp_path) + body],
        cwd=_ROOT,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, _TIMEOUT_VARIABLE: "30", **env},
        timeout=60,
    )


def _events(tmp_path: Path) -> list[str]:
    path = tmp_path / "events"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _remaining(tmp_path: Path) -> list[str]:
    return sorted(path.name for path in (tmp_path / "api").iterdir())


def _manifest(tmp_path: Path, kind: str, namespace: str, name: str) -> dict:
    path = tmp_path / "manifests" / f"{kind}.{namespace}.{name}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_probe_lost_create_response_is_cleaned_up_by_name(tmp_path):
    """[S-23] An object accepted before a lost response is found and deleted."""
    result = _run_probe(tmp_path, LOST_RESPONSE="Role/ns-a")
    assert result.returncode != 0
    assert _events(tmp_path) == [
        f"create ServiceAccount ns-a/{_NAME}",
        f"create Role ns-a/{_NAME}",
        _DELETE_ROLE,
        _DELETE_SA,
    ]
    assert not _remaining(tmp_path)


def test_probe_never_deletes_a_foreign_object_of_its_name(tmp_path):
    """[S-23] A same-named object with another nonce is reported, not deleted."""
    foreign = tmp_path / "api" / f"RoleBinding.ns-a.{_NAME}"
    foreign.parent.mkdir()
    foreign.write_text(f"uid-foreign\t{'f' * 32}\t{_ATTEMPT}\n", encoding="utf-8")
    result = _run_probe(tmp_path)
    assert result.returncode != 0
    assert f"could not delete sequencer probe RoleBinding ns-a/{_NAME}" in (
        result.stderr
    )
    events = _events(tmp_path)
    assert f"create-exists RoleBinding ns-a/{_NAME}" in events
    assert _DELETE_BINDING not in events
    assert events[-2:] == [_DELETE_ROLE, _DELETE_SA]
    assert _remaining(tmp_path) == [foreign.name]


# A test runner may start with SIGINT ignored (a background job) or blocked,
# and both carry over into bash: a shell cannot trap a signal ignored at its
# start, and a blocked one is never delivered. Run bash with both signals at
# their defaults and unblocked.
_EXEC_WITH_DEFAULT_SIGNALS = (
    "import os, signal, sys\n"
    "signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
    "signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
    "signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT, signal.SIGTERM})\n"
    "os.execv(sys.argv[1], sys.argv[1:])\n"
)


@pytest.mark.parametrize("target", ("caller", "process-group"))
@pytest.mark.parametrize(
    ("signal_number", "status"), ((signal.SIGTERM, 143), (signal.SIGINT, 130))
)
def test_probe_signal_mid_wait_still_deletes_every_object(
    tmp_path, signal_number, status, target
):
    """[S-23] TERM or INT while waiting still removes every probe object.

    The signal goes to the calling shell (which forwards it to the probe) or,
    as a terminal's Ctrl-C does, to the whole process group."""
    script = _prelude(tmp_path) + textwrap.dedent("""\
        kubectl_validate_sequencer_probe ns-b
        event "rc=$?"
        """)
    with subprocess.Popen(
        [sys.executable, "-c", _EXEC_WITH_DEFAULT_SIGNALS, _BASH, "-c", script],
        cwd=_ROOT,
        env={**os.environ, "STATES": ""},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as process:
        deadline = time.monotonic() + 30
        waiting = []
        while not waiting and time.monotonic() < deadline:
            waiting = [line for line in _events(tmp_path) if line.startswith("waiting")]
            time.sleep(0.05)
        assert waiting, _events(tmp_path)
        if target == "caller":
            os.kill(process.pid, signal_number)
        else:
            os.killpg(process.pid, signal_number)
        assert process.wait(timeout=30) == 0
    assert _events(tmp_path)[-7:] == [*_CROSS_NAMESPACE_DELETES, f"rc={status}"]
    assert not _remaining(tmp_path)


@pytest.mark.parametrize(("first", "second"), (("TERM", "TERM"), ("INT", "TERM")))
def test_a_second_signal_cannot_interrupt_the_exit_cleanup(tmp_path, first, second):
    """A signal arriving while the EXIT trap runs is ignored, so the probe's
    cleanup always finishes (the second one models the caller's forwarded TERM
    after a process-group Ctrl-C).

    Each signal is sent only once the child reports it reached that point (its
    traps installed, then its cleanup running), never after a fixed sleep."""
    ready, cleaning = tmp_path / "ready", tmp_path / "cleaning"
    script = textwrap.dedent(f"""\
        source {_FUNCTIONS!s}
        await() {{ until [[ -e "$1" ]]; do sleep 0.01; done; }}
        (
            cleanup() {{ : > {str(cleaning)!r}; sleep 0.5; echo cleaned; }}
            _kubectl_exit_on_signal cleanup
            grep -E '^Sig(Blk|Ign)' /proc/self/status >&2 2>/dev/null || true
            : > {str(ready)!r}
            sleep 5 & wait
        ) &
        child=$!
        await {str(ready)!r}
        kill -{first} "$child"
        await {str(cleaning)!r}
        kill -{second} "$child"
        wait "$child"
        echo "rc=$?"
        """)
    result = subprocess.run(
        [sys.executable, "-c", _EXEC_WITH_DEFAULT_SIGNALS, _BASH, "-c", script],
        check=False,
        text=True,
        capture_output=True,
        timeout=30,
    )
    status = 130 if first == "INT" else 143
    # stderr carries the child's signal mask, to explain any failure.
    assert result.stdout == f"cleaned\nrc={status}\n", result.stderr


@pytest.mark.parametrize("signal_number", (signal.SIGTERM, signal.SIGINT))
def test_repeated_signals_never_cut_probe_cleanup_short(tmp_path, signal_number):
    """[S-23] A burst of signals to the process group (each one also forwarded
    as TERM by the caller) cannot interrupt the cleanup the first one started."""
    script = _prelude(tmp_path) + textwrap.dedent("""\
        kubectl_validate_sequencer_probe ns-b
        event "rc=$?"
        """)
    with subprocess.Popen(
        [sys.executable, "-c", _EXEC_WITH_DEFAULT_SIGNALS, _BASH, "-c", script],
        cwd=_ROOT,
        env={**os.environ, "STATES": ""},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    ) as process:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not any(
            line.startswith("waiting") for line in _events(tmp_path)
        ):
            time.sleep(0.05)
        for _ in range(20):
            try:
                os.killpg(process.pid, signal_number)
            except ProcessLookupError:
                break
            time.sleep(0.002)
        process.wait(timeout=30)
    events = _events(tmp_path)
    # Signals left in the burst after the probe returns may end the caller
    # before it records its status; the probe's own cleanup must be complete.
    deletes = [event for event in events if event.startswith("delete ")]
    assert deletes == list(_CROSS_NAMESPACE_DELETES), events
    assert not _remaining(tmp_path)


@pytest.mark.parametrize("timeout", ("0", "abc", "010", "3601", "-5"))
def test_probe_rejects_an_invalid_timeout_before_any_create(tmp_path, timeout):
    """The probe timeout is validated before any object exists."""
    result = _run_probe(tmp_path, **{_TIMEOUT_VARIABLE: timeout})
    assert result.returncode != 0
    assert f"{_TIMEOUT_VARIABLE}=" in result.stderr
    assert _events(tmp_path) == []


def test_probe_tolerates_transient_observation_failures(tmp_path):
    """[S-23] Up to two consecutive failed observations are retried."""
    result = _run_probe(tmp_path, STATES="1 1 2 1 COMPLETE")
    assert result.returncode == 0, result.stderr
    assert "observe COMPLETE" in _events(tmp_path)
    assert not _remaining(tmp_path)


def test_probe_fails_after_three_failed_observations(tmp_path):
    """Persistent observation failure fails the probe and still cleans up."""
    result = _run_probe(tmp_path, STATES="1 1 1 COMPLETE")
    assert result.returncode != 0
    assert "could not observe the Kubernetes sequencer probe Job" in result.stderr
    assert "observe COMPLETE" not in _events(tmp_path)
    assert not _remaining(tmp_path)


def test_probe_exercises_a_cross_namespace_role_binding(tmp_path):
    """[S-23] A second namespace gets a Role and a binding to the ns-a account."""
    result = _run_probe(tmp_path, "kubectl_validate_sequencer_probe ns-b")
    assert result.returncode == 0, result.stderr
    created = [
        f"create ServiceAccount ns-a/{_NAME}",
        f"create Role ns-a/{_NAME}",
        f"create RoleBinding ns-a/{_NAME}",
        f"create Role ns-b/{_NAME_T1}",
        f"create RoleBinding ns-b/{_NAME_T1}",
        f"create Job ns-a/{_JOB}",
    ]
    assert _events(tmp_path) == [
        *created,
        "observe COMPLETE",
        *_CROSS_NAMESPACE_DELETES,
    ]
    role = _manifest(tmp_path, "Role", "ns-b", _NAME_T1)
    assert role["rules"][0]["resourceNames"] == [_ABSENT]
    binding = _manifest(tmp_path, "RoleBinding", "ns-b", _NAME_T1)
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": _NAME, "namespace": "ns-a"}
    ]
    job = _manifest(tmp_path, "Job", "ns-a", _JOB)
    assert job["spec"]["template"]["spec"]["containers"][0]["args"] == [
        "ns-a",
        _ABSENT,
        _ABSENT_UID,
        "ns-b",
        _ABSENT,
        _ABSENT_UID,
    ]


def test_probe_rejects_the_first_namespace_as_its_second(tmp_path):
    """The cross-namespace probe needs a namespace other than segment 0's."""
    result = _run_probe(tmp_path, "kubectl_validate_sequencer_probe ns-a")
    assert result.returncode != 0
    assert _events(tmp_path) == []


_NOT_FOUND_KUBECTL = """\
#!/bin/sh
printf '%s\\n' "$*" >> "$PROBE_CALLS"
echo "Error from server (NotFound): jobs.batch \\"$6\\" not found" >&2
exit 1
"""
_FORBIDDEN_KUBECTL = """\
#!/bin/sh
echo 'Error from server (Forbidden): jobs.batch is forbidden' >&2
exit 1
"""


def _probe_container(tmp_path: Path) -> list[str]:
    """Render the cross-namespace probe Job and return its full argv."""
    result = _run_probe(tmp_path, "kubectl_validate_sequencer_probe ns-b")
    assert result.returncode == 0, result.stderr
    container = _manifest(tmp_path, "Job", "ns-a", _JOB)["spec"]["template"]["spec"][
        "containers"
    ][0]
    return [*container["command"], *container["args"]]


def _run_container(tmp_path: Path, argv: list[str], kubectl: str | None, **kwargs):
    """Run the probe container command locally with only sh, grep and sleep."""
    bin_dir = tmp_path / "container-bin"
    bin_dir.mkdir(parents=True)
    for tool in ("sh", "grep", "sleep"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    if kubectl is not None:
        (bin_dir / "kubectl").write_text(kubectl, encoding="utf-8")
        (bin_dir / "kubectl").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return subprocess.run(
        argv,
        check=False,
        env={
            "PATH": str(bin_dir),
            "HOME": str(home),
            "PROBE_CALLS": str(tmp_path / "calls"),
            "KUBECTL_SEQUENCER_POLL_SECONDS": "1",
        },
        text=True,
        capture_output=True,
        **kwargs,
    )


def test_probe_job_runs_the_real_sequencer_against_absent_jobs(tmp_path):
    """[S-23] The probe passes only when the sequencer exits 3 for every Job."""
    argv = _probe_container(tmp_path)
    passed = _run_container(tmp_path / "ok", argv, _NOT_FOUND_KUBECTL)
    assert passed.returncode == 0, passed.stderr
    assert passed.stdout.count("answered NotFound") == 2
    calls = (tmp_path / "ok" / "calls").read_text(encoding="utf-8").splitlines()
    assert [call.split()[1:5] for call in calls] == [
        ["-n", "ns-a", "get", "job"],
        ["-n", "ns-b", "get", "job"],
    ]
    missing = _run_container(tmp_path / "missing", argv, None)
    assert missing.returncode == 1
    assert "the sequencer exited 5" in missing.stderr
    with pytest.raises(subprocess.TimeoutExpired):
        _run_container(tmp_path / "forbidden", argv, _FORBIDDEN_KUBECTL, timeout=4)


# --- kubectl_validate_sequencer_image: the default-mode image check ---------

_IMAGE_JOB = f"sst-elb-{_ATTEMPT}-sequencer-image"
_IMAGE_CREATE = f"create Job ns-a/{_IMAGE_JOB}"
_IMAGE_DELETE = f"delete Job ns-a/{_IMAGE_JOB}"
_IMAGE_CHECK = "kubectl_validate_sequencer_image"
# Pods and logs come from POD_ROWS and POD_LOGS; the Job lives in the fake API.
_FAKE_POD_OBSERVATION = textwrap.dedent("""\
    eval "$(declare -f kubectl_run_observational | sed '1s/.*/fake_api_observe ()/')"
    kubectl_run_observational() {
        case "$3 $4" in
            "get pods") printf '%b\\n' "${POD_ROWS:-}" ;;
            "logs -l") printf '%b\\n' "${POD_LOGS:-}" ;;
            *) fake_api_observe "$@" ;;
        esac
    }
    """)


def _run_image_check(tmp_path: Path, **env):
    """Run the image check against the fake API and fake Pod observations."""
    return _run_probe(tmp_path, _FAKE_POD_OBSERVATION + _IMAGE_CHECK, **env)


def test_image_check_passes_and_deletes_its_job(tmp_path):
    """A completed check Job passes and is deleted; no RBAC object is created."""
    result = _run_image_check(tmp_path)
    assert result.returncode == 0, result.stderr
    assert _events(tmp_path) == [_IMAGE_CREATE, "observe COMPLETE", _IMAGE_DELETE]
    assert not _remaining(tmp_path)


def test_image_check_job_has_no_token_no_api_access_and_the_helper_pod_shape(
    tmp_path,
):
    """The Job runs the effective image with the other helper Pods' settings."""
    assert _run_image_check(tmp_path).returncode == 0
    job = _manifest(tmp_path, "Job", "ns-a", _IMAGE_JOB)
    pod = job["spec"]["template"]["spec"]
    container = pod["containers"][0]
    assert pod["automountServiceAccountToken"] is False
    assert "serviceAccountName" not in pod
    assert pod["nodeSelector"] == {"storage-test": "true"}
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 2000,
        "runAsGroup": 2000,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container["image"] == _SEQUENCER_IMAGE
    assert container["imagePullPolicy"] == "Never"
    assert container["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }
    assert container["command"][:2] == ["/bin/sh", "-c"]
    script = container["command"][2]
    assert "for tool in kubectl grep sleep" in script
    assert "kubectl version --client" in script
    assert "get " not in script
    assert job["spec"]["activeDeadlineSeconds"] == 90


@pytest.mark.parametrize(
    "reason",
    (
        "ErrImagePull",
        "ImagePullBackOff",
        "ErrImageNeverPull",
        "InvalidImageName",
        "CreateContainerConfigError",
    ),
)
def test_image_check_fails_fast_on_a_fatal_waiting_reason(tmp_path, reason):
    """These waiting reasons end the wait at once and the Job is deleted."""
    started = time.monotonic()
    result = _run_image_check(
        tmp_path,
        STATES="",
        POD_ROWS=f'{reason}\\tImage "{_SEQUENCER_IMAGE}" is not present',
        **{_TIMEOUT_VARIABLE: "30"},
    )
    assert time.monotonic() - started < 15
    assert result.returncode != 0
    assert f"the Pod cannot start the image: {reason}: Image" in result.stderr
    assert _events(tmp_path)[0] == _IMAGE_CREATE
    assert _events(tmp_path)[-1] == _IMAGE_DELETE
    assert "observe COMPLETE" not in _events(tmp_path)
    assert not _remaining(tmp_path)


def test_image_check_times_out_on_a_slow_pull_and_deletes_its_job(tmp_path):
    """ContainerCreating is not fatal; the bounded wait ends at the timeout."""
    result = _run_image_check(
        tmp_path, STATES="", POD_ROWS="ContainerCreating\\t", **{_TIMEOUT_VARIABLE: "2"}
    )
    assert result.returncode != 0
    assert "did not finish within 2 seconds" in result.stderr
    assert _events(tmp_path)[-1] == _IMAGE_DELETE
    assert not _remaining(tmp_path)


def test_image_check_reports_the_failed_container_log(tmp_path):
    """A Job whose tools are missing fails with the container's own message."""
    result = _run_image_check(
        tmp_path, STATES="FAILED", POD_LOGS="the sequencer image lacks kubectl"
    )
    assert result.returncode != 0
    assert "the check failed in the image: the sequencer image lacks kubectl" in (
        result.stderr
    )
    assert _events(tmp_path) == [_IMAGE_CREATE, "observe FAILED", _IMAGE_DELETE]
    assert not _remaining(tmp_path)


def test_image_check_deletes_a_job_whose_create_response_was_lost(tmp_path):
    """A Job accepted before its response was lost is found by name and deleted."""
    result = _run_image_check(tmp_path, LOST_RESPONSE="Job/ns-a")
    assert result.returncode != 0
    assert _events(tmp_path) == [_IMAGE_CREATE, _IMAGE_DELETE]
    assert not _remaining(tmp_path)


def test_image_check_signal_mid_wait_still_deletes_the_job(tmp_path):
    """TERM while waiting removes the check Job and exits 143."""
    script = (
        _prelude(tmp_path)
        + _FAKE_POD_OBSERVATION
        + _IMAGE_CHECK
        + ('\nevent "rc=$?"\n')
    )
    with subprocess.Popen(
        [_BASH, "-c", script],
        cwd=_ROOT,
        env={**os.environ, "STATES": ""},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as process:
        deadline = time.monotonic() + 30
        waiting = []
        while not waiting and time.monotonic() < deadline:
            waiting = [line for line in _events(tmp_path) if line.startswith("waiting")]
            time.sleep(0.05)
        assert waiting, _events(tmp_path)
        os.kill(int(waiting[0].split()[1]), signal.SIGTERM)
        assert process.wait(timeout=30) == 0
    assert _events(tmp_path)[-2:] == [_IMAGE_DELETE, "rc=143"]
    assert not _remaining(tmp_path)


@pytest.mark.parametrize("timeout", ("0", "abc", "3601"))
def test_image_check_rejects_an_invalid_timeout_before_any_create(tmp_path, timeout):
    """The shared probe timeout is validated before the Job exists."""
    result = _run_image_check(tmp_path, **{_TIMEOUT_VARIABLE: timeout})
    assert result.returncode != 0
    assert f"{_TIMEOUT_VARIABLE}=" in result.stderr
    assert _events(tmp_path) == []


def test_image_check_container_script_needs_its_tools(tmp_path):
    """The container's script passes with the tools and fails naming a missing one."""
    assert _run_image_check(tmp_path).returncode == 0
    script = _manifest(tmp_path, "Job", "ns-a", _IMAGE_JOB)["spec"]["template"]["spec"][
        "containers"
    ][0]["command"]
    argv = [*script[:2], script[2]]
    ok = _run_container(
        tmp_path / "ok", argv, '#!/bin/sh\n[ "$1" = version ] && exit 0\nexit 9\n'
    )
    assert ok.returncode == 0, ok.stderr
    assert "sh, kubectl, grep and sleep are present" in ok.stdout
    missing = _run_container(tmp_path / "missing", argv, None)
    assert missing.returncode == 1
    assert "the sequencer image lacks kubectl" in missing.stderr
    broken = _run_container(tmp_path / "broken", argv, "#!/bin/sh\nexit 1\n")
    assert broken.returncode == 1
    assert "kubectl version --client failed" in broken.stderr
