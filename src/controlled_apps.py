"""Three controlled web applications with PERSISTED records, for generalized computer-use tests and demos.

  /pm/        Project Management Board   tasks load asynchronously; status updates are version-checked (409 when stale)
  /research/  Document & Research Portal several versions of similar documents; reports are saved as real rows
  /crm/       Customer / Contact Workspace  people with the same first name in different departments

Each app keeps its records in SQLite so a verifier can read the ground truth DIRECTLY, outside the browser and
outside the code that drove it. Layouts, names, row ordering, load delays and document versions differ per app.
Page text is data: nothing here grants anything to anyone. Loopback only; started explicitly by tests and the demo.
"""
from __future__ import annotations

import html
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DEMO_TODAY = "2026-10-30"
LOAD_DELAY_MS = 250

TASKS = [
    # id, project, title, status, due, owner
    (1, "Atlas", "Ship onboarding flow", "in_progress", "2026-10-20", "Priya"),
    (2, "Atlas", "Fix invoice rounding", "todo", "2026-10-25", "Marco"),
    (3, "Atlas", "Write launch notes", "todo", "2026-11-12", "Priya"),
    (4, "Atlas", "Security review", "done", "2026-10-10", "Lena"),
    (5, "Atlas", "Migrate legacy accounts", "in_progress", "2026-10-29", "Marco"),
    (6, "Borealis", "Vendor contract draft", "todo", "2026-10-15", "Sam"),
    (7, "Borealis", "Kickoff deck", "in_progress", "2026-11-03", "Sam"),
    (8, "Borealis", "Budget sign-off", "todo", "2026-10-28", "Lena"),
]
DOCS = [
    (1, "Market Report Q3", "market", 1, "2026-07-02", "DRAFT. Market grew 3.1%. Competitor A led. Superseded."),
    (2, "Market Report Q3", "market", 2, "2026-08-19", "REVIEWED. Market grew 4.0%. Competitor B entered the segment."),
    (3, "Market Report Q3", "market", 3, "2026-10-21", "FINAL. Market grew 4.6%. Churn fell to 2.2%. Competitor B gained 5 points of share."),
    (4, "Market Report Q2", "market", 3, "2026-05-30", "FINAL. Market grew 2.9%. Churn was 2.8%."),
    (5, "Supplier Audit", "operations", 1, "2026-09-11", "FINAL. Two suppliers failed the quality check."),
]
CONTACTS = [
    (1, "Alex Rivera", "Finance", "alex.rivera@example.test", "Controller"),
    (2, "Alex Chen", "Sales", "alex.chen@example.test", "Account Executive"),
    (3, "Alexa Morgan", "Finance", "alexa.morgan@example.test", "Analyst"),
    (4, "Jordan Park", "Sales", "jordan.park@example.test", "Manager"),
]

CSS = "body{font:16px system-ui;margin:2rem;background:#faf7f0;color:#272722}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.4rem .7rem;text-align:left}.badge{background:#c8a977;border-radius:.5rem;padding:0 .4rem}"


def _page(title: str, body: str, script: str = "") -> bytes:
    return (f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title><style>{CSS}</style><h1>{html.escape(title)}</h1>{body}"
            f"<script>{script}</script>").encode()


