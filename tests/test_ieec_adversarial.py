"""IEEC execution contract: state machine, guard reasons, the 21 adversarial scenarios, mutation checks,
real concurrency on the durable ledger, and a real DocumentStudio write through the mission store.

Deterministic: fixed scenarios, a controlled clock value per scenario, and Barriers instead of sleeps.
"""
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ieec_harness as h
import operation_contract as oc
from operation_contract import EffectLedger, IllegalTransition, OpState, transition

RESULTS = h.run_all()
BY_ID = {s.id: (s, n, r) for s, n, r in zip(h.SCENARIOS, RESULTS['naive'], RESULTS['resolve'])}


class StateMachine(unittest.TestCase):
    def test_legal_path_to_verified(self):
        st = OpState.PROPOSED
        for nxt in (OpState.AUTHORIZED, OpState.EXECUTING, OpState.EFFECT_OBSERVED, OpState.VERIFYING, OpState.VERIFIED):
            st = transition(st, nxt)
        self.assertIs(st, OpState.VERIFIED)

    def test_illegal_transitions_are_rejected(self):
        for cur, nxt in ((OpState.PROPOSED, OpState.EXECUTING), (OpState.AUTHORIZED, OpState.VERIFIED),
                         (OpState.EXECUTING, OpState.VERIFIED), (OpState.UNCERTAIN, OpState.EXECUTING),
                         (OpState.VERIFIED, OpState.EXECUTING), (OpState.REJECTED, OpState.AUTHORIZED)):
            with self.subTest(cur=cur.value, nxt=nxt.value), self.assertRaises(IllegalTransition):
                transition(cur, nxt)

    def test_terminal_states_have_no_outgoing_transitions(self):
        for st in oc.TERMINAL:
            self.assertEqual(oc.TRANSITIONS.get(st, frozenset()), frozenset(), st)

    def test_timeout_outcomes_cannot_become_verified_without_readback(self):
        # UNCERTAIN may only move to VERIFIED through reconciliation/read-back, never by simply being re-dispatched.
        self.assertNotIn(OpState.EXECUTING, oc.TRANSITIONS[OpState.UNCERTAIN])


class GuardReasons(unittest.TestCase):
    EXPECTED = {
        'wrong_recipient': 'WRONG_TARGET', 'file_changed_after_approval': 'TARGET_CHANGED',
        'calendar_revision_changed': 'REVISION_CHANGED', 'approval_revoked_while_waiting': 'APPROVAL_MISMATCH',
        'approval_expired': 'APPROVAL_EXPIRED', 'prompt_injection_changes_args': 'ARGS_MISMATCH',
        'resource_deleted_after_approval': 'TARGET_MISSING', 'cross_tenant_target': 'NOT_OWNER',
        'stale_worker_after_cancel': 'MISSION_INACTIVE', 'connector_revoked_mid_mission': 'PRECONDITION_FAILED',
        'deletion_races_queued_worker': 'MISSION_INACTIVE', 'duplicate_names_pick_wrong_one': 'TARGET_AMBIGUOUS',
    }

    def test_each_rejection_has_its_specific_reason(self):
        for sid, reason in self.EXPECTED.items():
            with self.subTest(sid=sid):
                _, _, r = BY_ID[sid]
                self.assertEqual(r['state'], 'rejected')
                self.assertEqual(r['reason'], reason)
                self.assertEqual(r['effects'], 0)


