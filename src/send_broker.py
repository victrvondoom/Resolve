"""Guarded execution broker for email writes (Gmail draft creation and sending).

Rules enforced here, not by the model:
- An operation runs only with an approval that is unexpired, unused, for this operation, and bound to the draft digest.
- The operation key is derived from the operation and the content digest; it is claimed durably BEFORE any provider call.
- A key that already exists is never dispatched again. Its state is returned, or reconciled read-only.
- Verification reads the stored message back and requires: the correlation Message-ID, the recipient, cc, subject and
  the exact normalized body. Nothing is inserted into the body for correlation. The provider response is not proof.
- A timeout or unreadable response is UNCERTAIN. Before any further action, Sent (or Drafts) is searched read-only by
  correlation Message-ID. Found -> verified against the stored content. Not found -> NEEDS_RECOVERY, which needs a new
  approval. It is never re-sent silently.
"""
from __future__ import annotations

import hashlib

from gmail_adapter import GmailError, build_raw_message, correlation_id, normalize_body
from inbox_store import InboxError  # noqa: F401  (re-exported for callers that handle both)


class BrokerRefused(Exception):
    pass


def operation_key(operation: str, digest: str) -> str:
    """Derived from the CONTENT digest and the operation, not from the approval. Approving identical content again after a
    timeout maps to the same record, so it is reconciled instead of being sent twice."""
    return hashlib.sha256(f"{operation}|{digest}".encode()).hexdigest()[:32]


def _mismatch(back: dict, draft: dict, key: str) -> str | None:
    """None when the provider's stored message is exactly the approved content; otherwise the first field that differs."""
    if back.get("message_id") != correlation_id(key):
        return "CORRELATION_MISSING"
    if (back.get("to") or "").strip() != draft["recipient"]:
        return "RECIPIENT_MISMATCH"
    if (back.get("cc") or "").strip() != (draft.get("cc") or ""):
        return "CC_MISMATCH"
    if back.get("subject") != draft["subject"]:
        return "SUBJECT_MISMATCH"
    if normalize_body(back.get("body", "")) != normalize_body(draft["body"]):
        return "BODY_MISMATCH"
    return None


