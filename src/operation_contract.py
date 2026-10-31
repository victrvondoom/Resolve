"""IEEC execution contract, operation state machine, backend guard and tenant-scoped dispatch record.

A consequential operation is dispatched only when the backend re-checks, immediately before the effect, that the
approved contract still matches the world: same user/workspace, same mission revision, same target version, same
normalized arguments, an unexpired and matching approval, and the preconditions still hold. Agents never decide this.

Invariants:
- A contract is validated and frozen at construction. An empty operation allowlist, a missing identity, or a
  non-finite expiry is a construction error, never a silent authorization.
- Canonical digests are strict: a non-JSON value raises instead of being stringified.
- The dispatch record is scoped by (user, workspace, idempotency key). The same key from another tenant is a different
  operation. The same key with a different contract is IDEMPOTENCY_CONFLICT, never a replay of someone else's result.
- A timeout or crash after dispatch is UNCERTAIN, never success and never an automatic retry.
- VERIFIED requires an independent read-back that matches the contract, not the executor's own return value.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType


class OpState(str, Enum):
    PROPOSED = "proposed"
    AUTHORIZED = "authorized"
    EXECUTING = "executing"
    EFFECT_OBSERVED = "effect_observed"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    UNCERTAIN = "uncertain"
    RECOVERY_REQUIRED = "recovery_required"


TERMINAL = {OpState.VERIFIED, OpState.REJECTED, OpState.FAILED, OpState.CANCELLED, OpState.EXPIRED}
TRANSITIONS: dict[OpState, frozenset[OpState]] = {
    OpState.PROPOSED: frozenset({OpState.AUTHORIZED, OpState.REJECTED, OpState.CANCELLED, OpState.EXPIRED}),
    OpState.AUTHORIZED: frozenset({OpState.EXECUTING, OpState.REJECTED, OpState.CANCELLED, OpState.EXPIRED}),
    OpState.EXECUTING: frozenset({OpState.EFFECT_OBSERVED, OpState.FAILED, OpState.UNCERTAIN, OpState.CANCELLED}),
    OpState.EFFECT_OBSERVED: frozenset({OpState.VERIFYING}),
    OpState.VERIFYING: frozenset({OpState.VERIFIED, OpState.FAILED, OpState.UNCERTAIN}),
    OpState.UNCERTAIN: frozenset({OpState.VERIFIED, OpState.RECOVERY_REQUIRED, OpState.FAILED}),
    OpState.RECOVERY_REQUIRED: frozenset({OpState.FAILED, OpState.CANCELLED}),   # a new approval starts a new contract
}


class IllegalTransition(Exception):
    pass


class IdempotencyConflict(Exception):
    """The same (user, workspace, key) was already used for a different contract."""


class StaleRecord(Exception):
    """A conditional state update found a different current state or contract."""


def transition(current: OpState, nxt: OpState) -> OpState:
    if nxt not in TRANSITIONS.get(current, frozenset()):
        raise IllegalTransition(f"{current.value} -> {nxt.value} is not allowed")
    return nxt


def _refuse_non_canonical(value):
    raise TypeError(f"non-canonical value in digest: {type(value).__name__}")


def canonical_digest(obj) -> str:
    """Strict canonical JSON: sorted keys, no NaN/Infinity, and any unsupported type is an error (never str())."""
    text = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=_refuse_non_canonical, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _plain(value):
    """Deep, JSON-only copy: detaches mutable caller data from the frozen contract."""
    return json.loads(json.dumps(value, sort_keys=True, default=_refuse_non_canonical, allow_nan=False))


@dataclass(frozen=True)
class Contract:
    """Everything the approval binds. Any change to these fields after approval invalidates the approval.
    Mutable inputs are copied and exposed read-only, so a caller cannot change an approved contract in place."""
    user_id: str
    workspace_id: str
    mission_id: str
    revision: int
    intent: str
    operation: str                     # e.g. "document.write"
    target_id: str                     # stable id, never a display name
    target_label: str                  # human-readable, for display only; not part of the digest
    target_version: str                # sha256 of the target as it was approved
    args: dict                         # normalized arguments
    approval_digest: str               # digest of the plan the user approved
    idempotency_key: str
    preconditions: dict = field(default_factory=dict)
    expires_at: float = 0.0
    allowed_operations: frozenset = frozenset()

    def __post_init__(self):
        for name in ("user_id", "workspace_id", "mission_id", "operation", "target_id", "approval_digest", "idempotency_key"):
            v = getattr(self, name)
            if not isinstance(v, str) or not v.strip():
                raise ValueError(f"contract field {name} is required")
        if not isinstance(self.revision, int) or isinstance(self.revision, bool) or self.revision < 1:
            raise ValueError("contract revision must be a positive integer")
        if not isinstance(self.allowed_operations, (set, frozenset)) or not self.allowed_operations:
            raise ValueError("an empty operation allowlist is refused: the contract must name the permitted operations")
        if not all(isinstance(x, str) and x for x in self.allowed_operations):
            raise ValueError("allowed operations must be non-empty strings")
        if not isinstance(self.expires_at, (int, float)) or isinstance(self.expires_at, bool) or not math.isfinite(self.expires_at) or self.expires_at <= 0:
            raise ValueError("approval expiry must be a finite positive timestamp")
        if not isinstance(self.args, dict) or not isinstance(self.preconditions, dict):
            raise ValueError("args and preconditions must be objects")
        object.__setattr__(self, "args", MappingProxyType(_plain(self.args)))
        object.__setattr__(self, "preconditions", MappingProxyType(_plain(self.preconditions)))
        object.__setattr__(self, "allowed_operations", frozenset(self.allowed_operations))

    def args_digest(self) -> str:
        return canonical_digest(dict(self.args))

    def digest(self) -> str:
        """Identity of the whole approved contract (everything except the display label)."""
        return canonical_digest({
            "user": self.user_id, "workspace": self.workspace_id, "mission": self.mission_id, "revision": self.revision,
            "intent": self.intent, "operation": self.operation, "target": self.target_id, "target_version": self.target_version,
            "args": dict(self.args), "approval": self.approval_digest, "key": self.idempotency_key,
            "preconditions": dict(self.preconditions), "expires_at": self.expires_at,
            "allowed_operations": sorted(self.allowed_operations),
        })


@dataclass(frozen=True)
class Observed:
    """The world, read by the backend immediately before dispatch."""
    user_id: str
    workspace_id: str
    mission_revision: int
    mission_active: bool
    target_candidates: int             # exactly 1 is required; 0 = missing, >1 = ambiguous
    presented_target_id: str           # the target the executor is about to act on
    target_version: str | None         # None = target missing
    target_owner_id: str | None        # must equal the approving user
    presented_approval_digest: str
    presented_args: dict
    preconditions: dict
    now: float


REJECT_CODES = ("NOT_AUTHORIZED_OPERATION", "NOT_OWNER", "MISSION_INACTIVE", "REVISION_CHANGED", "TARGET_MISSING", "TARGET_AMBIGUOUS",
                "WRONG_TARGET", "TARGET_CHANGED", "APPROVAL_EXPIRED", "APPROVAL_MISMATCH", "ARGS_MISMATCH", "PRECONDITION_FAILED",
                "IDEMPOTENCY_CONFLICT")


def guard(c: Contract, o: Observed) -> tuple[bool, str]:
    """Pure policy decision. Returns (allowed, reason). Checks run in a fixed order; first failure wins."""
    if not c.allowed_operations or c.operation not in c.allowed_operations:
        return False, "NOT_AUTHORIZED_OPERATION"
    if o.user_id != c.user_id or o.workspace_id != c.workspace_id:
        return False, "NOT_OWNER"
    if not o.mission_active:
        return False, "MISSION_INACTIVE"
    if o.mission_revision != c.revision:
        return False, "REVISION_CHANGED"
    if o.target_candidates == 0:
        return False, "TARGET_MISSING"
    if o.target_candidates > 1:
        return False, "TARGET_AMBIGUOUS"
    if o.presented_target_id != c.target_id:
        return False, "WRONG_TARGET"
    if o.target_version is None:
        return False, "TARGET_MISSING"
    if o.target_owner_id != c.user_id:              # None is NOT an owner: ownership must be positively verified
        return False, "NOT_OWNER"
    if o.target_version != c.target_version:
        return False, "TARGET_CHANGED"
    if not math.isfinite(o.now) or o.now > c.expires_at:
        return False, "APPROVAL_EXPIRED"
    if o.presented_approval_digest != c.approval_digest:
        return False, "APPROVAL_MISMATCH"
    try:
        presented = canonical_digest(_plain(o.presented_args))
    except (TypeError, ValueError):
        return False, "ARGS_MISMATCH"
    if presented != c.args_digest():
        return False, "ARGS_MISMATCH"
    for k, expected in c.preconditions.items():
        if o.preconditions.get(k) != expected:
            return False, "PRECONDITION_FAILED"
    return True, "AUTHORIZED"


class EffectTimeout(Exception):
    """The external call may or may not have taken effect."""


class EffectLedger:
    """Durable dispatch record, scoped by (user, workspace, idempotency key). Shared across processes via SQLite:
    claims use BEGIN IMMEDIATE so two processes cannot both insert the same key."""

    TABLE = "operation_effects_v2"

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute(f"""CREATE TABLE IF NOT EXISTS {self.TABLE} (
                user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                contract_digest TEXT NOT NULL, state TEXT NOT NULL, reason TEXT, updated_at REAL NOT NULL,
                PRIMARY KEY (user_id, workspace_id, idempotency_key))""")

    @contextmanager
    def _db(self, immediate: bool = False):
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    def claim(self, user_id: str, workspace_id: str, key: str, contract_digest: str) -> tuple[bool, str | None, str | None]:
        """Atomically claim the dispatch right. Returns (claimed, existing_state, existing_digest)."""
        with self._lock, self._db(immediate=True) as db:
            row = db.execute(f"SELECT state, contract_digest FROM {self.TABLE} WHERE user_id=? AND workspace_id=? AND idempotency_key=?",
                             (user_id, workspace_id, key)).fetchone()
            if row is not None:
                return False, row[0], row[1]
            db.execute(f"INSERT INTO {self.TABLE} VALUES (?,?,?,?,?,?,?)",
                       (user_id, workspace_id, key, contract_digest, OpState.EXECUTING.value, None, time.time()))
            return True, None, None

    def get(self, user_id: str, workspace_id: str, key: str) -> tuple[str, str] | None:
        with self._db() as db:
            row = db.execute(f"SELECT state, contract_digest FROM {self.TABLE} WHERE user_id=? AND workspace_id=? AND idempotency_key=?",
                             (user_id, workspace_id, key)).fetchone()
        return (row[0], row[1]) if row else None

    def set_state(self, user_id: str, workspace_id: str, key: str, contract_digest: str, new: OpState, reason: str | None,
                  expected: OpState = OpState.EXECUTING) -> None:
        """Conditional update: only the same contract, in the expected current state, may move the record."""
        transition(expected, new)
        with self._db(immediate=True) as db:
            cur = db.execute(f"UPDATE {self.TABLE} SET state=?, reason=?, updated_at=? WHERE user_id=? AND workspace_id=? AND idempotency_key=? "
                             f"AND contract_digest=? AND state=?",
                             (new.value, reason, time.time(), user_id, workspace_id, key, contract_digest, expected.value))
            if cur.rowcount != 1:
                raise StaleRecord(f"record for key {key} is not in state {expected.value} for this contract")

    def state_of(self, user_id: str, workspace_id: str, key: str) -> str | None:
        got = self.get(user_id, workspace_id, key)
        return got[0] if got else None


@dataclass
class Outcome:
    state: OpState
    reason: str
    effect_occurred: bool | None       # None = unknown
    dispatched: bool
    result: object = None


def execute(c: Contract, observe, effect, verify, ledger: EffectLedger, reconcile=None) -> Outcome:
    """Run one consequential operation through the contract.

    observe()  -> Observed, read at the moment of dispatch (not from the approval time)
    effect()   -> the external call; may raise EffectTimeout (outcome unknown)
    verify(r)  -> True only if an INDEPENDENT read-back proves the intended state for this contract
    reconcile()-> True (effect present), False (proven absent), None (cannot tell); used only after a timeout
    """
    cdig = c.digest()
    prior = ledger.get(c.user_id, c.workspace_id, c.idempotency_key)
    if prior is not None:
        return _replay(prior, cdig)

    ok, reason = guard(c, observe())
    if not ok:
        return Outcome(OpState.REJECTED, reason, effect_occurred=False, dispatched=False)
    claimed, state, digest = ledger.claim(c.user_id, c.workspace_id, c.idempotency_key, cdig)
    if not claimed:   # another worker claimed the key between the check and the claim
        return _replay((state, digest), cdig)

    try:
        result = effect()
    except EffectTimeout:
        # The effect may have happened. Do not retry; reconcile read-only if possible.
        try:
            truth = reconcile() if reconcile else None
        except Exception:          # a failing reconciliation is an unknown outcome, never a stuck EXECUTING record
            truth = None
        if truth is True:
            return _settle(ledger, c, cdig, [OpState.UNCERTAIN, OpState.VERIFIED], "RECONCILED_EFFECT_PRESENT",
                           Outcome(OpState.VERIFIED, "RECONCILED_AFTER_TIMEOUT", effect_occurred=True, dispatched=True))
        if truth is False:
            return _settle(ledger, c, cdig, [OpState.UNCERTAIN, OpState.RECOVERY_REQUIRED], "RECONCILED_ABSENT_NEW_APPROVAL_REQUIRED",
                           Outcome(OpState.RECOVERY_REQUIRED, "TIMEOUT_EFFECT_ABSENT", effect_occurred=False, dispatched=True))
        return _settle(ledger, c, cdig, [OpState.UNCERTAIN], "TIMEOUT_UNKNOWN_OUTCOME",
                       Outcome(OpState.UNCERTAIN, "TIMEOUT_UNKNOWN_OUTCOME", effect_occurred=None, dispatched=True))
    except Exception as exc:  # the executor failed; whether it changed anything is unknown unless verified below
        return _settle(ledger, c, cdig, [OpState.UNCERTAIN], f"EXECUTOR_ERROR:{type(exc).__name__}",
                       Outcome(OpState.UNCERTAIN, f"EXECUTOR_ERROR:{type(exc).__name__}", effect_occurred=None, dispatched=True))
    try:
        matched = bool(verify(result))
        why = "INDEPENDENT_READBACK_MATCHED" if matched else "READBACK_DID_NOT_MATCH"
    except Exception as exc:   # verifier unavailable or the claimed resource does not exist: not a success
        matched, why = False, f"READBACK_UNAVAILABLE:{type(exc).__name__}"
    if matched:
        return _settle(ledger, c, cdig, [OpState.EFFECT_OBSERVED, OpState.VERIFYING, OpState.VERIFIED], why,
                       Outcome(OpState.VERIFIED, why, effect_occurred=True, dispatched=True, result=result))
    return _settle(ledger, c, cdig, [OpState.EFFECT_OBSERVED, OpState.VERIFYING, OpState.UNCERTAIN], why,
                   Outcome(OpState.UNCERTAIN, why, effect_occurred=None, dispatched=True, result=result))


def _advance(ledger: EffectLedger, c: Contract, cdig: str, path: list, reason: str) -> None:
    """Persist a chain of states from EXECUTING. Each step must be a legal transition and is a conditional write."""
    prev = OpState.EXECUTING
    for nxt in path:
        transition(prev, nxt)
        ledger.set_state(c.user_id, c.workspace_id, c.idempotency_key, cdig, nxt, reason, expected=prev)
        prev = nxt


def _settle(ledger: EffectLedger, c: Contract, cdig: str, path: list, reason: str, outcome: Outcome) -> Outcome:
    """Persist the outcome. If the record is no longer the one this worker claimed, the effect may still have happened:
    report UNCERTAIN with the conflict, never the worker's own success claim."""
    try:
        _advance(ledger, c, cdig, path, reason)
    except StaleRecord:
        return Outcome(OpState.UNCERTAIN, "LEDGER_STATE_CONFLICT", effect_occurred=None, dispatched=True, result=outcome.result)
    return outcome


def _replay(prior, cdig: str) -> Outcome:
    state, digest = prior
    if digest != cdig:
        return Outcome(OpState.REJECTED, "IDEMPOTENCY_CONFLICT", effect_occurred=None, dispatched=False)
    if state == OpState.VERIFIED.value:
        return Outcome(OpState.VERIFIED, "DUPLICATE_SUPPRESSED_ALREADY_VERIFIED", effect_occurred=True, dispatched=False)
    return Outcome(OpState.UNCERTAIN, f"DUPLICATE_SUPPRESSED_PRIOR_{str(state).upper()}", effect_occurred=None, dispatched=False)
