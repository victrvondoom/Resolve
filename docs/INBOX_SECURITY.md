# Inbox operator security model

This document describes what the inbox operator enforces, where, and which test proves it. Items marked **NOT VERIFIED** are not proven by this repository.

## Trust boundaries

- **Email text is untrusted data.** It is never executed, never used as an instruction, and never shown without escaping. Instruction-like sentences are excluded from obligations and route the thread to human review (`src/inbox_intel.py`, `INJECTION_RE`; tests in `tests/test_inbox_intel.py::Injection`, benchmark fixtures V16 and V17).
- **Models are not trusted with permissions.** No model provider is configured. The rules engine has no send path (`tests/test_inbox_intel.py` asserts the module has no `send` function).
- **Browser input is untrusted.** Every mutation requires a JSON content type and the session token. Origin and host checks run before the route (`resolve_app.py::check_origin`).

## Tenant isolation

- Every store method takes `(user_id, workspace_id)` and filters by both. The API derives both from the authenticated session; a client-supplied `user_id` or `workspace_id` is ignored (`tests/test_inbox_api.py::test_a_client_supplied_user_id_is_never_trusted`).
- Cross-user reads, edits and drafts return 404 (`test_another_user_cannot_read_or_change_my_threads_commitments_or_drafts`).
- Replay data lives in `<workspace>.replay` and cannot appear in LIVE views (`test_replay_runs_the_same_pipeline_without_touching_gmail_and_stays_separate_from_live`).

## Encryption at rest

- Sealed with AES-256-GCM under a per-tenant key derived with HKDF from the master key (`src/at_rest.py`). Associated data binds each record id, user and workspace, so a sealed value cannot be moved to another record.
- Sealed fields: message subject and body, commitment obligation text and evidence, draft subject and body, draft versions, draft grounding metadata, commitment notes, follow-up notes, correction explanations, rule instructions, thread-state reasons, OAuth tokens, replay outbox messages.
- Verified end to end: `tests/test_inbox_api.py::test_email_text_never_appears_in_plaintext_in_the_database_file` searches the raw SQLite file for a unique phrase after sync, draft, approval, send and correction.
- Stored in plaintext (metadata, needed for queries): ids, timestamps, statuses, sender and recipient addresses, deadline timestamps, timezone names, categories, priorities, digests, and a keyed blind index of each obligation.
- **The database file itself is not fully encrypted.** Metadata above is readable by anyone with the file and the schema. This is a stated limitation, not a claim.
- Obligation identity is a keyed HMAC (`at_rest.SealedStore.blind_index`), so the database alone cannot be used to guess obligation text.

## Gmail OAuth

- Authorization code flow with PKCE (S256). The verifier stays on the server (`src/gmail_adapter.py::OAuthStates`). The start response never includes it (`test_authorization_url_uses_pkce_s256_and_only_the_requested_scopes`).
- State is random, single-use (removed on first presentation, valid or not), expires after 10 minutes, and is bound to the user and workspace that started the flow. Forged or replayed state is refused (`test_state_is_single_use_and_expires`, `test_forged_state_is_refused_and_no_token_is_stored`).
- Minimum scope is `gmail.readonly`. `gmail.compose` and `gmail.send` are requested only when the user asks to enable drafting or sending. Sends and drafts are refused with `SCOPE_NOT_GRANTED` when the permission is absent.
- Tokens are sealed, never returned by any route, never exported, and never placed in exceptions (`test_tokens_are_sealed_at_rest_and_never_returned`, `test_export_includes_inbox_data_and_no_credentials`).
- Refresh failure with `invalid_grant` is recorded as REVOKED on the connection and shown to the user. Rate limits are recorded as RATE_LIMITED. Neither is presented as success (`test_revoked_grant_is_reported_as_revoked_and_sync_is_refused`, `test_rate_limit_is_a_visible_state_not_a_success`).
- Disconnect attempts revocation at Google and reports whether Google confirmed it.
- **NOT VERIFIED:** the live OAuth flow and a live Gmail account. Requires `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI` and an authorized test account.
- Pending OAuth states are held in memory. A server restart drops them; the user starts the connection again. This fails closed.

## Approval and sending

- A draft's digest is computed by the store from thread, recipient, cc, subject and body (`src/inbox_store.py::content_digest`). Callers cannot supply it. Identical content always produces the same digest.
- Approval binds to the current digest. Any edit creates a new version and digest, invalidates earlier approvals, and returns the draft to DRAFT (`test_editing_after_approval_invalidates_the_approval`).
- An approval is single-use, expires after 15 minutes, and is refused if the draft's current digest no longer matches the approval. This last check was added after the broker test showed an old approval could send new content (`tests/test_send_broker.py::test_a_changed_draft_cannot_be_sent_with_an_old_approval`).
- A verified draft is terminal. A second approval of the same content is refused before any dispatch (benchmark send-path scenario `ok`).
- Execution is recorded durably before the provider is called. The operation key is derived from the operation and content digest, so identical content maps to one operation.
- Before any send, Sent is searched read-only by correlation Message-ID. A match is verified against the stored content and never re-sent.
- **No marker is added to outgoing content.** Correlation uses an RFC 822 `Message-ID` header (`resolve.<key>@mail.resolve.invalid`). `tests/test_send_broker.py::test_no_marker_or_extra_text_reaches_the_recipient` checks that the recipient's body is exactly the approved text.
- Success requires a read-back of the stored message matching the correlation Message-ID, recipient, cc, subject and normalized body. Anything else is UNCERTAIN with a specific reason (RECIPIENT_MISMATCH, BODY_MISMATCH, CORRELATION_MISSING, READBACK_FAILED).
- A timeout is UNCERTAIN, never SUCCESS. Reconciliation is read-only and never dispatches (`test_timeout_is_uncertain_and_reconciliation_finds_the_send_without_resending`).
- Mutation check: with the pre-send Sent search and the durable claim both removed, the same approved content is sent twice. With them present, it is sent once (`tests/test_send_broker.py::MutationCheck`).
- **NOT VERIFIED:** whether Gmail preserves a client-supplied `Message-ID` and answers `rfc822msgid:` searches unchanged. If it does not, read-back fails closed to UNCERTAIN and nothing is resent. Requires a live test account.

