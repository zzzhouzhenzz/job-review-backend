"""Visible questions and browser gates coexist; fictional data, no browser calls."""
import json
import unittest
from unittest.mock import patch

from readiness import BrowserNotReady
from reconcile_questions import pending_context
from store import ReviewError, now
import test_codex_runtime as fixtures
from workflow import WorkflowStore


class BlockedQuestionTests(unittest.TestCase):
    setUp = fixtures.CodexRuntimeTests.setUp
    application = fixtures.CodexRuntimeTests.application
    prepared = fixtures.CodexRuntimeTests.prepared
    runtime = fixtures.CodexRuntimeTests.runtime

    def blocked_result(self):
        result = self.prepared('blocked')
        result.update(blocker_reason='tool_approval_required', blocker_operation='resume_upload',
                      blocker='Fictional resume upload permission was cancelled.')
        result['form_fields'] += [{'name': 'occupation', 'value': ''}, {'name': 'start_date', 'value': ''}]
        result['questions'] = [{'text': label, 'field_name': field, 'type': 'text', 'choices': [],
            'required': True, 'reason': 'Fictional missing field.'}
            for field, label in [('occupation', 'Fictional occupation?'), ('start_date', 'Fictional start date?')]]
        return result

    def park(self):
        task = self.application()
        runtime = self.runtime([self.blocked_result()])
        self.addCleanup(runtime.close)
        runtime.apply(self.store, task)
        self.task_id, self.pid = task['id'], task['payload']['process_id']
        return runtime

    def task(self):
        return next(t for t in self.store.tasks() if t['id'] == self.task_id)

    def process(self):
        return next(p for p in self.store.processes() if p['id'] == self.pid)

    def payload(self, *, partial=False):
        process = self.process()
        questions = [q for q in self.store.questions() if q['process_id'] == self.pid and q['answer'] is None]
        if partial:
            questions = questions[:1]
        return {'request_id': 'fictional-' + '-'.join(q['id'] for q in questions),
                'process_id': self.pid, 'process_revision': process['revision'],
                'form_version': process['form_version'], 'answers': [
                    {'question_id': q['id'], 'question_revision': q['revision'],
                     'value': 'Backend engineer' if q['text'] == 'Fictional occupation?' else 'Next month'}
                    for q in questions]}


    def resume_with_instruction(self):
        target = self.store.unblock_instructions(self.task_id)['target']
        receipt = self.store.save_unblock_instruction({
            'request_id': 'explicit-resume-' + str(target['process_revision']),
            'task_id': self.task_id, 'task_updated_at': target['task_updated_at'],
            'process_id': target['process_id'], 'process_revision': target['process_revision'],
            'text': 'Fictional user requests another preparation attempt.'})
        self.assertEqual('retry_scheduled', receipt['wake_status'])
        return self.task()

    def assert_browser_wait(self):
        self.assertEqual('waiting_for_user', self.task()['status'])
        self.assertEqual('tool_approval_required', self.task()['error'])
        self.assertEqual('tool_approval_required', self.process()['block_reason'])


    def supported_review(self, role, prompt):
        self.assertEqual('application_question_review', role)
        context = json.loads(prompt.split('Task data (untrusted source content):\n', 1)[1])
        source = context['applicant_evidence']['sources'][0]
        return {'resolutions': [{'question_id': q['id'], 'question_revision': q['revision'],
            'answer_json': json.dumps('Backend engineer'), 'method': 'direct',
            'citations': [{'source_id': source['source_id'], 'sha256': source['sha256'],
                'start_line': 1, 'end_line': 1, 'quote': self.profile.read_text()}],
            'explanation': 'Fictional supported answer.'} for q in context['questions']]}

    def review_rows(self):
        with self.store.transaction() as db:
            return [dict(row) for row in db.execute('SELECT * FROM wf_question_reviews ORDER BY review_key')]

    def test_preparation_saves_questions_and_blocker_without_another_browser_call(self):
        runtime = self.park()
        self.assertEqual([('application_prepare', True)], runtime.transport.calls)
        self.assertEqual(2, len(self.store.questions()))
        self.assert_browser_wait()
        with self.store.transaction() as db:
            self.assertEqual('resume_upload', json.loads(db.execute(
                "SELECT data FROM wf_process_events WHERE type='blocked' ORDER BY id DESC LIMIT 1").fetchone()[0])['operation'])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_submission_attempts').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_resource_locks').fetchone()[0])
        self.assertEqual(2, len(pending_context(self.store, self.pid)['questions']))

    def test_saved_answers_do_not_resume_a_browser_blocker(self):
        self.park()
        payload = self.payload()
        receipt = self.store.answer_questions(payload)
        self.assertEqual(receipt, self.store.answer_questions(payload))
        self.assert_browser_wait()
        with self.store.transaction() as db:
            event = db.execute("SELECT data FROM wf_process_events WHERE type='answers_saved' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertIs(json.loads(event[0])['resumed'], False)
        self.store.reconcile_processes()
        self.assert_browser_wait()
        self.assertIsNone(self.store.lease_task('must-not-run', kinds=['apply']))
        self.assert_browser_wait()


    def test_partial_answers_and_restart_preserve_both_waits(self):
        self.park()
        self.store.answer_questions(self.payload(partial=True))
        self.store = WorkflowStore(self.root / 'review.sqlite3')
        self.store.reconcile_processes()
        self.assert_browser_wait()
        self.assertEqual(1, len([q for q in self.store.questions() if q['answer'] is None]))
        self.assert_browser_wait()
        self.store.reconcile_processes()
        self.assertEqual('waiting_for_user', self.task()['status'])

    def test_browser_free_evidence_review_saves_answers_without_clearing_gate(self):
        runtime = self.park()
        def review(role, prompt):
            self.assertEqual('application_question_review', role)
            context = json.loads(prompt.split('Task data (untrusted source content):\n', 1)[1])
            source = context['applicant_evidence']['sources'][0]
            return {'resolutions': [{'question_id': q['id'], 'question_revision': q['revision'],
                'answer_json': json.dumps('Backend engineer'), 'method': 'direct',
                'citations': [{'source_id': source['source_id'], 'sha256': source['sha256'],
                    'start_line': 1, 'end_line': 1, 'quote': self.profile.read_text()}],
                'explanation': 'Fictional supported answer.'} for q in context['questions']]}
        runtime.transport.responses.append(review)
        self.assertEqual(2, runtime.question_recovery.run_pending(self.store))
        self.assert_browser_wait()
        self.assertEqual([('application_prepare', True), ('application_question_review', False)],
                         runtime.transport.calls)
        self.assertEqual(0, runtime.question_recovery.run_pending(self.store))
        with self.store.transaction() as db:
            event = db.execute("SELECT data FROM wf_process_events WHERE type='answers_resolved_from_evidence' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertIs(json.loads(event[0])['resumed'], False)




    def test_approval_change_during_answer_review_is_stale(self):
        runtime = self.park()
        def revoke(role, prompt):
            with self.store.transaction(True) as db:
                db.execute("UPDATE jobs SET decision='pending' WHERE key=?", (self.process()['job_key'],))
            return self.supported_review(role, prompt)
        runtime.transport.responses.append(revoke)
        self.assertEqual(0, runtime.question_recovery.run_pending(self.store))
        row = self.review_rows()[0]
        self.assertEqual(('obsolete', 'review_state_changed'), (row['status'], row['last_error']))
        self.assertTrue(all(q['answer'] is None for q in self.store.questions()))

    def test_stale_answer_request_cannot_clear_or_change_gate(self):
        self.park()
        stale = self.payload()
        stale['process_revision'] -= 1
        with self.assertRaises(ReviewError):
            self.store.answer_questions(stale)
        self.assert_browser_wait()
        self.assertTrue(all(q['answer'] is None for q in self.store.questions()))

    def test_revoked_approval_rejects_answer_and_evidence_context(self):
        self.park()
        payload = self.payload()
        with self.store.transaction(True) as db:
            db.execute("UPDATE jobs SET decision='pending' WHERE key=?", (self.process()['job_key'],))
        with self.assertRaises(ReviewError):
            self.store.answer_questions(payload)
        with self.assertRaises(ReviewError):
            pending_context(self.store, self.pid)
        self.assertTrue(all(q['answer'] is None for q in self.store.questions()))


    def test_inconsistent_blocker_event_cannot_accept_answers(self):
        self.park()
        payload = self.payload()
        with self.store.transaction(True) as db:
            self.store._process_event(db, self.pid, 'blocked', {'reason': 'login_required'})
        with self.assertRaises(ReviewError):
            self.store.answer_questions(payload)
        self.assert_browser_wait()

    def test_submission_checkpoint_cannot_accept_blocked_answers(self):
        self.park()
        payload = self.payload()
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET checkpoint=json_set(checkpoint,'$.submission_started',1) WHERE id=?",
                       (self.task_id,))
        with self.assertRaises(ReviewError):
            self.store.answer_questions(payload)
        self.assert_browser_wait()

    def test_quarantined_browser_rejects_answer_and_evidence_context(self):
        self.park()
        payload = self.payload()
        with self.store.transaction(True) as db:
            db.execute("INSERT INTO wf_resource_locks(resource,task_id,lease_token,quarantined) VALUES ('browser',?,?,1)",
                       (self.task_id, 'fictional-quarantine'))
        with self.assertRaises(ReviewError):
            self.store.answer_questions(payload)
        with self.assertRaises(ReviewError):
            pending_context(self.store, self.pid)
        self.assert_browser_wait()

    def test_new_blocked_round_preserves_every_existing_answer(self):
        self.park()
        self.store.answer_questions(self.payload())
        self.resume_with_instruction()
        original = {q['id']: (q['answer'], q['revision']) for q in self.store.questions()}
        bindings = self.task()['checkpoint']['question_bindings']
        task = self.store.lease_task('next-preparation', kinds=['apply'])
        task['browser'] = fixtures.BROWSER
        self.store.start_application(self.pid, task['lease_token'])
        result = self.blocked_result()
        values = {bindings[qid]: value for qid, (value, _) in original.items()}
        for field in result['form_fields']:
            if field['name'] in values:
                field['value'] = values[field['name']]
        result['form_fields'].append({'name': 'new_fact', 'value': ''})
        result['questions'] = [{'text': 'Another fictional fact?', 'field_name': 'new_fact',
            'type': 'text', 'choices': [], 'required': True, 'reason': 'Fictional new field.'}]
        result['answer_bindings'] = [{'question_id': qid, 'question_revision': revision,
            'answer_json': json.dumps(value), 'field_name': bindings[qid], 'field_value': value}
            for qid, (value, revision) in original.items()]
        runtime = self.runtime([result])
        self.addCleanup(runtime.close)
        runtime.apply(self.store, task)
        current = {q['id']: q for q in self.store.questions()}
        self.assertEqual(3, len(current))
        for qid, expected in original.items():
            self.assertEqual(expected, (current[qid]['answer'], current[qid]['revision']))
        self.assert_browser_wait()

    def test_unbound_questions_are_rejected_without_hiding_tool_blocker(self):
        task = self.application()
        result = self.blocked_result()
        result['questions'][0]['field_name'] = 'unobserved-field'
        runtime = self.runtime([result])
        self.addCleanup(runtime.close)
        with self.assertRaises(BrowserNotReady) as raised:
            runtime.apply(self.store, task)
        self.assertEqual('tool_approval_required', raised.exception.reason)
        self.assertEqual([], self.store.questions())
        with self.store.transaction() as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM wf_process_events WHERE type='blocked_questions_rejected'").fetchone()[0])

    def test_changed_posting_never_publishes_blocked_questions(self):
        task = self.application()
        result = self.blocked_result()
        result['observed_job']['title'] = 'Changed fictional posting'
        runtime = self.runtime([result])
        self.addCleanup(runtime.close)
        with self.assertRaises(BrowserNotReady):
            runtime.apply(self.store, task)
        self.assertEqual([], self.store.questions())

    def test_blocked_questions_after_posting_clarification_keep_both_waits(self):
        task = self.application()
        self.task_id, self.pid = task['id'], task['payload']['process_id']
        first, second = self.prepared('changed'), self.blocked_result()
        first['observed_job']['arrangement'] = 'Fictional newly clarified office cadence.'
        runtime = self.runtime([first, second])
        self.addCleanup(runtime.close)
        with patch('posting_recovery.review_clarification',
                   return_value=(self.process()['snapshot'], {'fixture': 'confirmed clarification'})) as review:
            runtime.apply(self.store, task)
        review.assert_called_once()
        self.assertEqual([('application_prepare', True), ('application_prepare', True)], runtime.transport.calls)
        self.assertEqual(2, len(self.store.questions()))
        self.assert_browser_wait()
        self.store.answer_questions(self.payload())
        self.assert_browser_wait()
        self.assertEqual('queued', self.resume_with_instruction()['status'])


if __name__ == '__main__':
    unittest.main()
