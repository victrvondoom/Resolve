"""Inbox service: sync (persist, idempotent, failure states), triage and commitments per thread, tenant isolation,
the Morning Brief computed only from persisted records, and drafts that use only facts from the thread."""
import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from at_rest import SealedStore
from gmail_adapter import GmailError
from inbox_service import draft_reply, morning_brief, sync_connection
from inbox_store import InboxError, InboxStore

ME = "me@example.test"
NOW = 1_800_000_000.0


def msg(mid, thread, sender, body, subject="Project", sent=NOW):
    return {"provider_message_id": mid, "provider_thread_id": thread, "sender": sender, "subject": subject,
            "sent_at": sent, "body": body}


class FakeClient:
    def __init__(self, messages=None, error=None):
        self.messages, self.error = messages or [], error

    def sync_all(self, query):
        if self.error:
            raise self.error
        return list(self.messages)


class InboxServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        key = base64.urlsafe_b64encode(bytes(range(32))).decode()
        self.sealer = SealedStore(Path(self.tmp.name) / 'k.bin', env={'RESOLVE_MASTER_KEY': key})
        self.store = InboxStore(Path(self.tmp.name) / 'inbox.sqlite3', self.sealer)
        self.a = ('usr_a', 'ws_a')
        self.store.upsert_connection(*self.a, 'gmail', 'CONNECTED', ['gmail.readonly'])

    def tearDown(self):
        self.tmp.cleanup()

    def test_sync_persists_triage_and_commitments_and_is_idempotent(self):
        client = FakeClient([msg('m1', 't1', 'Ana <ana@example.test>', "Could you send the revised proposal by Friday?")])
        first = sync_connection(self.store, *self.a, client, ME)
        self.assertEqual(first['fetched'], 1)
        self.assertEqual(first['new_commitments'], 1)
        second = sync_connection(self.store, *self.a, client, ME)
        self.assertEqual(second['changed_messages'], 0)
        self.assertEqual(second['new_commitments'], 0)
        self.assertEqual(len(self.store.list_commitments(*self.a)), 1)
        self.assertEqual(len(self.store.list_triage(*self.a)), 1)

    def test_provider_failure_is_recorded_honestly_and_raises(self):
        with self.assertRaises(InboxError) as ctx:
            sync_connection(self.store, *self.a, FakeClient(error=GmailError('REVOKED', 'Access was revoked')), ME)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.store.get_connection(*self.a, 'gmail')['status'], 'REVOKED')
        self.assertEqual(self.store.list_commitments(*self.a), [])

    def test_unknown_error_is_error_state_not_success(self):
        with self.assertRaises(InboxError) as ctx:
            sync_connection(self.store, *self.a, FakeClient(error=GmailError('NETWORK', 'timeout')), ME)
        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(self.store.get_connection(*self.a, 'gmail')['status'], 'ERROR')

    def test_sync_requires_a_connection(self):
        with self.assertRaises(InboxError):
            sync_connection(self.store, 'usr_b', 'ws_b', FakeClient([]), ME)

    def test_other_tenant_cannot_see_synced_data(self):
        sync_connection(self.store, *self.a, FakeClient([msg('m1', 't1', 'Ana <ana@example.test>', "Could you send it by Friday?")]), ME)
        self.assertEqual(self.store.list_commitments('usr_b', 'ws_b'), [])
        self.assertEqual(self.store.list_triage('usr_b', 'ws_b'), [])

    def test_morning_brief_is_empty_without_data_and_built_from_records(self):
        empty = morning_brief(self.store, *self.a, user_address=ME, tz_name='Europe/Berlin', now=NOW)
        self.assertTrue(all(v == 0 for v in empty['summary'].values()))
        self.assertTrue(all(v == [] for k, v in empty.items() if isinstance(v, list)))
        sync_connection(self.store, *self.a, FakeClient([msg('m1', 't1', 'Ana <ana@example.test>', "Could you send the revised proposal by Friday?")]), ME)
        brief = morning_brief(self.store, *self.a, user_address=ME, tz_name='Europe/Berlin', now=NOW)
        self.assertGreater(brief['summary']['needs_attention'] + brief['summary']['replies_required'] + len(brief['requests_to_me']), 0)
        self.assertEqual(brief['my_commitments'], [])
        self.assertEqual(brief['summary']['overdue'], 0)

    def test_draft_uses_only_facts_the_user_gave_and_asks_for_the_rest(self):
        thread = [{"id": "m1", "sender": "Ana <ana@example.test>", "subject": "Pricing",
                   "body": "What is the price for the pilot?", "sent_at": NOW}]
        d = draft_reply(thread, ME, "Best", "neutral", {})
        self.assertNotIn("EUR", d['body'])
        self.assertTrue(any('price' in m for m in d['missing_information']))
        filled = draft_reply(thread, ME, "Best", "neutral", {"price": "4200 EUR"})
        self.assertIn("4200 EUR", filled['body'])
        self.assertEqual(filled['missing_information'], [])

    def test_draft_greets_by_sender_first_name_only(self):
        thread = [{"id": "m1", "sender": "Ana Ruiz <ana@example.test>", "subject": "Hi", "body": "Thanks for the update.", "sent_at": NOW}]
        self.assertTrue(draft_reply(thread, ME, "Best", "neutral", {})['body'].startswith("Hi Ana,"))


if __name__ == '__main__':
    unittest.main()
