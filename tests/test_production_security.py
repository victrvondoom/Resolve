"""Production-mode security: no shared token, no demo fallback, cross-tenant denial, headers, rate limits."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_web_api_g1 import ROOT, free_port
from server_security import ServerSecurity, ConfigError


class ProdServer:
    def __init__(self, tmp, port, extra=None):
        self.tmp, self.port, self.extra = tmp, port, extra or {}

    def start(self):
        env = dict(os.environ, RESOLVE_MODE='production', RESOLVE_ALLOWED_HOSTS=f'127.0.0.1:{self.port}',
                   RESOLVE_DB=str(Path(self.tmp) / 'r.sqlite3'), RESOLVE_PORT=str(self.port),
                   RESOLVE_FIXTURE_DIR=str(Path(self.tmp) / 'fx'), RESOLVE_TENANTS_DB=str(Path(self.tmp) / 't.sqlite3'),
                   RESOLVE_SANDBOXES_DIR=str(Path(self.tmp) / 'sb'), RESOLVE_SANDBOXES_DB=str(Path(self.tmp) / 'sb.sqlite3'),
                   RESOLVE_CONNECTORS_DB=str(Path(self.tmp) / 'c.sqlite3'), RESOLVE_DOCUMENTS_DIR=str(Path(self.tmp) / 'docs'),
                   **self.extra)
        self.proc = subprocess.Popen([sys.executable, str(ROOT / 'src' / 'resolve_app.py')], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                self.req('GET', '/api/health')
                return self
            except Exception:
                time.sleep(0.1)
        raise RuntimeError('server did not start')

    def stop(self):
        self.proc.terminate()
        self.proc.wait(10)

    def req(self, method, path, body=None, headers=None, raw=False):
        h = {'Content-Type': 'application/json'}
        h.update(headers or {})
        r = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', method=method, headers=h,
                                   data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                data = resp.read()
                return resp.status, (data if raw else json.loads(data)), resp.headers
        except urllib.error.HTTPError as e:
            data = e.read()
            return e.code, (data if raw else json.loads(data)), e.headers


class StartupPolicyTests(unittest.TestCase):
    def test_insecure_configs_rejected(self):
        with self.assertRaises(ConfigError):
            ServerSecurity(mode='production').validate_startup()          # no allowed hosts
        with self.assertRaises(ConfigError):
            ServerSecurity(mode='demo', bind_host='0.0.0.0').validate_startup()  # public bind in demo
        with self.assertRaises(ConfigError):
            ServerSecurity(mode='staging').validate_startup()
        ServerSecurity(mode='production', bind_host='0.0.0.0', allowed_hosts=frozenset({'x.example'})).validate_startup()

    def test_server_process_refuses_insecure_start(self):
        env = dict(os.environ, RESOLVE_MODE='production', RESOLVE_ALLOWED_HOSTS='')
        r = subprocess.run([sys.executable, str(ROOT / 'src' / 'resolve_app.py')], env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('refusing to start', r.stderr + r.stdout)


class ProductionApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = ProdServer(self.tmp.name, free_port()).start()

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def register(self):
        status, user, _ = self.srv.req('POST', '/api/auth/register', {'email': f'{uuid.uuid4().hex}@resolve.test', 'password': 'secure-pass-123'})
        self.assertEqual(status, 201, user)
        return {'Authorization': f"Bearer {user['token']}"}

    def test_no_shared_session_token_or_demo_fallback(self):
        self.assertEqual(self.srv.req('GET', '/api/session')[0], 404)
        for path in ('/api/auth/me', '/api/sandboxes', '/api/privacy/summary', '/api/documents', '/api/connectors'):
            self.assertEqual(self.srv.req('GET', path)[0], 401, path)
        self.assertEqual(self.srv.req('POST', '/api/documents/report', {'title': 't', 'text': 'x'})[0], 401)
        self.assertEqual(self.srv.req('GET', '/api/auth/me', headers={'Authorization': 'Bearer nonsense'})[0], 401)

    def test_legacy_global_ledger_routes_disabled(self):
        a = self.register()
        for path in ('/api/state', '/api/approvals', '/api/evidence', '/api/mission/history', '/api/stream', '/api/web/state', '/api/skills'):
            status, body, _ = self.srv.req('GET', path, headers=a)
            self.assertEqual(status, 404, (path, body))

    def test_user_b_cannot_read_user_a_documents(self):
        a, b = self.register(), self.register()
        status, made, _ = self.srv.req('POST', '/api/documents/report', {'title': 'Secret brief', 'text': 'Alice private text.'}, headers=a)
        self.assertEqual(status, 201, made)
        doc = made['document']
        # B guesses A's document id for every file operation
        status, body, _ = self.srv.req('GET', f"/api/documents/{doc['id']}/download", headers=b, raw=True)
        self.assertEqual(status, 404)
        self.assertNotIn(b'%PDF', body)
        self.assertNotIn(doc['sha256'].encode(), body)
        for op, extra in (('rotate', {'degrees': 90}), ('remove_pages', {'pages': [1]}), ('merge', {})):
            status, body, _ = self.srv.req('POST', '/api/documents/transform', {
                'operation': op, 'document_ids': [doc['id']], 'expected_sha256': {doc['id']: doc['sha256']}, **extra}, headers=b)
            self.assertIn(status, (400, 404), (op, body))
            self.assertNotIn('Secret brief', json.dumps(body))
        status, listing, _ = self.srv.req('GET', '/api/documents', headers=b)
        self.assertEqual(listing['documents'], [])
        status, exp, _ = self.srv.req('POST', '/api/privacy/export', {}, headers=b)
        self.assertNotIn(doc['id'], json.dumps(exp))
        # A can still download
        status, pdf, _ = self.srv.req('GET', f"/api/documents/{doc['id']}/download", headers=a, raw=True)
        self.assertEqual(status, 200)
        self.assertTrue(pdf.startswith(b'%PDF-'))

    def test_user_b_cannot_access_user_a_sandbox_or_frame(self):
        a, b = self.register(), self.register()
        status, sbx, _ = self.srv.req('POST', '/api/sandboxes', {}, headers=a)
        self.assertIn(status, (200, 201), sbx)
        sid = sbx.get('id') or sbx.get('sandbox', {}).get('id')
        for path in (f'/api/sandboxes/{sid}', f'/api/sandboxes/{sid}/frame'):
            status, body, _ = self.srv.req('GET', path, headers=b)
            self.assertIn(status, (403, 404), (path, body))
            self.assertNotIn('frame', body)
        self.assertEqual(self.srv.req('GET', '/api/sandboxes', headers=b)[1]['sandboxes'], [])

    def test_cloud_browser_refused_without_declared_isolation_and_no_storage_path_leak(self):
        a = self.register()
        status, sbx, _ = self.srv.req('POST', '/api/sandboxes', {}, headers=a)
        self.assertNotIn('storage_path', sbx)
        self.assertNotIn(self.tmp.name, json.dumps(sbx))
        status, body, _ = self.srv.req('GET', f"/api/sandboxes/{sbx['id']}/frame", headers=a)
        self.assertEqual(status, 503, body)
        self.assertIn('isolated', body['error'])
        self.assertEqual(self.srv.req('POST', '/api/cloud-browser/navigate', {'sandbox_id': sbx['id'], 'url': 'https://example.com'}, headers=a)[0], 503)

    def test_security_headers_and_host_check(self):
        status, _, h = self.srv.req('GET', '/api/health')
        self.assertEqual(h['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(h['X-Frame-Options'], 'DENY')
        self.assertIn("frame-ancestors 'none'", h['Content-Security-Policy'])
        status, body, _ = self.srv.req('GET', '/api/documents', headers={'Host': 'evil.example'})
        self.assertEqual(status, 403)
        self.assertEqual(self.srv.req('GET', '/api/health', headers={'Host': 'probe.internal'})[0], 200)  # platform health probe

    def test_logout_revokes_session(self):
        a = self.register()
        self.assertEqual(self.srv.req('GET', '/api/auth/me', headers=a)[0], 200)
        self.assertEqual(self.srv.req('POST', '/api/auth/logout', {}, headers=a)[0], 200)
        self.assertEqual(self.srv.req('GET', '/api/auth/me', headers=a)[0], 401)


class RateLimitTests(unittest.TestCase):
    def test_auth_brute_force_is_throttled(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = ProdServer(tmp, free_port(), {'RESOLVE_AUTH_RATE_PER_MIN': '5'}).start()
            try:
                codes = [srv.req('POST', '/api/auth/login', {'email': 'x@resolve.test', 'password': 'wrong-password'})[0] for _ in range(8)]
                self.assertIn(429, codes)
                self.assertNotIn(429, codes[:5])
            finally:
                srv.stop()


if __name__ == '__main__':
    unittest.main()
