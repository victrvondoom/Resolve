"""Regenerate the README screenshots from the running product (real server, real Chromium, real mission run).

    python scripts/capture_readme_screens.py [port]

Starts src/resolve_app.py in demo mode on the given loopback port, runs the sample brief mission with browser
diagnostics enabled, and writes PNGs to docs/readme/. Nothing is drawn by hand: every image is a capture of the page.
"""
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs' / 'readme'
sys.path.insert(0, str(ROOT / 'src'))


def wait_http(url, tries=120):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=2).read()
            return
        except Exception:
            time.sleep(0.25)
    raise SystemExit(f'server did not answer at {url}')


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else '8771'
    env = dict(os.environ, RESOLVE_PORT=port)
    server = subprocess.Popen([sys.executable, str(ROOT / 'src' / 'resolve_app.py')], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f'http://127.0.0.1:{port}'
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        wait_http(base + '/api/health')
        from playwright.sync_api import expect, sync_playwright
        from cloud_browser import _chromium_executable
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=_chromium_executable())
            page = browser.new_page(viewport={'width': 1440, 'height': 1000}, device_scale_factor=1)
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(base + '/')
            page.wait_for_selector('.i3d-host canvas, .i3d-host .i3d-stage')
            page.wait_for_timeout(900)
            page.locator('.hero').screenshot(path=str(OUT / 'landing-hero.png'))
            page.locator('.hero-model').screenshot(path=str(OUT / 'model-3d.png'))

            page.goto(base + '/app#flagship')
            page.wait_for_selector('#fmSample')
            page.click('#fmSample')
            expect(page.locator('#fmDoc')).not_to_have_value('')
            page.check('#fmInspectWeb')
            page.click('#fmStart')
            page.wait_for_selector('#fmPlanCard:not([hidden])', timeout=90000)
            page.wait_for_timeout(600)
            page.locator('#fmNodes').evaluate('e => e.closest("section").scrollIntoView({block:"start"})')
            page.wait_for_timeout(500)
            page.locator('#fmNodes').locator('xpath=ancestor::section[1]').screenshot(path=str(OUT / 'mission-workers.png'))
            page.locator('#fmComputerCard').screenshot(path=str(OUT / 'live-computer.png'))
            page.locator('#fmAssurance').screenshot(path=str(OUT / 'mission-assurance.png'))
            page.locator('#fmPlanCard').screenshot(path=str(OUT / 'plan-approval.png'))

            m = browser.new_page(viewport={'width': 390, 'height': 844}, is_mobile=True, has_touch=True)
            m.goto(base + '/')
            m.wait_for_timeout(900)
            m.screenshot(path=str(OUT / 'landing-mobile.png'))
            browser.close()
            if errors:
                print('page errors during capture:', errors)
                return 1
        print('wrote', sorted(p.name for p in OUT.glob('*.png')))
        return 0
    finally:
        server.terminate()
        server.wait(10)


if __name__ == '__main__':
    raise SystemExit(main())
