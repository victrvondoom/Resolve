"""Voice and typed policy rules: parsing, confirmation for high-impact rules, unknown instructions never applied,
conflicts with active rules, and corrections that are one-time or persistent, always reversible and never silent."""
import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from at_rest import SealedStore
from inbox_store import InboxError, InboxStore
import voice_policy as vp

EXAMPLE = ("Prioritize emails from my active project collaborators. Put actual deadlines above ordinary questions. "
           "Do not send anything without my approval. Track every commitment I make.")


class ParsingRules(unittest.TestCase):
    def test_the_spec_example_becomes_four_structured_rules(self):
        rules = vp.parse_instruction(EXAMPLE)
        self.assertEqual([r['type'] for r in rules], ['PRIORITY_RAISE', 'REVIEW_FIRST', 'APPROVAL_REQUIRED_TO_SEND', 'CAPTURE_COMMITMENTS'])
        self.assertEqual(rules[0]['scope'], 'SENDER_GROUP:collaborators')
        self.assertEqual(rules[0]['priority'], 'HIGH')

    def test_each_rule_keeps_its_original_instruction(self):
        for r in vp.parse_instruction(EXAMPLE):
            self.assertIn(r['instruction'], EXAMPLE)

    def test_unrecognised_instruction_is_unknown_and_not_applied(self):
        rules = vp.parse_instruction('Blorp the widgets quietly.')
        self.assertEqual(rules[0]['type'], 'UNKNOWN')
        self.assertTrue(vp.requires_confirmation(rules))

    def test_tone_and_signoff_are_parsed(self):
        t = vp.parse_instruction('Use a friendly but professional tone.')[0]
        self.assertEqual((t['type'], t['value']), ('TONE', 'friendly but professional'))

    def test_high_impact_rules_need_confirmation(self):
        self.assertTrue(vp.requires_confirmation(vp.parse_instruction('Do not send anything without my approval.')))
        self.assertTrue(vp.requires_confirmation(vp.parse_instruction('Track every commitment I make.')))

    def test_low_impact_rule_does_not_need_confirmation(self):
        self.assertFalse(vp.requires_confirmation(vp.parse_instruction('Use a friendly but professional tone.')))


class Conflicts(unittest.TestCase):
    def test_a_rule_that_would_send_without_approval_conflicts_with_the_approval_rule(self):
        active = vp.parse_instruction('Do not send anything without my approval.')
        new = vp.parse_instruction('Send the weekly update without asking me.')
        self.assertTrue(vp.conflicts(new, active))

    def test_different_tones_for_the_same_scope_are_reported(self):
        active = vp.parse_instruction('Use a formal tone.')
        new = vp.parse_instruction('Use a casual tone.')
        self.assertTrue(vp.conflicts(new, active))


class CorrectionsAndVersions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        key = base64.urlsafe_b64encode(bytes(range(32))).decode()
        self.store = InboxStore(Path(self.tmp.name) / 'i.sqlite3', SealedStore(Path(self.tmp.name) / 'k', env={'RESOLVE_MASTER_KEY': key}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_one_time_correction_is_stored_with_before_and_after(self):
        c = self.store.add_correction('u', 'w', 'thr1', {'category': 'URGENT'}, {'category': 'AUTOMATED_LOW_PRIORITY'},
                                      'It is an automated notification.', 'ONE_TIME', False)
        row = self.store.list_corrections('u', 'w')[0]
        self.assertEqual(row['original'], {'category': 'URGENT'})
        self.assertEqual(row['corrected'], {'category': 'AUTOMATED_LOW_PRIORITY'})
        self.assertEqual(row['scope'], 'ONE_TIME')

    def test_a_workspace_wide_preference_is_never_stored_without_confirmation(self):
        with self.assertRaises(InboxError):
            self.store.add_correction('u', 'w', None, {}, {'always': 'top'}, 'Always show this collaborator first.', 'PERSISTENT_WORKSPACE', False)
        self.assertEqual(self.store.list_corrections('u', 'w'), [])

    def test_a_correction_can_be_reversed_and_the_reversal_is_audited(self):
        c = self.store.add_correction('u', 'w', None, {'a': 1}, {'a': 2}, 'x', 'PERSISTENT_WORKSPACE', True)
        self.store.reverse_correction('u', 'w', c['id'])
        self.assertFalse(self.store.list_corrections('u', 'w')[0]['active'])
        self.assertTrue(any(e['action'] == 'correction.reversed' for e in self.store.audit_log('u', 'w')))
        with self.assertRaises(InboxError):
            self.store.reverse_correction('u', 'w', c['id'])

    def test_correction_explanations_are_encrypted_at_rest(self):
        self.store.add_correction('u', 'w', None, {}, {}, 'the private reason sentence', 'ONE_TIME', False)
        raw = (Path(self.tmp.name) / 'i.sqlite3').read_bytes()
        self.assertNotIn(b'private reason sentence', raw)


if __name__ == '__main__':
    unittest.main()
