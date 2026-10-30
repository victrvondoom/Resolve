# Hidden-problem audit of RESOLVE (assumption → failure → fix → test)

Method: treat each claim RESOLVE makes as an assumption ("what must be true for this to hold?"), attack it, and keep only findings
that were **reproduced by a failing test first**. 11 of 11 candidates below were red before the fix and are green now
(`tests/test_hidden_failures.py`, `tests/test_production_security.py`).

| # | Hidden assumption | How it silently failed | Consequence | Fix |
|---|---|---|---|---|
| 1 | "Production image ships without a browser, so it starts" | `cloud_browser.py` imported Playwright at module top | Docker image crashes on boot | Lazy import; 503 only if the browser is actually used |
| 2 | "No account has a guessable password" | `demo@resolve.local` / `demo123` seeded into every DB, production included | Public shared tenant on any deployment | Random per-process password, rotated on every start; not created in production |
| 3 | "UI says 10+ characters, so the server enforces it" | Server minimum was 6 | Weak passwords accepted | 10–256 enforced server-side |
| 4 | "Stored hashes are strong" | PBKDF2 at 100k rounds | Cheap offline cracking after a DB leak | 600k rounds, work factor stored with the hash, legacy hashes still verify |
| 5 | "Verifier: 'ISS-1 present' means issue 1 is in the PDF" | Substring match: `ISS-1` satisfied by `ISS-10`; `auth.py:1` by `auth.py:10` | **False confidence**: a report omitting an issue passes verification when there are ≥10 issues | Word-boundary matching |
| 6 | "State checks are atomic" | Check-then-write in Python | Two simultaneous approvals both ran (duplicate PDFs); a cancel could be overwritten by a finishing worker | SQL compare-and-set transitions; losers get 409 and compensate |
| 7 | "Node timeouts are enforced" | Timeout was only checked *after* a node returned | A hung worker left the mission RUNNING forever and consumed the per-tenant quota (self-DoS) | Each node runs under a hard deadline; hung node is abandoned and its late result rejected |
| 8 | "PDF text extraction is bounded" | No page cap | Parser DoS from a many-page upload | 100-page extraction cap (413) |
| 9 | "File names are valid, unique diagram ids" | Module `config`/`tests` collided with proposed nodes; `my-mod` produced invalid Mermaid | Mission fails or diagram wrong for ordinary projects | Generated ids (`m_*`, `new_*`) |
| 10 | "Socket address identifies a client" | Behind a platform proxy every user shares one address | One attacker exhausts the login limit for everybody; conversely trusting `X-Forwarded-For` is spoofable | `RESOLVE_TRUSTED_PROXY=1` → right-most XFF only |
| 11 | "Rate-limit state is small" | Unbounded dict keyed by client | Memory growth from rotating clients | LRU-bounded (10k keys) |

Also fixed: CI `portable` job ran browser tests without the runner-compatible sandbox flag.

## Residual risks (found, NOT fixed — stated so they are not mistaken for done)
- Session tokens are stored in plaintext in the tenant DB (a DB leak is session hijack). Hashing them touches every auth path.
- `register` reveals whether an email exists (409). Login errors are uniform.
- CSP still allows `'unsafe-inline'` scripts (inline handlers exist); a stored-XSS bug would expose the token kept in `sessionStorage`.
- Abandoned (timed-out) worker threads cannot be killed in-process; they finish in the background. Real isolation needs subprocess/VM workers.
- Rate limiting is per-instance memory. DNS-rebinding TOCTOU in the browser SSRF guard (see SECURITY.md). Upload/parse limits are size-based only.
- Real speech recognition, live deployment and GitHub CI execution remain unverified (see FINAL_REGRESSION.md).

## Round 2: "static where it should be dynamic" (fixed)
| Static / fake behaviour | Replacement | Test |
|---|---|---|
| Only a bundled sample project could be analyzed; analyzer was Python-only with fixed module names | Tenant-private project upload (zip read in memory, never extracted; zip-slip/bomb/symlink/quota guards), multi-language analyzer, proposed components attach to modules whose findings motivate them, mission bound to project SHA-256 and re-verified | `test_dynamic_inputs` (13), E2E |
| Architecture was read-only | Edit Mermaid in the page → parsed back to a graph → new digest, old approval dead, edit shown in PDF | `test_dynamic_inputs`, E2E |
| `/api/voice/transcribe` returned canned contacts (`alex-fin-02`) for the words "finance"/"sales" — ids that exist nowhere | Real resolution against ledger entities by stable id; ambiguity → candidates + clarification; route disabled in production (shared ledger) | `test_cloud_api` |
| Dead mic when no server STT | Browser speech fallback, labelled, per-use consent | E2E (badge) |
| Only the latest mission visible; static sidebar text; dead demo screens/buttons offered in production | Live mission history; sidebar note, nav and controls driven by `/api/health` mode + features | E2E |
| `hidden` attribute overridden by `.btn` CSS (buttons meant to be hidden stayed visible) | global `[hidden]{display:none!important}` | E2E |

## Round 3: UI/UX pass (found by looking at real screenshots, fixed, pinned by tests)
| What users saw | Actual cause | Fix | Test |
|---|---|---|---|
| Workspace switcher listed "Trust and Safety", "Commerce"… | `/api/workspaces?tenant=1` ignored `tenant` and returned the 80-concept catalog; switching sent `undefined` | Real per-user workspace list + active id | `test_account_api`, `test_ux_e2e` |
| Deleting your active workspace logged you out | Sessions `ON DELETE CASCADE` with their workspace | Sessions move to a surviving workspace before the delete | `test_account_api`, `test_ux_e2e` |
| Privacy page promised "offsite snapshots… 30 days" | Invented text; no backups exist | Truthful retention text in UI, summary and receipts | `test_account_api`, `test_ux_e2e` |
| "Connected apps" offered Google Drive/GitHub, "credentials are encrypted" | Connectors are fixture data with no OAuth or credentials | Hidden in production; labelled SIMULATED in demo mode | `test_ux_e2e` |
| Files sandbox buttons returned 404 in production | Backed by disabled shared-ledger routes | Not offered in production | `test_ux_e2e` |
| Account pill "Manage account" did nothing; showed `demo@resolve.local` before sign-in | No handler; static placeholder | Account menu (privacy, new workspace, sign out) and a live identity | `test_ux_e2e` |
| `/api/auth/me` echoed the session token | Returned the whole session row | Token removed | `test_account_api` |
| Mobile issue table showed "I S S - 1" letter by letter | Table squeezed into 390 px | Responsive two-line cards | `test_ux_e2e` |
| Black PDF box on phones | Phones have no inline PDF viewer | `pdfViewerEnabled` fallback with "Open the PDF"; no hidden iframe pointed at a PDF | `test_ux_e2e` |
| Architecture links that did not exist (proposed node "pointing" at another proposed node) | A layer wrapped onto two rows; edges ran into the lower row | Layers stay on one row; boxes shrink, then the diagram scrolls | visual check |
| Privacy/Documents/Flagship showed stale data after switching views | Data loaded once at page start | Views refresh when shown | `test_ux_e2e` |
| Landing page (judges' first view) was a dark neon theme about the old CRM demo, claimed Wispr was used for development notes, and showed a red benchmark error in production | Never updated | Cream redesign around the flagship, live server facts, no unverified Wispr claim, benchmark explained in hosted mode | `test_landing_ui` |
