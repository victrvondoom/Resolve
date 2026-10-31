"""Tenant-scoped PDF artifacts and verifiable PDF transformations."""
from __future__ import annotations

import difflib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
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


PAPER_SIZES = {"A4": (595.28, 841.89), "Letter": (612.0, 792.0)}   # PDF points, portrait
ORIENTATIONS = ("portrait", "landscape")

# Print status model. A browser print dialog is NOT proof of printing: the strongest state the browser path can
# reach is SUBMITTED, and that is only the user's own attestation. PRINTER_CONFIRMED needs a printer-side
# confirmation channel (for example an IPP job-state read-back). This deployment has none, so it is never set.
PRINT_STATES = ("PREVIEW_READY", "ARTIFACT_GENERATED", "PRINT_DIALOG_OPENED", "SUBMITTED",
                "PRINTER_CONFIRMED", "FAILED", "UNKNOWN")
PRINT_TRANSITIONS = {
    "PREVIEW_READY": {"ARTIFACT_GENERATED", "FAILED"},
    "ARTIFACT_GENERATED": {"PRINT_DIALOG_OPENED", "FAILED"},
    "PRINT_DIALOG_OPENED": {"PRINT_DIALOG_OPENED", "SUBMITTED", "UNKNOWN", "FAILED"},
    "SUBMITTED": {"PRINT_DIALOG_OPENED", "PRINTER_CONFIRMED", "UNKNOWN", "FAILED"},
    "UNKNOWN": {"PRINT_DIALOG_OPENED", "SUBMITTED", "PRINTER_CONFIRMED", "FAILED"},
    "PRINTER_CONFIRMED": set(),
    "FAILED": set(),
}
# Who causes each transition. The caller never chooses the source: it follows from the status.
PRINT_STATUS_SOURCE = {"ARTIFACT_GENERATED": "server", "PRINT_DIALOG_OPENED": "browser", "UNKNOWN": "browser",
                       "FAILED": "browser", "SUBMITTED": "user_attested"}
PRINT_NOTE = ("A browser print dialog is not proof of printing. RESOLVE records what the browser and the user report; "
              "PRINTER_CONFIRMED requires a printer-side confirmation channel, and none is configured.")
THUMBNAIL_RENDERER = "poppler pdftoppm"


def parse_page_ranges(spec, count: int) -> list[int]:
    """Parse a one-based page selection such as "1-3,5" against a page count. The result is in document order;
    duplicates, overlaps, reversed ranges and out-of-range pages are rejected."""
    if not isinstance(spec, str) or not spec.strip() or len(spec) > 200:
        raise DocumentError("Give a page range such as 1-3,5")
    pages: list[int] = []
    for token in spec.replace(" ", "").split(","):
        m = re.fullmatch(r"(\d{1,5})(?:-(\d{1,5}))?", token)
        if not m:
            raise DocumentError(f"Invalid page range part: {token[:20]!r}")
        start, end = int(m.group(1)), int(m.group(2) or m.group(1))
        if start < 1 or end < start or end > count:
            raise DocumentError(f"Page range {token} is outside 1-{count}")
        pages.extend(range(start, end + 1))
    if len(pages) != len(set(pages)):
        raise DocumentError("Page range lists a page more than once")
    return sorted(pages)


def paper_of(width: float, height: float):
    """Name the standard paper size a page box matches (either orientation, 2 pt tolerance), else None."""
    for name, (w, h) in PAPER_SIZES.items():
        if (abs(width - w) <= 2 and abs(height - h) <= 2) or (abs(width - h) <= 2 and abs(height - w) <= 2):
            return name
    return None


