"""Fictional stopped-turn recovery; never contact an employer or run a model."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import unittest
import uuid

from codex_transport import _replace_private_json
from store import ReviewError
from submission_recovery import requeue_unstarted_submission
import test_codex_runtime as fixtures


class UnstartedSubmissionRecoveryTests(unittest.TestCase):
    setUp = fixtures.CodexRuntimeTests.setUp
    application = fixtures.CodexRuntimeTests.application
    prepared = fixtures.CodexRuntimeTests.prepared
    runtime = fixtures.CodexRuntimeTests.runtime

    def scenario(self):
        task = self.application()
        pid = task['payload']['process_id']
        self.store.checkpoint_task(task['id'], task['lease_token'],
            {'prepared_form_hash': 'fictional-form', 'question_bindings': {'saved': 'occupation'}})
        attempt = self.store.before_submit(pid, task['lease_token'], 'fictional-form')
        self.store.fail_task(task['id'], task['lease_token'], 'submission_unknown', retryable=False)
        self.store.reconcile_processes()
        # The production transport produces exactly this token error after a
        # lost native lease; the scenario excludes any external operation.
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET error='submission_unknown' WHERE id=?", (task['id'],))
        p = self.store.processes()[0]
        thread, turn = str(uuid.uuid4()), str(uuid.uuid4())
        base = int(datetime.now(timezone.utc).timestamp())
        proof = {'version': 1, 'thread_id': thread, 'turn_id': turn, 'turn_status': 'interrupted',
            'stable_snapshots': 2, 'inspector_exit_code': 0, 'started_at': base, 'completed_at': base,
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'items_view': 'full', 'item_count': 1, 'item_type_counts': {'userMessage': 1},
            'external_item_count': 0, 'child_count': 0}
        runs = self.root / 'runs'; runs.mkdir()
        prepare_name = 'application_prepare-' + str(uuid.uuid4())
        submit_name = 'application_submit-' + str(uuid.uuid4())
        lease_digest = hashlib.sha256(task['lease_token'].encode()).hexdigest()
        common = {'task_id': task['id'], 'thread_id': thread, 'lease_digest': lease_digest,
                  'browser': True, 'transport': 'app-server'}
        for name, data in (
            (prepare_name, dict(common, role='application_prepare', status='completed', turn_completed=True,
                browser_verified=True, exit_code=0)),
            (submit_name, dict(common, role='application_submit', status='failed', turn_completed=False,
                browser_verified=False, tools=[], failure={'reason': 'lease_lost', 'turn_id': turn})),
        ):
            (runs / name).mkdir()
            _replace_private_json(runs / name / 'receipt.json', data)
        audit_path = self.root.resolve() / 'stopped.json'
        audit = {'phase': 'quarantine_cleared', 'process_id': pid, 'task_id': task['id'],
            'attempt_id': attempt['attempt_id'], 'submission_replayed': False, 'stopped_turn_proof': proof,
            'restored_record': {'version': 1, 'status': 'ready', 'task_id': task['id'], 'thread_id': thread,
                'receipt': prepare_name, 'lease_digest': lease_digest}}
        _replace_private_json(audit_path, audit)
        with self.store.transaction(True) as db:
            self.store._process_event(db, pid, 'worker_stopped_confirmed', {
                'thread_id': thread, 'turn_id': turn, 'audit': str(audit_path),
                'external_item_count': 0, 'submission_replayed': False}, 'operator')
            before_attempt = dict(db.execute('SELECT * FROM wf_submission_attempts').fetchone())
        args = {'process_id': pid, 'process_revision': p['revision'],
            'task_updated_at': self.store.tasks()[0]['updated_at'], 'attempt_id': attempt['attempt_id'],
            'audit_path': audit_path, 'run_directory': runs, 'authorization': 'Explicit fictional retry approval.'}
        return task, args, audit, before_attempt

    def test_requeues_same_task_preserving_attempt_answers_and_retry_count(self):
        task, args, audit, before = self.scenario()
        failures = self.store.tasks()[0]['failure_count']
        result = requeue_unstarted_submission(self.store, **args)
        self.assertEqual('queued', result['status'])
        p, t = self.store.processes()[0], self.store.tasks()[0]
        self.assertEqual(task['id'], p['task_id'])
        self.assertEqual('preparing', p['submission_status'])
        self.assertIsNone(p['block_reason'])
        self.assertNotIn('submission_started', t['checkpoint'])
        self.assertNotIn('prepared_form_hash', t['checkpoint'])
        self.assertEqual({'saved': 'occupation'}, t['checkpoint']['question_bindings'])
        self.assertEqual(failures, t['failure_count'])
        with self.store.transaction() as db:
            self.assertEqual(before, dict(db.execute('SELECT * FROM wf_submission_attempts').fetchone()))
            event = json.loads(db.execute("SELECT data FROM wf_process_events WHERE type='submission_attempt_reconciled'").fetchone()[0])
            self.assertTrue(event['previous_checkpoint']['submission_started'])
        with self.assertRaises(ReviewError):
            requeue_unstarted_submission(self.store, **args)

    def test_new_attempt_still_requires_fresh_prepare_and_receipt(self):
        task, args, audit, before = self.scenario()
        requeue_unstarted_submission(self.store, **args)
        new = self.store.lease_task('fictional-next-worker', kinds=['apply'])
        new['browser'] = fixtures.BROWSER
        self.store.start_application(args['process_id'], new['lease_token'])
        runtime = self.runtime([self.prepared(), {'outcome': 'unknown'}])
        runtime.apply(self.store, new)
        self.assertEqual([('application_prepare', True), ('application_submit', True)], runtime.transport.calls)
        self.assertEqual('submission_unknown', self.store.processes()[0]['submission_status'])
        with self.store.transaction() as db:
            attempts = [dict(row) for row in db.execute('SELECT * FROM wf_submission_attempts ORDER BY started_at')]
            self.assertEqual(2, len(attempts))
            self.assertEqual(before, attempts[0])
            self.assertNotEqual(before['id'], attempts[1]['id'])

    def test_missing_or_partial_or_external_proof_is_rejected(self):
        task, args, audit, before = self.scenario()
        for change in ({'stable_snapshots': 1}, {'items_view': 'summary'},
                {'item_type_counts': {'userMessage': 1, 'mcpToolCall': 1}, 'item_count': 2},
                {'external_item_count': 1}, {'child_count': 1}, {'turn_status': 'inProgress'}):
            with self.subTest(change=change):
                altered = copy.deepcopy(audit); altered['stopped_turn_proof'].update(change)
                _replace_private_json(args['audit_path'], altered)
                with self.assertRaises(ReviewError):
                    requeue_unstarted_submission(self.store, **args)
        self.assertEqual('submission_unknown', self.store.processes()[0]['submission_status'])

    def test_stale_revision_timestamp_or_attempt_is_rejected(self):
        task, args, audit, before = self.scenario()
        for change in ({'process_revision': 999}, {'task_updated_at': 'old'}, {'attempt_id': str(uuid.uuid4())}):
            with self.subTest(change=change), self.assertRaises(ReviewError):
                requeue_unstarted_submission(self.store, **dict(args, **change))

    def test_recorded_stop_and_no_tool_receipt_are_both_required(self):
        task, args, audit, before = self.scenario()
        receipt_path = next(args['run_directory'].glob('application_submit-*/receipt.json'))
        receipt = json.loads(receipt_path.read_text())
        changed = dict(receipt, tools=[{'server': 'fictional', 'tool': 'click'}])
        _replace_private_json(receipt_path, changed)
        with self.assertRaises(ReviewError):
            requeue_unstarted_submission(self.store, **args)
        _replace_private_json(receipt_path, receipt)
        with self.store.transaction(True) as db:
            db.execute("DELETE FROM wf_process_events WHERE type='worker_stopped_confirmed'")
        with self.assertRaises(ReviewError):
            requeue_unstarted_submission(self.store, **args)

    def test_quarantine_and_withdrawn_approval_remain_hard_gates(self):
        task, args, audit, before = self.scenario()
        with self.store.transaction(True) as db:
            db.execute('INSERT INTO wf_resource_locks VALUES (?,?,?,1)', ('browser', task['id'], 'old-token'))
        with self.assertRaises(ReviewError):
            requeue_unstarted_submission(self.store, **args)
        with self.store.transaction(True) as db:
            db.execute('DELETE FROM wf_resource_locks')
            db.execute("UPDATE jobs SET decision='reject' WHERE key=?", (self.store.processes()[0]['job_key'],))
        with self.assertRaises(ReviewError):
            requeue_unstarted_submission(self.store, **args)

    def test_additional_attempt_or_confirmation_cannot_be_erased(self):
        task, args, audit, before = self.scenario()
        with self.store.transaction(True) as db:
            db.execute('UPDATE wf_submission_attempts SET confirmed_at=? WHERE id=?', ('later', before['id']))
        with self.assertRaises(ReviewError):
            requeue_unstarted_submission(self.store, **args)
        with self.store.transaction(True) as db:
            db.execute('UPDATE wf_submission_attempts SET confirmed_at=NULL')
            db.execute('INSERT INTO wf_submission_attempts SELECT ?,process_id,task_id,form_version,started_at,NULL,NULL '
                       'FROM wf_submission_attempts', (str(uuid.uuid4()),))
        with self.assertRaises(ReviewError):
            requeue_unstarted_submission(self.store, **args)

    def test_other_active_browser_owner_is_preserved(self):
        task, args, audit, before = self.scenario()
        with self.store.transaction(True) as db:
            other = self.store._enqueue_task(db, 'explore', {}, 'fictional-other', resource='browser')
        other_task = self.store.lease_task('other-worker', kinds=['explore'])
        requeue_unstarted_submission(self.store, **args)
        self.assertIsNone(self.store.lease_task('waiter', kinds=['apply']))
        with self.store.transaction() as db:
            lock = db.execute('SELECT * FROM wf_resource_locks').fetchone()
            self.assertEqual(other, lock['task_id'])
            self.assertEqual(other_task['lease_token'], lock['lease_token'])
