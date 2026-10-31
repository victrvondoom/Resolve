"""Deterministic inbox intelligence: triage, commitment extraction and deadline resolution.

No model is called here. Every decision cites the exact sentence it came from. The rules are inspectable and the
labelled fixtures in benchmarks/inbox_fixtures.json measure them. Email text is untrusted data: nothing in a message
can change a rule, a permission or a category other than by matching these patterns.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CATEGORIES = ("ACTION_REQUIRED", "REPLY_REQUIRED", "WAITING_ON_SOMEONE", "DEADLINE_TIME_SENSITIVE", "IMPORTANT_INFORMATION",
              "GENERAL_INFORMATION", "AUTOMATED_LOW_PRIORITY", "UNCERTAIN_HUMAN_REVIEW")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
AUTOMATED_SENDER = re.compile(r"(no-?reply|donotreply|notifications?@|mailer-daemon|newsletter)", re.I)
REQUEST_RE = re.compile(r"\b(could you|can you|would you|please|need you to|send me|can we get|will you)\b", re.I)
PROMISE_RE = re.compile(r"\b(i['\u2019]ll|i will|i can|i['\u2019]m going to|i['\u2019]m sending|i promise)\b", re.I)
OTHER_PROMISE_RE = re.compile(r"\b(he['\u2019]ll|she['\u2019]ll|they['\u2019]ll|we['\u2019]ll|i['\u2019]ll|we will|will send|will share)\b", re.I)
DEPENDENCY_RE = re.compile(r"\b(before|ahead of|prior to)\b.*\b(call|meeting|review|demo|launch)\b", re.I)
HYPOTHETICAL_RE = re.compile(r"\b(maybe|might|possibly|perhaps|if we|we could consider|someday|would be nice)\b", re.I)
HISTORICAL_RE = re.compile(r"\b(i sent|we discussed|last week|yesterday i|already sent|you sent)\b", re.I)
URGENT_WORDS = re.compile(r"\b(urgent|asap|important|immediately)\b", re.I)
AMBIGUOUS_TIME = re.compile(r"\b(soon|later|next week|end of the month|at some point|when you can|shortly|when possible|whenever (?:you can|possible)|in due course|sometime)\b", re.I)
INJECTION_RE = re.compile(r"ignore (all |any )?(previous|prior|above) instructions|disregard (the |your )?(system|previous) (prompt|instructions)|"
                          r"send (me )?(the )?(full |entire |whole )?inbox|forward (me )?every (message|email)|reveal (your|the) (system )?prompt", re.I)
AUTOMATED_BODY = re.compile(r"(unsubscribe|this is an automated|do not reply|you are receiving this)", re.I)


def sentences(text: str) -> list[str]:
    """Sentences of the NEW text. Quoted lines (starting with '>') are earlier messages, not new statements, and are dropped."""
    fresh = "\n".join(line for line in (text or "").splitlines() if not line.lstrip().startswith(">"))
    parts = re.split(r"(?<=[.!?])\s+|\n+", fresh)
    return [p.strip() for p in parts if p and p.strip()]


TIME_OF_DAY = re.compile(r"\b(?:by|before|at|until|no later than|around|today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\s+(noon|midnight|\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\d{1,2}:\d{2})\b", re.I)


def _zone(tz_name):
    """An IANA zone name. An integer is a fixed UTC offset in hours (kept for older callers only)."""
    if isinstance(tz_name, int):
        return timezone(timedelta(hours=tz_name)), f"UTC{tz_name:+d}"
    try:
        return ZoneInfo(tz_name or "UTC"), tz_name or "UTC"
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown timezone: {tz_name!r}")


def _clock(text):
    """'noon' -> 12:00, '4 PM' -> 16:00, '16:30' -> 16:30. None when no clock time is stated."""
    m = TIME_OF_DAY.search(text)
    if not m:
        return None
    raw = m.group(1).lower().replace(".", "")
    if raw == "noon":
        return 12, 0
    if raw == "midnight":
        return 0, 0
    mm = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", raw)
    hour, minute = int(mm.group(1)), int(mm.group(2) or 0)
    if mm.group(3):
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if mm.group(3) == "pm" else 0)
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def resolve_deadline_detail(sentence: str, sent_at: float, tz_name: str = "UTC") -> dict:
    """Full deadline record: resolved instant, the original phrase, zone, confidence and whether it was explicit.
    A date without a clock time resolves to the end of that day in the configured zone. A clock time resolves to that
    instant. Vague phrases and 'next <weekday>' are AMBIGUOUS and get no date."""
    tz, zone_label = _zone(tz_name)
    base = datetime.fromtimestamp(sent_at, tz)
    s = sentence.lower()
    empty = {"deadline_at": None, "deadline_confidence": None, "review_reason": None, "phrase": None, "timezone": zone_label, "explicit": False}
    if AMBIGUOUS_TIME.search(s) and not re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|today|\d{1,2}\s*(st|nd|rd|th)?\b)", s):
        return {**empty, "deadline_confidence": "AMBIGUOUS", "review_reason": "the time phrase is vague; please set the deadline",
                "phrase": AMBIGUOUS_TIME.search(s).group(0)}
    nxt = re.search(r"\bnext (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", s)
    if nxt:
        return {**empty, "deadline_confidence": "AMBIGUOUS", "review_reason": "'next <weekday>' can mean two different days; please confirm", "phrase": nxt.group(0)}
    due, conf, phrase, explicit = None, None, None, False
    if re.search(r"\btomorrow\b", s):
        due, conf, phrase = base + timedelta(days=1), "RESOLVED_RELATIVE", "tomorrow"
    elif re.search(r"\btoday\b", s):
        due, conf, phrase = base, "RESOLVED_RELATIVE", "today"
    else:
        for i, name in enumerate(WEEKDAYS):
            hit = re.search(rf"\b(by|on|before|until)\s+({name})\b", s)
            if hit:
                delta = (i - base.weekday()) % 7
                if hit.group(1) == "before":
                    # 'before Friday' means the day before Friday; sent on a Friday it cannot be resolved without asking
                    if delta == 0:
                        return {**empty, "deadline_confidence": "AMBIGUOUS", "phrase": hit.group(0),
                                "review_reason": "'before <today's weekday>' is unclear; please confirm the date"}
                    due = base + timedelta(days=delta - 1)
                else:
                    due = base + timedelta(days=delta)
                due, conf, phrase = due, "RESOLVED_RELATIVE", hit.group(0)
                break
        else:
            m = re.search(r"\b(by|on|before)\s+(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})\b", s)
            if m:
                month, day = MONTHS[m.group(2)], int(m.group(3))
                year = base.year if (month, day) >= (base.month, base.day) else base.year + 1
                due, conf, phrase, explicit = datetime(year, month, day, tzinfo=tz), "EXPLICIT_DATE", m.group(0), True
            else:
                iso = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", s)
                if iso:
                    due = datetime(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)), tzinfo=tz)
                    conf, phrase, explicit = "EXPLICIT_DATE", iso.group(0), True
    clock = _clock(sentence)
    if clock is None and due is None:
        return empty
    if clock is not None:
        if due is None:
            due, conf, phrase, explicit = base, "RESOLVED_RELATIVE", TIME_OF_DAY.search(sentence).group(0), True
        else:
            tod = TIME_OF_DAY.search(sentence).group(0)
            head = phrase.split()[-1].lower() if phrase else ""
            # 'by Monday' + 'Monday 9 am' -> 'by Monday 9 am' (the day word is shared, not repeated)
            phrase = f"{phrase} {tod.split(' ', 1)[1]}" if head and tod.lower().startswith(head + " ") else f"{phrase} {tod}".strip()
        due = due.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        return {"deadline_at": due.timestamp(), "deadline_confidence": conf, "review_reason": None, "phrase": phrase, "timezone": zone_label, "explicit": explicit}
    end_of_day = due.replace(hour=23, minute=59, second=59, microsecond=0)
    return {"deadline_at": end_of_day.timestamp(), "deadline_confidence": conf, "review_reason": None, "phrase": phrase, "timezone": zone_label, "explicit": explicit}


def resolve_deadline(sentence: str, sent_at: float, tz_name="UTC") -> tuple[float | None, str | None, str | None]:
    """Compatibility wrapper: (deadline_ts, confidence, review_reason). Never invents a date."""
    d = resolve_deadline_detail(sentence, sent_at, tz_name)
    return d["deadline_at"], d["deadline_confidence"], d["review_reason"]


_STOP = {"reminder", "update", "updated", "updates", "again", "following", "follow", "checking", "ping", "could", "would", "will", "please", "that", "this", "with", "from", "your", "have", "just", "need", "want", "into", "about", "them", "there",
         "should", "they", "their", "then", "than", "instead", "moved", "legal", "following", "thanks", "thank", "also", "still", "again", "really",
         "today", "tomorrow", "soon", "when", "what", "which", "where", "while", "hello", "hope", "sorry", "kindly", "ahead", "anyway"}
_DEADLINE_WORDS = re.compile(r"\b(by|before|until|on|no later than|tomorrow|today|next|monday|tuesday|wednesday|thursday|friday|saturday|sunday|noon|midnight|at)\b|\d{1,2}(:\d{2})?\s*(am|pm)|\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}\b|\b20\d{2}-\d{2}-\d{2}\b", re.I)


def obligation_key(action: str) -> str:
    """Stable identity of an obligation inside a thread: its content words with the deadline phrase removed.
    'Could you send the revised proposal by Wednesday?' and '... by Friday?' share one key (one obligation, two dates);
    'send the checklist' and 'send the schema' do not."""
    text = _DEADLINE_WORDS.sub(" ", action.lower())
    words = sorted({w for w in re.findall(r"[a-z0-9]+", text) if len(w) > 2 and w not in _STOP and w not in {"you", "the", "and", "for"}})
    return " ".join(words)[:200]


def obligation_words(action: str) -> set[str]:
    """Content words of an obligation for matching the same obligation across messages (deadlines and filler removed)."""
    text = _DEADLINE_WORDS.sub(" ", (action or "").lower())
    return {w for w in re.findall(r"[a-z0-9]+", text) if len(w) > 2 and w not in _STOP and w not in {"you", "the", "and", "for"}}


def same_obligation(a: str, b: str, threshold: float = 0.7) -> bool:
    """Two requests are the same obligation when their content words overlap strongly (Jaccard). Different deliverables in
    the same thread are kept apart; a restated or re-dated request is matched."""
    wa, wb = obligation_words(a), obligation_words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= threshold


def _person(header: str) -> str:
    m = re.search(r"<([^>]+)>", header or "")
    return (m.group(1) if m else (header or "")).strip().lower()


def extract_commitments(messages: list[dict], user_address: str, tz_name="UTC") -> list[dict]:
    """messages: [{id, sender, to, body, sent_at}]. Returns commitments with the exact evidence sentence and the owners.

    Ownership is resolved from the authenticated mailbox address and the actual sender or recipient, never from a
    display name alone. If a party cannot be resolved the field is None and ownership_state says so.
    """
    out = []
    user = (user_address or "").lower().strip()
    for m in messages:
        sender_raw = m.get("sender") or ""
        sender = sender_raw.lower()
        if AUTOMATED_SENDER.search(sender) or AUTOMATED_BODY.search(m.get("body", "")):
            continue                      # automated notices never create commitments
        from_user = bool(user) and user in sender
        sender_addr = _person(sender_raw)
        recipient_addr = _person((m.get("to") or "").split(",")[0]) if m.get("to") else None
        for sent in sentences(m.get("body", "")):
            if HYPOTHETICAL_RE.search(sent) or HISTORICAL_RE.search(sent):
                continue
            if INJECTION_RE.search(sent):
                continue                  # instruction-like text is never turned into an obligation
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
            if kind == "PROMISE_BY_USER":
                requesting, responsible = (recipient_addr or None), user or None
            elif kind == "PROMISE_BY_OTHER":
                requesting, responsible = user or None, sender_addr or None
            elif kind == "REQUEST_TO_USER":
                requesting, responsible = sender_addr or None, user or None
            else:                                           # a dependency names no owner the mail can prove
                requesting, responsible = sender_addr or None, None
            state = "RESOLVED" if (requesting and responsible) else ("UNRESOLVED" if responsible is None else "PARTIAL")
            d = resolve_deadline_detail(sent, m["sent_at"], tz_name)
            out.append({"source_message_id": m["id"], "kind": kind, "required_action": action[:300], "evidence_text": sent[:500],
                        "deadline_at": d["deadline_at"], "deadline_confidence": d["deadline_confidence"], "review_reason": d["review_reason"],
                        "deadline_phrase": d["phrase"], "deadline_timezone": d["timezone"], "deadline_explicit": d["explicit"],
                        "requesting_person": requesting, "responsible_person": responsible, "ownership_state": state,
                        "obligation_key": obligation_key(action), "sent_at": m["sent_at"]})
    return out


def triage_thread(messages: list[dict], user_address: str, tz_name="UTC") -> dict:
    """Classify a thread from its messages. Returns category, priority, confidence, reasons and evidence sentences."""
    user = (user_address or "").lower()
    last = max(messages, key=lambda m: m["sent_at"])
    text = " ".join(m.get("body", "") for m in messages)
    reasons, evidence = [], []
    # Instruction-like text inside an email is untrusted data. It never changes permissions; it is routed to a human.
    injected = [sent for m in messages for sent in sentences(m.get("body", "")) if INJECTION_RE.search(sent)]
    if injected:
        return _decision("UNCERTAIN_HUMAN_REVIEW", "MEDIUM", 0.5,
                         ["the message contains instruction-like text; it is treated as data and needs your review"], [injected[0][:300]], messages)
    if AUTOMATED_SENDER.search(last.get("sender", "")) or AUTOMATED_BODY.search(text):
        return _decision("AUTOMATED_LOW_PRIORITY", "LOW", 0.9, ["automated sender or unsubscribe footer"], [last.get("sender", "")], messages)
    from_user_last = bool(user) and user in last.get("sender", "").lower()
    commitments = extract_commitments(messages, user_address, tz_name)
    # a deadline the USER must act on drives the deadline category; a deadline on someone else's promise is 'waiting'
    deadlines = [c for c in commitments if c["deadline_at"] and c["kind"] in ("REQUEST_TO_USER", "DEPENDENCY")]
    ambiguous = [c for c in commitments if c["deadline_confidence"] == "AMBIGUOUS"]
    questions = [s for s in sentences(last.get("body", "")) if s.endswith("?")]
    if ambiguous:
        return _decision("UNCERTAIN_HUMAN_REVIEW", "MEDIUM", 0.55, ["a deadline is ambiguous: " + ambiguous[0]["review_reason"]],
                         [ambiguous[0]["evidence_text"]], messages)
    if deadlines and not from_user_last:
        d = min(deadlines, key=lambda c: c["deadline_at"])
        return _decision("DEADLINE_TIME_SENSITIVE", "HIGH", 0.85, ["an explicit deadline is stated in the message"], [d["evidence_text"]], messages)
    request_texts = {c["evidence_text"] for c in commitments if c["kind"] == "REQUEST_TO_USER"}
    questions = [q for q in questions if q not in request_texts]       # a request phrased as a question is a request, not an open question
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
