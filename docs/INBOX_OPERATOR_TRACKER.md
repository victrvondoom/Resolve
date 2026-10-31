# RESOLVE — Inbox-to-Commitment Operator: implementation tracker

Status key: **DONE** (implemented and tested in this repository) · **PARTIAL** (implemented with a stated gap) · **NOT DONE** · **BLOCKED** (needs something this environment does not have).
Every DONE item names its evidence. Test names refer to `tests/`.

## Morning inbox

| Item | Status | Evidence / gap |
|---|---|---|
| Brief populated from stored records; every section computed | DONE | `src/inbox_service.py::morning_brief`; `test_morning_brief_is_built_from_persisted_records_in_the_user_timezone`; browser `test_replay_lab_runs_the_synthetic_morning_through_the_ui` |
| Summary numbers come from records; cards navigate to filtered views | DONE | `web/static/inbox.js` (`kpi` action); browser test checks non-zero counts after replay |
| Needs attention shows reason, evidence, decision, deadline, confidence, source link | DONE | `web/static/inbox.js` Morning brief; reasons come from triage (`src/inbox_intel.py`) |
| Replies to send excludes replies already sent | DONE | `awaiting_user_reply` uses the last message's direction; `test_thread_detail_returns_the_conversation_with_direction_and_linked_evidence` covers direction |
| Timing groups: overdue, today, soon, future, unknown kept separate | DONE | `_timing` in `src/inbox_service.py` |
| Recently resolved shows verified and user-confirmed separately, 14-day window | DONE | `morning_brief`; labels in `web/static/inbox.js` |
| Sync progress and failure states | PARTIAL | Each run records RUNNING/SUCCEEDED/FAILED with counts and error; one sync at a time per mailbox (`test_concurrent_sync_of_one_mailbox_is_refused_not_duplicated`). There is no percentage progress, no live stream and no history-id incremental sync for Gmail |
| LIVE and REPLAY visibly different | DONE | Banner and chips in `inbox.js`; separate workspaces (`test_replay_runs_the_same_pipeline_without_touching_gmail_and_stays_separate_from_live`) |
| LIVE never shows a demo mailbox | DONE | Brief returns no sections without a connection (`test_brief_is_empty_and_honest_when_not_connected`) |
| Empty, loading, error states on every section | DONE | `loading`, `empty`, `errorBox` helpers; empty states named per section |

## Inbox workspace

| Item | Status | Evidence / gap |
|---|---|---|
| Filters: all, action, reply, my commitments, waiting, deadline, important, automated, needs review | PARTIAL | Implemented in `GET /api/inbox/threads?filter=`. No dedicated filter test exists; the browser test only opens the default list |
| Search, sort by date and priority, date range, pagination | DONE | `q`, `sort`, `since_days`, `page`, `size` |
| Unread filter | NOT DONE | Gmail's UNREAD label is not stored. Documented as a limitation |
| Thread detail: full chronology, direction, recipients, attachments | PARTIAL | Direction and recipients shown. Attachments are not shown (not stored) |
| Analysis panel with evidence linked to message ids | DONE | `thread_detail` and `_message_for`; `test_thread_detail_returns_the_conversation_with_direction_and_linked_evidence` |
| Corrections: priority, category, not actionable, not a commitment, wrong owner, wrong deadline, completed, automated, other | DONE | `POST /api/inbox/threads/{id}/corrections`; `test_one_time_correction_changes_only_that_thread_and_shows_before_and_after`, `test_not_commitment_dismisses_with_a_reason`, `test_deadline_correction_keeps_the_previous_value` |

## Commitments

| Item | Status | Evidence / gap |
|---|---|---|
| Request to user, your promise, promise to you, dependency kept separate | DONE | `COMMITMENT_KINDS`; `tests/test_inbox_api.py::test_live_sync_assigns_owners_from_the_real_mailbox_and_uses_the_timezone` |
| Owners from the real mailbox address and actual sender or recipient | DONE | same test; `extract_commitments` |
| Unresolved owner stored as unresolved, not invented | DONE | `ownership_state` (`RESOLVED`, `PARTIAL`, `UNRESOLVED`, `USER_CORRECTED`) |
| Deadline keeps phrase, timezone, confidence, review reason, explicit flag | DONE | `commitments` columns, migration 5 |
| Configured IANA timezone; UTC is never silent | DONE | `set_settings` validation; brief shows "timezone not set" |
| Vague deadlines not invented | DONE | `AMBIGUOUS`; `test_ambiguous_deadline_is_flagged_not_invented`; fixtures V07, V21 |
| Changed deadline revises and keeps history | DONE | `reconcile_obligation`; `test_changed_deadline_is_revised_and_history_is_kept` |
| Duplicate control: re-sync and re-processing create nothing new | DONE | `reprocess_new_items_on_second_pass: 0` in `benchmarks/results/inbox_metrics.json`; `test_repeated_sync_creates_no_duplicates_and_records_each_run` |
| Restated and re-dated requests matched as one obligation | DONE | `same_obligation` (Jaccard 0.7); fixtures V18, V20 |
| Distinct requests in one email kept apart | DONE | fixture V19 |
| Validated status machine, no arbitrary PATCH | DONE | `COMMITMENT_TRANSITIONS`; `test_status_transitions_are_validated_and_terminal_states_are_final` |
| Verified completion only with a verified execution record | DONE | `test_verified_resolution_needs_a_verified_execution_record`; API `test_user_confirmation_is_distinct_from_verified_execution` |
| Follow-up with recorded trigger and target | PARTIAL | Recorded. No scheduler or notification is delivered; nothing is sent |
| Edit, correct owner, change deadline, notes, dismiss with reason, cancel | DONE | `PATCH /api/inbox/commitments/{id}`; `POST .../notes` |

