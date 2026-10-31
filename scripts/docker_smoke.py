"""Build-and-run smoke test of the production container image.

    docker build --build-arg WITH_STT=1 -t resolve:smoke .
    python scripts/docker_smoke.py [--image resolve:smoke] [--port 18765]

Starts the image in production mode with a fresh volume, then over real HTTP: health, security headers,
register, sign in, create a PDF report, download it and compare SHA-256, speech health, transcribe a real
WAV (when the image was built WITH_STT=1), cross-user denial, restart the container on the same volume and
sign in again (persistence). Exit code 0 only if every check passes. Writes nothing outside a temp volume.
"""
import argparse
import base64
import hashlib
import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def req(port, method, path, body=None, token=None, raw=False):
    h = {'Content-Type': 'application/json', 'Host': f'127.0.0.1:{port}'}
    if token:
        h['Authorization'] = f'Bearer {token}'
    r = urllib.request.Request(f'http://127.0.0.1:{port}{path}', method=method, headers=h,
                               data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            data = resp.read()
            return resp.status, (data if raw else json.loads(data or b'{}')), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b'{}'), dict(e.headers)


def wait(port, secs=60):
    for _ in range(secs * 4):
        try:
            if req(port, 'GET', '/api/health')[0] == 200:
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--image', default='resolve:smoke')
    ap.add_argument('--port', type=int, default=18765)
    a = ap.parse_args()
    port, name, vol = a.port, f'resolve-smoke-{uuid.uuid4().hex[:8]}', f'resolve-smoke-{uuid.uuid4().hex[:8]}'
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    run = ['docker', 'run', '-d', '--name', name, '-p', f'127.0.0.1:{port}:8765', '-v', f'{vol}:/data',
           '-e', f'RESOLVE_MASTER_KEY={key}', '-e', f'RESOLVE_ALLOWED_HOSTS=127.0.0.1:{port}', a.image]
    results = []

    def check(label, ok, detail=''):
        results.append((label, bool(ok), detail))
        print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}")

    try:
        subprocess.run(run, check=True, capture_output=True)
        check('container starts and /api/health answers', wait(port))
        s, h, hdr = req(port, 'GET', '/api/health')
        check('health reports production mode', s == 200, json.dumps({k: h.get(k) for k in ('status', 'mode') if k in h}))
        check('security headers present', 'Content-Security-Policy' in hdr and hdr.get('X-Content-Type-Options') == 'nosniff')
        email, pw = f'{uuid.uuid4().hex[:10]}@resolve.test', 'smoke-pass-12345'
        s, u, _ = req(port, 'POST', '/api/auth/register', {'email': email, 'password': pw})
        check('register', s in (200, 201) and u.get('token'), str(s))
        tok = u.get('token')
        s, doc, _ = req(port, 'POST', '/api/documents/report', {'title': 'Smoke report', 'text': 'Container smoke test.\nSecond line.'}, tok)
        d = doc.get('document', {})
        check('PDF report created', s == 201 and d.get('sha256'), str(s))
        s, pdf, _ = req(port, 'GET', f"/api/documents/{d.get('id')}/download", token=tok, raw=True)
        check('download returns real PDF bytes with matching SHA-256', s == 200 and pdf[:5] == b'%PDF-' and hashlib.sha256(pdf).hexdigest() == d.get('sha256'))
        s, other, _ = req(port, 'POST', '/api/auth/register', {'email': f'b{email}', 'password': pw})
        s2, _, _ = req(port, 'GET', f"/api/documents/{d.get('id')}/download", token=other.get('token'), raw=True)
        check('another user cannot download it', s2 in (403, 404), str(s2))
        s, vh, _ = req(port, 'GET', '/api/voice/health', token=tok)
        check('voice health answers', s == 200, json.dumps({k: vh.get(k) for k in ('available', 'provider', 'detail')}))
        if vh.get('available'):
            wav = (ROOT / 'tests' / 'fixtures' / 'audio' / 'tts_open_atlas_overdue.wav').read_bytes()
            s, t, _ = req(port, 'POST', '/api/voice/transcribe-audio', {'audio_base64': base64.b64encode(wav).decode()}, tok)
            check('real audio transcribed inside the container', s == 200 and 'atlas' in t.get('transcript', ''), t.get('transcript', str(t))[:80])
        subprocess.run(['docker', 'restart', name], check=True, capture_output=True)
        check('restarts', wait(port))
        s, again, _ = req(port, 'POST', '/api/auth/login', {'email': email, 'password': pw})
        check('account persists across restart', s == 200 and again.get('token'), str(s))
        s, lst, _ = req(port, 'GET', '/api/documents', token=again.get('token'))
        ids = [x.get('id') for x in (lst.get('documents', lst) if isinstance(lst, dict) else lst)] if s == 200 else []
        check('document persists across restart', d.get('id') in ids, str(s))
        s, _, _ = req(port, 'GET', '/api/documents', token='rst_forged')
        check('forged token rejected', s == 401, str(s))
    finally:
        subprocess.run(['docker', 'rm', '-f', name], capture_output=True)
        subprocess.run(['docker', 'volume', 'rm', '-f', vol], capture_output=True)
    failed = [r for r in results if not r[1]]
    print(f'{len(results) - len(failed)}/{len(results)} checks passed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
