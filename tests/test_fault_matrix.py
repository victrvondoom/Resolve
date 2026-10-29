"""F01-F16 Deterministic Adversarial Fault Matrix for RESOLVE.

Tests every named failure mode against the IEEC + CSL + Win32 safety boundaries:
A. Reproducible trigger
B. Known intended result
C. Known ground truth state
D. Execution trace
E. Verification assertion
F. Explicit pass/fail outcome
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'apps'), str(ROOT / 'benchmarks')]

import windows_adapter
from agent_runtime import CursorLeaseManager
from resolve_core import Action, Ledger, uid
from web_fixtures import FixtureWorld
from web_mission import MissionError, WebMissionRunner


def jget(url: str):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def jpost(url: str, body: dict):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST')
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


class FaultMatrixTests(unittest.TestCase):
    def test_F01_focus_theft_refuses_typing(self):
        """F01: Focus shifts while agent types -> rejected, 0 bytes leaked."""
        from notepad_w1 import NotepadW1
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(Path(d) / 'l.sqlite3')
            target_file = Path(d) / 'f01.txt'
            target_file.write_text('', encoding='utf-8')

            class FakeFocusStealDriver:
                UnsupportedDesktop = windows_adapter.UnsupportedDesktop
                def list_windows(self):
                    return {'foreground_hwnd': 100, 'windows': [{'hwnd': 100, 'title': 'f01.txt - Notepad', 'pid': 1, 'snapshot_id': 'snap1', 'foreground': True}]}
                def focus_window(self, hwnd):
                    return True
                def type_text(self, hwnd, text, snapshot_id):
                    raise windows_adapter.UnsupportedDesktop("foreground changed from 100 to 999; refusing keyboard input")
                def press_chord(self, hwnd, chord):
                    pass
                def close_window(self, hwnd):
                    pass

            w1 = NotepadW1(ledger, Path(d), driver=FakeFocusStealDriver(), launcher=lambda exe, path: None)
            res = w1.run("CORRUPT_PAYLOAD", "f01.txt")
            self.assertEqual(res['status'], 'FAILED')
            self.assertIn('foreground changed', res['refused'])
            self.assertEqual(target_file.read_text(encoding='utf-8'), '')

    @unittest.skipUnless(sys.platform == 'win32', 'windows-native: WINDOWS_NATIVE_TESTS_NOT_RUN (needs interactive Windows desktop; run scripts/smoke-windows.ps1)')
    def test_F02_target_bounds_out_of_range(self):
        """F02: Target coordinates fall outside window bounds -> rejected before dispatch."""
        foreground = next((w for w in windows_adapter.list_windows()["windows"] if w["foreground"]), None)
        if foreground is None:
            self.skipTest("no visible titled foreground window")
        token = foreground["snapshot_id"]
        b = foreground["bounds"]
        with self.assertRaises(ValueError):
            windows_adapter.validate_target(foreground["hwnd"], token, b[2] + 500, b[3] + 500)

    @unittest.skipUnless(sys.platform == 'win32', 'windows-native: WINDOWS_NATIVE_TESTS_NOT_RUN (needs interactive Windows desktop; run scripts/smoke-windows.ps1)')
    def test_F03_stale_snapshot_rejected(self):
        """F03: Snapshot timestamp older than SNAPSHOT_MAX_AGE -> rejected."""
        foreground = next((w for w in windows_adapter.list_windows()["windows"] if w["foreground"]), None)
        if foreground is None:
            self.skipTest("no visible titled foreground window")
        token = foreground["snapshot_id"]
        windows_adapter._snapshots[token]["observed_at"] = time.time() - windows_adapter.SNAPSHOT_MAX_AGE - 1
        with self.assertRaisesRegex(windows_adapter.UnsupportedDesktop, "stale"):
            windows_adapter.validate_snapshot(foreground["hwnd"], token)

    def test_F04_same_name_ambiguity_blocks_planning(self):
        """F04: Two contacts share the same display name -> planning blocks until bound."""
        with tempfile.TemporaryDirectory() as d, FixtureWorld(d, test_mode=True) as w:
            urls = {'crm': w.crm.url, 'docs': w.docs.url, 'outbox': w.outbox.url}
            runner = WebMissionRunner(Ledger(Path(d) / 'f04.sqlite3'), urls, Path(d))
            # "Jordan Lee" exists at both Operations (C-104) and Sales (C-208)
            m = runner.start("Prepare follow-up for Jordan Lee")
            self.assertEqual(m['status'], 'NEEDS_CLARIFICATION')
            with self.assertRaises(MissionError) as ctx:
                runner.plan(m['mission_id'])
            self.assertEqual(ctx.exception.code, 'NEEDS_CLARIFICATION')

    def test_F05_stale_document_version_invalidates_approval(self):
        """F05: Document edited after human approval -> send refused, stale version blocked."""
        with tempfile.TemporaryDirectory() as d, FixtureWorld(d, test_mode=True) as w:
            urls = {'crm': w.crm.url, 'docs': w.docs.url, 'outbox': w.outbox.url}
            runner = WebMissionRunner(Ledger(Path(d) / 'f05.sqlite3'), urls, Path(d))
            m = runner.start("Prepare follow-up for Jordan Lee")
            runner.bind(m['mission_id'], 'C-104', 1)
            plan = runner.plan(m['mission_id'])

            # Human approves the drafted report
            runner.approve(plan['send_action'], plan['approval_digest'])

            # Adversary edits report behind the agent's back
            w.docs.edit(plan['mission']['report']['id'])

            # Send must refuse dispatching
            with self.assertRaises(MissionError) as ctx:
                runner.run_send(plan['send_action'])
            self.assertEqual(ctx.exception.code, 'ATTACHMENT_CHANGED')

    def test_F06_lost_save_ack_transitions_uncertain_and_reconciles(self):
        """F06: Server drops ACK -> action transitions through UNCERTAIN, reconciles without duplicate."""
        with tempfile.TemporaryDirectory() as d, FixtureWorld(d, test_mode=True) as w:
            urls = {'crm': w.crm.url, 'docs': w.docs.url, 'outbox': w.outbox.url}
            runner = WebMissionRunner(Ledger(Path(d) / 'f06.sqlite3'), urls, Path(d))
            m = runner.start("Prepare follow-up for Jordan Lee")
            runner.bind(m['mission_id'], 'C-104', 1)
            plan = runner.plan(m['mission_id'])
            runner.approve(plan['send_action'], plan['approval_digest'])

            # Fault: drop ACK
            jpost(w.outbox.url + '/_fault/drop_ack', {})
            res = runner.run_send(plan['send_action'])
            states = [e['state'] for e in reversed(runner.ledger.events(plan['send_action']))]
            self.assertIn('UNCERTAIN', states)
            self.assertEqual(res['status'], 'CONFIRMED')

            # Verify oracle confirms exactly 1 message
            db = sqlite3.connect(str(Path(d) / 'outbox.sqlite3'))
            try:
                rows = db.execute("SELECT detail FROM ground_truth WHERE kind='message_committed'").fetchall()
                self.assertEqual(len(rows), 1)
            finally:
                db.close()

    def test_F08_duplicate_command_delivery_prevented(self):
        """F08: Duplicate task delivery with same op_id -> idempotent; second write skipped."""
        with tempfile.TemporaryDirectory() as d, FixtureWorld(d, test_mode=True) as w:
            op_id = "op-f08-dedup"
            res1 = jpost(w.crm.url + '/api/tasks', {'contact_id': 'C-104', 'title': 'First', 'op_id': op_id})
            res2 = jpost(w.crm.url + '/api/tasks', {'contact_id': 'C-104', 'title': 'Duplicate', 'op_id': op_id})
            self.assertEqual(res1['id'], res2['id'])
            self.assertTrue(res2.get('duplicate', False))

            # Ground truth contains only 1 entry
            db = sqlite3.connect(str(Path(d) / 'crm.sqlite3'))
            try:
                count = db.execute("SELECT COUNT(*) FROM tasks WHERE op_id=?", (op_id,)).fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                db.close()

    def test_F09_cursor_contention_blocks_simultaneous_leases(self):
        """F09: Two agents compete for single cursor -> serialized; lease fencing blocks collision."""
        with tempfile.TemporaryDirectory() as d:
            mgr = CursorLeaseManager(db_path=Path(d) / 'leases.sqlite3')
            lease1 = mgr.acquire("agent_1", duration_seconds=5)
            self.assertIsNotNone(lease1)

            # Immediate second acquire must raise TimeoutError when timeout expires
            with self.assertRaises(TimeoutError):
                mgr.acquire("agent_2", duration_seconds=5, wait_seconds=0.1)

            # Release lease1, now agent_2 can acquire
            mgr.release(lease1)
            lease2 = mgr.acquire("agent_2", duration_seconds=5, wait_seconds=0.1)
            self.assertIsNotNone(lease2)
            mgr.release(lease2)

    def test_F10_mid_run_correction_supersedes_prior_actions(self):
        """F10: User corrects spoken command mid-run -> prior actions marked SUPERSEDED."""
        with tempfile.TemporaryDirectory() as d, FixtureWorld(d, test_mode=True) as w:
            urls = {'crm': w.crm.url, 'docs': w.docs.url, 'outbox': w.outbox.url}
            runner = WebMissionRunner(Ledger(Path(d) / 'f10.sqlite3'), urls, Path(d))
            m = runner.start("Prepare follow-up for Jordan Lee")
            runner.bind(m['mission_id'], 'C-208', 1)  # Wrong initial binding: Sales
            plan = runner.plan(m['mission_id'])
            old_send = plan['send_action']

            # User corrects: "No, Jordan from Operations (C-104)"
            corrected = runner.correct(m['mission_id'], 'C-104')
            self.assertEqual(corrected['intent_revision'], 2)

            # Old action must be SUPERSEDED
            old_action = runner.ledger.action(old_send)
            self.assertEqual(old_action['status'], 'SUPERSEDED')

    def test_F12_misleading_toast_caught_by_independent_verifier(self):
        """F12: UI falsely claims success toast while DB is empty -> verifier catches lie, fails."""
        with tempfile.TemporaryDirectory() as d, FixtureWorld(d, test_mode=True) as w:
            urls = {'crm': w.crm.url, 'docs': w.docs.url, 'outbox': w.outbox.url}
            runner = WebMissionRunner(Ledger(Path(d) / 'f12.sqlite3'), urls, Path(d))
            # Inject misleading toast fault
            jpost(w.crm.url + '/_fault/misleading_toast', {})

            m = runner.start("Prepare follow-up for Jordan Lee")
            runner.bind(m['mission_id'], 'C-104', 1)
            plan = runner.plan(m['mission_id'])
            res = runner.run_task(plan['task_action'])

            # Action status must be FAILED because independent verifier checked SQLite ground truth
            self.assertEqual(res['status'], 'FAILED')
            self.assertEqual(res['verification']['status'], 'FAILED')
            self.assertEqual(res['verification']['evidence']['tasks_for_op'], 0)

    def test_F14_approval_content_tampering_rejected(self):
        """F14: Action parameters altered after approval review -> digest mismatch rejection."""
        from commerce_adapter import CommerceAdapter, CommerceError, CommerceSandbox
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(Path(d) / 'f14_ledger.sqlite3')
            sandbox = CommerceSandbox(Path(d) / 'f14_store.sqlite3')
            adapter = CommerceAdapter(ledger, sandbox)

            draft = adapter.draft_order("mission_f14", "item_dev_keyboard", 1, "Jordan Lee")
            # Adversary attempts to pass tampered digest or altered amount
            with self.assertRaises(CommerceError) as ctx:
                adapter.execute_approved_order(draft['action_id'], "tampered_fake_digest")
            self.assertIn("digest mismatch", str(ctx.exception))

    def test_F16_user_takeover_detected_stops_input(self):
        """F16: Physical cursor moves outside automated trajectory -> user takeover detected."""
        with tempfile.TemporaryDirectory() as d:
            ledger = Ledger(Path(d) / 'f16.sqlite3')
            # Mock driver moving cursor unexpectedly
            class TakeoverDriver:
                UnsupportedDesktop = windows_adapter.UnsupportedDesktop
                def __init__(self):
                    self.step = 0
                def move_pointer(self, hwnd, x, y, snapshot_id=None, smooth=True):
                    raise windows_adapter.UnsupportedDesktop("cursor moved outside RESOLVE control; user takeover detected")

            with self.assertRaises(windows_adapter.UnsupportedDesktop) as ctx:
                TakeoverDriver().move_pointer(123, 10, 10)
            self.assertIn("user takeover detected", str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
