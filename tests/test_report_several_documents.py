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

"""Several ``--from-json`` documents and the ``--comparisons`` file.

Each fixture is a real result directory reported once for its document;
the reporters then render several documents together as subprocesses.
All values are invented.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from lib.filesystem_batch import report_documents
from lib.filesystem_report_options import (
    add_analysis_arguments,
    add_report_destination_arguments,
    validate_from_json,
)
from lib.results_document import load_document
from tests.test_extract_elbencho_results_json import _DS, _ENV_USED, _write_result
from tests.test_extract_mdtest_elbencho import _DENSE_CSV, _DENSE_OUT
from tests.test_filesystem_results_end_to_end import _build_batch, _publish_results

REPO = Path(__file__).resolve().parent.parent
IO = REPO / "utils" / "extract-elbencho.py"
MDTEST = REPO / "utils" / "extract-mdtest-elbencho.py"
FILESYSTEM = REPO / "utils" / "extract-filesystem.py"
FROM_JSON = "--from-json"
OUTPUT_DIR = "--output-dir"
COMPARISONS = "--comparisons"
RESULTS = "results.json"
REPORT = "report.txt"
HTML = "report.html"
FORCED = "Forced pair"
MIXED = "--allow-mixed-targets"
REPLACES_INPUTS = "--from-json replaces input directories"
# A comparison set renders as <div class="cmp" id="..."><h3>TITLE ...
COMPARISON_TITLES = re.compile(r'<div class="cmp" id="[^"]+"><h3>([^<]*)')


def _run(script, *arguments, check=True):
    done = subprocess.run(
        [sys.executable, str(script), *map(str, arguments)],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if check:
        assert done.returncode == 0, done.stderr
    return done


def _titles(output):
    page = (output / HTML).read_text(encoding="utf-8")
    return [title.strip() for title in COMPARISON_TITLES.findall(page)]


def _io_document(root, name, direct=True, nodes=(1, 2, 4)):
    """Report a result directory of storage target ``name``; return its document."""
    result = root / name / f"elbencho-{_DS}"
    result.mkdir(parents=True)
    env = _ENV_USED + f"STORAGE_TARGET_NAME: {name}\n"
    (result / "env_used.yaml").write_text(env, encoding="utf-8")
    for count in nodes:
        _write_result(result, nodes=count, threads=4, direct=direct)
    output = root / f"report-{name}"
    _run(IO, result, OUTPUT_DIR, output)
    return output / RESULTS


@pytest.fixture(name="documents", scope="module")
def documents_fixture(tmp_path_factory):
    """Two documents differing only in storage target, and one differing also
    in IO mode (so that nothing pairs automatically)."""
    root = tmp_path_factory.mktemp("several")
    return {
        "alpha": _io_document(root, "alpha"),
        "beta": _io_document(root, "beta"),
        "slow": _io_document(root, "slow", direct=False),
        "root": root,
    }


def _forced(tmp_path, *sections):
    path = tmp_path / "comparisons.yaml"
    listing = "".join(f"    - '{section}'\n" for section in sections)
    path.write_text(f"- title: {FORCED}\n  sections:\n{listing}", encoding="utf-8")
    return path


def test_several_documents_render_one_report_with_a_comparison(tmp_path, documents):
    output = tmp_path / "out"
    done = _run(
        IO, FROM_JSON, documents["alpha"], documents["beta"], OUTPUT_DIR, output
    )
    # One page for both documents, and a comparison across them.
    assert sorted(path.name for path in output.glob("*.html")) == [HTML]
    assert _titles(output)
    # Each document's text and PNGs sit in its own subdirectory; none at the top.
    assert not (output / REPORT).exists()
    assert not list(output.glob("*.png"))
    for number in (1, 2):
        part = output / f"d{number}"
        assert (part / REPORT).is_file()
        assert list(part.glob("*.png"))
    assert "Storage target: alpha" in done.stdout
    assert "Storage target: beta" in done.stdout
    # Each part is what its document renders alone.
    for number, name in ((1, "alpha"), (2, "beta")):
        alone = tmp_path / f"alone-{name}"
        _run(IO, FROM_JSON, documents[name], OUTPUT_DIR, alone)
        part = output / f"d{number}"
        assert (part / REPORT).read_text(encoding="utf-8") == (
            alone / REPORT
        ).read_text(encoding="utf-8")
        assert sorted(p.name for p in part.glob("*.png")) == sorted(
            p.name for p in alone.glob("*.png")
        )


def test_front_door_filters_and_groups_apply_to_every_document(tmp_path, documents):
    output = tmp_path / "out"
    done = _run(
        FILESYSTEM,
        FROM_JSON,
        documents["alpha"],
        documents["beta"],
        OUTPUT_DIR,
        output,
        "--only-nodes",
        "2",
        "--groups",
        "0000",
    )
    rows = [line for line in done.stdout.splitlines() if line[:1].isdigit()]
    assert len(rows) == 4 and all(line.split("|")[0].strip() == "2" for line in rows)
    for number in (1, 2):
        assert (output / f"d{number}" / REPORT).is_file()
    assert (output / HTML).is_file()
    unknown = _run(
        FILESYSTEM,
        FROM_JSON,
        documents["alpha"],
        documents["beta"],
        OUTPUT_DIR,
        tmp_path / "other",
        "--groups",
        "0009",
        check=False,
    )
    assert unknown.returncode == 2
    assert "Unknown document groups: 0009" in unknown.stderr


def test_front_door_mixes_ordinary_and_batch_documents(tmp_path, documents):
    batch = tmp_path / "batch"
    _publish_results(_build_batch(batch))
    first = tmp_path / "batch-report"
    _run(FILESYSTEM, batch, OUTPUT_DIR, first)
    output = tmp_path / "out"
    _run(FILESYSTEM, FROM_JSON, documents["alpha"], first / RESULTS, OUTPUT_DIR, output)
    assert (output / "d1" / REPORT).is_file()
    for group in ("0001", "0002", "0003"):
        assert (output / "d2" / "groups" / group / REPORT).is_file()
    assert (output / "d2" / "reports.txt").is_file()
    assert (output / HTML).is_file()
    assert not (output / "d2" / HTML).exists()


def _mdtest_document(root, name):
    """Report a metadata result directory of storage target ``name``."""
    result = root / name / f"mdtest-elbencho-{_DS}"
    result.mkdir(parents=True)
    (result / "env_used.yaml").write_text(
        "MDTEST_BRANCH_FACTOR: 7\nMDTEST_ITEMS_PER_DIR: 100\n"
        f"MDTEST_ITERATIONS: 1\nSTORAGE_TARGET_NAME: {name}\n",
        encoding="utf-8",
    )
    stem = f"mdtest-elbencho-c_001-t_008_{_DS}_iter1"
    (result / f"{stem}.csv").write_text(_DENSE_CSV, encoding="utf-8")
    (result / f"{stem}.out").write_text(_DENSE_OUT, encoding="utf-8")
    output = root / f"report-{name}"
    _run(MDTEST, result, OUTPUT_DIR, output)
    return output / RESULTS


def test_several_mdtest_documents(tmp_path):
    documents = [_mdtest_document(tmp_path, name) for name in ("one", "two")]
    output = tmp_path / "out"
    done = _run(MDTEST, FROM_JSON, *documents, OUTPUT_DIR, output)
    assert done.stdout.count("=== Performance Rates (ops/sec) ===") == 2
    for number in (1, 2):
        assert (output / f"d{number}" / REPORT).is_file()
    assert _titles(output)


@pytest.fixture(name="mixed", scope="module")
def mixed_fixture(documents, tmp_path_factory):
    """One IO and one metadata document."""
    root = tmp_path_factory.mktemp("mixed")
    return [documents["alpha"], _mdtest_document(root, "meta")]


def test_front_door_options_apply_across_kinds_of_several_documents(tmp_path, mixed):
    output = tmp_path / "out"
    done = _run(
        FILESYSTEM, FROM_JSON, *mixed, OUTPUT_DIR, output, "--normalize-to", "4"
    )
    assert "normalized to 4 nodes" in done.stdout
    for number in (1, 2):
        assert (output / f"d{number}" / REPORT).is_file()
    assert (output / HTML).is_file()


def test_batch_and_ordinary_documents_share_options_across_kinds(tmp_path, mixed):
    batch = tmp_path / "batch"
    _publish_results(_build_batch(batch))
    first = tmp_path / "batch-report"
    _run(FILESYSTEM, batch, OUTPUT_DIR, first)
    output = tmp_path / "out"
    # The batch contributes only an IO group; --normalize-to is for the
    # other document.
    _run(
        FILESYSTEM,
        FROM_JSON,
        first / RESULTS,
        mixed[1],
        OUTPUT_DIR,
        output,
        "--groups",
        "0001,0000",
        "--normalize-to",
        "4",
    )
    assert (output / "d1" / "groups" / "0001" / REPORT).is_file()
    assert (output / "d2" / REPORT).is_file()


@pytest.mark.parametrize("selection", [["--kind", "io"], ["--groups", "0000"]])
def test_option_matching_no_selected_group_is_refused_before_any_output(
    tmp_path, documents, selection
):
    output = tmp_path / "out"
    done = _run(
        FILESYSTEM,
        FROM_JSON,
        documents["alpha"],
        documents["beta"],
        OUTPUT_DIR,
        output,
        "--normalize-to",
        "4",
        *selection,
        check=False,
    )
    assert done.returncode == 2
    assert "--normalize-to applies to no selected mdtest group" in done.stderr
    assert not output.exists()


def test_kind_selecting_only_io_refuses_normalize_to_among_mixed_documents(
    tmp_path, mixed
):
    output = tmp_path / "out"
    done = _run(
        FILESYSTEM,
        FROM_JSON,
        *mixed,
        OUTPUT_DIR,
        output,
        "--kind",
        "io",
        "--normalize-to",
        "4",
        check=False,
    )
    assert done.returncode == 2
    assert "applies to no selected mdtest group" in done.stderr
    assert not output.exists()


def _replay_args(output, *arguments):
    parser = argparse.ArgumentParser()
    add_analysis_arguments(parser, "all", explicit_only=True)
    add_report_destination_arguments(parser)
    return parser.parse_args([OUTPUT_DIR, str(output), *arguments])


def test_each_document_engine_receives_only_its_kinds_options(tmp_path, mixed):
    commands = []
    real_run = subprocess.run

    def engine(command, **kwargs):
        # Other commands (e.g. the project-version lookup) run for real.
        if command[0] != sys.executable:
            return real_run(command, **kwargs)  # pylint: disable=subprocess-run-check
        assert kwargs == {"check": False}
        commands.append(command)
        report = Path(command[command.index(OUTPUT_DIR) + 1]) / REPORT
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("text\n", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    args = _replay_args(
        tmp_path / "out", "--normalize-to=4", "--only-iodepths=1", "--only-nodes=1"
    )
    documents = [load_document(path) for path in mixed]
    with mock.patch("lib.filesystem_batch.subprocess.run", side_effect=engine):
        assert report_documents(documents, args, "all") == 0
    by_engine = {Path(command[1]).name: command[2:] for command in commands}
    io_options = [item for item in by_engine[IO.name] if item.startswith("--only")]
    mdtest_options = [item for item in by_engine[MDTEST.name] if item.startswith("--")]
    assert io_options == ["--only-nodes=1", "--only-iodepths=1"]
    assert "--normalize-to=4" in mdtest_options
    assert not any(item.startswith("--normalize") for item in by_engine[IO.name])
    assert "--only-iodepths=1" not in mdtest_options
    assert "--only-nodes=1" in mdtest_options


def test_options_matching_no_selected_group_start_no_engine(tmp_path, documents):
    args = _replay_args(tmp_path / "out", "--normalize-to=4")
    loaded = [load_document(documents[name]) for name in ("alpha", "beta")]
    with mock.patch("lib.filesystem_batch.subprocess.run") as engine:
        with pytest.raises(ValueError, match="no selected mdtest group"):
            report_documents(loaded, args, "all")
    engine.assert_not_called()
    assert not (tmp_path / "out").exists()


_USAGE_ERRORS = [
    (
        ("--normalize-to", "4"),
        "applies to no selected mdtest group|unrecognized arguments",
    ),
    (("--client-max-heatmap-rows", "0"), "must be greater than zero"),
    (("--only-nodes", "nope"), "(?i)invalid"),
    (("--only-threads", "x"), "(?i)invalid"),
    (("--only-iodepths", "x"), "(?i)invalid"),
    (("--only-sizes", "4X"), "(?i)invalid"),
    (("--groups", "9999"), "(?i)unknown (document )?groups"),
]


def _stale_outputs(output, several):
    """Write a stale report.txt where each document renders, and a bystander."""
    stale = [output / REPORT, output / "d1" / REPORT]
    stale += [output / "d2" / REPORT] if several else []
    for path in stale:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("old\n", encoding="utf-8")
    other = output / "other.txt"
    other.write_text("other\n", encoding="utf-8")
    return stale, other


def _assert_untouched(output, stale, other):
    assert not (output / HTML).exists()
    assert other.read_text(encoding="utf-8") == "other\n"
    for path in stale:
        assert path.read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("script", [IO, FILESYSTEM])
@pytest.mark.parametrize("several", [False, True])
@pytest.mark.parametrize("option, message", _USAGE_ERRORS)
def test_invalid_replay_options_keep_the_existing_report(
    tmp_path, documents, script, several, option, message
):
    output = tmp_path / "out"
    stale, other = _stale_outputs(output, several)
    inputs = [documents["alpha"], *([documents["beta"]] if several else [])]
    done = _run(script, FROM_JSON, *inputs, OUTPUT_DIR, output, *option, check=False)
    assert done.returncode == 2
    assert re.search(message, done.stderr), done.stderr
    _assert_untouched(output, stale, other)


@pytest.mark.parametrize("script", [MDTEST, FILESYSTEM])
@pytest.mark.parametrize("several", [False, True])
def test_invalid_mdtest_replay_groups_keep_the_existing_report(
    tmp_path, script, several
):
    documents = [_mdtest_document(tmp_path, name) for name in ("one", "two")]
    output = tmp_path / "out"
    stale, other = _stale_outputs(output, several)
    inputs = documents[: 2 if several else 1]
    done = _run(
        script, FROM_JSON, *inputs, OUTPUT_DIR, output, "--groups", "9999", check=False
    )
    assert done.returncode == 2
    assert re.search("(?i)unknown (document )?groups", done.stderr), done.stderr
    _assert_untouched(output, stale, other)


@pytest.mark.parametrize("several", [False, True])
@pytest.mark.parametrize("script", [IO, MDTEST, FILESYSTEM])
def test_valid_replay_matching_nothing_removes_the_stale_report(
    tmp_path, documents, script, several
):
    """The engine, not its caller, replaces the previous text report."""
    if script == IO:
        found = [documents["alpha"], documents["beta"]]
    else:
        found = [_mdtest_document(tmp_path, name) for name in ("one", "two")]
    output = tmp_path / "out"
    stale, other = _stale_outputs(output, several)
    inputs = found[: 2 if several else 1]
    done = _run(
        script,
        FROM_JSON,
        *inputs,
        OUTPUT_DIR,
        output,
        "--only-nodes",
        "99",
        check=False,
    )
    assert done.returncode == 1, done.stderr
    # A single document replaces the top-level report; several their own.
    for path in stale[1:] if several else stale[:1]:
        assert not path.exists()
    assert other.read_text(encoding="utf-8") == "other\n"


def test_invalid_batch_document_selection_keeps_the_group_reports(tmp_path):
    batch = tmp_path / "batch"
    _publish_results(_build_batch(batch))
    first = tmp_path / "batch-report"
    _run(FILESYSTEM, batch, OUTPUT_DIR, first)
    output = tmp_path / "out"
    _run(FILESYSTEM, FROM_JSON, first / RESULTS, OUTPUT_DIR, output)
    reports = sorted(output.glob(f"groups/*/{REPORT}"))
    assert reports
    kept = {path: path.read_bytes() for path in [*reports, output / "reports.txt"]}
    done = _run(
        FILESYSTEM,
        FROM_JSON,
        first / RESULTS,
        OUTPUT_DIR,
        output,
        "--only-nodes",
        "nope",
        check=False,
    )
    assert done.returncode == 2
    assert {path: path.read_bytes() for path in kept} == kept


def test_comparison_file_forces_a_pair_between_documents(tmp_path, documents):
    pair = (documents["alpha"], documents["slow"])
    plain = tmp_path / "plain"
    _run(IO, FROM_JSON, *pair, OUTPUT_DIR, plain)
    # The IO mode differs too, so nothing pairs automatically.
    assert not _titles(plain)
    comparisons = _forced(tmp_path, "1:0000", "2:0000")
    for script in (IO, FILESYSTEM):
        output = tmp_path / f"forced-{script.stem}"
        _run(script, FROM_JSON, *pair, OUTPUT_DIR, output, COMPARISONS, comparisons)
        assert _titles(output) == [FORCED]


def test_comparison_file_with_raw_inputs(tmp_path, documents):
    results = [
        documents["root"] / name / f"elbencho-{_DS}" for name in ("alpha", "slow")
    ]
    output = tmp_path / "out"
    comparisons = _forced(tmp_path, "1:0001", "1:0002")
    _run(IO, *results, OUTPUT_DIR, output, COMPARISONS, comparisons, MIXED)
    assert _titles(output) == [FORCED]


@pytest.mark.parametrize("script", [IO, MDTEST, FILESYSTEM])
@pytest.mark.parametrize(
    "content, message",
    [
        ("title: not a list\n", "expected a list"),
        ("- title: x\n  sections: ['1:0000']\n  extra: 1\n", "unknown key"),
        ("- [unbalanced\n", "not valid YAML"),
    ],
)
def test_invalid_comparison_file_fails_before_any_output(
    tmp_path, documents, script, content, message
):
    bad = tmp_path / "bad.yaml"
    bad.write_text(content, encoding="utf-8")
    output = tmp_path / "out"
    done = _run(
        script,
        FROM_JSON,
        documents["alpha"],
        OUTPUT_DIR,
        output,
        COMPARISONS,
        bad,
        check=False,
    )
    assert done.returncode == 2
    assert message in done.stderr
    assert not output.exists()


def test_invalid_comparison_file_fails_before_parsing_raw_inputs(tmp_path, documents):
    bad = tmp_path / "bad.yaml"
    bad.write_text("title: not a list\n", encoding="utf-8")
    result = documents["root"] / "alpha" / f"elbencho-{_DS}"
    output = tmp_path / "out"
    done = _run(IO, result, OUTPUT_DIR, output, COMPARISONS, bad, check=False)
    assert done.returncode == 2
    assert "expected a list" in done.stderr
    assert not output.exists()
    missing = _run(
        IO, result, OUTPUT_DIR, output, COMPARISONS, tmp_path / "none", check=False
    )
    assert missing.returncode == 2
    assert not output.exists()


def test_comparison_naming_an_unknown_section_writes_nothing(tmp_path, documents):
    output = tmp_path / "out"
    done = _run(
        IO,
        FROM_JSON,
        documents["alpha"],
        documents["beta"],
        OUTPUT_DIR,
        output,
        COMPARISONS,
        _forced(tmp_path, "1:0000", "3:0000"),
        check=False,
    )
    assert done.returncode == 2
    assert "no such document" in done.stderr
    assert not output.exists()


def test_output_dir_guardrails(tmp_path, documents):
    alpha, beta = documents["alpha"], documents["beta"]
    for output in (alpha.parent, beta.parent):
        done = _run(IO, FROM_JSON, alpha, beta, OUTPUT_DIR, output, check=False)
        assert done.returncode == 2
        assert "is a results document's directory" in done.stderr
    # Through a symlink too.
    link = tmp_path / "link"
    link.symlink_to(beta.parent)
    done = _run(FILESYSTEM, FROM_JSON, alpha, beta, OUTPUT_DIR, link, check=False)
    assert done.returncode == 2
    assert "choose a separate directory" in done.stderr
    missing = _run(IO, FROM_JSON, alpha, beta, check=False)
    assert missing.returncode == 2
    assert "--output-dir is required" in missing.stderr


def test_a_document_subdirectory_may_not_hold_another_documents_render(tmp_path):
    output = tmp_path / "out"
    (output / "d2").mkdir(parents=True)
    first, second = tmp_path / "first.json", output / "d2" / RESULTS
    first.write_text("{}", encoding="utf-8")
    second.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="results document's directory"):
        validate_from_json([str(first), str(second)], [], str(output))
    validate_from_json([str(first)], [], str(output))


@pytest.mark.parametrize("script", [IO, MDTEST, FILESYSTEM])
def test_input_directories_cannot_mix_with_documents(tmp_path, documents, script):
    result = documents["root"] / "alpha" / f"elbencho-{_DS}"
    output = tmp_path / "out"
    # A directory among the documents ...
    done = _run(
        script, FROM_JSON, documents["alpha"], result, OUTPUT_DIR, output, check=False
    )
    assert done.returncode == 2
    assert REPLACES_INPUTS in done.stderr
    assert not output.exists()
    # ... or before the option.
    done = _run(
        script,
        result,
        FROM_JSON,
        documents["alpha"],
        OUTPUT_DIR,
        output,
        check=False,
    )
    assert done.returncode == 2
    assert REPLACES_INPUTS in done.stderr
    assert not output.exists()


def test_help_describes_the_layout_and_the_comparison_file():
    done = _run(IO, "--help")
    assert "<output-dir>/dN/" in " ".join(done.stdout.split())
    assert COMPARISONS in done.stdout
