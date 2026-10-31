"""Focused guard-coverage tests found by scripts/mutation_check.py.

Each test pins one critical guard that the rest of the suite let a mutant disable without failing (see
docs/MUTATION_REPORT.md). They drive public entry points and inject faults only through inputs, test doubles or a
monkeypatched collaborator; production code is unchanged. The comment on each class names the mutant it kills."""
import base64
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import brief_mission as bm
from at_rest import SealedStore
from brief_mission import BriefMissionStore, MissionError as BriefError
from document_studio import DocumentStudio
from gmail_adapter import build_raw_message, correlation_id, parse_raw
from inbox_store import InboxError, InboxStore
from operation_contract import Contract, Observed, guard
from resolve_core import Action, Ledger, uid
from send_broker import SendBroker, operation_key
from web_mission import MissionError, WebMissionRunner

INTENT = ('Resolve, inspect my project brief, analyze the implementation issues, create a corrected architecture, '
          'generate a report and prepare it for print.')


# ---------------------------------------------------------------- web mission (no browser: approval/guards run before any page)

class WebMissionGuards(unittest.TestCase):
    """M01 (_guard_revision), M02 (stale-revision approval), M05 (exact approval digest)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.ledger = Ledger(root / 'ledger.sqlite3')
        self.runner = WebMissionRunner(self.ledger, {}, root)
        self.mid = uid('wm')
        self.mission = {'mission_id': self.mid, 'goal': 'send the report to Ada Lovelace', 'intent_revision': 1, 'person': 'Ada Lovelace',
                        'wants_send': True, 'status': 'ACTIVE', 'actions': [],
                        'binding': {'contact_id': 'c-1', 'email': 'ada@example.test', 'basis': 'unique name match'}}
        self.ledger.write_json('missions', self.mid, self.mission)

    def tearDown(self):
        self.tmp.cleanup()

    def _send_action(self, status: str) -> str:
        args = {'to_addr': 'ada@example.test', 'attachment_id': 'r-1', 'attachment_sha256': 'a' * 64, 'body': 'Hi Ada, attached.', 'op_id': uid('op')}
        a = Action(uid('act'), self.mid, 1, 'outbox_send', 'c-1', 'BrowserExecutor', 'outbox', None, args, None,
                   {'message_to': args['to_addr'], 'sha256': args['attachment_sha256']}, 'NON_IDEMPOTENT', 'consequential')
        self.ledger.create_action(a)
        for st in ('BOUND', 'NEEDS_APPROVAL', 'AUTHORIZED')[: ('BOUND', 'NEEDS_APPROVAL', 'AUTHORIZED').index(status) + 1]:
            self.ledger.transition(a.action_id, st, {})
        self.mission['actions'].append(a.action_id)
        self.ledger.write_json('missions', self.mid, self.mission)
        return a.action_id

    def _bump_revision(self):
        """The intent moved on (a correction from another process) while this action still carries revision 1."""
        self.mission['intent_revision'] = 2
        self.ledger.write_json('missions', self.mid, self.mission)

    def test_approval_with_the_wrong_digest_is_refused(self):
        aid = self._send_action('NEEDS_APPROVAL')
        a = self.ledger.action(aid)
        other_recipient = WebMissionRunner.action_digest({**a, 'arguments': {**a['arguments'], 'to_addr': 'eve@example.test'}})
        for presented in ('0' * 64, other_recipient):
            with self.subTest(presented=presented[:8]), self.assertRaises(MissionError) as ctx:
                self.runner.approve(aid, presented)
            self.assertEqual(ctx.exception.code, 'DIGEST_MISMATCH')
        self.assertEqual(self.ledger.action(aid)['status'], 'NEEDS_APPROVAL')
        self.assertEqual(self.runner.approve(aid, WebMissionRunner.action_digest(a))['status'], 'AUTHORIZED')

    def test_approval_of_a_superseded_revision_is_refused_even_with_its_own_digest(self):
        aid = self._send_action('NEEDS_APPROVAL')
        own_digest = WebMissionRunner.action_digest(self.ledger.action(aid))   # exact digest of the rev-1 action
        self._bump_revision()
        with self.assertRaises(MissionError) as ctx:
            self.runner.approve(aid, own_digest)
        self.assertEqual(ctx.exception.code, 'STALE_REVISION')
        self.assertEqual(self.ledger.action(aid)['status'], 'NEEDS_APPROVAL')

    def test_an_authorized_send_from_a_superseded_revision_never_dispatches(self):
        aid = self._send_action('AUTHORIZED')
        self._bump_revision()
        with self.assertRaises(MissionError) as ctx:
            self.runner.run_send(aid)
        self.assertEqual(ctx.exception.code, 'SUPERSEDED')
        self.assertNotIn('DISPATCHING', {e['state'] for e in self.ledger.events(aid)})
        self.assertEqual(self.ledger.action(aid)['status'], 'AUTHORIZED')

    def test_a_task_from_a_superseded_revision_never_dispatches(self):
        a = Action(uid('act'), self.mid, 1, 'crm_create_task', 'c-1', 'BrowserExecutor', 'crm', None,
                   {'title': 'Follow up with Ada Lovelace', 'op_id': uid('op')}, None, {'task_for_contact': 'c-1'}, 'IDEMPOTENT_WRITE', 'moderate')
        self.ledger.create_action(a)
        self.ledger.transition(a.action_id, 'BOUND', {})
        self._bump_revision()

        def no_browser():
            raise AssertionError('a superseded action must be refused before any browser is opened')
        self.runner.browser = no_browser
        with self.assertRaises(MissionError) as ctx:
            self.runner.run_task(a.action_id)
        self.assertEqual(ctx.exception.code, 'SUPERSEDED')
        self.assertEqual(self.ledger.action(a.action_id)['status'], 'BOUND')


# ---------------------------------------------------------------- brief mission (revision CAS, output-hash links)

def wait_state(store, mid, u, w, states, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        m = store.get(mid, u, w)
        if m['state'] in states:
            return m
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {states}; last={m['state']} err={m.get('error')}")


class BriefMissionGuards(unittest.TestCase):
    """M03 (revision compare-and-set), M16 (chain: read-back hash link), M17 (EFFECT_VERIFIER hash comparison)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = DocumentStudio(Path(self.tmp.name) / 'docs')
        self.store = BriefMissionStore(Path(self.tmp.name) / 'm.sqlite3', self.studio, hooks={})
        self.u, self.w = 'usr_a', 'ws_a'
        self.brief = self.store.seed_sample_brief(self.u, self.w)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def to_approval(self):
        m = self.store.start(self.u, self.w, INTENT, document_id=self.brief['id'])
        m = wait_state(self.store, m['id'], self.u, self.w, {'AWAITING_APPROVAL', 'FAILED'})
        self.assertEqual(m['state'], 'AWAITING_APPROVAL', m.get('error'))
        return m

    def reports(self):
        return [d for d in self.studio.list_documents(self.u, self.w) if d['operation'] == 'brief_report']

    def test_approval_read_at_an_old_revision_cannot_commit_onto_a_newer_revision(self):
        m = self.to_approval()
        real_row = self.store._row

        def read_then_revision_lands(mid, u, w):
            row = real_row(mid, u, w)            # the approval reads the mission at revision 1 ...
            self.store._row = real_row
            self.store.revise(mid, u, w, INTENT + ' Focus on secret handling first.')   # ... a correction lands ...
            wait_state(self.store, mid, u, w, {'AWAITING_APPROVAL'})                     # ... and its new plan is ready
            return row
        self.store._row = read_then_revision_lands
        with self.assertRaises(BriefError) as ctx:
            self.store.approve(m['id'], self.u, self.w, m['plan']['digest'])
        self.assertEqual(ctx.exception.status_code, 409)
        after = self.store.get(m['id'], self.u, self.w)
        self.assertEqual((after['state'], after['intent_revision']), ('AWAITING_APPROVAL', 2))
        self.assertNotIn('PLAN_APPROVED', {e['kind'] for e in after['events']})

    def test_effect_verifier_rejects_a_read_back_hash_that_differs_from_the_written_document(self):
        real = bm.verify_output

        def forged_hash(*a, **kw):
            res = real(*a, **kw)
            return {**res, 'output_sha256': 'e' * 64}         # content checks pass, but the re-hash names other bytes
        m = self.to_approval()
        with mock.patch.object(bm, 'verify_output', forged_hash):
            self.store.approve(m['id'], self.u, self.w, m['plan']['digest'])
            done = wait_state(self.store, m['id'], self.u, self.w, {'CONFIRMED', 'FAILED'})
        self.assertEqual(done['state'], 'FAILED')
        self.assertTrue(done['error'].startswith('Output failed independent verification'), done['error'])
        self.assertIn('VERIFICATION_FAILED', {e['kind'] for e in done['events'] if e['node'] == 'EFFECT_VERIFIER'})
        self.assertEqual(self.reports(), [])                   # no unverified artifact left behind

    def test_causal_chain_requires_the_read_back_hash_to_match_the_recorded_output(self):
        m = self.to_approval()
        self.store.approve(m['id'], self.u, self.w, m['plan']['digest'])
        m = wait_state(self.store, m['id'], self.u, self.w, {'CONFIRMED', 'FAILED'})
        self.assertEqual(m['state'], 'CONFIRMED', m.get('error'))
        self.assertEqual(bm.verify_chain(m, True), [])
        ver = m['receipt']['verification']
        for broken in ({**m, 'output_sha256': 'e' * 64},
                       {**m, 'output_sha256': None},
                       {**m, 'receipt': {**m['receipt'], 'verification': {**ver, 'output_sha256': 'e' * 64}}}):
            with self.subTest(output=broken['output_sha256'], readback=broken['receipt']['verification']['output_sha256'][:8]):
                self.assertIn('output hash not matched by read-back', bm.verify_chain(broken, True))


