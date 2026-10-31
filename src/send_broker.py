"""Guarded execution broker for email writes (Gmail draft creation and sending).

Rules enforced here, not by the model:
- An operation runs only with an approval that is unexpired, unused, for this operation, and bound to the draft digest.
- The operation key is derived from the approval; it is claimed durably BEFORE any provider call.
- A key that already exists is never dispatched again. Its state is returned, or reconciled read-only.
- A send is verified by reading the stored message back (recipient, subject, body marker). A timeout is UNCERTAIN.
- Before a send is retried after an unknown outcome, Sent is searched for the operation marker. If it is found the
  send is VERIFIED; if not, the record becomes NEEDS_RECOVERY and a new approval is required. It is never re-sent silently.
- Nothing here trusts a model's statement that an operation succeeded.
"""
from __future__ import annotations

import hashlib

from gmail_adapter import GmailError, build_raw_message, operation_marker


class BrokerRefused(Exception):
    pass


def operation_key(operation: str, digest: str) -> str:
    """Derived from the CONTENT and the operation, not from the approval. Approving identical content again after a
    timeout maps to the same record, so it can be reconciled against Sent instead of being sent twice."""
    return hashlib.sha256(f"{operation}|{digest}".encode()).hexdigest()[:32]


class SendBroker:
    def __init__(self, store, client, now):
        self.store = store
        self.client = client          # GmailSendClient (or a test double with the same methods)
        self.now = now                # callable -> float; injected so expiry tests are deterministic

    # ---------- draft creation (read back the stored draft before reporting success)
    def create_gmail_draft(self, user_id: str, workspace_id: str, approval_id: str, draft_id: str, digest: str) -> dict:
        bound = self.store.consume_approval(user_id, workspace_id, approval_id, "CREATE_GMAIL_DRAFT", digest, self.now())
        draft = self.store.get_draft(user_id, workspace_id, bound["draft_id"])
        key = operation_key("CREATE_GMAIL_DRAFT", digest)
        rec, created = self.store.claim_execution(user_id, workspace_id, approval_id, key, "CREATE_GMAIL_DRAFT")
        if not created:
            return {"execution_id": rec["id"], "state": rec["state"], "replayed": True}
        raw = build_raw_message(draft["recipient"], draft["subject"], draft["body"], key)
        try:
            resp = self.client.create_draft(raw)
        except GmailError as e:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "FAILED", None, e.code, "provider rejected the draft request")
            return {"execution_id": rec["id"], "state": "FAILED", "reason": e.code}
        except TimeoutError:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "UNCERTAIN", None, "TIMEOUT", "no response; the draft may exist")
            return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": "TIMEOUT"}
        draft_id_provider = resp.get("id")
        try:
            back = self.client.get_draft_raw(draft_id_provider)
        except (GmailError, TimeoutError) as e:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "UNCERTAIN", draft_id_provider, "READBACK_FAILED",
                                        "the draft was created but could not be read back")
            return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": "READBACK_FAILED"}
        if back["to"] != draft["recipient"] or back["subject"] != draft["subject"] or operation_marker(key) not in back["body"]:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "UNCERTAIN", draft_id_provider, "READBACK_MISMATCH",
                                        "the stored draft does not match the approved draft")
            return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": "READBACK_MISMATCH"}
        self.store.finish_execution(user_id, workspace_id, rec["id"], "VERIFIED", draft_id_provider, "DRAFT_READ_BACK_MATCHED",
                                    f"draft {draft_id_provider} read back: recipient, subject and operation marker match")
        return {"execution_id": rec["id"], "state": "VERIFIED", "provider_id": draft_id_provider}

    # ---------- sending (explicit approval, single dispatch, provider check, read-back)
    def send_email(self, user_id: str, workspace_id: str, approval_id: str, draft_id: str, digest: str) -> dict:
        bound = self.store.consume_approval(user_id, workspace_id, approval_id, "SEND_EMAIL", digest, self.now())
        draft = self.store.get_draft(user_id, workspace_id, bound["draft_id"])
        key = operation_key("SEND_EMAIL", digest)
        rec, created = self.store.claim_execution(user_id, workspace_id, approval_id, key, "SEND_EMAIL")
        if not created:
            return self._replay(user_id, workspace_id, rec, key)
        # before dispatch: has an earlier attempt with this marker already reached Sent?
        try:
            found = self.client.find_by_marker(key)
        except (GmailError, TimeoutError) as e:
            reason = getattr(e, "code", "TIMEOUT")
            self.store.finish_execution(user_id, workspace_id, rec["id"], "NEEDS_RECOVERY", None, reason,
                                        "could not check Sent before sending; nothing was sent")
            return {"execution_id": rec["id"], "state": "NEEDS_RECOVERY", "reason": reason}
        if found:
            return self._verify_sent(user_id, workspace_id, rec, found[0], draft, key, "ALREADY_IN_SENT")
        raw = build_raw_message(draft["recipient"], draft["subject"], draft["body"], key)
        try:
            resp = self.client.send_raw(raw)
        except TimeoutError:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "UNCERTAIN", None, "TIMEOUT",
                                        "the send timed out; Sent will be checked before any further action")
            return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": "TIMEOUT"}
        except GmailError as e:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "FAILED", None, e.code, "the provider rejected the send")
            return {"execution_id": rec["id"], "state": "FAILED", "reason": e.code}
        return self._verify_sent(user_id, workspace_id, rec, resp.get("id"), draft, key, "SEND_RESPONSE")

    def _verify_sent(self, user_id, workspace_id, rec, provider_id, draft, key, via) -> dict:
        try:
            back = self.client.get_message_raw(provider_id)
        except (GmailError, TimeoutError) as e:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "UNCERTAIN", provider_id, "READBACK_FAILED",
                                        f"sent id {provider_id} could not be read back")
            return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": "READBACK_FAILED"}
        if back["to"] != draft["recipient"] or back["subject"] != draft["subject"] or operation_marker(key) not in back["body"]:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "UNCERTAIN", provider_id, "READBACK_MISMATCH",
                                        "the stored message does not match the approved draft")
            return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": "READBACK_MISMATCH"}
        self.store.finish_execution(user_id, workspace_id, rec["id"], "VERIFIED", provider_id, "SENT_READ_BACK_MATCHED",
                                    f"message {provider_id} ({via}) read back: recipient, subject and operation marker match")
        return {"execution_id": rec["id"], "state": "VERIFIED", "provider_id": provider_id}

    def _replay(self, user_id, workspace_id, rec, key) -> dict:
        """A key that already exists is never dispatched again. An in-flight or uncertain record is reconciled read-only."""
        if rec["state"] == "VERIFIED":
            return {"execution_id": rec["id"], "state": "VERIFIED", "replayed": True}
        if rec["state"] in ("EXECUTING", "UNCERTAIN", "NEEDS_RECOVERY"):
            try:
                found = self.client.find_by_marker(key)
            except (GmailError, TimeoutError):
                return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": "RECONCILE_UNAVAILABLE", "replayed": True}
            if found:
                draft = self.store.draft_for_execution(user_id, workspace_id, rec["id"])
                out = self._verify_sent(user_id, workspace_id, rec, found[0], draft, key, "RECONCILED_FROM_SENT")
                out["replayed"] = True
                return out
            return {"execution_id": rec["id"], "state": "NEEDS_RECOVERY", "reason": "NOT_FOUND_IN_SENT", "replayed": True}
        return {"execution_id": rec["id"], "state": rec["state"], "replayed": True}
