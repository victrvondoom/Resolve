import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cloud_browser import CloudBrowserPool, CloudBrowserSession, CloudBrowserError


class CloudBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = CloudBrowserPool()

    def tearDown(self):
        self.pool.close_all()
        self.tmp.cleanup()

    def test_browser_session_lifecycle_and_actions(self):
        storage_path = Path(self.tmp.name) / "sbx_test"
        session = self.pool.get_or_create("sbx_test", "usr_alice", str(storage_path))
        
        # 1. Navigate to a test data URI
        html = "<html><body><h1>RESOLVE Cloud Test</h1><p>Active sandbox browser.</p></body></html>"
        res = session.navigate(f"data:text/html,{html}")
        self.assertIn("RESOLVE Cloud Test", session.extract_text())

        # 2. Capture real frame
        frame = session.capture_frame()
        self.assertTrue(frame["frame"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(frame["viewport"]["width"], 1280)
        self.assertEqual(frame["cursor"]["x"], 100)

        # 3. Click and move cursor
        click_res = session.click(350, 220)
        self.assertEqual(click_res["status"], "SUCCESS")
        self.assertEqual(session.cursor_pos, [350, 220])

        # 4. Pause and verify operations fail while paused
        session.pause()
        self.assertTrue(session.is_paused)
        with self.assertRaises(CloudBrowserError):
            session.click(10, 10)

        # 5. Resume
        session.resume()
        self.assertFalse(session.is_paused)

    def test_ssrf_metadata_blocked(self):
        storage_path = Path(self.tmp.name) / "sbx_ssrf"
        session = self.pool.get_or_create("sbx_ssrf", "usr_alice", str(storage_path))

        with self.assertRaises(CloudBrowserError) as ctx:
            session.navigate("http://169.254.169.254/latest/meta-data/")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_pool_cross_tenant_isolation(self):
        storage_path = Path(self.tmp.name) / "sbx_cross"
        self.pool.get_or_create("sbx_cross", "usr_alice", str(storage_path))

        # Bob attempts to get Alice's session
        with self.assertRaises(CloudBrowserError) as ctx:
            self.pool.get_session("sbx_cross", caller_user_id="usr_bob")
        self.assertEqual(ctx.exception.status_code, 403)


if __name__ == '__main__':
    unittest.main()

