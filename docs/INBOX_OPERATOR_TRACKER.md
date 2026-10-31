# RESOLVE — Inbox-to-Commitment Operator: implementation tracker

Status key: DONE (implemented and tested in this repo) · PARTIAL · TODO · BLOCKED (external dependency)

| Phase | Item | Status | Evidence |
|---|---|---|---|
| 1 | Audit of the existing repository | DONE | this session's inspection; mission engine, sandbox, IEEC and encryption modules reused |
| 2 | Data model: connections, threads, messages, commitments, voice rules, audit | DONE | `src/inbox_store.py`, `tests/test_inbox_store.py` (11 tests) |
| 2 | Tenant isolation on every store call | DONE | `tests/test_inbox_store.py::test_user_b_cannot_*` |
| 2 | Encryption of message bodies and commitment evidence | DONE | `test_message_bodies_and_evidence_are_encrypted_on_disk` |
| 3 | Gmail OAuth (PKCE, minimal scopes), pagination, expired/revoked/rate-limited handling | DONE (offline, recorded responses) | `src/gmail_adapter.py`, `tests/test_gmail_adapter.py` (14 tests) |
| 3 | Live Gmail connection | BLOCKED | needs `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` and an authorized test account |
| 4 | Triage (categories, evidence, confidence) | PARTIAL | `src/inbox_intel.py`; measured on 12 labelled fixtures: 8/12 fully passing, 8/12 category accuracy (`benchmarks/results/inbox_eval.json`). Four mismatches are kept visible, not tuned away. |
| 5 | Commitment extraction and deadline handling | PARTIAL | `src/inbox_intel.py`, `tests/test_inbox_intel.py` (14 tests): no invented deadlines, ambiguity flagged, automated mail ignored, injection cannot change decisions |
| 6 | Draft storage, explicit approval bound to draft digest, single-use approvals | DONE | `src/inbox_store.py` (migration 2), `tests/test_send_broker.py` |
| 6 | Explicit send approval | DONE | `src/send_broker.py::send_email` (approval must match the operation and digest; expires; single use) |
| 7 | Execution broker, idempotency, read-back verification | DONE (offline, scripted Gmail double) | `src/send_broker.py`: content-derived operation key, durable claim before dispatch, Sent check before any retry, read-back before VERIFIED; mutation check: duplicate-send test fails with `2 != 1` when the protections are removed
| 8 | Corrections and versioned preferences | DONE | `src/voice_policy.py` (parser, confirmation, conflicts), corrections table and reversal in `src/inbox_store.py`, `tests/test_voice_policy.py` (17 tests) |
| 9 | Morning Brief, Inbox, Thread, Commitments, Drafts, Activity, Rules, Connections, Privacy screens | TODO | — |
| 10 | Labelled benchmark fixtures and report | PARTIAL | `benchmarks/inbox_fixtures.json`, `benchmarks/inbox_eval.py` (12 fixtures; fixed expectations, written before the rules)
| 11 | Real Gmail demonstration | BLOCKED | same as Phase 3 |
| 12 | README, release | TODO | — |
