"""Every direct document write goes through one boundary and is audited; cross-tenant attempts are refused and
never persist the foreign id; export includes the audit; deletion removes it. Real server, production mode."""
import hashlib
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_production_security import ProdServer
from test_web_api_g1 import free_port


class WriteBoundaryApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = ProdServer(self.tmp.name, free_port()).start()

    def tearDown(self):
        self.srv.stop(); self.tmp.cleanup()

    def user(self):
        _, u, _ = self.srv.req('POST', '/api/auth/register', {'email': f'{uuid.uuid4().hex}@resolve.test', 'password': 'secure-pass-123'})
        return {'Authorization': f"Bearer {u['token']}"}

    def writes(self, h):
        return self.srv.req('GET', '/api/write-audit', headers=h)[1]['writes']

    def test_each_direct_write_is_recorded_with_hashes(self):
        a = self.user()
        s, doc, _ = self.srv.req('POST', '/api/documents/sample-brief', {}, headers=a)
        self.assertEqual(s, 201, doc)
        d = doc['document']
        w = self.writes(a)
        self.assertEqual([x['operation'] for x in w], ['document.seed_sample'])
        self.assertEqual(w[0]['outcome'], 'ALLOWED'); self.assertEqual(w[0]['after_sha256'], d['sha256'])
        s, rep, _ = self.srv.req('POST', '/api/documents/report', {'title': 'Audit me', 'text': 'Body text.'}, headers=a)
        self.assertEqual(s, 201, rep)
        s, tr, _ = self.srv.req('POST', '/api/documents/transform', {'operation': 'rotate', 'document_ids': [d['id']],
                                 'expected_sha256': {d['id']: d['sha256']}, 'degrees': 90}, headers=a)
        self.assertEqual(s, 201, tr)
        s, _, _ = self.srv.req('POST', f"/api/documents/{rep['document']['id']}/delete", {}, headers=a)
        self.assertEqual(s, 200)
        ops = {x['operation']: x for x in self.writes(a)}
        self.assertEqual(ops['document.report']['outcome'], 'ALLOWED')
        self.assertTrue(any(k.startswith('document.transform') and v['outcome'] == 'ALLOWED' and v['before_sha256'] == d['sha256']
                            and v['after_sha256'] == tr['document']['sha256'] for k, v in ops.items()))
        dele = ops['document.delete']
        self.assertEqual((dele['outcome'], dele['target_id']), ('ALLOWED', rep['document']['id']))
        self.assertEqual(dele['before_sha256'], rep['document']['sha256'])

    def test_cross_tenant_write_is_refused_and_foreign_id_is_not_persisted(self):
        a, b = self.user(), self.user()
        d = self.srv.req('POST', '/api/documents/sample-brief', {}, headers=a)[1]['document']
        s, _, _ = self.srv.req('POST', f"/api/documents/{d['id']}/delete", {}, headers=b)
        self.assertEqual(s, 404)
        s, _, _ = self.srv.req('POST', '/api/documents/transform', {'operation': 'rotate', 'document_ids': [d['id']],
                                 'expected_sha256': {d['id']: d['sha256']}, 'degrees': 90}, headers=b)
        self.assertEqual(s, 404)
        # A still owns the document: nothing was changed by B's attempts
        self.assertEqual(self.srv.req('GET', f"/api/documents/{d['id']}/download", headers=a, raw=True)[0], 200)
        bw = self.writes(b)
        self.assertTrue(bw and all(x['outcome'] == 'REJECTED' for x in bw))
        self.assertTrue(all(x['target_id'] is None for x in bw), bw)
        export = self.srv.req('POST', '/api/privacy/export', {}, headers=b)[1]
        self.assertIn('write_audit', export, 'export must be a real bundle, not an error body')
        self.assertNotIn(d['id'], str(export), 'a foreign document id must not appear in another tenant export')

    def test_export_includes_audit_and_workspace_deletion_removes_it(self):
        a = self.user()
        self.srv.req('POST', '/api/documents/sample-brief', {}, headers=a)
        export = self.srv.req('POST', '/api/privacy/export', {}, headers=a)[1]
        self.assertEqual([x['operation'] for x in export['write_audit']], ['document.seed_sample'])
        receipt = self.srv.req('POST', '/api/privacy/delete-account', {}, headers=a)
        self.assertEqual(receipt[0], 200, receipt)
        self.assertEqual(self.srv.req('GET', '/api/write-audit', headers=a)[0], 401)


if __name__ == '__main__':
    unittest.main()