# ---------------------------------------------------------------- inbox approvals and the Gmail send broker (scripted double)

class ScriptedGmail:
    """Minimal provider double: `sent` is Gmail's Sent folder keyed by provider id."""
    def __init__(self):
        self.sent, self.calls = {}, []

    def find_by_correlation(self, key):
        self.calls.append('find')
        return [pid for pid, raw in self.sent.items() if parse_raw(raw)['message_id'] == correlation_id(key)]

    def send_raw(self, raw):
        self.calls.append('send')
        pid = f'p{len(self.sent) + 1}'
        self.sent[pid] = raw
        return {'id': pid, 'threadId': pid}

    def get_message_raw(self, pid):
        self.calls.append('readback')
        return parse_raw(self.sent[pid])


class InboxApprovalGuards(unittest.TestCase):
    """M09 (consumed approval bound to the presented digest), M11 (approval of a non-current digest), M21 (Sent check)."""
    U, W = 'usr_a', 'ws_a'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        key = base64.urlsafe_b64encode(bytes(range(32))).decode()
        self.store = InboxStore(Path(self.tmp.name) / 'i.sqlite3', SealedStore(Path(self.tmp.name) / 'k', env={'RESOLVE_MASTER_KEY': key}))
        cid = self.store.upsert_connection(self.U, self.W, 'gmail', 'CONNECTED', ['gmail.readonly', 'gmail.compose'])['id']
        m = self.store.upsert_message(self.U, self.W, cid, 't1', 'pm1', 'Sam <sam@partner.io>', time.time(), 'Proposal', 'Could you send the proposal?')
        self.now = 1_000_000.0
        self.gmail = ScriptedGmail()
        self.broker = SendBroker(self.store, self.gmail, lambda: self.now)
        self.draft = self.store.create_draft(self.U, self.W, m['thread_id'], 'sam@partner.io', 'Proposal', 'Here is the proposal.')

    def tearDown(self):
        self.tmp.cleanup()

    def approve(self):
        return self.store.approve(self.U, self.W, self.draft['id'], self.draft['digest'], 'SEND_EMAIL', self.now)['id']

    def test_approving_a_digest_that_is_not_the_current_draft_is_refused(self):
        with self.assertRaises(InboxError) as ctx:
            self.store.approve(self.U, self.W, self.draft['id'], 'f' * len(self.draft['digest']), 'SEND_EMAIL', self.now)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.store.get_draft(self.U, self.W, self.draft['id'])['status'], 'DRAFT')

    def test_a_send_presenting_a_different_digest_than_the_approval_sends_nothing(self):
        aid = self.approve()
        with self.assertRaises(InboxError) as ctx:
            self.broker.send_email(self.U, self.W, aid, self.draft['id'], '0' * len(self.draft['digest']))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.gmail.calls, [])
        self.assertEqual(self.store.list_executions(self.U, self.W), [])

    def test_a_message_already_in_sent_is_verified_and_never_sent_again(self):
        d = self.store.get_draft(self.U, self.W, self.draft['id'])
        key = operation_key('SEND_EMAIL', self.draft['digest'])
        self.gmail.sent['p0'] = build_raw_message(d['recipient'], d['subject'], d['body'], key, d.get('cc') or None)   # an earlier attempt landed
        out = self.broker.send_email(self.U, self.W, self.approve(), self.draft['id'], self.draft['digest'])
        self.assertEqual(out['state'], 'VERIFIED', out)
        self.assertEqual(out['provider_id'], 'p0')
        self.assertNotIn('send', self.gmail.calls)
        self.assertEqual(list(self.gmail.sent), ['p0'])


