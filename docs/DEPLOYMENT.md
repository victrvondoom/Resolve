# Deployment (prepared, NOT deployed)

No hosting credentials existed in this session, so **there is no live URL**. Nothing here has been run on a hosting provider.

## Modes
| Mode | Use | Auth | Bind |
|---|---|---|---|
| `demo` (default) | local development | shared local token via `/api/session`, loopback only | 127.0.0.1 |
| `production` | judge/public deployment | real accounts only (`/api/auth/*`), no shared token, no demo-tenant fallback | `RESOLVE_BIND` |

The server **refuses to start** in production without `RESOLVE_ALLOWED_HOSTS` (and refuses a non-loopback bind in demo mode).

## Environment
```
RESOLVE_MODE=production
RESOLVE_ALLOWED_HOSTS=resolve.example.com        # exact Host header(s)
RESOLVE_BIND=0.0.0.0
RESOLVE_MASTER_KEY=<urlsafe base64, 32 bytes>        # REQUIRED in production; keep it secret and stable or sealed data becomes unreadable
RESOLVE_PORT=8765
RESOLVE_DOCUMENTS_DIR=/data/documents            # persistent volume
RESOLVE_TENANTS_DB=/data/tenants.sqlite3
RESOLVE_DB=/data/resolve.sqlite3
# optional speech provider (pick one; see docs/VOICE.md notes in JUDGE_SUBMISSION)
RESOLVE_VOSK_MODEL=/models/vosk-model-small-en-us-0.15    # offline
RESOLVE_STT_URL=... RESOLVE_STT_API_KEY=...               # off-server; per-request consent enforced
```
TLS must be terminated by the platform (the app speaks plain HTTP). Persistent disk is required for the SQLite files and PDFs.

## What is intentionally disabled in production
- `/api/state`, `/api/stream`, `/api/web/*`, approvals/evidence/skills/benchmark/UIA routes: they read one shared demo ledger that is not
  tenant-scoped (404 in production).
- **Cloud browser** (`/api/sandboxes/*/frame`, `/api/cloud-browser/*`): 503 unless `RESOLVE_CLOUD_BROWSER_ISOLATION` is `microvm` or
  `container-gvisor`, i.e. an operator asserts the browser runs in a per-tenant isolated runtime. A Playwright context inside the web
  process is not tenant isolation.
The flagship mission (documents, workers, PDF, privacy) needs no browser and works in production mode.

## Image
`Dockerfile` (untested — no image was built in this session). Single container, non-root user, health check `GET /api/health`.
Suggested hosts: any container PaaS with a persistent volume and TLS (Fly.io, Render, Railway, Cloud Run + volume). Cloud Run's
filesystem is ephemeral — use a mounted volume or accept that judge data resets on restart.

## Judge-demo checklist
1. Deploy with the env above. 2. Open the URL → account dialog → **Create demo account**. 3. Flagship mission → **Load sample brief** →
**Start mission** → **Approve** → download. 4. Delete from *Privacy & data* afterwards.

## Render (Blueprint)
`render.yaml` defines the service. Steps: Render dashboard -> New -> Blueprint -> pick this repo -> set `RESOLVE_MASTER_KEY` when prompted. The Blueprint needs a paid instance (persistent disk). Verified locally only: production-mode start with a master key returns 200 for `/api/health`, `/`, `/app` and static files; no Render deploy was run from this session.

## Why not Vercel
The server is a long-running stdlib HTTP server with SQLite files, background mission workers and a persistent disk. Vercel functions are short-lived with ephemeral storage, so the flagship mission would not work reliably there.
