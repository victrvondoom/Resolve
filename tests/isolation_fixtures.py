"""TEST DOUBLE ONLY. Lets state-machine tests (status checks, lifecycle) reach the execution code path.

It reports production_ready=True so the manager's gate opens, but it runs NOTHING and provides no isolation of any kind.
It must never be selected by configuration; src/isolation.py has no such provider.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from isolation import IsolationProvider, ProviderReport


class TestOnlyNonIsolatedDouble(IsolationProvider):
    def report(self) -> ProviderReport:
        return ProviderReport(name="test-double", isolation_type="test-double", production_ready=True, filesystem_isolated=False,
                              network_policy_enforced=False, resource_limits_enforced=False, process_cleanup_enforced=False,
                              available=True, reason="TEST DOUBLE: runs nothing, provides no isolation")

    def run(self, sandbox_root, command, timeout_s, output_limit_bytes):
        return {"exit_code": 0, "stdout": "double", "stderr": "", "duration_seconds": 0.0, "status": "COMPLETED"}