# ---------------------------------------------------------------- IEEC operation contract

def _contract(**kw):
    base = dict(user_id='usr_a', workspace_id='ws_a', mission_id='m1', revision=1, intent='edit alice', operation='record.write',
                target_id='alice', target_label='Alice', target_version='v1', args={'body': 'hello'}, approval_digest='D1',
                idempotency_key='key-1', preconditions={}, expires_at=1000.0, allowed_operations=frozenset({'record.write'}))
    base.update(kw)
    return Contract(**base)


def _observed(c, **kw):
    base = dict(user_id=c.user_id, workspace_id=c.workspace_id, mission_revision=c.revision, mission_active=True, target_candidates=1,
                presented_target_id=c.target_id, target_version=c.target_version, target_owner_id=c.user_id,
                presented_approval_digest=c.approval_digest, presented_args=dict(c.args), preconditions={}, now=10.0)
    base.update(kw)
    return Observed(**base)


class ContractOwnerGuard(unittest.TestCase):
    """M14: the executing tenant must be the approving tenant, workspace included."""

    def test_same_user_in_another_workspace_is_not_the_owner(self):
        c = _contract()
        self.assertEqual(guard(c, _observed(c)), (True, 'AUTHORIZED'))
        self.assertEqual(guard(c, _observed(c, workspace_id='ws_b')), (False, 'NOT_OWNER'))

    def test_another_user_is_not_the_owner_even_when_the_target_owner_matches(self):
        c = _contract()
        self.assertEqual(guard(c, _observed(c, user_id='usr_b')), (False, 'NOT_OWNER'))


