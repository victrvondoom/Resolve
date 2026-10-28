import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from connectors import ConnectorRegistry, ConnectorError


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_connectors.sqlite3"
        self.registry = ConnectorRegistry(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_connect_and_scoped_operations(self):
        # 1. Connect Google Drive with readonly
        drive = self.registry.connect("usr_alice", "ws_1", "google_drive", ["drive.readonly"])
        self.assertEqual(drive.status, "CONNECTED")
        
        # Read files works
        files = drive.list_authorized_resources()
        self.assertTrue(len(files) >= 1)

        # Write file fails because drive.file scope was not granted
        with self.assertRaises(ConnectorError) as ctx:
            drive.execute_authorized_operation("create_file", {"name": "test.txt", "content": "hello"})
        self.assertEqual(ctx.exception.status_code, 403)

        # 2. Re-connect granting both scopes
        drive_rw = self.registry.connect("usr_alice", "ws_1", "google_drive", ["drive.readonly", "drive.file"])
        res = drive_rw.execute_authorized_operation("create_file", {"name": "new_doc.txt", "content": "hello world"})
        self.assertEqual(res["status"], "CREATED")

    def test_crm_ambiguity_detection(self):
        crm = self.registry.connect("usr_alice", "ws_1", "crm", ["crm:read", "crm:write"])
        
        # Searching for "Alex" without stable ID or department raises 409 ambiguity error
        with self.assertRaises(ConnectorError) as ctx:
            crm.execute_authorized_operation("lookup_contact", {"name": "Alex"})
        self.assertEqual(ctx.exception.status_code, 409)

        # Disambiguating with stable ID succeeds
        res = crm.execute_authorized_operation("lookup_contact", {"name": "Alex", "id": "alex-fin-02"})
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["matches"][0]["department"], "Finance")

    def test_cross_tenant_isolation(self):
        self.registry.connect("usr_alice", "ws_alice", "github", ["repo:read"])

        # Bob attempts to access Alice's connector
        with self.assertRaises(ConnectorError) as ctx:
            self.registry.get_connector("usr_bob", "ws_bob", "github")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_disconnect_and_purge(self):
        self.registry.connect("usr_carol", "ws_carol", "crm", ["crm:read"])
        connectors = self.registry.list_connectors("usr_carol", "ws_carol")
        connected = [c for c in connectors if c["status"] == "CONNECTED"]
        self.assertEqual(len(connected), 1)

        # Disconnect
        self.registry.disconnect("usr_carol", "ws_carol", "crm")
        connectors_after = self.registry.list_connectors("usr_carol", "ws_carol")
        connected_after = [c for c in connectors_after if c["status"] == "CONNECTED"]
        self.assertEqual(len(connected_after), 0)


if __name__ == '__main__':
    unittest.main()

