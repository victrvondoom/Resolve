"""Local RESOLVE HTTP service. All mutations require a same-origin session token."""
from __future__ import annotations

import json
import base64
import binascii
import os
import re
import secrets
import threading
import sqlite3
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from resolve_core import Action, Ledger, digest, reconcile_uncertain_local, resolve_candidates, seed_demo, uid, verify_outbox
from agent_runtime import Cancelled, LeaseLost, MissionScheduler, TaskSpec, register_demo_readers
from capability_registry import WORKSPACES, concepts_for_workspace, load_registry
from skill_runtime import AmbiguousEntity, SkillError, SkillStore, candidate_from_confirmed
import windows_adapter
import uia_adapter
from web_api import WebApi
from tenant_auth import TenantAuthManager, TenantError
from sandbox_manager import SandboxManager, SandboxError, SandboxSecurityError
from cloud_browser import CloudBrowserPool, CloudBrowserError, CloudBrowserSession
from effect_graph import EffectDependencyGraph, NodeType, EdgeType
from connectors import ConnectorRegistry, ConnectorError
from privacy_engine import PrivacyEngine
from document_studio import DocumentStudio, DocumentError
from brief_mission import BriefMissionStore, MissionError, BUNDLED_PROJECTS, load_bundled
from command_router import interpret as interpret_command
from demo_webapp import DemoWebApp
from write_audit import WriteAudit
from document_studio import DocumentError
from computer_tasks import BROWSER_CAPS, ComputerTaskRunner, TaskRefused, browser_services
from controlled_apps import serve_controlled
from net_policy import NetPolicy, origin_of
from tool_registry import ToolContext, default_registry
from project_store import ProjectStore, ProjectError, fingerprint
from voice_engine import VoiceEngine, VoiceError
from server_security import ServerSecurity, RateLimiter, SECURITY_HEADERS, ConfigError, client_ip

DB = Path(os.environ.get('RESOLVE_DB', ROOT / 'data' / 'resolve.sqlite3'))
ledger = Ledger(DB)
seed_demo(ledger)
reconcile_uncertain_local(ledger)
SESSION_TOKEN = secrets.token_urlsafe(32)
scheduler = MissionScheduler(ledger.path, revision_check=lambda mid, revision: bool((mission := ledger.get_json('missions', mid)) and mission['status'] == 'ACTIVE' and mission['intent_revision'] == revision))
register_demo_readers(scheduler, ledger)
skill_store = SkillStore(ledger.path)
web_api = WebApi(ledger)

tenant_auth_mgr = TenantAuthManager()
sandbox_mgr = SandboxManager()
cloud_browser_pool = CloudBrowserPool()
connector_registry = ConnectorRegistry()
privacy_engine = PrivacyEngine(tenant_auth_mgr, sandbox_mgr, connector_registry)
document_studio = DocumentStudio(Path(os.environ.get('RESOLVE_DOCUMENTS_DIR', ROOT / 'data' / 'documents')))
default_demo = tenant_auth_mgr.seed_demo_tenant()
project_store = ProjectStore(document_studio.base_dir)
brief_missions = BriefMissionStore(Path(os.environ.get('RESOLVE_MISSIONS_DB', document_studio.base_dir / 'missions.sqlite3')), document_studio, projects=project_store)
privacy_engine.mission_store = brief_missions
privacy_engine.project_store = project_store
privacy_engine.browser_pool = cloud_browser_pool
SECURITY = ServerSecurity.from_env()
rate_limiter = RateLimiter()
voice_engine = VoiceEngine()
demo_webapp = DemoWebApp()
write_audit = WriteAudit(document_studio.base_dir / 'write_audit.sqlite3')
privacy_engine.audit_store = write_audit
tool_registry = default_registry()
_diag_slots = threading.BoundedSemaphore(2)       # at most two real Chromium diagnostics runs at once


def diagnostics_wanted() -> bool:
    """Demo mode: on when Playwright is importable. Production: only on explicit RESOLVE_DIAGNOSTICS=1 (it launches Chromium in this process,
    pointed ONLY at the bundled demo app; arbitrary-URL browsing stays gated by the cloud-browser isolation setting)."""
    flag = os.environ.get('RESOLVE_DIAGNOSTICS', '').strip()
    if flag == '0' or (SECURITY.production and flag != '1'):
        return False
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def enable_diagnostics(port: int) -> bool:
    """Give the mission engine a runner that drives a real Chromium against /demo-app/ on THIS server (exact-origin allowlist, nothing else)."""
    if not diagnostics_wanted():
        return False
    from diagnostics_agent import run_diagnostics
    base = f'http://127.0.0.1:{port}'
    policy = NetPolicy(allowed_origins=frozenset({origin_of(base)}))

    def runner(should_continue, on_step):
        with _diag_slots:
            return run_diagnostics(base + '/demo-app/', policy=policy, should_continue=should_continue, on_step=on_step)
    brief_missions.configure_diagnostics(runner, base + '/demo-app/')
    return True
# Routes backed by the single shared demo ledger (not tenant-scoped): demo mode only.
LEGACY_GLOBAL_PREFIXES = ('/api/state', '/api/stream', '/api/web/', '/api/approvals', '/api/evidence', '/api/mission', '/api/missions/',
                          '/api/benchmark', '/api/skills', '/api/windows', '/api/uia', '/api/session', '/api/action/', '/api/resolve', '/api/resume',
                          '/api/emergency-stop', '/api/agents/discover')


def browser_for(sbx, user):
    """Cloud browser access. Production requires an explicitly declared isolated runtime: a Playwright context
    inside the web process is not tenant-grade isolation, so it is refused rather than silently offered."""
    if SECURITY.production and not SECURITY.cloud_browser_enabled:
        raise ApiError(503, 'cloud browser is disabled on this deployment: no isolated per-tenant runtime is configured (RESOLVE_CLOUD_BROWSER_ISOLATION)')
    return cloud_browser_pool.get_or_create(sbx.id, user['user_id'], sbx.storage_path)


def write_boundary(user: dict, operation: str, target_id, fn, digest_of=None):
    """Single boundary for direct document writes. Records every attempt: allowed, rejected (wrong tenant, missing
    document) or failed, with the content hash before and after. The write itself runs only inside this boundary."""
    uid, wid = user['user_id'], user['workspace_id']
    before = None
    if target_id:
        try:
            before = document_studio.get_document(uid, wid, target_id)['sha256']
        except DocumentError:
            # Not owned by this tenant (or absent): refuse, and do NOT persist the foreign id in this tenant's audit trail.
            write_audit.record(uid, wid, operation, None, 'REJECTED', reason='target not found in this workspace')
            raise DocumentError('Document not found', 404)
    try:
        result = fn()
    except DocumentError as exc:
        write_audit.record(uid, wid, operation, target_id, 'REJECTED', before_sha256=before, reason=str(exc))
        raise
    except Exception as exc:
        write_audit.record(uid, wid, operation, target_id, 'FAILED', before_sha256=before, reason=type(exc).__name__)
        raise
    after = digest_of(result) if digest_of else None
    write_audit.record(uid, wid, operation, target_id, 'ALLOWED', before_sha256=before, after_sha256=after)
    return result


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def require(condition: bool, status: int, message: str) -> None:
    if not condition:
        raise ApiError(status, message)


def latest_report(ledger: Ledger, context: str) -> dict:
    reports = resolve_candidates(ledger, 'document', context=context)
    reports = [report for report in reports if report.get('modified_at')]
    require(bool(reports), 409, 'no report with a modification timestamp is available')
    return max(reports, key=lambda report: (report['modified_at'], report.get('version') or ''))