# ---------------------------------------------------------------- computer tasks: save_report digest (real Chromium, controlled app)

class SaveReportDigestGuard(unittest.TestCase):
    """M08: save_report writes only with the exact approved proposal digest."""

    @classmethod
    def setUpClass(cls):
        from cloud_browser import CloudBrowserSession
        from computer_tasks import BROWSER_CAPS, ComputerTaskRunner, browser_services
        from controlled_apps import serve_controlled
        from net_policy import NetPolicy, origin_of
        from tool_registry import ToolContext, default_registry
        cls.tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmpdir.name)
        cls.server, base, cls.apps = serve_controlled(str(tmp / 'apps.db'))
        cls.session = CloudBrowserSession('sbx_guard', 'user_a', tmp / 'profile', policy=NetPolicy(allowed_origins=frozenset({origin_of(base)})))
        ctx = ToolContext(user_id='user_a', workspace_id='ws_1', mission_id='m1', capabilities=frozenset(BROWSER_CAPS),
                          services=browser_services(cls.session), record=lambda r: 'ev')
        cls.runner = ComputerTaskRunner(default_registry(), ctx, {'pm': base, 'research': base, 'crm': base}, cls.apps.verify, lambda: False)

    @classmethod
    def tearDownClass(cls):
        cls.session.close()
        cls.server.shutdown()
        cls.tmpdir.cleanup()

    def test_a_forged_digest_saves_no_report(self):
        cmd = 'Open the research portal, find the correct document and save a report to my workspace.'
        res = self.runner.run(cmd, approved_digest='0' * 64)
        self.assertFalse(res['ok'])
        self.assertEqual(res['error']['code'], 'APPROVAL_MISMATCH')
        self.assertEqual(self.apps.reports(), [])


if __name__ == '__main__':
    unittest.main()
