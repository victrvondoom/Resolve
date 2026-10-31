"""Local mode: the app must open and work with NO Google credentials and NO internet.

A real server is started with every GOOGLE_* / model variable removed. A real Chromium loads /app with every request to a
non-loopback host aborted (and recorded). The test fails if anything external is requested, if the page logs an error,
if Gmail is shown as connected, or if the local features do not work. Static assets are also scanned for remote URLs."""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web_api_g1 import HAVE_PW, Server, free_port
from ui_nav import click_nav
if HAVE_PW:
    from playwright.sync_api import expect

ROOT = Path(__file__).resolve().parents[1]
CHROMIUM = '/opt/pw-browsers/chromium'
WEB = ROOT / 'web'


class StaticAssetsAreLocal(unittest.TestCase):
    def test_no_html_css_or_js_loads_a_remote_asset(self):
        remote = re.compile(r'''(?:src|href)\s*=\s*["'](https?:)?//(?!127\.0\.0\.1|localhost)[^"']+["']|url\(\s*["']?(https?:)?//|@import\s+["']?(https?:)?//''')
        offenders = []
        for f in [*WEB.glob('*.html'), *WEB.glob('static/*.css'), *WEB.glob('static/*.js')]:
            for i, line in enumerate(f.read_text(errors='ignore').splitlines(), 1):
                if remote.search(line):
                    offenders.append(f'{f.relative_to(ROOT)}:{i}')
        self.assertEqual(offenders, [], 'essential UI assets must load locally (no CDN, remote font or script)')


