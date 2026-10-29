"""Voice pipeline tests.

HONEST SCOPE: no real STT model is available in CI/cloud, so recognition itself is exercised through a
test-double provider and a local fake STT HTTP server. These tests prove audio validation, consent,
error handling, health reporting and the HTTP wire format. They do NOT prove speech recognition accuracy;
live-microphone / real-model verification is tracked as BLOCKED in harness/evidence/FINAL_REGRESSION.md.
"""
import base64
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
from voice_engine import VoiceEngine, VoiceError, SttProvider, HttpSttProvider, UnavailableProvider, parse_wav


def make_wav(seconds=1.0, rate=16000, amp=8000, channels=1, width=2):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(channels); w.setsampwidth(width); w.setframerate(rate)
        w.writeframes(b''.join(struct.pack('<h', int(amp * math.sin(2 * math.pi * 220 * i / rate))) for i in range(int(seconds * rate))) if width == 2 else b'\x00' * int(seconds * rate))
    return buf.getvalue()


def b64(raw):
    return base64.b64encode(raw).decode()


class DoubleProvider(SttProvider):
    name = 'test-double'
    def __init__(self, text='prepare the architecture report', off_device=False, fail=None):
        self.text, self.off_device, self.fail, self.calls = text, off_device, fail, 0
    def available(self):
        return True, 'test double'
    def transcribe(self, audio, language):
        self.calls += 1
        if self.fail:
            raise self.fail
        return self.text


class AudioValidationTests(unittest.TestCase):
    def code(self, raw):
        with self.assertRaises(VoiceError) as c:
            parse_wav(raw)
        return c.exception.code

    def test_valid_audio_parses_with_hash(self):
        a = parse_wav(make_wav())
        self.assertAlmostEqual(a.duration_s, 1.0, places=2)
        self.assertEqual(len(a.sha256), 64)

    def test_rejections(self):
        self.assertEqual(self.code(make_wav(amp=0)), 'EMPTY_SPEECH')            # empty/silent recording
        self.assertEqual(self.code(make_wav(seconds=0.05)), 'EMPTY_SPEECH')     # interrupted/too short
        self.assertEqual(self.code(make_wav(seconds=61, rate=8000)), 'AUDIO_TOO_LONG')
        self.assertEqual(self.code(b'not audio at all'), 'UNSUPPORTED_AUDIO')
        self.assertEqual(self.code(make_wav(channels=2)), 'UNSUPPORTED_AUDIO')
        self.assertEqual(self.code(make_wav(rate=96000, seconds=0.5)), 'UNSUPPORTED_AUDIO')
        self.assertEqual(self.code(make_wav(seconds=1)[:20000]), 'TRUNCATED_AUDIO')  # interrupted recording


class EngineTests(unittest.TestCase):
    def test_default_provider_is_honestly_unavailable(self):
        eng = VoiceEngine(UnavailableProvider())
        h = eng.health()
        self.assertFalse(h['available'])
        with self.assertRaises(VoiceError) as c:
            eng.transcribe_b64(b64(make_wav()), True)
        self.assertEqual(c.exception.status_code, 503)

    def test_transcript_is_editable_and_never_auto_submitted(self):
        res = VoiceEngine(DoubleProvider()).transcribe_b64(b64(make_wav()), False)
        self.assertEqual(res['transcript'], 'prepare the architecture report')
        self.assertFalse(res['submitted']); self.assertFalse(res['retained']); self.assertTrue(res['editable'])

    def test_off_device_provider_requires_consent_and_is_not_called_without_it(self):
        p = DoubleProvider(off_device=True)
        eng = VoiceEngine(p)
        with self.assertRaises(VoiceError) as c:
            eng.transcribe_b64(b64(make_wav()), False)
        self.assertEqual(c.exception.code, 'CONSENT_REQUIRED')
        self.assertEqual(p.calls, 0)
        self.assertEqual(eng.transcribe_b64(b64(make_wav()), True)['transcript'], 'prepare the architecture report')

    def test_empty_transcript_wrong_language_and_timeout(self):
        with self.assertRaises(VoiceError) as c:
            VoiceEngine(DoubleProvider(text='')).transcribe_b64(b64(make_wav()), True)
        self.assertEqual(c.exception.code, 'NO_TRANSCRIPT')
        with self.assertRaises(VoiceError) as c:
            VoiceEngine(DoubleProvider(fail=VoiceError('PROVIDER_TIMEOUT', 'timeout', 504))).transcribe_b64(b64(make_wav()), True)
        self.assertEqual(c.exception.status_code, 504)
        with self.assertRaises(VoiceError):
            VoiceEngine(DoubleProvider()).transcribe_b64('***not base64***', True)


