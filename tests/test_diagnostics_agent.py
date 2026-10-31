"""Diagnostics agent against the controlled demo web app, driven by a REAL Chromium (no mocks)."""
import hashlib
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cloud_browser import CloudBrowserSession
from demo_webapp import serve_demo
from diagnostics_agent import run_diagnostics
from net_policy import NetPolicy, PolicyViolation, origin_of

ERROR_KINDS = ('console_error', 'page_error', 'http_error', 'failed_request', 'blocked_request')


class DiagnosticsAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.base = serve_demo()
        cls.policy = NetPolicy(allowed_origins=frozenset({origin_of(cls.base)}))

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def kinds(self, result):
        return {f.kind for f in result.findings}

    def metadata_hits(self):
        return [p for _, p in self.server.request_log if '169.254' in p]

    def test_faulty_app_yields_genuine_findings(self):
        r = run_diagnostics(self.base + '/demo-app/', policy=self.policy)
        k = self.kinds(r)
        self.assertIn('page_error', k)
        self.assertIn('http_error', k)
        self.assertIn('blocked_request', k)
        self.assertIn('initInventoryTable', ' '.join(f.message for f in r.findings))
        statuses = {f.status for f in r.findings if f.kind == 'http_error'}
        self.assertTrue({500, 404} <= statuses, statuses)
        self.assertEqual(self.metadata_hits(), [])

    def test_frames_are_real_and_differ(self):
        r = run_diagnostics(self.base + '/demo-app/', policy=self.policy)
        self.assertGreaterEqual(len(r.frames), 4)
        for f in r.frames:
            self.assertTrue(f.jpeg.startswith(b'\xff\xd8'))
        self.assertGreater(len({hashlib.sha256(f.jpeg).hexdigest() for f in r.frames}), 1)

    def test_download_is_captured_with_hash(self):
        r = run_diagnostics(self.base + '/demo-app/', policy=self.policy)
        self.assertEqual(len(r.downloads), 1)
        d = r.downloads[0]
        self.assertEqual(d['name'], 'report.csv')
        self.assertEqual(d['sha256'], hashlib.sha256(d['bytes']).hexdigest())

    def test_fixed_app_has_no_error_findings(self):
        r = run_diagnostics(self.base + '/demo-app/fixed/', policy=self.policy)
        bad = [(f.kind, f.message) for f in r.findings if f.kind in ERROR_KINDS]
        self.assertEqual(bad, [])

    def test_popup_is_reported(self):
        r = run_diagnostics(self.base + '/demo-app/popup.html', policy=self.policy)
        self.assertIn('popup', self.kinds(r))

    def test_on_step_streams_every_step_with_frames(self):
        seen = []
        r = run_diagnostics(self.base + '/demo-app/', policy=self.policy, on_step=seen.append)
        self.assertEqual(len(seen), len(r.steps))
        self.assertTrue(any(s['frame'] is not None for s in seen))

    def test_abort_stops_at_a_boundary(self):
        calls = {'n': 0}

        def go():
            calls['n'] += 1
            return calls['n'] <= 2
        r = run_diagnostics(self.base + '/demo-app/', policy=self.policy, should_continue=go)
        self.assertTrue(r.aborted)
        self.assertLessEqual(len(r.steps), 2)

    def test_timeout_is_bounded_on_slow_page(self):
        t = time.monotonic()
        r = run_diagnostics(self.base + '/demo-app/api/slow', policy=self.policy, timeout_s=1.5)
        self.assertLess(time.monotonic() - t, 12)
        self.assertIn('navigation_timeout', self.kinds(r))

    def test_injection_text_is_data_only(self):
        r = run_diagnostics(self.base + '/demo-app/injection.html', policy=self.policy)
        self.assertIn('ignore previous instructions', r.page_text_excerpt)
        self.assertIn('blocked_request', self.kinds(r))
        self.assertEqual(self.metadata_hits(), [])
        self.assertEqual(r.downloads, [])

    def test_unlisted_origin_cannot_be_navigated(self):
        other = NetPolicy(allowed_origins=frozenset({'http://127.0.0.1:1'}))
        before = len(self.server.request_log)
        r = run_diagnostics(self.base + '/demo-app/', policy=other)
        self.assertEqual(r.frames, [])
        self.assertEqual(len(self.server.request_log), before)

    def test_page_closed_mid_run_reports_crash(self):
        holder = {}
        orig = CloudBrowserSession.__init__

        def spy(self_, *a, **kw):
            orig(self_, *a, **kw)
            holder['s'] = self_

        def kill(step):
            if step['n'] == 2:
                holder['s']._on_worker(holder['s']._page.close)   # Playwright objects are bound to the session's worker thread
        CloudBrowserSession.__init__ = spy
        try:
            r = run_diagnostics(self.base + '/demo-app/', policy=self.policy, on_step=kill)
        finally:
            CloudBrowserSession.__init__ = orig
        self.assertIn('browser_crash', self.kinds(r))


class PolicyOriginTests(unittest.TestCase):
    def test_userinfo_rejected(self):
        p = NetPolicy(allowed_origins=frozenset({'http://127.0.0.1:9000'}))
        with self.assertRaises(PolicyViolation):
            p.check_url('http://user:pw@127.0.0.1:9000/')

    def test_wrong_port_scheme_host_rejected(self):
        p = NetPolicy(allowed_origins=frozenset({'http://127.0.0.1:9000'}))
        for bad in ('http://127.0.0.1:9001/', 'https://127.0.0.1:9000/', 'http://localhost:9000/'):
            with self.subTest(url=bad), self.assertRaises(PolicyViolation):
                p.check_url(bad)

    def test_exact_origin_allowed(self):
        p = NetPolicy(allowed_origins=frozenset({'http://127.0.0.1:9000'}))
        self.assertEqual(p.check_url('http://127.0.0.1:9000/demo-app/'), 'http://127.0.0.1:9000/demo-app/')


if __name__ == '__main__':
    unittest.main()
