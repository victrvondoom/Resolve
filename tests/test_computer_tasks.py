"""Generalized computer use against three controlled apps with persisted records, driven by REAL Chromium through the
typed tool registry. Success is judged by reading each app's own database, not the page."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from cloud_browser import CloudBrowserError, CloudBrowserSession
from computer_tasks import BROWSER_CAPS, ComputerTaskRunner, browser_services
from controlled_apps import serve_controlled
from net_policy import NetPolicy, origin_of
from tool_registry import ToolContext, default_registry


class ComputerTaskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.server, cls.base, cls.apps = serve_controlled(str(Path(cls.tmp) / 'apps.db'))
        policy = NetPolicy(allowed_origins=frozenset({origin_of(cls.base)}))
        cls.session = CloudBrowserSession('sbx_t', 'user_a', Path(cls.tmp) / 'profile', policy=policy)
        cls.bases = {'pm': cls.base, 'research': cls.base, 'crm': cls.base}
        cls.reg = default_registry()

    @classmethod
    def tearDownClass(cls):
        cls.session.close()
        cls.server.shutdown()

    def runner(self, caps=BROWSER_CAPS, cancel=lambda: False, user='user_a'):
        ctx = ToolContext(user_id=user, workspace_id='ws_1', mission_id='m1', capabilities=frozenset(caps), services=browser_services(self.session), record=lambda r: 'ev')
        return ComputerTaskRunner(self.reg, ctx, self.bases, self.apps.verify, cancel)

    # ---- the five commands
    def test_overdue_tasks_matches_the_database(self):
        r = self.runner().run('Open Project Atlas and show my overdue tasks.')
        self.assertTrue(r['ok'], r)
        self.assertEqual(r['status'], 'CONFIRMED')
        self.assertEqual(sorted(r['overdue']), ['Fix invoice rounding', 'Migrate legacy accounts', 'Ship onboarding flow'])
        self.assertNotIn('Security review', r['overdue'])        # done, though past due

    def test_other_project_has_different_layout_and_result(self):
        r = self.runner().run('Open Project Borealis and show my overdue tasks.')
        self.assertEqual(sorted(r['overdue']), ['Budget sign-off', 'Vendor contract draft'])
        self.assertEqual(r['status'], 'CONFIRMED')

    def test_latest_report_picks_the_newest_version_and_summarizes_it(self):
        r = self.runner().run('Find the latest project report and summarize it.')
        self.assertTrue(r['ok'], r)
        self.assertEqual((r['document']['title'], r['document']['version']), ('Market Report Q3', 3))
        self.assertIn('4.6%', r['summary'])
        self.assertNotIn('Superseded', r['summary'])
        self.assertIn('extractive', r['summary_kind'])

    def test_update_task_needs_exact_approval_then_writes_and_verifies(self):
        cmd = "Update task 'Write launch notes' to done in Project Atlas"
        before = self.apps.task(3)
        prop = self.runner().run(cmd)
        self.assertEqual(prop['status'], 'AWAITING_APPROVAL')
        self.assertEqual(self.apps.task(3)['status'], before['status'], 'a proposal must not change anything')
        done = self.runner().run(cmd, approved_digest=prop['proposal']['digest'])
        self.assertEqual(done['status'], 'CONFIRMED', done)
        after = self.apps.task(3)
        self.assertEqual((after['status'], after['version']), ('done', before['version'] + 1))
        self.apps.force_task_version_bump(3)   # leave the DB consistent for other tests (status is unchanged semantically)

    def test_update_task_is_refused_when_the_target_changed_after_approval(self):
        cmd = "Update task 'Budget sign-off' to done in Project Borealis"
        prop = self.runner().run(cmd)
        self.assertEqual(prop['status'], 'AWAITING_APPROVAL')
        self.apps.force_task_version_bump(8)       # somebody else edited it after the user saw it
        res = self.runner().run(cmd, approved_digest=prop['proposal']['digest'])
        self.assertFalse(res['ok'])
        self.assertEqual(res['error']['code'], 'APPROVAL_MISMATCH')
        self.assertEqual(self.apps.task(8)['status'], 'todo', 'nothing may be written on a stale approval')

    def test_a_wrong_or_forged_digest_writes_nothing(self):
        res = self.runner().run("Update task 'Kickoff deck' to done in Project Borealis", approved_digest='0' * 64)
        self.assertEqual(res['error']['code'], 'APPROVAL_MISMATCH')
        self.assertEqual(self.apps.task(7)['status'], 'in_progress')

    def test_a_write_the_verifier_cannot_see_is_never_reported_as_confirmed(self):
        cmd = "Update task 'Vendor contract draft' to in progress in Project Borealis"
        r = self.runner()
        prop = r.run(cmd)
        real = self.apps.verify
        r2 = self.runner()
        r2.verifier = lambda kind, a: ({**real(kind, a), 'status': 'in_progress', 'version': 1} if kind == 'task' else real(kind, a))   # the database disagrees with the page
        res = r2.run(cmd, approved_digest=prop['proposal']['digest'])
        self.assertEqual(res['status'], 'UNCERTAIN')
        self.assertFalse(res['ok'])
        self.assertEqual(self.apps.task(6)['status'], 'in_progress')   # the page write really happened; only the verifier was made to disagree

    def test_unknown_task_title_is_not_guessed(self):
        res = self.runner().run("Update task 'Nonexistent thing' to done in Project Atlas")
        self.assertEqual(res['error']['code'], 'NOT_FOUND')

    def test_save_report_binds_document_version_and_is_verified(self):
        cmd = 'Open the research portal, find the correct document and save a report to my workspace.'
        prop = self.runner().run(cmd)
        self.assertEqual(prop['status'], 'AWAITING_APPROVAL')
        self.assertEqual(prop['document']['version'], 3)
        self.assertEqual(self.apps.reports(), [])
        done = self.runner().run(cmd, approved_digest=prop['proposal']['digest'])
        self.assertEqual(done['status'], 'CONFIRMED', done)
        rows = self.apps.reports()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['source_doc_id'], rows[0]['source_version']), (3, 3))
        self.assertIn('4.6%', rows[0]['body'])

    def test_find_person_resolves_by_department_not_by_name(self):
        r = self.runner().run('Find Alex from Finance, not Alex from Sales.')
        self.assertEqual(r['status'], 'CONFIRMED', r)
        self.assertEqual(r['person']['email'], 'alex.rivera@example.test')
        self.assertNotEqual(r['person']['name'], 'Alexa Morgan')   # same department, different person

    def test_ambiguous_person_is_refused_not_guessed(self):
        res = self.runner().run('Find Alex from Sales.')       # exactly one Alex in Sales: fine
        self.assertEqual(res['person']['name'], 'Alex Chen')
        res = self.runner().run('Find Jordan from Finance')    # Jordan is in Sales
        self.assertFalse(res['ok'])
        self.assertEqual(res['error']['code'], 'NOT_FOUND')

    # ---- safety properties of the tool layer
    def test_missing_capability_blocks_the_browser(self):
        res = self.runner(caps={'evidence:write'}).run('Open Project Atlas and show my overdue tasks.')
        self.assertEqual(res['error']['code'], 'PERMISSION_DENIED')

    def test_cancellation_stops_before_any_page_action(self):
        res = self.runner(cancel=lambda: True).run('Open Project Atlas and show my overdue tasks.')
        self.assertEqual(res['error']['code'], 'CANCELLED')
        self.assertEqual(res['steps'], [])

    def test_unsupported_task_is_refused_honestly(self):
        with self.assertRaises(Exception) as cm:
            self.runner().run('Book me a flight to Paris')
        self.assertEqual(getattr(cm.exception, 'code', ''), 'UNSUPPORTED_TASK')

    def test_origin_policy_blocks_navigation_outside_the_allowed_app(self):
        r = self.runner().reg.call(self.runner().ctx, 'browser.navigate', {'url': 'http://169.254.169.254/latest/meta-data/'})
        self.assertFalse(r.ok)

    def test_page_text_cannot_grant_capabilities(self):
        # a page can say anything; capabilities come only from the ToolContext
        ctx = ToolContext(user_id='u', workspace_id='w', capabilities=frozenset({'browser:navigate'}), services=browser_services(self.session))
        r = self.reg.call(ctx, 'browser.click', {'selector': 'body'})
        self.assertFalse(r.ok)
        self.assertEqual(r.error['code'], 'PERMISSION_DENIED')

    def test_new_tools_validate_input(self):
        ctx = self.runner().ctx
        self.assertFalse(self.reg.call(ctx, 'browser.wait', {'timeout_ms': 999999}).ok)
        self.assertFalse(self.reg.call(ctx, 'browser.find', {'role': 'BAD ROLE!'}).ok)
        self.assertFalse(self.reg.call(ctx, 'browser.select', {'selector': 'select'}).ok)

    def test_wait_fails_loudly_instead_of_passing(self):
        self.session.navigate(self.base + '/crm/')
        with self.assertRaises(CloudBrowserError):
            self.session.wait_for(selector='#does-not-exist', timeout_ms=400)


if __name__ == '__main__':
    unittest.main()
