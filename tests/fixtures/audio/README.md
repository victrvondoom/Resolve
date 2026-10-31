# Audio fixtures for the real speech-to-text tests

| File | Source | What it is |
|---|---|---|
| `human_digits_vosk.wav` | `python/example/test.wav` from https://github.com/alphacep/vosk-api (Apache-2.0) | A real human voice reading digits, 16 kHz mono. |
| `tts_open_atlas_overdue.wav` | Synthesized with Piper TTS, voice `en_US-lessac-medium` (rhasspy/piper-voices), resampled to 16 kHz mono with ffmpeg | "Open Project Atlas and show my overdue tasks." |
| `tts_find_alex.wav` | same | "Find Alex from Finance, not Alex from Sales." |
| `tts_stop.wav` | same | "Stop." |

The TTS clips are synthetic speech, not a person at a microphone. They prove that real audio is decoded by a
real recognizer (Vosk) and the transcript drives a real mission. They do not prove live-microphone quality;
that remains a manual check (see `docs/VOICE.md`).

Regenerate a TTS clip:

    echo "Open Project Atlas and show my overdue tasks." | python -m piper -m en_US-lessac-medium.onnx -f raw.wav
    ffmpeg -y -i raw.wav -ac 1 -ar 16000 -sample_fmt s16 tts_open_atlas_overdue.wav
