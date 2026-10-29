"""UX contract tests in a real Chromium against a PRODUCTION-mode server with CSP enforced.

They pin the behaviour users and judges see: sign-in validation, the account menu and real workspace switcher,
the progress stepper and worker pipeline, the compact mobile layout, the phone PDF fallback, truthful privacy data,
workspace lifecycle and per-document deletion. DOM-level checks only; no native input.
"""
import re
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


@unittest.skipUnless(HAVE_PW, 'browser-e2e: playwright not installed')
class UxE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = ProdServer(cls.tmp.name, free_port()).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=_chromium_executable())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.stop(); cls.tmp.cleanup()

    def page(self, **ctx):
        context = self.browser.new_context(**{'viewport': {'width': 1360, 'height': 900}, **ctx})
        self.addCleanup(context.close)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
        page.on('dialog', lambda d: d.accept('Client B') if d.type == 'prompt' else d.accept())
        page.goto(f'http://127.0.0.1:{self.srv.port}/app')
        return page, errors

    def demo_account(self, page):
        page.wait_for_selector('#authDialog[open]')
        page.click('#authDemo')
        page.wait_for_selector('#authDialog', state='hidden')

    def run_to_plan(self, page):
        page.click('#fmSample'); expect(page.locator('#fmDoc')).not_to_have_value('')
        expect(page.locator('#fmStart')).to_be_enabled()
        page.click('#fmStart')
        page.wait_for_selector('#fmPlanCard:not([hidden])', timeout=30000)

    def test_sign_in_account_menu_stepper_and_flow(self):
        page, errors = self.page(permissions=['clipboard-read', 'clipboard-write'])
        page.wait_for_selector('#authDialog[open]')
        expect(page.locator('#authDemo')).to_be_focused()
        page.fill('#authEmail', 'not-an-email'); page.click('#authLogin')
        expect(page.locator('#authMsg')).to_contain_text('valid email')
        page.fill('#authEmail', 'someone@example.com'); page.fill('#authPassword', 'short'); page.press('#authPassword', 'Enter')
        expect(page.locator('#authMsg')).to_contain_text('10 characters')
        page.click('#authDemo'); page.wait_for_selector('#authDialog', state='hidden')
        # account pill and the user's own workspaces (not the concept catalog)
        expect(page.locator('#userEmailBadge')).to_contain_text('@resolve.demo')
        expect(page.locator('#userAvatar')).to_have_text('D')
        expect(page.locator('#workspaceSwitcher')).to_be_visible()
        self.assertEqual(page.locator('#workspaceSwitcher option').all_inner_texts(), ["Demo's Workspace"])
        page.click('#userAccountPill')
        expect(page.locator('#accountMenu')).to_be_visible()
        self.assertEqual(page.get_attribute('#userAccountPill', 'aria-expanded'), 'true')
        page.keyboard.press('Escape')
        expect(page.locator('#accountMenu')).to_be_hidden()
        # production navigation offers no dead or simulated screens; footer reflects the mode
        for dead in ('mission', 'connectors', 'files', 'cloud', 'ledger'):
            self.assertEqual(page.locator(f'.sidebar a[data-view="{dead}"]').count(), 0, dead)
        expect(page.locator('#appFooter')).to_contain_text('private to your account')
        # before a mission: step 1 current, full pipeline visible, Start explains why it is disabled
        expect(page.locator('#fmSteps li[data-step="1"]')).to_have_class('current')
        expect(page.locator('#fmNodes li')).to_have_count(6)
        expect(page.locator('#fmNodes li .chip').first).to_have_text('NOT STARTED')
        expect(page.locator('#fmStart')).to_be_disabled()
        self.assertIn('sample brief', page.get_attribute('#fmStart', 'title'))
        self.run_to_plan(page)
        expect(page.locator('#fmSteps li[data-step="3"]')).to_have_class('current')
        expect(page.locator('#fmSteps li.done')).to_have_count(2)
        expect(page.locator('#fmOverlap')).to_contain_text('Measured')
        expect(page.locator('#fmSeverity')).to_contain_text('HIGH')
        page.locator('#fmIssues details').first.locator('summary').click()
        expect(page.locator('#fmIssues details').first).to_contain_text('Fix')
        page.click('#fmApprove')
        expect(page.locator('#fmHash')).to_have_text(re.compile('^[0-9a-f]{64}$'), timeout=30000)
        expect(page.locator('#fmSteps li.done')).to_have_count(4)
        expect(page.locator('#fmChecks')).to_contain_text('Cites the bound brief hash')
        expect(page.locator('#fmProv')).to_contain_text('Approved digest')
        page.click('#fmCopyHash')
        expect(page.locator('#fmCopyHash')).to_have_text(re.compile('Copied|Press Ctrl\\+C'))
        expect(page.locator('#fmHistory button')).to_have_count(1)
        # sign out returns to the sign-in gate
        page.click('#userAccountPill'); page.click('#signOutBtn')
        page.wait_for_selector('#authDialog[open]')
        self.assertEqual(errors, [])

    def test_mobile_layout_and_phone_pdf_fallback(self):
        page, errors = self.page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
        page.add_init_script("Object.defineProperty(Navigator.prototype, 'pdfViewerEnabled', {get: () => false})")
        page.reload()
        self.demo_account(page)
        self.assertLess(page.locator('.topbar').bounding_box()['height'], 80)        # compact header on phones
        self.run_to_plan(page)
        # measured in one synchronous read: the page may still be smooth-scrolling to the plan card
        geo = page.evaluate("""() => { const tr = document.querySelector('#fmIssues tr'), r = (sel) => tr.querySelector(sel).getBoundingClientRect();
          const id = r('td[data-label="ID"]'), sev = r('td[data-label="Severity"]');
          return {dy: Math.abs(id.y - sev.y), idH: id.height, rowH: tr.getBoundingClientRect().height}; }""")
        self.assertLess(geo['dy'], 8, geo)          # ID and severity share a line
        self.assertLess(geo['idH'], 40, geo)        # no letter-by-letter wrapping
        self.assertLess(geo['rowH'], 170, geo)
        self.assertTrue(page.evaluate(OVERFLOW_JS), page.evaluate('[document.scrollingElement.scrollWidth, innerWidth]'))
        page.click('#fmApprove')
        expect(page.locator('#fmHash')).to_have_text(re.compile('^[0-9a-f]{64}$'), timeout=30000)
        expect(page.locator('#fmPreviewFallback')).to_be_visible()
        expect(page.locator('#fmPreview')).to_be_hidden()
        self.assertIsNone(page.get_attribute('#fmPreview', 'src'))                   # no hidden iframe pointed at the PDF
        with page.context.expect_page() as popup:
            page.click('#fmOpenPdf')
        self.assertTrue(popup.value.url.startswith('blob:'))
        self.assertTrue(page.evaluate(OVERFLOW_JS))
        self.assertEqual(errors, [])

    def test_privacy_truthful_workspace_lifecycle_and_document_delete(self):
        page, errors = self.page()
        self.demo_account(page)
        page.click('#fmSample'); expect(page.locator('#fmDoc')).not_to_have_value('')
        page.click('.sidebar a[data-view="privacy"]')
        expect(page.locator('#privDocs')).to_have_text('1')
        self.assertNotIn('30 days', page.locator('#view-privacy').inner_text())
        self.assertNotIn('Offsite', page.locator('#view-privacy').inner_text())
        expect(page.locator('#privDeleteWorkspaceBtn')).to_be_disabled()
        self.assertIn('only workspace', page.get_attribute('#privDeleteWorkspaceBtn', 'title'))
        # new workspace from the account menu, then delete it while it is active: you stay signed in
        page.click('#userAccountPill'); page.click('#newWorkspaceBtn')
        page.wait_for_load_state('load')
        expect(page.locator('#workspaceSwitcher option:checked')).to_have_text('Client B', timeout=15000)
        page.click('.sidebar a[data-view="privacy"]')
        expect(page.locator('#privDeleteWorkspaceBtn')).to_be_enabled()
        page.click('#privDeleteWorkspaceBtn')
        expect(page.locator('#deletionReceiptLog')).to_contain_text('WORKSPACE deletion COMPLETED')
        page.wait_for_load_state('load')
        expect(page.locator('#workspaceSwitcher option')).to_have_count(1, timeout=15000)
        expect(page.locator('#workspaceSwitcher option:checked')).to_have_text("Demo's Workspace")
        self.assertFalse(page.locator('#authDialog').evaluate('d => d.open'))      # still signed in
        # per-document delete in Documents
        page.click('.sidebar a[data-view="documents"]')
        expect(page.locator('#docList .doc-row')).to_have_count(1)
        page.locator('#docList .doc-row button', has_text='Delete').click()
        expect(page.locator('#docList')).to_contain_text('No PDFs')
        expect(page.locator('#docPreviewEmpty')).to_be_visible()
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
