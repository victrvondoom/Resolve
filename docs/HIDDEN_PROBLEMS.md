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
