"""Gmail provider adapter (the only external email provider in this product).

- OAuth 2.0 authorization code flow with PKCE (S256). Read-only scope by default.
- Configuration comes from the environment only: GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,
  GOOGLE_OAUTH_REDIRECT_URI. Without them the status is NOT_CONFIGURED and nothing is fetched.
- HTTP is performed by an injected `transport(method, url, headers, body) -> (status, json_dict, response_headers)`,
  so tests use recorded fixtures and the production transport is the only code that touches the network.
- Errors are typed: TOKEN_EXPIRED (401, refresh once), REVOKED (403 invalid_grant / revoked),
  RATE_LIMITED (429, with retry_after), PROVIDER_ERROR (5xx), NOT_FOUND (404, inaccessible message).
- Tokens are never logged, never placed in exceptions and never sent to a model.
"""
from __future__ import annotations

import base64
import hashlib
import secrets
import time
from email import message_from_bytes
from email.message import EmailMessage
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import urlencode

READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"          # drafts only; requested only when the user enables drafting
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"               # requested only when the user enables sending
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailError(Exception):
    def __init__(self, code: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.code, self.message, self.retry_after = code, message, retry_after


@dataclass(frozen=True)
class GmailConfig:
    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def from_env(cls, env) -> "GmailConfig | None":
        cid = env.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
        secret = env.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
        redirect = env.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip()
        if not (cid and secret and redirect):
            return None
        return cls(cid, secret, redirect)


def connection_status(config: GmailConfig | None, has_token: bool, revoked: bool = False) -> dict:
    """Honest status for the Connections screen. Only CONNECTED is reached after a successful token exchange."""
    if config is None:
        return {"status": "NOT_CONFIGURED", "detail": "Set GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REDIRECT_URI."}
    if revoked:
        return {"status": "REVOKED", "detail": "Google reports this access was revoked. Reconnect to continue."}
    if not has_token:
        return {"status": "NOT_CONNECTED", "detail": "Gmail is configured but no account is connected yet."}
    return {"status": "CONNECTED", "detail": "Connected with read-only access."}


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorization_url(config: GmailConfig, state: str, code_challenge: str, scopes: list[str] | None = None) -> str:
    scopes = scopes or [READONLY_SCOPE]
    if READONLY_SCOPE not in scopes:
        scopes = [READONLY_SCOPE] + scopes
    params = {"client_id": config.client_id, "redirect_uri": config.redirect_uri, "response_type": "code",
              "scope": " ".join(sorted(set(scopes))), "access_type": "offline", "include_granted_scopes": "true",
              "state": state, "code_challenge": code_challenge, "code_challenge_method": "S256", "prompt": "consent"}
    return f"{AUTH_URL}?{urlencode(params)}"


def _decode_b64url(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _walk_parts(part: dict):
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk_parts(child)


def parse_message(resource: dict) -> dict:
    """Convert a Gmail API message resource (format=full) into the fields RESOLVE stores. Pure function."""
    headers = {h["name"].lower(): h["value"] for h in resource.get("payload", {}).get("headers", [])}
    body = ""
    for part in _walk_parts(resource.get("payload", {})):
        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
            body = _decode_b64url(part["body"]["data"])
            break
    sent_at = resource.get("internalDate")
    sent_ts = int(sent_at) / 1000.0 if sent_at else time.time()
    if not sent_at and headers.get("date"):
        try:
            sent_ts = parsedate_to_datetime(headers["date"]).timestamp()
        except (TypeError, ValueError):
            pass
    return {"provider_message_id": resource["id"], "provider_thread_id": resource.get("threadId", resource["id"]),
            "sender": headers.get("from", "")[:200], "to": headers.get("to", "")[:500], "subject": headers.get("subject", "")[:300],
            "sent_at": sent_ts, "body": body, "label_ids": resource.get("labelIds", [])}


@dataclass
class SyncPage:
    message_ids: list[str] = field(default_factory=list)
    next_page_token: str | None = None


class GmailClient:
    """All Gmail calls go through here. `refresh` returns a new access token or raises GmailError(REVOKED)."""

    def __init__(self, transport: Callable, access_token: str, refresh: Callable[[], str] | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_retries: int = 3):
        self._transport = transport
        self._token = access_token
        self._refresh = refresh
        self._sleep = sleep
        self._max_retries = max_retries

    def _call(self, method: str, path: str, body=None, params: dict | None = None) -> dict:  # noqa: C901
        url = f"{API}{path}" + (f"?{urlencode(params)}" if params else "")
        refreshed = False
        attempt = 0
        while True:
            status, data, headers = self._transport(method, url, {"Authorization": f"Bearer {self._token}"}, body)
            if status == 200 or status == 204:
                return data or {}
            if status == 401:
                if self._refresh is None or refreshed:
                    raise GmailError("TOKEN_EXPIRED", "access token expired and could not be refreshed")
                self._token = self._refresh()
                refreshed = True
                continue
            if status == 403 and ("invalid_grant" in str(data) or "revoked" in str(data).lower()):
                raise GmailError("REVOKED", "Google reports this access was revoked")
            if status == 429:
                retry = float((headers or {}).get("Retry-After", 1) or 1)
                attempt += 1
                if attempt > self._max_retries:
                    raise GmailError("RATE_LIMITED", "Gmail rate limit reached; try again later", retry_after=retry)
                self._sleep(min(retry, 30))
                continue
            if status == 404:
                raise GmailError("NOT_FOUND", "message is no longer accessible")
            if status >= 500:
                raise GmailError("PROVIDER_ERROR", f"Gmail returned an error ({status})")
            raise GmailError("PROVIDER_ERROR", f"unexpected response ({status})")

    def list_page(self, query: str = "", page_token: str | None = None, max_results: int = 100) -> SyncPage:
        params = {"maxResults": max_results}
        if query:
            params["q"] = query
        if page_token:
            params["pageToken"] = page_token
        data = self._call("GET", "/messages", params=params)
        return SyncPage([m["id"] for m in data.get("messages", [])], data.get("nextPageToken"))

    def get_message(self, message_id: str) -> dict:
        return parse_message(self._call("GET", f"/messages/{message_id}", params={"format": "full"}))

    def profile(self) -> dict:
        """The authenticated mailbox identity. Ownership of commitments is decided against this address."""
        data = self._call("GET", "/profile")
        return {"email": (data.get("emailAddress") or "").lower(), "messages_total": data.get("messagesTotal")}

    def sync_all(self, query: str = "", max_pages: int = 50) -> list[dict]:
        """Full pagination. Incremental sync is done by the caller with a stored history checkpoint (see inbox_sync)."""
        out, token = [], None
        for _ in range(max_pages):
            page = self.list_page(query, token)
            out.extend(self.get_message(mid) for mid in page.message_ids)
            token = page.next_page_token
            if not token:
                break
        return out


# ---------- send path: build, send, draft, read back, correlate by RFC 822 Message-ID

# The correlation identifier is an RFC 822 Message-ID header. It is not visible in the message body (no marker, footer,
# comment or invisible text is ever added to the approved content). Gmail search supports `rfc822msgid:` for Sent and
# Drafts, so a send whose response was lost can be located read-only. Whether Gmail keeps a client-supplied Message-ID
# unchanged is provider behaviour that must be confirmed with a live account; if it is rewritten, read-back fails
# closed to UNCERTAIN and nothing is resent.
CORRELATION_DOMAIN = "mail.resolve.invalid"


def correlation_id(operation_key: str) -> str:
    """Message-ID value without angle brackets (the form Gmail's rfc822msgid: operator uses)."""
    return f"resolve.{operation_key}@{CORRELATION_DOMAIN}"


def normalize_body(text: str) -> str:
    """Safe normalization for comparing the approved body with the stored body: line endings and surrounding whitespace only."""
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def build_raw_message(to: str, subject: str, body: str, operation_key: str, cc: str | None = None) -> str:
    """The message carries exactly the approved content plus standard addressing headers and the correlation Message-ID."""
    msg = EmailMessage()
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{correlation_id(operation_key)}>"
    msg.set_content(body)
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")


def parse_raw(raw_b64: str) -> dict:
    """Read back a raw message (what the provider actually stored): addressing, subject, Message-ID and body text."""
    data = base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4))
    m = message_from_bytes(data)
    body = ""
    if m.is_multipart():
        for part in m.walk():
            if part.get_content_type() == "text/plain":
                body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                break
    else:
        body = m.get_payload(decode=True).decode("utf-8", errors="replace")
    return {"to": m.get("To", ""), "cc": m.get("Cc", ""), "subject": m.get("Subject", ""),
            "message_id": (m.get("Message-ID", "") or "").strip().strip("<>"), "body": body}


