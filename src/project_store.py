"""Tenant-private storage for user-uploaded source projects (zip archives).

Security model
- The archive is stored as an opaque blob and read *in memory* member by member; nothing is ever extracted to disk,
  so zip-slip / symlink / device-file attacks have no filesystem surface.
- Limits: archive size, member count, per-member size, total uncompressed size (zip-bomb guard, enforced both from
  headers and while actually reading), allowed source extensions only, junk directories skipped.
- Content is identified by SHA-256 of the archive; missions bind to that hash.
- Every query is keyed by user_id + workspace_id.
"""
from __future__ import annotations

import hashlib
import io
import sqlite3
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

SOURCE_EXTENSIONS = {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".java", ".rb", ".php"}
SKIP_DIRS = {"node_modules", ".git", "__pycache__", "venv", ".venv", "dist", "build", "vendor", ".next", "target", "site-packages"}
MAX_ARCHIVE_BYTES = 5 * 1024 * 1024
MAX_MEMBERS = 2000
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_SOURCE_FILES = 400
MAX_PROJECTS_PER_TENANT = 20


class ProjectError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message, self.status_code = message, status_code


def fingerprint(files: dict[str, str]) -> str:
    """Content hash of a set of source files (used for bundled projects, which are directories rather than archives)."""
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode() + b"\0" + files[path].encode("utf-8", "replace") + b"\0")
    return h.hexdigest()


def read_archive(raw: bytes) -> dict[str, str]:
    """Validate a zip and return {relative_posix_path: text} for analyzable source files. Never touches the filesystem."""
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ProjectError(f"Archive exceeds {MAX_ARCHIVE_BYTES // (1024 * 1024)} MiB", 413)
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise ProjectError("Not a valid zip archive", 415)
    infos = zf.infolist()
    if len(infos) > MAX_MEMBERS:
        raise ProjectError(f"Archive has {len(infos)} entries (limit {MAX_MEMBERS})", 413)
    if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
        raise ProjectError("Archive expands beyond the allowed size (possible zip bomb)", 413)
    files: dict[str, str] = {}
    budget = MAX_TOTAL_BYTES
    for info in infos:
        if info.is_dir() or (info.external_attr >> 16) & 0o170000 == 0o120000:   # skip directories and symlinks
            continue
        name = info.filename.replace("\\", "/")
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or ":" in pure.parts[0]:
            raise ProjectError(f"Unsafe path in archive: {name[:80]}", 400)
        if any(part in SKIP_DIRS or part.startswith(".") and part not in (".",) for part in pure.parts[:-1]):
            continue
        if pure.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        if info.file_size > MAX_FILE_BYTES:
            continue                       # oversized generated/minified file: skipped, not fatal
        with zf.open(info) as fh:
            data = fh.read(MAX_FILE_BYTES + 1)   # bounded read: header sizes can lie
        budget -= len(data)
        if len(data) > MAX_FILE_BYTES or budget < 0:
            raise ProjectError("Archive member exceeds its declared size (possible zip bomb)", 413)
        files[pure.as_posix()] = data.decode("utf-8", "replace")
        if len(files) > MAX_SOURCE_FILES:
            raise ProjectError(f"More than {MAX_SOURCE_FILES} source files; upload a smaller project", 413)
    if not files:
        raise ProjectError("No analyzable source files found (supported: " + ", ".join(sorted(SOURCE_EXTENSIONS)) + ")", 422)
    return files


class ProjectStore:
    def __init__(self, base_dir: str | Path):
        self.base_dir = Path(base_dir)
        self.blob_dir = self.base_dir / "projects"
        self.blob_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.base_dir / "projects.sqlite3"
        self._lock = threading.RLock()
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL, name TEXT NOT NULL,
                sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, files_count INTEGER NOT NULL, created_at REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS projects_owner ON projects(user_id, workspace_id)")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _public(row) -> dict:
        d = dict(row)
        d.pop("user_id", None)
        return d

    def add(self, user_id: str, workspace_id: str, name: str, raw: bytes) -> dict:
        files = read_archive(raw)              # validates before anything is stored
        clean = Path(name.replace("\\", "/")).name[:100] or "project.zip"
        with self._lock:
            with self._db() as db:
                if db.execute("SELECT COUNT(*) FROM projects WHERE user_id=?", (user_id,)).fetchone()[0] >= MAX_PROJECTS_PER_TENANT:
                    raise ProjectError("Project quota reached; delete an old project", 429)
            pid = "prj_" + uuid.uuid4().hex[:12]
            (self.blob_dir / f"{pid}.zip").write_bytes(raw)
            with self._db() as db:
                db.execute("INSERT INTO projects VALUES (?,?,?,?,?,?,?,?)",
                           (pid, user_id, workspace_id, clean, hashlib.sha256(raw).hexdigest(), len(raw), len(files), time.time()))
        return self.get(user_id, workspace_id, pid)

    def get(self, user_id: str, workspace_id: str, project_id: str) -> dict:
        with self._db() as db:
            row = db.execute("SELECT * FROM projects WHERE id=? AND user_id=? AND workspace_id=?", (project_id, user_id, workspace_id)).fetchone()
        if row is None:
            raise ProjectError("Project not found", 404)
        return self._public(row)

    def list(self, user_id: str, workspace_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM projects WHERE user_id=? AND workspace_id=? ORDER BY created_at DESC", (user_id, workspace_id)).fetchall()
        return [self._public(r) for r in rows]

    def load(self, user_id: str, workspace_id: str, project_id: str) -> tuple[dict, dict[str, str]]:
        """Return (metadata, files) after re-verifying the stored blob against its recorded hash."""
        meta = self.get(user_id, workspace_id, project_id)
        raw = (self.blob_dir / f"{project_id}.zip").read_bytes()
        if hashlib.sha256(raw).hexdigest() != meta["sha256"]:
            raise ProjectError("Stored project changed since upload; refusing to analyze it", 409)
        return meta, read_archive(raw)

    def delete(self, user_id: str, workspace_id: str, project_id: str) -> bool:
        self.get(user_id, workspace_id, project_id)
        with self._lock:
            (self.blob_dir / f"{project_id}.zip").unlink(missing_ok=True)
            with self._db() as db:
                db.execute("DELETE FROM projects WHERE id=? AND user_id=? AND workspace_id=?", (project_id, user_id, workspace_id))
        return True

    def purge(self, user_id: str, workspace_id: str | None = None) -> int:
        q, args = ("user_id=?", (user_id,)) if workspace_id is None else ("user_id=? AND workspace_id=?", (user_id, workspace_id))
        with self._lock, self._db() as db:
            ids = [r[0] for r in db.execute(f"SELECT id FROM projects WHERE {q}", args)]
            for i in ids:
                (self.blob_dir / f"{i}.zip").unlink(missing_ok=True)
            db.execute(f"DELETE FROM projects WHERE {q}", args)
        return len(ids)
