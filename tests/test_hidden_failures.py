"""Regression tests for hidden failure modes found by auditing RESOLVE's own assumptions.

Each test states the assumption that was silently false before the fix.
"""
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import brief_mission as bm
from brief_mission import BriefMissionStore, MissionError
from document_studio import DocumentStudio, DocumentError
from test_brief_mission import INTENT, wait_state

ROOT = Path(__file__).resolve().parents[1]


class DeploymentAssumptions(unittest.TestCase):
    def test_server_imports_without_playwright(self):
        """Assumption: production image ships without a browser, yet the app must still start."""
        code = "import sys; sys.modules['playwright']=None; sys.modules['playwright.sync_api']=None; sys.path.insert(0,'src'); import resolve_app"
        r = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-600:])

    def test_weak_passwords_rejected_server_side(self):
        """Assumption: the UI's '10+ characters' rule is enforced by the server (it was 6)."""
        from tenant_auth import TenantAuthManager, TenantError
        with tempfile.TemporaryDirectory() as tmp:
            mgr = TenantAuthManager(db_path=Path(tmp) / 't.sqlite3') if 'db_path' in TenantAuthManager.__init__.__code__.co_varnames else TenantAuthManager()
            with self.assertRaises(TenantError):
                mgr.register('weak@resolve.test', '123456')


class VerifierFalseConfidence(unittest.TestCase):
    def test_issue_ids_are_not_matched_by_prefix(self):
        """Assumption: 'ISS-1 present' means issue 1 is in the PDF. It also matched ISS-10..ISS-19 and 'auth.py:10' for 'auth.py:1'."""
        with tempfile.TemporaryDirectory() as tmp:
            studio = DocumentStudio(Path(tmp) / 'docs')
            issues = [{'id': f'ISS-{n}', 'severity': 'LOW', 'rule': 'R', 'category': 'testing', 'snippet': 's', 'requirement': None, 'fix': 'f',
                       'file': 'auth.py', 'line': n, 'citation': f'auth.py:{n}'} for n in range(1, 13)]
            src_sha = 'a' * 64
            plan = {'title': 'T', 'status': 'DRAFT', 'method': 'm', 'intent': {'revision': 1, 'text': ''}, 'source': {'document_id': 'd', 'name': 'n', 'sha256': src_sha},
                    'project': {'name': 'p', 'id': None, 'sha256': 'b' * 64, 'files_scanned': 1, 'languages': {}, 'findings_total': 12, 'findings_omitted': 0},
                    'requirements': [], 'issues': issues, 'architecture': {'nodes': [{'id': 'a', 'label': 'a', 'kind': 'current'}], 'edges': []},
                    'mermaid': 'flowchart TD\n    a["a"]', 'tasks': [], 'test_strategy': []}
            plan['digest'] = bm.plan_digest(plan)
            missing_first = {**plan, 'issues': issues[1:]}          # PDF that silently omits ISS-1
            pdf = bm.compose_pdf(missing_first, {'id': 'bm_x', 'intent_revision': 1, 'intent_text': 'x'})
            doc = studio.save_generated('u', 'w', pdf, 'x.pdf')
            res = bm.verify_output(studio, 'u', 'w', doc['id'], plan, src_sha, 'b' * 64)
            self.assertFalse(res['checks']['contains_every_issue'])
            self.assertFalse(res['passed'])


class KnownCredentials(unittest.TestCase):
    def test_no_known_default_password_for_demo_account(self):
        """Assumption: no account ships with a guessable password. 'demo@resolve.local'/'demo123' was seeded into every database, production included."""
        from tenant_auth import TenantAuthManager, TenantError
        import inspect
        sig = inspect.signature(TenantAuthManager.__init__)
        with tempfile.TemporaryDirectory() as tmp:
            kw = {'db_path': Path(tmp) / 't.sqlite3'} if 'db_path' in sig.parameters else {}
            mgr = TenantAuthManager(**kw)
            mgr.seed_demo_tenant()                      # local-demo seeding
            with self.assertRaises(TenantError):
                mgr.login('demo@resolve.local', 'demo123')
            mgr.seed_demo_tenant(create_session=False)  # production path: also rotates any pre-existing account
            with self.assertRaises(TenantError):
                mgr.login('demo@resolve.local', 'demo123')


class PasswordStorage(unittest.TestCase):
    def test_new_hashes_use_current_work_factor_and_legacy_hashes_still_verify(self):
        """Assumption: stored hashes are strong and upgrades don't lock out existing users."""
        import hashlib
        import tenant_auth as ta
        h, salt = ta.hash_password('correct horse battery')
        self.assertTrue(h.startswith(f'{ta.PBKDF2_ITERATIONS}$'))
        self.assertTrue(ta.verify_password('correct horse battery', h, salt))
        self.assertFalse(ta.verify_password('wrong password!!', h, salt))
        legacy = hashlib.pbkdf2_hmac('sha256', b'old-password-1', bytes.fromhex(salt), 100_000).hex()   # pre-upgrade format
        self.assertTrue(ta.verify_password('old-password-1', legacy, salt))


