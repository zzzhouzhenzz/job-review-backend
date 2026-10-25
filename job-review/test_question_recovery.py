"""Automatic recovery uses fictional applicant data and no live browser."""
import threading
import time
import unittest
from unittest.mock import patch

from question_recovery import QuestionRecovery
from reconcile_questions import pending_context
import test_reconcile_questions as fixtures
from workflow import WorkflowStore


class QuestionRecoveryTests(unittest.TestCase):
    setUp = fixtures.ReconcileQuestionTests.setUp
    runtime = fixtures.ReconcileQuestionTests.runtime
    user_answer = fixtures.ReconcileQuestionTests.user_answer

    def runner(self, supported=(), during_review=None, *, clock=None):
        runtime = self.runtime(supported, during_review)
        self.addCleanup(runtime.close)
        runner = QuestionRecovery(runtime, poll_interval=0, **({'clock': clock} if clock else {}))
        self.addCleanup(runner.join)
        self.addCleanup(runner.close)
        return runtime, runner

    def rows(self):
        with self.store.transaction() as db:
            return [dict(row) for row in db.execute('SELECT * FROM wf_question_reviews ORDER BY created_at,review_key')]

    def test_manual_profile_change_rechecks_without_recorded_phone_answers(self):
        runtime, runner = self.runner()
        self.assertEqual(0, runner.run_pending(self.store))
        self.assertEqual(1, len(runtime.transport.calls))
        self.assertEqual(0, runner.run_pending(self.store))
        runtime.transport.supported = {'q1', 'q2'}
        self.profile.write_text(self.profile.read_text() + 'New confirmed evidence.\n')
        self.assertEqual(2, runner.run_pending(self.store))
        self.assertEqual(2, len(runtime.transport.calls))
        self.assertEqual('queued', self.store.tasks()[0]['status'])
        self.assertEqual(0, runner.run_pending(self.store))
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_profile_answers').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM wf_process_events WHERE type='answers_resolved_from_evidence'").fetchone()[0])

    def test_unchanged_unresolved_state_is_deduplicated_across_runtime_and_store_restarts(self):
        runtime, runner = self.runner()
        self.assertEqual(0, runner.run_pending(self.store))
        before = self.rows()
        restarted = WorkflowStore(self.db_path)
        newer, second = self.runner(('q1', 'q2'))
        self.assertEqual(0, second.run_pending(restarted))
        self.assertEqual([], newer.transport.calls)
        self.assertEqual(before, self.rows())
        self.assertEqual('waiting_for_user', restarted.tasks()[0]['status'])

    def test_partial_answers_do_not_review_same_unresolved_remainder_again(self):
        runtime, runner = self.runner(('q1',))
        self.assertEqual(1, runner.run_pending(self.store))
        self.assertEqual(0, runner.run_pending(self.store))
        newer, restarted = self.runner(('q2',))
        self.assertEqual(0, restarted.run_pending(self.store))
        self.assertEqual([], newer.transport.calls)
        self.assertEqual(2, len(self.rows()))
        self.assertEqual('waiting_for_user', self.store.tasks()[0]['status'])
        self.profile.write_text(self.profile.read_text() + 'A new profile revision.\n')
        self.assertEqual(1, restarted.run_pending(self.store))
        self.assertEqual('queued', self.store.tasks()[0]['status'])

    def test_resume_and_application_index_changes_are_part_of_evidence_key(self):
        runtime, runner = self.runner()
        for path in (None, self.resume_text, self.history):
            if path:
                path.write_text(path.read_text() + '\n')
            self.assertEqual(0, runner.run_pending(self.store))
            self.assertEqual(0, runner.run_pending(self.store))
        self.assertEqual(3, len(runtime.transport.calls))
        self.assertEqual(3, len({row['evidence_hash'] for row in self.rows()}))

    def test_question_revision_and_field_bindings_trigger_new_review(self):
        runtime, runner = self.runner()
        runner.run_pending(self.store)
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_questions SET revision=revision+1 WHERE id='q1'")
        runner.run_pending(self.store)
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET checkpoint=json_set(checkpoint,'$.question_bindings.q1','full_name')")
        runner.run_pending(self.store)
        self.assertEqual(3, len(runtime.transport.calls))

    def test_existing_user_answer_is_never_replaced(self):
        self.user_answer(value='Corrected user name')
        runtime, runner = self.runner(('q1', 'q2'))
        self.assertEqual(1, runner.run_pending(self.store))
        questions = {q['id']: q for q in self.store.questions()}
        self.assertEqual('Corrected user name', questions['q1']['answer'])
        self.assertEqual('Next month', questions['q2']['answer'])
        self.assertEqual('queued', self.store.tasks()[0]['status'])

    def test_concurrent_phone_answer_discards_stale_review_and_preserves_user_value(self):
        runtime, runner = self.runner(('q1', 'q2'), during_review=self.user_answer)
        self.assertEqual(0, runner.run_pending(self.store))
        self.assertEqual('obsolete', self.rows()[0]['status'])
        self.assertEqual('User supplied name', self.store.questions()[0]['answer'])
        runtime.transport.during_review = None
        self.assertEqual(1, runner.run_pending(self.store))
        self.assertEqual('queued', self.store.tasks()[0]['status'])

    def test_concurrent_evidence_edit_discards_proposal_then_reviews_new_sources(self):
        runtime, runner = self.runner(('q1', 'q2'), during_review=lambda: self.resume_text.write_text('Updated evidence.\n'))
        self.assertEqual(0, runner.run_pending(self.store))
        self.assertTrue(all(q['answer'] is None for q in self.store.questions()))
        self.assertEqual('obsolete', self.rows()[0]['status'])
        runtime.transport.during_review = None
        self.assertEqual(2, runner.run_pending(self.store))

    def test_retry_backoff_survives_restart_and_has_finite_attempts(self):
        stamp = [1000.0]
        runtime, runner = self.runner(clock=lambda: stamp[0])
        def failure():
            raise RuntimeError('Fictional private content must not reach saved errors.')
        runtime.transport.during_review = failure
        runner.run_pending(self.store)
        self.assertEqual('retry_wait', self.rows()[0]['status'])
        self.assertEqual('RuntimeError', self.rows()[0]['last_error'])
        newer, second = self.runner(during_review=failure, clock=lambda: stamp[0])
        second.run_pending(self.store)
        self.assertEqual([], newer.transport.calls)
        stamp[0] += 30
        second.run_pending(self.store)
        second.run_pending(self.store)
        self.assertEqual(1, len(newer.transport.calls))
        stamp[0] += 120
        second.run_pending(self.store)
        self.assertEqual('exhausted', self.rows()[0]['status'])
        stamp[0] += 3600
        second.run_pending(self.store)
        self.assertEqual(2, len(newer.transport.calls))
        newer.transport.during_review = None
        self.profile.write_text(self.profile.read_text() + 'A corrected source.\n')
        second.run_pending(self.store)
        self.assertEqual(3, len(newer.transport.calls))

    def test_failed_review_retry_can_complete_and_resume_once(self):
        stamp = [1000.0]
        def failure():
            raise RuntimeError('One transient failure')
        runtime, runner = self.runner(('q1', 'q2'), failure, clock=lambda: stamp[0])
        self.assertEqual(0, runner.run_pending(self.store))
        runtime.transport.during_review = None
        stamp[0] += 30
        self.assertEqual(2, runner.run_pending(self.store))
        self.assertEqual(0, runner.run_pending(self.store))
        self.assertEqual('completed', self.rows()[0]['status'])
        self.assertEqual(2, self.rows()[0]['attempts'])
        self.assertEqual('queued', self.store.tasks()[0]['status'])

    def test_background_agent_does_not_hold_scheduler_or_database(self):
        entered, release = threading.Event(), threading.Event()
        def block():
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test did not release reviewer')
        runtime, runner = self.runner((), block)
        self.addCleanup(release.set)
        runtime.question_recovery = runner
        start = time.monotonic()
        self.assertEqual(0, runtime.reuse_pending_answers(self.store))
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertTrue(entered.wait(2))
        self.assertEqual(0, runtime.reuse_pending_answers(self.store))
        self.user_answer(value='Concurrent phone value')
        release.set()
        runner.join(3)
        self.assertFalse(runner.thread.is_alive())
        self.assertEqual('Concurrent phone value', self.store.questions()[0]['answer'])
        self.assertEqual(1, len(runtime.transport.calls))

    def test_user_answer_wakes_idle_review_before_next_poll(self):
        from orchestrator import Orchestrator
        runtime, runner = self.runner(('q2',), clock=lambda: 1000.0)
        runtime.question_recovery = runner
        runner.next_poll = 1600.0
        orchestrator = Orchestrator(self.store, runtime)
        self.addCleanup(orchestrator.readiness.close)
        runner.schedule(self.store)
        self.assertIsNone(runner.thread)

        receipt = self.user_answer(value='New confirmed name')
        runner.join(3)

        self.assertTrue(receipt['accepted'])
        self.assertEqual(1, len(runtime.transport.calls))
        questions = {question['id']: question for question in self.store.questions()}
        self.assertEqual('New confirmed name', questions['q1']['answer'])
        self.assertEqual('Next month', questions['q2']['answer'])
        self.assertEqual('queued', self.store.tasks()[0]['status'])
        self.assertIn('New confirmed name', self.profile.read_text())

    def test_answer_during_review_coalesces_wakes_and_rechecks_without_another_tick(self):
        from orchestrator import Orchestrator
        entered, release = threading.Event(), threading.Event()
        first_review = [True]

        def block_first_review():
            if first_review[0]:
                first_review[0] = False
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('Test did not release reviewer')

        runtime, runner = self.runner(('q1', 'q2'), block_first_review,
                                      clock=lambda: 1000.0)
        self.addCleanup(release.set)
        runtime.question_recovery = runner
        runner.poll_interval = 600
        orchestrator = Orchestrator(self.store, runtime)
        self.addCleanup(orchestrator.readiness.close)
        with patch.object(runner, 'run_pending', wraps=runner.run_pending) as scan:
            runtime.reuse_pending_answers(self.store)
            self.assertTrue(entered.wait(2))
            receipt = self.user_answer(value='Concurrent confirmed name')
            for _ in range(10):
                runtime.answers_changed(self.store)
            self.assertEqual(1, len(runtime.transport.calls))
            release.set()
            runner.join(3)
            self.assertFalse(runner.thread.is_alive())
            self.assertEqual(2, scan.call_count)

        self.assertTrue(receipt['accepted'])
        self.assertEqual(2, len(runtime.transport.calls))
        questions = {question['id']: question for question in self.store.questions()}
        self.assertEqual('Concurrent confirmed name', questions['q1']['answer'])
        self.assertEqual('Next month', questions['q2']['answer'])
        self.assertEqual('queued', self.store.tasks()[0]['status'])
        self.assertEqual({'obsolete', 'completed'}, {row['status'] for row in self.rows()})
        with self.store.transaction() as db:
            self.assertEqual(['q2'], [row[0] for row in db.execute(
                'SELECT question_id FROM wf_answer_evidence')])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_submitted_answer_resolves_shared_questions_across_applications_once(self):
        from orchestrator import Orchestrator
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_questions SET text='Full legal name' WHERE id='q1'")
        shared_questions = []
        for key in ('a:2', 'b:b1'):
            with self.store.transaction(True) as db:
                db.execute("UPDATE jobs SET decision='approve',revision=revision+1 WHERE key=?", (key,))
                pid = self.store._create_application(
                    db, self.store._row(db, 'jobs', 'key', key), 'apply-' + key)
            task = self.store.lease_task('fixture', ['apply'])
            self.store.start_application(pid, task['lease_token'])
            qid = 'shared-name-' + key
            shared_questions.append(qid)
            self.store.checkpoint_task(task['id'], task['lease_token'], {
                'question_bindings': {qid: 'name-field-' + key}})
            self.store.ask_questions(pid, task['lease_token'], 'form-' + key, [
                {'id': qid, 'text': 'Full legal name', 'type': 'text', 'required': True}])

        runtime, runner = self.runner(clock=lambda: 1000.0)
        runtime.question_recovery = runner
        runner.next_poll = 1600.0
        orchestrator = Orchestrator(self.store, runtime)
        self.addCleanup(orchestrator.readiness.close)
        process = next(p for p in self.store.processes() if p['id'] == self.pid)
        questions = {q['id']: q for q in self.store.questions()}
        payload = {'request_id': 'shared-user-answer', 'process_id': self.pid,
            'process_revision': process['revision'], 'form_version': process['form_version'],
            'answers': [{'question_id': qid, 'question_revision': questions[qid]['revision'],
                         'value': value} for qid, value in (
                             ('q1', 'Confirmed Shared Name'), ('q2', 'Next month'))]}

        receipt = self.store.answer_questions(payload)
        runner.join(3)
        self.assertFalse(runner.thread.is_alive())
        self.assertTrue(receipt['accepted'])
        questions = {q['id']: q for q in self.store.questions()}
        for qid in shared_questions:
            self.assertEqual('Confirmed Shared Name', questions[qid]['answer'])
        self.assertEqual(['queued'] * 3, [task['status'] for task in self.store.tasks()])
        self.assertEqual([], runtime.transport.calls)
        reviewed = self.rows()
        self.assertEqual(2, len(reviewed))
        self.assertTrue(all(row['answered_count'] == 1 for row in reviewed))

        # An HTTP retry with the same request ID returns the original receipt.
        # It cannot add another review, answer event, or submission.
        self.assertEqual(receipt, self.store.answer_questions(payload))
        runner.join(3)
        self.assertEqual(reviewed, self.rows())
        self.assertEqual([], runtime.transport.calls)
        with self.store.transaction() as db:
            self.assertEqual(2, db.execute('SELECT COUNT(*) FROM wf_answer_evidence').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_answer_signal_does_not_bypass_unchanged_review_backoff(self):
        def failure():
            raise RuntimeError('Fictional temporary reviewer failure')

        runtime, runner = self.runner(during_review=failure, clock=lambda: 1000.0)
        runner.run_pending(self.store)
        before = self.rows()
        self.assertEqual('retry_wait', before[0]['status'])
        runner.next_poll = 1600.0

        runner.answers_changed(self.store)
        runner.join(3)

        self.assertEqual(1, len(runtime.transport.calls))
        self.assertEqual(before, self.rows())

    def test_answer_hook_failure_keeps_durable_receipt_and_logs_only_safe_type(self):
        private_message = 'Fictional applicant detail must not appear in logs'

        def failure():
            raise RuntimeError(private_message)

        self.store.application_profile = self.profile
        self.store.on_answers_changed = failure
        with self.assertLogs(level='WARNING') as captured:
            receipt = self.user_answer(value='Durable confirmed name')

        self.assertTrue(receipt['accepted'])
        self.assertEqual('Durable confirmed name', next(
            question for question in self.store.questions() if question['id'] == 'q1')['answer'])
        self.assertIn('Durable confirmed name', self.profile.read_text())
        output = '\n'.join(captured.output)
        self.assertIn('RuntimeError', output)
        self.assertNotIn(private_message, output)
        self.assertNotIn('Traceback', output)

    def test_runtime_answer_signal_respects_disabled_application_flow(self):
        runtime, runner = self.runner()
        runtime.question_recovery = runner
        with patch.object(runner, 'answers_changed') as changed:
            runtime.supported_kinds = ('rank',)
            runtime.answers_changed(self.store)
            changed.assert_not_called()
            runtime.supported_kinds = ('apply',)
            runtime.answers_changed(self.store)
            changed.assert_called_once_with(self.store)

    def test_durable_ownership_avoids_concurrent_reviews_and_recovers_expired_claim(self):
        stamp = [1000.0]
        runtime, first = self.runner(clock=lambda: stamp[0])
        context = pending_context(self.store, self.pid)
        evidence = runtime.evidence_snapshot(application=True)
        first._claim(self.store, context, evidence)
        newer, second = self.runner(clock=lambda: stamp[0])
        self.assertEqual(0, second.run_pending(self.store))
        self.assertEqual([], newer.transport.calls)
        stamp[0] += 61
        self.assertEqual(0, second.run_pending(self.store))
        self.assertEqual(1, len(newer.transport.calls))
        self.assertEqual('completed', self.rows()[0]['status'])
        self.assertEqual(2, self.rows()[0]['attempts'])

    def test_sleep_expiring_active_review_lease_retries_unchanged_state(self):
        stamp = [1000.0]
        def suspend():
            stamp[0] += 61
        runtime, runner = self.runner(('q1', 'q2'), suspend, clock=lambda: stamp[0])
        self.assertEqual(0, runner.run_pending(self.store))
        self.assertEqual('retry_wait', self.rows()[0]['status'])
        self.assertTrue(all(q['answer'] is None for q in self.store.questions()))
        self.assertEqual(0, runner.run_pending(self.store))
        runtime.transport.during_review = None
        stamp[0] += 30
        self.assertEqual(2, runner.run_pending(self.store))
        self.assertEqual('completed', self.rows()[0]['status'])
        self.assertEqual(2, self.rows()[0]['attempts'])

    def test_closed_recovery_does_not_start_new_reviews(self):
        runtime, runner = self.runner(('q1', 'q2'))
        runner.close()
        runner.schedule(self.store)
        runner.answers_changed(self.store)
        self.assertIsNone(runner.thread)
        self.assertEqual(0, runner.run_pending(self.store))
        self.assertEqual([], runtime.transport.calls)

    def test_empty_scan_does_not_touch_profile_or_start_agent(self):
        runtime, runner = self.runner()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET block_reason='login_required'")
        with patch.object(self.store, 'sync_profile_answers', side_effect=AssertionError('No profile lock')):
            self.assertEqual(0, runner.run_pending(self.store))
        self.assertEqual([], runtime.transport.calls)
        self.assertEqual([], self.rows())

    def test_clean_projection_scan_does_not_compete_for_profile_lock(self):
        runtime, runner = self.runner()
        with patch.object(self.store, 'sync_profile_answers', side_effect=AssertionError('No redundant lock')):
            self.assertEqual(0, runner.run_pending(self.store))
        self.assertEqual(1, len(runtime.transport.calls))

    def test_shutdown_join_reports_live_reviewer_until_it_has_stopped(self):
        from worker_runner import WorkerShutdownTimeout
        entered, release = threading.Event(), threading.Event()
        def block():
            entered.set()
            if not release.wait(5):
                raise RuntimeError('Test did not release reviewer')
        runtime, runner = self.runner((), block)
        self.addCleanup(release.set)
        runner.schedule(self.store)
        self.assertTrue(entered.wait(2))
        self.assertFalse(runner.thread.daemon)
        runner.close()
        with self.assertRaises(WorkerShutdownTimeout):
            runner.join(timeout=0.01)
        with self.assertRaises(WorkerShutdownTimeout):
            runner.join(timeout=0.01)
        release.set()
        runner.join(timeout=2)
        self.assertFalse(runner.thread.is_alive())

    def test_quarantined_resource_is_not_reviewed_or_resumed(self):
        runtime, runner = self.runner(('q1', 'q2'))
        with self.store.transaction(True) as db:
            db.execute("INSERT INTO wf_resource_locks VALUES ('browser',?,'quarantine-token',1)", (self.task_id,))
        self.assertEqual(0, runner.run_pending(self.store))
        self.assertEqual([], runtime.transport.calls)
        self.assertEqual([], self.rows())
        self.assertEqual('waiting_for_user', self.store.tasks()[0]['status'])

    def test_orchestrator_can_disable_agent_review_while_retaining_local_tick(self):
        from orchestrator import Orchestrator
        runtime, runner = self.runner()
        store = WorkflowStore(self.db_path, low_watermark=0)
        with patch.object(runtime, 'reuse_pending_answers') as schedule:
            disabled = Orchestrator(store, runtime, schedule_answer_reviews=False)
            self.addCleanup(disabled.readiness.close)
            self.assertFalse(callable(getattr(store, 'on_answers_changed', None)))
            disabled.tick()
            schedule.assert_not_called()
            enabled = Orchestrator(store, runtime)
            self.addCleanup(enabled.readiness.close)
            self.assertTrue(callable(getattr(store, 'on_answers_changed', None)))
            enabled.tick()
            schedule.assert_called_once_with(store)


if __name__ == '__main__':
    unittest.main()
