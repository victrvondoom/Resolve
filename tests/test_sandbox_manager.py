import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sandbox_manager import SandboxManager, SandboxState, SandboxError
from isolation import ExecutionBlocked
from isolation_fixtures import TestOnlyNonIsolatedDouble


class SandboxManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tmp.name) / "sandboxes"
        self.db_path = Path(self.tmp.name) / "test_sandboxes.sqlite3"
        self.mgr = SandboxManager(base_dir=self.base_dir, db_path=self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_lifecycle(self):
        sbx = self.mgr.create_sandbox("usr_123", "ws_abc", mission_id="m_1")
        self.assertTrue(sbx.id.startswith("sbx_"))
        self.assertEqual(sbx.status, SandboxState.READY.value)
        self.assertTrue(Path(sbx.storage_path).exists())
        self.assertTrue((Path(sbx.storage_path) / "workspace").exists())

        # Pause
        paused = self.mgr.pause_sandbox(sbx.id, caller_user_id="usr_123", caller_workspace_id="ws_abc")
        self.assertEqual(paused.status, SandboxState.PAUSED.value)

        # Resume
        resumed = self.mgr.resume_sandbox(sbx.id, caller_user_id="usr_123", caller_workspace_id="ws_abc")
        self.assertEqual(resumed.status, SandboxState.READY.value)

        # Terminate
        terminated = self.mgr.terminate_sandbox(sbx.id, caller_user_id="usr_123", caller_workspace_id="ws_abc")
        self.assertEqual(terminated.status, SandboxState.TERMINATING.value)

        # Destroy
        destroyed = self.mgr.destroy_sandbox(sbx.id, caller_user_id="usr_123", caller_workspace_id="ws_abc")
        self.assertTrue(destroyed)
        self.assertFalse(Path(sbx.storage_path).exists())

    def test_cross_tenant_access_denied(self):
        sbx = self.mgr.create_sandbox("user_alice", "ws_alice")
        
        # User Bob attempts to access Alice's sandbox
        # Cross-tenant lookups are indistinguishable from a missing sandbox (no existence leak)
        for call in (lambda: self.mgr.get_sandbox(sbx.id, caller_user_id="user_bob", caller_workspace_id="ws_bob"),
                     lambda: self.mgr.pause_sandbox(sbx.id, caller_user_id="user_bob", caller_workspace_id="ws_bob"),
                     lambda: self.mgr.destroy_sandbox(sbx.id, caller_user_id="user_bob", caller_workspace_id="ws_bob")):
            with self.assertRaises(SandboxError) as ctx:
                call()
            self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.mgr.get_sandbox(sbx.id, caller_user_id="user_alice", caller_workspace_id="ws_alice").status, "READY")

    def test_execution_refused_without_isolated_provider_and_no_process_started(self):
        """Production default: no provider. The command must not run, even one that would leave a marker on the host."""
        sbx = self.mgr.create_sandbox("usr_runner", "ws_runner")
        marker = Path(sbx.storage_path) / "workspace" / "marker.txt"
        with self.assertRaises(ExecutionBlocked) as ctx:
            self.mgr.execute_in_sandbox(sbx.id, ["python", "-c", "open('marker.txt','w').write('ran')"],
                                        caller_user_id="usr_runner", caller_workspace_id="ws_runner")
        self.assertIn("Commands are not run on the application host", ctx.exception.reason)
        self.assertFalse(marker.exists(), "a refused command must not run")
        self.assertEqual(self.mgr.get_sandbox(sbx.id, caller_user_id="usr_runner", caller_workspace_id="ws_runner").status, "READY")
        self.assertFalse(self.mgr.provider_report()["production_ready"])

    def test_execution_path_with_test_double_reaches_state_checks(self):
        """Exercises the lifecycle logic with a double that runs nothing; it is not evidence of isolation."""
        mgr = SandboxManager(base_dir=self.base_dir, db_path=self.db_path, provider=TestOnlyNonIsolatedDouble())
        sbx = mgr.create_sandbox("usr_runner", "ws_runner")
        res = mgr.execute_in_sandbox(sbx.id, ["echo", "x"], caller_user_id="usr_runner", caller_workspace_id="ws_runner")
        self.assertEqual(res["status"], "COMPLETED")
        self.assertEqual(mgr.get_sandbox(sbx.id, caller_user_id="usr_runner", caller_workspace_id="ws_runner").status, "READY")

    def test_orphan_cleanup(self):
        # Create an expired sandbox
        sbx = self.mgr.create_sandbox("usr_old", "ws_old", ttl_seconds=-10)
        self.assertTrue(Path(sbx.storage_path).exists())

        cleaned = self.mgr.cleanup_orphans()
        self.assertEqual(cleaned, 1)
        self.assertFalse(Path(sbx.storage_path).exists())


if __name__ == '__main__':
    unittest.main()

