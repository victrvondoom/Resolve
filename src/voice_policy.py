"""Natural-language policy parser for the inbox operator (voice or typed input).

Dictated or typed instructions become structured, inspectable rules. Nothing here changes behaviour by itself:
- high-impact rules (anything that could cause an email to be sent, or that changes all messages) require confirmation;
- instructions the parser does not recognise become UNKNOWN and are never applied;
- a rule that contradicts an active rule is reported as a conflict instead of being silently overridden.
Wispr Flow is an operating-system dictation tool: it types into this text box. RESOLVE has no Wispr API integration.
"""
from __future__ import annotations

import re

RULE_TYPES = ("PRIORITY_RAISE", "REVIEW_FIRST", "NO_DRAFT", "TONE", "SIGNOFF", "APPROVAL_REQUIRED_TO_SEND",
              "CAPTURE_COMMITMENTS", "SHOW_WAITING", "VERIFY_BEFORE_COMPLETE", "UNKNOWN")
HIGH_IMPACT = {"APPROVAL_REQUIRED_TO_SEND", "CAPTURE_COMMITMENTS", "NO_DRAFT"}
SCOPE_WORDS = {"project collaborators": "SENDER_GROUP:collaborators", "collaborator": "SENDER_GROUP:collaborators",
               "newsletters": "SENDER_GROUP:newsletters", "automated": "SENDER_GROUP:automated", "notifications": "SENDER_GROUP:automated"}


def parse_instruction(text: str) -> list[dict]:
    """Split on sentence boundaries and parse each sentence into one rule. Unknown sentences become UNKNOWN rules."""
    out = []
    for raw in re.split(r"(?<=[.!?])\s+|\n+", (text or "").strip()):
        sent = raw.strip()
        if not sent:
            continue
        out.append(_parse_one(sent))
    return out


def _scope(sent: str) -> str:
    low = sent.lower()
    for phrase, scope in SCOPE_WORDS.items():
        if phrase in low:
            return scope
    return "ALL_MESSAGES"


def _parse_one(sent: str) -> dict:
    low = sent.lower()
    rule = {"instruction": sent, "type": "UNKNOWN", "scope": _scope(sent), "priority": None, "value": None, "explanation": ""}
    if re.search(r"\b(never|do not|don't) send\b.*\bwithout\b.*\b(approv|my ok|showing me)\b|\bnever send\b.*\bwithout\b", low) or re.search(r"\bdo not send anything without\b", low):
        rule.update(type="APPROVAL_REQUIRED_TO_SEND", explanation="every send needs your explicit approval of the exact draft")
    elif re.search(r"\b(high priority|top|at the top|prioriti[sz]e|raise (the )?priority)\b", low):
        rule.update(type="PRIORITY_RAISE", priority="HIGH", explanation="raise the priority of matching messages")
    elif re.search(r"\b(deadline|due date)\b.*\b(review|above|first|before)\b|\breview first\b|\bput actual deadlines above\b", low):
        rule.update(type="REVIEW_FIRST", explanation="surface matching messages for review before ordinary ones")
    elif re.search(r"\bdo not draft\b|\bdon'?t draft\b|\bnever draft\b", low):
        rule.update(type="NO_DRAFT", explanation="do not prepare reply drafts for matching messages")
    elif re.search(r"\btone\b|\bfriendly\b|\bformal\b|\bcasual\b|\bprofessional\b", low):
        tone = "friendly but professional" if "friendly" in low and "professional" in low else ("friendly" if "friendly" in low else ("formal" if "formal" in low else ("casual" if "casual" in low else "professional")))
        rule.update(type="TONE", value=tone, explanation=f"write drafts in a {tone} tone")
    elif re.search(r"\bsign[- ]?off\b", low):
        m = re.search(r"sign[- ]?off (?:to |as |should be )?[\"']?([A-Za-z ,]+?)[\"']?[.]?$", sent, re.I)
        rule.update(type="SIGNOFF", value=(m.group(1).strip() if m else None), explanation="use this sign-off in drafts")
    elif re.search(r"\b(if i promise|track every commitment|create a commitment|commitments? i make)\b", low):
        rule.update(type="CAPTURE_COMMITMENTS", explanation="create a commitment when you make a promise in a message")
    elif re.search(r"\bwaiting (for|on)\b|\bwhat i am waiting\b|\bwhat i'm waiting\b", low):
        rule.update(type="SHOW_WAITING", explanation="show the obligations other people owe you")
    elif re.search(r"\bdo not mark\b.*\bcompleted\b|\bunless you can verify\b|\bonly (mark|count) .* verified\b", low):
        rule.update(type="VERIFY_BEFORE_COMPLETE", explanation="a commitment is complete only with verification evidence")
    return rule


def requires_confirmation(rules: list[dict]) -> bool:
    return any(r["type"] in HIGH_IMPACT or r["type"] == "UNKNOWN" or r["scope"] == "ALL_MESSAGES" and r["type"] in ("PRIORITY_RAISE", "REVIEW_FIRST")
               for r in rules)


def conflicts(new_rules: list[dict], active_rules: list[dict]) -> list[str]:
    """Contradictions with active rules. A rule that would make a send happen without approval conflicts with the approval rule."""
    found = []
    approval_on = any(r["type"] == "APPROVAL_REQUIRED_TO_SEND" for r in active_rules)
    for r in new_rules:
        if approval_on and r["type"] == "UNKNOWN" and re.search(r"\bsend\b.*\bwithout\b", r["instruction"], re.I):
            found.append(f"'{r['instruction']}' conflicts with the active rule that every send needs your approval")
        for a in active_rules:
            if r["type"] == a["type"] and r["scope"] == a["scope"] and r.get("value") != a.get("value") and r["type"] in ("TONE", "SIGNOFF"):
                found.append(f"{r['type']} for {r['scope']} differs from the active rule '{a['instruction']}'")
    return found
