"""Multi-tenant authentication, workspace isolation, and session security for RESOLVE."""
from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.environ.get('RESOLVE_TENANTS_DB', ROOT / 'data' / 'tenants.sqlite3'))


PBKDF2_ITERATIONS = 600_000      # OWASP-recommended work factor for PBKDF2-HMAC-SHA256
LEGACY_ITERATIONS = 100_000      # hashes stored before the work factor was recorded alongside the digest


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, PBKDF2_ITERATIONS).hex()
    return f"{PBKDF2_ITERATIONS}${digest}", salt.hex()


def verify_password(password: str, pwd_hash: str, salt_hex: str) -> bool:
    iterations, _, digest = pwd_hash.rpartition('$')
    rounds = int(iterations) if iterations else LEGACY_ITERATIONS
    check = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), bytes.fromhex(salt_hex), rounds).hex()
    return secrets.compare_digest(digest, check)


class TenantError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class TenantAuthManager:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextlib.contextmanager
    def _get_conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                display_name TEXT,
                created_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE'
            );

            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                storage_namespace TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_workspaces_owner ON workspaces(owner_user_id);
            """)

    def register(self, email: str, password: str, display_name: str | None = None) -> dict:
        email = email.strip().lower()
        if not email or '@' not in email:
            raise TenantError("Valid email required", status_code=400)
        if len(password) < 10 or len(password) > 256:
            raise TenantError("Password must be 10-256 characters", status_code=400)

        pwd_hash, salt = hash_password(password)
        user_id = f"usr_{uuid.uuid4().hex[:12]}"
        ws_id = f"ws_{uuid.uuid4().hex[:12]}"
        now = time.time()
        name = (str(display_name).strip()[:40] if display_name else '') or email.split('@')[0].capitalize()[:40]

        with self._get_conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO users (id, email, password_hash, salt, display_name, created_at, status) VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE')",
                    (user_id, email, pwd_hash, salt, name, now)
                )
                conn.execute(
                    "INSERT INTO workspaces (id, owner_user_id, name, storage_namespace, created_at) VALUES (?, ?, ?, ?, ?)",
                    (ws_id, user_id, f"{name}'s Workspace", f"ns_{user_id}", now)
                )
            except sqlite3.IntegrityError:
                raise TenantError("Email is already registered", status_code=409)

        return self.create_session(user_id, ws_id)

    def login(self, email: str, password: str) -> dict:
        email = email.strip().lower()
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM users WHERE email = ? AND status = 'ACTIVE'", (email,)).fetchone()
            if not row or not verify_password(password, row['password_hash'], row['salt']):
                raise TenantError("Invalid email or password", status_code=401)
            
            user_id = row['id']
            ws_row = conn.execute("SELECT id FROM workspaces WHERE owner_user_id = ? ORDER BY created_at ASC LIMIT 1", (user_id,)).fetchone()
            if not ws_row:
                ws_id = f"ws_{uuid.uuid4().hex[:12]}"
                conn.execute(
                    "INSERT INTO workspaces (id, owner_user_id, name, storage_namespace, created_at) VALUES (?, ?, ?, ?, ?)",
                    (ws_id, user_id, "Personal Workspace", f"ns_{user_id}", time.time())
                )
            else:
                ws_id = ws_row['id']

        return self.create_session(user_id, ws_id)

    def create_session(self, user_id: str, workspace_id: str, duration_sec: int = 86400 * 7) -> dict:
        token = f"rst_{secrets.token_urlsafe(32)}"
        now = time.time()
        expires_at = now + duration_sec

        with self._get_conn() as conn:
            user = conn.execute("SELECT id, email, display_name FROM users WHERE id = ? AND status = 'ACTIVE'", (user_id,)).fetchone()
            if not user:
                raise TenantError("User not found or inactive", status_code=404)
            ws = conn.execute("SELECT id, name, storage_namespace FROM workspaces WHERE id = ? AND owner_user_id = ?", (workspace_id, user_id)).fetchone()
            if not ws:
                raise TenantError("Workspace not found or unauthorized", status_code=403)

            conn.execute(
                "INSERT INTO sessions (token, user_id, workspace_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (token, user_id, workspace_id, now, expires_at)
            )

        return {
            "token": token,
            "user_id": user_id,
            "email": user['email'],
            "display_name": user['display_name'],
            "workspace_id": workspace_id,
            "workspace_name": ws['name'],
            "storage_namespace": ws['storage_namespace'],
            "expires_at": expires_at
        }

    def validate_session(self, token: str) -> dict:
        if not token:
            raise TenantError("Session token missing", status_code=401)
        now = time.time()
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT s.token, s.user_id, s.workspace_id, s.expires_at,
                       u.email, u.display_name, u.status as user_status,
                       w.name as workspace_name, w.storage_namespace
                FROM sessions s
                JOIN users u ON s.user_id = u.id
                JOIN workspaces w ON s.workspace_id = w.id
                WHERE s.token = ?
            """, (token,)).fetchone()

            if not row:
                raise TenantError("Invalid or expired session token", status_code=401)
            if row['expires_at'] < now or row['user_status'] != 'ACTIVE':
                conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
                raise TenantError("Session has expired", status_code=401)

            return {
                "token": row['token'],
                "user_id": row['user_id'],
                "email": row['email'],
                "display_name": row['display_name'],
                "workspace_id": row['workspace_id'],
                "workspace_name": row['workspace_name'],
                "storage_namespace": row['storage_namespace']
            }

    def switch_workspace(self, token: str, new_workspace_id: str) -> dict:
        session = self.validate_session(token)
        user_id = session['user_id']
        with self._get_conn() as conn:
            ws = conn.execute("SELECT id, name, storage_namespace FROM workspaces WHERE id = ? AND owner_user_id = ?", (new_workspace_id, user_id)).fetchone()
            if not ws:
                raise TenantError("Workspace not accessible", status_code=403)
            conn.execute("UPDATE sessions SET workspace_id = ? WHERE token = ?", (new_workspace_id, token))

        return self.validate_session(token)

    def logout(self, token: str) -> bool:
        with self._get_conn() as conn:
            res = conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return res.rowcount > 0

    def list_workspaces(self, user_id: str) -> list[dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT id, name, storage_namespace, created_at FROM workspaces WHERE owner_user_id = ? ORDER BY created_at ASC", (user_id,)).fetchall()
            return [dict(r) for r in rows]

    def create_workspace(self, user_id: str, name: str) -> dict:
        name = (name or "").strip()
        if not name or len(name) > 60:
            raise TenantError("Workspace name must be 1-60 characters", status_code=400)
        with self._get_conn() as conn:
            if conn.execute("SELECT COUNT(*) AS c FROM workspaces WHERE owner_user_id = ?", (user_id,)).fetchone()['c'] >= 10:
                raise TenantError("Workspace limit reached (10); delete one first", status_code=429)
        ws_id = f"ws_{uuid.uuid4().hex[:12]}"
        now = time.time()
        with self._get_conn() as conn:
            conn.execute(
                "INSERT INTO workspaces (id, owner_user_id, name, storage_namespace, created_at) VALUES (?, ?, ?, ?, ?)",
                (ws_id, user_id, name, f"ns_{user_id}_{ws_id[-6:]}", now)
            )
        return {"id": ws_id, "name": name, "owner_user_id": user_id, "created_at": now}

    def delete_workspace(self, user_id: str, workspace_id: str) -> bool:
        with self._get_conn() as conn:
            # Check count of workspaces; user must retain at least one workspace unless deleting account
            count = conn.execute("SELECT COUNT(*) as c FROM workspaces WHERE owner_user_id = ?", (user_id,)).fetchone()['c']
            if count <= 1:
                raise TenantError("Cannot delete your only workspace. Delete account to remove all data.", status_code=400)
            # Sessions inside the deleted workspace move to a surviving one FIRST: sessions cascade-delete with their
            # workspace, so deleting your *active* workspace used to silently log you out.
            survivor = conn.execute("SELECT id FROM workspaces WHERE owner_user_id = ? AND id != ? ORDER BY created_at ASC LIMIT 1",
                                    (user_id, workspace_id)).fetchone()
            if survivor:
                conn.execute("UPDATE sessions SET workspace_id = ? WHERE user_id = ? AND workspace_id = ?", (survivor['id'], user_id, workspace_id))
            res = conn.execute("DELETE FROM workspaces WHERE id = ? AND owner_user_id = ?", (workspace_id, user_id))
            return res.rowcount > 0

    def delete_user(self, user_id: str) -> bool:
        with self._get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM workspaces WHERE owner_user_id = ?", (user_id,))
            res = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            return res.rowcount > 0

    def seed_demo_tenant(self, create_session: bool = True) -> dict | None:
        """Local-demo identity. The password is random per process and never stored in source: the old fixed
        'demo123' meant every deployment contained a publicly known shared account. An existing demo account
        (e.g. from an older database) has its password rotated, so the old known password stops working."""
        email, pw = 'demo@resolve.local', secrets.token_urlsafe(24)
        with self._get_conn() as conn:
            row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if row:
                pwd_hash, salt = hash_password(pw)
                conn.execute("UPDATE users SET password_hash = ?, salt = ? WHERE email = ?", (pwd_hash, salt, email))
        if not create_session:
            return None
        return self.login(email, pw) if row else self.register(email, pw, 'Demo Operator')