## Wispr Flow workflow

| Item | Status | Evidence / gap |
|---|---|---|
| Dictation-friendly Voice Rules page with the required guidance | DONE | `web/index.html` view `rules`; `web/static/inbox.js` `renderRules` |
| Standard text fields accept dictation (plain textarea, no special handling) | DONE by design | Wispr Flow itself was **not** run here. Evidence needs the account holder's recording (`docs/WISPR_DEMO_SCRIPT.md`) |
| Structured rule preview with type, scope, value, status, explanation, confirmation, conflicts | DONE | `POST /api/inbox/rules/parse`; `06_voice_rules_preview.png` |
| Unknown rules never activated | DONE | `test_unknown_rules_are_never_activated` |
| High-impact and global rules need confirmation; send-approval removal refused | DONE | `test_high_impact_rule_needs_explicit_confirmation_and_can_be_reversed`, `test_a_rule_that_removes_send_approval_is_refused` |
| Conflicts reported, not silently overridden | DONE | `conflicts()`; `rules_add` refuses when `confirm` and conflicts |
| Corrections dictated into normal fields, with before, correction, after, impact | DONE | `test_one_time_correction_changes_only_that_thread_and_shows_before_and_after`; `03_thread_detail_replay.png` |
| One-time and persistent scopes separate; persistent needs confirmation | DONE | `test_workspace_preference_needs_explicit_confirmation` |
| Persistent preferences survive restart | DONE | `tests/test_local_mail.py::test_rules_and_corrections_persist_across_restart` |
| No undocumented Wispr API claimed | DONE | `docs/WISPR_DEMO_SCRIPT.md`, `docs/INBOX_SECURITY.md` |
| Demo script prepared; user recording left as user-owned evidence | DONE | `docs/WISPR_DEMO_SCRIPT.md` (no recording exists in the repository) |

## Drafts and verified execution

| Item | Status | Evidence / gap |
|---|---|---|
| Context-aware draft from the thread, facts only, missing information listed | DONE | `draft_reply`; `test_draft_recipient_comes_from_the_thread_and_missing_facts_are_listed`; `07_draft_editor_replay.png`; benchmark unsupported-claim rate 0.0 |
| Structured model provider path | DONE (transport) | `src/model_provider.py`: OpenAI-compatible endpoint, schema validation, quote verification, timeout/invalid fallback; `tests/test_model_provider.py` against a scripted HTTP server. **No model was run**: MODEL_ENABLED is BLOCKED here (no runtime) |
| Model-generated drafts | NOT DONE | Drafts stay rules-only. Model output is advisory analysis (`POST /api/inbox/threads/{id}/analyze`) |
| Drafts persist with versions, digest and source | DONE | `draft_versions`; `test_editing_after_approval_invalidates_the_approval` |
| Approval bound to exact content and invalidated by any change | DONE | `tests/test_send_broker.py`; `test_a_changed_draft_cannot_be_sent_with_an_old_approval` |
| Single-use, expiring approval | DONE | `test_send_is_read_back_verified_and_the_approval_is_single_use` |
| No visible marker in outgoing content | DONE | `test_no_marker_or_extra_text_reaches_the_recipient` |
| Correlation by RFC 822 Message-ID; Gmail behaviour verified | PARTIAL | Implemented and tested with the scripted double. Live Gmail preservation of the Message-ID and `rfc822msgid:` search are **BLOCKED** (no account) and fail closed |
| Provider response is not sufficient; read-back verifies content | DONE | `_verify_sent`, `_mismatch`; reasons RECIPIENT_MISMATCH, BODY_MISMATCH, CORRELATION_MISSING |
| Timeout is UNCERTAIN; uncertain never blindly resent; read-only reconciliation | DONE | `test_timeout_is_uncertain_and_reconciliation_finds_the_send_without_resending`; `test_reconcile_route_is_read_only_and_refuses_verified_records` |
| Duplicate-send protection mutation-tested | DONE | `tests/test_send_broker.py::MutationCheck` (sends twice when both protections are removed, once when present); benchmark send-path duplicate sends 0 |
| Execution timeline from stored records | DONE | `GET /api/inbox/executions/{id}`; `10_simulated_send_and_timeline.png` |
| Simulated sends labelled SIMULATED | DONE | `label` on send responses and in the UI |

## LOCAL mode (real local mail server)

