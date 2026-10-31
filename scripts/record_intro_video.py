"""Record the intro video: the REAL inbox-operator UI walking the product journey on the synthetic REPLAY mailbox.

    python scripts/record_intro_video.py
      -> docs/readme/intro.mp4   (H.264, for the README link/player)
      -> docs/readme/intro.gif   (animated, shows inline on GitHub)

Every frame is a screenshot of the running app, driven through its own UI (clicks and typing). The only addition is a caption
strip injected into the page so a viewer knows which step of the journey is on screen; it changes nothing in the app.
All data is the labelled synthetic REPLAY dataset; sends are SIMULATED. Requires Playwright + Chromium and ffmpeg on PATH.
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs' / 'readme'
sys.path.insert(0, str(ROOT / 'src'))

CAPTION_JS = """(t) => {
  let d = document.getElementById('intro-caption');
  if (!d) {
    d = document.createElement('div'); d.id = 'intro-caption';
    d.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;padding:16px 28px;background:rgba(17,24,39,.94);color:#fff;'
      + 'font:600 26px/1.3 system-ui,Segoe UI,Arial,sans-serif;text-align:center;letter-spacing:.2px;pointer-events:none';
    document.body.appendChild(d);
  }
  d.textContent = t;
}"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Recorder:
    """Takes real screenshots back to back; each frame lasts exactly as long as it really took."""
    def __init__(self, page, frames: Path):
        self.page, self.frames, self.stamps, self.t0 = page, frames, [], time.time()

    def hold(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            fp = self.frames / f'f{len(self.stamps):05d}.jpg'
            self.page.screenshot(path=str(fp), type='jpeg', quality=85)
            self.stamps.append((fp, time.time() - self.t0))

    def caption(self, text: str) -> None:
        self.page.evaluate(CAPTION_JS, text)


def main() -> int:
    if shutil.which('ffmpeg') is None:
        raise SystemExit('ffmpeg is required to build the video')
    from playwright.sync_api import sync_playwright
    from cloud_browser import _chromium_executable
    tmp = Path(tempfile.mkdtemp(prefix='intro-video-'))
    frames = tmp / 'frames'
    frames.mkdir()
    port = free_port()
    env = dict(os.environ, RESOLVE_DB=f'{tmp}/r.sqlite3', RESOLVE_PORT=str(port), RESOLVE_INBOX_DB=f'{tmp}/inbox.sqlite3',
               RESOLVE_DOCUMENTS_DIR=f'{tmp}/documents', RESOLVE_MISSIONS_DB=f'{tmp}/missions.sqlite3', RESOLVE_TENANTS_DB=f'{tmp}/t.sqlite3',
               RESOLVE_SANDBOXES_DIR=f'{tmp}/sbx', RESOLVE_SANDBOXES_DB=f'{tmp}/sbx.sqlite3', RESOLVE_CONNECTORS_DB=f'{tmp}/c.sqlite3')
    server = subprocess.Popen([sys.executable, str(ROOT / 'src' / 'resolve_app.py')], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(200):
            try:
                urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=2).read()
                break
            except Exception:
                time.sleep(0.25)
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=_chromium_executable(), args=['--no-sandbox'])
            pg = b.new_context(viewport={'width': 1280, 'height': 720}).new_page()
            pg.goto(f'http://127.0.0.1:{port}/app#morning', wait_until='networkidle')
            pg.wait_for_timeout(600)
            r = Recorder(pg, frames)

            r.caption('RESOLVE: from inbox noise to verified follow-through')
            r.hold(3.0)

            r.caption('1  Connect and synchronize. Here: the labelled synthetic REPLAY mailbox')
            pg.click('a[data-view="replay"]')
            pg.click('button[data-action="start"]')
            r.hold(1.5)
            for _ in range(19):
                pg.click('button[data-action="step"]')
                r.hold(0.12)
            r.hold(1.5)

            r.caption('2  Morning brief: what needs you, what you promised, what you wait for')
            pg.click('a[data-view="morning"]')
            pg.click('#view-morning button[data-value="REPLAY"]')
            pg.wait_for_selector('#view-morning .ib-kpi', timeout=8000)
            r.hold(4.0)

            r.caption('3  Understand complete threads. Each decision cites its source sentence')
            pg.click('a[data-view="inbox"]')
            pg.wait_for_selector('#view-inbox .ib-list li', timeout=8000)
            r.hold(2.0)
            pg.locator('#view-inbox a.ib-item-title').nth(0).click()
            pg.wait_for_selector('#view-thread .ib-msg', timeout=8000)
            r.hold(3.0)

            r.caption('4  Track who owes what, with evidence and a deadline')
            pg.click('a[data-view="commitments"]')
            pg.wait_for_selector('#view-commitments .ib-list li', timeout=8000)
            r.hold(2.5)
            pg.locator('#view-commitments a.ib-item-title').nth(0).click()
            pg.wait_for_selector('#view-commitment dl', timeout=8000)
            r.hold(3.0)

            r.caption('5  Correct by dictation: spoken or typed text in an ordinary field')
            pg.click('a[data-view="rules"]')
            pg.wait_for_selector('#ibRulesText', timeout=8000)
            r.hold(1.0)
            pg.click('#ibRulesText')
            for chunk in ('Show actual deadlines above ordinary questions. ', 'Track all promises I make. ', 'Never send an email without my approval.'):
                pg.keyboard.type(chunk, delay=15)
                r.hold(0.8)
            pg.click('form[data-ib-form="rules-parse"] button[type="submit"]')
            pg.wait_for_selector('#ibRulesPreview .ib-rule', timeout=8000)
            r.caption('Nothing is active until you approve the parsed preview')
            r.hold(3.5)

            r.caption('6  Prepare a reply. Missing facts are listed, never invented')
            pg.click('a[data-view="drafts"]')
            pg.wait_for_selector('#view-drafts .ib-list li', timeout=8000)
            pg.locator('#view-drafts a.ib-item-title').nth(0).click()
            pg.wait_for_selector('#view-draft .ib-panel', timeout=8000)
            r.hold(3.5)

            r.caption('7  Review the exact content, then approve. The approval is bound to that digest')
            pg.locator('#view-draft button[data-ib="approve"]').first.click()
            pg.wait_for_selector('.ib-approval', timeout=8000)
            r.hold(4.0)

            r.caption('8  Execute, read back, verify. In REPLAY the send is SIMULATED')
            pg.click('.ib-approval button[data-ib="send"]')
            pg.wait_for_timeout(500)
            pg.evaluate('window.scrollTo(0, 0)')
            r.hold(3.0)          # the draft now carries its "Sent · verified" badge
            pg.get_by_text('Execution timeline').first.scroll_into_view_if_needed()
            r.hold(3.5)          # the timeline line: SEND_EMAIL · VERIFIED · SIMULATED

            r.caption('Real Gmail is read-only by default. A timeout is UNCERTAIN and is never resent')
            r.hold(3.0)
            b.close()

        stamps = r.stamps
        lines = []
        for k, (fp, ts) in enumerate(stamps):
            nxt = stamps[k + 1][1] if k + 1 < len(stamps) else ts + 0.1
            lines += [f"file '{fp}'", f'duration {max(0.02, nxt - ts):.3f}']
        lines.append(f"file '{stamps[-1][0]}'")
        (tmp / 'list.txt').write_text('\n'.join(lines))
        OUT.mkdir(parents=True, exist_ok=True)
        mp4, gif = OUT / 'intro.mp4', OUT / 'intro.gif'
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', str(tmp / 'list.txt'), '-vf', 'fps=30,format=yuv420p',
                        '-c:v', 'libx264', '-crf', '24', '-movflags', '+faststart', str(mp4)], check=True)
        pal = tmp / 'palette.png'
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(mp4), '-vf', 'fps=8,scale=800:-1:flags=lanczos,palettegen=max_colors=96', str(pal)], check=True)
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(mp4), '-i', str(pal), '-lavfi',
                        'fps=8,scale=800:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4', '-loop', '0', str(gif)], check=True)
        print(f'wrote {mp4.name} {mp4.stat().st_size} bytes, {stamps[-1][1]:.1f} s; {gif.name} {gif.stat().st_size} bytes; {len(stamps)} frames')
        return 0
    finally:
        server.terminate()
        server.wait(10)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
