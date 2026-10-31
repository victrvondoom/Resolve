"""ProofPack construction and validation.

Invariants enforced in code (and covered by tests):
  * The gateway can only emit VERIFIED when a RESOLVE backend receipt with a post-action observation id
    is supplied. A model or MCP client cannot pass a boolean or a free-text claim to change the status.
  * Every pack is validated against ``schemas/proofpack.schema.json`` before it leaves the gateway.
  * Free-text is never placed in a pack: only digests, ids, enum values and fixed note codes.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources
from typing import Any

import jsonschema

from . import __version__
from .errors import BlockedError

STATES = ("PROPOSED", "AWAITING_APPROVAL", "AUTHORIZED", "EXECUTING", "VERIFIED", "FAILED", "UNCERTAIN", "BLOCKED", "CANCELLED", "STALE")


def sha256_hex(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_digest(payload: Any) -> str:
    """Digest of a canonical JSON encoding. Used for intent digests bound to the exact proposal."""
    return sha256_hex(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class BackendReceipt:
    """Evidence returned by RESOLVE itself for an executed action. Only the gateway's execution path constructs
    these, from RESOLVE responses, never from client arguments."""

    action_id: str
    started_at: str
    finished_at: str
    verification_status: str  # VERIFIED / FAILED / UNCERTAIN, as reported by RESOLVE
    post_observation_id: str | None
    evidence: tuple[str, ...] = ()


@dataclass
class ProofPackBuilder:
    task_digest: str
    task_length: int
    environment: str = "UNSPECIFIED"
    operation_id: str = field(default_factory=lambda: "op_" + secrets.token_hex(12))

    def build(
        self,
        *,
        status: str,
        intent_digest: str,
        intent_source: str = "external_agent_request",
        user_approved: bool = False,
        observation: dict[str, Any] | None = None,
        target: dict[str, Any] | None = None,
        authorization_decision: str = "NOT_EVALUATED",
        approval_id: str | None = None,
        authorization_checked_via: str = "NONE",
        method: str = "NONE",
        execution_route: str = "NOT_REQUESTED",
        receipt: BackendReceipt | None = None,
        notes: tuple[str, ...] = (),
        evidence: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if status not in STATES:
            raise BlockedError(f"unknown state {status!r}")
        if status == "VERIFIED" and (receipt is None or receipt.verification_status != "VERIFIED" or not receipt.post_observation_id):
            raise BlockedError("VERIFIED requires a RESOLVE backend receipt with a post-action observation", code="VERIFICATION_NOT_GROUNDED")
        if receipt is not None and status not in {"VERIFIED", "FAILED", "UNCERTAIN"}:
            raise BlockedError("a backend receipt may only accompany an executed outcome")
        exec_block = {
            "action_id": receipt.action_id if receipt else None,
            "method": method,
            "route": "RESOLVE_BACKEND" if receipt else execution_route,
            "started_at": receipt.started_at if receipt else None,
            "finished_at": receipt.finished_at if receipt else None,
        }
        verify_block = {
            "status": receipt.verification_status if receipt else "NOT_RUN",
            "method": "independent_observation" if receipt and receipt.post_observation_id else "none",
            "post_observation_id": receipt.post_observation_id if receipt else None,
        }
        all_notes = list(notes)
        if receipt is not None:
            all_notes.append("BACKEND_RECEIPT_ATTACHED")
        evidence_refs = list(evidence) + (list(receipt.evidence) if receipt else [])
        pack = {
            "schema_version": "resolve-proofpack/1",
            "operation_id": self.operation_id,
            "environment": self.environment,
            "status": status,
            "request": {"source": "external_mcp_client", "task_digest": self.task_digest, "task_length": self.task_length},
            "intent": {"digest": intent_digest, "source": intent_source, "user_approved": bool(user_approved)},
            "observation": observation,
            "target": target,
            "authorization": {"decision": authorization_decision, "approval_id": approval_id, "checked_via": authorization_checked_via},
            "execution": exec_block,
            "verification": verify_block,
            "evidence": evidence_refs[:32],
            "notes": sorted(set(all_notes)),
            "generated_by": f"resolve-mcp/{__version__}",
            "generated_at": utc_now(),
        }
        return validate_proofpack(pack)


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    text = resources.files("resolve_mcp").joinpath("schemas/proofpack.schema.json").read_text(encoding="utf-8")
    return json.loads(text)


def validate_proofpack(pack: dict[str, Any]) -> dict[str, Any]:
    """Raise BlockedError if the pack does not satisfy the published schema. Returns the pack unchanged."""
    try:
        jsonschema.Draft202012Validator(_schema()).validate(pack)
    except jsonschema.ValidationError as exc:
        raise BlockedError("ProofPack failed schema validation: " + exc.message[:200], code="PROOFPACK_INVALID") from None
    return pack
