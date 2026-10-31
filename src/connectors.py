"""Authorized Personal Connections Framework for RESOLVE cloud multi-tenancy."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import time
from abc import ABC, abstractmethod
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = Path(os.environ.get('RESOLVE_CONNECTORS_DB', ROOT / 'data' / 'connectors.sqlite3'))


class ConnectorError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class BaseConnector(ABC):
    connector_id: str
    display_name: str
    available_scopes: list[str]

    def __init__(self, user_id: str, workspace_id: str, granted_scopes: list[str] | None = None, connected_at: float | None = None):
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.granted_scopes = granted_scopes or []
        self.connected_at = connected_at or time.time()
        self.status = "CONNECTED" if self.granted_scopes else "DISCONNECTED"

    def has_scope(self, scope: str) -> bool:
        return scope in self.granted_scopes

    def require_scope(self, scope: str) -> None:
        if not self.has_scope(scope):
            raise ConnectorError(f"Missing required scope {scope!r} for connector {self.connector_id}", status_code=403)

    @abstractmethod
    def list_authorized_resources(self) -> list[dict]:
        """List resources accessible under currently granted scopes."""
        pass

    @abstractmethod
    def execute_authorized_operation(self, operation: str, params: dict) -> dict:
        """Execute a permitted operation."""
        pass

    def get_connection_status(self) -> dict:
        return {
            "connector_id": self.connector_id,
            "display_name": self.display_name,
            "user_id": self.user_id,
            "workspace_id": self.workspace_id,
            "status": self.status,
            "granted_scopes": self.granted_scopes,
            "connected_at": self.connected_at
        }


class CloudDriveConnector(BaseConnector):
    connector_id = "google_drive"
    display_name = "Google Drive & Cloud Documents"
    available_scopes = ["drive.readonly", "drive.file"]

    def __init__(self, user_id: str, workspace_id: str, granted_scopes: list[str] | None = None, connected_at: float | None = None):
        super().__init__(user_id, workspace_id, granted_scopes, connected_at)
        # Sandbox simulated virtual drive storage
        self._mock_files = {
            "file_q3_report": {"id": "file_q3_report", "name": "Q3_Financial_Summary.pdf", "mime": "application/pdf", "size_kb": 240, "sha256": "4b92b3b7c89f", "modified_at": "2026-10-25T14:30:00Z"},
            "file_specs": {"id": "file_specs", "name": "Q4_Platform_Roadmap.docx", "mime": "application/vnd.word", "size_kb": 115, "sha256": "8f12a9bc4310", "modified_at": "2026-10-27T09:15:00Z"},
            "file_customer_list": {"id": "file_customer_list", "name": "Target_Accounts.csv", "mime": "text/csv", "size_kb": 45, "sha256": "cd91283e74ab", "modified_at": "2026-10-28T18:00:00Z"}
        }

    def list_authorized_resources(self) -> list[dict]:
        self.require_scope("drive.readonly")
        return list(self._mock_files.values())

    def execute_authorized_operation(self, operation: str, params: dict) -> dict:
        if operation == "read_file":
            self.require_scope("drive.readonly")
            file_id = params.get("file_id")
            if file_id not in self._mock_files:
                raise ConnectorError(f"File {file_id} not found in connected drive", status_code=404)
            meta = self._mock_files[file_id]
            return {
                "file": meta,
                "content_preview": f"Simulated content stream for {meta['name']} (SHA-256: {meta['sha256']})",
                "verified": True
            }
        elif operation == "create_file":
            self.require_scope("drive.file")
            name = params.get("name", "untitled.txt")
            content = params.get("content", "")
            fid = f"file_{hashlib.sha256(name.encode()).hexdigest()[:8]}"
            new_file = {
                "id": fid,
                "name": name,
                "mime": "text/plain",
                "size_kb": len(content.encode()) // 1024 or 1,
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "modified_at": "2026-10-29T00:00:00Z"
            }
            self._mock_files[fid] = new_file
            return {"file": new_file, "status": "CREATED"}
        else:
            raise ConnectorError(f"Unsupported drive operation {operation}")


class GitHubConnector(BaseConnector):
    connector_id = "github"
    display_name = "GitHub Repositories"
    available_scopes = ["repo:read", "repo:status"]

    def list_authorized_resources(self) -> list[dict]:
        self.require_scope("repo:read")
        return [
            {"repo": "victrvondoom/Resolve", "branch": "master", "head_sha": "69f177b", "dirty": False, "open_issues": 0},
            {"repo": "victrvondoom/Proxy", "branch": "main", "head_sha": "8f32bc1", "dirty": False, "open_issues": 2}
        ]

    def execute_authorized_operation(self, operation: str, params: dict) -> dict:
        if operation == "get_repo_status":
            self.require_scope("repo:read")
            repo = params.get("repo", "victrvondoom/Resolve")
            return {
                "repo": repo,
                "branch": "master",
                "status": "CLEAN",
                "latest_commit": "Pass Gates G0-G9 baseline",
                "verified": True
            }
        elif operation == "create_status_check":
            self.require_scope("repo:status")
            return {"status": "SUCCESS", "context": "resolve/verify-continuity", "description": "All IEEC invariants satisfied"}
        else:
            raise ConnectorError(f"Unsupported GitHub operation {operation}")


class CRMConnector(BaseConnector):
    connector_id = "crm"
    display_name = "CRM & Customer Directory"
    available_scopes = ["crm:read", "crm:write"]

    def __init__(self, user_id: str, workspace_id: str, granted_scopes: list[str] | None = None, connected_at: float | None = None):
        super().__init__(user_id, workspace_id, granted_scopes, connected_at)
        self._contacts = [
            {"id": "alex-sales-01", "name": "Alex Morgan", "department": "Sales", "email": "alex.sales@northwind.local"},
            {"id": "alex-fin-02", "name": "Alex Morgan", "department": "Finance", "email": "alex.finance@northwind.local"},
            {"id": "jordan-lee-eng", "name": "Jordan Lee", "department": "Engineering", "email": "jordan@northwind.local"}
        ]

    def list_authorized_resources(self) -> list[dict]:
        self.require_scope("crm:read")
        return self._contacts

    def execute_authorized_operation(self, operation: str, params: dict) -> dict:
        if operation == "lookup_contact":
            self.require_scope("crm:read")
            query = params.get("name", "").strip().lower()
            matches = [c for c in self._contacts if query in c["name"].lower()]
            if len(matches) > 1 and not params.get("department") and not params.get("id"):
                raise ConnectorError(f"Ambiguity: {len(matches)} contacts named {query!r}. Department or stable ID required.", status_code=409)
            if params.get("id"):
                matches = [c for c in self._contacts if c["id"] == params["id"]]
            return {"matches": matches, "count": len(matches)}
        elif operation == "update_followup":
            self.require_scope("crm:write")
            contact_id = params.get("contact_id")
            if not contact_id:
                raise ConnectorError("contact_id required for followup update")
            return {"status": "UPDATED", "contact_id": contact_id, "followup_version": 4, "verified": True}
        else:
            raise ConnectorError(f"Unsupported CRM operation {operation}")


class ConnectorRegistry:
    """Manages multi-tenant authorized connectors in SQLite."""
    CONNECTOR_CLASSES = {
        "google_drive": CloudDriveConnector,
        "github": GitHubConnector,
        "crm": CRMConnector
    }

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
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
            CREATE TABLE IF NOT EXISTS tenant_connectors (
                user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                connector_id TEXT NOT NULL,
                granted_scopes TEXT NOT NULL,
                connected_at REAL NOT NULL,
                PRIMARY KEY (user_id, workspace_id, connector_id)
            );
            """)

    def connect(self, user_id: str, workspace_id: str, connector_id: str, scopes: list[str]) -> BaseConnector:
        if connector_id not in self.CONNECTOR_CLASSES:
            raise ConnectorError(f"Unknown connector type: {connector_id}", status_code=404)
        cls = self.CONNECTOR_CLASSES[connector_id]
        invalid_scopes = set(scopes) - set(cls.available_scopes)
        if invalid_scopes:
            raise ConnectorError(f"Invalid scopes for {connector_id}: {invalid_scopes}", status_code=400)

        now = time.time()
        scopes_json = json.dumps(scopes)
        with self._conn() as conn:
            conn.execute("""
            INSERT INTO tenant_connectors (user_id, workspace_id, connector_id, granted_scopes, connected_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, workspace_id, connector_id) DO UPDATE SET
                granted_scopes = excluded.granted_scopes,
                connected_at = excluded.connected_at
            """, (user_id, workspace_id, connector_id, scopes_json, now))

        return cls(user_id, workspace_id, granted_scopes=scopes, connected_at=now)

    def get_connector(self, user_id: str, workspace_id: str, connector_id: str) -> BaseConnector:
        if connector_id not in self.CONNECTOR_CLASSES:
            raise ConnectorError(f"Unknown connector: {connector_id}", status_code=404)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM tenant_connectors WHERE user_id = ? AND workspace_id = ? AND connector_id = ?",
                (user_id, workspace_id, connector_id)
            ).fetchone()
            if not row:
                raise ConnectorError(f"Connector {connector_id} not connected for this workspace", status_code=404)

        cls = self.CONNECTOR_CLASSES[connector_id]
        return cls(user_id, workspace_id, granted_scopes=json.loads(row['granted_scopes']), connected_at=row['connected_at'])

    def list_connectors(self, user_id: str, workspace_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM tenant_connectors WHERE user_id = ? AND workspace_id = ?",
                (user_id, workspace_id)
            ).fetchall()
            connected_map = {r['connector_id']: r for r in rows}

        results = []
        for cid, cls in self.CONNECTOR_CLASSES.items():
            if cid in connected_map:
                r = connected_map[cid]
                results.append({
                    "connector_id": cid,
                    "display_name": cls.display_name,
                    "status": "CONNECTED",
                    "available_scopes": cls.available_scopes,
                    "granted_scopes": json.loads(r['granted_scopes']),
                    "connected_at": r['connected_at']
                })
            else:
                results.append({
                    "connector_id": cid,
                    "display_name": cls.display_name,
                    "status": "DISCONNECTED",
                    "available_scopes": cls.available_scopes,
                    "granted_scopes": [],
                    "connected_at": None
                })
        return results

    def disconnect(self, user_id: str, workspace_id: str, connector_id: str) -> bool:
        with self._conn() as conn:
            res = conn.execute(
                "DELETE FROM tenant_connectors WHERE user_id = ? AND workspace_id = ? AND connector_id = ?",
                (user_id, workspace_id, connector_id)
            )
            return res.rowcount > 0

    def purge_user_connectors(self, user_id: str) -> int:
        with self._conn() as conn:
            res = conn.execute("DELETE FROM tenant_connectors WHERE user_id = ?", (user_id,))
            return res.rowcount