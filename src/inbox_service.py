"""Inbox service: sync, triage of COMPLETE threads, commitments with owners and deadlines, the Morning Brief (computed only
from persisted records) and facts-only draft preparation.

Every function takes an authenticated (user_id, workspace_id). The workspace is already scoped by the caller
(LIVE and REPLAY use different workspaces). Provider failures are recorded as honest connection states and sync runs.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import inbox_intel as ii
from gmail_adapter import GmailError
from inbox_store import InboxError

RECENT_WINDOW_DAYS = 14
DUE_SOON_SECONDS = 48 * 3600
OPEN_STATUSES = ("OPEN", "IN_PROGRESS", "WAITING", "UNCERTAIN", "NEEDS_REVIEW")


def _status_for_error(code: str) -> str:
    return {"REVOKED": "REVOKED", "TOKEN_EXPIRED": "EXPIRED", "RATE_LIMITED": "RATE_LIMITED"}.get(code, "ERROR")


def sync_connection(store, user_id: str, workspace_id: str, client, account_address: str, *, query: str = "newer_than:30d",
                    tz_name: str = "UTC", mode: str = "LIVE") -> dict:
    """Fetch recent mail, persist messages idempotently, re-triage every touched thread from ALL its stored messages, and
    reconcile commitments per obligation (revisions keep history). Every run is recorded, including failed ones."""
    conn = store.get_connection(user_id, workspace_id, "gmail" if mode == "LIVE" else "replay")
    if conn is None:
        raise InboxError("the mailbox is not connected", 409)
    run = store.start_sync_run(user_id, workspace_id, conn["id"], mode)
    try:
        messages = sorted(client.sync_all(query), key=lambda m: m["sent_at"])
    except GmailError as e:
        status = _status_for_error(e.code)
        store.upsert_connection(user_id, workspace_id, conn["provider"], status, conn["scopes"], e.message)
        store.finish_sync_run(user_id, workspace_id, run, "FAILED", error=f"{e.code}: {e.message}")
        raise InboxError(f"sync stopped: {e.message}", 502 if status == "ERROR" else 409)
    except TimeoutError:
        store.finish_sync_run(user_id, workspace_id, run, "FAILED", error="TIMEOUT")
        raise InboxError("sync timed out; nothing was marked as synchronized", 504)
    changed, touched = 0, set()
    for m in messages:
        res = store.upsert_message(user_id, workspace_id, conn["id"], m["provider_thread_id"], m["provider_message_id"],
                                   m["sender"], m["sent_at"], m["subject"], m["body"], recipients=m.get("to", ""))
        changed += 1 if res["changed"] else 0
        touched.add(res["thread_id"])
    new_commitments = 0
    for thread_id in touched:
        thread = _thread_messages(store, user_id, workspace_id, thread_id)
        store.save_triage(user_id, workspace_id, thread_id, ii.triage_thread(thread, account_address, tz_name))
        for c in ii.extract_commitments(thread, account_address, tz_name):
            res = store.reconcile_obligation(user_id, workspace_id, thread_id, c["kind"], c["obligation_key"], c)
            new_commitments += 1 if res["action"] == "CREATED" else 0
    store.upsert_connection(user_id, workspace_id, conn["provider"], "CONNECTED", conn["scopes"],
                            f"last sync {int(time.time())}: {len(messages)} messages")
    store.finish_sync_run(user_id, workspace_id, run, "SUCCEEDED", fetched=len(messages), changed=changed, new_commitments=new_commitments)
    return {"run_id": run, "mode": mode, "state": "SUCCEEDED", "fetched": len(messages), "threads": len(touched),
            "changed_messages": changed, "new_commitments": new_commitments, "account": account_address}


def _thread_messages(store, user_id, workspace_id, thread_id) -> list[dict]:
    out = []
    for m in store.list_messages(user_id, workspace_id, thread_id):
        out.append({"id": m["id"], "sender": m["sender"], "to": m.get("recipients", ""), "subject": m["subject"],
                    "body": m["body"], "sent_at": m["sent_at"]})
    return out


def _local(ts: float, tz_name: str) -> datetime:
    return datetime.fromtimestamp(ts, ZoneInfo(tz_name))


def _timing(deadline_at: float | None, now: float, tz_name: str) -> str:
    if deadline_at is None:
        return "UNKNOWN"
    if deadline_at < now:
        return "OVERDUE"
    if _local(deadline_at, tz_name).date() == _local(now, tz_name).date():
        return "DUE_TODAY"
    if deadline_at - now <= DUE_SOON_SECONDS:
        return "DUE_SOON"
    return "FUTURE"


def _is_from_user(sender: str, user_address: str) -> bool:
    return bool(user_address) and user_address.lower() in (sender or "").lower()


def morning_brief(store, user_id: str, workspace_id: str, *, user_address: str, tz_name: str = "UTC", now: float | None = None) -> dict:
    """Every section is computed from persisted records at request time. Empty sections are empty, never sample data."""
    now = time.time() if now is None else now
    local_now = _local(now, tz_name)
    commitments = store.list_commitments(user_id, workspace_id)
    open_c = [c for c in commitments if c["status"] in OPEN_STATUSES]
    for c in commitments:
        c["timing"] = _timing(c["deadline_at"], now, tz_name) if c["status"] in OPEN_STATUSES else None
    triage = store.list_triage(user_id, workspace_id)
    rows = []
    for t in triage:
        msgs = _thread_messages(store, user_id, workspace_id, t["thread_id"])
        if not msgs:
            continue
        last = max(msgs, key=lambda m: m["sent_at"])
        from_user_last = _is_from_user(last["sender"], user_address)
        last_c = [c for c in ii.extract_commitments([last], user_address, tz_name)]
        questions = [s for s in ii.sentences(last["body"]) if s.endswith("?")]
        thread_deadlines = [c for c in open_c if c["thread_id"] == t["thread_id"] and c["deadline_at"]]
        rows.append({"thread_id": t["thread_id"], "subject": last["subject"], "sender": last["sender"], "last_message_at": last["sent_at"],
                     "message_count": len(msgs), "category": t["category"], "priority": t["priority"], "confidence": t["confidence"],
                     "reasons": t["reasons"], "evidence": t["evidence"][:1],
                     "deadline": min((c["deadline_at"] for c in thread_deadlines), default=None),
                     "review_required": t["category"] == "UNCERTAIN_HUMAN_REVIEW",
                     "awaiting_user_reply": (not from_user_last) and bool(questions or any(c["kind"] == "REQUEST_TO_USER" for c in last_c)),
                     "question": questions[0] if questions else None})
    attention = [r for r in rows if not _is_from_user(r["sender"], user_address) and r["category"] not in ("AUTOMATED_LOW_PRIORITY", "GENERAL_INFORMATION")]
    attention.sort(key=lambda r: (r["priority"] != "HIGH", r["deadline"] or float("inf"), -r["last_message_at"]))
    replies = [r for r in rows if r["awaiting_user_reply"]]
    my = [c for c in open_c if c["kind"] == "PROMISE_BY_USER"]
    waiting = [c for c in open_c if c["kind"] == "PROMISE_BY_OTHER"]
    owed_to_me = [c for c in open_c if c["kind"] == "REQUEST_TO_USER"]
    timing = {k: [] for k in ("OVERDUE", "DUE_TODAY", "DUE_SOON", "FUTURE", "UNKNOWN")}
    for c in sorted(open_c, key=lambda c: (c["deadline_at"] is None, c["deadline_at"] or 0)):
        timing[c["timing"]].append(c)
    cutoff = now - RECENT_WINDOW_DAYS * 86400
    recent = [c for c in commitments if c["status"] in ("RESOLVED_VERIFIED", "RESOLVED_USER_CONFIRMED", "DISMISSED", "CANCELLED") and c["updated_at"] >= cutoff]
    drafts = [d for d in store.list_drafts(user_id, workspace_id) if d["status"] in ("DRAFT", "APPROVED", "SAVED_TO_GMAIL")]
    return {
        "generated_at": now, "timezone": tz_name, "local_date": local_now.date().isoformat(),
        "summary": {"needs_attention": len(attention), "replies_required": len(replies), "my_open_commitments": len(my),
                    "waiting_on_others": len(waiting), "overdue": len(timing["OVERDUE"]), "drafts_waiting": len(drafts)},
        "needs_attention": attention, "replies_to_send": replies,
        "my_commitments": my, "waiting_on_others": waiting, "requests_to_me": owed_to_me,
        "timing": timing, "drafts": drafts, "recently_resolved": recent, "recent_window_days": RECENT_WINDOW_DAYS,
    }


_GREETING_NAME = re.compile(r"^\s*([A-Za-z][A-Za-z'\-]+)")


def draft_reply(thread_messages: list[dict], user_address: str, signoff: str, tone: str, known_facts: dict,
                tz_name: str = "UTC") -> dict:
    """A reply built only from the thread and facts the user supplied. It never invents prices, dates, estimates, links,
    attachments or approvals. Anything it cannot state from the thread or the facts is listed as missing information."""
    last = max(thread_messages, key=lambda m: m["sent_at"])
    sender = last.get("sender", "")
    name_match = _GREETING_NAME.match(sender.split("<")[0].strip().strip('"')) if sender else None
    first = name_match.group(1) if name_match else "there"
    questions = [s for s in ii.sentences(last.get("body", "")) if s.endswith("?")]
    requests = [c for c in ii.extract_commitments(thread_messages, user_address, tz_name) if c["kind"] == "REQUEST_TO_USER"
                and c["source_message_id"] == last["id"]]
    lines = [f"Hi {first},", ""]
    assumptions, missing, sources = [], [], []
    if requests:
        r = requests[0]
        sources.append({"claim": "you were asked to act", "message_id": r["source_message_id"], "evidence": r["evidence_text"]})
        due = ""
        if r["deadline_at"] and r["deadline_confidence"] != "AMBIGUOUS":
            due = " by " + _local(r["deadline_at"], tz_name).strftime("%A, %B %d")
            sources.append({"claim": "deadline", "message_id": r["source_message_id"], "evidence": r["deadline_phrase"] or ""})
        elif r["deadline_confidence"] == "AMBIGUOUS":
            missing.append("the date you can commit to (the request says '" + (r["deadline_phrase"] or "soon") + "')")
        if "status" in known_facts:
            lines.append(f"Thanks for the request. {known_facts['status']}")
        else:
            lines.append(f"Thanks for the request. I will look into it{due}.")
            missing.append("what you want to say about progress on the request")
    elif questions:
        lines.append("Thanks for the question.")
    else:
        lines.append("Thanks for your message.")
        assumptions.append("the message needed only an acknowledgement")
    for q in questions[:2]:
        key = q.lower()
        if re.search(r"price|cost|budget|quote|estimate", key):
            if "price" in known_facts:
                lines.append(f"The price is {known_facts['price']}.")
            else:
                missing.append("the price or estimate you want to give; none exists in the thread or in your facts")
        elif re.search(r"\bwhen\b|\bdate\b|\btime\b|\bdeadline\b", key):
            if "when" in known_facts:
                lines.append(known_facts["when"])
            else:
                missing.append("the date or time you want to confirm")
        else:
            if "answer" in known_facts:
                lines.append(known_facts["answer"])
            else:
                missing.append(f"an answer to: {q}")
    lines += ["", signoff or "Best"]
    body = "\n".join(lines)
    introduces = [c["required_action"] for c in ii.extract_commitments(
        [{"id": "draft", "sender": user_address, "body": body, "sent_at": last["sent_at"]}], user_address, tz_name) if c["kind"] == "PROMISE_BY_USER"]
    return {"subject": "Re: " + (last.get("subject") or "").removeprefix("Re: "), "body": body, "assumptions": assumptions,
            "missing_information": missing, "sources": sources, "potential_commitments": introduces, "tone": tone}
