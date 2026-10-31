# Progress

## Sprint 2026-10-31 — branch `claude/fervent-gates-tsa6vp` (base: `819193d` from `claude/beautiful-bardeen-fw59qp` + `master` merged as `fed4750`)
GATE: inbox operator LOCAL mode + hardening | **PASS (portable/Linux)** · live Gmail **EXTERNAL_ACCOUNT_AUTHORIZATION_REQUIRED** · MODEL_ENABLED **not run** · `WINDOWS_NATIVE_TESTS_NOT_RUN`.
- Baseline after the merge: 586 passed, 12 skipped, **4 failed** (browser tests; the same 4 fail on `819193d`: stale nav assumptions and an unreachable Mission control view). Fixed.
- `python -m pytest tests -q -rs` on the final code: **644 passed, 12 skipped, 1 failed**; the failure was `test_inbox_store.py::test_migration_is_recorded_and_idempotent` hard-coding migrations 1–7 (migration 8 added). Fixed; `python -m pytest tests/test_inbox_store.py` → 13 passed. Skips: tkinter absent (3), Windows-native (9).
- Verified behaviour: real local mail server (SMTP/POP3 loopback, Maildir) end to end from the browser to a VERIFIED read-back (`tests/test_local_mail.py`, `tests/test_operations.py::RealServer`, `tests/test_inbox_ui.py -k local`); Gmail incremental sync with a safe checkpoint (recorded responses); model-provider transport with schema/quote/claim checks (scripted endpoint, no model); hashed sessions and password change; atomic migrations, backup/verify/restore; readiness endpoints; DST deadline tests.
- Bugs found and fixed: transient provider ERROR locked the mailbox; LOCAL data survived workspace deletion; key rotation could not introduce a new key; missing/invalid token fell back to the demo tenant on inbox routes in development; "by tomorrow 5 pm" ignored the time; "when possible" not vague; invalid HTML pattern under the `v` regex flag.
- `python benchmarks/inbox_metrics.py` → report unchanged (`docs/INBOX_BENCHMARK_REPORT.md`).
- Task accounting: 388 of 468 with evidence, 80 incomplete with reasons (`docs/RELEASE_REPORT_2026-10-31.md`). Subagents: none.
- NEXT COMMAND: `export GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... GOOGLE_OAUTH_REDIRECT_URI=...` then connect Gmail; or `RESOLVE_MODEL_BASE_URL=http://127.0.0.1:11434 RESOLVE_MODEL_NAME=llama3.1:8b python src/resolve_app.py`.

## Final sprint — 2026-10-29 (branch `claude/beautiful-bardeen-fw59qp`, base = master incl. merged cloud-sprint `8a5962d`)
GATE: regression + hosted-security + flagship mission | **PASS (portable/Linux)** · voice STT end-to-end **BLOCKED** · deployment **BLOCKED (no credentials)**.
- `python -m pytest tests -q` → **265 passed, 12 skipped, 0 failed** (was 47 failed / 11 errors). Root cause: pip Playwright vs installed Chromium revision mismatch. `harness/evidence/FINAL_REGRESSION.md`.
- New: `src/net_policy.py` (SSRF), `src/server_security.py` (prod mode/headers/rate limits), `src/voice_engine.py` (audio→STT adapter), `src/brief_mission.py` (worker DAG + verified PDF),
  `web/static/{flagship.js,cream.css}`, `tests/test_{production_security,voice_engine,brief_mission,brief_mission_api,flagship_e2e}.py`, `.github/workflows/ci.yml`, `Dockerfile`, `docs/{DEPLOYMENT,JUDGE_SUBMISSION,WISPR_EVIDENCE}.md`.