class GmailSendClient(GmailClient):
    def send_raw(self, raw: str) -> dict:
        return self._call("POST", "/messages/send", body={"raw": raw})

    def create_draft(self, raw: str) -> dict:
        return self._call("POST", "/drafts", body={"message": {"raw": raw}})

    def get_draft_raw(self, draft_id: str) -> dict:
        data = self._call("GET", f"/drafts/{draft_id}", params={"format": "raw"})
        return parse_raw(data["message"]["raw"])

    def get_message_raw(self, message_id: str) -> dict:
        data = self._call("GET", f"/messages/{message_id}", params={"format": "raw"})
        return parse_raw(data["raw"])

    def find_by_correlation(self, operation_key: str) -> list[str]:
        """Sent messages with this correlation Message-ID. An empty list does NOT prove the send did not happen."""
        data = self._call("GET", "/messages", params={"q": f"in:sent rfc822msgid:{correlation_id(operation_key)}", "maxResults": 5})
        return [m["id"] for m in data.get("messages", [])]

    def find_draft_by_correlation(self, operation_key: str) -> list[str]:
        data = self._call("GET", "/messages", params={"q": f"in:drafts rfc822msgid:{correlation_id(operation_key)}", "maxResults": 5})
        return [m["id"] for m in data.get("messages", [])]


