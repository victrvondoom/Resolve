"""Inbox-to-commitment data foundation: tenant-scoped records for email connections, threads, messages, commitments,
drafts, approvals, executions, verifications, voice rules, corrections and audit events.

Isolation: every public method takes (user_id, workspace_id) and every query is filtered by both. Identifiers from the
client are never trusted as ownership proof; a record outside the caller's scope is reported as not found.
Encryption at rest: message bodies, commitment evidence text and draft bodies are sealed per user/workspace with
AES-256-GCM (src/at_rest.py). Other columns (ids, timestamps, statuses, provider ids) are plaintext and are documented as such.
Audit events never contain message bodies, tokens or evidence text.
"""
from __future__ import annotations

import hashlib
import re

import inbox_intel as ii
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from at_rest import SealedStore

SCHEMA_VERSION = 6
COMMITMENT_KINDS = ("REQUEST_TO_USER", "PROMISE_BY_USER", "PROMISE_BY_OTHER", "DEPENDENCY", "FOLLOW_UP", "UNRESOLVED_OBLIGATION", "SUGGESTION", "HISTORICAL")
FOLLOW_UP_TRIGGERS = ("OUTSTANDING_OWN_COMMITMENT", "OTHER_PARTY_PROMISE", "UNANSWERED_THREAD", "DUE_DATE_APPROACHING")
EMAIL_RE = re.compile(r"^[^@\s<>]{1,64}@[^@\s<>]{1,255}\.[A-Za-z]{2,24}$")
COMMITMENT_STATUSES = ("OPEN", "IN_PROGRESS", "WAITING", "UNCERTAIN", "NEEDS_REVIEW", "RESOLVED_VERIFIED", "RESOLVED_USER_CONFIRMED", "DISMISSED", "CANCELLED")
# Explicit status machine. Terminal states never move; RESOLVED_VERIFIED needs a verified execution record.
COMMITMENT_TRANSITIONS = {
    "OPEN": {"IN_PROGRESS", "WAITING", "NEEDS_REVIEW", "UNCERTAIN", "RESOLVED_USER_CONFIRMED", "RESOLVED_VERIFIED", "DISMISSED", "CANCELLED"},
    "IN_PROGRESS": {"WAITING", "NEEDS_REVIEW", "UNCERTAIN", "RESOLVED_USER_CONFIRMED", "RESOLVED_VERIFIED", "DISMISSED", "CANCELLED"},
    "WAITING": {"IN_PROGRESS", "NEEDS_REVIEW", "UNCERTAIN", "RESOLVED_USER_CONFIRMED", "RESOLVED_VERIFIED", "DISMISSED", "CANCELLED"},
    "UNCERTAIN": {"OPEN", "IN_PROGRESS", "WAITING", "NEEDS_REVIEW", "RESOLVED_USER_CONFIRMED", "RESOLVED_VERIFIED", "DISMISSED", "CANCELLED"},
    "NEEDS_REVIEW": {"OPEN", "IN_PROGRESS", "WAITING", "DISMISSED", "CANCELLED"},
    "RESOLVED_VERIFIED": set(), "RESOLVED_USER_CONFIRMED": set(), "DISMISSED": set(), "CANCELLED": set(),
}
DRAFT_TRANSITIONS = {"DRAFT": {"APPROVED", "REJECTED"}, "APPROVED": {"DRAFT", "SENT_VERIFIED", "SAVED_TO_GMAIL", "REJECTED"},
                     "SAVED_TO_GMAIL": {"DRAFT", "APPROVED", "REJECTED"}, "REJECTED": set(), "SENT_VERIFIED": set()}
DRAFT_STATUSES = ("DRAFT", "APPROVED", "REJECTED", "DEFERRED", "SAVED_TO_GMAIL", "SENT_VERIFIED", "UNCERTAIN")
EXECUTION_STATES = ("REQUESTED", "EXECUTING", "VERIFIED", "UNCERTAIN", "FAILED", "REJECTED", "NEEDS_RECOVERY")


def content_digest(thread_id: str, recipient: str, cc: str, subject: str, body: str) -> str:
    """Identity of the exact content that would be sent. Excludes draft ids and versions so identical content maps to one
    operation key. Any change to any field changes the digest."""
    payload = json.dumps([thread_id, recipient, cc or "", subject, body], ensure_ascii=False, sort_keys=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]


class InboxError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message, self.status_code = message, status_code


