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
