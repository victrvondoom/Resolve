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
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from at_rest import SealedStore

SCHEMA_VERSION = 1
COMMITMENT_STATUSES = ("OPEN", "WAITING", "RESOLVED", "DISMISSED", "CANCELLED")
DRAFT_STATUSES = ("DRAFT", "APPROVED", "REJECTED", "DEFERRED", "SAVED_TO_GMAIL", "SENT_VERIFIED", "UNCERTAIN")
EXECUTION_STATES = ("REQUESTED", "EXECUTING", "VERIFIED", "UNCERTAIN", "FAILED", "REJECTED", "NEEDS_RECOVERY")


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
                       sender: str, sent_at: float, subject: str, body: str, content_hash: str | None = None) -> dict:
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
                db.execute("UPDATE email_messages SET body_sealed=?, content_hash=?, subject_sealed=? WHERE id=?",
                           (self._seal(body, user_id, workspace_id, existing["id"]), body_hash, self._seal(subject, user_id, workspace_id, existing["id"]), existing["id"]))
                self._audit(db, user_id, workspace_id, "message.changed", existing["id"], {})
                return {"id": existing["id"], "thread_id": tid, "changed": True}
            mid = self._id("msg")
            db.execute("""INSERT INTO email_messages(id,user_id,workspace_id,connection_id,thread_id,provider_message_id,sender,sent_at,
                          subject_sealed,body_sealed,content_hash,imported_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (mid, user_id, workspace_id, connection_id, tid, provider_message_id, sender[:200], sent_at,
                        self._seal(subject, user_id, workspace_id, mid), self._seal(body, user_id, workspace_id, mid), body_hash, time.time()))
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
                "sent_at": r["sent_at"], "subject": self._open(r["subject_sealed"], user_id, workspace_id, r["id"]),
                "body": self._open(r["body_sealed"], user_id, workspace_id, r["id"])}

    def list_messages(self, user_id: str, workspace_id: str, thread_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT id FROM email_messages WHERE thread_id=? AND user_id=? AND workspace_id=? ORDER BY sent_at", (thread_id, user_id, workspace_id)).fetchall()
        return [self.read_message(user_id, workspace_id, r["id"]) for r in rows]

    # ---------- commitments (deduplicated per source message and normalized text)
    def add_commitment(self, user_id: str, workspace_id: str, source_message_id: str, kind: str, required_action: str,
                       evidence_text: str, requesting_person: str | None, responsible_person: str | None,
                       deadline_at: float | None, deadline_confidence: str | None) -> dict:
        if kind not in ("REQUEST_TO_USER", "PROMISE_BY_USER", "PROMISE_BY_OTHER", "DEPENDENCY", "SUGGESTION", "HISTORICAL"):
            raise InboxError("unknown commitment kind", 400)
        self.read_message(user_id, workspace_id, source_message_id)      # ownership: 404 if not this tenant's message
        norm = " ".join(required_action.lower().split())
        dedupe = hashlib.sha256(f"{source_message_id}|{kind}|{norm}".encode()).hexdigest()
        with self._lock, self._db() as db:
            hit = db.execute("SELECT id FROM commitments WHERE user_id=? AND workspace_id=? AND dedupe_key=?", (user_id, workspace_id, dedupe)).fetchone()
            if hit:
                return {"id": hit["id"], "created": False}
            cid = self._id("cmt")
            now = time.time()
            db.execute("""INSERT INTO commitments(id,user_id,workspace_id,source_message_id,kind,status,dedupe_key,requesting_person,
                          responsible_person,deadline_at,deadline_confidence,required_action_sealed,evidence_sealed,created_at,updated_at)
                          VALUES (?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?,?)""",
                       (cid, user_id, workspace_id, source_message_id, kind, dedupe, requesting_person, responsible_person, deadline_at,
                        deadline_confidence, self._seal(required_action, user_id, workspace_id, cid), self._seal(evidence_text, user_id, workspace_id, cid), now, now))
            self._audit(db, user_id, workspace_id, "commitment.created", cid, {"kind": kind})
            return {"id": cid, "created": True}

    def list_commitments(self, user_id: str, workspace_id: str, status: str | None = None) -> list[dict]:
        q = "SELECT * FROM commitments WHERE user_id=? AND workspace_id=?"
        args: list = [user_id, workspace_id]
        if status:
            q += " AND status=?"; args.append(status)
        with self._db() as db:
            rows = db.execute(q + " ORDER BY created_at DESC", args).fetchall()
        out = []
        for r in rows:
            out.append({"id": r["id"], "source_message_id": r["source_message_id"], "kind": r["kind"], "status": r["status"],
                        "requesting_person": r["requesting_person"], "responsible_person": r["responsible_person"],
                        "deadline_at": r["deadline_at"], "deadline_confidence": r["deadline_confidence"],
                        "required_action": self._open(r["required_action_sealed"], user_id, workspace_id, r["id"]),
                        "evidence": self._open(r["evidence_sealed"], user_id, workspace_id, r["id"]),
                        "created_at": r["created_at"], "updated_at": r["updated_at"]})
        return out

    def set_commitment_status(self, user_id: str, workspace_id: str, commitment_id: str, status: str, evidence_id: str | None = None) -> dict:
        if status not in COMMITMENT_STATUSES:
            raise InboxError("unknown status", 400)
        if status == "RESOLVED" and not evidence_id:
            raise InboxError("a commitment can only be resolved with verification evidence", 409)
        with self._lock, self._db() as db:
            r = db.execute("UPDATE commitments SET status=?, updated_at=?, resolution_evidence_id=? WHERE id=? AND user_id=? AND workspace_id=?",
                           (status, time.time(), evidence_id, commitment_id, user_id, workspace_id))
            if r.rowcount != 1:
                raise InboxError("commitment not found", 404)
            self._audit(db, user_id, workspace_id, "commitment.status", commitment_id, {"status": status})
        return {"id": commitment_id, "status": status}

    # ---------- voice rules (versioned, reversible)
    def add_voice_rule(self, user_id: str, workspace_id: str, instruction: str, parsed: dict, scope: str, confirmed: bool) -> dict:
        with self._lock, self._db() as db:
            rid = self._id("rule")
            prev = db.execute("SELECT COALESCE(MAX(version),0) FROM voice_rules WHERE user_id=? AND workspace_id=?", (user_id, workspace_id)).fetchone()[0]
            db.execute("""INSERT INTO voice_rules(id,user_id,workspace_id,version,instruction_sealed,parsed_json,scope,active,confirmed,created_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?)""",
                       (rid, user_id, workspace_id, prev + 1, self._seal(instruction, user_id, workspace_id, rid), json.dumps(parsed, sort_keys=True),
                        scope, 1 if confirmed else 0, 1 if confirmed else 0, time.time()))
            self._audit(db, user_id, workspace_id, "voice_rule.added", rid, {"scope": scope, "version": prev + 1})
            return {"id": rid, "version": prev + 1, "active": bool(confirmed)}

    def list_voice_rules(self, user_id: str, workspace_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM voice_rules WHERE user_id=? AND workspace_id=? ORDER BY version DESC", (user_id, workspace_id)).fetchall()
        return [{"id": r["id"], "version": r["version"], "instruction": self._open(r["instruction_sealed"], user_id, workspace_id, r["id"]),
                 "parsed": json.loads(r["parsed_json"]), "scope": r["scope"], "active": bool(r["active"]), "confirmed": bool(r["confirmed"]),
                 "created_at": r["created_at"]} for r in rows]

    def set_voice_rule_active(self, user_id: str, workspace_id: str, rule_id: str, active: bool) -> dict:
        with self._lock, self._db() as db:
            r = db.execute("UPDATE voice_rules SET active=?, confirmed=CASE WHEN ?=1 THEN 1 ELSE confirmed END WHERE id=? AND user_id=? AND workspace_id=?",
                           (1 if active else 0, 1 if active else 0, rule_id, user_id, workspace_id))
            if r.rowcount != 1:
                raise InboxError("rule not found", 404)
            self._audit(db, user_id, workspace_id, "voice_rule.active", rule_id, {"active": bool(active)})
        return {"id": rule_id, "active": active}

    # ---------- audit (ids, counts, statuses only)
    def audit_log(self, user_id: str, workspace_id: str, limit: int = 200) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM audit_events WHERE user_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT ?",
                              (user_id, workspace_id, int(limit))).fetchall()
        return [{"id": r["id"], "action": r["action"], "subject_id": r["subject_id"], "detail": json.loads(r["detail"]), "created_at": r["created_at"]} for r in rows]

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
    def create_draft(self, user_id: str, workspace_id: str, thread_id: str, recipient: str, subject: str, body: str, digest: str) -> dict:
        with self._lock, self._db() as db:
            if db.execute("SELECT 1 FROM email_threads WHERE id=? AND user_id=? AND workspace_id=?", (thread_id, user_id, workspace_id)).fetchone() is None:
                raise InboxError("thread not found", 404)
            did = self._id("drf")
            db.execute("""INSERT INTO reply_drafts(id,user_id,workspace_id,thread_id,recipient,digest,subject_sealed,body_sealed,status,created_at,updated_at)
                          VALUES (?,?,?,?,?,?,?,?,'DRAFT',?,?)""",
                       (did, user_id, workspace_id, thread_id, recipient[:200], digest, self._seal(subject, user_id, workspace_id, did),
                        self._seal(body, user_id, workspace_id, did), time.time(), time.time()))
            self._audit(db, user_id, workspace_id, "draft.created", did, {})
        return {"id": did, "digest": digest}

    def get_draft(self, user_id: str, workspace_id: str, draft_id: str) -> dict:
        with self._db() as db:
            r = db.execute("SELECT * FROM reply_drafts WHERE id=? AND user_id=? AND workspace_id=?", (draft_id, user_id, workspace_id)).fetchone()
        if r is None:
            raise InboxError("draft not found", 404)
        return {"id": r["id"], "thread_id": r["thread_id"], "recipient": r["recipient"], "digest": r["digest"], "status": r["status"],
                "provider_draft_id": r["provider_draft_id"],
                "subject": self._open(r["subject_sealed"], user_id, workspace_id, r["id"]), "body": self._open(r["body_sealed"], user_id, workspace_id, r["id"])}

    def approve(self, user_id: str, workspace_id: str, draft_id: str, presented_digest: str, operation: str, now: float, ttl_seconds: float = 900) -> dict:
        """Explicit approval bound to the draft digest. A changed draft, an expired approval or a reused approval is refused."""
        draft = self.get_draft(user_id, workspace_id, draft_id)
        if presented_digest != draft["digest"]:
            raise InboxError("the draft changed after you reviewed it; approve the current version", 409)
        if operation not in ("CREATE_GMAIL_DRAFT", "SEND_EMAIL"):
            raise InboxError("unknown operation", 400)
        with self._lock, self._db() as db:
            aid = self._id("apr")
            db.execute("INSERT INTO approvals(id,user_id,workspace_id,draft_id,operation,digest,approved_at,expires_at) VALUES (?,?,?,?,?,?,?,?)",
                       (aid, user_id, workspace_id, draft_id, operation, draft["digest"], now, now + ttl_seconds))
            db.execute("UPDATE reply_drafts SET status='APPROVED', updated_at=? WHERE id=?", (now, draft_id))
            self._audit(db, user_id, workspace_id, "approval.granted", aid, {"operation": operation})
        return {"id": aid, "operation": operation, "expires_at": now + ttl_seconds}

    def consume_approval(self, user_id: str, workspace_id: str, approval_id: str, operation: str, digest: str, now: float) -> dict:
        """Mark an approval used exactly once. Refuses expired, reused, wrong-operation or wrong-digest approvals."""
        with self._lock, self._db() as db:
            r = db.execute("SELECT * FROM approvals WHERE id=? AND user_id=? AND workspace_id=?", (approval_id, user_id, workspace_id)).fetchone()
            if r is None:
                raise InboxError("approval not found", 404)
            if r["used_at"] is not None:
                raise InboxError("this approval was already used", 409)
            if now > r["expires_at"]:
                raise InboxError("this approval expired; approve again", 409)
            if r["operation"] != operation or r["digest"] != digest:
                raise InboxError("approval does not match this operation", 409)
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
        return {"connections": [self.get_connection(user_id, workspace_id, p) for p in ("gmail",) if self.get_connection(user_id, workspace_id, p)],
                "commitments": self.list_commitments(user_id, workspace_id), "voice_rules": self.list_voice_rules(user_id, workspace_id),
                "audit": self.audit_log(user_id, workspace_id, 10000)}

    def purge(self, user_id: str, workspace_id: str | None = None) -> int:
        scope = ("user_id=?", (user_id,)) if workspace_id is None else ("user_id=? AND workspace_id=?", (user_id, workspace_id))
        n = 0
        with self._lock, self._db() as db:
            for table in ("commitments", "email_messages", "email_threads", "voice_rules", "audit_events", "email_connections"):
                n += db.execute(f"DELETE FROM {table} WHERE {scope[0]}", scope[1]).rowcount
        return n


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
