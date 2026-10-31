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
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from document_studio import DocumentError, DocumentStudio
from operation_contract import Contract, EffectLedger, Observed, OpState, execute as execute_contract
from project_store import ProjectError, ProjectStore, SOURCE_EXTENSIONS, fingerprint

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
ALLOWED_STEPS = ("read_brief", "analyze_code", "inspect_web", "architecture", "report", "print")
DEFAULT_STEPS = ("read_brief", "analyze_code", "architecture", "report", "print")
REQUIRED_STEPS = ("read_brief", "analyze_code", "architecture", "report")
MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
MAX_EVIDENCE_PER_MISSION = 40
MAX_SKILLS_PER_TENANT = 30
MAX_PAUSE_S = 900.0   # a mission paused for longer than this fails instead of holding workers forever
RUNTIME_SEVERITY = {"page_error": "HIGH", "console_error": "HIGH", "http_error_5xx": "HIGH", "http_error": "MEDIUM", "failed_request": "MEDIUM",
                    "blocked_request": "MEDIUM", "navigation_timeout": "MEDIUM", "browser_crash": "HIGH", "popup": "LOW"}


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


MAX_ISSUES = 60
MAX_DIAGRAM_MODULES = 16
_JS_LIKE = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
_SECRET_RE = re.compile(r"""\b\w*(secret|password|passwd|api_?key|token)\w*\s*[:=]\s*(["'`])([^"'`\n]{8,})\2""", re.I)
_ENV_RE = re.compile(r"process\.env|os\.getenv|os\.environ|getenv\(|ENV\[|System\.getenv|\$\{")
_SQL_CALL_RE = re.compile(r"\b(query|execute|exec|raw|prepare|run|all)\s*\(")
_SQL_WORD_RE = re.compile(r"\b(select|insert|update|delete)\b", re.I)
_EMPTY_CATCH_RE = re.compile(r"catch\s*(\([^)]*\))?\s*\{\s*(//[^\n]*)?\s*\}")
_TODO_RE = re.compile(r"(#|//|/\*)\s*(TODO|FIXME)\b")
_IMPORT_RE = re.compile(r"""(?:from\s+|require\(\s*|import\s+)["'](\.{1,2}/[^"']+)["']""")


def _module_name(path: str) -> str:
    return str(PurePosixPath(path).with_suffix(""))


def _is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name.lower()
    return (name.startswith("test_") or ".test." in name or ".spec." in name or name.endswith(("_test.go", "test.java", "_spec.rb", "test.php"))
            or any(part in ("tests", "test", "__tests__", "spec") for part in p.parts[:-1]))


def _test_target(path: str) -> str:
    stem = PurePosixPath(path).stem.lower()
    for marker in ("test_", ".test", ".spec", "_test", "_spec"):
        stem = stem.replace(marker, "")
    return re.sub(r"test$", "", stem)


def analyze_sources(files: dict[str, str]) -> dict:
    """Static analysis of a {path: text} map. Nothing is executed or imported."""
    findings, imports, modules = [], {}, []
    stems: dict[str, list[str]] = {}
    test_targets = {_test_target(p) for p in files if _is_test_file(p)}
    for path in sorted(files):
        if not _is_test_file(path):
            modules.append(_module_name(path))
            stems.setdefault(PurePosixPath(path).stem.lower(), []).append(_module_name(path))
    langs: dict[str, int] = {}
    for path in sorted(files):
        src, ext = files[path], PurePosixPath(path).suffix.lower()
        langs[ext] = langs.get(ext, 0) + 1
        lines = src.splitlines()
        test_file = _is_test_file(path)
        mod = _module_name(path)
        if not test_file and PurePosixPath(path).stem.lower() not in test_targets:
            findings.append({"category": "testing", "file": path, "line": 1, "rule": "NO_TESTS", "snippet": f"no test file found for {PurePosixPath(path).name}"})
        if ext == ".py":
            try:
                tree = ast.parse(src)
            except SyntaxError as exc:
                findings.append({"category": "error-handling", "file": path, "line": exc.lineno or 1, "rule": "SYNTAX_ERROR", "snippet": (exc.text or "").strip()[:120]})
                continue
            if not test_file:
                imports[mod] = sorted({m for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
                                       for a in (n.names if isinstance(n, ast.Import) else [n])
                                       for m in stems.get((a.name if isinstance(n, ast.Import) else (n.module or "")).split(".")[-1].lower(), [])} - {mod})
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    for t in node.targets:
                        if isinstance(t, ast.Name) and re.search(r"(secret|password|api_?key|token)", t.id, re.I) and len(node.value.value) >= 8:
                            findings.append({"category": "secrets", "file": path, "line": node.lineno, "rule": "HARDCODED_SECRET", "snippet": f"{t.id} = <redacted literal>"})
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("execute", "executemany") and node.args \
                        and isinstance(node.args[0], (ast.JoinedStr, ast.BinOp)):
                    findings.append({"category": "validation", "file": path, "line": node.lineno, "rule": "SQL_STRING_BUILDING", "snippet": lines[node.lineno - 1].strip()[:120]})
                if isinstance(node, ast.ExceptHandler) and (node.type is None or (len(node.body) == 1 and isinstance(node.body[0], ast.Pass))):
                    findings.append({"category": "error-handling", "file": path, "line": node.lineno, "rule": "SILENT_EXCEPT", "snippet": lines[node.lineno - 1].strip()[:120]})
        else:
            if not test_file:
                deps = set()
                for ref in _IMPORT_RE.findall(src):
                    deps.update(stems.get(PurePosixPath(ref).stem.lower(), []))
                imports[mod] = sorted(deps - {mod})
            for i, text in enumerate(lines, 1):
                m = _SECRET_RE.search(text)
                if m and not _ENV_RE.search(text) and not test_file:
                    name = re.search(r"\b\w*(?:secret|password|passwd|api_?key|token)\w*", text, re.I).group(0)
                    findings.append({"category": "secrets", "file": path, "line": i, "rule": "HARDCODED_SECRET", "snippet": f"{name} = <redacted literal>"})
                if _SQL_CALL_RE.search(text) and _SQL_WORD_RE.search(text) and (("${" in text) or re.search(r"""["'`]\s*\+|\+\s*["'`]|%s?\s*%""", text)):
                    findings.append({"category": "validation", "file": path, "line": i, "rule": "SQL_STRING_BUILDING", "snippet": text.strip()[:120]})
                if _EMPTY_CATCH_RE.search(text):
                    findings.append({"category": "error-handling", "file": path, "line": i, "rule": "SILENT_EXCEPT", "snippet": text.strip()[:120]})
        for i, text in enumerate(lines, 1):
            if _TODO_RE.search(text):
                findings.append({"category": "maintainability", "file": path, "line": i, "rule": "OPEN_TODO", "snippet": text.strip()[:120]})
    findings.sort(key=lambda f: (f["file"], f["line"], f["rule"]))
    return {"modules": modules, "imports": imports, "findings": findings, "files_scanned": len(files), "languages": langs}


