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

"""Regression tests for the local CI check orchestrator."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CHECK_SCRIPT = _REPO_ROOT / "utils" / "run_ci_checks.sh"
_LINT_CHECKS = ("compliance", "shellcheck", "black", "pylint")


class TestRunCiChecksShell(unittest.TestCase):
    """Exercise buffered lint execution without invoking the real tools."""

    @staticmethod
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
        path.chmod(0o755)

    def _make_repo(self, root: Path) -> tuple[Path, Path, Path]:
        repo = root / "repo"
        (repo / "utils").mkdir(parents=True)
        shutil.copy2(_CHECK_SCRIPT, repo / "utils" / _CHECK_SCRIPT.name)
        (repo / "sample.py").write_text("value = 1\n", encoding="utf-8")
        (repo / "sample.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

        fake_python = repo / "fake-python"
        self._write_executable(
            fake_python,
            f"""
            #!{sys.executable}
            import os
            import sys

            arguments = sys.argv[1:]
            if arguments == ["utils/check_license_headers.py"]:
                check = "compliance"
            elif arguments[:2] == ["-m", "black"]:
                check = "black"
            elif arguments[:2] == ["-m", "pylint"]:
                check = "pylint"
            elif arguments[:2] == ["-m", "pytest"]:
                check = "pytest"
            else:
                print(f"Unexpected fake Python arguments: {{arguments}}")
                sys.exit(99)
            print(f"{{check}} diagnostic")
            sys.exit(7 if os.environ.get("FAIL_CHECK") == check else 0)
            """,
        )
        fake_shellcheck = repo / "fake-shellcheck"
        self._write_executable(
            fake_shellcheck,
            """
            #!/usr/bin/env bash
            echo "shellcheck diagnostic"
            [[ "${FAIL_CHECK:-}" != "shellcheck" ]]
            """,
        )
        subprocess.run(
            ["git", "init", "--quiet"], cwd=repo, check=True, capture_output=True
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        return repo, fake_python, fake_shellcheck

    def _run_checks(
        self, target: str = "lint", fail_check: str = ""
    ) -> tuple[subprocess.CompletedProcess, str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, fake_python, fake_shellcheck = self._make_repo(root)
            summary = root / "summary.md"
            env = os.environ.copy()
            env.update(
                {
                    "CI_BOOTSTRAP": "0",
                    "CI_CHECK_JOBS": "4",
                    "CI_PYTHON": str(fake_python),
                    "CI_SHELLCHECK": str(fake_shellcheck),
                    "FAIL_CHECK": fail_check,
                    "GITHUB_ACTIONS": "false",
                    "GITHUB_STEP_SUMMARY": str(summary),
                }
            )
            result = subprocess.run(
                ["bash", "utils/run_ci_checks.sh", target],
                cwd=repo,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            return result, summary.read_text(encoding="utf-8")

    def test_lint_buffers_each_successful_check_and_writes_summary(self) -> None:
        result, summary = self._run_checks()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("== concurrent lint (completed,", result.stdout)
        self.assertIn("| **Concurrent lint wall time** | **success** |", summary)
        previous_position = -1
        for check in _LINT_CHECKS:
            heading_position = result.stdout.index(f"== {check} (success,")
            diagnostic_position = result.stdout.index(f"{check} diagnostic")
            self.assertGreater(heading_position, previous_position)
            self.assertGreater(diagnostic_position, heading_position)
            previous_position = diagnostic_position
            self.assertIn(f"| {check} | success |", summary)

    def test_lint_reports_all_checks_when_one_fails(self) -> None:
        for failed_check in _LINT_CHECKS:
            with self.subTest(failed_check=failed_check):
                result, summary = self._run_checks(fail_check=failed_check)

                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(f"| {failed_check} | failure |", summary)
                self.assertIn(
                    "| **Concurrent lint wall time** | **failure** |", summary
                )
                for check in _LINT_CHECKS:
                    self.assertIn(f"{check} diagnostic", result.stdout)

    def test_all_buffers_lint_and_pytest_output(self) -> None:
        result, _ = self._run_checks(target="all")

        self.assertEqual(result.returncode, 0, result.stderr)
        all_position = result.stdout.index("== all checks (completed,")
        lint_position = result.stdout.index("== concurrent lint (completed,")
        pytest_heading_position = result.stdout.index("== pytest (success,")
        pytest_output_position = result.stdout.index("pytest diagnostic")
        self.assertLess(all_position, lint_position)
        self.assertLess(lint_position, pytest_heading_position)
        self.assertLess(pytest_heading_position, pytest_output_position)

    def test_all_reports_lint_output_when_pytest_fails(self) -> None:
        result, _ = self._run_checks(target="all", fail_check="pytest")

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("== pytest (failure,", result.stdout)
        for check in _LINT_CHECKS:
            self.assertIn(f"{check} diagnostic", result.stdout)


if __name__ == "__main__":
    unittest.main()