def synchronized(fn):
    @wraps(fn)
    def call(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return call


class DocumentStudio:
    MAX_PDF_BYTES = 20 * 1024 * 1024
    MAX_TEXT_PAGES = 100   # bound parser work when extracting text

    def __init__(self, base_dir: str | Path, sealer=None):
        self._lock = threading.RLock()
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir = self.base_dir / "pdfs"
        self.files_dir.mkdir(exist_ok=True)
        # encryption at rest: every PDF file is sealed per user/workspace (see src/at_rest.py)
        from at_rest import SealedStore
        self.sealer = sealer or SealedStore(self.base_dir / "keys" / "master.key")
        self.db_path = self.base_dir / "documents.sqlite3"
        with self._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                workspace_id TEXT NOT NULL, name TEXT NOT NULL,
                sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                created_at REAL NOT NULL, source_ids TEXT NOT NULL,
                operation TEXT NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS documents_owner ON documents(user_id,workspace_id)")
            cols = {r[1] for r in db.execute("PRAGMA table_info(documents)")}
            if "source_sha256" not in cols:   # migration: bind each derived version to the exact source hashes
                db.execute("ALTER TABLE documents ADD COLUMN source_sha256 TEXT NOT NULL DEFAULT ''")
            db.execute("""CREATE TABLE IF NOT EXISTS print_jobs (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
                document_id TEXT NOT NULL, document_sha256 TEXT NOT NULL,
                paper TEXT NOT NULL, orientation TEXT NOT NULL, page_range TEXT NOT NULL,
                pages_json TEXT NOT NULL, status TEXT NOT NULL,
                artifact_id TEXT, artifact_sha256 TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS print_jobs_owner ON print_jobs(user_id,workspace_id,document_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS print_events (
                job_id TEXT NOT NULL, seq INTEGER NOT NULL, from_status TEXT, to_status TEXT NOT NULL,
                source TEXT NOT NULL, note TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY (job_id, seq))""")

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
    def seal_legacy_files(self) -> int:
        """One-time migration: encrypt PDFs stored before encryption at rest existed. Returns how many were sealed."""
        sealed = 0
        with self._db() as db:
            rows = db.execute("SELECT id, user_id, workspace_id, sha256 FROM documents").fetchall()
        for r in rows:
            path = self.files_dir / f"{r['id']}.pdf"
            if not path.exists():
                continue
            raw = path.read_bytes()
            if self.sealer.is_sealed(raw):
                continue
            if hashlib.sha256(raw).hexdigest() != r["sha256"]:
                continue                      # do not seal a file that no longer matches its record
            fd, tmp = tempfile.mkstemp(dir=self.files_dir, prefix=".seal-", suffix=".tmp")
            with os.fdopen(fd, "wb") as out:
                out.write(self.sealer.seal(raw, r["user_id"], r["workspace_id"], r["id"]))
                out.flush(); os.fsync(out.fileno())
            os.replace(tmp, path)
            sealed += 1
        return sealed

    def read_pdf(self, user_id: str, workspace_id: str, document_id: str) -> bytes:
        doc = self.get_document(user_id, workspace_id, document_id)
        raw = (self.files_dir / f"{doc['id']}.pdf").read_bytes()
        data = self.sealer.open(raw, user_id, workspace_id, doc["id"]) if self.sealer.is_sealed(raw) else raw
        if hashlib.sha256(data).hexdigest() != doc["sha256"]:
            raise DocumentError("Document changed since storage; refusing to use it", 409)
        return data

    def _save(self, user_id: str, workspace_id: str, data: bytes, name: str,
              source_ids: str = "", operation: str = "upload", source_sha256: str = ""):
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
                out.write(self.sealer.seal(data, user_id, workspace_id, doc_id))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temp_name, dest)
            with self._db() as db:
                db.execute("INSERT INTO documents (id,user_id,workspace_id,name,sha256,size_bytes,created_at,"
                           "source_ids,operation,source_sha256) VALUES (?,?,?,?,?,?,?,?,?,?)",
                           (doc_id, user_id, workspace_id, clean_name,
                            hashlib.sha256(data).hexdigest(), len(data), time.time(),
                            source_ids, operation, source_sha256))
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
                  degrees: int | None = None, pages: list[int] | None = None,
                  order: list[int] | None = None, ranges: str | None = None):
        """Every operation writes a NEW immutable version; a source is never overwritten. Each source must be
        named with its current SHA-256 (a stale or missing hash is rejected with 409) and the new version
        records the exact source hashes it was derived from."""
        if operation not in {"rotate", "merge", "delete_pages", "reorder", "extract"}:
            raise DocumentError("Unsupported PDF operation")
        if not isinstance(document_ids, list) or not all(isinstance(i, str) for i in document_ids):
            raise DocumentError("document_ids must be a list of ids")
        if not isinstance(expected_sha256, dict):
            raise DocumentError("Source version changed or hash was not supplied", 409)
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
        elif operation in ("reorder", "extract"):
            reader = PdfReader(io.BytesIO(source_data[0][1]))
            count = len(reader.pages)
            if operation == "reorder":
                if (not isinstance(order, list) or any(type(n) is not int for n in order)
                        or sorted(order) != list(range(1, count + 1))):
                    raise DocumentError(f"Order must list every page 1-{count} exactly once")
                if order == list(range(1, count + 1)):
                    raise DocumentError("The new order is the same as the current order")
                chosen = order
            else:
                chosen = parse_page_ranges(ranges, count)
                if len(chosen) == count:
                    raise DocumentError("The range selects every page; nothing to extract")
            for n in chosen:
                writer.add_page(reader.pages[n - 1])
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
                          source_ids=",".join(document_ids), operation=operation,
                          source_sha256=",".join(d["sha256"] for d, _ in source_data))

    # ---- page metadata, thumbnails and comparison (read-only) ------------------------------------------------
    @synchronized
    def page_info(self, user_id: str, workspace_id: str, document_id: str) -> dict:
        """Real per-page values read from the stored PDF: box size in points, /Rotate, effective orientation."""
        doc = self.get_document(user_id, workspace_id, document_id)
        reader = PdfReader(io.BytesIO(self.read_pdf(user_id, workspace_id, document_id)))
        pages = []
        for n, page in enumerate(reader.pages, 1):
            box = page.mediabox
            w, h = round(float(box.width), 2), round(float(box.height), 2)
            rot = int(page.rotation or 0) % 360
            ew, eh = (h, w) if rot in (90, 270) else (w, h)
            pages.append({"page": n, "width_pt": w, "height_pt": h, "rotation": rot,
                          "orientation": "landscape" if ew > eh else "portrait", "paper": paper_of(w, h)})
        renderer = shutil.which("pdftoppm")
        return {"document_id": doc["id"], "sha256": doc["sha256"], "page_count": len(pages), "pages": pages,
                "thumbnails": {"available": bool(renderer), "renderer": THUMBNAIL_RENDERER if renderer else None,
                               "reason": None if renderer else
                               "No PDF rasterizer is installed (pypdfium2 and poppler pdftoppm are absent); "
                               "page metadata is still read from the PDF, but no images are rendered."}}

    def render_thumbnail(self, user_id: str, workspace_id: str, document_id: str, page_no: int, width: int = 160) -> bytes:
        """Render one page to PNG with poppler's pdftoppm when it is installed; never fabricate an image."""
        renderer = shutil.which("pdftoppm")
        if not renderer:
            raise DocumentError("Thumbnail rendering is unavailable: no PDF rasterizer (pdftoppm) is installed", 501)
        data = self.read_pdf(user_id, workspace_id, document_id)
        count = len(PdfReader(io.BytesIO(data)).pages)
        if type(page_no) is not int or not 1 <= page_no <= count:
            raise DocumentError(f"Page must be within 1-{count}", 404)
        width = max(48, min(int(width), 600))
        try:
            out = subprocess.run([renderer, "-png", "-f", str(page_no), "-l", str(page_no), "-scale-to", str(width),
                                  "-singlefile", "-"], input=data, capture_output=True, timeout=20, check=True).stdout
        except (subprocess.SubprocessError, OSError) as exc:
            raise DocumentError("Thumbnail rendering failed", 422) from exc
        if not out.startswith(b"\x89PNG\r\n\x1a\n"):
            raise DocumentError("Thumbnail rendering failed", 422)
        return out

    def _page_texts(self, user_id: str, workspace_id: str, document_id: str):
        reader = PdfReader(io.BytesIO(self.read_pdf(user_id, workspace_id, document_id)))
        if len(reader.pages) > self.MAX_TEXT_PAGES:
            raise DocumentError(f"Document has {len(reader.pages)} pages; comparison is limited to {self.MAX_TEXT_PAGES}", 413)
        return [((p.extract_text() or "").strip(), int(p.rotation or 0) % 360) for p in reader.pages]

    @synchronized
    def compare(self, user_id: str, workspace_id: str, a_id: str, b_id: str, expected_sha256: dict | None = None) -> dict:
        """Per-page text diff (pypdf extraction) between two documents of the same tenant, with both hashes."""
        if not isinstance(a_id, str) or not isinstance(b_id, str) or not a_id or not b_id:
            raise DocumentError("Provide two document ids, a and b")
        if a_id == b_id:
            raise DocumentError("Choose two different documents to compare")
        docs = {i: self.get_document(user_id, workspace_id, i) for i in (a_id, b_id)}
        if expected_sha256 is not None:
            if not isinstance(expected_sha256, dict) or any(expected_sha256.get(i) != d["sha256"] for i, d in docs.items()):
                raise DocumentError("Source version changed or hash was not supplied", 409)
        ta, tb = self._page_texts(user_id, workspace_id, a_id), self._page_texts(user_id, workspace_id, b_id)
        rows, summary = [], {"SAME": 0, "CHANGED": 0, "ADDED": 0, "REMOVED": 0}
        for n in range(1, max(len(ta), len(tb)) + 1):
            pa, pb = (ta[n - 1] if n <= len(ta) else None), (tb[n - 1] if n <= len(tb) else None)
            if pa is None or pb is None:
                status = "ADDED" if pa is None else "REMOVED"
                lines = (pb or pa)[0].splitlines()
                row = {"page": n, "status": status, "similarity": 0.0,
                       "lines_added": len(lines) if status == "ADDED" else 0,
                       "lines_removed": len(lines) if status == "REMOVED" else 0, "sample": []}
            else:
                la, lb = pa[0].splitlines(), pb[0].splitlines()
                diff = [d for d in difflib.ndiff(la, lb) if d[:2] in ("+ ", "- ")]
                status = "SAME" if pa[0] == pb[0] else "CHANGED"
                row = {"page": n, "status": status,
                       "similarity": round(difflib.SequenceMatcher(None, pa[0], pb[0]).ratio(), 3),
                       "lines_added": sum(d.startswith("+ ") for d in diff),
                       "lines_removed": sum(d.startswith("- ") for d in diff),
                       "sample": [d[:160] for d in diff[:4]],
                       "rotation_a": pa[1], "rotation_b": pb[1]}
            summary[status] += 1
            rows.append(row)
        return {"a": {"id": a_id, "name": docs[a_id]["name"], "sha256": docs[a_id]["sha256"], "page_count": len(ta)},
                "b": {"id": b_id, "name": docs[b_id]["name"], "sha256": docs[b_id]["sha256"], "page_count": len(tb)},
                "identical_bytes": docs[a_id]["sha256"] == docs[b_id]["sha256"],
                "pages": rows, "summary": {k.lower(): v for k, v in summary.items()},
                "compared": "extracted page text (pypdf); layout, fonts and images are not compared"}

    # ---- print jobs: recorded status transitions, never a claim of physical printing -------------------------
    def _print_job_row(self, db, user_id, workspace_id, document_id, job_id):
        row = db.execute("SELECT * FROM print_jobs WHERE id=? AND user_id=? AND workspace_id=? AND document_id=?",
                         (job_id, user_id, workspace_id, document_id)).fetchone()
        if row is None:
            raise DocumentError("Print job not found", 404)
        return row

    @staticmethod
    def _print_event(db, job_id, from_status, to_status, source, note):
        seq = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM print_events WHERE job_id=?", (job_id,)).fetchone()[0]
        db.execute("INSERT INTO print_events VALUES (?,?,?,?,?,?,?)", (job_id, seq, from_status, to_status, source, note, time.time()))

    def _print_job_public(self, db, row) -> dict:
        job = {k: row[k] for k in row.keys() if k not in ("user_id", "workspace_id", "pages_json")}
        job["pages"] = json.loads(row["pages_json"])
        job["events"] = [dict(e) for e in db.execute(
            "SELECT seq, from_status, to_status, source, note, at FROM print_events WHERE job_id=? ORDER BY seq", (row["id"],))]
        job["allowed_next"] = sorted(PRINT_TRANSITIONS[row["status"]] - {"PRINTER_CONFIRMED"})
        job["physical_printing_verified"] = False
        return job

    @synchronized
    def create_print_job(self, user_id: str, workspace_id: str, document_id: str, expected_sha256,
                         paper: str = "A4", orientation: str = "portrait", page_range: str | None = None) -> dict:
        """Record a print job in PREVIEW_READY, bound to the document's exact SHA-256 and paper settings."""
        doc = self.get_document(user_id, workspace_id, document_id)
        if expected_sha256 != doc["sha256"]:
            raise DocumentError("Source version changed or hash was not supplied", 409)
        if paper not in PAPER_SIZES:
            raise DocumentError("Paper must be A4 or Letter")
        if orientation not in ORIENTATIONS:
            raise DocumentError("Orientation must be portrait or landscape")
        count = len(PdfReader(io.BytesIO(self.read_pdf(user_id, workspace_id, document_id))).pages)
        spec = (page_range or "").strip() or (f"1-{count}" if count > 1 else "1")
        pages = parse_page_ranges(spec, count)
        job_id, now = uuid.uuid4().hex, time.time()
        with self._db() as db:
            db.execute("INSERT INTO print_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, user_id, workspace_id, document_id, doc["sha256"], paper, orientation, spec,
                        json.dumps(pages), "PREVIEW_READY", None, None, now, now))
            self._print_event(db, job_id, None, "PREVIEW_READY", "server", f"{paper} {orientation}, pages {spec}")
            return self._print_job_public(db, self._print_job_row(db, user_id, workspace_id, document_id, job_id))

    @synchronized
    def record_print_status(self, user_id: str, workspace_id: str, document_id: str, job_id: str, status: str,
                            artifact_sha256: str | None = None) -> dict:
        """Apply one allowed transition. ARTIFACT_GENERATED is performed by the server (it writes the print-ready
        PDF); PRINT_DIALOG_OPENED must name the artifact hash the browser printed; PRINTER_CONFIRMED is refused."""
        if status not in PRINT_STATES:
            raise DocumentError("Unknown print status")
        self.get_document(user_id, workspace_id, document_id)
        with self._db() as db:
            row = self._print_job_row(db, user_id, workspace_id, document_id, str(job_id))
        if status == "PRINTER_CONFIRMED":
            raise DocumentError("PRINTER_CONFIRMED cannot be set from this path: " + PRINT_NOTE, 403)
        current = row["status"]
        if status not in PRINT_TRANSITIONS[current]:
            raise DocumentError(f"Print status cannot move from {current} to {status}", 409)
        source, note, artifact = PRINT_STATUS_SOURCE[status], "", None
        if status == "ARTIFACT_GENERATED":
            doc = self.get_document(user_id, workspace_id, document_id)
            if doc["sha256"] != row["document_sha256"]:
                raise DocumentError("Document changed since the print job was created", 409)
            data = self.read_pdf(user_id, workspace_id, document_id)   # verifies the stored bytes against the hash
            pages = json.loads(row["pages_json"])
            reader = PdfReader(io.BytesIO(data))
            if pages == list(range(1, len(reader.pages) + 1)):
                artifact = doc                                         # whole document: print the bound version itself
                note = "print artifact is the bound document version"
            else:
                writer = PdfWriter()
                for n in pages:
                    writer.add_page(reader.pages[n - 1])
                buf = io.BytesIO(); writer.write(buf)
                artifact = self._save(user_id, workspace_id, buf.getvalue(),
                                      doc["name"].removesuffix(".pdf") + f"-print-{row['page_range']}.pdf",
                                      source_ids=document_id, operation="print_artifact", source_sha256=doc["sha256"])
                note = f"print artifact generated for pages {row['page_range']}"
        elif status == "PRINT_DIALOG_OPENED":
            if not artifact_sha256 or artifact_sha256 != row["artifact_sha256"]:
                raise DocumentError("The dialog report must name the print artifact's SHA-256", 409)
            note = "browser print dialog opened; physical printing is NOT verified"
        elif status == "SUBMITTED":
            note = "the user states they pressed Print; not verified by RESOLVE or a printer"
        elif status == "UNKNOWN":
            note = "print dialog closed; outcome unknown"
        else:
            note = "browser reported a print failure"
        with self._db() as db:
            if artifact is not None:
                db.execute("UPDATE print_jobs SET artifact_id=?, artifact_sha256=? WHERE id=?",
                           (artifact["id"], artifact["sha256"], row["id"]))
            updated = db.execute("UPDATE print_jobs SET status=?, updated_at=? WHERE id=? AND status=?",
                                 (status, time.time(), row["id"], current)).rowcount
            if updated != 1:
                raise DocumentError("Print job changed concurrently; reload it", 409)
            self._print_event(db, row["id"], current, status, source, note)
            return self._print_job_public(db, self._print_job_row(db, user_id, workspace_id, document_id, row["id"]))

    @synchronized
    def list_print_jobs(self, user_id: str, workspace_id: str, document_id: str) -> list[dict]:
        self.get_document(user_id, workspace_id, document_id)
        with self._db() as db:
            rows = db.execute("SELECT * FROM print_jobs WHERE user_id=? AND workspace_id=? AND document_id=? "
                              "ORDER BY created_at DESC", (user_id, workspace_id, document_id)).fetchall()
            return [self._print_job_public(db, r) for r in rows]

    @staticmethod
    def _purge_print_jobs(db, where: str, args: tuple):
        db.execute(f"DELETE FROM print_events WHERE job_id IN (SELECT id FROM print_jobs WHERE {where})", args)
        db.execute(f"DELETE FROM print_jobs WHERE {where}", args)

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
    def save_generated(self, user_id: str, workspace_id: str, data: bytes, name: str,
                       source_ids: str = "", operation: str = "generated"):
        """Persist a PDF produced by a mission, recording the source document ids (provenance)."""
        return self._save(user_id, workspace_id, data, name, source_ids=source_ids, operation=operation)

    @synchronized
    def extract_text(self, user_id: str, workspace_id: str, document_id: str) -> str:
        reader = PdfReader(io.BytesIO(self.read_pdf(user_id, workspace_id, document_id)), strict=True)
        if len(reader.pages) > self.MAX_TEXT_PAGES:
            raise DocumentError(f"Document has {len(reader.pages)} pages; text extraction is limited to {self.MAX_TEXT_PAGES}", 413)
        return "\n".join((page.extract_text() or "") for page in reader.pages)

    @synchronized
    def delete_document(self, user_id: str, workspace_id: str, document_id: str) -> bool:
        self.get_document(user_id, workspace_id, document_id)  # 404 unless owned by this tenant
        (self.files_dir / f"{document_id}.pdf").unlink(missing_ok=True)
        with self._db() as db:
            db.execute("DELETE FROM documents WHERE id=? AND user_id=? AND workspace_id=?",
                       (document_id, user_id, workspace_id))
            self._purge_print_jobs(db, "document_id=? AND user_id=? AND workspace_id=?", (document_id, user_id, workspace_id))
        return True

    @synchronized
    def delete_workspace(self, user_id: str, workspace_id: str) -> int:
        """Purge PDF artifacts for one tenant workspace; return removed count."""
        with self._db() as db:
            ids = [row[0] for row in db.execute(
                "SELECT id FROM documents WHERE user_id=? AND workspace_id=?", (user_id, workspace_id))]
            for doc_id in ids:
                (self.files_dir / f"{doc_id}.pdf").unlink(missing_ok=True)
            db.execute("DELETE FROM documents WHERE user_id=? AND workspace_id=?", (user_id, workspace_id))
            self._purge_print_jobs(db, "user_id=? AND workspace_id=?", (user_id, workspace_id))
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
            self._purge_print_jobs(db, "user_id=?", (user_id,))
        return len(ids)