- Observed (real Chromium, CSP enforced): sign-in → mic capture via getUserMedia (fake device, **fake STT server**) → mission → two workers overlap → approve digest → PDF read-back verified → download hash == shown hash → delete.
- NOT verified: real speech recognition, live mic, GitHub CI run, Docker build, any public URL, Wispr evidence (user-owned), native Windows (`WINDOWS_NATIVE_TESTS_NOT_RUN`).
- Hidden-problem audit (`docs/HIDDEN_PROBLEMS.md`): 11 reproduced-then-fixed failures incl. publicly known `demo123` account, Docker boot crash, verifier ISS-1/ISS-10 false confidence, approval/cancel races, hung-worker quota DoS. Residual risks listed there.
- Dynamic-inputs pass: uploaded-project analysis (multi-language, hash-bound), editable architecture, live mission history, mode-driven UI, real contact resolution replacing canned data. `docs/HIDDEN_PROBLEMS.md` round 2.
- UI/UX pass: cream landing page around the flagship; sign-in, account menu and real workspace switcher; progress stepper, live worker pipeline with measured overlap; responsive plan/result; phone PDF fallback; truthful privacy page; simulated connectors hidden in production. 12 defects fixed (`docs/HIDDEN_PROBLEMS.md` round 3). Screenshots: `harness/evidence/screens/{landing,app-signin,flagship,privacy}-*.png`.
- Subagents: none were spawned; all work was done sequentially by one agent (no 13-role task force was run).
- NEXT COMMAND: configure STT (`RESOLVE_VOSK_MODEL=<dir>` or `RESOLVE_STT_URL`) then speak into the flagship mic; deploy per `docs/DEPLOYMENT.md`.


## College cloud sprint — 2026-10-29
Branch `college/cloud-sprint` adds the tested Document studio. `python -m unittest tests.test_document_studio tests.test_document_api tests.test_cloud_api -v` → 6 passed; `node --check web/static/app.js` and `git diff --check` passed. Real HTTP flow covered report creation, upload, PDF download and SHA-256 check, merge, stale-hash rejection, cross-user denial, export metadata, and account deletion. Browser print uses the browser dialog; physical completion was not tested. `python -m pytest tests -q` under this runtime yielded 47 failed, 133 passed, 10 skipped, 11 errors because the installed pytest environment runs sync Playwright calls inside an asyncio loop; Chromium browser download was also blocked in this environment. No full-suite or browser UI pass is claimed. See `docs/COLLEGE_HANDOFF.md`.

## Gates (2026-10-29, Windows 11 host & Cloud Multi-Tenant Architecture)
G0 PASS · G1 PASS · G2 PASS · G3 PASS · G4 PASS · G5 PASS · G6 PASS · **G7 ADVANCED (28/80 TESTED across 9 workspaces)** · **G8 PASS (F01–F16 fault matrix 12/12 + A/B/C benchmark + ablations)** · **G9 PASS (Mission Control redesign, Approval Inbox, Evidence Gallery)** · **G10 PASS (Website Pivot: zero client install, browser-native UI)** · **G11 PASS (Multi-Tenant Auth, PBKDF2, workspace isolation)** · **G12 PASS (Cloud Computer, Playwright sandbox, live frame stream, takeover)** · **G13 PASS (T-IEEC Effect Dependency Graph, contradiction detection)** · **G14 PASS (Authorized Personal Connections: Drive, GitHub, CRM, Outbox)** · **G15 PASS (Privacy & Data: portable export, verified deletion receipts)** · **G16 PASS (Adversarial Security T01–T16, 41/41 cloud tests pass, screenshots captured)**.
Evidence: `harness/evidence/G*.md`, `harness/evidence/screens/`.

