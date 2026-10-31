"""HTTP surface of the Inbox-to-Commitment Operator (/api/inbox/*).

Identity: the caller is always the authenticated session user (resolve_app.get_session). Workspace: the session's
workspace; REPLAY uses the separate workspace '<workspace>.replay'. No client-supplied user or workspace id is trusted.
Every route validates its state transition and returns safe error messages. Tokens are never returned.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from typing import Callable
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import gmail_adapter as ga
import inbox_intel as ii
import inbox_replay as rp
import model_provider as mp
import inbox_service as svc
from gmail_adapter import GmailClient, GmailConfig, GmailError, GmailSendClient, OAuthStates
from inbox_store import COMMITMENT_STATUSES, InboxError, InboxStore
from send_broker import SendBroker
from voice_policy import HIGH_IMPACT, RULE_TYPES, conflicts, parse_instruction, requires_confirmation

REPLAY_SUFFIX = ".replay"
LOCAL_SUFFIX = ".local"
MODES = ("LIVE", "LOCAL", "REPLAY")
PROVIDER_FOR_MODE = {"LIVE": "gmail", "LOCAL": "local_mail", "REPLAY": "replay"}
REPLAY_SCRIPT_EXTRA = ("draft_T1", "approve_send_T1", "draft_T7")
CORRECTION_KINDS = ("WRONG_PRIORITY", "WRONG_CATEGORY", "NOT_ACTIONABLE", "NOT_COMMITMENT", "WRONG_OWNER", "WRONG_DEADLINE",
                    "ALREADY_COMPLETED", "AUTOMATED", "OTHER")
CORRECTION_SCOPES = ("ONE_TIME", "PERSISTENT_THREAD", "PERSISTENT_WORKSPACE")
EMAIL = re.compile(r"[^<>\s@,]+@[^<>\s@,]+\.[A-Za-z]{2,}")
MAX_TEXT = 4000


class ApiError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def _address(header: str) -> str:
    m = EMAIL.search(header or "")
    return m.group(0).lower() if m else ""


def scoped(workspace_id: str, mode: str) -> str:
    if mode == "REPLAY":
        return workspace_id + REPLAY_SUFFIX
    if mode == "LOCAL":
        return workspace_id + LOCAL_SUFFIX
    if mode == "LIVE":
        return workspace_id
    raise ApiError(400, "mode must be LIVE, LOCAL or REPLAY")


class InboxApi:
    def __init__(self, store: InboxStore, gmail_config: GmailConfig | None, transport: Callable, oauth: OAuthStates | None = None,
                 now: Callable[[], float] = time.time, public_base: str = "", local_mail=None, inference=None):
        self.store = store
        self.config = gmail_config
        self.transport = transport
        self.oauth = oauth or OAuthStates(now=now)
        self.now = now
        self.public_base = public_base
        self.replay: dict[tuple[str, str], dict] = {}         # replay progress (in memory; replay data itself is stored)
        self.inference = inference or mp.InboxInference(None)   # RULES_ONLY unless a model endpoint is configured
        self.local_mail = local_mail                          # local_mail.LocalMailServer or None (LOCAL mode not configured)

    # ------------------------------------------------------------------ dispatch
    def handle(self, method: str, path: str, query: dict, body: dict | None, user: dict) -> tuple[int, dict]:
        uid, wid = user["user_id"], user["workspace_id"]
        try:
            return self._route(method, path, query, body or {}, uid, wid)
        except InboxError as e:
            return e.status_code, {"error": e.message}
        except ApiError as e:
            return e.status, {"error": e.message, **e.extra}
        except GmailError as e:
            self._record_provider_state(uid, wid, method, e)
            return 502 if e.code == "PROVIDER_ERROR" else 409, {"error": e.message, "code": e.code}

    def _record_provider_state(self, uid, wid, method, e: GmailError) -> None:
        """A provider error that means the mailbox is unusable is written to the connection, so the UI shows it honestly."""
        status = {"REVOKED": "REVOKED", "TOKEN_EXPIRED": "EXPIRED", "RATE_LIMITED": "RATE_LIMITED"}.get(e.code)
        conn = self._gmail_conn(uid, wid)
        if status and conn is not None and conn["status"] != status and getattr(e, "provider", "gmail") == "gmail":
            self.store.upsert_connection(uid, wid, "gmail", status, conn["scopes"], e.message)

    def _mode(self, query: dict) -> str:
        return (query.get("mode") or ["LIVE"])[0].upper()

    def _route(self, method, path, query, body, uid, wid):  # noqa: C901
        m = lambda pat: re.fullmatch(pat, path)
        if method == "GET" and path == "/api/inbox/connections/gmail/callback":
            return self.gmail_callback(query)
        if path == "/api/inbox/connections" and method == "GET":
            return 200, self.connections(uid, wid)
        if path == "/api/inbox/connections/gmail/status" and method == "GET":
            return 200, self.gmail_status(uid, wid)
        if path == "/api/inbox/connections/gmail/start" and method == "POST":
            return 200, self.gmail_start(uid, wid, body)
        if path == "/api/inbox/connections/gmail" and method == "DELETE":
            return 200, self.gmail_disconnect(uid, wid)
        if path == "/api/inbox/connections/local/create" and method == "POST":
            return 201, self.local_create(uid, wid, body)
        if path == "/api/inbox/connections/local/connect" and method == "POST":
            return 200, self.local_connect(uid, wid, body)
        if path == "/api/inbox/connections/local" and method == "DELETE":
            return 200, self.local_disconnect(uid, wid)
        if path == "/api/inbox/local/deliver" and method == "POST":
            return 201, self.local_deliver(uid, wid, body)
        if path == "/api/inbox/settings" and method == "GET":
            return 200, self.store.get_settings(uid, wid)
        if path == "/api/inbox/settings" and method == "PUT":
            return 200, self.store.set_settings(uid, wid, timezone_name=_text(body, "timezone", 64, required=False),
                                                signoff=_text(body, "signoff", 60, required=False), tone=_text(body, "tone", 40, required=False))
        if path == "/api/inbox/sync" and method == "POST":
            return 200, self.sync(uid, wid, self._mode({"mode": [body.get("mode", "LIVE")]}), body)
        if path == "/api/inbox/sync/status" and method == "GET":
            return 200, self.sync_status(uid, wid, self._mode(query))
        if path == "/api/inbox/brief" and method == "GET":
            return 200, self.brief(uid, wid, self._mode(query))
        if path == "/api/inbox/threads" and method == "GET":
            return 200, self.thread_list(uid, wid, self._mode(query), query)
        if (mm := m(r"/api/inbox/threads/([A-Za-z0-9_.\-]+)")) and method == "GET":
            return 200, self.thread_detail(uid, wid, self._mode(query), mm.group(1))
        if (mm := m(r"/api/inbox/threads/([A-Za-z0-9_.\-]+)/analyze")) and method == "POST":
            return 200, self.analyze(uid, wid, self._mode(query), mm.group(1))
        if path == "/api/inbox/engine" and method == "GET":
            return 200, self.inference.status()
        if (mm := m(r"/api/inbox/threads/([A-Za-z0-9_.\-]+)/reprocess")) and method == "POST":
            return 200, self.reprocess(uid, wid, self._mode(query), mm.group(1))
        if (mm := m(r"/api/inbox/threads/([A-Za-z0-9_.\-]+)/corrections")) and method == "POST":
            return 200, self.correct(uid, wid, self._mode(query), mm.group(1), body)
        if (mm := m(r"/api/inbox/threads/([A-Za-z0-9_.\-]+)/state")) and method == "POST":
            return 200, self.thread_state(uid, wid, self._mode(query), mm.group(1), body)
        if (mm := m(r"/api/inbox/threads/([A-Za-z0-9_.\-]+)/drafts")) and method == "POST":
            return 201, self.create_draft(uid, wid, self._mode(query), mm.group(1), body)
        if path == "/api/inbox/commitments" and method == "GET":
            return 200, self.commitment_list(uid, wid, self._mode(query), query)
        if (mm := m(r"/api/inbox/commitments/([A-Za-z0-9_]+)")) and method == "GET":
            return 200, self.commitment_detail(uid, wid, self._mode(query), mm.group(1))
        if (mm := m(r"/api/inbox/commitments/([A-Za-z0-9_]+)")) and method == "PATCH":
            return 200, self.commitment_patch(uid, wid, self._mode(query), mm.group(1), body)
        if (mm := m(r"/api/inbox/commitments/([A-Za-z0-9_]+)/resolve")) and method == "POST":
            return 200, self.commitment_resolve(uid, wid, self._mode(query), mm.group(1), body)
        if (mm := m(r"/api/inbox/commitments/([A-Za-z0-9_]+)/follow-up")) and method == "POST":
            return 201, self.follow_up(uid, wid, self._mode(query), mm.group(1), body)
        if (mm := m(r"/api/inbox/commitments/([A-Za-z0-9_]+)/notes")) and method == "POST":
            return 201, self.commitment_note(uid, wid, self._mode(query), mm.group(1), body)
        if path == "/api/inbox/drafts" and method == "GET":
            return 200, {"drafts": self.store.list_drafts(uid, scoped(wid, self._mode(query)))}
        if (mm := m(r"/api/inbox/drafts/([A-Za-z0-9_]+)")) and method == "GET":
            return 200, self.draft_detail(uid, wid, self._mode(query), mm.group(1))
        if (mm := m(r"/api/inbox/drafts/([A-Za-z0-9_]+)")) and method == "PATCH":
            return 200, self.draft_patch(uid, wid, self._mode(query), mm.group(1), body)
        if (mm := m(r"/api/inbox/drafts/([A-Za-z0-9_]+)/approve")) and method == "POST":
            return 200, self.draft_approve(uid, wid, self._mode(query), mm.group(1), body)
        if (mm := m(r"/api/inbox/drafts/([A-Za-z0-9_]+)/gmail-draft")) and method == "POST":
            return 200, self.draft_gmail(uid, wid, self._mode(query), mm.group(1), body)
        if (mm := m(r"/api/inbox/drafts/([A-Za-z0-9_]+)/send")) and method == "POST":
            return 200, self.draft_send(uid, wid, self._mode(query), mm.group(1), body)
        if (mm := m(r"/api/inbox/drafts/([A-Za-z0-9_]+)/reject")) and method == "POST":
            return 200, self.store.reject_draft(uid, scoped(wid, self._mode(query)), mm.group(1), _text(body, "reason", 200, required=False) or "")
        if path == "/api/inbox/executions" and method == "GET":
            return 200, self.executions(uid, wid, self._mode(query))
        if (mm := m(r"/api/inbox/executions/([A-Za-z0-9_]+)")) and method == "GET":
            ws = scoped(wid, self._mode(query))
            rec = self.store.get_execution(uid, ws, mm.group(1))
            return 200, {**rec, "mode": self._mode(query), "label": self._label(self._mode(query))}
        if (mm := m(r"/api/inbox/executions/([A-Za-z0-9_]+)/reconcile")) and method == "POST":
            return 200, self.reconcile(uid, wid, self._mode(query), mm.group(1))
        if path == "/api/inbox/rules" and method == "GET":
            return 200, self.rules(uid, wid)
        if path == "/api/inbox/rules/parse" and method == "POST":
            return 200, self.rules_parse(uid, wid, body)
        if path == "/api/inbox/rules" and method == "POST":
            return 201, self.rules_add(uid, wid, body)
        if (mm := m(r"/api/inbox/rules/([A-Za-z0-9_]+)")) and method == "PATCH":
            return 200, self.rules_patch(uid, wid, mm.group(1), body)
        if (mm := m(r"/api/inbox/rules/([A-Za-z0-9_]+)/confirm")) and method == "POST":
            return 200, self.rules_confirm(uid, wid, mm.group(1), body)
        if (mm := m(r"/api/inbox/rules/([A-Za-z0-9_]+)/reverse")) and method == "POST":
            return 200, self.rules_reverse(uid, wid, mm.group(1))
        if path == "/api/inbox/activity" and method == "GET":
            return 200, self.activity(uid, wid, query)
        if path == "/api/inbox/replay/scenarios" and method == "GET":
            return 200, self.replay_scenarios(uid, wid)
        if path == "/api/inbox/replay/start" and method == "POST":
            return 200, self.replay_start(uid, wid)
        if path == "/api/inbox/replay/step" and method == "POST":
            return 200, self.replay_step(uid, wid, body)
        if path == "/api/inbox/replay/pause" and method == "POST":
            return 200, self._replay_set(uid, wid, "PAUSED")
        if path == "/api/inbox/replay/resume" and method == "POST":
            return 200, self._replay_set(uid, wid, "RUNNING")
        if path == "/api/inbox/replay/reset" and method == "POST":
            return 200, self.replay_reset(uid, wid)
        if path == "/api/inbox/replay/replay" and method == "POST":
            self.replay_reset(uid, wid)
            return 200, self.replay_start(uid, wid)
        raise ApiError(404, "no such inbox route")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _label(mode: str) -> str:
        return {"REPLAY": "REPLAY · SYNTHETIC DATA · SIMULATED SENDS", "LOCAL": "LOCAL · LOCAL MAIL SERVER (not Gmail)"}.get(mode, "LIVE · GMAIL")

    def _gmail_conn(self, uid, wid):
        return self.store.get_connection(uid, wid, "gmail")

    def _account(self, uid, wid, mode) -> str:
        if mode == "REPLAY":
            return rp.REPLAY_ADDRESS
        if mode == "LOCAL":
            return ((self.store.load_oauth_token(uid, scoped(wid, "LOCAL"), "local_mail") or {}).get("address") or "").lower()
        tok = self.store.load_oauth_token(uid, wid, "gmail") or {}
        return (tok.get("account") or "").lower()

    def _tz(self, uid, wid, mode):
        s = self.store.get_settings(uid, scoped(wid, mode))
        return s["timezone"], s["timezone_configured"]

    def _gmail_client(self, uid, wid, need: str | None = None):
        if self.config is None:
            raise ApiError(409, "Gmail is not configured on this server", code="NOT_CONFIGURED", required_env=["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI"])
        conn = self._gmail_conn(uid, wid)
        tok = self.store.load_oauth_token(uid, wid, "gmail")
        if conn is None or tok is None or conn["status"] != "CONNECTED":
            raise ApiError(409, "Gmail is not connected", code=(conn or {}).get("status", "NOT_CONNECTED"))
        if need and need not in (conn["scopes"] or []):
            raise ApiError(409, "this permission was not granted; enable it in Connections first", code="SCOPE_NOT_GRANTED", scope=need)
        access = tok["access_token"]

        def refresh() -> str:
            fresh = ga.refresh_access_token(self.config, tok["refresh_token"], self.transport)
            tok.update(fresh)
            self.store.save_oauth_token(uid, wid, "gmail", tok)
            return fresh["access_token"]
        if tok.get("expires_at", 0) < self.now() + 60:
            access = refresh()
        return GmailSendClient(self.transport, access, refresh)

    def _provider(self, uid, wid, mode, visible=None, inject=None):
        if mode == "REPLAY":
            return rp.ReplayProvider(self.store, uid, scoped(wid, "REPLAY"), self._anchor(uid, wid), visible, inject)
        if mode == "LOCAL":
            return self._local_client(uid, wid)
        return self._gmail_client(uid, wid)

    # ------------------------------------------------------------------ LOCAL mode (local mail server; never Gmail)
    def _local_required(self):
        if self.local_mail is None:
            raise ApiError(409, "the local mail server is not running on this server (set RESOLVE_LOCAL_MAIL=1)", code="NOT_CONFIGURED")
        return self.local_mail

    def _local_client(self, uid, wid):
        import local_mail as lm
        srv = self._local_required()
        ws = scoped(wid, "LOCAL")
        creds = self.store.load_oauth_token(uid, ws, "local_mail")
        conn = self.store.get_connection(uid, ws, "local_mail")
        if creds is None or conn is None or conn["status"] not in ("CONNECTED",):
            raise ApiError(409, "the local mailbox is not connected", code=(conn or {}).get("status", "NOT_CONNECTED"))
        return lm.LocalMailProvider(creds["address"], creds["password"], srv.host, srv.smtp_port, srv.pop3_port)

    def local_status(self, uid, wid) -> dict:
        ws = scoped(wid, "LOCAL")
        conn = self.store.get_connection(uid, ws, "local_mail")
        creds = self.store.load_oauth_token(uid, ws, "local_mail")
        if self.local_mail is None:
            return {"status": "NOT_CONFIGURED", "detail": "Start RESOLVE with RESOLVE_LOCAL_MAIL=1 to run the local mail server.", "account": ""}
        info = self.local_mail.info()
        return {"status": (conn or {}).get("status", "NOT_CONNECTED"), "detail": (conn or {}).get("detail", ""),
                "account": (creds or {}).get("address", ""), "domain": info["domain"], "smtp_port": info["smtp_port"],
                "pop3_port": info["pop3_port"], "updated_at": (conn or {}).get("updated_at"),
                "label": "LOCAL mail server on this machine. Delivers to local accounts only; never Gmail, never the internet."}

    def _local_save(self, uid, wid, address: str, password: str) -> dict:
        import local_mail as lm
        srv = self._local_required()
        client = lm.LocalMailProvider(address, password, srv.host, srv.smtp_port, srv.pop3_port)
        prof = client.profile()                      # real POP3 login: proves the credentials before anything is stored
        ws = scoped(wid, "LOCAL")
        self.store.save_oauth_token(uid, ws, "local_mail", {"address": address, "password": password})
        self.store.upsert_connection(uid, ws, "local_mail", "CONNECTED", ["local:read", "local:send", "local:drafts"],
                                     f"local mailbox {address}")
        return {**self.local_status(uid, wid), "messages_in_inbox": prof["messages_total"]}

    def local_create(self, uid, wid, body) -> dict:
        import secrets as _s
        srv = self._local_required()
        name = (_text(body, "name", 40) or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._\-]{0,39}", name):
            raise ApiError(400, "name may contain letters, digits, dot, dash and underscore")
        address = f"{name}@{srv.store.domain}"
        password = _s.token_urlsafe(18)
        try:
            srv.store.create_account(address, password)
        except ValueError as e:
            raise ApiError(409, str(e))
        return self._local_save(uid, wid, address, password)

    def local_connect(self, uid, wid, body) -> dict:
        address = (_text(body, "address", 120) or "").strip().lower()
        password = _text(body, "password", 200) or ""
        try:
            return self._local_save(uid, wid, address, password)
        except GmailError as e:
            raise ApiError(409, e.message, code=e.code)

    def local_disconnect(self, uid, wid) -> dict:
        ws = scoped(wid, "LOCAL")
        self.store.delete_oauth_token(uid, ws, "local_mail")
        if self.store.get_connection(uid, ws, "local_mail"):
            self.store.upsert_connection(uid, ws, "local_mail", "NOT_CONNECTED", [], "disconnected by the user")
        return {"status": "NOT_CONNECTED", "note": "Messages stay on the local mail server; stored records stay until you delete them."}

    def local_deliver(self, uid, wid, body) -> dict:
        """Deliver an incoming message into the connected local mailbox from a local colleague account, over SMTP.
        Colleague accounts are local-only and marked as such; a user's own mailbox can never be used as the sender."""
        import local_mail as lm
        srv = self._local_required()
        account = self._account(uid, wid, "LOCAL")
        if not account:
            raise ApiError(409, "connect a local mailbox first", code="NOT_CONNECTED")
        name = (_text(body, "from_name", 40) or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9._\-]{0,39}", name):
            raise ApiError(400, "from_name may contain letters, digits, dot, dash and underscore")
        subject = _text(body, "subject", 300)
        text = _text(body, "body", MAX_TEXT)
        in_reply_to = _text(body, "in_reply_to", 300, required=False)
        try:
            sender, password = srv.colleague(name)
        except ValueError as e:
            raise ApiError(409, str(e))
        mid = lm.send_as(sender, password, account, subject, text, srv.host, srv.smtp_port, in_reply_to=in_reply_to)
        return {"delivered": True, "from": sender, "to": account, "message_id": mid, "transport": "SMTP (local mail server)",
                "next": "Run Sync in LOCAL mode to bring it into RESOLVE."}

    def _anchor(self, uid, wid) -> float:
        """The replay clock is fixed once per replay so deadlines and timestamps stay stable across steps."""
        s = self.store.get_settings(uid, scoped(wid, "REPLAY"))
        if s["replay_anchor"] is None:
            s = self.store.set_settings(uid, scoped(wid, "REPLAY"), timezone_name="UTC", replay_anchor=rp.anchor_default(self.now()))
        return s["replay_anchor"]

    def _thread_row(self, uid, ws, thread_id):
        msgs = svc._thread_messages(self.store, uid, ws, thread_id)
        if not msgs:
            raise ApiError(404, "thread not found")
        return msgs

    def _effective(self, uid, ws, thread_id, decision: dict) -> dict:
        """Persisted decision with active user corrections applied (ONE_TIME and persistent scopes). Originals are kept."""
        eff = {k: decision[k] for k in ("category", "priority", "confidence", "reasons", "evidence")}
        eff["overridden_by"] = []
        msgs = self._thread_row(uid, ws, thread_id)
        sender = _address(max(msgs, key=lambda x: x["sent_at"])["sender"])
        for c in self.store.list_corrections(uid, ws):
            if not c["active"]:
                continue
            applies = (c["scope"] in ("ONE_TIME", "PERSISTENT_THREAD") and c["thread_id"] == thread_id) or \
                      (c["scope"] == "PERSISTENT_WORKSPACE" and sender and sender == c["corrected"].get("sender"))
            if applies:
                for k in ("category", "priority"):
                    if k in c["corrected"]:
                        eff[k] = c["corrected"][k]
                eff["overridden_by"].append(c["id"])
        return eff

    # ------------------------------------------------------------------ connections
    def connections(self, uid, wid) -> dict:
        gm = self._gmail_conn(uid, wid)
        tok = self.store.load_oauth_token(uid, wid, "gmail")
        status = ga.connection_status(self.config, has_token=tok is not None and gm is not None and gm["status"] == "CONNECTED",
                                      revoked=(gm or {}).get("status") == "REVOKED")
        if gm and gm["status"] in ("EXPIRED", "ERROR", "RATE_LIMITED") and self.config is not None:
            status = {"status": gm["status"], "detail": gm["detail"]}
        replay = self.store.get_connection(uid, scoped(wid, "REPLAY"), "replay")
        return {"gmail": {**status, "configured": self.config is not None, "scopes": (gm or {}).get("scopes", []),
                          "account": (tok or {}).get("account", "") if tok else "", "updated_at": (gm or {}).get("updated_at")},
                "replay": {"status": "REPLAY" if replay else "NOT_STARTED", "label": "synthetic data, simulated sends"},
                "local": self.local_status(uid, wid),
                "required_env": ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI"] if self.config is None else []}

    def gmail_status(self, uid, wid) -> dict:
        return self.connections(uid, wid)["gmail"]

    def gmail_start(self, uid, wid, body) -> dict:
        if self.config is None:
            raise ApiError(409, "GMAIL NOT CONFIGURED: set GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REDIRECT_URI, then restart RESOLVE",
                           code="NOT_CONFIGURED", required_env=["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI"])
        wanted = [ga.READONLY_SCOPE]
        requested = body.get("scopes") or ["read"]
        if not isinstance(requested, list) or any(x not in ("read", "compose", "send") for x in requested):
            raise ApiError(400, "scopes must be a list of read, compose or send")
        if "compose" in requested:
            wanted.append(ga.COMPOSE_SCOPE)
        if "send" in requested:
            wanted.append(ga.SEND_SCOPE)
        issued = self.oauth.issue(uid, wid, wanted)
        url = ga.authorization_url(self.config, issued["state"], issued["code_challenge"], wanted)
        return {"authorization_url": url, "scopes": sorted(set(wanted)), "state_expires_in": int(ga.OAUTH_STATE_TTL)}

    def gmail_callback(self, query) -> tuple[int, dict]:
        """Public redirect target. The state (single use, bound to the starting user) carries the PKCE verifier."""
        base = self.public_base or ""
        if (query.get("error") or [""])[0]:
            return 302, {"location": f"{base}/#inbox-connections?gmail=denied"}
        code = (query.get("code") or [""])[0]
        state = (query.get("state") or [""])[0]
        try:
            item = self.oauth.consume(state)
            if not code or len(code) > 2048:
                raise GmailError("PROVIDER_ERROR", "missing authorization code")
            tokens = ga.exchange_code(self.config, code, item["verifier"], self.transport)
            client = GmailClient(self.transport, tokens["access_token"])
            account = client.profile()["email"]
            tokens["account"] = account
            self.store.save_oauth_token(item["user_id"], item["workspace_id"], "gmail", tokens)
            self.store.upsert_connection(item["user_id"], item["workspace_id"], "gmail", "CONNECTED", tokens["scopes"], "connected")
            return 302, {"location": f"{base}/#inbox-connections?gmail=connected"}
        except GmailError as e:
            code_label = {"STATE_INVALID": "state", "REVOKED": "revoked"}.get(e.code, "failed")
            return 302, {"location": f"{base}/#inbox-connections?gmail={code_label}"}

    def gmail_disconnect(self, uid, wid) -> dict:
        tok = self.store.load_oauth_token(uid, wid, "gmail")
        revoked = False
        if tok and self.config is not None:
            revoked = ga.revoke_token(tok.get("refresh_token") or tok.get("access_token"), self.transport)
        self.store.delete_oauth_token(uid, wid, "gmail")
        if self._gmail_conn(uid, wid):
            self.store.upsert_connection(uid, wid, "gmail", "NOT_CONNECTED", [], "disconnected by the user")
        return {"status": "NOT_CONNECTED", "revoked_at_provider": revoked,
                "note": "Stored messages and commitments remain until you delete them in Privacy & Settings."}

    # ------------------------------------------------------------------ sync and brief
    def sync(self, uid, wid, mode, body) -> dict:
        ws = scoped(wid, mode)
        tz, configured = self._tz(uid, wid, mode)
        query = _text(body, "query", 200, required=False) or "newer_than:30d"
        if mode == "REPLAY":
            self.store.upsert_connection(uid, ws, "replay", "REPLAY", ["replay"], "synthetic replay data")
            prog = self.replay.get((uid, wid), {})
            visible = prog.get("visible", len(rp.DATASET))
            provider = rp.ReplayProvider(self.store, uid, ws, self._anchor(uid, wid), visible)
            acct = rp.REPLAY_ADDRESS
        elif mode == "LOCAL":
            provider = self._local_client(uid, wid)
            acct = self._account(uid, wid, "LOCAL")
        else:
            provider = self._gmail_client(uid, wid)             # a refused refresh is recorded by handle() as a REVOKED/EXPIRED state
            try:
                acct = provider.profile()["email"]
            except GmailError as e:
                st = {"REVOKED": "REVOKED", "TOKEN_EXPIRED": "EXPIRED", "RATE_LIMITED": "RATE_LIMITED"}.get(e.code, "ERROR")
                self.store.upsert_connection(uid, wid, "gmail", st, (self._gmail_conn(uid, wid) or {}).get("scopes", []), e.message)
                raise ApiError(409 if st != "ERROR" else 502, f"sync stopped: {e.message}", code=st)
        try:
            out = svc.sync_connection(self.store, uid, ws, provider, acct, query=query, tz_name=tz, mode=mode)
        except TimeoutError as e:
            raise ApiError(504, "the provider did not respond in time; nothing was marked as synchronized")
        out["timezone"] = tz
        out["timezone_configured"] = configured
        return out

    def sync_status(self, uid, wid, mode) -> dict:
        ws = scoped(wid, mode)
        runs = self.store.list_sync_runs(uid, ws, 10)
        ok = [r for r in runs if r["state"] == "SUCCEEDED"]
        last_ok = ok[0]["finished_at"] if ok else None
        stale = last_ok is None or (self.now() - last_ok) > 24 * 3600
        conn = self.store.get_connection(uid, ws, PROVIDER_FOR_MODE[mode])
        count = len(self.store.list_threads(uid, ws))
        processing = "STALE" if (last_ok and stale) else ("NOT_CONNECTED" if conn is None else (conn["status"] if conn["status"] != "CONNECTED" else mode))
        return {"mode": mode, "label": self._label(mode), "connection": conn, "runs": runs, "last_successful_sync_at": last_ok,
                "stale": bool(stale and last_ok is not None), "threads": count, "messages": self.store.count_messages(uid, ws),
                "processing_state": processing, "engine": "REPLAY_RULES" if mode == "REPLAY" else "RULES_ONLY",
                "engine_label": "Sync triage uses the deterministic rules engine (RULES_ONLY).",
                "inference": self.inference.status()}

    def brief(self, uid, wid, mode) -> dict:
        ws = scoped(wid, mode)
        tz, configured = self._tz(uid, wid, mode)
        account = self._account(uid, wid, mode)
        conn = self._gmail_conn(uid, wid) if mode == "LIVE" else self.store.get_connection(uid, ws, PROVIDER_FOR_MODE[mode])
        if mode in ("LIVE", "LOCAL") and (conn is None or conn["status"] != "CONNECTED"):
            return {"mode": mode, "label": self._label(mode), "connected": False, "connection_status": (conn or {}).get("status", "NOT_CONNECTED"),
                    "account": account, "timezone": tz, "timezone_configured": configured, "summary": {}, "needs_attention": [], "replies_to_send": [],
                    "my_commitments": [], "waiting_on_others": [], "requests_to_me": [], "timing": {}, "drafts": [], "recently_resolved": []}
        b = svc.morning_brief(self.store, uid, ws, user_address=account or rp.REPLAY_ADDRESS, tz_name=tz)
        b.update({"mode": mode, "label": self._label(mode), "connected": True, "account": account, "timezone_configured": configured,
                  "engine": "REPLAY_RULES" if mode == "REPLAY" else "RULES_ONLY",
                  "last_successful_sync_at": next((r["finished_at"] for r in self.store.list_sync_runs(uid, ws, 20) if r["state"] == "SUCCEEDED"), None)})
        return b

    # ------------------------------------------------------------------ threads
    def thread_list(self, uid, wid, mode, query) -> dict:
        ws = scoped(wid, mode)
        tz, _ = self._tz(uid, wid, mode)
        account = _address(self._account(uid, wid, mode)) or rp.REPLAY_ADDRESS
        flt = (query.get("filter") or ["all"])[0]
        since_days = int((query.get("since_days") or ["0"])[0] or 0)
        cutoff = self.now() - since_days * 86400 if since_days > 0 else 0
        states = self.store.thread_states(uid, ws)
        q = ((query.get("q") or [""])[0]).lower()[:200]
        sort = (query.get("sort") or ["date"])[0]
        page = max(1, int((query.get("page") or ["1"])[0] or 1))
        size = min(50, max(1, int((query.get("size") or ["25"])[0] or 25)))
        triage = {t["thread_id"]: t for t in self.store.list_triage(uid, ws)}
        commitments = self.store.list_commitments(uid, ws)
        rows = []
        for tid, t in triage.items():
            msgs = svc._thread_messages(self.store, uid, ws, tid)
            if not msgs:
                continue
            last = max(msgs, key=lambda x: x["sent_at"])
            if last["sent_at"] < cutoff:
                continue
            st = states.get(tid)
            if st and st["state"] == "DISMISSED":
                continue
            eff = self._effective(uid, ws, tid, t)
            mine = [c for c in commitments if c["thread_id"] == tid]
            open_mine = [c for c in mine if c["kind"] == "PROMISE_BY_USER" and c["status"] in svc.OPEN_STATUSES]
            open_waiting = [c for c in mine if c["kind"] == "PROMISE_BY_OTHER" and c["status"] in svc.OPEN_STATUSES]
            open_requests = [c for c in mine if c["kind"] == "REQUEST_TO_USER" and c["status"] in svc.OPEN_STATUSES]
            awaiting = (_address(last["sender"]) != account) and any(s.endswith("?") for s in ii.sentences(last["body"]))
            deadline = min((c["deadline_at"] for c in mine if c["deadline_at"] and c["status"] in svc.OPEN_STATUSES), default=None)
            row = {"thread_id": tid, "subject": last["subject"], "sender": last["sender"], "preview": (last["body"] or "")[:140],
                   "last_activity": last["sent_at"], "message_count": len(msgs), "category": eff["category"], "priority": eff["priority"],
                   "commitments": len(mine), "reply_status": "REPLY_OWED" if awaiting or open_requests else "NONE",
                   "deadline": deadline, "review_required": eff["category"] == "UNCERTAIN_HUMAN_REVIEW",
                   "last_decision_at": t["computed_at"], "overridden": bool(eff["overridden_by"]), "mode": mode}
            if flt == "action_required" and not (eff["category"] == "ACTION_REQUIRED" or open_requests):
                continue
            if flt == "reply_required" and not (awaiting or open_requests):
                continue
            if flt == "my_commitments" and not open_mine:
                continue
            if flt == "waiting" and not open_waiting:
                continue
            if flt == "deadline" and not (eff["category"] == "DEADLINE_TIME_SENSITIVE" or deadline):
                continue
            if flt == "important" and eff["category"] != "IMPORTANT_INFORMATION":
                continue
            if flt == "automated" and eff["category"] != "AUTOMATED_LOW_PRIORITY":
                continue
            if flt == "needs_review" and eff["category"] != "UNCERTAIN_HUMAN_REVIEW":
                continue
            if q and q not in (row["subject"] + " " + row["sender"] + " " + row["preview"]).lower():
                continue
            rows.append(row)
        key = (lambda r: (-{"HIGH": 3, "NORMAL": 2, "LOW": 1}.get(r["priority"], 0), -r["last_activity"])) if sort == "priority" else (lambda r: -r["last_activity"])
        rows.sort(key=key)
        total = len(rows)
        return {"mode": mode, "label": self._label(mode), "timezone": tz, "filter": flt, "total": total, "page": page, "size": size,
                "threads": rows[(page - 1) * size: page * size]}

    def thread_detail(self, uid, wid, mode, thread_id) -> dict:
        ws = scoped(wid, mode)
        tz, _ = self._tz(uid, wid, mode)
        account = _address(self._account(uid, wid, mode)) or rp.REPLAY_ADDRESS
        msgs = self._thread_row(uid, ws, thread_id)
        t = next((x for x in self.store.list_triage(uid, ws) if x["thread_id"] == thread_id), None)
        if t is None:
            raise ApiError(404, "thread has not been analysed yet; run sync or reprocess")
        eff = self._effective(uid, ws, thread_id, t)
        messages = [{"id": m["id"], "sender": m["sender"], "recipients": m.get("to", ""), "subject": m["subject"], "sent_at": m["sent_at"],
                     "body": m["body"], "direction": "OUTBOUND" if _address(m["sender"]) == account else "INBOUND"} for m in msgs]
        commitments = [c for c in self.store.list_commitments(uid, ws) if c["thread_id"] == thread_id]
        for c in commitments:
            c["revisions"] = self.store.commitment_revisions(uid, ws, c["id"])
        drafts = [d for d in self.store.list_drafts(uid, ws) if d["thread_id"] == thread_id]
        corrections = [c for c in self.store.list_corrections(uid, ws) if c["thread_id"] == thread_id or c["scope"] == "PERSISTENT_WORKSPACE"]
        return {"thread_id": thread_id, "mode": mode, "label": self._label(mode), "timezone": tz, "messages": messages,
                "analysis": {"understanding": _understanding(eff, t), "classification": eff["category"],
                             "classification_as_stored": t["category"], "priority": eff["priority"], "confidence": t["confidence"],
                             "decision_explanation": eff["reasons"], "evidence": [{"text": e, "message_id": _message_for(messages, e)} for e in t["evidence"]],
                             "commitments": commitments,
                             "dates": [{"commitment_id": c["id"], "phrase": c["deadline_text"], "at": c["deadline_at"], "timezone": c["deadline_timezone"],
                                        "confidence": c["deadline_confidence"], "explicit": c["deadline_explicit"]} for c in commitments if c["deadline_text"] or c["deadline_at"]],
                             "uncertainty": [c["review_reason"] for c in commitments if c["review_reason"]] +
                                            (["classification needs human review"] if eff["category"] == "UNCERTAIN_HUMAN_REVIEW" else [])},
                "corrections": corrections, "drafts": drafts}

    def thread_state(self, uid, wid, mode, thread_id, body) -> dict:
        ws = scoped(wid, mode)
        state = _text(body, "state", 20)
        until = None
        if state == "DEFERRED":
            raw = _text(body, "until_local", 40, required=False)
            if raw:
                tz = self.store.get_settings(uid, ws)["timezone"]
                try:
                    until = datetime.fromisoformat(raw).replace(tzinfo=ZoneInfo(tz)).timestamp()
                except ValueError:
                    raise ApiError(400, "until_local must be a local date and time such as 2026-11-06T09:00")
        return self.store.set_thread_state(uid, ws, thread_id, state, _text(body, "reason", 200, required=False) or "", until)

    def analyze(self, uid, wid, mode, thread_id) -> dict:
        """Advisory analysis of the persisted thread: rules baseline plus, when configured, a schema-validated and
        quote-verified model answer. Nothing is written; decisions change only through corrections or reprocessing."""
        ws = scoped(wid, mode)
        msgs = self._thread_row(uid, ws, thread_id)
        tz, _ = self._tz(uid, wid, mode)
        account = _address(self._account(uid, wid, mode)) or rp.REPLAY_ADDRESS
        engine = mp.InboxInference(None, replay=True) if mode == "REPLAY" else self.inference
        out = engine.analyze(msgs, account, tz)
        return {**out, "thread_id": thread_id, "mode": mode, "advisory": True,
                "note": "Analysis only. It cannot approve, send or change rules; apply a change through a correction."}

    def reprocess(self, uid, wid, mode, thread_id) -> dict:
        ws = scoped(wid, mode)
        tz, _ = self._tz(uid, wid, mode)
        account = self._account(uid, wid, mode) or rp.REPLAY_ADDRESS
        thread = svc._thread_messages(self.store, uid, ws, thread_id)
        if not thread:
            raise ApiError(404, "thread not found")
        self.store.save_triage(uid, ws, thread_id, ii.triage_thread(thread, account, tz))
        added = 0
        for c in ii.extract_commitments(thread, account, tz):
            res = self.store.reconcile_obligation(uid, ws, thread_id, c["kind"], c["obligation_key"], c)
            added += 1 if res["action"] == "CREATED" else 0
        return {"thread_id": thread_id, "reprocessed_messages": len(thread), "new_commitments": added}

    def correct(self, uid, wid, mode, thread_id, body) -> dict:
        ws = scoped(wid, mode)
        kind = _text(body, "kind", 40)
        if kind not in CORRECTION_KINDS:
            raise ApiError(400, "unknown correction kind")
        scope = _text(body, "scope", 40)
        if scope not in CORRECTION_SCOPES:
            raise ApiError(400, "scope must be ONE_TIME, PERSISTENT_THREAD or PERSISTENT_WORKSPACE")
        confirmed = body.get("confirmed") is True
        dictated = _text(body, "dictated_text", MAX_TEXT, required=False) or ""
        value = _text(body, "value", 200, required=False) or ""
        t = next((x for x in self.store.list_triage(uid, ws) if x["thread_id"] == thread_id), None)
        if t is None:
            raise ApiError(404, "thread not found")
        before = self._effective(uid, ws, thread_id, t)
        original = {k: before[k] for k in ("category", "priority")}
        corrected, effects = {}, []
        if kind == "WRONG_PRIORITY":
            if value not in ("HIGH", "NORMAL", "LOW"):
                raise ApiError(400, "value must be HIGH, NORMAL or LOW")
            corrected["priority"] = value
        elif kind == "WRONG_CATEGORY":
            if value not in ii.CATEGORIES:
                raise ApiError(400, "value must be one of the known categories")
            corrected["category"] = value
        elif kind == "NOT_ACTIONABLE":
            corrected["category"] = "GENERAL_INFORMATION"
        elif kind == "AUTOMATED":
            corrected.update(category="AUTOMATED_LOW_PRIORITY", priority="LOW")
        elif kind == "ALREADY_COMPLETED":
            for c in [c for c in self.store.list_commitments(uid, ws, status=None) if c["thread_id"] == thread_id and c["status"] in svc.OPEN_STATUSES]:
                self.store.set_commitment_status(uid, ws, c["id"], "RESOLVED_USER_CONFIRMED", reason=dictated or "marked completed by the user")
                effects.append(f"commitment {c['id']} → RESOLVED_USER_CONFIRMED (not verified)")
        elif kind == "NOT_COMMITMENT":
            for c in [c for c in self.store.list_commitments(uid, ws) if c["thread_id"] == thread_id and c["status"] in svc.OPEN_STATUSES]:
                self.store.set_commitment_status(uid, ws, c["id"], "DISMISSED", reason=dictated or "not a commitment")
                effects.append(f"commitment {c['id']} → DISMISSED")
        elif kind == "WRONG_OWNER":
            if not EMAIL.fullmatch(value):
                raise ApiError(400, "value must be the correct email address of the responsible person")
            for c in [c for c in self.store.list_commitments(uid, ws) if c["thread_id"] == thread_id and c["status"] in svc.OPEN_STATUSES]:
                self.store.update_commitment(uid, ws, c["id"], responsible_person=value, reason="owner corrected by the user")
                effects.append(f"commitment {c['id']} responsible person corrected")
        elif kind == "WRONG_DEADLINE":
            tz = self.store.get_settings(uid, ws)["timezone"]
            try:
                at = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(tz)).timestamp()
            except (ValueError, TypeError):
                raise ApiError(400, "value must be a local date and time such as 2026-11-06T16:00")
            for c in [c for c in self.store.list_commitments(uid, ws) if c["thread_id"] == thread_id and c["status"] in svc.OPEN_STATUSES]:
                self.store.update_commitment(uid, ws, c["id"], deadline_at=at, deadline_text=value, deadline_timezone=tz, reason="deadline corrected by the user")
                effects.append(f"commitment {c['id']} deadline corrected (previous kept in history)")
        elif kind == "OTHER":
            effects.append("recorded for review only; no automatic change")
        if corrected and scope == "PERSISTENT_WORKSPACE" and not confirmed:
            raise ApiError(409, "a workspace-wide preference needs your confirmation before it is stored", code="CONFIRMATION_REQUIRED")
        after = {**before, **{k: v for k, v in corrected.items()}}
        sender = _address(max(svc._thread_messages(self.store, uid, ws, thread_id), key=lambda x: x["sent_at"])["sender"])
        record = None
        if corrected or kind in ("OTHER",):
            record = self.store.add_correction(uid, ws, thread_id, original, {**corrected, "sender": sender} if scope == "PERSISTENT_WORKSPACE" else corrected,
                                               dictated or kind, scope, confirmed)
        impact = {"ONE_TIME": "only this message thread", "PERSISTENT_THREAD": "this thread, and it stays until reversed",
                  "PERSISTENT_WORKSPACE": "threads from the same sender, after your confirmation"}[scope]
        return {"correction_id": (record or {}).get("id"), "kind": kind, "scope": scope, "impact": impact,
                "before": {"category": before["category"], "priority": before["priority"]},
                "user_correction": dictated or kind, "after": {"category": after["category"], "priority": after["priority"]},
                "effects": effects, "recorded_only": kind == "OTHER"}

    # ------------------------------------------------------------------ commitments
    def commitment_list(self, uid, wid, mode, query) -> dict:
        ws = scoped(wid, mode)
        tz, _ = self._tz(uid, wid, mode)
        status = (query.get("status") or [None])[0]
        kind = (query.get("kind") or [None])[0]
        timing = (query.get("timing") or [None])[0]
        if status and status not in COMMITMENT_STATUSES:
            raise ApiError(400, "unknown status")
        now = self.now()
        out = []
        for c in self.store.list_commitments(uid, ws, status=status, kind=kind):
            c["timing"] = svc._timing(c["deadline_at"], now, tz) if c["status"] in svc.OPEN_STATUSES else None
            if timing and c["timing"] != timing:
                continue
            c["thread_subject"] = _subject(self.store, uid, ws, c["source_message_id"])
            out.append(c)
        return {"mode": mode, "label": self._label(mode), "timezone": tz, "commitments": out}

    def commitment_detail(self, uid, wid, mode, cid) -> dict:
        ws = scoped(wid, mode)
        tz, _ = self._tz(uid, wid, mode)
        c = self.store.get_commitment(uid, ws, cid)
        src = self.store.read_message(uid, ws, c["source_message_id"])
        related = [x for x in self.store.list_commitments(uid, ws) if x["thread_id"] == c["thread_id"] and x["id"] != cid]
        return {**c, "mode": mode, "timezone": tz, "source_message": {"id": src["id"], "sender": src["sender"], "sent_at": src["sent_at"],
                                                                     "subject": src["subject"], "thread_id": src["thread_id"]},
                "revisions": self.store.commitment_revisions(uid, ws, cid), "status_history": self.store.commitment_status_history(uid, ws, cid),
                "notes": self.store.list_commitment_notes(uid, ws, cid), "follow_ups": self.store.list_follow_ups(uid, ws, cid),
                "related": [{"id": x["id"], "kind": x["kind"], "status": x["status"], "required_action": x["required_action"]} for x in related],
                "completion_evidence": c["resolution_evidence_id"]}

    def commitment_patch(self, uid, wid, mode, cid, body) -> dict:
        ws = scoped(wid, mode)
        tz, _ = self._tz(uid, wid, mode)
        self.store.get_commitment(uid, ws, cid)
        reason = _text(body, "reason", 200, required=False) or ""
        fields = {}
        if "required_action" in body:
            fields["required_action"] = _text(body, "required_action", 300)
        if "responsible_person" in body:
            v = _text(body, "responsible_person", 200)
            if not EMAIL.fullmatch(v):
                raise ApiError(400, "responsible person must be an email address")
            fields["responsible_person"] = v
        if "requesting_person" in body:
            v = _text(body, "requesting_person", 200)
            if not EMAIL.fullmatch(v):
                raise ApiError(400, "requesting person must be an email address")
            fields["requesting_person"] = v
        if "deadline_local" in body:
            raw = _text(body, "deadline_local", 40)
            try:
                at = datetime.fromisoformat(raw).replace(tzinfo=ZoneInfo(tz)).timestamp()
            except ValueError:
                raise ApiError(400, "deadline must be a local date and time such as 2026-11-06T16:00")
            fields.update(deadline_at=at, deadline_text=raw, deadline_timezone=tz)
        if fields:
            self.store.update_commitment(uid, ws, cid, reason=reason, **fields)
        if "status" in body:
            self.store.set_commitment_status(uid, ws, cid, _text(body, "status", 40), reason=reason)
        return self.commitment_detail(uid, wid, mode, cid)

    def commitment_resolve(self, uid, wid, mode, cid, body) -> dict:
        ws = scoped(wid, mode)
        evidence = _text(body, "evidence_execution_id", 80, required=False)
        if evidence:
            self.store.set_commitment_status(uid, ws, cid, "RESOLVED_VERIFIED", evidence_id=evidence, reason="verified by an execution record")
        elif body.get("confirm") is True:
            reason = _text(body, "reason", 200)
            self.store.set_commitment_status(uid, ws, cid, "RESOLVED_USER_CONFIRMED", reason=reason)
        else:
            raise ApiError(400, "resolve with evidence_execution_id (verified) or confirm:true with a reason (user-confirmed)")
        return self.commitment_detail(uid, wid, mode, cid)

    def follow_up(self, uid, wid, mode, cid, body) -> dict:
        ws = scoped(wid, mode)
        trigger = _text(body, "trigger", 60)
        c = self.store.get_commitment(uid, ws, cid)
        target = _text(body, "target", 200, required=False) or ""
        if not target:
            target = (c["requesting_person"] if trigger == "OUTSTANDING_OWN_COMMITMENT" else c["responsible_person"]) or ""
        if not target:
            raise ApiError(409, "no recipient is known for this follow-up; provide target")
        return self.store.create_follow_up(uid, ws, cid, trigger, target, _text(body, "note", 500, required=False) or "")

    def commitment_note(self, uid, wid, mode, cid, body) -> dict:
        return self.store.add_commitment_note(uid, scoped(wid, mode), cid, _text(body, "note", 1000))

    # ------------------------------------------------------------------ drafts
    def create_draft(self, uid, wid, mode, thread_id, body) -> dict:
        ws = scoped(wid, mode)
        s = self.store.get_settings(uid, ws)
        account = _address(self._account(uid, wid, mode)) or rp.REPLAY_ADDRESS
        thread = self._thread_row(uid, ws, thread_id)
        facts = body.get("facts") or {}
        if not isinstance(facts, dict) or len(facts) > 10 or any(not isinstance(v, str) or len(v) > 300 for v in facts.values()):
            raise ApiError(400, "facts must be a small object of short strings")
        draft = svc.draft_reply(thread, account, _text(body, "signoff", 60, required=False) or s["signoff"],
                                       _text(body, "tone", 40, required=False) or s["tone"], facts, s["timezone"])
        last = max(thread, key=lambda x: x["sent_at"])
        recipient = _address(last["sender"]) if _address(last["sender"]) != account else _address(last.get("recipients", ""))
        if not recipient:
            raise ApiError(409, "the recipient address could not be determined from the thread; edit the recipient before approving")
        meta = {"sources": draft["sources"], "missing_information": draft["missing_information"], "assumptions": draft["assumptions"],
                "potential_commitments": draft["potential_commitments"], "engine": "REPLAY_RULES" if mode == "REPLAY" else "RULES_ONLY",
                "generated_at": self.now()}
        stored = self.store.create_draft(uid, ws, thread_id, recipient, draft["subject"], draft["body"], origin="REPLAY_RULES" if mode == "REPLAY" else "RULES_ENGINE", meta=meta)
        return {**self.store.get_draft(uid, ws, stored["id"]), "engine": "REPLAY_RULES" if mode == "REPLAY" else "RULES_ONLY",
                "engine_label": "deterministic rules; no model inference",
                "assumptions": draft["assumptions"], "missing_information": draft["missing_information"], "sources": draft["sources"],
                "potential_commitments": draft["potential_commitments"], "recipient": recipient}

    def draft_detail(self, uid, wid, mode, did) -> dict:
        ws = scoped(wid, mode)
        d = self.store.get_draft(uid, ws, did)
        return {**d, "mode": mode, "label": self._label(mode), "versions": self.store.list_draft_versions(uid, ws, did),
                "executions": self.store.draft_executions(uid, ws, did)}

    def draft_patch(self, uid, wid, mode, did, body) -> dict:
        ws = scoped(wid, mode)
        origin = _text(body, "origin", 20, required=False) or "USER"
        if origin not in ("USER", "DICTATED", "MODEL_SUGGESTED"):
            raise ApiError(400, "origin must be USER, DICTATED or MODEL_SUGGESTED")
        fields = {}
        if "recipient" in body:
            fields["recipient"] = _text(body, "recipient", 200)
        if "cc" in body:
            fields["cc"] = _text(body, "cc", 500, required=False) or ""
        if "subject" in body:
            fields["subject"] = _text(body, "subject", 300)
        if "body" in body:
            fields["body"] = _text(body, "body", 20000)
        res = self.store.update_draft(uid, ws, did, origin=origin, **fields)
        return {**self.store.get_draft(uid, ws, did), "update": res}

    def draft_approve(self, uid, wid, mode, did, body) -> dict:
        ws = scoped(wid, mode)
        op = _text(body, "operation", 40)
        if op not in ("CREATE_GMAIL_DRAFT", "SEND_EMAIL"):
            raise ApiError(400, "operation must be CREATE_GMAIL_DRAFT or SEND_EMAIL")
        digest = _text(body, "digest", 80)
        return self.store.approve(uid, ws, did, digest, op, self.now())

    def draft_gmail(self, uid, wid, mode, did, body) -> dict:
        ws = scoped(wid, mode)
        client = self._gmail_client(uid, wid, need=ga.COMPOSE_SCOPE) if mode == "LIVE" else self._provider(uid, wid, mode)
        broker = SendBroker(self.store, client, self.now)
        return broker.create_gmail_draft(uid, ws, _text(body, "approval_id", 80), did, _text(body, "digest", 80))

    def draft_send(self, uid, wid, mode, did, body) -> dict:
        ws = scoped(wid, mode)
        if mode == "LIVE":
            client = self._gmail_client(uid, wid, need=ga.SEND_SCOPE)
        elif mode == "LOCAL":
            client = self._local_client(uid, wid)
        else:
            client = self._provider(uid, wid, mode, inject=self.replay.get((uid, wid), {}).get("inject"))
            self.replay.get((uid, wid), {}).pop("inject", None)      # a failure injection applies to one send only
        broker = SendBroker(self.store, client, self.now)
        out = broker.send_email(uid, ws, _text(body, "approval_id", 80), did, _text(body, "digest", 80))
        out["label"] = {"REPLAY": "SIMULATED (replay outbox, not Gmail)", "LOCAL": "LOCAL mail server (real SMTP delivery on this machine, not Gmail)"}.get(mode, "Gmail")
        return out

    # ------------------------------------------------------------------ executions
    def executions(self, uid, wid, mode) -> dict:
        ws = scoped(wid, mode)
        rows = self.store.list_executions(uid, ws)
        for r in rows:
            r["label"] = {"REPLAY": "SIMULATED", "LOCAL": "LOCAL MAIL"}.get(mode, "Gmail")
        return {"mode": mode, "label": self._label(mode), "executions": rows}

    def reconcile(self, uid, wid, mode, eid) -> dict:
        ws = scoped(wid, mode)
        client = self._provider(uid, wid, mode)
        broker = SendBroker(self.store, client, self.now)
        try:
            return broker.reconcile(uid, ws, eid)
        except Exception as e:
            if type(e).__name__ == "BrokerRefused":
                raise ApiError(409, str(e))
            raise

    # ------------------------------------------------------------------ rules
    def rules(self, uid, wid) -> dict:
        rules = self.store.list_voice_rules(uid, wid)
        return {"rules": rules, "active": [r for r in rules if r["active"]], "corrections": self.store.list_corrections(uid, wid),
                "active_count": len([r for r in rules if r["active"]])}

    def rules_parse(self, uid, wid, body) -> dict:
        text = _text(body, "text", 2000)
        parsed = parse_instruction(text)
        active = [r["parsed"] for r in self.store.list_voice_rules(uid, wid) if r["active"]]
        return {"parsed": parsed, "conflicts": conflicts(parsed, active), "requires_confirmation": requires_confirmation(parsed),
                "unknown_count": sum(1 for p in parsed if p["type"] == "UNKNOWN"),
                "note": "Preview only. Nothing is stored or activated until you confirm."}

    def rules_add(self, uid, wid, body) -> dict:
        """Validate every rule BEFORE storing any of them. UNKNOWN rules are stored for review and never activated."""
        text = _text(body, "text", 2000)
        confirm = body.get("confirm") is True
        parsed = parse_instruction(text)
        active = [r["parsed"] for r in self.store.list_voice_rules(uid, wid) if r["active"]]
        found = conflicts(parsed, active)
        if any(p["type"] != "APPROVAL_REQUIRED_TO_SEND" and re.search(r"\bsend\b.*\bwithout\b", p["instruction"], re.I) for p in parsed):
            raise ApiError(409, "a rule that sends email without your approval is refused: every send needs your approval of the exact draft")
        if confirm and found:
            raise ApiError(409, "this conflicts with active rules; deactivate the older rule first", conflicts=found)
        if confirm and any(p["type"] in HIGH_IMPACT and body.get("confirm_high_impact") is not True for p in parsed):
            raise ApiError(409, "high-impact rules need confirm_high_impact:true", rule_types=sorted({p["type"] for p in parsed if p["type"] in HIGH_IMPACT}))
        stored, unknown = [], []
        for p in parsed:
            scope = "WORKSPACE" if p["scope"] == "ALL_MESSAGES" else p["scope"]
            is_unknown = p["type"] == "UNKNOWN"
            if is_unknown:
                unknown.append(p["instruction"])
            stored.append(self.store.add_voice_rule(uid, wid, p["instruction"], p, scope, confirmed=confirm and not is_unknown))
        return {"stored": stored, "conflicts": found, "activated": confirm and not unknown and bool(parsed),
                "not_activated_unknown": unknown,
                "note": "instructions RESOLVE does not recognise were stored for review and are not active" if unknown else ""}

    def rules_patch(self, uid, wid, rid, body) -> dict:
        current = self.store.get_voice_rule(uid, wid, rid)
        if "active" in body:
            active = body.get("active") is True
            if active:
                return self.rules_confirm(uid, wid, rid, body)
            return {**self.store.set_voice_rule_active(uid, wid, rid, False), "note": "deactivated"}
        instruction = _text(body, "instruction", 2000)
        parsed = parse_instruction(instruction)
        stored = self.store.add_voice_rule(uid, wid, instruction, parsed[0] if parsed else current["parsed"], current["scope"], confirmed=False)
        return {"new_version": stored, "note": "edited rules are stored as a new inactive version; confirm to activate"}

    def rules_confirm(self, uid, wid, rid, body) -> dict:
        rule = self.store.get_voice_rule(uid, wid, rid)
        if rule["parsed"].get("type") == "UNKNOWN":
            raise ApiError(409, "an unrecognised instruction cannot be activated")
        active = [r["parsed"] for r in self.store.list_voice_rules(uid, wid) if r["active"] and r["id"] != rid]
        found = conflicts([rule["parsed"]], active)
        if found:
            raise ApiError(409, "conflicts with an active rule; the active rule is not replaced silently", conflicts=found)
        if rule["parsed"].get("type") in HIGH_IMPACT and body.get("confirm_high_impact") is not True:
            raise ApiError(409, "this is a high-impact rule; confirm it explicitly", rule_type=rule["parsed"]["type"], requires="confirm_high_impact")
        return self.store.set_voice_rule_active(uid, wid, rid, True)

    def rules_reverse(self, uid, wid, rid) -> dict:
        return self.store.set_voice_rule_active(uid, wid, rid, False)

    # ------------------------------------------------------------------ activity
    def activity(self, uid, wid, query) -> dict:
        limit = min(200, max(1, int((query.get("limit") or ["50"])[0] or 50)))
        labels = {"connection.status": "Connection status changed", "connection.token_deleted": "Gmail access removed",
                  "message.imported": "Message stored", "message.changed": "Message changed on provider", "commitment.created": "Commitment recorded",
                  "commitment.status": "Commitment status changed", "commitment.deadline_revised": "Deadline revised (previous kept)",
                  "commitment.corrected": "Commitment corrected", "commitment.note_added": "Note added", "draft.created": "Draft created",
                  "draft.updated": "Draft edited (new version)", "draft.rejected": "Draft rejected", "approval.granted": "Exact content approved",
                  "execution.started": "Operation started", "execution.verified": "Operation verified", "execution.uncertain": "Operation uncertain",
                  "execution.failed": "Operation failed", "execution.needs_recovery": "Operation needs recovery", "voice_rule.added": "Rule stored",
                  "voice_rule.active": "Rule activation changed", "correction.added": "Correction recorded", "correction.reversed": "Correction reversed",
                  "settings.updated": "Settings changed", "follow_up.created": "Follow-up recorded"}
        out = []
        for e in self.store.audit_log(uid, wid, limit):
            out.append({"at": e["created_at"], "event": labels.get(e["action"], e["action"]), "action": e["action"], "subject_id": e["subject_id"],
                        "detail": e["detail"]})
        return {"events": out, "note": "Activity shows identifiers and statuses only, never message bodies."}

    # ------------------------------------------------------------------ replay lab
    def replay_scenarios(self, uid, wid) -> dict:
        prog = self.replay.get((uid, wid), {"state": "IDLE", "visible": 0, "log": []})
        total = len(rp.DATASET)
        return {"label": "SYNTHETIC REPLAY DATA", "threads": rp.scenario_summary(), "messages": total, "steps_total": total + len(REPLAY_SCRIPT_EXTRA),
                "state": prog["state"], "visible": prog["visible"], "log": prog["log"][-30:],
                "injectable_failures": ["send_timeout"], "note": "Replay runs the same sync, triage, commitment and send-broker code as LIVE. Sends are SIMULATED."}

    def _replay_set(self, uid, wid, state) -> dict:
        prog = self.replay.setdefault((uid, wid), {"state": "IDLE", "visible": 0, "log": [], "inject": {}, "script": 0})
        if prog["state"] == "IDLE":
            raise ApiError(409, "start the replay first")
        prog["state"] = state
        return {"state": state}

    def replay_start(self, uid, wid) -> dict:
        ws = scoped(wid, "REPLAY")
        self.store.upsert_connection(uid, ws, "replay", "REPLAY", ["replay"], "synthetic replay data")
        self._anchor(uid, wid)
        prog = self.replay.setdefault((uid, wid), {"state": "IDLE", "visible": 0, "log": [], "inject": {}, "script": 0})
        prog["state"] = "RUNNING"
        return {"state": "RUNNING", "visible": prog["visible"], "label": self._label("REPLAY")}

    def replay_reset(self, uid, wid) -> dict:
        self.store.purge(uid, scoped(wid, "REPLAY"))
        self.replay[(uid, wid)] = {"state": "IDLE", "visible": 0, "log": [], "inject": {}, "script": 0}
        return {"state": "IDLE", "reset": True, "note": "replay data removed; live data was not touched"}

    def replay_step(self, uid, wid, body) -> dict:
        prog = self.replay.get((uid, wid))
        if prog is None or prog["state"] == "IDLE":
            raise ApiError(409, "start the replay first")
        if body.get("inject") == "send_timeout":
            prog["inject"] = {"send": "timeout_after_store"}
        total = len(rp.DATASET)
        ws = scoped(wid, "REPLAY")
        if prog["visible"] < total:
            prog["visible"] += 1
            out = self.sync(uid, wid, "REPLAY", {})
            msg = rp.DATASET[prog["visible"] - 1]
            entry = {"step": prog["visible"], "action": "INGEST", "message": msg[1], "thread": self.store.thread_by_provider_id(uid, ws, f"replay-{msg[0]}"),
                     "label": "SYNTHETIC",
                     "result": {"new_commitments": out["new_commitments"], "fetched": out["fetched"]}}
        else:
            extra = REPLAY_SCRIPT_EXTRA[prog["script"]] if prog["script"] < len(REPLAY_SCRIPT_EXTRA) else None
            if extra is None:
                return {"state": "COMPLETE", "log": prog["log"][-30:], "note": "the scenario has finished; reset to replay again"}
            entry = self._replay_script(uid, wid, extra, prog)
            prog["script"] += 1
        prog["log"].append(entry)
        if prog["visible"] >= total and prog["script"] >= len(REPLAY_SCRIPT_EXTRA):
            prog["state"] = "COMPLETE"
        return {"state": prog["state"], "entry": entry, "log": prog["log"][-30:], "visible": prog["visible"]}

    def _replay_script(self, uid, wid, step, prog) -> dict:
        ws = scoped(wid, "REPLAY")
        if step == "draft_T1":
            tid = self.store.thread_by_provider_id(uid, ws, "replay-T1")
            d = self.create_draft(uid, wid, "REPLAY", tid, {"facts": {"status": "I will send the revised checklist before 4 PM today."}})
            return {"step": "draft", "action": "DRAFT", "thread": tid, "draft_id": d["id"], "label": "SYNTHETIC · RULES ENGINE",
                    "result": {"missing_information": d["missing_information"], "version": d["version"]}}
        if step == "approve_send_T1":
            t1 = self.store.thread_by_provider_id(uid, ws, "replay-T1")
            drafts = [x for x in self.store.list_drafts(uid, ws) if x["thread_id"] == t1 and x["status"] == "DRAFT"]
            if not drafts:
                raise ApiError(409, "no draft to approve")
            did = drafts[0]["id"]
            detail = self.store.get_draft(uid, ws, did)
            appr = self.store.approve(uid, ws, did, detail["digest"], "SEND_EMAIL", self.now())
            out = self.draft_send(uid, wid, "REPLAY", did, {"approval_id": appr["id"], "digest": detail["digest"]})
            return {"step": "send", "action": "APPROVE_AND_SEND", "thread": t1, "draft_id": did, "label": "SIMULATED — not a Gmail send",
                    "result": {"state": out["state"], "reason": out.get("reason"), "execution_id": out.get("execution_id")}}
        if step == "draft_T7":
            tid = self.store.thread_by_provider_id(uid, ws, "replay-T7")
            d = self.create_draft(uid, wid, "REPLAY", tid, {})
            return {"step": "draft", "action": "DRAFT", "thread": tid, "draft_id": d["id"], "label": "SYNTHETIC · RULES ENGINE",
                    "result": {"missing_information": d["missing_information"], "note": "not sent; needs your price or estimate"}}
        raise ApiError(500, "unknown replay step")


