"""Smoke-test every sidebar page and enabled control in the running app; report page errors, console errors and failed requests.

    python scripts/smoke_web.py [port]   -> prints a table, writes docs/SMOKE_WEB_REPORT.json
Destructive controls (delete, destroy, emergency stop, sign out, workspace/account deletion) are skipped by name.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
DESTRUCTIVE = ('delete', 'destroy', 'emergency', 'sign out', 'remove', 'purge', 'stop', 'revoke', 'disconnect', 'clear')


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else '8780'
    server = subprocess.Popen([sys.executable, str(ROOT / 'src' / 'resolve_app.py')], env=dict(os.environ, RESOLVE_PORT=port),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    report = {'pages': [], 'controls': []}
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=2).read(); break
            except Exception:
                time.sleep(0.25)
        from playwright.sync_api import sync_playwright
        from cloud_browser import _chromium_executable
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=_chromium_executable(), args=['--disable-dev-shm-usage'])
            page = b.new_page(viewport={'width': 1366, 'height': 900})
            errs, fails = [], []
            page.on('pageerror', lambda e: errs.append(str(e)[:200]))
            page.on('console', lambda m: errs.append('console: ' + m.text[:200]) if m.type == 'error' else None)
            page.on('response', lambda r: fails.append(f'{r.status} {r.url[-80:]}') if r.status >= 400 else None)
            crashes = []
            page.on('crash', lambda _: crashes.append(1))
            page.goto(f'http://127.0.0.1:{port}/app')
            page.wait_for_timeout(800)
            views = page.evaluate("[...document.querySelectorAll('.sidebar a[data-view]')].map(a=>a.dataset.view)")
            for v in dict.fromkeys(views):
                errs.clear(); fails.clear()
                try:
                    page.evaluate(f"document.querySelectorAll('.nav-more').forEach(d=>d.open=true)")
                    page.click(f'.sidebar a[data-view="{v}"]', timeout=4000)
                    page.wait_for_timeout(500)
                    visible = page.evaluate(f"!!document.getElementById('view-{v}') && !document.getElementById('view-{v}').hidden")
                    report['pages'].append({'view': v, 'opens': visible, 'page_errors': list(errs), 'failed_requests': list(fails)})
                except Exception as exc:
                    report['pages'].append({'view': v, 'opens': False, 'error': str(exc)[:160]})
                # controls in this view
                btns = page.evaluate(f"""[...document.querySelectorAll('#view-{v} button:not(:disabled), #view-{v} input[type=submit]')]
                    .map((b,i)=>({{i, text:(b.textContent||b.value||b.getAttribute('aria-label')||'').trim().slice(0,60)}}))""") if v else []
                for bt in btns:
                    name = bt['text'].lower()
                    if any(d in name for d in DESTRUCTIVE) or not name:
                        continue
                    errs.clear(); fails.clear()
                    if crashes:
                        crashes.clear(); page.goto(f'http://127.0.0.1:{port}/app'); page.wait_for_timeout(800)
                        page.evaluate("document.querySelectorAll('.nav-more').forEach(d=>d.open=true)"); page.click(f'.sidebar a[data-view="{v}"]'); page.wait_for_timeout(400)
                    try:
                        page.locator(f'#view-{v} button:not(:disabled)').nth(bt['i']).click(timeout=3000)
                        page.wait_for_timeout(350)
                        report['controls'].append({'view': v, 'control': bt['text'], 'page_errors': list(errs), 'failed_requests': list(fails)})
                    except Exception as exc:
                        report['controls'].append({'view': v, 'control': bt['text'], 'error': str(exc)[:120]})
            b.close()
    finally:
        server.terminate(); server.wait(10)
    bad_pages = [x for x in report['pages'] if not x.get('opens') or x.get('page_errors')]
    bad_ctrls = [x for x in report['controls'] if x.get('error') or x.get('page_errors') or x.get('failed_requests')]
    report['summary'] = {'pages_checked': len(report['pages']), 'pages_broken': len(bad_pages),
                         'controls_checked': len(report['controls']), 'controls_broken': len(bad_ctrls)}
    out = ROOT / 'docs' / 'SMOKE_WEB_REPORT.json'
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report['summary'], indent=2))
    for x in bad_pages:
        print('PAGE', x['view'], x.get('error') or x.get('page_errors') or 'not visible')
    for x in bad_ctrls[:40]:
        print('CTRL', x['view'], repr(x['control']), x.get('error') or x.get('page_errors') or x.get('failed_requests'))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
