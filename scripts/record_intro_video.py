"""Record the real landing-page intro as a video (Playwright page video), then convert it for the README.

    python scripts/record_intro_video.py [port]
      -> docs/readme/intro.mp4   (H.264, for the README link/player)
      -> docs/readme/intro.gif   (animated, shows inline on GitHub)
Requires ffmpeg on PATH for the conversion step. Nothing is drawn: every frame is the running page.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs' / 'readme'
sys.path.insert(0, str(ROOT / 'src'))


def main() -> int:
    port = sys.argv[1] if len(sys.argv) > 1 else '8774'
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
        tmp = Path(tempfile.mkdtemp(prefix='intro-video-'))
        frames_dir = tmp / 'frames'
        frames_dir.mkdir()
        stamps = []
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path=_chromium_executable())
            page = b.new_page(viewport={'width': 1280, 'height': 720})
            page.goto(f'http://127.0.0.1:{port}/')
            # Real page screenshots at timed intervals (the bundled Playwright video encoder is not installed here).
            t0 = time.time()
            i = 0
            while time.time() - t0 < 10.5:
                fp = frames_dir / f'f{i:05d}.jpg'
                page.screenshot(path=str(fp), type='jpeg', quality=88)
                stamps.append((fp.name, time.time() - t0))
                i += 1
            b.close()
        mp4 = OUT / 'intro.mp4'
        gif = OUT / 'intro.gif'
        if shutil.which('ffmpeg') is None:
            raise SystemExit('ffmpeg is required to build the video')
        # concat list with the measured duration of every frame, so playback speed matches the real timing
        lines = []
        for k, (name, ts) in enumerate(stamps):
            nxt = stamps[k + 1][1] if k + 1 < len(stamps) else ts + 0.1
            lines.append(f"file '{frames_dir / name}'")
            lines.append(f"duration {max(0.02, nxt - ts):.3f}")
        lines.append(f"file '{frames_dir / stamps[-1][0]}'")
        (tmp / 'list.txt').write_text('\n'.join(lines))
        webm = mp4
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', str(tmp / 'list.txt'), '-vf', 'fps=30,format=yuv420p',
                        '-c:v', 'libx264', '-crf', '23', '-movflags', '+faststart', str(mp4)], check=True)
        webm = webm  # the timed capture is already the mp4; the GIF is made from it
        palette = tmp / 'palette.png'
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(webm), '-vf', 'fps=12,scale=960:-1:flags=lanczos,palettegen', str(palette)], check=True)
        subprocess.run(['ffmpeg', '-y', '-loglevel', 'error', '-i', str(webm), '-i', str(palette), '-lavfi',
                        'fps=12,scale=960:-1:flags=lanczos[x];[x][1:v]paletteuse', '-loop', '0', str(gif)], check=True)
        print('wrote', mp4.name, mp4.stat().st_size, 'bytes;', gif.name, gif.stat().st_size, 'bytes')
        return 0
    finally:
        server.terminate(); server.wait(10)


if __name__ == '__main__':
    raise SystemExit(main())
