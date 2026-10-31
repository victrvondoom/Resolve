"""Operations and account security against real processes and real files:
atomic migrations, WAL, backup -> verify -> restore, session tokens stored hashed, renewal cap, password change revoking
every session, liveness/readiness endpoints, and the LOCAL inbox journey over the real HTTP server and local mail server.
"""
import base64
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))
import inbox_store as istore
import resolve_admin
import tenant_auth
from at_rest import SealedStore
from inbox_store import InboxStore


def sealer(tmp):
    return SealedStore(Path(tmp) / 'k', env={'RESOLVE_MASTER_KEY': base64.urlsafe_b64encode(bytes(range(32))).decode()})


class Migrations(unittest.TestCase):
    def test_idempotent_wal_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'inbox.sqlite3'
            InboxStore(db, sealer(tmp))
            st = InboxStore(db, sealer(tmp)).migration_status()
            self.assertEqual(st['applied'], [v for v, _ in istore.ALL_MIGRATIONS])
            self.assertEqual((st['pending'], st['up_to_date'], st['journal_mode'], st['integrity']), ([], True, 'wal', 'ok'))

    def test_a_failing_migration_leaves_no_partial_schema_and_no_version_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'inbox.sqlite3'
            InboxStore(db, sealer(tmp))
            saved = list(istore.ALL_MIGRATIONS)
            istore.ALL_MIGRATIONS.append((99, 'CREATE TABLE half_done (x INTEGER); INSERT INTO no_such_table VALUES (1);'))
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    InboxStore(db, sealer(tmp))
            finally:
                istore.ALL_MIGRATIONS[:] = saved
            conn = sqlite3.connect(db)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            versions = {r[0] for r in conn.execute('SELECT version FROM schema_migrations')}
            conn.close()
            self.assertNotIn('half_done', tables, 'the DDL rolled back with the failed statement')
            self.assertNotIn(99, versions)
            InboxStore(db, sealer(tmp))            # and the store still opens normally afterwards

    def test_upgrade_from_the_previous_schema_keeps_existing_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'inbox.sqlite3'
            saved = list(istore.ALL_MIGRATIONS)
            istore.ALL_MIGRATIONS[:] = [m for m in saved if m[0] <= 7]      # the schema shipped at the previous checkpoint
            try:
                old = InboxStore(db, sealer(tmp))
                old.upsert_connection('u1', 'w1', 'gmail', 'CONNECTED', ['read'], 'existing user data')
                conn_id = old.get_connection('u1', 'w1', 'gmail')['id']
                run = old.start_sync_run('u1', 'w1', conn_id, 'LIVE')
                old.finish_sync_run('u1', 'w1', run, 'SUCCEEDED', fetched=3)
            finally:
                istore.ALL_MIGRATIONS[:] = saved
            new = InboxStore(db, sealer(tmp))
            self.assertEqual(new.migration_status()['pending'], [])
            self.assertEqual(new.get_connection('u1', 'w1', 'gmail')['detail'], 'existing user data')
            runs = new.list_sync_runs('u1', 'w1')
            self.assertEqual((runs[0]['fetched'], runs[0]['kind']), (3, 'FULL'), 'old runs get the default kind')
            new.set_sync_cursor('u1', 'w1', 'gmail', '123')
            self.assertEqual(new.get_sync_cursor('u1', 'w1', 'gmail'), '123')

    def test_admin_migrations_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'inbox.sqlite3'
            InboxStore(db, sealer(tmp))
            out = resolve_admin.migrations(db)
            self.assertEqual((out['pending'], out['unknown_applied'], out['integrity']), ([], [], 'ok'))


class BackupRestore(unittest.TestCase):
    def test_backup_verify_restore_round_trip_and_tamper_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp) / 'data'
            store = InboxStore(data / 'inbox.sqlite3', sealer(tmp))
            store.upsert_connection('u1', 'w1', 'gmail', 'CONNECTED', ['read'], 'before backup')
            man = resolve_admin.backup(data, Path(tmp) / 'bk')
            self.assertFalse(man['key_included'])
            self.assertEqual([f['path'] for f in man['files']], ['inbox.sqlite3'])
            self.assertTrue(resolve_admin.verify(Path(tmp) / 'bk')['ok'])
            store.upsert_connection('u1', 'w1', 'gmail', 'REVOKED', [], 'after backup')
            resolve_admin.restore(Path(tmp) / 'bk', data)
            again = InboxStore(data / 'inbox.sqlite3', sealer(tmp))
            self.assertEqual(again.get_connection('u1', 'w1', 'gmail')['status'], 'CONNECTED')
            self.assertTrue((data / 'inbox.sqlite3.pre-restore').exists(), 'the replaced database is kept')
            with open(Path(tmp) / 'bk' / 'inbox.sqlite3', 'r+b') as f:
                f.seek(200)
                f.write(b'\x00\xff' * 8)
            bad = resolve_admin.verify(Path(tmp) / 'bk')
            self.assertFalse(bad['ok'])
            with self.assertRaises(SystemExit):
                resolve_admin.restore(Path(tmp) / 'bk', data)


