"""Brief -> analysis -> architecture -> verified PDF mission (the RESOLVE hero workflow).

Real worker graph (threads, with recorded start/end timestamps so concurrency is observable):

    DOCUMENT_READER ─┐
                     ├─> ARCHITECTURE_PLANNER ─> [user approval of plan digest] ─> REPORT_COMPOSER
    REPOSITORY_ANALYZER ┘                                                              │
                                              PRINT_PREPARER <─ EFFECT_VERIFIER <──────┘

Everything is tenant-scoped (user_id + workspace_id on every row and every document lookup).
The analysis is deterministic and rule-based (AST + regex). It is NOT an LLM and its output is
labelled DRAFT. CONFIRMED requires `verify_chain`: a verified link from authenticated user ->
intent revision -> source entity (id + sha256) -> approved plan digest -> output hash -> read-back.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import sqlite3
import textwrap
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from document_studio import DocumentError, DocumentStudio

ROOT = Path(__file__).resolve().parents[1]
PROJECTS_DIR = ROOT / "demo_data"
BUNDLED_PROJECTS = {"sample_project": PROJECTS_DIR / "sample_project"}  # allow-list: never arbitrary server paths
SAMPLE_BRIEF = PROJECTS_DIR / "project_brief.txt"
MAX_ACTIVE_PER_TENANT = 3
MAX_MISSIONS_PER_TENANT = 50
NODE_TIMEOUT_S = 30.0

CATEGORY_KEYWORDS = {
    "secrets": ("credential", "secret", "password", "api key", "token"),
    "validation": ("parameterized", "injection", "sanitiz", "validat"),
    "error-handling": ("error", "exception", "failure", "log"),
    "testing": ("test",),
    "maintainability": ("todo", "fixme", "tracked"),
}
SEVERITY = {"secrets": "HIGH", "validation": "HIGH", "error-handling": "MEDIUM", "testing": "MEDIUM", "maintainability": "LOW"}
FIX = {
    "secrets": "Load the value from an environment variable or secret manager; rotate the exposed value.",
    "validation": "Use parameterized queries (placeholders) for every database call.",
    "error-handling": "Catch specific exceptions, log them, and return an explicit error result.",
    "testing": "Add automated tests for this module before release.",
    "maintainability": "Resolve the item or link it to a tracked issue before release.",
}

TERMINAL = {"CONFIRMED", "FAILED", "CANCELLED"}


class MissionError(Exception):
    def __init__(self, message: str, status_code: int = 400, candidates: list | None = None):
        super().__init__(message)
        self.message, self.status_code, self.candidates = message, status_code, candidates


# ------------------------------------------------------------------ pure analysis helpers

def extract_requirements(brief_text: str) -> list[dict]:
    reqs = []
    for n, line in enumerate(brief_text.splitlines(), 1):
        m = re.match(r"^\s*(R\d+|REQ-\d+)[\s:.\-]+(.+)$", line.strip(), re.I)
        if m:
            reqs.append({"id": m.group(1).upper(), "text": m.group(2).strip(), "line": n})
    if not reqs:
        for n, line in enumerate(brief_text.splitlines(), 1):
            if re.search(r"\b(must|shall)\b", line, re.I):
                reqs.append({"id": f"L{n}", "text": line.strip(), "line": n})
    for r in reqs:
        low = r["text"].lower()
        r["category"] = next((c for c, kws in CATEGORY_KEYWORDS.items() if any(k in low for k in kws)), "general")
    return reqs


def analyze_project(project_dir: Path) -> dict:
    """Static analysis only; the project is parsed, never executed."""
    findings, modules, imports = [], [], {}
    py_files = sorted(p for p in project_dir.rglob("*.py") if "__pycache__" not in p.parts)
    names = {p.stem for p in py_files}
    test_names = {p.stem for p in py_files if p.stem.startswith("test_")}
    for path in py_files:
        rel = path.relative_to(project_dir).as_posix()
        src = path.read_text(encoding="utf-8", errors="replace")
        lines = src.splitlines()
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            findings.append({"category": "error-handling", "file": rel, "line": exc.lineno or 1, "rule": "SYNTAX_ERROR",
                             "snippet": (exc.text or "").strip()[:120]})
            continue
        if not path.stem.startswith("test_"):
            modules.append(path.stem)
            imports[path.stem] = sorted({(a.name if isinstance(n, ast.Import) else (n.module or "")).split(".")[0]
                                         for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in (n.names if isinstance(n, ast.Import) else [n])} & names)
            if f"test_{path.stem}" not in test_names:
                findings.append({"category": "testing", "file": rel, "line": 1, "rule": "NO_TESTS", "snippet": f"no test_{path.stem}.py found"})
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                for t in node.targets:
                    if isinstance(t, ast.Name) and re.search(r"(secret|password|api_?key|token)", t.id, re.I) and len(node.value.value) >= 8:
                        findings.append({"category": "secrets", "file": rel, "line": node.lineno, "rule": "HARDCODED_SECRET", "snippet": f"{t.id} = <redacted literal>"})
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("execute", "executemany") and node.args:
                a = node.args[0]
                if isinstance(a, (ast.JoinedStr, ast.BinOp)):
                    findings.append({"category": "validation", "file": rel, "line": node.lineno, "rule": "SQL_STRING_BUILDING", "snippet": lines[node.lineno - 1].strip()[:120]})
            if isinstance(node, ast.ExceptHandler) and (node.type is None or (len(node.body) == 1 and isinstance(node.body[0], ast.Pass))):
                findings.append({"category": "error-handling", "file": rel, "line": node.lineno, "rule": "SILENT_EXCEPT", "snippet": lines[node.lineno - 1].strip()[:120]})
        for i, text in enumerate(lines, 1):
            if re.search(r"#\s*(TODO|FIXME)\b", text):
                findings.append({"category": "maintainability", "file": rel, "line": i, "rule": "OPEN_TODO", "snippet": text.strip()[:120]})
    findings.sort(key=lambda f: (f["file"], f["line"], f["rule"]))
    return {"modules": modules, "imports": imports, "findings": findings, "files_scanned": len(py_files)}


def build_plan(requirements: list[dict], analysis: dict, source: dict, title: str, intent: dict | None = None) -> dict:
    issues = []
    for n, f in enumerate(analysis["findings"], 1):
        req = next((r for r in requirements if r["category"] == f["category"]), None)
        issues.append({"id": f"ISS-{n}", "severity": SEVERITY.get(f["category"], "LOW"), **f,
                       "requirement": ({"id": req["id"], "text": req["text"], "brief_line": req["line"]} if req else None),
                       "fix": FIX.get(f["category"], "Review manually."),
                       "citation": f"{f['file']}:{f['line']}" + (f" violates {req['id']} (brief line {req['line']})" if req else " (no matching brief requirement)")})
    graph = build_architecture(analysis, issues)
    mermaid = graph_to_mermaid(graph)
    validate_mermaid(mermaid)
    tasks = [{"id": f"T{i}", "title": f"Fix {x['id']}: {x['fix']}", "depends_on": [], "category": x["category"]} for i, x in enumerate(issues, 1)]
    tasks.append({"id": f"T{len(tasks) + 1}", "title": "Re-run analyzer and the new test suite; every ISS must clear", "depends_on": [t["id"] for t in tasks], "category": "verification"})
    plan = {
        "title": title, "status": "DRAFT", "intent": intent or {"revision": 1, "text": ""}, "method": "deterministic static analysis (AST + regex); not an LLM and not human-reviewed",
        "source": {"document_id": source["id"], "name": source["name"], "sha256": source["sha256"]},
        "requirements": requirements, "issues": issues, "architecture": graph, "mermaid": mermaid, "tasks": tasks,
        "test_strategy": ["Unit tests per module (one test_<module>.py each)", "Negative test per fixed issue (secret absent, SQL parameterized, error surfaced)",
                          "Re-run this analyzer in CI and fail on any HIGH issue"],
    }
    plan["digest"] = plan_digest(plan)
    return plan


def plan_digest(plan: dict) -> str:
    core = {k: plan[k] for k in ("title", "intent", "source", "requirements", "issues", "architecture", "mermaid", "tasks")}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


def build_architecture(analysis: dict, issues: list[dict]) -> dict:
    """Node ids are generated, never taken from file names: names can collide with proposed nodes or be invalid Mermaid."""
    ids: dict[str, str] = {}
    for m in analysis["modules"]:
        base = "m_" + (re.sub(r"\W", "_", m) or "x")
        cand, k = base, 2
        while cand in ids.values():
            cand, k = f"{base}_{k}", k + 1
        ids[m] = cand
    nodes = [{"id": ids[m], "label": m, "kind": "current"} for m in analysis["modules"]]
    edges = [{"from": ids[a], "to": ids[b]} for a, deps in sorted(analysis["imports"].items()) if a in ids for b in deps if b in ids]
    cats = {i["category"] for i in issues}
    additions = []
    if "secrets" in cats:
        additions.append(("new_config", "Config / secrets loader (env)", ["auth"]))
    if "validation" in cats:
        additions.append(("new_repo", "Repository layer (parameterized SQL)", ["db"]))
    if "error-handling" in cats:
        additions.append(("new_errors", "Error handling and logging", ["db", "app"]))
    if "testing" in cats:
        additions.append(("new_tests", "Automated test suite", list(analysis["modules"])))
    for nid, label, links in additions:
        nodes.append({"id": nid, "label": label, "kind": "proposed"})
        edges += [{"from": nid, "to": ids[x], "dashed": True} for x in links if x in ids]
    return {"nodes": nodes, "edges": edges}


def graph_to_mermaid(g: dict) -> str:
    lines = ["flowchart TD"]
    for n in g["nodes"]:
        label = n["label"].replace('"', "'")
        lines.append(f'    {n["id"]}["{label}"]' + (":::proposed" if n["kind"] == "proposed" else ""))
    for e in g["edges"]:
        lines.append(f'    {e["from"]} {"-.->" if e.get("dashed") else "-->"} {e["to"]}')
    lines.append("    classDef proposed stroke-dasharray: 4 3")
    return "\n".join(lines)


def validate_mermaid(src: str) -> list[str]:
    """Minimal syntax check for the flowchart subset we emit/accept. Raises MissionError when invalid."""
    lines = [l for l in src.strip().splitlines() if l.strip()]
    if not lines or not re.match(r"^(flowchart|graph)\s+(TD|TB|LR|RL|BT)\s*$", lines[0].strip()):
        raise MissionError("Mermaid must start with 'flowchart TD' (or graph/LR/...)", 422)
    ids = set()
    for l in lines[1:]:
        t = l.strip()
        if t.startswith("classDef"):
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\["[^"\]]*"\](:::[A-Za-z]+)?$', t)
        if m:
            ids.add(m.group(1))
            continue
        e = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+(-->|-\.->)\s+([A-Za-z_][A-Za-z0-9_]*)$", t)
        if e:
            for x in (e.group(1), e.group(3)):
                if x not in ids:
                    raise MissionError(f"Mermaid edge references undefined node '{x}'", 422)
            continue
        raise MissionError(f"Mermaid syntax error near: {t[:60]}", 422)
    return sorted(ids)


def layout_layers(g: dict) -> list[list[dict]]:
    """Longest-path layering used by both the PDF drawing and the frontend renderer."""
    ids = [n["id"] for n in g["nodes"]]
    depth = {i: 0 for i in ids}
    for _ in ids:
        for e in g["edges"]:
            if e["from"] in depth and e["to"] in depth and depth[e["to"]] <= depth[e["from"]]:
                depth[e["to"]] = depth[e["from"]] + 1
    layers: dict[int, list] = {}
    for n in g["nodes"]:
        layers.setdefault(depth[n["id"]], []).append(n)
    return [layers[k] for k in sorted(layers)]


# ------------------------------------------------------------------ PDF composition + verification

def compose_pdf(plan: dict, mission: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen.canvas import Canvas

    buf = io.BytesIO()
    c = Canvas(buf, pagesize=A4, invariant=1)
    c.setTitle(plan["title"])
    W, H = A4
    state = {"y": H - 60}
    cream, ink, sage, gold = colors.HexColor("#F8F5EE"), colors.HexColor("#272722"), colors.HexColor("#8A9C89"), colors.HexColor("#C8A977")

    def page():
        c.setFillColor(cream); c.rect(0, 0, W, H, stroke=0, fill=1); c.setFillColor(ink)
        c.setFont("Helvetica", 8); c.drawString(50, 28, f"RESOLVE · mission {mission['id']} · DRAFT (rule-based analysis)")
        state["y"] = H - 60

    def line(text, font="Helvetica", size=10, gap=14, indent=0, wrap=92):
        for raw in str(text).splitlines() or [""]:
            for part in textwrap.wrap(raw, width=wrap - indent // 5) or [""]:
                if state["y"] < 60:
                    c.showPage(); page()
                c.setFont(font, size); c.drawString(50 + indent, state["y"], part.encode("latin-1", "replace").decode("latin-1"))
                state["y"] -= gap

    page()
    for tl in textwrap.wrap(plan["title"], width=48):
        c.setFont("Helvetica-Bold", 17); c.drawString(50, state["y"], tl.encode("latin-1", "replace").decode("latin-1")); state["y"] -= 22
    state["y"] -= 2
    c.setStrokeColor(gold); c.setLineWidth(2); c.line(50, state["y"], W - 50, state["y"]); state["y"] -= 22
    line(f"Status: {plan['status']}   Method: {plan['method']}", size=9)
    line(f"Source brief: {plan['source']['name']}")
    line(f"SRC-SHA256: {plan['source']['sha256']}", font="Courier", size=8)
    line(f"Source document id: {plan['source']['document_id']}", font="Courier", size=8)
    line(f"Intent revision: {mission['intent_revision']}   Plan digest: {plan['digest'][:32]}", font="Courier", size=8)
    line(f"Request: {mission['intent_text']}", size=9)
    state["y"] -= 8
    sev = {s: sum(1 for i in plan["issues"] if i["severity"] == s) for s in ("HIGH", "MEDIUM", "LOW")}
    line(f"Summary: {len(plan['issues'])} issues found (HIGH {sev['HIGH']}, MEDIUM {sev['MEDIUM']}, LOW {sev['LOW']}) against {len(plan['requirements'])} brief requirements.", font="Helvetica-Bold")
    state["y"] -= 6
    line("1. Issues with evidence", font="Helvetica-Bold", size=13, gap=18)
    for i in plan["issues"]:
        line(f"{i['id']} [{i['severity']}] {i['rule']} - {i['category']}", font="Helvetica-Bold", size=10)
        line(f"Where: {i['citation']}", indent=12, size=9)
        line(f"Evidence: {i['snippet']}", font="Courier", indent=12, size=8)
        if i["requirement"]:
            line(f"Brief {i['requirement']['id']}: {i['requirement']['text']}", indent=12, size=9)
        line(f"Fix: {i['fix']}", indent=12, size=9)
        state["y"] -= 4
    c.showPage(); page()
    line("2. Corrected architecture", font="Helvetica-Bold", size=13, gap=18)
    layers = layout_layers(plan["architecture"])
    bw, bh, gx, gy = 108, 34, 12, 26
    top = state["y"] - 6
    pos, row = {}, 0
    for layer in layers:
        for start in range(0, len(layer), 4):  # at most 4 boxes per row so nothing leaves the page
            for ni, n in enumerate(layer[start:start + 4]):
                pos[n["id"]] = (50 + ni * (bw + gx), top - row * (bh + gy) - bh)
            row += 1
    for e in plan["architecture"]["edges"]:
        if e["from"] in pos and e["to"] in pos:
            (x1, y1), (x2, y2) = pos[e["from"]], pos[e["to"]]
            c.setStrokeColor(sage); c.setLineWidth(1); c.setDash(3, 3) if e.get("dashed") else c.setDash()
            c.line(x1 + bw / 2, y1, x2 + bw / 2, y2 + bh)
    c.setDash()
    for n in plan["architecture"]["nodes"]:
        x, y = pos[n["id"]]
        c.setFillColor(colors.HexColor("#FFFCF7")); c.setStrokeColor(gold if n["kind"] == "proposed" else sage); c.setLineWidth(1.4)
        c.roundRect(x, y, bw, bh, 6, stroke=1, fill=1); c.setFillColor(ink); c.setFont("Helvetica", 8)
        parts = textwrap.wrap(n["label"], width=24)[:2]
        for k, part in enumerate(parts):
            c.drawCentredString(x + bw / 2, y + bh / 2 + 2 - k * 9 + (0 if len(parts) > 1 else -2), part.encode("latin-1", "replace").decode("latin-1"))
    state["y"] = top - row * (bh + gy) - 14
    line("Solid border = current module; gold border = proposed component; dashed edge = proposed dependency.", size=8)
    line("Mermaid source:", font="Helvetica-Bold", size=10)
    for ml in plan["mermaid"].splitlines():
        line(ml, font="Courier", size=7, gap=9, wrap=110)
    state["y"] -= 8
    line("3. Implementation task graph", font="Helvetica-Bold", size=13, gap=18)
    for t in plan["tasks"]:
        dep = f" (after {', '.join(t['depends_on'][:6])}{'...' if len(t['depends_on']) > 6 else ''})" if t["depends_on"] else ""
        line(f"{t['id']}: {t['title']}{dep}", size=9)
    state["y"] -= 6
    line("4. Testing strategy", font="Helvetica-Bold", size=13, gap=18)
    for s in plan["test_strategy"]:
        line("- " + s, size=9)
    state["y"] -= 6
    line("5. Verification", font="Helvetica-Bold", size=13, gap=18)
    line("A PDF cannot contain its own hash. The output SHA-256 is recorded in the RESOLVE receipt after an independent read-back of the stored file.", size=9)
    c.showPage(); c.save()
    return buf.getvalue()


def verify_output(studio: DocumentStudio, user_id: str, workspace_id: str, doc_id: str, plan: dict, source_sha: str) -> dict:
    """Independent oracle: re-read stored bytes, re-hash, parse with pypdf, and check required content."""
    from pypdf import PdfReader
    raw = studio.read_pdf(user_id, workspace_id, doc_id)
    out_sha = hashlib.sha256(raw).hexdigest()
    reader = PdfReader(io.BytesIO(raw), strict=True)
    text = "\n".join((p.extract_text() or "") for p in reader.pages)
    flat = re.sub(r"\s+", "", text)
    checks = {
        "pdf_readable": len(reader.pages) >= 1,
        "contains_title": plan["title"][:30].replace(" ", "") in flat,
        "references_correct_source_sha": f"SRC-SHA256:{source_sha}" in flat,
        "no_other_source_sha": all(h == source_sha for h in re.findall(r"SRC-SHA256:([0-9a-f]{64})", flat)),
        # word-boundary matching: "ISS-1" must not be satisfied by "ISS-10", nor "auth.py:1" by "auth.py:10"
        "contains_every_issue": all(re.search(re.escape(i["id"]) + r"(?!\d)", flat) and re.search(re.escape(i["citation"].split()[0]) + r"(?!\d)", flat) for i in plan["issues"]),
        "contains_architecture": "flowchartTD" in flat,
        "contains_plan_digest": plan["digest"][:32] in flat,
    }
    return {"output_sha256": out_sha, "pages": len(reader.pages), "checks": checks, "passed": all(checks.values())}


def verify_chain(m: dict, doc_owner_ok: bool) -> list[str]:
    """Invariant required for CONFIRMED. Returns the list of broken links (empty means verified)."""
    broken = []
    r = m.get("receipt") or {}
    if not m.get("user_id") or not m.get("workspace_id"):
        broken.append("authenticated user/workspace missing")
    if not doc_owner_ok:
        broken.append("output document not owned by mission tenant")
    if r.get("intent_revision") != m.get("intent_revision"):
        broken.append("approval bound to a different intent revision")
    if not m.get("source_doc_id") or not m.get("source_sha256"):
        broken.append("source entity not bound")
    if ((m.get("plan") or {}).get("intent") or {}).get("revision") != m.get("intent_revision"):
        broken.append("plan was produced for a different intent revision")
    if (m.get("plan") or {}).get("source", {}).get("sha256") != m.get("source_sha256"):
        broken.append("plan derived from a different source version")
    if r.get("approved_plan_digest") != (m.get("plan") or {}).get("digest"):
        broken.append("approved digest differs from plan")
    ver = r.get("verification") or {}
    if not ver.get("passed"):
        broken.append("output verification failed")
    if not m.get("output_sha256") or ver.get("output_sha256") != m.get("output_sha256"):
        broken.append("output hash not matched by read-back")
    return broken


# ------------------------------------------------------------------ durable store + orchestration

class BriefMissionStore:
    def __init__(self, db_path: str | Path, studio: DocumentStudio, hooks: dict | None = None):
        self.db_path, self.studio, self.hooks = Path(db_path), studio, hooks if hooks is not None else {}
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="worker")  # node workers
        self._orch = ThreadPoolExecutor(max_workers=16, thread_name_prefix="orch")   # orchestrators block on node futures; separate pool avoids starvation
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS brief_missions (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, state TEXT NOT NULL,
                intent_text TEXT NOT NULL, intent_revision INTEGER NOT NULL, project TEXT NOT NULL,
                source_doc_id TEXT, source_sha256 TEXT, plan TEXT, output_doc_id TEXT, output_sha256 TEXT,
                receipt TEXT, error TEXT, cancelled INTEGER NOT NULL DEFAULT 0, created_at REAL, updated_at REAL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS mission_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT NOT NULL, ts REAL NOT NULL, node TEXT,
                worker TEXT, kind TEXT NOT NULL, detail TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS mission_events_m ON mission_events(mission_id)")
            # Worker crash recovery: nothing survives a restart, so in-flight missions are marked, never silently resumed.
            now = time.time()
            for (mid,) in db.execute("SELECT id FROM brief_missions WHERE state IN ('RUNNING','COMPOSING')").fetchall():
                db.execute("UPDATE brief_missions SET state='FAILED', error='server restarted mid-run; no side effect was assumed - start a new mission', updated_at=? WHERE id=?", (now, mid))
                db.execute("INSERT INTO mission_events(mission_id,ts,node,worker,kind,detail) VALUES (?,?,?,?,?,?)",
                           (mid, now, "MISSION", "recovery", "RECOVERED_AS_FAILED", "{}"))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        """Wait for in-flight workers (used by tests and graceful shutdown)."""
        self._orch.shutdown(wait=True)
        self._pool.shutdown(wait=True)

    # ---- persistence helpers
    def _row(self, mission_id: str, user_id: str, workspace_id: str):
        with self._db() as db:
            row = db.execute("SELECT * FROM brief_missions WHERE id=? AND user_id=? AND workspace_id=?", (mission_id, user_id, workspace_id)).fetchone()
        if row is None:
            raise MissionError("Mission not found", 404)
        return row

    def _alive(self, mission_id: str) -> sqlite3.Row | None:
        """Row if the mission still exists and is not cancelled; else None (late-result guard)."""
        with self._db() as db:
            row = db.execute("SELECT * FROM brief_missions WHERE id=?", (mission_id,)).fetchone()
        return row if row is not None and not row["cancelled"] and row["state"] not in TERMINAL else None

    def _event(self, mission_id: str, node: str, worker: str, kind: str, detail: dict | None = None, ts: float | None = None):
        with self._db() as db:
            if db.execute("SELECT 1 FROM brief_missions WHERE id=?", (mission_id,)).fetchone() is None:
                return  # mission deleted: never resurrect rows
            db.execute("INSERT INTO mission_events(mission_id,ts,node,worker,kind,detail) VALUES (?,?,?,?,?,?)",
                       (mission_id, ts or time.time(), node, worker, kind, json.dumps(detail or {})))

    def _update(self, mission_id: str, **fields):
        fields["updated_at"] = time.time()
        for k in ("plan", "receipt"):
            if k in fields and not isinstance(fields[k], str) and fields[k] is not None:
                fields[k] = json.dumps(fields[k])
        sets = ",".join(f"{k}=?" for k in fields)
        with self._db() as db:
            db.execute(f"UPDATE brief_missions SET {sets} WHERE id=?", (*fields.values(), mission_id))

    def _cas(self, mission_id: str, from_states: tuple, **fields) -> bool:
        """Atomic compare-and-set transition. A check-then-write in Python is racy (two approvals, or a cancel landing
        between a worker's liveness check and its commit); the WHERE clause makes the state test and the write one step."""
        fields["updated_at"] = time.time()
        for k in ("plan", "receipt"):
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        sets = ",".join(f"{k}=?" for k in fields)
        marks = ",".join("?" * len(from_states))
        with self._db() as db:
            cur = db.execute(f"UPDATE brief_missions SET {sets} WHERE id=? AND cancelled=0 AND state IN ({marks})", (*fields.values(), mission_id, *from_states))
            return cur.rowcount == 1

    def _public(self, row, with_events=True) -> dict:
        m = {k: row[k] for k in row.keys()}
        m["plan"] = json.loads(m["plan"]) if m["plan"] else None
        m["receipt"] = json.loads(m["receipt"]) if m["receipt"] else None
        m["cancelled"] = bool(m["cancelled"])
        if with_events:
            with self._db() as db:
                m["events"] = [{"seq": e["seq"], "ts": e["ts"], "node": e["node"], "worker": e["worker"], "kind": e["kind"], "detail": json.loads(e["detail"] or "{}")}
                               for e in db.execute("SELECT * FROM mission_events WHERE mission_id=? ORDER BY seq", (m["id"],))]
            m["nodes"] = node_summary(m["events"])
            m["chain_broken"] = verify_chain(m, True) if m["state"] == "CONFIRMED" else None
        return m

    # ---- public API
    def seed_sample_brief(self, user_id: str, workspace_id: str) -> dict:
        text = SAMPLE_BRIEF.read_text(encoding="utf-8")
        return self.studio.create_report(user_id, workspace_id, text.splitlines()[0], "\n".join(text.splitlines()[1:]))

    def list(self, user_id: str, workspace_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM brief_missions WHERE user_id=? AND workspace_id=? ORDER BY created_at DESC", (user_id, workspace_id)).fetchall()
        return [self._public(r, with_events=False) for r in rows]

    def get(self, mission_id: str, user_id: str, workspace_id: str) -> dict:
        return self._public(self._row(mission_id, user_id, workspace_id))

    def resolve_brief(self, user_id: str, workspace_id: str, document_id: str | None, document_name: str | None) -> dict:
        """Bind the intent to ONE stable document id. Names are only a lookup aid and must be unambiguous."""
        if document_id:
            return self.studio.get_document(user_id, workspace_id, document_id)
        if document_name:
            matches = [d for d in self.studio.list_documents(user_id, workspace_id) if d["name"].lower() == document_name.strip().lower()]
            if not matches:
                raise MissionError(f"No document named '{document_name}'", 404)
            if len(matches) > 1:
                raise MissionError(f"{len(matches)} documents are named '{document_name}'; choose one by id",
                                   409, candidates=[{"id": d["id"], "name": d["name"], "sha256": d["sha256"], "created_at": d["created_at"]} for d in matches])
            return matches[0]
        raise MissionError("document_id or document_name is required", 400)

    def start(self, user_id: str, workspace_id: str, intent_text: str, document_id: str | None = None, document_name: str | None = None,
              expected_sha256: str | None = None, project: str = "sample_project") -> dict:
        intent_text = (intent_text or "").strip()
        if not intent_text or len(intent_text) > 2000:
            raise MissionError("intent_text (1-2000 chars) required")
        if not re.search(r"\b(brief|report|architecture|spec)", intent_text, re.I):
            raise MissionError("Unsupported intent: this mission handles 'inspect brief -> analyze -> architecture -> report'", 422)
        if project not in BUNDLED_PROJECTS:
            raise MissionError(f"Unknown project '{project}'. Available: {sorted(BUNDLED_PROJECTS)}", 400)
        src = self.resolve_brief(user_id, workspace_id, document_id, document_name)
        if expected_sha256 and expected_sha256 != src["sha256"]:
            raise MissionError("Source document changed since it was selected (stale sha256); refusing to proceed", 409)
        with self._db() as db:
            active = db.execute("SELECT COUNT(*) FROM brief_missions WHERE user_id=? AND workspace_id=? AND state IN ('RUNNING','COMPOSING')", (user_id, workspace_id)).fetchone()[0]
            total = db.execute("SELECT COUNT(*) FROM brief_missions WHERE user_id=?", (user_id,)).fetchone()[0]
        if active >= MAX_ACTIVE_PER_TENANT:
            raise MissionError("Too many active missions; wait for one to finish or cancel it", 429)
        if total >= MAX_MISSIONS_PER_TENANT:
            raise MissionError("Mission quota reached; delete old missions", 429)
        mid, now = "bm_" + uuid.uuid4().hex[:12], time.time()
        with self._db() as db:
            db.execute("INSERT INTO brief_missions(id,user_id,workspace_id,state,intent_text,intent_revision,project,source_doc_id,source_sha256,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (mid, user_id, workspace_id, "RUNNING", intent_text, 1, project, src["id"], src["sha256"], now, now))
        self._event(mid, "MISSION", "orchestrator", "MISSION_CREATED", {"intent_revision": 1, "source_doc_id": src["id"], "source_sha256": src["sha256"], "name": src["name"]})
        self._orch.submit(self._run_analysis, mid)
        return self.get(mid, user_id, workspace_id)

    def _node(self, mid: str, node: str, fn):
        """Run one worker node with start/end events, timeout accounting and late-result rejection."""
        worker = f"{node}#{threading.current_thread().name}"
        t0 = time.time()
        self._event(mid, node, worker, "NODE_STARTED", ts=t0)
        hook = self.hooks.get(node)
        try:
            result = self._run_bounded(node, lambda: (hook() if hook else None, fn())[1])
        except Exception as exc:
            self._event(mid, node, worker, "NODE_FAILED", {"error": str(exc)[:300]})
            raise
        t1 = time.time()
        if self._alive(mid) is None:
            self._event(mid, node, worker, "LATE_RESULT_REJECTED", {"reason": "mission cancelled, finished or deleted"})
            raise MissionError("late result rejected", 409)
        self._event(mid, node, worker, "NODE_FINISHED", {"duration_s": round(t1 - t0, 4)}, ts=t1)
        return result

    @staticmethod
    def _run_bounded(node: str, fn):
        """Run fn in a daemon thread and give up after NODE_TIMEOUT_S. Python cannot kill a thread, so a hung node is
        abandoned (its result is discarded by the liveness/CAS guards) instead of blocking the mission and its quota slot forever."""
        box: dict = {}

        def target():
            try:
                box["value"] = fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised in the caller
                box["error"] = exc
        t = threading.Thread(target=target, name=f"node-{node}", daemon=True)
        t.start()
        t.join(NODE_TIMEOUT_S)
        if t.is_alive():
            raise MissionError(f"{node} exceeded {NODE_TIMEOUT_S}s (timeout)", 504)
        if "error" in box:
            raise box["error"]
        return box["value"]

    def _run_analysis(self, mid: str):
        try:
            row = self._alive(mid)
            if row is None:
                return
            uid, wid = row["user_id"], row["workspace_id"]
            fa = self._pool.submit(self._node, mid, "DOCUMENT_READER", lambda: self._read_brief(row))
            fb = self._pool.submit(self._node, mid, "REPOSITORY_ANALYZER", lambda: analyze_project(BUNDLED_PROJECTS[row["project"]]))
            reqs, analysis = fa.result(), fb.result()  # both must succeed; a failure in either fails the mission
            src = self.studio.get_document(uid, wid, row["source_doc_id"])
            plan = self._node(mid, "ARCHITECTURE_PLANNER", lambda: build_plan(reqs, analysis, src, self._title(src), {"revision": row["intent_revision"], "text": row["intent_text"]}))
            if not self._cas(mid, ("RUNNING",), plan=plan, state="AWAITING_APPROVAL"):
                self._event(mid, "ARCHITECTURE_PLANNER", "orchestrator", "LATE_RESULT_REJECTED", {"reason": "mission left RUNNING before the plan could be committed"})
                return
            self._event(mid, "MISSION", "orchestrator", "AWAITING_APPROVAL", {"plan_digest": plan["digest"], "issues": len(plan["issues"])})
        except Exception as exc:
            self._fail(mid, exc)

    @staticmethod
    def _title(src: dict) -> str:
        return "Project analysis and corrected architecture - " + re.sub(r"\.pdf$", "", src["name"], flags=re.I)[:50]

    def _read_brief(self, row) -> list[dict]:
        text = self.studio.extract_text(row["user_id"], row["workspace_id"], row["source_doc_id"])
        doc = self.studio.get_document(row["user_id"], row["workspace_id"], row["source_doc_id"])
        if doc["sha256"] != row["source_sha256"]:
            raise MissionError("source document version changed after the mission was bound", 409)
        reqs = extract_requirements(text)
        if not reqs:
            raise MissionError("No requirements found in the brief (expected lines like 'R1 ... must ...')", 422)
        return reqs

    def _fail(self, mid: str, exc: Exception):
        if self._cas(mid, ("RUNNING", "AWAITING_APPROVAL", "COMPOSING"), state="FAILED", error=str(exc)[:400]):
            self._event(mid, "MISSION", "orchestrator", "MISSION_FAILED", {"error": str(exc)[:400]})

    def approve(self, mission_id: str, user_id: str, workspace_id: str, plan_digest: str) -> dict:
        row = self._row(mission_id, user_id, workspace_id)
        if row["state"] != "AWAITING_APPROVAL" or row["cancelled"]:
            raise MissionError(f"Mission is {row['state']}; nothing to approve", 409)
        plan = json.loads(row["plan"])
        if plan_digest != plan["digest"]:
            self._event(mission_id, "APPROVAL", "user", "APPROVAL_REJECTED_STALE", {"presented": str(plan_digest)[:16], "current": plan["digest"][:16]})
            raise MissionError("Approval digest does not match the current plan (stale approval)", 409)
        if not self._cas(mission_id, ("AWAITING_APPROVAL",), state="COMPOSING"):   # exactly one concurrent approval wins
            raise MissionError("Mission is no longer awaiting approval (already approved, revised or cancelled)", 409)
        self._event(mission_id, "APPROVAL", user_id, "PLAN_APPROVED", {"plan_digest": plan_digest, "intent_revision": row["intent_revision"]})
        self._orch.submit(self._run_output, mission_id, plan_digest)
        return self.get(mission_id, user_id, workspace_id)

    def revise(self, mission_id: str, user_id: str, workspace_id: str, intent_text: str) -> dict:
        row = self._row(mission_id, user_id, workspace_id)
        if row["state"] in TERMINAL or row["state"] in ("RUNNING", "COMPOSING"):
            raise MissionError(f"Mission is {row['state']}; cannot revise now", 409)
        intent_text = (intent_text or "").strip()
        if not intent_text or len(intent_text) > 2000:
            raise MissionError("intent_text (1-2000 chars) required")
        rev = row["intent_revision"] + 1
        if not self._cas(mission_id, ("AWAITING_APPROVAL",), intent_text=intent_text, intent_revision=rev, state="RUNNING", plan=None):
            raise MissionError("Mission changed state while revising; reload and retry", 409)
        self._event(mission_id, "MISSION", user_id, "INTENT_REVISED", {"intent_revision": rev, "supersedes_approvals": True})
        self._orch.submit(self._run_analysis, mission_id)
        return self.get(mission_id, user_id, workspace_id)

    def cancel(self, mission_id: str, user_id: str, workspace_id: str) -> dict:
        row = self._row(mission_id, user_id, workspace_id)
        if row["state"] in TERMINAL:
            raise MissionError(f"Mission already {row['state']}", 409)
        if not self._cas(mission_id, ("RUNNING", "AWAITING_APPROVAL", "COMPOSING"), cancelled=1, state="CANCELLED"):
            raise MissionError("Mission already finished or cancelled", 409)
        self._event(mission_id, "MISSION", user_id, "MISSION_CANCELLED", {})
        return self.get(mission_id, user_id, workspace_id)

    def _run_output(self, mid: str, approved_digest: str):
        out_id, row = None, None
        try:
            row = self._alive(mid)
            if row is None:
                return
            uid, wid = row["user_id"], row["workspace_id"]
            plan = json.loads(row["plan"])
            if plan["digest"] != approved_digest:
                raise MissionError("plan changed after approval", 409)
            mission = {k: row[k] for k in row.keys()}

            def compose():
                nonlocal out_id
                src = self.studio.get_document(uid, wid, row["source_doc_id"])
                if src["sha256"] != row["source_sha256"]:
                    raise MissionError("source document version changed after approval", 409)
                pdf = compose_pdf(plan, mission)
                doc = self.studio.save_generated(uid, wid, pdf, f"{plan['title'][:60]}.pdf", source_ids=row["source_doc_id"], operation="brief_report")
                out_id = doc["id"]
                if self._alive(mid) is None:  # tenant deleted/cancelled while composing: compensate, no orphan output
                    self.studio.delete_document(uid, wid, out_id)
                    out_id = None
                return doc

            doc = self._node(mid, "REPORT_COMPOSER", compose)
            ver = self._node(mid, "EFFECT_VERIFIER", lambda: verify_output(self.studio, uid, wid, doc["id"], plan, row["source_sha256"]))
            if not ver["passed"] or ver["output_sha256"] != doc["sha256"]:
                self._event(mid, "EFFECT_VERIFIER", "verifier", "VERIFICATION_FAILED", ver)
                raise MissionError("Output failed independent verification: " + ", ".join(k for k, v in ver["checks"].items() if not v), 422)
            printing = self._node(mid, "PRINT_PREPARER", lambda: {"state": "PRINT_READY", "pages": ver["pages"], "default_range": f"1-{ver['pages']}",
                                                               "dialog_opened": False, "physical_print_verified": False})
            receipt = {"intent_revision": row["intent_revision"], "approved_plan_digest": approved_digest, "verification": ver, "print": printing,
                       "chain": ["user", "intent_revision", "source_entity(id+sha256)", "approved_plan_digest", "output_sha256", "read_back"],
                       "confirmed_at": time.time()}
            candidate = {**mission, "plan": plan, "receipt": receipt, "output_sha256": doc["sha256"]}
            broken = verify_chain(candidate, self._owns(uid, wid, doc["id"]))
            if broken:
                raise MissionError("Causal chain broken: " + "; ".join(broken), 422)
            if not self._cas(mid, ("COMPOSING",), state="CONFIRMED", output_doc_id=doc["id"], output_sha256=doc["sha256"], receipt=receipt):
                self._event(mid, "MISSION", "orchestrator", "LATE_RESULT_REJECTED", {"reason": "mission left COMPOSING before confirmation"})
                self.studio.delete_document(uid, wid, doc["id"])   # compensate: no confirmed receipt, so no orphan artifact
                return
            self._event(mid, "MISSION", "orchestrator", "MISSION_CONFIRMED", {"output_doc_id": doc["id"], "output_sha256": doc["sha256"]})
        except Exception as exc:
            if out_id and row is not None and self._alive(mid) is not None:  # failed verification: do not leave an unverified artifact
                try:
                    self.studio.delete_document(row["user_id"], row["workspace_id"], out_id)
                except DocumentError:
                    pass
            self._fail(mid, exc)

    def _owns(self, uid, wid, doc_id) -> bool:
        try:
            return self.studio.get_document(uid, wid, doc_id)["id"] == doc_id
        except DocumentError:
            return False

    def record_print_dialog(self, mission_id: str, user_id: str, workspace_id: str) -> dict:
        row = self._row(mission_id, user_id, workspace_id)
        if row["state"] != "CONFIRMED":
            raise MissionError("Only a confirmed mission can be printed", 409)
        self._event(mission_id, "PRINT_PREPARER", "browser", "PRINT_DIALOG_OPENED", {"note": "browser dialog opened; physical printing is NOT verified"})
        return self.get(mission_id, user_id, workspace_id)

    def delete(self, mission_id: str, user_id: str, workspace_id: str, delete_output: bool = True) -> dict:
        row = self._row(mission_id, user_id, workspace_id)
        with self._db() as db:  # delete first so any in-flight worker's late result is rejected
            db.execute("DELETE FROM brief_missions WHERE id=?", (mission_id,))
            db.execute("DELETE FROM mission_events WHERE mission_id=?", (mission_id,))
        removed = None
        if delete_output and row["output_doc_id"]:
            try:
                self.studio.delete_document(user_id, workspace_id, row["output_doc_id"])
                removed = row["output_doc_id"]
            except DocumentError:
                pass
        return {"deleted": mission_id, "output_document_removed": removed}

    def purge(self, user_id: str, workspace_id: str | None = None) -> int:
        """Called by privacy deletion: remove rows first (in-flight workers then reject their results)."""
        q, args = ("user_id=?", (user_id,)) if workspace_id is None else ("user_id=? AND workspace_id=?", (user_id, workspace_id))
        with self._db() as db:
            ids = [r[0] for r in db.execute(f"SELECT id FROM brief_missions WHERE {q}", args)]
            db.execute(f"DELETE FROM brief_missions WHERE {q}", args)
            for i in ids:
                db.execute("DELETE FROM mission_events WHERE mission_id=?", (i,))
        return len(ids)


def node_summary(events: list[dict]) -> list[dict]:
    """Per-node execution facts derived only from recorded events (never from a script)."""
    nodes: dict[str, dict] = {}
    for e in events:
        if e["node"] in ("MISSION", "APPROVAL") and e["kind"] not in ("PLAN_APPROVED",):
            continue
        n = nodes.setdefault(e["node"], {"node": e["node"], "worker": e["worker"], "status": "PENDING", "started_at": None, "finished_at": None, "run": 0})
        if e["kind"] == "NODE_STARTED":
            n.update(status="RUNNING", started_at=e["ts"], worker=e["worker"], finished_at=None)
        elif e["kind"] == "NODE_FINISHED":
            n.update(status="DONE", finished_at=e["ts"])
        elif e["kind"] == "NODE_FAILED":
            n.update(status="FAILED", error=e["detail"].get("error"))
        elif e["kind"] == "LATE_RESULT_REJECTED":
            n.update(status="REJECTED_LATE")
    return list(nodes.values())