def load_bundled(name: str) -> dict[str, str]:
    root = BUNDLED_PROJECTS[name]
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in SOURCE_EXTENSIONS and "__pycache__" not in p.parts}


def analyze_project(project_dir: Path) -> dict:   # kept for callers that pass a directory
    root = Path(project_dir)
    return analyze_sources({p.relative_to(root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
                            for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in SOURCE_EXTENSIONS and "__pycache__" not in p.parts})


def _clean(text, limit=160) -> str:
    """Page/console text is untrusted data: strip control characters and bound its length before it enters a report."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(text or "")).strip()[:limit]


def runtime_findings(findings: list) -> list[dict]:
    """Convert real browser observations (objects or dicts with kind/message/url/status/line) into plan findings."""
    out, seen = [], set()
    for f in findings:
        g = f if isinstance(f, dict) else {k: getattr(f, k, None) for k in ("kind", "message", "url", "status", "line")}
        kind, status = g.get("kind") or "unknown", g.get("status")
        if kind == "download":
            continue                                    # informational; listed under runtime evidence, not an issue
        key = (kind, g.get("message"), g.get("url"), status)
        if key in seen:
            continue
        seen.add(key)
        sev_key = "http_error_5xx" if kind == "http_error" and status and int(status) >= 500 else kind
        path = re.sub(r"\s+", "", urlparse(str(g.get("url") or "")).path or "/")[:80] or "/"
        n = len(out) + 1
        out.append({"category": "error-handling", "file": f"webapp:{path}", "line": int(g.get("line") or 0), "rule": "RUNTIME_" + kind.upper(),
                    "snippet": _clean(g.get("message")) + (f" [HTTP {status}]" if status else ""), "severity": RUNTIME_SEVERITY.get(sev_key, "LOW"),
                    "citation": f"runtime:{kind}#{n}:{path}", "source": "runtime"})
        if len(out) >= 12:
            break
    return out


def build_plan(requirements: list[dict], analysis: dict, source: dict, title: str, intent: dict | None = None, project: dict | None = None, runtime: dict | None = None) -> dict:
    issues = []
    sev_of = lambda f: f.get("severity") or SEVERITY.get(f["category"], "LOW")
    ranked = sorted(analysis["findings"], key=lambda f: ({"HIGH": 0, "MEDIUM": 1, "LOW": 2}[sev_of(f)], f["file"], f["line"]))
    kept = sorted(ranked[:MAX_ISSUES], key=lambda f: (f["file"], f["line"], f["rule"]))
    for n, f in enumerate(kept, 1):
        req = next((r for r in requirements if r["category"] == f["category"]), None)
        issues.append({"id": f"ISS-{n}", "severity": sev_of(f), **{k: v for k, v in f.items() if k != "severity"},
                       "requirement": ({"id": req["id"], "text": req["text"], "brief_line": req["line"]} if req else None),
                       "fix": FIX.get(f["category"], "Review manually."),
                       "citation": f.get("citation") or f"{f['file']}:{f['line']}" + (f" violates {req['id']} (brief line {req['line']})" if req else " (no matching brief requirement)")})
    graph = build_architecture(analysis, [i for i in issues if i.get("source") != "runtime"])
    mermaid = graph_to_mermaid(graph)
    validate_mermaid(mermaid)
    tasks = [{"id": f"T{i}", "title": f"Fix {x['id']}: {x['fix']}", "depends_on": [], "category": x["category"]} for i, x in enumerate(issues, 1)]
    tasks.append({"id": f"T{len(tasks) + 1}", "title": "Re-run analyzer and the new test suite; every ISS must clear", "depends_on": [t["id"] for t in tasks], "category": "verification"})
    plan = {
        "title": title, "status": "DRAFT", "intent": intent or {"revision": 1, "text": ""},
        "project": {**(project or {"name": "unknown", "id": None, "sha256": "0" * 64}), "files_scanned": analysis["files_scanned"], "languages": analysis.get("languages", {}),
                    "findings_total": len(analysis["findings"]), "findings_omitted": max(0, len(analysis["findings"]) - MAX_ISSUES)}, "method": "deterministic static analysis (AST + regex); not an LLM and not human-reviewed",
        "source": {"document_id": source["id"], "name": source["name"], "sha256": source["sha256"]},
        "runtime": runtime, "requirements": requirements, "issues": issues, "architecture": graph, "mermaid": mermaid, "tasks": tasks,
        "test_strategy": ["Unit tests per module (one test_<module>.py each)", "Negative test per fixed issue (secret absent, SQL parameterized, error surfaced)",
                          "Re-run this analyzer in CI and fail on any HIGH issue"],
    }
    plan["digest"] = plan_digest(plan)
    return plan


def plan_digest(plan: dict) -> str:
    core = {k: plan.get(k) for k in ("title", "intent", "source", "project", "runtime", "requirements", "issues", "architecture", "mermaid", "tasks")}
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


def build_architecture(analysis: dict, issues: list[dict]) -> dict:
    """Node ids are generated, never taken from file names. Proposed components attach to the modules whose
    findings motivate them, so the diagram follows the analyzed project instead of fixed module names."""
    by_module: dict[str, set] = {}
    for i in issues:
        by_module.setdefault(_module_name(i["file"]), set()).add(i["category"])
    degree = {m: len(analysis["imports"].get(m, [])) + sum(m in d for d in analysis["imports"].values()) for m in analysis["modules"]}
    ranked = sorted(analysis["modules"], key=lambda m: (-(2 * len(by_module.get(m, ())) + degree[m]), m))
    shown = sorted(ranked[:MAX_DIAGRAM_MODULES])
    ids: dict[str, str] = {}
    for m in shown:
        base = "m_" + (re.sub(r"\W", "_", m) or "x")
        cand, k = base, 2
        while cand in ids.values():
            cand, k = f"{base}_{k}", k + 1
        ids[m] = cand
    nodes = [{"id": ids[m], "label": m, "kind": "current"} for m in shown]
    edges = [{"from": ids[a], "to": ids[b]} for a, deps in sorted(analysis["imports"].items()) if a in ids for b in deps if b in ids]
    plan_for = (("secrets", "new_config", "Config / secrets loader (env)"), ("validation", "new_repo", "Validated data-access layer"),
                ("error-handling", "new_errors", "Error handling and logging"), ("testing", "new_tests", "Automated test suite"))
    for cat, nid, label in plan_for:
        targets = [m for m in shown if cat in by_module.get(m, ())][:6]
        if targets:
            nodes.append({"id": nid, "label": label, "kind": "proposed"})
            edges += [{"from": nid, "to": ids[m], "dashed": True} for m in targets]
    return {"nodes": nodes, "edges": edges, "modules_total": len(analysis["modules"]), "modules_omitted": len(analysis["modules"]) - len(shown)}


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


def parse_mermaid(src: str) -> dict:
    """Parse the validated flowchart subset back into the graph structure (so user edits drive the PDF and diagram)."""
    validate_mermaid(src)
    nodes, edges = [], []
    for line in src.strip().splitlines()[1:]:
        t = line.strip()
        if not t or t.startswith("classDef"):
            continue
        m = re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\["([^"\]]*)"\](:::[A-Za-z]+)?$', t)
        if m:
            nodes.append({"id": m.group(1), "label": m.group(2), "kind": "proposed" if m.group(3) == ":::proposed" else "current"})
            continue
        e = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+(-->|-\.->)\s+([A-Za-z_][A-Za-z0-9_]*)$", t)
        edges.append({"from": e.group(1), "to": e.group(3), **({"dashed": True} if e.group(2) == "-.->" else {})})
    if not nodes or len(nodes) > 60 or len(edges) > 300:
        raise MissionError("Architecture must have 1-60 nodes and at most 300 edges", 422)
    if len({n["id"] for n in nodes}) != len(nodes):
        raise MissionError("Duplicate node ids in architecture", 422)
    return {"nodes": nodes, "edges": edges}


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

def compose_pdf(plan: dict, mission: dict, evidence_images: dict | None = None) -> bytes:
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
    if plan.get("edited_by_user"):
        line("The architecture below was edited by the user before approval.", size=9)
    line(f"Source brief: {plan['source']['name']}")
    line(f"SRC-SHA256: {plan['source']['sha256']}", font="Courier", size=8)
    line(f"Source document id: {plan['source']['document_id']}", font="Courier", size=8)
    pj = plan["project"]
    line(f"Analyzed project: {pj['name']} ({pj['files_scanned']} source files, languages: {', '.join(sorted(pj['languages'])) or 'n/a'})", size=9)
    line(f"PROJECT-SHA256: {pj['sha256']}", font="Courier", size=8)
    if pj["findings_omitted"]:
        line(f"Note: {pj['findings_total']} findings detected; the {pj['findings_total'] - pj['findings_omitted']} highest-severity are listed.", size=9)
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
    rt = plan.get("runtime")
    if rt:
        state["y"] -= 6
        line("1b. Runtime diagnostics (real browser run)", font="Helvetica-Bold", size=13, gap=18)
        line(f"Application: {rt['app_url']}   Final URL: {rt.get('final_url') or '-'}   Page title: {rt.get('title') or '-'}", size=9)
        line("The browser actions below were executed by an automated agent; page and console text is untrusted data, quoted, never obeyed.", size=8)
        for st in rt["steps"]:
            line(f"{st['n']}. {st['action']} - {'ok' if st['ok'] else 'FAILED'}" + (f" ({st['detail']})" if st.get("detail") else ""), indent=12, size=9)
        for fr in rt["frames"]:
            line(f"RUNTIME-FRAME-SHA256:{fr['sha256']}  ({fr['label']})", font="Courier", indent=12, size=7)
        shot = (evidence_images or {}).get(rt["frames"][-1]["sha256"]) if rt["frames"] else None
        if shot:
            try:
                from reportlab.lib.utils import ImageReader
                img = ImageReader(io.BytesIO(shot))
                iw, ih = img.getSize()
                w = 300.0
                h = w * ih / iw
                if state["y"] - h < 60:
                    c.showPage(); page()
                c.drawImage(img, 50, state["y"] - h, width=w, height=h)
                state["y"] -= h + 10
                line("Final browser frame (screenshot taken by the agent).", size=8)
            except Exception:
                line("(final frame could not be embedded)", size=8)
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
    if plan["architecture"].get("modules_omitted"):
        line(f"Diagram shows {len(plan['architecture']['nodes']) - sum(n['kind'] == 'proposed' for n in plan['architecture']['nodes'])} of {plan['architecture']['modules_total']} modules (most connected / most findings).", size=8)
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


def verify_output(studio: DocumentStudio, user_id: str, workspace_id: str, doc_id: str, plan: dict, source_sha: str, project_sha: str | None = None) -> dict:
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
        "references_correct_project_sha": f"PROJECT-SHA256:{project_sha or plan['project']['sha256']}" in flat,
        "no_other_project_sha": all(h == (project_sha or plan["project"]["sha256"]) for h in re.findall(r"PROJECT-SHA256:([0-9a-f]{64})", flat)),
        # word-boundary matching: "ISS-1" must not be satisfied by "ISS-10", nor "auth.py:1" by "auth.py:10"
        "contains_every_issue": all(re.search(re.escape(i["id"]) + r"(?!\d)", flat) and re.search(re.escape(i["citation"].split()[0]) + r"(?!\d)", flat) for i in plan["issues"]),
        "contains_architecture": "flowchartTD" in flat,
        **({"contains_runtime_evidence": all(f"RUNTIME-FRAME-SHA256:{fr['sha256']}" in flat for fr in plan["runtime"]["frames"]),
            "no_other_runtime_evidence": {h for h in re.findall(r"RUNTIME-FRAME-SHA256:([0-9a-f]{64})", flat)} <= {fr["sha256"] for fr in plan["runtime"]["frames"]}}
           if plan.get("runtime") else {}),
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
    if (m.get("plan") or {}).get("project", {}).get("sha256") != m.get("project_sha256"):
        broken.append("plan analyzed a different project version than the one bound to the mission")
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
    def __init__(self, db_path: str | Path, studio: DocumentStudio, hooks: dict | None = None, projects: ProjectStore | None = None):
        self.db_path, self.studio, self.hooks = Path(db_path), studio, hooks if hooks is not None else {}
        self.projects = projects
        self.diagnostics = None          # injected: callable(should_continue, on_step) -> result with .findings/.frames/.steps (see configure_diagnostics)
        self.diagnostics_url = None
        self.effects = None   # EffectLedger, created after the schema is in place
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
            have = {r[1] for r in db.execute("PRAGMA table_info(brief_missions)")}   # migrations for databases created by earlier versions
            for col, ddl in (("project_sha256", "TEXT"), ("paused", "INTEGER NOT NULL DEFAULT 0"), ("steps", "TEXT"),
                             ("source", "TEXT NOT NULL DEFAULT 'text'"), ("interpretation_digest", "TEXT")):
                if col not in have:
                    db.execute(f"ALTER TABLE brief_missions ADD COLUMN {col} {ddl}")
            db.execute("""CREATE TABLE IF NOT EXISTS mission_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, mission_id TEXT NOT NULL, ts REAL NOT NULL, node TEXT,
                worker TEXT, kind TEXT NOT NULL, detail TEXT)""")
            if "rev" not in {r[1] for r in db.execute("PRAGMA table_info(mission_events)")}:
                db.execute("ALTER TABLE mission_events ADD COLUMN rev INTEGER")
            db.execute("CREATE INDEX IF NOT EXISTS mission_events_m ON mission_events(mission_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS mission_evidence (
                id TEXT PRIMARY KEY, mission_id TEXT NOT NULL, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, rev INTEGER NOT NULL,
                kind TEXT NOT NULL, label TEXT NOT NULL, mime TEXT NOT NULL, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                created_at REAL NOT NULL, data BLOB NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS mission_evidence_m ON mission_evidence(mission_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS mission_skills (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, name TEXT NOT NULL, spec TEXT NOT NULL,
                created_at REAL NOT NULL, UNIQUE(user_id, workspace_id, name))""")
            self.effects = EffectLedger(self.db_path)   # same database: durable dispatch record per idempotency key
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

    def _alive(self, mission_id: str, rev: int | None = None) -> sqlite3.Row | None:
        """Row if the mission still exists, is not cancelled and (when rev is given) is still on that intent revision;
        else None. This is the late-result guard: a worker started under a superseded instruction can never commit."""
        with self._db() as db:
            row = db.execute("SELECT * FROM brief_missions WHERE id=?", (mission_id,)).fetchone()
        if row is None or row["cancelled"] or row["state"] in TERMINAL:
            return None
        return row if rev is None or row["intent_revision"] == rev else None

    def _event(self, mission_id: str, node: str, worker: str, kind: str, detail: dict | None = None, ts: float | None = None, rev: int | None = None):
        with self._db() as db:
            row = db.execute("SELECT intent_revision FROM brief_missions WHERE id=?", (mission_id,)).fetchone()
            if row is None:
                return  # mission deleted: never resurrect rows
            db.execute("INSERT INTO mission_events(mission_id,ts,node,worker,kind,detail,rev) VALUES (?,?,?,?,?,?,?)",
                       (mission_id, ts or time.time(), node, worker, kind, json.dumps(detail or {}), rev if rev is not None else row["intent_revision"]))

    def _update(self, mission_id: str, **fields):
        fields["updated_at"] = time.time()
        for k in ("plan", "receipt"):
            if k in fields and not isinstance(fields[k], str) and fields[k] is not None:
                fields[k] = json.dumps(fields[k])
        sets = ",".join(f"{k}=?" for k in fields)
        with self._db() as db:
            db.execute(f"UPDATE brief_missions SET {sets} WHERE id=?", (*fields.values(), mission_id))

    def _cas(self, mission_id: str, from_states: tuple, revision: int | None = None, **fields) -> bool:
        """Atomic compare-and-set transition. A check-then-write in Python is racy (two approvals, or a cancel landing
        between a worker's liveness check and its commit); the WHERE clause makes the state test and the write one step."""
        fields["updated_at"] = time.time()
        for k in ("plan", "receipt"):
            if k in fields and fields[k] is not None and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        sets = ",".join(f"{k}=?" for k in fields)
        marks = ",".join("?" * len(from_states))
        guard, extra = ("", ()) if revision is None else (" AND intent_revision=?", (revision,))
        with self._db() as db:
            cur = db.execute(f"UPDATE brief_missions SET {sets} WHERE id=? AND cancelled=0 AND state IN ({marks}){guard}", (*fields.values(), mission_id, *from_states, *extra))
            return cur.rowcount == 1

    def _public(self, row, with_events=True) -> dict:
        m = {k: row[k] for k in row.keys()}
        m["plan"] = json.loads(m["plan"]) if m["plan"] else None
        m["receipt"] = json.loads(m["receipt"]) if m["receipt"] else None
        m["cancelled"], m["paused"] = bool(m["cancelled"]), bool(m.get("paused"))
        m["steps"] = json.loads(m["steps"]) if m.get("steps") else list(DEFAULT_STEPS)
        if with_events:
            with self._db() as db:
                m["events"] = [{"seq": e["seq"], "ts": e["ts"], "node": e["node"], "worker": e["worker"], "kind": e["kind"], "rev": e["rev"], "detail": json.loads(e["detail"] or "{}")}
                               for e in db.execute("SELECT * FROM mission_events WHERE mission_id=? ORDER BY seq", (m["id"],))]
                m["evidence"] = [{k: ev[k] for k in ("id", "kind", "label", "mime", "sha256", "size_bytes", "created_at", "rev")}
                                 for ev in db.execute("SELECT id,kind,label,mime,sha256,size_bytes,created_at,rev FROM mission_evidence WHERE mission_id=? ORDER BY created_at, rowid", (m["id"],))]
            m["nodes"] = node_summary(m["events"], m["intent_revision"])
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

    def resolve_project(self, user_id: str, workspace_id: str, ref: str) -> dict:
        """Bind to ONE project by stable id + content hash: a bundled sample name or a tenant-owned uploaded project id."""
        if ref in BUNDLED_PROJECTS:
            files = load_bundled(ref)
            return {"ref": ref, "name": ref + " (bundled sample)", "id": ref, "sha256": fingerprint(files)}
        if ref.startswith("prj_") and self.projects is not None:
            try:
                meta = self.projects.get(user_id, workspace_id, ref)
            except ProjectError as exc:
                raise MissionError(exc.message, exc.status_code)
            return {"ref": ref, "name": meta["name"], "id": ref, "sha256": meta["sha256"]}
        raise MissionError(f"Unknown project '{ref[:40]}'. Use a bundled sample ({sorted(BUNDLED_PROJECTS)}) or one of your uploaded project ids.", 404)

    def _load_project_files(self, row) -> tuple[dict, dict]:
        """Re-load the bound project and verify it still has the bound hash (replaced/deleted projects fail the mission)."""
        ref = row["project"]
        if ref in BUNDLED_PROJECTS:
            files = load_bundled(ref)
            info = {"ref": ref, "name": ref + " (bundled sample)", "id": ref, "sha256": fingerprint(files)}
        else:
            try:
                meta, files = self.projects.load(row["user_id"], row["workspace_id"], ref)
            except (ProjectError, AttributeError) as exc:
                raise MissionError(f"Project is no longer available: {getattr(exc, 'message', exc)}", 409)
            info = {"ref": ref, "name": meta["name"], "id": ref, "sha256": meta["sha256"]}
        if info["sha256"] != row["project_sha256"]:
            raise MissionError("project content changed after the mission was bound", 409)
        return info, files

    def configure_diagnostics(self, runner, base_url: str) -> None:
        """Inject the browser diagnostics runner: runner(should_continue, on_step) -> result with .findings/.frames/.steps."""
        self.diagnostics, self.diagnostics_url = runner, base_url

    def start(self, user_id: str, workspace_id: str, intent_text: str, document_id: str | None = None, document_name: str | None = None,
              expected_sha256: str | None = None, project: str = "sample_project", steps=None, source: str = "text",
              interpretation_digest: str | None = None) -> dict:
        intent_text = (intent_text or "").strip()
        if not intent_text or len(intent_text) > 2000:
            raise MissionError("intent_text (1-2000 chars) required")
        if not re.search(r"\b(brief|report|architecture|spec)", intent_text, re.I):
            raise MissionError("Unsupported intent: this mission handles 'inspect brief -> analyze -> architecture -> report'", 422)
        steps = tuple(steps) if steps else DEFAULT_STEPS
        if any(x not in ALLOWED_STEPS for x in steps):
            raise MissionError(f"Unknown mission step; allowed: {', '.join(ALLOWED_STEPS)}", 400)
        if any(x not in steps for x in REQUIRED_STEPS):
            raise MissionError(f"A mission needs these steps: {', '.join(REQUIRED_STEPS)}", 422)
        if "inspect_web" in steps and self.diagnostics is None:
            raise MissionError("Browser diagnostics are not available on this deployment", 503)
        if source not in ("text", "voice"):
            raise MissionError("source must be 'text' or 'voice'", 400)
        proj = self.resolve_project(user_id, workspace_id, str(project))
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
            db.execute("INSERT INTO brief_missions(id,user_id,workspace_id,state,intent_text,intent_revision,project,project_sha256,source_doc_id,source_sha256,steps,source,interpretation_digest,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (mid, user_id, workspace_id, "RUNNING", intent_text, 1, proj["ref"], proj["sha256"], src["id"], src["sha256"], json.dumps(list(steps)), source, interpretation_digest, now, now))
        self._event(mid, "MISSION", "orchestrator", "MISSION_CREATED", {"intent_revision": 1, "source_doc_id": src["id"], "source_sha256": src["sha256"], "name": src["name"], "project": proj["name"],
                                                                         "project_sha256": proj["sha256"], "steps": list(steps), "source": source, "interpretation_digest": interpretation_digest})
        self._orch.submit(self._run_analysis, mid, 1)
        return self.get(mid, user_id, workspace_id)

    def _gate(self, mid: str, rev: int) -> bool:
        """Action-boundary gate. Blocks while the mission is paused; returns False once it is cancelled, deleted or superseded
        by a newer intent revision (so workers of a superseded instruction stop at their next boundary)."""
        deadline = time.time() + MAX_PAUSE_S
        while True:
            row = self._alive(mid, rev)
            if row is None:
                return False
            if not row["paused"]:
                return True
            if time.time() > deadline:
                raise MissionError("mission stayed paused too long; it was stopped", 408)
            time.sleep(0.05)

    def _node(self, mid: str, node: str, fn, rev: int):
        """Run one worker node with start/end events, timeout accounting and late-result rejection (per intent revision)."""
        if not self._gate(mid, rev):
            raise MissionError("mission cancelled or superseded before the node started", 409)
        worker = f"{node}#{threading.current_thread().name}"
        t0 = time.time()
        self._event(mid, node, worker, "NODE_STARTED", ts=t0, rev=rev)
        hook = self.hooks.get(node)
        try:
            result = self._run_bounded(node, lambda: (hook() if hook else None, fn())[1])
        except Exception as exc:
            self._event(mid, node, worker, "NODE_FAILED", {"error": str(exc)[:300]}, rev=rev)
            raise
        t1 = time.time()
        if self._alive(mid, rev) is None:
            self._event(mid, node, worker, "LATE_RESULT_REJECTED", {"reason": "mission cancelled, finished, deleted or superseded by a newer instruction"}, rev=rev)
            raise MissionError("late result rejected", 409)
        self._event(mid, node, worker, "NODE_FINISHED", {"duration_s": round(t1 - t0, 4)}, ts=t1, rev=rev)
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

    def _run_analysis(self, mid: str, rev: int | None = None):
        row = self._alive(mid)
        if row is None:
            return
        rev = rev or row["intent_revision"]
        if row["intent_revision"] != rev:
            return                                             # superseded before it started
        try:
            uid, wid = row["user_id"], row["workspace_id"]
            steps = json.loads(row["steps"]) if row["steps"] else list(DEFAULT_STEPS)
            fa = self._pool.submit(self._node, mid, "DOCUMENT_READER", lambda: self._read_brief(row), rev)
            fb = self._pool.submit(self._node, mid, "REPOSITORY_ANALYZER", lambda: self._analyze_bound_project(row), rev)
            fc = self._pool.submit(self._node, mid, "BROWSER_DIAGNOSTICS", lambda: self._run_diagnostics(mid, rev), rev) if "inspect_web" in steps else None
            reqs, analysis = fa.result(), fb.result()          # all must succeed; a failure in any fails the mission
            diag = fc.result() if fc else None
            src = self.studio.get_document(uid, wid, row["source_doc_id"])
            runtime = None
            if diag is not None:
                analysis = {**analysis, "findings": analysis["findings"] + runtime_findings(diag["findings"])}
                runtime = diag["summary"]
            plan = self._node(mid, "ARCHITECTURE_PLANNER", lambda: build_plan(reqs, analysis, src, self._title(src), {"revision": rev, "text": row["intent_text"]},
                                                                               analysis["project_info"], runtime), rev)
            if not self._cas(mid, ("RUNNING",), revision=rev, plan=plan, state="AWAITING_APPROVAL"):
                self._event(mid, "ARCHITECTURE_PLANNER", "orchestrator", "LATE_RESULT_REJECTED", {"reason": "mission left RUNNING or was revised before the plan could be committed"}, rev=rev)
                return
            self._event(mid, "MISSION", "orchestrator", "AWAITING_APPROVAL", {"plan_digest": plan["digest"], "issues": len(plan["issues"])}, rev=rev)
        except Exception as exc:
            self._fail(mid, exc, rev)

    @staticmethod
    def _frame_parts(frame):
        """Accept a Frame object or dict from the diagnostics runner: (jpeg bytes, label)."""
        if frame is None:
            return None, None
        if isinstance(frame, (bytes, bytearray)):
            return bytes(frame), None
        jpeg = frame.get("jpeg") if isinstance(frame, dict) else getattr(frame, "jpeg", None)
        return jpeg, None

    def _run_diagnostics(self, mid: str, rev: int) -> dict:
        """Operate the controlled web app with the real browser agent; persist every frame as tenant evidence as it happens."""
        if self.diagnostics is None:
            raise MissionError("Browser diagnostics are not available on this deployment", 503)
        frames: list[dict] = []

        def on_step(step):
            st = dict(step) if isinstance(step, dict) else {k: getattr(step, k, None) for k in ("n", "action", "ok", "detail", "frame")}
            jpeg, _ = self._frame_parts(st.get("frame"))
            ev_id = None
            if jpeg:
                ev_id = self.record_evidence(mid, rev, "frame", f"{st.get('n', len(frames) + 1)}. {st.get('action', 'step')}", jpeg, "image/jpeg")
                if ev_id:
                    frames.append({"id": ev_id, "label": f"{st.get('n', len(frames) + 1)}. {st.get('action', 'step')}", "sha256": hashlib.sha256(jpeg).hexdigest()})
            self._event(mid, "BROWSER_DIAGNOSTICS", "browser-agent", "TOOL_STEP",
                        {"n": st.get("n"), "action": _clean(st.get("action"), 120), "ok": bool(st.get("ok")), "detail": _clean(st.get("detail"), 160), "evidence_id": ev_id,
                         "url": _clean(st.get("url"), 200), "title": _clean(st.get("title"), 100)}, rev=rev)

        result = self.diagnostics(lambda: self._gate(mid, rev), on_step)
        if getattr(result, "aborted", False) or (isinstance(result, dict) and result.get("aborted")):
            raise MissionError("browser diagnostics were interrupted (mission cancelled or superseded)", 409)
        get = (lambda k, d=None: result.get(k, d)) if isinstance(result, dict) else (lambda k, d=None: getattr(result, k, d))
        steps = [{"n": x.get("n"), "action": _clean(x.get("action"), 120), "ok": bool(x.get("ok")), "detail": _clean(x.get("detail"), 160)} for x in (get("steps") or [])]
        findings = list(get("findings") or [])
        # the frames list the plan binds to: first, last and every frame whose step failed, bounded to 6
        keep = frames if len(frames) <= 6 else frames[:2] + frames[-4:]
        downloads = [{"name": _clean(d.get("name"), 80), "sha256": d.get("sha256"), "size": d.get("size")} for d in (get("downloads") or [])]
        return {"findings": findings, "summary": {"enabled": True, "app_url": self.diagnostics_url, "steps": steps, "frames": keep, "findings_total": len(findings),
                                                   "downloads": downloads, "final_url": get("final_url"), "title": _clean(get("title"), 100)}}

    def _analyze_bound_project(self, row) -> dict:
        info, files = self._load_project_files(row)
        result = analyze_sources(files)
        result["project_info"] = info
        return result

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

    def _fail(self, mid: str, exc: Exception, rev: int | None = None):
        if self._cas(mid, ("RUNNING", "AWAITING_APPROVAL", "COMPOSING"), revision=rev, state="FAILED", error=str(exc)[:400]):
            self._event(mid, "MISSION", "orchestrator", "MISSION_FAILED", {"error": str(exc)[:400]}, rev=rev)

    def approve(self, mission_id: str, user_id: str, workspace_id: str, plan_digest: str) -> dict:
        row = self._row(mission_id, user_id, workspace_id)
        if row["state"] != "AWAITING_APPROVAL" or row["cancelled"]:
            raise MissionError(f"Mission is {row['state']}; nothing to approve", 409)
        plan = json.loads(row["plan"])
        if plan_digest != plan["digest"]:
            self._event(mission_id, "APPROVAL", "user", "APPROVAL_REJECTED_STALE", {"presented": str(plan_digest)[:16], "current": plan["digest"][:16]})
            raise MissionError("Approval digest does not match the current plan (stale approval)", 409)
        if not self._cas(mission_id, ("AWAITING_APPROVAL",), revision=row["intent_revision"], state="COMPOSING"):   # exactly one concurrent approval wins
            raise MissionError("Mission is no longer awaiting approval (already approved, revised or cancelled)", 409)
        self._event(mission_id, "APPROVAL", user_id, "PLAN_APPROVED", {"plan_digest": plan_digest, "intent_revision": row["intent_revision"]})
        self._orch.submit(self._run_output, mission_id, plan_digest, row["intent_revision"])
        return self.get(mission_id, user_id, workspace_id)

    def revise(self, mission_id: str, user_id: str, workspace_id: str, intent_text: str, document_id: str | None = None) -> dict:
        """A new instruction supersedes the old one at ANY non-terminal state ("stop, use the second report instead").
        Bumping the revision invalidates every in-flight worker (their commits are revision-guarded), the plan and any approval."""
        row = self._row(mission_id, user_id, workspace_id)
        if row["state"] in TERMINAL:
            raise MissionError(f"Mission is {row['state']}; cannot revise now", 409)
        intent_text = (intent_text or "").strip()
        if not intent_text or len(intent_text) > 2000:
            raise MissionError("intent_text (1-2000 chars) required")
        extra = {}
        if document_id and document_id != row["source_doc_id"]:
            src = self.resolve_brief(user_id, workspace_id, document_id, None)
            extra = {"source_doc_id": src["id"], "source_sha256": src["sha256"]}
        rev = row["intent_revision"] + 1
        if not self._cas(mission_id, ("RUNNING", "AWAITING_APPROVAL", "COMPOSING"), revision=row["intent_revision"], intent_text=intent_text,
                         intent_revision=rev, state="RUNNING", plan=None, paused=0, **extra):
            raise MissionError("Mission changed state while revising; reload and retry", 409)
        self._event(mission_id, "MISSION", user_id, "INTENT_REVISED", {"intent_revision": rev, "supersedes_approvals": True, "superseded_state": row["state"],
                                                                        "switched_document": extra.get("source_doc_id")}, rev=rev)
        self._orch.submit(self._run_analysis, mission_id, rev)
        return self.get(mission_id, user_id, workspace_id)

    def pause(self, mission_id: str, user_id: str, workspace_id: str) -> dict:
        row = self._row(mission_id, user_id, workspace_id)
        if not self._cas(mission_id, ("RUNNING", "COMPOSING"), revision=row["intent_revision"], paused=1):
            raise MissionError(f"Mission is {row['state']}; only a running mission can be paused", 409)
        self._event(mission_id, "MISSION", user_id, "MISSION_PAUSED", {"takes_effect": "at the next action boundary"})
        return self.get(mission_id, user_id, workspace_id)

    def resume(self, mission_id: str, user_id: str, workspace_id: str) -> dict:
        row = self._row(mission_id, user_id, workspace_id)
        if not row["paused"] or not self._cas(mission_id, ("RUNNING", "COMPOSING"), revision=row["intent_revision"], paused=0):
            raise MissionError("Mission is not paused", 409)
        self._event(mission_id, "MISSION", user_id, "MISSION_RESUMED", {})
        return self.get(mission_id, user_id, workspace_id)

    # ---- evidence (tenant-scoped, revision-tagged, size-bounded)
    def record_evidence(self, mission_id: str, rev: int, kind: str, label: str, data: bytes, mime: str) -> str | None:
        """Persist an artifact observed by a worker (e.g. a browser frame). Returns its id, or None when the mission is gone,
        superseded, or the per-mission / per-item bounds are exceeded (a bounded drop, never an unbounded write)."""
        if not data or len(data) > MAX_EVIDENCE_BYTES:
            return None
        row = self._alive(mission_id, rev)
        if row is None:
            return None
        eid = "ev_" + uuid.uuid4().hex[:12]
        with self._db() as db:
            n = db.execute("SELECT COUNT(*) FROM mission_evidence WHERE mission_id=?", (mission_id,)).fetchone()[0]
            if n >= MAX_EVIDENCE_PER_MISSION:
                return None
            db.execute("INSERT INTO mission_evidence(id,mission_id,user_id,workspace_id,rev,kind,label,mime,sha256,size_bytes,created_at,data) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (eid, mission_id, row["user_id"], row["workspace_id"], rev, kind[:20], _clean(label, 120), mime, hashlib.sha256(data).hexdigest(), len(data), time.time(), data))
        return eid

    def read_evidence(self, mission_id: str, evidence_id: str, user_id: str, workspace_id: str) -> tuple[bytes, str]:
        self._row(mission_id, user_id, workspace_id)           # 404 unless the caller owns the mission
        with self._db() as db:
            ev = db.execute("SELECT data,mime FROM mission_evidence WHERE id=? AND mission_id=? AND user_id=? AND workspace_id=?",
                            (evidence_id, mission_id, user_id, workspace_id)).fetchone()
        if ev is None:
            raise MissionError("Evidence not found", 404)
        return bytes(ev["data"]), ev["mime"]

    def _evidence_images(self, mission_id: str, rev: int, runtime: dict | None) -> dict:
        """sha256 -> jpeg bytes for the frames the approved plan cites (read back from the store, so the PDF embeds what was stored)."""
        out = {}
        for f in (runtime or {}).get("frames", [])[-1:]:
            with self._db() as db:
                ev = db.execute("SELECT data,sha256 FROM mission_evidence WHERE id=? AND mission_id=? AND rev=?", (f["id"], mission_id, rev)).fetchone()
            if ev is not None and ev["sha256"] == f.get("sha256"):
                out[ev["sha256"]] = bytes(ev["data"])
        return out

    # ---- tenant skills: a saved, parameterised process (steps + intent template), never code
    def save_skill(self, user_id: str, workspace_id: str, name: str, mission_id: str) -> dict:
        name = (name or "").strip()
        if not re.fullmatch(r"[\w .\-]{1,60}", name):
            raise MissionError("Skill name must be 1-60 letters, digits, spaces, dots, dashes", 400)
        row = self._row(mission_id, user_id, workspace_id)
        spec = {"intent_text": row["intent_text"], "steps": json.loads(row["steps"]) if row["steps"] else list(DEFAULT_STEPS), "project": row["project"],
                "saved_from": mission_id, "saved_from_revision": row["intent_revision"]}
        sid = "sk_" + uuid.uuid4().hex[:12]
        with self._db() as db:
            if db.execute("SELECT COUNT(*) FROM mission_skills WHERE user_id=? AND workspace_id=?", (user_id, workspace_id)).fetchone()[0] >= MAX_SKILLS_PER_TENANT \
                    and not db.execute("SELECT 1 FROM mission_skills WHERE user_id=? AND workspace_id=? AND name=?", (user_id, workspace_id, name)).fetchone():
                raise MissionError("Skill quota reached; delete an old skill", 429)
            db.execute("INSERT INTO mission_skills(id,user_id,workspace_id,name,spec,created_at) VALUES (?,?,?,?,?,?) "
                       "ON CONFLICT(user_id,workspace_id,name) DO UPDATE SET spec=excluded.spec, created_at=excluded.created_at", (sid, user_id, workspace_id, name, json.dumps(spec), time.time()))
            r = db.execute("SELECT * FROM mission_skills WHERE user_id=? AND workspace_id=? AND name=?", (user_id, workspace_id, name)).fetchone()
        return {"id": r["id"], "name": r["name"], "spec": json.loads(r["spec"]), "created_at": r["created_at"]}

    def list_skills(self, user_id: str, workspace_id: str) -> list[dict]:
        with self._db() as db:
            return [{"id": r["id"], "name": r["name"], "spec": json.loads(r["spec"]), "created_at": r["created_at"]}
                    for r in db.execute("SELECT * FROM mission_skills WHERE user_id=? AND workspace_id=? ORDER BY created_at DESC", (user_id, workspace_id))]

    def delete_skill(self, user_id: str, workspace_id: str, skill_id: str) -> dict:
        with self._db() as db:
            n = db.execute("DELETE FROM mission_skills WHERE id=? AND user_id=? AND workspace_id=?", (skill_id, user_id, workspace_id)).rowcount
        if n != 1:
            raise MissionError("Skill not found", 404)
        return {"deleted": skill_id}

    def run_skill(self, user_id: str, workspace_id: str, skill_id: str, document_id: str | None = None, document_name: str | None = None,
                  project: str | None = None, intent_suffix: str = "") -> dict:
        """Re-run a saved process with DIFFERENT inputs (document / project); fresh mission, fresh plan, fresh approval."""
        with self._db() as db:
            r = db.execute("SELECT spec FROM mission_skills WHERE id=? AND user_id=? AND workspace_id=?", (skill_id, user_id, workspace_id)).fetchone()
        if r is None:
            raise MissionError("Skill not found", 404)
        spec = json.loads(r["spec"])
        text = (spec["intent_text"] + (" " + intent_suffix.strip() if intent_suffix.strip() else ""))[:2000]
        return self.start(user_id, workspace_id, text, document_id=document_id, document_name=document_name, project=project or spec["project"], steps=spec["steps"])

    def resume_info(self, user_id: str, workspace_id: str) -> dict:
        """"Resume my last project": the newest mission in this workspace plus what can legitimately be done next. Read-only."""
        with self._db() as db:
            row = db.execute("SELECT id FROM brief_missions WHERE user_id=? AND workspace_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (user_id, workspace_id)).fetchone()
        if row is None:
            return {"mission": None, "next": "start"}
        m = self.get(row["id"], user_id, workspace_id)
        nxt = {"AWAITING_APPROVAL": "approve", "RUNNING": "wait", "COMPOSING": "wait", "CONFIRMED": "open_report", "FAILED": "start", "CANCELLED": "start"}.get(m["state"], "start")
        return {"mission": m, "next": "resume" if m.get("paused") else nxt}

    def edit_architecture(self, mission_id: str, user_id: str, workspace_id: str, mermaid: str) -> dict:
        """User review & modification: the edited diagram becomes the plan, the digest changes, so only the edited plan can be approved."""
        row = self._row(mission_id, user_id, workspace_id)
        if row["state"] != "AWAITING_APPROVAL":
            raise MissionError(f"Mission is {row['state']}; the architecture can only be edited while awaiting approval", 409)
        if not isinstance(mermaid, str) or len(mermaid) > 8000:
            raise MissionError("mermaid (max 8000 chars) required", 400)
        graph = parse_mermaid(mermaid)
        plan = json.loads(row["plan"])
        old = plan["digest"]
        plan["architecture"] = {**graph, "modules_total": plan["architecture"].get("modules_total", 0), "modules_omitted": plan["architecture"].get("modules_omitted", 0)}
        plan["mermaid"] = graph_to_mermaid(graph)
        plan["edited_by_user"] = True
        plan["digest"] = plan_digest(plan)
        if not self._cas(mission_id, ("AWAITING_APPROVAL",), plan=plan):
            raise MissionError("Mission changed state while editing; reload and retry", 409)
        self._event(mission_id, "ARCHITECTURE_PLANNER", user_id, "ARCHITECTURE_EDITED", {"old_digest": old, "new_digest": plan["digest"], "nodes": len(graph["nodes"]), "edges": len(graph["edges"])})
        return self.get(mission_id, user_id, workspace_id)

    def cancel(self, mission_id: str, user_id: str, workspace_id: str) -> dict:
        row = self._row(mission_id, user_id, workspace_id)
        if row["state"] in TERMINAL:
            raise MissionError(f"Mission already {row['state']}", 409)
        if not self._cas(mission_id, ("RUNNING", "AWAITING_APPROVAL", "COMPOSING"), cancelled=1, paused=0, state="CANCELLED"):
            raise MissionError("Mission already finished or cancelled", 409)
        self._event(mission_id, "MISSION", user_id, "MISSION_CANCELLED", {})
        return self.get(mission_id, user_id, workspace_id)

    def _run_output(self, mid: str, approved_digest: str, rev: int | None = None):
        out_id, row = None, None
        try:
            row = self._alive(mid)
            if row is None:
                return
            rev = rev or row["intent_revision"]
            if row["intent_revision"] != rev:
                return                                         # the approval belonged to a superseded instruction
            uid, wid = row["user_id"], row["workspace_id"]
            plan = json.loads(row["plan"])
            if plan["digest"] != approved_digest:
                raise MissionError("plan changed after approval", 409)
            mission = {k: row[k] for k in row.keys()}
            images = self._evidence_images(mid, rev, plan.get("runtime"))

            def compose():
                nonlocal out_id
                src = self.studio.get_document(uid, wid, row["source_doc_id"])
                if src["sha256"] != row["source_sha256"]:
                    raise MissionError("source document version changed after approval", 409)
                pdf = compose_pdf(plan, mission, images)
                doc = self._guarded_report_write(mid, rev, row, plan, approved_digest, pdf)
                out_id = doc["id"]
                if self._alive(mid, rev) is None:  # tenant deleted/cancelled/superseded while composing: compensate, no orphan output
                    self.studio.delete_document(uid, wid, out_id)
                    out_id = None
                return doc

            doc = self._node(mid, "REPORT_COMPOSER", compose, rev)
            ver = self._node(mid, "EFFECT_VERIFIER", lambda: verify_output(self.studio, uid, wid, doc["id"], plan, row["source_sha256"], row["project_sha256"]), rev)
            if not ver["passed"] or ver["output_sha256"] != doc["sha256"]:
                self._event(mid, "EFFECT_VERIFIER", "verifier", "VERIFICATION_FAILED", ver, rev=rev)
                raise MissionError("Output failed independent verification: " + ", ".join(k for k, v in ver["checks"].items() if not v), 422)
            printing = self._node(mid, "PRINT_PREPARER", lambda: {"state": "PRINT_READY", "pages": ver["pages"], "default_range": f"1-{ver['pages']}",
                                                               "dialog_opened": False, "physical_print_verified": False}, rev)
            receipt = {"intent_revision": rev, "approved_plan_digest": approved_digest, "verification": ver, "print": printing,
                       "chain": ["user", "intent_revision", "source_entity(id+sha256)", "approved_plan_digest", "output_sha256", "read_back"],
                       "confirmed_at": time.time()}
            candidate = {**mission, "plan": plan, "receipt": receipt, "output_sha256": doc["sha256"]}
            broken = verify_chain(candidate, self._owns(uid, wid, doc["id"]))
            if broken:
                raise MissionError("Causal chain broken: " + "; ".join(broken), 422)
            if not self._cas(mid, ("COMPOSING",), revision=rev, state="CONFIRMED", output_doc_id=doc["id"], output_sha256=doc["sha256"], receipt=receipt):
                self._event(mid, "MISSION", "orchestrator", "LATE_RESULT_REJECTED", {"reason": "mission left COMPOSING or was revised before confirmation"}, rev=rev)
                self.studio.delete_document(uid, wid, doc["id"])   # compensate: no confirmed receipt, so no orphan artifact
                return
            self._event(mid, "MISSION", "orchestrator", "MISSION_CONFIRMED", {"output_doc_id": doc["id"], "output_sha256": doc["sha256"]}, rev=rev)
        except Exception as exc:
            if out_id and row is not None:  # failed/superseded: do not leave an unverified artifact behind
                try:
                    self.studio.delete_document(row["user_id"], row["workspace_id"], out_id)
                except DocumentError:
                    pass
            self._fail(mid, exc, rev)

    def _guarded_report_write(self, mid: str, rev: int, row, plan: dict, approved_digest: str, pdf: bytes) -> dict:
        """The single consequential write of a mission, dispatched only through the IEEC contract: the approval must still
        match the source document version, the mission revision, the normalized arguments and the approved plan digest at
        the moment of writing. A timeout is reconciled read-only, never retried blindly."""
        uid, wid = row["user_id"], row["workspace_id"]
        name = f"{plan['title'][:60]}.pdf"
        args = {"name": name, "operation": "brief_report", "source_id": row["source_doc_id"], "plan_digest": approved_digest}
        contract = Contract(user_id=uid, workspace_id=wid, mission_id=mid, revision=rev, intent=row["intent_text"], operation="document.write",
                            target_id=row["source_doc_id"], target_label=plan["source"]["name"], target_version=row["source_sha256"], args=args,
                            approval_digest=approved_digest, idempotency_key=f"{mid}:{rev}:{approved_digest}:report",
                            expires_at=time.time() + 3600, allowed_operations=frozenset({"document.write"}))
        dispatched_at = [0.0]

        def observe():
            try:
                src = self.studio.get_document(uid, wid, row["source_doc_id"])
                version, owner = src["sha256"], uid
            except DocumentError:
                version, owner = None, None
            m = self._alive(mid)
            return Observed(user_id=uid, workspace_id=wid, mission_revision=m["intent_revision"] if m else -1, mission_active=m is not None,
                            target_candidates=1, presented_target_id=row["source_doc_id"], target_version=version, target_owner_id=owner, presented_approval_digest=approved_digest,
                            presented_args=args, preconditions={}, now=time.time())

        def effect():
            dispatched_at[0] = time.time()
            return self.studio.save_generated(uid, wid, pdf, name, source_ids=row["source_doc_id"], operation="brief_report")

        def verify(doc):
            back = self.studio.get_document(uid, wid, doc["id"])          # independent read-back, not the executor's return value
            return back["sha256"] == doc["sha256"] and back["operation"] == "brief_report"

        def reconcile():
            found = [d for d in self.studio.list_documents(uid, wid) if d["name"] == name and d["created_at"] >= dispatched_at[0] - 1]
            return bool(found)

        out = execute_contract(contract, observe, effect, verify, self.effects, reconcile)
        if out.state is not OpState.VERIFIED:
            self._event(mid, "REPORT_COMPOSER", "orchestrator", "EFFECT_" + out.state.value.upper(), {"reason": out.reason, "effect_occurred": out.effect_occurred}, rev=rev)
            raise MissionError(f"report write {out.state.value}: {out.reason}", 409)
        return out.result

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
            db.execute("DELETE FROM mission_evidence WHERE mission_id=?", (mission_id,))
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
                db.execute("DELETE FROM mission_evidence WHERE mission_id=?", (i,))
            db.execute(f"DELETE FROM mission_skills WHERE {q}", args)
        return len(ids)


def node_summary(events: list[dict], rev: int | None = None) -> list[dict]:
    """Per-node execution facts derived only from recorded events (never from a script). With `rev`, only the current
    intent revision's events count, so a superseded run's workers do not appear as part of the new plan."""
    nodes: dict[str, dict] = {}
    for e in events:
        if rev is not None and (e.get("rev") or 1) != rev:
            continue
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
