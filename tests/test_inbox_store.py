"""Inbox data foundation: tenant isolation, idempotent sync, commitment deduplication, verified resolution,
encryption at rest, reversible voice rules, and audit records without private content."""
import base64
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from at_rest import SealedStore
from inbox_store import InboxError, InboxStore

SECRET_BODY = "Could you send the revised proposal by Wednesday? Our pricing is 4200 EUR."


class InboxStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        key = base64.urlsafe_b64encode(bytes(range(32))).decode()
        self.sealer = SealedStore(Path(self.tmp.name) / 'k.bin', env={'RESOLVE_MASTER_KEY': key})
        self.store = InboxStore(Path(self.tmp.name) / 'inbox.sqlite3', self.sealer)
        self.a = ('usr_a', 'ws_a')
        self.b = ('usr_b', 'ws_b')
        self.conn_a = self.store.upsert_connection(*self.a, 'gmail', 'NOT_CONNECTED', ['gmail.readonly'], 'not connected yet')['id']

    def tearDown(self):
        self.tmp.cleanup()

    def msg(self, who=None, pid='m1', body=SECRET_BODY, conn=None):
        u, w = who or self.a
        return self.store.upsert_message(u, w, conn or self.conn_a, 'thr1', pid, 'client@example.com', time.time(), 'Proposal', body)

    def test_resync_does_not_duplicate_messages_and_detects_changes(self):
        first = self.msg()
        again = self.msg()
        self.assertEqual(first['id'], again['id'])
        self.assertFalse(again['changed'])
        changed = self.msg(body=SECRET_BODY + ' Updated.')
        self.assertTrue(changed['changed'])
        self.assertEqual(self.store.read_message(*self.a, first['id'])['body'], SECRET_BODY + ' Updated.')

    def test_commitments_are_deduplicated_when_a_thread_is_reprocessed(self):
        mid = self.msg()['id']
        for _ in range(3):
            self.store.add_commitment(*self.a, mid, 'REQUEST_TO_USER', 'Send the revised proposal', 'Could you send the revised proposal by Wednesday?',
                                      'client@example.com', 'usr_a', None, None)
        self.assertEqual(len(self.store.list_commitments(*self.a)), 1)

    def test_user_b_cannot_read_threads_messages_commitments_or_rules(self):
        mid = self.msg()['id']
        cid = self.store.add_commitment(*self.a, mid, 'PROMISE_BY_USER', 'Send the architecture', 'I will send the architecture tomorrow.',
                                        'usr_a', 'usr_a', None, None)['id']
        rid = self.store.add_voice_rule(*self.a, 'Prioritize collaborators', {'priority': 'high'}, 'workspace', True)['id']
        with self.assertRaises(InboxError) as ctx:
            self.store.read_message(*self.b, mid)
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self.store.list_commitments(*self.b), [])
        self.assertEqual(self.store.list_voice_rules(*self.b), [])
        with self.assertRaises(InboxError):
            self.store.set_commitment_status(*self.b, cid, 'DISMISSED')
        with self.assertRaises(InboxError):
            self.store.set_voice_rule_active(*self.b, rid, False)
        with self.assertRaises(InboxError):
            self.store.add_commitment(*self.b, mid, 'SUGGESTION', 'x', 'x', None, None, None, None)     # another tenant's message
        self.assertEqual(self.store.list_commitments(*self.a)[0]['status'], 'OPEN')                    # untouched

    def test_user_b_cannot_attach_a_message_to_their_own_connection(self):
        conn_b = self.store.upsert_connection(*self.b, 'gmail', 'NOT_CONNECTED', [], '')['id']
        with self.assertRaises(InboxError):
            self.store.upsert_message(*self.b, self.conn_a, 'thr', 'x', 'x', time.time(), 's', 'b')

    def test_verified_resolution_needs_a_verified_execution_record(self):
        mid = self.msg()['id']
        cid = self.store.add_commitment(*self.a, mid, 'PROMISE_BY_USER', 'Send the proposal', 'I will send it', None, 'usr_a', None, None)['id']
        with self.assertRaises(InboxError) as ctx:
            self.store.set_commitment_status(*self.a, cid, 'RESOLVED_VERIFIED', evidence_id='ver_123')
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.store.list_commitments(*self.a)[0]['status'], 'OPEN')
        # a real verified execution record in this workspace is the only evidence accepted
        m = self.msg()
        d = self.store.create_draft(*self.a, m['thread_id'], 'sam@partner.io', 'Re: proposal', 'Here it is.')
        aid = self.store.approve(*self.a, d['id'], d['digest'], 'SEND_EMAIL', time.time())['id']
        eid = self.store.claim_execution(*self.a, aid, 'key-1', 'SEND_EMAIL')[0]['id']
        self.store.finish_execution(*self.a, eid, 'VERIFIED', 'prov-1', 'SENT_READ_BACK_MATCHED', 'read back')
        self.store.set_commitment_status(*self.a, cid, 'RESOLVED_VERIFIED', evidence_id=eid)
        self.assertEqual(self.store.list_commitments(*self.a, status='RESOLVED_VERIFIED')[0]['id'], cid)

    def test_user_confirmation_is_never_recorded_as_verified(self):
        mid = self.msg()['id']
        cid = self.store.add_commitment(*self.a, mid, 'PROMISE_BY_USER', 'Send the proposal', 'I will send it', None, 'usr_a', None, None)['id']
        self.store.set_commitment_status(*self.a, cid, 'RESOLVED_USER_CONFIRMED', reason='I sent it from my laptop')
        self.assertEqual(self.store.list_commitments(*self.a)[0]['status'], 'RESOLVED_USER_CONFIRMED')
        with self.assertRaises(InboxError):
            self.store.set_commitment_status(*self.a, cid, 'RESOLVED_VERIFIED', evidence_id='anything')      # terminal states never move

    def test_status_transitions_are_validated_and_terminal_states_are_final(self):
        mid = self.msg()['id']
        cid = self.store.add_commitment(*self.a, mid, 'PROMISE_BY_USER', 'Send the proposal', 'I will send it', None, 'usr_a', None, None)['id']
        with self.assertRaises(InboxError) as ctx:
            self.store.set_commitment_status(*self.a, cid, 'NOT_A_STATUS')
        self.assertEqual(ctx.exception.status_code, 400)
        self.store.set_commitment_status(*self.a, cid, 'WAITING')
        with self.assertRaises(InboxError):
            self.store.set_commitment_status(*self.a, cid, 'OPEN')          # WAITING cannot jump back to OPEN directly
        with self.assertRaises(InboxError):
            self.store.set_commitment_status(*self.a, cid, 'DISMISSED')      # dismissing needs a reason
        self.store.set_commitment_status(*self.a, cid, 'DISMISSED', reason='no longer needed')
        with self.assertRaises(InboxError):
            self.store.set_commitment_status(*self.a, cid, 'IN_PROGRESS')   # terminal
        self.assertEqual([h['to_status'] for h in self.store.commitment_status_history(*self.a, cid)], ['WAITING', 'DISMISSED'])

    def test_missing_deadline_is_stored_as_null_not_invented(self):
        mid = self.msg()['id']
        cid = self.store.add_commitment(*self.a, mid, 'DEPENDENCY', 'Review before the call', 'We need to review it', None, None, None, None)['id']
        row = self.store.list_commitments(*self.a)[0]
        self.assertIsNone(row['deadline_at'])
        self.assertIsNone(row['deadline_confidence'])

    def test_message_bodies_and_evidence_are_encrypted_on_disk(self):
        mid = self.msg()['id']
        self.store.add_commitment(*self.a, mid, 'REQUEST_TO_USER', 'Send the revised proposal', SECRET_BODY, None, None, None, None)
        raw = (Path(self.tmp.name) / 'inbox.sqlite3').read_bytes()
        self.assertNotIn(b'revised proposal', raw)
        self.assertNotIn(b'4200 EUR', raw)
        self.assertIn(b'RSEV1', raw)

    def test_audit_never_contains_message_text_or_tokens(self):
        mid = self.msg()['id']
        self.store.add_commitment(*self.a, mid, 'REQUEST_TO_USER', 'Send the revised proposal', SECRET_BODY, None, None, None, None)
        self.store.add_voice_rule(*self.a, 'Never send without approval', {'rule': 'approval'}, 'workspace', True)
        log = str(self.store.audit_log(*self.a))
        self.assertNotIn('4200', log)
        self.assertNotIn('revised proposal', log)
        self.assertNotIn('Never send without approval', log)
        self.assertTrue(any(e['action'] == 'commitment.created' for e in self.store.audit_log(*self.a)))

    def test_voice_rules_are_versioned_and_reversible(self):
        r1 = self.store.add_voice_rule(*self.a, 'Use a friendly tone', {'tone': 'friendly'}, 'workspace', True)
        r2 = self.store.add_voice_rule(*self.a, 'Use a formal tone', {'tone': 'formal'}, 'workspace', False)
        self.assertEqual((r1['version'], r2['version']), (1, 2))
        self.assertFalse(r2['active'], 'a rule that needs confirmation is not active until confirmed')
        self.store.set_voice_rule_active(*self.a, r2['id'], True)
        active = [r for r in self.store.list_voice_rules(*self.a) if r['active']]
        self.assertEqual([r['version'] for r in active], [2, 1])
        self.store.set_voice_rule_active(*self.a, r2['id'], False)
        self.assertEqual([r['version'] for r in self.store.list_voice_rules(*self.a) if r['active']], [1])

    def test_purge_is_scoped_to_the_requested_workspace(self):
        self.msg()
        conn_b = self.store.upsert_connection(*self.b, 'gmail', 'NOT_CONNECTED', [], '')['id']
        self.store.upsert_message(*self.b, conn_b, 'thr', 'x', 'them@example.com', time.time(), 'subj', 'body of b')
        self.store.purge(*self.a)
        self.assertEqual(self.store.list_commitments(*self.a), [])
        with self.assertRaises(InboxError):
            self.store.read_message(*self.a, 'msg_none')
        self.assertEqual(self.store.get_connection(*self.a, 'gmail'), None)
        self.assertIsNotNone(self.store.get_connection(*self.b, 'gmail'))

    def test_migration_is_recorded_and_idempotent(self):
        again = InboxStore(Path(self.tmp.name) / 'inbox.sqlite3', self.sealer)
        conn = sqlite3.connect(Path(self.tmp.name) / 'inbox.sqlite3')
        try:
            self.assertEqual([r[0] for r in conn.execute('SELECT version FROM schema_migrations')], [1, 2, 3, 4, 5])
        finally:
            conn.close()


if __name__ == '__main__':
    unittest.main()
