# RESOLVE — UI/UX and product audit (inspection, not a redesign plan)

Inspected on branch `claude/beautiful-bardeen-fw59qp`. Each finding names the file that shows it.

## What works (keep it)
- Real mission workflow end to end: brief → parallel workers → approval of an exact plan → verified PDF (`src/brief_mission.py`, `tests/test_flagship_e2e.py`).
- Live computer viewer with real captured frames (`web/static/flagship.js`, `renderComputer`).
- Tenant isolation tests for documents, sandboxes, browser sessions and missions (`tests/test_remediation_p0.py`, `tests/test_tenant_security.py`).
- Honest status labels: the server reports SIMULATED / BLOCKED where that is true.

## Cons, ranked by how much they hurt the product
| # | Con | Where | Severity |
|---|---|---|---|
| 1 | **No encryption at rest.** Uploaded PDFs, generated reports, mission records and evidence are stored as plain bytes/SQLite. The `cryptography` package is not installed and nothing encrypts. | `src/document_studio.py`, `src/brief_mission.py`, `src/write_audit.py` | P0 for any privacy claim |
| 2 | **Jargon on the first screen.** The landing page says "IEEC", "ledger", "digest", "intent revision", "idempotent". A new visitor cannot tell what the product does. | `web/landing.html` (hero, "How it works", "Status") | P0 for the judges |
| 3 | **The landing page is static.** A single canvas graph and text; there is no entrance, no product moment, no way into the app beyond one button. | `web/landing.html`, `web/static/landing.css` | P1 |
| 4 | **Mission Control has 15 sidebar views**, most of them developer tools (Benchmarks, Windows native, Ledger & registry). Most users would be lost. The earlier compacting only hid them behind "More tools". | `web/index.html` sidebar | P1 |
| 5 | **Production browser execution is blocked** unless isolated infrastructure is configured. The UI shows this, but the demo path depends on the local demo mode. | `src/server_security.py`, `src/resolve_app.py::browser_for` | Known, honest |
| 6 | **Connectors and the outbox are simulated.** Accurate, but the product cannot yet act on a real account. | `src/connectors.py`, outbox in `src/brief_mission.py` docs | Known, honest |
| 7 | **No speech-to-text model is installed or reachable here**, so real microphone transcription is not verified. | `src/voice_engine.py`, `/api/voice/health` | Known, honest |
| 8 | **README is a wall of engineering notes.** It needs a plain-language opening, a diagram and a short path to try the product. | `README.md` | P1 |
| 9 | **No 3D or motion system** anywhere in the app shell; the only 3D is the device model added in the last round. | `web/static/icon3d.js` | Design goal not met |
| 10 | **Error text leaks internals.** A generic failure returns `str(exc)` to the browser. | `src/resolve_app.py` final `except Exception` | P2 security hygiene |

## Redesign decisions (this round)
1. Add a **cinematic intro** on the landing page: two single-colour 3D blocks enter from opposite sides, meet, and reveal the RESOLVE name. It can be skipped at any time, replayed, and is disabled for reduced-motion users. It never blocks the link into the app.
2. Rewrite the **landing hero and README opening in plain language**; technical names move into a glossary.
3. **Not in this round:** encryption at rest (needs a key-management decision and the `cryptography` package), the full Mission Control redesign, a 3D video, and any multi-screen 3D module transitions. These are listed in the final report as open.
