# Security and reliability remediation — findings log

Labels: CONFIRMED (reproduced here), POTENTIAL (code-read only), NOT REPRODUCIBLE, BLOCKED (missing infrastructure).
Severity: P0 (unsafe to run in production), P1, P2, P3.

## F-001 — P0 CONFIRMED — Host command execution through the sandbox API
- **File / function:** `src/resolve_app.py` `POST /api/sandboxes/{id}/action` → `src/sandbox_manager.py::execute_in_sandbox` (previously `subprocess.run` on the application host).
- **Reproduce (safe):** production server, register, create sandbox, `POST .../action {"command":["echo","RESOLVE-REPRO-MARKER"]}` → HTTP 200 with the marker in `stdout`. (Harmless marker only.)
- **Expected:** refused before any process is created, in production, unless an isolated provider is configured and reports itself production-ready.
- **Actual (before):** any authenticated user ran arbitrary commands as the server user.
- **Fix:** `src/isolation.py` defines `IsolationProvider` / `ProviderReport` (isolation type, production_ready, filesystem, network, limits, cleanup, availability). There is NO host-subprocess provider. Default is `UnavailableProvider`; unknown names are a startup error. `execute_in_sandbox` checks the report BEFORE any state change or process creation and raises `ExecutionBlocked`, mapped to HTTP 503 `EXECUTION_BLOCKED`.
- **Regression tests:** `tests/test_remediation_p0.py` (host command refused; marker not created; refusal for every command shape; unauthenticated refused; provider default and unknown-name refusal).
- **Mutation check:** making the unavailable provider run `subprocess` (both gates flipped) made 6 tests fail; restored, all pass.
- **Remaining limitation:** no isolated provider exists in this codebase. Command execution is therefore unavailable everywhere. Resource-limit enforcement (TASK 010) and disposable-environment reachability checks (TASK 009) cannot be verified until a real provider exists. Their requirements are encoded in the `ProviderReport` fields.
- **Tests changed (intentional):** `tests/test_sandbox_manager.py::test_command_execution_in_sandbox` and `tests/test_cloud_api.py` step 4 asserted that host commands succeed. They now assert the refusal. A test double (`tests/isolation_fixtures.py`, labelled as running nothing) exercises the state checks.

## F-002 — P0 CONFIRMED — Sandbox ownership check skipped when the caller identity is missing
- **File / function:** `src/sandbox_manager.py::get_sandbox` (`if caller_user_id and ...`), same pattern in `set_status`, `pause/resume/terminate/destroy`.
- **Expected:** a missing identity is refused; ownership is always checked.
- **Actual (before):** an optional identity meant no check at all.
- **Fix:** `caller_user_id` and `caller_workspace_id` are required; empty values raise `SandboxSecurityError` (tested).

## F-003 — P0 CONFIRMED — Sandbox access not bound to workspace
- **File / function:** `sandbox_manager.py` (lookup by id and user only); `resolve_app.py` sandbox routes.
- **Fix:** every lookup filters on `id AND owner_user_id AND workspace_id`. A cross-workspace or cross-user request gets the same 404 as a missing sandbox, so existence is not revealed. `list_sandboxes` is scoped to user and workspace. Privacy export/workspace deletion use workspace scope; account deletion uses an account-scope method that is never exposed to a request handler.
- **Regression tests:** `tests/test_remediation_p0.py::test_same_user_other_workspace_cannot_see_or_control_sandbox`, `::test_different_user_cannot_read_pause_destroy_or_execute`. Note: `POST /api/workspaces/switch` changes the session in place (same token, new workspace); the test documents this.

