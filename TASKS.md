# Tasks

## Inbox-to-Commitment Operator (ticked only when verified; see docs/INBOX_OPERATOR_TRACKER.md)
- [x] Semantic fixes: timezone deadlines with clock times, owners from the mailbox, obligation revisions, duplicate control (`tests/test_inbox_intel.py`, `tests/test_inbox_service.py`, `tests/test_inbox_api.py`)
- [x] Visible operation marker removed; Message-ID correlation; read-back of exact content (`tests/test_send_broker.py`)
- [x] Approval bound to current content, single use, refused after change; mutation-tested duplicate protection (`tests/test_send_broker.py::MutationCheck`)
- [x] HTTP API under /api/inbox/* with server-side identity and tenant scoping (`tests/test_inbox_api.py`, `docs/INBOX_API.md`, `tests/test_inbox_docs.py`)
- [x] Gmail OAuth start, PKCE, single-use state, callback, sealed tokens, revocation and refresh states (recorded responses)
- [x] Morning brief, inbox, thread detail, commitments, drafts, activity, voice rules, connections, settings, replay lab (`tests/test_inbox_ui.py`, `docs/screenshots/inbox/`)
- [x] Replay provider on the same pipeline; simulated sends labelled SIMULATED; failure injection (`tests/test_inbox_api.py`)
- [x] Email text sealed at rest; no plaintext in the database file after a full flow (`test_email_text_never_appears_in_plaintext_in_the_database_file`)
- [x] Privacy export and deletion include inbox data (`test_export_includes_inbox_data_and_no_credentials`)
- [x] 32 labelled scenarios written before scoring; generated benchmark report (`benchmarks/inbox_metrics.py`, `docs/INBOX_BENCHMARK_REPORT.md`)
- [ ] Live Gmail round trip (BLOCKED: OAuth credentials and an authorized test account)
- [x] LOCAL mode on a real local mail server: SMTP submission + POP3 read-back over loopback sockets, Maildir on disk, local-only delivery; same sync/broker/verifier (`tests/test_local_mail.py`, browser `test_local_mail_journey_through_the_ui_ends_verified`, `docs/screenshots/inbox/13_local_send_verified.png`)
- [x] Model-provider layer: OpenAI-compatible endpoint path, schema validation, quote verification against stored messages, fallback (`tests/test_model_provider.py`; transport tested against a scripted HTTP server, no model run)
- [ ] Model-assisted understanding with a real model run (BLOCKED: no model runtime in this environment; set RESOLVE_MODEL_BASE_URL/RESOLVE_MODEL_NAME)
- [ ] Model-generated drafts (NOT DONE: drafts stay rules-only; model analysis is advisory)
- [x] Inbox routes need an explicit valid token in every mode; session tokens stored hashed; password change revokes every session (`tests/test_operations.py`)
- [x] Atomic migrations with WAL, migration status, backup/verify/restore CLI (`tests/test_operations.py`, `scripts/resolve_admin.py`)
- [x] Liveness and readiness endpoints (`/api/livez`, `/api/readyz`; `tests/test_operations.py::RealServer`)
- [x] One sync at a time per mailbox (`test_concurrent_sync_of_one_mailbox_is_refused_not_duplicated`)
- [x] Workspace deletion and export include LOCAL data; credentials never exported (`test_workspace_deletion_and_export_cover_local_data`)
- [ ] Wispr Flow recording as evidence (user-owned; see docs/WISPR_DEMO_SCRIPT.md)
- [ ] Follow-up reminders delivered (NOT DONE: recorded only)
- [x] Persistence test for corrections and voice rules across restart (`test_rules_and_corrections_persist_across_restart`)
- [ ] Unread filter (NOT DONE: label not stored)


## Final sprint 2026-10-29 (ticked only when verified by tests above)
- [x] Full-suite regression diagnosed and repaired (231 passed / 12 skipped with reasons / 0 failed)
- [x] Production mode: no shared token, no demo fallback, host allow-list, startup validation, headers, rate limits, cross-tenant negative tests
- [x] Cloud browser SSRF policy: scheme allow-list, address classification, redirect-hop validation, sandbox flag off by default, browser disabled in prod without isolation
- [x] Brief→analysis→architecture→verified PDF mission: concurrent workers, approval digest, independent read-back, chain invariant, cancel/late-result/delete races
- [x] In-page microphone capture + consent + audio validation + provider adapter (tested with fake STT only)
- [x] Cream theme + flagship UI + account gate, real-browser E2E with screenshots
- [x] Upload-your-own-project analysis (multi-language), editable architecture, live history, mode-driven UI, real contact resolution
- [x] UI/UX pass: cream landing, account menu + sign-out, real workspace switcher, stepper, responsive mobile, phone PDF fallback, truthful privacy page (`test_ux_e2e`)
- [x] Generalized browser tools: `browser.find/select/wait/extract/verify/close` (+ existing 7) bound to a real Chromium session; every Playwright call runs on the session's own worker thread (fixes "cannot switch thread" for any caller but the creator)
- [x] Three controlled apps with persisted records (project board, research portal, contact workspace) and five supported computer tasks with exact-digest approval, stale-target refusal and database-side verification (`tests/test_computer_tasks.py`, `tests/test_computer_api.py`, mutation-checked)
- [x] `/api/computer/run` Live computer panel (run, approve, cancel) — `tests/test_computer_ui.py` (passes in the 694-test run)
- [ ] Real STT re-verified on current code (recorded audio only; historically DONE and merged via PR #12; NOT re-run in the 2026-10-31 closeout: model download blocked, 12 tests skipped. Live microphone NEVER tested. Original note: `vosk` installs but model hosts `alphacephei.com`, `huggingface.co` are unreachable from this sandbox, so no real transcription could run)
- [ ] Public deployment reachable by judges
- [ ] GitHub Actions run green
- [ ] Wispr Flow Insights screenshot (user), two-minute video (user)
- [ ] Voice bug-report skill, persona skill, spec-studio editing, per-tenant VM isolation (PLANNED). Document Studio reorder/extract/compare/print-status are implemented (`tests/test_document_studio.py`, `tests/test_document_print.py`; print status never claims a physical printer confirmed)

## College cloud sprint
- [x] PDF upload, report creation, page rotation/removal, merge, version guard, authenticated download, workspace/account purge with focused tests
- [x] Document studio UI with browser preview, download and print dialog
- [ ] Production account UX, audio transcription, hosted tenant runtime and real service OAuth

## Implemented and checked
- [x] Local dashboard/API, health route, SQLite WAL, startup scripts, and local demo fixtures
- [x] Typed contracts, mission revision history, narrow stop/send interpretation, same-name and report-version constraints
- [x] Approval digest, action-scoped atomic local effects, causal ledger, uncertain restart recovery, independent read-only reconciliation
- [x] Two concurrent read-only agent specialists, task DAG, cancellation, task events, cursor lease and fencing
- [x] Win32 observation snapshots, real input, controlled GUI/API physical tests, screenshot evidence, emergency stop
- [x] Browser-dependent dictation, exact demo commands, optional spoken feedback
- [x] 80-concept/10-workspace PLANNED registry and functional dashboard browser
- [x] Seeded eight-condition SQLite benchmark with separate ground truth, raw data, and aggregate report

## Added 2026-10-28 (verified on Linux; see harness/evidence)
- [x] Three separate browser fixture apps with hidden ground truth and test-mode-only faults
- [x] Playwright IEEC mission runner: ambiguity block, digest approval, stale-attachment invalidation, lost-ack reconcile, correction supersede
- [x] `/api/web/*` routes + SSE ledger stream + dashboard panel (UI E2E in headless Chromium)
- [x] Executor lease across concurrent missions with fencing check (with ablation)
- [x] Browser A/B/C benchmark (4 scenarios × 6 seeds) with oracle separate from verifier

## Added (second pass, verified on Linux & Windows)
- [x] Utterance classifier + revision protocol, spoken approval bound to presented digest, correction/cancel (W7, typed transcripts)
- [x] Supervisor with 13 tool-bounded roles, parallel fan-out, entity gate, separate verifier, retry, supersede (G5)
- [x] Recorded browser skills: record → compile → approve → replay → repair proposal (W8)
- [x] Sandboxed file worker with dry-run, stale-plan guard, undo after restart (W9)
- [x] 80-concept catalogue with validator; 28 TESTED across 9 workspaces (G7)
- [x] Ablations for six mechanisms; SIGKILL crash/restart test (W12)
- [x] 3D landing page and redesigned Mission Control with Approval Inbox, Evidence Gallery, and Interactive Viewport (G9)
- [x] W1 Notepad / focus-steal / UIA on real Windows (verified via smoke_windows.py and smoke-windows.ps1) (G2)
- [x] UI Automation semantic element tree (Windows) — verified via test_uia_adapter.py (G2)
- [x] Notepad type+save+verify and focus-steal refusal on Windows (G2)
- [x] Adversarial Fault Matrix (F01–F16) with 12 unit tests exercising all 16 fault categories with independent oracle verifiers (G8)
- [x] Formal academic research paper `docs/RESEARCH.md` with zero fabricated citations (G8)
- [x] Real cross-workspace execution adapters: ScienceAdapter (IEEE 1584/seismic) and CreativeAdapter (3D OBJ/walkthrough) (G7)
- [x] REST API endpoints for `/api/system/health`, `/api/capabilities`, `/api/agents`, `/api/approvals`, `/api/skills/{id}/run`, and `/api/evidence`
- [x] Multi-window guarded physical demo controller (`test_demo_controller.py`) with asynchronous focus theft recovery and outbox SHA-256 verification (G2/G9)
- [x] Real high-resolution screenshot evidence generated in `harness/evidence/screens/` (G9)

## Added (third pass: Cloud-Native Multi-Tenant Website Pivot, G10–G16)
- [x] Zero-install browser-native web application architecture running on 127.0.0.1:8765 (G10)
- [x] Multi-tenant PBKDF2 authentication, workspace segregation, and cryptographically secure session tokens in `src/tenant_auth.py` (G11)
- [x] Cloud computer and sandbox manager with isolated filesystem jails, quota controls, and lifecycle state machine in `src/sandbox_manager.py` (G12)
- [x] Isolated Playwright cloud browser engine with live JPEG frame streaming, interactive cursor aiming, and SSRF metadata defense in `src/cloud_browser.py` (G12)
- [x] Tenant-Intent-Entity-Effect Continuity (T-IEEC) and formal Effect Dependency Graph with mathematical invariant checks in `src/effect_graph.py` (G13)
- [x] Authorized Personal Connections framework for Google Drive, GitHub, CRM, and Messaging Outbox with alias disambiguation in `src/connectors.py` (G14)
- [x] User-controlled data deletion and portable export bundle with SHA-256 proof in `src/privacy_engine.py` (G15)
- [x] Adversarial Multi-Tenant Security Suite (T01–T16) covering cross-user denial, sandbox jail escapes, prompt-injection SSRF, and delayed worker protection in `tests/test_tenant_security.py` (G16)
- [x] End-to-end cloud web API test suite exercising register -> sandbox -> browser -> connectors -> voice correction -> export -> account deletion in `tests/test_cloud_api.py` (G16)
- [x] Redesigned Mission Control interface with Cloud Computer, Connected Apps, Privacy & Data, workspace switcher, and push-to-talk voice button in `web/index.html` and `web/static/app.js` (G10–G16)
- [x] Complete suite of 10 high-resolution screenshots generated in `harness/evidence/screens/` (G16)
- [x] Browser diagnostics agent + controlled faulty demo app (real Chromium), exact-origin NetPolicy allowlist (Operation Zero Hands)
- [x] Revision-guarded mission engine: mid-run revise, pause/resume, evidence frames, tenant skills, resume, digest-confirmed commands, typed tool registry, Live computer UI

## Outstanding gates / future work
- [ ] Connect production OAuth credentials for Google Workspace and GitHub when cloud provider API keys are provisioned
- [ ] Implement remaining workspace adapters toward all 80 concepts TESTED (currently 28/80 TESTED)
- [ ] Packaging, installer, demo video and authentic Wispr Flow Insights capture

## Release closeout 2026-10-31 (ticked only with the command output in PROGRESS.md)
- [x] Computer benchmark A naive / B target-guard / C RESOLVE moved into `benchmarks/` + `tests/`, run for real (105 runs), report `docs/COMPUTER_BENCHMARK.md`; `tests/test_computer_bench.py` 8 passed
- [x] Mutation checker re-run on this tree: 22/22 KILLED (`docs/MUTATION_REPORT.md`)
- [x] Full suite `python -m pytest tests -q`: 694 passed, 24 skipped (reasons recorded), 0 failed
- [ ] Docker image smoke 13/13 re-run on current code (BLOCKED: no Docker daemon in the sandbox; 13/13 is a historical 2026-10-31 result)
- [ ] Real Vosk tests re-run on current code (BLOCKED: model host unreachable; 12 skipped)
- [ ] Live microphone test with physical hardware (NOT STARTED — user)
- [ ] Public deployment on Render with `RESOLVE_MASTER_KEY` (BLOCKED — user account)
- [ ] GitHub Actions run observed green (NOT MEASURED)
- [ ] Wispr Flow Insights screenshot and two-minute demo recording (NOT STARTED — user-owned evidence)
- [ ] Live Gmail and MODEL_ENABLED results (BLOCKED/NOT MEASURED: need OAuth credentials / model endpoint)

## UI rescue 2026-10-31
- [x] App opens with no Google, no key and no internet; external requests blocked in `tests/test_offline_local_mode.py`
- [x] Connector label comes from real connection state; Gmail never shown as connected unless connected
- [x] Two areas (Workbench default, Operations); old routes still work
- [x] Outbox view; Workbench home reads live server state
- [x] Inbox stylesheets restored (merge regression)
- [x] README/landing/app wording about Wispr Flow corrected (used on the project; no SDK)
- [ ] React/TypeScript frontend and npm build (NOT STARTED)
- [ ] 3D architecture visualizer reflecting real service health (NOT STARTED)
- [ ] Agent timeline with parsed results; skill editor and runner; evidence gallery; research reports from supplied PDFs (NOT STARTED)
- [ ] Benchmark workspace with a Run action; model selector that switches the backend model (NOT STARTED)
- [ ] `.eml`/`.mbox` import; failed-delivery history in the Outbox (NOT STARTED)
- [ ] Narrated, subtitled demo video (PARTLY: regenerated with captions from the new UI, no audio)