class AdversarialScenarios(unittest.TestCase):
    def test_every_scenario_is_correct_for_resolve_except_the_documented_misroute(self):
        for sid, (s, n, r) in BY_ID.items():
            if sid == 'wrong_resource_id_returned':
                continue
            with self.subTest(sid=sid):
                self.assertTrue(r['correct'], (sid, r['state'], r['reason'], r['effects']))
                self.assertFalse(r['false_success'])
                self.assertFalse(r['duplicate'])

    def test_known_limitation_misrouted_write_is_detected_not_prevented(self):
        # The provider itself wrote to another record. No pre-dispatch check can prevent that; RESOLVE must at least
        # refuse to report success and must surface UNCERTAIN.
        s, n, r = BY_ID['wrong_resource_id_returned']
        self.assertEqual(r['state'], 'uncertain')
        self.assertFalse(r['false_success'])
        self.assertTrue(r['wrong_entity'])

    def test_naive_baseline_really_fails_the_protected_cases(self):
        # Proves the benchmark discriminates: the naive executor violates these exact invariants.
        for sid in ('wrong_recipient', 'calendar_revision_changed', 'approval_expired', 'prompt_injection_changes_args',
                    'cross_tenant_target', 'stale_worker_after_cancel', 'timeout_after_effect', 'fabricated_executor_success'):
            with self.subTest(sid=sid):
                self.assertFalse(BY_ID[sid][1]['correct'], sid)

    def test_timeout_after_effect_is_reconciled_once_not_retried(self):
        _, n, r = BY_ID['timeout_after_effect']
        self.assertEqual((r['state'], r['effects'], r['reason']), ('verified', 1, 'RECONCILED_AFTER_TIMEOUT'))
        self.assertEqual((n['effects'], n['duplicate']), (2, True))

    def test_timeout_before_effect_requires_recovery_not_blind_retry(self):
        _, _, r = BY_ID['timeout_before_effect']
        self.assertEqual((r['state'], r['effects']), ('recovery_required', 0))

    def test_retry_of_completed_action_is_suppressed(self):
        _, n, r = BY_ID['retry_after_unacked_success']
        self.assertEqual((r['effects'], r['reason']), (1, 'DUPLICATE_SUPPRESSED_ALREADY_VERIFIED'))
        self.assertEqual(n['effects'], 2)

    def test_unverified_success_is_never_reported(self):
        for sid in ('fabricated_executor_success', 'contradictory_service_content', 'verifier_unavailable'):
            with self.subTest(sid=sid):
                _, _, r = BY_ID[sid]
                self.assertEqual(r['state'], 'uncertain')
                self.assertFalse(r['reported_success'])

    def test_session_expiry_mid_mission_is_uncertain_not_failed_or_success(self):
        _, _, r = BY_ID['browser_session_expires_mid_mission']
        self.assertEqual(r['state'], 'uncertain')
        self.assertTrue(r['reason'].startswith('EXECUTOR_ERROR'))


class Mutations(unittest.TestCase):
    """Disable one protection at a time; the scenarios that depend on it must fail. Patches are restored afterwards."""

    def _run(self, ids):
        return {sid: h.run_resolve(h.SCENARIOS[[s.id for s in h.SCENARIOS].index(sid)]) for sid in ids}

    def test_revision_and_approval_guard_disabled_lets_stale_actions_through(self):
        with mock.patch.object(oc, 'guard', lambda c, o: (True, 'AUTHORIZED')):
            out = self._run(['calendar_revision_changed', 'approval_expired', 'approval_revoked_while_waiting', 'wrong_recipient'])
        for sid, r in out.items():
            with self.subTest(sid=sid):
                self.assertFalse(r['correct'], f"{sid} should fail once the guard is disabled")

    def test_target_validation_disabled_allows_wrong_entity_and_stale_target(self):
        real = oc.guard
        def no_target_checks(c, o):
            if o.presented_target_id != c.target_id or o.target_version != c.target_version:
                return True, 'AUTHORIZED'        # mutant: target identity and version are not checked
            return real(c, o)
        with mock.patch.object(oc, 'guard', no_target_checks):
            out = self._run(['wrong_recipient', 'file_changed_after_approval'])
        self.assertTrue(all(not r['correct'] for r in out.values()))

    def test_ownership_check_disabled_lets_cross_tenant_write_through(self):
        real = oc.guard
        def no_owner(c, o):
            if (o.user_id != c.user_id) or (o.target_owner_id not in (None, c.user_id)):
                return True, 'AUTHORIZED'
            return real(c, o)
        with mock.patch.object(oc, 'guard', no_owner):
            out = self._run(['cross_tenant_target'])
        self.assertFalse(out['cross_tenant_target']['correct'])

    def test_replay_check_disabled_is_still_blocked_by_the_version_guard(self):
        # Defence in depth: the retry changes the record's version, so the guard rejects it even without the replay check.
        with mock.patch.object(EffectLedger, 'get', lambda self, u, w, k: None):
            out = self._run(['retry_after_unacked_success'])
        self.assertEqual(out['retry_after_unacked_success']['effects'], 1)
        self.assertEqual(out['retry_after_unacked_success']['reason'], 'TARGET_CHANGED')

    def test_replay_check_and_guard_disabled_duplicates_the_retry(self):
        # Only with the replay check AND the guard removed does the retry dispatch a second effect. The claim still writes
        # the record, so the conditional state update stays consistent with the first dispatch.
        def lax_claim(self, u, w, k, d):
            with self._db(immediate=True) as db:
                db.execute(f"INSERT OR REPLACE INTO {self.TABLE} VALUES (?,?,?,?,?,?,?)", (u, w, k, d, "executing", None, 0.0))
            return True, None, None
        with mock.patch.object(EffectLedger, 'get', lambda self, u, w, k: None), \
                mock.patch.object(EffectLedger, 'claim', lax_claim), \
                mock.patch.object(oc, 'guard', lambda c, o: (True, 'AUTHORIZED')):
            out = self._run(['retry_after_unacked_success'])
        self.assertEqual(out['retry_after_unacked_success']['effects'], 2)
        self.assertTrue(out['retry_after_unacked_success']['duplicate'])

    def test_readback_disabled_reports_fabricated_success(self):
        # Mutant: execute trusts the executor's return value instead of an independent read-back.
        def trusting_execute(c, observe, effect, verify, ledger, reconcile=None):
            real_execute = oc.execute
            return real_execute(c, observe, effect, lambda res: True, ledger, reconcile)
        with mock.patch.object(h, 'execute', trusting_execute):
            out = self._run(['fabricated_executor_success'])
        r = out['fabricated_executor_success']
        self.assertTrue(r['false_success'], 'trusting the executor must produce a false success (mutation is detected)')


