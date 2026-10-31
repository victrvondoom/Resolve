# Mission Assurance sprint — evidence log

Living document. Every entry records what was verified, by which command, and what remains open.

## Baseline (verified 2026-10-31)
- Branch `claude/beautiful-bardeen-fw59qp`, HEAD = origin (`e398e9f` at start of this sprint). Working tree clean. No worktrees.
- Previous report's full-suite result: **1 failed, 402 passed, 12 skipped** (`/tmp/phase3_full.txt`). The one failure
  (`tests/test_dashboard_e2e.py`) was fixed in `e398e9f`; the full suite was NOT rerun after that fix at the time of this entry.
- Previous claims re-checked: `src/operation_contract.py`, `src/ieec_harness.py`, `tests/test_ieec_adversarial.py` (21 tests),
  `benchmarks/ieec_bench.py` and `benchmarks/results/ieec_latest.json` exist as described. Mission Assurance UI: NOT present before this sprint.

## Consequential write map (task 007–008)
| Path | Effect | Guarded by IEEC contract? |
|---|---|---|
| `POST /api/brief-missions` → report write (`_guarded_report_write`) | save generated PDF | **Yes** |
| `POST /api/documents/upload` (`import_pdf`) | store uploaded PDF | No (direct, user-initiated) |
| `POST /api/documents/transform` (`transform`) | new PDF version (rotate/merge/delete pages) | No (direct) |
| `POST /api/documents/report` (`create_report`) | store generated report | No (direct) |
| `POST /api/documents/<id>/delete` (`delete_document`) | delete PDF | No (direct) |
| Tool registry `document.*` / `workspace.create` / `mission.cancel` | same as above via `/api/tools/call` | Idempotency keys only; not the IEEC guard |
| `POST /api/commands/execute` (`DELETE_REPORT`, `REVISE_DOCUMENT`, `START_MISSION`) | delete mission output / revise / start | Digest-bound confirmation; delete goes through `brief_missions.delete` |
| `POST /api/projects/upload`, `/delete` | project store | No |
| `POST /api/workspaces/create`, `DELETE` workspace/account (privacy) | tenant lifecycle | Ownership checked; privacy receipt |
| Connectors `connect` / `disconnect` | simulated in-memory state | Simulated; no external network write exists |

Findings:
- Only the mission report write goes through the IEEC guard. Document uploads, transforms, reports and deletions are direct
  user-initiated writes with tenant scoping but **no audit record and no common boundary**. This is addressed in task 022 below.
- No connector performs a real external write: `src/connectors.py` has no HTTP client. Email and calendar adapters do not exist.

## Work log
(entries are appended as each task is completed)

### Round 1 (this sprint)
- **Tasks 001–010 (baseline):** verified branch `claude/beautiful-bardeen-fw59qp`, HEAD = origin = `e398e9f`, clean tree,
  one worktree. Previous full-suite result was 1 failed / 402 passed / 12 skipped; the failure was fixed in `e398e9f`.
- **Task 007–008 (write-path map):** see the table above. Found that document uploads, transforms, reports, sample-brief creation
  and deletion were direct writes with no audit trail.
- **Task 022 (partial, central write boundary):** `write_boundary()` in `src/resolve_app.py` now wraps every direct document
  write (import, sample brief, report, transform, delete). Each attempt is recorded in `src/write_audit.py`
  (`ALLOWED` / `REJECTED` / `FAILED`, operation, target id, before/after sha256). Tests: `tests/test_write_boundary.py` (3 tests, real server).
  - Bug found by the tests and fixed: a cross-tenant attempt stored the foreign document id in the attacker's own audit,
    which leaked into their export. Now the foreign id is never persisted.
  - Bug found by the tests and fixed: the sample-brief route was an unaudited direct write.
  - Tenant deletion and account deletion now purge audit rows; export includes them (`GET /api/write-audit`, `POST /api/privacy/export`).
- **Task 031–039 (partial, Mission Assurance panel):** `#fmAssurance` in `web/index.html`, `renderAssurance()` in `web/static/flagship.js`.
  Reads only the recorded mission (`events`, `evidence`, `receipt.verification`, `plan`, `state`). Approval digest, verification
  checks, uncertainty reasons and timeline all come from the backend. Test: `tests/test_mission_assurance_e2e.py` (real Chromium;
  checks the displayed digest equals the server's recorded approval digest). Screenshot: `docs/screenshots/mission-assurance-confirmed.png`.

## Not done yet (honest)
- 023–030: no alternate route audit beyond the table above; tool-registry `document.*` writes still bypass the IEEC guard
  (they use idempotency keys only). Not fixed in this round.
- 040 responsive/accessibility review of the panel: only desktop was inspected. Phone and tablet not checked.
- 041–050 demo mode: not built.
- 051–060 browser and connector integrity: not audited in this round. Connectors are simulated; no real provider adapters exist.
- 061–070 security/privacy: partially covered by the write-boundary and export tests; the full audit has not been done.
- 071–080 benchmark rework: not done in this round; the existing local simulation is unchanged.
- Mutation check for the write boundary (disable recording → tests must fail): not yet run.
