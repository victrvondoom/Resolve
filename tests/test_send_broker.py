"""Send and draft broker: approval binding, single dispatch, timeouts, provider reconciliation by correlation
Message-ID, read-back verification (exact content), revoked access, replay, and the rule that no marker is ever
placed in the content a recipient receives. The Gmail double is scripted; no network is used."""
import base64
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from at_rest import SealedStore
from gmail_adapter import GmailError, build_raw_message, correlation_id, parse_raw
from inbox_store import InboxError, InboxStore
from send_broker import SendBroker, operation_key

U, W = 'usr_a', 'ws_a'
BODY = 'Here is the revised proposal.\nIt covers pricing.'


def tamper(raw: str, *, to: str | None = None, cc: str | None = None, subject: str | None = None, body: str | None = None,
           drop_message_id: bool = False) -> str:
    """A provider-stored copy of a message with one field changed (the correlation header is kept unless dropped)."""
    import base64 as b64
    from email import message_from_bytes
    from email.message import EmailMessage
    m = message_from_bytes(b64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    out = EmailMessage()
    for k in ("To", "Cc", "Subject", "Message-ID"):
        if k == "Message-ID" and drop_message_id:
            continue
        v = m.get(k)
        if k == "To" and to is not None:
            v = to
        if k == "Cc" and cc is not None:
            v = cc
        if k == "Subject" and subject is not None:
            v = subject
        if v:
            out[k] = v
    out.set_content(body if body is not None else parse_raw(raw)["body"])
    return b64.urlsafe_b64encode(out.as_bytes()).decode().rstrip("=")


class FakeGmail:
    """Scripted provider. `sent` is what Gmail's Sent folder holds. Failures are injected per method."""
    def __init__(self):
        self.sent, self.drafts, self.calls = {}, {}, []
        self.send_behaviour = 'ok'     # ok | timeout_after_store | timeout_before_store | revoked | store_wrong_recipient | store_altered_body | drop_message_id
        self.readback_fails = False
        self.n = 0

    def _id(self):
        self.n += 1
        return f'p{self.n}'

    def find_by_correlation(self, key):
        self.calls.append('find')
        return [pid for pid, raw in self.sent.items() if parse_raw(raw)['message_id'] == correlation_id(key)]

    def find_draft_by_correlation(self, key):
        self.calls.append('find_draft')
        return [did for did, raw in self.drafts.items() if parse_raw(raw)['message_id'] == correlation_id(key)]

    def send_raw(self, raw):
        self.calls.append('send')
        if self.send_behaviour == 'revoked':
            raise GmailError('REVOKED', 'Google reports this access was revoked')
        pid = self._id()
        stored = raw
        if self.send_behaviour == 'store_wrong_recipient':
            stored = tamper(raw, to='evil@x.io')
        if self.send_behaviour == 'store_altered_body':
            stored = tamper(raw, body=parse_raw(raw)['body'] + ' (edited)')
        if self.send_behaviour == 'drop_message_id':
            stored = tamper(raw, drop_message_id=True)
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
        self.draft = self.store.create_draft(U, W, self.thread, 'sam@partner.io', 'Proposal', BODY)

    def tearDown(self):
        self.tmp.cleanup()

    def approve(self, op='SEND_EMAIL'):
        return self.store.approve(U, W, self.draft['id'], self.draft['digest'], op, self.clock[0])['id']

    def send(self, aid=None):
        return self.broker.send_email(U, W, aid or self.approve(), self.draft['id'], self.draft['digest'])

    def test_approved_send_is_read_back_and_verified(self):
        out = self.send()
        self.assertEqual(out['state'], 'VERIFIED')
        self.assertEqual(self.gmail.calls.count('send'), 1)
        ex = self.store.list_executions(U, W)[0]
        self.assertEqual((ex['state'], ex['reason']), ('VERIFIED', 'SENT_READ_BACK_MATCHED'))

    def test_no_marker_or_extra_text_reaches_the_recipient(self):
        self.send()
        stored = parse_raw(next(iter(self.gmail.sent.values())))
        self.assertEqual(stored['body'].strip(), BODY)
        self.assertNotIn('RESOLVE', stored['body'])
        self.assertNotIn('operation', stored['body'].lower())
        self.assertEqual(stored['message_id'], correlation_id(operation_key('SEND_EMAIL', self.draft['digest'])))

    def test_a_changed_draft_cannot_be_sent_with_an_old_approval(self):
        old_aid = self.approve()
        self.store.update_draft(U, W, self.draft['id'], body='A different body.')
        with self.assertRaises(InboxError) as ctx:
            self.broker.send_email(U, W, old_aid, self.draft['id'], self.draft['digest'])
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn('changed after', ctx.exception.message)
        self.assertEqual(self.gmail.calls, [])

    def test_an_approval_is_single_use(self):
        aid = self.approve()
        self.send(aid)
        with self.assertRaises(InboxError):
            self.send(aid)
        self.assertEqual(self.gmail.calls.count('send'), 1)

    def test_an_expired_approval_sends_nothing(self):
        aid = self.approve()
        self.clock[0] += 3600
        with self.assertRaises(InboxError):
            self.send(aid)
        self.assertEqual(self.gmail.calls, [])

    def test_an_approval_for_draft_creation_cannot_send(self):
        aid = self.approve('CREATE_GMAIL_DRAFT')
        with self.assertRaises(InboxError):
            self.send(aid)
        self.assertEqual(self.gmail.calls, [])

    def test_timeout_after_the_message_was_stored_is_not_reported_as_sent_and_is_not_resent(self):
        self.gmail.send_behaviour = 'timeout_after_store'
        out = self.send()
        self.assertEqual(out['state'], 'UNCERTAIN')
        self.assertEqual(self.gmail.calls.count('send'), 1)
        self.gmail.send_behaviour = 'ok'
        again = self.send()
        self.assertEqual(self.gmail.calls.count('send'), 1, 'a second send must not happen')
        self.assertEqual(again['state'], 'VERIFIED')
        self.assertTrue(again.get('replayed'))

    def test_timeout_before_storage_is_recovered_only_after_checking_sent(self):
        self.gmail.send_behaviour = 'timeout_before_store'
        out = self.send()
        self.assertEqual(out['state'], 'UNCERTAIN')
        again = self.send()
        self.assertEqual(again['state'], 'NEEDS_RECOVERY', 'nothing was found in Sent, so the user must re-approve after review')
        self.assertEqual(self.gmail.calls.count('send'), 1, 'the second attempt must not dispatch')

    def test_read_back_with_a_different_recipient_is_uncertain_not_verified(self):
        self.gmail.send_behaviour = 'store_wrong_recipient'
        out = self.send()
        self.assertEqual((out['state'], out['reason']), ('UNCERTAIN', 'RECIPIENT_MISMATCH'))

    def test_read_back_with_an_altered_body_is_uncertain(self):
        self.gmail.send_behaviour = 'store_altered_body'
        out = self.send()
        self.assertEqual((out['state'], out['reason']), ('UNCERTAIN', 'BODY_MISMATCH'))

    def test_missing_correlation_header_is_uncertain_and_never_verified(self):
        self.gmail.send_behaviour = 'drop_message_id'
        out = self.send()
        self.assertEqual((out['state'], out['reason']), ('UNCERTAIN', 'CORRELATION_MISSING'))

    def test_revoked_access_fails_without_retrying(self):
        self.gmail.send_behaviour = 'revoked'
        out = self.send()
        self.assertEqual((out['state'], out['reason']), ('FAILED', 'REVOKED'))
        self.assertEqual(self.gmail.calls.count('send'), 1)

    def test_a_draft_is_reported_created_only_after_it_is_read_back(self):
        out = self.broker.create_gmail_draft(U, W, self.approve('CREATE_GMAIL_DRAFT'), self.draft['id'], self.draft['digest'])
        self.assertEqual(out['state'], 'VERIFIED')
        self.assertIn('readback_draft', self.gmail.calls)

    def test_draft_read_back_failure_is_uncertain(self):
        self.gmail.readback_fails = True
        out = self.broker.create_gmail_draft(U, W, self.approve('CREATE_GMAIL_DRAFT'), self.draft['id'], self.draft['digest'])
        self.assertEqual((out['state'], out['reason']), ('UNCERTAIN', 'READBACK_FAILED'))

    def test_operation_key_depends_on_content_not_approval(self):
        self.assertEqual(operation_key('SEND_EMAIL', 'd'), operation_key('SEND_EMAIL', 'd'))
        self.assertNotEqual(operation_key('SEND_EMAIL', 'd'), operation_key('CREATE_GMAIL_DRAFT', 'd'))

    def test_user_b_cannot_consume_user_a_approval(self):
        aid = self.approve()
        with self.assertRaises(InboxError):
            self.store.consume_approval('usr_b', 'ws_b', aid, 'SEND_EMAIL', self.draft['digest'], self.clock[0])
        self.assertEqual(self.gmail.calls, [])

    def test_uncertain_draft_is_reconciled_by_correlation_without_a_second_draft(self):
        self.gmail.send_behaviour = 'ok'
        self.gmail.readback_fails = True
        aid = self.approve('CREATE_GMAIL_DRAFT')
        out = self.broker.create_gmail_draft(U, W, aid, self.draft['id'], self.draft['digest'])
        self.assertEqual(out['state'], 'UNCERTAIN')
        self.gmail.readback_fails = False
        again = self.broker.create_gmail_draft(U, W, self.approve('CREATE_GMAIL_DRAFT'), self.draft['id'], self.draft['digest'])
        self.assertEqual(again['state'], 'VERIFIED')
        self.assertEqual(len(self.gmail.drafts), 1, 'reconciliation must not create a second Gmail draft')


class MutationCheck(unittest.TestCase):
    """Duplicate-send protection is tested by removing it: the same approved content approved twice must yield one send.
    With both protections removed, the same approval twice sends twice: the failure this suite exists to catch."""

    def run_two_approvals(self, disable_replay: bool) -> int:
        tmp = tempfile.TemporaryDirectory()
        try:
            key = base64.urlsafe_b64encode(bytes(range(32))).decode()
            store = InboxStore(Path(tmp.name) / 'i.sqlite3', SealedStore(Path(tmp.name) / 'k', env={'RESOLVE_MASTER_KEY': key}))
            cid = store.upsert_connection(U, W, 'gmail', 'CONNECTED', ['gmail.readonly', 'gmail.compose'])['id']
            m = store.upsert_message(U, W, cid, 't1', 'pm1', 'Sam <sam@partner.io>', time.time(), 'Proposal', 'Could you send it?')
            draft = store.create_draft(U, W, m['thread_id'], 'sam@partner.io', 'Proposal', BODY)
            clock = [1.0]
            gmail = FakeGmail()
            broker = SendBroker(store, gmail, lambda: clock[0])
            if disable_replay:
                # mutation: remove BOTH protections: the durable claim returns "created" every time, and the pre-send Sent check finds nothing
                orig = broker.store.claim_execution
                def claim_fresh(*a, **k):
                    rec, _created = orig(*a, **k)
                    return rec, True
                broker.store.claim_execution = claim_fresh
                gmail.find_by_correlation = lambda key: []
            gmail.send_behaviour = 'timeout_after_store'
            a1 = store.approve(U, W, draft['id'], draft['digest'], 'SEND_EMAIL', clock[0])['id']
            broker.send_email(U, W, a1, draft['id'], draft['digest'])
            gmail.send_behaviour = 'ok'
            a2 = store.approve(U, W, draft['id'], draft['digest'], 'SEND_EMAIL', clock[0])['id']
            broker.send_email(U, W, a2, draft['id'], draft['digest'])
            return gmail.calls.count('send')
        finally:
            tmp.cleanup()

    def test_protected_broker_sends_exactly_once(self):
        self.assertEqual(self.run_two_approvals(disable_replay=False), 1)

    def test_mutation_without_replay_protection_sends_twice(self):
        self.assertEqual(self.run_two_approvals(disable_replay=True), 2)


if __name__ == '__main__':
    unittest.main()
