"""HTTP-level flagship journey and cross-tenant negatives for the brief mission (production mode, real server)."""
import hashlib
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_production_security import ProdServer
from test_web_api_g1 import free_port

INTENT = 'Resolve, inspect my project brief, analyze the implementation issues, create a corrected architecture, generate a polished report and prepare it for print.'


class BriefMissionApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = ProdServer(self.tmp.name, free_port()).start()

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def user(self):
        _, u, _ = self.srv.req('POST', '/api/auth/register', {'email': f'{uuid.uuid4().hex}@resolve.test', 'password': 'secure-pass-123'})
        return {'Authorization': f"Bearer {u['token']}"}

    def poll(self, mid, h, states):
        for _ in range(200):
            s, m, _ = self.srv.req('GET', f'/api/brief-missions/{mid}', headers=h)
            if m.get('state') in states:
                return m
            time.sleep(0.05)
        self.fail(f'timeout: {m}')

    def test_flagship_journey_download_verify_delete_and_cross_tenant_denial(self):
        a, b = self.user(), self.user()
        self.assertEqual(self.srv.req('POST', '/api/brief-missions', {'intent_text': INTENT})[0], 401)
        s, seeded, _ = self.srv.req('POST', '/api/documents/sample-brief', {}, headers=a)
        self.assertEqual(s, 201, seeded)
        brief = seeded['document']
        s, m, _ = self.srv.req('POST', '/api/brief-missions', {'intent_text': INTENT, 'document_id': brief['id'], 'expected_sha256': brief['sha256']}, headers=a)
        self.assertEqual(s, 202, m)
        m = self.poll(m['id'], a, {'AWAITING_APPROVAL', 'FAILED'})
        self.assertEqual(m['state'], 'AWAITING_APPROVAL', m)
        # user B: every operation on A's mission is a 404 with no data
        for method, path, body in (('GET', f"/api/brief-missions/{m['id']}", None), ('POST', f"/api/brief-missions/{m['id']}/approve", {'plan_digest': m['plan']['digest']}),
                                   ('POST', f"/api/brief-missions/{m['id']}/cancel", {}), ('POST', f"/api/brief-missions/{m['id']}/delete", {})):
            s, body_out, _ = self.srv.req(method, path, body, headers=b)
            self.assertEqual(s, 404, (path, body_out))
            self.assertNotIn('Inventory', str(body_out))
        self.assertEqual(self.srv.req('GET', '/api/brief-missions', headers=b)[1]['missions'], [])
        s, bad, _ = self.srv.req('POST', '/api/brief-missions', {'intent_text': INTENT, 'document_id': brief['id']}, headers=b)
        self.assertIn(s, (400, 404), bad)  # B cannot bind A's document as source
        # stale approval refused; correct approval accepted
        s, e, _ = self.srv.req('POST', f"/api/brief-missions/{m['id']}/approve", {'plan_digest': '0' * 64}, headers=a)
        self.assertEqual(s, 409, e)
        s, e, _ = self.srv.req('POST', f"/api/brief-missions/{m['id']}/approve", {'plan_digest': m['plan']['digest']}, headers=a)
        self.assertEqual(s, 200, e)
        done = self.poll(m['id'], a, {'CONFIRMED', 'FAILED'})
        self.assertEqual(done['state'], 'CONFIRMED', done)
        self.assertEqual(done['chain_broken'], [])
        s, pdf, h = self.srv.req('GET', f"/api/documents/{done['output_doc_id']}/download", headers=a, raw=True)
        self.assertEqual(s, 200)
        self.assertEqual(hashlib.sha256(pdf).hexdigest(), done['output_sha256'])
        self.assertEqual(h['Content-Type'], 'application/pdf')
        self.assertEqual(self.srv.req('GET', f"/api/documents/{done['output_doc_id']}/download", headers=b, raw=True)[0], 404)
        s, pe, _ = self.srv.req('POST', f"/api/brief-missions/{m['id']}/print-event", {}, headers=a)
        self.assertEqual(s, 200)
        # deletion: mission + output gone, later access denied
        s, d, _ = self.srv.req('POST', f"/api/brief-missions/{m['id']}/delete", {}, headers=a)
        self.assertEqual((s, d['output_document_removed']), (200, done['output_doc_id']))
        self.assertEqual(self.srv.req('GET', f"/api/documents/{done['output_doc_id']}/download", headers=a, raw=True)[0], 404)
        self.assertEqual(self.srv.req('GET', f"/api/brief-missions/{m['id']}", headers=a)[0], 404)

    def test_account_deletion_during_running_mission_cleans_everything(self):
        a = self.user()
        _, seeded, _ = self.srv.req('POST', '/api/documents/sample-brief', {}, headers=a)
        s, m, _ = self.srv.req('POST', '/api/brief-missions', {'intent_text': INTENT, 'document_id': seeded['document']['id']}, headers=a)
        s, rec, _ = self.srv.req('POST', '/api/privacy/delete-account', {}, headers=a)
        self.assertEqual(s, 200, rec)
        self.assertIn('missions_removed', rec['purged_items'])
        time.sleep(0.5)
        self.assertEqual(self.srv.req('GET', f"/api/brief-missions/{m['id']}", headers=a)[0], 401)  # session revoked

    def test_unsupported_intent_is_actionable_error(self):
        a = self.user()
        _, seeded, _ = self.srv.req('POST', '/api/documents/sample-brief', {}, headers=a)
        s, e, _ = self.srv.req('POST', '/api/brief-missions', {'intent_text': 'book me a flight', 'document_id': seeded['document']['id']}, headers=a)
        self.assertEqual(s, 422)
        self.assertIn('Unsupported intent', e['error'])


if __name__ == '__main__':
    unittest.main()
