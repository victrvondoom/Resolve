# Inbox operator API

All routes live under `/api/inbox/`, are served by `src/resolve_app.py` and implemented in `src/inbox_api.py`.

## Identity, workspace and mode

- The caller is the authenticated session user (`Authorization: Bearer <token>` or `X-Resolve-Token`). No client-supplied user or workspace id is trusted.
- Mutations require `Content-Type: application/json` (the server enforces this for every POST, PUT and PATCH).
- Every query takes `mode=LIVE|REPLAY` (default `LIVE`). REPLAY uses the separate workspace `<workspace>.replay`, so live and synthetic data never mix.
- Errors are JSON `{"error": "..."}`. Unexpected server errors return a generic message; the server log records the route and exception type only, never email text.
- Body size is capped at 32 KiB on ordinary routes. Text fields have per-field limits (for example, message bodies up to 20,000 characters).

## Connections (Gmail)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/inbox/connections` | Gmail state (`NOT_CONFIGURED`, `NOT_CONNECTED`, `CONNECTED`, `REVOKED`, `EXPIRED`, `RATE_LIMITED`, `ERROR`), granted scopes, account, replay state, and the environment variables required when not configured |
| GET | `/api/inbox/connections/gmail/status` | Gmail state only |
| POST | `/api/inbox/connections/gmail/start` | Returns the Google authorization URL. Body `{"scopes": ["read"\|"compose"\|"send"]}`. Returns 409 `NOT_CONFIGURED` with the required variable names when unconfigured |
| GET | `/api/inbox/connections/gmail/callback` | Public redirect target. Validates a single-use, expiring state bound to the user who started the flow, exchanges the code with PKCE, stores tokens sealed, then redirects to `/#inbox-connections?gmail=connected` (or `denied`, `state`, `revoked`, `failed`) |
| DELETE | `/api/inbox/connections/gmail` | Revokes at Google (best effort, reported as `revoked_at_provider`), deletes the local token. Stored messages remain until deleted in Privacy & data |

## Settings and sync

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/inbox/settings` | Timezone (`timezone_configured` says whether the user set it), sign-off, tone |
| PUT | `/api/inbox/settings` | Set an IANA timezone (validated), sign-off, tone |
| POST | `/api/inbox/sync` | Synchronize. Body `{"mode": "LIVE"\|"REPLAY", "query": "newer_than:30d"}`. Each run is recorded, including failed runs |
| GET | `/api/inbox/sync/status` | Connection state, recent runs, last successful sync, stale flag (over 24 h), message and thread counts, engine label |

## Morning brief and threads

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/inbox/brief` | Sections computed from stored records: summary counts, needs attention, replies to send, my commitments, waiting on others, requests to me, timing buckets (overdue, due today, due soon, future, unknown), drafts, recently resolved (14 days). Dismissed and deferred threads are excluded |
| GET | `/api/inbox/threads` | Conversations. Query: `filter` (`all`, `action_required`, `reply_required`, `my_commitments`, `waiting`, `deadline`, `important`, `automated`, `needs_review`), `q`, `sort` (`date`, `priority`), `since_days`, `page`, `size` (max 50) |
| GET | `/api/inbox/threads/{id}` | The full conversation with direction (INBOUND/OUTBOUND), recipients, the analysis (understanding, classification, stored versus effective classification, priority, explanation, evidence linked to message ids, commitments with source message ids, dates, uncertainty), corrections and drafts |
| POST | `/api/inbox/threads/{id}/reprocess` | Re-runs triage and commitment extraction over the stored thread. Repeats create no duplicates |
| POST | `/api/inbox/threads/{id}/state` | Body `{"state": "DEFERRED"\|"DISMISSED"\|"OPEN", "reason": "...", "until_local": "2026-11-06T09:00"}`. Reason required for defer and dismiss. Messages are never changed |
| POST | `/api/inbox/threads/{id}/corrections` | Body `{"kind", "scope", "value", "dictated_text", "confirmed"}`. Kinds: WRONG_PRIORITY, WRONG_CATEGORY, NOT_ACTIONABLE, NOT_COMMITMENT, WRONG_OWNER, WRONG_DEADLINE, ALREADY_COMPLETED, AUTOMATED, OTHER. Scopes: ONE_TIME, PERSISTENT_THREAD, PERSISTENT_WORKSPACE (needs `confirmed: true`). Returns before, your correction, after, impact and effects |
| POST | `/api/inbox/threads/{id}/drafts` | Prepares a reply from the thread. Body `{"facts": {"status": "..."}}` (facts you can confirm). Returns the draft with sources, missing information, assumptions and potential new commitments |

