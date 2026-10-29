"""User-supplied projects, multi-language analysis and editable architecture: nothing is bundled-only or canned."""
import base64
import io
import sys
import tempfile
import unittest
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import brief_mission as bm
import project_store as ps
from brief_mission import BriefMissionStore, MissionError
from document_studio import DocumentStudio
from project_store import ProjectStore, ProjectError, read_archive
from test_brief_mission import INTENT, wait_state

JS_APP = '''const db = require('./db');
const API_TOKEN = "live-token-abcdef123456";
function handler(req) {
  // TODO: validate input
  try { return db.find(req.sku); } catch (e) {}
}
module.exports = handler;
'''
JS_DB = '''exports.find = (sku) => conn.query(`SELECT * FROM items WHERE sku = '${sku}'`);
'''


def make_zip(files, symlink=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, text in files.items():
            z.writestr(name, text)
        if symlink:
            info = zipfile.ZipInfo(symlink[0]); info.external_attr = (0o120777 << 16)
            z.writestr(info, symlink[1])
    return buf.getvalue()


class ArchiveSafetyTests(unittest.TestCase):
    def test_unsafe_and_abusive_archives_rejected(self):
        for label, raw in {
            'zip-slip': make_zip({'../../etc/evil.py': 'x=1'}),
            'absolute': make_zip({'/etc/evil.py': 'x=1'}),
            'not a zip': b'PK-not-really',
            'no source': make_zip({'readme.md': 'hello', 'data.json': '{}'}),
        }.items():
            with self.subTest(label), self.assertRaises(ProjectError):
                read_archive(raw)

    def test_limits(self):
        old = (ps.MAX_MEMBERS, ps.MAX_TOTAL_BYTES)
        try:
            ps.MAX_MEMBERS = 3
            with self.assertRaises(ProjectError) as c:
                read_archive(make_zip({f'f{i}.py': 'x=1' for i in range(5)}))
            self.assertEqual(c.exception.status_code, 413)
            ps.MAX_MEMBERS, ps.MAX_TOTAL_BYTES = old[0], 1000
            with self.assertRaises(ProjectError) as c:        # zip bomb: tiny archive, huge expansion
                read_archive(make_zip({'big.py': 'a' * 100000}))
            self.assertEqual(c.exception.status_code, 413)
        finally:
            ps.MAX_MEMBERS, ps.MAX_TOTAL_BYTES = old

    def test_junk_dirs_symlinks_and_non_source_are_skipped(self):
        files = read_archive(make_zip({'src/a.py': 'x=1', 'node_modules/lib/b.js': 'y', '.git/c.py': 'z', 'docs/readme.md': 'm', 'web/app.ts': 'let a = 1'},
                                      symlink=('src/link.py', '/etc/passwd')))
        self.assertEqual(sorted(files), ['src/a.py', 'web/app.ts'])


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.studio = DocumentStudio(base / 'docs')
        self.projects = ProjectStore(base / 'docs')
        self.store = BriefMissionStore(base / 'm.sqlite3', self.studio, projects=self.projects)
        self.brief = self.store.seed_sample_brief('u', 'w')

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def project(self, files, user='u', ws='w', name='p.zip'):
        return self.projects.add(user, ws, name, make_zip(files))

    def run_mission(self, project_id, approve=True):
        m = self.store.start('u', 'w', INTENT, document_id=self.brief['id'], project=project_id)
        m = wait_state(self.store, m['id'], 'u', 'w', {'AWAITING_APPROVAL', 'FAILED'})
        if approve and m['state'] == 'AWAITING_APPROVAL':
            self.store.approve(m['id'], 'u', 'w', m['plan']['digest'])
            m = wait_state(self.store, m['id'], 'u', 'w', {'CONFIRMED', 'FAILED'})
        return m


class UploadedProjectMissionTests(Base):
    def test_javascript_project_is_analyzed_for_real(self):
        p = self.project({'api/handler.js': JS_APP, 'api/db.js': JS_DB})
        m = self.run_mission(p['id'])
        self.assertEqual(m['state'], 'CONFIRMED', m.get('error'))
        rules = {(i['rule'], i['file'], i['line']) for i in m['plan']['issues']}
        self.assertIn(('HARDCODED_SECRET', 'api/handler.js', 2), rules)
        self.assertIn(('SQL_STRING_BUILDING', 'api/db.js', 1), rules)
        self.assertIn(('SILENT_EXCEPT', 'api/handler.js', 5), rules)
        self.assertIn(('OPEN_TODO', 'api/handler.js', 4), rules)
        self.assertNotIn('live-token-abcdef123456', str(m['plan']))                      # secret value never copied
        self.assertEqual(m['plan']['project']['sha256'], p['sha256'])
        self.assertEqual(m['plan']['project']['languages'], {'.js': 2})
        text = self.studio.extract_text('u', 'w', m['output_doc_id']).replace('\n', '')
        self.assertIn('PROJECT-SHA256: ' + p['sha256'], text)
        labels = {n['label'] for n in m['plan']['architecture']['nodes']}
        self.assertIn('api/handler', labels)                                              # diagram follows the uploaded project
        self.assertEqual(m['chain_broken'], [])

    def test_different_projects_give_different_plans_and_outputs(self):
        a = self.run_mission(self.project({'a/x.py': 'PASSWORD = "supersecret123"\n'})['id'])
        b = self.run_mission(self.project({'b/y.ts': 'export const ok = 1;\n'})['id'])
        self.assertNotEqual(a['plan']['digest'], b['plan']['digest'])
        self.assertNotEqual(a['output_sha256'], b['output_sha256'])
        self.assertIn('HARDCODED_SECRET', {i['rule'] for i in a['plan']['issues']})
        self.assertNotIn('HARDCODED_SECRET', {i['rule'] for i in b['plan']['issues']})

    def test_tenant_isolation_for_projects(self):
        p = self.project({'a.py': 'x=1'})
        with self.assertRaises(ProjectError):
            self.projects.get('other', 'w', p['id'])
        with self.assertRaises(ProjectError):
            self.projects.load('u', 'other_ws', p['id'])
        b_brief = self.store.seed_sample_brief('other', 'w2')
        with self.assertRaises(MissionError) as c:
            self.store.start('other', 'w2', INTENT, document_id=b_brief['id'], project=p['id'])
        self.assertEqual(c.exception.status_code, 404)
        with self.assertRaises(MissionError):
            self.store.start('u', 'w', INTENT, document_id=self.brief['id'], project='prj_doesnotexist')

    def test_project_deleted_or_tampered_before_analysis_fails_the_mission(self):
        p = self.project({'a.py': 'x=1'})
        (self.projects.blob_dir / f"{p['id']}.zip").write_bytes(make_zip({'a.py': 'tampered=1'}))
        m = self.run_mission(p['id'])
        self.assertEqual(m['state'], 'FAILED')
        self.assertRegex(m['error'], r'changed|refusing')

    def test_purge_removes_projects(self):
        p = self.project({'a.py': 'x=1'})
        self.assertEqual(self.projects.purge('u', 'w'), 1)
        self.assertFalse((self.projects.blob_dir / f"{p['id']}.zip").exists())
        self.assertEqual(self.projects.list('u', 'w'), [])


class EditableArchitectureTests(Base):
    def awaiting(self):
        return self.run_mission('sample_project', approve=False)

    def test_edit_changes_digest_invalidates_old_approval_and_drives_the_pdf(self):
        m = self.awaiting()
        old = m['plan']['digest']
        src = m['plan']['mermaid'] + '\n    new_cache["Response cache"]:::proposed\n    new_cache -.-> m_inventory_app'
        m2 = self.store.edit_architecture(m['id'], 'u', 'w', src)
        self.assertNotEqual(m2['plan']['digest'], old)
        self.assertTrue(m2['plan']['edited_by_user'])
        with self.assertRaises(MissionError):
            self.store.approve(m['id'], 'u', 'w', old)                     # approval of the pre-edit plan is dead
        self.store.approve(m['id'], 'u', 'w', m2['plan']['digest'])
        done = wait_state(self.store, m['id'], 'u', 'w', {'CONFIRMED', 'FAILED'})
        self.assertEqual(done['state'], 'CONFIRMED', done.get('error'))
        text = self.studio.extract_text('u', 'w', done['output_doc_id']).replace('\n', '')
        self.assertIn('Response cache', text)
        self.assertIn('edited by the user', text)

    def test_invalid_edits_are_rejected_without_changing_the_plan(self):
        m = self.awaiting()
        for bad in ('', 'flowchart TD\n a --> b', 'not mermaid', 'flowchart TD\n a["A"]\n a --> zzz'):
            with self.subTest(bad=bad), self.assertRaises(MissionError) as c:
                self.store.edit_architecture(m['id'], 'u', 'w', bad)
            self.assertIn(c.exception.status_code, (400, 422))
        self.assertEqual(self.store.get(m['id'], 'u', 'w')['plan']['digest'], m['plan']['digest'])

    def test_edit_requires_ownership_and_awaiting_state(self):
        m = self.awaiting()
        with self.assertRaises(MissionError) as c:
            self.store.edit_architecture(m['id'], 'intruder', 'w', m['plan']['mermaid'])
        self.assertEqual(c.exception.status_code, 404)
        self.store.cancel(m['id'], 'u', 'w')
        with self.assertRaises(MissionError) as c:
            self.store.edit_architecture(m['id'], 'u', 'w', m['plan']['mermaid'])
        self.assertEqual(c.exception.status_code, 409)

    def test_parse_roundtrip(self):
        m = self.awaiting()
        g = bm.parse_mermaid(m['plan']['mermaid'])
        self.assertEqual(bm.graph_to_mermaid(g), m['plan']['mermaid'])


class Http(unittest.TestCase):
    def test_upload_project_run_mission_and_deny_other_user(self):
        from test_production_security import ProdServer
        from test_web_api_g1 import free_port
        with tempfile.TemporaryDirectory() as tmp:
            srv = ProdServer(tmp, free_port()).start()
            try:
                def user():
                    _, u, _ = srv.req('POST', '/api/auth/register', {'email': f'{uuid.uuid4().hex}@resolve.test', 'password': 'secure-pass-123'})
                    return {'Authorization': f"Bearer {u['token']}"}
                a, b = user(), user()
                self.assertEqual(srv.req('POST', '/api/projects/upload', {'name': 'x.zip', 'content_base64': ''})[0], 401)
                s, up, _ = srv.req('POST', '/api/projects/upload', {'name': 'shop.zip', 'content_base64': base64.b64encode(make_zip({'api/handler.js': JS_APP, 'api/db.js': JS_DB})).decode()}, headers=a)
                self.assertEqual(s, 201, up)
                pid = up['project']['id']
                s, bad, _ = srv.req('POST', '/api/projects/upload', {'name': 'evil.zip', 'content_base64': base64.b64encode(make_zip({'../x.py': '1'})).decode()}, headers=a)
                self.assertIn(s, (400,), bad)
                s, lst, _ = srv.req('GET', '/api/projects', headers=a)
                self.assertEqual([p['id'] for p in lst['uploaded']], [pid]); self.assertTrue(lst['bundled'])
                self.assertEqual(srv.req('GET', '/api/projects', headers=b)[1]['uploaded'], [])
                self.assertEqual(srv.req('POST', f'/api/projects/{pid}/delete', {}, headers=b)[0], 404)
                _, seeded, _ = srv.req('POST', '/api/documents/sample-brief', {}, headers=a)
                s, m, _ = srv.req('POST', '/api/brief-missions', {'intent_text': INTENT, 'document_id': seeded['document']['id'], 'project': pid}, headers=a)
                self.assertEqual(s, 202, m)
                import time
                for _ in range(200):
                    _, m, _ = srv.req('GET', f"/api/brief-missions/{m['id']}", headers=a)
                    if m['state'] != 'RUNNING': break
                    time.sleep(0.05)
                self.assertEqual(m['state'], 'AWAITING_APPROVAL', m)
                self.assertEqual(m['plan']['project']['sha256'], up['project']['sha256'])
                s, e, _ = srv.req('POST', f"/api/brief-missions/{m['id']}/edit-architecture", {'mermaid': m['plan']['mermaid'] + '\n    n1["Extra"]:::proposed'}, headers=a)
                self.assertEqual(s, 200, e)
                self.assertEqual(srv.req('POST', f"/api/brief-missions/{m['id']}/edit-architecture", {'mermaid': 'x'}, headers=b)[0], 404)
                self.assertEqual(srv.req('POST', f"/api/brief-missions/{m['id']}/edit-architecture", {'mermaid': 'garbage'}, headers=a)[0], 422)
                s, health, _ = srv.req('GET', '/api/health')
                self.assertEqual(health['mode'], 'production'); self.assertIn('features', health)
                s, d, _ = srv.req('POST', '/api/privacy/delete-account', {}, headers=a)
                self.assertEqual(d['purged_items']['projects_removed'], 1)
            finally:
                srv.stop()


if __name__ == '__main__':
    unittest.main()
