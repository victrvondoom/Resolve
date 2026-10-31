# RESOLVE release report: 2026-10-31 sprint

Evidence-based. Every "done" below points to a test, a file or a command in this repository. Nothing here was run
against live Gmail, a real language model, Wispr Flow, Docker or native Windows.

## A–B. Repository, branch, commits

- Repository: `victrvondoom/Resolve`.
- Work starts from the previous checkpoint, branch `claude/beautiful-bardeen-fw59qp` at `819193d` (which contains
  `7932353`), merged with `master` (`22c848d`) as `fed4750`.
- Work branch: `claude/fervent-gates-tsa6vp` (the session's designated branch). Final commit: see `git log -1`;
  commits of this sprint are listed under X.
- Push: see X. Nothing was force-pushed; no other branch was modified.

## C–D. Task accounting (self-assessment against evidence)

The 468 tasks were checked workstream by workstream against code and tests. **388 of 468** have an implementation and
automated-test or document evidence in this repository. **80 are incomplete.** This is a self-assessment, not an
independent audit, and "has evidence" is weaker than the brief's 10-point product contract: in particular no Gmail item
is verified against a live account, and no model item is verified with a real model.

| WS | Area | Evidenced | Incomplete task IDs (reason) |
|---|---|---:|---|
| 01 | Mission and contract | 10 | 6, 7, 12 (no complete inventory of every visible feature mapped to UI/API/persistence/test/evidence) |
| 02 | Forensic inspection | 11 | 24, 25 (duplicates and every security boundary not exhaustively audited; one orphaned view found and fixed) |
| 03 | Feature inventory | 6 | 30, 31, 32, 33, 36, 37, 39 (`docs/FEATURE_MATRIX.md` predates this sprint and is not a full per-route matrix) |
| 04 | Architecture | 5 | 40, 42, 43, 44, 49, 50, 51, 52 (no DI container, no request schema layer, no inbox event model/SSE, routes call stores directly) |
| 05 | Runtime and config | 6 | 53, 54, 55, 56, 60, 61, 65 (no single bootstrap command, versions not pinned, no graceful shutdown, no structured redacting logger, no Windows setup verified) |
| 06 | Auth and tenant isolation | 11 | 74 (cross-workspace browser/file tests not extended this sprint), 78 (no full authorization review of every endpoint) |
| 07 | Data model | 11 | 79 (no workspace membership table: owner-only workspaces), 90 (reminders recorded, never delivered) |
| 08 | DB consistency | 11 | 100 (concurrency tested for sync only), 103 (interrupted-write recovery tested for migrations only) |
| 09 | Encryption | 11 | 111 (logs reviewed by code only), 112 (backups hold sealed fields but the backup file itself is not encrypted) |
| 10 | Gmail OAuth | 11 | 128 (no step-by-step wizard beyond the Connections panel), 130 (no live verification command; needs an account) |
| 11 | Providers | 11 | 133 (Gmail HTML-only bodies), 134 (Gmail attachment retrieval; local adapter records attachment metadata only) |
| 12 | Sync engine | 12 | 148 (messages deleted at the provider are skipped, not reflected locally) |
| 13 | Hybrid intelligence | 10 | 159 (existing model routing not integrated), 160 (local model path configured but never executed here), 169 (model benchmark not measured) |
| 14 | Commitments | 13 | — |
| 15 | Deadlines | 12 | 191 (fulfilment by a later verified action is recorded via resolution, not detected automatically) |
| 16 | Morning brief | 11 | 206 (no date/priority filters on the brief endpoint), 208 (no restart test for the brief itself) |
| 17 | Wispr Flow workflow | 12 | 221 (the recording is user-owned evidence; none exists) |
| 18 | Voice policy | 12 | 226 (waiting-on tracking preference has no dedicated rule type) |
| 19 | Corrections | 12 | 243 (duplicate persistent rules not explicitly prevented by a test) |
| 20 | Reply generation | 12 | 258 (dictated correction rewrites are applied by the user's edit, not merged) |
| 21 | Drafts | 11 | 265 (no BCC field), 273 (no single lifecycle test across restart, edit, save and discard) |
| 22 | Approval engine | 10 | 274, 275 (approval engine is inbox-specific; no product-wide risk classes), 280 (attachments not supported) |
| 23 | Execution and read-back | 11 | 291, 292 (Gmail Message-ID preservation and `rfc822msgid:` search unverified without an account; fails closed) |
| 24 | Causal State Ledger | 11 | 300, 301 (inbox executions use their own records, not the effect graph in `src/effect_graph.py`) |
| 25 | Inbox HTTP API | 12 | 324 (pagination only on thread lists) |
| 26 | Frontend shell | 11 | 335 (keyboard path not tested end to end), 338 (screenshots captured, no visual diffing) |
| 27 | Brief/inbox/thread UI | 12 | 345 (attachments not shown) |
| 28 | Commitment UI | 11 | 360 (reminders not delivered), 364 (not every commitment action has a browser test) |
| 29 | Draft and approval UI | 12 | 377 (approval invalidation by edit tested at API level, not in a browser) |
| 30 | Activity, connections, privacy UI | 12 | 390 (not every page exercised against a production-mode server) |
| 31 | Replay lab | 12 | 398 (replay progress is in memory and lost on restart; replay data persists) |
| 32 | Sandbox | 11 | 406, 414 (filesystem separation is per-folder, not OS isolation; no escape tests beyond path checks) |
| 33 | Browser automation | 12 | 428 (navigation races and concurrent sessions not tested this sprint) |
| 34 | Agents and capabilities | 10 | 434 (agent actions do not use the inbox approval boundary), 440, 441 (adapters and registry not re-reviewed this sprint) |
| 35 | Security, tests, benchmarks | 9 | 443 (no single security matrix document), 452 (no latency/sync performance benchmark), 453, 454 (readiness only, no metrics; no reliability report) |
| 36 | Deployment and release | 11 | 459 (Docker image not built here), 467 (push not completed from this session; see X) |

## E. Working screens (real Chromium, DOM-level)

Morning brief, Inbox, Thread detail, Commitments and detail, Drafts and exact approval, Activity, Voice rules,
Connections (Gmail, **Local mail server**, Replay), Privacy & settings, Replay lab; Advanced tools (14, including the
restored Mission control link). Browser tests: `tests/test_inbox_ui.py` (4), `tests/test_landing_ui.py`,
`tests/test_ux_e2e.py`, `tests/test_flagship_e2e.py`.

## F. API groups

`/api/inbox/*` (connections incl. `local/*`, sync, brief, threads, analyze, corrections, commitments, drafts, approvals,
send, executions, reconcile, rules, activity, replay, engine; documented and checked by `tests/test_inbox_docs.py`),
`/api/auth/*` (incl. `password`), `/api/livez`, `/api/readyz`, plus the earlier documents, missions, sandboxes,
cloud-browser, privacy and tools groups.

## G–H. Providers and local-provider proof

- **Local mail server (LOCAL)**: real SMTP/POP3 over loopback, Maildir on disk. Proof: `tests/test_local_mail.py`
  (24 tests: protocol, relay refusal, spoofing refusal, persistence across server restart, provider contract, full
  journey VERIFIED by POP3 read-back and re-checked in the recipient's Maildir file, lost acknowledgement → UNCERTAIN →
  read-only reconcile, tenant separation, deletion/export), `tests/test_operations.py::RealServer` (HTTP against the
  real server process), browser `test_local_mail_journey_through_the_ui_ends_verified`,
  `docs/screenshots/inbox/13_local_send_verified.png`.
- **Gmail**: complete adapter with recorded-response tests (OAuth PKCE, refresh, revocation, rate limit, incremental
  history sync, retry policy, send/draft read-back). Status: **EXTERNAL_ACCOUNT_AUTHORIZATION_REQUIRED**.
- **Replay**: synthetic, labelled SIMULATED, same pipeline.

## I. Gmail OAuth

Implemented (PKCE, single-use state, minimal scopes upgraded on demand, sealed tokens, revocation). Activation needs
`GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GOOGLE_OAUTH_REDIRECT_URI` and an authorized account. Never run
live here.

## J. Model provider

`src/model_provider.py`: RULES_ONLY default; OpenAI-compatible endpoint path (`RESOLVE_MODEL_BASE_URL`,
`RESOLVE_MODEL_NAME`, optional `RESOLVE_MODEL_API_KEY`); schema validation; quote verification; unsupported-claim
guard for drafts; fallback reasons. Tested against a scripted HTTP server (`tests/test_model_provider.py`, 12) and an
in-process stub (`test_model_draft_path_needs_the_same_approval_and_falls_back_on_unsupported_claims`). **No model was
run; MODEL_ENABLED is not activated or measured.**

## K. Wispr Flow

Ordinary focused text fields (Voice rules, corrections, draft body, notes, local-mail message body). No SDK and no
Wispr integration is claimed. `docs/WISPR_DEMO_SCRIPT.md` is ready; the recording is user-owned and does not exist.

## L–O. Sandbox, browser, agents, documents

Unchanged this sprint except the restored Mission control navigation. Evidence remains `harness/evidence/G12.md`,
`G16.md` and their tests, which pass in the full suite. Sandbox isolation is per-folder, not OS-level (see 406/414).

## P. Authentication and tenant isolation

Inbox routes now need an explicit valid token in every mode (a missing or invalid token previously fell back to the
demo tenant in development). Session tokens are stored hashed; renewal capped at 30 days; password change revokes all
sessions. Cross-user tests: threads, drafts, approvals and executions over HTTP (`RealServer`), plus the earlier
`tests/test_inbox_api.py` and `tests/test_tenant_security.py`.

## Q. Encryption and privacy

Per-field AES-256-GCM with per-(user, workspace) HKDF keys and record binding (unchanged). Fixed: key rotation could
not introduce a new key version. Fixed: workspace deletion and export skipped LOCAL data and credentials. What stays
metadata is listed in `docs/INBOX_SECURITY.md`. No full-database encryption is claimed.

## R. Test suite

See `PROGRESS.md` for the exact final command and counts. Skips: Windows-native (`WINDOWS_NATIVE_TESTS_NOT_RUN`) and
environment-dependent tests with reasons (`pytest -rs`).

## S. Benchmarks (labelled fixtures only; `docs/INBOX_BENCHMARK_REPORT.md`)

Category accuracy 96.9% on 32 v2 scenarios (75.0% on 12 v1); commitments precision 83.3%, recall 100% (25 expected,
30 ledger items); owner accuracy 100%; deadline accuracy 92.0%; reply-needed precision 92.3% (tp 12, fp 1), recall
85.7% (fn 2); unsupported draft claims 0.0%; injection fixtures resisted 2 of 2; send-path duplicate sends 0. Re-run
after this sprint's deadline fixes: unchanged. Live provider and model: NOT MEASURED.

## T. Failure injection and mutation

Lost acknowledgement (Gmail double, replay, **real local mail server**), timeout before store, wrong recipient,
altered body, dropped Message-ID, failing migration (no partial schema), failed sync (checkpoint not advanced),
concurrent sync (refused), tampered backup (restore refused). Mutation: `tests/test_send_broker.py::MutationCheck`.

## U. Screenshots

`docs/screenshots/inbox/00`–`12` (earlier, replay) and `13_local_send_verified.png` (this sprint, captured by the
browser test from the running app).

## V. Setup

`docs/INBOX_OPERATOR_SETUP.md` (replay, local mail, model endpoint, operations, Gmail), `docs/DEPLOYMENT.md`.

## W. External requirements

Gmail: Google OAuth client and an authorized account. Model: a reachable OpenAI-compatible endpoint. Wispr Flow: the
account holder's recording. Windows native: an interactive Windows session. Docker: a host that can build images.

## Y. Known limitations (reproducible)

Replay progress is lost on restart; reminders are not delivered; no unread filter; attachments not shown; Gmail
deletions not reflected; drafts can be model-written only when a model is configured, and triage stays rules-based;
sandbox isolation is per-folder; the local mail server implements the SMTP/POP3 subset RESOLVE needs, not a full MTA.