## Commitments

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/inbox/commitments` | List. Query: `status`, `kind`, `timing` |
| GET | `/api/inbox/commitments/{id}` | Detail: owners and ownership state, deadline phrase, timezone, confidence, source message, exact sentence, revisions, status history, notes, follow-ups, related commitments, completion evidence |
| PATCH | `/api/inbox/commitments/{id}` | Edit `required_action`, `responsible_person`, `requesting_person`, `deadline_local` (local time, kept as a revision), and `status` (validated transition, `reason` required for dismiss or cancel) |
| POST | `/api/inbox/commitments/{id}/resolve` | `{"evidence_execution_id": "..."}` gives `RESOLVED_VERIFIED`, and needs a verified execution record. `{"confirm": true, "reason": "..."}` gives `RESOLVED_USER_CONFIRMED`, which is never presented as verified |
| POST | `/api/inbox/commitments/{id}/follow-up` | Records a follow-up with a trigger (`OUTSTANDING_OWN_COMMITMENT`, `OTHER_PARTY_PROMISE`, `UNANSWERED_THREAD`, `DUE_DATE_APPROACHING`) and a target. Nothing is sent |
| POST | `/api/inbox/commitments/{id}/notes` | Adds context to a commitment |

## Drafts and approval

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/inbox/drafts` | Drafts with status and version |
| GET | `/api/inbox/drafts/{id}` | Draft with recipient, cc, subject, body, digest, version history, grounding metadata and the execution timeline |
| PATCH | `/api/inbox/drafts/{id}` | Edit recipient, cc, subject, body. Any change creates a new version and digest, returns the draft to DRAFT, and invalidates earlier approvals |
| POST | `/api/inbox/drafts/{id}/approve` | Body `{"digest": "...", "operation": "SEND_EMAIL"\|"CREATE_GMAIL_DRAFT"}`. The digest must match the current content. Returns a single-use approval that expires after 15 minutes |
| POST | `/api/inbox/drafts/{id}/gmail-draft` | Creates the Gmail draft with `{"approval_id", "digest"}`. Reported VERIFIED only after read-back. LIVE needs `gmail.compose` |
| POST | `/api/inbox/drafts/{id}/send` | Sends with `{"approval_id", "digest"}`. Reported VERIFIED only after read-back. LIVE needs `gmail.send`. REPLAY sends are labelled SIMULATED |
| POST | `/api/inbox/drafts/{id}/reject` | Rejects a draft (`{"reason": "..."}`) |

## Executions

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/inbox/executions` | Operations with state, provider id, reason, verification method and evidence |
| GET | `/api/inbox/executions/{id}` | One operation with its timeline (each verification record) and approval details |
| POST | `/api/inbox/executions/{id}/reconcile` | Read-only reconciliation of UNCERTAIN or NEEDS_RECOVERY operations. Searches by correlation Message-ID and never dispatches |

## Voice rules

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/inbox/rules` | Stored rule versions, active rules, saved corrections |
| POST | `/api/inbox/rules/parse` | Preview only. Returns parsed rules, conflicts and whether confirmation is required. Stores nothing |
| POST | `/api/inbox/rules` | Stores rules. Body `{"text", "confirm"}`. Refused if a rule removes send approval, or conflicts with an active rule. UNKNOWN rules are stored inactive. High-impact rules need `confirm_high_impact: true` |
| PATCH | `/api/inbox/rules/{id}` | Deactivate, or edit (creates an inactive new version) |
| POST | `/api/inbox/rules/{id}/confirm` | Activates a rule. UNKNOWN rules cannot be activated |
| POST | `/api/inbox/rules/{id}/reverse` | Deactivates a rule. History is kept |

## Activity and replay

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/inbox/activity` | Audit events as human-readable labels. Identifiers and statuses only; message bodies never appear |
| GET | `/api/inbox/replay/scenarios` | The synthetic dataset (labelled SYNTHETIC), replay state and log |
| POST | `/api/inbox/replay/start` | Starts the replay (fixed replay clock) |
| POST | `/api/inbox/replay/step` | Reveals the next synthetic message and runs the real sync pipeline. After the last message the script runs: draft, approve and simulated send, draft. Optional `{"inject": "send_timeout"}` makes the next simulated send time out after storing |
| POST | `/api/inbox/replay/pause` | Pauses playback |
| POST | `/api/inbox/replay/resume` | Resumes playback |
| POST | `/api/inbox/replay/reset` | Removes replay data only. Live data is untouched |
| POST | `/api/inbox/replay/replay` | Reset, then start again |

## Privacy

Existing routes now include inbox data: `POST /api/privacy/export` returns an `inbox` section (commitments, drafts, corrections, sync runs, executions, settings, audit) and an `inbox_replay` section. Tokens are never exported. `POST /api/privacy/delete-workspace` and `delete-account` purge live and replay inbox rows.
