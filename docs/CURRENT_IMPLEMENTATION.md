# RESOLVE — Current Implementation Status Map

Date: 2026-10-28
Environment: Windows 11 Build 26100 (x86_64), Python 3.12.10, Node v24.14.0
Interactive Desktop Session: Yes (Session ID 3, `WinSta0\default`)
Local Git Commit: `a75b27e`

## Subsystem Classification

| Subsystem | Components | Status | Executable Evidence & Tests |
|---|---|---|---|
| **Windows Input & Cursor Control** | `src/windows_adapter.py`, `src/uia_adapter.py` | **VERIFIED** | Real `SendInput` pointer moves, clicks, keyboard typing, Unicode injection, 20ms modifier chords, optical jitter tolerance, desktop attachment (`WinSta0\default`), foreground lock bypass via `AttachThreadInput`, emergency stop. Tests: `tests/test_windows_adapter.py`, `tests/test_uia_adapter.py`, `scripts/smoke_windows.py` (12/12 PASS). |
| **W1 Notepad & Editor Automation** | `src/notepad_w1.py`, `src/native_editor_fixture.py` | **VERIFIED** | Real Win32 editor fixture matching Notepad semantics, Ctrl+S save to disk, re-read byte verification, focus-theft refusal blocks input without data corruption. Tests: `tests/test_w1_notepad.py` (5/5 PASS). |
| **IEEC & Causal State Ledger** | `src/resolve_core.py` | **VERIFIED** | CSL on SQLite WAL, typed `Action` state machine (`PROPOSED` -> `BOUND` -> `NEEDS_APPROVAL` -> `AUTHORIZED` -> `READY` -> `DISPATCHING` -> `DISPATCHED` -> `OBSERVING` -> `CONFIRMED`/`UNCERTAIN`/`FAILED`), stable entity IDs, approval digest binding, uncertain restart recovery. Tests: `tests/test_core.py`. |
| **Task DAG & Resource Lease** | `src/agent_runtime.py` | **VERIFIED** | Task dependency graph, parallel read workers, serialized cursor lease fencing, cancellation propagation, timeout enforcement. Tests: `tests/test_agent_runtime.py`. |
| **13-Role Multi-Agent Supervisor** | `src/supervisor.py` | **VERIFIED** | Tool-bounded agents (IntentInterpreter, EntityResolver, Researcher, FileWorker, OSObserver, Planner, SafetyApproval, BrowserExecutor, DesktopExecutor, IndependentVerifier, Recovery, SkillCompiler, PerformanceOptimizer). Tests: `tests/test_supervisor_g5.py`. |
| **Browser Execution & Fixtures** | `apps/web_fixtures.py`, `src/web_mission.py` | **VERIFIED** | 3 independent local web fixture apps (CRM, Docs, Outbox) with separate SQLite DBs and ground truth tables; Playwright runner enforcing ambiguity blocks, version staleness, and lost-ack recovery. Tests: `tests/test_web_g3.py`. |
| **Skill Learning & Replay** | `src/web_skills.py` | **VERIFIED** | Browser skill recording, parameter compilation, approved replay on fresh fixtures, and repair proposal generation on layout shifts. Tests: `tests/test_web_skills.py`. |
| **Personal Files Workspace (W9)** | `src/file_worker.py` | **VERIFIED** | Sandboxed file inventory, SHA-256 duplicate quarantine, dry-run plan, stale-plan guard, apply journal, restart undo. Tests: `tests/test_file_worker.py`. |
| **Development Operations Workspace** | `src/devops_adapter.py` | **VERIFIED** | Repository inspection (branch, head SHA, dirty count), scoped test runner, workspace containment. Tests: `tests/test_devops_adapter.py` (4/4 PASS). |
| **Commerce Workspace (Sandbox-Only)** | `src/commerce_adapter.py` | **VERIFIED** | Policy-compliant sandbox cart/order drafting, approval digest contract, SQLite merchant fixture, SHA-256 receipt proofs. Tests: `tests/test_commerce_adapter.py` (5/5 PASS). |
| **GTM Workflows** | `tests/test_gtm_workflows.py` | **VERIFIED** | Zorro signal-to-task creation, Sentinel lost-ack recovery without resending, PipeWork pipeline stage updates. Tests: `tests/test_gtm_workflows.py` (3/3 PASS). |
| **HTTP Backend & Live Streaming** | `src/resolve_app.py`, `src/web_api.py` | **VERIFIED** | Standard library HTTP server on 127.0.0.1:8765, SSE event streaming on `/api/stream`, guarded `/api/windows/*`, `/api/missions/*`, `/api/emergency-stop`. Tests: `tests/test_api_integration.py`, `tests/test_api_gui.py`. |
| **Adversarial Benchmark Lab** | `src/benchmark_lab.py`, `benchmarks/web_bench.py`, `benchmarks/ablations.py` | **VERIFIED** | 8-fault condition simulator, 24-seed web fixture benchmark, and component ablation tests isolating individual defenses. |
| **Capability Registry** | `capabilities/catalog.yaml`, `capabilities/registry.json`, `src/catalog.py` | **PARTIAL** | 80 concepts total (44 Computer Use, 36 Agentic GTM); 17 TESTED with AST-validated test node IDs; 12 SPECIFIED, 20 IDEA, 22 PROVIDER_REQUIRED, 9 BLOCKED. Tests: `tests/test_catalog.py` (7/7 PASS). |
| **Voice Command & Audio** | Browser Web Speech API, `tests/test_w7_voice_correction.py` | **PARTIAL** | Spoken correction protocol, typed transcripts, and digest-bound approvals verified; native offline microphone capture engine not yet standalone. |
| **Mission Control Dashboard** | `web/index.html`, `web/app.html`, `web/main.js` | **PARTIAL** | Dashboard UI and SSE live stream connected, but requires enhanced Mission Control panels (live computer view, entity inspector, approval queue, skill manager). |

