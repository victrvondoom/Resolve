"""Send and draft broker: approval binding, single dispatch, timeouts, provider check before any resend,
read-back verification, revoked access and replay. The Gmail double is scripted; no network is used."""
import base64
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from at_rest import SealedStore
from gmail_adapter import GmailError, build_raw_message, operation_marker, parse_raw
from inbox_store import InboxError, InboxStore
from send_broker import SendBroker, operation_key

U, W = 'usr_a', 'ws_a'


class FakeGmail:
    """Scripted provider. `sent` is what Gmail's Sent folder holds. Failures are injected per method."""
    def __init__(self):
        self.sent = {}            # provider id -> raw message text
        self.drafts = {}
        self.calls = []
        self.send_behaviour = 'ok'        # ok | timeout_after_store | timeout_before_store | revoked | store_wrong_recipient
        self.readback_fails = False
        self.marker_visible_in_sent = True
        self.n = 0

    def _id(self):
        self.n += 1
        return f'p{self.n}'

    def find_by_marker(self, key):
        self.calls.append('find')
        return [pid for pid, raw in self.sent.items() if operation_marker(key) in parse_raw(raw)['body']]

    def send_raw(self, raw):
        self.calls.append('send')
        if self.send_behaviour == 'revoked':
            raise GmailError('REVOKED', 'Google reports this access was revoked')
        pid = self._id()
        stored = raw if self.send_behaviour != 'store_wrong_recipient' else build_raw_message('evil@x.io', 'Hi', 'Body', 'x')
        if self.send_behaviour == 'timeout_before_store':
            raise TimeoutError('no response')
        self.sent[pid] = stored
        if self.send_behaviour == 'timeout_after_store':
            raise TimeoutError('applied, but the response timed out')
        return {'id': pid, 'threadId': pid}

    def get_message_raw(self, pid):
        self.calls.append('readback')
        if self.readback_fails:
            raise GmailError('NOT_FOUND', 'not accessible')
        return parse_raw(self.sent[pid])

    def create_draft(self, raw):
        self.calls.append('create_draft')
        did = self._id()
        self.drafts[did] = raw
        return {'id': did, 'message': {'id': did}}

    def get_draft_raw(self, did):
        self.calls.append('readback_draft')
        if self.readback_fails:
            raise GmailError('PROVIDER_ERROR', 'unavailable')
        return parse_raw(self.drafts[did])


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        key = base64.urlsafe_b64encode(bytes(range(32))).decode()
        self.store = InboxStore(Path(self.tmp.name) / 'i.sqlite3', SealedStore(Path(self.tmp.name) / 'k', env={'RESOLVE_MASTER_KEY': key}))
        cid = self.store.upsert_connection(U, W, 'gmail', 'CONNECTED', ['gmail.readonly', 'gmail.compose'])['id']
        m = self.store.upsert_message(U, W, cid, 't1', 'pm1', 'Sam <sam@partner.io>', time.time(), 'Proposal', 'Could you send the proposal?')
        self.thread = m['thread_id']
        self.clock = [1_000_000.0]
        self.gmail = FakeGmail()
        self.broker = SendBroker(self.store, self.gmail, lambda: self.clock[0])
        self.draft = self.store.create_draft(U, W, self.thread, 'sam@partner.io', 'Re: Proposal', 'Here is the revised proposal.', 'digest-1')

    def tearDown(self):
        self.tmp.cleanup()

    def approve(self, op='SEND_EMAIL'):
        return self.store.approve(U, W, self.draft['id'], 'digest-1', op, self.clock[0])['id']

    def test_approved_send_is_read_back_and_verified(self):
        out = self.broker.send_email(U, W, self.approve(), self.draft['id'], 'digest-1')
        self.assertEqual(out['state'], 'VERIFIED')
        self.assertEqual(self.gmail.calls.count('send'), 1)
        ex = self.store.list_executions(U, W)[0]
        self.assertEqual((ex['state'], ex['reason']), ('VERIFIED', 'SENT_READ_BACK_MATCHED'))

    def test_a_changed_draft_cannot_be_sent_with_an_old_approval(self):
        with self.assertRaises(InboxError) as ctx:
            self.store.approve(U, W, self.draft['id'], 'stale-digest', 'SEND_EMAIL', self.clock[0])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.gmail.calls, [])

    def test_an_approval_is_single_use(self):
        aid = self.approve()
        self.broker.send_email(U, W, aid, self.draft['id'], 'digest-1')
        with self.assertRaises(InboxError):
            self.broker.send_email(U, W, aid, self.draft['id'], 'digest-1')
        self.assertEqual(self.gmail.calls.count('send'), 1)

    def test_an_expired_approval_sends_nothing(self):
        aid = self.approve()
        self.clock[0] += 3600
        with self.assertRaises(InboxError):
            self.broker.send_email(U, W, aid, self.draft['id'], 'digest-1')
        self.assertEqual(self.gmail.calls, [])

    def test_an_approval_for_draft_creation_cannot_send(self):
        aid = self.approve('CREATE_GMAIL_DRAFT')
        with self.assertRaises(InboxError):
            self.broker.send_email(U, W, aid, self.draft['id'], 'digest-1')
        self.assertEqual(self.gmail.calls, [])

    def test_timeout_after_the_message_was_stored_is_not_reported_as_sent_and_is_not_resent(self):
        self.gmail.send_behaviour = 'timeout_after_store'
        out = self.broker.send_email(U, W, self.approve(), self.draft['id'], 'digest-1')
        self.assertEqual(out['state'], 'UNCERTAIN')
        self.assertEqual(self.gmail.calls.count('send'), 1)
        # the user approves the same content again: the provider is checked first and the stored message is found
        self.gmail.send_behaviour = 'ok'
        again = self.broker.send_email(U, W, self.approve(), self.draft['id'], 'digest-1')
        self.assertEqual(self.gmail.calls.count('send'), 1, 'a second send must not happen')
        self.assertEqual(again['state'], 'VERIFIED')
        self.assertTrue(again.get('replayed'))

    def test_timeout_before_storage_is_recovered_only_after_checking_sent(self):
        self.gmail.send_behaviour = 'timeout_before_store'
        out = self.broker.send_email(U, W, self.approve(), self.draft['id'], 'digest-1')
        self.assertEqual(out['state'], 'UNCERTAIN')
        again = self.broker.send_email(U, W, self.approve(), self.draft['id'], 'digest-1')
        self.assertEqual(again['state'], 'NEEDS_RECOVERY', 'nothing was found in Sent, so the user must re-approve after review')
        self.assertEqual(self.gmail.calls.count('send'), 1, 'the second attempt must not dispatch')

    def test_read_back_with_a_different_recipient_is_uncertain_not_verified(self):
        self.gmail.send_behaviour = 'store_wrong_recipient'
        out = self.broker.send_email(U, W, self.approve(), self.draft['id'], 'digest-1')
        self.assertEqual((out['state'], out['reason']), ('UNCERTAIN', 'READBACK_MISMATCH'))

    def test_revoked_access_fails_without_retrying(self):
        self.gmail.send_behaviour = 'revoked'
        out = self.broker.send_email(U, W, self.approve(), self.draft['id'], 'digest-1')
        self.assertEqual((out['state'], out['reason']), ('FAILED', 'REVOKED'))
        self.assertEqual(self.gmail.calls.count('send'), 1)

    def test_a_draft_is_reported_created_only_after_it_is_read_back(self):
        out = self.broker.create_gmail_draft(U, W, self.approve('CREATE_GMAIL_DRAFT'), self.draft['id'], 'digest-1')
        self.assertEqual(out['state'], 'VERIFIED')
        self.assertIn('readback_draft', self.gmail.calls)

    def test_draft_read_back_failure_is_uncertain(self):
        self.gmail.readback_fails = True
        out = self.broker.create_gmail_draft(U, W, self.approve('CREATE_GMAIL_DRAFT'), self.draft['id'], 'digest-1')
        self.assertEqual((out['state'], out['reason']), ('UNCERTAIN', 'READBACK_FAILED'))

    def test_operation_key_depends_on_content_not_approval(self):
        self.assertEqual(operation_key('SEND_EMAIL', 'd'), operation_key('SEND_EMAIL', 'd'))
        self.assertNotEqual(operation_key('SEND_EMAIL', 'd'), operation_key('CREATE_GMAIL_DRAFT', 'd'))

    def test_user_b_cannot_consume_user_a_approval(self):
        aid = self.approve()
        with self.assertRaises(InboxError):
            self.store.consume_approval('usr_b', 'ws_b', aid, 'SEND_EMAIL', 'digest-1', self.clock[0])
        self.assertEqual(self.gmail.calls, [])


if __name__ == '__main__':
    unittest.main()
