"""DevOps adapter for repository inspection and scoped test execution.

Operates locally within the repository boundary; enforces workspace containment
and records test execution results into the ledger.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from resolve_core import Action, Ledger, now, uid

ROOT = Path(__file__).resolve().parents[1]


class DevOpsError(RuntimeError):
    """DevOps operation violated boundaries or policies."""


class DevOpsAdapter:
    def __init__(self, repo_root: Path | str = ROOT, ledger: Ledger | None = None):
        self.repo_root = Path(repo_root).resolve()
        self.ledger = ledger

    def _contain(self, path: Path | str) -> Path:
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.repo_root):
            raise DevOpsError(f"path {resolved} escapes permitted repository root {self.repo_root}")
        return resolved

    def inspect_repo(self) -> dict[str, Any]:
        """Inspect repository status (branch, head SHA, modified files)."""
        try:
            branch = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.repo_root,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            head_sha = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=self.repo_root,
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            status_out = subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=self.repo_root,
                text=True,
                stderr=subprocess.DEVNULL,
            )
            dirty_count = len([line for line in status_out.splitlines() if line.strip()])
        except Exception as exc:
            raise DevOpsError(f"git inspection failed: {exc}") from exc

        return {
            "status": "OBSERVED",
            "repo_root": str(self.repo_root),
            "branch": branch,
            "head_sha": head_sha,
            "modified_files": dirty_count,
            "timestamp": now(),
        }

    def run_tests(
        self,
        test_module_or_file: str,
        mission_id: str = "mission_devops",
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """Run a scoped test file or module using python -m unittest."""
        # Sanitize target to ensure it is a safe test module under tests/
        target = test_module_or_file.strip()
        if target.startswith("tests/") or target.startswith("tests\\"):
            test_file = self._contain(self.repo_root / target)
            if not test_file.is_file():
                raise DevOpsError(f"test file does not exist: {target}")
            module_name = target.replace("/", ".").replace("\\", ".").removesuffix(".py")
        elif target.startswith("tests."):
            module_name = target
        else:
            raise DevOpsError(f"test target must be within tests package: {target}")

        action_id = uid("act_devops")
        if self.ledger:
            action = Action(
                action_id,
                mission_id,
                1,
                "devops_run_tests",
                f"module:{module_name}",
                "DevOpsRunner",
                sys.executable,
                None,
                {"target": module_name},
                None,
                {"expected_exit": 0},
                "READ_ONLY",
                "low",
            )
            self.ledger.create_action(action)
            self.ledger.transition(action_id, "BOUND")
            self.ledger.transition(action_id, "AUTHORIZED")
            self.ledger.transition(action_id, "READY")
            self.ledger.transition(action_id, "DISPATCHING")

        start = time.monotonic()
        env = os.environ.copy()
        env["PYTHONPATH"] = f"{self.repo_root / 'src'}{os.pathsep}{self.repo_root}"
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "unittest", module_name, "-v"],
                cwd=self.repo_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            elapsed = time.monotonic() - start
            output = proc.stderr + proc.stdout
            exit_code = proc.returncode

            # Parse unittest summary
            ran_match = re.search(r"Ran (\d+) tests? in ([\d\.]+)s", output)
            total_ran = int(ran_match.group(1)) if ran_match else 0
            is_ok = "OK" in output and exit_code == 0
            state = "CONFIRMED" if is_ok else "FAILED"

            res = {
                "action_id": action_id,
                "status": state,
                "target": module_name,
                "exit_code": exit_code,
                "tests_run": total_ran,
                "duration_seconds": round(elapsed, 3),
                "output_snippet": output[-500:],
                "verified": is_ok,
            }

            if self.ledger:
                self.ledger.transition(action_id, "DISPATCHED")
                self.ledger.transition(action_id, "OBSERVING")
                self.ledger.transition(
                    action_id,
                    state,
                    {"verification": res, "verification_status": state},
                )
            return res

        except subprocess.TimeoutExpired as exc:
            if self.ledger:
                self.ledger.transition(
                    action_id,
                    "FAILED",
                    {"error": f"test run timed out after {timeout}s"},
                )
            raise DevOpsError(f"test run timed out after {timeout}s") from exc
        except Exception as exc:
            if self.ledger:
                self.ledger.transition(
                    action_id,
                    "FAILED",
                    {"error": str(exc)},
                )
            raise
