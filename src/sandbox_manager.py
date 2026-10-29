"""Multi-tenant sandbox infrastructure, lifecycle manager, and isolation provider for RESOLVE."""
from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SANDBOX_DIR = Path(os.environ.get('RESOLVE_SANDBOXES_DIR', ROOT / 'data' / 'sandboxes'))
DEFAULT_DB = Path(os.environ.get('RESOLVE_SANDBOXES_DB', ROOT / 'data' / 'sandboxes.sqlite3'))


class SandboxState(str, Enum):
    REQUESTED = "REQUESTED"
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    BUSY = "BUSY"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    TERMINATING = "TERMINATING"
    DESTROYED = "DESTROYED"


class SandboxError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class SandboxSecurityError(SandboxError):
    def __init__(self, message: str = "Access to sandbox forbidden"):
        super().__init__(message, status_code=403)


@dataclass
class SandboxRecord:
    id: str
    owner_user_id: str
    workspace_id: str
    mission_id: str | None
    status: str
    provider: str
    storage_path: str
    created_at: float
    expires_at: float
    storage_quota_bytes: int = 100 * 1024 * 1024  # 100MB
    network_policy: str = "RESTRICTED"

    def to_dict(self) -> dict:
        return asdict(self)

    def public_dict(self) -> dict:
        """API-safe view: never reveals the server filesystem path."""
        d = asdict(self)
        d.pop('storage_path', None)
        return d


