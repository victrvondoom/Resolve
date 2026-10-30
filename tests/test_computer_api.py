"""POST /api/computer/run over HTTP against a real production-mode server: real Chromium, per-tenant controlled apps with persisted records."""
import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_production_security import ProdServer
from test_web_api_g1 import free_port


class ComputerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = ProdServer(cls.tmp.name, free_port(), extra={'RESOLVE_DIAGNOSTICS': '1'}).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.tmp.cleanup()

    def user(self):
        _, u, _ = self.srv.req('POST', '/api/auth/register', {'email': f'{uuid.uuid4().hex}@resolve.test', 'password': 'secure-pass-123'})
        return {'Authorization': f"Bearer {u['token']}"}

    def run_cmd(self, h, command, digest=None):
        body = {'command': command}
        if digest:
            body['approved_digest'] = digest
        return self.srv.req('POST', '/api/computer/run', body, headers=h)

    def test_requires_login_and_validates_input(self):
        self.assertEqual(self.srv.req('POST', '/api/computer/run', {'command': 'x' * 10})[0], 401)
        h = self.user()
        self.assertEqual(self.run_cmd(h, 'x')[0], 422)
        self.assertEqual(self.run_cmd(h, 'Find Alex from Finance', digest='nope')[0], 422)
        s, e, _ = self.run_cmd(h, 'Book a flight to Paris please')
        self.assertIn(s, (400, 422), e)

    def test_overdue_task_returns_a_real_frame_and_verified_result(self):
        h = self.user()
        s, r, _ = self.run_cmd(h, 'Open Project Atlas and show my overdue tasks.')
        self.assertEqual(s, 200, r)
        self.assertEqual(r['status'], 'CONFIRMED')
        self.assertEqual(sorted(r['overdue']), ['Fix invoice rounding', 'Migrate legacy accounts', 'Ship onboarding flow'])
        self.assertTrue(r['frame']['data_url'].startswith('data:image/jpeg;base64,'))
        self.assertTrue(r['evidence'] and all(e['ok'] for e in r['evidence']))
        self.assertTrue(re.fullmatch(r'http://127\.0\.0\.1:\d+', r['app_origin']))

    def test_update_is_two_step_persists_per_tenant_and_other_tenants_see_nothing(self):
        a, b = self.user(), self.user()
        cmd = "Update task 'Write launch notes' to done in Project Atlas"
        s, prop, _ = self.run_cmd(a, cmd)
        self.assertEqual((s, prop['status']), (200, 'AWAITING_APPROVAL'), prop)
        s, forged, _ = self.run_cmd(a, cmd, '0' * 64)
        self.assertEqual((s, forged['error']['code']), (409, 'APPROVAL_MISMATCH'))
        s, done, _ = self.run_cmd(a, cmd, prop['proposal']['digest'])
        self.assertEqual((s, done['status']), (200, 'CONFIRMED'), done)
        # tenant A's write persisted in A's own database; B's apps are untouched and a repeat of the same approval is refused
        s, again, _ = self.run_cmd(a, cmd, prop['proposal']['digest'])
        self.assertEqual(s, 409, again)       # the task is at a new version now: the old approval no longer matches
        s, bprop, _ = self.run_cmd(b, cmd)
        self.assertEqual(bprop['proposal']['observed_version'], 1)
        self.assertNotEqual(bprop['proposal']['digest'], done['proposal']['digest'] if 'proposal' in done else '')


if __name__ == '__main__':
    unittest.main()
