"""Real HTTP document workflow with tenant, version, and deletion boundaries."""
import base64
import hashlib
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web_api_g1 import Server, free_port


class DocumentApiTests(unittest.TestCase):
    def test_upload_transform_download_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            srv = Server(tmp, free_port()).start()
            try:
                def register():
                    status, user = srv.req('POST', '/api/auth/register', {
                        'email': f'{uuid.uuid4().hex}@resolve.test', 'password': 'secure-pass-123'})
                    self.assertEqual(status, 201)
                    return {'Authorization': f"Bearer {user['token']}"}, user

                a, user_a = register()
                b, _ = register()
                status, result = srv.req('POST', '/api/documents/report',
                    {'title': 'Project brief', 'text': 'A real PDF for the print workflow.'}, headers=a)
                self.assertEqual(status, 201, result)
                original = result['document']
                req = urllib.request.Request(
                    f"http://127.0.0.1:{srv.port}/api/documents/{original['id']}/download", headers=a)
                with urllib.request.urlopen(req) as response:
                    raw = response.read()
                    self.assertEqual(response.headers['Content-Type'], 'application/pdf')
                self.assertTrue(raw.startswith(b'%PDF-'))
                self.assertEqual(hashlib.sha256(raw).hexdigest(), original['sha256'])
                self.assertEqual(srv.req('GET', '/api/documents', headers={'X-Resolve-Token': ''})[0], 401)
                self.assertEqual(srv.req('GET', '/api/documents', headers={'Authorization': 'Bearer invalid'})[0], 401)

                status, denied = srv.req('GET', f"/api/documents/{original['id']}/download", headers=b)
                self.assertEqual(status, 404, denied)
                status, uploaded = srv.req('POST', '/api/documents/upload', {
                    'name': 'copy.pdf', 'content_base64': base64.b64encode(raw).decode()}, headers=a)
                self.assertEqual(status, 201, uploaded)
                copy = uploaded['document']
                status, merged = srv.req('POST', '/api/documents/transform', {
                    'operation': 'merge', 'document_ids': [original['id'], copy['id']],
                    'expected_sha256': {original['id']: original['sha256'], copy['id']: copy['sha256']}}, headers=a)
                self.assertEqual(status, 201, merged)
                status, stale = srv.req('POST', '/api/documents/transform', {
                    'operation': 'rotate', 'document_ids': [original['id']],
                    'expected_sha256': {original['id']: '0' * 64}, 'degrees': 90}, headers=a)
                self.assertEqual(status, 409, stale)
                status, export = srv.req('POST', '/api/privacy/export', {}, headers=a)
                self.assertEqual(status, 200)
                self.assertEqual(len(export['documents']), 3)
                status, deleted = srv.req('POST', '/api/privacy/delete-account', {}, headers=a)
                self.assertEqual(status, 200, deleted)
                self.assertTrue(deleted['purged_items']['documents_removed'])
                self.assertEqual(srv.req('GET', '/api/documents', headers=a)[0], 401)
            finally:
                srv.stop()


if __name__ == '__main__':
    unittest.main()