# ---------------------------------------------------------------------- module helpers
def _text(body: dict, key: str, limit: int, required: bool = True) -> str | None:
    v = body.get(key)
    if v is None or v == "":
        if required:
            raise ApiError(400, f"{key} is required")
        return None
    if not isinstance(v, str):
        raise ApiError(400, f"{key} must be text")
    if len(v) > limit:
        raise ApiError(413, f"{key} is too long")
    return v.strip()


def _message_for(messages: list[dict], evidence: str) -> str | None:
    for m in messages:
        if evidence and evidence.strip() and evidence.strip() in m["body"]:
            return m["id"]
    return None


def _subject(store, uid, ws, message_id) -> str:
    try:
        return store.read_message(uid, ws, message_id)["subject"]
    except InboxError:
        return ""


def _understanding(eff: dict, t: dict) -> str:
    cat = eff["category"]
    if cat == "AUTOMATED_LOW_PRIORITY":
        return "Automated notice. No commitment and no reply is needed."
    if cat == "UNCERTAIN_HUMAN_REVIEW":
        return "A deadline or request is unclear. Review it before anything is prepared."
    if cat == "REPLY_REQUIRED":
        return "The latest message asks a question that you have not answered."
    if cat == "ACTION_REQUIRED":
        return "Someone asked you to do something."
    if cat == "DEADLINE_TIME_SENSITIVE":
        return "You were asked to do something by a stated time."
    if cat == "WAITING_ON_SOMEONE":
        return "Someone else owes you a deliverable, or you are waiting on your own promise."
    if cat == "GENERAL_INFORMATION":
        return "Informational. No action detected."
    return "Classified as " + cat.lower().replace("_", " ") + "."
