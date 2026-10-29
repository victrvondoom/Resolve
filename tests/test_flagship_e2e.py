"""Browser E2E of the flagship journey in PRODUCTION mode with CSP enforced (no bypass).

HONEST SCOPE
- Real: Chromium, getUserMedia capture (Chromium fake-device flags feed a real WAV file into the real capture path),
  WAV encoding in the page, upload, auth, mission workers, PDF generation, download, hash comparison.
- Stubbed: the speech engine. A local fake HTTP STT server returns a fixed transcript, so this proves the audio
  path, NOT recognition accuracy. Live-speech verification is tracked separately as BLOCKED.
- Browser print: the dialog call is made; physical printing is never claimed.
"""
import hashlib
import re
import http.server
import io
import json
import math
import os
import struct
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from test_production_security import ProdServer
from test_web_api_g1 import free_port, ROOT
from cloud_browser import _chromium_executable

try:
    from playwright.sync_api import sync_playwright, expect
    HAVE_PW = True
except ImportError:
    HAVE_PW = False

TRANSCRIPT = 'Resolve, inspect my project brief, analyze the implementation issues, create a corrected architecture, generate a polished report and prepare it for print.'
SHOTS = Path(os.environ.get('RESOLVE_SCREENSHOT_DIR', '')) if os.environ.get('RESOLVE_SCREENSHOT_DIR') else None
OVERFLOW_JS = "document.scrollingElement.scrollWidth <= innerWidth + 1"


class FakeStt(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        FakeStt.received.append(body)
        out = json.dumps({'text': TRANSCRIPT}).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(out))); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


def tone_wav(path, seconds=3):
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
        w.writeframes(b''.join(struct.pack('<h', int(9000 * math.sin(2 * math.pi * 300 * i / 48000))) for i in range(48000 * seconds)))


@unittest.skipUnless(HAVE_PW, 'browser-e2e: playwright not installed')
class FlagshipE2E(unittest.TestCase):
    def test_voice_to_verified_pdf_download_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / 'mic.wav'; tone_wav(wav)
            stt = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeStt)
            threading.Thread(target=stt.serve_forever, daemon=True).start()
            FakeStt.received.clear()
            srv = ProdServer(tmp, free_port(), {'RESOLVE_STT_URL': f'http://127.0.0.1:{stt.server_address[1]}/v1/audio/transcriptions', 'RESOLVE_STT_API_KEY': 'test'}).start()
            errors = []
            try:
                with sync_playwright() as p:
                    exe = _chromium_executable()
                    b = p.chromium.launch(executable_path=exe, args=['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream', f'--use-file-for-fake-audio-capture={wav}'])
                    ctx = b.new_context(viewport={'width': 1360, 'height': 900}, accept_downloads=True, permissions=['microphone'])
                    page = ctx.new_page()
                    page.on('pageerror', lambda e: errors.append(f'pageerror: {e}'))
                    page.on('console', lambda m: errors.append(f'console: {m.text}') if m.type == 'error' else None)
                    page.on('dialog', lambda d: d.accept())   # the informed-consent confirm() for off-server STT
                    page.goto(f'http://127.0.0.1:{srv.port}/app')
                    # production: no anonymous access -> account dialog
                    page.wait_for_selector('#authDialog[open]')
                    page.click('#authDemo')
                    page.wait_for_selector('#authDialog', state='hidden')
                    # mic availability reflects real checks
                    expect(page.locator('#fmMicBadge')).to_contain_text('MIC READY', timeout=15000)
                    self.assertIn('CLOUD STT', page.inner_text('#fmMicBadge'))
                    # record real (fake-device) audio, transcribe through the server
                    page.fill('#fmIntent', '')
                    page.click('#fmMic')
                    expect(page.locator('#fmMicBadge')).to_contain_text('RECORDING')
                    page.wait_for_timeout(1500)
                    page.click('#fmMic')
                    expect(page.locator('#fmIntent')).not_to_have_value('', timeout=20000)
                    self.assertEqual(page.input_value('#fmIntent'), TRANSCRIPT)
                    self.assertEqual(len(FakeStt.received), 1)
                    self.assertIn(b'RIFF', FakeStt.received[0]); self.assertIn(b'WAVE', FakeStt.received[0])
                    self.assertGreater(len(FakeStt.received[0]), 16000 * 2 * 1)   # >= ~1 s of 16 kHz PCM16 actually captured
                    self.assertIn('Transcript ready', page.inner_text('#fmMicNote'))
                    self.assertEqual(page.locator('#fmNodes li.muted').count(), 1)  # transcript alone does NOT start a mission
                    # sample brief + mission
                    page.click('#fmSample')
                    expect(page.locator('#fmDoc')).not_to_have_value('')
                    page.click('#fmStart')
                    page.wait_for_selector('#fmPlanCard:not([hidden])', timeout=30000)
                    states = page.locator('#fmNodes li .chip').all_inner_texts()
                    self.assertEqual(states[:2], ['DONE', 'DONE'])
                    self.assertGreaterEqual(page.locator('#fmIssues tr').count(), 5)
                    self.assertGreater(page.locator('#fmDiagram svg rect').count(), 5)
                    self.assertTrue(page.evaluate(OVERFLOW_JS))
                    if SHOTS:
                        SHOTS.mkdir(parents=True, exist_ok=True); page.screenshot(path=str(SHOTS / 'flagship-plan-desktop.png'), full_page=True)
                    page.click('#fmApprove')
                    page.wait_for_selector('#fmResultCard:not([hidden]) #fmHash:not(:empty)', timeout=30000)
                    expect(page.locator('#fmHash')).to_have_text(re.compile('^[0-9a-f]{64}$'))
                    self.assertIn('CONFIRMED', page.inner_text('#fmResultChip'))
                    self.assertEqual(page.locator('#fmChecks .chip-bad').count(), 0)
                    shown_hash = page.inner_text('#fmHash')
                    expect(page.locator('#fmPreview')).to_have_attribute('src', re.compile('^blob:'))
                    with page.expect_download() as dl:
                        page.click('#fmDownload')
                    data = Path(dl.value.path()).read_bytes()
                    self.assertTrue(data.startswith(b'%PDF-'))
                    self.assertEqual(hashlib.sha256(data).hexdigest(), shown_hash)   # file the user got == hash the UI claims
                    if SHOTS:
                        page.screenshot(path=str(SHOTS / 'flagship-result-desktop.png'), full_page=True)
                    # mobile layout
                    mobile = b.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True)
                    self.assertTrue(page.evaluate(OVERFLOW_JS))
                    # delete
                    page.click('#fmDelete')
                    page.wait_for_selector('#fmResultCard', state='hidden')
                    self.assertIn('deleted', page.inner_text('#fmStatus').lower())
                    # mobile pass on the same signed-in session
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.wait_for_timeout(300)
                    self.assertTrue(page.evaluate(OVERFLOW_JS), page.evaluate('[document.scrollingElement.scrollWidth, innerWidth]'))
                    if SHOTS:
                        page.screenshot(path=str(SHOTS / 'flagship-mobile.png'), full_page=True)
                    b.close()
                self.assertEqual([e for e in errors if 'Failed to load resource' not in e], [], errors)
            finally:
                srv.stop(); stt.shutdown()


if __name__ == '__main__':
    unittest.main()
