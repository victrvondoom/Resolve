"""Server-side speech-to-text pipeline for the RESOLVE microphone.

Pipeline: browser getUserMedia -> 16-bit PCM WAV -> POST -> validate -> STT provider -> editable transcript.
Audio is held in memory only for the request and never written to disk (`retained: False`).

Providers (no model is committed; nothing is claimed unless one is configured and importable):
  * VoskProvider        - fully offline. Needs `pip install vosk` and a model: RESOLVE_VOSK_MODEL=<model dir>, or
                          `python scripts/fetch_stt_model.py` which places one under data/models/ (auto-discovered).
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
    languages: tuple = ()       # empty = provider decides (e.g. a hosted multilingual model)
    last_detail: dict = {}

    def available(self) -> tuple[bool, str]:
        return False, "no speech-to-text provider configured"

    def transcribe(self, audio: Audio, language: str) -> str:
        raise VoiceError("PROVIDER_UNAVAILABLE", "no speech-to-text provider configured", 503)


class UnavailableProvider(SttProvider):
    pass


class VoskProvider(SttProvider):
    name = "vosk"
    offline = True
    languages = ("en",)   # the bundled-by-script model is English; other models declare their own language

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
        rec.SetWords(True)
        # Feed in 0.25 s chunks so partial hypotheses are real recognizer output, not a re-split of the final text.
        step = max(2, int(audio.rate * 0.25) * 2)
        partials, words, texts = [], [], []
        for i in range(0, len(audio.pcm), step):
            if rec.AcceptWaveform(audio.pcm[i:i + step]):
                seg = json.loads(rec.Result())
                texts.append(seg.get("text", "")); words += seg.get("result", [])
            else:
                part = json.loads(rec.PartialResult()).get("partial", "")
                if part and (not partials or partials[-1] != part):
                    partials.append(part)
        seg = json.loads(rec.FinalResult())
        texts.append(seg.get("text", "")); words += seg.get("result", [])
        self.last_detail = {"partials": partials[-8:],
                            "words": [{"word": w.get("word"), "conf": round(float(w.get("conf", 0)), 3)} for w in words]}
        return " ".join(t for t in texts if t).strip()


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


MODELS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "models")


def discover_vosk_model(models_dir: str = MODELS_DIR) -> str | None:
    """A model fetched by scripts/fetch_stt_model.py (a directory containing am/ and conf/)."""
    if not os.path.isdir(models_dir):
        return None
    for name in sorted(os.listdir(models_dir)):
        d = os.path.join(models_dir, name)
        if name.startswith("vosk-model") and os.path.isdir(os.path.join(d, "am")) and os.path.isdir(os.path.join(d, "conf")):
            return d
    return None


def provider_from_env(env=os.environ) -> SttProvider:
    if env.get("RESOLVE_VOSK_MODEL"):
        return VoskProvider(env["RESOLVE_VOSK_MODEL"])
    if env.get("RESOLVE_STT_URL"):
        return HttpSttProvider(env["RESOLVE_STT_URL"], env.get("RESOLVE_STT_API_KEY", ""), env.get("RESOLVE_STT_MODEL", "whisper-1"))
    if env.get("RESOLVE_STT_DISABLE_DISCOVERY") != "1":
        found = discover_vosk_model()
        if found:
            return VoskProvider(found)
    return UnavailableProvider()



# Words the supported commands depend on. Recognizers often split or mis-hear rare words
# ("overdue" -> "over do"). Correction is limited to merging/replacing tokens that are within edit
# distance 1 (2 for a merged split word of 7+ letters) of a vocabulary word of length >= 6; every change is returned so the user sees it in the
# editable transcript before anything is submitted. The raw recognizer output is always kept.
COMMAND_VOCABULARY = ("overdue", "atlas", "project", "report", "research", "portal", "finance", "sales",
                      "architecture", "mermaid", "workspace", "document", "summarize", "approve", "inventory",
                      "diagnostics", "specification", "contacts", "resolve")


def _lev(a: str, b: str, cap: int = 3) -> int:
    if abs(len(a) - len(b)) >= cap:
        return cap
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return min(prev[-1], cap)


def correct_command_vocabulary(text: str, vocab=COMMAND_VOCABULARY) -> tuple[str, list]:
    """Only bare lower-case alphabetic tokens are candidates; punctuation, case and anything else is kept as-is."""
    toks, out, changes, i = text.split(), [], [], 0
    long_vocab = [v for v in vocab if len(v) >= 6]
    plain = lambda t: t.isalpha() and t.islower()
    while i < len(toks):
        t = toks[i]
        if i + 1 < len(toks) and plain(t) and plain(toks[i + 1]) and t not in vocab and toks[i + 1] not in vocab:
            joined = t + toks[i + 1]
            # a split word: two adjacent tokens merging to within 1 edit (2 for 7+ letters) of a vocabulary word
            hit = next((v for v in long_vocab if _lev(joined, v) <= (2 if len(v) >= 7 else 1)), None)
            if hit:
                out.append(hit); changes.append({"from": f"{t} {toks[i + 1]}", "to": hit}); i += 2
                continue
        hit = next((v for v in long_vocab if plain(t) and t != v and len(t) >= 5 and t not in vocab and _lev(t, v) <= 1), None) if plain(t) else None
        if hit:
            out.append(hit); changes.append({"from": t, "to": hit})
        else:
            out.append(t)
        i += 1
    return " ".join(out), changes


class VoiceEngine:
    def __init__(self, provider: SttProvider | None = None):
        self.provider = provider or provider_from_env()

    def health(self) -> dict:
        ok, why = self.provider.available()
        return {"available": ok, "provider": self.provider.name, "languages": list(self.provider.languages), "offline": self.provider.offline,
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
        lang = (language or "en-US").split("-")[0].lower()
        if self.provider.languages and lang not in self.provider.languages:
            raise VoiceError("UNSUPPORTED_LANGUAGE", f"the configured {self.provider.name} model supports {', '.join(self.provider.languages)}; type the command or configure a model for '{lang}'", 422)
        self.provider.last_detail = {}
        raw_text = self.provider.transcribe(audio, language)
        if not raw_text:
            raise VoiceError("NO_TRANSCRIPT", "speech was detected but nothing could be recognized; try again or type the command", 422)
        text, corrections = correct_command_vocabulary(raw_text)
        detail = dict(self.provider.last_detail or {})
        words = detail.get("words") or []
        low = [w["word"] for w in words if w.get("conf", 1) < 0.6]
        return {"transcript": text, "raw_transcript": raw_text, "corrections": corrections,
                "partials": detail.get("partials", []), "words": words, "low_confidence_words": low,
                "needs_review": bool(low or corrections),
                "provider": self.provider.name, "offline": self.provider.offline, "language": lang,
                "duration_s": round(audio.duration_s, 2), "audio_sha256": audio.sha256, "retained": False,
                "editable": True, "submitted": False}
