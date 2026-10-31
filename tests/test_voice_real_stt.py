"""Real speech-to-text: recorded audio -> Vosk (offline Kaldi recognizer) -> transcript -> real mission.

No test double here. Audio fixtures are in tests/fixtures/audio (see its README for provenance: one real
human recording, three Piper-synthesized clips). Requires `pip install vosk` and a model
(`python scripts/fetch_stt_model.py`, or RESOLVE_VOSK_MODEL=<dir>). Without them every test is skipped with
the environment class STT_MODEL_NOT_PRESENT; that skip is never reported as a pass of real STT.
Live-microphone capture is not exercised here (no audio hardware in CI); see docs/VOICE.md.
"""
import array
import base64
import io
import os
import random
import sys
import tempfile
import unittest
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / 'src'))
import voice_engine  # noqa: E402
from voice_engine import VoiceEngine, VoiceError, VoskProvider  # noqa: E402
from command_router import interpret  # noqa: E402

AUDIO = ROOT / 'tests' / 'fixtures' / 'audio'
MODEL = os.environ.get('RESOLVE_VOSK_MODEL') or voice_engine.discover_vosk_model()
try:
    import vosk  # noqa: F401
    HAVE_VOSK = True
except ImportError:
    HAVE_VOSK = False
SKIP = None if (HAVE_VOSK and MODEL and os.path.isdir(MODEL)) else \
    'STT_MODEL_NOT_PRESENT: environment has no vosk package or model; run pip install vosk && python scripts/fetch_stt_model.py'


def wav_bytes(name):
    return (AUDIO / name).read_bytes()


def b64(raw):
    return base64.b64encode(raw).decode()


