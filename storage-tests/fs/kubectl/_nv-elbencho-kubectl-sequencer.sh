#!/bin/sh

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

# Kubernetes sequencer: start suspended coordinator Jobs one storage target at
# a time. Arguments are NAMESPACE JOB_NAME JOB_UID triples in target order.
# Progress is derived from live Job state on every start, so a restarted
# sequencer resumes at the first Job that is not Complete. Plain POSIX sh: it
# runs as `sh FILE ARGS` or `sh -c SCRIPT NAME ARGS` in a kubectl image.
# Exit: 0 all complete or a target Failed, 2 bad input, 3 Job gone (the API
# server answered NotFound), 4 UID changed, 5 a required command (kubectl,
# grep, sleep) is missing or cannot run, or the in-cluster ServiceAccount
# credentials are missing.

set -u

POLL=${KUBECTL_SEQUENCER_POLL_SECONDS:-10}
TIMEOUT=${KUBECTL_SEQUENCER_REQUEST_TIMEOUT_SECONDS:-30}
JSONPATH='jsonpath={.metadata.uid}{"|"}{.spec.suspend}{"|"}{range .status.conditions[?(@.status=="True")]}{.type}{","}{end}'
NL='
'

info() { printf 'sequencer: %s\n' "$*"; }
warn() { printf 'sequencer: %s\n' "$*" >&2; }
die() {
    die_code=$1
    shift
    warn "$*"
    exit "$die_code"
}

# in_range VALUE MAX: decimal integer 1..MAX without leading zeros.
in_range() {
    case $1 in '' | 0* | *[!0-9]*) return 1 ;; esac
    [ "${#1}" -le 9 ] && [ "$1" -le "$2" ]
}

# matches VALUE MAX_LENGTH ERE: one line, bounded length, full ERE match.
matches() {
    case $1 in '' | *"$NL"*) return 1 ;; esac
    [ "${#1}" -le "$2" ] && printf '%s\n' "$1" | grep -Eq "$3"
}

