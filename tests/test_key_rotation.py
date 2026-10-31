"""Master-key rotation: new writes use the new key version, data sealed under the previous version stays readable, the
blind index does not change (so duplicate detection survives rotation), and data cannot be opened once its key is gone."""
import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from at_rest import SealedStore, SealError

K1 = base64.urlsafe_b64encode(bytes(range(32))).decode()
K2 = base64.urlsafe_b64encode(bytes(range(32, 64))).decode()


class KeyRotation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.kf = Path(self.tmp.name) / 'k'

    def tearDown(self):
        self.tmp.cleanup()

    def test_rotation_keeps_old_data_readable_and_writes_with_the_new_version(self):
        old = SealedStore(self.kf, env={'RESOLVE_MASTER_KEY': K1})
        blob1 = old.seal(b'private email body', 'u1', 'w1', 'rec1')
        idx1 = old.blind_index('u1', 'w1', 'send the checklist')
        new = SealedStore(self.kf, env={'RESOLVE_MASTER_KEY': K2, 'RESOLVE_MASTER_KEY_ID': '2', 'RESOLVE_MASTER_KEYS_OLD': f'1:{K1}'})
        self.assertEqual(new.open(blob1, 'u1', 'w1', 'rec1'), b'private email body')
        blob2 = new.seal(b'new data', 'u1', 'w1', 'rec2')
        self.assertEqual((new.key_version(blob1), new.key_version(blob2)), (1, 2))
        self.assertEqual(new.blind_index('u1', 'w1', 'send the checklist'), idx1, 'blind index pinned across rotation')
        resealed = new.reseal(blob1, 'u1', 'w1', 'rec1')
        self.assertEqual(new.key_version(resealed), 2)
        only_new = SealedStore(self.kf, env={'RESOLVE_MASTER_KEY': K2, 'RESOLVE_MASTER_KEY_ID': '2'})
        self.assertEqual(only_new.open(resealed, 'u1', 'w1', 'rec1'), b'private email body')
        with self.assertRaises(SealError):
            only_new.open(blob1, 'u1', 'w1', 'rec1')          # without the old key, un-resealed data is unreadable

    def test_scope_and_record_binding_still_hold(self):
        s = SealedStore(self.kf, env={'RESOLVE_MASTER_KEY': K1})
        blob = s.seal(b'x', 'u1', 'w1', 'rec1')
        for scope in (('u2', 'w1', 'rec1'), ('u1', 'w2', 'rec1'), ('u1', 'w1', 'rec2')):
            with self.assertRaises(SealError):
                s.open(blob, *scope)

    def test_invalid_rotation_configuration_is_refused(self):
        with self.assertRaises(SealError):
            SealedStore(self.kf, env={'RESOLVE_MASTER_KEY': K2, 'RESOLVE_MASTER_KEY_ID': '1', 'RESOLVE_MASTER_KEYS_OLD': f'1:{K1}'})
        with self.assertRaises(SealError):
            SealedStore(self.kf, env={'RESOLVE_MASTER_KEY': K1, 'RESOLVE_MASTER_KEY_ID': '300'})


if __name__ == '__main__':
    unittest.main()
