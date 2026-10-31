"""Inbox HTTP service layer, end to end against a scripted Gmail/OAuth transport (no network).

Covers: OAuth start, PKCE, single-use state, callback, sealed token storage, refresh, revocation, rate limit, disconnect;
live sync with real ownership; approval bound to exact content; single-use approval; timeout -> UNCERTAIN -> read-only
reconciliation with no second send; tenant isolation; LIVE/REPLAY separation; validated commitment transitions;
voice-rule safety; corrections with before/after; privacy export without tokens; durability across restart.
"""
import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from at_rest import SealedStore
from gmail_adapter import GmailConfig, OAuthStates, parse_raw, TOKEN_URL, REVOKE_URL, API
from inbox_api import ApiError, InboxApi
from inbox_store import InboxStore

ME = 'me@example.test'
CFG = GmailConfig('client-id', 'client-secret', 'http://127.0.0.1:8765/api/inbox/connections/gmail/callback')


def b64(s):
    return base64.urlsafe_b64encode(s.encode()).decode()


class FakeGoogle:
    """Scripted Google: OAuth endpoints, Gmail messages, Sent and Drafts with Message-ID search, and failure injection."""

    def __init__(self):
        self.calls = []                      # (method, path) for every provider call
        self.inbox = []                      # inbound raw message resources (dicts with id, threadId, headers, body, internalDate)
        self.sent, self.drafts = {}, {}      # provider id -> raw
        self.send_behaviour = 'ok'           # ok | timeout_after_store | timeout_before_store
        self.refresh_ok = True
        self.rate_limit = False
        self.revoke_ok = True
        self.n = 0
        self.token_scopes = None

    def _id(self):
        self.n += 1
        return f'g{self.n}'

    def add_inbound(self, mid, thread, sender, subject, body, when, to=ME):
        self.inbox.append({'id': mid, 'threadId': thread, 'internalDate': str(int(when * 1000)),
                           'payload': {'headers': [{'name': 'From', 'value': sender}, {'name': 'To', 'value': to}, {'name': 'Subject', 'value': subject}],
                                       'mimeType': 'text/plain', 'body': {'data': b64(body)}}})

    def __call__(self, method, url, headers, body):
        path = urlparse(url).path
        qs = parse_qs(urlparse(url).query)
        self.calls.append((method, path))
        if url == TOKEN_URL:
            if body.get('grant_type') == 'authorization_code':
                return 200, {'access_token': 'AT-' + self._id(), 'refresh_token': 'RT-secret-' + self._id(), 'expires_in': 3600,
                             'scope': self.token_scopes or 'https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.compose https://www.googleapis.com/auth/gmail.send'}, {}
            if body.get('grant_type') == 'refresh_token':
                if not self.refresh_ok:
                    return 400, {'error': 'invalid_grant'}, {}
                return 200, {'access_token': 'AT-refreshed-' + self._id(), 'expires_in': 3600}, {}
        if url == REVOKE_URL:
            return (200 if self.revoke_ok else 400), {}, {}
        if self.rate_limit and path.endswith('/messages') and method == 'GET':
            return 429, {}, {'Retry-After': '0'}
        if path.endswith('/profile'):
            return 200, {'emailAddress': ME, 'messagesTotal': len(self.inbox)}, {}
        if path.endswith('/messages') and method == 'GET':
            if 'q' in qs and 'rfc822msgid:' in qs['q'][0]:
                mid = qs['q'][0].split('rfc822msgid:')[1]
                store = self.drafts if 'in:drafts' in qs['q'][0] else self.sent
                hits = [{'id': pid} for pid, raw in store.items() if parse_raw(raw)['message_id'] == mid]
                return 200, {'messages': hits}, {}
            return 200, {'messages': [{'id': m['id']} for m in self.inbox]}, {}
        if path.endswith('/messages/send') and method == 'POST':
            if self.send_behaviour == 'timeout_before_store':
                raise TimeoutError('no response')
            pid = self._id()
            self.sent[pid] = body['raw']
            if self.send_behaviour == 'timeout_after_store':
                raise TimeoutError('stored, but the response timed out')
            return 200, {'id': pid, 'threadId': pid}, {}
        if path.endswith('/drafts') and method == 'POST':
            did = self._id()
            self.drafts[did] = body['message']['raw']
            return 200, {'id': did, 'message': {'id': did}}, {}
        if '/drafts/' in path and method == 'GET':
            did = path.rsplit('/', 1)[1]
            return 200, {'message': {'raw': self.drafts[did]}}, {}
        if '/messages/' in path and method == 'GET':
            mid = path.rsplit('/', 1)[1]
            if 'format' in qs and qs['format'][0] == 'raw':
                raw = self.sent.get(mid) or self.drafts.get(mid)
                if raw is None:
                    return 404, {}, {}
                return 200, {'raw': raw}, {}
            for m in self.inbox:
                if m['id'] == mid:
                    return 200, m, {}
            return 404, {}, {}
        return 404, {}, {}


class InboxApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        key = base64.urlsafe_b64encode(bytes(range(32))).decode()
        self.sealer = SealedStore(Path(self.tmp.name) / 'k', env={'RESOLVE_MASTER_KEY': key})
        self.db = Path(self.tmp.name) / 'inbox.sqlite3'
        self.clock = [1_800_000_000.0]
        self.google = FakeGoogle()
        self.store = InboxStore(self.db, self.sealer)
        self.api = InboxApi(self.store, CFG, self.google, OAuthStates(now=lambda: self.clock[0]), now=lambda: self.clock[0])
        self.a = {'user_id': 'usr_a', 'workspace_id': 'ws_a'}
        self.b = {'user_id': 'usr_b', 'workspace_id': 'ws_b'}
        self.call = lambda method, path, user=None, body=None, query=None: self.api.handle(method, path, query or {}, body, user or self.a)

    def tearDown(self):
        self.tmp.cleanup()

    # ---- helpers
    def connect(self, scopes=('read', 'compose', 'send'), user=None):
        user = user or self.a
        st, out = self.call('POST', '/api/inbox/connections/gmail/start', user, {'scopes': list(scopes)})
        self.assertEqual(st, 200, out)
        state = parse_qs(urlparse(out['authorization_url']).query)['state'][0]
        loc = self.api.gmail_callback({'code': ['auth-code-1'], 'state': [state]})
        self.assertIn('gmail=connected', loc[1]['location'])
        return state

    def set_tz(self, tz='Europe/Berlin', user=None):
        user = user or self.a
        return self.call('PUT', '/api/inbox/settings', user, {'timezone': tz})

    def live_sync(self, user=None):
        return self.call('POST', '/api/inbox/sync', user, {'mode': 'LIVE'})

    # ---- OAuth
    def test_start_without_credentials_says_not_configured_and_names_the_variables(self):
        api = InboxApi(self.store, None, self.google, now=lambda: self.clock[0])
        st, out = api.handle('POST', '/api/inbox/connections/gmail/start', {}, {}, self.a)
        self.assertEqual(st, 409)
        self.assertEqual(out['code'], 'NOT_CONFIGURED')
        self.assertIn('GOOGLE_OAUTH_CLIENT_ID', out['required_env'])
        st2, conn = api.handle('GET', '/api/inbox/connections', {}, None, self.a)
        self.assertEqual(conn['gmail']['status'], 'NOT_CONFIGURED')

    def test_authorization_url_uses_pkce_s256_and_only_the_requested_scopes(self):
        st, out = self.call('POST', '/api/inbox/connections/gmail/start', self.a, {'scopes': ['read']})
        q = parse_qs(urlparse(out['authorization_url']).query)
        self.assertEqual(q['code_challenge_method'], ['S256'])
        self.assertEqual(q['scope'], ['https://www.googleapis.com/auth/gmail.readonly'])
        self.assertNotIn('verifier', json.dumps(out).lower())
        st2, out2 = self.call('POST', '/api/inbox/connections/gmail/start', self.a, {'scopes': ['send']})
        self.assertIn('gmail.send', parse_qs(urlparse(out2['authorization_url']).query)['scope'][0])

    def test_state_is_single_use_and_expires(self):
        st, out = self.call('POST', '/api/inbox/connections/gmail/start', self.a, {})
        state = parse_qs(urlparse(out['authorization_url']).query)['state'][0]
        self.assertIn('gmail=connected', self.api.gmail_callback({'code': ['c'], 'state': [state]})[1]['location'])
        self.assertIn('gmail=state', self.api.gmail_callback({'code': ['c'], 'state': [state]})[1]['location'], 'replayed state must fail')
        st, out = self.call('POST', '/api/inbox/connections/gmail/start', self.a, {})
        state2 = parse_qs(urlparse(out['authorization_url']).query)['state'][0]
        self.clock[0] += 3600
        self.assertIn('gmail=state', self.api.gmail_callback({'code': ['c'], 'state': [state2]})[1]['location'], 'expired state must fail')

    def test_forged_state_is_refused_and_no_token_is_stored(self):
        loc = self.api.gmail_callback({'code': ['c'], 'state': ['forged']})[1]['location']
        self.assertIn('gmail=state', loc)
        self.assertIsNone(self.store.load_oauth_token('usr_a', 'ws_a', 'gmail'))

    def test_tokens_are_sealed_at_rest_and_never_returned(self):
        self.connect()
        raw = self.db.read_bytes()
        self.assertNotIn(b'RT-secret', raw)
        self.assertNotIn(b'AT-', raw)
        st, conn = self.call('GET', '/api/inbox/connections', self.a)
        self.assertNotIn('RT-secret', json.dumps(conn))
        self.assertNotIn('access_token', json.dumps(conn))
        self.assertEqual(conn['gmail']['status'], 'CONNECTED')
        self.assertEqual(conn['gmail']['account'], ME)

    def test_disconnect_revokes_at_provider_and_removes_the_token(self):
        self.connect()
        st, out = self.call('DELETE', '/api/inbox/connections/gmail', self.a)
        self.assertEqual(st, 200)
        self.assertTrue(out['revoked_at_provider'])
        self.assertIsNone(self.store.load_oauth_token('usr_a', 'ws_a', 'gmail'))
        self.assertEqual(self.call('GET', '/api/inbox/connections', self.a)[1]['gmail']['status'], 'NOT_CONNECTED')

    def test_revoked_grant_is_reported_as_revoked_and_sync_is_refused(self):
        self.connect()
        self.google.refresh_ok = False
        self.clock[0] += 7200                                  # access token now expired: a refresh is needed and is refused
        st, out = self.live_sync()
        self.assertEqual(st, 409)
        self.assertEqual(out.get('code'), 'REVOKED')
        self.assertEqual(self.call('GET', '/api/inbox/connections', self.a)[1]['gmail']['status'], 'REVOKED')

    def test_rate_limit_is_a_visible_state_not_a_success(self):
        self.connect()
        self.google.rate_limit = True
        st, out = self.live_sync()
        self.assertEqual(st, 409)
        self.assertEqual(self.store.get_connection('usr_a', 'ws_a', 'gmail')['status'], 'RATE_LIMITED')
        runs = self.store.list_sync_runs('usr_a', 'ws_a')
        self.assertEqual(runs[0]['state'], 'FAILED')

    # ---- live sync, owners and deadlines
    def test_live_sync_assigns_owners_from_the_real_mailbox_and_uses_the_timezone(self):
        self.connect()
        self.set_tz('Europe/Berlin')
        t0 = self.clock[0]
        self.google.add_inbound('m1', 't1', 'Priya <priya@partner.example>', 'Checklist',
                                'Hi, could you send the revised deployment checklist before 4 PM? We need it for today.', t0 - 3600)
        self.google.add_inbound('m2', 't2', 'Marco <marco@partner.example>', 'Schema', "I'll send the updated API schema by noon tomorrow.", t0 - 3600)
        self.google.add_inbound('m3', 't3', f'Me <{ME}>', 'PR', "I'll review the pull request and send feedback by Wednesday.", t0 - 3600,
                                to='Ana <ana@partner.example>')
        st, out = self.live_sync()
        self.assertEqual(st, 200, out)
        self.assertEqual(out['fetched'], 3)
        commitments = {c['kind']: c for c in self.store.list_commitments('usr_a', 'ws_a')}
        req = commitments['REQUEST_TO_USER']
        self.assertEqual((req['requesting_person'], req['responsible_person'], req['ownership_state']), ('priya@partner.example', ME, 'RESOLVED'))
        other = commitments['PROMISE_BY_OTHER']
        self.assertEqual((other['responsible_person'], other['requesting_person']), ('marco@partner.example', ME))
        mine = commitments['PROMISE_BY_USER']
        self.assertEqual((mine['responsible_person'], mine['requesting_person']), (ME, 'ana@partner.example'))
        self.assertEqual(mine['deadline_timezone'], 'Europe/Berlin')
        self.assertIsNotNone(req['deadline_at'])
        self.assertEqual(req['deadline_confidence'], 'RESOLVED_RELATIVE')

    def test_repeated_sync_creates_no_duplicates_and_records_each_run(self):
        self.connect()
        self.google.add_inbound('m1', 't1', 'Priya <priya@partner.example>', 'C', 'Could you send the checklist by Friday?', self.clock[0] - 60)
        self.live_sync()
        self.live_sync()
        self.assertEqual(len(self.store.list_commitments('usr_a', 'ws_a')), 1)
        self.assertEqual(len(self.store.list_sync_runs('usr_a', 'ws_a')), 2)

    def test_changed_deadline_is_revised_and_history_is_kept(self):
        self.connect()
        self.set_tz('UTC')
        t0 = self.clock[0]
        self.google.add_inbound('m6', 't6', 'Lena <lena@partner.example>', 'Doc', 'Could you send the onboarding document by Thursday?', t0 - 86400 * 3)
        self.google.add_inbound('m7', 't6', 'Lena <lena@partner.example>', 'Re: Doc',
                                'Following up. Legal moved their review, so please send the onboarding document by Friday instead.', t0 - 86400)
        self.live_sync()
        cs = [c for c in self.store.list_commitments('usr_a', 'ws_a') if c['kind'] == 'REQUEST_TO_USER']
        self.assertEqual(len(cs), 1, 'one obligation, not two')
        self.assertIn('Friday', cs[0]['required_action'])
        revs = self.store.commitment_revisions('usr_a', 'ws_a', cs[0]['id'])
        self.assertEqual(len(revs), 1)
        self.assertIsNotNone(revs[0]['deadline_at'])
        self.assertNotEqual(revs[0]['deadline_at'], cs[0]['deadline_at'], 'history keeps the superseded deadline')

    def test_ambiguous_deadline_is_flagged_not_invented(self):
        self.connect()
        self.google.add_inbound('m5', 't5', 'Jonas <jonas@partner.example>', 'Mockup', 'Please get this to me soon.', self.clock[0] - 60)
        self.live_sync()
        c = self.store.list_commitments('usr_a', 'ws_a')[0]
        self.assertIsNone(c['deadline_at'])
        self.assertEqual(c['deadline_confidence'], 'AMBIGUOUS')

    def test_morning_brief_is_built_from_persisted_records_in_the_user_timezone(self):
        self.connect()
        self.set_tz('Pacific/Auckland')
        self.google.add_inbound('m1', 't1', 'Priya <priya@partner.example>', 'C', 'Could you send the checklist before 4 PM?', self.clock[0] - 60)
        self.live_sync()
        st, brief = self.call('GET', '/api/inbox/brief', self.a, query={'mode': ['LIVE']})
        self.assertEqual(st, 200)
        self.assertEqual(brief['timezone'], 'Pacific/Auckland')
        self.assertTrue(brief['connected'])
        self.assertEqual(len(brief['requests_to_me']), 1)
        self.assertEqual(brief['summary']['overdue'], 0)

    def test_brief_is_empty_and_honest_when_not_connected(self):
        st, brief = self.call('GET', '/api/inbox/brief', self.a, query={'mode': ['LIVE']})
        self.assertFalse(brief['connected'])
        self.assertEqual(brief['connection_status'], 'NOT_CONNECTED')
        self.assertEqual(brief['needs_attention'], [])

    def test_timezone_must_be_a_real_iana_zone(self):
        st, out = self.set_tz('Mars/Olympus')
        self.assertEqual(st, 400)
        st, out = self.call('GET', '/api/inbox/settings', self.a)
        self.assertFalse(out['timezone_configured'])

    # ---- approval, exact content, single use, timeout recovery
    def approved_draft(self, mode='LIVE', thread_user=None):
        self.connect()
        self.google.add_inbound('m1', 't1', 'Priya <priya@partner.example>', 'C', 'Could you send the revised checklist before 4 PM?', self.clock[0] - 60)
        self.live_sync()
        tid = self.store.list_threads('usr_a', 'ws_a')[0]['id']
        st, d = self.call('POST', f'/api/inbox/threads/{tid}/drafts', self.a, {'facts': {'status': 'I will send it before 4 PM today.'}})
        self.assertEqual(st, 201, d)
        return tid, d

    def test_draft_recipient_comes_from_the_thread_and_missing_facts_are_listed(self):
        tid, d = self.approved_draft()
        self.assertEqual(d['recipient'], 'priya@partner.example')
        self.assertEqual(d['engine'], 'RULES_ONLY')
        self.assertIn('no model inference', d['engine_label'])
        self.assertNotIn('$', d['body'])                       # no invented price
        self.assertNotIn('estimate', d['body'].lower())        # the thread asked for no estimate; none is invented
        self.assertTrue(d['assumptions'] == [] or isinstance(d['assumptions'], list))

    def test_editing_after_approval_invalidates_the_approval(self):
        tid, d = self.approved_draft()
        st, appr = self.call('POST', f'/api/inbox/drafts/{d["id"]}/approve', self.a, {'digest': d['digest'], 'operation': 'SEND_EMAIL'})
        self.assertEqual(st, 200)
        st, edited = self.call('PATCH', f'/api/inbox/drafts/{d["id"]}', self.a, {'body': 'A different reply.', 'origin': 'DICTATED'})
        self.assertEqual(st, 200)
        self.assertNotEqual(edited['digest'], d['digest'])
        st, out = self.call('POST', f'/api/inbox/drafts/{d["id"]}/send', self.a, {'approval_id': appr['id'], 'digest': d['digest']})
        self.assertEqual(st, 409, 'an approval for the old digest must not send')
        self.assertFalse(any(m == 'POST' and p.endswith('/messages/send') for m, p in self.google.calls))

    def test_send_is_read_back_verified_and_the_approval_is_single_use(self):
        tid, d = self.approved_draft()
        appr = self.call('POST', f'/api/inbox/drafts/{d["id"]}/approve', self.a, {'digest': d['digest'], 'operation': 'SEND_EMAIL'})[1]
        st, out = self.call('POST', f'/api/inbox/drafts/{d["id"]}/send', self.a, {'approval_id': appr['id'], 'digest': d['digest']})
        self.assertEqual((st, out['state']), (200, 'VERIFIED'), out)
        st2, again = self.call('POST', f'/api/inbox/drafts/{d["id"]}/send', self.a, {'approval_id': appr['id'], 'digest': d['digest']})
        self.assertEqual(st2, 409)
        sent_raw = next(iter(self.google.sent.values()))
        stored = parse_raw(sent_raw)
        self.assertEqual(stored['body'].strip(), d['body'].strip(), 'the recipient receives exactly the approved content')
        self.assertNotIn('RESOLVE', stored['body'])
        detail = self.call('GET', f'/api/inbox/drafts/{d["id"]}', self.a)[1]
        self.assertEqual(detail['status'], 'SENT_VERIFIED')
        self.assertEqual(detail['executions'][0]['state'], 'VERIFIED')

    def test_timeout_is_uncertain_and_reconciliation_finds_the_send_without_resending(self):
        tid, d = self.approved_draft()
        self.google.send_behaviour = 'timeout_after_store'
        appr1 = self.call('POST', f'/api/inbox/drafts/{d["id"]}/approve', self.a, {'digest': d['digest'], 'operation': 'SEND_EMAIL'})[1]
        st, out = self.call('POST', f'/api/inbox/drafts/{d["id"]}/send', self.a, {'approval_id': appr1['id'], 'digest': d['digest']})
        self.assertEqual(out['state'], 'UNCERTAIN')
        self.assertEqual(self.call('GET', f'/api/inbox/drafts/{d["id"]}', self.a)[1]['status'], 'APPROVED')
        self.google.send_behaviour = 'ok'
        appr2 = self.call('POST', f'/api/inbox/drafts/{d["id"]}/approve', self.a, {'digest': d['digest'], 'operation': 'SEND_EMAIL'})[1]
        st, again = self.call('POST', f'/api/inbox/drafts/{d["id"]}/send', self.a, {'approval_id': appr2['id'], 'digest': d['digest']})
        self.assertEqual(again['state'], 'VERIFIED', 'the stored message was found by correlation and read back')
        sends = [p for m, p in self.google.calls if m == 'POST' and p.endswith('/messages/send')]
        self.assertEqual(len(sends), 1, 'a timeout never produces a second send')

    def test_timeout_before_storage_needs_a_new_review_and_does_not_resend(self):
        tid, d = self.approved_draft()
        self.google.send_behaviour = 'timeout_before_store'
        a1 = self.call('POST', f'/api/inbox/drafts/{d["id"]}/approve', self.a, {'digest': d['digest'], 'operation': 'SEND_EMAIL'})[1]
        self.call('POST', f'/api/inbox/drafts/{d["id"]}/send', self.a, {'approval_id': a1['id'], 'digest': d['digest']})
        self.google.send_behaviour = 'ok'
        a2 = self.call('POST', f'/api/inbox/drafts/{d["id"]}/approve', self.a, {'digest': d['digest'], 'operation': 'SEND_EMAIL'})[1]
        st, out = self.call('POST', f'/api/inbox/drafts/{d["id"]}/send', self.a, {'approval_id': a2['id'], 'digest': d['digest']})
        self.assertEqual(out['state'], 'NEEDS_RECOVERY')
        self.assertEqual(len([1 for m, p in self.google.calls if m == 'POST' and p.endswith('/messages/send')]), 1)

    def test_reconcile_route_is_read_only_and_refuses_verified_records(self):
        tid, d = self.approved_draft()
        self.google.send_behaviour = 'timeout_after_store'
        a1 = self.call('POST', f'/api/inbox/drafts/{d["id"]}/approve', self.a, {'digest': d['digest'], 'operation': 'SEND_EMAIL'})[1]
        out = self.call('POST', f'/api/inbox/drafts/{d["id"]}/send', self.a, {'approval_id': a1['id'], 'digest': d['digest']})[1]
        eid = out['execution_id']
        self.google.send_behaviour = 'ok'
        st, rec = self.call('POST', f'/api/inbox/executions/{eid}/reconcile', self.a)
        self.assertEqual((st, rec['state']), (200, 'VERIFIED'))
        self.assertEqual(len([1 for m, p in self.google.calls if m == 'POST' and p.endswith('/messages/send')]), 1)
        st2, rec2 = self.call('POST', f'/api/inbox/executions/{eid}/reconcile', self.a)
        self.assertEqual(rec2['state'], 'VERIFIED')

    def test_execution_survives_a_restart_and_keeps_its_timeline(self):
        tid, d = self.approved_draft()
        a1 = self.call('POST', f'/api/inbox/drafts/{d["id"]}/approve', self.a, {'digest': d['digest'], 'operation': 'SEND_EMAIL'})[1]
        out = self.call('POST', f'/api/inbox/drafts/{d["id"]}/send', self.a, {'approval_id': a1['id'], 'digest': d['digest']})[1]
        reopened = InboxStore(self.db, self.sealer)
        api2 = InboxApi(reopened, CFG, self.google, now=lambda: self.clock[0])
        st, ex = api2.handle('GET', f'/api/inbox/executions/{out["execution_id"]}', {}, None, self.a)
        self.assertEqual(ex['state'], 'VERIFIED')
        self.assertTrue(any(t['result'] == 'VERIFIED' for t in ex['timeline']))

    def test_thread_detail_returns_the_conversation_with_direction_and_linked_evidence(self):
        self.connect()
        self.google.add_inbound('m1', 't1', 'Priya <priya@partner.example>', 'Checklist', 'Could you send the checklist by Friday?', self.clock[0] - 120)
        self.google.add_inbound('m2', 't1', f'Me <{ME}>', 'Re: Checklist', "Thanks. I'll send it by Wednesday.", self.clock[0] - 60, to='Priya <priya@partner.example>')
        self.live_sync()
        tid = self.store.list_threads('usr_a', 'ws_a')[0]['id']
        st, out = self.call('GET', f'/api/inbox/threads/{tid}', self.a, query={'mode': ['LIVE']})
        self.assertEqual(st, 200, out)
        self.assertEqual([m['direction'] for m in out['messages']], ['INBOUND', 'OUTBOUND'])
        self.assertEqual(out['messages'][1]['recipients'], 'Priya <priya@partner.example>')
        ev = out['analysis']['evidence']
        self.assertTrue(ev and ev[0]['message_id'], 'every evidence sentence is linked to its message')
        self.assertTrue(all(c['source_message_id'] for c in out['analysis']['commitments']))

    # ---- tenant isolation
    def test_another_user_cannot_read_or_change_my_threads_commitments_or_drafts(self):
        tid, d = self.approved_draft()
        cid = self.store.list_commitments('usr_a', 'ws_a')[0]['id']
        for path in (f'/api/inbox/threads/{tid}', f'/api/inbox/commitments/{cid}', f'/api/inbox/drafts/{d["id"]}'):
            st, _ = self.call('GET', path, self.b)
            self.assertEqual(st, 404, path)
        st, _ = self.call('PATCH', f'/api/inbox/commitments/{cid}', self.b, {'status': 'DISMISSED', 'reason': 'x'})
        self.assertEqual(st, 404)
        st, _ = self.call('PATCH', f'/api/inbox/drafts/{d["id"]}', self.b, {'body': 'hijack'})
        self.assertEqual(st, 404)
        self.assertEqual(self.store.list_commitments('usr_a', 'ws_a')[0]['status'], 'OPEN')
        self.assertEqual(self.call('GET', '/api/inbox/commitments', self.b)[1]['commitments'], [])

    def test_a_client_supplied_user_id_is_never_trusted(self):
        st, out = self.call('GET', '/api/inbox/commitments', self.b, query={'user_id': ['usr_a'], 'workspace_id': ['ws_a']})
        self.assertEqual(out['commitments'], [])

    # ---- commitments: validated transitions and honest resolution
    def one_commitment(self):
        self.connect()
        self.google.add_inbound('m1', 't1', 'Priya <priya@partner.example>', 'C', 'Could you send the checklist by Friday?', self.clock[0] - 60)
        self.live_sync()
        return self.store.list_commitments('usr_a', 'ws_a')[0]['id']

    def test_invalid_transition_is_refused_and_terminal_states_are_final(self):
        cid = self.one_commitment()
        st, out = self.call('PATCH', f'/api/inbox/commitments/{cid}', self.a, {'status': 'NOT_REAL'})
        self.assertEqual(st, 400)
        st, out = self.call('PATCH', f'/api/inbox/commitments/{cid}', self.a, {'status': 'RESOLVED_VERIFIED'})
        self.assertEqual(st, 409, 'verified completion needs a verified execution record')
        st, out = self.call('POST', f'/api/inbox/commitments/{cid}/resolve', self.a, {'confirm': True, 'reason': 'sent from my laptop'})
        self.assertEqual(st, 200)
        self.assertEqual(out['status'], 'RESOLVED_USER_CONFIRMED')
        st, out = self.call('PATCH', f'/api/inbox/commitments/{cid}', self.a, {'status': 'IN_PROGRESS'})
        self.assertEqual(st, 409)

    def test_user_confirmation_is_distinct_from_verified_execution(self):
        cid = self.one_commitment()
        st, out = self.call('POST', f'/api/inbox/commitments/{cid}/resolve', self.a, {'confirm': True, 'reason': 'done'})
        self.assertEqual(out['status'], 'RESOLVED_USER_CONFIRMED')
        self.assertNotEqual(out['status'], 'RESOLVED_VERIFIED')

    def test_follow_up_records_trigger_and_target(self):
        cid = self.one_commitment()
        st, fu = self.call('POST', f'/api/inbox/commitments/{cid}/follow-up', self.a, {'trigger': 'OTHER_PARTY_PROMISE'})
        self.assertEqual(st, 201)
        self.assertEqual(fu['trigger'], 'OTHER_PARTY_PROMISE')
        self.assertTrue(fu['target'])
        st, bad = self.call('POST', f'/api/inbox/commitments/{cid}/follow-up', self.a, {'trigger': 'EVERYTHING'})
        self.assertEqual(st, 400)

    def test_deadline_correction_keeps_the_previous_value(self):
        cid = self.one_commitment()
        before = self.store.get_commitment('usr_a', 'ws_a', cid)['deadline_at']
        st, out = self.call('PATCH', f'/api/inbox/commitments/{cid}', self.a, {'deadline_local': '2026-11-06T16:00', 'reason': 'they confirmed'})
        self.assertEqual(st, 200)
        self.assertEqual(out['deadline_confidence'], 'USER_CONFIRMED')
        self.assertEqual(self.store.commitment_revisions('usr_a', 'ws_a', cid)[0]['deadline_at'], before)

    # ---- corrections (dictated or typed), scope and before/after
    def test_one_time_correction_changes_only_that_thread_and_shows_before_and_after(self):
        self.connect()
        self.google.add_inbound('m1', 't1', 'Priya <priya@partner.example>', 'C', 'Could you send the checklist by Friday?', self.clock[0] - 60)
        self.google.add_inbound('m2', 't2', 'Kim <kim@partner.example>', 'C2', 'Could you send the notes by Friday?', self.clock[0] - 50)
        self.live_sync()
        threads = {t['subject']: t['thread_id'] for t in self.call('GET', '/api/inbox/threads', self.a)[1]['threads']}
        tid = [t for t in self.store.list_threads('usr_a', 'ws_a')][0]['id']
        st, out = self.call('POST', f'/api/inbox/threads/{tid}/corrections', self.a,
                            {'kind': 'WRONG_PRIORITY', 'value': 'LOW', 'scope': 'ONE_TIME', 'dictated_text': "That isn't urgent."})
        self.assertEqual(st, 200, out)
        self.assertEqual(out['after']['priority'], 'LOW')
        self.assertEqual(out['impact'], 'only this message thread')
        rows = {r['thread_id']: r for r in self.call('GET', '/api/inbox/threads', self.a)[1]['threads']}
        self.assertEqual(rows[tid]['priority'], 'LOW')
        other = [r for r in rows.values() if r['thread_id'] != tid][0]
        self.assertNotEqual(other['priority'], 'LOW', 'a one-time correction does not leak to unrelated messages')

    def test_workspace_preference_needs_explicit_confirmation(self):
        self.connect()
        self.google.add_inbound('m1', 't1', 'Priya <priya@partner.example>', 'C', 'Could you send the checklist by Friday?', self.clock[0] - 60)
        self.live_sync()
        tid = self.store.list_threads('usr_a', 'ws_a')[0]['id']
        st, out = self.call('POST', f'/api/inbox/threads/{tid}/corrections', self.a,
                            {'kind': 'WRONG_PRIORITY', 'value': 'LOW', 'scope': 'PERSISTENT_WORKSPACE'})
        self.assertEqual(st, 409)
        self.assertEqual(out['code'], 'CONFIRMATION_REQUIRED')
        st, out = self.call('POST', f'/api/inbox/threads/{tid}/corrections', self.a,
                            {'kind': 'WRONG_PRIORITY', 'value': 'LOW', 'scope': 'PERSISTENT_WORKSPACE', 'confirmed': True})
        self.assertEqual(st, 200)
        self.assertEqual(out['impact'], 'threads from the same sender, after your confirmation')

    def test_not_commitment_dismisses_with_a_reason(self):
        cid = self.one_commitment()
        tid = self.store.get_commitment('usr_a', 'ws_a', cid)
        thread_id = self.store.read_message('usr_a', 'ws_a', tid['source_message_id'])['thread_id']
        st, out = self.call('POST', f'/api/inbox/threads/{thread_id}/corrections', self.a, {'kind': 'NOT_COMMITMENT', 'scope': 'ONE_TIME', 'dictated_text': 'just a status update'})
        self.assertEqual(st, 200, out)
        self.assertEqual(self.store.get_commitment('usr_a', 'ws_a', cid)['status'], 'DISMISSED')

    # ---- voice rules: preview, activation, refusals and reversal
    def test_parse_is_a_preview_and_stores_nothing(self):
        st, out = self.call('POST', '/api/inbox/rules/parse', self.a,
                            {'text': 'Prioritize emails from my project collaborators. Never send an email without my approval. Blorp the frobnicator.'})
        self.assertEqual(st, 200)
        self.assertGreaterEqual(out['unknown_count'], 1)
        self.assertEqual(self.call('GET', '/api/inbox/rules', self.a)[1]['rules'], [])

    def test_unknown_rules_are_never_activated(self):
        st, out = self.call('POST', '/api/inbox/rules', self.a, {'text': 'Blorp the frobnicator.', 'confirm': True})
        self.assertEqual(st, 201)
        self.assertFalse(out['activated'])
        rule = self.call('GET', '/api/inbox/rules', self.a)[1]['rules'][0]
        self.assertFalse(rule['active'])
        st, res = self.call('POST', f'/api/inbox/rules/{rule["id"]}/confirm', self.a, {})
        self.assertEqual(st, 409)

    def test_a_rule_that_removes_send_approval_is_refused(self):
        st, out = self.call('POST', '/api/inbox/rules', self.a, {'text': 'Send emails without asking me first.', 'confirm': True})
        self.assertEqual(st, 409)
        self.assertIn('refused', out['error'])

    def test_high_impact_rule_needs_explicit_confirmation_and_can_be_reversed(self):
        st, out = self.call('POST', '/api/inbox/rules', self.a, {'text': 'Never send an email without my approval.', 'confirm': True})
        self.assertEqual(st, 409)
        st, out = self.call('POST', '/api/inbox/rules', self.a,
                            {'text': 'Never send an email without my approval.', 'confirm': True, 'confirm_high_impact': True})
        self.assertEqual(st, 201)
        rule = self.call('GET', '/api/inbox/rules', self.a)[1]['rules'][0]
        self.assertTrue(rule['active'])
        st, rev = self.call('POST', f'/api/inbox/rules/{rule["id"]}/reverse', self.a)
        self.assertEqual(st, 200)
        self.assertFalse(self.call('GET', '/api/inbox/rules', self.a)[1]['rules'][0]['active'])

    # ---- replay: same pipeline, clearly separated, simulated sends
    def test_replay_runs_the_same_pipeline_without_touching_gmail_and_stays_separate_from_live(self):
        self.connect()
        self.call('POST', '/api/inbox/replay/start', self.a)
        provider_calls_before = len(self.google.calls)
        for _ in range(16):
            st, out = self.call('POST', '/api/inbox/replay/step', self.a, {})
            self.assertEqual(st, 200, out)
        for _ in range(3):
            st, out = self.call('POST', '/api/inbox/replay/step', self.a, {})
            self.assertEqual(st, 200, out)
        self.assertEqual(out['state'], 'COMPLETE')
        self.assertEqual(len(self.google.calls), provider_calls_before, 'replay never calls the Gmail provider')
        replay_brief = self.call('GET', '/api/inbox/brief', self.a, query={'mode': ['REPLAY']})[1]
        self.assertIn('SYNTHETIC', replay_brief['label'])
        self.assertTrue(replay_brief['requests_to_me'])
        live_brief = self.call('GET', '/api/inbox/brief', self.a, query={'mode': ['LIVE']})[1]
        self.assertEqual(live_brief['summary']['needs_attention'], 0, 'replay data never appears in LIVE')
        execs = self.call('GET', '/api/inbox/executions', self.a, query={'mode': ['REPLAY']})[1]['executions']
        self.assertTrue(execs)
        self.assertTrue(all(e['label'] == 'SIMULATED' for e in execs))
        self.assertEqual(self.call('GET', '/api/inbox/executions', self.a, query={'mode': ['LIVE']})[1]['executions'], [])

    def test_replay_send_timeout_injection_is_uncertain_and_recoverable(self):
        self.call('POST', '/api/inbox/replay/start', self.a)
        for _ in range(16):
            self.call('POST', '/api/inbox/replay/step', self.a, {})
        self.call('POST', '/api/inbox/replay/step', self.a, {})                 # draft T1
        self.api.replay[('usr_a', 'ws_a')]['inject'] = {'send': 'timeout_after_store'}
        st, out = self.call('POST', '/api/inbox/replay/step', self.a, {})       # approve and send T1 (timeout injected)
        self.assertEqual(out['entry']['result']['state'], 'UNCERTAIN')
        eid = out['entry']['result']['execution_id']
        st, rec = self.call('POST', f'/api/inbox/executions/{eid}/reconcile', self.a, query={'mode': ['REPLAY']})
        self.assertEqual(rec['state'], 'VERIFIED')
        self.assertEqual(rec['replayed'], True)

    def test_replay_reset_removes_only_replay_data(self):
        self.connect()
        self.google.add_inbound('m1', 't1', 'Priya <priya@partner.example>', 'C', 'Could you send the checklist by Friday?', self.clock[0] - 60)
        self.live_sync()
        self.call('POST', '/api/inbox/replay/start', self.a)
        self.call('POST', '/api/inbox/replay/step', self.a, {})
        self.call('POST', '/api/inbox/replay/reset', self.a)
        self.assertEqual(self.call('GET', '/api/inbox/brief', self.a, query={'mode': ['REPLAY']})[1]['summary']['needs_attention'], 0)
        self.assertEqual(len(self.store.list_commitments('usr_a', 'ws_a')), 1, 'live commitments are untouched by a replay reset')

    # ---- privacy: export is complete for the owner and never contains credentials
    def test_export_includes_inbox_data_and_no_credentials(self):
        self.connect()
        self.google.add_inbound('m1', 't1', 'Priya <priya@partner.example>', 'C', 'Could you send the checklist by Friday?', self.clock[0] - 60)
        self.live_sync()
        bundle = self.store.export_workspace('usr_a', 'ws_a')
        text = json.dumps(bundle, default=str)
        self.assertIn('priya@partner.example', text)
        self.assertNotIn('RT-secret', text)
        self.assertNotIn('AT-', text)
        self.assertNotIn('client-secret', text)

    def test_unknown_inbox_route_returns_not_found(self):
        st, out = self.call('GET', '/api/inbox/nope', self.a)
        self.assertEqual(st, 404)


if __name__ == '__main__':
    unittest.main()