@unittest.skipUnless(HAVE_PW, 'playwright not installed')
class LocalModeWithoutGoogleOrInternet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls._saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith('GOOGLE_') or k in ('RESOLVE_MODEL_BASE_URL', 'RESOLVE_MODEL_NAME', 'RESOLVE_MASTER_KEY')}
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = Server(cls.tmp.name, free_port()).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=CHROMIUM, args=['--no-sandbox']) if Path(CHROMIUM).exists() else cls.pw.chromium.launch(args=['--no-sandbox'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.srv.stop()
        cls.tmp.cleanup()
        os.environ.update(cls._saved)

    def offline_page(self):
        ctx = self.browser.new_context(viewport={'width': 1280, 'height': 900})
        pg = ctx.new_page()
        pg.external, pg.errors = [], []

        def gate(route):
            u = route.request.url
            if u.startswith(('http://127.0.0.1', 'data:', 'blob:')):
                route.continue_()
            else:
                pg.external.append(u)
                route.abort()
        pg.route('**/*', gate)
        pg.on('pageerror', lambda e: pg.errors.append(str(e)))
        pg.on('console', lambda m: pg.errors.append(m.text) if m.type == 'error' else None)
        pg.goto(f'http://127.0.0.1:{self.srv.port}/app', wait_until='networkidle')
        return ctx, pg

    def test_app_opens_with_the_workbench_and_makes_no_external_request(self):
        ctx, pg = self.offline_page()
        try:
            pg.wait_for_selector('#view-workbench:not([hidden])', timeout=10000)
            pg.wait_for_selector('#wbBody .wb-tile', timeout=10000)
            self.assertEqual(pg.external, [], 'nothing outside this computer may be requested while loading')
            self.assertEqual(pg.errors, [])
        finally:
            ctx.close()

    def test_google_is_not_configured_yet_the_inbox_works_locally(self):
        ctx, pg = self.offline_page()
        try:
            pg.click('[data-area-btn="operations"]')
            from playwright.sync_api import expect
            expect(pg.locator('#view-morning .ib-status')).to_contain_text('Local mail', timeout=10000)
            expect(pg.locator('#view-morning .ib-kpi').first).to_be_visible(timeout=10000)
            body = pg.inner_text('#view-morning')
            self.assertNotIn('Sign in with Google', body)
            self.assertNotIn('Gmail ·', body)
            click_nav(pg, 'connections')
            expect(pg.locator('#view-connections')).to_contain_text('GMAIL NOT CONFIGURED', timeout=10000)
            expect(pg.locator('#view-connections')).to_contain_text('CONNECTED', timeout=10000)   # the local mailbox
            self.assertEqual(pg.external, [])
            self.assertEqual(pg.errors, [])
        finally:
            ctx.close()

    def test_a_local_message_can_be_delivered_and_synced_offline(self):
        ctx, pg = self.offline_page()
        try:
            from playwright.sync_api import expect
            click_nav(pg, 'connections')
            panel = pg.locator('#ibLocalPanel')
            expect(panel.locator('form[data-ib-form="local-deliver"]')).to_be_visible(timeout=10000)
            pg.fill('#ibLdFrom', 'priya')
            pg.fill('#ibLdSubj', 'Offline check')
            pg.fill('#ibLdBody', 'Hi, could you send the revised checklist before 4 PM?')
            pg.click('#ibLocalPanel form[data-ib-form="local-deliver"] button[type="submit"]')
            expect(pg.locator('#ibLocalMsg')).to_contain_text('Delivered', timeout=10000)
            pg.click('#ibLocalPanel button[data-ib="local-sync"]')
            click_nav(pg, 'inbox')
            expect(pg.locator('#view-inbox')).to_contain_text('Offline check', timeout=15000)
            self.assertEqual(pg.external, [], 'a full local delivery and sync needs no internet')
        finally:
            ctx.close()

    def test_workbench_is_the_default_and_reads_real_server_state(self):
        ctx, pg = self.offline_page()
        try:
            pg.wait_for_selector('#view-workbench:not([hidden])', timeout=8000)
            self.assertEqual(pg.inner_text('#view-workbench h1'), 'Advanced Tools')
            pg.wait_for_selector('#wbBody .wb-tile', timeout=8000)
            self.assertGreaterEqual(pg.locator('#wbBody .wb-tile').count(), 10)
            # the registry table is the server's real /api/tools list, not a canned one
            n_ui = pg.locator('#wbBody .wb-table tbody tr').count()
            n_api = pg.evaluate("api('/api/tools').then(r => r.tools.length)")
            self.assertEqual(n_ui, n_api)
            self.assertGreater(n_api, 0)
            # Windows-only capability is reported honestly on this (non-Windows) host
            self.assertIn('not on this host', pg.locator('.wb-tile[data-wb-go="windows"]').inner_text())
            self.assertEqual(pg.external, [])
            self.assertEqual(pg.errors, [])
        finally:
            ctx.close()

    def test_operations_works_without_google_and_never_shows_gmail_as_connected(self):
        ctx, pg = self.offline_page()
        try:
            pg.click('[data-area-btn="operations"]')
            pg.wait_for_selector('#view-morning:not([hidden])', timeout=8000)
            expect(pg.locator('#view-morning h1')).to_have_text(re.compile(r'^Good (morning|afternoon|evening)$'), timeout=8000)
            # first visit: the local mail server is used automatically (no Google, no internet)
            expect(pg.locator('#view-morning .ib-status')).to_contain_text('Local mail', timeout=8000)
            expect(pg.locator('#view-morning .ib-kpi').first).to_be_visible(timeout=8000)
            text = pg.inner_text('#view-morning')
            self.assertNotIn('Gmail ·', text, 'Gmail is never shown as connected when it is not')
            counts = pg.locator('#view-morning .ib-kpi .n').all_inner_texts()
            self.assertEqual(len(counts), 6)
            self.assertTrue(all(c.isdigit() for c in counts), counts)   # counts come from stored records (earlier tests may have delivered mail)
            # choosing Gmail explicitly is honest about there being no Gmail connection
            pg.click('#view-morning button[data-value="LIVE"]')
            expect(pg.locator('#view-morning')).to_contain_text('No inbox connected', timeout=8000)
            expect(pg.locator('#view-morning .ib-status')).to_contain_text('No Gmail connected', timeout=8000)
            self.assertEqual(pg.locator('#view-morning .ib-kpi').count(), 0, 'no counts are shown without a connection')
            self.assertEqual(pg.external, [])
            self.assertEqual(pg.errors, [])
        finally:
            ctx.close()

    def test_outbox_is_reachable_and_empty_state_is_truthful(self):
        ctx, pg = self.offline_page()
        try:
            click_nav(pg, 'outbox')
            expect(pg.locator('#view-outbox')).to_be_visible(timeout=8000)
            expect(pg.locator('[data-ib-body="outbox"]')).to_contain_text('Nothing in the outbox', timeout=8000)
            self.assertEqual(pg.external, [])
            self.assertEqual(pg.errors, [])
        finally:
            ctx.close()