class StateMachineRaces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = DocumentStudio(Path(self.tmp.name) / 'docs')
        self.hooks = {}
        self.store = BriefMissionStore(Path(self.tmp.name) / 'm.sqlite3', self.studio, hooks=self.hooks)
        self.brief = self.store.seed_sample_brief('u', 'w')
        self.gates = []

    def tearDown(self):
        for g in self.gates:
            g.set()
        self.store.close()
        self.tmp.cleanup()

    def start(self):
        return self.store.start('u', 'w', INTENT, document_id=self.brief['id'])

    def test_concurrent_approvals_execute_exactly_once(self):
        """Assumption: check-then-act on state is atomic. Two simultaneous approvals both passed the check."""
        m = wait_state(self.store, self.start()['id'], 'u', 'w', {'AWAITING_APPROVAL'})
        results, barrier = [], threading.Barrier(8)

        def go():
            barrier.wait()
            try:
                self.store.approve(m['id'], 'u', 'w', m['plan']['digest'])
                results.append('ok')
            except MissionError as e:
                results.append(e.status_code)
        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(results.count('ok'), 1, results)
        wait_state(self.store, m['id'], 'u', 'w', {'CONFIRMED', 'FAILED'})
        self.assertEqual(len([d for d in self.studio.list_documents('u', 'w') if d['operation'] == 'brief_report']), 1)

    def test_cancel_is_never_overwritten_by_a_finishing_worker(self):
        """Assumption: a worker that passed its liveness check cannot clobber a cancel that lands just after it."""
        gate = threading.Event(); self.gates.append(gate)
        orig_alive = self.store._alive
        calls = {'n': 0}

        def racing_alive(mid):
            r = orig_alive(mid)
            calls['n'] += 1
            if calls['n'] == 4 and r is not None:      # run_analysis(1), reader(2), analyzer(3), planner post-check(4) = just before the plan commit
                self.store.cancel(mid, 'u', 'w')       # cancel lands inside the check-then-write window
            return r
        self.store._alive = racing_alive
        m = self.start()
        time.sleep(1.5)
        self.assertEqual(self.store.get(m['id'], 'u', 'w')['state'], 'CANCELLED')

    def test_hung_worker_fails_mission_instead_of_hanging_forever(self):
        """Assumption: node timeouts are enforced. They were only checked after a node returned, so a hang meant RUNNING forever."""
        gate = threading.Event(); self.gates.append(gate)
        self.hooks['REPOSITORY_ANALYZER'] = lambda: gate.wait(20)
        old = bm.NODE_TIMEOUT_S
        bm.NODE_TIMEOUT_S = 0.6
        try:
            m = wait_state(self.store, self.start()['id'], 'u', 'w', {'FAILED', 'AWAITING_APPROVAL'}, timeout=6)
        finally:
            bm.NODE_TIMEOUT_S = old
        self.assertEqual(m['state'], 'FAILED')
        self.assertRegex(m['error'], r'timeout|exceeded')

    def test_oversized_brief_is_refused_before_parsing(self):
        """Assumption: PDF text extraction is bounded. A many-page PDF would be parsed in full."""
        old = DocumentStudio.MAX_TEXT_PAGES
        DocumentStudio.MAX_TEXT_PAGES = 2
        try:
            docs = [self.brief, self.store.seed_sample_brief('u', 'w'), self.store.seed_sample_brief('u', 'w')]
            three = self.studio.transform('u', 'w', 'merge', [d['id'] for d in docs], {d['id']: d['sha256'] for d in docs})   # 3 pages > cap of 2
            with self.assertRaises(DocumentError):
                self.studio.extract_text('u', 'w', three['id'])
        finally:
            DocumentStudio.MAX_TEXT_PAGES = old


class ArchitectureIdentifiers(unittest.TestCase):
    def test_module_names_cannot_collide_with_proposed_nodes_or_break_mermaid(self):
        """Assumption: module file names are valid, unique Mermaid ids. 'config'/'tests' collided with proposed nodes; 'my-mod' broke syntax."""
        analysis = {'modules': ['config', 'tests', 'my-mod', 'app'], 'imports': {'app': ['config', 'my-mod']}, 'findings': [], 'files_scanned': 4}
        issues = [{'category': c, 'file': f} for c, f in (('secrets', 'config.py'), ('validation', 'my-mod.py'), ('error-handling', 'app.py'), ('testing', 'tests.py'))]
        g = bm.build_architecture({**analysis}, issues)
        ids = [n['id'] for n in g['nodes']]
        self.assertEqual(len(ids), len(set(ids)), ids)
        bm.validate_mermaid(bm.graph_to_mermaid(g))


class ClientIdentity(unittest.TestCase):
    def test_rate_limit_key_uses_forwarded_client_only_behind_trusted_proxy(self):
        """Assumption: socket address identifies the client. Behind a platform proxy every user shared one address (one attacker locks out all logins)."""
        from server_security import client_ip
        self.assertEqual(client_ip('10.0.0.1', '203.0.113.9, 198.51.100.7', trusted_proxy=False), '10.0.0.1')   # spoofable header ignored
        self.assertEqual(client_ip('10.0.0.1', '1.2.3.4, 198.51.100.7', trusted_proxy=True), '198.51.100.7')     # right-most = added by our proxy
        self.assertEqual(client_ip('10.0.0.1', '', trusted_proxy=True), '10.0.0.1')

    def test_rate_limiter_memory_is_bounded(self):
        from server_security import RateLimiter
        rl = RateLimiter(max_keys=100)
        for i in range(1000):
            rl.allow('auth', f'ip{i}', 5)
        self.assertLessEqual(len(rl._hits), 100)


if __name__ == '__main__':
    unittest.main()
