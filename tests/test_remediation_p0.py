"""P0 regression tests: host command execution refused, sandbox and browser access bound to user AND workspace,
unauthorised destroy has no side effects. Production-mode HTTP server plus direct unit checks on the pool."""
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_production_security import ProdServer
from test_web_api_g1 import free_port
from cloud_browser import CloudBrowserError, CloudBrowserPool
from isolation import ExecutionBlocked, select_provider, UnavailableProvider


class ProductionSandboxIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = ProdServer(cls.tmp.name, free_port()).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop(); cls.tmp.cleanup()

    def user(self):
        _, u, _ = self.srv.req('POST', '/api/auth/register', {'email': f'{uuid.uuid4().hex}@resolve.test', 'password': 'secure-pass-123'})
        return {'Authorization': f"Bearer {u['token']}"}, u

    def sandbox(self, h):
        s, body, _ = self.srv.req('POST', '/api/sandboxes', {}, headers=h)
        self.assertEqual(s, 201, body)
        return body

    # ---- TASK 002/003/008: host execution refused, and no process started
    def test_host_command_refused_in_production_and_no_side_effect(self):
        h, _ = self.user()
        sbx = self.sandbox(h)
        marker = Path(self.tmp.name) / 'sb' / 'marker_should_not_exist.txt'
        cmd = ['python3', '-c', f"open({str(marker)!r}, 'w').write('ran')"]
        s, body, _ = self.srv.req('POST', f"/api/sandboxes/{sbx['id']}/action", {'command': cmd}, headers=h)
        self.assertEqual(s, 503, body)
        self.assertEqual(body.get('status'), 'EXECUTION_BLOCKED')
        self.assertNotIn('stdout', body)
        self.assertFalse(marker.exists())
        self.assertFalse(any(p.name == 'marker_should_not_exist.txt' for p in Path(self.tmp.name).rglob('*')), 'no process may create files')
        # the sandbox is still usable: refusal did not move it into BUSY or any other state
        s, after, _ = self.srv.req('GET', f"/api/sandboxes/{sbx['id']}", headers=h)
        self.assertEqual(after['status'], 'READY')

    def test_refusal_is_the_same_for_every_command_shape(self):
        h, _ = self.user()
        sbx = self.sandbox(h)
        for cmd in (['echo', 'x'], ['sh', '-c', 'id'], ['/bin/true']):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.srv.req('POST', f"/api/sandboxes/{sbx['id']}/action", {'command': cmd}, headers=h)[0], 503)

    def test_sandbox_routes_cannot_reach_host_execution_by_other_paths(self):
        # the only route that executes commands is /action; pause/resume/destroy never run anything
        h, _ = self.user()
        sbx = self.sandbox(h)
        self.assertEqual(self.srv.req('POST', f"/api/sandboxes/{sbx['id']}/pause", {}, headers=h)[0], 200)
        self.assertEqual(self.srv.req('POST', f"/api/sandboxes/{sbx['id']}/action", {'command': ['echo', 'x']}, headers=h)[0], 503)

    def test_unauthenticated_action_is_refused(self):
        h, _ = self.user()
        sbx = self.sandbox(h)
        self.assertEqual(self.srv.req('POST', f"/api/sandboxes/{sbx['id']}/action", {'command': ['echo', 'x']})[0], 401)

    # ---- TASK 011–014/021/022: user AND workspace isolation on sandbox routes
    def test_same_user_other_workspace_cannot_see_or_control_sandbox(self):
        h, u = self.user()
        home_ws = u['workspace_id']
        sbx = self.sandbox(h)
        ws2 = self.srv.req('POST', '/api/workspaces/create', {'name': 'Second'}, headers=h)[1]
        # switching mutates this session in place (same token, new workspace): so the token now acts in ws2
        self.assertEqual(self.srv.req('POST', '/api/workspaces/switch', {'workspace_id': ws2['id']}, headers=h)[0], 200)
        self.assertEqual(self.srv.req('GET', f"/api/sandboxes/{sbx['id']}", headers=h)[0], 404)
        ids = [x['id'] for x in self.srv.req('GET', '/api/sandboxes', headers=h)[1]['sandboxes']]
        self.assertNotIn(sbx['id'], ids)
        self.assertEqual(self.srv.req('POST', f"/api/sandboxes/{sbx['id']}/pause", {}, headers=h)[0], 404)
        self.assertEqual(self.srv.req('POST', f"/api/sandboxes/{sbx['id']}/action", {'command': ['echo', 'x']}, headers=h)[0], 404)
        self.assertEqual(self.srv.req('POST', f"/api/sandboxes/{sbx['id']}/destroy", {}, headers=h)[0], 404)
        # back in its own workspace the sandbox is still there and unchanged
        self.assertEqual(self.srv.req('POST', '/api/workspaces/switch', {'workspace_id': home_ws}, headers=h)[0], 200)
        s0, body0, _ = self.srv.req('GET', f"/api/sandboxes/{sbx['id']}", headers=h)
        self.assertEqual((s0, body0.get('status')), (200, 'READY'), body0)

    def test_different_user_cannot_read_pause_destroy_or_execute(self):
        ha, _ = self.user()
        hb, _ = self.user()
        sbx = self.sandbox(ha)
        self.assertEqual(self.srv.req('GET', f"/api/sandboxes/{sbx['id']}", headers=hb)[0], 404)
        self.assertEqual(self.srv.req('POST', f"/api/sandboxes/{sbx['id']}/pause", {}, headers=hb)[0], 404)
        self.assertEqual(self.srv.req('POST', f"/api/sandboxes/{sbx['id']}/action", {'command': ['echo', 'x']}, headers=hb)[0], 404)
        # destroy by a non-owner: 404 and nothing is destroyed (TASK 019/023)
        self.assertEqual(self.srv.req('POST', f"/api/sandboxes/{sbx['id']}/destroy", {}, headers=hb)[0], 404)
        self.assertEqual(self.srv.req('GET', f"/api/sandboxes/{sbx['id']}", headers=ha)[1]['status'], 'READY')

    def test_missing_identity_is_rejected_at_the_manager_boundary(self):
        from sandbox_manager import SandboxManager, SandboxError
        with tempfile.TemporaryDirectory() as tmp:
            mgr = SandboxManager(base_dir=Path(tmp) / 's', db_path=Path(tmp) / 'd.sqlite3')
            sbx = mgr.create_sandbox('usr_a', 'ws_a')
            for kwargs in ({'caller_user_id': '', 'caller_workspace_id': 'ws_a'}, {'caller_user_id': 'usr_a', 'caller_workspace_id': ''}):
                with self.subTest(kwargs=kwargs), self.assertRaises(Exception):
                    mgr.get_sandbox(sbx.id, **kwargs)


