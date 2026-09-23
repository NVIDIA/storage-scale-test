<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: Apache-2.0

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
-->

# AGENTS.md

Canonical, always-on instructions for AI coding agents (Codex, Cursor, Claude
Code, etc.). `CLAUDE.md` imports this file so Claude Code reads the same source.
Humans should start with [README.md](README.md).

Kept deliberately lean. Depth lives in referenced docs that you read on demand:
- Coding standards (Python/shell detail, linting): [docs/CODING_STANDARDS.md](docs/CODING_STANDARDS.md)
- Architecture, design decisions, error patterns, recent work: [docs/CONTEXT.md](docs/CONTEXT.md) — read before non-trivial work; update it afterward.

## What this is

Shell and Python tooling that runs distributed storage benchmarks across a fleet
of client nodes and turns the raw output into readable tables and plots. It
orchestrates third-party benchmark binaries (elbencho for filesystem/network,
Warp for S3-compatible object storage) as **separate processes** — they are
**never bundled or vendored** into this repo; users download or build them.

## Architecture

- `storage-tests/{fs,object,network}/` — test entry points; each has `ssh/` and
  `sbatch/` subdirs for the two execution substrates.
- `lib/` — shared Bash libraries (`env_*.sh`, `_*_functions.sh`) + a few Python helpers.
- `utils/` — result processing (`extract-*.sh`/`.py`) and build helpers (`build_tarball.sh`, `build/`).
- `tests/` — `pytest` unit tests for the Python parsers/reporters.
- `docs/` — `DESIGN.md`, `REQUIREMENTS.md`, `ARCHITECTURE_DIAGRAMS.md`, `CONTEXT.md`, `CODING_STANDARDS.md`.
- Config is environment-driven (`env.sh`); `EXECUTION_SUBSTRATE` explicitly
  selects Slurm or passwordless SSH. kubectl is planned (see `ROADMAP.md`),
  not yet implemented.

## Setup

```bash
cp env.sh.template env.sh        # then edit for your environment
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt  # matplotlib, numpy, PyYAML, zstandard
./validate_env.sh                # iterate until it runs cleanly
```

See the README "Getting Started" section for the full quickstart and
[BENCHMARK_RECIPES_FILESYSTEM.md](BENCHMARK_RECIPES_FILESYSTEM.md) for tuned settings.

## Checks (run before committing)

```bash
./utils/run_ci_checks.sh
```

After changing `.github/`, parse every `.yml` and `.yaml` file beneath it with
a YAML parser before committing.

If any required check tool is missing from the sandbox, install it into the
repo's local environment and rerun the check. Do not skip required tooling just
because it is not preinstalled.

The script creates and reuses `.venv-ci` with the pinned tools. Pass
`lint` to run all static checks, or pass `compliance`, `shellcheck`, `black`,
`pylint`, or `pytest` to run one check. Set `CI_CHECK_JOBS=1` in a constrained
sandbox. In a pre-provisioned, network-restricted sandbox, set `CI_BOOTSTRAP=0`
and use `CI_PYTHON` or `CI_SHELLCHECK` to select installed tools.
Run tests and lint through this script or an environment populated from both
requirements files; never treat ambient Python tooling as authoritative.

`black` must be 25.9.0+; `pylint` must score 10.00/10. Details and rationale:
[docs/CODING_STANDARDS.md](docs/CODING_STANDARDS.md).

## Key conventions (non-obvious; not enforced by the linters)

- **Do not vendor third-party binaries or code** (elbencho is GPL-3.0, Warp is
  AGPL-3.0). They are invoked as separate processes; keep it that way.
- **License header**: every text file carries the NVIDIA Apache-2.0 header — the
  block at the top of this file, rendered in the file type's comment syntax.
  Copy it verbatim into new files. `LICENSE` contains the license text itself
  and is exempt; `README.md` carries the same notice in its Copyright section
  at the bottom so the project introduction remains first.
- **elbencho behavior**: do not rely on elbencho source older than **3.0.37**;
  prefer a local checkout at `tmp/elbencho-src` when present over web snippets.
- **Python**: keep each function's cognitive complexity <= 15 (not linter-enforced);
  avoid duplicating a literal string 3+ times (use a constant). `pylint` 10.00/10.
- Don't restate rules the linters already enforce; rely on `black`/`pylint`/`shellcheck`.

## Pull requests

Wrap commit-message lines at about 72 characters.

This project is currently not accepting external contributions. For maintainer
changes: keep PRs focused, ensure the checks above pass, and update
`README.md`/`docs/` when behavior or configuration changes.