## F-004 — P0 CONFIRMED — Destroy closed the browser before authorization
- **File / function:** `resolve_app.py` `POST /api/sandboxes/{id}/destroy` called `cloud_browser_pool.close_session(sbx_id)` before `destroy_sandbox` checked ownership, so a non-owner could close another user's browser.
- **Fix:** authorize the exact sandbox first, then close only the caller's own session, then destroy, then verify no session remains.
- **Regression tests:** `test_different_user_cannot_read_pause_destroy_or_execute` (non-owner destroy = 404 and the owner's sandbox is still READY); `tests/test_remediation_p0.py::BrowserPoolBinding::test_unauthorised_close_leaves_the_session_open`.

## F-005 — P0 CONFIRMED — Browser pool sessions not bound to user + workspace; optional identity bypass
- **File / function:** `src/cloud_browser.py::CloudBrowserPool` (`get_session` skipped the owner check when `caller_user_id` was None; `close_session` had no authorization).
- **Fix:** sessions carry `workspace_id`; `get_or_create`, `get_session`, `close_session` require both identities; mismatches are reported as "no active browser" (404). `close_session` returns False and changes nothing for a non-owner.
- **Regression tests:** `tests/test_remediation_p0.py::BrowserPoolBinding` (4 tests).

## F-006 — P2 CONFIRMED — Generic 500 responses return raw exception text
- **File:** `src/resolve_app.py` `do_POST` / `do_GET` final `except Exception` returns `str(exc)`.
- **Status:** NOT FIXED in this round (scheduled with the error-handling review, Phase 11).

## F-007 — P0 CONFIRMED (by code reading and tests) — Idempotency record not tenant-scoped; contract digest omitted the tenant
- **File / function:** `src/operation_contract.py` (`EffectLedger` keyed by idempotency key only; `execute` digest did not include user or workspace).
- **Expected:** a key from another user or workspace is a different operation; the same key with a different contract is a conflict, never a replay.
- **Actual (before):** the ledger returned a prior VERIFIED result for any caller presenting the same key.
- **Fix:** the record is scoped by `(user_id, workspace_id, idempotency_key)`; the contract digest covers the full approved identity (user, workspace, mission, revision, intent, operation, target, target version, normalized arguments, approval, key, preconditions, expiry, allowed operations); a key with a different digest returns `IDEMPOTENCY_CONFLICT` without dispatch. Claims and state updates use `BEGIN IMMEDIATE`; state transitions are conditional on the same contract and the expected current state (`StaleRecord` otherwise), and every persisted step is a legal transition.
- **Regression tests:** `tests/test_remediation_ieec.py` (conflict, other-tenant key, two ledger instances, reconcile that raises, conditional update, real cross-process claim race via fork+barrier). Mutation: removing the tenant scope fails 4 `TenantScopedIdempotency` tests.

## F-008 — P0 CONFIRMED — Direct tool API granted every capability and ran consequential services
- **File / function:** `src/resolve_app.py::handle_tool_call` (hard-coded capability set including `document:write`, `workspace:manage`, `mission:control`) and `src/tool_registry.py::ToolRegistry._authorize` (no approval check for `consequential` tools).
- **Reproduce (safe, production server):** `POST /api/tools/call {"name":"workspace.create", ...}` created a workspace; `document.generate` created a document.
- **Fix:** HTTP tool calls get a read-only capability set. The registry refuses a consequential tool unless `ToolContext.approved_operation` names that exact tool; that field is set only by the mission boundary and is never read from request data.
- **Regression tests:** `tests/test_remediation_p0.py::DirectToolApiCannotWrite` (refused, and storage checked for no created document/workspace; forged approval in body ignored; read-only tool still works); `tests/test_tool_registry.py::ConsequentialNeedsApproval`.
- **Remaining limitation:** consequential tool effects are NOT yet routed through the IEEC contract for the tool API. They are refused there; the mission path uses the guarded report write. Full routing of `document.transform`, `workspace.create` and connector writes through `execute()` is still open (see the readiness matrix).

## Tests changed in this round (intentional, documented)
- `tests/test_tool_registry.py`: validation and idempotency tests now run on an approved context, so they reach the code they test; new tests assert refusal without approval.
- `tests/test_sandbox_manager.py`, `tests/test_tenant_security.py`, `tests/test_cloud_browser.py`, `tests/test_cloud_api.py`, `tests/test_privacy_engine.py`: call signatures now include the workspace; execution assertions changed to refusal; cross-tenant assertions are 404 (uniform, no existence leak) instead of 403.
- `tests/test_ieec_adversarial.py`: mutation patches updated to the scoped ledger signatures; the stale-state path now reports `LEDGER_STATE_CONFLICT` (UNCERTAIN) instead of raising.

## Still open (not yet reproduced or fixed in this round)
See `docs/PRODUCTION_READINESS_MATRIX.md` for the status of every workstream.
