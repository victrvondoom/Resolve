"""Account/workspace behaviour the UI depends on (production mode, real server)."""
import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_production_security import ProdServer
from test_web_api_g1 import free_port


class AccountApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = ProdServer(self.tmp.name, free_port()).start()

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def user(self):
        _, u, _ = self.srv.req('POST', '/api/auth/register', {'email': f'{uuid.uuid4().hex}@resolve.test', 'password': 'secure-pass-123'})
        return {'Authorization': f"Bearer {u['token']}"}, u

    def test_workspace_switcher_lists_the_users_own_workspaces_not_the_concept_catalog(self):
        a, ua = self.user()
        b, _ = self.user()
        s, ws, _ = self.srv.req('GET', '/api/workspaces?tenant=1', headers=a)
        self.assertEqual(s, 200)
        self.assertEqual([w['id'] for w in ws['workspaces']], [ua['workspace_id']])
        self.assertEqual(ws['active_workspace_id'], ua['workspace_id'])
        self.assertTrue(all(w['id'].startswith('ws_') for w in ws['workspaces']))
        self.assertEqual(self.srv.req('GET', '/api/workspaces?tenant=1')[0], 401)
        self.assertNotIn(ua['workspace_id'], json.dumps(self.srv.req('GET', '/api/workspaces?tenant=1', headers=b)[1]))

    def test_deleting_the_active_workspace_keeps_you_signed_in(self):
        a, ua = self.user()
        s, second, _ = self.srv.req('POST', '/api/workspaces/create', {'name': 'Client B'}, headers=a)
        self.assertEqual(s, 201, second)
        self.assertEqual(self.srv.req('POST', '/api/workspaces/switch', {'workspace_id': second['id']}, headers=a)[0], 200)
        s, rec, _ = self.srv.req('POST', '/api/privacy/delete-workspace', {}, headers=a)   # deletes the ACTIVE workspace
        self.assertEqual(s, 200, rec)
        s, me, _ = self.srv.req('GET', '/api/auth/me', headers=a)
        self.assertEqual(s, 200, me)                                   # previously: 401, silently logged out
        self.assertEqual(me['workspace_id'], ua['workspace_id'])      # moved to the surviving workspace
        s, err, _ = self.srv.req('POST', '/api/privacy/delete-workspace', {}, headers=a)   # the last workspace is protected
        self.assertEqual(s, 400, err)

    def test_workspace_names_and_quota_are_validated(self):
        a, _ = self.user()
        for bad in ('', '   ', 'x' * 61):
            self.assertEqual(self.srv.req('POST', '/api/workspaces/create', {'name': bad}, headers=a)[0], 400, bad)
        codes = [self.srv.req('POST', '/api/workspaces/create', {'name': f'w{i}'}, headers=a)[0] for i in range(10)]
        self.assertIn(429, codes)

    def test_me_does_not_echo_the_session_token_and_privacy_summary_is_truthful(self):
        a, ua = self.user()
        s, me, _ = self.srv.req('GET', '/api/auth/me', headers=a)
        self.assertNotIn('token', me)
        self.assertNotIn(ua['token'], json.dumps(me))
        self.srv.req('POST', '/api/documents/sample-brief', {}, headers=a)
        s, summary, _ = self.srv.req('GET', '/api/privacy/summary', headers=a)
        self.assertEqual((summary['documents_count'], summary['missions_count'], summary['projects_count']), (1, 0, 0))
        self.assertGreater(summary['storage_used_bytes'], 0)
        self.assertNotIn('30 days', json.dumps(summary))                 # no invented backup retention
        s, rec, _ = self.srv.req('POST', '/api/privacy/delete-account', {}, headers=a)
        self.assertNotIn('30 days', rec['retention_notice'])
        self.assertIn('no backups of its own', rec['retention_notice'])


if __name__ == '__main__':
    unittest.main()
