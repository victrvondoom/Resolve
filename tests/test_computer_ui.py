"""Live computer panel (web/static/computer.js) in a real Chromium against a PRODUCTION-mode server with CSP enforced.

The panel posts to /api/computer/run, which drives a second, server-side Chromium against this tenant's own controlled
apps. Assertions compare what the panel shows with the tenant's app database read directly from disk, so a canned or
scripted UI would fail. Browser actions on both sides are DOM-level (Playwright), not physical cursor movement.
"""
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_production_security import ProdServer
from test_web_api_g1 import free_port
from cloud_browser import _chromium_executable

try:
    from playwright.sync_api import sync_playwright, expect
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

OVERFLOW_JS = 'document.scrollingElement.scrollWidth <= window.innerWidth + 1'
RUN_MS = 90000
UPDATE = "Update task 'Write launch notes' to done in Project Atlas"


@unittest.skipUnless(HAVE_PW, 'browser-e2e: playwright not installed')
class ComputerPanelE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = ProdServer(cls.tmp.name, free_port(), extra={'RESOLVE_DIAGNOSTICS': '1'}).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=_chromium_executable())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.stop(); cls.tmp.cleanup()

    def open_panel(self, width=1360, height=900):
        context = self.browser.new_context(viewport={'width': width, 'height': height})
        self.addCleanup(context.close)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('dialog', lambda d: d.accept())
        self.addCleanup(lambda: self.assertEqual(errors, []))
        page.goto(f'http://127.0.0.1:{self.srv.port}/app')
        page.wait_for_selector('#authDialog[open]'); page.click('#authDemo'); page.wait_for_selector('#authDialog', state='hidden')
        page.evaluate("location.hash = 'computer'")
        expect(page.locator('#view-computer')).to_be_visible()
        expect(page.locator('#cmpRun')).to_be_enabled()
        return page

    def db(self, page):
        """The tenant's controlled-app database, read straight from disk (the panel never sees this)."""
        uid = page.evaluate("""async () => { const t = sessionStorage.getItem('resolve_token');
            return (await (await fetch('/api/auth/me', {headers: {Authorization: 'Bearer ' + t}})).json()).user_id; }""")
        path = Path(self.tmp.name) / 'docs' / 'computer' / uid / 'apps.sqlite3'
        self.assertTrue(path.exists(), path)
        c = sqlite3.connect(path); c.row_factory = sqlite3.Row
        self.addCleanup(c.close)
        return c

    def task_row(self, page, title='Write launch notes'):
        r = self.db(page).execute('SELECT id, status, version FROM tasks WHERE title=?', (title,)).fetchone()
        return dict(r)

    def run_task(self, page, command, expect_status):
        page.fill('#cmpCommand', command)
        page.click('#cmpRun')
        expect(page.locator('#cmpStatus')).to_have_text(expect_status, timeout=RUN_MS)
        expect(page.locator('#cmpRun')).to_be_enabled(timeout=RUN_MS)

    def test_overdue_tasks_shows_the_real_rows_steps_and_frame(self):
        page = self.open_panel()
        self.assertEqual(page.locator('.sidebar a[data-view="computer"]').count(), 1)
        expect(page.locator('#cmpSession')).to_have_text('NO SESSION')
        self.run_task(page, 'Open Project Atlas and show my overdue tasks.', 'CONFIRMED')
        truth = sorted(r[0] for r in self.db(page).execute("SELECT title FROM tasks WHERE project='Atlas' AND due<'2026-10-30' AND status!='done'"))
        shown = sorted(page.locator('#cmpRows tbody tr td:first-child').all_inner_texts())
        self.assertEqual(shown, truth)
        self.assertEqual(len(truth), 3)
        expect(page.locator('#cmpTask')).to_have_text('overdue_tasks')
        expect(page.locator('#cmpVerified')).to_contain_text('application database')
        steps = page.locator('#cmpSteps li')
        self.assertGreaterEqual(steps.count(), 3)
        self.assertIn('browser.navigate', steps.first.inner_text())
        self.assertTrue(all(re.search(r'evidence tool_\d+ · args sha256 [0-9a-f]{12}', t) for t in steps.all_inner_texts()), steps.all_inner_texts())
        self.assertTrue(page.get_attribute('#cmpFrame', 'src').startswith('data:image/jpeg;base64,'))
        self.assertGreater(page.evaluate("document.getElementById('cmpFrame').naturalWidth"), 100)   # a real decoded screenshot
        expect(page.locator('#cmpFrameCap')).to_contain_text('/pm/?project=Atlas')
        # the server reports a one-shot session; the panel never claims LIVE
        expect(page.locator('#cmpSession')).to_have_text('ONE-SHOT RUN · NOT LIVE')

    def test_ambiguous_alex_asks_then_binds_the_chosen_contact(self):
        page = self.open_panel()
        self.run_task(page, 'Find Alex', 'NEEDS_CLARIFICATION')
        expect(page.locator('#cmpClarify')).to_contain_text('2 people named Alex')
        self.assertEqual(sorted(page.locator('#cmpClarify button').all_inner_texts()), ['Alex Chen · Sales', 'Alex Rivera · Finance'])   # Alexa Morgan is not an Alex
        expect(page.locator('#cmpTarget')).to_have_text('not resolved')
        page.click('#cmpClarify button:has-text("Finance")')
        expect(page.locator('#cmpStatus')).to_have_text('CONFIRMED', timeout=RUN_MS)
        expect(page.locator('#cmpCommand')).to_have_value('Find Alex from Finance')
        expect(page.locator('#cmpPerson')).to_contain_text('alex.rivera@example.test')
        expect(page.locator('#cmpTarget')).to_have_text('contact #1 · Alex Rivera (Finance)')

    def test_update_task_proposal_then_approve_is_confirmed_and_persisted(self):
        page = self.open_panel()
        self.run_task(page, UPDATE, 'AWAITING_APPROVAL')
        expect(page.locator('#cmpProposal')).to_be_visible()
        digest = page.locator('#cmpDigest').inner_text()
        self.assertRegex(digest, r'^[0-9a-f]{64}$')
        before = self.task_row(page)
        self.assertEqual((before['status'], before['version']), ('todo', 1), 'a proposal must not change anything')
        expect(page.locator('#cmpTarget')).to_have_text(f"task #{before['id']} · observed version 1 · controlled:pm")
        page.click('#cmpApprove')
        expect(page.locator('#cmpStatus')).to_have_text('CONFIRMED', timeout=RUN_MS)
        expect(page.locator('#cmpProposal')).to_be_hidden()
        after = self.task_row(page)
        self.assertEqual((after['status'], after['version']), ('done', 2))
        expect(page.locator('#cmpTarget')).to_have_text(f"task #{after['id']} · version 2 · status done (read from the app database)")
        expect(page.locator('#cmpVerified')).to_contain_text('Independent verifier agreed')
        expect(page.locator('#cmpEffect')).to_contain_text('done')
        self.assertIn('browser.select', page.locator('#cmpSteps').inner_text())

    def test_cancel_and_stale_approval_change_nothing(self):
        page = self.open_panel()
        # cancel: no approval is sent and the task is untouched
        self.run_task(page, UPDATE, 'AWAITING_APPROVAL')
        page.click('#cmpCancel')
        expect(page.locator('#cmpStatus')).to_have_text('CANCELLED')
        expect(page.locator('#cmpProposal')).to_be_hidden()
        self.assertEqual(self.task_row(page)['status'], 'todo')
        self.assertEqual(self.task_row(page)['version'], 1)
        # editing the command after a proposal disables approval of the old one
        self.run_task(page, UPDATE, 'AWAITING_APPROVAL')
        page.fill('#cmpCommand', UPDATE.replace('done', 'todo'))
        expect(page.locator('#cmpApprove')).to_be_disabled()
        expect(page.locator('#cmpPropStale')).to_be_visible()
        page.fill('#cmpCommand', UPDATE)
        expect(page.locator('#cmpApprove')).to_be_enabled()
        # the task changes elsewhere after the proposal: the panel's approval is refused and writes nothing
        other = page.evaluate("""async (cmd) => { const h = {Authorization: 'Bearer ' + sessionStorage.getItem('resolve_token'), 'Content-Type': 'application/json'};
            const post = (b) => fetch('/api/computer/run', {method: 'POST', headers: h, body: JSON.stringify(b)}).then(r => r.json());
            const p = await post({command: cmd}); return (await post({command: cmd, approved_digest: p.proposal.digest})).status; }""",
                              "Update task 'Write launch notes' to in progress in Project Atlas")
        self.assertEqual(other, 'CONFIRMED')
        self.assertEqual((self.task_row(page)['status'], self.task_row(page)['version']), ('in_progress', 2))
        page.click('#cmpApprove')
        expect(page.locator('#cmpStatus')).to_have_text('REFUSED', timeout=RUN_MS)
        expect(page.locator('#cmpError')).to_contain_text('APPROVAL_MISMATCH')
        expect(page.locator('#cmpVerified')).to_contain_text('nothing was written')
        self.assertEqual((self.task_row(page)['status'], self.task_row(page)['version']), ('in_progress', 2))

    def test_phone_width_has_no_horizontal_scroll(self):
        page = self.open_panel(390, 844)
        self.assertTrue(page.evaluate(OVERFLOW_JS))
        self.run_task(page, 'Open Project Atlas and show my overdue tasks.', 'CONFIRMED')
        self.assertTrue(page.evaluate(OVERFLOW_JS), 'overdue result overflows at 390px')
        self.run_task(page, UPDATE, 'AWAITING_APPROVAL')
        expect(page.locator('#cmpDigest')).to_be_visible()
        self.assertTrue(page.evaluate(OVERFLOW_JS), 'proposal digest overflows at 390px')


if __name__ == '__main__':
    unittest.main()