class BrowserPoolBinding(unittest.TestCase):
    """TASK 016–018: a browser session is bound to (sandbox, user, workspace); mismatches change nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pool = CloudBrowserPool()

    def tearDown(self):
        self.pool.close_all(); self.tmp.cleanup()

    def test_unauthorised_close_leaves_the_session_open(self):
        s = self.pool.get_or_create('sbx_1', 'usr_a', str(Path(self.tmp.name) / 'sbx_1'), 'ws_a')
        self.assertFalse(self.pool.close_session('sbx_1', 'usr_b', 'ws_a'))
        self.assertFalse(self.pool.close_session('sbx_1', 'usr_a', 'ws_other'))
        self.assertIs(self.pool.get_session('sbx_1', 'usr_a', 'ws_a'), s)     # still there, still the same object

    def test_owner_close_removes_the_session(self):
        self.pool.get_or_create('sbx_2', 'usr_a', str(Path(self.tmp.name) / 'sbx_2'), 'ws_a')
        self.assertTrue(self.pool.close_session('sbx_2', 'usr_a', 'ws_a'))
        with self.assertRaises(CloudBrowserError) as ctx:
            self.pool.get_session('sbx_2', 'usr_a', 'ws_a')
        self.assertEqual(ctx.exception.status_code, 404)

    def test_same_user_other_workspace_cannot_get_session(self):
        self.pool.get_or_create('sbx_3', 'usr_a', str(Path(self.tmp.name) / 'sbx_3'), 'ws_a')
        with self.assertRaises(CloudBrowserError):
            self.pool.get_session('sbx_3', 'usr_a', 'ws_b')

    def test_empty_identity_is_rejected_everywhere(self):
        with self.assertRaises(CloudBrowserError) as ctx:
            self.pool.get_session('sbx_4', '', '')
        self.assertEqual(ctx.exception.status_code, 401)
        with self.assertRaises(CloudBrowserError):
            self.pool.close_session('sbx_4', 'usr_a', '')


class ProviderSelection(unittest.TestCase):
    """TASK 004–006: default is unavailable; an unknown provider is a startup error, never a silent fallback."""

    def test_default_provider_is_unavailable_and_says_so(self):
        p = select_provider(None)
        self.assertIsInstance(p, UnavailableProvider)
        r = p.report()
        self.assertFalse(r.production_ready)
        self.assertFalse(r.available)
        self.assertEqual(r.isolation_type, 'none')
        with self.assertRaises(ExecutionBlocked):
            p.run('/tmp', ['echo', 'x'], 5, 1024)

    def test_unknown_provider_name_is_refused_not_replaced(self):
        for name in ('subprocess', 'host', 'local', 'directory'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                select_provider(name)


class DirectToolApiCannotWrite(unittest.TestCase):
    """TASK 045–050 / 060: a direct /api/tools/call cannot perform a consequential effect, with or without arguments that
    claim approval. Effects are checked in storage, not by the response alone."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = ProdServer(cls.tmp.name, free_port()).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop(); cls.tmp.cleanup()

    def user(self):
        _, u, _ = self.srv.req('POST', '/api/auth/register', {'email': f'{uuid.uuid4().hex}@resolve.test', 'password': 'secure-pass-123'})
        return {'Authorization': f"Bearer {u['token']}"}

    def call(self, h, name, args, **extra):
        body = {'name': name, 'args': args, **extra}
        return self.srv.req('POST', '/api/tools/call', body, headers=h)

    def test_consequential_tools_refused_and_no_effect_in_storage(self):
        h = self.user()
        docs_before = self.srv.req('GET', '/api/documents', headers=h)[1]['documents']
        ws_before = self.srv.req('GET', '/api/workspaces?tenant=1', headers=h)[1]['workspaces']
        cases = [
            ('document.generate', {'title': 'Injected', 'text': 'x', 'idempotency_key': 'k-gen'}),
            ('workspace.create', {'name': 'Injected', 'idempotency_key': 'k-ws'}),
            ('mission.cancel', {'mission_id': 'bm_nonexistent', 'idempotency_key': 'k-cancel'}),
        ]
        for name, args in cases:
            with self.subTest(name=name):
                s, r, _ = self.call(h, name, args)
                self.assertEqual(s, 200, r)
                self.assertFalse(r['ok'])
                # refused by the read-only scope (PERMISSION_DENIED) before the approval check; either refusal is acceptable
                self.assertIn(r['error']['code'], ('PERMISSION_DENIED', 'APPROVAL_REQUIRED'))
        # an approval claimed inside the arguments or the request body is ignored
        s, r, _ = self.call(h, 'document.generate', {'title': 'Forged', 'text': 'x', 'idempotency_key': 'k-forge',
                                                      'approved_operation': 'document.generate'}, approved_operation='document.generate', capabilities=['document:write'])
        self.assertFalse(r['ok'])
        self.assertIn(r['error']['code'], ('PERMISSION_DENIED', 'APPROVAL_REQUIRED'))
        # storage: nothing was created
        self.assertEqual(self.srv.req('GET', '/api/documents', headers=h)[1]['documents'], docs_before)
        self.assertEqual(self.srv.req('GET', '/api/workspaces?tenant=1', headers=h)[1]['workspaces'], ws_before)

    def test_read_only_tool_still_works_for_the_owner(self):
        h = self.user()
        brief = self.srv.req('POST', '/api/documents/sample-brief', {}, headers=h)[1]['document']
        s, r, _ = self.call(h, 'document.read', {'document_id': brief['id']})
        self.assertTrue(r['ok'], r)
        self.assertIn('Inventory', r['data']['text'])


if __name__ == '__main__':
    unittest.main()
