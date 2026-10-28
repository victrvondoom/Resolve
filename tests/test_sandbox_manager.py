import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sandbox_manager import SandboxManager, SandboxState, SandboxSecurityError, SandboxError


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
        paused = self.mgr.pause_sandbox(sbx.id, caller_user_id="usr_123")
        self.assertEqual(paused.status, SandboxState.PAUSED.value)

        # Resume
        resumed = self.mgr.resume_sandbox(sbx.id, caller_user_id="usr_123")
        self.assertEqual(resumed.status, SandboxState.READY.value)

        # Terminate
        terminated = self.mgr.terminate_sandbox(sbx.id, caller_user_id="usr_123")
        self.assertEqual(terminated.status, SandboxState.TERMINATING.value)

        # Destroy
        destroyed = self.mgr.destroy_sandbox(sbx.id, caller_user_id="usr_123")
        self.assertTrue(destroyed)
        self.assertFalse(Path(sbx.storage_path).exists())

    def test_cross_tenant_access_denied(self):
        sbx = self.mgr.create_sandbox("user_alice", "ws_alice")
        
        # User Bob attempts to access Alice's sandbox
        with self.assertRaises(SandboxSecurityError):
            self.mgr.get_sandbox(sbx.id, caller_user_id="user_bob")

        with self.assertRaises(SandboxSecurityError):
            self.mgr.pause_sandbox(sbx.id, caller_user_id="user_bob")

        with self.assertRaises(SandboxSecurityError):
            self.mgr.destroy_sandbox(sbx.id, caller_user_id="user_bob")

    def test_command_execution_in_sandbox(self):
        sbx = self.mgr.create_sandbox("usr_runner", "ws_runner")
        res = self.mgr.execute_in_sandbox(
            sbx.id,
            ["python", "-c", "import os; print('SANDBOX_USER=' + os.environ.get('RESOLVE_USER_ID', ''))"],
            caller_user_id="usr_runner"
        )
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("SANDBOX_USER=usr_runner", res["stdout"])

    def test_orphan_cleanup(self):
        # Create an expired sandbox
        sbx = self.mgr.create_sandbox("usr_old", "ws_old", ttl_seconds=-10)
        self.assertTrue(Path(sbx.storage_path).exists())

        cleaned = self.mgr.cleanup_orphans()
        self.assertEqual(cleaned, 1)
        self.assertFalse(Path(sbx.storage_path).exists())


if __name__ == '__main__':
    unittest.main()