class ConcurrencyOnDurableLedger(unittest.TestCase):
    def test_two_workers_released_together_dispatch_exactly_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = EffectLedger(Path(tmp) / 'e.sqlite3')
            provider = h.FakeProvider(h.base_records())
            s = h.SCENARIOS[0]
            contract = oc.Contract(user_id=h.USER, workspace_id='w1', mission_id='m1', revision=1, intent='x', operation='record.write',
                                   target_id='alice', target_label='Atlas report', target_version=1, args={'body': h.APPROVED_BODY},
                                   approval_digest=h.APPROVAL_DIGEST, idempotency_key='race:1', preconditions={'connector_active': True},
                                   expires_at=h.EXPIRES_AT, allowed_operations=frozenset({'record.write'}))
            gate = threading.Barrier(2)
            outcomes = []

            def observe():
                rec = provider.records['alice']
                return oc.Observed(user_id=h.USER, workspace_id='w1', mission_revision=1, mission_active=True, target_candidates=1,
                                   presented_target_id='alice', target_version=rec['version'], target_owner_id=h.USER,
                                   presented_approval_digest=h.APPROVAL_DIGEST, presented_args={'body': h.APPROVED_BODY},
                                   preconditions={'connector_active': True}, now=10.0)

            def worker():
                gate.wait(timeout=10)                       # both workers reach the dispatch point together
                outcomes.append(oc.execute(contract, observe, lambda: provider.write('alice', h.APPROVED_BODY),
                                           lambda r: provider.get('alice')['body'] == h.APPROVED_BODY, ledger))

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
            self.assertEqual(len(provider.effects), 1, 'the consequential effect must happen exactly once')
            # The worker that dispatched reports verified. The other is refused, not dispatched, and never claims success
            # on its own: it reports a suppressed duplicate.
            self.assertEqual(sum(1 for o in outcomes if o.dispatched), 1)
            winner = next(o for o in outcomes if o.dispatched)
            loser = next(o for o in outcomes if not o.dispatched)
            self.assertIs(winner.state, OpState.VERIFIED)
            # Which path the loser takes depends on scheduling: it may arrive while the winner is in flight (UNCERTAIN),
            # or after the winner finished (VERIFIED from the record). Both are non-dispatching replays; the invariant holds either way.
            self.assertIn('DUPLICATE_SUPPRESSED', loser.reason)


class RealDocumentWrite(unittest.TestCase):
    """The mission's report write goes through the contract on the real DocumentStudio and mission store."""

    def test_stale_revision_cannot_write_a_report(self):
        from test_brief_mission import Base, INTENT, wait_state
        import brief_mission as bm
        from brief_mission import MissionError
        class Case(Base):
            def runTest(self):
                m = self.start()
                m = wait_state(self.store, m['id'], self.u, self.w, {'AWAITING_APPROVAL'})
                self.store.revise(m['id'], self.u, self.w, INTENT + ' Focus on security.')
                row = self.store._row(m['id'], self.u, self.w)
                before = [d for d in self.studio.list_documents(self.u, self.w) if d.get('operation') == 'brief_report']
                with self.assertRaises(MissionError) as cm:
                    self.store._guarded_report_write(m['id'], 1, row, {'title': 'T', 'source': {'name': 'x'}}, 'deadbeef' * 8, b'%PDF-1.4 x')
                self.assertEqual(cm.exception.status_code, 409)
                after = [d for d in self.studio.list_documents(self.u, self.w) if d.get('operation') == 'brief_report']
                self.assertEqual(len(after), len(before), 'a stale revision must not create a document')
                self.assertIn('REVISION_CHANGED', str(cm.exception))
                kinds = [e['kind'] for e in self.store.get(m['id'], self.u, self.w)['events']]
                self.assertIn('EFFECT_REJECTED', kinds)                 # the refusal is recorded in the causal ledger
        t = Case(); t.setUp()
        try:
            t.runTest()
        finally:
            t.tearDown()


if __name__ == '__main__':
    unittest.main()
