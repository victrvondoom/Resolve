import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'benchmarks'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'apps'))
from web_bench import judge, wilson

CLEAN = dict(messages=1, tasks=1, wrong_recipient=0, wrong_version=0, duplicate=0, task_wrong_contact=0)


class OracleJudgeTests(unittest.TestCase):
    def test_judge_labels(self):
        self.assertEqual(judge(CLEAN, 'success', 'lost_ack'), 'true_success')
        self.assertEqual(judge({**CLEAN, 'wrong_recipient': 1}, 'success', 'ambiguous_name'), 'wrong_target_write')
        self.assertEqual(judge({**CLEAN, 'messages': 2, 'duplicate': 1}, 'success', 'lost_ack'), 'duplicate_effect')
        self.assertEqual(judge({**CLEAN, 'tasks': 0, 'messages': 0}, 'success', 'misleading_toast'), 'false_success')
        self.assertEqual(judge({**CLEAN, 'tasks': 0, 'messages': 0}, 'failed', 'misleading_toast'), 'honest_failure_or_hold')
        self.assertEqual(judge({**CLEAN, 'messages': 0}, 'hold', 'report_edited_after_approval'), 'safe_hold')

    def test_wilson_bounds(self):
        lo, hi = wilson(0, 24)
        self.assertEqual(lo, 0)
        self.assertLess(hi, 0.15)
        self.assertEqual(wilson(0, 0), (0.0, 1.0))


class ATestAcceptanceTests(unittest.TestCase):
    def test_acceptance_test_generation_and_execution(self):
        import tempfile
        from web_fixtures import FixtureWorld
        from web_bench import run_c, oracle, judge
        with tempfile.TemporaryDirectory() as d, FixtureWorld(d, test_mode=True) as w:
            claimed, approved_sha = run_c('lost_ack', w, Path(d), Path(d))
            o = oracle(Path(d), approved_sha)
            outcome = judge(o, claimed, 'lost_ack')
            self.assertEqual(outcome, 'true_success')
            self.assertEqual(o['wrong_recipient'], 0)
            self.assertEqual(o['duplicate'], 0)


if __name__ == '__main__':
    unittest.main()