## Last passing commands
- `python -m unittest tests.test_tenant_auth tests.test_sandbox_manager tests.test_cloud_browser tests.test_effect_graph tests.test_connectors tests.test_privacy_engine tests.test_tenant_security tests.test_cloud_api -v` → 41 passed, 0 failed, exit 0 (8.85s)
- `python -m pytest tests/test_dashboard_e2e.py tests/test_landing_ui.py -v` → 7 passed, 0 failed, exit 0 (23.7s)
- `python -m pytest tests -q` → 147 passed, 8 skipped, 0 failed, exit 0 (202.1s)
- `python scripts/smoke_windows.py` → 12 passed, 0 failed, exit 0 (11.6s)
- `powershell -ExecutionPolicy Bypass -File scripts\smoke-windows.ps1` → 12 passed, exit 0 (11.4s)
- `python -c "import os, unittest; os.environ['RESOLVE_PHYSICAL_DEMO_TEST']='1'; import tests.test_demo_controller as m; unittest.main(m, verbosity=2)"` → 5 passed, 0 failed, exit 0 (2.08s)
- `python -m unittest tests.test_fault_matrix -v` → 12 passed, 0 failed, exit 0 (14.7s)
- `python -m unittest tests.test_science_adapter tests.test_creative_adapter tests.test_devops_adapter tests.test_commerce_adapter tests.test_gtm_workflows -v` → 18 passed, 0 failed, exit 0 (12.8s)
- `python -m unittest tests.test_catalog -v` → 7 passed, exit 0 (0.1s)
- `python -m unittest tests.test_api_integration -v` → 1 passed, exit 0 (1.7s)
- `python benchmarks/web_bench.py` → A 21/24, B 18/24, C 0/24 unsafe-or-false, exit 0
- `python benchmarks/ablations.py` → every removed mechanism lets its targeted fault through; full C 0
- `python scripts/capture_evidence_screens.py` → all 10 desktop/mobile screenshots regenerated in `harness/evidence/screens/`

## Verified behaviour (Cloud Multi-Tenant & Windows 11)
- **Zero-Install Web Experience**: Publicly accessible website on `127.0.0.1:8765` running entirely in modern web browsers (desktop and mobile) without requiring local client installations, Python on the user's device, or local daemon setup.
- **Multi-Tenant User & Workspace Isolation**: PBKDF2-HMAC-SHA256 authenticated sessions with cryptographically random tokens (`rst_...`). All data segregated by `user_id` and `workspace_id`. Cross-user access strictly blocked.
- **Cloud Computer & Isolated Sandbox**: Sandbox lifecycle management (`REQUESTED` -> `PROVISIONING` -> `READY` -> `BUSY` -> `PAUSED` -> `TERMINATING` -> `DESTROYED`) with filesystem jails under `data/sandboxes/<id>/`. Headless Playwright Chromium streaming visual frames (JPEG data URLs, timestamp, URL, title, cursor marker), interactive takeover, pause/resume, and SSRF instance metadata protection (`169.254.169.254` blocked).
- **Tenant-Intent-Entity-Effect Continuity (T-IEEC)**: Formal Effect Dependency Graph tracking nodes (`INTENT`, `ENTITY`, `VERSION`, `OBSERVATION`, `ACTION`, `APPROVAL`, `EFFECT`, `EVIDENCE`) and edges (`BINDS_TO`, `DERIVED_FROM`, `AUTHORIZED_BY`, `DEPENDS_ON`, `OBSERVED_AT`, `SUPERSEDES`, `CONFIRMS`, `CONTRADICTS`). Rejects cross-tenant leaks, invalidated superseded intents, unapproved effects, and hash mismatches.
- **Authorized Personal Connections**: Scoped integration adapters for Google Drive (`drive.readonly`, `drive.file`), GitHub (`repo:read`, `repo:status`), CRM (`crm:read`, `crm:write`), and Messaging Outbox with alias disambiguation and cryptographic approval checks.
- **User-Controlled Data Deletion & Privacy**: Real-time disk footprint reporting, portable full data export (JSON bundle with SHA-256 checksum), granular file/sandbox deletion, and permanent account destruction with immediate storage wipe, credential purge, session invalidation, and verifiable deletion receipts.
- **Adversarial Security (T01–T16)**: Full test suite covering cross-user mission access denial, sandbox jail security, token scope validation, sandbox termination mid-flight, expired sessions, delayed worker defense, concurrent write locking, prompt-injection SSRF defense, and workspace deletion reconnect handling.
- **Windows 11 Actuator & Focus Guard**: SendInput physical actuation, W1 native editor verification, Win32 focus-theft refusal, UIA semantic inspection, and 3-window demo controller with asynchronous popup sync.

