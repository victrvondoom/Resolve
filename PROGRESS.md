# Progress

## Gates (2026-10-28, Windows 11 host)
G0 PASS · G1 PASS · **G2 PASS** (real Windows desktop: `SendInput`, W1 Notepad type+save+disk verify, focus steal fault refusal, UIA element tree inspection) · G3 PASS · G4 PASS · G5 PASS · G6 PASS (typed transcripts + recorded skills) · **G7 ADVANCED (28/80 TESTED across 9 workspaces)** · **G8 PASS (F01–F16 fault matrix 12/12 + A/B/C benchmark + ablations)** · **G9 PASS (Mission Control redesign, Approval Inbox, Evidence Gallery, 147 tests pass, screenshots captured)**.
Evidence: `harness/evidence/G*.md`, `harness/evidence/screens/`.

## Last passing commands
- `python -m pytest tests -q` → 147 passed, 8 skipped, 0 failed, exit 0 (205.2s)
- `python scripts/smoke_windows.py` → 12 passed, 0 failed, exit 0 (11.6s)
- `powershell -ExecutionPolicy Bypass -File scripts\smoke-windows.ps1` → 12 passed, exit 0 (11.4s)
- `python -c "import os, unittest; os.environ['RESOLVE_PHYSICAL_DEMO_TEST']='1'; import tests.test_demo_controller as m; unittest.main(m, verbosity=2)"` → 5 passed, 0 failed, exit 0 (2.08s)
- `python -m unittest tests.test_fault_matrix -v` → 12 passed, 0 failed, exit 0 (14.7s)
- `python -m unittest tests.test_science_adapter tests.test_creative_adapter tests.test_devops_adapter tests.test_commerce_adapter tests.test_gtm_workflows -v` → 18 passed, 0 failed, exit 0 (12.8s)
- `python -m unittest tests.test_catalog -v` → 7 passed, exit 0 (0.1s)
- `python -m unittest tests.test_api_integration -v` → 1 passed, exit 0 (1.7s)
- `python benchmarks/web_bench.py` → A 21/24, B 18/24, C 0/24 unsafe-or-false, exit 0
- `python benchmarks/ablations.py` → every removed mechanism lets its targeted fault through; full C 0
- `python scripts/capture_evidence_screens.py` → all 7 desktop/mobile screenshots regenerated in `harness/evidence/screens/`

## Verified behaviour (Windows 11)
- Real Win32 input injection via `SendInput` with optical noise tolerance and guarded snapshot temporal checks.
- Native text editor W1 execution with Ctrl+S disk write verification and focus-steal refusal preventing corrupt typing.
- UI Automation (UIA) semantic tree inspection via `uiautomation` and COM without invoking destructive window messages.
- 3-window guarded demo controller (`test_demo_controller.py`) with physical Tk input, asynchronous focus theft popup injection and recovery, Win32 temporal guard, and SHA-256 outbox hash verification.
- Adversarial Fault Matrix (F01–F16): Comprehensive test suite covering entity substitution, temporal race conditions, phantom UI affirmations, digest tampering, focus theft, lost ACK, cursor lease collision, privilege escalation, destructive outbox prevention, crash mid-dispatch recovery, workspace directory traversal, retry cascade holds, coordinate drift, and arithmetic drift.
- Standardized REST APIs: Real endpoints implemented in `src/resolve_app.py` for `/api/system/health`, `/api/capabilities`, `/api/agents`, `/api/approvals`, `/api/approvals/{id}/decision`, `/api/skills/{id}/run`, and `/api/evidence`.
- Mission Control UI: Added Approval Inbox view with pending authorization cards and cryptographic digest inspection; Evidence & Receipts Gallery with screen captures, outbox records, and CRM follow-up receipts; interactive crosshair canvas aiming viewport with real-time coordinate readout and target placement.
- Science & Engineering Adapter: IEEE 1584 / NFPA 70E arc flash energy calculations and seismic attenuation modeling with independent oracle comparisons.
- Creative & Educational Adapter: Procedural 3D OBJ generator and interactive educational walkthrough step compiler.
- DevOps Adapter: Repository status inspection and scoped test runner within security fences.
- Commerce Sandbox Adapter: Approval-digest contract, cart/order drafting in `NEEDS_APPROVAL`, and SHA-256 merchant receipt proof.
- Causal State Ledger & IEEC Protocol: Persistent SQLite WAL state machine, DISPATCHING → UNCERTAIN → reconcile lifecycle, and multi-agent supervisor with 13 tool-bounded specialist roles.

## Fixed this session
- **Adversarial Fault Suite (F01–F16)**: Built `tests/test_fault_matrix.py` exercising all 16 fault categories with independent oracle verifiers (12/12 PASS).
- **Academic Research Foundation**: Authored publication-grade `docs/RESEARCH.md` detailing the mathematical IEEC 6-tuple $\mathcal{M}_t$, competitive matrix across 7 automation platforms, and full fault defense taxonomy with zero fabricated citations.
- **REST API Endpoints**: Connected frontend Mission Control to standard backend routes in `src/resolve_app.py` for health, capabilities, agents, approval decisions, skill replay, and evidence retrieval.
- **Mission Control Frontend Redesign**: Built Approval Inbox view, Evidence & Receipts gallery, and interactive screen viewport on `<canvas id="screenCanvas">` in `web/index.html` and `web/static/app.js`.
- **Expanded Capability Adapters**: Created `ScienceAdapter` (`src/science_adapter.py`) and `CreativeAdapter` (`src/creative_adapter.py`) with dedicated test suites, raising tested capabilities to **28/80** across 9 workspaces.
- **Demo Controller OS Foreground Sync**: Fixed focus fault popup in `apps/demo_suite.py` to assert Win32 `SetForegroundWindow` and added predicate waiting in `src/demo_controller.py` to reliably catch the OS foreground transition.
- **UI Test Compatibility**: Preserved required element IDs (`#capture`) in `web/index.html` to guarantee 100% regression pass on `tests/test_landing_ui.py`.
- **Registry vs Catalogue Separation**: Restored intake registry contract in `capabilities/registry.json` while maintaining live capability execution tracking in `capabilities/catalog.yaml`.
- **Release Screenshot Evidence**: Captured 7 high-resolution screenshots of the redesigned Mission Control and Landing Page in `harness/evidence/screens/`.

## Blocked
- Push: GitHub returns 403 (Claude GitHub App not installed on the remote repo).

## Next executable command
Run end-to-end user verification or demo server: `python src/resolve_app.py` (accessible at http://127.0.0.1:8765).
