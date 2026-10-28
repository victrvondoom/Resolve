# RESOLVE — hard acceptance gates

Status: NOT_STARTED, IN_PROGRESS, PASS, FAIL, BLOCKED. A gate is PASS only with command, exit code, externally observable effect and evidence file.

| Gate | Required executable demonstration | Evidence |
|---|---|---|
| G0 | Existing code audited, baseline commands executed | harness/evidence/G0.md |
| G1 | UI POSTs mission to real server; SQLite persists; SSE/WebSocket streams; reload retains state | G1.md |
| G2 | Real Windows HWND/UIA inspect, real cursor move, Notepad type+save+file verify; focus fault blocks wrong input; STOP works | G2.md |
| G3 | Browser opens three stateful fixture apps; writes real DB state; independent verification; wrong-identity detection | G3.md |
| G4 | Intent revisions, entity IDs, approvals, CSL prevent duplicate / wrong target / stale version; crash recovery | G4.md |
| G5 | Concurrent tool-bounded agents, one fenced cursor lease; cancellation and failure tests | G5.md |
| G6 | Speech path if hardware supports; typed fallback; voice correction; recorded skill replay | G6.md |
| G7 | Shared capability registry + cross-workspace execution, each module tested | G7.md |
| G8 | Fault injection, baselines A/B/C with raw counts, ablations, reproducible benchmark | G8.md |
| G9 | Fresh start, demo, README, real screenshots, scripts, release checklist | G9.md |
| G10 | Website pivot: zero-install web app, running on 127.0.0.1:8765 without desktop app prerequisites | G10.md |
| G11 | Multi-tenant account & workspace system: PBKDF2 auth, session tokens, tenant isolation | G11.md |
| G12 | Cloud computer & sandbox engine: isolated Playwright browser sandbox, live frame streaming, takeover | G12.md |
| G13 | T-IEEC continuity: Effect Dependency Graph with mathematical invariant checks and contradiction detection | G13.md |
| G14 | Authorized personal connections: scoped Drive, GitHub, CRM, Outbox connectors with ambiguity defense | G14.md |
| G15 | User-controlled data deletion: portable export, granular and permanent account destruction with receipts | G15.md |
| G16 | Production readiness & security: T01–T16 adversarial suite, end-to-end browser workflows, screenshots | G16.md |

Anti-scaffold: a frontend card is not an agent; a class with no tool call is not an agent; hardcoded HTTP is not verification; SVG/CSS pointer is not the Windows cursor; a toast is not a confirmed effect; skipped Linux tests are not Windows success; an 80-name catalogue is not 80 capabilities.
