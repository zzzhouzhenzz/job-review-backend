"""Fictional blocked tasks and local HTTP only; no browser or production data."""
import json
import tempfile
import unittest
from pathlib import Path

from store import ReviewError, encode, now
from test_store import catalog
from workflow import WorkflowStore


def blocked_application(store, *, reason='login_required'):
    store.grant_project_consent({'request_id': 'consent', 'policy_version': 1, 'accepted': True})
    card = store.feed()['cards'][0]
    receipt = store.receive_decision({'request_id': 'apply', 'action': 'apply',
        'card_id': card['id'], 'card_revision': card['revision'], 'selected': [card['jobs'][0]['key']]})
    task_id, process_id = receipt['task_ids'][0], receipt['process_ids'][0]
    task = store.lease_task('fixture', kinds=['apply'])
    store.start_application(process_id, task['lease_token'])
    with store.transaction(True) as db:
        db.execute("UPDATE wf_processes SET block_reason=?,revision=revision+1 WHERE id=?", (reason, process_id))
        db.execute("UPDATE queue SET status='needs_input' WHERE id=?", (process_id,))
        store._release_task(db, task_id, 'waiting_for_user', error=reason)
        store._process_event(db, process_id, 'blocked',
            {'reason': reason, 'operation': 'sign_in' if reason == 'login_required' else 'resume_upload',
             'detail': 'Fictional stopped browser blocker'})
    return task_id, process_id


class UnblockInstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'fictional.sqlite3'
        self.store = WorkflowStore(self.path, low_watermark=0)
        self.store.import_catalog(catalog(), 'Fictional instruction tests')
        self.task_id, self.process_id = blocked_application(self.store)

    def payload(self, **changes):
        target = self.store.unblock_instructions(self.task_id)['target']
        return {'request_id': 'instruction-1', 'text': 'I attached the approved resume. Please check.',
                **{key: target[key] for key in ('task_id', 'task_updated_at', 'process_id', 'process_revision')},
                **changes}

    def task(self):
        return next(t for t in self.store.tasks() if t['id'] == self.task_id)

    def table(self, name):
        with self.store.transaction() as db:
            return [tuple(row) for row in db.execute('SELECT * FROM ' + name)]

    def denial(self):
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET block_reason='tool_approval_required' WHERE id=?", (self.process_id,))
            db.execute("UPDATE wf_tasks SET error='tool_approval_required' WHERE id=?", (self.task_id,))
            self.store._process_event(db, self.process_id, 'blocked',
                {'reason': 'tool_approval_required', 'operation': 'form_navigation', 'detail': 'Fictional stopped denial'})

    def lease_for_guidance(self):
        with self.store.transaction(True) as db:
            self.store._resume_task(db, self.task_id)
        return self.store.lease_task('explicit-fixture-resume', kinds=['apply'])

    def test_save_for_login_is_manual_and_preserves_required_gates(self):
        before = self.task()
        preserved = {name: self.table(name) for name in (
            'wf_processes', 'wf_questions', 'wf_submission_attempts', 'wf_project_consent', 'queue')}
        receipt = self.store.save_unblock_instruction(self.payload())
        self.assertEqual(('manual_review', 'saved_only'), (receipt['mode'], receipt['wake_status']))
        for field in ('status', 'attempts', 'failure_count', 'checkpoint', 'error', 'payload'):
            self.assertEqual(before[field], self.task()[field])
        for name, rows in preserved.items():
            self.assertEqual(rows, self.table(name), name)
        self.assertIsNone(self.store.lease_task('no-automatic-resume', kinds=['apply']))

    def test_guidance_is_available_only_after_explicit_resume_and_live_lease(self):
        receipt = self.store.save_unblock_instruction(self.payload())
        self.assertIsNone(self.store.lease_task('idle', kinds=['apply']))
        task = self.lease_for_guidance()
        self.store.heartbeat_task(task['id'], task['lease_token'])
        context = self.store.unblock_instruction_context(task['id'], task['lease_token'], purpose='readiness')
        self.assertEqual(receipt['instruction_id'], context['instruction_id'])
        self.assertIn('never replay', context['boundary'])

    def test_same_request_replays_after_state_changes_but_conflicting_body_fails(self):
        payload = self.payload()
        receipt = self.store.save_unblock_instruction(payload)
        self.lease_for_guidance()
        self.assertEqual(receipt, self.store.save_unblock_instruction(payload))
        with self.assertRaises(ReviewError) as caught:
            self.store.save_unblock_instruction({**payload, 'text': 'Changed'})
        self.assertEqual('request_id_conflict', caught.exception.reason)
        self.assertEqual(1, len(self.table('wf_unblock_instructions')))

    def test_stale_task_process_and_wrong_target_are_atomic(self):
        for changes in ({'task_updated_at': 'old'}, {'process_revision': 99},
                        {'process_revision': True}, {'process_id': 'another-process'}):
            with self.subTest(changes=changes), self.assertRaises(ReviewError) as caught:
                self.store.save_unblock_instruction(self.payload(**changes))
            self.assertEqual('stale_revision', caught.exception.reason)
        self.assertEqual([], self.table('wf_unblock_instructions'))

    def test_active_task_returns_temporary_busy(self):
        payload = self.payload()
        self.lease_for_guidance()
        with self.assertRaises(ReviewError) as caught:
            self.store.save_unblock_instruction(payload)
        self.assertEqual('task_busy', caught.exception.reason)

    def test_latest_instruction_supersedes_prior_guidance(self):
        self.store.save_unblock_instruction(self.payload())
        latest = self.store.save_unblock_instruction(self.payload(request_id='instruction-2', text='Correction: check sign-in first.'))
        history = self.store.unblock_instructions(self.task_id)['instructions']
        self.assertEqual(['superseded', 'current'], [row['status'] for row in history])
        task = self.lease_for_guidance()
        self.assertEqual(latest['instruction_id'], self.store.unblock_instruction_context(
            task['id'], task['lease_token'], purpose='readiness')['instruction_id'])

    def test_revision_or_new_blocker_expires_runtime_context(self):
        for mutation in ('revision', 'blocker'):
            with self.subTest(mutation=mutation):
                self.store.save_unblock_instruction(self.payload(request_id='instruction-' + mutation))
                task = self.lease_for_guidance()
                with self.store.transaction(True) as db:
                    if mutation == 'revision':
                        db.execute('UPDATE wf_processes SET revision=revision+1 WHERE id=?', (self.process_id,))
                    else:
                        self.store._process_event(db, self.process_id, 'blocked', {'reason': 'tool_approval_required'})
                self.assertIsNone(self.store.unblock_instruction_context(task['id'], task['lease_token'], purpose='readiness'))
                with self.store.transaction(True) as db:
                    self.store._release_task(db, task['id'], 'waiting_for_user', error='login_required')

    def test_questions_are_saved_only_and_never_become_answers(self):
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET block_reason='questions_pending' WHERE id=?", (self.process_id,))
            db.execute("UPDATE wf_tasks SET error='questions_pending' WHERE id=?", (self.task_id,))
        waits = self.table('wf_tasks')
        receipt = self.store.save_unblock_instruction(self.payload(text='I agree to all new terms.'))
        self.assertEqual(('questions_pending', 'saved_only'), (receipt['mode'], receipt['wake_status']))
        self.assertEqual([], self.table('wf_answer_requests'))
        self.assertEqual([(r[0], r[2]) for r in waits], [(r[0], r[2]) for r in self.table('wf_tasks')])
        self.assertEqual('waiting_for_user', self.task()['status'])

    def test_unknown_submission_is_reconciliation_only(self):
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_processes SET submission_status='submission_unknown' WHERE id=?", (self.process_id,))
            db.execute("UPDATE wf_tasks SET error='submission_unknown',checkpoint=? WHERE id=?",
                       (encode({'submission_started': True}), self.task_id))
        waits = self.table('wf_tasks')
        receipt = self.store.save_unblock_instruction(self.payload(text='Try submitting again.'))
        self.assertEqual(('reconciliation_only', 'saved_only'), (receipt['mode'], receipt['wake_status']))
        self.assertEqual([(r[0], r[2]) for r in waits], [(r[0], r[2]) for r in self.table('wf_tasks')])
        self.assertEqual('waiting_for_user', self.task()['status'])
        self.assertIsNone(self.store.lease_task('no-submission-replay', kinds=['apply']))

    def test_inactive_approval_is_not_overridden_by_free_text(self):
        with self.store.transaction(True) as db:
            db.execute("UPDATE jobs SET decision='pass'")
        with self.assertRaises(ReviewError) as caught:
            self.store.save_unblock_instruction(self.payload(text='Apply anyway.'))
        self.assertEqual('authorization_changed', caught.exception.reason)

    def test_quarantine_and_exhausted_budget_save_without_waking(self):
        for field in ('quarantine', 'budget'):
            with self.subTest(field=field):
                with self.store.transaction(True) as db:
                    if field == 'quarantine':
                        db.execute("INSERT INTO wf_resource_locks VALUES ('browser',?,'old',1)", (self.task_id,))
                    else:
                        db.execute('DELETE FROM wf_resource_locks')
                        db.execute('UPDATE wf_tasks SET failure_count=max_attempts WHERE id=?', (self.task_id,))
                waits = self.table('wf_tasks')
                receipt = self.store.save_unblock_instruction(self.payload(request_id=field))
                self.assertEqual(('manual_review', 'saved_only'), (receipt['mode'], receipt['wake_status']))
                self.assertEqual([(r[0], r[2]) for r in waits], [(r[0], r[2]) for r in self.table('wf_tasks')])

    def test_explore_instruction_is_saved_without_waking_and_new_blocker_expires_it(self):
        with self.store.transaction(True) as db:
            task_id = self.store._enqueue_task(db, 'explore', {'company_id': 'fictional'}, 'fictional-explore', resource='browser')
            self.store._release_task(db, task_id, 'waiting_for_user', error='login_required')
        self.task_id = task_id
        receipt = self.store.save_unblock_instruction(self.payload())
        self.assertEqual('saved_only', receipt['wake_status'])
        self.assertIsNone(self.store.lease_task('idle', kinds=['explore']))
        self.assertEqual('current', self.store.unblock_instructions(task_id)['instructions'][0]['status'])
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET error='challenge_required' WHERE id=?", (task_id,))
        self.assertEqual('stale', self.store.unblock_instructions(task_id)['instructions'][0]['status'])

    def test_validation_rejects_extra_authority_empty_or_oversize_text(self):
        for changes in ({'authority': 'user'}, {'text': '   '}, {'text': 'x' * 4001}):
            with self.subTest(changes=changes), self.assertRaises(ReviewError):
                self.store.save_unblock_instruction(self.payload(**changes))
        self.assertEqual([], self.table('wf_unblock_instructions'))

    def test_instruction_queues_same_denied_task_once_and_reaches_ordinary_preparation(self):
        self.denial()
        task_before = self.task()
        payload = self.payload(text='Use Back to inspect the existing form, then stop at any missing fact.')
        receipt = self.store.save_unblock_instruction(payload)
        self.assertEqual(('preparation_retry', 'retry_scheduled'), (receipt['mode'], receipt['wake_status']))
        self.assertEqual('queued', self.task()['status'])
        for key in ('attempts', 'failure_count', 'max_attempts', 'checkpoint'):
            self.assertEqual(task_before[key], self.task()[key])
        with self.store.transaction() as db:
            self.assertEqual('tool_approval_required', self.store._process(db, self.process_id)['block_reason'])
        self.assertEqual(receipt, self.store.save_unblock_instruction(payload))
        with self.assertRaises(ReviewError):
            self.store.save_unblock_instruction(self.payload(request_id='new-queued-request'))
        task = self.store.lease_task('normal-worker', kinds=['apply'])
        self.assertEqual(self.task_id, task['id'])
        self.assertEqual(receipt['instruction_id'], self.store.unblock_instruction_context(
            task['id'], task['lease_token'], purpose='readiness')['instruction_id'])
        self.store.start_application(self.process_id, task['lease_token'])
        self.assertEqual(receipt['instruction_id'], self.store.unblock_instruction_context(
            task['id'], task['lease_token'], purpose='application')['instruction_id'])
        self.store.heartbeat_task(task['id'], task['lease_token'])
        self.assertIsNotNone(self.store.unblock_instruction_context(task['id'], task['lease_token'], purpose='application'))
        with self.store.transaction(True) as db:
            self.store._process_event(db, self.process_id, 'answers_saved', {'answers': []}, 'user')
        self.assertIsNone(self.store.unblock_instruction_context(task['id'], task['lease_token'], purpose='application'))
        self.assertEqual(1, len(self.store.tasks()))

    def test_denied_task_with_missing_required_answer_is_saved_only(self):
        self.denial()
        with self.store.transaction(True) as db:
            stamp = now()
            db.execute('''INSERT INTO wf_questions
                (id,process_id,form_version,text,type,choices,required,scope,reason,created_at,updated_at)
                VALUES ('legal',?,'form','Do you agree?','text','[]',1,'application','specific consent',?,?)''',
                (self.process_id, stamp, stamp))
        waits = self.table('wf_tasks')
        receipt = self.store.save_unblock_instruction(self.payload(text='Choose yes to all terms.'))
        self.assertEqual(('manual_review', 'saved_only'), (receipt['mode'], receipt['wake_status']))
        self.assertEqual('waiting_for_user', self.task()['status'])
        self.assertEqual([(r[0], r[2]) for r in waits], [(r[0], r[2]) for r in self.table('wf_tasks')])
        self.assertIsNone(self.store.questions()[0]['answer'])

    def test_any_submission_attempt_blocks_preparation_retry_even_without_checkpoint(self):
        self.denial()
        with self.store.transaction(True) as db:
            db.execute('''INSERT INTO wf_submission_attempts
                (id,process_id,task_id,form_version,started_at) VALUES ('existing',?,?,'form',?)''',
                (self.process_id, self.task_id, now()))
        receipt = self.store.save_unblock_instruction(self.payload(text='Try again.'))
        self.assertEqual(('reconciliation_only', 'saved_only'), (receipt['mode'], receipt['wake_status']))
        self.assertEqual('waiting_for_user', self.task()['status'])

    def test_new_revision_does_not_masquerade_as_own_preparing_transition(self):
        self.denial()
        self.store.save_unblock_instruction(self.payload())
        task = self.store.lease_task('normal-worker', kinds=['apply'])
        with self.store.transaction(True) as db:
            db.execute('UPDATE wf_processes SET revision=revision+1 WHERE id=?', (self.process_id,))
            self.store._process_event(db, self.process_id, 'answers_saved', {'answers': []}, 'user')
        self.assertIsNone(self.store.unblock_instruction_context(task['id'], task['lease_token'], purpose='readiness'))
        self.store.start_application(self.process_id, task['lease_token'])
        self.assertIsNone(self.store.unblock_instruction_context(task['id'], task['lease_token'], purpose='application'))


