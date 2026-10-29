"""Server-side speech-to-text pipeline for the RESOLVE microphone.

Pipeline: browser getUserMedia -> 16-bit PCM WAV -> POST -> validate -> STT provider -> editable transcript.
Audio is held in memory only for the request and never written to disk (`retained: False`).

Providers (none is bundled; nothing is claimed unless one is configured and importable):
  * VoskProvider        - fully offline. Needs `pip install vosk` and RESOLVE_VOSK_MODEL=<model dir>.
  * HttpSttProvider     - OpenAI-compatible /audio/transcriptions endpoint (RESOLVE_STT_URL, RESOLVE_STT_API_KEY).
                          Audio leaves this server, so explicit user consent is required per request.
  * UnavailableProvider - default; transcription requests fail with 503 and health says why.

Wispr Flow is a separate desktop dictation tool that types into focused text fields; it has no
integration here beyond the plain text input.
"""
from __future__ import annotations

import array
import math
import sys
import base64
import hashlib
import io
import json
import os
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass

MAX_SECONDS = 60.0
MIN_SECONDS = 0.3
SILENCE_RMS = 80          # of 32768; below this the clip is treated as empty
MAX_AUDIO_BYTES = 2 * 1024 * 1024


class VoiceError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


@dataclass
class Audio:
    pcm: bytes
    rate: int
    duration_s: float
    rms: int
    sha256: str
    wav: bytes


def parse_wav(raw: bytes) -> Audio:
    if len(raw) > MAX_AUDIO_BYTES:
        raise VoiceError("AUDIO_TOO_LARGE", f"recording exceeds {MAX_AUDIO_BYTES // 1024} KiB", 413)
    try:
        with wave.open(io.BytesIO(raw), "rb") as w:
            if w.getsampwidth() != 2 or w.getnchannels() != 1:
                raise VoiceError("UNSUPPORTED_AUDIO", "expected mono 16-bit PCM WAV", 415)
            rate, frames = w.getframerate(), w.getnframes()
            pcm = w.readframes(frames)
    except (wave.Error, EOFError):
        raise VoiceError("UNSUPPORTED_AUDIO", "not a valid PCM WAV file", 415)
    if rate < 8000 or rate > 48000:
        raise VoiceError("UNSUPPORTED_AUDIO", f"unsupported sample rate {rate}", 415)
    if len(pcm) < frames * 2:
        raise VoiceError("TRUNCATED_AUDIO", "recording was cut off before it finished; record again", 422)
    duration = frames / rate
    if duration > MAX_SECONDS:
        raise VoiceError("AUDIO_TOO_LONG", f"recording longer than {int(MAX_SECONDS)}s", 413)
    if duration < MIN_SECONDS:
        raise VoiceError("EMPTY_SPEECH", "recording too short to contain speech", 422)
    samples = array.array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if sys.byteorder == "big":
        samples.byteswap()
    rms = int(math.sqrt(sum(x * x for x in samples) / len(samples))) if samples else 0
    if rms < SILENCE_RMS:
        raise VoiceError("EMPTY_SPEECH", "no speech detected (recording is silent)", 422)
    return Audio(pcm, rate, duration, rms, hashlib.sha256(raw).hexdigest(), raw)


class SttProvider:
    name = "none"
    offline = False
    off_device = False

    def available(self) -> tuple[bool, str]:
        return False, "no speech-to-text provider configured"

    def transcribe(self, audio: Audio, language: str) -> str:
        raise VoiceError("PROVIDER_UNAVAILABLE", "no speech-to-text provider configured", 503)


class UnavailableProvider(SttProvider):
    pass


