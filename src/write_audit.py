"""Audit trail for direct document writes, recorded at one boundary (write_boundary in resolve_app).

Every document write attempt, allowed or rejected, is recorded with the acting user and workspace, the operation,
the target id, the content hash before and after, and the outcome. Rows are tenant-scoped, exportable, and purged
with the workspace or account. Only ids and hashes are stored: never file contents or credentials.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

OUTCOMES = ("ALLOWED", "REJECTED", "FAILED")


class WriteAudit:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS write_audit (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, operation TEXT NOT NULL,
                target_id TEXT, before_sha256 TEXT, after_sha256 TEXT, outcome TEXT NOT NULL, reason TEXT, created_at REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS write_audit_scope ON write_audit(user_id, workspace_id, created_at)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, user_id: str, workspace_id: str, operation: str, target_id: str | None, outcome: str,
               before_sha256: str | None = None, after_sha256: str | None = None, reason: str | None = None) -> str:
        if outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {outcome}")
        rid = "wa_" + uuid.uuid4().hex[:12]
        with self._lock, self._db() as db:
            db.execute("INSERT INTO write_audit VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (rid, user_id, workspace_id, operation, target_id, before_sha256, after_sha256, outcome, (reason or "")[:200], time.time()))
        return rid

    def list(self, user_id: str, workspace_id: str, limit: int = 200) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM write_audit WHERE user_id=? AND workspace_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                              (user_id, workspace_id, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def purge(self, user_id: str, workspace_id: str | None = None) -> int:
        if workspace_id is None:
            q, args = "user_id=?", (user_id,)
        else:
            q, args = "user_id=? AND workspace_id=?", (user_id, workspace_id)
        with self._db() as db:
            return db.execute(f"DELETE FROM write_audit WHERE {q}", args).rowcount
