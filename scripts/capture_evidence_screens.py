"""Capture fresh high-resolution screenshots of Mission Control and Landing Page for G9 release evidence."""
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'src'))

from test_web_api_g1 import Server, free_port
from playwright.sync_api import sync_playwright

def capture_all():
    out_dir = ROOT / 'harness' / 'evidence' / 'screens'
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        port = free_port()
        srv = Server(tmp, port).start()
        print(f"Server started on port {port}")
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={'width': 1440, 'height': 900})
            page = ctx.new_page()

            # 1. Landing page
            print("Capturing landing...")
            page.goto(f'http://127.0.0.1:{port}/')
            page.wait_for_load_state('networkidle')
            time.sleep(1)
            page.screenshot(path=str(out_dir / 'landing_desktop_dark.png'))

            # 2. Mission control - mission view
            print("Capturing mission control...")
            page.goto(f'http://127.0.0.1:{port}/app#view-mission')
            page.wait_for_load_state('domcontentloaded')
            time.sleep(1)
            page.screenshot(path=str(out_dir / 'app_desktop_mission_dark.png'))

            # 3. Approval inbox view
            print("Capturing approval inbox...")
            page.click('a[data-view="approvals"]')
            time.sleep(1)
            page.screenshot(path=str(out_dir / 'app_desktop_approvals_dark.png'))

            # 4. Evidence view
            print("Capturing evidence & receipts...")
            page.click('a[data-view="evidence"]')
            time.sleep(1)
            page.screenshot(path=str(out_dir / 'app_desktop_evidence_dark.png'))

            # 5. Windows native view
            print("Capturing windows native control...")
            page.click('a[data-view="windows"]')
            time.sleep(1)
            page.screenshot(path=str(out_dir / 'app_desktop_windows_dark.png'))

            # 5b. Cloud computer view
            print("Capturing cloud computer...")
            page.click('a[data-view="cloud"]')
            time.sleep(1)
            page.screenshot(path=str(out_dir / 'app_desktop_cloud_dark.png'))

            # 5c. Connected apps view
            print("Capturing connected apps...")
            page.click('a[data-view="connectors"]')
            time.sleep(1)
            page.screenshot(path=str(out_dir / 'app_desktop_connectors_dark.png'))

            # 5d. Privacy & data view
            print("Capturing privacy & data...")
            page.click('a[data-view="privacy"]')
            time.sleep(1)
            page.screenshot(path=str(out_dir / 'app_desktop_privacy_dark.png'))

            # 6. Agents view
            print("Capturing agents view...")
            page.click('a[data-view="agents"]')
            time.sleep(1)
            page.screenshot(path=str(out_dir / 'app_desktop_agents_dark.png'))

            # 7. Mobile view
            print("Capturing mobile view...")
            mctx = browser.new_context(viewport={'width': 390, 'height': 844})
            mpage = mctx.new_page()
            mpage.goto(f'http://127.0.0.1:{port}/app#view-mission')
            mpage.wait_for_load_state('domcontentloaded')
            time.sleep(1)
            mpage.screenshot(path=str(out_dir / 'app_mobile_dark.png'))
            mctx.close()

            browser.close()
            pw.stop()
            print("All screenshots captured successfully to harness/evidence/screens/")
        finally:
            srv.stop()

if __name__ == '__main__':
    capture_all()

