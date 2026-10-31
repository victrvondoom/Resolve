# Voice: real speech-to-text

Path: browser `getUserMedia` → 16 kHz mono PCM WAV → `POST /api/voice/transcribe-audio` → validation
(`parse_wav`: size, length, sample rate, silence) → STT provider → **editable** transcript (`submitted: false`) →
the user reviews and submits → mission. Audio is processed in memory and never stored (`retained: false`).

## Providers
| Provider | How to enable | Audio leaves the server |
|---|---|---|
| Vosk (offline Kaldi) | `pip install vosk && python scripts/fetch_stt_model.py` (auto-discovered under `data/models/`) or `RESOLVE_VOSK_MODEL=<dir>` | no |
| OpenAI-compatible HTTP | `RESOLVE_STT_URL`, `RESOLVE_STT_API_KEY` | yes, per-request consent required |
| none | default | — (`/api/voice/health` says why; mic shows NOT CONFIGURED) |

The Vosk response includes real recognizer partial hypotheses (decoded in 0.25 s chunks), per-word confidence,
`low_confidence_words`, and the raw recognizer text. A small command-vocabulary repair (e.g. "over do" →
"overdue") is applied only within edit distance 1 (2 for a merged split word of 7+ letters) of a command word;
every repair is listed in `corrections` and shown in the UI before anything runs. Languages the model does not
support are refused (`UNSUPPORTED_LANGUAGE`), never guessed.

## Verified (measured on this host, 2026-10-31)
`python -m pytest tests/test_voice_real_stt.py -q` → **12 passed** with `vosk-model-small-en-us-0.15`:
- a real human recording (vosk-api `test.wav`) is recognized (opening and closing digits checked, word confidences 0–1);
- synthesized speech "Open Project Atlas and show my overdue tasks." → Vosk → transcript → `/api/computer/run` → real
  Chromium → `CONFIRMED` against the app database (over HTTP, production-mode server);
- "Find Alex from Finance, not Alex from Sales." keeps both departments; spoken "Stop." → `CANCEL` bound to the intent revision;
- noisy audio (10 dB SNR) still decodes or fails explicitly; silence → `EMPTY_SPEECH`; French → `UNSUPPORTED_LANGUAGE`; garbage → `UNSUPPORTED_AUDIO`.

Without the model the tests skip with `STT_MODEL_NOT_PRESENT` (never counted as a pass).

## Not verified
- A live microphone with a person speaking (no audio hardware here). Manual check: run the server with a model,
  open `/app`, press **Push to talk**, speak, and confirm the transcript appears for editing.
- Recognition accuracy on accents or real background noise beyond the synthetic SNR test.
- Wispr Flow: a separate desktop dictation tool that types into the command field. No SDK or API integration exists or is claimed.
