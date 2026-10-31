# System-design coverage check (TRACEN "Vibe Coding Trail", topics 1–20)

Audit of this repository against the 20 foundational topics. Status is from reading the code (`src/`, `Dockerfile`,
`.github/workflows/ci.yml`); only rows marked TESTED name a test that exercises the behavior. Nothing here claims a
production deployment, and RESOLVE is a single-process app, so several topics are deliberately out of scope.

| # | Topic | Status | Where / why |
|---|-------|--------|-------------|
| 1 | Rate limiting | PRESENT | `src/server_security.py` (per-client limiter, trusted-proxy-aware client id), `src/tenant_auth.py` (auth attempts); 429 handling for Gmail in `src/gmail_adapter.py` |
| 2 | Caching | PARTIAL | Narrow in-process caches (e.g. `src/inbox_store.py`, `src/gmail_adapter.py`); no Redis/CDN layer. Email content is sealed at rest and must never be cached in plaintext |
| 3/4 | Load balancer / reverse proxy | OUT OF SCOPE | Single process. `RESOLVE_ALLOWED_HOSTS` + trusted-proxy handling are in place so it can sit behind Nginx/Caddy (`docs/DEPLOYMENT.md`) |
| 5 | API gateway | OUT OF SCOPE | One service; auth, host checks and rate limits live in the app (`src/server_security.py`, `src/tenant_auth.py`) |
| 6 | CI/CD | PRESENT | `.github/workflows/ci.yml` runs the suite on every push/PR, split by marker group plus a browser e2e job. No automatic deploy step (deliberate) |
| 7 | Docker | PRESENT | `Dockerfile`: slim base, non-root user, HEALTHCHECK on `/api/health`. Marked "untested template" in the file itself |
| 8/9 | Kubernetes / service discovery | NOT PRESENT | Not needed for an MVP of one service; the guide itself recommends against it at this size |
| 10 | Circuit breaker | NOT PRESENT | Gmail errors are classified (`RATE_LIMITED`, `PROVIDER_ERROR`) and surfaced as connection state, but there is no open/half-open breaker |
| 11 | Timeouts | PRESENT | Provider and subprocess calls carry explicit timeouts (`src/gmail_adapter.py`, `src/inbox_api.py`, `src/agent_runtime.py`) |
| 12/13 | Retries + exponential backoff | PRESENT (reads only) | `src/gmail_adapter.py`: bounded retry with backoff for idempotent reads; **writes are never retried by design** |
| 14 | Idempotency | PRESENT | Re-sync is idempotent (`src/inbox_store.py`, `src/inbox_service.py`); sends are bound to an approval digest and an unknown outcome stays UNCERTAIN and is reconciled read-only, never resent (AGENTS.md rule) |
| 15 | Queue / DLQ | PARTIAL | Missions run through a scheduler (`src/supervisor.py`); no durable queue and no dead-letter queue |
| 16/17 | Pub/Sub, event-driven | NOT PRESENT | In-process audit/ledger records only; no broker |
| 18/19 | Distributed transactions / Saga | PARTIAL | No cross-service transactions. Compensating steps exist in `src/brief_mission.py` (a mission superseded mid-run deletes its orphan output) |
| 20 | Cron / scheduled jobs | PARTIAL | In-process mission scheduler; no cron or distributed lock (single instance, so no duplicate-execution risk today) |

## Gaps worth closing if RESOLVE becomes multi-instance

1. A circuit breaker around the Gmail adapter (fail fast while `PROVIDER_ERROR`/`RATE_LIMITED` persists).
2. A durable job queue with a dead-letter state for sync and mission work.
3. A distributed lock before running any scheduled job on more than one instance.

These are not implemented here and are not claimed.
