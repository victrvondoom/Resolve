import io
import sys
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader, PdfWriter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from document_studio import DocumentError, DocumentStudio


def sample_pdf(pages=2):
    writer = PdfWriter()
    for i in range(pages):
        writer.add_blank_page(width=300 + i, height=400)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def text_pdf(labels, pagesize=None):
    """Real PDF bytes with one text line per page (reportlab), so page order can be read back."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen.canvas import Canvas
    buffer = io.BytesIO()
    canvas = Canvas(buffer, pagesize=pagesize or A4)
    for label in labels:
        canvas.drawString(72, 720, label)
        canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def page_texts(studio, user, workspace, doc_id):
    reader = PdfReader(io.BytesIO(studio.read_pdf(user, workspace, doc_id)))
    return [(p.extract_text() or "").strip() for p in reader.pages]


class PageToolsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.studio = DocumentStudio(self.temp.name)
        self.doc = self.studio.import_pdf("alice", "work", text_pdf(["Alpha page", "Bravo page", "Charlie page", "Delta page"]), "abc.pdf")
        self.src = {self.doc["id"]: self.doc["sha256"]}

    def tearDown(self):
        self.temp.cleanup()

    def test_reorder_writes_new_version_in_permutation_order(self):
        out = self.studio.transform("alice", "work", "reorder", [self.doc["id"]], self.src, order=[3, 1, 4, 2])
        self.assertNotEqual(out["id"], self.doc["id"])
        self.assertEqual(page_texts(self.studio, "alice", "work", out["id"]),
                         ["Charlie page", "Alpha page", "Delta page", "Bravo page"])
        # the source is untouched and the new version records the exact source hash
        self.assertEqual(page_texts(self.studio, "alice", "work", self.doc["id"])[0], "Alpha page")
        self.assertEqual(out["source_ids"], self.doc["id"])
        self.assertEqual(out["source_sha256"], self.doc["sha256"])
        for bad in ([1, 2, 3], [1, 1, 2, 3], [0, 1, 2, 3], [1, 2, 3, 5], [1, 2, 3, 4], "4,3,2,1", [1.0, 2, 3, 4], None):
            with self.assertRaises(DocumentError, msg=repr(bad)):
                self.studio.transform("alice", "work", "reorder", [self.doc["id"]], self.src, order=bad)

    def test_extract_page_ranges(self):
        out = self.studio.transform("alice", "work", "extract", [self.doc["id"]], self.src, ranges="1-2, 4")
        self.assertEqual(page_texts(self.studio, "alice", "work", out["id"]), ["Alpha page", "Bravo page", "Delta page"])
        self.assertEqual(out["operation"], "extract")
        for bad in ("", "0-2", "3-1", "1-5", "1,1", "1-3,2", "a-b", "1-4"):
            with self.assertRaises(DocumentError, msg=bad):
                self.studio.transform("alice", "work", "extract", [self.doc["id"]], self.src, ranges=bad)

    def test_stale_or_missing_source_hash_rejected(self):
        for expected in ({self.doc["id"]: "0" * 64}, {}, None, "abc"):
            with self.assertRaises(DocumentError) as ctx:
                self.studio.transform("alice", "work", "reorder", [self.doc["id"]], expected, order=[2, 1, 3, 4])
            self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(DocumentError) as ctx:
            self.studio.transform("bob", "work", "extract", [self.doc["id"]], self.src, ranges="1")
        self.assertEqual(ctx.exception.status_code, 404)

    def test_compare_detects_changed_page_and_reports_hashes(self):
        other = self.studio.import_pdf("alice", "work", text_pdf(["Alpha page", "Bravo revised", "Charlie page"]), "abc2.pdf")
        result = self.studio.compare("alice", "work", self.doc["id"], other["id"])
        self.assertEqual(result["a"]["sha256"], self.doc["sha256"])
        self.assertEqual(result["b"]["sha256"], other["sha256"])
        self.assertEqual([p["status"] for p in result["pages"]], ["SAME", "CHANGED", "SAME", "REMOVED"])
        self.assertEqual(result["summary"], {"same": 2, "changed": 1, "added": 0, "removed": 1})
        self.assertIn("- Bravo page", result["pages"][1]["sample"])
        self.assertIn("+ Bravo revised", result["pages"][1]["sample"])
        with self.assertRaises(DocumentError):
            self.studio.compare("alice", "work", self.doc["id"], self.doc["id"])
        with self.assertRaises(DocumentError) as ctx:
            self.studio.compare("alice", "work", self.doc["id"], other["id"], expected_sha256={self.doc["id"]: "x"})
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(DocumentError) as ctx:
            self.studio.compare("bob", "work", self.doc["id"], other["id"])
        self.assertEqual(ctx.exception.status_code, 404)

    def test_page_info_reads_real_size_and_rotation(self):
        from reportlab.lib.pagesizes import letter
        mixed = self.studio.import_pdf("alice", "work", text_pdf(["one", "two"], pagesize=letter), "letter.pdf")
        rotated = self.studio.transform("alice", "work", "rotate", [mixed["id"]], {mixed["id"]: mixed["sha256"]}, degrees=90, pages=[2])
        info = self.studio.page_info("alice", "work", rotated["id"])
        self.assertEqual(info["page_count"], 2)
        self.assertEqual([(p["width_pt"], p["height_pt"]) for p in info["pages"]], [(612.0, 792.0)] * 2)
        self.assertEqual([p["rotation"] for p in info["pages"]], [0, 90])
        self.assertEqual([p["orientation"] for p in info["pages"]], ["portrait", "landscape"])
        self.assertEqual({p["paper"] for p in info["pages"]}, {"Letter"})
        self.assertEqual(self.studio.page_info("alice", "work", self.doc["id"])["pages"][0]["paper"], "A4")
        thumbs = info["thumbnails"]
        if thumbs["available"]:
            png = self.studio.render_thumbnail("alice", "work", rotated["id"], 2, width=120)
            self.assertTrue(png.startswith(b"\x89PNG"))
        else:
            self.assertIn("rasterizer", thumbs["reason"])
            with self.assertRaises(DocumentError) as ctx:
                self.studio.render_thumbnail("alice", "work", rotated["id"], 1)
            self.assertEqual(ctx.exception.status_code, 501)

    def test_print_status_transitions_and_printer_confirmation_refused(self):
        job = self.studio.create_print_job("alice", "work", self.doc["id"], self.doc["sha256"], paper="Letter",
                                           orientation="landscape", page_range="2-3")
        self.assertEqual((job["status"], job["paper"], job["orientation"], job["pages"]), ("PREVIEW_READY", "Letter", "landscape", [2, 3]))
        with self.assertRaises(DocumentError) as ctx:   # cannot skip the artifact
            self.studio.record_print_status("alice", "work", self.doc["id"], job["id"], "PRINT_DIALOG_OPENED", artifact_sha256="x")
        self.assertEqual(ctx.exception.status_code, 409)
        job = self.studio.record_print_status("alice", "work", self.doc["id"], job["id"], "ARTIFACT_GENERATED")
        self.assertEqual(page_texts(self.studio, "alice", "work", job["artifact_id"]), ["Bravo page", "Charlie page"])
        with self.assertRaises(DocumentError):          # the dialog report must name the exact artifact
            self.studio.record_print_status("alice", "work", self.doc["id"], job["id"], "PRINT_DIALOG_OPENED", artifact_sha256="0" * 64)
        job = self.studio.record_print_status("alice", "work", self.doc["id"], job["id"], "PRINT_DIALOG_OPENED", artifact_sha256=job["artifact_sha256"])
        for status in ("SUBMITTED", "PRINTER_CONFIRMED"):
            if status == "PRINTER_CONFIRMED":
                with self.assertRaises(DocumentError) as ctx:
                    self.studio.record_print_status("alice", "work", self.doc["id"], job["id"], status)
                self.assertEqual(ctx.exception.status_code, 403)
            else:
                job = self.studio.record_print_status("alice", "work", self.doc["id"], job["id"], status)
        self.assertEqual(job["status"], "SUBMITTED")
        self.assertFalse(job["physical_printing_verified"])
        self.assertEqual([(e["to_status"], e["source"]) for e in job["events"]],
                         [("PREVIEW_READY", "server"), ("ARTIFACT_GENERATED", "server"), ("PRINT_DIALOG_OPENED", "browser"), ("SUBMITTED", "user_attested")])
        with self.assertRaises(DocumentError) as ctx:
            self.studio.record_print_status("bob", "work", self.doc["id"], job["id"], "UNKNOWN")
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(DocumentError):
            self.studio.create_print_job("alice", "work", self.doc["id"], "stale")
        with self.assertRaises(DocumentError):
            self.studio.create_print_job("alice", "work", self.doc["id"], self.doc["sha256"], paper="A3")
        whole = self.studio.create_print_job("alice", "work", self.doc["id"], self.doc["sha256"])
        whole = self.studio.record_print_status("alice", "work", self.doc["id"], whole["id"], "ARTIFACT_GENERATED")
        self.assertEqual((whole["artifact_id"], whole["page_range"]), (self.doc["id"], "1-4"))
        self.assertEqual(len(self.studio.list_print_jobs("alice", "work", self.doc["id"])), 2)
        self.studio.delete_document("alice", "work", self.doc["id"])
        with self.studio._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM print_jobs").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM print_events").fetchone()[0], 0)


class DocumentStudioTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.studio = DocumentStudio(self.temp.name)
        self.a = self.studio.import_pdf("alice", "work", sample_pdf(), "../../report.pdf")

    def tearDown(self):
        self.temp.cleanup()

    def test_owner_isolation_and_tamper_detection(self):
        self.assertEqual(self.a["name"], "report.pdf")
        with self.assertRaises(DocumentError):
            self.studio.read_pdf("bob", "work", self.a["id"])
        with self.assertRaises(DocumentError):
            self.studio.read_pdf("alice", "other", self.a["id"])
        path = Path(self.temp.name) / "pdfs" / f"{self.a['id']}.pdf"
        path.write_bytes(sample_pdf(3))
        with self.assertRaisesRegex(DocumentError, "changed"):
            self.studio.read_pdf("alice", "work", self.a["id"])

    def test_rotate_merge_delete_with_hash_guards(self):
        src = {self.a["id"]: self.a["sha256"]}
        rotated = self.studio.transform("alice", "work", "rotate", [self.a["id"]], src, degrees=90, pages=[1])
        rotated_pdf = PdfReader(io.BytesIO(self.studio.read_pdf("alice", "work", rotated["id"])))
        self.assertEqual([p.get("/Rotate", 0) for p in rotated_pdf.pages], [90, 0])

        second = self.studio.import_pdf("alice", "work", sample_pdf(1), "another.pdf")
        merged = self.studio.transform("alice", "work", "merge", [self.a["id"], second["id"]],
                                       {**src, second["id"]: second["sha256"]})
        self.assertEqual(len(PdfReader(io.BytesIO(self.studio.read_pdf("alice", "work", merged["id"]))).pages), 3)

        deleted = self.studio.transform("alice", "work", "delete_pages", [merged["id"]],
                                        {merged["id"]: merged["sha256"]}, pages=[2])
        self.assertEqual(len(PdfReader(io.BytesIO(self.studio.read_pdf("alice", "work", deleted["id"]))).pages), 2)
        with self.assertRaisesRegex(DocumentError, "version"):
            self.studio.transform("alice", "work", "rotate", [self.a["id"]], {self.a["id"]: "old"}, degrees=90)
        with self.assertRaises(DocumentError):
            self.studio.transform("alice", "work", "delete_pages", [self.a["id"]], src, pages=[1, 2])

    def test_real_report_pdf(self):
        report = self.studio.create_report("alice", "work", "Architecture", "One verified paragraph.\n" * 90)
        pdf = PdfReader(io.BytesIO(self.studio.read_pdf("alice", "work", report["id"])))
        self.assertGreaterEqual(len(pdf.pages), 2)
        self.assertIn("Architecture", pdf.pages[0].extract_text())
        self.assertEqual(self.studio.list_documents("bob", "work"), [])

    def test_workspace_and_account_deletion_purge_pdf_bytes(self):
        other_workspace = self.studio.import_pdf("alice", "private", sample_pdf(1), "private.pdf")
        other_user = self.studio.import_pdf("bob", "work", sample_pdf(1), "bob.pdf")
        self.assertEqual(self.studio.delete_workspace("alice", "work"), 1)
        self.assertFalse((Path(self.temp.name) / "pdfs" / f"{self.a['id']}.pdf").exists())
        self.assertTrue((Path(self.temp.name) / "pdfs" / f"{other_workspace['id']}.pdf").exists())
        self.assertEqual(self.studio.delete_user("alice"), 1)
        self.assertEqual(self.studio.list_documents("alice", "private"), [])
        self.assertIsNotNone(self.studio.read_pdf("bob", "work", other_user["id"]))


if __name__ == "__main__":
    unittest.main()
