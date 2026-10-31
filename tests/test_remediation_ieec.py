"""IEEC remediation regression tests: strict canonical digests, frozen contracts, fail-closed allowlist and owner checks,
tenant-scoped idempotency with conflict detection, a real cross-process claim race, and failing reconciliation."""
import math
import multiprocessing as mp
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from operation_contract import (Contract, EffectLedger, EffectTimeout, Observed, OpState, canonical_digest, execute, guard)

NOW = 10.0


def contract(**kw):
    base = dict(user_id='usr_a', workspace_id='ws_a', mission_id='m1', revision=1, intent='edit alice', operation='record.write',
                target_id='alice', target_label='Alice', target_version='v1', args={'body': 'hello'}, approval_digest='D1',
                idempotency_key='key-1', preconditions={'connector_active': True}, expires_at=1000.0,
                allowed_operations=frozenset({'record.write'}))
    base.update(kw)
    return Contract(**base)


def observed(c, **kw):
    base = dict(user_id=c.user_id, workspace_id=c.workspace_id, mission_revision=c.revision, mission_active=True, target_candidates=1,
                presented_target_id=c.target_id, target_version=c.target_version, target_owner_id=c.user_id,
                presented_approval_digest=c.approval_digest, presented_args=dict(c.args), preconditions={'connector_active': True}, now=NOW)
    base.update(kw)
    return Observed(**base)