class SendBroker:
    def __init__(self, store, client, now):
        self.store = store
        self.client = client          # GmailSendClient (or a test double with the same methods)
        self.now = now                # callable -> float; injected so expiry tests are deterministic

    def _raw(self, draft: dict, key: str) -> str:
        return build_raw_message(draft["recipient"], draft["subject"], draft["body"], key, draft.get("cc") or None)

    # ---------- draft creation
    def create_gmail_draft(self, user_id: str, workspace_id: str, approval_id: str, draft_id: str, digest: str) -> dict:
        bound = self.store.consume_approval(user_id, workspace_id, approval_id, "CREATE_GMAIL_DRAFT", digest, self.now())
        draft = self.store.get_draft(user_id, workspace_id, bound["draft_id"])
        key = operation_key("CREATE_GMAIL_DRAFT", digest)
        rec, created = self.store.claim_execution(user_id, workspace_id, approval_id, key, "CREATE_GMAIL_DRAFT")
        if not created:
            return self._replay(user_id, workspace_id, rec, key)
        try:
            resp = self.client.create_draft(self._raw(draft, key))
        except GmailError as e:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "FAILED", None, e.code, "provider rejected the draft request")
            return {"execution_id": rec["id"], "state": "FAILED", "reason": e.code}
        except TimeoutError:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "UNCERTAIN", None, "TIMEOUT", "no response; a draft may exist")
            return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": "TIMEOUT"}
        provider_id = (resp.get("id") or (resp.get("message") or {}).get("id"))
        return self._verify_draft(user_id, workspace_id, rec, provider_id, draft, key)

    def _verify_draft(self, user_id, workspace_id, rec, provider_id, draft, key) -> dict:
        try:
            back = self.client.get_draft_raw(provider_id)
        except (GmailError, TimeoutError):
            self.store.finish_execution(user_id, workspace_id, rec["id"], "UNCERTAIN", provider_id, "READBACK_FAILED",
                                        "the draft was created but could not be read back")
            return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": "READBACK_FAILED"}
        why = _mismatch(back, draft, key)
        if why:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "UNCERTAIN", provider_id, why, "the stored draft does not match the approved draft")
            return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": why}
        self.store.finish_execution(user_id, workspace_id, rec["id"], "VERIFIED", provider_id, "DRAFT_READ_BACK_MATCHED",
                                    f"draft {provider_id} read back: correlation, recipient, cc, subject and body match")
        self.store.set_draft_provider_id(user_id, workspace_id, draft["id"], provider_id, "SAVED_TO_GMAIL")
        return {"execution_id": rec["id"], "state": "VERIFIED", "provider_id": provider_id}

    # ---------- sending
    def send_email(self, user_id: str, workspace_id: str, approval_id: str, draft_id: str, digest: str) -> dict:
        bound = self.store.consume_approval(user_id, workspace_id, approval_id, "SEND_EMAIL", digest, self.now())
        draft = self.store.get_draft(user_id, workspace_id, bound["draft_id"])
        key = operation_key("SEND_EMAIL", digest)
        rec, created = self.store.claim_execution(user_id, workspace_id, approval_id, key, "SEND_EMAIL")
        if not created:
            return self._replay(user_id, workspace_id, rec, key)
        # before dispatch: has an earlier attempt with this correlation already reached Sent?
        try:
            found = self.client.find_by_correlation(key)
        except (GmailError, TimeoutError) as e:
            reason = getattr(e, "code", "TIMEOUT")
            self.store.finish_execution(user_id, workspace_id, rec["id"], "NEEDS_RECOVERY", None, reason,
                                        "could not check Sent before sending; nothing was sent")
            return {"execution_id": rec["id"], "state": "NEEDS_RECOVERY", "reason": reason}
        if found:
            return self._verify_sent(user_id, workspace_id, rec, found[0], draft, key, "ALREADY_IN_SENT")
        try:
            resp = self.client.send_raw(self._raw(draft, key))
        except TimeoutError:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "UNCERTAIN", None, "TIMEOUT",
                                        "the send timed out; Sent will be checked read-only before any further action")
            return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": "TIMEOUT"}
        except GmailError as e:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "FAILED", None, e.code, "the provider rejected the send")
            return {"execution_id": rec["id"], "state": "FAILED", "reason": e.code}
        return self._verify_sent(user_id, workspace_id, rec, resp.get("id"), draft, key, "SEND_RESPONSE")

    def _verify_sent(self, user_id, workspace_id, rec, provider_id, draft, key, via) -> dict:
        try:
            back = self.client.get_message_raw(provider_id)
        except (GmailError, TimeoutError):
            self.store.finish_execution(user_id, workspace_id, rec["id"], "UNCERTAIN", provider_id, "READBACK_FAILED",
                                        f"sent id {provider_id} could not be read back")
            return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": "READBACK_FAILED"}
        why = _mismatch(back, draft, key)
        if why:
            self.store.finish_execution(user_id, workspace_id, rec["id"], "UNCERTAIN", provider_id, why,
                                        "the stored message does not match the approved draft")
            return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": why}
        self.store.finish_execution(user_id, workspace_id, rec["id"], "VERIFIED", provider_id, "SENT_READ_BACK_MATCHED",
                                    f"message {provider_id} ({via}) read back: correlation, recipient, cc, subject and body match")
        self.store.set_draft_provider_id(user_id, workspace_id, draft["id"], provider_id, "SENT_VERIFIED")
        return {"execution_id": rec["id"], "state": "VERIFIED", "provider_id": provider_id}

    # ---------- replay of an existing key: never a second dispatch
    def _replay(self, user_id, workspace_id, rec, key) -> dict:
        if rec["state"] == "VERIFIED":
            return {"execution_id": rec["id"], "state": "VERIFIED", "replayed": True}
        if rec["state"] in ("EXECUTING", "UNCERTAIN", "NEEDS_RECOVERY"):
            op = rec.get("operation") or "SEND_EMAIL"
            draft = self.store.draft_for_execution(user_id, workspace_id, rec["id"])
            try:
                found = (self.client.find_draft_by_correlation(key) if op == "CREATE_GMAIL_DRAFT" else self.client.find_by_correlation(key))
            except (GmailError, TimeoutError):
                return {"execution_id": rec["id"], "state": "UNCERTAIN", "reason": "RECONCILE_UNAVAILABLE", "replayed": True}
            if found:
                if op == "CREATE_GMAIL_DRAFT":
                    out = self._verify_draft(user_id, workspace_id, rec, found[0], draft, key)
                else:
                    out = self._verify_sent(user_id, workspace_id, rec, found[0], draft, key, "RECONCILED_FROM_SENT")
                out["replayed"] = True
                return out
            return {"execution_id": rec["id"], "state": "NEEDS_RECOVERY", "reason": "NOT_FOUND_BY_CORRELATION", "replayed": True}
        return {"execution_id": rec["id"], "state": rec["state"], "replayed": True}

    def reconcile(self, user_id: str, workspace_id: str, execution_id: str) -> dict:
        """Read-only reconciliation of an uncertain or interrupted operation. Never dispatches; never resends."""
        rec = self.store.get_execution(user_id, workspace_id, execution_id)
        if rec["state"] == "VERIFIED":
            return {"execution_id": rec["id"], "state": "VERIFIED", "replayed": True}
        if rec["state"] not in ("UNCERTAIN", "NEEDS_RECOVERY", "EXECUTING"):
            raise BrokerRefused("this operation is not in a state that needs reconciliation")
        return self._replay(user_id, workspace_id, rec, rec["operation_key"])