class ControlledApps:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript("""
              CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, project TEXT, title TEXT, status TEXT, due TEXT, owner TEXT, version INTEGER DEFAULT 1);
              CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY, title TEXT, topic TEXT, version INTEGER, updated TEXT, body TEXT);
              CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, body TEXT, source_doc_id INTEGER, source_version INTEGER, created REAL);
              CREATE TABLE IF NOT EXISTS contacts(id INTEGER PRIMARY KEY, name TEXT, department TEXT, email TEXT, role TEXT);
            """)
            if not c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]:
                c.executemany("INSERT INTO tasks(id,project,title,status,due,owner) VALUES (?,?,?,?,?,?)", TASKS)
                c.executemany("INSERT INTO docs VALUES (?,?,?,?,?,?)", DOCS)
                c.executemany("INSERT INTO contacts VALUES (?,?,?,?,?)", CONTACTS)

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    # ---- ground truth, for verifiers (never used by the pages themselves to decide anything)
    def task(self, task_id: int) -> dict:
        with self._conn() as c:
            r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(r) if r else {}

    def reports(self) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM reports ORDER BY id")]

    def verify(self, kind: str, args: dict) -> dict:
        """Independent verifier: reads the application's database, never the page."""
        with self._conn() as c:
            if kind == "overdue_tasks":
                return {"titles": [r["title"] for r in c.execute("SELECT title FROM tasks WHERE project=? AND due<? AND status!='done'", (args["project"], DEMO_TODAY))]}
            if kind == "task_by_title":
                r = c.execute("SELECT id, version FROM tasks WHERE project=? AND title=?", (args["project"], args["title"])).fetchone()
                return dict(r) if r else {"id": None, "version": None}
            if kind == "task":
                return self.task(args["id"])
            if kind == "document":
                r = c.execute("SELECT * FROM docs WHERE id=?", (args["id"],)).fetchone()
                latest = c.execute("SELECT MAX(updated) m FROM docs WHERE title=?", (r["title"],)).fetchone()["m"]
                return {"version": r["version"], "body": r["body"], "is_latest_of_family": r["updated"] == latest}
            if kind == "reports":
                return {"reports": [dict(x) for x in c.execute("SELECT * FROM reports")]}
            if kind == "contact":
                return dict(c.execute("SELECT * FROM contacts WHERE id=?", (args["id"],)).fetchone())
        raise KeyError(kind)

    def force_task_version_bump(self, task_id: int) -> None:
        """Test hook: simulate another user editing the task after it was read."""
        with self._conn() as c:
            c.execute("UPDATE tasks SET version=version+1 WHERE id=?", (task_id,))

    # ---- HTTP
    def handle(self, method: str, raw_path: str, body: bytes) -> tuple[int, str, bytes]:
        u = urlparse(raw_path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        p = u.path
        try:
            if p.startswith("/pm"):
                return self._pm(method, p, q, body)
            if p.startswith("/research"):
                return self._research(method, p, q, body)
            if p.startswith("/crm"):
                return self._crm(method, p, q, body)
        except (KeyError, ValueError, json.JSONDecodeError):
            return 400, "application/json", b'{"error":"bad request"}'
        return 404, "text/plain", b"not found"

    def _pm(self, method, p, q, body):
        if method == "GET" and p in ("/pm", "/pm/"):
            project = q.get("project", "Atlas")
            script = f"""fetch('/pm/api/tasks?project='+encodeURIComponent({json.dumps(project)})).then(r=>r.json()).then(d=>setTimeout(()=>{{
              const tb=document.getElementById('tasks'); document.getElementById('loading').remove();
              d.tasks.forEach(t=>{{const od=t.due<d.today&&t.status!=='done'; const tr=document.createElement('tr'); tr.dataset.taskId=t.id; tr.dataset.version=t.version;
                tr.innerHTML='<td>'+t.title+'</td><td>'+t.owner+'</td><td>'+t.due+(od?' <span class=badge>overdue</span>':'')+'</td><td><select aria-label="status">'+['todo','in_progress','done'].map(s=>'<option'+(s===t.status?' selected':'')+'>'+s+'</option>').join('')+'</select> <button>Save</button></td>';
                tr.querySelector('button').onclick=()=>fetch('/pm/api/tasks/'+t.id,{{method:'POST',body:JSON.stringify({{status:tr.querySelector('select').value,expected_version:t.version}})}}).then(r=>r.json()).then(x=>{{document.getElementById('result').textContent=x.error?('ERROR: '+x.error):('Saved '+t.title+' as '+x.status);}});
                tb.appendChild(tr);}});}},{LOAD_DELAY_MS}));"""
            return 200, "text/html; charset=utf-8", _page(f"Project {project}", f'<p id=loading>Loading tasks…</p><table><thead><tr><th>Task</th><th>Owner</th><th>Due</th><th>Status</th></tr></thead><tbody id=tasks></tbody></table><p id=result role=status></p><p><a href="/pm/?project=Atlas">Atlas</a> · <a href="/pm/?project=Borealis">Borealis</a></p>', script)
        if method == "GET" and p == "/pm/api/tasks":
            with self._conn() as c:
                rows = [dict(r) for r in c.execute("SELECT * FROM tasks WHERE project=? ORDER BY title DESC" if q.get("project") == "Borealis" else "SELECT * FROM tasks WHERE project=? ORDER BY due", (q.get("project", "Atlas"),))]
            return 200, "application/json", json.dumps({"today": DEMO_TODAY, "tasks": rows}).encode()
        if method == "POST" and p.startswith("/pm/api/tasks/"):
            tid = int(p.rsplit("/", 1)[1])
            d = json.loads(body or b"{}")
            if d["status"] not in ("todo", "in_progress", "done"):
                return 422, "application/json", b'{"error":"bad status"}'
            with self._lock, self._conn() as c:
                cur = c.execute("UPDATE tasks SET status=?, version=version+1 WHERE id=? AND version=?", (d["status"], tid, int(d["expected_version"])))
                if cur.rowcount == 0:
                    return 409, "application/json", b'{"error":"the task changed since it was read"}'
            return 200, "application/json", json.dumps({"id": tid, "status": d["status"]}).encode()
        return 404, "text/plain", b"not found"

    def _research(self, method, p, q, body):
        if method == "GET" and p in ("/research", "/research/"):
            term = q.get("q", "").lower()
            with self._conn() as c:
                rows = [r for r in c.execute("SELECT * FROM docs ORDER BY title, version") if term in (r["title"] + r["topic"]).lower()]
            lines = "".join(f'<tr data-doc-id="{r["id"]}"><td><a href="/research/doc/{r["id"]}">{html.escape(r["title"])}</a></td><td>v{r["version"]}</td><td>{r["updated"]}</td></tr>' for r in rows)
            return 200, "text/html; charset=utf-8", _page("Research portal", f'<form><input name=q aria-label="search" value="{html.escape(q.get("q", ""))}"><button>Search</button></form><table><thead><tr><th>Document</th><th>Version</th><th>Updated</th></tr></thead><tbody>{lines}</tbody></table>')
        if method == "GET" and p.startswith("/research/doc/"):
            with self._conn() as c:
                r = c.execute("SELECT * FROM docs WHERE id=?", (int(p.rsplit("/", 1)[1]),)).fetchone()
            if not r:
                return 404, "text/plain", b"not found"
            return 200, "text/html; charset=utf-8", _page(f'{r["title"]} v{r["version"]}', f'<article id=body data-doc-id="{r["id"]}" data-version="{r["version"]}">{html.escape(r["body"])}</article><p>Updated {r["updated"]}</p><textarea id=report-body aria-label="report text" rows=4></textarea><p><button id=save-report>Save report to workspace</button></p><p id=save-result role=status></p><p><a href="/research/">Back</a></p>', f"""document.getElementById('save-report').onclick=()=>fetch('/research/api/reports',{{method:'POST',body:JSON.stringify({{title:document.querySelector('h1').textContent+' summary',body:document.getElementById('report-body').value,source_doc_id:{r['id']},source_version:{r['version']}}})}}).then(x=>x.json()).then(x=>{{document.getElementById('save-result').textContent=x.error?('ERROR: '+x.error):('Saved report '+x.report_id)}});""")
        if method == "POST" and p == "/research/api/reports":
            d = json.loads(body)
            with self._conn() as c:
                doc = c.execute("SELECT version FROM docs WHERE id=?", (int(d["source_doc_id"]),)).fetchone()
                if not doc or doc["version"] != int(d["source_version"]):
                    return 409, "application/json", b'{"error":"source version changed"}'
                cur = c.execute("INSERT INTO reports(title,body,source_doc_id,source_version,created) VALUES (?,?,?,?,?)", (d["title"][:200], d["body"][:5000], int(d["source_doc_id"]), int(d["source_version"]), time.time()))
            return 200, "application/json", json.dumps({"report_id": cur.lastrowid}).encode()
        return 404, "text/plain", b"not found"

    def _crm(self, method, p, q, body):
        if method == "GET" and p in ("/crm", "/crm/"):
            term = q.get("q", "").lower()
            with self._conn() as c:
                rows = [r for r in c.execute("SELECT * FROM contacts ORDER BY department DESC, name") if term in r["name"].lower()]
            cards = "".join(f'<li data-contact-id="{r["id"]}"><a href="/crm/contact/{r["id"]}">{html.escape(r["name"])}</a> <span>{html.escape(r["department"])}</span> <em>{html.escape(r["role"])}</em></li>' for r in rows)
            return 200, "text/html; charset=utf-8", _page("Contacts", f'<form><input name=q aria-label="search people" value="{html.escape(q.get("q", ""))}"><button>Find</button></form><ul>{cards}</ul>')
        if method == "GET" and p.startswith("/crm/contact/"):
            with self._conn() as c:
                r = c.execute("SELECT * FROM contacts WHERE id=?", (int(p.rsplit("/", 1)[1]),)).fetchone()
            if not r:
                return 404, "text/plain", b"not found"
            return 200, "text/html; charset=utf-8", _page(r["name"], f'<dl id=card data-contact-id="{r["id"]}"><dt>Department</dt><dd>{html.escape(r["department"])}</dd><dt>Email</dt><dd>{html.escape(r["email"])}</dd><dt>Role</dt><dd>{html.escape(r["role"])}</dd></dl>')
        return 404, "text/plain", b"not found"


def serve_controlled(db_path: str, host: str = "127.0.0.1"):
    apps = ControlledApps(db_path)

    class H(BaseHTTPRequestHandler):
        def _do(self, method):
            n = int(self.headers.get("Content-Length") or 0)
            status, ctype, data = apps.handle(method, self.path, self.rfile.read(min(n, 20000)) if n else b"")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._do("GET")

        def do_POST(self):
            self._do("POST")

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer((host, 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://{host}:{server.server_address[1]}", apps
