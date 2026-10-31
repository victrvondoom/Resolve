"""RESOLVE local mail environment: a small, real, persistent mail server for LOCAL mode.

This is NOT Gmail and never claims to be. It exists so the whole inbox product can be operated end to end without any
external account:

* SMTP submission (RFC 5321 subset: EHLO/HELO, AUTH PLAIN, MAIL, RCPT, DATA, RSET, NOOP, QUIT) on 127.0.0.1.
* POP3 retrieval (RFC 1939 subset: USER, PASS, STAT, LIST, UIDL, RETR, DELE, RSET, NOOP, QUIT) on 127.0.0.1.
* Storage is a Maildir per account and folder on disk (`<root>/mail/<address>/<INBOX|Sent|Drafts>`), so mail survives
  restarts and can be inspected with any Maildir tool, independent of RESOLVE's own database.
* Delivery is LOCAL ONLY: a recipient outside the configured local domain is refused with 550 (no relay), so nothing can
  leave this machine. Every submission must be authenticated and MAIL FROM must equal the authenticated address.
* A message sent to `<local>+drafts@<domain>` by its owner is filed in that owner's Drafts folder and delivered nowhere.
* POP3 user `<address>` reads INBOX, `<address>/Sent` reads Sent and `<address>/Drafts` reads Drafts.

`LocalMailProvider` is the RESOLVE provider adapter for this server. It implements the same interface as
`gmail_adapter.GmailSendClient` (profile, sync_all, send_raw, get_message_raw, create_draft, get_draft_raw,
find_by_correlation, find_draft_by_correlation), so the same sync, triage, broker and verifier code runs against it.
It talks to the server only through real SMTP and POP3 sockets (stdlib smtplib / poplib); read-back after a send is an
independent POP3 fetch of the Sent folder, not the SMTP response.

CLI:
  python src/local_mail.py serve [--root data/localmail] [--smtp-port 2525] [--pop3-port 2110]
  python src/local_mail.py add-account ADDRESS PASSWORD [--root ...]
  python src/local_mail.py send --from A --password P --to B --subject S --body TEXT [--smtp-port ...]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import mailbox
import os
import poplib
import re
import secrets
import smtplib
import socket
import socketserver
import threading
import time
from email import message_from_bytes, policy
from email.utils import formatdate, getaddresses, make_msgid, parsedate_to_datetime

from gmail_adapter import GmailError, correlation_id, parse_raw

DEFAULT_DOMAIN = "mail.resolve.test"          # RFC 2606 reserved .test TLD: cannot collide with a real mail domain
MAX_MESSAGE = 2 * 1024 * 1024
FOLDERS = ("INBOX", "Sent", "Drafts")
ADDR_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}$")


# ---------------------------------------------------------------------------------------------------------- storage
def _key_order(key: str) -> tuple:
    """Delivery order from a Maildir key '<sec>.M<usec>P<pid>Q<n>.<host>' (usec is not zero padded, so compare numbers)."""
    m = re.match(r"^(\d+)\.M(\d+)P(\d+)Q(\d+)", key)
    return (int(m.group(1)), int(m.group(2)), int(m.group(4)), key) if m else (0, 0, 0, key)


class MailStore:
    """Accounts (salted PBKDF2 password hashes) and per-folder Maildirs under one root directory."""

    def __init__(self, root: str, domain: str = DEFAULT_DOMAIN):
        self.root = os.path.abspath(root)
        self.domain = domain.lower()
        self._lock = threading.RLock()
        os.makedirs(os.path.join(self.root, "mail"), exist_ok=True)
        self._accounts_path = os.path.join(self.root, "accounts.json")

    # accounts
    def _load(self) -> dict:
        try:
            with open(self._accounts_path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _save(self, data: dict) -> None:
        tmp = self._accounts_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        os.replace(tmp, self._accounts_path)

    @staticmethod
    def _hash(password: str, salt: bytes) -> str:
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000).hex()

    def is_local(self, address: str) -> bool:
        return address.lower().endswith("@" + self.domain)

    def create_account(self, address: str, password: str) -> dict:
        address = address.strip().lower()
        if not ADDR_RE.match(address) or not self.is_local(address) or "+" in address.split("@")[0]:
            raise ValueError(f"local accounts must be plain addresses at @{self.domain}")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        with self._lock:
            data = self._load()
            if address in data:
                raise ValueError("account already exists")
            salt = secrets.token_bytes(16)
            data[address] = {"salt": salt.hex(), "hash": self._hash(password, salt), "created_at": int(time.time())}
            self._save(data)
            for folder in FOLDERS:
                self._maildir(address, folder)
        return {"address": address}

    def ensure_account(self, address: str, password: str) -> None:
        if not self.account_exists(address):
            self.create_account(address, password)

    def account_exists(self, address: str) -> bool:
        return address.strip().lower() in self._load()

    def check_password(self, address: str, password: str) -> bool:
        acct = self._load().get(address.strip().lower())
        if not acct:
            self._hash(password, b"0" * 16)   # equalize timing for unknown accounts
            return False
        return hmac.compare_digest(acct["hash"], self._hash(password, bytes.fromhex(acct["salt"])))

    def accounts(self) -> list[str]:
        return sorted(self._load())

    # mailboxes
    def _maildir(self, address: str, folder: str) -> mailbox.Maildir:
        if folder not in FOLDERS:
            raise ValueError("unknown folder")
        safe = address.lower().replace("/", "_")
        path = os.path.join(self.root, "mail", safe, folder)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return mailbox.Maildir(path, factory=None, create=True)

    def deliver(self, address: str, folder: str, data: bytes) -> str:
        with self._lock:
            return self._maildir(address, folder).add(data)

    def list(self, address: str, folder: str) -> list[tuple[str, bytes]]:
        """(key, raw bytes) sorted by delivery time. Maildir keys start with the delivery timestamp."""
        md = self._maildir(address, folder)
        out = []
        for key in md.keys():
            try:
                f = md.get_file(key)
                try:
                    out.append((key, f.read()))
                finally:
                    f.close()
            except KeyError:
                continue
        out.sort(key=lambda kv: _key_order(kv[0]))
        return out

    def remove(self, address: str, folder: str, key: str) -> None:
        with self._lock:
            try:
                self._maildir(address, folder).remove(key)
            except KeyError:
                pass

    def counts(self, address: str) -> dict:
        return {f: len(self._maildir(address, f).keys()) for f in FOLDERS}


# ---------------------------------------------------------------------------------------------------------- SMTP
class _SMTPHandler(socketserver.StreamRequestHandler):
    timeout = 60

    def _send(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())

    def _readline(self) -> str | None:
        raw = self.rfile.readline(8192)
        if not raw:
            return None
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")

    def handle(self):  # noqa: C901 — a small protocol state machine
        store: MailStore = self.server.store
        user, sender, rcpts = None, None, []
        self._send(f"220 {store.domain} RESOLVE local mail ESMTP (local delivery only)")
        while True:
            try:
                line = self._readline()
            except (socket.timeout, ConnectionError):
                return
            if line is None:
                return
            verb, _, arg = line.partition(" ")
            verb = verb.upper()
            if verb in ("EHLO", "HELO"):
                if verb == "EHLO":
                    self._send(f"250-{store.domain}")
                    self._send(f"250-SIZE {MAX_MESSAGE}")
                    self._send("250-8BITMIME")
                    self._send("250 AUTH PLAIN")
                else:
                    self._send(f"250 {store.domain}")
            elif verb == "AUTH":
                mech, _, initial = arg.partition(" ")
                if mech.upper() != "PLAIN":
                    self._send("504 only AUTH PLAIN is supported")
                    continue
                if not initial:
                    self._send("334 ")
                    initial = self._readline() or ""
                try:
                    parts = base64.b64decode(initial).split(b"\0")
                    login, password = parts[1].decode(), parts[2].decode()
                except Exception:
                    self._send("501 malformed AUTH PLAIN")
                    continue
                if store.check_password(login, password):
                    user = login.lower()
                    self._send("235 authentication succeeded")
                else:
                    self._send("535 authentication failed")
            elif verb == "MAIL":
                if user is None:
                    self._send("530 authentication required")
                    continue
                m = re.match(r"(?i)FROM:\s*<([^>]*)>", arg)
                if not m or m.group(1).lower() != user:
                    self._send("553 MAIL FROM must be the authenticated address")
                    continue
                sender, rcpts = user, []
                self._send("250 sender ok")
            elif verb == "RCPT":
                if sender is None:
                    self._send("503 MAIL first")
                    continue
                m = re.match(r"(?i)TO:\s*<([^>]*)>", arg)
                addr = (m.group(1) if m else "").lower()
                local, _, dom = addr.partition("@")
                if local.endswith("+drafts") and f"{local[:-7]}@{dom}" == sender:
                    rcpts.append(("DRAFTS", sender))
                    self._send("250 draft recipient ok")
                elif not store.is_local(addr):
                    self._send("550 relay denied: this server delivers to local accounts only")
                elif not store.account_exists(addr):
                    self._send("550 no such local mailbox")
                else:
                    rcpts.append(("INBOX", addr))
                    self._send("250 recipient ok")
            elif verb == "DATA":
                if not rcpts:
                    self._send("503 RCPT first")
                    continue
                self._send("354 end data with <CRLF>.<CRLF>")
                chunks, size = [], 0
                while True:
                    raw = self.rfile.readline(MAX_MESSAGE + 3)
                    if not raw:
                        return
                    if raw in (b".\r\n", b".\n"):
                        break
                    if raw.startswith(b".."):
                        raw = raw[1:]
                    size += len(raw)
                    if size <= MAX_MESSAGE:
                        chunks.append(raw)
                if size > MAX_MESSAGE:
                    self._send("552 message too large")
                else:
                    data = _finalize(b"".join(chunks), store.domain)
                    drafts_only = all(kind == "DRAFTS" for kind, _ in rcpts)
                    for kind, addr in rcpts:
                        store.deliver(addr, "Drafts" if kind == "DRAFTS" else "INBOX", data)
                    if not drafts_only:
                        store.deliver(sender, "Sent", data)
                    self._send("250 message accepted for local delivery")
                sender, rcpts = None, []
            elif verb == "RSET":
                sender, rcpts = None, []
                self._send("250 reset")
            elif verb == "NOOP":
                self._send("250 ok")
            elif verb == "QUIT":
                self._send("221 bye")
                return
            else:
                self._send("502 command not implemented")


def _finalize(data: bytes, domain: str) -> bytes:
    """Ensure Date and Message-ID exist. Existing headers and the body are left untouched."""
    head, sep, _ = data.partition(b"\r\n\r\n")
    if not sep:
        head, sep, _ = data.partition(b"\n\n")
    lower = head.lower()
    extra = b""
    if not re.search(rb"(?m)^message-id:", lower):
        extra += f"Message-ID: {make_msgid(domain=domain)}\r\n".encode()
    if not re.search(rb"(?m)^date:", lower):
        extra += f"Date: {formatdate(localtime=False)}\r\n".encode()
    return extra + data


# ---------------------------------------------------------------------------------------------------------- POP3
class _POP3Handler(socketserver.StreamRequestHandler):
    timeout = 60

    def _send(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())

    def handle(self):  # noqa: C901
        store: MailStore = self.server.store
        login, folder, authed, items, deleted = None, "INBOX", False, [], set()
        self._send("+OK RESOLVE local mail POP3 ready")
        while True:
            try:
                raw = self.rfile.readline(8192)
            except (socket.timeout, ConnectionError):
                return
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            verb, _, arg = line.partition(" ")
            verb = verb.upper()
            if verb == "USER":
                name, _, fold = arg.strip().partition("/")
                folder = fold or "INBOX"
                login = name.lower()
                self._send("+OK" if folder in FOLDERS else "-ERR unknown folder")
            elif verb == "PASS":
                if login and folder in FOLDERS and store.check_password(login, arg):
                    authed = True
                    items = store.list(login, folder)
                    self._send(f"+OK {len(items)} messages")
                else:
                    self._send("-ERR authentication failed")
            elif not authed and verb != "QUIT":
                self._send("-ERR authenticate first")
            elif verb == "STAT":
                live = [(i, d) for i, (_, d) in enumerate(items) if i not in deleted]
                self._send(f"+OK {len(live)} {sum(len(d) for _, d in live)}")
            elif verb in ("LIST", "UIDL"):
                if arg.strip():
                    n = int(arg) if arg.strip().isdigit() else 0
                    if 1 <= n <= len(items) and (n - 1) not in deleted:
                        k, d = items[n - 1]
                        self._send(f"+OK {n} {len(d) if verb == 'LIST' else k}")
                    else:
                        self._send("-ERR no such message")
                    continue
                self._send("+OK")
                for i, (k, d) in enumerate(items):
                    if i not in deleted:
                        self._send(f"{i + 1} {len(d) if verb == 'LIST' else k}")
                self._send(".")
            elif verb == "RETR":
                n = int(arg) if arg.strip().isdigit() else 0
                if not (1 <= n <= len(items)) or (n - 1) in deleted:
                    self._send("-ERR no such message")
                    continue
                data = items[n - 1][1]
                self._send(f"+OK {len(data)} octets")
                for ln in data.replace(b"\r\n", b"\n").split(b"\n"):
                    self.wfile.write((b"." + ln if ln.startswith(b".") else ln) + b"\r\n")
                self._send(".")
            elif verb == "DELE":
                n = int(arg) if arg.strip().isdigit() else 0
                if 1 <= n <= len(items) and (n - 1) not in deleted:
                    deleted.add(n - 1)
                    self._send(f"+OK message {n} deleted")
                else:
                    self._send("-ERR no such message")
            elif verb == "RSET":
                deleted.clear()
                self._send("+OK")
            elif verb == "NOOP":
                self._send("+OK")
            elif verb == "QUIT":
                if authed:
                    for i in deleted:
                        store.remove(login, folder, items[i][0])
                self._send("+OK bye")
                return
            else:
                self._send("-ERR command not implemented")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, handler, store):
        self.store = store
        super().__init__(addr, handler)


class LocalMailServer:
    """SMTP + POP3 on 127.0.0.1 over one MailStore. Ports 0 pick free ports (tests)."""

    def __init__(self, root: str, smtp_port: int = 2525, pop3_port: int = 2110, domain: str = DEFAULT_DOMAIN, host: str = "127.0.0.1"):
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("the local mail server binds to loopback only")
        self.store = MailStore(root, domain)
        self.host = host
        self._smtp = _Server((host, smtp_port), _SMTPHandler, self.store)
        self._pop3 = _Server((host, pop3_port), _POP3Handler, self.store)
        self.smtp_port = self._smtp.server_address[1]
        self.pop3_port = self._pop3.server_address[1]
        self._threads: list[threading.Thread] = []

    def start(self) -> "LocalMailServer":
        for srv in (self._smtp, self._pop3):
            t = threading.Thread(target=srv.serve_forever, daemon=True, name=f"localmail-{srv.server_address[1]}")
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        for srv in (self._smtp, self._pop3):
            srv.shutdown()
            srv.server_close()

    def colleague(self, name: str) -> tuple[str, str]:
        """A local-only sender account used to deliver incoming test mail (address, password). Created on first use and
        recorded in colleagues.json; an existing non-colleague account (somebody's own mailbox) is never usable here."""
        address = f"{name.strip().lower()}@{self.store.domain}"
        path = os.path.join(self.store.root, "colleagues.json")
        with self.store._lock:
            try:
                with open(path, encoding="utf-8") as f:
                    known = json.load(f)
            except FileNotFoundError:
                known = {}
            if address in known:
                return address, known[address]
            if self.store.account_exists(address):
                raise ValueError("that address is a user mailbox, not a local colleague")
            password = secrets.token_urlsafe(18)
            self.store.create_account(address, password)
            known[address] = password
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(known, f)
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        return address, password

    def info(self) -> dict:
        return {"host": self.host, "smtp_port": self.smtp_port, "pop3_port": self.pop3_port, "domain": self.store.domain,
                "root": self.store.root, "accounts": len(self.store.accounts())}


# ---------------------------------------------------------------------------------------------------------- provider
def _addrs(*values) -> list[str]:
    """Addresses from header values. Empty values are dropped first: Python 3.13's strict getaddresses() returns
    [('', '')] for the whole list when any element is empty."""
    return [a.lower() for _, a in getaddresses([v for v in values if v and str(v).strip()]) if a]


_RE_PREFIX = re.compile(r"^\s*((re|fwd?|aw)\s*:\s*)+", re.I)


def _norm_subject(s: str) -> str:
    return _RE_PREFIX.sub("", s or "").strip().lower()


def _text_body(m) -> str:
    if m.is_multipart():
        for part in m.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                return (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
        for part in m.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                html = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
                return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
        return ""
    return (m.get_payload(decode=True) or b"").decode(m.get_content_charset() or "utf-8", errors="replace")


def _attachments(m) -> list[dict]:
    out = []
    if m.is_multipart():
        for part in m.walk():
            if part.get_filename():
                payload = part.get_payload(decode=True) or b""
                out.append({"filename": part.get_filename(), "content_type": part.get_content_type(), "size": len(payload),
                            "sha256": hashlib.sha256(payload).hexdigest()})
    return out


class LocalMailProvider:
    """RESOLVE provider adapter for the local mail server (LOCAL mode). Real SMTP/POP3 sockets only."""

    kind = "local_mail"

    def __init__(self, address: str, password: str, host: str = "127.0.0.1", smtp_port: int = 2525, pop3_port: int = 2110,
                 timeout: float = 10.0):
        self.address = address.lower()
        self.password = password
        self.host, self.smtp_port, self.pop3_port, self.timeout = host, smtp_port, pop3_port, timeout
        self.calls: list[str] = []

    # ---- POP3 read path
    def _fetch(self, folder: str = "INBOX") -> list[tuple[str, bytes]]:
        user = self.address if folder == "INBOX" else f"{self.address}/{folder}"
        try:
            pop = poplib.POP3(self.host, self.pop3_port, timeout=self.timeout)
        except (ConnectionRefusedError, OSError) as e:
            if isinstance(e, (socket.timeout, TimeoutError)):
                raise TimeoutError("the local mail server did not respond in time") from None
            raise GmailError("PROVIDER_ERROR", "the local mail server is not reachable") from None
        try:
            try:
                pop.user(user)
                pop.pass_(self.password)
            except poplib.error_proto:
                raise GmailError("REVOKED", "the local mailbox refused these credentials") from None
            out = []
            _, listing, _ = pop.uidl()
            for entry in listing:
                num, uid = entry.decode().split(" ", 1)
                _, lines, _ = pop.retr(int(num))
                out.append((uid, b"\r\n".join(lines) + b"\r\n"))
            return out
        finally:
            try:
                pop.quit()
            except Exception:
                pass

    @staticmethod
    def _mid(m) -> str:
        return (m.get("Message-ID", "") or "").strip().strip("<>")

    def profile(self) -> dict:
        self.calls.append("profile")
        inbox = self._fetch("INBOX")
        return {"email": self.address, "messages_total": len(inbox), "provider": "LOCAL_MAIL"}

    def sync_all(self, query: str = "") -> list[dict]:
        """All INBOX and Sent messages, parsed into the same shape as gmail_adapter.parse_message.
        Threading: the first Message-ID in References (or In-Reply-To), else a previous message with the same normalized
        subject and a shared participant, else the message's own Message-ID."""
        self.calls.append("sync")
        raw = [("INBOX", k, d) for k, d in self._fetch("INBOX")] + [("SENT", k, d) for k, d in self._fetch("Sent")]
        parsed = []
        for label, key, data in raw:
            m = message_from_bytes(data, policy=policy.compat32)
            mid = self._mid(m) or f"local-{hashlib.sha256(data).hexdigest()[:24]}"
            try:
                sent_at = parsedate_to_datetime(m.get("Date")).timestamp()
            except Exception:
                sent_at = float(key.split(".")[0]) if key.split(".")[0].isdigit() else time.time()
            refs = re.findall(r"<([^>]+)>", (m.get("References", "") or "") + " " + (m.get("In-Reply-To", "") or ""))
            parsed.append({"mid": mid, "refs": refs, "sent_at": sent_at, "label": label, "msg": m})
        parsed.sort(key=lambda p: p["sent_at"])
        thread_of: dict[str, str] = {}
        by_subject: dict[str, list[tuple[str, set]]] = {}
        out, seen = [], set()
        for p in parsed:
            m = p["msg"]
            if p["mid"] in seen:            # the same message in INBOX and Sent (sent to self) is one message
                continue
            seen.add(p["mid"])
            participants = set(_addrs(m.get("From"), m.get("To"), m.get("Cc")))
            tid = next((thread_of[r] for r in p["refs"] if r in thread_of), None) or (p["refs"][0] if p["refs"] else None)
            subj = _norm_subject(m.get("Subject", ""))
            if tid is None and subj:
                for cand, parts in reversed(by_subject.get(subj, [])):
                    if parts & participants - {self.address} or (parts & participants and len(participants) <= 2):
                        tid = cand
                        break
            tid = tid or p["mid"]
            thread_of[p["mid"]] = tid
            by_subject.setdefault(subj, []).append((tid, participants))
            to = ", ".join(x for x in [m.get("To", ""), m.get("Cc", "")] if x)
            out.append({"provider_thread_id": "local-" + hashlib.sha256(tid.encode()).hexdigest()[:20],
                        "provider_message_id": "local-" + hashlib.sha256(p["mid"].encode()).hexdigest()[:24],
                        "rfc822_message_id": p["mid"], "sender": m.get("From", ""), "to": to,
                        "subject": m.get("Subject", ""), "sent_at": p["sent_at"], "body": _text_body(m),
                        "label_ids": [p["label"]], "attachments": _attachments(m)})
        return out

    # ---- SMTP write path
    def _submit(self, raw_b64: str, drafts: bool) -> str:
        data = base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4))
        m = message_from_bytes(data, policy=policy.compat32)
        mid = self._mid(m)
        if not mid:
            raise GmailError("PROVIDER_ERROR", "outgoing message has no Message-ID")
        if "From" not in m:
            m["From"] = self.address
        if "Date" not in m:
            m["Date"] = formatdate(localtime=False)
        if drafts:
            local, _, dom = self.address.partition("@")
            rcpts = [f"{local}+drafts@{dom}"]
        else:
            rcpts = _addrs(m.get("To"), m.get("Cc"), m.get("Bcc"))
            if "Bcc" in m:
                del m["Bcc"]
        try:
            smtp = smtplib.SMTP(self.host, self.smtp_port, timeout=self.timeout)
        except (socket.timeout, TimeoutError):
            raise TimeoutError("the local mail server did not answer before submission") from None
        except OSError:
            raise GmailError("PROVIDER_ERROR", "the local mail server is not reachable; nothing was sent") from None
        try:
            smtp.ehlo()
            try:
                smtp.login(self.address, self.password)
            except smtplib.SMTPAuthenticationError:
                raise GmailError("REVOKED", "the local mailbox refused these credentials") from None
            try:
                refused = smtp.sendmail(self.address, rcpts, m.as_bytes())
            except smtplib.SMTPRecipientsRefused:
                raise GmailError("RECIPIENT_REFUSED", "the local mail server refused every recipient (local delivery only)") from None
            except smtplib.SMTPResponseException as e:
                raise GmailError("PROVIDER_ERROR", f"the local mail server rejected the message ({e.smtp_code})") from None
            if refused:
                # accepted for some recipients only: the effect exists, but not as approved -> read-back will decide
                self.calls.append("partial_refusal")
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
        return mid

    def send_raw(self, raw: str) -> dict:
        self.calls.append("send")
        mid = self._submit(raw, drafts=False)
        return {"id": mid, "threadId": mid}

    def create_draft(self, raw: str) -> dict:
        self.calls.append("create_draft")
        mid = self._submit(raw, drafts=True)
        return {"id": mid, "message": {"id": mid}}

    def _find_in(self, folder: str, message_id: str) -> bytes | None:
        for _, data in self._fetch(folder):
            if self._mid(message_from_bytes(data, policy=policy.compat32)) == message_id:
                return data
        return None

    def get_message_raw(self, message_id: str) -> dict:
        """Independent read-back from the Sent folder over POP3."""
        self.calls.append("readback")
        data = self._find_in("Sent", message_id)
        if data is None:
            raise GmailError("NOT_FOUND", "the message is not in the local Sent folder")
        return parse_raw(base64.urlsafe_b64encode(data).decode())

    def get_draft_raw(self, draft_id: str) -> dict:
        self.calls.append("readback_draft")
        data = self._find_in("Drafts", draft_id)
        if data is None:
            raise GmailError("NOT_FOUND", "the draft is not in the local Drafts folder")
        return parse_raw(base64.urlsafe_b64encode(data).decode())

    def _find_corr(self, folder: str, operation_key: str) -> list[str]:
        want = correlation_id(operation_key)
        return [want] if self._find_in(folder, want) is not None else []

    def find_by_correlation(self, operation_key: str) -> list[str]:
        self.calls.append("find")
        return self._find_corr("Sent", operation_key)

    def find_draft_by_correlation(self, operation_key: str) -> list[str]:
        self.calls.append("find_draft")
        return self._find_corr("Drafts", operation_key)


