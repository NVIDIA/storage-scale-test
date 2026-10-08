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

# Versions

Releases are annotated Git tags named `vMAJOR.MINOR.PATCH`, optionally with a
[SemVer](https://semver.org/) prerelease such as `v1.2.3-rc.1`. Changes between
releases are in [CHANGELOG.md](../CHANGELOG.md).

## Reading a version

| Version | Meaning |
| --- | --- |
| `v1.2.3` | Exactly the `v1.2.3` release. |
| `v1.2.3-4-g0123456789ab` | Four commits after `v1.2.3`, at commit `0123456789ab`. |
| `untagged-42-g0123456789ab` | Commit `0123456789ab`; no release tag is in its history. |
| `shallow-g0123456789ab` | A shallow clone whose commit has no release tag. |
| `unknown` | Neither Git metadata nor a `VERSION` file was found. |

A `-modified` suffix means the source differs from that commit or release.
In a Git checkout, that is any change `git status` reports; ignored files such
as `env.sh`, `results/`, and downloaded binaries don't count. Git 2.15 or newer
is required.

A deployment tarball (`utils/build_tarball.sh`) or release source archive has
no Git metadata. It carries a `VERSION` file and a `SOURCE_SHA256` list of its
files, and becomes `-modified` when a listed file changes. You can check one
yourself with `sha256sum -c SOURCE_SHA256`. Files a deployment is expected to
add or replace are not listed: `env.sh`, credentials, and benchmark binaries.

`lib/project_version.sh` implements all of this; Python commands call it.

## `--version`

These commands print the version and exit when given `--version`:
`validate_env.sh`, the `storage-tests/*/nv-*.sh` launchers,
`utils/build_tarball.sh`, the `utils/extract-*` and `utils/summarize-elbencho`
reporters, `utils/reconstruct_elbencho_env_used.py`, and the `utils/build/` and
`utils/slurm/` helpers. A built `s3test` binary reports the version it was built
from.

Launcher and reporter `--help` paths work before configuration or credentials
are loaded. Reporter help and version queries also work without installing the
reporting dependencies or creating a virtual environment.

## Results and reports

Every benchmark result file gets a `<name>.out.project-version` file holding
the version of the code that ran it. The launcher resolves its version once and
passes it to SSH workers and the Kubernetes coordinator; a Slurm job resolves
its own when it starts, because the checkout can change while the job waits.
Each run log also starts with the version. A resumed run keeps the versions of
results that already succeeded; results it runs now get the current version.

Reports start with the versions that produced the results they include and the
version of the reporter:

```text
Produced by storage-scale-test: v1.2.3, v1.3.0
Reported by storage-scale-test: v1.4.0
```

Several produced-by versions mean the results came from different code.
`unknown` covers results recorded before versioning, and metrics loaded from
reporter CSV files or Warp analyzed JSON, which carry no versions.

## Releasing

1. Move the `Unreleased` notes in `CHANGELOG.md` under a new
   `## [1.2.3] - YYYY-MM-DD` heading and merge that to `main`.
2. Tag the merged commit and push the tag:

   ```bash
   git tag -a v1.2.3 -m v1.2.3
   git push origin v1.2.3
   ```

The release workflow then runs the source checks, builds
`storage-scale-test-v1.2.3-source.tar.gz` and its `.sha256` from the tag with
`git archive`, checks that the archive verifies and that its commands report
`v1.2.3`, and publishes a GitHub release whose notes are the changelog section.
Prerelease tags make prereleases. The workflow refuses tags that are not on
`main` or have no changelog section. A rerun keeps any assets already published
and refuses to replace them with different content. It deletes and re-uploads
only an asset GitHub left in the `starter` state after a failed upload.

To build and check an archive locally, without publishing:

```bash
python3 utils/build_source_release.py --tag v1.2.3 --smoke
```

GitHub's automatic "Source code" downloads have no `VERSION` file and report
`unknown`; use the attached archive. Anyone who can push a `v*` tag can start a
release, so restrict tag creation with a repository ruleset.
