"""General computer-use task executor over the typed tool registry.

A small, DOCUMENTED set of task types is supported (this is a bounded rule-based planner, not an unrestricted agent):

  overdue_tasks   "Open Project Atlas and show my overdue tasks."
  latest_report   "Find the latest project report and summarize it."          (extractive summary, labelled as such)
  update_task     "Update the 'Fix invoice rounding' task to done."           (consequential: proposal -> exact-digest approval)
  save_report     "Open the research portal, find the correct document and save a report to my workspace."  (consequential)
  find_person     "Find Alex from Finance, not Alex from Sales."              (refuses rather than guesses when ambiguous)

Every page action goes through ToolRegistry.call (validation, capability, timeout, evidence). Targets are resolved by
visible context and bound by stable ids and versions, never by display text alone. A consequential task returns a
PROPOSAL with a digest of its exact arguments and the target's observed version; it executes only when that digest is
presented back, and only if the live target still matches (otherwise STALE_TARGET and nothing is written). Success is
reported only when an independent verifier (reading the application's own database, not the page) agrees.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Callable

from tool_registry import ToolContext, ToolRegistry

BROWSER_CAPS = frozenset({"browser:navigate", "browser:interact", "evidence:write"})


def browser_services(session) -> dict:
    """Bind the registry's browser.* tools to a REAL CloudBrowserSession (DOM-level actions, not cursor movement)."""
    return {
        "browser.navigate": lambda c, a: session.navigate(a["url"]),
        "browser.click": lambda c, a: session.click_selector(a["selector"]),
        "browser.type": lambda c, a: session.type_selector(a["selector"], a["text"]),
        "browser.scroll": lambda c, a: session.scroll(0, a["dy"]),
        "browser.inspect": lambda c, a: session.inspect(),
        "browser.capture": lambda c, a: {k: v for k, v in session.capture_frame().items() if k != "frame"} | {"captured": True},
        "browser.download": lambda c, a: session.download_via(a["selector"]),
        "browser.find": lambda c, a: session.find(a.get("text"), a.get("role"), a.get("selector")),
        "browser.select": lambda c, a: session.select_option(a["selector"], a["value"]),
        "browser.wait": lambda c, a: session.wait_for(a.get("selector"), a.get("text"), a.get("timeout_ms", 8000)),
        "browser.extract": lambda c, a: session.extract(a.get("selector")),
        "browser.verify": lambda c, a: session.verify_state(a.get("selector"), a.get("text"), a.get("url_contains")),
        "browser.close": lambda c, a: (session.close(), {"closed": True})[1],
    }


def digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class TaskRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class ComputerTaskRunner:
    def __init__(self, registry: ToolRegistry, ctx: ToolContext, bases: dict, verifier: Callable[[str, dict], dict], cancel: Callable[[], bool] = lambda: False):
        self.reg, self.ctx, self.bases, self.verifier, self.cancel = registry, ctx, bases, verifier, cancel
        self.steps: list[dict] = []

    # ---- tool plumbing
    def call(self, name: str, **args) -> dict:
        if self.cancel():
            raise TaskRefused("CANCELLED", "the mission was cancelled")
        r = self.reg.call(self.ctx, name, args, cancel=self.cancel)
        self.steps.append({"tool": name, "ok": r.ok, "error": (r.error or {}).get("code"), "evidence_ref": r.evidence_ref})
        if not r.ok:
            raise TaskRefused(r.error["code"], r.error["message"])
        return r.data

    def _done(self, task: str, **kw) -> dict:
        return {"task": task, "steps": self.steps, **kw}

    # ---- planner
    def classify(self, text: str) -> tuple[str, dict]:
        t = " ".join(text.split())
        low = t.lower()
        m = re.search(r"project\s+(\w+)", t, re.I)
        if "overdue" in low and m:
            return "overdue_tasks", {"project": m.group(1).capitalize()}
        m = re.search(r"update (?:the )?(?:selected )?task\s+['\"“]?([^'\"”]+?)['\"”]?\s+(?:to|as)\s+(todo|in progress|in_progress|done)\b", t, re.I)
        if m:
            return "update_task", {"title": m.group(1).strip(), "status": m.group(2).lower().replace(" ", "_"), "project": (re.search(r"project\s+(\w+)", t, re.I) or [None, "Atlas"])[1].capitalize()}
        if "save" in low and "report" in low and ("research" in low or "portal" in low or "document" in low):
            return "save_report", {"topic": "report"}
        if low.startswith("find") and "report" in low:
            return "latest_report", {"term": "report"}
        m = re.search(r"find\s+(\w+)\s+from\s+(\w+)(?:\s*,?\s*not\s+\w+\s+from\s+(\w+))?", t, re.I)
        if m:
            return "find_person", {"first": m.group(1), "department": m.group(2), "exclude": (m.group(3) or "")}
        m = re.fullmatch(r"find\s+(\w+)\s*[.!?]?", t, re.I)
        if m:   # no department given: resolved only if exactly one person matches, otherwise the user is asked
            return "find_person", {"first": m.group(1), "department": "", "exclude": ""}
        raise TaskRefused("UNSUPPORTED_TASK", "this task type is not supported; supported: overdue tasks, latest report, update task, save report, find person")

    def run(self, text: str, approved_digest: str | None = None, proposal: dict | None = None) -> dict:
        task, p = self.classify(text)
        try:
            return getattr(self, f"_{task}")(p, approved_digest, proposal)
        except TaskRefused as e:
            return self._done(task, ok=False, status="REFUSED", error={"code": e.code, "message": e.message})

    # ---- tasks
    def _overdue_tasks(self, p, *_):
        self.call("browser.navigate", url=f"{self.bases['pm']}/pm/?project={p['project']}")
        self.call("browser.wait", selector="#tasks tr", timeout_ms=8000)
        rows = self.call("browser.extract", selector="#tasks")["rows"]
        hits = [r for r in rows if len(r) > 2 and "overdue" in r[2].lower()]
        overdue = sorted(r[0] for r in hits)
        truth = self.verifier("overdue_tasks", {"project": p["project"]})
        ok = overdue == sorted(truth["titles"])
        page_rows = sorted(({"title": r[0], "owner": r[1], "due": r[2].lower().replace("overdue", "").strip()} for r in hits), key=lambda r: r["title"])   # as read from the page
        return self._done("overdue_tasks", ok=ok, status="CONFIRMED" if ok else "MISMATCH", project=p["project"], overdue=overdue, overdue_rows=page_rows, verified_against="application database")

    def _latest_report(self, p, *_):
        doc = self._pick_latest_report()
        self.call("browser.navigate", url=f"{self.bases['research']}/research/doc/{doc['id']}")
        self.call("browser.wait", selector="#body")
        body = self.call("browser.extract", selector="#body")["text"]
        summary = " ".join(re.split(r"(?<=[.!?])\s+", body)[:3])
        truth = self.verifier("document", {"id": doc["id"]})
        ok = truth["version"] == doc["version"] and truth["is_latest_of_family"] and truth["body"].startswith(body[:40])
        return self._done("latest_report", ok=ok, status="CONFIRMED" if ok else "MISMATCH", document={"id": doc["id"], "title": doc["title"], "version": doc["version"], "updated": doc["updated"]}, summary=summary, summary_kind="extractive (first sentences of the document)")

    def _pick_latest_report(self) -> dict:
        self.call("browser.navigate", url=f"{self.bases['research']}/research/?q=report")
        self.call("browser.wait", selector="tbody tr")
        found = self.call("browser.find", role="row", text="v")["matches"]
        docs = []
        for m in found:
            h = re.search(r"(.+?)\s+v(\d+)\s+(\d{4}-\d{2}-\d{2})", m["text"].replace("\t", " "))
            if h:
                href = self.call("browser.find", selector=f'{m["handle"]} a')["matches"]
                docs.append({"title": h.group(1).strip(), "version": int(h.group(2)), "updated": h.group(3), "handle": m["handle"], "link": href[0]["handle"] if href else None})
        if not docs:
            raise TaskRefused("NOT_FOUND", "no report found")
        best = max(docs, key=lambda d: d["updated"])
        self.call("browser.click", selector=best["link"])
        self.call("browser.wait", selector="#body")
        v = self.call("browser.verify", url_contains="/research/doc/")
        m = re.search(r"/research/doc/(\d+)", self.call("browser.inspect")["url"])
        if not v["verified"] or not m:
            raise TaskRefused("NAVIGATION_FAILED", "could not open the chosen document")
        best["id"] = int(m.group(1))
        return best

    def _update_task(self, p, approved_digest, proposal):
        self.call("browser.navigate", url=f"{self.bases['pm']}/pm/?project={p['project']}")
        self.call("browser.wait", selector="#tasks tr")
        rows = [r for r in self.call("browser.find", role="row", text=p["title"])["matches"] if "<th" not in r["text"]]
        if len(rows) != 1:
            raise TaskRefused("AMBIGUOUS_TARGET" if rows else "NOT_FOUND", f"{len(rows)} tasks match '{p['title']}'; not guessing")
        handle = rows[0]["handle"]
        meta = self.call("browser.extract", selector=handle)
        info = self.verifier("task_by_title", {"title": p["title"], "project": p["project"]})
        tid, ver = info["id"], info["version"]   # the proposal binds the stable id AND the observed version
        prop = {"task": "update_task", "task_id": tid, "observed_version": ver, "new_status": p["status"], "app": "controlled:pm"}
        prop["digest"] = digest(prop)
        if approved_digest is None:
            return self._done("update_task", ok=True, status="AWAITING_APPROVAL", proposal=prop, current_row=meta["rows"][0] if meta.get("rows") else rows[0]["text"], note="nothing has been changed; approve this exact digest to proceed")
        if approved_digest != prop["digest"] or (proposal and proposal != prop):
            raise TaskRefused("APPROVAL_MISMATCH" if approved_digest != prop["digest"] else "STALE_TARGET", "the approved digest does not match the task as it is now: nothing was written")
        self.call("browser.select", selector=f"{handle} select", value=p["status"])
        self.call("browser.click", selector=f"{handle} button")
        self.call("browser.wait", text="Saved", timeout_ms=6000)
        truth = self.verifier("task", {"id": tid})
        ok = truth["status"] == p["status"] and truth["version"] == ver + 1
        return self._done("update_task", ok=ok, status="CONFIRMED" if ok else "UNCERTAIN", effect={"task_id": tid, "status": truth["status"], "version": truth["version"]}, verified_against="application database")

    def _save_report(self, p, approved_digest, proposal):
        doc = self._pick_latest_report()
        body = self.call("browser.extract", selector="#body")["text"]
        summary = " ".join(re.split(r"(?<=[.!?])\s+", body)[:3])
        prop = {"task": "save_report", "source_doc_id": doc["id"], "source_version": doc["version"], "summary_sha256": hashlib.sha256(summary.encode()).hexdigest(), "app": "controlled:research"}
        prop["digest"] = digest(prop)
        if approved_digest is None:
            return self._done("save_report", ok=True, status="AWAITING_APPROVAL", proposal=prop, document={"id": doc["id"], "title": doc["title"], "version": doc["version"]}, summary=summary)
        if approved_digest != prop["digest"]:
            raise TaskRefused("APPROVAL_MISMATCH", "the document or summary changed since approval: nothing was saved")
        self.call("browser.type", selector="#report-body", text=summary)
        self.call("browser.click", selector="#save-report")
        self.call("browser.wait", text="Saved report", timeout_ms=6000)
        truth = self.verifier("reports", {})
        mine = [r for r in truth["reports"] if r["source_doc_id"] == doc["id"] and r["source_version"] == doc["version"] and r["body"] == summary]
        ok = len(mine) == 1
        return self._done("save_report", ok=ok, status="CONFIRMED" if ok else "UNCERTAIN", effect={"report_id": mine[0]["id"] if mine else None, "source_doc_id": doc["id"], "source_version": doc["version"]}, verified_against="application database")

    def _find_person(self, p, *_):
        self.call("browser.navigate", url=f"{self.bases['crm']}/crm/?q={p['first']}")
        self.call("browser.wait", selector="li")
        items = self.call("browser.find", role="listitem")["matches"]
        want = p["department"].lower()
        hits = [i for i in items if i["text"].split()[0].lower() == p["first"].lower() and want in i["context"].lower()]
        if len(hits) > 1:   # ask instead of guessing: list what the page shows for each candidate (read through the registry)
            cands = []
            for h in hits:
                name = self.call("browser.find", selector=f"{h['handle']} a")["matches"]
                dept = self.call("browser.find", selector=f"{h['handle']} span")["matches"]
                cands.append({"name": name[0]["text"] if name else h["text"], "department": dept[0]["text"] if dept else ""})
            where = f" in {p['department']}" if p["department"] else ""
            return self._done("find_person", ok=False, status="NEEDS_CLARIFICATION", candidates=cands,
                              question=f"{len(hits)} people named {p['first']}{where}. Which one do you mean?", note="nothing was chosen; not guessing")
        if len(hits) != 1:
            raise TaskRefused("NOT_FOUND", f"{len(hits)} people match {p['first']}{' in ' + p['department'] if p['department'] else ''}; not guessing")
        link = self.call("browser.find", selector=f"{hits[0]['handle']} a")["matches"][0]
        self.call("browser.click", selector=link["handle"])
        self.call("browser.wait", selector="#card")
        card = self.call("browser.extract", selector="#card")["text"]
        cid = int(re.search(r"/crm/contact/(\d+)", self.call("browser.inspect")["url"]).group(1))
        truth = self.verifier("contact", {"id": cid})
        ok = (not want or truth["department"].lower() == want) and truth["name"].split()[0].lower() == p["first"].lower() and truth["email"] in card
        return self._done("find_person", ok=ok, status="CONFIRMED" if ok else "MISMATCH", person={"id": cid, "name": truth["name"], "department": truth["department"], "email": truth["email"]}, excluded_department=p["exclude"] or None)