def with_noise(raw, snr_db, seed=7):
    with wave.open(io.BytesIO(raw)) as w:
        rate, pcm = w.getframerate(), w.readframes(w.getnframes())
    s = array.array('h'); s.frombytes(pcm)
    p_sig = sum(x * x for x in s) / len(s)
    sigma = (p_sig / (10 ** (snr_db / 10))) ** 0.5
    rnd = random.Random(seed)
    noisy = array.array('h', (max(-32768, min(32767, int(x + rnd.gauss(0, sigma)))) for x in s))
    out = io.BytesIO()
    with wave.open(out, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(noisy.tobytes())
    return out.getvalue()


@unittest.skipIf(SKIP, SKIP or '')
class RealVoskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = VoiceEngine(VoskProvider(MODEL))

    def tx(self, raw, language='en-US'):
        return self.engine.transcribe_b64(b64(raw), consent=False, language=language)

    def test_health_reports_real_offline_provider(self):
        h = self.engine.health()
        self.assertEqual((h['available'], h['provider'], h['offline'], h['audio_leaves_server']), (True, 'vosk', True, False))

    def test_human_recording_is_recognized(self):
        r = self.tx(wav_bytes('human_digits_vosk.wav'))
        # the speaker reads "one zero zero zero one nine zero two one zero zero one eight zero three"; the small
        # model mis-hears some middle digits, so require the opening and closing digits, not an exact string
        self.assertTrue(r['raw_transcript'].startswith('one zero zero zero one'), r['raw_transcript'])
        self.assertTrue(r['raw_transcript'].endswith('zero one eight zero three'), r['raw_transcript'])
        self.assertGreaterEqual(len(r['words']), 12)
        self.assertTrue(all(0 <= w['conf'] <= 1 for w in r['words']))
        self.assertFalse(r['retained'])
        self.assertEqual(len(r['audio_sha256']), 64)

    def test_partial_hypotheses_come_from_the_recognizer(self):
        r = self.tx(wav_bytes('human_digits_vosk.wav'))
        self.assertTrue(r['partials'], 'chunked decoding should yield partial hypotheses')
        self.assertTrue(r['raw_transcript'].endswith(r['partials'][-1]), (r['partials'], r['raw_transcript']))

    def test_spoken_command_routes_to_the_overdue_task(self):
        from computer_tasks import ComputerTaskRunner
        r = self.tx(wav_bytes('tts_open_atlas_overdue.wav'))
        self.assertIn('atlas', r['transcript'])
        self.assertIn('overdue', r['transcript'])
        if r['raw_transcript'] != r['transcript']:
            self.assertTrue(r['corrections'] and r['needs_review'])   # any repair is shown, never silent
        task, params = ComputerTaskRunner.classify(None, r['transcript'])
        self.assertEqual((task, params['project']), ('overdue_tasks', 'Atlas'))

    def test_spoken_disambiguation_keeps_both_departments(self):
        from computer_tasks import ComputerTaskRunner
        r = self.tx(wav_bytes('tts_find_alex.wav'))
        task, p = ComputerTaskRunner.classify(None, r['transcript'])
        self.assertEqual((task, p['first'].lower(), p['department'].lower(), p['exclude'].lower()), ('find_person', 'alex', 'finance', 'sales'))

    def test_spoken_stop_cancels_the_active_mission(self):
        r = self.tx(wav_bytes('tts_stop.wav'))
        self.assertEqual(r['transcript'], 'stop')
        out = interpret(r['transcript'], {'active_mission': {'id': 'm1', 'intent_revision': 3}}, source='voice')
        self.assertEqual(out['command'], 'CANCEL')
        self.assertEqual(out['args']['intent_revision'], 3)

    def test_noisy_audio_still_decodes_or_fails_explicitly(self):
        noisy = with_noise(wav_bytes('tts_open_atlas_overdue.wav'), snr_db=10)
        try:
            r = self.tx(noisy)
        except VoiceError as e:
            self.assertIn(e.code, ('NO_TRANSCRIPT', 'EMPTY_SPEECH'))
            return
        self.assertIn('atlas', r['transcript'])

    def test_unsupported_language_is_refused_not_guessed(self):
        with self.assertRaises(VoiceError) as c:
            self.tx(wav_bytes('tts_stop.wav'), language='fr-FR')
        self.assertEqual(c.exception.code, 'UNSUPPORTED_LANGUAGE')

    def test_silence_is_empty_speech(self):
        out = io.BytesIO()
        with wave.open(out, 'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b'\x00\x00' * 16000)
        with self.assertRaises(VoiceError) as c:
            self.tx(out.getvalue())
        self.assertEqual(c.exception.code, 'EMPTY_SPEECH')


@unittest.skipIf(SKIP, SKIP or '')
class RealVoiceToMissionHttpTests(unittest.TestCase):
    """Audio bytes over HTTP -> server-side Vosk -> transcript -> /api/computer/run -> real Chromium -> verified DB result."""

    @classmethod
    def setUpClass(cls):
        from test_production_security import ProdServer
        from test_web_api_g1 import free_port
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = ProdServer(cls.tmp.name, free_port(), extra={'RESOLVE_DIAGNOSTICS': '1', 'RESOLVE_VOSK_MODEL': MODEL}).start()
        _, u, _ = cls.srv.req('POST', '/api/auth/register', {'email': f'{uuid.uuid4().hex}@resolve.test', 'password': 'secure-pass-123'})
        cls.h = {'Authorization': f"Bearer {u['token']}"}

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()
        cls.tmp.cleanup()

    def test_health_is_real(self):
        s, h, _ = self.srv.req('GET', '/api/voice/health', headers=self.h)
        self.assertEqual((s, h['available'], h['provider']), (200, True, 'vosk'))

    def test_spoken_request_completes_a_verified_browser_task(self):
        s, t, _ = self.srv.req('POST', '/api/voice/transcribe-audio', {'audio_base64': b64(wav_bytes('tts_open_atlas_overdue.wav')), 'language': 'en-US'}, headers=self.h)
        self.assertEqual(s, 200, t)
        self.assertFalse(t['submitted'])           # transcript is shown for editing; nothing runs until the user submits
        s, r, _ = self.srv.req('POST', '/api/computer/run', {'command': t['transcript']}, headers=self.h)
        self.assertEqual(s, 200, r)
        self.assertEqual(r['status'], 'CONFIRMED')
        self.assertEqual(sorted(r['overdue']), ['Fix invoice rounding', 'Migrate legacy accounts', 'Ship onboarding flow'])

    def test_garbage_audio_is_rejected(self):
        s, e, _ = self.srv.req('POST', '/api/voice/transcribe-audio', {'audio_base64': b64(b'RIFF' + os.urandom(64))}, headers=self.h)
        self.assertEqual((s, e['error']['code'] if isinstance(e.get('error'), dict) else e.get('code')), (415, 'UNSUPPORTED_AUDIO'))


if __name__ == '__main__':
    unittest.main()
