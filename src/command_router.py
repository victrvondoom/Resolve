"""Deterministic interpretation of spoken/typed commands (pure, no I/O, no model).

interpret(text, state, source) maps an utterance to ONE typed command with resolved stable ids and a digest that binds
the interpretation to the exact state it was made against (mission id + intent revision, referenced document ids and
order). The server recomputes the digest at confirmation time; if the world moved (stale revision, reordered
documents, different mission) the digests differ and the confirmation is refused. Nothing here executes anything.

Anything not clearly recognised is UNKNOWN: a recognizer's uncertain text must never become an action by guessing.
"""
from __future__ import annotations

import hashlib
import json
import re

MAX_TEXT = 2000
COMMANDS = ("START_MISSION", "PAUSE", "RESUME", "CANCEL", "REVISE_DOCUMENT", "DELETE_REPORT", "SAVE_SKILL", "RUN_SKILL",
            "RESUME_PROJECT", "READ_RESULT", "UNKNOWN")
STEPS = ("read_brief", "analyze_code", "inspect_web", "architecture", "report", "print")
ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
            "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5, "6th": 6, "7th": 7, "8th": 8, "9th": 9, "10th": 10}
FILLER = re.compile(r"\b(um+|uh+|er+|erm|hmm+|like,|you know,|please)\b", re.I)
WAKE = re.compile(r"^\s*(hey\s+|ok(ay)?\s+)?resolve\b[\s,:;.!-]*", re.I)
ACTIVE_STATES = {"RUNNING", "AWAITING_APPROVAL", "COMPOSING"}


def normalise(text: str) -> tuple[str, list[str]]:
    warnings = []
    t = text if isinstance(text, str) else ""
    if len(t) > MAX_TEXT:
        t = t[:MAX_TEXT]
        warnings.append(f"input truncated to {MAX_TEXT} characters")
    t = re.sub(r"^[\s,.;:!-]*(?:(?:um+|uh+|er+|erm|hmm+)\b[\s,.;:!-]*)+", "", t.strip(), flags=re.I)
    t = WAKE.sub("", t)
    t = FILLER.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip().lower().rstrip(" .!?,;:")
    return t, warnings


def _sentences(t: str) -> list[str]:
    return [s.strip(" ,;:-") for s in re.split(r"[.!?]+", t) if s.strip(" ,;:-")]


def _digest(command: str, args: dict, bound: dict, norm: str) -> str:
    payload = {"command": command, "args": args, "bound": bound, "text": norm}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def _result(command, args, summary, norm, state, source, *, confirm, warnings, ambiguity=None, bound=None):
    bound = bound if bound is not None else _bound(state, command, args)
    return {"command": command, "args": args, "summary": summary, "requires_confirmation": bool(confirm),
            "ambiguity": ambiguity or [], "warnings": warnings, "source": source, "digest": _digest(command, args, bound, norm)}


def _bound(state: dict, command: str, args: dict) -> dict:
    """State the interpretation depends on: changes to any of it must change the digest."""
    am, lo, lm = state.get("active_mission"), state.get("latest_output_mission"), state.get("last_mission")
    b: dict = {}
    if am:
        b["active"] = {"id": am.get("id"), "rev": am.get("intent_revision"), "state": am.get("state"), "paused": bool(am.get("paused")), "doc": am.get("source_doc_id")}
    if command in ("DELETE_REPORT", "READ_RESULT") and lo:
        b["output"] = {"id": lo.get("id"), "doc": lo.get("output_doc_id"), "sha": lo.get("output_sha256")}
    if command in ("SAVE_SKILL", "RESUME_PROJECT") and lm:
        b["last"] = {"id": lm.get("id"), "state": lm.get("state")}
    if command == "REVISE_DOCUMENT":
        b["documents"] = [d.get("id") for d in state.get("documents", [])]   # order matters: "second" depends on it
        b["doc_sha"] = {d.get("id"): d.get("sha256") for d in state.get("documents", [])}
    if command == "RUN_SKILL":
        b["skills"] = sorted((s.get("id"), s.get("name")) for s in state.get("skills", []))
    if command == "START_MISSION":
        b["documents"] = [d.get("id") for d in state.get("documents", [])]
    return b


def _detect_steps(t: str) -> list[str]:
    steps = []
    if re.search(r"\b(brief|spec(ification)?|requirements?|document)\b", t):
        steps.append("read_brief")
    if re.search(r"\b(code|repo(sitory)?|project|source|codebase|implementation)\b", t) or re.search(r"\banaly[sz]e\b", t):
        steps.append("analyze_code")
    if re.search(r"\b(web ?app(lication)?|website|web page|webpage|browser|console|errors?|page)\b", t):
        steps.append("inspect_web")
    if re.search(r"\b(architecture|diagram|design)\b", t):
        steps.append("architecture")
    if re.search(r"\b(report|pdf|generate|document)\b", t):
        steps.append("report")
    if re.search(r"\bprint(ing|able)?\b", t):
        steps.append("print")
    return [s for s in STEPS if s in steps]