## Commitments

- Status changes follow an explicit transition table (`COMMITMENT_TRANSITIONS`). Terminal states do not move. Invalid transitions return 409.
- RESOLVED_VERIFIED requires a verified execution record in the same workspace. User confirmation is RESOLVED_USER_CONFIRMED and is never presented as verified (`test_user_confirmation_is_never_recorded_as_verified`, `test_verified_resolution_needs_a_verified_execution_record`).
- Owners come from the authenticated mailbox address and the actual sender or recipient. An unresolved party is stored as unresolved, not guessed (`ownership_state`).
- Deadlines use the user's configured IANA timezone. Without one, the UI says UTC is in use. Vague phrases ("soon", "when you can") are AMBIGUOUS and get no date.

## Voice rules

- Parsing is a preview. Nothing is stored or activated by parsing (`test_parse_is_a_preview_and_stores_nothing`).
- An instruction RESOLVE does not recognise (UNKNOWN) is stored inactive and cannot be activated (`test_unknown_rules_are_never_activated`).
- A rule that would send email without approval is refused (`test_a_rule_that_removes_send_approval_is_refused`).
- High-impact rules need an explicit second confirmation. Conflicts with active rules are reported and never silently overridden.
- Versions are kept; reversal is recorded in the audit log.

## Corrections

- Every correction records before, your words, after and impact. Scope is explicit. A workspace-wide preference needs `confirmed: true` (`test_workspace_preference_needs_explicit_confirmation`).
- A one-time correction affects only its conversation (`test_one_time_correction_changes_only_that_thread_and_shows_before_and_after`).

## Logging and audit

- The server logs the route and the exception type for inbox errors, never exception text or message content (`resolve_app.py::send_inbox`).
- Audit events store identifiers, statuses and metadata such as scope or operation. The audit writes in `src/inbox_store.py` pass only ids, statuses and short labels; no audit call receives message text. This is a code-review statement, not a dedicated test. The activity route is described in `docs/INBOX_API.md`.

## Privacy controls

- `POST /api/privacy/export` includes inbox data for the live and replay workspaces. Tokens are excluded.
- Workspace and account deletion purge inbox rows for both workspaces. The receipt reports `inbox_rows_removed` for workspace deletion.

## Known limitations

1. Live Gmail behaviour (Message-ID preservation, `rfc822msgid:` search, scopes returned by Google) is **NOT VERIFIED** here.
2. No model provider is configured. MODEL_ENABLED behaviour is **NOT MEASURED**; the product reports RULES_ONLY.
3. Instruction-detection is a list of phrases. It reduces the chance that email text becomes an obligation; it is not a security boundary.
4. Pending OAuth state and replay progress are in memory and are lost on restart.
5. The database file is not fully encrypted (see above).
6. Gmail's "unread" label is not stored, so the unread filter is not offered.
7. Sync is bounded by the query window (default 30 days) and a page limit; very large mailboxes are not fully synchronized.
8. A development key file is used unless `RESOLVE_MASTER_KEY` is set. Production refuses to start without the environment key (`at_rest.py`).

## Added in the 2026-10-31 sprint

- **Sessions.** The tenant database stores SHA-256 of each bearer token, never the token. Sessions created before this
  change are invalidated on start. Idle sessions expire after 7 days; renewal never extends a session beyond 30 days after
  sign-in. `POST /api/auth/password` verifies the current password and revokes every session of the user.
- **Inbox routes need an explicit valid token in every mode.** A missing or invalid token is 401, also in local
  development (the development UI sends its token explicitly).
- **Local mail server.** Binds to loopback only; every submission is authenticated; MAIL FROM must equal the
  authenticated account; recipients outside `mail.resolve.test` are refused (550), so nothing leaves the machine. Account
  passwords are PBKDF2-hashed on the server; RESOLVE stores the connected mailbox's password sealed like an OAuth token.
  Colleague accounts used to deliver test mail can never be a user's own mailbox.
- **Model output is advisory.** It is schema-validated, every quote is checked against the stored message, and it cannot
  approve, send, activate rules or change permissions. The API key is read from the environment only.
- **Privacy.** Export and workspace deletion cover LIVE, LOCAL and REPLAY scopes. Credentials are never exported.