class Handler(BaseHTTPRequestHandler):
    server_version = 'RESOLVE/0.2'

    _bare = False

    def end_headers(self):
        if not self._bare:
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
        super().end_headers()

    def serve_demo_app(self, method: str) -> None:
        """The controlled faulty web app. Only the server's own browser agent (loopback) may load it; it carries no tenant data."""
        require(self.client_address[0] in {'127.0.0.1', '::1'}, 403, 'the demo web app is only served to the local diagnostics agent')
        size = int(self.headers.get('Content-Length', '0') or 0)
        body = self.rfile.read(min(size, 65536)) if size > 0 else b''
        status, headers, payload = demo_webapp.handle(method, self.path, body, dict(self.headers))
        self._bare = True
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        print('%s - %s' % (self.address_string(), fmt % args))

    def send_json(self, status: int, data: dict) -> None:
        raw = json.dumps(data).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def get_session(self) -> dict:
        auth_h = self.headers.get('Authorization', '')
        res_h = self.headers.get('X-Resolve-Token', '')
        token = auth_h[7:].strip() if auth_h.startswith('Bearer ') else res_h.strip()
        if token.startswith('rst_'):
            try:
                return tenant_auth_mgr.validate_session(token)
            except TenantError as exc:
                raise ApiError(401, exc.message)
        elif not SECURITY.production and secrets.compare_digest(token, SESSION_TOKEN):
            return default_demo
        elif token:
            try:
                return tenant_auth_mgr.validate_session(token)
            except Exception:
                pass
        if SECURITY.production:
            raise ApiError(401, 'authentication required')
        return default_demo

    def check_origin(self, mutation: bool = False, public_auth: bool = False) -> None:
        port = self.server.server_port
        if not SECURITY.production:
            require(self.client_address[0] in {'127.0.0.1', '::1'}, 403, 'loopback access only')
        require(SECURITY.host_allowed(self.headers.get('Host'), port), 403, 'invalid Host header')
        origin = self.headers.get('Origin')
        if origin:
            host = self.headers.get('Host')
            require(origin in {f'http://{host}', f'https://{host}'}, 403, 'cross-origin request refused')
        path_only = self.path.split('?')[0]
        if SECURITY.production and (path_only.startswith(LEGACY_GLOBAL_PREFIXES) or path_only == '/api/voice/transcribe'):
            raise ApiError(404, 'route disabled in production: backed by the shared demo ledger, not tenant-scoped')
        if mutation:
            require(self.headers.get('Content-Type', '').split(';')[0].strip().lower() == 'application/json', 415, 'JSON content type required')
            if not public_auth:
                auth_h = self.headers.get('Authorization', '')
                res_h = self.headers.get('X-Resolve-Token', '')
                token = auth_h[7:].strip() if auth_h.startswith('Bearer ') else res_h.strip()
                if token.startswith('rst_'):
                    try:
                        tenant_auth_mgr.validate_session(token)
                    except TenantError as exc:
                        raise ApiError(401, exc.message)
                else:
                    require(not SECURITY.production, 401, 'authentication required')
                    require(secrets.compare_digest(token, SESSION_TOKEN), 403, 'session token required')

    def body(self, max_size: int = 32768) -> dict:
        size = int(self.headers.get('Content-Length', '0'))
        require(0 <= size <= max_size, 413, 'request too large')
        data = json.loads(self.rfile.read(size) or b'{}')
        require(isinstance(data, dict), 400, 'JSON object required')
        return data

    def document_user(self) -> dict:
        """Require an explicit valid token for private document reads."""
        auth = self.headers.get('Authorization', '')
        token = auth[7:].strip() if auth.startswith('Bearer ') else self.headers.get('X-Resolve-Token', '').strip()
        require(bool(token), 401, 'session token required')
        if not SECURITY.production and secrets.compare_digest(token, SESSION_TOKEN):
            return default_demo
        try:
            return tenant_auth_mgr.validate_session(token)
        except TenantError as exc:
            raise ApiError(401, exc.message)

    # ---- spoken/typed commands: interpret (pure) -> confirm -> execute against CURRENT state, bound by digest
    @staticmethod
    def command_state(user: dict) -> dict:
        uid, wid = user['user_id'], user['workspace_id']
        missions = brief_missions.list(uid, wid)                       # newest first
        active = next((m for m in missions if m['state'] in ('RUNNING', 'AWAITING_APPROVAL', 'COMPOSING')), None)
        slim = lambda m: m and {k: m.get(k) for k in ('id', 'state', 'intent_revision', 'paused', 'source_doc_id', 'output_doc_id', 'output_sha256')}
        docs = [{'id': d['id'], 'name': d['name'], 'sha256': d['sha256']} for d in document_studio.list_documents(uid, wid) if d.get('operation') != 'brief_report']
        return {'active_mission': slim(active), 'latest_output_mission': slim(next((m for m in missions if m['state'] == 'CONFIRMED' and m.get('output_doc_id')), None)),
                'last_mission': slim(missions[0] if missions else None), 'documents': docs,
                'skills': [{'id': k['id'], 'name': k['name']} for k in brief_missions.list_skills(uid, wid)]}

    def handle_command(self, execute: bool, user: dict, data: dict) -> dict:
        uid, wid = user['user_id'], user['workspace_id']
        text, source = str(data.get('text', '')), str(data.get('source', 'text'))
        require(source in ('text', 'voice'), 400, 'source must be text or voice')
        interp = interpret_command(text, self.command_state(user), source=source)
        if not execute:
            return {'interpretation': interp}
        require(data.get('digest') == interp['digest'], 409, 'the situation changed since this command was interpreted (stale digest); please confirm again')
        require(interp['command'] != 'UNKNOWN', 422, interp['summary'])
        require(not interp['ambiguity'], 409, 'the command is ambiguous; choose one option first')
        require(data.get('confirmed') is True or not interp['requires_confirmation'], 409, 'this command needs explicit confirmation')
        a, cmd = interp['args'], interp['command']
        if cmd == 'START_MISSION':
            docs = a.get('document_candidates') or []
            require(len(docs) >= 1, 409, 'no document is available; upload or create the sample brief first')
            require(len(docs) == 1 or data.get('document_id') in docs, 409, 'several documents exist; choose one by id', )
            steps = a['steps'] if all(x in ('read_brief', 'analyze_code', 'inspect_web', 'architecture', 'report', 'print') for x in a['steps']) else None
            steps = list(dict.fromkeys(['read_brief', 'analyze_code', 'architecture', 'report', *(steps or [])]))
            result = brief_missions.start(uid, wid, text, document_id=data.get('document_id') or docs[0], steps=steps, source=source, interpretation_digest=interp['digest'])
        elif cmd == 'PAUSE':
            result = brief_missions.pause(a['mission_id'], uid, wid)
        elif cmd == 'RESUME':
            result = brief_missions.resume(a['mission_id'], uid, wid)
        elif cmd == 'CANCEL':
            result = brief_missions.cancel(a['mission_id'], uid, wid)
        elif cmd == 'REVISE_DOCUMENT':
            cur = brief_missions.get(a['mission_id'], uid, wid)
            require(cur['intent_revision'] == a['intent_revision'], 409, 'the mission was revised since this command was interpreted')
            result = brief_missions.revise(a['mission_id'], uid, wid, cur['intent_text'] + f" (user switched the brief to {a.get('document_name')})", document_id=a['document_id'])
        elif cmd == 'DELETE_REPORT':
            cur = brief_missions.get(a['mission_id'], uid, wid)
            require(cur.get('output_doc_id') == a['document_id'] and cur.get('output_sha256') == a.get('output_sha256'), 409, 'the report changed since this command was interpreted')
            result = brief_missions.delete(a['mission_id'], uid, wid, delete_output=True)
        elif cmd == 'SAVE_SKILL':
            result = {'skill': brief_missions.save_skill(uid, wid, a['name'], a['mission_id'])}
        elif cmd == 'RUN_SKILL':
            result = brief_missions.run_skill(uid, wid, a['skill_id'], document_id=data.get('document_id') or (self.command_state(user)['documents'] or [{}])[0].get('id'))
        elif cmd == 'RESUME_PROJECT':
            result = brief_missions.resume_info(uid, wid)
        else:   # READ_RESULT
            result = brief_missions.get(a['mission_id'], uid, wid)
        return {'interpretation': interp, 'executed': cmd, 'result': result}

    def handle_computer_run(self, user: dict, data: dict) -> dict:
        """Run ONE supported computer task in a real Chromium against this tenant's own controlled apps (own port, own database).
        Capabilities are fixed here; consequential tasks return a proposal first and write only when its exact digest is presented."""
        require(diagnostics_wanted(), 503, 'computer tasks are disabled on this deployment (they launch Chromium in this process)')
        command = data.get('command')
        require(isinstance(command, str) and 3 <= len(command) <= 400, 422, 'command must be 3 to 400 characters')
        digest_in = data.get('approved_digest')
        require(digest_in is None or (isinstance(digest_in, str) and re.fullmatch(r'[0-9a-f]{64}', digest_in)), 422, 'approved_digest must be a sha256 hex string')
        uid, wid = user['user_id'], user['workspace_id']
        require(rate_limiter.allow('computer', uid, 20), 429, 'too many computer tasks')
        root = Path(document_studio.base_dir) / 'computer' / uid
        root.mkdir(parents=True, exist_ok=True)
        with _diag_slots:
            server, base, apps = serve_controlled(str(root / 'apps.sqlite3'))
            session = None
            try:
                import tempfile
                session = CloudBrowserSession(f'cmp_{uid[:20]}', uid, tempfile.mkdtemp(prefix='cmp_', dir=root), policy=NetPolicy(allowed_origins=frozenset({origin_of(base)})))
                trail: list[dict] = []
                ctx = ToolContext(user_id=uid, workspace_id=wid, mission_id=None, capabilities=BROWSER_CAPS, services=browser_services(session),
                                  record=lambda rec: (trail.append(rec), f'tool_{len(trail)}')[1])
                runner = ComputerTaskRunner(tool_registry, ctx, {'pm': base, 'research': base, 'crm': base}, apps.verify)
                try:
                    result = runner.run(command, approved_digest=digest_in)
                except TaskRefused as exc:
                    raise ApiError(422, f'{exc.code}: {exc.message}')
                frame = session.capture_frame()
                result['frame'] = {'data_url': frame.get('frame'), 'url': frame.get('url'), 'title': frame.get('title')}
                result['evidence'] = [{k: r.get(k) for k in ('tool', 'ok', 'error_code', 'args_sha256')} for r in trail]
                result['app_origin'] = origin_of(base)
                return result
            finally:
                if session:
                    session.close()
                server.shutdown()

    def handle_tool_call(self, user: dict, data: dict) -> dict:
        """Typed tool call. Capabilities are fixed here, server-side; nothing in the arguments or in page content can add one."""
        uid, wid = user['user_id'], user['workspace_id']
        caps = frozenset({'document:read', 'document:write', 'mission:control', 'approval:request', 'evidence:write', 'workspace:manage'})
        mid = data.get('mission_id') if isinstance(data.get('mission_id'), str) else None
        services = {
            'document.read': lambda c, a: {'text': document_studio.extract_text(uid, wid, a['document_id'])[:20000]},
            'document.transform': lambda c, a: {'document': document_studio.transform(uid, wid, a['operation'], a['document_ids'], a['expected_sha256'], degrees=a.get('degrees'), pages=a.get('pages'))},
            'document.generate': lambda c, a: {'document': document_studio.create_report(uid, wid, a['title'], a['text'])},
            'document.verify': lambda c, a: {'sha256_matches': document_studio.get_document(uid, wid, a['document_id'])['sha256'] == a['expected_sha256'],
                                             'pages': len(__import__('pypdf').PdfReader(__import__('io').BytesIO(document_studio.read_pdf(uid, wid, a['document_id']))).pages)},
            'workspace.create': lambda c, a: {'workspace': tenant_auth_mgr.create_workspace(uid, a['name'])},
            'workspace.export': lambda c, a: {'documents': len(document_studio.list_documents(uid, wid)), 'missions': len(brief_missions.list(uid, wid)),
                                              'skills': len(brief_missions.list_skills(uid, wid))},
            'mission.pause': lambda c, a: {'mission': brief_missions.pause(a['mission_id'], uid, wid)},
            'mission.cancel': lambda c, a: {'mission': brief_missions.cancel(a['mission_id'], uid, wid)},
            'approval.request': lambda c, a: self._approval_request(uid, wid, a),
        }
        if mid:
            services['evidence.record'] = lambda c, a: {'evidence_id': brief_missions.record_evidence(mid, brief_missions.get(mid, uid, wid)['intent_revision'], 'note', a['kind'], a['note'].encode(), 'text/plain')}
        trail: list[dict] = []
        ctx = ToolContext(user_id=uid, workspace_id=wid, mission_id=mid, capabilities=caps, services=services, record=lambda rec: (trail.append(rec), f'tool_{len(trail)}')[1])
        args = data.get('args') if isinstance(data.get('args'), dict) else {}
        res = tool_registry.call(ctx, str(data.get('name', '')), args)
        return {'tool': res.tool, 'ok': res.ok, 'data': res.data, 'error': res.error, 'started_at': res.started_at, 'ended_at': res.ended_at,
                'args_sha256': res.args_sha256, 'evidence_ref': res.evidence_ref}

    @staticmethod
    def _approval_request(uid, wid, a):
        m = brief_missions.get(a['mission_id'], uid, wid)
        require(m['state'] == 'AWAITING_APPROVAL' and m['plan'] and m['plan']['digest'] == a['plan_digest'], 409, 'no matching plan is awaiting approval')
        return {'awaiting_user_approval': True, 'plan_digest': a['plan_digest'], 'note': 'a tool can only request approval; the user must approve in the UI'}

    def do_GET(self):
        try:
            if urlparse(self.path).path in {'/api/health', '/api/system/health'}:  # platform probes use arbitrary Host headers; nothing sensitive
                return self.send_json(200, {'status': 'ok', 'name': 'RESOLVE', 'version': '0.2', 'database': 'sqlite-wal', 'mode': SECURITY.mode,
                                            'features': {'speech_server': voice_engine.health()['available'], 'cloud_browser': SECURITY.cloud_browser_enabled, 'diagnostics': brief_missions.diagnostics is not None}})
            if urlparse(self.path).path.startswith('/demo-app/'):
                return self.serve_demo_app('GET')
            self.check_origin()
            parsed = urlparse(self.path)
            path = parsed.path
            if path == '/api/stream':
                q = parse_qs(parsed.query)
                after = q.get('after', [self.headers.get('Last-Event-ID', '0')])[0] or '0'
                require(bool(re.fullmatch(r'\d+(\.\d+)?', after)), 400, 'bad stream cursor')
                return web_api.stream(self, after, min(float(q.get('max_s', ['300'])[0]), 3600))
            if path.startswith('/api/web/'):
                status, body = web_api.call('GET', path, {})
                return self.send_json(status, body)
            pages = {'/': 'landing.html', '/app': 'index.html', '/app/': 'index.html'}
            static = re.fullmatch(r'/static/([A-Za-z0-9_.-]+\.(js|css|svg))', path)
            if static:
                asset = ROOT / 'web' / 'static' / static.group(1)
                require(asset.is_file(), 404, 'asset not found')
                raw = asset.read_bytes()
                kind = {'js': 'text/javascript', 'css': 'text/css', 'svg': 'image/svg+xml'}[static.group(2)]
                self.send_response(200)
                self.send_header('Content-Type', kind + '; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if path in pages:
                page = ROOT / 'web' / pages[path]
                raw = (page if page.exists() else ROOT / 'web' / 'index.html').read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if path in {'/api/health', '/api/system/health'}:
                return self.send_json(200, {'status': 'ok', 'name': 'RESOLVE', 'version': '0.2', 'database': 'sqlite-wal'})
            if path == '/api/capabilities':
                from catalog import load_catalog, summary
                entries = load_catalog()
                return self.send_json(200, {'summary': summary(entries), 'concepts': entries})
            if path == '/api/agents':
                from supervisor import ROLES
                agents = [{'role': r, 'tools': sorted(c), 'read_only': ro} for r, (c, ro) in ROLES.items()]
                return self.send_json(200, {'agents': agents, 'count': len(agents)})
            if path == '/api/approvals':
                pending = [a for a in ledger.all_actions() if a.get('status') == 'NEEDS_APPROVAL']
                return self.send_json(200, {'approvals': pending, 'count': len(pending)})
            if path == '/api/evidence':
                evidence_dir = ROOT / 'data' / 'evidence'
                screens_dir = ROOT / 'harness' / 'evidence' / 'screens'
                items = []
                if evidence_dir.exists():
                    for p in evidence_dir.glob('*.png'):
                        items.append({'name': p.name, 'path': f'/api/evidence/{p.name}', 'size_bytes': p.stat().st_size, 'source': 'runtime'})
                if screens_dir.exists():
                    for p in screens_dir.glob('*.png'):
                        items.append({'name': p.name, 'path': f'/api/evidence/{p.name}', 'size_bytes': p.stat().st_size, 'source': 'screens'})
                return self.send_json(200, {'evidence': items, 'count': len(items)})
            if path == '/api/session' and not SECURITY.production:
                return self.send_json(200, {'token': SESSION_TOKEN})
            if path == '/api/state':
                return self.send_json(200, {'missions': ledger.all_json('missions'), 'entities': ledger.all_json('entities'), 'actions': ledger.all_actions(), 'events': ledger.events(), 'task_events': scheduler.events(), 'outbox': ledger.outbox(), 'followups': ledger.followups()})
            if path == '/api/mission/history':
                mission_id = parse_qs(parsed.query).get('mission_id', ['mission_demo'])[0]
                return self.send_json(200, {'mission_id': mission_id, 'revisions': ledger.mission_history(mission_id)})
            if path == '/api/benchmark':
                report = ROOT / 'data' / 'benchmarks' / 'aggregate.json'
                require(report.exists(), 404, 'no benchmark run has been recorded')
                return self.send_json(200, json.loads(report.read_text(encoding='utf-8')))
            if path == '/api/workspaces' and parse_qs(parsed.query).get('tenant'):
                # The signed-in user's own workspaces (the concept catalog below is a different thing).
                user = self.document_user()
                return self.send_json(200, {'workspaces': tenant_auth_mgr.list_workspaces(user['user_id']), 'active_workspace_id': user['workspace_id']})
            if path == '/api/workspaces':
                workspace = parse_qs(parsed.query).get('workspace', [None])[0]
                if workspace:
                    require(workspace in WORKSPACES, 400, 'unknown workspace')
                    return self.send_json(200, {'workspace': workspace, 'concepts': concepts_for_workspace(workspace)})
                concepts = load_registry()
                return self.send_json(200, {'total_concepts': len(concepts), 'workspaces': [{'name': name, 'planned_concepts': sum(item['resolve_workspace'] == name for item in concepts)} for name in sorted(WORKSPACES)]})
            if path == '/api/skills':
                return self.send_json(200, {'skills': skill_store.list_skills()})
            if path == '/api/documents':
                user = self.document_user()
                return self.send_json(200, {'documents': document_studio.list_documents(user['user_id'], user['workspace_id'])})
            m_doc = re.fullmatch(r'/api/documents/([A-Za-z0-9_-]+)/download', path)
            if m_doc:
                user = self.document_user()
                raw = document_studio.read_pdf(user['user_id'], user['workspace_id'], m_doc.group(1))
                self.send_response(200)
                self.send_header('Content-Type', 'application/pdf')
                self.send_header('Content-Disposition', 'attachment; filename="resolve-document.pdf"')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if path == '/api/auth/me':
                user = self.get_session()
                return self.send_json(200, {k: v for k, v in user.items() if k != 'token'})   # the client already holds its token
            if path == '/api/sandboxes':
                user = self.get_session()
                sandboxes = sandbox_mgr.list_sandboxes(user['user_id'])
                return self.send_json(200, {'sandboxes': [s.public_dict() for s in sandboxes]})
            m_sbx = re.fullmatch(r'/api/sandboxes/([A-Za-z0-9_-]+)', path)
            if m_sbx:
                sbx_id = m_sbx.group(1)
                user = self.get_session()
                sbx = sandbox_mgr.get_sandbox(sbx_id, caller_user_id=user['user_id'])
                return self.send_json(200, sbx.public_dict())
            m_sbx_fr = re.fullmatch(r'/api/sandboxes/([A-Za-z0-9_-]+)/frame', path)
            if m_sbx_fr:
                sbx_id = m_sbx_fr.group(1)
                user = self.get_session()
                sbx = sandbox_mgr.get_sandbox(sbx_id, caller_user_id=user['user_id'])
                session = browser_for(sbx, user)
                frame = session.capture_frame()
                return self.send_json(200, frame)
            if path == '/api/connectors':
                user = self.get_session()
                connectors = connector_registry.list_connectors(user['user_id'], user['workspace_id'])
                return self.send_json(200, {'connectors': connectors})
            m_conn_res = re.fullmatch(r'/api/connectors/([A-Za-z0-9_-]+)/resources', path)
            if m_conn_res:
                conn_id = m_conn_res.group(1)
                user = self.get_session()
                conn = connector_registry.get_connector(user['user_id'], user['workspace_id'], conn_id)
                return self.send_json(200, {'connector_id': conn_id, 'resources': conn.list_authorized_resources()})
            if path == '/api/brief-missions':
                user = self.document_user()
                return self.send_json(200, {'missions': brief_missions.list(user['user_id'], user['workspace_id'])})
            m_ev = re.fullmatch(r'/api/brief-missions/([A-Za-z0-9_-]+)/evidence/([A-Za-z0-9_-]+)', path)
            if m_ev:
                user = self.document_user()
                data_b, mime = brief_missions.read_evidence(m_ev.group(1), m_ev.group(2), user['user_id'], user['workspace_id'])
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Cache-Control', 'private, no-store')
                self.send_header('Content-Length', str(len(data_b)))
                self.end_headers()
                return self.wfile.write(data_b)
            if path == '/api/write-audit':
                user = self.document_user()
                return self.send_json(200, {'writes': write_audit.list(user['user_id'], user['workspace_id'])})
            if path == '/api/brief-skills':
                user = self.document_user()
                return self.send_json(200, {'skills': brief_missions.list_skills(user['user_id'], user['workspace_id'])})
            if path == '/api/brief-resume':
                user = self.document_user()
                return self.send_json(200, brief_missions.resume_info(user['user_id'], user['workspace_id']))
            if path == '/api/tools':
                self.document_user()
                return self.send_json(200, {'tools': [{'name': t.name, 'summary': t.summary, 'capability': t.capability, 'timeout_s': t.timeout_s, 'consequential': t.consequential,
                                                       'input': {k: {'type': f.type, 'required': f.required} for k, f in t.input.items()}} for t in map(tool_registry.get, tool_registry.names())]})
            m_bm = re.fullmatch(r'/api/brief-missions/([A-Za-z0-9_-]+)', path)
            if m_bm:
                user = self.document_user()
                return self.send_json(200, brief_missions.get(m_bm.group(1), user['user_id'], user['workspace_id']))
            if path == '/api/projects':
                user = self.document_user()
                bundled = [{'id': n, 'name': n + ' (bundled sample)', 'files_count': len(f), 'sha256': fingerprint(f), 'bundled': True}
                           for n in sorted(BUNDLED_PROJECTS) for f in [load_bundled(n)]]
                return self.send_json(200, {'bundled': bundled, 'uploaded': project_store.list(user['user_id'], user['workspace_id'])})
            if path == '/api/voice/health':
                self.get_session()
                return self.send_json(200, voice_engine.health())
            if path == '/api/privacy/summary':
                user = self.get_session()
                return self.send_json(200, privacy_engine.get_summary(user['user_id'], user['workspace_id']))
            m_m_gr = re.fullmatch(r'/api/missions/([A-Za-z0-9_-]+)/graph', path)
            if m_m_gr:
                m_id = m_m_gr.group(1)
                user = self.get_session()
                graph = EffectDependencyGraph(m_id, user['user_id'], user['workspace_id'])
                m = ledger.get_json('missions', m_id)
                if m:
                    graph.add_node(f"intent_{m.get('intent_revision', 1)}", NodeType.INTENT, {'text': m.get('original_request', 'Execute mission')})
                    for aid in m.get('actions', []):
                        a = ledger.action(aid)
                        if a:
                            graph.add_node(a['action_id'], NodeType.ACTION, a)
                            if a.get('target_entity_id'):
                                ent = ledger.get_json('entities', a['target_entity_id'])
                                if ent:
                                    graph.add_node(ent['entity_id'], NodeType.ENTITY, ent)
                                    graph.add_edge(f"intent_{m.get('intent_revision', 1)}", ent['entity_id'], EdgeType.BINDS_TO)
                                    graph.add_edge(a['action_id'], ent['entity_id'], EdgeType.DEPENDS_ON)
                            if a.get('status') in ('AUTHORIZED', 'CONFIRMED'):
                                app_id = f"app_{a['action_id']}"
                                graph.add_node(app_id, NodeType.APPROVAL, {'digest': a.get('approval_digest') or digest(a)})
                                graph.add_edge(a['action_id'], app_id, EdgeType.AUTHORIZED_BY)
                                eff_id = f"eff_{a['action_id']}"
                                graph.add_node(eff_id, NodeType.EFFECT, {'status': a['status'], 'expected_digest': a.get('approval_digest')})
                                graph.add_edge(eff_id, app_id, EdgeType.AUTHORIZED_BY)
                                ev_id = f"ev_{a['action_id']}"
                                graph.add_node(ev_id, NodeType.EVIDENCE, {'oracle': 'ledger_truth', 'receipt': a.get('evidence')})
                                graph.add_edge(ev_id, eff_id, EdgeType.CONFIRMS)
                return self.send_json(200, graph.to_dict())
            if path == '/api/windows':
                return self.send_json(200, windows_adapter.list_windows())
            if path == '/api/uia/availability':
                return self.send_json(200, uia_adapter.availability())
            if path.startswith('/api/evidence/'):
                require(secrets.compare_digest(self.headers.get('X-Resolve-Token', ''), SESSION_TOKEN), 403, 'session token required')
                name = path.removeprefix('/api/evidence/')
                require(bool(re.fullmatch(r'[A-Za-z0-9_-]+\.png', name)), 400, 'invalid evidence name')
                image_path = ROOT / 'data' / 'evidence' / name
                if not image_path.is_file():
                    image_path = ROOT / 'harness' / 'evidence' / 'screens' / name
                require(image_path.is_file(), 404, 'evidence not found')
                raw = image_path.read_bytes()
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            raise ApiError(404, 'not found')
        except ApiError as exc:
            self.send_json(exc.status, {'error': exc.message})
        except MissionError as exc:
            self.send_json(exc.status_code, {'error': exc.message, **({'candidates': exc.candidates} if exc.candidates else {})})
        except VoiceError as exc:
            self.send_json(exc.status_code, {'error': exc.message, 'code': exc.code})
        except (TenantError, SandboxError, CloudBrowserError, ConnectorError, DocumentError, ProjectError) as exc:
            self.send_json(getattr(exc, 'status_code', 400), {'error': exc.message if hasattr(exc, 'message') else str(exc)})
        except Exception as exc:
            self.send_json(503, {'error': str(exc)})

    def do_POST(self):
        try:
            path = urlparse(self.path).path
            if path.startswith('/demo-app/'):
                return self.serve_demo_app('POST')
            if path in {'/api/auth/register', '/api/auth/login'}:
                self.check_origin(mutation=True, public_auth=True)
                require(rate_limiter.allow('auth', client_ip(self.client_address[0], self.headers.get('X-Forwarded-For', ''), SECURITY.trusted_proxy), SECURITY.auth_rate_per_min), 429, 'too many authentication attempts; retry later')
                data = self.body()
                if path == '/api/auth/register':
                    session = tenant_auth_mgr.register(data.get('email', ''), data.get('password', ''), data.get('display_name'))
                    return self.send_json(201, session)
                elif path == '/api/auth/login':
                    session = tenant_auth_mgr.login(data.get('email', ''), data.get('password', ''))
                    return self.send_json(200, session)

            self.check_origin(mutation=True)
            data = self.body(max_size=7 * 1024 * 1024 if path in ('/api/documents/upload', '/api/projects/upload') else 3 * 1024 * 1024 if path == '/api/voice/transcribe-audio' else 32768)
            user = self.get_session()

            if path == '/api/documents/upload':
                try:
                    raw = base64.b64decode(data.get('content_base64', ''), validate=True)
                except (ValueError, binascii.Error):
                    raise ApiError(400, 'invalid base64 PDF')
                require(len(raw) <= 5 * 1024 * 1024, 413, 'PDF exceeds 5 MiB')
                doc = write_boundary(user, 'document.import', None, lambda: {'document': document_studio.import_pdf(user['user_id'], user['workspace_id'], raw, str(data.get('name', 'document.pdf')))},
                                     digest_of=lambda r: r['document']['sha256'])['document']
                return self.send_json(201, {'document': doc})
            if path == '/api/documents/transform':
                ids = data.get('document_ids', []) if isinstance(data.get('document_ids'), list) else []
                doc = write_boundary(user, 'document.transform:' + str(data.get('operation', ''))[:20], ids[0] if len(ids) == 1 else None,
                    lambda: {'document': document_studio.transform(user['user_id'], user['workspace_id'],
                        str(data.get('operation', '')), ids,
                        data.get('expected_sha256', {}), degrees=data.get('degrees'), pages=data.get('pages'))},
                    digest_of=lambda r: r['document']['sha256'])
                return self.send_json(201, doc)
            if path == '/api/projects/upload':
                try:
                    raw = base64.b64decode(data.get('content_base64', ''), validate=True)
                except (ValueError, binascii.Error):
                    raise ApiError(400, 'invalid base64 archive')
                require(rate_limiter.allow('project', user['user_id'], 10), 429, 'too many project uploads; retry shortly')
                return self.send_json(201, {'project': project_store.add(user['user_id'], user['workspace_id'], str(data.get('name', 'project.zip')), raw)})
            m_pd = re.fullmatch(r'/api/projects/([A-Za-z0-9_-]+)/delete', path)
            if m_pd:
                project_store.delete(user['user_id'], user['workspace_id'], m_pd.group(1))
                return self.send_json(200, {'deleted': m_pd.group(1)})
            if path == '/api/documents/sample-brief':
                doc = write_boundary(user, 'document.seed_sample', None, lambda: {'document': brief_missions.seed_sample_brief(user['user_id'], user['workspace_id'])},
                                     digest_of=lambda r: r['document']['sha256'])
                return self.send_json(201, doc)
            m_dd = re.fullmatch(r'/api/documents/([A-Za-z0-9_-]+)/delete', path)
            if m_dd:
                write_boundary(user, 'document.delete', m_dd.group(1), lambda: document_studio.delete_document(user['user_id'], user['workspace_id'], m_dd.group(1)))
                return self.send_json(200, {'deleted': m_dd.group(1)})
            if path == '/api/brief-missions':
                require(rate_limiter.allow('mission', user['user_id'], 10), 429, 'too many missions started; retry shortly')
                mission = brief_missions.start(user['user_id'], user['workspace_id'], str(data.get('intent_text', '')),
                    document_id=data.get('document_id'), document_name=data.get('document_name'),
                    expected_sha256=data.get('expected_sha256'), project=str(data.get('project', 'sample_project')),
                    steps=data.get('steps') if isinstance(data.get('steps'), list) else None, source=str(data.get('source', 'text')),
                    interpretation_digest=data.get('interpretation_digest') if isinstance(data.get('interpretation_digest'), str) else None)
                return self.send_json(202, mission)
            if path == '/api/brief-skills':
                return self.send_json(201, {'skill': brief_missions.save_skill(user['user_id'], user['workspace_id'], str(data.get('name', '')), str(data.get('mission_id', '')))})
            m_sk = re.fullmatch(r'/api/brief-skills/([A-Za-z0-9_-]+)/(run|delete)', path)
            if m_sk:
                if m_sk.group(2) == 'delete':
                    return self.send_json(200, brief_missions.delete_skill(user['user_id'], user['workspace_id'], m_sk.group(1)))
                require(rate_limiter.allow('mission', user['user_id'], 10), 429, 'too many missions started; retry shortly')
                return self.send_json(202, brief_missions.run_skill(user['user_id'], user['workspace_id'], m_sk.group(1), document_id=data.get('document_id'),
                                                                    document_name=data.get('document_name'), project=data.get('project'), intent_suffix=str(data.get('intent_suffix', ''))))
            if path in ('/api/commands/interpret', '/api/commands/execute'):
                return self.send_json(200, self.handle_command(path.endswith('execute'), user, data))
            if path == '/api/tools/call':
                return self.send_json(200, self.handle_tool_call(user, data))
            if path == '/api/computer/run':
                out = self.handle_computer_run(user, data)
                return self.send_json(409 if out.get('status') == 'REFUSED' else 200, out)
            m_bma = re.fullmatch(r'/api/brief-missions/([A-Za-z0-9_-]+)/(approve|cancel|revise|print-event|delete|edit-architecture|pause|resume)', path)
            if m_bma:
                mid, action = m_bma.groups()
                uid, wid = user['user_id'], user['workspace_id']
                if action == 'approve':
                    return self.send_json(200, brief_missions.approve(mid, uid, wid, str(data.get('plan_digest', ''))))
                if action == 'cancel':
                    return self.send_json(200, brief_missions.cancel(mid, uid, wid))
                if action == 'revise':
                    return self.send_json(200, brief_missions.revise(mid, uid, wid, str(data.get('intent_text', '')), document_id=data.get('document_id')))
                if action == 'pause':
                    return self.send_json(200, brief_missions.pause(mid, uid, wid))
                if action == 'resume':
                    return self.send_json(200, brief_missions.resume(mid, uid, wid))
                if action == 'edit-architecture':
                    return self.send_json(200, brief_missions.edit_architecture(mid, uid, wid, data.get('mermaid')))
                if action == 'print-event':
                    return self.send_json(200, brief_missions.record_print_dialog(mid, uid, wid))
                return self.send_json(200, brief_missions.delete(mid, uid, wid, delete_output=data.get('delete_output', True) is not False))
            if path == '/api/documents/report':
                doc = write_boundary(user, 'document.report', None, lambda: {'document': document_studio.create_report(user['user_id'], user['workspace_id'],
                    str(data.get('title', 'Report')), str(data.get('text', '')))}, digest_of=lambda r: r['document']['sha256'])
                return self.send_json(201, doc)

            if path == '/api/auth/logout':
                tenant_auth_mgr.logout(user['token'])
                return self.send_json(200, {'status': 'LOGGED_OUT'})
            if path == '/api/workspaces/switch':
                switched = tenant_auth_mgr.switch_workspace(user['token'], data['workspace_id'])
                return self.send_json(200, switched)
            if path == '/api/workspaces/create':
                ws = tenant_auth_mgr.create_workspace(user['user_id'], data.get('name', 'New Workspace'))
                return self.send_json(201, ws)
            if path == '/api/sandboxes':
                sbx = sandbox_mgr.create_sandbox(user['user_id'], user['workspace_id'], mission_id=data.get('mission_id'))
                return self.send_json(201, sbx.public_dict())
            m_sbx_act = re.fullmatch(r'/api/sandboxes/([A-Za-z0-9_-]+)/action', path)
            if m_sbx_act:
                sbx_id = m_sbx_act.group(1)
                res = sandbox_mgr.execute_in_sandbox(sbx_id, data.get('command', []), caller_user_id=user['user_id'])
                return self.send_json(200, res)
            m_sbx_p = re.fullmatch(r'/api/sandboxes/([A-Za-z0-9_-]+)/pause', path)
            if m_sbx_p:
                sbx_id = m_sbx_p.group(1)
                sbx = sandbox_mgr.pause_sandbox(sbx_id, caller_user_id=user['user_id'])
                try:
                    cloud_browser_pool.get_session(sbx_id, caller_user_id=user['user_id']).pause()
                except Exception:
                    pass
                return self.send_json(200, sbx.public_dict())
            m_sbx_r = re.fullmatch(r'/api/sandboxes/([A-Za-z0-9_-]+)/resume', path)
            if m_sbx_r:
                sbx_id = m_sbx_r.group(1)
                sbx = sandbox_mgr.resume_sandbox(sbx_id, caller_user_id=user['user_id'])
                try:
                    cloud_browser_pool.get_session(sbx_id, caller_user_id=user['user_id']).resume()
                except Exception:
                    pass
                return self.send_json(200, sbx.public_dict())
            m_sbx_d = re.fullmatch(r'/api/sandboxes/([A-Za-z0-9_-]+)/destroy', path)
            if m_sbx_d:
                sbx_id = m_sbx_d.group(1)
                cloud_browser_pool.close_session(sbx_id)
                sandbox_mgr.destroy_sandbox(sbx_id, caller_user_id=user['user_id'])
                return self.send_json(200, {'status': 'DESTROYED', 'sandbox_id': sbx_id})
            if path == '/api/cloud-browser/navigate':
                sbx_id = data.get('sandbox_id')
                sbx = sandbox_mgr.get_sandbox(sbx_id, caller_user_id=user['user_id'])
                session = browser_for(sbx, user)
                res = session.navigate(data['url'])
                return self.send_json(200, res)
            if path == '/api/cloud-browser/click':
                sbx_id = data.get('sandbox_id')
                session = cloud_browser_pool.get_session(sbx_id, caller_user_id=user['user_id'])
                res = session.click(int(data.get('x', 0)), int(data.get('y', 0)))
                return self.send_json(200, res)
            if path == '/api/cloud-browser/type':
                sbx_id = data.get('sandbox_id')
                session = cloud_browser_pool.get_session(sbx_id, caller_user_id=user['user_id'])
                res = session.type_text(str(data.get('text', '')))
                return self.send_json(200, res)
            m_conn_c = re.fullmatch(r'/api/connectors/([A-Za-z0-9_-]+)/connect', path)
            if m_conn_c:
                conn_id = m_conn_c.group(1)
                conn = connector_registry.connect(user['user_id'], user['workspace_id'], conn_id, data.get('scopes', []))
                return self.send_json(200, conn.get_connection_status())
            m_conn_dc = re.fullmatch(r'/api/connectors/([A-Za-z0-9_-]+)/disconnect', path)
            if m_conn_dc:
                conn_id = m_conn_dc.group(1)
                connector_registry.disconnect(user['user_id'], user['workspace_id'], conn_id)
                return self.send_json(200, {'status': 'DISCONNECTED', 'connector_id': conn_id})
            m_conn_ex = re.fullmatch(r'/api/connectors/([A-Za-z0-9_-]+)/execute', path)
            if m_conn_ex:
                conn_id = m_conn_ex.group(1)
                conn = connector_registry.get_connector(user['user_id'], user['workspace_id'], conn_id)
                res = conn.execute_authorized_operation(data.get('operation', ''), data.get('params', {}))
                return self.send_json(200, res)
            if path == '/api/privacy/export':
                bundle = privacy_engine.export_data(user['user_id'], user['workspace_id'])
                return self.send_json(200, bundle)
            if path == '/api/privacy/delete-account':
                cloud_browser_pool.close_all()
                receipt = privacy_engine.delete_account(user['user_id'])
                return self.send_json(200, receipt.to_dict())
            if path == '/api/privacy/delete-workspace':
                receipt = privacy_engine.delete_workspace(user['user_id'], data.get('workspace_id', user['workspace_id']))
                return self.send_json(200, receipt.to_dict())
            if path == '/api/voice/transcribe-audio':
                require(rate_limiter.allow('stt', user['user_id'], 20), 429, 'too many transcription requests')
                result = voice_engine.transcribe_b64(str(data.get('audio_base64', '')), data.get('consent') is True, str(data.get('language', 'en-US')))
                return self.send_json(200, result)
            if path == '/api/voice/transcribe':
                text = str(data.get('text', '')).strip()
                require(bool(text), 400, 'text is required; audio transcription is not configured')
                is_correction = bool(re.search(r'\b(no|not|wait|correction|instead)\b', text, re.IGNORECASE))
                # Real resolution against ledger entities (stable ids), not keyword -> canned contact.
                pool = resolve_candidates(ledger, 'contact')
                low = text.casefold()
                named = [c for c in pool if c['display_name'].casefold() in low or c['display_name'].casefold().split()[0] in low]
                by_context = [c for c in named if any(tok in low for tok in re.findall(r'[a-z0-9]+', (c.get('context') or '').casefold()) if tok not in {'account', 'the', 'team'})]
                candidates = by_context if by_context else named
                resolved_contact = None
                if len(candidates) == 1:
                    c = candidates[0]
                    resolved_contact = {'id': c['entity_id'], 'name': c['display_name'], 'context': c.get('context'), 'stable_id': c.get('stable_id')}
                options = [{'id': c['entity_id'], 'name': c['display_name'], 'context': c.get('context')} for c in candidates]
                if resolved_contact:
                    feedback = f"Understood. Targeting {resolved_contact['name']} ({resolved_contact['context']}) with the latest report."
                elif options:
                    feedback = f"{len(options)} contacts match. Which one: " + '; '.join(f"{o['name']} ({o['context']})" for o in options) + '?'
                else:
                    feedback = 'I could not match a contact. Please name the contact and their account.'
                response = {
                    "transcription": text,
                    "source": "submitted_text",
                    "user_id": user['user_id'],
                    "workspace_id": user['workspace_id'],
                    "is_correction": is_correction,
                    "intent": "UPDATE_AND_FOLLOWUP",
                    "resolved_entity": resolved_contact,
                    "candidates": options,
                    "confidence": None,
                    "recommended_action": "simulated_send_and_update_followup" if resolved_contact else "needs_clarification",
                    "spoken_feedback": feedback,
                }
                return self.send_json(200, response)
            path = urlparse(self.path).path
            routed = web_api.call('POST', path, data)
            if routed:
                return self.send_json(*routed)
            if path == '/api/resolve':
                return self.send_json(200, {'contacts': resolve_candidates(ledger, 'contact', data.get('name'), data.get('context')), 'documents': resolve_candidates(ledger, 'document', data.get('name'), data.get('context'))})
            if path == '/api/mission/revise':
                mission = ledger.revise_mission(data.get('mission_id', 'mission_demo'), data.get('expected_revision'), data.get('original_request', ''))
                return self.send_json(200, mission)
            if path == '/api/action/draft':
                return self.send_json(201, self.prepare_draft(data))
            if path == '/api/action/approve-send':
                return self.send_json(200, self.approve_send(data))
            m_appr = re.fullmatch(r'/api/approvals/([A-Za-z0-9_-]+)/decision', path)
            if m_appr:
                action_id = m_appr.group(1)
                decision = str(data.get('decision', 'APPROVE')).upper()
                if decision == 'REJECT':
                    with ledger.lock:
                        action = ledger.action(action_id)
                        require(bool(action), 404, 'action not found')
                        require(action['status'] == 'NEEDS_APPROVAL', 409, 'action is not awaiting approval')
                        ledger.transition(action_id, 'CANCELLED', {'reason': data.get('reason', 'User rejected approval')})
                        return self.send_json(200, {'action_id': action_id, 'status': 'CANCELLED'})
                elif decision == 'APPROVE':
                    web_m = [m for m in ledger.all_json('missions') if str(m.get('mission_id', '')).startswith('wm_')]
                    is_web = any(action_id in wm.get('actions', []) for wm in web_m)
                    if is_web and web_api.runner:
                        digest_val = str(data.get('digest', ''))
                        res = web_api.runner.approve(action_id, digest_val)
                        return self.send_json(200, {'action_id': action_id, 'status': 'AUTHORIZED', 'result': res})
                    else:
                        res = self.approve_send({'action_id': action_id, 'digest': data.get('digest')})
                        return self.send_json(200, res)
                else:
                    raise ApiError(400, 'decision must be APPROVE or REJECT')
            m_sk = re.fullmatch(r'/api/skills/([A-Za-z0-9_-]+)/run', path)
            if m_sk:
                skill_id = m_sk.group(1)
                if web_api.skills and skill_id in [s.get('skill_id') or s.get('id') for s in web_api.skills.list()]:
                    res = web_api.skills.replay(skill_id, data.get('params') or {}, web_api.runner.urls if web_api.runner else {}, ROOT, data.get('version'))
                    return self.send_json(200, res)
                else:
                    mission_id = str(data.get('mission_id', 'mission_demo'))
                    expected_rev = int(data.get('expected_revision', 1))
                    plan = skill_store.prepare_replay(skill_id, ledger, mission_id, expected_rev, dict(data.get('inputs', {})))
                    draft = self.prepare_draft({'mission_id': plan.mission_id, 'contact_id': plan.contact['entity_id'], 'report_id': plan.report['entity_id']})
                    return self.send_json(200, {'plan': asdict(plan), 'draft': draft})
            if path == '/api/agents/discover':
                return self.send_json(200, self.discover(data))
            if path == '/api/skills/propose':
                candidate = candidate_from_confirmed(ledger, data.get('action_id', ''), version=skill_store.next_version('northwind_followup'))
                review_digest = skill_store.propose(candidate)
                return self.send_json(201, {'skill': asdict(candidate), 'status': 'PENDING_APPROVAL', 'review_digest': review_digest})
            if path == '/api/skills/activate':
                skill = skill_store.activate(str(data['skill_id']), int(data['version']), str(data['review_digest']))
                return self.send_json(200, {'skill': asdict(skill), 'status': 'ACTIVE'})
            if path == '/api/skills/replay':
                plan = skill_store.prepare_replay(str(data['skill_id']), ledger, str(data.get('mission_id', 'mission_demo')), int(data['expected_revision']), dict(data.get('inputs', {})))
                draft = self.prepare_draft({'mission_id': plan.mission_id, 'contact_id': plan.contact['entity_id'], 'report_id': plan.report['entity_id']})
                return self.send_json(201, {'plan': asdict(plan), 'draft': draft})
            if path == '/api/emergency-stop':
                scheduler.cancel()
                windows_adapter.stop()
                return self.send_json(200, {'status': 'STOPPED', 'cursor_lease_revoked': True})
            if path == '/api/resume':
                scheduler.reset_cancellation()
                windows_adapter.reset_stop()
                windows_adapter.resume()
                return self.send_json(200, {'status': 'READY'})
            if path in {'/api/windows/move', '/api/windows/click', '/api/windows/type'}:
                return self.send_json(200, self.desktop_action(path.rsplit('/', 1)[1], data))
            if path == '/api/windows/capture':
                return self.send_json(200, self.capture_window(data))
            if path == '/api/uia/inspect':
                return self.send_json(200, self.inspect_uia(data))
            raise ApiError(404, 'not found')
        except VoiceError as exc:
            self.send_json(exc.status_code, {'error': exc.message, 'code': exc.code})
        except MissionError as exc:
            self.send_json(exc.status_code, {'error': exc.message, **({'candidates': exc.candidates} if exc.candidates else {})})
        except ApiError as exc:
            self.send_json(exc.status, {'error': exc.message})
        except (TenantError, SandboxError, CloudBrowserError, ConnectorError, DocumentError, ProjectError) as exc:
            self.send_json(getattr(exc, 'status_code', 400), {'error': exc.message if hasattr(exc, 'message') else str(exc)})
        except AmbiguousEntity as exc:
            self.send_json(409, {'error': str(exc), 'candidates': exc.candidates})
        except SkillError as exc:
            self.send_json(409, {'error': str(exc)})
        except ValueError as exc:
            self.send_json(409 if str(exc) == 'stale mission revision' else 400, {'error': str(exc)})
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            self.send_json(400, {'error': str(exc)})
        except sqlite3.IntegrityError as exc:
            self.send_json(409, {'error': str(exc)})
        except (Cancelled, LeaseLost, windows_adapter.UnsupportedDesktop, TimeoutError) as exc:
            self.send_json(409, {'error': str(exc)})
        except uia_adapter.UIAUnavailable as exc:
            self.send_json(503, {'error': str(exc), 'status': 'UNSUPPORTED'})
        except Exception as exc:
            self.send_json(500, {'error': str(exc)})

    def prepare_draft(self, data: dict) -> dict:
        with ledger.lock:
            mission = ledger.get_json('missions', data.get('mission_id', 'mission_demo'))
            require(bool(mission) and mission['status'] == 'ACTIVE', 409, 'mission is not active')
            require('prepare_draft' in mission['permitted_actions'], 403, 'drafting is not permitted')
            contact = ledger.get_json('entities', data.get('contact_id', ''))
            report = ledger.get_json('entities', data.get('report_id', ''))
            require(bool(contact) and bool(report), 400, 'select an existing contact and report')
            require(contact['kind'] == 'contact' and report['kind'] == 'document', 400, 'entity kinds do not match action')
            context = mission['constraints']['client']
            require(contact.get('context') == context and report.get('context') == context, 409, 'contact/report context differs from mission client')
            require(report['entity_id'] == latest_report(ledger, context)['entity_id'], 409, 'selected report is not latest under the stated modified-time policy')
            action = Action(uid('act'), mission['mission_id'], mission['intent_revision'], 'simulated_send_and_update_followup', contact['entity_id'], 'local-demo-agent', 'local-outbox', None, {'attachment_id': report['entity_id'], 'body': 'Approved Northwind follow-up with the latest report attached.', 'followup_id': 'followup_northwind'}, None, {'recipient_id': contact['stable_id'], 'attachment_id': report['stable_id'], 'followup_status': 'contacted'}, 'NON_IDEMPOTENT', 'consequential')
            ledger.create_action(action)
            ledger.transition(action.action_id, 'BOUND')
            ledger.transition(action.action_id, 'NEEDS_APPROVAL')
            current = ledger.action(action.action_id)
            return {'action': current, 'digest': digest(current), 'contact': contact, 'report': report}

    def discover(self, data: dict) -> dict:
        mission = ledger.get_json('missions', data.get('mission_id', 'mission_demo'))
        require(bool(mission) and mission['status'] == 'ACTIVE', 409, 'mission is not active')
        revision = mission['intent_revision']
        specs = [
            TaskSpec(uid('task'), mission['mission_id'], revision, 'contact-resolver', 'resolve_contacts', {'name': 'Alex'}, capabilities=frozenset({'entities.read'})),
            TaskSpec(uid('task'), mission['mission_id'], revision, 'report-resolver', 'resolve_reports', {'context': mission['constraints']['client']}, capabilities=frozenset({'entities.read'})),
        ]
        results = scheduler.run(specs)
        return {'tasks': [asdict(result) for result in results.values()], 'mission_id': mission['mission_id'], 'intent_revision': revision}

    def approve_send(self, data: dict) -> dict:
        with ledger.lock:
            action = ledger.action(data.get('action_id', ''))
            require(bool(action), 404, 'action not found')
            mission = ledger.get_json('missions', action['mission_id'])
            require(bool(mission) and mission['status'] == 'ACTIVE', 409, 'mission is not active')
            require(action['intent_revision'] == mission['intent_revision'], 409, 'approval refused: action was superseded by a newer intent revision')
            require(action['operation'] == 'simulated_send_and_update_followup', 403, 'operation cannot use this approval endpoint')
            require('simulated_send_after_approval' in mission['permitted_actions'], 403, 'send is not permitted')
            require(action['status'] == 'NEEDS_APPROVAL', 409, 'action is not awaiting approval')
            approval_digest = digest(action)
            require(data.get('digest') == approval_digest, 409, 'approval digest mismatch; action changed')
            contact = ledger.get_json('entities', action['target_entity_id'])
            report = ledger.get_json('entities', action['arguments']['attachment_id'])
            context = mission['constraints']['client']
            require(bool(contact) and bool(report) and contact.get('context') == context and report.get('context') == context, 409, 'target binding changed')
            require(report['entity_id'] == latest_report(ledger, context)['entity_id'], 409, 'report version changed after approval review')
            require(contact['stable_id'] == action['expected_effect']['recipient_id'] and report['stable_id'] == action['expected_effect']['attachment_id'], 409, 'stable entity identity changed')
            ledger.transition(action['action_id'], 'AUTHORIZED', {'approval_digest': approval_digest})
            ledger.transition(action['action_id'], 'READY')
            ledger.transition(action['action_id'], 'DISPATCHING')
            try:
                message_id = ledger.commit_simulated_send(action['action_id'], action['target_entity_id'], action['arguments']['attachment_id'], action['arguments']['body'], action['arguments']['followup_id'])
            except Exception:
                ledger.transition(action['action_id'], 'UNCERTAIN')
                raise
            ledger.transition(action['action_id'], 'DISPATCHED', {'message_id': message_id})
            ledger.transition(action['action_id'], 'OBSERVING')
            result = verify_outbox(ledger, action['target_entity_id'], action['arguments']['attachment_id'], action['arguments']['followup_id'], action['action_id'])
            ledger.transition(action['action_id'], result['status'], {'verification': result, 'verification_status': result['status'], 'evidence_references': [message_id]})
            return {'action': ledger.action(action['action_id']), 'verification': result}

    def desktop_action(self, operation: str, data: dict) -> dict:
        # A user invokes each input through the local dashboard. The bound snapshot
        # and current mission revision are checked again before SendInput.
        with ledger.lock:
            mission = ledger.get_json('missions', data.get('mission_id', 'mission_demo'))
            require(bool(mission) and mission['status'] == 'ACTIVE', 409, 'mission is not active')
            require(data.get('intent_revision') == mission['intent_revision'], 409, 'stale mission revision')
            require(f'physical_{operation}' in mission['permitted_actions'], 403, 'physical operation is not permitted')
            require(isinstance(data.get('snapshot_id'), str) and bool(data['snapshot_id']), 400, 'fresh window snapshot required')
            hwnd = int(data['hwnd'])
            if operation == 'move':
                require(isinstance(data.get('smooth', True), bool), 400, 'smooth must be a boolean')
            arguments = {key: data[key] for key in ('x', 'y', 'text', 'smooth') if key in data}
            action_id = uid('act')
            lease = scheduler.leases.acquire(action_id, duration_seconds=3, wait_seconds=2, stop=scheduler._stop)
            try:
                scheduler.leases.validate(lease)
                windows_adapter.validate_target(hwnd, data['snapshot_id'], int(data['x']) if operation != 'type' else None, int(data['y']) if operation != 'type' else None)
                action = Action(action_id, mission['mission_id'], mission['intent_revision'], f'physical_{operation}', f'hwnd:{hwnd}', 'local-user-control', 'windows-desktop', {'hwnd': hwnd, 'cursor_lease_generation': lease.generation}, arguments, data['snapshot_id'], {'input_dispatched': True}, 'IDEMPOTENT_WRITE' if operation == 'move' else 'UNKNOWN', 'moderate')
                ledger.create_action(action)
                ledger.transition(action.action_id, 'BOUND')
                ledger.transition(action.action_id, 'AUTHORIZED', {'approval_digest': digest({'operation': operation, 'hwnd': hwnd, 'arguments': arguments, 'snapshot_id': data['snapshot_id']})})
                ledger.transition(action.action_id, 'READY')
                ledger.transition(action.action_id, 'DISPATCHING')
                scheduler.leases.validate(lease)
                if operation == 'move':
                    result = windows_adapter.move_pointer(hwnd, int(data['x']), int(data['y']), snapshot_id=data['snapshot_id'], smooth=data.get('smooth', True))
                elif operation == 'click':
                    result = windows_adapter.click(hwnd, int(data['x']), int(data['y']), snapshot_id=data['snapshot_id'])
                else:
                    result = windows_adapter.type_text(hwnd, str(data['text']), snapshot_id=data['snapshot_id'])
            except Exception:
                if ledger.action(action_id) and ledger.action(action_id)['status'] == 'DISPATCHING':
                    ledger.transition(action_id, 'UNCERTAIN')
                raise
            finally:
                scheduler.leases.release(lease)
            ledger.transition(action.action_id, 'DISPATCHED')
            ledger.transition(action.action_id, 'OBSERVING')
            ledger.transition(action.action_id, 'UNCERTAIN', {'verification_status': 'UNCERTAIN', 'verification': {'reason': 'physical input dispatched; no independent business-effect verifier'}})
            return {'action_id': action.action_id, 'status': 'UNCERTAIN', 'input_result': result}

    def capture_window(self, data: dict) -> dict:
        mission = ledger.get_json('missions', data.get('mission_id', 'mission_demo'))
        require(bool(mission) and mission['status'] == 'ACTIVE', 409, 'mission is not active')
        require(data.get('intent_revision') == mission['intent_revision'], 409, 'stale mission revision')
        require('read' in mission['permitted_actions'], 403, 'window observation is not permitted')
        require(isinstance(data.get('snapshot_id'), str), 400, 'window snapshot required')
        result = windows_adapter.capture_window(int(data['hwnd']), data['snapshot_id'])
        return {**result, 'image_name': Path(result['path']).name}

    def inspect_uia(self, data: dict) -> dict:
        mission = ledger.get_json('missions', data.get('mission_id', 'mission_demo'))
        require(bool(mission) and mission['status'] == 'ACTIVE', 409, 'mission is not active')
        require(data.get('intent_revision') == mission['intent_revision'], 409, 'stale mission revision')
        require('read' in mission['permitted_actions'], 403, 'UIA inspection is not permitted')
        require(isinstance(data.get('snapshot_id'), str), 400, 'window snapshot required')
        hwnd = int(data['hwnd'])
        windows_adapter.validate_snapshot(hwnd, data['snapshot_id'])
        return uia_adapter.inspect_window(hwnd)


def main():
    try:
        SECURITY.validate_startup()
    except ConfigError as exc:
        raise SystemExit(f'refusing to start: {exc}')
    address = (SECURITY.bind_host, int(os.environ.get('RESOLVE_PORT', '8765')))
    server = ThreadingHTTPServer(address, Handler)
    diag = enable_diagnostics(server.server_port)
    print(f'browser diagnostics: {"enabled (real Chromium vs bundled demo app)" if diag else "disabled"}')
    print(f'RESOLVE local server at http://{address[0]}:{address[1]}  database={DB}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopping RESOLVE.')
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