class VoskProvider(SttProvider):
    name = "vosk"
    offline = True

    def __init__(self, model_dir: str):
        self.model_dir, self._model = model_dir, None

    def available(self):
        try:
            import vosk  # noqa: F401
        except ImportError:
            return False, "vosk is not installed (pip install vosk)"
        if not os.path.isdir(self.model_dir):
            return False, f"vosk model directory not found: {self.model_dir}"
        return True, "ready (offline)"

    def transcribe(self, audio, language):
        ok, why = self.available()
        if not ok:
            raise VoiceError("PROVIDER_UNAVAILABLE", why, 503)
        import vosk
        if self._model is None:
            self._model = vosk.Model(self.model_dir)
        rec = vosk.KaldiRecognizer(self._model, audio.rate)
        rec.AcceptWaveform(audio.pcm)
        return json.loads(rec.FinalResult()).get("text", "").strip()


class HttpSttProvider(SttProvider):
    name = "http-stt"
    off_device = True

    def __init__(self, url: str, api_key: str, model: str = "whisper-1", timeout: float = 30.0):
        self.url, self.api_key, self.model, self.timeout = url, api_key, model, timeout

    def available(self):
        return (True, "configured (audio is sent to the configured endpoint)") if self.url and self.api_key else (False, "RESOLVE_STT_URL/RESOLVE_STT_API_KEY missing")

    def transcribe(self, audio, language):
        boundary = uuid.uuid4().hex
        parts = []
        for k, v in (("model", self.model), ("language", language[:2]), ("response_format", "json")):
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
                      'Content-Type: audio/wav\r\n\r\n').encode() + audio.wav + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        req = urllib.request.Request(self.url, data=b"".join(parts), method="POST", headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
                if not isinstance(data, dict):
                    raise ValueError("unexpected response shape")
                return str(data.get("text", "")).strip()
        except TimeoutError:
            raise VoiceError("PROVIDER_TIMEOUT", "speech service timed out", 504)
        except urllib.error.URLError as exc:
            if isinstance(getattr(exc, "reason", None), TimeoutError):
                raise VoiceError("PROVIDER_TIMEOUT", "speech service timed out", 504)
            raise VoiceError("PROVIDER_UNAVAILABLE", f"speech service unreachable: {exc.reason}", 503)
        except (ValueError, json.JSONDecodeError):
            raise VoiceError("PROVIDER_ERROR", "speech service returned an invalid response", 502)


def provider_from_env(env=os.environ) -> SttProvider:
    if env.get("RESOLVE_VOSK_MODEL"):
        return VoskProvider(env["RESOLVE_VOSK_MODEL"])
    if env.get("RESOLVE_STT_URL"):
        return HttpSttProvider(env["RESOLVE_STT_URL"], env.get("RESOLVE_STT_API_KEY", ""), env.get("RESOLVE_STT_MODEL", "whisper-1"))
    return UnavailableProvider()


class VoiceEngine:
    def __init__(self, provider: SttProvider | None = None):
        self.provider = provider or provider_from_env()

    def health(self) -> dict:
        ok, why = self.provider.available()
        return {"available": ok, "provider": self.provider.name, "offline": self.provider.offline,
                "audio_leaves_server": self.provider.off_device, "detail": why,
                "audio_retention": "none (audio is processed in memory and discarded)",
                "wispr_flow": "separate desktop dictation tool; works by typing into the text box, no integration"}

    def transcribe_b64(self, audio_b64: str, consent: bool, language: str = "en-US") -> dict:
        try:
            raw = base64.b64decode(audio_b64, validate=True)
        except (ValueError, TypeError):
            raise VoiceError("BAD_REQUEST", "audio_base64 is not valid base64")
        audio = parse_wav(raw)
        if self.provider.off_device and not consent:
            raise VoiceError("CONSENT_REQUIRED", "this provider sends audio off the server; explicit consent is required", 403)
        ok, why = self.provider.available()
        if not ok:
            raise VoiceError("PROVIDER_UNAVAILABLE", why, 503)
        text = self.provider.transcribe(audio, language)
        if not text:
            raise VoiceError("NO_TRANSCRIPT", "speech was detected but nothing could be recognized; try again or type the command", 422)
        return {"transcript": text, "provider": self.provider.name, "offline": self.provider.offline,
                "duration_s": round(audio.duration_s, 2), "audio_sha256": audio.sha256, "retained": False,
                "editable": True, "submitted": False}
