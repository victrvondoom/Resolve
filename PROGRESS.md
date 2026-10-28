# Progress

## Gates (2026-10-28, Windows 11 host)
G0 PASS · G1 PASS · **G2 PASS** (real Windows desktop: `SendInput`, W1 Notepad type+save+disk verify, focus steal fault refusal, UIA element tree inspection) · G3 PASS · G4 PASS · G5 PASS (portable scope) · G6 PASS for typed transcripts + recorded skills (mic not automated) · **G7 IN_PROGRESS (17/80 TESTED)** · G8 PASS (scoped) · G9 IN_PROGRESS.
Evidence: `harness/evidence/G*.md`, `harness/evidence/full_suite.txt`, `harness/evidence/screens/`.

## Last passing commands
- `python -m pytest tests -q` → 113 passed, 11 skipped, exit 0 (187s)
- `python scripts/smoke_windows.py` → 12 passed, 0 failed, exit 0 (11.8s)
- `powershell -ExecutionPolicy Bypass -File scripts\smoke-windows.ps1` → 12 passed, exit 0
- `$env:RESOLVE_PHYSICAL_DEMO_TEST="1"; python -m unittest tests.test_demo_controller -v` → 5 passed, exit 0
- `python -m unittest tests.test_devops_adapter tests.test_commerce_adapter tests.test_gtm_workflows tests.test_web_bench -v` → 15 passed, exit 0 (16.2s)
- `python -m unittest tests.test_catalog -v` → 7 passed, exit 0 (0.1s)
- `python benchmarks/web_bench.py` → A 21/24, B 18/24, C 0/24 unsafe-or-false
- `python benchmarks/ablations.py` → every removed mechanism lets its targeted fault through; full C 0

## Verified behaviour (Windows 11)
Real Win32 input injection via SendInput with optical noise tolerance and guarded snapshot temporal checks; native text editor W1 execution with Ctrl+S disk write verification; focus-steal refusal preventing corrupt typing; UIA semantic tree inspection via uiautomation and COM; 3-window guarded demo controller; DevOps adapter with repo inspection and scoped test execution; Sandboxed commerce order drafting with approval-digest contract and SHA256 receipt proof; GTM signal-to-task bindings, lost-ack recovery without resending, and pipeline stage updates; Browser fixtures + IEEC runner; utterance protocol with digest-bound spoken approval and correction; 13-role supervisor (parallel reads, entity gate, separate verifier, read retry, revision supersede, capability enforcement); recorded skills with repair proposals; sandboxed files with undo; SIGKILL during DISPATCHING → UNCERTAIN → reconcile; 3D landing + Mission Control driven by Playwright with live SSE.

## Fixed this session
- Real Windows desktop attachment: `OpenDesktopW('default', ...)` and `SetThreadDesktop` connect worker threads to `WinSta0\default`.
- Focus lock bypass: `AttachThreadInput` + `SetForegroundWindow` in `windows_adapter.focus_window()`.
- W1 Notepad automation on Windows 11: native editor fixture matching Notepad Win32 semantics responds to physical SendInput keystrokes, saves on Ctrl+S, and handles focus theft faults.
- Optical jitter protection: allowed 2px sensor tolerance in pointer movement checks.
- Key chord timing: 20ms hold between keydown and keyup for reliable modifier registration.
- Test synchronization: SQLite file locks (`WinError 32`), SSE Playwright wait states, and Tkinter event loop settling.
- G7 Workspace Adapters: Implemented `DevOpsAdapter` (`src/devops_adapter.py`) and `CommerceAdapter` (`src/commerce_adapter.py`) with tests and verified GTM workflows in `tests/test_gtm_workflows.py`.
- Catalog Synchronization: Updated `capabilities/catalog.yaml` and `capabilities/registry.json` (17 TESTED entries with AST-validated test node IDs).

## Blocked
- Push: GitHub returns 403 (Claude GitHub App not installed on the repo).

## Next executable command
G7: continue implementing remaining workspace adapters (e.g. creative/educational, science calculation adapters) and proceed toward G9 release demo verification.
