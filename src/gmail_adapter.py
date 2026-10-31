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


# ---------- send path: build, send, draft, read back, search by operation marker

def operation_marker(operation_key: str) -> str:
    """A visible line placed in the message body. Gmail search can find it later, so a timed-out send can be checked."""
    return f"[RESOLVE operation {operation_key}]"


def build_raw_message(to: str, subject: str, body: str, operation_key: str) -> str:
    msg = EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(f"{body}\n\n{operation_marker(operation_key)}")
    return base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")


def parse_raw(raw_b64: str) -> dict:
    """Read back a raw message (what the provider actually stored): recipient, subject, body text."""
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
    return {"to": m.get("To", ""), "subject": m.get("Subject", ""), "body": body}


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

    def find_by_marker(self, operation_key: str) -> list[str]:
        """Messages in Sent that carry this operation's marker. An empty list does NOT prove the send did not happen."""
        q = f'in:sent "{operation_marker(operation_key)}"'
        data = self._call("GET", "/messages", params={"q": q, "maxResults": 5})
        return [m["id"] for m in data.get("messages", [])]
