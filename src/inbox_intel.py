"""Deterministic inbox intelligence: triage, commitment extraction and deadline resolution.

No model is called here. Every decision cites the exact sentence it came from. The rules are inspectable and the
labelled fixtures in benchmarks/inbox_fixtures.json measure them. Email text is untrusted data: nothing in a message
can change a rule, a permission or a category other than by matching these patterns.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

CATEGORIES = ("ACTION_REQUIRED", "REPLY_REQUIRED", "WAITING_ON_SOMEONE", "DEADLINE_TIME_SENSITIVE", "IMPORTANT_INFORMATION",
              "GENERAL_INFORMATION", "AUTOMATED_LOW_PRIORITY", "UNCERTAIN_HUMAN_REVIEW")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
AUTOMATED_SENDER = re.compile(r"(no-?reply|donotreply|notifications?@|mailer-daemon|newsletter)", re.I)
REQUEST_RE = re.compile(r"\b(could you|can you|would you|please|need you to|send me|can we get|will you)\b", re.I)
PROMISE_RE = re.compile(r"\b(i'?ll|i will|i can|i'?m going to|i'?m sending|i promise|i'?ll send)\b", re.I)
OTHER_PROMISE_RE = re.compile(r"\b(he'?ll|she'?ll|they'?ll|we'?ll|i'?ll|we will|i'?ll get back|will send|will share)\b", re.I)
DEPENDENCY_RE = re.compile(r"\b(before|ahead of|prior to)\b.*\b(call|meeting|review|demo|launch)\b", re.I)
HYPOTHETICAL_RE = re.compile(r"\b(maybe|might|possibly|perhaps|if we|we could consider|someday|would be nice)\b", re.I)
HISTORICAL_RE = re.compile(r"\b(i sent|we discussed|last week|yesterday i|already sent|you sent)\b", re.I)
URGENT_WORDS = re.compile(r"\b(urgent|asap|important|immediately)\b", re.I)
AMBIGUOUS_TIME = re.compile(r"\b(soon|later|next week|end of the month|at some point|when you can|shortly)\b", re.I)
AUTOMATED_BODY = re.compile(r"(unsubscribe|this is an automated|do not reply|you are receiving this)", re.I)


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return [p.strip() for p in parts if p and p.strip()]


def resolve_deadline(sentence: str, sent_at: float, tz_offset_hours: int = 0) -> tuple[float | None, str | None, str | None]:
    """Return (deadline_ts, confidence, review_reason). deadline is the END of the due day in the configured timezone.
    Never invents a date: no match -> (None, None, None). Ambiguous phrases -> (None, 'AMBIGUOUS', reason)."""
    tz = timezone(timedelta(hours=tz_offset_hours))
    base = datetime.fromtimestamp(sent_at, tz)
    s = sentence.lower()
    if AMBIGUOUS_TIME.search(s) and not re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today|\d{1,2}\s*(st|nd|rd|th)?\b)", s):
        return None, "AMBIGUOUS", "the time phrase is vague; please set the deadline"
    if re.search(r"\bnext (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", s):
        return None, "AMBIGUOUS", "'next <weekday>' can mean two different days; please confirm"
    due = None
    if re.search(r"\btomorrow\b", s):
        due = base + timedelta(days=1)
        conf = "RESOLVED_RELATIVE"
    elif re.search(r"\btoday\b", s):
        due = base
        conf = "RESOLVED_RELATIVE"
    else:
        for i, name in enumerate(WEEKDAYS):
            if re.search(rf"\b(by|on|before|until)\s+{name}\b", s):
                delta = (i - base.weekday()) % 7
                due = base + timedelta(days=delta)
                conf = "RESOLVED_RELATIVE"
                break
        else:
            m = re.search(r"\b(by|on|before)\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b", s)
            if m:
                month, day = MONTHS[m.group(2)], int(m.group(3))
                year = base.year if (month, day) >= (base.month, base.day) else base.year + 1
                due = datetime(year, month, day, tzinfo=tz)
                conf = "EXPLICIT_DATE"
            else:
                iso = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", s)
                if iso:
                    due = datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), tzinfo=tz)
                    conf = "EXPLICIT_DATE"
    if due is None:
        return None, None, None
    end_of_day = due.replace(hour=23, minute=59, second=59, microsecond=0)
    return end_of_day.timestamp(), conf, None


def extract_commitments(messages: list[dict], user_address: str) -> list[dict]:
    """messages: [{id, sender, body, sent_at}]. Returns commitments with exact evidence sentences."""
    out = []
    user = (user_address or "").lower()
    for m in messages:
        sender = (m.get("sender") or "").lower()
        if AUTOMATED_SENDER.search(sender) or AUTOMATED_BODY.search(m.get("body", "")):
            continue                      # automated notices never create commitments
        from_user = bool(user) and user in sender
        for sent in sentences(m.get("body", "")):
            if HYPOTHETICAL_RE.search(sent) or HISTORICAL_RE.search(sent):
                continue
            kind, action = None, None
            if PROMISE_RE.search(sent) and from_user:
                kind, action = "PROMISE_BY_USER", sent
            elif OTHER_PROMISE_RE.search(sent) and not from_user:
                kind, action = "PROMISE_BY_OTHER", sent
            elif REQUEST_RE.search(sent) and not from_user:
                kind, action = "REQUEST_TO_USER", sent
            elif DEPENDENCY_RE.search(sent) and not from_user:
                kind, action = "DEPENDENCY", sent
            if kind is None:
                continue
            deadline, conf, review = resolve_deadline(sent, m["sent_at"])
            out.append({"source_message_id": m["id"], "kind": kind, "required_action": action[:300], "evidence_text": sent[:500],
                        "deadline_at": deadline, "deadline_confidence": conf, "review_reason": review})
    return out


def triage_thread(messages: list[dict], user_address: str) -> dict:
    """Classify a thread from its messages. Returns category, priority, confidence, reasons and evidence sentences."""
    user = (user_address or "").lower()
    last = max(messages, key=lambda m: m["sent_at"])
    text = " ".join(m.get("body", "") for m in messages)
    reasons, evidence = [], []
    if AUTOMATED_SENDER.search(last.get("sender", "")) or AUTOMATED_BODY.search(text):
        return _decision("AUTOMATED_LOW_PRIORITY", "LOW", 0.9, ["automated sender or unsubscribe footer"], [last.get("sender", "")], messages)
    from_user_last = bool(user) and user in last.get("sender", "").lower()
    commitments = extract_commitments(messages, user_address)
    deadlines = [c for c in commitments if c["deadline_at"]]
    ambiguous = [c for c in commitments if c["deadline_confidence"] == "AMBIGUOUS"]
    questions = [s for s in sentences(last.get("body", "")) if s.endswith("?")]
    if ambiguous:
        return _decision("UNCERTAIN_HUMAN_REVIEW", "MEDIUM", 0.55, ["a deadline is ambiguous: " + ambiguous[0]["review_reason"]],
                         [ambiguous[0]["evidence_text"]], messages)
    if deadlines and not from_user_last:
        d = min(deadlines, key=lambda c: c["deadline_at"])
        return _decision("DEADLINE_TIME_SENSITIVE", "HIGH", 0.85, ["an explicit deadline is stated in the message"], [d["evidence_text"]], messages)
    if questions and not from_user_last:
        return _decision("REPLY_REQUIRED", "NORMAL", 0.8, ["the latest message asks a question"], [questions[0]], messages)
    req = [c for c in commitments if c["kind"] == "REQUEST_TO_USER"]
    if req and not from_user_last:
        return _decision("ACTION_REQUIRED", "NORMAL", 0.8, ["the message asks you to do something"], [req[0]["evidence_text"]], messages)
    waiting = [c for c in commitments if c["kind"] == "PROMISE_BY_OTHER"]
    if waiting and not from_user_last:
        return _decision("WAITING_ON_SOMEONE", "NORMAL", 0.7, ["another person promised something"], [waiting[0]["evidence_text"]], messages)
    if from_user_last and any(c["kind"] == "PROMISE_BY_USER" for c in commitments):
        return _decision("WAITING_ON_SOMEONE", "NORMAL", 0.7, ["you made a promise and are waiting to deliver"], [next(c for c in commitments if c["kind"] == "PROMISE_BY_USER")["evidence_text"]], messages)
    if URGENT_WORDS.search(text):
        reasons.append("urgent wording without a stated deadline does not raise priority")
    return _decision("GENERAL_INFORMATION", "LOW", 0.6, reasons or ["no request, promise or deadline found"], [], messages)


def _decision(category, priority, confidence, reasons, evidence, messages):
    return {"category": category, "priority": priority, "confidence": confidence, "reasons": reasons,
            "evidence": [e for e in evidence if e], "message_count": len(messages)}