# ---------- OAuth exchange, refresh, revocation and state (transport injected; tests use recorded responses)

OAUTH_STATE_TTL = 600.0
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def exchange_code(config: GmailConfig, code: str, verifier: str, transport: Callable) -> dict:
    """Authorization code -> tokens. Tokens are returned to the caller only for sealed storage; they are never logged."""
    status, data, _ = transport("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, {
        "code": code, "client_id": config.client_id, "client_secret": config.client_secret, "redirect_uri": config.redirect_uri,
        "grant_type": "authorization_code", "code_verifier": verifier})
    if status != 200 or not (data or {}).get("access_token"):
        err = (data or {}).get("error", "")
        if err == "invalid_grant":
            raise GmailError("REVOKED", "the authorization code was rejected or has expired; start the connection again")
        raise GmailError("PROVIDER_ERROR", "Google did not issue tokens for this authorization")
    granted = set((data.get("scope") or "").split())
    if READONLY_SCOPE not in granted:
        raise GmailError("PROVIDER_ERROR", "the read-only Gmail permission was not granted")
    return {"access_token": data["access_token"], "refresh_token": data.get("refresh_token"),
            "expires_at": time.time() + float(data.get("expires_in", 3600)), "scopes": sorted(granted)}


def refresh_access_token(config: GmailConfig, refresh_token: str, transport: Callable) -> dict:
    status, data, _ = transport("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, {
        "client_id": config.client_id, "client_secret": config.client_secret, "refresh_token": refresh_token, "grant_type": "refresh_token"})
    if status == 200 and (data or {}).get("access_token"):
        return {"access_token": data["access_token"], "expires_at": time.time() + float(data.get("expires_in", 3600))}
    if (data or {}).get("error") == "invalid_grant":
        raise GmailError("REVOKED", "Google reports this access was revoked; reconnect to continue")
    raise GmailError("TOKEN_EXPIRED", "the access token could not be refreshed")


def revoke_token(token: str, transport: Callable) -> bool:
    """Best-effort revocation at Google. Returns whether Google confirmed it; local deletion happens regardless."""
    try:
        status, _, _ = transport("POST", REVOKE_URL, {"Content-Type": "application/x-www-form-urlencoded"}, {"token": token})
        return status == 200
    except (TimeoutError, OSError):
        return False


class OAuthStates:
    """Server-side, single-use, expiring state for the Gmail authorization round trip. Each state is bound to the user and
    workspace that started it, so a callback cannot attach a mailbox to a different account."""

    def __init__(self, now: Callable[[], float] = time.time, max_pending: int = 500):
        self._now = now
        self._items: dict[str, dict] = {}
        self._max = max_pending

    def issue(self, user_id: str, workspace_id: str, scopes: list[str] | None = None) -> dict:
        self._sweep()
        if len(self._items) >= self._max:
            raise GmailError("RATE_LIMITED", "too many pending connections; try again shortly", retry_after=60)
        state = secrets.token_urlsafe(32)
        verifier, challenge = pkce_pair()
        self._items[state] = {"user_id": user_id, "workspace_id": workspace_id, "verifier": verifier, "scopes": list(scopes or []),
                              "expires_at": self._now() + OAUTH_STATE_TTL}
        return {"state": state, "code_challenge": challenge}        # the verifier never leaves the server

    def consume(self, state: str) -> dict:
        self._sweep()
        item = self._items.pop(state, None)            # single use: removed on first presentation, valid or not
        if item is None or self._now() > item["expires_at"]:
            raise GmailError("STATE_INVALID", "this connection request is unknown or has expired; start again")
        return item

    def _sweep(self) -> None:
        now = self._now()
        for k in [k for k, v in self._items.items() if v["expires_at"] < now]:
            self._items.pop(k, None)


def http_transport(method: str, url: str, headers: dict, body) -> tuple[int, dict, dict]:
    """The only function that touches the network. Bodies: dict for JSON, or a form for application/x-www-form-urlencoded.
    Times out after 15 seconds. The caller decides what a timeout means (UNCERTAIN for sends)."""
    import json as _json
    import socket
    import urllib.error
    import urllib.request
    from urllib.parse import urlencode as _enc
    headers = dict(headers)
    data = None
    if body is not None:
        if headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
            data = _enc(body).encode()
        else:
            data = _json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (_json.loads(raw) if raw else {}), dict(resp.headers)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = _json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        return e.code, payload, dict(e.headers or {})
    except (socket.timeout, TimeoutError) as e:
        raise TimeoutError("provider did not respond in time") from e