class Sessions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = tenant_auth.TenantAuthManager(Path(self.tmp.name) / 't.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_tokens_are_stored_hashed(self):
        s = self.mgr.register('a@example.test', 'correct horse battery')
        raw = (Path(self.tmp.name) / 't.sqlite3').read_bytes()
        self.assertNotIn(s['token'].encode(), raw)
        self.assertEqual(self.mgr.validate_session(s['token'])['user_id'], s['user_id'])

    def test_password_change_revokes_every_session(self):
        s1 = self.mgr.register('a@example.test', 'correct horse battery')
        s2 = self.mgr.login('a@example.test', 'correct horse battery')
        with self.assertRaises(tenant_auth.TenantError):
            self.mgr.change_password(s1['token'], 'wrong password!!', 'a brand new password')
        fresh = self.mgr.change_password(s1['token'], 'correct horse battery', 'a brand new password')
        for old in (s1, s2):
            with self.assertRaises(tenant_auth.TenantError):
                self.mgr.validate_session(old['token'])
        self.assertEqual(self.mgr.validate_session(fresh['token'])['user_id'], s1['user_id'])
        with self.assertRaises(tenant_auth.TenantError):
            self.mgr.login('a@example.test', 'correct horse battery')
        self.mgr.login('a@example.test', 'a brand new password')

    def test_renewal_slides_but_never_passes_the_absolute_cap(self):
        s = self.mgr.register('a@example.test', 'correct horse battery')
        db = sqlite3.connect(Path(self.tmp.name) / 't.sqlite3')
        now = time.time()
        db.execute('UPDATE sessions SET created_at=?, expires_at=?', (now - tenant_auth.SESSION_ABSOLUTE_SEC + 3600, now + 600))
        db.commit()
        self.mgr.validate_session(s['token'])
        exp = db.execute('SELECT expires_at FROM sessions').fetchone()[0]
        self.assertLessEqual(exp, now - tenant_auth.SESSION_ABSOLUTE_SEC + 3600 + tenant_auth.SESSION_ABSOLUTE_SEC + 1)
        self.assertGreater(exp, now + 600)
        db.execute('UPDATE sessions SET expires_at=?', (now - 1,))
        db.commit()
        db.close()
        with self.assertRaises(tenant_auth.TenantError):
            self.mgr.validate_session(s['token'])

    def test_legacy_plaintext_sessions_are_invalidated(self):
        s = self.mgr.register('a@example.test', 'correct horse battery')
        db = sqlite3.connect(Path(self.tmp.name) / 't.sqlite3')
        db.execute("INSERT INTO sessions VALUES ('rst_legacy', ?, ?, ?, ?)", (s['user_id'], s['workspace_id'], time.time(), time.time() + 999))
        db.commit()
        db.close()
        tenant_auth.TenantAuthManager(Path(self.tmp.name) / 't.sqlite3')
        with self.assertRaises(tenant_auth.TenantError):
            self.mgr.validate_session('rst_legacy')


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class RealServer(unittest.TestCase):
    """The production entry point (src/resolve_app.py) in a subprocess, with its own local mail server."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        t = Path(cls.tmp.name)
        cls.port = free_port()
        env = dict(os.environ, RESOLVE_PORT=str(cls.port), RESOLVE_DB=str(t / 'r.sqlite3'), RESOLVE_FIXTURE_DIR=str(t / 'fx'),
                   RESOLVE_TENANTS_DB=str(t / 'tenants.sqlite3'), RESOLVE_SANDBOXES_DIR=str(t / 'sbx'),
                   RESOLVE_SANDBOXES_DB=str(t / 'sbx.sqlite3'), RESOLVE_CONNECTORS_DB=str(t / 'conn.sqlite3'),
                   RESOLVE_DOCUMENTS_DIR=str(t / 'documents'), RESOLVE_INBOX_DB=str(t / 'inbox.sqlite3'),
                   RESOLVE_LOCAL_SMTP_PORT=str(free_port()), RESOLVE_LOCAL_POP3_PORT=str(free_port()), RESOLVE_DIAGNOSTICS='0')
        env.pop('RESOLVE_MODEL_BASE_URL', None)
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / 'src' / 'resolve_app.py')], env=env, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        for _ in range(150):
            try:
                if cls.req('GET', '/api/livez')[0] == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError('server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(10)
        cls.tmp.cleanup()

    @classmethod
    def req(cls, method, path, body=None, token=None):
        data = json.dumps(body).encode() if body is not None else None
        h = {'Content-Type': 'application/json', 'Origin': f'http://127.0.0.1:{cls.port}'}
        if token:
            h['Authorization'] = f'Bearer {token}'
        r = urllib.request.Request(f'http://127.0.0.1:{cls.port}{path}', data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b'{}')

    def register(self, email):
        st, s = self.req('POST', '/api/auth/register', {'email': email, 'password': 'correct horse battery'})
        self.assertEqual(st, 201, s)
        return s['token']

    def test_liveness_and_readiness(self):
        self.assertEqual(self.req('GET', '/api/livez'), (200, {'status': 'alive'}))
        st, ready = self.req('GET', '/api/readyz')
        self.assertEqual(st, 200, ready)
        self.assertTrue(ready['required']['inbox_database']['ok'])
        self.assertEqual(ready['required']['inbox_database']['journal_mode'], 'wal')
        self.assertEqual(ready['optional']['local_mail'], 'RUNNING')
        self.assertEqual(ready['optional']['model'], 'RULES_ONLY')
        self.assertNotIn('key', json.dumps(ready['optional']).lower().replace('sealing_key', ''))

    def test_local_journey_over_http_with_tenant_isolation(self):
        a = self.register('owner-a@example.test')
        b = self.register('owner-b@example.test')
        st, c = self.req('POST', '/api/inbox/connections/local/create', {'name': 'owner-a'}, a)
        self.assertEqual((st, c['status']), (201, 'CONNECTED'), c)
        self.assertNotIn('password', json.dumps(c))
        st, d = self.req('POST', '/api/inbox/local/deliver', {'from_name': 'priya', 'subject': 'Checklist',
                                                               'body': 'Could you send the deployment checklist before 4 PM?'}, a)
        self.assertEqual(st, 201, d)
        st, s = self.req('POST', '/api/inbox/sync', {'mode': 'LOCAL'}, a)
        self.assertEqual((st, s['fetched']), (200, 1), s)
        st, threads = self.req('GET', '/api/inbox/threads?mode=LOCAL', None, a)
        self.assertEqual(len(threads['threads']), 1)
        tid = threads['threads'][0]['thread_id']
        self.assertEqual(self.req('GET', f'/api/inbox/threads/{tid}?mode=LOCAL', None, b)[0], 404, 'B cannot read A\'s thread')
        self.assertEqual(self.req('GET', '/api/inbox/threads?mode=LOCAL', None, b)[1]['threads'], [])
        st, dr = self.req('POST', f'/api/inbox/threads/{tid}/drafts?mode=LOCAL', {'facts': {'status': 'Sending it at 3 PM.'}}, a)
        self.assertEqual(st, 201, dr)
        st, ap = self.req('POST', f'/api/inbox/drafts/{dr["id"]}/approve?mode=LOCAL', {'digest': dr['digest'], 'operation': 'SEND_EMAIL'}, b)
        self.assertEqual(st, 404, 'B cannot approve A\'s draft')
        st, ap = self.req('POST', f'/api/inbox/drafts/{dr["id"]}/approve?mode=LOCAL', {'digest': dr['digest'], 'operation': 'SEND_EMAIL'}, a)
        self.assertEqual(st, 200, ap)
        st, sent = self.req('POST', f'/api/inbox/drafts/{dr["id"]}/send?mode=LOCAL', {'approval_id': ap['id'], 'digest': dr['digest']}, a)
        self.assertEqual((st, sent['state']), (200, 'VERIFIED'), sent)
        st, ex = self.req('GET', f'/api/inbox/executions/{sent["execution_id"]}?mode=LOCAL', None, a)
        self.assertEqual(ex['state'], 'VERIFIED')
        self.assertEqual(self.req('GET', f'/api/inbox/executions/{sent["execution_id"]}?mode=LOCAL', None, b)[0], 404)
        self.assertEqual(self.req('GET', '/api/inbox/threads?mode=LOCAL')[0], 401, 'no session, no inbox')

    def test_password_change_over_http(self):
        tok = self.register('pw@example.test')
        st, out = self.req('POST', '/api/auth/password', {'current_password': 'correct horse battery', 'new_password': 'another long password'}, tok)
        self.assertEqual(st, 200, out)
        self.assertEqual(self.req('GET', '/api/auth/me', None, tok)[0], 401, 'the old token is revoked')
        self.assertEqual(self.req('GET', '/api/auth/me', None, out['token'])[0], 200)


if __name__ == '__main__':
    unittest.main()