class SandboxManager:
    def __init__(self, base_dir: Path | str | None = None, db_path: Path | str | None = None):
        self.base_dir = Path(base_dir) if base_dir else DEFAULT_SANDBOX_DIR
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
            CREATE TABLE IF NOT EXISTS sandboxes (
                id TEXT PRIMARY KEY,
                owner_user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                mission_id TEXT,
                status TEXT NOT NULL,
                provider TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                storage_quota_bytes INTEGER NOT NULL,
                network_policy TEXT NOT NULL
            );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sbx_owner ON sandboxes(owner_user_id);")

    def create_sandbox(
        self,
        owner_user_id: str,
        workspace_id: str,
        mission_id: str | None = None,
        provider: str = "local_isolated",
        ttl_seconds: int = 3600
    ) -> SandboxRecord:
        if not owner_user_id or not workspace_id:
            raise SandboxError("owner_user_id and workspace_id are required")

        sandbox_id = f"sbx_{uuid.uuid4().hex[:12]}"
        now = time.time()
        expires_at = now + ttl_seconds
        storage_path = str(self.base_dir / sandbox_id)

        # 1. Transition: REQUESTED -> PROVISIONING
        Path(storage_path).mkdir(parents=True, exist_ok=True)
        (Path(storage_path) / "workspace").mkdir(parents=True, exist_ok=True)
        (Path(storage_path) / "tmp").mkdir(parents=True, exist_ok=True)
        (Path(storage_path) / "browser_profile").mkdir(parents=True, exist_ok=True)

        record = SandboxRecord(
            id=sandbox_id,
            owner_user_id=owner_user_id,
            workspace_id=workspace_id,
            mission_id=mission_id,
            status=SandboxState.READY.value,
            provider=provider,
            storage_path=storage_path,
            created_at=now,
            expires_at=expires_at
        )

        with self._conn() as conn:
            conn.execute("""
            INSERT INTO sandboxes (
                id, owner_user_id, workspace_id, mission_id, status, provider,
                storage_path, created_at, expires_at, storage_quota_bytes, network_policy
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                record.id, record.owner_user_id, record.workspace_id, record.mission_id,
                record.status, record.provider, record.storage_path, record.created_at,
                record.expires_at, record.storage_quota_bytes, record.network_policy
            ))

        return record

    def get_sandbox(self, sandbox_id: str, caller_user_id: str | None = None) -> SandboxRecord:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sandboxes WHERE id = ?", (sandbox_id,)).fetchone()
            if not row:
                raise SandboxError(f"Sandbox {sandbox_id} not found", status_code=404)
            record = SandboxRecord(**dict(row))

        if caller_user_id and record.owner_user_id != caller_user_id:
            raise SandboxSecurityError(f"User {caller_user_id} unauthorized to access sandbox {sandbox_id}")
        return record

    def list_sandboxes(self, caller_user_id: str) -> list[SandboxRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sandboxes WHERE owner_user_id = ? AND status != 'DESTROYED' ORDER BY created_at DESC",
                (caller_user_id,)
            ).fetchall()
            return [SandboxRecord(**dict(r)) for r in rows]

    def set_status(self, sandbox_id: str, new_status: SandboxState, caller_user_id: str | None = None) -> SandboxRecord:
        sandbox = self.get_sandbox(sandbox_id, caller_user_id)
        with self._conn() as conn:
            conn.execute("UPDATE sandboxes SET status = ? WHERE id = ?", (new_status.value, sandbox_id))
        return self.get_sandbox(sandbox_id)

    def pause_sandbox(self, sandbox_id: str, caller_user_id: str | None = None) -> SandboxRecord:
        sandbox = self.get_sandbox(sandbox_id, caller_user_id)
        if sandbox.status not in (SandboxState.READY.value, SandboxState.BUSY.value):
            raise SandboxError(f"Cannot pause sandbox in state {sandbox.status}")
        return self.set_status(sandbox_id, SandboxState.PAUSED, caller_user_id)

    def resume_sandbox(self, sandbox_id: str, caller_user_id: str | None = None) -> SandboxRecord:
        sandbox = self.get_sandbox(sandbox_id, caller_user_id)
        if sandbox.status != SandboxState.PAUSED.value:
            raise SandboxError(f"Cannot resume sandbox in state {sandbox.status}")
        return self.set_status(sandbox_id, SandboxState.READY, caller_user_id)

    def terminate_sandbox(self, sandbox_id: str, caller_user_id: str | None = None) -> SandboxRecord:
        sandbox = self.get_sandbox(sandbox_id, caller_user_id)
        return self.set_status(sandbox_id, SandboxState.TERMINATING, caller_user_id)

    def destroy_sandbox(self, sandbox_id: str, caller_user_id: str | None = None) -> bool:
        sandbox = self.get_sandbox(sandbox_id, caller_user_id)
        # 1. Update state
        with self._conn() as conn:
            conn.execute("UPDATE sandboxes SET status = 'DESTROYED' WHERE id = ?", (sandbox_id,))

        # 2. Securely remove storage directory
        path = Path(sandbox.storage_path)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        return True

    def execute_in_sandbox(self, sandbox_id: str, command: list[str], caller_user_id: str | None = None, timeout_sec: int = 30) -> dict:
        sandbox = self.get_sandbox(sandbox_id, caller_user_id)
        if sandbox.status not in (SandboxState.READY.value, SandboxState.BUSY.value):
            raise SandboxError(f"Cannot execute in sandbox with status {sandbox.status}")

        self.set_status(sandbox_id, SandboxState.BUSY)
        work_dir = Path(sandbox.storage_path) / "workspace"
        start_t = time.time()
        try:
            # Execute with restricted environment
            restricted_env = {
                "PATH": os.environ.get("PATH", ""),
                "RESOLVE_SANDBOX_ID": sandbox.id,
                "RESOLVE_WORKSPACE_ID": sandbox.workspace_id,
                "RESOLVE_USER_ID": sandbox.owner_user_id,
                "TMP": str(Path(sandbox.storage_path) / "tmp"),
                "TEMP": str(Path(sandbox.storage_path) / "tmp")
            }
            res = subprocess.run(
                command,
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                env=restricted_env,
                check=False
            )
            duration = time.time() - start_t
            return {
                "sandbox_id": sandbox_id,
                "exit_code": res.returncode,
                "stdout": res.stdout,
                "stderr": res.stderr,
                "duration_seconds": duration,
                "status": "COMPLETED"
            }
        except subprocess.TimeoutExpired:
            return {
                "sandbox_id": sandbox_id,
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout_sec} seconds",
                "duration_seconds": timeout_sec,
                "status": "TIMEOUT"
            }
        finally:
            self.set_status(sandbox_id, SandboxState.READY)

    def cleanup_orphans(self) -> int:
        now = time.time()
        cleaned = 0
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, storage_path FROM sandboxes WHERE expires_at < ? AND status != 'DESTROYED'",
                (now,)
            ).fetchall()
            for r in rows:
                conn.execute("UPDATE sandboxes SET status = 'DESTROYED' WHERE id = ?", (r['id'],))
                shutil.rmtree(r['storage_path'], ignore_errors=True)
                cleaned += 1
        return cleaned