| Item | Status | Evidence / gap |
|---|---|---|
| Real SMTP + POP3 server on loopback, Maildir on disk, survives restart | DONE | `src/local_mail.py`; `tests/test_local_mail.py::ServerProtocol` |
| Local-only delivery, authenticated submission, no sender spoofing | DONE | `test_external_recipients_are_refused_so_nothing_leaves_the_machine`, `test_sender_cannot_be_spoofed` |
| Provider adapter with the Gmail client contract (send, drafts, read-back, correlation) | DONE | `tests/test_local_mail.py::ProviderContract` |
| Full pipeline: deliver → sync → brief → draft → exact approval → send → POP3 read-back VERIFIED; effect checked again in the recipient's Maildir file | DONE | `test_full_local_journey_is_verified_by_independent_read_back`; HTTP: `tests/test_operations.py::RealServer`; browser: `test_local_mail_journey_through_the_ui_ends_verified`; `docs/screenshots/inbox/13_local_send_verified.png` |
| Lost acknowledgement → UNCERTAIN → read-only reconcile finds the delivered message, no resend | DONE | `test_timeout_is_uncertain_and_reconcile_is_read_only` |
| LOCAL kept apart from LIVE and from other users | DONE | `test_local_records_are_separate_from_live_and_from_other_users` |

## Replay lab

| Item | Status | Evidence / gap |
|---|---|---|
| Synthetic dataset, 12 threads, 16 messages, all labelled | DONE | `src/inbox_replay.py`; `[REPLAY]` prefix and reserved test domain |
| Runs through the same sync, triage, commitment and send code | DONE | `ReplayProvider` implements the same interface; `replay_step` calls `svc.sync_connection` |
| Start, pause, step, resume, replay, reset | DONE | `/api/inbox/replay/*`; browser test `test_replay_lab_runs_the_synthetic_morning_through_the_ui` |
| Failure injection: send timeout after storing | DONE | `inject`; `test_replay_send_timeout_injection_is_uncertain_and_recoverable` |
| Replay progress survives a server restart | NOT DONE | Progress is in memory; replay data is stored. Documented |
| Reset removes only replay data | DONE | `test_replay_reset_removes_only_replay_data` |

## Gmail

| Item | Status | Evidence / gap |
|---|---|---|
| OAuth start, PKCE, state validation, callback, code exchange | DONE (recorded responses) | `tests/test_inbox_api.py` OAuth tests with a scripted transport |
| Refresh, revocation, rate limit, expired token states | DONE (recorded responses) | `test_revoked_grant...`, `test_rate_limit...` |
| Minimum permissions; scope upgrades on demand | DONE | `gmail_start` scopes; `SCOPE_NOT_GRANTED` |
| Sealed token storage; disconnect and revocation | DONE | `test_tokens_are_sealed_at_rest_and_never_returned`, `test_disconnect_revokes_at_provider_and_removes_the_token` |
| Live OAuth round trip with Google | BLOCKED | Requires `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI` and an authorized test account |
| GMAIL NOT CONFIGURED shown with the variable names | DONE | `connections` and `11_connections_gmail_not_configured.png` |

## Product and engineering

| Item | Status | Evidence / gap |
|---|---|---|
| Inbox is the default destination; Advanced tools grouped | DONE | `web/index.html` navigation; `app.js` default view `morning` |
| All primary screens reachable and backed by the API | DONE | Browser tests; `docs/screenshots/inbox/` |
| No dead buttons | PARTIAL | Each button calls a route, except navigation links. The workspace delete button calls `/api/privacy/delete-workspace` after two confirmations. Not every control has been exercised in a browser; the browser tests cover the main journey |
| Privacy controls include inbox data | DONE | `privacy_engine` export and deletion; `test_export_includes_inbox_data_and_no_credentials` |
| Email text sealed at rest; not plaintext in the database file | DONE (metadata not sealed) | `test_email_text_never_appears_in_plaintext_in_the_database_file`. Full database encryption is **not** claimed |
| Server logs carry no email text | DONE by code review | `resolve_app.py::send_inbox` logs route and exception type only |
| Benchmarks on expanded labelled set | DONE | 32 new scenarios written before scoring; `docs/INBOX_BENCHMARK_REPORT.md` |
| Live provider and model benchmarks | NOT MEASURED | Stated in the report |
| API documentation checked against the code | DONE | `tests/test_inbox_docs.py` |
| Screenshots captured from the running app | DONE | `scripts/capture_inbox_screens.py`; `docs/screenshots/inbox/` |
| Inbox routes refuse missing or invalid tokens in every mode; hashed session tokens; password change revokes sessions | DONE | `tests/test_operations.py` |
| Migrations atomic, WAL, status, backup/verify/restore | DONE | `tests/test_operations.py`; `scripts/resolve_admin.py` |
| Liveness and readiness endpoints | DONE | `/api/livez`, `/api/readyz`; `tests/test_operations.py::RealServer::test_liveness_and_readiness` |
| Frontend split into modules (`inbox-components.js`) | PARTIAL | One module, `web/static/inbox.js`, plus `inbox.css` |
