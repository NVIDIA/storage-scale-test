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

"""Schema validation of filesystem results documents (shared validator helpers)."""

import json
from pathlib import Path

import jsonschema
import pytest
import zstandard

from lib import filesystem_results as results

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "docs" / "schemas"


def _load(name):
    return json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))


def _validator(name):
    schema = _load(name)
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    )


def validate_results_document(doc):
    """Raise jsonschema.ValidationError unless ``doc`` conforms to the results schema."""
    _validator("filesystem-batch-results-1.schema.json").validate(doc)


def validate_sidecar_lines(path):
    """Validate every line of a zstd JSONL live sidecar; return the parsed lines."""
    validator = _validator("live-client-series-1.schema.json")
    raw = zstandard.ZstdDecompressor().stream_reader(open(path, "rb")).read()
    lines = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    for line in lines:
        validator.validate(line)
    assert "schema" in lines[0]
    assert all("schema" not in line for line in lines[1:])
    return lines


def minimal_document():
    """A small valid document (one ordinary-style group, one complete execution)."""
    fragment = {
        "fragment_version": results.FRAGMENT_VERSION,
        "kind": "mdtest",
        "executions": [],
        "diagnostics": [],
    }
    return {
        "schema": results.SCHEMA,
        "schema_version": results.SCHEMA_VERSION,
        "generator": results.generator_block(["x"], {}),
        "batch": {
            "root": "/r",
            "datestamp": None,
            "manifest_version": None,
            "manifest_sha256": None,
            "sealed": None,
            "substrate": None,
        },
        "batch_outcome": {
            "complete": False,
            "executions": {"total": 0, **{k: 0 for k in results.LEDGER_STATES}},
            "results": {k: 0 for k in results.RESULT_STATES},
            "groups_reported": [],
            "groups_not_reported": [],
        },
        "groups": [],
        "executions": [],
        "diagnostics": [],
    }, fragment


def test_schemas_are_valid_2020_12_and_accept_minimal_document():
    document, _ = minimal_document()
    validate_results_document(document)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("batch_outcome"),
        lambda d: d.update(schema="other"),
        lambda d: d.update(schema_version="2.0"),
        lambda d: d["batch_outcome"]["executions"].pop("total"),
        lambda d: d.update(diagnostics=[{"code": "x"}]),
    ],
)
def test_schema_rejects_malformed_documents(mutate):
    document, _ = minimal_document()
    mutate(document)
    with pytest.raises(jsonschema.ValidationError):
        validate_results_document(document)


def test_sidecar_lines_validate_and_bad_lines_fail(tmp_path):
    path = tmp_path / "s.jsonl.zst"
    header = {
        "schema": results.LIVE_SCHEMA,
        "schema_version": results.LIVE_SCHEMA_VERSION,
        "execution_id": "0001",
        "phase": "write",
        "mix_type": "",
        "occurrence": 0,
        "clients": ["a", "b"],
    }
    with results.SidecarWriter(path, header) as writer:
        writer.write_interval(1000, [1, None])
        writer.write_interval(2000, [2, 3], [2048, None])
    lines = validate_sidecar_lines(path)
    assert len(lines) == 3
    assert "done_bytes_delta" not in lines[1]
    assert lines[2]["done_bytes_delta"] == [2048, None]
    bad = _load("live-client-series-1.schema.json")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(bad).validate({"elapsed_ms": "x"})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(bad).validate(
            {"elapsed_ms": 1, "bytes_per_s": [], "done_bytes_delta": [1.5]}
        )
