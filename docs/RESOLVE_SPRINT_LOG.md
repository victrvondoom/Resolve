# RESOLVE sprint log — cinematic intro, partner identity, encryption at rest

Honest record: each line is something that was built AND run. Nothing here is planned work.

## Completed and verified
- **Intro sequence, in order:** two single-colour 3D blocks enter from opposite sides and assemble → "RESOLVE" → "Wispr & Solve" → Wispr Flow mark (text fallback) → WeMakeDevs mark (text fallback) and "No Hands: The Wispr Challenge". Skip, Escape and replay work; reduced motion skips it. Tests: `tests/test_intro_ui.py` (6 tests, incl. scene order and fallbacks).
- **Official brand route:** `/static/brand/<file>` serves only files placed in `web/static/brand/` (path-checked). Tests cover the fallback path.
- **Encryption at rest (partial):** AES-256-GCM via the `cryptography` package, per-user/workspace keys derived with HKDF, versioned key IDs, record-bound associated data. Covers PDFs (`src/document_studio.py`), project archives (`src/project_store.py`) and mission evidence frames (`src/brief_mission.py`). Migration functions seal existing plaintext files and rows. Tests: `tests/test_at_rest.py` (12 tests), `tests/test_zero_hands_api.py::test_evidence_frames_are_sealed_in_the_database_file`.
- **Production key policy:** production refuses to start without `RESOLVE_MASTER_KEY`. The development key file is created with mode 0600 and is refused if other users can read it.

## Found and fixed during this sprint
- The first production-mode test server failed to start because it lacked a key. The harness now passes a test-only 32-byte key. Production behaviour itself was not changed.
- The intro showed the partner name twice (fallback text plus caption). The caption is now hidden when the fallback is used.
- A landing-page console check treated the expected 404 for a missing optional logo as a failure. It now ignores only that specific case.

## Not done (blocked or out of scope for this sprint)
- Official Wispr Flow and WeMakeDevs logos: **blocked** (partner sites unreachable from the build environment). See `docs/BRAND_ASSETS.md`.
- Y Combinator and Bolna icons: **not attempted** (same network block); not used anywhere.
- Database encryption: **not implemented.** SQLite rows and filenames remain plaintext. This would need SQLCipher or a managed database; it is not claimed.
- The 250-task tracker, the "memory graph" of per-user context, the 3D video, and the full 3D application redesign: **not started.** They are large and were not completed here.
- Three.js / WebGL scenes: **not used**; the intro and device model are CSS 3D.
