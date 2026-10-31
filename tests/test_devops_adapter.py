"""Tests for DevOpsAdapter (Development Operations workspace)."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from devops_adapter import DevOpsAdapter, DevOpsError
from resolve_core import Ledger


class DevOpsAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name) / 'devops.sqlite3')
        self.adapter = DevOpsAdapter(repo_root=ROOT, ledger=self.ledger)

    def tearDown(self):
        self.tmp.cleanup()

    def test_repo_inspection_returns_head_and_branch(self):
        res = self.adapter.inspect_repo()
        self.assertEqual(res['status'], 'OBSERVED')
        self.assertTrue(len(res['head_sha']) >= 40)
        self.assertTrue(bool(res['branch']))
        self.assertIsInstance(res['modified_files'], int)

    def test_scoped_test_execution_confirms_clean_pass(self):
        res = self.adapter.run_tests('tests/test_web_bench.py')
        self.assertEqual(res['status'], 'CONFIRMED')
        self.assertEqual(res['exit_code'], 0)
        self.assertGreaterEqual(res['tests_run'], 2)
        self.assertTrue(res['verified'])

        # Verify ledger state transition
        action = self.ledger.action(res['action_id'])
        self.assertEqual(action['status'], 'CONFIRMED')
        self.assertEqual(action['operation'], 'devops_run_tests')

    def test_workspace_escape_refused(self):
        with self.assertRaises(DevOpsError):
            self.adapter.run_tests('../outside_repo.py')

        with self.assertRaises(DevOpsError):
            self.adapter.run_tests('src/devops_adapter.py')  # not in tests/

    def test_failing_target_transitions_to_failed(self):
        with self.assertRaises(DevOpsError):
            self.adapter.run_tests('tests/test_nonexistent_xyz.py')


if __name__ == '__main__':
    unittest.main()
