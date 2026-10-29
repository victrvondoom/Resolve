import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from tenant_auth import TenantAuthManager, TenantError


class TenantAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_tenants.sqlite3"
        self.auth = TenantAuthManager(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_and_login_flow(self):
        session = self.auth.register("alice@example.com", "password123", "Alice")
        self.assertTrue(session["token"].startswith("rst_"))
        self.assertEqual(session["email"], "alice@example.com")
        self.assertEqual(session["display_name"], "Alice")
        self.assertTrue(session["workspace_id"].startswith("ws_"))

        # Validate session
        valid = self.auth.validate_session(session["token"])
        self.assertEqual(valid["user_id"], session["user_id"])
        self.assertEqual(valid["workspace_id"], session["workspace_id"])

        # Login again
        login_session = self.auth.login("alice@example.com", "password123")
        self.assertEqual(login_session["user_id"], session["user_id"])
        self.assertEqual(login_session["workspace_id"], session["workspace_id"])

    def test_duplicate_registration_rejected(self):
        self.auth.register("bob@example.com", "secretpass")
        with self.assertRaises(TenantError) as ctx:
            self.auth.register("bob@example.com", "anotherpass")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_invalid_login_rejected(self):
        self.auth.register("charlie@example.com", "correctpass")
        with self.assertRaises(TenantError) as ctx:
            self.auth.login("charlie@example.com", "wrongpass")
        self.assertEqual(ctx.exception.status_code, 401)

        with self.assertRaises(TenantError) as ctx:
            self.auth.login("nobody@example.com", "pass")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_cross_tenant_workspace_isolation(self):
        user_a = self.auth.register("user_a@test.com", "pass_a_long_enough")
        user_b = self.auth.register("user_b@test.com", "pass_b_long_enough")

        # User A attempts to switch to User B's workspace
        with self.assertRaises(TenantError) as ctx:
            self.auth.switch_workspace(user_a["token"], user_b["workspace_id"])
        self.assertEqual(ctx.exception.status_code, 403)

    def test_workspace_management(self):
        user = self.auth.register("dave@test.com", "passdave-long-1")
        ws2 = self.auth.create_workspace(user["user_id"], "Project Alpha")
        self.assertTrue(ws2["id"].startswith("ws_"))

        workspaces = self.auth.list_workspaces(user["user_id"])
        self.assertEqual(len(workspaces), 2)

        # Switch to ws2
        switched = self.auth.switch_workspace(user["token"], ws2["id"])
        self.assertEqual(switched["workspace_id"], ws2["id"])

        # Delete ws2
        self.auth.delete_workspace(user["user_id"], ws2["id"])
        workspaces_after = self.auth.list_workspaces(user["user_id"])
        self.assertEqual(len(workspaces_after), 1)

        # Attempt to delete only workspace fails
        with self.assertRaises(TenantError) as ctx:
            self.auth.delete_workspace(user["user_id"], workspaces_after[0]["id"])
        self.assertEqual(ctx.exception.status_code, 400)

    def test_user_deletion_cascades(self):
        user = self.auth.register("eve@test.com", "passeve-long-1")
        token = user["token"]
        user_id = user["user_id"]

        self.auth.delete_user(user_id)
        # Token validation must fail
        with self.assertRaises(TenantError) as ctx:
            self.auth.validate_session(token)
        self.assertEqual(ctx.exception.status_code, 401)


if __name__ == '__main__':
    unittest.main()

