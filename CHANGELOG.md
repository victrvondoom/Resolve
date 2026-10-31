# Changelog

## Unreleased — Inbox-to-Commitment Operator

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
