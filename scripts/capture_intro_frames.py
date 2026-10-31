"""Capture the landing-page intro at fixed moments from a fresh session (real server, real Chromium).

    python scripts/capture_intro_frames.py [port]   -> docs/readme/intro-0{1..5}.png
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


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else '8772'
    server = subprocess.Popen([sys.executable, str(ROOT / 'src' / 'resolve_app.py')], env=dict(os.environ, RESOLVE_PORT=port),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=2).read(); break
            except Exception:
                time.sleep(0.25)
        from playwright.sync_api import sync_playwright
        from cloud_browser import _chromium_executable
        OUT.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=_chromium_executable())
            page = b.new_page(viewport={'width': 1280, 'height': 800})
            errors = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(f'http://127.0.0.1:{port}/')
            t0 = time.time()
            for i, at in enumerate([0.9, 1.9, 2.9, 3.7, 4.6], 1):
                time.sleep(max(0, at - (time.time() - t0)))
                page.screenshot(path=str(OUT / f'intro-0{i}.png'))
            page.wait_for_timeout(800)
            page.screenshot(path=str(OUT / 'landing-after-intro.png'))
            b.close()
        print('frames written; page errors:', errors)
        return 1 if errors else 0
    finally:
        server.terminate(); server.wait(10)


if __name__ == '__main__':
    raise SystemExit(main())