validate_args() {
    [ "$#" -gt 0 ] && [ $(($# % 3)) -eq 0 ] ||
        die 2 "usage: sequencer NAMESPACE JOB_NAME JOB_UID [NAMESPACE JOB_NAME JOB_UID ...]"
    while [ "$#" -gt 0 ]; do
        matches "$1" 63 '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$' ||
            die 2 "invalid namespace: $1"
        matches "$2" 253 '^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$' ||
            die 2 "invalid Job name: $2"
        matches "$3" 128 '^[A-Za-z0-9][A-Za-z0-9_.:-]*$' ||
            die 2 "invalid Job UID: $3"
        shift 3
    done
}

# require_commands: every external command the script runs must exist.
require_commands() {
    for required in kubectl grep sleep; do
        command -v "$required" >/dev/null 2>&1 ||
            die 5 "required command missing: $required"
    done
}

# use_service_account_kubeconfig: kubectl ignores its in-cluster credentials
# whenever a client flag such as --request-timeout is given and then falls
# back to localhost:8080. Inside a Pod, write an explicit kubeconfig for the
# mounted ServiceAccount instead; tokenFile lets kubectl pick up rotated tokens.
# Outside a Pod (no KUBERNETES_SERVICE_HOST) the caller's kubeconfig is used.
use_service_account_kubeconfig() {
    [ -n "${KUBERNETES_SERVICE_HOST:-}" ] || return 0
    sa_dir=${KUBECTL_SEQUENCER_SERVICE_ACCOUNT_DIR:-/var/run/secrets/kubernetes.io/serviceaccount}
    for sa_file in token ca.crt; do
        [ -r "$sa_dir/$sa_file" ] ||
            die 5 "in-cluster ServiceAccount credentials missing: $sa_dir/$sa_file"
    done
    sa_host=$KUBERNETES_SERVICE_HOST
    case $sa_host in *:*) sa_host="[$sa_host]" ;; esac
    sa_config_dir=${HOME:-/tmp}/.kube
    mkdir -p "$sa_config_dir" || die 5 "cannot create $sa_config_dir"
    KUBECONFIG=$sa_config_dir/sequencer-config
    (
        umask 077
        printf '%s\n' \
            'apiVersion: v1' \
            'kind: Config' \
            'clusters:' \
            '- name: in-cluster' \
            '  cluster:' \
            "    server: \"https://$sa_host:${KUBERNETES_SERVICE_PORT:-443}\"" \
            "    certificate-authority: \"$sa_dir/ca.crt\"" \
            'users:' \
            '- name: sequencer' \
            '  user:' \
            "    tokenFile: \"$sa_dir/token\"" \
            'contexts:' \
            '- name: in-cluster' \
            '  context:' \
            '    cluster: in-cluster' \
            '    user: sequencer' \
            'current-context: in-cluster' >"$KUBECONFIG"
    ) || die 5 "cannot write $KUBECONFIG"
    export KUBECONFIG
}

kube() { kubectl --request-timeout="${TIMEOUT}s" -n "$@"; }

# observe NS NAME: sets OBS_RC, OBS_OUT (stdout) and OBS_ERR (stderr). One
# capture holds kubectl stderr first, then a final "RC STDOUT" line.
observe() {
    obs_all=$({
        obs_out=$(kube "$1" get job "$2" -o "$JSONPATH")
        obs_rc=$?
        printf '\n%s %s' "$obs_rc" "$obs_out"
    } 2>&1)
    obs_last=${obs_all##*"$NL"}
    OBS_RC=${obs_last%% *}
    OBS_OUT=${obs_last#* }
    OBS_ERR=${obs_all%"$NL"*}
}

# parse_state: split OBS_OUT "UID|SUSPEND|COND,COND," into OBS_* fields.
parse_state() {
    case $OBS_OUT in *'|'*'|'*) ;; *) return 1 ;; esac
    OBS_UID=${OBS_OUT%%|*}
    obs_rest=${OBS_OUT#*|}
    OBS_SUSPEND=${obs_rest%%|*}
    OBS_CONDITIONS=",${obs_rest#*|}"
}

unsuspend() {
    kube "$1" patch job "$2" --type=json -p \
        '[{"op":"test","path":"/metadata/uid","value":"'"$3"'"},{"op":"replace","path":"/spec/suspend","value":false}]'
}

# run_target INDEX NS NAME UID: returns once the Job is Complete.
run_target() {
    tgt="target $1 (Job $2/$3)"
    patched=0
    while :; do
        observe "$2" "$3"
        if [ "$OBS_RC" != 0 ]; then
            # 126/127: the shell could not run kubectl at all. That is
            # never evidence that the Job is gone.
            case $OBS_RC in
            126 | 127) die 5 "required command missing or not executable: kubectl ($OBS_ERR)" ;;
            esac
            # Only the API server's NotFound proves the Job no longer exists.
            case $OBS_ERR in
            *"Error from server (NotFound)"*) die 3 "Job $2/$3 no longer exists" ;;
            esac
            warn "warning: cannot observe $tgt; retrying: $OBS_ERR"
        elif ! parse_state; then
            warn "warning: unexpected state for $tgt; retrying: $OBS_OUT"
        else
            [ "$OBS_UID" = "$4" ] ||
                die 4 "identity mismatch for $tgt: expected UID $4, found '$OBS_UID'"
            case $OBS_CONDITIONS in
            *,Complete,*)
                info "$tgt complete"
                return 0
                ;;
            *,Failed,*)
                info "$tgt failed; later targets will not start"
                exit 0
                ;;
            esac
            if [ "$OBS_SUSPEND" = true ] && [ "$patched" = 0 ]; then
                info "starting $tgt"
                if unsuspend "$2" "$3" "$4"; then
                    patched=1
                    continue
                fi
                warn "warning: unsuspend of $tgt failed; will re-observe"
            fi
        fi
        patched=0
        sleep "$POLL"
    done
}

require_commands
in_range "$POLL" 3600 ||
    die 2 "KUBECTL_SEQUENCER_POLL_SECONDS must be an integer 1..3600: $POLL"
in_range "$TIMEOUT" 3600 ||
    die 2 "KUBECTL_SEQUENCER_REQUEST_TIMEOUT_SECONDS must be an integer 1..3600: $TIMEOUT"
validate_args "$@"
use_service_account_kubeconfig

index=0
while [ "$#" -gt 0 ]; do
    run_target "$index" "$1" "$2" "$3"
    index=$((index + 1))
    shift 3
done
info "all targets complete"
