"""Cinematic intro: skippable, closed by Escape, replayable, skipped for reduced motion, never blocks the app link."""
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
    from playwright.sync_api import expect, sync_playwright
    HAVE_PW = True
except ImportError:
    HAVE_PW = False


@unittest.skipUnless(HAVE_PW, 'browser-e2e: playwright not installed')
class IntroUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = ProdServer(cls.tmp.name, free_port()).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(executable_path=_chromium_executable())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close(); cls.pw.stop(); cls.srv.stop(); cls.tmp.cleanup()

    def open(self, **ctx):
        context = self.browser.new_context(viewport={'width': 1280, 'height': 800}, **ctx)
        self.addCleanup(context.close)
        page = context.new_page()
        self.errors = []
        page.on('pageerror', lambda e: self.errors.append(str(e)))
        page.goto(f'http://127.0.0.1:{self.srv.port}/')
        return page

    def test_skip_button_ends_the_intro_and_reveals_the_hero(self):
        page = self.open()
        expect(page.locator('#intro')).to_be_visible()
        expect(page.locator('#intro .intro-word')).to_have_attribute('aria-label', 'RESOLVE')
        page.click('.intro-skip')
        expect(page.locator('#intro')).to_have_class('intro is-done', timeout=3000)
        expect(page.locator('#hero-title')).to_be_visible()
        self.assertEqual(self.errors, [])

    def test_escape_key_closes_the_intro(self):
        page = self.open()
        expect(page.locator('#intro')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator('#intro')).to_have_class('intro is-done', timeout=3000)

    def test_replay_button_plays_the_intro_again(self):
        page = self.open()
        page.click('.intro-skip')
        expect(page.locator('#intro')).to_have_class('intro is-done', timeout=3000)
        page.click('[data-replay-intro]')
        expect(page.locator('#intro')).not_to_have_class('intro is-done', timeout=2000)
        page.click('.intro-skip')
        expect(page.locator('#intro')).to_have_class('intro is-done', timeout=3000)

    def test_reduced_motion_never_shows_the_intro(self):
        page = self.open(reduced_motion='reduce')
        expect(page.locator('#intro')).to_have_class('intro is-done', timeout=3000)
        self.assertEqual(page.evaluate("getComputedStyle(document.getElementById('intro')).display"), 'none')

    def test_scenes_play_in_the_required_order(self):
        page = self.open()
        order = page.evaluate("[...document.querySelectorAll('#intro .scene')].map(e => e.className.replace('scene ', ''))")
        self.assertEqual(order, ['scene-a', 'scene-b', 'scene-c', 'scene-d'])
        texts = page.evaluate("[...document.querySelectorAll('#intro .scene')].map(e => e.textContent.trim().replace(/\\s+/g, ' '))")
        self.assertIn('RESOLVE', texts[0].replace(' ', ''))
        self.assertEqual(texts[1], 'Wispr & Solve')
        self.assertIn('Wispr Flow', texts[2])
        self.assertIn('WeMakeDevs', texts[3])
        self.assertIn('No Hands: The Wispr Challenge', texts[3])
        page.click('.intro-skip')

    def test_missing_partner_logos_fall_back_to_plain_names_not_imitations(self):
        page = self.open()
        page.wait_for_timeout(400)
        missing = page.evaluate("[...document.querySelectorAll('[data-asset-missing]')].map(e => e.textContent)")
        # the repository ships no official logo files, so both must fall back to the plain partner name
        self.assertEqual(sorted(missing), sorted(['Wispr Flow', 'WeMakeDevs']))
        self.assertEqual(page.evaluate("document.querySelectorAll('#intro img').length"), 0)
        page.click('.intro-skip')

    def test_app_link_works_while_the_intro_plays(self):
        page = self.open()
        expect(page.locator('#intro')).to_be_visible()
        page.click('.intro-skip')
        page.click('#cta')
        page.wait_for_url('**/app', timeout=10000)
        expect(page.locator('#fmSample')).to_be_visible(timeout=10000)


if __name__ == '__main__':
    unittest.main()
