"""Tests for CommerceAdapter (Commerce workspace, sandbox-only)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from commerce_adapter import CommerceAdapter, CommerceError, CommerceSandbox
from resolve_core import Ledger


class CommerceAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / 'ledger.sqlite3')
        self.sandbox = CommerceSandbox(Path(self.tmp.name) / 'store.sqlite3')
        self.adapter = CommerceAdapter(self.ledger, self.sandbox)

    def tearDown(self):
        self.tmp.cleanup()

    def test_draft_order_requires_approval_with_digest(self):
        draft = self.adapter.draft_order(
            mission_id='mission_commerce_1',
            item_id='item_dev_keyboard',
            quantity=2,
            recipient='Jordan Lee',
        )
        self.assertEqual(draft['status'], 'NEEDS_APPROVAL')
        self.assertEqual(draft['total_cents'], 17800)
        self.assertTrue(bool(draft['approval_digest']))

        action = self.ledger.action(draft['action_id'])
        self.assertEqual(action['status'], 'NEEDS_APPROVAL')

    def test_approved_order_executes_and_confirms_receipt(self):
        draft = self.adapter.draft_order(
            mission_id='mission_commerce_2',
            item_id='item_usb_dongle',
            quantity=1,
            recipient='Jordan Lee',
        )
        res = self.adapter.execute_approved_order(draft['action_id'], draft['approval_digest'])
        self.assertEqual(res['status'], 'CONFIRMED')
        self.assertTrue(bool(res['receipt_hash']))

        # Verify independent ground truth in sandbox store
        verified = self.sandbox.verify_order(draft['order_id'])
        self.assertIsNotNone(verified)
        self.assertEqual(verified['recipient'], 'Jordan Lee')
        self.assertEqual(verified['total_cents'], 4500)
        self.assertEqual(verified['receipt_hash'], res['receipt_hash'])

        # Verify causal ledger terminal state
        action = self.ledger.action(draft['action_id'])
        self.assertEqual(action['status'], 'CONFIRMED')

    def test_tampered_digest_refuses_execution(self):
        draft = self.adapter.draft_order(
            mission_id='mission_commerce_3',
            item_id='item_stand_riser',
            quantity=1,
            recipient='Jordan Lee',
        )
        with self.assertRaises(CommerceError):
            self.adapter.execute_approved_order(draft['action_id'], 'corrupted_digest_fake_1234')

        # Ledger action should not have dispatched or confirmed
        action = self.ledger.action(draft['action_id'])
        self.assertEqual(action['status'], 'NEEDS_APPROVAL')

    def test_invalid_quantity_refused(self):
        with self.assertRaises(CommerceError):
            self.adapter.draft_order(
                mission_id='mission_commerce_4',
                item_id='item_usb_dongle',
                quantity=-1,
                recipient='Jordan Lee',
            )

    def test_duplicate_execution_refused(self):
        draft = self.adapter.draft_order(
            mission_id='mission_commerce_5',
            item_id='item_usb_dongle',
            quantity=1,
            recipient='Jordan Lee',
        )
        res1 = self.adapter.execute_approved_order(draft['action_id'], draft['approval_digest'])
        self.assertEqual(res1['status'], 'CONFIRMED')

        with self.assertRaises(CommerceError):
            self.adapter.execute_approved_order(draft['action_id'], draft['approval_digest'])


if __name__ == '__main__':
    unittest.main()
