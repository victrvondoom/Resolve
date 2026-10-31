# RESOLVE — Inbox-to-Commitment Operator: implementation tracker

Status key: DONE (implemented and tested in this repo) · PARTIAL · TODO · BLOCKED (external dependency)

| Phase | Item | Status | Evidence |
|---|---|---|---|
| 1 | Audit of the existing repository | DONE | this session's inspection; mission engine, sandbox, IEEC and encryption modules reused |
| 2 | Data model: connections, threads, messages, commitments, voice rules, audit | DONE | `src/inbox_store.py`, `tests/test_inbox_store.py` (11 tests) |
| 2 | Tenant isolation on every store call | DONE | `tests/test_inbox_store.py::test_user_b_cannot_*` |
| 2 | Encryption of message bodies and commitment evidence | DONE | `test_message_bodies_and_evidence_are_encrypted_on_disk` |
| 3 | Gmail OAuth (PKCE, minimal scopes) | PARTIAL | adapter in `src/gmail_adapter.py` (see its tests) |
| 3 | Live Gmail connection | BLOCKED | needs `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` and an authorized test account |
| 4 | Triage (categories, evidence, confidence) | PARTIAL | `src/inbox_intel.py` (deterministic rules, labelled fixtures) |
| 5 | Commitment extraction and deadline handling | PARTIAL | `src/inbox_intel.py` |
| 6 | Drafting with source facts only | TODO | — |
| 6 | Explicit send approval | TODO | — |
| 7 | Execution broker, idempotency, read-back verification | TODO | — |
| 8 | Corrections and versioned preferences | PARTIAL | voice rules are versioned and reversible (`inbox_store`) |
| 9 | Morning Brief, Inbox, Thread, Commitments, Drafts, Activity, Rules, Connections, Privacy screens | TODO | — |
| 10 | Labelled benchmark fixtures and report | TODO | — |
| 11 | Real Gmail demonstration | BLOCKED | same as Phase 3 |
| 12 | README, release | TODO | — |
