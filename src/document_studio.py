"""Tenant-scoped PDF artifacts and verifiable PDF transformations."""
from __future__ import annotations

import hashlib
import io
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path
from functools import wraps
from contextlib import contextmanager

from pypdf import PdfReader, PdfWriter


class DocumentError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def synchronized(fn):
    @wraps(fn)
    def call(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return call


class DocumentStudio:
    MAX_PDF_BYTES = 20 * 1024 * 1024

    def __init__(self, base_dir: str | Path):
        self._lock = threading.RLock()
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir = self.base_dir / "pdfs"
        self.files_dir.mkdir(exist_ok=True)
        self.db_path = self.base_dir / "documents.sqlite3"
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL, name TEXT NOT NULL,
                sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                created_at REAL NOT NULL, source_ids TEXT NOT NULL,
                operation TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS documents_owner ON documents(user_id,workspace_id)")

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
    def _public(row):
        d = dict(row)
        d["kind"] = "pdf"
        return d

    @synchronized
    def list_documents(self, user_id: str, workspace_id: str):
        with self._db() as db:
            rows = db.execute("SELECT * FROM documents WHERE user_id=? AND workspace_id=? ORDER BY created_at DESC",
                              (user_id, workspace_id)).fetchall()
        return [self._public(r) for r in rows]

    @synchronized
    def get_document(self, user_id: str, workspace_id: str, document_id: str):
        with self._db() as db:
            row = db.execute("SELECT * FROM documents WHERE id=? AND user_id=? AND workspace_id=?",
                             (document_id, user_id, workspace_id)).fetchone()
        if row is None:
            raise DocumentError("Document not found", 404)
        return self._public(row)

    @synchronized
    def read_pdf(self, user_id: str, workspace_id: str, document_id: str) -> bytes:
        doc = self.get_document(user_id, workspace_id, document_id)
        data = (self.files_dir / f"{doc['id']}.pdf").read_bytes()
        if hashlib.sha256(data).hexdigest() != doc["sha256"]:
            raise DocumentError("Document changed since storage; refusing to use it", 409)
        return data

    def _save(self, user_id: str, workspace_id: str, data: bytes, name: str,
              source_ids: str = "", operation: str = "upload"):
        if not user_id or not workspace_id:
            raise DocumentError("Authenticated user and workspace required")
        if not isinstance(data, bytes) or not data.startswith(b"%PDF-") or len(data) > self.MAX_PDF_BYTES:
            raise DocumentError("Expected a PDF of at most 20 MiB")
        try:
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted or len(reader.pages) == 0:
                raise DocumentError("Encrypted or empty PDFs are unsupported")
            len(reader.pages)
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError("Invalid PDF") from exc
        clean_name = Path(name.replace("\\", "/")).name[:120]
        if not clean_name or clean_name in (".", ".."):
            clean_name = "document.pdf"
        if not clean_name.lower().endswith(".pdf"):
            clean_name += ".pdf"
        doc_id = uuid.uuid4().hex
        dest = self.files_dir / f"{doc_id}.pdf"
        fd, temp_name = tempfile.mkstemp(dir=self.files_dir, prefix=".pdf-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp_name, dest)
            with self._db() as db:
                db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?)",
                           (doc_id, user_id, workspace_id, clean_name,
                            hashlib.sha256(data).hexdigest(), len(data), time.time(),
                            source_ids, operation))
        except Exception:
            dest.unlink(missing_ok=True)
            raise
        finally:
            Path(temp_name).unlink(missing_ok=True)
        return self.get_document(user_id, workspace_id, doc_id)

    @synchronized
    def import_pdf(self, user_id: str, workspace_id: str, pdf_bytes: bytes, name: str):
        return self._save(user_id, workspace_id, pdf_bytes, name)

    @synchronized
    def transform(self, user_id: str, workspace_id: str, operation: str,
                  document_ids: list[str], expected_sha256: dict[str, str],
                  degrees: int | None = None, pages: list[int] | None = None):
        if operation not in {"rotate", "merge", "delete_pages"}:
            raise DocumentError("Unsupported PDF operation")
        if not document_ids or len(document_ids) > 20 or len(document_ids) != len(set(document_ids)):
            raise DocumentError("Provide 1 to 20 distinct documents")
        if operation == "merge" and len(document_ids) < 2:
            raise DocumentError("Merge requires at least two documents")
        if operation != "merge" and len(document_ids) != 1:
            raise DocumentError("This operation requires exactly one document")
        source_data = []
        for doc_id in document_ids:
            doc = self.get_document(user_id, workspace_id, doc_id)
            if expected_sha256.get(doc_id) != doc["sha256"]:
                raise DocumentError("Source version changed or hash was not supplied", 409)
            source_data.append((doc, self.read_pdf(user_id, workspace_id, doc_id)))
        writer = PdfWriter()
        if operation == "merge":
            for _, data in source_data:
                reader = PdfReader(io.BytesIO(data))
                for page in reader.pages:
                    writer.add_page(page)
        else:
            reader = PdfReader(io.BytesIO(source_data[0][1]))
            count = len(reader.pages)
            if operation == "rotate":
                if degrees not in (90, 180, 270):
                    raise DocumentError("Rotation must be 90, 180, or 270 degrees")
                targets = set(pages or range(1, count + 1))
            else:
                targets = set(pages or [])
                if not targets or len(targets) == count:
                    raise DocumentError("Select some pages, but leave at least one")
            if any(type(n) is not int or n < 1 or n > count for n in targets):
                raise DocumentError("Page numbers must be within the PDF")
            for index, page in enumerate(reader.pages, 1):
                if operation == "rotate" and index in targets:
                    page.rotate(degrees)
                if operation == "delete_pages" and index in targets:
                    continue
                writer.add_page(page)
        buf = io.BytesIO()
        writer.write(buf)
        base_name = source_data[0][0]["name"].removesuffix(".pdf")
        name = f"{base_name}-{operation}.pdf"
        return self._save(user_id, workspace_id, buf.getvalue(), name,
                          source_ids=",".join(document_ids), operation=operation)

    @synchronized
    def create_report(self, user_id: str, workspace_id: str, title: str, text: str):
        """Create a real print-ready PDF from validated plain text."""
        if not title.strip() or not text.strip() or len(text) > 100_000:
            raise DocumentError("A title and up to 100,000 characters of text are required")
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.pdfgen.canvas import Canvas
        except ImportError as exc:
            raise DocumentError("PDF report dependency is unavailable") from exc
        import textwrap
        buf = io.BytesIO()
        canvas = Canvas(buf, pagesize=A4)
        width, height = A4
        y = height - 55
        canvas.setFont("Helvetica-Bold", 16)
        canvas.drawString(50, y, title.strip()[:72])
        y -= 32
        canvas.setFont("Helvetica", 10)
        for raw in text.splitlines():
            for line in textwrap.wrap(raw, width=95) or [""]:
                if y < 55:
                    canvas.showPage()
                    canvas.setFont("Helvetica", 10)
                    y = height - 55
                canvas.drawString(50, y, line.encode("latin-1", "replace").decode("latin-1"))
                y -= 14
        canvas.save()
        return self._save(user_id, workspace_id, buf.getvalue(), title.strip()[:72] + ".pdf",
                          operation="report")

    @synchronized
    def delete_workspace(self, user_id: str, workspace_id: str) -> int:
        """Purge PDF artifacts for one tenant workspace; return removed count."""
        with self._db() as db:
            ids = [row[0] for row in db.execute(
                "SELECT id FROM documents WHERE user_id=? AND workspace_id=?", (user_id, workspace_id))]
            for doc_id in ids:
                (self.files_dir / f"{doc_id}.pdf").unlink(missing_ok=True)
            db.execute("DELETE FROM documents WHERE user_id=? AND workspace_id=?", (user_id, workspace_id))
        return len(ids)

    @synchronized
    def delete_user(self, user_id: str) -> int:
        """Purge PDF artifacts across all workspaces owned by one user."""
        with self._db() as db:
            ids = [row[0] for row in db.execute(
                "SELECT id FROM documents WHERE user_id=?", (user_id,))]
            for doc_id in ids:
                (self.files_dir / f"{doc_id}.pdf").unlink(missing_ok=True)
            db.execute("DELETE FROM documents WHERE user_id=?", (user_id,))
        return len(ids)
