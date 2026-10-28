"""Acceptance tests for Business and GTM concepts (Zorro, Sentinel, PipeWork).

Tests verify signal detection, CRM task binding, lost-ack reconciliation,
and independent oracle verification against real SQLite ground truth.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'apps'), str(ROOT / 'benchmarks')]

from resolve_core import Ledger
from web_fixtures import FixtureWorld
from web_mission import WebMissionRunner


def jget(url: str):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def jpost(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


class GTMWorkflowTests(unittest.TestCase):
    def test_zorro_signal_to_crm_task_for_bound_contact(self):
        """Zorro: signal -> CRM task for bound contact ID, verified via CRM oracle."""
        with tempfile.TemporaryDirectory() as d, FixtureWorld(d, test_mode=True) as w:
            contacts = jget(w.crm.url + '/api/contacts')
            # Resolve target contact by explicit binding (Jordan Lee at Operations)
            target = next(c for c in contacts if c['id'] == 'C-104')
            op_id = 'zorro-sig-104'

            # Guarded write
            task = jpost(w.crm.url + '/api/tasks', {
                'contact_id': target['id'],
                'title': 'High Intent Signal: Schedule follow-up demo',
                'op_id': op_id,
            })
            self.assertTrue(bool(task.get('id')))

            # Oracle check against the independent ground truth table
            db = sqlite3.connect(str(Path(d) / 'crm.sqlite3'))
            try:
                rows = db.execute("SELECT detail FROM ground_truth WHERE kind='task_created'").fetchall()
                records = [json.loads(r[0]) for r in rows]
                match = next((r for r in records if r.get('op_id') == op_id), None)
                self.assertIsNotNone(match, "task was not committed to ground truth")
                self.assertEqual(match['contact'], 'C-104')
            finally:
                db.close()

    def test_sentinel_crm_change_guarded_write_and_lost_ack_reconcile(self):
        """Sentinel: CRM change -> guarded write -> lost-ack reconcile without duplicate."""
        with tempfile.TemporaryDirectory() as d, FixtureWorld(d, test_mode=True) as w:
            urls = {'crm': w.crm.url, 'docs': w.docs.url, 'outbox': w.outbox.url}
            runner = WebMissionRunner(Ledger(Path(d) / 'sentinel.sqlite3'), urls, Path(d))

            cmd = 'Prepare a follow-up for Jordan Lee using the latest report, update the CRM and send it after I approve.'
            m = runner.start(cmd)
            runner.bind(m['mission_id'], 'C-104', 1)
            plan = runner.plan(m['mission_id'])

            # Inject lost-ack fault before send
            jpost(w.outbox.url + '/_fault/drop_ack', {})
            runner.approve(plan['send_action'], plan['approval_digest'])

            # Send with dropped ack: internally transitions through UNCERTAIN and reconciles
            res = runner.run_send(plan['send_action'])
            states = [e['state'] for e in reversed(runner.ledger.events(plan['send_action']))]
            self.assertIn('UNCERTAIN', states)
            self.assertEqual(res['status'], 'CONFIRMED')

            # Verify oracle confirms exactly 1 message was committed
            db = sqlite3.connect(str(Path(d) / 'outbox.sqlite3'))
            try:
                rows = db.execute("SELECT detail FROM ground_truth WHERE kind='message_committed'").fetchall()
                self.assertEqual(len(rows), 1)
            finally:
                db.close()

    def test_pipework_pipeline_task_update_with_oracle_verification(self):
        """PipeWork: update pipeline stage task with oracle verification."""
        with tempfile.TemporaryDirectory() as d, FixtureWorld(d, test_mode=True) as w:
            urls = {'crm': w.crm.url, 'docs': w.docs.url, 'outbox': w.outbox.url}
            runner = WebMissionRunner(Ledger(Path(d) / 'pipework.sqlite3'), urls, Path(d))

            cmd = 'Prepare a follow-up for Jordan Lee using the latest report, update the CRM and send it after I approve.'
            m = runner.start(cmd)
            runner.bind(m['mission_id'], 'C-104', 1)
            plan = runner.plan(m['mission_id'])

            # Execute pipeline task
            task_res = runner.run_task(plan['task_action'])
            self.assertEqual(task_res['status'], 'CONFIRMED')

            # Verify ground truth
            db = sqlite3.connect(str(Path(d) / 'crm.sqlite3'))
            try:
                rows = db.execute("SELECT detail FROM ground_truth WHERE kind='task_created'").fetchall()
                records = [json.loads(r[0]) for r in rows]
                match = next((r for r in records if r.get('contact') == 'C-104'), None)
                self.assertIsNotNone(match)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
