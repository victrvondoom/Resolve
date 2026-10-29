import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from tenant_auth import TenantAuthManager
from sandbox_manager import SandboxManager
from connectors import ConnectorRegistry
from privacy_engine import PrivacyEngine


class PrivacyEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.auth = TenantAuthManager(base / "tenants.sqlite3")
        self.sandbox = SandboxManager(base_dir=base / "sbx", db_path=base / "sbx.sqlite3")
        self.connectors = ConnectorRegistry(base / "conn.sqlite3")
        self.engine = PrivacyEngine(self.auth, self.sandbox, self.connectors, data_dir=base)

    def tearDown(self):
        self.tmp.cleanup()

    def test_summary_and_export(self):
        user = self.auth.register("dan@test.com", "passdan-long-1")
        uid = user["user_id"]
        wid = user["workspace_id"]

        # Create sandbox and connect app
        sbx = self.sandbox.create_sandbox(uid, wid)
        self.connectors.connect(uid, wid, "google_drive", ["drive.readonly"])

        # Check summary
        summary = self.engine.get_summary(uid, wid)
        self.assertEqual(summary["active_sandboxes"], 1)
        self.assertEqual(summary["connected_apps_count"], 1)

        # Export
        bundle = self.engine.export_data(uid, wid)
        self.assertEqual(bundle["user_id"], uid)
        self.assertTrue(len(bundle["sha256_checksum"]) == 64)
        self.assertEqual(len(bundle["connected_integrations"]), 3)

    def test_delete_sandbox_flow(self):
        user = self.auth.register("frank@test.com", "passfrank-long-1")
        uid = user["user_id"]
        wid = user["workspace_id"]

        sbx = self.sandbox.create_sandbox(uid, wid)
        receipt = self.engine.delete_sandbox(uid, sbx.id)
        self.assertEqual(receipt.status, "COMPLETED")
        self.assertEqual(receipt.target_id, sbx.id)
        self.assertTrue(len(receipt.sha256_proof) == 64)

    def test_delete_account_complete_purge(self):
        user = self.auth.register("grace@test.com", "passgrace-long-1")
        uid = user["user_id"]
        wid = user["workspace_id"]

        sbx = self.sandbox.create_sandbox(uid, wid)
        self.connectors.connect(uid, wid, "github", ["repo:read"])

        # Permanently delete account
        receipt = self.engine.delete_account(uid)
        self.assertEqual(receipt.status, "COMPLETED")
        self.assertIn(sbx.id, receipt.purged_items["destroyed_sandboxes"])
        self.assertEqual(receipt.purged_items["purged_connectors_count"], 1)

        # User and sessions should no longer exist
        with self.assertRaises(Exception):
            self.auth.validate_session(user["token"])


if __name__ == '__main__':
    unittest.main()

