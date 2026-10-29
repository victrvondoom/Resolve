# Security and privacy

- **Network surface**: binds 127.0.0.1 only; rejects foreign Host/Origin; mutations need a per-process token and JSON content type. Another process running as the same user can read the token — this is not a malware boundary.
- **Untrusted content**: page text, documents and fixture data are treated as data. No instruction found in content is executed; plans come only from user utterances through the deterministic interpreter.
- **Consequential actions**: external communication requires an approval bound to the exact digest; corrections, cancels and re-plans invalidate pending approvals. Real email, payments, trading, purchases and credential access are not implemented. The outbox is labelled SIMULATED DELIVERY.
- **Fault injection** (`/_fault/*`) is refused with 403 unless the fixtures run in test mode (`RESOLVE_TEST_MODE=1`).
- **Files**: the file worker refuses absolute paths, `..`, any symlink component and anything outside its sandbox after realpath; it never overwrites.
- **Windows**: no elevation, no UAC or secure-desktop bypass. Input is refused when the foreground HWND, PID, bounds or focus generation changed since observation; emergency stop revokes the cursor lease. User mouse movement during a pointer move is treated as takeover.
- **Data**: SQLite under `data/`; no telemetry leaves the machine. Browser speech recognition may send audio to the browser vendor — the UI says so before enabling the mic.
- **Code reuse**: no third-party hackathon code is copied.

## Hosted-mode threat model (added 2026-10-29)
- **Modes**: `RESOLVE_MODE=production` removes the shared `/api/session` token and the anonymous demo-tenant fallback, disables the shared-ledger routes,
  enforces `RESOLVE_ALLOWED_HOSTS`, and refuses to start when misconfigured. Tested against a real server (`tests/test_production_security.py`).
- **Cross-tenant**: every document, mission, sandbox and browser frame lookup is keyed by `user_id + workspace_id`; guessing another tenant's id returns 404 with no bytes or
  metadata (negative tests for download, rotate/remove/merge, export, sandbox, frame, mission approve/cancel/delete/print).
- **Sessions/abuse**: logout and account deletion revoke tokens; auth attempts are rate-limited per client IP; missions per tenant are capped; transcription is rate-limited per user.
  Rate limits are in-process memory (per instance), not distributed.
- **Headers**: `nosniff`, `X-Frame-Options: DENY`, CSP (`script-src 'self' 'unsafe-inline'` — inline handlers still exist; tightening to nonces is open), `Permissions-Policy: microphone=(self)`.
  CSRF: mutating routes require a JSON content type and an `Authorization` bearer header (not a cookie), and foreign `Origin` is refused.
- **Cloud browser SSRF** (`src/net_policy.py`): only `http`/`https`; `file:`/`data:`/`javascript:` rejected; every request (including redirect hops, handled manually with `max_redirects=0`)
  must resolve to globally routable addresses (loopback, link-local, private, metadata, IPv4-mapped IPv6 rejected); Chromium's own sandbox is ON (`--no-sandbox` only via explicit
  `RESOLVE_BROWSER_NO_SANDBOX=1`); service workers blocked; downloads disabled. **Residual risk: DNS rebinding** — Chromium resolves again after our check. Production must also enforce egress
  filtering at the network/sandbox boundary. No tenant-grade isolation is claimed; the browser is off in production unless isolation is declared.
- **Not done**: external pen-test, per-tenant VM isolation, secrets manager integration, distributed rate limiting, PDF parser resource sandboxing beyond size limits (20 MiB store / 5 MiB upload).
