"""Document & Print Studio over a real HTTP server: reorder, extract, compare, page metadata and the print status
model. PDFs are real bytes (reportlab) and every result is read back from the server with pypdf.

A browser print dialog is not proof of printing: these tests pin that PRINTER_CONFIRMED can never be set
through the API, whatever the job state."""
import base64
import io
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from pypdf import PdfReader

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_web_api_g1 import Server, free_port
from test_document_studio import text_pdf


class DocumentPrintApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = Server(cls.tmp.name, free_port()).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.tmp.cleanup()

    def register(self):
        status, user = self.srv.req('POST', '/api/auth/register', {'email': f'{uuid.uuid4().hex}@resolve.test', 'password': 'secure-pass-123'})
        self.assertEqual(status, 201, user)
        return {'Authorization': f"Bearer {user['token']}"}

    def upload(self, headers, labels, name='doc.pdf'):
        status, out = self.srv.req('POST', '/api/documents/upload', {'name': name, 'content_base64': base64.b64encode(text_pdf(labels)).decode()}, headers=headers)
        self.assertEqual(status, 201, out)
        return out['document']

    def download(self, headers, doc_id):
        req = urllib.request.Request(f'http://127.0.0.1:{self.srv.port}/api/documents/{doc_id}/download', headers=headers)
        with urllib.request.urlopen(req) as res:
            return res.read()

    def texts(self, headers, doc_id):
        return [(p.extract_text() or '').strip() for p in PdfReader(io.BytesIO(self.download(headers, doc_id))).pages]

    def transform(self, headers, doc, **body):
        return self.srv.req('POST', '/api/documents/transform', {'document_ids': [doc['id']], 'expected_sha256': {doc['id']: doc['sha256']}, **body}, headers=headers)

    def test_reorder_and_extract_read_back_from_server(self):
        a = self.register()
        doc = self.upload(a, ['Page One', 'Page Two', 'Page Three'])
        status, out = self.transform(a, doc, operation='reorder', order=[3, 1, 2])
        self.assertEqual(status, 201, out)
        reordered = out['document']
        self.assertEqual(self.texts(a, reordered['id']), ['Page Three', 'Page One', 'Page Two'])
        self.assertEqual(reordered['source_sha256'], doc['sha256'])
        self.assertEqual(self.texts(a, doc['id']), ['Page One', 'Page Two', 'Page Three'])   # source never overwritten
        status, bad = self.transform(a, doc, operation='reorder', order=[1, 1, 2])
        self.assertEqual(status, 400, bad)
        status, out = self.transform(a, reordered, operation='extract', ranges='1,3')
        self.assertEqual(status, 201, out)
        self.assertEqual(self.texts(a, out['document']['id']), ['Page Three', 'Page Two'])
        status, bad = self.transform(a, doc, operation='extract', ranges='2-9')
        self.assertEqual(status, 400, bad)
        status, stale = self.srv.req('POST', '/api/documents/transform', {'operation': 'reorder', 'document_ids': [doc['id']],
                                     'expected_sha256': {doc['id']: '0' * 64}, 'order': [2, 1, 3]}, headers=a)
        self.assertEqual(status, 409, stale)
        status, audit = self.srv.req('GET', '/api/write-audit', headers=a)
        self.assertEqual(status, 200)
        self.assertIn('document.transform:reorder', {w['operation'] for w in audit['writes']})

    def test_compare_detects_changed_page_and_page_metadata(self):
        a = self.register()
        one = self.upload(a, ['Intro', 'Budget 100', 'Close'], 'v1.pdf')
        two = self.upload(a, ['Intro', 'Budget 250', 'Close'], 'v2.pdf')
        status, cmp_ = self.srv.req('POST', '/api/documents/compare', {'a': one['id'], 'b': two['id']}, headers=a)
        self.assertEqual(status, 200, cmp_)
        self.assertEqual([p['status'] for p in cmp_['pages']], ['SAME', 'CHANGED', 'SAME'])
        self.assertEqual((cmp_['a']['sha256'], cmp_['b']['sha256']), (one['sha256'], two['sha256']))
        self.assertFalse(cmp_['identical_bytes'])
        status, stale = self.srv.req('POST', '/api/documents/compare', {'a': one['id'], 'b': two['id'], 'expected_sha256': {one['id']: one['sha256'], two['id']: 'old'}}, headers=a)
        self.assertEqual(status, 409, stale)
        status, info = self.srv.req('GET', f"/api/documents/{one['id']}/pages", headers=a)
        self.assertEqual(status, 200, info)
        self.assertEqual(info['page_count'], 3)
        self.assertEqual(info['pages'][0], {'page': 1, 'width_pt': 595.28, 'height_pt': 841.89, 'rotation': 0, 'orientation': 'portrait', 'paper': 'A4'})
        req = urllib.request.Request(f"http://127.0.0.1:{self.srv.port}/api/documents/{one['id']}/pages/2/thumbnail.png", headers=a)
        if info['thumbnails']['available']:
            with urllib.request.urlopen(req) as res:
                self.assertEqual(res.headers['Content-Type'], 'image/png')
                self.assertTrue(res.read().startswith(b'\x89PNG'))
        else:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req)
            self.assertEqual(ctx.exception.code, 501)

    def test_print_status_model_over_http(self):
        a = self.register()
        doc = self.upload(a, ['Cover', 'Body', 'Appendix'])
        base = f"/api/documents/{doc['id']}/print-status"
        status, stale = self.srv.req('POST', base, {'status': 'PREVIEW_READY', 'expected_sha256': 'old', 'paper': 'A4'}, headers=a)
        self.assertEqual(status, 409, stale)
        status, out = self.srv.req('POST', base, {'status': 'PREVIEW_READY', 'expected_sha256': doc['sha256'], 'paper': 'Letter',
                                                  'orientation': 'landscape', 'page_range': '1-2'}, headers=a)
        self.assertEqual(status, 201, out)
        job = out['job']
        self.assertEqual((job['status'], job['paper'], job['orientation'], job['page_range']), ('PREVIEW_READY', 'Letter', 'landscape', '1-2'))
        self.assertIn('not proof of printing', out['note'])
        # PRINTER_CONFIRMED is refused from the API in every state, including after the user's own SUBMITTED report.
        status, refused = self.srv.req('POST', base, {'status': 'PRINTER_CONFIRMED', 'job_id': job['id']}, headers=a)
        self.assertEqual(status, 403, refused)
        status, out = self.srv.req('POST', base, {'status': 'ARTIFACT_GENERATED', 'job_id': job['id']}, headers=a)
        self.assertEqual(status, 200, out)
        job = out['job']
        self.assertEqual(self.texts(a, job['artifact_id']), ['Cover', 'Body'])
        status, bad = self.srv.req('POST', base, {'status': 'PRINT_DIALOG_OPENED', 'job_id': job['id'], 'artifact_sha256': doc['sha256']}, headers=a)
        self.assertEqual(status, 409, bad)
        for step in ('PRINT_DIALOG_OPENED', 'SUBMITTED'):
            status, out = self.srv.req('POST', base, {'status': step, 'job_id': job['id'], 'artifact_sha256': job['artifact_sha256']}, headers=a)
            self.assertEqual(status, 200, out)
        status, refused = self.srv.req('POST', base, {'status': 'PRINTER_CONFIRMED', 'job_id': job['id'], 'source': 'printer'}, headers=a)
        self.assertEqual(status, 403, refused)
        status, back = self.srv.req('GET', base, headers=a)
        self.assertEqual(status, 200, back)
        job = back['jobs'][0]
        self.assertEqual(job['status'], 'SUBMITTED')
        self.assertFalse(job['physical_printing_verified'])
        self.assertNotIn('PRINTER_CONFIRMED', job['allowed_next'])
        self.assertEqual([(e['to_status'], e['source']) for e in job['events']],
                         [('PREVIEW_READY', 'server'), ('ARTIFACT_GENERATED', 'server'), ('PRINT_DIALOG_OPENED', 'browser'), ('SUBMITTED', 'user_attested')])
        self.assertIn('PRINTER_CONFIRMED', back['states'])
        status, bad = self.srv.req('POST', base, {'status': 'ARTIFACT_GENERATED', 'job_id': job['id']}, headers=a)
        self.assertEqual(status, 409, bad)   # not an allowed transition from SUBMITTED
        status, bad = self.srv.req('POST', base, {'status': 'DONE', 'job_id': job['id']}, headers=a)
        self.assertEqual(status, 400, bad)

    def test_cross_user_access_is_denied(self):
        a, b = self.register(), self.register()
        doc = self.upload(a, ['Secret one', 'Secret two'])
        other = self.upload(a, ['Secret one', 'Changed two'])
        mine = self.upload(b, ['Bob page'])
        status, out = self.srv.req('POST', f"/api/documents/{doc['id']}/print-status", {'status': 'PREVIEW_READY', 'expected_sha256': doc['sha256']}, headers=a)
        self.assertEqual(status, 201, out)
        job = out['job']
        checks = [
            self.transform(b, doc, operation='reorder', order=[2, 1]),
            self.transform(b, doc, operation='extract', ranges='1'),
            self.srv.req('POST', '/api/documents/compare', {'a': doc['id'], 'b': other['id']}, headers=b),
            self.srv.req('POST', '/api/documents/compare', {'a': mine['id'], 'b': doc['id']}, headers=b),
            self.srv.req('GET', f"/api/documents/{doc['id']}/print-status", headers=b),
            self.srv.req('POST', f"/api/documents/{doc['id']}/print-status", {'status': 'ARTIFACT_GENERATED', 'job_id': job['id']}, headers=b),
            self.srv.req('POST', f"/api/documents/{doc['id']}/print-status", {'status': 'PREVIEW_READY', 'expected_sha256': doc['sha256']}, headers=b),
            self.srv.req('POST', f"/api/documents/{mine['id']}/print-status", {'status': 'ARTIFACT_GENERATED', 'job_id': job['id']}, headers=b),
            self.srv.req('GET', f"/api/documents/{doc['id']}/pages", headers=b),
        ]
        for i, (status, body) in enumerate(checks):
            self.assertIn(status, (403, 404), (i, status, body))
            self.assertNotIn('Secret', str(body))
        self.assertEqual(self.srv.req('GET', f"/api/documents/{doc['id']}/print-status", headers={'X-Resolve-Token': ''})[0], 401)
        status, back = self.srv.req('GET', f"/api/documents/{doc['id']}/print-status", headers=a)
        self.assertEqual(back['jobs'][0]['status'], 'PREVIEW_READY')   # B's attempts changed nothing