def send_as(address: str, password: str, to: str, subject: str, body: str, host="127.0.0.1", smtp_port=2525,
            in_reply_to: str | None = None) -> str:
    """Submit a plain message as a local account (used to deliver incoming mail from local colleagues)."""
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = address, to, subject
    msg["Date"] = formatdate(localtime=False)
    mid = make_msgid(domain=address.split("@")[1])
    msg["Message-ID"] = mid
    if in_reply_to:
        msg["In-Reply-To"] = f"<{in_reply_to.strip('<>')}>"
        msg["References"] = f"<{in_reply_to.strip('<>')}>"
    msg.set_content(body)
    with smtplib.SMTP(host, smtp_port, timeout=10) as smtp:
        smtp.ehlo()
        smtp.login(address, password)
        smtp.send_message(msg)
    return mid.strip("<>")


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="RESOLVE local mail server (LOCAL mode; not Gmail)")
    ap.add_argument("--root", default=os.environ.get("RESOLVE_LOCAL_MAIL_ROOT", "data/localmail"))
    ap.add_argument("--smtp-port", type=int, default=int(os.environ.get("RESOLVE_LOCAL_SMTP_PORT", "2525")))
    ap.add_argument("--pop3-port", type=int, default=int(os.environ.get("RESOLVE_LOCAL_POP3_PORT", "2110")))
    ap.add_argument("--domain", default=DEFAULT_DOMAIN)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    a = sub.add_parser("add-account")
    a.add_argument("address")
    a.add_argument("password")
    s = sub.add_parser("send")
    s.add_argument("--from", dest="sender", required=True)
    s.add_argument("--password", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--subject", required=True)
    s.add_argument("--body", required=True)
    sub.add_parser("accounts")
    args = ap.parse_args(argv)
    if args.cmd == "serve":
        srv = LocalMailServer(args.root, args.smtp_port, args.pop3_port, args.domain).start()
        print(json.dumps({"serving": srv.info()}), flush=True)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            srv.stop()
        return 0
    store = MailStore(args.root, args.domain)
    if args.cmd == "add-account":
        print(json.dumps(store.create_account(args.address, args.password)))
    elif args.cmd == "accounts":
        print(json.dumps({a: store.counts(a) for a in store.accounts()}, indent=1))
    elif args.cmd == "send":
        print(json.dumps({"message_id": send_as(args.sender, args.password, args.to, args.subject, args.body, smtp_port=args.smtp_port)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