class _FakeStt(http.server.BaseHTTPRequestHandler):
    seen = {}
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        _FakeStt.seen = {'auth': self.headers.get('Authorization'), 'ctype': self.headers.get('Content-Type'), 'has_riff': b'RIFF' in body, 'body': body}
        if self.path == '/slow':
            import time; time.sleep(3)
        out = json.dumps({'text': 'wire format ok'} if self.path != '/bad' else ['x']).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass


class HttpAdapterTests(unittest.TestCase):
    def setUp(self):
        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _FakeStt)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
    def tearDown(self):
        self.httpd.shutdown()

    def test_wire_format_and_response_parsing(self):
        res = VoiceEngine(HttpSttProvider(self.url + '/v1/audio/transcriptions', 'k')).transcribe_b64(b64(make_wav()), True)
        self.assertEqual(res['transcript'], 'wire format ok')
        self.assertEqual(_FakeStt.seen['auth'], 'Bearer k')
        self.assertTrue(_FakeStt.seen['ctype'].startswith('multipart/form-data'))
        self.assertTrue(_FakeStt.seen['has_riff'])

    def test_timeout_and_bad_response_and_unreachable(self):
        with self.assertRaises(VoiceError) as c:
            VoiceEngine(HttpSttProvider(self.url + '/slow', 'k', timeout=0.5)).transcribe_b64(b64(make_wav()), True)
        self.assertEqual(c.exception.code, 'PROVIDER_TIMEOUT')
        with self.assertRaises(VoiceError) as c:
            VoiceEngine(HttpSttProvider(self.url + '/bad', 'k')).transcribe_b64(b64(make_wav()), True)
        self.assertIn(c.exception.code, ('PROVIDER_ERROR',))
        with self.assertRaises(VoiceError) as c:
            VoiceEngine(HttpSttProvider('http://127.0.0.1:1/x', 'k')).transcribe_b64(b64(make_wav()), True)
        self.assertEqual(c.exception.code, 'PROVIDER_UNAVAILABLE')


class VoiceHttpTests(unittest.TestCase):
    def test_endpoint_requires_auth_reports_health_and_rejects_bad_audio(self):
        from test_production_security import ProdServer
        from test_web_api_g1 import free_port
        import uuid
        with tempfile.TemporaryDirectory() as tmp:
            srv = ProdServer(tmp, free_port()).start()
            try:
                self.assertEqual(srv.req('GET', '/api/voice/health')[0], 401)
                self.assertEqual(srv.req('POST', '/api/voice/transcribe-audio', {'audio_base64': b64(make_wav())})[0], 401)
                _, u, _ = srv.req('POST', '/api/auth/register', {'email': f'{uuid.uuid4().hex}@resolve.test', 'password': 'secure-pass-123'})
                h = {'Authorization': f"Bearer {u['token']}"}
                s, health, _ = srv.req('GET', '/api/voice/health', headers=h)
                self.assertEqual((s, health['available']), (200, False))
                s, body, _ = srv.req('POST', '/api/voice/transcribe-audio', {'audio_base64': b64(make_wav(amp=0)), 'consent': True}, headers=h)
                self.assertEqual((s, body['code']), (422, 'EMPTY_SPEECH'))
                s, body, _ = srv.req('POST', '/api/voice/transcribe-audio', {'audio_base64': b64(make_wav()), 'consent': True}, headers=h)
                self.assertEqual((s, body['code']), (503, 'PROVIDER_UNAVAILABLE'))
            finally:
                srv.stop()


if __name__ == '__main__':
    unittest.main()
