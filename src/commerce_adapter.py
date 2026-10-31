"""Sandboxed commerce adapter for cart/order drafting and approval-gated purchase verification.

Policy: strictly sandbox-only; no external payment rails or real purchases.
Enforces Intent-Entity-Effect Continuity (IEEC): human approval digest is bound
to exact item, quantity, unit price, total amount, and recipient. Any post-draft
alteration invalidates the authorization.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
from pathlib import Path
from typing import Any

from resolve_core import Action, Ledger, digest, uid


class CommerceError(RuntimeError):
    """Commerce policy or approval constraint violation."""


DEFAULT_CATALOG = {
    "item_dev_keyboard": {"name": "Ergonomic Mechanical Keyboard", "price_cents": 8900, "in_stock": True},
    "item_usb_dongle": {"name": "USB-C Hardware Key", "price_cents": 4500, "in_stock": True},
    "item_stand_riser": {"name": "Aluminum Monitor Riser", "price_cents": 3400, "in_stock": True},
}


class CommerceSandbox:
    """Local SQLite-backed merchant store fixture."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path).resolve()
        self._init_db()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    action_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    total_cents INTEGER NOT NULL,
                    recipient TEXT NOT NULL,
                    receipt_hash TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def execute_order(self, order_id: str, action_id: str, item_id: str, quantity: int, total_cents: int, recipient: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT order_id FROM orders WHERE order_id = ?", (order_id,))
            if cur.fetchone():
                raise CommerceError(f"duplicate order {order_id}")

            receipt_payload = f"{order_id}:{action_id}:{item_id}:{quantity}:{total_cents}:{recipient}"
            receipt_hash = hashlib.sha256(receipt_payload.encode("utf-8")).hexdigest()
            created_at = time.time()
            conn.execute(
                "INSERT INTO orders (order_id, action_id, item_id, quantity, total_cents, recipient, receipt_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, action_id, item_id, quantity, total_cents, recipient, receipt_hash, created_at),
            )
            conn.commit()
        finally:
            conn.close()

        return {
            "order_id": order_id,
            "receipt_hash": receipt_hash,
            "total_cents": total_cents,
            "timestamp": created_at,
        }

    def verify_order(self, order_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
            row = cur.fetchone()
            if not row:
                return None
            return dict(row)
        finally:
            conn.close()


class CommerceAdapter:
    def __init__(self, ledger: Ledger, sandbox: CommerceSandbox, catalog: dict[str, dict] | None = None):
        self.ledger = ledger
        self.sandbox = sandbox
        self.catalog = catalog or DEFAULT_CATALOG

    def draft_order(
        self,
        mission_id: str,
        item_id: str,
        quantity: int,
        recipient: str,
        revision: int = 1,
    ) -> dict[str, Any]:
        """Draft a proposed purchase order requiring human approval."""
        if item_id not in self.catalog:
            raise CommerceError(f"item {item_id} not available in catalog")
        if quantity <= 0:
            raise CommerceError(f"quantity must be positive; got {quantity}")

        item = self.catalog[item_id]
        total_cents = item["price_cents"] * quantity
        order_id = uid("ord")
        action_id = uid("act_commerce")

        arguments = {
            "order_id": order_id,
            "item_id": item_id,
            "item_name": item["name"],
            "unit_price_cents": item["price_cents"],
            "quantity": quantity,
            "total_cents": total_cents,
            "recipient": recipient,
        }
        approval_contract = {
            "order_id": order_id,
            "item_id": item_id,
            "quantity": quantity,
            "total_cents": total_cents,
            "recipient": recipient,
        }
        approval_digest = digest(approval_contract)

        action = Action(
            action_id,
            mission_id,
            revision,
            "commerce_order_draft",
            f"order:{order_id}",
            "CommerceAgent",
            "CommerceSandbox",
            None,
            arguments,
            None,
            {"order_placed": True, "total_cents": total_cents, "recipient": recipient},
            "NON_IDEMPOTENT",
            "consequential",
        )

        self.ledger.create_action(action)
        self.ledger.transition(action_id, "BOUND")
        self.ledger.transition(action_id, "NEEDS_APPROVAL", {"approval_digest": approval_digest})

        return {
            "action_id": action_id,
            "order_id": order_id,
            "item": item["name"],
            "quantity": quantity,
            "total_cents": total_cents,
            "recipient": recipient,
            "approval_digest": approval_digest,
            "status": "NEEDS_APPROVAL",
        }

    def execute_approved_order(self, action_id: str, provided_digest: str) -> dict[str, Any]:
        """Execute order if and only if the provided digest matches the drafted order."""
        action = self.ledger.action(action_id)
        if not action:
            raise CommerceError(f"action {action_id} not found")
        if action["status"] != "NEEDS_APPROVAL":
            raise CommerceError(f"action {action_id} is in status {action['status']}, expected NEEDS_APPROVAL")

        args = action["arguments"]
        contract = {
            "order_id": args["order_id"],
            "item_id": args["item_id"],
            "quantity": args["quantity"],
            "total_cents": args["total_cents"],
            "recipient": args["recipient"],
        }
        expected_digest = digest(contract)
        if provided_digest != expected_digest:
            raise CommerceError("approval digest mismatch: order details do not match authorization")

        self.ledger.transition(action_id, "AUTHORIZED", {"approval_digest": provided_digest})
        self.ledger.transition(action_id, "READY")
        self.ledger.transition(action_id, "DISPATCHING")

        try:
            res = self.sandbox.execute_order(
                order_id=args["order_id"],
                action_id=action_id,
                item_id=args["item_id"],
                quantity=args["quantity"],
                total_cents=args["total_cents"],
                recipient=args["recipient"],
            )
            self.ledger.transition(action_id, "DISPATCHED", {"receipt": res})
        except Exception as exc:
            self.ledger.transition(action_id, "FAILED", {"error": str(exc)})
            raise

        self.ledger.transition(action_id, "OBSERVING")

        # Independent verification against the sandbox database
        verified = self.sandbox.verify_order(args["order_id"])
        if (
            verified
            and verified["receipt_hash"] == res["receipt_hash"]
            and verified["total_cents"] == args["total_cents"]
            and verified["recipient"] == args["recipient"]
        ):
            final_status = "CONFIRMED"
        else:
            final_status = "FAILED"

        self.ledger.transition(
            action_id,
            final_status,
            {"verification": verified, "verification_status": final_status},
        )
        return {
            "action_id": action_id,
            "status": final_status,
            "order_id": args["order_id"],
            "receipt_hash": res["receipt_hash"],
        }
