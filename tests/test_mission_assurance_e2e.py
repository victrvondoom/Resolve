"""Mission Assurance panel, real Chromium, production server: every value shown must match the recorded mission."""
import os
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

SHOTS = os.environ.get('RESOLVE_SAVE_SCREENSHOTS')


@unittest.skipUnless(HAVE_PW, 'browser-e2e: playwright not installed')
class MissionAssuranceE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = ProdServer(cls.tmp.name, free_port(), extra={'RESOLVE_DIAGNOSTICS': '1'}).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=_chromium_executable())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.stop(); cls.tmp.cleanup()

    def test_panel_reflects_recorded_approval_timeline_evidence_and_verification(self):
        context = self.browser.new_context(viewport={'width': 1360, 'height': 1000})
        self.addCleanup(context.close)
        page = context.new_page()
        self.errors = []
        page.on('pageerror', lambda e: self.errors.append(str(e)))
        page.on('dialog', lambda d: d.accept())
        page.goto(f'http://127.0.0.1:{self.srv.port}/app')
        page.wait_for_selector('#authDialog[open]'); page.click('#authDemo'); page.wait_for_selector('#authDialog', state='hidden')
        expect(page.locator('#fmAssureChip')).to_have_text('NO MISSION')
        page.click('#fmSample'); expect(page.locator('#fmDoc')).not_to_have_value('')
        page.click('#fmStart')
        page.wait_for_selector('#fmPlanCard:not([hidden])', timeout=60000)
        # before approval: panel says so, and the recorded timeline has the mission creation event
        expect(page.locator('#fmAssureApproval')).to_contain_text('Awaiting approval')
        expect(page.locator('#fmAssureTimeline')).to_contain_text('MISSION_CREATED')
        page.click('#fmApprove')
        expect(page.locator('#fmHash')).to_have_text(re.compile('^[0-9a-f]{64}$'), timeout=60000)
        expect(page.locator('#fmAssureChip')).to_have_text('CONFIRMED', timeout=30000)
        expect(page.locator('#fmAssureApproval')).to_contain_text('Approved')
        expect(page.locator('#fmAssureTimeline')).to_contain_text('PLAN_APPROVED')
        expect(page.locator('#fmAssureVerify')).to_contain_text('Independent checks passed')
        # the digest shown equals the digest the backend recorded for the approval
        shown = page.inner_text('#fmAssureApproval')
        token = page.evaluate("() => sessionStorage.getItem('resolve_token') || ''")
        listed = self.srv.req('GET', '/api/brief-missions', headers={'Authorization': 'Bearer ' + token})[1]['missions']
        mission = self.srv.req('GET', f"/api/brief-missions/{listed[0]['id']}", headers={'Authorization': 'Bearer ' + token})[1]
        approved = next(e for e in mission['events'] if e['kind'] == 'PLAN_APPROVED')
        self.assertIn(approved['detail']['plan_digest'], shown.replace('\n', ' '), 'panel must show the recorded approval digest')
        self.assertEqual(approved['detail']['plan_digest'], mission['plan']['digest'])
        self.assertEqual(page.locator('#fmAssureUncertain').inner_text().strip(), 'No uncertain outcome recorded.')
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            page.locator('#fmAssurance').screenshot(path=str(Path(SHOTS) / 'mission-assurance-confirmed.png'))
        self.assertEqual(self.errors, [])


if __name__ == '__main__':
    unittest.main()