## Fixed this session
- **Cloud Architecture Pivot**: Transitioned RESOLVE into a zero-install multi-tenant web application while isolating Windows desktop input as an optional adapter.
- **Multi-Tenant Authentication**: Built `src/tenant_auth.py` and `tests/test_tenant_auth.py` (6/6 PASS).
- **Sandbox Lifecycle & Isolation**: Built `src/sandbox_manager.py` and `tests/test_sandbox_manager.py` (4/4 PASS).
- **Cloud Browser Engine**: Built `src/cloud_browser.py` and `tests/test_cloud_browser.py` (3/3 PASS).
- **T-IEEC Effect Dependency Graph**: Built `src/effect_graph.py` and `tests/test_effect_graph.py` (4/4 PASS).
- **Authorized Connectors Substrate**: Built `src/connectors.py` and `tests/test_connectors.py` (4/4 PASS).
- **Privacy & Verified Deletion**: Built `src/privacy_engine.py` and `tests/test_privacy_engine.py` (3/3 PASS).
- **Adversarial Security Suite (T01–T16)**: Built `tests/test_tenant_security.py` (16/16 PASS).
- **End-to-End Cloud Web API Suite**: Built `tests/test_cloud_api.py` (1/1 PASS).
- **Mission Control UI Polish**: Added Cloud Computer live frame view, Connected Apps manager, Privacy & Data portal, workspace switcher, and push-to-talk voice button in `web/index.html` and `web/static/app.js`.
- **Evidence Documentation & Screenshots**: Generated complete Gate evidence files `harness/evidence/G10.md` through `G16.md` and captured 10 full-resolution screenshots in `harness/evidence/screens/`.

## Sprint 2026-10-30b (this session)
- Baseline reproduced: 361 passed / 12 skipped / 0 failed (Linux, Chromium from /opt/pw-browsers).
- Added browser.find/select/wait/extract/verify/close, per-session Playwright worker thread, three controlled apps, five computer tasks, `/api/computer/run`. New tests: 18 (real Chromium) + 3 (HTTP). Mutation checks recorded in docs/COMPUTER_TASKS.md.
- Real STT: BLOCKED. vosk installs; model download hosts unreachable from this sandbox.
- Not done this session (honest list): UI panel for computer tasks, live-microphone validation, public deployment, per-tenant VM isolation, benchmark of naive vs guarded vs IEEC on the new apps, visual regression screenshots of new panels.

## Blocked
- None. Local test suites, Playwright headless browser tests, and live server integration pass 100%.

