import json
import tempfile
import time
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from test_web_api_g1 import Server, free_port


class CloudApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.port = free_port()
        cls.srv = Server(cls.tmp.name, cls.port).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.tmp.cleanup()

    def test_end_to_end_cloud_web_flow(self):
        # 1. Register a new user
        reg_status, reg_body = self.srv.req(
            'POST', '/api/auth/register',
            {'email': 'cloud_user@resolve.test', 'password': 'mypassword123', 'display_name': 'Cloud Operator'}
        )
        self.assertEqual(reg_status, 201)
        token = reg_body['token']
        user_id = reg_body['user_id']
        workspace_id = reg_body['workspace_id']
        self.assertTrue(token.startswith('rst_'))

        # 2. Query /api/auth/me with Bearer token
        me_status, me_body = self.srv.req('GET', '/api/auth/me', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(me_status, 200)
        self.assertEqual(me_body['email'], 'cloud_user@resolve.test')
        self.assertEqual(me_body['user_id'], user_id)

        # 3. Provision an isolated cloud sandbox
        sbx_status, sbx_body = self.srv.req(
            'POST', '/api/sandboxes',
            {'mission_id': 'm_cloud_test'},
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(sbx_status, 201)
        sbx_id = sbx_body['id']
        self.assertTrue(sbx_id.startswith('sbx_'))
        self.assertEqual(sbx_body['status'], 'READY')

        # 4. Command execution is refused: no isolated provider is configured, so nothing runs on the host
        exec_status, exec_body = self.srv.req(
            'POST', f'/api/sandboxes/{sbx_id}/action',
            {'command': ['python', '-c', 'import os; print("ISOLATED_OK")']},
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(exec_status, 503)
        self.assertEqual(exec_body.get('status'), 'EXECUTION_BLOCKED')
        self.assertNotIn('ISOLATED_OK', str(exec_body))

        # 5. Pause and resume sandbox
        p_status, p_body = self.srv.req('POST', f'/api/sandboxes/{sbx_id}/pause', {}, headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(p_status, 200)
        self.assertEqual(p_body['status'], 'PAUSED')

        r_status, r_body = self.srv.req('POST', f'/api/sandboxes/{sbx_id}/resume', {}, headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(r_status, 200)
        self.assertEqual(r_body['status'], 'READY')

        # 6. Test connected apps framework
        conn_status, conn_body = self.srv.req('GET', '/api/connectors', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(conn_status, 200)
        self.assertTrue(len(conn_body['connectors']) >= 3)

        # Connect Google Drive with readonly scope
        c_status, c_body = self.srv.req(
            'POST', '/api/connectors/google_drive/connect',
            {'scopes': ['drive.readonly']},
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(c_status, 200)
        self.assertEqual(c_body['status'], 'CONNECTED')

        # List resources
        res_status, res_body = self.srv.req(
            'GET', '/api/connectors/google_drive/resources',
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(res_status, 200)
        self.assertTrue(len(res_body['resources']) >= 1)

        # 7. Voice text resolution is real: bound to ledger entity ids, ambiguity asks, nothing is canned
        voice_status, voice_body = self.srv.req(
            'POST', '/api/voice/transcribe',
            {'text': 'No, not that one. Alex Morgan at Contoso'},
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(voice_status, 200)
        self.assertTrue(voice_body['is_correction'])
        self.assertEqual(voice_body['resolved_entity']['id'], 'contact_alex_2')
        self.assertEqual(voice_body['resolved_entity']['stable_id'], 'crm:contact:208')
        s2, ambiguous = self.srv.req('POST', '/api/voice/transcribe', {'text': 'Send it to Alex'}, headers={'Authorization': f'Bearer {token}'})
        self.assertIsNone(ambiguous['resolved_entity'])
        self.assertEqual({c['id'] for c in ambiguous['candidates']}, {'contact_alex_1', 'contact_alex_2'})
        self.assertEqual(ambiguous['recommended_action'], 'needs_clarification')
        s3, nobody = self.srv.req('POST', '/api/voice/transcribe', {'text': 'Send it to Zed'}, headers={'Authorization': f'Bearer {token}'})
        self.assertIsNone(nobody['resolved_entity']); self.assertEqual(nobody['candidates'], [])

        # 8. Test privacy summary and data export
        priv_status, priv_body = self.srv.req('GET', '/api/privacy/summary', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(priv_status, 200)
        self.assertEqual(priv_body['active_sandboxes'], 1)
        self.assertEqual(priv_body['connected_apps_count'], 1)

        exp_status, exp_body = self.srv.req('POST', '/api/privacy/export', {}, headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(exp_status, 200)
        self.assertTrue(len(exp_body['sha256_checksum']) == 64)

        # 9. Test permanent account deletion
        del_status, del_body = self.srv.req('POST', '/api/privacy/delete-account', {}, headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(del_status, 200)
        self.assertEqual(del_body['status'], 'COMPLETED')
        self.assertTrue(del_body['purged_items']['sessions_invalidated'])

        # 10. Confirm token is dead
        dead_status, dead_body = self.srv.req('GET', '/api/auth/me', headers={'Authorization': f'Bearer {token}'})
        self.assertEqual(dead_status, 401)


if __name__ == '__main__':
    unittest.main()

