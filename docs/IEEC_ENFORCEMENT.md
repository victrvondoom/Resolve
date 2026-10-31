# IEEC execution contract: what is enforced, what is tested, what is not

## Where the contract is enforced
- `src/operation_contract.py`: `Contract` (what the user approved), `Observed` (the world read immediately before dispatch),
  `guard()` (pure policy decision with fixed-order rejection codes), `EffectLedger` (durable per-idempotency-key dispatch
  record, same SQLite database as missions), `execute()` (the only path that dispatches).
- `src/brief_mission.py::_guarded_report_write`: the mission's one consequential write (saving the generated PDF) is
  dispatched only through `execute()`. The approval is re-checked against the source document version, the mission
  revision, the normalized arguments, the approved plan digest and the mission's active state at the moment of writing.

## Outcome rules (tested)
- A rejected action never reaches the provider (`REJECTED`, `dispatched=False`).
- A timeout after a possible effect is reconciled with a read-only check: effect present → `VERIFIED`;
  effect absent → `RECOVERY_REQUIRED` (new approval needed); unknown → `UNCERTAIN`. No automatic retry.
- Any executor error after dispatch is `UNCERTAIN`, not failure, because the effect may have happened.
- `VERIFIED` requires an independent read-back that matches the contract. A verifier that raises is not success.
- A key already dispatched is answered from the record and is never dispatched again.

## Legal state transitions
`TRANSITIONS` in `src/operation_contract.py` defines them; illegal transitions raise `IllegalTransition`
(tested in `tests/test_ieec_adversarial.py::StateMachine`).

## Adversarial evidence
- `src/ieec_harness.py`: 21 deterministic scenarios (the listed cases, mapped to what this system actually does).
- `tests/test_ieec_adversarial.py`: 21 tests, including per-scenario outcomes, exact rejection reasons, mutation checks
  (guard, target/owner checks, ledger, read-back each disabled in turn and the relevant scenario must fail), a real
  two-thread race on the durable ledger, and a stale-revision write through the real mission store and `DocumentStudio`.

## Known limitations (stated, not hidden)
- The provider is a local fake. Real provider idempotency, latency, and read-back semantics are NOT verified.
- A provider that writes to a different record than requested is detected (UNCERTAIN) but not prevented.
- Reconciliation after timeout is only as strong as the provider's read-back.
- Email, calendar and file-deletion adapters do not exist in this codebase; they are not claimed.
- Mission Assurance UI (live trust panel and in-app demo mode) is NOT built in this change.

## Reproduce
```bash
python -m pytest tests/test_ieec_adversarial.py -q
python benchmarks/ieec_bench.py          # writes benchmarks/results/ieec_latest.json
```