class UnblockInstructionHTTPTests(unittest.TestCase):
    def setUp(self):
        from test_authenticated_server import AuthenticatedServerTests
        self.http = AuthenticatedServerTests()
        self.http.setUp()
        self.addCleanup(self.http.tearDown)
        self.store = self.http.server.store
        self.task_id, self.process_id = blocked_application(self.store)
        self.url = '/api/v1/unblock-instructions?task_id=' + self.task_id

    def test_authenticated_read_write_replay_and_capability(self):
        status, _, body = self.http.request(self.url)
        self.assertEqual(200, status)
        self.assertTrue(body['complete'])
        target = body['target']
        payload = {'request_id': 'http-instruction', 'text': 'Please check the existing tab.',
            **{key: target[key] for key in ('task_id', 'task_updated_at', 'process_id', 'process_revision')}}
        status, _, receipt = self.http.request('/api/v1/unblock-instructions', payload)
        self.assertEqual(200, status)
        self.assertEqual('saved_only', receipt['wake_status'])
        self.assertEqual(receipt, self.http.request('/api/v1/unblock-instructions', payload)[2])
        self.assertTrue(self.http.request('/api/v1/capabilities')[2]['capabilities']['unblock_instructions'])
        history = self.http.request(self.url)[2]
        self.assertEqual('current', history['instructions'][0]['status'])
        self.assertNotIn('lease_token', json.dumps(history))

    def test_auth_origin_token_and_query_errors_do_not_write(self):
        self.assertEqual(401, self.http.request(self.url, headers={'Authorization': None})[0])
        for headers in ({'Authorization': None}, {'Origin': 'https://evil.invalid'}, {'X-Review-Token': 'wrong'}):
            status = self.http.request('/api/v1/unblock-instructions', {}, headers=headers)[0]
            self.assertIn(status, (401, 403))
        for path in ('/api/v1/unblock-instructions', self.url + '&task_id=duplicate', self.url + '&extra=bad'):
            self.assertEqual(400, self.http.request(path)[0])
        with self.store.transaction() as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_unblock_instructions').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
