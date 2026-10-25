"""Saved-answer recovery retains application and submission gates."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from codex_runtime import CodexRuntime
from orchestrator import Orchestrator
from store import ReviewError, encode, now
from test_store import catalog
from workflow import WorkflowStore


class FairRuntime:
    enabled = True
    supported_kinds = ('apply', 'explore')

    def __init__(self, observation=None):
        self.observation = observation
        self.operations = []

    def prepare_browser(self, task, lease):
        return {'chrome': {'attached': True, 'session_id': 'fictional'},
                'linkedin': {'authenticated': True, 'session_id': 'fictional',
                    'tab_id': 'fictional-linkedin', 'url': 'https://www.linkedin.com/jobs/'}}


    def explore(self, context, lease):
        self.operations.append(('work', lease.task_id))
        return {'companies': [], 'source': 'Fictional discovery without openings.'}

    def apply(self, store, task):
        self.operations.append(('apply', task['id']))
        pid, token = task['payload']['process_id'], task['lease_token']
        store.before_submit(pid, token, 'fixture-form')
        store.confirm_submission(pid, token, {'url': 'https://jobs.example/confirmation',
            'evidence': 'Fictional application received.', 'observed_at': now(), 'reference': 'fixture-once'})


class BlockerResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'review.sqlite3'
        self.store = WorkflowStore(self.path, low_watermark=0)
        self.store.grant_project_consent({'request_id': 'fixture-consent', 'policy_version': 1, 'accepted': True})
        self.store.import_catalog(catalog(), 'Fictional blocker resume')
        card = self.store.feed()['cards'][0]
        receipt = self.store.receive_decision({'request_id': 'fictional-apply', 'action': 'apply',
            'card_id': card['id'], 'card_revision': card['revision'], 'selected': [card['jobs'][0]['key']]})
        self.task_id, self.process_id = receipt['task_ids'][0], receipt['process_ids'][0]
        task = self.store.lease_task('initial-worker', kinds=['apply'])
        self.store.start_application(self.process_id, task['lease_token'])
        self.checkpoint = {'question_bindings': {'phone': 'phone_type', 'start': 'start_date'},
                           'resume_upload_evidence': {'filename': 'fixture-resume.pdf', 'verified': True}}
        self.store.checkpoint_task(self.task_id, task['lease_token'], self.checkpoint)
        self.store.ask_questions(self.process_id, task['lease_token'], 'fixture-form', [
            {'id': 'phone', 'text': 'Phone device type?', 'type': 'text', 'choices': [],
             'required': True, 'scope': 'application', 'reason': 'Fictional field'},
            {'id': 'start', 'text': 'Start date?', 'type': 'text', 'choices': [],
             'required': True, 'scope': 'application', 'reason': 'Fictional field'},
            {'id': 'optional', 'text': 'Optional note?', 'type': 'text', 'choices': [],
             'required': False, 'scope': 'application', 'reason': 'Fictional optional field'}])

    def task(self):
        return next(t for t in self.store.tasks() if t['id'] == self.task_id)

    def process(self):
        return next(p for p in self.store.processes() if p['id'] == self.process_id)

    def setting(self, key):
        with self.store.transaction() as db:
            row = db.execute('SELECT value FROM wf_settings WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def tables(self, names):
        with self.store.transaction() as db:
            return {name: [tuple(row) for row in db.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
                    for name in names}

    def answers(self, question_ids=('phone', 'start')):
        process = self.process()
        questions = {q['id']: q for q in self.store.questions()}
        values = {'phone': 'Mobile', 'start': 'Next month'}
        self.store.answer_questions({'request_id': 'answer-' + '-'.join(question_ids),
            'process_id': self.process_id, 'process_revision': process['revision'],
            'form_version': process['form_version'], 'answers': [
                {'question_id': qid, 'question_revision': questions[qid]['revision'], 'value': values[qid]}
                for qid in question_ids]})

    def stale_answered_wait(self):
        self.answers()
        # Reproduce the prior stale projection after answers were already committed.
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET status='waiting_for_user',error='questions_pending' WHERE id=?", (self.task_id,))
            db.execute("UPDATE wf_processes SET block_reason='questions_pending' WHERE id=?", (self.process_id,))
            db.execute("UPDATE queue SET status='needs_input' WHERE id=?", (self.process_id,))

    def parked_browser(self):
        self.answers()
        task = self.store.lease_task('blocked-browser-worker', kinds=['apply'])
        CodexRuntime._park(self.store, task, 'tool_approval_required', 'Fictional upload was blocked.',
                           operation='resume_upload')
        self.due()

    def exhausted_browser(self, reason='application_page_transient'):
        self.answers()
        task = self.store.lease_task('terminal-typed-failure', kinds=['apply'])
        with self.store.transaction(True) as db:
            db.execute('UPDATE wf_tasks SET failure_count=max_attempts-1 WHERE id=?', (self.task_id,))
            self.store._process_event(db, self.process_id, 'blocked', {'reason': reason,
                'detail': 'Fictional typed stopped failure.'})
        return self.store.schedule_browser_recovery(task['id'], task['lease_token'], reason)

    def page_observation(self, **changes):
        return {**self.observation(), 'reason': 'application_page_transient',
                'resolution': 'draft_accessible', 'evidence': 'Exact fictional application draft is accessible.',
                **changes}




    def queued_work(self, count=1, kind='explore'):
        result = []
        with self.store.transaction(True) as db:
            for index in range(count):
                tid = self.store._enqueue_task(db, kind, {'after_explore': True},
                    f'fictional-{kind}-{index}', resource='browser' if kind == 'explore' else None)
                # Older normal work proves that resume uses its priority pointer.
                db.execute("UPDATE wf_tasks SET created_at='2000-01-01T00:00:00+00:00' WHERE id=?", (tid,))
                result.append(tid)
        return result

    def runner(self, runtime):
        runner = Orchestrator(self.store, runtime, schedule_answer_reviews=False)
        self.addCleanup(runner.readiness.close)
        runner.readiness.start()
        self.assertTrue(runner.readiness.wait(2))
        return runner

    def assert_still_waiting(self):
        before = self.task()
        self.store.reconcile_processes()
        current = self.task()
        self.assertEqual('waiting_for_user', current['status'])
        for field in ('attempts', 'failure_count', 'checkpoint'):
            self.assertEqual(before[field], current[field])
        self.assertIsNone(self.store.lease_task('must-not-replay', kinds=['apply']))
















    def test_stale_fully_answered_question_wait_is_repaired_once_with_facts_intact(self):
        self.stale_answered_wait()
        before, process = self.task(), self.process()
        facts = self.tables(('wf_questions', 'wf_answer_requests', 'wf_commands', 'wf_submission_attempts'))
        self.assertIn(self.process_id, self.store.reconcile_processes())
        current = self.task()
        self.assertEqual('queued', current['status'])
        self.assertIsNone(current['error'])
        self.assertIsNone(self.process()['block_reason'])
        self.assertEqual(process['revision'] + 1, self.process()['revision'])
        self.assertEqual('queued', next(q for q in self.store.state()['queue'] if q['id'] == self.process_id)['status'])
        for field in ('checkpoint', 'payload', 'attempts', 'failure_count'):
            self.assertEqual(before[field], current[field])
        self.assertEqual(facts, self.tables(tuple(facts)))
        after = self.tables(('wf_tasks', 'wf_processes', 'queue', 'wf_process_events', 'wf_settings'))
        self.assertEqual([], self.store.reconcile_processes())
        self.assertEqual(after, self.tables(tuple(after)))
        self.store = WorkflowStore(self.path, low_watermark=0)
        self.assertEqual([], self.store.reconcile_processes())
        self.assertEqual(after, self.tables(tuple(after)))

    def test_repaired_question_wait_runs_through_normal_orchestrator_once(self):
        self.stale_answered_wait()
        runtime = FairRuntime()
        runner = self.runner(runtime)
        runner.run_once(kinds=['apply'])
        self.assertEqual([('apply', self.task_id)], runtime.operations)
        self.assertEqual('submitted', self.process()['submission_status'])
        runner.run_once(kinds=['apply'])
        self.assertEqual([('apply', self.task_id)], runtime.operations)
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_partial_required_answers_stay_parked(self):
        self.answers(('phone',))
        questions = self.store.questions()
        self.assert_still_waiting()
        self.assertEqual(questions, self.store.questions())
        self.assertEqual('questions_pending', self.process()['block_reason'])

    def test_no_current_question_round_does_not_imply_ready(self):
        self.stale_answered_wait()
        for mutation in ("UPDATE wf_questions SET superseded=1",
                         "UPDATE wf_questions SET superseded=0,form_version='old-form'"):
            with self.subTest(mutation=mutation):
                with self.store.transaction(True) as db:
                    db.execute(mutation)
                self.assert_still_waiting()
    def test_revoked_approval_does_not_repair_answered_wait(self):
        self.stale_answered_wait()
        with self.store.transaction(True) as db:
            db.execute("UPDATE jobs SET decision='pass' WHERE key=?", (self.process()['job_key'],))
        self.assert_still_waiting()
        self.assertEqual('authorization_changed', self.process()['block_reason'])

    def test_other_browser_blocker_is_not_cleared_by_existing_answers(self):
        self.stale_answered_wait()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET error='tool_approval_required' WHERE id=?", (self.task_id,))
            db.execute("UPDATE wf_processes SET block_reason='tool_approval_required' WHERE id=?", (self.process_id,))
        self.assert_still_waiting()
        self.assertEqual('tool_approval_required', self.process()['block_reason'])

    def test_question_repair_requires_preparing_state_and_remaining_retry_budget(self):
        self.stale_answered_wait()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET submission_status='not_started' WHERE id=?", (self.process_id,))
        self.assert_still_waiting()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET submission_status='preparing' WHERE id=?", (self.process_id,))
            db.execute('UPDATE wf_tasks SET failure_count=max_attempts WHERE id=?', (self.task_id,))
        self.assert_still_waiting()

    def test_submission_checkpoint_or_attempt_prevents_question_repair(self):
        self.stale_answered_wait()
        with self.store.transaction(True) as db:
            db.execute('UPDATE wf_tasks SET checkpoint=? WHERE id=?',
                (encode({**self.checkpoint, 'submission_started': True}), self.task_id))
        self.assert_still_waiting()
        with self.store.transaction(True) as db:
            db.execute('UPDATE wf_tasks SET checkpoint=? WHERE id=?', (encode(self.checkpoint), self.task_id))
            db.execute('INSERT INTO wf_submission_attempts(id,process_id,task_id,form_version,started_at) VALUES (?,?,?,?,?)',
                ('fixture-existing-attempt', self.process_id, self.task_id, 'fixture-form', now()))
        self.assert_still_waiting()
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])

    def test_quarantine_prevents_question_repair(self):
        self.stale_answered_wait()
        with self.store.transaction(True) as db:
            db.execute('INSERT INTO wf_resource_locks(resource,task_id,lease_token,quarantined) VALUES (?,?,?,1)',
                ('browser', self.task_id, 'fictional-quarantined-token'))
        self.assert_still_waiting()
        self.assertTrue(self.task()['resource_quarantined'])

    def test_even_unquarantined_resource_lock_prevents_question_repair(self):
        self.stale_answered_wait()
        with self.store.transaction(True) as db:
            db.execute('INSERT INTO wf_resource_locks(resource,task_id,lease_token,quarantined) VALUES (?,?,?,0)',
                ('browser', self.task_id, 'fictional-still-owned-token'))
        self.assert_still_waiting()
        self.assertFalse(self.task()['resource_quarantined'])
        with self.store.transaction() as db:
            self.assertEqual('fictional-still-owned-token', db.execute('SELECT lease_token FROM wf_resource_locks').fetchone()[0])

    def test_process_browser_blocker_is_preserved_when_task_error_is_stale_questions_pending(self):
        self.stale_answered_wait()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET block_reason='tool_approval_required' WHERE id=?", (self.process_id,))
        self.assert_still_waiting()
        self.assertEqual('questions_pending', self.task()['error'])
        self.assertEqual('tool_approval_required', self.process()['block_reason'])







    def test_cancelled_question_process_is_not_resumed(self):
        self.answers()
        self.store.cancel_task_command({'request_id': 'cancel-before-repair', 'task_id': self.task_id})
        self.store.reconcile_processes()
        self.assertEqual('cancelled', self.task()['status'])
        self.assertIsNone(self.store.lease_task('repeat', kinds=['apply']))

    def test_unknown_submission_question_process_is_not_resumed(self):
        self.stale_answered_wait()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET submission_status='submission_unknown' WHERE id=?", (self.process_id,))
        self.assert_still_waiting()
        self.assertEqual('submission_unknown', self.process()['block_reason'])


if __name__ == '__main__':
    unittest.main()
