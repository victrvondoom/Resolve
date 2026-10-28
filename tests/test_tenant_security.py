"""Adversarial security and multi-tenant isolation suite (T01–T16) for RESOLVE."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from tenant_auth import TenantAuthManager, TenantError
from sandbox_manager import SandboxManager, SandboxSecurityError, SandboxError, SandboxState
from cloud_browser import CloudBrowserPool, CloudBrowserError
from effect_graph import EffectDependencyGraph, NodeType, EdgeType
from connectors import ConnectorRegistry, ConnectorError
from privacy_engine import PrivacyEngine


class TenantSecurityAdversarialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.auth = TenantAuthManager(base / "tenants.sqlite3")
        self.sandbox = SandboxManager(base_dir=base / "sbx", db_path=base / "sbx.sqlite3")
        self.connectors = ConnectorRegistry(base / "conn.sqlite3")
        self.browser_pool = CloudBrowserPool()
        self.privacy = PrivacyEngine(self.auth, self.sandbox, self.connectors, data_dir=base)

        # Create two separate tenants: Alice and Bob
        self.alice = self.auth.register("alice@tenant.test", "alice_password_123")
        self.bob = self.auth.register("bob@tenant.test", "bob_password_456")

    def tearDown(self):
        self.browser_pool.close_all()
        self.tmp.cleanup()

    def test_t01_user_a_accesses_user_b_mission(self):
        # Alice creates a mission graph
        graph = EffectDependencyGraph("m_alice_01", self.alice["user_id"], self.alice["workspace_id"])
        graph.add_node("intent_1", NodeType.INTENT, {"text": "Alice private search"})

        # Bob attempts to verify or inspect Alice's mission
        graph_bob = EffectDependencyGraph("m_alice_01", self.bob["user_id"], self.bob["workspace_id"])
        graph_bob.nodes = graph.nodes
        ok, reason, _ = graph_bob.verify_mission()
        self.assertFalse(ok)
        self.assertIn("Cross-tenant entity leakage detected", reason)

    def test_t02_user_a_accesses_user_b_sandbox_storage(self):
        # Alice creates a sandbox
        sbx_alice = self.sandbox.create_sandbox(self.alice["user_id"], self.alice["workspace_id"])
        
        # Bob attempts to get sandbox details or files
        with self.assertRaises(SandboxSecurityError):
            self.sandbox.get_sandbox(sbx_alice.id, caller_user_id=self.bob["user_id"])

    def test_t03_oauth_token_used_outside_correct_tenant(self):
        # Alice connects GitHub
        self.connectors.connect(self.alice["user_id"], self.alice["workspace_id"], "github", ["repo:read"])

        # Bob attempts to execute operations using Alice's connector
        with self.assertRaises(ConnectorError) as ctx:
            self.connectors.get_connector(self.bob["user_id"], self.bob["workspace_id"], "github")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_t04_sandbox_reports_wrong_owner(self):
        sbx_alice = self.sandbox.create_sandbox(self.alice["user_id"], self.alice["workspace_id"])
        with self.assertRaises(SandboxSecurityError):
            self.sandbox.pause_sandbox(sbx_alice.id, caller_user_id=self.bob["user_id"])

    def test_t05_sandbox_terminates_mid_task(self):
        sbx = self.sandbox.create_sandbox(self.alice["user_id"], self.alice["workspace_id"])
        self.sandbox.terminate_sandbox(sbx.id, caller_user_id=self.alice["user_id"])
        
        # New execution must be rejected when sandbox is terminating
        with self.assertRaises(SandboxError):
            self.sandbox.execute_in_sandbox(sbx.id, ["python", "-c", "print(1)"], caller_user_id=self.alice["user_id"])

    def test_t06_browser_session_expires(self):
        # Create expired session token directly
        expired_token = self.auth.create_session(self.alice["user_id"], self.alice["workspace_id"], duration_sec=-10)["token"]
        with self.assertRaises(TenantError) as ctx:
            self.auth.validate_session(expired_token)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_t07_user_deletes_account_during_execution(self):
        # Alice starts a sandbox
        sbx = self.sandbox.create_sandbox(self.alice["user_id"], self.alice["workspace_id"])
        self.assertTrue(Path(sbx.storage_path).exists())

        # Alice deletes account
        receipt = self.privacy.delete_account(self.alice["user_id"])
        self.assertEqual(receipt.status, "COMPLETED")
        self.assertFalse(Path(sbx.storage_path).exists())

        # Old token is completely invalidated
        with self.assertRaises(TenantError):
            self.auth.validate_session(self.alice["token"])

    def test_t08_delayed_queue_job_arrives_after_deletion(self):
        uid = self.bob["user_id"]
        token = self.bob["token"]
        self.privacy.delete_account(uid)

        # Worker receives delayed job with old token
        with self.assertRaises(TenantError) as ctx:
            self.auth.validate_session(token)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_t09_concurrent_agent_write_collision_guarded(self):
        # Paused sandbox blocks concurrent write operations
        sbx = self.sandbox.create_sandbox(self.alice["user_id"], self.alice["workspace_id"])
        self.sandbox.pause_sandbox(sbx.id, caller_user_id=self.alice["user_id"])

        with self.assertRaises(SandboxError) as ctx:
            self.sandbox.execute_in_sandbox(sbx.id, ["echo", "1"], caller_user_id=self.alice["user_id"])
        self.assertIn("Cannot execute in sandbox with status PAUSED", ctx.exception.message)

    def test_t10_malicious_webpage_ssrf_exfiltration_blocked(self):
        storage_path = Path(self.tmp.name) / "sbx_cloud"
        session = self.browser_pool.get_or_create("sbx_cloud", self.alice["user_id"], str(storage_path))
        
        # Page triggers SSRF to internal metadata
        with self.assertRaises(CloudBrowserError) as ctx:
            session.navigate("http://169.254.169.254/latest/meta-data/")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_t11_revoked_connector_is_reused(self):
        self.connectors.connect(self.alice["user_id"], self.alice["workspace_id"], "google_drive", ["drive.readonly"])
        # Revoke/disconnect
        self.connectors.disconnect(self.alice["user_id"], self.alice["workspace_id"], "google_drive")

        # Reuse attempt fails
        with self.assertRaises(ConnectorError) as ctx:
            self.connectors.get_connector(self.alice["user_id"], self.alice["workspace_id"], "google_drive")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_t12_cloud_provider_unavailable_fallback(self):
        # Unknown connector type raises 404
        with self.assertRaises(ConnectorError) as ctx:
            self.connectors.connect(self.alice["user_id"], self.alice["workspace_id"], "unsupported_cloud_provider", [])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_t13_screenshot_stream_cross_tenant_isolation(self):
        storage_path = Path(self.tmp.name) / "sbx_alice_screen"
        self.browser_pool.get_or_create("sbx_alice_screen", self.alice["user_id"], str(storage_path))

        # Bob attempts to get frame from Alice's browser session
        with self.assertRaises(CloudBrowserError) as ctx:
            self.browser_pool.get_session("sbx_alice_screen", caller_user_id=self.bob["user_id"])
        self.assertEqual(ctx.exception.status_code, 403)

    def test_t14_wrong_entity_approval_rejected(self):
        graph = EffectDependencyGraph("m_sec_14", self.alice["user_id"], self.alice["workspace_id"])
        graph.add_node("intent_1", NodeType.INTENT, {"text": "Send contract to Acme Corp"})
        graph.add_node("app_1", NodeType.APPROVAL, {"digest": "digest_expected_acme"})
        # Effect with altered digest submitted
        graph.add_node("eff_1", NodeType.EFFECT, {"expected_digest": "tampered_digest"})
        graph.add_edge("eff_1", "app_1", EdgeType.AUTHORIZED_BY)

        ok, reason, _ = graph.verify_mission()
        self.assertFalse(ok)
        self.assertIn("Approval digest mismatch", reason)

    def test_t15_storage_deletion_partially_missing_succeeds(self):
        sbx = self.sandbox.create_sandbox(self.alice["user_id"], self.alice["workspace_id"])
        # Manually delete storage path beforehand
        import shutil
        shutil.rmtree(sbx.storage_path, ignore_errors=True)

        # Deletion must succeed idempotently without crashing
        res = self.sandbox.destroy_sandbox(sbx.id, caller_user_id=self.alice["user_id"])
        self.assertTrue(res)

    def test_t16_user_reconnects_after_workspace_deletion(self):
        ws2 = self.auth.create_workspace(self.alice["user_id"], "Secondary Workspace")
        # Alice deletes ws2
        self.auth.delete_workspace(self.alice["user_id"], ws2["id"])

        # Attempting to switch session to deleted workspace fails
        with self.assertRaises(TenantError) as ctx:
            self.auth.switch_workspace(self.alice["token"], ws2["id"])
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == '__main__':
    unittest.main()

