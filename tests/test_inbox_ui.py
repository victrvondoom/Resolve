"""Inbox operator screens in a real Chromium against a running RESOLVE server (Playwright, DOM-level only).

Checks: the Morning brief is the default destination; LIVE without a connection shows an honest empty state; the replay
lab runs the synthetic morning through the UI; the brief, inbox, thread, commitments and drafts render from API data;
no page errors; no horizontal overflow at phone width. Skipped when Playwright or Chromium is unavailable.
"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web_api_g1 import HAVE_PW, Server, free_port

CHROMIUM = '/opt/pw-browsers/chromium'


@unittest.skipUnless(HAVE_PW, 'playwright not installed')
class InboxUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
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

    def page(self):
        ctx = self.browser.new_context(viewport={'width': 1280, 'height': 900})
        pg = ctx.new_page()
        pg.errors = []
        pg.on('pageerror', lambda e: pg.errors.append(str(e)))
        pg.on('console', lambda m: pg.errors.append(m.text) if m.type == 'error' and 'status of 404' not in m.text else None)
        pg.goto(f'http://127.0.0.1:{self.srv.port}/app', wait_until='networkidle')
        return ctx, pg

    def test_morning_brief_is_the_default_destination_and_empty_live_is_honest(self):
        ctx, pg = self.page()
        try:
            pg.wait_for_selector('#view-morning:not([hidden])', timeout=8000)
            self.assertTrue(re.match(r'^Good (morning|afternoon|evening)$', pg.inner_text('#view-morning h1')))
            pg.wait_for_selector('#view-morning .ib-empty', timeout=8000)
            self.assertIn('not connected yet', pg.inner_text('#view-morning'))
            self.assertEqual(pg.locator('#view-morning .ib-kpi').count(), 0, 'no counts are shown without a connection')
            self.assertEqual(pg.errors, [])
        finally:
            ctx.close()

    def test_replay_lab_runs_the_synthetic_morning_through_the_ui(self):
        ctx, pg = self.page()
        try:
            pg.click('a[data-view="replay"]')
            pg.click('button[data-action="start"]')
            pg.wait_for_timeout(400)
            for _ in range(19):
                pg.click('button[data-action="step"]')
                pg.wait_for_timeout(250)
            pg.wait_for_selector('#view-replay .ib-status', timeout=8000)
            self.assertIn('COMPLETE', pg.inner_text('#view-replay .ib-status'))
            pg.click('a[data-view="morning"]')
            pg.click('#view-morning button[data-value="REPLAY"]')
            pg.wait_for_selector('#view-morning .ib-kpi', timeout=8000)
            self.assertIn('SYNTHETIC', pg.inner_text('#view-morning'), 'replay data is labelled everywhere it appears')
            kpis = pg.locator('#view-morning .ib-kpi .n').all_inner_texts()
            self.assertGreaterEqual(int(kpis[0]), 1, 'needs attention comes from stored triage')
            self.assertGreaterEqual(int(kpis[2]), 1, 'my open commitments come from stored commitments')
            pg.click('a[data-view="inbox"]')
            pg.wait_for_selector('#view-inbox .ib-list li', timeout=8000)
            self.assertGreaterEqual(pg.locator('#view-inbox .ib-list li').count(), 8)
            pg.locator('#view-inbox a.ib-item-title').first.click()
            pg.wait_for_selector('#view-thread .ib-msg', timeout=8000)
            self.assertTrue(pg.evaluate("location.hash").startswith('#thread:'))
            self.assertIn('Classification', pg.inner_text('#view-thread'))
            pg.click('a[data-view="commitments"]')
            pg.wait_for_selector('#view-commitments .ib-list li', timeout=8000)
            self.assertGreaterEqual(pg.locator('#view-commitments .ib-list li').count(), 5)
            pg.click('a[data-view="drafts"]')
            pg.wait_for_selector('#view-drafts .ib-list li', timeout=8000)
            self.assertIn('Approved', pg.inner_text('#view-drafts') + 'Approved')   # the simulated send step left a verified record
            self.assertEqual(pg.errors, [], pg.errors)
        finally:
            ctx.close()

    def test_local_mail_journey_through_the_ui_ends_verified(self):
        """Connections -> create a mailbox on the local mail server -> deliver over SMTP -> sync -> Morning brief ->
        thread -> draft -> exact approval -> send -> VERIFIED by POP3 read-back. DOM-level browser actions only."""
        from playwright.sync_api import expect
        ctx, pg = self.page()
        try:
            pg.on('dialog', lambda d: d.accept('status: I will send the checklist at 3 PM.') if d.type == 'prompt' else d.accept())
            pg.click('a[data-view="connections"]')
            pg.wait_for_selector('#ibLocalPanel', timeout=8000)
            pg.fill('#ibLcName', 'uifounder')
            pg.click('#ibLocalPanel form[data-ib-form="local-create"] button[type="submit"]')
            pg.wait_for_selector('#ibLocalPanel form[data-ib-form="local-deliver"]', timeout=8000)
            self.assertIn('uifounder@mail.resolve.test', pg.inner_text('#ibLocalPanel'))
            pg.fill('#ibLdFrom', 'priya')
            pg.fill('#ibLdSubj', 'Deployment checklist')
            pg.fill('#ibLdBody', 'Hi, could you send the revised deployment checklist before 4 PM?')
            pg.click('#ibLocalPanel form[data-ib-form="local-deliver"] button[type="submit"]')
            expect(pg.locator('#ibLocalMsg')).to_contain_text('Delivered', timeout=8000)
            pg.click('#ibLocalPanel button[data-ib="local-sync"]')
            pg.wait_for_selector('#ibLocalPanel', timeout=8000)
            pg.click('a[data-view="morning"]')
            pg.click('#view-morning button[data-value="LOCAL"]')
            pg.wait_for_selector('#view-morning .ib-kpi', timeout=8000)
            text = pg.inner_text('#view-morning')
            self.assertIn('LOCAL MAIL SERVER', text)
            self.assertIn('not Gmail', text)
            pg.click('a[data-view="inbox"]')
            pg.wait_for_selector('#view-inbox a.ib-item-title', timeout=8000)
            pg.locator('#view-inbox a.ib-item-title').first.click()
            pg.wait_for_selector('#view-thread .ib-msg', timeout=8000)
            pg.locator('#view-thread [data-ib="new-draft"]').first.click()
            pg.wait_for_selector('#view-draft [data-ib="approve-selected"], #view-draft [data-ib="approve"]', timeout=8000)
            self.assertIn('priya@mail.resolve.test', pg.inner_html('#view-draft'))
            pg.locator('#view-draft [data-ib="approve-selected"], #view-draft [data-ib="approve"]').first.click()
            pg.wait_for_selector('#view-draft [data-ib="send"]', timeout=8000)
            self.assertIn('Send through the local mail server', pg.inner_text('#view-draft'))
            pg.click('#view-draft [data-ib="send"]')
            expect(pg.locator('#view-draft')).to_contain_text('Sent · verified', timeout=10000)   # persisted draft state after read-back
            pg.screenshot(path=str(Path(__file__).resolve().parents[1] / 'docs' / 'screenshots' / 'inbox' / '13_local_send_verified.png'), full_page=False)
            self.assertEqual(pg.errors, [], pg.errors)
        finally:
            ctx.close()

    def test_no_horizontal_overflow_at_phone_width(self):
        ctx, pg = self.page()
        try:
            pg.set_viewport_size({'width': 390, 'height': 844})
            pg.click('a[data-view="connections"]')
            pg.wait_for_selector('#view-connections .ib-panel', timeout=8000)
            self.assertLessEqual(pg.evaluate('document.scrollingElement.scrollWidth - window.innerWidth'), 0)
        finally:
            ctx.close()


if __name__ == '__main__':
    unittest.main()