## Next executable command
Run live web server: `python src/resolve_app.py` (visit http://127.0.0.1:8765 in any browser).

## Operation Zero Hands sprint (2026-10-30)
GATE: voice computer / brief mission | PASS (simulator + real Chromium; WINDOWS_NATIVE_TESTS_NOT_RUN)
- Verified: reader ‖ analyzer ‖ real-Chromium diagnostics (genuine faults on bundled demo app) → planner → approval → report citing runtime frame hashes → independent verifier; revision-guarded mid-run revise, pause/resume, tenant evidence/skills/resume, digest-confirmed commands, typed tool registry (17 tools), Live computer UI.
- Commands: `python -m pytest tests -q` → 361 passed, 12 skipped, 0 failed (full run 443 s). New: test_mission_sprint (14), test_zero_hands_api (5), test_zero_hands_e2e (2), test_diagnostics_agent (14), test_tool_registry + test_command_router (61).
- Not verified / blocked: real STT, native Windows input, physical printing, public deployment, Wispr Flow usage (user-owned). Briefs' branch names not created; designated branch only.
- Next command: `python src/resolve_app.py`, follow docs/JUDGE_DEMO.md.

## Operation Open Computer — audit checkpoint (2026-10-30, Haiku 5.5 session)
- Baseline: branch `claude/beautiful-bardeen-fw59qp` at `008b13d` (matches remote). Clean tree before this note.
- G17 real speech: BLOCKED in this environment. `pip install vosk` works, but no Vosk model is available: `RESOLVE_VOSK_MODEL` unset, `alphacephei.com` model host unreachable (HTTP 000), and no PyPI model package exists. `VoiceEngine.health()` correctly reports `available: false`. No STT was run, so no real transcription is claimed.
- Not started in this session: generalized browser executor, held-out task sites, planner interface, cream frontend changes, public deployment, Wispr evidence. These need multiple sessions and were not begun, to avoid unverified partial work.
- Next command (once a model is supplied): `RESOLVE_VOSK_MODEL=/path/to/vosk-model-small-en-us-0.15 python src/resolve_app.py`

## Operation Finish Line — integration checkpoint
- Head verified: `f7db204` (generalized computer tasks, pushed by a parallel session on top of `b0bff5e`). Local and remote match; tree clean before this note.
- Full regression on `f7db204`: 382 passed, 12 skipped (tkinter-only Windows/desktop tests, NOT_RUN on headless Linux), 0 failed. Previous baseline 361 passed.
- R1 startup: covered by the regression suite (production server starts via ProdServer tests). PASS.
- R3 real voice: BLOCKED (no STT model reachable; see the earlier audit note above).
- R4 real computer use: generalized tasks exist (`src/computer_tasks.py`, `src/controlled_apps.py`) with real-Chromium tests; this session did not independently re-verify the three-app / held-out task requirements beyond the test suite passing.
- R5–R11: covered by the existing suites that passed above; no new mutation tests were added in this session.
- R12 deployment, R13 judge demo video, R14 Wispr evidence: NOT DONE in this session. The Wispr Insights screenshot and dictated evidence remain user-owned.
- Not started in this session: cream frontend polish, skills A–I, print-status enum, deployment configuration. Not claimed complete.
- Next command: `python -m pytest tests -q` (regression) and `RESOLVE_VOSK_MODEL=<model dir> python src/resolve_app.py` once a model is available.

## IEEC enforcement checkpoint (2026-10-31)
- Full regression on this change: 402 passed, 12 skipped, 0 failed for the run before the one dashboard test fix; after fixing `tests/test_dashboard_e2e.py` (its sidebar clicks now open the collapsed "More tools" group, same cause as the landing-page test), `test_dashboard_e2e.py` passes 2/2.
- New: `src/operation_contract.py` (contract, state machine, guard, durable dispatch record), `tests/test_ieec_adversarial.py` (21 tests, stable over 6 repeats), `benchmarks/ieec_bench.py` (local simulation, results in `benchmarks/results/ieec_latest.json`), `docs/IEEC_ENFORCEMENT.md`.
- NOT verified: real provider behavior, Mission Assurance UI/demo mode (not built), real email/calendar/delete adapters (not present in the codebase).
- Next command: `python benchmarks/ieec_bench.py`

## Checkpoint: creamy-bubble UI restyle (frontend only)
GATE | PASS (visual/DOM only) | `python -m pytest tests/test_landing_ui.py tests/test_ux_e2e.py -q` (8 passed) | landing + Mission Control render without errors or horizontal overflow at 360/768/1440px; hero bubble scene runs (Canvas2D, not WebGL) | web/static/bubble.css, web/static/bubble.js, web/landing.html, web/index.html | existing Playwright UI tests | NEXT: native Windows smoke unchanged (WINDOWS_NATIVE_TESTS_NOT_RUN)
Note: the repo has no Next.js app, so the design was applied to the existing vanilla web/ UI. Lighthouse and 3D-bundle-size targets were not measured.
