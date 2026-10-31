"""benchmarks/computer_bench.py on a small seed set with REAL Chromium. Effects are counted by the benchmark's oracle from
each controlled app's own SQLite file, never from what an arm claims.

Fails if the faults stop provoking the naive arm (A must show wrong-target AND duplicate effects), if RESOLVE (C) ever
produces a wrong-target or duplicate effect, or if C's guards are removed (the ablated C must show the failures)."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'benchmarks')]
import computer_bench as cb
from controlled_apps import DROP_CONNECTION, ControlledApps


class ComputerBenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = cb.run_bench(seeds=[1], measure_rss=False)
        cls.s = cls.res['summary']

    def test_every_arm_ran_every_identical_case_without_harness_errors(self):
        self.assertEqual(len(self.res['rows']), len(cb.ARMS) * len(cb.SCENARIOS))
        self.assertEqual([r for r in self.res['rows'] if any(f.startswith('HARNESS') for f in r['final'])], [])
        self.assertEqual(cb.make_case('duplicate_name', 3), cb.make_case('duplicate_name', 3))   # seeded, identical per arm

    def test_resolve_has_no_wrong_target_or_duplicate_effects_and_no_false_success(self):
        c = self.s['C_resolve_ieec']
        self.assertEqual(c['wrong_target_effects'], 0, json.dumps(c))
        self.assertEqual(c['duplicate_effects'], 0, json.dumps(c))
        self.assertEqual(c['stale_or_unapproved_effects'], 0)
        self.assertEqual(c['false_success_claims'], 0)
        self.assertEqual(c['correct'], c['runs'])
        self.assertEqual(c['lost_ack_recovered_without_resend'], c['lost_ack_runs'])

    def test_the_faults_really_bite_the_naive_arm(self):
        a = self.s['A_naive']
        self.assertGreaterEqual(a['wrong_target_effects'], 1, json.dumps(a))
        self.assertGreaterEqual(a['duplicate_effects'], 1, json.dumps(a))
        self.assertGreaterEqual(a['false_success_claims'], 1)

    def test_target_guard_alone_does_not_stop_duplicates_or_stale_writes(self):
        b = self.s['B_target_guard']
        self.assertEqual(b['wrong_target_effects'], 0)
        self.assertGreaterEqual(b['duplicate_effects'], 1)
        self.assertGreaterEqual(b['stale_or_unapproved_effects'], 1)

    def test_lost_ack_is_uncertain_then_reconciled_read_only_never_resent(self):
        row = next(r for r in self.res['rows'] if r['arm'] == 'C_resolve_ieec' and r['scenario'] == 'lost_ack')
        self.assertEqual(row['final'], ['UNCERTAIN->reconciled:1'])
        self.assertEqual((row['effects'], row['resend_clicks']), (1, 0))

    def test_removing_resolve_guards_produces_the_failures(self):
        abl = cb.run_bench(seeds=[1], scenarios=('duplicate_submission', 'edited_after_approval'), arms=('C_resolve_ieec',),
                           ablate=frozenset({'single_use_approval', 'digest_binding'}), measure_rss=False)['summary']['C_resolve_ieec']
        self.assertGreaterEqual(abl['duplicate_effects'], 1, abl)          # approval no longer single-use
        self.assertGreaterEqual(abl['stale_or_unapproved_effects'], 1, abl)  # approval no longer bound to the digest
        self.assertGreaterEqual(abl['false_success_claims'], 1, abl)


class TestModeGatingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_fault_hooks_do_not_exist_outside_test_mode(self):
        apps = ControlledApps(str(Path(self.tmp.name) / 'a.db'))
        for hook in (lambda: apps.set_faults(load_delay_ms=1), apps.reset, lambda: apps.external_edit_task(1, 'x'),
                     lambda: apps.insert_task('Atlas', 't', '2026-01-01', 'x'), lambda: apps.revise_document(3, 'b', '2026-01-01')):
            with self.assertRaises(PermissionError):
                hook()
        self.assertEqual(apps.handle('POST', '/crm/api/followups', b'{"contact_id":1,"note":"n"}')[0], 404)
        self.assertNotIn(b'followup-note', apps.handle('GET', '/crm/contact/1', b'')[2])
        self.assertIn(b'250', apps.handle('GET', '/pm/?project=Atlas', b'')[2])

    def test_lost_ack_commits_the_write_and_returns_no_response(self):
        db = str(Path(self.tmp.name) / 'b.db')
        apps = ControlledApps(db, test_mode=True)
        apps.set_faults(drop_ack={'crm'})
        self.assertEqual(apps.handle('POST', '/crm/api/followups', b'{"contact_id":2,"note":"n"}')[0], DROP_CONNECTION)
        self.assertEqual(apps.handle('POST', '/crm/api/followups', b'{"contact_id":2,"note":"n"}')[0], 200)   # one-shot
        with sqlite3.connect(db) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM effect_log WHERE target_id=2').fetchone()[0], 2)
        apps.reset()
        with sqlite3.connect(db) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM effect_log').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
