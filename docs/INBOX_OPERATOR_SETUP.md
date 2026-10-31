# Setup: inbox operator

## Replay only (no credentials)

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt
python src/resolve_app.py
```

Open `http://127.0.0.1:8765/app`. Choose **Replay lab → Start**. Nothing is read from Gmail.

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
