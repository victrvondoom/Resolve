"""LOCAL mode: the real local mail server (SMTP + POP3 over loopback sockets, Maildir on disk) and the full inbox pipeline
against it. Nothing here is an in-memory canned provider: every send crosses a TCP socket, lands in a Maildir file, and is
read back over POP3. Effects are checked a second time by reading the Maildir directly (outside the generating code).
"""
import base64
import mailbox
import os
import smtplib
import socket
import sys
import tempfile
import time
import unittest
from email import message_from_bytes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import local_mail as lm
from at_rest import SealedStore
from gmail_adapter import GmailError, build_raw_message
from inbox_api import InboxApi
from inbox_store import InboxStore

DOM = lm.DEFAULT_DOMAIN


def maildir_messages(root, address, folder):
    md = mailbox.Maildir(os.path.join(root, 'mail', address, folder), factory=None, create=False)
    return [message_from_bytes(md.get_bytes(k)) for k in md.keys()]


class ServerProtocol(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = lm.LocalMailServer(self.tmp.name, 0, 0).start()
        self.srv.store.create_account(f'you@{DOM}', 'password-you')
        self.srv.store.create_account(f'ana@{DOM}', 'password-ana')

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def smtp(self):
        s = smtplib.SMTP('127.0.0.1', self.srv.smtp_port, timeout=5)
        s.ehlo()
        return s

    def test_submission_requires_authentication(self):
        s = self.smtp()
        code, _ = s.mail(f'you@{DOM}')
        self.assertEqual(code, 530)
        s.quit()

    def test_wrong_password_is_refused(self):
        s = self.smtp()
        with self.assertRaises(smtplib.SMTPAuthenticationError):
            s.login(f'you@{DOM}', 'wrong-password')
        s.quit()

    def test_sender_cannot_be_spoofed(self):
        s = self.smtp()
        s.login(f'you@{DOM}', 'password-you')
        code, _ = s.mail(f'ana@{DOM}')
        self.assertEqual(code, 553)
        s.quit()

    def test_external_recipients_are_refused_so_nothing_leaves_the_machine(self):
        s = self.smtp()
        s.login(f'you@{DOM}', 'password-you')
        s.mail(f'you@{DOM}')
        for rcpt in ('someone@gmail.com', 'x@example.com', f'nobody@{DOM}'):
            code, _ = s.rcpt(rcpt)
            self.assertEqual(code, 550, rcpt)
        s.quit()
        self.assertEqual(self.srv.store.counts(f'you@{DOM}')['Sent'], 0)

    def test_binds_to_loopback_only(self):
        with self.assertRaises(ValueError):
            lm.LocalMailServer(self.tmp.name, 0, 0, host='0.0.0.0')

    def test_delivery_is_persistent_maildir_and_survives_a_server_restart(self):
        mid = lm.send_as(f'ana@{DOM}', 'password-ana', f'you@{DOM}', 'Logs', 'Can you check the logs?', smtp_port=self.srv.smtp_port)
        self.srv.stop()
        on_disk = maildir_messages(self.tmp.name, f'you@{DOM}', 'INBOX')
        self.assertEqual([m['Message-ID'].strip('<>') for m in on_disk], [mid])
        self.assertEqual(len(maildir_messages(self.tmp.name, f'ana@{DOM}', 'Sent')), 1)
        self.srv = lm.LocalMailServer(self.tmp.name, 0, 0).start()      # restart over the same directory
        p = lm.LocalMailProvider(f'you@{DOM}', 'password-you', smtp_port=self.srv.smtp_port, pop3_port=self.srv.pop3_port)
        self.assertEqual(p.profile()['messages_total'], 1)
        self.assertEqual(p.sync_all()[0]['body'].strip(), 'Can you check the logs?')

    def test_pop3_dele_removes_only_that_message(self):
        lm.send_as(f'ana@{DOM}', 'password-ana', f'you@{DOM}', 'A', 'one', smtp_port=self.srv.smtp_port)
        lm.send_as(f'ana@{DOM}', 'password-ana', f'you@{DOM}', 'B', 'two', smtp_port=self.srv.smtp_port)
        import poplib
        pop = poplib.POP3('127.0.0.1', self.srv.pop3_port, timeout=5)
        pop.user(f'you@{DOM}')
        pop.pass_('password-you')
        pop.dele(1)
        pop.quit()
        left = maildir_messages(self.tmp.name, f'you@{DOM}', 'INBOX')
        self.assertEqual([m['Subject'] for m in left], ['B'])


class ProviderContract(unittest.TestCase):
    """The same contract the Gmail adapter satisfies, exercised against the running local server."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.srv = lm.LocalMailServer(self.tmp.name, 0, 0).start()
        self.srv.store.create_account(f'you@{DOM}', 'password-you')
        self.srv.store.create_account(f'ana@{DOM}', 'password-ana')
        self.p = lm.LocalMailProvider(f'you@{DOM}', 'password-you', smtp_port=self.srv.smtp_port, pop3_port=self.srv.pop3_port)

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def test_send_then_independent_read_back_and_correlation_lookup(self):
        raw = build_raw_message(f'ana@{DOM}', 'Re: Logs', 'Checked: the migration finished.', 'opkey1')
        self.assertEqual(self.p.find_by_correlation('opkey1'), [])
        resp = self.p.send_raw(raw)
        back = self.p.get_message_raw(resp['id'])
        self.assertEqual((back['to'], back['subject'], back['body'].strip()), (f'ana@{DOM}', 'Re: Logs', 'Checked: the migration finished.'))
        self.assertEqual(self.p.find_by_correlation('opkey1'), [resp['id']])
        delivered = maildir_messages(self.tmp.name, f'ana@{DOM}', 'INBOX')
        self.assertEqual(len(delivered), 1, 'the recipient mailbox on disk received it')

    def test_drafts_are_filed_and_never_delivered(self):
        d = self.p.create_draft(build_raw_message(f'ana@{DOM}', 'Draft', 'not yet', 'opkey2'))
        self.assertEqual(self.p.get_draft_raw(d['id'])['body'].strip(), 'not yet')
        self.assertEqual(self.p.find_draft_by_correlation('opkey2'), [d['id']])
        self.assertEqual(maildir_messages(self.tmp.name, f'ana@{DOM}', 'INBOX'), [])
        self.assertEqual(self.p.find_by_correlation('opkey2'), [], 'a draft is not in Sent')

    def test_external_recipient_is_a_typed_refusal(self):
        with self.assertRaises(GmailError) as cm:
            self.p.send_raw(build_raw_message('person@gmail.com', 's', 'b', 'opkey3'))
        self.assertEqual(cm.exception.code, 'RECIPIENT_REFUSED')

    def test_bad_credentials_and_unreachable_server_are_typed(self):
        bad = lm.LocalMailProvider(f'you@{DOM}', 'nope', smtp_port=self.srv.smtp_port, pop3_port=self.srv.pop3_port)
        with self.assertRaises(GmailError) as cm:
            bad.profile()
        self.assertEqual(cm.exception.code, 'REVOKED')
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()
        down = lm.LocalMailProvider(f'you@{DOM}', 'password-you', smtp_port=port, pop3_port=port)
        with self.assertRaises(GmailError) as cm:
            down.send_raw(build_raw_message(f'ana@{DOM}', 's', 'b', 'opkey4'))
        self.assertEqual(cm.exception.code, 'PROVIDER_ERROR')

    def test_replies_thread_with_the_original(self):
        mid = lm.send_as(f'ana@{DOM}', 'password-ana', f'you@{DOM}', 'Staging', 'Is the migration done?', smtp_port=self.srv.smtp_port)
        lm.send_as(f'ana@{DOM}', 'password-ana', f'you@{DOM}', 'Re: Staging', 'Any news?', smtp_port=self.srv.smtp_port, in_reply_to=mid)
        self.p.send_raw(build_raw_message(f'ana@{DOM}', 'Re: Staging', 'Yes, finished.', 'opkey5'))
        msgs = self.p.sync_all()
        self.assertEqual(len({m['provider_thread_id'] for m in msgs}), 1, msgs)
        self.assertEqual([m['label_ids'][0] for m in msgs], ['INBOX', 'INBOX', 'SENT'])


class LocalModePipeline(unittest.TestCase):
    """Connect a local mailbox, receive over SMTP, sync, brief, draft, exact approval, real local send, read-back VERIFIED."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        key = base64.urlsafe_b64encode(bytes(range(32))).decode()
        self.sealer = SealedStore(Path(self.tmp.name) / 'k', env={'RESOLVE_MASTER_KEY': key})
        self.mailroot = os.path.join(self.tmp.name, 'localmail')
        self.srv = lm.LocalMailServer(self.mailroot, 0, 0).start()
        self.db = Path(self.tmp.name) / 'inbox.sqlite3'
        self.new_api()
        self.a = {'user_id': 'usr_a', 'workspace_id': 'ws_a'}
        self.b = {'user_id': 'usr_b', 'workspace_id': 'ws_b'}

    def new_api(self):
        self.store = InboxStore(self.db, self.sealer)
        self.api = InboxApi(self.store, None, lambda *a: (500, {}, {}), local_mail=self.srv)

    def tearDown(self):
        self.srv.stop()
        self.tmp.cleanup()

    def call(self, method, path, user=None, body=None, mode='LOCAL'):
        return self.api.handle(method, path, {'mode': [mode]}, body, user or self.a)

    def connect_and_receive(self):
        st, out = self.call('POST', '/api/inbox/connections/local/create', body={'name': 'founder'})
        self.assertEqual(st, 201, out)
        self.assertEqual(out['status'], 'CONNECTED')
        st, d = self.call('POST', '/api/inbox/local/deliver', body={'from_name': 'priya', 'subject': 'Deployment checklist',
                                                                     'body': 'Hi, could you send the revised deployment checklist before 4 PM?'})
        self.assertEqual(st, 201, d)
        st, s = self.call('POST', '/api/inbox/sync', body={'mode': 'LOCAL'})
        self.assertEqual((st, s['state'], s['fetched']), (200, 'SUCCEEDED', 1), s)
        return out['account']

    def test_full_local_journey_is_verified_by_independent_read_back(self):
        account = self.connect_and_receive()
        self.assertEqual(account, f'founder@{DOM}')
        st, brief = self.call('GET', '/api/inbox/brief')
        self.assertTrue(brief['connected'])
        self.assertIn('LOCAL', brief['label'])
        self.assertTrue(brief['requests_to_me'] or brief['needs_attention'], brief)
        tid = self.call('GET', '/api/inbox/threads')[1]['threads'][0]['thread_id']
        st, d = self.call('POST', f'/api/inbox/threads/{tid}/drafts', body={'facts': {'status': 'I will send it before 4 PM today.'}})
        self.assertEqual((st, d['recipient']), (201, f'priya@{DOM}'), d)
        st, appr = self.call('POST', f'/api/inbox/drafts/{d["id"]}/approve', body={'digest': d['digest'], 'operation': 'SEND_EMAIL'})
        self.assertEqual(st, 200, appr)
        st, out = self.call('POST', f'/api/inbox/drafts/{d["id"]}/send', body={'approval_id': appr['id'], 'digest': d['digest']})
        self.assertEqual((st, out['state']), (200, 'VERIFIED'), out)
        self.assertIn('LOCAL mail server', out['label'])
        # independent evidence outside RESOLVE: the recipient's Maildir file on disk
        got = maildir_messages(self.mailroot, f'priya@{DOM}', 'INBOX')
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].get_payload(decode=True).decode().strip(), d['body'].strip())
        self.assertNotIn('resolve', got[0].get_payload(decode=True).decode().lower().replace('resolve.test', ''))
        # the approval is single use
        st, _ = self.call('POST', f'/api/inbox/drafts/{d["id"]}/send', body={'approval_id': appr['id'], 'digest': d['digest']})
        self.assertEqual(st, 409)
        self.assertEqual(len(maildir_messages(self.mailroot, f'priya@{DOM}', 'INBOX')), 1, 'no second delivery')
        # the reply appears in the thread after the next sync (from Sent), and resync creates no duplicates
        s2 = self.call('POST', '/api/inbox/sync', body={'mode': 'LOCAL'})[1]
        self.assertEqual(s2['fetched'], 2)
        s3 = self.call('POST', '/api/inbox/sync', body={'mode': 'LOCAL'})[1]
        self.assertEqual((s3['changed_messages'], s3['new_commitments']), (0, 0))
        detail = self.call('GET', f'/api/inbox/threads/{tid}')[1]
        self.assertEqual(len(detail['messages']), 2)

    def test_changed_content_after_approval_cannot_be_sent(self):
        self.connect_and_receive()
        tid = self.call('GET', '/api/inbox/threads')[1]['threads'][0]['thread_id']
        d = self.call('POST', f'/api/inbox/threads/{tid}/drafts', body={'facts': {}})[1]
        appr = self.call('POST', f'/api/inbox/drafts/{d["id"]}/approve', body={'digest': d['digest'], 'operation': 'SEND_EMAIL'})[1]
        self.call('PATCH', f'/api/inbox/drafts/{d["id"]}', body={'body': d['body'] + '\nP.S. changed'})
        st, out = self.call('POST', f'/api/inbox/drafts/{d["id"]}/send', body={'approval_id': appr['id'], 'digest': d['digest']})
        self.assertNotEqual(out.get('state'), 'VERIFIED', out)
        self.assertEqual(maildir_messages(self.mailroot, f'priya@{DOM}', 'INBOX'), [])

    def test_timeout_is_uncertain_and_reconcile_is_read_only(self):
        self.connect_and_receive()
        tid = self.call('GET', '/api/inbox/threads')[1]['threads'][0]['thread_id']
        d = self.call('POST', f'/api/inbox/threads/{tid}/drafts', body={'facts': {}})[1]
        appr = self.call('POST', f'/api/inbox/drafts/{d["id"]}/approve', body={'digest': d['digest'], 'operation': 'SEND_EMAIL'})[1]
        real_send = lm.LocalMailProvider.send_raw

        def lost_ack(self_, raw):          # the message really is delivered; only the acknowledgement is lost
            real_send(self_, raw)
            raise TimeoutError('acknowledgement lost')
        lm.LocalMailProvider.send_raw = lost_ack
        try:
            out = self.call('POST', f'/api/inbox/drafts/{d["id"]}/send', body={'approval_id': appr['id'], 'digest': d['digest']})[1]
        finally:
            lm.LocalMailProvider.send_raw = real_send
        self.assertEqual(out['state'], 'UNCERTAIN')
        calls = []
        orig = lm.LocalMailProvider.send_raw
        lm.LocalMailProvider.send_raw = lambda self_, raw: calls.append(raw) or orig(self_, raw)
        try:
            st, rec = self.call('POST', f'/api/inbox/executions/{out["execution_id"]}/reconcile')
        finally:
            lm.LocalMailProvider.send_raw = orig
        self.assertEqual((st, rec['state']), (200, 'VERIFIED'), rec)
        self.assertEqual(calls, [], 'reconciliation never sends')
        self.assertEqual(len(maildir_messages(self.mailroot, f'priya@{DOM}', 'INBOX')), 1)

    def test_local_records_are_separate_from_live_and_from_other_users(self):
        self.connect_and_receive()
        self.assertEqual(self.store.list_threads('usr_a', 'ws_a'), [], 'LIVE workspace untouched')
        self.assertEqual(len(self.store.list_threads('usr_a', 'ws_a.local')), 1)
        st, other = self.call('GET', '/api/inbox/threads', user=self.b)
        self.assertEqual(other['threads'], [])
        st, _ = self.call('POST', '/api/inbox/sync', user=self.b, body={'mode': 'LOCAL'})
        self.assertEqual(st, 409, 'user B has no local mailbox and cannot use A\'s')
        live = self.call('GET', '/api/inbox/brief', mode='LIVE')[1]
        self.assertFalse(live['connected'])

    def test_credentials_are_sealed_and_survive_restart(self):
        self.connect_and_receive()
        raw = self.db.read_bytes()
        creds = self.store.load_oauth_token('usr_a', 'ws_a.local', 'local_mail')
        self.assertNotIn(creds['password'].encode(), raw)
        self.new_api()                                          # application restart over the same database
        s = self.call('POST', '/api/inbox/sync', body={'mode': 'LOCAL'})[1]
        self.assertEqual((s['state'], s['changed_messages']), ('SUCCEEDED', 0))

    def test_colleague_sender_cannot_be_a_user_mailbox(self):
        self.connect_and_receive()
        st, out = self.call('POST', '/api/inbox/local/deliver', body={'from_name': 'founder', 'subject': 's', 'body': 'b'})
        self.assertEqual(st, 409, out)

    def test_disconnect_removes_credentials(self):
        self.connect_and_receive()
        self.call('DELETE', '/api/inbox/connections/local')
        self.assertIsNone(self.store.load_oauth_token('usr_a', 'ws_a.local', 'local_mail'))
        st, _ = self.call('POST', '/api/inbox/sync', body={'mode': 'LOCAL'})
        self.assertEqual(st, 409)

    def test_not_configured_is_honest(self):
        api = InboxApi(self.store, None, lambda *a: (500, {}, {}), local_mail=None)
        st, out = api.handle('POST', '/api/inbox/connections/local/create', {}, {'name': 'x'}, self.a)
        self.assertEqual((st, out.get('code')), (409, 'NOT_CONFIGURED'))
        self.assertEqual(api.handle('GET', '/api/inbox/connections', {}, None, self.a)[1]['local']['status'], 'NOT_CONFIGURED')


if __name__ == '__main__':
    unittest.main()
