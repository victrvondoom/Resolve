"""Inbox service: sync from the provider, triage, commitments, the Morning Brief and context-aware drafts.

All functions take an authenticated (user_id, workspace_id). Provider failures are recorded as honest connection states
and audit events; they never produce a partial 'success'.
"""
from __future__ import annotations

import re
import time

import inbox_intel as ii
from gmail_adapter import GmailError
from inbox_store import InboxError


def sync_connection(store, user_id: str, workspace_id: str, client, account_address: str, query: str = "newer_than:30d") -> dict:
    """Fetch recent mail, persist messages and threads idempotently, and recompute triage and commitments per thread.
    account_address is the connected mailbox address (from the provider profile), used to tell the user's own messages apart."""
    conn = store.get_connection(user_id, workspace_id, "gmail")
    if conn is None:
        raise InboxError("Gmail is not connected", 409)
    try:
        messages = client.sync_all(query)
    except GmailError as e:
        status = {"REVOKED": "REVOKED", "TOKEN_EXPIRED": "EXPIRED", "RATE_LIMITED": "RATE_LIMITED"}.get(e.code, "ERROR")
        store.upsert_connection(user_id, workspace_id, "gmail", status, conn["scopes"], e.message)
        raise InboxError(f"sync stopped: {e.message}", 502 if status == "ERROR" else 409)
    changed = 0
    by_thread: dict[str, list[dict]] = {}
    for m in messages:
        res = store.upsert_message(user_id, workspace_id, conn["id"], m["provider_thread_id"], m["provider_message_id"],
                                   m["sender"], m["sent_at"], m["subject"], m["body"])
        changed += 1 if res["changed"] else 0
        by_thread.setdefault(res["thread_id"], []).append({"id": res["id"], "sender": m["sender"], "subject": m["subject"],
                                                           "body": m["body"], "sent_at": m["sent_at"]})
    commitments_added = 0
    for thread_id, msgs in by_thread.items():
        store.save_triage(user_id, workspace_id, thread_id, ii.triage_thread(msgs, account_address))
        for c in ii.extract_commitments(msgs, account_address):
            res = store.add_commitment(user_id, workspace_id, c["source_message_id"], c["kind"], c["required_action"], c["evidence_text"],
                                       None, None, c["deadline_at"], c["deadline_confidence"])
            commitments_added += 1 if res["created"] else 0
    store.upsert_connection(user_id, workspace_id, "gmail", "CONNECTED", conn["scopes"], f"synced {len(messages)} messages")
    return {"fetched": len(messages), "threads": len(by_thread), "changed_messages": changed, "new_commitments": commitments_added}


def morning_brief(store, user_id: str, workspace_id: str, since: float = 0.0) -> dict:
    """Every section is computed from persisted records. Empty sections are empty, never filled with examples."""
    triage = [t for t in store.list_triage(user_id, workspace_id) if t["computed_at"] >= since]
    commitments = store.list_commitments(user_id, workspace_id)
    drafts = store.list_drafts(user_id, workspace_id)
    attention = [t for t in triage if t["priority"] == "HIGH" or t["category"] in ("UNCERTAIN_HUMAN_REVIEW", "DEADLINE_TIME_SENSITIVE")]
    return {
        "needs_attention": attention,
        "needs_reply": [t for t in triage if t["category"] == "REPLY_REQUIRED"],
        "you_promised": [c for c in commitments if c["kind"] == "PROMISE_BY_USER" and c["status"] == "OPEN"],
        "waiting_for": [c for c in commitments if c["kind"] == "PROMISE_BY_OTHER" and c["status"] == "OPEN"],
        "upcoming_deadlines": sorted([c for c in commitments if c["deadline_at"] and c["status"] == "OPEN"], key=lambda c: c["deadline_at"]),
        "ready_to_review_drafts": [d for d in drafts if d["status"] == "DRAFT"],
        "recently_resolved": [c for c in commitments if c["status"] == "RESOLVED"],
    }


_GREETING_NAME = re.compile(r"^\s*([A-Za-z][A-Za-z'\-]+)")


def draft_reply(thread_messages: list[dict], user_address: str, signoff: str, tone: str, known_facts: dict) -> dict:
    """A reply built only from the thread and facts the user provided. It never invents prices, dates or attachments.
    Returns the draft text, the assumptions it made and the information it still needs from the user."""
    last = max(thread_messages, key=lambda m: m["sent_at"])
    sender = last.get("sender", "")
    name_match = _GREETING_NAME.match(sender.split("<")[0].strip()) if sender else None
    first = name_match.group(1) if name_match else "there"
    questions = [s for s in ii.sentences(last.get("body", "")) if s.endswith("?")]
    requests = [c for c in ii.extract_commitments(thread_messages, user_address) if c["kind"] == "REQUEST_TO_USER"]
    lines = [f"Hi {first},", ""]
    assumptions, missing = [], []
    if requests:
        r = requests[0]
        due = ""
        if r["deadline_at"] and r["deadline_confidence"] != "AMBIGUOUS":
            from datetime import datetime, timezone
            due = " by " + datetime.fromtimestamp(r["deadline_at"], timezone.utc).strftime("%A, %B %d")
        lines.append(f"Thanks for the request. I will look into it{due}.")
    elif questions:
        lines.append("Thanks for the question.")
    else:
        lines.append("Thanks for your message.")
    for q in questions[:2]:
        key = q.lower()
        if "price" in key or "cost" in key or "budget" in key:
            if "price" in known_facts:
                lines.append(f"The price is {known_facts['price']}.")
            else:
                missing.append("the price, which you have not given me")
        elif "when" in key or "date" in key or "time" in key:
            if "when" in known_facts:
                lines.append(f"{known_facts['when']}.")
            else:
                missing.append("the date or time you want to confirm")
        else:
            if "answer" in known_facts:
                lines.append(known_facts["answer"])
            else:
                missing.append(f"an answer to: {q}")
    lines += ["", signoff or "Best"]
    body = "\n".join(lines)
    if not requests and not questions:
        assumptions.append("the message needed only an acknowledgement")
    return {"subject": "Re: " + (last.get("subject") or ""), "body": body, "assumptions": assumptions, "missing_information": missing,
            "introduces_commitments": [r["required_action"] for r in requests] if requests else [], "tone": tone}
