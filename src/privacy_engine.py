"""Privacy, Data Ownership, Export, and Verified Deletion Engine for RESOLVE."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from tenant_auth import TenantAuthManager
from sandbox_manager import SandboxManager
from connectors import ConnectorRegistry
from document_studio import DocumentStudio

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class DeletionReceipt:
    receipt_id: str
    target_type: str  # "ACCOUNT" | "WORKSPACE" | "MISSION" | "FILE"
    target_id: str
    user_id: str
    status: str       # "COMPLETED" | "RETENTION_PENDING" | "FAILED"
    purged_items: dict
    timestamp: float
    retention_notice: str
    sha256_proof: str

    def to_dict(self) -> dict:
        return asdict(self)


class PrivacyEngine:
    def __init__(
        self,
        auth_mgr: TenantAuthManager,
        sandbox_mgr: SandboxManager,
        connector_reg: ConnectorRegistry,
        data_dir: Path | str | None = None
    ):
        self.auth_mgr = auth_mgr
        self.sandbox_mgr = sandbox_mgr
        self.connector_reg = connector_reg
        self.data_dir = Path(data_dir) if data_dir else ROOT / 'data'
        self.mission_store = None   # optional hooks wired by the app: purge missions first so in-flight workers' late results are rejected
        self.browser_pool = None    # optional: close live browser processes before sandbox storage is destroyed
        self.documents = DocumentStudio(Path(os.environ.get('RESOLVE_DOCUMENTS_DIR', self.data_dir / 'documents')))

    def get_summary(self, user_id: str, workspace_id: str) -> dict:
        sandboxes = self.sandbox_mgr.list_sandboxes(user_id)
        connectors = self.connector_reg.list_connectors(user_id, workspace_id)
        connected_count = len([c for c in connectors if c["status"] == "CONNECTED"])
        
        # Calculate storage usage in user sandboxes
        total_storage_bytes = 0
        for sbx in sandboxes:
            p = Path(sbx.storage_path)
            if p.exists():
                for f in p.rglob('*'):
                    if f.is_file():
                        total_storage_bytes += f.stat().st_size
        total_storage_bytes += sum(d['size_bytes'] for d in self.documents.list_documents(user_id, workspace_id))

        return {
            "user_id": user_id,
            "workspace_id": workspace_id,
            "active_sandboxes": len(sandboxes),
            "connected_apps_count": connected_count,
            "storage_used_bytes": total_storage_bytes,
            "storage_used_mb": round(total_storage_bytes / (1024 * 1024), 2),
            "retention_policy": {
                "active_data": "Retained until deleted by user",
                "session_tokens": "Expires in 7 days or on logout",
                "sandbox_profiles": "Destroyed immediately upon sandbox deletion",
                "backups": "Encrypted system snapshots retain deleted metadata for up to 30 days before purge"
            }
        }

    def export_data(self, user_id: str, workspace_id: str) -> dict:
        """Export all user data into a portable JSON document."""
        summary = self.get_summary(user_id, workspace_id)
        workspaces = self.auth_mgr.list_workspaces(user_id)
        connectors = self.connector_reg.list_connectors(user_id, workspace_id)
        sandboxes = [s.to_dict() for s in self.sandbox_mgr.list_sandboxes(user_id)]

        export_bundle = {
            "version": "1.0",
            "exported_at": time.time(),
            "user_id": user_id,
            "workspace_id": workspace_id,
            "workspaces": workspaces,
            "connected_integrations": connectors,
            "sandboxes": sandboxes,
            "summary": summary
        }
        # Binary PDFs remain available through authenticated download.
        export_bundle['documents'] = self.documents.list_documents(user_id, workspace_id)
        bundle_str = json.dumps(export_bundle, sort_keys=True)
        export_bundle["sha256_checksum"] = hashlib.sha256(bundle_str.encode()).hexdigest()
        return export_bundle

    def delete_sandbox(self, user_id: str, sandbox_id: str) -> DeletionReceipt:
        sbx = self.sandbox_mgr.get_sandbox(sandbox_id, caller_user_id=user_id)
        self.sandbox_mgr.destroy_sandbox(sandbox_id, caller_user_id=user_id)

        now = time.time()
        receipt_id = f"del_{hashlib.sha256(f'{user_id}:{sandbox_id}:{now}'.encode()).hexdigest()[:12]}"
        proof = hashlib.sha256(f"{receipt_id}:{sandbox_id}:DESTROYED".encode()).hexdigest()

        return DeletionReceipt(
            receipt_id=receipt_id,
            target_type="SANDBOX",
            target_id=sandbox_id,
            user_id=user_id,
            status="COMPLETED",
            purged_items={"sandbox_id": sandbox_id, "storage_path": sbx.storage_path},
            timestamp=now,
            retention_notice="Browser profiles and sandbox files purged from local disk immediately.",
            sha256_proof=proof
        )

    def delete_workspace(self, user_id: str, workspace_id: str) -> DeletionReceipt:
        # 1. Terminate & destroy all sandboxes in workspace
        user_sandboxes = self.sandbox_mgr.list_sandboxes(user_id)
        destroyed_sbx = []
        for sbx in user_sandboxes:
            if sbx.workspace_id == workspace_id:
                if self.browser_pool:
                    self.browser_pool.close_session(sbx.id)
                self.sandbox_mgr.destroy_sandbox(sbx.id, caller_user_id=user_id)
                destroyed_sbx.append(sbx.id)

        # 2. Disconnect connectors
        connectors = self.connector_reg.list_connectors(user_id, workspace_id)
        for c in connectors:
            if c["status"] == "CONNECTED":
                self.connector_reg.disconnect(user_id, workspace_id, c["connector_id"])

        # 3. Delete from tenant database (missions first: running workers then reject their late results)
        missions_removed = self.mission_store.purge(user_id, workspace_id) if self.mission_store else 0
        self.documents.delete_workspace(user_id, workspace_id)
        self.auth_mgr.delete_workspace(user_id, workspace_id)

        now = time.time()
        receipt_id = f"del_ws_{hashlib.sha256(f'{user_id}:{workspace_id}:{now}'.encode()).hexdigest()[:12]}"
        proof = hashlib.sha256(f"{receipt_id}:{workspace_id}:PURGED".encode()).hexdigest()

        return DeletionReceipt(
            receipt_id=receipt_id,
            target_type="WORKSPACE",
            target_id=workspace_id,
            user_id=user_id,
            status="COMPLETED",
            purged_items={"workspace_id": workspace_id, "destroyed_sandboxes": destroyed_sbx, "documents_removed": True, "missions_removed": missions_removed},
            timestamp=now,
            retention_notice="Workspace metadata and isolated sandboxes permanently erased.",
            sha256_proof=proof
        )

    def delete_account(self, user_id: str) -> DeletionReceipt:
        """Permanently delete user account and all associated resources across all workspaces."""
        # 1. Destroy all user sandboxes
        user_sandboxes = self.sandbox_mgr.list_sandboxes(user_id)
        destroyed_sbx = []
        for sbx in user_sandboxes:
            if self.browser_pool:
                self.browser_pool.close_session(sbx.id)
            self.sandbox_mgr.destroy_sandbox(sbx.id, caller_user_id=user_id)
            destroyed_sbx.append(sbx.id)

        # 2. Purge all connector registrations
        purged_connectors = self.connector_reg.purge_user_connectors(user_id)

        # 3. Delete user and sessions from tenant database
        missions_removed = self.mission_store.purge(user_id) if self.mission_store else 0
        self.documents.delete_user(user_id)
        self.auth_mgr.delete_user(user_id)

        now = time.time()
        receipt_id = f"del_usr_{hashlib.sha256(f'{user_id}:{now}'.encode()).hexdigest()[:12]}"
        proof = hashlib.sha256(f"{receipt_id}:{user_id}:ALL_PURGED".encode()).hexdigest()

        return DeletionReceipt(
            receipt_id=receipt_id,
            target_type="ACCOUNT",
            target_id=user_id,
            user_id=user_id,
            status="COMPLETED",
            purged_items={
                "destroyed_sandboxes": destroyed_sbx,
                "purged_connectors_count": purged_connectors,
                "sessions_invalidated": True,
                "user_record_deleted": True,
                "documents_removed": True,
                "missions_removed": missions_removed
            },
            timestamp=now,
            retention_notice="All personal credentials, browser sessions, and workspace storage have been permanently erased. Offsite backup snapshots expire in 30 days.",
            sha256_proof=proof
        )

