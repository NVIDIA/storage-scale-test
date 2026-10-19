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

"""Storage target identity for saved snapshots, and the mixed-target report guard.

Standard library only: ``--help`` of the reporters must work before any
third-party dependency is available. ``storage_target_name_default`` in
``lib/env_functions.sh`` implements rule 2 below; a parity test keeps the two
in agreement.

Identity rule, first match wins:

1. ``STORAGE_TARGET_NAME`` is non-empty: that name (a *recorded* name).
2. ``TEST_DIRS`` keys, trailing ``/`` stripped, deduplicated, sorted by UTF-8
   bytes, joined with ``,``. On the kubectl substrate, when every key is a
   valid claim-qualified root (``[namespace/]claim:path``), each key is first
   normalized to ``<namespace>/<claim>:<path>``, the namespace defaulting to
   ``KUBECTL_NAMESPACE``.
3. Otherwise the target is unknown.

Snapshots written by the current shell tooling always carry the effective name:
when ``STORAGE_TARGET_NAME`` is unset, the shell derives it from ``TEST_DIRS``
(rule 2) and stores it in ``STORAGE_TARGET_NAME`` before saving, so those
snapshots are *recorded* (rule 1). Rule 2 is applied here only to
snapshots that lack the name (written before the feature existed, or edited by
hand); their names are marked *derived* (``recorded=False``) because the
snapshot never stated them.

Cached-report target cells (the ``storage_target_name`` CSV column) hold:

* empty: the target is unknown;
* a bare name: one known target;
* ``mixed: ITEM, ITEM, ...``: a label written for several inputs, where each
  ITEM is a JSON string (a known name, ``json.dumps(name,
  ensure_ascii=False)``) or the bare token ``unknown`` (an input whose target
  could not be verified). Older caches wrote the same layout with names
  between plain double quotes and no ``unknown`` item; they remain readable.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sys
from typing import Callable, Iterable, Mapping

STORAGE_TARGET_KEY = "STORAGE_TARGET_NAME"
UNKNOWN_TARGET = "unknown"
ALLOW_MIXED_TARGETS_FLAG = "--allow-mixed-targets"
MIXED_PREFIX = "mixed: "
UNKNOWN_ITEM = UNKNOWN_TARGET

_SUBSTRATE_KEY = "EXECUTION_SUBSTRATE"
_KUBECTL = "kubectl"
_NAMESPACE_KEY = "KUBECTL_NAMESPACE"
_TEST_DIRS_KEY = "TEST_DIRS"
_SEPARATOR = ", "
_RESERVED_COMPONENT = ".storage-scale-test"
_NAMESPACE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_CLAIM_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_COMPONENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True)
class StorageTarget:
    """A target name (None when unknown) and whether the snapshot stated it.

    ``recorded`` is False only for a name derived at report time from a
    snapshot that lacks ``STORAGE_TARGET_NAME`` (a pre-feature snapshot).
    """

    name: str | None
    recorded: bool = False


class MixedStorageTargetsError(ValueError):
    """Report inputs come from more than one known storage target."""


def _text(env: Mapping, key: str) -> str:
    """One snapshot scalar as a string; absent and null are empty."""
    value = env.get(key)
    return "" if value is None else str(value)


def test_dirs_target_name(env: Mapping) -> str | None:
    """Name from the TEST_DIRS roots alone (rule 2), or None without roots."""
    test_dirs = env.get(_TEST_DIRS_KEY)
    if not isinstance(test_dirs, Mapping):
        return None
    roots = {str(path).rstrip("/") or "/" for path in test_dirs if str(path)}
    if not roots:
        return None
    return ",".join(sorted(roots, key=lambda root: root.encode("utf-8")))


def _valid_claim(claim: str) -> bool:
    """A DNS-1123 subdomain, as ``kubectl_validate_object_name`` checks it."""
    if not claim or len(claim) > 253 or not _CLAIM_RE.match(claim):
        return False
    return all(
        label and len(label) <= 63 and _LABEL_RE.match(label)
        for label in claim.split(".")
    )


def _claim_path(value: str) -> str | None:
    """The normalized claim-relative path of a key, or None when invalid."""
    if not value or any(char in value for char in "\n\r\t"):
        return None
    components = [component for component in value.split("/") if component]
    for component in components:
        if component in (".", "..", _RESERVED_COMPONENT):
            return None
        if not _COMPONENT_RE.match(component):
            return None
    return "/".join(components) or None


def qualified_root(key: str, default_namespace: str) -> str | None:
    """``<namespace>/<claim>:<path>`` for a claim-qualified key, else None.

    Mirrors ``kubectl_split_test_dir_key`` in the Kubernetes helpers.
    """
    if ":" not in key:
        return None
    qualifier, rest = key.split(":", 1)
    if "/" in qualifier:
        namespace, claim = qualifier.split("/", 1)
    else:
        namespace, claim = default_namespace, qualifier
    if not namespace or len(namespace) > 63 or not _NAMESPACE_RE.match(namespace):
        return None
    if "/" in claim or not _valid_claim(claim):
        return None
    path = _claim_path(rest)
    if path is None:
        return None
    return f"{namespace}/{claim}:{path}"


def _qualified_roots_name(env: Mapping) -> str | None:
    """Rule 2 for kubectl: the normalized claim-qualified roots, when all are."""
    test_dirs = env.get(_TEST_DIRS_KEY)
    if not isinstance(test_dirs, Mapping) or not test_dirs:
        return None
    namespace = _text(env, _NAMESPACE_KEY)
    roots = {qualified_root(str(key), namespace) for key in test_dirs}
    if None in roots:
        return None
    return ",".join(sorted(roots, key=lambda root: root.encode("utf-8")))


def default_storage_target_name(env: Mapping | None) -> str | None:
    """Derive the name from the substrate or the roots, ignoring any explicit name."""
    if not isinstance(env, Mapping):
        return None
    if _text(env, _SUBSTRATE_KEY) == _KUBECTL:
        qualified = _qualified_roots_name(env)
        if qualified is not None:
            return qualified
    return test_dirs_target_name(env)


def storage_target_from_env(env: Mapping | None) -> StorageTarget:
    """Apply the identity rule to a saved snapshot (None or empty is unknown)."""
    if not isinstance(env, Mapping):
        return StorageTarget(None, False)
    explicit = _text(env, STORAGE_TARGET_KEY)
    if explicit:
        return StorageTarget(explicit, True)
    return StorageTarget(default_storage_target_name(env), False)


def describe_storage_target(target: StorageTarget) -> str:
    """Display a target: ``name``, ``name (derived)``, or ``unknown``.

    ``(derived)`` marks a name worked out from a snapshot that never recorded
    one; snapshots saved by the current tooling always show the bare name.
    """
    if target.name is None:
        return UNKNOWN_TARGET
    return target.name if target.recorded else f"{target.name} (derived)"


def _known_name(value: StorageTarget | str | None) -> str | None:
    name = value.name if isinstance(value, StorageTarget) else value
    return name or None


def quote_target_name(name: str) -> str:
    """One name as a JSON string: quotes and control characters cannot confuse it."""
    return json.dumps(name, ensure_ascii=False)


def mixed_target_label(names: Iterable[str], include_unknown: bool = False) -> str:
    """The ``mixed: ...`` label for ``names``, ending ``unknown`` when requested."""
    items = [quote_target_name(name) for name in names]
    if include_unknown:
        items.append(UNKNOWN_ITEM)
    return MIXED_PREFIX + _SEPARATOR.join(items)


def _mixed_error(
    by_name: dict[str, list[str]], derived: set[str]
) -> MixedStorageTargetsError:
    lines = ["inputs come from different storage targets:"]
    lines.extend(
        f"  {quote_target_name(name)}{' (derived)' if name in derived else ''}: "
        f"{_SEPARATOR.join(labels)}"
        for name, labels in by_name.items()
    )
    if derived:
        lines.append(
            "(derived: results saved before v2.0.0 have no STORAGE_TARGET_NAME; "
            "the name comes from their TEST_DIRS)"
        )
    lines.append(f"report each target separately or pass {ALLOW_MIXED_TARGETS_FLAG}")
    return MixedStorageTargetsError("\n".join(lines))


def resolve_report_target(
    sources: Iterable[tuple[str, StorageTarget | str | None]],
    allow_mixed: bool,
    warn: Callable[[str], None],
) -> str:
    """Return the report's target label, or refuse inputs from several targets.

    ``sources`` pairs a label (an input directory or CSV) with its target.
    Distinct known names raise ``MixedStorageTargetsError`` unless
    ``allow_mixed``, which warns and labels the report ``mixed: "a", "b"``.
    Inputs of unknown target beside a known one only warn.
    """
    by_name: dict[str, list[str]] = {}
    unknown: list[str] = []
    derived: set[str] = set()
    for label, value in sources:
        name = _known_name(value)
        if name is None:
            unknown.append(label)
            continue
        by_name.setdefault(name, []).append(label)
        if isinstance(value, StorageTarget) and not value.recorded:
            derived.add(name)
    names = sorted(by_name, key=lambda name: name.encode("utf-8"))
    by_name = {name: by_name[name] for name in names}
    if len(names) > 1:
        if not allow_mixed:
            raise _mixed_error(by_name, derived)
        shown = mixed_target_label(names)
        detail = _SEPARATOR.join(
            f"{quote_target_name(name)} ({_SEPARATOR.join(labels)})"
            for name, labels in by_name.items()
        )
        warn(f"report mixes storage targets: {detail}")
    elif names:
        shown = names[0]
    else:
        return UNKNOWN_TARGET
    if unknown:
        warn(
            f"cannot verify storage target of {len(unknown)} input(s): "
            f"{_SEPARATOR.join(unknown)}"
        )
    return shown


_QUOTED_NAME = re.compile(r'"([^"]*)"')
_DECODER = json.JSONDecoder()


def _parse_mixed_items(body: str) -> tuple[list[str], bool] | None:
    """Decode ``ITEM, ITEM, ...``; None when the body is not that grammar."""
    names: list[str] = []
    unknown = False
    position = 0
    while position < len(body):
        if body.startswith('"', position):
            try:
                name, position = _DECODER.raw_decode(body, position)
            except ValueError:
                return None
            names.append(name)
        elif body.startswith(UNKNOWN_ITEM, position):
            unknown = True
            position += len(UNKNOWN_ITEM)
        else:
            return None
        if position < len(body):
            if not body.startswith(_SEPARATOR, position):
                return None
            position += len(_SEPARATOR)
    return names, unknown


def parse_target_cell(value: str) -> tuple[list[str], bool]:
    """Known names in one cached cell, and whether it includes an unknown target.

    A cell is empty (unknown), a bare name, or a ``mixed:`` label (see the module
    docstring). A ``mixed:`` body that is not the JSON-item grammar is read as
    the older layout of plain double-quoted names.
    """
    if not value:
        return [], True
    if not value.startswith(MIXED_PREFIX):
        return [value], False
    body = value[len(MIXED_PREFIX) :]
    parsed = _parse_mixed_items(body)
    if parsed is None:
        return _QUOTED_NAME.findall(body), False
    return parsed


def cached_target_sources(
    csv_file: str, values: Iterable[str]
) -> list[tuple[str, str | None]]:
    """Guard sources for a cached report: one per distinct name, plus unknown rows.

    ``values`` holds the target column of every cached row; an empty cell, or a
    ``mixed:`` label that includes ``unknown``, contributes an unverified source.
    """
    parsed = [parse_target_cell(cell) for cell in values]
    names = sorted({name for known, _ in parsed for name in known})
    sources: list[tuple[str, str | None]] = [
        (f"{csv_file} rows", name) for name in names
    ]
    if any(unknown for _, unknown in parsed):
        sources.append((f"{csv_file} rows without a target", None))
    return sources


def has_unverified_source(
    sources: Iterable[tuple[str, StorageTarget | str | None]],
) -> bool:
    """True when any guard source has no known target."""
    return any(_known_name(value) is None for _, value in sources)


def exported_target_cell(label: str, unverified: bool) -> str:
    """The CSV cell for a report ``label`` that must not launder unknown inputs.

    A fully unknown report exports empty. A verified one exports its label. When
    some inputs were unverified the cell is a ``mixed:`` label ending ``unknown``,
    so a later ``--from-csv`` sees the uncertainty and warns again.
    """
    if label == UNKNOWN_TARGET:
        return ""
    if not unverified:
        return label
    names, _ = parse_target_cell(label)
    return mixed_target_label(names, include_unknown=True)


def guarded_report_target(
    sources: Iterable[tuple[str, StorageTarget | str | None]], allow_mixed: bool
) -> str:
    """Resolve the report label, warning on stderr; exit 1 on refused mixed targets."""
    try:
        return resolve_report_target(
            sources,
            allow_mixed,
            lambda text: print(f"WARNING: {text}", file=sys.stderr),
        )
    except MixedStorageTargetsError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
