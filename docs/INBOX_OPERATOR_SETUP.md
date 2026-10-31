# Setup: inbox operator

## Replay only (no credentials)

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
python src/resolve_app.py
```

Open `http://127.0.0.1:8765/app`. Choose **Replay lab → Start**. Nothing is read from Gmail.

## Local mail server (no credentials, real delivery on this machine)

LOCAL mode runs the whole product against a real mail server that RESOLVE starts on loopback (`src/local_mail.py`): SMTP
submission, POP3 retrieval, Maildir storage under `data/localmail/`. It delivers only to `@mail.resolve.test` addresses on
this machine. It is not Gmail.

```bash
python src/resolve_app.py            # starts the local mail server too (SMTP 2525, POP3 2110; free ports if busy)
```

In the app: **Connections → Local mail server → Create local mailbox**, then **Deliver a message into this mailbox** (it is
sent over SMTP from a local colleague account), **Sync local mailbox**, and switch the Morning brief to **Local mail**.
Replies you approve are delivered over SMTP and verified by reading the Sent folder back over POP3.

The server can also run on its own and be driven from the command line:

```bash
python src/local_mail.py serve --root data/localmail
```

```bash
python src/local_mail.py add-account ana@mail.resolve.test "a long password"
```

```bash
python src/local_mail.py send --from ana@mail.resolve.test --password "a long password" --to founder@mail.resolve.test --subject "Logs" --body "Can you check the staging logs?"
```

Variables: `RESOLVE_LOCAL_MAIL` (`0` disables it; production needs `1` to enable), `RESOLVE_LOCAL_MAIL_ROOT`,
`RESOLVE_LOCAL_SMTP_PORT`, `RESOLVE_LOCAL_POP3_PORT`.

## Optional model endpoint (MODEL_ENABLED)

Without these variables the engine is `RULES_ONLY` and says so. With them, `POST /api/inbox/threads/{id}/analyze` asks the
model for a structured analysis, validates it against a schema, checks every quoted sentence against the stored message,
and falls back to the rules engine on timeout or invalid output. Triage during sync stays rules-based.

| Variable | Example |
|---|---|
| `RESOLVE_MODEL_BASE_URL` | `http://127.0.0.1:11434` (Ollama), `http://127.0.0.1:8080` (llama.cpp `llama-server`) |
| `RESOLVE_MODEL_NAME` | `llama3.1:8b` |
| `RESOLVE_MODEL_API_KEY` | only for hosted OpenAI-compatible services; never logged or returned |
| `RESOLVE_MODEL_TIMEOUT` | seconds, default `20` |

Local install path (where the hardware allows): install Ollama from ollama.com, run `ollama pull llama3.1:8b`, then set the
two variables above. No model was run while building this repository; see `docs/INBOX_OPERATOR_TRACKER.md`.

## Operations

```bash
python scripts/resolve_admin.py migrations
```

```bash
python scripts/resolve_admin.py backup --out backups/$(date +%F)
```

```bash
python scripts/resolve_admin.py verify --from backups/2026-10-31
```

```bash
python scripts/resolve_admin.py restore --from backups/2026-10-31 --yes
```

Backups are consistent SQLite snapshots plus a SHA-256 manifest. Sealed fields stay encrypted and the master key is not
included: back up `RESOLVE_MASTER_KEY` (or `data/documents/keys/master.key`) separately. Stop the server before a restore;
the replaced files are kept as `*.pre-restore`. Health: `GET /api/livez` (process up) and `GET /api/readyz` (database
migrated and passing `quick_check`, sealing key usable; optional subsystems reported separately).

## Live Gmail

RESOLVE needs three environment variables. Without them, the Connections screen shows **GMAIL NOT CONFIGURED** and lists the missing names. Set them before starting the server:

```bash
export GOOGLE_OAUTH_CLIENT_ID="<your OAuth client id>"
export GOOGLE_OAUTH_CLIENT_SECRET="<your OAuth client secret>"
export GOOGLE_OAUTH_REDIRECT_URI="http://127.0.0.1:8765/api/inbox/connections/gmail/callback"
python src/resolve_app.py
```

Register the same redirect URI in your Google Cloud OAuth client. Use an OAuth client created for this purpose, and an authorized test account. Do not connect a mailbox you do not control.

Permissions:

- **Connect Gmail (read-only)** requests `gmail.readonly`.
- **Enable drafting** adds `gmail.compose`. Required before **Save as Gmail draft**.
- **Enable sending** adds `gmail.send`. Required before any live send.

Each permission is requested only when you choose it. Disconnect in Connections revokes RESOLVE's access at Google where Google confirms it.

## Other settings

| Variable | Purpose | Default |
|---|---|---|
| `RESOLVE_MASTER_KEY` | Encryption key for data at rest. Required in production | development key file (not for production) |
| `RESOLVE_INBOX_DB` | Path of the inbox database | `data/inbox.sqlite3` |
| `RESOLVE_PUBLIC_BASE` | Base URL used for redirects | empty (relative redirects) |

Set your timezone in **Privacy & settings** before relying on deadlines. Until then, RESOLVE shows UTC and says so.

## What is not verified here

Live Gmail behaviour has not been run against a real account in this repository. The status in the app and in `docs/INBOX_OPERATOR_TRACKER.md` is the source of truth.