class InboxStore:
    def __init__(self, db_path: str | Path, sealer: SealedStore):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.sealer = sealer
        self._lock = threading.RLock()
        self._migrate()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    def _migrate(self) -> None:
        # executescript commits on its own, so migrations run on a plain connection, not inside _db()
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)")
            done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
            if 1 not in done:
                conn.executescript(MIGRATION_1)
                conn.execute("INSERT INTO schema_migrations VALUES (1, ?)", (time.time(),))
            if 2 not in done:
                conn.executescript(MIGRATION_2)
                conn.execute("INSERT INTO schema_migrations VALUES (2, ?)", (time.time(),))
            if 3 not in done:
                conn.executescript(MIGRATION_3)
                conn.execute("INSERT INTO schema_migrations VALUES (3, ?)", (time.time(),))
            if 4 not in done:
                conn.executescript(MIGRATION_4)
                conn.execute("INSERT INTO schema_migrations VALUES (4, ?)", (time.time(),))
            if 5 not in done:
                conn.executescript(MIGRATION_5)
                conn.execute("INSERT INTO schema_migrations VALUES (5, ?)", (time.time(),))
            if 6 not in done:
                conn.executescript(MIGRATION_6)
                conn.execute("INSERT INTO schema_migrations VALUES (6, ?)", (time.time(),))
        finally:
            conn.close()

    # ---------- helpers
    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    def _seal(self, text: str | None, user_id: str, workspace_id: str, record_id: str) -> bytes | None:
        if text is None:
            return None
        return self.sealer.seal(text.encode("utf-8"), user_id, workspace_id, record_id)

    def _open(self, blob, user_id: str, workspace_id: str, record_id: str) -> str | None:
        if blob is None:
            return None
        return self.sealer.open(bytes(blob), user_id, workspace_id, record_id).decode("utf-8")

    def _audit(self, db, user_id: str, workspace_id: str, action: str, subject_id: str | None, detail: dict | None = None) -> None:
        # detail is restricted to ids, counts and statuses; never message text, tokens or evidence
        safe = {k: v for k, v in (detail or {}).items() if not isinstance(v, str) or len(v) <= 80}
        db.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?)",
                   (self._id("aud"), user_id, workspace_id, action, subject_id, json.dumps(safe, sort_keys=True), time.time()))

    # ---------- connections
    def upsert_connection(self, user_id: str, workspace_id: str, provider: str, status: str, scopes: list[str], detail: str = "") -> dict:
        if not user_id or not workspace_id:
            raise InboxError("authenticated user and workspace are required", 401)
        with self._lock, self._db() as db:
            row = db.execute("SELECT id FROM email_connections WHERE user_id=? AND workspace_id=? AND provider=?", (user_id, workspace_id, provider)).fetchone()
            cid = row["id"] if row else self._id("conn")
            db.execute("""INSERT INTO email_connections(id,user_id,workspace_id,provider,status,scopes,detail,updated_at)
                          VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status, scopes=excluded.scopes,
                          detail=excluded.detail, updated_at=excluded.updated_at""",
                       (cid, user_id, workspace_id, provider, status, json.dumps(sorted(scopes)), detail[:200], time.time()))
            self._audit(db, user_id, workspace_id, "connection.status", cid, {"provider": provider, "status": status})
        return self.get_connection(user_id, workspace_id, provider)

    def get_connection(self, user_id: str, workspace_id: str, provider: str) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT * FROM email_connections WHERE user_id=? AND workspace_id=? AND provider=?", (user_id, workspace_id, provider)).fetchone()
        if r is None:
            return None
        return {"id": r["id"], "provider": r["provider"], "status": r["status"], "scopes": json.loads(r["scopes"]),
                "detail": r["detail"], "updated_at": r["updated_at"]}

    # ---------- threads and messages (provider ids preserved; re-sync is idempotent)
    def upsert_message(self, user_id: str, workspace_id: str, connection_id: str, provider_thread_id: str, provider_message_id: str,
                       sender: str, sent_at: float, subject: str, body: str, content_hash: str | None = None, recipients: str = "") -> dict:
        self._require_connection(user_id, workspace_id, connection_id)
        body_hash = content_hash or hashlib.sha256(body.encode()).hexdigest()
        with self._lock, self._db() as db:
            thread = db.execute("SELECT id FROM email_threads WHERE user_id=? AND workspace_id=? AND connection_id=? AND provider_thread_id=?",
                                (user_id, workspace_id, connection_id, provider_thread_id)).fetchone()
            tid = thread["id"] if thread else self._id("thr")
            db.execute("INSERT OR IGNORE INTO email_threads(id,user_id,workspace_id,connection_id,provider_thread_id,updated_at) VALUES (?,?,?,?,?,?)",
                       (tid, user_id, workspace_id, connection_id, provider_thread_id, time.time()))
            existing = db.execute("SELECT id, content_hash FROM email_messages WHERE user_id=? AND workspace_id=? AND connection_id=? AND provider_message_id=?",
                                  (user_id, workspace_id, connection_id, provider_message_id)).fetchone()
            if existing is not None:
                if existing["content_hash"] == body_hash:
                    return {"id": existing["id"], "thread_id": tid, "changed": False}
                db.execute("UPDATE email_messages SET body_sealed=?, content_hash=?, subject_sealed=?, recipients=? WHERE id=?",
                           (self._seal(body, user_id, workspace_id, existing["id"]), body_hash, self._seal(subject, user_id, workspace_id, existing["id"]),
                            recipients[:500], existing["id"]))
                self._audit(db, user_id, workspace_id, "message.changed", existing["id"], {})
                return {"id": existing["id"], "thread_id": tid, "changed": True}
            mid = self._id("msg")
            db.execute("""INSERT INTO email_messages(id,user_id,workspace_id,connection_id,thread_id,provider_message_id,sender,sent_at,
                          subject_sealed,body_sealed,content_hash,imported_at,recipients) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (mid, user_id, workspace_id, connection_id, tid, provider_message_id, sender[:200], sent_at,
                        self._seal(subject, user_id, workspace_id, mid), self._seal(body, user_id, workspace_id, mid), body_hash, time.time(),
                        recipients[:500]))
            self._audit(db, user_id, workspace_id, "message.imported", mid, {})
            return {"id": mid, "thread_id": tid, "changed": True}

    def _require_connection(self, user_id: str, workspace_id: str, connection_id: str) -> None:
        with self._db() as db:
            if db.execute("SELECT 1 FROM email_connections WHERE id=? AND user_id=? AND workspace_id=?", (connection_id, user_id, workspace_id)).fetchone() is None:
                raise InboxError("connection not found", 404)

    def read_message(self, user_id: str, workspace_id: str, message_id: str) -> dict:
        with self._db() as db:
            r = db.execute("SELECT * FROM email_messages WHERE id=? AND user_id=? AND workspace_id=?", (message_id, user_id, workspace_id)).fetchone()
        if r is None:
            raise InboxError("message not found", 404)
        return {"id": r["id"], "thread_id": r["thread_id"], "provider_message_id": r["provider_message_id"], "sender": r["sender"],
                "recipients": r["recipients"] or "", "sent_at": r["sent_at"], "subject": self._open(r["subject_sealed"], user_id, workspace_id, r["id"]),
                "body": self._open(r["body_sealed"], user_id, workspace_id, r["id"])}

    def list_messages(self, user_id: str, workspace_id: str, thread_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT id FROM email_messages WHERE thread_id=? AND user_id=? AND workspace_id=? ORDER BY sent_at", (thread_id, user_id, workspace_id)).fetchall()
        return [self.read_message(user_id, workspace_id, r["id"]) for r in rows]

    # ---------- commitments (deduplicated per source message and normalized text)
    def add_commitment(self, user_id: str, workspace_id: str, source_message_id: str, kind: str, required_action: str,
                       evidence_text: str, requesting_person: str | None, responsible_person: str | None,
                       deadline_at: float | None, deadline_confidence: str | None, *, obligation_key: str | None = None,
                       ownership_state: str = "UNKNOWN", thread_id: str | None = None, deadline_text: str | None = None,
                       deadline_timezone: str | None = None, deadline_explicit: bool = False, review_reason: str | None = None) -> dict:
        if kind not in COMMITMENT_KINDS:
            raise InboxError("unknown commitment kind", 400)
        src = self.read_message(user_id, workspace_id, source_message_id)      # ownership: 404 if not this tenant's message
        raw_key = obligation_key if obligation_key is not None else " ".join(required_action.lower().split())
        key_text = self.sealer.blind_index(user_id, workspace_id, "obligation|" + raw_key)      # never store the words themselves
        dedupe = hashlib.sha256(f"{source_message_id}|{kind}|{key_text}".encode()).hexdigest()
        with self._lock, self._db() as db:
            hit = db.execute("SELECT id FROM commitments WHERE user_id=? AND workspace_id=? AND dedupe_key=?", (user_id, workspace_id, dedupe)).fetchone()
            if hit:
                return {"id": hit["id"], "created": False}
            cid = self._id("cmt")
            now = time.time()
            db.execute("""INSERT INTO commitments(id,user_id,workspace_id,source_message_id,kind,status,dedupe_key,requesting_person,
                          responsible_person,deadline_at,deadline_confidence,required_action_sealed,evidence_sealed,created_at,updated_at,
                          obligation_key,ownership_state,thread_id,deadline_text,deadline_timezone,deadline_explicit,review_reason)
                          VALUES (?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (cid, user_id, workspace_id, source_message_id, kind, dedupe, requesting_person, responsible_person, deadline_at,
                        deadline_confidence, self._seal(required_action, user_id, workspace_id, cid), self._seal(evidence_text, user_id, workspace_id, cid),
                        now, now, key_text, ownership_state, thread_id or src["thread_id"], deadline_text, deadline_timezone,
                        1 if deadline_explicit else 0, review_reason))
            self._audit(db, user_id, workspace_id, "commitment.created", cid, {"kind": kind, "ownership": ownership_state})
            return {"id": cid, "created": True}

    def reconcile_obligation(self, user_id: str, workspace_id: str, thread_id: str, kind: str, obligation_key_raw: str, new: dict) -> dict:
        """One obligation per thread: a newer message with an explicit, different deadline REVISES the active deadline and
        keeps the previous one in commitment_revisions. An older message never reactivates an old deadline. Repeats are no-ops.
        Returns {"id", "action": CREATED | UNCHANGED | DEADLINE_REVISED | SUPERSEDED_HISTORY}."""
        index = self.sealer.blind_index(user_id, workspace_id, "obligation|" + obligation_key_raw)
        open_states = "('OPEN','IN_PROGRESS','WAITING','UNCERTAIN','NEEDS_REVIEW')"
        with self._db() as db:
            cur = db.execute(f"""SELECT c.*, m.sent_at AS source_sent_at FROM commitments c JOIN email_messages m ON m.id=c.source_message_id
                                WHERE c.user_id=? AND c.workspace_id=? AND c.thread_id=? AND c.kind=? AND c.obligation_key=?
                                AND c.status IN {open_states} ORDER BY m.sent_at DESC LIMIT 1""",
                             (user_id, workspace_id, thread_id, kind, index)).fetchone()
            if cur is None:
                # the same obligation restated or re-dated in a later message: match by content, inside this thread only
                rows = db.execute(f"""SELECT c.*, m.sent_at AS source_sent_at FROM commitments c JOIN email_messages m ON m.id=c.source_message_id
                                     WHERE c.user_id=? AND c.workspace_id=? AND c.thread_id=? AND c.kind=? AND c.status IN {open_states}""",
                                  (user_id, workspace_id, thread_id, kind)).fetchall()
                for r in rows:
                    if ii.same_obligation(self._open(r["required_action_sealed"], user_id, workspace_id, r["id"]) or "", new["required_action"]):
                        cur = r
                        break
        if cur is None:
            res = self.add_commitment(user_id, workspace_id, new["source_message_id"], kind, new["required_action"], new["evidence_text"],
                                      new.get("requesting_person"), new.get("responsible_person"), new.get("deadline_at"), new.get("deadline_confidence"),
                                      obligation_key=obligation_key_raw, ownership_state=new.get("ownership_state", "UNKNOWN"), thread_id=thread_id,
                                      deadline_text=new.get("deadline_phrase"), deadline_timezone=new.get("deadline_timezone"),
                                      deadline_explicit=bool(new.get("deadline_explicit")), review_reason=new.get("review_reason"))
            return {"id": res["id"], "action": "CREATED" if res["created"] else "UNCHANGED"}
        if new["sent_at"] <= cur["source_sent_at"]:
            return {"id": cur["id"], "action": "SUPERSEDED_HISTORY" if new["source_message_id"] != cur["source_message_id"] else "UNCHANGED"}
        if new.get("deadline_at") is None or new.get("deadline_at") == cur["deadline_at"]:
            return {"id": cur["id"], "action": "UNCHANGED"}
        with self._lock, self._db() as db:
            prev_src = cur["source_message_id"]
            rid = self._id("rev")
            db.execute("""INSERT INTO commitment_revisions(id,commitment_id,user_id,workspace_id,deadline_at,deadline_text,deadline_confidence,
                          deadline_timezone,source_message_id,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                       (rid, cur["id"], user_id, workspace_id, cur["deadline_at"], cur["deadline_text"], cur["deadline_confidence"],
                        cur["deadline_timezone"], prev_src, "superseded by a newer message with a different explicit deadline", time.time()))
            db.execute("""UPDATE commitments SET deadline_at=?, deadline_confidence=?, deadline_text=?, deadline_timezone=?, deadline_explicit=?,
                          source_message_id=?, evidence_sealed=?, required_action_sealed=?, updated_at=? WHERE id=? AND user_id=? AND workspace_id=?""",
                       (new["deadline_at"], new.get("deadline_confidence"), new.get("deadline_phrase"), new.get("deadline_timezone"),
                        1 if new.get("deadline_explicit") else 0, new["source_message_id"], self._seal(new["evidence_text"], user_id, workspace_id, cur["id"]),
                        self._seal(new["required_action"], user_id, workspace_id, cur["id"]), time.time(), cur["id"], user_id, workspace_id))
            self._audit(db, user_id, workspace_id, "commitment.deadline_revised", cur["id"], {"revision": rid})
        return {"id": cur["id"], "action": "DEADLINE_REVISED"}

    def commitment_revisions(self, user_id: str, workspace_id: str, commitment_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM commitment_revisions WHERE commitment_id=? AND user_id=? AND workspace_id=? ORDER BY created_at",
                              (commitment_id, user_id, workspace_id)).fetchall()
        return [dict(r) for r in rows]

    def list_commitments(self, user_id: str, workspace_id: str, status: str | None = None, kind: str | None = None) -> list[dict]:
        q = "SELECT * FROM commitments WHERE user_id=? AND workspace_id=?"
        args: list = [user_id, workspace_id]
        if status:
            q += " AND status=?"; args.append(status)
        if kind:
            q += " AND kind=?"; args.append(kind)
        with self._db() as db:
            rows = db.execute(q + " ORDER BY created_at DESC", args).fetchall()
        out = []
        for r in rows:
            out.append({"id": r["id"], "source_message_id": r["source_message_id"], "thread_id": r["thread_id"], "kind": r["kind"],
                        "status": r["status"], "requesting_person": r["requesting_person"], "responsible_person": r["responsible_person"],
                        "ownership_state": r["ownership_state"], "deadline_at": r["deadline_at"], "deadline_confidence": r["deadline_confidence"],
                        "deadline_text": r["deadline_text"], "deadline_timezone": r["deadline_timezone"], "deadline_explicit": bool(r["deadline_explicit"]),
                        "review_reason": r["review_reason"], "obligation_key": r["obligation_key"],
                        "required_action": self._open(r["required_action_sealed"], user_id, workspace_id, r["id"]),
                        "evidence": self._open(r["evidence_sealed"], user_id, workspace_id, r["id"]),
                        "resolution_evidence_id": r["resolution_evidence_id"],
                        "created_at": r["created_at"], "updated_at": r["updated_at"]})
        return out

    def get_commitment(self, user_id: str, workspace_id: str, commitment_id: str) -> dict:
        for c in self.list_commitments(user_id, workspace_id):
            if c["id"] == commitment_id:
                return c
        raise InboxError("commitment not found", 404)

    def set_commitment_status(self, user_id: str, workspace_id: str, commitment_id: str, status: str, evidence_id: str | None = None,
                              reason: str = "") -> dict:
        """Validated transitions only. RESOLVED_VERIFIED needs a verified execution in this workspace; manual completion is
        RESOLVED_USER_CONFIRMED and is never presented as verified."""
        if status not in COMMITMENT_STATUSES:
            raise InboxError("unknown status", 400)
        with self._lock, self._db() as db:
            row = db.execute("SELECT status FROM commitments WHERE id=? AND user_id=? AND workspace_id=?", (commitment_id, user_id, workspace_id)).fetchone()
            if row is None:
                raise InboxError("commitment not found", 404)
            current = row["status"]
            if status not in COMMITMENT_TRANSITIONS.get(current, set()):
                raise InboxError(f"a commitment cannot move from {current} to {status}", 409)
            if status == "RESOLVED_VERIFIED":
                ok = db.execute("SELECT 1 FROM execution_records WHERE id=? AND user_id=? AND workspace_id=? AND state='VERIFIED'",
                                (evidence_id or "", user_id, workspace_id)).fetchone()
                if ok is None:
                    raise InboxError("RESOLVED_VERIFIED needs a verified execution record as evidence", 409)
            if status in ("DISMISSED", "CANCELLED") and not reason.strip():
                raise InboxError("dismissing or cancelling needs a reason", 400)
            db.execute("UPDATE commitments SET status=?, updated_at=?, resolution_evidence_id=COALESCE(?, resolution_evidence_id) WHERE id=?",
                       (status, time.time(), evidence_id, commitment_id))
            db.execute("INSERT INTO commitment_status_history(id,commitment_id,user_id,workspace_id,from_status,to_status,reason,evidence_id,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (self._id("sth"), commitment_id, user_id, workspace_id, current, status, reason[:200], evidence_id, time.time()))
            self._audit(db, user_id, workspace_id, "commitment.status", commitment_id, {"from": current, "to": status})
        return {"id": commitment_id, "status": status, "previous": current}

    def commitment_status_history(self, user_id: str, workspace_id: str, commitment_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT from_status, to_status, reason, evidence_id, created_at FROM commitment_status_history "
                              "WHERE commitment_id=? AND user_id=? AND workspace_id=? ORDER BY created_at", (commitment_id, user_id, workspace_id)).fetchall()
        return [dict(r) for r in rows]

    # ---------- voice rules (versioned, reversible)
    def add_voice_rule(self, user_id: str, workspace_id: str, instruction: str, parsed: dict, scope: str, confirmed: bool) -> dict:
        """A new version of a rule. An UNKNOWN rule is stored for review but is never active, even when confirmed."""
        unknown = parsed.get("type") == "UNKNOWN"
        if unknown and confirmed:
            raise InboxError("an instruction RESOLVE does not recognise cannot be activated; rephrase it or edit the parsed rule", 409)
        active = bool(confirmed) and not unknown
        with self._lock, self._db() as db:
            rid = self._id("rule")
            prev = db.execute("SELECT COALESCE(MAX(version),0) FROM voice_rules WHERE user_id=? AND workspace_id=?", (user_id, workspace_id)).fetchone()[0]
            db.execute("""INSERT INTO voice_rules(id,user_id,workspace_id,version,instruction_sealed,parsed_json,scope,active,confirmed,created_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?)""",
                       (rid, user_id, workspace_id, prev + 1, self._seal(instruction, user_id, workspace_id, rid), json.dumps(parsed, sort_keys=True),
                        scope, 1 if active else 0, 1 if confirmed and not unknown else 0, time.time()))
            self._audit(db, user_id, workspace_id, "voice_rule.added", rid, {"scope": scope, "version": prev + 1, "type": parsed.get("type")})
            return {"id": rid, "version": prev + 1, "active": active}

    def list_voice_rules(self, user_id: str, workspace_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM voice_rules WHERE user_id=? AND workspace_id=? ORDER BY version DESC", (user_id, workspace_id)).fetchall()
        return [{"id": r["id"], "version": r["version"], "instruction": self._open(r["instruction_sealed"], user_id, workspace_id, r["id"]),
                 "parsed": json.loads(r["parsed_json"]), "scope": r["scope"], "active": bool(r["active"]), "confirmed": bool(r["confirmed"]),
                 "created_at": r["created_at"]} for r in rows]

    def set_voice_rule_active(self, user_id: str, workspace_id: str, rule_id: str, active: bool) -> dict:
        with self._lock, self._db() as db:
            row = db.execute("SELECT parsed_json FROM voice_rules WHERE id=? AND user_id=? AND workspace_id=?", (rule_id, user_id, workspace_id)).fetchone()
            if row is None:
                raise InboxError("rule not found", 404)
            if active and json.loads(row["parsed_json"]).get("type") == "UNKNOWN":
                raise InboxError("an instruction RESOLVE does not recognise cannot be activated", 409)
            db.execute("UPDATE voice_rules SET active=?, confirmed=CASE WHEN ?=1 THEN 1 ELSE confirmed END WHERE id=? AND user_id=? AND workspace_id=?",
                       (1 if active else 0, 1 if active else 0, rule_id, user_id, workspace_id))
            self._audit(db, user_id, workspace_id, "voice_rule.active", rule_id, {"active": bool(active)})
        return {"id": rule_id, "active": active}

    def get_voice_rule(self, user_id: str, workspace_id: str, rule_id: str) -> dict:
        for r in self.list_voice_rules(user_id, workspace_id):
            if r["id"] == rule_id:
                return r
        raise InboxError("rule not found", 404)

    # ---------- audit (ids, counts, statuses only)
    def audit_log(self, user_id: str, workspace_id: str, limit: int = 200) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM audit_events WHERE user_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT ?",
                              (user_id, workspace_id, int(limit))).fetchall()
        return [{"id": r["id"], "action": r["action"], "subject_id": r["subject_id"], "detail": json.loads(r["detail"]), "created_at": r["created_at"]} for r in rows]

    # ---------- triage decisions (one per thread; recomputed on sync, never duplicated)
    def save_triage(self, user_id: str, workspace_id: str, thread_id: str, decision: dict) -> None:
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM email_threads WHERE id=? AND user_id=? AND workspace_id=?", (thread_id, user_id, workspace_id)).fetchone() is None:
                raise InboxError("thread not found", 404)
            db.execute("""INSERT INTO triage_decisions(thread_id,user_id,workspace_id,category,priority,confidence,reasons_json,evidence_sealed,computed_at)
                          VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(thread_id) DO UPDATE SET category=excluded.category, priority=excluded.priority,
                          confidence=excluded.confidence, reasons_json=excluded.reasons_json, evidence_sealed=excluded.evidence_sealed,
                          computed_at=excluded.computed_at""",
                       (thread_id, user_id, workspace_id, decision["category"], decision["priority"], decision["confidence"],
                        json.dumps(decision["reasons"]), self._seal("\n".join(decision["evidence"]), user_id, workspace_id, thread_id), time.time()))

    def list_triage(self, user_id: str, workspace_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT t.*, th.provider_thread_id FROM triage_decisions t JOIN email_threads th ON th.id=t.thread_id "
                              "WHERE t.user_id=? AND t.workspace_id=? ORDER BY t.computed_at DESC", (user_id, workspace_id)).fetchall()
        return [{"thread_id": r["thread_id"], "category": r["category"], "priority": r["priority"], "confidence": r["confidence"],
                 "reasons": json.loads(r["reasons_json"]),
                 "evidence": (self._open(r["evidence_sealed"], user_id, workspace_id, r["thread_id"]) or "").split("\n") if r["evidence_sealed"] else [],
                 "computed_at": r["computed_at"]} for r in rows]

    def list_threads(self, user_id: str, workspace_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT id, provider_thread_id, connection_id FROM email_threads WHERE user_id=? AND workspace_id=?", (user_id, workspace_id)).fetchall()
        return [dict(r) for r in rows]

    # ---------- corrections: one-time or persistent, always with before and after, always reversible
    def add_correction(self, user_id: str, workspace_id: str, thread_id: str | None, original: dict, corrected: dict,
                       explanation: str, scope: str, confirmed: bool) -> dict:
        if scope not in ("ONE_TIME", "PERSISTENT_THREAD", "PERSISTENT_WORKSPACE"):
            raise InboxError("unknown correction scope", 400)
        if scope == "PERSISTENT_WORKSPACE" and not confirmed:
            raise InboxError("a workspace-wide preference needs your confirmation before it is stored", 409)
        with self._lock, self._db() as db:
            cid = self._id("cor")
            db.execute("""INSERT INTO corrections(id,user_id,workspace_id,thread_id,original_json,corrected_json,explanation_sealed,scope,active,created_at)
                          VALUES (?,?,?,?,?,?,?,?,1,?)""",
                       (cid, user_id, workspace_id, thread_id, json.dumps(original, sort_keys=True), json.dumps(corrected, sort_keys=True),
                        self._seal(explanation, user_id, workspace_id, cid), scope, time.time()))
            self._audit(db, user_id, workspace_id, "correction.added", cid, {"scope": scope})
        return {"id": cid, "scope": scope, "active": True}

    def list_corrections(self, user_id: str, workspace_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM corrections WHERE user_id=? AND workspace_id=? ORDER BY created_at DESC", (user_id, workspace_id)).fetchall()
        return [{"id": r["id"], "thread_id": r["thread_id"], "original": json.loads(r["original_json"]), "corrected": json.loads(r["corrected_json"]),
                 "explanation": self._open(r["explanation_sealed"], user_id, workspace_id, r["id"]), "scope": r["scope"], "active": bool(r["active"]),
                 "created_at": r["created_at"]} for r in rows]

    def reverse_correction(self, user_id: str, workspace_id: str, correction_id: str) -> dict:
        with self._lock, self._db() as db:
            r = db.execute("UPDATE corrections SET active=0, reversed_at=? WHERE id=? AND user_id=? AND workspace_id=? AND active=1",
                           (time.time(), correction_id, user_id, workspace_id))
            if r.rowcount != 1:
                raise InboxError("correction not found or already reversed", 404)
            self._audit(db, user_id, workspace_id, "correction.reversed", correction_id, {})
        return {"id": correction_id, "active": False}

    # ---------- drafts, approvals and execution records (send path)
    def create_draft(self, user_id: str, workspace_id: str, thread_id: str, recipient: str, subject: str, body: str,
                     cc: str = "", origin: str = "USER") -> dict:
        """A new draft at version 1. The digest is computed here from the content, never supplied by a caller."""
        recipient, cc, subject = (recipient or "").strip(), (cc or "").strip(), (subject or "").strip()
        if not EMAIL_RE.match(recipient):
            raise InboxError("recipient must be a single email address", 400)
        if cc and any(not EMAIL_RE.match(a.strip()) for a in cc.split(",") if a.strip()):
            raise InboxError("every cc address must be an email address", 400)
        if len(subject) > 300 or len(body) > 20000:
            raise InboxError("draft is too long (subject 300 characters, body 20000)", 413)
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM email_threads WHERE id=? AND user_id=? AND workspace_id=?", (thread_id, user_id, workspace_id)).fetchone() is None:
                raise InboxError("thread not found", 404)
            did = self._id("drf")
            digest = content_digest(thread_id, recipient, cc, subject, body)
            now = time.time()
            db.execute("""INSERT INTO reply_drafts(id,user_id,workspace_id,thread_id,recipient,digest,subject_sealed,body_sealed,status,created_at,updated_at,cc,version)
                          VALUES (?,?,?,?,?,?,?,?,'DRAFT',?,?,?,1)""",
                       (did, user_id, workspace_id, thread_id, recipient[:200], digest, self._seal(subject, user_id, workspace_id, did),
                        self._seal(body, user_id, workspace_id, did), now, now, cc[:500]))
            self._version(db, user_id, workspace_id, did, 1, digest, recipient, cc, subject, body, origin)
            self._audit(db, user_id, workspace_id, "draft.created", did, {"origin": origin})
        return {"id": did, "digest": digest, "version": 1}

    def _version(self, db, user_id, workspace_id, draft_id, version, digest, recipient, cc, subject, body, origin):
        vid = self._id("dv")
        db.execute("""INSERT INTO draft_versions(id,draft_id,user_id,workspace_id,version,digest,recipient,cc,subject_sealed,body_sealed,origin,created_at)
                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (vid, draft_id, user_id, workspace_id, version, digest, recipient, cc, self._seal(subject, user_id, workspace_id, vid),
                    self._seal(body, user_id, workspace_id, vid), origin, time.time()))

    def update_draft(self, user_id: str, workspace_id: str, draft_id: str, *, recipient=None, cc=None, subject=None, body=None,
                     origin: str = "USER") -> dict:
        """Any change to recipient, cc, subject or body creates a new version, a new digest and returns the draft to DRAFT.
        Approvals bound to the old digest stop matching immediately. No change is a no-op."""
        cur = self.get_draft(user_id, workspace_id, draft_id)      # read before the write transaction: nested connections deadlock under BEGIN IMMEDIATE
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM reply_drafts WHERE id=? AND user_id=? AND workspace_id=?", (draft_id, user_id, workspace_id)).fetchone() is None:
                raise InboxError("draft not found", 404)
            new = {"recipient": (recipient if recipient is not None else cur["recipient"]).strip(),
                   "cc": (cc if cc is not None else cur["cc"]).strip(),
                   "subject": (subject if subject is not None else cur["subject"]).strip(),
                   "body": body if body is not None else cur["body"]}
            if not EMAIL_RE.match(new["recipient"]):
                raise InboxError("recipient must be a single email address", 400)
            if new["cc"] and any(not EMAIL_RE.match(a.strip()) for a in new["cc"].split(",") if a.strip()):
                raise InboxError("every cc address must be an email address", 400)
            if len(new["subject"]) > 300 or len(new["body"]) > 20000:
                raise InboxError("draft is too long (subject 300 characters, body 20000)", 413)
            digest = content_digest(cur["thread_id"], new["recipient"], new["cc"], new["subject"], new["body"])
            if digest == cur["digest"]:
                return {"id": draft_id, "version": cur["version"], "digest": digest, "status": cur["status"], "changed": False}
            if cur["status"] not in ("DRAFT", "APPROVED", "SAVED_TO_GMAIL"):
                raise InboxError(f"a {cur['status'].lower()} draft cannot be edited", 409)
            version = cur["version"] + 1
            now = time.time()
            db.execute("""UPDATE reply_drafts SET recipient=?, cc=?, digest=?, subject_sealed=?, body_sealed=?, status='DRAFT', updated_at=?, version=?
                          WHERE id=? AND user_id=? AND workspace_id=?""",
                       (new["recipient"][:200], new["cc"][:500], digest, self._seal(new["subject"], user_id, workspace_id, draft_id),
                        self._seal(new["body"], user_id, workspace_id, draft_id), now, version, draft_id, user_id, workspace_id))
            self._version(db, user_id, workspace_id, draft_id, version, digest, new["recipient"], new["cc"], new["subject"], new["body"], origin)
            invalidated = db.execute("SELECT COUNT(*) FROM approvals WHERE draft_id=? AND used_at IS NULL AND digest<>?", (draft_id, digest)).fetchone()[0]
            self._audit(db, user_id, workspace_id, "draft.updated", draft_id, {"version": version, "origin": origin, "approvals_invalidated": invalidated})
        return {"id": draft_id, "version": version, "digest": digest, "status": "DRAFT", "changed": True, "approvals_invalidated": invalidated}

    def list_draft_versions(self, user_id: str, workspace_id: str, draft_id: str) -> list[dict]:
        self.get_draft(user_id, workspace_id, draft_id)          # ownership
        with self._db() as db:
            rows = db.execute("SELECT * FROM draft_versions WHERE draft_id=? AND user_id=? AND workspace_id=? ORDER BY version", (draft_id, user_id, workspace_id)).fetchall()
        return [{"version": r["version"], "digest": r["digest"], "recipient": r["recipient"], "cc": r["cc"], "origin": r["origin"],
                 "subject": self._open(r["subject_sealed"], user_id, workspace_id, r["id"]), "body": self._open(r["body_sealed"], user_id, workspace_id, r["id"]),
                 "created_at": r["created_at"]} for r in rows]

    def reject_draft(self, user_id: str, workspace_id: str, draft_id: str, reason: str) -> dict:
        with self._lock, self._db() as db:
            r = db.execute("SELECT status FROM reply_drafts WHERE id=? AND user_id=? AND workspace_id=?", (draft_id, user_id, workspace_id)).fetchone()
            if r is None:
                raise InboxError("draft not found", 404)
            if "REJECTED" not in DRAFT_TRANSITIONS.get(r["status"], set()):
                raise InboxError(f"a {r['status'].lower()} draft cannot be rejected", 409)
            db.execute("UPDATE reply_drafts SET status='REJECTED', updated_at=? WHERE id=?", (time.time(), draft_id))
            self._audit(db, user_id, workspace_id, "draft.rejected", draft_id, {"reason": reason[:120]})
        return {"id": draft_id, "status": "REJECTED"}

    def set_draft_provider_id(self, user_id: str, workspace_id: str, draft_id: str, provider_draft_id: str, status: str) -> None:
        with self._lock, self._db() as db:
            db.execute("UPDATE reply_drafts SET provider_draft_id=?, status=?, updated_at=? WHERE id=? AND user_id=? AND workspace_id=?",
                       (provider_draft_id, status, time.time(), draft_id, user_id, workspace_id))

    # ---------- OAuth tokens: sealed at rest, never returned by any route
    def save_oauth_token(self, user_id: str, workspace_id: str, provider: str, token: dict) -> None:
        rid = f"oauth:{provider}"
        blob = self._seal(json.dumps(token, sort_keys=True), user_id, workspace_id, rid)
        with self._lock, self._db() as db:
            db.execute("""INSERT INTO oauth_tokens(user_id,workspace_id,provider,token_sealed,updated_at) VALUES (?,?,?,?,?)
                          ON CONFLICT(user_id, workspace_id, provider) DO UPDATE SET token_sealed=excluded.token_sealed, updated_at=excluded.updated_at""",
                       (user_id, workspace_id, provider, blob, time.time()))

    def load_oauth_token(self, user_id: str, workspace_id: str, provider: str) -> dict | None:
        with self._db() as db:
            r = db.execute("SELECT token_sealed FROM oauth_tokens WHERE user_id=? AND workspace_id=? AND provider=?", (user_id, workspace_id, provider)).fetchone()
        if r is None:
            return None
        return json.loads(self._open(r["token_sealed"], user_id, workspace_id, f"oauth:{provider}"))

    def delete_oauth_token(self, user_id: str, workspace_id: str, provider: str) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM oauth_tokens WHERE user_id=? AND workspace_id=? AND provider=?", (user_id, workspace_id, provider))
            self._audit(db, user_id, workspace_id, "connection.token_deleted", provider, {})

    # ---------- sync runs (honest progress and failure history; no message content)
    def start_sync_run(self, user_id: str, workspace_id: str, connection_id: str, mode: str) -> str:
        rid = self._id("syn")
        with self._lock, self._db() as db:
            db.execute("INSERT INTO sync_runs(id,user_id,workspace_id,connection_id,mode,state,started_at) VALUES (?,?,?,?,?,'RUNNING',?)",
                       (rid, user_id, workspace_id, connection_id, mode, time.time()))
        return rid

    def finish_sync_run(self, user_id: str, workspace_id: str, run_id: str, state: str, fetched: int = 0, changed: int = 0,
                        new_commitments: int = 0, error: str = "") -> None:
        with self._lock, self._db() as db:
            db.execute("""UPDATE sync_runs SET state=?, finished_at=?, fetched=?, changed=?, new_commitments=?, error=?
                          WHERE id=? AND user_id=? AND workspace_id=?""",
                       (state, time.time(), fetched, changed, new_commitments, error[:200], run_id, user_id, workspace_id))

    def list_sync_runs(self, user_id: str, workspace_id: str, limit: int = 20) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM sync_runs WHERE user_id=? AND workspace_id=? ORDER BY started_at DESC LIMIT ?",
                              (user_id, workspace_id, max(1, min(limit, 100)))).fetchall()
        return [dict(r) for r in rows]

    # ---------- follow-ups (recorded trigger and target)
    def create_follow_up(self, user_id: str, workspace_id: str, commitment_id: str, trigger: str, target: str, note: str = "") -> dict:
        if trigger not in FOLLOW_UP_TRIGGERS:
            raise InboxError("unknown follow-up trigger", 400)
        self.get_commitment(user_id, workspace_id, commitment_id)     # ownership
        fid = self._id("fup")
        with self._lock, self._db() as db:
            db.execute("INSERT INTO follow_ups(id,user_id,workspace_id,commitment_id,trigger,target,note_sealed,status,created_at) VALUES (?,?,?,?,?,?,?,'OPEN',?)",
                       (fid, user_id, workspace_id, commitment_id, trigger, target[:200], self._seal(note, user_id, workspace_id, fid), time.time()))
            self._audit(db, user_id, workspace_id, "follow_up.created", fid, {"trigger": trigger})
        return {"id": fid, "commitment_id": commitment_id, "trigger": trigger, "target": target[:200], "status": "OPEN"}

    def list_follow_ups(self, user_id: str, workspace_id: str, commitment_id: str | None = None) -> list[dict]:
        q = "SELECT * FROM follow_ups WHERE user_id=? AND workspace_id=?"
        args: list = [user_id, workspace_id]
        if commitment_id:
            q += " AND commitment_id=?"; args.append(commitment_id)
        with self._db() as db:
            rows = db.execute(q + " ORDER BY created_at DESC", args).fetchall()
        return [{"id": r["id"], "commitment_id": r["commitment_id"], "trigger": r["trigger"], "target": r["target"], "status": r["status"],
                 "note": self._open(r["note_sealed"], user_id, workspace_id, r["id"]), "created_at": r["created_at"]} for r in rows]

    # ---------- settings (timezone is never assumed silently: timezone_configured says whether the user set it)
    def get_settings(self, user_id: str, workspace_id: str) -> dict:
        with self._db() as db:
            r = db.execute("SELECT * FROM inbox_settings WHERE user_id=? AND workspace_id=?", (user_id, workspace_id)).fetchone()
        if r is None:
            return {"timezone": "UTC", "timezone_configured": False, "signoff": "Best", "tone": "neutral", "replay_anchor": None}
        return {"timezone": r["timezone"], "timezone_configured": True, "signoff": r["signoff"], "tone": r["tone"], "replay_anchor": r["replay_anchor"]}

    def set_settings(self, user_id: str, workspace_id: str, *, timezone_name: str | None = None, signoff: str | None = None,
                     tone: str | None = None, replay_anchor: float | None = None) -> dict:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        cur = self.get_settings(user_id, workspace_id)
        tz = timezone_name if timezone_name is not None else (cur["timezone"] if cur["timezone_configured"] else None)
        if timezone_name is not None:
            try:
                ZoneInfo(timezone_name)
            except (ZoneInfoNotFoundError, ValueError):
                raise InboxError("unknown timezone; use an IANA name such as Europe/Berlin", 400)
        if tz is None:
            raise InboxError("set your timezone first (an IANA name such as Europe/Berlin)", 400)
        sign = (signoff if signoff is not None else cur["signoff"]).strip()[:60]
        tn = (tone if tone is not None else cur["tone"]).strip()[:40]
        anchor = replay_anchor if replay_anchor is not None else cur["replay_anchor"]
        with self._lock, self._db() as db:
            db.execute("""INSERT INTO inbox_settings(user_id,workspace_id,timezone,signoff,tone,replay_anchor,updated_at) VALUES (?,?,?,?,?,?,?)
                          ON CONFLICT(user_id, workspace_id) DO UPDATE SET timezone=excluded.timezone, signoff=excluded.signoff, tone=excluded.tone,
                          replay_anchor=excluded.replay_anchor, updated_at=excluded.updated_at""",
                       (user_id, workspace_id, tz, sign, tn, anchor, time.time()))
            self._audit(db, user_id, workspace_id, "settings.updated", None, {"timezone": tz})
        return self.get_settings(user_id, workspace_id)

    # ---------- commitment notes (context added by the user) and user-supplied corrections
    def add_commitment_note(self, user_id: str, workspace_id: str, commitment_id: str, note: str) -> dict:
        self.get_commitment(user_id, workspace_id, commitment_id)
        nid = self._id("note")
        with self._lock, self._db() as db:
            db.execute("INSERT INTO commitment_notes(id,commitment_id,user_id,workspace_id,note_sealed,created_at) VALUES (?,?,?,?,?,?)",
                       (nid, commitment_id, user_id, workspace_id, self._seal(note, user_id, workspace_id, nid), time.time()))
            self._audit(db, user_id, workspace_id, "commitment.note_added", commitment_id, {})
        return {"id": nid}

    def list_commitment_notes(self, user_id: str, workspace_id: str, commitment_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT id, note_sealed, created_at FROM commitment_notes WHERE commitment_id=? AND user_id=? AND workspace_id=? ORDER BY created_at",
                              (commitment_id, user_id, workspace_id)).fetchall()
        return [{"id": r["id"], "note": self._open(r["note_sealed"], user_id, workspace_id, r["id"]), "created_at": r["created_at"]} for r in rows]

    def update_commitment(self, user_id: str, workspace_id: str, commitment_id: str, *, required_action: str | None = None,
                          responsible_person: str | None = None, requesting_person: str | None = None,
                          deadline_at: float | None = None, deadline_text: str | None = None, deadline_timezone: str | None = None,
                          reason: str = "") -> dict:
        """User corrections to an extracted commitment. Every change is audited and a deadline change keeps the previous one."""
        cur = self.get_commitment(user_id, workspace_id, commitment_id)
        with self._lock, self._db() as db:
            sets, args = [], []
            if required_action is not None:
                sets.append("required_action_sealed=?"); args.append(self._seal(required_action, user_id, workspace_id, commitment_id))
            if responsible_person is not None:
                sets.append("responsible_person=?, ownership_state='USER_CORRECTED'"); args.append(responsible_person[:200])
            if requesting_person is not None:
                sets.append("requesting_person=?"); args.append(requesting_person[:200])
            if deadline_at is not None and deadline_at != cur["deadline_at"]:
                rid = self._id("rev")
                db.execute("""INSERT INTO commitment_revisions(id,commitment_id,user_id,workspace_id,deadline_at,deadline_text,deadline_confidence,
                              deadline_timezone,source_message_id,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                           (rid, commitment_id, user_id, workspace_id, cur["deadline_at"], cur["deadline_text"], cur["deadline_confidence"],
                            cur["deadline_timezone"], cur["source_message_id"], "deadline corrected by the user", time.time()))
                sets.append("deadline_at=?, deadline_confidence='USER_CONFIRMED', deadline_text=?, deadline_timezone=?, deadline_explicit=1, review_reason=NULL")
                args += [deadline_at, (deadline_text or "")[:120], deadline_timezone]
            if not sets:
                return cur
            args += [time.time(), commitment_id, user_id, workspace_id]
            db.execute(f"UPDATE commitments SET {', '.join(sets)}, updated_at=? WHERE id=? AND user_id=? AND workspace_id=?", args)
            self._audit(db, user_id, workspace_id, "commitment.corrected", commitment_id, {"fields": [x.split('=')[0].strip() for x in sets], "reason": reason[:80]})
        return self.get_commitment(user_id, workspace_id, commitment_id)

    def draft_executions(self, user_id: str, workspace_id: str, draft_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("""SELECT e.id, e.operation, e.state, e.provider_id, e.reason, e.updated_at FROM execution_records e
                                 JOIN approvals a ON a.id=e.approval_id WHERE a.draft_id=? AND e.user_id=? AND e.workspace_id=? ORDER BY e.created_at""",
                              (draft_id, user_id, workspace_id)).fetchall()
        return [dict(r) for r in rows]

    def get_execution(self, user_id: str, workspace_id: str, execution_id: str) -> dict:
        with self._db() as db:
            r = db.execute("""SELECT e.*, a.draft_id, a.digest, a.approved_at, a.expires_at, a.used_at FROM execution_records e
                              LEFT JOIN approvals a ON a.id=e.approval_id WHERE e.id=? AND e.user_id=? AND e.workspace_id=?""",
                           (execution_id, user_id, workspace_id)).fetchone()
            ver = db.execute("SELECT method, result, evidence, created_at FROM verification_records WHERE execution_id=? ORDER BY created_at",
                             (execution_id,)).fetchall()
        if r is None:
            raise InboxError("execution not found", 404)
        return {"id": r["id"], "operation": r["operation"], "state": r["state"], "provider_id": r["provider_id"], "reason": r["reason"],
                "operation_key": r["operation_key"], "draft_id": r["draft_id"], "digest": r["digest"], "approved_at": r["approved_at"],
                "expires_at": r["expires_at"], "used_at": r["used_at"], "created_at": r["created_at"], "updated_at": r["updated_at"],
                "timeline": [dict(v) for v in ver]}

    # ---------- simulated outbox (REPLAY mode only; never a real delivery)
    def sim_put(self, user_id: str, workspace_id: str, message_id: str, raw: str) -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT INTO sim_outbox(id,user_id,workspace_id,raw_sealed,created_at) VALUES (?,?,?,?,?)",
                       (message_id, user_id, workspace_id, self._seal(raw, user_id, workspace_id, message_id), time.time()))

    def sim_get(self, user_id: str, workspace_id: str, message_id: str) -> str | None:
        with self._db() as db:
            r = db.execute("SELECT raw_sealed FROM sim_outbox WHERE id=? AND user_id=? AND workspace_id=?", (message_id, user_id, workspace_id)).fetchone()
        return None if r is None else self._open(r["raw_sealed"], user_id, workspace_id, message_id)

    def sim_find_correlation(self, user_id: str, workspace_id: str, message_id_header: str) -> list[str]:
        import base64 as _b
        out = []
        with self._db() as db:
            rows = db.execute("SELECT id, raw_sealed FROM sim_outbox WHERE user_id=? AND workspace_id=?", (user_id, workspace_id)).fetchall()
        for r in rows:
            raw = self._open(r["raw_sealed"], user_id, workspace_id, r["id"])
            if raw and f"Message-ID: <{message_id_header}>".lower() in _b.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "replace").lower():
                out.append(r["id"])
        return out

    def thread_by_provider_id(self, user_id: str, workspace_id: str, provider_thread_id: str) -> str | None:
        with self._db() as db:
            r = db.execute("SELECT id FROM email_threads WHERE user_id=? AND workspace_id=? AND provider_thread_id=?",
                           (user_id, workspace_id, provider_thread_id)).fetchone()
        return None if r is None else r["id"]

    def list_drafts(self, user_id: str, workspace_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT id, thread_id, recipient, cc, status, version, created_at, updated_at FROM reply_drafts "
                              "WHERE user_id=? AND workspace_id=? ORDER BY updated_at DESC", (user_id, workspace_id)).fetchall()
        return [dict(r) for r in rows]

    def get_draft(self, user_id: str, workspace_id: str, draft_id: str) -> dict:
        with self._db() as db:
            r = db.execute("SELECT * FROM reply_drafts WHERE id=? AND user_id=? AND workspace_id=?", (draft_id, user_id, workspace_id)).fetchone()
        if r is None:
            raise InboxError("draft not found", 404)
        return {"id": r["id"], "thread_id": r["thread_id"], "recipient": r["recipient"], "cc": r["cc"] or "", "digest": r["digest"],
                "status": r["status"], "version": r["version"], "provider_draft_id": r["provider_draft_id"],
                "created_at": r["created_at"], "updated_at": r["updated_at"],
                "subject": self._open(r["subject_sealed"], user_id, workspace_id, r["id"]), "body": self._open(r["body_sealed"], user_id, workspace_id, r["id"])}

    def approve(self, user_id: str, workspace_id: str, draft_id: str, presented_digest: str, operation: str, now: float, ttl_seconds: float = 900) -> dict:
        """Explicit approval bound to the draft digest. A changed draft, an expired approval or a reused approval is refused."""
        draft = self.get_draft(user_id, workspace_id, draft_id)
        if presented_digest != draft["digest"]:
            raise InboxError("the draft changed after you reviewed it; review and approve the current version", 409)
        if operation not in ("CREATE_GMAIL_DRAFT", "SEND_EMAIL"):
            raise InboxError("unknown operation", 400)
        if draft["status"] not in ("DRAFT", "APPROVED", "SAVED_TO_GMAIL"):
            raise InboxError(f"a {draft['status'].lower()} draft cannot be approved", 409)
        with self._lock, self._db() as db:
            aid = self._id("apr")
            db.execute("INSERT INTO approvals(id,user_id,workspace_id,draft_id,operation,digest,approved_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                       (aid, user_id, workspace_id, draft_id, operation, draft["digest"], now, now + ttl_seconds))
            db.execute("UPDATE reply_drafts SET status='APPROVED', updated_at=? WHERE id=?", (now, draft_id))
            self._audit(db, user_id, workspace_id, "approval.granted", aid, {"operation": operation, "version": draft["version"]})
        return {"id": aid, "operation": operation, "expires_at": now + ttl_seconds, "digest": draft["digest"], "version": draft["version"]}

    def consume_approval(self, user_id: str, workspace_id: str, approval_id: str, operation: str, digest: str, now: float) -> dict:
        """Mark an approval used exactly once. Refuses expired, reused, wrong-operation or wrong-digest approvals."""
        with self._lock, self._db() as db:
            r = db.execute("""SELECT a.*, d.digest AS current_digest FROM approvals a JOIN reply_drafts d ON d.id=a.draft_id
                              WHERE a.id=? AND a.user_id=? AND a.workspace_id=?""", (approval_id, user_id, workspace_id)).fetchone()
            if r is None:
                raise InboxError("approval not found", 404)
            if r["used_at"] is not None:
                raise InboxError("this approval was already used", 409)
            if now > r["expires_at"]:
                raise InboxError("this approval expired; approve again", 409)
            if r["operation"] != operation or r["digest"] != digest:
                raise InboxError("approval does not match this operation", 409)
            if r["current_digest"] != r["digest"]:
                raise InboxError("the draft changed after this approval; review and approve the current version", 409)
            db.execute("UPDATE approvals SET used_at=? WHERE id=? AND used_at IS NULL", (now, approval_id))
            return {"draft_id": r["draft_id"], "digest": r["digest"]}

    def claim_execution(self, user_id: str, workspace_id: str, approval_id: str, operation_key: str, operation: str) -> tuple[dict, bool]:
        """Returns (record, created). A repeated key returns the existing record and never a second dispatch."""
        with self._lock, self._db() as db:
            hit = db.execute("SELECT * FROM execution_records WHERE user_id=? AND workspace_id=? AND operation_key=?",
                             (user_id, workspace_id, operation_key)).fetchone()
            if hit is not None:
                return dict(hit), False
            eid = self._id("exe")
            db.execute("""INSERT INTO execution_records(id,user_id,workspace_id,approval_id,operation_key,operation,state,created_at,updated_at)
                          VALUES (?,?,?,?,?,?,'EXECUTING',?,?)""", (eid, user_id, workspace_id, approval_id, operation_key, operation, time.time(), time.time()))
            self._audit(db, user_id, workspace_id, "execution.started", eid, {"operation": operation})
            return {"id": eid, "state": "EXECUTING", "provider_id": None}, True

    def finish_execution(self, user_id: str, workspace_id: str, execution_id: str, state: str, provider_id: str | None, reason: str, evidence: str) -> None:
        if state not in EXECUTION_STATES:
            raise InboxError("unknown execution state", 400)
        with self._lock, self._db() as db:
            # a fresh attempt finishes from EXECUTING; a read-only reconciliation may finish from UNCERTAIN or NEEDS_RECOVERY
            r = db.execute("UPDATE execution_records SET state=?, provider_id=?, reason=?, updated_at=? WHERE id=? AND user_id=? AND workspace_id=? "
                           "AND state IN ('EXECUTING','UNCERTAIN','NEEDS_RECOVERY')",
                           (state, provider_id, reason[:200], time.time(), execution_id, user_id, workspace_id))
            if r.rowcount != 1:
                raise InboxError("execution is not in a state that can be finished", 409)
            db.execute("INSERT INTO verification_records(id,user_id,workspace_id,execution_id,method,result,evidence,created_at) VALUES (?,?,?,?,?,?,?,?)",
                       (self._id("ver"), user_id, workspace_id, execution_id, reason[:80], state, evidence[:500], time.time()))
            self._audit(db, user_id, workspace_id, f"execution.{state.lower()}", execution_id, {"reason": reason[:80]})

    def draft_for_execution(self, user_id: str, workspace_id: str, execution_id: str) -> dict:
        with self._db() as db:
            r = db.execute("SELECT a.draft_id FROM execution_records e JOIN approvals a ON a.id=e.approval_id "
                           "WHERE e.id=? AND e.user_id=? AND e.workspace_id=?", (execution_id, user_id, workspace_id)).fetchone()
        if r is None:
            raise InboxError("execution not found", 404)
        return self.get_draft(user_id, workspace_id, r["draft_id"])

    def list_executions(self, user_id: str, workspace_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT e.*, v.method, v.evidence FROM execution_records e LEFT JOIN verification_records v ON v.execution_id=e.id "
                              "WHERE e.user_id=? AND e.workspace_id=? ORDER BY e.created_at DESC", (user_id, workspace_id)).fetchall()
        return [{"id": r["id"], "operation": r["operation"], "state": r["state"], "provider_id": r["provider_id"], "reason": r["reason"],
                 "verification_method": r["method"], "verification_evidence": r["evidence"], "updated_at": r["updated_at"]} for r in rows]

    # ---------- export / deletion (tenant-scoped)
    def export_workspace(self, user_id: str, workspace_id: str) -> dict:
        """Everything this tenant stored, decrypted for the owner. Tokens are never exported."""
        connections = [c for c in (self.get_connection(user_id, workspace_id, p) for p in ("gmail", "replay")) if c]
        return {"connections": connections, "commitments": self.list_commitments(user_id, workspace_id),
                "drafts": [self.get_draft(user_id, workspace_id, d["id"]) for d in self.list_drafts(user_id, workspace_id)],
                "voice_rules": self.list_voice_rules(user_id, workspace_id), "corrections": self.list_corrections(user_id, workspace_id),
                "sync_runs": self.list_sync_runs(user_id, workspace_id, 100), "executions": self.list_executions(user_id, workspace_id),
                "settings": self.get_settings(user_id, workspace_id), "audit": self.audit_log(user_id, workspace_id, 10000)}

    def purge(self, user_id: str, workspace_id: str | None = None) -> int:
        scope = ("user_id=?", (user_id,)) if workspace_id is None else ("user_id=? AND workspace_id=?", (user_id, workspace_id))
        n = 0
        with self._lock, self._db() as db:
            for table in ("commitment_notes", "inbox_settings", "sim_outbox", "follow_ups", "sync_runs", "oauth_tokens", "commitment_status_history", "commitment_revisions",
                          "verification_records", "execution_records", "approvals", "draft_versions", "reply_drafts",
                          "corrections", "commitments", "email_messages", "email_threads", "voice_rules", "audit_events", "email_connections"):
                try:
                    n += db.execute(f"DELETE FROM {table} WHERE {scope[0]}", scope[1]).rowcount
                except sqlite3.OperationalError:
                    pass
        return n



MIGRATION_5 = """
ALTER TABLE commitments ADD COLUMN obligation_key TEXT NOT NULL DEFAULT '';
ALTER TABLE commitments ADD COLUMN ownership_state TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE commitments ADD COLUMN thread_id TEXT;
ALTER TABLE commitments ADD COLUMN deadline_text TEXT;
ALTER TABLE commitments ADD COLUMN deadline_timezone TEXT;
ALTER TABLE commitments ADD COLUMN deadline_explicit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE commitments ADD COLUMN review_reason TEXT;
CREATE INDEX commitments_obligation ON commitments(user_id, workspace_id, thread_id, kind, obligation_key);
CREATE TABLE commitment_revisions (
  id TEXT PRIMARY KEY, commitment_id TEXT NOT NULL REFERENCES commitments(id) ON DELETE CASCADE, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
  deadline_at REAL, deadline_text TEXT, deadline_confidence TEXT, deadline_timezone TEXT, source_message_id TEXT, reason TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE commitment_status_history (
  id TEXT PRIMARY KEY, commitment_id TEXT NOT NULL REFERENCES commitments(id) ON DELETE CASCADE, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
  from_status TEXT NOT NULL, to_status TEXT NOT NULL, reason TEXT, evidence_id TEXT, created_at REAL NOT NULL);
ALTER TABLE reply_drafts ADD COLUMN cc TEXT NOT NULL DEFAULT '';
ALTER TABLE email_messages ADD COLUMN recipients TEXT NOT NULL DEFAULT '';
ALTER TABLE reply_drafts ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
CREATE TABLE draft_versions (
  id TEXT PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES reply_drafts(id) ON DELETE CASCADE, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
  version INTEGER NOT NULL, digest TEXT NOT NULL, recipient TEXT NOT NULL, cc TEXT NOT NULL, subject_sealed BLOB NOT NULL, body_sealed BLOB NOT NULL,
  origin TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(draft_id, version));
CREATE TABLE oauth_tokens (
  user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, provider TEXT NOT NULL, token_sealed BLOB NOT NULL, updated_at REAL NOT NULL,
  PRIMARY KEY(user_id, workspace_id, provider));
CREATE TABLE sync_runs (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, connection_id TEXT, mode TEXT NOT NULL, state TEXT NOT NULL,
  started_at REAL NOT NULL, finished_at REAL, fetched INTEGER NOT NULL DEFAULT 0, changed INTEGER NOT NULL DEFAULT 0,
  new_commitments INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE INDEX sync_runs_scope ON sync_runs(user_id, workspace_id, started_at);
CREATE TABLE follow_ups (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, commitment_id TEXT NOT NULL REFERENCES commitments(id) ON DELETE CASCADE,
  trigger TEXT NOT NULL, target TEXT NOT NULL, note_sealed BLOB, status TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE sim_outbox (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, raw_sealed BLOB NOT NULL, created_at REAL NOT NULL);
"""

MIGRATION_1 = """
CREATE TABLE email_connections (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, provider TEXT NOT NULL,
  status TEXT NOT NULL, scopes TEXT NOT NULL, detail TEXT, updated_at REAL NOT NULL,
  UNIQUE(user_id, workspace_id, provider));
CREATE TABLE email_threads (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, connection_id TEXT NOT NULL REFERENCES email_connections(id) ON DELETE CASCADE,
  provider_thread_id TEXT NOT NULL, updated_at REAL NOT NULL,
  UNIQUE(user_id, workspace_id, connection_id, provider_thread_id));
CREATE TABLE email_messages (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, connection_id TEXT NOT NULL REFERENCES email_connections(id) ON DELETE CASCADE,
  thread_id TEXT NOT NULL REFERENCES email_threads(id) ON DELETE CASCADE, provider_message_id TEXT NOT NULL, sender TEXT NOT NULL,
  sent_at REAL NOT NULL, subject_sealed BLOB, body_sealed BLOB, content_hash TEXT NOT NULL, imported_at REAL NOT NULL,
  UNIQUE(user_id, workspace_id, connection_id, provider_message_id));
CREATE INDEX email_messages_thread ON email_messages(thread_id);
CREATE TABLE commitments (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
  source_message_id TEXT NOT NULL REFERENCES email_messages(id) ON DELETE CASCADE, kind TEXT NOT NULL, status TEXT NOT NULL,
  dedupe_key TEXT NOT NULL, requesting_person TEXT, responsible_person TEXT, deadline_at REAL, deadline_confidence TEXT,
  required_action_sealed BLOB NOT NULL, evidence_sealed BLOB NOT NULL, resolution_evidence_id TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(user_id, workspace_id, dedupe_key));
CREATE INDEX commitments_scope ON commitments(user_id, workspace_id, status);
CREATE TABLE voice_rules (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, version INTEGER NOT NULL, instruction_sealed BLOB NOT NULL,
  parsed_json TEXT NOT NULL, scope TEXT NOT NULL, active INTEGER NOT NULL, confirmed INTEGER NOT NULL, created_at REAL NOT NULL,
  UNIQUE(user_id, workspace_id, version));
CREATE TABLE audit_events (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, action TEXT NOT NULL, subject_id TEXT,
  detail TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX audit_scope ON audit_events(user_id, workspace_id, created_at);
"""


MIGRATION_2 = """
CREATE TABLE reply_drafts (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, thread_id TEXT NOT NULL REFERENCES email_threads(id) ON DELETE CASCADE,
  recipient TEXT NOT NULL, digest TEXT NOT NULL, subject_sealed BLOB NOT NULL, body_sealed BLOB NOT NULL,
  status TEXT NOT NULL, provider_draft_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX reply_drafts_scope ON reply_drafts(user_id, workspace_id, status);
CREATE TABLE approvals (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, draft_id TEXT NOT NULL REFERENCES reply_drafts(id) ON DELETE CASCADE,
  operation TEXT NOT NULL, digest TEXT NOT NULL, approved_at REAL NOT NULL, expires_at REAL NOT NULL, used_at REAL);
CREATE TABLE execution_records (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, approval_id TEXT NOT NULL REFERENCES approvals(id) ON DELETE CASCADE,
  operation_key TEXT NOT NULL, operation TEXT NOT NULL, state TEXT NOT NULL, provider_id TEXT, reason TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE(user_id, workspace_id, operation_key));
CREATE TABLE verification_records (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, execution_id TEXT NOT NULL REFERENCES execution_records(id) ON DELETE CASCADE,
  method TEXT NOT NULL, result TEXT NOT NULL, evidence TEXT NOT NULL, created_at REAL NOT NULL);
"""


MIGRATION_3 = """
CREATE TABLE corrections (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, thread_id TEXT, original_json TEXT NOT NULL,
  corrected_json TEXT NOT NULL, explanation_sealed BLOB NOT NULL, scope TEXT NOT NULL, active INTEGER NOT NULL,
  created_at REAL NOT NULL, reversed_at REAL);
CREATE INDEX corrections_scope ON corrections(user_id, workspace_id, active);
"""


MIGRATION_4 = """
CREATE TABLE triage_decisions (
  thread_id TEXT PRIMARY KEY REFERENCES email_threads(id) ON DELETE CASCADE, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
  category TEXT NOT NULL, priority TEXT NOT NULL, confidence REAL NOT NULL, reasons_json TEXT NOT NULL, evidence_sealed BLOB,
  computed_at REAL NOT NULL);
CREATE INDEX triage_scope ON triage_decisions(user_id, workspace_id, category);
"""

MIGRATION_6 = """
CREATE TABLE inbox_settings (
  user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, timezone TEXT NOT NULL, signoff TEXT NOT NULL, tone TEXT NOT NULL,
  replay_anchor REAL, updated_at REAL NOT NULL, PRIMARY KEY(user_id, workspace_id));
CREATE TABLE commitment_notes (
  id TEXT PRIMARY KEY, commitment_id TEXT NOT NULL REFERENCES commitments(id) ON DELETE CASCADE, user_id TEXT NOT NULL,
  workspace_id TEXT NOT NULL, note_sealed BLOB NOT NULL, created_at REAL NOT NULL);
"""
