# Tasks

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

## Outstanding gates / future work
- [ ] Local/offline speech, microphone verification, dependable barge-in and spoken disambiguation (mic hardware dependent)
- [ ] Remaining specialist agents, durable handoffs, policy scopes, and resource telemetry
- [ ] Implement remaining workspace adapters toward all 80 concepts TESTED (currently 28/80 TESTED)
- [ ] Packaging, installer, demo video and authentic Wispr Flow Insights capture
