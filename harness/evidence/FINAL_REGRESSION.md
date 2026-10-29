# Final regression — Linux cloud container, 2026-10-29

Environment: Linux 6.18 (container), Python 3.13.17, fresh `.venv` from `requirements-dev.txt`, Playwright (pip) with the
pre-installed Chromium under `/opt/pw-browsers`. Native Windows input was **not** available: `WINDOWS_NATIVE_TESTS_NOT_RUN`.

## Command and result
```
python -m pytest tests -q --no-header -p no:cacheprovider -rs
244 passed, 12 skipped, 27 subtests passed in 362.38s        (0 failed, 0 errors)
```
Reported handoff baseline: 47 failed, 133 passed, 10 skipped, 11 errors. Same repo, same suite; no test deleted, skipped to hide a
failure, or weakened.

## Root causes found and fixed
| Symptom | Actual cause | Fix |
|---|---|---|
| ~58 failures/errors in every Playwright-backed test | The venv's pip Playwright expects a newer Chromium revision (1248) than the pre-installed build (1243); every `launch()` raised "Executable doesn't exist". (The handoff blamed an asyncio loop; with `-p no:asyncio` in `pytest.ini` that was not reproducible here.) | `cloud_browser._chromium_executable()` honours `RESOLVE_CHROMIUM_PATH`, else uses an installed `/opt/pw-browsers` build. |
| `test_fault_matrix` F02/F03 failed on Linux | They call the Win32 window API | Marked `skipUnless(win32)` with a `WINDOWS_NATIVE_TESTS_NOT_RUN` reason (environment class: windows-native). |
| `test_landing_ui` disabled Document buttons unexplained | Real UI defect (`docDownload`/`docPrint` had no `title`) | Added explanatory titles. |

## Skips (all 12, with reasons)
- 3× `tkinter unavailable on this host` (test_api_gui, test_demo_controller, test_demo_suite) — class: desktop-GUI.
- 2× test_fault_matrix F02/F03, 2× test_w1_notepad, 4× test_windows_adapter, 1× test_uia_adapter — class: windows-native.

## Groups (`pytest -m <group>`, tests collected)
unit 99 · document 25 · cloud 17 · voice 15 · security 35 · api 6 · browser_e2e 32 · windows_native 11. CI runs them as
independent jobs (`.github/workflows/ci.yml`); **that workflow has not been executed on GitHub yet**.

## New tests this sprint (all passing)
`test_production_security` (10), `test_voice_engine` (9), `test_brief_mission` (20), `test_brief_mission_api` (3),
`test_flagship_e2e` (1, real Chromium, CSP enforced), `test_hidden_failures` (13, see docs/HIDDEN_PROBLEMS.md), plus 5 new `test_cloud_browser` SSRF/scheme tests.
A later audit pass found and fixed 11 hidden failures (known demo password, startup crash without Playwright, verifier false-confidence, races, hung workers…).

## NOT verified (stays open)
- **Real speech recognition.** No STT model could be downloaded here (Hugging Face / Vosk hosts unreachable) and no TTS exists to
  synthesize fixtures. The E2E feeds a real WAV through Chromium's real `getUserMedia` path to a *fake local STT server*; it proves
  capture, WAV encoding, upload, consent, error handling — not recognition accuracy. BLOCKED until a provider is configured
  (`RESOLVE_VOSK_MODEL` or `RESOLVE_STT_URL`) and a human speaks into a real microphone.
- GitHub Actions run, any public deployment, Docker image build, physical printing, native Windows input.
- DNS-rebinding TOCTOU in the browser SSRF guard (see docs/SECURITY.md).
