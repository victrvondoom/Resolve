import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import http.server
import threading
from cloud_browser import CloudBrowserPool, CloudBrowserSession, CloudBrowserError
from net_policy import NetPolicy, PolicyViolation


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/redirect-meta':
            self.send_response(302)
            self.send_header('Location', 'http://169.254.169.254/latest/meta-data/')
            self.end_headers()
            return
        body = b"<html><body><h1>RESOLVE Cloud Test</h1><p>Active sandbox browser.</p></body></html>"
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class PolicyUnitTests(unittest.TestCase):
    def test_scheme_and_address_rejections(self):
        p = NetPolicy()
        for bad in ("file:///etc/passwd", "data:text/html,x", "javascript:alert(1)", "ftp://example.com/",
                    "http://127.0.0.1/", "http://[::1]/", "http://10.0.0.5/", "http://192.168.1.1/",
                    "http://169.254.169.254/", "http://metadata.google.internal/", "http://localhost:8765/",
                    "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/"):
            with self.subTest(url=bad), self.assertRaises(PolicyViolation):
                p.check_url(bad)

    def test_dns_name_resolving_to_private_address_is_rejected(self):
        fake = lambda host, port, type=None: [(2, 1, 6, '', ('10.1.2.3', port))]
        with self.assertRaises(PolicyViolation):
            NetPolicy().check_url("http://innocent.example/", resolver=fake)
        mixed = lambda host, port, type=None: [(2, 1, 6, '', ('93.184.216.34', port)), (2, 1, 6, '', ('127.0.0.1', port))]
        with self.assertRaises(PolicyViolation):
            NetPolicy().check_url("http://rebind.example/", resolver=mixed)
        public = lambda host, port, type=None: [(2, 1, 6, '', ('93.184.216.34', port))]
        NetPolicy().check_url("https://example.com/", resolver=public)



class CloudBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = CloudBrowserPool(NetPolicy(allow_loopback=True))
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.pool.close_all()
        self.httpd.shutdown()
        self.tmp.cleanup()

    def test_browser_session_lifecycle_and_actions(self):
        storage_path = Path(self.tmp.name) / "sbx_test"
        session = self.pool.get_or_create("sbx_test", "usr_alice", str(storage_path), "ws_alice")
        
        # 1. Navigate to a test data URI
        res = session.navigate(self.base + "/")
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
        session = self.pool.get_or_create("sbx_ssrf", "usr_alice", str(storage_path), "ws_alice")

        with self.assertRaises(CloudBrowserError) as ctx:
            session.navigate("http://169.254.169.254/latest/meta-data/")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_file_and_data_urls_rejected(self):
        session = self.pool.get_or_create("sbx_scheme", "usr_alice", str(Path(self.tmp.name) / "sbx_scheme"), "ws_alice")
        for url in ("file:///etc/passwd", "data:text/html,<h1>x</h1>"):
            with self.subTest(url=url), self.assertRaises(CloudBrowserError):
                session.navigate(url)

    def test_redirect_to_metadata_blocked_at_request_level(self):
        session = self.pool.get_or_create("sbx_redir", "usr_alice", str(Path(self.tmp.name) / "sbx_redir"), "ws_alice")
        with self.assertRaises(Exception):
            session.navigate(self.base + "/redirect-meta")
        self.assertTrue(any("169.254.169.254" in b["reason"] for b in session.blocked_requests))

    def test_default_policy_blocks_loopback_server(self):
        strict = CloudBrowserPool()
        try:
            s = strict.get_or_create("sbx_strict", "usr_alice", str(Path(self.tmp.name) / "sbx_strict"), "ws_alice")
            with self.assertRaises(CloudBrowserError) as ctx:
                s.navigate(self.base + "/")
            self.assertEqual(ctx.exception.status_code, 403)
        finally:
            strict.close_all()

    def test_pool_cross_tenant_isolation(self):
        storage_path = Path(self.tmp.name) / "sbx_cross"
        self.pool.get_or_create("sbx_cross", "usr_alice", str(storage_path), "ws_alice")

        # Bob attempts to get Alice's session
        with self.assertRaises(CloudBrowserError) as ctx:
            self.pool.get_session("sbx_cross", "usr_bob", "ws_bob")
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == '__main__':
    unittest.main()

