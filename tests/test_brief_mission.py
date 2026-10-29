"""Brief -> architecture -> verified PDF mission: concurrency, identity binding, approvals, cancellation, deletion."""
import hashlib
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import brief_mission as bm
from brief_mission import BriefMissionStore, MissionError, validate_mermaid, extract_requirements, analyze_project
from document_studio import DocumentStudio, DocumentError

INTENT = 'Resolve, inspect my project brief, analyze the implementation issues, create a corrected architecture, generate a report and prepare it for print.'


def wait_state(store, mid, uid, wid, states, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        m = store.get(mid, uid, wid)
        if m['state'] in states:
            return m
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {states}; last={m['state']} err={m.get('error')}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.studio = DocumentStudio(Path(self.tmp.name) / 'docs')
        self.hooks = {}
        self.store = BriefMissionStore(Path(self.tmp.name) / 'm.sqlite3', self.studio, hooks=self.hooks)
        self.u, self.w = 'usr_a', 'ws_a'
        self.brief = self.store.seed_sample_brief(self.u, self.w)

    def tearDown(self):
        for g in getattr(self, 'gates', []):
            g.set()
        self.store.close()
        self.tmp.cleanup()

    def start(self, **kw):
        return self.store.start(self.u, self.w, INTENT, document_id=kw.pop('document_id', self.brief['id']), **kw)

    def to_approval(self, **kw):
        m = self.start(**kw)
        return wait_state(self.store, m['id'], self.u, self.w, {'AWAITING_APPROVAL', 'FAILED'})

    def run_full(self):
        m = self.to_approval()
        self.assertEqual(m['state'], 'AWAITING_APPROVAL', m.get('error'))
        self.store.approve(m['id'], self.u, self.w, m['plan']['digest'])
        return wait_state(self.store, m['id'], self.u, self.w, {'CONFIRMED', 'FAILED'})


class PureAnalysisTests(unittest.TestCase):
    def test_analyzer_finds_planted_defects_with_line_numbers(self):
        a = analyze_project(bm.BUNDLED_PROJECTS['sample_project'])
        rules = {(f['rule'], f['file'], f['line']) for f in a['findings']}
        self.assertIn(('HARDCODED_SECRET', 'inventory/auth.py', 1), rules)
        self.assertIn(('SQL_STRING_BUILDING', 'inventory/db.py', 7), rules)
        self.assertIn(('SILENT_EXCEPT', 'inventory/db.py', 9), rules)
        self.assertNotIn('inventory-prod-key-2024', str(a))  # secret value never copied into evidence

    def test_requirement_extraction_and_mermaid_validation(self):
        reqs = extract_requirements(bm.SAMPLE_BRIEF.read_text())
        self.assertEqual([r['id'] for r in reqs], ['R1', 'R2', 'R3', 'R4', 'R5'])
        self.assertEqual(reqs[0]['category'], 'secrets')
        validate_mermaid('flowchart TD\n    a["A"]\n    b["B"]\n    a --> b')
        for bad in ('', 'hello', 'flowchart TD\n a --> b', 'flowchart TD\n a["A"]\n a ==> a', 'flowchart TD\n a["A"\n'):
            with self.subTest(bad=bad), self.assertRaises(MissionError):
                validate_mermaid(bad)


class HappyPathTests(Base):
    def test_full_mission_confirms_with_verified_chain_and_provenance(self):
        m = self.run_full()
        self.assertEqual(m['state'], 'CONFIRMED', m.get('error'))
        self.assertEqual(m['chain_broken'], [])
        out = self.studio.get_document(self.u, self.w, m['output_doc_id'])
        self.assertEqual(out['sha256'], m['output_sha256'])
        self.assertEqual(out['source_ids'], self.brief['id'])                       # provenance link
        raw = self.studio.read_pdf(self.u, self.w, out['id'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), m['output_sha256'])       # independent re-hash
        text = self.studio.extract_text(self.u, self.w, out['id'])
        for needle in ('ISS-3', 'HARDCODED_SECRET', 'flowchart TD', self.brief['sha256']):
            self.assertIn(needle, text.replace('\n', ''))
        self.assertEqual(m['receipt']['print']['state'], 'PRINT_READY')
        self.assertFalse(m['receipt']['print']['physical_print_verified'])
        self.assertEqual(m['plan']['status'], 'DRAFT')

    def test_print_event_does_not_claim_physical_print(self):
        m = self.run_full()
        m = self.store.record_print_dialog(m['id'], self.u, self.w)
        ev = [e for e in m['events'] if e['kind'] == 'PRINT_DIALOG_OPENED'][0]
        self.assertIn('NOT verified', ev['detail']['note'])
        self.assertFalse(m['receipt']['print']['physical_print_verified'])


class ConcurrencyTests(Base):
    def test_reader_and_analyzer_really_overlap(self):
        # Both workers must be inside their node at the same time to pass the barrier; a serial executor would time out.
        barrier = threading.Barrier(2, timeout=5)
        self.hooks['DOCUMENT_READER'] = barrier.wait
        self.hooks['REPOSITORY_ANALYZER'] = barrier.wait
        m = self.to_approval()
        self.assertEqual(m['state'], 'AWAITING_APPROVAL', m.get('error'))
        n = {x['node']: x for x in m['nodes']}
        a, b = n['DOCUMENT_READER'], n['REPOSITORY_ANALYZER']
        self.assertLess(max(a['started_at'], b['started_at']), min(a['finished_at'], b['finished_at']))  # recorded timestamps overlap
        self.assertNotEqual(a['worker'], b['worker'])
        self.assertGreaterEqual(n['ARCHITECTURE_PLANNER']['started_at'], max(a['finished_at'], b['finished_at']))  # dependency respected

    def test_failure_in_one_worker_fails_mission_without_plan(self):
        def boom():
            raise RuntimeError('provider crashed')
        self.hooks['REPOSITORY_ANALYZER'] = boom
        m = self.to_approval()
        self.assertEqual(m['state'], 'FAILED')
        self.assertIsNone(m['plan'])
        self.assertTrue(any(e['kind'] == 'NODE_FAILED' for e in m['events']))


class IdentityBindingTests(Base):
    def test_same_name_documents_require_stable_id(self):
        other = self.store.seed_sample_brief(self.u, self.w)  # second doc with the same name
        self.assertEqual(other['name'], self.brief['name'])
        with self.assertRaises(MissionError) as c:
            self.store.start(self.u, self.w, INTENT, document_name=self.brief['name'])
        self.assertEqual(c.exception.status_code, 409)
        self.assertEqual({d['id'] for d in c.exception.candidates}, {self.brief['id'], other['id']})
        m = self.store.start(self.u, self.w, INTENT, document_id=other['id'])
        self.assertEqual(m['source_doc_id'], other['id'])

    def test_stale_expected_hash_rejected(self):
        with self.assertRaises(MissionError) as c:
            self.start(expected_sha256='0' * 64)
        self.assertEqual(c.exception.status_code, 409)

    def test_source_replaced_after_binding_is_rejected(self):
        self.hooks['DOCUMENT_READER'] = lambda: (Path(self.studio.files_dir) / f"{self.brief['id']}.pdf").write_bytes(b'%PDF-1.4 tampered')
        m = self.to_approval()
        self.assertEqual(m['state'], 'FAILED')
        self.assertRegex(m['error'], r'changed|refusing')

    def test_wrong_source_in_output_is_rejected_by_verifier(self):
        real = bm.compose_pdf

        def wrong_source(plan, mission):
            forged = {**plan, 'source': {**plan['source'], 'sha256': 'f' * 64}}
            return real(forged, mission)
        with mock.patch.object(bm, 'compose_pdf', wrong_source):
            m = self.run_full()
        self.assertEqual(m['state'], 'FAILED')
        self.assertIn('references_correct_source_sha', m['error'])
        self.assertEqual([d for d in self.studio.list_documents(self.u, self.w) if d['operation'] == 'brief_report'], [])  # no unverified artifact left

    def test_chain_invariant_rejects_tampered_receipt(self):
        m = self.run_full()
        tampered = {**m, 'receipt': {**m['receipt'], 'approved_plan_digest': 'x' * 64}}
        self.assertIn('approved digest differs from plan', bm.verify_chain(tampered, True))
        self.assertIn('output document not owned by mission tenant', bm.verify_chain(m, False))
        self.assertIn('approval bound to a different intent revision', bm.verify_chain({**m, 'intent_revision': 9}, True))


class ApprovalTests(Base):
    def test_stale_digest_rejected_and_logged(self):
        m = self.to_approval()
        with self.assertRaises(MissionError) as c:
            self.store.approve(m['id'], self.u, self.w, 'deadbeef')
        self.assertEqual(c.exception.status_code, 409)
        self.assertEqual(self.store.get(m['id'], self.u, self.w)['state'], 'AWAITING_APPROVAL')
        self.assertTrue(any(e['kind'] == 'APPROVAL_REJECTED_STALE' for e in self.store.get(m['id'], self.u, self.w)['events']))

    def test_intent_revision_invalidates_prior_approval_digest(self):
        m = self.to_approval()
        old_digest = m['plan']['digest']
        self.store.revise(m['id'], self.u, self.w, INTENT + ' Focus on security.')
        m2 = wait_state(self.store, m['id'], self.u, self.w, {'AWAITING_APPROVAL'})
        self.assertEqual(m2['intent_revision'], 2)
        self.assertNotEqual(m2['plan']['digest'], old_digest)  # revision is part of what the user must re-approve
        self.assertEqual(m2['plan']['digest'], bm.plan_digest(m2['plan']))
        # same plan content but approval presented for the old digest is refused
        with self.assertRaises(MissionError):
            self.store.approve(m['id'], self.u, self.w, old_digest)

    def test_double_approval_is_not_executed_twice(self):
        m = self.to_approval()
        self.store.approve(m['id'], self.u, self.w, m['plan']['digest'])
        with self.assertRaises(MissionError):
            self.store.approve(m['id'], self.u, self.w, m['plan']['digest'])
        done = wait_state(self.store, m['id'], self.u, self.w, {'CONFIRMED', 'FAILED'})
        self.assertEqual(len([d for d in self.studio.list_documents(self.u, self.w) if d['operation'] == 'brief_report']), 1)
        self.assertEqual(done['state'], 'CONFIRMED')


class CancellationAndDeletionTests(Base):
    def test_cancel_rejects_late_planner_result(self):
        gate = threading.Event()
        self.gates = [gate]
        self.hooks['ARCHITECTURE_PLANNER'] = lambda: gate.wait(5)
        m = self.start()
        end = time.time() + 10
        while time.time() < end and not any(n['node'] == 'ARCHITECTURE_PLANNER' for n in self.store.get(m['id'], self.u, self.w)['nodes']):
            time.sleep(0.02)
        self.store.cancel(m['id'], self.u, self.w)
        gate.set()
        time.sleep(0.5)
        final = self.store.get(m['id'], self.u, self.w)
        self.assertEqual(final['state'], 'CANCELLED')
        self.assertIsNone(final['plan'])
        self.assertTrue(any(e['kind'] == 'LATE_RESULT_REJECTED' for e in final['events']))

    def test_deleting_mission_during_composition_leaves_no_orphan_pdf(self):
        gate = threading.Event()
        self.gates = [gate]
        self.hooks['REPORT_COMPOSER'] = lambda: gate.wait(5)
        m = self.to_approval()
        self.store.approve(m['id'], self.u, self.w, m['plan']['digest'])
        time.sleep(0.3)
        self.store.delete(m['id'], self.u, self.w)
        gate.set()
        time.sleep(1.0)
        self.assertEqual([d for d in self.studio.list_documents(self.u, self.w) if d['operation'] == 'brief_report'], [])
        with self.assertRaises(MissionError):
            self.store.get(m['id'], self.u, self.w)
        with sqlite3.connect(self.store.db_path) as db:  # worker must not resurrect rows
            self.assertEqual(db.execute('SELECT COUNT(*) FROM mission_events').fetchone()[0], 0)

    def test_purge_workspace_removes_missions_only_for_that_tenant(self):
        mine = self.run_full()
        b_brief = self.store.seed_sample_brief('usr_b', 'ws_b')
        theirs = self.store.start('usr_b', 'ws_b', INTENT, document_id=b_brief['id'])
        wait_state(self.store, theirs['id'], 'usr_b', 'ws_b', {'AWAITING_APPROVAL'})
        self.assertEqual(self.store.purge(self.u, self.w), 1)
        with self.assertRaises(MissionError):
            self.store.get(mine['id'], self.u, self.w)
        self.assertEqual(self.store.get(theirs['id'], 'usr_b', 'ws_b')['state'], 'AWAITING_APPROVAL')

    def test_restart_marks_in_flight_missions_failed_not_resumed(self):
        gate = threading.Event()
        self.gates = [gate]
        self.hooks['DOCUMENT_READER'] = lambda: gate.wait(3)
        m = self.start()
        time.sleep(0.2)
        reopened = BriefMissionStore(self.store.db_path, self.studio)   # simulated process restart
        got = reopened.get(m['id'], self.u, self.w)
        self.assertEqual(got['state'], 'FAILED')
        self.assertIn('restarted', got['error'])
        gate.set()


class TenantIsolationTests(Base):
    def test_other_tenant_cannot_use_or_see_mission_or_document(self):
        m = self.run_full()
        for args in (('usr_b', 'ws_b'), (self.u, 'ws_other'), ('usr_b', self.w)):
            with self.subTest(args=args):
                for op in (lambda: self.store.get(m['id'], *args), lambda: self.store.cancel(m['id'], *args),
                           lambda: self.store.approve(m['id'], *args, 'x'), lambda: self.store.delete(m['id'], *args),
                           lambda: self.store.record_print_dialog(m['id'], *args)):
                    with self.assertRaises(MissionError) as c:
                        op()
                    self.assertEqual(c.exception.status_code, 404)
                self.assertEqual(self.store.list(*args), [])
                with self.assertRaises(DocumentError):
                    self.studio.read_pdf(*args, m['output_doc_id'])
        with self.assertRaises(DocumentError):   # B cannot bind A's brief as the source of B's mission
            self.store.start('usr_b', 'ws_b', INTENT, document_id=self.brief['id'])

    def test_unsupported_intent_unknown_project_and_quota(self):
        with self.assertRaises(MissionError):
            self.store.start(self.u, self.w, 'Order me a pizza', document_id=self.brief['id'])
        with self.assertRaises(MissionError):
            self.start(project='../../etc')
        gate = threading.Event()
        self.gates = [gate]
        self.hooks['DOCUMENT_READER'] = lambda: gate.wait(5)
        for _ in range(bm.MAX_ACTIVE_PER_TENANT):
            self.start()
        with self.assertRaises(MissionError) as c:
            self.start()
        self.assertEqual(c.exception.status_code, 429)
        gate.set()


if __name__ == '__main__':
    unittest.main()