try:
    from playwright.sync_api import sync_playwright, expect
    HAVE_PW = True
except ImportError:
    HAVE_PW = False


@unittest.skipUnless(HAVE_PW, 'browser-e2e: playwright not installed')
class DocumentStudioUiE2E(unittest.TestCase):
    """Real Chromium against a PRODUCTION-mode server (CSP enforced). DOM-level actions only (Playwright), not
    physical cursor movement. window.print() is replaced by a recorder, so no dialog is shown and nothing is
    printed: this pins what the UI reports, never that paper came out of a printer."""

    @classmethod
    def setUpClass(cls):
        from test_production_security import ProdServer
        from cloud_browser import _chromium_executable
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = ProdServer(cls.tmp.name, free_port()).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=_chromium_executable())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.stop(); cls.tmp.cleanup()

    def test_reorder_compare_and_print_status_in_ui(self):
        context = self.browser.new_context(viewport={'width': 1360, 'height': 900})
        self.addCleanup(context.close)
        # Record print() calls inside the preview frame instead of opening a dialog.
        context.add_init_script("window.print = () => { try { window.top.__printCalls = (window.top.__printCalls || 0) + 1; } catch (_) {} };")
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('dialog', lambda d: d.accept())
        page.goto(f'http://127.0.0.1:{self.srv.port}/app')
        page.wait_for_selector('#authDialog[open]'); page.click('#authDemo'); page.wait_for_selector('#authDialog', state='hidden')
        page.click('.sidebar a[data-view="documents"]')
        page.set_input_files('#docUpload', {'name': 'three.pdf', 'mimeType': 'application/pdf', 'buffer': text_pdf(['Page One', 'Page Two', 'Page Three'])})
        page.click('#docUploadBtn')
        expect(page.locator('#docStatus')).to_contain_text('PDF uploaded', timeout=15000)
        expect(page.locator('#docPageList li')).to_have_count(3)
        expect(page.locator('#docPageList li').first).to_contain_text('A4')

        page.select_option('#docOperation', 'reorder')
        expect(page.locator('#docOrder')).to_be_visible()
        page.fill('#docOrder', '3, 1, 2')
        page.click('#docTransform')
        expect(page.locator('#docStatus')).to_contain_text('New PDF created', timeout=15000)
        token = page.evaluate("sessionStorage.getItem('resolve_token')")
        auth = {'Authorization': 'Bearer ' + token}
        status, listing, _ = self.srv.req('GET', '/api/documents', headers=auth)
        self.assertEqual(status, 200)
        by_op = {d['operation']: d for d in listing['documents']}
        _, raw, _ = self.srv.req('GET', f"/api/documents/{by_op['reorder']['id']}/download", headers=auth, raw=True)
        self.assertEqual([(p.extract_text() or '').strip() for p in PdfReader(io.BytesIO(raw)).pages], ['Page Three', 'Page One', 'Page Two'])

        page.select_option('#docCompareA', by_op['upload']['id'])
        page.select_option('#docCompareB', by_op['reorder']['id'])
        page.click('#docCompare')
        expect(page.locator('#docCompareResult tr[data-status="CHANGED"]')).to_have_count(3, timeout=15000)

        # print the reordered version, pages 2-3, on Letter landscape
        page.select_option('#docPaper', 'Letter'); page.select_option('#docOrientation', 'landscape'); page.fill('#docPrintRange', '2-3')
        page.click('#docPrint')
        expect(page.locator('#docPrintState')).to_have_attribute('data-status', 'PRINT_DIALOG_OPENED', timeout=15000)
        expect(page.locator('#docPrintState')).to_contain_text('not proof of printing')
        self.assertGreaterEqual(page.evaluate('window.__printCalls || 0'), 1)
        page.click('#docPrintSubmitted')
        expect(page.locator('#docPrintState')).to_have_attribute('data-status', 'SUBMITTED')
        self.assertNotIn('Printer confirmed', page.locator('#view-documents').inner_text())
        status, jobs, _ = self.srv.req('GET', f"/api/documents/{by_op['reorder']['id']}/print-status", headers=auth)
        job = jobs['jobs'][0]
        self.assertEqual((job['status'], job['paper'], job['orientation'], job['page_range']), ('SUBMITTED', 'Letter', 'landscape', '2-3'))
        self.assertEqual([e['source'] for e in job['events']], ['server', 'server', 'browser', 'user_attested'])
        _, art, _ = self.srv.req('GET', f"/api/documents/{job['artifact_id']}/download", headers=auth, raw=True)
        self.assertEqual([(p.extract_text() or '').strip() for p in PdfReader(io.BytesIO(art)).pages], ['Page One', 'Page Two'])
        self.assertEqual(errors, [])


class LegacySchemaMigrationTests(unittest.TestCase):
    def test_existing_database_gains_source_hash_column(self):
        from document_studio import DocumentStudio
        with tempfile.TemporaryDirectory() as tmp:
            db = sqlite3.connect(Path(tmp) / 'documents.sqlite3')
            db.execute("""CREATE TABLE documents (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, name TEXT NOT NULL,
                          sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, created_at REAL NOT NULL, source_ids TEXT NOT NULL, operation TEXT NOT NULL)""")
            db.commit(); db.close()
            studio = DocumentStudio(tmp)
            doc = studio.import_pdf('u', 'w', text_pdf(['x', 'y']), 'a.pdf')
            out = studio.transform('u', 'w', 'reorder', [doc['id']], {doc['id']: doc['sha256']}, order=[2, 1])
            self.assertEqual(out['source_sha256'], doc['sha256'])


if __name__ == '__main__':
    unittest.main()
