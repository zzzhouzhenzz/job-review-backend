"""Inactive readiness recovery with fictional tasks and mocked native evidence."""
import fcntl
import hashlib
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from readiness_recovery import MAX_RECOVERIES, recover_interrupted_readiness
from store import ReviewError, encode, now
from test_store import catalog
from workflow import WorkflowStore


class ReadinessRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'review.sqlite3'
        self.store = WorkflowStore(self.path, low_watermark=0)
        self.store.grant_project_consent({'request_id': 'fixture-consent', 'policy_version': 1, 'accepted': True})
        self.store.import_catalog(catalog(), 'Fictional interrupted readiness')
        card = self.store.feed()['cards'][0]
        receipt = self.store.receive_decision({'request_id': 'fictional-apply', 'action': 'apply',
            'card_id': card['id'], 'card_revision': card['revision'], 'selected': [card['jobs'][0]['key']]})
        self.task_id, self.process_id = receipt['task_ids'][0], receipt['process_ids'][0]
        task = self.store.lease_task('fictional-prior-worker', kinds=['apply'])
        self.store.start_application(self.process_id, task['lease_token'])
        self.checkpoint = {'question_bindings': {'fixture-question': 'phone_type'},
                           'resume_upload_evidence': {'filename': 'fictional-resume.pdf', 'verified': True}}
        self.store.checkpoint_task(self.task_id, task['lease_token'], self.checkpoint)
        process = self.store.ask_questions(self.process_id, task['lease_token'], 'fictional-form', [
            {'id': 'fixture-question', 'text': 'Phone device type?', 'type': 'text', 'choices': [],
             'required': True, 'scope': 'application', 'reason': 'Fictional field'}])
        self.store.answer_questions({'request_id': 'fixture-answer', 'process_id': self.process_id,
            'process_revision': process['revision'], 'form_version': process['form_version'],
            'answers': [{'question_id': 'fixture-question', 'question_revision': 1, 'value': 'Mobile'}]})
        task = self.store.lease_task('fictional-interrupted-worker', kinds=['apply'])
        self.token = task['lease_token']
        with self.store.transaction(True) as db:
            db.execute("UPDATE wf_tasks SET failure_count=1,lease_until='2000-01-01T00:00:00+00:00' WHERE id=?",
                       (self.task_id,))
        self.store.recover_expired_tasks()
        self.thread_id = '11111111-1111-4111-8111-111111111111'
        self.turn_id = '22222222-2222-4222-8222-222222222222'
        self.other_id = '33333333-3333-4333-8333-333333333333'
        self.run_name = 'linkedin_session-44444444-4444-4444-8444-444444444444'
        self.runs = self.root / 'agent-runs'
        self.sessions = self.runs / '_browser_sessions'
        self.sessions.mkdir(parents=True, mode=0o700)
        self.run = self.runs / self.run_name
        self.run.mkdir(mode=0o700)
        self.binding_path = self.sessions / (self.task_id + '.readiness.json')
        self.journal_path = self.sessions / (self.task_id + '.readiness-recovery.json')
        self.application_path = self.sessions / (self.task_id + '.json')
        self.receipt_path = self.run / 'receipt.json'
        digest = hashlib.sha256(self.token.encode()).hexdigest()
        self.binding = {'version': 1, 'task_id': self.task_id, 'status': 'active',
                        'lease_digest': digest, 'receipt': self.run_name, 'thread_id': self.thread_id}
        self.receipt = {'version': 1, 'role': 'linkedin_session', 'transport': 'app-server',
                        'status': 'failed', 'task_id': self.task_id, 'lease_digest': digest,
                        'thread_id': self.thread_id, 'failure': {'reason': 'lease_lost', 'turn_id': self.turn_id}}
        self.proof = {'version': 1, 'thread_id': self.thread_id, 'turn_id': self.turn_id,
                      'turn_status': 'interrupted', 'started_at': 100, 'completed_at': 110,
                      'items_view': 'full', 'item_count': 2,
                      'item_type_counts': {'userMessage': 1, 'reasoning': 1},
                      'external_item_count': 0, 'child_count': 0, 'observed_at': now(),
                      'stable_snapshots': 2, 'inspector_exit_code': 0}
        self.write(self.binding_path, self.binding)
        self.write(self.receipt_path, self.receipt)
        self.transport = SimpleNamespace(browser_transport='app-server', _closed=threading.Event(),
            run_directory=self.runs, binary=Path('/usr/bin/true'), cwd=self.root)
        inspector = patch('codex_appserver.inspect_inactive_turn', return_value=self.proof)
        self.inspect = inspector.start()
        self.addCleanup(inspector.stop)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding='utf-8')
        path.chmod(0o600)

    def task(self):
        return next(task for task in self.store.tasks() if task['id'] == self.task_id)

    def process(self):
        return next(process for process in self.store.processes() if process['id'] == self.process_id)

    def database_snapshot(self):
        with self.store.transaction() as db:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            return {name: [tuple(row) for row in db.execute('SELECT * FROM "' + name + '" ORDER BY rowid')]
                    for name in names}

    def assert_unchanged_no_recovery(self, *, inspect_called=None):
        before = self.database_snapshot()
        binding_before = self.binding_path.read_bytes() if self.binding_path.exists() else None
        self.inspect.reset_mock()
        self.assertIsNone(recover_interrupted_readiness(self.store, self.transport))
        self.assertEqual(before, self.database_snapshot())
        if binding_before is not None:
            self.assertEqual(binding_before, self.binding_path.read_bytes())
        self.assertFalse(self.journal_path.exists())
        self.assertTrue(self.task()['resource_quarantined'])
        if inspect_called is not None:
            self.assertEqual(inspect_called, self.inspect.called)

    def test_exact_terminal_proof_requeues_same_task_and_credits_only_no_action_failure(self):
        before, process, questions = self.task(), self.process(), self.store.questions()
        with self.store.transaction() as db:
            decisions = [tuple(row) for row in db.execute('SELECT * FROM wf_commands ORDER BY id')]
            attempts = [tuple(row) for row in db.execute('SELECT * FROM wf_submission_attempts')]
        result = recover_interrupted_readiness(self.store, self.transport)
        self.assertEqual(self.task_id, result['id'])
        self.assertEqual('retry_wait', result['status'])
        self.assertFalse(result['resource_quarantined'])
        self.assertEqual(before['attempts'], result['attempts'])
        self.assertEqual(before['failure_count'] - 1, result['failure_count'])
        self.assertEqual({**self.checkpoint, 'readiness_recovery_count': 1}, result['checkpoint'])
        self.assertEqual(before['payload'], result['payload'])
        self.assertEqual('readiness_interrupted', result['error'])
        delay = (datetime.fromisoformat(result['available_at']) - datetime.fromisoformat(result['updated_at'])).total_seconds()
        self.assertAlmostEqual(5, delay, delta=1)
        self.assertEqual(process['decision_id'], self.process()['decision_id'])
        self.assertEqual(process['snapshot'], self.process()['snapshot'])
        self.assertEqual(process['submission_status'], self.process()['submission_status'])
        self.assertEqual(process['revision'] + 1, self.process()['revision'])
        self.assertEqual(questions, self.store.questions())
        self.assertFalse(self.binding_path.exists())
        journal = json.loads(self.journal_path.read_text())
        self.assertEqual(self.binding, journal['binding'])
        self.assertEqual(self.proof, journal['proof'])
        self.assertEqual(0, self.journal_path.stat().st_mode & 0o077)
        self.inspect.assert_called_once_with(binary=self.transport.binary, cwd=self.root,
            thread_id=self.thread_id, turn_id=self.turn_id, closed=self.transport._closed)
        with self.store.transaction() as db:
            self.assertEqual(decisions, [tuple(row) for row in db.execute('SELECT * FROM wf_commands ORDER BY id')])
            self.assertEqual(attempts, [tuple(row) for row in db.execute('SELECT * FROM wf_submission_attempts')])
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM wf_tasks').fetchone()[0])
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM wf_resource_locks').fetchone()[0])
        self.assertEqual('readiness_recovered', self.process()['events'][-1]['type'])
        self.assertIsNone(recover_interrupted_readiness(self.store, self.transport))
        self.assertEqual(1, self.task()['failure_count'])
        self.assertEqual(1, self.inspect.call_count)

    def test_missing_native_proof_never_releases_quarantine(self):
        self.inspect.return_value = None
        self.assert_unchanged_no_recovery(inspect_called=True)

    def test_native_tools_or_partial_history_result_none_is_not_success(self):
        # The inspector returns None for tool items, incomplete pages, or running turns.
        self.inspect.return_value = None
        self.assert_unchanged_no_recovery(inspect_called=True)
        self.assertEqual('worker_lease_lost', self.task()['error'])

    def test_missing_binding_or_receipt_never_invents_proof(self):
        self.binding_path.unlink()
        self.assert_unchanged_no_recovery(inspect_called=False)
        self.write(self.binding_path, self.binding)
        self.receipt_path.unlink()
        with self.assertLogs('readiness_recovery', level='WARNING'):
            self.assert_unchanged_no_recovery(inspect_called=False)

    def test_binding_task_status_digest_thread_and_run_name_must_match(self):
        invalid = [
            {'version': 2}, {'task_id': self.other_id}, {'status': 'ready'}, {'lease_digest': '0' * 64},
            {'thread_id': self.other_id}, {'receipt': '../receipt.json'},
            {'receipt': 'application_prepare-44444444-4444-4444-8444-444444444444'}, {'receipt': None},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.write(self.binding_path, {**self.binding, **changes})
                self.assert_unchanged_no_recovery(inspect_called=False)
        self.write(self.binding_path, self.binding)

    def test_receipt_requires_failed_appserver_readiness_and_exact_task_lease_thread_turn(self):
        invalid = [
            {'role': 'application_prepare'}, {'transport': 'exec'}, {'status': 'completed'},
            {'task_id': self.other_id}, {'lease_digest': '0' * 64}, {'thread_id': self.other_id},
            {'thread_id': 'not-a-thread'}, {'failure': {'reason': 'other', 'turn_id': self.turn_id}},
            {'failure': {'reason': 'lease_lost', 'turn_id': 'not-a-turn'}}, {'failure': {}},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.write(self.receipt_path, {**self.receipt, **changes})
                self.assert_unchanged_no_recovery(inspect_called=False)
        self.write(self.receipt_path, self.receipt)

    def test_private_metadata_is_required(self):
        for path in (self.sessions, self.binding_path, self.receipt_path):
            with self.subTest(path=path.name):
                prior_mode = path.stat().st_mode & 0o777
                path.chmod(prior_mode | 0o044)
                self.assert_unchanged_no_recovery(inspect_called=False)
                path.chmod(prior_mode)

    def test_symlink_binding_is_rejected(self):
        target = self.root / 'outside-binding.json'
        self.write(target, self.binding)
        self.binding_path.unlink()
        self.binding_path.symlink_to(target)
        with self.assertLogs('readiness_recovery', level='WARNING'):
            self.assert_unchanged_no_recovery(inspect_called=False)

    def test_active_application_binding_blocks_readiness_recovery(self):
        for status in ('active', 'uncertain', 'failed'):
            with self.subTest(status=status):
                self.write(self.application_path, {'version': 1, 'task_id': self.task_id,
                    'status': status, 'thread_id': self.other_id})
                self.assert_unchanged_no_recovery(inspect_called=False)

    def test_ready_application_binding_is_preserved(self):
        binding = {'version': 1, 'task_id': self.task_id, 'status': 'ready', 'thread_id': self.other_id}
        self.write(self.application_path, binding)
        content = self.application_path.read_bytes()
        self.assertIsNotNone(recover_interrupted_readiness(self.store, self.transport))
        self.assertEqual(content, self.application_path.read_bytes())

    def test_started_submission_never_recovers(self):
        with self.store.transaction(True) as db:
            db.execute('UPDATE wf_tasks SET checkpoint=? WHERE id=?',
                (encode({**self.checkpoint, 'submission_started': True}), self.task_id))
        self.assert_unchanged_no_recovery()

    def test_any_existing_submission_attempt_is_preserved_and_not_replayed(self):
        with self.store.transaction(True) as db:
            db.execute('INSERT INTO wf_submission_attempts(id,process_id,task_id,form_version,started_at) VALUES (?,?,?,?,?)',
                       ('fictional-prior-attempt', self.process_id, self.task_id, 'fictional-form', now()))
        self.assert_unchanged_no_recovery()

    def test_unknown_or_submitted_process_never_recovers(self):
        for status in ('submission_unknown', 'submitted'):
            with self.subTest(status=status):
                with self.store.transaction(True) as db:
                    db.execute('UPDATE wf_processes SET submission_status=? WHERE id=?', (status, self.process_id))
                self.assert_unchanged_no_recovery()

    def test_revoked_approval_and_changed_posting_are_not_overridden(self):
        with self.store.transaction(True) as db:
            row = db.execute('SELECT key,data FROM jobs WHERE key=?', (self.process()['job_key'],)).fetchone()
            job_key, original_data = row['key'], row['data']
            db.execute("UPDATE jobs SET decision='pass' WHERE key=?", (job_key,))
        with self.assertLogs('readiness_recovery', level='WARNING'):
            self.assert_unchanged_no_recovery()
        with self.store.transaction(True) as db:
            changed = json.loads(original_data)
            changed['title'] = 'Different fictional job'
            db.execute("UPDATE jobs SET decision='approve',data=? WHERE key=?", (encode(changed), job_key))
        with self.assertLogs('readiness_recovery', level='WARNING'):
            self.assert_unchanged_no_recovery()

    def test_missing_consent_or_required_answer_remains_parked(self):
        with self.store.transaction(True) as db:
            grant = tuple(db.execute('SELECT * FROM wf_project_consent').fetchone())
            db.execute('DELETE FROM wf_project_consent')
        self.assert_unchanged_no_recovery()
        with self.store.transaction(True) as db:
            db.execute('INSERT INTO wf_project_consent VALUES (?,?,?,?,?,?,?)', grant)
            db.execute('UPDATE wf_questions SET answer=NULL,answered_at=NULL')
        self.assert_unchanged_no_recovery()

    def test_task_change_during_native_probe_prevents_stale_commit(self):
        def changed(**kwargs):
            with self.store.transaction(True) as db:
                db.execute('UPDATE wf_tasks SET updated_at=? WHERE id=?', (now(), self.task_id))
            return self.proof
        self.inspect.side_effect = changed
        before = self.task()
        self.assertIsNone(recover_interrupted_readiness(self.store, self.transport))
        current = self.task()
        self.assertTrue(current['resource_quarantined'])
        self.assertNotEqual(before['updated_at'], current['updated_at'])
        for field in ('checkpoint', 'failure_count', 'attempts', 'status', 'error'):
            self.assertEqual(before[field], current[field])
        self.assertTrue(self.binding_path.exists())
        self.assertFalse(self.journal_path.exists())

    def test_resource_token_change_during_probe_preserves_new_owner(self):
        def changed(**kwargs):
            with self.store.transaction(True) as db:
                db.execute('UPDATE wf_resource_locks SET lease_token=? WHERE task_id=?', ('new-owner-token', self.task_id))
            return self.proof
        self.inspect.side_effect = changed
        self.assertIsNone(recover_interrupted_readiness(self.store, self.transport))
        with self.store.transaction() as db:
            self.assertEqual('new-owner-token', db.execute('SELECT lease_token FROM wf_resource_locks').fetchone()[0])
        self.assertTrue(self.task()['resource_quarantined'])
        self.assertTrue(self.binding_path.exists())

    def test_recovery_counter_is_finite_and_invalid_types_fail_closed(self):
        for value in (MAX_RECOVERIES + 1, -1, True, '1', None):
            with self.subTest(counter=value):
                with self.store.transaction(True) as db:
                    db.execute('UPDATE wf_tasks SET checkpoint=? WHERE id=?',
                        (encode({**self.checkpoint, 'readiness_recovery_count': value}), self.task_id))
                self.assert_unchanged_no_recovery()

    def test_exhausted_recovery_counter_releases_proven_inactive_lock_without_retry(self):
        with self.store.transaction(True) as db:
            db.execute('UPDATE wf_tasks SET checkpoint=? WHERE id=?',
                (encode({**self.checkpoint, 'readiness_recovery_count': MAX_RECOVERIES}), self.task_id))
        result = recover_interrupted_readiness(self.store, self.transport)
        self.assertEqual('failed', result['status'])
        self.assertFalse(result['resource_quarantined'])
        self.assertEqual(MAX_RECOVERIES + 1, result['checkpoint']['readiness_recovery_count'])
        self.assertEqual('readiness_recovery_exhausted', result['error'])
        self.assertIsNone(self.store.lease_task('another-worker', kinds=['apply']))
        with self.store.transaction(True) as db:
            explorer = self.store._enqueue_task(db, 'explore', {}, 'different-work', resource='browser')
        self.assertEqual(explorer, self.store.lease_task('other-work', kinds=['explore'])['id'])

    def test_final_allowed_recovery_preserves_prior_failures_and_backoff(self):
        with self.store.transaction(True) as db:
            db.execute('UPDATE wf_tasks SET checkpoint=? WHERE id=?',
                (encode({**self.checkpoint, 'readiness_recovery_count': MAX_RECOVERIES - 1}), self.task_id))
        result = recover_interrupted_readiness(self.store, self.transport)
        self.assertEqual(MAX_RECOVERIES, result['checkpoint']['readiness_recovery_count'])
        self.assertEqual(1, result['failure_count'])
        delay = (datetime.fromisoformat(result['available_at']) - datetime.fromisoformat(result['updated_at'])).total_seconds()
        self.assertAlmostEqual(5 * 2 ** (MAX_RECOVERIES - 1), delay, delta=1)

    def test_exhausted_prior_failure_budget_is_not_reset(self):
        with self.store.transaction(True) as db:
            db.execute('UPDATE wf_tasks SET failure_count=max_attempts+1 WHERE id=?', (self.task_id,))
        result = recover_interrupted_readiness(self.store, self.transport)
        self.assertEqual('failed', result['status'])
        self.assertEqual(result['max_attempts'], result['failure_count'])
        self.assertEqual('readiness_recovery_exhausted', result['error'])
        self.assertEqual('readiness_recovery_exhausted', self.process()['block_reason'])
        self.assertIsNone(self.store.lease_task('another-worker', kinds=['apply']))

    def test_journal_allows_restart_after_unlink_and_database_rollback_without_double_credit(self):
        before = self.database_snapshot()
        with patch.object(self.store, '_release_task', side_effect=ReviewError('Fictional commit-path failure')):
            with self.assertLogs('readiness_recovery', level='WARNING'):
                self.assertIsNone(recover_interrupted_readiness(self.store, self.transport))
        self.assertEqual(before, self.database_snapshot())
        self.assertFalse(self.binding_path.exists())
        self.assertTrue(self.journal_path.exists())
        self.store = WorkflowStore(self.path, low_watermark=0)
        self.inspect.return_value = None
        self.assertIsNone(recover_interrupted_readiness(self.store, self.transport))
        self.assertEqual(before, self.database_snapshot())
        self.inspect.return_value = self.proof
        result = recover_interrupted_readiness(self.store, self.transport)
        self.assertEqual('retry_wait', result['status'])
        self.assertEqual(1, result['failure_count'])
        self.assertEqual(1, result['checkpoint']['readiness_recovery_count'])
        self.assertEqual(3, self.inspect.call_count)
        self.store = WorkflowStore(self.path, low_watermark=0)
        self.assertIsNone(recover_interrupted_readiness(self.store, self.transport))
        self.assertEqual(1, self.task()['failure_count'])
        self.assertEqual(3, self.inspect.call_count)

    def test_concurrent_session_file_lock_prevents_native_probe_and_all_mutation(self):
        path = self.sessions / (self.task_id + '.lock')
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_unchanged_no_recovery(inspect_called=False)
        finally:
            os.close(descriptor)
        self.assertIsNotNone(recover_interrupted_readiness(self.store, self.transport))

    def shared_registry(self):
        from shared_browser_session import REGISTRY, initial_registry
        self.transport.browser_session_policy = 'shared'
        self.receipt['browser'] = True
        self.write(self.receipt_path, self.receipt)
        self.binding_path.unlink()
        path = self.sessions / REGISTRY
        self.write(path, initial_registry(self.binding))
        return path

    def test_shared_readiness_recovery_preserves_native_identity(self):
        from shared_browser_session import read_registry, verified_record
        path = self.shared_registry()
        result = recover_interrupted_readiness(self.store, self.transport)
        self.assertEqual('retry_wait', result['status'])
        record = read_registry(path)['generations'][0]['record']
        self.assertEqual(self.thread_id, record['thread_id'])
        self.assertEqual('ready', record['status'])
        self.assertEqual(self.proof, record['recovery_proof'])
        verified_record(self.runs, record)

    def test_shared_recovery_database_rollback_can_retry_without_losing_session(self):
        from shared_browser_session import read_registry
        path = self.shared_registry()
        before = self.database_snapshot()
        with patch.object(self.store, '_release_task', side_effect=ReviewError('Fictional failure')):
            with self.assertLogs('readiness_recovery', level='WARNING'):
                self.assertIsNone(recover_interrupted_readiness(self.store, self.transport))
        self.assertEqual(before, self.database_snapshot())
        self.assertEqual('ready', read_registry(path)['generations'][0]['record']['status'])
        self.assertEqual('retry_wait', recover_interrupted_readiness(self.store, self.transport)['status'])
        self.assertIsNone(recover_interrupted_readiness(self.store, self.transport))
        self.assertEqual(1, self.task()['checkpoint']['readiness_recovery_count'])

    def test_shared_lock_prevents_readiness_recovery_during_another_turn(self):
        self.shared_registry()
        descriptor = os.open(self.sessions / '_shared.lock', os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIsNone(recover_interrupted_readiness(self.store, self.transport))
            self.inspect.assert_not_called()
        finally:
            os.close(descriptor)

    def test_legacy_application_used_for_readiness_keeps_its_session_after_recovery(self):
        from codex_transport import _BrowserSession
        self.transport.browser_session_policy = 'shared'
        self.receipt['browser'] = True
        self.write(self.receipt_path, self.receipt)
        self.binding_path.rename(self.application_path)
        result = recover_interrupted_readiness(self.store, self.transport)
        self.assertEqual('retry_wait', result['status'])
        lease = SimpleNamespace(task_id=self.task_id, token=self.token)
        session = _BrowserSession(self.runs, lease)
        try:
            self.assertEqual(self.thread_id, session.thread_id)
        finally:
            session.close()

    def test_closed_or_non_appserver_transport_never_probes(self):
        self.transport.browser_transport = 'exec'
        self.assert_unchanged_no_recovery(inspect_called=False)
        self.transport.browser_transport = 'app-server'
        self.transport._closed.set()
        self.assert_unchanged_no_recovery(inspect_called=False)

    def test_transport_closes_during_probe_without_releasing_browser(self):
        def closed(**kwargs):
            self.transport._closed.set()
            return self.proof
        self.inspect.side_effect = closed
        self.assert_unchanged_no_recovery(inspect_called=True)

    def test_mismatched_native_thread_or_turn_proof_is_rejected(self):
        for field in ('thread_id', 'turn_id'):
            with self.subTest(field=field):
                self.inspect.return_value = {**self.proof, field: self.other_id}
                self.assert_unchanged_no_recovery(inspect_called=True)

    def test_native_proof_requires_interrupted_full_stable_zero_external_work(self):
        changes = ({'turn_status': 'running'}, {'turn_status': 'completed'},
                   {'items_view': 'summary'}, {'stable_snapshots': 1}, {'inspector_exit_code': 1},
                   {'external_item_count': 1}, {'external_item_count': False},
                   {'child_count': 1}, {'child_count': False})
        for change in changes:
            with self.subTest(change=change):
                self.inspect.return_value = {**self.proof, **change}
                self.assert_unchanged_no_recovery(inspect_called=True)

    def test_malformed_private_receipt_failure_is_no_proof(self):
        for failure in ('invalid failure', ['lease_lost'], 1):
            with self.subTest(failure=failure):
                self.write(self.receipt_path, {**self.receipt, 'failure': failure})
                self.assert_unchanged_no_recovery(inspect_called=False)

    def test_non_object_private_binding_and_receipt_fail_closed(self):
        for path, valid in ((self.binding_path, self.binding), (self.receipt_path, self.receipt)):
            for value in ([], None, 'invalid record'):
                with self.subTest(path=path.name, value=value):
                    self.write(path, value)
                    self.assert_unchanged_no_recovery(inspect_called=False)
                self.write(path, valid)


if __name__ == '__main__':
    unittest.main()
