"""Idle and parked tasks never start model monitoring, including old databases."""
import unittest

from orchestrator import Orchestrator
from readiness import BrowserNotReady
import test_unblock_instructions as fixtures


class NoBrowserMonitorTests(unittest.TestCase):
    setUp = fixtures.UnblockInstructionTests.setUp

    def runner(self):
        class Runtime:
            enabled = True
            supported_kinds = ('apply', 'explore')
            calls = 0

            def prepare_browser(self, task, lease):
                self.calls += 1
                raise BrowserNotReady('Fictional denied read.', reason='tool_approval_required')

            def check_browser_blocker(self, *args):
                raise AssertionError('Removed blocker monitor was called')

            def recover_interrupted_readiness(self, *args):
                raise AssertionError('Removed periodic native probe was called')

            def apply(self, *args):
                raise AssertionError('Blocked task entered application work')

        runtime = Runtime()
        runner = Orchestrator(self.store, runtime, schedule_answer_reviews=False)
        self.addCleanup(runner.readiness.close)
        runner.readiness.start()
        runner.readiness.wait(2)
        self.assertTrue(runner.readiness.storage_ready)
        return runner, runtime

    def assert_idle(self, runner, count=50):
        for _ in range(count):
            self.assertEqual('idle', runner.run_once()['worker_status'])

    def test_due_legacy_monitor_rows_are_inert_across_restart(self):
        with self.store.transaction(True) as db:
            # An old production table remains historical data, not work.
            db.execute('CREATE TABLE wf_browser_waits(task_id TEXT, reason TEXT, checks INTEGER, next_check_at TEXT)')
            db.execute('INSERT INTO wf_browser_waits VALUES (?, ?, 99, ?)',
                (self.task_id, 'login_required', '2000-01-01T00:00:00+00:00'))
            before = tuple(db.execute('SELECT * FROM wf_tasks WHERE id=?', (self.task_id,)).fetchone())
        for _ in range(2):
            self.store = fixtures.WorkflowStore(self.path, low_watermark=0)
            runner, runtime = self.runner()
            self.assert_idle(runner)
            self.assertEqual(0, runtime.calls)
            runner.readiness.close()
        with self.store.transaction() as db:
            self.assertEqual(before, tuple(db.execute('SELECT * FROM wf_tasks WHERE id=?', (self.task_id,)).fetchone()))
            self.assertEqual(99, db.execute('SELECT checks FROM wf_browser_waits').fetchone()[0])

    def test_explicit_preparation_retry_checks_once_then_stays_parked(self):
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET error='tool_approval_required' WHERE id=?", (self.task_id,))
            db.execute("UPDATE wf_processes SET block_reason='tool_approval_required' WHERE id=?", (self.process_id,))
        target = self.store.unblock_instructions(self.task_id)['target']
        receipt = self.store.save_unblock_instruction({
            'request_id': 'explicit-user-retry', 'task_id': self.task_id,
            'task_updated_at': target['task_updated_at'], 'process_id': self.process_id,
            'process_revision': target['process_revision'], 'text': 'Please retry preparation once.'})
        self.assertEqual('retry_scheduled', receipt['wake_status'])
        runner, runtime = self.runner()
        self.assertEqual('blocked', runner.run_once()['worker_status'])
        self.assertEqual(1, runtime.calls)
        self.assert_idle(runner)
        self.assertEqual(1, runtime.calls)

    def test_human_blockers_and_exhausted_transient_failures_never_poll_models(self):
        runner, runtime = self.runner()
        for reason in ('login_required', 'challenge_required', 'tool_approval_required',
                       'file_access_required', 'application_action_required', 'browser_session_unavailable'):
            with self.subTest(reason=reason):
                with self.store.transaction(True) as db:
                    db.execute('UPDATE wf_tasks SET error=?,failure_count=max_attempts WHERE id=?', (reason, self.task_id))
                    db.execute('UPDATE wf_processes SET block_reason=? WHERE id=?', (reason, self.process_id))
                self.assert_idle(runner, count=10)
        self.assertEqual(0, runtime.calls)

    def test_saving_login_guidance_does_not_start_monitor_or_wake_task(self):
        target = self.store.unblock_instructions(self.task_id)['target']
        receipt = self.store.save_unblock_instruction({
            'request_id': 'user-note', 'task_id': self.task_id,
            'task_updated_at': target['task_updated_at'], 'process_id': self.process_id,
            'process_revision': target['process_revision'], 'text': 'I completed sign-in.'})
        self.assertEqual(('manual_review', 'saved_only'), (receipt['mode'], receipt['wake_status']))
        runner, runtime = self.runner()
        self.assert_idle(runner)
        self.assertEqual(0, runtime.calls)
