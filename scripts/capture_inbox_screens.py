"""Capture real screenshots of the inbox operator from a running RESOLVE server, driven through the UI.

    python scripts/capture_inbox_screens.py  -> docs/screenshots/inbox/*.png

Only the REPLAY (synthetic) dataset is used, so every image is labelled synthetic. Nothing is generated or edited by hand.
Requires Playwright with a Chromium binary (uses /opt/pw-browsers/chromium when present).
"""
import os
import subprocess
import sys
import tempfile
import time
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs' / 'screenshots' / 'inbox'
CHROMIUM = '/opt/pw-browsers/chromium'


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def main():
    from playwright.sync_api import sync_playwright
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='resolve-shots-')
    port = free_port()
    env = dict(os.environ, RESOLVE_DB=f'{tmp}/r.sqlite3', RESOLVE_PORT=str(port), RESOLVE_INBOX_DB=f'{tmp}/inbox.sqlite3',
               RESOLVE_DOCUMENTS_DIR=f'{tmp}/documents', RESOLVE_MISSIONS_DB=f'{tmp}/missions.sqlite3', RESOLVE_TENANTS_DB=f'{tmp}/t.sqlite3',
               RESOLVE_SANDBOXES_DIR=f'{tmp}/sbx', RESOLVE_SANDBOXES_DB=f'{tmp}/sbx.sqlite3', RESOLVE_CONNECTORS_DB=f'{tmp}/c.sqlite3')
    proc = subprocess.Popen([sys.executable, str(ROOT / 'src' / 'resolve_app.py')], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        import urllib.request
        for _ in range(100):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=1)
                break
            except Exception:
                time.sleep(0.1)
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=CHROMIUM, args=['--no-sandbox']) if Path(CHROMIUM).exists() else p.chromium.launch(args=['--no-sandbox'])
            ctx = b.new_context(viewport={'width': 1360, 'height': 900})
            pg = ctx.new_page()
            pg.goto(f'http://127.0.0.1:{port}/app#morning', wait_until='networkidle')
            pg.wait_for_timeout(800)
            pg.screenshot(path=str(OUT / '00_morning_not_connected.png'))
            pg.click('a[data-view="replay"]')
            pg.click('button[data-action="start"]')
            pg.wait_for_timeout(300)
            for _ in range(19):
                pg.click('button[data-action="step"]')
                pg.wait_for_timeout(200)
            pg.wait_for_timeout(500)
            pg.screenshot(path=str(OUT / '09_replay_lab.png'), full_page=True)
            pg.click('a[data-view="morning"]')
            pg.click('#view-morning button[data-value="REPLAY"]')
            pg.wait_for_selector('#view-morning .ib-kpi', timeout=8000)
            pg.wait_for_timeout(400)
            pg.screenshot(path=str(OUT / '01_morning_brief_replay.png'), full_page=True)
            pg.click('a[data-view="inbox"]')
            pg.wait_for_selector('#view-inbox .ib-list li', timeout=8000)
            pg.wait_for_timeout(300)
            pg.screenshot(path=str(OUT / '02_inbox_replay.png'), full_page=True)
            pg.locator('#view-inbox a.ib-item-title').nth(0).click()
            pg.wait_for_selector('#view-thread .ib-msg', timeout=8000)
            pg.wait_for_timeout(300)
            pg.screenshot(path=str(OUT / '03_thread_detail_replay.png'), full_page=True)
            pg.click('a[data-view="commitments"]')
            pg.wait_for_selector('#view-commitments .ib-list li', timeout=8000)
            pg.wait_for_timeout(300)
            pg.screenshot(path=str(OUT / '04_commitments_replay.png'), full_page=True)
            pg.locator('#view-commitments a.ib-item-title').nth(0).click()
            pg.wait_for_selector('#view-commitment dl', timeout=8000)
            pg.wait_for_timeout(300)
            pg.screenshot(path=str(OUT / '05_commitment_detail_replay.png'), full_page=True)
            pg.click('a[data-view="rules"]')
            pg.wait_for_selector('#ibRulesText', timeout=8000)
            pg.fill('#ibRulesText', 'Prioritize emails from my project collaborators. Show actual deadlines above ordinary questions. Track all promises I make. Show me what other people owe me. Never send an email without my approval. Use a friendly, concise tone.')
            pg.click('form[data-ib-form="rules-parse"] button[type="submit"]')
            pg.wait_for_selector('#ibRulesPreview .ib-rule', timeout=8000)
            pg.wait_for_timeout(300)
            pg.screenshot(path=str(OUT / '06_voice_rules_preview.png'), full_page=True)
            pg.click('a[data-view="drafts"]')
            pg.wait_for_selector('#view-drafts .ib-list li', timeout=8000)
            pg.locator('#view-drafts a.ib-item-title').nth(0).click()
            pg.wait_for_selector('#view-draft .ib-panel', timeout=8000)
            pg.wait_for_timeout(300)
            pg.screenshot(path=str(OUT / '07_draft_editor_replay.png'), full_page=True)
            pg.locator('#view-draft button[data-ib="approve"]').first.click()
            pg.wait_for_selector('.ib-approval', timeout=8000)
            pg.wait_for_timeout(300)
            pg.screenshot(path=str(OUT / '08_approval_exact_content_replay.png'), full_page=True)
            pg.click('.ib-approval button[data-ib="send"]')
            pg.wait_for_timeout(800)
            pg.screenshot(path=str(OUT / '10_simulated_send_and_timeline.png'), full_page=True)
            pg.click('a[data-view="connections"]')
            pg.wait_for_selector('#view-connections .ib-panel', timeout=8000)
            pg.wait_for_timeout(300)
            pg.screenshot(path=str(OUT / '11_connections_gmail_not_configured.png'), full_page=True)
            pg.set_viewport_size({'width': 390, 'height': 844})
            pg.click('a[data-view="morning"]')
            pg.wait_for_timeout(800)
            pg.screenshot(path=str(OUT / '12_mobile_morning_replay.png'))
            b.close()
        print('wrote', sorted(p.name for p in OUT.glob('*.png')))
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == '__main__':
    main()
