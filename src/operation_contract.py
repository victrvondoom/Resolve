"""IEEC execution contract, operation state machine and backend guard.

A consequential operation is only dispatched when the backend re-checks, immediately before the effect, that the
approved contract still matches the world: same user/workspace, same mission revision, same target version, same
normalized arguments, an unexpired and matching approval, and the preconditions still hold. Agents never decide this.

Outcome rules (the core invariant):
- A timeout or crash after dispatch is UNCERTAIN, never success and never an automatic retry.
- An idempotency key that already reached EXECUTING/UNCERTAIN is never dispatched again.
- VERIFIED requires an independent read-back that matches the contract, not the executor's own return value.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


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


def transition(current: OpState, nxt: OpState) -> OpState:
    if nxt not in TRANSITIONS.get(current, frozenset()):
        raise IllegalTransition(f"{current.value} -> {nxt.value} is not allowed")
    return nxt


def canonical_digest(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass(frozen=True)
class Contract:
    """Everything the approval binds. Any change to these fields after approval invalidates the approval."""
    user_id: str
    workspace_id: str
    mission_id: str
    revision: int
    intent: str
    operation: str                     # e.g. "document.write"
    target_id: str                     # stable id, never a display name
    target_label: str                  # human-readable, for display only
    target_version: str                # sha256 of the target as it was approved
    args: dict                         # normalized arguments
    approval_digest: str               # digest of the plan the user approved
    idempotency_key: str
    preconditions: dict = field(default_factory=dict)
    expires_at: float = 0.0
    allowed_operations: frozenset = frozenset()

    def args_digest(self) -> str:
        return canonical_digest(self.args)


@dataclass(frozen=True)
class Observed:
    """The world, read by the backend immediately before dispatch."""
    user_id: str
    workspace_id: str
    mission_revision: int
    mission_active: bool
    target_candidates: int             # >1 means the target is ambiguous
    presented_target_id: str           # the target the executor is about to act on
    target_version: str | None         # None = target missing
    target_owner_id: str | None
    presented_approval_digest: str
    presented_args: dict
    preconditions: dict
    now: float


REJECT_CODES = ("NOT_AUTHORIZED_OPERATION", "NOT_OWNER", "MISSION_INACTIVE", "REVISION_CHANGED", "TARGET_AMBIGUOUS", "WRONG_TARGET",
                "TARGET_MISSING", "TARGET_CHANGED", "APPROVAL_EXPIRED", "APPROVAL_MISMATCH", "ARGS_MISMATCH", "PRECONDITION_FAILED")


def guard(c: Contract, o: Observed) -> tuple[bool, str]:
    """Pure policy decision. Returns (allowed, reason). Checks run in a fixed order; first failure wins."""
    if c.allowed_operations and c.operation not in c.allowed_operations:
        return False, "NOT_AUTHORIZED_OPERATION"
    if o.user_id != c.user_id or o.workspace_id != c.workspace_id:
        return False, "NOT_OWNER"
    if not o.mission_active:
        return False, "MISSION_INACTIVE"
    if o.mission_revision != c.revision:
        return False, "REVISION_CHANGED"
    if o.target_candidates > 1:
        return False, "TARGET_AMBIGUOUS"
    if o.presented_target_id != c.target_id:
        return False, "WRONG_TARGET"
    if o.target_version is None:
        return False, "TARGET_MISSING"
    if o.target_owner_id not in (None, c.user_id):
        return False, "NOT_OWNER"
    if o.target_version != c.target_version:
        return False, "TARGET_CHANGED"
    if o.now > c.expires_at:
        return False, "APPROVAL_EXPIRED"
    if o.presented_approval_digest != c.approval_digest:
        return False, "APPROVAL_MISMATCH"
    if canonical_digest(o.presented_args) != c.args_digest():
        return False, "ARGS_MISMATCH"
    for k, expected in c.preconditions.items():
        if o.preconditions.get(k) != expected:
            return False, "PRECONDITION_FAILED"
    return True, "AUTHORIZED"


class EffectTimeout(Exception):
    """The external call may or may not have taken effect."""


class EffectLedger:
    """Durable per-idempotency-key record of dispatch. A key that reached EXECUTING is never dispatched again."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS operation_effects (
                idempotency_key TEXT PRIMARY KEY, contract_digest TEXT NOT NULL, state TEXT NOT NULL,
                reason TEXT, updated_at REAL NOT NULL)""")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, key: str, contract_digest: str) -> tuple[bool, str | None]:
        """Atomically claim the right to dispatch. Returns (claimed, existing_state)."""
        with self._lock, self._db() as db:
            row = db.execute("SELECT state FROM operation_effects WHERE idempotency_key=?", (key,)).fetchone()
            if row is not None:
                return False, row[0]
            db.execute("INSERT INTO operation_effects VALUES (?,?,?,?,?)", (key, contract_digest, OpState.EXECUTING.value, None, time.time()))
            return True, None

    def set_state(self, key: str, state: OpState, reason: str | None = None) -> None:
        with self._db() as db:
            db.execute("UPDATE operation_effects SET state=?, reason=?, updated_at=? WHERE idempotency_key=?", (state.value, reason, time.time(), key))

    def state_of(self, key: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT state FROM operation_effects WHERE idempotency_key=?", (key,)).fetchone()
        return row[0] if row else None


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
    cdig = canonical_digest({"approval": c.approval_digest, "target": c.target_id, "version": c.target_version,
                             "args": c.args_digest(), "rev": c.revision, "op": c.operation})
    # A key that was already dispatched is a replay, not a new decision: answer from the record, never dispatch again.
    prior = ledger.state_of(c.idempotency_key)
    if prior is not None:
        if prior == OpState.VERIFIED.value:
            return Outcome(OpState.VERIFIED, "DUPLICATE_SUPPRESSED_ALREADY_VERIFIED", effect_occurred=True, dispatched=False)
        return Outcome(OpState.UNCERTAIN, f"DUPLICATE_SUPPRESSED_PRIOR_{prior.upper()}", effect_occurred=None, dispatched=False)
    ok, reason = guard(c, observe())
    if not ok:
        return Outcome(OpState.REJECTED, reason, effect_occurred=False, dispatched=False)
    claimed, existing = ledger.claim(c.idempotency_key, cdig)
    if not claimed:   # another worker claimed the key between the check and the claim
        if existing == OpState.VERIFIED.value:
            return Outcome(OpState.VERIFIED, "DUPLICATE_SUPPRESSED_ALREADY_VERIFIED", effect_occurred=True, dispatched=False)
        return Outcome(OpState.UNCERTAIN, f"DUPLICATE_SUPPRESSED_PRIOR_{existing.upper()}", effect_occurred=None, dispatched=False)
    try:
        result = effect()
    except EffectTimeout:
        # The effect may have happened. Do not retry; reconcile read-only if possible.
        truth = reconcile() if reconcile else None
        if truth is True:
            ledger.set_state(c.idempotency_key, OpState.VERIFIED, "RECONCILED_EFFECT_PRESENT")
            return Outcome(OpState.VERIFIED, "RECONCILED_AFTER_TIMEOUT", effect_occurred=True, dispatched=True)
        if truth is False:
            ledger.set_state(c.idempotency_key, OpState.RECOVERY_REQUIRED, "RECONCILED_ABSENT_NEW_APPROVAL_REQUIRED")
            return Outcome(OpState.RECOVERY_REQUIRED, "TIMEOUT_EFFECT_ABSENT", effect_occurred=False, dispatched=True)
        ledger.set_state(c.idempotency_key, OpState.UNCERTAIN, "TIMEOUT_UNKNOWN_OUTCOME")
        return Outcome(OpState.UNCERTAIN, "TIMEOUT_UNKNOWN_OUTCOME", effect_occurred=None, dispatched=True)
    except Exception as exc:  # the executor failed; whether it changed anything is unknown unless verified below
        ledger.set_state(c.idempotency_key, OpState.UNCERTAIN, f"EXECUTOR_ERROR:{type(exc).__name__}")
        return Outcome(OpState.UNCERTAIN, f"EXECUTOR_ERROR:{type(exc).__name__}", effect_occurred=None, dispatched=True)
    try:
        matched = bool(verify(result))
        why = "INDEPENDENT_READBACK_MATCHED" if matched else "READBACK_DID_NOT_MATCH"
    except Exception as exc:   # verifier unavailable or the claimed resource does not exist: not a success
        matched, why = False, f"READBACK_UNAVAILABLE:{type(exc).__name__}"
    if matched:
        ledger.set_state(c.idempotency_key, OpState.VERIFIED, why)
        return Outcome(OpState.VERIFIED, why, effect_occurred=True, dispatched=True, result=result)
    ledger.set_state(c.idempotency_key, OpState.UNCERTAIN, why)
    return Outcome(OpState.UNCERTAIN, why, effect_occurred=None, dispatched=True, result=result)