class StrictSerialization(unittest.TestCase):
    def test_non_finite_numbers_are_refused_not_hashed(self):
        for bad in (float('nan'), float('inf')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                contract(args={'body': bad})

    def test_unsupported_objects_are_refused_not_stringified(self):
        with self.assertRaises(TypeError):
            canonical_digest({'x': object()})
        with self.assertRaises(ValueError):
            contract(args={'body': math.nan})

    def test_digest_is_stable_and_changes_with_any_bound_field(self):
        base = contract()
        self.assertEqual(base.digest(), contract().digest())
        for kw in ({'user_id': 'usr_b'}, {'workspace_id': 'ws_b'}, {'mission_id': 'm2'}, {'revision': 2}, {'target_id': 'bob'},
                   {'target_version': 'v2'}, {'args': {'body': 'other'}}, {'operation': 'record.delete', 'allowed_operations': frozenset({'record.delete'})},
                   {'approval_digest': 'D2'}, {'preconditions': {'connector_active': False}}, {'expires_at': 999.0},
                   {'idempotency_key': 'key-2'}, {'intent': 'different'}):
            with self.subTest(kw=kw):
                self.assertNotEqual(base.digest(), contract(**kw).digest())

    def test_display_label_is_not_part_of_the_approval(self):
        self.assertEqual(contract().digest(), contract(target_label='Renamed').digest())


class FrozenContract(unittest.TestCase):
    def test_caller_cannot_mutate_an_approved_contract(self):
        args = {'body': 'hello'}
        c = contract(args=args)
        args['body'] = 'changed after approval'                       # the caller's dict is detached
        self.assertEqual(c.args['body'], 'hello')
        with self.assertRaises(TypeError):
            c.args['body'] = 'x'                                       # read-only view
        with self.assertRaises(TypeError):
            c.preconditions['connector_active'] = False
        with self.assertRaises(Exception):
            c.user_id = 'usr_b'                                        # dataclass is frozen

    def test_missing_identity_fields_are_construction_errors(self):
        for kw in ({'user_id': ''}, {'workspace_id': '   '}, {'mission_id': None}, {'target_id': ''}, {'idempotency_key': ''}):
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                contract(**kw)


class FailClosedAuthorization(unittest.TestCase):
    def test_empty_operation_allowlist_cannot_be_constructed(self):
        for allowed in (frozenset(), set(), None):
            with self.subTest(allowed=allowed), self.assertRaises(ValueError):
                contract(allowed_operations=allowed)

    def test_missing_owner_identity_is_not_an_owner(self):
        c = contract()
        ok, reason = guard(c, observed(c, target_owner_id=None))
        self.assertEqual((ok, reason), (False, 'NOT_OWNER'))

    def test_zero_candidates_is_missing_and_many_is_ambiguous(self):
        c = contract()
        self.assertEqual(guard(c, observed(c, target_candidates=0))[1], 'TARGET_MISSING')
        self.assertEqual(guard(c, observed(c, target_candidates=2))[1], 'TARGET_AMBIGUOUS')

    def test_invalid_expiry_values_are_refused(self):
        for bad in (float('nan'), float('inf'), 0, -5, None):
            with self.subTest(bad=bad), self.assertRaises((ValueError, TypeError)):
                contract(expires_at=bad)

    def test_operation_outside_allowlist_is_rejected_by_the_guard(self):
        c = contract(allowed_operations=frozenset({'record.write'}), operation='record.write')
        other = contract(operation='record.delete', allowed_operations=frozenset({'record.write'}))
        self.assertEqual(guard(other, observed(other))[1], 'NOT_AUTHORIZED_OPERATION')


class TenantScopedIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'effects.sqlite3'
        self.effects = []

    def tearDown(self):
        self.tmp.cleanup()

    def effect(self, c):
        def do():
            self.effects.append((c.user_id, c.workspace_id, c.target_id, c.args['body']))
            return {'id': c.target_id, 'body': c.args['body']}
        return do

    def verify(self, c):
        return lambda res: res['body'] == c.args['body']

    def test_same_key_different_contract_is_an_idempotency_conflict_and_no_effect(self):
        ledger = EffectLedger(self.db)
        first = contract()
        self.assertIs(execute(first, lambda: observed(first), self.effect(first), self.verify(first), ledger).state, OpState.VERIFIED)
        changed = contract(args={'body': 'a different body'}, approval_digest='D1')
        out = execute(changed, lambda: observed(changed), self.effect(changed), self.verify(changed), ledger)
        self.assertEqual((out.state, out.reason, out.dispatched), (OpState.REJECTED, 'IDEMPOTENCY_CONFLICT', False))
        self.assertEqual(len(self.effects), 1)

    def test_other_tenant_with_the_same_key_never_replays_this_tenants_result(self):
        ledger = EffectLedger(self.db)
        a = contract(user_id='usr_a', workspace_id='ws_a')
        execute(a, lambda: observed(a), self.effect(a), self.verify(a), ledger)
        b = contract(user_id='usr_b', workspace_id='ws_b', target_id='alice')
        out = execute(b, lambda: observed(b, user_id='usr_b', workspace_id='ws_b', target_owner_id='usr_b'),
                      self.effect(b), self.verify(b), ledger)
        self.assertTrue(out.dispatched, 'a different tenant must not be answered from another tenant record')
        self.assertEqual(out.state, OpState.VERIFIED)
        self.assertEqual(len(self.effects), 2)

    def test_two_independent_ledger_instances_share_one_record(self):
        first, second = EffectLedger(self.db), EffectLedger(self.db)
        c = contract()
        execute(c, lambda: observed(c), self.effect(c), self.verify(c), first)
        out = execute(c, lambda: observed(c), self.effect(c), self.verify(c), second)
        self.assertEqual((out.state, out.dispatched), (OpState.VERIFIED, False))
        self.assertEqual(len(self.effects), 1)

    def test_reconciliation_that_raises_persists_uncertain_not_executing(self):
        ledger = EffectLedger(self.db)
        c = contract()

        def timeout_effect():
            raise EffectTimeout('no response')

        def broken_reconcile():
            raise RuntimeError('provider read failed')

        out = execute(c, lambda: observed(c), timeout_effect, self.verify(c), ledger, broken_reconcile)
        self.assertEqual(out.state, OpState.UNCERTAIN)
        self.assertEqual(ledger.state_of(c.user_id, c.workspace_id, c.idempotency_key), 'uncertain')
        # the uncertain record is never dispatched again
        again = execute(c, lambda: observed(c), self.effect(c), self.verify(c), ledger)
        self.assertEqual((again.dispatched, again.state), (False, OpState.UNCERTAIN))

    def test_conditional_update_refuses_a_stale_expected_state(self):
        from operation_contract import StaleRecord
        ledger = EffectLedger(self.db)
        c = contract()
        ledger.claim(c.user_id, c.workspace_id, c.idempotency_key, c.digest())
        # legal transition, but the record is EXECUTING, not EFFECT_OBSERVED: the write must be refused
        with self.assertRaises(StaleRecord):
            ledger.set_state(c.user_id, c.workspace_id, c.idempotency_key, c.digest(), OpState.VERIFYING, 'x', expected=OpState.EFFECT_OBSERVED)
        with self.assertRaises(StaleRecord):
            ledger.set_state(c.user_id, c.workspace_id, c.idempotency_key, contract(approval_digest='D9').digest(), OpState.UNCERTAIN, 'x')


def _claim_and_record(db_path, log_path, barrier, result_queue):
    """Separate OS process: claim the same key and perform the effect if this process wins the claim."""
    ledger = EffectLedger(db_path)
    c = contract(idempotency_key='race-key')
    barrier.wait(timeout=20)

    def effect():
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        return {'id': c.target_id, 'body': c.args['body']}

    out = execute(c, lambda: observed(c), effect, lambda r: r['body'] == c.args['body'], ledger)
    result_queue.put((out.state.value, out.dispatched, out.reason))


class CrossProcessRace(unittest.TestCase):
    def test_two_processes_releasing_together_produce_exactly_one_effect(self):
        ctx = mp.get_context('fork')
        with tempfile.TemporaryDirectory() as tmp:
            db, log = str(Path(tmp) / 'effects.sqlite3'), str(Path(tmp) / 'effects.log')
            barrier, queue = ctx.Barrier(2), ctx.Queue()
            procs = [ctx.Process(target=_claim_and_record, args=(db, log, barrier, queue)) for _ in range(2)]
            for p in procs:
                p.start()
            results = [queue.get(timeout=30) for _ in procs]
            for p in procs:
                p.join(30)
            with open(log) as f:
                effects = [line for line in f.read().splitlines() if line]
            self.assertEqual(len(effects), 1, f'effect count across processes: {effects}')
            self.assertEqual(sum(1 for r in results if r[1]), 1, results)
            self.assertIn('verified', [r[0] for r in results])
            self.assertTrue(all(r[0] in ('verified', 'uncertain') for r in results), results)


if __name__ == '__main__':
    unittest.main()
