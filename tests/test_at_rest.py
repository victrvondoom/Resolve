"""Encryption at rest: authenticated sealing, scope binding, tamper detection, production key policy and migration."""
import base64
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from at_rest import MAGIC, SealedStore, SealError
from document_studio import DocumentStudio
from pypdf import PdfWriter


def pdf_bytes():
    w = PdfWriter(); w.add_blank_page(200, 200); b = io.BytesIO(); w.write(b); return b.getvalue()


class SealingPrimitives(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = SealedStore(Path(self.tmp.name) / 'master.key')

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_and_no_plaintext_in_ciphertext(self):
        blob = self.s.seal(b'private contract text', 'usr_a', 'ws_a', 'doc_1')
        self.assertTrue(blob.startswith(MAGIC))
        self.assertNotIn(b'private contract', blob)
        self.assertEqual(self.s.open(blob, 'usr_a', 'ws_a', 'doc_1'), b'private contract text')

    def test_wrong_user_workspace_or_record_cannot_open(self):
        blob = self.s.seal(b'x' * 64, 'usr_a', 'ws_a', 'doc_1')
        for who in (('usr_b', 'ws_a', 'doc_1'), ('usr_a', 'ws_b', 'doc_1'), ('usr_a', 'ws_a', 'doc_2')):
            with self.subTest(who=who), self.assertRaises(SealError):
                self.s.open(blob, *who)

    def test_any_bit_flip_is_detected(self):
        blob = self.s.seal(b'payload', 'usr_a', 'ws_a', 'doc_1')
        for pos in (len(MAGIC) + 1, len(MAGIC) + 5, len(blob) - 1):
            tampered = bytearray(blob); tampered[pos] ^= 0x01
            with self.subTest(pos=pos), self.assertRaises(SealError):
                self.s.open(bytes(tampered), 'usr_a', 'ws_a', 'doc_1')

    def test_wrong_master_key_cannot_open(self):
        blob = self.s.seal(b'payload', 'usr_a', 'ws_a', 'doc_1')
        other = SealedStore(Path(self.tmp.name) / 'other.key', env={'RESOLVE_MASTER_KEY': base64.urlsafe_b64encode(os.urandom(32)).decode()})
        with self.assertRaises(SealError):
            other.open(blob, 'usr_a', 'ws_a', 'doc_1')

    def test_key_rotation_keeps_old_data_readable(self):
        old_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        old = SealedStore(Path(self.tmp.name) / 'o.key', env={'RESOLVE_MASTER_KEY': old_key})
        blob = old.seal(b'before rotation', 'usr_a', 'ws_a', 'doc_1')
        new_key = base64.urlsafe_b64encode(os.urandom(32)).decode()
        rotated = SealedStore(Path(self.tmp.name) / 'n.key', env={'RESOLVE_MASTER_KEY': new_key})
        with self.assertRaises(SealError):                     # new key alone does not read old data
            rotated.open(blob, 'usr_a', 'ws_a', 'doc_1')
        # the new key needs its own version; reusing version 1 used to let the old key silently replace the new one
        still = SealedStore(Path(self.tmp.name) / 'n2.key', env={'RESOLVE_MASTER_KEY': new_key, 'RESOLVE_MASTER_KEY_ID': '2',
                                                                 'RESOLVE_MASTER_KEYS_OLD': '1:' + old_key})
        self.assertEqual(still.open(blob, 'usr_a', 'ws_a', 'doc_1'), b'before rotation')
        fresh = still.seal(b'after rotation', 'usr_a', 'ws_a', 'doc_2')
        self.assertEqual(still.key_version(fresh), 2, 'new writes use the new key')
        with self.assertRaises(SealError):                     # and the old key alone cannot read them
            old.open(fresh, 'usr_a', 'ws_a', 'doc_2')

    def test_production_refuses_the_development_key_file(self):
        with self.assertRaises(SealError):
            SealedStore(Path(self.tmp.name) / 'dev.key', require_env=True, env={})

    def test_development_key_file_is_private_and_persistent(self):
        path = Path(self.tmp.name) / 'keys' / 'master.key'
        a = SealedStore(path)
        b = SealedStore(path)
        self.assertEqual(a.fingerprint(), b.fingerprint())
        if os.name == 'posix':
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            os.chmod(path, 0o644)
            with self.assertRaises(SealError):
                SealedStore(path)

    def test_invalid_key_material_is_rejected(self):
        with self.assertRaises(SealError):
            SealedStore(Path(self.tmp.name) / 'x.key', env={'RESOLVE_MASTER_KEY': base64.urlsafe_b64encode(b'short').decode()})


class DocumentsAreSealed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = DocumentStudio(Path(self.tmp.name) / 'docs')

    def tearDown(self):
        self.tmp.cleanup()

    def test_stored_pdf_is_not_plaintext_and_roundtrips_for_owner_only(self):
        doc = self.studio.save_generated('usr_a', 'ws_a', pdf_bytes(), 'secret.pdf')
        raw = (self.studio.files_dir / f"{doc['id']}.pdf").read_bytes()
        self.assertTrue(raw.startswith(MAGIC))
        self.assertNotIn(b'%PDF', raw)
        self.assertEqual(self.studio.read_pdf('usr_a', 'ws_a', doc['id'])[:5], b'%PDF-')
        with self.assertRaises(Exception):
            self.studio.read_pdf('usr_b', 'ws_a', doc['id'])

    def test_legacy_plaintext_files_are_migrated_and_still_readable(self):
        data = pdf_bytes()
        doc = self.studio.save_generated('usr_a', 'ws_a', data, 'old.pdf')
        path = self.studio.files_dir / f"{doc['id']}.pdf"
        path.write_bytes(data)                                  # simulate a file stored before encryption existed
        self.assertEqual(self.studio.read_pdf('usr_a', 'ws_a', doc['id']), data)
        self.assertEqual(self.studio.seal_legacy_files(), 1)
        self.assertTrue(path.read_bytes().startswith(MAGIC))
        self.assertEqual(self.studio.read_pdf('usr_a', 'ws_a', doc['id']), data)
        self.assertEqual(self.studio.seal_legacy_files(), 0)    # idempotent

    def test_a_file_that_no_longer_matches_its_record_is_not_sealed(self):
        doc = self.studio.save_generated('usr_a', 'ws_a', pdf_bytes(), 'x.pdf')
        path = self.studio.files_dir / f"{doc['id']}.pdf"
        path.write_bytes(b'%PDF-1.4 tampered')
        self.assertEqual(self.studio.seal_legacy_files(), 0)
        self.assertEqual(path.read_bytes(), b'%PDF-1.4 tampered')


if __name__ == '__main__':
    unittest.main()
