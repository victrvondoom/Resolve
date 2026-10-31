# Changelog

## Unreleased — 2026-10-31 sprint

- LOCAL mode on a real local mail server (`src/local_mail.py`): SMTP + POP3 on loopback, Maildir storage, local-only delivery; same sync, broker and read-back verification as Gmail; Connections panel and Local mail mode in the UI.
- Model-provider layer (`src/model_provider.py`): OpenAI-compatible endpoint, schema validation, quote verification, unsupported-claim guard for drafts, fallback to rules. Not run against a model here.
- Gmail incremental sync via the History API with a checkpoint stored only after success; expired history falls back to a full sync; reads retried with backoff, sends never retried.
- Fixes: transient provider ERROR no longer blocks later syncs; LOCAL data and credentials were not removed by workspace deletion; master-key rotation could not introduce a new key version; inbox routes fell back to the demo tenant for a missing or invalid token in development; clock times after a day word were ignored; "when possible" was not treated as vague; Mission control view had no navigation link.
- Sessions stored as token hashes, sliding renewal with a 30-day cap, password change revoking every session.
- Operations: atomic migrations with WAL, `scripts/resolve_admin.py` (migrations, backup, verify, restore), `/api/livez`, `/api/readyz`.

## Earlier — Inbox-to-Commitment Operator

- Semantic fixes: timezone-aware deadlines with clock times ("before 4 PM", "noon tomorrow"); owners and requesters resolved from the mailbox and real senders; changed deadlines revise the obligation and keep history; restated requests are one obligation; quoted text and instruction-like sentences do not create obligations; bare "well"/"ill" no longer match promises.
- Send path: the visible operation marker is removed from outgoing content; correlation uses an RFC 822 Message-ID with read-back of the exact content; an approval is refused when the draft's content changed since approval; drafts are SENT_VERIFIED or SAVED_TO_GMAIL only after read-back.
- Storage: migrations 4 to 7 (triage, obligation revisions, status history, draft versions and metadata, OAuth tokens sealed, sync runs, follow-ups, simulated outbox, inbox settings, notes, thread states). Obligation identity is a keyed blind index.
- HTTP API under `/api/inbox/*` (see `docs/INBOX_API.md`), Gmail OAuth start and callback with single-use state and PKCE, replay provider, privacy export and deletion include inbox data.
- Screens: Morning brief, Inbox, Thread detail with corrections, Commitments and detail, Drafts and exact approval, Activity, Voice rules, Connections, Privacy & settings, Replay lab. Advanced tools grouped under one menu.
- Benchmarks: 32 labelled scenarios written before scoring; persisted-ledger metrics; generated report (`docs/INBOX_BENCHMARK_REPORT.md`).
- Known: live Gmail and model results are not measured; see `docs/INBOX_SECURITY.md` limitations.

## 0.2.0 — Guarded desktop and measured local workflow
- Added parallel read-only resolver agents, mission DAG, durable task events and fenced cursor lease.
- Added short-lived Win32 observations, real controlled pointer/click/type verification, screenshot evidence and emergency stop.
- Added action-scoped simulator effects, atomic mission revisions, safe stop-send interpretation, local recovery, loopback session token, and live API/GUI checks.
- Added browser-dependent demo voice commands and optional spoken narration, a planned 80-concept registry, and an eight-condition SQLite benchmark with raw ground truth.

## 0.1.0 — Initial local prototype
- Added Python standard-library dashboard/API, SQLite WAL mission/action ledger, deterministic CRM/report/outbox simulator, and basic Win32 foreground-guarded physical input.
- Added source-backed prior-art notes, known limitations, demo runbook, and seven unit tests.
- Voice, UIA semantics, agent DAG, external integrations, and comparative benchmark remain unimplemented.