def interpret(text: str, state: dict, *, source: str = "text") -> dict:
    state = state or {}
    norm, warnings = normalise(text)
    am = state.get("active_mission")
    docs = state.get("documents") or []

    def unknown(msg):
        return _result("UNKNOWN", {}, msg, norm, state, source, confirm=False, warnings=warnings + [msg], bound={})

    if not norm:
        return unknown("nothing was recognised")
    first = _sentences(norm)[0] if _sentences(norm) else norm
    has_stop = bool(re.match(r"^(stop|cancel|abort)\b", first)) or bool(re.search(r"\bcancel (the |this |my )?(mission|task|job|run)\b", norm))
    revise_lead = has_stop or bool(re.match(r"^(wait|hold on)\b", first))

    # --- revise: "stop. use the second report instead"
    m = re.search(r"\b(?:use|switch to|take|pick|choose|go with)\s+(?:the\s+)?(?:(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|1st|2nd|3rd|4th|5th|6th|7th|8th|9th|10th|previous|other|another|last|latest|newest|oldest)\s+)?(?:report|document|brief|file|pdf)(?:\s+instead)?\b", norm)
    if m and (revise_lead or "instead" in norm):
        if not am:
            return unknown("there is no active mission to change")
        word = m.group(1)
        if word in ORDINALS:
            n = ORDINALS[word]
            if n > len(docs):
                return unknown(f"you asked for document number {n} but only {len(docs)} are available")
            d = docs[n - 1]
            args = {"mission_id": am["id"], "intent_revision": am.get("intent_revision"), "document_id": d["id"], "document_name": d.get("name"), "ordinal": n}
            return _result("REVISE_DOCUMENT", args, f"Stop the current work and switch the brief to document {n}: {d.get('name')}.",
                           norm, state, source, confirm=True, warnings=warnings)
        if word in ("last", "oldest"):
            if not docs:
                return unknown("there are no documents to choose from")
            d, n = docs[-1], len(docs)
            args = {"mission_id": am["id"], "intent_revision": am.get("intent_revision"), "document_id": d["id"], "document_name": d.get("name"), "ordinal": n}
            return _result("REVISE_DOCUMENT", args, f"Stop the current work and switch the brief to the last document: {d.get('name')}.", norm, state, source, confirm=True, warnings=warnings)
        if word in ("latest", "newest"):
            if not docs:
                return unknown("there are no documents to choose from")
            d = docs[0]
            args = {"mission_id": am["id"], "intent_revision": am.get("intent_revision"), "document_id": d["id"], "document_name": d.get("name"), "ordinal": 1}
            return _result("REVISE_DOCUMENT", args, f"Stop the current work and switch the brief to the newest document: {d.get('name')}.", norm, state, source, confirm=True, warnings=warnings)
        # "the other / previous / another report": only unambiguous with exactly two documents
        current = am.get("source_doc_id")
        others = [d for d in docs if d["id"] != current]
        if len(others) == 1:
            d = others[0]
            args = {"mission_id": am["id"], "intent_revision": am.get("intent_revision"), "document_id": d["id"], "document_name": d.get("name"), "ordinal": docs.index(d) + 1}
            return _result("REVISE_DOCUMENT", args, f"Stop the current work and switch the brief to the other document: {d.get('name')}.", norm, state, source, confirm=True, warnings=warnings)
        cands = [{"document_id": d["id"], "name": d.get("name"), "ordinal": docs.index(d) + 1} for d in others]
        return _result("REVISE_DOCUMENT", {"mission_id": am["id"], "intent_revision": am.get("intent_revision")}, "Which document should I use?", norm, state, source,
                       confirm=True, warnings=warnings + ["ambiguous document reference"], ambiguity=[{"field": "document_id", "candidates": cands}])

    # --- delete the generated report (always confirmed)
    if re.search(r"\bdelete\b.*\b(generated |output |final |latest |last )?(report|pdf)\b", norm) and not re.search(r"\b(everything|all (my )?(files|data|documents|reports))\b", norm):
        lo = state.get("latest_output_mission")
        if not lo or not lo.get("output_doc_id"):
            return unknown("there is no generated report to delete")
        args = {"mission_id": lo["id"], "document_id": lo["output_doc_id"], "output_sha256": lo.get("output_sha256")}
        return _result("DELETE_REPORT", args, "Permanently delete the generated report and its mission output.", norm, state, source, confirm=True, warnings=warnings)

    # --- save skill
    if re.search(r"\b(save|store|record|remember)\b.*\b(process|workflow|steps?|this|mission)\b.*\b(as )?(a )?(skill|macro)\b", norm) or re.search(r"\bsave (this )?(as )?(a )?(skill|macro)\b", norm):
        target = am or state.get("last_mission")
        if not target:
            return unknown("there is no mission to save as a skill")
        nm = re.search(r"\b(?:called|named|name it)\s+([a-z0-9 _-]{1,60})", norm)
        raw = nm.group(1).strip() if nm else "saved process"
        name = re.sub(r"[^A-Za-z0-9 _-]", "", raw).strip()[:40] or "saved process"
        return _result("SAVE_SKILL", {"mission_id": target["id"], "name": name}, f"Save this mission's process as the skill “{name}”.", norm, state, source, confirm=False, warnings=warnings)

    # --- run skill
    m = re.search(r"\b(?:run|start|use|play)\s+(?:the\s+)?(?:skill|macro)\s+(?:called\s+|named\s+)?([a-z0-9 _-]{1,60})", norm)
    if m:
        want = m.group(1).strip()
        skills = state.get("skills") or []
        exact = [s for s in skills if (s.get("name") or "").casefold() == want]
        cands = exact or [s for s in skills if want and (want in (s.get("name") or "").casefold())]
        if len(cands) == 1:
            return _result("RUN_SKILL", {"skill_id": cands[0]["id"], "name": cands[0]["name"]}, f"Run the skill “{cands[0]['name']}”.", norm, state, source, confirm=True, warnings=warnings)
        if not cands:
            return unknown(f"no skill named “{want}”")
        return _result("RUN_SKILL", {}, "Which skill do you mean?", norm, state, source, confirm=True, warnings=warnings + ["ambiguous skill name"],
                       ambiguity=[{"field": "skill_id", "candidates": [{"skill_id": s["id"], "name": s["name"]} for s in cands]}])

    # --- resume project / read result
    if re.search(r"\bresume\b.*\b(last|previous|my)\b.*\b(project|work|workspace|mission)\b", norm):
        lm = state.get("last_mission")
        if not lm:
            return unknown("there is no earlier project to resume")
        return _result("RESUME_PROJECT", {"mission_id": lm["id"]}, "Reopen your last project.", norm, state, source, confirm=False, warnings=warnings)
    if re.search(r"\bread\b.*\b(result|report|summary|output)\b", norm) and not re.search(r"\b(inspect|analy[sz]e|generate|create)\b", norm):
        lo = state.get("latest_output_mission") or state.get("last_mission")
        if not lo:
            return unknown("there is no result to read")
        return _result("READ_RESULT", {"mission_id": lo["id"]}, "Read out the current result.", norm, state, source, confirm=False, warnings=warnings)

    # --- control words (short utterances only, so long instructions containing "stop" are not hijacked)
    short = len(norm.split()) <= 8
    if short and has_stop:
        if not am:
            return unknown("there is no active mission to cancel")
        return _result("CANCEL", {"mission_id": am["id"], "intent_revision": am.get("intent_revision")}, "Cancel the current mission.", norm, state, source, confirm=True, warnings=warnings)
    if short and re.match(r"^(pause|hold on|hold it|freeze)\b", norm):
        if not am:
            return unknown("there is no active mission to pause")
        return _result("PAUSE", {"mission_id": am["id"], "intent_revision": am.get("intent_revision")}, "Pause the current mission.", norm, state, source, confirm=False, warnings=warnings)
    if short and re.match(r"^(resume|continue|carry on|go on|unpause)\b", norm):
        if not am:
            return unknown("there is no active mission to resume")
        return _result("RESUME", {"mission_id": am["id"], "intent_revision": am.get("intent_revision")}, "Resume the paused mission.", norm, state, source, confirm=False, warnings=warnings)

    # --- start a mission (long instruction with task vocabulary)
    if re.search(r"\b(brief|report|architecture|inspect|analy[sz]e|generate|diagram|specification)\b", norm) and len(norm.split()) >= 4:
        if re.search(r"\b(ignore|disregard|forget)\b.*\b(rules|instructions|previous|above)\b", norm):
            return unknown("the request tries to override the assistant's rules, so it was not interpreted as a command")
        steps = _detect_steps(norm)
        args = {"steps": steps, "text": norm, "document_candidates": [d["id"] for d in docs][:10]}
        return _result("START_MISSION", args, "Start a mission: " + ", ".join(s.replace("_", " ") for s in steps) + ".", norm, state, source,
                       confirm=(source == "voice"), warnings=warnings)

    return unknown("the command was not recognised")
